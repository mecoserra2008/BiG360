"""Scores, conformal quantiles, projections and set-size metrics (paper §3, eq. (1)-(4); App. C.2 / D.2).

Covariance sequences are carried by `Cov`: either dense (T, N, N) or diagonal + low rank, Sigma_t = diag(d_t) + L_t L_t'
with d (T, N) and L (T, N, r). The low-rank form uses the Woodbury identity and the matrix-determinant lemma, so the
inverse / log-determinant cost is O(N r^2 + r^3) per step instead of O(N^3):

    Sigma^-1        = D^-1 - D^-1 L M^-1 L' D^-1,   M = I_r + L' D^-1 L
    log det Sigma   = sum log d + log det M

All functions are batched over the leading time axis and work in float64.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EPS_D = 1e-4          # paper App. C.2 diagonal floor (in the filter's working units)


# ------------------------------------------------------------------------------------------------ covariance carrier


@dataclass
class Cov:
    """A sequence of T covariance matrices (dense or diagonal + low rank). A single matrix is T = 1."""
    dense: np.ndarray | None = None      # (T, N, N)
    d: np.ndarray | None = None          # (T, N)
    L: np.ndarray | None = None          # (T, N, r)

    def __post_init__(self) -> None:
        if self.dense is not None:
            self.dense = np.asarray(self.dense, dtype=np.float64)
            if self.dense.ndim == 2:
                self.dense = self.dense[None]
        else:
            if self.d is None:
                raise ValueError("Cov needs dense or d")
            self.d = np.asarray(self.d, dtype=np.float64)
            if self.d.ndim == 1:
                self.d = self.d[None]
            if self.L is None:
                self.L = np.zeros(self.d.shape + (0,))
            self.L = np.asarray(self.L, dtype=np.float64)
            if self.L.ndim == 2:
                self.L = self.L[None]

    @property
    def lowrank(self) -> bool:
        return self.dense is None

    @property
    def T(self) -> int:  # noqa: N802
        return int(self.dense.shape[0] if self.dense is not None else self.d.shape[0])  # type: ignore[union-attr]

    @property
    def N(self) -> int:  # noqa: N802
        return int(self.dense.shape[1] if self.dense is not None else self.d.shape[1])  # type: ignore[union-attr]

    def to_dense(self) -> np.ndarray:
        if self.dense is not None:
            return self.dense
        assert self.d is not None and self.L is not None
        out = np.einsum("tnr,tmr->tnm", self.L, self.L)
        idx = np.arange(self.N)
        out[:, idx, idx] += self.d
        return out

    def diag(self) -> np.ndarray:
        """(T, N) diagonal of Sigma_t."""
        if self.dense is not None:
            return np.diagonal(self.dense, axis1=1, axis2=2).copy()
        assert self.d is not None and self.L is not None
        return self.d + (self.L ** 2).sum(-1)

    def take(self, idx: np.ndarray | slice) -> Cov:
        if self.dense is not None:
            return Cov(dense=self.dense[idx])
        return Cov(d=self.d[idx], L=self.L[idx])  # type: ignore[index]

    def scale(self, s: np.ndarray | float) -> Cov:
        """Sigma_t * s_t (s scalar or (T,))."""
        s = np.asarray(s, dtype=np.float64)
        if self.dense is not None:
            return Cov(dense=self.dense * (s[:, None, None] if s.ndim else s))
        assert self.d is not None and self.L is not None
        return Cov(d=self.d * (s[:, None] if s.ndim else s), L=self.L * (np.sqrt(s)[:, None, None] if s.ndim else math.sqrt(s)))


def _lr_parts(cov: Cov) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Dinv (T,N), DL = D^-1 L (T,N,r), Minv (T,r,r)."""
    assert cov.d is not None and cov.L is not None
    dinv = 1.0 / cov.d
    DL = cov.L * dinv[..., None]
    r = cov.L.shape[-1]
    M = np.eye(r)[None] + np.einsum("tnr,tns->trs", cov.L, DL)
    Minv = np.linalg.inv(M) if r else np.zeros((cov.T, 0, 0))
    return dinv, DL, Minv


def _chol_dense(S: np.ndarray, jitter: float = 1e-12) -> np.ndarray:
    try:
        return np.linalg.cholesky(S)
    except np.linalg.LinAlgError:
        n = S.shape[-1]
        scale = np.maximum(np.trace(S, axis1=-2, axis2=-1) / n, 1e-300)
        for k in range(8):
            try:
                return np.linalg.cholesky(S + (jitter * 10 ** k) * scale[..., None, None] * np.eye(n))
            except np.linalg.LinAlgError:
                continue
        raise


# ------------------------------------------------------------------------------------------------ scores


def solve(cov: Cov, r: np.ndarray) -> np.ndarray:
    """Sigma_t^-1 r_t, (T, N)."""
    r = np.atleast_2d(np.asarray(r, dtype=np.float64))
    if cov.dense is not None:
        S = np.broadcast_to(cov.dense, (r.shape[0],) + cov.dense.shape[1:]) if cov.T == 1 else cov.dense
        return np.linalg.solve(S, r[..., None])[..., 0]
    dinv, DL, Minv = _lr_parts(cov)
    u = np.einsum("tnr,tn->tr", cov.L, dinv * r)  # type: ignore[arg-type]
    return dinv * r - np.einsum("tnr,tr->tn", DL, np.einsum("trs,ts->tr", Minv, u))


