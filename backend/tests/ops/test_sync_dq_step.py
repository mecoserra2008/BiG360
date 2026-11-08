"""A5 2026-11: dq_checks runs after a sync publish (at most every 20 h). Root cause of the 10-31 -> 11-06 gap: under the
weekly profile only the Saturday chain ran it, while the 30-min syncs (dq_checks excluded from their plan) published."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


def _sync(tmp_data, monkeypatch, mode="incremental", runs=None, pressure=None, outcome=None, expected=None,
          opts_log=None):
    """outcome(job, opts) -> JobOutcome | None overrides the stub's success; expected = {job: p95 seconds};
    opts_log collects (job, RunOptions) of every run."""
    from app.ops.sync.run import SyncRun
    from app.ops.sync.scan import ScanResult
    from app.scheduler.runner import JobOutcome, JobRegistry, RunReport

    monkeypatch.setenv("BIG360_HEAVY_SLOT", "0")
    calls: list[tuple[str, dict]] = []
    frame = runs if runs is not None else pl.DataFrame()
    if expected is not None:
        monkeypatch.setattr(SyncRun, "_expected_s", lambda self, job: expected.get(job))

    class Store:
        def read(self):
            return frame

    class Runner:
        store = Store()

        def run_jobs(self, ids, opts):
            calls.append((ids[0], dict(opts.params)))
            if opts_log is not None:
                opts_log.append((ids[0], opts))
            o = outcome(ids[0], opts) if outcome is not None else None
            o = o or JobOutcome(ids[0], "success", None, ["R1"], 1, 0.01, 0, None, "ok")
            return RunReport(ids[0], None, opts.as_of or NOW.date(), {ids[0]: o}, datetime.now(UTC))

    def scan(settings, datasets=None, ledger=None):
        return ScanResult(datetime.now(UTC).isoformat(), "2026-11-05", [], {}, {"US": "2026-11-05"}, 0.01)

    sr = SyncRun(tmp_data, trigger="interval", registry=JobRegistry.load(), runner=Runner(), scan_fn=scan,
                 publish_fn=lambda pass_no, ran: {"mode": mode, "status": "success"}, passes=2,
                 pressure_fn=lambda: pressure, sleep_s=0.01, now=NOW)
    monkeypatch.setattr(sr, "_refresh_gaps", lambda: None)
    sr.run()
    return sr, calls


def _runs(*rows):
    return pl.DataFrame([{"job_id": "dq_checks", "status": s, "error_type": e, "started_at": t, "finished_at": t}
                         for s, e, t in rows],
                        schema={"job_id": pl.Utf8, "status": pl.Utf8, "error_type": pl.Utf8,
                                "started_at": pl.Datetime("us", "UTC"), "finished_at": pl.Datetime("us", "UTC")})


@pytest.mark.parametrize("mode", ["incremental", "full"])
def test_dq_runs_once_after_a_publish_before_the_guard(tmp_data, monkeypatch, mode):
    # last complete run 10-31 21:53 (the audit's "6 days old"): due after this sync's publish
    sr, calls = _sync(tmp_data, monkeypatch, mode, runs=_runs(("success", None, datetime(2026, 10, 31, 21, 53, tzinfo=UTC))))
    jobs = [c[0] for c in calls]
    assert jobs.count("dq_checks") == 1 and jobs[-2:] == ["dq_checks", "qa_regression"]
    assert ("dq_checks", {"trigger": "sync"}) in calls
    node = next(j for j in sr.status["jobs"] if j["job"] == "dq_checks")
    assert node["trigger"] == "post" and node["status"] == "success" and node["lane"] == "heavy"


def test_dq_not_run_without_a_publish(tmp_data, monkeypatch):
    _, calls = _sync(tmp_data, monkeypatch, "none")
    assert "dq_checks" not in [c[0] for c in calls]


def test_dq_throttled_and_skipped_under_pressure(tmp_data, monkeypatch):
    sr, calls = _sync(tmp_data, monkeypatch, runs=_runs(("success", None, NOW - timedelta(hours=3))))
    assert "dq_checks" not in [c[0] for c in calls] and calls[-1][0] == "qa_regression"
    node = next(j for j in sr.status["jobs"] if j["job"] == "dq_checks")
    assert node["status"] == "skipped" and "at most every 20 h" in node["reason"]
    assert "dq_checks" not in (sr.status.get("failed_jobs") or [])
    sr, calls = _sync(tmp_data, monkeypatch, runs=_runs(("success", None, NOW - timedelta(hours=30))),
                      pressure="free RAM 1.2 GB")
    assert "dq_checks" not in [c[0] for c in calls]
    node = next(j for j in sr.status["jobs"] if j["job"] == "dq_checks")
    assert node["status"] == "skipped" and "memory pressure" in node["reason"]


def test_dq_excluded_from_the_plan():
    from app.ops.sync import policy as P

    assert P.DQ_JOB == "dq_checks" and P.DQ_JOB in P.EXCLUDE


_DUE = (("success", None, datetime(2026, 10, 31, 21, 53, tzinfo=UTC)),)


def test_dq_admission_is_bounded(tmp_data, monkeypatch):
    """A5 review: the post-publish dq_checks gets the A3 start-admission rules - deferred instead of waiting when the RAM
    floor or the heavy slot keeps it from starting within policy.DQ_START_WAIT_S (or the sync budget, if sooner)."""
    import time

    from app.ops.sync import policy as P

    log: list = []
    t0 = time.time()
    sr, calls = _sync(tmp_data, monkeypatch, runs=_runs(*_DUE), opts_log=log)
    o = next(op for j, op in log if j == "dq_checks")
    assert o.ram_defer is True and o.start_by is not None
    assert o.start_by <= time.time() + P.DQ_START_WAIT_S and o.start_by <= sr.budget.deadline
    assert o.start_by >= t0                                     # a real window, not an instant deferral
    assert o.ram_wait_max_s is not None and o.ram_wait_max_s <= P.DQ_START_WAIT_S
    qa = next(op for j, op in log if j == "qa_regression")
    assert qa.start_by is None                                   # the guard's admission is unchanged


def test_dq_deferred_when_a_big_job_holds_the_heavy_slot(tmp_data, monkeypatch):
    """prices_finalize (10 GB) of another process holds the heavy slot when the sync reaches dq_step: without start_by
    heavy_slot.acquire_heavy would wait for as long as it holds it (the sync lock with it). With the bounded admission
    the runner gives up at start_by and reports `deferred`: the sync finishes, the guard still runs, and dq_checks is
    recorded as deferred for the next publishing sync."""
    from app.scheduler.runner import JobOutcome

    def outcome(job, opts):
        if job != "dq_checks":
            return None
        assert opts.start_by is not None, "dq_checks would wait for the heavy slot without limit"
        msg = ("deferred: start budget ran out while waiting (heavy slot held by prices_finalize (10 GB)); "
               "the next scheduled run retries")
        return JobOutcome(job, "skipped", "deferred", ["R2"], 0, 0.0, 0, "HeavySlotBusy", msg)

    sr, calls = _sync(tmp_data, monkeypatch, runs=_runs(*_DUE), outcome=outcome)
    assert sr.status["result"] in ("success", "partial") and sr.phase == "done"
    assert [c[0] for c in calls][-2:] == ["dq_checks", "qa_regression"]
    node = next(j for j in sr.status["jobs"] if j["job"] == "dq_checks")
    assert node["status"] == "skipped" and node["reason"].startswith("deferred:") and "prices_finalize" in node["reason"]
    assert any(d["job"] == "dq_checks" and d["trigger"] == "post" for d in sr.deferred)
    assert "dq_checks" not in (sr.status.get("failed_jobs") or [])


def test_dq_deferred_when_its_p95_exceeds_the_sync_budget_left(tmp_data, monkeypatch):
    # dq_checks p95 ~ 38 min (history 6-38 min); give it a p95 longer than the whole 120-min budget
    sr, calls = _sync(tmp_data, monkeypatch, runs=_runs(*_DUE), expected={"dq_checks": 10 * 3600.0})
    assert "dq_checks" not in [c[0] for c in calls] and calls[-1][0] == "qa_regression"
    node = next(j for j in sr.status["jobs"] if j["job"] == "dq_checks")
    assert node["status"] == "skipped" and "deferred:" in node["reason"] and "(p95)" in node["reason"]
    assert any(d["job"] == "dq_checks" for d in sr.deferred)
    # a short p95 fits: it runs
    _, calls = _sync(tmp_data, monkeypatch, runs=_runs(*_DUE), expected={"dq_checks": 600.0})
    assert "dq_checks" in [c[0] for c in calls]


def test_dq_deferral_is_not_planned_as_a_pass_job(tmp_data, monkeypatch):
    """The carry entry a deferred dq_checks leaves never turns it into a pass job of the next sync (EXCLUDE): only
    dq_step runs it, as a post node - also when that sync publishes nothing (coverage v3 A3, caps.py: the live
    snapshot still needs its checks) and exempt from the budget rules once (the 10-h p95 is still patched in) - and the
    entry is dropped once it ran. Review 2026-11-07: a carry entry made build_pass raise AttributeError
    (_post_carried was missing), so every sync after a deferral crashed before its first fetch."""
    from app.ops.sync import caps as C

    _sync(tmp_data, monkeypatch, runs=_runs(*_DUE), expected={"dq_checks": 10 * 3600.0})
    assert "dq_checks" in C.read_carry(tmp_data)
    sr, calls = _sync(tmp_data, monkeypatch, "none", runs=_runs(*_DUE))
    assert sr.status["result"] == "success" and [c[0] for c in calls] == ["dq_checks"]
    assert not any(n.job == "dq_checks" for (p, k), n in sr.nodes.items() if not str(k).startswith("post:"))
    node = sr.nodes[(sr.passes, "post:dq_checks")]
    assert node.status == "success" and node.carried == 1
    assert any(w.startswith("deferred by the previous sync") for w in node.why)
    assert "dq_checks" not in C.read_carry(tmp_data)


def _carry(tmp_data, job, run_id="S0"):
    from app.ops.sync import caps as C
    from app.ops.sync import state as S

    C.write_carry([{"job": job, "pass_no": 2, "trigger": "post", "reason": "sync budget of 120 min used up",
                    "deferred_at": S.now_iso()}], run_id, tmp_data)


def test_carried_dq_settled_by_a_recent_run_kept_under_pressure(tmp_data, monkeypatch):
    """A carried dq_checks that a complete run already covers (e.g. the Saturday chain, 3 h ago) is settled: not run,
    and dropped from the carry file. Skipped for memory pressure it stays carried: the next sync retries."""
    from app.ops.sync import caps as C

    _carry(tmp_data, "dq_checks")
    sr, calls = _sync(tmp_data, monkeypatch, "none", runs=_runs(("success", None, NOW - timedelta(hours=3))))
    assert sr.status["result"] == "success" and calls == []
    node = sr.nodes[(sr.passes, "post:dq_checks")]
    assert node.status == "skipped" and "at most every 20 h" in node.reason
    assert "dq_checks" not in C.read_carry(tmp_data)

    _carry(tmp_data, "dq_checks", "S1")
    sr, calls = _sync(tmp_data, monkeypatch, "none", runs=_runs(*_DUE), pressure="free RAM 1.2 GB")
    assert calls == [] and "memory pressure" in sr.nodes[(sr.passes, "post:dq_checks")].reason
    assert "dq_checks" in C.read_carry(tmp_data)


def test_carried_guard_runs_without_a_publish(tmp_data, monkeypatch):
    """A carried qa_regression runs in the next sync even when it publishes nothing, and is then dropped."""
    from app.ops.sync import caps as C

    _carry(tmp_data, "qa_regression")
    sr, calls = _sync(tmp_data, monkeypatch, "none", runs=_runs(("success", None, NOW - timedelta(hours=3))))
    assert sr.status["result"] == "success" and [c[0] for c in calls] == ["qa_regression"]
    node = sr.nodes[(sr.passes, "post:qa_regression")]
    assert node.status == "success" and node.carried == 1
    assert "qa_regression" not in C.read_carry(tmp_data)
