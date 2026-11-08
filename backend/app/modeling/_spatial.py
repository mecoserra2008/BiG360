"""Spatial / spillover estimation core (owner: event-models; used by Model Studio spatial methods and the
app.events_models jobs). Pure numpy / scipy / pandas: exported Studio scripts embed this file verbatim.

Panel layout: arrays stacked time-major, y[t * N + i] (T blocks of the N cross-section units, balanced).
W is N x N (zero diagonal); estimators row-standardize it unless told otherwise.

Contents
* weights helpers: row_standardize, spatial_lag
* ML spatial panels (Elhorst 2014): SAR (spatial lag), SEM (spatial error), SDM (SAR + W X), SLX (OLS + W X), with
  entity / two-way / no fixed effects (within transform), numerical-Hessian standard errors
* LeSage-Pace direct / indirect / total effects with simulation inference
* Anselin LM diagnostics (lag, error, robust) on OLS residuals
* Diebold-Yilmaz connectedness (generalized FEVD of a VAR, optional ridge), rolling
* panel local projections (Jorda) with Driscoll-Kraay standard errors
* spatial VAR impulse responses
"""

import warnings as _sw

import numpy as np
import pandas as pd
from scipy import optimize as _opt
from scipy import stats as _st

# ------------------------------------------------------------------------------------------------ weights


def row_standardize(W):
    W = np.asarray(W, dtype=float).copy()
    np.fill_diagonal(W, 0.0)
    rs = W.sum(axis=1)
    rs[rs == 0] = 1.0
    return W / rs[:, None]


def spatial_lag(v, W, N):
    """W applied block-wise to a time-major stacked vector / matrix (T*N [, k])."""
    v = np.asarray(v, dtype=float)
    T = v.shape[0] // N
    if v.ndim == 1:
        return (v.reshape(T, N) @ W.T).reshape(-1)
    k = v.shape[1]
    return np.einsum("ij,tjk->tik", W, v.reshape(T, N, k)).reshape(T * N, k)


def demean_panel(a, N, fe="entity"):
    """Within transform of a time-major stacked array. fe: none | entity | time | twoway."""
    a = np.asarray(a, dtype=float)
    if fe == "none":
        return a
    T = a.shape[0] // N
    sh = (T, N) + a.shape[1:]
    A = a.reshape(sh)
    out = A.copy()
    if fe in ("entity", "twoway"):
        out = out - A.mean(axis=0, keepdims=True)
    if fe in ("time", "twoway"):
        out = out - A.mean(axis=1, keepdims=True)
        if fe == "twoway":
            out = out + A.mean(axis=(0, 1), keepdims=True)
    return out.reshape(a.shape)


def _eig(W):
    w = np.linalg.eigvals(W)
    return w


def _logdet(rho, w):
    return float(np.sum(np.log(1.0 - rho * w)).real)


def _bounds(w):
    re = np.real(w[np.abs(np.imag(w)) < 1e-9]) if np.any(np.abs(np.imag(w)) < 1e-9) else np.real(w)
    lo = 1.0 / re.min() if re.min() < 0 else -0.999
    hi = 1.0 / re.max() if re.max() > 0 else 0.999
    return max(lo, -0.999) + 1e-6, min(hi, 0.999) - 1e-6


def _num_hess(f, x, eps=1e-4):
    x = np.asarray(x, dtype=float)
    k = len(x)
    H = np.zeros((k, k))
    h = eps * np.maximum(np.abs(x), 1.0)
    for i in range(k):
        for j in range(i, k):
            xpp = x.copy(); xpp[i] += h[i]; xpp[j] += h[j]
            xpm = x.copy(); xpm[i] += h[i]; xpm[j] -= h[j]
            xmp = x.copy(); xmp[i] -= h[i]; xmp[j] += h[j]
            xmm = x.copy(); xmm[i] -= h[i]; xmm[j] -= h[j]
            H[i, j] = H[j, i] = (f(xpp) - f(xpm) - f(xmp) + f(xmm)) / (4 * h[i] * h[j])
    return H


