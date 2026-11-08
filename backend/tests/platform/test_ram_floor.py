"""Coverage v3 A2: the 3 GB RAM floor (free - reservations - budget >= floor) is never bypassed by the "alone" / "max
wait" escapes of the ram_fix admission; below it a job waits, then is deferred (RunOptions.ram_defer, the startup
sync) or starts in spill mode (DuckDB sized from what fits above the floor, Polars streaming engine). Heavy jobs
(budget >= 7 GB) always get the spill environment. Wait reasons are written per runner process and merged."""

from __future__ import annotations

import json
import os
import time
from datetime import date

import polars as pl
import pytest

from app.ops import ram_budget as RB
from app.ops import winsys
from app.scheduler.runner import JobRunner, JobRunStore, RunOptions
from tests.platform.conftest import FAKE, make_registry

AS_OF = date(2026, 11, 6)
win_only = pytest.mark.skipif(not winsys.IS_WINDOWS, reason="process jobs are spawned with Windows Job Objects")


def _info(free, out=0.0, running=0, headroom=4.0):
    return {"free_gb": free, "outstanding_gb": out, "running": running, "headroom_gb": headroom, "holders": ["x"]}


# ------------------------------------------------------------------------------------------------ pure rule


def test_floor_rule_table():
    adm = RB.admission
    kw = {"light": False, "wait_max": 60.0, "floor": 3.0, "strict": False}
    # floor met: the pre-v3 rules (start / alone / wait / anyway)
    assert adm(2.0, True, _info(10.0), own_running=False, waited_s=0, **kw).action == "start"
    a = adm(5.0, False, _info(8.5), own_running=False, waited_s=0, **kw)       # 8.5-5=3.5 >= 3, headroom short
    assert a.action == "start" and a.note == "alone"
    assert adm(5.0, False, _info(8.5, 0.0, 1), own_running=True, waited_s=0, **kw).action == "wait"
    a = adm(4.0, False, _info(9.0, 1.0, 1), own_running=False, waited_s=61, **kw)   # 9-1-4=4 >= 3
    assert a.action == "start" and a.note == "anyway"
    # below the floor, other reservations: wait, then defer (sync) or spill
    a = adm(4.0, True, _info(9.0, 3.0, 1), own_running=False, waited_s=10, **kw)    # 9-3-4 = 2 < 3
    assert a.action == "wait" and a.kind == "floor" and "floor 3 GB" in a.reason
    a = adm(4.0, True, _info(9.0, 3.0, 1), own_running=False, waited_s=61, defer=True, **kw)
    assert a.action == "defer" and a.kind == "floor"
    a = adm(4.0, True, _info(9.0, 3.0, 1), own_running=False, waited_s=61, **kw)
    assert a.action == "start" and a.note == "spill" and a.fit_gb == pytest.approx(3.0)   # 9 - 3 - 3
    assert adm(4.0, True, _info(9.0, 3.0, 1), own_running=True, waited_s=10, **kw).action == "wait"
    assert adm(4.0, True, _info(9.0, 3.0, 1), own_running=True, waited_s=999, defer=True, **kw).action == "wait"
    # below the floor, nothing of ours runs: waiting cannot help -> spill right away (strict: defer)
    a = adm(9.0, False, _info(6.6), own_running=False, waited_s=0, **kw)
    assert a.action == "start" and a.note == "spill_alone" and a.fit_gb == pytest.approx(3.6)
    assert adm(9.0, False, _info(6.6), own_running=False, waited_s=0, **{**kw, "strict": True}).action == "defer"
    assert adm(9.0, False, _info(2.0), own_running=False, waited_s=0, **kw).fit_gb == RB.MIN_FIT_GB
    # exemptions: light thread jobs, floor off, unknown free RAM; anti-starvation first
    assert adm(0.5, False, _info(2.0), own_running=False, waited_s=0, **{**kw, "light": True}).action == "start"
    assert adm(9.0, False, _info(6.6), own_running=False, waited_s=0, **{**kw, "floor": 0.0}).note == "alone"
    assert adm(9.0, True, _info(None), own_running=False, waited_s=0, **kw).action == "start"
    assert adm(2.0, True, _info(20.0), own_running=False, waited_s=0, starving=True, **kw).kind == "starve"


