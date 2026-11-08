"""Gap filler (O1.3/O1.4): long-running, low-priority process that drains data_gaps and runs the backfill campaigns.

    python -m app.ops.filler            (normally started by the supervisor: python -m app.ops.supervisor)
    python -m app.ops.filler --plan     print the governor decision + candidate queue (starts nothing)

Loop (every governor.tick_s, default 15 s):
  1. reap finished children: attempts / exponential back-off / unfillable after max_attempts on data_gaps rows,
     campaign bookkeeping (data/state/ops/filler.json)
  2. resource governor (app.ops.governor): battery, metered network, free RAM < 3 GB or a growing pagefile
     (machine guard), disk, refresh DAG running, RAM cap 4 GB for the filler's task jobs -> stop / pause tasks
     (graceful: campaign STOP file, else CTRL+BREAK; the task's Job Object is killed after the grace period -
     pause_grace_s (180 s) for a pause / RAM stop; every task is resumable). filler.PAUSE stops RUNNING tasks too.
  3. start the most urgent due work that fits: one task per source lane, per-source rate budgets
     (app.ops.budgets), RAM admission, campaign locks (an externally started GDELT backfill is adopted, never
     duplicated), preemption of a much less urgent preemptible task
  4. keep the PC awake while tasks run on AC power; housekeeping every 6 h; full gap detection every 24 h
Children run at BelowNormal priority (inherited by their own workers). Stop: Ctrl+C / SIGBREAK / SIGTERM or the
file data/state/ops/filler.STOP; pause: data/state/ops/filler.PAUSE (coverage v3 A4: with an expiry - `until=` in
the file, 24 h by default for new pauses, legacy files without it expire 7 days after they were written; an expired
file is ignored with a warning, app.ops.pause). Coverage v3 A2: while a job of >= 7 GB holds the heavy slot the
filler's heavy tasks stop / wait (governor pseudo-DAG HEAVY_SLOT); a task whose step the runner deferred (taskrun
result status deferred) is no attempt, backs off and is parked with an alert after DEFER_PARK_AFTER deferrals in a
row, and its retry skips the steps that already succeeded (taskrun step memo).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl
from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger, redact, setup_logging
from app.ops import winsys
from app.ops.budgets import RateBudgets
from app.ops.coverage import load_coverage
from app.ops.governor import RunningInfo, Sample, admit, decide, is_heavy, machine_guard, should_preempt
from app.ops.paths import ops_config, ops_state_dir, read_json, write_json_atomic
from app.ops.tasks import TaskProc, TaskSpec, campaign_tasks, find_lock_holder_pid, gap_tasks, lock_held, probe

log = get_logger("app.ops.filler")

EXIT_OK, EXIT_FAILED, EXIT_BLOCKED, EXIT_SKIPPED = 0, 1, 3, 4
#: taskrun's deferred exit: the blocked code with result status "deferred" (compatible with older fillers)
EXIT_DEFERRED = EXIT_BLOCKED
#: coverage v3 A2 review 2026-11-07: a task whose step the runner deferred (RAM floor / heavy slot; taskrun exit 3
#: with result status "deferred") is not an attempt, but deferrals in a row back off (DEFER_BASE_MIN x 2^(n-1), at most
#: DEFER_CAP_H) and from the DEFER_PARK_AFTER-th on the task is parked DEFER_PARK_H with an alert (filler state
#: `deferrals`, Data Health) - it can neither loop every 30 min nor stall unseen. A real attempt (ok / failed /
#: skipped) resets the count.
DEFER_BASE_MIN, DEFER_CAP_H, DEFER_PARK_AFTER, DEFER_PARK_H = 30.0, 6.0, 6, 24.0


def backoff_delay(attempts: int, base_min: float = 30.0, cap_h: float = 24.0) -> timedelta:
    """Exponential back-off: base * 2^(attempts-1), capped (attempts >= 1)."""
    m = base_min * (2 ** max(0, attempts - 1))
    return timedelta(minutes=min(m, cap_h * 60))


def state_path(settings: Settings | None = None):
    return ops_state_dir(settings) / "filler.json"


def read_state(settings: Settings | None = None) -> dict[str, Any]:
    return read_json(state_path(settings), {}) or {}


class Filler:
    def __init__(self, settings: Settings | None = None, cfg: dict[str, Any] | None = None):
        self.s = settings or get_settings()
        self.cfg = cfg or ops_config(self.s)
        self.gov = self.cfg["governor"]
        self.reg = load_coverage(self.s)
        self.budgets = RateBudgets.load(self.s, self.reg["sources"])
        self.running: dict[str, TaskProc] = {}
        self.state: dict[str, Any] = read_state(self.s)
        self.state.setdefault("campaigns", {})
        self.state.setdefault("recent", [])
        self.paused_for_ram = False
        self.stop_event = threading.Event()
        self.last_decision: dict[str, Any] = {}
        self.last_sample: dict[str, Any] = {}
        self.waiting: dict[str, str] = {}
        self.pause: dict[str, Any] | None = None
        self._last_hk = 0.0
        self._last_detect = float(self.state.get("last_detect_mono", 0) or 0)
        self._lock = FileLock(str(ops_state_dir(self.s) / "filler.lock"), timeout=0)

    # ------------------------------------------------------------------ resources
    def sample(self) -> Sample:
        mem = winsys.memory_status() or {}
        procs = winsys.process_list()
        rss = sum(t.rss_gb(procs) for t in self.running.values() if t.pid)
        g = self.gov
        mg = machine_guard(min_free_gb=float(g.get("min_free_ram_gb", 3.0)),
                           growth_gb=float(g.get("pagefile_growth_gb", 1.0)),
                           window_s=float(g.get("pagefile_window_s", 300)))
        pw = winsys.power_status()
        net = winsys.network_cost() if self.gov.get("pause_on_metered", True) else {"metered": None}
        dags = [d for d in ("nightly", "weekly", "monthly") if lock_held(f"dag_{d}", self.s)]
        try:  # startup sync (docs/STARTUP_SYNC_PLAN.md A4): yield while it fetches / publishes the latest data (pass 1)
            if lock_held("dag_sync", self.s):
                ph = str((read_json(ops_state_dir(self.s) / "sync_status.json", {}) or {}).get("phase") or "")
                if not ph.startswith(("pass 2", "verify 2")):
                    dags.append("sync")
        except Exception:  # pragma: no cover - advisory
            pass
        try:  # refresh-pipeline fixes 2026-10-26: build_serving_db's exclusive heavy slot
            from app.ops.governor import EXCLUSIVE_SLOT
            from app.ops.heavy_slot import exclusive_holder

            if exclusive_holder(self.s):
                dags.append(EXCLUSIVE_SLOT)
        except Exception:  # pragma: no cover - advisory
            pass
        slot_job = None
        try:  # coverage v3 A2: a job of >= 7 GB holds the heavy slot -> heavy tasks stop / wait
            from app.ops.governor import HEAVY_SLOT
            from app.ops.heavy_slot import big_holder

            bh = big_holder(self.s)
            if bh:
                dags.append(HEAVY_SLOT)
                slot_job = bh.get("job_id")
        except Exception:  # pragma: no cover - advisory
            pass
        from app.ops.pause import filler_pause

        pause = filler_pause(self.s)   # coverage v3 A4: expired / legacy pause files no longer pause
        self.pause = pause.to_dict() if pause.present else None
        sm = Sample(free_ram_gb=mem.get("avail_gb"), filler_rss_gb=rss, on_ac=pw.get("on_ac"), metered=net.get("metered"),
                    disk_free_gb=winsys.disk_free_gb(self.s.data_dir), dags_running=dags,
                    manual_pause=pause.active,
                    pagefile_growing=mg["reason"] if (mg.get("reason") or "").startswith("pagefile") else None)
        self.last_sample = {"free_ram_gb": round(sm.free_ram_gb or 0, 2), "filler_rss_gb": round(rss, 2),
                            "on_ac": sm.on_ac, "battery_pct": pw.get("battery_pct"), "metered": sm.metered,
                            "network": net.get("cost_type"), "disk_free_gb": round(sm.disk_free_gb or 0, 1),
                            "dags_running": dags, "manual_pause": sm.manual_pause, "pause": self.pause,
                            "heavy_slot_job": slot_job,
                            "pagefile_used_gb": round(mg["pagefile_used_gb"], 1) if mg.get("pagefile_used_gb") is not None else None,
                            "pagefile_growth_gb": mg.get("pagefile_growth_gb")}
        return sm

    def _infos(self) -> list[RunningInfo]:
        procs = winsys.process_list()
        return [RunningInfo(t.spec.task_id, t.spec.priority, t.rss_gb(procs) if t.pid else 0.0,
                            t.spec.preemptible, t.spec.yield_to_dags, self._heavy(t.spec)) for t in self.running.values()]

    def _heavy(self, spec: TaskSpec) -> bool:
        return bool(spec.heavy) or is_heavy(spec.est_ram_gb, self.gov)

    # ------------------------------------------------------------------ gaps bookkeeping
    def _update_gaps(self, ids: list[str], fn: Any) -> None:
        if not ids:
            return
        from app.core.storage import write_table
        from app.ops.gaps import gaps_lock, read_gaps

        with gaps_lock(self.s):
            df = read_gaps(self.s)
            if df.is_empty():
                return
            rows = []
            for r in df.iter_rows(named=True):
                if r["gap_id"] in ids:
                    r = dict(r)
                    fn(r)
                    r["updated_at"] = datetime.now(UTC)
                rows.append(r)
            write_table(pl.DataFrame(rows, schema=df.schema), "data_gaps", mode="overwrite", settings=self.s)

    def _deferral(self, t: TaskProc, outcome: str, err: str | None) -> dict[str, Any] | None:
        """Deferral bookkeeping per task (state `deferrals`, coverage v3 A2 review): the updated record for a deferred
        outcome ({n, next_attempt_at, parked, reason, ...}); a real attempt (ok / failed / skipped) drops the record,
        stopped / blocked leave it."""
        book = self.state.setdefault("deferrals", {})
        tid = t.spec.task_id
        old = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
        for k in [k for k, v in book.items() if str((v or {}).get("last_at") or "") < old]:
            book.pop(k, None)   # tasks that went away (gap filled elsewhere, campaign done)
        if outcome != "deferred":
            if outcome in ("ok", "failed", "skipped"):
                book.pop(tid, None)
            return None
        now = datetime.now(UTC)
        d = dict(book.get(tid) or {"first_at": now.isoformat(timespec="seconds")})
        n = int(d.get("n") or 0) + 1
        parked = n >= DEFER_PARK_AFTER
        delay = timedelta(hours=DEFER_PARK_H) if parked else backoff_delay(n, DEFER_BASE_MIN, DEFER_CAP_H)
        d.update(n=n, parked=parked, label=t.spec.label, kind=t.spec.kind, dataset=t.spec.dataset,
                 campaign=t.spec.campaign, last_at=now.isoformat(timespec="seconds"),
                 next_attempt_at=(now + delay).isoformat(timespec="seconds"), reason=(err or "deferred")[:300])
        book[tid] = d
        if parked:
            log.warning("task %s deferred %d times in a row (%s): parked %g h", tid, n, d["reason"][:160], DEFER_PARK_H)
        else:
            log.info("task %s deferred (%d in a row): retry in %.0f min", tid, n, delay.total_seconds() / 60)
        return d

    def _gap_result(self, t: TaskProc, outcome: str, err: str | None, deferral: dict[str, Any] | None = None) -> None:
        now = datetime.now(UTC)
        defaults = self.reg["defaults"]
        spec = self.reg["datasets"].get(t.spec.dataset or "", {})
        max_att = int(spec.get("max_attempts", defaults.get("max_attempts", 6)))
        base, cap = float(defaults.get("backoff_min", 30)), float(defaults.get("backoff_max_h", 24))
        outage_n = int(((spec.get("entities") or {}).get("outage_attempts")) or 3)

        def upd(r: dict[str, Any]) -> None:
            if r["status"] in ("filled", "manual", "unfillable", "ignored"):
                return
            if outcome == "deferred" and deferral:   # not an attempt; backs off, parked after DEFER_PARK_AFTER
                r["status"] = "open"
                r["next_attempt_at"] = datetime.fromisoformat(deferral["next_attempt_at"])
                mark = "parked after repeated deferrals"
                if deferral.get("parked") and mark not in (r["note"] or ""):
                    r["note"] = ((r["note"] or "") + f" | {mark} (RAM floor / heavy slot, "
                                 f"{deferral['n']} in a row): retries every {DEFER_PARK_H:g} h")
                return
            if outcome in ("stopped", "blocked"):
                r["status"] = "open"
                r["next_attempt_at"] = now + timedelta(minutes=5 if outcome == "stopped" else 30)
                return
            if outcome == "skipped":
                r["status"] = "manual"
                r["last_error"] = (err or "skipped (missing API key?)")[:1000]
                return
            r["attempts"] = int(r["attempts"] or 0) + 1
            r["last_attempt_at"] = now
            r["next_attempt_at"] = now + backoff_delay(r["attempts"], base, cap)
            outage = False
            try:
                outage = bool((json.loads(r["fill_params"] or "{}") or {}).get("outage"))
            except ValueError:
                pass
            if outcome == "ok":
                r["status"] = "open"     # the detector (detect_after / next run) decides whether it is filled
                if outage and r["attempts"] >= outage_n:
                    r["status"], r["gap_class"] = "unfillable", "unfillable"
                    r["note"] = (r["note"] or "") + f" | source has no files after {r['attempts']} attempts (outage)"
                elif r["attempts"] >= max_att:
                    r["status"], r["gap_class"] = "unfillable", "unfillable"
                    r["note"] = (r["note"] or "") + f" | still missing after {r['attempts']} successful fills: source has no data"
                return
            r["last_error"] = redact(err or "failed")[:1000]
            r["status"] = "unfillable" if r["attempts"] >= max_att else "failed"
            if r["status"] == "unfillable":
                r["gap_class"] = "unfillable"
                r["note"] = (r["note"] or "") + f" | gave up after {r['attempts']} failed attempts"

        self._update_gaps(t.spec.gap_ids, upd)

    def _campaign_result(self, t: TaskProc, outcome: str, err: str | None,
                         deferral: dict[str, Any] | None = None) -> None:
        cid = t.spec.campaign
        if not cid:
            return
        cst = self.state["campaigns"].setdefault(cid, {})
        now = datetime.now(UTC)
        cst["last_finished_at"] = now.isoformat(timespec="seconds")
        cst["last_outcome"] = outcome
        if outcome != "deferred":
            cst.pop("deferrals", None)
        if outcome == "ok":
            cst.update(attempts=0, last_success_at=now.isoformat(timespec="seconds"), last_error=None, next_attempt_at=None)
        elif outcome == "deferred" and deferral:   # coverage v3 A2 review: not an attempt; backs off, parks
            cst.update(next_attempt_at=deferral["next_attempt_at"], deferrals=deferral["n"],
                       last_deferred_reason=deferral["reason"])
        elif outcome in ("stopped", "blocked"):
            cst["next_attempt_at"] = (now + timedelta(minutes=5 if outcome == "stopped" else 30)).isoformat(timespec="seconds")
        else:
            n = int(cst.get("attempts") or 0) + 1
            cst.update(attempts=n, last_error=redact(err or "failed")[:1000],
                       next_attempt_at=(now + backoff_delay(n, 30, 24)).isoformat(timespec="seconds"))

    def _finish(self, t: TaskProc, code: int) -> None:
        res = t.result() or {}
        stopped = t.stop_requested_at is not None
        if t.adopted:
            outcome = "stopped" if stopped else "ok"
        elif stopped:
            outcome = "stopped"
        elif code == EXIT_OK:
            outcome = "ok"
        elif code == EXIT_DEFERRED and res.get("status") == "deferred":   # coverage v3 A2 review: taskrun's result
            outcome = "deferred"
        elif code == EXIT_BLOCKED or (t.spec.lock and code == 2):
            outcome = "blocked"
        elif code == EXIT_SKIPPED:
            outcome = "skipped"
        else:
            outcome = "failed"
        err = None
        if outcome in ("failed", "skipped", "deferred"):
            err = res.get("message") or "\n".join(t.tail(1500).strip().splitlines()[-6:]) or f"exit code {code}"
        log.info("task %s finished exit=%s outcome=%s", t.spec.task_id, code, outcome)
        deferral = self._deferral(t, outcome, err)
        if t.spec.kind == "gap":
            self._gap_result(t, outcome, err, deferral)
        if t.spec.kind == "campaign":
            self._campaign_result(t, outcome, err, deferral)
        if t.spec.kind == "detect" and outcome == "ok":
            self.state["last_detect_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        t.cleanup()
        rec = {"task_id": t.spec.task_id, "label": t.spec.label, "kind": t.spec.kind, "outcome": outcome, "exit": code,
               "started_at": t.started_at.isoformat(timespec="seconds") if t.started_at else None,
               "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "error": (err or "")[:500] or None,
               "adopted": t.adopted, "units": res.get("units"), "leftovers_killed": getattr(t, "leftovers_killed", 0) or None}
        self.state["recent"] = ([rec] + list(self.state.get("recent") or []))[:60]

    def reap(self) -> None:
        for tid, t in list(self.running.items()):
            code = t.poll()
            if code is not None:
                self.running.pop(tid)
                self._finish(t, code)

    # ------------------------------------------------------------------ scheduling
    def _detect_task(self) -> TaskSpec | None:
        every = float(self.cfg["filler"].get("detect_every_h", 24))
        last = self.state.get("last_detect_at")
        if last:
            try:
                if datetime.now(UTC) - datetime.fromisoformat(last) < timedelta(hours=every):
                    return None
            except ValueError:
                pass
        if "detect" in self.running:
            return None
        from app.ops.tasks import _taskrun_cmd, _write_spec

        sp, rp = _write_spec("detect", {"kind": "detect"}, self.s)
        return TaskSpec("detect", "Gap detection (all datasets)", "detect", 5, _taskrun_cmd(sp), source="local",
                        cost_units=1, est_ram_gb=1.2, preemptible=False, yield_to_dags=True, spec_path=sp, result_path=rp)

    def candidates(self) -> list[TaskSpec]:
        from app.ops.gaps import read_gaps

        self.reg = load_coverage(self.s)
        gaps = read_gaps(self.s)
        skip = set(self.running)
        out: list[TaskSpec] = []
        d = self._detect_task()
        if d:
            out.append(d)
        out += campaign_tasks(self.reg, self.state, gaps, self.s, skip_ids=skip)
        out += gap_tasks(gaps, self.reg, self.s, skip_ids=skip)
        return sorted(out, key=lambda t: t.priority)

    def start_next(self, dec: Any) -> None:
        maxc = int(self.gov.get("max_concurrency", 2))
        self.waiting = {}
        for c in self.candidates():
            if len(self.running) >= maxc:
                self.waiting[c.task_id] = "max concurrency"
                continue
            if c.source and c.source != "local" and any(t.spec.source == c.source for t in self.running.values()):
                self.waiting[c.task_id] = f"source lane {c.source} busy"
                continue
            if c.lock and lock_held(c.lock, self.s):
                pid = find_lock_holder_pid(self.reg["campaigns"].get(c.campaign or "", {}), self.s) if c.campaign else None
                if pid and c.task_id not in self.running:
                    self.running[c.task_id] = TaskProc.adopt(c, pid, self.s)
                    log.info("adopted external %s (pid %s)", c.task_id, pid)
                else:
                    self.waiting[c.task_id] = f"lock {c.lock} held"
                continue
            if not self.budgets.can_spend(c.source, c.cost_units):
                self.waiting[c.task_id] = f"rate budget {c.source} (retry in {self.budgets.wait_s(c.source, c.cost_units) / 60:.0f} min)"
                continue
            infos = self._infos()
            heavy = self._heavy(c)
            if not admit(c.est_ram_gb, dec, len(self.running), heavy=heavy, running=infos):
                busy_heavy = [r.task_id for r in infos if r.heavy]
                victim = should_preempt(c.priority, infos)
                if victim and victim in self.running:
                    log.info("preempting %s for %s", victim, c.task_id)
                    self.running[victim].request_stop()
                if not dec.allow_new:
                    self.waiting[c.task_id] = "; ".join(dec.reasons)
                elif heavy and dec.block_heavy:
                    self.waiting[c.task_id] = (f"heavy slot held by {self.last_sample.get('heavy_slot_job') or 'a job'}"
                                               " of >= 7 GB: heavy tasks wait")
                elif busy_heavy:
                    self.waiting[c.task_id] = f"heavy task {busy_heavy[0]} runs alone"
                elif heavy and self.running:
                    self.waiting[c.task_id] = "heavy task: waits until no other task runs"
                else:
                    self.waiting[c.task_id] = "RAM cap"
                continue
            tp = TaskProc(c, self.s, priority=str(self.gov.get("priority", "below_normal")))
            try:
                tp.start()
            except OSError as e:
                log.error("cannot start %s: %s", c.task_id, e)
                continue
            self.running[c.task_id] = tp
            self.budgets.spend(c.source, c.cost_units)
            if c.kind == "gap":
                self._update_gaps(c.gap_ids, lambda r: r.update(status="filling") if r["status"] in ("open", "failed", "queued") else None)
            if c.campaign:
                cst = self.state["campaigns"].setdefault(c.campaign, {})
                cst["last_started_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            dec.ram_budget_gb -= c.est_ram_gb

    # ------------------------------------------------------------------ loop
    def tick(self) -> dict[str, Any]:
        self.reap()
        sm = self.sample()
        dec = decide(sm, self._infos(), self.gov, self.paused_for_ram)
        self.paused_for_ram = any(r.startswith(("free RAM", "pagefile")) for r in dec.reasons)
        # a pause (user flag, RAM / pagefile pressure, battery ...) stops RUNNING tasks with a short graceful window
        pause_grace = float(self.gov.get("pause_grace_s", 180)) if dec.state == "paused" else None
        for tid in dec.stop:
            if tid in self.running:
                if self.running[tid].stop_requested_at is None:
                    log.info("governor %s: stopping %s (%s)", dec.state, tid, "; ".join(dec.reasons))
                self.running[tid].request_stop(pause_grace)
        for t in list(self.running.values()):
            t.enforce_stop()
        self.reap()
        if dec.allow_new and not self.stop_event.is_set():
            try:
                self.start_next(dec)
            except Exception:
                log.exception("filler scheduling error")
        else:
            self.waiting = {"*": "; ".join(dec.reasons) or dec.state}
        winsys.KEEP_AWAKE.set("filler", bool(self.running) and sm.on_ac is not False)
        if time.monotonic() - self._last_hk > float(self.cfg["housekeeping"].get("every_h", 6)) * 3600:
            self._last_hk = time.monotonic()
            try:
                from app.ops.housekeeping import run_housekeeping

                self.state["housekeeping"] = run_housekeeping(self.s, self.cfg["housekeeping"])
            except Exception as e:
                log.warning("housekeeping failed: %s", e)
        self.last_decision = dec.to_dict()
        self.budgets.save()
        self.write_state()
        return self.last_decision

    def write_state(self) -> None:
        procs = winsys.process_list()
        run = {}
        for tid, t in self.running.items():
            run[tid] = {"label": t.spec.label, "kind": t.spec.kind, "pid": t.pid, "adopted": t.adopted,
                        "started_at": t.started_at.isoformat(timespec="seconds") if t.started_at else None,
                        "priority": t.spec.priority, "source": t.spec.source, "dataset": t.spec.dataset,
                        "campaign": t.spec.campaign, "gap_ids": t.spec.gap_ids[:20], "stopping": t.stop_requested_at is not None,
                        "rss_gb": round(t.rss_gb(procs) if t.pid else 0.0, 2), "heavy": self._heavy(t.spec),
                        "leftovers_killed": t.leftovers_killed,
                        "log": str(t.log_path.relative_to(self.s.logs_dir)).replace("\\", "/")}
        camps = {}
        for cid, c in self.reg.get("campaigns", {}).items():
            camps[cid] = {**(self.state["campaigns"].get(cid) or {}), "label": c.get("label", cid), "kind": c.get("kind"),
                          "priority": c.get("priority"), "source": c.get("source"),
                          "progress": probe(c, self.s) if c.get("progress") else None,
                          "running": f"campaign:{cid}" in self.running}
        self.state.update({
            "pid": os.getpid(), "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "started_at": self.state.get("started_at_run"), "pause": self.pause,
            "governor": {**self.last_decision, "sample": self.last_sample,
                                                                       "config": {k: self.gov.get(k) for k in ("ram_cap_gb", "min_free_ram_gb", "max_concurrency", "priority", "pause_on_battery", "pause_on_metered", "min_disk_free_gb")}},
            "running": run, "waiting": dict(list(self.waiting.items())[:40]), "budgets": self.budgets.snapshot(),
            "campaign_status": camps, "keep_awake": winsys.KEEP_AWAKE.reasons(),
        })
        write_json_atomic(state_path(self.s), self.state)

    def shutdown(self, grace_s: float = 300.0) -> None:
        log.info("filler stopping %d task(s)", len(self.running))
        for t in self.running.values():
            if not t.adopted:
                t.request_stop()
        deadline = time.monotonic() + grace_s
        while any(not t.adopted and t.poll() is None for t in self.running.values()) and time.monotonic() < deadline:
            time.sleep(1)
        for t in list(self.running.values()):
            if not t.adopted and t.poll() is None and t.pid:
                t.kill()
            t.cleanup()
        self.reap()
        winsys.KEEP_AWAKE.set("filler", False)
        self.running.clear()
        self.state["stopped_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        self.write_state()

    def run_forever(self) -> None:
        tick = float(self.gov.get("tick_s", 15))
        stop_file = ops_state_dir(self.s) / "filler.STOP"
        while not self.stop_event.is_set():
            if stop_file.exists():
                log.info("filler.STOP present - stopping")
                try:
                    stop_file.unlink()
                except OSError:
                    pass
                break
            try:
                self.tick()
            except Exception:
                log.exception("filler tick error")
            self.stop_event.wait(tick)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="app.ops.filler", description="BiG Equity 360 gap filler")
    ap.add_argument("--plan", action="store_true", help="print the governor decision and the candidate queue, start nothing")
    a = ap.parse_args(argv)
    s = get_settings()
    s.ensure_dirs()
    setup_logging(log_file="filler.log", service="filler")
    winsys.set_priority(os.getpid(), "below_normal")
    f = Filler(s)
    try:
        f._lock.acquire(timeout=0)
    except Timeout:
        print("filler already running", file=sys.stderr)
        return 2
    f.state["started_at_run"] = datetime.now(UTC).isoformat(timespec="seconds")
    log.info("filler starting pid=%d data=%s", os.getpid(), s.data_dir)

    def handler(signum: int, _frame: Any) -> None:
        log.info("signal %s", signum)
        f.stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
    try:
        if a.plan:
            sm = f.sample()
            dec = decide(sm, f._infos(), f.gov, False)
            print(json.dumps({"sample": f.last_sample, "decision": dec.to_dict(),
                              "candidates": [{"task_id": c.task_id, "label": c.label, "priority": c.priority,
                                              "source": c.source, "est_ram_gb": c.est_ram_gb, "lock": c.lock}
                                             for c in f.candidates()]}, indent=1, default=str))
            return 0
        f.run_forever()
        return 0
    finally:
        if not a.plan:
            f.shutdown(grace_s=300)
        try:
            f._lock.release()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
