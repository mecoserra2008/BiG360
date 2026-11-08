"""Machine-wide RAM budget for job admission (ram_fix 2026-10-31). Owner: platform.

Before 2026-10-31 the runner started up to 6 jobs at once with no idea of their memory: thread jobs (Polars, DuckDB at
12 GB / 16 threads, numpy) ran inside the long-lived worker / sync process, kept what they allocated, and several ran
side by side, so the worker reached 13-18 GB and the machine dropped to ~0.1 GB free during the nightly / weekly DAGs.

Rules
* every job has a budget: contracts/jobs.yaml `mem_gb`, else DEFAULT_THREAD_GB / DEFAULT_PROCESS_GB by executor;
* a process job runs in its own Job Object whose committed memory is capped at the budget (runner);
* admission: a job starts only when   free RAM - outstanding reservations - headroom >= its budget,
  where a reservation is a running job's budget minus what it already uses (its job-process tree's working set;
  a thread job counts its whole budget). Reservations are small JSON files in data/state/ops/ram/ written by every
  runner (worker, startup sync, `app.cli jobs run`), so two runners never both count the same free RAM;
* headroom BIG360_RAM_HEADROOM_GB (default 4 GB: the API, realtime and quotes keep working, the target of the fix);
* progress: a job blocked while nothing of its own runs starts anyway after BIG360_RAM_WAIT_MAX_S (default 30 min),
  with a warning (its Job Object cap still protects the machine).
Markers carry the owner pid and an expiry: a crashed runner never holds RAM (dead pid or expired = removed).
BIG360_RAM_BUDGET=0 disables admission (budgets / caps still apply).

Coverage v3 A2 (2026-11; 87 of 950 measured runs saw < 2 GB free, the "alone" and "max wait" escapes started jobs
whose budget did not fit):
* floor: a job of FLOOR_MIN_BUDGET_GB or more also needs   free RAM - outstanding reservations - its budget >=
  BIG360_RAM_FLOOR_GB (default 3 GB, 0 = off). The escapes above bypass the 4 GB headroom only, never the floor:
  - other runners / jobs of this run hold reservations -> the job waits (their end frees RAM) up to the max wait,
    then it is deferred when the caller asked for it (RunOptions.ram_defer: the startup sync carries it to its next
    pass) or BIG360_RAM_FLOOR_STRICT=1, else it starts in spill mode;
  - nothing of ours holds RAM, or even ending every live reservation (check() reclaim_gb) would leave the job below
    the floor (waiting cannot help: the rest belongs to other programs) -> spill mode right away (deferred only with
    BIG360_RAM_FLOOR_STRICT=1);
  - spill mode: the job keeps its Job Object cap, but its DuckDB memory_limit is sized from what fits above the floor
    (free - outstanding - floor; DuckDB spills to its temp_directory) and Polars runs on the streaming engine;
* spill-to-disk for heavy jobs (budget >= SPILL_GB, the heavy-slot class): job_env() always sets the Polars
  streaming engine affinity (POLARS_ENGINE_AFFINITY=streaming, falls back to in-memory per query; BIG360_POLARS_
  STREAMING=0 off) and a spill directory next to the DuckDB one; DuckDB memory_limit / temp_directory were already
  applied to every job connection (app.core.storage.duckdb_connect, serving_build_connection);
* wait reasons: every runner process writes data/state/ops/admission/<pid>.json {job: {kind, reason, since}} while
  a job waits for RAM, the floor or the heavy slot (waits() merges the live ones: GET /system/status, sync status).
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

DEFAULT_THREAD_GB = 0.5
DEFAULT_PROCESS_GB = 2.0
DEFAULT_HEADROOM_GB = 4.0
DEFAULT_WAIT_MAX_S = 30 * 60.0
#: a ready job blocked by RAM for longer than this stops smaller jobs from overtaking it (no starvation)
STARVE_S = 10 * 60.0


def enabled() -> bool:
    return os.environ.get("BIG360_RAM_BUDGET", "1") != "0"


def headroom_gb() -> float:
    try:
        return float(os.environ.get("BIG360_RAM_HEADROOM_GB", DEFAULT_HEADROOM_GB))
    except ValueError:
        return DEFAULT_HEADROOM_GB


def wait_max_s() -> float:
    try:
        return float(os.environ.get("BIG360_RAM_WAIT_MAX_S", DEFAULT_WAIT_MAX_S))
    except ValueError:
        return DEFAULT_WAIT_MAX_S


def budget_of(spec: Any) -> float:
    """GB budget of a job spec (jobs.yaml mem_gb, else the executor default)."""
    v = getattr(spec, "mem_gb", None)
    if v:
        return float(v)
    return DEFAULT_PROCESS_GB if getattr(spec, "executor", "thread") == "process" else DEFAULT_THREAD_GB


# ------------------------------------------------------------------------------------------------ per-job caps


def parse_size_gb(v: str | float | int | None) -> float | None:
    """'12GB' (DuckDB: 10^9 bytes) / '7GiB' / '512MB' / 4 -> GiB."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?)(I?B)?\s*", str(v).upper())
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2) or "G"
    binary = (m.group(3) or "").startswith("I")
    base = 1024.0 if binary else 1000.0
    mult = {"K": base, "M": base**2, "G": base**3, "T": base**4}[unit]
    return n * mult / 1024**3


