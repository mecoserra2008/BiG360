"""Filtered conformal ellipsoid core for Model Studio (owner: conformal; Limmer 2026, arXiv:2606.17014v2). numpy only,
embedded verbatim by app.modeling.export so an exported script reproduces the run without the backend.

A frozen filter emits, before Y_{t+1} is observed, a predictive mean mu_t and covariance S_t; the score
R_t = (Y_{t+1} - mu_t)' S_t^-1 (Y_{t+1} - mu_t) is calibrated by split conformal over a rolling window of past realised
scores (radius q = the ceil((n + 1)(1 - alpha))-th smallest), optionally ACI-adjusted (Gibbs & Candes 2021). The set
{y : (y - mu)' S^-1 (y - mu) <= q} has MARGINAL coverage ~ 1 - alpha over time; projections give a band for any
linear combination a'Y: a'mu +- sqrt(q a'S a). MODEL-BASED.
"""

import math

import numpy as np

FILTERS = ("ewma", "static_shrink", "ewma_vol_static_corr")


def ewma_filter(X, lam=0.94, lam_corr=0.985, shrink=0.1, init=30):
    """Row t = law of X[t+1] from X[:t+1]: zero mean, EWMA volatilities (lam) and an EWMA correlation of the
    standardised returns (lam_corr) shrunk toward the identity. NaN rows are skipped (no update)."""
    X = np.asarray(X, dtype=float)
    T, N = X.shape
    head = X[: max(init, 5)]
    v = np.nanvar(head, axis=0)
    v = np.where(np.isfinite(v) & (v > 0), v, np.nanmedian(v[np.isfinite(v) & (v > 0)]) if np.any(v > 0) else 1.0)
    C = np.eye(N)
    mu = np.zeros((T, N))
    S = np.empty((T, N, N))
    for t in range(T):
        x = X[t]
        if np.all(np.isfinite(x)):
            z = x / np.sqrt(v)
            C = lam_corr * C + (1 - lam_corr) * np.outer(z, z)
            v = lam * v + (1 - lam) * x * x
        d = np.sqrt(np.diag(C))
        R = C / np.outer(d, d)
        R = (1 - shrink) * R + shrink * np.eye(N)
        sd = np.sqrt(v)
        S[t] = R * np.outer(sd, sd)
    return mu, S


def static_filter(X, fit_rows, shrink=0.1):
    """Mean and covariance of the pretrain block (frozen), covariance shrunk toward its diagonal."""
    X = np.asarray(X, dtype=float)
    F = X[fit_rows]
    F = F[np.all(np.isfinite(F), axis=1)]
    m = F.mean(axis=0)
    C = np.cov(F, rowvar=False).reshape(X.shape[1], X.shape[1])
    C = (1 - shrink) * C + shrink * np.diag(np.diag(C))
    T = X.shape[0]
    return np.tile(m, (T, 1)), np.tile(C, (T, 1, 1))


def ewma_vol_static_corr(X, fit_rows, lam=0.94, shrink=0.1):
    """EWMA volatilities with the pretrain block's (frozen, shrunk) correlation."""
    _, Cs = static_filter(X, fit_rows, shrink)
    C0 = Cs[0]
    d0 = np.sqrt(np.diag(C0))
    R = C0 / np.outer(d0, d0)
    _, Se = ewma_filter(X, lam=lam, lam_corr=0.0, shrink=1.0)
    sd = np.sqrt(np.diagonal(Se, axis1=1, axis2=2))
    S = R[None] * sd[:, :, None] * sd[:, None, :]
    return np.zeros((X.shape[0], X.shape[1])), S


def scores(X, mu, S):
    """R[t] = squared Mahalanobis distance of X[t+1] from the law emitted at t (NaN when incomplete)."""
    X = np.asarray(X, dtype=float)
    T, N = X.shape
    R = np.full(T, np.nan)
    for t in range(T - 1):
        y = X[t + 1]
        if not np.all(np.isfinite(y)):
            continue
        r = y - mu[t]
        try:
            R[t] = float(r @ np.linalg.solve(S[t] + 1e-12 * np.eye(N), r))
        except np.linalg.LinAlgError:
            R[t] = float(r @ np.linalg.pinv(S[t]) @ r)
    return R


