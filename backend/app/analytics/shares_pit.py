"""Point-in-time share counts for market caps (coverage v3 A6). Owner: analytics-scoring.

`share_events(issuer_ids)` -> one row per (issuer, period_end, knowledge_date) the valuation as-of join uses:
[issuer_id, period_end, knowledge_date, shares, shares_w, basis_date, src, unit_fix].

Built from fundamentals_canonical (it carries the winning source per item; fundamentals_wide does not) and fixes the
three causes of the implausible valuation_latest market caps (docs/audits/STATEMENTS_GAP_AUDIT_2026-11.md section 6.2):

1. Source precedence (8766.T Tokio Marine, USD 1.6 tn): Yahoo statement share counts are a fallback. A Yahoo row is
   dropped when, at its own knowledge date, a regulator share count (EDINET, SEC, ESEF, DART, ...) was already known
   for a period ending at most REGULATOR_FRESH_DAYS before it. Point in time: only rows known by then decide.
2. Split basis: Yahoo restates every share count to the split basis of the day it was fetched (Tokio Marine 15:1 on
   2026-09-29: the 2026-06 count is post-split), so a Yahoo count is split-adjusted from its FETCH date
   (fundamentals_raw yfinance_fund ingested_at; else the canonical write time) and a regulator count from its period
   end. `basis_date` is that date: valuation multiplies by split_factor(D) / split_factor(basis_date). The old code used
   the period end for both, so the Yahoo post-split count was multiplied by 15 again.
3. XBRL share-scale errors (RPAY 8.3e10, TBLA 2.9e11, ASM 1.8e11: filer-tagged x1000). A rescale is PROPOSED when
   the filer's own net income / EPS of the same period (derivation reported / fallback_tag only: a formula EPS
   `net_income / shares_weighted` is circular; |EPS| >= MIN_ABS_EPS, known by the same date) implies a count 700-1400x
   smaller (or larger), or (filers that scaled EPS too: TBLA) when an outstanding (weighted) count is 700-1400x the
   previous outstanding (weighted) count of the same issuer that latest_known keeps, that the guard did not rescale
   and whose period ended at most HISTORY_MAX_DAYS before. It is APPLIED only when the traded volume confirms it: the
   implied turnover (median daily volume of the issuer's primary line / shares) must move closer to TYPICAL_TURNOVER
   (-3 needs turnover < ~6e-5 before, +3 > ~0.06). Without market data, or when the volume contradicts it, the count
   is kept (`unit_suspect` records the vetoed proposal; mcap_plausibility flags implausible turnover). A6 review
   2026-11-07: the unvalidated guard cut BRK-B (2.14e9 -> 2.14e6, chained from a 2015 Class-A-equivalent row), GERN
   (chained from a restated weighted row tagged in thousands), GBR (derived EPS) and inflated KFFB (net income x1000).
   Skipped for issuers with an ADR / GDR line (EPS may be per ADS) or several common lines (BRK-A / BRK-B: EPS per
   Class A share), see `unit_guard_skip`. `unit_fix` records the applied -3 / +3.

4. Issued counts that include treasury stock (the EDINET filing-date cover fallback; Honda 4.53e9 issued vs 3.89e9
   outstanding) are used only when no outstanding count of a recent period is known (`demote_issued_counts`).

5. Counts restated for a split between period end and filing (`split_restated_basis`, used by valuation per security):
   US GAAP / IFRS restate share counts retroactively for a split effective before the statements are issued (MLI 2:1
   on 2026-07-01, Q2 10-Q count 2.21e8 already post-split), so the period end is the wrong split basis. A regulator
   count with a split in (period_end, knowledge_date] is compared with the previous event's count on the latest split
   basis: if the knowledge-date basis is the closer reading, basis_date = knowledge_date (`basis_rule` restated),
   else it stays the period end (EDINET period-end actual counts; `basis_rule` period).

6. Only genuine share splits move a count (A6 review, FDX): prices_daily split_factor also carries Yahoo 'split'
   entries that are spin-off price adjustments (FDX 1.241 on 2026-06-01, FedEx Freight; MMM 1.196 Solventum), which
   leave the share count alone. Valuation multiplies counts by the `share_split_factor` built from the steps of the
   price split factor that `classify_split_steps` accepts: a Polygon split record on the ex-date, else a clean ratio
   (r or 1/r = p/q, q <= 20: 2, 3:2, 1:20), else a non-clean step >= 15 % whose raw volume confirms it (median volume
   after / before closer to r than to 1, within [r/2, 2r]). Non-clean steps below 15 % are never applied (stock
   dividends of a few % err by at most that). An ADR / GDR line takes a step only when its underlying line has the
   same split (ADS ratio changes are recorded as splits of the ADS series, the underlying count does not move).
   APPROXIMATION: the volume test reads up to 60 sessions after the step, i.e. it classifies a public corporate
   action with data from after its ex-date; raw volume is a weak signal (JP / CN 2:1 splits often show no volume
   change), which is why clean ratios need no volume.

Yahoo `info:sharesOutstanding` rows are listing-level counts stored on the issuer (RO.SW and ROG.SW share one issuer):
they never set the count, they only date the latest fetch.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

import polars as pl

from app.core.config import Settings
from app.core.storage import scan_table

YAHOO_SOURCES = frozenset({"yfinance", "yfinance_fund", "yahoo"})
SHARE_ITEMS = ("shares_outstanding", "shares_diluted_weighted", "shares_basic_weighted")
EPS_ITEMS = ("eps_basic", "eps_diluted")
NI_ITEMS = ("net_income_common", "net_income_parent", "net_income")
REGULATOR_FRESH_DAYS = 400
UNIT_BAND = (700.0, 1400.0)
MIN_ABS_EPS = 0.03
#: canonical derivations that are the filer's own number (module doc 3): never derived_formula (net income / our
#: weighted share count: circular), derived_period (Q4 = FY - 9M) or ttm_sum
REPORTED_DERIVATIONS = ("reported", "fallback_tag")
#: history check: the previous count must be of a period ending at most this many days earlier
HISTORY_MAX_DAYS = 400
#: market check (module doc 3): typical median daily turnover (volume / shares) of a listed share; a x1000 rescale is
#: applied only when it moves the implied turnover closer to this (log distance), i.e. -3 needs < 6.3e-5 before
#: (RPAY 1.2e-5, TBLA 5e-6) and +3 > 6.3e-2 (KFFB 3.7e-4: vetoed). APPROXIMATION: raw volume vs a count on its own
#: split basis; a split between them shifts turnover by its ratio, well inside the 31.6x margin either side.
TYPICAL_TURNOVER = 2e-3
VOLUME_SESSIONS = 60
VOLUME_MIN_SESSIONS = 20
VOLUME_MAX_GAP_DAYS = 370
_KIND_RANK = {"Q": 0, "H": 1, "FY": 2, "TTM": 3, "INSTANT": 0}

_OUT = {"issuer_id": pl.Utf8, "period_end": pl.Date, "knowledge_date": pl.Date, "shares": pl.Float64,
        "shares_w": pl.Float64, "basis_date": pl.Date, "src": pl.Utf8, "unit_fix": pl.Int8}


def isin_ids(col: str, values: Iterable[str]) -> pl.Expr:
    """pl.col(col).is_in(values) for LAZY lake scans. polars 1.44 expands a plain Python list of fewer than ~100
    values into a predicate that reads far more than it filters: _prices for 97 ADR ids took 31 s and 8.7 GB commit
    (0.6 s / 0.8 GB with the imploded Series; 120 ids: 0.7 s either way). A6 review round 2."""
    return pl.col(col).is_in(pl.Series(values=list(values), dtype=pl.Utf8).implode())


def empty_events() -> pl.DataFrame:
    return pl.DataFrame(schema=_OUT)


def load_items(issuer_ids: Sequence[str], settings: Settings | None = None) -> pl.DataFrame:
    """Canonical rows of the share / EPS / net-income items of `issuer_ids`."""
    lf = scan_table("fundamentals_canonical", settings)
    der = (pl.col("derivation").cast(pl.Utf8) if "derivation" in lf.collect_schema().names()
           else pl.lit("reported")).alias("derivation")
    return (lf.filter(pl.col("item_id").is_in([*SHARE_ITEMS, *EPS_ITEMS, *NI_ITEMS]),
                      isin_ids("issuer_id", issuer_ids))
            .select("issuer_id", "item_id", "period_kind", "period_end", "knowledge_date", "value", "source_id",
                    "source_concept", der, pl.col("ingested_at").dt.date().alias("written"))
            .collect())


def yahoo_fetch_dates(issuer_ids: Sequence[str], settings: Settings | None = None) -> pl.DataFrame:
    """[issuer_id, period_end, source_concept, fetched]: last fetch date of each raw Yahoo share fact."""
    try:
        lf = scan_table("fundamentals_raw", settings)
        return (lf.filter(pl.col("source_id") == "yfinance_fund", isin_ids("issuer_id", issuer_ids),
                          pl.col("unit") == "shares")
                .group_by("issuer_id", "period_end", pl.col("concept").alias("source_concept"))
                .agg(pl.col("ingested_at").max().dt.date().alias("fetched")).collect())
    except Exception:  # noqa: BLE001 - empty / missing raw table: canonical write dates are used
        return pl.DataFrame(schema={"issuer_id": pl.Utf8, "period_end": pl.Date, "source_concept": pl.Utf8,
                                    "fetched": pl.Date})


def _src() -> pl.Expr:
    return pl.when(pl.col("source_id").is_in(list(YAHOO_SOURCES))).then(pl.lit("yahoo")).otherwise(pl.lit("regulator"))


def _kind_expr(weighted: pl.Expr | None = None) -> pl.Expr:
    """outstanding (a reported period-end / cover count) | weighted (a weighted-average count, or the canonical
    'fallback:' to one) | issued (includes treasury stock, ISSUED_CONCEPTS)."""
    sc = pl.col("source_concept").fill_null("")
    w = sc.str.starts_with("fallback:")
    if weighted is not None:
        w = w | weighted
    return (pl.when(sc.str.contains("|".join(ISSUED_CONCEPTS))).then(pl.lit("issued"))
            .when(w).then(pl.lit("weighted")).otherwise(pl.lit("outstanding")))


def _kind(df: pl.DataFrame) -> pl.Expr:
    if "kind" in df.columns:
        return pl.col("kind")
    return _kind_expr() if "source_concept" in df.columns else pl.lit("outstanding")


def build_candidates(items: pl.DataFrame) -> pl.DataFrame:
    """Pure. One share-count candidate per (issuer, period_end, knowledge_date): a reported shares_outstanding (any
    period kind), else the weighted diluted / basic count of the shortest period ending then. Regulator rows win over
    Yahoo rows, reported counts over weighted ones. Columns: issuer_id, period_end, knowledge_date, shares, shares_w,
    src, source_concept, written."""
    it = items.filter(pl.col("value") > 0).with_columns(_src().alias("src"))
    so = (it.filter((pl.col("item_id") == "shares_outstanding")
                    & ~pl.col("source_concept").fill_null("").str.starts_with("info:"))
          .select("issuer_id", "period_end", "knowledge_date", "src", pl.col("value").alias("so"), "source_concept",
                  "written"))
    w = (it.filter(pl.col("item_id").is_in(["shares_diluted_weighted", "shares_basic_weighted"])
                   & pl.col("period_kind").is_in(["Q", "H", "FY"]))
         .with_columns(pl.col("period_kind").replace_strict(_KIND_RANK, default=9).alias("__r"),
                       (pl.col("item_id") == "shares_basic_weighted").alias("__b"))
         .sort("issuer_id", "period_end", "knowledge_date", "src", "__r", "__b")
         .group_by("issuer_id", "period_end", "knowledge_date", "src", maintain_order=True)
         .agg(pl.col("value").first().alias("w"), pl.col("source_concept").first().alias("__wc"),
              pl.col("written").first().alias("__ww")))
    a = so.join(w.select("issuer_id", "period_end", "knowledge_date", "src", "w"),
                on=["issuer_id", "period_end", "knowledge_date", "src"], how="left").select(
        "issuer_id", "period_end", "knowledge_date", "src", pl.col("so").alias("shares"), pl.col("w").alias("shares_w"),
        "source_concept", "written", pl.lit(0).alias("__weighted"))
    b = (w.join(so.select("issuer_id", "period_end", "knowledge_date", "src"),
                on=["issuer_id", "period_end", "knowledge_date", "src"], how="anti")
         .select("issuer_id", "period_end", "knowledge_date", "src", pl.col("w").alias("shares"),
                 pl.col("w").alias("shares_w"), pl.col("__wc").alias("source_concept"), pl.col("__ww").alias("written"),
                 pl.lit(1).alias("__weighted")))
    c = pl.concat([a, b], how="vertical_relaxed")
    c = c.with_columns(_kind_expr(pl.col("__weighted") == 1).alias("kind"))
    return (c.sort("issuer_id", "period_end", "knowledge_date", pl.col("src") == "yahoo", "__weighted")
            .unique(subset=["issuer_id", "period_end", "knowledge_date"], keep="first", maintain_order=True)
            .drop("__weighted"))


def eps_implied_shares(items: pl.DataFrame) -> pl.DataFrame:
    """Pure. [issuer_id, period_end, knowledge_date, implied] = |net income| / |EPS| of the same period and vintage.
    Only the filer's own numbers (REPORTED_DERIVATIONS; a frame without `derivation` counts as reported): a formula
    EPS (net income / our weighted count, GBR / LFST) would imply the very count it is meant to check."""
    k = ["issuer_id", "period_end", "period_kind", "knowledge_date"]
    base = items.filter(pl.col("period_kind").is_in(["Q", "H", "FY"]))
    if "derivation" in base.columns:
        base = base.filter(pl.col("derivation").fill_null("reported").is_in(list(REPORTED_DERIVATIONS)))
    eps = (base.filter(pl.col("item_id").is_in(list(EPS_ITEMS)) & (pl.col("value").abs() >= MIN_ABS_EPS))
           .sort(*k, pl.col("item_id") != "eps_basic").unique(subset=k, keep="first", maintain_order=True)
           .select(*k, pl.col("value").abs().alias("__eps")))
    rank = {n: i for i, n in enumerate(NI_ITEMS)}
    ni = (base.filter(pl.col("item_id").is_in(list(NI_ITEMS)) & (pl.col("value") != 0))
          .with_columns(pl.col("item_id").replace_strict(rank, default=9).alias("__r"))
          .sort(*k, "__r").unique(subset=k, keep="first", maintain_order=True)
          .select(*k, pl.col("value").abs().alias("__ni")))
    j = eps.join(ni, on=k, how="inner").with_columns((pl.col("__ni") / pl.col("__eps")).alias("implied"),
                                                     pl.col("period_kind").replace_strict(_KIND_RANK, default=9)
                                                     .alias("__r"))
    return (j.sort("issuer_id", "period_end", "knowledge_date", "__r")
            .unique(subset=["issuer_id", "period_end", "knowledge_date"], keep="first", maintain_order=True)
            .select("issuer_id", "period_end", "knowledge_date", "implied"))


def _asof_value(left: pl.DataFrame, on: str, right: pl.DataFrame, value: str, key: str = "issuer_id",
                max_gap_days: int | None = None, out: str = "__v") -> pl.DataFrame:
    """left + column `out` = right[value] on the last right date <= left[on] (the first date after it when left[on]
    precedes the series; within max_gap_days when given). right [key, date, value]. Row order of `left` is kept."""
    lf = left.with_row_index("__o")
    if right.is_empty() or lf.is_empty():
        return left.with_columns(pl.lit(None, pl.Float64).alias(out))
    lk = lf.select("__o", key, pl.col(on).alias("__d")).filter(pl.col("__d").is_not_null()).sort(key, "__d")
    rk = (right.select(key, pl.col("date").alias("__rd"), pl.col(value).cast(pl.Float64).alias("__rv"))
          .drop_nulls().sort(key, "__rd"))
    parts = []
    for strategy in ("backward", "forward"):
        j = lk.join_asof(rk, left_on="__d", right_on="__rd", by=key, strategy=strategy, check_sortedness=False)
        if max_gap_days is not None:
            j = j.with_columns(pl.when((pl.col("__d") - pl.col("__rd")).dt.total_days().abs() <= max_gap_days)
                               .then(pl.col("__rv")).otherwise(None).alias("__rv"))
        parts.append(j.select("__o", pl.col("__rv").alias(f"__{strategy}")))
    v = parts[0].join(parts[1], on="__o", how="left").select(
        "__o", pl.coalesce(pl.col("__backward"), pl.col("__forward")).alias(out))
    return lf.join(v, on="__o", how="left").sort("__o").drop("__o")


def _market_confirms(turnover: float | None, fix: int, typical: float = TYPICAL_TURNOVER) -> bool:
    """A x10^fix rescale of the count divides the implied turnover by 10^fix: confirmed when that moves it closer to
    the typical turnover (log distance). No turnover: not confirmed."""
    if turnover is None or not turnover > 0 or not math.isfinite(turnover):
        return False
    before = abs(math.log10(turnover / typical))
    after = abs(math.log10(turnover / 10.0 ** fix / typical))
    return after < before


def _kind_list(c: pl.DataFrame) -> list[str]:
    if "source_concept" in c.columns:
        return c.select(_kind_expr().alias("k")).get_column("k").to_list()
    return ["outstanding"] * c.height


def apply_unit_guard(cands: pl.DataFrame, implied: pl.DataFrame, skip: Iterable[str] = (),
                     volume: pl.DataFrame | None = None) -> pl.DataFrame:
    """Pure. Propose and (when the traded volume confirms it) apply x1000 share-scale repairs (module doc 3).

    cands: build_candidates rows (+ kind / basis_date when present); implied: eps_implied_shares; skip: issuers left
    untouched; volume [issuer_id, date, med_volume]: rolling median daily volume of the issuer's primary line
    (`primary_volume`), read at the count's split basis (basis_date, else period_end). None, or no row for an issuer,
    means no market data: every proposal is vetoed. Adds unit_fix (applied: 0 / -3 / +3) and unit_suspect (the
    vetoed proposal).

    a) EPS check: count / (|net income| / |EPS|) of its own period within UNIT_BAND (or its inverse) -> -3 (+3).
    b) History check, for filers that scaled EPS too (TBLA 2026 10-Qs: shares AND EPS x1000): a count UNIT_BAND times
       the previous count of the same kind (outstanding / weighted / issued) that latest_known keeps, that was not
       rescaled and whose period ended at most HISTORY_MAX_DAYS before -> -3, unless its own NI / EPS confirms the
       new level. Only upward jumps: a 1-for-1000 reverse split is real, a 1000-for-1 split is not."""
    c = cands.with_row_index("__i")
    skip = set(skip)
    lo, hi = UNIT_BAND
    if implied.is_empty():
        c = c.with_columns(pl.lit(0, pl.Int8).alias("__pe"), pl.lit(None, pl.Float64).alias("__imp"))
    else:
        m = (c.select("__i", "issuer_id", "period_end", "knowledge_date")
             .join(implied.rename({"knowledge_date": "__ikd"}), on=["issuer_id", "period_end"], how="inner")
             .filter(pl.col("__ikd") <= pl.col("knowledge_date"))
             .sort("__i", "__ikd").group_by("__i").agg(pl.col("implied").last().alias("__imp")))
        c = c.join(m, on="__i", how="left")
        r = pl.col("shares") / pl.col("__imp")
        c = c.with_columns(pl.when((r >= lo) & (r <= hi)).then(pl.lit(-3, pl.Int8))
                           .when((r >= 1.0 / hi) & (r <= 1.0 / lo)).then(pl.lit(3, pl.Int8))
                           .otherwise(pl.lit(0, pl.Int8)).fill_null(0).alias("__pe"))
    bcol = "basis_date" if "basis_date" in c.columns else "period_end"
    if volume is not None and volume.height:
        c = _asof_value(c, bcol, volume, "med_volume", max_gap_days=VOLUME_MAX_GAP_DAYS, out="__mv")
    else:
        c = c.with_columns(pl.lit(None, pl.Float64).alias("__mv"))
    c = c.with_columns((pl.col("__mv") / pl.col("shares")).alias("__t"))
    # sequential, issuer by issuer in knowledge-date order (small frames: one issuer chunk of valuation)
    c = c.sort("issuer_id", "knowledge_date", "period_end", "__i")
    c = c.with_columns((pl.col("period_end") >= pl.col("period_end").cum_max().over("issuer_id")).alias("__kept"))
    kinds = c.get_column("kind").fill_null("outstanding").to_list() if "kind" in c.columns else _kind_list(c)
    iss = c.get_column("issuer_id").to_list()
    pe = c.get_column("period_end").to_list()
    sh = c.get_column("shares").to_list()
    imp = c.get_column("__imp").to_list()
    prop = c.get_column("__pe").to_list()
    tv = c.get_column("__t").to_list()
    kept = c.get_column("__kept").to_list()
    fix = [0] * len(sh)
    sus = [0] * len(sh)
    prev: dict[str, tuple[float, object]] = {}
    for i in range(len(sh)):
        if i == 0 or iss[i] != iss[i - 1]:
            prev = {}
        s = sh[i]
        if iss[i] in skip or not s:
            continue
        p = int(prop[i] or 0)
        q = prev.get(kinds[i])
        if (not p and q is not None and pe[i] is not None and q[1] is not None
                and (pe[i] - q[1]).days <= HISTORY_MAX_DAYS and lo <= s / q[0] <= hi
                and not (imp[i] and 0.7 <= s / imp[i] <= 1.3)):
            p = -3
        if p:
            if _market_confirms(tv[i], p):
                fix[i] = p
            else:
                sus[i] = p
        if kept[i] and not fix[i]:
            prev[kinds[i]] = (s, pe[i])   # never chain from a rescaled row
    c = c.with_columns(pl.Series("unit_fix", fix, pl.Int8), pl.Series("unit_suspect", sus, pl.Int8))
    f = pl.lit(10.0).pow(pl.col("unit_fix").cast(pl.Float64))
    c = c.with_columns((pl.col("shares") * f).alias("shares"), (pl.col("shares_w") * f).alias("shares_w"))
    return c.sort("__i").drop("__i", "__imp", "__pe", "__mv", "__t", "__kept")


def unit_guard_skip(issuer_ids: Sequence[str], settings: Settings | None = None) -> set[str]:
    """Issuers the unit guard leaves untouched: any ADR / GDR line (EPS may be per ADS: TLK, ENELCHILE.SN / ENIC) or
    several active common lines (BRK-A / BRK-B: EPS per Class A share, counts in Class-B equivalents)."""
    ids = list(issuer_ids)
    if not ids:
        return set()
    sm = (scan_table("securities_master", settings).filter(pl.col("issuer_id").is_in(ids))
          .select("issuer_id", "security_type", "is_active").collect())
    g = sm.group_by("issuer_id").agg(
        pl.col("security_type").is_in(["adr", "gdr"]).any().alias("dr"),
        ((pl.col("security_type") == "common") & pl.col("is_active").fill_null(True)).sum().alias("n_common"))
    return set(g.filter(pl.col("dr") | (pl.col("n_common") >= 2)).get_column("issuer_id").to_list())


def primary_volume(issuer_ids: Sequence[str], settings: Settings | None = None) -> pl.DataFrame:
    """[issuer_id, date, med_volume]: rolling median (VOLUME_SESSIONS sessions, >= VOLUME_MIN_SESSIONS traded) of the
    raw daily volume of each issuer's primary line (prices_daily). Only read for issuers with a unit proposal."""
    empty = pl.DataFrame(schema={"issuer_id": pl.Utf8, "date": pl.Date, "med_volume": pl.Float64})
    ids = list(issuer_ids)
    if not ids:
        return empty
    sm = (scan_table("securities_master", settings).filter(pl.col("issuer_id").is_in(ids))
          .select("security_id", "issuer_id", "is_primary_security", "security_type").collect())
    prim = (sm.with_columns(pl.col("is_primary_security").fill_null(False).alias("__p"),
                            (pl.col("security_type") == "common").fill_null(False).alias("__c"))
            .sort(["issuer_id", "__p", "__c", "security_id"], descending=[False, True, True, False])
            .unique(subset=["issuer_id"], keep="first", maintain_order=True).select("security_id", "issuer_id"))
    if prim.is_empty():
        return empty
    px = (scan_table("prices_daily", settings)
          .filter(isin_ids("security_id", prim.get_column("security_id").to_list()), pl.col("volume") > 0)
          .select("security_id", "date", pl.col("volume").cast(pl.Float64)).collect()
          .unique(subset=["security_id", "date"], keep="last").sort("security_id", "date"))
    if px.is_empty():
        return empty
    px = px.with_columns(pl.col("volume").rolling_median(VOLUME_SESSIONS, min_samples=VOLUME_MIN_SESSIONS)
                         .over("security_id").alias("med_volume")).drop_nulls("med_volume")
    return px.join(prim, on="security_id", how="inner").select("issuer_id", "date", "med_volume")


