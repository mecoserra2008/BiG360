"""Numba kernels for the technicals package (owner: technicals).

All kernels are `nogil` so a thread pool scales across cores, and `cache=True` so spawned worker processes reuse the
compiled code. Conventions:
* bar arrays o/h/l/c/v are float64 of length n (adjusted prices), atr is Wilder ATR14 including bar t;
* a value used *at* bar t must only depend on bars < t (point-in-time): kernels read atr[t-1], never atr[t];
* index n (one past the last real bar) is the "next session" — level instances with start <= n < end are the
  levels active for the next session (that is the tech_levels snapshot).
"""

from __future__ import annotations

import math

import numpy as np
from numba import njit

_NB = {"cache": True, "nogil": True}


# ------------------------------------------------------------------------------------------------ indicators


@njit(**_NB)
def atr_wilder(h, l, c, period):  # noqa: E741
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    tr = np.empty(n)
    tr[0] = h[0] - l[0]
    for i in range(1, n):
        a = h[i] - l[i]
        b = abs(h[i] - c[i - 1])
        d = abs(l[i] - c[i - 1])
        tr[i] = max(a, max(b, d))
    s = 0.0
    for i in range(1, period + 1):
        s += tr[i]
    out[period] = s / period
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


@njit(**_NB)
def rsi_wilder(c, period):
    n = c.shape[0]
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    g = 0.0
    lo = 0.0
    for i in range(1, period + 1):
        d = c[i] - c[i - 1]
        if d > 0:
            g += d
        else:
            lo -= d
    ag = g / period
    al = lo / period
    out[period] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    for i in range(period + 1, n):
        d = c[i] - c[i - 1]
        up = d if d > 0 else 0.0
        dn = -d if d < 0 else 0.0
        ag = (ag * (period - 1) + up) / period
        al = (al * (period - 1) + dn) / period
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


@njit(**_NB)
def sma_prior(c, w):
    """out[t] = mean(c[t-w .. t-1]) for t in 0..n (length n+1; value known before bar t)."""
    n = c.shape[0]
    out = np.full(n + 1, np.nan)
    s = 0.0
    for t in range(1, n + 1):
        s += c[t - 1]
        if t > w:
            s -= c[t - 1 - w]
        if t >= w:
            out[t] = s / w
    return out


@njit(**_NB)
def rolling_extreme_prior(x, w, is_max, min_periods):
    """value/argindex of max (or min) of x[t-w .. t-1], for t in 0..n (length n+1). O(n) monotonic deque."""
    n = x.shape[0]
    val = np.full(n + 1, np.nan)
    arg = np.full(n + 1, -1, np.int64)
    dq = np.empty(n, np.int64)
    head = 0
    tail = 0
    for t in range(1, n + 1):
        i = t - 1  # push bar i
        xi = x[i]
        while tail > head:
            xj = x[dq[tail - 1]]
            if (is_max and xj <= xi) or ((not is_max) and xj >= xi):
                tail -= 1
            else:
                break
        dq[tail] = i
        tail += 1
        while dq[head] < t - w:
            head += 1
        if t >= min_periods:
            val[t] = x[dq[head]]
            arg[t] = dq[head]
    return val, arg


# ------------------------------------------------------------------------------------------------ pivots


@njit(**_NB)
def fractal_flags(h, l, k):  # noqa: E741
    """Fractal pivots: high[i] > highs of the k bars before and >= the k bars after (mirror for lows)."""
    n = h.shape[0]
    fh = np.zeros(n, np.bool_)
    fl = np.zeros(n, np.bool_)
    for i in range(k, n - k):
        okh = True
        okl = True
        for j in range(1, k + 1):
            if not (h[i] > h[i - j] and h[i] >= h[i + j]):
                okh = False
            if not (l[i] < l[i - j] and l[i] <= l[i + j]):
                okl = False
            if not okh and not okl:
                break
        fh[i] = okh
        fl[i] = okl
    return fh, fl


