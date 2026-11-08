"""Phase B methods on synthetic data with known parameters."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.modeling.registry import FitData
from tests.modeling.test_methods import coef, fit, months, ts_data


def panel(cols: dict[str, np.ndarray], n_ent: int, per: int, y: str | None, x: list[str]) -> FitData:
    ds = months(per, date(2010, 1, 31))
    f = pl.DataFrame({"date": ds * n_ent, "entity_id": [f"E{i:03d}" for i in range(n_ent) for _ in range(per)],
                      "entity_label": [f"T{i}" for i in range(n_ent) for _ in range(per)],
                      **{k: np.asarray(v, float) for k, v in cols.items()}})
    return FitData(f, {"kind": "panel", "frequency": "M", "variables": list(cols), "lead_variables": []}, y, x, None, 42)


def test_panel_fe_re_hausman():
    g = np.random.default_rng(1)
    N, T = 60, 20
    a = np.repeat(g.normal(size=N), T)
    x = a + g.normal(size=N * T)              # regressor correlated with the entity effect
    y = 1.5 * x + 2 * a + g.normal(size=N * T)
    res = fit("panel", panel({"y": y, "x": x}, N, T, "y", ["x"]), estimator="fe")
    assert coef(res, "x")["estimate"] == pytest.approx(1.5, abs=0.06)
    h = next(t for t in res["tests"] if t["name"].startswith("Hausman"))
    assert h["p_value"] < 0.01 and h["passed"] is None
    re = fit("panel", panel({"y": y, "x": x}, N, T, "y", ["x"]), estimator="re", cov_type="kernel")
    assert "const" in [c["term"] for c in re["coefficients"]]


def test_fama_macbeth_premium():
    g = np.random.default_rng(2)
    N, T = 80, 60
    x = g.normal(size=N * T)
    lam = 0.02 + g.normal(scale=0.01, size=T)
    y = np.repeat(lam, 1).reshape(1, T).repeat(N, 0).ravel() * x + g.normal(scale=0.05, size=N * T)
    res = fit("fama_macbeth", panel({"y": y, "x": x}, N, T, "y", ["x"]))
    c = coef(res, "x")
    assert c["estimate"] == pytest.approx(0.02, abs=0.004) and c["statistic"] > 5
    assert res["fit_stats"]["periods"] == T and "lambdas" in res["plots"]


def test_iv2sls_recovers_and_diagnoses():
    g = np.random.default_rng(3)
    n = 3000
    z1, z2, u = g.normal(size=n), g.normal(size=n), g.normal(size=n)
    xe = z1 + 0.5 * z2 + 0.8 * u + g.normal(size=n)
    y = 1 + 2 * xe + u
    d = ts_data({"y": y, "xe": xe, "z1": z1, "z2": z2}, "y", [])
    res = fit("iv2sls", d, endog=["xe"], instruments=["z1", "z2"])
    assert coef(res, "xe")["estimate"] == pytest.approx(2.0, abs=0.08)
    names = {t["name"]: t for t in res["tests"]}
    assert names["First-stage F (xe)"]["passed"] is True
    assert names["Sargan"]["p_value"] > 0.01 and names["Wu-Hausman"]["p_value"] < 0.01


def _did_panel(N=60, T=40, t0=20, staggered=False, effect=1.0, seed=4):
    g = np.random.default_rng(seed)
    alpha = np.repeat(g.normal(size=N), T)
    lam = np.tile(g.normal(size=T), N)
    treated = np.arange(N) < N // 2
    start = np.where(treated, (t0 if not staggered else np.where(np.arange(N) % 2 == 0, 15, 25)), 10 ** 6)
    tt = np.tile(np.arange(T), N)
    D = (tt >= np.repeat(start, T)).astype(float)
    y = alpha + lam + effect * D + g.normal(scale=0.3, size=N * T)
    d = panel({"y": y, "D": D}, N, T, "y", [])
    ds = months(T, date(2010, 1, 31))
    events = [{"entity": f"E{i:03d}", "date": ds[int(start[i])].isoformat()} for i in range(N) if treated[i]]
    return d, events, ds


def test_did_designs_recover_effect():
    d, events, ds = _did_panel()
    ents = [e["entity"] for e in events]
    es = fit("did", d, design="event_study", leads=4, lags=6,
             treatment={"mode": "entities", "entities": ents, "date": ds[20].isoformat()})
    path = es["plots"]["event_path"]
    est = dict(zip(path["x"], path["y"], strict=True))
    assert est[-1] == 0 and abs(est[-3]) < 0.25 and est[3] == pytest.approx(1.0, abs=0.25)
    assert next(t for t in es["tests"] if t["name"].startswith("Pre-trend"))["p_value"] > 0.01
    two = fit("did", d, design="2x2", treatment={"mode": "entities", "entities": ents, "date": ds[20].isoformat()})
    assert coef(two, "did")["estimate"] == pytest.approx(1.0, abs=0.15)
    tw = fit("did", d, design="twfe", treatment={"mode": "events", "events": events})
    assert coef(tw, "D")["estimate"] == pytest.approx(1.0, abs=0.1)
    ds2, ev2, _ = _did_panel(staggered=True)
    cs = fit("did", ds2, design="staggered", bootstrap=99, treatment={"mode": "events", "events": ev2})
    assert cs["metrics"]["att"] == pytest.approx(1.0, abs=0.15) and cs["metrics"]["att_se"] > 0
    assert "group_time_att" in {t["id"] for t in cs["tables"]}
    col = fit("did", panel({"y": d.frame["y"].to_numpy(), "D": d.frame["D"].to_numpy()}, 60, 40, "y", []),
              design="twfe", treatment={"mode": "column", "column": "D"})
    assert coef(col, "D")["estimate"] == pytest.approx(1.0, abs=0.1)


def test_synthetic_control():
    g = np.random.default_rng(5)
    T, J = 60, 8
    donors = np.cumsum(g.normal(size=(T, J)), axis=0) + 50
    treated = 0.5 * donors[:, 0] + 0.5 * donors[:, 1] + g.normal(scale=0.05, size=T)
    treated[40:] += 2.0
    Y = np.column_stack([treated, donors])
    d = panel({"y": Y.T.ravel()}, J + 1, T, "y", [])
    ds = months(T, date(2010, 1, 31))
    res = fit("synthetic_control", d, treated="T0", treatment_date=ds[40].isoformat(), placebo=True)
    w = {r[1]: r[2] for r in next(t for t in res["tables"] if t["id"] == "weights")["rows"]}
    assert w["T1"] == pytest.approx(0.5, abs=0.1) and w["T2"] == pytest.approx(0.5, abs=0.1)
    assert res["fit_stats"]["avg_post_gap"] == pytest.approx(2.0, abs=0.2)
    assert res["fit_stats"]["placebo_p_value"] <= 0.2 and res["plots"]["synthetic"]["kind"] == "ts_band"


def test_event_car():
    g = np.random.default_rng(6)
    N, T = 30, 400
    m = g.normal(scale=0.01, size=T)
    R = 0.0002 + 1.2 * m[:, None] + g.normal(scale=0.01, size=(T, N))
    ev_t = 330
    R[ev_t, :20] += 0.05
    d = panel({"r": R.T.ravel(), "mkt": np.tile(m, N)}, N, T, "r", [])
    ds = months(T, date(2010, 1, 31))
    events = [{"entity": f"E{i:03d}", "date": ds[ev_t].isoformat()} for i in range(20)]
    res = fit("event_car", d, events=events, market="mkt", est_window=[-250, -30], event_window=[-5, 5])
    assert res["fit_stats"]["caar"] == pytest.approx(0.05, abs=0.015)
    t = {x["name"]: x for x in res["tests"]}
    assert t["Patell Z"]["p_value"] < 0.001 and res["plots"]["event_path"]["kind"] == "event_path"
    ev = next(x for x in res["tables"] if x["id"] == "events")
    assert np.mean([r[ev["columns"].index("beta")] for r in ev["rows"]]) == pytest.approx(1.2, abs=0.15)


def test_var_irf_fevd_granger():
    g = np.random.default_rng(7)
    n = 600
    Acoef = np.array([[0.5, 0.3], [0.0, 0.4]])
    Y = np.zeros((n, 2))
    for t in range(1, n):
        Y[t] = Acoef @ Y[t - 1] + g.normal(size=2)
    res = fit("var", ts_data({"a": Y[:, 0], "b": Y[:, 1]}, None, ["a", "b"]), irf_periods=10)
    assert res["summary"]["lags"] == 1
    assert coef(res, "a: L1.b")["estimate"] == pytest.approx(0.3, abs=0.06)
    irf = res["plots"]["irf"]
    assert irf["kind"] == "irf" and len(irf["cells"]) == 4 and len(irf["cells"][0]["mean"]) == 11
    assert res["plots"]["fevd"]["kind"] == "fevd"
    gr = {t["name"]: t["p_value"] for t in res["tests"] if t["group"] == "granger"}
    assert gr["Granger: b -> a"] < 1e-6 and gr["Granger: a -> b"] > 1e-3


def test_cointegration_and_vecm():
    g = np.random.default_rng(8)
    n = 500
    x = np.cumsum(g.normal(size=n)) + 100
    y = 2 * x + 5 + g.normal(scale=0.5, size=n)
    d = ts_data({"y": y, "x": x}, "y", ["x"])
    res = fit("cointegration", d)
    assert res["fit_stats"]["hedge_ratio"] == pytest.approx(2.0, abs=0.02) and res["fit_stats"]["eg_p"] < 0.01
    assert res["fit_stats"]["johansen_rank"] == 1 and res["plots"]["spread"]["kind"] == "ts_band"
    assert res["fit_stats"]["half_life"] < 2
    v = fit("vecm", ts_data({"y": y, "x": x}, None, ["y", "x"]))
    assert v["summary"]["rank"] == 1
    beta = next(t for t in v["tables"] if t["id"] == "beta")["rows"]
    assert beta[1][1] / beta[0][1] == pytest.approx(-2.0, abs=0.05)  # y - 2x


def test_garch_recovers_parameters():
    g = np.random.default_rng(9)
    n, om, al, be = 4000, 0.05, 0.10, 0.85
    r, s2 = np.zeros(n), om / (1 - al - be)
    for t in range(n):
        r[t] = np.sqrt(s2) * g.normal()
        s2 = om + al * r[t] ** 2 + be * s2
    res = fit("garch", ts_data({"r": r / 100}, "r", []), dist="normal", mean="zero")
    assert coef(res, "alpha[1]")["estimate"] == pytest.approx(al, abs=0.04)
    assert coef(res, "beta[1]")["estimate"] == pytest.approx(be, abs=0.05)
    assert res["fit_stats"]["persistence"] == pytest.approx(0.95, abs=0.03)
    k = next(t for t in res["tests"] if t["name"] == "Kupiec POF VaR 5%")
    assert k["p_value"] > 0.01 and res["forecast"]["steps"] == 10 and res["plots"]["cond_vol"]["kind"] == "ts_band"


def test_markov_switching_regimes():
    g = np.random.default_rng(10)
    n = 400
    s = np.zeros(n, int)
    for t in range(1, n):
        s[t] = s[t - 1] if g.uniform() < 0.97 else 1 - s[t - 1]
    y = np.where(s == 0, 0.5 + g.normal(scale=0.5, size=n), -1 + g.normal(scale=2.0, size=n))
    res = fit("markov_switching", ts_data({"y": y}, "y", []), search_reps=5)
    rp = res["plots"]["regime_prob"]
    assert rp["kind"] == "regime_prob" and len(rp["regimes"]) == 2
    calm = next(r for r in rp["regimes"] if r["label"] == "calm")
    acc = np.mean((np.array(calm["prob"]) > 0.5) == (s == 0))
    assert acc > 0.85
    reg = next(t for t in res["tables"] if t["id"] == "regimes")["rows"]
    assert min(r[4] for r in reg) > 10


def test_state_space_and_tv_beta():
    g = np.random.default_rng(11)
    n = 300
    lvl = np.cumsum(g.normal(scale=0.3, size=n))
    res = fit("state_space", ts_data({"y": lvl + g.normal(size=n)}, "y", []), forecast_steps=6)
    assert "component_level" in res["plots"] and res["forecast"]["steps"] == 6
    x = g.normal(size=n)
    beta = np.linspace(0.5, 1.5, n)
    y = beta * x + g.normal(scale=0.2, size=n)
    tv = fit("tv_beta", ts_data({"y": y, "x": x}, "y", ["x"]))
    assert tv["fit_stats"]["last_x"] == pytest.approx(1.5, abs=0.25)
    path = tv["plots"]["beta_x"]
    assert path["kind"] == "ts_band" and path["series"][0]["y"][0] < 0.9
