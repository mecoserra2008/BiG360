"""Backtest Lab hooks for the conformal sets (docs/CONFORMAL_TECHNICALS_PLAN.md P3). EXPERIMENTAL.

All three economic tests of the P4 gate (docs/CONFORMAL_VALIDATION.md) were negative or mixed:
  (i)   calibrated-band breakout vs a naive N-day-high breakout: FAIL (not better, t = -0.4 to -3.2)
  (ii)  calibrated band stops vs ATR stops: band stops beat ATR stops, but no stop beat "no stop"
  (iii) peer-group break-away fade: FAIL (negative Sharpe after costs, not better than a naive fade)
They are offered as clearly labelled experimental triggers / stop modes, never promoted.

Bands here are recomputed point-in-time from the backtest's own price panel (the nightly conformal_envelopes table
keeps only the last 30 origins): EWMA variance (lambda 0.94) of daily log returns in the panel's currency,
V(h) = h sigma^2, and a pooled (one pool = the backtest universe) rolling split-conformal radius of y^2 / V(h) over the
last 500 realised origins (outcome lag h, re-sorted every 5 sessions) - the production marginal band without the
region x sector pools and the earnings jump term.
"""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.conformal import envelope as V

LEVELS = (80, 90, 95)
HORIZONS = (1, 5, 10, 20)
WINDOW = 500
EXPERIMENTAL_NOTE = ("EXPERIMENTAL - failed / neutral in validation (docs/CONFORMAL_VALIDATION.md): {what}. Model-based; "
                     "not a recommended strategy.")
NOTES = {
    "calibrated_band_breakout": "band breakout was not better than a naive N-day-high breakout",
    "peer_breakaway": "fading the peer-group break-away name had a negative Sharpe after costs (null vs a naive fade)",
    "band_stops": "calibrated band stops beat ATR stops but no stop beat 'no stop' on 12-1 momentum",
}
EVENT_EMPTY = {"entity_id": pl.Utf8, "date": pl.Date, "direction": pl.Int8, "strength": pl.Float64}


def note(kind: str) -> str:
    return EXPERIMENTAL_NOTE.format(what=NOTES[kind])


def log_returns(px: Any) -> np.ndarray:
    """(T, N) daily log returns from the PricePanel, NaN outside each entity's priced life."""
    r = np.log1p(np.clip(px.R, -0.999999, None)).astype(np.float64)
    T = r.shape[0]
    idx = np.arange(T)[:, None]
    alive = (idx > px.first[None, :]) & (idx <= px.last[None, :]) & (px.first[None, :] >= 0)
    return np.where(alive, r, np.nan)


def walk_forward_halfwidths(px: Any, horizon: int, window: int = WINDOW, min_n: int = 200) -> np.ndarray:
    """(T, N, 3) marginal band half-widths sqrt(q_t V_t(h)) at 80 / 90 / 95%, emitted at the close of t from data
    <= t only (radius from origins s with s + h < t). NaN before enough calibration scores exist."""
    if horizon not in HORIZONS:
        raise ValueError(f"horizon must be one of {HORIZONS}")
    r = log_returns(px)
    T, N = r.shape
    E = np.zeros((T, N), dtype=bool)
    s2 = V.ewma_var(r, E)
    Vh = horizon * s2
    L = np.where(np.isfinite(r), r, 0.0).cumsum(axis=0)
    alive = np.isfinite(r) | (np.arange(T)[:, None] == px.first[None, :])
    Y = np.full((T, N), np.nan)
    if horizon < T:
        Y[:-horizon] = L[horizon:] - L[:-horizon]
        ok = alive[:-horizon] & alive[horizon:]
        Y[:-horizon] = np.where(ok, Y[:-horizon], np.nan)
    R = Y * Y / np.maximum(Vh, 1e-14)
    pool = np.zeros(N, dtype=np.int64)
    cal = V.PoolCal(R, pool, np.zeros((T, N), dtype=bool), lag=horizon, window=window, min_n=min_n,
                    alphas=tuple(1 - x / 100 for x in LEVELS)).run(min(T, max(60, horizon + 1)))
    q = cal.q[:, 0, 0, :]                                     # (T, 3) normal pool, plain radius
    return np.sqrt(np.maximum(q, 0.0)[:, None, :] * Vh[:, :, None])


def breakout_events(px: Any, horizon: int = 5, level: int = 90, direction: str = "up",
                    lo: date | None = None, hi: date | None = None) -> pl.DataFrame:
    """Fresh exits of the calibrated band: the h-session move ending at t exceeds the band emitted at t - h."""
    if level not in LEVELS:
        raise ValueError("level must be 80, 90 or 95")
    hw = walk_forward_halfwidths(px, horizon)[:, :, LEVELS.index(level)]
    r = log_returns(px)
    L = np.where(np.isfinite(r), r, 0.0).cumsum(axis=0)
    T, N = L.shape
    h = horizon
    mv = np.full((T, N), np.nan)
    lim = np.full((T, N), np.nan)
    mv[h:] = L[h:] - L[:-h]
    lim[h:] = hw[:-h]
    rows: list[tuple[str, date, int, float]] = []
    dates = px.dates
    for sign in ((1,) if direction == "up" else (1, -1)):
        exc = (sign * mv > lim) & np.isfinite(lim)
        fresh = exc & ~np.vstack([np.zeros((1, N), bool), exc[:-1]])
        ii, jj = np.nonzero(fresh)
        for i, j in zip(ii, jj, strict=True):
            d = dates[i]
            if (lo and d < lo) or (hi and d > hi):
                continue
            rows.append((px.ids[j], d, sign, float(abs(mv[i, j]) / lim[i, j])))
    if not rows:
        return pl.DataFrame(schema=EVENT_EMPTY)
    return pl.DataFrame(rows, schema=EVENT_EMPTY, orient="row")


