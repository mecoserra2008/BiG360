"""Alert hygiene 2026-10-30, alert 1: the realtime (and quotes) child must leave deterministically.

* a fake child that hangs in its interpreter shutdown like the realtime child did (a non-daemon worker thread that
  never finishes and an atexit join on it, the way torch / OpenMP pools or a busy executor thread keep an
  interpreter alive) is gone right after the supervisor's grace period: its Job Object is terminated, no CTRL+BREAK;
* a fake realtime child built on the real app.realtime.supervisor.Supervisor with the same hazards (an executor
  thread blocked in a cancelled to_thread call, a spawn process pool running a task, a torch-like non-daemon thread,
  an atexit hang) exits BY ITSELF within the grace (exit code 0, not killed) through app.ops.shutdown;
* realtime and quotes are stopped through their STOP files only;
* a process caught in its normal exit teardown is not a zombie (reaper dwell), and the supervisor waits for the
  teardown before it starts the next instance.

Every test only touches processes it started itself."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import app
from app.ops import reaper as R
from app.ops import shutdown as SD
from app.ops import supervisor as SUP
from app.ops import winsys

BACKEND = Path(app.__file__).resolve().parents[1]
NOWIN = pytest.mark.skipif(not winsys.IS_WINDOWS, reason="Windows Job Objects")


def _wait(cond, timeout=60.0, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return False


#: torch-like: main sees the STOP file and returns, then the interpreter shutdown joins a worker that never ends
HANG_AT_EXIT = r"""
import atexit, pathlib, sys, threading, time
stop, ready = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
pool = threading.Thread(target=time.sleep, args=(600,), name="omp-like-worker")   # non-daemon
pool.start()
atexit.register(pool.join)
ready.write_text("ok")
while not stop.exists():
    time.sleep(0.05)
print("main returned; the interpreter shutdown now hangs", flush=True)
"""

#: the real realtime Supervisor with the hazards of the 2026-10-26 / 10-29 stops, ended like `realtime start`
FAKE_REALTIME = r"""
import asyncio, atexit, multiprocessing as mp, sys, threading, time
sys.path.insert(0, {backend!r})
from concurrent.futures import ProcessPoolExecutor


def long_parse(sec):
    time.sleep(sec)
    return 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(name)s | %(message)s")
    from app.core.config import get_settings
    from app.ops.shutdown import finish_child, kill_process_pool
    from app.realtime.streams.base import StreamStats
    from app.realtime.supervisor import Supervisor

    class HungStream:
        # like the GDELT stream during a network outage: a poll blocked in the default executor (cancelling the
        # task does not stop the thread) waiting on a parse child that is busy
        name = "hung"

        def __init__(self):
            self.stats = StreamStats("hung", "stream")
            self.pool = ProcessPoolExecutor(1, mp_context=mp.get_context("spawn"))

        async def run(self, ctx):
            fut = self.pool.submit(long_parse, 600)
            # the parse waiter is released when the pool is killed; the "HTTP retry loop" (sleep) never is
            await asyncio.gather(ctx.run_sync(fut.result, 600), ctx.run_sync(time.sleep, 600))

        def kill_children(self):
            pool, self.pool = self.pool, None
            return kill_process_pool(pool) if pool is not None else 0

        def close(self):
            self.kill_children()

    threading.Thread(target=time.sleep, args=(600,), name="omp-like-worker").start()   # torch-like native pool
    atexit.register(time.sleep, 600)                                                    # torch-like teardown
    s = get_settings()
    s.ensure_dirs()
    sup = Supervisor(s, workers=[HungStream()], grace_s=1.0, drain_s=2.0, status_every_s=0.2,
                     exit_deadline_s={deadline!r})
    rc = sup.run()
    finish_child(rc, "fake-realtime")
