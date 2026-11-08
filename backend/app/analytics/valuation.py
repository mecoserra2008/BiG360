"""Daily valuation metrics -> valuation_daily (owner: analytics-scoring). Job id `valuation_daily`.

Per security and trading date D (USD on both sides, ARCHITECTURE §8):
    shares_out      PIT issuer shares (latest-period row with knowledge_date <= D), split-adjusted to D with the
                    security's split_factor (shares_D = shares * sf(D) / sf(basis); basis = period end of a regulator
                    count, or its knowledge date when the filing already restated it for a split after the period
                    end (shares_pit.split_restated_basis, compared with the previous count), the fetch date of a
                    Yahoo count, which Yahoo restates to the current split basis; without a previous count, skipped
                    when a reported count already looks post-split, i.e. shares_out / shares_diluted_weighted ~ the
                    split ratio). Coverage v3 A6 (app.analytics.shares_pit): regulator counts win over Yahoo ones,
                    filer-tagged x1000 share scales are repaired against net income / EPS when the traded volume
                    confirms the repair. A6 review: the split factor applied to counts is the SHARE split factor
                    (`share_split_factors`: genuine splits only, not Yahoo's spin-off price adjustments such as FDX
                    1.241), and one share event per knowledge date (latest period; latest_known) makes the count
                    independent of the batch a security is valued in.
    market_cap_usd  shares_out * close_usd / adr_ratio  (ADR: shares are underlying ordinary shares; adr_ratio =
                    underlying shares per ADR from securities_master, else the ADR seeds (`with_seed_adr_ratios`),
                    default 1 - withheld when the ADR's volume contradicts 1:1, A6).
                    Issuer-level (all classes ~ total shares x this class price).
    EV              mcap + total_debt + preferred_equity + minority_interest - cash_and_st_investments (USD, PIT)
    multiples       TTM USD fundamentals known at D (period_end within 400 days, FY fallback for issuers without
                    TTM). P/E is computed issuer-level as mcap / net income to common (== price / EPS, but immune to
                    share-class / ADR / split mismatches).
    gp_to_ev        gross profit TTM / EV (Novy-Marx quality-per-unit-of-valuation; additive column v2)

Helpers reused by ratios (Altman Z mcap()) and scoring: `universe_frame`, `security_meta`, `daily_market_cap`,
`fundamental_snapshots`.

Params: universe (mvp|full, default mvp), start (ISO date; default: since/last 15 days in nightly mode),
        mode = latest (default, last 15 days) | history (from start, default 2010-01-01), tickers.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta, timezone

import polars as pl

from app.analytics.shares_pit import isin_ids
from app.core.config import Settings, get_settings
from app.core.storage import scan_table, write_table

UNIVERSES = {"mvp": ("mvp_us", "mvp_global"), "full": ("full", "mvp_us", "mvp_global", "watch")}
SIZE_LARGE, SIZE_MID = 10.0e9, 2.0e9
STALE_DAYS = 400
SHARES_STALE_DAYS = 800
MCAP_MAX = 3.0e13

FUND_COLS = [
    "revenue", "gross_profit", "ebit", "ebitda", "net_income", "net_income_common", "net_income_parent", "fcf",
    "dividends_paid", "preferred_dividends", "buybacks", "stock_issuance", "total_debt", "preferred_equity",
    "minority_interest", "cash_and_st_investments", "total_equity_parent", "tangible_book", "goodwill",
    "intangible_assets", "ffo", "real_estate_depreciation", "depreciation_amortization", "impairment",
    "gain_on_property_sales",
]


# ------------------------------------------------------------------------------------------------ universe / meta


def universe_frame(universe: str | Sequence[str] = "mvp", as_of: date | None = None,
                   settings: Settings | None = None, tickers: Iterable[str] | None = None) -> pl.DataFrame:
    """security_id, issuer_id of securities in the universe (valid at as_of when given; any time otherwise)."""
    ids = UNIVERSES.get(universe, (universe,)) if isinstance(universe, str) else tuple(universe)
    um = scan_table("universe_membership", settings).filter(pl.col("universe_id").is_in(list(ids)))
    if as_of is not None:
        um = um.filter((pl.col("valid_from") <= as_of) | pl.col("valid_from").is_null(),
                       pl.col("valid_to").is_null() | (pl.col("valid_to") >= as_of))
    sec = scan_table("securities_master", settings).select("security_id", "issuer_id", "display_ticker")
    out = um.select("security_id").unique().join(sec, on="security_id", how="inner").collect()
    if tickers:
        t = {x.upper() for x in tickers}
        out = out.filter(pl.col("display_ticker").str.to_uppercase().is_in(list(t)))
    return out.select("security_id", "issuer_id").unique()


def security_meta(security_ids: Iterable[str] | None = None, settings: Settings | None = None) -> pl.DataFrame:
    sec = scan_table("securities_master", settings).select(
        "security_id", "issuer_id", "security_type", "adr_ratio", "is_primary_security", "trading_currency",
        "display_ticker", "underlying_security_id")
    if security_ids is not None:
        sec = sec.filter(pl.col("security_id").is_in(list(security_ids)))
    return sec.collect()


def _prices(security_ids: Sequence[str], start: date, end: date | None, settings: Settings | None,
            cols: Sequence[str] = ("close", "close_usd", "split_factor")) -> pl.DataFrame:
    lf = scan_table("prices_daily", settings).filter(isin_ids("security_id", security_ids),
                                                      pl.col("date") >= start)
    if end is not None:
        lf = lf.filter(pl.col("date") <= end)
    return (lf.select("security_id", "date", *cols).collect()
            .unique(subset=["security_id", "date"], keep="last").sort("security_id", "date"))


# ------------------------------------------------------------------------------------------------ PIT snapshots


def latest_known(rows: pl.DataFrame, entity: str = "issuer_id", date_col: str = "period_end",
                 kd_col: str = "knowledge_date") -> pl.DataFrame:
    """Keep only rows that are, at their own knowledge date, the latest-period row known for the entity.

    A subsequent backward as-of join on `knowledge_date` then returns, for any date D, the row with the greatest
    period_end among rows with knowledge_date <= D (latest version for that period). Late-filed history rows
    (older period_end than already known) are dropped.

    One row per (entity, knowledge_date): a filing that first reports several periods at once (late filers, first
    filings: FY23 + FY24 + FY25 on one date) keeps only its latest period, so the as-of join never picks among tied
    knowledge dates (A6 review: CAAS / SLGB / MNY share counts depended on the batch they were valued in). Stable
    sorts throughout: rows equal on all three keys keep their input order and the last one wins.
    """
    if rows.is_empty():
        return rows
    r = rows.sort(entity, kd_col, date_col, maintain_order=True)
    r = r.with_columns(pl.col(date_col).cum_max().over(entity).alias("__cm"))
    r = r.filter(pl.col(date_col) == pl.col("__cm")).drop("__cm")
    return r.unique(subset=[entity, kd_col], keep="last", maintain_order=True)


def fundamental_snapshots(issuer_ids: Sequence[str], basis: str = "USD", settings: Settings | None = None,
                          cols: Sequence[str] = FUND_COLS) -> dict[str, pl.DataFrame]:
    """{'TTM': frame, 'FY': frame}: latest-known rows (see latest_known) with the requested item columns."""
    lf = scan_table("fundamentals_wide", settings)
    names = lf.collect_schema().names()
    want = [c for c in cols if c in names]
    base = (lf.filter(pl.col("currency_basis") == basis, pl.col("period_kind").is_in(["TTM", "FY"]),
                      isin_ids("issuer_id", issuer_ids))
            .select("issuer_id", "period_kind", "period_end", "knowledge_date", "dq_score", *want).collect())
    for c in cols:
        if c not in base.columns:
            base = base.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    return {k: latest_known(base.filter(pl.col("period_kind") == k).drop("period_kind")) for k in ("TTM", "FY")}


def shares_events(issuer_ids: Sequence[str], settings: Settings | None = None,
                  skip_unit_guard: Iterable[str] = ()) -> pl.DataFrame:
    """PIT share counts: issuer_id, period_end, knowledge_date, shares, shares_w, basis_date (split basis of the count),
    src (regulator | yahoo | wide), unit_fix. Coverage v3 A6: from fundamentals_canonical with regulator precedence,
    the Yahoo fetch-date split basis and the volume-confirmed x1000 share-scale guard (app.analytics.shares_pit);
    issuers without canonical share rows fall back to the fundamentals_wide pivot (basis = period end). The split
    restatement of a regulator count (basis = knowledge date) is decided per security in daily_market_cap."""
    from app.analytics.shares_pit import share_events  # noqa: PLC0415

    ids = list(issuer_ids)
    ev = share_events(ids, settings, skip_unit_guard)
    missing = sorted(set(ids) - set(ev.get_column("issuer_id").unique().to_list()))
    if missing:
        legacy = _shares_events_wide(missing, settings)
        if legacy.height:
            ev = pl.concat([ev, legacy.with_columns(pl.col("period_end").alias("basis_date"), pl.lit("wide").alias("src"),
                                                    pl.lit(0, pl.Int8).alias("unit_fix")).select(ev.columns)],
                           how="vertical_relaxed")
    return ev


def _shares_events_wide(issuer_ids: Sequence[str], settings: Settings | None = None) -> pl.DataFrame:
    """Pre-A6 share counts from fundamentals_wide: issuer_id, period_end, knowledge_date, shares, shares_w."""
    lf = scan_table("fundamentals_wide", settings)
    names = lf.collect_schema().names()
    so = pl.col("shares_outstanding") if "shares_outstanding" in names else pl.lit(None, pl.Float64)
    sd = pl.col("shares_diluted_weighted") if "shares_diluted_weighted" in names else pl.lit(None, pl.Float64)
    sb = pl.col("shares_basic_weighted") if "shares_basic_weighted" in names else pl.lit(None, pl.Float64)
    df = (lf.filter(pl.col("currency_basis") == "LOCAL", isin_ids("issuer_id", issuer_ids),
                    pl.col("period_kind").is_in(["TTM", "FY", "Q", "H"]))
          .select("issuer_id", "period_end", "knowledge_date", "period_kind",
                  so.cast(pl.Float64).alias("so"), sd.cast(pl.Float64).alias("sd"), sb.cast(pl.Float64).alias("sb"))
          .collect())
    df = df.with_columns(
        pl.when(pl.col("so") > 0).then(pl.col("so")).otherwise(pl.coalesce(pl.col("sd"), pl.col("sb"))).alias("shares"),
        pl.coalesce(pl.col("sd"), pl.col("sb")).alias("shares_w"),
    ).filter(pl.col("shares") > 0)
    # one row per (issuer, period_end, knowledge_date): prefer rows with a reported shares_outstanding
    df = (df.sort("issuer_id", "period_end", "knowledge_date", pl.col("so").is_not_null())
          .unique(subset=["issuer_id", "period_end", "knowledge_date"], keep="last"))
    return latest_known(df.select("issuer_id", "period_end", "knowledge_date", "shares", "shares_w"))


def _asof(left: pl.DataFrame, right: pl.DataFrame, by: str, left_on: str, right_on: str, suffix: str = "") -> pl.DataFrame:
    if right.is_empty():
        extra = [c for c in right.columns if c != by]
        return left.with_columns([pl.lit(None, dtype=right.schema[c]).alias(c + suffix) for c in extra])
    r = right.sort(by, right_on)
    if suffix:
        r = r.rename({c: c + suffix for c in r.columns if c != by})
        right_on = right_on + suffix
    return left.sort(by, left_on).join_asof(r, left_on=left_on, right_on=right_on, by=by, strategy="backward",
                                            check_sortedness=False)


#: coverage v3 A6: an ADR / GDR without a known ratio is valued 1:1 only while its traded volume agrees with that
#: (rolling median ADS volume / underlying shares >= this). Listed 1:1 ADRs trade ~2e-4 .. 3e-3 of their shares a day;
#: TLK (100 ordinary per ADS) shows 8.6e-6, AKTX (2000:1) 3e-7, CHT (10:1) 2.4e-5, VEON (25:1) 8e-5 -> market cap
#: withheld (availability code `suppressed`) until app.ingestion.adr_ratios_sec / seeds/adr_ratios.csv give the ratio
#: (the major 1:1 ADRs are curated; every ratio the SEC sample found for an unknown ADR was != 1)
ADR_UNKNOWN_MIN_TURNOVER = 5e-4
ADR_VOLUME_SESSIONS = 60


def _suppress_unknown_adr_ratio(d: pl.DataFrame, meta: pl.DataFrame, start: date, end: date | None,
                                settings: Settings) -> pl.DataFrame:
    unk = meta.filter(pl.col("security_type").is_in(["adr", "gdr"]) & pl.col("adr_ratio").is_null()) \
        .get_column("security_id").to_list()
    if not unk or d.is_empty():
        return d
    vol = _prices(unk, start - timedelta(days=ADR_VOLUME_SESSIONS * 2), end, settings, cols=("volume",))
    if vol.is_empty():
        return d
    vol = (vol.filter(pl.col("volume") > 0).sort("security_id", "date")
           .with_columns(pl.col("volume").rolling_median(ADR_VOLUME_SESSIONS, min_samples=20).over("security_id")
                         .alias("__mv"))
           .select("security_id", pl.col("date").alias("__vd"), "__mv").drop_nulls())
    if vol.is_empty():
        return d
    j = d.sort("security_id", "date").join_asof(vol.sort("security_id", "__vd"), left_on="date", right_on="__vd",
                                                by="security_id", strategy="backward", check_sortedness=False)
    bad = pl.col("security_id").is_in(unk) & (pl.col("__mv") / pl.col("shares_out") < ADR_UNKNOWN_MIN_TURNOVER)
    return j.with_columns(pl.when(bad.fill_null(False)).then(None).otherwise(pl.col("market_cap_usd"))
                          .alias("market_cap_usd")).drop("__vd", "__mv")


def with_seed_adr_ratios(meta: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    """ADR / GDR rows without securities_master.adr_ratio take the seed ratio (seeds/adr_ratios.csv curated, else
    seeds/adr_ratios_sec.csv from SEC cover pages; app.normalize.identity_universe.load_adr_ratios) right away, before
    the next identity_resolve writes it to the master (A6 review: the suppression below withheld ADRs whose ratio the
    SEC seed already had)."""
    if meta.is_empty() or "adr_ratio" not in meta.columns:
        return meta
    is_dr = pl.col("security_type").is_in(["adr", "gdr"])
    if meta.filter(is_dr & pl.col("adr_ratio").is_null()).is_empty():
        return meta
    try:
        from app.normalize.identity_universe import adr_ticker_key, load_adr_ratios  # noqa: PLC0415

        ratios = load_adr_ratios(settings)
    except Exception:  # noqa: BLE001 - seeds unreadable: master values only
        return meta
    if not ratios:
        return meta
    keys = meta.get_column("display_ticker").to_list()
    seed = pl.Series("__seed", [ratios.get(adr_ticker_key(k)) if k else None for k in keys], pl.Float64)
    return (meta.with_columns(seed).with_columns(
        pl.when(is_dr & pl.col("adr_ratio").is_null()).then(pl.col("__seed")).otherwise(pl.col("adr_ratio"))
        .cast(pl.Float64).alias("adr_ratio")).drop("__seed"))


def share_split_factors(meta: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, date, split_factor] on the SHARE basis: the cumulative factor of the genuine split steps of each
    security's price split factor (app.analytics.shares_pit module doc 6), full history (not capped at `end`: a
    Yahoo fetch date after `end` must still see its factor). ADR / GDR lines are checked against their underlying."""
    from app.analytics import shares_pit as SP  # noqa: PLC0415

    ids = meta.get_column("security_id").to_list()
    dr = (meta.filter(pl.col("security_type").is_in(["adr", "gdr"]) & pl.col("underlying_security_id").is_not_null())
          .select("security_id", pl.col("underlying_security_id").alias("underlying"))
          if "underlying_security_id" in meta.columns else pl.DataFrame())
    und = [x for x in (dr.get_column("underlying").to_list() if dr.height else []) if x not in set(ids)]
    bars = _prices(ids + und, date(1990, 1, 1), None, settings, cols=("split_factor", "volume"))
    if bars.is_empty():
        return bars.select("security_id", "date", "split_factor")
    steps = SP.split_steps(bars.select("security_id", "date", "split_factor"))
    cls = SP.classify_split_steps(steps, bars.select("security_id", "date", "volume"),
                                  SP.polygon_splits(sorted({*steps.get_column("security_id").to_list()}), settings)
                                  if steps.height else None, dr if dr.height else None)
    # ADR / GDR lines without a known underlying: never moved by an ADS split step
    lone_dr = set(meta.filter(pl.col("security_type").is_in(["adr", "gdr"])).get_column("security_id").to_list()) - (
        set(dr.get_column("security_id").to_list()) if dr.height else set())
    if lone_dr and cls.height:
        cls = cls.with_columns(pl.when(pl.col("security_id").is_in(sorted(lone_dr))).then(False)
                               .otherwise(pl.col("genuine")).alias("genuine"))
    return SP.share_split_factor(bars.filter(pl.col("security_id").is_in(ids)), cls)


