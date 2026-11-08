"""T3: zone hold / break model, P(hold | touch) (docs/TECHNICALS_V2_PLAN.md T3). MODEL-BASED.

Rows: zones touched within history.TOUCH_H sessions of a weekly origin (history.process_security), label hold
(reversal >= HOLD_K sigma before a close beyond the far edge by >= BREAK_M sigma, within RESOLVE_R sessions).
Two calibrated classifiers per sample set, fitted on purged walk-forward splits:

  logit   standardised logistic regression (L2, C = 1)
  hgb     sklearn HistGradientBoostingClassifier (300 iterations, 31 leaves, >= 200 rows per leaf)
  both followed by isotonic calibration on a held-out year that precedes the test period (purged by 60 days).

Real zones use all features (level-family presence, counts, confluence weight / old strength, + the band
features); the random-control model uses the band features only and is trained on the random control bands
(same width, centre shifted +-U(0.75, 2.5) ATR). Edge of a zone = P_real(hold | features) - P_control(hold | its
band features): what the levels add over touching an arbitrary band with the same width, approach, trend, regime,
moving-average and volume-profile context. The edge is reported per predicted-edge bucket with block-bootstrap
CIs from the walk-forward; edge_significant = the bucket CI excludes 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

from app.techprob import barriers as B
from app.techprob import calib as K
from app.techprob.history import FAMILIES, REAL_ONLY

BAND = ["side", "in_zone", "dist_sigma", "width_sigma", "approach5", "approach20", "trend_signed", "struct_signed",
        "rsi_signed", "ma50_dist_atr", "ma200_dist_atr", "ma50_in", "ma200_in", "close_ma200", "vp_dist_atr",
        "prior_touches", "prior_resolved", "prior_hold_rate", "vol_regime", "earn_in20", "vix_high", "region_code"]
# the old strength / weight sum are NOT model inputs: the walk-forward zones are built with neutral level-type
# reliability (no point-in-time level_type_stats), the live zones with it, so their scale differs; they are kept as
# the "old strength" baseline only. Family presence, member / type counts and the relative density are scale-free.
REAL_ONLY_MODEL = [c for c in REAL_ONLY if c not in ("strength", "log_weight_sum")]
REAL = BAND + REAL_ONLY_MODEL
CLIP = {"dist_sigma": (0, 30), "width_sigma": (0, 15), "approach5": (-6, 6), "approach20": (-6, 6), "ma50_dist_atr": (-20, 20),
        "ma200_dist_atr": (-40, 40), "close_ma200": (-30, 30), "vp_dist_atr": (0, 10), "prior_touches": (0, 30),
        "prior_resolved": (0, 30), "log_weight_sum": (0, 8)}
N_BUCKETS = 5


def matrix(df: pl.DataFrame, cols: list[str]) -> np.ndarray:
    if "region_code" not in df.columns and "region" in df.columns:
        df = df.with_columns(pl.col("region").map_elements(B.region_code, return_dtype=pl.Int64).alias("region_code"))
    X = np.column_stack([df[c].cast(pl.Float64).fill_null(0.0).fill_nan(0.0).to_numpy() if c in df.columns
                         else np.zeros(df.height) for c in cols]).astype(np.float32)
    for j, c in enumerate(cols):
        if c in CLIP:
            X[:, j] = np.clip(X[:, j], *CLIP[c])
    return X


@dataclass
class Calibrated:
    kind: str
    cols: list[str]
    model: Any = None
    iso: Any = None
    mu: np.ndarray | None = None
    sd: np.ndarray | None = None
    base_rate: float = 0.5
    info: dict[str, Any] = field(default_factory=dict)

    def raw(self, X: np.ndarray) -> np.ndarray:
        if self.kind == "logit":
            Z = (X - self.mu) / self.sd
            return self.model.predict_proba(Z)[:, 1]
        return self.model.predict_proba(X)[:, 1]

    def predict(self, X: np.ndarray) -> np.ndarray:
        p = self.raw(X)
        return self.iso.predict(p) if self.iso is not None else p


def fit(kind: str, X: np.ndarray, y: np.ndarray, Xc: np.ndarray | None, yc: np.ndarray | None, cols: list[str],
        seed: int = 0) -> Calibrated:
    """Fit on (X, y), isotonic-calibrate on (Xc, yc) (skipped when no calibration set)."""
    from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415

    m = Calibrated(kind=kind, cols=cols, base_rate=float(np.mean(y)) if len(y) else 0.5)
    if kind == "logit":
        from sklearn.linear_model import LogisticRegression  # noqa: PLC0415

        m.mu = X.mean(0)
        m.sd = X.std(0) + 1e-6
        m.model = LogisticRegression(C=1.0, max_iter=400).fit((X - m.mu) / m.sd, y)
    elif kind == "hgb":
        from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: PLC0415

        m.model = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=200,
                                                 l2_regularization=1.0, early_stopping=False, random_state=seed).fit(X, y)
    else:
        raise ValueError(kind)
    if Xc is not None and yc is not None and len(yc) >= 500:
        m.iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(m.raw(Xc), yc.astype(float))
    return m


def subsample(n: int, k: int, seed: int) -> np.ndarray:
    if n <= k:
        return np.arange(n)
    return np.sort(np.random.default_rng(seed).choice(n, k, replace=False))


def bucket_edges(edge_train: np.ndarray, n: int = N_BUCKETS) -> np.ndarray:
    q = np.quantile(edge_train[np.isfinite(edge_train)], np.linspace(0, 1, n + 1))
    q[0], q[-1] = -np.inf, np.inf
    return q


def bucket_of(edge: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.clip(np.searchsorted(edges, edge, side="right") - 1, 0, len(edges) - 2)


def bucket_stats(edge_pred: np.ndarray, y: np.ndarray, p_ctrl: np.ndarray, p_real: np.ndarray, dates: np.ndarray,
                 edges: np.ndarray, B_: int = 300) -> list[dict[str, Any]]:
    """Per predicted-edge bucket: realised edge = mean(y - p_ctrl) with a month-block bootstrap CI."""
    k = bucket_of(edge_pred, edges)
    blocks = K.month_blocks(dates)
    out = []
    for b in range(len(edges) - 1):
        m = k == b
        if m.sum() < 200:
            out.append({"bucket": b, "n": int(m.sum()), "edge_lo": float(edges[b]), "edge_hi": float(edges[b + 1])})
            continue
        d = (y[m] - p_ctrl[m]).astype(np.float64)
        bs = K.block_bootstrap(lambda ix, dd=d: float(dd[ix].mean()), blocks[m], B=B_)
        out.append({"bucket": b, "n": int(m.sum()), "edge_lo": float(edges[b]), "edge_hi": float(edges[b + 1]),
                    "pred_edge": float(edge_pred[m].mean()), "realised_edge": bs["est"], "ci_lo": bs["lo"], "ci_hi": bs["hi"],
                    "hold_rate": float(y[m].mean()), "p_real": float(p_real[m].mean()), "p_ctrl": float(p_ctrl[m].mean()),
                    "significant": bool(bs["lo"] is not None and (bs["lo"] > 0 or bs["hi"] < 0))})
    return out


def edge_flag(edge: float | None, buckets: list[dict[str, Any]], gate_pass: bool) -> dict[str, Any]:
    """Live per-zone edge verdict from the walk-forward bucket table."""
    if edge is None or not math.isfinite(edge) or not buckets:
        return {"edge_significant": False, "edge_bucket": None, "edge_ci": None, "edge_direction": None}
    edges = np.array([buckets[0]["edge_lo"], *[b["edge_hi"] for b in buckets]], dtype=np.float64)
    edges[0], edges[-1] = -np.inf, np.inf
    b = int(bucket_of(np.array([edge]), edges)[0])
    st = buckets[b]
    sig = bool(st.get("significant")) and gate_pass
    direction = None
    if st.get("ci_lo") is not None:
        direction = "better_than_random" if st["ci_lo"] > 0 else ("worse_than_random" if st["ci_hi"] < 0 else "none")
    return {"edge_significant": sig, "edge_bucket": b, "edge_ci": [st.get("ci_lo"), st.get("ci_hi")],
            "edge_realised": st.get("realised_edge"), "edge_direction": direction, "edge_bucket_n": st.get("n")}


def family_list() -> list[str]:
    return list(FAMILIES)
