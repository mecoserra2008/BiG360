"""Surprises vs MODEL-BASED expectations (not consensus) and Citi-style surprise indices (W2.3). Job `macro_surprise`.

* Per release (macro_surprises): error = actual - primary model expectation (app.macro.expectations);
  scale = std of the same series' forecast errors of releases strictly before this one (last 36, min 6), else the std
  of the series' past release-to-release changes (min 6); surprise_z = clip(error / scale, -5, 5);
  signed_z = surprise_z * direction (+1 higher = stronger growth / hotter inflation / tighter labor; -1 unemployment
  and jobless claims).
* Index (macro_surprise_index): per country (+ GLOBAL) and theme (all, growth, inflation, labor), daily
  value = 100 * sum(w_i * signed_z_i) / max(sum(w_i), 1), releases i with release_date in (d - 365, d],
  w_i = tier weight (1.0 / 0.6 / 0.3) * 0.5 ** (age_days / HALFLIFE_DAYS). The max(., 1) floor shrinks the index
  toward 0 when little recent information exists.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

HALFLIFE_DAYS = 45
WINDOW_DAYS = 365
TIER_WEIGHT = {1: 1.0, 2: 0.6, 3: 0.3}
MIN_ERRORS = 6
ERR_WINDOW = 36
Z_CLIP = 5.0
THEMES = ("all", "growth", "inflation", "labor")


def surprise_rows(ev: pl.DataFrame, ex: pl.DataFrame, rel: pl.DataFrame, direction: dict[str, int],
                  now: datetime | None = None) -> pl.DataFrame:
    """ev: released macro_release_events; ex: primary expectations; rel: macro_releases (importance)."""
    now = now or datetime.now(UTC)
    j = (ev.filter(pl.col("actual").is_not_null() & pl.col("period").is_not_null())
         .join(ex.select("release_event_id", "series_id", pl.col("target_period").alias("period"),
                         pl.col("point").alias("expectation"), "lo80", "hi80", "model"),
               on=["release_event_id", "series_id", "period"], how="inner")   # forecast must target the released period
         .join(rel.select("release_event_id", "importance"), on="release_event_id", how="left")
         .sort("series_id", "scheduled_at"))
    out: list[dict[str, Any]] = []
    for (sid,), g in j.group_by(["series_id"], maintain_order=True):
        errs: list[float] = []
        acts: list[float] = []
        for r in g.iter_rows(named=True):
            err = float(r["actual"]) - float(r["expectation"])
            past = errs[-ERR_WINDOW:]
            if len(past) >= MIN_ERRORS:
                scale = float(np.std(past, ddof=1))
            else:
                ch = np.diff(acts[-ERR_WINDOW - 1:]) if len(acts) > MIN_ERRORS else np.array([])
                scale = float(np.std(ch, ddof=1)) if len(ch) >= MIN_ERRORS else None
            z = float(np.clip(err / scale, -Z_CLIP, Z_CLIP)) if scale and scale > 0 else None
            d = int(direction.get(sid, 1))
            out.append({"release_event_id": r["release_event_id"], "series_id": sid, "country": r["country"],
                        "theme": r["theme"], "importance": int(r["importance"] or 3), "release_date": r["release_date"],
                        "scheduled_at": r["scheduled_at"], "period": r["period"], "measure": r["measure"],
                        "actual": float(r["actual"]), "expectation": float(r["expectation"]), "lo80": r["lo80"],
                        "hi80": r["hi80"], "model": r["model"], "error": err, "error_std": scale,
                        "n_hist_errors": len(past), "surprise_z": z, "direction": d,
                        "signed_z": z * d if z is not None else None, "model_based": True, "computed_at": now})
            errs.append(err)
            acts.append(float(r["actual"]))
    schema = {"release_event_id": pl.Utf8, "series_id": pl.Utf8, "country": pl.Utf8, "theme": pl.Utf8,
              "importance": pl.Int8, "release_date": pl.Date, "scheduled_at": pl.Datetime("us", "UTC"),
              "period": pl.Date, "measure": pl.Utf8, "actual": pl.Float64, "expectation": pl.Float64,
              "lo80": pl.Float64, "hi80": pl.Float64, "model": pl.Utf8, "error": pl.Float64, "error_std": pl.Float64,
              "n_hist_errors": pl.Int32, "surprise_z": pl.Float64, "direction": pl.Int8, "signed_z": pl.Float64,
              "model_based": pl.Boolean, "computed_at": pl.Datetime("us", "UTC")}
    return pl.DataFrame(out, schema=schema, orient="row") if out else pl.DataFrame(schema=schema)


def index_series(dates: np.ndarray, ev_dates: np.ndarray, z: np.ndarray, w_tier: np.ndarray,
                 halflife: int = HALFLIFE_DAYS, window: int = WINDOW_DAYS) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """dates, ev_dates: int day numbers. Returns (value, n_events, last_event_day or -1) per date. Point-in-time:
    only events with ev_date <= date contribute."""
    age = dates[:, None] - ev_dates[None, :]
    inwin = (age >= 0) & (age < window)
    w = np.where(inwin, w_tier[None, :] * 0.5 ** (np.clip(age, 0, None) / halflife), 0.0)
    num = (w * z[None, :]).sum(axis=1)
    den = w.sum(axis=1)
    val = 100.0 * num / np.maximum(den, 1.0)
    n = inwin.sum(axis=1)
    last = np.where(inwin, ev_dates[None, :], -1).max(axis=1)
    return val, n, last


def build_index(sur: pl.DataFrame, start: date, end: date, now: datetime | None = None,
                halflife: int = HALFLIFE_DAYS) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    s = sur.filter(pl.col("signed_z").is_not_null())
    days = np.arange(start.toordinal(), end.toordinal() + 1)
    rows: list[pl.DataFrame] = []
    groups: list[tuple[str, str, pl.DataFrame]] = []
    for cc in sorted(s["country"].unique().to_list()) + ["GLOBAL"]:
        sc = s if cc == "GLOBAL" else s.filter(pl.col("country") == cc)
        for th in THEMES:
            g = sc if th == "all" else sc.filter(pl.col("theme") == th)
            if g.height:
                groups.append((cc, th, g))
    for cc, th, g in groups:
        evd = np.array([d.toordinal() for d in g["release_date"].to_list()])
        z = g["signed_z"].to_numpy()
        wt = np.array([TIER_WEIGHT.get(int(i), 0.3) for i in g["importance"].to_list()])
        first = int(evd.min())
        dd = days[days >= first]
        if not len(dd):
            continue
        val, n, last = index_series(dd, evd, z, wt, halflife)
        rows.append(pl.DataFrame({
            "country": cc, "theme": th, "date": [date.fromordinal(int(x)) for x in dd],
            "value": np.where(n > 0, val, np.nan), "n_events": n.astype(np.int32),
            "last_event_date": [date.fromordinal(int(x)) if x > 0 else None for x in last],
            "halflife_days": np.int16(halflife), "model_based": True, "computed_at": now},
            schema_overrides={"last_event_date": pl.Date, "halflife_days": pl.Int16}))
    if not rows:
        return pl.DataFrame()
    out = pl.concat(rows).with_columns(pl.col("value").fill_nan(None))
    return out


def run_surprise(settings: Settings | None = None, today: date | None = None, write: bool = True) -> dict[str, Any]:
    from app.core.storage import read_table, write_table  # noqa: PLC0415
    from app.macro.calendar import tracked_series  # noqa: PLC0415

    s = settings or get_settings()
    today = today or datetime.now(UTC).date()
    now = datetime.now(UTC)
    ev = read_table("macro_release_events", filters=pl.col("status") == "released", settings=s)
    ex = read_table("macro_expectations", filters=pl.col("is_primary"), settings=s)
    # one primary per (event, series): latest version wins
    ex = ex.sort("version", "forecast_made_at").unique(["release_event_id", "series_id"], keep="last")
    rel = read_table("macro_releases", columns=["release_event_id", "importance"], settings=s)
    direction = {t.series_id: t.direction for t in tracked_series(s)}
    sur = surprise_rows(ev, ex, rel, direction, now)
    rep: dict[str, Any] = {"surprises": sur.height, "with_z": sur.filter(pl.col("surprise_z").is_not_null()).height}
    if sur.is_empty():
        return rep
    start = min(sur["release_date"].min(), today)
    idx = build_index(sur, start, today, now)
    rep["index_rows"] = idx.height
    if write:
        rep["rows_surprises"] = write_table(sur, "macro_surprises", mode="overwrite", settings=s)
        rep["rows_index"] = write_table(idx, "macro_surprise_index", mode="overwrite", settings=s) if idx.height else 0
    latest = idx.filter((pl.col("date") == today) & (pl.col("theme") == "all")).select("country", "value", "n_events")
    rep["latest_all"] = {r["country"]: (round(r["value"], 1) if r["value"] is not None else None, r["n_events"])
                         for r in latest.iter_rows(named=True)}
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_surprise (nightly)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_surprise(ctx.settings)
    return JobResult(status="success", rows_written={"macro_surprises": rep.get("rows_surprises", 0),
                                                     "macro_surprise_index": rep.get("rows_index", 0)},
                     entities_ok=rep.get("surprises", 0), message=json.dumps(rep, default=str)[:4000])
