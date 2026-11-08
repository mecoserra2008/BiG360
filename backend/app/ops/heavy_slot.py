"""Exclusive "heavy" slot for build_serving_db (refresh-pipeline fixes 2026-10-26) and the heavy-job slot by RAM
budget (coverage v3 A2, 2026-11). Owner: platform.

The serving build is the most memory-hungry step of the weekly refresh (DuckDB budget BIG360_SERVING_DUCKDB_MEMORY
plus ~1.5 GB). Before 2026-10-26 it ran next to process jobs of the same DAG, the hourly news DAG and gap-filler
tasks, slowed down 20x and ran out of memory. Rules:

* jobs declare a resource class (jobs.yaml `resource`, default: `exclusive` for EXCLUSIVE_JOBS, `big` for a RAM
  budget (mem_gb) >= SLOT_GB, `heavy` for executor=process jobs (and thread jobs of >= PEER_GB), `light` otherwise);
* an **exclusive** job announces itself first (marker), then waits until no heavy job and no big job holding the
  slot runs (up to EXCLUSIVE_WAIT_MAX_S), then runs alone;
* a **heavy** job announces itself, then checks for an exclusive holder; while one exists it withdraws and waits (up
  to HEAVY_WAIT_MAX_S, then runs anyway with a warning) - or, for the hourly news jobs (DEFER_ON_EXCLUSIVE), is
  deferred to the next hourly run;
* the gap filler treats a held exclusive slot like a running refresh DAG (no new task, yielding tasks stop).

Coverage v3 A2 (87 of 950 measured runs saw < 2 GB free; peaks up to 21.9 GB on a 31.6 GB laptop): heavy jobs
strictly one at a time.
* a **big** job (budget >= SLOT_GB = 7 GB, env BIG360_HEAVY_SLOT_GB) takes the heavy slot exclusively: no other job
  with a budget >= PEER_GB (3 GB, env BIG360_HEAVY_PEER_GB) runs alongside it, in any runner (worker DAGs, the
  startup sync, `app.cli jobs run`, filler tasks). It announces a `big` marker (state waiting), waits until no
  exclusive holder, no other big holder, no older waiting big (FIFO by announce time: no two bigs wait on each other)
  and no peer heavy marker exists, then switches the marker to `holding`. Another big holding the slot (or an
  exclusive job) is never bypassed; after BIG_WAIT_MAX_S it stops waiting for peers / queued bigs only;
* a heavy job with a budget >= PEER_GB backs off while any big marker exists (waiting or holding), so a waiting big
  is not starved by a stream of medium jobs; it runs anyway after HEAVY_WAIT_MAX_S only while the big is merely
  waiting, never next to a big that holds the slot. Heavy jobs below PEER_GB ignore big markers;
* heavy markers record mem_gb; a legacy marker without it counts as a peer (conservative);
* the gap filler stops / does not start its heavy tasks (est_ram_gb >= heavy_ram_gb) while a big job holds the slot
  (governor pseudo-DAG HEAVY_SLOT); its light tasks go on;
* every wait is recorded with its reason (app.ops.ram_budget.note_wait: GET /system/status `admission_waits`).

Announce-then-check on both sides means two racing jobs always see each other (no overlap, no deadlock: only the
heavy side backs off). Markers are small JSON files in data/state/ops/heavy/ with the owner pid and an expiry, so a
crashed worker never blocks the slot (dead pid or expired marker = ignored and removed). Cross-process: the worker,
`app.cli jobs run`, and the filler all see the same markers. BIG360_HEAVY_SLOT=0 disables the slot (tests).
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

EXCLUSIVE_JOBS: frozenset[str] = frozenset({"build_serving_db"})
#: hourly news jobs: skipped while the slot is held (the next hourly run folds the missed hours)
DEFER_ON_EXCLUSIVE: frozenset[str] = frozenset({"news_hourly", "news_finalise"})
EXCLUSIVE_WAIT_MAX_S = 60 * 60.0
HEAVY_WAIT_MAX_S = 90 * 60.0
#: coverage v3 A2: after this a waiting big job stops waiting for medium peers and queued bigs (never for a holder)
BIG_WAIT_MAX_S = 120 * 60.0
POLL_S = 15.0
DEFAULT_SLOT_GB = 7.0
DEFAULT_PEER_GB = 3.0
CLASSES = ("light", "heavy", "big", "exclusive")


def enabled() -> bool:
    return os.environ.get("BIG360_HEAVY_SLOT", "1") != "0"


def _env_gb(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, default))
        return v if v > 0 else default
    except ValueError:
        return default


def slot_gb() -> float:
    """Budget from which a job takes the heavy slot exclusively (BIG360_HEAVY_SLOT_GB, default 7)."""
    return _env_gb("BIG360_HEAVY_SLOT_GB", DEFAULT_SLOT_GB)


def peer_gb() -> float:
    """Budget from which a job may not run next to a slot holder (BIG360_HEAVY_PEER_GB, default 3)."""
    return _env_gb("BIG360_HEAVY_PEER_GB", DEFAULT_PEER_GB)


def slot_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).state_dir / "ops" / "heavy"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Marker:
    path: Path
    job_id: str
    kind: str
    record: dict[str, Any] = field(default_factory=dict)
    #: companion markers released together (a big holder also holds a `heavy` marker, so every reader of the heavy
    #: markers - the serving build, the QA smoke planner, the studio lane - still sees it)
    extra: list[Path] = field(default_factory=list)

    def release(self) -> None:
        for p in [self.path, *self.extra]:
            _unlink(p)

    def set_state(self, state: str) -> None:
        self.record["state"] = state
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.record), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:  # pragma: no cover
            pass


def _unlink(p: Path, tries: int = 25) -> None:
    """Remove a marker. On Windows a delete fails while another runner is reading the file (PermissionError): retry
    briefly instead of leaving a marker that would hold the slot until it expires (coverage v3 A2)."""
    for i in range(tries):
        try:
            p.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.01 * (i + 1) if i < 10 else 0.1)
    log.warning("heavy slot marker %s could not be removed (kept until it expires)", p.name)  # pragma: no cover


def _alive(pid: int) -> bool:
    try:
        from app.ops.winsys import pid_running

        return bool(pid_running(pid))
    except Exception:  # pragma: no cover
        return True


def _write(kind: str, job_id: str, ttl_s: float, settings: Settings | None, mem_gb: float | None = None,
           state: str | None = None) -> Marker:
    d = slot_dir(settings)
    p = d / f"{kind}-{job_id}-{os.getpid()}-{secrets.token_hex(3)}.json"
    rec = {"kind": kind, "job_id": job_id, "pid": os.getpid(), "thread": threading.current_thread().name,
           "started_at": datetime.now(UTC).isoformat(timespec="seconds"), "expires": time.time() + ttl_s,
           "mem_gb": None if mem_gb is None else float(mem_gb), "seq": time.time_ns()}
    if state:
        rec["state"] = state
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec), encoding="utf-8")
    os.replace(tmp, p)
    return Marker(p, job_id, kind, rec)


def holders(kind: str, settings: Settings | None = None, exclude: Marker | None = None) -> list[dict[str, Any]]:
    """Live markers of `kind` (stale ones - dead owner or expired - are removed)."""
    out = []
    for f in slot_dir(settings).glob(f"{kind}-*.json"):
        if exclude is not None and f == exclude.path:
            continue
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
        rec["_file"] = f.name
        out.append(rec)
    return out


def _is_peer(rec: dict[str, Any]) -> bool:
    mem = rec.get("mem_gb")
    return mem is None or float(mem) >= peer_gb()


def _note(job_id: str, reason: str | None, settings: Settings | None) -> None:
    try:
        from app.ops import ram_budget as RB  # noqa: PLC0415

        if reason is None:
            RB.clear_wait(job_id, settings)
        else:
            RB.note_wait(job_id, "slot", reason, settings)
    except Exception:  # pragma: no cover - visibility only
        pass


def _names(recs: list[dict[str, Any]]) -> str:
    return ", ".join(sorted({str(b.get("job_id")) for b in recs})) or "-"


def exclusive_holder(settings: Settings | None = None) -> dict[str, Any] | None:
    if not enabled():
        return None
    h = holders("exclusive", settings)
    return h[0] if h else None


def big_holder(settings: Settings | None = None) -> dict[str, Any] | None:
    """The big job holding the heavy slot (coverage v3 A2), or None."""
    if not enabled():
        return None
    h = [b for b in holders("big", settings) if b.get("state") == "holding"]
    return h[0] if h else None


def slot_status(settings: Settings | None = None) -> dict[str, Any]:
    """{enabled, slot_gb, peer_gb, holder, exclusive, waiting, peers} for GET /system/status (never raises)."""
    out: dict[str, Any] = {"enabled": enabled(), "slot_gb": slot_gb(), "peer_gb": peer_gb(), "holder": None,
                           "exclusive": None, "waiting": [], "peers": []}
    if not enabled():
        return out
    try:
        keep = ("job_id", "pid", "started_at", "mem_gb", "state")
        bigs = holders("big", settings)
        hold = [b for b in bigs if b.get("state") == "holding"]
        out["holder"] = {k: hold[0].get(k) for k in keep} if hold else None
        ex = holders("exclusive", settings)
        out["exclusive"] = {k: ex[0].get(k) for k in keep} if ex else None
        out["waiting"] = [{k: b.get(k) for k in keep} for b in sorted(bigs, key=lambda b: b.get("seq") or 0)
                          if b.get("state") != "holding"]
        mine = {(b.get("job_id"), b.get("pid")) for b in hold}   # a holder's companion heavy marker
        out["peers"] = [{k: h.get(k) for k in keep} for h in holders("heavy", settings)
                        if _is_peer(h) and (h.get("job_id"), h.get("pid")) not in mine]
    except OSError:  # pragma: no cover
        pass
    return out


def acquire_exclusive(job_id: str, ttl_s: float, settings: Settings | None = None,
                      stop: threading.Event | None = None, wait_max_s: float = EXCLUSIVE_WAIT_MAX_S,
                      poll_s: float = POLL_S) -> Marker | None:
    """Announce an exclusive job, then wait until no heavy job runs (and no other exclusive one, and no big job
    holding the slot). Returns the marker (release() it when done) or None when the slot is disabled."""
    if not enabled():
        return None
    m = _write("exclusive", job_id, ttl_s + wait_max_s, settings)
    deadline = time.monotonic() + wait_max_s
    logged = False
    try:
        while True:
            busy = holders("heavy", settings) + holders("exclusive", settings, exclude=m) + \
                [b for b in holders("big", settings) if b.get("state") == "holding"]
            if not busy:
                return m
            if time.monotonic() > deadline or (stop is not None and stop.is_set()):
                log.warning("%s: exclusive slot - still busy after %.0f min (%s); starting anyway", job_id,
                            wait_max_s / 60, _names(busy))
                return m
            why = f"waiting for the exclusive heavy slot (running: {_names(busy)})"
            if not logged:
                log.info("%s: %s", job_id, why)
                logged = True
            _note(job_id, why, settings)
            if stop is not None:
                stop.wait(poll_s)
            else:
                time.sleep(poll_s)
    finally:
        _note(job_id, None, settings)


def acquire_heavy(job_id: str, ttl_s: float, settings: Settings | None = None, stop: threading.Event | None = None,
                  wait_max_s: float = HEAVY_WAIT_MAX_S, poll_s: float = POLL_S,
                  defer: bool = False, mem_gb: float | None = None,
                  give_up_at: float | None = None) -> tuple[Marker | None, str | None]:
    """Announce a heavy job; while an exclusive job holds the slot - or, for a budget >= PEER_GB (or unknown), while
    a big job waits for / holds the heavy slot - withdraw and wait. Returns (marker, None), or (None, reason) when
    `defer` is set and the slot is held, or when the wall-clock `give_up_at` (epoch s) passes while waiting (the
    caller skips the job)."""
    if not enabled():
        return None, None
    peer = mem_gb is None or float(mem_gb) >= peer_gb()
    deadline = time.monotonic() + wait_max_s
    logged = False
    try:
        while True:
            ex = holders("exclusive", settings)
            bigs = holders("big", settings) if peer else []
            if not ex and not bigs:   # looks free: announce, then check again (a racing exclusive / big sees us)
                m = _write("heavy", job_id, ttl_s, settings, mem_gb=mem_gb)
                ex = holders("exclusive", settings)
                bigs = holders("big", settings) if peer else []
                if not ex and not bigs:
                    return m, None
                m.release()
            hold = [b for b in bigs if b.get("state") == "holding"]
            if ex:
                why = f"exclusive heavy slot held by {ex[0].get('job_id')}"
            elif hold:
                why = f"heavy slot held by {hold[0].get('job_id')} ({float(hold[0].get('mem_gb') or 0):g} GB)"
            else:
                why = f"heavy slot claimed by {_names(bigs)} (waiting for running heavy jobs to finish)"
            if defer:
                return None, why
            if give_up_at is not None and time.time() > give_up_at:
                return None, f"start budget ran out while waiting ({why})"
            past = time.monotonic() > deadline or (stop is not None and stop.is_set())
            if past and not hold:   # a big job holding the slot is never joined (strict); the others time out
                log.warning("%s: %s after %.0f min; starting anyway", job_id, why, wait_max_s / 60)
                return _write("heavy", job_id, ttl_s, settings, mem_gb=mem_gb), None
            if not logged:
                log.info("%s: waiting - %s", job_id, why)
                logged = True
            _note(job_id, why, settings)
            if stop is not None:
                stop.wait(poll_s)
            else:
                time.sleep(poll_s)
    finally:
        _note(job_id, None, settings)


def acquire_big(job_id: str, mem_gb: float, ttl_s: float, settings: Settings | None = None,
                stop: threading.Event | None = None, wait_max_s: float = BIG_WAIT_MAX_S,
                poll_s: float = POLL_S, give_up_at: float | None = None) -> tuple[Marker | None, str | None]:
    """Coverage v3 A2: take the heavy slot exclusively for a big job (budget >= SLOT_GB). Returns (marker, None) once
    the slot is held (release() it when done), (None, None) when the slot is disabled, (None, reason) when the
    runner stops - or the wall-clock `give_up_at` (epoch s) passes - while waiting."""
    if not enabled():
        return None, None
    m = _write("big", job_id, ttl_s + wait_max_s, settings, mem_gb=mem_gb, state="waiting")
    mine = (m.record["seq"], m.path.name)
    deadline = time.monotonic() + wait_max_s
    last_why = None
    try:
        while True:
            if stop is not None and stop.is_set():
                m.release()
                return None, "the runner is stopping"
            ex = holders("exclusive", settings)
            bigs = holders("big", settings, exclude=m)
            hold = [b for b in bigs if b.get("state") == "holding"]
            older = [b for b in bigs if b.get("state") != "holding" and (b.get("seq") or 0, b["_file"]) < mine]
            peers = [h for h in holders("heavy", settings) if _is_peer(h)]
            past = time.monotonic() > deadline
            why = None
            if ex:
                why = f"exclusive heavy slot held by {ex[0].get('job_id')}"
            elif hold:
                why = f"heavy slot held by {hold[0].get('job_id')} ({float(hold[0].get('mem_gb') or 0):g} GB)"
            elif (older or peers) and not past:
                why = ("heavy slot: queued behind " + _names(older)) if older else \
                    f"heavy slot: waiting for running jobs >= {peer_gb():g} GB to finish ({_names(peers)})"
            if why is None:
                if older or peers:
                    log.warning("%s: heavy slot - still busy after %.0f min (%s); taking it", job_id, wait_max_s / 60,
                                _names(older + peers))
                m.set_state("holding")
                rival = [b for b in holders("big", settings, exclude=m) if b.get("state") == "holding"
                         and (b.get("seq") or 0, b["_file"]) < mine]
                if not rival:
                    log.info("%s: holds the heavy slot (%.1f GB)", job_id, mem_gb)
                    m.extra.append(_write("heavy", job_id, ttl_s, settings, mem_gb=mem_gb).path)
                    return m, None
                m.set_state("waiting")   # an older big switched at the same instant: it goes first
                why = f"heavy slot held by {rival[0].get('job_id')}"
            if give_up_at is not None and time.time() > give_up_at:
                m.release()
                return None, f"start budget ran out while waiting ({why})"
            if why != last_why:
                log.info("%s: waiting - %s", job_id, why)
                last_why = why
            _note(job_id, why, settings)
            if stop is not None:
                stop.wait(poll_s)
            else:
                time.sleep(poll_s)
    finally:
        _note(job_id, None, settings)


def resource_class(job_id: str, executor: str, declared: str | None = None, mem_gb: float | None = None) -> str:
    """light | heavy | big | exclusive (declared jobs.yaml `resource` wins)."""
    if declared in CLASSES:
        return declared
    if job_id in EXCLUSIVE_JOBS:
        return "exclusive"
    if mem_gb is not None and float(mem_gb) >= slot_gb():
        return "big"
    if executor == "process":
        return "heavy"
    if mem_gb is not None and float(mem_gb) >= peer_gb():
        return "heavy"
    return "light"
