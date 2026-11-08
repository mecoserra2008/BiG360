"""Model-based expectations (W2.2). Jobs `macro_expectations` (nightly) and `macro_expectations_backfill` (manual).

MODEL-BASED EXPECTATION (NOT CONSENSUS). For each upcoming release (next 14 days) and each linked tracked series we
snapshot a forecast of the value about to be published, strictly before the release:
* sarima: SARIMA / ARIMA on the release measure (e.g. m/m % for US CPI) with a bounded AIC order search
  (p, q <= 2; P, Q <= 1 when the series is seasonal; d by ADF/KPSS, max 1), fitted with Model Studio's SARIMAX fitter
  (app.modeling.methods.timeseries._fit_sarimax) on the last <= 15 years. The selected order is re-searched at most once
  a year per series (the order is chosen with data available at that origin, so the backfill stays point-in-time).
* seasonal_naive / naive benchmark: the value one season earlier (seasonal series) or the last value; interval from
  the empirical spread of past same-horizon naive errors.
* dfm_nowcast (GDP): our DynamicFactorMQ nowcast (app.macro.nowcast) for the target quarter when one exists with
  as_of < release date; it becomes the primary expectation.
Point-in-time: the training data is the as-of series with knowledge_date < release date (ALFRED vintages for US vintage
series, realtime knowledge dates otherwise); forecast_made_at < scheduled_at always (live: now; backfill: the release
time - 12 h with data known through the previous day). Rows are immutable: an existing
(release_event_id, series_id, model, version) key is never rewritten.

Backfill is resumable: series are processed in chunks and written as they complete; already-stored keys are skipped;
progress is kept in data/state/macro_expectations_backfill.json.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import time
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.macro.calendar import (MODEL_LABEL, Tracked, apply_measure, asof_series, load_obs, next_period, season_len,
                                tracked_series)

log = get_logger(__name__)

VERSION = "v2"   # v2: data-driven primary (trailing out-of-sample RMSE, quarterly; default SARIMA first)
HORIZON_DAYS = 14
MAX_TRAIN = {"M": 180, "Q": 64, "W": 260}
MIN_TRAIN = {"M": 36, "Q": 20, "W": 104}
Z80, Z95 = 1.2815515655446004, 1.959963984540054
#: pandemic observations are masked (treated as missing) when fitting: they dominate variances and ARMA estimates
COVID = (date(2020, 3, 1), date(2021, 9, 30))
GDP_SERIES = {"fred:A191RL1Q225SBEA": "dfm_mq_us_v1", "ecb:MNA.Q.Y.I10.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.G1": "dfm_mq_ea_v1",
              "ons:IHYQ": "dfm_mq_gb_v1", "oecd:QNA.JPN.G1": "dfm_mq_jp_v1"}


# ------------------------------------------------------------------------------------------------ models


def _steps(last: date, target: date, freq: str) -> int:
    h, p = 0, last
    while p < target and h < 60:
        p = next_period(p, freq)
        h += 1
    return h if p == target else 0


def naive_forecast(y: pd.Series, h: int, seasonal: int) -> dict[str, Any] | None:
    """Seasonal-naive (seasonal > 0) or naive forecast h steps after the last observation, with an empirical
    interval from past h-step errors of the same rule."""
    v = y.dropna()
    if v.empty:
        return None
    arr = y.to_numpy(dtype=float)
    n = len(arr)
    if seasonal:
        k = seasonal * math.ceil(h / seasonal)        # same season, latest available
        if n - 1 + h - k < 0 or not np.isfinite(arr[n - 1 + h - k]):
            return None
        point = float(arr[n - 1 + h - k])
        err = arr[k:] - arr[:-k] if n > k else np.array([])
        spec, model = f"value {k} periods earlier (seasonal naive)", "seasonal_naive"
    else:
        point = float(v.iloc[-1])
        err = arr[h:] - arr[:-h] if n > h else np.array([])
        spec, model = "last value (naive)", "naive"
    err = err[np.isfinite(err)][-120:]
    sd = float(np.std(err, ddof=1)) if len(err) >= 6 else None
    return {"model": model, "model_spec": spec, "point": point, "sd": sd, "n_train": int(len(v)), "params": None}


def sarima_forecast(y: pd.Series, h: int, seasonal: int, order_hint: dict[str, Any] | None = None
                    ) -> dict[str, Any] | None:
    """Bounded AIC order search (or the hinted order) -> h-step forecast with 80/95% intervals."""
    from app.modeling import diagnostics as dg  # noqa: PLC0415
    from app.modeling.methods.timeseries import _fit_sarimax, _trend_for  # noqa: PLC0415

    yy = y.astype(float)
    first = yy.first_valid_index()
    yy = yy.loc[first:] if first is not None else yy
    arr = yy.to_numpy(dtype=float)          # interior NaNs (gaps, masked COVID window) are handled by the Kalman filter
    nfin = int(np.isfinite(arr).sum())
    if nfin < 24 or (seasonal and nfin < 3 * seasonal) or not np.isfinite(arr[-1]):
        return None
    s = seasonal or 0
    t0 = time.perf_counter()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if order_hint:
            d = order_hint["d"]
            grid = [tuple(order_hint["order"]) + tuple(order_hint["seasonal_order"])]
        else:
            d = min(1, dg.suggest_d(arr, max_d=1))
            grid = sorted(itertools.product(range(3), range(3), range(2) if s else [0], range(2) if s else [0]),
                          key=lambda g: (sum(g), g))[:16]
        trend = _trend_for(d, 0, None, False)
        best = None
        for g in grid:
            pp, qq, PP, QQ = g[0], g[1], g[2] if len(g) > 2 else 0, g[3] if len(g) > 3 else 0
            if len(g) == 7:  # hinted (p, d, q, P, D, Q, s)
                pp, qq, PP, QQ = g[0], g[2], g[3], g[5]
            o, so = (pp, d, qq), ((PP, 0, QQ, s) if s else (0, 0, 0, 0))
            try:
                r = _fit_sarimax(arr, None, o, so, trend, 100)
                aic = float(r.aic)
            except Exception:  # noqa: BLE001
                continue
            if np.isfinite(aic) and (best is None or aic < best[0]):
                best = (aic, o, so, r)
    if best is None:
        return None
    aic, o, so, r = best
    try:
        fc = r.get_forecast(h)
        mean = float(np.asarray(fc.predicted_mean)[-1])
        se = float(np.sqrt(np.asarray(fc.var_pred_mean)[-1]))
    except Exception:  # noqa: BLE001
        return None
    if not (np.isfinite(mean) and np.isfinite(se)):
        return None
    spec = (f"SARIMA{o}x{so}" if s else f"ARIMA{o}") + (f" trend={trend}" if trend != "n" else "")
    params = {"order": list(o), "seasonal_order": list(so), "d": d, "aic": round(aic, 3), "trend": trend,
              "coef": {k: round(float(v), 6) for k, v in zip(r.model.param_names, np.asarray(r.params), strict=False)},
              "searched": len(grid), "seconds": round(time.perf_counter() - t0, 3)}
    return {"model": "sarima", "model_spec": spec, "point": mean, "sd": se, "n_train": int(len(arr)), "params": params}


def forecast_event(y_measure: pd.Series, target: date, t: Tracked, order_hint: dict[str, Any] | None = None
                   ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Forecast rows (sarima + naive benchmark) for one event. y_measure: as-of measure series on the period grid."""
    y = y_measure.dropna()
    if y.empty:
        return [], order_hint
    last = y.index[-1]
    h = _steps(last, target, t.freq)
    if h <= 0:
        return [], order_hint
    grid = y_measure.loc[y_measure.first_valid_index():].copy()
    grid = grid.iloc[-MAX_TRAIN.get(t.freq, 180):]
    covid = [(i >= COVID[0]) and (i <= COVID[1]) for i in grid.index]
    if any(covid) and not covid[-1]:
        grid[covid] = np.nan                  # documented: 2020-03..2021-09 excluded from fitting
    out = []
    nv = naive_forecast(grid, h, t.seasonal)
    if nv:
        out.append(nv)
    if len(grid.dropna()) >= MIN_TRAIN.get(t.freq, 36):
        sm = sarima_forecast(grid, h, t.seasonal, order_hint)
        if sm is None and order_hint:  # hinted order failed -> fresh search
            sm = sarima_forecast(grid, h, t.seasonal, None)
        if sm:
            out.append(sm)
            if not order_hint or sm["params"]["order"] != order_hint.get("order"):
                order_hint = {"order": sm["params"]["order"], "seasonal_order": sm["params"]["seasonal_order"],
                              "d": sm["params"]["d"]}
    for o in out:
        o["h"] = h
    return out, order_hint


