"""#5 Volume-confirmed breakouts & accumulation (pure numpy on wide (T, n) arrays; no `app` imports).

Event (point-in-time, known at the close of t):
  up breakout of lookback N   C[t] > max(High[t-N .. t-1]) and C[t-1] <= max(High[t-1-N .. t-2]) (a fresh close beyond
                              the prior N-session high), no up event of the same N in the previous DEDUP sessions;
                              level L = max(High[t-N .. t-1]). Down breakouts mirror it on the prior N-session low.
Follow-through label (first passage over the next H sessions, from the breakout close):
  target = C[t] * exp(dir * K * sigma * sqrt(H)), sigma = EWMA (lambda 0.94) daily log-return vol at t - 1;
  success = the high (up) / low (down) reaches the target before any CLOSE back inside the level (C < L for up);
  fail    = a close back inside the level first (a bar with both = fail: intrabar order unknown);
  neither = none within H sessions. P(follow-through) = P(success).
Geometry: b = dir * log(C / L) / (sigma sqrt(H)) (how far beyond the level the breakout closed, in sigma_H units) - the
  main confounder of any volume effect (high-volume breakouts close further beyond the level).
Random control (base rate): for each event, a random session u of the same security within +-CTRL_SPAN sessions that
  is not itself an event of the same (N, dir); pseudo-level L' = C[u] exp(-dir b sigma_u sqrt(H)) (the SAME b) and the
  same target rule -> the geometry-matched base rate of "staying beyond a level" with no breakout.
Features (all at the close of t, dir-signed where marked *):
  relvol20 / relvol50   V[t] / mean(V[t-20 .. t-1]) / (50)                     Lee & Swaminathan (2000);
  clv*                  close location in the day's range (2C - H - L) / (H - L)   Gervais, Kaniel & Mingelgrin (2001)
  cmf20*                Chaikin money flow: sum(clv V) / sum(V) over 20 sessions
  obv_slope20*          (OBV[t] - OBV[t-20]) / sum(V) over 20 sessions; obv_confirm = OBV at its own N-session extreme
  d_illiq               log(Amihud |r| / dollar volume, 20 sessions / the previous 60) (negative = more liquid)
  gap_z*, gap_beyond    opening gap in sigma units; the open already beyond the level
  ret_z*, range_z       the breakout day's return / range in sigma units; trend200* log(C / SMA200);
  prior20_z*            the 20-session return before the breakout in sigma_20 units; vol_ratio sigma / median 252
Confirmation (pre-registered rule): relvol50 >= 1.5 and clv* >= 0.5. Confirmation score 0..100 = the mean of four
  bounded components (relvol50 on a log scale 1 -> 0, 3 -> 1; clv*; cmf20*; obv_slope20*), fixed ex ante.
"""

from __future__ import annotations

import warnings

import numpy as np

from app.techv3 import factors as F

LOOKBACKS = (20, 55)
HORIZONS = (10, 20)
K = 1.0
DEDUP = 10
CTRL_SPAN = 250
LAMBDA = 0.94
CONFIRM_RELVOL = 1.5
CONFIRM_CLV = 0.5
OUTCOMES = ("success", "fail", "neither", "open")
FEATURES = ("b_dist", "relvol20", "relvol50", "clv", "cmf20", "obv_slope20", "obv_confirm", "d_illiq", "gap_z",
            "gap_beyond", "ret_z", "range_z", "trend200", "prior20_z", "vol_ratio")
VOLUME_FEATURES = ("relvol20", "relvol50", "clv", "cmf20", "obv_slope20", "obv_confirm", "d_illiq", "gap_z", "gap_beyond")


