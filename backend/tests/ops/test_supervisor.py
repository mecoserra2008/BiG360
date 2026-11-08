"""Supervisor: starts children, restarts crashed ones with back-off, adopts external instances, stops via STOP file."""

from __future__ import annotations

import json
import sys
import threading
import time

from filelock import FileLock

from app.ops import supervisor as SUP
from app.ops import winsys


def _wait(cond, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_supervisor_restart_external_and_stop(tmp_data, tmp_path, monkeypatch):
    monkeypatch.setattr(SUP, "BACKOFF_S", (0.5, 0.5, 0.5, 0.5))
    sup = SUP.Supervisor(tmp_data, api=False, worker=False, filler=False)
    stop_flag = tmp_path / "svc.STOP"
    loop = ("import pathlib,time\n"
            f"p=pathlib.Path(r'{stop_flag}')\n"
            "while not p.exists(): time.sleep(0.1)\n")
    crash = "import sys,time; time.sleep(0.5); sys.exit(3)"
    lk_path = tmp_path / "ext.lock"
    sup.children = [
        SUP.Child("svc", [sys.executable, "-c", loop], grace_s=30, stop_file=stop_flag),
        SUP.Child("crashy", [sys.executable, "-c", crash], grace_s=5),
        SUP.Child("ext", [sys.executable, "-c", "import time; time.sleep(60)"], lock_path=lk_path),
    ]
    held = FileLock(str(lk_path), timeout=0)
    held.acquire()                                       # another instance "already runs"
    t = threading.Thread(target=sup.run, daemon=True)
    t.start()
    try:
        svc, crashy, ext = sup.children
        assert _wait(lambda: svc.pid is not None and winsys.pid_alive(svc.pid))
        assert (sup.logs / "svc.pid").read_text() == str(svc.pid)
        assert _wait(lambda: crashy.restarts >= 2), crashy.history   # crash -> back-off -> restart
        assert crashy.history[0]["exit"] == 3
        assert ext.state == "external" and ext.popen is None
        assert _wait(lambda: (sup.logs / "supervisor.json").exists())
        st = json.loads((sup.logs / "supervisor.json").read_text())
        assert st["children"]["svc"]["state"] == "running"
        held.release()                                   # the external instance went away -> taken over
        ext.next_try = 0
        assert _wait(lambda: ext.state == "running" and ext.pid is not None)
        (tmp_data.state_dir / "ops" / "supervisor.STOP").write_text("x")
        t.join(90)
        assert not t.is_alive()
        assert svc.popen is None and svc.last_exit == 0  # graceful: the child saw its STOP file
        assert not stop_flag.exists() and not (sup.logs / "supervisor.pid").exists()
    finally:
        sup.stop_event.set()
        for c in sup.children:
            if c.pid:
                winsys.kill_tree(c.pid)
        try:
            held.release()
        except Exception:
            pass