def daily_market_cap(securities: pl.DataFrame, start: date, end: date | None = None,
                     settings: Settings | None = None) -> pl.DataFrame:
    """security_id, issuer_id, date, close, close_usd, shares_out, market_cap_usd (see module doc)."""
    s = settings or get_settings()
    if securities.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "issuer_id": pl.Utf8, "date": pl.Date})
    meta = with_seed_adr_ratios(security_meta(securities["security_id"].to_list(), s), s)
    px = _prices(meta["security_id"].to_list(), start - timedelta(days=0), end, s)
    if px.is_empty():
        return px.with_columns(pl.lit(None, pl.Utf8).alias("issuer_id"), pl.lit(None, pl.Float64).alias("shares_out"),
                               pl.lit(None, pl.Float64).alias("market_cap_usd"))
    px = px.join(meta.select("security_id", "issuer_id", "security_type", "adr_ratio"), on="security_id", how="left")
    is_dr = pl.col("security_type").is_in(["adr", "gdr"])
    dr_issuers = meta.filter(is_dr).get_column("issuer_id").unique().to_list()   # EPS may be per ADS: no unit guard
    sh = shares_events(meta["issuer_id"].unique().to_list(), s, skip_unit_guard=dr_issuers)
    # split factor at the share count's split basis: its period end, the knowledge date for a regulator count already
    # restated for a split filed after the period (A6 review: MLI 2:1, ALIT 1:20), or for a Yahoo count the day it was
    # fetched (Yahoo restates counts to the current split basis; A6 8766.T 15:1 split counted twice). Loaded without
    # the `end` cap: a fetch date after `end` must still see its split factor (A6 review: end-capped windows gave 15x).
    from app.analytics.shares_pit import split_restated_basis  # noqa: PLC0415

    ssf = share_split_factors(meta, s)
    ev = meta.select("security_id", "issuer_id").join(sh, on="issuer_id", how="inner").drop("issuer_id")
    # deterministic event order whatever the batch (A6 review: the as-of join must not depend on the join order)
    ev = ev.sort("security_id", "knowledge_date", "period_end", "src", "shares", maintain_order=True)
    ev = split_restated_basis(ev, ssf)
    px = px.join(ssf.rename({"split_factor": "__ssf"}), on=["security_id", "date"], how="left")
    d = _asof(px, ev.rename({"period_end": "shares_pe", "knowledge_date": "shares_kd", "basis_date": "shares_basis",
                             "sf_basis": "sf_pe"}), "security_id", "date", "shares_kd")
    # only genuine share splits move the count (shares_pit module doc 6: FDX's 1.241 spin-off factor does not)
    m = (pl.col("__ssf").fill_null(1.0) / pl.col("sf_pe")).fill_null(1.0)
    # no previous count to compare with (basis_rule null): a reported count that already looks post-split against its
    # own weighted count is not multiplied again
    already = ((pl.col("src") != "yahoo") & pl.col("basis_rule").is_null()
               & (((pl.col("shares") / pl.col("shares_w")) / m - 1).abs() < 0.2))
    mult = pl.when((m - 1).abs() < 1e-6).then(1.0).when(already.fill_null(False)).then(1.0).otherwise(m)
    adr = pl.when(is_dr).then(pl.col("adr_ratio").fill_null(1.0)).otherwise(1.0)
    fresh = (pl.col("date") - pl.col("shares_pe")).dt.total_days() <= SHARES_STALE_DAYS
    d = d.with_columns(pl.when(fresh).then(pl.col("shares") * mult).otherwise(None).alias("shares_out"))
    d = d.with_columns((pl.col("shares_out") * pl.col("close_usd") / adr).alias("market_cap_usd"))
    d = _suppress_unknown_adr_ratio(d, meta, start, end, s)
    d = d.with_columns(pl.when((pl.col("market_cap_usd") > 0) & (pl.col("market_cap_usd") < MCAP_MAX))
                       .then(pl.col("market_cap_usd")).otherwise(None).alias("market_cap_usd"))
    return d.select("security_id", "issuer_id", "date", "close", "close_usd", "split_factor", "shares_out",
                    "market_cap_usd").sort("security_id", "date")


