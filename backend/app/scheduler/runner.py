"""DAG runner for contracts/jobs.yaml (owner: platform).

    from app.scheduler.runner import JobRegistry, JobRunner, RunOptions
    reg = JobRegistry.load()                       # parses + validates jobs.yaml (unknown deps / cycles raise)
    print(format_plan(reg.plan("nightly")))        # waves, availability, estimates, critical path (dry run)
    runner = JobRunner(reg)
    runner.run_dag("nightly", RunOptions(trigger="schedule"))
    runner.run_jobs(["prices_finalize"], RunOptions(as_of=date(2026, 10, 9), params={"tickers": ["AAPL"]}))

Semantics
* Jobs reference a callable: `module` (+ optional `function`, default `run`) or `module: "pkg.mod:func"`. A job whose
  module does not exist yet (or has no callable) is *unavailable*: it gets a `skipped` job_runs row with
  error_type=JobUnavailable and does NOT block its dependents (they run on whatever data exists).
* Dependencies are resolved inside the set of jobs being run (cross-DAG deps are ignored). Cycles raise DagCycleError.
* Parallelism: a thread pool of `worker_max_jobs` (default 6). CPU-heavy jobs (`executor: process`, see PROCESS_JOBS)
  run in a spawned child process (killable on timeout), at most `worker_max_process_jobs` at once. Jobs declare the
  rate-limited sources they hit (JOB_SOURCES / jobs.yaml `sources`); at most SOURCE_MAX_JOBS[source] such jobs run
  concurrently (e.g. one Polygon job), on top of the token buckets in app.core.ratelimit.
* RAM (ram_fix 2026-10-31, app.ops.ram_budget): every job has a budget (jobs.yaml `mem_gb`, else 0.5 GB thread /
  2 GB process). A job starts only while free RAM - every runner's outstanding reservations - headroom (4 GB) covers
  it. A process job with `mem_gb` runs in a Job Object whose committed memory is capped at the budget, with DuckDB
  memory_limit / threads and Polars / BLAS pools sized from it; hitting the cap = MemoryLimitExceeded ("OOM-killed
  at X GB"), retried once with `oom_params` (smaller chunks) where the job declares them, never repeated as is.
  Per-attempt memory (peak working set / commit, retained memory of thread jobs, lowest free RAM) goes to the job
  log ("mem ...") and data/state/ops/job_mem.jsonl. Thread jobs hand freed memory back after they end (gc + heap
  compaction).
* Retries: `retries` (default 2) with exponential backoff retry_backoff_s * 2**(attempt-1) (60 s, 120 s). Each attempt
  is its own job_runs row (attempt = 1..n). A JobResult(status="failed", retryable=False) and thread-timeouts are not
  retried.
* Timeouts: `timeout_min`. Thread jobs get ctx.cancel_event set (cooperative; see JobContext.check_cancelled); a thread
  that ignores it is stopped by injecting JobKilled (app.scheduler.threadkill) and the attempt is retried; only a thread
  stuck inside one C call (zombie) is not retried. Process jobs are killed with their whole process tree, then retried.
* Upstream failure: `on_upstream_failure: skip` (default) skips dependents of failed jobs (cascading); `run` runs anyway.
  `partial` upstream still runs dependents. Per edge: `soft_depends_on` (a subset of depends_on) only orders execution;
  a failed/skipped soft upstream never blocks the job (event-derived inputs to scoring etc.).
* Idempotency: run_key = job_id|as_of|sha1(params). If a `success` row with the same run_key exists the job is not
  re-run (unless force). A job is never executed twice concurrently (cross-process lock data/.locks/job_<id>.lock).
* Resume: run_dag(..., RunOptions(resume=True)) reuses the latest dag_run_id/as_of of that DAG and only re-runs jobs
  that did not finish with success/partial.
* job_runs rows are written at start (running) and at the end (status, timings, rows, error summary, log path).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import multiprocessing as mp
import os
import socket
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
import yaml
from filelock import FileLock, Timeout

from app import __version__
from app.core.config import Settings, get_settings
from app.core.jobqueue import job_lock
from app.core.logsetup import JsonFormatter, SecretRedactingFilter, get_logger, job_log_handler, redact, register_job_thread
from app.core.schema import get_contract
from app.core.storage import read_table, write_table
from app.scheduler.base import JobCancelled, JobContext, JobResult, new_run_id
from app.scheduler.threadkill import JobKilled, stop_thread

log = get_logger("app.scheduler.runner")

# ------------------------------------------------------------------------------------------------ resource hints
# Overridable per job in jobs.yaml with `executor: thread|process` and `sources: [...]` (additive optional fields).

PROCESS_JOBS: frozenset[str] = frozenset({
    "nlp_sentiment", "lazy_prices", "tech_levels", "forecast", "ml_features", "rebuild_features", "ml_predict",
    "level_type_backtest", "macro_regime_fit", "macro_betas", "insider_models_fit",
    "ml_train_xgb", "ml_train_transformer", "ml_ensemble",
})

JOB_SOURCES: dict[str, tuple[str, ...]] = {
    "ref_universe": ("sec_edgar",),
    "fx_rates": ("ecb", "fred"),
    "prices_us_polygon": ("polygon",),
    "prices_global_yf": ("yfinance",),
    "corporate_actions": ("polygon", "yfinance"),
    "macro_fred": ("fred",),
    "macro_global": ("ecb", "oecd", "imf", "dbnomics", "worldbank", "bok_ecos"),
    "gdelt_events": ("gdelt_files",),
    "gdelt_gkg": ("gdelt_files",),
    "sec_filings_index": ("sec_edgar",),
    "sec_xbrl_facts": ("sec_edgar",),
    "sec_form4": ("sec_edgar",),
    "sec_text": ("sec_edgar",),
    "esef_filings": ("esef_xbrl_org",),
    "edinet_filings": ("edinet",),
    "edinet_listed": ("edinet",),            # COVERAGE_V3 B1: one EDINET API job at a time (1 req/s, polite)
    "edinet_listed_backfill": ("edinet",),
    "opendart_filings": ("opendart",),
    "cn_fundamentals": ("akshare",),
    "sec_xbrl_bulk": ("sec_edgar",),
    "sec_13f": ("sec_edgar",),
    "finra_short_interest": ("finra",),
    "cvm_filings": ("cvm",),
    "uk_companies_house": ("companies_house",),
    "yf_fundamentals": ("yfinance",),
    "openfigi_refresh": ("openfigi",),
    "backfill_prices": ("polygon", "yfinance"),
    "backfill_fundamentals": ("sec_edgar",),
    "prices_kr_krx": ("krx",),
    "prices_cn_baostock": ("baostock",),
}

#: max concurrently running jobs per rate-limited source (default 2)
SOURCE_MAX_JOBS: dict[str, int] = {
    "polygon": 1, "yfinance": 1, "akshare": 1, "opendart": 1, "edinet": 1, "openfigi": 1, "companies_house": 1,
    "esef_xbrl_org": 1, "oecd": 1, "gdelt_doc": 1, "sec_edgar": 3, "gdelt_files": 2,
}

#: before a DAG starts, wait (up to DAG_WAIT_MAX_S) while these DAGs are running. `sync` = the startup sync
#: (app.ops.sync, lock dag_sync.lock): a waiting refresh writes data/state/ops/sync.YIELD, the sync then starts no
#: new job and exits once its running jobs finish (docs/STARTUP_SYNC_PLAN.md A4: no overlap)
DAG_WAITS: dict[str, tuple[str, ...]] = {"nightly": ("sync",), "weekly": ("nightly", "sync"),
                                         "monthly": ("nightly", "weekly", "sync")}
DAG_WAIT_MAX_S = 6 * 3600
SYNC_WAIT_MAX_S = 3 * 3600

OK_STATUSES = ("success", "partial")
RUN_KEY_PARAM_EXCLUDE = {"force", "dry_run", "timeout_min"}


class DagCycleError(ValueError):
    pass


class JobUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------------------------------------ registry


@dataclass(frozen=True)
class JobSpec:
    id: str
    dag: str
    module: str
    function: str = "run"
    owner: str = ""
    depends_on: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)
    retries: int = 2
    retry_backoff_s: float = 60.0
    timeout_min: float = 60.0
    on_upstream_failure: str = "skip"
    est_min_mvp: float | None = None
    est_min_full: float | None = None
    note: str | None = None
    executor: str = "thread"
    sources: tuple[str, ...] = ()
    #: subset of depends_on that only orders execution: their failure/skip never blocks this job
    soft_depends_on: tuple[str, ...] = ()
    #: resource class (app.ops.heavy_slot): light | heavy | exclusive; None = default by executor / EXCLUSIVE_JOBS
    resource: str | None = None
    #: RAM budget in GB (ram_fix 2026-10-31, app.ops.ram_budget): admission against free RAM; for process jobs also
    #: the Job Object's committed-memory cap and the DuckDB / Polars / BLAS limits of the job process
    mem_gb: float | None = None
    #: params applied to the one retry after the job hit its memory cap (smaller chunks), e.g. {chunk_size: 1500}
    oom_params: dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @property
    def target(self) -> str:
        return f"{self.module}:{self.function}"

    def est_min(self, universe: str = "mvp") -> float:
        v = self.est_min_full if universe == "full" else self.est_min_mvp
        return float(v if v is not None else (self.est_min_mvp or 1.0))


def _opt_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return -1.0  # reported by validate()


def _parse_target(module: str, function: str | None) -> tuple[str, str]:
    if ":" in module:
        m, f = module.split(":", 1)
        return m, f
    return module, function or "run"


def check_available(spec: JobSpec) -> tuple[bool, str | None]:
    """Cheap availability check without importing the module: (True, None) or (False, reason)."""
    try:
        found = importlib.util.find_spec(spec.module)
    except (ModuleNotFoundError, ValueError) as e:
        return False, f"module {spec.module} not found ({e})"
    if found is None:
        return False, f"module {spec.module} not found"
    return True, None


def resolve_callable(target: str) -> Callable[[JobContext], Any]:
    """'pkg.mod:func' -> callable. Raises JobUnavailable when the module/function does not exist."""
    mod_name, func = _parse_target(target, None)
    try:
        mod = importlib.import_module(mod_name)
    except ModuleNotFoundError as e:
        if e.name and (mod_name == e.name or mod_name.startswith(e.name + ".")):
            raise JobUnavailable(f"module {mod_name} not found") from e
        raise  # a dependency of an existing module is missing -> real failure
    fn = getattr(mod, func, None)
    if not callable(fn):
        raise JobUnavailable(f"{mod_name} has no callable {func}()")
    return fn


class JobRegistry:
    """Parsed + validated contracts/jobs.yaml."""

    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        self.version = doc.get("version")
        self.timezone: str = doc.get("timezone", "America/New_York")
        self.dags: dict[str, dict[str, Any]] = dict(doc.get("dags", {}))
        d = dict(doc.get("defaults", {}))
        self.defaults = d
        self.jobs: dict[str, JobSpec] = {}
        for j in doc.get("jobs", []):
            if j["id"] in self.jobs:
                raise ValueError(f"duplicate job id {j['id']}")
            module, func = _parse_target(j["module"], j.get("function"))
            jid = j["id"]
            self.jobs[jid] = JobSpec(
                id=jid,
                dag=j.get("dag", "manual"),
                module=module,
                function=func,
                owner=str(j.get("owner", "")),
                depends_on=tuple(j.get("depends_on", []) or []),
                writes=tuple(j.get("writes", []) or []),
                params=dict(j.get("params", {}) or {}),
                retries=int(j.get("retries", d.get("retries", 2))),
                retry_backoff_s=float(j.get("retry_backoff_s", d.get("retry_backoff_s", 60))),
                timeout_min=float(j.get("timeout_min", d.get("timeout_min", 60))),
                on_upstream_failure=str(j.get("on_upstream_failure", d.get("on_upstream_failure", "skip"))),
                est_min_mvp=j.get("est_min_mvp"),
                est_min_full=j.get("est_min_full"),
                note=j.get("note"),
                executor=str(j.get("executor") or ("process" if jid in PROCESS_JOBS else "thread")),
                sources=tuple(j.get("sources") or JOB_SOURCES.get(jid, ())),
                soft_depends_on=tuple(j.get("soft_depends_on", []) or []),
                resource=j.get("resource"),
                mem_gb=_opt_float(j.get("mem_gb")),
                oom_params=dict(j.get("oom_params") or {}),
            )
        errs = self.validate()
        if errs:
            raise ValueError("jobs.yaml invalid: " + "; ".join(errs))

    # ---- construction
    @classmethod
    def load(cls, path: str | Path | None = None, settings: Settings | None = None) -> JobRegistry:
        p = Path(path) if path else (settings or get_settings()).contracts_dir / "jobs.yaml"
        return cls(yaml.safe_load(p.read_text(encoding="utf-8")))

    # ---- validation
    def validate(self) -> list[str]:
        errs: list[str] = []
        for j in self.jobs.values():
            for dep in j.depends_on:
                if dep not in self.jobs:
                    errs.append(f"{j.id}: unknown dependency {dep!r}")
            if j.dag not in self.dags:
                errs.append(f"{j.id}: unknown dag {j.dag!r}")
            if j.executor not in ("thread", "process"):
                errs.append(f"{j.id}: invalid executor {j.executor!r}")
            if j.resource is not None and j.resource not in ("light", "heavy", "big", "exclusive"):
                errs.append(f"{j.id}: invalid resource {j.resource!r}")
            if j.mem_gb is not None and not (0 < j.mem_gb <= 64):
                errs.append(f"{j.id}: invalid mem_gb {j.mem_gb!r} (0 < mem_gb <= 64)")
            if j.oom_params and j.executor != "process":
                errs.append(f"{j.id}: oom_params needs executor: process (only process jobs have a memory cap)")
            if j.on_upstream_failure not in ("skip", "run"):
                errs.append(f"{j.id}: invalid on_upstream_failure {j.on_upstream_failure!r}")
            for dep in j.soft_depends_on:
                if dep not in j.depends_on:
                    errs.append(f"{j.id}: soft dependency {dep!r} must also be listed in depends_on")
        if not errs:
            try:
                self.topo_order(self.jobs.keys())
            except DagCycleError as e:
                errs.append(str(e))
        return errs

    # ---- graph helpers
    def dag_jobs(self, dag: str) -> list[str]:
        if dag not in self.dags:
            raise KeyError(f"unknown dag {dag!r} (known: {sorted(self.dags)})")
        return [j.id for j in self.jobs.values() if j.dag == dag]

    def deps_within(self, job_id: str, ids: set[str]) -> list[str]:
        return [d for d in self.jobs[job_id].depends_on if d in ids]

    def topo_order(self, ids: Iterable[str]) -> list[str]:
        """Kahn's algorithm, stable w.r.t. jobs.yaml order. Raises DagCycleError naming a cycle."""
        idset = list(dict.fromkeys(ids))
        s = set(idset)
        indeg = {j: len(self.deps_within(j, s)) for j in idset}
        children: dict[str, list[str]] = {j: [] for j in idset}
        for j in idset:
            for d in self.deps_within(j, s):
                children[d].append(j)
        order: list[str] = []
        ready = [j for j in idset if indeg[j] == 0]
        while ready:
            j = ready.pop(0)
            order.append(j)
            for c in children[j]:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
        if len(order) != len(idset):
            raise DagCycleError("dependency cycle: " + " -> ".join(self._find_cycle([j for j in idset if j not in order])))
        return order

    def _find_cycle(self, nodes: list[str]) -> list[str]:
        s = set(nodes)
        state: dict[str, int] = {}
        stack: list[str] = []

        def dfs(n: str) -> list[str] | None:
            state[n] = 1
            stack.append(n)
            for d in self.jobs[n].depends_on:
                if d not in s:
                    continue
                if state.get(d) == 1:
                    return stack[stack.index(d):] + [d]
                if state.get(d) is None:
                    r = dfs(d)
                    if r:
                        return r
            state[n] = 2
            stack.pop()
            return None

        for n in nodes:
            if state.get(n) is None:
                r = dfs(n)
                if r:
                    return r
        return nodes

    def waves(self, ids: Iterable[str]) -> list[list[str]]:
        """Group jobs into dependency levels (wave k depends only on waves < k)."""
        order = self.topo_order(ids)
        s = set(order)
        level: dict[str, int] = {}
        for j in order:
            deps = self.deps_within(j, s)
            level[j] = 1 + max((level[d] for d in deps), default=-1)
        out: list[list[str]] = [[] for _ in range(max(level.values(), default=-1) + 1)]
        for j in order:
            out[level[j]].append(j)
        return out

    def upstream(self, job_id: str) -> list[str]:
        seen: set[str] = set()
        todo = list(self.jobs[job_id].depends_on)
        while todo:
            d = todo.pop()
            if d in seen or d not in self.jobs:
                continue
            seen.add(d)
            todo.extend(self.jobs[d].depends_on)
        return [j for j in self.jobs if j in seen]

    def downstream(self, job_id: str, same_dag: bool = True) -> list[str]:
        dag = self.jobs[job_id].dag
        out: set[str] = set()
        changed = True
        frontier = {job_id}
        while changed:
            changed = False
            for j in self.jobs.values():
                if j.id in out or j.id == job_id or (same_dag and j.dag != dag):
                    continue
                if any(d in frontier or d in out for d in j.depends_on):
                    out.add(j.id)
                    changed = True
        return [j for j in self.jobs if j in out]

    def remaining_path(self, ids: Iterable[str], universe: str = "mvp") -> dict[str, float]:
        """Longest estimated path (minutes) from each job to the end of the run: used as scheduling priority."""
        order = self.topo_order(ids)
        s = set(order)
        children: dict[str, list[str]] = {j: [] for j in order}
        for j in order:
            for d in self.deps_within(j, s):
                children[d].append(j)
        rp: dict[str, float] = {}
        for j in reversed(order):
            rp[j] = self.jobs[j].est_min(universe) + max((rp[c] for c in children[j]), default=0.0)
        return rp

    def critical_path(self, ids: Iterable[str], universe: str = "mvp") -> tuple[float, list[str]]:
        order = self.topo_order(ids)
        s = set(order)
        best: dict[str, tuple[float, list[str]]] = {}
        for j in order:
            prev = max((best[d] for d in self.deps_within(j, s)), key=lambda t: t[0], default=(0.0, []))
            best[j] = (prev[0] + self.jobs[j].est_min(universe), prev[1] + [j])
        return max(best.values(), key=lambda t: t[0], default=(0.0, []))

    def plan(self, target: str | Iterable[str], universe: str = "mvp") -> dict[str, Any]:
        """Dry-run plan for a DAG name or explicit job ids (no execution, no imports)."""
        if isinstance(target, str) and target in self.dags:
            ids, label = self.dag_jobs(target), target
        else:
            ids = [target] if isinstance(target, str) else list(target)
            unknown = [j for j in ids if j not in self.jobs]
            if unknown:
                raise KeyError(f"unknown job(s): {unknown}")
            label = ",".join(ids)
        waves = self.waves(ids)
        cp_min, cp = self.critical_path(ids, universe)
        rows = []
        for w, wave in enumerate(waves):
            for j in wave:
                spec = self.jobs[j]
                ok, why = check_available(spec)
                rows.append({
                    "wave": w, "job_id": j, "dag": spec.dag, "owner": spec.owner, "target": spec.target,
                    "available": ok, "unavailable_reason": why,
                    "depends_on": [d for d in spec.depends_on if d in set(ids)],
                    "est_min": spec.est_min(universe), "executor": spec.executor, "sources": list(spec.sources),
                    "mem_gb": spec.mem_gb,
                    "retries": spec.retries, "timeout_min": spec.timeout_min, "params": spec.params,
                    "on_critical_path": j in cp,
                })
        return {
            "target": label, "universe": universe, "n_jobs": len(ids),
            "n_available": sum(r["available"] for r in rows), "waves": len(waves),
            "critical_path_min": cp_min, "critical_path": cp,
            "total_est_min": sum(r["est_min"] for r in rows), "jobs": rows,
            "cron": self.dags.get(label, {}).get("cron") if label in self.dags else None,
        }


