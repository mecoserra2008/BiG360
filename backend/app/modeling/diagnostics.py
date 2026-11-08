"""Shared diagnostics + guardrails for Model Studio methods (owner: model-studio).

Regression diagnostics: residuals/fitted plot data, QQ, histogram, Breusch-Pagan, White, Durbin-Watson, Ljung-Box,
Jarque-Bera, VIF, condition number. Stationarity: ADF + KPSS, ACF/PACF. Rolling-origin out-of-sample evaluation
(expanding window) against naive / random-walk-with-drift / historical-mean baselines.

Guardrails: look-ahead regressors (error), observations per parameter, near-singularity, non-stationarity
(spurious regression), multiple-testing notes.
"""

from __future__ import annotations

import warnings as _w
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy import stats

from .results import GuardrailError, Result, clean, downsample_idx, series

MIN_OBS_PER_PARAM = 10


# ------------------------------------------------------------------------------------------------ guardrails


def check_lookahead(meta: dict[str, Any], x: list[str], weights: str | None = None) -> None:
    """A regressor built with a lead / forward transform is look-ahead bias by construction -> hard error."""
    leads = set(meta.get("lead_variables") or [])
    pairs = meta.get("interaction_pairs") or {}
    bad = [c for c in x + ([weights] if weights else [])
           if c in leads or any(p in leads for p in pairs.get(c, []))]
    if bad:
        raise GuardrailError("lookahead", f"look-ahead: {', '.join(bad)} use a lead/forward transform and cannot be "
                             "regressors/features (only the dependent variable may look forward)")


def check_obs(res: Result, n: int, k: int, what: str = "parameters") -> None:
    if n < k + 2:
        raise GuardrailError("too_few_obs", f"only {n} complete observations for {k} {what} (need at least {k + 2})")
    if n < MIN_OBS_PER_PARAM * k:
        res.warn("few_obs_per_param", f"{n} observations for {k} {what} ({n / max(k, 1):.1f} per parameter; "
                 f"rule of thumb >= {MIN_OBS_PER_PARAM}): estimates may be unstable / overfit")


def check_design(res: Result, X: np.ndarray, names: list[str], has_const: bool = True) -> dict[str, Any]:
    """Rank, condition number (standardized, excluding the constant) and VIF. Singular -> GuardrailError."""
    k = X.shape[1]
    rank = np.linalg.matrix_rank(X)
    if rank < k:
        # find a minimal set of offending columns for the message
        offenders = []
        for j in range(k):
            if np.linalg.matrix_rank(np.delete(X, j, axis=1)) == rank:
                offenders.append(names[j])
        raise GuardrailError("singular", f"design matrix is singular (rank {rank} < {k} columns); collinear: "
                             f"{', '.join(offenders[:8])}")
    cols = [j for j, n in enumerate(names) if not (has_const and n == "const")]
    out: dict[str, Any] = {"rank": int(rank)}
    if cols:
        Z = X[:, cols]
        sd = Z.std(axis=0)
        sd[sd == 0] = 1.0
        Zs = (Z - Z.mean(axis=0)) / sd
        try:
            cond = float(np.linalg.cond(Zs)) if Zs.shape[1] > 1 else 1.0
        except np.linalg.LinAlgError:
            cond = float("inf")
        out["condition_number"] = cond
        if cond > 30:
            res.warn("near_singular", f"condition number {cond:.0f} (> 30 on standardized regressors): strong "
                     "multicollinearity; coefficients may be imprecise")
        vifs = vif(X, names, has_const)
        out["vif"] = vifs
        high = [f"{r['term']} ({r['vif']:.1f})" for r in vifs if r["vif"] is not None and r["vif"] > 10]
        if high:
            res.warn("high_vif", "VIF > 10: " + ", ".join(high[:10]))
    return out


def vif(X: np.ndarray, names: list[str], has_const: bool = True) -> list[dict[str, Any]]:
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    out = []
    if X.shape[1] < (3 if has_const else 2):
        return [{"term": n, "vif": 1.0 if n != "const" else None} for n in names if n != "const"]
    for j, n in enumerate(names):
        if has_const and n == "const":
            continue
        try:
            with np.errstate(divide="ignore", invalid="ignore"):
                v = float(variance_inflation_factor(X, j))
        except Exception:
            v = None
        out.append({"term": n, "vif": clean(v)})
    return out


