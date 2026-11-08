"""docs/STUDIO_V2_PLAN.md S1: learned estimates, preview-then-full, heavy extras opt-in, the warm studio lane
(end to end with a real spawned worker) and its worker fallback / kill switch."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.modeling import engine, estimator, lane
from app.modeling.registry import StudySpec, get_method


def ts_study(**kw) -> StudySpec:
    doc = {"name": "daily ret", "method": "ols", "y": "ret", "x": ["ret_l1"],
           "dataset": {"kind": "timeseries", "frequency": "D", "start": "2015-02-01", "end": "2021-12-31",
                       "variables": [
                           {"name": "ret", "source": "prices", "field": "ret", "entities": {"tickers": ["T2"]}},
                           {"name": "ret_l1", "source": "prices", "field": "ret", "entities": {"tickers": ["T3"]},
                            "transforms": [{"op": "lag", "k": 1}]}]}}
    doc.update(kw)
    return StudySpec.model_validate(doc)


def _sample(i: int, method: str, prior: float, actual: float, **kw) -> dict:
    return {"run_id": f"R{i}", "created_at": f"2026-11-01T00:00:{i:02d}", "method": method, "path": "sync",
            "rows": 400 + i, "n_x": 2, "prior_fit": prior, "prior_build": 0.5, "extras_s": 0.0, "cached": True,
            "build_s": 0.05, "fit_s": actual, "run_s": actual + 0.1, "wait_s": 0.0, "e2e_s": actual + 0.1,
            "estimate_s": prior, **kw}


def test_learned_estimator_corrects_a_biased_prior():
    rows = [_sample(i, "arima", 2.0, 6.0 + 0.1 * (i % 3)) for i in range(12)] + \
           [_sample(20 + i, "ols", 1.0, 0.2) for i in range(12)]
    m = estimator.fit_studio(rows)
    arima = estimator.predict_fit(m, "arima", 405, 2, 2.0, 0.0)
    ols = estimator.predict_fit(m, "ols", 405, 2, 1.0, 0.0)
    assert 4.5 < arima < 8.0 and 0.1 < ols < 0.4          # per-method correction of the static cost()
    new = estimator.predict_fit(m, "brand_new_method", 405, 2, 1.0, 0.0)
    assert 0.1 < new < 3.0                                  # unseen method: its own cost() still counts
    same = estimator.predict_fit(estimator.fit_studio(rows + [_sample(40, "ols", 1.0, 3.0, spec_hash="H")] * 3),
                                 "ols", 405, 2, 1.0, 0.0, "H")
    assert same > 1.5                                       # the same spec ran before: its observed times dominate
    assert estimator.fit_studio([])["global"] is None and estimator.predict_fit({}, "x", 10, 1, 0.7, 0.3) == 1.0


def test_extras_and_preview_params():
    p = get_method("tree_ensemble").parse_params({})
    assert p.shap is False                                  # S1.4: opt-in
    ex = estimator.extras("tree_ensemble", get_method("tree_ensemble").parse_params({"shap": True}), 5000, 4)
    assert ex[0]["name"] == "shap" and ex[0]["enabled"] and ex[0]["cost_s"] > 0
    assert get_method("synthetic_control").parse_params({"treated": "A", "treatment_date": "2020-01-01"}).placebo is False
    q = engine.preview_params(get_method("tree_ensemble").parse_params({"shap": True}))
    assert q.shap is False
    did = get_method("did").parse_params({"design": "staggered", "treatment": {"mode": "attribute", "attribute": "region",
                                                                               "values": ["US"], "date": "2022-01-01"}})
    assert engine.preview_params(did).bootstrap == 49
    from app.backtest.catalog import PRESETS
    from app.backtest.spec import BacktestSpec

    assert BacktestSpec.model_validate({"name": "x", "signal": {"variables": [
        {"name": "v", "source": "factor_scores", "field": "value"}]}}).robustness.permutations == 0
    assert all(p["spec"]["robustness"]["permutations"] == 20 for p in PRESETS)


def test_entity_counts_and_estimate_breakdown(mini_lake):
    s = mini_lake["settings"]
    estimator.reset_cache()
    assert estimator.entity_count({"universe": "mvp_us"}, 3000, s) == 5     # current members (one left in 2019)
    assert estimator.entity_count({"universe": "mvp_us", "sectors": ["TECH"]}, 3000, s) == 3
    assert estimator.entity_count({"tickers": ["T1", "T2"]}, 3000, s) == 2
    assert estimator.entity_count({"universe": "mvp_us"}, 2, s) == 2       # capped like the builder
    est = estimator.estimate_studio(ts_study(), s, path="worker")
    assert est["path"] == "worker" and est["overhead_s"] == estimator.OVERHEAD_PRIOR["worker"]
    assert est["total_s"] == pytest.approx(est["queue_s"] + est["overhead_s"] + est["run_s"], abs=0.02)


def test_preview_then_full(mini_lake):
    s = mini_lake["settings"]
    study = ts_study()
    st = engine.prepare(study, "async", estimate=9.0, estimate_detail={"fit_s": 8.0, "run_s": 9.0}, lane="lane")
    out = engine.execute(st["run_id"], s, preview=True)
    assert out["status"] == "completed" and out["stage"] is None
    pv = engine.read_preview(st["run_id"], s)
    full = engine.read_result(st["run_id"], s)
    assert pv["preview"]["by"] == "dates" and 0 < pv["preview"]["n_rows"] < pv["preview"]["n_rows_full"]
    assert pv["result"]["summary"]["n_obs"] < full["result"]["summary"]["n_obs"]
    assert out["preview"]["status"] == "completed" and full["reproducibility"]["timings"]["preview_s"] > 0
    # short fits get no preview
    st2 = engine.prepare(study, "async", estimate_detail={"fit_s": 0.2, "run_s": 0.5})
    engine.execute(st2["run_id"], s, preview=True)
    assert engine.read_preview(st2["run_id"], s) is None


def test_lane_unavailable_falls_back_to_the_worker(mini_lake, monkeypatch):
    from app.core import jobqueue

    s = mini_lake["settings"]
    assert lane.available(s) is False
    code, st = engine.submit(ts_study(name="fallback"), mode="async")
    assert code == 202 and st["lane"] == "worker" and st["estimate"]["path"] == "worker"
    reqs = jobqueue.list_requests("pending", s)
    assert any(r["params"].get("run_id") == st["run_id"] for r in reqs)
    # a lane run whose lane went away is withdrawn and moved to the worker
    st2 = engine.prepare(ts_study(name="orphan"), "async", lane="lane")
    lane.submit("studio", st2["run_id"], 1.0, s)
    assert engine.queued_runs(s).count(st2["run_id"]) == 0          # the worker drain skips lane runs
    old = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    st2 = engine._update_status(st2["run_id"], s, created_at=old)
    moved = engine.requeue_if_stale(st2, s)
    assert moved["lane"] == "worker" and moved["queue_request_id"] and not lane._inbox(s)
    # kill switch
    monkeypatch.setenv("BIG360_STUDIO_LANE", "0")
    assert lane.available(s) is False


def test_lane_end_to_end(mini_lake, monkeypatch):
    """A real lane (one spawned warm worker): routing, queue info, preview + full result, cancel, heartbeat."""
    s = mini_lake["settings"]
    monkeypatch.setenv("BIG360_RAM_BUDGET", "0")
    ln = lane.Lane(s, pool=1)
    th = threading.Thread(target=ln.run_forever, daemon=True)
    th.start()
    try:
        t0 = time.time()
        while time.time() - t0 < 90:
            st = lane.status(s, max_age_s=0) or {}
            if st.get("alive") and all(w["ready"] for w in st.get("workers") or [{}]):
                break
            time.sleep(0.2)
        assert lane.available(s), lane.status(s, max_age_s=0)
        code, st = engine.submit(ts_study(name="on the lane"), mode="async")
        assert code == 202 and st["lane"] == "lane" and st["estimate"]["path"] == "lane"
        info = lane.live_fields(engine.read_status(st["run_id"], s), s)
        assert info["lane"] == "lane" and info["eta_s"] is not None
        t1 = time.time()
        while time.time() - t1 < 60 and engine.read_status(st["run_id"], s)["status"] not in engine.TERMINAL:
            time.sleep(0.1)
        done = engine.read_status(st["run_id"], s)
        assert done["status"] == "completed", done
        assert done["pid"] != __import__("os").getpid()         # ran in the lane's worker, not here
        # cancel while queued on the lane
        st3 = engine.prepare(ts_study(name="cancel me"), "async", lane="lane")
        engine.cancel(st3["run_id"], s)
        lane.submit("studio", st3["run_id"], 1.0, s)
        time.sleep(0.5)
        assert engine.read_status(st3["run_id"], s)["status"] == "cancelled" and not lane._inbox(s)
        hb = json.loads((lane.lane_dir(s) / "lane.json").read_text(encoding="utf-8"))
        assert hb["pool"] == 1 and hb["done"] >= 1 and hb["priority"] in ("normal", "below_normal")
    finally:
        ln.stop_event.set()
        th.join(40)
    assert not th.is_alive() and lane.available(s) is False
