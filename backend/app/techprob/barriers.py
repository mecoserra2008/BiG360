"""T2: two-barrier first-passage probabilities for a trade setup (docs/TECHNICALS_V2_PLAN.md T2). MODEL-BASED.

Event. From the origin close C_t, an upper barrier at log distance x_up > 0 and a lower barrier at x_dn > 0. On the
sessions k = 1..H after t (the security's own prints), the high reaching C_t e^{x_up} is an "up" passage and the low
reaching C_t e^{-x_dn} a "down" passage. Outcome within H sessions: `up` (up first), `down` (down first), `tie` (both
first reached on the same bar; the intrabar order is unknown) or `neither`. A long setup (T1 above, invalidation
below) wins on `up` and loses on `down` or `tie` (conservative); a short setup mirrors it.

Volatility units. Distances are standardised by sigma_H = sqrt(V(H)), the conformal envelope's variance term
V(H) = H s2_t + J_t 1[earnings day in (t, t + H]] (EWMA lambda 0.94 on non-earnings days + the earnings jump
variance, app.conformal.envelope): a = x_up / sigma_H, b = x_dn / sigma_H.

Estimator (empirical, a path event; not Gaussian). Over a log-spaced grid of (a, b) we count, for every historical
origin, which outcome its realised path produced (one pass per origin gives the whole grid because the running
extremes are monotone). Counts are kept per pool = region x volatility regime x earnings window (vol regime =
sigma_d(t) / median sigma_d over the previous 252 sessions: low < 0.85 <= mid <= 1.2 < high; earnings = an earnings
day within the H sessions) and per origin quarter, so the walk-forward fit uses only quarters that ended (plus a
one-quarter purge) before the test year. The fit shrinks each pool toward its region (all vol regimes) and the
region toward the all-region pool (beta-binomial pseudo-counts m), then smooths monotonically (P(up) non-increasing
in a and non-decreasing in b; P(down) mirrored; iterated pool-adjacent-violators). Probabilities at arbitrary (a, b)
are bilinear in (log a, log b). The expected terminal move of `neither` paths (in sigma_H units) is kept per cell so
the expected R can also be reported with the time exit marked to market.

Baselines: the naive "R:R only" rule (P(win) = risk / (reward + risk), i.e. the driftless gambler's-ruin exit
probability with no horizon), and the driftless Brownian two-barrier formula (continuous monitoring, unit time in
sigma_H units), `bm_two_barrier`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

GRID = np.array([0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.65, 0.8, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.5, 8.0])
LOG_GRID = np.log(GRID)
G = len(GRID)
HORIZONS = (5, 10, 20, 40, 60)
HMAX = max(HORIZONS)
REGIONS = ("US", "EU", "UK", "JP", "OTHER")
ALL_REGION = "ALL"
VOL_REGIMES = ("low", "mid", "high")
VOL_CUTS = (0.85, 1.2)
VOL_LOOKBACK = 252
OUT = ("up", "down", "tie", "mtm")       # per-cell sums: counts of up / down / tie, sum of z_H over `neither` paths
SHRINK_M = 500.0
PURGE_QUARTERS = 1
Q0_YEAR = 2000                            # quarter index 0 = 2000Q1


def region_code(region: str | None) -> int:
    r = (region or "").upper()
    return REGIONS.index(r) if r in REGIONS else REGIONS.index("OTHER")


def quarter_index(dates: np.ndarray) -> np.ndarray:
    m = dates.astype("datetime64[M]").astype(np.int64)          # months since 1970-01
    return (m - (Q0_YEAR - 1970) * 12) // 3


def n_quarters(last_year: int = 2027) -> int:
    return (last_year - Q0_YEAR + 1) * 4


# ------------------------------------------------------------------------------------------------ paths


def path_extremes(lc: np.ndarray, lh: np.ndarray, ll: np.ndarray, hmax: int = HMAX) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per origin t: U[t, k-1] = max_{j<=k} log(H_{t+j}/C_t), D[t, k-1] = max_{j<=k} log(C_t/L_{t+j}),
    Z[t, k-1] = log(C_{t+k}/C_t); NaN when t + k is beyond the series."""
    n = lc.shape[0]
    U = np.full((n, hmax), np.nan)
    D = np.full((n, hmax), np.nan)
    Z = np.full((n, hmax), np.nan)
    cu = np.full(n, -np.inf)
    cd = np.full(n, -np.inf)
    for k in range(1, hmax + 1):
        if k >= n:
            break
        su = np.full(n, np.nan)
        sd = np.full(n, np.nan)
        su[: n - k] = lh[k:] - lc[: n - k]
        sd[: n - k] = lc[: n - k] - ll[k:]
        cu = np.fmax(cu, su)
        cd = np.fmax(cd, sd)
        ok = np.zeros(n, dtype=bool)
        ok[: n - k] = True
        U[:, k - 1] = np.where(ok, cu, np.nan)
        D[:, k - 1] = np.where(ok, cd, np.nan)
        Z[: n - k, k - 1] = lc[k:] - lc[: n - k]
    return U, D, Z


