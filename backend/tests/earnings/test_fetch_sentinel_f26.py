"""F26: Yahoo "no dates" answers no longer write 1900-01-01 marker rows; legacy markers move to the fetch log."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from app.core.storage import scan_table
from app.earnings import common as C
from app.earnings import fetch as F


def _row(sid: str, d: date, fetched: datetime) -> dict:
    return {"source_id": "yahoo", "security_id": sid, "issuer_id": "I" + sid, "event_date": d, "event_ts": None,
            "time_known": False, "period_hint": None, "provider_symbol": sid, "knowledge_date": fetched.date(),
            "fetched_at": fetched}


def test_purge_moves_markers_to_the_fetch_log(tmp_data):
    t1, t2 = datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 10, 2, tzinfo=UTC)
    C.write(pl.DataFrame([_row("S1", F.SENTINEL, t1), _row("S2", date(2026, 11, 5), t2), _row("S3", F.SENTINEL, t2)]),
            "earnings_announcements", tmp_data, mode="upsert")
    assert F.purge_sentinels(tmp_data) == 2
    left = scan_table("earnings_announcements", tmp_data).collect()
    assert left["security_id"].to_list() == ["S2"] and F.SENTINEL not in left["event_date"].to_list()
    log = F.read_fetch_log(tmp_data)
    assert set(log) == {"S1", "S3"} and log["S1"].startswith("2026-10-01")
    assert F.purge_sentinels(tmp_data) == 0  # idempotent


def test_empty_answers_go_to_the_log_not_the_table(tmp_data, monkeypatch):
    targets = [{"security_id": "S9", "issuer_id": "I9", "yf_ticker": "S9", "timezone": None, "region": "US"}]
    monkeypatch.setattr(F, "_yahoo_targets", lambda *a, **k: targets)
    monkeypatch.setattr(F, "yahoo_dates", lambda *a, **k: [])
    out = F.fetch_yahoo(tmp_data, pl.DataFrame(), ["mvp_us"], limit=4, refresh_days=7, max_minutes=1, rate=100.0)
    assert out["empty"] == 1 and out["rows_written"] == 0
    assert "S9" in F.read_fetch_log(tmp_data)
    assert scan_table("earnings_announcements", tmp_data).collect().is_empty()
