"""#1 Evidence-based technical factors (pure numpy, no `app` imports).

Inputs are wide arrays (T sessions of a region calendar x n securities), NaN where a security did not print. Every
output dated t uses rows <= t only (trailing windows); the `causal` tests in tests/techv3 check it bit-for-bit.

Factors (sign = the direction the literature reports as predictive of HIGHER future returns):
  hi52    + close / max(close over the last 252 sessions)                         George & Hwang (2004)
  mom     + 12-1 momentum: log C[t-21] - log C[t-252]                              Jegadeesh & Titman (1993)
  rmom    + residual momentum: rolling 36-month OLS of monthly (21-session) log returns on the region market and
            sector returns; sum of the residuals of months t-12..t-2 divided by their standard deviation
            (Blitz, Huij & Martens 2011; months anchored at t, the most recent month is skipped)
  strev   - 1-month reversal: log C[t] - log C[t-21]                               Jegadeesh (1990), Lehmann (1990)
  maxret  - MAX: largest daily simple return over the last 21 sessions             Bali, Cakici & Whitelaw (2011)
  ivol    - idiosyncratic volatility: std of the residuals of a 21-session daily OLS on market + sector returns,
            annualised (Ang, Hodrick, Xing & Zhang 2006)
  indmom  + industry momentum: the region x sector equal-weight log return over the last 126 sessions
            (Moskowitz & Grinblatt 1999); ranked within the REGION (constant within a sector)
Market / sector returns: equal-weight mean of the daily log returns (clipped at +-50%) of the LIQUID names (20-day
ADV >= the liquidity floor at t-1) of the region / region x sector; a sector needs >= MIN_SECTOR names, otherwise
the regressions use the market alone.
"""

from __future__ import annotations

import math

import numpy as np

FACTORS = ("hi52", "mom", "rmom", "strev", "maxret", "ivol", "indmom")
SIGN = {"hi52": 1, "mom": 1, "rmom": 1, "strev": -1, "maxret": -1, "ivol": -1, "indmom": 1}
LABEL = {"hi52": "52-week-high proximity", "mom": "12-1 month momentum", "rmom": "Residual momentum",
         "strev": "1-month reversal", "maxret": "MAX (largest daily return, 1 month)", "ivol": "Idiosyncratic volatility",
         "indmom": "Industry momentum (6 months)"}
REFERENCE = {
    "hi52": "George & Hwang (2004), The 52-Week High and Momentum Investing, Journal of Finance 59(5)",
    "mom": "Jegadeesh & Titman (1993), Returns to Buying Winners and Selling Losers, Journal of Finance 48(1)",
    "rmom": "Blitz, Huij & Martens (2011), Residual Momentum, Journal of Empirical Finance 18(3)",
    "strev": "Jegadeesh (1990), Evidence of Predictable Behavior of Security Returns, Journal of Finance 45(3); "
             "Lehmann (1990), Fads, Martingales, and Market Efficiency, QJE 105(1)",
    "maxret": "Bali, Cakici & Whitelaw (2011), Maxing Out: Stocks as Lotteries, Journal of Financial Economics 99(2)",
    "ivol": "Ang, Hodrick, Xing & Zhang (2006), The Cross-Section of Volatility and Expected Returns, Journal of Finance 61(1)",
    "indmom": "Moskowitz & Grinblatt (1999), Do Industries Explain Momentum?, Journal of Finance 54(4)",
}
#: windows (sessions)
W52, MONTH, YEAR, IND_W = 252, 21, 252, 126
RM_BLOCKS, RM_FORM = 36, 11          # regression months, formation months (t-12 .. t-2)
RM_MIN_REG, RM_MIN_FORM = 24, 8
IVOL_W, IVOL_MIN = 21, 15
MAX_MIN = 15
HI52_MIN = 126
MIN_SECTOR = 5
RET_CLIP = 0.5
MAX_ABS_LOGRET = 1.0
FFILL_LIMIT = 5


