"""Recession-risk panel (W2.5). Job `macro_recession` (nightly). Every row carries a method label.

* nyfed_yield_curve_official: the NY Fed's published probability (allmonth.xls, sheet rec_prob; probit of
  NBER recession 12 months ahead on the 10y - 3m Treasury spread, bond-equivalent 3m). The file dates each probability
  at the TARGET month; we store it at the spread observation month (target - 12) with details.target_month.
* yield_curve_probit: our re-estimate of the same probit, P(rec_{t+12}) = Phi(a + b * spread_t), monthly averages,
  per country with official 10y and 3m rates (catalog indicators yield_10y / rate_3m, first rank with data).
  Recession indicator: NBER (FRED USREC) for the US; elsewhere two or more consecutive quarters of negative official
  real GDP q/q (latest vintage of the tracked gdp_qoq series). Needs >= 2 recession episodes; in-sample fit labelled.
* sahm_realtime: FRED SAHMREALTIME (real-time Sahm rule, St. Louis Fed); signal when >= 0.5.
* hmm_regime: P(contraction) from macro_regimes (our Gaussian HMM, filtered probabilities).
* oecd_cli_turning_point: OECD CLI (amplitude adjusted) phases and turning points: local extremum within +-6 months,
  confirmed after 6 months, alternating peaks/troughs; signal = 'slowdown' phase (below 100 and falling) or the latest
  confirmed turning point is a peak and the CLI is below 100.
"""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.macro.calendar import TARGET_COUNTRIES, load_obs, tracked_series

log = get_logger(__name__)

NYFED_RECPROB_URL = "https://www.newyorkfed.org/medialibrary/media/research/capital_markets/allmonth.xls"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126 Safari/537.36 BiG-Equity-360 research"}
LABELS = {
    "nyfed_yield_curve_official": "NY Fed yield-curve recession probability, 12m ahead (official, published)",
    "yield_curve_probit": "Yield-curve probit 3m10y, 12m ahead (model-based re-estimate, ours)",
    "sahm_realtime": "Sahm rule, real-time (official, FRED SAHMREALTIME)",
    "hmm_regime": "HMM regime P(contraction) (model-based, ours)",
    "oecd_cli_turning_point": "OECD CLI turning points / phase (official CLI; turning-point rule ours)",
}
HMM_SCOPE = {"US": "US", "GLOBAL": "GLOBAL", "EU": "EA", "JP": "JP", "CN": "CN"}
SCHEMA = {"method": pl.Utf8, "country": pl.Utf8, "date": pl.Date, "probability": pl.Float64, "value": pl.Float64,
          "signal": pl.Boolean, "threshold": pl.Float64, "horizon_months": pl.Int16, "method_label": pl.Utf8,
          "official": pl.Boolean, "source": pl.Utf8, "details": pl.Utf8, "computed_at": pl.Datetime("us", "UTC")}