def multiple_testing_note(res: Result, n_tests: int, what: str = "coefficients") -> None:
    if n_tests > 5:
        res.info("multiple_testing", f"{n_tests} {what} tested at once: about {0.05 * n_tests:.1f} would look "
                 "significant at 5% by chance. Use the Benjamini-Hochberg adjusted p-values (p_value_adj) or a "
                 "pre-registered hypothesis.")


# ------------------------------------------------------------------------------------------------ stationarity


def adf_kpss(x: np.ndarray, regression: str = "c") -> dict[str, Any]:
    """ADF (H0 unit root) + KPSS (H0 stationary) with a combined verdict."""
    from statsmodels.tsa.stattools import adfuller, kpss

    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    out: dict[str, Any] = {"n": int(len(x))}
    if len(x) < 12 or np.nanstd(x) == 0:
        out["verdict"] = "insufficient data"
        return out
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        try:
            a = adfuller(x, regression=regression, autolag="AIC")
            out.update(adf_stat=float(a[0]), adf_p=float(a[1]), adf_lags=int(a[2]))
        except Exception as e:  # pragma: no cover
            out["adf_error"] = str(e)
        try:
            k = kpss(x, regression=regression, nlags="auto")
            out.update(kpss_stat=float(k[0]), kpss_p=float(k[1]))
        except Exception as e:  # pragma: no cover
            out["kpss_error"] = str(e)
    ap, kp = out.get("adf_p"), out.get("kpss_p")
    if ap is None or kp is None:
        out["verdict"] = "inconclusive"
    elif ap < 0.05 and kp > 0.05:
        out["verdict"] = "stationary"
    elif ap >= 0.05 and kp <= 0.05:
        out["verdict"] = "unit root"
    elif ap < 0.05 and kp <= 0.05:
        out["verdict"] = "conflicting (possibly trend/difference-stationary or structural break)"
    else:
        out["verdict"] = "conflicting (low power)"
    return out


def suggest_d(x: np.ndarray, max_d: int = 2) -> int:
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    for d in range(max_d + 1):
        r = adf_kpss(np.diff(x, n=d) if d else x)
        if r.get("verdict") == "insufficient data":
            return d
        # difference while KPSS rejects stationarity or ADF cannot reject a unit root at 10%
        if (r.get("adf_p") if r.get("adf_p") is not None else 1.0) < 0.10 and                 (r.get("kpss_p") if r.get("kpss_p") is not None else 1.0) > 0.05:
            return d
    return max_d


def nonstationarity_check(res: Result, df: pd.DataFrame, y: str | None, x: list[str], max_vars: int = 8) -> None:
    """Time-series regression: warn when y and some regressors look I(1) (spurious-regression risk)."""
    cols = ([y] if y else []) + [c for c in x if df[c].nunique() > 2][:max_vars]
    verdicts, adf_p = {}, {}
    for c in cols:
        r = adf_kpss(df[c].to_numpy())
        verdicts[c], adf_p[c] = r.get("verdict"), r.get("adf_p")
    # conservative screen: a unit root not rejected by ADF at 10% counts as "possibly I(1)"
    ur = [c for c in cols if verdicts[c] == "unit root" or (adf_p[c] is not None and adf_p[c] >= 0.10)]
    if y and y in ur and any(c in ur for c in x):
        res.warn("nonstationary", f"{y} and {', '.join(c for c in ur if c != y)} may be non-stationary (ADF/KPSS): "
                 "levels-on-levels regression may be spurious; difference the series or test for cointegration")
    elif ur:
        res.info("nonstationary_some", "possible unit roots in: " + ", ".join(ur) + " (ADF/KPSS)")
    res.add_table("stationarity", "Stationarity screen (ADF H0: unit root; KPSS H0: stationary)",
                  ["variable", "verdict"], [[c, v] for c, v in verdicts.items()])


