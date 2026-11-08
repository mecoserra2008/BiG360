"""Profile-driven scheduling inside the worker process (O3.4 + F01 / R1).

The worker (app.scheduler.worker) owns one ProfileScheduler:

    ps = ProfileScheduler(runner, settings, registry, keep_awake=KEEP_AWAKE)
    ps.start()          # APScheduler jobs for the active profile's entries + a catch-up check
    ps.tick()           # from the worker loop: hot reload on profile change, periodic catch-up check, wake timer
    ps.heartbeat()      # fields merged into data/state/worker.json
    ps.shutdown()

* Entries of kind `dag` run runner.run_dag(dag, trigger=schedule); `chain` runs the steps in order
  (app.ops.schedule.run_entry). A per-entry guard prevents overlapping runs of one entry; the DAG file locks prevent
  overlaps across processes.
* Misfire grace per entry (profile misfire_grace_h) covers sleep while the process stays alive; the catch-up rule
  (app.ops.schedule.catchup_due) covers restarts / long sleeps: checked at start and every catchup_check_min.
* Keep-awake (SetThreadExecutionState) while an entry runs; a wake timer is armed 2 minutes before the next fire
  time so a sleeping PC wakes for the refresh (needs "Allow wake timers" in the power plan; harmless otherwise).
* data/state/ops/scheduler_seen.json records the last time the scheduler was seen running (for the
  "scheduler off > 1 day" alert).
* "Refresh now" (O3 tray / CLI): request_refresh() writes data/state/ops/refresh_now.json; the next tick() takes it
  and fires the active profile's primary refresh entry (weekly: the nightly -> weekly -> monthly_if_due chain) in a
  background thread, exactly like a scheduled run. The outcome is in the heartbeat (refresh_request).
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops import schedule as sched
from app.ops.paths import ops_state_dir, read_json, write_json_atomic

log = get_logger("app.ops.worker_schedule")

REFRESH_REQUEST = "refresh_now.json"


def refresh_request_path(settings: Settings | None = None):
    return ops_state_dir(settings) / REFRESH_REQUEST


def request_refresh(settings: Settings | None = None, entry_id: str | None = None, by: str = "tray") -> dict[str, Any]:
    """Ask the running worker to start a refresh now (entry_id None = the active profile's primary entry)."""
    req = {"entry": entry_id, "requested_at": datetime.now(UTC).isoformat(timespec="seconds"), "by": by}
    write_json_atomic(refresh_request_path(settings), req)
    return req


def primary_entry(entries: list[sched.Entry]) -> sched.Entry | None:
    """The profile's main refresh: the first chain, else the first entry running the nightly DAG, else the first."""
    return (next((e for e in entries if e.kind == "chain"), None)
            or next((e for e in entries if "nightly" in e.dags), None)
            or (entries[0] if entries else None))


class ProfileScheduler:
    def __init__(self, runner: Any, settings: Settings | None = None, registry: Any = None, keep_awake: Any = None,
                 wake_timer: Any = None, scheduler_factory: Any = None, completed_fn: Any = None):
        self.runner = runner
        self.s = settings or get_settings()
        self.registry = registry
        self.keep_awake = keep_awake
        self.wake = wake_timer
        self._factory = scheduler_factory
        self._completed_fn = completed_fn
        self.scheduler: Any = None
        self.profile: str | None = None
        self.profile_source: str | None = None
        self.entries: list[sched.Entry] = []
        self.running: set[str] = set()
        self._lock = threading.Lock()
        self._state_mtime: float | None = None
        self._env = os.environ.get("BIG360_SCHEDULE_PROFILE")
        self._last_catchup = 0.0
        self._last_seen_write = 0.0
        self.threads: list[threading.Thread] = []
        self.error: str | None = None
        self.last_refresh_request: dict[str, Any] | None = None

    # ------------------------------------------------------------------ helpers
    def completed(self) -> dict[str, dict[str, Any]]:
        if self._completed_fn is not None:
            return self._completed_fn()
        try:
            return sched.completed_dag_runs(self.runner.store.read())
        except Exception as e:  # pragma: no cover - bookkeeping must not kill scheduling
            log.warning("completed runs unavailable: %s", e)
            return {}

    def _state_file_mtime(self) -> float | None:
        p = self.s.state_dir / "schedule.json"
        try:
            return p.stat().st_mtime
        except OSError:
            return None

    def _make_scheduler(self) -> Any:
        if self._factory is not None:
            return self._factory()
        from apscheduler.schedulers.background import BackgroundScheduler

        return BackgroundScheduler(job_defaults={"coalesce": True, "max_instances": 1})

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.profile, self.profile_source = sched.active_profile(self.s)
        self.entries = sched.profile_entries(self.profile, self.registry, self.s)
        self._state_mtime = self._state_file_mtime()
        if self.scheduler is None:
            self.scheduler = self._make_scheduler()
            self.scheduler.start()
        for j in list(self.scheduler.get_jobs()):
            j.remove()
        for e in self.entries:
            self.scheduler.add_job(self.fire, e.trigger(), args=[e.id, "schedule"], id=f"entry_{e.id}", name=f"{e.kind} {e.id}",
                                   misfire_grace_time=e.misfire_grace_s, coalesce=True, max_instances=1)
        for j in self.scheduler.get_jobs():
            log.info("schedule profile %s: %s next at %s", self.profile, j.name, getattr(j, "next_run_time", None))
        self._last_catchup = 0.0
        self.error = None
        self.tick()

    def shutdown(self) -> None:
        if self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
            self.scheduler = None
        if self.wake is not None:
            try:
                self.wake.cancel()
            except Exception:
                pass

    def reload_if_changed(self) -> bool:
        env = os.environ.get("BIG360_SCHEDULE_PROFILE")
        mt = self._state_file_mtime()
        if mt == self._state_mtime and env == self._env:
            return False
        self._state_mtime, self._env = mt, env
        new, _src = sched.active_profile(self.s)
        if new == self.profile:
            return False
        log.info("schedule profile changed %s -> %s: rescheduling (hot reload)", self.profile, new)
        try:
            self.start()
        except Exception as e:  # keep the old schedule on a broken config
            self.error = f"{type(e).__name__}: {e}"
            log.exception("profile reload failed; keeping %s", self.profile)
            return False
        return True

    # ------------------------------------------------------------------ work
    def fire(self, entry_id: str, trigger: str = "schedule") -> dict[str, Any] | None:
        entry = next((e for e in self.entries if e.id == entry_id), None)
        if entry is None:
            return None
        with self._lock:
            if entry_id in self.running:
                log.info("schedule %s already running: skipped (%s)", entry_id, trigger)
                return None
            self.running.add(entry_id)
        try:
            log.info("schedule %s starting (%s)", entry_id, trigger)
            return sched.run_entry(entry, self.runner, trigger="schedule", completed_fn=self.completed, settings=self.s,
                                   keep_awake=self.keep_awake)
        finally:
            with self._lock:
                self.running.discard(entry_id)
                idle = not self.running
            if idle:  # ram_fix 2026-10-31: do not keep the high-water mark of the refresh in the idle worker
                try:
                    from app.ops.winsys import release_memory

                    r = release_memory(trim_working_set=True)
                    log.info("schedule %s done: worker memory ws %s -> %s MB, private %s -> %s MB", entry_id,
                             *(None if r.get(k) is None else round(r[k]) for k in
                               ("before_mb", "after_mb", "private_before_mb", "private_after_mb")))
                except Exception as e:  # pragma: no cover
                    log.debug("release_memory failed: %s", e)

    def catchup(self, now: datetime | None = None) -> list[str]:
        """Start missed entries in background threads (returns their ids)."""
        due = sched.catchup_due(self.entries, self.completed(), now, running=set(self.running))
        started = []
        for e, missed in due:
            log.warning("catch-up: %s missed its run at %s (PC asleep / off?) - running now", e.id, missed)
            th = threading.Thread(target=self.fire, args=(e.id, "catchup"), name=f"catchup-{e.id}", daemon=True)
            th.start()
            self.threads.append(th)
            started.append(e.id)
        return started

    def take_refresh_request(self) -> dict[str, Any] | None:
        """Consume data/state/ops/refresh_now.json (if any) and start the requested entry in a background thread."""
        p = refresh_request_path(self.s)
        if not p.exists():
            return None
        req = read_json(p, {}) or {}
        try:
            p.unlink()
        except FileNotFoundError:
            return None
        except OSError:
            return None  # locked (AV / writer): next tick
        want = req.get("entry")
        entry = next((e for e in self.entries if e.id == want), None) if want else primary_entry(self.entries)
        info = {**req, "handled_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "entry": entry.id if entry else want, "profile": self.profile}
        if entry is None:
            info["status"] = "no_entry"
        elif entry.id in self.running:
            info["status"] = "already_running"
        else:
            th = threading.Thread(target=self.fire, args=(entry.id, "manual"), name=f"refresh-{entry.id}", daemon=True)
            th.start()
            self.threads.append(th)
            info["status"] = "started"
        log.info("refresh request (%s): %s %s", req.get("by"), info["status"], info.get("entry"))
        self.last_refresh_request = info
        return info

    def tick(self, now: datetime | None = None) -> None:
        self.reload_if_changed()
        try:
            self.take_refresh_request()
        except Exception:
            log.exception("refresh request failed")
        cfg = sched.load_config(self.s)
        every = float(cfg.get("catchup_check_min", 15)) * 60
        mono = time.monotonic()
        if mono - self._last_catchup >= every or self._last_catchup == 0.0:
            self._last_catchup = mono
            try:
                self.catchup(now)
            except Exception:
                log.exception("catch-up check failed")
        if mono - self._last_seen_write >= 300 or self._last_seen_write == 0.0:
            self._last_seen_write = mono
            try:
                write_json_atomic(ops_state_dir(self.s) / "scheduler_seen.json",
                                  {"last_on_at": datetime.now(UTC).isoformat(timespec="seconds"), "profile": self.profile,
                                   "pid": os.getpid()})
            except OSError:
                pass
        self._arm_wake(now)

    def _arm_wake(self, now: datetime | None = None) -> None:
        if self.wake is None:
            return
        nxt = [t for t in (e.next_fire(now) for e in self.entries if e.wake) if t is not None]
        if not nxt:
            return
        due = min(nxt) - timedelta(minutes=2)
        if self.wake.due is None or abs((self.wake.due - due).total_seconds()) > 60:
            if self.wake.arm(due):
                log.info("wake timer armed for %s", due)

    # ------------------------------------------------------------------ status
    def next_runs(self) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        if self.scheduler is not None:
            for j in self.scheduler.get_jobs():
                t = getattr(j, "next_run_time", None)
                out[j.id.removeprefix("entry_")] = t.isoformat() if t else None
        return out

    def heartbeat(self) -> dict[str, Any]:
        return {"schedule_profile": self.profile, "schedule_profile_source": self.profile_source,
                "next_runs": self.next_runs(), "next_runs_by_dag": sched.next_runs_by_dag(self.entries),
                "schedule_running": sorted(self.running), "schedule_error": self.error,
                "wake_timer": self.wake.due.isoformat() if (self.wake is not None and self.wake.due) else None,
                "accepts_refresh_request": True, "refresh_request": self.last_refresh_request}
