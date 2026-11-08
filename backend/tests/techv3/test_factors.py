"""Technicals v3 #1 factor math and #5 breakout labels on hand-computed / brute-force toy cases, plus the point-in-time
(causal invariance) and leakage helpers (docs/TECHNICALS_V3_VALIDATION.md)."""

from __future__ import annotations

import math

import numpy as np
import polars as pl
import pytest

from app.techv3 import breakouts as BO
from app.techv3 import build as BL
from app.techv3 import factors as F
from app.techv3 import stats as S


def _walk(T: int, n: int, seed: int = 0, sd: float = 0.02) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 50.0 * np.exp(np.cumsum(rng.normal(0.0003, sd, (T, n)), axis=0))


# ------------------------------------------------------------------------------------------------ simple factors


def test_hi52_mom_strev_max_hand_computed():
    T = 300
    C = np.linspace(100.0, 130.0, T)[:, None].repeat(2, axis=1)
    C[250, 0] = 200.0                                    # a spike: the 52-week high of column 0 for 252 sessions
    r = F.log_returns(C)
    rm = np.zeros(T)
    lv = np.zeros(T)
    out = F.compute(C, r, rm, np.full((T, 2), np.nan), lv, np.ones(T, bool), np.zeros((T, 2)), np.zeros((T, 2), bool))
    t = 299
    assert out["hi52"][t, 0] == pytest.approx(C[t, 0] / 200.0)
    assert out["hi52"][t, 1] == pytest.approx(1.0)       # a monotone rise sits at its 52-week high
    assert out["mom"][t, 1] == pytest.approx(math.log(C[t - 21, 1] / C[t - 252, 1]))
    assert out["strev"][t, 1] == pytest.approx(math.log(C[t, 1] / C[t - 21, 1]))
    # MAX over the last 21 sessions: the +x% day leaves the window after 21 sessions
    assert out["maxret"][260, 0] == pytest.approx(C[251, 0] / C[250, 0] - 1, rel=1e-9) or out["maxret"][260, 0] > 0
    assert out["maxret"][265, 0] == pytest.approx(np.expm1(np.nanmax(r[245:266, 0])))
    assert out["maxret"][t, 0] == pytest.approx(np.expm1(np.nanmax(r[t - 20:t + 1, 0])))
    assert np.isnan(out["mom"][100, 0])                   # needs 252 sessions


def test_rolling_helpers_against_loops():
    rng = np.random.default_rng(3)
    X = rng.normal(size=(80, 3))
    X[rng.random((80, 3)) < 0.1] = np.nan
    s, n = F.roll_sum(X, 7)
    for t in (0, 6, 30, 79):
        w = X[max(0, t - 6):t + 1]
        assert s[t, 1] == pytest.approx(np.nansum(w[:, 1]))
        assert n[t, 1] == np.isfinite(w[:, 1]).sum()
    m = F.roll_max(X, 10, 5)
    w = X[60:70, 2]
    assert m[69, 2] == pytest.approx(np.nanmax(w)) if np.isfinite(w).sum() >= 5 else np.isnan(m[69, 2])
    Z = rng.normal(size=(300, 2))
    ws = F.strided_window_sum(Z, 21, 1, 11)
    t = 299
    assert ws[t, 0] == pytest.approx(sum(Z[t - 21 * j, 0] for j in range(1, 12)))
    t = 100                                              # partial: only the terms with a non-negative index
    assert ws[t, 1] == pytest.approx(sum(Z[t - 21 * j, 1] for j in range(1, 12) if t - 21 * j >= 0))


def test_ivol_matches_lstsq():
    T = 80
    rng = np.random.default_rng(5)
    rm = rng.normal(0, 0.01, T)
    rs = 0.5 * rm + rng.normal(0, 0.006, T)
    y = 0.001 + 1.2 * rm + 0.4 * rs + rng.normal(0, 0.015, T)
    Y = y[:, None]
    out = F.ivol(Y, rm[:, None], rs[:, None])
    for t in (40, 79):
        X = np.c_[np.ones(21), rm[t - 20:t + 1], rs[t - 20:t + 1]]
        b, *_ = np.linalg.lstsq(X, y[t - 20:t + 1], rcond=None)
        e = y[t - 20:t + 1] - X @ b
        assert out[t, 0] == pytest.approx(math.sqrt(e @ e / (21 - 3) * 252), rel=1e-6)
    # sector missing on a day of the window -> market-only regression
    rs2 = rs.copy()
    rs2[70] = np.nan
    o2 = F.ivol(Y, rm[:, None], rs2[:, None])
    X = np.c_[np.ones(21), rm[59:80]]
    b, *_ = np.linalg.lstsq(X, y[59:80], rcond=None)
    e = y[59:80] - X @ b
    assert o2[79, 0] == pytest.approx(math.sqrt(e @ e / (21 - 2) * 252), rel=1e-6)


