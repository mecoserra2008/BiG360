"""File-based job queue between the API / CLI (producers) and the worker process (single consumer).

Layout (data/state/queue/):
    <ts>-<request_id>.json          pending request (written atomically: .tmp file + os.replace)
    processing/<same name>          claimed by the worker (atomic rename; a failed rename means someone else claimed it)
    done/<same name>                finished (request + result), pruned after 30 days
    failed/<same name>              request that crashed the worker twice or could not be parsed

Request JSON:
    {"request_id", "run_id", "kind": "job"|"dag", "job_id" | "dag", "params": {...}, "include_downstream": bool,
     "trigger": "api"|"manual", "requested_at": ISO-UTC, "attempts": 0}

API usage (POST /system/jobs/{job_id}/trigger):
    from app.core.jobqueue import enqueue_job, job_is_running, JobNotFound
    if job_is_running(job_id): -> 409
    req = enqueue_job(job_id, params=body.params, include_downstream=body.include_downstream)   # raises JobNotFound
    return {"run_id": req["run_id"], "job_id": job_id}      # 202; the worker uses this run_id for attempt 1

Only stdlib + filelock: safe to import from the API process (no APScheduler, no job modules).
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .config import Settings, get_settings

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class JobNotFound(KeyError):
    pass


def _new_id(prefix: str) -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}{ts}{''.join(secrets.choice(_B32) for _ in range(4))}"


def queue_dirs(settings: Settings | None = None) -> dict[str, Path]:
    s = settings or get_settings()
    root = s.queue_dir
    d = {"pending": root, "processing": root / "processing", "done": root / "done", "failed": root / "failed"}
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _retry_fs(fn, *args, attempts: int = 10, delay: float = 0.05):
    """Windows: rename/replace fails with PermissionError while another process has the file open (AV scanners too)."""
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (2**i))


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    tmp = path.parent / f".tmp-{path.name}-{secrets.token_hex(4)}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    _retry_fs(os.replace, tmp, path)


def _known_job_ids(settings: Settings) -> tuple[set[str], set[str]]:
    """(job ids, dag names) from contracts/jobs.yaml."""
    import yaml

    doc = yaml.safe_load((settings.contracts_dir / "jobs.yaml").read_text(encoding="utf-8"))
    return {j["id"] for j in doc.get("jobs", [])}, set(doc.get("dags", {}))


def _read_json(path: Path) -> dict[str, Any]:
    """Read a queue file. Windows: retry briefly while another handle (a losing claimer, AV scanner) is open."""
    text = _retry_fs(path.read_text, "utf-8")
    return json.loads(text)


def enqueue(request: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Low-level: write a request (fills request_id/run_id/requested_at) and return it."""
    s = settings or get_settings()
    d = queue_dirs(s)
    req = dict(request)
    req.setdefault("request_id", _new_id("Q"))
    req.setdefault("run_id", _new_id("R"))
    req.setdefault("requested_at", datetime.now(UTC).isoformat())
    req.setdefault("params", {})
    req.setdefault("include_downstream", False)
    req.setdefault("trigger", "api")
    req.setdefault("attempts", 0)
    name = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}-{req['request_id']}.json"
    _atomic_write_json(d["pending"] / name, req)
    return req


def enqueue_job(job_id: str, params: dict[str, Any] | None = None, include_downstream: bool = False,
                trigger: str = "api", settings: Settings | None = None) -> dict[str, Any]:
    """Queue one job (optionally with everything downstream of it in the same DAG). Raises JobNotFound."""
    s = settings or get_settings()
    jobs, _ = _known_job_ids(s)
    if job_id not in jobs:
        raise JobNotFound(job_id)
    return enqueue({"kind": "job", "job_id": job_id, "params": params or {}, "include_downstream": include_downstream,
                    "trigger": trigger}, s)