def format_plan(plan: dict[str, Any]) -> str:
    lines = [
        f"PLAN {plan['target']}  universe={plan['universe']}  jobs={plan['n_jobs']} "
        f"(available {plan['n_available']}, unavailable {plan['n_jobs'] - plan['n_available']})  waves={plan['waves']}",
    ]
    if plan.get("cron"):
        lines.append(f"cron: {plan['cron']}")
    cur = -1
    for r in plan["jobs"]:
        if r["wave"] != cur:
            cur = r["wave"]
            lines.append(f"-- wave {cur}")
        flag = "OK  " if r["available"] else "N/A "
        cp = "*" if r["on_critical_path"] else " "
        deps = ",".join(r["depends_on"]) or "-"
        src = ",".join(r["sources"]) or "-"
        mem = f"{r['mem_gb']:g}G" if r.get("mem_gb") else "-"
        lines.append(f"  {flag}{cp} {r['job_id']:<26} {r['est_min']:>6.0f} min  {r['executor']:<7} mem={mem:<5} "
                     f"src={src:<22} deps={deps}")
    lines.append(f"critical path ~{plan['critical_path_min']:.0f} min: {' -> '.join(plan['critical_path'])}")
    lines.append(f"sum of job estimates {plan['total_est_min']:.0f} min ('*' = on critical path, N/A = module not built yet)")
    return "\n".join(lines)


