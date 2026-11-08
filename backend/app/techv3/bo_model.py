"""#5 calibrated P(follow-through) of a breakout (MODEL-BASED, historical frequency).

Two classifiers on the same events (app.techv3.breakouts): the GEOMETRY baseline (how far beyond the level the
breakout closed, b in sigma_H units; the volatility regime; lookback; direction; region) and the FULL model (the
geometry plus the volume / accumulation features). Logistic regression on standardised, clipped features followed by
isotonic calibration on a held-out year that precedes the test period (purged). What volume adds = FULL minus
GEOMETRY (out-of-sample Brier difference with a month-block bootstrap CI).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl

REGION_CODE = {"US": 0, "EU": 1, "UK": 2, "JP": 3}
GEOMETRY = ["b", "log_vol_ratio", "n55", "dir", "r_eu", "r_uk", "r_jp"]
VOLUME = ["log_relvol20", "log_relvol50", "clv", "cmf20", "obv_slope20", "obv_confirm", "d_illiq", "gap_z", "gap_beyond",
          "ret_z", "range_z", "trend200", "prior20_z"]
FULL = GEOMETRY + VOLUME
CLIP = {"b": (0.0, 3.0), "log_vol_ratio": (-1.5, 1.5), "log_relvol20": (-3.0, 3.0), "log_relvol50": (-3.0, 3.0),
        "clv": (-1.0, 1.0), "cmf20": (-1.0, 1.0), "obv_slope20": (-1.0, 1.0), "d_illiq": (-4.0, 4.0), "gap_z": (-8.0, 8.0),
        "ret_z": (-10.0, 10.0), "range_z": (0.0, 15.0), "trend200": (-1.5, 1.5), "prior20_z": (-6.0, 6.0)}


def design(df: pl.DataFrame, H: int) -> pl.DataFrame:
    """Model columns from an events frame (columns of breakouts.events_for_chunk + region)."""
    rg = pl.col("region") if "region" in df.columns else pl.lit("US")
    return df.select(
        pl.col(f"b_{H}").alias("b"),
        pl.col("vol_ratio").clip(0.05, 20).log().alias("log_vol_ratio"),
        (pl.col("N") == 55).cast(pl.Float64).alias("n55"),
        pl.col("direction").cast(pl.Float64).alias("dir"),
        (rg == "EU").cast(pl.Float64).alias("r_eu"), (rg == "UK").cast(pl.Float64).alias("r_uk"),
        (rg == "JP").cast(pl.Float64).alias("r_jp"),
        pl.col("relvol20").clip(0.01, 100).log().alias("log_relvol20"),
        pl.col("relvol50").clip(0.01, 100).log().alias("log_relvol50"),
        "clv", "cmf20", "obv_slope20", pl.col("obv_confirm").cast(pl.Float64), "d_illiq", "gap_z",
        pl.col("gap_beyond").cast(pl.Float64), "ret_z", "range_z", "trend200", "prior20_z")


def matrix(D: pl.DataFrame, cols: list[str]) -> np.ndarray:
    X = np.column_stack([D[c].cast(pl.Float64).fill_null(0.0).fill_nan(0.0).to_numpy() for c in cols])
    for j, c in enumerate(cols):
        if c in CLIP:
            X[:, j] = np.clip(X[:, j], *CLIP[c])
    return X


@dataclass
class Calibrated:
    cols: list[str]
    kind: str = "logit"
    model: Any = None
    iso: Any = None
    mu: np.ndarray | None = None
    sd: np.ndarray | None = None
    base_rate: float = 0.0
    info: dict[str, Any] = field(default_factory=dict)

    def raw(self, X: np.ndarray) -> np.ndarray:
        if self.kind == "logit":
            return self.model.predict_proba((X - self.mu) / self.sd)[:, 1]
        return self.model.predict_proba(X)[:, 1]

    def predict(self, X: np.ndarray) -> np.ndarray:
        p = self.raw(X)
        return self.iso.predict(p) if self.iso is not None else p


def fit(X: np.ndarray, y: np.ndarray, Xc: np.ndarray | None, yc: np.ndarray | None, cols: list[str], kind: str = "logit",
        seed: int = 0) -> Calibrated:
    from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415

    m = Calibrated(cols=cols, kind=kind, base_rate=float(np.mean(y)) if len(y) else 0.0)
    if kind == "logit":
        from sklearn.linear_model import LogisticRegression  # noqa: PLC0415

        m.mu = X.mean(0)
        m.sd = X.std(0) + 1e-6
        m.model = LogisticRegression(C=1.0, max_iter=500).fit((X - m.mu) / m.sd, y)
    else:
        from sklearn.ensemble import HistGradientBoostingClassifier  # noqa: PLC0415

        m.model = HistGradientBoostingClassifier(max_iter=250, learning_rate=0.06, max_leaf_nodes=31, min_samples_leaf=300,
                                                 l2_regularization=1.0, early_stopping=False, random_state=seed).fit(X, y)
    if Xc is not None and yc is not None and len(yc) >= 500:
        m.iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(m.raw(Xc), yc.astype(float))
    return m


def subsample(n: int, k: int, seed: int) -> np.ndarray:
    if n <= k:
        return np.arange(n)
    return np.sort(np.random.default_rng(seed).choice(n, k, replace=False))


def coefficients(m: Calibrated) -> dict[str, float]:
    """Standardised logit coefficients (per 1 sd of the feature) - the interpretable 'what drives it'."""
    if m.kind != "logit" or m.model is None:
        return {}
    return {c: float(v) for c, v in zip(m.cols, m.model.coef_[0], strict=True)}
