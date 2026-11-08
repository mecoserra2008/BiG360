"""Fake job callables for runner / worker tests (referenced as tests.platform.fake_jobs:<fn>)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.scheduler.base import JobContext, JobResult

COUNTERS: dict[str, int] = {}


def ok(ctx: JobContext) -> JobResult:
    ctx.log.info("hello from %s", ctx.job_id)
    return JobResult(rows_written={"fx_rates": 3}, entities_ok=3, watermark=ctx.as_of.isoformat())


def fail(ctx: JobContext) -> JobResult:
    raise RuntimeError("boom api_key=SECRET123")


def flaky(ctx: JobContext) -> JobResult:
    key = ctx.params.get("key", ctx.job_id)
    COUNTERS[key] = COUNTERS.get(key, 0) + 1
    if COUNTERS[key] <= int(ctx.params.get("fail_times", 1)):
        raise ConnectionError(f"transient failure #{COUNTERS[key]}")
    return JobResult(rows_written={"x": 1})


def sleepy(ctx: JobContext) -> JobResult:
    t0 = time.time()
    end = time.monotonic() + float(ctx.params.get("sleep", 0.3))
    while time.monotonic() < end:
        ctx.check_cancelled()
        time.sleep(0.01)
    rec = ctx.params.get("record")
    if rec:
        with open(rec, "a", encoding="utf-8") as f:
            f.write(f"{ctx.job_id} {t0} {time.time()}\n")
    return JobResult()


def hang(ctx: JobContext) -> JobResult:
    while True:
        ctx.check_cancelled()
        time.sleep(0.02)


def stubborn(ctx: JobContext) -> JobResult:
    """Ignores the cancel token and swallows ordinary exceptions (only JobKilled - a BaseException - stops it)."""
    while True:
        try:
            time.sleep(0.02)
            ctx.check_cancelled()
        except Exception:
            pass


def blocked(ctx: JobContext) -> JobResult:
    """One long C call (time.sleep): an injected exception only lands when it returns."""
    time.sleep(float(ctx.params.get("sleep", 5)))
    return JobResult()


def pid(ctx: JobContext) -> JobResult:
    return JobResult(message=str(os.getpid()), rows_written={"pid": 1})


def no_retry(ctx: JobContext) -> JobResult:
    COUNTERS["no_retry"] = COUNTERS.get("no_retry", 0) + 1
    return JobResult(status="failed", retryable=False, message="dq errors")


def partial(ctx: JobContext) -> JobResult:
    return JobResult(status="partial", entities_ok=2, entities_failed=1)


def fail_if_flag(ctx: JobContext) -> JobResult:
    if Path(ctx.params["flag"]).exists():
        raise RuntimeError("flag present")
    return JobResult(rows_written={"y": 2})


def record(ctx: JobContext) -> JobResult:
    with open(ctx.params["record"], "a", encoding="utf-8") as f:
        f.write(f"{ctx.job_id}\n")
    return JobResult()


# ---- incident 2026-10-24: leaked descendants of process jobs


def leak_grandchild(ctx: JobContext) -> JobResult:
    """Leaves an orphaned spawn grandchild behind (its parent exits) and returns success."""
    import multiprocessing as mp

    from tests.ops.proc_helpers import spawn_grandchild_and_exit

    pidfile = ctx.params["pidfile"]
    c = mp.get_context("spawn").Process(target=spawn_grandchild_and_exit, args=(pidfile,))
    c.start()
    c.join(120)
    return JobResult(message=Path(pidfile).read_text(encoding="utf-8"))


def pool_hang(ctx: JobContext) -> JobResult:
    """Starts a process pool, records the worker pids, then hangs (ignores the cancel token)."""
    from concurrent.futures import ProcessPoolExecutor

    ex = ProcessPoolExecutor(2)
    pids = sorted({f.result() for f in [ex.submit(os.getpid) for _ in range(6)]})
    tmp = Path(ctx.params["pidfile"] + ".tmp")
    tmp.write_text(" ".join(str(p) for p in [os.getpid(), *pids]), encoding="utf-8")
    os.replace(tmp, ctx.params["pidfile"])
    while True:
        time.sleep(0.05)


# ---- ram_fix 2026-10-31: jobs that allocate memory


def alloc(ctx: JobContext) -> JobResult:
    """Allocates params.alloc_mb (touching every page, so it is resident and committed) in 16 MB blocks, holds it
    for params.hold_s, records "<job> <start> <end> <pid>" to params.record, reports the env caps it saw."""
    t0 = time.time()
    mb = int(ctx.params.get("alloc_mb", 64))
    blocks = []
    for _ in range(max(1, mb // 16)):
        b = bytearray(16 * 1024 * 1024)
        for i in range(0, len(b), 4096):
            b[i] = 1
        blocks.append(b)
    end = time.monotonic() + float(ctx.params.get("hold_s", 0.0))
    while time.monotonic() < end:
        ctx.check_cancelled()
        time.sleep(0.02)
    rec = ctx.params.get("record")
    if rec:
        with open(rec, "a", encoding="utf-8") as f:
            f.write(f"{ctx.job_id} {t0} {time.time()} {os.getpid()}\n")
    env = {k: os.environ.get(k) for k in ("POLARS_MAX_THREADS", "BIG360_DUCKDB_MEMORY", "BIG360_JOB_MEM_GB")}
    return JobResult(rows_written={"mb": mb}, message=" ".join(f"{k}={v}" for k, v in env.items()))



def env_threads(_x: object) -> str | None:
    """pmap worker probe (ram_fix 2026-10-31): the Polars pool size a spawned pool worker sees."""
    return os.environ.get("POLARS_MAX_THREADS")


def env_probe(ctx: JobContext) -> JobResult:
    """Coverage v3 A2: reports params.vars (environment of the job process: spill / DuckDB / Polars settings)."""
    names = ctx.params.get("vars") or ["BIG360_DUCKDB_MEMORY", "BIG360_JOB_FIT_GB", "POLARS_ENGINE_AFFINITY",
                                       "BIG360_SPILL"]
    return JobResult(message=" ".join(f"{k}={os.environ.get(k)}" for k in names))
