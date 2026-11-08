"""Adaptive conformal inference on the scalar score (paper App. C.5 / D.3).

ACI (Gibbs & Candes 2021): the nominal level is adapted online,

    alpha_{t+1} = alpha_t + gamma (alpha - err_t),     err_t = 1{R_t > q_hat(alpha_t)},

where q_hat(alpha_t) is recomputed from the (frozen or rolling) calibration scores. Long-run average miscoverage
converges to alpha for ANY score sequence (|mean err - alpha| <= (max(alpha_1, 1 - alpha_1) + gamma) / (gamma T)).
alpha_t <= 0 means an unbounded set (q = inf), alpha_t >= 1 the empty set.

Extensions used here:
  * delayed feedback: a set issued at t for an outcome known only at t + lag is scored when the outcome arrives;
    the update at t uses err_{t - lag} (bounded delay keeps the long-run guarantee, with T replaced by T / (lag + 1));
  * pooled feedback: when M series share one level (a calibration pool), err_t is the fraction of the M series
    whose score exceeded q_hat (missing scores ignored), i.e. ACI on the pool's average miscoverage.

AgACI (Zaffran et al. 2022): K ACI experts with gamma in {0.001, 0.005, 0.025} (paper App. D.3) whose thresholds are
aggregated by exponentially weighted averaging on the pinball loss at level 1 - alpha (adaptive learning rate
sqrt(8 log K / t) / B, B = running loss range). Infinite expert thresholds are replaced, for aggregation only, by the
largest score seen so far (calibration scores and already-realised test scores; Zaffran et al. likewise replace
infinite bounds by the observed range).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import asdict, dataclass

import numpy as np

QFun = Callable[[int, float], float]      # (t, level alpha) -> q_hat at time t
DEFAULT_GAMMA = 0.005
AGACI_GAMMAS = (0.001, 0.005, 0.025)


@dataclass
class ACIState:
    alpha: float
    gamma: float = DEFAULT_GAMMA
    alpha_t: float | None = None
    n: int = 0
    err_sum: float = 0.0

    def __post_init__(self) -> None:
        if self.alpha_t is None:
            self.alpha_t = self.alpha

    def update(self, err: float) -> float:
        """Apply one feedback (err in [0, 1]); returns the new alpha_t."""
        if err is None or not math.isfinite(err):
            return float(self.alpha_t)  # type: ignore[arg-type]
        self.alpha_t = float(self.alpha_t) + self.gamma * (self.alpha - float(err))  # type: ignore[arg-type]
        self.n += 1
        self.err_sum += float(err)
        return self.alpha_t

    @property
    def realised_miscoverage(self) -> float | None:
        return self.err_sum / self.n if self.n else None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ACIState:
        return cls(**{k: d[k] for k in ("alpha", "gamma", "alpha_t", "n", "err_sum") if k in d})


def _err(R_t: np.ndarray, q: float) -> float:
    r = np.atleast_1d(R_t)
    r = r[np.isfinite(r)]
    if not r.size:
        return float("nan")
    return float((r > q).mean())


def aci_path(R: np.ndarray, qfun: QFun, alpha: float, gamma: float = DEFAULT_GAMMA, lag: int = 0,
             alpha0: float | None = None) -> dict[str, np.ndarray]:
    """Run ACI over T steps. R: (T,) or (T, M) scores (NaN = not observed / not issued).
    Returns alpha_t (level used at t), q_t (radius used at t), err_t (miscoverage of the set issued at t)."""
    R = np.asarray(R, dtype=np.float64)
    T = R.shape[0]
    st = ACIState(alpha=alpha, gamma=gamma, alpha_t=alpha if alpha0 is None else alpha0)
    a_t, q_t, e_t = np.empty(T), np.empty(T), np.full(T, np.nan)
    for t in range(T):
        a_t[t] = st.alpha_t
        q_t[t] = qfun(t, float(st.alpha_t))  # type: ignore[arg-type]
        e_t[t] = _err(R[t], q_t[t])
        k = t - lag
        if k >= 0:
            st.update(e_t[k])
    return {"alpha_t": a_t, "q_t": q_t, "err_t": e_t}


def pinball(q: np.ndarray, r: np.ndarray, tau: float) -> np.ndarray:
    """Pinball loss of threshold(s) q against observed score(s) r at level tau (mean over finite r)."""
    r = np.atleast_1d(r)
    r = r[np.isfinite(r)]
    if not r.size:
        return np.full(np.shape(q), np.nan)
    u = r[None, :] - np.asarray(q, dtype=np.float64)[:, None]
    return (u * (tau - (u < 0))).mean(axis=1)


def agaci_path(R: np.ndarray, qfun: QFun, alpha: float, gammas: tuple[float, ...] = AGACI_GAMMAS, lag: int = 0,
               qmax_fun: Callable[[int], float] | None = None, level_space: bool = True) -> dict[str, np.ndarray]:
    """Aggregated ACI. Expert weights come from the pinball loss of each expert's threshold (qmax_fun(t) / the largest
    score seen so far stands in for infinite thresholds). level_space=True (default) aggregates the experts' LEVELS
    (q_t = q_hat(sum_k w_k alpha_k,t)), which avoids the stand-in leaking into the emitted radius; level_space=False
    averages the thresholds themselves (Zaffran et al.'s bound aggregation)."""
    R = np.asarray(R, dtype=np.float64)
    T = R.shape[0]
    K = len(gammas)
    experts = [ACIState(alpha=alpha, gamma=g) for g in gammas]
    cum_loss = np.zeros(K)
    n_loss, B = 0, 1e-12
    pending: dict[int, np.ndarray] = {}
    seen_max = -np.inf
    q_t, e_t, w_hist = np.empty(T), np.full(T, np.nan), np.empty((T, K))
    for t in range(T):
        qs = np.array([qfun(t, float(e.alpha_t)) for e in experts])  # type: ignore[arg-type]
        if qmax_fun is not None:
            stand_in = max(qmax_fun(t), seen_max)
            qs = np.where(np.isfinite(qs), qs, stand_in)
        eta = math.sqrt(8.0 * math.log(max(K, 2)) / max(n_loss, 1)) / B
        w = np.exp(-eta * (cum_loss - cum_loss.min()))
        w /= w.sum()
        w_hist[t] = w
        lev = float(w @ np.array([float(e.alpha_t) for e in experts]))  # type: ignore[arg-type]
        q_t[t] = qfun(t, lev) if level_space else (float(w @ qs) if np.isfinite(qs).all() else float("inf"))
        e_t[t] = _err(R[t], q_t[t])
        pending[t] = qs
        k = t - lag
        if k >= 0 and k in pending:
            qk = pending.pop(k)
            rk = np.atleast_1d(R[k])
            if np.isfinite(rk).any():
                seen_max = max(seen_max, float(np.nanmax(rk)))
            ell = pinball(qk, R[k], 1.0 - alpha)
            if np.isfinite(ell).all():
                cum_loss += ell
                n_loss += 1
                B = max(B, float(np.max(ell) - np.min(ell)), 1e-12)
            for e, qe in zip(experts, qk, strict=True):
                e.update(_err(R[k], qe))
    return {"q_t": q_t, "err_t": e_t, "weights": w_hist}


def long_run_bound(alpha1: float, gamma: float, T: int, lag: int = 0) -> float:
    """Gibbs & Candes Prop. 4.1 bound on |mean err - alpha| (with bounded delay lag)."""
    T_eff = max(T // (lag + 1), 1)
    return (max(alpha1, 1.0 - alpha1) + gamma) / (gamma * T_eff)
