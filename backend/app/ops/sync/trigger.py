"""Supervisor side of the startup sync (docs/STARTUP_SYNC_PLAN.md A4).

The supervisor owns one SyncTrigger. Every loop it calls tick(); the trigger starts
`python -m app.ops.sync run --trigger <why>` as a one-shot child in its own Windows Job Object at BelowNormal
priority (the whole tree dies with the job: no leaked job processes):
  * `startup`  - once, start_delay_s after the API child is ready (the dashboard opens first);
  * `interval` - every interval_min while the app is open (counted from the end of the previous sync);
  * `manual` / `api` / `tray` - on a request file data/state/ops/sync_now.json (tray "Sync now",
    POST /api/v1/system/sync, `python -m app.ops.sync request`), picked up within one supervisor loop (~2 s).
Never two at once: the sync's own lock (dag_sync.lock) is checked first (a hand-started sync counts), and nothing
starts while a refresh DAG (nightly / weekly / monthly) holds its lock - the refresh covers the latest data and the
interval tick retries later. Stale detection: a lock is an OS file lock, released when its process dies.
Settings: config/ops.yaml `sync:` {enabled, start_delay_s, interval_min, passes}; env BIG360_SYNC=0 disables it.
Coverage v3 A3 - stale-lock recovery: while a sync holds the lock, every tick checks app.ops.sync.state.lock_status
(heartbeat older than stale_heartbeat_s, or older than hard_max_h). Stale for stale_confirm_s in a row (a resumed
laptop's sync heartbeats again within 5 s) -> sync.STOP is written; still running stop_grace_s later -> its Job
Object is terminated (our child), or the owner pid's tree when it is an `app.ops.sync` process started outside the
supervisor. The next interval sync then starts normally. BIG360_SYNC_CAPS=0 turns the recovery off.
"""

from __future__ import annotations

import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.paths import app_logs_dir, config_dir, read_yaml
from app.ops.sync import state as S

log = get_logger("app.ops.sync.trigger")

DEFAULTS: dict[str, Any] = {"enabled": True, "start_delay_s": 90, "interval_min": 30, "passes": 2,
                            "stop_grace_s": 120}


def sync_config(settings: Settings | None = None) -> dict[str, Any]:
    doc = read_yaml(config_dir(settings) / "ops.yaml")
    cfg = {**DEFAULTS, **(doc.get("sync") or {})}
    if os.environ.get("BIG360_SYNC", "1") == "0":
        cfg["enabled"] = False
    return cfg


