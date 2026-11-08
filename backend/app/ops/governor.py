"""Resource governor for the gap filler (O1.3). Pure decision logic over a resource sample (unit-tested).

Rules (config/ops.yaml `governor`, defaults below):
* hard pause - stop every running filler task, start nothing:
    on battery (pause_on_battery) | metered network (pause_on_metered) | free disk < min_disk_free_gb |
    free RAM < min_free_ram_gb (resume only above resume_free_ram_gb: hysteresis) | manual pause flag
* yield - start nothing new; stop tasks marked yield_to_dags (heavy, resumable) while a refresh DAG runs; while
  build_serving_db holds the exclusive heavy slot (pseudo-DAG EXCLUSIVE_SLOT) heavy tasks are stopped as well
* RAM cap - the filler process tree may use ram_cap_gb (default 4 GB): no new task when it would exceed the cap
  (a task always may start when nothing runs); above ram_cap_gb * hard_factor the lowest-priority preemptible task
  is stopped
* max_concurrency running tasks; BelowNormal CPU priority for every child (applied by the task runner)
* incident 2026-10-24:
  - machine guard (machine_guard(), shared with the worker's process jobs): free RAM < min_free_ram_gb OR the
    pagefile growing by > pagefile_growth_gb within pagefile_window_s while free RAM < 6 GB -> hard pause (the
    2026-10-24 night had 4 GB "free" but 9.5 GB in the pagefile, so the free-RAM rule alone never fired);
  - heavy tasks (module campaigns: GDELT E6, CC-NEWS; est_ram_gb >= heavy_ram_gb, e.g. GDELT redo) run alone: a
    heavy task starts only when nothing else runs, nothing starts next to one, and E6 (priority 70) goes before
    CC-NEWS (72);
  - the RAM in use is measured per task Job Object (orphaned descendants included), not by the parent-pid tree.
* coverage v3 A2: while a job of >= 7 GB holds the heavy slot (app.ops.heavy_slot, pseudo-DAG HEAVY_SLOT) the
  filler's heavy tasks are stopped and none starts (Decision.block_heavy); light tasks go on. The manual pause flag
  (filler.PAUSE) has an expiry (app.ops.pause): an expired file no longer pauses.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

DEFAULTS: dict[str, Any] = {
    "ram_cap_gb": 4.0,
    "hard_factor": 1.5,
    "min_free_ram_gb": 3.0,
    "resume_free_ram_gb": 3.5,
    "max_concurrency": 2,
    "priority": "below_normal",
    "pause_on_battery": True,
    "pause_on_metered": True,
    "min_disk_free_gb": 15.0,
    "yield_to_dags": ["nightly", "weekly", "monthly", "sync"],
    "tick_s": 15,
    "heavy_ram_gb": 3.0,
    "pagefile_growth_gb": 1.0,
    "pagefile_window_s": 300,
    "pause_grace_s": 180,
}

#: pseudo-DAG name in Sample.dags_running while build_serving_db holds the exclusive heavy slot (app.ops.heavy_slot):
#: always yielded to, whatever `yield_to_dags` says
EXCLUSIVE_SLOT = "serving_build"
#: pseudo-DAG name in Sample.dags_running while a big job (>= heavy_slot.slot_gb()) holds the heavy slot: heavy tasks
#: stop / wait, light tasks go on (coverage v3 A2)
HEAVY_SLOT = "heavy_slot"

#: (monotonic, pagefile_used_gb) samples of this process (machine_guard keeps ~15 min)
_PF_HISTORY: deque[tuple[float, float]] = deque(maxlen=400)


def machine_guard(sample: dict[str, float] | None = None, min_free_gb: float = 3.0, growth_gb: float = 1.0,
                  window_s: float = 300.0, now: float | None = None,
                  history: deque[tuple[float, float]] | None = None) -> dict[str, Any]:
    """Machine-level memory guard. {"pressure": bool, "reason": str | None, "avail_gb", "pagefile_used_gb",
    "pagefile_growth_gb"}. Call it regularly (every tick) so the pagefile trend has history."""
    if sample is None:
        from app.ops import winsys  # noqa: PLC0415

        sample = winsys.pressure_sample() or {}
    hist = _PF_HISTORY if history is None else history
    now = time.monotonic() if now is None else now
    avail = sample.get("avail_gb")
    pf = sample.get("pagefile_used_gb")
    growth = None
    if pf is not None:
        hist.append((now, float(pf)))
        while hist and now - hist[0][0] > 3 * window_s:
            hist.popleft()
        old = [v for t, v in hist if now - t <= window_s]
        if len(old) >= 2:
            growth = pf - min(old)
    reason = None
    if avail is not None and avail < min_free_gb:
        reason = f"free RAM {avail:.1f} GB < {min_free_gb:g} GB"
    elif growth is not None and growth > growth_gb and (avail is None or avail < 6.0):
        reason = f"pagefile growing (+{growth:.1f} GB in {window_s / 60:.0f} min, free RAM {avail or 0:.1f} GB)"
    return {"pressure": reason is not None, "reason": reason, "avail_gb": avail, "pagefile_used_gb": pf,
            "pagefile_growth_gb": round(growth, 2) if growth is not None else None}


@dataclass
class Sample:
    free_ram_gb: float | None = None
    filler_rss_gb: float = 0.0
    on_ac: bool | None = None
    metered: bool | None = None
    disk_free_gb: float | None = None
    dags_running: list[str] = field(default_factory=list)
    manual_pause: bool = False
    pagefile_growing: str | None = None          # machine_guard reason when the pagefile grows (else None)


@dataclass
class RunningInfo:
    task_id: str
    priority: int
    rss_gb: float
    preemptible: bool = True
    yield_to_dags: bool = False
    heavy: bool = False


@dataclass
class Decision:
    state: str                                   # running | paused | yielding | capped
    reasons: list[str] = field(default_factory=list)
    allow_new: bool = True
    stop: list[str] = field(default_factory=list)  # task ids to stop gracefully
    ram_budget_gb: float = 0.0                   # RAM a new task may still use
    block_heavy: bool = False                    # a big job holds the heavy slot: no heavy task may start

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reasons": self.reasons, "allow_new": self.allow_new, "stop": self.stop,
                "ram_budget_gb": round(self.ram_budget_gb, 2), "block_heavy": self.block_heavy}


def decide(sample: Sample, running: list[RunningInfo], cfg: dict[str, Any] | None = None,
           was_paused_for_ram: bool = False) -> Decision:
    c = {**DEFAULTS, **(cfg or {})}
    reasons: list[str] = []
    if sample.manual_pause:
        reasons.append("paused by user")
    if c["pause_on_battery"] and sample.on_ac is False:
        reasons.append("on battery")
    if c["pause_on_metered"] and sample.metered:
        reasons.append("metered network")
    if sample.disk_free_gb is not None and sample.disk_free_gb < float(c["min_disk_free_gb"]):
        reasons.append(f"disk free {sample.disk_free_gb:.1f} GB < {float(c['min_disk_free_gb']):g} GB")
    if sample.free_ram_gb is not None:
        thr = float(c["resume_free_ram_gb"] if was_paused_for_ram else c["min_free_ram_gb"])
        # RAM held by our own tasks would be released by stopping them: judge the machine without them only when
        # resuming (otherwise a running task could never be stopped by this rule)
        if sample.free_ram_gb < thr:
            reasons.append(f"free RAM {sample.free_ram_gb:.1f} GB < {thr:g} GB")
    if sample.pagefile_growing:
        reasons.append(sample.pagefile_growing)
    if reasons:
        return Decision("paused", reasons, allow_new=False, stop=[r.task_id for r in running])
    cap = float(c["ram_cap_gb"])
    used = sum(r.rss_gb for r in running)
    stop: list[str] = []
    state = "running"
    allow = len(running) < int(c["max_concurrency"])
    yield_dags = [d for d in sample.dags_running if d in set(c.get("yield_to_dags") or []) or d == EXCLUSIVE_SLOT]
    block_heavy = HEAVY_SLOT in sample.dags_running
    if block_heavy:   # coverage v3 A2: a big job holds the heavy slot - heavy tasks stop, light ones go on
        reasons.append("heavy slot held by a job of >= 7 GB (heavy tasks wait)")
        stop += [r.task_id for r in running if r.heavy]
    if yield_dags:
        reasons.append(f"refresh running ({', '.join(yield_dags)})")
        # the exclusive serving-build slot also stops heavy tasks (refresh-pipeline fixes 2026-10-26)
        stop += [r.task_id for r in running if r.yield_to_dags or (EXCLUSIVE_SLOT in yield_dags and r.heavy)]
        allow, state = False, "yielding"
    if used > cap * float(c["hard_factor"]):
        victims = sorted([r for r in running if r.preemptible and r.task_id not in stop],
                         key=lambda r: (-r.priority, -r.rss_gb))
        if victims:
            stop.append(victims[0].task_id)
            reasons.append(f"filler RAM {used:.1f} GB > hard cap {cap * float(c['hard_factor']):.1f} GB")
            state = "capped"
        allow = False
    elif used >= cap and running:
        reasons.append(f"filler RAM {used:.1f} GB >= cap {cap:g} GB")
        allow, state = False, "capped" if state == "running" else state
    if not allow and state == "running":
        reasons.append(f"max concurrency {c['max_concurrency']}")
    return Decision(state, reasons, allow_new=allow, stop=list(dict.fromkeys(stop)),
                    ram_budget_gb=max(0.0, cap - used), block_heavy=block_heavy)


def admit(est_ram_gb: float, decision: Decision, n_running: int, heavy: bool = False,
          running: list[RunningInfo] | None = None) -> bool:
    """A task may start when the governor allows new work and its RAM estimate fits the remaining budget (any task
    may start when nothing else runs: a single campaign larger than the cap still makes progress alone). Heavy
    tasks run alone: a heavy task needs an idle filler, and nothing starts next to a running heavy task."""
    if not decision.allow_new:
        return False
    if heavy and decision.block_heavy:
        return False
    if any(r.heavy for r in running or []):
        return False
    if heavy and n_running > 0:
        return False
    return n_running == 0 or est_ram_gb <= decision.ram_budget_gb


def is_heavy(est_ram_gb: float, cfg: dict[str, Any] | None = None) -> bool:
    c = {**DEFAULTS, **(cfg or {})}
    return float(est_ram_gb or 0) >= float(c["heavy_ram_gb"])


def should_preempt(candidate_priority: int, running: list[RunningInfo], margin: int = 20) -> str | None:
    """Task id to stop so a much more urgent candidate can run (lower number = more urgent)."""
    victims = [r for r in running if r.preemptible and r.priority - candidate_priority >= margin]
    if not victims:
        return None
    return sorted(victims, key=lambda r: (-r.priority, -r.rss_gb))[0].task_id
