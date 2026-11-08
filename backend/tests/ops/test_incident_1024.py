"""Incident 2026-10-24 (docs/INCIDENT_2026-10-24.md): Job Object tree kill, orphan reaper, supervisor cold-start
grace / back-off / no overlap, filler pause + heavy tasks + machine guard, runner leak reaping + pressure deferral,
news finalise guard / attempt back-off / timeout, bounded GDELT in-flight window.

Every test only touches processes it started itself (the reaper is always restricted with only_pids)."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import app
from app.ops import governor as gov
from app.ops import reaper as R
from app.ops import supervisor as SUP
from app.ops import tasks as T
from app.ops import winsys
from tests.ops import proc_helpers as H

BACKEND = Path(app.__file__).resolve().parents[1]
NOWIN = pytest.mark.skipif(not winsys.IS_WINDOWS, reason="Windows Job Objects / process APIs")


def _wait(cond, timeout=60.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


def _kill_all(pids):
    for p in pids:
        if p and winsys.pid_alive(p):
            winsys.kill_tree(p)


# ------------------------------------------------------------------------------------------------ Job Object


@NOWIN
def test_job_object_kills_orphaned_spawn_grandchild(tmp_path):
    """parent -> spawn child -> spawn grandchild; the child dies (grandchild orphaned: invisible to the parent-pid
    tree walk of kill_tree). Terminating the job must kill the grandchild too."""
    pidfile = tmp_path / "pids.txt"
    script = H.write_launcher(tmp_path, "launch", "orphan_grandchild", pidfile, BACKEND)
    job = winsys.JobObject(name="test")
    assert job.ok, job.error
    p = winsys.start_in_job([sys.executable, str(script)], job, cwd=str(BACKEND))   # suspended -> assigned -> resumed
    assert job.contains(p.pid)
    child = grand = None
    try:
        child, grand = H.wait_pidfile(pidfile)
        assert _wait(lambda: not winsys.pid_alive(child), 30), "child should have exited"
        assert winsys.pid_alive(grand)
        assert grand not in winsys.process_tree(p.pid), "the pid-tree walk cannot see the orphan (the 2026-10-24 leak)"
        assert job.contains(grand) and grand in job.pids()
        assert job.memory_mb() > 0
        n = job.terminate()
        assert n >= 2
        assert job.wait_empty(15)
        assert _wait(lambda: not winsys.pid_alive(grand), 15) and p.wait(15) is not None
    finally:
        _kill_all([grand, p.pid])
        job.close()


@NOWIN
def test_job_close_kills_members(tmp_path):
    # fallback path (no suspended start): the tree is assigned deepest first once it exists
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    job = winsys.JobObject(name="close")
    try:
        _wait(lambda: len(winsys.process_tree(p.pid)) >= 2, 10)   # venv launcher -> interpreter
        assert job.assign_tree(p.pid) >= 1
        assert all(job.contains(x) for x in winsys.process_tree(p.pid))
        job.close()                                   # KILL_ON_JOB_CLOSE (= the owner died)
        assert p.wait(15) is not None
    finally:
        _kill_all([p.pid])


# ------------------------------------------------------------------------------------------------ reaper


@NOWIN
def test_reaper_only_kills_our_orphans(tmp_path):
    ours_pf, foreign_pf, live_pf = tmp_path / "ours.pid", tmp_path / "foreign.pid", tmp_path / "live.pid"
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    procs = [
        subprocess.Popen([sys.executable, str(H.write_launcher(tmp_path, "a", "orphan_child", ours_pf, BACKEND))],
                         cwd=str(BACKEND)),
        subprocess.Popen([sys.executable, str(H.write_launcher(tmp_path, "b", "orphan_child", foreign_pf, BACKEND))],
                         cwd=str(foreign_cwd)),                      # same interpreter, NOT our installation
        subprocess.Popen([sys.executable, str(H.write_launcher(tmp_path, "c", "live_parent", live_pf, BACKEND))],
                         cwd=str(BACKEND)),                          # ours, but its parent is alive
    ]
    plain = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], cwd=str(BACKEND))  # not spawn
    pids: list[int] = []
    try:
        ours, foreign, live = (H.wait_pidfile(f)[0] for f in (ours_pf, foreign_pf, live_pf))
        pids = [ours, foreign, live]
        assert _wait(lambda: procs[0].poll() is not None and procs[1].poll() is not None, 30)
        only = {ours, foreign, live, plain.pid}
        found = R.find_leaked(project_root=BACKEND.parent, only_pids=only, min_age_s=0)
        assert [x["pid"] for x in found] == [ours], found
        assert found[0]["cwd"].lower().startswith(str(BACKEND).lower())
        # a young orphan is left alone (its parent may still be attaching)
        assert R.find_leaked(project_root=BACKEND.parent, only_pids=only, min_age_s=3600) == []
        rep = R.reap(project_root=BACKEND.parent, only_pids=only, min_age_s=0)
        assert rep["leaked"] == 1 and ours in rep["killed"]
        assert _wait(lambda: not winsys.pid_alive(ours), 15)
        assert winsys.pid_alive(foreign) and winsys.pid_alive(live) and plain.poll() is None
        dry = R.reap(project_root=BACKEND.parent, only_pids=only, min_age_s=0, dry_run=True)
        assert dry["leaked"] == 0
    finally:
        _kill_all(pids + [p.pid for p in procs] + [plain.pid])


def test_stray_api_detection_helpers():
    assert R.is_serve_api('"C:\\x\\python.exe" -m app.cli serve api --host 127.0.0.1 --port 8010')
    assert not R.is_serve_api("python -m app.cli realtime start")
    assert R.api_port_of("python -m app.cli serve api --port 8790") == 8790
    assert R.api_port_of("python -m app.cli serve api") == 8010


# ------------------------------------------------------------------------------------------------ supervisor


FAKE_API = """\
import http.server, socketserver, sys, time, pathlib
port, delay, ok_n, mark = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
pathlib.Path(mark).open("a").write("start\\n")
time.sleep(delay)                               # slow cold start: the port opens only after `delay`
n = [0]
class Hd(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        n[0] += 1
        self.send_response(200 if n[0] <= ok_n else 500)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a):
        pass
