"""Studio v2 S2 controls & causal core: double / debiased ML (PLR, IRM), propensity scores (IPW, AIPW, nearest-neighbour
matching) with balance tables, regression discontinuity (sharp / fuzzy, Imbens-Kalyanaraman bandwidth, CCT-style robust
bias-corrected inference) and coefficient stability with Oster's delta (owner: model-studio).

Pure functions (numpy, pandas, scipy, scikit-learn, xgboost, lightgbm); embedded verbatim in exported scripts after the
app/modeling/_ml.py core, so the names imported below already exist there.
"""

from __future__ import annotations

import os
import warnings as _w
from typing import Any, Callable

import numpy as np
from scipy import stats

try:  # inside the app; in an exported script the _ml core is already defined above this module
    from ._ml import cv_splits, n_jobs  # noqa: F401
except ImportError:  # pragma: no cover
    pass

CAUSAL2_VERSION = "1.0"


def n_threads() -> int:
    """CPU threads for one Studio fit: the shared budget of _ml.n_jobs() (studio lane BIG360_STUDIO_NJOBS, else the
    job's OMP_NUM_THREADS, else cores - 2), capped at 4 for these small models."""
    try:
        nj = n_jobs()  # noqa: F821 - from _ml (import above, or the embedded core in an exported script)
    except NameError:  # pragma: no cover - older _ml without n_jobs
        nj = max(1, (os.cpu_count() or 2) - 2)
    return max(1, min(4, int(nj)))


# ------------------------------------------------------------------------------------------------ learners


def learner(kind: str, task: str, seed: int) -> Any:
    """Nuisance learner: lasso (LassoCV / L1-logit CV on standardized x), rf, xgb or lgbm; sklearn fit / predict API."""
    nt = n_threads()
    if kind == "lasso":
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        if task == "classification":
            from sklearn.linear_model import LogisticRegressionCV

            return make_pipeline(StandardScaler(), LogisticRegressionCV(Cs=8, l1_ratios=(1.0,), solver="saga", cv=3,
                                                                        max_iter=3000, scoring="neg_log_loss",
                                                                        use_legacy_attributes=False))
        from sklearn.linear_model import LassoCV

        return make_pipeline(StandardScaler(), LassoCV(cv=3, alphas=30, max_iter=5000))
    if kind == "ols":
        from sklearn.linear_model import LinearRegression, LogisticRegression

        return LogisticRegression(C=1e6, max_iter=2000) if task == "classification" else LinearRegression()
    if kind == "rf":
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

        cls = RandomForestClassifier if task == "classification" else RandomForestRegressor
        return cls(n_estimators=200, min_samples_leaf=20, max_features=0.5, n_jobs=nt, random_state=seed)
    if kind == "xgb":
        import xgboost as xgb

        cls = xgb.XGBClassifier if task == "classification" else xgb.XGBRegressor
        return cls(n_estimators=300, max_depth=4, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                   min_child_weight=20, n_jobs=nt, random_state=seed, verbosity=0)
    if kind == "lgbm":
        import lightgbm as lgb

        cls = lgb.LGBMClassifier if task == "classification" else lgb.LGBMRegressor
        return cls(n_estimators=300, learning_rate=0.05, num_leaves=15, min_child_samples=20, subsample=0.8,
                   subsample_freq=1, colsample_bytree=0.8, n_jobs=nt, random_state=seed, verbose=-1, deterministic=True,
                   force_row_wise=True)
    raise ValueError(f"unknown learner {kind!r}")


def learner_cost(kind: str, n: int, k: int) -> float:
    """Rough seconds for one nuisance fit (static prior for the run estimator)."""
    k = max(k, 1)
    return {"ols": 0.01 + n * k * 2e-8, "lasso": 0.1 + n * k * 3e-6, "rf": 0.2 + n * k * 2e-7,
            "xgb": 0.2 + n * k * 1e-7, "lgbm": 0.1 + n * k * 4e-8}.get(kind, 0.5 + n * k * 2e-7)


def _predict(m: Any, X: np.ndarray, task: str) -> np.ndarray:
    return m.predict_proba(X)[:, 1] if task == "classification" else m.predict(X)


def is_binary(v: np.ndarray) -> bool:
    u = np.unique(v[np.isfinite(v)])
    return len(u) == 2 and set(u.tolist()) <= {0.0, 1.0}