# ------------------------------------------------------------------------------------------------ helpers


def previous_business_day(d: date) -> date:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def scheduler_tz(settings: Settings | None = None, registry: JobRegistry | None = None) -> ZoneInfo:
    s = settings or get_settings()
    name = s.scheduler_timezone
    if name.lower() == "contract":
        name = registry.timezone if registry else "America/New_York"
    return ZoneInfo(name)


def default_as_of(settings: Settings | None = None) -> date:
    """Previous business day in the scheduler's local timezone (the session the nightly run processes)."""
    return previous_business_day(datetime.now(scheduler_tz(settings)).date())


_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _expand_cron_dow(field_: str) -> str:
    """Standard cron day-of-week (0/7=Sun) -> APScheduler names. APScheduler 3.x numbers days from Monday=0 and its
    from_crontab() does NOT translate, so '2-6' would silently mean Wed-Sun."""
    if field_ in ("*", "?"):
        return "*"
    days: set[int] = set()
    for tok in field_.split(","):
        step = 1
        if "/" in tok:
            tok, st = tok.split("/", 1)
            step = int(st)
        if tok in ("*", ""):
            lo, hi = 0, 6
        elif "-" in tok:
            a, b = tok.split("-", 1)
            lo, hi = (_DOW_NAMES.index(a.lower()[:3]) if not a.isdigit() else int(a)),                      (_DOW_NAMES.index(b.lower()[:3]) if not b.isdigit() else int(b))
        else:
            lo = hi = _DOW_NAMES.index(tok.lower()[:3]) if not tok.isdigit() else int(tok)
        rng = range(lo, hi + 1, step) if lo <= hi else list(range(lo, 7, step)) + list(range(0, hi + 1, step))
        days.update(d % 7 for d in rng)
    return ",".join(_DOW_NAMES[d] for d in sorted(days))


def cron_trigger(expr: str, tz: ZoneInfo):
    """APScheduler CronTrigger from a standard 5-field crontab (standard weekday numbering)."""
    from apscheduler.triggers.cron import CronTrigger

    minute, hour, dom, month, dow = expr.split()
    return CronTrigger(minute=minute, hour=hour, day=dom, month=month, day_of_week=_expand_cron_dow(dow), timezone=tz)


def make_run_key(job_id: str, as_of: date, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in sorted(params.items()) if k not in RUN_KEY_PARAM_EXCLUDE}
    h = hashlib.sha1(json.dumps(clean, sort_keys=True, default=str).encode()).hexdigest()[:10]
    return f"{job_id}|{as_of.isoformat()}|{h}"


def new_dag_run_id(dag: str) -> str:
    return f"{new_run_id('D')}-{dag}"


def dag_of_run_id(dag_run_id: str | None) -> str | None:
    if not dag_run_id or "-" not in dag_run_id:
        return None
    return dag_run_id.rsplit("-", 1)[1]


def _coerce_result(res: Any) -> JobResult:
    if res is None:
        return JobResult()
    if isinstance(res, JobResult):
        return res
    if isinstance(res, dict):
        return JobResult(**{k: v for k, v in res.items() if k in JobResult.__dataclass_fields__})
    # duck-typed JobResult from another import path (process boundary)
    return JobResult(**{k: getattr(res, k) for k in JobResult.__dataclass_fields__ if hasattr(res, k)})


# ------------------------------------------------------------------------------------------------ job_runs I/O


