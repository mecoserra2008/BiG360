"""W3.2 curve math (pure functions, no I/O): par bootstrap, Svensson curves, forward averages, implied policy path.

Conventions: times in years (ACT/365.25 from the curve date); rates in percent; discount factors continuous.

Curve representations (all expose ``fwd_avg(t1, t2)`` = average continuously-compounded forward rate over [t1, t2]
in percent, and ``zero(t)``):
  KnotCurve   discount-factor knots, log-linear interpolation (= piecewise-flat forwards between knots). Built from
              zero-coupon yields (bills, OIS, spot curves) and/or bootstrapped par yields (coupon bonds).
  FuncCurve   closed-form instantaneous forward f(t) (ECB Svensson parameters) or a forward grid (BoE OIS
              instantaneous forwards at monthly maturities, linear between grid points, flat beyond the ends);
              averages by numerical integration.

Implied policy path (method ``implied_v1``): for meeting k with effective time e_k, the market-implied rate over the
inter-meeting period [e_k, e_{k+1}) is the average forward over that window minus a basis. The basis anchors the
curve to today's policy rate: basis = average forward over the stub [0, e_1) (the period before the next decision,
in which the policy rate is known) - current policy rate. It absorbs the instrument basis (bills/govvies vs overnight
rate, overnight rate vs policy rate) and is assumed constant along the path - an approximation.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

YEAR = 365.25
MIN_STUB_YEARS = 10 / YEAR
STEP_BP = 25.0


def yf(d0: date, d1: date) -> float:
    return (d1 - d0).days / YEAR


# ------------------------------------------------------------------------------------------------ conversions

def df_from_yield(y_pct: float, t: float, comp: str = "semi") -> float:
    """Discount factor from a zero-coupon yield. comp: 'semi' (bond-equivalent), 'annual', 'simple', 'cont'."""
    y = y_pct / 100.0
    if t <= 0:
        return 1.0
    if comp == "semi":
        return (1 + y / 2) ** (-2 * t)
    if comp == "annual":
        return (1 + y) ** (-t)
    if comp == "simple":
        return 1.0 / (1 + y * t)
    return math.exp(-y * t)


def cont_rate(df: float, t: float) -> float:
    return -math.log(df) / t * 100.0 if t > 0 and df > 0 else float("nan")


def _interp(xs: Sequence[float], ys: Sequence[float], x: float) -> float:
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    i = bisect_right(xs, x) - 1
    w = (x - xs[i]) / (xs[i + 1] - xs[i])
    return ys[i] * (1 - w) + ys[i + 1] * w


def bootstrap_par(zero_pts: Sequence[tuple], par_pts: Sequence[tuple[float, float]],
                  zero_comp: str = "semi", freq: int = 2) -> list[tuple[float, float]]:
    """Discount-factor knots from zero-coupon points (t <= ~1y; bills/OIS/spot) and par yields of coupon bonds
    (t > 1y, ``freq`` coupons per year). Par yields are linearly interpolated on the coupon grid; each coupon-date
    DF is solved from price = 1 with earlier DFs from the curve built so far (log-linear between knots).
    Returns sorted [(t, DF)] including (0, 1)."""
    knots: dict[float, float] = {0.0: 1.0}
    for zp in sorted(zero_pts):
        t, y = zp[0], zp[1]
        comp = zp[2] if len(zp) > 2 else zero_comp
        if t > 0 and y is not None and not math.isnan(y):
            knots[round(t, 6)] = df_from_yield(y, t, comp)
    par = sorted((t, y) for t, y in par_pts if y is not None and not math.isnan(y))
    if par:
        # par interpolation grid includes the longest zero point so short coupons are consistent
        zl = max((t for t in knots if t > 0), default=0.0)
        pxs = ([zl] if zl > 0 else []) + [t for t, _ in par if t > zl + 1e-9]
        pys = ([_par_from_knots(knots, zl, freq)] if zl > 0 else []) + [y for t, y in par if t > zl + 1e-9]
        if not pxs:
            return sorted(knots.items())
        h = 1.0 / freq
        n_max = int(round(pxs[-1] / h))
        for n in range(1, n_max + 1):
            tn = round(n * h, 6)
            if tn <= zl + 1e-9:
                continue
            c = _interp(pxs, pys, tn) / 100.0
            s = sum(_df_at(knots, round(i * h, 6)) for i in range(1, n))
            knots[tn] = (1 - c / freq * s) / (1 + c / freq)
    return sorted(knots.items())


def _par_from_knots(knots: dict[float, float], t: float, freq: int) -> float:
    """Par yield implied by the knots at maturity t (used to splice zero and par segments)."""
    h = 1.0 / freq
    n = max(1, int(round(t / h)))
    dfs = [_df_at(knots, round(i * h, 6)) for i in range(1, n + 1)]
    return (1 - dfs[-1]) / (h * sum(dfs)) * 100.0


def _df_at(knots: dict[float, float] | Sequence[tuple[float, float]], t: float) -> float:
    items = sorted(knots.items()) if isinstance(knots, dict) else list(knots)
    ts = [k for k, _ in items]
    if t <= 0:
        return 1.0
    if t in ts:
        return items[ts.index(t)][1]
    i = bisect_right(ts, t) - 1
    if i >= len(ts) - 1:  # flat forward extrapolation from the last segment
        t0, d0 = items[-2] if len(items) > 1 else (0.0, 1.0)
        t1, d1 = items[-1]
        f = -math.log(d1 / d0) / (t1 - t0) if t1 > t0 else 0.0
        return d1 * math.exp(-f * (t - t1))
    t0, d0 = items[i]
    t1, d1 = items[i + 1]
    w = (t - t0) / (t1 - t0)
    return math.exp(math.log(d0) * (1 - w) + math.log(d1) * w)


# ------------------------------------------------------------------------------------------------ curve objects

@dataclass
class KnotCurve:
    """DF knots; y(t) = -ln DF(t) interpolated with a monotone cubic (PCHIP) so forwards f = y' are continuous
    (no saw-tooth from piecewise-flat forwards between bill knots). Beyond the last knot: flat forward."""
    knots: list[tuple[float, float]]           # [(t, DF)] incl. (0, 1)
    smooth: bool = True
    _p: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        ks = sorted({round(t, 8): d for t, d in self.knots}.items())
        if not ks or ks[0][0] > 0:
            ks = [(0.0, 1.0)] + ks
        self.knots = ks
        if self.smooth and len(ks) >= 3:
            try:
                from scipy.interpolate import PchipInterpolator  # noqa: PLC0415

                self._p = PchipInterpolator([t for t, _ in ks], [-math.log(d) for _, d in ks], extrapolate=False)
            except Exception:  # noqa: BLE001
                self._p = None

    def _y(self, t: float) -> float:
        if t <= 0:
            return 0.0
        tl = self.knots[-1][0]
        if self._p is not None and t <= tl:
            return float(self._p(t))
        if t > tl and self._p is not None:
            f_end = float(self._p.derivative()(tl))
            return float(self._p(tl)) + f_end * (t - tl)
        return -math.log(_df_at(self.knots, t))

    def df(self, t: float) -> float:
        return math.exp(-self._y(t))

    def zero(self, t: float) -> float:
        return self._y(t) / t * 100.0 if t > 0 else float("nan")

    def fwd_avg(self, t1: float, t2: float) -> float:
        t1 = max(t1, 0.0)
        if t2 <= t1:
            t2 = t1 + 1 / 365
        return (self._y(t2) - self._y(t1)) / (t2 - t1) * 100.0

    def inst_fwd(self, t: float) -> float:
        return self.fwd_avg(t, t + 1 / 365)

    @property
    def max_t(self) -> float:
        return self.knots[-1][0]


@dataclass
class FuncCurve:
    f: Callable[[float], float]                 # instantaneous forward (percent) at t
    max_t: float = 30.0
    steps_per_year: int = 365
    _cache: dict = field(default_factory=dict, repr=False)

    def fwd_avg(self, t1: float, t2: float) -> float:
        t1 = max(t1, 0.0)
        if t2 <= t1:
            return self.f(t1)
        n = max(4, int((t2 - t1) * self.steps_per_year))
        h = (t2 - t1) / n
        s = 0.5 * (self.f(t1) + self.f(t2)) + sum(self.f(t1 + i * h) for i in range(1, n))
        return s * h / (t2 - t1)

    def zero(self, t: float) -> float:
        return self.fwd_avg(0.0, t)

    def inst_fwd(self, t: float) -> float:
        return self.f(t)


def svensson_forward(t: float, b0: float, b1: float, b2: float, b3: float, tau1: float, tau2: float) -> float:
    """Instantaneous forward of the Svensson (1994) model as used by the ECB yield curves (percent)."""
    t = max(t, 1e-9)
    x1, x2 = t / tau1, t / tau2
    return b0 + b1 * math.exp(-x1) + b2 * x1 * math.exp(-x1) + b3 * x2 * math.exp(-x2)


def svensson_spot(t: float, b0: float, b1: float, b2: float, b3: float, tau1: float, tau2: float) -> float:
    t = max(t, 1e-9)
    x1, x2 = t / tau1, t / tau2
    g1 = (1 - math.exp(-x1)) / x1
    g2 = (1 - math.exp(-x2)) / x2
    return b0 + b1 * g1 + b2 * (g1 - math.exp(-x1)) + b3 * (g2 - math.exp(-x2))


def svensson_curve(params: dict[str, float]) -> FuncCurve:
    p = (params["BETA0"], params["BETA1"], params["BETA2"], params["BETA3"], params["TAU1"], params["TAU2"])
    return FuncCurve(lambda t: svensson_forward(t, *p), max_t=30.0, steps_per_year=120)


def grid_forward_curve(points: Sequence[tuple[float, float]]) -> FuncCurve:
    """Forward curve from (t, inst forward) grid points: linear between points, flat beyond the ends."""
    pts = sorted((t, f) for t, f in points if f is not None and not math.isnan(f))
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return FuncCurve(lambda t: _interp(xs, ys, t), max_t=xs[-1] if xs else 0.0, steps_per_year=120)


# ------------------------------------------------------------------------------------------------ term premium

def tp_forward(tp_pts: Sequence[tuple[float, float]], t1: float, t2: float) -> float:
    """Forward term premium over [t1, t2] from yield term premia TP(n) (percent, n in years; ACM TP01..TP10).
    TP(t) is linear between knots with TP(0) = 0; forward TP = (TP(t2) t2 - TP(t1) t1) / (t2 - t1)."""
    pts = [(0.0, 0.0)] + sorted(tp_pts)
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]

    def tp(t: float) -> float:
        return _interp(xs, ys, t) if t <= xs[-1] else ys[-1]
    if t2 <= t1:
        t2 = t1 + 1 / 365
    return (tp(t2) * t2 - tp(t1) * t1) / (t2 - t1)


# ------------------------------------------------------------------------------------------------ implied path

@dataclass
class PathPoint:
    point_type: str          # 'meeting' | 'horizon'
    label: str
    target_date: date
    t_start: float
    t_end: float
    implied: float           # implied policy rate (raw forwards, basis-adjusted)
    implied_tp: float | None = None   # term-premium-adjusted (US only)
    change_bp: float = 0.0   # vs current policy rate
    incremental_bp: float | None = None  # vs previous meeting


def implied_path(curve, as_of: date, policy_rate: float, meetings: Sequence[date],  # noqa: ANN001
                 horizons: Sequence[tuple[str, int]] = (("3m", 3), ("6m", 6), ("12m", 12), ("24m", 24)),
                 effective_lag_days: int = 1, tp_pts: Sequence[tuple[float, float]] | None = None,
                 max_meetings: int = 12) -> tuple[list[PathPoint], dict[str, float]]:
    """Implied policy rates at upcoming meetings and fixed horizons (see module docstring).
    Meetings beyond the curve's reliable range (``curve.max_t``) are skipped."""
    from datetime import timedelta  # noqa: PLC0415

    ms = sorted(m for m in meetings if m > as_of)
    eff = [yf(as_of, m + timedelta(days=effective_lag_days)) for m in ms]
    eff = [e for e in eff if e <= getattr(curve, "max_t", 30.0)][:max_meetings]
    # stub = [0, next decision); when the next decision is < 10 days away (or no meeting calendar) the anchor window is
    # [0, 1m], which may contain a priced move -> flagged in meta (the basis then absorbs part of that move)
    stub_start = 0.0
    stub_end = eff[0] if eff and eff[0] >= MIN_STUB_YEARS else 1 / 12
    basis = curve.fwd_avg(stub_start, stub_end) - policy_rate
    meta = {"basis_bp": round(basis * 100, 2), "stub_years": round(stub_end - stub_start, 4),
            "anchor": "stub_to_next_meeting" if eff and eff[0] >= MIN_STUB_YEARS else "first_month",
            "stub_includes_meeting": bool(eff and eff[0] < MIN_STUB_YEARS)}

    def adj(t1: float, t2: float) -> float | None:
        if tp_pts is None:
            return None
        return tp_forward(tp_pts, t1, t2)
    tp_basis = adj(stub_start, stub_end) if tp_pts else None
    pts: list[PathPoint] = []
    prev = policy_rate
    for k, e in enumerate(eff):
        nxt = eff[k + 1] if k + 1 < len(eff) else e + 6 / 52
        raw = curve.fwd_avg(e, nxt) - basis
        tpa = None
        if tp_pts:
            tpa = raw - (adj(e, nxt) - (tp_basis or 0.0))
        pts.append(PathPoint("meeting", ms[k].isoformat(), ms[k], e, nxt, raw, tpa,
                             (raw - policy_rate) * 100, (raw - prev) * 100))
        prev = raw
    for lab, months in horizons:
        t2 = months / 12.0
        if t2 > getattr(curve, "max_t", 30.0) + 1e-9:
            continue
        t1 = t2 - 1 / 12
        raw = curve.fwd_avg(t1, t2) - basis
        tpa = raw - (adj(t1, t2) - (tp_basis or 0.0)) if tp_pts else None
        td = as_of + timedelta(days=int(round(t2 * YEAR)))
        pts.append(PathPoint("horizon", lab, td, t1, t2, raw, tpa, (raw - policy_rate) * 100, None))
    return pts, meta


def moves_priced(change_bp: float, step_bp: float = STEP_BP) -> float:
    """Cumulative moves priced in units of ``step_bp`` (negative = cuts)."""
    return change_bp / step_bp
