"""ingest-market: rate limiter, Polygon client, split/dividend adjustment, reconciliation, finalize (no network)."""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import polars as pl
import pytest
import respx

from app.core import config as config_mod
from app.core.ratelimit import (
    DEFAULT_LIMITS,
    RateLimit,
    SharedTokenBucket,
    configure_limiter,
    get_limiter,
    reset_limiters,
)
from app.core.schema import conform
from app.core.storage import read_table, write_table
from app.normalize import prices as P

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------------------------------------ rate limiter


def test_polygon_default_limit_is_at_most_5_per_minute():
    lim = DEFAULT_LIMITS["polygon"]
    assert lim.rate_per_sec * 60 <= 5.0 + 1e-9
    assert lim.burst == 1


def test_shared_bucket_state_persists_across_instances(tmp_path):
    lim = RateLimit.per_minute(5, burst=1)
    a = SharedTokenBucket(lim, "polygon", tmp_path)
    assert a.acquire(timeout=0) is True
    # a brand-new bucket (= another process / a later run) sees the consumed token
    b = SharedTokenBucket(lim, "polygon", tmp_path)
    t0 = time.monotonic()
    assert b.acquire(timeout=0.2) is False
    assert time.monotonic() - t0 < 1.0
    assert (tmp_path / "polygon.json").exists()


def test_shared_bucket_penalize_blocks_other_instances(tmp_path):
    lim = RateLimit(100.0, 1)
    a = SharedTokenBucket(lim, "polygon", tmp_path)
    a.penalize(30)
    b = SharedTokenBucket(lim, "polygon", tmp_path)
    assert b.acquire(timeout=0.05) is False


@pytest.fixture()
def fast_polygon(tmp_data, monkeypatch):
    monkeypatch.setenv("POLYGON_APIKEY", "test-key-123456")
    config_mod.reset_settings_cache()
    reset_limiters()
    configure_limiter("polygon", RateLimit(1000.0, 10))
    yield config_mod.get_settings()
    reset_limiters()


@respx.mock
def test_polygon_client_429_backoff_bearer_and_no_key_in_url(fast_polygon):
    from app.ingestion.polygon_prices import HOSTS, PolygonClient

    payload = json.loads((FIX / "polygon_grouped_2026-10-08.json").read_text())
    route = respx.get(f"{HOSTS[0]}/v2/aggs/grouped/locale/us/market/stocks/2026-10-08").mock(
        side_effect=[httpx.Response(429, json={"status": "ERROR"}), httpx.Response(200, json=payload)])
    pc = PolygonClient(fast_polygon, penalty_s=0.001)
    out = pc.grouped_daily(date(2026, 10, 8))
    assert out["resultsCount"] == len(payload["results"])
    assert route.call_count == 2 and pc.n_429 == 1
    req = route.calls[-1].request
    assert req.headers["Authorization"] == "Bearer test-key-123456"
    assert "test-key" not in str(req.url) and "apiKey" not in str(req.url)
    assert "adjusted=false" in str(req.url)
    # the 429 drained the process-wide polygon bucket
    assert get_limiter("polygon").total_acquired >= 2


@respx.mock
def test_polygon_client_auth_error_is_not_retried(fast_polygon):
    from app.ingestion.polygon_prices import HOSTS, PolygonAuthError, PolygonClient

    route = respx.get(f"{HOSTS[0]}/v3/reference/tickers/AAPL").mock(
        return_value=httpx.Response(403, json={"status": "NOT_AUTHORIZED", "message": "plan"}))
    with pytest.raises(PolygonAuthError):
        PolygonClient(fast_polygon, penalty_s=0.001).get("/v3/reference/tickers/AAPL")
    assert route.call_count == 1


def test_grouped_to_rows_conforms_to_staging():
    from app.ingestion.polygon_prices import grouped_to_rows

    payload = json.loads((FIX / "polygon_grouped_2026-10-08.json").read_text())
    df = conform(grouped_to_rows(payload, date(2026, 10, 8)), "prices_staging")
    assert set(df.get_column("price_basis").unique()) == {"raw"}
    aapl = df.filter(pl.col("symbol") == "AAPL").row(0, named=True)
    assert aapl["close"] > 0 and aapl["trades"] > 0 and aapl["currency"] == "USD" and aapl["year"] == 2026


# ------------------------------------------------------------------------------------------------ adjustments


def _bars(closes, start=date(2026, 1, 5), key="S1"):
    days = [start + timedelta(days=i) for i in range(len(closes))]
    return pl.DataFrame({"security_id": [key] * len(closes), "date": days, "close": closes})


