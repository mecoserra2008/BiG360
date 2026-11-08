"""Model Studio Phase C core: time-series-aware CV, regularized regression, tree ensembles, density / mixture
clustering, factor construction + GRS, Diebold-Mariano, leaderboard candidates (owner: model-studio).

Pure functions (numpy, pandas, scipy, scikit-learn, statsmodels, xgboost, shap); embedded verbatim in exported scripts.
"""

from __future__ import annotations

import warnings as _w
from typing import Any, Callable

import numpy as np
import pandas as pd
from scipy import stats

ML_VERSION = "1.0"


# ------------------------------------------------------------------------------------------------ cross-validation


def cv_splits(dates: Any, scheme: str = "expanding", n_folds: int = 5, purge: int = 1, embargo: int = 0,
              min_train: float = 0.4, seed: int = 42) -> list[tuple[np.ndarray, np.ndarray]]:
    """Row-index folds.
    expanding: walk-forward on blocks of unique dates (train = all dates before the test block minus `purge` periods);
    purged_kfold: contiguous date blocks as test sets, train = other dates minus `purge` periods before and
    `embargo` periods after the test block (Lopez de Prado);
    kfold: shuffled K-fold on rows (cross-sections without time)."""
    d = pd.Series(pd.to_datetime(pd.Series(dates)).to_numpy())
    n = len(d)
    if scheme == "kfold":
        idx = np.random.default_rng(seed).permutation(n)
        parts = np.array_split(idx, n_folds)
        return [(np.sort(np.concatenate([p for j, p in enumerate(parts) if j != k])), np.sort(parts[k]))
                for k in range(n_folds)]
    u = np.sort(d.unique())
    pos = d.map({v: i for i, v in enumerate(u)}).to_numpy()
    T = len(u)
    out = []
    if scheme == "expanding":
        start = max(int(T * min_train), 2)
        edges = np.linspace(start, T, n_folds + 1).astype(int)
        for k in range(n_folds):
            a, b = edges[k], edges[k + 1]
            if b <= a:
                continue
            tr = np.where(pos < a - purge)[0]
            te = np.where((pos >= a) & (pos < b))[0]
            if len(tr) and len(te):
                out.append((tr, te))
        return out
    edges = np.linspace(0, T, n_folds + 1).astype(int)
    for k in range(n_folds):
        a, b = edges[k], edges[k + 1]
        te = np.where((pos >= a) & (pos < b))[0]
        tr = np.where((pos < a - purge) | (pos >= b + embargo + purge))[0]
        if len(tr) and len(te):
            out.append((tr, te))
    return out


def reg_metrics(y: np.ndarray, p: np.ndarray, ybar_train: float | None = None) -> dict[str, float]:
    ok = np.isfinite(p) & np.isfinite(y)
    y, p = y[ok], p[ok]
    e = y - p
    base = np.mean(y) if ybar_train is None else ybar_train
    sst = np.sum((y - base) ** 2)
    return {"rmse": float(np.sqrt(np.mean(e ** 2))), "mae": float(np.mean(np.abs(e))),
            "r2_oos": float(1 - np.sum(e ** 2) / sst) if sst > 0 else float("nan"),
            "corr": float(np.corrcoef(y, p)[0, 1]) if len(y) > 2 and np.std(p) > 0 else float("nan"), "n": int(len(y))}


def clf_metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    ok = np.isfinite(p) & np.isfinite(y)
    y, p = (y[ok] > 0.5).astype(int), np.clip(p[ok], 1e-9, 1 - 1e-9)
    two = len(np.unique(y)) == 2
    return {"auc": float(roc_auc_score(y, p)) if two else float("nan"), "logloss": float(log_loss(y, p, labels=[0, 1])),
            "brier": float(brier_score_loss(y, p)), "accuracy": float(np.mean((p > 0.5) == y)), "n": int(len(y))}


