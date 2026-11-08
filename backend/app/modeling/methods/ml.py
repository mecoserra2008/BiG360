"""Phase C machine learning: regularized regression (Lasso / Ridge / ElasticNet), tree ensembles (RF / HistGBM /
XGBoost) with SHAP, GMM and density clustering, the factor-model builder and the model leaderboard (owner: model-studio)."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from .. import _ml as M
from .. import diagnostics as dg
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, series
from .clustering import FeatureParams, _check_n, _quality, cluster_outputs, feature_matrix, pca_scatter


class CVParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folds: int = Field(default=5, ge=2, le=20)
    scheme: Literal["auto", "expanding", "purged_kfold", "kfold"] = Field(
        default="auto", description="auto: expanding (time series), purged_kfold (panel), kfold (cross-section)")
    purge: int | None = Field(default=None, ge=0, le=60, description="periods dropped between train and test (default: target horizon, >= 1)")
    embargo: int = Field(default=0, ge=0, le=60)
    conformal_alpha: float | None = Field(
        default=None, gt=0.005, lt=0.5, description="Studio v2 S3: split-conformal prediction intervals on the out-of-fold "
        "predictions of any regressor (0.1 = 90 % intervals; calibrated on earlier / other folds only)")


def make_folds(data: FitData, df: pd.DataFrame, cv: CVParams) -> tuple[list, str, int]:
    scheme = cv.scheme
    if scheme == "auto":
        scheme = {"timeseries": "expanding", "panel": "purged_kfold"}.get(data.kind, "kfold")
    lead_k = int((data.meta.get("lead_k") or {}).get(data.y or "", 0))
    purge = cv.purge if cv.purge is not None else max(1, lead_k)
    folds = M.cv_splits(df["date"], scheme, cv.folds, purge, cv.embargo, seed=data.seed)
    data.meta["_conformal_alpha"] = cv.conformal_alpha  # read back by _oos_block (every CV regressor gets the option)
    if len(folds) < 2:
        raise GuardrailError("too_few_obs", "not enough periods for the requested cross-validation")
    return folds, scheme, purge


def _task(y: np.ndarray, task: str) -> str:
    if task != "auto":
        return task
    u = np.unique(y[np.isfinite(y)])
    return "classification" if len(u) <= 2 and set(u.tolist()) <= {0.0, 1.0} else "regression"


def _oos_block(res: Result, data: FitData, df: pd.DataFrame, y: np.ndarray, p: np.ndarray, task: str, folds: list,
               scheme: str, purge: int, label: str) -> dict[str, Any]:
    tested = np.zeros(len(y), bool)
    for _, te in folds:
        tested[te] = True
    m = (M.clf_metrics if task == "classification" else M.reg_metrics)(y[tested], p[tested])
    if data.is_panel and "entity_id" in df.columns:  # out-of-fold predictions -> backtest lab source `studio_run`
        fold = np.full(len(y), -1)
        for i, (_, te) in enumerate(folds):
            fold[te] = i
        res.artifacts["oos_predictions"] = pl.DataFrame({
            "entity_id": df["entity_id"].astype(str).to_numpy()[tested],
            "date": pl.Series(pd.to_datetime(df["date"]).dt.date.to_numpy()[tested].tolist(), dtype=pl.Date), "prediction": p[tested].astype(float),
            "fold": fold[tested].astype(int), "scheme": [scheme] * int(tested.sum()), "model": [label] * int(tested.sum())})
    if data.is_panel and task == "regression":
        ic, ic_t, nd = M.rank_ic(df["date"].to_numpy()[tested], y[tested], p[tested])
        m.update(rank_ic=ic, rank_ic_t=ic_t, ic_dates=nd)
    oos_eval_artifact(res, data, df, y, p, folds, label)
    alpha = (data.meta or {}).get("_conformal_alpha")
    if alpha and task == "regression":
        conformal_block(res, df, y, p, folds, float(alpha))
    res.evaluation = clean({"scheme": scheme, "horizon": 1, "n_folds": len(folds), "purge": purge, "model": label,
                            "metrics": {"model": m}, "note": "out-of-fold predictions; standardization / fitting "
                            "inside each training fold"})
    res.metrics.update(clean({f"oos_{k}": v for k, v in m.items() if k != "n"}))
    res.add_table("cv_folds", "Cross-validation folds", ["fold", "train_rows", "test_rows", "test_start", "test_end"],
                  clean([[i + 1, len(tr), len(te), df["date"].iloc[te].min(), df["date"].iloc[te].max()]
                         for i, (tr, te) in enumerate(folds)]))
    return m


def oos_eval_artifact(res: Result, data: FitData, df: pd.DataFrame, y: np.ndarray, p: np.ndarray, folds: list,
                      label: str, extra: dict[str, np.ndarray] | None = None) -> None:
    """Studio v2 S3: out-of-fold (date, entity, y, prediction, fold) for every CV model, time series included; the
    compare-runs tests (Diebold-Mariano / Clark-West), the equity curve and registry monitoring read it."""
    from .._prob import fold_index

    fi = fold_index(len(y), folds)
    t = fi >= 0
    ent = df["entity_id"].astype(str).to_numpy()[t] if "entity_id" in df.columns else np.array([""] * int(t.sum()))
    cols = {"date": pl.Series(pd.to_datetime(df["date"]).dt.date.to_numpy()[t].tolist(), dtype=pl.Date), "entity_id": ent, "y": y[t].astype(float),
            "prediction": np.asarray(p, float)[t], "fold": fi[t].astype(int), "model": [label] * int(t.sum())}
    for k, v in (extra or {}).items():
        cols[k] = np.asarray(v, float)[t]
    res.artifacts["oos_eval"] = pl.DataFrame(cols)


def conformal_block(res: Result, df: pd.DataFrame, y: np.ndarray, center: np.ndarray, folds: list, alpha: float,
                    scale: np.ndarray | None = None, lo: np.ndarray | None = None, hi: np.ndarray | None = None) -> dict[str, Any]:
    """Split-conformal intervals on the CV predictions (app/modeling/_prob.conformal_cv) -> table, ts_band plot, metrics."""
    from .. import _prob as P
    from ..results import ts_band

    c = P.conformal_cv(y, center, folds, alpha, scale, lo, hi)
    res.add_table("conformal", f"Split-conformal {1 - alpha:.0%} intervals on out-of-fold predictions ({c['score']} score)",
                  ["target_coverage", "empirical_coverage", "mean_width", "interval_score", "rows_with_interval"],
                  clean([[c["target"], c["coverage"], c["mean_width"], c["interval_score"], c["n_covered_rows"]]]),
                  note="each test fold is calibrated on the scores of rows in its own training set that were predicted out "
                       "of fold (walk-forward: earlier folds only); folds without 30 calibration rows get no interval")
    ok = np.isfinite(c["lower"])
    if ok.any() and "entity_id" not in df.columns:
        o = np.where(ok)[0]
        ts_band(res, "conformal_band", f"Out-of-fold prediction with {1 - alpha:.0%} conformal band", df["date"].to_numpy()[o],
                [("actual", y[o]), ("prediction", center[o])], [(f"{1 - alpha:.0%} conformal", c["lower"][o], c["upper"][o])])
    gap = c["coverage"] - c["target"] if np.isfinite(c["coverage"]) else None
    if gap is not None and gap < -0.05:
        res.warn("conformal_undercoverage", f"conformal coverage {c['coverage']:.1%} is below the {c['target']:.0%} target: "
                 "the residual distribution shifts across folds (non-exchangeable); widen with a lower alpha or a rolling window")
    res.metrics.update(clean({"conformal_coverage": c["coverage"], "conformal_target": c["target"],
                              "conformal_width": c["mean_width"], "conformal_interval_score": c["interval_score"]}))
    return c


# ------------------------------------------------------------------------------------------------ regularized


class RegParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["lasso", "ridge", "elasticnet"] = "elasticnet"
    l1_ratio: float = Field(default=0.5, gt=0, le=1)
    n_alphas: int = Field(default=50, ge=5, le=200)
    alpha: float | None = Field(default=None, gt=0, description="fixed penalty; null = choose by CV")
    selection: Literal["min", "1se"] = Field(default="min", description="CV rule: minimum MSE or the 1-SE (sparser) alpha")
    cv: CVParams = Field(default_factory=CVParams)


@register
class Regularized(Method):
    id = "regularized"
    family = "ml"
    title = "Regularized regression (Lasso / Ridge / ElasticNet)"
    description = ("Penalized linear regression on standardized features; penalty chosen by time-series-aware CV "
                   "(expanding window / purged K-fold); coefficient paths vs alpha, selected features, OOS metrics vs OLS.")
    min_x = 1
    Params = RegParams
    outputs = ("coefficients (original scale)", "coefficient paths (coef_path)", "CV curve", "OOS metrics")
    diagnostics = ("OOS R2 vs OLS", "selected features", "CV folds")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 0.3 + n_rows * n_x * params.cv.folds * params.n_alphas * 2e-8

    def fit(self, data: FitData, p: RegParams) -> Result:
        res = Result(self.id, self.family, p.model.title())
        df, dropped = data.complete()
        res.summary["rows_dropped_missing"] = dropped
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        dg.check_obs(res, len(y), 2)
        folds, scheme, purge = make_folds(data, df, p.cv)
        alphas = np.array([p.alpha]) if p.alpha else M.alpha_grid(X, y, p.model, p.l1_ratio, p.n_alphas)
        cvr = M.regularized_cv(X, y, folds, p.model, p.l1_ratio, alphas, data.check)
        k = 0 if p.alpha else (cvr["one_se"] if p.selection == "1se" else cvr["best"])
        alpha = float(alphas[k])
        coef, b0, bstd = M.fit_regularized(X, y, p.model, alpha, p.l1_ratio)
        rows = [{"term": "const", "estimate": b0, "std_error": None, "statistic": None, "p_value": None, "ci_low": None,
                 "ci_high": None}]
        for n, c, cs in zip(data.x, coef, bstd, strict=True):
            rows.append({"term": n, "estimate": c, "std_error": None, "statistic": None, "p_value": None, "ci_low": None,
                         "ci_high": None, "std_estimate": cs, "selected": bool(abs(cs) > 1e-10)})
        res.coefficients = clean(rows)
        if len(alphas) > 1:
            paths = M.path_fit(X, y, p.model, alphas, p.l1_ratio)
            res.plots["coef_path"] = clean({"kind": "coef_path", "title": f"{p.model} coefficient paths (standardized)",
                                            "x_label": "log10(alpha)", "y_label": "coefficient", "x": np.log10(alphas),
                                            "paths": [{"term": n, "y": paths[:, j]} for j, n in enumerate(data.x)],
                                            "selected_x": float(np.log10(alpha)),
                                            "min_x": float(np.log10(alphas[cvr["best"]])), "one_se_x": float(np.log10(alphas[cvr["one_se"]]))})
            res.line_plot("cv_curve", "Cross-validated MSE", [series("CV MSE", np.log10(alphas), cvr["mse"],
                                                                     ci_low=cvr["mse"] - cvr["mse_se"], ci_high=cvr["mse"] + cvr["mse_se"])],
                          "log10(alpha)", "MSE", kind="errorbar")
        m = _oos_block(res, data, df, y, cvr["preds"][k], "regression", folds, scheme, purge, p.model)
        ols = M.cv_predict(lambda: _OLS(), X, y, folds, "regression")
        mo = M.reg_metrics(y[np.isfinite(ols)], ols[np.isfinite(ols)])
        res.evaluation["metrics"]["ols"] = clean(mo)
        sel = [r["term"] for r in rows[1:] if r["selected"]]
        res.summary.update({"dependent": data.y, "n_obs": len(y), "alpha": alpha, "model": p.model,
                            "n_selected": len(sel), "selected": sel, "cv_scheme": scheme})
        res.fit_stats.update(clean({"alpha": alpha, "n_selected": len(sel), "oos_rmse": m["rmse"], "ols_oos_rmse": mo["rmse"]}))
        res.metrics.update(clean({"n_obs": len(y), "n_selected": len(sel)}))
        if mo["rmse"] and m["rmse"] > mo["rmse"]:
            res.info("ols_better", "plain OLS has a lower out-of-fold RMSE than the penalized model")
        return res


class _OLS:
    def fit(self, X: np.ndarray, y: np.ndarray) -> "_OLS":
        A = np.column_stack([np.ones(len(X)), X])
        self.b = np.linalg.lstsq(A, y, rcond=None)[0]
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        return self.b[0] + X @ self.b[1:]


# ------------------------------------------------------------------------------------------------ tree ensembles


class TreeParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["rf", "hgb", "xgb"] = "hgb"
    task: Literal["auto", "regression", "classification"] = "auto"
    n_estimators: int = Field(default=300, ge=10, le=3000)
    max_depth: int | None = Field(default=None, ge=1, le=32)
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    min_samples_leaf: int = Field(default=20, ge=1, le=10000)
    cv: CVParams = Field(default_factory=CVParams)
    shap: bool = Field(default=False, description="SHAP values on a sample (heavy extra, opt-in since S1.4; presets turn it on)")
    shap_sample: int = Field(default=2000, ge=100, le=20000)
    pdp_features: int = Field(default=4, ge=0, le=12)
    permutation: bool = True


@register
class TreeEnsemble(Method):
    id = "tree_ensemble"
    family = "ml"
    title = "Tree ensemble (random forest / gradient boosting)"
    description = ("Random forest, sklearn HistGradientBoosting or XGBoost for regression or classification with "
                   "purged / walk-forward CV; impurity vs permutation importance, SHAP summary (mean |SHAP| + beeswarm "
                   "data), partial dependence.")
    min_x = 1
    Params = TreeParams
    outputs = ("OOS metrics", "importance (impurity vs permutation)", "shap_beeswarm", "partial dependence")
    diagnostics = ("OOS AUC / RMSE / rank IC", "train vs OOS gap")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 1.0 + n_rows * max(n_x, 1) * params.n_estimators * (params.cv.folds + 2) * 4e-9

    def fit(self, data: FitData, p: TreeParams) -> Result:
        from sklearn.inspection import partial_dependence, permutation_importance

        df, dropped = data.complete()
        # float32 features for rf / xgb (S1.5): both cast to float32 internally anyway (same result, half the memory)
        X, y = df[data.x].to_numpy(np.float32 if p.model in ("rf", "xgb") else float), df[data.y].to_numpy(float)
        task = _task(y, p.task)
        res = Result(self.id, self.family, f"{ {'rf': 'Random forest', 'hgb': 'HistGradientBoosting', 'xgb': 'XGBoost'}[p.model]} ({task})")
        res.summary["rows_dropped_missing"] = dropped
        _check_n(len(y), 50, "a tree ensemble")
        folds, scheme, purge = make_folds(data, df, p.cv)
        prm = p.model_dump()
        fac = lambda: M.make_tree(p.model, task, prm, data.seed)  # noqa: E731
        oos = M.cv_predict(fac, X, y, folds, task, data.check)
        mo = _oos_block(res, data, df, y, oos, task, folds, scheme, purge, p.model)
        data.check()
        model = fac().fit(X, y)
        ins = (M.clf_metrics if task == "classification" else M.reg_metrics)(y, M.model_predict(model, X, task))
        res.fit_stats.update(clean({f"in_sample_{k}": v for k, v in ins.items() if k != "n"}))
        key = "auc" if task == "classification" else "rmse"
        if task == "classification" and ins["auc"] - (mo["auc"] or 0) > 0.15:
            res.warn("overfit", f"in-sample AUC {ins['auc']:.2f} vs OOS {mo['auc']:.2f}: strong overfitting")
        imp = getattr(model, "feature_importances_", None)
        tr, te = folds[-1]
        perm = None
        if p.permutation:
            data.check()
            mf = fac().fit(X[tr], y[tr])
            pi = permutation_importance(mf, X[te], y[te], n_repeats=5, random_state=data.seed,
                                        scoring="roc_auc" if task == "classification" else "neg_root_mean_squared_error")
            perm = pi.importances_mean
        rows = []
        for j, n in enumerate(data.x):
            rows.append([n, float(imp[j]) if imp is not None else None, float(perm[j]) if perm is not None else None])
        order = sorted(range(len(rows)), key=lambda j: -(rows[j][2] if rows[j][2] is not None else (rows[j][1] or 0)))
        if imp is None:
            res.info("no_impurity", "HistGradientBoosting has no impurity importance: only permutation importance is reported")
            res.add_table("importance", "Feature importance (permutation on the last test fold)", ["feature", "permutation"],
                          clean([[rows[j][0], rows[j][2]] for j in order]))
        else:
            res.add_table("importance", "Feature importance (impurity / gain vs permutation on the last test fold)",
                          ["feature", "impurity", "permutation"], clean([rows[j] for j in order]))
        res.line_plot("importance", "Feature importance", [s for s in (
            series("impurity", [rows[j][0] for j in order], [rows[j][1] for j in order]) if imp is not None else None,
            series("permutation", [rows[j][0] for j in order], [rows[j][2] for j in order]) if perm is not None else None) if s],
            "feature", "importance", kind="bar")
        rng = np.random.default_rng(data.seed)
        idx = rng.choice(len(X), min(len(X), p.shap_sample), replace=False)
        if p.shap:
            data.check()
            try:
                sv = M.shap_values(model, X[idx], task)
                ma = np.abs(sv).mean(0)
                so = np.argsort(-ma)
                feats = []
                for j in so[:30]:
                    v = X[idx, j]
                    lo, hi = np.nanpercentile(v, 2), np.nanpercentile(v, 98)
                    vn = np.clip((v - lo) / (hi - lo), 0, 1) if hi > lo else np.full(len(v), 0.5)
                    sub = rng.choice(len(v), min(len(v), 500), replace=False)
                    feats.append({"feature": data.x[j], "mean_abs": float(ma[j]), "shap": sv[sub, j], "value": vn[sub]})
                res.plots["shap_beeswarm"] = clean({"kind": "shap_beeswarm", "title": "SHAP values (sample)",
                                                    "x_label": "SHAP value", "features": feats})
                res.add_table("shap", "Mean |SHAP|", ["feature", "mean_abs_shap"], clean([[data.x[j], ma[j]] for j in so]))
            except Exception as e:  # noqa: BLE001
                res.info("shap_failed", f"SHAP not computed: {type(e).__name__}: {e}")
        top = [rows[j][0] for j in order[: p.pdp_features]]
        for f in top:
            data.check()
            j = data.x.index(f)
            pdr = partial_dependence(model, X[idx], [j], grid_resolution=20, kind="average")
            res.line_plot(f"pdp_{f}", f"Partial dependence: {f}", [series(f, pdr["grid_values"][0], pdr["average"][0])],
                          f, "prediction")
        res.summary.update({"dependent": data.y, "n_obs": len(y), "task": task, "model": p.model, "cv_scheme": scheme,
                            "features": list(data.x)})
        res.metrics.update(clean({"n_obs": len(y), f"oos_{key}": mo.get(key)}))
        return res


# ------------------------------------------------------------------------------------------------ clustering


class GMMParams(FeatureParams):
    k_min: int = Field(default=1, ge=1, le=30)
    k_max: int = Field(default=8, ge=1, le=30)
    covariance_type: Literal["full", "tied", "diag", "spherical"] = "full"


@register
class GMM(Method):
    id = "gmm"
    family = "clustering"
    title = "Gaussian mixture clustering"
    description = "Gaussian mixture with k chosen by BIC; soft assignments (membership probabilities), profiles, PCA view."
    needs_y = False
    min_x = 1
    Params = GMMParams
    outputs = ("BIC / AIC by k", "soft assignments", "profiles", "PCA scatter")
    diagnostics = ("BIC", "average max membership probability", "silhouette")

    def fit(self, data: FitData, p: GMMParams) -> Result:
        res = Result(self.id, self.family, self.title)
        Xs, names, info, raw = feature_matrix(data, p, res)
        _check_n(len(Xs), 10, "GMM")
        g, rows = M.gmm_select(Xs, p.k_min, min(p.k_max, len(Xs) - 1), p.covariance_type, data.seed)
        res.add_table("k_selection", "Model selection (lower BIC is better)", ["k", "bic", "aic"], clean(rows))
        res.line_plot("bic", "BIC by number of components", [series("BIC", [r[0] for r in rows], [r[1] for r in rows]),
                                                            series("AIC", [r[0] for r in rows], [r[2] for r in rows])], "k", "criterion")
        prob = g.predict_proba(Xs)
        labels = prob.argmax(1)
        if len(np.unique(labels)) > 1:
            _quality(res, Xs, labels)
        cluster_outputs(res, labels, Xs, raw, names, info)
        pm = prob.max(1)
        lab_p = dict(zip(info["label"], pm, strict=True))
        for mbr in res.clusters["members"]:
            mbr["probability"] = clean(lab_p.get(mbr["label"]))
        res.clusters.update({"soft": True, "noise_count": 0, "mean_max_probability": float(pm.mean())})
        pca_scatter(res, Xs, info, labels, data.seed)
        k = int(g.n_components)
        res.summary.update({"n_obs": len(Xs), "k": k, "covariance_type": p.covariance_type})
        res.fit_stats.update(clean({"bic": g.bic(Xs), "aic": g.aic(Xs), "k": k, "mean_max_probability": pm.mean()}))
        res.metrics.update(clean({"k": k, "bic": g.bic(Xs), "n_obs": len(Xs)}))
        if pm.mean() < 0.7:
            res.warn("fuzzy", f"average max membership probability {pm.mean():.2f}: overlapping components")
        return res


class DensityParams(FeatureParams):
    algorithm: Literal["hdbscan", "dbscan"] = "hdbscan"
    eps: float = Field(default=0.5, gt=0, description="DBSCAN neighbourhood radius (standardized units)")
    min_samples: int = Field(default=5, ge=1, le=500)
    min_cluster_size: int = Field(default=10, ge=2, le=5000, description="HDBSCAN")


@register
class DensityClustering(Method):
    id = "density_clustering"
    family = "clustering"
    title = "Density clustering (HDBSCAN / DBSCAN)"
    description = "HDBSCAN or DBSCAN: arbitrary-shape clusters plus noise points (cluster -1)."
    needs_y = False
    min_x = 1
    Params = DensityParams
    outputs = ("clusters + noise", "profiles", "PCA scatter")
    diagnostics = ("noise share", "silhouette (non-noise)")

    def fit(self, data: FitData, p: DensityParams) -> Result:
        res = Result(self.id, self.family, f"{p.algorithm.upper()} clustering")
        Xs, names, info, raw = feature_matrix(data, p, res)
        _check_n(len(Xs), 10, "density clustering")
        labels = M.density_cluster(Xs, p.algorithm, p.eps, p.min_samples, p.min_cluster_size)
        noise = labels == -1
        k = len(set(labels[~noise].tolist()))
        if k == 0:
            raise GuardrailError("no_clusters", "every point is noise: lower min_cluster_size / raise eps")
        if k > 1:
            _quality(res, Xs[~noise], labels[~noise])
        cluster_outputs(res, labels[~noise], Xs[~noise], raw[~noise], names, info.loc[~noise].reset_index(drop=True))
        nz = info.loc[noise]
        res.clusters["members"] += [{"cluster": -1, "label": r["label"], "entity_id": r.get("entity_id"),
                                     "sector_id": r.get("sector_id"), "distance_to_center": None} for _, r in nz.iterrows()][:3000]
        res.clusters.update({"noise_count": int(noise.sum()), "soft": False})
        pca_scatter(res, Xs, info, labels, data.seed)
        share = float(noise.mean())
        res.summary.update({"n_obs": len(Xs), "k": k, "noise_share": share, "algorithm": p.algorithm})
        res.fit_stats.update({"k": k, "noise_share": share})
        res.metrics.update({"k": k, "noise_share": share, "n_obs": len(Xs)})
        if share > 0.5:
            res.warn("mostly_noise", f"{share:.0%} of points are noise")
        return res


# ------------------------------------------------------------------------------------------------ factor model


class FactorParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor_scores: list[str] = Field(default_factory=list, description="score columns -> long-short quantile factors")
    quantiles: int = Field(default=5, ge=2, le=10)
    include_market: bool = Field(default=True, description="add the equal-weight market return of the panel")
    test_assets: Literal["portfolios", "entities"] = Field(default="portfolios", description="quantile portfolios of each score, or entities (balanced subset)")
    max_entities: int = Field(default=60, ge=2, le=500)
    hac_lags: int | None = Field(default=None, ge=0, le=24)


@register
class FactorModel(Method):
    id = "factor_model"
    family = "panel"
    title = "Factor model builder (time-series + cross-sectional)"
    description = ("Builds long-short quantile factor returns from score columns (sorted on last period's score), "
                   "optionally adds the market and user factors (x columns, time-series variables), runs time-series "
                   "regressions of test assets (quantile portfolios or entities) on the factors (alphas, betas, GRS "
                   "test) and a second-pass cross-sectional (Fama-MacBeth) regression of returns on the betas.")
    shapes = ("panel",)
    min_x = 0
    Params = FactorParams
    outputs = ("factor return series + stats", "alphas / betas (GRS)", "second-pass premia", "beta heatmap")
    diagnostics = ("GRS F-test", "alpha t-stats", "factor Sharpe ratios")

    def extra_columns(self, params: FactorParams) -> list[str]:
        return list(params.factor_scores)

    def regressor_columns(self, params: FactorParams) -> list[str]:
        return list(params.factor_scores)

    def fit(self, data: FitData, p: FactorParams) -> Result:
        from .. import _algos as A

        res = Result(self.id, self.family, self.title)
        if not p.factor_scores and not data.x and not p.include_market:
            raise GuardrailError("no_factors", "give factor_scores, user factors (x) or include_market")
        df, _ = data.complete(list(p.factor_scores))
        y = data.y
        F = {}
        stats_rows = []
        ann = {"D": 252, "W": 52, "M": 12, "Q": 4}[data.freq]
        for s in p.factor_scores:
            ls = M.long_short_factor(df, s, y, p.quantiles).set_index("date")["ls"]
            F[f"{s}_LS"] = ls
        if p.include_market:
            F["MKT"] = df.groupby("date")[y].mean()
        for c in data.x:
            F[c] = df.groupby("date")[c].first()
        F = pd.DataFrame(F).sort_index().dropna()
        if len(F) < 24:
            raise GuardrailError("too_few_obs", f"only {len(F)} periods with all factor returns")
        for c in F.columns:
            v = F[c].to_numpy()
            m, sd = float(v.mean()), float(v.std(ddof=1))
            stats_rows.append([c, m, sd, m / sd * np.sqrt(ann) if sd else None, m / (sd / np.sqrt(len(v))) if sd else None])
        res.add_table("factors", "Factor returns", ["factor", "mean", "sd", "sharpe_ann", "t_mean"], clean(stats_rows))
        dates = list(F.index)
        res.line_plot("factor_cum", "Cumulative factor returns", [series(c, dates, np.cumsum(F[c].to_numpy())) for c in F.columns],
                      "date", "cumulative return")
        if p.test_assets == "portfolios":
            if not p.factor_scores:
                raise GuardrailError("no_assets", "test_assets=portfolios needs factor_scores")
            R = pd.concat([M.quantile_portfolios(df, s, y, p.quantiles) for s in p.factor_scores], axis=1)
        else:
            W = df.pivot_table(index="date", columns="entity_label", values=y, aggfunc="first")
            W = W.loc[:, W.notna().mean() >= 0.95].dropna()
            R = W.iloc[:, : p.max_entities]
        try:
            ts = M.ts_factor_regressions(R, F, p.hac_lags)
        except ValueError as e:
            raise GuardrailError("too_few_obs", str(e)) from e
        tb = ts["table"]
        res.add_table("time_series", "Time-series regressions (HAC)", list(tb.columns), clean(tb.values.tolist()))
        res.add_test("GRS (all alphas = 0)", ts["grs"], ts["grs_p"], [ts["N"], ts["T"] - ts["N"] - ts["L"]],
                     "the factors price the test assets", group="specification", passed=None,
                     conclusion="alphas jointly significant: model rejected" if ts["grs_p"] < 0.05 else "model not rejected")
        bcols = [c for c in tb.columns if c.startswith("beta_")]
        res.plots["beta_heatmap"] = clean({"kind": "heatmap", "title": "Factor loadings (betas)", "x": [c[5:] for c in bcols],
                                           "y": tb["asset"].tolist(), "z": tb[bcols].to_numpy()})
        res.line_plot("alphas", "Alphas by test asset", [series("alpha", tb["asset"].tolist(), tb["alpha"].to_numpy(),
                                                                ci_low=(tb["alpha"] - 1.96 * tb["alpha"] / tb["alpha_t"]).to_numpy(),
                                                                ci_high=(tb["alpha"] + 1.96 * tb["alpha"] / tb["alpha_t"]).to_numpy())],
                      "asset", "alpha per period", kind="errorbar")
        # second pass: cross-sectional regressions of asset returns on full-sample betas, per date
        J = ts["joined"]
        long = J[list(R.columns)].stack().rename("r").reset_index()
        long.columns = ["date", "asset", "r"]
        B = tb.set_index("asset")[bcols]
        long = long.join(B, on="asset")
        try:
            fm = A.fama_macbeth(long, "r", bcols, None, len(bcols) + 2)
            rows = fm["table"].drop(columns=["share_positive"]).to_dict("records")
            for r in rows:
                r["term"] = "lambda_" + r["term"].replace("beta_", "") if r["term"] != "const" else "const (zero-beta)"
            res.coefficients = clean(rows)
            res.info("shanken", "second-pass SEs ignore beta estimation error (no Shanken correction)")
        except ValueError as e:
            res.info("second_pass_skipped", str(e))
        mean_abs_alpha = float(np.mean(np.abs(tb["alpha"])))
        res.fit_stats.update(clean({"grs": ts["grs"], "grs_p": ts["grs_p"], "T": ts["T"], "N": ts["N"], "L": ts["L"],
                                    "mean_abs_alpha": mean_abs_alpha, "avg_r2": float(tb["r2"].mean())}))
        res.summary.update({"dependent": y, "factors": list(F.columns), "test_assets": p.test_assets, "n_obs": ts["T"] * ts["N"]})
        res.metrics.update(clean({"grs_p": ts["grs_p"], "mean_abs_alpha": mean_abs_alpha, "avg_r2": float(tb["r2"].mean()),
                                  "n_obs": ts["T"] * ts["N"]}))
        return res


# ------------------------------------------------------------------------------------------------ leaderboard


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    method: Literal["mean", "ols", "logit", "ridge", "lasso", "elasticnet", "rf", "hgb", "xgb", "arima"]
    params: dict[str, Any] = Field(default_factory=dict)
    x: list[str] | None = Field(default=None, description="feature subset (default: the study's x)")


class LeaderboardParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[Candidate] = Field(min_length=2, max_length=12)
    task: Literal["auto", "regression", "classification"] = "auto"
    metric: Literal["rmse", "mae", "auc", "logloss"] | None = None
    cv: CVParams = Field(default_factory=CVParams)


@register
class Leaderboard(Method):
    id = "leaderboard"
    family = "ml"
    title = "Model leaderboard"
    description = ("Several candidate models on the same dataset / target with identical CV folds, ranked by an "
                   "out-of-sample metric, with Diebold-Mariano tests of each model against the best.")
    min_x = 0
    Params = LeaderboardParams
    outputs = ("ranking table", "DM tests vs best", "OOS metric bars", "cumulative loss difference")
    diagnostics = ("Diebold-Mariano",)

    def extra_columns(self, params: LeaderboardParams) -> list[str]:
        return sorted({c for cd in params.candidates for c in (cd.x or [])})

    def regressor_columns(self, params: LeaderboardParams) -> list[str]:
        return self.extra_columns(params)

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        heavy = sum(1 for c in params.candidates if c.method in ("rf", "hgb", "xgb", "arima", "lasso", "ridge", "elasticnet"))
        return 0.5 + params.cv.folds * (len(params.candidates) * 0.05 + heavy * (0.4 + n_rows * n_x * 2e-6))

    def fit(self, data: FitData, p: LeaderboardParams) -> Result:
        res = Result(self.id, self.family, self.title)
        allx = sorted(set(data.x) | set(self.extra_columns(p)))
        df, dropped = data.complete([c for c in allx if c not in data.x])
        y = df[data.y].to_numpy(float)
        task = _task(y, p.task)
        metric = p.metric or ("auc" if task == "classification" else "rmse")
        folds, scheme, purge = make_folds(data, df, p.cv)
        if any(c.method == "arima" for c in p.candidates) and (data.is_panel or scheme != "expanding"):
            raise GuardrailError("arima_scheme", "ARIMA candidates need a time series with the expanding CV scheme")
        tested = np.zeros(len(y), bool)
        for _, te in folds:
            tested[te] = True
        preds, rows = {}, []
        for cd in p.candidates:
            cols = cd.x if cd.x is not None else list(data.x)
            X = df[cols].to_numpy(float) if cols else np.empty((len(y), 0))
            pr = np.full(len(y), np.nan)
            for tr, te in folds:
                data.check()
                try:
                    pr[te] = M.candidate_predict(cd.model_dump(), X[tr], y[tr], X[te], task, data.seed)
                except Exception as e:  # noqa: BLE001
                    res.warn("candidate_failed", f"{cd.name}: {type(e).__name__}: {e}")
                    break
            preds[cd.name] = pr
            mm = (M.clf_metrics if task == "classification" else M.reg_metrics)(y[tested], pr[tested])
            rows.append({"model": cd.name, "method": cd.method, **mm})
        lower_better = metric in ("rmse", "mae", "logloss")
        ok = [r for r in rows if np.isfinite(r.get(metric, np.nan))]
        if not ok:
            raise GuardrailError("all_failed", "no candidate produced predictions")
        ok.sort(key=lambda r: r[metric] if lower_better else -r[metric])
        best = ok[0]["model"]

        def loss(pr: np.ndarray) -> np.ndarray:
            yt, pt = y[tested], pr[tested]
            if task == "classification":
                pt = np.clip(pt, 1e-9, 1 - 1e-9)
                return -(yt * np.log(pt) + (1 - yt) * np.log(1 - pt))
            return np.abs(yt - pt) if metric == "mae" else (yt - pt) ** 2

        order = np.argsort(pd.to_datetime(df["date"].to_numpy()[tested]), kind="stable")
        lb = loss(preds[best])[order]
        for rank, r in enumerate(ok, 1):
            r["rank"] = rank
            if r["model"] == best:
                r["dm_stat"], r["dm_p"] = None, None
                continue
            stat, pv = M.dm_test(loss(preds[r["model"]])[order], lb)
            r["dm_stat"], r["dm_p"] = stat, pv
            res.add_test(f"Diebold-Mariano: {r['model']} vs {best}", stat, pv, None, "equal predictive accuracy",
                         group="inference", passed=None,
                         conclusion=f"{best} significantly better (5%)" if (pv is not None and pv < 0.05 and stat > 0) else "not significantly different")
        cols = ["rank", "model", "method", metric] + [k for k in ok[0] if k not in ("rank", "model", "method", metric)]
        res.add_table("leaderboard", f"Leaderboard by OOS {metric} ({scheme}, {len(folds)} folds, identical splits)", cols,
                      clean([[r.get(c) for c in cols] for r in ok]))
        res.line_plot("leaderboard", f"OOS {metric}", [series(metric, [r["model"] for r in ok], [r[metric] for r in ok])],
                      "model", metric, kind="bar")
        dts = pd.to_datetime(df["date"].to_numpy()[tested])[order]
        res.line_plot("cum_loss_diff", f"Cumulative loss difference vs {best} (above 0 = worse than best)",
                      [series(r["model"], [d.date() for d in dts], np.nancumsum(loss(preds[r["model"]])[order] - lb))
                       for r in ok[1:]], "date", "cumulative loss difference")
        res.summary.update({"dependent": data.y, "n_obs": int(tested.sum()), "task": task, "metric": metric, "best": best,
                            "cv_scheme": scheme, "purge": purge})
        res.metrics.update(clean({"best_" + metric: ok[0][metric], "n_candidates": len(rows), "n_obs": int(tested.sum())}))
        res.coefficients = []
        res.evaluation = clean({"scheme": scheme, "horizon": 1, "n_folds": len(folds), "purge": purge,
                                "metrics": {r["model"]: {k: r.get(k) for k in ("rmse", "mae", "auc", "logloss", "n") if k in r}
                                            for r in ok}})
        return res