class JobRunStore:
    """Reads/writes the job_runs table. Write failures are logged, never raised (bookkeeping must not kill jobs)."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        c = get_contract("job_runs")
        full = {col: row.get(col) for col in c.column_names}
        try:
            df = pl.DataFrame([full], schema=c.polars_schema())
            with self._lock:
                write_table(df, "job_runs", settings=self.settings)
        except Exception as e:  # pragma: no cover - defensive
            log.error("job_runs write failed for %s: %s", row.get("run_id"), e)

    def read(self) -> pl.DataFrame:
        try:
            return read_table("job_runs", settings=self.settings)
        except Exception as e:  # pragma: no cover - defensive
            log.error("job_runs read failed: %s", e)
            from app.core.schema import empty_frame

            return empty_frame("job_runs")

    def last_watermark(self, job_id: str) -> str | None:
        df = self.read()
        if df.is_empty():
            return None
        df = df.filter((pl.col("job_id") == job_id) & pl.col("status").is_in(list(OK_STATUSES))
                       & pl.col("watermark").is_not_null()).sort("started_at", descending=True)
        return df["watermark"][0] if df.height else None

    def succeeded(self, run_key: str) -> dict[str, Any] | None:
        df = self.read()
        if df.is_empty() or "run_key" not in df.columns:
            return None
        df = df.filter((pl.col("run_key") == run_key) & (pl.col("status") == "success")).sort("started_at", descending=True)
        return df.row(0, named=True) if df.height else None

    def recent(self, limit: int = 50, job_id: str | None = None, status: str | None = None,
               dag_run_id: str | None = None) -> pl.DataFrame:
        df = self.read()
        if job_id:
            df = df.filter(pl.col("job_id") == job_id)
        if status:
            df = df.filter(pl.col("status") == status)
        if dag_run_id:
            df = df.filter(pl.col("dag_run_id") == dag_run_id)
        return df.sort("started_at", descending=True).head(limit)

    def latest_dag_run(self, dag: str) -> tuple[str, date | None, dict[str, str]] | None:
        """(dag_run_id, as_of, {job_id: final status of its latest attempt}) of the latest run of a DAG."""
        df = self.read()
        if df.is_empty():
            return None
        df = df.filter(pl.col("dag_run_id").is_not_null() & pl.col("dag_run_id").str.ends_with(f"-{dag}"))
        if df.is_empty():
            return None
        last = df.sort("started_at", descending=True)["dag_run_id"][0]
        sub = df.filter(pl.col("dag_run_id") == last).sort("started_at")
        statuses = {r["job_id"]: r["status"] for r in sub.iter_rows(named=True)}
        as_of = next((r for r in sub["as_of_date"].to_list() if r is not None), None) if "as_of_date" in sub.columns else None
        return last, as_of, statuses

    def mark_orphans(self) -> int:
        """Rows stuck in 'running' whose job lock is free (crashed process) -> failed. Called at worker start."""
        from app.core.jobqueue import job_is_running

        df = self.read()
        if df.is_empty():
            return 0
        stuck = df.filter(pl.col("status") == "running")
        n = 0
        for r in stuck.iter_rows(named=True):
            if job_is_running(r["job_id"], self.settings):
                continue
            now = datetime.now(UTC)
            r.update(status="failed", finished_at=now, error_type="Orphaned",
                     error_message="process died while the job was running (marked at worker start)",
                     duration_s=(now - r["started_at"]).total_seconds() if r.get("started_at") else None)
            self.write(r)
            n += 1
        return n


# ------------------------------------------------------------------------------------------------ execution


@dataclass
class RunOptions:
    as_of: date | None = None
    params: dict[str, Any] = field(default_factory=dict)
    trigger: str = "manual"                     # schedule | manual | backfill | dependency | api
    force: bool = False                         # ignore run_key idempotency
    dry_run: bool = False
    resume: bool = False                        # run_dag: continue the latest dag run
    run_id: str | None = None                   # run_id for attempt 1 of the first job (API-queued requests)
    dag_run_id: str | None = None
    max_workers: int | None = None
    max_process_jobs: int | None = None
    backoff_scale: float = 1.0                  # tests: 0 disables sleeping between retries
    retries: int | None = None                  # override per-job retries
    record_unavailable: bool = True             # write a skipped job_runs row for unavailable jobs
    wait_for_dags: bool = True
    #: coverage v3 A2: a job that cannot meet the RAM floor after the max wait is deferred (skipped, reason
    #: `deferred`, error_type RamFloor) instead of starting in spill mode (the startup sync: the next pass retries)
    ram_defer: bool = False
    #: max wait for RAM admission of this run (default BIG360_RAM_WAIT_MAX_S; the sync bounds it by its pass budget)
    ram_wait_max_s: float | None = None
    #: coverage v3 A3: wall-clock time (epoch s) after which a job that has not started yet (waiting for RAM or the
    #: heavy slot) is deferred instead (skipped, reason `deferred`) - the startup sync's pass budget
    start_by: float | None = None


@dataclass
class JobOutcome:
    job_id: str
    status: str                                 # success | partial | failed | skipped
    reason: str | None = None                   # unavailable | upstream_failed | idempotent | already_running | stopped | dry_run | job_skipped | resumed
    run_ids: list[str] = field(default_factory=list)
    attempts: int = 0
    duration_s: float = 0.0
    rows_written: int = 0
    error_type: str | None = None
    message: str | None = None

    @property
    def blocks_dependents(self) -> bool:
        if self.status == "failed":
            return True
        return self.status == "skipped" and self.reason in ("upstream_failed", "already_running", "stopped")


@dataclass
class RunReport:
    target: str
    dag_run_id: str | None
    as_of: date
    outcomes: dict[str, JobOutcome]
    started_at: datetime
    finished_at: datetime | None = None

    @property
    def status(self) -> str:
        sts = [o.status for o in self.outcomes.values()]
        failed = sum(s == "failed" for s in sts) + sum(o.reason == "upstream_failed" for o in self.outcomes.values())
        ok = sum(s in OK_STATUSES for s in sts)
        if failed == 0:
            return "success"
        return "partial" if ok else "failed"

    def counts(self) -> dict[str, int]:
        c = Counter((o.status if not o.reason else f"{o.status}:{o.reason}") for o in self.outcomes.values())
        return dict(sorted(c.items()))

    def summary(self) -> str:
        lines = [f"{self.target} dag_run_id={self.dag_run_id} as_of={self.as_of} status={self.status} {self.counts()}"]
        for o in self.outcomes.values():
            extra = f" ({o.reason})" if o.reason else ""
            err = f" {o.error_type}: {o.message}" if o.error_type else (f" {o.message}" if o.message and o.status != "success" else "")
            lines.append(f"  {o.status:<8}{extra:<20} {o.job_id:<26} {o.duration_s:7.1f}s rows={o.rows_written}{err}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target, "dag_run_id": self.dag_run_id, "as_of": self.as_of.isoformat(),
                "status": self.status, "counts": self.counts(),
                "outcomes": {k: asdict(v) for k, v in self.outcomes.items()}}


def _process_entry(target: str, payload: dict[str, Any], conn: Any, log_path: str | None) -> None:
    """Child-process entry for executor=process jobs (spawned; must stay importable at module level)."""
    import logging
    import signal as _signal

    try:  # incident 2026-10-24: CTRL+BREAK -> KeyboardInterrupt (normal Python exit) instead of the console default
        # handler's ExitProcess, which could deadlock in DLL teardown and leave the process stuck holding its RAM
        if hasattr(_signal, "SIGBREAK"):
            _signal.signal(_signal.SIGBREAK, _signal.default_int_handler)
    except (ValueError, OSError):  # pragma: no cover
        pass
    try:
        from app.core.logsetup import setup_logging

        setup_logging()
        if log_path:
            fh = logging.FileHandler(log_path, encoding="utf-8", delay=True)
            fh.setFormatter(JsonFormatter() if get_settings().log_json else logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
            fh.addFilter(SecretRedactingFilter())
            logging.getLogger().addHandler(fh)
        fn = resolve_callable(target)
        ctx = JobContext(
            job_id=payload["job_id"], run_id=payload["run_id"], as_of=date.fromisoformat(payload["as_of"]),
            params=payload["params"], watermark=payload.get("watermark"), dag_run_id=payload.get("dag_run_id"),
            trigger=payload.get("trigger", "manual"), log=get_logger(f"app.job.{payload['job_id']}"),
        )
        res = _coerce_result(fn(ctx))
        conn.send(("ok", asdict(res)))
    except BaseException as e:  # noqa: BLE001 - report everything to the parent
        try:
            conn.send(("err", type(e).__name__, redact(str(e))[:2000], traceback.format_exc()))
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _kill_tree(proc: Any, job: Any = None) -> None:
    """Kill a timed-out process job with all its children (pools of workers would otherwise be orphaned, R4). With
    a Job Object every descendant dies, also those whose own parent already exited (incident 2026-10-24)."""
    if job is not None:
        try:
            job.terminate()
        except Exception:  # pragma: no cover
            pass
    try:
        from app.ops.winsys import kill_tree

        kill_tree(proc.pid)
    except Exception:  # pragma: no cover - fall back to the process alone
        pass
    if proc.is_alive():
        proc.terminate()
    proc.join(10)


_SPAWN_LOCK = threading.Lock()
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _start_in_own_group(proc: Any, env: dict[str, str] | None = None) -> None:
    """proc.start() with CREATE_NEW_PROCESS_GROUP (Windows): a CTRL+BREAK sent to the worker's / taskrun's group
    never reaches the job process nor its pool workers (they inherit the new group). Incident 2026-10-24: the
    console's default CTRL+BREAK handler ran ExitProcess inside job processes, which then stayed stuck in their
    exit for hours (~0.9 GB each). The runner stops a job by killing its Job Object (external, always completes).
    multiprocessing exposes no creation flags, so its CreateProcess is wrapped for the duration of start().
    `env` (ram_fix 2026-10-31): extra environment of the child only (DuckDB / Polars / BLAS caps from the job's
    budget); they must be in place at process creation because the child imports polars before running the job."""
    if os.name != "nt":
        with _SPAWN_LOCK:
            old = {k: os.environ.get(k) for k in (env or {})}
            os.environ.update(env or {})
            try:
                proc.start()
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        return
    try:
        import _winapi
        import multiprocessing.popen_spawn_win32 as psw
    except ImportError:  # pragma: no cover
        proc.start()
        return

    class _Win:
        def __getattr__(self, name: str) -> Any:
            return getattr(_winapi, name)

        @staticmethod
        def CreateProcess(app, cmd, pattr, tattr, inherit, flags, env_, cwd, si):  # noqa: N802, ANN001
            if env:
                env_ = {**(env_ if env_ is not None else os.environ), **env}
            return _winapi.CreateProcess(app, cmd, pattr, tattr, inherit, flags | _CREATE_NEW_PROCESS_GROUP, env_,
                                         cwd, si)

    with _SPAWN_LOCK:
        orig = psw._winapi
        psw._winapi = _Win()
        try:
            proc.start()
        finally:
            psw._winapi = orig


def _job_object(proc: Any, name: str) -> Any:
    """JobObject holding a freshly started job process (None when unsupported / failed)."""
    try:
        from app.ops.winsys import IS_WINDOWS, JobObject

        if not IS_WINDOWS:
            return None
        job = JobObject(name=name)
        if job.ok and job.assign_tree(proc.pid):
            return job
        log.debug("job object for %s unavailable (error %s)", name, job.error)
        job.close()
    except Exception as e:  # pragma: no cover - never block a job on this
        log.debug("job object for %s failed: %s", name, e)
    return None


def _reap_job(job: Any, job_id: str) -> int:
    """After a process job ended: kill whatever is left in its job (leaked pool workers, grandchildren)."""
    if job is None:
        return 0
    try:
        n = job.terminate()
        if n:
            log.warning("%s: %d leftover process(es) of the job process killed (leak prevented)", job_id, n)
            job.wait_empty(10)
        return n
    except Exception:  # pragma: no cover
        return 0
    finally:
        job.close()


#: ram_fix 2026-10-31: per-attempt memory records (data/state/ops/job_mem.jsonl, rotated at 5 MB) and OOM detection
JOB_MEM_FILE = "job_mem.jsonl"
JOB_MEM_MAX_BYTES = 5 * 1024**2
#: committed memory at >= 98 % of the Job Object cap = the job ran into its cap: it is killed and reported as OOM
OOM_NEAR = 0.98
#: a failed job whose peak reached 80 % of the cap AND whose error / exit code says "out of memory" is OOM as well
OOM_HINT = 0.80
OOM_MARKERS = ("memoryerror", "outofmemory", "out of memory", "memory allocation of", "bad_alloc",
               "not enough memory", "arraymemoryerror", "cannot allocate", "mmap")
#: exit codes of an allocator abort: MSVC abort() 3, Rust __fastfail 0xC0000409, STATUS_NO_MEMORY, commitment limit,
#: access violation after a failed allocation (OpenBLAS); only counted when the peak reached OOM_HINT of the cap
OOM_EXIT_CODES = frozenset({3, 0xC0000409, 0xC0000017, 0xC000012D, 0xC0000005, -1073740791, -1073741801, -1073741523,
                            -1073741819})
SAMPLE_S = 1.0


def _record_mem(settings: Settings, rec: dict[str, Any]) -> None:
    """Append one attempt's memory record (never raises)."""
    try:
        from app.ops.paths import ops_state_dir

        p = ops_state_dir(settings) / JOB_MEM_FILE
        if p.exists() and p.stat().st_size > JOB_MEM_MAX_BYTES:
            os.replace(p, p.with_suffix(".jsonl.1"))
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:  # pragma: no cover - bookkeeping only
        log.debug("job_mem record failed: %s", e)


def _is_oom(err: tuple[str, str, str] | None, exitcode: int | None, peak_mb: float | None, limit_mb: float | None) -> bool:
    if not limit_mb or not peak_mb:
        return False
    if peak_mb >= limit_mb * OOM_NEAR:
        return True
    if peak_mb < limit_mb * OOM_HINT:
        return False
    text = " ".join(err[:2]).lower() if err else ""
    return any(m in text for m in OOM_MARKERS) or (exitcode is not None and exitcode in OOM_EXIT_CODES)


def _r(v: Any) -> int | None:
    return None if v is None else int(round(float(v)))


def _budget(spec: JobSpec) -> float:
    from app.ops import ram_budget as RB

    return RB.budget_of(spec)


