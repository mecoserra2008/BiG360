"""Startup sync A4: the supervisor trigger (startup after the API is ready, every 30 min, sync-now requests; never
two at once, never next to a refresh DAG) and the tray / API entry points."""

from __future__ import annotations

from filelock import FileLock

from app.ops.sync import state as S
from app.ops.sync.trigger import SyncTrigger


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd, self.pid, self.returncode = cmd, 4242, None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


def _trigger(s, **cfg):
    clock = Clock()
    procs: list[FakeProc] = []

    def popen(cmd, **kw):
        p = FakeProc(cmd, **kw)
        procs.append(p)
        return p

    t = SyncTrigger(s, python="py", cfg={"enabled": True, "start_delay_s": 90, "interval_min": 30, "passes": 2,
                                         "stop_grace_s": 1, **cfg}, clock=clock, popen=popen)
    return t, clock, procs


def test_startup_after_the_api_is_ready_then_every_30_min(tmp_data):
    t, clock, procs = _trigger(tmp_data)
    assert t.tick(api_ready=False) is None and procs == []
    assert t.tick(api_ready=True) is None                     # API just became ready: wait start_delay_s
    clock.t += 91
    assert t.tick(api_ready=True) == "startup"
    assert procs[0].cmd[:5] == ["py", "-m", "app.ops.sync", "run", "--trigger"] and "startup" in procs[0].cmd
    clock.t += 5
    assert t.tick() is None and t.status()["state"] == "running"   # never two at once
    procs[0].returncode = 0
    clock.t += 60
    assert t.tick() is None and t.status()["state"] == "idle" and t.status()["next_in_s"] == 1800
    clock.t += 1801
    assert t.tick() == "interval" and len(procs) == 2


def test_requests_from_tray_api_and_cli(tmp_data):
    t, clock, procs = _trigger(tmp_data)
    S.request_sync(tmp_data, by="tray", datasets=["fx.rates"])
    assert t.tick(api_ready=True) == "tray"
    assert procs[0].cmd[-2:] == ["--dataset", "fx.rates"] and S.pending_request(tmp_data) is None
    S.request_sync(tmp_data, by="api")
    assert t.tick() is None and S.pending_request(tmp_data) is not None   # running: the request waits for it
    procs[0].returncode = 0
    assert t.tick() == "api" and len(procs) == 2


def test_no_start_while_another_sync_or_a_refresh_runs(tmp_data):
    t, clock, procs = _trigger(tmp_data, start_delay_s=0)
    lk = S.sync_lock(tmp_data)
    lk.acquire()
    try:
        assert t.tick(api_ready=True) is None and t.state == "external" and procs == []
    finally:
        lk.release()
    tmp_data.locks_dir.mkdir(parents=True, exist_ok=True)
    dl = FileLock(str(tmp_data.locks_dir / "dag_nightly.lock"), timeout=0)
    dl.acquire()
    try:
        S.request_sync(tmp_data, by="api")
        assert t.tick() is None and t.state == "waiting" and "nightly" in t.detail and procs == []
    finally:
        dl.release()
    assert t.tick() == "api"


def test_disabled_by_env(tmp_data, monkeypatch):
    from app.ops.sync.trigger import sync_config

    monkeypatch.setenv("BIG360_SYNC", "0")
    assert sync_config(tmp_data)["enabled"] is False
    t = SyncTrigger(tmp_data, cfg=sync_config(tmp_data), popen=lambda *a, **k: None)
    assert t.tick() is None and t.status()["state"] == "disabled"


def test_stop_writes_the_stop_file(tmp_data):
    t, clock, procs = _trigger(tmp_data, start_delay_s=0)
    t.tick(api_ready=True)
    t.stop(grace_s=0.1)
    assert S.stop_path(tmp_data).exists() and not t.running()


def test_tray_sync_now_and_toast(tmp_data):
    from app.desktop.tray_actions import TrayController

    notes: list[tuple[str, str, str]] = []

    class Api:
        def get(self, path):
            raise OSError("down")

    ctl = TrayController(tmp_data, api=Api(), notify=lambda *n: notes.append(n), cfg={
        "api_host": "127.0.0.1", "api_port": 1, "notifications": True, "update_check_h": 1e9})
    msg = ctl.dispatch("sync")
    assert "Sync requested" in msg and S.pending_request(tmp_data)["by"] == "tray"
    assert any(i.id == "sync" for i in ctl.menu())
    ctl.last_update_check = 1e18
    ctl.seen_sync = "S0"
    S.write_status({"run_id": "S1", "state": "idle", "result": "partial", "trigger": "startup",
                    "finished_at": S.now_iso(), "data_as_of": {"overall": "2026-10-27"},
                    "counts": {"fresh": 30, "failed": 1, "needs_key": 1},
                    "datasets": [{"dataset": "cftc.cot", "reason": "failed"}]}, tmp_data)
    out = ctl._sync_notes()
    assert out and out[0][2] == "warn" and "1 failure" in out[0][0] and "cftc.cot" in out[0][1]
    assert ctl._sync_notes() == []        # each run notified once


def test_supervisor_owns_the_trigger(tmp_data, monkeypatch):
    from app.ops.supervisor import Supervisor

    monkeypatch.setenv("BIG360_SUPERVISOR_REAPER", "0")
    sup = Supervisor(tmp_data, api=False, worker=False, filler=False, sync=True)
    assert sup.sync_trigger is not None and sup._api_ready()        # no API child: ready at once
    st = sup.status()["sync"]
    assert st["enabled"] is True and st["state"] in ("idle", "disabled") and st["interval_min"] == 30
    assert Supervisor(tmp_data, api=False, worker=False, filler=False).sync_trigger is None   # off by default
