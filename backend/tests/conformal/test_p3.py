"""Conformal P3 integrations: query-time helpers (bands, touch, pair bands), the two alert evaluators, the Backtest
Lab hooks (walk-forward bands, breakout events, band stops, spec / plan) and the Model Studio method."""

from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from statistics import NormalDist

import numpy as np
import polars as pl
import pytest

from app.conformal import serve as S

NOW = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)


def _row(lc: float = 100.0, sd: float = 0.02, ev: bool = False) -> dict:
    r = {"as_of_date": date(2026, 10, 9), "last_close": lc, "sigma_d": sd, "pool_key": "US|TECH", "region": "US",
         "earn_in_window": ev, "earn_jump_var": 4 * sd * sd, "earn_days_ahead": 3 if ev else None}
    for h in S.HORIZONS:
        r[f"v_{h}"] = h * sd * sd
        r[f"mu_{h}"] = 0.0
        for lvl, q in ((80, 1.64), (90, 2.71), (95, 3.84)):
            r[f"qm_{h}_{lvl}"] = q
    return r


def _curves(region: str = "US") -> list[dict]:
    probs = [round(0.01 * i, 2) for i in range(1, 100)]
    nd = NormalDist()
    return [{"pool_key": f"{region}|*", "horizon": h, "side": side, "kind": kind, "n": 1000, "probs": probs,
             "u": [nd.inv_cdf((1 + p) / 2) for p in probs]}
            for h in S.HORIZONS for side in ("up", "down") for kind in ("normal", "event")]


# ------------------------------------------------------------------------------------------------ serve helpers


def test_marginal_bands_prices_and_nesting():
    b = S.marginal_bands(_row())
    assert len(b) == 12
    by = {(x["horizon"], x["level"]): x for x in b}
    x = by[(5, 90)]
    hw = math.sqrt(2.71 * 5 * 0.02 ** 2)
    assert x["lo"] == pytest.approx(100 * math.exp(-hw)) and x["hi"] == pytest.approx(100 * math.exp(hw))
    assert by[(5, 80)]["hi"] < by[(5, 90)]["hi"] < by[(5, 95)]["hi"]
    assert by[(1, 90)]["hi"] < by[(20, 90)]["hi"]
    assert x["target_date"] == "2026-10-16"
    assert S.outside_band(hw * 1.01, _row(), 5, 90) and not S.outside_band(hw * 0.99, _row(), 5, 90)


def test_touch_probability_monotone_and_fallbacks():
    idx = S.curve_index(_curves())
    row = _row()
    near = S.touch(101.0, row, 20, idx)
    far = S.touch(130.0, row, 20, idx)
    assert near["side"] == "up" and 0 < far["p_touch"] < near["p_touch"] <= 1
    assert near["curve"]["fallback"] is True and near["curve"]["pool_key"] == "US|*"   # sector pool missing
    # shorter horizon -> smaller chance at the same level
    assert S.touch(110.0, row, 1, idx)["p_touch"] < S.touch(110.0, row, 20, idx)["p_touch"]
    # u at a 1-sigma(20d) distance: P(|Z| >= 1) under the synthetic curve ~ 0.317
    lvl = 100 * math.exp(math.sqrt(20) * 0.02)
    assert S.touch(lvl, row, 20, idx)["p_touch"] == pytest.approx(0.317, abs=0.01)
    ext = S.touch(110.0, row, 60, idx)
    assert ext["extrapolated"] and ext["curve"]["horizon"] == 20
    assert S.touch(110.0, row, 20, {})["p_touch"] is None