def conformal_quantile(sorted_scores, alpha):
    """ceil((n + 1)(1 - alpha))-th smallest score; inf when that index exceeds n (or alpha <= 0)."""
    n = len(sorted_scores)
    if alpha <= 0 or n == 0:
        return math.inf
    k = math.ceil((n + 1) * (1 - alpha))
    if k > n:
        return math.inf
    return float(sorted_scores[max(k, 1) - 1])


def calibrate(R, start, alpha, window=250, min_n=50, aci_gamma=0.0):
    """Radius q[t] for decision times t >= start from the last `window` realised scores R[s], s < t (score s is
    realised at s + 1 <= t). With aci_gamma > 0 the level alpha_t adapts online to the observed misses."""
    T = len(R)
    q = np.full(T, np.nan)
    a_t = alpha
    alphas = np.full(T, np.nan)
    for t in range(start, T):
        past = R[max(0, t - window):t]
        past = np.sort(past[np.isfinite(past)])
        if len(past) >= min_n:
            q[t] = conformal_quantile(past, a_t)
        alphas[t] = a_t
        if aci_gamma > 0 and np.isfinite(R[t]) and np.isfinite(q[t]):
            a_t = a_t + aci_gamma * (alpha - float(R[t] > q[t]))
    return q, alphas


def run(X, filter_kind="ewma", alpha=0.1, pretrain_frac=0.3, window=250, min_n=50, aci_gamma=0.0, lam=0.94,
        lam_corr=0.985, shrink=0.1):
    """Full protocol on a (T, N) matrix of (already transformed) observations. Returns arrays and summary numbers."""
    X = np.asarray(X, dtype=float)
    T, N = X.shape
    t_fit = max(int(T * pretrain_frac), 30)
    fit_rows = slice(0, t_fit)
    if filter_kind == "ewma":
        mu, S = ewma_filter(X, lam, lam_corr, shrink)
    elif filter_kind == "static_shrink":
        mu, S = static_filter(X, fit_rows, shrink)
    elif filter_kind == "ewma_vol_static_corr":
        mu, S = ewma_vol_static_corr(X, fit_rows, lam, shrink)
    else:
        raise ValueError(f"unknown filter {filter_kind!r}")
    R = scores(X, mu, S)
    q, alphas = calibrate(R, t_fit, alpha, window, min_n, aci_gamma)
    test = np.isfinite(R) & np.isfinite(q)
    test[: t_fit] = False
    cov = float(np.mean(R[test] <= q[test])) if test.any() else float("nan")
    diag = np.diagonal(S, axis1=1, axis2=2)
    fin = test & np.isfinite(q) & (q < np.inf)
    w_proj = float(np.mean(2 * np.sqrt(q[fin, None] * diag[fin]))) if fin.any() else float("nan")
    logv = []
    for t in np.nonzero(fin)[0]:
        sign, ld = np.linalg.slogdet(S[t])
        if sign > 0:
            logv.append(0.5 * (N * math.log(max(q[t], 1e-300)) + ld) / N)
    member_cov = None
    if fin.any():
        idx = np.nonzero(fin)[0]
        resid = np.abs(X[idx + 1] - mu[idx])
        member_cov = np.mean(resid <= np.sqrt(q[idx, None] * diag[idx]), axis=0)
    # next-step set from the last emitted law
    last = T - 1
    past = R[max(0, last - window):last]
    past = np.sort(past[np.isfinite(past)])
    q_next = conformal_quantile(past, alphas[last] if np.isfinite(alphas[last]) else alpha) if len(past) >= min_n else float("nan")
    return {"mu": mu, "S": S, "R": R, "q": q, "alphas": alphas, "test": test, "coverage": cov, "n_test": int(test.sum()),
            "w_proj": w_proj, "log_volume": float(np.mean(logv)) if logv else float("nan"), "member_coverage": member_cov,
            "q_next": q_next, "t_fit": t_fit}


def projection(mu, S, q, a):
    """Band a'mu +- sqrt(q a'S a) of the ellipsoid {y: (y - mu)' S^-1 (y - mu) <= q}."""
    a = np.asarray(a, dtype=float)
    m = float(a @ mu)
    hw = math.sqrt(max(q, 0.0) * float(a @ S @ a))
    return m - hw, m + hw


def contributions(r, S):
    """Exact additive split of the score: r_i (S^-1 r)_i, and conditional z (S^-1 r)_i / sqrt((S^-1)_ii)."""
    P = np.linalg.pinv(S)
    w = P @ r
    return r * w, w / np.sqrt(np.maximum(np.diag(P), 1e-300))