# ------------------------------------------------------------------------------------------------ rows


def _row(ev: dict[str, Any], t: Tracked, f: dict[str, Any], made_at: datetime, cutoff: date, backfill: bool,
         run_id: str | None, now: datetime) -> dict[str, Any]:
    sd = f.get("sd")
    p = f["point"]
    return {"release_event_id": ev["release_event_id"], "series_id": t.series_id, "country": t.country,
            "measure": t.measure, "target_period": ev["period"], "scheduled_at": ev["scheduled_at"],
            "forecast_made_at": made_at, "data_cutoff": cutoff, "point": float(p),
            "lo80": p - Z80 * sd if sd else None, "hi80": p + Z80 * sd if sd else None,
            "lo95": p - Z95 * sd if sd else None, "hi95": p + Z95 * sd if sd else None,
            "model": f["model"], "model_spec": f.get("model_spec"),
            "params": json.dumps({**(f.get("params") or {}), "h": f.get("h")}, default=str),
            "n_train": f.get("n_train"), "is_primary": False, "model_based": True, "label": MODEL_LABEL,
            "version": VERSION, "backfill": backfill, "computed_at": now, "run_id": run_id}


#: default preference when the data-driven rule has too little history (v2: SARIMA first, nowcast second)
DEFAULT_RANK = {"sarima": 0, "dfm_nowcast": 1, "seasonal_naive": 2, "naive": 2}
PRIMARY_WINDOW = 12      # trailing releases scored
PRIMARY_MIN = 4          # common scored releases needed