def test_pair_band_moments_match_dense():
    rng = np.random.default_rng(0)
    n = 6
    d = rng.uniform(1e-4, 3e-4, n)
    L = rng.normal(0, 0.01, (n, 4))
    mu = rng.normal(0, 1e-3, n)
    a = rng.normal(0, 1, n)
    m, var = S.combo_moments(a, mu, d, L)
    Sig = np.diag(d) + L @ L.T
    assert m == pytest.approx(a @ mu) and var == pytest.approx(a @ Sig @ a)
    b = S.hedge_beta(0, 1, d, L)
    assert b == pytest.approx(Sig[0, 1] / Sig[1, 1])
    e = np.zeros(n)
    e[0], e[1] = 1, -b
    _, v_hedged = S.combo_moments(e, mu, d, L)
    for bb in (b - 0.2, b + 0.2):    # minimum-variance hedge
        e[1] = -bb
        assert S.combo_moments(e, mu, d, L)[1] >= v_hedged - 1e-15
    # marginal calibration of one combination over a history of laws
    days = [date(2026, 1, 1) + timedelta(days=k) for k in range(80)]
    by = {}
    for k, dd in enumerate(days):
        by[dd] = [{"security_id": s_, "r": float(rng.normal(0, 0.02)), "mu_next": 0.0, "d_next": 4e-4, "l1_next": 0.0,
                   "l2_next": 0.0, "l3_next": 0.0, "l4_next": 0.0} for s_ in ("A", "B")]
    zs = S.combo_abs_z(by, {"A": 1.0, "B": -1.0})
    assert len(zs) == 79 and all(z >= 0 for _, z in zs)
    q90 = S.conformal_abs([z for _, z in zs], 90)
    assert 1.2 < q90 < 2.3                          # |N(0,1)| 90% quantile ~1.64
    assert S.conformal_abs([1.0] * 10, 90) is None
    lo, hi = S.band(0.0, 4.0, 9.0)
    assert (lo, hi) == (-6.0, 6.0) and S.band(0.0, 1.0, None) is None


# ------------------------------------------------------------------------------------------------ alerts


def _bars(sid: str, closes: list[float], end: date) -> pl.DataFrame:
    n = len(closes)
    days = [end - timedelta(days=n - 1 - k) for k in range(n)]
    return pl.DataFrame({"security_id": [sid] * n, "listing_id": ["L"] * n, "date": days, "region": ["US"] * n,
                         "year": [x.year for x in days], "open": closes, "high": closes, "low": closes, "close": closes,
                         "adj_factor": [1.0] * n, "split_factor": [1.0] * n, "adj_close": closes, "currency": ["USD"] * n,
                         "source_id": ["polygon"] * n, "ingested_at": [NOW] * n})


def _alert(rule_type: str, params: dict, security_id: str = "S1") -> dict:
    return {"alert_id": f"A_{rule_type}", "security_id": security_id, "watchlist_id": None, "rule_type": rule_type,
            "params": json.dumps(params), "enabled": True, "cooldown_days": 0, "last_fired_at": None, "created_at": NOW}


def test_alert_price_outside_calibrated_range():
    from app.scheduler import alerts as al

    d1, d2 = date(2026, 10, 8), date(2026, 10, 9)
    prices = pl.concat([_bars("S1", [100.0, 106.0], d2), _bars("S2", [100.0, 101.0], d2)])
    env = []
    for sid in ("S1", "S2"):
        r = {**_row(), "as_of_date": d1, "security_id": sid, "region": "US", "pool_key": "US|*", "sector_id": "TECH",
             "filter": "ewma", "radius_mode": "plain", "model_based": True, "q_pool_kind": "normal", "nb_earn": False,
             "computed_at": NOW}
        env.append(r)
    dp = al.frames_provider({"prices_daily": prices, "conformal_envelopes": pl.DataFrame(env, infer_schema_length=None)})
    # 95% 1-session half-width = sqrt(3.84) x 2% = 3.9%: +5.8% (log) is outside, +1% inside
    ev = al.evaluate_alerts([_alert("price_outside_calibrated_range", {"level": 95, "horizon": 1}, "S1"),
                             {**_alert("price_outside_calibrated_range", {"level": 95}, "S2"), "alert_id": "A2"}], dp, d2, now=NOW)
    assert [e["security_id"] for e in ev] == ["S1"]
    p = json.loads(ev[0]["payload"])
    assert p["direction"] == "up" and p["model_based"] and p["origin_date"] == str(d1)
    assert not al.evaluate_alerts([_alert("price_outside_calibrated_range", {"level": 95, "side": "down"})], dp, d2, now=NOW)


