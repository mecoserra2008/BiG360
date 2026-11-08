"""Alert hygiene 2026-10-30 (docs/OPERATIONS_ROADMAP.md "Alert hygiene 2026-10-30"), alerts 2-4:

2. dataset alerts follow the sync engine's reason codes (app.ops.freshness): only failed, due / source_unavailable
   past the SLA, or `stale` (no recent sync verdict) alert; not_yet_published / needs_key / no_source / fresh show
   their reason and never alert, whatever the gap detector's own SLA opinion is;
3. refresh failures are per job: the latest attempt across nightly / weekly / sync / manual runs decides, the jobs
   still failing are named; last refresh = the most recent of the sync and the scheduled runs;
4. the regression guard runs after every publishing sync (API + golden; UI smoke at most daily, never next to a
   heavy job or with < 4 GB free), the state is merged per suite and old failures age out (stale / superseded)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from app.ops import freshness as F
from app.ops import health as H

NOW = datetime(2026, 10, 30, 7, 40, tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ 2. datasets

#: the gap detector's view on 2026-10-30 (its own SLA rule says macro.ingest and short_interest are stale)
COV = {"datasets": {
    "macro.ingest": {"label": "Macro ingestion", "status": "stale", "data_as_of": "2026-10-29", "lag": 19, "sla": 9.0,
                     "detail": "4 of 33 source_id(s) stale"},
    "short_interest": {"label": "FINRA short interest", "status": "stale", "data_as_of": "2026-09-30", "lag": 15,
                       "sla": 10.0, "detail": "8 settlements in 150 days"},
    "prices.US": {"label": "Prices US", "status": "stale", "data_as_of": "2026-10-23", "lag": 4, "sla": 2},
    "prices.EU": {"label": "Prices EU", "status": "fresh", "data_as_of": "2026-10-28", "lag": 1, "sla": 6},
    "prices.KR": {"label": "Prices KR", "status": "fresh", "data_as_of": "2026-10-29", "lag": 0, "sla": 6},
    "fx.rates": {"label": "FX", "status": "stale", "data_as_of": "2026-10-24", "lag": 4, "sla": 2},
    "cftc.cot": {"label": "CFTC", "status": "fresh", "data_as_of": "2026-10-20", "lag": 0, "sla": 9},
    "inst.13f": {"label": "13F", "status": "missing", "lag": None, "sla": 9},
    "eu.shorts.de": {"label": "DE shorts", "status": "stale", "lag": 40, "sla": 9},
    "news.items": {"label": "News", "status": "partial", "data_as_of": "2026-10-29", "lag": 0, "sla": 6},
}}


def _sync(finished_at: datetime, state: str = "idle") -> dict:
    ds = [
        {"dataset": "macro.ingest", "reason": "fresh", "detail": "33 sources; every group pulled within 20 h"},
        {"dataset": "short_interest", "reason": "not_yet_published", "next_expected": "2026-11-11T23:00:00+00:00",
         "retry_at": "2026-10-30T08:36:02+00:00", "attempts": 3, "detail": "settlement 2026-10-15 not published yet"},
        {"dataset": "prices.US", "reason": "due", "detail": "4 sessions to fetch"},
        {"dataset": "prices.EU", "reason": "due", "detail": "1 session to fetch"},
        {"dataset": "prices.KR", "reason": "needs_key", "keys": ["KRX_APIKEY"], "detail": "secondary source needs a key"},
        {"dataset": "fx.rates", "reason": "source_unavailable", "retry_at": "2026-10-30T09:00:00+00:00"},
        {"dataset": "cftc.cot", "reason": "failed", "attempts": 3, "detail": "parked after 3 failures"},
        {"dataset": "inst.13f", "reason": "no_source", "detail": "documented"},
        {"dataset": "eu.shorts.de", "reason": "no_source", "detail": "no free official source"},
        {"dataset": "news.items", "reason": "fresh"},
    ]
    return {"state": state, "run_id": "S1", "finished_at": finished_at.isoformat(), "heartbeat_at": finished_at.isoformat(),
            "started_at": (finished_at - timedelta(minutes=30)).isoformat(), "datasets": ds}


def test_sync_reasons_decide_dataset_alerts():
    vs = F.dataset_views(COV, _sync(NOW - timedelta(minutes=10)), NOW)
    alerting = sorted(k for k, v in vs.items() if v["alerting"])
    # due past SLA (US 4 > 2), source_unavailable past SLA (FX 4 > 2), failed (CFTC); NOT macro / FINRA / KR / 13F / DE
    assert alerting == ["cftc.cot", "fx.rates", "prices.US"]
    assert vs["macro.ingest"]["reason"] == "fresh" and vs["macro.ingest"]["status"] == "fresh"
    assert vs["macro.ingest"]["detector_status"] == "stale" and vs["macro.ingest"]["reason_source"] == "sync"
    si = vs["short_interest"]
    assert si["reason"] == "not_yet_published" and not si["alerting"] and si["status"] == "fresh"
    assert si["next_expected"].startswith("2026-11-11") and si["retry_at"] and si["attempts"] == 3
    assert vs["prices.EU"]["reason"] == "due" and not vs["prices.EU"]["alerting"] and vs["prices.EU"]["status"] == "fresh"
    assert vs["prices.KR"]["keys"] == ["KRX_APIKEY"] and not vs["prices.KR"]["alerting"]
    assert vs["inst.13f"]["status"] == "partial" and not vs["inst.13f"]["alerting"]       # missing but no_source
    assert vs["news.items"]["status"] == "partial"                                       # detector gaps kept
    sch = {"worker_alive": True, "enabled": True, "period_days": 7, "last_success": {}}
    al = {a["id"]: a for a in H.ops_alerts(sch, COV, {"heartbeat_at": NOW.isoformat()}, now=NOW,
                                            sync=_sync(NOW - timedelta(minutes=10)))}
    assert "Prices US: due" in al["sla_critical"]["detail"] and "FX: source unavailable" in al["sla_critical"]["detail"]
    other = al["sla_other"]
    assert other["title"] == "1 dataset(s) failing or stale beyond SLA" and other["detail"].startswith("CFTC: failed")
    for quiet in ("Macro ingestion", "FINRA", "Prices EU", "Prices KR", "13F", "DE shorts"):
        assert quiet not in other["detail"] and quiet not in al["sla_critical"]["detail"]


def test_the_two_freshness_views_never_disagree():
    """For every reason code and every detector status, the alert follows the sync verdict; the detector's
    lag / SLA is only the clock for due / source_unavailable."""
    for reason in ("fresh", "due", "not_yet_published", "source_unavailable", "no_source", "needs_key", "failed"):
        for dstat, lag in (("fresh", 0), ("partial", 1), ("stale", 30), ("missing", None)):
            det = {"status": dstat, "lag": lag, "sla": 5}
            v = F.verdict(det, {"reason": reason}, use_sync=True)
            want = reason == "failed" or (reason in ("due", "source_unavailable") and lag is not None and lag > 5)
            assert v["alerting"] is want, (reason, dstat, v)
            assert (v["status"] in ("stale", "missing")) is want, (reason, dstat, v)


def test_old_or_missing_sync_status_falls_back_to_the_detector():
    for sync in (None, {"state": "never"}, _sync(NOW - timedelta(hours=F.SYNC_AUTHORITATIVE_H + 1))):
        vs = F.dataset_views(COV, sync, NOW)
        assert vs["macro.ingest"]["reason"] == "stale" and vs["macro.ingest"]["alerting"]
        assert vs["macro.ingest"]["reason_source"] == "detector"
    running = _sync(NOW - timedelta(hours=9), state="running")
    running["heartbeat_at"] = (NOW - timedelta(seconds=20)).isoformat()   # a sync running now: authoritative
    assert F.sync_authoritative(running, NOW) and not F.dataset_views(COV, running, NOW)["macro.ingest"]["alerting"]


def test_data_health_rows_show_reasons(tmp_data, monkeypatch):
    from app.ops.paths import ops_state_dir, write_json_atomic

    write_json_atomic(ops_state_dir(tmp_data) / "coverage_status.json", {"generated_at": NOW.isoformat(), **COV})
    write_json_atomic(ops_state_dir(tmp_data) / "sync_status.json", _sync(NOW - timedelta(minutes=5)))
    monkeypatch.setattr("app.ops.sync.state.sync_running", lambda s=None: False)
    d = H.data_health(tmp_data, job_runs=pl.DataFrame(), hb=None, registry=None, now=NOW)
    rows = {r["dataset"]: r for r in d["datasets"]}
    assert rows["short_interest"]["reason"] == "not_yet_published" and rows["short_interest"]["alerting"] is False
    assert rows["macro.ingest"]["status"] == "fresh" and rows["macro.ingest"]["detector_status"] == "stale"
    assert {b["dataset"] for b in d["sla_breaches"]} <= {"cftc.cot", "fx.rates", "prices.US"}
    assert "short_interest" not in {b["dataset"] for b in d["sla_breaches"]}
    assert d["freshness"]["source"] == "sync"
    assert not any("Macro ingestion" in (a.get("detail") or "") for a in d["alerts"])


# ------------------------------------------------------------------------------------------------ 3. refreshes


def _r(job, dag_run, status, start, trigger="schedule", err=None):
    return {"run_id": f"R{job}{start:%d%H%M}", "job_id": job, "dag_run_id": dag_run, "status": status, "trigger": trigger,
            "started_at": start, "finished_at": start + timedelta(minutes=5), "error_message": err}


def _job_runs() -> pl.DataFrame:
    t = datetime(2026, 10, 24, 10, tzinfo=UTC)
    n = "D20261024T070000Q09X-nightly"
    rows = [_r(j, n, "failed", t + timedelta(hours=i), err=f"{j} broke") for i, j in enumerate(
        ("dq_checks", "event_hotspots", "build_serving_db", "event_changepoints", "conformal_envelopes"))]
    rows += [_r(j, n, "success", t) for j in ("prices_us_polygon", "scoring")]
    s1, s2 = "D20261029T2325033AE1-sync", "D20261030T0654226FF4-sync"
    rows += [_r("event_hotspots", s1, "success", datetime(2026, 10, 30, 3, 49, tzinfo=UTC), "manual"),
             _r("event_changepoints", s1, "success", datetime(2026, 10, 30, 0, 34, tzinfo=UTC), "manual"),
             _r("conformal_envelopes", s1, "success", datetime(2026, 10, 30, 3, 36, tzinfo=UTC), "manual"),
             _r("build_serving_db", s2, "success", datetime(2026, 10, 30, 7, 2, tzinfo=UTC)),
             _r("dq_checks", None, "success", datetime(2026, 10, 25, 23, 39, tzinfo=UTC), "manual"),   # manual job run
             # still failing: latest attempt failed in a sync (after an earlier failure), and one in the monthly
             _r("fundamentals_normalize", s1, "failed", datetime(2026, 10, 30, 0, 1, tzinfo=UTC), err="timed out"),
             _r("fundamentals_normalize", n, "failed", datetime(2026, 10, 24, 20, tzinfo=UTC), err="timed out"),
             _r("openfigi_refresh", "D20261024T201944TNDT-monthly", "failed", datetime(2026, 10, 24, 22, 24, tzinfo=UTC),
                err="timed out after 60.00 min"),
             # a failure followed by a skipped attempt is still failing; one followed by partial is resolved
             _r("sec_text", s2, "failed", datetime(2026, 10, 30, 7, 0, tzinfo=UTC)),
             _r("sec_text", s2, "skipped", datetime(2026, 10, 30, 7, 10, tzinfo=UTC)),
             _r("ratios_kpis", s1, "failed", datetime(2026, 10, 29, 23, 40, tzinfo=UTC)),
             _r("ratios_kpis", s2, "partial", datetime(2026, 10, 30, 7, 5, tzinfo=UTC)),
             # a failure outside a refresh DAG run (manual job run) is not a refresh failure
             _r("ad_hoc_export", None, "failed", datetime(2026, 10, 30, 6, tzinfo=UTC), "manual")]
    return pl.DataFrame(rows)


def test_refresh_failures_use_the_latest_attempt_per_job():
    f = H.refresh_failures(_job_runs(), NOW)
    assert [x["job_id"] for x in f] == ["fundamentals_normalize", "openfigi_refresh", "sec_text"]
    fn = f[0]
    assert fn["since"].startswith("2026-10-24T20") and fn["failed_at"].startswith("2026-10-30T00") and fn["dag"] == "sync"
    assert {x["job_id"]: x["dag"] for x in f}["openfigi_refresh"] == "monthly"
    sch = {"worker_alive": True, "enabled": True, "period_days": 7, "last_success": {},
           "last_refresh": {"at": "2026-10-30T07:32:54+00:00", "dag": "sync"}}
    ld = [{"dag": "nightly", "dag_run_id": "D20261024T070000Q09X-nightly", "status": "failed", "jobs_total": 111,
           "jobs_failed": 5, "finished_at": "2026-10-25T23:38:26+00:00"}]
    al = {a["id"]: a for a in H.ops_alerts(sch, None, {"heartbeat_at": NOW.isoformat()}, ld, now=NOW,
                                            job_runs=_job_runs())}
    assert "nightly_failed" not in al                                   # the old whole-DAG alert is gone
    a = al["refresh_failed"]
    assert a["title"] == "3 refresh job(s) failing" and a["severity"] == "warn"
    for j in ("fundamentals_normalize", "openfigi_refresh", "sec_text"):
        assert j in a["detail"]
    for j in ("dq_checks", "event_hotspots", "build_serving_db", "event_changepoints", "conformal_envelopes",
              "ratios_kpis", "ad_hoc_export"):
        assert j not in a["detail"]
    # every failing job resolved -> no alert at all
    ok = _job_runs().filter(~pl.col("job_id").is_in(["fundamentals_normalize", "openfigi_refresh", "sec_text"]))
    assert H.refresh_failures(ok, NOW) == []


def test_last_refresh_is_the_most_recent_of_sync_and_scheduled_runs(tmp_data):
    t0 = datetime(2026, 10, 20, 4, tzinfo=UTC)
    runs = pl.DataFrame([_r("a", "D1-nightly", "success", t0), _r("b", "D2-weekly", "success", t0 + timedelta(days=4)),
                         _r("c", "D3-sync", "success", datetime(2026, 10, 30, 7, 28, tzinfo=UTC)),
                         _r("d", "D4-sync", "running", datetime(2026, 10, 30, 7, 35, tzinfo=UTC))])
    st = H.scheduler_status(tmp_data, {"alive": True, "scheduler_enabled": True}, None, runs, NOW)
    lr = st["last_refresh"]
    assert lr["dag"] == "sync" and lr["dag_run_id"] == "D3-sync" and lr["at"].startswith("2026-10-30T07:33")
    ids = {a["id"] for a in H.ops_alerts(st, filler={"heartbeat_at": NOW.isoformat()}, now=NOW)}
    assert "refresh_overdue" not in ids                     # nightly 10 days old, but the sync refreshed 7 min ago
    old = pl.DataFrame([_r("a", "D1-nightly", "success", t0)])
    st2 = H.scheduler_status(tmp_data, {"alive": True, "scheduler_enabled": True}, None, old, NOW)
    al = {a["id"]: a for a in H.ops_alerts(st2, filler={"heartbeat_at": NOW.isoformat()}, now=NOW)}
    assert st2["last_refresh"]["dag"] == "nightly" and "(nightly)" in al["refresh_overdue"]["detail"]
    assert H.last_refresh({}) is None


# ------------------------------------------------------------------------------------------------ 4. QA


def _suite(status, failed=0):
    return {"status": status, "failed": failed, "counts": {status: 1}}


OLD_RUN = {"run_id": "QA20261024T145229Z", "status": "failed", "finished_at": "2026-10-24T15:22:34+00:00",
           "suites": {"api": _suite("warn"), "golden": _suite("passed"), "smoke": _suite("failed", 20)},
           "issues": [{"suite": "smoke", "item": "/macro (dark)", "detail": "load 9705 ms > 8000 ms"}]}


def test_qa_state_merges_per_suite_and_old_failures_age_out():
    from app.qa.common import qa_alert, suite_views
    from app.qa.regression import merge_state

    # legacy single-run state (the 2026-10-24 file): 6 days old -> stale, info only (no error banner)
    a = qa_alert(state=OLD_RUN, now=NOW)
    assert a["severity"] == "info" and a["stale"] and a["title"] == "Regression guard: stale result"
    assert a["age_h"] > 100 and "smoke: failed (20 failed, stale" in a["detail"]
    # the same failure while it is fresh: error, as before
    fresh_now = datetime(2026, 10, 24, 16, tzinfo=UTC)
    assert qa_alert(state=OLD_RUN, now=fresh_now)["severity"] == "error"
    # a newer light run (API + golden after a sync publish, smoke left out): smoke is kept but marked superseded
    light = {"run_id": "QA20261024T171500Z", "status": "passed", "finished_at": "2026-10-24T17:20:00+00:00",
             "suites": {"api": _suite("passed"), "golden": _suite("passed")}, "issues": [],
             "skipped": {"smoke": "UI smoke ran 2.0 h ago"}, "trigger": "sync"}
    st = merge_state(OLD_RUN, light)
    vs = suite_views(st, datetime(2026, 10, 24, 18, tzinfo=UTC))
    assert vs["smoke"]["run_id"] == "QA20261024T145229Z" and vs["smoke"]["superseded_by"] == "QA20261024T171500Z"
    assert vs["smoke"]["stale"] and not vs["api"]["stale"] and vs["api"]["status"] == "passed"
    assert st["status"] == "warn" and st["run_status"] == "passed"         # a stale failure is not a current failure
    assert [i["run_id"] for i in st["issues"]] == ["QA20261024T145229Z"] and len(st["runs"]) == 1
    a = qa_alert(state=st, now=datetime(2026, 10, 24, 18, tzinfo=UTC))
    assert a["severity"] == "warn" and a["stale"] and a["superseded_by"] == "QA20261024T171500Z"
    # a newer smoke run that passes replaces the old result: no alert
    full = {"run_id": "QA20261030T074011Z", "status": "passed", "finished_at": "2026-10-30T07:47:38+00:00",
            "suites": {"api": _suite("passed"), "golden": _suite("passed"), "smoke": _suite("passed")}, "issues": []}
    st2 = merge_state(st, full)
    assert qa_alert(state=st2, now=NOW) is None and st2["status"] == "passed" and st2["issues"] == []
    assert [r["run_id"] for r in st2["runs"]] == ["QA20261030T074011Z", "QA20261024T171500Z"]
    # a skipped suite (no Chrome) never overwrites a real earlier result
    sk = {"run_id": "QA3", "status": "passed", "finished_at": NOW.isoformat(),
          "suites": {"api": _suite("passed"), "smoke": {"status": "skipped", "message": "Chrome not found"}}}
    st3 = merge_state(st2, sk)
    assert st3["suites"]["smoke"]["run_id"] == "QA20261030T074011Z" and st3["suites"]["smoke"]["last_skip"]["run_id"] == "QA3"


def test_plan_suites_light_every_time_smoke_at_most_daily(tmp_data):
    from app.qa.regression import plan_suites

    st = {"run_id": "Q1", "finished_at": (NOW - timedelta(hours=3)).isoformat(),
          "suites": {"smoke": {"status": "failed", "finished_at": (NOW - timedelta(hours=3)).isoformat()}}}
    suites, why = plan_suites(tmp_data, NOW, st, free_gb=20.0, heavy=[])
    assert suites == ("api", "golden") and "3.0 h ago" in why
    st["suites"]["smoke"]["finished_at"] = (NOW - timedelta(hours=25)).isoformat()
    assert plan_suites(tmp_data, NOW, st, free_gb=20.0, heavy=[]) == (("api", "golden", "smoke"), None)
    suites, why = plan_suites(tmp_data, NOW, st, free_gb=20.0, heavy=["build_serving_db"])
    assert suites == ("api", "golden") and "heavy job running (build_serving_db)" in why
    suites, why = plan_suites(tmp_data, NOW, st, free_gb=3.2, heavy=[])
    assert suites == ("api", "golden") and "free RAM 3.2 GB < 4 GB" in why
    assert plan_suites(tmp_data, NOW, {}, free_gb=8.0, heavy=[])[0] == ("api", "golden", "smoke")   # never ran


@pytest.mark.parametrize("mode,expect_qa", [("incremental", True), ("full", True), ("none", False)])
def test_sync_runs_the_light_guard_after_a_publish(tmp_data, monkeypatch, mode, expect_qa):
    from app.ops.sync.run import SyncRun
    from app.ops.sync.scan import ScanResult
    from app.scheduler.runner import JobOutcome, JobRegistry, RunReport

    monkeypatch.setenv("BIG360_HEAVY_SLOT", "0")
    reg = JobRegistry.load()
    calls: list[tuple[str, dict]] = []

    class Runner:
        def __init__(self):
            from app.scheduler.runner import JobRunStore

            self.store = JobRunStore(tmp_data)

        def run_jobs(self, ids, opts):
            calls.append((ids[0], dict(opts.params)))
            o = JobOutcome(ids[0], "success", None, ["R1"], 1, 0.01, 0, None, "ok")
            return RunReport(ids[0], None, opts.as_of or NOW.date(), {ids[0]: o}, datetime.now(UTC))

    def scan(settings, datasets=None, ledger=None):
        return ScanResult(datetime.now(UTC).isoformat(), "2026-10-29", [], {}, {"US": "2026-10-29"}, 0.01)

    def publish(pass_no, ran):
        return {"mode": mode, "status": "success"}

    sr = SyncRun(tmp_data, trigger="interval", registry=reg, runner=Runner(), scan_fn=scan, publish_fn=publish,
                 passes=2, pressure_fn=lambda: None, sleep_s=0.01)
    monkeypatch.setattr(sr, "_refresh_gaps", lambda: None)
    sr.run()
    qa = [c for c in calls if c[0] == "qa_regression"]
    if expect_qa:
        assert qa == [("qa_regression", {"suites": "auto", "trigger": "sync"})]      # once per sync, after the passes
        assert calls[-1][0] == "qa_regression"
        assert any(j["job"] == "qa_regression" and j["trigger"] == "post" for j in sr.status["jobs"])
    else:
        assert qa == []
