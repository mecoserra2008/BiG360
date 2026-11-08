"""Job graph helpers of the startup sync: lanes, tiers, derive candidates and input fingerprints.

Input fingerprint of a derive job = the newest Parquet file (mtime) of its input tables, i.e. the `writes` of its
upstream jobs in contracts/jobs.yaml (cross-DAG upstreams included, its own outputs excluded). The job is due when
that is newer than the start of its last successful run: something it reads changed after it last looked.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.ops.sync import policy as P


def is_fetch(spec: Any) -> bool:
    return spec.module.startswith("app.ingestion.") or spec.id in P.FETCH_EXTRA


def _heavy_process(spec: Any) -> bool:
    """A process job is heavy (one at a time) unless jobs.yaml gives it a budget below P.HEAVY_MEM_GB (ram_fix
    2026-10-31: medium jobs moved to the process executor for isolation, not because they are big; the runner's RAM
    budget admits them)."""
    if spec.executor != "process":
        return False
    mem = getattr(spec, "mem_gb", None)
    return mem is None or float(mem) >= P.HEAVY_MEM_GB


def lane(spec: Any) -> str:
    """network | heavy | light."""
    if is_fetch(spec) and spec.id not in P.HEAVY_JOBS and (not _heavy_process(spec) or spec.id in P.NETWORK_PROCESS_OK):
        return "network"
    if _heavy_process(spec) or spec.id in P.HEAVY_JOBS:
        return "heavy"
    return "light"


def tier(spec: Any) -> str:
    if is_fetch(spec):
        return "extended" if spec.id in P.EXTENDED_FETCH else "core"
    return "core" if spec.id in P.CORE_DERIVED else "extended"


def sync_set(registry: Any, extra: Iterable[str] = ()) -> set[str]:
    """Jobs the sync may run: nightly + cftc_weekly jobs with outputs (minus EXCLUDE), plus `extra` (weekly jobs a
    dataset names, e.g. finra_short_interest)."""
    out = {j.id for j in registry.jobs.values() if j.dag in P.SYNC_DAGS and j.id not in P.EXCLUDE and j.writes}
    return out | {j for j in extra if j in registry.jobs}


def downstream(registry: Any, roots: Iterable[str], allowed: set[str]) -> set[str]:
    """Transitive dependents of `roots` inside `allowed` (across DAGs)."""
    roots = set(roots)
    out: set[str] = set()
    frontier = set(roots)
    while frontier:
        nxt: set[str] = set()
        for j in registry.jobs.values():
            if j.id in out or j.id in roots or j.id not in allowed:
                continue
            if any(d in frontier for d in j.depends_on):
                nxt.add(j.id)
        out |= nxt
        frontier = nxt
    return out


def ancestors(registry: Any, job: str, nodes: set[str]) -> set[str]:
    return set(registry.upstream(job)) & nodes


def inputs(registry: Any, job: str) -> set[str]:
    spec = registry.jobs[job]
    out: set[str] = set()
    for d in spec.depends_on:
        if d in registry.jobs:
            out |= set(registry.jobs[d].writes)
    return out - set(spec.writes)


class TableClock:
    """Newest Parquet mtime per lake table (cached; invalidate() after a job wrote)."""

    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self._cache: dict[str, float | None] = {}

    def _dir(self, table: str) -> Path | None:
        try:
            from app.core.schema import get_contract
            from app.core.storage import table_dir

            c = get_contract(table)
            if not c.is_parquet:
                return None
            return table_dir(c, self.s)
        except Exception:
            return None

    def mtime(self, table: str) -> float | None:
        if table in self._cache:
            return self._cache[table]
        d = self._dir(table)
        best: float | None = None
        if d is not None and d.exists():
            stack = [str(d)]
            while stack:
                cur = stack.pop()
                try:
                    with os.scandir(cur) as it:
                        for e in it:
                            if e.is_dir(follow_symlinks=False):
                                if not e.name.startswith((".tmp-", ".old-")):
                                    stack.append(e.path)
                            elif e.name.endswith(".parquet"):
                                t = e.stat().st_mtime
                                if best is None or t > best:
                                    best = t
                except OSError:
                    continue
        self._cache[table] = best
        return best

    def newest(self, tables: Iterable[str]) -> tuple[float | None, str | None]:
        best, which = None, None
        for t in tables:
            m = self.mtime(t)
            if m is not None and (best is None or m > best):
                best, which = m, t
        return best, which

    def invalidate(self, tables: Iterable[str] | None = None) -> None:
        if tables is None:
            self._cache.clear()
        for t in tables or []:
            self._cache.pop(t, None)


def inputs_changed(registry: Any, job: str, last_ok_start: datetime | None, clock: TableClock) -> tuple[bool, str]:
    """(due, why) by the input-fingerprint rule."""
    ins = inputs(registry, job)
    if not ins:
        return False, "no input tables"
    m, which = clock.newest(ins)
    if m is None:
        return False, "inputs empty"
    if last_ok_start is None:
        return True, "never succeeded"
    t = datetime.fromtimestamp(m, UTC)
    if t > last_ok_start:
        return True, f"{which} changed {t:%Y-%m-%d %H:%M} UTC after the last run ({last_ok_start:%Y-%m-%d %H:%M})"
    return False, f"inputs unchanged since {last_ok_start:%Y-%m-%d %H:%M} UTC"