class SyncTrigger:
    def __init__(self, settings: Settings | None = None, python: str | None = None, cwd: Path | None = None,
                 env: dict[str, str] | None = None, cfg: dict[str, Any] | None = None, clock: Any = time.monotonic,
                 popen: Any = None):
        import sys

        self.s = settings or get_settings()
        self.cfg = cfg or sync_config(self.s)
        self.python = python or sys.executable
        self.cwd = cwd or (self.s.project_root / "backend")
        self.env = dict(env or {})
        self.clock = clock
        self._popen = popen
        self.proc: Any = None
        self.job: Any = None
        self.trigger: str | None = None
        self.started_at: str | None = None
        self.started_mono: float | None = None
        self.last_exit: int | None = None
        self.last_finished_mono: float | None = None
        self.last_finished_at: str | None = None
        self.started_once = False
        self.api_ready_mono: float | None = None
        self.state = "idle" if self.cfg.get("enabled", True) else "disabled"
        self.detail: str | None = None
        self.runs = 0
        # coverage v3 A3: stale-lock recovery
        self._stale_since: float | None = None
        self._stale_stop_at: float | None = None
        self.stale_kills = 0
        self.last_stale: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _reap(self) -> None:
        if self.proc is None or self.proc.poll() is None:
            return
        self.last_exit = self.proc.returncode
        self.proc = None
        if self.job is not None:
            try:
                left = self.job.terminate()
                if left:
                    log.warning("sync: %d leftover process(es) killed with its job object", left)
                self.job.close()
            except Exception:  # pragma: no cover
                pass
            self.job = None
        self.last_finished_mono = self.clock()
        self.last_finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.state = "idle"
        log.info("sync (%s) finished with exit code %s", self.trigger, self.last_exit)

    def next_due_in_s(self) -> float | None:
        if not self.enabled:
            return None
        now = self.clock()
        if not self.started_once:
            if self.api_ready_mono is None:
                return None
            return max(0.0, self.api_ready_mono + float(self.cfg["start_delay_s"]) - now)
        base = self.last_finished_mono if self.last_finished_mono is not None else self.started_mono or now
        return max(0.0, base + float(self.cfg["interval_min"]) * 60 - now)

    def tick(self, api_ready: bool = True) -> str | None:
        """Start a sync when one is due; returns the trigger it started with (or None)."""
        self._reap()
        if not self.enabled:
            self.state = "disabled"
            return None
        if api_ready and self.api_ready_mono is None:
            self.api_ready_mono = self.clock()
        if self.running():
            self.state = "running"
            self._check_stale(ours=True)
            return None
        req = S.pending_request(self.s)
        why: str | None = None
        if req is not None:
            by = str(req.get("by") or "manual")
            why = by if by in ("api", "tray", "cli") else "manual"
            why = "manual" if why == "cli" else why
        elif not self.started_once and self.api_ready_mono is not None and \
                self.clock() - self.api_ready_mono >= float(self.cfg["start_delay_s"]):
            why = "startup"
        elif self.started_once and (self.next_due_in_s() or 0.0) <= 0.0:
            why = "interval"
        if why is None:
            self.state = "idle"
            return None
        if S.sync_running(self.s):
            self.state, self.detail = "external", "a sync started outside the supervisor is running"
            if why == "startup":
                self.started_once = True
            self._check_stale(ours=False)
            return None
        from app.ops.sync.run import REFRESH_DAGS, _dag_lock_held

        held = [d for d in REFRESH_DAGS if _dag_lock_held(d, self.s)]
        if held:
            self.state, self.detail = "waiting", f"refresh DAG {held[0]} running: the sync waits for it"
            if why == "startup":
                self.started_once = True
                self.last_finished_mono = self.clock()   # retry on the interval
            return None
        datasets = list((req or {}).get("datasets") or [])
        if req is not None:
            S.take_request(self.s)
        self.start(why, datasets)
        return why

    def _check_stale(self, ours: bool) -> None:
        """Coverage v3 A3: stop / kill a sync that holds the lock but is hung (see the module docstring)."""
        from app.ops.sync import caps as C

        caps = C.load_caps(self.cfg)
        if not caps.enabled:
            return
        try:
            ls = S.lock_status(self.s, caps.stale_heartbeat_s, caps.hard_max_h, held=True)
        except Exception as e:  # pragma: no cover - never break the supervisor loop
            log.debug("sync lock status failed: %s", e)
            return
        if not ls.get("stale"):
            self._stale_since = self._stale_stop_at = None
            return
        now = self.clock()
        if self._stale_since is None:
            self._stale_since = now
            log.warning("sync looks stale (%s); confirming for %.0f s", ls.get("why"), caps.stale_confirm_s)
            return
        if now - self._stale_since < caps.stale_confirm_s:
            return
        owner = ls.get("owner") or {}
        if self._stale_stop_at is None:
            try:
                S.stop_path(self.s).write_text(f"supervisor: stale sync ({ls.get('why')})", encoding="utf-8")
            except OSError:  # pragma: no cover
                pass
            self._stale_stop_at = now
            self.detail = f"stale sync {owner.get('run_id') or ''} ({ls.get('why')}): stop requested".strip()
            log.error("sync %s is stale (%s): STOP requested", owner.get("run_id"), ls.get("why"))
            return
        if now - self._stale_stop_at < float(self.cfg.get("stop_grace_s", 120)):
            return
        killed = self._kill_stale(ours, owner)
        self.stale_kills += 1
        self.last_stale = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "run_id": owner.get("run_id"),
                           "pid": owner.get("pid"), "why": ls.get("why"), "killed": killed, "ours": ours}
        self.detail = f"stale sync {owner.get('run_id') or ''} killed ({ls.get('why')})"
        log.error("sync %s did not stop after the STOP request: killed=%s", owner.get("run_id"), killed)
        self._stale_since = self._stale_stop_at = None

    def _kill_stale(self, ours: bool, owner: dict[str, Any]) -> bool:
        from app.ops import winsys

        if ours and self.proc is not None:
            if self.job is not None:
                return bool(self.job.terminate() or True)
            try:
                winsys.kill_tree(self.proc.pid)
            except Exception:  # pragma: no cover
                self.proc.kill()
            return True
        pid = int(owner.get("pid") or 0)
        if not pid or not winsys.pid_alive(pid):
            return False
        cmd = (winsys.process_cmdline(pid) or "").lower()
        if "app.ops.sync" not in cmd:   # never kill a process we cannot identify as a sync
            log.warning("stale sync owner pid %s is not an app.ops.sync process (%s): not killed", pid, cmd[:120])
            return False
        return bool(winsys.kill_tree(pid))

    def command(self, why: str, datasets: list[str] | None = None) -> list[str]:
        cmd = [self.python, "-m", "app.ops.sync", "run", "--trigger", why, "--passes", str(int(self.cfg.get("passes", 2)))]
        for d in datasets or []:
            cmd += ["--dataset", d]
        return cmd

    def start(self, why: str, datasets: list[str] | None = None) -> None:
        from app.ops import winsys

        cmd = self.command(why, datasets)
        logp = app_logs_dir(self.s) / "sync.out.log"
        fh = open(logp, "ab")  # noqa: SIM115 - handed to the child
        fh.write(f"\n==== {datetime.now(UTC).isoformat(timespec='seconds')} sync start ({why})\n".encode())
        fh.flush()
        env = dict(os.environ)
        env.update(self.env)
        env["PYTHONUNBUFFERED"] = "1"
        try:
            if self._popen is not None:
                self.proc = self._popen(cmd, cwd=str(self.cwd), env=env, stdout=fh, stderr=subprocess.STDOUT)
                self.job = None
            else:
                job = winsys.JobObject(name="sync")
                self.proc = winsys.start_in_job(cmd, job if job.ok else None, cwd=str(self.cwd), env=env, stdout=fh,
                                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                creationflags=winsys.priority_creationflags("below_normal",
                                                                                            new_group=True))
                self.job = job if job.ok else None
                if not job.ok:
                    job.close()
        finally:
            fh.close()
        self.trigger, self.started_once = why, True
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.started_mono = self.clock()
        self.state, self.detail = "running", None
        self.runs += 1
        log.info("sync started (%s) pid=%s", why, getattr(self.proc, "pid", None))

    def stop(self, grace_s: float | None = None) -> None:
        """Supervisor shutdown: ask the sync to stop (STOP file), then kill its job after the grace period."""
        if not self.running():
            return
        grace = float(grace_s if grace_s is not None else self.cfg.get("stop_grace_s", 120))
        try:
            S.stop_path(self.s).write_text("supervisor", encoding="utf-8")
        except OSError:
            pass
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            log.warning("sync did not stop within %.0fs: killing its job", grace)
            if self.job is not None:
                self.job.terminate()
            else:
                try:
                    from app.ops import winsys

                    winsys.kill_tree(self.proc.pid)
                except Exception:  # pragma: no cover
                    self.proc.kill()
        self._reap()

    def status(self) -> dict[str, Any]:
        nxt = self.next_due_in_s()
        return {"enabled": self.enabled, "state": self.state, "detail": self.detail, "trigger": self.trigger,
                "pid": self.proc.pid if self.running() else None, "started_at": self.started_at,
                "last_finished_at": self.last_finished_at, "last_exit": self.last_exit, "runs": self.runs,
                "interval_min": self.cfg.get("interval_min"), "start_delay_s": self.cfg.get("start_delay_s"),
                "next_in_s": round(nxt) if nxt is not None and not self.running() else None,
                "job_object": self.job is not None, "stale_kills": self.stale_kills, "last_stale": self.last_stale}