class _MemSampler:
    """Peak / start / end working set and private commit of a job: its Job Object (process jobs) or this process
    (thread jobs: the delta is the job's, blurred by other thread jobs running at the same time), plus the machine's
    lowest free RAM while it ran."""

    def __init__(self, job: Any = None, pid: int | None = None) -> None:
        self.job = job
        self.pid = pid
        self.n = 0
        self.start_ws = self.start_pv = None
        self.end_ws = self.end_pv = None
        self.peak_ws = self.peak_pv = 0.0
        self.min_free: float | None = None
        self.limit: dict[str, Any] = {}
        self._last = 0.0

    def sample(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self.n and now - self._last < SAMPLE_S * 0.9:
            return
        self._last = now
        try:
            from app.ops import winsys

            if self.job is not None:
                d = self.job.memory_detail()
                ws, pv = d.get("ws_mb", 0.0), d.get("private_mb", 0.0)
                self.limit = self.job.limit_info() or self.limit
            elif self.pid:   # process job without a Job Object (non-Windows / creation failed): its process tree
                ws = pv = 0.0
                for p in winsys.process_tree(self.pid):
                    d = winsys.process_memory_detail(p) or {}
                    ws += float(d.get("rss_mb") or 0.0)
                    pv += float(d.get("private_mb") or 0.0)
            else:
                d = winsys.process_memory_detail(os.getpid()) or {}
                ws, pv = float(d.get("rss_mb") or 0.0), float(d.get("private_mb") or 0.0)
            m = winsys.memory_status() or {}
        except Exception:  # pragma: no cover - measurement never breaks a job
            return
        self.n += 1
        if self.start_ws is None:
            self.start_ws, self.start_pv = ws, pv
        if ws or pv or (self.job is None and not self.pid):
            self.end_ws, self.end_pv = ws, pv
        self.peak_ws, self.peak_pv = max(self.peak_ws, ws), max(self.peak_pv, pv)
        fr = m.get("avail_gb")
        if fr is not None and (m.get("total_gb") or 0) > 0:
            self.min_free = fr if self.min_free is None else min(self.min_free, fr)

    @property
    def peak_commit_mb(self) -> float | None:
        v = self.limit.get("peak_job_mb")
        return max(float(v), self.peak_pv) if v else (self.peak_pv or None)

    def summary(self) -> dict[str, Any]:
        out = {"peak_ws_mb": _r(self.peak_ws), "peak_private_mb": _r(self.peak_pv),
               "start_ws_mb": _r(self.start_ws), "end_ws_mb": _r(self.end_ws),
               "start_private_mb": _r(self.start_pv), "end_private_mb": _r(self.end_pv),
               "min_free_gb": None if self.min_free is None else round(self.min_free, 2), "samples": self.n}
        if self.job is not None or self.pid:
            out.update(peak_commit_mb=_r(self.peak_commit_mb), limit_mb=_r(self.limit.get("limit_mb")))
        else:  # thread job: growth of the shared process
            if self.start_ws is not None:
                out.update(delta_peak_ws_mb=_r(self.peak_ws - self.start_ws),
                           retained_private_mb=_r((self.end_pv or 0) - (self.start_pv or 0)))
        return out


def oom_message(peak_mb: float, limit_mb: float, detail: str = "") -> str:
    return (f"OOM-killed at {peak_mb / 1024:.2f} GB (budget {limit_mb / 1024:.2f} GB, Job Object memory cap)"
            + (f": {detail[:300]}" if detail else ""))


#: incident 2026-10-24: machine-level guard. Process jobs (the heavy ones) wait while free RAM is below the floor or
#: the pagefile is growing; jobs in DEFER_ON_PRESSURE are skipped instead (the next hourly run retries).
PRESSURE_MIN_FREE_GB = 3.0
PRESSURE_WAIT_MAX_S = 600.0
DEFER_ON_PRESSURE: frozenset[str] = frozenset({"news_finalise"})


def machine_pressure() -> str | None:
    """Reason string when the machine is under memory pressure, else None (app.ops.governor.machine_guard)."""
    try:
        from app.ops.governor import machine_guard

        return machine_guard().get("reason")
    except Exception:  # pragma: no cover
        return None


class JobRunner:
    #: seconds to keep injecting JobKilled into a timed-out thread before declaring it a zombie
    kill_wait_s: float = 15.0

    def __init__(self, registry: JobRegistry | None = None, settings: Settings | None = None,
                 stop_event: threading.Event | None = None):
        self.settings = settings or get_settings()
        self.registry = registry or JobRegistry.load(settings=self.settings)
        self.store = JobRunStore(self.settings)
        self.stop_event = stop_event or threading.Event()
        self.host = socket.gethostname()
        #: machine-level guard for process jobs (env BIG360_PRESSURE_GUARD=0 disables it, e.g. in tests)
        self.pressure_guard = os.environ.get("BIG360_PRESSURE_GUARD", "1") != "0"
        #: ram_fix 2026-10-31: admission against the machine RAM budget (app.ops.ram_budget; BIG360_RAM_BUDGET=0 off)
        from app.ops import ram_budget as RB

        self.ram_budget = RB.enabled()
        self._mem_lock = threading.Lock()
        self._thread_jobs = 0

    def _wait_for_resources(self, spec: JobSpec) -> str | None:
        """Machine guard (incident 2026-10-24): wait while free RAM < 3 GB or the pagefile grows. Returns a reason to
        defer the job (jobs in DEFER_ON_PRESSURE are skipped right away), None to run it."""
        if not self.pressure_guard:
            return None
        why = machine_pressure()
        if not why:
            return None
        if spec.id in DEFER_ON_PRESSURE:
            return why
        log.warning("%s: machine under memory pressure (%s) - waiting up to %.0f min before starting", spec.id, why,
                    PRESSURE_WAIT_MAX_S / 60)
        deadline = time.monotonic() + PRESSURE_WAIT_MAX_S
        while why and time.monotonic() < deadline and not self.stop_event.wait(30):
            why = machine_pressure()
        if why:
            log.warning("%s: still under pressure (%s) - starting anyway", spec.id, why)
        return None

    def _acquire_slot(self, spec: JobSpec, opts: RunOptions | None = None) -> tuple[Any, str | None]:
        """Exclusive heavy slot (app.ops.heavy_slot, refresh-pipeline fixes 2026-10-26): build_serving_db runs alone;
        heavy (process) jobs wait while it runs; the hourly news jobs are deferred instead. Coverage v3 A2: a job with
        a budget >= heavy_slot.slot_gb() (7 GB) takes the heavy slot exclusively (no other job >= 3 GB beside it)."""
        from app.ops import heavy_slot as HS
        from app.ops import ram_budget as RB

        budget = RB.budget_of(spec)
        kind = HS.resource_class(spec.id, spec.executor, spec.resource, budget)
        if kind == "light" or not HS.enabled():
            return None, None
        ttl = spec.timeout_min * 60.0 * (spec.retries + 1) + 3600.0
        try:
            if kind == "exclusive":
                return HS.acquire_exclusive(spec.id, ttl, self.settings, self.stop_event), None
            if kind == "big":
                return HS.acquire_big(spec.id, budget, ttl, self.settings, self.stop_event,
                                      give_up_at=opts.start_by if opts is not None else None)
            return HS.acquire_heavy(spec.id, ttl, self.settings, self.stop_event,
                                    defer=spec.id in HS.DEFER_ON_EXCLUSIVE, mem_gb=budget,
                                    give_up_at=opts.start_by if opts is not None else None)
        except OSError as e:  # pragma: no cover - never block a job on the slot bookkeeping
            log.warning("%s: heavy slot unavailable (%s)", spec.id, e)
            return None, None

    def _slot_busy(self, spec: JobSpec) -> str | None:
        """Cheap pre-check before a job is admitted (coverage v3 A2): a big job (>= 7 GB) or a peer (>= 3 GB) is not
        submitted - and reserves no RAM - while a big job holds the heavy slot; a peer neither while a big job waits
        for it. Advisory only: the binding check is the announce-then-check of _acquire_slot."""
        from app.ops import heavy_slot as HS
        from app.ops import ram_budget as RB

        if not HS.enabled():
            return None
        budget = RB.budget_of(spec)
        kind = HS.resource_class(spec.id, spec.executor, spec.resource, budget)
        if kind not in ("big", "heavy") or budget < HS.peer_gb():
            return None
        try:
            bigs = [b for b in HS.holders("big", self.settings) if b.get("job_id") != spec.id]
        except OSError:  # pragma: no cover
            return None
        hold = [b for b in bigs if b.get("state") == "holding"]
        if hold:
            return f"heavy slot held by {hold[0].get('job_id')} ({float(hold[0].get('mem_gb') or 0):g} GB)"
        if kind == "heavy" and bigs:
            return f"heavy slot claimed by {bigs[0].get('job_id')} (waiting for running heavy jobs)"
        return None

    def _admit_ram(self, spec: JobSpec, blocked: dict[str, float], own_running: bool,
                   opts: RunOptions | None = None) -> tuple[bool, Any]:
        """RAM-budget admission (ram_fix 2026-10-31): (start?, reservation). A job starts when free RAM minus every
        runner's outstanding reservations minus the headroom covers its budget. While it waits, smaller jobs may
        overtake it for STARVE_S, then nothing else starts before it. With nothing of this run in flight, a light
        thread job never waits and any other job starts anyway after BIG360_RAM_WAIT_MAX_S.

        Coverage v3 A2: the 3 GB floor (app.ops.ram_budget.admission) is never bypassed by those escapes - a job
        below it waits, then is deferred (opts.ram_defer: returns (False, reason)) or starts in spill mode (the
        reservation carries fit_gb). Every wait is recorded with its reason (RB.note_wait)."""
        from app.ops import ram_budget as RB

        budget = RB.budget_of(spec)
        now = time.monotonic()
        try:
            ok, info = RB.check(budget, self.settings)
        except Exception as e:  # pragma: no cover - never block a job on the bookkeeping
            log.debug("ram check failed for %s: %s", spec.id, e)
            ok, info = True, {}
        # anti-starvation: once some job has waited STARVE_S, only jobs that have waited that long may start
        # (any of them - two starving jobs never block each other)
        starving = [b for b, t in blocked.items() if b != spec.id and now - t > RB.STARVE_S]
        self_starving = spec.id in blocked and now - blocked[spec.id] > RB.STARVE_S
        first = blocked.get(spec.id)
        wait_max = opts.ram_wait_max_s if opts is not None and opts.ram_wait_max_s is not None else RB.wait_max_s()
        d = RB.admission(budget, ok, info, light=spec.executor == "thread" and budget <= RB.DEFAULT_THREAD_GB,
                         own_running=own_running, waited_s=0.0 if first is None else now - first, wait_max=wait_max,
                         starving=bool(starving) and not self_starving,
                         defer=bool(opts is not None and opts.ram_defer))
        if d.action == "wait":
            blocked.setdefault(spec.id, now)
            if first is None and d.kind != "starve":
                log.info("%s: %s", spec.id, d.reason)
            RB.note_wait(spec.id, d.kind, d.reason, self.settings, info)
            return False, None
        blocked.pop(spec.id, None)
        RB.clear_wait(spec.id, self.settings)
        if d.action == "defer":
            log.warning("%s: deferred - %s", spec.id, d.reason)
            return False, f"deferred: {d.reason}"
        if d.note == "alone":
            log.info("%s: starting alone - budget %.1f GB, free %s GB, headroom %.0f GB (nothing else reserved)",
                     spec.id, budget, info.get("free_gb"), info.get("headroom_gb", 0.0))
        elif d.note:
            log.warning("%s: %s", spec.id, d.reason)
        ttl = spec.timeout_min * 60.0 * (spec.retries + 2) + 3600.0
        res = RB.reserve(spec.id, budget, spec.executor, ttl, self.settings)
        if d.fit_gb is not None:
            res.fit_gb = round(float(d.fit_gb), 2)
            res.record["fit_gb"] = res.fit_gb
        return True, res

    # ------------------------------------------------------------------ public API
    def run_dag(self, dag: str, opts: RunOptions | None = None) -> RunReport:
        opts = opts or RunOptions()
        ids = self.registry.dag_jobs(dag)
        if opts.dry_run:
            return self._dry_run(dag, ids, opts)
        if opts.wait_for_dags:
            self._wait_for_dags(DAG_WAITS.get(dag, ()))
        lock = FileLock(str(self.settings.locks_dir / f"dag_{dag}.lock"), timeout=0)
        self.settings.locks_dir.mkdir(parents=True, exist_ok=True)
        try:
            lock.acquire()
        except Timeout:
            log.warning("dag %s is already running in another thread/process; not starting", dag)
            as_of = opts.as_of or default_as_of(self.settings)
            return RunReport(dag, None, as_of, {j: JobOutcome(j, "skipped", "already_running") for j in ids},
                             datetime.now(UTC), datetime.now(UTC))
        try:
            pre: dict[str, JobOutcome] = {}
            if opts.resume:
                last = self.store.latest_dag_run(dag)
                if last:
                    dag_run_id, as_of, statuses = last
                    opts = replace(opts, dag_run_id=opts.dag_run_id or dag_run_id, as_of=opts.as_of or as_of)
                    for j, st in statuses.items():
                        if st in OK_STATUSES and j in ids:
                            pre[j] = JobOutcome(j, "skipped", "resumed", message=f"{st} in {dag_run_id}")
                    log.info("resuming %s (%d jobs already done)", dag_run_id, len(pre))
            opts = replace(opts, dag_run_id=opts.dag_run_id or new_dag_run_id(dag))
            return self._run_graph(dag, ids, opts, pre)
        finally:
            lock.release()

    def run_jobs(self, job_ids: Iterable[str], opts: RunOptions | None = None, with_upstream: bool = False,
                 with_downstream: bool = False) -> RunReport:
        opts = opts or RunOptions()
        ids = list(dict.fromkeys(job_ids))
        unknown = [j for j in ids if j not in self.registry.jobs]
        if unknown:
            raise KeyError(f"unknown job(s): {unknown}")
        extra: list[str] = []
        for j in ids:
            if with_upstream:
                extra += self.registry.upstream(j)
            if with_downstream:
                extra += self.registry.downstream(j)
        ids = [j for j in self.registry.jobs if j in set(ids) | set(extra)]
        label = ",".join(job_ids)
        if opts.dry_run:
            return self._dry_run(label, ids, opts)
        return self._run_graph(label, ids, opts, {})

    def run_job(self, job_id: str, opts: RunOptions | None = None) -> JobOutcome:
        return self.run_jobs([job_id], opts).outcomes[job_id]

    # ------------------------------------------------------------------ internals
    def _wait_for_dags(self, dags: Iterable[str]) -> None:
        for other in dags:
            lk = FileLock(str(self.settings.locks_dir / f"dag_{other}.lock"), timeout=0)
            deadline = time.monotonic() + (SYNC_WAIT_MAX_S if other == "sync" else DAG_WAIT_MAX_S)
            asked = False
            while not self.stop_event.is_set():
                try:
                    lk.acquire()
                    lk.release()
                    break
                except Timeout:
                    if other == "sync" and not asked:   # ask the startup sync to yield (it exits after its jobs)
                        asked = True
                        try:
                            from app.ops.paths import ops_state_dir

                            (ops_state_dir(self.settings) / "sync.YIELD").write_text(
                                f"refresh waiting {datetime.now(UTC).isoformat(timespec='seconds')}", encoding="utf-8")
                        except OSError:  # pragma: no cover
                            pass
                    if time.monotonic() > deadline:
                        log.warning("gave up waiting for dag %s", other)
                        break
                    log.info("waiting for dag %s to finish", other)
                    self.stop_event.wait(30)

    def _dry_run(self, label: str, ids: list[str], opts: RunOptions) -> RunReport:
        as_of = opts.as_of or default_as_of(self.settings)
        outcomes = {}
        for j in self.registry.topo_order(ids):
            ok, why = check_available(self.registry.jobs[j])
            outcomes[j] = JobOutcome(j, "skipped", "dry_run", message=None if ok else why)
        return RunReport(label, opts.dag_run_id, as_of, outcomes, datetime.now(UTC), datetime.now(UTC))

    def _run_graph(self, label: str, ids: list[str], opts: RunOptions, pre: dict[str, JobOutcome]) -> RunReport:
        reg = self.registry
        as_of = opts.as_of or default_as_of(self.settings)
        idset = set(ids)
        order = reg.topo_order(ids)
        prio = reg.remaining_path(ids, str(opts.params.get("universe", "mvp")))
        max_workers = max(1, opts.max_workers or self.settings.worker_max_jobs)
        max_proc = max(1, opts.max_process_jobs or self.settings.worker_max_process_jobs)
        report = RunReport(label, opts.dag_run_id, as_of, {}, datetime.now(UTC))
        outcomes: dict[str, JobOutcome] = dict(pre)
        pending = [j for j in order if j not in outcomes]
        running: dict[Future, str] = {}
        lanes: Counter[str] = Counter()
        n_proc = 0
        first_run_id = opts.run_id
        reservations: dict[Future, Any] = {}
        blocked: dict[str, float] = {}       # job -> monotonic time it was first held back by the RAM budget
        log.info("run %s dag_run_id=%s as_of=%s jobs=%d workers=%d", label, opts.dag_run_id, as_of, len(pending), max_workers)

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="job") as pool:
            while pending or running:
                started_any = False
                if self.stop_event.is_set():
                    for j in pending:
                        outcomes[j] = JobOutcome(j, "skipped", "stopped", message="worker shutting down")
                    pending = []
                # resolve skips caused by upstream failures (cascades in one pass thanks to topo order)
                for j in list(pending):
                    deps = reg.deps_within(j, idset)
                    if not all(d in outcomes for d in deps):
                        continue
                    soft = reg.jobs[j].soft_depends_on
                    blockers = [d for d in deps if outcomes[d].blocks_dependents and d not in soft]
                    if blockers and reg.jobs[j].on_upstream_failure == "skip":
                        msg = f"upstream not ok: {', '.join(blockers)}"
                        outcomes[j] = JobOutcome(j, "skipped", "upstream_failed", message=msg)
                        self._record_skip(reg.jobs[j], opts, as_of, "UpstreamFailed", msg)
                        pending.remove(j)
                ready = [j for j in pending if all(d in outcomes for d in reg.deps_within(j, idset))]
                ready.sort(key=lambda j: -prio.get(j, 0))
                for b in [b for b in blocked if b not in ready]:   # skipped / no longer ready: never "starving"
                    blocked.pop(b, None)
                for j in ready:
                    if len(running) >= max_workers:
                        break
                    spec = reg.jobs[j]
                    if any(lanes[s] >= SOURCE_MAX_JOBS.get(s, 2) for s in spec.sources):
                        continue
                    if spec.executor == "process" and n_proc >= max_proc:
                        continue
                    res = None
                    if opts.start_by is not None and time.time() > opts.start_by:   # coverage v3 A3
                        msg = "deferred: the run's start budget ran out before the job could start"
                        outcomes[j] = JobOutcome(j, "skipped", "deferred", error_type="StartBudget", message=msg)
                        self._record_skip(spec, opts, as_of, "StartBudget", msg)
                        pending.remove(j)
                        continue
                    busy = self._slot_busy(spec) if not opts.dry_run else None
                    if busy:   # coverage v3 A2: wait for the heavy slot without holding a RAM reservation
                        from app.ops import ram_budget as RB

                        RB.note_wait(spec.id, "slot", busy, self.settings)
                        continue
                    if self.ram_budget and not opts.dry_run:
                        ok, res = self._admit_ram(spec, blocked, bool(running), opts)
                        if not ok:
                            if isinstance(res, str):   # coverage v3 A2: deferred below the RAM floor
                                outcomes[j] = JobOutcome(j, "skipped", "deferred", error_type="RamFloor", message=res)
                                self._record_skip(spec, opts, as_of, "RamFloor", res)
                                pending.remove(j)
                            continue
                    for s in spec.sources:
                        lanes[s] += 1
                    if spec.executor == "process":
                        n_proc += 1
                    run_id, first_run_id = first_run_id, None
                    # an upstream job that actually ran and wrote rows in this run invalidates the idempotency skip
                    # (same as_of, new inputs: e.g. a weekend re-run must still rebuild scoring/serving)
                    upstream_changed = any(
                        outcomes[d].status in OK_STATUSES and outcomes[d].reason is None and outcomes[d].rows_written > 0
                        for d in reg.deps_within(j, idset))
                    fut = pool.submit(self._execute, spec, opts, as_of, run_id, upstream_changed, res)
                    running[fut] = j
                    reservations[fut] = res
                    pending.remove(j)
                    started_any = True
                if not running:
                    if pending and not ready:  # cannot happen with a validated DAG; guard against infinite loop
                        for j in pending:
                            outcomes[j] = JobOutcome(j, "skipped", "upstream_failed", message="unresolvable dependencies")
                        pending = []
                    elif pending and not started_any:   # held back by the RAM budget: poll, do not spin
                        self.stop_event.wait(2.0)
                    continue
                done, _ = wait(list(running), timeout=1.0, return_when=FIRST_COMPLETED)
                for f in done:
                    j = running.pop(f)
                    r = reservations.pop(f, None)
                    if r is not None:
                        r.release()
                    spec = reg.jobs[j]
                    for s in spec.sources:
                        lanes[s] -= 1
                    if spec.executor == "process":
                        n_proc -= 1
                    try:
                        outcomes[j] = f.result()
                    except Exception as e:  # pragma: no cover - _execute never raises
                        outcomes[j] = JobOutcome(j, "failed", error_type=type(e).__name__, message=str(e))
        report.outcomes = {j: outcomes[j] for j in order if j in outcomes}
        report.finished_at = datetime.now(UTC)
        log.info("run %s finished status=%s %s", label, report.status, report.counts())
        return report

    def _base_row(self, spec: JobSpec, opts: RunOptions, as_of: date, run_id: str, attempt: int,
                  params: dict[str, Any], run_key: str | None) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "run_id": run_id, "year_month": now.strftime("%Y-%m"), "job_id": spec.id, "dag_run_id": opts.dag_run_id,
            "trigger": opts.trigger if opts.trigger in ("schedule", "manual", "backfill", "dependency", "api") else "manual",
            "status": "running", "attempt": attempt, "started_at": now,
            "params": json.dumps({"as_of": as_of.isoformat(), **params}, default=str, sort_keys=True),
            "as_of_date": as_of, "run_key": run_key, "tables_written": list(spec.writes) or None,
            "host": self.host, "code_version": __version__,
        }

    def _record_skip(self, spec: JobSpec, opts: RunOptions, as_of: date, error_type: str, msg: str,
                     run_id: str | None = None) -> str:
        rid = run_id or new_run_id()
        row = self._base_row(spec, opts, as_of, rid, 1, {**spec.params, **opts.params}, None)
        row.update(status="skipped", finished_at=row["started_at"], duration_s=0.0, error_type=error_type,
                   error_message=msg[:2000], tables_written=None)
        self.store.write(row)
        return rid

    def _execute(self, spec: JobSpec, opts: RunOptions, as_of: date, first_run_id: str | None,
                 upstream_changed: bool = False, reservation: Any = None) -> JobOutcome:
        """Run one job with locks, idempotency, retries and bookkeeping. Never raises."""
        t0 = time.monotonic()
        params = {**spec.params, **opts.params}
        run_key = make_run_key(spec.id, as_of, params)
        try:
            if not opts.force and not upstream_changed:
                prev = self.store.succeeded(run_key)
                if prev:
                    log.info("%s: already succeeded for %s (run %s); skipping (use force)", spec.id, run_key, prev["run_id"])
                    return JobOutcome(spec.id, "skipped", "idempotent", message=f"already succeeded in {prev['run_id']}")
            ok, why = check_available(spec)
            if not ok:
                log.info("%s unavailable: %s", spec.id, why)
                rid = self._record_skip(spec, opts, as_of, "JobUnavailable", why or "unavailable", first_run_id) \
                    if opts.record_unavailable else None
                return JobOutcome(spec.id, "skipped", "unavailable", run_ids=[rid] if rid else [], message=why)
            lock = job_lock(spec.id, self.settings)
            try:
                lock.acquire(timeout=0)
            except Timeout:
                msg = "another run of this job is in progress"
                rid = self._record_skip(spec, opts, as_of, "AlreadyRunning", msg, first_run_id)
                return JobOutcome(spec.id, "skipped", "already_running", run_ids=[rid], message=msg)
            slot = None
            try:
                slot, why = self._acquire_slot(spec, opts)
                if why:
                    msg = f"deferred: {why}; the next scheduled run retries"
                    log.warning("%s %s", spec.id, msg)
                    rid = self._record_skip(spec, opts, as_of, "HeavySlotBusy", msg, first_run_id)
                    return JobOutcome(spec.id, "skipped", "deferred", run_ids=[rid], message=msg)
                why = self._wait_for_resources(spec) if spec.executor == "process" else None
                if why:
                    msg = f"deferred: machine under memory pressure ({why}); the next scheduled run retries"
                    log.warning("%s %s", spec.id, msg)
                    rid = self._record_skip(spec, opts, as_of, "ResourcePressure", msg, first_run_id)
                    return JobOutcome(spec.id, "skipped", "deferred", run_ids=[rid], message=msg)
                return self._attempts(spec, opts, as_of, params, run_key, first_run_id, t0, reservation)
            finally:
                if slot is not None:
                    slot.release()
                lock.release()
        except Exception as e:  # pragma: no cover - defensive
            log.exception("runner error in %s", spec.id)
            return JobOutcome(spec.id, "failed", error_type=type(e).__name__, message=redact(str(e))[:500],
                              duration_s=time.monotonic() - t0)

    def _attempts(self, spec: JobSpec, opts: RunOptions, as_of: date, params: dict[str, Any], run_key: str,
                  first_run_id: str | None, t0: float, reservation: Any = None) -> JobOutcome:
        retries = spec.retries if opts.retries is None else opts.retries
        outcome = JobOutcome(spec.id, "failed")
        watermark = self.store.last_watermark(spec.id)
        oom_retried = False
        attempt = 0
        while True:
            attempt += 1
            run_id = (first_run_id if attempt == 1 and first_run_id else new_run_id())
            outcome.run_ids.append(run_id)
            outcome.attempts = attempt
            row = self._base_row(spec, opts, as_of, run_id, attempt, params, run_key)
            self.store.write(row)
            ctx = JobContext(job_id=spec.id, run_id=run_id, as_of=as_of, params=dict(params), watermark=watermark,
                             dag_run_id=opts.dag_run_id, trigger=row["trigger"], settings=self.settings,
                             log=get_logger(f"app.job.{spec.id}"))
            mem: dict[str, Any] = {}
            with job_log_handler(spec.id, run_id) as log_path:
                ctx.log.info("start %s attempt %d as_of=%s params=%s", spec.id, attempt, as_of, params)
                result, err, retryable = self._invoke(spec, ctx, str(log_path), reservation, mem)
                if err:
                    ctx.log.error("%s failed: %s", spec.id, err[0] + ": " + err[1])
                    if err[2]:
                        ctx.log.error("traceback:\n%s", err[2])
                else:
                    ctx.log.info("end %s status=%s rows=%s msg=%s", spec.id, result.status, result.rows_written, result.message)
                if mem:
                    ctx.log.info("mem %s: %s", spec.id, json.dumps(
                        {k: v for k, v in mem.items() if k not in ("job_id", "run_id")}, default=str))
            if mem:
                _record_mem(self.settings, {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "job_id": spec.id,
                                            "run_id": run_id, "dag_run_id": opts.dag_run_id, "attempt": attempt,
                                            "status": "failed" if err else result.status, **mem})
            fin = datetime.now(UTC)
            row.update(finished_at=fin, duration_s=(fin - row["started_at"]).total_seconds(),
                       log_path=str(log_path.relative_to(self.settings.logs_dir)).replace("\\", "/"))
            if err:
                row.update(status="failed", error_type=err[0], error_message=redact(err[1])[:2000])
                outcome.error_type, outcome.message = err[0], redact(err[1])[:500]
                outcome.status = "failed"
            else:
                st = result.status if result.status in ("success", "partial", "failed", "skipped") else "success"
                row.update(status=st, rows_written=result.total_rows, entities_ok=result.entities_ok,
                           entities_failed=result.entities_failed, http_requests=result.http_requests,
                           cache_hits=result.cache_hits,
                           watermark=result.watermark if st in OK_STATUSES else None,
                           tables_written=sorted(result.rows_written) or list(spec.writes) or None,
                           error_message=redact(result.message)[:2000] if result.message else None,
                           error_type="JobReportedFailure" if st == "failed" else None)
                outcome.status, outcome.message = st, result.message
                outcome.rows_written = result.total_rows
                outcome.error_type = row["error_type"]
                if st == "skipped":
                    outcome.reason = "job_skipped"
                retryable = retryable and result.retryable
            self.store.write(row)
            if err and err[0] == "MemoryLimitExceeded":
                # ram_fix 2026-10-31: one retry with smaller chunks where the job supports it; never the same run again
                if spec.oom_params and not oom_retried and not self.stop_event.is_set():
                    oom_retried = True
                    params = {**params, **spec.oom_params}
                    log.warning("%s attempt %d %s; retrying once with %s", spec.id, attempt, err[1], spec.oom_params)
                    continue
                break
            if outcome.status != "failed" or not retryable or attempt > retries or self.stop_event.is_set():
                break
            backoff = spec.retry_backoff_s * (2 ** (attempt - 1)) * opts.backoff_scale
            log.warning("%s attempt %d failed (%s); retrying in %.0fs", spec.id, attempt, outcome.message, backoff)
            if backoff > 0 and self.stop_event.wait(backoff):
                break
        outcome.duration_s = time.monotonic() - t0
        return outcome

    def _invoke(self, spec: JobSpec, ctx: JobContext, log_path: str, reservation: Any = None,
                mem: dict[str, Any] | None = None) -> tuple[JobResult, tuple[str, str, str] | None, bool]:
        """Returns (result, error=(type, message, traceback) | None, retryable). `mem` receives the attempt's memory
        measurements (ram_fix 2026-10-31)."""
        # params.timeout_min overrides the contract timeout for one run (e.g. prices_finalize mode=full back-fills)
        try:
            tmin = float((ctx.params or {}).get("timeout_min") or spec.timeout_min)
        except (TypeError, ValueError):
            tmin = spec.timeout_min
        timeout_s = max(0.1, tmin * 60.0)
        mem = mem if mem is not None else {}
        if spec.executor == "process":
            return self._invoke_process(spec, ctx, timeout_s, log_path, reservation, mem)
        return self._invoke_thread(spec, ctx, timeout_s, mem)

    def _invoke_thread(self, spec: JobSpec, ctx: JobContext, timeout_s: float, mem: dict[str, Any] | None = None):
        holder: dict[str, Any] = {}
        mem = mem if mem is not None else {}
        sampler = _MemSampler(None)
        with self._mem_lock:
            self._thread_jobs += 1
            concurrent = self._thread_jobs

        def target() -> None:
            register_job_thread(spec.id, ctx.run_id)
            try:
                fn = resolve_callable(spec.target)
                holder["result"] = _coerce_result(fn(ctx))
            except BaseException as e:  # noqa: BLE001
                holder["error"] = (type(e).__name__, str(e), traceback.format_exc())
                holder["retryable"] = not isinstance(e, (JobUnavailable, JobCancelled, JobKilled, KeyboardInterrupt))

        th = threading.Thread(target=target, name=f"job-{spec.id}", daemon=True)
        sampler.sample(force=True)
        th.start()
        register_job_thread(spec.id, ctx.run_id, th.ident)
        deadline = time.monotonic() + timeout_s
        timed_out = False
        try:
            while th.is_alive():
                th.join(timeout=min(SAMPLE_S, max(0.01, deadline - time.monotonic())))
                sampler.sample()
                with self._mem_lock:
                    concurrent = max(concurrent, self._thread_jobs)
                if time.monotonic() >= deadline and th.is_alive():
                    timed_out = True
                    ctx.cancel_event.set()
                    th.join(timeout=min(30.0, timeout_s))
                    break
                if self.stop_event.is_set():
                    ctx.cancel_event.set()
        finally:
            with self._mem_lock:
                self._thread_jobs -= 1
                last = self._thread_jobs == 0
            sampler.sample(force=True)
            mem.update(sampler.summary(), executor="thread", concurrent_thread_jobs=concurrent,
                       budget_gb=_budget(spec))
            # ram_fix 2026-10-31: hand what the job freed back to the OS (gc + heap compaction; malloc_trim has no
            # Windows equivalent). Only when no other thread job runs: a collection mid-job of another would only
            # cost time, and its working set would blur the measurement.
            if last and not th.is_alive() and os.environ.get("BIG360_THREAD_RELEASE", "1") != "0":
                try:
                    from app.ops.winsys import release_memory

                    rel = release_memory(trim_working_set=False)
                    mem.update(after_release_ws_mb=_r(rel.get("after_mb")),
                               after_release_private_mb=_r(rel.get("private_after_mb")))
                except Exception:  # pragma: no cover
                    pass
        if timed_out:
            # F18: the cooperative cancel was ignored -> inject JobKilled until the thread exits, then retry
            if not th.is_alive() or stop_thread(th, JobKilled, wait_s=self.kill_wait_s):
                return JobResult(status="failed"), ("TimeoutError", f"timed out after {timeout_s / 60:.2f} min (thread stopped)", ""), True
            # blocked inside one C call: not retried while the zombie thread may still write
            return JobResult(status="failed"), ("TimeoutError", f"timed out after {timeout_s / 60:.2f} min (thread still running; cancellation requested)", ""), False
        if "error" in holder:
            return JobResult(status="failed"), holder["error"], holder.get("retryable", True)
        return holder.get("result", JobResult()), None, True

    def _invoke_process(self, spec: JobSpec, ctx: JobContext, timeout_s: float, log_path: str,
                        reservation: Any = None, mem: dict[str, Any] | None = None):
        from app.ops import ram_budget as RB

        mem = mem if mem is not None else {}
        budget = RB.budget_of(spec)
        # ram_fix 2026-10-31: DuckDB memory_limit / threads, Polars / BLAS pools sized from the budget (child only)
        env = RB.job_env(budget, self.settings, fit_gb=getattr(reservation, "fit_gb", None)) if spec.mem_gb else {}
        mpc = mp.get_context("spawn")
        recv, send = mpc.Pipe(duplex=False)
        payload = {"job_id": ctx.job_id, "run_id": ctx.run_id, "as_of": ctx.as_of.isoformat(), "params": ctx.params,
                   "watermark": ctx.watermark, "dag_run_id": ctx.dag_run_id, "trigger": ctx.trigger}
        proc = mpc.Process(target=_process_entry, args=(spec.target, payload, send, log_path),
                           name=f"job-{spec.id}", daemon=False)
        _start_in_own_group(proc, env)
        send.close()
        job = _job_object(proc, f"job-{spec.id}")
        limit_mb = None
        if job is not None and spec.mem_gb and os.environ.get("BIG360_JOB_MEM_CAP", "1") != "0":
            if job.set_memory_limit(int(budget * 1024**3)):
                limit_mb = budget * 1024.0
            else:  # pragma: no cover
                log.warning("%s: job memory cap not applied (error %s)", spec.id, job.error)
        if reservation is not None:
            reservation.set_child(proc.pid)
        sampler = _MemSampler(job, None if job is not None else proc.pid)
        try:
            return self._wait_process(spec, proc, recv, job, timeout_s, sampler, limit_mb, mem)
        finally:
            sampler.sample(force=True)
            mem.update(sampler.summary(), executor="process", budget_gb=budget,
                       caps={k: v for k, v in env.items() if k in ("BIG360_DUCKDB_MEMORY", "POLARS_MAX_THREADS")}
                       or None, exitcode=proc.exitcode)
            _reap_job(job, spec.id)   # every descendant dies with the job: end, failure or timeout
            if reservation is not None:
                reservation.set_child(None)
            try:
                recv.close()
            except Exception:  # pragma: no cover
                pass

    def _wait_process(self, spec: JobSpec, proc: Any, recv: Any, job: Any, timeout_s: float,
                      sampler: _MemSampler | None = None, limit_mb: float | None = None,
                      mem: dict[str, Any] | None = None):
        deadline = time.monotonic() + timeout_s
        msg = None
        oom_kill = False
        sampler = sampler or _MemSampler(job)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if self.stop_event.is_set():
                remaining = min(remaining, 60.0)  # give the child a minute on shutdown, then kill
                deadline = time.monotonic() + remaining
            if recv.poll(min(SAMPLE_S, remaining)):
                try:
                    msg = recv.recv()
                except EOFError:
                    msg = None
                break
            sampler.sample()
            peak = sampler.peak_commit_mb
            if limit_mb and peak and peak >= limit_mb * OOM_NEAR and proc.is_alive():
                oom_kill = True    # ran into its cap: stop it now instead of letting it thrash on failed allocations
                break
            if not proc.is_alive():
                if recv.poll(0.2):
                    try:
                        msg = recv.recv()
                    except EOFError:
                        msg = None
                break
        sampler.sample(force=True)
        peak = sampler.peak_commit_mb
        if oom_kill:
            _kill_tree(proc, job)
            if mem is not None:
                mem["oom_killed"] = True
            log.error("%s: %s", spec.id, oom_message(peak or 0.0, limit_mb or 0.0))
            return JobResult(status="failed"), ("MemoryLimitExceeded", oom_message(peak or 0.0, limit_mb or 0.0), ""), False
        if msg is None and proc.is_alive():
            _kill_tree(proc, job)
            if self.stop_event.is_set():
                return JobResult(status="failed"), ("JobCancelled", "stopped: the runner is shutting down (process killed "
                                                    "after 60 s)", ""), False
            return JobResult(status="failed"), ("TimeoutError", f"timed out after {timeout_s / 60:.1f} min (process killed)", ""), True
        proc.join(30)
        if proc.is_alive():  # pragma: no cover
            proc.terminate()
        if msg is None:
            err = ("ProcessDied", f"child exited with code {proc.exitcode} without a result", "")
            if _is_oom(err, proc.exitcode, peak, limit_mb):
                if mem is not None:
                    mem["oom_killed"] = True
                return JobResult(status="failed"), ("MemoryLimitExceeded", oom_message(peak or 0.0, limit_mb or 0.0, err[1]), ""), False
            return JobResult(status="failed"), err, True
        if msg[0] == "ok":
            return _coerce_result(msg[1]), None, True
        _, etype, emsg, tb = msg
        if _is_oom((etype, emsg, ""), proc.exitcode, peak, limit_mb):
            if mem is not None:
                mem["oom_killed"] = True
            return JobResult(status="failed"), ("MemoryLimitExceeded", oom_message(peak or 0.0, limit_mb or 0.0, f"{etype}: {emsg}"), tb), False
        return JobResult(status="failed"), (etype, emsg, tb), etype not in ("JobUnavailable", "JobCancelled")


