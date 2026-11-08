"""Run engine: sync path, async path through the real worker + studio_run job (process executor), cancellation,
timeout, stale-run detection, reproducibility block and the exported script."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.modeling import engine
from app.modeling.export import export_script
from app.modeling.registry import StudySpec, list_presets
from app.modeling.results import GuardrailError


def ols_study(**kw) -> StudySpec:
    doc = {"name": "t2 vs rates", "method": "ols", "y": "ret", "x": ["d_rate", "ret_l1"],
           "params": {"cov_type": "HAC"},
           "dataset": {"kind": "timeseries", "frequency": "M", "start": "2016-01-01", "end": "2021-06-30",
                       "variables": [
                           {"name": "ret", "source": "prices", "field": "ret", "entities": {"tickers": ["T2"]}},
                           {"name": "ret_l1", "source": "prices", "field": "ret", "entities": {"universe": "mvp_us"},
                            "transforms": [{"op": "lag", "k": 1}]},
                           {"name": "d_rate", "source": "macro", "field": "test:D", "transforms": [{"op": "diff"}]}]}}
    doc.update(kw)
    return StudySpec.model_validate(doc)


def test_presets_are_valid_studies():
    ps = list_presets()
    assert len(ps) == 22 + 42  # + conformal_macro_ellipsoid (conformal P3) + 42 Studio v2 S3 recipes
    rec = [p for p in ps if p["id"].startswith("r_")]
    assert len(rec) == 42 and all(p["method_note"] and p["expected_outputs"] and p["question"] for p in rec)
    for p in ps:
        engine.validate_study(StudySpec.model_validate(p["study"]))


def test_sync_run_persists_envelope(mini_lake):
    code, st = engine.submit(ols_study(), mode="sync")
    assert code == 200 and st["status"] == "completed", st
    env = engine.read_result(st["run_id"])
    assert env["status"] == "completed" and env["result"]["coefficients"]
    rep = env["reproducibility"]
    assert rep["seed"] == 42 and rep["dataset_key"] and rep["snapshot_id"] == "nosnapshot"
    assert {"statsmodels", "numpy", "polars", "duckdb"} <= set(rep["versions"])
    assert (engine.run_dir(st["run_id"]) / "dataset.parquet").exists()
    assert st["metrics"]["n_obs"] == env["result"]["summary"]["n_obs"]
    # same spec again: dataset served from cache
    code2, st2 = engine.submit(ols_study(), mode="sync")
    assert engine.read_result(st2["run_id"])["dataset"]["cached"] is True


def test_guardrails_reject_before_running(mini_lake):
    bad = ols_study(x=["fwd"], dataset={"kind": "timeseries", "frequency": "M", "variables": [
        {"name": "ret", "source": "prices", "field": "ret", "entities": {"tickers": ["T2"]}},
        {"name": "fwd", "source": "prices", "field": "ret", "entities": {"tickers": ["T1"]},
         "transforms": [{"op": "fwd_sum", "k": 3}]}]})
    with pytest.raises(GuardrailError) as e:
        engine.submit(bad, mode="sync")
    assert e.value.code == "lookahead"
    with pytest.raises(GuardrailError) as e:
        engine.submit(ols_study(x=["nope"]), mode="sync")
    assert e.value.code == "unknown_column"
    with pytest.raises(GuardrailError):
        engine.submit(ols_study(method="arima", dataset={"kind": "panel", "variables": [
            {"name": "ret", "source": "prices", "field": "ret"}]}, x=[]), mode="sync")
    # failures after validation become a failed run (not an exception)
    st = engine.prepare(ols_study(dataset={"kind": "timeseries", "frequency": "M", "variables": [
        {"name": "ret", "source": "prices", "field": "ret", "entities": {"tickers": ["NOPE"]}},
        {"name": "d_rate", "source": "macro", "field": "test:D"}, {"name": "ret_l1", "source": "macro", "field": "test:M"}]}),
        "sync")
    out = engine.execute(st["run_id"])
    assert out["status"] == "failed" and out["error_code"] == "dataset"


def test_cancel_and_timeout(mini_lake):
    st = engine.prepare(ols_study(), "async")
    assert engine.cancel(st["run_id"])["status"] == "cancelled"
    assert engine.execute(st["run_id"])["status"] == "cancelled"
    st = engine.prepare(ols_study(), "async")
    flag = engine.run_dir(st["run_id"]) / "CANCEL"
    calls = {"n": 0}

    def ext() -> None:  # simulate a DELETE arriving while the run is in progress
        calls["n"] += 1
        if calls["n"] == 2:
            flag.write_text("x")

    out = engine.execute(st["run_id"], external_check=ext)
    assert out["status"] == "cancelled" and out["error_code"] == "cancelled"
    chk = engine._make_check(st["run_id"], mini_lake["settings"], time.monotonic() - 1, None)
    flag.unlink()
    with pytest.raises(engine.RunTimeout):
        chk()


def test_stale_running_run_is_failed(mini_lake):
    st = engine.prepare(ols_study(), "async")
    old = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    engine._update_status(st["run_id"], mini_lake["settings"], status="running", started_at=old, heartbeat_at=old)
    out = engine.read_status(st["run_id"])
    assert out["status"] == "failed" and out["error_code"] == "stale"


def test_estimate_and_auto_mode(mini_lake):
    small = StudySpec.model_validate({"method": "stationarity", "x": ["m"], "dataset": {
        "kind": "timeseries", "frequency": "M", "variables": [{"name": "m", "source": "macro", "field": "test:M"}]}})
    assert engine.estimate_seconds(small) < engine.SYNC_BUDGET_S
    code, st = engine.submit(small, mode="auto")
    assert code == 200 and st["mode"] == "sync" and st["status"] == "completed"
    big = StudySpec.model_validate({"method": "arima", "y": "m", "params": {"auto": True, "max_p": 4, "max_q": 4},
                                    "dataset": small.dataset.model_dump()})
    assert engine.estimate_seconds(big) > engine.SYNC_BUDGET_S


def test_async_run_through_worker_job(mini_lake):
    from app.core import jobqueue
    from app.scheduler.worker import Worker

    code, st = engine.submit(ols_study(name="async one"), mode="async")
    assert code == 202 and st["status"] == "queued"
    st2 = engine.prepare(ols_study(name="drained"), "async")  # queued without its own queue request
    reqs = jobqueue.list_requests("pending", mini_lake["settings"])
    assert any(r["job_id"] == "studio_run" and r["params"]["run_id"] == st["run_id"] for r in reqs)
    w = Worker(mini_lake["settings"], enable_schedule=False, poll_s=0.1)
    assert w.poll_queue(block=True) >= 1
    for rid in (st["run_id"], st2["run_id"]):
        out = engine.read_status(rid)
        assert out["status"] == "completed", out
    assert engine.read_result(st["run_id"])["result"]["coefficients"]


def test_exported_script_reproduces_the_fit(mini_lake, tmp_path):
    code, st = engine.submit(ols_study(), mode="sync")
    env = engine.read_result(st["run_id"])
    src = export_script(env, engine.run_dir(st["run_id"]), mini_lake["settings"].data_dir)
    f = tmp_path / "study.py"
    f.write_text(src, encoding="utf-8")
    for extra in ([], ["--from-artifact"]):
        r = subprocess.run([sys.executable, str(f), "--data-dir", str(mini_lake["settings"].data_dir), *extra],
                           capture_output=True, text=True, cwd=str(tmp_path), timeout=300)
        assert r.returncode == 0, r.stderr[-2000:]
        line = next(x for x in r.stdout.splitlines() if x.startswith("RESULT_JSON "))
        got = json.loads(line.removeprefix("RESULT_JSON "))
        for c in env["result"]["coefficients"]:
            assert got["params"][c["term"]] == pytest.approx(c["estimate"], rel=1e-6, abs=1e-9)
            assert got["bse"][c["term"]] == pytest.approx(c["std_error"], rel=1e-6, abs=1e-9)
    # standalone: no unconditional backend import (only guarded / lazy ones inside functions)
    assert not [ln for ln in src.splitlines() if ln.startswith(("from app", "import app"))]


@pytest.mark.parametrize("study", [
    {"method": "fama_macbeth", "y": "ret_next", "x": ["v"], "params": {"min_obs": 4}, "dataset": {
        "kind": "panel", "frequency": "M", "start": "2016-01-01", "end": "2021-06-30", "entities": {"universe": "mvp_us"},
        "variables": [{"name": "ret_next", "source": "prices", "field": "ret", "transforms": [{"op": "lead", "k": 1}]},
                      {"name": "v", "source": "factor_scores", "field": "value", "transforms": [{"op": "cs_zscore"}]}]}},
    {"method": "var", "x": ["m", "d"], "params": {"lags": 1, "irf_periods": 6}, "dataset": {
        "kind": "timeseries", "frequency": "M", "start": "2015-06-01", "end": "2021-06-30", "macro_alignment": "period",
        "variables": [{"name": "m", "source": "macro", "field": "test:M", "transforms": [{"op": "diff"}]},
                      {"name": "d", "source": "macro", "field": "test:D", "agg": "mean", "transforms": [{"op": "diff"}]}]}},
    {"method": "did", "y": "ret", "params": {"design": "twfe", "treatment": {"mode": "attribute", "attribute": "sector_id",
                                                                             "values": ["FINS"], "date": "2019-01-01"}},
     "dataset": {"kind": "panel", "frequency": "M", "start": "2017-01-01", "end": "2020-12-31", "entities": {"universe": "mvp_us"},
                 "variables": [{"name": "ret", "source": "prices", "field": "ret"}]}},
])
def test_phase_b_export_reproduces(mini_lake, tmp_path, study):
    st = StudySpec.model_validate(study)
    code, out = engine.submit(st, mode="sync")
    assert out["status"] == "completed", out
    env = engine.read_result(out["run_id"])
    f = tmp_path / "s.py"
    f.write_text(export_script(env, engine.run_dir(out["run_id"]), mini_lake["settings"].data_dir), encoding="utf-8")
    r = subprocess.run([sys.executable, str(f), "--data-dir", str(mini_lake["settings"].data_dir)],
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(next(x for x in r.stdout.splitlines() if x.startswith("RESULT_JSON ")).removeprefix("RESULT_JSON "))
    res = env["result"]
    if st.method == "did":
        assert got["att"] == pytest.approx(res["metrics"]["att"], rel=1e-6)
    else:
        for c in res["coefficients"]:
            assert got["params"][c["term"]] == pytest.approx(c["estimate"], rel=1e-6, abs=1e-12)


_TS = {"kind": "timeseries", "frequency": "M", "start": "2015-06-01", "end": "2021-06-30", "macro_alignment": "period",
       "variables": [{"name": "m", "source": "macro", "field": "test:M", "transforms": [{"op": "diff"}]},
                     {"name": "d", "source": "macro", "field": "test:D", "agg": "mean", "transforms": [{"op": "diff"}]},
                     {"name": "d_l1", "source": "macro", "field": "test:D", "agg": "mean", "transforms": [{"op": "diff"}, {"op": "lag", "k": 1}]}]}


@pytest.mark.parametrize("study", [
    {"method": "regularized", "y": "d", "x": ["m", "d_l1"], "params": {"model": "ridge", "cv": {"folds": 3}}, "dataset": _TS},
    {"method": "leaderboard", "y": "d", "x": ["m", "d_l1"], "params": {"cv": {"folds": 3}, "candidates": [
        {"name": "mean", "method": "mean"}, {"name": "ols", "method": "ols"}]}, "dataset": _TS},
    {"method": "gmm", "x": ["m", "d"], "params": {"k_min": 1, "k_max": 3}, "dataset": _TS},
])
def test_phase_c_export_reproduces(mini_lake, tmp_path, study):
    st = StudySpec.model_validate(study)
    code, out = engine.submit(st, mode="sync")
    assert out["status"] == "completed", out
    env = engine.read_result(out["run_id"])
    f = tmp_path / "s.py"
    f.write_text(export_script(env, engine.run_dir(out["run_id"]), mini_lake["settings"].data_dir), encoding="utf-8")
    r = subprocess.run([sys.executable, str(f), "--data-dir", str(mini_lake["settings"].data_dir)],
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(next(x for x in r.stdout.splitlines() if x.startswith("RESULT_JSON ")).removeprefix("RESULT_JSON "))
    res = env["result"]
    if st.method == "regularized":
        for c in res["coefficients"]:
            assert got["params"][c["term"]] == pytest.approx(c["estimate"], rel=1e-6, abs=1e-12)
    elif st.method == "leaderboard":
        tbl = next(t for t in res["tables"] if t["id"] == "leaderboard")
        for row in tbl["rows"]:
            assert got["metrics"][row[1]]["rmse"] == pytest.approx(row[3], rel=1e-9)
    else:
        assert got["k"] == res["summary"]["k"]


def test_scheduled_studies_and_drift(mini_lake):
    from types import SimpleNamespace

    from app.modeling import scheduled, store

    st = store.create_study("sched", StudySpec.model_validate(
        {"method": "ols", "y": "d", "x": ["m"], "dataset": _TS, "params": {"rolling_eval": False}}))
    store.set_schedule(st["study_id"], "weekly", 0.1)
    assert store.get_study(st["study_id"])["alert_id"]
    ctx = SimpleNamespace(settings=mini_lake["settings"], params={}, check_cancelled=lambda: None)
    r1 = scheduled.run(ctx)
    assert r1.entities_ok == 1
    assert scheduled.run(ctx).entities_ok == 0  # not due again within the week
    store.update_study(st["study_id"], {"last_scheduled_at": None})
    assert scheduled.run(ctx).entities_ok == 1
    runs = store.list_runs(st["study_id"])
    assert len(runs) == 2 and all(r["status"] == "completed" for r in runs)
    old = {"result": {"metrics": {"r2": 0.5, "oos_rmse": 1.0}, "coefficients": [{"term": "x", "estimate": 1.0, "std_error": 0.1}]}}
    new = {"result": {"metrics": {"r2": 0.3, "oos_rmse": 1.05}, "coefficients": [{"term": "x", "estimate": 2.0, "std_error": 0.1}]}}
    d = scheduled.detect_drift(old, new, 0.2)
    assert d["degraded"] and any("r2" in i for i in d["issues"]) and d["coef_shifts"][0]["term"] == "x"
    assert not scheduled.detect_drift(old, old, 0.2)["degraded"]