def _quarter_start(d: date) -> date:
    return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)


def select_primary(history: list[tuple[date, str, float, float]], rel_day: date, candidates: set[str]) -> tuple[str, dict]:
    """Data-driven primary model, point-in-time and re-evaluated quarterly: among the candidate models, the one with
    the lowest RMSE over the last PRIMARY_WINDOW releases dated before the start of the release's quarter on which
    every candidate had a forecast (>= PRIMARY_MIN such releases); else DEFAULT_RANK. history: (release_date, model,
    point, actual) of past releases of the same series (naive / seasonal_naive pooled as 'benchmark')."""
    norm = (lambda m: "benchmark" if m in ("naive", "seasonal_naive") else m)
    cands = {norm(c) for c in candidates}
    default = min(candidates, key=lambda m: DEFAULT_RANK.get(m, 9))
    cutoff = _quarter_start(rel_day)
    by_day: dict[date, dict[str, float]] = {}
    for d, m, p, a in history:
        if d < cutoff and a is not None and p is not None:
            by_day.setdefault(d, {})[norm(m)] = float(a) - float(p)
    common = sorted(d for d, e in by_day.items() if cands <= set(e))[-PRIMARY_WINDOW:]
    if len(cands) < 2 or len(common) < PRIMARY_MIN:
        return default, {"rule": "default_rank", "n": len(common)}
    rmse = {m: float(np.sqrt(np.mean([by_day[d][m] ** 2 for d in common]))) for m in cands}
    best = min(sorted(rmse), key=lambda m: (rmse[m], DEFAULT_RANK.get(m if m != "benchmark" else "naive", 9)))
    chosen = best if best != "benchmark" else next(c for c in candidates if norm(c) == "benchmark")
    return chosen, {"rule": "trailing_rmse", "n": len(common), "cutoff": cutoff.isoformat(),
                    "rmse": {k: round(v, 6) for k, v in rmse.items()}}


