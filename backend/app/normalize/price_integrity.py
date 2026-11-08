"""Price-history integrity guards (O2.2a data integrity, docs/AUDIT_2026-10-19.md F04 / F05 / F32). Owner: ingest-market.

Three independent, pure (Polars-in, Polars-out) checks used by `app.normalize.prices.build_prices` (job
`prices_finalize`) and by the Data Ops gap detector:

1. Identifier reuse (`detect_identity_breaks` / `apply_identity_breaks`, audit F04).
   Polygon grouped-daily bars are keyed by the ticker string of the day, so when an exchange re-assigns a ticker
   (SPCX: an ETF until 2026-04, SpaceX from 2026-06-12; BNY: a muni fund until 2026-02, BNY Mellon from 2026-05-21;
   FIG: an ETF until 2025-05, Figma from 2025-07-31) the old instrument's bars land on the new security. Yahoo
   symbols are re-used the same way (HK codes, B3 tickers). A *break* is a junction in ONE source's series where
       * the split-neutral price ratio is >= `min_jump` (or <= 1/min_jump), with no split / stock dividend between
         the two bars, AND
       * at least `min_gap_sessions` business days are missing between the two bars (the old instrument stopped
         trading before the new one listed), AND
       * corroboration: the ratio is >= `strong_jump`, OR the traded volume regime shifts by >= `vol_shift`x, OR
         another source's series for the security starts at the junction (Yahoo history of a new listing), OR
         another source is continuous across the junction and agrees with the post-break level only (BNY), OR
         the gap is >= `long_gap_sessions` (a year without prints).
   The latest segment is taken to be the current issuer (identity is resolved on today's listings); that source's
   bars before the latest break are removed from the security, other sources' bars are untouched.
   Manual overrides: `data/reference/seeds/price_identity_breaks.csv` (force / suppress a break).

2. Unit / scale flips (`repair_unit_scale`, audit F32). Adjusted closes that jump by ~10^k (k = 2, 3; Yahoo quoting a
   GBp line in GBP for a few days, ZAc/ILA lines, a reverse split Yahoo neither adjusted nor dated correctly, DIA.MC)
   are rescaled to the level of the latest segment when the evidence is unambiguous:
       * flip pair: an opposite jump of the same decade within `max_flip_bars` bars (temporary unit change), or
       * persistent jump whose volume moves inversely by the same decade (unrecorded reverse split: price x 10^k,
         volume / 10^k), or
       * persistent 100x jump on a minor-unit quote line (GBp/ZAc/ILA) with no volume change (unit switch).
   Anything else is only reported (`unresolved`).

3. Missing exchange sessions (`session_coverage`, `missing_price_sessions`, audit F05). Expected sessions come from
   exchange_calendars (the same calendars prices_finalize uses for its non_session flag); a session whose bar count
   is below `min_coverage` x the market's typical count is `partial`, zero bars is `missing`.
   `missing_price_sessions(...)` is the function the Data Ops gap detector (contracts/coverage.yaml / data_gaps)
   calls; `refetch_symbols_for_gaps(...)` lists the Yahoo symbols to re-download for those gaps.

Also: `drop_unfinished_bars` (Yahoo returns the in-progress session as a daily bar; the 2026-10-13 morning fetch
stored partial EU/UK bars as final closes) and `fx_scale_glitches` (Yahoo FX pairs, PHASE3_TODO).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import polars as pl

from app.core.logsetup import get_logger

log = get_logger(__name__)

KEY = ("security_id", "source_id")
#: minor-unit quote currencies (GBp/ZAc/ILA lines: Yahoo sometimes switches the unit)
MINOR_UNIT_CCY = ("GBp", "GBX", "ZAc", "ZAC", "ILA", "ILa")
_EMPTY_BREAKS = {"security_id": pl.Utf8, "source_id": pl.Utf8, "prev_date": pl.Date, "break_date": pl.Date,
                 "prev_close": pl.Float64, "close": pl.Float64, "ratio": pl.Float64, "gap_sessions": pl.Int64,
                 "vol_ratio": pl.Float64, "evidence": pl.List(pl.Utf8), "is_break": pl.Boolean}


# ================================================================================================ 1. identity breaks


def _asof(left: pl.DataFrame, right: pl.DataFrame, **kw: Any) -> pl.DataFrame:
    """join_asof without the 'sortedness cannot be checked' warning (both sides sorted on the asof key)."""
    import warnings  # noqa: PLC0415

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Sortedness of columns cannot be checked")
        return left.join_asof(right, **kw)


@dataclass(frozen=True)
class BreakParams:
    min_jump: float = 2.5          # split-neutral close ratio at the junction (x or 1/x)
    strong_jump: float = 4.0       # sufficient corroboration on its own (with the gap)
    min_gap_sessions: int = 10     # business days without a print between the two bars
    long_gap_sessions: int = 250   # ~1 year without prints: continuity is meaningless anyway
    vol_shift: float = 8.0         # median volume after / before (or before / after)
    vol_window: int = 20
    start_window_days: int = 7     # another source starting within +-N days of the junction
    agree_tol: float = 0.10        # cross-source agreement after the break


def _split_products(cands: pl.DataFrame, actions: pl.DataFrame | None) -> pl.DataFrame:
    """Product of split / stock-dividend ratios with ex_date in (prev_date, date] per candidate (1.0 if none)."""
    if actions is None or actions.is_empty() or cands.is_empty():
        return cands.with_columns(pl.lit(1.0).alias("__split"))
    sp = actions.filter(pl.col("action_type").is_in(["split", "stock_dividend"]) & (pl.col("ratio") > 0)
                        & ((pl.col("ratio") - 1.0).abs() > 1e-9)).select("security_id", "ex_date", "ratio")
    if sp.is_empty():
        return cands.with_columns(pl.lit(1.0).alias("__split"))
    j = (cands.select("security_id", "source_id", "date", "prev_date").unique()
         .join(sp, on="security_id", how="inner")
         .filter((pl.col("ex_date") > pl.col("prev_date")) & (pl.col("ex_date") <= pl.col("date")))
         .group_by("security_id", "source_id", "date").agg(pl.col("ratio").product().alias("__split")))
    return cands.join(j, on=["security_id", "source_id", "date"], how="left").with_columns(
        pl.col("__split").fill_null(1.0))


def detect_identity_breaks(bars: pl.DataFrame, actions: pl.DataFrame | None = None,
                           params: BreakParams | None = None) -> pl.DataFrame:
    """Junction candidates + decision per (security_id, source_id) series.

    bars: [security_id, source_id, date, close(, volume)] on a RAW, major-unit basis (one row per source and date).
    actions: [security_id, ex_date, action_type, ratio] (splits use ratio new/old: 2.0 = 2-for-1).
    Returns one row per candidate junction: security_id, source_id, prev_date, break_date, prev_close, close, ratio
    (split-neutral), gap_sessions, vol_ratio, evidence (list), is_break."""
    p = params or BreakParams()
    if bars.is_empty():
        return pl.DataFrame(schema=_EMPTY_BREAKS)
    vol = pl.col("volume") if "volume" in bars.columns else pl.lit(None, pl.Float64)
    b = (bars.select("security_id", "source_id", "date", pl.col("close").cast(pl.Float64),
                     vol.cast(pl.Float64).alias("volume"))
         .filter(pl.col("close") > 0).sort("security_id", "source_id", "date"))
    b = b.with_columns(pl.col("close").shift(1).over(KEY).alias("prev_close"),
                       pl.col("date").shift(1).over(KEY).alias("prev_date"))
    lo, hi = 1.0 / p.min_jump, p.min_jump
    c = b.filter(pl.col("prev_close").is_not_null()
                 & (((pl.col("close") / pl.col("prev_close")) >= hi) | ((pl.col("close") / pl.col("prev_close")) <= lo)))
    if c.is_empty():
        return pl.DataFrame(schema=_EMPTY_BREAKS)
    c = c.with_columns((pl.business_day_count(pl.col("prev_date"), pl.col("date")) - 1).cast(pl.Int64)
                       .alias("gap_sessions")).filter(pl.col("gap_sessions") >= p.min_gap_sessions)
    if c.is_empty():
        return pl.DataFrame(schema=_EMPTY_BREAKS)
    c = _split_products(c, actions).with_columns(
        (pl.col("close") / pl.col("prev_close") * pl.col("__split")).alias("ratio"))
    c = c.filter((pl.col("ratio") >= hi) | (pl.col("ratio") <= lo))
    if c.is_empty():
        return pl.DataFrame(schema=_EMPTY_BREAKS)
    # volume regime: median of `vol_window` bars before / from the junction (only series with candidates)
    keys = c.select(*KEY).unique()
    bb = b.join(keys, on=list(KEY), how="semi").with_columns(pl.int_range(pl.len()).over(KEY).alias("__i"))
    ci = c.join(bb.select(*KEY, "date", "__i"), on=[*KEY, "date"], how="left")
    w = p.vol_window
    pre = (ci.select(*KEY, "date", "__i").join(bb.select(*KEY, pl.col("__i").alias("__j"), "volume"), on=list(KEY))
           .filter((pl.col("__j") < pl.col("__i")) & (pl.col("__j") >= pl.col("__i") - w))
           .group_by(*KEY, "date").agg(pl.col("volume").filter(pl.col("volume") > 0).median().alias("__vb")))
    post = (ci.select(*KEY, "date", "__i").join(bb.select(*KEY, pl.col("__i").alias("__j"), "volume"), on=list(KEY))
            .filter((pl.col("__j") >= pl.col("__i")) & (pl.col("__j") < pl.col("__i") + w))
            .group_by(*KEY, "date").agg(pl.col("volume").filter(pl.col("volume") > 0).median().alias("__va")))
    c = (c.join(pre, on=[*KEY, "date"], how="left").join(post, on=[*KEY, "date"], how="left")
         .with_columns((pl.col("__va") / pl.col("__vb")).alias("vol_ratio")))
    # other sources of the same security: first date, level around the junction
    first = b.group_by("security_id", "source_id").agg(pl.col("date").min().alias("__first"))
    other = (c.select(*KEY, "date", "prev_date", "prev_close", "close").join(
        first.rename({"source_id": "__os"}), on="security_id", how="inner").filter(pl.col("__os") != pl.col("source_id")))
    started = other.filter((pl.col("__first") - pl.col("date")).dt.total_days().abs() <= p.start_window_days
                           ).select(*KEY, "date").unique().with_columns(pl.lit(True).alias("__ev_start"))
    # continuity: the other source has bars around both sides of the junction without a jump, agrees with the new
    # level and disagrees with the old one
    ob = b.select("security_id", pl.col("source_id").alias("__os"), pl.col("date").alias("__od"),
                  pl.col("close").alias("__oc")).sort("__od")
    o2 = other.select(*KEY, "date", "prev_date", "prev_close", "close", "__os").sort("prev_date")
    o2 = _asof(o2, ob.rename({"__od": "__od_pre", "__oc": "__oc_pre"}), left_on="prev_date", right_on="__od_pre",
               by=["security_id", "__os"], strategy="backward", tolerance=timedelta(days=10))
    o2 = _asof(o2.sort("date"), ob.rename({"__od": "__od_post", "__oc": "__oc_post"}), left_on="date",
               right_on="__od_post", by=["security_id", "__os"], strategy="forward", tolerance=timedelta(days=10))
    t = p.agree_tol
    cont = o2.filter(
        pl.col("__oc_pre").is_not_null() & pl.col("__oc_post").is_not_null()
        & ((pl.col("__oc_post") / pl.col("__oc_pre")) < 1.5) & ((pl.col("__oc_post") / pl.col("__oc_pre")) > 1 / 1.5)
        & ((pl.col("close") / pl.col("__oc_post") - 1).abs() <= t)
        & (((pl.col("prev_close") / pl.col("__oc_pre")) >= p.min_jump)
           | ((pl.col("prev_close") / pl.col("__oc_pre")) <= 1 / p.min_jump))
    ).select(*KEY, "date").unique().with_columns(pl.lit(True).alias("__ev_cont"))
    c = c.join(started, on=[*KEY, "date"], how="left").join(cont, on=[*KEY, "date"], how="left")
    jump = pl.max_horizontal(pl.col("ratio"), 1.0 / pl.col("ratio"))
    vr = pl.col("vol_ratio")
    ev = pl.concat_list([
        pl.when(jump >= p.strong_jump).then(pl.lit("strong_jump")),
        pl.when((vr >= p.vol_shift) | (vr <= 1.0 / p.vol_shift)).then(pl.lit("volume_regime")),
        pl.when(pl.col("__ev_start").fill_null(False)).then(pl.lit("other_source_starts")),
        pl.when(pl.col("__ev_cont").fill_null(False)).then(pl.lit("other_source_continuous")),
        pl.when(pl.col("gap_sessions") >= p.long_gap_sessions).then(pl.lit("long_gap")),
    ]).list.drop_nulls()
    out = c.with_columns(ev.alias("evidence")).with_columns((pl.col("evidence").list.len() > 0).alias("is_break"))
    return out.select("security_id", "source_id", "prev_date", pl.col("date").alias("break_date"), "prev_close", "close",
                      "ratio", "gap_sessions", "vol_ratio", "evidence", "is_break").sort("security_id", "break_date")


def load_break_overrides(settings: Any = None) -> pl.DataFrame:
    """Manual overrides (data/reference/seeds/price_identity_breaks.csv):
    key (security_id or source symbol), source_id ('*' = any), break_date, action (force|suppress), note."""
    schema = {"key": pl.Utf8, "source_id": pl.Utf8, "break_date": pl.Date, "action": pl.Utf8}
    try:
        from app.core.config import get_settings  # noqa: PLC0415

        s = settings or get_settings()
        f = s.data_dir / "reference" / "seeds" / "price_identity_breaks.csv"
        if not f.exists():
            return pl.DataFrame(schema=schema)
        df = pl.read_csv(f, comment_prefix="#", infer_schema_length=0)
        return df.select(pl.col("key").str.strip_chars(), pl.col("source_id").fill_null("*").str.strip_chars(),
                         pl.col("break_date").str.to_date(strict=False), pl.col("action").str.strip_chars().str.to_lowercase())
    except Exception as e:  # noqa: BLE001 - a bad override file must not stop prices_finalize
        log.warning("price_identity_breaks.csv unreadable (%s); no overrides", e)
        return pl.DataFrame(schema=schema)


def apply_overrides(breaks: pl.DataFrame, overrides: pl.DataFrame, symbols: pl.DataFrame | None = None) -> pl.DataFrame:
    """Force / suppress breaks. `symbols` [source_id, symbol, security_id] maps symbol keys to securities."""
    if overrides is None or overrides.is_empty():
        return breaks
    ov = overrides
    if symbols is not None and not symbols.is_empty():
        by_sym = ov.join(symbols.select(pl.col("symbol").alias("key"), pl.col("security_id").alias("__sid")).unique(),
                         on="key", how="left")
        ov = by_sym.with_columns(pl.coalesce(pl.col("__sid"), pl.col("key")).alias("security_id")).drop("__sid")
    else:
        ov = ov.with_columns(pl.col("key").alias("security_id"))
    sup = ov.filter(pl.col("action") == "suppress")
    out = breaks
    if sup.height and out.height:
        idx = out.with_row_index("__rid")
        m = idx.select("__rid", "security_id", "source_id", "break_date").join(
            sup.select("security_id", pl.col("source_id").alias("__os"), pl.col("break_date").alias("__bd")),
            on="security_id", how="inner")
        # suppress: matching source ('*' = any) and break date (null = every break of the security)
        hit = (((pl.col("__os") == "*") | (pl.col("__os") == pl.col("source_id")))
               & (pl.col("__bd").is_null() | (pl.col("__bd") == pl.col("break_date"))))
        rids = m.filter(hit).get_column("__rid").unique()
        out = idx.with_columns((pl.col("is_break") & ~pl.col("__rid").is_in(rids.implode())).alias("is_break")).drop("__rid")
    frc = ov.filter((pl.col("action") == "force") & pl.col("break_date").is_not_null())
    if frc.height:
        add = frc.select("security_id", pl.col("source_id"), pl.lit(None, pl.Date).alias("prev_date"), "break_date",
                         pl.lit(None, pl.Float64).alias("prev_close"), pl.lit(None, pl.Float64).alias("close"),
                         pl.lit(None, pl.Float64).alias("ratio"), pl.lit(None, pl.Int64).alias("gap_sessions"),
                         pl.lit(None, pl.Float64).alias("vol_ratio"), pl.lit(["manual_override"]).alias("evidence"),
                         pl.lit(True).alias("is_break"))
        out = pl.concat([out, add.cast(out.schema)], how="vertical_relaxed")  # type: ignore[arg-type]
    return out


def apply_identity_breaks(res: pl.DataFrame, breaks: pl.DataFrame) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Drop each source's bars dated before its latest confirmed break (source_id '*' = every source)."""
    rep: dict[str, Any] = {"series_split": 0, "securities_split": 0, "rows_dropped": 0}
    ok = breaks.filter(pl.col("is_break")) if breaks.height else breaks
    if ok.is_empty() or res.is_empty():
        return res, rep
    cut = ok.group_by("security_id", "source_id").agg(pl.col("break_date").max().alias("__cut"))
    star = cut.filter(pl.col("source_id") == "*").select("security_id", pl.col("__cut").alias("__cut_all"))
    per = cut.filter(pl.col("source_id") != "*")
    t = res.join(per, on=["security_id", "source_id"], how="left")
    if star.height:
        t = t.join(star, on="security_id", how="left")
        cutc = pl.max_horizontal(pl.col("__cut"), pl.col("__cut_all"))
    else:
        cutc = pl.col("__cut")
    drop = cutc.is_not_null() & (pl.col("date") < cutc)
    n0 = t.height
    out = t.filter(~drop.fill_null(False)).drop([c for c in ("__cut", "__cut_all") if c in t.columns])
    rep.update({"series_split": cut.height, "securities_split": cut.get_column("security_id").n_unique(),
                "rows_dropped": n0 - out.height,
                "breaks": ok.sort("break_date", descending=True).head(25).select(
                    "security_id", "source_id", pl.col("break_date").cast(pl.Utf8), pl.col("ratio").round(3),
                    "gap_sessions", pl.col("evidence").list.join("+")).to_dicts()})
    return out, rep


