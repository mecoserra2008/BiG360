"""Studio v2 S3 probabilistic core (owner: model-studio): proper scoring rules (Gaussian / quantile CRPS, pinball),
PIT and calibration curves, split-conformal intervals on cross-validated predictions (reusing the conformal quantile of
app/modeling/_conformal.py), Bayesian ridge / ARD, Gaussian-process and NGBoost cross-fits, LightGBM quantile
regression and a hierarchical partial-pooling regression by group (empirical Bayes or a numpy Gibbs sampler with split
R-hat / ESS). Everything here is fitted inside the training rows of each fold.

Pure functions (numpy, scipy, scikit-learn, lightgbm, ngboost); embedded verbatim in exported scripts after
app/modeling/_ml.py, _conformal.py and _advml.py, so the names imported below already exist there.
"""

from __future__ import annotations

import math
import warnings as _w
from typing import Any, Callable

import numpy as np
from scipy import stats

try:  # inside the app; in an exported script the cores are already defined above this module
    from ._advml import make_lgbm  # noqa: F401
    from ._causal2 import n_threads  # noqa: F401
    from ._conformal import conformal_quantile  # noqa: F401
except ImportError:  # pragma: no cover
    pass

PROB_VERSION = "1.0"
#: Gaussian-process regression is O(n^3) in time and O(n^2) in memory: refuse above this many training rows
GP_HARD_CAP = 3000
_SQPI = 1.0 / math.sqrt(math.pi)


def _noop() -> None:
    return None


# ------------------------------------------------------------------------------------------------ scoring rules


def crps_gauss(y: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    """Closed-form CRPS of N(mu, sd^2) at y (Gneiting & Raftery 2007); lower is better, same units as y."""
    sd = np.maximum(np.asarray(sd, float), 1e-12)
    z = (np.asarray(y, float) - mu) / sd
    return sd * (z * (2 * stats.norm.cdf(z) - 1) + 2 * stats.norm.pdf(z) - _SQPI)


def pinball(y: np.ndarray, q: np.ndarray, tau: float) -> np.ndarray:
    u = np.asarray(y, float) - np.asarray(q, float)
    return np.maximum(tau * u, (tau - 1) * u)


def crps_quantiles(y: np.ndarray, Q: np.ndarray, taus: list[float]) -> np.ndarray:
    """CRPS approximated by 2 x the mean pinball loss over the predicted quantile levels (exact as the grid fills [0,1])."""
    return 2 * np.mean([pinball(y, Q[:, j], t) for j, t in enumerate(taus)], axis=0)


def sort_quantiles(Q: np.ndarray) -> np.ndarray:
    """Monotone rearrangement (Chernozhukov, Fernandez-Val & Galichon 2010): sorting removes quantile crossing."""
    return np.sort(Q, axis=1)


def pit_gauss(y: np.ndarray, mu: np.ndarray, sd: np.ndarray) -> np.ndarray:
    return stats.norm.cdf((np.asarray(y, float) - mu) / np.maximum(sd, 1e-12))


def pit_quantiles(y: np.ndarray, Q: np.ndarray, taus: list[float]) -> np.ndarray:
    """PIT from a predicted quantile set: linear interpolation of the CDF between quantiles, tails at half the edge level."""
    t = np.asarray(taus, float)
    out = np.empty(len(y))
    for i in range(len(y)):
        q = Q[i]
        if y[i] < q[0]:
            out[i] = t[0] / 2
        elif y[i] > q[-1]:
            out[i] = (1 + t[-1]) / 2
        else:
            out[i] = float(np.interp(y[i], q, t))
    return out


def pit_histogram(pit: np.ndarray, bins: int = 10) -> dict[str, Any]:
    """Counts per PIT bin + a chi-square test of uniformity (calibrated forecasts give a flat histogram)."""
    p = np.asarray(pit, float)
    p = p[np.isfinite(p)]
    counts, edges = np.histogram(np.clip(p, 0, 1), bins=bins, range=(0, 1))
    exp = len(p) / bins
    chi = float(((counts - exp) ** 2 / exp).sum()) if exp > 0 else float("nan")
    pv = float(stats.chi2.sf(chi, bins - 1)) if exp > 0 else float("nan")
    return {"counts": counts.tolist(), "edges": edges.tolist(), "expected": exp, "chi2": chi, "p_value": pv, "n": int(len(p))}


LEVELS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95)