def threads_for(budget_gb: float, cpu: int | None = None) -> int:
    """Worker threads for Polars / DuckDB / BLAS in a job of this budget: ~1.5 per GB, 2..12, never above the cores.
    Fewer threads = smaller per-thread hash tables and buffers (incident 2026-10-24: one pool thread per core)."""
    cpu = cpu or os.cpu_count() or 8
    return max(2, min(12, cpu, int(math.ceil(budget_gb * 1.5))))


#: coverage v3 A2: jobs at or above this budget spill (Polars streaming engine); the heavy-slot threshold
SPILL_GB = 7.0


def polars_streaming_enabled() -> bool:
    return os.environ.get("BIG360_POLARS_STREAMING", "1") != "0"


def job_env(budget_gb: float, settings: Settings | None = None, fit_gb: float | None = None) -> dict[str, str]:
    """Environment of a process job with budget `budget_gb`: DuckDB memory_limit / threads (job connections 60 % of
    the budget, the serving build 70 %, never above their configured values), Polars / OpenMP / MKL / OpenBLAS
    thread pools, and BIG360_JOB_MEM_GB for jobs that size their chunks from it.

    Spill mode (coverage v3 A2) - heavy jobs (budget >= SPILL_GB) and jobs admitted below the RAM floor (`fit_gb`:
    the RAM that fits above the floor): Polars streaming engine (spills to POLARS_TEMP_DIR), and with `fit_gb` the
    DuckDB limits are sized from fit_gb instead of the budget (DuckDB spills to its temp_directory). The Job Object
    cap stays the budget."""
    s = settings or get_settings()
    th = threads_for(budget_gb)
    base = budget_gb if fit_gb is None else max(0.5, min(budget_gb, float(fit_gb)))
    duck = min(parse_size_gb(s.duckdb_memory_limit) or 12 * 0.93, max(0.5, base * 0.6))
    serving = min(parse_size_gb(getattr(s, "serving_duckdb_memory_limit", "7GB")) or 6.5, max(0.5, base * 0.7))
    env = {
        "BIG360_JOB_MEM_GB": f"{budget_gb:g}",
        "BIG360_DUCKDB_MEMORY": f"{int(duck * 1024)}MiB",
        "BIG360_DUCKDB_THREADS": str(min(th, int(s.duckdb_threads or th))),
        "BIG360_SERVING_DUCKDB_MEMORY": f"{int(serving * 1024)}MiB",
        "BIG360_SERVING_DUCKDB_THREADS": str(min(th, int(getattr(s, "serving_duckdb_threads", th) or th))),
        "POLARS_MAX_THREADS": str(th),
        "OMP_NUM_THREADS": str(th), "MKL_NUM_THREADS": str(th), "OPENBLAS_NUM_THREADS": str(th),
        "NUMEXPR_MAX_THREADS": str(th),
    }
    if budget_gb >= SPILL_GB or fit_gb is not None:
        spill = s.data_dir / "tmp" / "spill"
        try:
            spill.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - the job still runs, Polars uses the system temp dir
            pass
        env["BIG360_SPILL"] = "1"
        env["POLARS_TEMP_DIR"] = str(spill)
        if polars_streaming_enabled():
            env["POLARS_ENGINE_AFFINITY"] = "streaming"
        if fit_gb is not None:
            env["BIG360_JOB_FIT_GB"] = f"{base:.2f}"
    return env


