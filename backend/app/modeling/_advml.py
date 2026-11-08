"""Studio v2 S2 advanced ML core: LightGBM with early stopping inside each training fold, Optuna tuning on purged
walk-forward CV with an untouched hold-out, stacking with inner out-of-fold meta-features, and LightGBM LambdaRank by
date group scored by rank IC / NDCG (owner: model-studio).

Pure functions (numpy, pandas, scikit-learn, xgboost, lightgbm, optuna); embedded verbatim in exported scripts after
app/modeling/_ml.py and app/modeling/_causal2.py, so the names imported below already exist there.
"""

from __future__ import annotations

import warnings as _w
from typing import Any, Callable

import numpy as np
import pandas as pd

try:  # inside the app; in an exported script the cores are already defined above this module
    from ._causal2 import n_threads  # noqa: F401
    from ._ml import candidate_predict, cv_splits  # noqa: F401
except ImportError:  # pragma: no cover
    pass

ADVML_VERSION = "1.0"


# ------------------------------------------------------------------------------------------------ builders


def make_lgbm(task: str, p: dict[str, Any], seed: int, objective: str | None = None) -> Any:
    import lightgbm as lgb

    sub = float(p.get("subsample", 0.8))
    kw = dict(n_estimators=int(p.get("n_estimators", 300)), learning_rate=float(p.get("learning_rate", 0.05)),
              num_leaves=int(p.get("num_leaves", 31)), max_depth=int(p.get("max_depth") or -1),
              min_child_samples=int(p.get("min_child_samples", 20)), subsample=sub, subsample_freq=1 if sub < 1 else 0,
              colsample_bytree=float(p.get("colsample_bytree", 0.8)), reg_lambda=float(p.get("reg_lambda", 0.0)),
              reg_alpha=float(p.get("reg_alpha", 0.0)), random_state=seed, n_jobs=n_threads(), verbose=-1,
              deterministic=True, force_row_wise=True)
    if objective:
        kw["objective"] = objective
    if "alpha" in p:  # quantile / huber objectives (Studio v2 S3 quantile GBM)
        kw["alpha"] = float(p["alpha"])
    if task == "rank":
        return lgb.LGBMRanker(**kw)
    return (lgb.LGBMClassifier if task == "classification" else lgb.LGBMRegressor)(**kw)


def build_model(model: str, task: str, p: dict[str, Any], seed: int) -> Any:
    """Estimator for a tunable model family (thread-capped)."""
    nt = n_threads()
    clf = task == "classification"
    if model == "lgbm":
        return make_lgbm(task, p, seed)
    if model == "xgb":
        import xgboost as xgb

        return (xgb.XGBClassifier if clf else xgb.XGBRegressor)(
            n_estimators=int(p.get("n_estimators", 300)), max_depth=int(p.get("max_depth", 4)),
            learning_rate=float(p.get("learning_rate", 0.05)), subsample=float(p.get("subsample", 0.8)),
            colsample_bytree=float(p.get("colsample_bytree", 0.8)), min_child_weight=float(p.get("min_child_weight", 20)),
            n_jobs=nt, random_state=seed, verbosity=0)
    if model == "rf":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

        return (RandomForestClassifier if clf else RandomForestRegressor)(
            n_estimators=int(p.get("n_estimators", 300)), max_depth=p.get("max_depth"),
            min_samples_leaf=int(p.get("min_samples_leaf", 20)), max_features=float(p.get("max_features", 0.5)),
            n_jobs=nt, random_state=seed)
    if model == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

        return (HistGradientBoostingClassifier if clf else HistGradientBoostingRegressor)(
            max_iter=int(p.get("max_iter", 300)), learning_rate=float(p.get("learning_rate", 0.05)),
            max_leaf_nodes=int(p.get("max_leaf_nodes", 31)), min_samples_leaf=int(p.get("min_samples_leaf", 20)),
            l2_regularization=float(p.get("l2", 0.0)), random_state=seed)
    if model == "elasticnet":
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        if clf:
            from sklearn.linear_model import LogisticRegression

            return make_pipeline(StandardScaler(), LogisticRegression(C=float(p.get("C", 1.0)), l1_ratio=float(p.get("l1_ratio", 0.5)),
                                                                      solver="saga", max_iter=3000))
        from sklearn.linear_model import ElasticNet

        return make_pipeline(StandardScaler(), ElasticNet(alpha=float(p.get("alpha", 0.01)), l1_ratio=float(p.get("l1_ratio", 0.5)),
                                                          max_iter=5000))
    if model == "linear":
        from sklearn.linear_model import LinearRegression, LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        return make_pipeline(StandardScaler(), LogisticRegression(C=1e4, max_iter=3000)) if clf else LinearRegression()
    raise ValueError(f"unknown model {model!r}")