def rank_ic(dates: Any, y: np.ndarray, p: np.ndarray) -> tuple[float, float, int]:
    """Mean per-date Spearman correlation between predictions and outcomes (panels), its t-stat and #dates."""
    df = pd.DataFrame({"d": np.asarray(dates), "y": y, "p": p}).dropna()
    ics = [g["y"].rank().corr(g["p"].rank()) for _, g in df.groupby("d") if len(g) >= 5]
    ics = np.array([x for x in ics if np.isfinite(x)])
    if len(ics) < 2:
        return float("nan"), float("nan"), len(ics)
    return float(ics.mean()), float(ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))), len(ics)


def dm_test(loss_a: np.ndarray, loss_b: np.ndarray, h: int = 1) -> tuple[float, float]:
    """Diebold-Mariano on loss differentials d = loss_a - loss_b (HAC with h-1 lags, Harvey small-sample adj.)."""
    d = np.asarray(loss_a, float) - np.asarray(loss_b, float)
    d = d[np.isfinite(d)]
    T = len(d)
    if T < 10 or np.allclose(d, d[0]):
        return float("nan"), float("nan")
    u = d - d.mean()
    lags = max(h - 1, int(np.floor(T ** (1 / 3))))
    s = u @ u / T
    for L in range(1, lags + 1):
        s += 2 * (1 - L / (lags + 1)) * (u[L:] @ u[:-L]) / T
    dm = d.mean() / np.sqrt(max(s, 1e-300) / T)
    adj = np.sqrt((T + 1 - 2 * h + h * (h - 1) / T) / T)
    stat = float(dm * adj)
    return stat, float(2 * stats.t.sf(abs(stat), T - 1))


# ------------------------------------------------------------------------------------------------ regularized


def _std(Xtr: np.ndarray, Xte: np.ndarray | None = None):
    mu, sd = Xtr.mean(0), Xtr.std(0)
    sd = np.where(sd > 0, sd, 1.0)
    return (Xtr - mu) / sd, (None if Xte is None else (Xte - mu) / sd), mu, sd


def alpha_grid(X: np.ndarray, y: np.ndarray, model: str, l1_ratio: float, n: int = 50) -> np.ndarray:
    Xs, _, _, _ = _std(X)
    yc = y - y.mean()
    if model == "ridge":
        top = float(np.linalg.norm(Xs, 2) ** 2)
        return np.logspace(np.log10(top * 10), np.log10(top * 1e-5), n)
    r = l1_ratio if model == "elasticnet" else 1.0
    amax = float(np.max(np.abs(Xs.T @ yc)) / (len(y) * max(r, 1e-3)))
    return np.logspace(np.log10(amax), np.log10(amax * 1e-4), n)


def path_fit(X: np.ndarray, y: np.ndarray, model: str, alphas: np.ndarray, l1_ratio: float) -> np.ndarray:
    """Standardized coefficients for each alpha: (n_alphas, k)."""
    from sklearn.linear_model import Ridge, enet_path

    Xs, _, _, _ = _std(X)
    yc = y - y.mean()
    if model == "ridge":
        return np.vstack([Ridge(alpha=a, fit_intercept=False).fit(Xs, yc).coef_ for a in alphas])
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        _, coefs, _ = enet_path(Xs, yc, l1_ratio=1.0 if model == "lasso" else l1_ratio, alphas=alphas, max_iter=5000)
    return coefs.T


