"""Studio v2 S2 methods on synthetic data with known effects (no lake): DML, propensity scores, RD, coefficient
stability; LightGBM, Optuna, stacking, learning-to-rank; MLP, GRU, autoencoder (with the linear-baseline DM test)."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.modeling.registry import FitData, get_method
from tests.modeling.test_methods import coef, fit, ts_data
from tests.modeling.test_methods_b import panel


def cs_data(cols: dict[str, np.ndarray], y: str | None, x: list[str]) -> FitData:
    n = len(next(iter(cols.values())))
    f = pl.DataFrame({"date": [date(2024, 12, 31)] * n, "entity_id": [f"E{i}" for i in range(n)],
                      "entity_label": [f"E{i}" for i in range(n)], **{k: np.asarray(v, float) for k, v in cols.items()}})
    return FitData(f, {"kind": "cross_section", "frequency": "M", "variables": list(cols), "lead_variables": []}, y, x, None, 42)


def _conf(n: int, seed: int = 0):
    g = np.random.default_rng(seed)
    X = g.normal(size=(n, 4))
    return g, X, {f"w{j}": X[:, j] for j in range(4)}


# ------------------------------------------------------------------------------------------------ controls & causal


def test_dml_plr_and_irm_recover_effects():
    g, X, cols = _conf(1500)
    d = np.sin(2 * X[:, 0]) + 0.5 * X[:, 1] + g.normal(scale=0.7, size=1500)
    y = 0.7 * d + np.cos(2 * X[:, 0]) + X[:, 1] ** 2 + g.normal(scale=0.5, size=1500)
    res = fit("dml", cs_data({"y": y, "d": d, **cols}, "y", list(cols)), treatment="d", learner_y="lgbm", learner_d="lgbm")
    assert coef(res, "θ (PLR)")["estimate"] == pytest.approx(0.7, abs=0.08)
    assert res["metrics"]["p_value"] < 1e-6 and res["plots"]["residuals"]["kind"] == "scatter"
    D = (g.uniform(size=1500) < 1 / (1 + np.exp(-1.5 * X[:, 0]))).astype(float)
    y2 = 1.0 * D + 2 * X[:, 0] + g.normal(scale=0.5, size=1500)
    r2 = fit("dml", cs_data({"y": y2, "d": D, **cols}, "y", list(cols)), treatment="d", model="irm", n_rep=2)
    assert coef(r2, "ATE (IRM)")["estimate"] == pytest.approx(1.0, abs=0.15)
    assert coef(r2, "naive OLS with controls")["estimate"] == pytest.approx(1.0, abs=0.15)  # linear DGP: OLS fine too
    assert "overlap" in r2["plots"] and next(t for t in r2["tables"] if t["id"] == "repetitions")


def test_propensity_ipw_aipw_matching_and_balance():
    g, X, cols = _conf(2000, 3)
    D = (g.uniform(size=2000) < 1 / (1 + np.exp(-(X[:, 0] - 0.5 * X[:, 1])))).astype(float)
    y = 1.0 * D + 1.5 * X[:, 0] + X[:, 1] + g.normal(scale=0.5, size=2000)
    res = fit("propensity", cs_data({"y": y, "t": D, **cols}, "y", list(cols)), treatment="t")
    for k in ("ATE (IPW)", "ATE (AIPW)", "ATT (NN matching)"):
        assert coef(res, k)["estimate"] == pytest.approx(1.0, abs=0.25), k
    assert coef(res, "difference in means (unadjusted)")["estimate"] > 1.6  # confounded
    m = res["metrics"]
    assert m["max_smd_weighted"] < 0.1 < m["max_smd_raw"]
    bal = next(t for t in res["tables"] if t["id"] == "balance")
    assert bal["columns"][3] == "smd_raw" and len(bal["rows"]) == 4
    assert {"overlap", "love"} <= set(res["plots"])


def test_rdd_sharp_and_fuzzy():
    g = np.random.default_rng(4)
    n = 3000
    x = g.uniform(-1, 1, n)
    w = g.normal(size=n)
    y = 0.5 + 0.8 * x + 1.5 * (x >= 0) + g.normal(scale=0.3, size=n)
    res = fit("rdd", cs_data({"y": y, "r": x, "w": w}, "y", ["w"]), running="r")
    assert res["metrics"]["estimate"] == pytest.approx(1.5, abs=0.12) and 0.05 < res["metrics"]["h"] < 1
    assert res["metrics"]["estimate_robust"] == pytest.approx(1.5, abs=0.2)
    mc = next(t for t in res["tests"] if t["name"].startswith("McCrary"))
    assert mc["p_value"] > 0.01
    assert {"rd_plot", "bandwidth_sensitivity"} <= set(res["plots"])
    assert next(t for t in res["tables"] if t["id"] == "covariate_continuity")["rows"][0][3] > 0.001  # calibrated: 5% rejections on noise (checked by simulation)
    D = (g.uniform(size=n) < 0.2 + 0.6 * (x >= 0)).astype(float)
    y2 = 2.0 * D + x + g.normal(scale=0.3, size=n)
    f = fit("rdd", cs_data({"y": y2, "r": x, "t": D}, "y", []), running="r", design="fuzzy", treatment="t", bandwidth="cct")
    assert f["metrics"]["estimate"] == pytest.approx(2.0, abs=0.5) and f["fit_stats"]["first_stage_F"] > 10


def test_coef_stability_oster():
    g = np.random.default_rng(5)
    n = 2000
    w1, w2 = g.normal(size=n), g.normal(size=n)
    d = 0.8 * w1 + g.normal(size=n)
    y = 0.5 * d + 1.0 * w1 + 0.3 * w2 + g.normal(size=n)
    res = fit("coef_stability", cs_data({"y": y, "d": d, "w1": w1, "w2": w2}, "y", ["w1", "w2"]), treatment="d")
    m = res["metrics"]
    assert m["beta_full"] == pytest.approx(0.5, abs=0.06) and m["beta_short"] > 0.8
    st = next(t for t in res["tables"] if t["id"] == "stability")
    assert len(st["rows"]) == 3 and res["plots"]["beta_path"]["kind"] == "errorbar"
    assert np.isfinite(m["delta_for_zero"]) and m["beta_star"] < m["beta_full"]


# ------------------------------------------------------------------------------------------------ advanced ML


def _nonlin(n: int = 800, seed: int = 6, linear: bool = False):
    g = np.random.default_rng(seed)
    X = g.normal(size=(n, 3))
    y = (X[:, 0] + 0.5 * X[:, 1]) if linear else (2 * np.sin(2 * X[:, 0]) + X[:, 1])
    y = y + g.normal(scale=0.3, size=n)
    return {"y": y, **{f"x{j}": X[:, j] for j in range(3)}}, [f"x{j}" for j in range(3)]


def test_lightgbm_beats_linear_only_when_it_should():
    cols, xs = _nonlin()
    res = fit("lightgbm", ts_data(cols, "y", xs), cv={"folds": 3}, shap=True, shap_sample=200)
    assert res["metrics"]["oos_r2_oos"] > 0.7 and res["summary"]["beats_linear"] is True
    assert {"learning_curve", "shap_beeswarm", "importance"} <= set(res["plots"])
    assert any(t["name"] == "Diebold-Mariano: LightGBM vs linear baseline" for t in res["tests"])
    cols, xs = _nonlin(linear=True)
    lin = fit("lightgbm", ts_data(cols, "y", xs), cv={"folds": 3}, shap=False)
    assert lin["summary"]["beats_linear"] is False and any(w["code"].startswith("no_gain") for w in lin["warnings"])


def test_optuna_tunes_on_inner_cv_and_reports_holdout():
    cols, xs = _nonlin(600)
    res = fit("optuna_tune", ts_data(cols, "y", xs), model="lgbm", n_trials=6, timeout_s=60, cv={"folds": 3})
    ids = {t["id"] for t in res["tables"]}
    assert {"holdout", "best_params", "trials"} <= ids and "optimization_history" in res["plots"]
    assert 1 <= res["fit_stats"]["n_trials_completed"] <= 6 and res["summary"]["n_holdout"] >= 100
    assert res["metrics"]["holdout_rmse"] < res["metrics"]["linear_holdout_rmse"]


def test_stacking_dm_against_best_base():
    cols, xs = _nonlin(600)
    res = fit("stacking", ts_data(cols, "y", xs), bases=["mean", "ols", "lgbm"], cv={"folds": 3})
    lb = next(t for t in res["tables"] if t["id"] == "leaderboard")
    assert lb["rows"][-1][0] == "mean" and res["summary"]["best_base"] == "lgbm"
    assert any(t["name"].startswith("Diebold-Mariano: stack vs best base") for t in res["tests"])
    w = {r[0]: r[1] for r in next(t for t in res["tables"] if t["id"] == "meta_weights")["rows"]}
    assert w["lgbm"] > w["mean"]


def test_ltr_ranks_by_date():
    g = np.random.default_rng(7)
    N, T = 40, 48
    X = g.normal(size=(N * T, 3))
    y = np.tanh(2 * X[:, 0]) + 0.5 * (X[:, 1] > 0) + g.normal(scale=0.5, size=N * T)
    res = fit("ltr", panel({"y": y, **{f"x{j}": X[:, j] for j in range(3)}}, N, T, "y", ["x0", "x1", "x2"]), cv={"folds": 3})
    assert res["metrics"]["oos_rank_ic"] > 0.3 and res["metrics"]["oos_ndcg"] > 0.5
    rk = next(t for t in res["tables"] if t["id"] == "ranking")
    assert [r[0] for r in rk["rows"]] == ["lambdarank", "pointwise_lgbm", "ols"]
    assert {"ic_by_date", "cum_spread"} <= set(res["plots"])


# ------------------------------------------------------------------------------------------------ neural networks


def test_mlp_vs_linear_baseline_is_mandatory_and_honest():
    cols, xs = _nonlin(800)
    res = fit("mlp", ts_data(cols, "y", xs), cv={"folds": 3}, n_seeds=2, max_epochs=150, lr=3e-3)
    assert res["metrics"]["oos_r2_oos"] > 0.6 and res["summary"]["beats_linear"] is True
    assert "linear" in res["evaluation"]["metrics"] and res["plots"]["training_curve"]["series"][1]["name"] == "validation"
    again = fit("mlp", ts_data(cols, "y", xs), cv={"folds": 3}, n_seeds=2, max_epochs=150, lr=3e-3)
    assert again["metrics"]["oos_rmse"] == res["metrics"]["oos_rmse"]  # deterministic seeds
    cols, xs = _nonlin(400, linear=True)
    lin = fit("mlp", ts_data(cols, "y", xs), cv={"folds": 3}, n_seeds=1, max_epochs=60)
    assert lin["summary"]["beats_linear"] is False and any(w["code"] == "no_gain_vs_linear_baseline" for w in lin["warnings"])


def test_gru_on_time_series_and_panel():
    g = np.random.default_rng(8)
    n = 500
    x = g.normal(size=n)
    y = np.zeros(n)
    for t in range(2, n):
        y[t] = 0.6 * y[t - 1] + np.tanh(2 * x[t - 1]) + g.normal(scale=0.2)
    res = fit("rnn", ts_data({"y": y, "x_l1": np.r_[np.nan, x[:-1]]}, "y", ["x_l1"]), window=4, cv={"folds": 3},
              n_seeds=1, max_epochs=60)
    assert res["metrics"]["oos_r2_oos"] > 0.5 and res["summary"]["y_lag_offset"] == 1
    assert "oos_fit" in res["plots"] and any(t["name"].startswith("Diebold-Mariano: GRU") for t in res["tests"])
    N_, T = 12, 40
    X = g.normal(size=N_ * T)
    yp = np.tanh(X) + g.normal(scale=0.3, size=N_ * T)
    rp = fit("rnn", panel({"y": yp, "x": X}, N_, T, "y", ["x"]), cell="lstm", window=3, include_y_lags=False,
             cv={"folds": 3}, n_seeds=1, max_epochs=40)
    assert rp["summary"]["n_obs"] == N_ * (T - 2) and rp["metrics"]["oos_r2_oos"] > 0.3


def test_autoencoder_vs_pca():
    g = np.random.default_rng(9)
    n = 1200
    z = g.normal(size=n)
    X = np.column_stack([z, z ** 2, np.sin(2 * z), np.tanh(z), -z] ) + g.normal(scale=0.05, size=(n, 5))
    cols = {f"f{j}": X[:, j] for j in range(5)}
    res = fit("autoencoder", ts_data(cols, None, list(cols)), n_factors=1, n_seeds=1, max_epochs=150, lr=3e-3)
    m = res["metrics"]
    assert m["holdout_r2"] > m["pca_holdout_r2"] and res["summary"]["beats_pca"] is True
    assert res["plots"]["loadings_heatmap"]["kind"] == "heatmap" and "factors" in res["plots"]


# ------------------------------------------------------------------------------------------------ export + engine


_PANEL = {"kind": "panel", "frequency": "M", "start": "2016-01-01", "end": "2020-12-31", "entities": {"universe": "mvp_us"},
          "variables": [{"name": "ret", "source": "prices", "field": "ret"},
                        {"name": "ret_l1", "source": "prices", "field": "ret", "transforms": [{"op": "lag", "k": 1}]},
                        {"name": "ret_l2", "source": "prices", "field": "ret", "transforms": [{"op": "lag", "k": 2}]}]}


@pytest.mark.parametrize("study", [
    {"method": "dml", "y": "d", "x": ["d_l1"], "params": {"treatment": "m", "crossfit": {"folds": 3}}},
    {"method": "coef_stability", "y": "d", "x": ["d_l1"], "params": {"treatment": "m"}},
    {"method": "lightgbm", "y": "d", "x": ["m", "d_l1"], "params": {"cv": {"folds": 3}, "n_estimators": 50, "shap": False}},
    {"method": "mlp", "y": "ret", "x": ["ret_l1", "ret_l2"], "params": {"cv": {"folds": 3}, "max_epochs": 5, "n_seeds": 1}, "dataset": _PANEL},
    {"method": "rnn", "y": "ret", "x": ["ret_l1"], "params": {"cv": {"folds": 3}, "max_epochs": 5, "n_seeds": 1, "window": 3}, "dataset": _PANEL},
])
def test_s2_export_reproduces(mini_lake, tmp_path, study):
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
    if st.method == "dml":
        assert got["theta"] == pytest.approx(m["theta"], rel=1e-6)
    elif st.method == "coef_stability":
        assert got["beta_star"] == pytest.approx(m["beta_star"], rel=1e-6)
    else:
        assert got["metrics"]["rmse"] == pytest.approx(m["oos_rmse"], rel=1e-5)


def test_s2_methods_registered_with_cost():
    from app.modeling.registry import all_methods

    ids = {"dml", "propensity", "rdd", "coef_stability", "lightgbm", "optuna_tune", "stacking", "ltr", "mlp", "rnn", "autoencoder"}
    ms = {m.id: m for m in all_methods()}
    assert ids <= set(ms)
    req = {"dml": {"treatment": "d"}, "propensity": {"treatment": "d"}, "rdd": {"running": "r"}, "coef_stability": {"treatment": "d"}}
    for i in ids:
        m = ms[i]
        c = m.cost(10_000, 8, m.parse_params(req.get(i, {})))
        assert 0 < c < 3600, (i, c)
        assert m.describe()["params_schema"]["properties"]