def _set_primary(rows: list[dict[str, Any]], history: list[tuple[date, str, float, float]] | None = None,
                 rel_day: date | None = None) -> None:
    by: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault((r["release_event_id"], r["series_id"]), []).append(r)
    for rs in by.values():
        if history is not None and rel_day is not None:
            m, info = select_primary(history, rel_day, {r["model"] for r in rs})
        else:
            m, info = min((r["model"] for r in rs), key=lambda x: DEFAULT_RANK.get(x, 9)), {"rule": "default_rank"}
        best = next(r for r in rs if r["model"] == m)
        best["is_primary"] = True
        p = json.loads(best["params"] or "{}")
        p["primary_selection"] = info
        best["params"] = json.dumps(p, default=str)


SCHEMA = {"release_event_id": pl.Utf8, "series_id": pl.Utf8, "country": pl.Utf8, "measure": pl.Utf8,
          "target_period": pl.Date, "scheduled_at": pl.Datetime("us", "UTC"), "forecast_made_at": pl.Datetime("us", "UTC"),
          "data_cutoff": pl.Date, "point": pl.Float64, "lo80": pl.Float64, "hi80": pl.Float64, "lo95": pl.Float64,
          "hi95": pl.Float64, "model": pl.Utf8, "model_spec": pl.Utf8, "params": pl.Utf8, "n_train": pl.Int32,
          "is_primary": pl.Boolean, "model_based": pl.Boolean, "label": pl.Utf8, "version": pl.Utf8,
          "backfill": pl.Boolean, "computed_at": pl.Datetime("us", "UTC"), "run_id": pl.Utf8}


def rows_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=SCHEMA, orient="row") if rows else pl.DataFrame(schema=SCHEMA)


