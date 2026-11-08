"""Runner execution: job_runs bookkeeping, retries/backoff, upstream skips, unavailable modules, idempotency,
timeouts, process executor, resume, parallelism and per-source lanes."""

from __future__ import annotations

import os
import threading
import time
from datetime import date

import polars as pl
import pytest

from app.core.jobqueue import job_lock
from app.scheduler.runner import JobRunner, JobRunStore, RunOptions
from tests.platform import fake_jobs
from tests.platform.conftest import FAKE, make_registry

AS_OF = date(2026, 10, 9)


@pytest.fixture(autouse=True)
def _reset_counters():
    fake_jobs.COUNTERS.clear()
    yield


def _runs(settings) -> pl.DataFrame:
    return JobRunStore(settings).read().sort("started_at")


def test_success_writes_job_runs_and_log(tmp_data):
    reg = make_registry([{"id": "a", "module": f"{FAKE}:ok"}])
    rep = JobRunner(reg, tmp_data).run_jobs(["a"], RunOptions(as_of=AS_OF, params={"tickers": ["AAPL"]}))
    o = rep.outcomes["a"]
    assert o.status == "success" and o.rows_written == 3 and rep.status == "success"
    runs = _runs(tmp_data)
    assert runs.height == 1
    r = runs.row(0, named=True)
    assert r["status"] == "success" and r["attempt"] == 1 and r["rows_written"] == 3
    assert r["as_of_date"] == AS_OF and r["run_key"].startswith("a|2026-10-09|")
    assert r["watermark"] == "2026-10-09" and r["tables_written"] == ["fx_rates"]
    assert '"tickers": ["AAPL"]' in r["params"]
    log_file = tmp_data.logs_dir / r["log_path"]
    assert log_file.exists() and "hello from a" in log_file.read_text(encoding="utf-8")
    # watermark is handed to the next run
    assert JobRunStore(tmp_data).last_watermark("a") == "2026-10-09"


def test_retries_with_backoff_then_success(tmp_data):
    reg = make_registry([{"id": "f", "module": f"{FAKE}:flaky", "retries": 2, "retry_backoff_s": 0.05,
                          "params": {"fail_times": 2}}])
    t0 = time.monotonic()
    o = JobRunner(reg, tmp_data).run_job("f", RunOptions(as_of=AS_OF))
    assert o.status == "success" and o.attempts == 3 and len(o.run_ids) == 3
    assert time.monotonic() - t0 >= 0.05 + 0.1  # 0.05 * 2**0 + 0.05 * 2**1
    runs = _runs(tmp_data)
    assert runs["attempt"].to_list() == [1, 2, 3]
    assert runs["status"].to_list() == ["failed", "failed", "success"]
    assert runs["error_type"].to_list()[:2] == ["ConnectionError", "ConnectionError"]


def test_failure_skips_dependents_but_not_independent_branches(tmp_data):
    reg = make_registry([
        {"id": "bad", "module": f"{FAKE}:fail", "retries": 1},
        {"id": "child", "module": f"{FAKE}:ok", "depends_on": ["bad"]},
        {"id": "grandchild", "module": f"{FAKE}:ok", "depends_on": ["child"]},
        {"id": "tolerant", "module": f"{FAKE}:ok", "depends_on": ["bad"], "on_upstream_failure": "run"},
        {"id": "independent", "module": f"{FAKE}:ok"},
    ])
    rep = JobRunner(reg, tmp_data).run_jobs(list(reg.jobs), RunOptions(as_of=AS_OF, backoff_scale=0))
    out = rep.outcomes
    assert out["bad"].status == "failed" and out["bad"].attempts == 2
    assert "SECRET123" not in (out["bad"].message or "")  # redacted
    assert (out["child"].status, out["child"].reason) == ("skipped", "upstream_failed")
    assert (out["grandchild"].status, out["grandchild"].reason) == ("skipped", "upstream_failed")
    assert out["tolerant"].status == "success" and out["independent"].status == "success"
    assert rep.status == "partial"
    runs = _runs(tmp_data)
    skipped = runs.filter(pl.col("job_id") == "child").row(0, named=True)
    assert skipped["status"] == "skipped" and skipped["error_type"] == "UpstreamFailed"
    assert all("SECRET123" not in (m or "") for m in runs["error_message"].to_list())


