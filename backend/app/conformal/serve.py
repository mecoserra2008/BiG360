"""Query-time helpers for the conformal API (P3 of docs/CONFORMAL_TECHNICALS_PLAN.md). MODEL-BASED.

Scope follows the P4 gate (docs/CONFORMAL_VALIDATION.md):
  - marginal per-horizon bands (qm_*): "calibrated range" - on target per horizon over the 2010-2026 walk-forward
    (89.6-90.0% at 90%), but no sharper than a conformally calibrated ATR band;
  - touch probabilities: calibrated path probabilities (predicted vs realised within 0.2 pp);
  - next-bar bands: coverage passes, sharpness fails -> experimental only;
  - the joint simultaneous envelope (b{h}_*) FAILED and is never served here;
  - peer-group ellipsoids: PASS as a descriptive measure (anomaly percentile, pair / basket bands); the break-away
    fade had no economic value -> never a trading signal.

Pure functions (numpy only): the router (app.api.routers.conformal), the alert evaluators and tests call them.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np

HORIZONS = (1, 5, 10, 20)
LEVELS = (80, 90, 95)
NB_FIELDS = (("h", "high"), ("l", "low"), ("c", "close"))
MAX_TOUCH_HORIZON = 252
VALIDATION_DOC = "docs/CONFORMAL_VALIDATION.md"

LABELS = {
    "model_based": "Model-based. Coverage is marginal over history (all securities and days of the pool), not a "
                   "statement about any single day.",
    "calibrated_range": "Calibrated range: per-horizon band calibrated to X% over history (2010-2026 walk-forward: "
                        "89.6-90.0% realised at the 90% level). It is not 'X% chance today', and it is no sharper "
                        "than a calibrated ATR band - the gain over a 2xATR band comes from calibration.",
    "touch": "Historical frequency of touching the level within h sessions for similar setups (same calibration "
             "pool, standardised distance). Calibrated within 0.2 pp over 2011-2026 (US); a path frequency, not a "
             "Gaussian probability.",
    "touch_extrapolated": "Horizons beyond 20 sessions reuse the 20-session curve with the variance scaled by the "
                          "horizon: an extrapolation, NOT validated.",
    "fan": "Calibrated range for every horizon 1-20 sessions: each horizon's band is calibrated alone to X% over history "
           "(pooled per-horizon split conformal, 2010-2026 walk-forward on target at every h), not 'X% chance today' "
           "and not a joint path band. No direction call: the centre is the last close.",
    "nextbar": "Experimental: next-bar high / low / close bands pass coverage but failed the sharpness test (wider "
               "than a calibrated ATR box); for Model Studio / Backtest Lab use only.",
    "peer": "Descriptive, model-based; not a trading signal. Joint-anomaly percentile of the peer group's one-day move "
            "vs the GCN-GRU + ACI ellipsoid (passed the coverage and sharpness gate); fading the break-away name had "
            "no economic value in validation.",
    "pair": "Calibrated next-session band for a linear combination of members, a'mu +- sqrt(q a'Sigma a): a "
            "projection of the 90% / 95% joint set, hence conservative (simultaneous over all combinations; members "
            "covered ~99.9% in validation). Model-based, log-return units.",
    "pair_marginal": "Marginal band for this combination only: the split-conformal quantile of its own standardised "
                     "next-session moves under the same model over the last ~125 sessions (n_cal). Narrower than the "
                     "joint projection; calibrated on a short window, not separately walk-forward validated.",
}

GATES = [
    {"application": "peer", "title": "Peer-group ellipsoid (GCN-GRU + ACI)", "verdict": "pass",
     "shown_as": "descriptive anomaly percentile and pair / basket bands",
     "detail": "Joint coverage 90.4% / 95.2%; worst regime Covid 85.3% at 90%; 10% sharper than EWMA. Economic test "
               "(fade the break-away name) null: not a trading signal."},
    {"application": "envelope_marginal", "title": "Marginal per-horizon bands (calibrated range)", "verdict": "pass_coverage",
     "shown_as": "calibrated range on the Company 360 chart",
     "detail": "Per-horizon coverage 89.6-90.0% at 90%; about 40% narrower than the 2xATR band (98% coverage) but "
               "not sharper than a calibrated ATR band."},
    {"application": "touch", "title": "Touch probabilities", "verdict": "pass",
     "shown_as": "chance-of-touch column, thesis target / stop",
     "detail": "Predicted vs realised within 0.2 pp at every distance and horizon (US mvp 2011-2026)."},
    {"application": "envelope_joint", "title": "Joint 1/5/10/20 simultaneous envelope", "verdict": "fail",
     "shown_as": "not shown",
     "detail": "Covid regime 77% at 90%; axis projections 35% wider than a calibrated ATR box."},
    {"application": "nextbar", "title": "Next-bar high / low / close bands", "verdict": "fail_sharpness",
     "shown_as": "experimental (Studio / Backtest Lab)",
     "detail": "Coverage on target in every regime with ACI; projections 43% wider than a calibrated ATR box."},
    {"application": "econ_breakout", "title": "Band breakout (Backtest Lab)", "verdict": "fail",
     "shown_as": "experimental signal", "detail": "Not better than a naive N-day-high breakout (t = -0.4 to -3.2)."},
    {"application": "econ_stops", "title": "Calibrated band stops (Backtest Lab)", "verdict": "mixed",
     "shown_as": "experimental stop / target mode",
     "detail": "Band stops beat ATR stops (+6%/yr, t = 2.7-3.6) but no stop beat 'no stop' on 12-1 momentum."},
    {"application": "econ_peer", "title": "Peer break-away fade (Backtest Lab)", "verdict": "fail",
     "shown_as": "experimental signal", "detail": "Negative Sharpe after costs; not better than a naive fade."},
]


def f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def add_sessions(d: date, n: int) -> date:
    """Approximate target date n trading sessions after d (weekdays; exchange holidays ignored)."""
    out = np.busday_offset(np.datetime64(d, "D"), n, roll="forward")
    return out.astype(object)


# ------------------------------------------------------------------------------------------------ marginal bands


def half_width(row: dict[str, Any], h: int, lvl: int) -> float | None:
    q, v = f(row.get(f"qm_{h}_{lvl}")), f(row.get(f"v_{h}"))
    if q is None or v is None or q < 0 or v <= 0:
        return None
    return math.sqrt(q * v)


def marginal_bands(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Calibrated (marginal, single-horizon) price bands: close x exp(mu +- sqrt(qm v))."""
    lc = f(row.get("last_close"))
    out: list[dict[str, Any]] = []
    if lc is None or lc <= 0:
        return out
    d0 = row.get("as_of_date")
    for h in HORIZONS:
        mu = f(row.get(f"mu_{h}")) or 0.0
        for lvl in LEVELS:
            hw = half_width(row, h, lvl)
            if hw is None:
                continue
            out.append({"horizon": h, "level": lvl, "lo": lc * math.exp(mu - hw), "hi": lc * math.exp(mu + hw),
                        "half_width_log": hw, "lo_pct": math.exp(mu - hw) - 1.0, "hi_pct": math.exp(mu + hw) - 1.0,
                        "target_date": add_sessions(d0, h).isoformat() if isinstance(d0, date) else None})
    return out


