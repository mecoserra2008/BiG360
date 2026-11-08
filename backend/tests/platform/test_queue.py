"""File job queue (atomic enqueue/claim on Windows), crash recovery, worker request handling, single-instance lock."""

from __future__ import annotations

import json
import threading

import polars as pl
import pytest

from app.core import jobqueue
from app.scheduler.runner import JobRunStore
from app.scheduler.worker import Worker, WorkerAlreadyRunning, read_heartbeat, worker_is_running
from tests.platform.conftest import FAKE, make_registry


def test_enqueue_claim_complete(tmp_data):
    req = jobqueue.enqueue_job("dq_checks", params={"tables": ["fx_rates"]})
    assert req["run_id"].startswith("R") and req["request_id"].startswith("Q")
    pending = jobqueue.list_requests("pending")
    assert len(pending) == 1 and pending[0]["job_id"] == "dq_checks"
    assert not list(tmp_data.queue_dir.glob(".tmp-*"))  # atomic write leaves no temp file
    assert jobqueue.job_is_queued("dq_checks")
    path, got = jobqueue.claim_next()
    assert got["request_id"] == req["request_id"] and path.parent.name == "processing"
    assert jobqueue.claim_next() is None
    out = jobqueue.complete(path, got, {"status": "success"})
    assert out.parent.name == "done" and not path.exists()
    assert json.loads(out.read_text(encoding="utf-8"))["result"]["status"] == "success"
    assert not jobqueue.job_is_queued("dq_checks")


def test_unknown_job_rejected(tmp_data):
    with pytest.raises(jobqueue.JobNotFound):
        jobqueue.enqueue_job("no_such_job")
    with pytest.raises(jobqueue.JobNotFound):
        jobqueue.enqueue_dag("no_such_dag")       # ("hourly" became a real DAG with roadmap v3 N3)


def test_concurrent_claims_each_request_once(tmp_data):
    for i in range(20):
        jobqueue.enqueue({"kind": "job", "job_id": f"j{i}"})
    claimed: list[str] = []
    lock = threading.Lock()

    def consumer():
        while True:
            c = jobqueue.claim_next()
            if c is None:
                return
            with lock:
                claimed.append(c[1]["job_id"])

    threads = [threading.Thread(target=consumer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(f"j{i}" for i in range(20))


def test_unparseable_request_goes_to_failed(tmp_data):
    d = jobqueue.queue_dirs()
    (d["pending"] / "20260101T000000-bad.json").write_text("{not json", encoding="utf-8")
    assert jobqueue.claim_next() is None
    assert len(list(d["failed"].glob("*.json"))) == 1


def test_recover_processing_requeues_then_fails(tmp_data):
    jobqueue.enqueue({"kind": "job", "job_id": "x"})
    jobqueue.claim_next()
    assert jobqueue.recover_processing(max_attempts=2) == {"requeued": 1, "failed": 0}
    assert len(jobqueue.list_requests("pending")) == 1
    jobqueue.claim_next()
    assert jobqueue.recover_processing(max_attempts=2) == {"requeued": 0, "failed": 1}
    assert jobqueue.list_requests("pending") == [] and len(jobqueue.list_requests("failed")) == 1


def test_job_is_running_reflects_lock(tmp_data):
    assert not jobqueue.job_is_running("abc")
    lk = jobqueue.job_lock("abc")
    lk.acquire()
    try:
        assert jobqueue.job_is_running("abc")
    finally:
        lk.release()
    assert not jobqueue.job_is_running("abc")


def test_worker_processes_queue_with_downstream(tmp_data):
    reg = make_registry([
        {"id": "a", "module": f"{FAKE}:ok"},
        {"id": "b", "module": f"{FAKE}:ok", "depends_on": ["a"]},
        {"id": "c", "module": f"{FAKE}:ok"},
    ])
    req = jobqueue.enqueue({"kind": "job", "job_id": "a", "include_downstream": True,
                            "params": {"as_of": "2026-10-09", "universe": "mvp"}})
    w = Worker(tmp_data, reg, enable_schedule=False, poll_s=0.1)
    w.acquire_lock()
    try:
        assert worker_is_running(tmp_data)
        with pytest.raises(WorkerAlreadyRunning):
            Worker(tmp_data, reg, enable_schedule=False).acquire_lock()
        w.startup_hygiene()
        assert w.poll_queue(block=True) == 1
        w.heartbeat()
        hb = read_heartbeat(tmp_data)
        assert hb and hb["alive"] and hb["scheduler_enabled"] is False
    finally:
        w.shutdown()
    assert not worker_is_running(tmp_data)
    done = jobqueue.list_requests("done")
    assert len(done) == 1 and done[0]["result"]["status"] == "success"
    runs = JobRunStore(tmp_data).read()
    assert sorted(runs["job_id"].to_list()) == ["a", "b"]
    first = runs.filter(pl.col("job_id") == "a").row(0, named=True)
    assert first["run_id"] == req["run_id"] and first["trigger"] == "api"  # API-visible run id is honoured
    assert str(first["as_of_date"]) == "2026-10-09"


def test_worker_marks_orphaned_running_rows(tmp_data):
    from datetime import UTC, datetime

    JobRunStore(tmp_data).write({"run_id": "RORPHAN", "year_month": "2026-10", "job_id": "zzz", "trigger": "manual",
                                 "status": "running", "attempt": 1, "started_at": datetime.now(UTC)})
    assert JobRunStore(tmp_data).mark_orphans() == 1
    r = JobRunStore(tmp_data).read().row(0, named=True)
    assert r["status"] == "failed" and r["error_type"] == "Orphaned"


def test_worker_scheduler_registers_crons(tmp_data):
    reg = make_registry([{"id": "a", "module": f"{FAKE}:ok", "dag": "nightly"}])
    w = Worker(tmp_data, reg, enable_schedule=True, shutdown_grace_s=1)
    w.acquire_lock()
    try:
        w.start_scheduler()                       # default profile: weekly (O3.4)
        nxt = w.next_runs()
        # roadmap v3 N3: every profile also carries the hourly live-news entry
        assert set(nxt) == {"weekly_refresh", "cftc_weekly", "hourly_news"} and "T02:00:00" in nxt["weekly_refresh"]
        from app.ops.schedule import set_active_profile

        set_active_profile("daily", tmp_data)      # hot reload on the next tick: jobs.yaml crons
        w.profile_sched.tick()
        nxt = w.next_runs()
        assert "nightly" in nxt and "01:30:00" in nxt["nightly"]
    finally:
        w.shutdown()
