"""Tray menu handlers without a GUI: menu model, open / refresh / pause / schedule / quit, notifications, supervisor
start with back-off, autostart (test registry key only), update check."""

from __future__ import annotations

import json
import sys
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from filelock import FileLock

from app.desktop import autostart, tray_actions as TA
from app.desktop.appconfig import parse_version
from app.desktop.updates import check_for_update, scan_update_dir

NOW = datetime.now(UTC)


def status_payload(alerts=None, enabled=True):
    nxt = (NOW + timedelta(days=3)).isoformat()
    return {
        "data_as_of_overall": "2026-10-16", "serving_as_of": "2026-10-16",
        "scheduler": {"enabled": enabled, "profile": "weekly", "profile_label": "Weekly", "reason": None,
                      "entries": [{"id": "weekly_refresh", "kind": "chain", "dags": ["nightly", "weekly", "monthly"],
                                   "description": "Saturday 08:00 Lisbon", "next_run": nxt},
                                  {"id": "cftc_weekly", "kind": "dag", "dags": ["cftc_weekly"], "next_run": nxt}]},
        "ops_alerts": alerts or [],
    }


SCHEDULE = {"active": "weekly", "profiles": [{"id": "weekly", "label": "Weekly"}, {"id": "daily", "label": "Daily"},
                                             {"id": "intraday", "label": "Intraday (placeholder)", "placeholder": True}]}


class FakeApi:
    def __init__(self):
        self.status = status_payload()
        self.down = False
        self.puts = []

    def get(self, path):
        if self.down:
            raise urllib.error.URLError("connection refused")
        return self.status if path.endswith("/system/status") else SCHEDULE

    def put(self, path, body):
        if self.down:
            raise urllib.error.URLError("connection refused")
        self.puts.append((path, body))
        return {**SCHEDULE, "active": body["profile"]}


class FakePopen:
    calls: list = []

    def __init__(self, cmd, **kw):
        FakePopen.calls.append((cmd, kw))
        self.pid = 4242


@pytest.fixture()
def ctl(tmp_data, monkeypatch):
    urls, paths, notes = [], [], []
    FakePopen.calls = []
    state = {"on": False}
    monkeypatch.setattr(autostart, "is_enabled", lambda *a, **k: state["on"])
    monkeypatch.setattr(autostart, "enable", lambda cmd=None, *a, **k: state.update(on=True) or cmd)
    monkeypatch.setattr(autostart, "disable", lambda *a, **k: state.update(on=False) or True)
    cfg = {"api_host": "127.0.0.1", "api_port": 8095, "serve_ui": True, "filler": False, "realtime": False,
           "start_supervisor": True, "status_poll_s": 60, "notifications": True, "notify_severities": ["error", "warn"],
           "update_dir": "", "update_check_h": 24}
    c = TA.TrayController(tmp_data, api=FakeApi(), open_url=urls.append, open_path=paths.append,
                          notify=lambda *n: notes.append(n), cfg=cfg, popen=FakePopen)
    c._urls, c._paths, c._notes, c._auto = urls, paths, notes, state
    return c


def _ids(items):
    out = []
    for it in items:
        if it.id:
            out.append(it.id)
        out += _ids(it.children)
    return out


def test_menu_model(ctl):
    ctl.poll()
    m = ctl.menu()
    ids = _ids(m)
    for want in ("open", "status", "status:refresh", "refresh", "pause", "schedule:weekly", "schedule:daily",
                 "health", "logs", "autostart", "quit"):
        assert want in ids, want
    assert m[0].id == "open" and m[0].default
    sched = next(i for i in m if i.id == "schedule")
    assert [c.checked for c in sched.children] == [True, False, False]
    assert sched.children[2].enabled is False                       # placeholder profile
    status = next(i for i in m if i.id == "status")
    text = " | ".join(c.label for c in status.children)
    assert "Data as of 2026-10-16" in text and "Schedule: Weekly, scheduler on" in text and "Next refresh:" in text
    assert "Gap filler: not running" in text and "Supervisor: not running" in text
    assert "data 2026-10-16" in ctl.tooltip() and len(ctl.tooltip()) <= 127