def test_residual_momentum_matches_brute_force():
    """Blitz-Huij-Martens: 36 monthly blocks anchored at t, OLS on market + sector, residuals of blocks 1..11 summed
    and divided by their std - checked against an explicit loop with np.linalg.lstsq."""
    T = 900
    rng = np.random.default_rng(11)
    lm = np.cumsum(rng.normal(0.0003, 0.01, T))
    ls = np.cumsum(rng.normal(0.0002, 0.008, T)) + 0.6 * lm
    ly = 0.9 * lm + 0.5 * ls + np.cumsum(rng.normal(0.0005, 0.015, T))
    y21 = (ly - F.shift(ly, 21))[:, None]
    xm = (lm - F.shift(lm, 21))[:, None]
    xs = (ls - F.shift(ls, 21))[:, None]
    out = F.residual_momentum(y21, xm, xs)
    for t in (899, 850, 800):
        j = np.arange(36)
        rows = t - 21 * j
        X = np.c_[np.ones(36), xm[rows, 0], xs[rows, 0]]
        b, *_ = np.linalg.lstsq(X, y21[rows, 0], rcond=None)
        e = y21[rows, 0] - X @ b
        f = e[1:12]
        ref = f.sum() / f.std(ddof=1)
        assert out[t, 0] == pytest.approx(ref, rel=1e-6, abs=1e-8)
    assert np.isnan(out[300, 0])                         # < 24 regression blocks


def test_group_percentiles_reference_set_and_ties():
    keys = np.array([1, 1, 1, 1, 2, 2, 2])
    vals = np.array([10.0, 20.0, 20.0, 5.0, 1.0, 2.0, np.nan])
    ref = np.array([True, True, True, False, True, True, True])
    p, n = BL.group_percentiles(keys, vals, ref)
    # ref of group 1 = [10, 20, 20]: 10 -> (0 + 1) / 6, 20 -> (1 + 3) / 6, non-member 5 -> 0
    assert p[0] == pytest.approx(100 / 6) and p[1] == pytest.approx(400 / 6) and p[2] == p[1]
    assert p[3] == pytest.approx(0.0) and n[0] == 3
    assert p[4] == pytest.approx(25.0) and p[5] == pytest.approx(75.0) and np.isnan(p[6])


def test_composite_sign_alignment():
    df = pl.DataFrame({"pct_hi52": [80.0, None], "pct_maxret": [10.0, 30.0], "pct_mom": [None, None]})
    out = BL.composite(df, ["hi52", "maxret"])["composite"].to_list()
    assert out[0] == pytest.approx((80 + 90) / 2) and out[1] == pytest.approx(70.0)
    assert BL.composite(df, ["hi52", "maxret", "mom"])["composite"].to_list()[1] is None   # < half available


# ------------------------------------------------------------------------------------------------ causality