def test_alert_conformal_peer_anomaly():
    from app.scheduler import alerts as al

    d = date(2026, 10, 9)
    days = [d - timedelta(days=k) for k in range(99, -1, -1)]
    groups = pl.DataFrame({"version": ["v1"] * 3, "group_id": ["G"] * 3, "security_id": ["S1", "S2", "S3"],
                           "region_bloc": ["US"] * 3, "group_kind": ["industry"] * 3, "label": ["semis (US)"] * 3,
                           "member_rank": [1, 2, 3], "n_members": [3] * 3, "built_as_of": [d] * 3, "computed_at": [NOW] * 3})
    scores = pl.DataFrame({"as_of_date": [d], "group_id": ["G"], "year": [2026], "version": ["v1"], "filter": ["gcn_gru"],
                           "radius_mode": ["aci"], "model_based": [True], "region_bloc": ["US"], "n_members": [3],
                           "score": [30.0], "score_pct": [0.97], "anomaly_90": [True], "top_member": ["S2"],
                           "breakaway_member": ["S2"], "breakaway_z": [3.5], "computed_at": [NOW]})
    rows = []
    for k, x in enumerate(days):
        for sid in ("S1", "S2"):
            z = 0.1 * (k % 10) if not (x == d and sid == "S2") else 4.0
            rows.append({"as_of_date": x, "group_id": "G", "security_id": sid, "year": x.year, "z_cond": z, "computed_at": NOW})
    dp = al.frames_provider({"conformal_peer_groups": groups, "conformal_peer_scores": scores,
                             "conformal_peer_members": pl.DataFrame(rows)})
    grp = al.evaluate_alerts([_alert("conformal_peer_anomaly", {"percentile": 0.95, "scope": "group"}, "S1")], dp, d, now=NOW)
    assert len(grp) == 1 and json.loads(grp[0]["payload"])["scope"] == "group"
    assert not al.evaluate_alerts([_alert("conformal_peer_anomaly", {"percentile": 0.98, "scope": "group"}, "S1")], dp, d, now=NOW)
    mem = al.evaluate_alerts([_alert("conformal_peer_anomaly", {"percentile": 0.99, "scope": "member"}, "S2")], dp, d, now=NOW)
    assert len(mem) == 1 and json.loads(mem[0]["payload"])["scope"] == "member"
    assert not al.evaluate_alerts([_alert("conformal_peer_anomaly", {"percentile": 0.99, "scope": "member"}, "S1")], dp, d, now=NOW)


def test_alert_param_validation():
    from fastapi import HTTPException

    from app.api.routers.alerts import validate_params

    assert validate_params("price_outside_calibrated_range", {}) == {"level": 95, "horizon": 1, "side": "any"}
    assert validate_params("conformal_peer_anomaly", {"percentile": 97})["percentile"] == pytest.approx(0.97)
    for rt, p in (("price_outside_calibrated_range", {"level": 85}), ("price_outside_calibrated_range", {"horizon": 3}),
                  ("conformal_peer_anomaly", {"percentile": 0.2}), ("conformal_peer_anomaly", {"scope": "x"})):
        with pytest.raises(HTTPException):
            validate_params(rt, p)


# ------------------------------------------------------------------------------------------------ backtest hooks


def _panel(T: int = 900, N: int = 30, seed: int = 1):
    from app.backtest import _engine as E

    rng = np.random.default_rng(seed)
    days = [date(2020, 1, 1) + timedelta(days=k) for k in range(int(T * 1.5)) if (date(2020, 1, 1) + timedelta(days=k)).weekday() < 5][:T]
    rows = []
    for j in range(N):
        sd = 0.01 + 0.0005 * j
        px = 50 * np.exp(np.cumsum(rng.normal(0, sd, T)))
        rows += [{"entity_id": f"E{j:02d}", "date": d, "px": float(p)} for d, p in zip(days, px, strict=True)]
    return E.price_panel_from_long(pl.DataFrame(rows))


