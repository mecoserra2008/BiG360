"""Gap-filler tasks (O1.3/O1.4): what to run next and how to run it as a low-priority child process.

TaskSpec       one unit of filler work: a gap-fill (rows of data_gaps grouped per dataset / entity / action) or a
               campaign (long resumable backfill: GDELT E6, 13F history, CFTC history, earnings chain, Yahoo history)
TaskProc       the running child: BelowNormal priority, own process group, own Windows Job Object (incident
               2026-10-24: descendants that outlive the task root - e.g. a job process whose taskrun parent crashed
               on CTRL+BREAK - are killed when the root exits), stdout -> data/logs/ops/tasks/<id>.log, graceful
               stop (campaign STOP file, else CTRL+BREAK) then job / process-tree kill after a grace period
gap_tasks()    data_gaps rows -> TaskSpecs (due by next_attempt_at, executable fill kinds only)
campaign_tasks() coverage.yaml campaigns -> TaskSpecs when due (progress probes decide)
probe()        progress / ETA of a campaign from its own state files (resumable state is never touched)
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops import winsys
from app.ops.paths import read_json

log = get_logger("app.ops.tasks")

_UNSAFE_FN = str.maketrans({c: "_" for c in '<>:"/\\|?*'})


def safe_name(task_id: str) -> str:
    """Task id -> Windows-safe file stem (ids like 'gap_prices.CN_*' carry wildcards / separators)."""
    return task_id.translate(_UNSAFE_FN).strip(" .") or "task"

EXEC_FILL_KINDS = ("polygon_dates", "yf_refetch", "job", "gdelt_redo")
GRACE_S = {"module": 900.0, "default": 120.0}   # graceful stop window before the tree is killed


@dataclass
class TaskSpec:
    task_id: str
    label: str
    kind: str                                   # gap | campaign | detect
    priority: int
    command: list[str]
    source: str | None = None
    cost_units: float | None = None
    est_ram_gb: float = 1.0
    preemptible: bool = True
    yield_to_dags: bool = True
    stop_file: Path | None = None
    lock: str | None = None                     # data/.locks/<lock>.lock (another holder = blocked / adopted)
    gap_ids: list[str] = field(default_factory=list)
    dataset: str | None = None
    campaign: str | None = None
    spec_path: Path | None = None
    result_path: Path | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    heavy: bool = False                         # runs alone (app.ops.governor.admit): E6, CC-NEWS, GDELT redo


def python_exe() -> str:
    return sys.executable


def backend_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).project_root / "backend"


def task_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).state_dir / "ops" / "tasks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def task_log_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).logs_dir / "ops" / "tasks"
    p.mkdir(parents=True, exist_ok=True)
    return p


def lock_held(name: str, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    lk = FileLock(str(s.locks_dir / f"{name}.lock"), timeout=0)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True
    lk.release()
    return False


# ------------------------------------------------------------------------------------------------ process


class TaskProc:
    def __init__(self, spec: TaskSpec, settings: Settings | None = None, priority: str = "below_normal"):
        self.spec = spec
        self.settings = settings or get_settings()
        self.priority = priority
        self.popen: subprocess.Popen | None = None
        self.pid: int | None = None
        self.started_at: datetime | None = None
        self.stop_requested_at: float | None = None
        self.created_stop_file = False
        self.log_path = task_log_dir(self.settings) / f"{safe_name(spec.task_id)}.log"
        self.adopted = False
        self.job: Any = None                    # winsys.JobObject (None when adopted / unsupported)
        self.stop_grace_s: float | None = None  # override of GRACE_S for this stop request (pause / RAM)
        self.leftovers_killed = 0

    def start(self) -> None:
        env = dict(os.environ)
        env.setdefault("BIG360_DATA_DIR", str(self.settings.data_dir))
        env["PYTHONUNBUFFERED"] = "1"
        if self.spec.stop_file and self.spec.stop_file.exists():
            try:
                self.spec.stop_file.unlink()
            except OSError:
                pass
        fh = open(self.log_path, "ab")  # noqa: SIM115 - the child owns the handle for its lifetime
        fh.write(f"\n==== {datetime.now(UTC).isoformat(timespec='seconds')} start {self.spec.command}\n".encode())
        fh.flush()
        job = winsys.JobObject(name=self.spec.task_id)
        try:
            self.popen = winsys.start_in_job(self.spec.command, job if job.ok else None,
                                             cwd=str(backend_dir(self.settings)), env=env, stdout=fh,
                                             stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                             creationflags=winsys.priority_creationflags(self.priority))
        except BaseException:
            job.close()
            raise
        finally:
            fh.close()
        self.pid = self.popen.pid
        self.started_at = datetime.now(UTC)
        if job.ok and (self.pid in job.assigned or job.contains(self.pid)):
            self.job = job
        else:
            job.close()
        if not winsys.IS_WINDOWS:
            winsys.set_priority(self.pid, self.priority)
        log.info("task %s started pid=%s: %s", self.spec.task_id, self.pid, " ".join(self.spec.command[:8]))

    @classmethod
    def adopt(cls, spec: TaskSpec, pid: int, settings: Settings | None = None) -> TaskProc:
        """Monitor an instance started outside the filler (e.g. the hand-launched GDELT backfill)."""
        t = cls(spec, settings)
        t.pid, t.adopted, t.started_at = pid, True, datetime.now(UTC)
        return t

    def poll(self) -> int | None:
        """Exit code, or None while running."""
        if self.popen is not None:
            return self.popen.poll()
        return None if winsys.pid_alive(self.pid) else 0

    def rss_gb(self, procs: list[tuple[int, int, str]] | None = None) -> float:
        """RAM of the task: every process of its job (orphaned descendants included), else the pid tree."""
        if self.job is not None:
            return self.job.memory_mb() / 1024
        if not self.pid:
            return 0.0
        return winsys.tree_memory_mb(self.pid, procs) / 1024

    def kill(self) -> list[int]:
        killed = []
        if self.job is not None:
            n = self.job.terminate()
            killed = [-1] * n
        if self.pid:
            killed = winsys.kill_tree(self.pid) or killed
        return killed

    def reap_leftovers(self) -> int:
        """After the root exited: kill whatever is still in the task's job (leak prevention)."""
        if self.job is None:
            return 0
        n = self.job.terminate()
        if n:
            log.warning("task %s exited but left %d process(es) running: killed (leak prevented)", self.spec.task_id, n)
            self.job.wait_empty(10)
            self.leftovers_killed += n
        self.job.close()
        self.job = None
        return n

    def request_stop(self, grace_s: float | None = None) -> None:
        if grace_s is not None:   # a later, more urgent request (pause) may shorten the window
            self.stop_grace_s = grace_s if self.stop_grace_s is None else min(self.stop_grace_s, grace_s)
        if self.stop_requested_at is not None:
            return
        self.stop_requested_at = time.monotonic()
        if self.spec.stop_file is not None:
            self.spec.stop_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.spec.stop_file.exists():
                self.spec.stop_file.write_text(f"filler {datetime.now(UTC).isoformat()}", encoding="utf-8")
                self.created_stop_file = True
            log.info("task %s: STOP file written (graceful)", self.spec.task_id)
            return
        if self.popen is not None and winsys.IS_WINDOWS:
            try:
                os.kill(self.popen.pid, signal.CTRL_BREAK_EVENT)  # new process group -> only this tree
            except OSError:
                pass
        elif self.popen is not None:
            self.popen.terminate()
        log.info("task %s: stop requested", self.spec.task_id)

    def enforce_stop(self) -> bool:
        """Kill the tree once the graceful window passed. True when the process is gone."""
        if self.poll() is not None:
            self.cleanup()
            return True
        if self.stop_requested_at is None:
            return False
        grace = GRACE_S["module"] if self.spec.stop_file else GRACE_S["default"]
        if self.stop_grace_s is not None:
            grace = min(grace, self.stop_grace_s)
        if time.monotonic() - self.stop_requested_at > grace and self.pid:
            killed = self.kill()
            log.warning("task %s: killed after %.0fs grace (%s)", self.spec.task_id, grace, killed)
            if self.popen is not None:
                try:
                    self.popen.wait(10)
                except subprocess.TimeoutExpired:
                    pass
            self.cleanup()
            return True
        return False

    def cleanup(self) -> None:
        if self.popen is not None and self.popen.poll() is not None:
            self.reap_leftovers()
        if self.created_stop_file and self.spec.stop_file is not None and self.poll() is not None:
            try:
                self.spec.stop_file.unlink()
            except OSError:
                pass
            self.created_stop_file = False

    def tail(self, n: int = 2000) -> str:
        try:
            with open(self.log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - n))
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def result(self) -> dict[str, Any] | None:
        if self.spec.result_path and self.spec.result_path.exists():
            try:
                return json.loads(self.spec.result_path.read_text(encoding="utf-8"))
            except ValueError:
                return None
        return None


