"""Job `earnings_expectations`: MODEL-BASED EXPECTATIONS (NOT CONSENSUS) for quarterly / half-yearly revenue and
diluted EPS, and the surprise when the actual lands.

Data: fundamentals_canonical discrete periods (period_kind Q, else H for half-yearly reporters), items revenue and
eps_diluted (eps_basic when the issuer has no diluted series: eps_basis). Point in time: for a target period with
release date R the model is fitted at origin O = R - 1 day on the as-of values of earlier periods (row with the
latest knowledge_date <= O per period); EPS values are put on the share basis at O with corporate_actions splits
(ex date in (knowledge_date, O]); Q4 = FY - 9M rows (derivation derived_period) are used as-is and flagged.
Upcoming periods (earnings_calendar, releases without an actual yet) use O = run date.

Models (series = EPS level, revenue in logs when all positive)
* sarima: Foster (1977) seasonal model SARIMA(1,0,0)x(0,1,0)s with drift (the classic time-series earnings
  expectation of the SUE / PEAD literature), estimated by conditional least squares on the seasonal differences
  (fast enough to refit at every historical origin); 80 / 95% intervals from the h-step forecast variance (log
  revenue intervals are exponentiated). Needs >= 2 seasons + 4 observations.
* seasonal_naive: same period a year earlier (EPS: + mean seasonal change of the last 8 periods; revenue: x trailing
  seasonal growth); sigma from the issuer's past errors of the same rule (>= 6).
* forecast_path (upcoming periods only): the analytics forecast (forecasts, scenario base, bear / bull for the 80%
  band) for the target fiscal year x the period's average share of the fiscal year over the last 3 years.
primary: the model with the lower mean absolute % error over the issuer's previous <= 8 released targets known at
O (>= 4 needed), else sarima, else seasonal_naive.
params: mode (nightly | backfill; nightly = targets released in the last `recent_days` (120) + pending + calendar),
since (backfill first release date, default 2016-01-01), workers (default 6), max_minutes (default 60).
"""

from __future__ import annotations

import math
import os
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.storage import scan_table
from app.earnings import common as C

log = C.log
Z80, Z95 = 1.2815516, 1.959964
ITEMS = ("revenue", "eps_diluted")


# ------------------------------------------------------------------------------------------------ models


def foster_cls(y: np.ndarray, h: int, s: int) -> tuple[float, float, int] | None:
    """Foster (1977) SARIMA(1,0,0)x(0,1,0)s with drift by conditional least squares: z_t = y_t - y_{t-s},
    z_t = c + phi z_{t-1} + e_t. Returns (forecast of y_{T+h}, its standard error, n pairs); h <= s."""
    n = len(y)
    z = np.full(n, np.nan)
    z[s:] = y[s:] - y[:-s]
    x0, x1 = z[s:-1], z[s + 1:]
    ok = np.isfinite(x0) & np.isfinite(x1)
    if ok.sum() < max(6, s + 2) or h > s:
        return None
    a, b = x0[ok], x1[ok]
    X = np.column_stack([np.ones(len(a)), a])
    coef, *_ = np.linalg.lstsq(X, b, rcond=None)
    c, phi = float(coef[0]), float(np.clip(coef[1], -0.95, 0.95))
    e = b - (c + phi * a)
    sig = float(np.sqrt(e @ e / max(len(b) - 2, 1)))
    zl = z[-1] if np.isfinite(z[-1]) else (np.nanmean(z[-s:]) if np.isfinite(z[-s:]).any() else None)
    if zl is None or not np.isfinite(y[n - 1 + h - s]):
        return None
    zf, var = float(zl), 0.0
    for k in range(h):
        zf = c + phi * zf
        var += sig ** 2 * phi ** (2 * k)
    return float(y[n - 1 + h - s] + zf), float(np.sqrt(var)), int(ok.sum())