def regularized_cv(X: np.ndarray, y: np.ndarray, folds: list, model: str, l1_ratio: float, alphas: np.ndarray,
                   check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Out-of-fold MSE per alpha (standardization fitted on each training fold) and OOS predictions."""
    mse = np.full((len(folds), len(alphas)), np.nan)
    preds = np.full((len(alphas), len(y)), np.nan)
    for f, (tr, te) in enumerate(folds):
        if check:
            check()
        Xs, Xt, mu, sd = _std(X[tr], X[te])
        B = path_fit(X[tr], y[tr], model, alphas, l1_ratio)  # standardized by the training fold
        P = y[tr].mean() + Xt @ B.T
        preds[:, te] = P.T
        mse[f] = ((P - y[te][:, None]) ** 2).mean(0)
    m = np.nanmean(mse, 0)
    se = np.nanstd(mse, 0, ddof=1) / np.sqrt(len(folds))
    best = int(np.nanargmin(m))
    one_se = int(np.min(np.where(m <= m[best] + se[best])[0]))  # largest alpha (grid is decreasing) within 1 SE
    return {"mse": m, "mse_se": se, "best": best, "one_se": one_se, "preds": preds}


def fit_regularized(X: np.ndarray, y: np.ndarray, model: str, alpha: float, l1_ratio: float) -> tuple[np.ndarray, float, np.ndarray]:
    """(coef on original scale, intercept, standardized coef)."""
    Xs, _, mu, sd = _std(X)
    b = path_fit(X, y, model, np.array([alpha]), l1_ratio)[0]
    coef = b / sd
    return coef, float(y.mean() - mu @ coef), b


# ------------------------------------------------------------------------------------------------ tree ensembles


def n_jobs() -> int:
    """Tree / boosting threads: the studio lane's per-worker cores (BIG360_STUDIO_NJOBS, S1.5), else the job's
    thread budget (OMP_NUM_THREADS from ram_budget.job_env), else cores - 2."""
    import os

    for k in ("BIG360_STUDIO_NJOBS", "OMP_NUM_THREADS"):
        try:
            v = int(os.environ.get(k) or 0)
        except ValueError:
            v = 0
        if v > 0:
            return v
    return max(1, (os.cpu_count() or 4) - 2)


def make_tree(kind: str, task: str, p: dict[str, Any], seed: int):
    if kind == "rf":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

        cls = RandomForestClassifier if task == "classification" else RandomForestRegressor
        return cls(n_estimators=int(p.get("n_estimators", 300)), max_depth=p.get("max_depth"),
                   min_samples_leaf=int(p.get("min_samples_leaf", 20)), max_features=p.get("max_features", 0.5),
                   n_jobs=n_jobs(), random_state=seed)
    if kind == "xgb":
        import xgboost as xgb

        cls = xgb.XGBClassifier if task == "classification" else xgb.XGBRegressor
        return cls(n_estimators=int(p.get("n_estimators", 300)), max_depth=int(p.get("max_depth") or 4),
                   learning_rate=float(p.get("learning_rate", 0.05)), subsample=0.8, colsample_bytree=0.8,
                   min_child_weight=float(p.get("min_samples_leaf", 20)), n_jobs=n_jobs(), random_state=seed, verbosity=0)
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

    cls = HistGradientBoostingClassifier if task == "classification" else HistGradientBoostingRegressor
    return cls(max_iter=int(p.get("n_estimators", 300)), max_depth=p.get("max_depth"),
               learning_rate=float(p.get("learning_rate", 0.05)), min_samples_leaf=int(p.get("min_samples_leaf", 20)),
               l2_regularization=float(p.get("l2", 0.0)), random_state=seed)


def model_predict(m: Any, X: np.ndarray, task: str) -> np.ndarray:
    return m.predict_proba(X)[:, 1] if task == "classification" else m.predict(X)


def cv_predict(factory: Callable[[], Any], X: np.ndarray, y: np.ndarray, folds: list, task: str,
               check: Callable[[], None] | None = None) -> np.ndarray:
    p = np.full(len(y), np.nan)
    for tr, te in folds:
        if check:
            check()
        m = factory().fit(X[tr], y[tr])
        p[te] = model_predict(m, X[te], task)
    return p


def shap_values(model: Any, X: np.ndarray, task: str) -> np.ndarray:
    import shap

    ex = shap.TreeExplainer(model)
    v = ex.shap_values(X, check_additivity=False)
    if isinstance(v, list):  # older API: one array per class
        v = v[1]
    v = np.asarray(v)
    if v.ndim == 3:
        v = v[:, :, -1]
    return v


# ------------------------------------------------------------------------------------------------ clustering


def gmm_select(X: np.ndarray, k_min: int, k_max: int, cov: str, seed: int) -> tuple[Any, list[tuple[int, float, float]]]:
    from sklearn.mixture import GaussianMixture

    rows, best = [], None
    for k in range(k_min, k_max + 1):
        g = GaussianMixture(n_components=k, covariance_type=cov, n_init=3, random_state=seed).fit(X)
        b, a = float(g.bic(X)), float(g.aic(X))
        rows.append((k, b, a))
        if best is None or b < best[0]:
            best = (b, g)
    return best[1], rows


def density_cluster(X: np.ndarray, algo: str, eps: float, min_samples: int, min_cluster_size: int) -> np.ndarray:
    if algo == "dbscan":
        from sklearn.cluster import DBSCAN

        return DBSCAN(eps=eps, min_samples=min_samples).fit_predict(X)
    from sklearn.cluster import HDBSCAN

    return HDBSCAN(min_cluster_size=min_cluster_size, min_samples=min_samples).fit_predict(X)


# ------------------------------------------------------------------------------------------------ factor models


def long_short_factor(df: pd.DataFrame, score: str, ret: str, q: int = 5) -> pd.DataFrame:
    """Per date: equal-weight return of the top minus the bottom quantile, sorting on the score known at the
    previous date (df: date, entity_id, score, ret). Returns date, ls, top, bottom, n."""
    d = df[["date", "entity_id", score, ret]].sort_values(["entity_id", "date"]).copy()
    d["s_lag"] = d.groupby("entity_id")[score].shift(1)
    d = d.dropna(subset=["s_lag", ret])
    out = []
    for dt, g in d.groupby("date", sort=True):
        if len(g) < 2 * q:
            continue
        r = g["s_lag"].rank(pct=True)
        top, bot = g.loc[r > 1 - 1 / q, ret].mean(), g.loc[r <= 1 / q, ret].mean()
        out.append((dt, top - bot, top, bot, len(g)))
    return pd.DataFrame(out, columns=["date", "ls", "top", "bottom", "n"])


def quantile_portfolios(df: pd.DataFrame, score: str, ret: str, q: int = 5) -> pd.DataFrame:
    """date x q equal-weight portfolio returns sorted on the lagged score (test assets)."""
    d = df[["date", "entity_id", score, ret]].sort_values(["entity_id", "date"]).copy()
    d["s_lag"] = d.groupby("entity_id")[score].shift(1)
    d = d.dropna(subset=["s_lag", ret])
    d["bucket"] = d.groupby("date")["s_lag"].transform(lambda s: np.ceil(s.rank(pct=True) * q).clip(1, q))
    return d.pivot_table(index="date", columns="bucket", values=ret, aggfunc="mean").rename(
        columns=lambda b: f"{score}_Q{int(b)}")


def ts_factor_regressions(R: pd.DataFrame, F: pd.DataFrame, hac_lags: int | None = None) -> dict[str, Any]:
    """Time-series regressions of each test asset on the factors (balanced sample) + GRS test."""
    import statsmodels.api as sm

    J = R.join(F, how="inner").dropna()
    assets, facs = list(R.columns), list(F.columns)
    T, N, L = len(J), len(assets), len(facs)
    if T <= N + L + 1:
        raise ValueError(f"GRS needs T > N + L + 1 (T={T}, N={N}, L={L}); use fewer test assets")
    X = sm.add_constant(J[facs].to_numpy(float))
    lags = hac_lags if hac_lags is not None else int(np.floor(4 * (T / 100) ** (2 / 9)))
    rows, alphas, resid = [], [], []
    for a in assets:
        r = sm.OLS(J[a].to_numpy(float), X).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
        alphas.append(r.params[0])
        resid.append(r.resid)
        rows.append({"asset": a, "alpha": r.params[0], "alpha_t": r.tvalues[0], "alpha_p": r.pvalues[0], "r2": r.rsquared,
                     **{f"beta_{f}": b for f, b in zip(facs, r.params[1:], strict=True)},
                     **{f"t_{f}": t for f, t in zip(facs, r.tvalues[1:], strict=True)}})
    a = np.asarray(alphas)
    E = np.vstack(resid).T
    S = E.T @ E / (T - L - 1)
    Fm = J[facs].to_numpy(float)
    mu = Fm.mean(0)
    Om = np.atleast_2d(np.cov(Fm, rowvar=False, ddof=1))
    grs = (T / N) * ((T - N - L) / (T - L - 1)) * float(a @ np.linalg.pinv(S) @ a) / (1 + float(mu @ np.linalg.pinv(Om) @ mu))
    p = float(stats.f.sf(grs, N, T - N - L))
    return {"table": pd.DataFrame(rows), "grs": grs, "grs_p": p, "T": T, "N": N, "L": L, "joined": J}


# ------------------------------------------------------------------------------------------------ leaderboard


def candidate_predict(spec: dict[str, Any], Xtr: np.ndarray, ytr: np.ndarray, Xte: np.ndarray, task: str,
                      seed: int) -> np.ndarray:
    """OOS predictions of one leaderboard candidate fitted on (Xtr, ytr)."""
    m = spec["method"]
    p = spec.get("params") or {}
    if m in ("ols", "logit") or (m == "glm" and task == "classification"):
        import statsmodels.api as sm

        A, B = sm.add_constant(Xtr, has_constant="add"), sm.add_constant(Xte, has_constant="add")
        if task == "classification":
            with _w.catch_warnings():
                _w.simplefilter("ignore")
                r = sm.Logit(ytr, A).fit(disp=0, maxiter=200)
            return r.predict(B)
        return B @ np.linalg.lstsq(A, ytr, rcond=None)[0]
    if m in ("ridge", "lasso", "elasticnet"):
        if task == "classification":
            from sklearn.linear_model import LogisticRegression

            Xs, Xt, _, _ = _std(Xtr, Xte)
            pen = {"ridge": "l2", "lasso": "l1", "elasticnet": "elasticnet"}[m]
            c = LogisticRegression(penalty=pen, C=float(p.get("C", 1.0)), solver="saga", l1_ratio=float(p.get("l1_ratio", 0.5))
                                   if m == "elasticnet" else None, max_iter=2000).fit(Xs, ytr)
            return c.predict_proba(Xt)[:, 1]
        alpha = p.get("alpha")
        if alpha is None:  # inner expanding CV on the training window
            grid = alpha_grid(Xtr, ytr, m, float(p.get("l1_ratio", 0.5)), 25)
            inner = cv_splits(np.arange(len(ytr)), "kfold", 3, seed=seed)
            cvr = regularized_cv(Xtr, ytr, inner, m, float(p.get("l1_ratio", 0.5)), grid)
            alpha = grid[cvr["best"]]
        coef, b0, _ = fit_regularized(Xtr, ytr, m, float(alpha), float(p.get("l1_ratio", 0.5)))
        return b0 + Xte @ coef
    if m in ("rf", "hgb", "xgb"):
        mod = make_tree(m, task, p, seed).fit(Xtr, ytr)
        return model_predict(mod, Xte, task)
    if m == "arima":
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        order = tuple(p.get("order", [1, 0, 0]))
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            r = SARIMAX(ytr, exog=Xtr if Xtr.shape[1] else None, order=order,
                        trend="c" if order[1] == 0 else "n").fit(disp=False, maxiter=200)
            return np.asarray(r.forecast(steps=len(Xte), exog=Xte if Xte.shape[1] else None), float)
    if m == "mean":
        return np.full(len(Xte), ytr.mean())
    raise ValueError(f"unsupported leaderboard method {m!r}")
