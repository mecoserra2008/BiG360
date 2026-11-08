"""Unattended-running hygiene (O1.7): log rotation, disk-space guard, serving snapshot clean-up.

    run_housekeeping(settings) -> {"rotated": [...], "job_logs_deleted": n, "snapshots_removed": [...],
                                   "disk_free_gb": x, "warnings": [...]}

* Logs: any *.log under data/logs (not data/logs/jobs) larger than log_max_mb is copy-truncated into
  <name>.<n>.log.gz (newest = 1, keep log_keep). Copy-truncate works while another process appends to the file
  (Windows CRT shares read/write), so the API / worker / backfill logs rotate without restarts. Logs already managed
  by RotatingFileHandler stay below the threshold and are left alone.
* Per-run job logs (data/logs/jobs/<job>/<run>.log) older than job_logs_max_age_days are deleted.
* Serving snapshots: keep the newest snapshots_keep (default 2); the CURRENT one is never deleted and files still
  open by a reader are skipped (retried next time) - app.api.serving.cleanup_snapshots.
* Disk guard: free space of the data drive vs disk_warn_gb / disk_min_gb (the governor pauses the filler below
  governor.min_disk_free_gb; health raises an alert).
"""

from __future__ import annotations

import gzip
import shutil
import time
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.winsys import disk_free_gb

log = get_logger("app.ops.housekeeping")

DEFAULTS: dict[str, Any] = {
    "log_max_mb": 50,
    "log_keep": 3,
    "job_logs_max_age_days": 45,
    "snapshots_keep": 2,
    "disk_warn_gb": 25.0,
    "disk_min_gb": 15.0,
    "every_h": 6,
}


def rotate_log(path: Path, keep: int = 3) -> bool:
    """Copy-truncate `path` to path.1.log.gz (shifting older archives). Returns True when rotated."""
    stem = path.name[:-4] if path.name.endswith(".log") else path.name
    arch = [path.with_name(f"{stem}.{i}.log.gz") for i in range(1, keep + 1)]
    try:
        if arch[-1].exists():
            arch[-1].unlink()
        for i in range(keep - 1, 0, -1):
            if arch[i - 1].exists():
                arch[i - 1].replace(arch[i])
        tmp = arch[0].with_suffix(".gz.tmp")
        with open(path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        with open(path, "r+b") as f:
            f.truncate(0)
        tmp.replace(arch[0])
        return True
    except OSError as e:
        log.info("log rotation skipped for %s: %s", path.name, e)
        return False


def rotate_logs(settings: Settings | None = None, max_mb: float = 50, keep: int = 3) -> list[str]:
    s = settings or get_settings()
    out = []
    root = s.logs_dir
    if not root.exists():
        return out
    jobs = s.job_logs_dir.resolve()
    for p in root.rglob("*.log"):
        try:
            if jobs in p.resolve().parents:
                continue
            if p.stat().st_size > max_mb * 1024**2 and rotate_log(p, keep):
                out.append(str(p.relative_to(root)).replace("\\", "/"))
        except OSError:
            continue
    return out


def prune_job_logs(settings: Settings | None = None, max_age_days: float = 45) -> int:
    s = settings or get_settings()
    root = s.job_logs_dir
    if not root.exists():
        return 0
    cut = time.time() - max_age_days * 86400
    n = 0
    for p in root.rglob("*.log"):
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
                n += 1
        except OSError:
            continue
    return n


def cleanup_snapshots(settings: Settings | None = None, keep: int = 2) -> list[str]:
    from app.api.serving import cleanup_snapshots as _cleanup

    try:
        return _cleanup(keep=max(1, keep), settings=settings)
    except Exception as e:  # pragma: no cover - never fail housekeeping on this
        log.warning("snapshot cleanup failed: %s", e)
        return []


def run_housekeeping(settings: Settings | None = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    c = {**DEFAULTS, **(cfg or {})}
    rep: dict[str, Any] = {"warnings": []}
    rep["rotated"] = rotate_logs(s, float(c["log_max_mb"]), int(c["log_keep"]))
    rep["job_logs_deleted"] = prune_job_logs(s, float(c["job_logs_max_age_days"]))
    rep["snapshots_removed"] = cleanup_snapshots(s, int(c["snapshots_keep"]))
    free = disk_free_gb(s.data_dir)
    rep["disk_free_gb"] = round(free, 1) if free is not None else None
    if free is not None and free < float(c["disk_min_gb"]):
        rep["warnings"].append(f"disk free {free:.1f} GB below minimum {c['disk_min_gb']} GB")
    elif free is not None and free < float(c["disk_warn_gb"]):
        rep["warnings"].append(f"disk free {free:.1f} GB below warning level {c['disk_warn_gb']} GB")
    rep["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    log.info("housekeeping: %s", rep)
    return rep