def _ols(y, X):
    XtX = X.T @ X
    b = np.linalg.solve(XtX, X.T @ y) if np.linalg.matrix_rank(XtX) == XtX.shape[0] else np.linalg.lstsq(X, y, rcond=None)[0]
    return b, y - X @ b


def _prep(y, X, W, N, fe, durbin, add_const):
    """-> (y~, X~ incl. W X when durbin, names suffix info, W row-standardized)."""
    y = np.asarray(y, dtype=float).reshape(-1)
    X = np.asarray(X, dtype=float)
    if X.ndim == 1:
        X = X[:, None]
    if y.shape[0] % N or X.shape[0] != y.shape[0]:
        raise ValueError("y / X length must be T * N (balanced, time-major)")
    WX = spatial_lag(X, W, N) if durbin else np.empty((X.shape[0], 0))
    Z = np.column_stack([X, WX])
    yt, Zt = demean_panel(y, N, fe), demean_panel(Z, N, fe)
    if fe == "none" and add_const:
        Zt = np.column_stack([np.ones(len(yt)), Zt])
    return yt, Zt


def sar_panel(y, X, W, N, fe="entity", durbin=False, add_const=True, standardize=True):
    """ML spatial lag (SAR) panel; durbin=True -> spatial Durbin (SDM, regressors [X, W X]).
    Returns dict: rho, beta, sigma2, cov (beta..., rho, sigma2), se, llf, n, k, aic, bic, resid, fitted."""
    W = row_standardize(W) if standardize else np.asarray(W, dtype=float)
    yt, Zt = _prep(y, X, W, N, fe, durbin, add_const)
    n = len(yt)
    T = n // N
    w = _eig(W)
    Wy = spatial_lag(yt, W, N)
    b0, e0 = _ols(yt, Zt)
    bd, ed = _ols(Wy, Zt)
    lo, hi = _bounds(w)

    def conc(r):
        e = e0 - r * ed
        s2 = e @ e / n
        return -(-n / 2 * np.log(s2) + T * _logdet(r, w))

    r = _opt.minimize_scalar(conc, bounds=(lo, hi), method="bounded", options={"xatol": 1e-8}).x
    beta = b0 - r * bd
    e = yt - r * Wy - Zt @ beta
    s2 = float(e @ e / n)
    k = Zt.shape[1]

    def ll(th):
        b, rr, sg = th[:k], th[k], th[k + 1]
        if sg <= 0 or not (lo < rr < hi):
            return -1e300
        ee = yt - rr * Wy - Zt @ b
        return -n / 2 * np.log(2 * np.pi * sg) + T * _logdet(rr, w) - ee @ ee / (2 * sg)

    th = np.concatenate([beta, [r, s2]])
    cov = _safe_inv(-_num_hess(ll, th))
    llf = float(ll(th))
    yhat = yt - e
    return {"model": "sdm" if durbin else "sar", "rho": float(r), "beta": beta, "sigma2": s2, "cov": cov,
            "se": np.sqrt(np.clip(np.diag(cov), 0, None)), "llf": llf, "n": n, "N": N, "T": T, "k": k,
            "aic": -2 * llf + 2 * (k + 2), "bic": -2 * llf + np.log(n) * (k + 2), "resid": e, "fitted": yhat,
            "pseudo_r2": float(np.corrcoef(yhat, yt)[0, 1] ** 2) if np.std(yhat) > 0 else 0.0,
            "W": W, "durbin": durbin, "fe": fe, "has_const": fe == "none" and add_const}


