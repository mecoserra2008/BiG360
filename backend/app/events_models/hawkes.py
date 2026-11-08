"""C1: multivariate discrete-time Hawkes processes (EM, exponential kernels) and job `event_escalation`.

Model (daily counts N_k(t), k = 1..K):
    lambda_k(t) = mu_k * s_k(dow(t)) + sum_j alpha_kj X_j(t),   X_j(t) = sum_{l>=1} g(l) N_j(t-l),
    g(l) = (1 - e^-beta) e^(-beta (l-1))  (discretized exponential kernel, sum_l g(l) = 1)
so alpha_kj = expected number of type-k events triggered by one type-j event (branching matrix); its spectral radius
is the branching ratio. EM on the latent branching structure (each count split into background / offspring of each
type with probabilities proportional to the intensity components); beta by profile likelihood on a grid. The
day-of-week background factor s_k absorbs the weekly news cycle (optional).

Escalation probability: Monte-Carlo continuation of the fitted process from the current state.
"""

from __future__ import annotations

import time
import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import polars as pl
from scipy.special import gammaln

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_models import common as C
from app.events_models.weights import get_weights

log = get_logger(__name__)

BETAS = (0.15, 0.3, 0.6, 1.2, 2.5)


def excitation(N: np.ndarray, beta: float, x0: np.ndarray | None = None) -> np.ndarray:
    """X[t] = e^-b X[t-1] + (1 - e^-b) N[t-1]  (T x K); X[0] = x0 (default 0)."""
    T, K = N.shape
    X = np.zeros((T, K))
    d = np.exp(-beta)
    if x0 is not None:
        X[0] = x0
    for t in range(1, T):
        X[t] = d * X[t - 1] + (1 - d) * N[t - 1]
    return X


@dataclass
class HawkesFit:
    mu: np.ndarray                     # K
    alpha: np.ndarray                  # K x K, alpha[k, j] = j -> k
    beta: float
    season: np.ndarray                 # K x 7 multiplicative day-of-week factors (mean 1)
    llf: float
    converged: bool
    n_iter: int
    X_last: np.ndarray = field(default_factory=lambda: np.zeros(0))
    N_last: np.ndarray = field(default_factory=lambda: np.zeros(0))
    dispersion: np.ndarray = field(default_factory=lambda: np.ones(0))   # Pearson var / mean per dim (>= 1)

    @property
    def branching_ratio(self) -> float:
        return float(np.max(np.abs(np.linalg.eigvals(self.alpha))))


def fit_hawkes(N: np.ndarray, beta: float | None = None, dow: np.ndarray | None = None, max_iter: int = 400,
               tol: float = 1e-6, burn: int = 0) -> HawkesFit:
    """EM fit on counts N (T x K). dow: int day-of-week per row (0..6) to estimate seasonal backgrounds.
    beta None -> best of BETAS by log-likelihood. burn: initial rows used only as history."""
    N = np.asarray(N, float)
    if beta is None:
        fits = [fit_hawkes(N, b, dow, max_iter, tol, burn) for b in BETAS]
        return max(fits, key=lambda f: f.llf)
    T, K = N.shape
    X = excitation(N, beta)
    rows = np.arange(burn, T)
    Nr, Xr = N[rows], X[rows]
    D = dow[rows] if dow is not None else np.zeros(len(rows), int)
    nd = 7 if dow is not None else 1
    Dm = np.eye(nd)[D]                                             # T' x nd one-hot
    cnt_d = Dm.sum(axis=0)
    mu = np.maximum(Nr.mean(axis=0) * 0.5, 1e-6)
    seas = np.ones((K, nd))
    alpha = np.full((K, K), 0.1 / K)
    sumX = Xr.sum(axis=0)
    ll_old = -np.inf
    conv = False
    it = 0
    for it in range(1, max_iter + 1):
        base = mu[None, :] * seas[:, D].T                         # T' x K
        trig = Xr[:, None, :] * alpha[None, :, :]                  # T' x K x K  (k <- j)
        lam = base + trig.sum(axis=2)
        lam = np.maximum(lam, 1e-12)
        w0 = Nr * base / lam                                       # expected background counts
        wj = Nr[:, :, None] * trig / lam[:, :, None]                # expected offspring counts per parent type
        # M-step
        alpha = wj.sum(axis=0) / np.maximum(sumX[None, :], 1e-12)
        bg_d = w0.T @ Dm                                           # K x nd
        rate_d = bg_d / np.maximum(cnt_d[None, :], 1)
        mu = np.maximum(rate_d.mean(axis=1), 1e-9)
        seas = np.where(cnt_d[None, :] > 0, rate_d / mu[:, None], 1.0)
        ll = float((Nr * np.log(lam) - lam - gammaln(Nr + 1)).sum())
        if abs(ll - ll_old) < tol * (1 + abs(ll)):
            conv = True
            break
        ll_old = ll
    base = mu[None, :] * seas[:, D].T
    lam = np.maximum(base + Xr @ alpha.T, 1e-12)
    ll = float((Nr * np.log(lam) - lam - gammaln(Nr + 1)).sum())
    d = np.exp(-beta)
    x_next = d * X[-1] + (1 - d) * N[-1]
    if nd == 1:
        seas = np.repeat(seas, 7, axis=1)
    phi = np.maximum(((Nr - lam) ** 2 / lam).mean(axis=0), 1.0)
    return HawkesFit(mu, alpha, beta, seas, ll, conv, it, x_next, N[-1], phi)


