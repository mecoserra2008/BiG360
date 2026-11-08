"""Technicals v2 T2-T4 estimators and the T6 leakage tests (docs/TECHNICALS_V2_PLAN.md)."""

from __future__ import annotations

import math
from datetime import date

import numpy as np
import polars as pl
import pytest
from scipy.stats import norm

from app.techprob import barriers as B
from app.techprob import calib as K
from app.techprob import history as Hh
from app.techprob import score_cal as SC
from app.techprob import zone_model as M

BG = 0.5826                      # Broadie-Glasserman continuity correction (discrete monitoring of a continuous BM)


# ------------------------------------------------------------------------------------------------ T2: analytic formula


def test_brownian_two_barrier_limits():
    # far lower barrier -> one-sided reflection principle P(max_{t<=1} W >= a) = 2 (1 - Phi(a))
    pu, _ = B.bm_two_barrier(np.array([0.5, 1.0, 2.0]), np.array([60.0, 60.0, 60.0]))
    assert np.allclose(pu, 2 * (1 - norm.cdf([0.5, 1.0, 2.0])), atol=1e-6)
    # symmetry and the long-run gambler's-ruin limit b / (a + b)
    pu, pd = B.bm_two_barrier(np.array([0.7]), np.array([1.3]))
    qu, qd = B.bm_two_barrier(np.array([1.3]), np.array([0.7]))
    assert pu[0] == pytest.approx(qd[0], abs=1e-9) and pd[0] == pytest.approx(qu[0], abs=1e-9)
    lu, ld = B.bm_two_barrier(np.array([0.2]), np.array([0.3]), t=50.0)
    assert lu[0] == pytest.approx(0.6, abs=1e-6) and ld[0] == pytest.approx(0.4, abs=1e-6)
    # monotone: a farther upper barrier is reached less often, a farther lower barrier makes "up first" more likely
    a = np.array([0.3, 0.6, 1.0, 2.0])
    pu, _ = B.bm_two_barrier(a, np.full(4, 1.0))
    assert np.all(np.diff(pu) < 0)
    pu2, _ = B.bm_two_barrier(np.full(4, 1.0), a)
    assert np.all(np.diff(pu2) > 0)