def sarima_forecast(y: np.ndarray, h: int, s: int, kind: str) -> dict[str, Any] | None:
    """y: regular series (NaN gaps allowed), forecast h steps ahead. kind: 'log' (revenue) | 'level' (EPS)."""
    nfin = int(np.isfinite(y).sum())
    if nfin < 2 * s + 4 or not np.isfinite(y[-1]):
        return None
    f = foster_cls(y, h, s)
    if f is None:
        return None
    mu, se, _npairs = f
    name = f"SARIMA(1,0,0)x(0,1,0,{s}) with drift (Foster), CLS"
    if not (np.isfinite(mu) and np.isfinite(se)) or se <= 0:
        return None
    if kind == "log":
        if abs(mu) > 50 or se > 3:
            return None
        pt = math.exp(mu)
        return {"model": "sarima", "spec": name + " on log revenue", "point": pt,
                "lo80": math.exp(mu - Z80 * se), "hi80": math.exp(mu + Z80 * se),
                "lo95": math.exp(mu - Z95 * se), "hi95": math.exp(mu + Z95 * se), "sigma": pt * se, "n": nfin}
    return {"model": "sarima", "spec": name + " on EPS", "point": mu, "lo80": mu - Z80 * se, "hi80": mu + Z80 * se,
            "lo95": mu - Z95 * se, "hi95": mu + Z95 * se, "sigma": se, "n": nfin}


def naive_forecast(y: np.ndarray, h: int, s: int, kind: str) -> dict[str, Any] | None:
    """Seasonal naive with drift (level) / trailing seasonal growth (log); sigma from past errors of the rule."""
    n = len(y)
    j = n - 1 + h - s * math.ceil(h / s)
    if j < 0 or not np.isfinite(y[j]):
        return None
    d = y[s:] - y[:-s]
    d = d[np.isfinite(d)]
    drift = float(np.mean(d[-8:])) if len(d) >= 2 else 0.0
    mu = float(y[j] + drift)
    # past errors of the same rule (one-season-ahead with the trailing drift known then)
    errs = []
    for t in range(2 * s, n):
        if np.isfinite(y[t]) and np.isfinite(y[t - s]):
            dd = y[s:t] - y[:t - s]
            dd = dd[np.isfinite(dd)]
            if len(dd) >= 2:
                errs.append(y[t] - (y[t - s] + float(np.mean(dd[-8:]))))
    sd = float(np.std(errs[-12:], ddof=1)) if len(errs) >= 6 else None
    if kind == "log":
        pt = math.exp(mu)
        out = {"model": "seasonal_naive", "spec": "same period last year x trailing seasonal growth", "point": pt,
               "n": int(np.isfinite(y).sum())}
        if sd:
            out |= {"lo80": math.exp(mu - Z80 * sd), "hi80": math.exp(mu + Z80 * sd), "lo95": math.exp(mu - Z95 * sd),
                    "hi95": math.exp(mu + Z95 * sd), "sigma": pt * sd}
        return out
    out = {"model": "seasonal_naive", "spec": "same period last year + mean seasonal change (8 periods)", "point": mu,
           "n": int(np.isfinite(y).sum())}
    if sd:
        out |= {"lo80": mu - Z80 * sd, "hi80": mu + Z80 * sd, "lo95": mu - Z95 * sd, "hi95": mu + Z95 * sd, "sigma": sd}
    return out


# ------------------------------------------------------------------------------------------------ series


def regular_series(rows: list[tuple[date, float]], target: date, months: int) -> tuple[np.ndarray, int] | None:
    """Values on a regular grid of `months` ending at the last observation; h = steps from it to target."""
    if not rows:
        return None
    rows = sorted(rows)
    step = 30.44 * months
    last = rows[-1][0]
    h = round((target - last).days / step)
    if h < 1 or h > 3:
        return None
    k = [round((last - d).days / step) for d, _ in rows]
    n = max(k) + 1
    y = np.full(n, np.nan)
    for (_, v), kk in zip(rows, k, strict=True):
        y[n - 1 - kk] = v
    return y, h


def clean_series(y: np.ndarray, s: int) -> np.ndarray | None:
    """Data-quality guard: needs >= s observations in the last 2 seasons (no stale / gappy series) and masks robust
    outliers (|x - median| > 8 x 1.4826 MAD over the last 4 seasons), e.g. pre-split values or broken Q4 = FY - 9M
    derivations. Returns None when the series is unusable."""
    y = y.copy()
    if np.isfinite(y[-2 * s:]).sum() < s:
        return None
    w = y[-4 * s:]
    w = w[np.isfinite(w)]
    med = float(np.median(w))
    mad = float(np.median(np.abs(w - med))) * 1.4826
    if mad > 0:
        bad = np.isfinite(y) & (np.abs(y - med) > 8 * mad)
        y[bad] = np.nan
    if not np.isfinite(y[-1]):
        return None
    return y