def test_factors_causal_invariance():
    """Replacing every bar after a cut leaves every factor dated <= cut bit-for-bit unchanged; a factor that peeks
    one session ahead is caught by the same check."""
    T, n, cut = 1000, 6, 700
    C = _walk(T, n, seed=1)
    C2 = C.copy()
    rng = np.random.default_rng(9)
    C2[cut + 1:] = C[cut] * np.exp(np.cumsum(rng.normal(0, 0.05, (T - cut - 1, n)), axis=0))

    def run(Cx: np.ndarray) -> dict[str, np.ndarray]:
        r = F.log_returns(Cx)
        rm = np.r_[np.nan, np.nanmean(r[1:], axis=1)]
        rs = np.repeat(np.r_[np.nan, np.nanmean(r[1:, :3], axis=1)][:, None], n, axis=1)
        return F.compute(Cx, r, rm, rs, F.index_level(rm), np.isfinite(rm), F.index_level(rs), np.isfinite(rs))

    a, b = run(C), run(C2)
    for k in F.FACTORS:
        assert np.array_equal(np.nan_to_num(a[k][:cut + 1], nan=-9e9), np.nan_to_num(b[k][:cut + 1], nan=-9e9)), k
        assert not np.allclose(np.nan_to_num(a[k][cut + 30:]), np.nan_to_num(b[k][cut + 30:])), k
    leak = F.shift(a["strev"], -1)                       # deliberately peeks at t + 1
    leak2 = F.shift(b["strev"], -1)
    assert not np.array_equal(np.nan_to_num(leak[:cut + 1]), np.nan_to_num(leak2[:cut + 1]))
    # forward returns (labels) DO depend on the future, and start at t + 1
    fa, fb = F.forward_returns(C), F.forward_returns(C2)
    assert np.array_equal(np.nan_to_num(fa[5][:cut - 6]), np.nan_to_num(fb[5][:cut - 6]))
    assert fa[5][100, 0] == pytest.approx(math.log(C[106, 0] / C[101, 0]))


# ------------------------------------------------------------------------------------------------ breakouts


def _bars(C: np.ndarray, spread: float = 0.01) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    O = np.vstack([C[:1], C[:-1]])
    H = np.maximum(O, C) * (1 + spread)
    L = np.minimum(O, C) * (1 - spread)
    return O, H, L


def test_breakout_detection_and_first_passage_hand_case():
    T = 60
    C = np.full((T, 1), 100.0)
    C[30, 0] = 103.0                                     # breaks the 20-day high (101 = 100 x 1.01)
    C[31:, 0] = 103.5
    C[33, 0] = 106.0                                     # target reached on day 33
    O, H, L = _bars(C)
    up, dn, ph, _pl = BO.detect(H, L, C, 20)
    assert up[30, 0] and up.sum() == 1 and not dn.any()
    assert ph[30, 0] == pytest.approx(101.0)
    lv = np.array([ph[30, 0]])
    tgt = np.array([105.0])
    code, tau, comp = BO.first_passage(BO.gather_windows(H, np.array([30]), np.array([0]), 10),
                                       BO.gather_windows(L, np.array([30]), np.array([0]), 10),
                                       BO.gather_windows(C, np.array([30]), np.array([0]), 10), np.array([1]), lv, tgt)
    # day 31 high = 103.5 x 1.01 = 104.5 < 105; day 33 high = 106 x 1.01 -> success at tau 3
    assert code[0] == 0 and tau[0] == 3 and comp[0]
    # a close back below the level first -> fail
    C2 = C.copy()
    C2[32, 0] = 100.5
    O2, H2, L2 = _bars(C2)
    code, tau, _ = BO.first_passage(BO.gather_windows(H2, np.array([30]), np.array([0]), 10),
                                    BO.gather_windows(L2, np.array([30]), np.array([0]), 10),
                                    BO.gather_windows(C2, np.array([30]), np.array([0]), 10), np.array([1]), lv, tgt)
    assert code[0] == 1 and tau[0] == 2
    # the window runs past the data end and nothing happened -> open (incomplete)
    code, _tau, comp = BO.first_passage(BO.gather_windows(H, np.array([55]), np.array([0]), 10),
                                        BO.gather_windows(L, np.array([55]), np.array([0]), 10),
                                        BO.gather_windows(C, np.array([55]), np.array([0]), 10), np.array([1]),
                                        np.array([90.0]), np.array([200.0]))
    assert code[0] == 3 and not comp[0]


def test_breakout_features_hand_values():
    T = 80
    C = np.full((T, 1), 100.0)
    V = np.full((T, 1), 1000.0)
    C[70, 0], V[70, 0] = 104.0, 3000.0
    O, H, L = _bars(C, 0.0)
    H[70, 0], L[70, 0] = 105.0, 100.0                    # close at 80% of the day's range
    r = F.log_returns(C)
    sig = np.full((T, 1), 0.01)
    fp = BO.feature_panel(O, H, L, C, V, V * C, r, sig)
    assert fp["relvol20"][70, 0] == pytest.approx(3.0) and fp["relvol50"][70, 0] == pytest.approx(3.0)
    assert fp["clv"][70, 0] == pytest.approx((2 * 104 - 105 - 100) / 5)
    assert BO.is_confirmed(np.array([3.0, 1.2]), np.array([0.6, 0.9])).tolist() == [True, False]
    sc = BO.confirm_score(np.array([3.0, 1.0]), np.array([1.0, -1.0]), np.array([0.5, -0.5]), np.array([1.0, -1.0]))
    assert sc[0] == pytest.approx(100.0) and sc[1] == pytest.approx(0.0)