def test_unavailable_module_does_not_block(tmp_data):
    reg = make_registry([
        {"id": "future", "module": "app.not_built_yet.thing"},
        {"id": "after", "module": f"{FAKE}:ok", "depends_on": ["future"]},
    ])
    rep = JobRunner(reg, tmp_data).run_jobs(["future", "after"], RunOptions(as_of=AS_OF))
    assert (rep.outcomes["future"].status, rep.outcomes["future"].reason) == ("skipped", "unavailable")
    assert rep.outcomes["after"].status == "success"
    r = _runs(tmp_data).filter(pl.col("job_id") == "future").row(0, named=True)
    assert r["error_type"] == "JobUnavailable"


def test_missing_function_is_unavailable_at_run_time(tmp_data):
    reg = make_registry([{"id": "nf", "module": f"{FAKE}:no_such_function", "retries": 3}])
    o = JobRunner(reg, tmp_data).run_job("nf", RunOptions(as_of=AS_OF, backoff_scale=0))
    assert o.status == "failed" and o.error_type == "JobUnavailable" and o.attempts == 1  # not retried


def test_idempotent_run_key_and_force(tmp_data):
    reg = make_registry([{"id": "a", "module": f"{FAKE}:ok"}])
    runner = JobRunner(reg, tmp_data)
    assert runner.run_job("a", RunOptions(as_of=AS_OF)).status == "success"
    again = runner.run_job("a", RunOptions(as_of=AS_OF))
    assert (again.status, again.reason) == ("skipped", "idempotent")
    other_params = runner.run_job("a", RunOptions(as_of=AS_OF, params={"universe": "full"}))
    assert other_params.status == "success"
    forced = runner.run_job("a", RunOptions(as_of=AS_OF, force=True))
    assert forced.status == "success"
    assert _runs(tmp_data).height == 3


def test_upstream_rows_invalidate_idempotency(tmp_data):
    # same as_of, but an upstream job wrote new rows in this run -> the dependent must re-run (weekend/re-run case)
    reg = make_registry([{"id": "up", "module": f"{FAKE}:ok"},
                         {"id": "down", "module": f"{FAKE}:ok", "depends_on": ["up"]}])
    runner = JobRunner(reg, tmp_data)
    assert runner.run_job("down", RunOptions(as_of=AS_OF)).status == "success"
    rep = runner.run_jobs(["up", "down"], RunOptions(as_of=AS_OF))
    assert rep.outcomes["up"].status == "success"
    assert (rep.outcomes["down"].status, rep.outcomes["down"].reason) == ("success", None)
    # nothing upstream changed -> both skip as idempotent
    rep2 = runner.run_jobs(["up", "down"], RunOptions(as_of=AS_OF))
    assert [rep2.outcomes[j].reason for j in ("up", "down")] == ["idempotent", "idempotent"]


def test_non_retryable_result_and_partial(tmp_data):
    reg = make_registry([
        {"id": "nr", "module": f"{FAKE}:no_retry", "retries": 3},
        {"id": "p", "module": f"{FAKE}:partial"},
        {"id": "after_p", "module": f"{FAKE}:ok", "depends_on": ["p"]},
    ])
    rep = JobRunner(reg, tmp_data).run_jobs(list(reg.jobs), RunOptions(as_of=AS_OF, backoff_scale=0))
    assert rep.outcomes["nr"].status == "failed" and fake_jobs.COUNTERS["no_retry"] == 1
    assert rep.outcomes["p"].status == "partial"
    assert rep.outcomes["after_p"].status == "success"  # partial upstream still runs dependents


