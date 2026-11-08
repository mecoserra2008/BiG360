"""Hard-stop fallback for thread-executor jobs (F18 / R3).

Python cannot kill a thread, but it can make one raise: PyThreadState_SetAsyncExc schedules an exception that is
raised the next time the thread executes Python bytecode (between two C calls). The runner first sets the job's
cooperative cancel_event (JobContext.check_cancelled); if the thread is still alive after a grace period it injects
JobKilled (a BaseException) with async_raise(); a thread blocked inside one long C call (e.g. a socket read without timeout) only
dies when that call returns, so the runner waits a little longer and reports a zombie if it is still alive.
"""

from __future__ import annotations

import ctypes
import threading


class JobKilled(BaseException):
    """Injected into a timed-out job thread. A BaseException, so `except Exception` blocks in jobs cannot swallow it."""


def async_raise(thread: threading.Thread, exc_type: type[BaseException]) -> bool:
    """Schedule `exc_type` in `thread`. True when exactly one thread state was modified."""
    tid = thread.ident
    if tid is None or not thread.is_alive():
        return False
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), ctypes.py_object(exc_type))
    if n > 1:  # pragma: no cover - undo: affected more than one thread state
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid), None)
        return False
    return n == 1


def stop_thread(thread: threading.Thread, exc_type: type[BaseException], wait_s: float = 15.0, poll_s: float = 0.2) -> bool:
    """Inject `exc_type` (repeatedly: a broad `except` in the job may swallow one) until the thread exits or wait_s
    passes. True when the thread is gone."""
    import time

    deadline = time.monotonic() + wait_s
    while thread.is_alive() and time.monotonic() < deadline:
        async_raise(thread, exc_type)
        thread.join(poll_s)
    return not thread.is_alive()