def _row(method: str, cc: str, d: date, prob: float | None = None, value: float | None = None,
         signal: bool | None = None, threshold: float | None = None, horizon: int | None = None,
         official: bool = False, source: str = "", details: dict[str, Any] | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    f = (lambda x: None if x is None or (isinstance(x, float) and not np.isfinite(x)) else float(x))
    return {"method": method, "country": cc, "date": d, "probability": f(prob), "value": f(value), "signal": signal,
            "threshold": threshold, "horizon_months": horizon, "method_label": LABELS[method], "official": official,
            "source": source, "details": json.dumps(details, default=str) if details else None,
            "computed_at": now or datetime.now(UTC)}


# ------------------------------------------------------------------------------------------------ NY Fed official


def parse_nyfed_recprob(content: bytes, now: datetime | None = None) -> list[dict[str, Any]]:
    df = pd.read_excel(io.BytesIO(content), sheet_name="rec_prob")
    df["Date"] = pd.to_datetime(df["Date"])
    spread = {pd.Period(d, "M"): s for d, s in zip(df["Date"], df["Spread"], strict=False) if pd.notna(s)}
    out = []
    for d, p in zip(df["Date"], df["Rec_prob"], strict=False):
        if pd.isna(p):
            continue
        tgt = pd.Period(d, "M")
        obs_m = tgt - 12
        out.append(_row("nyfed_yield_curve_official", "US", obs_m.start_time.date(), float(p), spread.get(obs_m),
                        float(p) >= 0.3, 0.3, 12, True, f"Federal Reserve Bank of New York ({NYFED_RECPROB_URL})",
                        {"target_month": str(tgt)}, now))
    return out


# ------------------------------------------------------------------------------------------------ probit


def technical_recession_months(gdp_qoq: pd.Series) -> pd.Series:
    """Quarterly q/q growth (PeriodIndex Q) -> monthly 0/1 flag: months in runs of >= 2 negative quarters."""
    neg = (gdp_qoq < 0).astype(int)
    run = neg & ((neg.shift(1, fill_value=0) == 1) | (neg.shift(-1, fill_value=0) == 1))
    months = {}
    for q, v in run.items():
        for m in pd.period_range(q.asfreq("M", "start"), q.asfreq("M", "end"), freq="M"):
            months[m] = int(v)
    return pd.Series(months).sort_index()


def fit_probit(spread: pd.Series, rec: pd.Series, horizon: int = 12) -> dict[str, Any] | None:
    """spread: monthly (PeriodIndex M); rec: monthly 0/1. Returns coefficients and fitted probabilities for all months
    with a spread (target rec_{t+h})."""
    import statsmodels.api as sm  # noqa: PLC0415

    y = rec.shift(-horizon).reindex(spread.index)
    d = pd.DataFrame({"y": y, "x": spread}).dropna()
    if len(d) < 120 or d["y"].sum() < 6:
        return None
    episodes = int(((d["y"] == 1) & (d["y"].shift(1, fill_value=0) == 0)).sum())
    if episodes < 2:
        return None
    try:
        r = sm.Probit(d["y"].astype(float), sm.add_constant(d["x"].astype(float))).fit(disp=False)
    except Exception:  # noqa: BLE001
        return None
    a, b = float(r.params["const"]), float(r.params["x"])
    from scipy.stats import norm  # noqa: PLC0415

    prob = pd.Series(norm.cdf(a + b * spread.to_numpy(dtype=float)), index=spread.index)
    return {"a": a, "b": b, "n": len(d), "episodes": episodes, "pseudo_r2": float(r.prsquared), "prob": prob,
            "sample": [str(d.index[0]), str(d.index[-1])]}


def _monthly_avg(obs: pd.DataFrame | None) -> pd.Series:
    if obs is None or obs.empty:
        return pd.Series(dtype=float)
    o = obs.sort_values(["date", "knowledge_date"]).drop_duplicates("date", keep="last")
    s = pd.Series(o["value"].to_numpy(dtype=float), index=pd.PeriodIndex([pd.Period(d, "M") for d in o["date"]], freq="M"))
    return s.groupby(level=0).mean()


def _quarterly_latest(obs: pd.DataFrame | None) -> pd.Series:
    if obs is None or obs.empty:
        return pd.Series(dtype=float)
    o = obs.sort_values(["date", "knowledge_date"]).drop_duplicates("date", keep="last")
    return pd.Series(o["value"].to_numpy(dtype=float), index=pd.PeriodIndex([pd.Period(d, "Q") for d in o["date"]], freq="Q"))


def _best_series(settings: Settings, indicator: str) -> dict[str, str]:
    """country -> best-ranked catalog series id for an indicator (with data)."""
    from app.core.storage import read_table  # noqa: PLC0415
    from app.macro.calendar import NON_OFFICIAL_SOURCES, _catalog_rows  # noqa: PLC0415

    have = set(read_table("macro_series", columns=["series_id", "last_observation_date"], settings=settings)
               .filter(pl.col("last_observation_date") >= date.today() - timedelta(days=200))["series_id"].to_list())
    best: dict[str, tuple[int, str]] = {}
    for g in _catalog_rows(settings):
        if g.series_id not in have or g.source_id in NON_OFFICIAL_SOURCES:
            continue
        if indicator in (g.indicator or "").split(",") and (g.country not in best or g.rank < best[g.country][0]):
            best[g.country] = (g.rank, g.series_id)
    return {k: v[1] for k, v in best.items()}


def probit_rows(settings: Settings, now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    y10 = _best_series(settings, "yield_10y")
    r3 = _best_series(settings, "rate_3m")
    y10["US"], r3["US"] = "fred:DGS10", "fred:DGS3MO"
    gdp = {t.country: t.series_id for t in tracked_series(settings) if t.indicator == "gdp_qoq"}
    ccs = [c for c in TARGET_COUNTRIES if c in y10 and c in r3]
    obs = load_obs(set(y10[c] for c in ccs) | set(r3[c] for c in ccs) | set(gdp.values()) | {"fred:USREC"}, settings)
    rows, rep = [], {}
    for cc in ccs:
        sp = (_monthly_avg(obs.get(y10[cc])) - _monthly_avg(obs.get(r3[cc]))).dropna()
        if len(sp) < 120:
            rep[cc] = "spread history < 10y"
            continue
        if cc == "US":
            rec = _monthly_avg(obs.get("fred:USREC")).round().astype(int)
            rdef = "NBER (FRED USREC)"
        else:
            g = _quarterly_latest(obs.get(gdp.get(cc, ""))) if cc in gdp else pd.Series(dtype=float)
            if len(g) < 40:
                rep[cc] = "no official quarterly GDP history"
                continue
            rec = technical_recession_months(g)
            rdef = f"2+ negative quarters of {gdp[cc]}"
        # the label is known only up to the last month with recession data
        rec = rec[rec.index <= rec.index.max()]
        f = fit_probit(sp, rec)
        if f is None:
            rep[cc] = "too few recession episodes for a probit"
            continue
        src = f"spread {y10[cc]} - {r3[cc]}; recession {rdef}"
        det = {"a": round(f["a"], 4), "b": round(f["b"], 4), "n": f["n"], "episodes": f["episodes"],
               "pseudo_r2": round(f["pseudo_r2"], 3), "sample": f["sample"], "in_sample": True}
        for m, p in f["prob"].items():
            if m.start_time.date() < date(1990, 1, 1):
                continue
            rows.append(_row("yield_curve_probit", cc, m.start_time.date(), float(p), float(sp.get(m)), float(p) >= 0.3,
                             0.3, 12, False, src, det if m == f["prob"].index[-1] else {"a": det["a"], "b": det["b"]}, now))
        rep[cc] = {"latest": round(float(f["prob"].iloc[-1]), 3), "month": str(f["prob"].index[-1]), **det}
    return rows, rep


# ------------------------------------------------------------------------------------------------ Sahm / HMM / CLI


def sahm_rows(settings: Settings, now: datetime) -> list[dict[str, Any]]:
    obs = load_obs(["fred:SAHMREALTIME"], settings).get("fred:SAHMREALTIME")
    s = _monthly_avg(obs)
    return [_row("sahm_realtime", "US", m.start_time.date(), None, float(v), bool(v >= 0.5), 0.5, None, True,
                 "FRED SAHMREALTIME (St. Louis Fed)", None, now) for m, v in s.items() if pd.notna(v)
            and m.year >= 1990]


def hmm_rows(settings: Settings, now: datetime) -> list[dict[str, Any]]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        rg = read_table("macro_regimes", settings=settings)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rg.iter_rows(named=True):
        labels = list(r["state_labels"] or [])
        probs = list(r["probabilities"] or [])
        if "contraction" not in labels or len(probs) != len(labels):
            continue
        p = float(probs[labels.index("contraction")])
        cc = HMM_SCOPE.get(r["region_scope"], r["region_scope"])
        out.append(_row("hmm_regime", cc, r["date"], p, None, p >= 0.5, 0.5, None, False, f"macro_regimes {r['model_id']}",
                        {"model_id": r["model_id"], "regime": r["regime_label"],
                         "probabilities": dict(zip(labels, [round(x, 4) for x in probs], strict=False))}, now))
    # several models may map to the same (country, date): keep the first model id alphabetically (stable)
    seen, uniq = set(), []
    for x in sorted(out, key=lambda x: (x["country"], x["date"], x["source"])):
        k = (x["country"], x["date"])
        if k not in seen:
            seen.add(k)
            uniq.append(x)
    return uniq


def turning_points(s: pd.Series, k: int = 6) -> list[tuple[pd.Period, str]]:
    """Alternating confirmed peaks/troughs: local extremum within +-k months, confirmed when k months follow."""
    v = s.dropna()
    pts: list[tuple[pd.Period, str]] = []
    arr = v.to_numpy()
    for i in range(k, len(arr) - k):
        w = arr[i - k:i + k + 1]
        if arr[i] == w.max() and arr[i] > arr[i - 1]:
            t = "peak"
        elif arr[i] == w.min() and arr[i] < arr[i - 1]:
            t = "trough"
        else:
            continue
        if pts and pts[-1][1] == t:   # keep the more extreme of two consecutive same-type points
            j = v.index.get_loc(pts[-1][0])
            if (t == "peak" and arr[i] > arr[j]) or (t == "trough" and arr[i] < arr[j]):
                pts[-1] = (v.index[i], t)
            continue
        pts.append((v.index[i], t))
    return pts


def cli_rows(settings: Settings, now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.core.storage import read_table  # noqa: PLC0415

    ms = read_table("macro_series", columns=["series_id", "country"], settings=settings)
    cands = ms.filter(pl.col("series_id").str.contains(r"^oecd:CLI\.[A-Z0-9]+\.LI$"))
    ids = dict(zip(cands["series_id"].to_list(), cands["country"].to_list(), strict=False))
    obs = load_obs(ids, settings)
    rows, rep = [], {}
    for sid, cc in ids.items():
        s = _monthly_avg(obs.get(sid)).dropna()
        if len(s) < 60:
            continue
        tps = turning_points(s)
        last_tp: tuple[pd.Period, str] | None = None
        j = 0
        for m in s.index:
            while j < len(tps) and tps[j][0] + 6 <= m:   # confirmed 6 months after the turning point
                last_tp = tps[j]
                j += 1
            v = float(s[m])
            prev = s.get(m - 1)
            rising = prev is not None and pd.notna(prev) and v >= float(prev)
            phase = ("expansion" if rising else "downturn") if v >= 100 else ("recovery" if rising else "slowdown")
            sig = phase == "slowdown" or (last_tp is not None and last_tp[1] == "peak" and v < 100)
            if m.year < 1990:
                continue
            rows.append(_row("oecd_cli_turning_point", cc, m.start_time.date(), None, v, bool(sig), 100.0, None, True,
                             f"OECD CLI {sid}", {"phase": phase, "last_turning_point": (
                                 {"type": last_tp[1], "month": str(last_tp[0])} if last_tp else None)}, now))
        rep[cc] = {"series": sid, "turning_points": len(tps), "latest_phase": rows[-1] and json.loads(rows[-1]["details"])["phase"]}
    return rows, rep


# ------------------------------------------------------------------------------------------------ job


def run_recession(settings: Settings | None = None, write: bool = True) -> dict[str, Any]:
    from app.core.http import HttpClient  # noqa: PLC0415
    from app.core.ratelimit import RateLimit  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    now = datetime.now(UTC)
    rep: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    try:
        with HttpClient("newyorkfed", settings=s, headers=UA, rate_limit=RateLimit(0.5, 1)) as h:
            b = h.get(NYFED_RECPROB_URL, cache_ttl=6 * 3600).content
        raw = s.raw_dir / "macro_v2"
        raw.mkdir(parents=True, exist_ok=True)
        (raw / "nyfed_allmonth.xls").write_bytes(b)
        r = parse_nyfed_recprob(b, now)
        rows += r
        rep["nyfed_yield_curve_official"] = {"status": "ok", "rows": len(r), "latest": (str(r[-1]["date"]), round(r[-1]["probability"], 3)) if r else None}
    except Exception as e:  # noqa: BLE001
        rep["nyfed_yield_curve_official"] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    for name, fn in (("yield_curve_probit", probit_rows), ("oecd_cli_turning_point", cli_rows)):
        try:
            r, sub = fn(s, now)
            rows += r
            rep[name] = {"status": "ok", "rows": len(r), "countries": sub}
        except Exception as e:  # noqa: BLE001
            rep[name] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    for name, fn in (("sahm_realtime", sahm_rows), ("hmm_regime", hmm_rows)):
        try:
            r = fn(s, now)
            rows += r
            rep[name] = {"status": "ok", "rows": len(r), "latest": (str(r[-1]["date"]), r[-1]["value"] if name == "sahm_realtime" else r[-1]["probability"]) if r else None}
        except Exception as e:  # noqa: BLE001
            rep[name] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
    df = pl.DataFrame(rows, schema=SCHEMA, orient="row").unique(["method", "country", "date"], keep="last") if rows \
        else pl.DataFrame(schema=SCHEMA)
    rep["rows"] = df.height
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    if write and df.height:
        rep["rows_written"] = write_table(df, "macro_recession", mode="overwrite", settings=s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_recession (nightly)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_recession(ctx.settings)
    bad = [k for k, v in rep.items() if isinstance(v, dict) and v.get("status") == "failed"]
    return JobResult(status="partial" if bad else "success", rows_written={"macro_recession": rep.get("rows_written", 0)},
                     entities_ok=5 - len(bad), entities_failed=len(bad), message=json.dumps(rep, default=str)[:4000])
