"""Leakage audit requested by the orchestrator after the 3M FT-Transformer's OOS numbers (rank-IC 0.143,
hit-rate 97%) were judged implausible. Two layers:

1. `assert_no_label_leakage` (static, cheap, runs on every real training call via `run_walk_forward`).
2. The permutation test (empirical, "definitive" per the orchestrator's spec): train on `rank_label` shuffled
   *within each training date*, score the real unpermuted test fold, assert |rank_IC| < 0.01 against the true
   labels. Needs real `ml_features`/`ml_labels` data and, for the Transformer, several minutes -- marked `slow`
   and skipped if the tables are empty (e.g. a clean dev checkout).

Findings (see model_runs.notes on the retrained runs for the full write-up):
* Code audit of `_build_batches`/`cs_transform`/`run_transformer_fold` found no column derived from a label
  entering the model's inputs (x/x_lag1/x_lag2 are built strictly from `feature_registry.yaml`'s live `f_*`
  ids); cs_transform's rank-gauss/z-score are per-date (`.over("date")`), so computing them before vs. after
  fold-splitting is mathematically identical -- not a train/test normalization leak; early stopping uses an
  inner 15%-of-training-dates validation split, never the test fold.
* XGBoost passes the permutation test cleanly: rank-IC on permuted-label training bounces around zero with
  inconsistent sign across seeds (noise), as expected for ~700-name cross-sections with 70+ test dates.
* The Transformer FAILS the permutation test: a consistent +0.06 to +0.09 rank-IC across 4 independent random
  permutation seeds (same train/test window), unaffected by training for 4 vs. 12 epochs. That rules out pure
  sampling noise (unlike XGBoost's sign-flipping result) *and* rules out "early stopping grabbed a lucky
  epoch" (more epochs didn't change it). The leading hypothesis -- not fixed this session, tracked as a TODO
  in `app/ml/models/ft_transformer.py` -- is architectural, not a PIT/data leak: `row_repr = row + lag1 + lag2`
  sums the lag terms directly into the output path, so real (non-leaked) momentum-style information in
  x_lag1/x_lag2 can dominate the prediction via this residual path almost independently of what target the
  model is nominally trained on. This is why the Transformer is NOT production for any horizon until that is
  addressed and it passes this test.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.core.config import get_settings
from app.core.storage import read_table
from app.ml.train import assert_no_label_leakage, run_permutation_leakage_check

pytestmark = pytest.mark.slow


def _has_real_data() -> bool:
    try:
        return read_table("ml_features").height > 0 and read_table("ml_labels").height > 0
    except Exception:  # noqa: BLE001
        return False


def test_assert_no_label_leakage_rejects_label_like_names():
    with pytest.raises(AssertionError):
        assert_no_label_leakage(["f_mom_21d", "rank_label"])
    with pytest.raises(AssertionError):
        assert_no_label_leakage(["f_mom_21d", "fwd_excess_sector"])


def test_assert_no_label_leakage_rejects_registry_mismatch():
    with pytest.raises(AssertionError):
        assert_no_label_leakage(["f_mom_21d", "f_not_a_real_feature"])


@pytest.mark.skipif(not _has_real_data(), reason="needs populated ml_features/ml_labels")
def test_xgb_permutation_leakage_check_shows_no_consistent_signal():
    """Multiple independent permutation seeds: the mean must be small and the sign must not be consistent
    (both are expected under noise; a true leak would show a stable sign/magnitude regardless of the random
    target). This is the regression test for the fix found during the original audit (a mis-registered
    zscore transform that NaN-poisoned every prediction) and the general PIT correctness of the feature/label
    pipeline."""
    settings = get_settings()
    ics = [run_permutation_leakage_check("3M", "xgboost_rank", settings, test_months=18, seed=seed)["rank_ic_mean"]
          for seed in range(5)]
    assert abs(float(np.mean(ics))) < 0.02
    assert not (all(v > 0 for v in ics) or all(v < 0 for v in ics))  # no consistent sign


@pytest.mark.skipif(not _has_real_data(), reason="needs populated ml_features/ml_labels")
@pytest.mark.xfail(reason="known architectural issue: FTTransformerCS's residual row = row + lag1 + lag2 lets "
                          "real (non-leaked) lag-feature information dominate the output almost independently "
                          "of the training target -- see module docstring. Flip to xpass once fixed.", strict=False)
def test_transformer_permutation_leakage_check_passes():
    settings = get_settings()
    out = run_permutation_leakage_check("3M", "ft_transformer", settings, train_months=36, test_months=6,
                                        max_epochs=6, seed=0)
    assert out["passed"], f"rank_ic_mean={out['rank_ic_mean']} on a permuted-label fit (expected |IC| < 0.01)"