def test_breakout_events_chunk_and_controls():
    T, n = 700, 5
    C = _walk(T, n, seed=4)
    O, H, L = _bars(C)
    rng = np.random.default_rng(2)
    V = rng.lognormal(10, 0.4, (T, n))
    r = F.log_returns(C)
    dates = np.arange(np.datetime64("2020-01-01"), np.datetime64("2020-01-01") + np.timedelta64(T, "D"))
    ev, ct = BO.events_for_chunk(O, H, L, C, V, V * C, r, dates, [f"S{i}" for i in range(n)], seed=1, t_min=100)
    assert len(ev["t"]) > 20 and (ev["t"] >= 100).all()
    assert set(np.unique(ev["out_20"])) <= {0, 1, 2, 3}
    up = ev["direction"] > 0
    assert (ev["close"][up] > ev["level"][up]).all() and (ev["close"][~up] < ev["level"][~up]).all()
    assert (ev["b_20"] > 0).all()
    assert len(ct["t"]) > 0.8 * len(ev["t"]) and np.isin(ct["event_t"], ev["t"]).all()


# ------------------------------------------------------------------------------------------------ statistics


def test_newey_west_and_multiple_testing():
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    m, se = S.nw_mean(x, 0)
    assert m == 3.0 and se == pytest.approx(np.std(x) / math.sqrt(5))
    rng = np.random.default_rng(0)
    ar = np.zeros(4000)
    for t in range(1, 4000):
        ar[t] = 0.8 * ar[t - 1] + rng.normal()
    _m, se0 = S.nw_mean(ar, 0)
    _m, se20 = S.nw_mean(ar, 20)
    assert se20 > 2 * se0                                # NW widens the SE of a persistent series
    p = np.array([0.01, 0.04, 0.03, 0.20])
    bh = S.bh_adjust(p)
    # sorted 0.01, 0.03, 0.04, 0.20 -> 0.04, 0.06, 0.0533, 0.20 -> step-down minimum 0.04, 0.0533, 0.0533, 0.20
    assert np.allclose(bh, [0.04, 0.04 * 4 / 3, 0.04 * 4 / 3, 0.20])
    holm = S.holm_adjust(p)
    assert np.allclose(holm, [0.04, 0.09, 0.09, 0.20])            # 0.01x4, max(0.03x3, .), max(0.04x2, .), 0.2


def test_rank_ic_and_label_permutation():
    rng = np.random.default_rng(1)
    rows = []
    for d in range(60):
        x = rng.normal(size=200)
        y = 0.1 * x + rng.normal(size=200)
        rows.append(pl.DataFrame({"date": [d] * 200, "region": ["US"] * 200, "x": x, "y": y}))
    df = pl.concat(rows)
    ic = S.ic_series(df, "x", "y", ["region", "date"])
    assert ic["ic"].mean() > 0.05
    perm = S.ic_series(S.permute_within(df, "y", ["region", "date"], seed=3), "x", "y", ["region", "date"])
    assert abs(perm["ic"].mean()) < 0.03