def test_split_and_dividend_factors():
    bars = _bars([100.0, 102.0, 104.0, 52.0, 50.0, 49.0])
    d = bars.get_column("date").to_list()
    actions = pl.DataFrame({"security_id": ["S1", "S1"], "ex_date": [d[3], d[5]],
                            "action_type": ["split", "cash_dividend"], "ratio": [2.0, None], "amount": [None, 1.0]})
    out = P.compute_adjustment_factors(bars, actions).sort("date")
    sf = out.get_column("split_factor").to_list()
    af = out.get_column("adj_factor").to_list()
    assert sf == [0.5, 0.5, 0.5, 1.0, 1.0, 1.0]
    f = 1 - 1.0 / 50.0  # dividend 1.0 against the previous raw close 50
    assert af[:3] == pytest.approx([0.5 * f] * 3)
    assert af[3:5] == pytest.approx([f, f])
    assert af[5] == 1.0
    adj = (out.get_column("close") * out.get_column("adj_factor")).to_list()
    assert adj[2] == pytest.approx(52.0 * f)  # pre-split 104 -> 52 after the 2:1 split, then dividend-adjusted


def test_bad_dividend_is_ignored_and_flagged():
    bars = _bars([10.0, 10.0, 10.0])
    actions = pl.DataFrame({"security_id": ["S1"], "ex_date": [bars["date"][2]], "action_type": ["cash_dividend"],
                            "ratio": [None], "amount": [25.0]}, schema_overrides={"ratio": pl.Float64})
    out = P.compute_adjustment_factors(bars, actions)
    assert out.get_column("adj_factor").to_list() == [1.0, 1.0, 1.0]
    assert out.filter(pl.col("__div_bad").fill_null(False)).height == 1


def test_unadjust_split_basis_restores_raw_prints():
    # Yahoo split-adjusted series around NVDA's 10:1 split (2024-06-10)
    days = [date(2024, 6, 6), date(2024, 6, 7), date(2024, 6, 10)]
    adj = pl.DataFrame({"symbol": ["NVDA"] * 3, "date": days, "close": [120.998, 120.888, 121.79],
                        "volume": [664_696_000.0, 412_386_000.0, 313_434_100.0]})
    splits = pl.DataFrame({"symbol": ["NVDA"], "ex_date": [date(2024, 6, 10)], "ratio": [10.0]})
    raw = P.unadjust_split_basis(adj, splits).sort("date")
    assert raw.get_column("close").to_list() == pytest.approx([1209.98, 1208.88, 121.79])
    assert raw.get_column("volume").to_list() == pytest.approx([66_469_600.0, 41_238_600.0, 313_434_100.0])


def test_yf_long_to_staging_raw_basis_and_dividends():
    from app.ingestion.yf_prices import long_to_staging

    days = [date(2024, 6, 6), date(2024, 6, 7), date(2024, 6, 10), date(2024, 6, 11)]
    long = pl.DataFrame({"symbol": ["NVDA"] * 4, "date": days, "open": [121.0, 120.0, 120.4, 121.8],
                         "high": [122.0, 121.0, 123.1, 122.0], "low": [119.0, 119.5, 117.0, 118.7],
                         "close": [120.998, 120.888, 121.79, 120.91], "adj_close": [120.96, 120.85, 121.75, 120.91],
                         "volume": [6.6e8, 4.1e8, 3.1e8, 2.2e8], "dividend": [0.0, 0.0, 0.0, 0.01],
                         "split": [0.0, 0.0, 10.0, 0.0]})
    prices, actions = long_to_staging(long, {"NVDA": "USD"}, set())
    prices = conform(prices, "prices_staging")
    actions = conform(actions, "corporate_actions_staging")
    p = prices.sort("date")
    assert p.get_column("close").to_list()[:2] == pytest.approx([1209.98, 1208.88])
    assert p.get_column("price_basis").unique().to_list() == ["raw"]
    split = actions.filter(pl.col("action_type") == "split").row(0, named=True)
    assert split["ratio"] == 10.0 and split["ex_date"] == date(2024, 6, 10)
    div = actions.filter(pl.col("action_type") == "cash_dividend").row(0, named=True)
    assert div["amount"] == pytest.approx(0.01) and div["amount_basis"] == "raw"


# ------------------------------------------------------------------------------------------------ reconciliation


def test_reconcile_sources_flags_beyond_tolerance():
    d1, d2 = date(2026, 10, 7), date(2026, 10, 8)
    bars = pl.DataFrame({"security_id": ["A", "A", "A", "A", "B"], "date": [d1, d1, d2, d2, d1],
                         "source_id": ["polygon", "yfinance"] * 2 + ["yfinance"],
                         "close": [100.0, 100.5, 100.0, 103.0, 50.0]})
    diffs, summ = P.reconcile_sources(bars, tolerance=0.01)
    assert summ["overlap_rows"] == 2 and summ["flagged_rows"] == 1
    flagged = diffs.filter(pl.col("disagree")).row(0, named=True)
    assert flagged["date"] == d2 and flagged["rel_diff"] == pytest.approx(0.03)


def _yf_rows(symbol, rows, currency="USD"):
    now = datetime.now(UTC)
    return pl.DataFrame([{"source_id": "yfinance", "year": d.year, "symbol": symbol, "date": d, "open": c, "high": c * 1.01,
                          "low": c * 0.99, "close": c, "adj_close": c, "volume": 1000.0, "vwap": None, "trades": None,
                          "currency": currency, "price_basis": "raw", "is_benchmark": False, "ingested_at": now}
                         for d, c in rows])


