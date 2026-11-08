"""Startup sync A1: freshness scan reason codes on a synthetic lake (fake calendars, fake keys, fake ledger).

fresh, due (with incremental params), not_yet_published (ledger: fetched but not published yet), source_unavailable,
failed (a broken check), no_source (documented entities), needs_key (key names, never values), metered network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.ops import calendars as cal
from app.ops import gaps as G
from app.ops.sync import expect as X
from app.ops.sync.scan import scan
from app.ops.sync.state import Ledger

FRI_EVENING = datetime(2026, 10, 16, 22, 30, tzinfo=UTC)
TUE_MORNING = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fast_calendars(monkeypatch):
    # exchange_calendars is slow to build: Mon-Fri sessions closing 21:00 UTC for every MIC
    import app.normalize.price_integrity as pi

    monkeypatch.setattr(cal, "sessions", lambda mic, a, b, settings=None: cal.business_days(a, b))
    monkeypatch.setattr(X, "_xcal", lambda mic: None)
    monkeypatch.setattr(pi, "last_closed_session", lambda c, now=None, buffer_min=30: None)


def _days(a: date, b: date) -> list[date]:
    return [d for d in (a + timedelta(days=i) for i in range((b - a).days + 1)) if d.weekday() < 5]


def _prices(s, region: str, last: date, n: int = 100) -> None:
    rows = [{"security_id": f"S{i:04d}", "date": d} for d in _days(last - timedelta(days=40), last) for i in range(n)]
    df = pl.DataFrame(rows, schema={"security_id": pl.Utf8, "date": pl.Date})
    for y in sorted({d.year for d in df["date"].to_list()}):
        p = s.data_dir / "normalized" / "prices_daily" / f"region={region}" / f"year={y}"
        p.mkdir(parents=True, exist_ok=True)
        df.filter(pl.col("date").dt.year() == y).write_parquet(p / "part-test.parquet")


def _scan(s, now, datasets, **kw):
    kw.setdefault("key_present", lambda name: True)
    kw.setdefault("metered", False)
    kw.setdefault("job_runs", pl.DataFrame())
    return scan(s, now=now, datasets=datasets, **kw)


def test_prices_fresh_then_due_with_incremental_params(tmp_data):
    _prices(tmp_data, "US", date(2026, 10, 16))
    r = _scan(tmp_data, FRI_EVENING, ["prices.US"])
    d = r.by_id()["prices.US"]
    assert d.reason == "fresh" and d.data_as_of == "2026-10-16" and d.expected_as_of == "2026-10-16"
    assert d.next_expected and d.next_expected.startswith("2026-10-19T22:00")   # Monday close + 60 min
    assert r.plan == {}
    r = _scan(tmp_data, TUE_MORNING, ["prices.US"])
    d = r.by_id()["prices.US"]
    assert d.reason == "due" and d.expected_as_of == "2026-10-19"
    assert r.plan["prices_us_polygon"].params == {"dates": ["2026-10-19"]}
    assert r.plan["prices_finalize"].params == {"regions": ["US"]} and r.plan["prices_finalize"].trigger == "upstream"
    assert r.plan["prices_global_yf"].params["symbols"][0] == "^GSPC"            # US: Yahoo only for the benchmarks
    assert r.plan["prices_us_polygon"].periods == {"prices.US|US": ["2026-10-19"]}


def test_ledger_turns_due_into_not_yet_published_or_source_unavailable(tmp_data):
    _prices(tmp_data, "US", date(2026, 10, 16))
    led = Ledger(tmp_data, doc={})
    led.record("prices.US", "US", "2026-10-19", "not_yet_published", 60, "prices_us_polygon ran but 2026-10-19 is "
               "still missing", "prices_us_polygon", now=TUE_MORNING)
    r = _scan(tmp_data, TUE_MORNING + timedelta(minutes=5), ["prices.US"], ledger=led)
    d = r.by_id()["prices.US"]
    assert d.reason == "not_yet_published" and d.retry_at.startswith("2026-10-20T10:00") and r.plan == {}
    assert d.attempts == 1 and "not published it yet" in d.detail
    # after the retry time the item is due again (and fetched again)
    r = _scan(tmp_data, TUE_MORNING + timedelta(minutes=61), ["prices.US"], ledger=led)
    assert r.by_id()["prices.US"].reason == "due" and "prices_us_polygon" in r.plan
    led.record("prices.US", "US", "2026-10-19", "source_unavailable", 30, "ConnectionError: reset", "x", now=TUE_MORNING)
    r = _scan(tmp_data, TUE_MORNING + timedelta(minutes=1), ["prices.US"], ledger=led)
    assert r.by_id()["prices.US"].reason == "source_unavailable" and r.plan == {}


def test_needs_key_names_the_key_and_never_its_value(tmp_data):
    _prices(tmp_data, "US", date(2026, 10, 16))
    r = _scan(tmp_data, TUE_MORNING, ["prices.US", "job:opendart_filings"],
              key_present=lambda name: name not in ("polygon_apikey", "opendart_apikey"))
    us = r.by_id()["prices.US"]
    alt = next(e for e in us.entities if e.entity == "prices_us_polygon")
    assert alt.reason == "needs_key" and alt.keys == ["POLYGON_APIKEY"] and alt.secondary
    assert us.reason == "due" and "prices_us_polygon" not in r.plan and "prices_global_yf" in r.plan
    od = r.by_id()["job:opendart_filings"]
    assert od.reason == "needs_key" and od.keys == ["OPENDART_APIKEY"] and "opendart_filings" not in r.plan


def test_no_source_entities_and_fx_due(tmp_data):
    rows = []
    for d in _days(date(2026, 9, 1), date(2026, 10, 15)):
        rows.append({"date": d, "currency": "EUR", "usd_per_unit": 1.1})
        if d <= date(2026, 10, 2):
            rows.append({"date": d, "currency": "TWD", "usd_per_unit": 0.031})
    p = tmp_data.data_dir / "normalized" / "fx_rates" / "year=2026"
    p.mkdir(parents=True)
    pl.DataFrame(rows, schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64}).write_parquet(p / "x.parquet")
    r = _scan(tmp_data, TUE_MORNING, ["fx.rates"])
    fx = r.by_id()["fx.rates"]
    twd = next(e for e in fx.entities if e.entity == "TWD")
    assert twd.reason == "no_source" and "CBC" in twd.detail and twd.secondary
    assert fx.reason == "due" and fx.expected_as_of == "2026-10-19" and "fx_rates" in r.plan


def test_broken_check_is_failed_and_the_scan_goes_on(tmp_data, monkeypatch):
    _prices(tmp_data, "EU", date(2026, 10, 16))

    def boom(cx, spec, st):
        if spec["id"] == "prices.US":
            raise RuntimeError("disk on fire")
        return orig(cx, spec, st)

    orig = G.CHECKS["sessions"]
    monkeypatch.setitem(G.CHECKS, "sessions", boom)
    r = _scan(tmp_data, TUE_MORNING, ["prices.US", "prices.EU"])
    assert r.by_id()["prices.US"].reason == "failed" and "disk on fire" in r.by_id()["prices.US"].detail
    assert r.by_id()["prices.EU"].reason == "due" and "prices.US" in r.errors


def test_macro_release_due_lagging_and_next(tmp_data):
    now = TUE_MORNING
    ev = pl.DataFrame({
        "release_event_id": ["RE1", "RE2", "RE3"], "series_id": ["fred:CPIAUCSL", "ecb:HICP", "fred:UNRATE"],
        "country": ["US", "EA", "US"], "theme": ["inflation", "inflation", "labor"], "measure": ["mom_pct"] * 3,
        "release_date": [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 23)],
        "scheduled_at": [now - timedelta(hours=20), now - timedelta(minutes=30), now + timedelta(days=3)],
        "status": ["pending", "pending", "pending"], "computed_at": [now] * 3})
    from app.core.storage import write_table

    write_table(ev, "macro_release_events", mode="overwrite", settings=tmp_data)
    r = _scan(tmp_data, now, ["macro.releases"])
    d = r.by_id()["macro.releases"]
    by = {e.entity: e for e in d.entities}
    assert by["US"].reason == "due" and "fred:CPIAUCSL" in by["US"].detail
    assert by["EA"].reason == "not_yet_published" and by["EA"].next_expected   # inside the 2-h publication lag
    assert d.reason == "due" and "macro_fred" in r.plan
    assert d.next_expected.startswith("2026-10-23")


def test_cftc_waits_for_the_friday_release(tmp_data):
    rows = [{"report_date": d, "market_code": f"M{i}"} for d in [date(2026, 10, 6), date(2026, 10, 13)] for i in range(200)]
    p = tmp_data.data_dir / "normalized" / "cftc_positions" / "report_type=legacy_fut"
    p.mkdir(parents=True)
    pl.DataFrame(rows, schema={"report_date": pl.Date, "market_code": pl.Utf8}).write_parquet(p / "part.parquet")
    thu = datetime(2026, 10, 22, 12, tzinfo=UTC)       # report of Tue 10-20 is released Fri 10-23 15:30 ET
    d = _scan(tmp_data, thu, ["cftc.cot"]).by_id()["cftc.cot"]
    assert d.reason == "fresh" and d.next_expected.startswith("2026-10-23T19:30")
    sat = datetime(2026, 10, 24, 12, tzinfo=UTC)
    r = _scan(tmp_data, sat, ["cftc.cot"])
    assert r.by_id()["cftc.cot"].reason == "due" and "cftc_cot" in r.plan


def test_metered_network_defers_downloads(tmp_data):
    _prices(tmp_data, "US", date(2026, 10, 16))
    r = _scan(tmp_data, TUE_MORNING, ["prices.US"], metered=True)
    assert r.by_id()["prices.US"].reason == "source_unavailable" and r.plan == {}
    assert "metered" in r.by_id()["prices.US"].detail


def test_expect_helpers():
    assert X.target_day(date(2026, 12, 25)) is False and X.target_day(date(2026, 10, 27)) is True
    assert X.ecb_expected(datetime(2026, 10, 27, 14, tzinfo=UTC)) == date(2026, 10, 26)     # before 16:00 CET
    assert X.ecb_expected(datetime(2026, 10, 27, 16, tzinfo=UTC)) == date(2026, 10, 27)
    assert X.gdelt_expected(datetime(2026, 10, 27, 0, 20, tzinfo=UTC)) == date(2026, 10, 25)
    assert X.gdelt_expected(datetime(2026, 10, 27, 1, 0, tzinfo=UTC)) == date(2026, 10, 26)
    assert X.next_cftc(datetime(2026, 10, 27, 12, tzinfo=UTC)).isoformat().startswith("2026-10-30T19:30")
    assert X.expected_13f(datetime(2026, 10, 27, tzinfo=UTC)) == date(2026, 6, 30)