#: share counts that INCLUDE treasury stock: the EDINET cover fallback (issued shares as of the filing date; the
#: canonical JP mapping prefers issued - treasury at period end). Honda holds ~14 % in treasury.
ISSUED_CONCEPTS = ("NumberOfIssuedSharesAsOfFilingDate",)


def _known_pe(base: pl.DataFrame) -> pl.DataFrame:
    """[issuer_id, __rkd, __rpe]: greatest period end among `base` rows known by each knowledge date."""
    return (base.sort("issuer_id", "knowledge_date")
            .with_columns(pl.col("period_end").cum_max().over("issuer_id").alias("__rpe"))
            .group_by("issuer_id", "knowledge_date").agg(pl.col("__rpe").max())
            .sort("issuer_id", "knowledge_date").rename({"knowledge_date": "__rkd"}))


def _superseded(rows: pl.DataFrame, by: pl.DataFrame, fresh_days: int) -> pl.DataFrame:
    """Rows of `rows` NOT superseded by a `by` count known at their knowledge date for a period ending at most
    fresh_days before theirs."""
    if rows.is_empty() or by.is_empty():
        return rows
    j = rows.sort("issuer_id", "knowledge_date").join_asof(
        _known_pe(by), left_on="knowledge_date", right_on="__rkd", by="issuer_id", strategy="backward",
        check_sortedness=False)
    return j.filter(pl.col("__rpe").is_null()
                    | (pl.col("__rpe") < pl.col("period_end") - pl.duration(days=fresh_days))).drop("__rkd", "__rpe")