def passage_times(M: np.ndarray, thr: np.ndarray) -> np.ndarray:
    """First k (1-based) with M[:, k-1] >= thr[:, j] for monotone M (n, H) and thresholds (n, J); H + 1 = never."""
    H = M.shape[1]
    cnt = (M[:, :, None] >= thr[:, None, :]).sum(1)
    return H - cnt + 1


def outcomes(U: np.ndarray, D: np.ndarray, x_up: np.ndarray, x_dn: np.ndarray, H: int) -> np.ndarray:
    """Outcome codes for explicit barriers (log distances, (n,)): 0 neither, 1 up, 2 down, 3 tie; -1 incomplete path."""
    Uh, Dh = U[:, :H], D[:, :H]
    ok = np.isfinite(Uh).all(1) & np.isfinite(Dh).all(1) & np.isfinite(x_up) & np.isfinite(x_dn)
    tu = passage_times(np.nan_to_num(Uh, nan=-np.inf), np.nan_to_num(x_up, nan=np.inf)[:, None])[:, 0]
    td = passage_times(np.nan_to_num(Dh, nan=-np.inf), np.nan_to_num(x_dn, nan=np.inf)[:, None])[:, 0]
    out = np.zeros(len(x_up), dtype=np.int8)
    out[(tu < td) & (tu <= H)] = 1
    out[(td < tu) & (td <= H)] = 2
    out[(tu == td) & (tu <= H)] = 3
    out[~ok] = -1
    return out


def passage_bars(U: np.ndarray, D: np.ndarray, x_up: np.ndarray, x_dn: np.ndarray, H: int) -> tuple[np.ndarray, np.ndarray]:
    """(tau_up, tau_down) 1-based first-passage bars within H (H + 1 = not within H)."""
    tu = passage_times(np.nan_to_num(U[:, :H], nan=-np.inf), np.nan_to_num(x_up, nan=np.inf)[:, None])[:, 0]
    td = passage_times(np.nan_to_num(D[:, :H], nan=-np.inf), np.nan_to_num(x_dn, nan=np.inf)[:, None])[:, 0]
    return tu, td


def vol_regime(s2: np.ndarray, lookback: int = VOL_LOOKBACK) -> np.ndarray:
    """0 low / 1 mid / 2 high: sigma_d(t) vs the median sigma_d of the previous `lookback` sessions (-1 unknown)."""
    s = np.sqrt(s2)
    n = s.shape[0]
    out = np.full(n, -1, dtype=np.int8)
    if n <= 20:
        return out
    from numpy.lib.stride_tricks import sliding_window_view

    lb = min(lookback, n - 1)
    win = sliding_window_view(s, lb)                       # win[i] = s[i : i + lb]
    with np.errstate(invalid="ignore"):
        med = np.nanmedian(win, axis=1)                     # median of s[t - lb : t] for t = i + lb
    ratio = np.full(n, np.nan)
    ratio[lb:] = s[lb:] / med[: n - lb]
    out[np.isfinite(ratio) & (ratio < VOL_CUTS[0])] = 0
    out[np.isfinite(ratio) & (ratio >= VOL_CUTS[0]) & (ratio <= VOL_CUTS[1])] = 1
    out[np.isfinite(ratio) & (ratio > VOL_CUTS[1])] = 2
    return out


