"""Job contract shared by every job module listed in contracts/jobs.yaml.

Each job module exposes:   def run(ctx: JobContext) -> JobResult
The scheduler (owned by the platform agent) builds the JobContext, records job_runs rows before/after, handles retries,
timeouts and DAG ordering. Jobs must be idempotent: re-running for the same as_of_date yields the same tables.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import lru_cache
from logging import Logger
from typing import Any

import yaml

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32


def new_run_id(prefix: str = "R") -> str:
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}{ts}{''.join(secrets.choice(_B32) for _ in range(4))}"


@dataclass
class JobContext:
    job_id: str
    run_id: str
    as_of: date                                  # data date being processed (previous session for nightly)
    params: dict[str, Any] = field(default_factory=dict)
    watermark: str | None = None                 # last successful watermark for this job (from job_runs)
    dag_run_id: str | None = None
    trigger: str = "manual"
    settings: Settings = field(default_factory=get_settings)
    log: Logger = field(default_factory=lambda: get_logger("app.job"))
    #: set by the runner on timeout / shutdown; long loops should call ctx.check_cancelled() between entities
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def check_cancelled(self) -> None:
        """Raise JobCancelled if the runner asked this job to stop (timeout or worker shutdown)."""
        if self.cancel_event.is_set():
            raise JobCancelled(f"{self.job_id} cancelled")


class JobCancelled(RuntimeError):
    """Raised inside a job when the runner cancelled it (see JobContext.check_cancelled)."""


@dataclass
class JobResult:
    status: str = "success"                      # success | partial | failed | skipped
    rows_written: dict[str, int] = field(default_factory=dict)
    entities_ok: int = 0
    entities_failed: int = 0
    watermark: str | None = None                 # committed only when status in (success, partial)
    http_requests: int = 0
    cache_hits: int = 0
    message: str | None = None
    retryable: bool = True                       # False: the runner must not retry a 'failed' result (e.g. dq errors)

    @property
    def total_rows(self) -> int:
        return sum(self.rows_written.values())


@lru_cache(maxsize=1)
def load_job_registry() -> dict[str, Any]:
    path = get_settings().contracts_dir / "jobs.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["jobs_by_id"] = {j["id"]: j for j in doc["jobs"]}
    return doc


def topo_order(dag: str) -> list[str]:
    """Job ids of a DAG in dependency order (dependencies on other DAGs are ignored)."""
    reg = load_job_registry()
    jobs = {j["id"]: j for j in reg["jobs"] if j["dag"] == dag}
    order: list[str] = []
    state: dict[str, int] = {}

    def visit(j: str) -> None:
        if state.get(j) == 2:
            return
        if state.get(j) == 1:
            raise ValueError(f"cycle at {j}")
        state[j] = 1
        for d in jobs[j].get("depends_on", []):
            if d in jobs:
                visit(d)
        state[j] = 2
        order.append(j)

    for j in jobs:
        visit(j)
    return order
