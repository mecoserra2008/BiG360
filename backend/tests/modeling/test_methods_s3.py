"""Studio v2 S3 probabilistic methods on synthetic data with known structure (no lake): Bayesian ridge / ARD, GP (with
the n cap), quantile GBM, NGBoost, hierarchical partial pooling (EB + Gibbs with R-hat), the conformal option on any
CV regressor, the out-of-fold artifact; plus export reproduction on the mini lake."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest

from app.modeling import _prob as P
from app.modeling.registry import get_method
from app.modeling.results import GuardrailError
from tests.modeling.test_methods import coef, fit, ts_data
from tests.modeling.test_methods_b import panel
from tests.modeling.test_methods_s2 import cs_data


def _hetero(n: int = 900, seed: int = 0):
    g = np.random.default_rng(seed)
    x1, x2, x3 = g.normal(size=(3, n))
    y = 0.8 * x1 + g.normal(size=n) * (0.2 + 0.8 * np.abs(x2))
    return {"y": y, "x1": x1, "x2": x2, "x3": x3}


def test_scoring_rules_and_conformal_core():
    g = np.random.default_rng(1)
    y = g.normal(size=4000)
    c = P.crps_gauss(y, np.zeros_like(y), np.ones_like(y)).mean()
    assert c == pytest.approx(1 / np.sqrt(np.pi), abs=0.01)  # E CRPS of N(0,1) at N(0,1) draws = 1 / sqrt(pi)
    pit = P.pit_gauss(y, 0.0, 1.0)
    assert P.pit_histogram(pit)["p_value"] > 0.01
    assert dict(P.coverage_gauss(y, 0.0, 1.0))[0.9] == pytest.approx(0.9, abs=0.02)
    taus = [0.05, 0.25, 0.5, 0.75, 0.95]
    from scipy import stats

    Q = np.tile(stats.norm.ppf(taus), (len(y), 1))
    assert P.crps_quantiles(y, Q, taus).mean() == pytest.approx(1 / np.sqrt(np.pi), rel=0.2)
    folds = [(np.arange(0, 1000 * k), np.arange(1000 * k, 1000 * (k + 1))) for k in range(1, 4)]
    cf = P.conformal_cv(y, np.zeros_like(y), folds, 0.1)
    assert cf["coverage"] == pytest.approx(0.9, abs=0.03) and np.isnan(cf["lower"][:1000]).all()


def test_bayes_ridge_and_ard():
    cols = _hetero()
    res = fit("bayes_ridge", ts_data(cols, "y", ["x1", "x2", "x3"]), cv={"folds": 3})
    assert coef(res, "x1")["estimate"] == pytest.approx(0.8, abs=0.08)
    assert {"pit", "calibration"} <= set(res["plots"]) and res["metrics"]["crps"] > 0
    assert next(t for t in res["tables"] if t["id"] == "prob_scores")
    assert any(t["name"].startswith("Diebold-Mariano on CRPS") for t in res["tests"])
    ard = fit("bayes_ridge", ts_data(cols, "y", ["x1", "x2", "x3"]), model="ard", cv={"folds": 3})
    rel = {r[0]: r[3] for r in next(t for t in ard["tables"] if t["id"] == "relevance")["rows"]}
    assert rel["x3"] > 10 * rel["x1"]  # irrelevant feature gets a much larger prior precision


def test_gp_beats_linear_on_nonlinear_and_refuses_large_n():
    g = np.random.default_rng(2)
    x = g.uniform(-3, 3, 300)
    y = np.sin(2 * x) + 0.2 * g.normal(size=300)
    res = fit("gp_regression", cs_data({"y": y, "x": x}, "y", ["x"]), cv={"folds": 3})
    assert res["summary"]["beats_baseline"] and res["metrics"]["crps"] < 0.5 * res["metrics"]["crps_linear"]
    assert "slice_x" in res["plots"]
    m = get_method("gp_regression")
    with pytest.raises(GuardrailError) as e:
        m.fit(cs_data({"y": y, "x": x}, "y", ["x"]), m.parse_params({"max_n": 100, "cv": {"folds": 3}}))
    assert e.value.code == "too_many_obs" and "O(n^3)" in str(e.value)


def test_quantile_gbm_heteroskedastic_and_cqr():
    cols = _hetero(1500, 3)
    res = fit("quantile_gbm", ts_data(cols, "y", ["x1", "x2"]), n_estimators=150, cv={"folds": 3, "conformal_alpha": 0.1})
    assert res["summary"]["beats_baseline"], res["summary"]["verdict"]
    assert res["metrics"]["coverage_90"] == pytest.approx(0.9, abs=0.07)
    assert res["metrics"]["conformal_coverage"] == pytest.approx(0.9, abs=0.05)
    assert {"pinball", "fan", "conformal_band"} <= set(res["plots"])


def test_ngboost_learns_the_scale():
    cols = _hetero(1200, 4)
    res = fit("ngboost", ts_data(cols, "y", ["x1", "x2"]), n_estimators=300, learning_rate=0.05, cv={"folds": 3})
    assert res["metrics"]["crps"] < res["metrics"]["crps_linear"]
    imp = {r[0]: (r[1], r[2]) for r in next(t for t in res["tables"] if t["id"] == "importance")["rows"]}
    assert imp["x2"][1] > imp["x1"][1]  # x2 drives the scale


def _groups(J: int = 12, n_small: int = 8, seed: int = 5):
    g = np.random.default_rng(seed)
    sizes = [n_small] * (J // 2) + [120] * (J - J // 2)
    G = np.concatenate([[j] * s for j, s in enumerate(sizes)])
    b = 0.6 + 0.15 * g.normal(size=J)
    x = g.normal(size=len(G))
    y = 0.1 + b[G] * x + g.normal(size=len(G))
    d = cs_data({"y": y, "x": x}, "y", ["x"])
    d.frame = d.frame.with_columns(pl.Series("sector_id", [f"S{j:02d}" for j in G]))
    return d, b


def test_hier_pool_eb_and_gibbs():
    d, b = _groups()
    eb = fit("hier_pool", d, estimator="empirical_bayes", min_group_n=5, cv={"folds": 4})
    gb = fit("hier_pool", d, estimator="gibbs", n_iter=1500, burn=500, min_group_n=5, cv={"folds": 4})
    for r in (eb, gb):
        t = next(t for t in r["tables"] if t["id"] == "pooling")
        ix = t["columns"].index
        small = [row for row in t["rows"] if row[ix("term")] == "x" and row[ix("n")] < 20]
        # small groups are shrunk toward the population mean: their own-data weight is well below 1
        assert all(row[ix("weight_on_own_data")] < 0.8 for row in small)
        assert r["evaluation"]["metrics"]["model"]["rmse"] <= r["evaluation"]["metrics"]["no_pooling"]["rmse"] + 1e-9
    assert gb["metrics"]["max_rhat"] < 1.05 and "trace" in gb["plots"] and "forest" in gb["plots"]
    assert coef(gb, "mu[x]")["estimate"] == pytest.approx(coef(eb, "mu[x]")["estimate"], abs=0.08)
    with pytest.raises(GuardrailError):
        get_method("hier_pool").fit(cs_data({"y": np.ones(60), "x": np.ones(60)}, "y", ["x"]), get_method("hier_pool").parse_params({}))


def test_conformal_option_on_existing_regressor_and_oos_artifact():
    cols = _hetero(900, 6)
    m = get_method("regularized")
    res = m.fit(ts_data(cols, "y", ["x1", "x2"]), m.parse_params({"model": "ridge", "cv": {"folds": 4, "conformal_alpha": 0.2}}))
    d = res.to_dict()
    assert d["metrics"]["conformal_coverage"] == pytest.approx(0.8, abs=0.06)
    art = res.artifacts["oos_eval"]
    assert {"date", "entity_id", "y", "prediction", "fold"} <= set(art.columns) and art.height > 300


# ------------------------------------------------------------------------------------------------ export + registry


_PANEL = {"kind": "panel", "frequency": "M", "start": "2016-01-01", "end": "2020-12-31", "entities": {"universe": "mvp_us"},
          "variables": [{"name": "ret", "source": "prices", "field": "ret"},
                        {"name": "ret_l1", "source": "prices", "field": "ret", "transforms": [{"op": "lag", "k": 1}]}]}


@pytest.mark.parametrize("study", [
    {"method": "bayes_ridge", "y": "d", "x": ["m", "d_l1"], "params": {"cv": {"folds": 3}}},
    {"method": "quantile_gbm", "y": "d", "x": ["m", "d_l1"], "params": {"cv": {"folds": 3}, "n_estimators": 30, "min_child_samples": 5}},
    {"method": "ngboost", "y": "d", "x": ["m", "d_l1"], "params": {"cv": {"folds": 3}, "n_estimators": 30}},
    {"method": "hier_pool", "y": "ret", "x": ["ret_l1"], "params": {"group": "entity_id", "estimator": "empirical_bayes", "cv": {"folds": 3}}, "dataset": _PANEL},
])
def test_s3_export_reproduces(mini_lake, tmp_path, study):
    import json
    import subprocess
    import sys

    from app.modeling import engine
    from app.modeling.export import export_script
    from app.modeling.registry import StudySpec
    from tests.modeling.test_engine import _TS

    st = StudySpec.model_validate({"dataset": _TS, **study})
    _, out = engine.submit(st, mode="sync")
    assert out["status"] == "completed", out
    env = engine.read_result(out["run_id"])
    f = tmp_path / "s.py"
    f.write_text(export_script(env, engine.run_dir(out["run_id"]), mini_lake["settings"].data_dir), encoding="utf-8")
    r = subprocess.run([sys.executable, str(f), "--data-dir", str(mini_lake["settings"].data_dir)],
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=300)
    assert r.returncode == 0, r.stderr[-2000:]
    got = json.loads(next(x for x in r.stdout.splitlines() if x.startswith("RESULT_JSON ")).removeprefix("RESULT_JSON "))
    m = env["result"]["metrics"]
    assert got["metrics"]["rmse"] == pytest.approx(m["oos_rmse"], rel=1e-5)
    assert got["crps"] == pytest.approx(m["crps"], rel=1e-5)
    assert (engine.run_dir(out["run_id"]) / "oos_eval.parquet").exists()


def test_s3_methods_registered_with_cost():
    from app.modeling.registry import all_methods

    ids = {"bayes_ridge", "gp_regression", "quantile_gbm", "ngboost", "hier_pool"}
    ms = {m.id: m for m in all_methods()}
    assert ids <= set(ms)
    for i in ids:
        m = ms[i]
        c = m.cost(10_000, 8, m.parse_params({}))
        assert 0 < c < 3600, (i, c)
        assert m.describe()["params_schema"]["properties"] and m.family == "probabilistic"


# ------------------------------------------------------------------------------------------------ guide / lint / compare / monitoring


def test_guide_rules_and_lint():
    from app.modeling.guide import Question, lint, suggest
    from app.modeling.registry import StudySpec

    s = suggest(Question.model_validate({"goal": "effect", "outcome": {"source": "prices", "field": "ret"},
                                         "driver": {"source": "tech_factors", "field": "mom"}, "horizon": 3}))
    st = s["study"]
    assert s["method"] == "dml" and st["params"]["treatment"] == "mom" and st["dataset"]["kind"] == "panel"
    assert st["dataset"]["dummies"] == ["sector"] and {"log_mcap", "value_score"} <= set(st["x"])
    assert st["dataset"]["variables"][0]["transforms"][0] == {"op": "fwd_compound", "k": 3}
    p = suggest(Question.model_validate({"goal": "predict", "outcome": {"source": "macro", "field": "fred:INDPRO"},
                                         "outcome_kind": "change", "universe": None, "horizon": 2}))
    assert p["method"] == "regularized" and p["study"]["params"]["cv"] == {"folds": 5, "scheme": "expanding", "purge": 2}
    assert {"d_10y", "d_baa", "d_vix"} <= set(p["study"]["x"])
    u = suggest(Question.model_validate({"goal": "heterogeneity", "outcome": {"source": "prices", "field": "ret"},
                                         "driver": {"source": "valuation", "field": "earnings_yield"}}))
    assert u["method"] == "hier_pool"
    base = {"kind": "panel", "frequency": "M", "entities": {"universe": "mvp_us"},
            "variables": [{"name": "y", "source": "prices", "field": "ret", "transforms": [{"op": "fwd_compound", "k": 3}]},
                          {"name": "y2", "source": "prices", "field": "ret", "transforms": [{"op": "fwd_compound", "k": 3}]},
                          {"name": "r", "source": "prices", "field": "ret"},
                          {"name": "m", "source": "tech_factors", "field": "mom", "transforms": [{"op": "standardize", "scope": "all"}]}]}
    bad = StudySpec.model_validate({"method": "lightgbm", "y": "y", "x": ["y2", "m"], "dataset": base,
                                    "params": {"cv": {"scheme": "kfold", "purge": 1}}})
    codes = {i["code"]: i["severity"] for i in lint(bad)["issues"]}
    assert codes["lookahead_regressor"] == "error" and "overlap_purge" in codes and "shuffled_cv" in codes
    assert codes["global_normalization"] == "warning"
    ok = StudySpec.model_validate({"method": "lightgbm", "y": "y", "x": ["r", "m"], "dataset": dict(base, pit_universe=False, entities={"tickers": ["AAPL"]}),
                                   "params": {"cv": {"purge": 3}}})
    li = lint(ok)
    c2 = {i["code"] for i in li["issues"]}
    assert li["ok"] and {"survivorship_list", "survivorship_universe"} <= c2 and "overlap_purge" not in c2


def test_compare_tests_equity_and_monitoring(monkeypatch):
    import pandas as pd

    from app.modeling import compare as C

    g = np.random.default_rng(9)
    dates = pd.date_range("2015-01-31", periods=60, freq="ME")
    ent = [f"E{i}" for i in range(30)]
    idx = pd.MultiIndex.from_product([dates, ent], names=["date", "entity_id"]).to_frame(index=False)
    x = g.normal(size=len(idx))
    idx["y"] = 0.3 * x + g.normal(size=len(idx))
    small = idx.assign(prediction=0.0 + 0.01 * g.normal(size=len(idx)), fold=0)
    big = idx.assign(prediction=0.3 * x, fold=0)
    frames = {"A": small, "B": big}
    monkeypatch.setattr(C, "load_oos", lambda r, settings=None: frames.get(r))
    sa = {"y": "y", "x": ["a"], "dataset": {"kind": "panel", "variables": [{"name": "y"}, {"name": "a"}]}}
    sb = {"y": "y", "x": ["a", "b"], "dataset": {"kind": "panel", "variables": [{"name": "y"}, {"name": "a"}, {"name": "b"}]}}
    out = C.compare_oos(["A", "B", "Z"], {"A": sa, "B": sb})
    t = out["tests"][0]
    assert out["benchmark_run_id"] == "A" and out["skipped"][0]["run_id"] == "Z"
    assert t["nested"] and t["dm_stat"] > 2 and t["cw_p"] < 0.01 and t["verdict"] == "beats the benchmark"
    eq = {e["run_id"]: e for e in out["equity"]}
    assert eq["B"]["kind"] == "top_minus_bottom_quintile" and eq["B"]["mean_ic"] > 0.2 and eq["B"]["cum_return"][-1] > 0
    prof = C.profile({"f": g.normal(size=5000)})
    assert C.psi(prof["f"]["shares"], C.shares(g.normal(size=5000), prof["f"]["edges"])) < 0.02
    assert C.psi(prof["f"]["shares"], C.shares(g.normal(1.0, 1.0, 5000), prof["f"]["edges"])) > 0.25
    exp = C.expanding_metric(big)
    assert exp[-1]["expanding_ic"] > 0.2 and exp[-1]["n_dates"] == 60