def simulate(fit: HawkesFit, horizon: int, n_paths: int = 2000, start_dow: int | None = None, seed: int = 0,
             overdispersed: bool = True) -> np.ndarray:
    """Forward paths of counts (n_paths x horizon x K) from the fitted state. overdispersed: negative-binomial
    draws with the fitted Pearson dispersion (var = phi * lambda; Poisson when phi = 1). An explosive excitation
    matrix (branching ratio >= 1) is scaled to a spectral radius of 0.98 for the simulation."""
    rng = np.random.default_rng(seed)
    K = len(fit.mu)
    d = np.exp(-fit.beta)
    A = fit.alpha
    br = fit.branching_ratio
    if br >= 0.98:
        A = A * (0.98 / br)
    phi = fit.dispersion if (overdispersed and len(fit.dispersion) == K) else np.ones(K)
    X = np.repeat(fit.X_last[None, :], n_paths, axis=0)
    out = np.zeros((n_paths, horizon, K))
    for h in range(horizon):
        s = fit.season[:, (start_dow + h) % 7] if start_dow is not None else np.ones(K)
        lam = np.maximum(fit.mu[None, :] * s[None, :] + X @ A.T, 1e-9)
        if np.any(phi > 1.0001):
            ph = np.broadcast_to(phi[None, :], lam.shape)
            g = rng.gamma(shape=lam / np.maximum(ph - 1, 1e-9), scale=np.maximum(ph - 1, 1e-9))
            lam_draw = np.where(ph > 1.0001, g, lam)
        else:
            lam_draw = lam
        Nh = rng.poisson(lam_draw)
        out[:, h] = Nh
        X = d * X + (1 - d) * Nh
    return out


def expected_path(fit: HawkesFit, horizon: int, start_dow: int | None = None) -> np.ndarray:
    K = len(fit.mu)
    d = np.exp(-fit.beta)
    X = fit.X_last.copy()
    out = np.zeros((horizon, K))
    for h in range(horizon):
        s = fit.season[:, (start_dow + h) % 7] if start_dow is not None else np.ones(K)
        lam = fit.mu * s + fit.alpha @ X
        out[h] = lam
        X = d * X + (1 - d) * lam
    return out


# ------------------------------------------------------------------------------------------------ job


