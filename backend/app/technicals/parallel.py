"""Parallel map for per-security work (owner: technicals).

mode="auto" (default): a spawn-based process pool with chunked tasks when there are many items, else a thread pool.
Per-security work mixes nogil numba kernels with many small Polars/NumPy calls; measured on 20 threads, threads only
reach ~1.5x (GIL / shared Polars pool), processes scale ~linearly. Callables passed in process mode must be picklable
(module-level functions or functools.partial of them). If the process pool cannot start or breaks, the map falls
back to threads. Worker processes exit by themselves when the parent dies (broken call-queue pipe).
"""

from __future__ import annotations

import os
import pickle
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Any, TypeVar

from app.core.logsetup import get_logger
from app.scheduler.base import JobCancelled

log = get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")

PROCESS_MIN_ITEMS = 48


#: ram_fix 2026-10-31: Polars / BLAS threads of each pool worker. Workers do many small per-security calls; with the
#: default pool (one thread per core) each spawned worker committed ~1.8 GB, so tech_levels / confluence_zones
#: committed 19-20 GB for ~5-7 GB of working set and could not run under a Job Object memory cap.
WORKER_THREADS = 2
#: committed memory budgeted per pool worker when the job runs under a RAM budget (BIG360_JOB_MEM_GB)
WORKER_BUDGET_GB = 0.75
_THREAD_ENV = ("POLARS_MAX_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
               "NUMEXPR_MAX_THREADS")


def default_workers() -> int:
    n = max(1, min(12, (os.cpu_count() or 4) // 2))
    try:  # inside a budgeted job process (app.ops.ram_budget.job_env): leave room for the parent (~half the budget)
        budget = float(os.environ.get("BIG360_JOB_MEM_GB") or 0)
    except ValueError:
        budget = 0.0
    if budget > 0:
        n = max(1, min(n, int(budget * 0.5 / WORKER_BUDGET_GB)))
    return n


def _run_chunk(fn: Callable[[Any], Any], items: list[Any]) -> list[Any]:
    return [fn(x) for x in items]


def _drive(ex, fn, chunks: list[list[Any]], cancel, max_inflight: int) -> list[list[Any]]:
    results: list[Any] = [None] * len(chunks)
    pending: dict[Future, int] = {}
    it = iter(enumerate(chunks))
    exhausted = False
    while True:
        while not exhausted and len(pending) < max_inflight:
            try:
                i, ch = next(it)
            except StopIteration:
                exhausted = True
                break
            pending[ex.submit(_run_chunk, fn, ch)] = i
        if not pending:
            break
        done, _ = wait(list(pending), return_when=FIRST_COMPLETED)
        for f in done:
            results[pending.pop(f)] = f.result()
        if cancel:
            try:
                cancel()
            except BaseException:
                for f in pending:
                    f.cancel()
                raise
    return results


def pmap(fn: Callable[[T], R], items: Sequence[T] | Iterable[T], workers: int | None = None, mode: str = "auto",
         cancel: Callable[[], Any] | None = None, chunk: int | None = None) -> list[R]:
    """Ordered map of fn over items. `cancel` (e.g. ctx.check_cancelled) is called between completions and may raise."""
    items = list(items)
    workers = int(workers or default_workers())
    if workers <= 1 or len(items) <= 1:
        out = []
        for x in items:
            if cancel:
                cancel()
            out.append(fn(x))
        return out
    if mode == "auto":
        mode = "process" if len(items) >= PROCESS_MIN_ITEMS else "thread"
    if mode == "process":
        chunk = chunk or max(1, min(16, len(items) // (workers * 4) or 1))
        chunks = [items[i:i + chunk] for i in range(0, len(items), chunk)]
        try:
            import multiprocessing as mp

            old_env = {k: os.environ.get(k) for k in _THREAD_ENV}
            os.environ.update({k: str(WORKER_THREADS) for k in _THREAD_ENV})   # inherited by the spawned workers
            try:
                with ProcessPoolExecutor(max_workers=min(workers, len(chunks)), mp_context=mp.get_context("spawn")) as ex:
                    res = _drive(ex, fn, chunks, cancel, max_inflight=workers * 3)
            finally:
                for k, v in old_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            return [y for part in res for y in part]
        except JobCancelled:
            raise
        except (BrokenProcessPool, OSError, AttributeError, TypeError, pickle.PicklingError) as e:
            log.warning("process pool unavailable (%s: %s); falling back to threads", type(e).__name__, e)
    chunks = [[x] for x in items]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = _drive(ex, fn, chunks, cancel, max_inflight=workers * 4)
    return [y for part in res for y in part]