# ================================================================================================ 2. unit / scale flips


@dataclass(frozen=True)
class UnitParams:
    decades: tuple[int, ...] = (2, 3)  # 100x and 1000x
    tol: float = 0.12                   # |log10(ratio) - k| (100x: 76..132)
    max_flip_bars: int = 90             # flip pair: opposite jump within this many bars
    vol_tol: float = 0.5                # inverse volume move within half a decade of k
    vol_window: int = 10


def _decade(r: pl.Expr, p: UnitParams) -> pl.Expr:
    lg = r.log10()
    k = lg.round(0)
    near = (lg - k).abs() <= p.tol
    return pl.when(near & k.abs().is_in(list(p.decades))).then(k.cast(pl.Int64)).otherwise(None)


def detect_unit_jumps(df: pl.DataFrame, p: UnitParams | None = None, price_col: str = "adj_close",
                      raw_col: str | None = None) -> pl.DataFrame:
    """Rows of `df` [security_id, date, <price_col>(, <raw_col>), volume] where the price jumps by ~10^k vs the
    previous bar: security_id, date, prev_date, k (signed decade), vol_k (signed decade of the volume move),
    raw_k (decade of the raw-print move when `raw_col` is given; differs from k when the jump comes from the
    adjustment factors, i.e. a corporate-action error rather than a unit flip)."""
    p = p or UnitParams()
    d = df.sort("security_id", "date").with_columns(
        (pl.col(price_col) / pl.col(price_col).shift(1).over("security_id")).alias("__r"),
        pl.col("date").shift(1).over("security_id").alias("prev_date"),
        pl.int_range(pl.len()).over("security_id").alias("__i"))
    rk = (_decade(pl.col(raw_col) / pl.col(raw_col).shift(1).over("security_id"), p) if raw_col
          else _decade(pl.col("__r"), p))
    d = d.with_columns(rk.alias("raw_k"))
    j = d.filter(_decade(pl.col("__r"), p).is_not_null()).with_columns(_decade(pl.col("__r"), p).alias("k"))
    if j.is_empty():
        return j.select("security_id", "date", "prev_date", "k", "__i", "raw_k").with_columns(
            pl.lit(None, pl.Float64).alias("vol_k"))
    if "volume" in d.columns:
        w = p.vol_window
        v = d.select("security_id", pl.col("__i").alias("__j"), "volume")
        jj = j.select("security_id", "date", "__i")
        pre = (jj.join(v, on="security_id").filter((pl.col("__j") < pl.col("__i")) & (pl.col("__j") >= pl.col("__i") - w))
               .group_by("security_id", "date").agg(pl.col("volume").filter(pl.col("volume") > 0).median().alias("__vb")))
        post = (jj.join(v, on="security_id").filter((pl.col("__j") >= pl.col("__i")) & (pl.col("__j") < pl.col("__i") + w))
                .group_by("security_id", "date").agg(pl.col("volume").filter(pl.col("volume") > 0).median().alias("__va")))
        j = (j.join(pre, on=["security_id", "date"], how="left").join(post, on=["security_id", "date"], how="left")
             .with_columns((pl.col("__va") / pl.col("__vb")).log10().alias("vol_k")))
    else:
        j = j.with_columns(pl.lit(None, pl.Float64).alias("vol_k"))
    return j.select("security_id", "date", "prev_date", "k", "__i", "vol_k", "raw_k")