http.server.ThreadingHTTPServer(("127.0.0.1", port), Hd).serve_forever()
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def fast_health(monkeypatch):
    monkeypatch.setattr(SUP, "HEALTH_EVERY_S", 0.3)
    monkeypatch.setattr(SUP, "HEALTH_FIRST_S", 0.0)
    monkeypatch.setattr(SUP, "HEALTH_FAILS", 2)
    monkeypatch.setattr(SUP, "HEALTH_TIMEOUT_S", 2.0)
    monkeypatch.setattr(SUP, "HEALTH_TIMEOUT_LOAD_S", 2.0)
    monkeypatch.setattr(SUP, "RESTART_BACKOFF_S", (1, 2, 4))
    monkeypatch.setattr(SUP, "ALERT_RESTARTS", 3)
    monkeypatch.setattr(SUP, "under_pressure", lambda: False)


def _drive(sup, c, cond, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        sup.check_child(c)
        if cond():
            return True
        time.sleep(0.1)
    return False


@NOWIN
def test_supervisor_cold_start_grace_then_backoff_and_alert(tmp_data, tmp_path, monkeypatch, fast_health):
    """A child that binds its port only after 4 s (3 s cold-start budget would be too short for a 1-s grace, the old
    90-s grace analogue) is never health-failed before the port opens; once ready and failing, restarts back off
    exponentially, a new instance never starts while the old one is alive, and 3 restarts raise the alert."""
    monkeypatch.setattr(SUP, "COLD_START_MAX_S", 20.0)
    port, mark = _free_port(), tmp_path / "starts.txt"
    script = tmp_path / "fake_api.py"
    script.write_text(FAKE_API, encoding="utf-8")
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("api", [sys.executable, str(script), str(port), "4", "1", str(mark)], grace_s=2,
                  port=("127.0.0.1", port))
    sup.children = [c]
    starts: list[tuple[float, list[int]]] = []
    real_start = sup.start_child

    def start_spy(ch):
        alive_old = [p for p in ch.old_pids if winsys.pid_alive(p)]
        starts.append((time.monotonic(), alive_old))
        real_start(ch)

    monkeypatch.setattr(sup, "start_child", start_spy)
    try:
        sup.start_child(c)
        assert c.state == "running" and c.job is not None
        t0 = time.monotonic()
        max_fail_before_ready = 0
        while not c.ready and time.monotonic() - t0 < 30:
            sup.check_child(c)
            max_fail_before_ready = max(max_fail_before_ready, c.health_fail)
            time.sleep(0.1)
        assert c.ready, "port never opened"
        assert time.monotonic() - t0 >= 3.5                       # slow bind honoured
        assert max_fail_before_ready == 0 and c.restarts == 0       # nothing counted while the port was closed
        # health: 1 OK then 500s -> restart #1 (back-off 1 s), #2 (2 s), #3 (4 s) -> alert
        assert _drive(sup, c, lambda: c.restarts >= 3, timeout=120), (c.state, c.reasons)
        backoffs = [r["backoff_s"] for r in reversed(c.reasons)]
        assert backoffs[:3] == [1, 2, 4], c.reasons
        assert all(r["reason"].startswith("health:") for r in c.reasons[:3])
        assert len(starts) >= 3
        assert all(not alive for _, alive in starts), "a new instance started while the old one was alive"
        assert c.alert and c.alert["restarts_30m"] >= 3
        st = sup.status()
        assert st["children"]["api"]["restarts_30m"] >= 3 and st["restarts_total"] >= 3
        assert any(a["id"] == "restart_storm_api" for a in st["alerts"])
        assert st["children"]["api"]["restart_reasons"][0]["reason"].startswith("health:")
    finally:
        sup.stop_child(c)
        if c.job is not None:
            c.job.terminate()
            c.job.close()


@NOWIN
def test_supervisor_cold_start_timeout_restarts(tmp_data, tmp_path, monkeypatch, fast_health):
    monkeypatch.setattr(SUP, "COLD_START_MAX_S", 2.0)
    port, mark = _free_port(), tmp_path / "starts.txt"
    script = tmp_path / "fake_api.py"
    script.write_text(FAKE_API, encoding="utf-8")
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("api", [sys.executable, str(script), str(port), "600", "1", str(mark)], grace_s=1,
                  port=("127.0.0.1", port))
    sup.children = [c]
    try:
        sup.start_child(c)
        old = c.popen.pid
        assert _drive(sup, c, lambda: c.restarts >= 1, timeout=30)
        assert c.reasons[0]["reason"].startswith("cold start") and c.health_fail == 0
        assert _wait(lambda: not winsys.pid_alive(old), 15)
        assert c.state in ("backoff", "stopping")
    finally:
        sup.stop_child(c)
        if c.job is not None:
            c.job.terminate()
            c.job.close()


@NOWIN
def test_supervisor_stop_kills_orphaned_descendants(tmp_data, tmp_path):
    """A child whose own child died leaving a grandchild: stop_child must leave nothing behind (job object)."""
    pidfile = tmp_path / "pids.txt"
    script = H.write_launcher(tmp_path, "launch", "orphan_grandchild", pidfile, BACKEND)
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("svc", [sys.executable, str(script)], grace_s=1)
    sup.children = [c]
    grand = None
    try:
        sup.start_child(c)
        _child, grand = H.wait_pidfile(pidfile)
        assert winsys.pid_alive(grand)
        sup.stop_child(c)
        assert c.state == "stopped"
        assert _wait(lambda: not winsys.pid_alive(grand), 15)
    finally:
        _kill_all([grand])


# ------------------------------------------------------------------------------------------------ filler


STOP_AWARE = "import time,pathlib\np=pathlib.Path(r'{stop}')\nwhile not p.exists(): time.sleep(0.05)\n"
STUBBORN = ("import signal,time\n"
            "signal.signal(getattr(signal,'SIGBREAK',signal.SIGINT), signal.SIG_IGN)\n"
            "time.sleep(300)\n")


@NOWIN
def test_filler_pause_stops_running_tasks(tmp_data, tmp_path, monkeypatch):
    from app.ops.filler import Filler

    f = Filler(tmp_data)
    f.gov = {**f.gov, "pause_on_metered": False, "pause_on_battery": False, "pause_grace_s": 1,
             "min_free_ram_gb": 0, "resume_free_ram_gb": 0, "min_disk_free_gb": 0}
    f.gov["max_concurrency"] = 2
    f._last_hk = time.monotonic()
    monkeypatch.setattr(f, "candidates", lambda: [])          # this test starts nothing from the queue
    monkeypatch.setattr("app.ops.filler.machine_guard", lambda **kw: {"reason": None})
    stop = tmp_path / "STOP"
    graceful = T.TaskProc(T.TaskSpec("campaign:x", "x", "campaign", 70, [sys.executable, "-c", STOP_AWARE.format(stop=stop)],
                                     stop_file=stop), tmp_data, priority="normal")
    stubborn = T.TaskProc(T.TaskSpec("gap:y", "y", "gap", 20, [sys.executable, "-c", STUBBORN]), tmp_data,
                          priority="normal")
    try:
        for t in (graceful, stubborn):
            t.start()
            f.running[t.spec.task_id] = t
        assert graceful.job is not None and stubborn.job is not None
        f.tick()
        assert f.last_decision["state"] == "running" and len(f.running) == 2
        (tmp_data.state_dir / "ops" / "filler.PAUSE").write_text("test", encoding="utf-8")
        f.tick()
        assert f.last_decision["state"] == "paused" and "paused by user" in f.last_decision["reasons"]
        assert stop.exists() and graceful.stop_requested_at and stubborn.stop_requested_at
        assert stubborn.stop_grace_s == 1                     # pause grace, not the 120 s / 900 s default
        assert _wait(lambda: (f.tick() or True) and not f.running, 30), list(f.running)
        rec = {r["task_id"]: r["outcome"] for r in f.state["recent"]}
        assert rec == {"campaign:x": "stopped", "gap:y": "stopped"}
        assert _wait(lambda: not winsys.pid_alive(stubborn.pid) and not winsys.pid_alive(graceful.pid), 15)
        assert not stop.exists()
    finally:
        for t in (graceful, stubborn):
            if t.pid and winsys.pid_alive(t.pid):
                t.kill()


@NOWIN
def test_task_leftovers_are_reaped_when_the_root_exits(tmp_data, tmp_path):
    """The JP-gap pattern: the task root exits, its job process (a spawn child) keeps running -> killed."""
    pidfile = tmp_path / "pid.txt"
    script = H.write_launcher(tmp_path, "launch", "orphan_child", pidfile, BACKEND)
    t = T.TaskProc(T.TaskSpec("gap:jp", "jp", "gap", 20, [sys.executable, str(script)]), tmp_data)
    t.start()
    orphan = None
    try:
        orphan = H.wait_pidfile(pidfile)[0]
        assert _wait(lambda: t.poll() is not None, 30)
        assert winsys.pid_alive(orphan)
        t.cleanup()
        assert t.leftovers_killed >= 1
        assert _wait(lambda: not winsys.pid_alive(orphan), 15)
    finally:
        _kill_all([orphan])


def test_heavy_tasks_run_alone_and_machine_guard():
    dec = gov.Decision("running", allow_new=True, ram_budget_gb=10.0)
    light = gov.RunningInfo("gap:a", 20, 0.5)
    heavy = gov.RunningInfo("campaign:gdelt_e6", 70, 3.0, heavy=True)
    assert gov.admit(1.0, dec, 1, running=[light])                     # light next to light: ok
    assert not gov.admit(2.0, dec, 1, heavy=True, running=[light])     # CC-NEWS next to a taskrun: no
    assert not gov.admit(0.5, dec, 1, running=[heavy])                 # nothing next to E6 / CC-NEWS
    assert gov.admit(2.0, dec, 0, heavy=True, running=[])              # alone: yes
    assert gov.is_heavy(4.5) and not gov.is_heavy(2.0)
    # machine guard: free RAM floor, pagefile growth trend
    h: deque = deque()
    assert gov.machine_guard({"avail_gb": 2.0, "pagefile_used_gb": 1.0}, history=h, now=0)["pressure"]
    h.clear()
    assert not gov.machine_guard({"avail_gb": 5.0, "pagefile_used_gb": 4.0}, history=h, now=0)["pressure"]
    assert not gov.machine_guard({"avail_gb": 5.0, "pagefile_used_gb": 4.5}, history=h, now=60)["pressure"]
    g = gov.machine_guard({"avail_gb": 4.5, "pagefile_used_gb": 6.0}, history=h, now=200)
    assert g["pressure"] and g["reason"].startswith("pagefile growing") and g["pagefile_growth_gb"] == 2.0
    assert not gov.machine_guard({"avail_gb": 12.0, "pagefile_used_gb": 8.0}, history=h, now=250)["pressure"]
    d = gov.decide(gov.Sample(free_ram_gb=4.5, on_ac=True, pagefile_growing=g["reason"]),
                   [gov.RunningInfo("campaign:ccnews_history", 72, 2.0)])
    assert d.state == "paused" and d.stop == ["campaign:ccnews_history"]


# ------------------------------------------------------------------------------------------------ runner


def test_runner_reaps_leaked_grandchild_of_a_process_job(tmp_data, tmp_path):
    if not winsys.IS_WINDOWS:
        pytest.skip("Job Objects")
    from app.scheduler.runner import JobRunner, RunOptions
    from tests.platform.conftest import FAKE, make_registry

    pidfile = tmp_path / "pids.txt"
    reg = make_registry([{"id": "leaky", "module": f"{FAKE}:leak_grandchild", "executor": "process", "timeout_min": 2,
                          "params": {"pidfile": str(pidfile)}}])
    o = JobRunner(reg, tmp_data).run_job("leaky", RunOptions(as_of=date(2026, 10, 23)))
    assert o.status == "success", o.message
    _child, grand = (int(x) for x in o.message.split())
    assert _wait(lambda: not winsys.pid_alive(grand), 15), "leaked grandchild survived the job"


def test_news_finalise_style_timeout_kills_pool_tree(tmp_data, tmp_path):
    from app.scheduler.runner import JobRunner, RunOptions
    from tests.platform.conftest import FAKE, make_registry

    pidfile = tmp_path / "pool.txt"
    reg = make_registry([{"id": "hung", "module": f"{FAKE}:pool_hang", "executor": "process", "timeout_min": 0.5,
                          "retries": 0, "params": {"pidfile": str(pidfile)}}])
    pids: list[int] = []
    try:
        o = JobRunner(reg, tmp_data).run_job("hung", RunOptions(as_of=date(2026, 10, 23)))
        assert o.status == "failed" and o.error_type == "TimeoutError", o.message
        pids = [int(x) for x in pidfile.read_text().split()]
        assert len(pids) >= 2
        assert _wait(lambda: not any(winsys.pid_alive(p) for p in pids), 15), [p for p in pids if winsys.pid_alive(p)]
    finally:
        _kill_all(pids)


def test_runner_defers_heavy_job_under_memory_pressure(tmp_data, monkeypatch):
    from app.scheduler import runner as RN
    from tests.platform.conftest import FAKE, make_registry

    reg = make_registry([{"id": "news_finalise", "module": f"{FAKE}:pid", "executor": "process"}])
    r = RN.JobRunner(reg, tmp_data)
    r.pressure_guard = True
    monkeypatch.setattr(RN, "machine_pressure", lambda: "free RAM 1.2 GB < 3 GB")
    o = r.run_job("news_finalise", RN.RunOptions(as_of=date(2026, 10, 23)))
    assert o.status == "skipped" and o.reason == "deferred" and "pressure" in o.message
    assert not o.blocks_dependents


def test_jobs_yaml_news_finalise_bounded():
    from app.scheduler.runner import JobRegistry

    j = JobRegistry.load().jobs["news_finalise"]
    assert j.executor == "process" and j.timeout_min <= 45 and j.retries == 0


# ------------------------------------------------------------------------------------------------ news finalise


def test_news_finalise_run_marker_stale_guard_and_attempt_backoff(tmp_data):
    from app.news_live import compaction as C

    now = datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
    # free -> ours, released afterwards
    assert C.acquire_run_marker(tmp_data, now) is None
    assert C.run_marker_path(tmp_data).exists()
    C.release_run_marker(tmp_data)
    assert not C.run_marker_path(tmp_data).exists()
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)  # app.news_live finalise"])
    try:
        mk = C.run_marker_path(tmp_data)
        # a live, fresh run of another process -> skip (never queue behind it)
        C.hot.write_json_atomic({"pid": other.pid, "started_at": datetime.now(UTC).isoformat()}, mk)
        assert "pid" in (C.acquire_run_marker(tmp_data, now) or "")
        rep = C.run_finalise(tmp_data, now=now, days=[date(2026, 10, 23)])
        assert rep["days"] == {} and rep.get("already_running")
        # dead pid -> stale, taken over
        C.hot.write_json_atomic({"pid": 999999, "started_at": datetime.now(UTC).isoformat()}, mk)
        assert C.acquire_run_marker(tmp_data, now) is None
        # live but older than the stale age -> taken over, our stale process is killed
        C.hot.write_json_atomic({"pid": other.pid, "started_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
                                mk)
        assert C.acquire_run_marker(tmp_data, now) is None
        assert other.wait(15) is not None
        C.release_run_marker(tmp_data)
    finally:
        if other.poll() is None:
            other.kill()
    # attempts: 3 failed tries -> deferred for FINALISE_RETRY_AFTER_H; force ignores it; HOLD defers everything
    d = date(2026, 10, 23)
    for _ in range(C.FINALISE_MAX_ATTEMPTS):
        C.note_attempt(tmp_data, d, now)
    st = C.read_state(tmp_data)
    assert C.attempt_backoff_h(st, d, now + timedelta(hours=1)) == pytest.approx(C.FINALISE_RETRY_AFTER_H - 1, abs=0.01)
    assert C.attempt_backoff_h(st, d, now + timedelta(hours=7)) == 0.0
    rep = C.run_finalise(tmp_data, now=now + timedelta(hours=1), days=[d], sentiment=False)
    assert d.isoformat() in rep["deferred"] and rep["days"] == {}
    C.finalise_hold_path(tmp_data).parent.mkdir(parents=True, exist_ok=True)
    C.finalise_hold_path(tmp_data).write_text("x", encoding="utf-8")
    assert C.run_finalise(tmp_data, now=now, days=[d]).get("held")
    from tests.news_live.helpers import seed_dicts

    seed_dicts(tmp_data)
    rep = C.run_finalise(tmp_data, now=now + timedelta(hours=1), days=[d], sentiment=False, force=True)
    assert "deferred" not in rep and d.isoformat() in rep["days"]       # forced: attempted (no data -> trivial)
    assert not C.run_marker_path(tmp_data).exists()


def test_finalise_job_reports_skip_when_held(tmp_data):
    from app.news_live import compaction as C
    from app.news_live import jobs as J
    from app.scheduler.base import JobContext

    C.finalise_hold_path(tmp_data).parent.mkdir(parents=True, exist_ok=True)
    C.finalise_hold_path(tmp_data).write_text("x", encoding="utf-8")
    ctx = JobContext(job_id="news_finalise", run_id="R1", as_of=date(2026, 10, 23), params={}, settings=tmp_data)
    res = J.finalise(ctx)
    assert res.status == "skipped" and "held" in res.message


def test_fetch_day_bounds_files_in_memory(tmp_data, monkeypatch):
    """At most max_inflight files are downloaded-but-not-parsed (the old code queued a whole day of zips)."""
    from app.events_v2 import pipeline as PL

    import threading

    held = {"now": 0, "max": 0}
    lk = threading.Lock()

    def fake_fetch(url):
        with lk:
            held["now"] += 1
            held["max"] = max(held["max"], held["now"])
        return b"zip", 0.0

    def fake_parse(kind, data, legacy=False):
        time.sleep(0.005)
        with lk:
            held["now"] -= 1
        return kind, None, {"rows": 1}, None

    monkeypatch.setattr(PL, "parse_file", fake_parse)
    st = object.__new__(PL.Streamer)
    st.s, st.legacy, st.use_hot, st.max_inflight = tmp_data, False, False, 5
    st.pause_check = st.stop_check = None
    st._fetch = fake_fetch
    st.dpool, st.ppool = ThreadPoolExecutor(4), ThreadPoolExecutor(2)
    try:
        out = st.fetch_day(date(2026, 10, 20), datetime(2026, 10, 20, 5, 45, tzinfo=UTC))
    finally:
        st.dpool.shutdown()
        st.ppool.shutdown()
    assert out["files"] == 24 * 6 and out["files_ok"] == 24 * 6
    assert held["max"] <= 5, held


# ------------------------------------------------------------------------------------------------ zombies (addendum)


def test_reaper_reports_zombies_once_and_never_kills_them(monkeypatch):
    """A process stuck in its exit (2026-10-24: 3 x ~0.9 GB for 4-6 h) is reported, not retried."""
    procs = [(4242, 999999, "python.exe")]
    monkeypatch.setattr(R.winsys, "process_cmdline", lambda p: "python -c from multiprocessing.spawn import spawn_main")
    monkeypatch.setattr(R.winsys, "process_image", lambda p: sys.executable)
    monkeypatch.setattr(R.winsys, "process_cwd", lambda p: str(BACKEND))
    monkeypatch.setattr(R.winsys, "process_create_time", lambda p: 1)
    monkeypatch.setattr(R.winsys, "tree_memory_mb", lambda p, procs=None: 900.0)
    monkeypatch.setattr(R.winsys, "process_memory_detail", lambda p: {"rss_mb": 280.0, "private_mb": 910.0})
    monkeypatch.setattr(R.winsys, "exiting", lambda p: True)
    monkeypatch.setattr(R, "ZOMBIE_MIN_S", 0.0)   # 2026-10-30 dwell (test_child_exit.py): stuck from the first sweep
    kills: list[int] = []
    monkeypatch.setattr(R.winsys, "kill_tree", lambda p, procs=None: kills.append(p) or [p])
    R._ZOMBIES_SEEN.clear()
    R._EXITING_SEEN.clear()
    pys = {R._norm(sys.executable)}
    for _ in range(3):
        rep = R.reap(project_root=BACKEND.parent, procs=procs, pythons=pys, min_age_s=0)
        assert rep["leaked"] == 0 and rep["killed"] == [] and [z["pid"] for z in rep["zombies"]] == [4242]
        assert rep["zombie_mb"] == 910
    assert kills == [] and len(R._ZOMBIES_SEEN) == 1
    monkeypatch.setattr(R.winsys, "exiting", lambda p: False)     # it finished on its own -> a normal orphan now
    rep = R.reap(project_root=BACKEND.parent, procs=procs, pythons=pys, min_age_s=0)
    assert rep["zombies"] == [] and kills == [4242] and not R._ZOMBIES_SEEN


def test_supervisor_restart_not_blocked_by_zombie(tmp_data, monkeypatch):
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("svc", [sys.executable, "-c", "pass"])
    c.old_pids = [4242]
    monkeypatch.setattr(SUP.winsys, "pid_alive", lambda p: True)
    monkeypatch.setattr(SUP.winsys, "exiting", lambda p: True)
    monkeypatch.setattr(SUP.winsys, "pid_running", lambda p: False)
    c.stopping_since = time.monotonic() - SUP.EXIT_TEARDOWN_S - 1   # 2026-10-30: in its exit past the teardown window
    assert sup._gone(c)                                  # stuck-in-exit: reported, restart proceeds
    monkeypatch.setattr(SUP.winsys, "pid_running", lambda p: True)
    monkeypatch.setattr(SUP.winsys, "exiting", lambda p: False)
    assert not sup._gone(c)                              # really still running: wait


def test_stop_file_children_get_no_ctrl_break(tmp_data, tmp_path, monkeypatch):
    sent: list[int] = []
    monkeypatch.setattr(SUP.os, "kill", lambda pid, sig: sent.append(pid))
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    stop = tmp_path / "svc.STOP"
    c = SUP.Child("svc", [sys.executable, "-c",
                          f"import pathlib,time\np=pathlib.Path(r'{stop}')\nwhile not p.exists(): time.sleep(0.05)\n"],
                  grace_s=30, stop_file=stop)
    sup.children = [c]
    sup.start_child(c)
    try:
        sup.stop_child(c)
        assert sent == [] and c.last_exit == 0 and c.state == "stopped"   # STOP file only, graceful exit
    finally:
        if c.job is not None:
            c.job.terminate()
            c.job.close()


def test_job_processes_start_in_their_own_process_group(monkeypatch):
    if not winsys.IS_WINDOWS:
        pytest.skip("Windows process groups")
    import _winapi
    import multiprocessing.popen_spawn_win32 as psw

    from app.scheduler import runner as RN

    seen: list[int] = []

    class FakeProc:
        def start(self):
            seen.append(psw._winapi.CreateProcess("x", "y", None, None, False, 0, None, None, None))

    monkeypatch.setattr(_winapi, "CreateProcess", lambda *a: a[5])
    orig = psw._winapi
    RN._start_in_own_group(FakeProc())
    assert seen == [RN._CREATE_NEW_PROCESS_GROUP] and psw._winapi is orig
