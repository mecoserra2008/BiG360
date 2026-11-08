"""Schedule profiles (config/schedule.yaml) -> APScheduler triggers, catch-up rule and the refresh chain.

    cfg = load_config()                               # profiles + defaults
    pid, source = active_profile()                    # env BIG360_SCHEDULE_PROFILE > data/state/schedule.json > default
    entries = profile_entries(pid, registry)          # [Entry(id, kind, dag | steps, cron, tz, ...)]
    set_active_profile("daily", by="api")             # persisted; the worker hot-reloads it
    due = catchup_due(entries, completed_runs(...))   # entries whose last scheduled time was missed
    run_entry(entry, runner)                          # one DAG, or the chain nightly -> weekly -> monthly_if_due

The worker (app.scheduler.worker) owns the APScheduler instance; this module is pure logic + small state files so
it is unit-testable and usable by the API without a running worker.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.paths import config_dir, ops_state_dir, read_json, read_yaml, write_json_atomic

log = get_logger("app.ops.schedule")

PROFILE_IDS = ("weekly", "daily", "intraday")
#: roadmap v3 N3: the live news compaction runs hourly in every profile, independent of the refresh entries; quiet
#: (no tray toast per run), no wake timer, forced past the runner's run-key idempotency (each run is a new hour)
HOURLY_ENTRY: dict[str, Any] = {"id": "hourly_news", "kind": "dag", "dag": "hourly", "cron": "10 * * * *",
                                "timezone": "UTC", "notify": False, "wake": False, "force": True,
                                "catch_up": False, "misfire_grace_h": 0.5}
CHAIN_STEPS = ("nightly", "weekly", "monthly", "monthly_if_due", "cftc_weekly")
DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1, "default_profile": "weekly", "timezone": "Europe/Lisbon", "catchup_check_min": 15,
    "monthly_min_days": 28,
    "profiles": {
        "weekly": {"label": "Weekly", "period_days": 7, "sla_days": 9, "max_catchup_days": 6, "misfire_grace_h": 144,
                   "entries": [{"id": "weekly_refresh", "kind": "chain", "steps": ["nightly", "weekly", "monthly_if_due"],
                                "cron": "0 2 * * 6", "timezone": "Europe/Lisbon",
                                "params": {"nightly": {"max_days": 10}}},
                               {"id": "cftc_weekly", "kind": "dag", "dag": "cftc_weekly", "cron": "0 19 * * 5",
                                "timezone": "America/New_York"}, HOURLY_ENTRY]},
        "daily": {"label": "Daily", "period_days": 1, "sla_days": 3, "max_catchup_days": 2, "misfire_grace_h": 20,
                  "entries": [{"id": d, "kind": "dag", "dag": d, "cron": "jobs.yaml"} for d in ("nightly", "weekly", "monthly")]
                  + [{"id": "cftc_weekly", "kind": "dag", "dag": "cftc_weekly", "cron": "0 19 * * 5",
                      "timezone": "America/New_York"}, HOURLY_ENTRY]},
        "intraday": {"label": "Intraday (placeholder)", "placeholder": True, "base": "daily"},
    },
}


class ScheduleError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    id: str
    kind: str                                   # dag | chain
    cron: str
    tz: str
    dag: str | None = None
    steps: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    catch_up: bool = True
    max_catchup_days: float = 2.0
    misfire_grace_s: int = 6 * 3600
    description: str = ""
    notify: bool = True                         # tray toast when a run finishes
    wake: bool = True                           # arm the wake timer for this entry's next fire time
    force: bool = False                         # bypass the runner's run-key idempotency (frequent entries)

    @property
    def dags(self) -> tuple[str, ...]:
        """DAGs this entry runs (monthly_if_due -> monthly)."""
        if self.kind == "dag":
            return (self.dag,) if self.dag else ()
        return tuple("monthly" if s == "monthly_if_due" else s for s in self.steps)

    @property
    def required_dags(self) -> tuple[str, ...]:
        """DAGs that must have completed after the scheduled time for the entry to count as done."""
        if self.kind == "dag":
            return self.dags
        return tuple(s for s in self.steps if s != "monthly_if_due")

    def trigger(self) -> Any:
        from app.scheduler.runner import cron_trigger

        return cron_trigger(self.cron, ZoneInfo(self.tz))

    def next_fire(self, now: datetime | None = None) -> datetime | None:
        now = now or datetime.now(UTC)
        return self.trigger().get_next_fire_time(None, now.astimezone(ZoneInfo(self.tz)))

    def previous_fire(self, now: datetime | None = None, lookback_days: float = 40.0) -> datetime | None:
        """Most recent scheduled time <= now (iterates forward from now - lookback)."""
        now = now or datetime.now(UTC)
        tz = ZoneInfo(self.tz)
        trig = self.trigger()
        t = trig.get_next_fire_time(None, (now - timedelta(days=lookback_days)).astimezone(tz))
        prev = None
        n = 0
        while t is not None and t <= now and n < 10000:
            prev = t
            t = trig.get_next_fire_time(t, t + timedelta(seconds=1))
            n += 1
        return prev

    def to_dict(self, now: datetime | None = None) -> dict[str, Any]:
        nf = self.next_fire(now)
        return {"id": self.id, "kind": self.kind, "dags": list(self.dags), "steps": list(self.steps), "cron": self.cron,
                "timezone": self.tz, "catch_up": self.catch_up, "description": self.description,
                "next_run": nf.isoformat() if nf else None}


# ------------------------------------------------------------------------------------------------ config


def load_config(settings: Settings | None = None) -> dict[str, Any]:
    doc = read_yaml(config_dir(settings) / "schedule.yaml")
    if not doc.get("profiles"):
        return DEFAULT_CONFIG
    out = {**DEFAULT_CONFIG, **{k: v for k, v in doc.items() if k != "profiles"}}
    out["profiles"] = dict(doc["profiles"])
    return out


def _state_path(settings: Settings | None = None):
    return (settings or get_settings()).state_dir / "schedule.json"


def active_profile(settings: Settings | None = None) -> tuple[str, str]:
    """(profile_id, source) with source env | state | default."""
    cfg = load_config(settings)
    env = os.environ.get("BIG360_SCHEDULE_PROFILE")
    if env and env in cfg["profiles"]:
        return env, "env"
    st = read_json(_state_path(settings), {}) or {}
    if st.get("profile") in cfg["profiles"]:
        return st["profile"], "state"
    return str(cfg.get("default_profile", "weekly")), "default"


def schedule_state(settings: Settings | None = None) -> dict[str, Any]:
    return read_json(_state_path(settings), {}) or {}


def set_active_profile(profile: str, settings: Settings | None = None, by: str = "api") -> dict[str, Any]:
    cfg = load_config(settings)
    if profile not in cfg["profiles"]:
        raise ScheduleError(f"unknown profile {profile!r} (known: {sorted(cfg['profiles'])})")
    st = schedule_state(settings)
    prev = st.get("profile")
    st.update(profile=profile, updated_at=datetime.now(UTC).isoformat(timespec="seconds"), updated_by=by)
    if prev != profile:
        st.setdefault("history", []).append({"from": prev, "to": profile, "at": st["updated_at"], "by": by})
        st["history"] = st["history"][-20:]
    write_json_atomic(_state_path(settings), st)
    log.info("schedule profile set to %s (was %s) by %s", profile, prev, by)
    return st


def profile_spec(profile: str, settings: Settings | None = None) -> dict[str, Any]:
    cfg = load_config(settings)
    p = dict(cfg["profiles"].get(profile) or {})
    if not p:
        raise ScheduleError(f"unknown profile {profile!r}")
    if p.get("base") and not p.get("entries"):
        base = dict(cfg["profiles"].get(p["base"]) or {})
        base.update({k: v for k, v in p.items() if k not in ("entries",)})
        p = base
    return p


def profile_entries(profile: str, registry: Any = None, settings: Settings | None = None) -> list[Entry]:
    """Entries of a profile with crons resolved (cron: jobs.yaml -> the DAG's jobs.yaml cron, interpreted in the
    worker's scheduler timezone exactly like before profiles existed)."""
    s = settings or get_settings()
    cfg = load_config(s)
    p = profile_spec(profile, s)
    default_tz = str(cfg.get("timezone") or "Europe/Lisbon")
    grace = int(float(p.get("misfire_grace_h", 6)) * 3600)
    max_cu = float(p.get("max_catchup_days", 2))
    out: list[Entry] = []
    for e in p.get("entries") or []:
        kind = e.get("kind", "dag")
        cron = str(e.get("cron") or "")
        tz = str(e.get("timezone") or default_tz)
        if cron == "jobs.yaml":
            if registry is None:
                from app.scheduler.runner import JobRegistry

                registry = JobRegistry.load(settings=s)
            cron = str((registry.dags.get(e.get("dag")) or {}).get("cron") or "")
            if not e.get("timezone"):
                from app.scheduler.runner import scheduler_tz

                tz = str(scheduler_tz(s, registry))
        if not cron:
            continue
        steps = tuple(e.get("steps") or ())
        bad = [x for x in steps if x not in CHAIN_STEPS]
        if bad:
            raise ScheduleError(f"{profile}.{e.get('id')}: unknown chain steps {bad}")
        if kind not in ("dag", "chain"):
            raise ScheduleError(f"{profile}.{e.get('id')}: unknown kind {kind!r}")
        out.append(Entry(id=str(e["id"]), kind=kind, cron=cron, tz=tz, dag=e.get("dag"), steps=steps,
                         params=dict(e.get("params") or {}), catch_up=bool(e.get("catch_up", True)),
                         max_catchup_days=float(e.get("max_catchup_days", max_cu)),
                         misfire_grace_s=int(float(e.get("misfire_grace_h", grace / 3600)) * 3600),
                         description=str(e.get("description") or ""), notify=bool(e.get("notify", True)),
                         wake=bool(e.get("wake", True)), force=bool(e.get("force", False))))
    return out


def next_runs_by_dag(entries: list[Entry], now: datetime | None = None) -> dict[str, str | None]:
    """{dag: earliest next fire across entries that run it} (a chain's later steps share the chain's time)."""
    out: dict[str, datetime] = {}
    for e in entries:
        nf = e.next_fire(now)
        if nf is None:
            continue
        for d in e.dags:
            if d not in out or nf < out[d]:
                out[d] = nf
    return {d: t.isoformat() for d, t in out.items()}


# ------------------------------------------------------------------------------------------------ completed runs


def completed_dag_runs(job_runs: Any) -> dict[str, dict[str, Any]]:
    """{dag: {dag_run_id, started_at, finished_at, status, trigger}} of the latest *completed* run per DAG. A run is
    completed when none of its jobs is still running and at least one job finished success / partial (a run where
    everything failed or was skipped does not count). Input: job_runs as a Polars frame (or rows)."""
    import polars as pl

    df = job_runs if isinstance(job_runs, pl.DataFrame) else pl.DataFrame(list(job_runs or []))
    if df.is_empty() or "dag_run_id" not in df.columns:
        return {}
    df = df.filter(pl.col("dag_run_id").is_not_null())
    if df.is_empty():
        return {}
    latest = df.sort("started_at").group_by(["dag_run_id", "job_id"], maintain_order=True).last()
    g = latest.group_by("dag_run_id").agg(
        pl.col("started_at").min().alias("started_at"), pl.col("finished_at").max().alias("finished_at"),
        (pl.col("status") == "running").sum().alias("n_running"),
        pl.col("status").is_in(["success", "partial"]).sum().alias("n_ok"),
        (pl.col("status") == "failed").sum().alias("n_failed"), pl.len().alias("n"),
        pl.col("trigger").first().alias("trigger"))
    g = g.filter((pl.col("n_running") == 0) & (pl.col("n_ok") > 0)).with_columns(
        pl.col("dag_run_id").str.split("-").list.last().alias("dag"))
    out: dict[str, dict[str, Any]] = {}
    for r in g.sort("finished_at").iter_rows(named=True):
        out[r["dag"]] = {"dag_run_id": r["dag_run_id"], "started_at": r["started_at"], "finished_at": r["finished_at"],
                         "status": "success" if r["n_failed"] == 0 else "partial", "trigger": r["trigger"],
                         "jobs": r["n"], "jobs_failed": r["n_failed"]}
    return out


def _aware(t: Any) -> datetime | None:
    if t is None:
        return None
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def entry_last_done(entry: Entry, completed: dict[str, dict[str, Any]]) -> datetime | None:
    """When the entry last completed = the oldest finish time among its required DAGs (None if any never ran)."""
    times = [_aware((completed.get(d) or {}).get("finished_at")) for d in entry.required_dags]
    if not times or any(t is None for t in times):
        return None
    return min(times)  # type: ignore[type-var]


def catchup_due(entries: list[Entry], completed: dict[str, dict[str, Any]], now: datetime | None = None,
                running: set[str] | None = None) -> list[tuple[Entry, datetime]]:
    """[(entry, missed_scheduled_time)] for entries whose most recent scheduled time passed without a completed run
    finishing after it, within max_catchup_days."""
    now = now or datetime.now(UTC)
    out = []
    for e in entries:
        if not e.catch_up or (running and e.id in running):
            continue
        prev = e.previous_fire(now, lookback_days=max(8.0, e.max_catchup_days + 1))
        if prev is None or (now - prev) > timedelta(days=e.max_catchup_days):
            continue
        done = entry_last_done(e, completed)
        if done is None or done < prev:
            out.append((e, prev))
    return out


def monthly_due(completed: dict[str, dict[str, Any]], now: datetime | None = None, min_days: float = 28.0) -> bool:
    now = now or datetime.now(UTC)
    last = _aware((completed.get("monthly") or {}).get("finished_at"))
    return last is None or (now - last) >= timedelta(days=min_days)


# ------------------------------------------------------------------------------------------------ execution


def _runs_path(settings: Settings | None = None):
    return ops_state_dir(settings) / "schedule_runs.json"


_RUNS_LOCK = threading.Lock()


def record_entry_run(entry_id: str, info: dict[str, Any], settings: Settings | None = None) -> None:
    with _RUNS_LOCK:
        _record_entry_run(entry_id, info, settings)


def _record_entry_run(entry_id: str, info: dict[str, Any], settings: Settings | None = None) -> None:
    p = _runs_path(settings)
    st = read_json(p, {}) or {}
    cur = st.setdefault(entry_id, {})
    cur.update(info)
    if info.get("status") in ("success", "partial"):
        cur["last_success_at"] = info.get("finished_at")
    hist = cur.setdefault("history", [])
    if info.get("finished_at"):
        hist.append({k: info.get(k) for k in ("started_at", "finished_at", "status", "trigger", "dags")})
        cur["history"] = hist[-30:]
    write_json_atomic(p, st)


def entry_runs(settings: Settings | None = None) -> dict[str, Any]:
    return read_json(_runs_path(settings), {}) or {}


def run_entry(entry: Entry, runner: Any, trigger: str = "schedule", completed_fn: Callable[[], dict] | None = None,
              settings: Settings | None = None, keep_awake: Any = None) -> dict[str, Any]:
    """Execute an entry with the JobRunner. A chain runs its steps in order; monthly_if_due consults completed runs.
    Returns {"status", "dags": {dag: status}, ...}; records data/state/ops/schedule_runs.json."""
    from app.scheduler.runner import RunOptions

    s = settings or get_settings()
    cfg = load_config(s)
    started = datetime.now(UTC)
    record_entry_run(entry.id, {"started_at": started.isoformat(timespec="seconds"), "status": "running",
                                "trigger": trigger, "finished_at": None, "notify": entry.notify}, s)
    if keep_awake is not None:
        keep_awake.acquire(f"schedule:{entry.id}")
    statuses: dict[str, str] = {}
    try:
        steps = entry.steps if entry.kind == "chain" else (entry.dag,)
        for step in steps:
            dag = "monthly" if step == "monthly_if_due" else step
            if step == "monthly_if_due":
                comp = completed_fn() if completed_fn else {}
                if not monthly_due(comp, min_days=float(cfg.get("monthly_min_days", 28))):
                    statuses[dag] = "not_due"
                    continue
            if runner.stop_event.is_set():
                statuses[dag] = "stopped"
                break
            params = dict((entry.params or {}).get(dag) or {}) if entry.kind == "chain" else dict(entry.params or {})
            log.info("schedule %s: running dag %s params=%s", entry.id, dag, params)
            rep = runner.run_dag(dag, RunOptions(trigger=trigger, params=params, force=entry.force))
            statuses[dag] = rep.status
            log.info("schedule %s: dag %s finished %s\n%s", entry.id, dag, rep.status, rep.summary())
    except Exception as e:  # pragma: no cover - defensive: never kill the scheduler thread
        log.exception("schedule entry %s crashed", entry.id)
        statuses["error"] = f"{type(e).__name__}: {e}"
    finally:
        if keep_awake is not None:
            keep_awake.release(f"schedule:{entry.id}")
    ran = {k: v for k, v in statuses.items() if v not in ("not_due",)}
    if "error" in statuses or not ran:
        status = "failed"
    elif all(v == "success" for v in ran.values()):
        status = "success"
    elif any(v in ("success", "partial") for v in ran.values()):
        status = "partial"
    else:
        status = "failed"
    out = {"status": status, "dags": statuses, "started_at": started.isoformat(timespec="seconds"),
           "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "trigger": trigger, "notify": entry.notify}
    record_entry_run(entry.id, out, s)
    return out


# ------------------------------------------------------------------------------------------------ API summary


def schedule_summary(settings: Settings | None = None, heartbeat: dict[str, Any] | None = None,
                     registry: Any = None, completed: dict[str, dict[str, Any]] | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """GET /system/schedule payload (no worker needed: next runs are computed from the profile)."""
    s = settings or get_settings()
    cfg = load_config(s)
    now = now or datetime.now(UTC)
    active, source = active_profile(s)
    st = schedule_state(s)
    hb = heartbeat or {}
    applied = hb.get("schedule_profile") if hb.get("alive") else None
    profiles = []
    for pid in cfg["profiles"]:
        try:
            spec = profile_spec(pid, s)
            ents = profile_entries(pid, registry, s)
        except Exception as e:  # pragma: no cover - surfaced, never raised
            spec, ents = {"label": pid, "description": f"invalid: {e}"}, []
        profiles.append({"id": pid, "label": spec.get("label", pid), "description": spec.get("description", ""),
                         "placeholder": bool(spec.get("placeholder")), "period_days": spec.get("period_days"),
                         "sla_days": spec.get("sla_days"), "entries": [e.to_dict(now) for e in ents]})
    ents = profile_entries(active, registry, s)
    comp = completed or {}
    runs = entry_runs(s)
    due = [e.id for e, _ in catchup_due(ents, comp, now)] if comp is not None else []
    return {
        "active": active, "source": source, "default": cfg.get("default_profile"),
        "updated_at": st.get("updated_at"), "updated_by": st.get("updated_by"),
        "worker_profile": applied, "worker_alive": bool(hb.get("alive")),
        "scheduler_enabled": bool(hb.get("alive") and hb.get("scheduler_enabled")),
        "pending_apply": bool(hb.get("alive") and hb.get("scheduler_enabled") and applied not in (None, active)),
        "timezone": cfg.get("timezone"), "profiles": profiles,
        "next_runs": next_runs_by_dag(ents, now),
        "entries": [{**e.to_dict(now), "last_success_at": (runs.get(e.id) or {}).get("last_success_at"),
                     "last_status": (runs.get(e.id) or {}).get("status"), "catchup_due": e.id in due}
                    for e in ents],
        "sla_days": profile_spec(active, s).get("sla_days"),
        "period_days": profile_spec(active, s).get("period_days"),
    }