def sem_panel(y, X, W, N, fe="entity", add_const=True, standardize=True, durbin=False):
    """ML spatial error (SEM) panel: y = X b + u, u = lambda W u + e. durbin=True -> SDEM (X, W X)."""
    W = row_standardize(W) if standardize else np.asarray(W, dtype=float)
    yt, Zt = _prep(y, X, W, N, fe, durbin, add_const)
    n = len(yt)
    T = n // N
    w = _eig(W)
    Wy, WZ = spatial_lag(yt, W, N), spatial_lag(Zt, W, N)
    lo, hi = _bounds(w)

    def fit_l(lam):
        ys, Zs = yt - lam * Wy, Zt - lam * WZ
        b, e = _ols(ys, Zs)
        return b, e

    def conc(lam):
        _, e = fit_l(lam)
        return -(-n / 2 * np.log(e @ e / n) + T * _logdet(lam, w))

    lam = _opt.minimize_scalar(conc, bounds=(lo, hi), method="bounded", options={"xatol": 1e-8}).x
    beta, e = fit_l(lam)
    s2 = float(e @ e / n)
    k = Zt.shape[1]

    def ll(th):
        b, lm, sg = th[:k], th[k], th[k + 1]
        if sg <= 0 or not (lo < lm < hi):
            return -1e300
        ee = (yt - lm * Wy) - (Zt - lm * WZ) @ b
        return -n / 2 * np.log(2 * np.pi * sg) + T * _logdet(lm, w) - ee @ ee / (2 * sg)

    th = np.concatenate([beta, [lam, s2]])
    cov = _safe_inv(-_num_hess(ll, th))
    llf = float(ll(th))
    yhat = Zt @ beta
    return {"model": "sdem" if durbin else "sem", "lambda": float(lam), "rho": float(lam), "beta": beta, "sigma2": s2,
            "cov": cov, "se": np.sqrt(np.clip(np.diag(cov), 0, None)), "llf": llf, "n": n, "N": N, "T": T, "k": k,
            "aic": -2 * llf + 2 * (k + 2), "bic": -2 * llf + np.log(n) * (k + 2), "resid": yt - yhat, "fitted": yhat,
            "pseudo_r2": float(np.corrcoef(yhat, yt)[0, 1] ** 2) if np.std(yhat) > 0 else 0.0,
            "W": W, "durbin": durbin, "fe": fe, "has_const": fe == "none" and add_const}


def slx_panel(y, X, W, N, fe="entity", add_const=True, standardize=True, lagged=True):
    """SLX: OLS of y on [X, W X] (within transform), entity-clustered (cross-section) robust covariance.
    lagged=False -> plain pooled / FE OLS on X (model 'ols')."""
    W = row_standardize(W) if standardize else np.asarray(W, dtype=float)
    yt, Zt = _prep(y, X, W, N, fe, lagged, add_const)
    n, k = Zt.shape
    T = n // N
    b, e = _ols(yt, Zt)
    XtXi = _safe_inv(Zt.T @ Zt)
    ent = np.tile(np.arange(N), T)
    meat = np.zeros((k, k))
    for g in range(N):
        s = Zt[ent == g].T @ e[ent == g]
        meat += np.outer(s, s)
    dof = (N / max(N - 1, 1)) * ((n - 1) / max(n - k, 1))
    cov = dof * XtXi @ meat @ XtXi
    s2 = float(e @ e / n)
    llf = float(-n / 2 * (np.log(2 * np.pi * s2) + 1))
    yhat = Zt @ b
    return {"model": "slx" if lagged else "ols", "rho": 0.0, "beta": b, "sigma2": s2, "cov": np.pad(cov, ((0, 2), (0, 2))),
            "se": np.concatenate([np.sqrt(np.clip(np.diag(cov), 0, None)), [0.0, 0.0]]), "llf": llf, "n": n, "N": N,
            "T": T, "k": k, "aic": -2 * llf + 2 * (k + 1), "bic": -2 * llf + np.log(n) * (k + 1), "resid": e,
            "fitted": yhat, "pseudo_r2": float(1 - e @ e / max(((yt - yt.mean()) ** 2).sum(), 1e-300)),
            "W": W, "durbin": lagged, "fe": fe, "has_const": fe == "none" and add_const}


def _safe_inv(A):
    try:
        return np.linalg.inv(A)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(A)