def crossfit(kind: str, task: str, X: np.ndarray, y: np.ndarray, folds: list, seed: int,
             subset: np.ndarray | None = None, check: Callable[[], None] | None = None) -> np.ndarray:
    """Out-of-fold predictions for every row; `subset` restricts the TRAINING rows (e.g. D == 1 for g1)."""
    out = np.full(len(y), np.nan)
    for tr, te in folds:
        if check:
            check()
        t = tr if subset is None else tr[subset[tr]]
        if len(t) < 10 or (task == "classification" and len(np.unique(y[t])) < 2):
            out[te] = np.mean(y[t]) if len(t) else np.nan
            continue
        with _w.catch_warnings():
            _w.simplefilter("ignore")
            m = learner(kind, task, seed).fit(X[t], y[t])
            out[te] = _predict(m, X[te], task)
    return out


def _se(psi: np.ndarray, J: float, groups: np.ndarray | None) -> float:
    """Sandwich SE of a moment estimator with score psi and Jacobian J (cluster-robust when groups are given)."""
    n = len(psi)
    if groups is not None:
        g = np.unique(groups, return_inverse=True)[1]
        s = np.bincount(g, weights=psi)
        G = len(s)
        return float(np.sqrt(np.sum(s ** 2) * G / max(G - 1, 1)) / (n * abs(J)))
    return float(np.sqrt(np.mean(psi ** 2) / n) / abs(J))


def r2(y: np.ndarray, p: np.ndarray) -> float:
    ok = np.isfinite(p)
    sst = np.sum((y[ok] - y[ok].mean()) ** 2)
    return float(1 - np.sum((y[ok] - p[ok]) ** 2) / sst) if sst > 0 else float("nan")


