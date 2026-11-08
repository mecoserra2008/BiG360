"""Macro v2 cycle clock (W1.2). Job `macro_cycle` -> macro_cycle (one row per country x month).

Method (METHOD_VERSION cycle_v1; reproducible from macro_cell_history, the canonical primary series of each cell):
  1. Inputs per country - growth: gdp_yoy (gdp_qoq when the country has no gdp_yoy cell), industrial_production,
     retail_sales, pmi_proxy (OECD CLI/BCI, EC ESI, Tankan ...); inflation: cpi_yoy, core_cpi_yoy, ppi_yoy.
     Each is the cell's canonical measure (y/y % for index/level series, else the published rate/index), taken from
     the cell's primary series, or - when that history is shorter than 42 months - from the longest candidate
     series of the same cell that is still current (pick_input).
  2. Monthly grid (month start). Monthly inputs are carried forward at most 3 months, quarterly 6, annual 12, so a
     discontinued input drops out instead of freezing the clock.
  3. Momentum: m_t = x_t - x_{t-6} (6-month change, in the input's own units).
  4. Standardisation: z_t = m_t / sd_t, sd_t = std of m over the trailing 120 months ending at t (>= 36 months,
     point-in-time in the sense of using no future observations), clipped to [-3, 3].
  5. Composites: G_t = mean of the available growth z's, I_t = mean of the available inflation z's (n_growth /
     n_inflation report how many inputs contributed); then a 3-month moving average (growth_momentum,
     inflation_momentum). Raw (unsmoothed) composites are kept as growth_raw / inflation_raw.
  6. Quadrant (sign of the smoothed composites): recovery = G > 0, I <= 0; expansion = G > 0, I > 0;
     slowdown = G <= 0, I > 0; contraction = G <= 0, I <= 0. angle_deg = atan2(I, G) in degrees (0 = east = pure
     growth acceleration, counter-clockwise), magnitude = sqrt(G^2 + I^2).
Data vintage: the history uses the latest revised values (macro_cell_history), not first releases; the table is
recomputed nightly. The API serves the last 12 months as the clock trail.
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, write_table
from app.macro.indicators import COUNTRIES

log = get_logger(__name__)

METHOD_VERSION = "cycle_v1"
GROWTH_INPUTS = ["gdp_yoy", "industrial_production", "retail_sales", "pmi_proxy"]
INFLATION_INPUTS = ["cpi_yoy", "core_cpi_yoy", "ppi_yoy"]
MOMENTUM_MONTHS = 6
Z_WINDOW, Z_MIN = 120, 36
Z_CLIP = 3.0
SMOOTH = 3
FFILL_LIMIT = {"D": 3, "W": 3, "M": 3, "Q": 6, "A": 12}
START = date(1995, 1, 1)
QUADRANTS = ("recovery", "expansion", "slowdown", "contraction")

CYCLE_SCHEMA: dict[str, Any] = {
    "country": pl.Utf8, "date": pl.Date, "growth_momentum": pl.Float64, "inflation_momentum": pl.Float64,
    "growth_raw": pl.Float64, "inflation_raw": pl.Float64, "quadrant": pl.Utf8, "angle_deg": pl.Float64,
    "magnitude": pl.Float64, "n_growth": pl.Int8, "n_inflation": pl.Int8, "inputs": pl.Utf8,
    "method_version": pl.Utf8, "computed_at": pl.Datetime("us", "UTC"),
}


def quadrant(g: float | None, i: float | None) -> str | None:
    if g is None or i is None or not (math.isfinite(g) and math.isfinite(i)):
        return None
    if g > 0:
        return "expansion" if i > 0 else "recovery"
    return "slowdown" if i > 0 else "contraction"


def monthly_grid(dates: list[date], values: list[float], freq: str, end: date) -> pd.Series:
    """Canonical (period date, value) -> month-start series up to `end`, carried forward FFILL_LIMIT months."""
    s = pd.Series(values, index=pd.to_datetime(dates)).sort_index()
    s = s[~s.index.duplicated(keep="last")]
    s.index = s.index.to_period("M").to_timestamp()
    s = s.groupby(level=0).last()
    idx = pd.date_range(max(pd.Timestamp(START), s.index.min()) if len(s) else pd.Timestamp(START),
                        pd.Timestamp(end).to_period("M").to_timestamp(), freq="MS")
    return s.reindex(s.index.union(idx)).ffill(limit=FFILL_LIMIT.get(freq, 3)).reindex(idx)


def momentum_z(x: pd.Series) -> pd.Series:
    m = x - x.shift(MOMENTUM_MONTHS)
    sd = m.rolling(Z_WINDOW, min_periods=Z_MIN).std()
    return (m / sd.replace(0.0, np.nan)).clip(-Z_CLIP, Z_CLIP)


def country_cycle(inputs: dict[str, tuple[list[date], list[float], str]], end: date) -> pd.DataFrame:
    """inputs: indicator -> (dates, canonical values, frequency). Returns a monthly frame (see module doc)."""
    zs: dict[str, pd.Series] = {}
    g_inputs = [k for k in GROWTH_INPUTS if k in inputs]
    if "gdp_yoy" not in inputs and "gdp_qoq" in inputs:
        g_inputs = ["gdp_qoq", *g_inputs]
    for k in g_inputs + [k for k in INFLATION_INPUTS if k in inputs]:
        d, v, f = inputs[k]
        if len(d) < Z_MIN // 3:
            continue
        zs[k] = momentum_z(monthly_grid(d, v, f, end))
    if not zs:
        return pd.DataFrame()
    df = pd.DataFrame(zs)
    gc = [k for k in df.columns if k not in INFLATION_INPUTS]
    ic = [k for k in df.columns if k in INFLATION_INPUTS]
    out = pd.DataFrame(index=df.index)
    out["growth_raw"] = df[gc].mean(axis=1) if gc else np.nan
    out["inflation_raw"] = df[ic].mean(axis=1) if ic else np.nan
    out["n_growth"] = df[gc].notna().sum(axis=1) if gc else 0
    out["n_inflation"] = df[ic].notna().sum(axis=1) if ic else 0
    out["growth_momentum"] = out["growth_raw"].rolling(SMOOTH, min_periods=1).mean()
    out["inflation_momentum"] = out["inflation_raw"].rolling(SMOOTH, min_periods=1).mean()
    out["inputs"] = [json.dumps({k: (None if pd.isna(v) else round(float(v), 4)) for k, v in r.items()})
                     for _, r in df.iterrows()]
    return out[(out["n_growth"] > 0) | (out["n_inflation"] > 0)]


def build_cycle(hist: pl.DataFrame, as_of: date | None = None) -> pl.DataFrame:
    """hist = macro_cell_history (primary rows used). -> macro_cycle rows."""
    as_of = as_of or datetime.now(UTC).date()
    now = datetime.now(UTC)
    want = set(GROWTH_INPUTS) | set(INFLATION_INPUTS) | {"gdp_qoq"}
    h = hist.filter(pl.col("indicator").is_in(list(want)) & (pl.col("date") <= as_of)
                    & pl.col("country").is_in(COUNTRIES)).drop_nulls("value")
    rows: list[dict[str, Any]] = []
    for (cc,), g in h.group_by("country"):
        inputs = {}
        for (ind,), gi in g.group_by("indicator"):
            gi = pick_input(gi, as_of)
            if gi is None:
                continue
            inputs[ind] = (gi.get_column("date").to_list(), gi.get_column("value").to_list(), gi.get_column("frequency")[0])
        cyc = country_cycle(inputs, as_of)
        for ts, r in cyc.iterrows():
            gm, im = _f(r["growth_momentum"]), _f(r["inflation_momentum"])
            rows.append({"country": cc, "date": ts.date(), "growth_momentum": gm, "inflation_momentum": im,
                         "growth_raw": _f(r["growth_raw"]), "inflation_raw": _f(r["inflation_raw"]),
                         "quadrant": quadrant(gm, im),
                         "angle_deg": math.degrees(math.atan2(im, gm)) if gm is not None and im is not None else None,
                         "magnitude": math.hypot(gm, im) if gm is not None and im is not None else None,
                         "n_growth": int(r["n_growth"]), "n_inflation": int(r["n_inflation"]), "inputs": r["inputs"],
                         "method_version": METHOD_VERSION, "computed_at": now})
    return pl.DataFrame(rows, schema=CYCLE_SCHEMA) if rows else pl.DataFrame(schema=CYCLE_SCHEMA)


MIN_MONTHS = Z_MIN + MOMENTUM_MONTHS


def pick_input(g: pl.DataFrame, as_of: date) -> pl.DataFrame | None:
    """The cell's primary series when its history spans >= MIN_MONTHS months, else the candidate (same country and
    indicator) with the longest history whose last period is within ~13 months of as_of (e.g. a new monthly CPI
    replaced by the long quarterly one)."""
    best, best_span, prim = None, -1, None
    for (sid,), gs in g.group_by("series_id"):
        gs = gs.sort("date")
        d0, d1 = gs.get_column("date")[0], gs.get_column("date")[-1]
        span = (d1.year - d0.year) * 12 + d1.month - d0.month
        if gs.get_column("is_primary").any():
            prim = (gs, span)
        if (as_of - d1).days <= 400 and span > best_span:
            best, best_span = gs, span
    if prim is not None and prim[1] >= MIN_MONTHS:
        return prim[0]
    return best if best is not None else (prim[0] if prim else None)


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def run_cycle(settings: Settings | None = None, as_of: date | None = None, write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    hist = read_table("macro_cell_history", settings=s)
    cyc = build_cycle(hist, as_of)
    rep: dict[str, Any] = {"rows": cyc.height, "countries": cyc.get_column("country").n_unique() if cyc.height else 0}
    if cyc.height:
        last = cyc.sort("date").group_by("country").agg(pl.all().last())
        rep["latest"] = {r["country"]: r["quadrant"] for r in last.iter_rows(named=True)}
    if write and cyc.height:
        rep["rows_written"] = write_table(cyc, "macro_cycle", mode="overwrite", settings=s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_cycle (after macro_cells). params: as_of (ISO date)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_cycle(ctx.settings, date.fromisoformat(p["as_of"]) if p.get("as_of") else getattr(ctx, "as_of", None))
    log.info("macro_cycle: %s", {k: v for k, v in rep.items() if k != "latest"})
    return JobResult(status="success" if rep["rows"] else "failed", rows_written={"macro_cycle": rep.get("rows_written", 0)},
                     entities_ok=rep["countries"], watermark=datetime.now(UTC).date().isoformat(),
                     message=str(rep)[:2000])
