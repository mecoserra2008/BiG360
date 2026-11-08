"""Leakage checks for filters (the filter must emit (Y_hat_{t+1|t}, Sigma_hat_{t+1|t}) from X[: t + 1] only).

causal_invariance  re-emit after replacing the FUTURE (rows > c) with shuffled, rescaled data; rows <= c must be
                   bit-for-bit unchanged (up to float tolerance). Any dependence on future rows fails.
shift_check        empirical shuffled-time test on data with no honest predictability (i.i.d. innovations): the
                   mean score of the aligned pairs (row t vs X[t + 1]) must match the mean score against
                   time-shuffled targets; a filter that peeks at X[t + 1] scores the aligned pairs far better.
PeekingFilter      a deliberately leaky wrapper (mean <- X[t + 1] * w) used by the tests to prove the checks bite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.conformal.filters import Filter
from app.conformal.scores import Cov, mahalanobis


def _cov_rows(cov: Cov, k: int) -> np.ndarray:
    return cov.to_dense()[: k + 1]


def causal_invariance(f: Filter, X: np.ndarray, cuts: tuple[float, ...] = (0.3, 0.6, 0.9), seed: int = 0,
                      tol: float = 1e-9) -> dict[str, Any]:
    X = np.asarray(X, dtype=np.float64)
    rng = np.random.default_rng(seed)
    mu, cov = f.emit(X)
    S = cov.to_dense()
    worst = 0.0
    for c in cuts:
        k = int(len(X) * c)
        X2 = X.copy()
        fut = X2[k + 1:]
        X2[k + 1:] = fut[rng.permutation(len(fut))] * 3.0 + rng.normal(0, np.nanstd(X), fut.shape)
        mu2, cov2 = f.emit(X2)
        S2 = cov2.to_dense()
        scale = max(float(np.nanmax(np.abs(S[: k + 1]))), 1e-300)
        d = max(float(np.nanmax(np.abs(mu[: k + 1] - mu2[: k + 1]))) / max(float(np.nanstd(X)), 1e-300),
                float(np.nanmax(np.abs(S[: k + 1] - S2[: k + 1]))) / scale)
        worst = max(worst, d)
    return {"passed": bool(worst <= tol), "max_rel_change": worst}


def shift_check(f: Filter, X: np.ndarray, t0: int = 50, n_shuffle: int = 5, seed: int = 0,
                ratio_floor: float = 0.85) -> dict[str, Any]:
    """Aligned mean score / time-shuffled-target mean score; passes when the ratio >= ratio_floor (use on i.i.d.
    data: an honest filter gives ~1, a peeking filter << 1)."""
    X = np.asarray(X, dtype=np.float64)
    rng = np.random.default_rng(seed)
    mu, cov = f.emit(X)
    idx = np.arange(t0, len(X) - 1)
    cv = cov.take(idx)
    aligned = float(np.nanmean(mahalanobis(X[idx + 1] - mu[idx], cv)))
    sh = []
    for _ in range(n_shuffle):
        p = rng.permutation(idx) + 1
        sh.append(float(np.nanmean(mahalanobis(X[p] - mu[idx], cv))))
    shuffled = float(np.mean(sh))
    ratio = aligned / shuffled if shuffled > 0 else float("nan")
    return {"passed": bool(ratio >= ratio_floor), "aligned": aligned, "shuffled": shuffled, "ratio": ratio}


@dataclass
class PeekingFilter(Filter):
    inner: Filter | None = None
    w: float = 0.8
    name: str = "peeking"

    def fit(self, X: np.ndarray, Y: np.ndarray | None = None) -> PeekingFilter:
        assert self.inner is not None
        self.inner.fit(X, Y)
        return self

    def emit(self, X: np.ndarray) -> tuple[np.ndarray, Cov]:
        assert self.inner is not None
        mu, cov = self.inner.emit(X)
        nxt = np.vstack([X[1:], np.zeros((1, X.shape[1]))])
        return mu + self.w * nxt, cov