def predict(m: Any, X: np.ndarray, task: str) -> np.ndarray:
    return m.predict_proba(X)[:, 1] if task == "classification" else m.predict(X)


def loss_vec(y: np.ndarray, p: np.ndarray, task: str) -> np.ndarray:
    if task == "classification":
        q = np.clip(p, 1e-9, 1 - 1e-9)
        return -(y * np.log(q) + (1 - y) * np.log(1 - q))
    return (y - p) ** 2


def cv_fit_predict(model: str, task: str, p: dict[str, Any], X: np.ndarray, y: np.ndarray, folds: list, seed: int,
                   check: Callable[[], None] | None = None) -> np.ndarray:
    out = np.full(len(y), np.nan)
    for tr, te in folds:
        if check:
            check()
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            out[te] = predict(build_model(model, task, p, seed).fit(X[tr], y[tr]), X[te], task)
    return out


def inner_val_split(tr: np.ndarray, dates: np.ndarray, val_frac: float, purge: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Time-ordered validation split of training rows: the last val_frac of the training dates (minus `purge` periods
    before them); rows are shuffled when there is no time dimension (one date)."""
    d = pd.to_datetime(pd.Series(dates[tr])).to_numpy()
    u = np.unique(d)
    if len(u) < 5:
        perm = np.random.default_rng(seed).permutation(len(tr))
        k = max(1, int(len(tr) * val_frac))
        return np.sort(tr[perm[k:]]), np.sort(tr[perm[:k]])
    k = max(1, int(np.ceil(len(u) * val_frac)))
    cut = len(u) - k
    pos = np.searchsorted(u, d)
    return tr[pos < cut - purge], tr[pos >= cut]


# ------------------------------------------------------------------------------------------------ LightGBM


def lgbm_cv(X: np.ndarray, y: np.ndarray, dates: np.ndarray, folds: list, task: str, p: dict[str, Any], seed: int,
            early_stopping: bool = True, val_frac: float = 0.2, purge: int = 1, rounds: int = 50,
            check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Out-of-fold LightGBM predictions. With early stopping, each training fold is split in time (fit / validation);
    the best iteration is found on the validation block and the model is refitted on the whole training fold."""
    import lightgbm as lgb

    oos = np.full(len(y), np.nan)
    best, curve = [], None
    metric = "binary_logloss" if task == "classification" else "l2"
    for tr, te in folds:
        if check:
            check()
        n_iter = int(p.get("n_estimators", 300))
        if early_stopping:
            fi, vi = inner_val_split(tr, dates, val_frac, purge, seed)
            if len(fi) >= 20 and len(vi) >= 5:
                ev: dict[str, Any] = {}
                m = make_lgbm(task, p, seed).fit(X[fi], y[fi], eval_X=(X[fi], X[vi]), eval_y=(y[fi], y[vi]),
                                                 eval_names=["train", "valid"], eval_metric=metric,
                                                 callbacks=[lgb.early_stopping(rounds, verbose=False), lgb.record_evaluation(ev)])
                n_iter = max(int(m.best_iteration_ or n_iter), 1)
                curve = {"train": list(ev["train"][metric]), "valid": list(ev["valid"][metric]), "best": n_iter}
        best.append(n_iter)
        m = make_lgbm(task, {**p, "n_estimators": n_iter}, seed).fit(X[tr], y[tr])
        oos[te] = predict(m, X[te], task)
    return {"oos": oos, "best_iters": best, "curve": curve, "metric": metric}


def lgbm_contrib(model: Any, X: np.ndarray) -> np.ndarray:
    """TreeSHAP contributions from LightGBM itself (pred_contrib); last column (bias) dropped."""
    return np.asarray(model.predict(X, pred_contrib=True))[:, :-1]


# ------------------------------------------------------------------------------------------------ Optuna


def search_space(trial: Any, model: str, task: str) -> dict[str, Any]:
    s = trial.suggest_float, trial.suggest_int
    f, i = s
    if model == "lgbm":
        return {"num_leaves": i("num_leaves", 7, 127, log=True), "learning_rate": f("learning_rate", 0.01, 0.3, log=True),
                "n_estimators": i("n_estimators", 50, 1000, log=True), "min_child_samples": i("min_child_samples", 5, 200, log=True),
                "subsample": f("subsample", 0.5, 1.0), "colsample_bytree": f("colsample_bytree", 0.5, 1.0),
                "reg_lambda": f("reg_lambda", 1e-3, 10.0, log=True)}
    if model == "xgb":
        return {"max_depth": i("max_depth", 2, 8), "learning_rate": f("learning_rate", 0.01, 0.3, log=True),
                "n_estimators": i("n_estimators", 50, 1000, log=True), "min_child_weight": f("min_child_weight", 1, 50, log=True),
                "subsample": f("subsample", 0.5, 1.0), "colsample_bytree": f("colsample_bytree", 0.5, 1.0)}
    if model == "rf":
        return {"n_estimators": i("n_estimators", 100, 500, log=True), "max_depth": i("max_depth", 3, 20),
                "min_samples_leaf": i("min_samples_leaf", 5, 200, log=True), "max_features": f("max_features", 0.2, 1.0)}
    if model == "hgb":
        return {"max_iter": i("max_iter", 50, 800, log=True), "learning_rate": f("learning_rate", 0.01, 0.3, log=True),
                "max_leaf_nodes": i("max_leaf_nodes", 7, 63, log=True), "min_samples_leaf": i("min_samples_leaf", 5, 200, log=True),
                "l2": f("l2", 1e-3, 10.0, log=True)}
    if task == "classification":
        return {"C": f("C", 1e-3, 100.0, log=True), "l1_ratio": f("l1_ratio", 0.05, 1.0)}
    return {"alpha": f("alpha", 1e-4, 10.0, log=True), "l1_ratio": f("l1_ratio", 0.05, 1.0)}


def holdout_split(dates: np.ndarray, frac: float, purge: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    """Tuning rows (dates before the hold-out minus `purge` periods) and the final hold-out rows (last frac of dates)."""
    allr = np.arange(len(dates))
    return inner_val_split(allr, dates, frac, purge, seed)


def optuna_tune(model: str, task: str, X: np.ndarray, y: np.ndarray, folds: list, n_trials: int, timeout: float,
                seed: int, check: Callable[[], None] | None = None) -> Any:
    """TPE search (seeded, sequential) minimizing the mean fold loss (MSE / log-loss) over `folds`; median pruning
    after each fold. Returns the optuna Study."""
    import optuna

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study(direction="minimize", sampler=optuna.samplers.TPESampler(seed=seed),
                                pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=0))

    def objective(trial: Any) -> float:
        prm = search_space(trial, model, task)
        losses = []
        for k, (tr, te) in enumerate(folds):
            if check:
                check()
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                m = build_model(model, task, prm, seed).fit(X[tr], y[tr])
            losses.append(float(np.mean(loss_vec(y[te], predict(m, X[te], task), task))))
            trial.report(float(np.mean(losses)), k)
            if trial.should_prune():
                raise optuna.TrialPruned()
        return float(np.mean(losses))

    study.optimize(objective, n_trials=n_trials, timeout=timeout, gc_after_trial=False)
    return study


# ------------------------------------------------------------------------------------------------ stacking


def base_predict(name: str, task: str, Xtr: np.ndarray, ytr: np.ndarray, Xte: np.ndarray, seed: int) -> np.ndarray:
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        if name in ("lgbm", "xgb", "rf", "hgb"):
            return predict(build_model(name, task, {}, seed).fit(Xtr, ytr), Xte, task)
        return np.asarray(candidate_predict({"method": name, "params": {}}, Xtr, ytr, Xte, task, seed), float)


def _logit(p: np.ndarray) -> np.ndarray:
    q = np.clip(p, 1e-6, 1 - 1e-6)
    return np.log(q / (1 - q))


def meta_fit(O: np.ndarray, y: np.ndarray, meta: str, task: str) -> tuple[np.ndarray, float]:
    """Meta-learner weights (and intercept) on out-of-fold base predictions: nnls (non-negative, no intercept),
    ridge (alpha 1, intercept) or mean (equal weights). Classification combines on the logit scale."""
    B = O.shape[1]
    if meta == "mean":
        return np.full(B, 1.0 / B), 0.0
    Z = _logit(O) if task == "classification" else O
    if task == "classification":
        from sklearn.linear_model import LogisticRegression

        lr = LogisticRegression(C=1.0, max_iter=2000).fit(Z, y)
        return lr.coef_[0], float(lr.intercept_[0])
    if meta == "nnls":
        from scipy.optimize import nnls

        w, _ = nnls(Z, y)
        return w, 0.0
    from sklearn.linear_model import Ridge

    r = Ridge(alpha=1.0).fit(Z, y)
    return r.coef_, float(r.intercept_)


def meta_apply(P: np.ndarray, w: np.ndarray, b: float, task: str, meta: str) -> np.ndarray:
    if task == "classification":
        if meta == "mean":
            return P @ w
        return 1 / (1 + np.exp(-(_logit(P) @ w + b)))
    return P @ w + b


def stack_cv(X: np.ndarray, y: np.ndarray, dates: np.ndarray, folds: list, bases: list[str], meta: str, task: str,
             scheme: str, inner_folds: int, purge: int, embargo: int, seed: int,
             check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Outer CV; inside each training fold, inner CV (same scheme) gives out-of-fold base predictions on which the
    meta-learner is fitted, so the stack never sees test-fold outcomes."""
    n, B = len(y), len(bases)
    P = np.full((n, B), np.nan)
    stack, eq = np.full(n, np.nan), np.full(n, np.nan)
    W = []
    for tr, te in folds:
        inner = cv_splits(dates[tr], scheme, inner_folds, purge, embargo, seed=seed)
        O = np.full((len(tr), B), np.nan)
        for itr, ite in inner:
            for j, b in enumerate(bases):
                if check:
                    check()
                O[ite, j] = base_predict(b, task, X[tr][itr], y[tr][itr], X[tr][ite], seed)
        ok = np.all(np.isfinite(O), axis=1)
        if ok.sum() < max(10, 2 * B):
            w, b0 = np.full(B, 1.0 / B), 0.0
        else:
            w, b0 = meta_fit(O[ok], y[tr][ok], meta, task)
        W.append(np.append(w, b0))
        for j, b in enumerate(bases):
            if check:
                check()
            P[te, j] = base_predict(b, task, X[tr], y[tr], X[te], seed)
        stack[te] = meta_apply(P[te], w, b0, task, meta)
        eq[te] = P[te].mean(1)
    return {"base": P, "stack": stack, "equal": eq, "weights": np.array(W)}


# ------------------------------------------------------------------------------------------------ learning to rank


def grades(dates: np.ndarray, y: np.ndarray, n_grades: int) -> np.ndarray:
    """Relevance grade 0..n_grades-1 = quantile bucket of y within each date (labels for LambdaRank / NDCG)."""
    r = pd.Series(y).groupby(pd.Series(dates)).rank(pct=True, method="average").to_numpy()
    return np.clip(np.floor(r * n_grades - 1e-9), 0, n_grades - 1).astype(int)


def ltr_cv(X: np.ndarray, y: np.ndarray, dates: np.ndarray, folds: list, p: dict[str, Any], n_grades: int, seed: int,
           min_group: int = 5, check: Callable[[], None] | None = None) -> np.ndarray:
    """Out-of-fold LambdaRank scores; query groups = dates (rows of one date are ranked against each other)."""
    out = np.full(len(y), np.nan)
    d = pd.to_datetime(pd.Series(dates)).to_numpy()
    for tr, te in folds:
        if check:
            check()
        t = tr[np.argsort(d[tr], kind="stable")]
        cnt = pd.Series(d[t]).value_counts(sort=False)
        keep = np.isin(d[t], cnt[cnt >= min_group].index.to_numpy())
        t = t[keep]
        if len(t) < 50:
            continue
        sizes = pd.Series(d[t]).value_counts(sort=False).reindex(pd.unique(d[t])).to_numpy()
        lab = grades(d[t], y[t], n_grades)
        m = make_lgbm("rank", p, seed, objective="lambdarank")
        m.set_params(label_gain=[float(2 ** g - 1) for g in range(n_grades)])
        m.fit(X[t], lab, group=sizes)
        out[te] = m.predict(X[te])
    return out


def per_date_scores(dates: np.ndarray, y: np.ndarray, s: np.ndarray, n_grades: int, k: int, q: int = 5) -> pd.DataFrame:
    """Per test date: Spearman rank IC, NDCG@k (graded relevance from y), top-minus-bottom quantile mean of y."""
    from sklearn.metrics import ndcg_score

    df = pd.DataFrame({"d": pd.to_datetime(pd.Series(dates)).to_numpy(), "y": y, "s": s}).dropna()
    rows = []
    for dt, g in df.groupby("d", sort=True):
        if len(g) < max(5, q):
            continue
        ic = g["y"].rank().corr(g["s"].rank())
        rel = grades(np.zeros(len(g)), g["y"].to_numpy(), n_grades)
        nd = float(ndcg_score([rel], [g["s"].to_numpy()], k=min(k, len(g)))) if rel.max() > 0 else np.nan
        r = g["s"].rank(pct=True)
        spread = g.loc[r > 1 - 1 / q, "y"].mean() - g.loc[r <= 1 / q, "y"].mean()
        rows.append((dt, ic, nd, spread, len(g)))
    return pd.DataFrame(rows, columns=["date", "ic", "ndcg", "spread", "n"])
