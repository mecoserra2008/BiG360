"""Startup sync executor (docs/STARTUP_SYNC_PLAN.md A2): minimal plan -> parallel fetch -> derive -> publish.

    python -m app.ops.sync run [--trigger startup] [--dataset prices.EU ...] [--no-publish] [--passes 2]
    SyncRun(settings, trigger="startup").run() -> summary dict

One sync =
  0. lock data/.locks/dag_sync.lock (no overlap with itself; a refresh DAG running -> skipped; the refresh DAGs wait
     for the lock, and their waiting writes sync.YIELD so a long sync stops early);
  1. freshness scan (app.ops.sync.scan) -> datasets with reason codes + the fetch plan;
  2. pass 1 ("core"): the planned fetch jobs of the coverage datasets plus the core derive jobs downstream of them;
     pass 2 ("extended"): daily-cadence source jobs and the slow model analytics.
     Each pass is one dependency graph: a node starts when every upstream node of the pass has finished (any
     outcome: dependencies are soft except policy.HARD_DEPS). Network fetchers run in parallel lanes (per source at
     most lane_max jobs, NETWORK_MAX in total); heavy jobs (process executor / HEAVY_JOBS) one at a time, only when
     the machine guard reports no memory pressure; light derive jobs share LIGHT_LANE_MAX slots. A derive job runs
     only if its input fingerprint changed (graph.inputs_changed) and its min interval elapsed. Every job goes
     through the JobRunner (job_runs rows, job locks, process jobs in Job Objects, timeouts, the heavy slot);
  3. after each pass: verify (re-scan), ledger update (still missing after a successful fetch -> not_yet_published
     with back-off; failures -> failed / source_unavailable, parked after PARK_AFTER_FAILURES), then a
     section-incremental publish (app.ops.sync.publish; the full build when most sections are dirty), then
     alerts_eval / thesis_eval on the new snapshot;
  4. gap detection refresh (Data Health); after a publish dq_checks (at most every 20 h, dq_step) and the
     regression guard (qa_step) - either one also without a publish when a previous sync deferred it (carried);
     final scan, status + history.
Nothing aborts the run: a failed job marks its datasets (and, through serving_sections, its serving section) only.

Coverage v3 A3 (interval syncs ran 33-39 h on 11-01..11-04; app.ops.sync.caps): a wall-clock / job-count budget per
sync (pending work past it is skipped as "deferred: ..." and carried to the next sync, with priority), per-job
timeouts from run history (p95 x 1.5, clamped), jobs that cannot start in the budget (RAM floor, heavy slot) are
deferred by the runner, a lock-owner record for stale-lock recovery (app.ops.sync.trigger), and the summary / status
record what was deferred and why. BIG360_SYNC_CAPS=0 turns the caps and history timeouts off.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from filelock import Timeout

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.sync import caps as C
from app.ops.sync import graph as G
from app.ops.sync import policy as P
from app.ops.sync import state as S
from app.ops.sync.scan import PlanItem, ScanResult, scan

log = get_logger("app.ops.sync.run")

REFRESH_DAGS = ("nightly", "weekly", "monthly")


@dataclass
class Node:
    job: str
    pass_no: int
    lane: str
    tier: str
    params: dict[str, Any] = field(default_factory=dict)
    datasets: list[str] = field(default_factory=list)
    why: list[str] = field(default_factory=list)
    trigger: str = "derive"            # dataset | cadence | upstream | fingerprint | derive | post
    periods: dict[str, list[str]] = field(default_factory=dict)
    status: str = "pending"            # pending | running | success | partial | failed | skipped
    reason: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    rows: int | None = None
    message: str | None = None
    error_type: str | None = None
    run_ids: list[str] = field(default_factory=list)
    race_retry: bool = False           # re-queued once after a concurrent-write file race
    carried: int = 0                   # coverage v3 A3: times a previous sync deferred it (goes first)

    @property
    def done(self) -> bool:
        return self.status not in ("pending", "running")

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("job", "pass_no", "lane", "tier", "trigger", "status", "reason",
                                               "started_at", "finished_at", "duration_s", "rows", "message",
                                               "error_type", "datasets")} | {
            "params": {k: (v if not isinstance(v, list) or len(v) <= 12 else f"{len(v)} items") for k, v in self.params.items()},
            "why": self.why[:4], "carried": self.carried or None}


def ready_order(n: Node, prio: dict[str, float]) -> tuple[int, int, float, str]:
    """Start order among ready nodes: network first, then work carried from a previous sync (coverage v3 A3), then
    the longest remaining path (critical path), then the name."""
    return (0 if n.lane == "network" else 1, 0 if n.carried else 1, -prio.get(n.job, 0.0), n.job)


def _is_deferred(n: Node) -> bool:
    return n.status == "skipped" and (n.reason or "").startswith("deferred")


class RamMonitor:
    """Peak memory of the sync process tree (sync process + job processes) and the machine's lowest free RAM."""

    def __init__(self) -> None:
        self.peak_mb = 0.0                # private commit (includes paged-out memory)
        self.peak_at: str | None = None
        self.peak_ws_mb = 0.0             # working set (physical RAM actually held)
        self.min_free_gb: float | None = None
        self.samples = 0

    def sample(self) -> dict[str, Any]:
        try:
            from app.ops import winsys

            procs = winsys.process_list()
            pids = winsys.process_tree(os.getpid(), procs) or [os.getpid()]
            tot = ws = 0.0
            for p in pids:
                d = winsys.process_memory_detail(p)
                if d:
                    tot += float(d.get("private_mb") or d.get("rss_mb") or 0.0)
                    ws += float(d.get("rss_mb") or 0.0)
            mem = winsys.memory_status() or {}
            free = mem.get("avail_gb") if (mem.get("total_gb") or 0) > 0 and (mem.get("avail_gb") or 0) > 0.01 else None
        except Exception:  # noqa: BLE001
            return {}
        self.samples += 1
        if tot > self.peak_mb:
            self.peak_mb, self.peak_at = tot, S.now_iso()
        self.peak_ws_mb = max(self.peak_ws_mb, ws)
        if free is not None and (self.min_free_gb is None or free < self.min_free_gb):
            self.min_free_gb = float(free)
        return {"tree_mb": round(tot), "free_gb": round(free, 2) if free is not None else None}

    def to_dict(self) -> dict[str, Any]:
        return {"peak_tree_mb": round(self.peak_mb), "peak_at": self.peak_at, "peak_ws_mb": round(self.peak_ws_mb),
                "min_machine_free_gb": round(self.min_free_gb, 2) if self.min_free_gb is not None else None,
                "samples": self.samples}