def issuer_mcap_at(issuer_dates: pl.DataFrame, settings: Settings | None = None,
                   end: date | None = None) -> pl.DataFrame:
    """issuer_id, date -> market_cap_usd as of date (primary security; last price <= date within 10 days).
    `end`: last price date loaded (default: the latest date asked; ratios._mcap_at_vintages passes one end for
    all its issuer chunks so chunked calls equal one call)."""
    if issuer_dates.is_empty():
        return issuer_dates.with_columns(pl.lit(None, pl.Float64).alias("market_cap_usd"))
    meta = security_meta(None, settings).filter(pl.col("issuer_id").is_in(issuer_dates["issuer_id"].unique().to_list()))
    prim = meta.sort("is_primary_security").unique(subset=["issuer_id"], keep="last").select("security_id", "issuer_id")
    start = issuer_dates["date"].min()
    mc = daily_market_cap(prim, start - timedelta(days=15), max(end or issuer_dates["date"].max(), issuer_dates["date"].max()),
                          settings)
    if mc.is_empty():
        return issuer_dates.with_columns(pl.lit(None, pl.Float64).alias("market_cap_usd"))
    r = mc.filter(pl.col("market_cap_usd").is_not_null()).select("issuer_id", pl.col("date").alias("mc_date"),
                                                                     "market_cap_usd")
    out = _asof(issuer_dates.with_row_index("__r"), r, "issuer_id", "date", "mc_date")
    out = out.with_columns(pl.when((pl.col("date") - pl.col("mc_date")).dt.total_days() <= 10)
                           .then(pl.col("market_cap_usd")).otherwise(None).alias("market_cap_usd"))
    return out.sort("__r").drop("__r", "mc_date")


