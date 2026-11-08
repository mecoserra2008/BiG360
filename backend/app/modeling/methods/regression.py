"""Linear regression family: OLS / WLS / GLS (feasible GLS with AR errors), distributed-lag regression, quantile
regression (owner: model-studio)."""

from __future__ import annotations

import warnings as _w
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .. import diagnostics as dg
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, bh_adjust, clean, coef_rows, downsample_idx, series

CovType = Literal["nonrobust", "HC0", "HC1", "HC2", "HC3", "HAC", "cluster"]


class LinearParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_constant: bool = True
    cov_type: CovType = Field(default="HC1", description="nonrobust | HC0 (White) | HC1..HC3 | HAC (Newey-West) | cluster")
    hac_maxlags: int | None = Field(default=None, ge=0, le=60, description="HAC lags (default floor(4 (n/100)^(2/9)))")
    cluster_by: Literal["entity", "date", "both"] = Field(default="entity", description="cluster-robust groups (panel)")
    alpha: float = Field(default=0.05, gt=0, lt=0.5, description="confidence level 1 - alpha for intervals")
    rolling_eval: bool = Field(default=True, description="time series only: expanding-window 1-step out-of-sample evaluation")
    eval_initial: float = Field(default=0.6, gt=0.1, lt=0.95, description="share of the sample in the first training window")


class GLSParams(LinearParams):
    ar_order: int = Field(default=1, ge=1, le=12, description="AR order of the error process (GLSAR, iterated FGLS)")
    max_iter: int = Field(default=20, ge=1, le=200)


class DistLagParams(LinearParams):
    cov_type: CovType = "HAC"
    lags: int = Field(default=3, ge=0, le=36, description="max lag L applied to every x (0..L)")
    lag_overrides: dict[str, int] = Field(default_factory=dict, description="per-variable max lag")
    include_contemporaneous: bool = Field(default=True, description="include lag 0")
    y_lags: int = Field(default=0, ge=0, le=24, description="autoregressive lags of y (ADL model)")


class QuantileParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantiles: list[float] = Field(default=[0.1, 0.25, 0.5, 0.75, 0.9], min_length=1, max_length=19)
    add_constant: bool = True
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    max_iter: int = Field(default=2000, ge=50, le=20000)


# ------------------------------------------------------------------------------------------------ helpers


def default_hac_lags(n: int) -> int:
    return int(np.floor(4 * (n / 100.0) ** (2.0 / 9.0)))


def design(df: pd.DataFrame, x: list[str], add_constant: bool) -> tuple[np.ndarray, list[str]]:
    X = df[x].to_numpy(dtype=float) if x else np.empty((len(df), 0))
    names = list(x)
    if add_constant:
        X = np.column_stack([np.ones(len(df)), X])
        names = ["const"] + names
    return X, names


def cov_kwds(p: LinearParams, df: pd.DataFrame, data: FitData, n: int) -> tuple[str, dict[str, Any], str]:
    """statsmodels cov_type + kwds + human label."""
    if p.cov_type == "HAC":
        lags = p.hac_maxlags if p.hac_maxlags is not None else default_hac_lags(n)
        if data.is_panel:
            # On a panel, clustering by entity is robust to arbitrary within-entity serial correlation (HAC-like).
            g = pd.factorize(df["entity_id"])[0]
            return "cluster", {"groups": g}, "cluster(entity) (HAC requested on a panel)"
        return "HAC", {"maxlags": lags, "use_correction": True}, f"HAC Newey-West (lags {lags})"
    if p.cov_type == "cluster":
        if not data.is_panel:
            raise GuardrailError("cluster_needs_panel", "cluster-robust SEs need a panel / cross-section dataset")
        if p.cluster_by == "entity":
            g = pd.factorize(df["entity_id"])[0]
        elif p.cluster_by == "date":
            g = pd.factorize(df["date"])[0]
        else:
            g = np.column_stack([pd.factorize(df["entity_id"])[0], pd.factorize(df["date"])[0]])
        return "cluster", {"groups": g}, f"cluster({p.cluster_by})"
    return p.cov_type, {}, {"nonrobust": "classical", "HC0": "White (HC0)"}.get(p.cov_type, p.cov_type)


def _fit_cov(model: Any, ctype: str, kw: dict[str, Any]) -> Any:
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        return model.fit(cov_type=ctype, cov_kwds=kw or None)


