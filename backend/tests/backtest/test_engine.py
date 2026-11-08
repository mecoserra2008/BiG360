"""Backtesting lab engine core: hand-computed toy portfolios, cost math, construction rules, look-ahead and
label-permutation sanity checks, DSR / PBO against known values (pure, no lake)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from app.backtest import _engine as E


def bdays(a: date, n: int) -> list[date]:
    out, d = [], a
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def toy_px(R: np.ndarray, dates: list[date], ids: list[str], currency: list[str] | None = None) -> E.PricePanel:
    T, N = R.shape
    return E.PricePanel(dates=dates, ids=ids, R=R.astype(float), first=np.zeros(N, dtype=np.int64),
                        last=np.full(N, T - 1, dtype=np.int64), currency=currency or ["USD"] * N, rf=np.zeros(T))


def panel_rows(rows: list[tuple[date, str, float]], **extra: list) -> pl.DataFrame:
    df = pl.DataFrame({"date": [r[0] for r in rows], "entity_id": [r[1] for r in rows], "signal": [r[2] for r in rows]},
                      schema={"date": pl.Date, "entity_id": pl.Utf8, "signal": pl.Float64})
    df = df.with_columns(pl.lit(True).alias("eligible"))
    for k, v in extra.items():
        df = df.with_columns(pl.Series(k, v))
    return df


NOCOST = {"enabled": False}
DATES = bdays(date(2024, 1, 1), 6)  # Mon 1 .. Mon 8
RA = [0.0, 0.10, -0.05, 0.02, 0.0, 0.01]
RB = [0.0, -0.02, 0.03, 0.0, 0.05, -0.01]


def spec(**cons) -> dict:
    return {"construction": {"min_names": 1, **cons}, "costs": NOCOST}


# ------------------------------------------------------------------------------------------------ toy portfolios


def test_toy_long_top_name_hand_computed():
    px = toy_px(np.column_stack([RA, RB]), DATES, ["A", "B"])
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)])
    sim = E.simulate_rank(p, px, spec(quantiles=2))
    # signal at Jan 1 -> trade at the close of Jan 2 (t+1) -> earns Jan 3.. returns; Jan 2's +10% is NOT captured
    assert sim.dates[0] == DATES[1]
    nav = float(np.prod(1 + sim.ret))
    assert nav == pytest.approx(0.95 * 1.02 * 1.0 * 1.01, abs=1e-12)
    assert sim.holdings[0]["entity_id"] == "A" and sim.holdings[0]["weight"] == pytest.approx(1.0)


def test_toy_equal_weight_drift_hand_computed():
    px = toy_px(np.column_stack([RA, RB]), DATES, ["A", "B"])
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)])
    sim = E.simulate_rank(p, px, spec(selection="all"))
    va = 0.5 * 0.95 * 1.02 * 1.0 * 1.01
    vb = 0.5 * 1.03 * 1.00 * 1.05 * 0.99
    assert float(np.prod(1 + sim.ret)) == pytest.approx(va + vb, abs=1e-12)


def test_toy_long_short_dollar_neutral_hand_computed():
    px = toy_px(np.column_stack([RA, RB]), DATES, ["A", "B"])
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)])
    sim = E.simulate_rank(p, px, spec(side="long_short", quantiles=2))
    pa = 0.95 * 1.02 * 1.0 * 1.01
    pb = 1.03 * 1.0 * 1.05 * 0.99
    assert float(np.prod(1 + sim.ret)) == pytest.approx(1 + pa - pb, abs=1e-12)  # $1 long A, $1 short B, $1 collateral
    assert sim.n_long[-1] == 1 and sim.n_short[-1] == 1


def test_execution_lag_zero_trades_on_signal_close():
    px = toy_px(np.column_stack([RA, RB]), DATES, ["A", "B"])
    p = panel_rows([(DATES[1], "A", 2.0), (DATES[1], "B", 1.0)])
    sim = E.simulate_rank(p, px, spec(quantiles=2, execution_lag_days=0))
    assert sim.dates[0] == DATES[1]
    assert float(np.prod(1 + sim.ret)) == pytest.approx(0.95 * 1.02 * 1.0 * 1.01, abs=1e-12)


def test_delisted_position_goes_to_cash_with_delisting_return():
    R = np.column_stack([RA, RB])
    px = toy_px(R, DATES, ["A", "B"])
    px.last = np.array([3, 5])  # A stops trading after Jan 4
    R[4:, 0] = 0.0
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)])
    sim = E.simulate_rank(p, px, {**spec(quantiles=2), "delisting_return": -0.5})
    assert float(np.prod(1 + sim.ret)) == pytest.approx(0.95 * 1.02 * 0.5, abs=1e-12)
    assert sim.gross_exp[-1] == 0.0


def test_rebalance_month_end_and_overlap_tranches():
    dates = bdays(date(2024, 1, 1), 70)
    rng = np.random.default_rng(1)
    R = rng.normal(0, 0.01, (70, 4))
    px = toy_px(R, dates, list("ABCD"))
    me = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 29)]
    sig = {me[0]: [4, 3, 2, 1], me[1]: [1, 2, 3, 4], me[2]: [4, 3, 2, 1]}
    p = panel_rows([(d, e, float(v)) for d, vs in sig.items() for e, v in zip("ABCD", vs, strict=True)])
    sim = E.simulate_rank(p, px, spec(quantiles=4, holding_periods=2))
    by = {}
    for h in sim.holdings:
        by.setdefault(h["date"], {})[h["entity_id"]] = h["weight"]
    assert by[me[0]] == {"A": pytest.approx(1.0)}
    assert by[me[1]] == {"A": pytest.approx(0.5), "D": pytest.approx(0.5)}  # mean of the last 2 tranches
    assert by[me[2]] == {"D": pytest.approx(0.5), "A": pytest.approx(0.5)}


# ------------------------------------------------------------------------------------------------ costs


def test_cost_math_buckets_commission_impact_fx_borrow():
    c = E.CostModel.from_spec({"commission_bps": 1.0, "impact_coef": 0.1, "capital_usd": 1e7, "fx_bps": 2.0})
    adv = np.array([5e8, 2e9, 1e6, np.nan])
    assert c.half_spread_bps(adv).tolist() == [3.0, 1.0, 50.0, 50.0]
    dw = np.array([1.0, 0.5, 0.1, 0.1])
    vol = np.array([0.02, 0.01, 0.03, 0.03])
    fx = np.array([False, True, False, False])
    got = c.trade_cost(dw, adv, vol, fx, 1.0)
    exp0 = 1.0 * 4e-4 + 1.0 * 0.1 * 0.02 * math.sqrt(1e7 / 5e8)
    exp1 = 0.5 * (1 + 1 + 2) / 1e4 + 0.5 * 0.1 * 0.01 * math.sqrt(0.5e7 / 2e9)
    exp2 = 0.1 * 51e-4 + 0.1 * 0.1 * 0.03 * math.sqrt(1e6 / 1e6)
    exp3 = 0.1 * 51e-4 + 0.1 * 0.1 * 0.03 * 1.0  # unknown ADV: worst bucket, participation 1
    assert got.tolist() == pytest.approx([exp0, exp1, exp2, exp3], rel=1e-12)
    br = c.borrow_rate_daily(np.array([1e7, 1e6]))
    assert br.tolist() == pytest.approx([50e-4 / 252, 300e-4 / 252])


def test_cost_deducted_on_execution_day():
    px = toy_px(np.column_stack([RA, RB]), DATES, ["A", "B"])
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)], adv_usd=[5e8, 5e8], vol=[0.02, 0.02])
    s = {"construction": {"min_names": 1, "quantiles": 2},
         "costs": {"commission_bps": 1.0, "impact_coef": 0.0, "fx_bps": 0.0}}
    sim = E.simulate_rank(p, px, s)
    assert sim.cost[0] == pytest.approx(4e-4)
    assert float(np.prod(1 + sim.ret)) == pytest.approx((1 - 4e-4) * 0.95 * 1.02 * 1.0 * 1.01, abs=1e-12)


def test_short_borrow_charged_daily():
    R = np.zeros((6, 2))
    px = toy_px(R, DATES, ["A", "B"])
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)], adv_usd=[1e8, 1e8], vol=[0.0, 0.0])
    s = {"construction": {"min_names": 1, "quantiles": 2, "side": "long_short"},
         "costs": {"commission_bps": 0.0, "impact_coef": 0.0, "spread_model": "fixed", "fixed_spread_bps": 0.0,
                   "borrow_bps": 50.0}}
    sim = E.simulate_rank(p, px, s)
    # short weight grows slightly as the NAV pays the fee (weights are fractions of NAV)
    assert sim.borrow[1:].tolist() == pytest.approx([50e-4 / 252] * 4, rel=1e-3)


def test_turnover_cap_partial_rebalance():
    px = toy_px(np.zeros((6, 2)), DATES, ["A", "B"])
    p = panel_rows([(DATES[0], "A", 2.0), (DATES[0], "B", 1.0)])
    sim = E.simulate_rank(p, px, spec(quantiles=2, max_turnover=0.3))
    assert sim.rebalances[0]["turnover"] == pytest.approx(0.3)
    assert sim.gross_exp[-1] == pytest.approx(0.6)  # w = 0 + (0.3 / 0.5) x (1 - 0)


# ------------------------------------------------------------------------------------------------ construction


def test_cap_weights_redistribute():
    w, bad = E._cap_weights(np.array([1.0, 1.0, 1.0, 10.0]), 0.4, 1.0)
    assert not bad and w.sum() == pytest.approx(1.0) and w.max() <= 0.4 + 1e-12
    assert w.tolist() == pytest.approx([0.2, 0.2, 0.2, 0.4])
    w, bad = E._cap_weights(np.ones(2), 0.3, 1.0)
    assert bad and w.tolist() == [0.3, 0.3]


def test_sector_neutral_long_short_nets_zero_per_group():
    ids = [f"S{i}" for i in range(12)]
    px = toy_px(np.zeros((6, 12)), DATES, ids)
    grp = ["T"] * 6 + ["F"] * 6
    p = panel_rows([(DATES[0], e, float(i)) for i, e in enumerate(ids)], group_sector=grp)
    sim = E.simulate_rank(p, px, spec(side="long_short", quantiles=3, neutralize="sector"))
    net = {}
    for h in sim.holdings:
        net[h["group"]] = net.get(h["group"], 0.0) + h["weight"]
    assert set(net) == {"T", "F"} and all(abs(v) < 1e-12 for v in net.values())
    assert sum(h["weight"] for h in sim.holdings if h["weight"] > 0) == pytest.approx(1.0)


def test_weighting_schemes():
    ids = list("ABCD")
    px = toy_px(np.random.default_rng(3).normal(0, 0.01, (6, 4)), DATES, ids)
    p = panel_rows([(DATES[0], e, float(i)) for i, e in enumerate(ids)], mcap=[1.0, 1.0, 1.0, 3.0],
                   vol=[0.01, 0.01, 0.02, 0.04])
    cap = {h["entity_id"]: h["weight"] for h in E.simulate_rank(p, px, spec(quantiles=2, weighting="cap")).holdings}
    assert cap == {"C": pytest.approx(0.25), "D": pytest.approx(0.75)}
    iv = {h["entity_id"]: h["weight"] for h in E.simulate_rank(p, px, spec(quantiles=2, weighting="inverse_vol")).holdings}
    assert iv == {"C": pytest.approx(2 / 3), "D": pytest.approx(1 / 3)}


def test_erc_equals_inverse_vol_for_diagonal_covariance():
    sd = np.array([0.1, 0.2, 0.4])
    w = E._erc_weights(np.diag(sd ** 2))
    assert w.tolist() == pytest.approx((1 / sd / (1 / sd).sum()).tolist(), rel=1e-6)
    cov = np.array([[0.04, 0.01, 0.0], [0.01, 0.09, 0.02], [0.0, 0.02, 0.16]])
    w = E._erc_weights(cov, iters=200)
    rc = w * (cov @ w)
    assert np.ptp(rc / rc.sum()) < 1e-6  # equal risk contributions


def test_rank_pct_ties():
    assert E._rank_pct(np.array([3.0, 1.0, 1.0, 2.0])).tolist() == pytest.approx([0.875, 0.25, 0.25, 0.625])


# ------------------------------------------------------------------------------------------------ look-ahead / permutation


def synth(alpha: float, n: int = 60, months: int = 96, seed: int = 0):
    """Daily returns with a monthly cross-sectional signal: s known at month-end m predicts returns in month m+1
    (alpha per month, spread evenly over its trading days)."""
    rng = np.random.default_rng(seed)
    dates = bdays(date(2015, 1, 1), months * 22)
    D = np.array(dates, dtype="datetime64[D]")
    T = len(dates)
    R = rng.normal(0, 0.012, (T, n))
    ids = [f"E{i:03d}" for i in range(n)]
    me = sorted({date(d.year, d.month, 1) for d in dates})
    rows = []
    for k in range(len(me) - 1):
        m0 = me[k]
        end = max(d for d in dates if d.year == m0.year and d.month == m0.month)
        s = rng.normal(size=n)
        nxt = (D > np.datetime64(end)) & (D < np.datetime64(me[k + 1] + timedelta(days=40)))
        nxt &= np.array([d.month == me[k + 1].month for d in dates])
        days = np.where(nxt)[0]
        if len(days):
            R[days[1:], :] += alpha * s / max(len(days) - 1, 1)  # from the 2nd day (execution at day-1 close)
        rows += [(end, e, float(v)) for e, v in zip(ids, s, strict=True)]
    return toy_px(R, dates, ids), panel_rows(rows)


def sharpe(sim: E.SimResult) -> float:
    st = E.sharpe_stats(sim.ret)
    return st["sr"] * math.sqrt(252)


LS = {"construction": {"side": "long_short", "quantiles": 5, "min_names": 5}, "costs": NOCOST}


def test_true_signal_is_detected_and_stale_signal_does_not_improve():
    px, p = synth(alpha=0.02)
    base = sharpe(E.simulate_rank(p, px, LS))
    stale = sharpe(E.simulate_rank(E.shift_signal(p, 1), px, LS))
    assert base > 1.5
    assert stale < base and abs(stale) < 0.8  # the signal is i.i.d. monthly: one month stale it carries no information


def test_contemporaneous_return_signal_has_no_edge():
    """A signal equal to the return realised over the month ending at t must not earn that return (it is already
    in the price): with the t+1 execution lag the Sharpe is ~0 on random-walk prices."""
    srs = []
    for seed in range(8):  # one 8-year path has a null Sharpe s.d. of ~0.35-0.45: average over independent paths
        px, p = synth(alpha=0.0, seed=seed)
        L = px.L
        rows = []
        for t in sorted(p["date"].unique().to_list()):
            i1 = px.last_index_on_or_before(t)
            i0 = max(i1 - 21, 0)
            past = np.exp(L[i1] - L[i0]) - 1
            rows += [(t, e, float(v)) for e, v in zip(px.ids, past, strict=True)]
        srs.append(sharpe(E.simulate_rank(panel_rows(rows), px, LS)))
    assert abs(float(np.mean(srs))) < 0.45
    # the same information one step "too early" (look-ahead: trading on the close of t+1's own return) is huge
    rows_la = []
    for t in sorted(p["date"].unique().to_list()):
        e = px.day_index(t, "right")
        fwd = np.exp(L[min(e + 21, px.T - 1)] - L[e]) - 1
        rows_la += [(t, ent, float(v)) for ent, v in zip(px.ids, fwd, strict=True)]
    assert sharpe(E.simulate_rank(panel_rows(rows_la), px, LS)) > 5


def test_label_permutation_gives_zero_sharpe():
    px, p = synth(alpha=0.02, seed=2)
    perms = E.permutation_sharpes(p, px, LS, 30, seed=7)
    assert abs(float(np.mean(perms))) < 0.25
    assert sharpe(E.simulate_rank(p, px, LS)) > max(perms)


def test_decile_analysis_monotone_with_true_signal():
    px, p = synth(alpha=0.02, seed=4)
    d = E.decile_analysis(p, px, 1, 5, min_names=10)
    assert d["n_periods"] > 80 and d["ic_mean"] > 0.05 and d["spread_mean"] > 0.01
    assert d["monotonicity"] > 0.8


# ------------------------------------------------------------------------------------------------ events


def test_event_trade_time_exit_and_stop():
    R = np.zeros((6, 2))
    R[:, 0] = [0.0, 0.0, 0.05, 0.05, 0.05, 0.05]
    R[:, 1] = [0.0, 0.0, -0.06, -0.06, 0.0, 0.0]
    px = toy_px(R, DATES, ["A", "B"])
    ev = pl.DataFrame({"entity_id": ["A", "B"], "date": [DATES[0], DATES[0]], "direction": [1, 1], "strength": [1.0, 1.0]},
                      schema={"entity_id": pl.Utf8, "date": pl.Date, "direction": pl.Int8, "strength": pl.Float64})
    s = {"event": {"hold_days": 2, "stop_loss": 0.1, "max_positions": 2, "position_weight": 0.5}, "costs": NOCOST}
    sim = E.simulate_events(ev, px, s)
    tr = {t["entity_id"]: t for t in sim.trades}
    assert tr["A"]["exit_reason"] == "time" and tr["A"]["return"] == pytest.approx(1.05 ** 2 - 1)
    assert tr["B"]["exit_reason"] == "stop" and tr["B"]["return"] == pytest.approx(0.94 ** 2 - 1)
    assert tr["A"]["entry_date"] == DATES[1] and tr["A"]["exit_date"] == DATES[3]


# ------------------------------------------------------------------------------------------------ overfitting stats


def test_deflated_sharpe_known_value():
    """Bailey & Lopez de Prado (2014) numerical example: SR 2.5 (annual), T = 1250 daily obs, skew -3, kurtosis 10,
    N = 100 trials with variance 0.5 (annual) -> SR0 = 0.1132 (daily units) and DSR = 0.9004."""
    sr = 2.5 / math.sqrt(250)
    out = E.deflated_sharpe(sr, 1250, -3.0, 10.0, 100, 0.5 / 250)
    assert out["sr0"] == pytest.approx(0.1132, abs=2e-4)
    assert out["dsr"] == pytest.approx(0.9004, abs=1e-3)
    assert E.psr(0.0, 100, 0.0, 3.0) == pytest.approx(0.5)
    assert E.deflated_sharpe(sr, 1250, -3.0, 10.0, 1, 0.1)["dsr"] == pytest.approx(E.psr(sr, 1250, -3.0, 10.0))


def test_pbo_known_cases():
    T = 400
    rng = np.random.default_rng(0)
    dom = rng.normal(0, 0.01, (T, 6))
    dom[:, 2] += 0.01  # one configuration dominates in every block -> never overfit
    assert E.pbo_cscv(dom, 10)["pbo"] == 0.0
    # two configurations whose in-sample winner is always the out-of-sample loser
    x = np.zeros((T, 2))
    x[: T // 2, 0], x[T // 2:, 0] = 0.01, -0.01
    x[: T // 2, 1], x[T // 2:, 1] = -0.01, 0.01
    x += rng.normal(0, 1e-4, x.shape)
    assert E.pbo_cscv(x, 2)["pbo"] == 1.0
    noise = rng.normal(0, 0.01, (1000, 20))
    assert 0.25 < E.pbo_cscv(noise, 10)["pbo"] < 0.75


def test_walk_forward_picks_best_training_config():
    rng = np.random.default_rng(1)
    M = rng.normal(0, 0.01, (500, 3))
    M[:, 1] += 0.003
    wf = E.walk_forward(M, 4)
    assert wf["chosen"] == [1, 1, 1, 1] and len(wf["returns"]) == 500 - wf["start"]


def test_metrics_basics():
    dates = bdays(date(2020, 1, 1), 504)
    r = np.full(504, 0.001)
    r[100] = -0.2
    m = E.perf_metrics(r, dates, bench=np.full(504, 0.0005))
    assert m["max_drawdown"] == pytest.approx(-0.2)
    assert m["total_return"] == pytest.approx(float(np.prod(1 + r) - 1))
    assert m["hit_rate"] == pytest.approx(503 / 504)
    assert m["beta"] is None  # constant benchmark: beta undefined


# ------------------------------------------------------------------------------------------------ regression: cost blow-up


def test_cost_tiny_adv_residue_is_unknown_and_cost_is_capped():
    """Guide 11 / test copy 'ML 3M top decile': a zero-volume 20-day window left an ADV of ~5e-12 USD (rolling-mean
    residue), sqrt(notional / ADV) became ~1e9 and one trade cost thousands of times the NAV (CAGR -100%)."""
    c = E.CostModel.from_spec({"commission_bps": 1.0, "impact_coef": 0.1, "capital_usd": 1e7, "fx_bps": 2.0})
    dw = np.array([0.03, 0.03, 0.5])
    adv = np.array([5.09e-12, np.nan, 100.0])
    vol = np.array([0.02, 0.02, 0.03])
    got = c.trade_cost(dw, adv, vol, np.zeros(3, dtype=bool), 10.0)
    unknown = 0.03 * 51e-4 + 0.03 * 0.1 * 0.02 * 1.0  # worst spread bucket, participation 1
    assert got[0] == pytest.approx(unknown, rel=1e-12)
    assert got[1] == pytest.approx(unknown, rel=1e-12)
    assert got[2] == pytest.approx(0.5 * 0.10, rel=1e-12)  # $50M into a $100 ADV name: capped at 1000 bp
    assert c.n_capped == 1
    assert np.all(got <= np.abs(dw) * c.max_cost_bps / 1e4 + 1e-15)


def test_zero_volume_name_cannot_wipe_out_the_book():
    dates = bdays(date(2024, 1, 1), 70)
    R = np.full((70, 3), 0.001)
    px = toy_px(R, dates, ["A", "B", "C"])
    reb = [dates[0], dates[21], dates[42]]
    rows, adv, vol = [], [], []
    for k, d in enumerate(reb):
        for e, s in (("A", 3.0 - k), ("B", 2.0 + k), ("C", 1.0)):
            rows.append((d, e, s))
            adv.append(5.09e-12 if e == "A" else 5e8)
            vol.append(0.02)
    p = panel_rows(rows, adv_usd=adv, vol=vol)
    sim = E.simulate_rank(p, px, {"construction": {"min_names": 1, "quantiles": 3}, "costs": {"impact_coef": 0.1}})
    nav = np.cumprod(1 + sim.ret)
    assert nav.min() > 0.9
    assert float(sim.cost.max()) < 0.01 and float(sim.cost.sum()) < 0.03  # 71 bp per full switch at most


def test_nav_ruin_is_absorbing_and_warned():
    dates = bdays(date(2024, 1, 1), 8)
    R = np.zeros((8, 2))
    R[3, 0] = -1.0                                         # the only holding goes to zero
    R[5:, 0] = 0.5                                         # later prints must not resurrect a ruined NAV
    px = toy_px(R, dates, ["A", "B"])
    p = panel_rows([(dates[0], "A", 2.0), (dates[0], "B", 1.0)])
    sim = E.simulate_rank(p, px, spec(quantiles=2))
    nav = np.cumprod(1 + sim.ret)
    assert nav.min() == 0.0 and nav[-1] == 0.0
    assert np.all(sim.ret >= -1.0)
    assert any("NAV fell to zero" in w for w in sim.warnings)