def asof_values(vers: list[tuple[date, date, float, str]], origin: date, before: date,
                splits: list[tuple[date, float]], eps: bool) -> list[tuple[date, float]]:
    """vers: [(period_end, knowledge_date, value, derivation)] -> as-of value per period_end < before."""
    best: dict[date, tuple[date, float]] = {}
    for pe, kd, v, _ in vers:
        if pe >= before or kd > origin:
            continue
        if pe not in best or kd > best[pe][0]:
            best[pe] = (kd, v)
    out = []
    for pe, (kd, v) in best.items():
        if eps and splits:
            f = 1.0
            for ex, ratio in splits:
                if kd < ex <= origin and ratio and ratio > 0:
                    f *= ratio
            v = v / f
        out.append((pe, v))
    return out


def first_actual(vers: list[tuple[date, date, float, str]], pe: date) -> tuple[float, date, bool] | None:
    xs = [(kd, v, der) for p, kd, v, der in vers if abs((p - pe).days) <= 7]
    if not xs:
        return None
    kd, v, der = min(xs)
    return v, kd, der == "derived_period"


SPEC_RW = "same period a year earlier (seasonal random walk, no estimation)"
SPEC_RW_COMPARATIVE = ("same period a year earlier (seasonal random walk) - prior-year comparative as reported in this "
                       "release; approximates the figure public a year earlier (restatements possible)")


def seasonal_rw(vers: list[tuple[date, date, float, str]], vals: list[tuple[date, float]], pe: date, origin: date,
                act: tuple[float, date, bool] | None, splits: list[tuple[date, float]], eps: bool) -> dict[str, Any] | None:
    """Seasonal random walk E[y_t] = y_{t-1 year} (Foster 1977 / Ball & Watts benchmark) for issuers whose history
    is too short for the estimated models. Point in time: the year-ago value as known at the origin; for a released
    target without one, the prior-year comparative published IN the release (TDnet tanshin / interim reports carry
    it) - flagged in model_spec (an approximation of the value public a year earlier). No interval (sigma unknown)."""
    lo, hi = pe - timedelta(days=375), pe - timedelta(days=355)
    prev = [(d, v) for d, v in vals if lo <= d <= hi]
    if prev:
        return {"model": "seasonal_rw", "model_spec": SPEC_RW, "expectation": max(prev)[1], "lo80": None, "hi80": None,
                "lo95": None, "hi95": None, "sigma": None, "n_train": 1}
    if act is None:
        return None
    # comparative: a year-ago row first known with (or before) the actual, after the origin
    cands = [(kd, v) for p, kd, v, _ in vers if lo <= p <= hi and origin < kd <= act[1]]
    if not cands:
        return None
    kd, v = min(cands)
    if eps and splits:
        f = 1.0
        for ex, ratio in splits:
            if kd < ex <= act[1] and ratio and ratio > 0:
                f *= ratio
        v = v / f
    return {"model": "seasonal_rw", "model_spec": SPEC_RW_COMPARATIVE, "expectation": v, "lo80": None, "hi80": None,
            "lo95": None, "hi95": None, "sigma": None, "n_train": 1}