def effects(fit, n_x, draws=500, seed=0, alpha=0.05):
    """LeSage-Pace average direct / indirect / total effects for the first n_x regressors (the X block).
    SAR: S_k = (I - rho W)^-1 beta_k; SDM: (I - rho W)^-1 (beta_k I + theta_k W); SLX: direct beta_k, indirect theta_k;
    SEM: direct beta_k, indirect 0. Inference by Monte-Carlo draws from N(theta_hat, cov).
    Returns list of dicts {x, direct, indirect, total, *_se, *_lo, *_hi, *_p}."""
    W = fit["W"]
    N = W.shape[0]
    model = fit["model"]
    c0 = 1 if fit.get("has_const") else 0
    k = fit["k"]
    th = np.concatenate([fit["beta"], [fit["rho"]]])
    cov = fit["cov"][: k + 1, : k + 1]
    I = np.eye(N)
    rng = np.random.default_rng(seed)

    def eff(theta):
        b, r = theta[:k], theta[k]
        out = np.zeros((n_x, 3))
        if model in ("sar", "sdm"):
            Ainv = np.linalg.inv(I - r * W)
            tr1, tot1 = np.trace(Ainv) / N, Ainv.sum() / N
            AW = Ainv @ W
            tr2, tot2 = np.trace(AW) / N, AW.sum() / N
        for j in range(n_x):
            bj = b[c0 + j]
            th_j = b[c0 + n_x + j] if fit["durbin"] else 0.0
            if model in ("sar", "sdm"):
                d, t = bj * tr1 + th_j * tr2, bj * tot1 + th_j * tot2
            elif model in ("slx", "sdem"):
                d, t = bj, bj + th_j * (W.sum() / N)
            else:
                d, t = bj, bj
            out[j] = (d, t - d, t)
        return out

    point = eff(th)
    sims = []
    if draws and np.all(np.isfinite(cov)):
        try:
            L = np.linalg.cholesky(cov + 1e-12 * np.eye(len(cov)))
        except np.linalg.LinAlgError:
            vals, vecs = np.linalg.eigh(cov)
            L = vecs @ np.diag(np.sqrt(np.clip(vals, 0, None)))
        lo_r, hi_r = _bounds(_eig(W))
        for _ in range(draws):
            d = th + L @ rng.standard_normal(len(th))
            d[k] = min(max(d[k], lo_r), hi_r)
            sims.append(eff(d))
    S = np.array(sims) if sims else None
    rows = []
    q = (alpha / 2, 1 - alpha / 2)
    for j in range(n_x):
        r = {"index": j}
        for m, name in enumerate(("direct", "indirect", "total")):
            r[name] = float(point[j, m])
            if S is not None:
                s = S[:, j, m]
                se = float(s.std(ddof=1))
                r[name + "_se"] = se
                r[name + "_lo"], r[name + "_hi"] = (float(v) for v in np.quantile(s, q))
                z = point[j, m] / se if se > 0 else np.nan
                r[name + "_p"] = float(2 * _st.norm.sf(abs(z))) if np.isfinite(z) else None
            else:
                r[name + "_se"] = r[name + "_lo"] = r[name + "_hi"] = r[name + "_p"] = None
        rows.append(r)
    return rows


def impact_matrix(fit, j=0, n_x=1):
    """N x N impact matrix of regressor j: dy / dx_j' = (I - rho W)^-1 (beta_j I + theta_j W) (SAR / SDM);
    beta_j I + theta_j W (SLX / SDEM); beta_j I (SEM / OLS). Column sums off the diagonal = spillover transmitted."""
    W = fit["W"]
    N = W.shape[0]
    c0 = 1 if fit.get("has_const") else 0
    b = fit["beta"][c0 + j]
    th = fit["beta"][c0 + n_x + j] if fit.get("durbin") and len(fit["beta"]) > c0 + n_x + j else 0.0
    M = b * np.eye(N) + th * W
    if fit["model"] in ("sar", "sdm"):
        return np.linalg.solve(np.eye(N) - fit["rho"] * W, M)
    return M


