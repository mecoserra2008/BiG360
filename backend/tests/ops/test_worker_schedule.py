"""ProfileScheduler: profile -> APScheduler jobs, hot reload on a profile switch, catch-up on start, wake timer."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from app.ops import schedule as S
from app.ops.worker_schedule import ProfileScheduler
from tests.ops.test_schedule import FakeRunner, reg


class FakeWake:
    def __init__(self):
        self.due = None
        self.armed = []

    def arm(self, when):
        self.due = when
        self.armed.append(when)
        return True

    def cancel(self):
        self.due = None


class Awake:
    def __init__(self):
        self.calls = []

    def acquire(self, r):
        self.calls.append(("+", r))

    def release(self, r):
        self.calls.append(("-", r))


def test_profile_jobs_hot_reload_and_heartbeat(tmp_data):
    r = FakeRunner()
    recent = datetime.now(UTC)
    ps = ProfileScheduler(r, tmp_data, reg(), wake_timer=FakeWake(),
                          completed_fn=lambda: {d: {"finished_at": recent} for d in ("nightly", "weekly", "monthly", "cftc_weekly")})
    try:
        ps.start()
        assert sorted(j.id for j in ps.scheduler.get_jobs()) == ["entry_cftc_weekly", "entry_hourly_news",
                                                                 "entry_weekly_refresh"]
        hb = ps.heartbeat()
        assert hb["schedule_profile"] == "weekly" and hb["next_runs"]["weekly_refresh"] and hb["next_runs_by_dag"]["nightly"]
        assert ps.wake.due is not None and ps.wake.due < datetime.fromisoformat(hb["next_runs"]["cftc_weekly"])
        assert r.calls == []                                   # everything recently completed: no catch-up
        S.set_active_profile("daily", tmp_data)
        assert ps.reload_if_changed()
        assert sorted(j.id for j in ps.scheduler.get_jobs()) == ["entry_cftc_weekly", "entry_hourly_news", "entry_monthly",
                                                                 "entry_nightly", "entry_weekly"]
        assert ps.heartbeat()["schedule_profile"] == "daily"
        assert not ps.reload_if_changed()                      # unchanged file: no reschedule
        assert (tmp_data.state_dir / "ops" / "scheduler_seen.json").exists()
    finally:
        ps.shutdown()


def test_catchup_runs_missed_chain_once_with_keep_awake(tmp_data):
    r = FakeRunner()
    aw = Awake()
    ps = ProfileScheduler(r, tmp_data, reg(), keep_awake=aw, completed_fn=lambda: {})
    try:
        ps.start()                                              # never completed -> the missed weekly chain runs now
        for th in ps.threads:
            th.join(10)
        dags = [c[0] for c in r.calls]
        assert dags[:2] == ["nightly", "weekly"] and dags.count("nightly") == 1
        assert ("+", "schedule:weekly_refresh") in aw.calls and ("-", "schedule:weekly_refresh") in aw.calls
        # an entry that is running is never started twice
        ps.running.add("weekly_refresh")
        assert ps.catchup() == [] or "weekly_refresh" not in ps.catchup()
    finally:
        ps.shutdown()


def test_fire_guard_prevents_overlap(tmp_data):
    gate = threading.Event()

    class Slow(FakeRunner):
        def run_dag(self, dag, opts):
            gate.wait(5)
            return super().run_dag(dag, opts)

    r = Slow()
    ps = ProfileScheduler(r, tmp_data, reg(), completed_fn=lambda: {d: {"finished_at": datetime.now(UTC)} for d in ("nightly", "weekly", "cftc_weekly")})
    try:
        ps.start()
        t = threading.Thread(target=ps.fire, args=("cftc_weekly",))
        t.start()
        for _ in range(100):
            if "cftc_weekly" in ps.running:
                break
            threading.Event().wait(0.02)
        assert ps.fire("cftc_weekly") is None                   # second trigger while the first runs
        gate.set()
        t.join(10)
        assert [c[0] for c in r.calls] == ["cftc_weekly"]
    finally:
        ps.shutdown()
