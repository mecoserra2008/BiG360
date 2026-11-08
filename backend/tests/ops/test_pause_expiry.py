"""Coverage v3 A4: PAUSE files with an expiry (app.ops.pause). The 2026-10-24 filler.PAUSE ("paused by orchestrator:
API restart loop") was never removed and kept the gap filler paused for 13 days. Now a flag carries until=, new pauses
default to 24 h, legacy files (no until=) expire 7 days after they were written, an expired flag is ignored with a
warning, and the state (reason, owner, expiry) reaches the governor, Data Health and GET /system/status."""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.ops import governor as gov
from app.ops import pause as PZ

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def test_parse_until_reason_by():
    st = PZ.parse_pause("filler", "x", "until=2026-11-06T18:00:00+00:00\nreason=maintenance\nby=ops\n", None, NOW)
    assert st.present and st.active and not st.expired and not st.legacy
    assert (st.reason, st.by, st.until) == ("maintenance", "ops", "2026-11-06T18:00:00+00:00")
    assert st.remaining_h == pytest.approx(6.0)
    # JSON flags and naive timestamps (UTC) are accepted too
    js = PZ.parse_pause("filler", "x", '{"until": "2026-11-06T13:00:00", "reason": "json"}', None, NOW)
    assert js.active and js.reason == "json" and js.until == "2026-11-06T13:00:00+00:00"
    # expired by its until
    old = PZ.parse_pause("filler", "x", "until=2026-11-06T11:59:00Z", None, NOW)
    assert old.present and old.expired and not old.active and old.expired_at == "2026-11-06T11:59:00+00:00"


def test_legacy_file_expires_seven_days_after_it_was_written():
    text = "paused by orchestrator 2026-10-24 04:45: API restart loop under CPU/RAM pressure"
    written = datetime(2026, 10, 24, 4, 45, tzinfo=UTC).timestamp()
    st = PZ.parse_pause("filler", "x", text, written, NOW)       # the live file of the audit: 13 days old
    assert st.legacy and st.expired and not st.active
    assert st.reason.startswith("paused by orchestrator") and st.until == "2026-10-31T04:45:00+00:00"
    fresh = PZ.parse_pause("filler", "x", text, (NOW - timedelta(days=2)).timestamp(), NOW)
    assert fresh.legacy and fresh.active and fresh.remaining_h == pytest.approx(5 * 24)
    empty = PZ.parse_pause("filler", "x", "", (NOW - timedelta(hours=1)).timestamp(), NOW)   # `New-Item filler.PAUSE`
    assert empty.legacy and empty.active and empty.reason is None
    bad = PZ.parse_pause("filler", "x", "until=tomorrow", (NOW - timedelta(days=8)).timestamp(), NOW)
    assert bad.legacy and bad.expired and "unparseable" in bad.detail
    assert PZ.parse_pause("filler", "x", None, None, NOW).present is False


def test_write_default_24h_and_read_with_warning(tmp_path, caplog):
    p = tmp_path / "filler.PAUSE"
    st = PZ.write_pause(p, reason="tray", by="tray", now=NOW)
    assert st.active and st.until == (NOW + timedelta(hours=24)).isoformat(timespec="seconds")
    assert "until=" in p.read_text(encoding="utf-8") and "reason=tray" in p.read_text(encoding="utf-8")
    # a legacy file older than 7 days: ignored with one warning per file version
    legacy = tmp_path / "old.PAUSE"
    legacy.write_text("paused by hand", encoding="utf-8")
    t = time.time() - 8 * 86400
    os.utime(legacy, (t, t))
    with caplog.at_level(logging.WARNING, logger="app.ops.pause"):
        a = PZ.read_pause(legacy, "filler")
        PZ.read_pause(legacy, "filler")
    assert a.expired and sum("expired" in r.message for r in caplog.records) == 1
    assert PZ.clear_pause(p) and not p.exists() and not PZ.clear_pause(p)


def test_filler_sample_ignores_an_expired_flag(tmp_data, monkeypatch):
    from app.ops import filler as FL

    f = FL.Filler(tmp_data)
    flag = tmp_data.state_dir / "ops" / "filler.PAUSE"
    flag.write_text("paused by orchestrator: restart loop", encoding="utf-8")
    t = time.time() - 13 * 86400
    os.utime(flag, (t, t))
    sm = f.sample()
    assert sm.manual_pause is False and f.pause["expired"] and f.pause["legacy"]
    assert f.last_sample["pause"]["reason"].startswith("paused by orchestrator")
    assert "paused by user" not in gov.decide(sm, [], {"pause_on_battery": False, "pause_on_metered": False}).reasons
    PZ.write_pause(flag, reason="maintenance", by="ops", hours=2)
    sm = f.sample()
    d = gov.decide(sm, [], {"pause_on_battery": False, "pause_on_metered": False})
    assert sm.manual_pause and d.state == "paused" and "paused by user" in d.reasons
    f.last_decision = d.to_dict()
    f.write_state()
    st = FL.read_state(tmp_data)
    assert st["pause"]["active"] and st["pause"]["reason"] == "maintenance" and st["governor"]["sample"]["pause"]