def test_thread_timeout_cancels_and_retries(tmp_data):
    # F18: a timed-out thread that honours the cancel token is gone -> the attempt is retried (no zombie)
    reg = make_registry([{"id": "h", "module": f"{FAKE}:hang", "timeout_min": 0.01, "retries": 2}])
    t0 = time.monotonic()
    o = JobRunner(reg, tmp_data).run_job("h", RunOptions(as_of=AS_OF, backoff_scale=0))
    assert o.status == "failed" and o.error_type == "TimeoutError" and o.attempts == 3
    assert "thread stopped" in o.message
    assert time.monotonic() - t0 < 120  # bookkeeping writes dominate on a loaded machine


def test_thread_ignoring_cancel_is_killed_and_retried(tmp_data):
    reg = make_registry([{"id": "s", "module": f"{FAKE}:stubborn", "timeout_min": 0.01, "retries": 1}])
    t0 = time.monotonic()
    o = JobRunner(reg, tmp_data).run_job("s", RunOptions(as_of=AS_OF, backoff_scale=0))
    assert o.status == "failed" and o.attempts == 2 and "thread stopped" in o.message
    assert time.monotonic() - t0 < 150
    assert not any(t.name == "job-s" and t.is_alive() for t in threading.enumerate())


def test_thread_blocked_in_c_call_is_a_zombie_not_retried(tmp_data):
    reg = make_registry([{"id": "b", "module": f"{FAKE}:blocked", "timeout_min": 0.01, "retries": 2, "params": {"sleep": 4}}])
    runner = JobRunner(reg, tmp_data)
    runner.kill_wait_s = 0.3
    o = runner.run_job("b", RunOptions(as_of=AS_OF, backoff_scale=0))
    assert o.status == "failed" and o.attempts == 1 and "still running" in o.message


def test_job_lock_prevents_concurrent_runs(tmp_data):
    reg = make_registry([{"id": "a", "module": f"{FAKE}:ok"}])
    lk = job_lock("a", tmp_data)
    lk.acquire()
    try:
        o = JobRunner(reg, tmp_data).run_job("a", RunOptions(as_of=AS_OF))
    finally:
        lk.release()
    assert (o.status, o.reason) == ("skipped", "already_running")


def test_resume_reruns_only_unfinished_jobs(tmp_data, tmp_path):
    flag = tmp_path / "flag"
    flag.write_text("x")
    rec = tmp_path / "rec.txt"
    reg = make_registry([
        {"id": "a", "module": f"{FAKE}:record", "params": {"record": str(rec)}},
        {"id": "b", "module": f"{FAKE}:fail_if_flag", "depends_on": ["a"], "params": {"flag": str(flag)}},
        {"id": "c", "module": f"{FAKE}:record", "depends_on": ["b"], "params": {"record": str(rec)}},
    ], dags={"t": {"cron": None}})
    runner = JobRunner(reg, tmp_data)
    rep1 = runner.run_dag("t", RunOptions(as_of=AS_OF, wait_for_dags=False))
    assert rep1.outcomes["b"].status == "failed" and rep1.outcomes["c"].reason == "upstream_failed"
    flag.unlink()
    rep2 = runner.run_dag("t", RunOptions(resume=True, wait_for_dags=False))
    assert rep2.dag_run_id == rep1.dag_run_id and rep2.as_of == AS_OF
    assert rep2.outcomes["a"].reason == "resumed"
    assert rep2.outcomes["b"].status == "success" and rep2.outcomes["c"].status == "success"
    assert rec.read_text().split() == ["a", "c"]  # a ran once only


def _intervals(path) -> dict[str, tuple[float, float]]:
    out = {}
    for line in path.read_text().splitlines():
        j, a, b = line.split()
        out[j] = (float(a), float(b))
    return out


