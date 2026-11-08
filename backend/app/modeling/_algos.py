"""Model Studio Phase B estimation core (owner: model-studio).

Pure functions on pandas / numpy inputs (deps: numpy, pandas, scipy, statsmodels, linearmodels, arch). The method
wrappers in app/modeling/methods/* format their outputs; app.modeling.export embeds this file verbatim so exported
scripts call exactly the same estimation code. No imports from `app`.
"""

from __future__ import annotations

import warnings as _w
from typing import Any

import numpy as np
import pandas as pd
from scipy import optimize, stats

ALGOS_VERSION = "1.0"


def _ols(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ b
    return b, e, float(1 - (e @ e) / max(((y - y.mean()) ** 2).sum(), 1e-300))


def nw_mean(x: np.ndarray, lags: int) -> tuple[float, float]:
    """Mean and Newey-West standard error of a series."""
    x = np.asarray(x, float)
    n = len(x)
    u = x - x.mean()
    s = (u @ u) / n
    for L in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - L / (lags + 1)) * (u[L:] @ u[:-L]) / n
    return float(x.mean()), float(np.sqrt(max(s, 0) / n))


# ------------------------------------------------------------------------------------------------ panel


def panel_frame(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    return d.set_index(["entity_id", "date"])[cols].sort_index()


def panel_fit(df: pd.DataFrame, y: str, x: list[str], estimator: str = "fe", entity_effects: bool = True,
              time_effects: bool = False, cov_type: str = "clustered", cluster_entity: bool = True,
              cluster_time: bool = False, kernel_bandwidth: int | None = None) -> Any:
    from linearmodels.panel import BetweenOLS, FirstDifferenceOLS, PanelOLS, PooledOLS, RandomEffects

    d = panel_frame(df, [y] + x)
    exog = d[x].assign(const=1.0) if estimator in ("re", "pooled", "between") else d[x]
    kw: dict[str, Any] = {}
    if cov_type == "clustered":
        kw = {"cluster_entity": cluster_entity, "cluster_time": cluster_time}
    elif cov_type == "kernel" and kernel_bandwidth:
        kw = {"bandwidth": kernel_bandwidth}
    if estimator == "fe":
        m = PanelOLS(d[y], exog, entity_effects=entity_effects, time_effects=time_effects, drop_absorbed=True)
    elif estimator == "re":
        m = RandomEffects(d[y], exog)
    elif estimator == "between":
        m = BetweenOLS(d[y], exog)
        cov_type = "robust" if cov_type in ("clustered", "kernel") else cov_type
        kw = {}
    elif estimator == "first_diff":
        m = FirstDifferenceOLS(d[y], exog)
    else:
        m = PooledOLS(d[y], exog)
    return m.fit(cov_type=cov_type, **kw)


def hausman(fe: Any, re: Any) -> tuple[float, int, float]:
    common = [c for c in fe.params.index if c in re.params.index and c != "const"]
    b = (fe.params[common] - re.params[common]).to_numpy()
    V = (fe.cov.loc[common, common] - re.cov.loc[common, common]).to_numpy()
    stat = float(b @ np.linalg.pinv(V) @ b)
    k = len(common)
    return stat, k, float(stats.chi2.sf(abs(stat), k))


def fama_macbeth(df: pd.DataFrame, y: str, x: list[str], nw_lags: int | None = None,
                 min_obs: int | None = None) -> dict[str, Any]:
    """Cross-sectional OLS per date -> lambda series; means with Newey-West SEs; average R2."""
    k = len(x) + 1
    min_obs = min_obs or k + 5
    lam, r2, nobs, dates = [], [], [], []
    for d, g in df.groupby("date", sort=True):
        g = g[[y] + x].dropna()
        if len(g) < min_obs:
            continue
        X = np.column_stack([np.ones(len(g)), g[x].to_numpy(float)])
        if np.linalg.matrix_rank(X) < k:
            continue
        b, _, rr = _ols(g[y].to_numpy(float), X)
        lam.append(b)
        r2.append(rr)
        nobs.append(len(g))
        dates.append(d)
    if len(lam) < 3:
        raise ValueError("fewer than 3 cross-sections with enough observations")
    L = pd.DataFrame(lam, index=pd.Index(dates, name="date"), columns=["const"] + x)
    T = len(L)
    lags = int(nw_lags if nw_lags is not None else np.floor(4 * (T / 100) ** (2 / 9)))
    rows = []
    for c in L.columns:
        m, se = nw_mean(L[c].to_numpy(), lags)
        t = m / se if se > 0 else np.nan
        rows.append({"term": c, "estimate": m, "std_error": se, "statistic": t,
                     "p_value": float(2 * stats.t.sf(abs(t), T - 1)) if np.isfinite(t) else np.nan,
                     "ci_low": m - stats.t.ppf(0.975, T - 1) * se, "ci_high": m + stats.t.ppf(0.975, T - 1) * se,
                     "share_positive": float((L[c] > 0).mean())})
    return {"lambdas": L, "r2": pd.Series(r2, index=L.index), "n": pd.Series(nobs, index=L.index),
            "table": pd.DataFrame(rows), "nw_lags": lags, "periods": T}


def iv_fit(df: pd.DataFrame, y: str, exog: list[str], endog: list[str], instruments: list[str],
           estimator: str = "2sls", cov_type: str = "robust", clusters: np.ndarray | None = None) -> Any:
    from linearmodels.iv import IV2SLS, IVGMM, IVLIML

    d = df[[y] + exog + endog + instruments].astype(float)
    ex = d[exog].assign(const=1.0)
    cls = {"2sls": IV2SLS, "liml": IVLIML, "gmm": IVGMM}[estimator]
    m = cls(d[y], ex, d[endog], d[instruments])
    if cov_type == "clustered":
        return m.fit(cov_type="clustered", clusters=pd.Series(clusters, index=d.index))
    return m.fit(cov_type=cov_type)


# ------------------------------------------------------------------------------------------------ causal


def period_index(dates: pd.Series) -> tuple[dict[Any, int], list[Any]]:
    u = sorted(pd.unique(dates))
    return {d: i for i, d in enumerate(u)}, u


def treat_period(df: pd.DataFrame, treat_dates: dict[str, Any]) -> pd.Series:
    """entity -> first treated period index (NaN = never treated), using the panel's period grid."""
    _, grid = period_index(df["date"])
    gts = [pd.Timestamp(g) for g in grid]
    out = {}
    for e in df["entity_id"].unique():
        d = treat_dates.get(e)
        if d is None or (isinstance(d, float) and np.isnan(d)):
            out[e] = np.nan
            continue
        later = [i for i, g in enumerate(gts) if g >= pd.Timestamp(d)]
        out[e] = float(later[0]) if later else np.nan
    return pd.Series(out)


def match_entities(df: pd.DataFrame, names: list[str]) -> set[str]:
    """entity_ids whose id or label matches one of names (case-insensitive)."""
    want = {str(n).strip().upper() for n in names}
    lab = df["entity_label"] if "entity_label" in df.columns else df["entity_id"]
    m = pd.DataFrame({"entity_id": df["entity_id"].to_numpy(), "entity_label": lab.to_numpy()}).drop_duplicates("entity_id")
    return set(m.loc[m["entity_id"].astype(str).str.upper().isin(want) |
                     m["entity_label"].astype(str).str.upper().isin(want), "entity_id"])


def resolve_treatment_dates(df: pd.DataFrame, mode: str, entities: list[str] | None = None,
                            attribute: str | None = None, values: list[str] | None = None, column: str | None = None,
                            date: Any = None, events: list[tuple[str, Any]] | None = None) -> dict[str, Any]:
    """entity_id -> adoption date (absent = never treated). Raises ValueError on an unusable definition."""
    if mode == "events":
        out: dict[str, Any] = {}
        for ent, d in events or []:
            for e in match_entities(df, [ent]):
                out[e] = min(out.get(e, d), d)
        return out
    if mode == "column":
        if not column or column not in df.columns:
            raise ValueError("treatment.mode=column needs treatment.column (a dataset column)")
        return df[df[column] > 0.5].groupby("entity_id")["date"].min().to_dict()
    if date is None:
        raise ValueError("treatment.date is required for mode entities / attribute")
    if mode == "entities":
        ents = match_entities(df, entities or [])
    else:
        if not attribute or attribute not in df.columns:
            raise ValueError("treatment.attribute must be sector_id, region or country (security-level panel)")
        ents = set(df.loc[df[attribute].isin(values or []), "entity_id"])
    if not ents:
        raise ValueError("the treatment definition matches no entity in the dataset")
    return {e: date for e in ents}


def did_2x2(df: pd.DataFrame, y: str, treated: pd.Series, event_date: Any) -> dict[str, Any]:
    import statsmodels.api as sm

    d = df[["entity_id", "date", y]].dropna().copy()
    d["treated"] = d["entity_id"].map(treated).fillna(0).astype(float)
    d["post"] = (pd.to_datetime(d["date"]) >= pd.Timestamp(event_date)).astype(float)
    d["did"] = d["treated"] * d["post"]
    X = sm.add_constant(d[["treated", "post", "did"]])
    r = sm.OLS(d[y].astype(float), X).fit(cov_type="cluster", cov_kwds={"groups": pd.factorize(d["entity_id"])[0]})
    means = d.groupby(["treated", "post"])[y].mean().unstack()
    return {"res": r, "means": means, "n": len(d)}


def twfe(df: pd.DataFrame, y: str, D: pd.Series, controls: list[str]) -> Any:
    from linearmodels.panel import PanelOLS

    d = df.copy()
    d["D"] = D.to_numpy(float)
    p = panel_frame(d, [y, "D"] + controls).dropna()
    return PanelOLS(p[y], p[["D"] + controls], entity_effects=True, time_effects=True,
                    drop_absorbed=True).fit(cov_type="clustered", cluster_entity=True)


def event_study(df: pd.DataFrame, y: str, tp: pd.Series, leads: int, lags: int, controls: list[str]) -> dict[str, Any]:
    """TWFE event study: relative-period dummies (endpoints binned), reference -1, entity + time FE, clustered SEs."""
    from linearmodels.panel import PanelOLS

    pos, _ = period_index(df["date"])
    d = df.copy()
    t = d["date"].map(pos).astype(float)
    g = d["entity_id"].map(tp).astype(float)
    rel = (t - g).clip(-leads, lags)
    ks = [k for k in range(-leads, lags + 1) if k != -1]
    names = []
    for k in ks:
        nm = f"rel_m{-k}" if k < 0 else f"rel_p{k}"
        d[nm] = ((rel == k) & g.notna()).astype(float)
        names.append(nm)
    p = panel_frame(d, [y] + names + controls).dropna()
    keep = [c for c in names if p[c].abs().sum() > 0]
    r = PanelOLS(p[y], p[keep + controls], entity_effects=True, time_effects=True,
                 drop_absorbed=True).fit(cov_type="clustered", cluster_entity=True)
    path = []
    for k, nm in zip(ks, names, strict=True):
        if nm in r.params.index:
            ci = r.conf_int().loc[nm]
            path.append((k, float(r.params[nm]), float(r.std_errors[nm]), float(ci.iloc[0]), float(ci.iloc[1])))
    path.append((-1, 0.0, 0.0, 0.0, 0.0))
    path.sort()
    pre = [nm for k, nm in zip(ks, names, strict=True) if k <= -2 and nm in r.params.index]
    pre_test = None
    if pre:
        b = r.params[pre].to_numpy()
        V = r.cov.loc[pre, pre].to_numpy()
        stat = float(b @ np.linalg.pinv(V) @ b)
        pre_test = (stat, len(pre), float(stats.chi2.sf(stat, len(pre))))
    post = [nm for k, nm in zip(ks, names, strict=True) if k >= 0 and nm in r.params.index]
    att = att_se = None
    if post:
        w = np.full(len(post), 1.0 / len(post))
        att = float(w @ r.params[post].to_numpy())
        att_se = float(np.sqrt(w @ r.cov.loc[post, post].to_numpy() @ w))
    return {"res": r, "path": path, "pre_test": pre_test, "att": att, "att_se": att_se}


def callaway_santanna(df: pd.DataFrame, y: str, tp: pd.Series, control: str = "never", B: int = 199,
                      seed: int = 42) -> dict[str, Any]:
    """Group-time ATT(g,t) (Callaway & Sant'Anna 2021, unconditional parallel trends, outcome differences),
    aggregated overall and by event time; multiplier-free entity bootstrap for SEs."""
    pos, grid = period_index(df["date"])
    W = df.assign(t=df["date"].map(pos)).pivot_table(index="entity_id", columns="t", values=y, aggfunc="first")
    ents = W.index.to_numpy()
    Y = W.to_numpy(float)
    G = tp.reindex(ents).to_numpy(float)
    T = Y.shape[1]
    groups = sorted({int(g) for g in G[~np.isnan(G)] if 1 <= g < T})
    if not groups:
        raise ValueError("no treated cohort with a pre-period inside the panel")
    rng = np.random.default_rng(seed)
    N = len(ents)
    Wb = np.vstack([np.ones(N), rng.multinomial(N, np.ones(N) / N, size=B)]).astype(float)  # row 0 = point estimate
    cells = []
    for g in groups:
        tr = G == g
        for t in range(T):
            if t == g - 1:
                continue
            base = g - 1 if t >= g else t - 1
            if base < 0:
                continue
            if control == "never":
                ct = np.isnan(G)
            else:
                ct = np.isnan(G) | (G > max(t, base))
                ct &= ~tr
            dlt = Y[:, t] - Y[:, base]
            ok = ~np.isnan(dlt)
            a, c = tr & ok, ct & ok
            if a.sum() < 1 or c.sum() < 2:
                continue
            dz = np.nan_to_num(dlt)
            att = (Wb[:, a] @ dz[a]) / np.maximum(Wb[:, a].sum(1), 1e-12) - (Wb[:, c] @ dz[c]) / np.maximum(Wb[:, c].sum(1), 1e-12)
            cells.append((g, t, t - g, int(a.sum()), att))
    if not cells:
        raise ValueError("no estimable group-time cell (check controls / overlap)")
    gt = pd.DataFrame([(grid[g], grid[t], e, n, a[0], np.std(a[1:], ddof=1)) for g, t, e, n, a in cells],
                      columns=["group", "time", "event_time", "n_treated", "att", "se"])
    post = [(n, a) for g, t, e, n, a in cells if e >= 0]
    wts = np.array([n for n, _ in post], float)
    overall = (np.vstack([a for _, a in post]).T @ wts) / wts.sum()
    dyn = {}
    for g, t, e, n, a in cells:
        dyn.setdefault(e, []).append((n, a))
    dyn_rows = []
    for e in sorted(dyn):
        w = np.array([n for n, _ in dyn[e]], float)
        v = (np.vstack([a for _, a in dyn[e]]).T @ w) / w.sum()
        dyn_rows.append((e, v[0], np.std(v[1:], ddof=1)))
    return {"group_time": gt, "overall": (float(overall[0]), float(np.std(overall[1:], ddof=1))),
            "dynamic": pd.DataFrame(dyn_rows, columns=["event_time", "att", "se"]), "n_groups": len(groups),
            "n_never": int(np.isnan(G).sum()), "bootstrap": B}


def synth_weights(y1: np.ndarray, Y0: np.ndarray) -> np.ndarray:
    """Donor weights w >= 0, sum 1, minimizing ||y1 - Y0 w||^2 over the pre-period."""
    J = Y0.shape[1]
    f = lambda w: float(((y1 - Y0 @ w) ** 2).sum())  # noqa: E731
    g = lambda w: -2 * Y0.T @ (y1 - Y0 @ w)  # noqa: E731
    r = optimize.minimize(f, np.full(J, 1 / J), jac=g, method="SLSQP", bounds=[(0, 1)] * J,
                          constraints=[{"type": "eq", "fun": lambda w: w.sum() - 1}], options={"maxiter": 500})
    w = np.clip(r.x, 0, None)
    return w / w.sum()


def synthetic_control(Y: pd.DataFrame, treated: str, t0: int, placebo: bool = True) -> dict[str, Any]:
    """Y: periods x units (complete). t0: index of the first treated period."""
    def one(unit: str, donors: list[str]) -> tuple[np.ndarray, np.ndarray, float, float]:
        y1 = Y[unit].to_numpy(float)
        Y0 = Y[donors].to_numpy(float)
        w = synth_weights(y1[:t0], Y0[:t0])
        synth = Y0 @ w
        gap = y1 - synth
        pre = float(np.sqrt(np.mean(gap[:t0] ** 2)))
        post = float(np.sqrt(np.mean(gap[t0:] ** 2))) if t0 < len(gap) else np.nan
        return w, gap, pre, post

    donors = [c for c in Y.columns if c != treated]
    w, gap, pre, post = one(treated, donors)
    out = {"weights": pd.Series(w, index=donors), "synthetic": Y[donors].to_numpy(float) @ w, "gap": gap,
           "pre_rmspe": pre, "post_rmspe": post, "ratio": post / pre if pre > 0 else np.nan, "placebos": {}}
    if placebo:
        for d in donors:
            others = [c for c in donors if c != d]
            if len(others) < 2:
                continue
            _, pg, ppre, ppost = one(d, others)
            out["placebos"][d] = {"gap": pg, "pre_rmspe": ppre, "ratio": ppost / ppre if ppre > 0 else np.nan}
        ratios = [v["ratio"] for v in out["placebos"].values() if np.isfinite(v["ratio"])]
        out["p_value"] = (1 + sum(r >= out["ratio"] for r in ratios)) / (1 + len(ratios)) if ratios else None
    return out


def car_study(R: pd.DataFrame, mkt: pd.Series, events: list[tuple[str, Any]], est: tuple[int, int] = (-250, -30),
              win: tuple[int, int] = (-5, 5), model: str = "market") -> dict[str, Any]:
    """Event study on returns R (dates x entities) with market-model or constant-mean benchmark.
    Returns per-event AR / CAR / BHAR, average paths, cross-sectional t and Patell Z."""
    dates = list(R.index)
    m_all = mkt.reindex(R.index).to_numpy(float)
    L = win[1] - win[0] + 1
    rows, ars, sars = [], [], []
    for ent, d in events:
        if ent not in R.columns:
            continue
        i0 = next((i for i, x in enumerate(dates) if pd.Timestamp(x) >= pd.Timestamp(d)), None)
        if i0 is None or i0 + est[0] < 0 or i0 + win[1] >= len(dates):
            continue
        r = R[ent].to_numpy(float)
        ei = np.arange(i0 + est[0], i0 + est[1] + 1)
        ok = np.isfinite(r[ei]) & np.isfinite(m_all[ei])
        if ok.sum() < 30:
            continue
        re_, me_ = r[ei][ok], m_all[ei][ok]
        if model == "market":
            X = np.column_stack([np.ones(len(me_)), me_])
            b, e, _ = _ols(re_, X)
        else:
            b, e = np.array([re_.mean(), 0.0]), re_ - re_.mean()
        Te = len(re_)
        s = float(np.sqrt(e @ e / (Te - 2)))
        wi = np.arange(i0 + win[0], i0 + win[1] + 1)
        rw, mw = r[wi], m_all[wi]
        if not (np.isfinite(rw).all() and np.isfinite(mw).all()):
            continue
        exp_ = b[0] + b[1] * mw
        ar = rw - exp_
        if model == "market":
            adj = 1 + 1 / Te + (mw - me_.mean()) ** 2 / max(((me_ - me_.mean()) ** 2).sum(), 1e-300)
        else:
            adj = 1 + 1 / Te + 0 * mw
        sar = ar / (s * np.sqrt(adj))
        ars.append(ar)
        sars.append((sar, Te))
        rows.append({"entity_id": ent, "event_date": pd.Timestamp(dates[i0]).date(), "alpha": b[0], "beta": b[1],
                     "car": float(ar.sum()), "bhar": float(np.prod(1 + rw) - np.prod(1 + exp_)), "sigma": s, "n_est": Te})
    if len(rows) < 2:
        raise ValueError(f"only {len(rows)} usable events (need >= 2 with full estimation and event windows)")
    A = np.vstack(ars)
    C = np.cumsum(A, axis=1)
    N = len(rows)
    car = np.array([r["car"] for r in rows])
    t_cs = float(car.mean() / (car.std(ddof=1) / np.sqrt(N)))
    csar = np.array([s.sum() / np.sqrt(L) for s, _ in sars])
    var = np.array([(Te - 2) / (Te - 4) for _, Te in sars])
    z_patell = float(csar.sum() / np.sqrt(var.sum()))
    rel = list(range(win[0], win[1] + 1))
    return {"events": pd.DataFrame(rows), "rel": rel, "aar": A.mean(0), "caar": C.mean(0),
            "caar_se": C.std(0, ddof=1) / np.sqrt(N), "t_cs": t_cs, "p_cs": float(2 * stats.t.sf(abs(t_cs), N - 1)),
            "z_patell": z_patell, "p_patell": float(2 * stats.norm.sf(abs(z_patell))), "n_events": N,
            "mean_bhar": float(np.mean([r["bhar"] for r in rows]))}


# ------------------------------------------------------------------------------------------------ multivariate ts


def var_fit(Y: pd.DataFrame, maxlags: int = 8, ic: str | None = "bic", lags: int | None = None, trend: str = "c"):
    from statsmodels.tsa.api import VAR

    m = VAR(Y.astype(float).reset_index(drop=True))
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        if lags is None:
            sel = m.select_order(maxlags=maxlags, trend=trend)
            p = max(1, int(getattr(sel, ic or "bic")))
        else:
            sel, p = None, lags
        r = m.fit(p, trend=trend)
    return r, sel, p


def johansen(Y: pd.DataFrame, det_order: int = 0, k_ar_diff: int = 1) -> dict[str, Any]:
    from statsmodels.tsa.vector_ar.vecm import coint_johansen

    j = coint_johansen(Y.astype(float).to_numpy(), det_order, k_ar_diff)
    k = Y.shape[1]
    rows = []
    rank = 0
    for r in range(k):
        tr, cv = float(j.lr1[r]), j.cvt[r]
        rows.append({"rank_le": r, "trace": tr, "cv90": float(cv[0]), "cv95": float(cv[1]), "cv99": float(cv[2]),
                     "max_eig": float(j.lr2[r]), "max_cv95": float(j.cvm[r][1])})
        if tr > cv[1] and rank == r:
            rank = r + 1
    return {"table": pd.DataFrame(rows), "rank": rank, "eig": j.eig, "evec": j.evec}


def pairs_spread(y: np.ndarray, x: np.ndarray, window: int = 60) -> dict[str, Any]:
    from statsmodels.tsa.stattools import coint

    X = np.column_stack([np.ones(len(x)), x])
    b, s, _ = _ols(y, X)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        eg = coint(y, x, trend="c")
    ds, sl = np.diff(s), s[:-1]
    phi = _ols(ds, np.column_stack([np.ones(len(sl)), sl]))[0][1]
    rho = 1 + phi  # AR(1) coefficient of the spread
    hl = 0.0 if rho <= 0 else (float(-np.log(2) / np.log(rho)) if rho < 1 else np.inf)
    sr = pd.Series(s)
    z_roll = ((sr - sr.rolling(window).mean()) / sr.rolling(window).std()).to_numpy()
    return {"alpha": float(b[0]), "hedge_ratio": float(b[1]), "spread": s, "z": (s - s.mean()) / s.std(ddof=1),
            "z_rolling": z_roll, "half_life": hl, "eg_stat": float(eg[0]), "eg_p": float(eg[1]),
            "eg_crit": [float(c) for c in eg[2]]}


# ------------------------------------------------------------------------------------------------ volatility / regimes


def garch_fit(r: np.ndarray, vol: str = "garch", p: int = 1, q: int = 1, dist: str = "t", mean: str = "constant",
              ar_lags: int = 0):
    from arch import arch_model

    o = 1 if vol == "gjr" else 0
    m = arch_model(r, mean={"constant": "Constant", "zero": "Zero", "ar": "AR"}[mean], lags=ar_lags or None,
                   vol="EGARCH" if vol == "egarch" else "GARCH", p=p, o=o, q=q,
                   dist={"normal": "normal", "t": "t", "skewt": "skewt"}[dist])
    return m.fit(disp="off", show_warning=False)


def kupiec(exceed: np.ndarray, alpha: float) -> tuple[float, float]:
    n, x = len(exceed), int(exceed.sum())
    if n == 0:
        return np.nan, np.nan
    ph = x / n
    ll0 = (n - x) * np.log(1 - alpha) + x * np.log(alpha)
    ll1 = ((n - x) * np.log(1 - ph) if ph < 1 else 0) + (x * np.log(ph) if ph > 0 else 0)
    lr = -2 * (ll0 - ll1)
    return float(lr), float(stats.chi2.sf(lr, 1))


def markov_fit(y: np.ndarray, k: int = 2, model: str = "regression", order: int = 1, exog: np.ndarray | None = None,
               switching_variance: bool = True, search_reps: int = 10, seed: int = 42):
    import statsmodels.api as sm

    np.random.seed(seed)
    if model == "ar":
        m = sm.tsa.MarkovAutoregression(y, k_regimes=k, order=order, exog=exog, switching_variance=switching_variance)
    else:
        m = sm.tsa.MarkovRegression(y, k_regimes=k, trend="c", exog=exog, switching_variance=switching_variance)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        try:
            return m.fit(search_reps=search_reps)
        except np.linalg.LinAlgError:  # a random start diverged (Studio v2 S3): fall back to the default start values
            return m.fit(search_reps=0)


def ucm_fit(y: np.ndarray, level: str = "local level", seasonal: int | None = None, exog: np.ndarray | None = None):
    import statsmodels.api as sm

    m = sm.tsa.UnobservedComponents(y, level=level, seasonal=seasonal or None, exog=exog)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        return m.fit(disp=False, maxiter=500)


def tv_beta(y: np.ndarray, X: np.ndarray):
    """Time-varying regression y_t = x_t' b_t + e_t, b_t = b_{t-1} + u_t (Kalman filter / smoother, MLE variances)."""
    from statsmodels.tsa.statespace.mlemodel import MLEModel

    class TVRegression(MLEModel):
        def __init__(self, endog: np.ndarray, exog: np.ndarray):
            k = exog.shape[1]
            super().__init__(endog, k_states=k, initialization="diffuse")
            self.k = k
            self.ssm["design"] = exog.T[np.newaxis, :, :]
            self.ssm["selection"] = np.eye(k)
            self.ssm["transition"] = np.eye(k)
            self._scale = float(np.var(endog))

        @property
        def param_names(self) -> list[str]:
            return ["sigma2.obs"] + [f"sigma2.beta{i}" for i in range(self.k)]

        @property
        def start_params(self) -> np.ndarray:
            return np.r_[self._scale * 0.8, np.full(self.k, self._scale * 1e-3)]

        def transform_params(self, u: np.ndarray) -> np.ndarray:
            return u ** 2

        def untransform_params(self, c: np.ndarray) -> np.ndarray:
            return c ** 0.5

        def update(self, params: np.ndarray, **kw: Any) -> None:
            params = super().update(params, **kw)
            self.ssm["obs_cov", 0, 0] = params[0]
            self.ssm["state_cov"] = np.diag(params[1:])

    m = TVRegression(np.asarray(y, float), np.asarray(X, float))
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        return m.fit(disp=False, maxiter=500)