"""


@pytest.fixture()
def no_ctrl_break(monkeypatch):
    sent: list[int] = []
    monkeypatch.setattr(SUP.os, "kill", lambda pid, sig: sent.append(pid))
    return sent


@NOWIN
def test_torch_like_exit_hang_is_gone_right_after_the_grace(tmp_data, tmp_path, no_ctrl_break):
    stop, ready = tmp_path / "rt.STOP", tmp_path / "ready"
    script = tmp_path / "hang_at_exit.py"
    script.write_text(HANG_AT_EXIT, encoding="utf-8")
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("realtime", [sys.executable, str(script), str(stop), str(ready)], grace_s=3.0, stop_file=stop)
    sup.children = [c]
    sup.start_child(c)
    try:
        assert _wait(ready.exists, 60), "fake child did not start"
        pids = c.job.pids()
        assert pids
        t0 = time.monotonic()
        sup.stop_child(c)
        took = time.monotonic() - t0
        assert no_ctrl_break == []                                       # STOP file only (addendum rule)
        assert took < c.grace_s + 10, took                              # job terminated right after the grace
        assert c.last_exit not in (None, 0)                              # it did not exit by itself: it was killed
        assert _wait(lambda: not any(winsys.pid_alive(p) for p in pids), 30)
        assert _wait(lambda: (sup.check_child(c), c.state)[1] == "stopped", 120)
        assert c.history[0]["exit"] == "stop"
    finally:
        if c.job is not None:
            c.job.terminate()
            c.job.close()


@NOWIN
def test_realtime_child_exits_by_itself_within_the_grace(tmp_data, tmp_path, no_ctrl_break):
    from app.realtime.supervisor import status_path, stop_file

    script = tmp_path / "fake_realtime.py"
    script.write_text(FAKE_REALTIME.format(backend=str(BACKEND), deadline=20.0), encoding="utf-8")
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("realtime", [sys.executable, str(script)], grace_s=30.0, stop_file=stop_file(tmp_data))
    sup.children = [c]
    sup.start_child(c)
    try:
        assert _wait(lambda: status_path(tmp_data).exists(), 90), "fake realtime child did not start"
        assert _wait(lambda: len(c.job.pids()) >= 2, 60), "the parse pool child did not start"  # launcher/child/pool
        pids = c.job.pids()
        time.sleep(1.0)                                                  # the hung poll is in flight
        t0 = time.monotonic()
        sup.stop_child(c)
        took = time.monotonic() - t0
        assert no_ctrl_break == []
        assert c.last_exit == 0, f"killed after the grace instead of a clean exit (code {c.last_exit})"
        assert took < c.grace_s, took
        assert _wait(lambda: not any(winsys.pid_alive(p) for p in pids), 30), "a member outlived the stop"
        text = (SUP.app_logs_dir(tmp_data) / "realtime.out.log").read_text(encoding="utf-8", errors="replace")
        assert "realtime supervisor stopped" in text                      # its own cleanup ran ...
        assert "executor thread(s) still busy" in text                    # ... the hung poll was abandoned ...
        assert "fake-realtime: exiting (cleanup done, code 0)" in text    # ... and it hard-exited
    finally:
        if c.job is not None:
            c.job.terminate()
            c.job.close()


@NOWIN
def test_realtime_and_quotes_children_stop_by_file_only(tmp_data, tmp_path, no_ctrl_break):
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False, live_news=True, live_quotes=True)
    kids = {c.name: c for c in sup.children}
    assert set(kids) == {"realtime", "quotes"}
    assert kids["realtime"].stop_file == tmp_data.state_dir / "realtime.STOP"
    assert kids["quotes"].stop_file == tmp_data.state_dir / "quotes.STOP"
    for c in kids.values():
        c.command = [sys.executable, "-c", f"import pathlib,time\np=pathlib.Path(r'{c.stop_file}')\n"
                                           "while not p.exists(): time.sleep(0.05)\n"]
        c.grace_s = 30
        sup.start_child(c)
    try:
        for c in kids.values():
            sup.stop_child(c)
            assert c.last_exit == 0 and c.state == "stopped", (c.name, c.last_exit, c.state)
        assert no_ctrl_break == []
    finally:
        for c in kids.values():
            if c.job is not None:
                c.job.terminate()
                c.job.close()


# ------------------------------------------------------------------------------------------------ helpers


def _pool_sleep(sec: float) -> int:
    time.sleep(sec)
    return 1


def test_kill_process_pool_and_shutdown_executor_never_block():
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

    pool = ProcessPoolExecutor(1, mp_context=mp.get_context("spawn"))
    fut = pool.submit(_pool_sleep, 600)
    assert _wait(lambda: any(p.is_alive() for p in pool._processes.values()), 60)
    procs = list(pool._processes.values())
    t0 = time.monotonic()
    assert SD.kill_process_pool(pool) == 1
    assert time.monotonic() - t0 < 10 and not any(p.is_alive() for p in procs)
    assert fut.done() or _wait(fut.done, 10)                    # the waiter gets BrokenProcessPool, not a hang
    ex = ThreadPoolExecutor(2, thread_name_prefix="rtx")
    ev = threading.Event()
    ex.submit(ev.wait, 600)
    t0 = time.monotonic()
    left = SD.shutdown_executor(ex, wait_s=0.5)
    assert time.monotonic() - t0 < 3 and left and left[0].startswith("rtx")
    ev.set()


def test_child_shutdown_watcher_and_deadline(tmp_path):
    stop = tmp_path / "x.STOP"
    calls: list[str] = []
    ctl = SD.ChildShutdown("t", stop, lambda: calls.append("stop"), deadline_s=None, poll_s=0.05).start()
    try:
        stop.write_text("supervisor")
        assert _wait(lambda: calls == ["stop"], 5)
        ctl.trigger("again")
        assert calls == ["stop"] and ctl.why == "stop file"      # once only
    finally:
        ctl.close()
    # the deadline hard-exits a process whose cleanup blocks (code 3), whatever its threads / atexit do
    code = ("import sys, time, threading, atexit\n"
            f"sys.path.insert(0, {str(BACKEND)!r})\n"
            "from app.ops.shutdown import ChildShutdown\n"
            "threading.Thread(target=time.sleep, args=(600,)).start()\n"
            "atexit.register(time.sleep, 600)\n"
            "ChildShutdown('t', None, lambda: None, deadline_s=1.0).trigger('test')\n"
            "time.sleep(600)\n")
    t0 = time.monotonic()
    p = subprocess.run([sys.executable, "-c", code], timeout=60, capture_output=True)
    assert p.returncode == SD.DEADLINE_EXIT_CODE and time.monotonic() - t0 < 30


# ------------------------------------------------------------------------------------------------ teardown vs zombie


def test_reaper_exit_teardown_is_not_a_zombie_until_it_lasts(monkeypatch):
    """07:27:52 UTC: a sync job process caught 0.2 s after its job ended (2.8 GB being freed) was reported as an
    unkillable zombie. Now: first sighting = `exiting` (no alert); still in its exit ZOMBIE_MIN_S later = zombie."""
    procs = [(5151, 777, "python.exe")]
    monkeypatch.setattr(R.winsys, "process_image", lambda p: sys.executable)
    monkeypatch.setattr(R.winsys, "process_cmdline", lambda p: "python -m app.scheduler.jobproc confluence_zones")
    monkeypatch.setattr(R.winsys, "process_create_time", lambda p: 7)
    monkeypatch.setattr(R.winsys, "process_memory_detail", lambda p: {"rss_mb": 1730.0, "private_mb": 2814.0})
    monkeypatch.setattr(R.winsys, "exiting", lambda p: True)
    R._ZOMBIES_SEEN.clear()
    R._EXITING_SEEN.clear()
    pys = {R._norm(sys.executable)}
    clock = [1000.0]
    monkeypatch.setattr(R, "_mono", lambda: clock[0])
    rep = R.reap(project_root=BACKEND.parent, procs=procs, pythons=pys, min_age_s=0)
    assert rep["zombies"] == [] and rep["zombie_mb"] == 0 and [e["pid"] for e in rep["exiting"]] == [5151]
    clock[0] += R.ZOMBIE_MIN_S + 1                                  # next sweep: still in its exit
    rep = R.reap(project_root=BACKEND.parent, procs=procs, pythons=pys, min_age_s=0)
    assert [z["pid"] for z in rep["zombies"]] == [5151] and rep["zombie_mb"] == 2814
    assert rep["zombies"][0]["exiting_s"] >= R.ZOMBIE_MIN_S and "confluence_zones" in rep["zombies"][0]["cmd"]
    monkeypatch.setattr(R.winsys, "exiting", lambda p: False)       # gone
    rep = R.reap(project_root=BACKEND.parent, procs=[], pythons=pys, min_age_s=0)
    assert rep["zombies"] == [] and not R._ZOMBIES_SEEN and not R._EXITING_SEEN


def test_supervisor_waits_for_exit_teardown_before_restart(tmp_data, monkeypatch):
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    c = SUP.Child("realtime", [sys.executable, "-c", "pass"])
    c.old_pids = [4242]
    monkeypatch.setattr(SUP.winsys, "pid_alive", lambda p: True)
    monkeypatch.setattr(SUP.winsys, "pid_running", lambda p: False)
    monkeypatch.setattr(SUP.winsys, "exiting", lambda p: True)
    c.stopping_since = time.monotonic()
    assert not sup._gone(c)                                    # tearing down: the next instance waits
    c.stopping_since = time.monotonic() - SUP.EXIT_TEARDOWN_S - 1
    assert sup._gone(c)                                        # stuck in its exit for too long: zombie, go on


def test_zombie_alert_names_the_owner():
    from app.ops.health import supervisor_alerts

    sup = {"alive": True, "children": {}, "reaper": {
        "zombies": [{"pid": 30740, "ppid": 63680, "private_mb": 2814, "first_seen": "2026-10-30T07:27:52+00:00",
                     "owner": "sync", "exiting_s": 400}],
        "zombie_mb": 2814, "exiting": [{"pid": 1, "owner": "realtime", "exiting_s": 3}]}}
    al = {a["id"]: a for a in supervisor_alerts(sup)}
    assert "sync" in al["zombie_processes"]["detail"] and "30740" in al["zombie_processes"]["detail"]
    sup["reaper"]["zombies"] = []
    assert "zombie_processes" not in {a["id"] for a in supervisor_alerts(sup)}   # teardown only: no alert
