"""Technicals v2 T5 (docs/TECHNICALS_V2_PLAN.md): volatility, regime and earnings context for one security.

Pure numpy on the security's own adjusted daily bars (the API passes ~3.5 years from the serving snapshot):

  range width   today's calibrated 20-session 90% band width vs the same band on each of the last 504 sessions. The
                band is 2 sqrt(q V(20)) with V(20) = 20 s2_t (+ the earnings jump J_t when an earnings day falls in
                (t, t+20]) from the envelope's EWMA filter (app.conformal.envelope: lambda 0.94, earnings days
                excluded); the history uses today's calibrated radius q, so the percentile compares the volatility
                term. squeeze <= 10th percentile, expansion >= 90th, otherwise normal. MODEL-BASED.
  realised vol  stdev of the last 20 daily log returns x sqrt(252), percentile vs the last 504 sessions
  implied vol   no per-stock implied-volatility source (US: only VIX-type index volatility) -> n/a
  ATR           Wilder ATR(14) / close, percentile vs the last 504 sessions
  ADX           Wilder ADX(14) with +DI / -DI: < 20 weak, 20-25 emerging, >= 25 trending, >= 40 strong
  trend         daily (close vs SMA50 and the SMA50 slope over 10 sessions) vs weekly (last weekly close vs the
                30-week SMA and its slope over 4 weeks): aligned up / aligned down / mixed / flat
Percentiles are the share of the lookback whose value is <= today's (today included), in [0, 1].
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import numpy as np

from app.conformal import envelope as V

LOOKBACK = 504                 # sessions (~2 years)
SQUEEZE, EXPANSION = 0.10, 0.90


def pct_rank(hist: np.ndarray, x: float | None) -> float | None:
    v = hist[np.isfinite(hist)]
    if x is None or not math.isfinite(x) or v.size < 60:
        return None
    return float((v <= x + 1e-15).mean())


def ewma_v20(closes: np.ndarray, E: np.ndarray, h: int = 20) -> np.ndarray:
    """(T,) V(h) of the envelope's EWMA filter (incl. the earnings jump when an event falls in (t, t+h])."""
    C = closes.reshape(-1, 1).astype(np.float64)
    Ec = E.reshape(-1, 1).astype(bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.vstack([[np.nan], np.diff(np.log(C), axis=0)])
    r = np.where(np.abs(np.nan_to_num(r)) > 1.0, np.nan, r)
    s2 = V.ewma_var(r, Ec)
    J = V.jump_var(r, Ec, s2)
    ke = V.first_event_offset(Ec, h)
    return V.horizon_V(s2, J, ke, (h,))[:, 0, 0]


def realised_vol(closes: np.ndarray, n: int = 20) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(closes))
    out = np.full(len(closes), np.nan)
    for t in range(n, len(closes)):
        w = r[t - n:t]
        w = w[np.isfinite(w)]
        if w.size >= n - 2:
            out[t] = float(np.std(w, ddof=1) * math.sqrt(252))
    return out


def wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    acc = np.nanmean(x[:n])
    out[n - 1] = acc
    for t in range(n, len(x)):
        v = x[t] if np.isfinite(x[t]) else acc
        acc = (acc * (n - 1) + v) / n
        out[t] = acc
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:  # noqa: E741
    pc = np.concatenate([[c[0]], c[:-1]])
    return np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))


