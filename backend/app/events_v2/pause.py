"""GDELT bandwidth priority: the nightly `gdelt_stream` job pauses the detached deep backfill.

`data/state/events_v2/PAUSE` (JSON: owner, pid, since). While it exists and its owner process is alive, the backfill
(app.events_v2.backfill) issues no new GDELT downloads (checked before every file and between days); in-flight files
finish and nothing already downloaded is lost. `gdelt_priority()` sets the flag and always clears it (try/finally).
A flag whose owner pid is dead, or older than MAX_AGE_H, is ignored (a crashed nightly can't freeze the backfill).
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings

MAX_AGE_H = 6.0


def pause_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "events_v2" / "PAUSE"


def _pid_alive(pid: int) -> bool:
    """Liveness check without signalling (os.kill(pid, 0) would TerminateProcess on Windows)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_pause(settings: Settings | None = None) -> dict[str, Any] | None:
    p = pause_path(settings)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        return json.loads(raw) or {}
    except ValueError:
        return {}


def pause_active(settings: Settings | None = None, max_age_h: float = MAX_AGE_H) -> bool:
    """True when a live, non-stale PAUSE flag exists (an empty/unparseable flag counts, subject to max age)."""
    p = pause_path(settings)
    info = read_pause(settings)
    if info is None:
        return False
    try:
        age_h = (time.time() - p.stat().st_mtime) / 3600
    except OSError:
        return False
    if age_h > max_age_h:
        return False
    pid = info.get("pid")
    return not (isinstance(pid, int) and not _pid_alive(pid))


def set_pause(settings: Settings | None = None, owner: str = "gdelt_stream") -> Path:
    p = pause_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"owner": owner, "pid": os.getpid(),
                               "since": datetime.now(UTC).isoformat(timespec="seconds")}), encoding="utf-8")
    tmp.replace(p)
    return p


def clear_pause(settings: Settings | None = None) -> None:
    """Remove the flag if this process owns it (or it is unowned / unreadable)."""
    info = read_pause(settings)
    if info is None:
        return
    if info.get("pid") not in (None, os.getpid()) and _pid_alive(int(info["pid"])):
        return
    try:
        pause_path(settings).unlink()
    except FileNotFoundError:
        pass


@contextmanager
def gdelt_priority(settings: Settings | None = None, owner: str = "gdelt_stream") -> Iterator[Path]:
    """Hold the PAUSE flag for the duration of the block (nightly stream gets the full GDELT bandwidth)."""
    p = set_pause(settings, owner)
    try:
        yield p
    finally:
        clear_pause(settings)
