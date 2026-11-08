"""Studio v2 S3 compare runs + model monitoring statistics (owner: model-studio; roadmap v3 item S).

Reads the out-of-fold artifact every CV model writes (data/modeling/runs/<run_id>/oos_eval.parquet: date, entity_id, y,
prediction, fold) and computes, on the rows two runs share:
* Diebold-Mariano (squared error; panels: the cross-sectional mean loss per date, then DM over dates, which is robust to
  cross-sectional correlation) and Clark-West (2007) for nested models (the benchmark's regressors are a subset of the
  model's: MSPE-adjusted, one-sided, Newey-West variance);
* an equity curve per run: panels = equal-weight top-minus-bottom quintile of the prediction each date (y realised);
  time series = sign(prediction) x y; plus the per-date rank IC;
* monitoring: expanding out-of-sample metric (rank IC for panels, R2_oos / RMSE otherwise), PSI of feature and
  prediction distributions against a stored baseline (deciles of the reference sample).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from . import _ml as M
from .results import clean

PSI_BINS = 10
PSI_WARN, PSI_ALERT = 0.10, 0.25


def load_oos(run_id: str, settings: Any = None) -> pd.DataFrame | None:
    from . import engine

    p = engine.run_dir(run_id, settings) / "oos_eval.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    df["date"] = pd.to_datetime(df["date"])
    df["entity_id"] = df["entity_id"].astype(str)
    return df


def _nw_var(d: np.ndarray, lags: int | None = None) -> float:
    T = len(d)
    u = d - d.mean()
    L = int(np.floor(T ** (1 / 3))) if lags is None else lags
    s = u @ u / T
    for k in range(1, L + 1):
        s += 2 * (1 - k / (L + 1)) * (u[k:] @ u[:-k]) / T
    return float(s)


def clark_west(y: np.ndarray, p_small: np.ndarray, p_big: np.ndarray) -> tuple[float, float]:
    """Clark & West (2007) MSPE-adjusted test that the larger (nesting) model improves on the nested benchmark.
    f_t = e_small^2 - (e_big^2 - (p_small - p_big)^2); t = mean(f) / sqrt(NW var / T); one-sided p = 1 - Phi(t)."""
    f = (y - p_small) ** 2 - ((y - p_big) ** 2 - (p_small - p_big) ** 2)
    f = f[np.isfinite(f)]
    T = len(f)
    if T < 10 or np.allclose(f, f[0]):
        return float("nan"), float("nan")
    t = f.mean() / math.sqrt(max(_nw_var(f), 1e-300) / T)
    return float(t), float(stats.norm.sf(t))


def _by_date_loss(df: pd.DataFrame, col: str) -> pd.Series:
    return df.assign(_l=(df["y"] - df[col]) ** 2).groupby("date")["_l"].mean().sort_index()


def equity_curve(df: pd.DataFrame, panel: bool, q: int = 5) -> pd.DataFrame:
    """Per date: strategy return (panel: top minus bottom quintile mean y; time series: sign(pred) * y) and rank IC."""
    if panel:
        rows = []
        for d, g in df.groupby("date"):
            if len(g) < 2 * q:
                continue
            r = g["prediction"].rank(method="first")
            b = pd.qcut(r, q, labels=False)
            ic = g["y"].rank().corr(g["prediction"].rank())
            rows.append((d, g.loc[b == q - 1, "y"].mean() - g.loc[b == 0, "y"].mean(), ic))
        out = pd.DataFrame(rows, columns=["date", "ret", "ic"])
    else:
        g = df.sort_values("date")
        out = pd.DataFrame({"date": g["date"], "ret": np.sign(g["prediction"]) * g["y"], "ic": np.nan})
    out = out.sort_values("date").reset_index(drop=True)
    out["cum"] = out["ret"].cumsum()
    return out


def _sharpe(r: pd.Series, freq_per_year: float) -> float | None:
    r = r.dropna()
    if len(r) < 3 or r.std(ddof=1) == 0:
        return None
    return float(r.mean() / r.std(ddof=1) * math.sqrt(freq_per_year))


def _ppy(dates: pd.Series) -> float:
    u = np.sort(pd.to_datetime(dates.unique()))
    if len(u) < 2:
        return 12.0
    gap = float(np.median(np.diff(u).astype("timedelta64[D]").astype(float)))
    return 365.25 / max(gap, 1.0)


def nested(spec_a: dict[str, Any], spec_b: dict[str, Any]) -> bool:
    """True when run a's regressors are a strict subset of run b's on the same target and dataset (a nested in b)."""
    if not spec_a or not spec_b or spec_a.get("y") != spec_b.get("y"):
        return False
    da, db = dict(spec_a.get("dataset") or {}), dict(spec_b.get("dataset") or {})
    va = {v["name"]: v for v in da.pop("variables", [])}
    vb = {v["name"]: v for v in db.pop("variables", [])}
    if da != db or any(vb.get(k) != v for k, v in va.items() if k in set(spec_a.get("x") or []) | {spec_a.get("y")}):
        return False
    xa, xb = set(spec_a.get("x") or []), set(spec_b.get("x") or [])
    return xa < xb


def compare_oos(run_ids: list[str], specs: dict[str, dict[str, Any]], benchmark: str | None = None,
                settings: Any = None) -> dict[str, Any]:
    """DM / Clark-West of every run against the benchmark (default: the first run with OOS predictions) plus the
    equity curves, on the (date, entity) rows each pair shares. Runs without oos_eval.parquet are listed as skipped."""
    oos = {r: load_oos(r, settings) for r in run_ids}
    have = [r for r in run_ids if oos[r] is not None and len(oos[r])]
    skipped = [{"run_id": r, "reason": "no out-of-fold predictions (method without CV, or run older than v1.50)"}
               for r in run_ids if r not in have]
    out: dict[str, Any] = {"benchmark_run_id": None, "tests": [], "equity": [], "skipped": skipped}
    if not have:
        return out
    bench = benchmark if benchmark in have else have[0]
    out["benchmark_run_id"] = bench
    B = oos[bench]
    for r in have:
        df = oos[r]
        panel = bool((df["entity_id"] != "").any())
        eq = equity_curve(df, panel)
        ppy = _ppy(df["date"])
        idx = np.unique(np.linspace(0, len(eq) - 1, min(len(eq), 600)).astype(int)) if len(eq) else []
        out["equity"].append(clean({
            "run_id": r, "kind": "top_minus_bottom_quintile" if panel else "sign_of_prediction",
            "dates": [eq["date"].iloc[i] for i in idx], "cum_return": [eq["cum"].iloc[i] for i in idx],
            "sharpe": _sharpe(eq["ret"], ppy), "mean_ic": float(eq["ic"].mean()) if panel and eq["ic"].notna().any() else None,
            "n_periods": int(len(eq))}))
        if r == bench:
            continue
        j = B.merge(df, on=["date", "entity_id"], suffixes=("_b", "_m"))
        j = j[np.isfinite(j["prediction_b"]) & np.isfinite(j["prediction_m"]) & np.isfinite(j["y_b"])]
        row: dict[str, Any] = {"run_id": r, "benchmark_run_id": bench, "n_rows": int(len(j)), "n_dates": int(j["date"].nunique())}
        if len(j) < 10:
            row["note"] = "fewer than 10 shared out-of-fold rows (different dataset / folds)"
            out["tests"].append(clean(row))
            continue
        if not np.allclose(j["y_b"], j["y_m"], equal_nan=True):
            row["note"] = "the runs predict different targets: tests skipped"
            out["tests"].append(clean(row))
            continue
        j = j.rename(columns={"y_b": "y"})
        lb, lm = _by_date_loss(j, "prediction_b"), _by_date_loss(j, "prediction_m")
        dm, dmp = M.dm_test(lb.to_numpy(), lm.to_numpy(), 1)
        row.update({"mse_model": float(((j["y"] - j["prediction_m"]) ** 2).mean()),
                    "mse_benchmark": float(((j["y"] - j["prediction_b"]) ** 2).mean()), "dm_stat": dm, "dm_p": dmp,
                    "dm_note": "stat > 0: the run is more accurate than the benchmark (loss per date, HAC)"})
        is_nested = nested(specs.get(bench) or {}, specs.get(r) or {})
        row["nested"] = is_nested
        if is_nested:
            jj = j.sort_values("date")
            cw, cwp = clark_west(jj["y"].to_numpy(), jj["prediction_b"].to_numpy(), jj["prediction_m"].to_numpy())
            row.update({"cw_stat": cw, "cw_p": cwp, "cw_note": "Clark-West: H0 the nested benchmark is as accurate; one-sided"})
        better = row["mse_model"] < row["mse_benchmark"]
        sig = (row.get("cw_p") if is_nested else dmp)
        row["verdict"] = ("beats the benchmark" if better and sig is not None and np.isfinite(sig) and sig < 0.05 else
                          "not significantly better" if better else "worse than the benchmark")
        out["tests"].append(clean(row))
    return out


# ------------------------------------------------------------------------------------------------ monitoring


def decile_edges(x: np.ndarray, bins: int = PSI_BINS) -> list[float]:
    x = x[np.isfinite(x)]
    if len(x) < bins:
        return []
    e = np.unique(np.quantile(x, np.linspace(0, 1, bins + 1)[1:-1]))
    return e.tolist()


def shares(x: np.ndarray, edges: list[float]) -> list[float]:
    x = x[np.isfinite(x)]
    if not len(x):
        return []
    c = np.bincount(np.searchsorted(np.asarray(edges), x, side="right"), minlength=len(edges) + 1)
    return (c / c.sum()).tolist()


def psi(ref: list[float], cur: list[float], eps: float = 1e-4) -> float | None:
    """Population stability index sum (c - r) ln(c / r) over the reference bins (0.1 / 0.25: watch / drift)."""
    if not ref or not cur or len(ref) != len(cur):
        return None
    r, c = np.maximum(np.asarray(ref), eps), np.maximum(np.asarray(cur), eps)
    return float(np.sum((c - r) * np.log(c / r)))


def profile(values: dict[str, np.ndarray]) -> dict[str, dict[str, list[float]]]:
    out = {}
    for k, v in values.items():
        e = decile_edges(np.asarray(v, float))
        if e:
            out[k] = {"edges": e, "shares": shares(np.asarray(v, float), e)}
    return out


def expanding_metric(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Expanding out-of-sample metric by date: panels -> cumulative mean rank IC (and its t-stat); time series ->
    expanding R2_oos (vs the expanding mean of y) and RMSE."""
    df = df.sort_values("date")
    panel = bool((df["entity_id"] != "").any())
    out = []
    if panel:
        ics = []
        for d, g in df.groupby("date"):
            if len(g) >= 5:
                ic = g["y"].rank().corr(g["prediction"].rank())
                if np.isfinite(ic):
                    ics.append(ic)
                    a = np.asarray(ics)
                    t = float(a.mean() / (a.std(ddof=1) / math.sqrt(len(a)))) if len(a) > 2 and a.std(ddof=1) > 0 else None
                    out.append({"date": d, "ic": ic, "expanding_ic": float(a.mean()), "expanding_ic_t": t, "n_dates": len(a)})
    else:
        e2 = (df["y"] - df["prediction"]) ** 2
        ybar = df["y"].expanding().mean().shift(1)
        b2 = (df["y"] - ybar) ** 2
        ce, cb = e2.cumsum(), b2.cumsum()
        for i, d in enumerate(df["date"]):
            if i >= 5:
                out.append({"date": d, "expanding_rmse": float(math.sqrt(ce.iloc[i] / (i + 1))),
                            "expanding_r2_oos": float(1 - ce.iloc[i] / cb.iloc[i]) if cb.iloc[i] > 0 else None, "n": i + 1})
    return clean(out)