def test_parallel_pool_and_source_lanes(tmp_data, tmp_path):
    rec = tmp_path / "intervals.txt"
    p = {"sleep": 0.5, "record": str(rec)}
    reg = make_registry([
        {"id": "s1", "module": f"{FAKE}:sleepy", "params": p},
        {"id": "s2", "module": f"{FAKE}:sleepy", "params": p},
        {"id": "p1", "module": f"{FAKE}:sleepy", "params": p, "sources": ["polygon"]},
        {"id": "p2", "module": f"{FAKE}:sleepy", "params": p, "sources": ["polygon"]},
    ])
    rep = JobRunner(reg, tmp_data).run_jobs(list(reg.jobs), RunOptions(as_of=AS_OF, max_workers=4))
    assert all(o.status == "success" for o in rep.outcomes.values())
    iv = _intervals(rec)

    def overlap(a: str, b: str) -> bool:
        return iv[a][0] < iv[b][1] and iv[b][0] < iv[a][1]

    assert overlap("s1", "s2")      # independent jobs run concurrently
    assert not overlap("p1", "p2")  # the polygon lane allows one job at a time


def test_process_executor_runs_in_child_and_times_out(tmp_data):
    reg = make_registry([
        {"id": "cpu", "module": f"{FAKE}:pid", "executor": "process", "timeout_min": 2},
        {"id": "stuck", "module": f"{FAKE}:hang", "executor": "process", "timeout_min": 0.12, "retries": 0},
    ])
    runner = JobRunner(reg, tmp_data)
    o = runner.run_job("cpu", RunOptions(as_of=AS_OF))
    assert o.status == "success", o.message
    assert o.message != str(os.getpid())
    r = _runs(tmp_data).filter(pl.col("job_id") == "cpu").row(0, named=True)
    assert r["rows_written"] == 1
    o2 = runner.run_job("stuck", RunOptions(as_of=AS_OF))
    assert o2.status == "failed" and o2.error_type == "TimeoutError"


def test_stop_event_skips_pending_jobs(tmp_data):
    reg = make_registry([
        {"id": "a", "module": f"{FAKE}:sleepy", "params": {"sleep": 0.3}},
        {"id": "b", "module": f"{FAKE}:ok", "depends_on": ["a"]},
    ])
    runner = JobRunner(reg, tmp_data)
    runner.stop_event.set()
    rep = runner.run_jobs(["a", "b"], RunOptions(as_of=AS_OF))
    assert {o.reason for o in rep.outcomes.values()} == {"stopped"}


def test_dag_dry_run_executes_nothing(tmp_data):
    reg = make_registry([{"id": "a", "module": f"{FAKE}:fail"}], dags={"t": {"cron": None}})
    rep = JobRunner(reg, tmp_data).run_dag("t", RunOptions(dry_run=True))
    assert rep.outcomes["a"].reason == "dry_run"
    assert _runs(tmp_data).height == 0


def test_soft_dependency_orders_but_never_blocks(tmp_data):
    reg = make_registry([
        {"id": "events", "module": f"{FAKE}:fail", "retries": 0},
        {"id": "events_child", "module": f"{FAKE}:ok", "depends_on": ["events"]},
        {"id": "prices", "module": f"{FAKE}:ok"},
        {"id": "scoring", "module": f"{FAKE}:ok", "depends_on": ["prices", "events_child"],
         "soft_depends_on": ["events_child"]},
        {"id": "hard_bad", "module": f"{FAKE}:fail", "retries": 0},
        {"id": "mixed", "module": f"{FAKE}:ok", "depends_on": ["hard_bad", "events"], "soft_depends_on": ["events"]},
    ])
    out = JobRunner(reg, tmp_data).run_jobs(list(reg.jobs), RunOptions(as_of=AS_OF, backoff_scale=0)).outcomes
    assert (out["events_child"].status, out["events_child"].reason) == ("skipped", "upstream_failed")
    assert out["scoring"].status == "success"            # soft upstream skipped -> still runs
    assert (out["mixed"].status, out["mixed"].reason) == ("skipped", "upstream_failed")  # hard edge still blocks
    assert "hard_bad" in (out["mixed"].message or "") and "events" not in (out["mixed"].message or "").split(": ")[1]


def test_soft_dependency_must_be_listed_in_depends_on():
    with pytest.raises(ValueError, match="soft dependency"):
        make_registry([{"id": "a", "module": f"{FAKE}:ok"},
                       {"id": "b", "module": f"{FAKE}:ok", "soft_depends_on": ["a"]}])