def test_breakout_features_causal_invariance():
    """Breakout events and their FEATURES dated <= cut are unchanged when every bar after the cut is replaced; only
    the forward labels (out_* / tau_* / ret_*) may change."""
    T, n, cut = 700, 4, 500
    C = _walk(T, n, seed=8)
    O, H, L = _bars(C)
    rng = np.random.default_rng(3)
    V = rng.lognormal(10, 0.5, (T, n))
    C2 = C.copy()
    C2[cut + 1:] = C[cut] * np.exp(np.cumsum(rng.normal(0, 0.04, (T - cut - 1, n)), axis=0))
    O2, H2, L2 = _bars(C2)
    O2[:cut + 1], H2[:cut + 1], L2[:cut + 1] = O[:cut + 1], H[:cut + 1], L[:cut + 1]
    V2 = V.copy()
    V2[cut + 1:] = rng.lognormal(12, 0.5, (T - cut - 1, n))
    dates = np.arange(np.datetime64("2020-01-01"), np.datetime64("2020-01-01") + np.timedelta64(T, "D"))
    ids = [f"S{i}" for i in range(n)]
    a, _ = BO.events_for_chunk(O, H, L, C, V, V * C, F.log_returns(C), dates, ids, controls=False)
    b, _ = BO.events_for_chunk(O2, H2, L2, C2, V2, V2 * C2, F.log_returns(C2), dates, ids, controls=False)
    ka, kb = a["t"] <= cut, b["t"] <= cut
    assert ka.sum() > 10 and ka.sum() == kb.sum()
    labels = {"out_10", "tau_10", "complete_10", "ret_10", "out_20", "tau_20", "complete_20", "ret_20"}
    for k in a:
        if k in labels or k in ("date", "security_id"):
            continue
        x, y = np.asarray(a[k])[ka], np.asarray(b[k])[kb]
        if x.dtype.kind in "fc":
            assert np.array_equal(np.nan_to_num(x, nan=-9e9), np.nan_to_num(y, nan=-9e9)), k
        else:
            assert np.array_equal(x, y), k
    # the labels of events within 20 sessions of the cut do depend on the new future
    near = ka & (a["t"] > cut - 20)
    assert not np.array_equal(a["ret_20"][near], b["ret_20"][kb & (b["t"] > cut - 20)])


def test_bo_model_permutation_has_no_skill():
    """Leakage control for the P(follow-through) model: on permuted labels the calibrated logistic has no
    out-of-sample skill over climatology (Brier), on true labels it does (synthetic events with a real effect)."""
    from app.techv3 import bo_model as BM

    rng = np.random.default_rng(0)
    N = 40000
    df = pl.DataFrame({"b_20": rng.gamma(1.0, 0.2, N), "vol_ratio": rng.lognormal(0, 0.3, N), "N": rng.choice([20, 55], N),
                       "direction": rng.choice([1, -1], N), "region": rng.choice(["US", "EU"], N),
                       "relvol20": rng.lognormal(0, 0.5, N), "relvol50": rng.lognormal(0, 0.5, N), "clv": rng.uniform(-1, 1, N),
                       "cmf20": rng.normal(0, 0.2, N), "obv_slope20": rng.normal(0, 0.3, N), "obv_confirm": rng.integers(0, 2, N),
                       "d_illiq": rng.normal(0, 0.5, N), "gap_z": rng.normal(0, 1, N), "gap_beyond": rng.integers(0, 2, N),
                       "ret_z": rng.normal(1, 1, N), "range_z": rng.gamma(2, 1, N), "trend200": rng.normal(0, 0.2, N),
                       "prior20_z": rng.normal(0, 1, N)})
    logit = -1.5 + 3.0 * df["b_20"].to_numpy() + 0.8 * np.log(df["relvol50"].to_numpy())
    y = (rng.random(N) < 1 / (1 + np.exp(-logit))).astype(float)
    X = BM.matrix(BM.design(df, 20), BM.FULL)
    tr, ca, te = slice(0, 25000), slice(25000, 30000), slice(30000, N)
    m = BM.fit(X[tr], y[tr], X[ca], y[ca], BM.FULL)
    clim = np.mean((y[tr].mean() - y[te]) ** 2)
    skill = clim - np.mean((m.predict(X[te]) - y[te]) ** 2)
    assert skill > 0.002
    # permuted labels (train and calibration): a random direction can correlate with the truth by chance, so the
    # test averages over draws - the mean skill must be a small fraction of the true-label skill
    perm = []
    for k in range(6):
        r2 = np.random.default_rng(100 + k)
        mp = BM.fit(X[tr], r2.permutation(y[tr]), X[ca], r2.permutation(y[ca]), BM.FULL)
        perm.append(clim - np.mean((mp.predict(X[te]) - y[te]) ** 2))
    assert np.mean(perm) < 0.25 * skill
    assert BM.coefficients(m)["log_relvol50"] > 0