# ------------------------------------------------------------------------------------------------ API helpers


def next_run_times(registry: JobRegistry | None = None, settings: Settings | None = None) -> dict[str, str | None]:
    """{dag: next fire time ISO} of the ACTIVE schedule profile (app.ops.schedule; no running worker needed); falls
    back to the jobs.yaml crons if the profile cannot be resolved."""
    reg = registry or JobRegistry.load(settings=settings)
    try:
        from app.ops.schedule import active_profile, next_runs_by_dag, profile_entries

        prof = next_runs_by_dag(profile_entries(active_profile(settings)[0], reg, settings))
        return {dag: prof.get(dag) for dag in reg.dags}
    except Exception:  # pragma: no cover - fall back to the raw crons
        pass
    tz = scheduler_tz(settings, reg)
    now = datetime.now(tz)
    out: dict[str, str | None] = {}
    for dag, spec in reg.dags.items():
        cron = (spec or {}).get("cron")
        if not cron:
            out[dag] = None
            continue
        nxt = cron_trigger(cron, tz).get_next_fire_time(None, now)
        out[dag] = nxt.isoformat() if nxt else None
    return out


def job_definitions(universe: str = "mvp", settings: Settings | None = None) -> list[dict[str, Any]]:
    """Rows for GET /system/jobs (contracts/api.yaml JobDefinition): registry + last run + next scheduled run."""
    s = settings or get_settings()
    reg = JobRegistry.load(settings=s)
    store = JobRunStore(s)
    runs = store.read()
    last: dict[str, dict[str, Any]] = {}
    if not runs.is_empty():
        for r in runs.sort("started_at", descending=True).unique(subset=["job_id"], keep="first").iter_rows(named=True):
            last[r["job_id"]] = r
    nxt = next_run_times(reg, s)
    out = []
    for j in reg.jobs.values():
        lr = last.get(j.id)
        out.append({
            "job_id": j.id, "dag": j.dag, "module": j.module, "owner": j.owner, "depends_on": list(j.depends_on),
            "writes": list(j.writes), "est_min": j.est_min(universe),
            "available": check_available(j)[0],
            "last_run": None if lr is None else {
                k: (v.isoformat() if isinstance(v, (datetime, date)) else v) for k, v in lr.items()
                if k in ("run_id", "job_id", "dag_run_id", "trigger", "status", "attempt", "started_at", "finished_at",
                         "duration_s", "rows_written", "entities_ok", "entities_failed", "error_message")
            },
            "next_run_at": nxt.get(j.dag),
        })
    return out
