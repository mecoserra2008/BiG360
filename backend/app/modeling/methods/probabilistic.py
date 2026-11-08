"""Studio v2 S3 probabilistic models (owner: model-studio): Bayesian ridge / ARD, Gaussian-process regression (hard
cap on n), quantile gradient boosting (LightGBM quantile objectives), NGBoost and hierarchical partial pooling by
sector (empirical Bayes or a numpy Gibbs sampler with R-hat / ESS). Core: app/modeling/_prob.py.

Every model is scored out of fold with proper scoring rules (CRPS, pinball), a PIT histogram and a calibration curve,
and is compared with a linear-Gaussian baseline (OLS mean + training residual sd) on the same folds by a
Diebold-Mariano test on CRPS; `cv.conformal_alpha` adds split-conformal intervals (normalized by the predictive sd,
or CQR for quantile models)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import _ml as M
from .. import _prob as P
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, series
from .advml import _lead, dm_vs, tested_mask
from .ml import CVParams, _oos_block, conformal_block, make_folds, oos_eval_artifact

FAMILY = "probabilistic"
PINBALL_TAUS = (0.05, 0.25, 0.5, 0.75, 0.95)


# ------------------------------------------------------------------------------------------------ shared scoring


def _dm_loss(res: Result, df: pd.DataFrame, lm: np.ndarray, lr: np.ndarray, label: str, ref: str, what: str,
             horizon: int) -> dict[str, Any]:
    """DM on a per-row loss (date-ordered): stat > 0 = the model has the lower loss."""
    ok = np.isfinite(lm) & np.isfinite(lr)
    order = np.argsort(pd.to_datetime(df["date"].to_numpy()[ok]), kind="stable")
    a, b = lr[ok][order], lm[ok][order]
    stat, pv = M.dm_test(a, b, max(horizon, 1))
    better = bool(np.mean(b) < np.mean(a))
    sig = pv is not None and np.isfinite(pv) and pv < 0.05
    verdict = (f"{label} beats {ref} on {what} (DM p = {pv:.3g})" if better and sig else
               f"{label} does not significantly beat {ref} on {what} (DM p = {pv:.3g})" if better else
               f"{label} is worse than {ref} on {what} (mean {np.mean(b):.4g} vs {np.mean(a):.4g})")
    res.add_test(f"Diebold-Mariano on {what}: {label} vs {ref}", stat, pv, None, "equal expected score", group="inference",
                 passed=None, conclusion=verdict)
    if not (better and sig):
        res.warn(f"no_{what.split()[0].lower()}_gain_vs_{ref.split()[0]}", verdict, "warning" if not better else "info")
    return {"stat": stat, "p": pv, "beats": bool(better and sig), "verdict": verdict, "loss_model": float(np.mean(b)),
            "loss_ref": float(np.mean(a))}


def prob_block(res: Result, df: pd.DataFrame, y: np.ndarray, mask: np.ndarray, label: str, base: dict[str, np.ndarray],
               horizon: int, mu: np.ndarray | None = None, sd: np.ndarray | None = None, Q: np.ndarray | None = None,
               taus: list[float] | None = None) -> dict[str, Any]:
    """PIT histogram, calibration curve, CRPS / pinball table and the CRPS DM test against the linear-Gaussian baseline."""
    ok = mask & (np.isfinite(mu) & np.isfinite(sd) if Q is None else np.isfinite(Q).all(1)) & np.isfinite(base["lin_mu"])
    yo = y[ok]
    if Q is None:
        crps = P.crps_gauss(yo, mu[ok], sd[ok])
        pit = P.pit_gauss(yo, mu[ok], sd[ok])
        cal = P.coverage_gauss(yo, mu[ok], sd[ok])
        qs = {t: mu[ok] + sd[ok] * float(__import__("scipy").stats.norm.ppf(t)) for t in PINBALL_TAUS}
    else:
        crps = P.crps_quantiles(yo, Q[ok], taus)
        pit = P.pit_quantiles(yo, Q[ok], taus)
        cal = P.coverage_quantiles(yo, Q[ok], taus)
        qs = {t: Q[ok][:, j] for j, t in enumerate(taus)}
    from scipy import stats

    lin_c = P.crps_gauss(yo, base["lin_mu"][ok], base["lin_sd"][ok])
    clim_c = P.crps_gauss(yo, base["clim_mu"][ok], base["clim_sd"][ok])
    h = P.pit_histogram(pit)
    res.line_plot("pit", f"PIT histogram (flat = calibrated; chi-square p = {h['p_value']:.3g})",
                  [series("share of rows", [f"{h['edges'][i]:.1f}-{h['edges'][i + 1]:.1f}" for i in range(len(h["counts"]))],
                          np.asarray(h["counts"]) / max(h["n"], 1)),
                   series("uniform", [f"{h['edges'][i]:.1f}-{h['edges'][i + 1]:.1f}" for i in range(len(h["counts"]))],
                          [1 / len(h["counts"])] * len(h["counts"]))], "PIT bin", "share", kind="bar")
    res.line_plot("calibration", "Calibration: nominal vs empirical coverage of central intervals",
                  [series(label, [c[0] for c in cal], [c[1] for c in cal]), series("45-degree", [0, 1], [0, 1])],
                  "nominal coverage", "empirical coverage", kind="scatter")
    res.add_test("PIT uniformity (chi-square, 10 bins)", h["chi2"], h["p_value"], 9, "PIT values are uniform (calibrated)",
                 group="diagnostics", note="U-shape = intervals too narrow (overconfident); hump = too wide")
    pin = {t: float(np.mean(P.pinball(yo, q, t))) for t, q in qs.items() if t in PINBALL_TAUS or Q is not None}
    lin_pin = {t: float(np.mean(P.pinball(yo, base["lin_mu"][ok] + base["lin_sd"][ok] * stats.norm.ppf(t), t))) for t in pin}
    rows = [["CRPS", float(np.mean(crps)), float(np.mean(lin_c)), float(np.mean(clim_c))]]
    rows += [[f"pinball tau={t:g}", v, lin_pin[t], None] for t, v in pin.items()]
    c90 = next((e for n, e in cal if abs(n - 0.9) < 1e-6), None)
    rows.append(["coverage of 90 % interval", c90, float(np.mean(np.abs(yo - base["lin_mu"][ok]) <= 1.645 * base["lin_sd"][ok])), None])
    res.add_table("prob_scores", "Out-of-fold probabilistic scores (lower is better except coverage)",
                  ["score", label, "linear-Gaussian baseline", "climatology"], clean(rows),
                  note="linear-Gaussian = OLS mean + training residual sd; climatology = N(training mean, training sd); "
                       "both refitted inside each training fold")
    full_lm = np.full(len(y), np.nan)
    full_lr = np.full(len(y), np.nan)
    full_lm[ok], full_lr[ok] = crps, lin_c
    dm = _dm_loss(res, df, full_lm, full_lr, label, "linear-Gaussian baseline", "CRPS", horizon)
    res.metrics.update(clean({"crps": float(np.mean(crps)), "crps_linear": float(np.mean(lin_c)),
                              "crps_climatology": float(np.mean(clim_c)), "crps_skill_vs_linear": 1 - float(np.mean(crps)) / float(np.mean(lin_c)),
                              "pit_chi2_p": h["p_value"], "coverage_90": c90, "dm_p_crps_vs_linear": dm["p"]}))
    res.evaluation = {**(res.evaluation or {}), "probabilistic": clean({"crps": float(np.mean(crps)), "pit": h,
                                                                        "calibration": cal, "dm_crps": dm})}
    return dm


def _common(data: FitData, p: Any, need: int, what: str) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, list, str, int, float | None]:
    df, dropped = data.complete()
    X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
    if len(y) < need:
        raise GuardrailError("too_few_obs", f"{what} needs at least {need} complete rows")
    if len(np.unique(y)) <= 2:
        raise GuardrailError("bad_target", f"{what} is a regression model: the target looks binary (use logit / lightgbm)")
    folds, scheme, purge = make_folds(data, df, p.cv)
    alpha = data.meta.pop("_conformal_alpha", None)  # the normalized / CQR version below replaces the absolute one
    df.attrs["dropped"] = dropped
    return df, X, y, folds, scheme, purge, alpha


def _finish(res: Result, data: FitData, df: pd.DataFrame, y: np.ndarray, label: str, dm_point: dict[str, Any],
            dm_crps: dict[str, Any], scheme: str, extra: dict[str, Any] | None = None) -> None:
    beats = bool(dm_crps["beats"])
    res.summary.update({"dependent": data.y, "n_obs": len(y), "model": label, "cv_scheme": scheme, "features": list(data.x),
                        "rows_dropped_missing": df.attrs.get("dropped", 0), "beats_linear": bool(dm_point["beats"]),
                        "beats_baseline": beats, "verdict": dm_crps["verdict"] + "; point forecast: " + dm_point["verdict"],
                        **(extra or {})})
    res.metrics.update(clean({"n_obs": len(y), "dm_p_vs_linear": dm_point["dm_p"], "beats_baseline": beats}))


def _point_vs_linear(res: Result, df: pd.DataFrame, y: np.ndarray, mu: np.ndarray, base: dict[str, np.ndarray],
                     mask: np.ndarray, label: str, data: FitData) -> dict[str, Any]:
    from .advml import metrics_of

    res.evaluation["metrics"]["linear"] = clean(metrics_of(y[mask], base["lin_mu"][mask], "regression"))
    return dm_vs(res, df, y, mu, base["lin_mu"], "regression", mask, label, "linear baseline", _lead(data))


# ------------------------------------------------------------------------------------------------ Bayesian ridge / ARD


class BayesRidgeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["bayes_ridge", "ard"] = Field(default="bayes_ridge", description="bayes_ridge: one shared weight "
                                                 "precision; ard: one precision per feature (automatic relevance determination)")
    max_iter: int = Field(default=300, ge=10, le=5000)
    cv: CVParams = Field(default_factory=CVParams)


@register
class BayesRidge(Method):
    id = "bayes_ridge"
    family = FAMILY
    title = "Bayesian ridge / ARD regression"
    description = ("Bayesian linear regression with evidence-maximized priors (BayesianRidge: one weight precision; "
                   "ARD: one per feature, prunes irrelevant ones). Posterior coefficient sds, predictive mean and sd per "
                   "row, CRPS / pinball / PIT / calibration out of fold vs a linear-Gaussian baseline, optional conformal.")
    min_x = 1
    Params = BayesRidgeParams
    outputs = ("posterior coefficients", "ARD relevance", "PIT histogram", "calibration curve", "CRPS / pinball table",
               "conformal intervals (cv.conformal_alpha)")
    diagnostics = ("PIT uniformity", "DM on CRPS vs linear-Gaussian", "DM on squared error vs OLS")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        k = max(n_x, 1)
        per = n_rows * k * (k + 5) * 4e-9 * (3 if params.model == "ard" else 1)  # calibrated on real runs (S3 recipes)
        return 0.3 + (params.cv.folds + 1) * per

    def fit(self, data: FitData, p: BayesRidgeParams) -> Result:
        df, X, y, folds, scheme, purge, alpha = _common(data, p, 30, "Bayesian ridge")
        label = "ARD" if p.model == "ard" else "Bayesian ridge"
        res = Result(self.id, self.family, f"{label} regression")
        cv = P.bayes_linear_cv(X, y, folds, p.model, p.max_iter, data.check)
        _oos_block(res, data, df, y, cv["mu"], "regression", folds, scheme, purge, p.model)
        oos_eval_artifact(res, data, df, y, cv["mu"], folds, p.model, {"pred_sd": cv["sd"]})
        mask = tested_mask(len(y), folds)
        base = P.baseline_cv(X, y, folds)
        dmp = _point_vs_linear(res, df, y, cv["mu"], base, mask, label, data)
        dmc = prob_block(res, df, y, mask, label, base, _lead(data), cv["mu"], cv["sd"])
        if alpha:
            conformal_block(res, df, y, cv["mu"], folds, float(alpha), scale=cv["sd"])
        data.check()
        f = P.bayes_linear_fit(X, y, p.model, p.max_iter)
        z = np.where(f["se"] > 0, f["coef"] / np.where(f["se"] > 0, f["se"], 1), np.nan)
        res.coefficients = [clean({"term": "const", "estimate": f["intercept"], "std_error": None, "statistic": None,
                                   "p_value": None, "ci_low": None, "ci_high": None})]
        for j, nm in enumerate(data.x):
            res.coefficients.append(clean({"term": nm, "estimate": f["coef"][j], "std_error": f["se"][j], "statistic": z[j],
                                           "p_value": None, "ci_low": f["coef"][j] - 1.96 * f["se"][j],
                                           "ci_high": f["coef"][j] + 1.96 * f["se"][j], "posterior_prob_positive":
                                           float(__import__("scipy").stats.norm.cdf(z[j])) if np.isfinite(z[j]) else None}))
        res.add_table("relevance", "Prior precision per feature (ARD: large = pruned toward 0; standardized scale)",
                      ["feature", "coef_standardized", "posterior_sd_standardized", "prior_precision_lambda"],
                      clean([[nm, f["coef_std"][j], f["se_std"][j], f["lambda"][j]] for j, nm in enumerate(data.x)]))
        res.line_plot("coef_posterior", "Posterior mean +/- 1.96 sd (standardized features)",
                      [series("posterior", list(data.x), f["coef_std"], ci_low=list(f["coef_std"] - 1.96 * f["se_std"]),
                              ci_high=list(f["coef_std"] + 1.96 * f["se_std"]))], "feature", "coefficient", kind="errorbar")
        res.fit_stats.update(clean({"noise_precision_alpha": f["alpha"], "noise_sd": f["noise_sd"],
                                    "weight_precision_lambda": float(np.median(f["lambda"])), "n_iter": f["n_iter"]}))
        res.info("bayes_ci", "coefficient intervals are Bayesian credible intervals (Gaussian posterior); no p-values")
        _finish(res, data, df, y, label, dmp, dmc, scheme)
        return res


# ------------------------------------------------------------------------------------------------ Gaussian process


class GPParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kernel: Literal["rbf", "matern32", "matern52", "rational_quadratic"] = "matern52"
    ard: bool = Field(default=False, description="one length scale per feature")
    restarts: int = Field(default=1, ge=0, le=10, description="optimizer restarts of the marginal likelihood")
    max_n: int = Field(default=2000, ge=50, le=P.GP_HARD_CAP, description=f"refuse above this many training rows (hard cap {P.GP_HARD_CAP})")
    cv: CVParams = Field(default_factory=CVParams)


@register
class GaussianProcess(Method):
    id = "gp_regression"
    family = FAMILY
    title = "Gaussian-process regression (small n)"
    description = ("Non-parametric Bayesian regression with an RBF / Matern / rational-quadratic kernel plus white noise, "
                   "hyper-parameters by marginal likelihood inside each training fold; predictive mean and sd, CRPS / PIT "
                   f"/ calibration vs a linear-Gaussian baseline. O(n^3): refused above max_n (hard cap {P.GP_HARD_CAP}).")
    min_x = 1
    max_x = 30
    Params = GPParams
    outputs = ("fitted kernel per fold", "PIT histogram", "calibration curve", "CRPS / pinball table", "partial dependence (1-D slices)")
    diagnostics = ("PIT uniformity", "DM on CRPS vs linear-Gaussian", "n cap")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        n = min(n_rows, params.max_n)
        per_eval = 0.002 + n ** 3 * 1.5e-10 * (1 + 0.3 * n_x * params.ard)
        return 0.5 + (params.cv.folds + 1) * (params.restarts + 1) * 60 * per_eval

    def fit(self, data: FitData, p: GPParams) -> Result:
        df, X, y, folds, scheme, purge, alpha = _common(data, p, 20, "Gaussian-process regression")
        n_tr = max(len(tr) for tr, _ in folds)
        try:
            P.gp_check_n(max(n_tr, len(y)), p.max_n)
        except ValueError as e:
            raise GuardrailError("too_many_obs", str(e)) from e
        res = Result(self.id, self.family, f"Gaussian process ({p.kernel}{', ARD' if p.ard else ''})")
        cv = P.gp_cv(X, y, folds, p.kernel, p.ard, p.restarts, data.seed, data.check)
        _oos_block(res, data, df, y, cv["mu"], "regression", folds, scheme, purge, "gp")
        oos_eval_artifact(res, data, df, y, cv["mu"], folds, "gp", {"pred_sd": cv["sd"]})
        mask = tested_mask(len(y), folds)
        base = P.baseline_cv(X, y, folds)
        dmp = _point_vs_linear(res, df, y, cv["mu"], base, mask, "GP", data)
        dmc = prob_block(res, df, y, mask, "GP", base, _lead(data), cv["mu"], cv["sd"])
        if alpha:
            conformal_block(res, df, y, cv["mu"], folds, float(alpha), scale=cv["sd"])
        res.add_table("kernels", "Fitted kernel per fold (standardized inputs, normalized y)", ["fold", "kernel"],
                      [[i + 1, k] for i, k in enumerate(cv["kernels"])])
        data.check()
        g, mu, sdv = P.gp_fit(X, y, p.kernel, p.ard, p.restarts, data.seed)
        res.fit_stats.update(clean({"log_marginal_likelihood": float(g.log_marginal_likelihood_value_), "kernel": str(g.kernel_)}))
        for j, nm in enumerate(data.x[:4]):  # 1-D slices through the median of the other features
            grid = np.linspace(np.nanpercentile(X[:, j], 2), np.nanpercentile(X[:, j], 98), 40)
            Z = np.tile(np.median(X, 0), (40, 1))
            Z[:, j] = grid
            m, s = g.predict((Z - mu) / sdv, return_std=True)
            from ..results import ts_band

            ts_band(res, f"slice_{nm}", f"GP posterior along {nm} (others at their median)", grid, [("mean", m)],
                    [("95 % band", m - 1.96 * s, m + 1.96 * s)], x_label=nm, y_label=data.y or "")
        _finish(res, data, df, y, "GP", dmp, dmc, scheme, {"max_n": p.max_n})
        return res


# ------------------------------------------------------------------------------------------------ quantile GBM


class QuantileGBMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quantiles: list[float] = Field(default_factory=lambda: [0.05, 0.25, 0.5, 0.75, 0.95], min_length=3, max_length=11)
    n_estimators: int = Field(default=300, ge=10, le=3000)
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    num_leaves: int = Field(default=15, ge=2, le=512)
    min_child_samples: int = Field(default=50, ge=1, le=10000)
    subsample: float = Field(default=0.8, gt=0, le=1)
    colsample_bytree: float = Field(default=0.8, gt=0, le=1)
    cv: CVParams = Field(default_factory=CVParams)

    @field_validator("quantiles")
    @classmethod
    def _q(cls, v: list[float]) -> list[float]:
        v = sorted({round(float(x), 4) for x in v})
        if any(not 0 < x < 1 for x in v) or 0.5 not in v:
            raise ValueError("quantiles must lie in (0, 1) and include the median 0.5")
        return v


@register
class QuantileGBM(Method):
    id = "quantile_gbm"
    family = FAMILY
    title = "Quantile gradient boosting (LightGBM)"
    description = ("One LightGBM model per quantile level (pinball objective), crossing removed by monotone "
                   "rearrangement; pinball loss per level, CRPS (quantile approximation), PIT and interval coverage out of "
                   "fold vs climatological quantiles and a linear-Gaussian baseline; conformalized quantile regression (CQR).")
    min_x = 1
    Params = QuantileGBMParams
    outputs = ("pinball loss per quantile", "PIT histogram", "calibration curve", "fan chart (time series)", "CQR intervals")
    diagnostics = ("quantile crossing rate (before rearrangement)", "DM on CRPS vs linear-Gaussian", "PIT uniformity")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.5 + len(params.quantiles) * (params.cv.folds + 1) * n_rows * max(n_x, 1) * params.n_estimators * 9e-9

    def fit(self, data: FitData, p: QuantileGBMParams) -> Result:
        df, X, y, folds, scheme, purge, alpha = _common(data, p, 50, "quantile GBM")
        taus = list(p.quantiles)
        res = Result(self.id, self.family, f"Quantile GBM ({len(taus)} levels)")
        prm = p.model_dump(exclude={"cv", "quantiles"})
        out = P.quantile_lgbm_cv(X, y, folds, taus, prm, data.seed, data.check)
        Q = out["Q"]
        med = Q[:, taus.index(0.5)]
        _oos_block(res, data, df, y, med, "regression", folds, scheme, purge, "quantile_gbm")
        oos_eval_artifact(res, data, df, y, med, folds, "quantile_gbm", {f"q{t:g}": Q[:, j] for j, t in enumerate(taus)})
        mask = tested_mask(len(y), folds)
        base = P.baseline_cv(X, y, folds)
        dmp = _point_vs_linear(res, df, y, med, base, mask, "quantile GBM (median)", data)
        dmc = prob_block(res, df, y, mask, "quantile GBM", base, _lead(data), Q=Q, taus=taus)
        clim = P.clim_quantiles_cv(y, folds, taus)
        ok = mask & np.isfinite(Q).all(1)
        rows = [[t, float(np.mean(P.pinball(y[ok], Q[ok, j], t))), float(np.mean(P.pinball(y[ok], clim[ok, j], t)))] for j, t in enumerate(taus)]
        res.add_table("pinball", "Pinball loss per quantile level vs climatological (training-fold) quantiles",
                      ["tau", "quantile_gbm", "climatology"], clean(rows))
        res.line_plot("pinball", "Pinball loss by quantile level", [series("quantile GBM", taus, [r[1] for r in rows]),
                      series("climatology", taus, [r[2] for r in rows])], "tau", "pinball loss")
        res.fit_stats["crossing_rate_raw"] = clean(out["crossing_rate"])
        if out["crossing_rate"] > 0.05:
            res.info("quantile_crossing", f"{out['crossing_rate']:.0%} of rows had crossing quantiles before rearrangement")
        if not data.is_panel and ok.any():
            from ..results import ts_band

            o = np.where(ok)[0]
            lo_i, hi_i = 0, len(taus) - 1
            ts_band(res, "fan", f"Out-of-fold quantile fan ({taus[lo_i]:g}-{taus[hi_i]:g})", df["date"].to_numpy()[o],
                    [("actual", y[o]), ("median", med[o])], [(f"{taus[lo_i]:g}-{taus[hi_i]:g}", Q[o, lo_i], Q[o, hi_i])])
        if alpha:  # CQR on the pair of levels nearest alpha / 2 and 1 - alpha / 2
            a = float(alpha)
            lo_j = int(np.argmin([abs(t - a / 2) for t in taus]))
            hi_j = int(np.argmin([abs(t - (1 - a / 2)) for t in taus]))
            conformal_block(res, df, y, med, folds, a, lo=Q[:, lo_j], hi=Q[:, hi_j])
        res.metrics["pinball_median"] = clean(rows[taus.index(0.5)][1])
        _finish(res, data, df, y, "quantile GBM", dmp, dmc, scheme, {"quantiles": taus})
        return res


# ------------------------------------------------------------------------------------------------ NGBoost


class NGBoostParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_estimators: int = Field(default=400, ge=10, le=3000, description="boosting rounds (early stopping on the last 15 % of each training fold)")
    learning_rate: float = Field(default=0.03, gt=0, le=1)
    max_depth: int = Field(default=3, ge=1, le=8, description="depth of the base regression trees")
    min_samples_leaf: int = Field(default=50, ge=1, le=10000, description="leaf size of the base trees (small leaves overfit the scale)")
    minibatch_frac: float = Field(default=1.0, gt=0, le=1, description="row subsample per round")
    cv: CVParams = Field(default_factory=CVParams)


@register
class NGBoost(Method):
    id = "ngboost"
    family = FAMILY
    title = "NGBoost (natural-gradient boosting, Normal)"
    description = ("Boosts the location and log-scale of a Normal predictive distribution with natural gradients "
                   "(Duan et al. 2020): heteroskedastic predictive sd per row; CRPS / PIT / calibration out of fold vs a "
                   "linear-Gaussian baseline, normalized conformal option.")
    min_x = 1
    Params = NGBoostParams
    outputs = ("predictive mean and sd", "PIT histogram", "calibration curve", "feature importance (loc / scale)")
    diagnostics = ("PIT uniformity", "DM on CRPS vs linear-Gaussian", "best iteration per fold")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        per = params.n_estimators * (7e-3 + 2 * n_rows * params.minibatch_frac * max(n_x, 1) * params.max_depth * 2e-8)
        return 1.0 + (params.cv.folds + 1) * per

    def fit(self, data: FitData, p: NGBoostParams) -> Result:
        df, X, y, folds, scheme, purge, alpha = _common(data, p, 50, "NGBoost")
        res = Result(self.id, self.family, "NGBoost (Normal)")
        prm = p.model_dump(exclude={"cv"})
        dates = pd.to_datetime(df["date"]).to_numpy()
        cv = P.ngboost_cv(X, y, folds, prm, data.seed, data.check, dates)
        _oos_block(res, data, df, y, cv["mu"], "regression", folds, scheme, purge, "ngboost")
        oos_eval_artifact(res, data, df, y, cv["mu"], folds, "ngboost", {"pred_sd": cv["sd"]})
        mask = tested_mask(len(y), folds)
        base = P.baseline_cv(X, y, folds)
        dmp = _point_vs_linear(res, df, y, cv["mu"], base, mask, "NGBoost", data)
        dmc = prob_block(res, df, y, mask, "NGBoost", base, _lead(data), cv["mu"], cv["sd"])
        if alpha:
            conformal_block(res, df, y, cv["mu"], folds, float(alpha), scale=cv["sd"])
        res.add_table("best_iterations", "Boosting rounds per fold (early stopping)", ["fold", "n_estimators"],
                      [[i + 1, b] for i, b in enumerate(cv["best_iters"])])
        data.check()
        n_final = max(10, int(np.median(cv["best_iters"])))
        m = P.ngboost_model(n_final, p.learning_rate, p.max_depth, p.minibatch_frac, data.seed, p.min_samples_leaf).fit(X, y)
        fi = np.asarray(m.feature_importances_, float)  # [loc, scale] x features
        res.add_table("importance", "Feature importance for the location and the scale", ["feature", "loc", "scale"],
                      clean([[nm, fi[0, j], fi[1, j]] for j, nm in enumerate(data.x)]))
        res.line_plot("importance", "Feature importance (location vs scale)", [series("loc", list(data.x), fi[0]),
                      series("scale", list(data.x), fi[1])], "feature", "importance", kind="bar")
        sd_ok = cv["sd"][np.isfinite(cv["sd"])]
        res.fit_stats.update(clean({"final_n_estimators": n_final, "pred_sd_p10": np.percentile(sd_ok, 10) if len(sd_ok) else None,
                                    "pred_sd_p90": np.percentile(sd_ok, 90) if len(sd_ok) else None}))
        _finish(res, data, df, y, "NGBoost", dmp, dmc, scheme)
        return res


# ------------------------------------------------------------------------------------------------ hierarchical pooling


class HierPoolParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group: str = Field(default="sector_id", description="grouping column: sector_id | region | country | entity_id "
                       "(security-level panel / cross-section attributes)")
    estimator: Literal["empirical_bayes", "gibbs"] = Field(default="gibbs", description="empirical Bayes (EM on the "
                                                           "marginal likelihood) or a numpy Gibbs sampler (conjugate)")
    n_iter: int = Field(default=2000, ge=200, le=20000, description="Gibbs iterations per chain (incl. burn-in)")
    burn: int = Field(default=1000, ge=50, le=10000)
    chains: int = Field(default=4, ge=2, le=8)
    min_group_n: int = Field(default=10, ge=3, le=10000, description="groups with fewer rows are dropped")
    cv: CVParams = Field(default_factory=CVParams)

    @field_validator("burn")
    @classmethod
    def _burn(cls, v: int, info: Any) -> int:
        if v >= (info.data.get("n_iter") or 2000):
            raise ValueError("burn must be smaller than n_iter")
        return v


def _grouped(data: FitData, group: str, min_n: int) -> tuple[pd.DataFrame, np.ndarray]:
    if group not in data.frame.columns:
        raise GuardrailError("missing_group", f"group column {group!r} is not in the dataset (security-level panels carry "
                             "sector_id / region / country; set entity_level=security)")
    cols = data.columns()
    keep = ["date"] + [c for c in ("entity_id", "entity_label") if c in data.frame.columns] + ([group] if group not in ("entity_id",) else [])
    f = data.frame.select(list(dict.fromkeys(keep + cols)))
    cond = pl.col(group).is_not_null()
    for c in cols:
        e = pl.col(c).cast(pl.Float64)
        cond = cond & e.is_not_null() & e.is_finite()
    df = f.filter(cond).to_pandas()
    for c in cols:
        df[c] = df[c].astype(float)
    g = df[group].astype(str)
    cnt = g.value_counts()
    df = df[g.isin(cnt[cnt >= min_n].index)].reset_index(drop=True)
    return df, df[group].astype(str).to_numpy()


@register
class HierPool(Method):
    id = "hier_pool"
    family = FAMILY
    title = "Hierarchical regression: partial pooling by sector"
    description = ("Varying intercepts and slopes by group (default GICS sector): beta_sector ~ N(mu, diag(tau^2)). "
                   "Empirical Bayes or a numpy Gibbs sampler (4 chains, split R-hat, ESS). Shrinks noisy small-sector "
                   "estimates toward the cross-sector mean; compared out of fold with complete pooling (one OLS) and no "
                   "pooling (OLS per sector), with CRPS / PIT / calibration of the posterior predictive.")
    shapes = ("panel", "cross_section")
    min_x = 1
    max_x = 12
    Params = HierPoolParams
    outputs = ("per-group estimates: no / partial / complete pooling", "shrinkage factors", "forest plot",
               "population mean and between-group sd", "R-hat / ESS", "trace of mu")
    diagnostics = ("split R-hat < 1.01", "ESS", "DM partial vs complete pooling", "PIT uniformity")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        k = max(n_x, 1) + 1
        J = 12
        stats_s = n_rows * 4e-6  # group sufficient statistics, no-pool fits, vectorized prediction (calibrated on real runs)
        if params.estimator == "gibbs":
            it = J * (k * k * 1e-5 + 5e-5)  # one blocked sweep (sufficient-statistics SSR: O(J k^3), not O(n))
            per = stats_s + params.chains * params.n_iter * it
            cv_per = stats_s + 2 * max(500, params.n_iter // 4) * it
        else:
            per = cv_per = stats_s + 200 * J * k ** 3 * 2e-6
        return 0.5 + per + params.cv.folds * cv_per

    def fit(self, data: FitData, p: HierPoolParams) -> Result:
        df, g = _grouped(data, p.group, p.min_group_n)
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        groups = sorted(set(g))
        if len(groups) < 3:
            raise GuardrailError("too_few_groups", f"partial pooling needs at least 3 groups with >= {p.min_group_n} rows; got {len(groups)}")
        if len(y) < 50:
            raise GuardrailError("too_few_obs", "hierarchical pooling needs at least 50 complete rows")
        folds, scheme, purge = make_folds(data, df, p.cv)
        alpha = data.meta.pop("_conformal_alpha", None)
        est = "Gibbs" if p.estimator == "gibbs" else "empirical Bayes"
        res = Result(self.id, self.family, f"Partial pooling by {p.group} ({est})")
        gcv = {"n_iter": max(500, p.n_iter // 4), "burn": max(250, p.n_iter // 8), "chains": 2}
        cv = P.pool_cv(X, y, g, folds, p.estimator, gcv, data.seed, data.check)
        _oos_block(res, data, df, y, cv["mu"], "regression", folds, scheme, purge, "hier_pool")
        oos_eval_artifact(res, data, df, y, cv["mu"], folds, "hier_pool", {"pred_sd": cv["sd"]})
        mask = tested_mask(len(y), folds)
        base = P.baseline_cv(X, y, folds)  # baseline_cv's OLS = complete pooling
        dmp = _point_vs_linear(res, df, y, cv["mu"], base, mask, "partial pooling", data)
        dm_vs(res, df, y, cv["mu"], cv["nopool"], "regression", mask, "partial pooling", "no pooling", _lead(data))
        from .advml import metrics_of

        res.evaluation["metrics"]["no_pooling"] = clean(metrics_of(y[mask], cv["nopool"][mask], "regression"))
        dmc = prob_block(res, df, y, mask, "partial pooling", base, _lead(data), cv["mu"], cv["sd"])
        if alpha:
            conformal_block(res, df, y, cv["mu"], folds, float(alpha), scale=cv["sd"])
        data.check()
        gfull = {"n_iter": p.n_iter, "burn": p.burn, "chains": p.chains}
        f = P.pool_fit(X, y, g, groups, p.estimator, gfull, data.seed, data.check)
        B, V, _ = P.no_pool(X, y, g, groups)
        bp, *_ = np.linalg.lstsq(P._design(X), y, rcond=None)
        names = ["const"] + list(data.x)
        n_by = {j: int((g == j).sum()) for j in groups}
        rows = []
        for i, j in enumerate(groups):
            for q, nm in enumerate(names):
                sh = (1 - f["beta_sd"][i, q] ** 2 / max(V[i, q, q], 1e-300)) if V[i, q, q] > 0 else None
                rows.append([j, nm, n_by[j], B[i, q], np.sqrt(V[i, q, q]), f["beta"][i, q], f["beta_sd"][i, q],
                             f["beta_lo"][i, q], f["beta_hi"][i, q], bp[q], None if sh is None else 1 - max(min(sh, 1), 0)])
        res.add_table("pooling", "Per-group coefficients: no pooling (OLS by group), partial pooling (posterior), complete pooling",
                      ["group", "term", "n", "no_pool", "no_pool_se", "partial", "partial_sd", "ci_low", "ci_high", "complete_pool",
                       "weight_on_own_data"], clean(rows),
                      note="weight_on_own_data = 1 - shrinkage toward the population mean (1 - posterior var / OLS var)")
        tau = np.sqrt(f["tau2"])
        res.coefficients = [clean({"term": f"mu[{nm}]", "estimate": f["mu"][q], "std_error": (f.get("mu_sd") if f.get("mu_sd") is not None else np.full(len(names), np.nan))[q],
                                   "statistic": None, "p_value": None, "ci_low": None, "ci_high": None, "between_group_sd": tau[q]})
                            for q, nm in enumerate(names)]
        slope = 1 if len(names) > 1 else 0
        res.line_plot("forest", f"{names[slope]} by {p.group}: partial pooling (95 % interval) vs no pooling vs complete pooling",
                      [series("partial pooling", groups, f["beta"][:, slope], ci_low=list(f["beta_lo"][:, slope]),
                              ci_high=list(f["beta_hi"][:, slope])),
                       series("no pooling", groups, B[:, slope]), series("complete pooling", groups, [bp[slope]] * len(groups))],
                      p.group, names[slope], kind="errorbar")
        res.fit_stats.update(clean({"n_groups": len(groups), "sigma": float(np.sqrt(f["sigma2"])),
                                    **{f"tau_{nm}": tau[q] for q, nm in enumerate(names)}}))
        if p.estimator == "gibbs":
            rh = np.concatenate([f["rhat_beta"].ravel(), f["rhat_mu"], [f["rhat_sigma"]]])
            es = np.concatenate([f["ess_beta"].ravel(), f["ess_mu"], [f["ess_sigma"]]])
            res.add_table("convergence", "Gibbs convergence (split R-hat, effective sample size)",
                          ["parameter", "rhat", "ess"],
                          clean([[f"beta[{j},{nm}]", f["rhat_beta"][i, q], f["ess_beta"][i, q]] for i, j in enumerate(groups) for q, nm in enumerate(names)]
                                + [[f"mu[{nm}]", f["rhat_mu"][q], f["ess_mu"][q]] for q, nm in enumerate(names)]
                                + [[f"log tau2[{nm}]", f["rhat_tau"][q], None] for q, nm in enumerate(names)]
                                + [["sigma2", f["rhat_sigma"], f["ess_sigma"]]]))
            mx, mn = float(np.nanmax(rh)), float(np.nanmin(es))
            res.add_test("max split R-hat", mx, None, None, "chains mixed (R-hat < 1.01)", group="diagnostics",
                         passed=bool(mx < 1.01), conclusion="converged" if mx < 1.01 else "not converged: raise n_iter")
            res.fit_stats.update(clean({"max_rhat": mx, "min_ess": mn, "n_draws": f["n_draws"]}))
            if mx >= 1.01 or mn < 400:
                res.warn("mcmc_convergence", f"max R-hat {mx:.3f}, min ESS {mn:.0f}: raise n_iter / burn (target R-hat < 1.01, ESS >= 400)")
            tr = f["trace_mu"][:, :, slope]
            it = list(range(tr.shape[1]))
            step = max(1, len(it) // 500)
            res.line_plot("trace", f"Trace of mu[{names[slope]}] by chain (post burn-in)",
                          [series(f"chain {c + 1}", it[::step], tr[c, ::step]) for c in range(tr.shape[0])], "draw", "mu")
            res.metrics["max_rhat"] = clean(mx)
        _finish(res, data, df, y, f"partial pooling ({est})", dmp, dmc, scheme, {"group": p.group, "n_groups": len(groups),
                                                                             "estimator": p.estimator})
        return res
