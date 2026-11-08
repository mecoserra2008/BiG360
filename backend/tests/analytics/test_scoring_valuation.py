"""app.analytics.valuation (PIT market cap with split adjustment, USD multiples) and scoring.prune_peer_scores,
against a temp data dir."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from app.core.storage import read_table, write_table

NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _seed(tmp_data) -> None:  # noqa: ANN001
    write_table(pl.DataFrame({
        "security_id": ["S1"], "issuer_id": ["I1"], "security_type": ["common"], "primary_listing_id": ["L1"],
        "display_ticker": ["TST"], "name": ["Test"], "trading_currency": ["USD"], "is_primary_security": [True],
        "is_active": [True], "updated_at": [NOW]}), "securities_master")
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(120)]
    # 2:1 split effective 2024-03-01: raw price halves, split_factor 0.5 before the split
    split = date(2024, 3, 1)
    close = [100.0 if d < split else 50.0 for d in days]
    sf = [0.5 if d < split else 1.0 for d in days]
    write_table(pl.DataFrame({
        "security_id": "S1", "listing_id": "L1", "date": days, "region": "US", "year": [d.year for d in days],
        "close": close, "adj_factor": sf, "split_factor": sf, "adj_close": [c * f for c, f in zip(close, sf, strict=True)],
        "currency": "USD", "close_usd": close, "source_id": "yfinance", "ingested_at": NOW}), "prices_daily")
    base = {"issuer_id": "I1", "currency": "USD", "gaap": "us_gaap", "knowledge_date_estimated": False,
            "coverage": 1.0, "dq_score": 1.0, "computed_at": NOW}
    rows = []
    for basis in ("LOCAL", "USD"):
        rows.append(dict(base, currency_basis=basis, period_kind="TTM", fiscal_year=2023, fiscal_period="TTM",
                         period_end=date(2023, 12, 31), knowledge_date=date(2024, 2, 1), shares_outstanding=10.0,
                         shares_diluted_weighted=10.0, net_income_common=50.0, revenue=500.0, ebitda=100.0,
                         total_debt=200.0, cash_and_st_investments=100.0, total_equity_parent=400.0, fcf=40.0,
                         dividends_paid=10.0, buybacks=5.0, gross_profit=200.0, ebit=80.0))
    write_table(pl.DataFrame(rows), "fundamentals_wide")


def test_valuation_pit_split_adjusted(tmp_data):  # noqa: ANN001
    from app.analytics.valuation import compute_valuation

    _seed(tmp_data)
    v = compute_valuation(pl.DataFrame({"security_id": ["S1"], "issuer_id": ["I1"]}), date(2024, 1, 2),
                          date(2024, 4, 30)).sort("date")
    before = v.filter(pl.col("date") < date(2024, 2, 1))
    assert before["pe_ttm"].null_count() == before.height            # fundamentals not yet public (PIT)
    assert before["market_cap_usd"].null_count() == before.height    # shares not yet known either
    pre = v.filter(pl.col("date") == date(2024, 2, 15)).row(0, named=True)
    post = v.filter(pl.col("date") == date(2024, 3, 15)).row(0, named=True)
    assert pre["market_cap_usd"] == pytest.approx(1000.0)            # 10 sh x 100
    assert post["market_cap_usd"] == pytest.approx(1000.0)           # 20 sh x 50 after the 2:1 split
    assert post["shares_out"] == pytest.approx(20.0)
    assert post["pe_ttm"] == pytest.approx(20.0)
    assert post["enterprise_value_usd"] == pytest.approx(1100.0)
    assert post["ev_ebitda"] == pytest.approx(11.0)
    assert post["fcf_yield"] == pytest.approx(0.04)
    assert post["shareholder_yield"] == pytest.approx(0.015)
    assert post["size_bucket"] == "small"


def test_prune_peer_scores_retention(tmp_data):  # noqa: ANN001
    from app.analytics.scoring import prune_peer_scores

    ds = [date(2024, 1, 1) + timedelta(days=i) for i in range(90)]
    df = pl.DataFrame({"as_of_date": ds, "as_of_month": [d.strftime("%Y-%m") for d in ds], "security_id": "S1",
                       "issuer_id": "I1", "metric_id": "roe", "factor_id": "profitability", "imputed": False,
                       "computed_at": NOW})
    write_table(df, "peer_scores")
    D = date(2024, 3, 30)
    removed = prune_peer_scores(D, tmp_data)
    left = read_table("peer_scores")["as_of_date"].sort().to_list()
    old = [d for d in left if d < D - timedelta(days=35)]
    assert removed > 0
    assert all(d.weekday() == 4 or (d + timedelta(days=1)).month != d.month for d in old)
    assert date(2024, 1, 31) in left and date(2024, 1, 5) in left and date(2024, 1, 4) not in left
    assert all(d in left for d in ds if d >= D - timedelta(days=35))
