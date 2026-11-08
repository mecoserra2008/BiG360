"""Schedule profiles (config/schedule.yaml), catch-up rule and the weekly refresh chain."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.ops import schedule as S
from tests.platform.conftest import make_registry

LIS = ZoneInfo("Europe/Lisbon")


def reg():
    return make_registry([{"id": "a", "module": "tests.platform.fake_jobs:ok"}],
                         dags={"t": {"cron": None}, "nightly": {"cron": "30 1 * * 2-6"}, "weekly": {"cron": "0 6 * * 6"},
                               "monthly": {"cron": "0 8 1-7 * 0"}, "cftc_weekly": {"cron": "0 19 * * 5"}})


def test_profiles_load_with_weekly_default(tmp_data):
    cfg = S.load_config(tmp_data)
    assert set(cfg["profiles"]) >= {"weekly", "daily", "intraday"}
    assert S.active_profile(tmp_data) == ("weekly", "default")


def test_weekly_profile_is_saturday_0200_lisbon_chain(tmp_data):
    """Refresh-pipeline fixes 2026-10-26: the weekly refresh moved from 08:00 to 02:00 Saturday (Lisbon)."""
    es = {e.id: e for e in S.profile_entries("weekly", reg(), tmp_data)}
    w = es["weekly_refresh"]
    assert w.kind == "chain" and w.steps == ("nightly", "weekly", "monthly_if_due")
    assert w.required_dags == ("nightly", "weekly") and w.dags == ("nightly", "weekly", "monthly")
    nf = w.next_fire(datetime(2026, 10, 20, 12, tzinfo=UTC)).astimezone(LIS)
    assert (nf.weekday(), nf.hour, nf.minute, nf.date().isoformat()) == (5, 2, 0, "2026-10-24")
    c = es["cftc_weekly"]  # CFTC runs after the Friday 15:30 ET release, independently of the chain
    nf = c.next_fire(datetime(2026, 10, 20, tzinfo=UTC)).astimezone(ZoneInfo("America/New_York"))
    assert (nf.weekday(), nf.hour) == (4, 19)
    nr = S.next_runs_by_dag(list(es.values()), datetime(2026, 10, 20, tzinfo=UTC))
    assert nr["nightly"] == nr["weekly"] == nr["monthly"]


def test_daily_profile_uses_jobs_yaml_crons(tmp_data):
    es = {e.id: e for e in S.profile_entries("daily", reg(), tmp_data)}
    assert es["nightly"].cron == "30 1 * * 2-6" and es["weekly"].cron == "0 6 * * 6"
    nf = es["nightly"].next_fire(datetime(2026, 10, 20, 12, tzinfo=UTC)).astimezone(LIS)
    assert nf.date().isoformat() == "2026-10-21" and nf.hour == 1 and nf.minute == 30  # Wed 01:30 (Tue-Sat)
    # intraday is a placeholder that behaves like daily
    assert [e.id for e in S.profile_entries("intraday", reg(), tmp_data)] == list(es)


def test_set_profile_persists_and_env_wins(tmp_data, monkeypatch):
    S.set_active_profile("daily", tmp_data, by="test")
    assert S.active_profile(tmp_data) == ("daily", "state")
    st = S.schedule_state(tmp_data)
    assert st["history"][-1]["to"] == "daily" and st["updated_by"] == "test"
    monkeypatch.setenv("BIG360_SCHEDULE_PROFILE", "weekly")
    assert S.active_profile(tmp_data) == ("weekly", "env")
    with pytest.raises(S.ScheduleError):
        S.set_active_profile("hourly", tmp_data)


def test_previous_fire_and_catchup_rule(tmp_data):
    es = S.profile_entries("weekly", reg(), tmp_data)
    w = next(e for e in es if e.id == "weekly_refresh")
    now = datetime(2026, 10, 19, 10, tzinfo=UTC)          # Monday: the PC was off on Saturday 10-17
    prev = w.previous_fire(now)
    assert prev.astimezone(LIS).date().isoformat() == "2026-10-17"
    fri = datetime(2026, 10, 16, 20, tzinfo=UTC)
    sun = datetime(2026, 10, 18, 9, tzinfo=UTC)
    due = S.catchup_due([w], {"nightly": {"finished_at": fri}, "weekly": {"finished_at": fri}}, now)
    assert [e.id for e, _ in due] == ["weekly_refresh"]
    # a run that completed after the scheduled time (manual catch-up) satisfies it
    assert S.catchup_due([w], {"nightly": {"finished_at": sun}, "weekly": {"finished_at": sun}}, now) == []
    # both required DAGs must be done
    assert S.catchup_due([w], {"nightly": {"finished_at": sun}, "weekly": {"finished_at": fri}}, now)
    # never ran -> due; too old (> max_catchup_days) -> left for the next scheduled run
    assert S.catchup_due([w], {}, now)
    assert S.catchup_due([w], {}, datetime(2026, 10, 23, 23, tzinfo=UTC)) == []
    # an entry already running is not started twice
    assert S.catchup_due([w], {}, now, running={"weekly_refresh"}) == []


def test_monthly_due():
    now = datetime(2026, 10, 24, 8, tzinfo=UTC)
    assert S.monthly_due({}, now)
    assert not S.monthly_due({"monthly": {"finished_at": now - timedelta(days=10)}}, now)
    assert S.monthly_due({"monthly": {"finished_at": now - timedelta(days=29)}}, now)


def _jr(dag_run_id, job, status, start, end=None, trigger="schedule"):
    return {"run_id": f"R{job}{start.isoformat()}", "dag_run_id": dag_run_id, "job_id": job, "status": status,
            "started_at": start, "finished_at": end or start + timedelta(minutes=5), "trigger": trigger}


def test_completed_dag_runs():
    t = datetime(2026, 10, 17, 7, tzinfo=UTC)
    df = pl.DataFrame([
        _jr("D1-nightly", "a", "success", t), _jr("D1-nightly", "b", "failed", t),
        _jr("D2-nightly", "a", "failed", t + timedelta(days=1)),                     # all failed: not completed
        _jr("D3-weekly", "a", "running", t, None),                                   # still running
        _jr("D4-weekly", "a", "partial", t - timedelta(days=7)),
    ])
    c = S.completed_dag_runs(df)
    assert c["nightly"]["dag_run_id"] == "D1-nightly" and c["nightly"]["status"] == "partial"
    assert c["weekly"]["dag_run_id"] == "D4-weekly"


class _Rep:
    def __init__(self, status):
        self.status = status

    def summary(self):
        return self.status


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.stop_event = threading.Event()

    def run_dag(self, dag, opts):
        self.calls.append((dag, dict(opts.params), opts.trigger))
        return _Rep("success")


def test_run_entry_chain_order_and_monthly_if_due(tmp_data):
    w = next(e for e in S.profile_entries("weekly", reg(), tmp_data) if e.id == "weekly_refresh")
    r = FakeRunner()
    out = S.run_entry(w, r, completed_fn=lambda: {"monthly": {"finished_at": datetime.now(UTC) - timedelta(days=3)}},
                      settings=tmp_data)
    assert [c[0] for c in r.calls] == ["nightly", "weekly"]
    assert r.calls[0][1] == {"max_days": 10} and r.calls[0][2] == "schedule"
    assert out["status"] == "success" and out["dags"]["monthly"] == "not_due"
    r2 = FakeRunner()
    S.run_entry(w, r2, completed_fn=lambda: {}, settings=tmp_data)
    assert [c[0] for c in r2.calls] == ["nightly", "weekly", "monthly"]
    runs = S.entry_runs(tmp_data)["weekly_refresh"]
    assert runs["status"] == "success" and runs["last_success_at"] and len(runs["history"]) == 2


def test_schedule_summary_without_worker(tmp_data):
    out = S.schedule_summary(tmp_data, None, reg(), {})
    assert out["active"] == "weekly" and not out["scheduler_enabled"] and not out["pending_apply"]
    assert {p["id"] for p in out["profiles"]} >= {"weekly", "daily", "intraday"}
    assert out["next_runs"]["nightly"] and out["entries"][0]["id"] == "weekly_refresh"
    out = S.schedule_summary(tmp_data, {"alive": True, "scheduler_enabled": True, "schedule_profile": "daily"}, reg(), {})
    assert out["pending_apply"] and out["worker_profile"] == "daily"