def test_walk_forward_bands_calibrated_and_causal():
    from app.conformal import backtest_hooks as CH

    px = _panel()
    hw = CH.walk_forward_halfwidths(px, 5)
    assert hw.shape == (px.T, len(px.ids), 3)
    r = CH.log_returns(px)
    L = np.where(np.isfinite(r), r, 0.0).cumsum(0)
    mv = L[5:] - L[:-5]
    lim = hw[:-5, :, 1]
    ok = np.isfinite(lim)
    assert ok[-200:].all() and not ok[:50].any()
    cov = float((np.abs(mv[ok]) <= lim[ok]).mean())
    assert 0.86 < cov < 0.94                      # iid returns: marginal 90% band on target
    # causality: changing future prices does not change past bands
    px2 = _panel()
    px2.R[-50:] *= 3.0
    px2._L = None
    hw2 = CH.walk_forward_halfwidths(px2, 5)
    assert np.allclose(np.nan_to_num(hw[:-60]), np.nan_to_num(hw2[:-60]))


def test_breakout_events_and_band_stops():
    from app.conformal import backtest_hooks as CH

    px = _panel()
    ev = CH.breakout_events(px, 5, 90, "both")
    assert ev.height > 0 and set(ev["direction"].unique().to_list()) <= {1, -1}
    up = CH.breakout_events(px, 5, 90, "up")
    assert up.height < ev.height and (up["direction"] == 1).all()
    st = CH.band_stops(ev, px, 20, 90, target=True)
    ok = st.drop_nulls("stop_loss")
    assert ok.height > 0 and ((ok["stop_loss"] > 0) & (ok["stop_loss"] <= 0.9)).all()
    lng = ok.filter(pl.col("direction") == 1).row(0, named=True)
    assert lng["take_profit"] > lng["stop_loss"]          # exp(hw) - 1 > 1 - exp(-hw)
    assert "failed" in CH.note("calibrated_band_breakout") or "EXPERIMENTAL" in CH.note("calibrated_band_breakout")


def test_backtest_spec_plan_experimental():
    from app.backtest.spec import BacktestSpec, make_plan

    spec = BacktestSpec.model_validate({
        "mode": "event", "universe": {"universe_id": "mvp_us"},
        "signal": {"variables": [{"name": "m", "source": "price", "field": "mom_12_1"}]},
        "event": {"trigger": "calibrated_band_breakout", "params": {"horizon": 5, "level": 90}, "stop_mode": "calibrated_band",
                  "band_level": 95, "band_horizon": 20}})
    plan, warns = make_plan(spec)
    assert plan["events"]["band_stops"] == {"level": 95, "horizon": 20, "target": False}
    assert sum("EXPERIMENTAL" in w for w in warns) == 2
    with pytest.raises(ValueError):
        BacktestSpec.model_validate({"mode": "event", "signal": {"variables": [{"name": "m", "source": "price", "field": "mom_12_1"}]},
                                     "event": {"trigger": "peer_breakaway", "band_level": 85}})


def test_peer_breakaway_events_from_files(tmp_path):
    from app.conformal import backtest_hooks as CH

    d = tmp_path / "reports" / "conformal" / "wf_peer"
    d.mkdir(parents=True)
    pl.DataFrame({"group_id": ["G", "G", "G", "H"], "date": [date(2020, 1, 2)] * 3 + [date(2020, 1, 3)],
                  "security_id": ["A", "B", "C", "A"], "z_cond": [3.0, -4.0, 1.0, 2.6], "z_marg": [0.0] * 4,
                  "score": [50.0, 50.0, 50.0, 10.0], "q90": [40.0] * 4, "r": [0.0] * 4, "group_move": [0.0] * 4}
                 ).write_parquet(d / "signals_US_gcn_gru.parquet")
    ev, used = CH.peer_breakaway_events(["A", "B", "C"], 2.5, "auto", tmp_path)
    assert used == "walk_forward"
    assert ev.to_dicts() == [{"entity_id": "B", "date": date(2020, 1, 2), "direction": 1, "strength": 4.0}]