def ols_core(res: Result, df: pd.DataFrame, data: FitData, y: str, x: list[str], p: LinearParams,
             weights: np.ndarray | None = None, gls: GLSParams | None = None) -> Any:
    import statsmodels.api as sm

    Y = df[y].to_numpy(dtype=float)
    X, names = design(df, x, p.add_constant)
    n, k = X.shape
    dg.check_obs(res, n, k)
    des = dg.check_design(res, X, names, p.add_constant)
    ctype, kw, label = cov_kwds(p, df, data, n)
    if gls is not None:
        if data.is_panel:
            raise GuardrailError("gls_needs_timeseries", "GLS with AR errors needs a time-series dataset")
        model = sm.GLSAR(Y, X, rho=gls.ar_order)
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            r = model.iterative_fit(maxiter=gls.max_iter)
            if ctype != "nonrobust":
                r = model.fit(cov_type=ctype, cov_kwds=kw or None)
        res.fit_stats["rho"] = clean(np.atleast_1d(model.rho).tolist())
    elif weights is not None:
        if np.any(weights <= 0):
            raise GuardrailError("bad_weights", "WLS weights must be strictly positive")
        r = _fit_cov(sm.WLS(Y, X, weights=weights), ctype, kw)
    else:
        r = _fit_cov(sm.OLS(Y, X), ctype, kw)
    ci = np.asarray(r.conf_int(alpha=p.alpha))
    rows = coef_rows(names, r.params, r.bse, r.tvalues, r.pvalues, ci)
    adj = bh_adjust([row["p_value"] for row in rows if row["term"] != "const"])
    j = 0
    vif_map = {v["term"]: v["vif"] for v in des.get("vif", [])}
    for row in rows:
        if row["term"] != "const":
            row["p_value_adj"] = adj[j]
            j += 1
        row["vif"] = vif_map.get(row["term"])
    res.coefficients = rows
    dg.multiple_testing_note(res, len(x))
    res.summary.update({"dependent": y, "n_obs": n, "n_params": k, "cov_type": label,
                        "estimator": "GLSAR (iterated FGLS)" if gls else ("WLS" if weights is not None else "OLS")})
    fs = {"r2": r.rsquared, "adj_r2": r.rsquared_adj, "f_stat": getattr(r, "fvalue", None),
          "f_pvalue": getattr(r, "f_pvalue", None), "aic": r.aic, "bic": r.bic, "llf": r.llf,
          "sigma": float(np.sqrt(r.scale)), "df_resid": r.df_resid,
          "condition_number": des.get("condition_number")}
    res.fit_stats.update({k2: clean(v) for k2, v in fs.items()})
    res.metrics.update({"r2": fs["r2"], "adj_r2": fs["adj_r2"], "aic": fs["aic"], "bic": fs["bic"], "n_obs": n,
                        "rmse_in_sample": float(np.sqrt(np.mean(np.asarray(r.resid) ** 2)))})
    time_ordered = not data.is_panel
    dates = df["date"].tolist()
    labels = df["entity_label"].tolist() if data.is_panel else None
    dg.residual_diagnostics(res, Y, np.asarray(r.fittedvalues), np.asarray(r.resid), X, dates, time_ordered, labels)
    if time_ordered:
        idx = downsample_idx(n)
        res.line_plot("fit", "Actual vs fitted", [series("actual", [dates[i] for i in idx], Y[idx]),
                                                    series("fitted", [dates[i] for i in idx], np.asarray(r.fittedvalues)[idx])],
                      "date", y)
        dg.nonstationarity_check(res, df, y, x)
    res.line_plot("coef_ci", f"Coefficients with {int((1 - p.alpha) * 100)}% CI",
                  [series("estimate", [r_["term"] for r_ in rows if r_["term"] != "const"],
                          [r_["estimate"] for r_ in rows if r_["term"] != "const"],
                          ci_low=[r_["ci_low"] for r_ in rows if r_["term"] != "const"],
                          ci_high=[r_["ci_high"] for r_ in rows if r_["term"] != "const"])], "term", "estimate",
                  kind="errorbar")
    return r


def ts_rolling_eval(res: Result, df: pd.DataFrame, y: str, x: list[str], add_constant: bool, initial: float,
                    data: FitData) -> None:
    import statsmodels.api as sm

    Y = df[y].to_numpy(dtype=float)
    X, _ = design(df, x, add_constant)

    def fc(t: int, h: int) -> np.ndarray:
        b = np.linalg.lstsq(X[:t], Y[:t], rcond=None)[0]
        return X[t:t + h] @ b

    ev = dg.rolling_origin(Y, fc, df["date"].tolist(), initial=max(int(len(Y) * initial), X.shape[1] + 5),
                           horizon=1, check=data.check)
    ev["note"] = "1-step conditional forecasts: regressors at t are taken as known (use lagged regressors for a true forecast)"
    dg.add_eval_metrics(res, ev)
    _ = sm  # keep import for type parity


# ------------------------------------------------------------------------------------------------ methods


