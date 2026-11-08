"""Panel econometrics: fixed / random effects (linearmodels) with Hausman test, Fama-MacBeth, IV / 2SLS
(owner: model-studio)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from .. import _algos as A
from .. import diagnostics as dg
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, bh_adjust, clean, coef_rows, series


def lm_coefs(r: Any, alpha: float = 0.05) -> list[dict[str, Any]]:
    ci = r.conf_int(level=1 - alpha)
    rows = coef_rows(list(r.params.index), r.params, r.std_errors, r.tstats, r.pvalues, ci.to_numpy())
    adj = bh_adjust([x["p_value"] for x in rows if x["term"] != "const"])
    j = 0
    for x in rows:
        if x["term"] != "const":
            x["p_value_adj"] = adj[j]
            j += 1
    return rows


class PanelParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    estimator: Literal["fe", "re", "pooled", "between", "first_diff"] = "fe"
    entity_effects: bool = True
    time_effects: bool = False
    cov_type: Literal["unadjusted", "robust", "clustered", "kernel"] = Field(
        default="clustered", description="kernel = Driscoll-Kraay (robust to cross-sectional and serial correlation)")
    cluster_entity: bool = True
    cluster_time: bool = False
    kernel_bandwidth: int | None = Field(default=None, ge=1, le=60)
    hausman: bool = Field(default=True, description="also fit FE and RE (entity effects) and run the Hausman test")
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class Panel(Method):
    id = "panel"
    family = "panel"
    title = "Panel regression (FE / RE / pooled / between / first-difference)"
    description = ("linearmodels PanelOLS / RandomEffects / PooledOLS / BetweenOLS / FirstDifferenceOLS with entity and "
                   "time effects; clustered (entity / time / two-way) or Driscoll-Kraay standard errors; Hausman FE vs RE.")
    shapes = ("panel",)
    min_x = 1
    Params = PanelParams
    outputs = ("coefficients", "R2 within / between / overall", "Hausman test", "F-test for poolability")
    diagnostics = ("Hausman", "F poolability", "VIF")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.3 + n_rows * (n_x + 1) * 2e-6 * (3 if params.hausman else 1)

    def fit(self, data: FitData, p: PanelParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        X = df[data.x].to_numpy(float)
        dg.check_obs(res, len(df), len(data.x) + 1)
        dg.check_design(res, np.column_stack([np.ones(len(df)), X]), ["const"] + data.x)
        r = A.panel_fit(df, data.y, data.x, p.estimator, p.entity_effects, p.time_effects, p.cov_type,
                        p.cluster_entity, p.cluster_time, p.kernel_bandwidth)
        res.coefficients = lm_coefs(r, p.alpha)
        res.title = {"fe": "Fixed effects", "re": "Random effects", "pooled": "Pooled OLS", "between": "Between",
                     "first_diff": "First differences"}[p.estimator] + " panel regression"
        fs = {"r2": float(r.rsquared), "n_obs": int(r.nobs), "n_entities": int(r.entity_info["total"]),
              "n_periods": int(r.time_info["total"])}
        for k in ("rsquared_within", "rsquared_between", "rsquared_overall"):
            if hasattr(r, k):
                fs[k.replace("rsquared", "r2")] = float(getattr(r, k))
        res.fit_stats.update(clean(fs))
        if hasattr(r, "f_pooled") and r.f_pooled is not None:
            fp = r.f_pooled
            res.add_test("F-test for poolability (effects = 0)", fp.stat, fp.pval, fp.df, "no entity/time effects",
                         group="specification", passed=None)
        if p.hausman and p.estimator in ("fe", "re"):
            data.check()
            fe = A.panel_fit(df, data.y, data.x, "fe", True, False, "unadjusted")
            re = A.panel_fit(df, data.y, data.x, "re", False, False, "unadjusted")
            stat, k, pv = A.hausman(fe, re)
            res.add_test("Hausman (FE vs RE)", stat, pv, k, "random effects are consistent (RE preferred)",
                         group="specification", passed=None,
                         conclusion="use fixed effects" if pv < 0.05 else "random effects not rejected")
            res.fit_stats["hausman_p"] = clean(pv)
        res.summary.update({"dependent": data.y, "estimator": p.estimator, "entity_effects": p.entity_effects,
                            "time_effects": p.time_effects, "cov_type": p.cov_type, "n_obs": int(r.nobs)})
        res.metrics.update(clean({"r2": fs["r2"], "r2_within": fs.get("r2_within"), "n_obs": int(r.nobs)}))
        resid = np.asarray(r.resids, float)
        fitted = np.asarray(r.fitted_values, float).ravel()
        dg.residual_diagnostics(res, df[data.y].to_numpy(float), fitted, resid, None, None, False,
                                df["entity_label"].tolist(), tests=False)
        dg.multiple_testing_note(res, len(data.x))
        return res


class FMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nw_lags: int | None = Field(default=None, ge=0, le=36, description="Newey-West lags on the lambda series (default 4(T/100)^(2/9))")
    min_obs: int | None = Field(default=None, ge=3, description="min firms per cross-section")


@register
class FamaMacBeth(Method):
    id = "fama_macbeth"
    family = "panel"
    title = "Fama-MacBeth regression"
    description = ("Cross-sectional OLS at every date; the time-series means of the slopes (risk premia) with "
                   "Newey-West standard errors on the lambda series; average cross-sectional R2.")
    shapes = ("panel",)
    min_x = 1
    Params = FMParams
    outputs = ("premia with NW SEs", "lambda time series", "average R2", "share of positive lambdas")
    diagnostics = ("Newey-West t", "lambda autocorrelation")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.3 + n_rows * 2e-6

    def fit(self, data: FitData, p: FMParams) -> Result:
        res = Result(self.id, self.family, self.title)
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        try:
            fm = A.fama_macbeth(df, data.y, data.x, p.nw_lags, p.min_obs)
        except ValueError as e:
            raise GuardrailError("too_few_obs", str(e)) from e
        tb = fm["table"]
        res.coefficients = clean(tb.drop(columns=["share_positive"]).to_dict("records"))
        adj = bh_adjust([r["p_value"] for r in res.coefficients if r["term"] != "const"])
        j = 0
        for r, sp in zip(res.coefficients, tb["share_positive"], strict=True):
            r["share_positive"] = clean(sp)
            if r["term"] != "const":
                r["p_value_adj"] = adj[j]
                j += 1
        L = fm["lambdas"]
        dates = list(L.index)
        res.line_plot("lambdas", "Cross-sectional slopes (lambda_t)",
                      [series(c, dates, L[c].to_numpy()) for c in L.columns if c != "const"], "date", "slope")
        res.line_plot("lambdas_cum", "Cumulative lambdas", [series(c, dates, L[c].cumsum().to_numpy())
                                                           for c in L.columns if c != "const"], "date", "cumulative")
        for c in L.columns:
            if c == "const" or len(L) < 12:
                continue
            ac = float(pd.Series(L[c].to_numpy()).autocorr(1))
            res.add_test(f"lambda autocorrelation lag 1: {c}", ac, None, None, None, group="lambda_series",
                         conclusion="persistent: rely on the NW SEs" if abs(ac) > 0.2 else "weak", passed=None)
        avg_r2 = float(fm["r2"].mean())
        res.fit_stats.update({"avg_r2": avg_r2, "periods": fm["periods"], "avg_n_per_period": float(fm["n"].mean()),
                              "nw_lags": fm["nw_lags"]})
        res.summary.update({"dependent": data.y, "n_obs": int(fm["n"].sum()), "periods": fm["periods"]})
        res.metrics.update({"avg_r2": avg_r2, "n_obs": int(fm["n"].sum()), "periods": fm["periods"]})
        dg.multiple_testing_note(res, len(data.x), "premia")
        if fm["periods"] < 36:
            res.warn("short_lambda_series", f"only {fm['periods']} cross-sections: premia are imprecise")
        return res


class IVParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endog: list[str] = Field(min_length=1, description="endogenous regressors (dataset columns)")
    instruments: list[str] = Field(min_length=1, description="excluded instruments (dataset columns)")
    estimator: Literal["2sls", "liml", "gmm"] = "2sls"
    cov_type: Literal["unadjusted", "robust", "clustered", "kernel"] = "robust"
    alpha: float = Field(default=0.05, gt=0, lt=0.5)


@register
class IV2SLS(Method):
    id = "iv2sls"
    family = "regression"
    title = "Instrumental variables (2SLS / LIML / GMM)"
    description = ("linearmodels IV estimation: y on exogenous x (role x) and endogenous regressors (params.endog) "
                   "instrumented by params.instruments. First-stage F / partial R2, Sargan / Hansen J "
                   "overidentification, Wu-Hausman endogeneity test.")
    min_x = 0
    Params = IVParams
    outputs = ("coefficients", "first-stage diagnostics", "overidentification", "endogeneity test")
    diagnostics = ("first-stage F", "Sargan / Hansen J", "Wu-Hausman")

    def extra_columns(self, params: IVParams) -> list[str]:
        return list(params.endog) + list(params.instruments)

    def regressor_columns(self, params: IVParams) -> list[str]:
        return list(params.endog) + list(params.instruments)

    def fit(self, data: FitData, p: IVParams) -> Result:
        res = Result(self.id, self.family, f"IV ({p.estimator.upper()})")
        if len(p.instruments) < len(p.endog):
            raise GuardrailError("underidentified", "need at least as many instruments as endogenous regressors")
        df, dropped = data.complete(p.endog + p.instruments)
        res.summary["rows_dropped_missing"] = dropped
        dg.check_obs(res, len(df), len(data.x) + len(p.endog) + 1)
        cl = pd.factorize(df["entity_id"])[0] if (p.cov_type == "clustered" and data.is_panel) else None
        if p.cov_type == "clustered" and cl is None:
            raise GuardrailError("cluster_needs_panel", "clustered SEs need a panel dataset")
        r = A.iv_fit(df, data.y, list(data.x), p.endog, p.instruments, p.estimator, p.cov_type, cl)
        res.coefficients = lm_coefs(r, p.alpha)
        fsd = r.first_stage.diagnostics
        rows = []
        for en, row in fsd.iterrows():
            f = float(row.get("f.stat", np.nan))
            rows.append([en, float(row.get("rsquared", np.nan)), float(row.get("partial.rsquared", np.nan)), f,
                         float(row.get("f.pval", np.nan))])
            res.add_test(f"First-stage F ({en})", f, row.get("f.pval"), None, "instruments irrelevant",
                         group="instruments", passed=bool(f >= 10), conclusion="strong (F >= 10)" if f >= 10 else "weak instruments")
            if f < 10:
                res.warn("weak_instruments", f"first-stage F for {en} is {f:.1f} < 10: weak instruments bias 2SLS towards OLS")
        res.add_table("first_stage", "First stage", ["endogenous", "r2", "partial_r2", "F", "p_value"], clean(rows))
        if len(p.instruments) > len(p.endog):
            try:
                ov = r.j_stat if p.estimator == "gmm" else r.sargan
                res.add_test("Hansen J" if p.estimator == "gmm" else "Sargan", ov.stat, ov.pval, ov.df,
                             "instruments valid (overidentifying restrictions hold)", group="diagnostics")
            except Exception as e:  # pragma: no cover
                res.info("overid_failed", f"overidentification test unavailable: {e}")
        else:
            res.info("just_identified", "just identified: no overidentification test")
        try:
            wh = r.wu_hausman()
            res.add_test("Wu-Hausman", wh.stat, wh.pval, wh.df, "regressors exogenous (OLS consistent)",
                         group="specification", passed=None)
        except Exception:
            pass
        res.fit_stats.update(clean({"r2": float(r.rsquared), "n_obs": int(r.nobs)}))
        res.summary.update({"dependent": data.y, "exog": list(data.x), "endog": p.endog, "instruments": p.instruments,
                            "n_obs": int(r.nobs), "cov_type": p.cov_type})
        res.metrics.update({"r2": float(r.rsquared), "n_obs": int(r.nobs)})
        return res