_THREAD_VARS = ("POLARS_MAX_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                "NUMEXPR_MAX_THREADS")


def pool_workers(default: int, per_worker_gb: float, share: float = 0.5) -> int:
    """Worker count for a job-internal process pool: `default`, but inside a budgeted job process
    (BIG360_JOB_MEM_GB) at most share * budget / per_worker_gb, so the pool fits the job's Job Object cap
    (verification 2026-10-31: event_escalation's 10 hawkes workers committed > 4 GB and broke the pool)."""
    try:
        budget = float(os.environ.get("BIG360_JOB_MEM_GB") or 0)
    except ValueError:
        budget = 0.0
    n = max(1, int(default))
    if budget > 0 and per_worker_gb > 0:
        n = max(1, min(n, int(budget * share / per_worker_gb)))
    return n


@contextmanager
def small_worker_threads(n: int = 2):
    """Spawned pool workers started inside this block get n Polars / OpenMP / BLAS threads (they inherit the
    environment; the job's own pools are already initialised and unaffected). The environment is restored after."""
    old = {k: os.environ.get(k) for k in _THREAD_VARS}
    os.environ.update({k: str(n) for k in _THREAD_VARS})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ------------------------------------------------------------------------------------------------ reservations


def ram_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).state_dir / "ops" / "ram"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Reservation:
    path: Path | None
    job_id: str
    budget_gb: float
    kind: str = "thread"
    child_pid: int | None = None
    record: dict[str, Any] = field(default_factory=dict)
    #: coverage v3 A2: admitted below the RAM floor in spill mode -> the RAM that fitted above it (job_env sizing)
    fit_gb: float | None = None

    def set_child(self, pid: int | None) -> None:
        """The job process started: its tree's working set now counts against the reservation."""
        self.child_pid = int(pid) if pid else None
        if self.path is None:
            return
        self.record["child_pid"] = self.child_pid
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.record), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:  # pragma: no cover - bookkeeping never blocks a job
            pass

    def release(self) -> None:
        if self.path is not None:
            for i in range(25):   # Windows: a delete fails while another runner reads the file - retry briefly
                try:
                    self.path.unlink(missing_ok=True)
                    break
                except OSError:  # pragma: no cover - removed as stale later otherwise
                    time.sleep(0.01 * (i + 1) if i < 10 else 0.1)
            self.path = None


def reserve(job_id: str, budget_gb: float, kind: str = "thread", ttl_s: float = 6 * 3600.0,
            settings: Settings | None = None) -> Reservation:
    rec = {"job_id": job_id, "budget_gb": float(budget_gb), "kind": kind, "pid": os.getpid(), "child_pid": None,
           "started_at": datetime.now(UTC).isoformat(timespec="seconds"), "expires": time.time() + ttl_s}
    try:
        d = ram_dir(settings)
        p = d / f"{job_id}-{os.getpid()}-{secrets.token_hex(3)}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        os.replace(tmp, p)
    except OSError as e:  # pragma: no cover
        log.debug("ram reservation for %s not written: %s", job_id, e)
        p = None
    return Reservation(p, job_id, float(budget_gb), kind, None, rec)


def _alive(pid: int) -> bool:
    try:
        from app.ops.winsys import pid_running

        return bool(pid_running(pid))
    except Exception:  # pragma: no cover
        return True