def test_build_prices_end_to_end(tmp_data):
    from app.ingestion.polygon_prices import grouped_to_rows

    payload = json.loads((FIX / "polygon_grouped_2026-10-08.json").read_text())
    poly = grouped_to_rows(payload, date(2026, 10, 8))
    aapl_pg = poly.filter(pl.col("symbol") == "AAPL").item(0, "close")
    yf = pl.concat([
        _yf_rows("AAPL", [(date(2026, 10, 7), aapl_pg * 0.99), (date(2026, 10, 8), aapl_pg * 1.05)]),
        _yf_rows("SHEL.L", [(date(2026, 10, 7), 3700.0), (date(2026, 10, 8), 3780.5)], currency="GBp"),
    ])
    staged = conform(pl.concat([conform(poly, "prices_staging"), conform(yf, "prices_staging")]), "prices_staging")
    # FX for GBP so close_usd is filled
    write_table(pl.DataFrame({"date": [date(2026, 10, 7), date(2026, 10, 8)], "year": [2026, 2026],
                              "currency": ["GBP", "GBP"], "usd_per_unit": [1.33, 1.32], "units_per_usd": [1 / 1.33, 1 / 1.32],
                              "source_id": ["ecb", "ecb"], "ingested_at": [datetime.now(UTC)] * 2}), "fx_rates")
    uni = P.load_universe(tmp_data)
    out, rep = P.build_prices(staged, uni, None, settings=tmp_data)
    out = conform(out, "prices_daily")
    a = out.filter(pl.col("security_id") == "P:AAPL").sort("date")
    assert a.get_column("source_id").to_list() == ["yfinance", "polygon"]  # polygon wins on overlap
    assert "source_disagreement" in a.row(1, named=True)["dq_flags"]
    assert rep["reconciliation"]["flagged_rows"] == 1
    shel = out.filter(pl.col("security_id") == "P:SHEL.L").sort("date").row(1, named=True)
    assert shel["close"] == pytest.approx(37.805) and shel["currency"] == "GBP"  # GBp -> GBP
    assert shel["close_usd"] == pytest.approx(37.805 * 1.32)
    assert out.filter(pl.col("security_id") == "ETF:SPY").height == 1  # benchmark ETF from grouped-daily
    assert out.filter(pl.col("security_id").str.contains("W$")).height == 0  # warrants not in universe dropped
    assert rep["unresolved_symbols"] > 0


def test_finalize_writes_prices_daily_and_replaces_provisional(tmp_data):
    from app.ingestion.polygon_prices import grouped_to_rows

    payload = json.loads((FIX / "polygon_grouped_2026-10-08.json").read_text())
    write_table(grouped_to_rows(payload, date(2026, 10, 8)), "prices_staging")
    rep = P.finalize(tmp_data)
    assert rep["rows_written"] > 0
    df = read_table("prices_daily")
    assert df.filter(pl.col("security_id") == "P:NVDA").height == 1
    assert df.get_column("region").unique().to_list() == ["US"]
    # reports land in data/logs/reports
    assert list((tmp_data.logs_dir / "reports").glob("prices_finalize_*.json"))


def test_market_metadata_helpers():
    assert P.yf_suffix("7203.T") == ".T" and P.yf_suffix("BRK-B") == "" and P.yf_suffix("^GSPC") == ""
    assert P.market_for_yf("EDP.LS").currency == "EUR" and P.market_for_yf("SHEL.L").currency == "GBp"
    assert P.country_to_region("PT") == "EU" and P.country_to_region("GB") == "UK" and P.country_to_region("BR") == "LATAM"
    assert P.polygon_to_yf("BRK.B") == "BRK-B" and P.yf_to_polygon("BRK-B") == "BRK.B"
    assert P.benchmark_security_id("^GSPC") == "IDX:^GSPC" and P.benchmark_security_id("XLK") == "ETF:XLK"
    assert P.benchmark_security_id("^PSI20") == "IDX:PSI20.LS"


def test_trading_calendar_awareness():
    ss = P.trading_sessions("XNYS", date(2026, 7, 1), date(2026, 7, 7))
    assert ss is not None and date(2026, 7, 3) not in ss and date(2026, 7, 6) in ss  # Independence Day observed
    assert P.last_completed_session("XNYS", date(2026, 10, 12)) == date(2026, 10, 9)


def test_staging_upsert_twice_with_partition_column_in_pk(tmp_data):
    """prices_staging PK includes its partition column source_id: the second upsert into an existing partition must
    dedupe on the in-file key columns (regression for app.core.storage.write_table)."""
    from app.ingestion.polygon_prices import grouped_to_rows

    payload = json.loads((FIX / "polygon_grouped_2026-10-08.json").read_text())
    rows = grouped_to_rows(payload, date(2026, 10, 8))
    write_table(rows, "prices_staging")
    write_table(rows.with_columns(pl.col("close") * 2), "prices_staging")
    df = read_table("prices_staging")
    assert df.height == rows.height
    a = df.filter(pl.col("symbol") == "AAPL").item(0, "close")
    assert a == pytest.approx(rows.filter(pl.col("symbol") == "AAPL").item(0, "close") * 2)  # new rows win
