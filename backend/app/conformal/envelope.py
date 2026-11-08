"""Application A: conformal price envelope per security (docs/CONFORMAL_TECHNICALS_PLAN.md §2-3). MODEL-BASED.

Target vectors (origin t = close of t, all from split + dividend adjusted closes, local currency):
  joint    Y_t = (log C_{t+1}/C_t, log C_{t+5}/C_t, log C_{t+10}/C_t, log C_{t+20}/C_t)   (overlapping horizons)
  nextbar  X_t = (log H_{t+1}/C_t, log L_{t+1}/C_t, log C_{t+1}/C_t)

Filters (emit before the outcome, from data <= t):
  ewma     RiskMetrics EWMA daily variance s2_t (lambda 0.94); joint covariance Sigma_ij = V(min(h_i, h_j)) with
           V(h) = h s2_t (+ J_t if an earnings day falls in (t, t+h]) - the random-walk "nested" shape: the
           increments (Y_1, Y_5 - Y_1, Y_10 - Y_5, Y_20 - Y_10) are uncorrelated with variances V(h_k) - V(h_k-1),
           so R_t = sum_k increment_k^2 / (V(h_k) - V(h_k-1)) exactly; mean 0 (return means are noise)
  garch    GARCH(1,1) per security (variance targeting, grid MLE on the fit window, frozen), V(h) from the GARCH
           variance term structure, same nested shape
  gru      learned horizon head (app.conformal.learned.HorizonGRU) on per-security features, mvp only
nextbar shape: z = X / s_t standardised by the EWMA close vol; slow EWMA (0.99) mean m_t and covariance C_t of z,
  Y_hat = s_t m_t, Sigma = s_t^2 C_t (+ J_t 11' when t+1 is an earnings day: a gap moves H, L and C together).

Earnings days and gaps: earnings reaction days never update the variance filters (no inflation of normal-day
bands); their jump variance J_t (mean excess squared reaction-day return over the last 8 events, shrunk to
4 s2_t) enters the shape only for horizons that contain an expected event; and the radius is calibrated in
separate pools (normal vs event origins). Overnight gaps (|log O_{t+1}/C_t| > 3 s_t) are flagged on realised rows
and audited separately.

Calibration: pooled split conformal per (region, sector) pool (fallback region-wide when the pool has < 30
securities), rolling window of the last `window` realised origins (origin s usable at t when s + lag < t; lag = 20
for the joint set, 1 for nextbar), re-sorted every `recal_every` sessions; ACI on the pool's average miscoverage
(gamma 0.005) gives q_aci. Coverage is MARGINAL over time and securities - "calibrated to X% over history", never
"X% chance today".

touch_probability(level, horizon): a touch within h sessions is a PATH event. We calibrate it empirically: per pool
and horizon, the distribution of the standardised running maximum M_up = max_{k<=h} log(H_{t+k}/C_t) / sqrt(V_t(h))
(and M_dn with lows) over the calibration origins; P(touch) = share of calibration origins with M >= the level's
standardised distance (a conformal-style empirical probability, accurate to ~1/(n+1) under exchangeability). It is
NOT a Gaussian probability.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.conformal.data import ffill, log_returns
from app.conformal.filters import garch_filter, garch_grid_fit, garch_term
from app.conformal.scores import quantile_from_sorted

HORIZONS = (1, 5, 10, 20)
# Technicals v2 T1 (docs/TECHNICALS_V2_PLAN.md): the chart fan = the MARGINAL band at every horizon 1..20 sessions,
# V(h) from the same variance term structure, radius calibrated per h in the same pools (scripts/conformal/fan_check.py)
FAN_HORIZONS = tuple(range(1, 21))
ALPHAS = (0.2, 0.1, 0.05)
LAM = 0.94
LAM_SHAPE = 0.99
JUMP_EVENTS = 8
JUMP_PRIOR_MULT = 4.0
GAP_SIGMAS = 3.0
TOUCH_PROBS = np.round(np.arange(0.01, 1.0, 0.01), 2)
S2_FLOOR = 1e-8                # daily variance floor (0.01% vol): stale prints must not make the shape singular


# ------------------------------------------------------------------------------------------------ inputs


@dataclass
class EnvArrays:
    r: np.ndarray          # (T, N) close-to-close log returns
    lc: np.ndarray         # log adj close, ffilled (limit 3)
    lh: np.ndarray         # log adj high (NaN on non-print days)
    ll: np.ndarray
    lo: np.ndarray
    printed: np.ndarray    # (T, N) bool: security printed at t
    E: np.ndarray          # (T, N) bool: earnings reaction day
    bad: np.ndarray | None = None   # (T, N) bool: data-error bar (|log return| > 1 or high/low range > 1: unit flips)

    def bad_in(self, h: int) -> np.ndarray:
        """(T, N) bool: a data-error bar falls in (t, t + h]."""
        T, N = self.lc.shape
        b = self.bad if self.bad is not None else np.zeros((T, N), dtype=bool)
        cs = np.vstack([np.zeros((1, N)), np.cumsum(b, axis=0)])
        k = np.arange(T)
        hi = np.minimum(k + h, T - 1)
        out = (cs[hi + 1] - cs[k + 1]) > 0
        return out


def make_arrays(C: np.ndarray, H: np.ndarray, L: np.ndarray, O: np.ndarray, E: np.ndarray) -> EnvArrays:  # noqa: E741
    printed = np.isfinite(C) & (C > 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        lc_raw = np.log(C)
        lh, ll = np.log(H), np.log(L)
        raw_r = log_returns(C, max_abs=np.inf)
        bad = (np.abs(np.nan_to_num(raw_r)) > 1.0) | (np.nan_to_num(lh - ll) > 1.0)
        r = np.where(bad, np.nan, raw_r)
        return EnvArrays(r=r, lc=ffill(lc_raw, 3), lh=lh, ll=ll, lo=np.log(O), printed=printed, E=E.astype(bool), bad=bad)


def forward_targets(a: EnvArrays, horizons: tuple[int, ...] = HORIZONS) -> np.ndarray:
    """(T, N, K) forward log returns from origin t (NaN if t not printed or t + h beyond the panel)."""
    T, N = a.lc.shape
    Y = np.full((T, N, len(horizons)), np.nan)
    for k, h in enumerate(horizons):
        if h < T:
            Y[: T - h, :, k] = a.lc[h:] - a.lc[: T - h]
        Y[a.bad_in(h), k] = np.nan
    Y[~a.printed] = np.nan
    return Y


def nextbar_targets(a: EnvArrays) -> np.ndarray:
    """(T, N, 3) (log H_{t+1}/C_t, log L_{t+1}/C_t, log C_{t+1}/C_t); NaN unless printed at t and t + 1."""
    T, N = a.lc.shape
    X = np.full((T, N, 3), np.nan)
    ok = a.printed[:-1] & a.printed[1:]
    base = a.lc[:-1]
    X[:-1, :, 0] = np.where(ok, a.lh[1:] - base, np.nan)
    X[:-1, :, 1] = np.where(ok, a.ll[1:] - base, np.nan)
    X[:-1, :, 2] = np.where(ok, a.lc[1:] - base, np.nan)
    X[a.bad_in(1)] = np.nan
    return X


def first_event_offset(E: np.ndarray, hmax: int = 20) -> np.ndarray:
    """(T, N) int: smallest k in 1..hmax with E[t + k] (0 = none within hmax)."""
    T, N = E.shape
    out = np.zeros((T, N), dtype=np.int16)
    for k in range(hmax, 0, -1):
        hit = np.zeros((T, N), dtype=bool)
        hit[: T - k] = E[k:]
        out = np.where(hit, k, out)
    return out


# ------------------------------------------------------------------------------------------------ variance filters


def ewma_var(r: np.ndarray, E: np.ndarray, lam: float = LAM, init_n: int = 60) -> np.ndarray:
    """s2[t] = sigma^2_{t+1|t} after consuming r_t; NaN returns and earnings days are 'no news'."""
    T, N = r.shape
    x2 = np.where(E, np.nan, r * r)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        v0 = np.nanmean(x2[: max(init_n, 5)], axis=0)
    glob = np.nanmedian(v0) if np.isfinite(v0).any() else 4e-4
    s2 = np.where(np.isfinite(v0) & (v0 > 0), v0, glob)
    out = np.empty((T, N))
    for t in range(T):
        x = x2[t]
        ok = np.isfinite(x)
        s2 = np.where(ok, lam * s2 + (1 - lam) * np.where(ok, x, 0.0), s2)
        out[t] = s2
    seen = np.cumsum(np.isfinite(x2), axis=0)
    out = np.maximum(out, S2_FLOOR)
    out[seen < 20] = np.nan                    # not enough history yet
    return out


def jump_var(r: np.ndarray, E: np.ndarray, s2: np.ndarray, k: int = JUMP_EVENTS, prior_mult: float = JUMP_PRIOR_MULT) -> np.ndarray:
    """J[t]: earnings jump variance known at t = mean excess squared reaction-day return over the last k events,
    shrunk toward prior_mult * s2 with 2 pseudo-events."""
    T, N = r.shape
    s2_prev = np.vstack([s2[:1], s2[:-1]])
    exc = np.where(E & np.isfinite(r) & np.isfinite(s2_prev), np.maximum(r * r - s2_prev, 0.0), np.nan)
    out = np.empty((T, N))
    ring = np.zeros((k, N))
    pos = np.zeros(N, dtype=np.int64)
    cnt = np.zeros(N)
    tot = np.zeros(N)
    for t in range(T):
        hit = np.nonzero(np.isfinite(exc[t]))[0]
        if hit.size:
            old = ring[pos[hit], hit]
            full = cnt[hit] >= k
            tot[hit] += exc[t, hit] - np.where(full, old, 0.0)
            ring[pos[hit], hit] = exc[t, hit]
            pos[hit] = (pos[hit] + 1) % k
            cnt[hit] = np.minimum(cnt[hit] + 1, k)
        m = np.where(cnt > 0, tot / np.maximum(cnt, 1), 0.0)
        out[t] = (cnt * m + 2.0 * prior_mult * s2[t]) / (cnt + 2.0)
    return out


@dataclass
class GarchParams:
    a: np.ndarray
    b: np.ndarray
    vbar: np.ndarray


def garch_fit(r: np.ndarray, E: np.ndarray, fit_rows: slice) -> GarchParams:
    x = np.where(E, np.nan, r)[fit_rows]
    p = garch_grid_fit(x)
    return GarchParams(p["a"], p["b"], p["vbar"])


def garch_var(r: np.ndarray, E: np.ndarray, p: GarchParams) -> np.ndarray:
    x = np.where(E, np.nan, r)
    out = np.maximum(garch_filter(x, p.a, p.b, p.vbar), S2_FLOOR)
    seen = np.cumsum(np.isfinite(x), axis=0)
    out[seen < 20] = np.nan
    return out


def horizon_V(s2: np.ndarray, J: np.ndarray, ke: np.ndarray, horizons: tuple[int, ...] = HORIZONS,
              garch: GarchParams | None = None) -> np.ndarray:
    """Cumulative variances V(h) (T, N, K) including the earnings jump for horizons covering the first event."""
    if garch is None:
        V = s2[..., None] * np.array(horizons, dtype=np.float64)
    else:
        V = garch_term(s2, garch.a, garch.b, garch.vbar, list(horizons))
    hz = np.array(horizons)
    jump = (ke[..., None] > 0) & (ke[..., None] <= hz)
    return V + np.where(jump, J[..., None], 0.0)


def nested_score(Y: np.ndarray, mu: np.ndarray, V: np.ndarray) -> np.ndarray:
    """Exact squared Mahalanobis score under the nested shape Sigma_ij = V(min(h_i, h_j)), (T, N)."""
    r = Y - mu
    inc = np.diff(r, axis=-1, prepend=0.0)
    w = np.diff(V, axis=-1, prepend=0.0)
    w = np.maximum(w, 1e-14)
    return (inc * inc / w).sum(-1)


# ------------------------------------------------------------------------------------------------ next bar


def nextbar_filter(a: EnvArrays, s2: np.ndarray, J: np.ndarray, lam: float = LAM_SHAPE, init_n: int = 60
                   ) -> tuple[np.ndarray, np.ndarray]:
    """(mu (T, N, 3), Sigma (T, N, 3, 3)) for the bar after t, from the standardised next-bar vectors up to t."""
    T, N = s2.shape
    Xn = nextbar_targets(a)                         # row t: bar t+1 relative to close t (known at close t+1)
    s_prev = np.sqrt(s2)
    Z = Xn / s_prev[..., None]                      # standardised by the vol known at t
    Ev_next = np.zeros_like(a.E)
    Ev_next[:-1] = a.E[1:]
    Z[Ev_next] = np.nan
    mu = np.empty((T, N, 3))
    Sg = np.empty((T, N, 3, 3))
    z0 = Z[:init_n]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        m = np.nanmean(z0, axis=0)
    m = np.where(np.isfinite(m), m, np.array([0.8, -0.8, 0.0]))
    d0 = z0 - m
    C = np.einsum("tni,tnj->nij", np.nan_to_num(d0), np.nan_to_num(d0)) / np.maximum(np.isfinite(d0[..., 0]).sum(0), 1)[:, None, None]
    bad = ~np.isfinite(C).all(axis=(1, 2)) | (np.trace(C, axis1=1, axis2=2) <= 0)
    C[bad] = np.array([[0.5, 0.2, 0.4], [0.2, 0.5, 0.4], [0.4, 0.4, 1.0]])
    for t in range(T):
        # row t's outcome (Z[t]) is only known at t + 1: the law for bar t+1 uses Z rows <= t - 1
        mu[t] = m * s_prev[t][:, None]
        Sg[t] = C * s2[t][:, None, None]
        z = Z[t]
        ok = np.isfinite(z).all(-1) & (np.abs(np.nan_to_num(z)) < 25.0).all(-1)   # |z| >= 25: data error, no update
        if ok.any():
            dz = np.where(ok[:, None], z - m, 0.0)
            m = np.where(ok[:, None], lam * m + (1 - lam) * np.where(ok[:, None], z, 0.0), m)
            C = np.where(ok[:, None, None], lam * C + (1 - lam) * np.einsum("ni,nj->nij", dz, dz), C)
    jump = Ev_next & np.isfinite(J)
    Sg = Sg + np.where(jump[..., None, None], J[..., None, None] * np.ones((3, 3)), 0.0)
    return mu, Sg


def mahal_small(r: np.ndarray, S: np.ndarray, chunk: int = 200_000) -> np.ndarray:
    """Batched squared Mahalanobis for small K: r (..., K), S (..., K, K)."""
    shp = r.shape[:-1]
    K = r.shape[-1]
    rf = r.reshape(-1, K)
    Sf = S.reshape(-1, K, K)
    out = np.full(rf.shape[0], np.nan)
    ok = np.isfinite(rf).all(-1) & np.isfinite(Sf).all((-1, -2))
    idx = np.nonzero(ok)[0]
    for a in range(0, len(idx), chunk):
        ii = idx[a:a + chunk]
        S_ = Sf[ii]
        jit = 1e-9 * np.maximum(np.trace(S_, axis1=1, axis2=2) / K, 1e-16)
        S_ = S_ + jit[:, None, None] * np.eye(K)
        try:
            z = np.linalg.solve(S_, rf[ii][..., None])[..., 0]
        except np.linalg.LinAlgError:
            z = np.einsum("nij,nj->ni", np.linalg.pinv(S_), rf[ii])
        out[ii] = (rf[ii] * z).sum(-1)
    return out.reshape(shp)


# ------------------------------------------------------------------------------------------------ pooled calibration


@dataclass
class PoolCal:
    """Pooled rolling split-conformal radii with separate normal / event pools and pooled ACI.

    R (T, N) scores by origin, pool (N,) int pool index, ev (T, N) bool event-origin flags, lag (outcome delay)."""
    R: np.ndarray
    pool: np.ndarray
    ev: np.ndarray
    lag: int
    window: int = 500
    recal_every: int = 5
    min_n: int = 200
    alphas: tuple[float, ...] = ALPHAS
    gamma: float = 0.005
    aci: bool = True                                       # False: plain radii only (no ACI path / feedback)
    q: np.ndarray = field(default=None, repr=False)        # (T, P, 2, A) plain radii
    q_aci: np.ndarray = field(default=None, repr=False)    # (T, P, 2, A)
    alpha_t: np.ndarray = field(default=None, repr=False)  # (T, P, A)
    n_cal: np.ndarray = field(default=None, repr=False)    # (T, P, 2)

    def run(self, t_start: int, t_end: int | None = None) -> PoolCal:
        T, N = self.R.shape
        t_end = t_end or T
        P = int(self.pool.max()) + 1 if self.pool.size else 0
        A = len(self.alphas)
        self.q = np.full((T, P, 2, A), np.nan)
        self.q_aci = np.full((T, P, 2, A), np.nan)
        self.alpha_t = np.full((T, P, A), np.nan)
        self.n_cal = np.zeros((T, P, 2), dtype=np.int64)
        members = [np.nonzero(self.pool == p)[0] for p in range(P)]
        for p, mem in enumerate(members):
            if not mem.size:
                continue
            Rp = self.R[:, mem]
            Ep = self.ev[:, mem]
            alpha_t = np.array(self.alphas, dtype=np.float64)
            sorted_cache: tuple[int, np.ndarray, np.ndarray, np.ndarray] | None = None
            pending: list[tuple[int, np.ndarray]] = []        # (t, levels used)
            for t in range(t_start, t_end):
                key = t - (t % self.recal_every)
                if sorted_cache is None or sorted_cache[0] != key:
                    hi = key - self.lag
                    lo = max(0, hi - self.window)
                    blk, eb = Rp[lo:max(hi, lo)], Ep[lo:max(hi, lo)]
                    ok = np.isfinite(blk)
                    sn = np.sort(blk[ok & ~eb])
                    se = np.sort(blk[ok & eb])
                    if se.size < self.min_n:                 # thin event pool: fall back to all scores
                        se = np.sort(np.concatenate([sn, se]))
                    # plain radii only change at a recalibration: computed once per block
                    qp = np.full((2, A), np.nan)
                    for k, s in enumerate((sn, se)):
                        if s.size >= self.min_n:
                            qp[k] = quantile_from_sorted(s, np.array(self.alphas))
                    sorted_cache = (key, sn, se, qp)
                _, sn, se, qp = sorted_cache
                self.n_cal[t, p] = (sn.size, se.size)
                self.q[t, p] = qp
                self.alpha_t[t, p] = alpha_t
                if not self.aci:
                    continue
                for k, s in enumerate((sn, se)):
                    if s.size >= self.min_n:
                        # vectorised over the levels; alpha <= 0 gives an unbounded (inf) radius
                        self.q_aci[t, p, k] = quantile_from_sorted(s, alpha_t)
                pending.append((t, self.q_aci[t, p].copy()))
                # feedback: origin s = t - lag is realised at t + 1 -> use it for the level at t + 1
                s_idx = t - self.lag
                if pending and pending[0][0] == s_idx:
                    _, qa = pending.pop(0)
                    rs, es = Rp[s_idx], Ep[s_idx]
                    okk = np.isfinite(rs)
                    if okk.any() and np.isfinite(qa).any():
                        qsel = np.where(es[:, None], qa[1][None, :], qa[0][None, :])     # (M, A)
                        err = (rs[okk][:, None] > qsel[okk]).mean(0)
                        alpha_t = alpha_t + self.gamma * (np.array(self.alphas) - err)
                while pending and pending[0][0] < s_idx:
                    pending.pop(0)
        return self

    def radius(self, t: int, n: int, ev: bool, aci: bool = False) -> np.ndarray:
        src = self.q_aci if aci else self.q
        return src[t, self.pool[n], int(ev)]


def pool_index(keys: list[str]) -> tuple[np.ndarray, list[str]]:
    u = sorted(set(keys))
    m = {k: i for i, k in enumerate(u)}
    return np.array([m[k] for k in keys], dtype=np.int64), u


def pool_keys(region: str, sectors: list[str], min_size: int = 30) -> list[str]:
    from collections import Counter

    c = Counter(sectors)
    return [f"{region}|{s}" if c[s] >= min_size else f"{region}|*" for s in sectors]


# ------------------------------------------------------------------------------------------------ touch probabilities


def running_extremes(a: EnvArrays, horizons: tuple[int, ...] = HORIZONS) -> tuple[np.ndarray, np.ndarray]:
    """(T, N, K) max_{k<=h} log(H_{t+k}/C_t) and max_{k<=h} log(C_t / L_{t+k}) (NaN when the path is incomplete)."""
    T, N = a.lc.shape
    K = len(horizons)
    up = np.full((T, N, K), np.nan)
    dn = np.full((T, N, K), np.nan)
    hmax = max(horizons)
    cu = np.full((T, N), -np.inf)
    cd = np.full((T, N), -np.inf)
    nvalid = np.zeros((T, N))
    for k in range(1, hmax + 1):
        sh = np.full((T, N), np.nan)
        sl = np.full((T, N), np.nan)
        sh[: T - k] = a.lh[k:] - a.lc[: T - k]
        sl[: T - k] = a.lc[: T - k] - a.ll[k:]
        cu = np.fmax(cu, sh)
        cd = np.fmax(cd, sl)
        nvalid += np.isfinite(sh)
        if k in horizons:
            j = horizons.index(k)
            full = np.zeros((T, N), dtype=bool)
            full[: T - k] = True
            okp = full & (nvalid >= max(1, int(0.8 * k))) & a.printed & ~a.bad_in(k)
            up[..., j] = np.where(okp, cu, np.nan)
            dn[..., j] = np.where(okp, cd, np.nan)
    return up, dn


def touch_curve(M_std: np.ndarray, probs: np.ndarray = TOUCH_PROBS) -> np.ndarray:
    """Quantiles of the standardised running extreme at levels `probs` (empirical CDF inverse)."""
    v = M_std[np.isfinite(M_std)]
    if v.size < 50:
        return np.full(len(probs), np.nan)
    return np.quantile(v, probs)


def touch_probability(level: float, last_close: float, horizon: int, V_h: float, curve_up: np.ndarray,
                      curve_dn: np.ndarray, probs: np.ndarray = TOUCH_PROBS) -> float | None:
    """Calibrated P(price touches `level` within `horizon` sessions) from the envelope's V(h) and the pool's
    calibrated running-extreme curves (MODEL-BASED, marginal over history; a path probability, not Gaussian)."""
    if not (level > 0 and last_close > 0 and V_h > 0):
        return None
    x = math.log(level / last_close)
    if x == 0:
        return 1.0
    u = abs(x) / math.sqrt(V_h)
    q = curve_up if x > 0 else curve_dn
    if q is None or not np.isfinite(q).all():
        return None
    if u <= q[0]:                                   # closer than the 1% quantile: interpolate to P = 1 at u = 0
        return float(1.0 - probs[0] * (u / q[0])) if q[0] > 0 else float(1.0 - probs[0])
    if u >= q[-1]:                                  # beyond the 99% quantile: report the floor (< 1%)
        return float(0.5 * (1.0 - probs[-1]))
    return float(1.0 - np.interp(u, q, probs))


def band_prices(last_close: np.ndarray, mu: np.ndarray, var: np.ndarray, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    with np.errstate(over="ignore", invalid="ignore"):
        hw = np.sqrt(np.maximum(q, 0.0) * var)
        lo, hi = last_close * np.exp(mu - hw), last_close * np.exp(mu + hw)
    return np.where(np.isfinite(lo), lo, np.nan), np.where(np.isfinite(hi), hi, np.nan)


def summary_cols() -> list[str]:
    cols = []
    for h in HORIZONS:
        for a in ALPHAS:
            pct = int(round(100 * (1 - a)))
            cols += [f"b{h}_{pct}_lo", f"b{h}_{pct}_hi"]
    return cols


def params() -> dict[str, Any]:
    return {"horizons": HORIZONS, "fan_horizons": FAN_HORIZONS, "alphas": ALPHAS, "lam": LAM, "lam_shape": LAM_SHAPE, "jump_events": JUMP_EVENTS,
            "jump_prior_mult": JUMP_PRIOR_MULT, "gap_sigmas": GAP_SIGMAS}