def vol_regime_last(s2_hist: np.ndarray, lookback: int = VOL_LOOKBACK) -> int:
    s = np.sqrt(np.asarray(s2_hist, dtype=np.float64))
    s = s[np.isfinite(s)]
    if s.size < 60:
        return 1
    ref = np.median(s[-lookback - 1:-1])
    r = s[-1] / ref if ref > 0 else 1.0
    return 0 if r < VOL_CUTS[0] else (2 if r > VOL_CUTS[1] else 1)


# ------------------------------------------------------------------------------------------------ grid counts


@dataclass
class GridCounts:
    """Sufficient statistics of one region: C[pool, quarter, h, ia, ib, OUT] and N[pool, quarter, h] (valid origins);
    pool = vol_regime x 2 + earnings_window (6 pools)."""
    region: str
    C: np.ndarray
    N: np.ndarray

    @classmethod
    def empty(cls, region: str, nq: int | None = None) -> GridCounts:
        nq = nq or n_quarters()
        return cls(region, np.zeros((len(VOL_REGIMES) * 2, nq, len(HORIZONS), G, G, len(OUT)), dtype=np.float64),
                   np.zeros((len(VOL_REGIMES) * 2, nq, len(HORIZONS)), dtype=np.float64))

    def add(self, other: GridCounts) -> None:
        self.C += other.C
        self.N += other.N


def accumulate(gc: GridCounts, U: np.ndarray, D: np.ndarray, Z: np.ndarray, V: np.ndarray, vreg: np.ndarray,
               earn_in: np.ndarray, qidx: np.ndarray, use: np.ndarray) -> None:
    """Add one security's origins. V (n, len(HORIZONS)) = V(H) at origin; earn_in (n, len(HORIZONS)) bool;
    vreg (n,) 0/1/2 (-1 skipped); qidx (n,) quarter index; use (n,) bool origin mask."""
    nq = gc.N.shape[1]
    for h, H in enumerate(HORIZONS):
        sig = np.sqrt(V[:, h])
        ok = use & np.isfinite(sig) & (sig > 0) & (vreg >= 0) & (qidx >= 0) & (qidx < nq)
        ok &= np.isfinite(U[:, H - 1]) & np.isfinite(D[:, H - 1]) & np.isfinite(Z[:, H - 1])
        idx = np.nonzero(ok)[0]
        if not idx.size:
            continue
        s = sig[idx]
        thr = s[:, None] * GRID[None, :]
        tu = passage_times(U[idx, :H], thr)                     # (n, G)
        td = passage_times(D[idx, :H], thr)
        a_ = tu[:, :, None]
        b_ = td[:, None, :]
        up = (a_ < b_) & (a_ <= H)
        dn = (b_ < a_) & (b_ <= H)
        tie = (a_ == b_) & (a_ <= H)
        nei = ~(up | dn | tie)
        z = Z[idx, H - 1] / s
        pid = vreg[idx].astype(np.int64) * 2 + earn_in[idx, h].astype(np.int64)
        g = pid * nq + qidx[idx]
        order = np.argsort(g, kind="stable")
        gs = g[order]
        starts = np.r_[0, np.nonzero(np.diff(gs))[0] + 1]
        keys = gs[starts]
        cube = np.stack([up, dn, tie], axis=-1)[order].astype(np.float32)
        sums = np.add.reduceat(cube, starts, axis=0)
        mt = np.add.reduceat((nei * z[:, None, None])[order].astype(np.float64), starts, axis=0)
        cnt = np.diff(np.r_[starts, len(gs)])
        for kk, key in enumerate(keys):
            p_, q = divmod(int(key), nq)
            gc.C[p_, q, h, :, :, :3] += sums[kk]
            gc.C[p_, q, h, :, :, 3] += mt[kk]
            gc.N[p_, q, h] += cnt[kk]


# ------------------------------------------------------------------------------------------------ surface fit


def _pav(y: np.ndarray, w: np.ndarray, increasing: bool) -> np.ndarray:
    """Weighted isotonic regression (pool-adjacent-violators) of a short 1-D vector."""
    if not increasing:
        return -_pav(-y, w, True)
    vals: list[float] = []
    wts: list[float] = []
    lens: list[int] = []
    for yi, wi in zip(y, w, strict=True):
        vals.append(float(yi))
        wts.append(float(wi))
        lens.append(1)
        while len(vals) > 1 and vals[-2] > vals[-1]:
            v2, w2, l2 = vals.pop(), wts.pop(), lens.pop()
            v1, w1, l1 = vals.pop(), wts.pop(), lens.pop()
            ww = w1 + w2
            vals.append((v1 * w1 + v2 * w2) / ww if ww > 0 else (v1 + v2) / 2)
            wts.append(ww)
            lens.append(l1 + l2)
    return np.repeat(vals, lens)