def test_open_health_logs(ctl):
    ctl.dispatch("open")
    ctl.dispatch("health")
    ctl.dispatch("logs")
    assert ctl._urls == ["http://127.0.0.1:8095/", "http://127.0.0.1:8095/system/data-health"]
    assert ctl._paths == [str(ctl.s.logs_dir)]


def test_pause_toggle(ctl):
    flag = ctl.s.state_dir / "ops" / "filler.PAUSE"
    assert "paused" in ctl.dispatch("pause") and flag.exists()
    assert next(i for i in ctl.menu() if i.id == "pause").label == "Resume background jobs"
    assert "resumed" in ctl.dispatch("pause") and not flag.exists()


def test_refresh_request_and_fallback(ctl):
    req = ctl.s.state_dir / "ops" / "refresh_now.json"
    msg = ctl.dispatch("refresh")                                     # no worker heartbeat: request waits for it
    assert "as soon as the worker" in msg and json.loads(req.read_text())["by"] == "tray"
    req.unlink()
    hb = {"heartbeat_at": datetime.now(UTC).isoformat(), "scheduler_enabled": True, "accepts_refresh_request": True}  # fresh: NOW is taken at collection
    (ctl.s.state_dir / "worker.json").write_text(json.dumps(hb))
    ctl.poll()
    assert "within a few seconds" in ctl.dispatch("refresh") and req.exists()
    req.unlink()
    hb["accepts_refresh_request"] = False                             # older worker: queue the nightly DAG instead
    (ctl.s.state_dir / "worker.json").write_text(json.dumps(hb))
    assert "nightly DAG" in ctl.dispatch("refresh") and not req.exists()
    queued = list((ctl.s.state_dir / "queue").glob("*.json"))
    assert len(queued) == 1 and json.loads(queued[0].read_text())["dag"] == "nightly"


def test_schedule_via_api_and_local_fallback(ctl):
    assert "daily" in ctl.dispatch("schedule:daily")
    assert ctl.api.puts == [("/api/v1/system/schedule", {"profile": "daily"})]
    ctl.api.down = True                                               # API down: same state file, worker hot-reloads
    assert "weekly" in ctl.dispatch("schedule:weekly")
    assert json.loads((ctl.s.state_dir / "schedule.json").read_text())["profile"] == "weekly"
    assert "not changed" in ctl.set_schedule("nope")


def test_notifications(ctl):
    err = {"id": "worker_down", "severity": "error", "title": "Worker not running", "detail": "x"}
    warn = {"id": "sla_other", "severity": "warn", "title": "2 dataset(s) stale beyond SLA", "detail": "GDELT"}
    info = {"id": "filler_down", "severity": "info", "title": "Gap filler not running", "detail": ""}
    ctl.quiet_until = 1e18                                            # start-up quiet period: nothing
    ctl.api.status = status_payload([err])
    assert ctl.poll() == [] and ctl.seen_alerts is None
    ctl.quiet_until = 0.0
    n = ctl.poll()                                                    # first poll: one summary of errors
    assert len(n) == 1 and n[0][2] == "error" and "1 ops alert" in n[0][0]
    assert ctl.poll() == []                                           # unchanged: silent
    ctl.api.status = status_payload([err, warn, info])
    n = ctl.poll()
    assert [x[0] for x in n] == ["BiG360: 2 dataset(s) stale beyond SLA"]   # info is not notified
    assert ctl._notes[-1] == n[-1]
    # refresh finished / failed (schedule_runs.json written by the worker)
    runs = ctl.s.state_dir / "ops" / "schedule_runs.json"
    runs.write_text(json.dumps({"weekly_refresh": {"status": "success", "finished_at": "2026-10-17T09:00:00+00:00",
                                                   "dags": {"nightly": "success"}}}))
    assert ctl.poll() == [("BiG360: refresh weekly_refresh success", "nightly success", "info")]
    assert ctl.poll() == []
    runs.write_text(json.dumps({"weekly_refresh": {"status": "failed", "finished_at": "2026-10-24T09:00:00+00:00",
                                                   "dags": {"nightly": "failed"}}}))
    n = ctl.poll()
    assert n == [("BiG360: refresh weekly_refresh failed", "nightly failed", "error")]
    ctl.api.down = True
    ctl.poll()
    assert ctl.status is None and "not reachable" in " ".join(ctl.status_lines())
    assert ctl.tooltip() == "BiG Equity 360 - stopped"


