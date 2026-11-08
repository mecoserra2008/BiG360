"""Process helpers for the incident 2026-10-24 tests (importable by multiprocessing spawn children)."""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from pathlib import Path


def sleeper(ready: str, secs: float = 300.0) -> None:
    """Spawn target: say "running" (the parent may die after that: the pipe handle is already ours), then sleep."""
    Path(ready).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(secs)


def _wait_file(path: str | Path, timeout_s: float = 90.0) -> None:
    end = time.time() + timeout_s
    while time.time() < end and not Path(path).exists():
        time.sleep(0.05)


def spawn_grandchild_and_exit(pidfile: str, sleep_s: float = 300.0) -> None:
    """Child: start a spawn grandchild, record both pids, then die WITHOUT joining it (the grandchild is orphaned:
    its parent is dead, exactly like the leaked job processes of 2026-10-24)."""
    ready = pidfile + ".g_ready"
    g = mp.get_context("spawn").Process(target=sleeper, args=(ready, sleep_s), daemon=False)
    g.start()
    _wait_file(ready)
    tmp = Path(pidfile + ".tmp")
    tmp.write_text(f"{os.getpid()} {g.pid}", encoding="utf-8")
    os.replace(tmp, pidfile)
    os._exit(0)


def wait_pidfile(path: Path, timeout_s: float = 90.0) -> list[int]:
    end = time.time() + timeout_s
    while time.time() < end:
        if path.exists():
            txt = path.read_text(encoding="utf-8").split()
            if txt:
                return [int(x) for x in txt]
        time.sleep(0.1)
    raise TimeoutError(f"no pid file {path}")


LAUNCHER = """\
import multiprocessing as mp, os, sys, time
sys.path.insert(0, {backend!r})
from tests.ops import proc_helpers as H
if __name__ == "__main__":
    mode, pidfile = {mode!r}, {pidfile!r}
    if mode == "orphan_grandchild":
        c = mp.get_context("spawn").Process(target=H.spawn_grandchild_and_exit, args=(pidfile,))
        c.start()
        c.join(120)
        time.sleep(300)
    else:                                  # orphan_child: exit once the child runs | live_parent: stay alive
        c = mp.get_context("spawn").Process(target=H.sleeper, args=(pidfile + ".ready", 300), daemon=False)
        c.start()
        H._wait_file(pidfile + ".ready")
        tmp = pidfile + ".tmp"
        open(tmp, "w").write(str(c.pid))
        os.replace(tmp, pidfile)
        if mode == "orphan_child":
            os._exit(0)
        time.sleep(300)
"""


def write_launcher(tmp_dir: Path, name: str, mode: str, pidfile: Path, backend: Path) -> Path:
    p = tmp_dir / f"{name}.py"
    p.write_text(LAUNCHER.format(backend=str(backend), mode=mode, pidfile=str(pidfile)), encoding="utf-8")
    return p
