"""Deterministic exit for the long-running supervisor children `realtime` and `quotes` (alert hygiene 2026-10-30).

Why: the realtime child (FinBERT / torch, an asyncio loop with a 24-thread default executor, a spawn
ProcessPoolExecutor for the GDELT parse) did not always finish its exit within the supervisor's 60 s grace
(2026-10-26 02:41 and 2026-10-29 23:07: "realtime supervisor stopped" was logged, the process was still alive 35 s
later and the supervisor killed its Job Object). The time went into the interpreter shutdown, not into torch:
  * asyncio.run() -> loop.shutdown_default_executor() joins every executor thread (up to 300 s), and then
    concurrent.futures.thread._python_exit joins them again without any limit. A thread still inside a cancelled
    asyncio.to_thread() call keeps the process alive: an HTTP request with retries while the network is down (both
    stops had getaddrinfo failures in the minutes before), a GDELT parse waiting up to 600 s on the parse child, a
    FinBERT batch;
  * concurrent.futures.process._python_exit joins the parse pool's manager thread, i.e. waits until the parse
    child has finished its current file (`shutdown(wait=False)` does not stop a running task).
Measured on this machine (scratchpad/alerts_fix/exit_probe.py, finbert_exit.py): a cancelled to_thread(25 s) kept
asyncio.run() from returning for 25 s; a running spawn-pool task kept the interpreter alive 22 s after main()
returned; loading FinBERT (2 threads) and scoring from a worker thread then exiting normally took 0.9 s, os._exit
0.2 s, so torch / OpenMP itself does not hang the exit.

What the children do now:
  1. a STOP-file watcher thread (ChildShutdown) sees the supervisor's stop request within 0.5 s;
  2. the service stops its own work: tasks get a short grace, the executor is shut down without waiting
     (shutdown_executor), the parse pool's processes are killed (kill_process_pool), stores are closed, status and
     logs are flushed;
  3. hard_exit(): logging.shutdown(), flush stdout / stderr, os._exit(code). No interpreter finalisation, no
     thread / pool joins, no atexit handlers - the work they would wait for is already abandoned on purpose;
  4. an exit deadline armed at the stop request hard-exits anyway (code 3) if step 2 itself blocks;
  5. the supervisor still terminates the child's Job Object after its grace period (the last resort, which always
     completes) and waits for the job's members to finish their teardown before it starts a new instance.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("app.ops.shutdown")

#: exit code of a deadline hard exit (cleanup itself did not finish in time)
DEADLINE_EXIT_CODE = 3


def busy_threads(exclude_daemon: bool = False) -> list[str]:
    """Names of the threads still alive besides the calling one."""
    me = threading.current_thread()
    return [t.name for t in threading.enumerate() if t is not me and t.is_alive() and not (exclude_daemon and t.daemon)]


def shutdown_executor(ex: Any, wait_s: float = 3.0) -> list[str]:
    """Shut a ThreadPoolExecutor down without waiting for running work (queued work is cancelled), then give its
    threads up to `wait_s` to finish. Returns the names of the threads still busy (left to the process exit)."""
    if ex is None:
        return []
    try:
        ex.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass
    threads = [t for t in list(getattr(ex, "_threads", ()) or ()) if t.is_alive()]
    end = time.monotonic() + max(0.0, wait_s)
    for t in threads:
        t.join(max(0.0, end - time.monotonic()))
    return [t.name for t in threads if t.is_alive()]


def kill_process_pool(pool: Any) -> int:
    """Stop a ProcessPoolExecutor NOW: cancel queued work, kill its worker processes (a running task is abandoned;
    the manager thread then sees a broken pool and ends, so nothing joins a busy child at exit). Returns the number
    of worker processes that were still alive."""
    if pool is None:
        return 0
    procs = list((getattr(pool, "_processes", None) or {}).values())
    try:
        pool.shutdown(wait=False, cancel_futures=True)
    except Exception:  # noqa: BLE001
        pass
    n = 0
    for p in procs:
        try:
            if p.is_alive():
                n += 1
                p.kill()
        except Exception:  # noqa: BLE001
            pass
    for p in procs:
        try:
            p.join(2.0)
        except Exception:  # noqa: BLE001
            pass
    return n


def flush_all() -> None:
    try:
        logging.shutdown()
    except Exception:  # noqa: BLE001
        pass
    for st in (sys.stdout, sys.stderr):
        try:
            if st is not None:
                st.flush()
        except Exception:  # noqa: BLE001
            pass


def hard_exit(code: int = 0, name: str = "child", reason: str = "cleanup done") -> None:
    """Flush the logs and stdio, then os._exit(code): the interpreter's own shutdown (thread / pool joins, atexit,
    module teardown of torch & co.) is skipped on purpose. Only call this after the service's own cleanup."""
    left = busy_threads()
    try:
        log.info("%s: exiting (%s, code %s)%s", name, reason, code,
                 f"; {len(left)} thread(s) abandoned: {', '.join(sorted(left)[:12])}" if left else "")
    except Exception:  # noqa: BLE001
        pass
    flush_all()
    os._exit(int(code))