def test_floor_gap_that_waiting_cannot_close_starts_in_spill_mode():
    """Review 2026-11-07 (live 08:30 UTC): prices_finalize (10 GB) with 7.9 GB free and one 0.5 GB thread job of the
    worker reserved waited the full max wait, then was deferred - although ending that reservation could never have
    closed the 3 GB floor gap (7.9 - 10 = -2.1 GB). Such a gap is like `alone`: spill mode right away."""
    adm = RB.admission
    kw = {"light": False, "wait_max": 1800.0, "floor": 3.0, "strict": False}
    live = {**_info(7.9, 0.5, 1), "reclaim_gb": 0.5, "holders": ["cftc_signals"]}
    a = adm(10.0, False, live, own_running=False, waited_s=0, defer=True, **kw)
    assert a.action == "start" and a.note == "spill_alone" and a.kind == "floor"
    assert a.fit_gb == pytest.approx(7.9 - 0.5 - 3.0) and "waiting cannot help" in a.reason
    assert adm(10.0, False, live, own_running=False, waited_s=0, **{**kw, "strict": True}).action == "defer"
    # jobs of the same run in flight: still waits for them (their end gives the spill start more room)
    assert adm(10.0, False, live, own_running=True, waited_s=0, defer=True, **kw).action == "wait"
    # a reservation big enough to close the gap: waiting can help -> wait, then defer (sync) as before
    big = {**_info(9.0, 3.0, 1), "reclaim_gb": 5.0}
    assert adm(4.0, True, big, own_running=False, waited_s=10, defer=True, **kw).action == "wait"
    assert adm(4.0, True, big, own_running=False, waited_s=1801, defer=True, **kw).action == "defer"
    # reclaim counts a process job's usage too: 2 GB outstanding + 4 GB used could close a 4 GB gap
    assert adm(10.0, False, {**_info(11.0, 2.0, 1), "reclaim_gb": 6.0}, own_running=False, waited_s=0, defer=True,
               **kw).action == "wait"


def test_check_reports_what_ending_the_reservations_would_reclaim(tmp_data, monkeypatch):
    recs = [{"job_id": "t", "budget_gb": 0.5, "child_pid": None},
            {"job_id": "p", "budget_gb": 6.0, "child_pid": 4242}]
    monkeypatch.setattr(RB, "usage_gb", lambda r: 4.0 if r.get("child_pid") else 0.0)
    ok, info = RB.check(10.0, tmp_data, free=12.0, recs=recs)
    assert info["outstanding_gb"] == pytest.approx(0.5 + 2.0) and info["reclaim_gb"] == pytest.approx(0.5 + 6.0)
    monkeypatch.setattr(RB, "usage_gb", lambda r: 7.0 if r.get("child_pid") else 0.0)   # over its budget
    assert RB.check(10.0, tmp_data, free=12.0, recs=recs)[1]["reclaim_gb"] == pytest.approx(0.5 + 7.0)


def test_floor_env_and_job_env_spill(monkeypatch):
    monkeypatch.delenv("BIG360_RAM_FLOOR_GB", raising=False)
    assert RB.floor_gb() == 3.0
    monkeypatch.setenv("BIG360_RAM_FLOOR_GB", "2.5")
    assert RB.floor_gb() == 2.5
    monkeypatch.setenv("BIG360_RAM_FLOOR_STRICT", "1")
    assert RB.floor_strict()
    small = RB.job_env(4.0)
    assert "POLARS_ENGINE_AFFINITY" not in small and "BIG360_SPILL" not in small
    big = RB.job_env(9.0)                                    # heavy-slot class: spill to disk
    assert big["POLARS_ENGINE_AFFINITY"] == "streaming" and big["BIG360_SPILL"] == "1" and big["POLARS_TEMP_DIR"]
    fit = RB.job_env(6.0, fit_gb=3.0)                        # admitted below the floor: DuckDB from what fits
    assert fit["BIG360_DUCKDB_MEMORY"] == f"{int(3.0 * 0.6 * 1024)}MiB" and fit["BIG360_JOB_FIT_GB"] == "3.00"
    assert fit["BIG360_JOB_MEM_GB"] == "6"                   # the Job Object cap stays the budget
    monkeypatch.setenv("BIG360_POLARS_STREAMING", "0")
    assert "POLARS_ENGINE_AFFINITY" not in RB.job_env(9.0)


# ------------------------------------------------------------------------------------------------ runner