def atr_pct(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> tuple[np.ndarray, np.ndarray]:  # noqa: E741
    atr = wilder(true_range(h, l, c), n)
    return atr, atr / c


def adx(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:  # noqa: E741
    """Wilder ADX(n), +DI, -DI."""
    up = np.concatenate([[np.nan], h[1:] - h[:-1]])
    dn = np.concatenate([[np.nan], l[:-1] - l[1:]])
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = true_range(h, l, c)
    tr[0] = np.nan
    atr = wilder(tr[1:], n)
    pdi = 100 * wilder(pdm[1:], n) / atr
    mdi = 100 * wilder(mdm[1:], n) / atr
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = 100 * np.abs(pdi - mdi) / (pdi + mdi)
    first = np.argmax(np.isfinite(dx)) if np.isfinite(dx).any() else len(dx)
    a = np.full(len(dx), np.nan)
    if len(dx) - first >= n:
        a[first:] = wilder(dx[first:], n)
    pad = np.array([np.nan])
    return np.concatenate([pad, a]), np.concatenate([pad, pdi]), np.concatenate([pad, mdi])


def adx_state(a: float | None) -> str:
    if a is None or not math.isfinite(a):
        return "n/a"
    return "strong" if a >= 40 else "trending" if a >= 25 else "emerging" if a >= 20 else "weak"


def sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= n:
        cs = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def _dir(close: float, ma: float, ma_prev: float) -> str:
    if not all(math.isfinite(x) for x in (close, ma, ma_prev)):
        return "n/a"
    rising = ma > ma_prev
    if close > ma and rising:
        return "up"
    if close < ma and not rising:
        return "down"
    return "flat"


def trend_alignment(dates: list[date], closes: np.ndarray) -> dict[str, Any]:
    d50 = sma(closes, 50)
    daily = _dir(closes[-1], d50[-1], d50[-11]) if len(closes) >= 61 else "n/a"
    # weekly closes: last close of each ISO week
    wk: dict[tuple[int, int], float] = {}
    for d, c in zip(dates, closes, strict=True):
        wk[d.isocalendar()[:2]] = float(c)
    w = np.array(list(wk.values()))
    w30 = sma(w, 30)
    weekly = _dir(w[-1], w30[-1], w30[-5]) if len(w) >= 35 else "n/a"
    if "n/a" in (daily, weekly):
        align = "n/a"
    elif daily == weekly and daily != "flat":
        align = f"aligned_{daily}"
    elif "flat" in (daily, weekly):
        align = "flat" if daily == weekly else "partial"
    else:
        align = "mixed"
    return {"daily": daily, "weekly": weekly, "alignment": align,
            "daily_close_vs_sma50": _r(closes[-1] / d50[-1] - 1) if math.isfinite(d50[-1]) else None,
            "weekly_close_vs_sma30w": _r(w[-1] / w30[-1] - 1) if len(w) and math.isfinite(w30[-1]) else None}


def _r(x: Any, nd: int = 6) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def range_state(p: float | None) -> str:
    if p is None:
        return "n/a"
    return "squeeze" if p <= SQUEEZE else "expansion" if p >= EXPANSION else "normal"


def context(dates: list[date], o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray,  # noqa: E741
            earn_days: list[date], q90_20: float | None, lookback: int = LOOKBACK) -> dict[str, Any]:
    """Volatility / regime context from adjusted daily bars (oldest first). q90_20 = today's calibrated 20-session
    90% marginal radius (cf_envelopes.qm_20_90) or None."""
    T = len(c)
    out: dict[str, Any] = {"n_bars": T, "lookback_sessions": lookback}
    lb = slice(max(0, T - lookback), T)
    # range width
    E = np.zeros(T, dtype=bool)
    if earn_days:
        dd = np.array(dates, dtype="datetime64[D]")
        ev = np.array(earn_days, dtype="datetime64[D]")
        k = np.searchsorted(dd, ev, side="left")
        ok = (k < T) & ((dd[np.minimum(k, T - 1)] - ev) <= np.timedelta64(4, "D"))
        E[k[ok]] = True
    rw: dict[str, Any] = {"level": 90, "horizon": 20, "width_log": None, "width_pct": None, "percentile": None,
                          "state": "n/a", "n": 0, "radius": _r(q90_20)}
    if q90_20 is not None and q90_20 > 0 and T >= 80:
        v20 = ewma_v20(c, E)
        w = 2 * np.sqrt(q90_20 * v20)
        hist = w[lb]
        now = float(w[-1]) if math.isfinite(w[-1]) else None
        p = pct_rank(hist, now)
        rw.update(width_log=_r(now), width_pct=_r(math.exp(now / 2) - math.exp(-now / 2)) if now is not None else None,
                  percentile=_r(p, 4), state=range_state(p), n=int(np.isfinite(hist).sum()),
                  median_width_log=_r(float(np.nanmedian(hist))) if np.isfinite(hist).any() else None)
    out["range_width"] = rw
    # realised vol
    rv = realised_vol(c)
    now = float(rv[-1]) if T and math.isfinite(rv[-1]) else None
    p = pct_rank(rv[lb], now)
    out["realised_vol"] = {"window": 20, "vol": _r(now, 5), "percentile": _r(p, 4), "state": range_state(p),
                           "n": int(np.isfinite(rv[lb]).sum())}
    # ATR
    atr, ap = atr_pct(h, l, c)
    nowp = float(ap[-1]) if T and math.isfinite(ap[-1]) else None
    p = pct_rank(ap[lb], nowp)
    out["atr"] = {"window": 14, "atr": _r(atr[-1]) if T else None, "atr_pct": _r(nowp, 5), "percentile": _r(p, 4),
                  "state": range_state(p), "n": int(np.isfinite(ap[lb]).sum())}
    # ADX
    a, pdi, mdi = adx(h, l, c)
    av = float(a[-1]) if T and math.isfinite(a[-1]) else None
    out["adx"] = {"window": 14, "adx": _r(av, 2), "plus_di": _r(pdi[-1], 2) if T else None,
                  "minus_di": _r(mdi[-1], 2) if T else None, "state": adx_state(av),
                  "direction": None if av is None else ("up" if pdi[-1] > mdi[-1] else "down")}
    out["trend"] = trend_alignment(dates, c) if T else {"daily": "n/a", "weekly": "n/a", "alignment": "n/a",
                                                        "daily_close_vs_sma50": None, "weekly_close_vs_sma30w": None}
    return out


NOTES = {
    "range_width": "Width of today's calibrated 20-session 90% range (model-based) vs the same band on each of the last "
                   "2 years of sessions (EWMA volatility term with today's calibrated radius; the earnings jump counts when "
                   "an event is inside the window). Squeeze = at or below the 10th percentile, expansion = at or above the "
                   "90th. Descriptive: a squeeze is not a breakout signal.",
    "realised_vol": "Annualised standard deviation of the last 20 daily log returns, and its percentile over the last 2 "
                    "years of the stock's own history.",
    "implied_vol": "Implied volatility: n/a - there is no per-stock option-implied volatility source (for the US only "
                   "VIX-type index volatility exists).",
    "atr": "Wilder ATR(14) as a share of the price, and its percentile over the last 2 years.",
    "adx": "Wilder ADX(14) trend strength (direction from +DI vs -DI): below 20 weak / no trend, 20-25 emerging, 25+ "
           "trending, 40+ strong. Strength only - it says nothing about what happens next.",
    "trend": "Daily trend = close vs the 50-day average and its 10-session slope; weekly = last weekly close vs the "
             "30-week average and its 4-week slope. Aligned when both point the same way.",
    "earnings": "Next expected earnings date (company / provider / model-projected, labelled) and, when it is within 20 "
                "sessions, the calibrated range uses the earnings-event calibration pool (wider). Typical move = median "
                "|CAR| over [-1, +1] sessions around the stock's past releases (market-model abnormal return).",
}