def demote_issued_counts(cands: pl.DataFrame, fresh_days: int = REGULATOR_FRESH_DAYS) -> pl.DataFrame:
    """Pure. Issued-incl.-treasury counts (ISSUED_CONCEPTS) are a fallback: dropped when an outstanding count of any
    source was known by then for a period ending at most fresh_days before (Honda 4.53e9 issued vs 3.89e9
    outstanding)."""
    is_iss = _kind(cands) == "issued"
    iss, other = cands.filter(is_iss), cands.filter(~is_iss)
    if iss.is_empty() or other.is_empty():
        return cands
    return pl.concat([other, _superseded(iss, other, fresh_days)], how="vertical_relaxed").sort(
        "issuer_id", "period_end", "knowledge_date")


def regulator_precedence(cands: pl.DataFrame, fresh_days: int = REGULATOR_FRESH_DAYS) -> pl.DataFrame:
    """Pure. Drop Yahoo rows superseded by a regulator count known at the Yahoo row's knowledge date (module doc 1)."""
    reg = cands.filter(pl.col("src") == "regulator")
    yh = cands.filter(pl.col("src") == "yahoo")
    if reg.is_empty() or yh.is_empty():
        return cands
    # only a reported outstanding count supersedes: an issued-incl.-treasury fallback (8766.T: the 2025-06-19 EDINET
    # cover count would hide every later Yahoo count) or a weighted average (Honda FY 4.67e9 vs 3.89e9 at the end of a
    # buyback year) is a poorer period-end count than Yahoo's
    strong = reg.filter(_kind(reg) == "outstanding")
    return pl.concat([reg, _superseded(yh, strong, fresh_days)], how="vertical_relaxed").sort(
        "issuer_id", "period_end", "knowledge_date")