# ------------------------------------------------------------------------------------------------ fan (T1)

FAN_HORIZONS = tuple(range(1, 21))


def _list_at(row: dict[str, Any], key: str, h: int) -> float | None:
    v = row.get(key)
    if v is None or isinstance(v, (str, bytes)):
        return None
    try:
        return f(list(v)[h - 1]) if len(v) >= h else None
    except TypeError:
        return None


def _interp_anchor(row: dict[str, Any], key: str, h: int) -> float | None:
    """Linear interpolation in h between the stored 1/5/10/20 values of `key.format(h)`."""
    if h in HORIZONS:
        return f(row.get(key.format(h)))
    lo = max(a for a in HORIZONS if a < h)
    hi = min(a for a in HORIZONS if a > h)
    a, b = f(row.get(key.format(lo))), f(row.get(key.format(hi)))
    if a is None or b is None:
        return None
    w = (h - lo) / (hi - lo)
    return (1 - w) * a + w * b


def fan_variance(row: dict[str, Any], h: int) -> tuple[float | None, bool]:
    """(V(h), from_fan): the job's per-h variance term structure (fan_v); fallback for rows written before the fan
    existed = the EWMA term structure h sigma_d^2 (+ the earnings jump when the first expected event is within h),
    else linear in h between the stored v_1/5/10/20."""
    v = _list_at(row, "fan_v", h)
    if v is not None and v > 0:
        return v, True
    if h in HORIZONS and f(row.get(f"v_{h}")):
        return f(row.get(f"v_{h}")), False
    s = f(row.get("sigma_d"))
    if s is not None and (row.get("filter") or "ewma") == "ewma":
        out = h * s * s
        ed, j = row.get("earn_days_ahead"), f(row.get("earn_jump_var"))
        if j and ed is not None and 0 < int(ed) <= h:
            out += j
        return out, False
    return _interp_anchor(row, "v_{}", h), False


