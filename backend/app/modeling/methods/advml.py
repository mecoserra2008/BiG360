"""Studio v2 S2 advanced ML: LightGBM, Optuna tuning on purged walk-forward CV, stacking with a DM test against the
best base model, and learning-to-rank (LightGBM LambdaRank by date) (owner: model-studio). Core: app/modeling/_advml.py.
Every model is compared with a linear baseline on the same folds; the result says so when it does not beat it."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
import pandas as pd
import polars as pl
from pydantic import BaseModel, ConfigDict, Field

from .. import _advml as A
from .. import _ml as M
from ..registry import FitData, Method, register
from ..results import GuardrailError, Result, clean, series
from .ml import CVParams, _oos_block, _task, make_folds

Task = Literal["auto", "regression", "classification"]


def tested_mask(n: int, folds: list) -> np.ndarray:
    t = np.zeros(n, bool)
    for _, te in folds:
        t[te] = True
    return t


def dm_vs(res: Result, df: pd.DataFrame, y: np.ndarray, p_model: np.ndarray, p_ref: np.ndarray, task: str,
          mask: np.ndarray, label: str, ref: str, horizon: int = 1) -> dict[str, Any]:
    """Diebold-Mariano on the date-ordered loss differential (ref - model): stat > 0 = the model is more accurate.
    Adds the test and an honest reading; returns stat, p, mean losses and the verdict."""
    ok = mask & np.isfinite(p_model) & np.isfinite(p_ref)
    order = np.argsort(pd.to_datetime(df["date"].to_numpy()[ok]), kind="stable")
    lm = A.loss_vec(y[ok], p_model[ok], task)[order]
    lr = A.loss_vec(y[ok], p_ref[ok], task)[order]
    stat, pv = M.dm_test(lr, lm, max(horizon, 1))
    better = bool(np.mean(lm) < np.mean(lr))
    sig = pv is not None and np.isfinite(pv) and pv < 0.05
    verdict = (f"{label} beats {ref} (DM p = {pv:.3g})" if better and sig else
               f"{label} does not significantly beat {ref} (DM p = {pv:.3g})" if better else
               f"{label} is worse than {ref} out of sample (mean loss {np.mean(lm):.4g} vs {np.mean(lr):.4g})")
    res.add_test(f"Diebold-Mariano: {label} vs {ref}", stat, pv, None, "equal predictive accuracy", group="inference",
                 passed=None, conclusion=verdict)
    if not (better and sig):
        res.warn(f"no_gain_vs_{ref.replace(' ', '_')}", verdict + ": treat its extra complexity as unproven",
                 "warning" if not better else "info")
    return {"dm_stat": stat, "dm_p": pv, "loss_model": float(np.mean(lm)), "loss_ref": float(np.mean(lr)),
            "beats": bool(better and sig), "verdict": verdict}


def linear_oos(X: np.ndarray, y: np.ndarray, folds: list, task: str, check: Any = None) -> np.ndarray:
    return A.cv_fit_predict("linear", task, {}, X, y, folds, 0, check)


def metrics_of(y: np.ndarray, p: np.ndarray, task: str) -> dict[str, float]:
    ok = np.isfinite(p)
    return (M.clf_metrics if task == "classification" else M.reg_metrics)(y[ok], p[ok])


def beeswarm(res: Result, names: list[str], X: np.ndarray, sv: np.ndarray, seed: int) -> None:
    rng = np.random.default_rng(seed)
    ma = np.abs(sv).mean(0)
    so = np.argsort(-ma)
    feats = []
    for j in so[:30]:
        v = X[:, j]
        lo, hi = np.nanpercentile(v, 2), np.nanpercentile(v, 98)
        vn = np.clip((v - lo) / (hi - lo), 0, 1) if hi > lo else np.full(len(v), 0.5)
        sub = rng.choice(len(v), min(len(v), 500), replace=False)
        feats.append({"feature": names[j], "mean_abs": float(ma[j]), "shap": sv[sub, j], "value": vn[sub]})
    res.plots["shap_beeswarm"] = clean({"kind": "shap_beeswarm", "title": "SHAP values (LightGBM TreeSHAP, sample)",
                                        "x_label": "SHAP value", "features": feats})
    res.add_table("shap", "Mean |SHAP|", ["feature", "mean_abs_shap"], clean([[names[j], ma[j]] for j in so]))


def _lead(data: FitData) -> int:
    return int((data.meta.get("lead_k") or {}).get(data.y or "", 0))


# ------------------------------------------------------------------------------------------------ LightGBM


class LGBMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: Task = "auto"
    n_estimators: int = Field(default=500, ge=10, le=5000, description="maximum boosting rounds (early stopping picks fewer)")
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    num_leaves: int = Field(default=31, ge=2, le=1024)
    max_depth: int | None = Field(default=None, ge=1, le=32)
    min_child_samples: int = Field(default=20, ge=1, le=10000)
    subsample: float = Field(default=0.8, gt=0, le=1)
    colsample_bytree: float = Field(default=0.8, gt=0, le=1)
    reg_lambda: float = Field(default=0.0, ge=0, le=100)
    early_stopping: bool = Field(default=True, description="time-ordered validation block inside each training fold")
    val_frac: float = Field(default=0.2, gt=0, lt=0.5)
    stopping_rounds: int = Field(default=50, ge=5, le=500)
    cv: CVParams = Field(default_factory=CVParams)
    shap: bool = Field(default=False, description="opt-in (S1.4): TreeSHAP on a sample adds about n x features x 1e-7 s")
    shap_sample: int = Field(default=2000, ge=100, le=20000)


@register
class LightGBM(Method):
    id = "lightgbm"
    family = "ml"
    title = "LightGBM (gradient boosting)"
    description = ("LightGBM regression or classification with purged / walk-forward CV; early stopping on a "
                   "time-ordered validation block inside each training fold; gain / split importance, TreeSHAP, "
                   "learning curve, and a Diebold-Mariano test against a linear baseline on the same folds.")
    min_x = 1
    Params = LGBMParams
    outputs = ("OOS metrics vs linear baseline", "importance (gain / split)", "shap_beeswarm", "learning curve")
    diagnostics = ("DM test vs linear", "best iteration per fold", "train vs OOS gap")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        k = params.cv.folds + 1 + (params.cv.folds if params.early_stopping else 0)
        return 0.5 + n_rows * max(n_x, 1) * params.n_estimators * k * 3e-9 + (n_rows * n_x * 1e-7 if params.shap else 0)

    def fit(self, data: FitData, p: LGBMParams) -> Result:
        df, dropped = data.complete()
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        task = _task(y, p.task)
        res = Result(self.id, self.family, f"LightGBM ({task})")
        res.summary["rows_dropped_missing"] = dropped
        if len(y) < 50:
            raise GuardrailError("too_few_obs", "LightGBM needs at least 50 complete rows")
        folds, scheme, purge = make_folds(data, df, p.cv)
        prm = p.model_dump(exclude={"cv", "task", "shap", "shap_sample", "early_stopping", "val_frac", "stopping_rounds"})
        dates = df["date"].to_numpy()
        cv = A.lgbm_cv(X, y, dates, folds, task, prm, data.seed, p.early_stopping, p.val_frac, purge, p.stopping_rounds, data.check)
        oos = cv["oos"]
        mo = _oos_block(res, data, df, y, oos, task, folds, scheme, purge, "lightgbm")
        lin = linear_oos(X, y, folds, task, data.check)
        mask = tested_mask(len(y), folds)
        res.evaluation["metrics"]["linear"] = clean(metrics_of(y[mask], lin[mask], task))
        dm = dm_vs(res, df, y, oos, lin, task, mask, "LightGBM", "linear baseline", _lead(data))
        n_final = int(np.median(cv["best_iters"]))
        data.check()
        model = A.make_lgbm(task, {**prm, "n_estimators": n_final}, data.seed).fit(X, y)
        ins = metrics_of(y, A.predict(model, X, task), task)
        res.fit_stats.update(clean({f"in_sample_{k}": v for k, v in ins.items() if k != "n"}))
        res.fit_stats["final_n_estimators"] = n_final
        gain = model.booster_.feature_importance("gain")
        split = model.booster_.feature_importance("split")
        order = np.argsort(-gain)
        res.add_table("importance", "Feature importance (final model: total gain, number of splits)", ["feature", "gain", "split"],
                      clean([[data.x[j], gain[j], int(split[j])] for j in order]))
        res.line_plot("importance", "Feature importance (gain)", [series("gain", [data.x[j] for j in order], gain[order])],
                      "feature", "gain", kind="bar")
        res.add_table("best_iterations", "Boosting rounds per fold (early stopping on the validation block)", ["fold", "n_estimators"],
                      [[i + 1, int(b)] for i, b in enumerate(cv["best_iters"])])
        if cv["curve"]:
            c = cv["curve"]
            it = list(range(1, len(c["train"]) + 1))
            res.line_plot("learning_curve", f"Learning curve, last fold ({cv['metric']}; best round {c['best']})",
                          [series("train", it, c["train"]), series("validation", it, c["valid"])], "boosting round", cv["metric"])
        if p.shap:
            data.check()
            idx = np.random.default_rng(data.seed).choice(len(X), min(len(X), p.shap_sample), replace=False)
            beeswarm(res, list(data.x), X[idx], A.lgbm_contrib(model, X[idx]), data.seed)
        key = "auc" if task == "classification" else "rmse"
        res.summary.update({"dependent": data.y, "n_obs": len(y), "task": task, "model": "lightgbm", "cv_scheme": scheme,
                            "features": list(data.x), "beats_linear": dm["beats"], "verdict": dm["verdict"]})
        res.metrics.update(clean({"n_obs": len(y), f"oos_{key}": mo.get(key), "dm_p_vs_linear": dm["dm_p"],
                                  f"linear_oos_{key}": res.evaluation["metrics"]["linear"].get(key)}))
        return res


# ------------------------------------------------------------------------------------------------ Optuna


class OptunaParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["lgbm", "xgb", "rf", "hgb", "elasticnet"] = "lgbm"
    task: Task = "auto"
    n_trials: int = Field(default=30, ge=2, le=500, description="trial budget")
    timeout_s: int = Field(default=120, ge=5, le=3000, description="search stops after this many seconds (fewer trials)")
    holdout_frac: float = Field(default=0.2, gt=0.05, lt=0.5, description="last share of dates never seen by the search")
    cv: CVParams = Field(default_factory=lambda: CVParams(folds=4), description="inner purged / walk-forward CV on the tuning window")


@register
class OptunaTune(Method):
    id = "optuna_tune"
    family = "ml"
    title = "Hyper-parameter tuning (Optuna)"
    description = ("Seeded TPE search with median pruning over a model's hyper-parameters, scored by purged / embargoed "
                   "walk-forward CV on the tuning window; the best configuration is then refitted and judged on an "
                   "untouched final hold-out against the default configuration and a linear baseline (DM tests).")
    min_x = 1
    Params = OptunaParams
    outputs = ("best parameters", "optimization history", "parameter importance", "hold-out: tuned vs default vs linear")
    diagnostics = ("DM tuned vs default", "DM tuned vs linear", "CV-to-hold-out gap (selection optimism)")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        per = A_fit_cost(params.model, n_rows, n_x)
        return 1.5 + min(params.timeout_s, params.n_trials * params.cv.folds * per * 0.6) + 3 * per

    def fit(self, data: FitData, p: OptunaParams) -> Result:
        df, dropped = data.complete()
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        task = _task(y, p.task)
        res = Result(self.id, self.family, f"Optuna tuning of {p.model} ({task})")
        res.summary["rows_dropped_missing"] = dropped
        if len(y) < 100:
            raise GuardrailError("too_few_obs", "tuning needs at least 100 complete rows")
        dates = df["date"].to_numpy()
        purge = p.cv.purge if p.cv.purge is not None else max(1, _lead(data))
        tune, hold = A.holdout_split(dates, p.holdout_frac, purge, data.seed)
        scheme = p.cv.scheme if p.cv.scheme != "auto" else {"timeseries": "expanding", "panel": "purged_kfold"}.get(data.kind, "kfold")
        inner = M.cv_splits(dates[tune], scheme, p.cv.folds, purge, p.cv.embargo, seed=data.seed)
        if len(inner) < 2 or len(hold) < 20:
            raise GuardrailError("too_few_obs", "not enough periods for inner CV plus a hold-out")
        Xt, yt = X[tune], y[tune]
        study = A.optuna_tune(p.model, task, Xt, yt, inner, p.n_trials, float(p.timeout_s), data.seed, data.check)
        done = [t for t in study.trials if t.value is not None and t.state.name == "COMPLETE"]
        if not done:
            raise GuardrailError("all_failed", "no Optuna trial completed (all pruned or timed out)")
        best = study.best_params
        data.check()
        pt = A.predict(A.build_model(p.model, task, best, data.seed).fit(Xt, yt), X[hold], task)
        pdft = A.predict(A.build_model(p.model, task, {}, data.seed).fit(Xt, yt), X[hold], task)
        plin = A.predict(A.build_model("linear", task, {}, data.seed).fit(Xt, yt), X[hold], task)
        n = len(y)

        def full(v: np.ndarray) -> np.ndarray:
            a = np.full(n, np.nan)
            a[hold] = v
            return a

        mask = np.zeros(n, bool)
        mask[hold] = True
        Pt, Pd, Pl = full(pt), full(pdft), full(plin)
        mt, md, ml = metrics_of(y[hold], pt, task), metrics_of(y[hold], pdft, task), metrics_of(y[hold], plin, task)
        d1 = dm_vs(res, df, y, Pt, Pd, task, mask, "tuned", "default configuration", _lead(data))
        d2 = dm_vs(res, df, y, Pt, Pl, task, mask, "tuned", "linear baseline", _lead(data))
        key = "auc" if task == "classification" else "rmse"
        res.add_table("holdout", f"Final hold-out ({len(hold)} rows from {pd.to_datetime(dates[hold]).min().date()}; never seen by the search)",
                      ["model", *[k for k in mt if k != "n"], "n"], clean([[nm, *[m[k] for k in mt if k != "n"], m["n"]]
                                                                         for nm, m in (("tuned", mt), ("default", md), ("linear", ml))]))
        res.add_table("best_params", "Best hyper-parameters", ["parameter", "value"], clean([[k, v] for k, v in best.items()]))
        vals = [t.value for t in done]
        nums = [t.number + 1 for t in done]
        res.line_plot("optimization_history", "Optimization history (inner-CV loss per completed trial)",
                      [series("trial loss", nums, vals), series("best so far", nums, np.minimum.accumulate(vals))],
                      "trial", "MSE" if task == "regression" else "log-loss")
        top = sorted(done, key=lambda t: t.value)[:10]
        cols = sorted(best)
        res.add_table("trials", "Top trials", ["trial", "cv_loss", *cols], clean([[t.number + 1, t.value, *[t.params.get(c) for c in cols]] for t in top]))
        try:
            import optuna

            imp = optuna.importance.get_param_importances(study) if len(done) >= 5 else {}
            if imp:
                res.line_plot("param_importance", "Hyper-parameter importance (fANOVA)",
                              [series("importance", list(imp), list(imp.values()))], "parameter", "importance", kind="bar")
        except Exception as e:  # noqa: BLE001
            res.info("importance_failed", f"parameter importance not computed: {type(e).__name__}")
        cv_best = float(study.best_value)
        hold_loss = float(np.mean(A.loss_vec(y[hold], pt, task)))
        res.fit_stats.update(clean({"cv_best_loss": cv_best, "holdout_loss": hold_loss, "n_trials_completed": len(done),
                                    "n_trials_pruned": sum(t.state.name == "PRUNED" for t in study.trials),
                                    "selection_optimism": hold_loss - cv_best}))
        if len(study.trials) < p.n_trials:
            res.info("timeout", f"{len(study.trials)} of {p.n_trials} trials ran before the {p.timeout_s}s timeout; "
                                "a re-run may finish a different number of trials")
        res.evaluation = clean({"scheme": f"holdout after {scheme} inner CV", "horizon": 1, "n_folds": len(inner), "purge": purge,
                                "model": p.model, "metrics": {"tuned": mt, "default": md, "linear": ml},
                                "note": "inner CV only on the tuning window; metrics on the untouched final hold-out"})
        res.summary.update({"dependent": data.y, "task": task, "model": p.model, "cv_scheme": scheme, "purge": purge,
                            "n_tune": len(tune), "n_holdout": len(hold), "best_params": best,
                            "beats_default": d1["beats"], "beats_linear": d2["beats"], "verdict": d2["verdict"]})
        res.metrics.update(clean({f"holdout_{key}": mt.get(key), f"default_holdout_{key}": md.get(key),
                                  f"linear_holdout_{key}": ml.get(key), "cv_best_loss": cv_best, "n_obs": n,
                                  "dm_p_vs_default": d1["dm_p"], "dm_p_vs_linear": d2["dm_p"]}))
        return res


def A_fit_cost(model: str, n: int, k: int) -> float:
    k = max(k, 1)
    return {"lgbm": 0.1 + n * k * 1e-7, "xgb": 0.15 + n * k * 2e-7, "rf": 0.2 + n * k * 4e-7,
            "hgb": 0.15 + n * k * 1.5e-7, "elasticnet": 0.02 + n * k * 2e-7}.get(model, 0.3 + n * k * 2e-7)


# ------------------------------------------------------------------------------------------------ stacking


BASES = Literal["mean", "ols", "ridge", "lasso", "elasticnet", "rf", "hgb", "xgb", "lgbm"]


class StackParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bases: list[BASES] = Field(default_factory=lambda: ["ols", "ridge", "rf", "lgbm"], min_length=2, max_length=8)
    meta: Literal["nnls", "ridge", "mean"] = Field(default="nnls", description="nnls: non-negative weights; classification uses a logistic meta-learner on logits")
    inner_folds: int = Field(default=3, ge=2, le=10)
    task: Task = "auto"
    cv: CVParams = Field(default_factory=CVParams)


@register
class Stacking(Method):
    id = "stacking"
    family = "ml"
    title = "Stacking ensemble"
    description = ("Base models combined by a meta-learner fitted on inner out-of-fold predictions inside each "
                   "training fold (no test-fold leakage); OOS metrics of every base, the equal-weight average and the "
                   "stack, with Diebold-Mariano tests of the stack against the best base model and the average.")
    min_x = 1
    Params = StackParams
    outputs = ("OOS metrics per base / average / stack", "meta weights", "cumulative loss difference")
    diagnostics = ("DM stack vs best base", "DM stack vs equal weights")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        heavy = {"rf": 4e-7, "hgb": 1.5e-7, "xgb": 2e-7, "lgbm": 1e-7, "ridge": 3e-7, "lasso": 3e-7, "elasticnet": 3e-7}
        per = sum(0.05 + n_rows * max(n_x, 1) * heavy.get(b, 2e-8) for b in params.bases)
        return 0.5 + params.cv.folds * (params.inner_folds + 1) * per

    def fit(self, data: FitData, p: StackParams) -> Result:
        df, dropped = data.complete()
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        task = _task(y, p.task)
        bases = list(dict.fromkeys(p.bases))
        if len(bases) < 2:
            raise GuardrailError("too_few_bases", "stacking needs at least two different base models")
        res = Result(self.id, self.family, f"Stacking ({', '.join(bases)} → {p.meta})")
        res.summary["rows_dropped_missing"] = dropped
        if len(y) < 100:
            raise GuardrailError("too_few_obs", "stacking needs at least 100 complete rows")
        folds, scheme, purge = make_folds(data, df, p.cv)
        out = A.stack_cv(X, y, df["date"].to_numpy(), folds, bases, p.meta, task, scheme, p.inner_folds, purge,
                         p.cv.embargo, data.seed, data.check)
        mask = tested_mask(len(y), folds)
        ms = _oos_block(res, data, df, y, out["stack"], task, folds, scheme, purge, "stack")
        key = "auc" if task == "classification" else "rmse"
        lower = key != "auc"
        rows = []
        for j, b in enumerate(bases):
            m = metrics_of(y[mask], out["base"][mask, j], task)
            res.evaluation["metrics"][b] = clean(m)
            rows.append((b, m))
        me = metrics_of(y[mask], out["equal"][mask], task)
        res.evaluation["metrics"]["equal_weight"] = clean(me)
        ok = [r for r in rows if r[1].get(key) is not None and np.isfinite(r[1][key])]
        best = (min if lower else max)(ok, key=lambda r: r[1][key])[0]
        jb = bases.index(best)
        d1 = dm_vs(res, df, y, out["stack"], out["base"][:, jb], task, mask, "stack", f"best base ({best})", _lead(data))
        d2 = dm_vs(res, df, y, out["stack"], out["equal"], task, mask, "stack", "equal-weight average", _lead(data))
        allr = rows + [("equal_weight", me), ("stack", ms)]
        mk = [k for k in ms if k != "n"]
        allr.sort(key=lambda r: (r[1].get(key) if r[1].get(key) is not None else np.inf) * (1 if lower else -1))
        res.add_table("leaderboard", f"OOS metrics ({scheme}, {len(folds)} folds; meta fitted on inner OOF predictions)",
                      ["model", *mk, "n"], clean([[nm, *[m.get(k) for k in mk], m.get("n")] for nm, m in allr]))
        res.line_plot("leaderboard", f"OOS {key}", [series(key, [r[0] for r in allr], [r[1].get(key) for r in allr])], "model", key, kind="bar")
        W = out["weights"]
        names = bases + ["intercept"]
        res.add_table("meta_weights", "Meta-learner weights (mean and SD across outer folds)", ["base", "mean_weight", "sd_weight"],
                      clean([[names[j], W[:, j].mean(), W[:, j].std()] for j in range(W.shape[1])]))
        t = mask & np.isfinite(out["stack"]) & np.isfinite(out["base"][:, jb])
        order = np.argsort(pd.to_datetime(df["date"].to_numpy()[t]), kind="stable")
        dts = pd.to_datetime(df["date"].to_numpy()[t])[order]
        diff = (A.loss_vec(y[t], out["stack"][t], task) - A.loss_vec(y[t], out["base"][t, jb], task))[order]
        res.line_plot("cum_loss_diff", f"Cumulative loss difference: stack − {best} (below 0 = stack better)",
                      [series("stack − best base", [d.date() for d in dts], np.cumsum(diff))], "date", "cumulative loss difference")
        res.summary.update({"dependent": data.y, "task": task, "bases": bases, "meta": p.meta, "best_base": best,
                            "cv_scheme": scheme, "beats_best_base": d1["beats"], "verdict": d1["verdict"]})
        res.metrics.update(clean({f"oos_{key}": ms.get(key), f"best_base_oos_{key}": dict(rows)[best].get(key),
                                  "dm_p_vs_best_base": d1["dm_p"], "dm_p_vs_equal": d2["dm_p"], "n_obs": int(mask.sum())}))
        return res


# ------------------------------------------------------------------------------------------------ learning to rank


class LTRParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n_grades: int = Field(default=5, ge=2, le=20, description="relevance grades = quantile buckets of y per date")
    ndcg_k: int = Field(default=10, ge=1, le=500)
    n_estimators: int = Field(default=300, ge=10, le=3000)
    learning_rate: float = Field(default=0.05, gt=0, le=1)
    num_leaves: int = Field(default=31, ge=2, le=1024)
    min_child_samples: int = Field(default=20, ge=1, le=10000)
    subsample: float = Field(default=0.8, gt=0, le=1)
    colsample_bytree: float = Field(default=0.8, gt=0, le=1)
    quantiles: int = Field(default=5, ge=2, le=10, description="top-minus-bottom spread buckets")
    cv: CVParams = Field(default_factory=CVParams)


@register
class LearningToRank(Method):
    id = "ltr"
    family = "ml"
    title = "Learning to rank (LightGBM LambdaRank)"
    description = ("Cross-sectional ranking model for panels: LambdaRank with one query group per date and graded "
                   "relevance from y quantiles; scored out of sample by per-date rank IC, NDCG@k and the top-minus-"
                   "bottom spread, against a pointwise LightGBM regressor and OLS on identical folds.")
    shapes = ("panel",)
    min_x = 1
    Params = LTRParams
    outputs = ("rank IC / NDCG / spread vs baselines", "IC by date", "cumulative top-minus-bottom spread")
    diagnostics = ("DM on per-date IC vs pointwise and OLS", "IC t-stat")

    def cost(self, n_rows: int, n_x: int, params: Any) -> float:
        return 1.0 + n_rows * max(n_x, 1) * params.n_estimators * (params.cv.folds * 2.2) * 3e-9 + n_rows * 2e-5

    def fit(self, data: FitData, p: LTRParams) -> Result:
        df, dropped = data.complete()
        X, y = df[data.x].to_numpy(float), df[data.y].to_numpy(float)
        res = Result(self.id, self.family, "Learning to rank (LambdaRank)")
        res.summary["rows_dropped_missing"] = dropped
        if len(y) < 200:
            raise GuardrailError("too_few_obs", "learning to rank needs at least 200 complete panel rows")
        folds, scheme, purge = make_folds(data, df, p.cv)
        dates = df["date"].to_numpy()
        prm = p.model_dump(include={"n_estimators", "learning_rate", "num_leaves", "min_child_samples", "subsample", "colsample_bytree"})
        s_ltr = A.ltr_cv(X, y, dates, folds, prm, p.n_grades, data.seed, check=data.check)
        s_pt = A.lgbm_cv(X, y, dates, folds, "regression", prm, data.seed, early_stopping=False, check=data.check)["oos"]
        s_ols = linear_oos(X, y, folds, "regression", data.check)
        mask = tested_mask(len(y), folds) & np.isfinite(s_ltr)
        per = {k: A.per_date_scores(dates[mask], y[mask], s[mask], p.n_grades, p.ndcg_k, p.quantiles)
               for k, s in (("lambdarank", s_ltr), ("pointwise_lgbm", s_pt), ("ols", s_ols))}
        if len(per["lambdarank"]) < 5:
            raise GuardrailError("too_few_obs", "fewer than 5 test dates with enough entities to rank")
        rows = []
        for k, t in per.items():
            ic = t["ic"].dropna()
            rows.append([k, ic.mean(), ic.mean() / (ic.std(ddof=1) / np.sqrt(len(ic))) if len(ic) > 1 else None,
                         t["ndcg"].mean(), t["spread"].mean(), float((ic > 0).mean()), len(t)])
        res.add_table("ranking", f"Out-of-fold ranking quality ({scheme}, {len(folds)} folds; per-date averages)",
                      ["model", "rank_ic", "ic_t", f"ndcg@{p.ndcg_k}", f"top_minus_bottom_q{p.quantiles}", "share_ic_positive", "n_dates"],
                      clean(rows))
        j = per["lambdarank"][["date", "ic"]].merge(per["pointwise_lgbm"][["date", "ic"]], on="date", suffixes=("", "_pt")) \
            .merge(per["ols"][["date", "ic"]].rename(columns={"ic": "ic_ols"}), on="date").dropna()
        verdicts = {}
        for ref, col in (("pointwise LightGBM", "ic_pt"), ("OLS", "ic_ols")):
            stat, pv = M.dm_test(-j[col].to_numpy(), -j["ic"].to_numpy(), max(1, _lead(data)))
            better = j["ic"].mean() > j[col].mean()
            sig = pv is not None and np.isfinite(pv) and pv < 0.05
            v = (f"LambdaRank beats {ref} on rank IC (DM p = {pv:.3g})" if better and sig else
                 f"LambdaRank does not significantly beat {ref} on rank IC (DM p = {pv:.3g})")
            verdicts[ref] = (better and sig, pv, v)
            res.add_test(f"Diebold-Mariano on per-date IC: LambdaRank vs {ref}", stat, pv, None, "equal mean rank IC",
                         group="inference", passed=None, conclusion=v)
            if not (better and sig):
                res.warn(f"no_gain_vs_{col}", v, "info" if better else "warning")
        t = per["lambdarank"]
        res.line_plot("ic_by_date", "Rank IC by test date", [series(k, [d.date() for d in per[k]["date"]], per[k]["ic"]) for k in per],
                      "date", "Spearman IC")
        res.line_plot("cum_spread", f"Cumulative top-minus-bottom (Q{p.quantiles}) mean y by score",
                      [series(k, [d.date() for d in per[k]["date"]], np.nancumsum(per[k]["spread"])) for k in per], "date", "cumulative spread",
                      note="sum of per-date spreads; with a multi-period forward y the periods overlap (not a tradable P&L)")
        fold = np.full(len(y), -1)
        for i, (_, te) in enumerate(folds):
            fold[te] = i
        res.artifacts["oos_predictions"] = pl.DataFrame({
            "entity_id": df["entity_id"].astype(str).to_numpy()[mask], "date": pd.to_datetime(df["date"]).dt.date.to_numpy()[mask],
            "prediction": s_ltr[mask].astype(float), "fold": fold[mask].astype(int), "scheme": [scheme] * int(mask.sum()),
            "model": ["lambdarank"] * int(mask.sum())})
        res.add_table("cv_folds", "Cross-validation folds", ["fold", "train_rows", "test_rows", "test_start", "test_end"],
                      clean([[i + 1, len(tr), len(te), df["date"].iloc[te].min(), df["date"].iloc[te].max()] for i, (tr, te) in enumerate(folds)]))
        ic = t["ic"].dropna()
        res.evaluation = clean({"scheme": scheme, "horizon": 1, "n_folds": len(folds), "purge": purge, "model": "lambdarank",
                                "metrics": {r[0]: {"rank_ic": r[1], "rank_ic_t": r[2], "ndcg": r[3], "spread": r[4], "n": r[6]} for r in rows},
                                "note": "scores are ranks within a date, not forecasts of y: compare ranking metrics only"})
        res.summary.update({"dependent": data.y, "features": list(data.x), "cv_scheme": scheme, "n_grades": p.n_grades,
                            "beats_pointwise": verdicts["pointwise LightGBM"][0], "beats_ols": verdicts["OLS"][0],
                            "verdict": verdicts["pointwise LightGBM"][2]})
        res.metrics.update(clean({"oos_rank_ic": ic.mean(), "oos_rank_ic_t": rows[0][2], "oos_ndcg": t["ndcg"].mean(),
                                  "oos_spread": t["spread"].mean(), "pointwise_rank_ic": rows[1][1], "ols_rank_ic": rows[2][1],
                                  "dm_p_vs_pointwise": verdicts["pointwise LightGBM"][1], "n_obs": int(mask.sum())}))
        return res
