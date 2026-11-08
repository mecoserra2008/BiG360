"""Studio v2 S2 controls & causal methods: DML (PLR / IRM), propensity scores (IPW / AIPW / matching) with balance
tables, regression discontinuity (sharp / fuzzy) and the coefficient-stability table with Oster's delta
(owner: model-studio). Estimation core: app/modeling/_causal2.py."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field
from scipy import stats

from .. import _causal2 as C2
from .. import _ml as M
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, coef_rows, downsample_idx, series

Learner = Literal["lasso", "rf", "xgb", "lgbm"]


class CrossFit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folds: int = Field(default=5, ge=2, le=10)
    scheme: Literal["auto", "purged_kfold", "kfold"] = Field(
        default="auto", description="auto: purged K-fold on date blocks (time series / panel), shuffled K-fold (cross-section)")
    purge: int | None = Field(default=None, ge=0, le=60, description="periods dropped around each test block (default: target horizon, >= 1)")
    embargo: int = Field(default=0, ge=0, le=60)


def crossfit_folds(data: FitData, df: pd.DataFrame, cf: CrossFit, n_rep: int = 1) -> tuple[list[list], str, int]:
    scheme = cf.scheme if cf.scheme != "auto" else ("kfold" if data.kind == "cross_section" else "purged_kfold")
    lead_k = int((data.meta.get("lead_k") or {}).get(data.y or "", 0))
    purge = cf.purge if cf.purge is not None else max(1, lead_k)
    reps = n_rep if scheme == "kfold" else 1  # contiguous date blocks are deterministic: repetitions add nothing
    sets = [M.cv_splits(df["date"], scheme, cf.folds, purge, cf.embargo, seed=data.seed + r) for r in range(reps)]
    if any(len(s) < 2 for s in sets):
        raise GuardrailError("too_few_obs", "not enough periods for cross-fitting")
    return sets, scheme, purge


def groups_of(data: FitData, df: pd.DataFrame, cluster: str) -> np.ndarray | None:
    if cluster == "auto":
        cluster = "entity" if data.is_panel else "none"
    if cluster == "entity" and "entity_id" in df.columns:
        return df["entity_id"].astype(str).to_numpy()
    if cluster == "date":
        return pd.to_datetime(df["date"]).to_numpy()
    return None


def _need_n(n: int, k: int, what: str) -> None:
    if n < k:
        raise GuardrailError("too_few_obs", f"{what} needs at least {k} complete rows (got {n})")


def _binary(v: np.ndarray, name: str) -> np.ndarray:
    if not C2.is_binary(v):
        raise GuardrailError("bad_treatment", f"{name} must be a 0/1 treatment indicator")
    return v > 0.5


def overlap_plot(res: Result, key: str, e: np.ndarray, D: np.ndarray, bins: int = 20) -> None:
    edges = np.linspace(0, 1, bins + 1)
    mid = np.round((edges[:-1] + edges[1:]) / 2, 3)
    ht = np.histogram(e[D], edges)[0] / max(int(D.sum()), 1)
    hc = np.histogram(e[~D], edges)[0] / max(int((~D).sum()), 1)
    res.line_plot(key, "Propensity-score overlap (share of each group per bin)",
                  [series("treated", mid, ht), series("control", mid, hc)], "propensity score", "share of group", kind="bar")


def _effect_rows(res: Result, rows: list[tuple[str, float, float, int]]) -> None:
    names = [r[0] for r in rows]
    est = np.array([r[1] for r in rows], float)
    se = np.array([r[2] for r in rows], float)
    z = np.where(se > 0, est / np.where(se > 0, se, 1), np.nan)

    pv = 2 * stats.norm.sf(np.abs(z))
    res.coefficients = coef_rows(names, est, se, z, pv, np.column_stack([est - 1.96 * se, est + 1.96 * se]),
                                 n_obs=[r[3] for r in rows])


# ------------------------------------------------------------------------------------------------ DML


class DMLParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    treatment: str = Field(description="treatment / variable of interest D (dataset column); x = controls")
    model: Literal["plr", "irm"] = Field(default="plr", description="plr: partially linear Y = θD + g(X) + e; irm: binary D, AIPW score")
    estimand: Literal["ate", "att"] = Field(default="ate", description="irm only")
    learner_y: Learner = Field(default="lasso", description="nuisance learner for E[Y|X] (and E[Y|X,D] for irm)")
    learner_d: Learner = Field(default="lasso", description="nuisance learner for E[D|X] / P(D=1|X)")
    crossfit: CrossFit = Field(default_factory=CrossFit)
    n_rep: int = Field(default=1, ge=1, le=10, description="repeated cross-fitting (shuffled K-fold only); median aggregation")
    trim: float = Field(default=0.01, ge=0, le=0.2, description="irm: clip propensity scores to [trim, 1 - trim]")
    cluster: Literal["auto", "none", "entity", "date"] = Field(default="auto", description="auto: entity for panels")
    compare_learners: bool = Field(default=False, description="re-estimate θ with every learner (sensitivity table; slower)")


@register
class DML(Method):
    id = "dml"
    family = "causal"
    title = "Double / debiased machine learning (DML)"
    description = ("Effect of a treatment D on y controlling flexibly for x (Chernozhukov et al. 2018): cross-fitted "
                   "nuisance models (Lasso, random forest, XGBoost or LightGBM) on purged date blocks, partially linear "
                   "(PLR) or interactive (IRM, binary D) score, cluster-robust SE, comparison with naive OLS.")
    min_x = 1
    Params = DMLParams
    outputs = ("θ with SE / CI", "naive OLS vs DML", "nuisance fit (R2 / AUC)", "overlap (irm)", "residual scatter (plr)")
    diagnostics = ("nuisance out-of-fold fit", "propensity overlap", "learner sensitivity")

    def extra_columns(self, params: DMLParams) -> list[str]:
        return [params.treatment]

    def regressor_columns(self, params: DMLParams) -> list[str]:
        return [params.treatment]

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        per = C2.learner_cost(params.learner_y, n_rows, n_x) * (1 if params.model == "plr" else 2) + \
            C2.learner_cost(params.learner_d, n_rows, n_x)
        extra = sum(C2.learner_cost(k, n_rows, n_x) * 2 for k in ("lasso", "rf", "xgb", "lgbm")) if params.compare_learners else 0
        return 0.3 + params.crossfit.folds * (per * params.n_rep + extra)

    def fit(self, data: FitData, p: DMLParams) -> Result:
        if p.treatment in data.x or p.treatment == data.y:
            raise GuardrailError("bad_treatment", "the treatment column must not also be y or a control")
        df, dropped = data.complete([p.treatment])
        y, d, X = df[data.y].to_numpy(float), df[p.treatment].to_numpy(float), df[data.x].to_numpy(float)
        _need_n(len(y), 50, "DML")
        res = Result(self.id, self.family, f"DML {p.model.upper()} ({p.learner_y} / {p.learner_d})")
        res.summary["rows_dropped_missing"] = dropped
        sets, scheme, purge = crossfit_folds(data, df, p.crossfit, p.n_rep)
        g = groups_of(data, df, p.cluster)
        try:
            out = C2.dml(y, d, X, sets, p.model, p.learner_y, p.learner_d, p.estimand, p.trim, g, data.seed, data.check)
        except ValueError as e:
            raise GuardrailError("bad_treatment", str(e)) from e
        label = "θ (PLR)" if p.model == "plr" else f"{p.estimand.upper()} (IRM)"
        naive = C2.ols_hc1(y, np.column_stack([d, X]), g)
        _effect_rows(res, [(label, out["theta"], out["se"], len(y)),
                           ("naive OLS with controls", float(naive["beta"][1]), float(naive["se"][1]), len(y))])
        r0 = out["reps"][0]
        res.add_table("nuisance", "Nuisance models: out-of-fold fit (cross-fitted)", ["model", "metric", "value"],
                      clean([[k.split("_given_")[1] if "_given_" in k else k, k.split("_")[0], v] for k, v in r0["nuis"].items()]))
        if len(out["thetas"]) > 1:
            res.add_table("repetitions", "Repeated cross-fitting", ["repetition", "theta", "se"],
                          clean([[i + 1, t, r["se"]] for i, (t, r) in enumerate(zip(out["thetas"], out["reps"], strict=True))]))
        if p.model == "irm":
            D = d > 0.5
            e = r0["m_hat"]
            overlap_plot(res, "overlap", e, D)
            crossfit_share = float(np.mean((e <= p.trim + 1e-12) | (e >= 1 - p.trim - 1e-12)))
            res.fit_stats["share_at_trim_bounds"] = crossfit_share
            if crossfit_share > 0.05:
                res.warn("overlap", f"{crossfit_share:.0%} of propensity scores sit at the trimming bounds: weak overlap, "
                                    "the effect leans on extrapolation")
        else:
            u, v = y - r0["l_hat"], d - r0["m_hat"]
            idx = downsample_idx(len(u), 1500)
            lo, hi = float(np.nanmin(v)), float(np.nanmax(v))
            res.line_plot("residuals", "Residual-on-residual (Robinson): y − E[y|x] vs D − E[D|x]",
                          [series("rows", v[idx], u[idx]), series("θ slope", [lo, hi], [out["theta"] * lo, out["theta"] * hi])],
                          "D residual", "y residual", kind="scatter")
            nk = "auc_d_given_x" if out["d_binary"] else "r2_d_given_x"
            if (r0["nuis"].get("r2_d_given_x") or 0) > 0.95:
                res.warn("little_variation", "the controls explain more than 95% of D: little independent variation is left, θ is fragile")
            res.fit_stats["d_residual_sd"] = float(np.std(v))
            res.fit_stats[nk] = r0["nuis"].get(nk)
        if p.compare_learners:
            rows = []
            for k in ("lasso", "rf", "xgb", "lgbm"):
                data.check()
                o = C2.dml(y, d, X, sets[:1], p.model, k, k, p.estimand, p.trim, g, data.seed, data.check)
                rows.append([k, o["theta"], o["se"], o["ci"][0], o["ci"][1]])
            res.add_table("learner_sensitivity", "θ by nuisance learner (same folds)", ["learner", "theta", "se", "ci_low", "ci_high"], clean(rows))
            th = [r[1] for r in rows]
            if np.ptp(th) > 2 * out["se"]:
                res.warn("learner_sensitive", "θ moves by more than 2 SE across nuisance learners: the estimate depends on how x is modelled")
        sig = out["p"] < 0.05
        res.info("reading", f"{label} = {out['theta']:.4g} (SE {out['se']:.3g}); naive OLS {float(naive['beta'][1]):.4g}. "
                 + ("Significant at 5%." if sig else "Not significant at 5%.")
                 + " Causal only if x contains every confounder (no unobserved confounding) and D is not itself affected by y.")
        res.summary.update({"dependent": data.y, "treatment": p.treatment, "controls": list(data.x), "model": p.model,
                            "estimand": p.estimand if p.model == "irm" else "plr", "learner_y": p.learner_y,
                            "learner_d": p.learner_d, "cv_scheme": scheme, "purge": purge, "n_folds": len(sets[0]),
                            "n_rep": len(sets), "cluster": "none" if g is None else p.cluster})
        res.metrics.update(clean({"theta": out["theta"], "se": out["se"], "p_value": out["p"], "n_obs": len(y),
                                  "naive_ols": float(naive["beta"][1])}))
        return res


# ------------------------------------------------------------------------------------------------ propensity scores


class PSParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    treatment: str = Field(description="0/1 treatment column; x = covariates of the propensity model")
    estimator: Literal["all", "ipw", "aipw", "matching"] = "all"
    estimand: Literal["ate", "att"] = Field(default="ate", description="IPW / AIPW; matching always estimates the ATT")
    ps_model: Literal["logit", "lasso", "rf", "xgb", "lgbm"] = "logit"
    outcome_model: Literal["ols", "lasso", "rf", "xgb", "lgbm"] = Field(default="ols", description="AIPW outcome regressions")
    trim: float = Field(default=0.01, ge=0, le=0.2, description="clip propensity scores to [trim, 1 - trim]")
    k_neighbors: int = Field(default=1, ge=1, le=10)
    caliper: float = Field(default=0.2, ge=0, le=2, description="matching caliper in SDs of the logit propensity (0 = none)")
    crossfit: CrossFit = Field(default_factory=CrossFit)
    cluster: Literal["auto", "none", "entity", "date"] = "auto"


@register
class Propensity(Method):
    id = "propensity"
    family = "causal"
    title = "Propensity scores: IPW, AIPW and matching"
    description = ("Effect of a 0/1 treatment adjusting for observed covariates: cross-fitted propensity score (logit or "
                   "ML), normalized inverse-probability weighting, doubly robust AIPW and nearest-neighbour matching on "
                   "the logit score with a caliper; balance table (standardized mean differences before / after) and "
                   "overlap plot.")
    min_x = 1
    Params = PSParams
    outputs = ("ATE / ATT by estimator", "balance table (SMD, variance ratio)", "overlap plot", "love plot")
    diagnostics = ("max |SMD| after weighting / matching", "effective sample size", "common support")

    def extra_columns(self, params: PSParams) -> list[str]:
        return [params.treatment]

    def regressor_columns(self, params: PSParams) -> list[str]:
        return [params.treatment]

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        ps = C2.learner_cost("ols" if params.ps_model == "logit" else params.ps_model, n_rows, n_x)
        out = 2 * C2.learner_cost(params.outcome_model, n_rows, n_x) if params.estimator in ("all", "aipw") else 0
        match = n_rows * 2e-6 if params.estimator in ("all", "matching") else 0
        return 0.3 + params.crossfit.folds * (ps + out) + match

    def fit(self, data: FitData, p: PSParams) -> Result:
        if p.treatment in data.x or p.treatment == data.y:
            raise GuardrailError("bad_treatment", "the treatment column must not also be y or a covariate")
        df, dropped = data.complete([p.treatment])
        y, X = df[data.y].to_numpy(float), df[data.x].to_numpy(float)
        D = _binary(df[p.treatment].to_numpy(float), p.treatment)
        _need_n(len(y), 50, "Propensity-score estimation")
        if min(D.sum(), (~D).sum()) < 10:
            raise GuardrailError("too_few_obs", "need at least 10 treated and 10 control rows")
        res = Result(self.id, self.family, f"Propensity scores ({p.ps_model})")
        res.summary["rows_dropped_missing"] = dropped
        sets, scheme, purge = crossfit_folds(data, df, p.crossfit)
        folds = sets[0]
        g = groups_of(data, df, p.cluster)
        kind = "ols" if p.ps_model == "logit" else p.ps_model
        e_raw = C2.crossfit(kind, "classification", X, D.astype(float), folds, data.seed, check=data.check)
        e = np.clip(e_raw, max(p.trim, 1e-6), 1 - max(p.trim, 1e-6))
        rows, est = [], {}
        w = C2.ipw_weights(e, D, p.estimand)
        if p.estimator in ("all", "ipw"):
            o = C2.ipw(y, D, e, p.estimand, g)
            est["ipw"] = o
            rows.append((f"{p.estimand.upper()} (IPW)", o["tau"], o["se"], len(y)))
        if p.estimator in ("all", "aipw"):
            g0 = C2.crossfit(p.outcome_model, "regression", X, y, folds, data.seed, subset=~D, check=data.check)
            g1 = C2.crossfit(p.outcome_model, "regression", X, y, folds, data.seed, subset=D, check=data.check)
            o = C2.aipw(y, D, e, g0, g1, p.estimand, g)
            est["aipw"] = o
            rows.append((f"{p.estimand.upper()} (AIPW)", o["tau"], o["se"], len(y)))
        mt = None
        if p.estimator in ("all", "matching"):
            mt = C2.nn_match(y, D, e, p.k_neighbors, p.caliper)
            est["matching"] = mt
            rows.append(("ATT (NN matching)", mt["tau"], mt["se"], mt["n_matched"]))
            if mt["n_matched"] < mt["n_treated"]:
                res.info("caliper_drop", f"{mt['n_treated'] - mt['n_matched']} of {mt['n_treated']} treated rows have no "
                                         "control within the caliper and are dropped from the matched ATT")
        naive = float(y[D].mean() - y[~D].mean())
        rows.append(("difference in means (unadjusted)", naive, float(np.sqrt(y[D].var(ddof=1) / D.sum() + y[~D].var(ddof=1) / (~D).sum())), len(y)))
        _effect_rows(res, rows)
        # balance
        bal, love = [], []
        wm = None
        if mt is not None:
            wm = np.zeros(len(y))
            wm[mt["used"]] = 1.0
            wm[~D] = mt["K"][~D]
        for j, c in enumerate(data.x):
            x = X[:, j]
            t0, c0, s0, v0 = C2.smd(x, D)
            _, _, s1, v1 = C2.smd(x, D, w)
            s2_, v2 = (C2.smd(x, D, wm)[2:] if wm is not None else (None, None))
            bal.append([c, t0, c0, s0, s1, s2_, v0, v1, v2])
            love.append((c, abs(s0), abs(s1), abs(s2_) if s2_ is not None else None))
        res.add_table("balance", "Covariate balance (standardized mean difference; |SMD| < 0.1 is the usual target)",
                      ["covariate", "mean_treated", "mean_control", "smd_raw", f"smd_ipw_{p.estimand}", "smd_matched",
                       "var_ratio_raw", f"var_ratio_ipw_{p.estimand}", "var_ratio_matched"], clean(bal))
        res.line_plot("love", "Love plot: |SMD| before and after adjustment", [s for s in (
            series("raw", [r[0] for r in love], [r[1] for r in love]),
            series(f"IPW ({p.estimand})", [r[0] for r in love], [r[2] for r in love]),
            series("matched", [r[0] for r in love], [r[3] for r in love]) if wm is not None else None) if s],
            "covariate", "|SMD|", kind="bar", hlines=[{"y": 0.1, "label": "0.1"}])
        overlap_plot(res, "overlap", e, D)
        max_w = max(abs(r[4]) for r in bal)
        if max_w > 0.1:
            res.warn("imbalance", f"after weighting the largest |SMD| is {max_w:.2f} (> 0.1): the propensity model does "
                                  "not balance the covariates; try a flexible ps_model or check overlap")
        ess = {k: float(v.sum() ** 2 / (v ** 2).sum()) for k, v in (("treated", w[D]), ("control", w[~D]))}
        support = float(np.mean((e_raw[D] > e_raw[~D].max()) | (e_raw[D] < e_raw[~D].min())))
        ps_auc = C2.auc(D.astype(float), e_raw)
        res.fit_stats.update(clean({"ps_auc_oof": ps_auc, "ess_treated": ess["treated"], "ess_control": ess["control"],
                                    "share_treated_outside_support": support, "n_treated": int(D.sum()),
                                    "n_control": int((~D).sum()), "share_trimmed": float(np.mean(e != e_raw))}))
        if ps_auc > 0.9:
            res.warn("overlap", f"the propensity model separates the groups almost perfectly (AUC {ps_auc:.2f}): weak "
                                "overlap, weights are extreme and estimates extrapolate")
        head = "aipw" if "aipw" in est else ("ipw" if "ipw" in est else "matching")
        res.info("reading", "Selection on observables: causal only if x contains every confounder. AIPW is consistent "
                            "if either the propensity or the outcome model is right. SEs treat the cross-fitted score as given.")
        res.summary.update({"dependent": data.y, "treatment": p.treatment, "covariates": list(data.x), "estimand": p.estimand,
                            "headline": head, "ps_model": p.ps_model, "outcome_model": p.outcome_model,
                            "cv_scheme": scheme, "purge": purge, "cluster": "none" if g is None else p.cluster})
        res.metrics.update(clean({"effect": est[head]["tau"], "se": est[head]["se"], "naive_diff": naive,
                                  "max_smd_raw": max(abs(r[3]) for r in bal), "max_smd_weighted": max_w, "n_obs": len(y),
                                  **{f"tau_{k}": v["tau"] for k, v in est.items()}}))
        return res


# ------------------------------------------------------------------------------------------------ regression discontinuity


class RDParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    running: str = Field(description="running (forcing) variable column; treatment switches at the cutoff")
    cutoff: float = 0.0
    design: Literal["sharp", "fuzzy"] = "sharp"
    treatment: str | None = Field(default=None, description="fuzzy: 0/1 (or take-up share) treatment column")
    bandwidth: Literal["ik", "cct", "fixed"] = Field(
        default="ik", description="ik: Imbens-Kalyanaraman MSE-optimal; cct: same h with CCT robust bias-corrected inference as headline; fixed: h")
    h: float | None = Field(default=None, gt=0, description="fixed bandwidth (running-variable units)")
    kernel: Literal["triangular", "epanechnikov", "uniform"] = "triangular"
    cluster: Literal["auto", "none", "entity", "date"] = "auto"
    n_bins: int = Field(default=20, ge=5, le=100, description="bins per side in the RD plot")
    placebo: bool = Field(default=True, description="placebo cutoffs at the median of each side")


@register
class RDD(Method):
    id = "rdd"
    family = "causal"
    title = "Regression discontinuity (sharp / fuzzy)"
    description = ("Jump in y at a cutoff of a running variable: local-linear fits on each side with a triangular kernel, "
                   "Imbens-Kalyanaraman bandwidth, CCT-style robust bias-corrected CI (local quadratic at h = b), fuzzy "
                   "design by the Wald ratio; McCrary density test, covariate continuity (x), placebo cutoffs and "
                   "bandwidth sensitivity.")
    min_x = 0
    Params = RDParams
    outputs = ("RD estimate (conventional + robust)", "RD plot (bin means + local fits)", "bandwidth sensitivity",
               "covariate continuity", "placebo cutoffs")
    diagnostics = ("McCrary density test", "covariate jumps at the cutoff", "placebo cutoffs", "first-stage F (fuzzy)")

    def extra_columns(self, params: RDParams) -> list[str]:
        return [params.running] + ([params.treatment] if params.treatment else [])

    def regressor_columns(self, params: RDParams) -> list[str]:
        return self.extra_columns(params)

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.2 + n_rows * (12 + n_x) * 3e-7

    def fit(self, data: FitData, p: RDParams) -> Result:
        if p.design == "fuzzy" and not p.treatment:
            raise GuardrailError("missing_treatment", "a fuzzy RD needs the treatment column")
        if p.bandwidth == "fixed" and not p.h:
            raise GuardrailError("missing_h", "bandwidth 'fixed' needs h")
        df, dropped = data.complete(self.extra_columns(p))
        y = df[data.y].to_numpy(float)
        xc = df[p.running].to_numpy(float) - p.cutoff
        d = df[p.treatment].to_numpy(float) if p.design == "fuzzy" else None
        _need_n(len(y), 40, "Regression discontinuity")
        if (xc < 0).sum() < 10 or (xc >= 0).sum() < 10:
            raise GuardrailError("too_few_obs", "need at least 10 rows on each side of the cutoff")
        res = Result(self.id, self.family, f"{p.design.title()} regression discontinuity")
        res.summary["rows_dropped_missing"] = dropped
        g = groups_of(data, df, p.cluster)
        try:
            bw = C2.ik_bandwidth(xc, y, p.kernel)
        except ValueError as e:
            raise GuardrailError("too_few_obs", str(e)) from e
        h = float(p.h) if p.bandwidth == "fixed" else bw["h"]
        h = min(h, float(np.max(np.abs(xc))))
        try:
            conv = C2.rd_estimate(xc, y, h, p.kernel, 1, d, g)
            rob = C2.rd_estimate(xc, y, h, p.kernel, 2, d, g)
        except ValueError as e:
            raise GuardrailError("too_few_obs", str(e)) from e
        what = "LATE (fuzzy)" if d is not None else "jump"
        rows = [(f"{what}: conventional local-linear", conv["tau"], conv["se"], conv["n_left"] + conv["n_right"]),
                (f"{what}: robust bias-corrected (CCT, local quadratic)", rob["tau"], rob["se"], rob["n_left"] + rob["n_right"])]
        if d is not None:
            rows.append(("first stage: jump in treatment", conv["jump_d"], conv["se_d"], conv["n_left"] + conv["n_right"]))
            rows.append(("reduced form: jump in y", conv["jump_y"], conv["se_y"], conv["n_left"] + conv["n_right"]))
        _effect_rows(res, rows)
        head = rob if p.bandwidth == "cct" else conv
        res.add_table("bandwidth", "Bandwidth selection (Imbens-Kalyanaraman 2012)", ["quantity", "value"], clean([
            ["h used", h], ["h_IK", bw["h"]], ["pilot h1", bw["h_pilot"]], ["density at cutoff f(c)", bw["f0"]],
            ["curvature m2 left", bw["m2_left"]], ["curvature m2 right", bw["m2_right"]],
            ["regularization left", bw["reg_left"]], ["regularization right", bw["reg_right"]],
            ["rows left / right within h", f"{conv['n_left']} / {conv['n_right']}"], ["kernel", p.kernel]]))
        # RD plot
        xl, ml, xr, mr = C2.rd_bins(xc, y, p.n_bins)
        bL, bR = conv["coef"], conv["coef"]
        res.line_plot("rd_plot", f"RD plot: bin means and local-linear fits within h = {h:.4g}", [
            series("bins (left)", np.array(xl) + p.cutoff, ml), series("bins (right)", np.array(xr) + p.cutoff, mr),
            series("fit (left)", [p.cutoff - h, p.cutoff], [bL[0] - bL[2] * h, bL[0]]),
            series("fit (right)", [p.cutoff, p.cutoff + h], [bR[0] + bR[1], bR[0] + bR[1] + (bR[2] + bR[3]) * h])],
            p.running, data.y, kind="scatter", vlines=[{"x": p.cutoff, "label": "cutoff"}])
        # bandwidth sensitivity
        sens = []
        for m in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
            try:
                o = C2.rd_estimate(xc, y, h * m, p.kernel, 1, d, g)
                sens.append([round(h * m, 6), o["tau"], o["se"], o["n_left"] + o["n_right"]])
            except ValueError:
                continue
        res.add_table("bandwidth_sensitivity", "Estimate by bandwidth (multiples of h)", ["h", "estimate", "se", "n"], clean(sens))
        res.line_plot("bandwidth_sensitivity", "Estimate vs bandwidth (95% CI)", [series(
            "estimate", [f"{r[0]:.3g}" for r in sens], [r[1] for r in sens], ci_low=[r[1] - 1.96 * r[2] for r in sens],
            ci_high=[r[1] + 1.96 * r[2] for r in sens])], "bandwidth", "estimate", kind="errorbar")
        # McCrary
        mc = C2.mccrary(xc, h)
        res.add_test("McCrary density discontinuity", mc["z"], mc["p"], None, "no jump in the density of the running variable",
                     group="diagnostics", note=f"log density difference {mc['theta']:.3g}" if np.isfinite(mc["theta"]) else None)
        if np.isfinite(mc["p"]) and mc["p"] < 0.05:
            res.warn("manipulation", "the density of the running variable jumps at the cutoff: possible sorting / manipulation")
        if d is not None:
            res.fit_stats["first_stage_F"] = conv["first_stage_f"]
            res.add_test("First stage (jump in treatment)", conv["jump_d"] / conv["se_d"] if conv["se_d"] > 0 else None,
                         2 * stats.norm.sf(abs(conv["jump_d"] / conv["se_d"])) if conv["se_d"] > 0 else None,
                         None, "no jump in treatment take-up", group="inference")
            if (conv["first_stage_f"] or 0) < 10:
                res.warn("weak_first_stage", f"first-stage F = {conv['first_stage_f']:.1f} < 10: weak discontinuity in take-up, the fuzzy LATE is unreliable")
        # covariate continuity
        cov_rows = []
        for c in data.x:
            if c in (p.running, p.treatment):
                continue
            try:
                o = C2.rd_estimate(xc, df[c].to_numpy(float), h, p.kernel, 1, None, g)
                z = o["tau"] / o["se"] if o["se"] > 0 else np.nan
                pv = float(2 * stats.norm.sf(abs(z)))
                cov_rows.append([c, o["tau"], o["se"], pv])
            except ValueError:
                continue
        if cov_rows:
            res.add_table("covariate_continuity", "Covariates at the cutoff (should not jump)", ["covariate", "jump", "se", "p_value"], clean(cov_rows))
            bad = [r[0] for r in cov_rows if r[3] < 0.05]
            if bad:
                res.warn("covariate_jump", f"covariates jump at the cutoff: {', '.join(bad[:6])} (p < 0.05): the design may not be as good as random")
        if p.placebo:
            pl_rows = []
            for side, mask in (("left", xc < 0), ("right", xc >= 0)):
                c0 = float(np.median(xc[mask]))
                sub = mask
                xs = xc[sub] - c0
                try:
                    hb = min(h, float(np.max(np.abs(xs))))
                    o = C2.rd_estimate(xs, y[sub], hb, p.kernel, 1, None, g[sub] if g is not None else None)
                    z = o["tau"] / o["se"] if o["se"] > 0 else np.nan
                    pl_rows.append([side, c0 + p.cutoff, o["tau"], o["se"], float(2 * stats.norm.sf(abs(z)))])
                except ValueError:
                    continue
            if pl_rows:
                res.add_table("placebo", "Placebo cutoffs (median of each side; should show no jump)",
                              ["side", "cutoff", "estimate", "se", "p_value"], clean(pl_rows))
                if any(r[4] < 0.05 for r in pl_rows):
                    res.warn("placebo_jump", "a placebo cutoff shows a significant jump: the main estimate may reflect non-linearity, not the cutoff")
        res.summary.update({"dependent": data.y, "running": p.running, "cutoff": p.cutoff, "design": p.design,
                            "bandwidth": p.bandwidth, "h": h, "kernel": p.kernel,
                            "headline": "robust bias-corrected" if p.bandwidth == "cct" else "conventional",
                            "cluster": "none" if g is None else p.cluster})
        res.metrics.update(clean({"estimate": head["tau"], "se": head["se"], "h": h, "n_effective": head["n_left"] + head["n_right"],
                                  "estimate_robust": rob["tau"], "se_robust": rob["se"], "mccrary_p": mc["p"], "n_obs": len(y)}))
        res.info("reading", "The estimate is local to units at the cutoff. Robust bias-corrected inference follows Calonico, "
                            "Cattaneo & Titiunik (2014) with pilot bandwidth b = h (local-quadratic point estimate and SE); "
                            "the bandwidth itself is the IK plug-in, not the full CCT selector.")
        return res


# ------------------------------------------------------------------------------------------------ coefficient stability


class StabParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    treatment: str = Field(description="variable of interest; x = controls, added one at a time in the order given")
    r_max_mult: float = Field(default=1.3, ge=1, le=3, description="Oster R_max = min(1, mult × R² of the full model)")
    r_max: float | None = Field(default=None, gt=0, le=1, description="explicit R_max (overrides the multiplier)")
    delta: float = Field(default=1.0, ge=0, le=5, description="proportional selection δ for the bias-adjusted β*")
    cluster: Literal["auto", "none", "entity", "date"] = "auto"


@register
class CoefStability(Method):
    id = "coef_stability"
    family = "causal"
    title = "Coefficient stability (controls stepwise, Oster δ)"
    description = ("How the coefficient of a variable of interest moves as controls are added one at a time, with R²; "
                   "Oster (2019) bias-adjusted β* for a given δ and R_max, the δ that would drive the effect to zero, "
                   "and the Altonji-Elder-Taber ratio.")
    min_x = 1
    Params = StabParams
    outputs = ("stepwise table (β, SE, R²)", "β path with CI", "Oster β*, δ for β = 0, AET ratio")
    diagnostics = ("Oster δ > 1 = robust (rule of thumb)", "identified set [β̃, β*] excludes 0")

    def extra_columns(self, params: StabParams) -> list[str]:
        return [params.treatment]

    def regressor_columns(self, params: StabParams) -> list[str]:
        return [params.treatment]

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.1 + n_rows * (n_x + 1) ** 2 * 3e-8

    def fit(self, data: FitData, p: StabParams) -> Result:
        if p.treatment in data.x or p.treatment == data.y:
            raise GuardrailError("bad_treatment", "the variable of interest must not also be y or a control")
        df, dropped = data.complete([p.treatment])
        y, d, C = df[data.y].to_numpy(float), df[p.treatment].to_numpy(float), df[data.x].to_numpy(float)
        _need_n(len(y), len(data.x) + 10, "The stability table")
        res = Result(self.id, self.family, f"Coefficient stability of {p.treatment}")
        res.summary["rows_dropped_missing"] = dropped
        g = groups_of(data, df, p.cluster)
        steps = C2.coef_stability(y, d, C, g)
        labels = ["(none)"] + [f"+ {c}" for c in data.x]
        res.add_table("stability", f"Coefficient of {p.treatment} as controls are added", ["step", "added", "beta", "se",
                      "ci_low", "ci_high", "r2", "change_vs_short"], clean([[s["k"], labels[s["k"]], s["beta"], s["se"],
                      s["beta"] - 1.96 * s["se"], s["beta"] + 1.96 * s["se"], s["r2"], s["beta"] - steps[0]["beta"]] for s in steps]))
        res.line_plot("beta_path", f"β of {p.treatment} by step (95% CI)", [series(
            "beta", labels, [s["beta"] for s in steps], ci_low=[s["beta"] - 1.96 * s["se"] for s in steps],
            ci_high=[s["beta"] + 1.96 * s["se"] for s in steps])], "controls added", "beta", kind="errorbar")
        res.line_plot("r2_path", "R² by step", [series("R²", labels, [s["r2"] for s in steps])], "controls added", "R²")
        s0, sK = steps[0], steps[-1]
        rmax = p.r_max if p.r_max else min(1.0, p.r_max_mult * sK["r2"])
        if rmax <= sK["r2"]:
            res.warn("r_max", "R_max is not above the full-model R²: Oster's adjustment is undefined (set r_max or r_max_mult > 1)")
        o = C2.oster(s0["beta"], s0["r2"], sK["beta"], sK["r2"], rmax, p.delta)
        lo, hi = sorted([sK["beta"], o["beta_star"]])
        robust_set = not (lo <= 0 <= hi)
        res.add_table("oster", "Oster (2019) sensitivity to unobserved selection", ["quantity", "value"], clean([
            ["β short (no controls)", s0["beta"]], ["R² short", s0["r2"]], ["β full (all controls)", sK["beta"]],
            ["R² full", sK["r2"]], ["R_max", rmax], [f"β* (δ = {p.delta:g})", o["beta_star"]],
            ["identified set", f"[{lo:.4g}, {hi:.4g}]"], ["δ for β = 0", o["delta_for_zero"]],
            ["Altonji-Elder-Taber ratio", o["aet_ratio"]]]),
            note="approximation of Oster's estimator (eq. 3, Prop. 3); assumes selection on unobservables is δ times selection on the included controls")
        _effect_rows(res, [(f"{p.treatment}: short", s0["beta"], s0["se"], len(y)), (f"{p.treatment}: full", sK["beta"], sK["se"], len(y)),
                           (f"{p.treatment}: Oster β*", o["beta_star"], float("nan"), len(y))])
        dz = o["delta_for_zero"]
        sig_full = sK["se"] > 0 and abs(sK["beta"] / sK["se"]) > 1.96
        verdict = ("no effect: the full-model coefficient is not significant at 5%, so there is nothing for Oster's δ to explain away"
                   if not sig_full else
                   "robust: unobservables would need to be more than as important as the controls (|δ| > 1) to explain the effect away"
                   if np.isfinite(dz) and abs(dz) > 1 and robust_set else
                   "fragile: modest selection on unobservables (|δ| ≤ 1) or the identified set contains 0")
        res.info("reading", f"Oster δ for β = 0: {dz:.3g}; {verdict}.")
        res.summary.update({"dependent": data.y, "treatment": p.treatment, "controls": list(data.x), "r_max": rmax,
                            "delta": p.delta, "verdict": verdict.split(":")[0], "cluster": "none" if g is None else p.cluster})
        res.metrics.update(clean({"beta_short": s0["beta"], "beta_full": sK["beta"], "beta_star": o["beta_star"],
                                  "delta_for_zero": dz, "aet_ratio": o["aet_ratio"], "r2_full": sK["r2"], "n_obs": len(y)}))
        return res
