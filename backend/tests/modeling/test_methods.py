"""Phase A methods on synthetic data with known parameters (no lake): coefficient / order / cluster recovery,
diagnostics present, guardrails."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.modeling.diagnostics import check_lookahead, rolling_origin
from app.modeling.registry import FitData, all_methods, get_method
from app.modeling.results import GuardrailError, bh_adjust

RNG = np.random.default_rng(11)


def months(n: int, start: date = date(2000, 1, 31)) -> list[date]:
    out, d = [], start
    for _ in range(n):
        out.append(d)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        d = date(nxt.year + (nxt.month == 12), nxt.month % 12 + 1, 1) - timedelta(days=1)
    return out


def ts_data(cols: dict[str, np.ndarray], y: str | None, x: list[str], weights: str | None = None,
            lead: list[str] | None = None) -> FitData:
    n = len(next(iter(cols.values())))
    f = pl.DataFrame({"date": months(n), **{k: v.astype(float) for k, v in cols.items()}})
    meta = {"kind": "timeseries", "frequency": "M", "variables": list(cols), "lead_variables": lead or [],
            "interaction_pairs": {}}
    return FitData(f, meta, y, x, weights, 42)


def panel_data(cols: dict[str, np.ndarray], n_ent: int, y: str | None, x: list[str]) -> FitData:
    n = len(next(iter(cols.values())))
    per = n // n_ent
    ds = months(per)
    f = pl.DataFrame({"date": ds * n_ent, "entity_id": [f"E{i}" for i in range(n_ent) for _ in range(per)],
                      "entity_label": [f"E{i}" for i in range(n_ent) for _ in range(per)],
                      **{k: v.astype(float) for k, v in cols.items()}})
    meta = {"kind": "panel", "frequency": "M", "variables": list(cols), "lead_variables": [], "interaction_pairs": {}}
    return FitData(f, meta, y, x, None, 42)


def fit(method: str, data: FitData, **params):
    m = get_method(method)
    return m.fit(data, m.parse_params(params)).to_dict()


def coef(res: dict, term: str) -> dict:
    return next(c for c in res["coefficients"] if c["term"] == term)


def test_registry_describes_every_method():
    ids = {m.id for m in all_methods()}
    assert {"ols", "wls", "gls", "distributed_lag", "quantile", "glm", "arima", "stationarity", "kmeans", "hierarchical",
            "pca"} <= ids
    for m in all_methods():
        d = m.describe()
        assert d["params_schema"]["type"] == "object" and d["shapes"]


def test_ols_recovers_coefficients_with_diagnostics():
    n = 400
    x1, x2 = RNG.normal(size=n), RNG.normal(size=n)
    y = 1.0 + 2.0 * x1 - 3.0 * x2 + RNG.normal(scale=0.5, size=n)
    res = fit("ols", ts_data({"y": y, "x1": x1, "x2": x2}, "y", ["x1", "x2"]), cov_type="HAC")
    assert coef(res, "x1")["estimate"] == pytest.approx(2.0, abs=0.1)
    assert coef(res, "x2")["estimate"] == pytest.approx(-3.0, abs=0.1)
    assert coef(res, "const")["ci_low"] < 1.0 < coef(res, "const")["ci_high"]
    names = {t["name"] for t in res["tests"]}
    assert {"Breusch-Pagan", "White", "Durbin-Watson", "Jarque-Bera"} <= names and any(n.startswith("Ljung-Box") for n in names)
    assert coef(res, "x1")["vif"] == pytest.approx(1.0, abs=0.2)
    assert res["fit_stats"]["r2"] > 0.9 and "HAC" in res["summary"]["cov_type"]
    assert {"qq", "residuals_vs_fitted", "fit", "coef_ci"} <= set(res["plots"])
    ev = res["evaluation"]
    assert ev["metrics"]["model"]["rmse"] < ev["metrics"]["naive"]["rmse"]


def test_ols_cluster_and_white_on_panel():
    n_ent, per = 20, 30
    n = n_ent * per
    x = RNG.normal(size=n)
    y = 0.5 * x + np.repeat(RNG.normal(size=n_ent), per) + RNG.normal(size=n)
    d = panel_data({"y": y, "x": x}, n_ent, "y", ["x"])
    r1 = fit("ols", d, cov_type="cluster", cluster_by="entity")
    r2 = fit("ols", d, cov_type="HC0")
    assert coef(r1, "x")["estimate"] == pytest.approx(0.5, abs=0.15)
    assert "cluster(entity)" in r1["summary"]["cov_type"] and "White" in r2["summary"]["cov_type"]
    assert r1["evaluation"] is None  # rolling-origin only for time series


def test_guardrails_singular_few_obs_and_lookahead():
    n = 50
    x1 = RNG.normal(size=n)
    with pytest.raises(GuardrailError) as e:
        fit("ols", ts_data({"y": RNG.normal(size=n), "x1": x1, "x2": 2 * x1}, "y", ["x1", "x2"]))
    assert e.value.code == "singular"
    with pytest.raises(GuardrailError) as e:
        fit("ols", ts_data({"y": RNG.normal(size=3), "x1": RNG.normal(size=3)}, "y", ["x1"]))
    assert e.value.code == "too_few_obs"
    res = fit("ols", ts_data({"y": RNG.normal(size=25), "a": RNG.normal(size=25), "b": RNG.normal(size=25),
                              "c": RNG.normal(size=25)}, "y", ["a", "b", "c"]), rolling_eval=False)
    assert any(w["code"] == "few_obs_per_param" for w in res["warnings"])
    with pytest.raises(GuardrailError) as e:
        check_lookahead({"lead_variables": ["fwd"], "interaction_pairs": {"fwd_x_z": ["fwd", "z"]}}, ["z", "fwd_x_z"])
    assert e.value.code == "lookahead"
    check_lookahead({"lead_variables": ["fwd"]}, ["z"])  # lead only as target: fine


def test_nonstationarity_warning():
    n = 300
    g = np.random.default_rng(5)
    y = np.cumsum(g.normal(size=n))
    x = np.cumsum(g.normal(size=n))
    res = fit("ols", ts_data({"y": y, "x": x}, "y", ["x"]), rolling_eval=False)
    assert any(w["code"] == "nonstationary" for w in res["warnings"])


def test_wls_and_gls():
    n = 400
    x = RNG.normal(size=n)
    sd = np.exp(RNG.normal(size=n))
    y = 1 + 2 * x + RNG.normal(size=n) * sd
    r = fit("wls", ts_data({"y": y, "x": x, "w": 1 / sd ** 2}, "y", ["x"], weights="w"), rolling_eval=False)
    assert coef(r, "x")["estimate"] == pytest.approx(2.0, abs=0.1) and r["summary"]["estimator"] == "WLS"
    e = np.zeros(n)
    for t in range(1, n):
        e[t] = 0.6 * e[t - 1] + RNG.normal()
    y2 = 0.5 + 1.5 * x + e
    g = fit("gls", ts_data({"y": y2, "x": x}, "y", ["x"]), cov_type="nonrobust", rolling_eval=False)
    assert g["fit_stats"]["rho"][0] == pytest.approx(0.6, abs=0.1)
    assert coef(g, "x")["estimate"] == pytest.approx(1.5, abs=0.1)


def test_distributed_lag_recovers_lags_and_long_run():
    n = 500
    x = RNG.normal(size=n)
    y = np.zeros(n)
    y[2:] = 0.5 * x[1:-1] + 0.3 * x[:-2]
    y += RNG.normal(scale=0.3, size=n)
    res = fit("distributed_lag", ts_data({"y": y, "x": x}, "y", ["x"]), lags=3)
    assert coef(res, "x_L1")["estimate"] == pytest.approx(0.5, abs=0.06)
    assert coef(res, "x_L2")["estimate"] == pytest.approx(0.3, abs=0.06)
    assert abs(coef(res, "x")["estimate"]) < 0.08
    tbl = next(t for t in res["tables"] if t["id"] == "lag_blocks")
    row = tbl["rows"][0]
    assert row[0] == "x" and row[4] < 1e-6 and row[5] == pytest.approx(0.8, abs=0.1)
    assert "lag_profile_x" in res["plots"]


def test_quantile_regression():
    n = 600
    x = RNG.normal(size=n)
    y = 1 + 2 * x + RNG.standard_t(3, size=n)
    res = fit("quantile", ts_data({"y": y, "x": x}, "y", ["x"]), quantiles=[0.25, 0.5, 0.75])
    med = next(c for c in res["coefficients"] if c["term"] == "x" and c["quantile"] == 0.5)
    assert med["estimate"] == pytest.approx(2.0, abs=0.15)
    assert "process_x" in res["plots"]


def test_glm_families():
    n = 3000
    x = RNG.normal(size=n)
    p = 1 / (1 + np.exp(-(-0.5 + 1.2 * x)))
    yb = (RNG.uniform(size=n) < p).astype(float)
    r = fit("glm", ts_data({"y": yb, "x": x}, "y", ["x"]), family="binomial")
    assert coef(r, "x")["estimate"] == pytest.approx(1.2, abs=0.15)
    assert coef(r, "x")["exp_estimate"] == pytest.approx(np.exp(coef(r, "x")["estimate"]))
    assert r["fit_stats"]["auc"] > 0.7 and "roc" in r["plots"] and "calibration" in r["plots"]
    lam = np.exp(0.3 + 0.7 * x)
    yp = RNG.poisson(lam).astype(float)
    r = fit("glm", ts_data({"y": yp, "x": x}, "y", ["x"]), family="poisson")
    assert coef(r, "x")["estimate"] == pytest.approx(0.7, abs=0.06)
    assert r["fit_stats"]["dispersion"] == pytest.approx(1.0, abs=0.15)
    ynb = RNG.negative_binomial(2, 2 / (2 + lam)).astype(float)  # mean lam, alpha = 0.5
    r = fit("glm", ts_data({"y": ynb, "x": x}, "y", ["x"]), family="poisson")
    assert any(w["code"] == "overdispersion" for w in r["warnings"])
    r = fit("glm", ts_data({"y": ynb, "x": x}, "y", ["x"]), family="negative_binomial")
    assert r["fit_stats"]["nb_alpha_mle"] == pytest.approx(0.5, abs=0.15)
    yg = RNG.gamma(2.0, np.exp(0.2 + 0.4 * x) / 2.0)
    r = fit("glm", ts_data({"y": yg, "x": x}, "y", ["x"]), family="gamma", link="log")
    assert coef(r, "x")["estimate"] == pytest.approx(0.4, abs=0.06)
    with pytest.raises(GuardrailError):
        fit("glm", ts_data({"y": x, "x": x}, "y", ["x"]), family="binomial")


def _ar2(n: int, phi=(0.5, 0.35), c=0.0, seed: int = 3) -> np.ndarray:
    y = np.zeros(n + 100)
    e = np.random.default_rng(seed).normal(size=n + 100)
    for t in range(2, n + 100):
        y[t] = c + phi[0] * y[t - 1] + phi[1] * y[t - 2] + e[t]
    return y[100:]


def test_arima_order_search_recovers_ar2_and_forecasts():
    y = _ar2(500)
    res = fit("arima", ts_data({"y": y}, "y", []), auto=True, auto_d=False, max_p=3, max_q=2, criterion="bic",
              forecast_steps=6, eval_folds=5)
    assert res["summary"]["order"] == [2, 0, 0]
    assert coef(res, "ar.L1")["estimate"] == pytest.approx(0.5, abs=0.1)
    assert coef(res, "ar.L2")["estimate"] == pytest.approx(0.35, abs=0.1)
    fc = res["forecast"]
    assert fc["steps"] == 6 and len(fc["mean"]) == 6 and all(lo < m < hi for lo, m, hi in zip(fc["lower"], fc["mean"], fc["upper"]))
    assert fc["dates"][0] > res["summary"]["end"]
    assert any(t["name"].startswith("Ljung-Box") for t in res["tests"])
    assert res["evaluation"]["n_folds"] == 5 and "order_search" in {t["id"] for t in res["tables"]}
    assert "acf_y" in res["plots"]


def test_arimax_exog_and_differencing():
    n = 300
    x = RNG.normal(size=n)
    y = 0.8 * x + np.cumsum(RNG.normal(scale=0.5, size=n))  # regression with ARIMA(0,1,0) errors
    res = fit("arima", ts_data({"y": y, "x": x}, "y", ["x"]), order=[0, 1, 0], forecast_steps=3,
              exog_future="values", exog_values={"x": [0.0, 1.0, -1.0]}, rolling_eval=False)
    assert coef(res, "x")["estimate"] == pytest.approx(0.8, abs=0.08)
    assert res["summary"]["model"].startswith("ARIMAX") and res["forecast"]["steps"] == 3
    st = next(t for t in res["tables"] if t["id"] == "stationarity")
    assert st["rows"][0][5] in ("unit root", "conflicting (low power)")


def test_seasonal_arima_runs():
    n = 180
    t = np.arange(n)
    y = 2 * np.sin(2 * np.pi * t / 12) + _ar2(n, (0.5, 0.0)) * 0.3
    res = fit("arima", ts_data({"y": y}, "y", []), order=[1, 0, 0], seasonal_order=[1, 0, 0, 12], forecast_steps=12,
              eval_folds=3)
    assert res["summary"]["model"].startswith("SARIMA") and "seasonal_naive" in res["evaluation"]["metrics"]


def test_stationarity_method():
    n = 300
    res = fit("stationarity", ts_data({"rw": np.cumsum(RNG.normal(size=n)), "wn": RNG.normal(size=n)}, None, ["rw", "wn"]))
    assert res["summary"]["suggested_d"] == {"rw": 1, "wn": 0}
    assert "acf_rw" in res["plots"]


def _blobs(n_per: int = 60):
    centers = np.array([[0, 0, 0], [6, 6, 0], [0, 6, 6]], dtype=float)
    X = np.vstack([c + RNG.normal(scale=0.6, size=(n_per, 3)) for c in centers])
    truth = np.repeat(np.arange(3), n_per)
    return X, truth


def _xs(X: np.ndarray) -> FitData:
    n = len(X)
    f = pl.DataFrame({"date": [date(2024, 1, 31)] * n, "entity_id": [f"E{i}" for i in range(n)],
                      "entity_label": [f"E{i}" for i in range(n)], "sector_id": ["TECH"] * n,
                      "a": X[:, 0], "b": X[:, 1], "c": X[:, 2]})
    return FitData(f, {"kind": "cross_section", "frequency": "M", "lead_variables": []}, None, ["a", "b", "c"], None, 7)


def test_kmeans_recovers_blobs():
    from sklearn.metrics import adjusted_rand_score

    X, truth = _blobs()
    res = fit("kmeans", _xs(X), k_min=2, k_max=6)
    assert res["summary"]["k"] == 3
    lab = {m["label"]: m["cluster"] for m in res["clusters"]["members"]}
    pred = np.array([lab[f"E{i}"] for i in range(len(X))])
    assert adjusted_rand_score(truth, pred) == pytest.approx(1.0)
    assert {"elbow", "silhouette", "pca_scatter", "profile_heatmap"} <= set(res["plots"])
    assert res["fit_stats"]["silhouette"] > 0.6 and sorted(res["clusters"]["sizes"]) == [60, 60, 60]


def test_hierarchical_rows_and_pivot_correlation():
    from sklearn.metrics import adjusted_rand_score

    X, truth = _blobs(30)
    res = fit("hierarchical", _xs(X), linkage="ward")
    lab = {m["label"]: m["cluster"] for m in res["clusters"]["members"]}
    assert adjusted_rand_score(truth, [lab[f"E{i}"] for i in range(len(X))]) == pytest.approx(1.0)
    dd = res["plots"]["dendrogram"]
    assert len(dd["icoord"]) == len(X) - 1 and len(dd["leaf_labels"]) == len(X)
    assert res["fit_stats"]["cophenetic_correlation"] > 0.7
    # pivot layout: 8 entities' return series in two correlated groups
    per = 120
    f1, f2 = RNG.normal(size=per), RNG.normal(size=per)
    series = [(f1 if i < 4 else f2) + RNG.normal(scale=0.4, size=per) for i in range(8)]
    d = panel_data({"ret": np.concatenate(series)}, 8, None, ["ret"])
    res = fit("hierarchical", d, layout="pivot", pivot_var="ret", metric="correlation", linkage="average",
              standardize=False, n_clusters=2)
    lab = {m["label"]: m["cluster"] for m in res["clusters"]["members"]}
    groups = [{f"E{i}" for i in range(4)}, {f"E{i}" for i in range(4, 8)}]
    for g in groups:
        assert len({lab[e] for e in g}) == 1
    assert lab["E0"] != lab["E7"] and "correlation_heatmap" in res["plots"]
    with pytest.raises(GuardrailError):
        fit("hierarchical", _xs(X), linkage="ward", metric="correlation")


def test_pca_factor_structure():
    n = 400
    level, slope = RNG.normal(size=n), RNG.normal(size=n) * 0.5
    cols = {f"y{k}": level + slope * (k - 3) / 3 + RNG.normal(scale=0.1, size=n) for k in range(7)}
    res = fit("pca", ts_data(cols, None, list(cols)), n_components=3)
    assert res["fit_stats"]["pc1_share"] > 0.6 and res["summary"]["components_for_90pct"] <= 2
    assert {"scree", "biplot", "scores_time", "loadings_heatmap"} <= set(res["plots"])


def test_rolling_origin_and_bh():
    y = np.arange(100, dtype=float)
    ev = rolling_origin(y, lambda t, h: np.array([y[t - 1] + 1] * h), initial=50, horizon=1)
    assert ev["metrics"]["model"]["rmse"] == pytest.approx(0.0)
    assert ev["metrics"]["naive"]["rmse"] == pytest.approx(1.0)
    assert ev["metrics"]["random_walk_drift"]["rmse"] == pytest.approx(0.0)
    adj = bh_adjust([0.01, 0.04, None, 0.03])
    assert adj[2] is None and adj[0] == pytest.approx(0.03) and adj[1] == pytest.approx(0.04)
