"""Ops health: per-region data as-of, scheduler status, ops alerts (scheduler off > 1 day, SLA, disk)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from app.ops import health as H

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)


def test_region_as_of_ignores_a_few_early_bars():
    rows = [("US", date(2026, 10, 13), 68), ("US", date(2026, 10, 12), 5781), ("US", date(2026, 10, 9), 7),
            ("EU", date(2026, 10, 13), 1370), ("EU", date(2026, 10, 12), 14), ("JP", None, 3), (None, date(2026, 1, 1), 1)]
    out = H.region_as_of(rows)
    assert out == {"US": "2026-10-12", "EU": "2026-10-13"}
    assert H.overall_as_of({"US": "2026-10-12", "EU": "2026-10-13", "KR": "2026-10-16"}) == "2026-10-13"
    assert H.overall_as_of({"KR": "2026-10-16"}) == "2026-10-16" and H.overall_as_of({}) is None
    assert H.is_optional_table("live_macro") and not H.is_optional_table("prices_daily")


def _runs(trigger="manual", start=datetime(2026, 10, 10, 8, tzinfo=UTC)):
    return pl.DataFrame([{"run_id": "R1", "dag_run_id": "D1-nightly", "job_id": "a", "status": "success", "trigger": trigger,
                          "started_at": start, "finished_at": start + timedelta(hours=1), "error_message": None}])


def test_scheduler_off_alert_after_one_day(tmp_data):
    hb = {"alive": True, "scheduler_enabled": False, "started_at": "2026-10-19T22:40:00+00:00"}
    st = H.scheduler_status(tmp_data, hb, None, _runs(), NOW)
    assert not st["enabled"] and st["worker_alive"] and st["reason"] == "worker started with --no-schedule"
    assert st["off_since"].startswith("2026-10-10")             # no scheduled run ever: earliest tracked run
    assert st["off_hours"] > 24 and st["profile"] == "weekly" and st["next_runs"]["nightly"]
    assert st["last_success"]["nightly"].startswith("2026-10-10")
    al = {a["id"]: a for a in H.ops_alerts(st, now=NOW)}
    assert al["scheduler_off"]["severity"] == "error"
    assert al["refresh_overdue"]["severity"] == "error"         # weekly profile: > 8 days since the last nightly
    assert al["filler_down"]["severity"] == "info"
    # recently switched off: warning only
    st2 = H.scheduler_status(tmp_data, hb, None, _runs("schedule", NOW - timedelta(hours=5)), NOW)
    assert {a["id"]: a for a in H.ops_alerts(st2, now=NOW)}["scheduler_off"]["severity"] == "warn"


def test_worker_down_and_healthy_states(tmp_data):
    st = H.scheduler_status(tmp_data, None, None, pl.DataFrame(), NOW)
    assert st["reason"] == "worker has never run" and not st["enabled"]
    assert any(a["id"] == "worker_down" for a in H.ops_alerts(st, now=NOW))
    ok = H.scheduler_status(tmp_data, {"alive": True, "scheduler_enabled": True}, None,
                            _runs("schedule", NOW - timedelta(days=2)), NOW)
    ids = {a["id"] for a in H.ops_alerts(ok, filler={"heartbeat_at": NOW.isoformat(), "governor": {"state": "running"}}, now=NOW)}
    assert not ids & {"worker_down", "scheduler_off", "refresh_overdue", "filler_down"}


def test_sla_and_disk_alerts():
    sch = {"worker_alive": True, "enabled": True, "period_days": 7, "last_success": {}}
    cov = {"datasets": {"prices.US": {"label": "Prices US", "status": "stale", "data_as_of": "2026-10-01", "lag": 13, "sla": 6},
                        "news.items": {"label": "News", "status": "stale"}, "prices.EU": {"label": "EU", "status": "fresh"}}}
    al = {a["id"]: a for a in H.ops_alerts(sch, cov, {"heartbeat_at": NOW.isoformat()}, disk_free_gb=12.0, now=NOW)}
    assert al["sla_critical"]["severity"] == "error" and "Prices US" in al["sla_critical"]["detail"]
    assert al["sla_other"]["severity"] == "warn" and al["disk_low"]["severity"] == "error"
