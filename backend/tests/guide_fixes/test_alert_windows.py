"""Guide 13: under the weekly schedule the window rules (filings, 13D, short interest, EU shorts, macro releases /
surprises, earnings surprises) only looked at (previous business day, as_of], so events from Monday-Thursday never
alerted. The window now starts at the last successful alerts run (profile-aware default) and events are
de-duplicated by event id."""

from __future__ import annotations

import json
from datetime import date, timedelta

import duckdb
import polars as pl

from app.core.storage import ensure_duckdb_tables
from app.scheduler import alerts as al
from tests.platform.test_alerts import NOW, _alert

MON, THU, FRI = date(2026, 10, 5), date(2026, 10, 8), date(2026, 10, 9)


def _provider(extra_filing: bool = False):
    rows = [("sec_edgar:mon", "10-Q", "quarterly", MON), ("sec_edgar:fri", "8-K", "current_report", FRI)]
    if extra_filing:
        rows.append(("sec_edgar:fri2", "10-K", "annual", FRI))
    n = len(rows)
    filings = pl.DataFrame({
        "filing_id": [r[0] for r in rows], "source_id": ["sec_edgar"] * n, "year": [2026] * n,
        "source_entity_id": ["1"] * n, "issuer_id": ["I1"] * n, "form_type": [r[1] for r in rows],
        "form_class": [r[2] for r in rows], "knowledge_date": [r[3] for r in rows], "is_amendment": [False] * n,
        "has_xbrl": [True] * n, "url": ["u"] * n, "ingested_at": [NOW] * n,
    })
    secm = pl.DataFrame({
        "security_id": ["S1"], "issuer_id": ["I1"], "security_type": ["common"], "primary_listing_id": ["L1"],
        "display_ticker": ["AAA"], "name": ["A"], "trading_currency": ["USD"], "is_primary_security": [True],
        "is_active": [True], "updated_at": [NOW],
    })
    return al.frames_provider({"filings_index": filings, "securities_master": secm})


RULE = _alert("new_filing", {"form_classes": ["quarterly", "annual", "current_report"]})


def test_default_window_is_previous_business_day_only():
    ev = al.evaluate_alerts([RULE], _provider(), FRI, now=NOW)
    assert len(ev) == 1 and json.loads(ev[0]["payload"])["filings"][0]["filing_id"] == "sec_edgar:fri"


def test_weekly_window_sees_the_whole_week():
    start = al.alert_window_start(FRI, state={}, period_days=7)
    assert start == FRI - timedelta(days=7)
    ev = al.evaluate_alerts([RULE], _provider(), FRI, now=NOW, window_start=start)
    p = json.loads(ev[0]["payload"])
    assert {f["filing_id"] for f in p["filings"]} == {"sec_edgar:mon", "sec_edgar:fri"}
    assert set(p["dedup_keys"]) == {"filing:sec_edgar:mon", "filing:sec_edgar:fri"} and p["window_start"] == str(start)


def test_window_starts_at_last_successful_run():
    assert al.alert_window_start(FRI, state={"last_as_of": "2026-10-02"}, period_days=7) == date(2026, 10, 2)
    # daily profile, last run Thursday -> (Thu, Fri]
    assert al.alert_window_start(FRI, state={"last_as_of": THU.isoformat()}, period_days=1) == THU
    # same-date re-run keeps the previous window
    assert al.alert_window_start(FRI, state={"last_as_of": FRI.isoformat(), "window_start": "2026-10-02"},
                                 period_days=7) == date(2026, 10, 2)
    # never later than the previous business day, never more than MAX_WINDOW_DAYS back
    assert al.alert_window_start(date(2026, 10, 12), state={"last_as_of": "2026-10-11"}, period_days=7) == FRI
    assert al.alert_window_start(FRI, state={"last_as_of": "2026-01-01"}, period_days=7) == FRI - timedelta(days=31)


def test_overlapping_windows_dedupe_by_event_id():
    start = FRI - timedelta(days=7)
    first = al.evaluate_alerts([RULE], _provider(), FRI, now=NOW, window_start=start)
    # re-run over the same window: nothing new
    assert al.evaluate_alerts([RULE], _provider(), FRI, existing_events=first, now=NOW + timedelta(days=7),
                              window_start=start) == []
    # a new filing appears in the overlapping window: only it fires
    again = al.evaluate_alerts([RULE], _provider(extra_filing=True), FRI, existing_events=first,
                               now=NOW + timedelta(days=7), window_start=start)
    assert len(again) == 1
    p = json.loads(again[0]["payload"])
    assert [f["filing_id"] for f in p["filings"]] == ["sec_edgar:fri2"] and again[0]["message"] == "new filing: 10-K"


def test_evaluate_and_store_records_the_window(tmp_data):
    con = duckdb.connect(str(tmp_data.app_state_db_path))
    ensure_duckdb_tables(con, ["alerts", "watchlist_items"])
    con.execute("INSERT INTO alerts (alert_id, security_id, watchlist_id, rule_type, params, enabled, cooldown_days, "
                "last_fired_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [RULE["alert_id"], "S1", None, RULE["rule_type"], RULE["params"], True, 0, None, NOW])
    ev = al.evaluate_and_store(FRI, provider=_provider(), settings=tmp_data, con=con)
    assert len(ev) == 1 and len(json.loads(ev[0]["payload"])["filings"]) == 2   # first run: one profile period back
    st = al.window_state(tmp_data)
    assert st["last_as_of"] == FRI.isoformat() and st["events"] == 1
    # the same date again: same window, already alerted -> quiet
    assert al.evaluate_and_store(FRI, provider=_provider(), settings=tmp_data, con=con) == []
    # next weekly run: window starts at Friday, the stored payload keys keep old filings out
    nxt = FRI + timedelta(days=7)
    assert al.alert_window_start(nxt, tmp_data) == FRI
    con.close()