def fan_radius(row: dict[str, Any], h: int, lvl: int) -> tuple[float | None, bool]:
    """(q(h), calibrated_per_h): the per-h calibrated marginal radius (fan_qm_*); fallback = linear interpolation in h
    between the calibrated 1/5/10/20 radii (qm_*), flagged."""
    q = _list_at(row, f"fan_qm_{lvl}", h)
    if q is not None and q >= 0:
        return q, True
    return _interp_anchor(row, "qm_{}_" + str(lvl), h), h in HORIZONS


def fan_points(row: dict[str, Any], dates: list[date]) -> tuple[list[dict[str, Any]], str]:
    """Calibrated marginal band at every horizon h = 1..len(dates) (<= 20) as prices: close x exp(+- sqrt(q(h) V(h)))
    at 80 / 90 / 95%, dated by the exchange sessions after the origin. Returns (points, source) with source
    calibrated (per-h radii from the envelope job), interpolated (radius interpolated between 1/5/10/20) or none.
    No drift: the centre is the last close."""
    lc = f(row.get("last_close"))
    if lc is None or lc <= 0:
        return [], "none"
    ed = row.get("earn_days_ahead")
    ed = int(ed) if ed is not None else None
    pts: list[dict[str, Any]] = []
    all_cal = True
    for h, d in zip(FAN_HORIZONS, dates, strict=False):
        V, _ = fan_variance(row, h)
        if V is None or V <= 0:
            return [], "none"
        p: dict[str, Any] = {"h": h, "date": d.isoformat(), "event_pool": bool(ed is not None and 0 < ed <= h)}
        for lvl in LEVELS:
            q, cal = fan_radius(row, h, lvl)
            if q is None or q < 0:
                return [], "none"
            all_cal = all_cal and cal
            hw = math.sqrt(q * V)
            p[f"lo_{lvl}"], p[f"hi_{lvl}"] = lc * math.exp(-hw), lc * math.exp(hw)
        pts.append(p)
    return pts, ("calibrated" if all_cal else "interpolated")


def fan_sessions(d0: date, n: int, mic: str | None, region: str | None, settings: Any = None) -> tuple[list[date], str]:
    """The next n exchange sessions after d0 from the O1 calendar cache (app.ops.calendars, data/cache/ops/sessions;
    never built at request time): the listing's MIC when cached, else the region's coverage calendar, else Mon-Fri."""
    from app.ops import calendars as cal  # noqa: PLC0415
    from app.ops.paths import read_json  # noqa: PLC0415

    import bisect  # noqa: PLC0415

    for m in [x for x in (CAL_ALIAS.get(mic or "", mic), REGION_CALENDAR.get(region or "")) if x]:
        path = cal._cache_path(m, settings)
        try:
            mt = path.stat().st_mtime
        except OSError:
            continue
        hit = _CAL_CACHE.get(str(path))
        if hit is None or hit[0] != mt:
            doc = read_json(path, {}) or {}
            try:
                hit = (mt, sorted(date.fromisoformat(x) for x in doc.get("sessions") or []))
            except ValueError:
                continue
            _CAL_CACHE[str(path)] = hit
        days = hit[1]
        i = bisect.bisect_right(days, d0)
        if len(days) - i >= n:
            return days[i:i + n], m
    out, d = [], d0
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out, "weekdays"


_CAL_CACHE: dict[str, tuple[float, list[date]]] = {}
#: exchanges sharing a holiday calendar with a cached one (US venues follow NYSE)
CAL_ALIAS = {"XNAS": "XNYS", "ARCX": "XNYS", "XASE": "XNYS", "BATS": "XNYS", "XTXS": "XNYS", "IEXG": "XNYS",
             "XNGS": "XNYS", "XNCM": "XNYS", "XNMS": "XNYS", "XJPX": "XTKS", "XSHE": "XSHG", "XFRA": "XETR"}
