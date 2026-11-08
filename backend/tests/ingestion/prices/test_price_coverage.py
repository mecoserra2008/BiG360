"""Recorded-fixture tests for the price-coverage parsers (TWSE/TPEx, BaoStock ex-date actions, B3 listed seeds,
TW company masters) and the identity/finalize wiring. Fixtures recorded live on 2026-10-11 (trimmed)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.ingestion.b3_cotahist import parse_cotahist
from app.ingestion.baostock_prices import bs_rows, derive_cn_actions
from app.ingestion.price_coverage_ext import parse_b3_listed, parse_tw_companies, tw_isin
from app.ingestion.twse_prices import (
    parse_tpex_day,
    parse_tpex_exrights,
    parse_twse_day,
    parse_twse_exrights,
    roc_date,
)
from app.normalize.prices import compute_adjustment_factors

FX = Path(__file__).parent / "fixtures"


def _j(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))


def test_roc_dates():
    assert roc_date("113年01月04日") == date(2024, 1, 4)
    assert roc_date("113/07/01") == date(2024, 7, 1)
    assert roc_date("1151008") == date(2026, 10, 8)
    assert roc_date("96/07/02") == date(2007, 7, 2)
    assert roc_date("garbage") is None


def test_twse_day_keeps_ordinary_shares_only():
    df = parse_twse_day(_j("twse_mi_index_20241008.json"), date(2024, 10, 8))
    syms = set(df.get_column("symbol").to_list())
    assert {"1101.TW", "2330.TW", "2454.TW", "6442.TW"} <= syms
    assert "0050.TW" not in syms and "2881A.TW" not in syms  # ETF / preferred dropped
    r = df.filter(pl.col("symbol") == "2330.TW").row(0, named=True)
    assert (r["open"], r["high"], r["low"], r["close"]) == (1000.0, 1010.0, 997.0, 1010.0)
    assert r["volume"] == 35344059.0 and r["trades"] == 42227 and r["currency"] == "TWD"
    assert r["source_id"] == "twse" and r["price_basis"] == "raw" and r["year"] == 2024
    assert 997 <= r["vwap"] <= 1010


def test_twse_holiday_is_empty():
    assert parse_twse_day(_j("twse_mi_index_holiday.json"), date(2024, 10, 12)).is_empty()


def test_tpex_day_both_layouts_and_date_echo():
    new = parse_tpex_day(_j("tpex_daily_20241008.json"), date(2024, 10, 8))
    assert new.height >= 3 and new.get_column("symbol").str.ends_with(".TWO").all()
    assert new.filter(pl.col("symbol") == "6532.TWO").get_column("close").item() == 72.4
    old = parse_tpex_day(_j("tpex_daily_20070702.json"), date(2007, 7, 2))  # 17-column layout + 管理股票 table
    assert old.height >= 1 and old.get_column("date").unique().to_list() == [date(2007, 7, 2)]
    # an endpoint that silently returns another day must not be staged under the requested date
    assert parse_tpex_day(_j("tpex_daily_20241008.json"), date(2024, 10, 9)).is_empty()


def test_twse_exrights_cash_and_stock():
    ca = parse_twse_exrights(_j("twse_twt49u_202401.json"))
    mtk = ca.filter(pl.col("symbol") == "2454.TW")
    assert mtk.get_column("action_type").to_list() == ["cash_dividend"]
    assert mtk.get_column("amount").item() == pytest.approx(953.0 - 928.4)
    stock = ca.filter(pl.col("symbol") == "6442.TW")
    assert stock.get_column("action_type").to_list() == ["stock_dividend"]
    assert stock.get_column("ratio").item() == pytest.approx(67.2 / 65.94, rel=1e-5)
    assert "0050.TW" not in ca.get_column("symbol").to_list()


def test_tpex_exrights_cash_and_rights():
    ca = parse_tpex_exrights(_j("tpex_exdailyq_202407.json"))
    cash = ca.filter(pl.col("symbol") == "3373.TWO")
    assert cash.get_column("amount").item() == pytest.approx(1.0)
    rights = ca.filter(pl.col("symbol") == "4188.TWO")  # cash capital increase: stock-like step only
    assert rights.get_column("action_type").to_list() == ["stock_dividend"]
    assert rights.get_column("ratio").item() == pytest.approx(27.6 / 26.87, rel=1e-5)


def test_tw_exrights_factor_is_exact():
    """cash + stock rows of one event must multiply to ref/close_before in compute_adjustment_factors."""
    ca = parse_twse_exrights(_j("twse_twt49u_202401.json")).filter(pl.col("symbol") == "6442.TW")
    bars = pl.DataFrame({"security_id": ["x", "x"], "date": [date(2024, 1, 9), date(2024, 1, 10)],
                         "close": [67.2, 66.0]})
    acts = ca.select(pl.lit("x").alias("security_id"), "ex_date", "action_type", "ratio", "amount")
    f = compute_adjustment_factors(bars, acts).sort("date")
    assert f.get_column("adj_factor").to_list()[0] == pytest.approx(65.94 / 67.2, rel=1e-6)


def test_baostock_preclose_actions_decompose_bonus_issue():
    """浦发银行 2002-08-22: 10转5派2 (5 bonus per 10 + CNY 0.2 cash pre-tax) -> stock 1.5 + cash ~0.2."""
    raw = _j("baostock_600000_200208.json")
    bars = bs_rows("600000.SS", raw["rows"], raw["fields"], keep_preclose=True)
    assert "preclose" not in bs_rows("600000.SS", raw["rows"], raw["fields"]).columns
    assert "preclose" in bars.columns and bars.get_column("currency").unique().to_list() == ["CNY"]
    acts = derive_cn_actions(bars.select("symbol", "date", "close", "preclose"))
    ev = acts.filter(pl.col("ex_date") == date(2002, 8, 22))
    st = ev.filter(pl.col("action_type") == "stock_dividend").get_column("ratio").item()
    cash = ev.filter(pl.col("action_type") == "cash_dividend").get_column("amount").item()
    assert st == pytest.approx(1.5)
    assert cash == pytest.approx(0.2, abs=0.01)
    # total step equals close_{t-1} / preclose_t exactly
    b = bars.sort("date")
    prev = b.filter(pl.col("date") < date(2002, 8, 22)).get_column("close").to_list()[-1]
    pre = b.filter(pl.col("date") == date(2002, 8, 22)).get_column("preclose").item()
    a = ev.select(pl.lit("x").alias("security_id"), "ex_date", "action_type", "ratio", "amount")
    f = compute_adjustment_factors(b.select(pl.lit("x").alias("security_id"), "date", "close"), a).sort("date")
    first_before = f.filter(pl.col("date") < date(2002, 8, 22)).get_column("adj_factor").to_list()[-1]
    assert first_before == pytest.approx(pre / prev, rel=1e-6)


def test_baostock_pure_cash_dividend():
    bars = pl.DataFrame({"symbol": ["A.SS"] * 3, "date": [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)],
                         "close": [10.0, 9.8, 9.9], "preclose": [9.9, 9.5, 9.8]})
    acts = derive_cn_actions(bars)
    assert acts.height == 1 and acts.get_column("action_type").item() == "cash_dividend"
    assert acts.get_column("amount").item() == pytest.approx(0.5)


def test_b3_listed_seeds_and_prices():
    text = (FX / "cotahist_trim.txt").read_text(encoding="latin-1")
    seeds = parse_b3_listed(text)
    by = {r["ticker"]: r for r in seeds.iter_rows(named=True)}
    assert {"PETR3", "PETR4", "VALE3", "TAEE11"} <= set(by)
    assert "HGLG11" not in by  # FII (CODBDI 12) is not an equity seed
    assert by["PETR4"]["issuer_group"] == by["PETR3"]["issuer_group"] == "B3:PETR"
    assert by["PETR4"]["share_class"] == "PN" and by["PETR4"]["isin"].startswith("BRPETR")
    assert by["TAEE11"]["share_class"] == "UNT" and by["PETR4"]["yf_ticker"] == "PETR4.SA"
    px = parse_cotahist(text)
    assert px.filter(pl.col("symbol") == "PETR4.SA").height >= 1


def test_tw_company_seeds():
    d = _j("tw_company_lists.json")
    seeds = parse_tw_companies(d["twse"], d["tpex"])
    r = seeds.filter(pl.col("ticker") == "1101").row(0, named=True)
    assert r["mic"] == "XTAI" and r["yf_ticker"] == "1101.TW" and r["name"] == "TCC" and r["native_industry"] == "01"
    assert r["isin"] == tw_isin("1101")
    o = seeds.filter(pl.col("ticker") == "1240").row(0, named=True)
    assert o["mic"] == "ROCO" and o["yf_ticker"] == "1240.TWO" and o["name"] == "MORNSUN"
    assert tw_isin("2330") == "TW0002330008"


def test_coverage_records_dedupe_and_flags():
    from app.normalize.identity import coverage_records

    curated = [{"local_symbol": "600519", "mic": "XSHG", "yf_ticker": "600519.SS", "isin": None}]
    cov = pl.DataFrame({"ticker": ["600519", "600000", "600000"], "mic": ["XSHG"] * 3,
                        "yf_ticker": ["600519.SS", "600000.SS", "600000.SS"], "name": ["Moutai", "SPDB", "SPDB"],
                        "country": ["CN"] * 3, "region": ["CN"] * 3, "native_industry": [None, "J66货币金融服务", None],
                        "coverage_source": ["baostock"] * 3})
    for c in ("isin", "adr_ticker", "issuer_group", "share_class", "notes", "lei", "fye_mmdd"):
        cov = cov.with_columns(pl.lit(None, pl.Utf8).alias(c))
    out = coverage_records(curated, cov)
    assert [r["yf_ticker"] for r in out] == ["600000.SS"]
    assert out[0]["coverage_only"] and out[0]["csrc_code"].startswith("J66") and out[0]["cn_code"] == "SSE:600000"
    assert curated[0]["coverage"] is True and not curated[0].get("coverage_only")


def test_official_suffixes_skipped_on_yahoo(monkeypatch):
    import app.ingestion.yf_prices as yp

    uni = pl.DataFrame({"yf_ticker": ["600519.SS", "600000.SS", "2330.TW", "6488.TWO", "PETR4.SA", "7203.T", "AAPL"]})
    monkeypatch.setattr(yp, "load_universe", lambda s=None: uni)
    monkeypatch.setattr(yp, "_curated_yf", lambda s=None: {"600519.SS", "2330.TW", "PETR4.SA"})
    ad = yp.YFinancePricesAdapter.__new__(yp.YFinancePricesAdapter)
    ad.settings = None
    got = ad.universe_symbols()
    assert got == ["600519.SS", "2330.TW", "PETR4.SA", "7203.T", "AAPL"]


def test_delisted_window_resolution():
    """A delisted listing takes bars up to its valid_to; the active listing reusing the symbol only after it."""
    from app.normalize.prices import _RES_SCHEMA, resolve_symbols

    base = {c: None for c in _RES_SCHEMA}
    uni = pl.DataFrame([
        {**base, "key": "ABC", "yf_ticker": "ABC", "polygon_ticker": "ABC", "security_id": "old", "region": "US",
         "price_divisor": 1.0, "is_benchmark": False, "provisional": False, "px_to": date(2025, 3, 31)},
        {**base, "key": "ABC", "yf_ticker": "ABC", "polygon_ticker": "ABC", "security_id": "new", "region": "US",
         "price_divisor": 1.0, "is_benchmark": False, "provisional": False, "px_from": date(2025, 4, 1)},
    ], schema=_RES_SCHEMA)
    st = pl.DataFrame({"source_id": ["polygon", "polygon", "yfinance"], "symbol": ["ABC", "ABC", "ABC"],
                       "date": [date(2025, 3, 31), date(2025, 6, 2), date(2025, 6, 3)], "close": [1.0, 2.0, 2.1]})
    got = {(r["date"], r["security_id"]) for r in resolve_symbols(st, uni).iter_rows(named=True)}
    assert got == {(date(2025, 3, 31), "old"), (date(2025, 6, 2), "new"), (date(2025, 6, 3), "new")}


def test_select_delisted_drops_renames_and_requires_bars():
    from app.ingestion.price_coverage_delisted import select_delisted

    inactive = pl.DataFrame({
        "ticker": ["GONE", "FB", "THIN"], "name": ["Gone Inc", "Facebook", "Thin Co"],
        "primary_exchange": ["XNYS", "XNAS", "XNAS"], "type": ["CS", "CS", "CS"],
        "cik": ["123", "1326801", "999"], "composite_figi": ["BBG1", "BBG_META", "BBG3"],
        "share_class_figi": ["S1", "S_META", "S3"], "delisted_utc": ["2025-05-01T00:00:00Z"] * 3,
        "currency_name": ["usd"] * 3, "delisted_date": [date(2025, 5, 1)] * 3})
    listings = pl.DataFrame({"polygon_ticker": ["META"], "valid_to": [None]}, schema={"polygon_ticker": pl.Utf8,
                                                                                      "valid_to": pl.Date})
    secs = pl.DataFrame({"figi": ["S_META"], "is_active": [True]})
    issuers = pl.DataFrame({"cik": ["0001326801"], "is_active": [True]})
    days = [date(2025, 1, 1) + __import__("datetime").timedelta(days=i) for i in range(40)]
    staged = pl.DataFrame({"symbol": ["GONE"] * 40 + ["THIN"] * 5, "date": days + days[:5]})
    sel = select_delisted(inactive, listings, secs, issuers, staged, date(2024, 10, 1), min_bars=20)
    assert sel.get_column("ticker").to_list() == ["GONE"]
    assert sel.get_column("n_bars").item() == 40 and sel.get_column("cik").item() == "0000000123"