def attach_basis(cands: pl.DataFrame, fetched: pl.DataFrame | None = None) -> pl.DataFrame:
    """Pure. basis_date = period_end (regulator) or max(period_end, Yahoo fetch date) (module doc 2)."""
    c = cands
    if fetched is not None and fetched.height:
        c = c.join(fetched, on=["issuer_id", "period_end", "source_concept"], how="left")
    else:
        c = c.with_columns(pl.lit(None, pl.Date).alias("fetched"))
    yb = pl.max_horizontal(pl.col("period_end"), pl.coalesce(pl.col("fetched"), pl.col("written"), pl.col("period_end")))
    return c.with_columns(pl.when(pl.col("src") == "yahoo").then(yb).otherwise(pl.col("period_end"))
                          .alias("basis_date")).drop("fetched")


def split_restated_basis(events: pl.DataFrame, sf: pl.DataFrame, key: str = "security_id",
                         max_prev_days: int = HISTORY_MAX_DAYS) -> pl.DataFrame:
    """Pure (module doc 5). events [key, period_end, knowledge_date, shares, basis_date, src, ...]: one key's rows are
    its latest_known event list; sf [key, date, split_factor] (prices_daily cumulative split factors, latest = 1).
    For a non-Yahoo count with a split between its basis (period end) and its knowledge date, the two readings of the
    count on the latest split basis (shares / sf(period end) vs shares / sf(knowledge date)) are compared with the
    previous event's (shares / sf(its basis), period ending at most max_prev_days before): the closer one wins.
    Returns events + basis_date (moved to the knowledge date when restated), sf_basis (split factor at the final
    basis; null without price bars) and basis_rule (restated | period | null = no split in between or nothing to
    compare with: valuation's shares / shares_w test then decides). Split factors: last bar on or before the date,
    else the first bar after it."""
    if events.is_empty():
        return events.with_columns(pl.lit(None, pl.Float64).alias("sf_basis"), pl.lit(None, pl.Utf8).alias("basis_rule"))
    ev = events.with_row_index("__j").sort(key, "knowledge_date", "period_end", "__j")
    s = sf.filter(pl.col("split_factor") > 0) if sf.height else sf
    ev = _asof_value(ev, "basis_date", s, "split_factor", key=key, out="__sfb")
    ev = _asof_value(ev, "knowledge_date", s, "split_factor", key=key, out="__sfk")
    keys = ev.get_column(key).to_list()
    pe = ev.get_column("period_end").to_list()
    kd = ev.get_column("knowledge_date").to_list()
    bd = ev.get_column("basis_date").to_list()
    src = ev.get_column("src").to_list()
    sh = ev.get_column("shares").to_list()
    sfb = ev.get_column("__sfb").to_list()
    sfk = ev.get_column("__sfk").to_list()
    out_b, out_f, out_r = [], [], []
    prev: tuple[float, object] | None = None
    for i in range(len(sh)):
        if i == 0 or keys[i] != keys[i - 1]:
            prev = None
        b, f, rule = bd[i], sfb[i], None
        if (src[i] != "yahoo" and prev is not None and sh[i] and sfb[i] and sfk[i]
                and abs(sfk[i] / sfb[i] - 1.0) > 1e-6 and pe[i] is not None and prev[1] is not None
                and (pe[i] - prev[1]).days <= max_prev_days):
            at_pe = abs(math.log(sh[i] / sfb[i] / prev[0]))
            at_kd = abs(math.log(sh[i] / sfk[i] / prev[0]))
            if at_kd < at_pe:
                b, f, rule = kd[i], sfk[i], "restated"
            else:
                rule = "period"
        out_b.append(b)
        out_f.append(f)
        out_r.append(rule)
        if sh[i] and f:
            prev = (sh[i] / f, pe[i])
    ev = ev.with_columns(pl.Series("basis_date", out_b, pl.Date), pl.Series("sf_basis", out_f, pl.Float64),
                         pl.Series("basis_rule", out_r, pl.Utf8))
    return ev.sort("__j").drop("__j", "__sfb", "__sfk")