def monotone_smooth(p: np.ndarray, dec_a: bool, passes: int = 3) -> np.ndarray:
    """Iterated 1-D isotonic fits on a (G, G) surface: along a non-increasing (dec_a) and along b non-decreasing,
    or the mirror (P(down))."""
    out = p.copy()
    w = np.ones(p.shape[0])
    for _ in range(passes):
        for j in range(out.shape[1]):
            out[:, j] = _pav(out[:, j], w, increasing=not dec_a)
        for i in range(out.shape[0]):
            out[i, :] = _pav(out[i, :], w, increasing=dec_a)
    return np.clip(out, 0.0, 1.0)


@dataclass
class Surface:
    """Fitted probabilities P[region_slot, vol, earn, h, ia, ib, k] for k in (up, down, tie, mtm_mean); region
    slots = REGIONS + ALL; n[region_slot, vol, earn, h] = origins behind each pool."""
    P: np.ndarray
    n: np.ndarray
    meta: dict[str, Any]

    def slot(self, region: str | None) -> tuple[int, bool]:
        r = (region or "").upper()
        if r in REGIONS:
            k = REGIONS.index(r)
            if self.n[k].sum() > 0:
                return k, False
        return len(REGIONS), True                       # ALL (fallback)

    def eval(self, region: str | None, vol: int, earn: bool, H: int, a: float, b: float) -> dict[str, Any]:
        if H not in HORIZONS:
            raise ValueError(f"horizon {H} not in {HORIZONS}")
        h = HORIZONS.index(H)
        k, fb = self.slot(region)
        v = int(vol) if vol in (0, 1, 2) else 1
        surf = self.P[k, v, int(bool(earn)), h]           # (G, G, 4)
        la, lb = math.log(max(a, 1e-9)), math.log(max(b, 1e-9))
        vals = bilinear(surf, la, lb)
        up, dn, tie = (float(np.clip(x, 0.0, 1.0)) for x in vals[:3])
        tot = up + dn + tie
        if tot > 1.0:
            up, dn, tie = up / tot, dn / tot, tie / tot
        return {"p_up": up, "p_down": dn, "p_tie": tie, "p_neither": max(0.0, 1.0 - up - dn - tie),
                "mtm_neither_z": float(vals[3]), "n_pool": int(self.n[k, v, int(bool(earn)), h]),
                "region_fallback": fb, "clamped": bool(a < GRID[0] or a > GRID[-1] or b < GRID[0] or b > GRID[-1])}

    def eval_many(self, region: np.ndarray, vol: np.ndarray, earn: np.ndarray, H: int, a: np.ndarray, b: np.ndarray
                  ) -> np.ndarray:
        """Vectorised eval -> (n, 4) [up, down, tie, mtm]; region as codes (0..len(REGIONS)-1, or len = ALL)."""
        h = HORIZONS.index(H)
        out = np.zeros((len(a), 4))
        la = np.log(np.clip(a, GRID[0], GRID[-1]))
        lb = np.log(np.clip(b, GRID[0], GRID[-1]))
        ia = np.clip(np.searchsorted(LOG_GRID, la, side="right") - 1, 0, G - 2)
        ib = np.clip(np.searchsorted(LOG_GRID, lb, side="right") - 1, 0, G - 2)
        fa = (la - LOG_GRID[ia]) / (LOG_GRID[ia + 1] - LOG_GRID[ia])
        fb = (lb - LOG_GRID[ib]) / (LOG_GRID[ib + 1] - LOG_GRID[ib])
        S = self.P[region, np.clip(vol, 0, 2), earn.astype(int), h]       # (n, G, G, 4)
        r = np.arange(len(a))
        out = ((1 - fa)[:, None] * (1 - fb)[:, None] * S[r, ia, ib] + fa[:, None] * (1 - fb)[:, None] * S[r, ia + 1, ib]
               + (1 - fa)[:, None] * fb[:, None] * S[r, ia, ib + 1] + fa[:, None] * fb[:, None] * S[r, ia + 1, ib + 1])
        p = np.clip(out[:, :3], 0, 1)
        tot = p.sum(1, keepdims=True)
        p = np.where(tot > 1, p / np.maximum(tot, 1e-12), p)
        out[:, :3] = p
        return out