def expectations_for_series(t: Tracked, obs: pd.DataFrame | None, events: list[dict[str, Any]], backfill: bool,
                            now: datetime, run_id: str | None = None,
                            nowcasts: pd.DataFrame | None = None,
                            history: list[tuple[date, str, float, float]] | None = None) -> list[dict[str, Any]]:
    """All expectation rows of one series for the given events (dicts with release_event_id, period, release_date,
    scheduled_at). Live: data known through today (strictly before the release date). Backfill: data known before
    the release date, forecast_made_at = scheduled_at - 12 h."""
    if obs is None or obs.empty:
        return []
    rows: list[dict[str, Any]] = []
    hist = list(history or [])      # (release_date, model, point, actual) of earlier releases (primary selection)
    hint: dict[str, Any] | None = None
    hint_at: date | None = None
    for ev in sorted(events, key=lambda e: e["release_date"]):
        rel_day: date = ev["release_date"]
        sched: datetime = ev["scheduled_at"]
        if backfill:
            cutoff_excl = rel_day
            made_at = sched - timedelta(hours=12)
        else:
            cutoff_excl = min(rel_day, now.date() + timedelta(days=1))
            made_at = now
        if made_at >= sched or made_at > now or ev.get("period") is None:   # never a snapshot dated in the future
            continue
        raw = asof_series(obs, cutoff_excl, t.freq, strict=True)
        if raw.dropna().empty:
            continue
        y = apply_measure(raw, t.measure, t.freq)
        if hint_at is None or (rel_day - hint_at).days > 365:
            hint, hint_at = None, rel_day
        fs, hint = forecast_event(y, ev["period"], t, hint)
        known = obs[obs["knowledge_date"] < cutoff_excl]["knowledge_date"]
        cutoff = max(known) if len(known) else cutoff_excl - timedelta(days=1)
        rs = [_row(ev, t, f, made_at, cutoff, backfill, run_id, now) for f in fs]
        # GDP: our DFM nowcast for the target quarter, known before the release
        mid = GDP_SERIES.get(t.series_id)
        if mid and nowcasts is not None and not nowcasts.empty:
            nc = nowcasts[(nowcasts["model_id"] == mid) & (nowcasts["target_period"] == ev["period"])
                          & (nowcasts["as_of"] < rel_day)]
            if not nc.empty:
                n = nc.sort_values("as_of").iloc[-1]
                sd = (float(n["hi"]) - float(n["lo"])) / (2 * Z80) if pd.notna(n["hi"]) and pd.notna(n["lo"]) else None
                rs.append(_row(ev, t, {"model": "dfm_nowcast", "model_spec": f"{mid} as of {n['as_of']}",
                                       "point": float(n["nowcast"]), "sd": sd, "n_train": None,
                                       "params": {"nowcast_as_of": str(n["as_of"]), "model_id": mid}, "h": 0},
                               made_at, min(cutoff, n["as_of"]), backfill, run_id, now))
        _set_primary(rs, hist, rel_day)
        rows += rs
        if ev.get("actual") is not None:   # known only after this release; selection uses quarters before
            hist += [(rel_day, r["model"], r["point"], float(ev["actual"])) for r in rs]
    return rows


# ------------------------------------------------------------------------------------------------ inputs


def _events(settings: Settings, status: tuple[str, ...], start: date | None = None, end: date | None = None,
            after: datetime | None = None, before: datetime | None = None) -> pl.DataFrame:
    from app.core.storage import read_table  # noqa: PLC0415

    ev = read_table("macro_release_events", columns=["release_event_id", "series_id", "release_date", "scheduled_at",
                                                     "period", "status", "actual"], settings=settings)
    f = pl.col("status").is_in(list(status)) & pl.col("period").is_not_null()
    if start:
        f &= pl.col("release_date") >= start
    if end:
        f &= pl.col("release_date") <= end
    if after:
        f &= pl.col("scheduled_at") > after
    if before:
        f &= pl.col("scheduled_at") <= before
    return ev.filter(f)


def _existing_keys(settings: Settings) -> set[tuple[str, str, str, str]]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        ex = read_table("macro_expectations", columns=["release_event_id", "series_id", "model", "version"],
                        settings=settings)
    except Exception:  # noqa: BLE001
        return set()
    return set(ex.iter_rows())


def _nowcasts(settings: Settings) -> pd.DataFrame:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        nc = read_table("macro_nowcasts", columns=["model_id", "target_period", "as_of", "nowcast", "lo", "hi"],
                        filters=pl.col("model_id").str.starts_with("dfm_mq"), settings=settings)
        if not nc.height:
            return pd.DataFrame()
        p = nc.to_pandas()
        for c in ("target_period", "as_of"):
            p[c] = pd.to_datetime(p[c]).dt.date
        return p
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def write_new(rows: list[dict[str, Any]], settings: Settings, existing: set | None = None) -> int:
    """Append-only write (immutability): drop keys already stored."""
    from app.core.storage import write_table  # noqa: PLC0415

    if not rows:
        return 0
    existing = existing if existing is not None else _existing_keys(settings)
    rows = [r for r in rows if (r["release_event_id"], r["series_id"], r["model"], r["version"]) not in existing]
    if not rows:
        return 0
    n = write_table(rows_frame(rows), "macro_expectations", mode="upsert", settings=settings)
    existing.update((r["release_event_id"], r["series_id"], r["model"], r["version"]) for r in rows)
    return n


