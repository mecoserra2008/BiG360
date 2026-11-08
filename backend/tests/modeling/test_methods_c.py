"""Phase C methods on synthetic data with known structure + the factor_ls dataset source."""

from __future__ import annotations

import numpy as np
import pytest

from app.modeling import _ml as M
from app.modeling.datasets import DatasetSpec, build_dataset
from tests.modeling.test_methods import _blobs, _xs, coef, fit, ts_data
from tests.modeling.test_methods_b import panel


def test_cv_splits_are_purged_and_ordered():
    dates = np.repeat(np.arange(100), 3).astype("datetime64[D]")
    for tr, te in M.cv_splits(dates, "expanding", 4, purge=2):
        assert dates[tr].max() < dates[te].min() - np.timedelta64(2, "D")
    for tr, te in M.cv_splits(dates, "purged_kfold", 5, purge=3, embargo=1):
        lo, hi = dates[te].min(), dates[te].max()
        near = (dates[tr] >= lo - np.timedelta64(3, "D")) & (dates[tr] <= hi + np.timedelta64(4, "D"))
        assert not near.any()


def test_regularized_selects_true_features():
    g = np.random.default_rng(1)
    n, k = 400, 12
    X = g.normal(size=(n, k))
    y = 1.0 + 2 * X[:, 0] - 1.5 * X[:, 3] + g.normal(scale=0.5, size=n)
    cols = {f"x{j}": X[:, j] for j in range(k)}
    res = fit("regularized", ts_data({"y": y, **cols}, "y", list(cols)), model="lasso", selection="1se")
    sel = set(res["summary"]["selected"])
    assert {"x0", "x3"} <= sel and len(sel) <= 5
    assert coef(res, "x0")["estimate"] == pytest.approx(2.0, abs=0.15)
    assert res["plots"]["coef_path"]["kind"] == "coef_path" and res["evaluation"]["metrics"]["model"]["r2_oos"] > 0.8
    r = fit("regularized", ts_data({"y": y, **cols}, "y", list(cols)), model="ridge")
    assert coef(r, "x3")["estimate"] == pytest.approx(-1.5, abs=0.2)


def test_tree_ensemble_classification_with_shap():
    g = np.random.default_rng(2)
    n = 1500
    X = g.normal(size=(n, 4))
    p = 1 / (1 + np.exp(-(2 * X[:, 0] - 2 * (X[:, 1] > 0))))
    y = (g.uniform(size=n) < p).astype(float)
    cols = {f"f{j}": X[:, j] for j in range(4)}
    res = fit("tree_ensemble", ts_data({"y": y, **cols}, "y", list(cols)), model="hgb", n_estimators=100,
              shap_sample=300, shap=True, cv={"folds": 3})
    assert res["summary"]["task"] == "classification" and res["metrics"]["oos_auc"] > 0.75
    sb = res["plots"]["shap_beeswarm"]
    assert sb["kind"] == "shap_beeswarm" and {sb["features"][0]["feature"], sb["features"][1]["feature"]} == {"f0", "f1"}
    imp = next(t for t in res["tables"] if t["id"] == "importance")["rows"]
    assert imp[0][0] in ("f0", "f1") and "pdp_" + imp[0][0] in res["plots"]
    rf = fit("tree_ensemble", ts_data({"y": X[:, 0] ** 2 + g.normal(scale=0.1, size=n), **cols}, "y", list(cols)),
             model="rf", n_estimators=100, shap=False, cv={"folds": 3})
    assert rf["metrics"]["oos_r2_oos"] > 0.7


def test_gmm_and_density_clustering():
    X, truth = _blobs(60)
    res = fit("gmm", _xs(X), k_min=1, k_max=6)
    assert res["summary"]["k"] == 3 and res["clusters"]["soft"] is True
    assert all(m["probability"] > 0.9 for m in res["clusters"]["members"])
    Xn = np.vstack([X, np.random.default_rng(3).uniform(-20, 30, size=(15, 3))])
    d = fit("density_clustering", _xs(Xn), algorithm="hdbscan", min_cluster_size=15)
    assert d["summary"]["k"] == 3 and d["clusters"]["noise_count"] >= 5
    assert any(m["cluster"] == -1 for m in d["clusters"]["members"])


def test_factor_model_grs():
    g = np.random.default_rng(4)
    N, T = 150, 120
    score = g.normal(size=(N, T))
    f_true = 0.01 + g.normal(scale=0.02, size=T)       # premium on lagged score
    mkt = g.normal(scale=0.04, size=T)
    lag = np.roll(score, 1, axis=1)
    r = mkt[None, :] + f_true[None, :] * lag + g.normal(scale=0.03, size=(N, T))
    d = panel({"r": r.ravel(), "s": score.ravel()}, N, T, "r", [])
    res = fit("factor_model", d, factor_scores=["s"], include_market=True)
    fac = {row[0]: row for row in next(t for t in res["tables"] if t["id"] == "factors")["rows"]}
    assert fac["s_LS"][1] > 0.02 and fac["s_LS"][4] > 5
    assert res["fit_stats"]["N"] == 5 and res["fit_stats"]["grs_p"] > 0.01  # the LS + market factors price the quintiles
    assert res["plots"]["beta_heatmap"]["kind"] == "heatmap" and any(c["term"].startswith("lambda_") for c in res["coefficients"])


def test_leaderboard_ranks_and_dm():
    g = np.random.default_rng(5)
    n = 400
    X = g.normal(size=(n, 3))
    y = np.sin(2 * X[:, 0]) * 2 + X[:, 1] + g.normal(scale=0.3, size=n)
    cols = {f"x{j}": X[:, j] for j in range(3)}
    res = fit("leaderboard", ts_data({"y": y, **cols}, "y", list(cols)), cv={"folds": 4}, candidates=[
        {"name": "mean", "method": "mean"}, {"name": "ols", "method": "ols"},
        {"name": "gbm", "method": "hgb", "params": {"n_estimators": 150}}])
    tbl = next(t for t in res["tables"] if t["id"] == "leaderboard")
    assert tbl["rows"][0][1] == "gbm" and tbl["rows"][-1][1] == "mean"
    dm = {t["name"]: t for t in res["tests"]}
    assert dm["Diebold-Mariano: mean vs gbm"]["p_value"] < 0.01


def test_factor_ls_dataset_source(mini_lake):
    ds = build_dataset(DatasetSpec.model_validate({
        "kind": "timeseries", "frequency": "M", "start": "2016-01-01", "end": "2020-12-31",
        "variables": [{"name": "val_ls", "source": "factor_ls", "field": "value", "options": {"quantiles": 2},
                       "entities": {"universe": "mvp_us"}}]}), use_cache=False)
    v = ds.frame["val_ls"].drop_nulls()
    assert v.len() > 40 and ds.meta["kind"] == "timeseries"