def plan_unit_repairs(jumps: pl.DataFrame, n_bars: dict[str, int] | None = None, minor_unit: set[str] | None = None,
                      p: UnitParams | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Decide segment repairs from the 10^k jumps of each security. Returns (repairs, unresolved).

    repair: {security_id, start_i, end_i (inclusive bar index), price_mult, vol_div, factor_mult, reason, date, k}:
      flip_pair          opposite jump of the same decade within max_flip_bars: prices of the segment x 10^-k
      double_unadjusted  persistent jump, volume moves by -2k decades (source history already raw and split-
                         unadjusted again, DIA.MC): earlier prices x 10^k, volume / 10^k
      unrecorded_split   persistent jump, volume moves by -k decades (split missing from the actions): earlier
                         adj/split factors x 10^k, raw prints untouched
      minor_unit_switch  persistent 100x jump on a GBp/ZAc/ILA line, volume unchanged: earlier prices x 10^k"""
    p = p or UnitParams()
    minor_unit = minor_unit or set()
    repairs: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []

    def near(x: float | None, target: float) -> bool:
        return x is not None and math.isfinite(x) and abs(x - target) <= p.vol_tol

    for sid, g in jumps.sort("security_id", "__i").group_by("security_id", maintain_order=True):
        sid = sid[0] if isinstance(sid, tuple) else sid
        rows = g.to_dicts()
        used: set[int] = set()
        for a in range(len(rows)):
            if a in used:
                continue
            ra = rows[a]
            k = int(ra["k"])
            base = {"security_id": sid, "date": ra["date"], "k": k}
            if "raw_k" in ra and ra["raw_k"] != k:  # the factors jump, not the prints: corporate-action error
                used.add(a)
                unresolved.append({**base, "vol_k": None, "why": "factor_artifact"})
                continue
            pair = next((b for b in range(a + 1, len(rows)) if b not in used and rows[b]["k"] == -k
                         and rows[b]["__i"] - ra["__i"] <= p.max_flip_bars), None)
            if pair is not None:
                used |= {a, pair}
                repairs.append({**base, "start_i": ra["__i"], "end_i": rows[pair]["__i"] - 1, "price_mult": 10.0 ** (-k),
                                "vol_div": 1.0, "factor_mult": 1.0, "reason": "flip_pair"})
                continue
            vk = ra.get("vol_k")
            seg = {"start_i": 0, "end_i": ra["__i"] - 1}
            if near(vk, -2 * k):
                used.add(a)
                repairs.append({**base, **seg, "price_mult": 10.0 ** k, "vol_div": 10.0 ** k, "factor_mult": 1.0,
                                "reason": "double_unadjusted"})
            elif near(vk, -k):
                used.add(a)
                repairs.append({**base, **seg, "price_mult": 1.0, "vol_div": 1.0, "factor_mult": 10.0 ** k,
                                "reason": "unrecorded_split"})
            elif sid in minor_unit and abs(k) == 2 and (vk is None or not math.isfinite(vk) or near(vk, 0.0)):
                used.add(a)
                repairs.append({**base, **seg, "price_mult": 10.0 ** k, "vol_div": 1.0, "factor_mult": 1.0,
                                "reason": "minor_unit_switch"})
            else:
                unresolved.append({**base, "vol_k": None if vk is None or not math.isfinite(vk) else round(vk, 2)})
    _ = n_bars
    return repairs, unresolved


def repair_unit_scale(win: pl.DataFrame, p: UnitParams | None = None,
                      price_cols: Sequence[str] = ("open", "high", "low", "close", "vwap")) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Repair 10^k unit flips in finalized bars (see plan_unit_repairs). `win` needs security_id, date, close,
    adj_factor (+ split_factor, volume, quote_currency/currency when present); the caller recomputes adj_close.
    Returns (bars, report); repaired bars carry `__unit_fix` = True."""
    p = p or UnitParams()
    rep: dict[str, Any] = {"securities_repaired": 0, "bars_rescaled": 0, "by_reason": {}, "unresolved": 0}
    if win.is_empty():
        return win.with_columns(pl.lit(False).alias("__unit_fix")), rep
    w = win.sort("security_id", "date").with_columns(
        (pl.col("close") * pl.col("adj_factor").fill_null(1.0)).alias("__a"),
        pl.int_range(pl.len()).over("security_id").alias("__i"))
    jumps = detect_unit_jumps(w.select("security_id", "date", "__a", "close",
                                       *(["volume"] if "volume" in w.columns else [])), p, price_col="__a", raw_col="close")
    if jumps.is_empty():
        return w.drop("__a", "__i").with_columns(pl.lit(False).alias("__unit_fix")), rep
    qc = "quote_currency" if "quote_currency" in w.columns else ("currency" if "currency" in w.columns else None)
    minor: set[str] = set()
    if qc:
        minor = set(w.filter(pl.col(qc).is_in(list(MINOR_UNIT_CCY))).get_column("security_id").unique().to_list())
    repairs, unresolved = plan_unit_repairs(jumps, None, minor, p)
    rep["unresolved"] = len(unresolved)
    rep["unresolved_top"] = [{**u, "date": str(u["date"])} for u in unresolved[:15]]
    rep["_unresolved"] = [(u["security_id"], u["date"]) for u in unresolved]   # incremental finalize (escalation)
    if not repairs:
        return w.drop("__a", "__i").with_columns(pl.lit(False).alias("__unit_fix")), rep
    rp = pl.DataFrame(repairs).select("security_id", "start_i", "end_i", "price_mult", "vol_div", "factor_mult")
    seg = (w.select("security_id", "__i").join(rp, on="security_id", how="inner")
           .filter((pl.col("__i") >= pl.col("start_i")) & (pl.col("__i") <= pl.col("end_i")))
           .group_by("security_id", "__i").agg(pl.col("price_mult").product().alias("__m"),
                                               pl.col("vol_div").product().alias("__vd"),
                                               pl.col("factor_mult").product().alias("__fm")))
    w = w.join(seg, on=["security_id", "__i"], how="left").with_columns(
        pl.col("__m").fill_null(1.0), pl.col("__vd").fill_null(1.0), pl.col("__fm").fill_null(1.0))
    fix = [(pl.col(c) * pl.col("__m")).alias(c) for c in price_cols if c in w.columns]
    if "volume" in w.columns:
        fix.append((pl.col("volume") / pl.col("__vd")).alias("volume"))
    fix += [(pl.col(c) * pl.col("__fm")).alias(c) for c in ("adj_factor", "split_factor") if c in w.columns]
    w = w.with_columns(fix).with_columns(
        ((pl.col("__m") != 1.0) | (pl.col("__vd") != 1.0) | (pl.col("__fm") != 1.0)).alias("__unit_fix"))
    rep["securities_repaired"] = len({r["security_id"] for r in repairs})
    rep["bars_rescaled"] = int(w.get_column("__unit_fix").sum())
    by: dict[str, int] = {}
    for r in repairs:
        by[r["reason"]] = by.get(r["reason"], 0) + 1
    rep["by_reason"] = by
    rep["repairs_top"] = [{"security_id": r["security_id"], "date": str(r["date"]), "k": r["k"], "reason": r["reason"]}
                          for r in repairs[:25]]
    return w.drop("__a", "__i", "__m", "__vd", "__fm"), rep


def fx_scale_glitches(fx: pl.DataFrame, key: str = "security_id", value_col: str = "close",
                      decades: tuple[int, ...] = (1, 2, 3), max_flip_bars: int = 30) -> pl.DataFrame:
    """Yahoo FX pairs (prices_daily region FX): flip pairs of 10^k (k = 1..3; FX never moves 10x in a day).
    Returns [key, date, k, start, end] for each temporary scale segment (to rescale or drop)."""
    p = UnitParams(decades=decades, max_flip_bars=max_flip_bars, tol=0.15)
    d = fx.rename({key: "security_id"}) if key != "security_id" else fx
    j = detect_unit_jumps(d.select("security_id", "date", value_col), p, price_col=value_col).drop("raw_k")
    if j.is_empty():
        return pl.DataFrame(schema={key: pl.Utf8, "start": pl.Date, "end": pl.Date, "k": pl.Int64})
    reps, _ = plan_unit_repairs(j.with_columns(pl.lit(None, pl.Float64).alias("vol_k")), {}, set(), p)
    rows = [r for r in reps if r["reason"] == "flip_pair"]
    if not rows:
        return pl.DataFrame(schema={key: pl.Utf8, "start": pl.Date, "end": pl.Date, "k": pl.Int64})
    idx = d.sort("security_id", "date").with_columns(pl.int_range(pl.len()).over("security_id").alias("__i"))
    out = []
    for r in rows:
        seg = idx.filter((pl.col("security_id") == r["security_id"]) & (pl.col("__i") >= r["start_i"])
                         & (pl.col("__i") <= r["end_i"]))
        out.append({key: r["security_id"], "start": seg["date"].min(), "end": seg["date"].max(), "k": r["k"]})
    return pl.DataFrame(out)


# ================================================================================================ 3. missing sessions


def session_coverage(counts: pl.DataFrame, expected: dict[str, Iterable[date]], *, min_coverage: float = 0.5,
                     baseline_window: int = 10, min_baseline: int = 5) -> pl.DataFrame:
    """Pure check. counts [calendar, date, n] (bars per calendar and date), expected {calendar: sessions}.
    Baseline = median n of the surrounding `baseline_window` sessions on each side that had bars. Returns one row
    per expected session below min_coverage: calendar, session_date, n_bars, baseline, coverage, status
    (missing | partial). Calendars whose baseline is below `min_baseline` bars are ignored (too few listings)."""
    rows = []
    cmap: dict[str, dict[date, int]] = {}
    for cal, d, n in counts.select("calendar", "date", "n").iter_rows():
        cmap.setdefault(cal, {})[d] = int(n)
    for cal, sessions in expected.items():
        ss = sorted(set(sessions))
        got = cmap.get(cal, {})
        ns = [got.get(d, 0) for d in ss]
        for i, d in enumerate(ss):
            ctx = [x for x in ns[max(0, i - baseline_window):i] + ns[i + 1:i + 1 + baseline_window] if x > 0]
            if not ctx:
                continue
            ctx.sort()
            base = ctx[len(ctx) // 2] if len(ctx) % 2 else (ctx[len(ctx) // 2 - 1] + ctx[len(ctx) // 2]) / 2
            if base < min_baseline:
                continue
            cov = ns[i] / base
            if cov < min_coverage:
                rows.append({"calendar": cal, "session_date": d, "n_bars": ns[i], "baseline": float(base),
                             "coverage": round(cov, 4), "status": "missing" if ns[i] == 0 else "partial"})
    schema = {"calendar": pl.Utf8, "session_date": pl.Date, "n_bars": pl.Int64, "baseline": pl.Float64,
              "coverage": pl.Float64, "status": pl.Utf8}
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


@lru_cache(maxsize=128)
def window_sessions(calendar: str, lo: date, hi: date) -> tuple[date, ...]:
    """Sessions of `calendar` in [lo, hi] from a calendar built for that window only (much faster than the
    1990-onwards calendars of app.normalize.prices.trading_sessions; same exchange_calendars rules)."""
    try:
        import exchange_calendars as xcals  # noqa: PLC0415

        cal = xcals.get_calendar(calendar, start=(lo - timedelta(days=7)).isoformat(),
                                 end=(hi + timedelta(days=7)).isoformat())
        return tuple(d.date() for d in cal.sessions if lo <= d.date() <= hi)
    except Exception as e:  # noqa: BLE001 - unknown code / window outside the calendar bounds
        log.debug("window_sessions(%s) failed: %s", calendar, e)
        from app.normalize.prices import trading_sessions  # noqa: PLC0415

        ss = trading_sessions(calendar, lo, hi)
        return tuple(sorted(ss)) if ss else ()


def _mic_calendar() -> dict[str, str]:
    from app.normalize.prices import _MARKETS, MIC_CALENDAR_ALIASES  # noqa: PLC0415

    return {**{m.mic: m.calendar for m in _MARKETS if m.calendar}, **MIC_CALENDAR_ALIASES}


def missing_price_sessions(start: date, end: date, *, regions: Sequence[str] | None = None, settings: Any = None,
                           min_coverage: float = 0.5, today: date | None = None) -> pl.DataFrame:
    """Calendar-based missing-session check per exchange (entry point for the Data Ops gap detector).

    For every exchange calendar of the current primary listings (prices_daily x listings.mic -> exchange_calendars
    code), compares bars per session in [start, end] with the market's typical count. Sessions after the last
    completed session (`today` - 1) are not expected yet. Lazy scans, one region partition at a time.
    Returns: calendar, region, session_date, n_bars, baseline, coverage, status (missing | partial), mics (list)."""
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415
    s = settings or get_settings()
    today = today or datetime.now(UTC).date()
    end = min(end, today - timedelta(days=1))
    lo = start - timedelta(days=30)
    hi = min(end + timedelta(days=30), today)
    cal_of = _mic_calendar()
    lst = (scan_table("listings", s).filter(pl.col("is_primary"))
           .select("listing_id", "mic").collect())
    lst = lst.with_columns(pl.col("mic").replace_strict(cal_of, default=None, return_dtype=pl.Utf8).alias("calendar"))
    lst = lst.filter(pl.col("calendar").is_not_null())
    lf = scan_table("prices_daily", s).filter((pl.col("year") >= lo.year) & (pl.col("year") <= hi.year)
                                             & (pl.col("date") >= lo) & (pl.col("date") <= hi)
                                             & ~pl.col("security_id").str.contains(r"^(IDX|ETF):"))
    regs = list(regions) if regions else None
    if regs:
        lf = lf.filter(pl.col("region").is_in(regs))
    counts = (lf.select("listing_id", "date", "region").join(lst.lazy(), on="listing_id", how="inner")
              .group_by("calendar", "region", "date").agg(pl.len().alias("n")).collect())
    if counts.is_empty():
        return pl.DataFrame(schema={"calendar": pl.Utf8, "region": pl.Utf8, "session_date": pl.Date, "n_bars": pl.Int64,
                                    "baseline": pl.Float64, "coverage": pl.Float64, "status": pl.Utf8,
                                    "mics": pl.List(pl.Utf8)})
    reg_of = (counts.group_by("calendar", "region").agg(pl.col("n").sum()).sort("n", descending=True)
              .unique(subset=["calendar"], keep="first"))
    c2 = counts.group_by("calendar", "date").agg(pl.col("n").sum())
    expected: dict[str, list[date]] = {}
    for cal in c2.get_column("calendar").unique().to_list():
        ss = window_sessions(cal, lo, hi)
        if ss:
            expected[cal] = list(ss)
    gaps = session_coverage(c2, expected, min_coverage=min_coverage)
    gaps = gaps.filter((pl.col("session_date") >= start) & (pl.col("session_date") <= end))
    mics = lst.group_by("calendar").agg(pl.col("mic").unique().sort().alias("mics"))
    return (gaps.join(reg_of.select("calendar", "region"), on="calendar", how="left")
            .join(mics, on="calendar", how="left")
            .select("calendar", "region", "session_date", "n_bars", "baseline", "coverage", "status", "mics")
            .sort("calendar", "session_date"))


def refetch_symbols_for_gaps(gaps: pl.DataFrame, settings: Any = None) -> list[tuple[date, list[str]]]:
    """Yahoo symbols (current primary listings of the gap calendars, minus official bulk markets) grouped by fetch
    start (earliest gap session - 3 days per calendar): [(start, [yf symbols])]."""
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.ingestion.yf_prices import OFFICIAL_BULK_SUFFIXES  # noqa: PLC0415
    from app.normalize.prices import yf_suffix  # noqa: PLC0415

    if gaps.is_empty():
        return []
    s = settings or get_settings()
    cal_of = _mic_calendar()
    lst = (scan_table("listings", s).filter(pl.col("is_primary") & pl.col("valid_to").is_null()
                                             & pl.col("yf_ticker").is_not_null())
           .select("yf_ticker", "mic").collect()
           .with_columns(pl.col("mic").replace_strict(cal_of, default=None, return_dtype=pl.Utf8).alias("calendar")))
    first = gaps.group_by("calendar").agg(pl.col("session_date").min().alias("d0"))
    out: dict[date, list[str]] = {}
    for cal, d0 in first.iter_rows():
        syms = [t for t in lst.filter(pl.col("calendar") == cal).get_column("yf_ticker").to_list()
                if yf_suffix(t) not in OFFICIAL_BULK_SUFFIXES]
        if syms:
            out.setdefault(d0 - timedelta(days=3), []).extend(syms)
    return sorted((k, sorted(set(v))) for k, v in out.items())


# ================================================================================================ unfinished bars


def last_closed_session(calendar: str | None, now: datetime | None = None, buffer_min: int = 30) -> date | None:
    """Latest session of `calendar` whose close (+ buffer) is at or before `now` (UTC). None if unknown."""
    if not calendar:
        return None
    now = now or datetime.now(UTC)
    try:
        import exchange_calendars as xcals  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415

        cal = _xcal(calendar)
        if cal is None:
            return None
        ts = pd.Timestamp(now).tz_convert("UTC") if pd.Timestamp(now).tzinfo else pd.Timestamp(now, tz="UTC")
        ts = ts - pd.Timedelta(minutes=buffer_min)
        lo = (ts - pd.Timedelta(days=15)).normalize().tz_localize(None)
        ss = cal.sessions_in_range(max(lo, cal.first_session), min(ts.normalize().tz_localize(None), cal.last_session))
        for sess in reversed(list(ss)):
            if cal.session_close(sess) <= ts:
                return sess.date()
        _ = xcals
    except Exception as e:  # noqa: BLE001
        log.debug("last_closed_session(%s) failed: %s", calendar, e)
    return None


_XCAL: dict[str, Any] = {}


def _xcal(code: str) -> Any:
    if code not in _XCAL:
        try:
            import exchange_calendars as xcals  # noqa: PLC0415

            _XCAL[code] = xcals.get_calendar(code)
        except Exception:  # noqa: BLE001
            _XCAL[code] = None
    return _XCAL[code]


def drop_unfinished_bars(prices: pl.DataFrame, now: datetime | None = None, symbol_col: str = "symbol",
                         calendar_of: Any = None, buffer_min: int = 30) -> tuple[pl.DataFrame, int]:
    """Remove daily bars of sessions that had not closed at fetch time (Yahoo serves the live session as a daily
    bar). `calendar_of(symbol) -> exchange_calendars code` (default: Yahoo suffix table). Benchmarks included."""
    if prices.is_empty() or "date" not in prices.columns:
        return prices, 0
    if calendar_of is None:
        from app.normalize.prices import BENCHMARK_BY_TICKER, market_for_yf  # noqa: PLC0415

        def calendar_of(sym: str) -> str | None:  # noqa: ANN001
            b = BENCHMARK_BY_TICKER.get(sym)
            if b is not None:
                return b.calendar
            try:
                return market_for_yf(sym).calendar
            except KeyError:
                return None
    now = now or datetime.now(UTC)
    recent = prices.filter(pl.col("date") >= (now - timedelta(days=3)).date())
    if recent.is_empty():
        return prices, 0
    cut: dict[str, date] = {}
    for sym in recent.get_column(symbol_col).unique().to_list():
        cal = calendar_of(sym)
        if cal and cal not in cut:
            lc = last_closed_session(cal, now, buffer_min)
            if lc is not None:
                cut[cal] = lc
    if not cut:
        return prices, 0
    m = pl.DataFrame({symbol_col: recent.get_column(symbol_col).unique().to_list()}).with_columns(
        pl.col(symbol_col).map_elements(lambda s: cut.get(calendar_of(s) or ""), return_dtype=pl.Date).alias("__lc"))
    t = prices.join(m, on=symbol_col, how="left")
    keep = pl.col("__lc").is_null() | (pl.col("date") <= pl.col("__lc"))
    out = t.filter(keep).drop("__lc")
    return out, prices.height - out.height


def session_close_utc(calendar: str | None, d: date) -> datetime | None:
    """Scheduled close (UTC) of session `d` on `calendar` (None if unknown / not a session)."""
    cal = _xcal(calendar) if calendar else None
    if cal is None:
        return None
    try:
        import pandas as pd  # noqa: PLC0415

        ts = pd.Timestamp(d)
        if not cal.is_session(ts):
            return None
        return cal.session_close(ts).to_pydatetime().astimezone(UTC)
    except Exception:  # noqa: BLE001
        return None


def drop_unfinished_staged(res: pl.DataFrame, now: datetime | None = None, recent_days: int = 10,
                           buffer_min: int = 30, sources: Sequence[str] = ("yfinance",)) -> tuple[pl.DataFrame, int]:
    """prices_finalize guard: drop staged bars of `sources` that were INGESTED before their session closed (+buffer),
    i.e. Yahoo's in-progress daily bar stored as if final (audit F05: SHEL.L 2026-10-13 volume 1.1 M vs ~8 M).
    Needs res[source_id, calendar, date, ingested_at]; only the last `recent_days` are checked (older bars were
    re-fetched by the 3-day overlap of the nightly). The next fetch after the close restores the bar."""
    need = {"source_id", "calendar", "date", "ingested_at"}
    if res.is_empty() or not need <= set(res.columns):
        return res, 0
    now = now or datetime.now(UTC)
    cut = (now - timedelta(days=recent_days)).date()
    rec = res.filter(pl.col("source_id").is_in(list(sources)) & (pl.col("date") >= cut))
    if rec.is_empty():
        return res, 0
    keys = rec.select("calendar", "date").unique().drop_nulls()
    closes = [{"calendar": c, "date": d, "__close": session_close_utc(c, d)} for c, d in keys.iter_rows()]
    cl = pl.DataFrame(closes, schema={"calendar": pl.Utf8, "date": pl.Date, "__close": pl.Datetime("us", "UTC")})
    t = res.join(cl, on=["calendar", "date"], how="left")
    ing = pl.col("ingested_at").cast(pl.Datetime("us", "UTC"))
    bad = (pl.col("source_id").is_in(list(sources)) & pl.col("__close").is_not_null()
           & (ing < pl.col("__close") + pl.duration(minutes=buffer_min)))
    out = t.filter(~bad.fill_null(False)).drop("__close")
    return out, res.height - out.height



def refill_missing_sessions(start: date, end: date, *, regions: Sequence[str] | None = None, settings: Any = None,
                            min_coverage: float = 0.5, dry_run: bool = False, batch_size: int = 200) -> dict[str, Any]:
    """Targeted Yahoo re-fetch of the sessions `missing_price_sessions` reports (same adapter + staging upsert as the
    nightly, so a refill is identical to a live fetch). Returns {gaps, symbols, rows_written, regions}; the caller
    then re-runs prices_finalize for `regions`."""
    from app.core.config import get_settings  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415
    from app.ingestion.yf_prices import YFinancePricesAdapter  # noqa: PLC0415

    s = settings or get_settings()
    gaps = missing_price_sessions(start, end, regions=regions, settings=s, min_coverage=min_coverage)
    plan = refetch_symbols_for_gaps(gaps, s)
    out: dict[str, Any] = {"gaps": gaps.with_columns(pl.col("session_date").cast(pl.Utf8)).to_dicts(),
                           "symbols": sum(len(v) for _, v in plan), "rows_written": {}, "dropped_unfinished": 0,
                           "regions": sorted({r for r in gaps.get_column("region").to_list() if r})}
    if dry_run or not plan:
        return out
    ad = YFinancePricesAdapter(s, batch_size=batch_size)
    for st, syms in plan:
        fr = ad.fetch(entities=syms, start=st)
        for t, v in ad.normalize(fr).items():
            if t == "prices_staging":
                v, n = drop_unfinished_bars(v)
                out["dropped_unfinished"] += n
            out["rows_written"][t] = out["rows_written"].get(t, 0) + write_table(v, t, settings=s)
        log.info("refill %s: %d symbols from %s (%d ok)", ",".join(out["regions"]), len(syms), st, len(fr.entities_ok))
    return out


__all__ = [
    "BreakParams", "UnitParams", "detect_identity_breaks", "apply_identity_breaks", "apply_overrides",
    "load_break_overrides", "detect_unit_jumps", "plan_unit_repairs", "repair_unit_scale", "fx_scale_glitches",
    "session_coverage", "missing_price_sessions", "refetch_symbols_for_gaps", "last_closed_session",
    "drop_unfinished_bars", "drop_unfinished_staged", "session_close_utc", "refill_missing_sessions",
]