def enqueue_dag(dag: str, params: dict[str, Any] | None = None, trigger: str = "api",
                settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    _, dags = _known_job_ids(s)
    if dag not in dags:
        raise JobNotFound(dag)
    return enqueue({"kind": "dag", "dag": dag, "params": params or {}, "trigger": trigger}, s)


def list_requests(state: str = "pending", settings: Settings | None = None) -> list[dict[str, Any]]:
    d = queue_dirs(settings)[state]
    out = []
    for p in sorted(d.glob("*.json")):
        if p.name.startswith(".tmp-"):
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")) | {"_file": p.name})
        except (OSError, ValueError):
            continue
    return out


def claim_next(settings: Settings | None = None) -> tuple[Path, dict[str, Any]] | None:
    """Move the oldest pending request to processing/. Returns (processing_path, request) or None.

    Claims are serialized with a file lock: on Windows two concurrent renames of the same file can both "succeed"
    (the loser's open handle renames the already-moved file to its own name), so rename alone is not a safe claim.
    Unparseable files are moved to failed/.
    """
    d = queue_dirs(settings)
    with FileLock(str(d["pending"] / ".claim.lock"), timeout=60):
        for p in sorted(d["pending"].glob("*.json")):
            if p.name.startswith(".tmp-"):
                continue
            target = d["processing"] / p.name
            try:
                _retry_fs(os.rename, p, target)
            except (FileNotFoundError, FileExistsError):
                continue  # claimed by another consumer
            except PermissionError:
                continue  # still locked by a writer / scanner: try next poll
            try:
                req = _read_json(target)
            except ValueError as e:
                _finish_file(target, d["failed"], {"error": f"unparseable request: {e}"}, {"file": p.name})
                continue
            return target, req
    return None


def _finish_file(path: Path, dest_dir: Path, result: dict[str, Any], req: dict[str, Any] | None = None) -> Path:
    if req is None:
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            req = {"raw": path.read_text(encoding="utf-8", errors="replace") if path.exists() else None}
    out = dest_dir / path.name
    _atomic_write_json(out, {**req, "finished_at": datetime.now(UTC).isoformat(), "result": result})
    try:
        _retry_fs(os.remove, path)
    except FileNotFoundError:
        pass
    return out


def complete(path: Path, req: dict[str, Any], result: dict[str, Any], ok: bool = True,
             settings: Settings | None = None) -> Path:
    d = queue_dirs(settings)
    return _finish_file(path, d["done"] if ok else d["failed"], result, req)


def recover_processing(max_attempts: int = 2, settings: Settings | None = None) -> dict[str, int]:
    """At worker start (single instance): requeue requests left in processing/ by a crashed worker.

    A request is retried at most `max_attempts` times, then moved to failed/.
    """
    d = queue_dirs(settings)
    n_requeued = n_failed = 0
    for p in sorted(d["processing"].glob("*.json")):
        try:
            req = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _finish_file(p, d["failed"], {"error": "unparseable after crash"})
            n_failed += 1
            continue
        req["attempts"] = int(req.get("attempts", 0)) + 1
        if req["attempts"] >= max_attempts:
            _finish_file(p, d["failed"], {"error": "worker crashed while processing (gave up)"}, req)
            n_failed += 1
            continue
        _atomic_write_json(d["pending"] / p.name, req)
        _retry_fs(os.remove, p)
        n_requeued += 1
    return {"requeued": n_requeued, "failed": n_failed}


def prune(days: int = 30, settings: Settings | None = None) -> int:
    d = queue_dirs(settings)
    cutoff = time.time() - timedelta(days=days).total_seconds()
    n = 0
    for state in ("done", "failed"):
        for p in d[state].glob("*.json"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    return n


# ------------------------------------------------------------------------------------------------ job locks


def job_lock_path(job_id: str, settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    return s.locks_dir / f"job_{job_id}.lock"


def job_lock(job_id: str, settings: Settings | None = None) -> FileLock:
    """Cross-process lock held by the runner while a job executes (one concurrent run per job id)."""
    return FileLock(str(job_lock_path(job_id, settings)), timeout=0)


def job_is_running(job_id: str, settings: Settings | None = None) -> bool:
    """True if some process currently holds the job's lock (i.e. the job is executing)."""
    lk = job_lock(job_id, settings)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True
    lk.release()
    return False


def job_is_queued(job_id: str, settings: Settings | None = None) -> bool:
    return any(r.get("job_id") == job_id for st in ("pending", "processing") for r in list_requests(st, settings))