@pytest.fixture()
def budget_on(monkeypatch):
    monkeypatch.setenv("BIG360_RAM_BUDGET", "1")
    monkeypatch.setenv("BIG360_RAM_HEADROOM_GB", "4")
    monkeypatch.delenv("BIG360_RAM_FLOOR_GB", raising=False)
    monkeypatch.delenv("BIG360_RAM_FLOOR_STRICT", raising=False)
    monkeypatch.setattr(RB, "free_gb", lambda: 10.0)
    yield


def test_runner_defers_below_the_floor_when_asked(tmp_data, budget_on):
    other = RB.reserve("prices_finalize", 5.0, "process", settings=tmp_data)   # another runner's job
    try:
        reg = make_registry([{"id": "mid", "module": f"{FAKE}:ok", "mem_gb": 2.5}])   # 10 - 5 - 2.5 = 2.5 < 3
        t0 = time.monotonic()
        o = JobRunner(reg, tmp_data).run_job("mid", RunOptions(as_of=AS_OF, ram_defer=True, ram_wait_max_s=0.6))
        assert o.status == "skipped" and o.reason == "deferred" and o.error_type == "RamFloor"
        assert "RAM floor" in o.message and time.monotonic() - t0 >= 0.5
        row = JobRunStore(tmp_data).read().filter(pl.col("job_id") == "mid").row(0, named=True)
        assert row["status"] == "skipped" and row["error_type"] == "RamFloor"
        assert not o.blocks_dependents                                         # dependents run on what exists
        assert RB.waits(tmp_data) == []                                        # the wait record is cleared
    finally:
        other.release()


def test_runner_records_the_wait_reason_while_waiting(tmp_data, budget_on):
    import threading

    other = RB.reserve("build_serving_db", 5.0, "process", settings=tmp_data)
    seen: list = []

    def watch() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not seen:
            w = RB.waits(tmp_data)
            if w:
                seen.extend(w)
            time.sleep(0.05)

    th = threading.Thread(target=watch)
    th.start()
    threading.Timer(1.0, other.release).start()
    reg = make_registry([{"id": "mid", "module": f"{FAKE}:ok", "mem_gb": 2.5}])
    o = JobRunner(reg, tmp_data).run_job("mid", RunOptions(as_of=AS_OF, ram_wait_max_s=30))
    th.join(6)
    assert o.status == "success"                                               # started once the RAM was freed
    assert seen and seen[0]["job_id"] == "mid" and seen[0]["kind"] == "floor" and seen[0]["pid"] == os.getpid()
    assert "build_serving_db" in seen[0]["reason"]
    assert RB.waits(tmp_data) == []


def test_start_by_in_the_past_defers(tmp_data):
    reg = make_registry([{"id": "j", "module": f"{FAKE}:ok"}])
    o = JobRunner(reg, tmp_data).run_job("j", RunOptions(as_of=AS_OF, start_by=time.time() - 1))
    assert o.status == "skipped" and o.reason == "deferred" and o.error_type == "StartBudget"


@win_only
def test_alone_below_the_floor_starts_in_spill_mode(tmp_data, budget_on, monkeypatch):
    monkeypatch.setattr(RB, "free_gb", lambda: 7.0)                            # 7 - 6 = 1 GB < floor, nothing else
    reg = make_registry([{"id": "dj", "module": f"{FAKE}:env_probe", "executor": "process", "mem_gb": 6,
                          "timeout_min": 3}])
    o = JobRunner(reg, tmp_data).run_job("dj", RunOptions(as_of=AS_OF))
    assert o.status == "success", o.message
    fit = 7.0 - 0.0 - 3.0
    assert f"BIG360_JOB_FIT_GB={fit:.2f}" in o.message and "POLARS_ENGINE_AFFINITY=streaming" in o.message
    assert f"BIG360_DUCKDB_MEMORY={int(fit * 0.6 * 1024)}MiB" in o.message