#: region -> exchange calendar (contracts/coverage.yaml prices.* entries)
REGION_CALENDAR = {"US": "XNYS", "CA": "XTSE", "EU": "XETR", "UK": "XLON", "JP": "XTKS", "KR": "XKRX", "HK": "XHKG",
                   "CN": "XSHG", "LATAM": "BVMF", "APAC": "XTAI"}


def nextbar_bands(row: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for k, name in NB_FIELDS:
        for lvl in LEVELS:
            lo, hi = f(row.get(f"nb_{k}_{lvl}_lo")), f(row.get(f"nb_{k}_{lvl}_hi"))
            if lo is None or hi is None:
                continue
            out.append({"field": name, "level": lvl, "lo": lo, "hi": hi})
    return out


def outside_band(move_log: float, row: dict[str, Any], h: int, lvl: int) -> bool | None:
    """Was a realised h-session log move outside the marginal band emitted at the origin row?"""
    hw = half_width(row, h, lvl)
    if hw is None or move_log is None or not math.isfinite(move_log):
        return None
    mu = f(row.get(f"mu_{h}")) or 0.0
    return abs(move_log - mu) > hw


# ------------------------------------------------------------------------------------------------ touch


def curve_index(curves: list[dict[str, Any]]) -> dict[tuple[str, int, str, str], tuple[np.ndarray, np.ndarray, int]]:
    out = {}
    for c in curves:
        u = np.asarray(c.get("u") or [], dtype=np.float64)
        p = np.asarray(c.get("probs") or [], dtype=np.float64)
        if u.size and u.size == p.size and np.isfinite(u).all():
            out[(c["pool_key"], int(c["horizon"]), c["side"], c["kind"])] = (p, u, int(c.get("n") or 0))
    return out


def pick_curve(idx: dict, pool_key: str, region: str | None, h: int, side: str, kind: str
               ) -> tuple[tuple[np.ndarray, np.ndarray, int] | None, dict[str, Any]]:
    """Pool curve with fallbacks: same pool other kind, region-wide pool ('{region}|*'), any pool of the region."""
    cands = [(pool_key, kind), (pool_key, "normal" if kind == "event" else "event")]
    if region:
        cands += [(f"{region}|*", kind), (f"{region}|*", "normal")]
    for pk, kd in cands:
        c = idx.get((pk, h, side, kd))
        if c is not None:
            return c, {"pool_key": pk, "kind": kd, "n": c[2], "fallback": (pk, kd) != (pool_key, kind)}
    if region:
        for (pk, hh, sd, kd), c in sorted(idx.items()):
            if hh == h and sd == side and pk.startswith(f"{region}|"):
                return c, {"pool_key": pk, "kind": kd, "n": c[2], "fallback": True}
    return None, {}


def p_from_curve(u: float, probs: np.ndarray, q: np.ndarray) -> float:
    """1 - CDF of the standardised running extreme at u (app.conformal.envelope.touch_probability)."""
    if u <= 0:
        return 1.0
    if u <= q[0]:
        return float(1.0 - probs[0] * (u / q[0])) if q[0] > 0 else float(1.0 - probs[0])
    if u >= q[-1]:
        return float(0.5 * (1.0 - probs[-1]))
    return float(1.0 - np.interp(u, q, probs))


def horizon_variance(row: dict[str, Any], h: int) -> tuple[float | None, bool]:
    """V(h) for the touch distance: the envelope's own v_h on validated horizons; beyond 20 sessions an
    extrapolation h x sigma_d^2 (+ the earnings jump when an event is expected within h)."""
    if h in HORIZONS:
        return f(row.get(f"v_{h}")), False
    s = f(row.get("sigma_d"))
    if s is None:
        v20 = f(row.get("v_20"))
        return (v20 * h / 20.0 if v20 else None), True
    v = h * s * s
    j = f(row.get("earn_jump_var"))
    ed = row.get("earn_days_ahead")
    if j and (row.get("earn_in_window") or (ed is not None and int(ed) <= h)):
        v += j
    return v, True


def curve_horizon(h: int) -> int:
    if h in HORIZONS:
        return h
    if h > 20:
        return 20
    return min(HORIZONS, key=lambda x: (abs(x - h), x))


def touch(level: float, row: dict[str, Any], h: int, idx: dict) -> dict[str, Any]:
    """P(touch `level` within h sessions) from the envelope row and the pool's calibrated curves."""
    lc = f(row.get("last_close"))
    out: dict[str, Any] = {"price": level, "p_touch": None, "u": None, "side": None, "distance_pct": None,
                           "extrapolated": h not in HORIZONS, "curve": None}
    if lc is None or lc <= 0 or not (level and level > 0):
        return out
    x = math.log(level / lc)
    out["distance_pct"] = level / lc - 1.0
    out["side"] = "up" if x >= 0 else "down"
    V, extrap = horizon_variance(row, h)
    out["extrapolated"] = extrap or h not in HORIZONS
    if V is None or V <= 0:
        return out
    u = abs(x) / math.sqrt(V)
    out["u"] = u
    kind = "event" if row.get("earn_in_window") else "normal"
    c, meta = pick_curve(idx, str(row.get("pool_key") or ""), row.get("region"), curve_horizon(h), out["side"], kind)
    if c is None:
        return out
    probs, q, _ = c
    out["p_touch"] = 1.0 if x == 0 else p_from_curve(u, probs, q)
    out["curve"] = {**meta, "horizon": curve_horizon(h)}
    return out


# ------------------------------------------------------------------------------------------------ peer groups / pairs


def law(members: list[dict[str, Any]]) -> tuple[list[str], np.ndarray, np.ndarray, np.ndarray]:
    """(ids, mu (N,), d (N,), L (N, 4)) from conformal_peer_members rows of one group-date (next-session law)."""
    rows = [m for m in members if f(m.get("d_next")) is not None]
    ids = [m["security_id"] for m in rows]
    mu = np.array([f(m.get("mu_next")) or 0.0 for m in rows], dtype=np.float64)
    d = np.array([f(m.get("d_next")) for m in rows], dtype=np.float64)
    L = np.array([[f(m.get(f"l{k}_next")) or 0.0 for k in range(1, 5)] for m in rows], dtype=np.float64).reshape(len(rows), 4)
    return ids, mu, d, L


def combo_moments(a: np.ndarray, mu: np.ndarray, d: np.ndarray, L: np.ndarray) -> tuple[float, float]:
    """a'mu and a'Sigma a for Sigma = diag(d) + L L'."""
    m = float(a @ mu)
    la = L.T @ a
    var = float((a * a) @ d + la @ la)
    return m, max(var, 0.0)


def cov_ij(i: int, j: int, d: np.ndarray, L: np.ndarray) -> float:
    return float(L[i] @ L[j] + (d[i] if i == j else 0.0))


def hedge_beta(i: int, j: int, d: np.ndarray, L: np.ndarray) -> float:
    """Minimum-variance hedge ratio of i on j under the predicted covariance."""
    vj = cov_ij(j, j, d, L)
    return cov_ij(i, j, d, L) / vj if vj > 0 else 1.0


def combo_abs_z(by_date: dict[Any, list[dict[str, Any]]], w: dict[str, float]) -> list[tuple[Any, float]]:
    """Realised standardised moves of the combination a'Y: for consecutive sessions (s, t) the law emitted at s and
    the members' returns of t -> |a'r_t - a'mu_s| / sqrt(a' Sigma_s a). Sessions where a leg is missing are skipped."""
    days = sorted(by_date)
    out: list[tuple[Any, float]] = []
    for s, t in zip(days, days[1:], strict=False):
        ids, mu, d, L = law(by_date[s])
        pos = {x: k for k, x in enumerate(ids)}
        rr = {m["security_id"]: f(m.get("r")) for m in by_date[t]}
        if not all(x in pos and rr.get(x) is not None for x in w):
            continue
        a = np.zeros(len(ids))
        for x, v in w.items():
            a[pos[x]] = v
        m, var = combo_moments(a, mu, d, L)
        if var <= 0:
            continue
        val = sum(v * (rr[x] or 0.0) for x, v in w.items())
        out.append((t, abs(val - m) / math.sqrt(var)))
    return out


def conformal_abs(zs: list[float], level: int, min_n: int = 30) -> float | None:
    """Split-conformal quantile of |z|: the ceil((n + 1) level / 100)-th smallest (None when n is too small)."""
    n = len(zs)
    if n < min_n:
        return None
    k = math.ceil((n + 1) * level / 100.0)
    if k > n:
        return None
    return float(sorted(zs)[k - 1])


def band(mean: float, var: float, q: float | None) -> tuple[float, float] | None:
    if q is None or not math.isfinite(q) or q < 0:
        return None
    hw = math.sqrt(q * var)
    return mean - hw, mean + hw