def mahalanobis(r: np.ndarray, cov: Cov) -> np.ndarray:
    """Squared Mahalanobis score R_t = r_t' Sigma_t^-1 r_t (eq. 1), (T,). Rows with non-finite r give NaN."""
    r = np.atleast_2d(np.asarray(r, dtype=np.float64))
    bad = ~np.isfinite(r).all(axis=1)
    rr = np.where(np.isfinite(r), r, 0.0)
    if cov.dense is not None:
        Lc = _chol_dense(cov.dense)
        if cov.T == 1:
            Lc = np.broadcast_to(Lc, (rr.shape[0],) + Lc.shape[1:])
        z = np.linalg.solve(Lc, rr[..., None])[..., 0]
        out = (z * z).sum(-1)
    else:
        dinv, DL, Minv = _lr_parts(cov)
        u = np.einsum("tnr,tn->tr", cov.L, dinv * rr)  # type: ignore[arg-type]
        out = (rr * rr * dinv).sum(-1) - np.einsum("tr,trs,ts->t", u, Minv, u)
    out = np.maximum(out, 0.0)
    out[bad] = np.nan
    return out


def logdet(cov: Cov) -> np.ndarray:
    """log det Sigma_t, (T,)."""
    if cov.dense is not None:
        sign, ld = np.linalg.slogdet(cov.dense)
        return np.where(sign > 0, ld, np.nan)
    assert cov.d is not None and cov.L is not None
    r = cov.L.shape[-1]
    if r == 0:
        return np.log(cov.d).sum(-1)
    M = np.eye(r)[None] + np.einsum("tnr,tns->trs", cov.L, cov.L / cov.d[..., None])
    return np.log(cov.d).sum(-1) + np.linalg.slogdet(M)[1]


def gaussian_nll(r: np.ndarray, cov: Cov) -> np.ndarray:
    """Per-step Gaussian negative log-likelihood (without the constant N/2 log 2 pi), (T,)."""
    return 0.5 * (mahalanobis(r, cov) + logdet(cov))


def inv_diag(cov: Cov) -> np.ndarray:
    """diag(Sigma_t^-1), (T, N)."""
    if cov.dense is not None:
        return np.diagonal(np.linalg.inv(cov.dense), axis1=1, axis2=2).copy()
    dinv, DL, Minv = _lr_parts(cov)
    return dinv - np.einsum("tnr,trs,tns->tn", DL, Minv, DL)


def decompose(r: np.ndarray, cov: Cov) -> dict[str, np.ndarray]:
    """Per-coordinate attribution of R_t (documented, exact):

    contrib_i = r_i (Sigma^-1 r)_i       sums exactly to R_t (Euler / gradient split: R = sum_i r_i dR/dr_i / 2);
                                          negative when coordinate i offsets the others (hedge-like)
    z_marg_i  = r_i / sqrt(Sigma_ii)      the coordinate's own standardised move
    z_cond_i  = (Sigma^-1 r)_i / sqrt((Sigma^-1)_ii)
                                          standardised residual of r_i given all other coordinates under the predictive
                                          Gaussian (= (r_i - E[r_i | r_-i]) / sd(r_i | r_-i)): "this name broke away"
    """
    r = np.atleast_2d(np.asarray(r, dtype=np.float64))
    w = solve(cov, r)
    idg = inv_diag(cov)
    dg = cov.diag()
    return {"contrib": r * w, "z_marg": r / np.sqrt(dg), "z_cond": w / np.sqrt(np.maximum(idg, 1e-300))}


# ------------------------------------------------------------------------------------------------ conformal quantile


def conformal_index(n: int, alpha: float) -> int:
    """1-based order-statistic index ceil((n + 1)(1 - alpha)) of split conformal (> n means an unbounded set)."""
    return int(math.ceil((n + 1) * (1.0 - alpha) - 1e-12))


def conformal_quantile(scores: np.ndarray, alpha: float | np.ndarray) -> float | np.ndarray:
    """q_hat_{1-alpha} = R_(ceil((n+1)(1-alpha))) over the finite calibration scores (inf when the index exceeds n,
    0 when alpha >= 1). Vectorised over alpha."""
    s = np.sort(np.asarray(scores, dtype=np.float64)[np.isfinite(scores)])
    return quantile_from_sorted(s, alpha)


def quantile_from_sorted(s: np.ndarray, alpha: float | np.ndarray) -> float | np.ndarray:
    n = len(s)
    a = np.atleast_1d(np.asarray(alpha, dtype=np.float64))
    k = np.ceil((n + 1) * (1.0 - a) - 1e-12).astype(np.int64)
    out = np.full(a.shape, np.inf)
    ok = (k >= 1) & (k <= n)
    out[ok] = s[k[ok] - 1]
    out[k < 1] = 0.0
    return float(out[0]) if np.ndim(alpha) == 0 else out