def headline(exp: list[dict[str, Any]]) -> tuple[str, float | None]:
    if not exp:
        return "none", None
    last = exp[-1]
    if "expanding_ic" in last:
        return "rank_ic", last["expanding_ic"]
    return "r2_oos", last.get("expanding_r2_oos")


def run_frames(run_id: str, settings: Any = None) -> tuple[pd.DataFrame | None, pd.DataFrame | None, dict[str, Any]]:
    from . import engine

    d = engine.run_dir(run_id, settings)
    ds = pd.read_parquet(d / "dataset.parquet") if (d / "dataset.parquet").exists() else None
    if ds is not None:
        ds["date"] = pd.to_datetime(ds["date"])
    spec = engine.read_spec(run_id, settings)
    return ds, load_oos(run_id, settings), (spec.model_dump(mode="json") if spec else {})


def baseline(run_id: str, settings: Any = None) -> dict[str, Any]:
    """Monitoring baseline at promotion: decile profile of every feature over the run's dataset, of the out-of-fold
    predictions, the last date and the headline OOS metric."""
    ds, oos, spec = run_frames(run_id, settings)
    feats = [c for c in (spec.get("x") or []) if ds is not None and c in ds.columns]
    prof = profile({c: ds[c].to_numpy(float) for c in feats}) if ds is not None else {}
    pred = profile({"prediction": oos["prediction"].to_numpy(float)}) if oos is not None else {}
    exp = expanding_metric(oos) if oos is not None else []
    kind, val = headline(exp)
    return clean({"features": prof, "prediction": pred.get("prediction"), "end_date": ds["date"].max() if ds is not None else None,
                  "oos_metric": {"kind": kind, "value": val}, "n_features_profiled": len(prof)})