def auc(y: np.ndarray, p: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    ok = np.isfinite(p)
    return float(roc_auc_score(y[ok], p[ok])) if len(np.unique(y[ok])) == 2 else float("nan")


# ------------------------------------------------------------------------------------------------ DML


def dml(y: np.ndarray, d: np.ndarray, X: np.ndarray, fold_sets: list[list], model: str = "plr", learner_y: str = "lasso",
        learner_d: str = "lasso", estimand: str = "ate", trim: float = 0.01, groups: np.ndarray | None = None,
        seed: int = 42, check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Double / debiased ML (Chernozhukov et al. 2018). plr: Y = theta D + g(X) + e (Robinson residual-on-residual);
    irm: binary D, AIPW score for the ATE or ATT. `fold_sets`: one fold list per repetition (median aggregation)."""
    d_bin = is_binary(d)
    if model == "irm" and not d_bin:
        raise ValueError("the interactive regression model needs a binary (0/1) treatment")
    reps = []
    for r, folds in enumerate(fold_sets):
        s = seed + r
        if model == "plr":
            l_hat = crossfit(learner_y, "regression", X, y, folds, s, check=check)
            m_hat = crossfit(learner_d, "classification" if d_bin else "regression", X, d, folds, s, check=check)
            u, v = y - l_hat, d - m_hat
            theta = float(np.sum(v * u) / np.sum(v * v))
            psi = (u - theta * v) * v
            J = -float(np.mean(v * v))
            reps.append({"theta": theta, "se": _se(psi, J, groups), "l_hat": l_hat, "m_hat": m_hat, "psi": psi / -J,
                         "nuis": {"r2_y_given_x": r2(y, l_hat),
                                  ("auc_d_given_x" if d_bin else "r2_d_given_x"): auc(d, m_hat) if d_bin else r2(d, m_hat)}})
        else:
            D = d > 0.5
            m_hat = np.clip(crossfit(learner_d, "classification", X, d, folds, s, check=check), trim, 1 - trim)
            g0 = crossfit(learner_y, "regression", X, y, folds, s, subset=~D, check=check)
            g1 = crossfit(learner_y, "regression", X, y, folds, s, subset=D, check=check) if estimand == "ate" else None
            if estimand == "ate":
                sc = g1 - g0 + D * (y - g1) / m_hat - (~D) * (y - g0) / (1 - m_hat)
                theta = float(np.mean(sc))
                psi, J = sc - theta, -1.0
            else:
                pbar = float(np.mean(D))
                a = D * (y - g0) - m_hat * (~D) * (y - g0) / (1 - m_hat)
                theta = float(np.sum(a) / np.sum(D))
                psi, J = (a - D * theta) / pbar, -1.0
            reps.append({"theta": theta, "se": _se(psi, J, groups), "m_hat": m_hat, "g0": g0, "g1": g1, "psi": psi,
                         "nuis": {"auc_d_given_x": auc(d, m_hat), "r2_y_given_x_d0": r2(y[~D], g0[~D]),
                                  **({"r2_y_given_x_d1": r2(y[D], g1[D])} if g1 is not None else {})}})
    th = np.array([x["theta"] for x in reps])
    se = np.array([x["se"] for x in reps])
    theta = float(np.median(th))
    se_agg = float(np.sqrt(np.median(se ** 2 + (th - theta) ** 2))) if len(reps) > 1 else float(se[0])
    z = theta / se_agg if se_agg > 0 else float("nan")
    p = float(2 * stats.norm.sf(abs(z)))
    return {"theta": theta, "se": se_agg, "z": z, "p": p, "ci": (theta - 1.96 * se_agg, theta + 1.96 * se_agg),
            "reps": reps, "thetas": th.tolist(), "d_binary": d_bin, "model": model, "estimand": estimand}


def ols_hc1(y: np.ndarray, X: np.ndarray, groups: np.ndarray | None = None) -> dict[str, Any]:
    """OLS with a constant prepended; HC1 or cluster-robust covariance. Returns beta, se, r2."""
    A = np.column_stack([np.ones(len(y)), X])
    XtX_inv = np.linalg.pinv(A.T @ A)
    b = XtX_inv @ A.T @ y
    e = y - A @ b
    n, k = A.shape
    if groups is not None:
        g = np.unique(groups, return_inverse=True)[1]
        G = int(g.max()) + 1
        S = np.zeros((G, k))
        np.add.at(S, g, A * e[:, None])
        meat = S.T @ S * (G / max(G - 1, 1)) * ((n - 1) / max(n - k, 1))
    else:
        meat = (A * e[:, None] ** 2).T @ A * n / max(n - k, 1)
    V = XtX_inv @ meat @ XtX_inv
    sst = np.sum((y - y.mean()) ** 2)
    return {"beta": b, "se": np.sqrt(np.clip(np.diag(V), 0, None)), "r2": float(1 - e @ e / sst) if sst > 0 else float("nan"),
            "resid": e}


# ------------------------------------------------------------------------------------------------ propensity scores


def smd(x: np.ndarray, D: np.ndarray, w: np.ndarray | None = None) -> tuple[float, float, float, float]:
    """(mean treated, mean control, standardized mean difference, variance ratio) with optional weights; the SMD
    denominator is the unweighted pooled SD (Austin 2009) so raw and weighted SMDs are comparable."""
    w = np.ones(len(x)) if w is None else w
    t, c = D, ~D
    mt = np.average(x[t], weights=w[t]) if w[t].sum() > 0 else np.nan
    mc = np.average(x[c], weights=w[c]) if w[c].sum() > 0 else np.nan
    vt = np.average((x[t] - mt) ** 2, weights=w[t]) if w[t].sum() > 0 else np.nan
    vc = np.average((x[c] - mc) ** 2, weights=w[c]) if w[c].sum() > 0 else np.nan
    sd = np.sqrt((np.var(x[t]) + np.var(x[c])) / 2)
    return float(mt), float(mc), float((mt - mc) / sd) if sd > 0 else 0.0, float(vt / vc) if vc and vc > 0 else float("nan")


def ipw_weights(e: np.ndarray, D: np.ndarray, estimand: str) -> np.ndarray:
    if estimand == "att":
        return np.where(D, 1.0, e / (1 - e))
    return np.where(D, 1 / e, 1 / (1 - e))


def ipw(y: np.ndarray, D: np.ndarray, e: np.ndarray, estimand: str, groups: np.ndarray | None = None) -> dict[str, Any]:
    """Normalized (Hajek) IPW; SE from the estimator's influence function, treating e as known (usually conservative)."""
    w = ipw_weights(e, D, estimand)
    w1, w0 = np.where(D, w, 0.0), np.where(~D, w, 0.0)
    mu1, mu0 = np.sum(w1 * y) / np.sum(w1), np.sum(w0 * y) / np.sum(w0)
    psi = w1 * (y - mu1) / np.mean(w1) - w0 * (y - mu0) / np.mean(w0)
    tau = float(mu1 - mu0)
    return {"tau": tau, "se": _se(psi, 1.0, groups), "weights": w}


def aipw(y: np.ndarray, D: np.ndarray, e: np.ndarray, g0: np.ndarray, g1: np.ndarray | None, estimand: str,
         groups: np.ndarray | None = None) -> dict[str, Any]:
    """Augmented IPW (doubly robust) with cross-fitted nuisances; ATE or ATT score."""
    if estimand == "ate":
        sc = g1 - g0 + D * (y - g1) / e - (~D) * (y - g0) / (1 - e)
        tau = float(np.mean(sc))
        return {"tau": tau, "se": _se(sc - tau, 1.0, groups)}
    a = D * (y - g0) - e * (~D) * (y - g0) / (1 - e)
    tau = float(np.sum(a) / np.sum(D))
    return {"tau": tau, "se": _se((a - D * tau) / np.mean(D), 1.0, groups)}


def nn_match(y: np.ndarray, D: np.ndarray, e: np.ndarray, k: int = 1, caliper: float = 0.2) -> dict[str, Any]:
    """ATT by k-nearest-neighbour matching on the logit propensity score, with replacement, caliper in SDs of the
    logit. Variance: Abadie-Imbens (2006) form with a homoskedastic conditional variance of control outcomes (it ignores
    the estimation of e). Returns match counts K_j per control for the balance table."""
    lg = np.log(e / (1 - e))
    cal = caliper * float(np.std(lg))
    ti, ci = np.where(D)[0], np.where(~D)[0]
    order = np.argsort(lg[ci])
    cs = lg[ci][order]
    K = np.zeros(len(y))
    taus, used = [], []
    for i in ti:
        pos = np.searchsorted(cs, lg[i])
        lo, hi = max(pos - k, 0), min(pos + k, len(cs))
        cand = np.arange(lo, hi)
        cand = cand[np.argsort(np.abs(cs[cand] - lg[i]), kind="stable")][:k]
        cand = cand[np.abs(cs[cand] - lg[i]) <= cal] if cal > 0 else cand
        if not len(cand):
            continue
        js = ci[order[cand]]
        K[js] += 1.0 / len(js)
        taus.append(y[i] - y[js].mean())
        used.append(i)
    taus = np.asarray(taus)
    n1 = len(taus)
    if n1 < 2:
        return {"tau": float("nan"), "se": float("nan"), "n_matched": n1, "n_treated": len(ti), "K": K, "used": np.array(used, int)}
    tau = float(taus.mean())
    mc = K > 0
    s2 = float(np.var(y[mc], ddof=1)) if mc.sum() > 1 else 0.0
    # K_j = (times control j is used) / k; the second term is the extra variance from controls re-used across matches
    var = (np.sum((taus - tau) ** 2) + np.sum(K[mc] * (K[mc] - 1.0 / k)) * s2) / n1 ** 2
    return {"tau": tau, "se": float(np.sqrt(max(var, 0.0))), "n_matched": n1, "n_treated": len(ti), "K": K,
            "used": np.array(used, int)}


# ------------------------------------------------------------------------------------------------ regression discontinuity


KERNEL_CK = {"triangular": 3.4375, "epanechnikov": 3.1999, "uniform": 5.4}


def kernel_w(u: np.ndarray, kernel: str) -> np.ndarray:
    a = np.abs(u)
    if kernel == "uniform":
        return np.where(a <= 1, 0.5, 0.0)
    if kernel == "epanechnikov":
        return np.where(a <= 1, 0.75 * (1 - a ** 2), 0.0)
    return np.where(a <= 1, 1 - a, 0.0)


def ik_bandwidth(xc: np.ndarray, y: np.ndarray, kernel: str = "triangular") -> dict[str, float]:
    """Imbens & Kalyanaraman (2012) MSE-optimal bandwidth for local-linear RD (cutoff at 0)."""
    N = len(xc)
    left, right = xc < 0, xc >= 0
    h1 = 1.84 * float(np.std(xc, ddof=1)) * N ** (-1 / 5)
    wl, wr = left & (xc >= -h1), right & (xc <= h1)
    nl, nr = int(wl.sum()), int(wr.sum())
    if nl < 3 or nr < 3:
        raise ValueError("too few observations near the cutoff for the IK bandwidth")
    f = (nl + nr) / (2 * N * h1)
    s2l, s2r = float(np.var(y[wl], ddof=1)), float(np.var(y[wr], ddof=1))
    ml, mr = float(np.median(xc[left])), float(np.median(xc[right]))
    sel = (xc >= ml) & (xc <= mr)
    A = np.column_stack([np.ones(sel.sum()), (xc[sel] >= 0).astype(float), xc[sel], xc[sel] ** 2, xc[sel] ** 3])
    m3 = 6 * float(np.linalg.lstsq(A, y[sel], rcond=None)[0][4])
    m3 = m3 if abs(m3) > 1e-12 else 1e-12
    Nl, Nr = int(left.sum()), int(right.sum())
    h2l = 3.56 * (s2l / (f * m3 ** 2)) ** (1 / 7) * Nl ** (-1 / 7)
    h2r = 3.56 * (s2r / (f * m3 ** 2)) ** (1 / 7) * Nr ** (-1 / 7)

    def curv(mask: np.ndarray) -> tuple[float, int]:
        if mask.sum() < 4:
            return 0.0, int(mask.sum())
        B = np.column_stack([np.ones(mask.sum()), xc[mask], xc[mask] ** 2])
        return 2 * float(np.linalg.lstsq(B, y[mask], rcond=None)[0][2]), int(mask.sum())

    m2r, n2r = curv(right & (xc <= h2r))
    m2l, n2l = curv(left & (xc >= -h2l))
    rr = 2160 * s2r / (max(n2r, 1) * h2r ** 4)
    rl = 2160 * s2l / (max(n2l, 1) * h2l ** 4)
    h = KERNEL_CK.get(kernel, 3.4375) * ((s2l + s2r) / (f * ((m2r - m2l) ** 2 + rr + rl))) ** (1 / 5) * N ** (-1 / 5)
    return {"h": float(h), "h_pilot": h1, "f0": f, "sigma2_left": s2l, "sigma2_right": s2r, "m2_left": m2l,
            "m2_right": m2r, "m3": m3, "reg_left": rl, "reg_right": rr}


def _wls_sandwich(A: np.ndarray, w: np.ndarray, ys: list[np.ndarray], groups: np.ndarray | None):
    """Weighted LS for several outcomes on one design; returns coefs and the per-observation influence rows."""
    Aw = A * w[:, None]
    B = np.linalg.pinv(A.T @ Aw)
    out = []
    for yy in ys:
        b = B @ (Aw.T @ yy)
        e = yy - A @ b
        infl = (Aw * e[:, None]) @ B.T  # row i: contribution of observation i to b - beta
        if groups is not None:
            g = np.unique(groups, return_inverse=True)[1]
            S = np.zeros((int(g.max()) + 1, A.shape[1]))
            np.add.at(S, g, infl)
            G = S.shape[0]
            infl = S * np.sqrt(G / max(G - 1, 1))
        else:
            infl = infl * np.sqrt(len(yy) / max(len(yy) - A.shape[1], 1))
        out.append((b, infl))
    return out


def rd_estimate(xc: np.ndarray, y: np.ndarray, h: float, kernel: str = "triangular", p: int = 1,
                d: np.ndarray | None = None, groups: np.ndarray | None = None) -> dict[str, Any]:
    """Local-polynomial RD at cutoff 0 with bandwidth h (separate slopes each side). Sharp: jump in y. Fuzzy (d given):
    Wald ratio jump_y / jump_d with a delta-method SE from the joint influence functions."""
    w = kernel_w(xc / h, kernel)
    s = w > 0
    xs, ys, ws = xc[s], y[s], w[s]
    T = (xs >= 0).astype(float)
    cols = [np.ones(len(xs)), T]
    for k in range(1, p + 1):
        cols += [xs ** k, T * xs ** k]
    A = np.column_stack(cols)
    nl, nr = int((xs < 0).sum()), int((xs >= 0).sum())
    if nl < p + 2 or nr < p + 2:
        raise ValueError(f"too few observations within the bandwidth (left {nl}, right {nr})")
    g = groups[s] if groups is not None else None
    res = _wls_sandwich(A, ws, [ys] + ([d[s]] if d is not None else []), g)
    by, iy = res[0]
    jy, vy = float(by[1]), float(np.sum(iy[:, 1] ** 2))
    out = {"n_left": nl, "n_right": nr, "h": h, "p": p, "jump_y": jy, "se_y": float(np.sqrt(vy)), "coef": by}
    if d is None:
        out.update(tau=jy, se=float(np.sqrt(vy)))
        return out
    bd, idd = res[1]
    jd, vd = float(bd[1]), float(np.sum(idd[:, 1] ** 2))
    cov = float(np.sum(iy[:, 1] * idd[:, 1]))
    tau = jy / jd if abs(jd) > 1e-12 else float("nan")
    var = (vy - 2 * tau * cov + tau ** 2 * vd) / jd ** 2 if abs(jd) > 1e-12 else float("nan")
    out.update(tau=tau, se=float(np.sqrt(max(var, 0))), jump_d=jd, se_d=float(np.sqrt(vd)), coef_d=bd,
               first_stage_f=float((jd / np.sqrt(vd)) ** 2) if vd > 0 else float("nan"))
    return out


def mccrary(xc: np.ndarray, h: float) -> dict[str, float]:
    """McCrary (2008) density discontinuity test at 0: histogram (bin width 2 sd N^-1/2), triangular local-linear fit of
    bin densities on each side within h; theta = log f+ - log f-."""
    N = len(xc)
    b = 2 * float(np.std(xc, ddof=1)) * N ** -0.5
    if b <= 0 or h <= b:
        return {"theta": float("nan"), "se": float("nan"), "z": float("nan"), "p": float("nan")}

    def side(sign: int) -> float:
        v = xc[(xc >= 0) if sign > 0 else (xc < 0)]
        edges = np.arange(0, (np.abs(v).max() if len(v) else 0) + b, b)
        if len(edges) < 3:
            return float("nan")
        cnt, _ = np.histogram(np.abs(v), edges)
        mid = (edges[:-1] + edges[1:]) / 2
        dens = cnt / (N * b)
        w = kernel_w(mid / h, "triangular")
        k = w > 0
        if k.sum() < 3:
            return float("nan")
        A = np.column_stack([np.ones(k.sum()), mid[k]])
        Aw = A * w[k][:, None]
        return float(np.linalg.lstsq(Aw.T @ A, Aw.T @ dens[k], rcond=None)[0][0])

    fr, fl = side(1), side(-1)
    if not (fr > 0 and fl > 0):
        return {"theta": float("nan"), "se": float("nan"), "z": float("nan"), "p": float("nan")}
    th = float(np.log(fr) - np.log(fl))
    se = float(np.sqrt((1 / (N * h)) * (24 / 5) * (1 / fr + 1 / fl)))
    z = th / se
    return {"theta": th, "se": se, "z": z, "p": float(2 * stats.norm.sf(abs(z))), "f_left": fl, "f_right": fr}


def rd_bins(xc: np.ndarray, y: np.ndarray, n_bins: int = 20) -> tuple[list, list, list, list]:
    """Evenly spaced bin means on each side of the cutoff (plot data)."""
    out = []
    for m in (xc < 0, xc >= 0):
        v, yy = xc[m], y[m]
        if not len(v):
            out.append(([], []))
            continue
        edges = np.linspace(v.min(), v.max() + 1e-12, n_bins + 1)
        idx = np.clip(np.digitize(v, edges) - 1, 0, n_bins - 1)
        xs, ms = [], []
        for b in range(n_bins):
            k = idx == b
            if k.any():
                xs.append(float(v[k].mean()))
                ms.append(float(yy[k].mean()))
        out.append((xs, ms))
    return out[0][0], out[0][1], out[1][0], out[1][1]


# ------------------------------------------------------------------------------------------------ coefficient stability


def oster(beta_short: float, r2_short: float, beta_full: float, r2_full: float, r_max: float,
          delta: float = 1.0) -> dict[str, float]:
    """Oster (2019) bias-adjusted coefficient (approximation of Prop. 3 / eq. 3) and the delta that would make the
    effect zero; plus the Altonji-Elder-Taber ratio beta_full / (beta_short - beta_full)."""
    db, dr = beta_short - beta_full, r2_full - r2_short
    if abs(dr) < 1e-12 or abs(db) < 1e-15:
        return {"beta_star": beta_full, "delta_for_zero": float("inf") if abs(beta_full) > 0 else 0.0,
                "aet_ratio": float("inf") if abs(db) < 1e-15 else float("nan"), "r_max": r_max}
    beta_star = beta_full - delta * db * (r_max - r2_full) / dr
    d0 = beta_full * dr / (db * (r_max - r2_full)) if r_max > r2_full else float("inf")
    return {"beta_star": float(beta_star), "delta_for_zero": float(d0), "aet_ratio": float(beta_full / db), "r_max": r_max}


def coef_stability(y: np.ndarray, d: np.ndarray, C: np.ndarray, groups: np.ndarray | None = None) -> list[dict[str, Any]]:
    """Coefficient of d (with SE, R2) as the controls C[:, :k] are added one at a time (k = 0..K)."""
    rows = []
    for k in range(C.shape[1] + 1):
        r = ols_hc1(y, np.column_stack([d] + ([C[:, :k]] if k else [])), groups)
        rows.append({"k": k, "beta": float(r["beta"][1]), "se": float(r["se"][1]), "r2": r["r2"]})
    return rows
