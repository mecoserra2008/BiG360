"""Technicals: pivots, structure, period levels, no-look-ahead properties, event outcomes, shrinkage, KDE zones,
R:R math and an end-to-end run of the three jobs on synthetic prices (owner: technicals)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import numpy as np
import polars as pl
import pytest

from app.technicals import kernels as K
from app.technicals import taxonomy as T
from app.technicals.confluence import build_setup, find_zones
from app.technicals.data import Bars
from app.technicals.level_backtest import DEFAULT_BT, combine, sufficient_stats, ticker_events
from app.technicals.levels import build_levels, snapshot


def make_bars(n: int = 900, seed: int = 1, start: str = "2020-01-06", sid: str = "SYN") -> Bars:
    rng = np.random.default_rng(seed)
    dates = np.busday_offset(np.datetime64(start, "D"), np.arange(n), roll="forward").astype("datetime64[D]")
    r = rng.normal(0.0003, 0.015, n)
    c = 100.0 * np.exp(np.cumsum(r))
    o = c * np.exp(rng.normal(0, 0.004, n))
    hi = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.006, n)))
    lo = np.minimum(o, c) * np.exp(-np.abs(rng.normal(0, 0.006, n)))
    v = rng.uniform(1e5, 5e5, n)
    return Bars(sid, dates, o, hi, lo, c, v, earnings=dates[[100, 350, 600]] if n > 600 else dates[:0])


# ------------------------------------------------------------------------------------------------ pivots / structure

def test_fractal_flags_marks_strict_local_extremes():
    h = np.array([1, 2, 5, 2, 1, 2, 3, 2, 1, 2.5, 1], dtype=float)
    l = h - 0.5  # noqa: E741
    fh, fl = K.fractal_flags(h, l, 2)
    assert list(np.flatnonzero(fh)) == [2, 6]
    assert list(np.flatnonzero(fl)) == [4, 8]  # ties on the right are allowed (>=), strict on the left
    fh3, _ = K.fractal_flags(h, l, 3)
    assert list(np.flatnonzero(fh3)) == [6]  # index 2 lacks 3 bars on the left


def test_zigzag_confirms_after_reversal_and_structure_labels():
    # up 0->10, down to 4, up to 14, down to 8, up to 20 (triangle waves, ATR=1, mult=3)
    path = np.r_[np.linspace(0, 10, 11), np.linspace(9, 4, 6), np.linspace(5, 14, 10), np.linspace(13, 8, 6), np.linspace(9, 20, 12)]
    h, l, c = path + 0.1, path - 0.1, path  # noqa: E741
    atr = np.ones_like(path)
    idx, px, d, conf = K.zigzag(h, l, atr, 3.0)
    # pivots: low @0 (idx 0), high @10 (idx 10), low @4 (idx 16), high @14 (idx 26), low @8 (idx 32)
    assert list(idx[:5]) == [0, 10, 16, 26, 32]
    assert list(d[:5]) == [-1, 1, -1, 1, -1]
    assert all(conf[i] > idx[i] for i in range(5))
    # confirmation = first bar whose opposite extreme is >= 3 ATR from the extreme
    assert h[10] - l[conf[1]] >= 3.0 and h[10] - l[conf[1] - 1] < 3.0
    bb, bp, bk, bd, bi, trend = K.structure_breaks(c, idx, px, d, conf)
    # close above the first high (10.1) -> first break (CHoCH, trend was 0), then above 14.1 -> BOS up
    assert bd[0] == 1 and bk[0] == 2 and np.isclose(bp[0], 10.1)
    assert bd[-1] == 1 and bk[-1] == 1 and np.isclose(bp[-1], 14.1)
    assert trend[-1] == 1


def test_period_levels_prev_week_high_active_only_next_week():
    b = make_bars(60)
    ls = build_levels(b)
    kid = T.KEY_ID["prev_high@W"]
    wk = (b.dates.astype(np.int64) + 3) // 7
    weeks = np.unique(wk)
    sel = np.flatnonzero(ls.key == kid)
    for i in sel[:5]:
        st = ls.start[i]
        src = wk[ls.anchor[i]]
        assert ls.px[i] == pytest.approx(b.h[wk == src].max())
        assert wk[st] == weeks[np.searchsorted(weeks, src) + 1]  # starts on the first bar of the following week
        assert (ls.end[i] >= len(b.c)) or wk[ls.end[i]] != wk[st]


def test_snapshot_rolls_period_levels_on_last_session_of_week():
    b = make_bars(80)
    # truncate at a Friday: next session is Monday -> prev_high@W must be this week's high
    wd = (b.dates.astype(np.int64) + 3) % 7  # Monday = 0
    t = int(np.flatnonzero(wd == 4)[-2]) + 1
    bt = Bars(b.security_id, b.dates[:t], b.o[:t], b.h[:t], b.l[:t], b.c[:t], b.v[:t])
    snap = snapshot(build_levels(bt), bt)
    week = (bt.dates.astype(np.int64) + 3) // 7
    this_week_high = bt.h[week == week[-1]].max()
    ph = snap.filter(pl.col("level_key") == "prev_high@W")
    assert ph.height == 1 and ph["price"][0] == pytest.approx(this_week_high)
    assert "cur_open@W" not in snap["level_key"].to_list()  # next week's open is unknown


# ------------------------------------------------------------------------------------------------ no look-ahead

def _active(ls, T_):
    m = (ls.start <= T_) & (ls.end > T_)
    vp = np.isin(ls.key, [T.KEY_ID[k] for k in T.KEY_ID if k.startswith("vp_")])
    m &= ~vp
    return sorted(zip(ls.key[m].tolist(), np.round(ls.px[m], 9).tolist(), ls.anchor[m].tolist(), strict=True))


@pytest.mark.parametrize("cut", [400, 517, 650, 801])
def test_levels_known_at_t_do_not_depend_on_future_bars(cut):
    b = make_bars(900, seed=3)
    full = build_levels(b)
    bt = Bars(b.security_id, b.dates[:cut], b.o[:cut], b.h[:cut], b.l[:cut], b.c[:cut], b.v[:cut], earnings=b.earnings)
    trunc = build_levels(bt)
    assert _active(full, cut) == _active(trunc, cut)
    # series values usable at bars <= cut are identical
    for k in np.unique(trunc.s_key):
        a = full.s_px[full.s_key == k][:, : cut + 1]
        bb = trunc.s_px[trunc.s_key == k][:, : cut + 1]
        np.testing.assert_allclose(np.sort(a, axis=0), np.sort(bb, axis=0), rtol=1e-12, equal_nan=True)


def test_volume_profile_levels_use_only_the_trailing_window():
    b = make_bars(400, seed=5)
    code, px, st, en, sg, an = K.volume_profile_levels(b.h, b.l, b.v, 126, 5, 60, 3)
    r = int(st[np.argmin(st)])
    # recompute the profile on bars[:r] only: same POC as the instance starting at r
    code2, px2, st2, *_ = K.volume_profile_levels(b.h[:r], b.l[:r], b.v[:r], 126, 5, 60, 3)
    poc_full = px[(st == r) & (code == 0)][0]
    poc_trunc = px2[(st2 == r) & (code2 == 0)][0]
    assert poc_full == pytest.approx(poc_trunc)
    assert (an[st == r] < r).all()


def test_backtest_events_resolved_before_cut_are_identical_on_truncated_data():
    b = make_bars(900, seed=7)
    cut = 700
    bt = Bars(b.security_id, b.dates[:cut], b.o[:cut], b.h[:cut], b.l[:cut], b.c[:cut], b.v[:cut], earnings=b.earnings)
    vp = [T.KEY_ID[k] for k in T.KEY_ID if k.startswith("vp_")]
    ef = ticker_events(b, with_control=False).filter(~pl.col("key").is_in(vp))
    et = ticker_events(bt, with_control=False).filter(~pl.col("key").is_in(vp))
    last = bt.dates[-1].astype("datetime64[D]").item()
    ef = ef.filter(pl.col("res_date") <= last).sort(["key", "t", "side"])
    et = et.sort(["key", "t", "side"])
    assert ef.height > 100
    cols = ["key", "t", "side", "hit", "brk", "mfe_atr", "mae_atr"]
    assert ef.select(cols).equals(et.select(cols))


# ------------------------------------------------------------------------------------------------ event outcomes

def _bars(rows):
    a = np.asarray(rows, dtype=float)
    return a[:, 0].copy(), a[:, 1].copy(), a[:, 2].copy(), a[:, 3].copy()


def test_outcome_hit_break_and_conservative_same_bar_rule():
    # support at 100, ATR 2 -> band 0.5; hit needs +2 before a close < 99
    base = [(103, 104, 102, 103)] * 3
    o, h, l, c = _bars(base + [(102, 102.5, 100.2, 101.0), (101, 102.5, 100.5, 102), (102, 102.4, 101, 102)])  # noqa: E741
    hit, brk, res, mfe, mae, ft, r = K._outcome(o, h, l, c, 3, 100.0, 1, 2.0, 10, 1.0, 0.5, 0.5, 0.5, 2.0)
    assert hit and not brk and res == 4  # high 102.5 - 100 = 2.5 >= 2 on bar 4
    o, h, l, c = _bars(base + [(102, 102.5, 100.2, 101.0), (101, 103.0, 98.0, 98.5)])  # noqa: E741
    hit, brk, res, *_ = K._outcome(o, h, l, c, 3, 100.0, 1, 2.0, 10, 1.0, 0.5, 0.5, 0.5, 2.0)
    assert brk and not hit and res == 4  # intrabar +3 but close below 99 -> break wins
    o, h, l, c = _bars(base + [(102, 102.5, 99.5, 98.8)])  # noqa: E741
    hit, brk, res, *_ = K._outcome(o, h, l, c, 3, 100.0, 1, 2.0, 10, 1.0, 0.5, 0.5, 0.5, 2.0)
    assert brk and res == 3  # touch bar itself closes through


def test_scan_events_open_side_rule_and_censoring():
    n = 60
    c = np.full(n, 105.0)
    o, h, l = c.copy(), c + 0.5, c - 0.5  # noqa: E741
    l[20] = 100.1  # dip to the level from above
    atr = np.full(n, 2.0)
    out = K.scan_events(o, h, l, c, atr, np.array([0]), np.array([100.0]), np.array([0]), np.array([n]), np.array([0]),
                        np.zeros((0, n + 1)), np.zeros(0, np.int64), 1, 10, 1.0, 0.5, 0.25, 0.5, 2.0, 100)
    m = out[0]
    assert m == 1 and out[2][0] == 20 and out[3][0] == 1  # one support test at bar 20
    l[55] = 100.1  # too close to the end: outcome window not observed -> censored
    out = K.scan_events(o, h, l, c, atr, np.array([0]), np.array([100.0]), np.array([0]), np.array([n]), np.array([0]),
                        np.zeros((0, n + 1)), np.zeros(0, np.int64), 1, 10, 1.0, 0.5, 0.25, 0.5, 2.0, 100)
    assert out[0] == 1


def test_combine_shrinks_ticker_toward_pool_and_scores_edge():
    rows = []
    for sid, n, hits, cn, ch in (("A", 10, 9, 10, 5), ("B", 4000, 2400, 4000, 2000)):
        for ctrl, nn, hh in ((False, n, hits), (True, cn, ch)):
            rows.append({"as_of_date": date(2026, 1, 2), "security_id": sid, "pool_key": "X|US", "key": 0, "is_control": ctrl,
                         "n": nn, "hits": hh, "holds": hh, "breaks": nn - hh, "n_sup": nn, "hits_sup": hh, "sum_mfe": float(nn),
                         "sum_mae": 0.5 * nn, "sum_mfe_pct": 0.01 * nn, "n_ft": 0, "sum_ft": 0.0, "n_r": nn, "sum_r": 0.1 * nn})
    out = combine(pl.DataFrame(rows), DEFAULT_BT, ("as_of_date",))
    a = out.filter(pl.col("security_id") == "A").row(0, named=True)
    b = out.filter(pl.col("security_id") == "B").row(0, named=True)
    assert a["hit_rate"] == pytest.approx(0.9)
    pool = out.filter(pl.col("security_id") == "*X|US").row(0, named=True)
    # 10 events cannot move the estimate away from the (shrunk) pool rate under a 2000+ pseudo-event prior
    assert abs(a["hit_rate_shrunk"] - pool["hit_rate_shrunk"]) < 0.005
    assert b["hit_rate_shrunk"] > pool["hit_rate_shrunk"] - 0.01
    assert b["edge"] > 0.02 and b["score"] > 0.7 and b["score"] > a["score"] - 1e-9
    assert {"*", "*X|US"} <= set(out["security_id"].to_list())
    assert a["rank"] == 1 and out["level_key"][0] == T.KEY_BY_ID[0]


# ------------------------------------------------------------------------------------------------ confluence

def _levels(prices_types):
    return pl.DataFrame({
        "price": [p for p, _ in prices_types], "weight": [1.0] * len(prices_types),
        "level_key": [f"{t}@NA" for _, t in prices_types], "level_type": [t for _, t in prices_types],
        "family": [T.FAMILY_OF_TYPE.get(t, "swing_pivot") for _, t in prices_types],
        "type_rank_score": [0.5] * len(prices_types),
    })


def test_kde_zones_cluster_nearby_levels_and_rank_by_strength():
    lv = _levels([(99.9, "ms_hl"), (100.0, "avwap_ytd"), (100.1, "vp_poc_1y"), (100.15, "sma_200"), (100.05, "gap_up"),
                  (110.0, "ms_hh"), (110.1, "high_52w"), (110.2, "round_major"), (105.0, "sma_50")])
    zones = find_zones(lv, close=104.0, atr=1.0)
    z1 = zones[0]
    assert z1["rank"] == 1 and z1["side"] == "support"
    assert z1["zone_low"] <= 99.9 and z1["zone_high"] >= 100.15
    assert z1["member_count"] == 5 and z1["n_families"] == 5
    res = [z for z in zones if z["side"] == "resistance"]
    assert any(z["zone_low"] <= 110.0 <= z["zone_high"] and z["member_count"] == 3 for z in res)
    assert z1["strength"] > max(z["strength"] for z in res)
    # zone plan: long from the support zone, target = nearest resistance zone center above price
    assert z1["entry"] == pytest.approx(z1["zone_high"])
    assert z1["invalidation"] == pytest.approx(z1["zone_low"] - 0.5)
    first_res = min((z for z in zones if z["side"] == "resistance"), key=lambda z: z["center"])
    assert z1["target"] == pytest.approx(first_res["center"])
    assert z1["reward_risk"] == pytest.approx((z1["target"] - z1["entry"]) / (z1["entry"] - z1["invalidation"]))


def test_identical_prices_are_not_double_counted():
    one = _levels([(100.0, "swing_low_k2")])
    six = _levels([(100.0, t) for t in ("swing_low_k2", "swing_low_k3", "swing_low_k5", "swing_low_k10", "ms_hl", "low_52w")])
    s1 = find_zones(one, 104.0, 1.0)[0]["strength"]
    s6 = find_zones(six, 104.0, 1.0)[0]["strength"]
    assert s6 < 4 * s1  # merged: max + 0.25 x rest (+ diversity), not 6x


def test_setup_rr_math_and_score_bounds():
    zones = [
        {"zone_id": 1, "rank": 1, "side": "support", "zone_low": 98.0, "zone_high": 99.0, "center": 98.5, "strength": 70.0,
         "distance_atr": -1.5, "member_level_keys": ["ms_hl@NA"]},
        {"zone_id": 2, "rank": 2, "side": "resistance", "zone_low": 104.0, "zone_high": 105.0, "center": 104.5, "strength": 60.0,
         "distance_atr": 4.5, "member_level_keys": ["ms_hh@NA"]},
        {"zone_id": 3, "rank": 3, "side": "resistance", "zone_low": 108.0, "zone_high": 109.0, "center": 108.5, "strength": 50.0,
         "distance_atr": 8.5, "member_level_keys": ["high_52w@NA"]},
    ]
    ctx = {"close": 100.0, "atr": 1.0, "rsi": 55.0, "sma50": 97.0, "sma200": 90.0, "slope50": 1.0, "structure_state": "bullish",
           "last_break_kind": "bos", "last_break_dir": "up", "last_break_date": date(2026, 9, 1), "last_break_price": 97.0}
    s = build_setup(zones, ctx)
    assert s["bias"] == "long" and s["trend"] == "up"
    assert s["entry"] == 99.0 and s["invalidation"] == pytest.approx(97.5)
    assert s["target1"] == 104.5 and s["target2"] == 108.5
    assert s["reward_risk"] == pytest.approx((104.5 - 99.0) / (99.0 - 97.5))
    assert 0 <= s["technical_score"] <= 100 and s["support_zone_id"] == 1 and s["resistance_zone_id"] == 2
    assert "zone #1" in s["note"]
    bear = dict(ctx, sma50=95.0, sma200=110.0, slope50=-1.0, structure_state="bearish", close=100.0)
    sb = build_setup(zones, bear)
    assert sb["bias"] == "short" and sb["entry"] == 104.0 and sb["invalidation"] == pytest.approx(105.5)
    assert sb["target1"] == 98.5 and sb["reward_risk"] == pytest.approx((104.0 - 98.5) / (105.5 - 104.0))
    assert sb["technical_score"] < s["technical_score"]


# ------------------------------------------------------------------------------------------------ jobs end to end

def _write_prices(bars_list):
    from app.core.storage import write_table

    rows = []
    for b in bars_list:
        n = b.n
        rows.append(pl.DataFrame({
            "security_id": [b.security_id] * n, "listing_id": ["L" + b.security_id] * n,
            "date": b.dates.astype("datetime64[D]"), "region": ["US"] * n,
            "year": b.dates.astype("datetime64[Y]").astype(int) + 1970,
            "open": b.o, "high": b.h, "low": b.l, "close": b.c, "volume": b.v, "adj_factor": [1.0] * n, "split_factor": [1.0] * n,
            "adj_close": b.c, "currency": ["USD"] * n, "source_id": ["yfinance"] * n, "ingested_at": [datetime.now(UTC)] * n,
        }))
    write_table(pl.concat(rows).with_columns(pl.col("date").cast(pl.Date)), "prices_daily")


def test_jobs_end_to_end(tmp_data):
    from app.core.storage import read_table
    from app.scheduler.base import JobContext
    from app.technicals import confluence, level_backtest, levels

    bars = [make_bars(1400, seed=s, start="2021-01-04", sid=f"S{s:012d}") for s in range(4)]
    _write_prices(bars)
    as_of = bars[0].dates[-1].astype("datetime64[D]").item()
    ctx = JobContext(job_id="level_type_backtest", run_id="Rtest", as_of=as_of, params={"years": 4, "workers": 2})
    r = level_backtest.run(ctx)
    assert r.status == "success", r.message
    st = read_table("level_type_stats")
    assert set(st["security_id"].unique()) >= {b.security_id for b in bars} | {"*"}
    assert st["score"].drop_nulls().is_between(0, 1).all()
    r = levels.run(JobContext(job_id="tech_levels", run_id="Rtest2", as_of=as_of, params={"workers": 2}))
    assert r.status == "success", r.message
    tl = read_table("tech_levels")
    assert tl["security_id"].n_unique() == 4 and tl["type_rank_score"].null_count() == 0
    assert tl.filter(pl.col("known_from") > as_of)["level_key"].len() > 0  # next-session levels
    r = confluence.run(JobContext(job_id="confluence_zones", run_id="Rtest3", as_of=as_of, params={"workers": 2}))
    assert r.status == "success", r.message
    cz = read_table("confluence_zones")
    ts = read_table("tech_setups")
    assert ts.height == 4 and ts["technical_score"].is_between(0, 100).all()
    assert cz["strength"].is_between(0, 100).all() and (cz["zone_low"] <= cz["zone_high"]).all()
    assert cz.group_by("security_id").agg(pl.col("rank").min())["rank"].to_list() == [1, 1, 1, 1]