def group_counts(settings: Settings) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Daily event counts per (iso2, group) incl. group 'all' (theme -1). Missing days -> 0."""
    t1 = C.load_t1("country", settings=settings)
    if t1.is_empty():
        return pl.DataFrame(), {}
    t1 = t1.filter(pl.col("label") != "GLOBAL")
    grp = pl.DataFrame({"theme": list(C.THEME_GROUP.keys()), "grp": list(C.THEME_GROUP.values())},
                       schema={"theme": pl.Int8, "grp": pl.Utf8})
    by = t1.filter(pl.col("theme") >= 0).with_columns(pl.col("theme").cast(pl.Int8)).join(grp, on="theme") \
        .group_by("date", "label", "grp").agg(pl.col("ev_n").sum().alias("n"))
    allg = t1.filter(pl.col("theme") == -1).select("date", "label", pl.lit("all").alias("grp"), pl.col("ev_n").alias("n"))
    df = pl.concat([by.select(allg.columns).cast(allg.schema), allg]).rename({"label": "iso2"})
    return df, C.window_desc(t1)


def _fit_one(task: tuple) -> dict[str, Any] | None:
    c, g, own, nbr, dow, n_paths, first, last, now = task
    Nmat = np.column_stack([own, nbr]) if nbr.sum() > 0 else own[:, None]
    try:
        f = fit_hawkes(Nmat, None, dow, burn=7)
    except Exception as e:  # noqa: BLE001
        log.info("hawkes %s/%s failed: %s", c, g, e)
        return None
    sd = int((dow[-1] + 1) % 7)
    sims = simulate(f, 30, n_paths, sd, seed=zlib.crc32(f"{c}|{g}".encode()))
    s7, s30 = sims[:, :7, 0].sum(axis=1), sims[:, :, 0].sum(axis=1)
    r7 = pd.Series(own).rolling(7).sum().dropna()
    thr7 = float(np.quantile(r7, 0.9)) if len(r7) >= 7 else None
    if len(own) >= 60:
        thr30 = float(np.quantile(pd.Series(own).rolling(30).sum().dropna(), 0.9))
    else:
        thr30 = thr7 * 30 / 7 if thr7 is not None else None
    ep = expected_path(f, 30, sd)[:, 0]
    a = f.alpha
    return {"as_of": last, "iso2": c, "theme_group": g, "window_start": first, "n_days": len(own),
            "n_events": int(own.sum()), "mu": float(f.mu[0]), "alpha_self": float(a[0, 0]),
            "alpha_from_nbrs": float(a[0, 1]) if a.shape[1] > 1 else None,
            "alpha_to_nbrs": float(a[1, 0]) if a.shape[0] > 1 else None, "beta": f.beta,
            "branching_ratio": f.branching_ratio, "lambda_now": float(f.mu[0] * f.season[0, sd] + a[0] @ f.X_last),
            "expected_7d": float(ep[:7].sum()), "expected_30d": float(ep.sum()), "threshold_7d": thr7,
            "threshold_30d": thr30, "p_escalation_7d": float((s7 > thr7).mean()) if thr7 is not None else None,
            "p_escalation_30d": float((s30 > thr30).mean()) if thr30 is not None else None,
            "llf": f.llf, "converged": bool(f.converged), "computed_at": now}


def run_escalation(settings: Settings | None = None, min_rate: float = 5.0, n_paths: int = 2000,
                   w_kind: str = "contiguity", min_days: int = 21) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    df, win = group_counts(s)
    if df.is_empty():
        return {"rows": 0, "note": "no T1"}
    days = pd.date_range(df["date"].min(), df["date"].max(), freq="D")
    if len(days) < min_days:
        return {"rows": 0, "note": f"only {len(days)} days of history (< {min_days})", "window": win}
    dow = days.dayofweek.to_numpy()
    now = C.now_utc()
    pdf = df.to_pandas()
    tasks = []
    for g, sub in pdf.groupby("grp"):
        M = sub.pivot_table(index="date", columns="iso2", values="n", aggfunc="sum").reindex(days).fillna(0.0)
        codes = [c for c in M.columns if M[c].mean() >= min_rate]
        if len(codes) < 2:
            continue
        have, Wb, _ = get_weights(w_kind, list(M.columns))
        pos = {c: i for i, c in enumerate(have)}
        Mh = M[have].to_numpy()
        for c in codes:
            if c not in pos:
                continue
            nb = np.nonzero(Wb[pos[c]])[0]
            own = M[c].to_numpy()
            nbr = Mh[:, nb].sum(axis=1) if len(nb) else np.zeros(len(own))
            tasks.append((c, g, own, nbr, dow, n_paths, days[0].date(), days[-1].date(), now))
    rows = []
    import concurrent.futures as cf  # noqa: PLC0415
    import os  # noqa: PLC0415

    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, "1")
    from app.ops.ram_budget import pool_workers, small_worker_threads  # noqa: PLC0415

    nw = pool_workers(max(1, min(10, (os.cpu_count() or 8) - 6)), per_worker_gb=0.5)   # ram_fix 2026-10-31
    if len(tasks) >= 50 and nw > 1:
        with small_worker_threads(1), cf.ProcessPoolExecutor(max_workers=nw) as ex:
            rows = [r for r in ex.map(_fit_one, tasks, chunksize=16) if r]
    else:
        rows = [r for r in map(_fit_one, tasks) if r]
    out = pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()
    n = C.write(out, "event_escalation", settings=s)
    top = []
    if n:
        top = out.filter(pl.col("theme_group").is_in(["geo", "social", "politics"]) & (pl.col("n_events") >= 500)) \
            .sort("p_escalation_7d", descending=True).head(10) \
            .select("iso2", "theme_group", "p_escalation_7d", "branching_ratio").to_dicts()
    rep = {"rows": n, "window": win, "seconds": round(time.perf_counter() - t0, 1),
           "top_geo_social_7d": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in r.items()} for r in top]}
    C.update_state("event_escalation", rep | {"at": now.isoformat(timespec="seconds")}, s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_escalation(ctx.settings, min_rate=float(p.get("min_rate", 5.0)), n_paths=int(p.get("paths", 2000)),
                         w_kind=str(p.get("w_kind", "contiguity")))
    return JobResult(status="success", rows_written={"event_escalation": rep.get("rows", 0)}, message=str(rep)[:2000])
