"""Level-type event study + the weekly `level_type_backtest` job (owner: technicals) -> level_type_stats.

Question answered per ticker: *which level types does this stock actually respect?*

Event (point-in-time). For every level instance/series from `levels.build_levels` (identical code to the live levels),
a bar t is a touch event when the bar OPENS more than `band_k`·ATR14(t-1) away from the level and its range then comes
within `band_k`·ATR of it. side = support test if the open was above, resistance test if below. After an event the
instance stays idle until the outcome resolved (no double counting of one test). Only levels with start <= t are
visible (start is after the defining information was complete).

Outcome (horizon h = 10 bars, ATR = ATR14(t-1)):
  hit / bounce   favourable excursion >= hit_x (1.0) ATR before an adverse CLOSE beyond brk_b (0.5) ATR
                 (touch bar counts its close only; a bar that both reaches the target intrabar and closes through
                 the level counts as a break — conservative);
  break          adverse close beyond 0.5 ATR first;
  reaction       MFE / MAE in ATR, follow-through after a break (move in the break direction over the next h bars);
  expectancy_r   limit at the band edge, stop 0.5 ATR beyond the level, target 2R, time exit after h bars.
An event is used for an as-of date D only if its whole outcome window (t + 2h bars) is <= D (no look-ahead).

Random-level control ("shuffle" baseline): every instance/series is replayed with its price shifted by
±U(0.75, 2.5)·ATR (series: a new shift every 20 bars). Same event/outcome logic, same timing distribution, so the
control hit rate is what touching *any* price yields for this stock; edge = level hit rate − control hit rate.

Shrinkage (beta-binomial, hierarchical): universe -> (sector, region) pool -> ticker. Prior strength m per level_key is
estimated by method of moments from the dispersion of ticker hit rates and clipped to [2000, 5000] pseudo-events (the
range that validated best out of sample, see BTParams); rates are shrunk as (hits + m·p_parent) / (n + m), for both the
level and its control. reliability score = logistic(edge_shrunk / 0.02).
"""

from __future__ import annotations

import time
import zlib
from functools import partial
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl

from app.core.logsetup import get_logger
from app.scheduler.base import JobContext, JobResult

from . import kernels as K
from . import taxonomy as T
from .data import Bars
from .levels import LevelSet, build_levels

log = get_logger(__name__)


@dataclass(frozen=True)
class BTParams:
    horizon: int = 10
    hit_x: float = 1.0
    brk_b: float = 0.5
    band_k: float = 0.25
    rr_stop: float = 0.5
    rr_target: float = 2.0
    ctrl_lo: float = 0.75
    ctrl_hi: float = 2.5
    ctrl_block: int = 20
    edge_scale: float = 0.02
    min_events_rank: int = 8
    # prior strength (pseudo-events). Chosen by IS-internal validation (scores from events resolved < 2017 ranked the
    # 2017-2020 OOS edge best at m ~ 3000; the 2021+ hold-out confirmed it): per-ticker deviations from the pooled
    # (sector, region) rate carry little information, so shrinkage is heavy. MoM estimate clipped into this range.
    prior_min: float = 2000.0
    prior_max: float = 5000.0
    prior_default: float = 3000.0
    pool_prior_mult: float = 30.0     # pool -> universe prior = m x 30 (IS-internal validation; small sector x region pools are noisy)


DEFAULT_BT = BTParams()

EVENT_SCHEMA = {
    "key": pl.Int32, "t": pl.Int32, "side": pl.Int8, "hit": pl.Boolean, "hold": pl.Boolean, "brk": pl.Boolean, "mfe_atr": pl.Float64,
    "mae_atr": pl.Float64, "ft_atr": pl.Float64, "r": pl.Float64, "mfe_pct": pl.Float64, "is_control": pl.Boolean,
    "t_date": pl.Date, "res_date": pl.Date,
}


def _seed(security_id: str) -> int:
    return zlib.crc32(security_id.encode("utf-8")) & 0x7FFFFFFF


def _scan(b: Bars, ls: LevelSet, px: np.ndarray, s_px: np.ndarray, t_min: int, p: BTParams):
    cap = max(4096, 4 * (ls.n_static + s_px.shape[0] * 64))
    while True:
        out = K.scan_events(b.o, b.h, b.l, b.c, ls.atr, ls.key, px, ls.start, ls.end, ls.maxev, s_px, ls.s_key,
                            t_min, p.horizon, p.hit_x, p.brk_b, p.band_k, p.rr_stop, p.rr_target, cap)
        if out[0] >= 0:
            return out
        cap *= 4