def lm_tests(y, X, W, N, fe="entity", add_const=True):
    """Anselin (1988) / Anselin et al. (1996) LM tests on pooled (within) OLS residuals, W_NT = I_T kron W.
    Returns {lm_lag, lm_error, rlm_lag, rlm_error} -> (stat, p)."""
    W = row_standardize(W)
    yt, Zt = _prep(y, X, W, N, fe, False, add_const)
    n = len(yt)
    T = n // N
    b, e = _ols(yt, Zt)
    s2 = e @ e / n
    Tw = T * float(np.trace(W.T @ W + W @ W))
    We, Wy = spatial_lag(e, W, N), spatial_lag(yt, W, N)
    WXb = spatial_lag(Zt @ b, W, N)
    _, mWXb = _ols(WXb, Zt)
    J = (mWXb @ mWXb + Tw * s2) / s2
    a_err = e @ We / s2
    a_lag = e @ Wy / s2
    lm_err = a_err ** 2 / Tw
    lm_lag = a_lag ** 2 / J
    rlm_lag = (a_lag - a_err) ** 2 / (J - Tw)
    rlm_err = (a_err - Tw / J * a_lag) ** 2 / (Tw * (1 - Tw / J))
    p = lambda s: float(_st.chi2.sf(s, 1))  # noqa: E731
    return {"lm_lag": (float(lm_lag), p(lm_lag)), "lm_error": (float(lm_err), p(lm_err)),
            "rlm_lag": (float(rlm_lag), p(rlm_lag)), "rlm_error": (float(rlm_err), p(rlm_err))}


# ------------------------------------------------------------------------------------------------ Diebold-Yilmaz


def var_ols(Y, p=1, ridge=0.0):
    """VAR(p) with intercept by (ridge) OLS. Y: T x k. Returns (A list p of k x k, c, Sigma, resid)."""
    Y = np.asarray(Y, dtype=float)
    T, k = Y.shape
    X = np.column_stack([np.ones(T - p)] + [Y[p - l - 1: T - l - 1] for l in range(p)])
    Yt = Y[p:]
    XtX = X.T @ X
    if ridge > 0:
        pen = ridge * np.trace(XtX) / XtX.shape[0] * np.eye(XtX.shape[0])
        pen[0, 0] = 0
        XtX = XtX + pen
    B = np.linalg.solve(XtX, X.T @ Yt)
    U = Yt - X @ B
    Sigma = U.T @ U / max(len(U) - X.shape[1], 1)
    A = [B[1 + l * k: 1 + (l + 1) * k].T for l in range(p)]
    return A, B[0], Sigma, U


def ma_coefs(A, H):
    k = A[0].shape[0]
    p = len(A)
    Psi = [np.eye(k)]
    for h in range(1, H):
        acc = np.zeros((k, k))
        for l in range(1, min(h, p) + 1):
            acc += A[l - 1] @ Psi[h - l]
        Psi.append(acc)
    return Psi


def gfevd(A, Sigma, H=10):
    """Generalized FEVD (Pesaran-Shin), rows normalized to 1. theta[i, j] = share of i's H-step error variance due to
    shocks in j."""
    Psi = ma_coefs(A, H)
    k = Sigma.shape[0]
    sd = np.sqrt(np.clip(np.diag(Sigma), 1e-300, None))
    num = np.zeros((k, k))
    den = np.zeros(k)
    for P in Psi:
        PS = P @ Sigma
        num += (PS / sd[None, :]) ** 2
        den += np.einsum("ij,jk,ik->i", P, Sigma, P)
    th = num / den[:, None]
    return th / th.sum(axis=1, keepdims=True)


def connectedness_table(theta):
    """-> dict(total, to (k), from (k), net (k), pairwise_net (k x k: j->i minus i->j), theta)."""
    k = theta.shape[0]
    off = theta - np.diag(np.diag(theta))
    frm = off.sum(axis=1) * 100
    to = off.sum(axis=0) * 100
    return {"total": float(off.sum() / k * 100), "to": to, "from": frm, "net": to - frm,
            "pairwise_net": (off.T - off) * 100, "theta": theta * 100}


def dy_connectedness(Y, p=1, H=10, ridge=0.0):
    A, _, Sigma, _ = var_ols(Y, p, ridge)
    return connectedness_table(gfevd(A, Sigma, H))


def rolling_connectedness(df, window=200, step=5, p=1, H=10, ridge=0.0, min_obs=None):
    """Rolling DY on a date-indexed DataFrame (columns = nodes, rows dropped if any NaN in the window).
    Returns list of (end_date, names, table)."""
    out = []
    vals = df.to_numpy(float)
    idx = list(df.index)
    names = list(df.columns)
    min_obs = min_obs or int(window * 0.8)
    for end in range(window, len(df) + 1, step):
        Y = vals[end - window: end]
        ok = np.isfinite(Y).all(axis=1)
        if ok.sum() < min_obs:
            continue
        out.append((idx[end - 1], names, dy_connectedness(Y[ok], p, H, ridge)))
    if out and idx[-1] != out[-1][0] and len(df) >= window:
        Y = vals[-window:]
        ok = np.isfinite(Y).all(axis=1)
        if ok.sum() >= min_obs:
            out.append((idx[-1], names, dy_connectedness(Y[ok], p, H, ridge)))
    return out