def test_no_alert_burst_when_status_cache_is_warming_or_ops_block_failed(ctl):
    """Coverage v3 A7 review: a warming / failed status cache or a failed ops block (empty ops_alerts with HTTP 200)
    must not reset seen_alerts - the next good poll would re-toast every active alert."""
    a1 = {"id": "worker_down", "severity": "error", "title": "Worker not running", "detail": "x"}
    a2 = {"id": "sla_other", "severity": "warn", "title": "2 dataset(s) stale beyond SLA", "detail": "GDELT"}
    ctl.quiet_until = 0.0
    ctl.api.status = status_payload([a1, a2])
    assert len(ctl.poll()) == 1 and ctl.seen_alerts == {"worker_down", "sla_other"}
    for cache in ({"state": "warming", "ttl_s": 10, "section_errors": []},
                  {"state": "error", "ttl_s": 10, "section_errors": []},
                  {"state": "partial", "ttl_s": 10, "section_errors": ["ops_status: ValueError: jobs.yaml invalid"]}):
        ctl.api.status = {**status_payload([]), "cache": cache}
        assert ctl.poll() == [] and ctl.seen_alerts == {"worker_down", "sla_other"}
    ctl.api.status = {**status_payload([a1, a2]), "cache": {"state": "fresh", "ttl_s": 10, "section_errors": []}}
    assert ctl.poll() == []                                           # same alerts again: no burst
    # another section failing (not the ops block) still diffs alerts normally
    ctl.api.status = {**status_payload([a1]), "cache": {"state": "partial", "ttl_s": 10,
                                                         "section_errors": ["last_dag_runs: RuntimeError: x"]}}
    assert ctl.poll() == [] and ctl.seen_alerts == {"worker_down"}


def test_quit_and_supervisor_management(ctl):
    lock_path = ctl.s.state_dir / "ops" / "supervisor.lock"
    held = FileLock(str(lock_path), timeout=0)
    held.acquire()                                                    # "a supervisor is running"
    try:
        assert ctl.supervisor_running()
        assert ctl.ensure_supervisor() is None and FakePopen.calls == []   # running: never started twice
        assert "Stopping" in ctl.dispatch("quit")
        assert (ctl.s.state_dir / "ops" / "supervisor.STOP").exists()
        assert ctl.wait_stopped(timeout=0.3, step=0.1) is False
    finally:
        held.release()
    assert ctl.wait_stopped(timeout=2, step=0.1) is True
    assert ctl.ensure_supervisor() is None and FakePopen.calls == []       # quitting: no restart


def test_quit_during_startup_race(ctl, monkeypatch):
    import threading
    import time

    alive = {"v": True}
    monkeypatch.setattr(TA.TrayController, "_own_supervisor_alive", lambda self: alive["v"])
    ctl.supervisor_pid = 4242
    assert "still starting" in ctl.quit()                             # no lock yet: STOP would be ignored now
    stop = ctl.s.state_dir / "ops" / "supervisor.STOP"
    assert not stop.exists()
    lk = FileLock(str(ctl.s.state_dir / "ops" / "supervisor.lock"), timeout=0)

    def supervisor():
        time.sleep(0.3)
        lk.acquire()                                                  # it finally takes its lock ...
        for _ in range(100):
            if stop.exists():                                         # ... sees the stop request ...
                break
            time.sleep(0.05)
        lk.release()                                                  # ... and exits
        alive["v"] = False

    t = threading.Thread(target=supervisor)
    t.start()
    assert ctl.wait_stopped(timeout=10, step=0.05)
    t.join()
    assert stop.exists()