def control_prices(b: Bars, ls: LevelSet, p: BTParams, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Random-level control: static prices and series shifted by ±U(ctrl_lo, ctrl_hi)·ATR (fixed seed per ticker)."""
    rng = np.random.default_rng(seed)
    atr = ls.atr
    a_start = atr[np.clip(ls.start - 1, 0, ls.n - 1)]
    a_start = np.where(np.isfinite(a_start), a_start, np.nanmedian(atr))
    sgn = np.where(rng.random(ls.n_static) < 0.5, -1.0, 1.0)
    px = ls.px + sgn * rng.uniform(p.ctrl_lo, p.ctrl_hi, ls.n_static) * a_start
    ns, n1 = ls.s_px.shape
    if ns:
        nblk = n1 // p.ctrl_block + 1
        u = np.where(rng.random((ns, nblk)) < 0.5, -1.0, 1.0) * rng.uniform(p.ctrl_lo, p.ctrl_hi, (ns, nblk))
        u = np.repeat(u, p.ctrl_block, axis=1)[:, :n1]
        a_prev = np.r_[np.nan, atr]  # series value at t is compared with atr[t-1]
        s_px = ls.s_px + u * a_prev[None, :n1]
    else:
        s_px = ls.s_px
    return np.where(px > 0, px, np.nan), s_px


def ticker_events(b: Bars, t_min_date: date | None = None, p: BTParams = DEFAULT_BT, with_control: bool = True,
                  ls: LevelSet | None = None) -> pl.DataFrame:
    """All resolved touch events (real + control) for one ticker. One row per (key, bar, side, is_control)."""
    ls = ls or build_levels(b)
    t_min = 1
    if t_min_date is not None:
        t_min = max(1, int(np.searchsorted(b.dates, np.datetime64(t_min_date, "D"), side="left")))
    frames = []
    runs = [(False, ls.px, ls.s_px)]
    if with_control:
        cpx, cs = control_prices(b, ls, p, _seed(b.security_id))
        runs.append((True, cpx, cs))
    for is_ctrl, px, s_px in runs:
        m, k, t, side, epx, eatr, hit, brk, mfe, mae, ft, r, res, hold = _scan(b, ls, px, s_px, t_min, p)
        if m == 0:
            continue
        sl = slice(0, m)
        a = eatr[sl]
        df = pl.DataFrame({
            "key": k[sl].astype(np.int32), "t": t[sl].astype(np.int32), "side": side[sl].astype(np.int8),
            "hit": hit[sl], "hold": hold[sl], "brk": brk[sl], "mfe_atr": mfe[sl] / a, "mae_atr": mae[sl] / a, "ft_atr": ft[sl] / a,
            "r": r[sl], "mfe_pct": mfe[sl] / epx[sl], "is_control": np.full(m, is_ctrl),
            "t_date": b.dates[t[sl]], "res_date": b.dates[np.minimum(res[sl], b.n - 1)],
        })
        frames.append(df)
    if not frames:
        return pl.DataFrame(schema=EVENT_SCHEMA)
    ev = pl.concat(frames).with_columns(pl.col("t_date").cast(pl.Date), pl.col("res_date").cast(pl.Date))
    return ev.unique(subset=["key", "t", "side", "is_control"], keep="first", maintain_order=True)


# ------------------------------------------------------------------------------------------------ aggregation

def sufficient_stats(ev: pl.DataFrame, by: list[str], with_median: bool = False) -> pl.DataFrame:
    """Additive sufficient statistics of events grouped by `by` (+ key, is_control)."""
    sup = pl.col("side") == 1
    aggs = [
        pl.len().cast(pl.Int64).alias("n"),
        pl.col("hit").sum().cast(pl.Int64).alias("hits"),
        pl.col("brk").sum().cast(pl.Int64).alias("breaks"),
        pl.col("hold").sum().cast(pl.Int64).alias("holds"),
        sup.sum().cast(pl.Int64).alias("n_sup"),
        (pl.col("hit") & sup).sum().cast(pl.Int64).alias("hits_sup"),
        pl.col("mfe_atr").sum().alias("sum_mfe"),
        pl.col("mae_atr").sum().alias("sum_mae"),
        pl.col("mfe_pct").sum().alias("sum_mfe_pct"),
        pl.col("ft_atr").is_not_nan().sum().cast(pl.Int64).alias("n_ft"),
        pl.col("ft_atr").fill_nan(0.0).sum().alias("sum_ft"),
        pl.col("r").is_not_nan().sum().cast(pl.Int64).alias("n_r"),
        pl.col("r").fill_nan(0.0).sum().alias("sum_r"),
    ]
    if with_median:
        aggs.append(pl.col("mfe_atr").median().alias("median_mfe"))
    return ev.group_by([*by, "key", "is_control"]).agg(aggs)


_SUM_COLS = ["n", "hits", "holds", "breaks", "n_sup", "hits_sup", "sum_mfe", "sum_mae", "sum_mfe_pct", "n_ft", "sum_ft", "n_r", "sum_r"]


def ticker_stats(b: Bars, as_of_dates: list[date], years: float, p: BTParams = DEFAULT_BT) -> pl.DataFrame:
    """Per-ticker sufficient stats for each as-of date (events with t_date >= as_of - years and res_date <= as_of)."""
    oldest = min(as_of_dates) - timedelta(days=int(365.25 * years))
    ev = ticker_events(b, t_min_date=oldest, p=p)
    if ev.is_empty():
        return pl.DataFrame()
    out = []
    for d in as_of_dates:
        lo = d - timedelta(days=int(365.25 * years))
        sub = ev.filter((pl.col("res_date") <= d) & (pl.col("t_date") >= lo))
        if sub.is_empty():
            continue
        out.append(sufficient_stats(sub, [], with_median=True).with_columns(pl.lit(d).alias("as_of_date"),
                                                                           pl.lit(sub["t_date"].min()).alias("window_start")))
    if not out:
        return pl.DataFrame()
    return pl.concat(out).with_columns(pl.lit(b.security_id).alias("security_id"), pl.lit(b.pool_key).alias("pool_key"))


def prior_strength(tick: pl.DataFrame, p: BTParams = DEFAULT_BT, by: list[str] | None = None) -> pl.DataFrame:
    """Beta-binomial prior strength m per key (method of moments across tickers): m = p(1-p)/tau^2 - 1."""
    by = by or []
    t = tick.filter(pl.col("n") >= 5).with_columns((pl.col("hits") / pl.col("n")).alias("rate"))
    g = t.group_by([*by, "key"]).agg(
        (pl.col("hits").sum() / pl.col("n").sum()).alias("pbar"),
        pl.col("n").mean().alias("nbar"),
        ((pl.col("n") * (pl.col("rate") - pl.col("hits").sum() / pl.col("n").sum()) ** 2).sum() / pl.col("n").sum()).alias("var_w"),
        (1.0 / pl.col("n")).mean().alias("inv_n"),
        pl.len().alias("n_tickers"),
    )
    pq = pl.col("pbar") * (1 - pl.col("pbar"))
    tau2 = (pl.col("var_w") - pq * pl.col("inv_n")).clip(lower_bound=1e-6)
    m = (pq / tau2 - 1.0).clip(p.prior_min, p.prior_max)
    return g.with_columns(pl.when(pl.col("n_tickers") >= 5).then(m).otherwise(p.prior_default).alias("m")).select([*by, "key", "m"])


def combine(tick: pl.DataFrame, p: BTParams = DEFAULT_BT, by: tuple[str, ...] = ("as_of_date",)) -> pl.DataFrame:
    """Ticker sufficient stats (real + control rows) -> level_type_stats rows (ticker, pool and universe rows).

    `by` are extra grouping columns (as_of_date in the job; a split label in research). Returns contract columns
    plus 'key' (int) helper.
    """
    by = list(by)
    real = tick.filter(~pl.col("is_control"))
    ctrl = tick.filter(pl.col("is_control")).select([*by, "security_id", "key", pl.col("n").alias("c_n"), pl.col("hits").alias("c_hits"),
                                                     pl.col("holds").alias("c_holds")])
    ms = prior_strength(real, p, by)
    sums = [pl.col(c).sum() for c in _SUM_COLS]
    uni_c = ctrl.group_by([*by, "key"]).agg(pl.col("c_n").sum(), pl.col("c_hits").sum(), pl.col("c_holds").sum()).with_columns(
        (pl.col("c_hits") / pl.col("c_n")).alias("pc_u"))
    # universe level rate shrunk toward the universe control rate of the same key (edge -> 0 when n is small)
    uni = (real.group_by([*by, "key"]).agg(sums)
           .join(uni_c.select([*by, "key", "pc_u"]), on=[*by, "key"], how="left")
           .join(ms, on=[*by, "key"], how="left").with_columns(pl.col("m").fill_null(p.prior_default))
           .with_columns(pl.col("pc_u").fill_null(pl.col("hits") / pl.col("n")))
           .with_columns(((pl.col("hits") + pl.col("m") * pl.col("pc_u")) / (pl.col("n") + pl.col("m"))).alias("p_u"))
           .drop("pc_u", "m"))
    pool = real.group_by([*by, "pool_key", "key"]).agg(sums)
    pool_c = ctrl.join(real.select([*by, "security_id", "pool_key"]).unique(), on=[*by, "security_id"], how="left") \
        .group_by([*by, "pool_key", "key"]).agg(pl.col("c_n").sum(), pl.col("c_hits").sum(), pl.col("c_holds").sum())
    # pool shrunk toward universe
    pool = (pool.join(pool_c, on=[*by, "pool_key", "key"], how="left")
            .join(uni.select([*by, "key", "p_u"]), on=[*by, "key"], how="left")
            .join(uni_c.select([*by, "key", "pc_u"]), on=[*by, "key"], how="left")
            .join(ms, on=[*by, "key"], how="left")
            .with_columns(pl.col("m").fill_null(p.prior_default), pl.col("c_n").fill_null(0), pl.col("c_hits").fill_null(0), pl.col("c_holds").fill_null(0),
                          pl.col("pc_u").fill_null(pl.col("p_u")))
            .with_columns((pl.col("m") * p.pool_prior_mult).alias("_mp"))
            .with_columns(
                ((pl.col("hits") + pl.col("_mp") * pl.col("p_u")) / (pl.col("n") + pl.col("_mp"))).alias("p_pool"),
                ((pl.col("c_hits") + pl.col("_mp") * pl.col("pc_u")) / (pl.col("c_n") + pl.col("_mp"))).alias("pc_pool"),
            ).drop("_mp"))
    tk = (real.join(ctrl, on=[*by, "security_id", "key"], how="left")
          .join(pool.select([*by, "pool_key", "key", "p_pool", "pc_pool", "m"]), on=[*by, "pool_key", "key"], how="left")
          .with_columns(pl.col("c_n").fill_null(0), pl.col("c_hits").fill_null(0), pl.col("c_holds").fill_null(0))
          .with_columns(
              ((pl.col("hits") + pl.col("m") * pl.col("p_pool")) / (pl.col("n") + pl.col("m"))).alias("hit_rate_shrunk"),
              ((pl.col("c_hits") + pl.col("m") * pl.col("pc_pool")) / (pl.col("c_n") + pl.col("m"))).alias("control_hit_rate_shrunk"),
          ))
    pool_rows = pool.with_columns(
        (pl.lit("*") + pl.col("pool_key")).alias("security_id"),
        pl.col("p_pool").alias("hit_rate_shrunk"), pl.col("pc_pool").alias("control_hit_rate_shrunk"),
    )
    uni_rows = (uni.join(uni_c, on=[*by, "key"], how="left").join(ms, on=[*by, "key"], how="left")
                .with_columns(pl.col("m").fill_null(p.prior_default), pl.col("c_n").fill_null(0), pl.col("c_hits").fill_null(0), pl.col("c_holds").fill_null(0),
                              pl.col("pc_u").fill_null(pl.col("p_u")))
                .with_columns(pl.lit("*").alias("security_id"), pl.lit("*").alias("pool_key"),
                              pl.col("p_u").alias("hit_rate_shrunk"), pl.col("pc_u").alias("control_hit_rate_shrunk")))
    cols = [*by, "security_id", "pool_key", "key", *_SUM_COLS, "c_n", "c_hits", "c_holds", "hit_rate_shrunk", "control_hit_rate_shrunk", "m"]
    extra = [c for c in ("median_mfe", "window_start") if c in tk.columns]
    allr = pl.concat([tk.select(cols + extra), pool_rows.select(cols), uni_rows.select(cols)], how="diagonal_relaxed")
    if "window_start" in allr.columns:  # pool / universe rows: earliest window start of their members
        allr = allr.with_columns(pl.col("window_start").fill_null(pl.col("window_start").min().over(by) if by else pl.col("window_start").min()))
    return _finalize(allr, p, by)


def _finalize(df: pl.DataFrame, p: BTParams, by: list[str]) -> pl.DataFrame:
    n = pl.col("n").cast(pl.Float64)
    nz = pl.when(n > 0)
    df = df.with_columns(
        nz.then(pl.col("hits") / n).alias("hit_rate"),
        nz.then(pl.col("breaks") / n).alias("break_rate"),
        nz.then(pl.col("holds") / n).alias("hold_rate"),
        pl.when(pl.col("c_n") > 0).then(pl.col("c_holds") / pl.col("c_n")).alias("control_hold_rate"),
        nz.then(pl.col("sum_mfe") / n).alias("mean_reaction_atr"),
        nz.then(pl.col("sum_mae") / n).alias("mean_adverse_atr"),
        nz.then(pl.col("sum_mfe_pct") / n).alias("mean_reaction_pct"),
        pl.when(pl.col("n_ft") > 0).then(pl.col("sum_ft") / pl.col("n_ft")).alias("follow_through_atr"),
        pl.when(pl.col("n_r") > 0).then(pl.col("sum_r") / pl.col("n_r")).alias("expectancy_r"),
        pl.when(pl.col("n_sup") > 0).then(pl.col("hits_sup") / pl.col("n_sup")).alias("hit_rate_support"),
        pl.when(pl.col("n") - pl.col("n_sup") > 0).then((pl.col("hits") - pl.col("hits_sup")) / (pl.col("n") - pl.col("n_sup"))).alias("hit_rate_resistance"),
        pl.when(pl.col("c_n") > 0).then(pl.col("c_hits") / pl.col("c_n")).alias("control_hit_rate"),
        (pl.col("hit_rate_shrunk") - pl.col("control_hit_rate_shrunk")).alias("edge"),
        pl.col("n_sup").cast(pl.Int32).alias("n_support"),
        (pl.col("n") - pl.col("n_sup")).cast(pl.Int32).alias("n_resistance"),
        pl.col("c_n").cast(pl.Int32).alias("control_n"),
        pl.col("n").cast(pl.Int32).alias("n_events"),
        pl.col("m").alias("prior_strength"),
    ).with_columns(
        (pl.col("hit_rate") - pl.col("control_hit_rate")).alias("edge_raw"),
        (1.0 / (1.0 + (-pl.col("edge") / p.edge_scale).exp())).alias("score"),
    )
    df = df.with_columns(
        pl.struct((pl.col("n_events") >= p.min_events_rank).cast(pl.Int8), pl.col("score").fill_null(0.0))
        .rank(method="ordinal", descending=True).over([*by, "security_id"]).cast(pl.Int16).alias("rank")
    )
    key_names = pl.Series("level_key", T.KEY_BY_ID)
    lk = pl.DataFrame({"key": np.arange(T.N_KEYS, dtype=np.int32), "level_key": key_names,
                       "level_type": [k.level_type for k in T.LEVEL_KEYS], "timeframe": [k.timeframe for k in T.LEVEL_KEYS],
                       "family": [k.family for k in T.LEVEL_KEYS]})
    df = df.with_columns(pl.col("key").cast(pl.Int32)).join(lk, on="key", how="left")
    if "median_mfe" in df.columns:
        df = df.rename({"median_mfe": "median_reaction_atr"})
    return df


# ------------------------------------------------------------------------------------------------ job

def _snapshot_dates(as_of: date, history: int) -> list[date]:
    out = [as_of]
    d = date(as_of.year, as_of.month, 1) - timedelta(days=1)
    for _ in range(max(0, history)):
        out.append(d)
        d = date(d.year, d.month, 1) - timedelta(days=1)
    return out


def compute_stats(bars: list[Bars], as_of_dates: list[date], years: float, p: BTParams = DEFAULT_BT,
                  workers: int | None = None, cancel=None) -> tuple[pl.DataFrame, int, int]:
    """Event study over Bars -> (level_type_stats frame, n_ok, n_failed). Convenience wrapper for scripts/tests."""
    tick, ok, failed = _tick_stats_parallel(bars, as_of_dates, years, workers, cancel, p)
    return (combine(tick, p, ("as_of_date",)) if not tick.is_empty() else tick), ok, failed


def run(ctx: JobContext) -> JobResult:
    """Weekly level_type_backtest -> level_type_stats (latest as_of + optional month-end history)."""
    from app.core.schema import get_contract
    from app.core.storage import read_table, write_table

    from .data import iter_bars, list_securities, load_ath_prior, load_earnings_dates, load_meta, load_price_panel, years_before
    from .levels import month_end_retention

    t0 = time.perf_counter()
    as_of = ctx.as_of
    years = float(ctx.params.get("years", 10))
    history = int(ctx.params.get("history", 0))
    source = str(ctx.params.get("source", "prices_daily"))
    workers = ctx.params.get("workers")
    batch = int(ctx.params.get("batch", 1500))
    tickers = ctx.params.get("tickers")
    dates = _snapshot_dates(as_of, history)
    load_start = years_before(min(dates), years + 1.5)  # +1.5y warm-up for 52w / 1y profile / swings
    sids = list(tickers) if tickers else list_securities(source, min_last_date=as_of - timedelta(days=30), since=load_start, min_bars=300)
    if not sids:
        return JobResult(status="success", message="no securities with >= 300 bars")
    meta = load_meta(sids)
    earnings = load_earnings_dates(meta)
    parts: list[pl.DataFrame] = []
    ok_total = failed_total = 0
    for i in range(0, len(sids), batch):
        ctx.check_cancelled()
        chunk = sids[i:i + batch]
        panel = load_price_panel(chunk, start=load_start, end=as_of, source=source)
        ath = load_ath_prior(chunk, load_start, source=source)
        bars = list(iter_bars(panel, meta, earnings, ath, min_bars=300))
        tick = _tick_stats_parallel(bars, dates, years, workers, ctx.check_cancelled)
        ok_total += tick[1]
        failed_total += tick[2]
        if not tick[0].is_empty():
            parts.append(tick[0])
        ctx.log.info("level_type_backtest: %d/%d securities done (%.0fs)", min(i + batch, len(sids)), len(sids), time.perf_counter() - t0)
    if not parts:
        return JobResult(status="failed" if failed_total else "success", entities_failed=failed_total, message="no events")
    stats = combine(pl.concat(parts, how="diagonal_relaxed"), DEFAULT_BT, ("as_of_date",))
    out = stats.with_columns(pl.lit(datetime.now(UTC)).alias("computed_at"))
    cols = get_contract("level_type_stats").column_names
    out = out.select([c for c in cols if c in out.columns])
    keep = month_end_retention(read_table("level_type_stats"), as_of)
    if not keep.is_empty():
        keep = keep.filter(~pl.col("as_of_date").is_in(dates))
        out = pl.concat([keep, out], how="diagonal_relaxed")
    rows = write_table(out, "level_type_stats", mode="overwrite")
    ctx.log.info("level_type_backtest: %d securities in %.1fs", ok_total, time.perf_counter() - t0)
    return JobResult(status="partial" if failed_total else "success", rows_written={"level_type_stats": rows},
                     entities_ok=ok_total, entities_failed=failed_total)


def _stats_task(b: Bars, dates: list[date], years: float, p: BTParams) -> pl.DataFrame | None:
    try:
        return ticker_stats(b, dates, years, p)
    except Exception as e:  # noqa: BLE001
        log.warning("level backtest %s failed: %s", b.security_id, e)
        return None


def _tick_stats_parallel(bars: list[Bars], dates: list[date], years: float, workers, cancel,
                         p: BTParams = DEFAULT_BT) -> tuple[pl.DataFrame, int, int]:
    from .parallel import pmap

    res = pmap(partial(_stats_task, dates=dates, years=years, p=p), bars, workers=int(workers) if workers else None, cancel=cancel)
    ok = [r for r in res if r is not None and not r.is_empty()]
    failed = sum(r is None for r in res)
    return (pl.concat(ok, how="diagonal_relaxed") if ok else pl.DataFrame()), len(ok), failed
