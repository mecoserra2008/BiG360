"""Background-computed, single-flight cache for polled status payloads (coverage v3 A7, owner api).

GET /system/status was polled ~18k times in the 2026-11 audit window (p95 5.4 s under refresh load) and computed
everything inline on every poll: job_runs, dq_results, the job registry, the supervisor / sync / filler state files
and several snapshot aggregates. One transient error (contracts/jobs.yaml invalid while it was being edited) turned
442 polls into HTTP 500s. `BackgroundCache` takes all of that off the request path:

* a request is answered from the last computed payload at once (stale-while-revalidate); when that payload is older
  than `ttl_s`, or was computed on another serving snapshot (`tag`), ONE background thread recomputes it. Concurrent
  pollers never start a second computation (single flight, per key);
* only a cold cache (first request of the process, another data dir, or after `reset()`) waits for the computation,
  and at most `cold_wait_s`; past that the caller gets nothing and serves its own light fallback (state `warming`);
* a failed computation keeps the last good payload and records `last_error` (a poll never turns into an HTTP 500);
* a computation still running after `max_compute_s` no longer blocks a new one (a hung read cannot freeze the cache);
* `ttl_s <= 0` disables caching: every call computes inline (tests that mutate state between calls; the default test
  setting, see tests/conftest.py).

The payload is computed by the caller-supplied `compute()` (no arguments; it returns `(payload, tag)`), so this module
holds no endpoint logic.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logsetup import get_logger

log = get_logger("app.api.status_cache")


@dataclass(frozen=True)
class Entry:
    key: str
    value: Any
    tag: str | None
    computed_at: datetime
    computed_mono: float
    compute_ms: float


@dataclass
class _Run:
    started_mono: float
    done: threading.Event


class BackgroundCache:
    def __init__(self, name: str, compute: Callable[[], tuple[Any, str | None]], *, ttl_s: float = 10.0,
                 cold_wait_s: float = 10.0, max_compute_s: float = 120.0):
        self.name = name
        self._compute = compute
        self.ttl_s = float(ttl_s)
        self.cold_wait_s = float(cold_wait_s)
        self.max_compute_s = float(max_compute_s)
        self._lock = threading.Lock()
        self._entry: Entry | None = None
        self._runs: dict[str, _Run] = {}
        self._errors: dict[str, tuple[datetime, str]] = {}
        self._gen = 0           # bumped by reset(): a computation started before it never stores its result
        self.computations = 0   # completed compute() calls (tests / metrics)

    # ------------------------------------------------------------------------------------------------ public

    def get(self, key: str, tag: str | None = None) -> tuple[Entry | None, dict[str, Any]]:
        """(entry or None, cache meta). Never computes on the caller's thread unless ttl_s <= 0."""
        if self.ttl_s <= 0:
            return self._inline(key)
        now = time.monotonic()
        with self._lock:
            e = self._entry if self._entry is not None and self._entry.key == key else None
            run = self._runs.get(key)
            if run is not None and now - run.started_mono > self.max_compute_s:
                log.warning("%s: computation running for %.0f s - allowing a new one", self.name, now - run.started_mono)
                run = None
            due = e is None or now - e.computed_mono >= self.ttl_s or (tag is not None and e.tag != tag)
            start = due and run is None
            if start:
                run = _Run(now, threading.Event())
                self._runs[key] = run
        if start:
            threading.Thread(target=self._refresh, args=(key, run), name=f"{self.name}-refresh", daemon=True).start()
        if e is None and run is not None:
            run.done.wait(self.cold_wait_s)   # cold cache only: bounded wait for the first payload
            with self._lock:
                e = self._entry if self._entry is not None and self._entry.key == key else None
        return e, self._meta(key, e, tag)

    def peek(self, key: str) -> Entry | None:
        with self._lock:
            return self._entry if self._entry is not None and self._entry.key == key else None

    def reset(self) -> None:
        """Forget the cached payload and errors (tests; a running computation finishes but is not awaited)."""
        with self._lock:
            self._entry = None
            self._errors.clear()
            self._runs.clear()
            self._gen += 1

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Tests: wait until no computation is running."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                runs = list(self._runs.values())
            if not runs:
                return True
            runs[0].done.wait(max(0.0, end - time.monotonic()))
        return False

    # ------------------------------------------------------------------------------------------------ internals

    def _inline(self, key: str) -> tuple[Entry | None, dict[str, Any]]:
        self._refresh(key, None)
        e = self.peek(key)
        return e, self._meta(key, e, e.tag if e else None)

    def _refresh(self, key: str, run: _Run | None) -> None:
        t0 = time.monotonic()
        with self._lock:
            gen = self._gen
        try:
            value, tag = self._compute()
            entry = Entry(key, value, tag, datetime.now(UTC), time.monotonic(), round((time.monotonic() - t0) * 1000, 1))
            with self._lock:
                self.computations += 1
                if gen == self._gen:
                    self._entry = entry
                    self._errors.pop(key, None)
        except Exception as e:  # noqa: BLE001 - keep serving the last good payload
            msg = f"{type(e).__name__}: {e}"[:500]
            log.warning("%s: computation failed after %.1f s (serving the previous payload): %s", self.name,
                        time.monotonic() - t0, msg)
            with self._lock:
                if gen == self._gen:
                    self._errors[key] = (datetime.now(UTC), msg)
        finally:
            if run is not None:
                with self._lock:
                    if self._runs.get(key) is run:
                        del self._runs[key]
                run.done.set()

    def _meta(self, key: str, e: Entry | None, tag: str | None) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            refreshing = key in self._runs
            err = self._errors.get(key)
        age = None if e is None else round(now - e.computed_mono, 3)
        if e is None:
            state = "error" if err and not refreshing else "warming"
        elif age is not None and age < self.ttl_s and (tag is None or e.tag == tag):
            state = "fresh"
        elif self.ttl_s <= 0:
            state = "fresh"
        else:
            state = "stale"
        return {"state": state, "computed_at": e.computed_at.isoformat() if e else None, "age_s": age,
                "ttl_s": self.ttl_s, "refreshing": refreshing, "compute_ms": e.compute_ms if e else None,
                "snapshot_run_id": e.tag if e else None,
                "last_error": err[1] if err else None, "last_error_at": err[0].isoformat() if err else None}