def issuer_targets(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Worker: all expectation rows of one issuer. task = {issuer_id, months, series: {item: vers}, eps_basis,
    splits, targets: [{period_end, fiscal_period, fiscal_year, release_date, origin}], currency, fpath}."""
    out = []
    months = task["months"]
    s = 4 if months == 3 else 2
    for item, vers in task["series"].items():
        eps = item == "eps_diluted"
        for t in task["targets"]:
            pe, origin = t["period_end"], t["origin"]
            vals = asof_values(vers, origin, pe - timedelta(days=20), task["splits"] if eps else [], eps)
            act = first_actual(vers, pe)
            if act is not None and act[1] <= origin:
                act = None  # actual already public at the origin (inconsistent dates): not a forecast target
            fpd = t.get("fiscal_period")
            if fpd == "FY":
                fpd = "Q4" if months == 3 else "H2"
            elif fpd not in ("Q1", "Q2", "Q3", "Q4", "H1", "H2"):
                fpd = None
            base = {"issuer_id": task["issuer_id"], "item_id": item, "period_end": pe,
                    "fiscal_period": fpd, "fiscal_year": t.get("fiscal_year"),
                    "currency": task.get("currency"),
                    "eps_basis": task.get("eps_basis") if eps else None, "origin_date": origin,
                    "release_date": t.get("release_date")}
            if act is not None:
                base |= {"actual": act[0], "actual_knowledge_date": act[1], "derived_q4": act[2]}
            if len(vals) < 2 * s + 2:
                # short history (TDnet tanshin since 2026-09, new listings, EU / UK half-yearly reporters):
                # the seasonal random walk is the only model that needs no estimation (W1 2026-10-31)
                rw = seasonal_rw(vers, vals, pe, origin, act, task["splits"] if eps else [], eps)
                if rw is not None:
                    out.append(base | rw)
                continue
            kind = "level"
            if not eps:
                if any(v <= 0 for _, v in vals):
                    continue
                kind = "log"
                vals = [(d, math.log(v)) for d, v in vals]
            rs = regular_series(vals, pe, months)
            if rs is None:
                continue
            y, h = rs
            y = clean_series(y, s)
            if y is None:
                continue
            scale = float(np.nanmax(np.abs(np.exp(y[-2 * s:]) if kind == "log" else y[-2 * s:])))
            for f in (sarima_forecast(y, h, s, kind), naive_forecast(y, h, s, kind)):
                if f is not None and abs(f["point"]) <= 5 * scale:  # implausible extrapolation of a noisy series
                    out.append(base | {"model": f["model"], "model_spec": f["spec"], "expectation": f["point"],
                                       "lo80": f.get("lo80"), "hi80": f.get("hi80"), "lo95": f.get("lo95"),
                                       "hi95": f.get("hi95"), "sigma": f.get("sigma"), "n_train": f.get("n")})
            fp = (task.get("fpath") or {}).get((item, pe))
            if fp is not None and act is None:
                out.append(base | fp)
    return out


def _chunk_worker(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    rows = []
    for t in tasks:
        try:
            rows += issuer_targets(t)
        except Exception as e:  # noqa: BLE001 - one issuer must not stop the chunk
            log.warning("expectations %s failed: %s", t.get("issuer_id"), e)
    return rows


# ------------------------------------------------------------------------------------------------ primary + surprise


def finish(df: pl.DataFrame, prior: pl.DataFrame | None = None) -> pl.DataFrame:
    """Surprises, status, primary model (trailing MAPE over released targets known at the origin). `prior`: earlier
    released rows of the same issuers already in the table (nightly runs compute only recent targets)."""
    if df.is_empty():
        return df
    for c, dt in (("actual", pl.Float64), ("actual_knowledge_date", pl.Date), ("derived_q4", pl.Boolean),
                  ("sigma", pl.Float64)):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, dtype=dt).alias(c))
    df = df.with_columns(
        (pl.col("actual") - pl.col("expectation")).alias("surprise"),
        pl.when(pl.col("expectation").abs() > 1e-9).then((pl.col("actual") - pl.col("expectation")) / pl.col("expectation").abs())
        .otherwise(None).alias("surprise_pct"),
        pl.when(pl.col("sigma") > 0).then((pl.col("actual") - pl.col("expectation")) / pl.col("sigma")).otherwise(None)
        .alias("surprise_z"),
        pl.when(pl.col("actual").is_not_null()).then(pl.lit("released")).otherwise(pl.lit("pending")).alias("status"),
    )
    rows = df.sort("issuer_id", "item_id", "period_end").to_dicts()
    hist: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_key: dict[tuple[str, str, date], list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_key[(r["issuer_id"], r["item_id"], r["period_end"])].append(r)
        if r["actual"] is not None and r["expectation"] is not None:
            hist[(r["issuer_id"], r["item_id"])].append(r)
    if prior is not None and prior.height:
        have = set(by_key)
        for r in prior.iter_rows(named=True):
            if (r["issuer_id"], r["item_id"], r["period_end"]) not in have and r["actual"] is not None:
                hist[(r["issuer_id"], r["item_id"])].append(r)
    for (iid, item, _pe), grp in by_key.items():
        origin = grp[0]["origin_date"]
        models = {g["model"] for g in grp if g["expectation"] is not None}
        past = [h for h in hist[(iid, item)] if h["actual_knowledge_date"] is not None and h["actual_knowledge_date"] <= origin
                and h["model"] in ("sarima", "seasonal_naive")]
        err: dict[str, list[float]] = defaultdict(list)
        for h in sorted(past, key=lambda x: x["period_end"]):
            if abs(h["actual"]) > 1e-9:
                err[h["model"]].append(abs(h["actual"] - h["expectation"]) / abs(h["actual"]))
        pick = None
        e1, e2 = err.get("sarima", [])[-8:], err.get("seasonal_naive", [])[-8:]
        if len(e1) >= 4 and len(e2) >= 4 and "sarima" in models and "seasonal_naive" in models:
            pick = "sarima" if np.mean(e1) <= np.mean(e2) else "seasonal_naive"
        if pick is None:
            pick = next((m for m in ("sarima", "seasonal_naive", "forecast_path", "seasonal_rw") if m in models), None)
        for g in grp:
            g["is_primary"] = g["model"] == pick
    out = pl.DataFrame(rows, infer_schema_length=None)
    return out.with_columns(pl.lit(C.LABEL_EXPECTATION).alias("label"))


# ------------------------------------------------------------------------------------------------ inputs


def load_fundamentals(s: Settings, issuers: set[str] | None = None) -> pl.DataFrame:
    lf = (scan_table("fundamentals_canonical", s)
          .filter(pl.col("item_id").is_in(["revenue", "eps_diluted", "eps_basic"]) & pl.col("period_kind").is_in(["Q", "H"]))
          .select("issuer_id", "item_id", "period_kind", "period_end", "fiscal_period", "fiscal_year", "value", "currency",
                  "knowledge_date", "derivation"))
    if issuers is not None:
        lf = lf.filter(pl.col("issuer_id").is_in(list(issuers)))
    return lf.collect()


def forecast_paths(s: Settings, fund: pl.DataFrame, targets: dict[str, list[dict[str, Any]]]) -> dict[str, dict]:
    """issuer -> {(item, period_end): row fields} for upcoming targets with a known fiscal year/period."""
    try:
        fc = (scan_table("forecasts", s).filter(pl.col("item_id").is_in(["revenue", "eps_diluted"]) & ~pl.col("is_actual"))
              .filter(pl.col("as_of_date") == pl.col("as_of_date").max())
              .select("issuer_id", "scenario", "fiscal_year", "item_id", "value").collect())
    except Exception:  # noqa: BLE001
        return {}
    if fc.is_empty():
        return {}
    fcd: dict[tuple[str, str, int], dict[str, float]] = defaultdict(dict)
    for i, sc, fy, it, v in fc.iter_rows():
        fcd[(i, it, int(fy))][sc] = v
    # seasonal shares: discrete period value / sum of the fiscal year's periods (last 3 complete fiscal years)
    q = fund.filter(pl.col("period_kind") == "Q").sort("knowledge_date").unique(
        ["issuer_id", "item_id", "period_end"], keep="first")
    share: dict[tuple[str, str, str], float] = {}
    for (iid, it), g in q.group_by("issuer_id", "item_id"):
        fy_tot = g.group_by("fiscal_year").agg(pl.col("value").sum().alias("tot"), pl.len().alias("n")).filter(pl.col("n") == 4)
        gg = g.join(fy_tot, on="fiscal_year").filter(pl.col("tot").abs() > 0).sort("fiscal_year")
        fys = gg.get_column("fiscal_year").unique().sort().to_list()[-3:]
        gg = gg.filter(pl.col("fiscal_year").is_in(fys))
        for fp, v in gg.group_by("fiscal_period").agg((pl.col("value") / pl.col("tot")).mean()).iter_rows():
            share[(iid, it, fp)] = float(v)
    out: dict[str, dict] = defaultdict(dict)
    for iid, ts in targets.items():
        for t in ts:
            if t.get("release_date") and t["release_date"] < C.now_utc().date() - timedelta(days=3):
                continue
            fy, fp = t.get("fiscal_year"), t.get("fiscal_period")
            if not fy or fp not in ("Q1", "Q2", "Q3", "Q4"):
                continue
            for it in ITEMS:
                sh = share.get((iid, it, fp))
                f = fcd.get((iid, it, int(fy)))
                if sh is None or not f or "base" not in f:
                    continue
                vals = {k: f[k] * sh for k in f}
                lo, hi = sorted([vals.get("bear", vals["base"]), vals.get("bull", vals["base"])])
                out[iid][(it, t["period_end"])] = {
                    "model": "forecast_path", "model_spec": f"analytics forecast FY{fy} (base; bear/bull band) x {fp} share",
                    "expectation": vals["base"], "lo80": lo, "hi80": hi, "lo95": None, "hi95": None,
                    "sigma": (hi - lo) / (2 * Z80) if hi > lo else None, "n_train": None}
    return out


def build_tasks(s: Settings, mode: str, since: date, today: date, recent_days: int,
                only: set[str] | None = None) -> list[dict[str, Any]]:
    ev = scan_table("earnings_events", s).select("issuer_id", "security_id", "period_end", "fiscal_period", "fiscal_year",
                                                 "release_date").collect()
    if only:
        ev = ev.filter(pl.col("issuer_id").is_in(list(only)))
    try:
        cal = scan_table("earnings_calendar", s).select("issuer_id", "security_id", "period_end", "fiscal_period",
                                                        "expected_date").collect()
    except Exception:  # noqa: BLE001
        cal = pl.DataFrame()
    if mode == "backfill":
        ev = ev.filter(pl.col("release_date") >= since)
    else:
        ev = ev.filter(pl.col("release_date") >= today - timedelta(days=recent_days))
    issuers = set(ev.get_column("issuer_id").to_list()) | (set(cal.get_column("issuer_id").to_list()) if cal.height else set())
    fund = load_fundamentals(s, issuers)
    if fund.is_empty():
        return []
    sec_of = dict(scan_table("earnings_events", s).select("issuer_id", "security_id").drop_nulls().unique("issuer_id")
                  .collect().iter_rows())
    if only and cal.height:
        cal = cal.filter(pl.col("issuer_id").is_in(list(only)))
    if cal.height:
        sec_of |= {i: x for i, x in cal.select("issuer_id", "security_id").drop_nulls().iter_rows() if i not in sec_of}
    sp = (scan_table("corporate_actions", s).filter(pl.col("action_type") == "split")
          .select("security_id", "ex_date", "ratio").collect())
    splits: dict[str, list[tuple[date, float]]] = defaultdict(list)
    for sid, ex, ratio in sp.iter_rows():
        splits[sid].append((ex, ratio))
    targets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in ev.iter_rows(named=True):
        targets[r["issuer_id"]].append({"period_end": r["period_end"], "fiscal_period": r["fiscal_period"],
                                        "fiscal_year": r["fiscal_year"], "release_date": r["release_date"],
                                        "origin": r["release_date"] - timedelta(days=1)})
    for r in (cal.iter_rows(named=True) if cal.height else []):
        prev = [t for t in targets[r["issuer_id"]] if 350 <= (r["period_end"] - t["period_end"]).days <= 380]
        fy = (prev[0]["fiscal_year"] + 1) if prev and prev[0].get("fiscal_year") else None
        targets[r["issuer_id"]].append({"period_end": r["period_end"], "fiscal_period": r["fiscal_period"],
                                        "fiscal_year": fy, "release_date": r["expected_date"],
                                        "origin": min(today, r["expected_date"] - timedelta(days=1))})
    fpaths = forecast_paths(s, fund, targets)
    tasks = []
    for (iid,), g in fund.partition_by("issuer_id", as_dict=True).items():
        if iid not in targets:
            continue
        nq = g.filter(pl.col("period_kind") == "Q").height
        nh = g.filter(pl.col("period_kind") == "H").height
        kind = "Q" if nq >= 8 or nq >= nh else "H"
        g = g.filter(pl.col("period_kind") == kind)
        # >= 2 rows: the seasonal random walk needs the target and the year-ago period only (the estimated models
        # check their own minimum history per target)
        if g.height < 2:
            continue
        series: dict[str, list[tuple[date, date, float, str]]] = {}
        rev = g.filter(pl.col("item_id") == "revenue")
        if rev.height:
            series["revenue"] = list(rev.select("period_end", "knowledge_date", "value", "derivation").iter_rows())
        dil = g.filter(pl.col("item_id") == "eps_diluted")
        bas = g.filter(pl.col("item_id") == "eps_basic")
        basis = ("diluted" if dil.height >= max(min(6, bas.height), bas.height // 2, 1)
                 else ("basic" if bas.height else None))
        e = dil if basis == "diluted" else bas
        if basis:
            series["eps_diluted"] = list(e.select("period_end", "knowledge_date", "value", "derivation").iter_rows())
        cur = rev.get_column("currency").drop_nulls().tail(1).to_list()
        tg = {}
        for t in targets[iid]:
            tg.setdefault(t["period_end"], t)  # first (events) wins over calendar duplicates
        tasks.append({"issuer_id": iid, "months": 3 if kind == "Q" else 6, "series": series, "eps_basis": basis,
                      "splits": splits.get(sec_of.get(iid, ""), []), "targets": list(tg.values()),
                      "currency": cur[0] if cur else None, "fpath": fpaths.get(iid, {})})
    return tasks


def run_expectations(settings: Settings | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    p = params or {}
    t0 = time.perf_counter()
    mode = str(p.get("mode", "nightly"))
    today = C.now_utc().date()
    only = {x.strip() for x in str(p["issuers"]).split(",") if x.strip()} if p.get("issuers") else None
    tasks = build_tasks(s, mode, date.fromisoformat(str(p.get("since", "2016-01-01"))), today,
                        int(p.get("recent_days", 120)), only)
    from app.ops.ram_budget import pool_workers, small_worker_threads  # noqa: PLC0415

    # full universe (W1 2026-10-31): the pool fits the job's RAM budget (each spawned worker ~0.35 GB with polars /
    # numpy and one chunk of 25 issuers) and runs with 1-thread workers
    workers = pool_workers(int(p.get("workers", 6)), 0.35)
    max_s = float(p.get("max_minutes", 60)) * 60
    rows: list[dict[str, Any]] = []
    chunks = [tasks[i:i + 25] for i in range(0, len(tasks), 25)]
    n_written, done = 0, 0
    if workers <= 1 or len(chunks) <= 1:
        for ch in chunks:
            rows += _chunk_worker(ch)
            done += 1
            if time.perf_counter() - t0 > max_s:
                break
    else:
        with small_worker_threads(1), ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_chunk_worker, ch) for ch in chunks]
            for f in as_completed(futs):
                rows += f.result()
                done += 1
                if time.perf_counter() - t0 > max_s:
                    for g in futs:
                        g.cancel()
                    break
    df = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    if df.height:
        prior = None
        if mode != "backfill":
            try:
                ids = list({t["issuer_id"] for t in tasks})
                prior = (scan_table("earnings_expectations", s).filter(pl.col("issuer_id").is_in(ids)
                                                                      & (pl.col("status") == "released"))
                         .select("issuer_id", "item_id", "period_end", "model", "expectation", "actual",
                                 "actual_knowledge_date").collect())
            except Exception:  # noqa: BLE001 - first run
                prior = None
        df = finish(df, prior).with_columns(pl.lit(C.now_utc()).alias("computed_at"), pl.lit(p.get("_run_id")).alias("run_id"))
        # nightly: earlier released rows stay as written (upsert on the key; same origin -> same values)
        # a full backfill replaces the table (drops rows of targets that no longer pass the guards); nightly upserts
        full = mode == "backfill" and not only and done >= len(chunks)
        n_written = C.write(df, "earnings_expectations", s, mode="overwrite" if full else "upsert")
    rep = {"mode": mode, "issuers": len(tasks), "chunks_done": done, "chunks": len(chunks), "rows": n_written,
           "seconds": round(time.perf_counter() - t0, 1)}
    if df.height:
        rel = df.filter(pl.col("is_primary") & (pl.col("status") == "released"))
        rep["released_primary"] = rel.height
        for it in ITEMS:
            x = rel.filter(pl.col("item_id") == it)
            if x.height:
                rep[f"{it}_median_abs_surprise_pct"] = round(float(x.get_column("surprise_pct").abs().median() or 0), 4)
    C.update_state(f"earnings_expectations_{mode}", rep | {"at": C.now_utc().isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = dict(ctx.params or {})
    p["_run_id"] = ctx.run_id
    rep = run_expectations(ctx.settings, p)
    st = "success" if rep["chunks_done"] >= rep["chunks"] else "partial"
    return JobResult(status=st, rows_written={"earnings_expectations": rep["rows"]}, entities_ok=rep["issuers"],
                     message=str(rep))