def bilinear(surf: np.ndarray, la: float, lb: float) -> np.ndarray:
    la = min(max(la, LOG_GRID[0]), LOG_GRID[-1])
    lb = min(max(lb, LOG_GRID[0]), LOG_GRID[-1])
    ia = int(np.clip(np.searchsorted(LOG_GRID, la, side="right") - 1, 0, G - 2))
    ib = int(np.clip(np.searchsorted(LOG_GRID, lb, side="right") - 1, 0, G - 2))
    fa = (la - LOG_GRID[ia]) / (LOG_GRID[ia + 1] - LOG_GRID[ia])
    fb = (lb - LOG_GRID[ib]) / (LOG_GRID[ib + 1] - LOG_GRID[ib])
    return ((1 - fa) * (1 - fb) * surf[ia, ib] + fa * (1 - fb) * surf[ia + 1, ib] + (1 - fa) * fb * surf[ia, ib + 1]
            + fa * fb * surf[ia + 1, ib + 1])


def fit_surface(counts: dict[str, GridCounts] | GridCounts, q_lo: int, q_hi: int, m: float = SHRINK_M,
                smooth: bool = True) -> Surface:
    """Fit from origins in quarters [q_lo, q_hi) (exclusive end; the caller applies the purge)."""
    if isinstance(counts, GridCounts):
        counts = {counts.region: counts}
    nR, nV = len(REGIONS), len(VOL_REGIMES)
    C = np.zeros((nR, nV, 2, len(HORIZONS), G, G, len(OUT)))
    N = np.zeros((nR, nV, 2, len(HORIZONS)))
    for rg, gc in counts.items():
        k = region_code(rg)
        C[k] += gc.C[:, q_lo:q_hi].sum(1).reshape(nV, 2, len(HORIZONS), G, G, len(OUT))
        N[k] += gc.N[:, q_lo:q_hi].sum(1).reshape(nV, 2, len(HORIZONS))
    # hierarchy: root (earn) <- region (earn) <- pool (region, vol, earn); ALL slot: (vol, earn) <- root
    C_root, N_root = C.sum((0, 1)), N.sum((0, 1))                    # (2, h, G, G, 4), (2, h)
    C_reg, N_reg = C.sum(1), N.sum(1)                                # (R, 2, h, ...)
    C_allv, N_allv = C.sum(0), N.sum(0)                              # (V, 2, h, ...)

    def rate(c: np.ndarray, n: np.ndarray) -> np.ndarray:
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(n[..., None, None, None] > 0, c[..., :3] / np.maximum(n, 1)[..., None, None, None], np.nan)

    def nei(c: np.ndarray, n: np.ndarray) -> np.ndarray:
        return np.maximum(n[..., None, None] - c[..., 0] - c[..., 1] - c[..., 2], 0.0)

    def shrink(c: np.ndarray, n: np.ndarray, prior: np.ndarray) -> np.ndarray:
        nn = n[..., None, None, None]
        return (c[..., :3] + m * prior) / np.maximum(nn + m, 1e-12)

    p_root = np.nan_to_num(rate(C_root, N_root), nan=0.0)
    mt_root = np.where(nei(C_root, N_root) > 0, C_root[..., 3] / np.maximum(nei(C_root, N_root), 1), 0.0)
    p_reg = shrink(C_reg, N_reg, p_root[None])
    nr = nei(C_reg, N_reg)
    mt_reg = (C_reg[..., 3] + m * mt_root[None]) / np.maximum(nr + m, 1e-12)
    p_pool = shrink(C, N, p_reg[:, None])
    npool = nei(C, N)
    mt_pool = (C[..., 3] + m * mt_reg[:, None]) / np.maximum(npool + m, 1e-12)
    p_all = shrink(C_allv, N_allv, p_root[None])
    na = nei(C_allv, N_allv)
    mt_all = (C_allv[..., 3] + m * mt_root[None]) / np.maximum(na + m, 1e-12)
    P = np.zeros((nR + 1, nV, 2, len(HORIZONS), G, G, 4))
    P[:nR, ..., :3] = p_pool
    P[:nR, ..., 3] = mt_pool
    P[nR, ..., :3] = p_all
    P[nR, ..., 3] = mt_all
    n_out = np.zeros((nR + 1, nV, 2, len(HORIZONS)))
    n_out[:nR] = N
    n_out[nR] = N_allv
    if smooth:
        for idx in np.ndindex(*P.shape[:4]):
            P[idx][..., 0] = monotone_smooth(P[idx][..., 0], dec_a=True)
            P[idx][..., 1] = monotone_smooth(P[idx][..., 1], dec_a=False)
    return Surface(P=P, n=n_out, meta={"q_lo": q_lo, "q_hi": q_hi, "m": m, "smooth": smooth})


