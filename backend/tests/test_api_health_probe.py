"""Perf pass: /health stays constant-time (no wait on the snapshot-switch lock, dq flag computed off-thread)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from app.api import deps


def test_peek_does_not_wait_for_the_switch_lock(monkeypatch, tmp_data):
    m = deps._SnapshotManager()
    old = SimpleNamespace(run_id="old")
    m._snap, m._key, m._checked = old, (str(tmp_data.data_dir), "serving_old.duckdb"), 0.0
    monkeypatch.setattr(deps, "current_serving_db", lambda s: Path("serving_new.duckdb"))
    m._lock.acquire()  # another thread is opening the new snapshot
    try:
        t = time.monotonic()
        assert m.get(wait=False) is old
        assert time.monotonic() - t < 0.5
    finally:
        m._lock.release()


def test_dq_flag_is_refreshed_off_thread(monkeypatch):
    import app.core.dq as dq

    gate = threading.Event()
    calls: list[int] = []

    def slow_summary(*a, **k):  # noqa: ANN002, ANN003
        calls.append(1)
        gate.wait(5)
        return {"blocking": True}

    monkeypatch.setattr(dq, "dq_summary", slow_summary)
    monkeypatch.setattr(deps, "_dq_state", {"at": float("-inf"), "blocking": False, "running": False})
    t = time.monotonic()
    assert deps.dq_blocking_cached() is False          # last known value, immediately
    assert deps.dq_blocking_cached() is False          # no second refresh while one is running
    assert time.monotonic() - t < 0.5
    gate.set()
    deadline = time.monotonic() + 5
    while deps._dq_state["running"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert deps.dq_blocking_cached() is True and calls == [1]
