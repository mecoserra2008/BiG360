"""Startup sync caps (coverage v3 A3, 2026-11): a wall-clock / job-count budget per sync, per-job timeouts from run
history, and the carry-over of what a sync deferred to the next one.

Evidence (docs/audits/APP_MAP_AND_GAPS_2026-11.md section 3 #4): interval syncs on 11-01, 11-03 and 11-04 ran 39.1 h,
39.4 h and 33.2 h (52 datasets due, 23 failed jobs). Jobs waited or ran far past their 30-min cadence: e.g.
earnings_dates_fetch 1,468 min and fundamentals_normalize 1,422 min (both TimeoutError, killed together when the
laptop woke up), cmf_cl_filings / afm_nl_filings ~180 min (TimeoutError), qa_regression 1,554 min.

Rules (config/ops.yaml `sync:` keys below; env BIG360_SYNC_CAPS=0 turns every cap and history timeout off and
restores the pre-v3 behaviour - the no-overlap lock always stays on):
* budget per sync: max_wall_min (interval / manual syncs, default 120) or startup_max_wall_min (the first sync after
  the app starts, default 240), measured on the WALL clock (time.time(): hours a suspended laptop slept count, the
  monotonic timers of a sleeping process do not help there), and pass_max_jobs (default 45) jobs started per pass.
  When the budget is gone nothing new starts: running jobs finish (bounded by their timeouts), pending nodes are
  skipped with reason "deferred: ..." and carried to the next sync; pass 2 is then deferred whole; the publish of what
  ran still happens (that is what makes fetched data visible), the QA guard / dq_checks are deferred and run in the
  next sync even when it publishes nothing (their carry entry: the live snapshot still needs its checks), exempt
  from that sync's budget so they are never starved;
* a job is not started when its expected duration (p95 of its history) exceeds what is left of the budget - it is
  carried instead (carried jobs, fetch jobs included, are exempt from this rule and go first next time, so a long job
  is never starved); a job whose p95 exceeds the WHOLE budget can never fit and is started instead of deferred;
* per-job timeout: p95 of the job's successful / partial durations (last timeout_history_days days, at least
  timeout_min_samples runs) x timeout_p95_factor (1.5), clamped to [timeout_floor_min (10), max(timeout_ceiling_min
  (180), contract timeout)]; fewer samples -> the contract timeout_min. Only comparable runs count (comparable_run:
  no back-fill trigger, no one-off param such as `since` / `full` / an entity list, no `max_*` work bound above the
  contract default). Escalation: the latest run timed out and the history timeout would kill it there again -> twice
  the timeout that killed it (at least the contract, at most the ceiling), so a job whose work grew is not killed
  every time;
* carry-over: data/state/ops/sync_carry.json lists the deferred jobs (job, pass, reason, datasets, times carried);
  the next sync adds the derive jobs among them to their pass with priority, marks the fetch jobs among them that the
  scan re-planned (a dataset still due is still due) as carried, runs the deferred post-publish guards, and drops
  entries older than carry_max_age_h (24 h).
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import Settings
from app.ops.paths import ops_state_dir, read_json, write_json_atomic

DEFAULTS: dict[str, Any] = {
    "max_wall_min": 120.0,
    "startup_max_wall_min": 240.0,
    "pass_max_jobs": 45,
    "timeout_p95_factor": 1.5,
    "timeout_floor_min": 10.0,
    "timeout_ceiling_min": 180.0,
    "timeout_history_days": 30,
    "timeout_min_samples": 3,
    "carry_max_age_h": 24.0,
    # stale-lock recovery (app.ops.sync.state.lock_status / trigger): the sync heartbeats every 5 s
    "stale_heartbeat_s": 900.0,
    "stale_confirm_s": 120.0,
    "hard_max_h": 8.0,
}


def enabled() -> bool:
    return os.environ.get("BIG360_SYNC_CAPS", "1") != "0"


def _num(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


@dataclass
class Caps:
    enabled: bool = True
    max_wall_min: float = DEFAULTS["max_wall_min"]
    pass_max_jobs: int = DEFAULTS["pass_max_jobs"]
    p95_factor: float = DEFAULTS["timeout_p95_factor"]
    floor_min: float = DEFAULTS["timeout_floor_min"]
    ceiling_min: float = DEFAULTS["timeout_ceiling_min"]
    history_days: int = DEFAULTS["timeout_history_days"]
    min_samples: int = DEFAULTS["timeout_min_samples"]
    carry_max_age_h: float = DEFAULTS["carry_max_age_h"]
    stale_heartbeat_s: float = DEFAULTS["stale_heartbeat_s"]
    stale_confirm_s: float = DEFAULTS["stale_confirm_s"]
    hard_max_h: float = DEFAULTS["hard_max_h"]

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "max_wall_min": self.max_wall_min, "pass_max_jobs": self.pass_max_jobs,
                "timeout_p95_factor": self.p95_factor, "timeout_floor_min": self.floor_min,
                "timeout_ceiling_min": self.ceiling_min}


def load_caps(cfg: dict[str, Any] | None = None, trigger: str | None = None) -> Caps:
    """Caps from the ops.yaml `sync:` block (`cfg`), env overrides BIG360_SYNC_MAX_WALL_MIN / BIG360_SYNC_PASS_MAX_JOBS,
    kill switch BIG360_SYNC_CAPS=0."""
    c = {**DEFAULTS, **(cfg or {})}
    wall = c["startup_max_wall_min"] if trigger == "startup" else c["max_wall_min"]
    wall = _num(os.environ.get("BIG360_SYNC_MAX_WALL_MIN", wall), DEFAULTS["max_wall_min"])
    jobs = int(_num(os.environ.get("BIG360_SYNC_PASS_MAX_JOBS", c["pass_max_jobs"]), DEFAULTS["pass_max_jobs"]))
    return Caps(enabled=enabled(), max_wall_min=max(1.0, wall), pass_max_jobs=max(1, jobs),
                p95_factor=max(1.0, _num(c["timeout_p95_factor"], 1.5)),
                floor_min=max(1.0, _num(c["timeout_floor_min"], 10)),
                ceiling_min=max(1.0, _num(c["timeout_ceiling_min"], 180)),
                history_days=int(_num(c["timeout_history_days"], 30)),
                min_samples=max(1, int(_num(c["timeout_min_samples"], 3))),
                carry_max_age_h=_num(c["carry_max_age_h"], 24), stale_heartbeat_s=_num(c["stale_heartbeat_s"], 900),
                stale_confirm_s=_num(c["stale_confirm_s"], 120), hard_max_h=_num(c["hard_max_h"], 8))


# ------------------------------------------------------------------------------------------------ budget


@dataclass
class Budget:
    """Wall-clock and job-count budget of one sync run."""

    caps: Caps
    t0: float = field(default_factory=time.time)
    clock: Any = time.time
    started: dict[int, int] = field(default_factory=dict)
    hit: dict[str, Any] | None = None

    @property
    def deadline(self) -> float:
        return self.t0 + self.caps.max_wall_min * 60.0

    def remaining_s(self) -> float:
        return self.deadline - self.clock()

    def _hit(self, reason: str) -> str:
        if self.hit is None:
            self.hit = {"at": datetime.fromtimestamp(self.clock(), UTC).isoformat(timespec="seconds"),
                        "reason": reason}
        return reason

    def exhausted(self) -> str | None:
        """Why nothing new may start in this sync any more (None while there is budget)."""
        if not self.caps.enabled:
            return None
        if self.clock() >= self.deadline:
            return self._hit(f"sync budget of {self.caps.max_wall_min:g} min used up")
        return None

    def can_start(self, pass_no: int, expected_s: float | None = None, carried: bool = False) -> tuple[bool, str | None]:
        if not self.caps.enabled:
            return True, None
        why = self.exhausted()
        if why:
            return False, why
        if self.started.get(pass_no, 0) >= self.caps.pass_max_jobs:
            return False, self._hit(f"pass {pass_no} started its maximum of {self.caps.pass_max_jobs} jobs")
        left = self.remaining_s()
        if expected_s and not carried and expected_s > left:
            if expected_s > self.caps.max_wall_min * 60.0:
                # longer than a whole sync budget: it can never fit, so deferring it would only starve it (review
                # 2026-11-07) - it starts now (bounded by its timeout) and nothing new starts after the budget
                return True, None
            return False, (f"needs ~{expected_s / 60:.0f} min (p95), {max(0.0, left) / 60:.0f} min left in the sync "
                           "budget: first in line next sync")
        return True, None

    def note_start(self, pass_no: int) -> None:
        self.started[pass_no] = self.started.get(pass_no, 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {**self.caps.to_dict(),
                "started_at": datetime.fromtimestamp(self.t0, UTC).isoformat(timespec="seconds"),
                "deadline": datetime.fromtimestamp(self.deadline, UTC).isoformat(timespec="seconds"),
                "remaining_min": round(max(0.0, self.remaining_s()) / 60, 1) if self.caps.enabled else None,
                "jobs_started": {str(k): v for k, v in sorted(self.started.items())}, "hit": self.hit}


# ------------------------------------------------------------------------------------------------ timeouts


def p95(values: list[float]) -> float | None:
    v = sorted(float(x) for x in values if x is not None and x >= 0)
    if not v:
        return None
    k = max(0, math.ceil(0.95 * len(v)) - 1)
    return v[k]


def timeout_for(contract_min: float, durations_s: list[float], caps: Caps,
                last_timed_out_below_contract: bool = False,
                last_timeout_min: float | None = None) -> tuple[float, str, float | None]:
    """(timeout_min, source, p95_min) for one job. source: p95 | contract | escalated.

    Escalation: `last_timeout_min` is the timeout that killed the job's latest run (TimeoutError). When the history
    timeout would kill it at that timeout (or sooner) again, it doubles instead - at least the contract, at most the
    ceiling max(timeout_ceiling_min, contract): failed runs never enter the p95, so without this a job whose work grew
    past its timeout (e.g. a catch-up run after days of backlog) was killed at the same timeout on every sync, also
    when the p95 x factor lies above the contract (review 2026-11-07). `last_timed_out_below_contract` is the older
    flag form (the contract timeout when the history timeout is below it)."""
    contract_min = float(contract_min or 60.0)
    if not caps.enabled:
        return contract_min, "contract", None
    q = p95(durations_s) if len(durations_s) >= caps.min_samples else None
    if q is None:
        return contract_min, "contract", None
    q_min = q / 60.0
    cap = max(caps.ceiling_min, contract_min)
    t = min(max(q_min * caps.p95_factor, caps.floor_min), cap)
    used = None if last_timeout_min is None else float(last_timeout_min)
    if used is not None and used < cap and t <= used + 0.05:
        return round(min(cap, max(contract_min, 2.0 * used)), 1), "escalated", round(q_min, 2)
    if last_timed_out_below_contract and t < contract_min:
        return contract_min, "escalated", round(q_min, 2)
    return round(t, 1), "p95", round(q_min, 2)


#: run params of one-off work (a back-fill range, a full rebuild, an explicit entity list...): such runs say nothing
#: about a job's routine duration and stay out of its p95. Review 2026-11-07: a single earnings_dates_fetch back-fill
#: (trigger backfill, max_minutes=300, 250 min) was the p95 of its 11 runs - longer than any sync budget, so the job
#: was deferred on every sync; gdelt_stream's manual `since` catch-ups (126 min) did the same to the 3-8 s routine runs.
ONE_OFF_PARAMS: frozenset[str] = frozenset({
    "since", "full", "run_all", "rebuild", "redo", "backfill", "history", "max_new", "entities", "issuer_ids",
    "issuers", "ciks", "coverage_universe", "universes", "nsm_since", "history_weeks", "years"})
#: triggers whose runs are never routine (the gap filler / campaigns run as `backfill`)
ONE_OFF_TRIGGERS: frozenset[str] = frozenset({"backfill"})


def comparable_run(trigger: Any, params: Any, spec_params: dict[str, Any] | None = None) -> bool:
    """A run whose duration says something about the job's routine (sync / scheduled) duration: not a back-fill, no
    one-off param (ONE_OFF_PARAMS), and no `max_*` work bound above the contract default (spec.params), e.g.
    earnings_dates_fetch max_minutes=300 against its default 30."""
    import json  # noqa: PLC0415

    if str(trigger or "") in ONE_OFF_TRIGGERS:
        return False
    try:
        p = json.loads(params) if isinstance(params, str) else (params or {})
    except (TypeError, ValueError):
        return True
    if not isinstance(p, dict):
        return True
    if any(k in ONE_OFF_PARAMS and v not in (None, False, "", [], {}) for k, v in p.items()):
        return False
    for k, d in (spec_params or {}).items():
        if not str(k).startswith("max_") or k not in p:
            continue
        try:
            if float(p[k]) > float(d):
                return False
        except (TypeError, ValueError):
            continue
    return True


def history_timeouts(job_runs: Any, registry: Any, caps: Caps, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{job: {timeout_min, source, p95_min, n, contract_min}} for every job of the registry with history."""
    import polars as pl  # noqa: PLC0415

    out: dict[str, dict[str, Any]] = {}
    if job_runs is None or getattr(job_runs, "is_empty", lambda: True)() or not caps.enabled:
        return out
    now = now or datetime.now(UTC)
    cut = now - timedelta(days=caps.history_days)
    try:
        cols = ["job_id", "status", "duration_s", "started_at", "error_type", "params"]
        jr = job_runs.filter(pl.col("started_at") >= cut)
        jr = (jr.select(*cols, "trigger") if "trigger" in jr.columns
              else jr.select(*cols).with_columns(pl.lit(None, pl.Utf8).alias("trigger"))).sort("started_at")
    except Exception:  # noqa: BLE001 - malformed table: contract timeouts
        return out
    # only comparable runs count, for the p95 and for the latest-run escalation (review 2026-11-07: back-fills / one-off
    # runs never set a job's routine timeout or budget fit)
    durs: dict[str, list[float]] = {}
    last: dict[str, dict[str, Any]] = {}
    for r in jr.filter(pl.col("status").is_in(["success", "partial", "failed"])).iter_rows(named=True):
        spec = registry.jobs.get(r["job_id"])
        if spec is None or not comparable_run(r.get("trigger"), r.get("params"), getattr(spec, "params", None)):
            continue
        last[r["job_id"]] = r
        if r["status"] in ("success", "partial") and r.get("duration_s") is not None:
            durs.setdefault(r["job_id"], []).append(float(r["duration_s"]))
    for jid, spec in registry.jobs.items():
        d = durs.get(jid, [])[-60:]
        lr = last.get(jid) or {}
        contract = float(spec.timeout_min)
        used = None
        if lr.get("status") == "failed" and lr.get("error_type") == "TimeoutError":
            # the timeout that killed it: the run's timeout_min param, else the contract timeout (runner default)
            used = _param_timeout(lr.get("params"))
            used = contract if used is None else used
        t, src, q = timeout_for(contract, d, caps, last_timeout_min=used)
        if src == "contract" and not d:
            continue
        out[jid] = {"timeout_min": t, "source": src, "p95_min": q, "n": len(d), "contract_min": contract}
    return out


