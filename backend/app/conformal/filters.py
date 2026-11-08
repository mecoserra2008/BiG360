"""Predictive filters for filtered conformal prediction (paper §3-4, App. C-D). numpy only (the learned GCN-GRU is
in app.conformal.learned so that torch is imported lazily).

Alignment convention (all filters): inputs X (T, N) are the observations, row t known at the close of t. emit(X)
returns (mu, Cov) whose row t is the one-step predictive law of X[t + 1] computed from X[: t + 1] only, i.e.
(Y_hat_{t+1|t}, Sigma_hat_{t+1|t}). Scores pair row t with X[t + 1]. Nothing in row t may depend on X[t + 1:]
(app.conformal.leakage tests this for every filter). fit() sees only the pretraining block and the filter is frozen
afterwards (Algorithm 1).

Rows with a NaN observation are "no news": the filter state is carried (EWMA / GARCH treat the coordinate as
unobserved for that step).

Filters
    EWMAFilter      RiskMetrics EWMA covariance (lambda 0.94); optional slower correlation lambda, correlation
                    shrinkage to I, optional lambda choice by pretrain Gaussian NLL; mean 0 by default (return means
                    are noise), 'ewma' / 'static' means available (needed e.g. for next-bar high / low vectors)
    StaticLWFilter  Ledoit-Wolf shrinkage covariance of the pretrain block (constant Sigma)
    FactorFilter    PCA factor covariance B B' + diag(psi) of the pretrain block (rank r, default 4)
    GarchFilter     GARCH(1,1) per coordinate (variance targeting, grid MLE on the pretrain block) with constant
                    (CCC), identity ('diag') or EWMA (DCC-lite) correlation of the standardised residuals
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.conformal.scores import Cov, gaussian_nll, nearest_pd, to_lowrank

RISKMETRICS_LAMBDA = 0.94


class Filter:
    name = "filter"

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> Filter:  # noqa: ARG002 - interface
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        raise NotImplementedError

    def params(self) -> dict[str, Any]:
        return {}


def _nan_cov(X: np.ndarray, demean: bool) -> np.ndarray:
    ok = np.isfinite(X).all(axis=1)
    Z = X[ok]
    if demean:
        Z = Z - Z.mean(0)
    n = max(Z.shape[0] - (1 if demean else 0), 1)
    return Z.T @ Z / n


def _shrink_corr(S: np.ndarray, s: float) -> np.ndarray:
    if s <= 0:
        return S
    sd = np.sqrt(np.maximum(np.diagonal(S, axis1=-2, axis2=-1), 1e-300))
    C = S / (sd[..., :, None] * sd[..., None, :])
    n = S.shape[-1]
    C = (1.0 - s) * C + s * np.eye(n)
    return C * (sd[..., :, None] * sd[..., None, :])


# ------------------------------------------------------------------------------------------------ EWMA


@dataclass
class EWMAFilter(Filter):
    lam: float = RISKMETRICS_LAMBDA
    lam_corr: float | None = None        # slower lambda for correlations (None = same as lam: plain EWMA covariance)
    shrink: float = 0.0                  # correlation shrinkage toward I
    mean: str = "zero"                   # zero | ewma | static
    lam_mean: float = 0.97
    choose_lambda: bool = False          # pick lam on the pretrain block by Gaussian NLL
    lam_grid: tuple[float, ...] = (0.90, 0.94, 0.97, 0.985)
    rank: int | None = None              # emit diag + rank-r (top-r eigen) instead of dense
    name: str = "ewma"
    S0: np.ndarray | None = field(default=None, repr=False)
    m0: np.ndarray | None = field(default=None, repr=False)

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> EWMAFilter:
        X = np.asarray(X, dtype=np.float64)
        self.m0 = np.nanmean(X, axis=0) if self.mean != "zero" else np.zeros(X.shape[1])
        self.S0 = _nan_cov(X - self.m0, demean=False) + 1e-12 * np.eye(X.shape[1])
        if self.choose_lambda and X.shape[0] > 120:
            best = None
            for lam in self.lam_grid:
                f = EWMAFilter(lam=lam, lam_corr=self.lam_corr, shrink=self.shrink, mean=self.mean,
                               lam_mean=self.lam_mean, S0=self.S0, m0=self.m0)
                mu, cov = f.emit(X[:-1])
                r = X[1:] - mu
                ok = np.isfinite(r).all(axis=1)
                ok[: min(50, ok.size)] = False
                nll = float(np.nanmean(gaussian_nll(r[ok], cov.take(ok))))
                if best is None or nll < best[0]:
                    best = (nll, lam)
            self.lam = best[1] if best else self.lam
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        X = np.asarray(X, dtype=np.float64)
        T, N = X.shape
        if self.S0 is None:
            self.fit(X[: max(20, min(T, 60))])
        S = self.S0.copy()  # type: ignore[union-attr]
        Sc = S.copy()
        m = self.m0.copy()  # type: ignore[union-attr]
        lam, lc = self.lam, (self.lam_corr or self.lam)
        mus = np.empty((T, N))
        out = np.empty((T, N, N))
        for t in range(T):
            x = X[t]
            ok = np.isfinite(x)
            if ok.all():
                e = x - m
                S = lam * S + (1 - lam) * np.outer(e, e)
                if self.lam_corr is not None:
                    Sc = lc * Sc + (1 - lc) * np.outer(e, e)
                if self.mean == "ewma":
                    m = self.lam_mean * m + (1 - self.lam_mean) * x
            elif ok.any():  # partial row: update observed block only
                e = np.where(ok, x - m, 0.0)
                upd = np.outer(ok, ok)
                S = np.where(upd, lam * S + (1 - lam) * np.outer(e, e), S)
                if self.lam_corr is not None:
                    Sc = np.where(upd, lc * Sc + (1 - lc) * np.outer(e, e), Sc)
                if self.mean == "ewma":
                    m = np.where(ok, self.lam_mean * m + (1 - self.lam_mean) * np.where(ok, x, 0.0), m)
            if self.lam_corr is not None:
                sd = np.sqrt(np.maximum(np.diag(S), 1e-300))
                sdc = np.sqrt(np.maximum(np.diag(Sc), 1e-300))
                C = Sc / np.outer(sdc, sdc)
                out[t] = C * np.outer(sd, sd)
            else:
                out[t] = S
            mus[t] = m
        if self.shrink > 0:
            out = _shrink_corr(out, self.shrink)
        if self.rank is not None:
            return mus, to_lowrank(out, self.rank)
        return mus, Cov(dense=out)

    def params(self) -> dict[str, Any]:
        return {"lam": self.lam, "lam_corr": self.lam_corr, "shrink": self.shrink, "mean": self.mean, "rank": self.rank}


# ------------------------------------------------------------------------------------------------ static


@dataclass
class StaticLWFilter(Filter):
    demean: bool = False
    name: str = "static_lw"
    S: np.ndarray | None = field(default=None, repr=False)
    m: np.ndarray | None = field(default=None, repr=False)
    shrinkage: float | None = None

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> StaticLWFilter:
        from sklearn.covariance import ledoit_wolf  # noqa: PLC0415

        X = np.asarray(X, dtype=np.float64)
        Z = X[np.isfinite(X).all(axis=1)]
        self.m = Z.mean(0) if self.demean else np.zeros(X.shape[1])
        S, self.shrinkage = ledoit_wolf(Z - self.m, assume_centered=True)
        self.S = nearest_pd(S)
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        T = np.asarray(X).shape[0]
        return np.broadcast_to(self.m, (T, len(self.m))).copy(), Cov(dense=np.broadcast_to(self.S, (T,) + self.S.shape).copy())  # type: ignore[arg-type]

    def params(self) -> dict[str, Any]:
        return {"shrinkage": self.shrinkage, "demean": self.demean}


@dataclass
class FactorFilter(Filter):
    r: int = 4
    vol_scale: bool = False              # rescale the static factor shape by EWMA vols (time-varying variant)
    lam: float = RISKMETRICS_LAMBDA
    name: str = "factor"
    d: np.ndarray | None = field(default=None, repr=False)
    L: np.ndarray | None = field(default=None, repr=False)
    sd0: np.ndarray | None = field(default=None, repr=False)

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> FactorFilter:
        X = np.asarray(X, dtype=np.float64)
        S = _nan_cov(X, demean=False)
        r = min(self.r, max(S.shape[0] - 1, 0))
        c = to_lowrank(S, r)
        self.d, self.L = c.d[0], c.L[0]  # type: ignore[index]
        self.sd0 = np.sqrt(np.diag(S))
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        X = np.asarray(X, dtype=np.float64)
        T, N = X.shape
        d = np.broadcast_to(self.d, (T, N)).copy()  # type: ignore[arg-type]
        L = np.broadcast_to(self.L, (T,) + self.L.shape).copy()  # type: ignore[union-attr]
        if self.vol_scale:
            v = self.sd0 ** 2  # type: ignore[operator]
            sc = np.empty((T, N))
            for t in range(T):
                x = X[t]
                ok = np.isfinite(x)
                v = np.where(ok, self.lam * v + (1 - self.lam) * np.where(ok, x, 0.0) ** 2, v)
                sc[t] = np.sqrt(v) / self.sd0
            d = d * sc ** 2
            L = L * sc[..., None]
        return np.zeros((T, N)), Cov(d=d, L=L)

    def params(self) -> dict[str, Any]:
        return {"r": self.r, "vol_scale": self.vol_scale}


# ------------------------------------------------------------------------------------------------ GARCH


GARCH_A = np.array([0.02, 0.03, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12, 0.15, 0.20])
GARCH_B = np.array([0.70, 0.75, 0.80, 0.84, 0.87, 0.90, 0.92, 0.94, 0.96, 0.98])


def garch_grid_fit(X: np.ndarray, burn: int = 20) -> dict[str, np.ndarray]:
    """Vectorised GARCH(1,1) MLE with variance targeting over a (a, b) grid, a + b <= 0.995, for M series at once.
    X: (T, M) innovations (NaN allowed). Returns {'a', 'b', 'vbar'} arrays of length M."""
    X = np.asarray(X, dtype=np.float64)
    T, M = X.shape
    vbar = np.nanmean(X * X, axis=0)
    vbar = np.where(np.isfinite(vbar) & (vbar > 0), vbar, 1e-8)
    grid = [(a, b) for a in GARCH_A for b in GARCH_B if a + b <= 0.995]
    A = np.array([g[0] for g in grid])[:, None]
    B = np.array([g[1] for g in grid])[:, None]
    om = vbar[None, :] * (1.0 - A - B)
    s2 = np.broadcast_to(vbar, (len(grid), M)).copy()
    nll = np.zeros((len(grid), M))
    for t in range(T):
        x2 = X[t] * X[t]
        ok = np.isfinite(x2)
        if t >= burn:
            nll += np.where(ok, np.log(s2) + np.where(ok, x2, 0.0) / s2, 0.0)
        s2 = om + A * np.where(ok, x2, s2) + B * s2
    k = np.argmin(nll, axis=0)
    return {"a": A[k, 0], "b": B[k, 0], "vbar": vbar}


def garch_filter(X: np.ndarray, a: np.ndarray, b: np.ndarray, vbar: np.ndarray, s0: np.ndarray | None = None) -> np.ndarray:
    """One-step variance forecasts sigma^2_{t+1|t} (row t uses X[: t + 1]), (T, M)."""
    X = np.asarray(X, dtype=np.float64)
    T, M = X.shape
    s2 = (vbar if s0 is None else s0).astype(np.float64).copy()
    om = vbar * (1.0 - a - b)
    out = np.empty((T, M))
    for t in range(T):
        x2 = X[t] * X[t]
        ok = np.isfinite(x2)
        s2 = om + a * np.where(ok, x2, s2) + b * s2
        out[t] = s2
    return out


def garch_term(s2_next: np.ndarray, a: np.ndarray, b: np.ndarray, vbar: np.ndarray, horizons: list[int]) -> np.ndarray:
    """Cumulative variance V(h) = sum_{k=1..h} E[sigma^2_{t+k} | t] for each horizon, (..., len(horizons))."""
    p = a + b
    out = []
    for h in horizons:
        # sum_{k=0}^{h-1} p^k = (1 - p^h) / (1 - p)
        geo = np.where(p < 1 - 1e-9, (1 - p ** h) / np.maximum(1 - p, 1e-9), float(h))
        out.append(h * vbar + (s2_next - vbar) * geo)
    return np.stack(out, axis=-1)


@dataclass
class GarchFilter(Filter):
    corr: str = "dcc"                    # dcc (EWMA of standardised residuals) | ccc | diag
    lam_corr: float = 0.985
    shrink: float = 0.1
    rank: int | None = None
    name: str = "garch_dcc"
    a: np.ndarray | None = field(default=None, repr=False)
    b: np.ndarray | None = field(default=None, repr=False)
    vbar: np.ndarray | None = field(default=None, repr=False)
    Q0: np.ndarray | None = field(default=None, repr=False)

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> GarchFilter:
        X = np.asarray(X, dtype=np.float64)
        p = garch_grid_fit(X)
        self.a, self.b, self.vbar = p["a"], p["b"], p["vbar"]
        s2 = garch_filter(X, self.a, self.b, self.vbar)
        z = X[1:] / np.sqrt(s2[:-1])
        self.Q0 = _nan_cov(np.vstack([X[:1] / np.sqrt(self.vbar), z]), demean=False)
        sd = np.sqrt(np.maximum(np.diag(self.Q0), 1e-300))
        self.Q0 = self.Q0 / np.outer(sd, sd)
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        X = np.asarray(X, dtype=np.float64)
        T, N = X.shape
        s2 = garch_filter(X, self.a, self.b, self.vbar)  # type: ignore[arg-type]
        sd_next = np.sqrt(s2)
        sd_prev = np.vstack([np.sqrt(self.vbar)[None], sd_next[:-1]])  # type: ignore[index]
        if self.corr == "diag":
            out = np.zeros((T, N, N))
            idx = np.arange(N)
            out[:, idx, idx] = s2
        else:
            Q = self.Q0.copy()  # type: ignore[union-attr]
            out = np.empty((T, N, N))
            for t in range(T):
                if self.corr == "dcc":
                    z = X[t] / sd_prev[t]
                    ok = np.isfinite(z)
                    if ok.all():
                        Q = self.lam_corr * Q + (1 - self.lam_corr) * np.outer(z, z)
                    elif ok.any():
                        zz = np.where(ok, z, 0.0)
                        Q = np.where(np.outer(ok, ok), self.lam_corr * Q + (1 - self.lam_corr) * np.outer(zz, zz), Q)
                q = np.sqrt(np.maximum(np.diag(Q), 1e-300))
                C = Q / np.outer(q, q)
                if self.shrink > 0:
                    C = (1 - self.shrink) * C + self.shrink * np.eye(N)
                out[t] = C * np.outer(sd_next[t], sd_next[t])
        mu = np.zeros((T, N))
        if self.rank is not None:
            return mu, to_lowrank(out, self.rank)
        return mu, Cov(dense=out)

    def params(self) -> dict[str, Any]:
        return {"corr": self.corr, "lam_corr": self.lam_corr, "shrink": self.shrink,
                "a_med": float(np.median(self.a)) if self.a is not None else None,
                "b_med": float(np.median(self.b)) if self.b is not None else None}


def make_filter(kind: str, **kw: Any) -> Filter:
    """Factory by name: ewma | ewma_lr | static_lw | factor | factor_vs | garch_diag | garch_ccc | garch_dcc | gcn_gru."""
    if kind == "ewma":
        return EWMAFilter(**kw)
    if kind == "ewma_lr":
        return EWMAFilter(rank=kw.pop("rank", 4), name="ewma_lr", **kw)
    if kind == "static_lw":
        return StaticLWFilter(**kw)
    if kind == "factor":
        return FactorFilter(**kw)
    if kind == "factor_vs":
        return FactorFilter(vol_scale=True, name="factor_vs", **kw)
    if kind in ("garch_diag", "garch_ccc", "garch_dcc"):
        return GarchFilter(corr=kind.split("_")[1], name=kind, **kw)
    if kind == "gcn_gru":
        from app.conformal.learned import GCNGRUFilter  # noqa: PLC0415

        return GCNGRUFilter(**kw)
    raise ValueError(f"unknown filter {kind!r}")


def standardized_innovations(X: np.ndarray, mu: np.ndarray, cov: Cov) -> np.ndarray:
    """Whitened one-step residuals z_t = Sigma^{-1/2}(X[t+1] - mu[t]) (Cholesky), (T-1, N). Diagnostics only."""
    S = cov.to_dense()[:-1]
    r = X[1:] - mu[:-1]
    Lc = np.linalg.cholesky(S)
    return np.linalg.solve(Lc, r[..., None])[..., 0]


def nll_per_step(X: np.ndarray, mu: np.ndarray, cov: Cov, t0: int = 0) -> float:
    r = X[1:] - mu[:-1]
    ok = np.isfinite(r).all(axis=1)
    ok[:t0] = False
    v = gaussian_nll(r[ok], cov.take(np.nonzero(ok)[0]))
    return float(np.nanmean(v) + 0.5 * X.shape[1] * math.log(2 * math.pi))