def coverage_gauss(y: np.ndarray, mu: np.ndarray, sd: np.ndarray, levels: tuple[float, ...] = LEVELS) -> list[tuple[float, float]]:
    """(nominal, empirical) coverage of central Gaussian intervals; on the diagonal = calibrated."""
    out = []
    for lv in levels:
        h = stats.norm.ppf(0.5 + lv / 2) * sd
        out.append((lv, float(np.mean(np.abs(y - mu) <= h))))
    return out


def coverage_quantiles(y: np.ndarray, Q: np.ndarray, taus: list[float]) -> list[tuple[float, float]]:
    """Central intervals from symmetric quantile pairs (tau, 1 - tau)."""
    t = [round(x, 6) for x in taus]
    out = []
    for i, a in enumerate(t):
        b = round(1 - a, 6)
        if a < 0.5 and b in t:
            j = t.index(b)
            out.append((b - a, float(np.mean((y >= Q[:, i]) & (y <= Q[:, j])))))
    return sorted(out)


def interval_score(y: np.ndarray, lo: np.ndarray, hi: np.ndarray, alpha: float) -> np.ndarray:
    """Winkler / interval score of a (1 - alpha) interval: width + 2/alpha x the miss distance (lower is better)."""
    return (hi - lo) + 2 / alpha * (np.maximum(lo - y, 0) + np.maximum(y - hi, 0))


# ------------------------------------------------------------------------------------------------ conformal on CV


def fold_index(n: int, folds: list) -> np.ndarray:
    f = np.full(n, -1)
    for k, (_, te) in enumerate(folds):
        f[te] = k
    return f


def conformal_cv(y: np.ndarray, center: np.ndarray, folds: list, alpha: float, scale: np.ndarray | None = None,
                 lo: np.ndarray | None = None, hi: np.ndarray | None = None, min_cal: int = 30) -> dict[str, Any]:
    """Split-conformal intervals for out-of-fold predictions. Calibration set of test fold k = rows scored in the OTHER
    folds that lie in fold k's training rows (walk-forward: only earlier folds, so the interval never sees the future).
    Score: |y - center| / scale (absolute or normalized residual) or, with lo / hi, the CQR score max(lo - y, y - hi)
    (Romano et al. 2019). Radius = the ceil((n + 1)(1 - alpha))-th smallest score (_conformal.conformal_quantile).
    Folds with fewer than min_cal calibration rows get no interval (NaN)."""
    n = len(y)
    fi = fold_index(n, folds)
    s = np.ones(n) if scale is None else np.maximum(np.asarray(scale, float), 1e-12)
    cqr = lo is not None and hi is not None
    score = np.maximum(lo - y, y - hi) if cqr else np.abs(y - center) / s
    L, H = np.full(n, np.nan), np.full(n, np.nan)
    qs = []
    for k, (tr, te) in enumerate(folds):
        cal = tr[(fi[tr] >= 0) & (fi[tr] != k)]
        sc = np.sort(score[cal][np.isfinite(score[cal])])
        if len(sc) < min_cal:
            qs.append(None)
            continue
        q = conformal_quantile(sc, alpha)
        qs.append(q)
        if not math.isfinite(q):
            continue
        if cqr:
            L[te], H[te] = lo[te] - q, hi[te] + q
        else:
            L[te], H[te] = center[te] - q * s[te], center[te] + q * s[te]
    ok = np.isfinite(L) & np.isfinite(H) & np.isfinite(y)
    cov = float(np.mean((y[ok] >= L[ok]) & (y[ok] <= H[ok]))) if ok.any() else float("nan")
    return {"lower": L, "upper": H, "coverage": cov, "target": 1 - alpha, "mean_width": float(np.mean(H[ok] - L[ok])) if ok.any() else float("nan"),
            "n_covered_rows": int(ok.sum()), "radius_by_fold": qs, "score": "cqr" if cqr else ("normalized" if scale is not None else "absolute"),
            "interval_score": float(np.mean(interval_score(y[ok], L[ok], H[ok], alpha))) if ok.any() else float("nan")}


