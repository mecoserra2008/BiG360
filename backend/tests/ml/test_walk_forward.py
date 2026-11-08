"""Purge/embargo correctness of app.ml.train.walk_forward_folds (no data needed: pure date arithmetic)."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.ml.train import HORIZON_DAYS, walk_forward_folds


def _daily_dates(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


@pytest.mark.parametrize("horizon", ["1M", "3M", "6M"])
def test_folds_are_expanding_and_ordered(horizon):
    dates = _daily_dates(date(2005, 1, 3), date(2024, 12, 31))
    folds = walk_forward_folds(dates, horizon, min_train_years=5, test_months=24, step_months=24, embargo_days=10)
    assert len(folds) >= 3
    for f in folds:
        assert f.train_start < f.train_end < f.test_start < f.test_end
    # expanding window: every fold's train_start is the same (start of history), train_end strictly grows
    assert len({f.train_start for f in folds}) == 1
    train_ends = [f.train_end for f in folds]
    assert train_ends == sorted(train_ends) and len(set(train_ends)) == len(train_ends)


@pytest.mark.parametrize("horizon", ["1M", "3M", "6M"])
def test_purge_gap_covers_the_full_label_window(horizon):
    """The gap between train_end and test_start must be >= the label's forward window (horizon trading days,
    converted to calendar days) so that no training sample's [D+1, D+1+h] label window overlaps the test
    period -- otherwise the model would train on information generated inside what is supposed to be unseen
    (OOS) data, a direct form of look-ahead leakage in walk-forward evaluation."""
    dates = _daily_dates(date(2005, 1, 3), date(2024, 12, 31))
    embargo = 10
    folds = walk_forward_folds(dates, horizon, min_train_years=5, test_months=24, step_months=24,
                               embargo_days=embargo)
    assert folds
    min_required_calendar_gap = HORIZON_DAYS[horizon] * 7 / 5  # trading days -> approx calendar days
    for f in folds:
        gap_days = (f.test_start - f.train_end).days
        assert gap_days >= min_required_calendar_gap, (horizon, f)
        assert gap_days >= embargo


def test_longer_horizon_needs_a_bigger_purge_gap():
    dates = _daily_dates(date(2005, 1, 3), date(2024, 12, 31))
    gaps = {}
    for h in ("1M", "3M", "6M"):
        f0 = walk_forward_folds(dates, h, min_train_years=5, test_months=24, step_months=24, embargo_days=10)[0]
        gaps[h] = (f0.test_start - f0.train_end).days
    assert gaps["1M"] < gaps["3M"] < gaps["6M"]


def test_no_folds_when_history_too_short():
    dates = _daily_dates(date(2023, 1, 1), date(2023, 12, 31))  # well under min_train_years
    assert walk_forward_folds(dates, "1M", min_train_years=5) == []


def test_empty_dates_returns_no_folds():
    assert walk_forward_folds([], "1M") == []
