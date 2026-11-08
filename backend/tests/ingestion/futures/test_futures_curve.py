"""Futures term-structure ingestion (roadmap v2 #5a): symbols, listing cycles, expiry rules, bar cleaning, paced
fetching with throttle handling, and the end-to-end write into futures_curve / futures_contracts (offline)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import polars as pl
import pytest

from app.core.storage import read_table
from app.ingestion import futures_curve as fc


def test_symbol_format_and_parse():
    r = fc.ROOT_BY_ID["CL"]
    assert r.symbol(2026, 12) == "CLZ26.NYM"
    assert fc.ROOT_BY_ID["GC"].symbol(2027, 2) == "GCG27.CMX"
    assert fc.ROOT_BY_ID["ES"].symbol(2027, 3) == "ESH27.CME"
    assert fc.ROOT_BY_ID["ZN"].symbol(2026, 12) == "ZNZ26.CBT"
    assert fc.ROOT_BY_ID["KC"].symbol(2026, 12) == "KCZ26.NYB"
    assert fc.parse_contract("Z26") == (2026, 12)
    assert fc.parse_contract("F31") == (2031, 1)
    assert r.front_security_id == "IDX:CL=F"
    assert all(set(x.cycle) <= set(fc.MONTH_CODES) for x in fc.ROOTS)
    assert all(x.rule in fc.EXPIRY_RULES for x in fc.ROOTS)


@pytest.mark.parametrize("root,y,m,expected", [
    ("CL", 2026, 12, date(2026, 11, 20)),   # 25 Nov 2026 is a Wednesday -> 3 business days before
    ("CL", 2027, 1, date(2026, 12, 21)),    # 25 Dec is a holiday -> 4 business days before the 25th
    ("ES", 2026, 12, date(2026, 12, 18)),   # third Friday
    ("GC", 2026, 12, date(2026, 12, 29)),   # third last business day
    ("6E", 2026, 12, date(2026, 12, 14)),   # 2 business days before the third Wednesday (16 Dec)
    ("ZN", 2026, 12, date(2026, 12, 21)),   # 7th business day before the last business day (25 Dec holiday)
    ("NG", 2026, 12, date(2026, 11, 25)),   # 3 business days before 1 Dec
    ("ZC", 2026, 12, date(2026, 12, 14)),   # business day before the 15th
    ("BTC", 2026, 12, date(2026, 12, 24)),  # last Friday (25 Dec) is a holiday -> previous business day
    ("BZ", 2026, 12, date(2026, 10, 30)),   # last business day of the second month before
])
def test_expiry_rules(root, y, m, expected):
    assert fc.expiry(root, y, m) == expected


def test_business_day_helpers():
    assert not fc.is_bd(date(2026, 12, 25)) and not fc.is_bd(date(2026, 10, 17))
    assert fc.bd_before(date(2026, 10, 19), 1) == date(2026, 10, 16)
    assert fc.nth_last_bd(2026, 12, 1) == date(2026, 12, 31)
    assert fc.nth_bd(2026, 11, 1) == date(2026, 11, 2)
    assert fc.last_session(date(2026, 10, 19)) == date(2026, 10, 16)


def test_listed_contracts_cycle_serial_and_expired():
    as_of = date(2026, 10, 16)
    gc = [c["contract"] for c in fc.listed_contracts(fc.ROOT_BY_ID["GC"], as_of)]
    # 3 serial months (V26 X26 Z26) then the G J M Q V Z cycle; V26 expires 28 Oct 2026 >= as_of
    assert gc == ["V26", "X26", "Z26", "G27", "J27", "M27", "Q27", "V27"]
    cl = fc.listed_contracts(fc.ROOT_BY_ID["CL"], date(2026, 10, 21))
    assert cl[0]["contract"] == "Z26"            # X26 expired on 20 Oct
    assert len(cl) == 12 and all(c["expiry"] >= date(2026, 10, 21) for c in cl)
    es = [c["contract"] for c in fc.listed_contracts(fc.ROOT_BY_ID["ES"], as_of)]
    assert es == ["Z26", "H27", "M27", "U27", "Z27"]
    frame = fc.contract_frame(as_of, ["CL", "ES"])
    assert set(frame.get_column("root")) == {"CL", "ES"} and frame.height == 17
    assert frame.get_column("symbol").is_unique().all()


def _bars(symbols, dates, close=100.0):
    rows = []
    for s in symbols:
        for i, d in enumerate(dates):
            rows.append({"symbol": s, "date": d, "open": close + i, "high": close + i + 1, "low": close + i - 1,
                         "close": close + i, "volume": 1000.0 + i})
    return pl.DataFrame(rows)


def test_bars_to_rows_drops_weekend_and_future_bars():
    contracts = fc.contract_frame(date(2026, 10, 16), ["CL"])
    sym = "CLZ26.NYM"
    dates = [date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 18), date(2026, 10, 19)]  # Sun partial + Mon
    rows = fc.bars_to_rows(_bars([sym, "UNKNOWN.NYM"], dates), contracts, as_of=date(2026, 10, 16))
    assert rows.get_column("date").to_list() == [date(2026, 10, 15), date(2026, 10, 16)]
    r = rows.row(-1, named=True)
    assert r["root"] == "CL" and r["contract"] == "Z26" and r["contract_month"] == date(2026, 12, 1)
    assert r["expiry"] == date(2026, 11, 20) and r["days_to_expiry"] == 35
    assert r["source"] == "yahoo" and r["knowledge_date"] == r["date"]
    assert rows.schema["close"] == pl.Float32 and rows.schema["volume"] == pl.Int64


def test_fetch_symbols_throttle_cooldown_and_single_retry():
    calls, sleeps = [], []
    canary_answers = iter([False, False])

    def dl(chunk, period):
        calls.append(tuple(chunk))
        if len(calls) <= 2:                       # first two downloads throttled (empty frame)
            return pl.DataFrame()
        return _bars([s for s in chunk if s != "MISS"], [date(2026, 10, 16)])

    bars, unchecked = fc.fetch_symbols(["A", "B", "MISS"], "1mo", batch=3, pause=1.0, sleep=sleeps.append,
                                       downloader=dl, canary=lambda: next(canary_answers, True))
    assert set(bars.get_column("symbol")) == {"A", "B"} and not unchecked
    assert sleeps[:2] == [60.0, 120.0]            # two cool-downs, then success
    assert calls[-1] == ("MISS",)                 # empty symbol retried one by one


def test_fetch_symbols_marks_unchecked_when_throttle_persists():
    bars, unchecked = fc.fetch_symbols(["A", "B"], "1mo", batch=2, pause=0, sleep=lambda s: None,
                                       downloader=lambda c, p: pl.DataFrame(), canary=lambda: False, max_cooldowns=1)
    assert bars.is_empty() and unchecked == {"A", "B"}


def test_run_fetch_end_to_end(tmp_data, monkeypatch):
    as_of = date(2026, 10, 16)
    state_file = tmp_data.data_dir / "state" / "roadmap_v2.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"item6": {"keep": True}}), encoding="utf-8")
    missing = "ESU27.CME"
    days = [as_of - timedelta(days=k) for k in range(10) if (as_of - timedelta(days=k)).weekday() < 5]

    def dl(chunk, period):
        return _bars([s for s in chunk if s != missing], days)

    monkeypatch.setattr(fc, "_download", dl)
    monkeypatch.setattr(fc, "_canary_ok", lambda: True)
    monkeypatch.setattr(fc.time, "sleep", lambda s: None)
    res = fc.run_fetch("backfill", as_of=as_of, roots=["ES"], settings=tmp_data, pause=0)
    assert res["contracts"] == 5 and res["served"] == 4 and res["missing"] == 1 and res["unchecked"] == 0
    curve = read_table("futures_curve", settings=tmp_data)
    assert curve.height == 4 * len(days) and set(curve.get_column("root")) == {"ES"}
    cov = read_table("futures_contracts", settings=tmp_data)
    assert cov.filter(pl.col("symbol") == missing).get_column("status").item() == "missing"
    assert cov.filter(pl.col("status") == "served").height == 4
    doc = json.loads(state_file.read_text(encoding="utf-8"))
    assert doc["item6"] == {"keep": True}                      # other items' keys are preserved
    assert doc["item5a"]["coverage"]["ES"]["missing"] == ["U27"]
    snap = fc.curve_snapshot("ES", as_of, settings=tmp_data)
    assert snap.height == 4 and snap.get_column("expiry").is_sorted()
    # incremental: stored contracts use the 1-month window, the never-served one is asked with period=max
    periods = []
    monkeypatch.setattr(fc, "_download", lambda c, p: (periods.append((tuple(c), p)), dl(c, p))[1])
    fc.run_fetch("incremental", as_of=as_of, roots=["ES"], settings=tmp_data, pause=0)
    assert ((missing,), "max") in periods and any(p == "1mo" and len(c) == 4 for c, p in periods)
    # a later run after the front contract expired keeps it in the register as expired
    fc.run_fetch("incremental", as_of=date(2026, 12, 21), roots=["ES"], settings=tmp_data, pause=0)
    cov = read_table("futures_contracts", settings=tmp_data)
    assert cov.filter(pl.col("contract") == "Z26").get_column("status").item() == "expired"
