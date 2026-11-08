"""W4.3 regime-conditional performance (job `regime_performance`, nightly) -> regime_performance.

Monthly returns of sector / subsector / region-sector / region portfolios (cw + ew), factor long-short portfolios and
ETFs / indices (app.macro.markets.returns), grouped by the regime state of the same month (contemporaneous) or of the
previous month (next_month) for: cycle_quadrant (US, GLOBAL), hmm_regime (US, GLOBAL; model-based), rate_cycle
(fed, ecb, boe, boj, pboc; macro-w3 cb_rate_cycle or the provisional BIS rule), inflation_regime (US).
metric excess = r - cash (3m T-bill / 12); relative = r - benchmark (S&P 500 for global aggregates and instruments,
the regional index for region aggregates). Factor long-short returns are self-financing: excess = r, no relative.
Statistics per cell: mean, median, ann_mean (x12), vol, hit rate (share > 0), t = mean / (sd / sqrt(n)), n.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import write_table
from app.macro.markets import factors as fx
from app.macro.markets.returns import GLOBAL_BENCH, REGION_BENCH, build_panels

log = get_logger(__name__)

START = date(1995, 1, 1)
RATE_CYCLE_BANKS = ("fed", "ecb", "boe", "boj", "pboc")
MIN_N = 3
SCHEMA: dict[str, Any] = {
    "entity_type": pl.Utf8, "entity_id": pl.Utf8, "weighting": pl.Utf8, "regime_type": pl.Utf8, "scope": pl.Utf8,
    "regime": pl.Utf8, "timing": pl.Utf8, "metric": pl.Utf8, "mean": pl.Float64, "median": pl.Float64,
    "ann_mean": pl.Float64, "vol": pl.Float64, "hit_rate": pl.Float64, "t_stat": pl.Float64, "n": pl.Int32,
    "first_month": pl.Date, "last_month": pl.Date, "is_current": pl.Boolean, "benchmark": pl.Utf8,
    "model_based": pl.Boolean, "computed_at": pl.Datetime("us", "UTC")}


def benchmark_for(key: tuple[str, str, str]) -> str:
    et, eid, _ = key
    if et == "region":
        return REGION_BENCH.get(eid, GLOBAL_BENCH)
    if et == "sector" and ":" in eid:
        return REGION_BENCH.get(eid.split(":")[0], GLOBAL_BENCH)
    return GLOBAL_BENCH


def long_returns(panel, cash: pl.DataFrame) -> pl.DataFrame:  # noqa: ANN001 - returns.Panel
    """[entity_type, entity_id, weighting, month, excess, relative, benchmark]."""
    bench = panel.meta.get("bench", {})
    months = panel.dates
    frames = []
    cash_map = dict(zip(cash["date"].to_list(), cash["value"].to_list(), strict=True))
    c = np.array([cash_map.get(m, np.nan) for m in months], dtype=float)
    bvec = {}
    for b, (bd, bv) in bench.items():
        pos = {d: i for i, d in enumerate(bd)}
        bvec[b] = np.array([bv[pos[m]] if m in pos else np.nan for m in months], dtype=float)
    for j, k in enumerate(panel.keys):
        r = panel.R[:, j]
        ok = np.isfinite(r)
        if ok.sum() < 12:
            continue
        b = benchmark_for(k)
        if k[0] == "factor":
            ex, rel = r, np.full_like(r, np.nan)
        else:
            ex = r - c
            rel = r - bvec.get(b, np.full_like(r, np.nan))
            if k[1] == b:
                rel = np.full_like(r, np.nan)
        frames.append(pl.DataFrame({"entity_type": k[0], "entity_id": k[1], "weighting": k[2],
                                    "month": [m for m, o in zip(months, ok, strict=True) if o],
                                    "excess": ex[ok], "relative": rel[ok], "benchmark": b if k[0] != "factor" else None},
                                   schema_overrides={"month": pl.Date, "benchmark": pl.Utf8}))
    return pl.concat(frames).with_columns(pl.col("excess", "relative").fill_nan(None))


def regime_table(rets: pl.DataFrame, states: pl.DataFrame) -> pl.DataFrame:
    """rets: long_returns; states: [regime_type, scope, date (month), state, model_based]."""
    st = states.select("regime_type", "scope", pl.col("date").alias("month"), "state", "model_based")
    cur = st.sort("month").group_by("regime_type", "scope").agg(pl.col("state").last().alias("current"))
    nxt = st.with_columns(pl.col("month").dt.offset_by("1mo"))
    parts = []
    for timing, s in (("contemporaneous", st), ("next_month", nxt)):
        j = rets.join(s, on="month", how="inner")
        for metric in ("excess", "relative"):
            g = j.filter(pl.col(metric).is_not_null()).group_by(
                "entity_type", "entity_id", "weighting", "regime_type", "scope", "state").agg(
                pl.col(metric).mean().alias("mean"), pl.col(metric).median().alias("median"),
                pl.col(metric).std().alias("vol"), (pl.col(metric) > 0).mean().alias("hit_rate"),
                pl.len().alias("n"), pl.col("month").min().alias("first_month"), pl.col("month").max().alias("last_month"),
                pl.col("benchmark").first().alias("benchmark"), pl.col("model_based").first().alias("model_based"))
            parts.append(g.with_columns(pl.lit(timing).alias("timing"), pl.lit(metric).alias("metric")))
    out = pl.concat(parts).filter(pl.col("n") >= MIN_N).rename({"state": "regime"})
    out = out.join(cur, on=["regime_type", "scope"], how="left").with_columns(
        (pl.col("regime") == pl.col("current")).fill_null(False).alias("is_current"),
        (pl.col("mean") * 12).alias("ann_mean"),
        (pl.col("mean") / (pl.col("vol") / pl.col("n").cast(pl.Float64).sqrt())).alias("t_stat"),
        pl.when(pl.col("metric") == "relative").then(pl.col("benchmark")).otherwise(None).alias("benchmark"))
    return out


def run_regimes(settings: Settings | None = None, end: date | None = None, write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    end = end or datetime.now(UTC).date()
    t0 = time.perf_counter()
    try:
        fac = fx.load_factors(s, "M")
    except Exception:  # noqa: BLE001
        fac = pl.DataFrame()
    if fac.is_empty():
        fac, _ = fx.build_factors(s, end)
        fac = fac.filter(pl.col("frequency") == "M")
    states = fx.states_long(fac)
    states = states.filter((pl.col("regime_type") != "rate_cycle") | pl.col("scope").is_in(list(RATE_CYCLE_BANKS)))
    cash = fac.filter(pl.col("factor_id") == "cash").select("date", "value")
    pan = build_panels(s, "M", START, end, securities=False)["aggregate"]
    rets = long_returns(pan, cash)
    out = regime_table(rets, states)
    out = out.with_columns(pl.lit(datetime.now(UTC)).alias("computed_at"), pl.col(pl.Float64).fill_nan(None))
    out = out.select(list(SCHEMA)).cast(SCHEMA)  # type: ignore[arg-type]
    rep: dict[str, Any] = {"rows": out.height, "entities": rets.select("entity_type", "entity_id", "weighting").unique().height,
                           "months": rets["month"].n_unique()}
    cur = states.sort("date").group_by("regime_type", "scope").agg(pl.col("state").last(), pl.col("date").last())
    rep["current"] = {f"{r['regime_type']}:{r['scope']}": r["state"] for r in cur.iter_rows(named=True)}
    if write:
        rep["rows_written"] = write_table(out, "regime_performance", mode="overwrite", settings=s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job regime_performance (nightly, after macro_factors)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    try:
        rep = run_regimes(ctx.settings, getattr(ctx, "as_of", None))
    except Exception as e:  # noqa: BLE001
        log.exception("regime_performance failed")
        return JobResult(status="failed", message=str(e)[:2000], retryable=True)
    log.info("regime_performance: %s", {k: v for k, v in rep.items() if k != "current"})
    return JobResult(status="success" if rep["rows"] else "failed",
                     rows_written={"regime_performance": rep.get("rows_written", 0)}, entities_ok=rep["entities"],
                     watermark=datetime.now(UTC).date().isoformat(), message=json.dumps(rep, default=str)[:3000])
