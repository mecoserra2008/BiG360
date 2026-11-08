"""W3.2 curve math on known synthetic curves (no network)."""

from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from app.macro.cb.curves import (
    FuncCurve,
    KnotCurve,
    bootstrap_par,
    df_from_yield,
    grid_forward_curve,
    implied_path,
    moves_priced,
    svensson_curve,
    svensson_forward,
    svensson_spot,
    tp_forward,
)


def _par_from_cont(r: float, t: float, freq: int = 2) -> float:
    """Par yield (percent) of a bond on a flat continuously-compounded curve r (decimal)."""
    h = 1 / freq
    n = int(round(t / h))
    dfs = [math.exp(-r * i * h) for i in range(1, n + 1)]
    return (1 - dfs[-1]) / (h * sum(dfs)) * 100


def test_bootstrap_flat_curve_recovers_discount_factors():
    r = 0.04
    zero = [(t, (math.exp(r * t) - 1) / t * 100, "simple") for t in (1 / 12, 0.25, 0.5)]
    par = [(t, _par_from_cont(r, t)) for t in (1.0, 2.0, 3.0, 5.0, 7.0, 10.0)]
    knots = bootstrap_par(zero, par, freq=2)
    for t, d in knots:
        if t > 0:
            assert d == pytest.approx(math.exp(-r * t), rel=2e-5), t
    c = KnotCurve(knots)
    for t1, t2 in ((0.1, 0.4), (0.5, 1.5), (2.0, 4.0), (6.0, 9.0)):
        assert c.fwd_avg(t1, t2) == pytest.approx(4.0, abs=0.01)
    assert c.zero(3.0) == pytest.approx(4.0, abs=0.01)


def test_bootstrap_upward_curve_forward_above_zero():
    zero = [(0.25, 3.0, "simple"), (0.5, 3.2, "simple")]
    par = [(2.0, 3.8), (5.0, 4.2), (10.0, 4.5)]
    c = KnotCurve(bootstrap_par(zero, par))
    assert c.fwd_avg(4.0, 5.0) > c.zero(5.0) > c.zero(0.5)
    assert all(d2 < d1 for (_, d1), (_, d2) in zip(c.knots, c.knots[1:], strict=False))


def test_df_conventions():
    assert df_from_yield(4.0, 1.0, "annual") == pytest.approx(1 / 1.04)
    assert df_from_yield(4.0, 0.5, "simple") == pytest.approx(1 / 1.02)
    assert df_from_yield(4.0, 1.0, "semi") == pytest.approx(1 / 1.02 ** 2)
    assert df_from_yield(4.0, 2.0, "cont") == pytest.approx(math.exp(-0.08))


def test_svensson_forward_and_spot_consistency():
    p = (2.0, 1.0, -0.5, 0.8, 1.2, 9.0)
    assert svensson_forward(0.0, *p) == pytest.approx(3.0, abs=1e-6)  # f(0) = b0 + b1
    assert svensson_forward(200.0, *p) == pytest.approx(2.0, abs=1e-3)  # long end -> b0
    c = svensson_curve(dict(zip(("BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2"), p, strict=True)))
    # spot rate = average forward from 0 to t
    for t in (0.5, 2.0, 7.0):
        assert c.fwd_avg(0.0, t) == pytest.approx(svensson_spot(t, *p), abs=2e-3)


def test_grid_forward_curve_interpolates_and_extrapolates_flat():
    c = grid_forward_curve([(1 / 12, 4.0), (1.0, 5.0)])
    assert c.inst_fwd(0.0) == pytest.approx(4.0)
    assert c.inst_fwd(3.0) == pytest.approx(5.0)
    mid = (1 / 12 + 1.0) / 2
    assert c.inst_fwd(mid) == pytest.approx(4.5, abs=1e-9)


def test_tp_forward_linear_term_structure():
    pts = [(1.0, 0.2), (2.0, 0.4)]
    # TP(t) t: 0.2 at 1y, 0.8 at 2y -> forward TP over [1, 2] = 0.6
    assert tp_forward(pts, 1.0, 2.0) == pytest.approx(0.6)
    assert tp_forward(pts, 0.0, 0.5) == pytest.approx(0.1)


def test_implied_path_flat_curve_prices_nothing():
    as_of = date(2026, 10, 9)
    c = FuncCurve(lambda t: 4.10, max_t=5.0)  # flat forward; basis 10bp vs 4.00 policy
    meets = [as_of + timedelta(days=d) for d in (19, 61, 110, 160)]
    pts, meta = implied_path(c, as_of, 4.0, meets)
    assert meta["basis_bp"] == pytest.approx(10.0, abs=1e-6) and meta["anchor"] == "stub_to_next_meeting"
    assert all(abs(p.change_bp) < 1e-6 for p in pts)
    assert {p.label for p in pts if p.point_type == "horizon"} == {"3m", "6m", "12m", "24m"}


def test_implied_path_detects_a_hike_at_the_second_meeting():
    as_of = date(2026, 10, 9)
    m1, m2, m3 = (as_of + timedelta(days=d) for d in (20, 62, 111))
    t2 = (m2 - as_of).days / 365.25 + 1 / 365.25
    c = FuncCurve(lambda t: 4.0 if t < t2 else 4.25, max_t=5.0, steps_per_year=3650)
    pts, _ = implied_path(c, as_of, 4.0, [m1, m2, m3])
    by = {p.label: p for p in pts if p.point_type == "meeting"}
    assert by[m1.isoformat()].change_bp == pytest.approx(0.0, abs=0.5)
    assert by[m2.isoformat()].change_bp == pytest.approx(25.0, abs=0.5)
    assert by[m2.isoformat()].incremental_bp == pytest.approx(25.0, abs=0.5)
    assert moves_priced(by[m3.isoformat()].change_bp) == pytest.approx(1.0, abs=0.02)


def test_implied_path_tp_adjustment():
    as_of = date(2026, 10, 9)
    c = FuncCurve(lambda t: 4.0 + 0.5 * t, max_t=5.0)
    tp = [(float(n), 0.25 * n) for n in range(1, 6)]  # TP rising with maturity
    pts, _ = implied_path(c, as_of, 4.0, [], tp_pts=tp)
    h24 = next(p for p in pts if p.label == "24m")
    assert h24.implied_tp is not None and h24.implied_tp < h24.implied


def test_first_month_anchor_when_meeting_is_imminent():
    as_of = date(2026, 10, 9)
    c = FuncCurve(lambda t: 4.0, max_t=5.0)
    _, meta = implied_path(c, as_of, 4.0, [as_of + timedelta(days=3), as_of + timedelta(days=50)])
    assert meta["anchor"] == "first_month" and meta["stub_includes_meeting"]