def finish_child(code: int, name: str) -> int:
    """End of a supervisor child's CLI command: hard exit (default), or return the code when
    BIG360_CHILD_HARD_EXIT=0 (in-process use)."""
    if os.environ.get("BIG360_CHILD_HARD_EXIT", "1") == "0":
        return int(code)
    hard_exit(code, name)
    return int(code)  # pragma: no cover - os._exit never returns


class ChildShutdown:
    """STOP-file watcher + exit deadline of a supervisor child.

    start() launches a daemon thread that polls `stop_file` every `poll_s`; the first time it exists (or trigger()
    is called, e.g. from a signal handler) `on_stop()` runs once and, when `deadline_s` is set, a daemon timer is
    armed that hard-exits the process with DEADLINE_EXIT_CODE if it is still alive `deadline_s` later. The file is
    left in place (the service's own loop removes it)."""

    def __init__(self, name: str, stop_file: Path | None, on_stop: Callable[[], Any], deadline_s: float | None = None,
                 poll_s: float = 0.5):
        self.name = name
        self.stop_file = stop_file
        self.on_stop = on_stop
        self.deadline_s = deadline_s
        self.poll_s = poll_s
        self.triggered_at: float | None = None
        self.why: str | None = None
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> ChildShutdown:
        if self.stop_file is None or self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._watch, name=f"{self.name}-stop-watch", daemon=True)
        self._thread.start()
        return self

    def _watch(self) -> None:
        while not self._done.wait(self.poll_s):
            try:
                if self.stop_file is not None and self.stop_file.exists():
                    self.trigger("stop file")
                    return
            except OSError:
                continue

    def trigger(self, why: str = "requested") -> None:
        with self._lock:
            if self.triggered_at is not None:
                return
            self.triggered_at = time.monotonic()
            self.why = why
        log.info("%s: stop requested (%s)%s", self.name, why,
                 f"; hard exit in {self.deadline_s:.0f}s at the latest" if self.deadline_s else "")
        try:
            self.on_stop()
        except Exception:  # noqa: BLE001 - the deadline below still guarantees the exit
            log.exception("%s: on_stop failed", self.name)
        self.arm_deadline()

    def arm_deadline(self) -> None:
        if not self.deadline_s:
            return
        t = threading.Thread(target=self._deadline, name=f"{self.name}-exit-deadline", daemon=True)
        t.start()

    def _deadline(self) -> None:
        if self._done.wait(float(self.deadline_s or 0)):
            return
        try:
            log.error("%s: cleanup still running %.0fs after the stop request - hard exit (code %d); busy threads: %s",
                      self.name, self.deadline_s, DEADLINE_EXIT_CODE, ", ".join(sorted(busy_threads())[:15]))
        except Exception:  # noqa: BLE001
            pass
        flush_all()
        os._exit(DEADLINE_EXIT_CODE)

    def close(self) -> None:
        """Cleanup finished in time (or the service runs in-process): stop the watcher and disarm the deadline."""
        self._done.set()