def evaluate(base: dict[str, Any], run_id: str, psi_threshold: float = PSI_ALERT, metric_floor: float | None = None,
             recent_frac: float = 0.2, settings: Any = None) -> dict[str, Any]:
    """Monitoring of a model against its baseline using run `run_id` (the promoted run, or a fresh re-run):
    the current sample = rows after the baseline end date when there are any, else the last `recent_frac` of dates.
    Breaches: a feature or the prediction PSI above psi_threshold, or the expanding OOS metric below metric_floor."""
    ds, oos, spec = run_frames(run_id, settings)
    end = pd.to_datetime(base.get("end_date")) if base.get("end_date") else None

    def recent(df: pd.DataFrame | None) -> pd.DataFrame | None:
        if df is None or not len(df):
            return df
        if end is not None and (df["date"] > end).any():
            return df[df["date"] > end]
        u = np.sort(df["date"].unique())
        return df[df["date"] >= u[int(len(u) * (1 - recent_frac))]]

    cur, cur_oos = recent(ds), recent(oos)
    rows = []
    for f, b in (base.get("features") or {}).items():
        if cur is None or f not in cur.columns:
            continue
        v = psi(b["shares"], shares(cur[f].to_numpy(float), b["edges"]))
        rows.append({"feature": f, "psi": v, "status": None if v is None else ("drift" if v > psi_threshold else "watch" if v > PSI_WARN else "stable"),
                     "n_current": int(np.isfinite(cur[f].to_numpy(float)).sum())})
    rows.sort(key=lambda r: -(r["psi"] or 0))
    bp = base.get("prediction")
    ppsi = psi(bp["shares"], shares(cur_oos["prediction"].to_numpy(float), bp["edges"])) if bp and cur_oos is not None and len(cur_oos) else None
    exp = expanding_metric(oos) if oos is not None else []
    kind, val = headline(exp)
    breaches = [f"feature {r['feature']} PSI {r['psi']:.2f} > {psi_threshold:g}" for r in rows if r["psi"] is not None and r["psi"] > psi_threshold]
    if ppsi is not None and ppsi > psi_threshold:
        breaches.append(f"prediction PSI {ppsi:.2f} > {psi_threshold:g}")
    if metric_floor is not None and val is not None and val < metric_floor:
        breaches.append(f"expanding OOS {kind} {val:.4f} < floor {metric_floor:g}")
    step = max(1, len(exp) // 400)
    return clean({"run_id": run_id, "current_window": {"start": cur["date"].min() if cur is not None and len(cur) else None,
                                                       "end": cur["date"].max() if cur is not None and len(cur) else None,
                                                       "after_baseline_end": bool(end is not None and ds is not None and (ds["date"] > end).any())},
                  "feature_psi": rows, "prediction_psi": ppsi, "metric": {"kind": kind, "value": val,
                                                                          "baseline": (base.get("oos_metric") or {}).get("value")},
                  "expanding": exp[::step] + (exp[-1:] if exp and (len(exp) - 1) % step else []), "breaches": breaches,
                  "drift": bool(breaches), "psi_threshold": psi_threshold, "metric_floor": metric_floor})


__all__ = ["compare_oos", "clark_west", "equity_curve", "psi", "profile", "baseline", "evaluate", "expanding_metric"]