# ------------------------------------------------------------------------------------------------ helpers


def ffill(X: np.ndarray, limit: int = FFILL_LIMIT) -> np.ndarray:
    """Forward-fill NaN along axis 0 up to `limit` rows (a later print resets the age)."""
    out = np.array(X, dtype=np.float64, copy=True)
    T = out.shape[0]
    last = np.full(out.shape[1:], np.nan)
    age = np.zeros(out.shape[1:], dtype=np.int64)
    for t in range(T):
        ok = np.isfinite(out[t])
        age = np.where(ok, 0, age + 1)
        last = np.where(ok, out[t], last)
        out[t] = np.where(ok, out[t], np.where(age <= limit, last, np.nan))
    return out


def shift(X: np.ndarray, k: int) -> np.ndarray:
    """X[t - k] at row t (k > 0: lag; k < 0: lead), NaN outside."""
    out = np.full(X.shape, np.nan)
    if k > 0:
        out[k:] = X[:-k]
    elif k < 0:
        out[:k] = X[-k:]
    else:
        out[:] = X
    return out


def log_returns(C: np.ndarray) -> np.ndarray:
    """Close-to-close log returns on each security's own print days (the next print spans a gap); |r| > 1 -> NaN."""
    lc = np.log(np.where(C > 0, C, np.nan))
    T = lc.shape[0]
    out = np.full(lc.shape, np.nan)
    last = np.full(lc.shape[1:], np.nan)
    for t in range(T):
        x = lc[t]
        ok = np.isfinite(x)
        out[t] = np.where(ok, x - last, np.nan)
        last = np.where(ok, x, last)
    out[np.abs(out) > MAX_ABS_LOGRET] = np.nan
    return out


def roll_sum(X: np.ndarray, w: int) -> tuple[np.ndarray, np.ndarray]:
    """Trailing w-row sums ignoring NaN and the count of finite values (rows < w use the available rows)."""
    ok = np.isfinite(X)
    z = np.where(ok, X, 0.0)
    cs = np.cumsum(z, axis=0)
    cn = np.cumsum(ok, axis=0)
    s, n = cs.copy(), cn.astype(np.float64)
    s[w:] -= cs[:-w]
    n[w:] -= cn[:-w]
    return s, n


def roll_mean(X: np.ndarray, w: int, min_n: int) -> np.ndarray:
    s, n = roll_sum(X, w)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n >= min_n, s / np.maximum(n, 1), np.nan)


