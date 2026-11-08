"""Worker process: APScheduler crons from contracts/jobs.yaml + consumer of the file job queue (owner: platform).

Run (from backend/):  ..\\.venv\\Scripts\\python.exe -m app.scheduler.worker          (or: python -m app.cli worker start)
Options:              --once           process the queue once and exit (no scheduler)
                      --no-schedule    consume the queue only (no cron DAG runs)
                      --realtime       also run the realtime supervisor (app.realtime.supervisor, R1) in a thread of
                                       this process; default OFF (or env BIG360_REALTIME=1)

* Schedule (O3.4): the ACTIVE schedule profile (config/schedule.yaml; switch with PUT /api/v1/system/schedule or env
  BIG360_SCHEDULE_PROFILE) is mapped to APScheduler triggers by app.ops.worker_schedule.ProfileScheduler: `weekly`
  (default) = Saturday 02:00 Lisbon chain nightly -> weekly -> monthly-if-due + CFTC Friday 19:00 ET; `daily` = the
  jobs.yaml crons (interpreted in Settings.scheduler_timezone, default Europe/Lisbon). Profile changes are hot-reloaded.
  Catch-up: entries whose last scheduled time was missed (PC asleep / off) run at start and on the periodic check;
  misfire grace per profile. The PC is kept awake while a refresh runs and a wake timer is armed before the next one.
* --no-schedule (queue only) is logged as a warning and shown on the System page / header ("Scheduler off").
* Queue: data/state/queue/*.json written by the API / `cli jobs enqueue` (app.core.jobqueue), polled every
  Settings.worker_poll_s (5 s). Requests run concurrently (max 2) on top of the per-run job pool.
* Single instance: data/state/worker.lock (OS-level file lock, released automatically if the process dies).
* Heartbeat: data/state/worker.json {pid, host, started_at, heartbeat_at, scheduler_tz, next_runs, active} for the
  System page.
* Graceful shutdown: Ctrl+C / Ctrl+Break / SIGTERM -> stop accepting work, signal running jobs (ctx.cancel_event),
  wait up to `shutdown_grace_s` for in-flight requests and DAG runs, then exit.
* Start-up hygiene: removes stale .tmp-/.old- partition dirs, requeues requests left in processing/ by a crash, marks
  orphaned `running` job_runs rows as failed, prunes old done/failed queue files.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from app import __version__
from app.core import jobqueue
from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger, setup_logging
from app.core.storage import cleanup_leftovers
from app.scheduler.runner import JobRegistry, JobRunner, JobRunStore, RunOptions, scheduler_tz

log = get_logger("app.scheduler.worker")


class WorkerAlreadyRunning(RuntimeError):
    pass


def worker_lock(settings: Settings | None = None) -> FileLock:
    s = settings or get_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(s.state_dir / "worker.lock"), timeout=0)


def worker_is_running(settings: Settings | None = None) -> bool:
    lk = worker_lock(settings)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True
    lk.release()
    return False


def read_heartbeat(settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    p = s.state_dir / "worker.json"
    try:
        hb = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hb["alive"] = worker_is_running(s)
    return hb


def _parse_params(params: dict[str, Any]) -> tuple[dict[str, Any], date | None, bool | None]:
    p = dict(params or {})
    as_of = p.pop("as_of", None)
    force = p.pop("force", None)
    return p, (date.fromisoformat(as_of) if isinstance(as_of, str) and as_of else None), force


class Worker:
    def __init__(self, settings: Settings | None = None, registry: JobRegistry | None = None,
                 enable_schedule: bool = True, poll_s: float | None = None, max_concurrent_requests: int = 2,
                 shutdown_grace_s: float = 300.0):
        self.settings = settings or get_settings()
        self.registry = registry or JobRegistry.load(settings=self.settings)
        self.stop_event = threading.Event()
        self.runner = JobRunner(self.registry, self.settings, stop_event=self.stop_event)
        self.enable_schedule = enable_schedule
        self.poll_s = poll_s if poll_s is not None else self.settings.worker_poll_s
        self.pool = ThreadPoolExecutor(max_workers=max_concurrent_requests, thread_name_prefix="queue")
        self.max_concurrent = max_concurrent_requests
        self.shutdown_grace_s = shutdown_grace_s
        self.active: dict[str, dict[str, Any]] = {}
        self._active_lock = threading.Lock()
        self._futures: list[Future] = []
        self.scheduler: Any = None
        self.profile_sched: Any = None
        self.no_schedule_reason: str | None = None if enable_schedule else "worker started with --no-schedule"
        self._lock = worker_lock(self.settings)
        self.started_at = datetime.now(UTC)
        self.tz = scheduler_tz(self.settings, self.registry)

    # ------------------------------------------------------------------ lifecycle
    def acquire_lock(self) -> None:
        try:
            self._lock.acquire(timeout=0)
        except Timeout as e:
            raise WorkerAlreadyRunning(f"another worker holds {self._lock.lock_file}") from e

    def release_lock(self) -> None:
        try:
            self._lock.release(force=True)
        except Exception:
            pass

    def startup_hygiene(self) -> None:
        n_tmp = cleanup_leftovers(self.settings)
        rec = jobqueue.recover_processing(settings=self.settings)
        n_orph = JobRunStore(self.settings).mark_orphans()
        n_pruned = jobqueue.prune(settings=self.settings)
        log.info("startup: removed %d stale tmp dirs, queue %s, %d orphaned job_runs, pruned %d queue files",
                 n_tmp, rec, n_orph, n_pruned)

    def start_scheduler(self) -> None:
        """Profile-driven schedule (app.ops.worker_schedule): triggers of the active profile + catch-up check."""
        from app.ops.winsys import KEEP_AWAKE, WakeTimer
        from app.ops.worker_schedule import ProfileScheduler

        ps = ProfileScheduler(self.runner, self.settings, self.registry, keep_awake=KEEP_AWAKE, wake_timer=WakeTimer())
        ps.start()
        self.profile_sched = ps
        self.scheduler = ps.scheduler

    def next_runs(self) -> dict[str, str | None]:
        if self.profile_sched is None:
            return {}
        return self.profile_sched.next_runs()

    def heartbeat(self) -> None:
        with self._active_lock:
            active = list(self.active.values())
        hb = {
            "pid": os.getpid(), "host": socket.gethostname(), "version": __version__,
            "started_at": self.started_at.isoformat(), "heartbeat_at": datetime.now(UTC).isoformat(),
            "scheduler_enabled": self.scheduler is not None, "scheduler_tz": str(self.tz), "next_runs": self.next_runs(),
            "active": active, "poll_s": self.poll_s, "scheduler_disabled_reason": self.no_schedule_reason,
            "pressure": getattr(self, "pressure", None),
        }
        if self.profile_sched is not None:
            try:
                hb.update(self.profile_sched.heartbeat())
            except Exception as e:  # pragma: no cover
                hb["schedule_error"] = str(e)
        p = self.settings.state_dir / "worker.json"
        tmp = p.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(hb, indent=1, default=str), encoding="utf-8")
            jobqueue._retry_fs(os.replace, tmp, p)
        except OSError as e:  # pragma: no cover
            log.debug("heartbeat write failed: %s", e)

    # ------------------------------------------------------------------ work
    def _track(self, key: str, info: dict[str, Any] | None) -> None:
        with self._active_lock:
            if info is None:
                self.active.pop(key, None)
            else:
                self.active[key] = info

    def _release_if_idle(self, what: str) -> None:
        """ram_fix 2026-10-31: once no run is in flight, give the memory thread jobs freed back to the OS (gc + heap
        compaction) and trim the idle worker's working set (EmptyWorkingSet), so a long-lived worker does not keep
        the high-water mark of the last DAG. Heavy jobs run in their own processes and return everything at exit."""
        with self._active_lock:
            busy = bool(self.active)
        if busy:
            return
        try:
            from app.ops.winsys import release_memory

            r = release_memory(trim_working_set=True)
            log.info("idle after %s: worker memory ws %s -> %s MB, private %s -> %s MB", what,
                     *(None if r.get(k) is None else round(r[k]) for k in
                       ("before_mb", "after_mb", "private_before_mb", "private_after_mb")))
        except Exception as e:  # pragma: no cover
            log.debug("release_memory failed: %s", e)

    def _scheduled_dag(self, dag: str) -> None:
        key = f"schedule:{dag}"
        self._track(key, {"kind": "dag", "dag": dag, "trigger": "schedule", "since": datetime.now(UTC).isoformat()})
        try:
            rep = self.runner.run_dag(dag, RunOptions(trigger="schedule"))
            log.info("scheduled dag finished:\n%s", rep.summary())
        except Exception:
            log.exception("scheduled dag %s crashed", dag)
        finally:
            self._track(key, None)
            self._release_if_idle(f"dag {dag}")

    def handle_request(self, path: Path, req: dict[str, Any]) -> dict[str, Any]:
        """Execute one queue request synchronously and move it to done/ or failed/."""
        key = f"queue:{req.get('request_id')}"
        self._track(key, {k: req.get(k) for k in ("request_id", "run_id", "kind", "job_id", "dag", "trigger")}
                    | {"since": datetime.now(UTC).isoformat()})
        try:
            params, as_of, force = _parse_params(req.get("params", {}))
            trigger = req.get("trigger", "api")
            opts = RunOptions(as_of=as_of, params=params, trigger=trigger, run_id=req.get("run_id"),
                              force=bool(force) if force is not None else trigger == "api")
            if req.get("kind") == "dag":
                rep = self.runner.run_dag(req["dag"], opts)
            else:
                rep = self.runner.run_jobs([req["job_id"]], opts, with_downstream=bool(req.get("include_downstream")))
            result = rep.to_dict()
            jobqueue.complete(path, req, result, ok=rep.status != "failed", settings=self.settings)
            log.info("queue request %s done: %s", req.get("request_id"), rep.status)
            return result
        except Exception as e:
            log.exception("queue request %s crashed", req.get("request_id"))
            result = {"error": f"{type(e).__name__}: {e}"}
            jobqueue.complete(path, req, result, ok=False, settings=self.settings)
            return result
        finally:
            self._track(key, None)
            self._release_if_idle(f"request {req.get('request_id')}")

    def poll_queue(self, block: bool = False) -> int:
        """Claim pending requests up to the concurrency limit. block=True runs them inline (used by --once/tests)."""
        n = 0
        self._futures = [f for f in self._futures if not f.done()]
        while not self.stop_event.is_set() and (block or len(self._futures) < self.max_concurrent):
            claimed = jobqueue.claim_next(self.settings)
            if not claimed:
                break
            path, req = claimed
            n += 1
            log.info("claimed queue request %s (%s %s)", req.get("request_id"), req.get("kind"), req.get("job_id") or req.get("dag"))
            if block:
                self.handle_request(path, req)
            else:
                self._futures.append(self.pool.submit(self.handle_request, path, req))
        return n

    def run_forever(self) -> None:
        last_prune = time.monotonic()
        stop_file = self.settings.state_dir / "worker.STOP"
        while not self.stop_event.is_set():
            if stop_file.exists():  # file-based stop (supervisor / tray app; works without a console)
                log.info("worker.STOP present - shutting down")
                try:
                    stop_file.unlink()
                except OSError:
                    pass
                self.stop_event.set()
                break
            try:
                self.poll_queue()
                if self.profile_sched is not None:
                    self.profile_sched.tick()
                try:  # incident 2026-10-24: keep the pagefile trend of the machine guard (process jobs defer on it)
                    from app.ops.governor import machine_guard

                    self.pressure = machine_guard()
                except Exception:  # noqa: BLE001
                    pass
                self.heartbeat()
                if time.monotonic() - last_prune > 24 * 3600:
                    jobqueue.prune(settings=self.settings)
                    last_prune = time.monotonic()
            except Exception:
                log.exception("worker loop error")
            self.stop_event.wait(self.poll_s)

    def shutdown(self) -> None:
        log.info("worker shutting down (grace %.0fs)", self.shutdown_grace_s)
        self.stop_event.set()
        if self.profile_sched is not None:
            self.profile_sched.shutdown()
        elif self.scheduler is not None:
            try:
                self.scheduler.shutdown(wait=False)
            except Exception:
                pass
        deadline = time.monotonic() + self.shutdown_grace_s
        for f in self._futures:
            try:
                f.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception:
                pass
        while self.active and time.monotonic() < deadline:  # scheduled DAG runs live in APScheduler threads
            time.sleep(0.5)
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.heartbeat()
        self.release_lock()
        log.info("worker stopped")


def _install_signal_handlers(worker: Worker) -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.info("signal %s received", signum)
        worker.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # not in main thread / unsupported
                pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.scheduler.worker", description="BiG Equity 360 worker")
    ap.add_argument("--once", action="store_true", help="process the job queue once and exit")
    ap.add_argument("--no-schedule", action="store_true", help="do not run cron DAGs (queue only)")
    ap.add_argument("--poll", type=float, default=None, help="queue poll interval seconds")
    ap.add_argument("--realtime", action="store_true", help="also run the realtime supervisor (R1); default off")
    args = ap.parse_args(argv)

    s = get_settings()
    s.ensure_dirs()
    setup_logging(log_file="worker.log", service="worker")
    worker = Worker(s, enable_schedule=not (args.no_schedule or args.once), poll_s=args.poll)
    try:
        worker.acquire_lock()
    except WorkerAlreadyRunning as e:
        print(f"worker already running: {e}", file=sys.stderr)
        return 2
    log.info("worker %s starting pid=%d tz=%s data=%s", __version__, os.getpid(), worker.tz, s.data_dir)
    try:
        worker.startup_hygiene()
        if args.once:
            n = worker.poll_queue(block=True)
            log.info("processed %d queue request(s)", n)
            return 0
        _install_signal_handlers(worker)
        if worker.enable_schedule:
            worker.start_scheduler()
        else:
            log.warning("SCHEDULER OFF: worker started with --no-schedule - no scheduled refresh will run (the System "
                        "page and the header show 'Scheduler off'; start without --no-schedule or use the supervisor)")
        rt = None
        if args.realtime or os.environ.get("BIG360_REALTIME", "0") == "1":
            try:
                from app.realtime.supervisor import Supervisor

                rt = Supervisor(s)
                rt.start_in_thread()
                log.info("realtime supervisor started inside the worker")
            except Exception:
                log.exception("realtime supervisor could not start (worker continues without it)")
                rt = None
        worker.heartbeat()
        try:
            worker.run_forever()
        except KeyboardInterrupt:
            worker.stop_event.set()
        finally:
            if rt is not None:
                rt.stop(timeout=60)
        return 0
    finally:
        worker.shutdown()


if __name__ == "__main__":
    sys.exit(main())