def _param_timeout(params: Any) -> float | None:
    import json  # noqa: PLC0415

    try:
        v = (json.loads(params) if isinstance(params, str) else (params or {})).get("timeout_min")
        return float(v) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


# ------------------------------------------------------------------------------------------------ carry-over


def carry_path(settings: Settings | None = None):
    return ops_state_dir(settings) / "sync_carry.json"


def read_carry(settings: Settings | None = None, caps: Caps | None = None,
               now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{job: entry} still worth carrying (entries older than carry_max_age_h are dropped)."""
    doc = read_json(carry_path(settings), {}) or {}
    now = now or datetime.now(UTC)
    max_h = (caps or Caps()).carry_max_age_h
    out = {}
    for e in doc.get("jobs") or []:
        try:
            t = datetime.fromisoformat(str(e.get("deferred_at")).replace("Z", "+00:00"))
            t = t if t.tzinfo else t.replace(tzinfo=UTC)
        except ValueError:
            continue
        if now - t <= timedelta(hours=max_h) and e.get("job"):
            out[str(e["job"])] = e
    return out


def write_carry(entries: list[dict[str, Any]], run_id: str, settings: Settings | None = None,
                previous: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    prev = previous or {}
    jobs = []
    seen = set()
    for e in entries:
        j = e.get("job")
        if not j or j in seen:
            continue
        seen.add(j)
        jobs.append({**e, "times": int((prev.get(j) or {}).get("times") or 0) + 1})
    doc = {"run_id": run_id, "updated_at": datetime.now(UTC).isoformat(timespec="seconds"), "jobs": jobs}
    write_json_atomic(carry_path(settings), doc)
    return doc


__all__ = ["Budget", "Caps", "DEFAULTS", "ONE_OFF_PARAMS", "ONE_OFF_TRIGGERS", "carry_path", "comparable_run",
           "enabled", "history_timeouts", "load_caps", "p95", "read_carry", "timeout_for", "write_carry"]