# ------------------------------------------------------------------------------------------------ projections / size


def quad_form(cov: Cov, A: np.ndarray) -> np.ndarray:
    """a' Sigma_t a for directions A (K, N) or (T, K, N); returns (T, K)."""
    A = np.asarray(A, dtype=np.float64)
    if A.ndim == 1:
        A = A[None]
    if cov.dense is not None:
        if A.ndim == 2:
            return np.einsum("kn,tnm,km->tk", A, cov.dense, A)
        return np.einsum("tkn,tnm,tkm->tk", A, cov.dense, A)
    assert cov.d is not None and cov.L is not None
    if A.ndim == 2:
        return np.einsum("kn,tn->tk", A * A, cov.d) + (np.einsum("kn,tnr->tkr", A, cov.L) ** 2).sum(-1)
    return np.einsum("tkn,tn->tk", A * A, cov.d) + (np.einsum("tkn,tnr->tkr", A, cov.L) ** 2).sum(-1)


def project_band(mu: np.ndarray, cov: Cov, q: np.ndarray | float, A: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Closed-form projection of the ellipsoid onto a'Y: a'mu +- sqrt(q a'Sigma a) (support function). (T, K) each."""
    mu = np.atleast_2d(np.asarray(mu, dtype=np.float64))
    A = np.asarray(A, dtype=np.float64)
    if A.ndim == 1:
        A = A[None]
    c = np.einsum("kn,tn->tk", A, mu) if A.ndim == 2 else np.einsum("tkn,tn->tk", A, mu)
    q = np.asarray(q, dtype=np.float64)
    hw = np.sqrt(np.maximum(q if q.ndim == 0 else q[:, None], 0.0) * quad_form(cov, A))
    return c - hw, c + hw


def coord_halfwidth(cov: Cov, q: np.ndarray | float) -> np.ndarray:
    """Exact coordinate half-widths sqrt(q Sigma_ii) of the ellipsoid, (T, N)."""
    q = np.asarray(q, dtype=np.float64)
    return np.sqrt(np.maximum(q if q.ndim == 0 else q[:, None], 0.0) * cov.diag())


def w_proj(halfwidth: np.ndarray) -> float:
    """Mean coordinate-projection length (eq. 3): mean over steps and coordinates of u - l = 2 h."""
    h = np.asarray(halfwidth, dtype=np.float64)
    return float(np.nanmean(2.0 * h[np.isfinite(h).all(axis=-1)])) if h.size else float("nan")


def log_unit_ball(N: int) -> float:
    return 0.5 * N * math.log(math.pi) - math.lgamma(0.5 * N + 1.0)


def log_volume(q: np.ndarray | float, ld: np.ndarray, N: int) -> np.ndarray:
    """Normalised log-volume (eq. 4): (1/N) log v_N + 1/2 log q + (1/(2N)) log det Sigma (q = squared radius)."""
    q = np.asarray(q, dtype=np.float64)
    with np.errstate(divide="ignore"):
        return log_unit_ball(N) / N + 0.5 * np.log(q) + ld / (2.0 * N)


def box_log_volume(halfwidth: np.ndarray) -> np.ndarray:
    """Normalised log-volume of an axis-aligned box (1/N) sum log(2 h_i), comparable with log_volume."""
    h = np.asarray(halfwidth, dtype=np.float64)
    with np.errstate(divide="ignore"):
        return np.log(2.0 * h).mean(-1)


# ------------------------------------------------------------------------------------------------ conversions


def to_lowrank(S: np.ndarray, r: int, floor: float = 0.0) -> Cov:
    """Dense (T, N, N) -> diag + rank-r (top-r eigenpairs; d = diag(S - L L') so the diagonal is preserved exactly,
    floored at `floor`)."""
    S = np.asarray(S, dtype=np.float64)
    if S.ndim == 2:
        S = S[None]
    w, V = np.linalg.eigh(S)                       # ascending
    r = min(r, S.shape[-1])
    wr = np.clip(w[..., -r:], 0.0, None) if r else w[..., :0]
    L = V[..., -r:] * np.sqrt(wr)[:, None, :] if r else np.zeros(S.shape[:2] + (0,))
    dg = np.diagonal(S, axis1=1, axis2=2)
    d = np.maximum(dg - (L ** 2).sum(-1), np.maximum(floor, 1e-6 * np.maximum(dg, 1e-300)))
    return Cov(d=d, L=L)


def nearest_pd(S: np.ndarray, rel_floor: float = 1e-8) -> np.ndarray:
    """Symmetrise and clip eigenvalues at rel_floor x mean eigenvalue (batched)."""
    S = 0.5 * (S + np.swapaxes(S, -1, -2))
    w, V = np.linalg.eigh(S)
    fl = rel_floor * np.maximum(w.mean(-1, keepdims=True), 1e-300)
    w = np.maximum(w, fl)
    return np.einsum("...ij,...j,...kj->...ik", V, w, V)