def test_tray_pause_gets_an_expiry_and_expired_flag_reads_as_not_paused(tmp_data):
    from app.desktop.tray_actions import TrayController

    ctl = TrayController.__new__(TrayController)
    ctl.s = tmp_data
    msg = ctl.toggle_pause()
    st = PZ.filler_pause(tmp_data)
    assert "until" in msg and st.active and st.by == "tray" and st.remaining_h == pytest.approx(24, abs=0.1)
    assert ctl.is_paused()
    flag = PZ.filler_pause_path(tmp_data)
    flag.write_text("until=2020-01-01T00:00:00+00:00\nreason=old", encoding="utf-8")
    assert not ctl.is_paused()                       # expired: the tray offers "Pause" again
    assert "paused" in ctl.toggle_pause() and ctl.is_paused()


def test_status_payloads_expose_pause_state(tmp_data):
    from app.ops.health import ops_alerts

    flag = PZ.filler_pause_path(tmp_data)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("old orchestrator pause", encoding="utf-8")
    t = time.time() - 9 * 86400
    os.utime(flag, (t, t))
    rt = PZ.runtime_status(tmp_data)
    assert [p["name"] for p in rt["pauses"]] == ["filler"] and rt["pauses"][0]["expired"]
    assert rt["heavy_slot"]["slot_gb"] == 7.0 and rt["admission_waits"] == []
    # Data Health / ops alerts: expired flag reported (info), the filler runs again
    pz = PZ.filler_pause(tmp_data).to_dict()
    filler = {"heartbeat_at": datetime.now(UTC).isoformat(), "pause": pz,
              "governor": {"state": "running", "reasons": [], "sample": {"pause": pz}}}
    sch = {"enabled": True, "worker_alive": True, "profile": "weekly", "next_runs": {}, "last_success": {}}
    alerts = ops_alerts(sch, {"datasets": {}}, filler, [], None)
    ex = [a for a in alerts if a["id"] == "pause_expired"]
    assert ex and "runs again" in ex[0]["detail"]
    # an active pause: the alert names the reason and the expiry
    PZ.write_pause(flag, reason="disk maintenance", by="ops", hours=3)
    pz = PZ.filler_pause(tmp_data).to_dict()
    filler = {"heartbeat_at": datetime.now(UTC).isoformat(), "pause": pz,
              "governor": {"state": "paused", "reasons": ["paused by user"], "sample": {"pause": pz}}}
    a = next(a for a in ops_alerts(sch, {"datasets": {}}, filler, [], None) if a["id"] == "filler_paused")
    assert "disk maintenance" in a["detail"] and "expires" in a["detail"] and "(by ops)" in a["detail"]


def test_system_status_endpoint_contract(tmp_data, monkeypatch):
    """GET /system/status carries pauses / heavy_slot / admission_waits and validates against the contract."""
    from fastapi.testclient import TestClient

    from app.api.deps import snapshots
    from app.api.main import create_app
    from app.api.routers import system
    from app.ops import ram_budget as RB
    from tests.api_contract import validate

    snapshots.reset()
    system.reset_status_cache()
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "0")
    PZ.write_pause(PZ.filler_pause_path(tmp_data), reason="contract test", by="pytest", hours=1)
    RB.note_wait("prices_finalize", "floor", "RAM floor: free 5.0 GB - reserved 0.0 GB - budget 10.0 GB", tmp_data)
    try:
        b = TestClient(create_app()).get("/api/v1/system/status").json()
        validate("GET", "/api/v1/system/status", 200, b)
        assert b["pauses"][0]["active"] and b["pauses"][0]["reason"] == "contract test"
        assert b["heavy_slot"]["slot_gb"] == 7.0 and b["heavy_slot"]["holder"] is None
        assert [w["job_id"] for w in b["admission_waits"]] == ["prices_finalize"]
    finally:
        RB.clear_wait("prices_finalize", tmp_data)
        system.reset_status_cache()
        snapshots.reset()
