"""Svensson (1994) / Nelson-Siegel curve reconstruction (pure functions, numpy).

Conventions (verified against the published values, tests/rates/test_svensson.py):
  * ECB euro area yield curves and the Bundesbank term structure publish Svensson parameters (beta0..beta3 in
    percent, tau1 / tau2 in years) of the CONTINUOUSLY compounded zero-coupon (spot) curve:
        s(t) = b0 + b1 g1 + b2 (g1 - e1) + b3 (g2 - e2),  g_i = (1 - e_i) / x_i, e_i = exp(-x_i), x_i = t / tau_i
    instantaneous forward  f(t) = b0 + b1 e1 + b2 x1 e1 + b3 x2 e2.
  * ECB par yields (PY_*) are continuous-coupon par yields: c(T) = (1 - D(T)) / integral_0^T D(t) dt with
    D(t) = exp(-s(t) t); reproduced to < 0.01 bp (the annual-coupon par yield differs by ~5 bp at 10y).
  * A Nelson-Siegel parameter set is a Svensson set with beta3 = 0 (tau2 then irrelevant).
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

PARAMS = ("BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2")


def _x(t: np.ndarray, tau: float) -> np.ndarray:
    return np.maximum(t, 1e-9) / max(float(tau), 1e-9)


def spot(t: Sequence[float] | np.ndarray, b0: float, b1: float, b2: float, b3: float, tau1: float,
         tau2: float) -> np.ndarray:
    """Continuously compounded zero rates (percent) at maturities t (years)."""
    t = np.asarray(t, dtype=float)
    x1, x2 = _x(t, tau1), _x(t, tau2)
    e1, e2 = np.exp(-x1), np.exp(-x2)
    g1, g2 = (1 - e1) / x1, (1 - e2) / x2
    return b0 + b1 * g1 + b2 * (g1 - e1) + (b3 * (g2 - e2) if b3 else 0.0)


def forward(t: Sequence[float] | np.ndarray, b0: float, b1: float, b2: float, b3: float, tau1: float,
            tau2: float) -> np.ndarray:
    """Instantaneous forward rates (percent)."""
    t = np.asarray(t, dtype=float)
    x1, x2 = _x(t, tau1), _x(t, tau2)
    e1, e2 = np.exp(-x1), np.exp(-x2)
    return b0 + b1 * e1 + b2 * x1 * e1 + (b3 * x2 * e2 if b3 else 0.0)


def discount(t: Sequence[float] | np.ndarray, *p: float) -> np.ndarray:
    t = np.asarray(t, dtype=float)
    return np.exp(-spot(t, *p) / 100.0 * t)


def par_continuous(T: Sequence[float] | np.ndarray, *p: float, steps_per_year: int = 365) -> np.ndarray:
    """Continuous-coupon par yields (percent): (1 - D(T)) / int_0^T D(t) dt (trapezoid on a daily grid)."""
    T = np.atleast_1d(np.asarray(T, dtype=float))
    out = np.empty_like(T)
    tmax = float(T.max()) if T.size else 0.0
    n = max(8, int(np.ceil(tmax * steps_per_year)))
    grid = np.linspace(0.0, tmax, n + 1)
    d = discount(grid, *p)
    d[0] = 1.0
    cum = np.concatenate([[0.0], np.cumsum((d[1:] + d[:-1]) * 0.5 * np.diff(grid))])
    for i, t in enumerate(T):
        if t <= 0:
            out[i] = np.nan
            continue
        area = float(np.interp(t, grid, cum))
        out[i] = (1.0 - float(discount([t], *p)[0])) / area * 100.0 if area > 0 else np.nan
    return out


def par_annual(T: Sequence[float] | np.ndarray, *p: float) -> np.ndarray:
    """Annual-coupon par yields (percent) for integer-year maturities (fractional maturities: coupon dates counted
    back from T); for T < 1 the annually compounded zero rate."""
    T = np.atleast_1d(np.asarray(T, dtype=float))
    out = np.empty_like(T)
    for i, t in enumerate(T):
        if t < 1:
            out[i] = (np.exp(spot([t], *p)[0] / 100.0) - 1) * 100.0
            continue
        cds = t - np.arange(int(np.floor(t + 1e-9)))[::-1]
        cds = cds[cds > 1e-9]
        d = discount(cds, *p)
        out[i] = (1 - d[-1]) / d.sum() * 100.0
    return out


def curve_points(params: dict[str, float], tenors_months: Sequence[int],
                 measures: Sequence[str] = ("spot", "par", "forward")) -> dict[str, dict[int, float]]:
    """{measure: {tenor_months: percent}} from one parameter set (keys BETA0..TAU2; missing BETA3/TAU2 = NS)."""
    p = (float(params["BETA0"]), float(params["BETA1"]), float(params["BETA2"]), float(params.get("BETA3") or 0.0),
         float(params["TAU1"]), float(params.get("TAU2") or params["TAU1"]))
    t = np.asarray(tenors_months, dtype=float) / 12.0
    out: dict[str, dict[int, float]] = {}
    if "spot" in measures:
        out["spot"] = dict(zip(tenors_months, spot(t, *p).tolist(), strict=True))
    if "forward" in measures:
        out["forward"] = dict(zip(tenors_months, forward(t, *p).tolist(), strict=True))
    if "par" in measures:
        out["par"] = dict(zip(tenors_months, par_continuous(t, *p).tolist(), strict=True))
    return out


def valid(params: dict[str, float]) -> bool:
    try:
        vals = [float(params[k]) for k in ("BETA0", "BETA1", "BETA2", "TAU1")]
    except (KeyError, TypeError, ValueError):
        return False
    return all(np.isfinite(vals)) and float(params["TAU1"]) > 0