def surface_rows(s: Surface, fit_date: Any) -> list[dict[str, Any]]:
    """Lake rows (tech_prob_surfaces): one per region slot x vol x earn x horizon, flattened (G x G) lists."""
    rows = []
    slots = [*REGIONS, ALL_REGION]
    for k, r in enumerate(slots):
        for v, vn in enumerate(VOL_REGIMES):
            for e in (0, 1):
                for h, H in enumerate(HORIZONS):
                    S = s.P[k, v, e, h]
                    rows.append({"fit_date": fit_date, "region": r, "vol_regime": vn, "earn_window": bool(e), "horizon": H,
                                 "n_origins": int(s.n[k, v, e, h]), "grid": GRID.tolist(),
                                 "p_up": S[..., 0].ravel().tolist(), "p_down": S[..., 1].ravel().tolist(),
                                 "p_tie": S[..., 2].ravel().tolist(), "mtm_neither_z": S[..., 3].ravel().tolist()})
    return rows


def surface_from_rows(rows: list[dict[str, Any]]) -> Surface | None:
    if not rows:
        return None
    slots = [*REGIONS, ALL_REGION]
    P = np.zeros((len(slots), len(VOL_REGIMES), 2, len(HORIZONS), G, G, 4))
    n = np.zeros((len(slots), len(VOL_REGIMES), 2, len(HORIZONS)))
    for r in rows:
        if r["region"] not in slots or r["vol_regime"] not in VOL_REGIMES or int(r["horizon"]) not in HORIZONS:
            continue
        k, v, e, h = slots.index(r["region"]), VOL_REGIMES.index(r["vol_regime"]), int(bool(r["earn_window"])), HORIZONS.index(int(r["horizon"]))
        for j, col in enumerate(("p_up", "p_down", "p_tie", "mtm_neither_z")):
            P[k, v, e, h, :, :, j] = np.asarray(r[col], dtype=np.float64).reshape(G, G)
        n[k, v, e, h] = r.get("n_origins") or 0
    return Surface(P=P, n=n, meta={"fit_date": str(rows[0].get("fit_date"))})


# ------------------------------------------------------------------------------------------------ baselines


