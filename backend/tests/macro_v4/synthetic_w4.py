"""Synthetic macro v2 W4 inputs for the small serving lake (tests/fixtures/serving_small.py hook): the official series
the W4 factor set needs but the base fixture lacks (curve, breakeven, real yield, Baa spread, broad USD, IMF commodity /
copper, BIS policy rates), consistent with the fixture's DGS2 / DGS10, then the real W4 pipeline (macro_factors,
macro_exposures incl. episode replay, regime_performance) runs on the synthetic lake."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table


def _obs_rows(sid: str, src: str, dates: list[date], vals: np.ndarray, lag: int) -> list[tuple[Any, ...]]:
    return [(sid, src, d, float(v), d + timedelta(days=lag)) for d, v in zip(dates, vals, strict=True)]


def add_official_series(s: Settings, as_of: date, seed: int = 11) -> int:
    from app.macro.markets.factors import latest_vintage  # noqa: PLC0415

    rng = np.random.default_rng(seed)
    base = latest_vintage(["fred:DGS2", "fred:DGS10"], s).pivot(on="series_id", index="date", values="value") \
        .drop_nulls().sort("date")
    if base.is_empty():
        start = as_of - timedelta(days=3650)
        days = [start + timedelta(days=i) for i in range((as_of - start).days) if (start + timedelta(days=i)).weekday() < 5]
        d2 = 3.9 + np.cumsum(rng.normal(0, 0.03, len(days)))
        d10 = 4.1 + np.cumsum(rng.normal(0, 0.03, len(days)))
    else:
        days = base["date"].to_list()
        d2, d10 = base["fred:DGS2"].to_numpy(), base["fred:DGS10"].to_numpy()
    n = len(days)
    be = 2.3 + np.cumsum(rng.normal(0, 0.01, n))
    rows: list[tuple[Any, ...]] = []
    rows += _obs_rows("fred:T10Y2Y", "fred", days, d10 - d2, 1)
    rows += _obs_rows("fred:T10YIE", "fred", days, be, 1)
    rows += _obs_rows("fred:DFII10", "fred", days, d10 - be, 1)
    rows += _obs_rows("fred:BAA10Y", "fred", days, 2.0 + np.abs(np.cumsum(rng.normal(0, 0.02, n))), 1)
    rows += _obs_rows("fred:DTWEXBGS", "fred", days, 120 * np.exp(np.cumsum(rng.normal(0, 0.003, n))), 3)
    months = sorted({date(d.year, d.month, 1) for d in days})
    m = len(months)
    rows += _obs_rows("fred:PALLFNFINDEXM", "fred", months, 180 * np.exp(np.cumsum(rng.normal(0, 0.03, m))), 20)
    rows += _obs_rows("fred:PCOPPUSDM", "fred", months, 9000 * np.exp(np.cumsum(rng.normal(0, 0.04, m))), 20)
    for area, lvl in (("US", 4.5), ("XM", 2.5), ("GB", 4.0), ("JP", 0.25), ("CN", 3.1)):
        path = np.maximum(lvl + np.round(np.cumsum(rng.choice([-0.25, 0, 0, 0, 0.25], m)), 2), 0.0)
        rows += _obs_rows(f"bis:WS_CBPOL.M.{area}", "bis", months, path, 30)
    now = datetime.now(UTC)
    df = (pl.DataFrame(rows, schema=["series_id", "source_id", "date", "value", "knowledge_date"], orient="row")
          .filter(pl.col("knowledge_date") <= as_of)
          .with_columns(pl.lit(False).alias("knowledge_date_estimated"), pl.lit(now).alias("ingested_at")))
    return write_table(df, "macro_observations", mode="upsert", settings=s)


def add_macro_w4(s: Settings, as_of: date) -> dict[str, Any]:
    from app.macro.markets.exposures import run_exposures  # noqa: PLC0415
    from app.macro.markets.factors import run_factors  # noqa: PLC0415
    from app.macro.markets.regimes import run_regimes  # noqa: PLC0415

    counts: dict[str, Any] = {"macro_observations_w4": add_official_series(s, as_of)}
    counts["macro_factors"] = run_factors(s, as_of)["rows"]
    rep = run_exposures(s, as_of, run_id="Rsynthetic", history_months=3)
    counts["macro_exposures"] = rep["rows"]
    counts["macro_exposure_fits"] = rep["fits"]
    counts["regime_performance"] = run_regimes(s, as_of)["rows"]
    return counts