# ------------------------------------------------------------------------------------------------ share split factor

#: a split-factor step counts as a share split (module doc 6) when its ratio (or the inverse) is p/q with q <= this
CLEAN_MAX_Q = 20
CLEAN_TOL = 5e-4
#: non-clean steps smaller than this (|ln r|) are never applied: stock dividends of a few % and spin-off price
#: adjustments (O 1.032 Orion) are indistinguishable by volume, and the error is at most this much
SMALL_STEP = math.log(1.15)
#: volume confirmation window (raw prices_daily volume): median of up to this many traded sessions each side
STEP_VOLUME_SESSIONS = 60
STEP_VOLUME_MIN = 10
STEP_VOLUME_DAYS = 120
#: Polygon split on the ex-date (+- days, ratio within 1 %) / underlying line's split for an ADR (+- days)
POLYGON_MATCH_DAYS = 3
UNDERLYING_MATCH_DAYS = 15


def clean_ratio(r: float, max_q: int = CLEAN_MAX_Q, tol: float = CLEAN_TOL) -> bool:
    """r or 1/r equals p/q (q <= max_q) within a relative `tol`: 2, 3, 1.5 (3:2), 1.25 (5:4), 0.05 (1:20), 1/30."""
    if not r or not math.isfinite(r) or r <= 0:
        return False
    for x in (r, 1.0 / r):
        for q in range(1, max_q + 1):
            p = round(x * q)
            if p >= 1 and abs(p / q - x) <= tol * x:
                return True
    return False