# ------------------------------------------------------------------------------------------------ live job


def run_live(settings: Settings | None = None, now: datetime | None = None, horizon_days: int = HORIZON_DAYS,
             run_id: str | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    ev = _events(s, ("pending",), after=now, before=now + timedelta(days=horizon_days))
    tracked = {t.series_id: t for t in tracked_series(s)}
    ev = ev.filter(pl.col("series_id").is_in(list(tracked)))
    obs = load_obs(ev["series_id"].unique().to_list(), s)
    nc = _nowcasts(s)
    rows: list[dict[str, Any]] = []
    hist = _history(s, ev["series_id"].unique().to_list())
    for (sid,), g in ev.group_by(["series_id"]):
        rows += expectations_for_series(tracked[sid], obs.get(sid), g.to_dicts(), False, now, run_id, nc,
                                        hist.get(sid, []))
    n = write_new(rows, s)
    return {"events": ev.height, "rows": len(rows), "rows_written": n, "series": ev["series_id"].n_unique()}


def _history(s: Settings, series: list[str]) -> dict[str, list[tuple[date, str, float, float]]]:
    """Stored expectations (current version) of released events with their actuals, per series."""
    from app.core.storage import read_table  # noqa: PLC0415

    if not series:
        return {}
    try:
        x = read_table("macro_expectations", columns=["release_event_id", "series_id", "model", "point", "version"],
                       filters=(pl.col("version") == VERSION) & pl.col("series_id").is_in(series), settings=s)
        e = read_table("macro_release_events", columns=["release_event_id", "series_id", "release_date", "actual"],
                       filters=pl.col("actual").is_not_null() & pl.col("series_id").is_in(series), settings=s)
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, list[tuple[date, str, float, float]]] = {}
    for r in x.join(e, on=["release_event_id", "series_id"]).iter_rows(named=True):
        out.setdefault(r["series_id"], []).append((r["release_date"], r["model"], r["point"], r["actual"]))
    return out


# ------------------------------------------------------------------------------------------------ backfill


def _bf_worker(args: tuple[Tracked, pd.DataFrame | None, list[dict[str, Any]], str | None, pd.DataFrame]
               ) -> tuple[str, list[dict[str, Any]], float]:
    t, obs, events, run_id, nc = args
    t0 = time.perf_counter()
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    rows = expectations_for_series(t, obs, events, True, datetime.now(UTC), run_id, nc)
    return t.series_id, rows, time.perf_counter() - t0


def _state_path(s: Settings):
    return s.data_dir / "state" / "macro_expectations_backfill.json"