def test_supervisor_start_backoff_and_command(ctl, monkeypatch):
    assert ctl.ensure_supervisor() is None                            # first start: silent
    assert len(FakePopen.calls) == 1
    cmd, kw = FakePopen.calls[0]
    assert cmd[1:5] == ["-m", "app.ops.supervisor", "--app", "--api-host"]
    assert "--serve-ui" in cmd and "--no-filler" in cmd and cmd[cmd.index("--api-port") + 1] == "8095"
    assert kw["cwd"].endswith("backend") and kw["env"]["BIG360_APP"] == "1"
    assert ctl.ensure_supervisor() is None and len(FakePopen.calls) == 1   # back-off window
    ctl.sup_next_try = 0.0
    assert "restarted" in ctl.ensure_supervisor() and len(FakePopen.calls) == 2
    monkeypatch.setattr(sys, "executable", str(Path(sys.executable).with_name("pythonw.exe")))
    if Path(sys.executable).with_name("python.exe").exists():
        assert Path(ctl.supervisor_command()[0]).name.lower() == "python.exe"   # console python (hidden window)


def test_autostart_toggle(ctl):
    assert "starts at login" in ctl.dispatch("autostart") and ctl._auto["on"]
    assert next(i for i in ctl.menu() if i.id == "autostart").checked
    assert "no longer" in ctl.dispatch("autostart") and not ctl._auto["on"]


@pytest.mark.skipif(sys.platform != "win32", reason="registry")
def test_autostart_registry_test_key(tmp_path):
    import winreg

    key = r"Software\BiG360-pytest\Run"
    try:
        assert autostart.get(key, "T") is None
        assert autostart.enable('"C:\\x\\BiG360.exe" --background', key, "T").endswith("--background")
        assert autostart.is_enabled(key, "T") and autostart.get(key, "T") == '"C:\\x\\BiG360.exe" --background'
        assert autostart.disable(key, "T") and not autostart.is_enabled(key, "T")
        assert autostart.disable(key, "T") is False
    finally:
        for k in (r"Software\BiG360-pytest\Run", r"Software\BiG360-pytest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, k)
            except OSError:
                pass
    (tmp_path / "BiG360.exe").write_bytes(b"MZ")
    assert autostart.launch_command(tmp_path) == f'"{tmp_path / "BiG360.exe"}" --background'
    assert autostart.launch_command(tmp_path / "nope").startswith("wscript.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="directory junction")
def test_models_link(tmp_path):
    from types import SimpleNamespace

    import os

    models = tmp_path / "models_store"
    (models / "m1").mkdir(parents=True)
    (models / "m1" / "model.json").write_text("{}")
    app = tmp_path / "install"
    (app / "models_store").mkdir(parents=True)                         # empty placeholder (ensure_dirs)
    s = SimpleNamespace(project_root=app, data_dir=tmp_path / "data")
    try:
        assert TA.ensure_models_link(s) == str(models)
        assert (app / "models_store" / "m1" / "model.json").is_file() and (app / "models_store").is_junction()
        assert TA.ensure_models_link(s) is None                       # already linked
    finally:
        if (app / "models_store").is_junction():
            os.rmdir(app / "models_store")                            # removes the link only
    assert (models / "m1" / "model.json").is_file()
    dev = SimpleNamespace(project_root=tmp_path, data_dir=tmp_path / "data")   # dev tree: models next to data
    assert TA.ensure_models_link(dev) is None


def test_update_check(tmp_path, ctl):
    assert parse_version("1.2.10") > parse_version("1.2.9") and parse_version("1.0") == parse_version("1.0.0")
    assert check_for_update("1.0.0", None)["enabled"] is False
    assert scan_update_dir(tmp_path / "missing") is None
    (tmp_path / "BiG360Setup-1.0.0.exe").write_bytes(b"x")
    (tmp_path / "BiG360-1.2.0-portable.zip").write_bytes(b"x")
    (tmp_path / "BiG360Setup-1.2.0.exe").write_bytes(b"x")
    r = check_for_update("1.0.0", tmp_path)
    assert r["available"] and r["latest"] == "1.2.0" and r["file"].endswith("BiG360Setup-1.2.0.exe")
    assert not check_for_update("1.2.0", tmp_path)["available"]
    (tmp_path / "latest.json").write_text(json.dumps({"version": "2.0.0", "file": "BiG360Setup-2.0.0.exe"}))
    assert check_for_update("1.2.0", tmp_path)["latest"] == "2.0.0"
    ctl.cfg["update_dir"] = str(tmp_path)
    n = ctl.check_update()
    assert n and "2.0.0" in n[0][1]
    assert any(i.id == "update" for i in ctl.menu())
    ctl.dispatch("update")
    assert ctl._paths[-1] == str(tmp_path)