def split_steps(sf: pl.DataFrame, key: str = "security_id") -> pl.DataFrame:
    """Pure. [key, date, r]: every change of the cumulative split factor between consecutive bars (r = sf(date) /
    sf(previous bar); 2.0 for a 2-for-1, 0.05 for a 1-for-20 reverse split). sf [key, date, split_factor]."""
    if sf.is_empty():
        return pl.DataFrame(schema={key: pl.Utf8, "date": pl.Date, "r": pl.Float64})
    s = sf.filter(pl.col("split_factor") > 0).sort(key, "date")
    s = s.with_columns((pl.col("split_factor") / pl.col("split_factor").shift(1).over(key)).alias("r"))
    return s.filter((pl.col("r") - 1).abs() > 1e-6).select(key, "date", pl.col("r").cast(pl.Float64))


def _step_volume(steps: pl.DataFrame, bars: pl.DataFrame, key: str) -> pl.DataFrame:
    """steps + v = median raw volume after / before the step (None without STEP_VOLUME_MIN sessions each side)."""
    if "volume" not in bars.columns or steps.is_empty():
        return steps.with_columns(pl.lit(None, pl.Float64).alias("v"))
    st = steps.select(key, pl.col("date").alias("__t"))
    b = bars.filter(pl.col("volume") > 0).select(key, "date", pl.col("volume").cast(pl.Float64))
    j = st.join(b, on=key, how="inner").filter(
        (pl.col("date") >= pl.col("__t") - pl.duration(days=STEP_VOLUME_DAYS))
        & (pl.col("date") <= pl.col("__t") + pl.duration(days=STEP_VOLUME_DAYS))).sort(key, "__t", "date")
    pre = (j.filter(pl.col("date") < pl.col("__t")).group_by(key, "__t")
           .agg(pl.col("volume").tail(STEP_VOLUME_SESSIONS).median().alias("__vb"), pl.len().alias("__nb")))
    post = (j.filter(pl.col("date") >= pl.col("__t")).group_by(key, "__t")
            .agg(pl.col("volume").head(STEP_VOLUME_SESSIONS).median().alias("__va"), pl.len().alias("__na")))
    out = (steps.join(pre, left_on=[key, "date"], right_on=[key, "__t"], how="left")
           .join(post, left_on=[key, "date"], right_on=[key, "__t"], how="left"))
    ok = (pl.col("__nb") >= STEP_VOLUME_MIN) & (pl.col("__na") >= STEP_VOLUME_MIN) & (pl.col("__vb") > 0)
    return out.with_columns(pl.when(ok).then(pl.col("__va") / pl.col("__vb")).otherwise(None).alias("v")).drop(
        "__vb", "__nb", "__va", "__na")