# ------------------------------------------------------------------------------------------------ local projections


def driscoll_kraay(X, e, t_index, L):
    """Driscoll-Kraay covariance for pooled OLS (X already within-transformed). t_index: integer period per row."""
    XtXi = _safe_inv(X.T @ X)
    ts = np.unique(t_index)
    hmat = np.vstack([X[t_index == t].T @ e[t_index == t] for t in ts])
    S = hmat.T @ hmat
    for l in range(1, L + 1):
        w = 1 - l / (L + 1)
        G = hmat[l:].T @ hmat[:-l]
        S += w * (G + G.T)
    return XtXi @ S @ XtXi


def local_projection(df, y, shocks, controls=(), H=10, y_lags=2, shock_lags=0, entity="entity_id", time="date",
                     cumulative=True, fe="entity", alpha=0.05):
    """Panel (or single-series) Jorda local projections.
    Response at h: cumulative -> sum_{s=0..h} y_{t+s}; else y_{t+h}. Regressors: shocks at t (+ lags), controls at t,
    y lags 1..y_lags. FE by within transform; Driscoll-Kraay SE (bandwidth h + 1).
    Returns DataFrame [h, shock, coef, se, lo, hi, p, n]."""
    d = df.sort_values([entity, time]).reset_index(drop=True) if entity in df.columns else df.sort_values(time).reset_index(drop=True)
    if entity not in d.columns:
        d = d.assign(**{entity: "_all"})
        fe = "none" if fe == "entity" else fe
    g = d.groupby(entity, sort=False)
    regs = list(shocks)
    for s in shocks:
        for l in range(1, shock_lags + 1):
            d[f"{s}_L{l}"] = g[s].shift(l)
            regs.append(f"{s}_L{l}")
    for l in range(1, y_lags + 1):
        d[f"_ylag{l}"] = g[y].shift(l)
        regs.append(f"_ylag{l}")
    regs += list(controls)
    tcodes = pd.factorize(d[time])[0]
    z = _st.norm.ppf(1 - alpha / 2)
    rows = []
    for h in range(H + 1):
        if cumulative:
            fut = sum(g[y].shift(-s) for s in range(h + 1))
        else:
            fut = g[y].shift(-h)
        dd = d.assign(_resp=fut.to_numpy())
        m = dd[["_resp"] + regs].notna().all(axis=1).to_numpy()
        sub = dd[m]
        if len(sub) < len(regs) + 10:
            continue
        Yv = sub["_resp"].to_numpy(float)
        Xv = sub[regs].to_numpy(float)
        if fe in ("entity", "twoway"):
            Yv = Yv - sub.groupby(entity)["_resp"].transform("mean").to_numpy()
            Xv = Xv - sub.groupby(entity)[regs].transform("mean").to_numpy()
        if fe in ("time", "twoway"):
            tmp = pd.DataFrame(np.column_stack([Yv, Xv]), index=sub.index)
            tm = tmp.groupby(sub[time].to_numpy()).transform("mean").to_numpy()
            Yv, Xv = Yv - tm[:, 0], Xv - tm[:, 1:]
        if fe == "none":
            Xv = np.column_stack([np.ones(len(Yv)), Xv])
        b, e = _ols(Yv, Xv)
        V = driscoll_kraay(Xv, e, tcodes[m], h + 1)
        off = 1 if fe == "none" else 0
        for j, s in enumerate(shocks):
            c, se = float(b[off + j]), float(np.sqrt(max(V[off + j, off + j], 0)))
            rows.append({"h": h, "shock": s, "coef": c, "se": se, "lo": c - z * se, "hi": c + z * se,
                         "p": float(2 * _st.norm.sf(abs(c / se))) if se > 0 else None, "n": int(len(Yv))})
    return pd.DataFrame(rows)