def acf_pacf_plot(res: Result, key: str, x: np.ndarray, nlags: int, title: str) -> None:
    from statsmodels.tsa.stattools import acf, pacf

    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = len(x)
    nlags = int(max(1, min(nlags, n // 2 - 1)))
    if n < 8:
        return
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        a = acf(x, nlags=nlags, fft=True)
        try:
            p = pacf(x, nlags=nlags, method="ywm")
        except Exception:
            p = np.full(nlags + 1, np.nan)
    band = 1.96 / np.sqrt(n)
    lags = list(range(nlags + 1))
    res.line_plot(key, title, [series("ACF", lags, a), series("PACF", lags, p)], "lag", "correlation", kind="bar",
                  confidence_band=[-band, band])


# ------------------------------------------------------------------------------------------------ regression diagnostics


def residual_diagnostics(res: Result, y: np.ndarray, fitted: np.ndarray, resid: np.ndarray, exog: np.ndarray | None,
                         dates: list[Any] | None = None, time_ordered: bool = True, labels: list[Any] | None = None,
                         tests: bool = True) -> None:
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_breuschpagan, het_white
    from statsmodels.stats.stattools import durbin_watson, jarque_bera

    resid = np.asarray(resid, dtype=float)
    fitted = np.asarray(fitted, dtype=float)
    n = len(resid)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        if tests:
            _residual_tests(res, resid, exog, n, time_ordered)
    _residual_plots(res, fitted, resid, n, dates, time_ordered, labels)


def _residual_tests(res: Result, resid: np.ndarray, exog: np.ndarray | None, n: int, time_ordered: bool) -> None:
    from statsmodels.stats.diagnostic import acorr_ljungbox, het_breuschpagan, het_white
    from statsmodels.stats.stattools import durbin_watson, jarque_bera

    if True:
        if exog is not None and exog.shape[1] >= 2:
            try:
                lm, lmp, f, fp = het_breuschpagan(resid, exog)
                res.add_test("Breusch-Pagan", lm, lmp, exog.shape[1] - 1, "homoskedastic errors")
            except Exception as e:
                res.info("bp_failed", f"Breusch-Pagan not computed: {e}")
            if exog.shape[1] <= 12 and n > exog.shape[1] * (exog.shape[1] + 1) / 2 + 5:
                try:
                    lm, lmp, f, fp = het_white(resid, exog)
                    res.add_test("White", lm, lmp, None, "homoskedastic errors")
                except Exception as e:
                    res.info("white_failed", f"White test not computed: {e}")
            else:
                res.info("white_skipped", "White test skipped (too many regressors for the auxiliary regression)")
        dw = float(durbin_watson(resid))
        res.add_test("Durbin-Watson", dw, None, None, "no first-order autocorrelation", passed=1.5 <= dw <= 2.5,
                     conclusion="~2 = none; < 1.5 positive / > 2.5 negative autocorrelation" +
                     (" (time-ordered)" if time_ordered else " (rows not time-ordered: interpret with care)"))
        if time_ordered and n > 10:
            lags = int(min(10, max(1, n // 5)))
            lb = acorr_ljungbox(resid, lags=[lags], return_df=True)
            res.add_test(f"Ljung-Box (lag {lags})", lb["lb_stat"].iloc[0], lb["lb_pvalue"].iloc[0], lags,
                         "no autocorrelation up to lag")
        jb, jbp, skew, kurt = jarque_bera(resid)
        res.add_test("Jarque-Bera", jb, jbp, 2, "normal residuals", note=f"skew {skew:.2f}, kurtosis {kurt:.2f}")


def _residual_plots(res: Result, fitted: np.ndarray, resid: np.ndarray, n: int, dates: list[Any] | None,
                    time_ordered: bool, labels: list[Any] | None) -> None:
    idx = downsample_idx(n)
    res.line_plot("residuals_vs_fitted", "Residuals vs fitted",
                  [series("residuals", fitted[idx], resid[idx], labels=clean([labels[i] for i in idx]) if labels else None)],
                  "fitted", "residual", kind="scatter")
    if dates is not None and time_ordered:
        res.line_plot("residuals_time", "Residuals over time", [series("residual", [dates[i] for i in idx], resid[idx])],
                      "date", "residual")
    # QQ
    q_idx = downsample_idx(n, 400)
    srt = np.sort(resid)
    sd = srt.std(ddof=1) or 1.0
    theo = stats.norm.ppf((np.arange(1, n + 1) - 0.5) / n)
    res.line_plot("qq", "Normal QQ plot (standardized residuals)",
                  [series("residuals", theo[q_idx], ((srt - srt.mean()) / sd)[q_idx]),
                   series("45-degree", [theo[0], theo[-1]], [theo[0], theo[-1]])], "theoretical", "sample",
                  kind="scatter")
    hist, edges = np.histogram(resid[np.isfinite(resid)], bins=min(40, max(5, n // 10)))
    res.line_plot("residual_hist", "Residual distribution", [series("count", (edges[:-1] + edges[1:]) / 2, hist)],
                  "residual", "count", kind="bar")
    if time_ordered:
        acf_pacf_plot(res, "residual_acf", resid, min(24, n // 4), "Residual ACF / PACF")


# ------------------------------------------------------------------------------------------------ out-of-sample


def _metrics(actual: np.ndarray, pred: np.ndarray) -> dict[str, float | None]:
    e = actual - pred
    ok = np.isfinite(e)
    if not ok.any():
        return {"mae": None, "rmse": None, "mape": None, "n": 0}
    e, a = e[ok], actual[ok]
    nz = np.abs(a) > 1e-9
    return {"mae": float(np.mean(np.abs(e))), "rmse": float(np.sqrt(np.mean(e ** 2))),
            "mape": float(np.mean(np.abs(e[nz] / a[nz])) * 100) if nz.any() else None, "n": int(ok.sum())}


def rolling_origin(y: np.ndarray, forecast_fn: Callable[[int, int], np.ndarray], dates: list[Any] | None = None,
                   initial: int | None = None, horizon: int = 1, max_folds: int = 60, season: int = 0,
                   check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Expanding-window evaluation. forecast_fn(train_end, h) -> h forecasts for y[train_end : train_end + h]
    using information up to train_end (exclusive). Baselines: naive (last value), random walk with drift,
    historical mean, seasonal naive (if season > 1)."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    initial = int(initial or max(24, int(n * 0.6)))
    if n - initial < horizon + 1:
        return {"error": f"not enough observations for rolling-origin evaluation (n={n}, initial={initial})"}
    origins = list(range(initial, n - horizon + 1))
    if len(origins) > max_folds:
        origins = sorted(set(np.linspace(initial, n - horizon, max_folds).astype(int).tolist()))
    rows, model_p, actual, naive, drift, mean_b, snaive = [], [], [], [], [], [], []
    for t in origins:
        if check:
            check()
        try:
            f = np.asarray(forecast_fn(t, horizon), dtype=float)
        except Exception:
            f = np.full(horizon, np.nan)
        hist = y[:t]
        last = hist[-1]
        dr = (hist[-1] - hist[0]) / max(len(hist) - 1, 1)
        for h in range(horizon):
            a = y[t + h]
            actual.append(a)
            model_p.append(f[h] if h < len(f) else np.nan)
            naive.append(last)
            drift.append(last + dr * (h + 1))
            mean_b.append(np.nanmean(hist))
            snaive.append(hist[-season + (h % season)] if season > 1 and len(hist) >= season else np.nan)
            rows.append([dates[t + h] if dates is not None else t + h, h + 1, a, model_p[-1]])
    actual_a = np.array(actual)
    out = {"scheme": "expanding window", "initial_train": initial, "horizon": horizon, "n_folds": len(origins),
           "metrics": {"model": _metrics(actual_a, np.array(model_p)), "naive": _metrics(actual_a, np.array(naive)),
                       "random_walk_drift": _metrics(actual_a, np.array(drift)),
                       "historical_mean": _metrics(actual_a, np.array(mean_b))}}
    if season > 1:
        out["metrics"]["seasonal_naive"] = _metrics(actual_a, np.array(snaive))
    m, nv = out["metrics"]["model"]["rmse"], out["metrics"]["naive"]["rmse"]
    out["relative_rmse_vs_naive"] = (m / nv) if (m is not None and nv) else None
    hm = out["metrics"]["historical_mean"]["rmse"]
    out["oos_r2_vs_mean"] = (1 - (m ** 2) / (hm ** 2)) if (m is not None and hm) else None
    out["points"] = clean(rows[-500:])
    out["columns"] = ["date", "h", "actual", "forecast"]
    return clean(out)


def add_eval_metrics(res: Result, ev: dict[str, Any]) -> None:
    res.evaluation = ev
    if "metrics" in ev:
        mm = ev["metrics"]
        res.metrics.update({"oos_rmse": mm["model"]["rmse"], "oos_mae": mm["model"]["mae"],
                            "oos_mape": mm["model"]["mape"], "oos_rmse_naive": mm["naive"]["rmse"],
                            "oos_relative_rmse": ev.get("relative_rmse_vs_naive"),
                            "oos_r2": ev.get("oos_r2_vs_mean")})
        rows = [[k, v["mae"], v["rmse"], v["mape"], v["n"]] for k, v in mm.items()]
        res.add_table("oos_eval", f"Rolling-origin out-of-sample ({ev['scheme']}, h={ev['horizon']}, "
                      f"{ev['n_folds']} origins)", ["model", "MAE", "RMSE", "MAPE %", "n"], clean(rows))
        rel = ev.get("relative_rmse_vs_naive")
        if rel is not None and rel >= 1:
            res.warn("oos_worse_than_naive", f"out-of-sample RMSE is {rel:.2f}x the naive forecast: the model does "
                     "not beat a no-change forecast")
    elif "error" in ev:
        res.info("oos_skipped", ev["error"])