def ewma_sigma(r: np.ndarray, lam: float = LAMBDA, warm: int = 20) -> np.ndarray:
    """EWMA daily vol; s[t] uses returns <= t (seeded by the first `warm` returns' variance)."""
    T, n = r.shape
    out = np.full((T, n), np.nan)
    v = np.full(n, np.nan)
    seen = np.zeros(n, dtype=np.int64)
    acc = np.zeros(n)
    for t in range(T):
        x = r[t]
        ok = np.isfinite(x)
        seen = seen + ok
        acc = acc + np.where(ok, x * x, 0.0)
        start = ok & ~np.isfinite(v) & (seen >= warm)
        v = np.where(start, acc / np.maximum(seen, 1), v)
        upd = ok & np.isfinite(v) & ~start
        v = np.where(upd, lam * v + (1 - lam) * x * x, v)
        out[t] = np.sqrt(v)
    return out


def feature_panel(O: np.ndarray, H: np.ndarray, L: np.ndarray, C: np.ndarray, V: np.ndarray, DV: np.ndarray,
                  r: np.ndarray, sig_prev: np.ndarray) -> dict[str, np.ndarray]:
    """Direction-free feature arrays (T, n); the dir sign is applied at gather time."""
    out: dict[str, np.ndarray] = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        Vz = np.where(np.isfinite(V) & (V >= 0), V, np.nan)
        m20 = F.shift(F.roll_mean(Vz, 20, 15), 1)
        m50 = F.shift(F.roll_mean(Vz, 50, 35), 1)
        out["relvol20"] = np.where(m20 > 0, Vz / m20, np.nan)
        out["relvol50"] = np.where(m50 > 0, Vz / m50, np.nan)
        rng_ = H - L
        clv = np.where(rng_ > 0, (2 * C - H - L) / rng_, 0.0)
        clv = np.where(np.isfinite(C), clv, np.nan)
        out["clv"] = clv
        sv, _ = F.roll_sum(Vz, 20)
        smf, _ = F.roll_sum(clv * Vz, 20)
        out["cmf20"] = np.where(sv > 0, smf / sv, np.nan)
        sgn = np.sign(np.nan_to_num(r, nan=0.0))
        obv = np.cumsum(sgn * np.nan_to_num(Vz, nan=0.0), axis=0)
        out["obv"] = obv
        out["obv_slope20"] = np.where(sv > 0, (obv - F.shift(obv, 20)) / sv, np.nan)
        am = np.where((DV > 0) & np.isfinite(r), np.abs(r) / DV, np.nan)
        i20 = F.roll_mean(am, 20, 15)
        i60 = F.shift(F.roll_mean(am, 60, 40), 20)
        out["d_illiq"] = np.where((i20 > 0) & (i60 > 0), np.log(i20 / i60), np.nan)
        cprev = F.shift(F.ffill(C), 1)
        out["gap"] = np.where((O > 0) & (cprev > 0), np.log(O / cprev) / sig_prev, np.nan)
        out["ret"] = r / sig_prev
        out["range_z"] = np.where((H > 0) & (L > 0), np.log(H / L) / sig_prev, np.nan)
        lcf = F.ffill(np.log(np.where(C > 0, C, np.nan)))
        sma = F.roll_mean(np.exp(lcf), 200, 150)
        out["trend200"] = np.where(sma > 0, lcf - np.log(sma), np.nan)
        out["prior20"] = (F.shift(lcf, 1) - F.shift(lcf, 21)) / (sig_prev * np.sqrt(20.0))
        med = F.shift(_roll_median_sigma(sig_prev), 0)
        out["vol_ratio"] = np.where(med > 0, sig_prev / med, np.nan)
    return out


def _roll_median_sigma(s: np.ndarray, w: int = 252, step: int = 5) -> np.ndarray:
    """Approximate trailing 252-session median of sigma (recomputed every `step` rows, carried in between)."""
    T, n = s.shape
    out = np.full((T, n), np.nan)
    last = np.full(n, np.nan)
    for t in range(T):
        if t % step == 0 and t >= 60:
            win = s[max(0, t - w + 1): t + 1]
            cnt = np.isfinite(win).sum(axis=0)
            with warnings.catch_warnings(), np.errstate(all="ignore"):
                warnings.simplefilter("ignore", RuntimeWarning)
                m = np.nanmedian(win, axis=0) if win.size else np.full(n, np.nan)
            last = np.where(cnt >= 60, m, np.nan)
        out[t] = last
    return out