# ------------------------------------------------------------------------------------------------ spatial VAR


def spatial_var(Y, S, W, p=1, fe="entity"):
    """Pooled spatial VAR(p) for N units: y_it = a_i + sum_l (phi_l y_i,t-l + psi_l (W y)_i,t-l) + c s_it + d (W s)_it.
    Y, S: T x N arrays. Returns dict(params (phi.., psi.., c, d), se, cov, n) and the system companion for IRFs."""
    W = row_standardize(W)
    Y, S = np.asarray(Y, float), np.asarray(S, float)
    T, N = Y.shape
    WY, WS = Y @ W.T, S @ W.T
    cols = []
    for l in range(1, p + 1):
        cols += [Y[p - l: T - l].reshape(-1), WY[p - l: T - l].reshape(-1)]
    cols += [S[p:].reshape(-1), WS[p:].reshape(-1)]
    X = np.column_stack(cols)
    yv = Y[p:].reshape(-1)
    ok = np.isfinite(X).all(axis=1) & np.isfinite(yv)
    # balanced requirement for the within transform: fill NaN rows by dropping whole periods
    Tm = T - p
    okp = ok.reshape(Tm, N).all(axis=1)
    X3, y3 = X.reshape(Tm, N, -1)[okp], yv.reshape(Tm, N)[okp]
    n_t = X3.shape[0]
    Xf, yf = X3.reshape(n_t * N, -1), y3.reshape(-1)
    Xd, yd = demean_panel(Xf, N, fe), demean_panel(yf, N, fe)
    b, e = _ols(yd, Xd)
    tcodes = np.repeat(np.arange(n_t), N)
    V = driscoll_kraay(Xd, e, tcodes, 2)
    return {"params": b, "se": np.sqrt(np.clip(np.diag(V), 0, None)), "cov": V, "n": int(len(yd)), "p": p, "W": W,
            "N": N}


def spatial_var_irf(fit, H=10, unit=None):
    """IRF of all N units to a unit shock s_j = 1 at t=0 (unit j, or all units equally when unit is None).
    Returns H+1 x N array (response by unit)."""
    W, N, p, b = fit["W"], fit["N"], fit["p"], fit["params"]
    I = np.eye(N)
    A = [b[2 * (l - 1)] * I + b[2 * (l - 1) + 1] * W for l in range(1, p + 1)]
    B = b[2 * p] * I + b[2 * p + 1] * W
    s = np.zeros(N)
    if unit is None:
        s[:] = 1.0 / N
    else:
        s[unit] = 1.0
    resp = [B @ s]
    for h in range(1, H + 1):
        acc = np.zeros(N)
        for l in range(1, min(h, p) + 1):
            acc += A[l - 1] @ resp[h - l]
        resp.append(acc)
    return np.array(resp)


def spatial_var_irf_bands(fit, H=10, unit=None, draws=300, seed=0, agg="mean", alpha=0.05):
    """Simulation bands for the aggregate IRF (mean over units, or the shocked unit's own / others' response)."""
    rng = np.random.default_rng(seed)
    base = spatial_var_irf(fit, H, unit)

    def red(r):
        if agg == "own" and unit is not None:
            return r[:, unit]
        if agg == "others" and unit is not None:
            return np.delete(r, unit, axis=1).mean(axis=1)
        return r.mean(axis=1)

    sims = []
    try:
        L = np.linalg.cholesky(fit["cov"] + 1e-14 * np.eye(len(fit["params"])))
        for _ in range(draws):
            f2 = dict(fit, params=fit["params"] + L @ rng.standard_normal(len(fit["params"])))
            sims.append(red(spatial_var_irf(f2, H, unit)))
    except np.linalg.LinAlgError:
        pass
    pt = red(base)
    if sims:
        S = np.array(sims)
        lo, hi = np.quantile(S, alpha / 2, axis=0), np.quantile(S, 1 - alpha / 2, axis=0)
        se = S.std(axis=0, ddof=1)
    else:
        lo = hi = se = np.full_like(pt, np.nan)
    return pt, se, lo, hi