def roll_max(X: np.ndarray, w: int, min_n: int) -> np.ndarray:
    """Trailing w-row max ignoring NaN (needs >= min_n finite values)."""
    from scipy.ndimage import maximum_filter1d  # noqa: PLC0415

    ok = np.isfinite(X)
    z = np.where(ok, X, -np.inf)
    # maximum_filter1d is centred: origin shifts it so the window is [t - w + 1, t]
    m = maximum_filter1d(z, size=w, axis=0, origin=(w - 1) // 2, mode="constant", cval=-np.inf)
    _, n = roll_sum(np.where(ok, 1.0, np.nan), w)
    return np.where((n >= min_n) & np.isfinite(m), m, np.nan)


def roll_min(X: np.ndarray, w: int, min_n: int) -> np.ndarray:
    return -roll_max(-X, w, min_n)


def strided_cumsum(Z: np.ndarray, s: int) -> np.ndarray:
    """cs[t] = Z[t] + cs[t - s] (per phase t mod s)."""
    cs = np.empty_like(Z)
    for p in range(min(s, Z.shape[0])):
        cs[p::s] = np.cumsum(Z[p::s], axis=0)
    return cs


def strided_window_sum(Z: np.ndarray, s: int, j0: int, W: int) -> np.ndarray:
    """sum_{j = j0}^{j0 + W - 1} Z[t - s j] (terms with negative index omitted)."""
    cs = strided_cumsum(Z, s)
    hi = shift(cs, s * j0)
    lo = shift(cs, s * (j0 + W))
    hi = np.nan_to_num(hi, nan=0.0)
    lo = np.nan_to_num(lo, nan=0.0)
    return hi - lo


# ------------------------------------------------------------------------------------------------ market / sector


def group_daily_returns(r: np.ndarray, liquid_prev: np.ndarray, groups: np.ndarray, min_names: int = MIN_SECTOR
                        ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Equal-weight daily log returns of the liquid names: the market (T,) and per group code (T,) (NaN when fewer
    than min_names liquid names printed). `liquid_prev` (T, n) bool = liquid at t - 1; groups (n,) codes."""
    rc = np.clip(r, -RET_CLIP, RET_CLIP)
    m = np.isfinite(rc) & liquid_prev
    z = np.where(m, rc, 0.0)
    cnt = m.sum(axis=1)
    with np.errstate(invalid="ignore", divide="ignore"):
        mkt = np.where(cnt >= min_names, z.sum(axis=1) / np.maximum(cnt, 1), np.nan)
    out = {}
    for g in np.unique(groups):
        k = groups == g
        c = m[:, k].sum(axis=1)
        with np.errstate(invalid="ignore", divide="ignore"):
            out[str(g)] = np.where(c >= min_names, z[:, k].sum(axis=1) / np.maximum(c, 1), np.nan)
    return mkt, out


def index_level(r: np.ndarray) -> np.ndarray:
    """Cumulative log level of a daily return series (NaN days contribute 0; validity tracked separately)."""
    return np.cumsum(np.nan_to_num(r, nan=0.0), axis=0)


def block_returns(level: np.ndarray, valid: np.ndarray, w: int = MONTH, min_valid: int = 15) -> np.ndarray:
    """w-session log return of an index level ending at each t; NaN when fewer than min_valid valid days."""
    out = level - shift(level, w)
    _, n = roll_sum(np.where(valid, 1.0, np.nan), w)
    return np.where(n >= min_valid, out, np.nan)


# ------------------------------------------------------------------------------------------------ OLS by window sums


def _ols2(n, sy, s1, s2, syy, s11, s22, sy1, sy2, s12, min_n):
    """Closed-form OLS of y on [1, x1, x2] from window sums -> (alpha, b1, b2, sse, ok2); falls back to the market
    alone (b2 = 0) where x2 is missing / collinear. All arguments arrays of the same shape."""
    with np.errstate(invalid="ignore", divide="ignore"):
        nn = np.maximum(n, 1.0)
        c11 = s11 - s1 * s1 / nn
        c22 = s22 - s2 * s2 / nn
        c12 = s12 - s1 * s2 / nn
        c1y = sy1 - s1 * sy / nn
        c2y = sy2 - s2 * sy / nn
        cyy = syy - sy * sy / nn
        det = c11 * c22 - c12 * c12
        ok2 = (n >= min_n) & (det > 1e-12 * np.maximum(c11 * c22, 1e-300)) & (c22 > 0)
        b1_2 = (c22 * c1y - c12 * c2y) / det
        b2_2 = (c11 * c2y - c12 * c1y) / det
        b1_1 = c1y / c11
        b1 = np.where(ok2, b1_2, b1_1)
        b2 = np.where(ok2, b2_2, 0.0)
        alpha = (sy - b1 * s1 - b2 * s2) / nn
        sse = np.where(ok2, cyy - b1_2 * c1y - b2_2 * c2y, cyy - b1_1 * c1y)
        okn = (n >= min_n) & (c11 > 0)
    return alpha, b1, b2, np.maximum(sse, 0.0), okn, ok2


def _sums(y: np.ndarray, x1: np.ndarray, x2: np.ndarray, fn) -> dict[str, np.ndarray]:
    """Window sums of the regression moments where y and x1 are finite; x2 missing -> its terms 0 and flagged."""
    m = np.isfinite(y) & np.isfinite(x1)
    m2 = m & np.isfinite(x2)
    Y = np.where(m, y, 0.0)
    X1 = np.where(m, x1, 0.0)
    X2 = np.where(m2, x2, 0.0)
    one = m.astype(np.float64)
    out = {"n": fn(one), "n2": fn(m2.astype(np.float64)), "sy": fn(Y), "s1": fn(X1), "s2": fn(X2), "syy": fn(Y * Y),
           "s11": fn(X1 * X1), "s22": fn(X2 * X2), "sy1": fn(Y * X1), "sy2": fn(Y * X2), "s12": fn(X1 * X2)}
    return out


def ivol(r: np.ndarray, rm: np.ndarray, rs: np.ndarray, w: int = IVOL_W, min_n: int = IVOL_MIN) -> np.ndarray:
    """Annualised std of the residuals of the trailing w-session OLS r ~ 1 + rm + rs (rm, rs (T, n) broadcast).
    A window where the sector return is missing on any used day falls back to the market-only regression."""
    def fn(Z: np.ndarray) -> np.ndarray:
        return roll_sum(Z, w)[0]

    S = _sums(r, rm, rs, fn)
    full = S["n2"] >= S["n"] - 0.5                 # sector present on every used day
    s2 = {k: np.where(full, S[k], 0.0) for k in ("s2", "s22", "sy2", "s12")}
    _a, _b1, _b2, sse, okn, ok2 = _ols2(S["n"], S["sy"], S["s1"], s2["s2"], S["syy"], S["s11"], s2["s22"], S["sy1"],
                                         s2["sy2"], s2["s12"], min_n)
    k = np.where(ok2 & full, 3.0, 2.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        v = sse / np.maximum(S["n"] - k, 1.0)
    return np.where(okn, np.sqrt(v * 252.0), np.nan)


def residual_momentum(y21: np.ndarray, xm21: np.ndarray, xs21: np.ndarray, s: int = MONTH, n_reg: int = RM_BLOCKS,
                      n_form: int = RM_FORM, min_reg: int = RM_MIN_REG, min_form: int = RM_MIN_FORM) -> np.ndarray:
    """Blitz-Huij-Martens residual momentum from 21-session block returns (each array: the block return ENDING at
    row t; block j of origin t is row t - 21 j). Regression over blocks j = 0..n_reg-1, residuals of the formation
    blocks j = 1..n_form (the most recent month j = 0 skipped), scaled by their standard deviation."""
    def reg(Z: np.ndarray) -> np.ndarray:
        return strided_window_sum(Z, s, 0, n_reg)

    def form(Z: np.ndarray) -> np.ndarray:
        return strided_window_sum(Z, s, 1, n_form)

    R = _sums(y21, xm21, xs21, reg)
    full = R["n2"] >= R["n"] - 0.5
    for k in ("s2", "s22", "sy2", "s12"):
        R[k] = np.where(full, R[k], 0.0)
    a, b1, b2, _sse, okn, _ok2 = _ols2(R["n"], R["sy"], R["s1"], R["s2"], R["syy"], R["s11"], R["s22"], R["sy1"],
                                        R["sy2"], R["s12"], min_reg)
    b2 = np.where(full, b2, 0.0)
    # formation-window moments (x2 terms only where the sector block exists; b2 = 0 otherwise)
    Fm = _sums(y21, xm21, np.where(np.isfinite(xs21), xs21, 0.0), form)
    n = Fm["n"]
    se = Fm["sy"] - n * a - b1 * Fm["s1"] - b2 * Fm["s2"]
    see = (Fm["syy"] + n * a * a + b1 * b1 * Fm["s11"] + b2 * b2 * Fm["s22"]
           - 2 * a * Fm["sy"] - 2 * b1 * Fm["sy1"] - 2 * b2 * Fm["sy2"]
           + 2 * a * b1 * Fm["s1"] + 2 * a * b2 * Fm["s2"] + 2 * b1 * b2 * Fm["s12"])
    with np.errstate(invalid="ignore", divide="ignore"):
        var = (see - se * se / np.maximum(n, 1.0)) / np.maximum(n - 1.0, 1.0)
        out = se / np.sqrt(var)
    ok = okn & (n >= min_form) & (var > 1e-14)
    return np.where(ok, out, np.nan)


# ------------------------------------------------------------------------------------------------ factor panel


def compute(C: np.ndarray, r: np.ndarray, rm: np.ndarray, rs_by_col: np.ndarray, lm: np.ndarray, lm_valid: np.ndarray,
            ls_by_col: np.ndarray, ls_valid_by_col: np.ndarray) -> dict[str, np.ndarray]:
    """Every factor for a chunk of securities.

    C (T, n) adjusted close (NaN = no print); r (T, n) log returns; rm (T,) market daily log return; rs_by_col (T, n)
    the security's sector daily return; lm (T,) market log level, lm_valid (T,) bool; ls_by_col / ls_valid_by_col
    (T, n) the security's sector log level and validity. Returns {factor: (T, n) float64}."""
    T, n = C.shape
    lcf = ffill(np.log(np.where(C > 0, C, np.nan)))
    out: dict[str, np.ndarray] = {}
    cf = np.exp(lcf)
    hi = roll_max(np.where(np.isfinite(C), C, np.nan), W52, HI52_MIN)
    with np.errstate(invalid="ignore", divide="ignore"):
        out["hi52"] = np.where(np.isfinite(C), C / hi, np.where(np.isfinite(cf), cf / hi, np.nan))
    out["mom"] = shift(lcf, MONTH) - shift(lcf, YEAR)
    out["strev"] = lcf - shift(lcf, MONTH)
    mx = roll_max(r, MONTH, MAX_MIN)
    out["maxret"] = np.expm1(mx)
    rmb = np.broadcast_to(rm[:, None], (T, n))
    out["ivol"] = ivol(r, rmb, rs_by_col)
    y21 = lcf - shift(lcf, MONTH)
    xm = block_returns(lm, lm_valid)
    xm21 = np.broadcast_to(xm[:, None], (T, n))
    xs21 = ls_by_col - shift(ls_by_col, MONTH)
    _, nv = roll_sum(np.where(ls_valid_by_col, 1.0, np.nan), MONTH)
    xs21 = np.where(nv >= 15, xs21, np.nan)
    out["rmom"] = residual_momentum(y21, np.ascontiguousarray(xm21), xs21)
    ind = ls_by_col - shift(ls_by_col, IND_W)
    _, ni = roll_sum(np.where(ls_valid_by_col, 1.0, np.nan), IND_W)
    out["indmom"] = np.where(ni >= int(0.8 * IND_W), ind, np.nan)
    # a factor is only defined where the security has a (recent) price
    alive = np.isfinite(lcf)
    for k in out:
        out[k] = np.where(alive, out[k], np.nan)
    return out


def forward_returns(C: np.ndarray, horizons: tuple[int, ...] = (5, 20, 60), lag: int = 1) -> dict[int, np.ndarray]:
    """Labels: log C[t + lag + h] - log C[t + lag] (execution at the close of t + lag, the pit_rule convention);
    forward-filled prices (limit FFILL_LIMIT) so a missing print near the end does not drop the label."""
    lcf = ffill(np.log(np.where(C > 0, C, np.nan)))
    base = shift(lcf, -lag)
    return {h: shift(lcf, -(lag + h)) - base for h in horizons}


def sector_valid(level_ok: np.ndarray) -> np.ndarray:
    return np.asarray(level_ok, dtype=bool)


def annualise(x: float) -> float:
    return x * math.sqrt(252.0)
