"""XGBoost baseline. Regresses on `rank_label` (cross-sectional percentile 0..1 of fwd_excess_sector), not a raw
pairwise ranking loss (`rank:pairwise` needs per-date `qid` groups and converges less predictably on noisy
financial labels at MVP data volumes) -- documented simplification behind the `xgboost_rank` model_type name.
SHAP via XGBoost's native TreeSHAP (`pred_contribs=True`, exact for tree ensembles, no sampling approximation).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import xgboost as xgb

DEFAULT_PARAMS: dict[str, Any] = {
    "n_estimators": 300,
    "max_depth": 4,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.7,
    "reg_lambda": 1.0,
    "min_child_weight": 20,
    "n_jobs": 8,  # this box runs several agents' jobs concurrently (observed <4GB free of 32GB); 20 threads'
                  # worth of per-thread histogram buffers was enough to push XGBoost into "bad allocation"
    "tree_method": "hist",
    "max_bin": 128,
    "objective": "reg:squarederror",
    "random_state": 42,
}


def fit(X: np.ndarray, y: np.ndarray, params: dict[str, Any] | None = None) -> xgb.XGBRegressor:
    model = xgb.XGBRegressor(**{**DEFAULT_PARAMS, **(params or {})})
    model.fit(X, y)
    return model


def predict(model: xgb.XGBRegressor, X: np.ndarray) -> np.ndarray:
    return model.predict(X)


def shap_contributions(model: xgb.XGBRegressor, X: np.ndarray) -> np.ndarray:
    """(N, F+1): column F is the bias term (TreeSHAP's expected-value offset), columns 0..F-1 sum with it to the
    raw prediction (additivity property of SHAP)."""
    booster = model.get_booster()
    dmat = xgb.DMatrix(X)
    return booster.predict(dmat, pred_contribs=True)


def feature_importance(model: xgb.XGBRegressor, feature_names: list[str], X: np.ndarray, sample: int = 4000
                        ) -> list[dict[str, Any]]:
    idx = np.random.default_rng(0).choice(len(X), size=min(sample, len(X)), replace=False)
    contrib = shap_contributions(model, X[idx])[:, :-1]
    mean_abs = np.abs(contrib).mean(axis=0)
    order = np.argsort(-mean_abs)[:50]
    return [{"feature": feature_names[i], "importance": float(mean_abs[i])} for i in order]
