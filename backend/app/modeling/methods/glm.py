"""Generalized linear models: gaussian, binomial (logit/probit), poisson, gamma, negative binomial (owner: model-studio)."""

from __future__ import annotations

import warnings as _w
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .. import diagnostics as dg
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, bh_adjust, clean, coef_rows, downsample_idx, series
from .regression import cov_kwds, design


class GLMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Literal["gaussian", "binomial", "poisson", "gamma", "negative_binomial"] = "gaussian"
    link: Literal["default", "identity", "log", "logit", "probit", "cloglog", "inverse_power"] = "default"
    nb_alpha: float | None = Field(default=None, gt=0, description="negative binomial dispersion; null = estimate (NB2 MLE)")
    add_constant: bool = True
    cov_type: Literal["nonrobust", "HC0", "HC1", "HC3", "HAC", "cluster"] = "HC1"
    hac_maxlags: int | None = Field(default=None, ge=0, le=60)
    cluster_by: Literal["entity", "date", "both"] = "entity"
    alpha: float = Field(default=0.05, gt=0, lt=0.5)
    offset: str | None = Field(default=None, description="offset column (e.g. log exposure) for count models")
    max_iter: int = Field(default=100, ge=10, le=1000)


def _family(p: GLMParams, alpha_nb: float | None):
    import statsmodels.api as sm
    from statsmodels.genmod.families import links as L

    link = {"identity": L.Identity(), "log": L.Log(), "logit": L.Logit(), "probit": L.Probit(),
            "cloglog": L.CLogLog(), "inverse_power": L.InversePower()}.get(p.link)
    fam = {"gaussian": sm.families.Gaussian, "binomial": sm.families.Binomial, "poisson": sm.families.Poisson,
           "gamma": sm.families.Gamma}.get(p.family)
    if p.family == "negative_binomial":
        return sm.families.NegativeBinomial(link=link, alpha=alpha_nb) if link else sm.families.NegativeBinomial(alpha=alpha_nb)
    return fam(link=link) if link else fam()


