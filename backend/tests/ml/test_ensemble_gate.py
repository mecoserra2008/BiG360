"""Production gate (app.ml.ensemble): non-xgboost runs need a recorded passed leakage check; ensemble fold metrics /
inherited importance (F28)."""

from __future__ import annotations

import json
from datetime import date

import polars as pl

from app.ml import ensemble as E


def test_gate_eligibility():
    assert E.gate_eligible("xgboost_rank", {"notes": ""})
    assert not E.gate_eligible("ft_transformer", {"notes": "rank-IC 0.14"})
    assert E.gate_eligible("ft_transformer", {"notes": f"x {E.LEAKAGE_PASS_MARK} 2026-10-20"})
    assert not E.gate_eligible("ensemble", {"notes": None})


def test_gate_rejection_note_is_not_a_pass():
    """2026-10-30: the gate's own old rejection note quoted the pass mark ("no '[LEAKAGE_CHECK] passed' recorded"); a
    substring test then counted it as a pass and re-promoted the leaky FT-Transformer at the next weekly gate."""
    old = "9 folds [GATE] not eligible for production: no '[LEAKAGE_CHECK] passed' recorded (label-permutation ...)."
    assert not E.gate_eligible("ft_transformer", {"notes": old})
    from app.api.routers.models import leakage_gate

    assert leakage_gate({"model_type": "ft_transformer", "notes": old})[0] == "failed"
    cand = {"ft_transformer": {"model_run_id": "T", "rank_ic_mean": 0.14, "notes": old},
            "xgboost_rank": {"model_run_id": "X", "rank_ic_mean": 0.03, "notes": ""}}
    scored = sorted(((mt, r, r["rank_ic_mean"]) for mt, r in cand.items()),
                    key=lambda t: (E.gate_eligible(t[0], t[1]), t[2]), reverse=True)
    assert scored[0][0] == "xgboost_rank"


def test_inherited_importance_is_labelled_and_weighted():
    row = {"model_run_id": "M1", "feature_importance": json.dumps([{"feature": "f_mom_12m", "importance": 0.4}])}
    fi = E.inherited_importance(row)
    assert fi == [{"feature": "f_mom_12m", "importance": 0.2, "source": "xgboost_rank base M1 x 0.5 (rank-average weight)"}]
    assert E.inherited_importance({"feature_importance": None}) is None


def test_ensemble_fold_metrics_on_base_windows():
    rows = []
    for d in (date(2020, 1, 31), date(2020, 2, 28), date(2021, 1, 29)):
        for i in range(30):
            rows.append({"date": d, "security_id": f"S{i}", "score": i / 30, "fwd_return": i / 300,
                         "fwd_excess_sector": i / 300, "rank_label": i / 30})
    oos = pl.DataFrame(rows)
    folds = [{"fold_id": "F0", "train_start": "2015-01-01", "train_end": "2019-12-31", "test_start": "2020-01-01",
              "test_end": "2020-12-31", "n_train": 100},
             {"fold_id": "F1", "train_start": "2015-01-01", "train_end": "2020-12-31", "test_start": "2022-01-01",
              "test_end": "2022-12-31", "n_train": 120}]
    fm = E.ensemble_fold_metrics(oos, folds, "1M")
    assert [f["fold_id"] for f in fm] == ["F0"]  # F1 has no OOS rows in its window
    assert fm[0]["n_test"] == 60 and fm[0]["rank_ic"] > 0.99 and fm[0]["test_end"] == "2020-12-31"