def live(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Live reservations of every runner on this machine (stale ones are removed)."""
    out = []
    for f in ram_dir(settings).glob("*.json"):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if float(rec.get("expires", 0)) < time.time() or not _alive(int(rec.get("pid") or 0)):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        rec["_path"] = str(f)
        out.append(rec)
    return out


def usage_gb(rec: dict[str, Any]) -> float:
    """What a reservation already uses: working set of its job-process tree (0 for thread jobs: not attributable)."""
    pid = rec.get("child_pid")
    if not pid:
        return 0.0
    try:
        from app.ops.winsys import tree_memory_mb

        return tree_memory_mb(int(pid)) / 1024.0
    except Exception:  # pragma: no cover
        return 0.0


def outstanding_gb(recs: list[dict[str, Any]], usage: Any = usage_gb) -> float:
    return sum(max(0.0, float(r.get("budget_gb") or 0.0) - usage(r)) for r in recs)


def fits(budget_gb: float, free_gb: float | None, outstanding: float, headroom: float) -> bool:
    """Pure admission rule: free RAM, minus what running jobs may still allocate, minus headroom, covers the budget.
    Unknown free RAM (non-Windows / API failure) never blocks."""
    if free_gb is None:
        return True
    return free_gb - outstanding - headroom >= budget_gb


def free_gb() -> float | None:
    try:
        from app.ops.winsys import memory_status

        m = memory_status() or {}
        v = m.get("avail_gb")
        return float(v) if v is not None and (m.get("total_gb") or 0) > 0 else None
    except Exception:  # pragma: no cover
        return None


def check(budget_gb: float, settings: Settings | None = None, free: float | None = None,
          recs: list[dict[str, Any]] | None = None) -> tuple[bool, dict[str, Any]]:
    """(admit?, detail) for a job of `budget_gb` right now."""
    recs = live(settings) if recs is None else recs
    fr = free_gb() if free is None else free
    pairs = [(float(r.get("budget_gb") or 0.0), usage_gb(r)) for r in recs]   # one usage probe per reservation
    out = sum(max(0.0, b - u) for b, u in pairs)                              # = outstanding_gb(recs)
    # coverage v3 A2 review: what ending every live reservation could give back - its outstanding part plus what its
    # job processes already use (a thread job counts its whole budget): sum(max(budget, usage))
    reclaim = sum(max(b, u) for b, u in pairs)
    hr = headroom_gb()
    ok = fits(budget_gb, fr, out, hr)
    return ok, {"budget_gb": round(budget_gb, 2), "free_gb": None if fr is None else round(fr, 2),
                "outstanding_gb": round(out, 2), "headroom_gb": hr, "running": len(recs),
                "reclaim_gb": round(reclaim, 2), "holders": sorted({r.get("job_id") for r in recs})[:12]}


# ---------------------------------------------------------------------------------------- floor (coverage v3 A2)

DEFAULT_FLOOR_GB = 3.0
#: jobs below this budget (the 0.5 GB light thread jobs) keep the pre-floor rule
FLOOR_MIN_BUDGET_GB = 1.0
#: smallest DuckDB sizing base of a spill-mode start
MIN_FIT_GB = 0.5


def floor_gb() -> float:
    try:
        return max(0.0, float(os.environ.get("BIG360_RAM_FLOOR_GB", DEFAULT_FLOOR_GB)))
    except ValueError:
        return DEFAULT_FLOOR_GB


def floor_strict() -> bool:
    """BIG360_RAM_FLOOR_STRICT=1: a job that cannot meet the floor is deferred, never started in spill mode."""
    return os.environ.get("BIG360_RAM_FLOOR_STRICT", "0") == "1"


def free_after_gb(budget_gb: float, free: float | None, outstanding: float) -> float | None:
    """Machine RAM left once the job used its whole budget (None = free RAM unknown)."""
    return None if free is None else free - outstanding - budget_gb


@dataclass
class Admission:
    """Decision of admission() for one job at one moment."""

    action: str                       # start | wait | defer
    kind: str | None = None           # ram (headroom) | floor | starve: what holds the job back / what it bypassed
    reason: str | None = None         # human-readable wait / defer / start-anyway reason
    fit_gb: float | None = None       # start in spill mode: RAM that fits above the floor
    note: str | None = None           # alone | anyway | spill | spill_alone (what the caller logs)


def admission(budget_gb: float, ok: bool, info: dict[str, Any], *, light: bool, own_running: bool, waited_s: float,
              wait_max: float, starving: bool = False, defer: bool = False, floor: float | None = None,
              strict: bool | None = None) -> Admission:
    """Pure admission rule of the runner (ram_fix 2026-10-31 + coverage v3 A2 floor).

    ok / info: RB.check() for the job's budget (headroom rule); light: a light thread job (<= DEFAULT_THREAD_GB);
    own_running: other jobs of the same run are in flight; waited_s: how long this job has been held back;
    starving: another job of the run has waited past STARVE_S (it goes first); defer: the caller prefers a defer over
    a start below the floor (startup sync)."""
    floor = floor_gb() if floor is None else floor
    strict = floor_strict() if strict is None else strict
    if starving:
        return Admission("wait", "starve", "another job has waited longer for RAM (anti-starvation: it starts first)")
    fr = info.get("free_gb")
    out = float(info.get("outstanding_gb") or 0.0)
    alone = not own_running and not info.get("running")
    after = free_after_gb(budget_gb, fr, out)
    if floor > 0 and budget_gb >= FLOOR_MIN_BUDGET_GB and after is not None and after < floor:
        fit = max(MIN_FIT_GB, float(fr) - out - floor)
        why = (f"RAM floor: free {fr:.1f} GB - reserved {out:.1f} GB - budget {budget_gb:.1f} GB = {after:.1f} GB "
               f"< floor {floor:g} GB")
        # review 2026-11-07: even if every live reservation ended (reclaim_gb, default: the outstanding part), the
        # gap would stay below the floor -> waiting cannot help, same as alone (live: a 10 GB prices_finalize waited
        # 30 min for a 0.5 GB thread job of the worker, then was deferred, on every sync)
        reclaim = float(info.get("reclaim_gb", out) or 0.0)
        hopeless = not own_running and after + reclaim < floor
        if alone or hopeless:   # nothing of ours holds RAM that could close the gap: other programs hold the rest
            ctx = ("nothing else of ours runs" if alone else
                   f"ending every reservation ({reclaim:.1f} GB) would still leave {after + reclaim:.1f} GB: "
                   "waiting cannot help")
            if strict:
                return Admission("defer", "floor", why + f" ({ctx}; BIG360_RAM_FLOOR_STRICT)")
            return Admission("start", "floor", why + f"; {ctx}: spill mode ({fit:.1f} GB fits)",
                             fit_gb=fit, note="spill_alone")
        if own_running or waited_s < wait_max:   # jobs of this run will free their RAM: keep waiting for them
            holders = ", ".join(info.get("holders") or []) or "-"
            return Admission("wait", "floor", why + f" (held by {holders})")
        if defer or strict:
            return Admission("defer", "floor", why + f" after {waited_s / 60:.0f} min of waiting")
        return Admission("start", "floor", why + f" after {waited_s / 60:.0f} min: spill mode ({fit:.1f} GB fits)",
                         fit_gb=fit, note="spill")
    if ok:
        return Admission("start")
    if alone and not light:
        return Admission("start", "ram", "nothing else reserved: starting alone under its cap", note="alone")
    if light and not own_running:
        return Admission("start")
    if own_running or waited_s < wait_max:
        return Admission("wait", "ram", (f"waiting for RAM: budget {budget_gb:.1f} GB, free {fr} GB, reserved "
                                         f"{out:.1f} GB by {', '.join(info.get('holders') or []) or '-'}, headroom "
                                         f"{info.get('headroom_gb', 0.0):g} GB"))
    return Admission("start", "ram", f"RAM budget still short after {waited_s / 60:.0f} min; starting anyway "
                                     f"(memory cap {budget_gb:.1f} GB applies)", note="anyway")


# ------------------------------------------------------------------------------------------------ wait state

_WAITS: dict[str, dict[str, Any]] = {}
_WAITS_LOCK = threading.Lock()
_WAITS_WRITTEN = [0.0]
WAIT_WRITE_EVERY_S = 20.0


def admission_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).state_dir / "ops" / "admission"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_waits(settings: Settings | None, force: bool) -> None:
    now = time.time()
    if not force and now - _WAITS_WRITTEN[0] < WAIT_WRITE_EVERY_S:
        return
    _WAITS_WRITTEN[0] = now
    try:
        p = admission_dir(settings) / f"{os.getpid()}.json"
        with _WAITS_LOCK:
            doc = {"pid": os.getpid(), "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                   "waits": {k: dict(v) for k, v in _WAITS.items()}}
        if not doc["waits"]:
            p.unlink(missing_ok=True)
            return
        tmp = p.with_suffix(f".{secrets.token_hex(2)}.tmp")
        tmp.write_text(json.dumps(doc, default=str), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:  # pragma: no cover - bookkeeping never blocks a job
        pass


def note_wait(job_id: str, kind: str | None, reason: str | None, settings: Settings | None = None,
              detail: dict[str, Any] | None = None) -> None:
    """A job of this process is held back (kind ram | floor | starve | slot). Logged by the caller; written to the
    process's admission file (immediately on a new / changed reason, else every WAIT_WRITE_EVERY_S)."""
    with _WAITS_LOCK:
        prev = _WAITS.get(job_id)
        changed = prev is None or prev.get("kind") != kind   # numbers in the reason change every poll: throttled
        rec = {"job_id": job_id, "kind": kind, "reason": (reason or "")[:400] or None,
               "since": (prev or {}).get("since") or datetime.now(UTC).isoformat(timespec="seconds"),
               "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
        if detail:
            rec["detail"] = {k: v for k, v in detail.items() if k in ("budget_gb", "free_gb", "outstanding_gb",
                                                                      "headroom_gb", "holders", "running")}
        _WAITS[job_id] = rec
    _write_waits(settings, force=changed)


def clear_wait(job_id: str, settings: Settings | None = None) -> None:
    with _WAITS_LOCK:
        had = _WAITS.pop(job_id, None) is not None
    if had:
        _write_waits(settings, force=True)


def current_waits() -> dict[str, dict[str, Any]]:
    """Waits of THIS process (the startup sync shows them per job)."""
    with _WAITS_LOCK:
        return {k: dict(v) for k, v in _WAITS.items()}


def waits(settings: Settings | None = None) -> list[dict[str, Any]]:
    """Every live runner's waiting jobs (dead pids' files are removed)."""
    out: list[dict[str, Any]] = []
    try:
        files = list(admission_dir(settings).glob("*.json"))
    except OSError:  # pragma: no cover
        return out
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(doc.get("pid") or 0)
        if pid != os.getpid() and not _alive(pid):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        for rec in (doc.get("waits") or {}).values():
            out.append({**rec, "pid": pid})
    return sorted(out, key=lambda r: str(r.get("since") or ""))


__all__ = ["DEFAULT_FLOOR_GB", "DEFAULT_PROCESS_GB", "DEFAULT_THREAD_GB", "FLOOR_MIN_BUDGET_GB", "SPILL_GB",
           "Admission", "Reservation", "admission", "budget_of", "check", "clear_wait", "current_waits", "enabled",
           "fits", "floor_gb", "floor_strict", "free_after_gb", "free_gb", "headroom_gb", "job_env", "live",
           "note_wait", "outstanding_gb", "parse_size_gb", "polars_streaming_enabled", "pool_workers", "reserve",
           "small_worker_threads", "threads_for", "usage_gb", "wait_max_s", "waits"]
