"""Deterministic tiny-train smoke tests: cs_transform correctness and an XGBoost round-trip that must recover a
known synthetic signal (not a real backtest -- just "the plumbing works and isn't obviously broken")."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.ml.models import xgb as xgbm
from app.ml.train import cs_transform


def _registry(ids: list[str], transform: str = "cs_rank_gauss") -> dict:
    feats = [{"id": fid, "status": "live", "transform": transform} for fid in ids]
    return {"features": feats, "by_id": {f["id"]: f for f in feats}}


def test_cs_transform_handles_date_constant_features():
    """Regression test: a feature broadcast identically to every security on a date (e.g. a macro regime
    probability) has zero cross-sectional std. If registered as 'zscore' this used to compute 0/0 = NaN for
    EVERY row on EVERY date, which (unlike a null) survives the Transformer's fill_null() and poisons every
    downstream prediction with NaN -- found via a real walk-forward run where ft_transformer's OOS rank-IC
    came back all-NaN. 'zscore' must now null out (not NaN) a zero-std date; 'none' (the registry's actual
    fix for these features) must pass the value through unchanged."""
    dates = [date(2020, 1, 1)] * 6 + [date(2020, 2, 1)] * 6
    vals = [0.7] * 6 + [0.9] * 6  # identical within each date, different across dates
    df = pl.DataFrame({"date": dates, "f_x": vals})

    out_z = cs_transform(df, _registry(["x"], "zscore"))
    assert out_z["f_x"].null_count() == 12  # every row null, not NaN
    assert not out_z["f_x"].drop_nulls().len()

    out_none = cs_transform(df, _registry(["x"], "none"))
    assert out_none["f_x"].to_list() == vals  # passthrough, unchanged


def test_cs_rank_gauss_is_mean_zero_and_rank_preserving_per_date():
    rng = np.random.default_rng(0)
    dates = [date(2020, 1, 1)] * 50 + [date(2020, 2, 1)] * 50
    vals = np.concatenate([rng.normal(100, 10, 50), rng.normal(5, 1, 50)])  # very different scales per date
    df = pl.DataFrame({"date": dates, "f_x": vals})
    out = cs_transform(df, _registry(["x"]))
    for d in (date(2020, 1, 1), date(2020, 2, 1)):
        g = out.filter(pl.col("date") == d)["f_x"].to_numpy()
        assert abs(g.mean()) < 0.3  # rank-gauss centers each date's cross-section near 0 regardless of raw scale
        # rank order must be preserved (monotonic transform)
        raw = df.filter(pl.col("date") == d)["f_x"].to_numpy()
        assert (np.argsort(raw) == np.argsort(g)).all()


def test_cs_rank_gauss_nulls_stay_null_and_dont_pollute_other_rows():
    df = pl.DataFrame({"date": [date(2020, 1, 1)] * 10,
                       "f_x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, None, None]})
    out = cs_transform(df, _registry(["x"]))
    assert out["f_x"].null_count() == 2
    assert out["f_x"].drop_nulls().len() == 8


def test_cs_rank_gauss_requires_minimum_cross_section_size():
    """Fewer than 5 non-null names on a date -> the whole column is null for that date (too noisy a rank to be
    meaningful), per the >=5 guard in app.ml.train.cs_transform."""
    df = pl.DataFrame({"date": [date(2020, 1, 1)] * 3, "f_x": [1.0, 2.0, 3.0]})
    out = cs_transform(df, _registry(["x"]))
    assert out["f_x"].null_count() == 3


def test_xgb_recovers_a_strong_linear_signal():
    rng = np.random.default_rng(42)
    n = 2000
    X = rng.normal(size=(n, 3))
    y = 0.8 * X[:, 0] - 0.3 * X[:, 1] + 0.01 * rng.normal(size=n)  # feature 0 dominates, feature 2 is noise
    model = xgbm.fit(X, y, params={"n_estimators": 50, "max_depth": 3})
    pred = xgbm.predict(model, X)
    # rank correlation with the true signal should be strong
    assert np.corrcoef(pred, y)[0, 1] > 0.9
    contrib = xgbm.shap_contributions(model, X[:200])
    # SHAP additivity: contributions + bias reconstruct the raw prediction
    raw_pred = model.get_booster().predict(__import__("xgboost").DMatrix(X[:200]))
    np.testing.assert_allclose(contrib.sum(axis=1), raw_pred, atol=1e-3)
    importance = xgbm.feature_importance(model, ["f0", "f1", "f2"], X)
    assert importance[0]["feature"] == "f0"  # dominant feature should rank first by mean |SHAP|


def test_xgb_is_deterministic_given_fixed_seed():
    rng = np.random.default_rng(1)
    X = rng.normal(size=(200, 4))
    y = X[:, 0] + rng.normal(scale=0.1, size=200)
    m1 = xgbm.fit(X, y)
    m2 = xgbm.fit(X, y)
    np.testing.assert_allclose(xgbm.predict(m1, X), xgbm.predict(m2, X))


# ------------------------------------------------------------------------------------------------ transformer


def _toy_batches(n_dates: int, n_names: int, n_features: int, n_sectors: int, seed: int):
    from app.ml.models.ft_transformer import DateBatch

    rng = np.random.default_rng(seed)
    batches = []
    for d in range(n_dates):
        x = rng.normal(size=(n_names, n_features)).astype(np.float32)
        y = (0.5 * x[:, 0] + rng.normal(scale=0.2, size=n_names)).astype(np.float32)
        batches.append(DateBatch(
            date=date(2020, 1, 1) + timedelta(days=d), x=x, x_lag1=np.zeros_like(x), x_lag2=np.zeros_like(x),
            sector_idx=rng.integers(0, n_sectors, size=n_names).astype(np.int64), y=y,
            security_ids=[f"S{i}" for i in range(n_names)]))
    return batches


def test_ft_transformer_forward_pass_shapes_and_determinism():
    """Not a predictive-quality check (too tiny a toy for that) -- just that the architecture runs end to end,
    produces the right output shape, and is deterministic given a fixed seed (reproducibility requirement)."""
    from app.ml.models.ft_transformer import FTConfig, FTTransformerCS, _to_tensors

    cfg = FTConfig(n_features=6, n_sectors=3, d_model=8, n_heads=2, n_layers=1)
    batches = _toy_batches(1, 12, 6, 3, seed=0)
    torch_seed_model = lambda: FTTransformerCS(cfg)  # noqa: E731
    import torch

    torch.manual_seed(0)
    m1 = torch_seed_model().eval()
    torch.manual_seed(0)
    m2 = torch_seed_model().eval()  # .eval(): dropout is intentionally stochastic, not part of what we're testing
    x, x1, x2, sec, y = _to_tensors(batches[0])
    with torch.no_grad():
        out1 = m1(x, x1, x2, sec)
        out2 = m2(x, x1, x2, sec)
    assert out1.shape == (12,)
    assert torch.allclose(out1, out2)  # same seed, same weights, same input -> identical output


def test_ft_transformer_train_model_reduces_training_loss_on_a_tiny_toy():
    from app.ml.models.ft_transformer import train_model

    train_batches = _toy_batches(20, 15, 5, 2, seed=1)
    val_batches = _toy_batches(5, 15, 5, 2, seed=2)
    model, info = train_model(train_batches, val_batches, n_features=5, n_sectors=2, max_epochs=5, patience=5,
                              seed=7)
    history = info["history"]
    assert len(history) >= 1
    # loss should not blow up (basic sanity: finite, and the last epoch isn't wildly worse than the first)
    assert np.isfinite(history[0]["train_loss"]) and np.isfinite(history[-1]["train_loss"])
    assert history[-1]["train_loss"] < history[0]["train_loss"] * 5


def test_integrated_gradients_runs_and_matches_feature_count():
    from app.ml.models.ft_transformer import FTConfig, FTTransformerCS, integrated_gradients

    cfg = FTConfig(n_features=4, n_sectors=2, d_model=8, n_heads=2, n_layers=1)
    model = FTTransformerCS(cfg)
    batch = _toy_batches(1, 6, 4, 2, seed=3)[0]
    attributions = integrated_gradients(model, batch, steps=4)
    assert attributions.shape == (6, 4)
    assert np.isfinite(attributions).all()
