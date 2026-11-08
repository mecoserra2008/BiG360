"""Technicals v3 #2 meta-labelling (docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling"): the meta model must find a
context effect that exists and add ~nothing when context is noise; PIT / leakage of the features; sample weights;
decisions; the Backtest Lab hook and the engine's per-event weight."""

from __future__ import annotations

import json
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.techmeta import features as F
from app.techmeta import job as J
from app.techmeta import leakage as LK
from app.techmeta import model as M


def make_ds(effect: float, seed: int = 0, n_sec: int = 120, start: date = date(2012, 1, 6), weeks: int = 360) -> pl.DataFrame:
    """Weekly origins x securities with a calibrated base P(win) (the 'T2' column) and context features. effect > 0:
    the squeeze percentile and the signed peer z move the true P(win); effect = 0: context is pure noise."""
    rng = np.random.default_rng(seed)
    cols = F.all_features()
    n = n_sec * weeks
    X = {c: rng.normal(0, 1, n) for c in cols}
    for c in F.GROUPS["sector"]:
        X[c] = (rng.random(n) < 0.25).astype(float)
    pw = rng.uniform(0.2, 0.65, n)
    pl_ = (1 - pw) * rng.uniform(0.55, 0.9, n)
    X["t2_logit_pw"], X["t2_logit_pl"] = M.logit(pw), M.logit(pl_)
    X["c_rw_pct"] = rng.uniform(0, 1, n)
    X["p_z_signed"] = np.where(rng.random(n) < 0.15, rng.normal(0, 2.5, n), 0.0)
    X["si_pct"] = np.where(np.arange(n) < n // 2, np.nan, rng.uniform(0, 0.2, n))     # source coverage starts mid-sample
    z = M.logit(pw) + effect * (1.6 * (X["c_rw_pct"] - 0.5) + 0.25 * X["p_z_signed"])
    pt = M.sigmoid(z)
    u = rng.random(n)
    yw = u < pt
    yl = (~yw) & (u < pt + (1 - pt) * 0.8)
    rr = rng.uniform(0.8, 3.0, n)
    dates = [start + timedelta(days=7 * (i // n_sec)) for i in range(n)]
    real = np.where(yw, rr, np.where(yl, -1.0, 0.0)) - 0.02
    return pl.DataFrame({"security_id": [f"S{i % n_sec:04d}" for i in range(n)], "date": dates, "out": np.ones(n, dtype=np.int64),
                         "y_win": yw, "y_loss": yl, "w_uniq": np.full(n, 0.8), "rr_close": rr, "cost_r": np.full(n, 0.02),
                         "mtm_r": np.zeros(n), "real_r_mtm": real, "pw": pw, **X})


def _split(df: pl.DataFrame, cut: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    return df.filter(pl.col("date") < cut), df.filter(pl.col("date") >= cut + timedelta(days=60))


def _ll(p: np.ndarray, y: np.ndarray) -> float:
    return M.wlogloss(p, y.astype(float))


@pytest.mark.parametrize("kind", ["lr", "xgb"])
def test_meta_finds_a_context_effect_that_exists(kind):
    df = make_ds(effect=1.0, seed=1)
    tr, te = _split(df, date(2017, 6, 1))
    cols = F.all_features()
    X, Xt = F.matrix(tr, cols), F.matrix(te, cols)
    y, yt = tr["y_win"].cast(pl.Float64).to_numpy(), te["y_win"].to_numpy()
    d = tr["date"].to_numpy().astype("datetime64[D]")
    iv = d >= np.datetime64("2016-06-01")
    L = M.fit_learner(kind, X, y, tr["w_uniq"].to_numpy(), cols, "t2_logit_pw", inner_val=iv, inner_train=d < np.datetime64("2016-04-01"))
    p = M.sigmoid(L.margin(Xt))
    gain = _ll(te["pw"].to_numpy(), yt) - _ll(p, yt)
    assert gain > 0.008, gain                                      # clearly better than the base probability
    C, names = L.contributions(Xt[:2000])
    imp = dict(zip(names, np.abs(C).mean(0), strict=False))
    top = sorted(imp, key=lambda k: -imp[k])[:3]
    assert "c_rw_pct" in top                                       # the driver that truly matters ranks first
    assert J.group_importance(imp)["techctx"] > J.group_importance(imp).get("smart", 0.0)


@pytest.mark.parametrize("kind", ["lr", "xgb"])
def test_meta_adds_nothing_when_context_is_noise(kind):
    df = make_ds(effect=0.0, seed=2)
    tr, te = _split(df, date(2017, 6, 1))
    cols = F.all_features()
    X, Xt = F.matrix(tr, cols), F.matrix(te, cols)
    y, yt = tr["y_win"].cast(pl.Float64).to_numpy(), te["y_win"].to_numpy()
    d = tr["date"].to_numpy().astype("datetime64[D]")
    L = M.fit_learner(kind, X, y, tr["w_uniq"].to_numpy(), cols, "t2_logit_pw", inner_val=d >= np.datetime64("2016-06-01"),
                      inner_train=d < np.datetime64("2016-04-01"))
    p = M.sigmoid(L.margin(Xt))
    gain = _ll(te["pw"].to_numpy(), yt) - _ll(p, yt)
    assert abs(gain) < 0.003, gain                                 # skill ~ 0 (offset model ~ the calibrated base)


def test_label_permutation_has_no_skill():
    df = make_ds(effect=1.0, seed=3)
    tr, te = _split(df, date(2017, 6, 1))
    cols = F.all_features()
    rng = np.random.default_rng(0)
    y = tr["y_win"].cast(pl.Float64).to_numpy()[rng.permutation(tr.height)]
    L = M.fit_learner("xgb", F.matrix(tr, cols), y, tr["w_uniq"].to_numpy(), cols, "t2_logit_pw",
                      params={"max_depth": 3, "min_child_weight": 300.0, "n_trees": 150})
    cal, _ = M.fit_calibrator(L.margin(F.matrix(tr, cols)), y, tr["w_uniq"].to_numpy(), tr["date"].to_numpy())
    p = cal(L.margin(F.matrix(te, cols)))
    yt = te["y_win"].cast(pl.Float64).to_numpy()
    clim = float(y.mean())
    bss = (np.mean((clim - yt) ** 2) - np.mean((p - yt) ** 2)) / np.mean((clim - yt) ** 2)
    assert bss < 0.005, bss


def test_fit_production_and_scoring_roundtrip():
    df = make_ds(effect=1.0, seed=4, n_sec=80, weeks=320)
    mm = J.fit_production(df, 20, date(2018, 1, 1))
    assert mm.kind in ("lr", "xgb") and mm.threshold in M.THRESHOLDS and mm.info["n_fit"] > 0
    assert mm.info["importance"][mm.kind]["group"]
    te = df.filter(pl.col("date") >= date(2018, 1, 1)).head(500).with_columns(
        pl.lit(1).alias("direction"), pl.lit(0.3).alias("pn"), pl.lit(0.05).alias("exp_r_mtm"), pl.lit(1.0).alias("a"),
        pl.lit(1.0).alias("b"), pl.col("pw").alias("pw"), pl.lit(0.25).alias("pl"), pl.lit(100.0).alias("entry"),
        pl.lit(100.0).alias("close"), pl.lit(0.015).alias("sigma_d"), pl.lit(1).alias("vol_regime"), pl.lit(False).alias("earn"),
        pl.lit("up").alias("trend"), pl.lit("bullish").alias("structure_state"), pl.lit("US").alias("region"),
        pl.lit("TECH").alias("sector_id"), *[pl.lit(50.0).alias(f"score_{k}") for k in ("trend", "structure", "location", "momentum")])
    sc = J.score_frame(mm, te)
    p = sc["m_p_win"].to_numpy()
    assert np.all((p > 0) & (p < 1)) and np.allclose(sc["m_p_win"] + sc["m_p_loss"] + sc["m_p_neither"], 1.0, atol=1e-9)
    k = sc["m_kelly"].to_numpy()
    assert np.all((k >= 0) & (k <= M.KELLY_CAP + 1e-12)) and np.all(k[~sc["m_take"].to_numpy()] == 0)
    drv = json.loads(sc["m_drivers"][0])
    assert len(drv) == 5 and all(not d["feature"].startswith("t2_") for d in drv)


def test_average_uniqueness_and_decisions():
    sec = np.array(["a", "a", "a", "b"])
    st = np.array(["2020-01-06", "2020-01-13", "2020-03-02", "2020-01-06"], dtype="datetime64[D]")
    w = M.avg_uniqueness(sec, st, np.array([10, 10, 10, 10]))
    assert w[2] == pytest.approx(1.0) and w[3] == pytest.approx(1.0)          # no overlap -> unique
    assert w[0] == pytest.approx(0.75) and w[1] == pytest.approx(0.75)        # 5 of 10 days shared by two labels
    pw, pl_ = np.array([0.5, 0.2]), np.array([0.3, 0.7])
    er = M.expected_r(pw, pl_, np.array([2.0, 1.0]), np.array([0.02, 0.02]), np.array([0.1, 0.0]))
    assert er[0] == pytest.approx(0.5 * 2 - 0.3 - 0.02 + 0.2 * 0.1) and er[1] < 0
    k = M.kelly_fraction(pw, pl_, np.array([2.0, 1.0]), np.zeros(2), er)
    assert 0 < k[0] <= M.KELLY_CAP and k[1] == 0
    wk = M.week_index(np.array(["2020-01-06"] * 30 + ["2020-01-13"] * 30, dtype="datetime64[D]"))
    thr, info = M.choose_threshold(np.linspace(-0.2, 0.2, 60), np.linspace(-1, 1, 60), np.tile(wk, 1))
    assert thr in M.THRESHOLDS and info["t_by_threshold"]


def test_calibrator_choice_is_in_sample():
    rng = np.random.default_rng(5)
    m = rng.normal(0, 1, 20000)
    y = (rng.random(20000) < M.sigmoid(0.5 * m - 0.3)).astype(float)       # mis-scaled margin: Platt / isotonic fix it
    cal, info = M.fit_calibrator(m, y, np.ones_like(m), np.arange(20000))
    assert info["chosen"] in ("platt", "isotonic") and info["half_logloss"]["identity"] > min(info["half_logloss"].values())
    y2 = (rng.random(20000) < M.sigmoid(m)).astype(float)                   # already calibrated: identity is competitive
    _, info2 = M.fit_calibrator(m, y2, np.ones_like(m), np.arange(20000))
    assert info2["half_logloss"]["identity"] <= min(info2["half_logloss"].values()) + 1e-3


# ------------------------------------------------------------------------------------------------ PIT / leakage


class _SI:
    def __init__(self, bars, earn):
        self.bars, self.earn, self.dv_usd = bars, earn, np.full(bars.n, 1e7)


def _bars(n: int = 1400, seed: int = 0):
    from app.technicals.data import Bars

    rng = np.random.default_rng(seed)
    c = 50 * np.exp(np.cumsum(rng.normal(0.0002, 0.016, n)))
    h, l_ = c * np.exp(np.abs(rng.normal(0, 0.006, n))), c * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    d = np.busday_offset(np.datetime64("2008-01-02"), np.arange(n), roll="forward")
    return Bars("SSYNTH", d, c.copy(), h, l_, c, rng.uniform(1e5, 1e6, n), region="US", sector_id="TECH"), d[::63][1:]


def test_bar_features_are_point_in_time_and_a_peek_is_caught():
    b, earn = _bars()
    si = _SI(b, earn)
    ok = LK.causal_bars(si, cuts=(0.5, 0.8))
    assert ok["passed"], ok
    bad = LK.causal_bars(si, cuts=(0.5, 0.8), peek=True)
    assert not bad["passed"] and all("c_ret5" in c["changed_features"] for c in bad["cuts"])


def test_context_joins_ignore_rows_known_later(tmp_data):
    class Ctx:
        start, end = date(2015, 1, 1), date(2020, 12, 31)
        mk = pl.DataFrame({"date": pl.date_range(date(2015, 1, 1), date(2020, 12, 31), "1d", eager=True)}).with_columns(
            pl.int_range(pl.len()).cast(pl.Float64).alias("m_vix"))
        mm = pl.DataFrame(schema={"region": pl.Utf8, "month": pl.Date})
        peer = pl.DataFrame({"security_id": ["S1"], "date": [date(2018, 5, 2)], "z_cond": [2.5], "ratio": [1.2]})
        members = {"S1"}
        icov = {"US": date(2015, 1, 1)}

    X = pl.DataFrame({"security_id": ["S1", "S1", "S2"], "date": [date(2018, 5, 4), date(2019, 3, 1), date(2019, 3, 1)],
                      "direction": [1, -1, 1], "region": ["US", "US", "US"]})
    ok = LK.causal_context(X, Ctx())
    assert ok["passed"], ok
    assert not LK.causal_context(X, Ctx(), peek=True)["passed"]
    j = F.join_peer(X, Ctx.peer, Ctx.members)
    assert j["p_z_signed"].to_list() == [2.5, 0.0, None]                     # member hit / member no hit / no group


# ------------------------------------------------------------------------------------------------ Backtest Lab


def test_engine_per_event_weight_and_meta_hook(tmp_path):
    from app.backtest import _engine as E
    from app.techmeta import backtest_hooks as MH

    dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(120)]
    rows = [{"entity_id": e, "date": d, "px": 100.0 * (1.001 ** i) * (1 if e == "A" else 0.999 ** i)}
            for e in ("A", "B") for i, d in enumerate(dates)]
    px = E.price_panel_from_long(pl.DataFrame(rows))
    ev = pl.DataFrame({"entity_id": ["A", "B"], "date": [dates[5], dates[5]], "direction": [1, 1], "strength": [1.0, 1.0],
                       "weight": [0.30, 0.0]})
    spec = {"event": {"hold_days": 10, "max_positions": 10, "position_weight": 0.1, "entry_lag_days": 1}, "costs": {"enabled": False}}
    sim = E.simulate_events(ev, px, spec, None)
    assert [t["entity_id"] for t in sim.trades] == ["A"] and sim.trades[0]["weight"] == pytest.approx(0.30)
    assert sim.gross_exp.max() == pytest.approx(0.30, rel=0.05)
    sim0 = E.simulate_events(ev.drop("weight"), px, spec, None)               # default unchanged without the column
    assert {t["weight"] for t in sim0.trades} == {0.1}
    rd = tmp_path / "reports" / "technicals_v3" / "meta"
    rd.mkdir(parents=True)
    pl.DataFrame({"security_id": ["A", "B", "A"], "date": [dates[5], dates[5], dates[40]], "direction": [1, 1, -1],
                  "er_sel": [0.1, -0.1, 0.2], "take": [True, False, True], "size_rel": [1.5, 0.0, 1.0],
                  "x_up": [0.05, 0.05, 0.04], "x_dn": [0.03, 0.03, 0.06]}).write_parquet(rd / "meta_oos_h20.parquet")
    (rd / "gate.json").write_text(json.dumps({"outputs": {"meta_h20": {"verdict": "fail"}}}), encoding="utf-8")
    out, used = MH.meta_events(["A", "B"], 20, "sized", "auto", "long", tmp_path, None, base_weight=0.05)
    assert used == "walk_forward" and out.height == 1 and out["weight"][0] == pytest.approx(0.075)
    assert out["stop_loss"][0] == pytest.approx(1 - np.exp(-0.03)) and out["take_profit"][0] == pytest.approx(np.exp(0.05) - 1)
    both, _ = MH.meta_events(["A", "B"], 20, "all", "walk_forward", "both", tmp_path, None)
    assert both.height == 3 and both.filter(pl.col("direction") < 0)["stop_loss"][0] == pytest.approx(np.exp(0.04) - 1)
    note, prod = MH.gate_note(tmp_path, 20)
    assert not prod and "EXPERIMENTAL" in note
    with pytest.raises(ValueError):
        MH.meta_events(["A"], 15, "take", "auto", "long", tmp_path, None)


def test_v3_extras_are_point_in_time():
    """#1 factor percentiles and #5 breakouts: only rows dated on or before the origin are used."""
    X = pl.DataFrame({"security_id": ["S1", "S1"], "date": [date(2020, 3, 6), date(2020, 3, 13)], "direction": [1, -1]})
    fac = pl.DataFrame({"security_id": ["S1"] * 3, "date": [date(2020, 3, 5), date(2020, 3, 12), date(2020, 3, 14)],
                        "pct_hi52": [80.0, 20.0, 99.0], "pct_maxret": [50.0, 50.0, 0.0], "pct_ivol": [10.0, 90.0, 0.0]})
    j = F.join_factors(X, fac).sort("date")
    assert j["x_pct_hi52"].to_list() == [80.0, 20.0]                         # the 03-14 row (after both) is never used
    assert j["x_hi52_signed"].to_list() == pytest.approx([0.6, 0.6])         # (20 - 50) / 50 x -1
    bo = pl.DataFrame({"security_id": ["S1", "S1"], "date": [date(2020, 3, 4), date(2020, 3, 16)], "direction": [1, -1],
                       "confirmed": [True, True], "confirm_score": [80.0, 90.0], "relvol50": [2.0, 3.0]})
    b = F.join_breakouts(X, bo).sort("date")
    assert b["x_bo_align"].to_list() == [1.0, 0.0]                           # 03-04 up breakout aligned; none within 7 d
    assert b["x_bo_score_signed"].to_list() == pytest.approx([0.8, 0.0]) and b["x_bo_days"].to_list() == [2.0, 8.0]