# ------------------------------------------------------------------------------------------------ Model Studio


def _studio_ts(T: int = 400, seed: int = 3):
    from app.modeling.registry import FitData

    rng = np.random.default_rng(seed)
    C = np.array([[1.0, 0.6, 0.3], [0.6, 1.0, 0.4], [0.3, 0.4, 1.0]])
    X = rng.multivariate_normal(np.zeros(3), C, T) * 0.01
    days = [date(2015, 1, 1) + timedelta(days=k) for k in range(T)]
    f = pl.DataFrame({"date": days, "a": X[:, 0], "b": X[:, 1], "c": X[:, 2]})
    return FitData(f, {"kind": "timeseries", "frequency": "D", "variables": ["a", "b", "c"], "lead_variables": []},
                   None, ["a", "b", "c"], None, 42)


def test_studio_conformal_ellipsoid_and_export(tmp_path):
    import contextlib
    import io

    from app.modeling.datasets import DatasetSpec
    from app.modeling.export import export_script
    from app.modeling.registry import StudySpec, get_method

    m = get_method("conformal_ellipsoid")
    assert m.describe()["family"] == "multivariate"
    d = _studio_ts()
    res = m.fit(d, m.parse_params({"filter": "static_shrink", "aci_gamma": 0.0})).to_dict()
    cov = res["metrics"]["coverage"]
    assert 0.84 < cov < 0.96 and res["metrics"]["n_test"] > 200
    assert {t["id"] for t in res["tables"]} >= {"projection_coverage", "anomalies", "attribution_last"}
    assert res["plots"]["score"]["kind"] == "ts_band" and len(res["forecast"]["by_variable"]) == 3
    assert any(w["code"] == "model_based" for w in res["warnings"])
    ds = DatasetSpec.model_validate({"kind": "timeseries", "variables": [{"name": v, "source": "prices", "field": "ret"}
                                                                          for v in "abc"]})
    spec = StudySpec(dataset=ds, method="conformal_ellipsoid", x=["a", "b", "c"], params={"filter": "static_shrink", "aci_gamma": 0.0})
    src = export_script({"run_id": "R1", "study": spec.model_dump(mode="json"), "result": res, "reproducibility": {}},
                        tmp_path, tmp_path)
    ns: dict = {"__name__": "exported"}
    exec(compile(src, "exported.py", "exec"), ns)  # noqa: S102 - our own generated script
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ns["fit"](d.frame)
    line = next(ln for ln in buf.getvalue().splitlines() if ln.startswith("RESULT_JSON "))
    out = json.loads(line[len("RESULT_JSON "):])
    assert out["coverage"] == pytest.approx(cov, abs=1e-12)
    assert out["q_next"] == pytest.approx(res["metrics"]["q_next"], rel=1e-9)


def test_studio_conformal_panel_and_guardrails():
    from app.modeling.registry import FitData, get_method
    from app.modeling.results import GuardrailError

    rng = np.random.default_rng(5)
    T, ents = 300, ["E1", "E2", "E3", "E4"]
    days = [date(2016, 1, 1) + timedelta(days=k) for k in range(T)]
    f = pl.DataFrame({"date": [x for x in days for _ in ents], "entity_id": ents * T, "entity_label": ents * T,
                      "ret": rng.normal(0, 0.01, T * len(ents))})
    d = FitData(f, {"kind": "panel", "frequency": "D", "variables": ["ret"], "lead_variables": []}, None, ["ret"], None, 42)
    m = get_method("conformal_ellipsoid")
    res = m.fit(d, m.parse_params({})).to_dict()
    assert res["summary"]["variables"] == sorted(ents) or set(res["summary"]["variables"]) == set(ents)
    with pytest.raises(GuardrailError):
        short = FitData(f.head(80), d.meta, None, ["ret"], None, 42)
        m.fit(short, m.parse_params({}))