def _near_match(steps: pl.DataFrame, ref: pl.DataFrame, key: str, days: int, out: str) -> pl.DataFrame:
    """steps + bool `out`: `ref` [key, date, r] has a step within +- days whose ratio is within 1 %."""
    if ref.is_empty() or steps.is_empty():
        return steps.with_columns(pl.lit(False).alias(out))
    j = (steps.with_row_index("__s").join(ref.select(key, pl.col("date").alias("__rd"), pl.col("r").alias("__rr")),
                                          on=key, how="inner")
         .filter(((pl.col("date") - pl.col("__rd")).dt.total_days().abs() <= days)
                 & ((pl.col("r") / pl.col("__rr")).log().abs() <= 0.01))
         .select("__s").unique().with_columns(pl.lit(True).alias(out)))
    return (steps.with_row_index("__s").join(j, on="__s", how="left")
            .with_columns(pl.col(out).fill_null(False)).drop("__s"))


def classify_split_steps(steps: pl.DataFrame, bars: pl.DataFrame | None = None,
                         polygon: pl.DataFrame | None = None, dr_underlying: pl.DataFrame | None = None,
                         key: str = "security_id") -> pl.DataFrame:
    """Pure (module doc 6). steps [key, date, r] (split_steps); bars [key, date, volume] (raw volume); polygon
    [key, date, r] (Polygon split records); dr_underlying [key, underlying] for ADR / GDR lines (their steps count only
    when the underlying line has the same step, as classified here). Adds v, clean, why, genuine."""
    if steps.is_empty():
        return steps.with_columns(pl.lit(None, pl.Float64).alias("v"), pl.lit(None, pl.Boolean).alias("clean"),
                                  pl.lit(None, pl.Utf8).alias("why"), pl.lit(None, pl.Boolean).alias("genuine"))
    st = _step_volume(steps, bars if bars is not None else pl.DataFrame(), key)
    st = _near_match(st, polygon if polygon is not None else pl.DataFrame(), key, POLYGON_MATCH_DAYS, "__poly")
    st = st.with_columns(pl.col("r").map_elements(clean_ratio, return_dtype=pl.Boolean).alias("clean"))
    lr, lv = pl.col("r").log(), pl.col("v").log()
    vol_ok = pl.col("v").is_not_null() & ((lv - lr).abs() < lv.abs()) & ((lv - lr).abs() <= math.log(2.0))
    why = (pl.when(pl.col("__poly")).then(pl.lit("polygon"))
           .when(pl.col("clean")).then(pl.lit("clean_ratio"))
           .when(lr.abs() < SMALL_STEP).then(pl.lit("small_unclean"))
           .when(vol_ok.fill_null(False)).then(pl.lit("volume"))
           .otherwise(pl.lit("unconfirmed")))
    st = st.with_columns(why.alias("why")).with_columns(
        pl.col("why").is_in(["polygon", "clean_ratio", "volume"]).alias("genuine")).drop("__poly")
    if dr_underlying is not None and dr_underlying.height:
        dr = set(dr_underlying.get_column(key).to_list())
        und = (st.filter(pl.col("genuine")).join(dr_underlying.select(pl.col("underlying").alias(key),
                                                                      pl.col(key).alias("__dr")), on=key, how="inner")
               .select(pl.col("__dr").alias(key), "date", "r"))
        sdr = _near_match(st.filter(pl.col(key).is_in(list(dr))), und, key, UNDERLYING_MATCH_DAYS, "__u")
        sdr = sdr.with_columns(pl.when(pl.col("__u")).then(pl.lit("underlying")).otherwise(pl.lit("dr_no_underlying"))
                               .alias("why"), pl.col("__u").alias("genuine")).drop("__u")
        st = pl.concat([st.filter(~pl.col(key).is_in(list(dr))), sdr], how="vertical_relaxed")
    return st.sort(key, "date")


