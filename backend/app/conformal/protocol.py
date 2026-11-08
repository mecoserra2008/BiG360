"""Algorithm 1 (filtered split-conformal prediction) with the production extensions we need for finance.

    1. pretrain block      fit the filter, then FREEZE it
    2. calibration block   run the frozen filter, collect squared-Mahalanobis scores R_t, discard warm-up t < t0 (>= 50)
    3. test                q_hat = R_(ceil((m+1)(1-alpha))) over the calibration scores; emit the ellipsoid

Extensions (all keep the "scores come from the frozen filter, computed before Y_t is seen" property):
  * rolling recalibration: at test time t the calibration set is the scores of the last `window` origins whose
    outcome was already realised (origin <= t - lag - 1 for an outcome observed `lag` steps after the origin; lag = 0
    for one-step targets); `recal_every` re-sorts the window only every k steps;
  * pooled scores: R may be (T, M) - M series sharing one calibration pool (e.g. securities of a sector-region);
  * ACI / AgACI wrap on the level (app.conformal.aci), fed with delayed (lag) and pooled feedback.

`Calibrator.q(t, alpha)` is the radius used at test time t; `run_split` evaluates a whole test period.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.conformal import aci as A
from app.conformal.scores import quantile_from_sorted

WARMUP = 50


@dataclass
class Split:
    """Chronological indices: [0, pretrain_end) pretrain, [cal_start, test_start) calibration, [test_start, T) test."""
    pretrain_end: int
    test_start: int
    T: int
    t0: int = WARMUP                      # warm-up discarded after the filter starts running

    @property
    def cal_start(self) -> int:
        return self.pretrain_end + self.t0 if self.pretrain_end > 0 else self.t0


def chronological_split(T: int, frac: tuple[float, float, float] = (0.4, 0.4, 0.2), t0: int = WARMUP) -> Split:
    a = int(T * frac[0])
    b = int(T * (frac[0] + frac[1]))
    return Split(pretrain_end=a, test_start=b, T=T, t0=t0)


@dataclass
class Calibrator:
    """Calibration-score buffer. R (T,) or (T, M): score of origin t (NaN when not available / warm-up)."""
    R: np.ndarray
    start: int                            # first origin allowed in calibration (post-freeze, post-warm-up)
    window: int | None = None             # rolling window in origins (None = fixed block [start, fixed_end))
    lag: int = 0                          # outcome of origin s is known at s + lag + 1 (decision time)
    fixed_end: int | None = None
    recal_every: int = 1
    min_n: int = 30
    _cache: dict[int, np.ndarray] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.R = np.asarray(self.R, dtype=np.float64)
        if self.R.ndim == 1:
            self.R = self.R[:, None]

    def _key(self, t: int) -> int:
        if self.window is None and self.fixed_end is not None:
            return -1
        return t - (t % max(self.recal_every, 1))

    def sorted_at(self, t: int) -> np.ndarray:
        k = self._key(t)
        if k in self._cache:
            return self._cache[k]
        if self.window is None:            # fixed block, or expanding when fixed_end is None
            hi = self.fixed_end if self.fixed_end is not None else k - self.lag
            lo = self.start
        else:
            hi = k - self.lag                 # origins s < hi have outcomes known at decision time k
            lo = max(self.start, hi - self.window)
        hi = max(hi, lo)
        v = self.R[lo:hi].ravel()
        s = np.sort(v[np.isfinite(v)])
        if len(self._cache) > 4:
            self._cache.clear()
        self._cache[k] = s
        return s

    def n_at(self, t: int) -> int:
        return len(self.sorted_at(t))

    def q(self, t: int, alpha: float) -> float:
        s = self.sorted_at(t)
        if len(s) < self.min_n:
            return float("nan")
        return float(quantile_from_sorted(s, alpha))

    def qmax(self, t: int) -> float:
        s = self.sorted_at(t)
        return float(s[-1]) if len(s) else float("nan")


def run_split(R: np.ndarray, cal: Calibrator, test_idx: np.ndarray, alphas: tuple[float, ...] = (0.2, 0.1, 0.05),
              aci: str | None = None, gamma: float = A.DEFAULT_GAMMA) -> dict[str, Any]:
    """Radii and coverage over the test origins. aci in (None, 'aci', 'agaci'). Returns per alpha: q (len test),
    covered (len test x M, NaN where R missing), and for ACI the level path."""
    R = np.asarray(R, dtype=np.float64)
    R2 = R[:, None] if R.ndim == 1 else R
    test_idx = np.asarray(test_idx)
    out: dict[str, Any] = {"test_idx": test_idx}
    for a in alphas:
        if aci is None:
            q = np.array([cal.q(int(t), a) for t in test_idx])
            extra: dict[str, Any] = {}
        else:
            sub = R2[test_idx]

            def qfun(i: int, lev: float, _ti=test_idx) -> float:
                return cal.q(int(_ti[i]), lev) if lev > 0 else float("inf")

            if aci == "aci":
                res = A.aci_path(sub, qfun, a, gamma=gamma, lag=cal.lag)
                extra = {"alpha_t": res["alpha_t"]}
            else:
                res = A.agaci_path(sub, qfun, a, lag=cal.lag, qmax_fun=lambda i, _ti=test_idx: cal.qmax(int(_ti[i])))
                extra = {"weights": res["weights"]}
            q = res["q_t"]
        cov = np.where(np.isfinite(R2[test_idx]), (R2[test_idx] <= q[:, None]).astype(float), np.nan)
        out[a] = {"q": q, "covered": cov, **extra}
    return out


def coverage_summary(covered: np.ndarray) -> dict[str, float | int | None]:
    c = np.asarray(covered, dtype=np.float64)
    c = c[np.isfinite(c)]
    if not c.size:
        return {"coverage": None, "n": 0}
    return {"coverage": float(c.mean()), "n": int(c.size)}


def mc_band(alpha: float, n_cal: int, n_test: int, z: float = 3.0) -> tuple[float, float]:
    """Proposition 8 / Theorem 1 band [1 - alpha, 1 - alpha + 1/(n+1)] widened by z Monte-Carlo standard errors."""
    p = 1 - alpha
    se = (p * (1 - p) / max(n_test, 1)) ** 0.5
    return p - z * se, p + 1.0 / (n_cal + 1) + z * se
