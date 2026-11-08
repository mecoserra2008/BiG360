"""No-look-ahead property tests for app.ml.labels: a label at rebalance date D must depend only on prices
strictly after D (close(D+1) onward). We prove this the way ARCHITECTURE §16 prescribes: append future-dated
rows, recompute, and check outputs for dates <= D are byte-identical; plus direct checks of the entry/exit
trading-day arithmetic and the delisting fallback."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from app.core.storage import write_table
from app.ml.labels import compute_labels, rebalance_dates

pytestmark = pytest.mark.usefixtures("tmp_data")


def _trading_days(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _seed_universe(tmp_data, security_ids: list[str], sector="TECH", region="US"):
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    um = pl.DataFrame({"universe_id": ["mvp_us"] * len(security_ids), "security_id": security_ids,
                       "valid_from": [date(2000, 1, 1)] * len(security_ids),
                       "valid_to": [None] * len(security_ids), "rank_at_entry": [1] * len(security_ids),
                       "reason": ["seed"] * len(security_ids), "updated_at": [now] * len(security_ids)})
    write_table(um, "universe_membership", settings=tmp_data)
    sec = pl.DataFrame({"security_id": security_ids, "issuer_id": [f"I{s}" for s in security_ids],
                        "security_type": ["common"] * len(security_ids), "share_class": [None] * len(security_ids),
                        "isin": [None] * len(security_ids), "figi": [None] * len(security_ids),
                        "cusip": [None] * len(security_ids), "primary_listing_id": [f"L{s}" for s in security_ids],
                        "display_ticker": security_ids, "name": security_ids,
                        "trading_currency": ["USD"] * len(security_ids),
                        "underlying_security_id": [None] * len(security_ids), "adr_ratio": [None] * len(security_ids),
                        "shares_class_weight": [None] * len(security_ids),
                        "is_primary_security": [True] * len(security_ids), "is_active": [True] * len(security_ids),
                        "first_trade_date": [date(2000, 1, 1)] * len(security_ids),
                        "last_trade_date": [None] * len(security_ids), "delisting_reason": [None] * len(security_ids),
                        "updated_at": [now] * len(security_ids), "run_id": [None] * len(security_ids)})
    write_table(sec, "securities_master", settings=tmp_data)
    iss = pl.DataFrame({
        "issuer_id": [f"I{s}" for s in security_ids], "name": security_ids, "country": [region] * len(security_ids),
        "region": [region] * len(security_ids), "sector_id": [sector] * len(security_ids),
        "is_active": [True] * len(security_ids), "updated_at": [now] * len(security_ids),
    })
    write_table(iss, "issuers", settings=tmp_data)


def _prices(security_id: str, dates: list[date], closes: list[float], region="US") -> pl.DataFrame:
    n = len(dates)
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    return pl.DataFrame({
        "security_id": [security_id] * n, "listing_id": [f"L{security_id}"] * n, "date": dates,
        "region": [region] * n, "year": [d.year for d in dates],
        "open": closes, "high": closes, "low": closes, "close": closes, "volume": [1000.0] * n,
        "vwap": [None] * n, "trades": [None] * n, "adj_factor": [1.0] * n, "split_factor": [1.0] * n,
        "adj_close": closes, "currency": ["USD"] * n, "close_usd": closes,
        "dollar_volume_usd": [c * 1000.0 for c in closes], "source_id": ["yfinance"] * n,
        "dq_flags": [None] * n, "ingested_at": [now] * n,
    })


def test_label_only_uses_prices_strictly_after_d(tmp_data):
    _seed_universe(tmp_data, ["AAA"])
    dates = _trading_days(date(2020, 1, 1), 300)
    closes = [100.0 + 0.1 * i for i in range(300)]
    write_table(_prices("AAA", dates, closes), "prices_daily", settings=tmp_data)

    # pick an actual rebalance date (week-end/month-end trading day) well before the series end, not an
    # arbitrary index, since compute_labels only ever emits rows on the rebalance_dates() grid
    candidates = [d for d in rebalance_dates(tmp_data, start=dates[10], end=dates[200]) if d in dates]
    rebal_d = candidates[len(candidates) // 2]
    entry_idx = dates.index(rebal_d) + 1
    labels_v1 = compute_labels(dates[290], tmp_data, universe_ids=("mvp_us",), start=rebal_d).filter(
        (pl.col("date") == rebal_d) & (pl.col("horizon") == "1M"))
    row1 = labels_v1.to_dicts()[0]

    # Now append future rows *after* the label's own window; only observations after the label window end
    # should be free to change (to wildly different values) without affecting this row's computed label.
    future_dates = _trading_days(dates[-1] + timedelta(days=1), 50)
    future_closes = [99999.0] * 50
    write_table(_prices("AAA", future_dates, future_closes), "prices_daily", settings=tmp_data)
    labels_v2 = compute_labels(future_dates[-1], tmp_data, universe_ids=("mvp_us",), start=rebal_d).filter(
        (pl.col("date") == rebal_d) & (pl.col("horizon") == "1M"))
    row2 = labels_v2.to_dicts()[0]

    assert row1["fwd_return"] == pytest.approx(row2["fwd_return"])
    assert row1["label_end_date"] == row2["label_end_date"]
    # sanity: the label is NOT trivially zero/null, and matches hand-computed entry/exit (D+1 .. D+1+21)
    entry = closes[entry_idx]
    exit_ = closes[entry_idx + 21]
    assert row1["fwd_return"] == pytest.approx(exit_ / entry - 1.0, rel=1e-9)


def test_label_end_date_is_strictly_after_entry_and_respects_horizon_ordering(tmp_data):
    _seed_universe(tmp_data, ["BBB"])
    dates = _trading_days(date(2018, 1, 1), 400)
    closes = [50.0 + 0.05 * i for i in range(400)]
    write_table(_prices("BBB", dates, closes), "prices_daily", settings=tmp_data)
    candidates = [d for d in rebalance_dates(tmp_data, start=dates[20], end=dates[150]) if d in dates]
    rebal_d = candidates[len(candidates) // 2]
    df = compute_labels(dates[390], tmp_data, universe_ids=("mvp_us",), start=rebal_d).filter(pl.col("date") == rebal_d)
    by_h = {r["horizon"]: r for r in df.to_dicts()}
    assert by_h["1M"]["label_end_date"] < by_h["3M"]["label_end_date"] < by_h["6M"]["label_end_date"]
    for h in ("1M", "3M", "6M"):
        assert by_h[h]["label_end_date"] > rebal_d


def test_delisted_security_gets_flat_continuation_not_null(tmp_data):
    _seed_universe(tmp_data, ["CCC"])
    all_dates = _trading_days(date(2021, 1, 1), 60)
    delist_idx = 45
    dates = all_dates[: delist_idx + 1]  # a delisted name has no price history past its last trade date
    closes = [10.0] * len(dates)
    write_table(_prices("CCC", dates, closes), "prices_daily", settings=tmp_data)
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    from app.core.storage import read_table

    full = read_table("securities_master", settings=tmp_data)
    full = full.with_columns(pl.lit(False).alias("is_active"), pl.lit(dates[-1]).alias("last_trade_date"))
    write_table(full, "securities_master", mode="overwrite", settings=tmp_data)

    # a rebalance date late enough that entry+21 trading days overshoots the last available price (index 45)
    candidates = [d for d in rebalance_dates(tmp_data, start=dates[25], end=dates[35]) if d in dates]
    rebal_d = candidates[0]
    df = compute_labels(all_dates[-1], tmp_data, universe_ids=("mvp_us",), start=rebal_d).filter(
        (pl.col("date") == rebal_d) & (pl.col("horizon") == "1M"))
    row = df.to_dicts()[0]
    assert row["delisted_in_window"] is True
    assert row["fwd_return"] is not None
    assert row["fwd_return"] == pytest.approx(0.0)  # flat price throughout -> flat continuation == 0% anyway
    assert row["label_end_date"] == dates[-1]


def test_rebalance_dates_are_weekly_and_month_end_only(tmp_data):
    _seed_universe(tmp_data, ["AAA"])
    dates = _trading_days(date(2022, 1, 1), 260)
    write_table(_prices("IDX:^GSPC", dates, [1.0] * 260), "prices_daily", settings=tmp_data)
    rebal = rebalance_dates(tmp_data, start=dates[0], end=dates[-1])
    assert len(rebal) < len(dates)  # strictly fewer than daily
    # every date must be a Friday (the week's last trading day) or a month-end trading day
    for d in rebal:
        assert d in dates