class SyncRun:
    def __init__(self, settings: Settings | None = None, trigger: str = "manual", datasets: list[str] | None = None,
                 passes: int = 2, publish: bool = True, dry_run: bool = False, now: datetime | None = None,
                 registry: Any = None, runner: Any = None, scan_fn: Any = None, publish_fn: Any = None,
                 pressure_fn: Any = None, sleep_s: float = 1.0, retries: int | None = 1):
        from app.scheduler.runner import JobRegistry, JobRunner

        self.s = settings or get_settings()
        self.trigger = trigger
        self.only = list(datasets or []) or None
        self.passes = max(1, int(passes))
        self.do_publish = publish
        self.dry_run = dry_run
        self._now = now
        self.reg = registry or JobRegistry.load(settings=self.s)
        self.stop_event = threading.Event()      # hard stop (STOP file): running jobs are cancelled
        self.yield_event = threading.Event()     # soft stop (refresh waiting): running jobs finish
        self.runner = runner or JobRunner(self.reg, self.s, stop_event=self.stop_event)
        self.scan_fn = scan_fn or scan
        self.publish_fn = publish_fn or self._publish
        self.pressure_fn = pressure_fn
        self.sleep_s = sleep_s
        self.retries = retries
        self.ledger = S.Ledger(self.s)
        self.clock = G.TableClock(self.s)
        self.ram = RamMonitor()
        self.run_id = "S" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + os.urandom(2).hex().upper()
        self.dag_run_id = f"D{self.run_id[1:]}-sync"
        self.nodes: dict[tuple[int, str], Node] = {}
        self.publishes: list[dict[str, Any]] = []
        self.status: dict[str, Any] = {}
        self.first_scan: ScanResult | None = None
        self.last_scan: ScanResult | None = None
        self.as_of: date | None = None
        self.last_ok: dict[str, datetime | None] = {}
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self.phase = "starting"
        self.notes: list[str] = []
        # coverage v3 A3: budget, history timeouts, deferred / carried work
        self.caps = C.load_caps(_sync_cfg(self.s), trigger)
        self.budget = C.Budget(self.caps)
        self.deferred: list[dict[str, Any]] = []
        self.carry_in: dict[str, dict[str, Any]] = {}
        self._timeouts: dict[str, dict[str, Any]] | None = None
        self.timeouts_applied: dict[str, dict[str, Any]] = {}
        self.suspended_s = 0.0
        self._carry_done: set[str] = set()       # carried entries settled without a run (e.g. dq_checks no longer due)

    # ---------------------------------------------------------------- time / status
    def now(self) -> datetime:
        return self._now or datetime.now(UTC)

    def _write_status(self, state: str = "running", final: dict[str, Any] | None = None) -> None:
        nodes = list(self.nodes.values())
        ds = (self.last_scan or self.first_scan)
        dsets = [d.to_dict() for d in ds.datasets] if ds else []
        counts: dict[str, int] = {}
        for d in dsets:
            counts[d["reason"]] = counts.get(d["reason"], 0) + 1
        total = len(nodes) + (len(self.publishes_planned()) if state == "running" else 0)
        done = sum(1 for n in nodes if n.done) + sum(1 for p in self.publishes if p.get("status") != "running")
        fetched = sorted({d for n in nodes if n.lane == "network" and n.status in ("success", "partial")
                          for d in n.datasets})
        doc = {
            "run_id": self.run_id, "state": state, "phase": self.phase, "trigger": self.trigger, "pid": os.getpid(),
            "started_at": self.status.get("started_at") or S.now_iso(), "heartbeat_at": S.now_iso(),
            "finished_at": final.get("finished_at") if final else None,
            "as_of": self.as_of.isoformat() if self.as_of else (ds.as_of if ds else None),
            "progress": {"datasets_checked": len(dsets), "datasets_total": len(dsets),
                         "due": len([d for d in (self.first_scan.datasets if self.first_scan else []) if d.reason == "due"]),
                         "fetched": len(fetched),
                         "skipped": sum(counts.get(r, 0) for r in ("not_yet_published", "source_unavailable",
                                                                    "needs_key", "failed")),
                         "failed": counts.get("failed", 0),
                         "jobs_done": done, "jobs_total": max(total, done)},
            "counts": counts, "datasets": dsets,
            "jobs": [n.to_dict() for n in sorted(nodes, key=lambda n: (n.pass_no, n.started_at or "~", n.job))],
            "publish": self.publishes, "ram": self.ram.to_dict(), "notes": self.notes[-20:],
            "data_as_of": self._data_as_of(), "detail": self.status.get("detail"),
            # coverage v3 A3: what this sync deferred (and why), its budget, and the jobs waiting for admission
            "deferred": self.deferred[-200:], "caps": self.budget.to_dict(),
            "carried_in": sorted(self.carry_in), "suspended_s": round(self.suspended_s) or None,
            "admission_waits": _admission_waits(),
        }
        if final:
            doc.update(final)
        self.status = doc
        try:
            S.write_status(doc, self.s)
        except OSError as e:  # pragma: no cover
            log.warning("sync status write failed: %s", e)

    def publishes_planned(self) -> list[int]:
        return [] if not self.do_publish else list(range(self.passes))

    def _data_as_of(self) -> dict[str, Any]:
        ds = self.last_scan or self.first_scan
        if not ds:
            return {}
        reg = {k: v for k, v in ds.price_as_of.items() if v}
        core = [v for k, v in reg.items() if k in ("US", "EU", "UK", "JP")]
        return {"overall": max(core) if core else (max(reg.values()) if reg else None), "regions": reg}

    def _heartbeat_loop(self) -> None:
        last = time.time()
        while not self._hb_stop.wait(5.0):
            now = time.time()
            gap, last = now - last - 5.0, now
            if gap > 120:   # coverage v3 A3: the wall clock jumped - the machine slept; the budget counts it
                self.suspended_s += gap
                self.notes.append(f"{S.now_iso()} machine suspended ~{gap / 60:.0f} min (wall clock jumped); "
                                  "the sync budget counts it")
            self.ram.sample()
            self._check_stop()
            with self._lock:
                self._write_status("running")

    def _check_stop(self) -> None:
        if S.stop_path(self.s).exists():
            if not self.stop_event.is_set():
                log.warning("sync.STOP present: stopping (running jobs are cancelled)")
                self.notes.append(f"{S.now_iso()} stop requested")
            self.stop_event.set()
            self.yield_event.set()
        if not self.yield_event.is_set():
            why = None
            if S.yield_path(self.s).exists():
                why = "a refresh DAG is waiting for the sync"
            elif time.monotonic() - getattr(self, "_last_dag_probe", 0.0) < 15.0:
                pass   # lock probes are rare: a probe holds the lock for an instant and could collide with a DAG start
            else:
                self._last_dag_probe = time.monotonic()
                held = [d for d in REFRESH_DAGS if _dag_lock_held(d, self.s)]
                if held:
                    why = f"refresh DAG {held[0]} started"
            if why:
                log.warning("sync yields: %s (running jobs finish, nothing new starts)", why)
                self.notes.append(f"{S.now_iso()} yielded: {why}")
                self.yield_event.set()

    # ---------------------------------------------------------------- main
    def run(self) -> dict[str, Any]:
        lk = S.sync_lock(self.s)
        try:
            lk.acquire(timeout=0)
        except Timeout:
            log.info("sync already running (lock held): not starting")
            return {"status": "skipped", "reason": "already running"}
        try:
            S.write_owner(self.run_id, self.trigger, self.s)   # coverage v3 A3: who holds the lock (stale recovery)
            prev = S.read_status(self.s)
            for p in (S.stop_path(self.s), S.yield_path(self.s)):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            held = [d for d in REFRESH_DAGS if _dag_lock_held(d, self.s)]
            self.status = {"started_at": S.now_iso()}
            if held:
                self.status["detail"] = f"refresh DAG {held[0]} is running: the sync is skipped (the refresh covers it)"
                self.phase = "skipped"
                fin = {"finished_at": S.now_iso(), "result": "skipped", "previous_state": prev.get("state")}
                self._write_status("idle", fin)
                return {"status": "skipped", "reason": self.status["detail"]}
            if prev.get("state") == "interrupted":
                self.notes.append(f"previous sync {prev.get('run_id')} was interrupted at {prev.get('heartbeat_at')}")
            self._hb_stop = threading.Event()
            hb = threading.Thread(target=self._heartbeat_loop, name="sync-heartbeat", daemon=True)
            hb.start()
            try:
                return self._run()
            finally:
                self._hb_stop.set()
                hb.join(10)
        finally:
            S.clear_owner(self.run_id, self.s)
            try:
                lk.release()
            except Exception:  # pragma: no cover
                pass

    def _run(self) -> dict[str, Any]:
        t0 = time.monotonic()
        self.budget = C.Budget(self.caps)   # coverage v3 A3: the wall-clock budget starts with the run
        if self.caps.enabled and not self.dry_run:
            try:
                self.carry_in = C.read_carry(self.s, self.caps)
            except Exception as e:  # noqa: BLE001 - a broken carry file never blocks a sync
                self.notes.append(f"carry file unreadable: {e}"[:200])
            if self.carry_in:
                self.notes.append(f"{S.now_iso()} carried from the previous sync: {', '.join(sorted(self.carry_in))}"[:400])
        self.phase = "scan"
        self._write_status("scanning")
        res = self.scan_fn(self.s, datasets=self.only, ledger=self.ledger)
        self.first_scan = self.last_scan = res
        self.as_of = date.fromisoformat(res.as_of) if res.as_of else None
        log.info("sync %s: scan %.1fs %s; plan %s", self.run_id, res.seconds, res.counts(), sorted(res.plan))
        self._write_status("running")
        result = "success"
        try:
            for pass_no in range(1, self.passes + 1):
                if self.yield_event.is_set():
                    break
                self.phase = f"pass {pass_no}"
                nodes = self.build_pass(pass_no, res)
                self._write_status("running")
                if self.dry_run:
                    for n in nodes:
                        n.status, n.reason = "skipped", "dry_run"
                    continue
                self.execute(nodes)
                self.phase = f"verify {pass_no}"
                res = self.verify(pass_no)
                if self.do_publish and not self.stop_event.is_set():
                    self.phase = f"publish {pass_no}"
                    self._write_status("publishing")
                    self.publish_step(pass_no)
            if not self.dry_run and not self.stop_event.is_set():
                self.phase = "gaps"
                self._refresh_gaps()
            if self.do_publish and not self.dry_run:
                self.dq_step()
                self.qa_step()
        except Exception as e:  # never let one bug kill the status: record it
            result = "failed"
            msg = f"{type(e).__name__}: {e}"
            log.error("sync %s crashed: %s\n%s", self.run_id, msg, traceback.format_exc())
            self.notes.append(f"{S.now_iso()} sync error: {msg[:300]}")
        self._write_carry()
        try:
            self.last_scan = self.scan_fn(self.s, datasets=self.only, ledger=self.ledger)
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"final scan failed: {e}")
        self.ledger.save()
        nodes = list(self.nodes.values())
        failed = [n.job for n in nodes if n.status == "failed"]
        if result == "success" and (failed or self.stop_event.is_set()):
            result = "partial"
        if self.yield_event.is_set() and not self.stop_event.is_set():
            result = "yielded" if result == "success" else result
        self.phase = "done"
        self.ram.sample()
        fin = {"finished_at": S.now_iso(), "result": result, "seconds": round(time.monotonic() - t0, 1),
               "failed_jobs": failed}
        with self._lock:
            self._write_status("idle", fin)
        summary = self.summary()
        try:
            S.append_history(summary, self.s)
        except OSError:  # pragma: no cover
            pass
        log.info("sync %s finished %s in %.0fs: %s", self.run_id, result, fin["seconds"], json.dumps(summary["counts"]))
        return summary

    def summary(self) -> dict[str, Any]:
        st = self.status
        nodes = list(self.nodes.values())
        return {"run_id": self.run_id, "trigger": self.trigger, "result": st.get("result"),
                "started_at": st.get("started_at"), "finished_at": st.get("finished_at"), "seconds": st.get("seconds"),
                "as_of": st.get("as_of"), "data_as_of": st.get("data_as_of"), "counts": st.get("counts"),
                "progress": st.get("progress"),
                "due_first": sorted(d.dataset for d in (self.first_scan.datasets if self.first_scan else [])
                                    if d.reason == "due"),
                "skipped": [{"dataset": d["dataset"], "reason": d["reason"], "detail": (d.get("detail") or "")[:200],
                             "retry_at": d.get("retry_at"), "next_expected": d.get("next_expected"),
                             "keys": d.get("keys")}
                            for d in st.get("datasets") or [] if d["reason"] in ("not_yet_published", "source_unavailable",
                                                                                  "needs_key", "failed", "no_source")],
                "jobs": {n.job + (f"#{n.pass_no}" if n.pass_no > 1 else ""): {
                    "status": n.status, "reason": n.reason, "duration_s": n.duration_s, "rows": n.rows,
                    "lane": n.lane, "message": (n.message or "")[:160]} for n in nodes},
                "publish": self.publishes, "ram": self.ram.to_dict(), "failed_jobs": st.get("failed_jobs"),
                "notes": self.notes[-10:],
                # coverage v3 A3: what was deferred and why, the budget, the history timeouts applied
                "deferred": self.deferred, "caps": self.budget.to_dict(), "carried_in": sorted(self.carry_in),
                "timeouts": self.timeouts_applied, "suspended_s": round(self.suspended_s) or None}

    # ---------------------------------------------------------------- planning
    def build_pass(self, pass_no: int, res: ScanResult) -> list[Node]:
        reg = self.reg
        planned = res.plan if pass_no == 1 else self._pass2_plan(res)
        extra = {j for j in planned if j in reg.jobs}
        allowed = G.sync_set(reg, extra)
        nodes: dict[str, Node] = {}

        def add(job: str, trigger: str, it: PlanItem | None = None) -> None:
            spec = reg.jobs[job]
            n = nodes.get(job)
            if n is None:
                n = nodes[job] = Node(job, pass_no, G.lane(spec), G.tier(spec), trigger=trigger)
            if it is not None:
                n.params.update(it.params)
                n.datasets = sorted(set(n.datasets) | set(it.datasets))
                n.why += it.why
                for k, v in it.periods.items():
                    n.periods[k] = sorted(set(n.periods.get(k, [])) | set(v))
                if n.trigger in ("derive", "fingerprint") and trigger in ("dataset", "cadence", "upstream"):
                    n.trigger = trigger

        for job, it in planned.items():
            if job not in reg.jobs:
                continue
            spec = reg.jobs[job]
            p1 = (it.trigger in ("dataset", "upstream", "fingerprint") or job == "corporate_actions"
                  or job in P.MACRO_GROUP_JOBS) and G.tier(spec) == "core"
            if (pass_no == 1) == p1 or pass_no > 1 and it.trigger == "fingerprint":
                add(job, it.trigger, it)
        if pass_no > 1:   # prices staged by a pass-2 fetcher (BaoStock) are finalised again for their regions
            regs = sorted({d.split(".", 1)[1] for n in nodes.values() if "prices_staging" in reg.jobs[n.job].writes
                           for d in n.datasets if d.startswith("prices.")})
            if regs and "prices_finalize" in reg.jobs:
                add("prices_finalize", "upstream", PlanItem("prices_finalize", {"regions": regs},
                                                            [f"prices.{r}" for r in regs],
                                                            [f"regions {', '.join(regs)} staged in pass 2"]))
        roots = [j for j in nodes]
        want_tier = "core" if pass_no == 1 else None
        for j in G.downstream(reg, roots, allowed):
            spec = reg.jobs[j]
            if G.is_fetch(spec) and not any(d in nodes for d in spec.depends_on):
                continue   # root fetchers only through the scan plan
            if want_tier and G.tier(spec) != want_tier:
                continue
            add(j, "derive")
        if pass_no > 1:   # every derive job of the sync set is a candidate (fingerprint decides at its turn)
            for j in sorted(allowed):
                spec = reg.jobs[j]
                if not G.is_fetch(spec) or any(d in allowed and G.is_fetch(reg.jobs[d]) for d in spec.depends_on):
                    if G.is_fetch(spec) and not G.inputs(reg, j):
                        continue
                    add(j, "derive")
        else:             # core derive jobs whose inputs changed outside the plan (e.g. a weekly refresh output)
            for j in sorted(allowed):
                spec = reg.jobs[j]
                if j in nodes or G.is_fetch(spec) or G.tier(spec) != "core":
                    continue
                due, _why = G.inputs_changed(reg, j, self._last_ok(j), self.clock)
                if due:
                    add(j, "fingerprint")
        for job, e in self.carry_in.items():   # coverage v3 A3: work a previous sync deferred goes first
            spec = reg.jobs.get(job)
            if spec is None or self._post_carried(job):
                continue   # the post-publish guards: qa_step / dq_step run them
            if job not in nodes:
                if G.is_fetch(spec):
                    continue   # fetch jobs: only when the scan re-planned them (a dataset still due is still due)
                want = 1 if G.tier(spec) == "core" else 2
                if job not in allowed or want != pass_no:
                    continue
                add(job, "carry")
            nodes[job].carried = max(1, int(e.get("times") or 1))
            nodes[job].why.insert(0, f"deferred by the previous sync: {(e.get('reason') or '')[:160]}")
        out = list(nodes.values())
        for n in out:
            self.nodes[(pass_no, n.job)] = n
        log.info("sync pass %d: %d node(s): %s", pass_no, len(out), ", ".join(sorted(nodes)))
        return out

    def _pass2_plan(self, res: ScanResult) -> dict[str, PlanItem]:
        """Pass 2 takes the plan items left for it by pass 1 (cadence fetchers, extended fetch jobs)."""
        done = {n.job for (p, _), n in self.nodes.items() if p == 1}
        return {j: it for j, it in self.first_scan.plan.items() if j not in done} if self.first_scan else {}

    def _last_ok(self, job: str) -> datetime | None:
        if job not in self.last_ok:
            try:
                import polars as pl

                jr = self.runner.store.read()
                x = jr.filter((pl.col("job_id") == job) & pl.col("status").is_in(["success", "partial"]))
                self.last_ok[job] = S.parse_ts(x.get_column("started_at").max()) if x.height else None
            except Exception:  # noqa: BLE001
                self.last_ok[job] = None
        return self.last_ok[job]

    # ---------------------------------------------------------------- execution
    def _pressure(self) -> str | None:
        if self.pressure_fn is not None:
            return self.pressure_fn()
        from app.scheduler.runner import machine_pressure

        return machine_pressure()

    def gate(self, n: Node, nodes: dict[str, Node]) -> tuple[bool, str | None]:
        """Decide at a node's turn whether it runs: (run, skip_reason)."""
        if n.trigger in ("dataset", "cadence", "upstream"):
            hard = P.HARD_DEPS.get(n.job, frozenset())
            bad = [d for d in hard if d in nodes and nodes[d].status == "failed"]
            return (False, f"upstream {bad[0]} failed") if bad else (True, None)
        hard = P.HARD_DEPS.get(n.job, frozenset())
        bad = [d for d in hard if d in nodes and nodes[d].status == "failed"]
        if bad:
            return False, f"upstream {bad[0]} failed (data dependency)"
        last = self._last_ok(n.job)
        due, why = G.inputs_changed(self.reg, n.job, last, self.clock)
        if not due:
            return False, why
        iv = P.min_interval_h(n.job, False)
        if iv and last and self.now() - last < timedelta(hours=iv):
            return False, f"ran {last:%Y-%m-%d %H:%M} UTC; at most every {iv:g} h ({why})"
        n.why.append(why)
        return True, None

    def execute(self, nodes: list[Node]) -> None:
        by_job = {n.job: n for n in nodes}
        ids = set(by_job)
        anc = {j: G.ancestors(self.reg, j, ids) for j in ids}
        try:
            prio = self.reg.remaining_path(ids, "full")
        except Exception:  # noqa: BLE001
            prio = {}
        lanes: dict[str, int] = {}
        counts = {"network": 0, "heavy": 0, "light": 0}
        caps = {"network": P.NETWORK_MAX, "heavy": P.HEAVY_LANE_MAX, "light": P.LIGHT_LANE_MAX}
        running: dict[Future, Node] = {}
        heavy_wait_since: float | None = None
        with ThreadPoolExecutor(max_workers=P.NETWORK_MAX + P.HEAVY_LANE_MAX + P.LIGHT_LANE_MAX + 2,
                                thread_name_prefix="sync") as pool:
            while True:
                pending = [n for n in nodes if n.status == "pending"]
                if not pending and not running:
                    break
                self._check_stop()
                if self.yield_event.is_set():
                    for n in pending:
                        n.status, n.reason = "skipped", "stopped" if self.stop_event.is_set() else "yielded"
                    pending = []
                ready = [n for n in pending if all(by_job[a].done for a in anc[n.job])]
                # network first, then work carried from the previous sync, then the longest job first (critical
                # path): a long download never waits behind short ones (the remaining-path priority of the runner)
                ready.sort(key=lambda n: ready_order(n, prio))
                for n in ready:
                    # a deferred true data dependency (policy.HARD_DEPS) defers the job too; soft dependents go
                    # through the fingerprint gate as usual (they run only when some input changed)
                    up = next((a for a in sorted(P.HARD_DEPS.get(n.job, ())) if a in by_job
                               and _is_deferred(by_job[a])), None)
                    if up is not None:
                        self._defer(n, f"upstream {up} deferred")
                        continue
                    run, why = self.gate(n, by_job)
                    if not run:
                        n.status, n.reason = "skipped", why
                        n.finished_at = S.now_iso()
                        continue
                    ok, why = self.budget.can_start(n.pass_no, self._expected_s(n.job), bool(n.carried))
                    if not ok:   # coverage v3 A3: the sync budget - carried to the next sync
                        self._defer(n, why)
                        continue
                    if counts[n.lane] >= caps[n.lane]:
                        continue
                    spec = self.reg.jobs[n.job]
                    srcs = list(spec.sources) or [f"job:{n.job}"]
                    if n.lane == "network" and any(lanes.get(s, 0) >= P.lane_max(s) for s in srcs):
                        continue
                    if n.race_retry and any(set(spec.writes) & set(self.reg.jobs[r.job].writes)
                                            for r in running.values() if r.job in self.reg.jobs):
                        continue   # the retry of a file race waits until no other writer of its tables runs
                    if n.lane == "heavy" or spec.executor == "process":   # spawned interpreters cost RAM too
                        pr = self._pressure()
                        if pr:
                            heavy_wait_since = heavy_wait_since or time.monotonic()
                            if time.monotonic() - heavy_wait_since < 900:
                                if int(time.monotonic() - heavy_wait_since) % 60 < 2:
                                    log.info("sync: %s waits - machine under memory pressure (%s)", n.job, pr)
                                continue
                            self.notes.append(f"{S.now_iso()} {n.job} started under memory pressure ({pr})")
                        heavy_wait_since = None
                    counts[n.lane] += 1
                    if n.lane == "network":
                        for s in srcs:
                            lanes[s] = lanes.get(s, 0) + 1
                    self.budget.note_start(n.pass_no)
                    n.status, n.started_at = "running", S.now_iso()
                    running[pool.submit(self._run_job, n)] = n
                if not running:
                    if any(n.status == "pending" for n in nodes):
                        time.sleep(self.sleep_s)
                    continue
                done, _ = wait(list(running), timeout=self.sleep_s, return_when=FIRST_COMPLETED)
                for f in done:
                    n = running.pop(f)
                    counts[n.lane] -= 1
                    if n.lane == "network":
                        for s in list(self.reg.jobs[n.job].sources) or [f"job:{n.job}"]:
                            lanes[s] = lanes.get(s, 1) - 1
                    try:
                        f.result()
                    except Exception as e:  # pragma: no cover - _run_job never raises
                        n.status, n.message = "failed", str(e)
                    self.clock.invalidate(self.reg.jobs[n.job].writes)
                    if n.status == "failed" and not n.race_retry and _file_race(n.message, n.error_type):
                        # a concurrent writer swapped a partition the job was reading (two fetchers writing the same
                        # lake table, e.g. sec_xbrl_facts next to cn_fundamentals): run it again, alone on its tables
                        n.race_retry, n.status, n.reason = True, "pending", None
                        self.notes.append(f"{S.now_iso()} {n.job}: file race with a concurrent writer; re-queued")
                        log.warning("sync: %s hit a file race (%s); re-queued after the other writers", n.job,
                                    (n.message or "")[:160])
                    with self._lock:
                        self._write_status("running")

    def _run_job(self, n: Node) -> None:
        from app.scheduler.runner import RunOptions

        t0 = time.monotonic()
        trig = "schedule" if self.trigger in ("startup", "interval", "schedule") else "manual"
        try:
            spec = self.reg.jobs.get(n.job)
            if n.trigger == "post" and n.job in (P.QA_JOB, getattr(P, "DQ_JOB", "dq_checks")):
                why = self.budget.exhausted()   # coverage v3 A3: the guards wait for the next sync's budget
                if why and not self._post_carried(n.job):   # deferred once at most: a carried guard runs anyway
                    self._defer(n, f"{why}; the next sync runs it")
                    return
            # long jobs are not retried inside the sync (a 60-min timeout twice blocks the pass): the ledger retries
            # them on the next sync with back-off
            retries = 0 if (spec is not None and spec.timeout_min >= 45 and self.retries is not None) else self.retries
            params = dict(n.params)
            if spec is not None and "timeout_min" not in params:
                tmin = self._timeout_for(spec)
                if tmin and n.trigger == "publish":   # a killed publish wastes the sync: never below the contract
                    tmin = max(float(tmin), float(spec.timeout_min))
                if tmin:
                    params["timeout_min"] = tmin
            extra: dict[str, Any] = {}
            if self.caps.enabled and n.trigger not in ("publish", "post"):
                # coverage v3 A3: a job that cannot start within the budget (RAM floor, heavy slot) is deferred - once:
                # a job a previous sync already deferred (n.carried) starts in spill mode after the RAM wait instead
                # of being carried again (review 2026-11-07: prices_finalize was deferred on every sync)
                from app.ops import ram_budget as RB

                extra = {"ram_defer": not n.carried, "start_by": self.budget.deadline,
                         "ram_wait_max_s": max(60.0, min(RB.wait_max_s(), self.budget.remaining_s()))}
            elif n.trigger == "post" and n.job == P.DQ_JOB:
                extra = self._dq_admission()
            opts = RunOptions(as_of=self.as_of, params=params, trigger=trig, force=True,
                              dag_run_id=self.dag_run_id, retries=retries, wait_for_dags=False,
                              backoff_scale=0.5, **extra)
            rep = self.runner.run_jobs([n.job], opts)
            o = rep.outcomes.get(n.job)
            if o is None:
                n.status, n.message = "failed", "no outcome"
            else:
                n.status, n.reason, n.message = o.status, o.reason, (o.message or "")[:600] or None
                n.rows, n.error_type, n.run_ids = o.rows_written, o.error_type, list(o.run_ids)
                if o.status in ("success", "partial"):
                    self.last_ok[n.job] = datetime.now(UTC) - timedelta(seconds=o.duration_s or 0)
                elif o.status == "skipped" and o.reason == "deferred":   # RAM floor / start budget / heavy slot
                    why = (o.message or "could not start").removeprefix("deferred: ")
                    self._defer(n, why[:300], status_set=False)
        except Exception as e:  # noqa: BLE001
            n.status, n.message, n.error_type = "failed", f"{type(e).__name__}: {e}"[:600], type(e).__name__
        n.duration_s = round(time.monotonic() - t0, 1)
        n.finished_at = S.now_iso()
        log.info("sync job %s -> %s %s (%.0fs) %s", n.job, n.status, n.reason or "", n.duration_s, (n.message or "")[:200])

    def _defer(self, n: Node, why: str | None, status_set: bool = True) -> None:
        """Coverage v3 A3: skip a node as deferred (carried to the next sync) and record why."""
        why = why or "deferred"
        n.status, n.reason = "skipped", f"deferred: {why}"[:400]
        if status_set:
            n.finished_at = S.now_iso()
        self.deferred.append({"job": n.job, "pass_no": n.pass_no, "lane": n.lane, "trigger": n.trigger,
                              "reason": why[:300], "datasets": list(n.datasets)[:12], "deferred_at": S.now_iso()})
        log.info("sync: %s deferred - %s", n.job, why)

    def _write_carry(self) -> None:
        """The next sync's carry-over: this run's deferred jobs plus earlier carried ones it did not get to."""
        if not self.caps.enabled or self.dry_run:
            return
        attempted = {n.job for n in self.nodes.values() if n.status in ("success", "partial", "failed")}
        attempted |= self._carry_done
        mine = {d["job"] for d in self.deferred}
        keep = [e for j, e in self.carry_in.items() if j not in attempted and j not in mine]
        try:
            C.write_carry([d for d in self.deferred if d["job"] not in attempted] + keep, self.run_id, self.s,
                          self.carry_in)
        except OSError as e:  # pragma: no cover
            self.notes.append(f"carry file not written: {e}"[:200])

    def _timeouts_map(self) -> dict[str, dict[str, Any]]:
        if self._timeouts is None:
            try:
                self._timeouts = C.history_timeouts(self.runner.store.read(), self.reg, self.caps)
            except Exception as e:  # noqa: BLE001 - contract timeouts then
                log.warning("sync: history timeouts unavailable: %s", e)
                self._timeouts = {}
        return self._timeouts

    def _expected_s(self, job: str) -> float | None:
        m = self._timeouts_map().get(job) or {}
        return float(m["p95_min"]) * 60.0 if m.get("p95_min") else None

    def _timeout_for(self, spec: Any) -> float | None:
        """Coverage v3 A3: the job's timeout from its run history (app.ops.sync.caps.timeout_for: p95 x 1.5 clamped to
        [10 min, max(180 min, contract)], the contract timeout with fewer than 3 runs or after a timeout under a
        shorter history timeout). None = the contract timeout. With BIG360_SYNC_CAPS=0 the pre-v3 rule: a longer
        timeout (1.5 x the last success, at most 180 min) for jobs whose last success took > 80 % of the contract."""
        if self.caps.enabled:
            m = self._timeouts_map().get(spec.id)
            if not m or m.get("source") == "contract":
                return None
            self.timeouts_applied[spec.id] = {k: m.get(k) for k in ("timeout_min", "source", "p95_min", "n",
                                                                    "contract_min")}
            return float(m["timeout_min"])
        try:
            import polars as pl

            jr = self.runner.store.read()
            x = jr.filter((pl.col("job_id") == spec.id) & (pl.col("status") == "success")
                          & pl.col("duration_s").is_not_null())
            if x.is_empty():
                return None
            last = float(x.sort("started_at").get_column("duration_s")[-1])
        except Exception:  # noqa: BLE001
            return None
        if last < 0.8 * spec.timeout_min * 60:
            return None
        return int(min(180, max(spec.timeout_min, round(last * 1.5 / 60))))

    # ---------------------------------------------------------------- verify / ledger
    def verify(self, pass_no: int) -> ScanResult:
        """Re-scan, then update the ledger once per (dataset, entity, period) a fetch of this pass should have
        brought: present -> cleared; still missing and a fetch job failed -> failed / source_unavailable (parked after
        PARK_AFTER_FAILURES); still missing although every fetch succeeded -> not_yet_published with back-off
        (parked after NOT_PUBLISHED_PARK_ATTEMPTS); a job skipped for a missing key -> needs_key; a job of the period
        deferred (coverage v3 A3) and none failed -> no ledger change (not attempted end to end)."""
        post = self.scan_fn(self.s, datasets=self.only, ledger=self.ledger)
        by = post.by_id()
        now = datetime.now(UTC)
        items: dict[tuple[str, str, str], list[Node]] = {}
        for (p, _), n in self.nodes.items():
            if p != pass_no or not n.periods or n.status in ("pending",):
                continue
            for key, periods in n.periods.items():
                did, ent = key.split("|", 1)
                for period in periods:
                    items.setdefault((did, ent, period), []).append(n)
        ran_jobs = {n.job for n in self.nodes.values()}
        later = {d for j, it in (self.first_scan.plan.items() if self.first_scan else []) if j not in ran_jobs
                 for d in it.datasets} if pass_no < self.passes else set()
        for (did, ent, period), ns in items.items():
            d = by.get(did)
            if d is None or (did in later and not self.yield_event.is_set()):
                continue   # another planned fetch of this dataset runs in a later pass (e.g. BaoStock for CN)
            e = next((x for x in d.entities if x.entity == ent), None)
            still = e is not None and e.reason == "due"
            failed = [n for n in ns if n.status == "failed"]
            keyless = [n for n in ns if n.status == "skipped" and P.missing_key_names(n.message)]
            ran = [n for n in ns if n.status in ("success", "partial")]
            if not still:
                self.ledger.clear(did, ent)
                continue
            if keyless and not ran:
                names = P.missing_key_names(keyless[0].message)
                self.ledger.record(did, ent, period, "needs_key", 24 * 60, keyless[0].message, keyless[0].job, now,
                                   key=",".join(names))
            elif failed:
                f = failed[0]
                net = P.is_network_error(f.message) or P.is_network_error(f.error_type)
                reason = "source_unavailable" if net else "failed"
                prev = self.ledger.get(did, ent, period) or {}
                k = int(prev.get("attempts") or 0) if prev.get("reason") == reason else 0
                parked = k + 1 >= P.PARK_AFTER_FAILURES
                wait_m = P.PARK_HOURS * 60 if parked else P.RETRY_UNAVAILABLE_MIN[min(k, len(P.RETRY_UNAVAILABLE_MIN) - 1)]
                self.ledger.record(did, ent, period, reason, wait_m,
                                   f"{f.job}: {f.error_type or ''} {f.message or ''}".strip()
                                   + (f" (parked after {k + 1} failures)" if parked else ""), f.job, now)
                if parked:
                    self.notes.append(f"{S.now_iso()} ALERT {did}/{ent} parked after {k + 1} failed attempts ({f.job})")
            elif any(_is_deferred(n) for n in ns):
                # coverage v3 A3 review: a job of this period was deferred (e.g. the fetchers succeeded and
                # prices_finalize waits for the RAM floor): it was not attempted end to end, so the source is not to
                # blame - no not_yet_published / source_unavailable attempt or back-off; the carry re-runs it
                pass
            elif ran and all(n.status == "partial" and P.is_network_error(n.message)
                             for n in ran if n.lane == "network") and any(n.lane == "network" for n in ran):
                # every fetch ended partial with a network symptom (e.g. "baostock unreachable (3 consecutive
                # timeouts)"): the source is down for this period, not merely late
                f = next(n for n in ran if n.lane == "network")
                prev = self.ledger.get(did, ent, period) or {}
                k = int(prev.get("attempts") or 0) if prev.get("reason") == "source_unavailable" else 0
                parked = k + 1 >= P.PARK_AFTER_FAILURES
                wait_m = P.PARK_HOURS * 60 if parked else P.RETRY_UNAVAILABLE_MIN[min(k, len(P.RETRY_UNAVAILABLE_MIN) - 1)]
                self.ledger.record(did, ent, period, "source_unavailable", wait_m,
                                   f"{f.job}: {(f.message or '')[:300]}" + (f" (parked after {k + 1} attempts)" if parked else ""),
                                   f.job, now)
            elif ran:
                prev = self.ledger.get(did, ent, period) or {}
                k = int(prev.get("attempts") or 0) if prev.get("reason") == "not_yet_published" else 0
                parked = k + 1 >= P.NOT_PUBLISHED_PARK_ATTEMPTS
                wait_m = P.PARK_HOURS * 60 if parked else P.RETRY_NOT_PUBLISHED_MIN[min(k, len(P.RETRY_NOT_PUBLISHED_MIN) - 1)]
                self.ledger.record(did, ent, period, "not_yet_published", wait_m,
                                   f"{', '.join(n.job for n in ran if n.lane == 'network') or ran[0].job} ran but {period} is still missing at the source"
                                   + (" (parked)" if parked else ""), ran[0].job, now)
        self.ledger.save()
        post = self.scan_fn(self.s, datasets=self.only, ledger=self.ledger)
        self.last_scan = post
        return post

    # ---------------------------------------------------------------- publish
    def publish_step(self, pass_no: int) -> None:
        ran = [n for (p, _), n in self.nodes.items() if p == pass_no and n.status in ("success", "partial")]
        rec: dict[str, Any] = {"pass": pass_no, "status": "running", "started_at": S.now_iso()}
        self.publishes.append(rec)
        t0 = time.monotonic()
        try:
            out = self.publish_fn(pass_no, ran)
            rec.update(out or {})
            rec["status"] = (out or {}).get("status", "success")
        except Exception as e:  # noqa: BLE001 - a failed publish keeps the previous snapshot live
            rec.update(status="failed", error=f"{type(e).__name__}: {e}"[:400])
            log.error("sync publish failed: %s\n%s", e, traceback.format_exc())
        rec["seconds"] = round(time.monotonic() - t0, 1)
        rec["finished_at"] = S.now_iso()
        if rec.get("mode") in ("incremental", "full") and rec["status"] == "success":
            for j in P.POST_PUBLISH:
                n = Node(j, pass_no, "light", "core", trigger="post")
                self.nodes[(pass_no, f"post:{j}")] = n
                n.status, n.started_at = "running", S.now_iso()
                self._run_job(n)

    def _publish(self, pass_no: int, ran: list[Node]) -> dict[str, Any]:
        from app.ops.sync import publish as PB

        as_of = PB.default_as_of(self.s)
        plan = PB.plan_publish(self.s, as_of)
        out: dict[str, Any] = {"mode": plan["mode"], "as_of": plan["as_of"], "dirty": plan["dirty"],
                               "est_s": plan["est_s"], "full_s": plan["full_s"], "from_snapshot": plan["snapshot"]}
        if plan["mode"] == "none":
            out["status"] = "success"
            out["detail"] = "no serving section changed"
            return out
        noisy = set(plan["dirty"]) <= set(P.NOISY_SECTIONS)
        last = PB.last_published_at(self.s)
        if noisy and last is not None and datetime.now(UTC) - last < timedelta(hours=P.NOISY_PUBLISH_MIN_H):
            # only the news / event sections changed (the hourly fold): live news is served by the intraday store,
            # so the snapshot is republished at most every NOISY_PUBLISH_MIN_H hours for them
            out.update(mode="none", status="success",
                       detail=f"only {', '.join(sorted(plan['dirty']))} changed; next publish for them after "
                              f"{(last + timedelta(hours=P.NOISY_PUBLISH_MIN_H)):%Y-%m-%d %H:%M} UTC")
            return out
        job = "build_serving_db" if plan["mode"] == "full" else "serving_sections_publish"
        params: dict[str, Any] = {"as_of": as_of.isoformat()}
        if job == "serving_sections_publish":
            params["sections"] = list(plan["dirty"])
        n = Node(job, pass_no, "heavy", "core", params=params, trigger="publish")
        self.nodes[(pass_no, f"publish:{job}")] = n
        n.status, n.started_at = "running", S.now_iso()
        self._run_job(n)
        out["job"] = job
        out["status"] = "success" if n.status in ("success", "partial") else "failed"
        out["message"] = n.message
        from app.core.storage import current_serving_db

        cur = current_serving_db(self.s)
        out["snapshot"] = cur.name if cur else None
        return out

    def published(self) -> bool:
        """This run published a new snapshot (an incremental or full publish succeeded)."""
        return any(p.get("mode") in ("incremental", "full") and p.get("status") == "success" for p in self.publishes)

    def _post_carried(self, job: str) -> bool:
        """Coverage v3 A3 (caps.py docstring): a carry entry of a post-publish guard (qa_regression / dq_checks) that a
        previous sync deferred. build_pass never plans it as a pass job; qa_step / dq_step run it, also in a sync that
        publishes nothing (the live snapshot still needs its checks) and exempt from this sync's budget (deferred once
        at most). Their admission (RAM floor / heavy slot, _dq_admission) stays bounded."""
        return job in (P.QA_JOB, P.DQ_JOB) and job in self.carry_in

    def dq_step(self) -> None:
        """A5 2026-11: data-quality checks (job dq_checks, the full default rule set) after a sync that published a
        snapshot, at most every dq.POST_PUBLISH_MIN_INTERVAL_H hours (app.core.dq.post_publish_due). Under the weekly
        profile the nightly DAG, the job's only other caller, runs on Saturdays, so dq_checks last ran on 10-31 while
        the syncs kept publishing. Monitoring only (the snapshot is already live): results go to dq_results / the
        System page; skipped when the sync was stopped or yielded, or the machine is under memory pressure.
        Sync budget (coverage v3 A3): deferred to the next publishing sync when the budget is used up or the job's
        p95 run time exceeds what is left of it, and it never waits more than policy.DQ_START_WAIT_S for the heavy slot
        or the RAM floor (_dq_admission) - a big job of another process never holds the sync behind it.
        Carried (a previous sync deferred it, _post_carried): it runs in this sync even when nothing was published and
        is exempt from the budget rules (deferred once at most); when a complete run is recent enough (e.g. the Saturday
        chain ran it) the carry entry is settled (_carry_done) instead, so _write_carry drops it."""
        carried = self._post_carried(P.DQ_JOB)
        if not (self.published() or carried) or self.stop_event.is_set() or self.yield_event.is_set():
            return
        if P.DQ_JOB not in self.reg.jobs:
            return
        from app.core.dq import post_publish_due

        settled = False
        try:
            due, why = post_publish_due(self.runner.store.read(), self.now())
            settled = not due and why.startswith("dq_checks ran ")   # a complete run within the interval
        except Exception as e:  # noqa: BLE001 - never let the schedule check break the sync
            due, why = False, f"schedule check failed: {type(e).__name__}: {e}"[:300]
        if due:
            pr = self._pressure()
            if pr:
                due, why = False, f"memory pressure ({pr}); next sync retries"
        whys = [why] + ([f"deferred by the previous sync: {(self.carry_in[P.DQ_JOB].get('reason') or '')[:160]}"]
                        if carried else [])
        n = Node(P.DQ_JOB, self.passes, "heavy", "core", params={"trigger": "sync"}, trigger="post", why=whys)
        if carried:
            n.carried = max(1, int(self.carry_in[P.DQ_JOB].get("times") or 1))
        self.nodes[(self.passes, f"post:{P.DQ_JOB}")] = n
        if not due:
            n.status, n.reason = "skipped", why
            if carried and settled:
                self._carry_done.add(P.DQ_JOB)
            return
        late = None if carried else self._dq_budget_check()
        if late:
            self._defer(n, f"{late}; the next publishing sync runs it")
            return
        self.phase = "dq"
        n.status, n.started_at = "running", S.now_iso()
        with self._lock:
            self._write_status("running")
        self._run_job(n)

    def _dq_budget_check(self) -> str | None:
        """Why the post-publish dq_checks may not start in this sync (None = it may). The budget rules of the pass jobs
        (Budget.can_start) minus the per-pass job cap: dq_checks is not a pass job, and as a monitoring job it is never
        exempt from the p95 rule the way carried pass jobs are."""
        if not self.caps.enabled:
            return None
        why = self.budget.exhausted()
        if why:
            return why
        exp, left = self._expected_s(P.DQ_JOB), self.budget.remaining_s()
        if exp and exp > left:
            return f"needs ~{exp / 60:.0f} min (p95), {max(0.0, left) / 60:.0f} min left in the sync budget"
        return None

    def _dq_admission(self) -> dict[str, Any]:
        """RunOptions extras of the post-publish dq_checks: deferred (skipped, reason `deferred`) instead of waiting
        when the heavy slot (a big job >= 7 GB of any process holds it: heavy_slot.acquire_heavy would wait without
        limit) or the RAM floor keeps it from starting within policy.DQ_START_WAIT_S - or the sync budget, if sooner.
        Applies with the sync caps switched off too: the wait holds the sync lock."""
        from app.ops import ram_budget as RB

        start_by = time.time() + P.DQ_START_WAIT_S
        if self.caps.enabled:
            start_by = min(start_by, self.budget.deadline)
        return {"ram_defer": True, "start_by": start_by,
                "ram_wait_max_s": max(60.0, min(RB.wait_max_s(), start_by - time.time()))}

    def qa_step(self) -> None:
        """Alert hygiene 2026-10-30: after a sync that published a snapshot, the regression guard runs on it
        (job qa_regression, params suites=auto: the API sweep + golden checks every time, the UI smoke at most once
        a day and never next to a heavy job or with < 4 GB free RAM - app.qa.regression.plan_suites). Skipped when
        the sync was stopped or yielded to a refresh DAG (the refresh runs its own guard). Carried (a previous sync
        deferred it, _post_carried): it runs in this sync even when nothing was published."""
        carried = self._post_carried(P.QA_JOB)
        if not (self.published() or carried) or self.stop_event.is_set() or self.yield_event.is_set():
            return
        if P.QA_JOB not in self.reg.jobs:
            return
        self.phase = "qa"
        why = ["snapshot published by this sync"] if self.published() else []
        if carried:
            why.append(f"deferred by the previous sync: {(self.carry_in[P.QA_JOB].get('reason') or '')[:160]}")
        n = Node(P.QA_JOB, self.passes, "light", "core", params={"suites": "auto", "trigger": "sync"}, trigger="post",
                 why=why)
        if carried:
            n.carried = max(1, int(self.carry_in[P.QA_JOB].get("times") or 1))
        self.nodes[(self.passes, f"post:{P.QA_JOB}")] = n
        n.status, n.started_at = "running", S.now_iso()
        with self._lock:
            self._write_status("running")
        self._run_job(n)

    def _refresh_gaps(self) -> None:
        try:
            from app.ops.gaps import detect

            rep = detect(self.s, run_id=self.run_id)
            self.notes.append(f"{S.now_iso()} gap detection: {rep.summary()[:200]}")
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"gap detection failed: {e}")