# ------------------------------------------------------------------------------------------------ fold helpers


def _std(Xtr: np.ndarray, Xte: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mu, sd = Xtr.mean(0), Xtr.std(0)
    sd = np.where(sd > 0, sd, 1.0)
    return (Xtr - mu) / sd, (Xte - mu) / sd


def baseline_cv(X: np.ndarray, y: np.ndarray, folds: list) -> dict[str, np.ndarray]:
    """Per fold: OLS mean + training-residual sd (linear-Gaussian baseline) and climatology N(mean_train, sd_train)."""
    n = len(y)
    out = {k: np.full(n, np.nan) for k in ("lin_mu", "lin_sd", "clim_mu", "clim_sd")}
    for tr, te in folds:
        A = np.column_stack([np.ones(len(tr)), X[tr]])
        b, *_ = np.linalg.lstsq(A, y[tr], rcond=None)
        r = y[tr] - A @ b
        out["lin_mu"][te] = np.column_stack([np.ones(len(te)), X[te]]) @ b
        out["lin_sd"][te] = math.sqrt(max(r @ r / max(len(tr) - A.shape[1], 1), 1e-18))
        out["clim_mu"][te] = y[tr].mean()
        out["clim_sd"][te] = max(y[tr].std(ddof=1), 1e-12)
    return out


def bayes_linear_cv(X: np.ndarray, y: np.ndarray, folds: list, model: str = "bayes_ridge", max_iter: int = 300,
                    check: Callable[[], None] = _noop) -> dict[str, np.ndarray]:
    """BayesianRidge / ARDRegression (evidence maximization) per fold on standardized features: predictive mean + sd."""
    from sklearn.linear_model import ARDRegression, BayesianRidge

    n = len(y)
    mu, sd = np.full(n, np.nan), np.full(n, np.nan)
    for tr, te in folds:
        check()
        Xa, Xb = _std(X[tr], X[te])
        m = (ARDRegression(max_iter=max_iter) if model == "ard" else BayesianRidge(max_iter=max_iter)).fit(Xa, y[tr])
        mu[te], sd[te] = m.predict(Xb, return_std=True)
    return {"mu": mu, "sd": sd}


def bayes_linear_fit(X: np.ndarray, y: np.ndarray, model: str = "bayes_ridge", max_iter: int = 300) -> dict[str, Any]:
    """Full-sample fit: posterior mean / sd of each coefficient on the original feature scale, the noise and weight
    precisions (alpha_, lambda_) and, for ARD, the per-feature relevance (posterior precision lambda_j)."""
    from sklearn.linear_model import ARDRegression, BayesianRidge

    mu, sdv = X.mean(0), X.std(0)
    sdv = np.where(sdv > 0, sdv, 1.0)
    Z = (X - mu) / sdv
    m = (ARDRegression(max_iter=max_iter) if model == "ard" else BayesianRidge(max_iter=max_iter)).fit(Z, y)
    cov = np.asarray(m.sigma_, float)
    coef = np.asarray(m.coef_, float)
    if model == "ard":  # sigma_ only covers the kept (lambda below threshold) features
        keep = np.asarray(m.lambda_ < m.threshold_lambda)
        se = np.zeros(len(coef))
        se[keep] = np.sqrt(np.clip(np.diag(cov), 0, None))[: int(keep.sum())]
        lam = np.asarray(m.lambda_, float)
    else:
        se = np.sqrt(np.clip(np.diag(cov), 0, None))
        lam = np.full(len(coef), float(m.lambda_))
    return {"coef": coef / sdv, "se": se / sdv, "coef_std": coef, "se_std": se, "intercept": float(m.intercept_ - (coef / sdv) @ mu),
            "alpha": float(m.alpha_), "lambda": lam, "noise_sd": 1 / math.sqrt(max(float(m.alpha_), 1e-300)),
            "n_iter": int(getattr(m, "n_iter_", 0) or 0), "model": m, "x_mean": mu, "x_sd": sdv}


def gp_kernel(kind: str, n_x: int, ard: bool):
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel, Matern, RationalQuadratic, WhiteKernel

    ls = np.ones(n_x) if ard else 1.0
    base = {"rbf": RBF(length_scale=ls, length_scale_bounds=(1e-2, 1e3)),
            "matern32": Matern(length_scale=ls, nu=1.5, length_scale_bounds=(1e-2, 1e3)),
            "matern52": Matern(length_scale=ls, nu=2.5, length_scale_bounds=(1e-2, 1e3)),
            "rational_quadratic": RationalQuadratic(length_scale=1.0, alpha=1.0)}[kind]
    return ConstantKernel(1.0, (1e-3, 1e3)) * base + WhiteKernel(0.5, (1e-6, 1e2))


def gp_check_n(n_train: int, cap: int) -> None:
    lim = min(int(cap), GP_HARD_CAP)
    if n_train > lim:
        raise ValueError(f"Gaussian-process regression is O(n^3): {n_train} training rows exceed the cap of {lim} "
                         f"(about {n_train ** 2 * 8 / 1e9:.1f} GB for one kernel matrix). Use a shorter window, a "
                         "sector / region subset, a lower frequency, or bayes_ridge / ngboost for large n.")


def gp_cv(X: np.ndarray, y: np.ndarray, folds: list, kernel: str, ard: bool, restarts: int, seed: int,
          check: Callable[[], None] = _noop) -> dict[str, Any]:
    """GaussianProcessRegressor per fold (standardized X, normalized y; kernel hyper-parameters by marginal likelihood)."""
    from sklearn.gaussian_process import GaussianProcessRegressor

    n = len(y)
    mu, sd = np.full(n, np.nan), np.full(n, np.nan)
    kernels = []
    for tr, te in folds:
        check()
        Xa, Xb = _std(X[tr], X[te])
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            g = GaussianProcessRegressor(gp_kernel(kernel, X.shape[1], ard), normalize_y=True, n_restarts_optimizer=restarts,
                                         random_state=seed).fit(Xa, y[tr])
        mu[te], sd[te] = g.predict(Xb, return_std=True)
        kernels.append(str(g.kernel_))
    return {"mu": mu, "sd": sd, "kernels": kernels}


def gp_fit(X: np.ndarray, y: np.ndarray, kernel: str, ard: bool, restarts: int, seed: int) -> Any:
    from sklearn.gaussian_process import GaussianProcessRegressor

    mu, sdv = X.mean(0), X.std(0)
    sdv = np.where(sdv > 0, sdv, 1.0)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        g = GaussianProcessRegressor(gp_kernel(kernel, X.shape[1], ard), normalize_y=True, n_restarts_optimizer=restarts,
                                     random_state=seed).fit((X - mu) / sdv, y)
    return g, mu, sdv


def quantile_lgbm_cv(X: np.ndarray, y: np.ndarray, folds: list, taus: list[float], p: dict[str, Any], seed: int,
                     check: Callable[[], None] = _noop) -> dict[str, Any]:
    """One LightGBM model per quantile level (objective='quantile', alpha=tau) per fold; crossing removed by sorting."""
    n = len(y)
    Q = np.full((n, len(taus)), np.nan)
    for tr, te in folds:
        for j, t in enumerate(taus):
            check()
            m = make_lgbm("regression", {**p, "alpha": t}, seed, objective="quantile").fit(X[tr], y[tr])
            Q[te, j] = m.predict(X[te])
    raw_cross = float(np.mean(np.any(np.diff(Q[np.isfinite(Q).all(1)], axis=1) < 0, axis=1))) if np.isfinite(Q).all(1).any() else float("nan")
    ok = np.isfinite(Q).all(1)
    Q[ok] = sort_quantiles(Q[ok])
    return {"Q": Q, "crossing_rate": raw_cross}


def clim_quantiles_cv(y: np.ndarray, folds: list, taus: list[float]) -> np.ndarray:
    """Unconditional training-fold quantiles (the climatological baseline for a quantile forecast)."""
    Q = np.full((len(y), len(taus)), np.nan)
    for tr, te in folds:
        Q[te] = np.quantile(y[tr], taus)
    return Q


def ngboost_model(n_estimators: int, learning_rate: float, max_depth: int, minibatch_frac: float, seed: int,
                  min_samples_leaf: int = 50):
    from ngboost import NGBRegressor
    from ngboost.distns import Normal
    from sklearn.tree import DecisionTreeRegressor

    base = DecisionTreeRegressor(max_depth=max_depth, min_samples_leaf=min_samples_leaf, random_state=seed)
    return NGBRegressor(Dist=Normal, Base=base, n_estimators=n_estimators, learning_rate=learning_rate,
                        minibatch_frac=minibatch_frac, random_state=seed, verbose=False)


def ngboost_cv(X: np.ndarray, y: np.ndarray, folds: list, prm: dict[str, Any], seed: int,
               check: Callable[[], None] = _noop, dates: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """NGBoost (natural-gradient boosting of a Normal's location and log-scale) per fold; the last 15 % of each training
    fold is the early-stopping validation block."""
    n = len(y)
    mu, sd = np.full(n, np.nan), np.full(n, np.nan)
    iters = []
    for tr, te in folds:
        check()
        tr = tr[np.argsort(dates[tr], kind="stable")] if dates is not None else tr
        cut = int(len(tr) * 0.85)
        a, v = tr[:cut], tr[cut:]
        m = ngboost_model(seed=seed, **prm)
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            m.fit(X[a], y[a], X_val=X[v], Y_val=y[v], early_stopping_rounds=prm.get("n_estimators", 500) // 10 or None)
        d = m.pred_dist(X[te])
        mu[te], sd[te] = d.params["loc"], d.params["scale"]
        iters.append(int(getattr(m, "best_val_loss_itr", None) or m.n_estimators))
    return {"mu": mu, "sd": sd, "best_iters": iters}


# ------------------------------------------------------------------------------------------------ hierarchical pooling


def _design(X: np.ndarray) -> np.ndarray:
    return np.column_stack([np.ones(len(X)), X])


def group_stats(X: np.ndarray, y: np.ndarray, g: np.ndarray, groups: list[Any]) -> dict[str, Any]:
    A = _design(X)
    XtX = np.array([A[g == j].T @ A[g == j] for j in groups])
    Xty = np.array([A[g == j].T @ y[g == j] for j in groups])
    n = np.array([int((g == j).sum()) for j in groups])
    return {"A": A, "XtX": XtX, "Xty": Xty, "n": n}


def no_pool(X: np.ndarray, y: np.ndarray, g: np.ndarray, groups: list[Any]) -> tuple[np.ndarray, np.ndarray, float]:
    """Separate OLS per group (ridge 1e-8 for tiny groups); returns betas, their covariance and the pooled residual var."""
    st = group_stats(X, y, g, groups)
    k = st["A"].shape[1]
    B, V, ssr, df = np.zeros((len(groups), k)), np.zeros((len(groups), k, k)), 0.0, 0
    for i, j in enumerate(groups):
        M = st["XtX"][i] + 1e-8 * np.eye(k)
        B[i] = np.linalg.solve(M, st["Xty"][i])
        r = y[g == j] - st["A"][g == j] @ B[i]
        ssr += float(r @ r)
        df += max(int(st["n"][i]) - k, 0)
    s2 = ssr / max(df, 1)
    for i in range(len(groups)):
        V[i] = s2 * np.linalg.pinv(st["XtX"][i] + 1e-8 * np.eye(k))
    return B, V, s2


def eb_pool(X: np.ndarray, y: np.ndarray, g: np.ndarray, groups: list[Any], n_iter: int = 200) -> dict[str, Any]:
    """Empirical-Bayes normal-normal partial pooling: beta_j ~ N(mu, diag(tau^2)), beta_hat_j | beta_j ~ N(beta_j, V_j).
    mu and tau^2 by an EM / fixed-point iteration on the marginal likelihood; posterior beta_j = precision-weighted mix
    of the group OLS and mu (James-Stein-type shrinkage)."""
    B, V, s2 = no_pool(X, y, g, groups)
    J, k = B.shape
    mu = B.mean(0)
    tau2 = np.maximum(B.var(0, ddof=1) if J > 1 else np.ones(k), 1e-10)
    for _ in range(n_iter):
        P = np.array([np.linalg.inv(np.linalg.inv(V[i] + 1e-12 * np.eye(k)) + np.diag(1 / tau2)) for i in range(J)])
        M = np.array([P[i] @ (np.linalg.solve(V[i] + 1e-12 * np.eye(k), B[i]) + mu / tau2) for i in range(J)])
        mu_new = M.mean(0)
        tau2_new = np.maximum(((M - mu_new) ** 2 + np.array([np.diag(P[i]) for i in range(J)])).mean(0), 1e-10)
        if np.allclose(mu_new, mu, atol=1e-10) and np.allclose(tau2_new, tau2, rtol=1e-6):
            mu, tau2 = mu_new, tau2_new
            break
        mu, tau2 = mu_new, tau2_new
    shrink = np.array([1 - np.diag(P[i]) / np.maximum(np.diag(V[i]), 1e-300) for i in range(J)])
    return {"beta": M, "cov": P, "mu": mu, "tau2": tau2, "sigma2": s2, "beta_nopool": B, "cov_nopool": V,
            "shrinkage": np.clip(1 - shrink, 0, 1)}


def gibbs_pool(X: np.ndarray, y: np.ndarray, g: np.ndarray, groups: list[Any], n_iter: int = 2000, burn: int = 1000,
               chains: int = 4, seed: int = 42, check: Callable[[], None] = _noop) -> dict[str, Any]:
    """Blocked Gibbs sampler for y_i = x_i' beta_g(i) + e_i, e ~ N(0, s2); beta_j ~ N(mu, diag(tau2)); flat prior on mu;
    tau_k ~ half-Cauchy(0, A_k) through its inverse-gamma mixture (Makalic & Schmidt 2016; A_k = the spread of the
    per-group OLS estimates, weakly informative); p(s2) ~ 1 / s2. Each sweep draws mu from its conditional with the
    betas integrated out (Woodbury, k x k per group), then beta_j | mu, then tau2 / its auxiliary, then s2: blocking
    (mu, beta) removes the strong mu-beta correlation of the centered parameterization. All conditionals are
    conjugate. Returns post-burn-in draws per chain (over-dispersed starting values, one seed per chain)."""
    st = group_stats(X, y, g, groups)
    A, XtX, Xty = st["A"], st["XtX"], st["Xty"]
    J, k = len(groups), A.shape[1]
    yty = float(y @ y)  # SSR from sufficient statistics: y'y - 2 b'X'y + b'X'Xb per group (O(k^2), not O(n))
    B0, V0, s20 = no_pool(X, y, g, groups)
    scale = np.maximum(np.maximum(B0.std(0) if J > 1 else np.ones(k), np.sqrt(np.median([np.diag(v) for v in V0], 0))), 1e-6)
    n = len(y)
    keep = n_iter - burn
    D = {"beta": np.zeros((chains, keep, J, k)), "mu": np.zeros((chains, keep, k)), "tau2": np.zeros((chains, keep, k)),
         "sigma2": np.zeros((chains, keep))}
    I = np.eye(k)
    for c in range(chains):
        rng = np.random.default_rng(seed + 1000 * c)
        beta = B0 + rng.normal(scale=0.5 * scale, size=B0.shape)  # over-dispersed starts
        tau2 = (scale * rng.uniform(0.3, 2.0, k)) ** 2
        aux = np.ones(k)
        s2 = s20 * rng.uniform(0.5, 2.0)
        for it in range(n_iter):
            if it % 200 == 0:
                check()
            Ti = np.diag(1 / tau2)
            Pm, h, Ks = np.zeros((k, k)), np.zeros(k), []
            for j in range(J):  # mu | tau2, s2, y (betas integrated out)
                M, cc = XtX[j] / s2, Xty[j] / s2
                K = np.linalg.inv(Ti + M)
                Ks.append(K)
                Pm += M - M @ K @ M
                h += cc - M @ K @ cc
            Pm = (Pm + Pm.T) / 2 + 1e-12 * I
            Lm = np.linalg.cholesky(Pm)
            mu = np.linalg.solve(Pm, h) + np.linalg.solve(Lm.T, rng.standard_normal(k))
            for j in range(J):  # beta_j | mu, tau2, s2
                K = (Ks[j] + Ks[j].T) / 2
                m = K @ (Xty[j] / s2 + mu / tau2)
                beta[j] = m + np.linalg.cholesky(K + 1e-15 * I) @ rng.standard_normal(k)
            ss = ((beta - mu) ** 2).sum(0)
            tau2 = (1 / aux + ss / 2) / rng.gamma((J + 1) / 2, 1.0, k)  # tau2_k | beta, mu, aux
            aux = (1 / scale ** 2 + 1 / tau2) / rng.gamma(1.0, 1.0, k)  # half-Cauchy auxiliary
            ssr = max(float(yty - 2 * np.einsum("jk,jk->", beta, Xty) + np.einsum("jk,jkl,jl->", beta, XtX, beta)), 1e-12)
            s2 = (ssr / 2) / rng.gamma(n / 2, 1.0)  # s2 | beta (Jeffreys)
            if it >= burn:
                t = it - burn
                D["beta"][c, t], D["mu"][c, t], D["tau2"][c, t], D["sigma2"][c, t] = beta, mu, tau2, s2
    return D


def split_rhat(x: np.ndarray) -> float:
    """Split R-hat (Gelman et al. BDA3 / Vehtari et al. 2021, without rank normalization) for draws [chains, n]."""
    c, n = x.shape
    h = n // 2
    if h < 2:
        return float("nan")
    s = np.concatenate([x[:, :h], x[:, h:2 * h]], axis=0)
    m = s.shape[0]
    means, vars_ = s.mean(1), s.var(1, ddof=1)
    W, Bv = vars_.mean(), h * means.var(ddof=1)
    if W <= 0:
        return 1.0
    var_hat = (h - 1) / h * W + Bv / h
    return float(np.sqrt(var_hat / W)) if m > 1 else float("nan")


def ess(x: np.ndarray) -> float:
    """Effective sample size over chains [chains, n] with Geyer's initial monotone positive-sequence estimator."""
    c, n = x.shape
    if n < 4:
        return float("nan")
    xc = x - x.mean(1, keepdims=True)
    f = np.fft.rfft(xc, n=2 * n, axis=1)
    ac = np.fft.irfft(f * np.conj(f), axis=1)[:, :n] / n
    var = ac[:, 0]
    if np.all(var <= 0):
        return float(c * n)
    W = x.var(1, ddof=1).mean()
    var_plus = (n - 1) / n * W + (x.mean(1).var(ddof=1) if c > 1 else 0.0)
    rho = 1 - (W - ac.mean(0)) / max(var_plus, 1e-300)
    rho[0] = 1.0
    tau, t = 1.0, 1
    prev = np.inf
    while t + 1 < n:
        pair = rho[t] + rho[t + 1]
        if pair < 0:
            break
        pair = min(pair, prev)
        tau += 2 * pair
        prev = pair
        t += 2
    return float(c * n / max(tau, 1e-12))


def gibbs_summary(D: dict[str, Any]) -> dict[str, Any]:
    beta = D["beta"]
    c, n, J, k = beta.shape
    rh = np.array([[split_rhat(beta[:, :, j, q]) for q in range(k)] for j in range(J)])
    es = np.array([[ess(beta[:, :, j, q]) for q in range(k)] for j in range(J)])
    flat = beta.reshape(c * n, J, k)
    return {"beta": flat.mean(0), "beta_sd": flat.std(0, ddof=1), "beta_lo": np.quantile(flat, 0.025, axis=0),
            "beta_hi": np.quantile(flat, 0.975, axis=0), "cov": np.array([np.cov(flat[:, j, :].T).reshape(k, k) for j in range(J)]),
            "mu": D["mu"].reshape(c * n, k).mean(0), "mu_sd": D["mu"].reshape(c * n, k).std(0, ddof=1),
            "tau2": D["tau2"].reshape(c * n, k).mean(0), "sigma2": float(D["sigma2"].mean()),
            "rhat_beta": rh, "ess_beta": es,
            "rhat_mu": np.array([split_rhat(D["mu"][:, :, q]) for q in range(k)]),
            "ess_mu": np.array([ess(D["mu"][:, :, q]) for q in range(k)]),
            "rhat_tau": np.array([split_rhat(np.log(D["tau2"][:, :, q])) for q in range(k)]),
            "rhat_sigma": split_rhat(D["sigma2"]), "ess_sigma": ess(D["sigma2"]),
            "trace_mu": D["mu"][:, :, :], "n_draws": int(c * n)}


def pool_fit(X: np.ndarray, y: np.ndarray, g: np.ndarray, groups: list[Any], estimator: str, gibbs: dict[str, Any],
             seed: int, check: Callable[[], None] = _noop) -> dict[str, Any]:
    if estimator == "gibbs":
        D = gibbs_pool(X, y, g, groups, seed=seed, check=check, **gibbs)
        s = gibbs_summary(D)
        s["estimator"] = "gibbs"
        return s
    e = eb_pool(X, y, g, groups)
    sd = np.sqrt(np.clip(np.array([np.diag(P) for P in e["cov"]]), 0, None))
    return {"beta": e["beta"], "beta_sd": sd, "beta_lo": e["beta"] - 1.96 * sd, "beta_hi": e["beta"] + 1.96 * sd,
            "cov": e["cov"], "mu": e["mu"], "tau2": e["tau2"], "sigma2": e["sigma2"], "shrinkage": e["shrinkage"],
            "estimator": "empirical_bayes"}


def pool_predict(fit: dict[str, Any], X: np.ndarray, g: np.ndarray, groups: list[Any]) -> tuple[np.ndarray, np.ndarray]:
    """Posterior predictive mean / sd (vectorized per group); a group unseen in training uses the population mean mu
    and diag(tau2)."""
    A = _design(X)
    mu, sd = np.empty(len(X)), np.empty(len(X))
    pos = {str(j): i for i, j in enumerate(groups)}
    gs = np.asarray([str(v) for v in g])
    for key in np.unique(gs):
        ix = np.where(gs == key)[0]
        j = pos.get(key)
        b, C = (fit["mu"], np.diag(fit["tau2"])) if j is None else (fit["beta"][j], fit["cov"][j])
        Ai = A[ix]
        mu[ix] = Ai @ b
        sd[ix] = np.sqrt(np.maximum(fit["sigma2"] + np.einsum("ij,jk,ik->i", Ai, C, Ai), 1e-18))
    return mu, sd


def pool_cv(X: np.ndarray, y: np.ndarray, g: np.ndarray, folds: list, estimator: str, gibbs: dict[str, Any], seed: int,
            check: Callable[[], None] = _noop) -> dict[str, np.ndarray]:
    """Partial pooling vs complete pooling (one OLS) vs no pooling (OLS per group) on the same folds."""
    n = len(y)
    out = {k: np.full(n, np.nan) for k in ("mu", "sd", "nopool", "pooled")}
    for tr, te in folds:
        check()
        groups = sorted({str(v) for v in g[tr]})
        f = pool_fit(X[tr], y[tr], g[tr], groups, estimator, gibbs, seed, check)
        out["mu"][te], out["sd"][te] = pool_predict(f, X[te], g[te], groups)
        B, _, _ = no_pool(X[tr], y[tr], g[tr], groups)
        A = _design(X[te])
        pos = {j: i for i, j in enumerate(groups)}
        bp, *_ = np.linalg.lstsq(_design(X[tr]), y[tr], rcond=None)
        out["pooled"][te] = A @ bp
        gt = np.asarray([str(v) for v in g[te]])
        Bt = np.array([B[pos[k]] if k in pos else bp for k in gt]) if len(gt) else np.zeros((0, A.shape[1]))
        out["nopool"][te] = np.einsum("ij,ij->i", A, Bt)
    return out
