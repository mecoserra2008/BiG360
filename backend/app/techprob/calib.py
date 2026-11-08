"""Calibration metrics for the T6 gate: reliability bins, ECE, Brier, log-loss, block bootstrap.

ECE (expected calibration error) = sum_b (n_b / N) |mean(y_b) - mean(p_b)| over quantile bins of the prediction
(default 15 bins; quantile bins keep every bin populated). Reported in probability units (x 100 = pp).
Calibration-in-the-large gap = mean(y) - mean(p) (the "regime off by" measure of the gate).
Block bootstrap: resample whole blocks (e.g. calendar months) with replacement, so overlapping / autocorrelated
outcomes inside a block stay together.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np

EPS = 1e-6


def _clean(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    p = np.asarray(p, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    ok = np.isfinite(p) & np.isfinite(y)
    return p[ok], y[ok]


def bin_edges(p: np.ndarray, n_bins: int = 15, strategy: str = "quantile") -> np.ndarray:
    if strategy == "uniform":
        return np.linspace(0.0, 1.0, n_bins + 1)
    q = np.unique(np.quantile(p, np.linspace(0, 1, n_bins + 1)))
    if q.size < 2:
        return np.array([0.0, 1.0])
    q[0], q[-1] = -np.inf, np.inf
    return q


def reliability(p: np.ndarray, y: np.ndarray, n_bins: int = 15, strategy: str = "quantile") -> list[dict[str, Any]]:
    p, y = _clean(p, y)
    if not p.size:
        return []
    e = bin_edges(p, n_bins, strategy)
    k = np.clip(np.searchsorted(e, p, side="right") - 1, 0, len(e) - 2)
    out = []
    for b in range(len(e) - 1):
        m = k == b
        if not m.any():
            continue
        out.append({"bin": b, "n": int(m.sum()), "p_mean": float(p[m].mean()), "y_mean": float(y[m].mean()),
                    "p_lo": float(p[m].min()), "p_hi": float(p[m].max())})
    return out


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = 15, strategy: str = "quantile") -> float | None:
    rel = reliability(p, y, n_bins, strategy)
    n = sum(r["n"] for r in rel)
    if not n:
        return None
    return float(sum(r["n"] * abs(r["y_mean"] - r["p_mean"]) for r in rel) / n)


def brier(p: np.ndarray, y: np.ndarray) -> float | None:
    p, y = _clean(p, y)
    return float(np.mean((p - y) ** 2)) if p.size else None


def logloss(p: np.ndarray, y: np.ndarray) -> float | None:
    p, y = _clean(p, y)
    if not p.size:
        return None
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def gap(p: np.ndarray, y: np.ndarray) -> float | None:
    p, y = _clean(p, y)
    return float(y.mean() - p.mean()) if p.size else None


def summary(p: np.ndarray, y: np.ndarray, n_bins: int = 15) -> dict[str, Any]:
    p_, y_ = _clean(p, y)
    return {"n": int(p_.size), "mean_p": float(p_.mean()) if p_.size else None, "mean_y": float(y_.mean()) if y_.size else None,
            "gap": gap(p_, y_), "ece": ece(p_, y_, n_bins), "brier": brier(p_, y_), "logloss": logloss(p_, y_)}


def block_bootstrap(stat: Callable[[np.ndarray], float], blocks: np.ndarray, B: int = 400, seed: int = 0,
                    alpha: float = 0.05) -> dict[str, Any]:
    """CI of stat(index_array) by resampling whole blocks (labels in `blocks`)."""
    blocks = np.asarray(blocks)
    u, inv = np.unique(blocks, return_inverse=True)
    groups = [np.nonzero(inv == g)[0] for g in range(len(u))]
    rng = np.random.default_rng(seed)
    est = float(stat(np.arange(len(blocks))))
    vals = []
    for _ in range(B):
        pick = rng.integers(0, len(groups), len(groups))
        idx = np.concatenate([groups[g] for g in pick]) if len(groups) else np.zeros(0, dtype=np.int64)
        if idx.size:
            v = stat(idx)
            if v is not None and np.isfinite(v):
                vals.append(float(v))
    if not vals:
        return {"est": est, "lo": None, "hi": None, "se": None, "B": 0}
    v = np.array(vals)
    return {"est": est, "lo": float(np.quantile(v, alpha / 2)), "hi": float(np.quantile(v, 1 - alpha / 2)),
            "se": float(v.std(ddof=1)) if v.size > 1 else None, "B": int(v.size),
            "p_le_0": float((v <= 0).mean()), "p_ge_0": float((v >= 0).mean())}


def month_blocks(dates: np.ndarray) -> np.ndarray:
    return np.asarray(dates, dtype="datetime64[D]").astype("datetime64[M]").astype(np.int64)