def _sync_cfg(settings: Settings) -> dict[str, Any]:
    """config/ops.yaml `sync:` block (caps keys of app.ops.sync.caps.DEFAULTS)."""
    try:
        from app.ops.paths import config_dir, read_yaml

        return dict(read_yaml(config_dir(settings) / "ops.yaml").get("sync") or {})
    except Exception:  # noqa: BLE001
        return {}


def _admission_waits() -> list[dict[str, Any]]:
    """Jobs of this sync process waiting for RAM / the floor / the heavy slot (coverage v3 A2)."""
    try:
        from app.ops import ram_budget as RB

        return list(RB.current_waits().values())
    except Exception:  # noqa: BLE001
        return []


_RACE_HINTS = ("os error 2", "no such file", "filenotfounderror", "cannot find the file", "não conseguiu localizar",
               "the process cannot access the file")


def _file_race(message: str | None, error_type: str | None) -> bool:
    m = f"{error_type or ''} {message or ''}".lower()
    return any(h in m for h in _RACE_HINTS) and (".parquet" in m or "filenotfound" in m)


def _dag_lock_held(dag: str, settings: Settings, confirm: int = 3, gap_s: float = 0.4) -> bool:
    """True when a refresh DAG holds its lock. Debounced: other processes (the gap filler's governor, the tray)
    probe the same lock files by acquiring them for a few microseconds, so one busy probe is not proof - the lock
    must be busy on `confirm` probes `gap_s` apart (a real DAG holds it for hours)."""
    from app.ops.tasks import lock_held

    try:
        for k in range(max(1, confirm)):
            if not lock_held(f"dag_{dag}", settings):
                return False
            if k + 1 < confirm:
                time.sleep(gap_s)
        return True
    except Exception:  # pragma: no cover
        return False