# ------------------------------------------------------------------------------------------------ valuation


def _pos(e: pl.Expr) -> pl.Expr:
    return pl.when(e > 0).then(e).otherwise(None)


def compute_valuation(securities: pl.DataFrame, start: date, end: date | None = None,
                      settings: Settings | None = None) -> pl.DataFrame:
    s = settings or get_settings()
    mc = daily_market_cap(securities, start, end, s)
    if mc.is_empty():
        return mc
    snaps = fundamental_snapshots(mc["issuer_id"].unique().to_list(), "USD", s)
    d = mc
    for k, suf in (("TTM", "_t"), ("FY", "_f")):
        sn = snaps[k].rename({"period_end": "pe", "knowledge_date": "kd"})
        d = _asof(d, sn, "issuer_id", "date", "kd", suffix=suf)
    use_t = (pl.col("date") - pl.col("pe_t")).dt.total_days() <= STALE_DAYS
    use_f = (pl.col("date") - pl.col("pe_f")).dt.total_days() <= STALE_DAYS
    pick = []
    for c in FUND_COLS + ["pe", "kd", "dq_score"]:
        pick.append(pl.when(use_t.fill_null(False)).then(pl.col(c + "_t"))
                    .when(use_f.fill_null(False)).then(pl.col(c + "_f")).otherwise(None).alias(c))
    d = d.with_columns(pick).drop([c for c in d.columns if c.endswith("_t") or c.endswith("_f")])
    mcap = pl.col("market_cap_usd")
    ni = pl.coalesce(pl.col("net_income_common"), pl.col("net_income_parent"), pl.col("net_income"))
    ev = (mcap + pl.col("total_debt").fill_null(0) + pl.col("preferred_equity").fill_null(0)
          + pl.col("minority_interest").fill_null(0) - pl.col("cash_and_st_investments").fill_null(0))
    tbv = pl.coalesce(pl.col("tangible_book"), pl.col("total_equity_parent") - pl.col("goodwill").fill_null(0)
                      - pl.col("intangible_assets").fill_null(0))
    ffo = pl.coalesce(pl.col("ffo"), ni + pl.coalesce(pl.col("real_estate_depreciation"), pl.col("depreciation_amortization"))
                      - pl.col("gain_on_property_sales").fill_null(0) + pl.col("impairment").fill_null(0))
    divs = (pl.col("dividends_paid") - pl.col("preferred_dividends").fill_null(0)).clip(lower_bound=0)
    has_f = pl.col("kd").is_not_null()
    d = d.with_columns(ev.alias("ev"), ni.alias("ni"), tbv.alias("tbv"), ffo.alias("ffo_x"), divs.alias("divs"))
    d = d.with_columns(
        pl.when(has_f).then(pl.col("ev")).otherwise(None).alias("enterprise_value_usd"),
        pl.when(mcap >= SIZE_LARGE).then(pl.lit("large")).when(mcap >= SIZE_MID).then(pl.lit("mid"))
        .when(mcap > 0).then(pl.lit("small")).otherwise(None).alias("size_bucket"),
        (mcap / _pos(pl.col("ni"))).alias("pe_ttm"),
        (pl.col("ni") / mcap).alias("earnings_yield"),
        (mcap / _pos(pl.col("total_equity_parent"))).alias("pb"),
        (mcap / _pos(pl.col("revenue"))).alias("ps_ttm"),
        (pl.col("ev") / _pos(pl.col("revenue"))).alias("ev_sales"),
        (pl.col("ev") / _pos(pl.col("ebitda"))).alias("ev_ebitda"),
        (pl.col("ev") / _pos(pl.col("ebit"))).alias("ev_ebit"),
        (pl.col("fcf") / mcap).alias("fcf_yield"),
        (pl.col("divs") / mcap).alias("dividend_yield"),
        ((pl.col("divs").fill_null(0) + pl.col("buybacks").fill_null(0) - pl.col("stock_issuance").fill_null(0))
         / mcap).alias("shareholder_yield"),
        (mcap / _pos(pl.col("ffo_x"))).alias("p_ffo"),
        (mcap / _pos(pl.col("tbv"))).alias("p_tbv"),
        (pl.col("gross_profit") / _pos(pl.col("ev"))).alias("gp_to_ev"),
        pl.col("kd").alias("fundamentals_knowledge_date"),
        pl.col("date").dt.year().cast(pl.Int16).alias("year"),
    )
    num = ["pe_ttm", "earnings_yield", "pb", "ps_ttm", "ev_sales", "ev_ebitda", "ev_ebit", "fcf_yield", "dividend_yield",
           "shareholder_yield", "p_ffo", "p_tbv", "gp_to_ev", "enterprise_value_usd"]
    d = d.with_columns([pl.when(has_f & pl.col(c).is_finite()).then(pl.col(c)).otherwise(None).alias(c) for c in num])
    # dividend yield without dividends reported -> 0 when fundamentals present
    d = d.with_columns(pl.when(has_f & mcap.is_not_null() & pl.col("dividend_yield").is_null()
                               & pl.col("dividends_paid").is_null()).then(0.0)
                       .otherwise(pl.col("dividend_yield")).alias("dividend_yield"))
    return d.filter(pl.col("issuer_id").is_not_null()).select(
        "security_id", "issuer_id", "date", "year", "shares_out", "market_cap_usd", "enterprise_value_usd",
        "size_bucket", "pe_ttm", "earnings_yield", "pb", "ps_ttm", "ev_sales", "ev_ebitda", "ev_ebit", "fcf_yield",
        "dividend_yield", "shareholder_yield", "p_ffo", "p_tbv", "gp_to_ev", "close_usd", "fundamentals_knowledge_date")


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult

    p = ctx.params or {}
    s = ctx.settings
    uni = universe_frame(p.get("universe", "mvp"), None, s, p.get("tickers"))
    mode = p.get("mode", "latest")
    if p.get("start") or p.get("since"):
        start = date.fromisoformat(str(p.get("start") or p.get("since")))
    elif mode == "history":
        start = date(2010, 1, 1)
    else:
        start = ctx.as_of - timedelta(days=15)
    end = date.fromisoformat(str(p["end"])) if p.get("end") else ctx.as_of
    total = 0
    now = datetime.now(timezone.utc)
    ids = uni["security_id"].to_list()
    chunk = int(p.get("chunk", 250))
    for i in range(0, len(ids), chunk):
        ctx.check_cancelled()
        v = compute_valuation(uni.filter(pl.col("security_id").is_in(ids[i:i + chunk])), start, end, s)
        if v.is_empty():
            continue
        total += write_table(v.with_columns(pl.lit(now).alias("computed_at")), "valuation_daily")
    latest = 0
    if total:
        latest = (scan_table("valuation_daily", s).filter(pl.col("date") == pl.col("date").max())
                  .select(pl.len()).collect().item())
    return JobResult(status="success", rows_written={"valuation_daily": total}, entities_ok=len(ids),
                     message=f"{len(ids)} securities, {start}..{end}, latest-date rows {latest}")
