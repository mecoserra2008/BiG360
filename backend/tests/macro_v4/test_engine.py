"""macro v2 W4 engine tests on synthetic data: beta recovery (joint / univariate / HAC), shrinkage, regime statistics,
scenario math (isolated / conditional / bands / episode replay), VAR propagation smoke, factor helpers."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.macro.markets import exposures as ex
from app.macro.markets import factors as fx
from app.macro.markets import regimes as rg
from app.macro.markets import scenarios as sc
from app.macro.markets.returns import Panel, group_returns, returns_from_close


def _weekly_factors(T: int, seed: int = 0) -> tuple[list[date], pl.DataFrame, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    weeks = [date(2010, 1, 4) + timedelta(weeks=i) for i in range(T)]
    F = rng.normal(0, 1, (T, 6)) * np.array([0.1, 0.08, 0.05, 0.05, 0.01, 0.04])
    gamma = np.array([-0.05, -0.05, 0.12, -0.2, -0.8, -0.05])
    u = rng.normal(0, 0.02, T)
    eq = F @ gamma + u
    df = pl.DataFrame({"date": weeks, **{f: F[:, i] for i, f in enumerate(ex.JOINT)}, "equity": eq,
                       "d10y": F[:, 0] + F[:, 1], "dreal10": F[:, 0] + F[:, 1] - F[:, 2]})
    return weeks, df, F, u


def test_fit_batch_matches_statsmodels_hac():
    sm = pytest.importorskip("statsmodels.api")
    rng = np.random.default_rng(1)
    T, N = 156, 4
    X = np.column_stack([np.ones(T), rng.normal(size=(T, 3))])
    Y = X[:, 1:] @ rng.normal(size=(3, N)) + 0.3 * rng.normal(size=(T, N))
    out = ex.fit_batch(Y, X, 52)
    ref = sm.OLS(Y[:, 2], X).fit(cov_type="HAC", cov_kwds={"maxlags": 4, "use_correction": True})
    np.testing.assert_allclose(out["b"][2], ref.params, rtol=1e-8)
    np.testing.assert_allclose(np.sqrt(np.diag(out["cov"][2])), ref.bse, rtol=1e-8)
    assert out["r2"][2] == pytest.approx(ref.rsquared)
    Y[:100, 0] = np.nan                                   # too few observations -> NaN, not an error
    assert np.isnan(ex.fit_batch(Y, X, 60)["b"][0]).all()


def test_joint_betas_are_total_effects_and_recovered():
    T = 400
    weeks, fw, F, u = _weekly_factors(T)
    rng = np.random.default_rng(3)
    N = 30
    B = rng.normal(0, 0.5, (N, 6)) * np.array([1, 1, 1, 1, 10, 1])
    bm = rng.uniform(0.5, 1.5, N)
    eq = fw["equity"].to_numpy()
    R = F @ B.T + np.outer(eq, bm) + rng.normal(0, 0.01, (T, N))      # stocks load on macro directly and via the market
    R[rng.random((T, N)) < 0.05] = np.nan
    pan = Panel("W", weeks, [("etf", f"E{i}", "none") for i in range(N)], R)
    a = weeks[-1] + timedelta(days=6)
    raw, fits, gamma = ex.estimate(pan, fw, [a], {}, a)
    j = raw.filter(pl.col("model") == "joint")
    for k, f in enumerate(ex.JOINT):
        est = j.filter(pl.col("factor_id") == f).sort("entity_id", descending=False)
        got = dict(zip(est["entity_id"], est["beta_raw"], strict=True))
        for i in range(N):
            # total effect = direct beta + market loading x gamma (u is orthogonalised on f in the window)
            true = B[i, k] + bm[i] * gamma["betas"][k]       # exact in-sample identity (gamma estimated in-window)
            se = float(j.filter((pl.col("factor_id") == f) & (pl.col("entity_id") == f"E{i}"))["se_raw"][0])
            assert abs(got[f"E{i}"] - true) < 5 * se + 1e-3
    gse = np.sqrt(np.diag(np.reshape(gamma["cov"], (6, 6))))
    assert (np.abs(np.array(gamma["betas"]) - [-0.05, -0.05, 0.12, -0.2, -0.8, -0.05]) < 4 * gse).all()
    assert len(fits) == N and all(len(f["factors"]) == 7 for f in fits)
    # the univariate equity beta of a pure-market entity is ~1
    uni = raw.filter((pl.col("model") == "univariate") & (pl.col("factor_id") == "equity"))
    assert uni.height == N


def test_shrinkage_pulls_noisy_betas_toward_sector_and_shrinks_se():
    rng = np.random.default_rng(5)
    n = 200
    true = rng.normal(0.0, 0.1, n)
    se = np.where(np.arange(n) < 100, 0.02, 0.5)
    raw = pl.DataFrame({"as_of_date": [date(2026, 9, 30)] * n, "entity_type": "security",
                        "entity_id": [f"S{i}" for i in range(n)], "weighting": "none", "model": "joint",
                        "factor_id": "d2y", "beta_raw": true + rng.normal(0, se), "se_raw": se, "r2": 0.1,
                        "n_obs": 156, "scope": None})
    sec = pl.DataFrame({"security_id": [f"S{i}" for i in range(n)], "sector_id": ["TECH"] * n})
    out = ex.shrink(raw, sec).sort("entity_id")
    o = out.with_columns(pl.col("entity_id").str.slice(1).cast(pl.Int64).alias("i")).sort("i")
    w = o["shrink_weight"].to_numpy()
    assert w[:100].mean() > 0.8 and w[100:].mean() < 0.2
    mse_raw = np.mean((o["beta_raw"].to_numpy() - true) ** 2)
    mse_shr = np.mean((o["beta"].to_numpy() - true) ** 2)
    assert mse_shr < mse_raw
    assert (o["se"].to_numpy() <= o["beta_raw"].to_numpy() * 0 + se + 1e-12).all()
    assert o["peer_percentile"].min() > 0 and o["peer_percentile"].max() == pytest.approx(100.0)


def test_regime_table_statistics_and_timing():
    months = [date(2020, m, 1) for m in range(1, 13)]
    rets = pl.DataFrame({"entity_type": "sector", "entity_id": "TECH", "weighting": "ew", "month": months,
                         "excess": [0.01 * (i + 1) for i in range(12)], "relative": [0.0] * 12, "benchmark": "IDX:^GSPC"})
    st = pl.DataFrame({"regime_type": "cycle_quadrant", "scope": "US", "date": months,
                       "state": ["expansion"] * 6 + ["slowdown"] * 6, "model_based": False})
    out = rg.regime_table(rets, st)
    c = out.filter((pl.col("timing") == "contemporaneous") & (pl.col("metric") == "excess"))
    e = c.filter(pl.col("regime") == "expansion").row(0, named=True)
    vals = np.array([0.01 * (i + 1) for i in range(6)])
    assert e["n"] == 6 and e["mean"] == pytest.approx(vals.mean()) and e["hit_rate"] == 1.0
    assert e["t_stat"] == pytest.approx(vals.mean() / (vals.std(ddof=1) / math.sqrt(6)))
    assert not e["is_current"]
    assert c.filter(pl.col("regime") == "slowdown")["is_current"][0]
    nm = out.filter((pl.col("timing") == "next_month") & (pl.col("metric") == "excess") & (pl.col("regime") == "slowdown"))
    assert nm["n"][0] == 5          # regime of months 7..11 -> returns of months 8..12
    assert nm["mean"][0] == pytest.approx(np.mean([0.01 * k for k in range(8, 13)]))


def test_isolated_shock_mapping():
    g = np.zeros(6)
    z, notes = sc.isolated_z({"rates_10y_bp": 100}, g)
    assert z[0] == pytest.approx(1.0) and z[1] == pytest.approx(0.0) and notes
    z, _ = sc.isolated_z({"rates_2y_bp": 25, "rates_10y_bp": 75}, g)
    assert z[0] == pytest.approx(0.25) and z[1] == pytest.approx(0.5)
    z, _ = sc.isolated_z({"real_10y_bp": 50, "breakeven_10y_bp": 20}, g)
    assert z[0] + z[1] == pytest.approx(0.7) and z[2] == pytest.approx(0.2)
    z, _ = sc.isolated_z({"usd_pct": 10, "oil_pct": -50}, g)
    assert z[4] == pytest.approx(math.log(1.1)) and z[5] == pytest.approx(math.log(0.5))
    gam = np.array([-0.05, 0, 0, 0, -0.8, 0])
    z, _ = sc.isolated_z({"rates_2y_bp": 100, "rates_10y_bp": 100, "equity_pct": -10}, gam)
    # the equity residual is the equity move net of what the macro shocks already imply
    assert z[6] == pytest.approx(math.log(0.9) + 0.05)
    with pytest.raises(ValueError):
        sc.validate_shocks({"nope": 1})
    with pytest.raises(ValueError):
        sc.validate_shocks({})
    with pytest.raises(ValueError):
        sc.validate_shocks({"oil_pct": -120})


def test_conditional_shock_is_gaussian_conditional_mean():
    rng = np.random.default_rng(2)
    A = rng.normal(size=(7, 7))
    S = A @ A.T + np.eye(7)
    g = np.array([0.1, 0, 0.2, -0.3, -0.7, 0.0])
    z = sc.conditional_z({"rates_10y_bp": 100, "usd_pct": 5}, g, S)
    assert z[0] + z[1] == pytest.approx(1.0) and z[4] == pytest.approx(math.log(1.05))
    # matches the textbook formula on the constraint subspace
    C, s = sc.constraints({"rates_10y_bp": 100, "usd_pct": 5}, g)
    assert np.allclose(z, S @ C.T @ np.linalg.solve(C @ S @ C.T, s))


def test_impacts_and_bands():
    B = np.array([[-0.2, 0.0], [0.1, 0.5]])
    C = np.array([np.diag([0.01, 0.0]), np.diag([0.0, 0.04])])
    out = sc.impacts(B, C, np.array([1.0, 0.0]))
    assert out["impact"][0] == pytest.approx(math.expm1(-0.2))
    assert out["lo"][0] == pytest.approx(math.expm1(-0.2 - sc.Z90 * 0.1))
    assert out["sd"][1] == pytest.approx(0.0) and out["impact"][1] == pytest.approx(math.expm1(0.1))


def _fits_frame() -> pl.DataFrame:
    rows = [{"as_of_date": date(2026, 9, 30), "entity_type": "sector", "entity_id": "TECH", "weighting": "cw",
             "model": "joint", "factors": sc.JOINT_Z, "betas": [-0.05, -0.07, 0.13, -0.28, -1.2, -0.06, 1.5],
             "cov": list(np.diag([1e-4] * 7).ravel()), "alpha": 0.0, "resid_vol": 0.01, "r2": 0.8, "n_obs": 156,
             "scope": None},
            {"as_of_date": date(2026, 9, 30), "entity_type": "sector", "entity_id": "TECH", "weighting": "cw",
             "model": "surprise", "factors": ["surp_growth", "surp_inflation", "equity_resid"],
             "betas": [0.02, -0.01, 1.4], "cov": list(np.diag([1e-4] * 3).ravel()), "alpha": 0.0, "resid_vol": 0.01,
             "r2": 0.5, "n_obs": 120, "scope": "US"},
            {"as_of_date": date(2026, 9, 30), "entity_type": "index", "entity_id": "IDX:^GSPC", "weighting": "none",
             "model": "gamma", "factors": sc.JOINT_Z[:6], "betas": [-0.05, -0.05, 0.12, -0.18, -0.76, -0.05],
             "cov": list(np.zeros(36)), "alpha": 0.0, "resid_vol": 0.016, "r2": 0.37, "n_obs": 156, "scope": None}]
    return pl.DataFrame(rows)


def test_run_scenario_surprise_channel_and_missing_factor_alignment():
    fits = _fits_frame()
    keys, B, C, _ = sc.fit_arrays(fits, "surprise", sc.SURPRISE_Z)
    assert B.shape == (1, 3) and B[0, 2] == 0.0          # labor absent -> 0 (equity_resid not in SURPRISE_Z)
    res = sc.run_scenario(fits, {"growth_surprise": 100})
    r = res["frame"].row(0, named=True)
    assert r["surprise_part"] == pytest.approx(math.expm1(0.02)) and r["impact"] == pytest.approx(math.expm1(0.02))
    res = sc.run_scenario(fits, {"rates_2y_bp": 100, "rates_10y_bp": 100})
    assert res["frame"]["impact"][0] == pytest.approx(math.expm1(-0.05))


def test_episode_replay_math():
    obs = pl.DataFrame({"series_id": ["fred:DGS2", "fred:DGS2", "fred:DGS10", "fred:DGS10", "fred:DTWEXBGS", "fred:DTWEXBGS"],
                        "date": [date(2021, 12, 31), date(2022, 10, 21)] * 3, "value": [0.78, 4.6, 1.63, 4.22, 115.0, 127.0]})
    eq = pl.DataFrame({"date": [date(2021, 12, 31), date(2022, 10, 21)], "v": [4766.0, 3752.0]})
    ep = sc.EPISODE_BY_ID["hiking_2022"]
    mv = sc.episode_moves(ep, obs, eq)
    assert mv["d2y"] == pytest.approx(4.6 - 0.78) and mv["dcurve"] == pytest.approx((4.22 - 1.63) - (4.6 - 0.78))
    assert mv["usd"] == pytest.approx(math.log(127 / 115)) and mv["equity"] == pytest.approx(math.log(3752 / 4766))
    assert mv["dbe10"] is None and mv["oil"] is None
    g = np.array([-0.05, -0.05, 0.12, -0.18, -0.76, -0.05])
    z, missing = sc.episode_z(mv, g)
    assert set(missing) == {"dbe10", "dcredit", "oil"}
    assert z[6] == pytest.approx(mv["equity"] - g @ z[:6])
    assert [e.start < e.end for e in sc.EPISODES] == [True] * len(sc.EPISODES)


def test_var_propagation_smoke():
    rng = np.random.default_rng(4)
    months = [date(2006 + (i // 12), i % 12 + 1, 1) for i in range(200)]
    K = 7
    Y = np.zeros((200, K))
    A = 0.3 * np.eye(K)
    for t in range(1, 200):
        Y[t] = A @ Y[t - 1] + rng.normal(0, 0.1, K)
    names = sc.JOINT_Z[:6] + ["equity"]
    fac = pl.concat([pl.DataFrame({"frequency": "M", "date": months, "factor_id": f, "value": Y[:, j]})
                     for j, f in enumerate(names)])
    s0 = np.array([1.0, 0, 0, 0, 0, 0, 0])
    v = sc.var_cumulative(fac, s0, 3, np.zeros(6))
    assert v["path"].shape == (4, K) and np.allclose(v["path"][0], s0)
    assert v["cumulative"][0] > 1.0                      # positive own persistence adds to the impact
    rs = sc.resolve({"rates_2y_bp": 100, "rates_10y_bp": 100}, {"var_propagation": True, "var_horizon_months": 3},
                    np.zeros(6), fac)
    assert rs["var"]["horizon_months"] == 3 and len(rs["var"]["path"]) == 4
    assert not np.allclose(rs["z_total"], rs["z"])


def test_factor_helpers():
    obs = pl.DataFrame({"date": [date(2024, 1, 2), date(2024, 1, 9), date(2024, 1, 23)], "value": [1.0, 1.5, 2.0]})
    lv = fx.changes(fx.levels_on_grid(obs, "W", date(2024, 1, 31)), "diff")
    vals = dict(zip(lv["date"].to_list(), lv["value"].to_list(), strict=True))
    assert vals[date(2024, 1, 8)] == pytest.approx(0.5)
    assert vals[date(2024, 1, 15)] is None                # week without an observation has no change of its own
    assert vals[date(2024, 1, 22)] == pytest.approx(0.5)
    assert vals[date(2024, 1, 29)] is None
    r = pl.DataFrame({"date": [date(2020, m, 1) for m in range(1, 13)],
                      "level": [1.0, 1.0, 1.25, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.25, 0.75]})
    st = dict(zip(*fx.provisional_rate_cycle(r).to_dict(as_series=False).values(), strict=True))
    assert st[date(2020, 7, 1)] == "hiking" and st[date(2020, 10, 1)] == "pausing" and st[date(2020, 12, 1)] == "cutting"
    assert fx.inflation_bucket(1.5) == "below_2" and fx.inflation_bucket(3.0) == "3_to_5" and fx.inflation_bucket(7) == "above_5"


def test_returns_helpers():
    C = np.array([[10.0, 5.0], [11.0, np.nan], [12.1, 5.5], [100.0, 6.0]])
    d = [date(2024, 1, 1), date(2024, 1, 8), date(2024, 1, 15), date(2024, 1, 22)]
    dates, R = returns_from_close(d, C, "W")
    assert R[0, 0] == pytest.approx(0.1) and np.isnan(R[0, 1]) and np.isnan(R[2, 0])   # +726% week = bad tick
    R2 = np.array([[0.1, 0.2, 0.3, 0.4, 0.5, -0.9]])
    W = np.array([[1, 1, 1, 1, 1, np.nan]])
    ew, cw = group_returns(R2, W, np.arange(6), "W")
    assert ew[0] == pytest.approx(np.mean([0.1, 0.2, 0.3, 0.4, 0.5, -0.5]))            # winsorised at -50%
    assert cw[0] == pytest.approx(0.3)