# ------------------------------------------------------------------------------------------------ CLI


def _print_scan(res: ScanResult) -> None:
    print(f"scan {res.generated_at}  as_of={res.as_of}  {res.seconds}s  {res.counts()}")
    for d in res.datasets:
        print(f"  {d.dataset:32s} {d.reason:18s} have={d.data_as_of} exp={d.expected_as_of} next={d.next_expected}"
              f" jobs={','.join(d.jobs)}  {(d.detail or '')[:90]}")
        for e in d.entities:
            if e.reason != "fresh":
                print(f"      - {e.entity:24s} {e.reason:18s} {'(alt) ' if e.secondary else ''}{(e.detail or '')[:110]}")
    print("plan:")
    for j, it in res.plan.items():
        pp = {k: (v if not isinstance(v, list) or len(v) <= 8 else f"[{len(v)}]") for k, v in it.params.items()}
        print(f"  {j:26s} {it.trigger:9s} {pp} <- {', '.join(it.datasets)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.ops.sync", description="BiG Equity 360 startup sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sc = sub.add_parser("scan", help="freshness scan only (read-only)")
    sc.add_argument("--dataset", action="append")
    sc.add_argument("--json", action="store_true")
    rn = sub.add_parser("run", help="scan, fetch what is missing, publish")
    rn.add_argument("--trigger", default="manual", choices=["startup", "interval", "manual", "api", "tray"])
    rn.add_argument("--dataset", action="append")
    rn.add_argument("--passes", type=int, default=2)
    rn.add_argument("--no-publish", action="store_true")
    rn.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="print data/state/ops/sync_status.json")
    rq = sub.add_parser("request", help="ask the supervisor to start a sync now")
    rq.add_argument("--dataset", action="append")
    sub.add_parser("stop", help="stop the running sync (running jobs are cancelled)")
    a = ap.parse_args(argv)
    from app.core.logsetup import setup_logging

    s = get_settings()
    if a.cmd == "status":
        st = S.read_status(s)
        print(json.dumps({k: v for k, v in st.items() if k not in ("datasets",)}, indent=1, default=str))
        return 0
    if a.cmd == "request":
        print(json.dumps(S.request_sync(s, by="cli", datasets=a.dataset)))
        return 0
    if a.cmd == "stop":
        S.stop_path(s).write_text("cli", encoding="utf-8")
        print("stop requested")
        return 0
    s.ensure_dirs()
    setup_logging(log_file="sync.log", service="sync")
    if a.cmd == "scan":
        res = scan(s, datasets=a.dataset)
        if a.json:
            print(json.dumps(res.to_dict(), indent=1, default=str))
        else:
            _print_scan(res)
        return 0
    try:
        from app.ops import winsys

        winsys.set_priority(os.getpid(), "below_normal")
    except Exception:  # pragma: no cover
        pass
    rep = SyncRun(s, trigger=a.trigger, datasets=a.dataset, passes=a.passes, publish=not a.no_publish,
                  dry_run=a.dry_run).run()
    print(json.dumps({k: rep.get(k) for k in ("run_id", "result", "seconds", "as_of", "data_as_of", "counts",
                                              "failed_jobs", "ram", "publish")}, indent=1, default=str))
    return 0 if rep.get("result") in ("success", "partial", "yielded", None) or rep.get("status") == "skipped" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