@njit(**_NB)
def zigzag(h, l, atr, mult):  # noqa: E741
    """ATR ZigZag. Returns (idx, price, dir(+1 high/-1 low), confirm_idx) of confirmed pivots, in time order.

    A pivot is confirmed on the first bar whose opposite extreme is >= mult*ATR(that bar) away from the running extreme;
    it is therefore known from confirm_idx + 1 on.
    """
    n = h.shape[0]
    pidx = np.empty(n, np.int64)
    ppx = np.empty(n)
    pdir = np.empty(n, np.int64)
    pconf = np.empty(n, np.int64)
    m = 0
    start = -1
    for i in range(n):
        if atr[i] > 0:
            start = i
            break
    if start < 0:
        return pidx[:0], ppx[:0], pdir[:0], pconf[:0]
    direction = 0
    hi = h[start]
    hi_i = start
    lo = l[start]
    lo_i = start
    for t in range(start + 1, n):
        a = atr[t] * mult
        if not (a > 0):
            continue
        if direction == 0:
            if h[t] > hi:
                hi = h[t]
                hi_i = t
            if l[t] < lo:
                lo = l[t]
                lo_i = t
            if hi - l[t] >= a and hi_i < t:
                pidx[m] = hi_i; ppx[m] = hi; pdir[m] = 1; pconf[m] = t; m += 1  # noqa: E702
                direction = -1
                lo = l[t]
                lo_i = t
            elif h[t] - lo >= a and lo_i < t:
                pidx[m] = lo_i; ppx[m] = lo; pdir[m] = -1; pconf[m] = t; m += 1  # noqa: E702
                direction = 1
                hi = h[t]
                hi_i = t
        elif direction == -1:  # seeking a low
            if l[t] < lo:
                lo = l[t]
                lo_i = t
            elif h[t] - lo >= a:
                pidx[m] = lo_i; ppx[m] = lo; pdir[m] = -1; pconf[m] = t; m += 1  # noqa: E702
                direction = 1
                hi = h[t]
                hi_i = t
        else:  # seeking a high
            if h[t] > hi:
                hi = h[t]
                hi_i = t
            elif hi - l[t] >= a:
                pidx[m] = hi_i; ppx[m] = hi; pdir[m] = 1; pconf[m] = t; m += 1  # noqa: E702
                direction = -1
                lo = l[t]
                lo_i = t
    return pidx[:m].copy(), ppx[:m].copy(), pdir[:m].copy(), pconf[:m].copy()


@njit(**_NB)
def structure_breaks(c, pidx, ppx, pdir, pconf):
    """Break of Structure / Change of Character from ZigZag pivots.

    Walks the bars; a pivot becomes 'known' at bar t when pconf < t. The first close beyond the last known high (low)
    pivot breaks it: BOS if the prevailing trend already pointed that way, CHoCH otherwise.
    Returns (break_bar, price, kind(1=BOS, 2=CHoCH), dir(+1 up/-1 down), pivot_idx) and trend per bar (length n).
    """
    n = c.shape[0]
    npv = pidx.shape[0]
    bb = np.empty(npv + 1, np.int64)
    bp = np.empty(npv + 1)
    bk = np.empty(npv + 1, np.int64)
    bd = np.empty(npv + 1, np.int64)
    bi = np.empty(npv + 1, np.int64)
    trend_arr = np.zeros(n, np.int64)
    m = 0
    p = 0
    trend = 0
    has_h = False
    has_l = False
    h_px = 0.0
    l_px = 0.0
    h_i = -1
    l_i = -1
    h_broken = True
    l_broken = True
    for t in range(n):
        while p < npv and pconf[p] < t:
            if pdir[p] == 1:
                has_h = True; h_px = ppx[p]; h_i = pidx[p]; h_broken = False  # noqa: E702
            else:
                has_l = True; l_px = ppx[p]; l_i = pidx[p]; l_broken = False  # noqa: E702
            p += 1
        if has_h and (not h_broken) and c[t] > h_px:
            bb[m] = t; bp[m] = h_px; bk[m] = 1 if trend == 1 else 2; bd[m] = 1; bi[m] = h_i  # noqa: E702
            m += 1
            trend = 1
            h_broken = True
        if has_l and (not l_broken) and c[t] < l_px:
            bb[m] = t; bp[m] = l_px; bk[m] = 1 if trend == -1 else 2; bd[m] = -1; bi[m] = l_i  # noqa: E702
            m += 1
            trend = -1
            l_broken = True
        trend_arr[t] = trend
    return bb[:m].copy(), bp[:m].copy(), bk[:m].copy(), bd[:m].copy(), bi[:m].copy(), trend_arr


# ------------------------------------------------------------------------------------------------ volume profile


