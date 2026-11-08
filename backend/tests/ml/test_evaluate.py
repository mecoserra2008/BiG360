"""Regression tests for the evaluation bugs found via frontend verification against live Model Lab data:
(1) a fold Sharpe ratio stored under metric='fold_rank_ic' (values >1, not a correlation);
(2) a cumulative long-short series built by summing overlapping-window forward returns (~3,200%)."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.ml.evaluate import add_fold_metrics, evaluate_predictions, non_overlapping_subsample


def test_add_fold_metrics_only_stores_rank_ic_never_a_sharpe():
    rows: list[dict] = []
    add_fold_metrics(rows, "F0", date(2020, 1, 1), rank_ic=0.42)
    assert len(rows) == 1  # previously 2: a sharpe row piggybacked on the same metric name
    assert rows[0]["metric"] == "fold_rank_ic"
    assert -1.0 <= rows[0]["value"] <= 1.0


def test_non_overlapping_subsample_keeps_spaced_rows_only():
    # weekly dates, horizon = 90 calendar days -> should keep roughly 1 in 13
    dates = [date(2020, 1, 1) + timedelta(weeks=i) for i in range(60)]
    df = pl.DataFrame({"date": dates, "x": list(range(60))})
    out = non_overlapping_subsample(df, horizon_days=90)
    kept = out["date"].to_list()
    for a, b in zip(kept, kept[1:], strict=False):
        assert (b - a).days >= 90
    assert 3 <= len(kept) <= 10


def test_non_overlapping_subsample_passthrough_when_already_spaced():
    dates = [date(2020, 1, 1), date(2020, 4, 1), date(2020, 7, 1)]
    df = pl.DataFrame({"date": dates, "x": [1, 2, 3]})
    out = non_overlapping_subsample(df, horizon_days=85)
    assert out.height == 3


def _make_pred(n_dates: int, n_names: int, ls_spread: float, horizon: str, step_days: int) -> pl.DataFrame:
    """Synthetic OOS predictions where decile 10 always beats decile 1 by exactly `ls_spread` per period, at a
    rebalance grid finer than the horizon (step_days < horizon's calendar length) -- the overlapping case that
    triggered the bug."""
    rng = np.random.default_rng(0)
    rows = []
    start = date(2015, 1, 1)
    for i in range(n_dates):
        d = start + timedelta(days=i * step_days)
        scores = rng.permutation(n_names).astype(float)  # deterministic decile assignment by permutation
        for j in range(n_names):
            decile_frac = (scores[j] + 1) / n_names
            base = 0.01 * rng.normal()
            excess = base + ls_spread * (decile_frac - 0.5)  # top names get + , bottom get -
            rows.append({"date": d, "security_id": f"S{j}", "score": scores[j], "fwd_return": excess,
                        "fwd_excess_sector": excess, "rank_label": decile_frac})
    return pl.DataFrame(rows)


def test_decile_spread_and_cum_long_short_are_not_inflated_by_overlap():
    """At a weekly grid with a 3M (horizon) forward-return window, summing every week's spread as if the
    windows didn't overlap is exactly the ~3,200% bug. With this fixed evaluator, the annualized spread and the
    final cumulative value must stay in a realistic range for a per-period spread of 2%."""
    pred = _make_pred(n_dates=60, n_names=40, ls_spread=0.02, horizon="3M", step_days=7)
    out = evaluate_predictions(pred, "3M")
    assert abs(out["summary"]["decile_spread_ann"]) < 1.0  # annualized, not a multi-hundred-percent figure
    cum_rows = [r for r in out["series"] if r["metric"] == "cum_long_short"]
    assert cum_rows
    final_cum = cum_rows[-1]["value"]
    assert -0.95 < final_cum < 5.0  # nowhere near +3,200%
    assert "non-overlapping" in out["method_note"]


def test_rank_ic_mean_stays_in_valid_correlation_range():
    pred = _make_pred(n_dates=30, n_names=30, ls_spread=0.03, horizon="1M", step_days=7)
    out = evaluate_predictions(pred, "1M")
    assert -1.0 <= out["summary"]["rank_ic_mean"] <= 1.0