def _brownian_bars(n_bars: int, sub: int, sd: float, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    x = np.concatenate([[0.0], np.cumsum(rng.normal(0, sd / math.sqrt(sub), n_bars * sub))])
    c = x[::sub]
    starts = np.arange(0, n_bars * sub, sub)
    hi = np.r_[c[0], np.maximum.reduceat(x[1:], starts)]
    lo = np.r_[c[0], np.minimum.reduceat(x[1:], starts)]
    prev = np.r_[c[0], c[:-1]]
    return c, np.maximum(hi, prev), np.minimum(lo, prev)


def test_grid_estimator_matches_brownian_two_barrier():
    """Synthetic Brownian case: the empirical (calibrated) grid estimator recovers the analytic two-barrier
    probability (discrete-monitoring corrected) at every grid cell checked. Independent paths: one origin every
    HMAX + 1 bars, 24k paths."""
    sd, sub, step = 0.01, 32, B.HMAX + 1
    gc = B.GridCounts.empty("US", nq=2)
    for k in range(4):
        lc, lh, ll = _brownian_bars(6000 * step, sub, sd, seed=3 + k)
        U, D, Z = B.path_extremes(lc, lh, ll)
        n = len(lc)
        V = np.stack([np.full(n, H * sd * sd) for H in B.HORIZONS], 1)
        use = (np.arange(n) % step) == 0
        B.accumulate(gc, U, D, Z, V, np.ones(n, np.int8), np.zeros((n, len(B.HORIZONS)), bool), np.zeros(n, np.int64), use)
    surf = B.fit_surface(gc, 0, 1, m=0.0, smooth=False)
    worst = 0.0
    for H in (20, 60):
        shift = BG * (sd / math.sqrt(sub)) / math.sqrt(H * sd * sd)       # barrier shift in sigma_H units
        for a, b in ((0.5, 0.5), (1.0, 0.5), (0.5, 1.5), (1.5, 1.0), (2.0, 2.0), (0.3, 3.0)):
            e = surf.eval("US", 1, False, H, a, b)
            pu, pd = B.bm_two_barrier(np.array([a + shift]), np.array([b + shift]))
            worst = max(worst, abs(e["p_up"] - pu[0]), abs(e["p_down"] + e["p_tie"] - pd[0]))
            assert e["p_up"] + e["p_down"] + e["p_tie"] + e["p_neither"] == pytest.approx(1.0, abs=1e-9)
    assert worst < 0.015, worst
    # the shrunk + smoothed fit stays monotone
    sm = B.fit_surface(gc, 0, 1)
    P = sm.P[0, 1, 0, B.HORIZONS.index(20)]
    assert np.all(np.diff(P[..., 0], axis=0) <= 1e-12) and np.all(np.diff(P[..., 0], axis=1) >= -1e-12)


def test_outcomes_explicit_barriers_and_ties():
    lc = np.log(np.array([100.0, 101, 103, 99, 96, 98, 100]))
    lh = np.log(np.array([100.0, 102, 104, 101, 98, 99, 101]))
    ll = np.log(np.array([100.0, 100, 102, 97, 95, 97, 99]))
    U, D, _ = B.path_extremes(lc, lh, ll, 5)
    o = B.outcomes(U[:1], D[:1], np.array([math.log(1.035)]), np.array([math.log(100 / 96)]), 5)
    assert o[0] == 1                                    # high 104 on bar 2 before the low 95 on bar 4
    o = B.outcomes(U[:1], D[:1], np.array([math.log(1.05)]), np.array([math.log(100 / 96)]), 5)
    assert o[0] == 2
    o = B.outcomes(U[:1], D[:1], np.array([math.log(1.10)]), np.array([math.log(100 / 80)]), 5)
    assert o[0] == 0
    # both barriers inside the same bar -> tie (counted as a loss for a long)
    o = B.outcomes(U[3:4], D[3:4], np.array([math.log(98 / 99)]) * -1, np.array([math.log(99 / 95.5)]), 1)
    assert o[0] in (2, 3)


def test_pav_and_expected_r():
    y = np.array([0.1, 0.3, 0.2, 0.5, 0.4])
    iso = B._pav(y, np.ones(5), increasing=True)  # noqa: SLF001
    assert np.all(np.diff(iso) >= 0) and iso.sum() == pytest.approx(y.sum())
    e = B.expected_r(0.45, 0.40, 1.5, 0.02, 0.15, 0.2)
    assert e["expected_r"] == pytest.approx(0.45 * 1.5 - 0.40 - 0.02)
    assert e["break_even_p"] == pytest.approx(1 / 2.5) and e["expected_r_mtm"] == pytest.approx(e["expected_r"] + 0.03)
    c = B.round_trip_cost(5e8, 0.015, False, 10, {})
    cv = B.round_trip_cost_vec(np.array([5e8]), np.array([0.015]), np.array([False]), np.array([10.0]), {})
    assert c["round_trip"] == pytest.approx(float(cv[0])) and 0 < c["round_trip"] < 0.01
    assert B.round_trip_cost(5e8, 0.015, True, 10, {})["round_trip"] > c["round_trip"]       # borrow
    assert B.setup_horizon(0.05, lambda H: H * 0.02 ** 2) == 10       # 0.05 <= sqrt(10) x 2% = 6.3%


def test_surface_roundtrip_rows():
    gc = B.GridCounts.empty("EU", nq=2)
    rng = np.random.default_rng(0)
    gc.C[...] = rng.integers(0, 50, gc.C.shape)
    gc.N[...] = 200
    s = B.fit_surface(gc, 0, 2)
    s2 = B.surface_from_rows(B.surface_rows(s, date(2026, 10, 22)))
    assert np.allclose(s.P, s2.P) and s2.slot("EU") == (1, False) and s2.slot("KR")[1]


# ------------------------------------------------------------------------------------------------ T3 labels


def test_zone_outcome_kernel():
    # support zone 98-100, close 103; touch on bar 2 (low 99.5), hold: high >= 100 e^{1.5 sigma} later
    c = np.array([103.0, 102, 100.5, 101, 103.5, 104])
    h = c + 0.5
    l_ = c - 1.0
    sd = 0.01
    kh, mb = math.exp(1.5 * sd), math.exp(0.5 * sd)
    t, tau, oc, comp = Hh.zone_outcome(h, l_, c, 98.0, 100.0, 1, False, 0, 20, 10, kh, mb)
    assert (t, tau, oc) == (1, 2, 1)
    # break: a close below 98 e^{-0.5 sigma}
    c2 = np.array([103.0, 101, 99.5, 97.0, 96, 95])
    t, tau, oc, comp = Hh.zone_outcome(c2 + 0.3, c2 - 0.6, c2, 98.0, 100.0, 1, False, 0, 20, 10, kh, mb)
    assert (t, oc) == (1, 2)
    # never reaches the zone within the horizon (path long enough) -> untouched, complete
    c3 = np.full(30, 110.0)
    t, tau, oc, comp = Hh.zone_outcome(c3 + 1, c3 - 1, c3, 98.0, 100.0, 1, False, 0, 20, 10, kh, mb)
    assert (t, comp) == (0, 1)
    tch, res, hol = Hh.prior_band_stats(h, l_, c, 98.0, 100.0, 5, 252, 10, kh, mb)
    assert tch == 1 and res == 1 and hol == 1


def test_edge_buckets_and_flag():
    rng = np.random.default_rng(1)
    pred = rng.normal(0, 0.02, 20000)
    edges = M.bucket_edges(pred)
    y = (rng.random(20000) < 0.5 + pred).astype(float)
    pc = np.full(20000, 0.5)
    dates = np.datetime64("2015-01-01") + rng.integers(0, 3000, 20000).astype("timedelta64[D]")
    tab = M.bucket_stats(pred, y, pc, pc + pred, dates, edges, B_=100)
    assert tab[-1]["significant"] and tab[-1]["realised_edge"] > 0 and tab[0]["realised_edge"] < 0
    fl = M.edge_flag(0.05, tab, gate_pass=True)
    assert fl["edge_significant"] and fl["edge_direction"] == "better_than_random"
    assert not M.edge_flag(0.05, tab, gate_pass=False)["edge_significant"]
    assert not M.edge_flag(None, tab, gate_pass=True)["edge_significant"]


# ------------------------------------------------------------------------------------------------ calibration + T4


def test_calibration_metrics():
    rng = np.random.default_rng(2)
    p = rng.uniform(0.05, 0.95, 50000)
    y = (rng.random(50000) < p).astype(float)
    assert K.ece(p, y) < 0.01 and abs(K.gap(p, y)) < 0.01
    assert K.ece(np.clip(p + 0.1, 0, 1), y) > 0.08
    assert K.brier(p, y) < K.brier(np.full_like(p, 0.5), y)
    bs = K.block_bootstrap(lambda ix: float(y[ix].mean()), np.arange(50000) // 500, B=100)
    assert bs["lo"] < 0.5 < bs["hi"]


def test_score_percentiles_and_buckets():
    df = pl.DataFrame({"date": [date(2020, 1, 3)] * 6, "region": ["US"] * 6, "sector_id": ["A", "A", "A", "B", "B", "B"],
                       "technical_score": [10.0, 50, 90, 20, 20, 80], **{c: [100.0, 100, 0, 50, 25, 75] for c in SC.SUBS}})
    out = SC.add_percentiles(df)
    assert out.filter(pl.col("sector_id") == "A")["pct_trend"].to_list() == pytest.approx([100 * 2.0 / 3, 100 * 2.0 / 3, 100 * 0.5 / 3])
    assert out["technical_score_pct"].is_not_null().all() and "pct_technical" in out.columns
    x = np.linspace(0, 100, 1000)
    edges = SC.decile_edges(x)
    assert SC.decile_of(np.array([0.0, 55.0, 100.0]), edges).tolist() == [0, 5, 9]
    rows = SC.bucket_table(x, x / 1000 - 0.05, np.datetime64("2015-01-01") + np.arange(1000).astype("timedelta64[D]"), edges, B=50)
    assert rows[0]["median"] < rows[-1]["median"] and SC.lookup(55.0, rows)["decile"] == 6


# ------------------------------------------------------------------------------------------------ T6 leakage


def _synthetic_input(n: int = 1400, seed: int = 5) -> Hh.SecInput:
    from app.technicals.data import Bars

    rng = np.random.default_rng(seed)
    dates = np.busday_offset(np.datetime64("2015-01-02"), np.arange(n), roll="forward").astype("datetime64[D]")
    lc = np.log(50.0) + np.cumsum(rng.normal(0.0003, 0.015, n))
    c = np.exp(lc)
    o = np.r_[c[0], c[:-1]] * np.exp(rng.normal(0, 0.003, n))
    h = np.maximum(o, c) * np.exp(np.abs(rng.normal(0, 0.008, n)))
    l_ = np.minimum(o, c) / np.exp(np.abs(rng.normal(0, 0.008, n)))
    v = rng.uniform(1e6, 3e6, n)
    b = Bars("STEST", dates, o, h, l_, c, v, region="US", sector_id="TECH")
    ev = dates[::63][1:]
    return Hh.SecInput(b, c * v, ev)


@pytest.fixture(scope="module")
def syn():
    return _synthetic_input()


def test_causal_history_passes_and_peeking_feature_is_caught(syn):
    from app.techprob import leakage as L

    honest = L.causal_history(syn, "US", cuts=(0.75,))
    assert honest["passed"], honest
    leaky = L.causal_history(syn, "US", cuts=(0.75,), peek=True)
    assert not leaky["passed"]
    assert any(v["feature"] == "approach5" for c in leaky["cuts"] for v in c["violations"])


def test_label_permutation_gives_no_skill():
    from app.techprob import leakage as L

    rng = np.random.default_rng(4)
    n = 30000
    X = rng.normal(size=(n, 5)).astype(np.float32)
    y = (rng.random(n) < 1 / (1 + np.exp(-(1.5 * X[:, 0] - X[:, 1])))).astype(float)
    dates = np.datetime64("2012-01-01") + np.sort(rng.integers(0, 4000, n)).astype("timedelta64[D]")
    perm = L.permutation_skill(X, y, dates, kind="logit")
    # Brier skill ~0 (AUC is not informative here: tiny noise coefficients still rank by the true signal)
    assert abs(perm["brier_skill"]) < 0.01
    # sanity: the same pipeline on the true labels has skill
    from app.techprob import zone_model as ZM

    tr = dates < dates[int(n * 0.7)] - np.timedelta64(60, "D")
    te = dates >= dates[int(n * 0.7)]
    m = ZM.fit("logit", X[tr], y[tr], None, None, [f"x{i}" for i in range(5)])
    p = m.predict(X[te])
    assert 1 - np.mean((p - y[te]) ** 2) / np.mean((y[tr].mean() - y[te]) ** 2) > 0.1
