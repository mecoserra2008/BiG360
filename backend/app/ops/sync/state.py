"""Startup sync state (docs/STARTUP_SYNC_PLAN.md A): status file, attempt ledger, history, lock and request files.

Files (data/state/ops/):
    sync_status.json     live status of the running / last sync (GET /system/sync, Data Health "Data Sync" panel,
                         header chip, tray toast). Written atomically every few seconds while a sync runs.
    sync_ledger.json     attempts per (dataset, entity, expected period): reason, attempts, retry_at, last error.
                         It is what turns "due" into not_yet_published / source_unavailable / failed (parked) when
                         a fetch did not bring the period.
    sync_history.jsonl   one summary line per finished sync (append-only, trimmed to 400 lines).
    sync_now.json        request: "sync now" (tray / POST /system/sync / CLI). The supervisor starts the sync.
    sync.STOP            request: stop the running sync (finish running jobs, start nothing new).
    sync.YIELD           written by a refresh DAG waiting for the sync: the sync stops scheduling and exits.
Lock: data/.locks/dag_sync.lock (OS file lock: released when the process dies, so a crashed sync never blocks the
next one; the status file then says `interrupted`). The runner's refresh DAGs wait on it (no overlap).
Coverage v3 A3: sync.owner.json {pid, run_id, trigger, started_at, host} names the holder while it runs; lock_status()
calls the holder stale when the lock is held but its status heartbeat (every 5 s) is older than stale_heartbeat_s
(900 s) or the run is older than hard_max_h (8 h) - a hung sync, not a long one (the budget of app.ops.sync.caps
ends a healthy sync in 2-4 h). The supervisor's trigger then stops it (STOP file, then its Job Object) after
stale_confirm_s of confirmation, so a suspended-and-resumed laptop is not mistaken for a hang.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.ops.paths import ops_state_dir, read_json, write_json_atomic

REASONS = ("fresh", "due", "not_yet_published", "source_unavailable", "no_source", "needs_key", "failed")
#: dataset-level precedence (the most actionable entity reason wins)
REASON_RANK = {"failed": 0, "due": 1, "source_unavailable": 2, "needs_key": 3, "not_yet_published": 4,
               "no_source": 5, "fresh": 6}
SKIPPED_REASONS = ("source_unavailable", "needs_key", "no_source", "failed")
HISTORY_KEEP = 400


def _s(settings: Settings | None) -> Settings:
    return settings or get_settings()


def status_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync_status.json"


def ledger_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync_ledger.json"


def history_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync_history.jsonl"


def request_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync_now.json"


def stop_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync.STOP"


def yield_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync.YIELD"


def owner_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync.owner.json"


def lock_path(settings: Settings | None = None) -> Path:
    s = _s(settings)
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    return s.locks_dir / "dag_sync.lock"


def sync_lock(settings: Settings | None = None) -> FileLock:
    return FileLock(str(lock_path(settings)), timeout=0)


def sync_running(settings: Settings | None = None) -> bool:
    lk = sync_lock(settings)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True
    lk.release()
    return False


def write_owner(run_id: str, trigger: str | None = None, settings: Settings | None = None) -> None:
    """Coverage v3 A3: record who holds the sync lock (written right after the lock is taken)."""
    import socket  # noqa: PLC0415

    try:
        write_json_atomic(owner_path(settings), {"pid": os.getpid(), "run_id": run_id, "trigger": trigger,
                                                 "started_at": now_iso(), "host": socket.gethostname()})
    except OSError:  # pragma: no cover - bookkeeping only
        pass


def clear_owner(run_id: str, settings: Settings | None = None) -> None:
    p = owner_path(settings)
    try:
        if (read_json(p, {}) or {}).get("run_id") == run_id:
            p.unlink(missing_ok=True)
    except OSError:  # pragma: no cover
        pass


def read_owner(settings: Settings | None = None) -> dict[str, Any]:
    return read_json(owner_path(settings), {}) or {}


def lock_status(settings: Settings | None = None, stale_heartbeat_s: float = 900.0, hard_max_h: float = 8.0,
                now: datetime | None = None, held: bool | None = None) -> dict[str, Any]:
    """{held, owner, heartbeat_at, heartbeat_age_s, run_age_s, stale, why} of the sync lock (never raises)."""
    now = now or datetime.now(UTC)
    held = sync_running(settings) if held is None else held
    owner = read_owner(settings)
    st = read_json(status_path(settings), {}) or {}
    # the status file belongs to the holder: same run id, or (no owner record yet) a status that says it runs - a
    # sync that just started must not be judged by the previous run's last heartbeat
    if owner.get("run_id"):
        same = st.get("run_id") == owner["run_id"]
    else:
        same = st.get("state") in ("running", "scanning", "publishing")
    hb = parse_ts(st.get("heartbeat_at")) if same else None
    started = parse_ts(owner.get("started_at")) or (parse_ts(st.get("started_at")) if same else None)
    hb_age = (now - hb).total_seconds() if hb else None
    run_age = (now - started).total_seconds() if started else None
    why = None
    if held:
        if hb_age is not None and hb_age > stale_heartbeat_s:
            why = f"no heartbeat for {hb_age / 60:.0f} min (expected every 5 s)"
        elif run_age is not None and run_age > hard_max_h * 3600:
            why = f"running for {run_age / 3600:.1f} h (hard maximum {hard_max_h:g} h)"
    return {"held": bool(held), "owner": owner or None, "heartbeat_at": st.get("heartbeat_at") if same else None,
            "heartbeat_age_s": round(hb_age) if hb_age is not None else None,
            "run_age_s": round(run_age) if run_age is not None else None, "stale": why is not None, "why": why}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ status


def read_status(settings: Settings | None = None) -> dict[str, Any]:
    """Status doc; a `running` status whose lock is free means the previous sync died -> state `interrupted`."""
    st = read_json(status_path(settings), {}) or {}
    if st.get("state") in ("running", "scanning", "publishing") and not sync_running(settings):
        st = dict(st, state="interrupted", detail="the previous sync process ended without finishing (app closed or "
                                                  "crashed); the next sync resumes from the watermarks")
    return st


def write_status(doc: dict[str, Any], settings: Settings | None = None) -> None:
    write_json_atomic(status_path(settings), doc)


def append_history(summary: dict[str, Any], settings: Settings | None = None) -> None:
    p = history_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(summary, default=str)
    try:
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    except OSError:
        lines = []
    lines = (lines + [line])[-HISTORY_KEEP:]
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def read_history(settings: Settings | None = None, limit: int = 20) -> list[dict[str, Any]]:
    p = history_path(settings)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------------------------------------ requests


def request_sync(settings: Settings | None = None, by: str = "api", datasets: list[str] | None = None) -> dict[str, Any]:
    req = {"requested_at": now_iso(), "by": by, "datasets": list(datasets or []) or None}
    write_json_atomic(request_path(settings), req)
    return req


def take_request(settings: Settings | None = None) -> dict[str, Any] | None:
    p = request_path(settings)
    if not p.exists():
        return None
    req = read_json(p, {}) or {}
    try:
        p.unlink()
    except OSError:
        return None
    return req


def pending_request(settings: Settings | None = None) -> dict[str, Any] | None:
    p = request_path(settings)
    return (read_json(p, {}) or {}) if p.exists() else None


# ------------------------------------------------------------------------------------------------ ledger


class Ledger:
    """Attempt bookkeeping per (dataset, entity, period). Keys 'dataset|entity|period'."""

    def __init__(self, settings: Settings | None = None, doc: dict[str, Any] | None = None):
        self.s = settings
        self.items: dict[str, dict[str, Any]] = dict((doc if doc is not None else
                                                       (read_json(ledger_path(settings), {}) or {})).get("items") or {})

    @staticmethod
    def key(dataset: str, entity: str, period: Any) -> str:
        return f"{dataset}|{entity}|{period or '-'}"

    def get(self, dataset: str, entity: str, period: Any) -> dict[str, Any] | None:
        return self.items.get(self.key(dataset, entity, period))

    def active(self, dataset: str, entity: str, period: Any, now: datetime | None = None) -> dict[str, Any] | None:
        """The ledger entry when it still holds the item back (retry_at in the future), else None."""
        rec = self.get(dataset, entity, period)
        if not rec:
            return None
        t = parse_ts(rec.get("retry_at"))
        now = now or datetime.now(UTC)
        return rec if (t is not None and t > now) else None

    def record(self, dataset: str, entity: str, period: Any, reason: str, retry_after_min: float | None,
               error: str | None = None, job: str | None = None, now: datetime | None = None,
               key: str | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        k = self.key(dataset, entity, period)
        prev = self.items.get(k) or {}
        rec = dict(prev) if prev else {"first_at": now.isoformat(timespec="seconds")}
        rec["attempts"] = int(prev.get("attempts") or 0) + 1 if prev.get("reason") == reason else 1
        rec.update(reason=reason, last_at=now.isoformat(timespec="seconds"), job=job,
                   error=(error or "")[:500] or None, key=key,
                   retry_at=(now + timedelta(minutes=retry_after_min)).isoformat(timespec="seconds")
                   if retry_after_min is not None else None)
        self.items[k] = rec
        return rec

    def clear(self, dataset: str, entity: str | None = None) -> None:
        pre = f"{dataset}|" + (f"{entity}|" if entity is not None else "")
        for k in [k for k in self.items if k.startswith(pre)]:
            self.items.pop(k, None)

    def prune(self, keep_days: int = 30, now: datetime | None = None) -> None:
        cut = (now or datetime.now(UTC)) - timedelta(days=keep_days)
        for k in [k for k, v in self.items.items() if (parse_ts(v.get("last_at")) or cut) < cut]:
            self.items.pop(k, None)

    def save(self) -> None:
        self.prune()
        write_json_atomic(ledger_path(self.s), {"updated_at": now_iso(), "items": self.items})