def share_split_factor(sf: pl.DataFrame, steps: pl.DataFrame, key: str = "security_id") -> pl.DataFrame:
    """Pure. sf [key, date, ...] -> [key, date, split_factor]: the cumulative factor of the GENUINE steps only (latest
    = 1; before a genuine r-for-1 step it is 1/r of the value after). Every bar of sf is kept."""
    base = sf.select(key, "date").unique(subset=[key, "date"], keep="first")
    g = steps.filter(pl.col("genuine")) if steps.height and "genuine" in steps.columns else steps.head(0)
    if g.is_empty():
        return base.with_columns(pl.lit(1.0).alias("split_factor")).sort(key, "date")
    ev = (g.select(key, pl.col("date").alias("__t"), "r").sort([key, "__t"], descending=[False, True])
          .with_columns((1.0 / pl.col("r")).cum_prod().over(key).alias("__f")).sort(key, "__t"))
    b = base.with_columns((pl.col("date") + pl.duration(days=1)).alias("__nx")).sort(key, "__nx")
    j = b.join_asof(ev.select(key, "__t", "__f"), left_on="__nx", right_on="__t", by=key, strategy="forward",
                    check_sortedness=False)
    return j.select(key, "date", pl.col("__f").fill_null(1.0).alias("split_factor")).sort(key, "date")


def polygon_splits(security_ids: Sequence[str], settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, date, r]: Polygon split records (corporate_actions source polygon)."""
    empty = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "r": pl.Float64})
    ids = list(security_ids)
    if not ids:
        return empty
    try:
        return (scan_table("corporate_actions", settings)
                .filter(isin_ids("security_id", ids), pl.col("action_type") == "split",
                        pl.col("source_id") == "polygon", pl.col("ratio") > 0)
                .select("security_id", pl.col("ex_date").alias("date"), pl.col("ratio").cast(pl.Float64).alias("r"))
                .collect())
    except Exception:  # noqa: BLE001 - no corporate actions table yet
        return empty


def share_events(issuer_ids: Sequence[str], settings: Settings | None = None,
                 skip_unit_guard: Iterable[str] = ()) -> pl.DataFrame:
    """See module doc. Rows pass valuation.latest_known (latest period known at each knowledge date)."""
    from app.analytics.valuation import latest_known  # noqa: PLC0415 - valuation imports this module

    ids = list(issuer_ids)
    if not ids:
        return empty_events()
    items = load_items(ids, settings)
    if items.is_empty():
        return empty_events()
    cands = build_candidates(items.filter(pl.col("item_id").is_in(list(SHARE_ITEMS))))
    if cands.is_empty():
        return empty_events()
    cands = regulator_precedence(demote_issued_counts(cands))
    yids = cands.filter(pl.col("src") == "yahoo").get_column("issuer_id").unique().to_list()
    cands = attach_basis(cands, yahoo_fetch_dates(yids, settings) if yids else None)
    skip = set(skip_unit_guard) | unit_guard_skip(cands.get_column("issuer_id").unique().to_list(), settings)
    implied = eps_implied_shares(items)
    # proposals first (no market data: nothing applied); the volume is read only for issuers with a proposal (the
    # first proposal of an issuer sees the same history in both passes, so no issuer is missed)
    probe = apply_unit_guard(cands, implied, skip)
    sus = probe.filter(pl.col("unit_suspect") != 0).get_column("issuer_id").unique().to_list()
    cands = apply_unit_guard(cands, implied, skip, primary_volume(sus, settings)) if sus else probe
    out = latest_known(cands.select(*_OUT))
    return out.select(*_OUT).cast(_OUT)  # type: ignore[arg-type]


__all__ = ["ISSUED_CONCEPTS", "REGULATOR_FRESH_DAYS", "REPORTED_DERIVATIONS", "TYPICAL_TURNOVER", "YAHOO_SOURCES",
           "apply_unit_guard", "attach_basis", "build_candidates", "classify_split_steps", "clean_ratio",
           "demote_issued_counts", "empty_events", "eps_implied_shares", "polygon_splits", "primary_volume",
           "regulator_precedence", "share_events", "share_split_factor", "split_restated_basis", "split_steps",
           "unit_guard_skip"]