def detect(H: np.ndarray, L: np.ndarray, C: np.ndarray, N: int, dedup: int = DEDUP
           ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fresh breakout masks (T, n) for up and down and their levels: returns (up_mask, dn_mask, ph, pl)."""
    hh = np.where(np.isfinite(H), H, np.nan)
    ll = np.where(np.isfinite(L), L, np.nan)
    ph = F.shift(F.roll_max(hh, N, int(0.8 * N)), 1)
    pl = F.shift(F.roll_min(ll, N, int(0.8 * N)), 1)
    with np.errstate(invalid="ignore"):
        above = np.isfinite(C) & np.isfinite(ph) & (C > ph)
        below = np.isfinite(C) & np.isfinite(pl) & (C < pl)
    # fresh: the previous print was not already beyond its own prior extreme
    up = above & ~_prev_true(above)
    dn = below & ~_prev_true(below)
    return _dedupe(up, dedup), _dedupe(dn, dedup), ph, pl


def _prev_true(M: np.ndarray) -> np.ndarray:
    out = np.zeros_like(M)
    out[1:] = M[:-1]
    return out


def _dedupe(M: np.ndarray, k: int) -> np.ndarray:
    out = np.zeros_like(M)
    for j in range(M.shape[1]):
        idx = np.nonzero(M[:, j])[0]
        last = -10 ** 9
        for t in idx:
            if t - last > k:
                out[t, j] = True
                last = t
    return out


def first_passage(Hf: np.ndarray, Lf: np.ndarray, Cf: np.ndarray, d: np.ndarray, level: np.ndarray, target: np.ndarray
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Outcome per event from the next-H-session windows (n_ev, H) of high / low / close.
    Returns (outcome code 0 success / 1 fail / 2 neither / 3 open (window incomplete), tau sessions (1-based, 0 if
    none), complete flag)."""
    n_ev, Hh = Cf.shape
    dd = d[:, None]
    hit_t = np.where(dd > 0, Hf >= target[:, None], Lf <= target[:, None])
    back = np.where(dd > 0, Cf < level[:, None], Cf > level[:, None])
    hit_t &= np.isfinite(Cf)
    back &= np.isfinite(Cf)
    BIG = Hh + 10
    ti = np.where(hit_t.any(1), hit_t.argmax(1), BIG)
    bi = np.where(back.any(1), back.argmax(1), BIG)
    # windows that run past the data end: incomplete unless already resolved
    last_ok = np.where(np.isfinite(Cf), np.arange(Hh)[None, :], -1).max(1)
    complete_win = np.isfinite(Cf[:, -1])
    code = np.full(n_ev, 2, dtype=np.int8)
    tau = np.zeros(n_ev, dtype=np.int16)
    succ = ti < bi
    fail = (bi <= ti) & (bi < BIG)
    code[succ] = 0
    code[fail] = 1
    tau[succ] = (ti[succ] + 1).astype(np.int16)
    tau[fail] = (bi[fail] + 1).astype(np.int16)
    unresolved = ~(succ | fail)
    open_ = unresolved & ~complete_win
    code[open_] = 3
    complete = (succ | fail | complete_win)
    _ = last_ok
    return code, tau, complete


def gather_windows(X: np.ndarray, t: np.ndarray, j: np.ndarray, H: int) -> np.ndarray:
    """X[t+1 .. t+H, j] per event -> (n_ev, H) (NaN beyond the array end)."""
    T = X.shape[0]
    idx = t[:, None] + np.arange(1, H + 1)[None, :]
    ok = idx < T
    out = np.full(idx.shape, np.nan)
    out[ok] = X[np.where(ok, idx, 0), np.broadcast_to(j[:, None], idx.shape)][ok]
    return out


def confirm_score(relvol50: np.ndarray, clv: np.ndarray, cmf: np.ndarray, obv_slope: np.ndarray) -> np.ndarray:
    """Fixed ex-ante 0..100 score (see module notes); NaN components are skipped."""
    with np.errstate(invalid="ignore", divide="ignore"):
        s1 = np.clip(np.log(np.maximum(relvol50, 1e-9)) / np.log(3.0), 0.0, 1.0)
        s2 = np.clip((clv + 1.0) / 2.0, 0.0, 1.0)
        s3 = np.clip(cmf + 0.5, 0.0, 1.0)
        s4 = np.clip((obv_slope + 1.0) / 2.0, 0.0, 1.0)
        S = np.vstack([s1, s2, s3, s4])
        ok = np.isfinite(S)
        out = np.where(ok.sum(0) > 0, np.nansum(np.where(ok, S, 0.0), 0) / np.maximum(ok.sum(0), 1), np.nan)
    return 100.0 * out


def is_confirmed(relvol50: np.ndarray, clv: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return (relvol50 >= CONFIRM_RELVOL) & (clv >= CONFIRM_CLV)


def events_for_chunk(O, H, L, C, V, DV, r, dates, ids, region_mkt_lvl=None, horizons=HORIZONS, lookbacks=LOOKBACKS,
                     k=K, seed=0, controls=True, t_min=0):
    """Every breakout event of a chunk with features and labels (+ random controls). Returns (events, controls) as
    dicts of 1-d arrays (columns). t_min: only events at rows >= t_min are returned (labels still use later rows)."""
    T, n = C.shape
    sig = ewma_sigma(r)
    sig_prev = F.shift(sig, 1)
    fp = feature_panel(O, H, L, C, V, DV, r, sig_prev)
    rng = np.random.default_rng(seed)
    ev_cols: dict[str, list[np.ndarray]] = {}
    ct_cols: dict[str, list[np.ndarray]] = {}

    def put(dst: dict[str, list[np.ndarray]], **kv: np.ndarray) -> None:
        for kk, vv in kv.items():
            dst.setdefault(kk, []).append(vv)

    obv = fp["obv"]
    for N in lookbacks:
        up, dn, ph, pl = detect(H, L, C, N)
        obv_hi = F.shift(F.roll_max(obv, N, int(0.8 * N)), 1)
        obv_lo = F.shift(F.roll_min(obv, N, int(0.8 * N)), 1)
        for dirn, M, lev in ((1, up, ph), (-1, dn, pl)):
            M = M & np.isfinite(sig_prev) & (sig_prev > 0)
            if t_min:
                M[:t_min] = False
            t_i, j_i = np.nonzero(M)
            if not len(t_i):
                continue
            d = np.full(len(t_i), dirn, dtype=np.int8)
            c0 = C[t_i, j_i]
            lv = lev[t_i, j_i]
            s0 = sig_prev[t_i, j_i]
            g = {kk: fp[kk][t_i, j_i] for kk in ("relvol20", "relvol50", "clv", "cmf20", "obv_slope20", "d_illiq", "gap",
                                                  "ret", "range_z", "trend200", "prior20", "vol_ratio")}
            ob = obv[t_i, j_i]
            obc = (ob >= obv_hi[t_i, j_i]) if dirn > 0 else (ob <= obv_lo[t_i, j_i])
            o0 = O[t_i, j_i]
            gap_beyond = (o0 > lv) if dirn > 0 else (o0 < lv)
            base = dict(t=t_i.astype(np.int32), j=j_i.astype(np.int32), N=np.full(len(t_i), N, np.int16), direction=d,
                        close=c0, level=lv, sigma_d=s0,
                        relvol20=g["relvol20"], relvol50=g["relvol50"], clv=dirn * g["clv"], cmf20=dirn * g["cmf20"],
                        obv_slope20=dirn * g["obv_slope20"], obv_confirm=obc.astype(np.int8), d_illiq=g["d_illiq"],
                        gap_z=dirn * g["gap"], gap_beyond=gap_beyond.astype(np.int8), ret_z=dirn * g["ret"],
                        range_z=g["range_z"], trend200=dirn * g["trend200"], prior20_z=dirn * g["prior20"],
                        vol_ratio=g["vol_ratio"])
            for Hh in horizons:
                sH = s0 * np.sqrt(Hh)
                with np.errstate(invalid="ignore", divide="ignore"):
                    base[f"b_{Hh}"] = dirn * np.log(c0 / lv) / sH
                tgt = c0 * np.exp(dirn * k * sH)
                code, tau, comp = first_passage(gather_windows(H, t_i, j_i, Hh), gather_windows(L, t_i, j_i, Hh),
                                                gather_windows(C, t_i, j_i, Hh), d, lv, tgt)
                cl = gather_windows(C, t_i, j_i, Hh)[:, -1]
                with np.errstate(invalid="ignore", divide="ignore"):
                    base[f"ret_{Hh}"] = dirn * np.log(cl / c0)
                base[f"target_{Hh}"] = tgt
                base[f"out_{Hh}"] = code
                base[f"tau_{Hh}"] = tau
                base[f"complete_{Hh}"] = comp
            put(ev_cols, **base)
            if controls:
                ctl = _controls(t_i, j_i, M, C, H, L, sig_prev, dirn, base, horizons, k, rng)
                if ctl:
                    put(ct_cols, **ctl)
    ev = {kk: np.concatenate(v) for kk, v in ev_cols.items()} if ev_cols else {}
    ct = {kk: np.concatenate(v) for kk, v in ct_cols.items()} if ct_cols else {}
    for dst in (ev, ct):
        if dst:
            dst["date"] = dates[dst["t"]]
            dst["security_id"] = np.asarray(ids, dtype=object)[dst["j"]]
    if ev:
        ev["confirmed"] = is_confirmed(ev["relvol50"], ev["clv"])
        ev["confirm_score"] = confirm_score(ev["relvol50"], ev["clv"], ev["cmf20"], ev["obv_slope20"])
    return ev, ct


def _controls(t_i, j_i, M, C, H, L, sig_prev, dirn, base, horizons, k, rng):
    T = C.shape[0]
    off = rng.integers(-CTRL_SPAN, CTRL_SPAN + 1, len(t_i))
    off = np.where(np.abs(off) < 5, off + np.sign(off + 0.5).astype(int) * 5, off)
    u = np.clip(t_i + off, 0, T - 1)
    ok = ~M[u, j_i] & np.isfinite(C[u, j_i]) & np.isfinite(sig_prev[u, j_i]) & (sig_prev[u, j_i] > 0)
    u, j = u[ok], j_i[ok]
    if not len(u):
        return None
    c0 = C[u, j]
    s0 = sig_prev[u, j]
    d = np.full(len(u), dirn, dtype=np.int8)
    out = {"t": u.astype(np.int32), "j": j.astype(np.int32), "N": base["N"][ok], "direction": d,
           "event_t": base["t"][ok]}
    for Hh in horizons:
        sH = s0 * np.sqrt(Hh)
        b = base[f"b_{Hh}"][ok]
        lv = c0 * np.exp(-dirn * b * sH)
        tgt = c0 * np.exp(dirn * k * sH)
        code, tau, comp = first_passage(gather_windows(H, u, j, Hh), gather_windows(L, u, j, Hh), gather_windows(C, u, j, Hh),
                                        d, lv, tgt)
        out[f"b_{Hh}"] = b
        out[f"out_{Hh}"] = code
        out[f"complete_{Hh}"] = comp
    return out
