"""F16 / F17 on the REAL jobs.yaml graph: analytics or ref_universe failures no longer block publishing market data."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import yaml

from app.core.config import get_settings
from app.scheduler.runner import JobRegistry, JobRunner, RunOptions

FAKE = "tests.platform.fake_jobs"
AS_OF = date(2026, 10, 16)


def nightly_registry(failing: set[str]) -> JobRegistry:
    doc = yaml.safe_load((get_settings().contracts_dir / "jobs.yaml").read_text(encoding="utf-8"))
    jobs = []
    for j in doc["jobs"]:
        if j["dag"] != "nightly":
            continue
        jobs.append({"id": j["id"], "dag": "nightly", "module": f"{FAKE}:{'fail' if j['id'] in failing else 'ok'}",
                     "depends_on": [d for d in j.get("depends_on", [])],
                     "soft_depends_on": list(j.get("soft_depends_on", []) or []), "executor": "thread", "writes": []})
    ids = {j["id"] for j in jobs}
    for j in jobs:  # cross-DAG deps are ignored by the runner; keep the registry self-contained
        j["depends_on"] = [d for d in j["depends_on"] if d in ids]
        j["soft_depends_on"] = [d for d in j["soft_depends_on"] if d in ids]
    return JobRegistry({"version": 1, "timezone": "America/New_York", "dags": {"nightly": {"cron": None}},
                        "defaults": {"retries": 0, "retry_backoff_s": 0, "timeout_min": 1, "on_upstream_failure": "skip"},
                        "jobs": jobs})


def test_build_serving_db_has_only_soft_dependencies():
    reg = JobRegistry.load()
    b = reg.jobs["build_serving_db"]
    assert set(b.depends_on) >= {"dq_checks", "prices_finalize", "fx_rates"}
    assert set(b.depends_on) == set(b.soft_depends_on)
    dq = reg.jobs["dq_checks"]
    assert set(dq.depends_on) == set(dq.soft_depends_on)
    for j in ("prices_us_polygon", "prices_global_yf", "gdelt_stream", "news_ingest", "identity_resolve"):
        assert "ref_universe" in reg.jobs[j].soft_depends_on, j
    for j in ("prices_global_yf", "gdelt_stream", "earnings_dates_fetch", "yf_fundamentals"):
        assert reg.jobs[j].executor == "process", j          # F18: killable on timeout
    assert reg.jobs["data_gaps_detect"].dag == "nightly"


def test_analytics_and_ref_universe_failures_still_publish(tmp_data):
    reg = nightly_registry({"scoring", "confluence_zones", "ref_universe", "opendart_filings"})
    rep = JobRunner(reg, tmp_data).run_dag("nightly", RunOptions(as_of=AS_OF, wait_for_dags=False, backoff_scale=0,
                                                                  max_workers=8))
    o = rep.outcomes
    assert o["scoring"].status == "failed" and o["confluence_zones"].status == "failed"
    for j in ("ml_features", "ml_predict", "composite_ml_blend"):           # true analytics dependents still skip
        assert o[j].reason == "upstream_failed", j
    for j in ("prices_us_polygon", "prices_global_yf", "prices_finalize", "identity_resolve", "fundamentals_normalize",
              "dq_checks", "build_serving_db", "alerts_eval", "data_gaps_detect"):
        assert o[j].status == "success", (j, o[j].status, o[j].reason, o[j].message)


def test_dq_failure_in_the_same_dag_run_blocks_publish(tmp_data):
    from app.api.serving import _dq_failed_in_dag
    from app.core.schema import get_contract
    from app.core.storage import write_table

    c = get_contract("job_runs")
    now = datetime.now(UTC)
    rows = [{"run_id": "R1", "year_month": now.strftime("%Y-%m"), "job_id": "dq_checks", "dag_run_id": "D1-nightly",
             "trigger": "schedule", "status": "failed", "attempt": 1, "started_at": now}]
    write_table(pl.DataFrame([{k: r.get(k) for k in c.column_names} for r in rows], schema=c.polars_schema()), "job_runs",
                settings=tmp_data)

    class Ctx:
        settings = tmp_data
        dag_run_id = "D1-nightly"

    assert _dq_failed_in_dag(Ctx())
    Ctx.dag_run_id = "D2-nightly"                                        # dq skipped / not run in this dag run
    assert not _dq_failed_in_dag(Ctx())