def band_stops(events: pl.DataFrame, px: Any, horizon: int = 20, level: int = 90, target: bool = False) -> pl.DataFrame:
    """Per-event stop_loss (and optionally take_profit) at the calibrated band edge emitted on the event date: long =
    1 - exp(-hw) below / exp(hw) - 1 above; short mirrored. Fractions, capped at 0.9."""
    if events.is_empty():
        return events.with_columns(pl.lit(None, pl.Float64).alias("stop_loss"), pl.lit(None, pl.Float64).alias("take_profit"))
    hw = walk_forward_halfwidths(px, horizon)[:, :, LEVELS.index(level)]
    col = px.col
    sl, tp = [], []
    for ent, d, dirn in events.select("entity_id", "date", "direction").iter_rows():
        j = col.get(ent)
        k = px.day_index(d, "right") - 1
        w = hw[k, j] if (j is not None and 0 <= k < px.T) else np.nan
        if not np.isfinite(w):
            sl.append(None)
            tp.append(None)
            continue
        down, up = 1.0 - math.exp(-w), math.exp(w) - 1.0
        s_, t_ = (down, up) if (dirn or 1) > 0 else (up, down)
        sl.append(float(min(0.9, s_)))
        tp.append(float(min(10.0, t_)) if target else None)
    return events.with_columns(pl.Series("stop_loss", sl, dtype=pl.Float64), pl.Series("take_profit", tp, dtype=pl.Float64))


def _wf_signal_files(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "reports" / "conformal" / "wf_peer").glob("signals_*_gcn_gru.parquet"))


def peer_breakaway_events(ids: list[str], z: float = 2.5, source: str = "auto", data_dir: Path | None = None,
                          con: Any = None, lo: date | None = None, hi: date | None = None,
                          warns: list[str] | None = None) -> tuple[pl.DataFrame, str]:
    """Fade the member that broke away: days on which the group's joint score exceeded its 90% radius and a member had
    |z_cond| >= z; direction = -sign(z_cond) (shorts need allow_short). One event per (group, date) - the largest
    |z_cond| - and per (security, date). source: walk_forward (P4 GCN-GRU walk-forward signals 2010-2026, research
    files under data/reports/conformal/wf_peer), live (conformal_peer_scores / members: about one year), auto."""
    warns = warns if warns is not None else []
    files = _wf_signal_files(data_dir) if data_dir else []
    use = source if source != "auto" else ("walk_forward" if files else "live")
    idset = set(ids)
    if use == "walk_forward":
        if not files:
            warns.append("peer break-away: no walk-forward signal files (data/reports/conformal/wf_peer); using live tables")
            use = "live"
        else:
            sg = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
            sg = sg.filter((pl.col("score") > pl.col("q90")) & (pl.col("z_cond").abs() >= z))
    if use == "live":
        if con is None:
            return pl.DataFrame(schema=EVENT_EMPTY), use
        try:
            sg = con.execute("""SELECT m.group_id, CAST(m.as_of_date AS DATE) AS date, m.security_id, m.z_cond
                                FROM conformal_peer_members m JOIN conformal_peer_scores s
                                  ON s.group_id = m.group_id AND s.as_of_date = m.as_of_date
                                WHERE coalesce(s.anomaly_90, FALSE) AND abs(m.z_cond) >= ?""", [z]).pl()
        except Exception as e:  # noqa: BLE001 - tables missing in an old lake
            warns.append(f"peer break-away: live tables unavailable ({type(e).__name__})")
            return pl.DataFrame(schema=EVENT_EMPTY), use
        if sg.height and sg["date"].n_unique() < 250:
            warns.append(f"peer break-away (live): only {sg['date'].n_unique()} sessions of history in "
                         "conformal_peer_scores; use source=walk_forward for 2010-2026")
    if sg.is_empty():
        return pl.DataFrame(schema=EVENT_EMPTY), use
    sg = sg.with_columns(pl.col("date").cast(pl.Date)).filter(pl.col("security_id").is_in(list(idset)))
    sg = sg.sort(pl.col("z_cond").abs(), descending=True).unique(["group_id", "date"], keep="first")
    sg = sg.unique(["security_id", "date"], keep="first")
    out = sg.select(pl.col("security_id").alias("entity_id"), "date",
                    (-pl.col("z_cond").sign()).cast(pl.Int8).alias("direction"),
                    pl.col("z_cond").abs().cast(pl.Float64).alias("strength"))
    if lo:
        out = out.filter(pl.col("date") >= lo)
    if hi:
        out = out.filter(pl.col("date") <= hi)
    return out.sort(["date", "entity_id"]), use
