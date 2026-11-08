"""Supervisor (O1 integration; core of the O3 Windows app): starts and monitors the API, the worker (scheduler ON with
the active schedule profile), the gap filler and - roadmap v3 N2.6 - the realtime / live news service, restarting
them when they crash.

    python -m app.ops.supervisor                       # API :8010 + worker + filler
    python -m app.ops.supervisor --api-port 8080 --no-filler
    python -m app.ops.supervisor --status              # print data/logs/app/supervisor.json
    python -m app.ops.supervisor --app                 # O3 app mode: the API also serves the built UI at /
    python -m app.ops.supervisor --app --api-port 8095 --no-filler --no-schedule   # smoke test on a spare port

* App mode (--app, used by the tray / BiG360.exe): --serve-ui defaults on, i.e. the API child gets BIG360_SERVE_UI=1
  and BIG360_UI_DIR (<root>/app_dist or <root>/frontend/dist_app; app.api.static_ui) so http://host:port/ is the
  dashboard with no Node at runtime. --serve-ui / --no-serve-ui override it in either mode.
* API keys stored in Windows Credential Manager (app.desktop.credentials) are passed to every child as environment
  variables (they win over .env; an explicit environment variable wins over both). --no-credentials turns it off.

* Children run in their own process group (CTRL+BREAK reaches only them); stdout/stderr -> data/logs/app/<name>.out.log
  (rotated by housekeeping). pids -> data/logs/app/<name>.pid, status -> data/logs/app/supervisor.json.
* Crash -> restart with back-off 5 s, 15 s, 60 s, 5 min (reset after 10 min of uptime). A child that cannot start
  because another instance holds its lock (worker.lock / filler.lock) or the API port is already served by a
  foreign program is marked `external` and re-checked every minute - so the supervisor can be started next to
  hand-started processes and takes over when they stop (switch-over without a gap).
* Incident 2026-10-24 hardening:
  - every child runs in its own Windows Job Object (KILL_ON_JOB_CLOSE): stop / restart terminates the whole tree,
    including multiprocessing spawn workers whose parent already died; if the supervisor dies the kernel kills
    the children with it (no orphaned API pairs holding RAM and DuckDB handles);
  - API readiness: health counting starts only once the port is open (cold-start grace up to COLD_START_MAX_S =
    5 min, then a failure); then GET /health every HEALTH_EVERY_S, timeout 10 s (30 s while the machine is under
    memory pressure or after a failed probe), HEALTH_FAILS consecutive failures -> restart;
  - restarts (health / cold start) back off exponentially (RESTART_BACKOFF_S, reset after 10 min ready), and a new
    instance is never started while the previous one is still exiting (state `stopping` until its job is empty);
  - before an API start the port is cleared: a stray `app.cli serve api` of our interpreter holding it is killed
    (warning); a foreign listener -> `external`;
  - housekeeping every REAP_EVERY_S: the orphan reaper (app.ops.reaper) kills our leaked spawn children and stray
    APIs (on our port, or orphaned; BIG360_SUPERVISOR_STRAY_API=all kills every unowned one, =off none);
  - observability: per-child restart reasons, restarts in the last 30 min, an alert after ALERT_RESTARTS restarts
    within ALERT_WINDOW_S, reaper metrics (leaked / killed) -> supervisor.json -> /system/status + Data Health.
* Startup sync (docs/STARTUP_SYNC_PLAN.md A, app.ops.sync.trigger): ~90 s after the API is ready, every 30 min and on
  a sync-now request (tray, POST /system/sync) the supervisor starts `python -m app.ops.sync run` in its own Job
  Object at BelowNormal priority (never two at once, never next to a refresh DAG; --no-sync / BIG360_SYNC=0).
* Stop: Ctrl+C / SIGBREAK / SIGTERM, or create data/state/ops/supervisor.STOP. Children with a STOP file (worker,
  filler, realtime, quotes) get only the file - never CTRL+BREAK (incident 2026-10-24 addendum); only the API
  (uvicorn, no pools) gets CTRL+BREAK. The worker finishes / cancels running jobs (grace 330 s); realtime (60 s) and
  quotes (30 s) clean up and hard-exit by themselves (app.ops.shutdown, alert hygiene 2026-10-30). After the grace
  the child's Job Object is terminated; a member still in its exit then blocks the next start for up to
  EXIT_TEARDOWN_S (teardown of a multi-GB process) before it is reported as a zombie.
* Single instance: data/state/ops/supervisor.lock.
* Live news (4th child `realtime` = `python -m app.cli realtime start`: GDELT 15-min, RSS, Polygon news, filings /
  macro streams, news desk, live alerts): on by default in app mode (--live-news / --no-live-news). The tray toggle
  "Live news on/off" writes / removes data/state/ops/live_news.OFF; the supervisor stops (realtime.STOP, graceful) or
  starts the child within ~2 s. RAM governor: the child's process tree may use LIVE_NEWS_RAM_CAP_MB (1536, env
  BIG360_LIVE_NEWS_RAM_MB); 6 samples in a row above the cap (~60 s) -> graceful restart (recorded in its history).
* Live quotes (5th child `quotes` = `python -m app.cli realtime quotes`, workstream B of docs/STARTUP_SYNC_PLAN.md:
  delayed Yahoo / Polygon quotes for watchlists, theses, indices, FX, futures and the cockpit while markets are open):
  on by default in app mode (--live-quotes / --no-live-quotes). Its own child, not part of `realtime`: the realtime
  child already runs at ~1.3-1.5 GB (FinBERT) against its 1536 MB cap and is restarted by the cap every 1-2 h; the
  quotes child is ~150-250 MB and must keep polling through those restarts. Tray toggle "Live quotes on/off" ->
  data/state/ops/live_quotes.OFF. RAM cap LIVE_QUOTES_RAM_CAP_MB (512, env BIG360_LIVE_QUOTES_RAM_MB). Own Job Object
  like every child; graceful stop via data/state/quotes.STOP.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from datetime import timedelta as _td
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger, setup_logging
from app.ops import winsys
from app.ops.paths import app_logs_dir, ops_state_dir, read_json, write_json_atomic

log = get_logger("app.ops.supervisor")

BACKOFF_S = (5, 15, 60, 300)
HEALTHY_AFTER_S = 600
COLD_START_MAX_S = float(os.environ.get("BIG360_API_COLD_START_S", "300"))  # port must open within this
HEALTH_EVERY_S = 60.0
HEALTH_FIRST_S = 5.0          # first probe this long after the port opened
HEALTH_FAILS = 3
HEALTH_TIMEOUT_S = 10.0
HEALTH_TIMEOUT_LOAD_S = 30.0  # under memory pressure / after a failed probe
RESTART_BACKOFF_S = (10, 30, 60, 120, 300, 600)
STOP_POLL_S = 15.0            # wait for the previous instance's job to empty before giving up for this tick
ALERT_RESTARTS = 3
ALERT_WINDOW_S = 1800.0
REAP_EVERY_S = 300.0
STRAY_API_POLICY = os.environ.get("BIG360_SUPERVISOR_STRAY_API", "default")  # default | all | off
LIVE_NEWS_RAM_CAP_MB = float(os.environ.get("BIG360_LIVE_NEWS_RAM_MB", "2048"))
LIVE_QUOTES_RAM_CAP_MB = float(os.environ.get("BIG360_LIVE_QUOTES_RAM_MB", "512"))
#: studio lane (docs/STUDIO_V2_PLAN.md S1.1): hard committed-memory cap of its Job Object (lane + warm workers)
STUDIO_RAM_CAP_MB = float(os.environ.get("BIG360_STUDIO_RAM_MB", "6144"))
RAM_GRACE_S = 600   # no RAM strikes in the first 10 min after a (re)start
RAM_STRIKES = 6     # ~60 s sustained (a GDELT parse peak of the child lasts < 30 s)
#: alert hygiene 2026-10-30: a member that is still in its exit right after a stop / job termination is tearing down
#: (a 1.5-2.8 GB process takes seconds to minutes to free its memory) - it blocks a restart for up to this long;
#: only after that is it a zombie (stuck in its exit: reported, never waited on again)
EXIT_TEARDOWN_S = float(os.environ.get("BIG360_EXIT_TEARDOWN_S", "90"))
TEARDOWN_WAIT_S = 5.0   # inline wait for members in their exit after a stop (then the `stopping` state takes over)


def live_news_flag(settings: Settings | None = None) -> Path:
    """Present = live news switched off (tray toggle)."""
    return ops_state_dir(settings) / "live_news.OFF"


def live_news_enabled(settings: Settings | None = None) -> bool:
    return not live_news_flag(settings).exists()


def set_live_news(on: bool, settings: Settings | None = None, by: str = "tray") -> bool:
    f = live_news_flag(settings)
    if on:
        f.unlink(missing_ok=True)
    else:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"off by {by} {datetime.now(UTC).isoformat(timespec='seconds')}", encoding="utf-8")
    return on


def live_quotes_flag(settings: Settings | None = None) -> Path:
    """Present = live quotes switched off (tray toggle)."""
    return ops_state_dir(settings) / "live_quotes.OFF"


def live_quotes_enabled(settings: Settings | None = None) -> bool:
    return not live_quotes_flag(settings).exists()


def set_live_quotes(on: bool, settings: Settings | None = None, by: str = "tray") -> bool:
    f = live_quotes_flag(settings)
    if on:
        f.unlink(missing_ok=True)
    else:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"off by {by} {datetime.now(UTC).isoformat(timespec='seconds')}", encoding="utf-8")
    return on


def _lock_free(path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    lk = FileLock(str(path), timeout=0)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return False
    lk.release()
    return True


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def api_healthy(host: str, port: int, timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as r:  # noqa: S310 - localhost
            return r.status == 200
    except Exception:
        return False


def under_pressure() -> bool:
    """Machine under memory pressure (RAM nearly full): slow responses are expected, probes get more time."""
    ps = winsys.pressure_sample()
    return bool(ps) and (ps["avail_gb"] < 3.0 or ps["load_pct"] >= 85)


def health_timeout(c: Child) -> float:
    return HEALTH_TIMEOUT_LOAD_S if (c.health_fail > 0 or under_pressure()) else HEALTH_TIMEOUT_S


@dataclass
class Child:
    name: str
    command: list[str]
    priority: str = "normal"
    grace_s: float = 60.0
    lock_path: Path | None = None              # single-instance lock of the child (external instance detection)
    port: tuple[str, int] | None = None        # API: host / port
    stop_file: Path | None = None              # file-based graceful stop (works without a shared console)
    env: dict[str, str] = field(default_factory=dict)  # extra environment of this child
    ram_cap_mb: float | None = None             # RAM governor (process tree); None = not governed
    job_mem_mb: float | None = None             # hard Job Object committed-memory cap (allocations fail beyond it)
    toggle: Path | None = None                 # flag file: present = switched off (state `disabled`)
    ram_strikes: int = 0
    rss_mb: float | None = None
    popen: subprocess.Popen | None = None
    state: str = "stopped"                     # starting | running | external | backoff | stopped | failed
    restarts: int = 0
    started_at: float | None = None
    started_at_iso: str | None = None
    last_exit: int | None = None
    next_try: float = 0.0
    fail_streak: int = 0
    health_fail: int = 0
    last_health: float = 0.0
    log_path: Path | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    ready_file: Path | None = None             # optional "ready" marker written by the child (alternative to the port)
    job: Any = None                            # winsys.JobObject of the running / exiting instance
    ready: bool = False
    ready_at: float | None = None
    last_health_ok_at: str | None = None
    restart_streak: int = 0                    # consecutive health / cold-start restarts (exponential back-off)
    restart_pending: bool = False              # `stopping` -> `backoff` (True) or `stopped` (False)
    stopping_since: float | None = None
    old_pids: list[int] = field(default_factory=list)
    reasons: list[dict[str, Any]] = field(default_factory=list)   # newest first: {at, reason, backoff_s}
    restart_times: list[float] = field(default_factory=list)      # wall clock of every restart (alert window)
    alert: dict[str, Any] | None = None

    @property
    def pid(self) -> int | None:
        return self.popen.pid if self.popen is not None and self.popen.poll() is None else None

    def restarts_within(self, window_s: float = ALERT_WINDOW_S, now: float | None = None) -> int:
        now = time.time() if now is None else now
        return sum(1 for t in self.restart_times if now - t <= window_s)


class Supervisor:
    def __init__(self, settings: Settings | None = None, api_host: str = "127.0.0.1", api_port: int = 8010,
                 api: bool = True, worker: bool = True, filler: bool = True, realtime: bool = False,
                 python: str | None = None, serve_ui: bool = False, ui_dir: Path | None = None,
                 schedule: bool = True, extra_env: dict[str, str] | None = None, app_mode: bool = False,
                 live_news: bool = False, live_quotes: bool = False, sync: bool = False, studio: bool = False):
        self.s = settings or get_settings()
        py = python or sys.executable
        self.backend = self.s.project_root / "backend"
        self.logs = app_logs_dir(self.s)
        self.stop_event = threading.Event()
        self.children: list[Child] = []
        self.api_host, self.api_port = api_host, api_port
        self.app_mode = app_mode
        self.extra_env = dict(extra_env or {})     # e.g. API keys from Credential Manager: never logged / in status
        self.ui_dir: Path | None = None
        if serve_ui:
            from app.api.static_ui import resolve_ui_dir

            self.ui_dir = ui_dir or resolve_ui_dir(self.s)
            if self.ui_dir is None:
                log.warning("--serve-ui: no built UI found (npm run build:app -> frontend/dist_app); API only")
        if api:
            api_env = {"BIG360_SERVE_UI": "1", "BIG360_UI_DIR": str(self.ui_dir)} if self.ui_dir else {}
            self.children.append(Child("api", [py, "-m", "app.cli", "serve", "api", "--host", api_host, "--port", str(api_port)],
                                       grace_s=20, port=(api_host, api_port), env=api_env))
        if worker:
            # the live news child owns the realtime layer: the worker must not also start it in a thread
            cmd = [py, "-m", "app.scheduler.worker"] + (["--realtime"] if realtime and not live_news else []) \
                + ([] if schedule else ["--no-schedule"])
            self.children.append(Child("worker", cmd, grace_s=330, lock_path=self.s.state_dir / "worker.lock",
                                       stop_file=self.s.state_dir / "worker.STOP"))
        if filler:
            self.children.append(Child("filler", [py, "-m", "app.ops.filler"], priority="below_normal", grace_s=330,
                                       lock_path=ops_state_dir(self.s) / "filler.lock",
                                       stop_file=ops_state_dir(self.s) / "filler.STOP"))
        if studio:
            # warm interactive lane for Model Studio / backtest runs (S1.1): outside the worker queue and the heavy
            # slot, its own Job Object with a hard RAM cap; kill switch BIG360_STUDIO_LANE=0 (API -> worker jobs)
            self.children.append(Child("studio", [py, "-m", "app.modeling.lane"], grace_s=30,
                                       lock_path=self.s.state_dir / "studio_lane.lock",
                                       stop_file=self.s.state_dir / "studio_lane" / "STOP",
                                       job_mem_mb=STUDIO_RAM_CAP_MB,
                                       env={"BIG360_STUDIO_RAM_MB": f"{STUDIO_RAM_CAP_MB:g}"}))
        if live_news:
            self.children.append(Child("realtime", [py, "-m", "app.cli", "realtime", "start"], grace_s=60,
                                       lock_path=self.s.state_dir / "realtime.lock",
                                       stop_file=self.s.state_dir / "realtime.STOP",
                                       ram_cap_mb=LIVE_NEWS_RAM_CAP_MB, toggle=live_news_flag(self.s),
                                       env={"POLARS_MAX_THREADS": "2", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2"}))
        if live_quotes:
            self.children.append(Child("quotes", [py, "-m", "app.cli", "realtime", "quotes"], priority="below_normal",
                                       grace_s=30, lock_path=self.s.state_dir / "quotes.lock",
                                       stop_file=self.s.state_dir / "quotes.STOP",
                                       ram_cap_mb=LIVE_QUOTES_RAM_CAP_MB, toggle=live_quotes_flag(self.s),
                                       env={"POLARS_MAX_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}))
        self._lock = FileLock(str(ops_state_dir(self.s) / "supervisor.lock"), timeout=0)
        self.started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.reaper_stats: dict[str, Any] = {"runs": 0, "killed_total": 0, "freed_mb_total": 0, "leaked_now": 0,
                                             "last_run_at": None, "last_killed": [], "stray_api_killed_total": 0,
                                             "stray_apis": [], "zombies": [], "zombie_mb": 0, "exiting": [],
                                             "error": None}
        self._last_reap = 0.0
        self._warned_stray: set[int] = set()
        #: reaper + stray-API housekeeping (BIG360_SUPERVISOR_REAPER=0 turns it off; the test-suite does)
        self.housekeeping_enabled = os.environ.get("BIG360_SUPERVISOR_REAPER", "1") != "0"
        #: startup sync (docs/STARTUP_SYNC_PLAN.md A4): after the API is ready, every 30 min, on request
        self.sync_trigger: Any = None
        if sync:
            from app.ops.sync.trigger import SyncTrigger

            self.sync_trigger = SyncTrigger(self.s, python=py, cwd=self.backend, env=self.extra_env)

    # ------------------------------------------------------------------ children
    def _external(self, c: Child) -> bool:
        if c.lock_path is not None and not _lock_free(c.lock_path):
            return True
        if c.port is not None and port_open(*c.port):
            return True
        return False

    # ---- ownership / port hygiene
    def _owned_pids(self) -> set[int]:
        procs = winsys.process_list()
        out: set[int] = {os.getpid()}
        for c in self.children:
            if c.job is not None:
                out |= set(c.job.pids())
            if c.popen is not None and c.popen.poll() is None:
                out |= set(winsys.process_tree(c.popen.pid, procs))
            out |= set(c.old_pids)
        return out

    def _kill_stray(self, item: dict[str, Any], why: str) -> list[int]:
        log.warning("stray API instance pid %s (port %s, %s MB, orphaned=%s) not owned by this supervisor: killing "
                    "(%s): %s", item["pid"], item["port"], item["rss_mb"], item["orphaned"], why, item["cmd"][:160])
        killed = winsys.kill_tree(item["pid"])
        self.reaper_stats["stray_api_killed_total"] += 1
        return killed

    def _clear_port(self, c: Child) -> bool:
        """Before an API start: True when the port is free (stray `app.cli serve api` instances of ours holding it
        are killed), False when a foreign program listens on it (-> external)."""
        if c.port is None:
            return True
        host, port = c.port
        holders = winsys.listening_pids(port)
        if not holders and not port_open(host, port, 0.5):
            return True
        from app.ops import reaper  # noqa: PLC0415

        owned = self._owned_pids()
        strays = {x["pid"]: x for x in reaper.find_stray_apis(owned, port, self.s.project_root)}
        parent = {p: pp for p, pp, _ in winsys.process_list()}
        foreign = []
        for pid in holders:
            top = parent.get(pid) if parent.get(pid) in strays else pid
            if top in strays:
                self._kill_stray(strays.pop(top), f"it holds port {port}")
            elif pid not in owned:
                foreign.append(pid)
        if foreign:
            log.error("port %s is held by a foreign process %s (not an app.cli serve api of ours): not starting %s",
                      port, foreign, c.name)
            return False
        end = time.monotonic() + STOP_POLL_S
        while time.monotonic() < end and (winsys.listening_pids(port) or (not winsys.IS_WINDOWS and port_open(host, port, 0.5))):
            time.sleep(0.25)
        return not winsys.listening_pids(port)

    def start_child(self, c: Child) -> None:
        if c.state == "stopping":       # never start while the previous instance is still exiting
            return
        if c.port is not None and not self._clear_port(c):
            if c.state != "external":
                log.info("%s port held outside the supervisor: monitoring (re-check every 60 s)", c.name)
            c.state, c.next_try = "external", time.monotonic() + 60
            self._pidfile(c, None)
            return
        if self._external(c):
            if c.state != "external":
                log.info("%s already running outside the supervisor: monitoring (re-check every 60 s)", c.name)
            c.state, c.next_try = "external", time.monotonic() + 60
            self._pidfile(c, None)
            return
        if c.stop_file is not None and c.stop_file.exists():  # stale stop request from a previous shutdown
            try:
                c.stop_file.unlink()
            except OSError:
                pass
        if c.ready_file is not None:
            try:
                c.ready_file.unlink()
            except OSError:
                pass
        c.log_path = self.logs / f"{c.name}.out.log"
        fh = open(c.log_path, "ab")  # noqa: SIM115 - handed to the child
        fh.write(f"\n==== {datetime.now(UTC).isoformat(timespec='seconds')} supervisor start {' '.join(c.command)}\n".encode())
        fh.flush()
        env = dict(os.environ)
        for k, v in self.extra_env.items():
            if not any(e.lower() == k.lower() for e in os.environ):  # an explicit variable wins
                env[k] = v
        env.update(c.env)
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("BIG360_API_PORT", str(self.api_port))
        job = winsys.JobObject(name=c.name)
        if c.job_mem_mb and job.ok and not job.set_memory_limit(int(c.job_mem_mb * 1024 * 1024)):
            log.warning("%s: job memory cap not applied (error %s)", c.name, job.error)
        try:
            c.popen = winsys.start_in_job(c.command, job if job.ok else None, cwd=str(self.backend), env=env,
                                          stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                          creationflags=winsys.priority_creationflags(c.priority, new_group=True))
        except BaseException:
            job.close()
            raise
        finally:
            fh.close()
        if job.ok and (c.popen.pid in job.assigned or job.contains(c.popen.pid)):
            c.job = job
        else:
            job.close()
            c.job = None
            if winsys.IS_WINDOWS:
                log.warning("%s: no job object (error %s): falling back to the process-tree kill", c.name, job.error)
        c.state, c.started_at = "running", time.monotonic()
        c.started_at_iso = datetime.now(UTC).isoformat(timespec="seconds")
        c.health_fail, c.ready, c.ready_at, c.last_health = 0, False, None, 0.0
        c.old_pids = []
        self._pidfile(c, c.popen.pid)
        log.info("started %s pid=%s%s", c.name, c.popen.pid, " (job object)" if c.job else "")

    def _pidfile(self, c: Child, pid: int | None) -> None:
        p = self.logs / f"{c.name}.pid"
        try:
            if pid:
                p.write_text(str(pid), encoding="utf-8")
            elif p.exists() and c.state != "external":
                p.unlink()
        except OSError:
            pass

    def _record_restart(self, c: Child, reason: str, backoff_s: float) -> None:
        now = time.time()
        c.reasons = ([{"at": datetime.now(UTC).isoformat(timespec="seconds"), "reason": reason,
                       "backoff_s": round(backoff_s, 1)}] + c.reasons)[:20]
        c.restart_times = [t for t in c.restart_times if now - t <= 24 * 3600] + [now]
        n = c.restarts_within(ALERT_WINDOW_S, now)
        if n >= ALERT_RESTARTS:
            first = c.alert is None
            c.alert = {"id": f"restart_storm_{c.name}", "severity": "error", "restarts_30m": n,
                       "since": datetime.fromtimestamp(min(t for t in c.restart_times if now - t <= ALERT_WINDOW_S),
                                                       UTC).isoformat(timespec="seconds"),
                       "last_reason": reason}
            if first:
                log.error("ALERT: %s restarted %d times within %.0f min (last: %s)", c.name, n, ALERT_WINDOW_S / 60,
                          reason)
        elif c.alert is not None and n < ALERT_RESTARTS:
            log.info("%s restart alert cleared (%d restarts in the window)", c.name, n)
            c.alert = None

    def restart_child(self, c: Child, reason: str) -> None:
        """Health / cold-start restart with exponential back-off (never overlapping the exiting instance)."""
        delay = RESTART_BACKOFF_S[min(c.restart_streak, len(RESTART_BACKOFF_S) - 1)]
        c.restart_streak += 1
        c.restarts += 1
        self._record_restart(c, reason, delay)
        c.history = ([{"exit": "restart", "reason": reason, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                       "uptime_s": round(time.monotonic() - (c.started_at or time.monotonic()))}] + c.history)[:20]
        log.error("%s: %s -> restart #%d (back-off %ss)", c.name, reason, c.restarts, delay)
        self.stop_child(c, restart=True)
        c.next_try = time.monotonic() + delay

    def check_child(self, c: Child) -> None:
        now = time.monotonic()
        if c.state == "stopping":   # the previous instance is still exiting: finish that first (whatever the toggle)
            self._finish_stop(c)
            return
        if c.toggle is not None:   # live news on/off (tray)
            if c.toggle.exists():
                if c.popen is not None and c.popen.poll() is None:
                    log.info("%s switched off (%s): stopping", c.name, c.toggle.name)
                    self.stop_child(c)
                if c.state != "stopping":
                    c.state, c.next_try = "disabled", now + 2
                return
            if c.state == "disabled":
                log.info("%s switched on: starting", c.name)
                c.state, c.next_try, c.fail_streak = "stopped", now, 0
        if c.state in ("external", "backoff", "stopped"):
            if now >= c.next_try:
                self.start_child(c)
            return
        if c.popen is None:
            self.start_child(c)
            return
        code = c.popen.poll()
        if code is None:
            if c.started_at and now - c.started_at > HEALTHY_AFTER_S:
                c.fail_streak = 0
            if c.ready_at and now - c.ready_at > HEALTHY_AFTER_S and c.health_fail == 0:
                c.restart_streak = 0
            if c.ram_cap_mb and self.ram_check(c):
                return
            if c.port is not None:
                self.health_check(c, now)
            return
        c.last_exit = code
        c.history = ([{"exit": code, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                       "uptime_s": round(now - (c.started_at or now))}] + c.history)[:20]
        self._pidfile(c, None)
        c.popen = None
        left = len(c.job.live_pids()) if c.job is not None else 0   # not counting members in their exit
        if c.job is not None:
            c.job.terminate()
        self._wait_teardown(c, min(TEARDOWN_WAIT_S, 3.0))
        if left:
            log.warning("%s exited with %s but left %d process(es) in its job: killed", c.name, code, left)
        if code == 2 and c.lock_path is not None:  # lost the single-instance race to an external instance
            self._close_job(c)
            c.state, c.next_try = "external", now + 60
            log.info("%s exited 2 (another instance holds its lock): monitoring", c.name)
            return
        delay = BACKOFF_S[min(c.fail_streak, len(BACKOFF_S) - 1)]
        c.fail_streak += 1
        c.restarts += 1
        self._record_restart(c, f"exited with code {code}", delay)
        c.next_try = now + delay
        c.restart_pending = True
        c.stopping_since = now           # teardown clock (members still in their exit block for EXIT_TEARDOWN_S)
        if self._gone(c):
            self._close_job(c)
            c.state, c.stopping_since = "backoff", None
        else:
            c.state = "stopping"
        log.warning("%s exited with %s; restart #%d in %ss", c.name, code, c.restarts, delay)

    def _is_ready(self, c: Child) -> bool:
        if c.ready_file is not None and c.ready_file.exists():
            return True
        return c.port is not None and port_open(c.port[0], c.port[1], 1.0)

    def health_check(self, c: Child, now: float | None = None) -> None:
        """Readiness, then liveness. Failures only count once the port is open (cold start can take minutes when
        the machine is loaded: imports 10x slower on 2026-10-24)."""
        now = time.monotonic() if now is None else now
        if not c.ready:
            if self._is_ready(c):
                c.ready, c.ready_at = True, now
                c.last_health = now - HEALTH_EVERY_S + HEALTH_FIRST_S
                log.info("%s ready after %.0fs (port %s open)", c.name, now - (c.started_at or now), c.port[1] if c.port else "-")
            elif now - (c.started_at or now) > COLD_START_MAX_S:
                self.restart_child(c, f"cold start: port not open after {COLD_START_MAX_S:.0f}s")
            return
        if now - c.last_health < HEALTH_EVERY_S:
            return
        c.last_health = now
        timeout = health_timeout(c)
        if api_healthy(*c.port, timeout=timeout):
            if c.health_fail:
                log.info("%s health ok again after %d failure(s)", c.name, c.health_fail)
            c.health_fail = 0
            c.last_health_ok_at = datetime.now(UTC).isoformat(timespec="seconds")
            return
        c.health_fail += 1
        log.warning("%s health check failed (%d/%d, timeout %.0fs)", c.name, c.health_fail, HEALTH_FAILS, timeout)
        if c.health_fail >= HEALTH_FAILS:
            self.restart_child(c, f"health: {c.health_fail} consecutive /health failures (timeout {timeout:.0f}s)")

    def ram_check(self, c: Child) -> bool:
        """RAM governor for capped children: True when the child was restarted for exceeding its cap."""
        if c.job is not None:
            mem = c.job.memory_mb() if c.pid else None
        else:
            mem = winsys.tree_memory_mb(c.pid) if c.pid else None
        c.rss_mb = mem
        if mem is None or not c.ram_cap_mb:
            return False
        if c.started_at and time.monotonic() - c.started_at < RAM_GRACE_S:
            return False      # start-up catch-up (bus backlog, missed GDELT slots, model load) is allowed to peak
        if mem <= c.ram_cap_mb:
            c.ram_strikes = 0
            return False
        c.ram_strikes += 1
        if c.ram_strikes < RAM_STRIKES:
            return False
        log.warning("%s uses %.0f MB > cap %.0f MB (%d samples): graceful restart", c.name, mem, c.ram_cap_mb,
                    c.ram_strikes)
        c.history = ([{"exit": "ram_cap", "at": datetime.now(UTC).isoformat(timespec="seconds"),
                       "rss_mb": round(mem)}] + c.history)[:20]
        c.ram_strikes = 0
        c.restarts += 1
        self._record_restart(c, f"RAM {mem:.0f} MB > cap {c.ram_cap_mb:.0f} MB", 5)
        self.stop_child(c, restart=True)
        return True

    # ---- stopping
    def _kill(self, c: Child, pid: int | None) -> None:
        if c.job is not None:
            c.job.terminate()
        if pid:
            winsys.kill_tree(pid)   # fallback / members that escaped the job

    def _gone(self, c: Child) -> bool:
        """Previous instance fully gone: no member of its job / no old pid still RUNNING, our port no longer served
        by them. A member that is in its exit is tearing down (normal right after a stop / job termination) and
        blocks the restart for EXIT_TEARDOWN_S counted from `stopping_since` (a caller without it starts the clock
        now); after that it is a zombie (stuck in its exit, unkillable): logged once, reported by the reaper, and it
        no longer blocks."""
        if c.job is not None and c.job.live_pids():
            return False
        if any(winsys.pid_running(p) for p in c.old_pids):
            return False
        if c.port is not None and {p for p in c.old_pids if winsys.pid_running(p)} & winsys.listening_pids(c.port[1]):
            return False
        z = sorted(set([p for p in c.old_pids if winsys.exiting(p)] + (c.job.zombies() if c.job is not None else [])))
        if z:
            now = time.monotonic()
            if c.stopping_since is None:
                c.stopping_since = now
            waited = now - c.stopping_since
            if waited < EXIT_TEARDOWN_S:
                return False             # still freeing its memory: never start the next instance next to it
            log.error("%s: previous instance left %d process(es) stuck in their exit %s for %.0fs (unkillable): "
                      "restarting anyway; see reaper.zombies", c.name, len(z), z, waited)
        return True

    def _wait_teardown(self, c: Child, timeout_s: float) -> bool:
        """Wait (bounded) until no old pid / job member exists any more, members in their exit included (the job's
        process count drops before e.g. conhost's process object is signalled). True when everything is gone."""
        end = time.monotonic() + max(0.0, timeout_s)
        while True:
            pids = set(c.old_pids) | (set(c.job.pids()) if c.job is not None else set())
            if not any(winsys.pid_alive(p) for p in pids):
                return True
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)

    def _close_job(self, c: Child) -> None:
        if c.job is not None:
            c.job.close()
        c.job = None
        c.old_pids = []

    def _finish_stop(self, c: Child) -> bool:
        """`stopping`: keep killing until the old instance is gone, then go to backoff / stopped."""
        self._kill(c, None)
        for p in c.old_pids:
            if winsys.pid_alive(p):
                winsys.kill_tree(p)
        if not self._gone(c):
            since = c.stopping_since or time.monotonic()
            if time.monotonic() - since > 60 and int(time.monotonic() - since) % 60 < 3:
                log.error("%s: previous instance still exiting after %.0fs (pids %s)", c.name,
                          time.monotonic() - since, [p for p in c.old_pids if winsys.pid_alive(p)])
            return False
        self._close_job(c)
        c.stopping_since = None
        c.state = "backoff" if c.restart_pending else "stopped"
        if not c.restart_pending:
            c.next_try = time.monotonic() + 1e12
        log.info("%s: previous instance gone", c.name)
        return True

    def stop_child(self, c: Child, restart: bool = False) -> None:
        if c.popen is None or c.popen.poll() is not None:
            c.popen = None
            if c.job is not None and c.job.active():
                c.job.terminate()
            return
        pid = c.popen.pid
        procs = winsys.process_list()
        c.old_pids = sorted(set(winsys.process_tree(pid, procs)) | (set(c.job.pids()) if c.job is not None else set()))
        if c.stop_file is not None:
            try:
                c.stop_file.write_text("supervisor", encoding="utf-8")
            except OSError:
                pass
        # incident 2026-10-24: children with a STOP file (worker, filler, realtime) get NO CTRL+BREAK - it reaches
        # their whole process group (pool workers, job processes) and the console default handler's ExitProcess
        # left processes stuck in their exit for hours. After the grace the job is killed externally instead.
        if c.stop_file is None:
            try:
                if winsys.IS_WINDOWS:
                    os.kill(pid, signal.CTRL_BREAK_EVENT)
                else:
                    c.popen.send_signal(signal.SIGINT)
            except OSError:
                pass
        t_stop = time.monotonic()
        try:
            c.popen.wait(c.grace_s)
        except subprocess.TimeoutExpired:
            # alert hygiene 2026-10-30: the child did not finish its own (deterministic) exit within the grace ->
            # terminate its whole Job Object (external kill: always completes, unlike a console-handler exit)
            log.warning("%s did not stop within %.0fs: terminating its job object", c.name, c.grace_s)
            self._kill(c, pid)
            try:
                c.popen.wait(15)
            except subprocess.TimeoutExpired:
                pass
        c.last_exit = c.popen.returncode
        c.popen = None
        if c.job is not None:
            live = len(c.job.live_pids())     # members in their exit (conhost, a process freeing memory) are not
            c.job.terminate()                 # leftovers: descendants that outlived the root (pool workers, ...)
            if live:
                log.warning("%s: %d leftover process(es) of the stopped instance killed", c.name, live)
            c.job.wait_empty(STOP_POLL_S)
        self._wait_teardown(c, TEARDOWN_WAIT_S)   # members in their exit: usually done within a second
        if c.stop_file is not None and c.stop_file.exists():
            try:
                c.stop_file.unlink()
            except OSError:
                pass
        self._pidfile(c, None)
        c.history = ([{"exit": "stop", "code": c.last_exit, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                       "stop_s": round(time.monotonic() - t_stop, 1)}] + c.history)[:20]
        c.restart_pending = restart
        c.next_try = time.monotonic() + (5 if restart else 1e12)
        c.stopping_since = time.monotonic()  # teardown clock: members still in their exit block a restart a while
        if self._gone(c):
            self._close_job(c)
            c.state, c.stopping_since = ("backoff" if restart else "stopped"), None
        else:
            c.state = "stopping"
            log.warning("%s: previous instance still exiting - no restart until it is gone", c.name)

    # ------------------------------------------------------------------ housekeeping
    def housekeeping(self, force: bool = False) -> None:
        """Every REAP_EVERY_S: orphan reaper + stray API instances."""
        if not self.housekeeping_enabled:
            return
        if not force and time.monotonic() - self._last_reap < REAP_EVERY_S:
            return
        self._last_reap = time.monotonic()
        from app.ops import reaper  # noqa: PLC0415

        st = self.reaper_stats
        try:
            rep = reaper.reap(self.s.project_root)
            st["runs"] += 1
            st["last_run_at"] = rep["at"]
            st["leaked_now"] = rep["leaked"]
            owners = self._job_owners()
            for z in rep.get("zombies", []) + rep.get("exiting", []):
                z["owner"] = owners.get(int(z.get("pid") or 0)) or owners.get(int(z.get("ppid") or 0))
            st["zombies"], st["zombie_mb"] = rep.get("zombies", [])[:10], rep.get("zombie_mb", 0)
            st["exiting"] = rep.get("exiting", [])[:10]
            st["killed_total"] += len(rep["killed"])
            st["freed_mb_total"] += rep["freed_mb"]
            if rep["killed"]:
                st["last_killed"] = ([{"at": rep["at"], "pids": rep["killed"], "freed_mb": rep["freed_mb"],
                                       "cmd": [i["cmd"][:120] for i in rep["items"]][:5]}] + st["last_killed"])[:10]
            if self.api_port and STRAY_API_POLICY != "off" and any(c.port is not None for c in self.children):
                strays = reaper.find_stray_apis(self._owned_pids(), self.api_port, self.s.project_root)
                keep = []
                for x in strays:
                    if STRAY_API_POLICY == "all" or x["ours_port"] or x["orphaned"]:
                        self._kill_stray(x, "on our port" if x["ours_port"] else "orphaned" if x["orphaned"] else "policy all")
                    else:
                        keep.append(x)
                        if x["pid"] not in self._warned_stray:
                            self._warned_stray.add(x["pid"])
                            log.warning("unowned API instance pid %s on port %s (%s MB, parent alive) left running "
                                        "(BIG360_SUPERVISOR_STRAY_API=all would kill it)", x["pid"], x["port"], x["rss_mb"])
                st["stray_apis"] = [{k: x[k] for k in ("pid", "port", "rss_mb", "orphaned")} for x in keep][:10]
            st["error"] = None
        except Exception as e:  # noqa: BLE001 - housekeeping never breaks supervision
            st["error"] = f"{type(e).__name__}: {e}"[:300]
            log.exception("supervisor housekeeping failed")

    def _job_owners(self) -> dict[int, str]:
        """pid -> owning child name (its Job Object / old pids), `sync` for the startup sync's job."""
        out: dict[int, str] = {}
        for c in self.children:
            try:
                for p in (c.job.pids() if c.job is not None else []):
                    out[int(p)] = c.name
            except Exception:  # noqa: BLE001
                pass
            for p in c.old_pids:
                out.setdefault(int(p), c.name)
        job = getattr(self.sync_trigger, "job", None)
        if job is not None:
            try:
                for p in job.pids():
                    out.setdefault(int(p), "sync")
            except Exception:  # noqa: BLE001
                pass
        return out

    def alerts(self) -> list[dict[str, Any]]:
        out = [dict(c.alert, child=c.name) for c in self.children if c.alert]
        rs = self.reaper_stats
        if rs.get("last_killed") and rs["last_killed"][0]["at"] >= (datetime.now(UTC) - _td(hours=6)).isoformat():
            out.append({"id": "leaked_processes", "severity": "warn", "child": None,
                        "killed_6h": sum(len(x["pids"]) for x in rs["last_killed"]
                                         if x["at"] >= (datetime.now(UTC) - _td(hours=6)).isoformat()),
                        "last_reason": "orphaned multiprocessing children reaped"})
        return out

    # ------------------------------------------------------------------ loop
    def status(self) -> dict[str, Any]:
        out = {"pid": os.getpid(), "started_at": self.started_at, "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "api": f"http://{self.api_host}:{self.api_port}", "app_mode": self.app_mode,
               "ui": f"http://{self.api_host}:{self.api_port}/" if self.ui_dir else None,
               "ui_dir": str(self.ui_dir) if self.ui_dir else None,
               "credentials_loaded": len(self.extra_env), "children": {}}
        now = time.time()
        for c in self.children:
            if c.job is not None and c.pid:
                mem = c.job.memory_mb()
            else:
                mem = winsys.tree_memory_mb(c.pid) if c.pid else None
            out["children"][c.name] = {"state": c.state, "pid": c.pid, "restarts": c.restarts, "last_exit": c.last_exit,
                                       "started_at": c.started_at_iso, "command": c.command[1:],
                                       "rss_mb": round(mem) if mem else None, "log": str(c.log_path) if c.log_path else None,
                                       "history": c.history[:5], "ram_cap_mb": c.ram_cap_mb, "job_mem_mb": c.job_mem_mb,
                                       "enabled": (not c.toggle.exists()) if c.toggle is not None else True,
                                       "ready": c.ready if c.port is not None else None,
                                       "health_fail": c.health_fail if c.port is not None else None,
                                       "last_health_ok_at": c.last_health_ok_at,
                                       "restarts_30m": c.restarts_within(ALERT_WINDOW_S, now),
                                       "restart_reasons": c.reasons[:10], "job_object": c.job is not None,
                                       "job_processes": c.job.active() if c.job is not None else None,
                                       "alert": c.alert}
        out["restarts_total"] = sum(c.restarts for c in self.children)
        out["reaper"] = dict(self.reaper_stats)
        out["alerts"] = self.alerts()
        if self.sync_trigger is not None:
            out["sync"] = self.sync_trigger.status()
        return out

    def _api_ready(self) -> bool:
        """The startup sync waits for the dashboard API (no API child: ready at once)."""
        api = next((c for c in self.children if c.name == "api"), None)
        return api is None or api.state == "external" or (api.state == "running" and api.ready)

    def write_status(self) -> None:
        write_json_atomic(self.logs / "supervisor.json", self.status())

    def run(self) -> int:
        try:
            self._lock.acquire(timeout=0)
        except Timeout:
            print("supervisor already running", file=sys.stderr)
            return 2
        (self.logs / "supervisor.pid").write_text(str(os.getpid()), encoding="utf-8")
        stop_file = ops_state_dir(self.s) / "supervisor.STOP"
        if stop_file.exists():
            stop_file.unlink()
        log.info("supervisor starting pid=%d children=%s", os.getpid(), [c.name for c in self.children])
        try:
            for c in self.children:
                self.start_child(c)
            last_status = 0.0
            while not self.stop_event.is_set():
                if stop_file.exists():
                    log.info("supervisor.STOP present - stopping")
                    stop_file.unlink(missing_ok=True)
                    break
                for c in self.children:
                    try:
                        self.check_child(c)
                    except Exception:
                        log.exception("supervisor check of %s failed", c.name)
                try:
                    self.housekeeping()
                except Exception:
                    log.exception("supervisor housekeeping failed")
                if self.sync_trigger is not None:
                    try:
                        self.sync_trigger.tick(api_ready=self._api_ready())
                    except Exception:
                        log.exception("supervisor sync trigger failed")
                if time.monotonic() - last_status > 10:
                    self.write_status()
                    last_status = time.monotonic()
                self.stop_event.wait(2)
        finally:
            self.shutdown()
        return 0

    def shutdown(self) -> None:
        log.info("supervisor stopping children")
        if self.sync_trigger is not None:
            try:
                self.sync_trigger.stop()
            except Exception:
                log.exception("stopping the sync failed")
        threads = [threading.Thread(target=self.stop_child, args=(c,), daemon=True) for c in reversed(self.children)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(400)
        for c in self.children:   # whatever survived the graceful stop dies with its job
            if c.job is not None:
                left = c.job.terminate()
                if left:
                    log.warning("%s: %d process(es) still in its job at shutdown: killed", c.name, left)
                c.job.close()
                c.job = None
        self.write_status()
        for p in (self.logs / "supervisor.pid",):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self._lock.release()
        except Exception:
            pass
        log.info("supervisor stopped")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.ops.supervisor", description="BiG Equity 360 supervisor")
    ap.add_argument("--api-host", default=os.environ.get("BIG360_API_HOST", "127.0.0.1"))
    ap.add_argument("--api-port", type=int, default=int(os.environ.get("BIG360_SUPERVISOR_API_PORT", "8010")))
    ap.add_argument("--no-api", action="store_true")
    ap.add_argument("--no-worker", action="store_true")
    ap.add_argument("--no-filler", action="store_true")
    ap.add_argument("--no-studio", action="store_true", help="no warm studio lane (Studio / backtest runs use the worker)")
    ap.add_argument("--realtime", action="store_true", help="worker also runs the realtime supervisor (R1)")
    ap.add_argument("--live-news", action=argparse.BooleanOptionalAction, default=None,
                    help="run the realtime / live news service as a 4th child (default: on in app mode)")
    ap.add_argument("--live-quotes", action=argparse.BooleanOptionalAction, default=None,
                    help="run the delayed quotes service as a 5th child (default: on in app mode)")
    ap.add_argument("--no-schedule", action="store_true", help="worker consumes the job queue only (no cron runs)")
    ap.add_argument("--sync", action=argparse.BooleanOptionalAction, default=None,
                    help="startup sync: fetch what is missing after start, every 30 min and on request "
                         "(default: on unless --no-schedule; config/ops.yaml sync, env BIG360_SYNC=0)")
    ap.add_argument("--app", action="store_true", default=os.environ.get("BIG360_APP", "") == "1",
                    help="app mode (tray / BiG360.exe): --serve-ui on by default")
    ap.add_argument("--serve-ui", action=argparse.BooleanOptionalAction, default=None,
                    help="API also serves the built UI at / (default: on in app mode)")
    ap.add_argument("--ui-dir", default=None, help="built UI folder (default: app_dist / frontend/dist_app)")
    ap.add_argument("--no-credentials", action="store_true",
                    help="do not pass API keys from Windows Credential Manager to the children")
    ap.add_argument("--status", action="store_true", help="print the last supervisor status and exit")
    a = ap.parse_args(argv)
    s = get_settings()
    if a.status:
        print(json.dumps(read_json(app_logs_dir(s) / "supervisor.json", {}), indent=1))
        return 0
    s.ensure_dirs()
    setup_logging(log_file="supervisor.log", service="supervisor")
    serve_ui = a.app if a.serve_ui is None else a.serve_ui
    extra_env: dict[str, str] = {}
    if not a.no_credentials:
        try:
            from app.desktop.credentials import load_into_env

            names = load_into_env(extra_env)
            if names:
                log.info("API keys from Windows Credential Manager: %d (%s)", len(names), ", ".join(names))
        except Exception as e:  # never block the start on the credential store
            log.warning("credential store unavailable (%s): using .env / environment only", type(e).__name__)
    sup = Supervisor(s, a.api_host, a.api_port, api=not a.no_api, worker=not a.no_worker, filler=not a.no_filler,
                     realtime=a.realtime, serve_ui=serve_ui, ui_dir=Path(a.ui_dir) if a.ui_dir else None,
                     schedule=not a.no_schedule, extra_env=extra_env, app_mode=a.app,
                     live_news=a.app if a.live_news is None else a.live_news,
                     live_quotes=a.app if a.live_quotes is None else a.live_quotes,
                     sync=(not a.no_schedule) if a.sync is None else a.sync,
                     studio=not (a.no_studio or a.no_api or os.environ.get("BIG360_STUDIO_LANE", "1") == "0"))

    def handler(signum: int, _frame: Any) -> None:
        log.info("signal %s", signum)
        sup.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    return sup.run()


if __name__ == "__main__":
    raise SystemExit(main())