@register
class OLS(Method):
    id = "ols"
    family = "regression"
    title = "OLS regression"
    description = "Ordinary least squares with classical, White/HC, Newey-West HAC or cluster-robust standard errors."
    min_x = 1
    Params = LinearParams
    outputs = ("coefficients", "fit_stats", "residual plots", "rolling-origin OOS (time series)")
    diagnostics = ("Breusch-Pagan", "White", "Durbin-Watson", "Ljung-Box", "Jarque-Bera", "VIF", "condition number",
                   "ADF/KPSS screen")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.15 + n_rows * (n_x + 1) * 3e-7 + (0.3 if getattr(params, "rolling_eval", False) else 0)

    def fit(self, data: FitData, params: LinearParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        w = df[data.weights].to_numpy(dtype=float) if data.weights else None
        ols_core(res, df, data, data.y, data.x, params, weights=w)
        if not data.is_panel and params.rolling_eval:
            ts_rolling_eval(res, df, data.y, data.x, params.add_constant, params.eval_initial, data)
        return res


@register
class WLS(OLS):
    id = "wls"
    title = "WLS regression"
    description = "Weighted least squares (weights column = inverse variance or any positive weight)."
    uses_weights = True

    def validate_roles(self, study: Any) -> None:
        super().validate_roles(study)
        if not study.weights:
            raise GuardrailError("missing_weights", "WLS needs a weights column")


@register
class GLS(Method):
    id = "gls"
    family = "regression"
    title = "GLS regression (AR errors)"
    description = "Feasible GLS with AR(p) errors (statsmodels GLSAR, iterated). Time series only."
    shapes = ("timeseries",)
    min_x = 1
    Params = GLSParams
    outputs = OLS.outputs
    diagnostics = OLS.diagnostics

    def fit(self, data: FitData, params: GLSParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        ols_core(res, df, data, data.y, data.x, params, gls=params)
        if params.rolling_eval:
            ts_rolling_eval(res, df, data.y, data.x, params.add_constant, params.eval_initial, data)
        return res


def add_lags(data: FitData, cols: list[str], lag_map: dict[str, list[int]]) -> tuple[pd.DataFrame, list[str], dict[str, list[str]]]:
    """Lagged copies (within entity for panels) on the full frame, then listwise deletion."""
    import polars as pl

    f = data.frame
    over = "entity_id" if data.is_panel else None
    blocks: dict[str, list[str]] = {}
    exprs = []
    for v, lags in lag_map.items():
        names = []
        for L in lags:
            nm = v if L == 0 else f"{v}_L{L}"
            if L > 0:
                e = pl.col(v).shift(L)
                exprs.append((e.over(over) if over else e).alias(nm))
            names.append(nm)
        blocks[v] = names
    if exprs:
        f = f.with_columns(exprs)
    allx = [c for b in blocks.values() for c in b]
    sub = FitData(f, data.meta, data.y, allx, data.weights, data.seed, data.check)
    df, _ = sub.complete()
    return df, allx, blocks


@register
class DistributedLag(Method):
    id = "distributed_lag"
    family = "regression"
    title = "Distributed-lag regression (ADL)"
    description = ("y on x_t, x_{t-1}, ..., x_{t-L} (optionally y lags). Joint F/Wald tests per lag block and the "
                   "long-run multiplier (sum of lag coefficients) with its standard error.")
    shapes = ("timeseries", "panel")
    min_x = 1
    Params = DistLagParams
    outputs = ("coefficients", "lag profiles", "joint lag-block tests", "long-run multipliers", "residual plots")
    diagnostics = OLS.diagnostics

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + n_rows * (n_x * (getattr(params, "lags", 3) + 1)) * 3e-7 + 0.3

    def fit(self, data: FitData, params: DistLagParams) -> Result:
        res = Result(self.id, self.family, self.title)
        lag_map: dict[str, list[int]] = {}
        for v in data.x:
            L = params.lag_overrides.get(v, params.lags)
            lag_map[v] = list(range(0 if params.include_contemporaneous else 1, L + 1))
            if not lag_map[v]:
                raise GuardrailError("no_lags", f"{v}: empty lag set")
        ylag = {data.y: list(range(1, params.y_lags + 1))} if params.y_lags else {}
        df, allx, blocks = add_lags(data, data.x, {**lag_map, **ylag})
        res.summary["lag_structure"] = {k: v for k, v in blocks.items()}
        r = ols_core(res, df, data, data.y, allx, params)
        names = (["const"] if params.add_constant else []) + allx
        rows = []
        for v, cols in blocks.items():
            idx = [names.index(c) for c in cols]
            R = np.zeros((len(idx), len(names)))
            for i, j in enumerate(idx):
                R[i, j] = 1.0
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                wt = r.wald_test(R, scalar=True, use_f=True)
                s = np.zeros(len(names))
                s[idx] = 1.0
                tt = r.t_test(s)
            stat = float(np.squeeze(wt.statistic))
            pv = float(np.squeeze(wt.pvalue))
            lr, lr_se = float(np.squeeze(tt.effect)), float(np.squeeze(tt.sd))
            lr_p = float(np.squeeze(tt.pvalue))
            role = "y (autoregressive)" if v == data.y else "x"
            res.add_test(f"Joint lags of {v} = 0", stat, pv, len(idx), f"all {len(idx)} lag coefficients of {v} are zero",
                         group="lag_blocks")
            rows.append([v, role, len(idx), stat, pv, lr, lr_se, lr_p])
            co = [c for c in res.coefficients if c["term"] in cols]
            lags = [0 if c["term"] == v else int(c["term"].rsplit("_L", 1)[1]) for c in co]
            res.line_plot(f"lag_profile_{v}", f"Lag profile: {v}",
                          [series("estimate", lags, [c["estimate"] for c in co],
                                  ci_low=[c["ci_low"] for c in co], ci_high=[c["ci_high"] for c in co])],
                          "lag", "coefficient", kind="errorbar")
        res.add_table("lag_blocks", "Lag blocks: joint test and long-run multiplier (sum of lag coefficients)",
                      ["variable", "role", "n_lags", "F", "p_value", "long_run", "long_run_se", "long_run_p"],
                      clean(rows))
        dg.multiple_testing_note(res, len(blocks), "lag blocks")
        if not data.is_panel and params.rolling_eval:
            ts_rolling_eval(res, df, data.y, allx, params.add_constant, params.eval_initial, data)
        return res


@register
class Quantile(Method):
    id = "quantile"
    family = "regression"
    title = "Quantile regression"
    description = "Conditional quantiles (Koenker-Bassett) at several quantiles; coefficient process vs quantile, OLS for reference."
    min_x = 1
    Params = QuantileParams
    outputs = ("coefficients by quantile", "coefficient process plot", "pseudo R2 by quantile")
    diagnostics = ("pseudo R2 (Koenker-Machado)", "OLS comparison")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + len(getattr(params, "quantiles", [0.5])) * n_rows * (n_x + 1) * 2e-6

    def fit(self, data: FitData, params: QuantileParams) -> Result:
        import statsmodels.api as sm

        res = Result(self.id, self.family, self.title)
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        Y = df[data.y].to_numpy(dtype=float)
        X, names = design(df, data.x, params.add_constant)
        dg.check_obs(res, len(Y), X.shape[1])
        dg.check_design(res, X, names, params.add_constant)
        rows, r2 = [], []
        proc: dict[str, dict[str, list[Any]]] = {n: {"q": [], "est": [], "lo": [], "hi": []} for n in names}
        model = sm.QuantReg(Y, X)
        for q in sorted(params.quantiles):
            if not 0 < q < 1:
                raise GuardrailError("bad_quantile", f"quantile {q} not in (0, 1)")
            data.check()
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                r = model.fit(q=q, max_iter=params.max_iter)
            ci = np.asarray(r.conf_int(alpha=params.alpha))
            for row in coef_rows(names, r.params, r.bse, r.tvalues, r.pvalues, ci, quantile=q):
                rows.append(row)
                d = proc[row["term"]]
                d["q"].append(q)
                d["est"].append(row["estimate"])
                d["lo"].append(row["ci_low"])
                d["hi"].append(row["ci_high"])
            r2.append([q, clean(r.prsquared)])
        adj = bh_adjust([r_["p_value"] for r_ in rows if r_["term"] != "const"])
        j = 0
        for r_ in rows:
            if r_["term"] != "const":
                r_["p_value_adj"] = adj[j]
                j += 1
        res.coefficients = rows
        ols = sm.OLS(Y, X).fit()
        for n_, d in proc.items():
            if n_ == "const":
                continue
            res.line_plot(f"process_{n_}", f"Quantile process: {n_}",
                          [series("estimate", d["q"], d["est"], ci_low=d["lo"], ci_high=d["hi"]),
                           series("OLS", [d["q"][0], d["q"][-1]], [ols.params[names.index(n_)]] * 2)],
                          "quantile", "coefficient", kind="errorbar")
        res.add_table("pseudo_r2", "Pseudo R2 by quantile", ["quantile", "pseudo_r2"], r2)
        res.summary.update({"dependent": data.y, "n_obs": len(Y), "quantiles": sorted(params.quantiles)})
        res.fit_stats.update({"ols_r2": clean(ols.rsquared)})
        med = [x for x in r2 if abs(x[0] - 0.5) < 1e-9]
        res.metrics.update({"n_obs": len(Y), "pseudo_r2_median": med[0][1] if med else r2[len(r2) // 2][1]})
        dg.multiple_testing_note(res, len(data.x) * len(params.quantiles), "quantile coefficients")
        return res