@njit(**_NB)
def _profile(h, l, v, i0, i1, nb):  # noqa: E741
    lo = np.inf
    hi = -np.inf
    for i in range(i0, i1):
        if l[i] < lo:
            lo = l[i]
        if h[i] > hi:
            hi = h[i]
    hist = np.zeros(nb)
    if not (hi > lo):
        return hist, lo, 0.0
    w = (hi - lo) / nb
    for i in range(i0, i1):
        vol = v[i]
        if not (vol > 0):
            continue
        a = l[i]
        b = h[i]
        ka = int((a - lo) / w)
        kb = int((b - lo) / w)
        if ka >= nb:
            ka = nb - 1
        if kb >= nb:
            kb = nb - 1
        if ka < 0:
            ka = 0
        if b - a <= 1e-12 or ka == kb:
            hist[ka] += vol
            continue
        span = b - a
        for k in range(ka, kb + 1):
            x0 = lo + k * w
            x1 = x0 + w
            ov = min(b, x1) - max(a, x0)
            if ov > 0:
                hist[k] += vol * ov / span
    return hist, lo, w


@njit(**_NB)
def volume_profile_levels(h, l, v, window, step, nb, max_hvn):  # noqa: E741
    """Rolling volume-at-price levels. Refresh points r = n, n-step, ... (>= window); profile over bars [r-window, r).

    Returns arrays (code, price, start, end, strength, anchor): code 0=POC, 1=VAH, 2=VAL, 3=HVN; level valid on bars
    [r, r+step). strength = node volume / POC volume. anchor = bar with the largest volume inside the node's bin.
    """
    n = h.shape[0]
    cap = (n // step + 2) * (3 + max_hvn)
    code = np.empty(cap, np.int64)
    px = np.empty(cap)
    st = np.empty(cap, np.int64)
    en = np.empty(cap, np.int64)
    sg = np.empty(cap)
    an = np.empty(cap, np.int64)
    m = 0
    r = n
    while r >= window:
        hist, lo, w = _profile(h, l, v, r - window, r, nb)
        tot = hist.sum()
        if w > 0 and tot > 0:
            sm = np.empty(nb)
            for k in range(nb):
                a = hist[k - 1] if k > 0 else hist[k]
                b = hist[k + 1] if k < nb - 1 else hist[k]
                sm[k] = 0.25 * a + 0.5 * hist[k] + 0.25 * b
            poc = int(np.argmax(sm))
            pv = sm[poc]
            # value area 70% around POC (on raw hist)
            lo_k = poc
            hi_k = poc
            acc = hist[poc]
            while acc < 0.7 * tot and (lo_k > 0 or hi_k < nb - 1):
                up = hist[hi_k + 1] if hi_k < nb - 1 else -1.0
                dn = hist[lo_k - 1] if lo_k > 0 else -1.0
                if up >= dn:
                    hi_k += 1
                    acc += up
                else:
                    lo_k -= 1
                    acc += dn
            cand_k = np.empty(nb, np.int64)
            cand_v = np.empty(nb)
            nc = 0
            for k in range(1, nb - 1):
                if sm[k] >= sm[k - 1] and sm[k] > sm[k + 1] and abs(k - poc) > 2 and sm[k] >= 0.5 * pv:
                    cand_k[nc] = k
                    cand_v[nc] = sm[k]
                    nc += 1
            order = np.argsort(-cand_v[:nc])
            # emit POC, VAH, VAL
            for j in range(3):
                if j == 0:
                    k = poc
                    price = lo + (poc + 0.5) * w
                    s = 1.0
                elif j == 1:
                    k = hi_k
                    price = lo + (hi_k + 1) * w
                    s = sm[hi_k] / pv
                else:
                    k = lo_k
                    price = lo + lo_k * w
                    s = sm[lo_k] / pv
                code[m] = j; px[m] = price; st[m] = r; en[m] = r + step; sg[m] = s  # noqa: E702
                an[m] = _anchor_bar(h, l, v, r - window, r, lo + k * w, lo + (k + 1) * w)
                m += 1
            for jj in range(min(max_hvn, nc)):
                k = cand_k[order[jj]]
                code[m] = 3; px[m] = lo + (k + 0.5) * w; st[m] = r; en[m] = r + step  # noqa: E702
                sg[m] = sm[k] / pv
                an[m] = _anchor_bar(h, l, v, r - window, r, lo + k * w, lo + (k + 1) * w)
                m += 1
        r -= step
    return code[:m].copy(), px[:m].copy(), st[:m].copy(), en[:m].copy(), sg[:m].copy(), an[:m].copy()


@njit(**_NB)
def _anchor_bar(h, l, v, i0, i1, x0, x1):  # noqa: E741
    best = -1.0
    bi = i0
    for i in range(i0, i1):
        if l[i] <= x1 and h[i] >= x0 and v[i] > best:
            best = v[i]
            bi = i
    return bi


# ------------------------------------------------------------------------------------------------ event study


@njit(**_NB)
def _outcome(o, h, l, c, t, p, side, a, horizon, hit_x, brk_b, band, rr_stop, rr_target):  # noqa: E741
    """Outcome of a touch at bar t of level p from `side` (+1 support test, -1 resistance test), atr a = atr[t-1].

    Returns (hit, broke, resolve_bar, mfe, mae, follow_through, r_multiple). mfe/mae/ft in price units (>= 0).
    Conservative bar ordering: on the touch bar only the close counts as favourable; an adverse close beyond
    brk_b*a on a bar beats a favourable intrabar excursion on that same bar.
    """
    n = c.shape[0]
    fav_t = hit_x * a
    brk_t = brk_b * a
    hit = False
    broke = False
    res = -1
    mfe = max(0.0, side * (c[t] - p))
    xadv = l[t] if side == 1 else h[t]
    mae = max(0.0, side * (p - xadv))
    last = min(t + horizon, n)  # bars t .. t+horizon-1
    if side * (p - c[t]) > brk_t:
        broke = True
        res = t
    elif mfe >= fav_t:
        hit = True
        res = t
    else:
        for j in range(t + 1, last):
            xadv = l[j] if side == 1 else h[j]
            ad = side * (p - xadv)
            if ad > mae:
                mae = ad
            if side * (p - c[j]) > brk_t:
                broke = True
                res = j
                break
            xfav = h[j] if side == 1 else l[j]
            f = side * (xfav - p)
            if f > mfe:
                mfe = f
            if mfe >= fav_t:
                hit = True
                res = j
                break
    if res < 0:
        res = last - 1
    # follow-through after a break: max move in the break direction over the next `horizon` bars, from the break close
    ft = np.nan
    if broke:
        cb = c[res]
        ft = 0.0
        for q in range(res + 1, min(res + 1 + horizon, n)):
            x = l[q] if side == 1 else h[q]
            f = -side * (x - cb)
            if f > ft:
                ft = f
    # trade: limit at the band edge, stop rr_stop*a beyond the level, target rr_target R, time exit at horizon
    entry = p + side * band
    stop = p - side * rr_stop * a
    risk = abs(entry - stop)
    r_mult = np.nan
    if risk > 0:
        tgt = entry + side * rr_target * risk
        xadv = l[t] if side == 1 else h[t]
        if side * (stop - xadv) >= 0:
            r_mult = -1.0
        else:
            done = False
            for j in range(t + 1, last):
                xadv = l[j] if side == 1 else h[j]
                if side * (stop - xadv) >= 0:
                    r_mult = -1.0 if side * (o[j] - stop) > 0 else side * (o[j] - entry) / risk
                    done = True
                    break
                xfav = h[j] if side == 1 else l[j]
                if side * (xfav - tgt) >= 0:
                    r_mult = rr_target
                    done = True
                    break
            if not done:
                r_mult = side * (c[last - 1] - entry) / risk
    return hit, broke, res, mfe, mae, ft, r_mult


@njit(**_NB)
def scan_events(o, h, l, c, atr, key, px, start, end, maxev, series_px, series_key,  # noqa: E741
                t_min, horizon, hit_x, brk_b, band_k, rr_stop, rr_target, cap):
    """Touch events for static level instances (key/px/start/end/maxev) and per-bar series (series_px[s, t]).

    Event at bar t (t >= t_min, t >= start): open more than band_k*ATR beyond the level and the bar's range comes
    within band_k*ATR of it. After an event the instance is idle until the outcome resolved. Events whose full
    outcome window (2*horizon bars: reaction + follow-through) is not inside the data are censored.
    Returns m (number of events, or -1 if `cap` was too small) and the event columns.
    """
    n = c.shape[0]
    e_key = np.empty(cap, np.int64)
    e_t = np.empty(cap, np.int64)
    e_side = np.empty(cap, np.int64)
    e_px = np.empty(cap)
    e_atr = np.empty(cap)
    e_hit = np.empty(cap, np.bool_)
    e_brk = np.empty(cap, np.bool_)
    e_mfe = np.empty(cap)
    e_mae = np.empty(cap)
    e_ft = np.empty(cap)
    e_r = np.empty(cap)
    e_res = np.empty(cap, np.int64)
    e_hold = np.empty(cap, np.bool_)
    m = 0
    t_last = n - 2 * horizon  # last bar whose full outcome window is observed
    n_static = px.shape[0]
    n_series = series_px.shape[0]
    for i in range(n_static + n_series):
        is_series = i >= n_static
        s = 0
        p = 0.0
        if is_series:
            s = i - n_static
            k = series_key[s]
            t = max(1, t_min)
            e0 = t_last
            mx = 0
        else:
            k = key[i]
            t = max(max(1, t_min), start[i])
            e0 = min(end[i], t_last)
            mx = maxev[i]
            p = px[i]
        nev = 0
        while t < e0:
            if is_series:
                p = series_px[s, t]
                if not (p > 0):
                    t += 1
                    continue
            a = atr[t - 1]
            if not (a > 0):
                t += 1
                continue
            band = band_k * a
            side = 0
            if o[t] > p + band and l[t] <= p + band:
                side = 1
            elif o[t] < p - band and h[t] >= p - band:
                side = -1
            if side == 0:
                t += 1
                continue
            hit, broke, res, mfe, mae, ft, r_mult = _outcome(o, h, l, c, t, p, side, a, horizon, hit_x, brk_b, band,
                                                            rr_stop, rr_target)
            if m >= cap:
                return -1, e_key, e_t, e_side, e_px, e_atr, e_hit, e_brk, e_mfe, e_mae, e_ft, e_r, e_res, e_hold
            e_key[m] = k; e_t[m] = t; e_side[m] = side; e_px[m] = p; e_atr[m] = a  # noqa: E702
            e_hit[m] = hit; e_brk[m] = broke; e_mfe[m] = mfe; e_mae[m] = mae; e_ft[m] = ft; e_r[m] = r_mult  # noqa: E702
            e_res[m] = t + 2 * horizon
            e_hold[m] = side * (c[t] - p) > 0  # touch bar closed back on the approach side (level held)
            m += 1
            nev += 1
            if mx > 0 and nev >= mx:
                break
            t = max(res, t) + 1
    return m, e_key, e_t, e_side, e_px, e_atr, e_hit, e_brk, e_mfe, e_mae, e_ft, e_r, e_res, e_hold


@njit(**_NB)
def touch_stats(h, l, c, atr, px, start, role, t_end, band_k, brk_b):  # noqa: E741
    """For each static instance: number of distinct touches (range within band_k*ATR, separated by a non-touching
    bar) on bars [start, t_end), last touch bar (-1 if none) and whether the level was closed through by more than
    brk_b*ATR against its natural role (role +1 support / -1 resistance; 0 never 'broken')."""
    m = px.shape[0]
    touches = np.zeros(m, np.int64)
    last = np.full(m, -1, np.int64)
    broken = np.zeros(m, np.bool_)
    for i in range(m):
        p = px[i]
        prev = False
        for t in range(max(start[i], 1), t_end):
            a = atr[t - 1]
            if not (a > 0):
                continue
            band = band_k * a
            tch = l[t] <= p + band and h[t] >= p - band
            if tch and not prev:
                touches[i] += 1
            if tch:
                last[i] = t
            prev = tch
            if role[i] == 1 and c[t] < p - brk_b * a:
                broken[i] = True
            elif role[i] == -1 and c[t] > p + brk_b * a:
                broken[i] = True
    return touches, last, broken


@njit(**_NB)
def series_touch_stats(h, l, atr, sp, t0, t_end, band_k):  # noqa: E741
    """Touch count and last touch bar of each per-bar series sp[s, t] over bars [t0, t_end)."""
    ns = sp.shape[0]
    touches = np.zeros(ns, np.int64)
    last = np.full(ns, -1, np.int64)
    for s in range(ns):
        prev = False
        for t in range(max(t0, 1), t_end):
            p = sp[s, t]
            a = atr[t - 1]
            if not (p > 0) or not (a > 0):
                prev = False
                continue
            band = band_k * a
            tch = l[t] <= p + band and h[t] >= p - band
            if tch and not prev:
                touches[s] += 1
            if tch:
                last[s] = t
            prev = tch
    return touches, last


# ------------------------------------------------------------------------------------------------ KDE


@njit(**_NB)
def weighted_kde(x, w, grid, bw):
    out = np.zeros(grid.shape[0])
    inv = 1.0 / (bw * math.sqrt(2.0 * math.pi))
    for j in range(grid.shape[0]):
        g = grid[j]
        s = 0.0
        for i in range(x.shape[0]):
            z = (g - x[i]) / bw
            if -6.0 < z < 6.0:
                s += w[i] * math.exp(-0.5 * z * z)
        out[j] = s * inv
    return out