def moran_numpy(y, W):
    """Global Moran's I with the normality-assumption moments (fallback when esda is unavailable)."""
    W = row_standardize(W)
    z = np.asarray(y, float) - np.mean(y)
    n = len(z)
    S0 = W.sum()
    I = n / S0 * (z @ W @ z) / (z @ z)
    S1 = 0.5 * ((W + W.T) ** 2).sum()
    S2 = ((W.sum(0) + W.sum(1)) ** 2).sum()
    EI = -1 / (n - 1)
    VI = (n * n * S1 - n * S2 + 3 * S0 * S0) / ((n * n - 1) * S0 * S0) - EI ** 2
    zI = (I - EI) / np.sqrt(VI)
    with _sw.catch_warnings():
        _sw.simplefilter("ignore")
        return {"I": float(I), "EI": EI, "z": float(zI), "p": float(2 * _st.norm.sf(abs(zI)))}


def moran_perm(y, W, perms=499, seed=0):
    """Global Moran's I (row-standardized W) with a permutation p (two-sided pseudo p as in esda) and z_sim."""
    W = row_standardize(W)
    z = np.asarray(y, float) - np.mean(y)
    n = len(z)
    I = n / W.sum() * (z @ W @ z) / (z @ z)
    rng = np.random.default_rng(seed)
    Zp = z[rng.random((perms, n)).argsort(axis=1)]
    sims = n / W.sum() * np.einsum("pi,pi->p", Zp @ W.T, Zp) / (z @ z)
    above = (sims >= I).sum()
    larger = min(above, perms - above)
    p = (larger + 1.0) / (perms + 1.0)
    zs = (I - sims.mean()) / sims.std() if sims.std() > 0 else np.nan
    return {"I": float(I), "p_sim": float(p), "z_sim": float(zs), "EI_sim": float(sims.mean())}


def lisa_perm(y, W, perms=499, seed=0):
    """Local Moran (Anselin 1995) with conditional permutation pseudo p-values (esda's algorithm, numpy only).
    Returns dict Is, p_sim, q (1 HH, 2 LH, 3 LL, 4 HL), lag."""
    W = row_standardize(W)
    y = np.asarray(y, float)
    n = len(y)
    z = (y - y.mean()) / (y.std() if y.std() > 0 else 1.0)
    lag = W @ z
    m2 = (z @ z) / n
    Is = z * lag / m2
    rng = np.random.default_rng(seed)
    card = (W > 0).sum(axis=1)
    kmax = int(card.max()) if n else 0
    rids = rng.random((perms, n - 1)).argsort(axis=1)[:, :kmax] if kmax else np.zeros((perms, 0), int)
    p = np.ones(n)
    if kmax:
        Wk = np.zeros((n, kmax))
        for i in range(n):
            nb = np.nonzero(W[i])[0]
            Wk[i, : len(nb)] = W[i, nb]
        idx = np.broadcast_to(rids[None, :, :], (n, perms, kmax)).copy()
        idx += idx >= np.arange(n)[:, None, None]
        sim = z[:, None] * np.einsum("npk,nk->np", z[idx], Wk) / m2
        above = (sim >= Is[:, None]).sum(axis=1)
        larger = np.minimum(above, perms - above)
        p = np.where(card > 0, (larger + 1.0) / (perms + 1.0), 1.0)
    q = np.where(z > 0, np.where(lag > 0, 1, 4), np.where(lag > 0, 2, 3))
    return {"Is": Is, "p_sim": p, "q": q, "lag": lag}


def gi_star(x: np.ndarray, W: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Getis-Ord Gi* z (Ord & Getis 1995), weights incl. self (W raw + I); analytic normal two-sided p."""
    n = len(x)
    Ws = W + np.eye(n)
    xb = x.mean()
    S = np.sqrt(max((x ** 2).mean() - xb ** 2, 1e-300))
    wi = Ws.sum(1)
    num = Ws @ x - xb * wi
    den = S * np.sqrt(np.clip((n * (Ws ** 2).sum(1) - wi ** 2) / (n - 1), 1e-300, None))
    z = num / den
    return z, 2 * _st.norm.sf(np.abs(z))