# ------------------------------------------------------------------------------------------------ gap tasks


def _taskrun_cmd(spec_path: Path) -> list[str]:
    return [python_exe(), "-m", "app.ops.taskrun", str(spec_path)]


def _write_spec(task_id: str, body: dict[str, Any], settings: Settings) -> tuple[Path, Path]:
    safe = safe_name(task_id)
    sp = task_dir(settings) / f"{safe}.json"
    rp = task_dir(settings) / f"{safe}.result.json"
    if rp.exists():
        rp.unlink()
    sp.write_text(json.dumps({**body, "task_id": task_id, "result_path": str(rp)}, default=str, indent=1),
                  encoding="utf-8")
    return sp, rp


def _aware(t: Any) -> datetime | None:
    if t is None:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def due_gaps(gaps: pl.DataFrame, now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    if gaps.is_empty():
        return gaps
    df = gaps.filter(pl.col("status").is_in(["open", "failed", "queued"]) & pl.col("fill_kind").is_in(list(EXEC_FILL_KINDS)))
    if df.is_empty():
        return df
    return df.filter(pl.col("next_attempt_at").is_null() | (pl.col("next_attempt_at") <= now)).sort(["priority", "first_seen"])


def gap_tasks(gaps: pl.DataFrame, reg: dict[str, Any], settings: Settings | None = None,
              now: datetime | None = None, limit: int = 20, skip_ids: set[str] | None = None) -> list[TaskSpec]:
    """Group due gaps into tasks: one per (dataset, entity) for price re-fetches (all dates at once), one per GDELT
    day, one per dataset for job fills."""
    s = settings or get_settings()
    df = due_gaps(gaps, now)
    out: list[TaskSpec] = []
    if df.is_empty():
        return out
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in df.iter_rows(named=True):
        fk = r["fill_kind"]
        ent = r["entity"] if fk in ("yf_refetch", "gdelt_redo", "polygon_dates") else "*"
        groups.setdefault((r["dataset"], ent, fk), []).append(r)
    for (ds, ent, fk), rows in sorted(groups.items(), key=lambda kv: min(x["priority"] for x in kv[1])):
        spec = reg["datasets"].get(ds, {})
        prio = int(min(x["priority"] for x in rows))
        ids = [x["gap_id"] for x in rows]
        dates = sorted({d.isoformat() for x in rows for d in _dates(x)})
        src = rows[0]["source"]
        units = sum(float(x["cost_units"] or 0) for x in rows) or None
        tid = f"gap:{ds}:{ent}"
        if skip_ids and tid in skip_ids:
            continue
        if fk == "polygon_dates":
            if not dates:
                continue
            body = {"kind": "polygon_dates", "dates": dates, "detect_after": [ds]}
            units = float(len(dates))
            ram = 1.5
        elif fk == "yf_refetch":
            if not dates:
                continue
            region = spec.get("region") or ent
            body = {"kind": "yf_refetch", "region": region, "dates": dates, "detect_after": [ds]}
            st = read_json(s.state_dir / "ops" / "coverage_status.json", {}) or {}
            typ = (((st.get("datasets") or {}).get(ds) or {}).get("entities") or {}).get(region, {}).get("typical")
            units = float(typ or 500)
            ram = 2.0
        elif fk == "gdelt_redo":
            day = (json.loads(rows[0]["fill_params"] or "{}") or {}).get("day") or (dates[0] if dates else None)
            if not day:
                continue
            cmd = [python_exe(), "-m", "app.events_v2.backfill", "--label", "redo", "--start", day, "--end", day,
                   "--redo", "--batch-days", "1", "--workers", "2", "--dl-threads", "4", "--retention"]
            out.append(TaskSpec(tid, f"GDELT re-run {day}", "gap", prio, cmd, source=src or "gdelt_files",
                                cost_units=3.0, est_ram_gb=3.0, stop_file=s.state_dir / "events_v2" / "STOP",
                                lock="events_v2_backfill", gap_ids=ids, dataset=ds, meta={"day": day}, heavy=True))
            continue
        else:  # job
            fp = json.loads(rows[0]["fill_params"] or "{}") or {}
            jobs = fp.get("jobs") or ((spec.get("fill") or {}).get("jobs")) or []
            if not jobs:
                continue
            body = {"kind": "jobs", "jobs": jobs, "detect_after": [ds]}
            ram = 2.0
        sp, rp = _write_spec(tid, body, s)
        out.append(TaskSpec(tid, f"Fill {spec.get('label', ds)}" + (f" {ent}" if ent != "*" else "")
                            + (f" ({', '.join(dates[:3])}{'...' if len(dates) > 3 else ''})" if dates else ""),
                            "gap", prio, _taskrun_cmd(sp), source=src, cost_units=units, est_ram_gb=ram,
                            preemptible=True, yield_to_dags=True, gap_ids=ids, dataset=ds, spec_path=sp,
                            result_path=rp, meta={"dates": dates, "fill_kind": fk}))
        if len(out) >= limit:
            break
    return out


def _dates(r: dict[str, Any]) -> list[date]:
    a, b = r.get("period_start"), r.get("period_end")
    if a is None and b is None:
        return []
    a = a or b
    b = b or a
    if (b - a).days > 400:
        return [a, b]
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


# ------------------------------------------------------------------------------------------------ campaigns


def probe(campaign: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """{"done": bool, "pct": float|None, "done_units", "total_units", "unit", "rate_per_h", "eta_h", "detail"}."""
    s = settings or get_settings()
    pr = campaign.get("progress") or {}
    kind = pr.get("probe")
    out: dict[str, Any] = {"done": False, "pct": None, "done_units": None, "total_units": None, "unit": None,
                           "rate_per_h": None, "eta_h": None, "detail": None}
    try:
        if kind == "gdelt_range":
            st = read_json(s.state_dir / "events_v2.json", {}) or {}
            a, b = date.fromisoformat(pr["start"]), date.fromisoformat(pr["end"])
            days = st.get("days") or {}
            total = (b - a).days + 1
            done = sum(1 for k in days if a.isoformat() <= k <= b.isoformat())
            run = (st.get("backfill_runs") or {}).get(pr.get("label", "e6")) or {}
            batches = [x for x in run.get("batches") or [] if x.get("days_per_hour")][-8:]
            rate = (sum(x["days_per_hour"] for x in batches) / len(batches)) if batches else None
            rem = total - done
            out.update(done=rem <= 0 or run.get("status") == "done", pct=round(100 * done / total, 1), done_units=done,
                       total_units=total, unit="days", rate_per_h=round(rate, 2) if rate else None,
                       eta_h=round(rem / rate, 1) if rate and rem > 0 else (0 if rem <= 0 else None),
                       detail=f"run {run.get('status', '-')} pid {run.get('pid', '-')}; {rem} days left (newest first)")
        elif kind == "sec_13f_backfill":
            st = read_json(s.state_dir / "sec_13f_backfill.json", {}) or {}
            merged, staged = list(st.get("merged") or []), dict(st.get("staged") or {})
            tot = len(merged) + len(staged)
            out.update(done=not staged and bool(merged), pct=round(100 * len(merged) / tot, 1) if tot else None,
                       done_units=len(merged), total_units=tot, unit="windows",
                       detail=f"merged {len(merged)} windows, staged {len(staged)}; updated {st.get('updated_at', '-')}")
            out["last_run_at"] = st.get("updated_at")
        elif kind == "cftc_history":
            st = read_json(s.state_dir / "cftc.json", {}) or {}
            ing = st.get("ingest") or {}
            done = st.get("status") == "done" and bool(ing.get("full_backfill"))
            out.update(done=done, pct=100.0 if done else None, unit="reports",
                       detail=f"full backfill {ing.get('full_backfill', '-')}; max report {ing.get('max_report_date', '-')}")
            out["last_run_at"] = ing.get("full_backfill", "")[:10] or None
        elif kind == "earnings_chain":
            st = read_json(s.state_dir / "earnings.json", {}) or {}
            ch = st.get("backfill_chain") or {}
            yf = ((st.get("earnings_dates_fetch") or {}).get("yahoo")) or {}
            done = ch.get("last_step") == "earnings_signals"
            tgt = yf.get("targets")
            ok = (yf.get("ok") or 0) + (yf.get("empty") or 0)
            out.update(done=done, pct=round(100 * ok / tgt, 1) if tgt else (100.0 if done else None), done_units=ok,
                       total_units=tgt, unit="securities", detail=f"last step {ch.get('last_step', '-')} at {ch.get('at', '-')}")
            out["last_run_at"] = ch.get("at")
        elif kind == "earnings_dates":  # W1 2026-10-31: job campaign earnings_dates_fetch over universe full
            st = read_json(s.state_dir / "earnings.json", {}) or {}
            run = st.get("earnings_dates_fetch") or {}
            yf = run.get("yahoo") or {}
            tgt = yf.get("targets")
            seen = (yf.get("ok") or 0) + (yf.get("empty") or 0) + (yf.get("failed") or 0)
            # done when the last run got through every security that was due (nothing left for the next run)
            done = tgt is not None and seen >= tgt
            out.update(done=done, pct=round(100 * min(seen, tgt) / tgt, 1) if tgt else (100.0 if done else None),
                       done_units=seen, total_units=tgt, unit="securities due",
                       detail=f"last run {run.get('at', '-')}: {yf.get('ok', 0)} with dates, {yf.get('empty', 0)} empty, "
                              f"{yf.get('failed', 0)} failed of {tgt} due")
            out["last_run_at"] = run.get("at")
        elif kind == "register_backfill":  # coverage v2 W2: register statements campaign (job register_backfill)
            st = read_json(s.state_dir / "register_backfill.json", {}) or {}
            runs = st.get("runs") or {}
            steps = [x for x in (pr.get("steps") or []) if x != "normalize"] or list(runs)
            last = [runs.get(x) or {} for x in steps]
            ok = [x for x, r in zip(steps, last, strict=True) if r.get("status") == "done"]
            out.update(done=bool(steps) and len(ok) == len(steps), pct=round(100 * len(ok) / len(steps), 1) if steps else None,
                       done_units=len(ok), total_units=len(steps), unit="steps",
                       detail="; ".join(f"{x}: {r.get('status', 'not run')}" for x, r in zip(steps, last, strict=True))[:300])
            ats = [r.get("at") for r in last if r.get("at")]
            # a step stopped on its time budget (partial) leaves last_run_at empty -> the campaign is due again
            out["last_run_at"] = max(ats) if ats and out["done"] else None
        elif kind == "price_history":
            st = read_json(s.state_dir / "price_history.json", {}) or {}
            tasks = st.get("tasks") or {}
            n_done = sum(1 for v in tasks.values() if isinstance(v, dict) and v.get("status") == "done")
            cov = read_json(s.state_dir / "ops" / "coverage_status.json", {}) or {}
            ph = ((cov.get("datasets") or {}).get("prices.history") or {})
            unpriced = ph.get("lag")
            out.update(done=bool(tasks) and n_done == len([v for v in tasks.values() if isinstance(v, dict)]) and not unpriced,
                       pct=round(100 * n_done / len(tasks), 1) if tasks else None, done_units=n_done,
                       total_units=len(tasks), unit="steps",
                       detail=f"workstream steps {n_done}/{len(tasks)}; unpriced securities {unpriced if unpriced is not None else '-'}")
        elif kind == "ccnews_months":  # N1 CC-NEWS backfill (app.events_v2.ccnews)
            from app.events_v2.ccnews import probe_progress

            out.update(probe_progress(pr, s))
        elif kind == "rates_backfill":  # W3 official curves full history (app.rates.curves_job, per source)
            from app.rates.curves_job import probe_progress as rb_probe

            out.update(rb_probe(s))
        elif kind == "openfigi_bonds":  # W3 US bond reference from OpenFIGI (app.rates.bonds)
            from app.rates.bonds import probe_progress as of_probe

            out.update(of_probe(s))
        elif kind == "news_filings":  # N1 SEC 8-K / exchange announcements -> story layer (app.events_v2.filing_news)
            from app.events_v2.filing_news import probe_progress as fn_probe

            out.update(fn_probe(pr, s))
    except Exception as e:  # probes never break the filler
        out["detail"] = f"probe error: {type(e).__name__}: {e}"[:300]
    return out


def find_lock_holder_pid(campaign: dict[str, Any], settings: Settings | None = None) -> int | None:
    """pid of an externally started instance of a module campaign (state file pid + command-line match)."""
    s = settings or get_settings()
    if campaign.get("module") != "app.events_v2.backfill":
        return None
    st = read_json(s.state_dir / "events_v2.json", {}) or {}
    label = (campaign.get("progress") or {}).get("label", "e6")
    pid = ((st.get("backfill_runs") or {}).get(label) or {}).get("pid")
    if isinstance(pid, int) and winsys.pid_alive(pid):
        return pid
    return None


def campaign_tasks(reg: dict[str, Any], state: dict[str, Any], gaps: pl.DataFrame, settings: Settings | None = None,
                   now: datetime | None = None, skip_ids: set[str] | None = None) -> list[TaskSpec]:
    """Due campaigns. module: until the probe says done. job / script / prices_history: every `every_days` (since
    the last filler run or the probe's last_run_at) or when an open gap points at the campaign."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    out: list[TaskSpec] = []
    wanted = set()
    if not gaps.is_empty() and "fill_params" in gaps.columns:
        for r in gaps.filter(pl.col("status").is_in(["open", "failed"]) & (pl.col("fill_kind") == "campaign")).iter_rows(named=True):
            try:
                c = (json.loads(r["fill_params"] or "{}") or {}).get("campaign")
            except ValueError:
                c = None
            if c:
                wanted.add(c)
    for cid, c in reg.get("campaigns", {}).items():
        kind = c.get("kind")
        if kind == "gap_queue" or (skip_ids and f"campaign:{cid}" in skip_ids):
            continue
        cst = (state.get("campaigns") or {}).get(cid) or {}
        nxt = cst.get("next_attempt_at")
        if nxt and datetime.fromisoformat(nxt) > now:
            continue
        pr = probe(c, s)
        if kind == "module":
            if pr.get("done"):
                continue
        else:
            every = float(c.get("every_days", 0) or 0)
            last = cst.get("last_success_at") or pr.get("last_run_at")
            last_dt = None
            if last:
                try:
                    last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                    last_dt = last_dt if last_dt.tzinfo else last_dt.replace(tzinfo=UTC)
                except ValueError:
                    last_dt = None
            stale = every > 0 and (last_dt is None or now - last_dt >= timedelta(days=every))
            if not (stale or cid in wanted):
                continue
        cmd: list[str]
        sp = rp = None
        stop = None
        if kind == "module":
            cmd = [python_exe(), "-m", c["module"], *[str(x) for x in c.get("args") or []]]
            stop = s.data_dir / c["stop_file"] if c.get("stop_file") else None
        elif kind == "script":
            cmd = [python_exe(), str(s.project_root / c["script"]), *[str(x) for x in c.get("args") or []]]
        elif kind == "job":
            sp, rp = _write_spec(f"campaign:{cid}", {"kind": "jobs", "jobs": [{"job": c["job"], "params": c.get("params") or {}}]}, s)
            cmd = _taskrun_cmd(sp)
        elif kind == "prices_history":
            sp, rp = _write_spec(f"campaign:{cid}", {"kind": "prices_history", **(c.get("params") or {}),
                                                    "detect_after": ["prices.history"]}, s)
            cmd = _taskrun_cmd(sp)
        else:
            continue
        out.append(TaskSpec(f"campaign:{cid}", c.get("label", cid), "campaign", int(c.get("priority", 70)), cmd,
                            source=c.get("source"), cost_units=None, est_ram_gb=float(c.get("est_ram_gb", 2.0)),
                            preemptible=bool(c.get("preemptible", True)), yield_to_dags=True, stop_file=stop,
                            lock=c.get("lock"), campaign=cid, spec_path=sp, result_path=rp, meta={"progress": pr},
                            heavy=bool(c.get("heavy", kind == "module"))))
    return out
