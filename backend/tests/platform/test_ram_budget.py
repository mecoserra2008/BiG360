"""ram_fix 2026-10-31: per-job RAM budgets. Admission against free RAM minus every runner's outstanding reservations
minus headroom, the Job Object committed-memory cap (OOM -> clear reason, one retry with smaller chunks), the
DuckDB / Polars caps passed to a job process, per-job memory records, memory release after thread jobs."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date

import polars as pl
import pytest

from app.ops import ram_budget as RB
from app.ops import winsys
from app.scheduler.runner import JobRunner, JobRunStore, RunOptions
from tests.platform import fake_jobs
from tests.platform.conftest import FAKE, make_registry

AS_OF = date(2026, 10, 30)
win_only = pytest.mark.skipif(not winsys.IS_WINDOWS, reason="Job Objects are Windows-only")


@pytest.fixture(autouse=True)
def _reset():
    fake_jobs.COUNTERS.clear()
    yield


@pytest.fixture()
def budget_on(monkeypatch):
    """Admission on, with a fake machine: 10 GB free, 4 GB headroom -> 6 GB for jobs."""
    monkeypatch.setenv("BIG360_RAM_BUDGET", "1")
    monkeypatch.setenv("BIG360_RAM_HEADROOM_GB", "4")
    monkeypatch.setattr(RB, "free_gb", lambda: 10.0)
    yield


def _intervals(path) -> dict[str, tuple[float, float]]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        j, a, b, *_ = line.split()
        out[j] = (float(a), float(b))
    return out


def _max_overlap(iv: dict[str, tuple[float, float]]) -> int:
    ev = sorted([(a, 1) for a, _ in iv.values()] + [(b, -1) for _, b in iv.values()])
    cur = best = 0
    for _, d in ev:
        cur += d
        best = max(best, cur)
    return best


def _mem_records(settings) -> list[dict]:
    p = settings.state_dir / "ops" / "job_mem.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


# ------------------------------------------------------------------------------------------------ pure rules


def test_fits_rule_and_sizes():
    assert RB.fits(2.0, free_gb=10.0, outstanding=3.0, headroom=4.0)          # 10 - 3 - 4 = 3 >= 2
    assert not RB.fits(4.0, free_gb=10.0, outstanding=3.0, headroom=4.0)
    assert RB.fits(64.0, free_gb=None, outstanding=0.0, headroom=4.0)         # unknown free RAM never blocks
    assert RB.parse_size_gb("12GB") == pytest.approx(12e9 / 1024**3)
    assert RB.parse_size_gb("7GiB") == pytest.approx(7.0)
    assert RB.parse_size_gb("512MiB") == pytest.approx(0.5)
    assert RB.threads_for(0.5, cpu=20) == 2 and RB.threads_for(4, cpu=20) == 6 and RB.threads_for(20, cpu=20) == 12
    env = RB.job_env(4.0)
    assert env["BIG360_DUCKDB_MEMORY"] == f"{int(4.0 * 0.6 * 1024)}MiB" and env["POLARS_MAX_THREADS"] == "6"
    assert env["BIG360_JOB_MEM_GB"] == "4"


def test_reservations_outstanding_and_stale(tmp_data):
    r = RB.reserve("job_a", 3.0, "process", settings=tmp_data)
    recs = RB.live(tmp_data)
    assert [x["job_id"] for x in recs] == ["job_a"]
    # a reservation counts its budget minus what its job process already uses
    assert RB.outstanding_gb(recs, usage=lambda rec: 1.0) == pytest.approx(2.0)
    assert RB.outstanding_gb(recs, usage=lambda rec: 5.0) == 0.0
    ok, info = RB.check(3.5, tmp_data, free=10.0, recs=recs)
    assert not ok and info["holders"] == ["job_a"]                           # 10 - 3 - 4 = 3 < 3.5
    assert RB.check(3.0, tmp_data, free=10.0, recs=recs)[0]
    # stale markers (dead owner / expired) are removed
    d = RB.ram_dir(tmp_data)
    (d / "dead.json").write_text(json.dumps({"job_id": "x", "budget_gb": 9, "pid": 999999, "expires": time.time() + 99}))
    (d / "old.json").write_text(json.dumps({"job_id": "y", "budget_gb": 9, "pid": 1, "expires": time.time() - 1}))
    assert [x["job_id"] for x in RB.live(tmp_data)] == ["job_a"]
    assert not (d / "dead.json").exists() and not (d / "old.json").exists()
    r.release()
    assert RB.live(tmp_data) == []


def test_reserve_counts_child_tree(tmp_data):
    p = subprocess.Popen([sys.executable, "-c", "b = bytearray(200 * 1024 * 1024); b[::4096] = b'x' * len(b[::4096]); "
                                                "import time; time.sleep(30)"])
    try:
        r = RB.reserve("job_b", 1.0, "process", settings=tmp_data)
        r.set_child(p.pid)
        deadline = time.monotonic() + 15
        used = 0.0
        while time.monotonic() < deadline and used < 0.15:
            used = RB.usage_gb(RB.live(tmp_data)[0])
            time.sleep(0.2)
        assert used >= 0.15                                                   # ~200 MB resident in the child
        assert RB.outstanding_gb(RB.live(tmp_data)) <= 1.0 - 0.15
        r.release()
    finally:
        p.kill()
        p.wait(10)


# ------------------------------------------------------------------------------------------------ runner admission


def test_runner_budget_limits_parallel_jobs(tmp_data, tmp_path, budget_on):
    rec = tmp_path / "iv.txt"
    p = {"alloc_mb": 64, "hold_s": 0.8, "record": str(rec)}
    reg = make_registry([{"id": f"m{i}", "module": f"{FAKE}:alloc", "params": p, "mem_gb": 2.5} for i in range(4)])
    rep = JobRunner(reg, tmp_data).run_jobs(list(reg.jobs), RunOptions(as_of=AS_OF, max_workers=4))
    assert all(o.status == "success" for o in rep.outcomes.values()), rep.summary()
    # 6 GB for jobs / 2.5 GB each -> never more than 2 at once although 4 workers are free
    assert _max_overlap(_intervals(rec)) == 2
    recs = _mem_records(tmp_data)
    assert {r["job_id"] for r in recs} == {"m0", "m1", "m2", "m3"}
    assert all(r["executor"] == "thread" and r["budget_gb"] == 2.5 and r["peak_ws_mb"] > 0 for r in recs)
    assert RB.live(tmp_data) == []                                             # every reservation released


def test_runner_without_budget_runs_all_in_parallel(tmp_data, tmp_path, monkeypatch):
    monkeypatch.setattr(RB, "free_gb", lambda: 10.0)                           # admission off (conftest default)
    rec = tmp_path / "iv.txt"
    p = {"alloc_mb": 16, "hold_s": 0.8, "record": str(rec)}
    reg = make_registry([{"id": f"m{i}", "module": f"{FAKE}:alloc", "params": p, "mem_gb": 2.5} for i in range(4)])
    JobRunner(reg, tmp_data).run_jobs(list(reg.jobs), RunOptions(as_of=AS_OF, max_workers=4))
    assert _max_overlap(_intervals(rec)) == 4


def test_runner_waits_for_another_runners_reservation(tmp_data, tmp_path, budget_on, monkeypatch):
    monkeypatch.setenv("BIG360_RAM_WAIT_MAX_S", "1.5")
    other = RB.reserve("build_serving_db", 5.0, "process", settings=tmp_data)  # e.g. the sync's serving build
    reg = make_registry([{"id": "big", "module": f"{FAKE}:alloc", "params": {"alloc_mb": 16}, "mem_gb": 2.5}])
    t0 = time.monotonic()
    o = JobRunner(reg, tmp_data).run_job("big", RunOptions(as_of=AS_OF))
    waited = time.monotonic() - t0
    other.release()
    assert o.status == "success"
    assert waited >= 1.4                       # 10 - 5 - 4 = 1 GB < 2.5: held back, then started after the max wait
    o2 = JobRunner(reg, tmp_data).run_job("big", RunOptions(as_of=AS_OF, force=True))
    assert o2.status == "success" and o2.duration_s < 1.4                     # nothing reserved: starts at once


def test_light_thread_job_is_not_held_back_when_nothing_runs(tmp_data, budget_on, monkeypatch):
    monkeypatch.setattr(RB, "free_gb", lambda: 3.0)                            # below the headroom
    monkeypatch.setenv("BIG360_RAM_WAIT_MAX_S", "30")
    reg = make_registry([{"id": "light", "module": f"{FAKE}:ok"}])
    t0 = time.monotonic()
    assert JobRunner(reg, tmp_data).run_job("light", RunOptions(as_of=AS_OF)).status == "success"
    assert time.monotonic() - t0 < 10


def test_mem_gb_validation():
    with pytest.raises(ValueError, match="mem_gb"):
        make_registry([{"id": "x", "module": f"{FAKE}:ok", "mem_gb": 0}])
    with pytest.raises(ValueError, match="oom_params"):
        make_registry([{"id": "x", "module": f"{FAKE}:ok", "oom_params": {"chunk": 1}}])
    reg = make_registry([{"id": "x", "module": f"{FAKE}:ok", "executor": "process", "mem_gb": 3,
                          "oom_params": {"chunk": 1}}])
    assert reg.jobs["x"].mem_gb == 3.0 and reg.jobs["x"].oom_params == {"chunk": 1}


# ------------------------------------------------------------------------------------------------ Job Object cap


@win_only
def test_job_object_memory_limit_fails_allocation():
    job = winsys.JobObject(name="test-cap")
    assert job.ok and job.set_memory_limit(300 * 1024**2)
    code = ("import sys\ntry:\n    b = bytearray(600 * 1024 * 1024)\nexcept MemoryError:\n    sys.exit(42)\nsys.exit(0)")
    p = winsys.start_in_job([sys.executable, "-c", code], job)
    assert p.wait(60) == 42                                                    # the 600 MB allocation was refused
    li = job.limit_info()
    # the refused request still shows in the job's peak (it is what tripped the cap): >= the cap, never below it
    assert li["limit_mb"] == pytest.approx(300.0) and li["peak_job_mb"] >= 300 * 0.98
    # KILL_ON_JOB_CLOSE survives setting the cap
    p2 = winsys.start_in_job([sys.executable, "-c", "import time; time.sleep(60)"], job)
    job.close()
    assert p2.wait(20) is not None


@win_only
def test_process_job_oom_is_reported_and_retried_once_with_smaller_chunks(tmp_data):
    reg = make_registry([{"id": "chunky", "module": f"{FAKE}:alloc", "executor": "process", "timeout_min": 3,
                          "retries": 0, "mem_gb": 0.75, "params": {"alloc_mb": 1600},
                          "oom_params": {"alloc_mb": 32}}])
    o = JobRunner(reg, tmp_data).run_job("chunky", RunOptions(as_of=AS_OF))
    assert o.status == "success" and o.attempts == 2, o.message
    runs = JobRunStore(tmp_data).read().filter(pl.col("job_id") == "chunky").sort("attempt")
    first = runs.row(0, named=True)
    assert first["status"] == "failed" and first["error_type"] == "MemoryLimitExceeded"
    assert "OOM-killed at" in first["error_message"] and "budget 0.75 GB" in first["error_message"]
    # the retry ran with the smaller chunk parameter and saw the caps derived from its 0.75 GB budget
    assert "POLARS_MAX_THREADS=2" in o.message and f"BIG360_DUCKDB_MEMORY={int(max(0.5, 0.75 * 0.6) * 1024)}MiB" in o.message
    recs = [r for r in _mem_records(tmp_data) if r["job_id"] == "chunky"]
    assert recs[0]["oom_killed"] and recs[0]["limit_mb"] == 768 and recs[0]["peak_commit_mb"] >= 0.8 * 768
    assert recs[1]["status"] == "success" and recs[1]["peak_commit_mb"] < 768


@win_only
def test_process_job_oom_without_chunk_param_fails_once_not_retried(tmp_data):
    reg = make_registry([{"id": "fat", "module": f"{FAKE}:alloc", "executor": "process", "timeout_min": 3,
                          "retries": 2, "mem_gb": 0.75, "params": {"alloc_mb": 1600}}])
    o = JobRunner(reg, tmp_data).run_job("fat", RunOptions(as_of=AS_OF))
    assert o.status == "failed" and o.attempts == 1 and o.error_type == "MemoryLimitExceeded"
    assert o.message.startswith("OOM-killed at")


@win_only
def test_process_job_records_peak_and_runs_under_budget(tmp_data, budget_on):
    reg = make_registry([{"id": "fits", "module": f"{FAKE}:alloc", "executor": "process", "timeout_min": 3,
                          "mem_gb": 1.5, "params": {"alloc_mb": 256, "hold_s": 1.5}}])
    o = JobRunner(reg, tmp_data).run_job("fits", RunOptions(as_of=AS_OF))
    assert o.status == "success", o.message
    r = [x for x in _mem_records(tmp_data) if x["job_id"] == "fits"][-1]
    assert r["executor"] == "process" and r["limit_mb"] == 1536
    assert 256 <= r["peak_commit_mb"] < 1536 and r["peak_ws_mb"] >= 200


def test_thread_job_memory_is_released_after_the_job(tmp_data):
    reg = make_registry([{"id": "t", "module": f"{FAKE}:alloc", "params": {"alloc_mb": 320, "hold_s": 1.2}}])
    assert JobRunner(reg, tmp_data).run_job("t", RunOptions(as_of=AS_OF)).status == "success"
    r = [x for x in _mem_records(tmp_data) if x["job_id"] == "t"][-1]
    assert r["executor"] == "thread" and r["delta_peak_ws_mb"] >= 250
    # the job's 320 MB are handed back: the process ends near where it started, not at its peak
    assert r["after_release_private_mb"] is not None
    assert r["after_release_private_mb"] - r["start_private_mb"] < 120


def test_sync_lane_uses_budget():
    from app.ops.sync import graph as G

    reg = make_registry([
        {"id": "medium", "module": "app.analytics.x", "executor": "process", "mem_gb": 1.5},
        {"id": "big", "module": "app.analytics.y", "executor": "process", "mem_gb": 6},
        {"id": "legacy", "module": "app.analytics.z", "executor": "process"},
        {"id": "fetch", "module": "app.ingestion.w", "executor": "process", "mem_gb": 1},
    ])
    assert G.lane(reg.jobs["medium"]) == "light" and G.lane(reg.jobs["big"]) == "heavy"
    assert G.lane(reg.jobs["legacy"]) == "heavy" and G.lane(reg.jobs["fetch"]) == "network"


def test_pmap_workers_get_small_thread_pools_and_budgeted_count(monkeypatch):
    from app.technicals import parallel as PAR

    monkeypatch.setenv("POLARS_MAX_THREADS", "9")
    got = PAR.pmap(fake_jobs.env_threads, list(range(8)), workers=2, mode="process")
    assert set(got) == {str(PAR.WORKER_THREADS)}
    import os as _os
    assert _os.environ["POLARS_MAX_THREADS"] == "9"                      # the parent's env is restored
    monkeypatch.setenv("BIG360_JOB_MEM_GB", "3")
    assert PAR.default_workers() == min(max(1, min(12, (_os.cpu_count() or 4) // 2)), 2)   # 3 * 0.5 / 0.75
    monkeypatch.delenv("BIG360_JOB_MEM_GB")
    assert PAR.default_workers() == max(1, min(12, (_os.cpu_count() or 4) // 2))


def test_pool_workers_fit_the_job_budget(monkeypatch):
    monkeypatch.delenv("BIG360_JOB_MEM_GB", raising=False)
    assert RB.pool_workers(10, per_worker_gb=0.5) == 10                    # not in a budgeted job: unchanged
    monkeypatch.setenv("BIG360_JOB_MEM_GB", "4")
    assert RB.pool_workers(10, per_worker_gb=0.5) == 4                     # 4 GB * 0.5 / 0.5 GB
    assert RB.pool_workers(2, per_worker_gb=0.5) == 2
    monkeypatch.setenv("POLARS_MAX_THREADS", "6")
    with RB.small_worker_threads(1):
        import os as _os
        assert _os.environ["POLARS_MAX_THREADS"] == "1" and _os.environ["OMP_NUM_THREADS"] == "1"
    assert _os.environ["POLARS_MAX_THREADS"] == "6"


def test_job_alone_starts_without_waiting_when_nothing_is_reserved(tmp_data, budget_on, monkeypatch):
    """Free RAM below budget + headroom but no job of any runner holds RAM: waiting cannot help (the rest is not
    ours) - the job starts at once under its cap instead of waiting BIG360_RAM_WAIT_MAX_S."""
    monkeypatch.setattr(RB, "free_gb", lambda: 7.0)
    monkeypatch.setenv("BIG360_RAM_WAIT_MAX_S", "60")
    reg = make_registry([{"id": "big", "module": f"{FAKE}:alloc", "params": {"alloc_mb": 16}, "mem_gb": 6}])
    t0 = time.monotonic()
    assert JobRunner(reg, tmp_data).run_job("big", RunOptions(as_of=AS_OF)).status == "success"
    assert time.monotonic() - t0 < 10


def test_two_starving_jobs_do_not_block_each_other(tmp_data, budget_on, monkeypatch):
    """Regression (verification 2026-10-31): two jobs that had both waited past STARVE_S refused each other forever."""
    import threading

    monkeypatch.setattr(RB, "STARVE_S", 0.3)
    monkeypatch.setenv("BIG360_RAM_WAIT_MAX_S", "120")
    other = RB.reserve("serving", 5.0, "process", settings=tmp_data)       # 10 - 5 - 4 = 1 GB left
    threading.Timer(1.5, other.release).start()
    reg = make_registry([{"id": j, "module": f"{FAKE}:alloc", "params": {"alloc_mb": 16}, "mem_gb": 2}
                         for j in ("a", "b", "c")])
    t0 = time.monotonic()
    rep = JobRunner(reg, tmp_data).run_jobs(["a", "b", "c"], RunOptions(as_of=AS_OF, max_workers=3))
    assert all(o.status == "success" for o in rep.outcomes.values())
    assert 1.4 <= time.monotonic() - t0 < 20
