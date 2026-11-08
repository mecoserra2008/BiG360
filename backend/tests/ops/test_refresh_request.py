"""O3 "Run refresh now": request file -> the worker's ProfileScheduler fires the active profile's primary entry."""

from __future__ import annotations

from datetime import UTC, datetime

from app.ops import schedule as S
from app.ops.worker_schedule import ProfileScheduler, primary_entry, refresh_request_path, request_refresh
from tests.ops.test_schedule import FakeRunner, reg


def _recent():
    return {d: {"finished_at": datetime.now(UTC)} for d in ("nightly", "weekly", "monthly", "cftc_weekly")}


def test_primary_entry_per_profile(tmp_data):
    weekly = S.profile_entries("weekly", reg(), tmp_data)
    daily = S.profile_entries("daily", reg(), tmp_data)
    assert primary_entry(weekly).id == "weekly_refresh"
    assert primary_entry(daily).id == "nightly"
    assert primary_entry([]) is None


def test_refresh_request_fires_chain_once(tmp_data):
    r = FakeRunner()
    ps = ProfileScheduler(r, tmp_data, reg(), completed_fn=_recent)
    try:
        ps.start()                                        # all recently completed: nothing runs by itself
        assert r.calls == []
        assert ps.take_refresh_request() is None          # no request file
        request_refresh(tmp_data, by="test")
        assert refresh_request_path(tmp_data).exists()
        ps.tick()
        for th in ps.threads:
            th.join(10)
        assert not refresh_request_path(tmp_data).exists()   # consumed
        assert [c[0] for c in r.calls][:2] == ["nightly", "weekly"]
        hb = ps.heartbeat()
        assert hb["accepts_refresh_request"] is True
        assert hb["refresh_request"]["status"] == "started" and hb["refresh_request"]["entry"] == "weekly_refresh"
        assert hb["refresh_request"]["by"] == "test"
        # while the entry runs, a second request is refused (no overlap)
        ps.running.add("weekly_refresh")
        request_refresh(tmp_data)
        assert ps.take_refresh_request()["status"] == "already_running"
        ps.running.discard("weekly_refresh")
        # explicit entry id / unknown id
        request_refresh(tmp_data, entry_id="nope")
        assert ps.take_refresh_request()["status"] == "no_entry"
    finally:
        ps.shutdown()