def run_backfill(settings: Settings | None = None, workers: int | None = None, series: list[str] | None = None,
                 run_id: str | None = None, cancel: Any = None) -> dict[str, Any]:
    """Point-in-time expectations for every past release event (resumable)."""
    s = settings or get_settings()
    now = datetime.now(UTC)
    tracked = {t.series_id: t for t in tracked_series(s)}
    ev = _events(s, ("released", "not_published"), before=now)
    ev = ev.filter(pl.col("series_id").is_in(list(series or tracked)))
    existing = _existing_keys(s)
    done_pairs = {(a, b) for a, b, _, v in existing if v == VERSION}
    ev = ev.filter(~pl.struct("release_event_id", "series_id").map_elements(
        lambda r: (r["release_event_id"], r["series_id"]) in done_pairs, return_dtype=pl.Boolean))
    todo = ev["series_id"].unique().sort().to_list()
    st_path = _state_path(s)
    st_path.parent.mkdir(parents=True, exist_ok=True)
    state = {"started_at": now.isoformat(), "version": VERSION, "series_total": len(todo), "series_done": [],
             "rows_written": 0, "status": "running"}
    st_path.write_text(json.dumps(state, indent=1))
    if not todo:
        state["status"] = "complete"
        st_path.write_text(json.dumps(state, indent=1))
        return {"events": 0, "rows_written": 0, "series": 0}
    obs = load_obs(todo, s)
    nc = _nowcasts(s)
    workers = workers or max(1, min(16, (os.cpu_count() or 4) - 4))
    groups = {sid: g.to_dicts() for (sid,), g in ev.group_by(["series_id"])}
    tasks = [(tracked[sid], obs.get(sid), groups[sid], run_id, nc) for sid in todo]
    t0 = time.perf_counter()
    buf: list[dict[str, Any]] = []
    total = 0
    timings: dict[str, float] = {}

    def flush() -> None:
        nonlocal buf, total
        total += write_new(buf, s, existing)
        buf = []
        state.update(rows_written=total, elapsed_s=round(time.perf_counter() - t0, 1))
        st_path.write_text(json.dumps(state, indent=1))

    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_bf_worker, a) for a in tasks]
        for f in as_completed(futs):
            try:
                sid, rows, sec = f.result()
            except Exception as e:  # noqa: BLE001
                log.warning("expectations backfill worker failed: %s", e)
                continue
            buf += rows
            timings[sid] = round(sec, 1)
            state["series_done"].append(sid)
            if len(buf) >= 4000:
                flush()
            if cancel is not None and cancel():
                for x in futs:
                    x.cancel()
                break
    flush()
    state["status"] = "complete" if len(state["series_done"]) == len(todo) else "partial"
    state["timings_s"] = timings
    st_path.write_text(json.dumps(state, indent=1))
    return {"events": ev.height, "series": len(todo), "rows_written": total,
            "seconds": round(time.perf_counter() - t0, 1), "status": state["status"]}


# ------------------------------------------------------------------------------------------------ accuracy


def accuracy(settings: Settings | None = None) -> pl.DataFrame:
    """RMSE of each model vs actual per series (backfill + live), for the report."""
    from app.core.storage import read_table  # noqa: PLC0415

    s = settings or get_settings()
    ex = read_table("macro_expectations", columns=["release_event_id", "series_id", "model", "point", "is_primary"],
                    settings=s)
    ev = read_table("macro_release_events", columns=["release_event_id", "series_id", "actual"], settings=s
                    ).filter(pl.col("actual").is_not_null())
    j = ex.join(ev, on=["release_event_id", "series_id"])
    j = j.with_columns(pl.when(pl.col("model").is_in(["naive", "seasonal_naive"])).then(pl.lit("benchmark"))
                       .otherwise(pl.col("model")).alias("model"))
    return (j.group_by("series_id", "model").agg(((pl.col("actual") - pl.col("point")) ** 2).mean().sqrt().alias("rmse"),
                                                pl.len().alias("n"))
            .pivot(on="model", index="series_id", values=["rmse", "n"]).sort("series_id"))


# ------------------------------------------------------------------------------------------------ jobs


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_expectations (nightly): snapshot model-based expectations for releases in the next 14 days."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_live(ctx.settings, horizon_days=int(p.get("horizon_days", HORIZON_DAYS)), run_id=ctx.run_id)
    return JobResult(status="success", rows_written={"macro_expectations": rep["rows_written"]},
                     entities_ok=rep["series"], message=json.dumps(rep, default=str))


def run_backfill_job(ctx):  # noqa: ANN001 - JobContext
    """Job macro_expectations_backfill (manual, resumable). params: workers, series (list)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    series = p.get("series")
    if isinstance(series, str):
        series = [x for x in series.split(",") if x]
    rep = run_backfill(ctx.settings, workers=int(p["workers"]) if p.get("workers") else None, series=series,
                       run_id=ctx.run_id, cancel=ctx.cancel_event.is_set)
    return JobResult(status="success" if rep.get("status", "complete") == "complete" else "partial",
                     rows_written={"macro_expectations": rep["rows_written"]}, entities_ok=rep["series"],
                     message=json.dumps(rep, default=str))