@register
class GLM(Method):
    id = "glm"
    family = "regression"
    title = "Generalized linear model"
    description = ("GLM with gaussian, binomial (logit/probit), poisson, gamma or negative-binomial family. Average "
                   "marginal effects; classification metrics for binomial; overdispersion check for counts.")
    min_x = 1
    uses_weights = True
    Params = GLMParams
    outputs = ("coefficients", "exp(coef) (odds / incidence-rate ratios)", "average marginal effects", "fit stats",
               "ROC / calibration (binomial)")
    diagnostics = ("deviance & Pearson chi2", "overdispersion (counts)", "AUC / Brier / log-loss (binomial)", "VIF")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + n_rows * (n_x + 1) * 1.5e-6

    def fit(self, data: FitData, params: GLMParams) -> Result:
        import statsmodels.api as sm

        res = Result(self.id, self.family, f"GLM ({params.family})")
        extra = [params.offset] if params.offset else []
        df, dropped = data.complete(extra)
        res.summary["rows_dropped_missing"] = dropped
        Y = df[data.y].to_numpy(dtype=float)
        if params.family == "binomial":
            if not np.all((Y >= 0) & (Y <= 1)):
                raise GuardrailError("bad_y", "binomial family needs y in [0, 1] (use the binarize transform)")
            if Y.min() == Y.max():
                raise GuardrailError("bad_y", "binomial y has a single class")
        if params.family in ("poisson", "negative_binomial"):
            if np.any(Y < 0):
                raise GuardrailError("bad_y", "count models need y >= 0")
            if np.any(np.abs(Y - np.round(Y)) > 1e-9):
                res.warn("non_integer_counts", "y has non-integer values; a count model treats them as counts")
        if params.family == "gamma" and np.any(Y <= 0):
            raise GuardrailError("bad_y", "gamma family needs y > 0")
        X, names = design(df, data.x, params.add_constant)
        n, k = X.shape
        dg.check_obs(res, n, k)
        des = dg.check_design(res, X, names, params.add_constant)
        off = df[params.offset].to_numpy(dtype=float) if params.offset else None
        w = df[data.weights].to_numpy(dtype=float) if data.weights else None
        alpha_nb = params.nb_alpha
        if params.family == "negative_binomial" and alpha_nb is None:
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                nb = sm.NegativeBinomial(Y, X, offset=off).fit(disp=0, maxiter=params.max_iter)
            alpha_nb = float(nb.params[-1])
            res.fit_stats["nb_alpha_mle"] = clean(alpha_nb)
            res.fit_stats["nb_alpha_se"] = clean(nb.bse[-1])
        lp = type("P", (), {"cov_type": params.cov_type, "hac_maxlags": params.hac_maxlags,
                            "cluster_by": params.cluster_by})()
        ctype, kw, label = cov_kwds(lp, df, data, n)
        model = sm.GLM(Y, X, family=_family(params, alpha_nb), offset=off, freq_weights=w)
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            r = model.fit(cov_type=ctype, cov_kwds=kw or None, maxiter=params.max_iter)
        if hasattr(r, "converged") and not r.converged:
            res.warn("not_converged", "IRLS did not converge; results unreliable")
        ci = np.asarray(r.conf_int(alpha=params.alpha))
        rows = coef_rows(names, r.params, r.bse, r.tvalues, r.pvalues, ci)
        link_name = type(model.family.link).__name__.lower()
        expo = link_name in ("logit", "log")
        # average marginal effects dmu/dx = mean(dmu/deta) * beta
        eta = X @ r.params + (off if off is not None else 0)
        with np.errstate(all="ignore"):
            dmu = np.asarray(model.family.link.inverse_deriv(eta), dtype=float)
        ame_scale = float(np.nanmean(dmu))
        adj = bh_adjust([row["p_value"] for row in rows if row["term"] != "const"])
        j = 0
        vif_map = {v["term"]: v["vif"] for v in des.get("vif", [])}
        for row in rows:
            if expo and row["estimate"] is not None:
                row["exp_estimate"] = clean(np.exp(row["estimate"]))
                row["exp_ci_low"] = clean(np.exp(row["ci_low"])) if row["ci_low"] is not None else None
                row["exp_ci_high"] = clean(np.exp(row["ci_high"])) if row["ci_high"] is not None else None
            if row["term"] != "const":
                row["p_value_adj"] = adj[j]
                row["marginal_effect"] = clean(ame_scale * row["estimate"]) if row["estimate"] is not None else None
                row["vif"] = vif_map.get(row["term"])
                j += 1
        res.coefficients = rows
        dg.multiple_testing_note(res, len(data.x))
        mu = np.asarray(r.fittedvalues, dtype=float)
        null_dev = float(r.null_deviance)
        fs = {"deviance": r.deviance, "null_deviance": null_dev, "pearson_chi2": r.pearson_chi2, "aic": r.aic,
              "bic": getattr(r, "bic_llf", None), "llf": r.llf, "df_resid": r.df_resid,
              "pseudo_r2_mcfadden": 1 - r.deviance / null_dev if null_dev else None,
              "condition_number": des.get("condition_number")}
        try:
            fs["pseudo_r2_cs"] = float(r.pseudo_rsquared(kind="cs"))
        except Exception:
            pass
        res.fit_stats.update(clean(fs))
        res.summary.update({"dependent": data.y, "n_obs": n, "n_params": k, "family": params.family,
                            "link": link_name, "cov_type": label,
                            "interpretation": ("exp_estimate = odds ratio" if link_name == "logit" else
                                               "exp_estimate = incidence-rate / multiplicative effect" if link_name == "log"
                                               else "coefficients on the link scale")})
        res.metrics.update({"n_obs": n, "aic": clean(r.aic), "deviance": clean(r.deviance),
                            "pseudo_r2": clean(fs["pseudo_r2_mcfadden"])})
        if params.family in ("poisson", "negative_binomial"):
            disp = float(r.pearson_chi2 / max(r.df_resid, 1))
            res.add_test("Pearson dispersion (chi2/df)", disp, None, r.df_resid, "dispersion = 1 (equidispersion)",
                         conclusion="overdispersed" if disp > 1.5 else ("underdispersed" if disp < 0.67 else "ok"),
                         passed=0.67 <= disp <= 1.5)
            res.fit_stats["dispersion"] = clean(disp)
            if params.family == "poisson" and disp > 1.5:
                res.warn("overdispersion", f"Pearson dispersion {disp:.2f} > 1.5: Poisson SEs are too small; use "
                         "family=negative_binomial or robust (HC) standard errors")
            zeros_obs = float(np.mean(Y == 0))
            zeros_pred = float(np.mean(np.exp(-mu))) if params.family == "poisson" else None
            res.fit_stats.update({"share_zeros_observed": zeros_obs, "share_zeros_poisson": clean(zeros_pred)})
        if params.family == "binomial":
            self._classification(res, Y, mu)
        resid = np.asarray(r.resid_deviance, dtype=float)
        idx = downsample_idx(n)
        res.line_plot("residuals_vs_fitted", "Deviance residuals vs fitted mean",
                      [series("deviance residual", mu[idx], resid[idx])], "fitted mean", "deviance residual",
                      kind="scatter")
        if not data.is_panel:
            dates = df["date"].tolist()
            res.line_plot("fit", "Actual vs fitted mean", [series("actual", [dates[i] for i in idx], Y[idx]),
                                                         series("fitted", [dates[i] for i in idx], mu[idx])], "date", data.y)
            dg.acf_pacf_plot(res, "residual_acf", resid, min(24, n // 4), "Deviance-residual ACF / PACF")
        res.line_plot("coef_ci", f"Coefficients with {int((1 - params.alpha) * 100)}% CI (link scale)",
                      [series("estimate", [c["term"] for c in rows if c["term"] != "const"],
                              [c["estimate"] for c in rows if c["term"] != "const"],
                              ci_low=[c["ci_low"] for c in rows if c["term"] != "const"],
                              ci_high=[c["ci_high"] for c in rows if c["term"] != "const"])], "term", "estimate",
                      kind="errorbar")
        return res

    @staticmethod
    def _classification(res: Result, y: np.ndarray, p: np.ndarray) -> None:
        from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score, roc_curve

        p = np.clip(p, 1e-9, 1 - 1e-9)
        yb = (y > 0.5).astype(int)
        auc = float(roc_auc_score(yb, p)) if len(np.unique(yb)) == 2 else None
        acc = float(np.mean((p > 0.5) == yb))
        base = float(max(yb.mean(), 1 - yb.mean()))
        cls = {"auc": auc, "accuracy": acc, "accuracy_baseline": base, "brier": float(brier_score_loss(yb, p)),
               "log_loss": float(log_loss(yb, p, labels=[0, 1])), "base_rate": float(yb.mean())}
        res.fit_stats.update(clean(cls))
        res.metrics.update(clean({"auc": auc, "brier": cls["brier"], "accuracy": acc}))
        if auc is not None:
            fpr, tpr, _ = roc_curve(yb, p)
            ix = downsample_idx(len(fpr), 300)
            res.line_plot("roc", f"ROC curve (AUC {auc:.3f})", [series("model", fpr[ix], tpr[ix]),
                                                                  series("chance", [0, 1], [0, 1])], "FPR", "TPR")
            if auc < 0.55:
                res.info("weak_classifier", f"AUC {auc:.3f}: barely better than chance")
        q = np.quantile(p, np.linspace(0, 1, 11))
        b = np.clip(np.searchsorted(q, p, side="right") - 1, 0, 9)
        mp = [float(p[b == i].mean()) if (b == i).any() else None for i in range(10)]
        ob = [float(yb[b == i].mean()) if (b == i).any() else None for i in range(10)]
        res.line_plot("calibration", "Calibration (deciles of predicted probability)",
                      [series("observed", mp, ob), series("perfect", [0, 1], [0, 1])], "predicted", "observed",
                      kind="scatter")