def bm_two_barrier(a: np.ndarray | float, b: np.ndarray | float, t: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Driftless Brownian motion (unit variance per unit time) started at 0 with barriers +a, -b: (P(up first by t),
    P(down first by t)) under continuous monitoring. Spectral series on the interval (0, L), L = a + b, start x = b:
    P(exit at L by t) = x/L - sum_n 2(-1)^{n+1}/(n pi) sin(n pi x/L) exp(-n^2 pi^2 t / (2 L^2))."""
    a = np.atleast_1d(np.asarray(a, dtype=np.float64))
    b = np.atleast_1d(np.asarray(b, dtype=np.float64))
    a, b = np.broadcast_arrays(a, b)
    L = a + b
    nmax = int(np.clip(np.ceil(np.nanmax(L) * 12.0 / max(math.sqrt(t), 1e-3)) + 60, 60, 6000))
    k = np.arange(1, nmax + 1, dtype=np.float64)

    def p_exit(x: np.ndarray) -> np.ndarray:
        out = np.empty(x.shape)
        flat_x, flat_L, flat_o = x.ravel(), L.ravel(), out.ravel()
        for s in range(0, flat_x.size, 2048):
            xx = flat_x[s:s + 2048, None]
            LL = flat_L[s:s + 2048, None]
            terms = 2.0 * ((-1.0) ** (k + 1)) / (k * math.pi) * np.sin(k * math.pi * xx / LL) * np.exp(-(k ** 2) * math.pi ** 2 * t / (2.0 * LL ** 2))
            flat_o[s:s + 2048] = (xx[:, 0] / LL[:, 0]) - terms.sum(1)
        return np.clip(out, 0.0, 1.0)

    return p_exit(b), p_exit(a)


def naive_rr(rr: np.ndarray | float) -> np.ndarray:
    """'R:R only' rule: P(win) = risk / (reward + risk) = 1 / (1 + R:R), no horizon, no 'neither'."""
    rr = np.asarray(rr, dtype=np.float64)
    return 1.0 / (1.0 + rr)


# ------------------------------------------------------------------------------------------------ setup evaluation


def setup_horizon(x_target: float, V_of: Any) -> int:
    """The setup's own horizon: the first H in HORIZONS at which the target is within one sigma_H
    (|log(T1 / close)| <= sqrt(V(H))), else the longest horizon."""
    for H in HORIZONS:
        v = V_of(H)
        if v and v > 0 and abs(x_target) <= math.sqrt(v):
            return H
    return HORIZONS[-1]


def round_trip_cost(adv_usd: float | None, sigma_d: float | None, short: bool, hold_days: float,
                    costs: dict[str, Any] | None = None, position_usd: float = 2.0e5) -> dict[str, float]:
    """Round-trip cost as a fraction of the position from the Backtest Lab cost model (app.backtest._engine.CostModel):
    2 x (commission + ADV-bucket half spread) + 2 x square-root impact, + borrow for shorts over the holding period."""
    from app.backtest._engine import CostModel  # noqa: PLC0415

    cm = CostModel.from_spec(costs or {})
    adv = np.array([adv_usd if adv_usd and adv_usd > 0 else np.nan])
    hs = float(cm.half_spread_bps(adv)[0])
    sig = sigma_d if sigma_d and sigma_d > 0 else 0.02
    part = position_usd / float(adv[0]) if np.isfinite(adv[0]) else 1.0
    impact = cm.impact_coef * sig * math.sqrt(max(part, 0.0))
    one_way = (cm.commission_bps + hs) / 1e4 + impact
    borrow = float(cm.borrow_rate_daily(adv)[0]) * hold_days if short else 0.0
    return {"round_trip": 2.0 * one_way + borrow, "half_spread_bps": hs, "commission_bps": cm.commission_bps,
            "impact_one_way": impact, "borrow": borrow}


def round_trip_cost_vec(adv_usd: np.ndarray, sigma_d: np.ndarray, short: np.ndarray, hold_days: np.ndarray,
                        costs: dict[str, Any] | None = None, position_usd: float = 2.0e5) -> np.ndarray:
    """Vectorised round_trip_cost (fraction of the position)."""
    from app.backtest._engine import CostModel  # noqa: PLC0415

    cm = CostModel.from_spec(costs or {})
    adv = np.where(np.isfinite(adv_usd) & (adv_usd > 0), adv_usd, np.nan)
    hs = cm.half_spread_bps(adv)
    sig = np.where(np.isfinite(sigma_d) & (sigma_d > 0), sigma_d, 0.02)
    part = np.where(np.isfinite(adv), position_usd / np.where(np.isfinite(adv), adv, 1.0), 1.0)
    impact = cm.impact_coef * sig * np.sqrt(np.clip(part, 0.0, None))
    one_way = (cm.commission_bps + hs) / 1e4 + impact
    borrow = np.where(short, cm.borrow_rate_daily(adv) * hold_days, 0.0)
    return 2.0 * one_way + borrow


def expected_r(p_win: float, p_loss: float, rr: float, cost_r: float, p_neither: float = 0.0,
               mtm_neither_r: float | None = None) -> dict[str, float | None]:
    """E[R] = P(win) R:R - P(loss) - costs (R units); the marked-to-market variant adds P(neither) E[R | neither]."""
    e = p_win * rr - p_loss - cost_r
    mtm = None if mtm_neither_r is None else e + p_neither * mtm_neither_r
    return {"expected_r": e, "expected_r_mtm": mtm, "break_even_p": 1.0 / (1.0 + rr) if rr > 0 else None,
            "break_even_p_costs": (1.0 + cost_r) / (1.0 + rr) if rr > 0 else None}