def test_wait_files_are_per_process_and_dead_ones_removed(tmp_data):
    RB.note_wait("tech_levels", "slot", "heavy slot held by prices_finalize (10 GB)", tmp_data)
    w = RB.waits(tmp_data)
    assert [(x["job_id"], x["kind"]) for x in w] == [("tech_levels", "slot")]
    assert RB.current_waits()["tech_levels"]["reason"].startswith("heavy slot held")
    dead = RB.admission_dir(tmp_data) / "999999.json"
    dead.write_text(json.dumps({"pid": 999999, "waits": {"x": {"job_id": "x"}}}), encoding="utf-8")
    assert [x["job_id"] for x in RB.waits(tmp_data)] == ["tech_levels"] and not dead.exists()
    RB.clear_wait("tech_levels", tmp_data)
    assert RB.waits(tmp_data) == [] and not list(RB.admission_dir(tmp_data).glob("*.json"))


def test_filler_tasks_defer_instead_of_spilling(tmp_data, monkeypatch):
    """Gap-filler work is opportunistic: taskrun asks for ram_defer and maps a deferred job to `deferred` (exit 3 + result
    status deferred: the filler retries later with back-off, without counting an attempt; review 2026-11-07 - no longer `blocked`)."""
    from app.ops import taskrun
    from app.scheduler import runner as R

    seen = {}

    def fake_run_jobs(self, ids, opts):
        seen["ram_defer"] = opts.ram_defer
        j = ids[0]
        o = R.JobOutcome(j, "skipped", "deferred", [], 0, 0.0, 0, "RamFloor", "deferred: RAM floor ...")
        return R.RunReport(j, None, AS_OF, {j: o}, __import__("datetime").datetime.now())

    monkeypatch.setattr(R.JobRunner, "run_jobs", fake_run_jobs)
    status, outcomes, _ = taskrun.run_jobs([{"job": "fx_rates"}])
    assert status == "deferred" and seen["ram_defer"] is True and outcomes["fx_rates"]["reason"] == "deferred"
    assert taskrun.EXIT["deferred"] == taskrun.EXIT["blocked"] == 3   # older fillers: blocked (no attempt)


def test_filler_task_retry_skips_the_fetch_that_already_succeeded(tmp_data, monkeypatch):
    """Review 2026-11-07: a yf_refetch / polygon_dates task whose prices_finalize the runner deferred re-ran its fetch
    step (force=True) on every retry - an endless Yahoo / Polygon re-download. Steps that succeeded are remembered per
    task: the retry runs prices_finalize only; once the task ends ok the memo is gone."""
    from app.ops import taskrun
    from app.scheduler import runner as R

    calls: list[str] = []
    defer = {"on": True}

    def fake_run_jobs(self, ids, opts):
        j = ids[0]
        calls.append(j)
        if j == "prices_finalize" and defer["on"]:
            o = R.JobOutcome(j, "skipped", "deferred", [], 0, 0.0, 0, "RamFloor", "deferred: RAM floor: ... < floor 3 GB")
        else:
            o = R.JobOutcome(j, "success", None, ["R1"], 1, 0.1, 120)
        return R.RunReport(j, None, AS_OF, {j: o}, __import__("datetime").datetime.now())

    monkeypatch.setattr(R.JobRunner, "run_jobs", fake_run_jobs)
    spec = {"kind": "polygon_dates", "dates": ["2026-11-05", "2026-11-06"], "task_id": "gap:prices.US:US"}
    res = taskrun.execute(spec)
    assert res["status"] == "deferred" and "RAM floor" in res["message"] and taskrun.EXIT[res["status"]] == 3
    assert calls == ["prices_us_polygon", "prices_finalize"]
    assert taskrun.memo_path("gap:prices.US:US").exists()
    res = taskrun.execute(spec)                                   # retry: still deferred, the fetch is not repeated
    assert res["status"] == "deferred" and calls[2:] == ["prices_finalize"]
    assert res["outcomes"]["prices_us_polygon"]["reason"] == "done_earlier"
    other = {**spec, "dates": ["2026-11-06"]}                     # different work (params): the fetch runs again
    taskrun.execute({**other, "task_id": "gap:prices.US:US"})
    assert calls[3] == "prices_us_polygon"
    defer["on"] = False
    calls.clear()
    res = taskrun.execute(spec)
    assert res["status"] == "ok" and calls == ["prices_finalize"]
    assert not taskrun.memo_path("gap:prices.US:US").exists()     # done: the next task fetches afresh
    # the memo expires: an old staged fetch is not trusted
    taskrun._write_memo("t1", {taskrun.step_key("j", {}): {"job": "j", "finished_at": "2026-01-01T00:00:00+00:00"}})
    assert taskrun.read_memo("t1") == {} and taskrun.read_memo(None) == {}
