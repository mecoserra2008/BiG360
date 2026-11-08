"""Job `conformal_envelopes` (nightly, after prices_finalize): application A for the full universe -> tables
conformal_envelopes (last `emit_days` origins rewritten: realised fields fill in), conformal_touch_curves (latest),
conformal_coverage (rolling audit rows, application envelope / nextbar). MODEL-BASED.

Fan (Technicals v2 T1, docs/TECHNICALS_V2_PLAN.md): besides the 1/5/10/20 marginal radii (qm_*), every emitted row
carries the marginal band for EVERY horizon h = 1..20 sessions: fan_v[h-1] = V(h) from the filter's variance term
structure (h s2 + the earnings jump when an event falls in (t, t+h]) and fan_qm_{80,90,95}[h-1] = the radius calibrated
per h in the same pooled rolling split-conformal scheme (pool x normal / event, window 500, lag h). Each pool holds
>= 30 securities x 500 origins (>= 15,000 scores, min_n 200), so the per-h calibration is never thin and no
interpolation across horizons is needed (walk-forward check: scripts/conformal/fan_check.py, docs/CONFORMAL_VALIDATION.md
A.5). The intermediate horizons are calibrated for the emitted origins only (plain radius; the ACI replay only in aci
mode); FAN_AUDIT adds their nightly rolling coverage rows.

Stateless and idempotent: every run recomputes filters, scores, pooled calibration and the pooled ACI path from
prices over the last ~4.5 years, one region (prices_daily partition) at a time and securities in chunks of 800
(peak RAM ~1.5 GB on the largest region). ACI is replayed from `replay` sessions back, so the stored ACI state
is a deterministic function of the data.

    python -m app.conformal.envelope_job [--as-of 2026-10-20] [--regions US,EU] [--emit-days 21] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.conformal import data as D
from app.conformal import envelope as V
from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.conformal.envelope_job")

HISTORY_DAYS = 1700          # calendar days of prices loaded per region
REPLAY = 600                 # sessions of pooled calibration / ACI replay before the as-of date
WINDOW = 500                 # rolling calibration window (origins)
AUDIT_N = 250                # realised origins audited
EMIT_DAYS = 21               # origin dates (re)written per run
RETENTION = 30               # origin dates kept in conformal_envelopes
CHUNK = 800
MIN_PRINTS = 250
# production radii (P4 decision, docs/CONFORMAL_VALIDATION.md): joint + marginal multi-session bands use the plain
# rolling radius (ACI with a 20-session feedback lag gained little and produced unbounded sets 1-4% of the time);
# next-bar bands use ACI (1-session feedback: on target in every regime, never unbounded in the walk-forward)
RADIUS_MODE = "plain"
NB_RADIUS_MODE = "aci"
# fan (T1): the intermediate horizons are calibrated for the emitted origins only; their coverage is validated in the
# walk-forward (scripts/conformal/fan_check.py) - FAN_AUDIT = True also audits them nightly (rows m2..m19, ~+30% time)
FAN_AUDIT = False
LEVELS = tuple(int(round(100 * (1 - a))) for a in V.ALPHAS)        # 80, 90, 95


def _f32(x: np.ndarray) -> np.ndarray:
    return np.asarray(x, dtype=np.float32)


def _extend_events(dates: np.ndarray, E: np.ndarray, ids: list[str], upcoming: pl.DataFrame, extra: int = 30
                   ) -> tuple[np.ndarray, np.ndarray]:
    """Append `extra` future weekdays to the calendar and flag upcoming (expected) earnings days on them."""
    last = dates[-1]
    fut = np.busday_offset(last, np.arange(1, extra + 1), roll="forward")
    d_ext = np.concatenate([dates, fut.astype("datetime64[D]")])
    E_ext = np.vstack([E, np.zeros((extra, E.shape[1]), dtype=bool)])
    if not upcoming.is_empty():
        F = D.flag_matrix(d_ext, ids, upcoming)
        E_ext[len(dates):] |= F[len(dates):]
    return d_ext, E_ext


def _month_states(states: pl.DataFrame) -> dict[tuple[str, str], dict[np.datetime64, str]]:
    out: dict[tuple[str, str], dict[np.datetime64, str]] = defaultdict(dict)
    for rt, sc, m, s in states.iter_rows():
        out[(rt, sc)][np.datetime64(m, "M")] = s
    return out


REGIME_SPECS = (("cycle_quadrant", "US"), ("cycle_quadrant", "GLOBAL"), ("hmm_regime", "US"), ("hmm_regime", "GLOBAL"))


class Audit:
    """Accumulates (n, covered) per (application, filter, segment_type, segment, horizon, level)."""

    def __init__(self) -> None:
        self.c: dict[tuple, list] = {}

    def add(self, app: str, flt: str, st: str, seg: str, hz: str, lvl: int, cov: np.ndarray, d0: Any, d1: Any) -> None:
        cov = np.asarray(cov)
        ok = np.isfinite(cov) if cov.dtype.kind == "f" else np.ones(cov.shape, bool)
        n = int(ok.sum())
        if not n:
            return
        k = (app, flt, st, seg, hz, str(lvl))
        cur = self.c.get(k)
        cv = int(np.asarray(cov[ok], dtype=np.float64).sum())
        if cur is None:
            self.c[k] = [n, cv, d0, d1]
        else:
            cur[0] += n
            cur[1] += cv
            cur[2] = min(cur[2], d0)
            cur[3] = max(cur[3], d1)

    def frame(self, as_of: date) -> pl.DataFrame:
        rows = []
        now = datetime.now(UTC)
        for (app, flt, st, seg, hz, lvl), (n, cv, d0, d1) in self.c.items():
            tgt = int(lvl) / 100.0
            cov = cv / n if n else None
            gap = None if cov is None else 100 * (cov - tgt)
            status = "thin" if n < 200 else ("ok" if abs(gap) <= 1.5 else ("under" if gap < 0 else "over"))
            rows.append({"as_of_date": as_of, "application": app, "filter": flt, "segment_type": st, "segment": seg,
                         "horizon": hz, "level": lvl, "target": tgt, "n": n, "covered": cv, "coverage": cov,
                         "gap_pp": gap, "status": status, "window_start": _d(d0), "window_end": _d(d1),
                         "model_based": True, "computed_at": now})
        return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def _d(x: Any) -> date | None:
    if x is None:
        return None
    return np.datetime64(x, "D").astype(object)


def run_region(region: str, as_of: date, meta: pl.DataFrame, settings: Settings, emit_days: int = EMIT_DAYS,
               flt: str = "ewma", audit: Audit | None = None, vix: pl.DataFrame | None = None,
               states: pl.DataFrame | None = None, radius_mode: str = RADIUS_MODE) -> dict[str, Any]:
    t0 = time.perf_counter()
    ids_all = meta["security_id"].to_list()
    pan = D.load_panel(region, as_of - timedelta(days=HISTORY_DAYS), as_of, ids_all, settings, meta=meta)
    if pan is None or pan.T < WINDOW + 120:
        return {"region": region, "rows": None, "note": "not enough history"}
    printed = np.isfinite(pan.C)
    act = (printed.sum(0) >= MIN_PRINTS) & printed[-10:].any(0)
    pan = pan.subset([s for s, k in zip(pan.ids, act, strict=True) if k])
    T, N = pan.T, pan.N
    if N == 0:
        return {"region": region, "rows": None, "note": "no active securities"}
    dates = pan.dates
    start_d = dates[0].astype(object)
    ev = D.earnings_days(pan.ids, start_d, as_of, settings)
    E = D.flag_matrix(dates, pan.ids, ev)
    up = D.upcoming_earnings(pan.ids, as_of, 45, settings)
    _, E_ext = _extend_events(dates, E, pan.ids, up)
    ke_all = V.first_event_offset(E_ext, 20)[:T]
    m = meta.unique("security_id").select("security_id", "sector_id")
    sec = dict(zip(m["security_id"].to_list(), m["sector_id"].to_list(), strict=True))
    sectors = [sec.get(s) or "NA" for s in pan.ids]
    pkeys = V.pool_keys(region, sectors)
    pool, pool_names = V.pool_index(pkeys)

    R_joint = np.full((T, N), np.nan, dtype=np.float32)
    R_nb = np.full((T, N), np.nan, dtype=np.float32)
    R_m = np.full((len(V.HORIZONS), T, N), np.nan, dtype=np.float32)      # marginal per-horizon scores Y_h^2 / V_h
    ev_nb = np.zeros((T, N), dtype=bool)
    ev_nb[:-1] = E_ext[1:T]
    ev_nb[-1] = E_ext[T]
    ev_joint = ke_all > 0
    em0 = T - emit_days
    au0 = max(0, T - 1 - 20 - AUDIT_N)
    tc0 = max(0, T - 21 - WINDOW)
    keep: dict[str, np.ndarray] = {}
    aud: dict[str, np.ndarray] = {}
    tch_up = np.full((T - tc0, N, len(V.HORIZONS)), np.nan, dtype=np.float32)
    tch_dn = np.full_like(tch_up, np.nan)
    # fan (T1): marginal scores of the intermediate horizons, rows [f0, T) only (calibration window of the audit /
    # emitted origins; the 1/5/10/20 anchors reuse R_m and cal_m)
    fan_h = V.FAN_HORIZONS
    fan_mid = [h for h in fan_h if h not in V.HORIZONS]
    f_start = max(120, T - REPLAY) if radius_mode == "aci" else (min(au0, em0) if FAN_AUDIT else em0)
    f0 = max(0, f_start - max(fan_h) - WINDOW - 5)
    f0 -= f0 % 5
    R_f = np.full((len(fan_mid), T - f0, N), np.nan, dtype=np.float32)
    fan_v = np.full((T - em0, N, len(fan_h)), np.nan, dtype=np.float32)

    def put(store: dict[str, np.ndarray], name: str, sl: slice, val: np.ndarray, shape: tuple) -> None:
        if name not in store:
            store[name] = np.full(shape, np.nan, dtype=np.float32)
        store[name][:, sl] = val

    for c0 in range(0, N, CHUNK):
        sl = slice(c0, min(N, c0 + CHUNK))
        a = V.make_arrays(pan.C[:, sl], pan.H[:, sl], pan.L[:, sl], pan.O[:, sl], E[:, sl])
        if flt == "garch":
            gp = V.garch_fit(a.r, a.E, slice(0, max(260, T - REPLAY)))
            s2 = V.garch_var(a.r, a.E, gp)
        else:
            gp = None
            s2 = V.ewma_var(a.r, a.E)
        J = V.jump_var(a.r, a.E, s2)
        ke = ke_all[:, sl]
        Vh = V.horizon_V(s2, J, ke, garch=gp)
        Y = V.forward_targets(a)
        R_joint[:, sl] = V.nested_score(Y, 0.0, Vh)
        R_m[:, :, sl] = np.moveaxis(Y * Y / np.maximum(Vh, 1e-14), -1, 0)
        Vf = V.horizon_V(s2, J, ke, fan_h, garch=gp)                     # (T, n, 20) variance term structure
        fan_v[:, sl] = Vf[em0:]
        if fan_mid:
            Yf = V.forward_targets(a, tuple(fan_mid))
            Vm = Vf[f0:, :, [h - 1 for h in fan_mid]]
            R_f[:, :, sl] = np.moveaxis(Yf[f0:] ** 2 / np.maximum(Vm, 1e-14), -1, 0)
            del Yf, Vm
        del Vf
        mu_nb, S_nb = V.nextbar_filter(a, s2, J)
        Xn = V.nextbar_targets(a)
        R_nb[:, sl] = V.mahal_small(Xn - mu_nb, S_nb)
        n = sl.stop - sl.start
        # rows emitted
        e = slice(em0, T)
        ne = T - em0
        put(keep, "s2", sl, s2[e], (ne, N))
        put(keep, "J", sl, J[e], (ne, N))
        for k in range(4):
            put(keep, f"V{k}", sl, Vh[e, :, k], (ne, N))
            put(keep, f"Y{k}", sl, Y[e, :, k], (ne, N))
        for k in range(3):
            put(keep, f"nbmu{k}", sl, mu_nb[e, :, k], (ne, N))
            put(keep, f"nbX{k}", sl, Xn[e, :, k], (ne, N))
        for nm, (i, j) in (("hh", (0, 0)), ("ll", (1, 1)), ("cc", (2, 2)), ("hl", (0, 1)), ("hc", (0, 2)), ("lc", (1, 2))):
            put(keep, f"nbS{nm}", sl, S_nb[e, :, i, j], (ne, N))
        gap = np.full((T, n), np.nan)
        gap[:-1] = np.where(np.isfinite(a.lo[1:]) & np.isfinite(a.lc[:-1]),
                            (np.abs(a.lo[1:] - a.lc[:-1]) > V.GAP_SIGMAS * np.sqrt(s2[:-1])).astype(float), np.nan)
        put(keep, "gap", sl, gap[e], (ne, N))
        # audit window
        w = slice(au0, T)
        na = T - au0
        for k in range(4):
            put(aud, f"V{k}", sl, Vh[w, :, k], (na, N))
            put(aud, f"Y{k}", sl, Y[w, :, k], (na, N))
        for k in range(3):
            put(aud, f"nbmu{k}", sl, mu_nb[w, :, k], (na, N))
            put(aud, f"nbX{k}", sl, Xn[w, :, k], (na, N))
            put(aud, f"nbS{k}", sl, S_nb[w, :, k, k], (na, N))
        put(aud, "gap", sl, gap[w], (na, N))
        # touch calibration rows
        upx, dnx = V.running_extremes(a)
        sd = np.sqrt(np.maximum(Vh[tc0:], 1e-14))
        tch_up[:, sl] = upx[tc0:] / sd
        tch_dn[:, sl] = dnx[tc0:] / sd
        del a, s2, J, Vh, Y, mu_nb, S_nb, Xn, upx, dnx
    t_start = max(120, T - REPLAY)
    cal_j = V.PoolCal(R_joint, pool, ev_joint, lag=20, window=WINDOW).run(t_start)
    cal_n = V.PoolCal(R_nb, pool, ev_nb, lag=1, window=WINDOW).run(t_start)
    cal_m = [V.PoolCal(R_m[k], pool, (ke_all > 0) & (ke_all <= h), lag=h, window=WINDOW).run(t_start)
             for k, h in enumerate(V.HORIZONS)]
    cal_f = {h: V.PoolCal(R_f[i], pool, ((ke_all > 0) & (ke_all <= h))[f0:], lag=h, window=WINDOW,
                          aci=radius_mode == "aci").run(f_start - f0)
             for i, h in enumerate(fan_mid)}

    def fan_q(h: int, t: int, j: int, src_kind: str) -> np.ndarray:
        """(N,) marginal radius at horizon h for origin t (event pool when an earnings day falls in (t, t+h])."""
        if h in V.HORIZONS:
            cm, tt = cal_m[V.HORIZONS.index(h)], t
        else:
            cm, tt = cal_f[h], t - f0
        src = cm.q_aci if src_kind == "aci" else cm.q
        evh = (ke_all[t] > 0) & (ke_all[t] <= h)
        return np.where(evh, src[tt, pool, 1, j], src[tt, pool, 0, j])

    def lists(arr: np.ndarray) -> list[list[float] | None]:
        """(N, 20) -> per-row lists at float32 precision (None unless all 20 horizons are finite)."""
        a32 = arr.astype(np.float32).astype(np.float64)
        ok = np.isfinite(a32).all(-1)
        return [r.tolist() if k else None for r, k in zip(a32, ok, strict=True)]

    # ------------------------------------------------------------------------------------------ emitted rows
    rows: list[pl.DataFrame] = []
    now = datetime.now(UTC)
    alphas = np.array(V.ALPHAS)
    for i, t in enumerate(range(em0, T)):
        d = dates[t].astype(object)
        evj = ev_joint[t]
        evn = ev_nb[t]
        qj = np.where(evj[:, None], cal_j.q[t, pool, 1], cal_j.q[t, pool, 0])            # (N, A)
        qja = np.where(evj[:, None], cal_j.q_aci[t, pool, 1], cal_j.q_aci[t, pool, 0])
        qn = np.where(evn[:, None], cal_n.q[t, pool, 1], cal_n.q[t, pool, 0])
        qna = np.where(evn[:, None], cal_n.q_aci[t, pool, 1], cal_n.q_aci[t, pool, 0])
        ncal = np.where(evj, cal_j.n_cal[t, pool, 1], cal_j.n_cal[t, pool, 0])
        qb = qja if radius_mode == "aci" else qj
        qnb = qna if NB_RADIUS_MODE == "aci" else qn
        lc = pan.close_raw[t]
        Vt = np.stack([keep[f"V{k}"][i] for k in range(4)], -1)
        out: dict[str, Any] = {
            "as_of_date": [d] * N, "security_id": pan.ids, "region": [region] * N,
            "pool_key": [pool_names[p] for p in pool], "sector_id": sectors, "filter": [flt] * N,
            "radius_mode": [radius_mode] * N, "model_based": [True] * N, "last_close": lc,
            "sigma_d": np.sqrt(keep["s2"][i]), "earn_jump_var": keep["J"][i],
            "earn_days_ahead": np.where(ke_all[t] > 0, ke_all[t], -1), "earn_in_window": evj,
            "q_n_cal": ncal, "q_pool_kind": np.where(evj, "event", "normal"),
            "nb_earn": evn, "computed_at": [now] * N,
        }
        for k, h in enumerate(V.HORIZONS):
            out[f"mu_{h}"] = np.zeros(N)
            out[f"v_{h}"] = Vt[:, k]
            out[f"y_{h}"] = keep[f"Y{k}"][i]
        for j, lvl in enumerate(LEVELS):
            out[f"q_{lvl}"] = qj[:, j]
            out[f"q_aci_{lvl}"] = np.where(np.isfinite(qja[:, j]), qja[:, j], np.nan)
            out[f"aci_alpha_{lvl}"] = cal_j.alpha_t[t, pool, j]
            out[f"nb_q_{lvl}"] = qn[:, j]
            out[f"nb_q_aci_{lvl}"] = np.where(np.isfinite(qna[:, j]), qna[:, j], np.nan)
            for k, h in enumerate(V.HORIZONS):
                lo, hi = V.band_prices(lc, 0.0, Vt[:, k], qb[:, j])
                out[f"b{h}_{lvl}_lo"], out[f"b{h}_{lvl}_hi"] = lo, hi
                evh = (ke_all[t] > 0) & (ke_all[t] <= h)
                cm = cal_m[k]
                src = cm.q_aci if radius_mode == "aci" else cm.q
                qm = np.where(evh, src[t, pool, 1, j], src[t, pool, 0, j])
                out[f"qm_{h}_{lvl}"] = np.where(np.isfinite(qm), qm, np.nan)
            for k, nm in enumerate("hlc"):
                var = keep[f"nbS{nm}{nm}"][i]
                lo, hi = V.band_prices(lc, keep[f"nbmu{k}"][i], var, qnb[:, j])
                out[f"nb_{nm}_{lvl}_lo"], out[f"nb_{nm}_{lvl}_hi"] = lo, hi
        for k, nm in enumerate("hlc"):
            out[f"nb_mu_{nm}"] = keep[f"nbmu{k}"][i]
            out[f"nb_y_{nm}"] = keep[f"nbX{k}"][i]
        for nm in ("hh", "ll", "cc", "hl", "hc", "lc"):
            out[f"nb_s_{nm}"] = keep[f"nbS{nm}"][i]
        out["fan_v"] = lists(fan_v[i].astype(np.float64))
        for j, lvl in enumerate(LEVELS):
            qf = np.stack([fan_q(h, t, j, radius_mode) for h in fan_h], -1)
            out[f"fan_qm_{lvl}"] = lists(np.where(np.isfinite(qf), qf, np.nan))
        sj = R_joint[t].astype(np.float64)
        out["score_joint"] = sj
        out["score_nb"] = R_nb[t].astype(np.float64)
        g = keep["gap"][i]
        out["gap_flag"] = [None if not np.isfinite(x) else bool(x > 0.5) for x in g]
        er = E[t + 1:min(T, t + 21)].any(0) if t + 1 < T else np.zeros(N, dtype=bool)
        out["earn_realized"] = [bool(x) if (t + 20 < T or x) else None for x in er]
        df = pl.DataFrame(out, nan_to_null=True, strict=False)
        df = df.with_columns(pl.when(pl.col("earn_days_ahead") < 0).then(None).otherwise(pl.col("earn_days_ahead"))
                             .cast(pl.Int16).alias("earn_days_ahead"))
        rows.append(df.filter(pl.col("last_close").is_not_null() & pl.col("v_1").is_not_null()))
    emitted = pl.concat(rows, how="diagonal_relaxed") if rows else pl.DataFrame()

    # ------------------------------------------------------------------------------------------ touch curves
    curves = []
    tch_rows = np.arange(tc0, T)
    usable = tch_rows + 20 < T - 1                       # outcome paths complete before the as-of date
    for p, pname in enumerate(pool_names):
        mem = pool == p
        for k, h in enumerate(V.HORIZONS):
            sel_t = usable & (tch_rows >= T - 1 - h - WINDOW)
            for kind, evsel in (("normal", ~ev_joint[tch_rows]), ("event", ev_joint[tch_rows])):
                msk = sel_t[:, None] & mem[None, :] & evsel
                for side, arr in (("up", tch_up), ("down", tch_dn)):
                    vals = arr[:, :, k][msk]
                    vals = vals[np.isfinite(vals)]
                    if vals.size < 200:
                        continue
                    curves.append({"as_of_date": as_of, "pool_key": pname, "region": region, "horizon": h,
                                   "side": side, "kind": kind, "n": int(vals.size),
                                   "probs": V.TOUCH_PROBS.tolist(), "u": np.quantile(vals, V.TOUCH_PROBS).tolist(),
                                   "computed_at": now})

    # ------------------------------------------------------------------------------------------ audit
    if audit is not None:
        _audit_region(audit, region, flt, dates, pan.ids, sectors, pool, au0, T, R_joint, R_nb, ev_joint, ev_nb,
                      cal_j, cal_n, aud, vix, states)
        for k, h in enumerate(V.HORIZONS):              # marginal single-horizon bands (qm_*)
            tt = np.arange(max(au0, T - 1 - h - AUDIT_N), T - h)
            evh = (ke_all[tt] > 0) & (ke_all[tt] <= h)
            Rk = R_m[k][tt].astype(np.float64)
            for mode, src in (("", cal_m[k].q), ("+aci", cal_m[k].q_aci)):
                for j, lvl in enumerate(LEVELS):
                    q = np.where(evh, src[tt][:, pool, 1, j], src[tt][:, pool, 0, j])
                    cov = np.where(np.isfinite(Rk) & np.isfinite(q), (Rk <= q).astype(float), np.nan)
                    audit.add("envelope", flt + mode, "overall", "all", f"m{h}", lvl, cov, dates[tt[0]], dates[tt[-1]])
                    audit.add("envelope", flt + mode, "region", region, f"m{h}", lvl, cov, dates[tt[0]], dates[tt[-1]])
        for i, h in enumerate(fan_mid if FAN_AUDIT else []):   # fan: intermediate horizons (plain; + ACI in aci mode)
            tt = np.arange(max(au0, T - 1 - h - AUDIT_N), T - h)
            evh = (ke_all[tt] > 0) & (ke_all[tt] <= h)
            Rk = R_f[i][tt - f0].astype(np.float64)
            modes = (("", cal_f[h].q),) + ((("+aci", cal_f[h].q_aci),) if radius_mode == "aci" else ())
            for mode, src in modes:
                for j, lvl in enumerate(LEVELS):
                    q = np.where(evh, src[tt - f0][:, pool, 1, j], src[tt - f0][:, pool, 0, j])
                    cov = np.where(np.isfinite(Rk) & np.isfinite(q), (Rk <= q).astype(float), np.nan)
                    audit.add("envelope", flt + mode, "overall", "all", f"m{h}", lvl, cov, dates[tt[0]], dates[tt[-1]])
                    audit.add("envelope", flt + mode, "region", region, f"m{h}", lvl, cov, dates[tt[0]], dates[tt[-1]])
    del R_f, cal_f
    return {"region": region, "rows": emitted, "curves": curves, "N": N, "T": T, "pools": len(pool_names),
            "seconds": round(time.perf_counter() - t0, 1)}


def _audit_region(audit: Audit, region: str, flt: str, dates: np.ndarray, ids: list[str], sectors: list[str],
                  pool: np.ndarray, au0: int, T: int, R_joint: np.ndarray, R_nb: np.ndarray, ev_joint: np.ndarray,
                  ev_nb: np.ndarray, cal_j: V.PoolCal, cal_n: V.PoolCal, aud: dict[str, np.ndarray],
                  vix: pl.DataFrame | None, states: pl.DataFrame | None) -> None:
    ms = _month_states(states) if states is not None and not states.is_empty() else {}
    if vix is not None and not vix.is_empty():
        vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
        vv = vix["vix"].to_numpy()
    sec_arr = np.array(sectors)
    for app, R, ev, cal, lag in (("envelope", R_joint, ev_joint, cal_j, 20), ("nextbar", R_nb, ev_nb, cal_n, 1)):
        t_lo = max(au0, T - 1 - lag - AUDIT_N)
        t_hi = T - 1 - lag                      # last origin whose outcome is realised
        if t_hi <= t_lo:
            continue
        tt = np.arange(t_lo, t_hi + 1)
        Rw = R[tt].astype(np.float64)
        evw = ev[tt]
        d0, d1 = dates[tt[0]], dates[tt[-1]]
        months = dates[tt].astype("datetime64[M]")
        if vix is not None and not vix.is_empty():
            k = np.clip(np.searchsorted(vd, dates[tt], side="right") - 1, 0, len(vd) - 1)
            hv = vv[k] > 25.0
        else:
            hv = np.zeros(len(tt), dtype=bool)
        for mode, src in (("", cal.q), ("+aci", cal.q_aci)):
            for j, lvl in enumerate(LEVELS):
                q = np.where(evw, src[tt][:, pool, 1, j], src[tt][:, pool, 0, j])     # (t, N)
                cov = np.where(np.isfinite(Rw) & np.isfinite(q), (Rw <= q).astype(float), np.nan)
                f = flt + mode
                audit.add(app, f, "overall", "all", "joint", lvl, cov, d0, d1)
                audit.add(app, f, "region", region, "joint", lvl, cov, d0, d1)
                for s in np.unique(sec_arr):
                    audit.add(app, f, "sector", f"{region}:{s}", "joint", lvl, cov[:, sec_arr == s], d0, d1)
                    audit.add(app, f, "sector", str(s), "joint", lvl, cov[:, sec_arr == s], d0, d1)
                audit.add(app, f, "earnings", "event", "joint", lvl, np.where(evw, cov, np.nan), d0, d1)
                audit.add(app, f, "earnings", "normal", "joint", lvl, np.where(~evw, cov, np.nan), d0, d1)
                audit.add(app, f, "vix", "high_vix", "joint", lvl, cov[hv], d0, d1)
                audit.add(app, f, "vix", "normal_vix", "joint", lvl, cov[~hv], d0, d1)
                for (rt, sc) in REGIME_SPECS:
                    mp = ms.get((rt, sc), {})
                    lab = np.array([mp.get(m, "") for m in months])
                    for s in np.unique(lab):
                        if s:
                            audit.add(app, f, "regime", f"{rt}:{sc}={s}", "joint", lvl, cov[lab == s], d0, d1)
                if app == "nextbar":
                    g = aud["gap"][tt - au0]
                    audit.add(app, f, "gap", "gap", "joint", lvl, np.where(g > 0.5, cov, np.nan), d0, d1)
                    audit.add(app, f, "gap", "no_gap", "joint", lvl, np.where(g < 0.5, cov, np.nan), d0, d1)
                # per-coordinate coverage (projection bands)
                if app == "envelope":
                    for k, h in enumerate(V.HORIZONS):
                        Vk = aud[f"V{k}"][tt - au0].astype(np.float64)
                        Yk = aud[f"Y{k}"][tt - au0].astype(np.float64)
                        ck = np.where(np.isfinite(Yk) & np.isfinite(q), (np.abs(Yk) <= np.sqrt(q * Vk)).astype(float), np.nan)
                        audit.add(app, f, "overall", "all", str(h), lvl, ck, d0, d1)
                        audit.add(app, f, "region", region, str(h), lvl, ck, d0, d1)
                else:
                    for k, nm in enumerate("hlc"):
                        Sk = aud[f"nbS{k}"][tt - au0].astype(np.float64)
                        Xk = aud[f"nbX{k}"][tt - au0].astype(np.float64) - aud[f"nbmu{k}"][tt - au0]
                        ck = np.where(np.isfinite(Xk) & np.isfinite(q), (np.abs(Xk) <= np.sqrt(q * Sk)).astype(float), np.nan)
                        audit.add(app, f, "overall", "all", nm, lvl, ck, d0, d1)
                        audit.add(app, f, "region", region, nm, lvl, ck, d0, d1)


def prune_old(settings: Settings, keep_after: date) -> int:
    """Drop conformal_envelopes origin partitions older than keep_after (retention)."""
    from app.core.storage import _rmtree, table_dir, table_lock  # noqa: PLC0415

    root = table_dir("conformal_envelopes", settings)
    n = 0
    with table_lock("conformal_envelopes", settings):
        for p in root.glob("as_of_date=*"):
            try:
                d = date.fromisoformat(p.name.split("=", 1)[1])
            except ValueError:
                continue
            if d < keep_after:
                _rmtree(p)
                n += 1
    return n


def _region_task(args: tuple) -> dict[str, Any]:
    """Process-pool worker: one region end to end (build, write its rows) -> small result for the parent."""
    rg, as_of, meta, emit_days, vix, states, radius_mode, write = args
    from app.core.storage import write_table  # noqa: PLC0415

    s = get_settings()
    audit = Audit()
    res = run_region(rg, as_of, meta, s, emit_days, audit=audit, vix=vix, states=states, radius_mode=radius_mode)
    df = res.get("rows")
    info = {k: v for k, v in res.items() if k not in ("rows", "curves")}
    n, last = 0, None
    if isinstance(df, pl.DataFrame) and df.height:
        info["rows"] = df.height
        last = df["as_of_date"].max()
        # upsert: each region merges into the shared origin-date partitions (overwrite_partition would drop the
        # regions written before it); concurrent regions serialise on the table lock
        n = write_table(df, "conformal_envelopes", mode="upsert", settings=s) if write else df.height
    return {"region": rg, "info": info, "rows": n, "last": last, "curves": res.get("curves") or [], "audit": audit.c}


def _merge_audit(dst: Audit, c: dict[tuple, list]) -> None:
    for k, (n, cv, d0, d1) in c.items():
        cur = dst.c.get(k)
        if cur is None:
            dst.c[k] = [n, cv, d0, d1]
        else:
            cur[0] += n
            cur[1] += cv
            cur[2] = min(cur[2], d0)
            cur[3] = max(cur[3], d1)


def _ckpt_path(s: Settings, as_of: date, emit_days: int, radius_mode: str) -> Any:
    return s.data_dir / "state" / "conformal" / f"envelopes_ckpt_{as_of.isoformat()}_{emit_days}_{radius_mode}.pkl"


#: regions processed in parallel worker processes (each ~1.5 GB peak on the largest region; numpy is single-threaded
#: here, so 2 workers roughly halve the wall time of the ~25k-security universe)
PARALLEL_REGIONS = 2
CKPT_MAX_AGE_H = 36


def run_envelopes(settings: Settings | None = None, as_of: date | None = None, regions: list[str] | None = None,
                  emit_days: int = EMIT_DAYS, write: bool = True, universes: tuple[str, ...] = ("full",),
                  radius_mode: str = RADIUS_MODE, parallel: int | None = None) -> dict[str, Any]:
    """All regions -> conformal_envelopes / touch curves / coverage audit.

    Refresh-pipeline fixes 2026-10-26: the universe grew to ~25k securities and the stateless recompute took ~70-80 min
    on a quiet machine (APAC 5, CN 10-15, EU 4, HK 7, JP 9, US 15+ min) against a 45-min timeout, and every retry
    started again from the first region, so the job never finished under load. Now regions run in PARALLEL_REGIONS
    worker processes and each finished region is checkpointed (data/state/conformal/envelopes_ckpt_<as_of>_*.pkl:
    regions done, their audit counts and touch curves): a retry / re-run for the same as_of resumes with the
    remaining regions. The results are identical (per-region computations are independent; the audit is a sum)."""
    import pickle  # noqa: PLC0415

    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    meta = D.universe_ids(universes, s)
    regions = regions or D.price_regions(s)
    vix = D.vix_daily(as_of - timedelta(days=HISTORY_DAYS), as_of, s)
    states = D.macro_states(s)
    audit = Audit()
    rep: dict[str, Any] = {"as_of": str(as_of), "regions": {}}
    total = 0
    all_curves: list[dict[str, Any]] = []
    last_dates = []
    ck = _ckpt_path(s, as_of, emit_days, radius_mode)
    if write and ck.exists() and time.time() - ck.stat().st_mtime < CKPT_MAX_AGE_H * 3600:
        try:
            st = pickle.loads(ck.read_bytes())
            rep["regions"] = dict(st["regions"])
            _merge_audit(audit, st["audit"])
            all_curves = list(st["curves"])
            total = int(st["total"])
            last_dates = list(st["last_dates"])
            rep["resumed_regions"] = sorted(rep["regions"])
            log.info("conformal_envelopes: resuming %s (done: %s)", as_of, ", ".join(rep["resumed_regions"]))
        except Exception as e:  # noqa: BLE001 - a bad checkpoint only costs a full recompute
            log.warning("conformal_envelopes: checkpoint unreadable (%s); recomputing every region", e)
    todo = [rg for rg in regions if rg not in rep["regions"]]

    def done(r: dict[str, Any]) -> None:
        nonlocal total
        rg = r["region"]
        total += int(r["rows"] or 0)
        if r["last"] is not None:
            last_dates.append(r["last"])
        all_curves.extend(r["curves"])
        _merge_audit(audit, r["audit"])
        rep["regions"][rg] = r["info"]
        log.info("conformal_envelopes %s: %s", rg, r["info"])
        if write:
            ck.parent.mkdir(parents=True, exist_ok=True)
            tmp = ck.with_suffix(".tmp")
            tmp.write_bytes(pickle.dumps({"regions": rep["regions"], "audit": audit.c, "curves": all_curves,
                                          "total": total, "last_dates": last_dates}))
            tmp.replace(ck)

    n_par = max(1, int(parallel or PARALLEL_REGIONS))
    if n_par > 1 and len(todo) > 1:
        import concurrent.futures as cf  # noqa: PLC0415
        import multiprocessing as mp  # noqa: PLC0415

        args = [(rg, as_of, meta, emit_days, vix, states, radius_mode, write) for rg in todo]
        with cf.ProcessPoolExecutor(max_workers=min(n_par, len(todo)), mp_context=mp.get_context("spawn")) as ex:
            futs = [ex.submit(_region_task, a) for a in args]
            for f in cf.as_completed(futs):
                done(f.result())
    else:
        for rg in todo:
            done(_region_task((rg, as_of, meta, emit_days, vix, states, radius_mode, write)))
    cov = audit.frame(as_of)
    if write:
        if all_curves:
            write_table(pl.DataFrame(all_curves, infer_schema_length=None), "conformal_touch_curves", mode="overwrite", settings=s)
        if cov.height:
            write_table(cov, "conformal_coverage", mode="upsert", settings=s)
        if last_dates:
            cutoff = sorted(set(last_dates))[-1] - timedelta(days=int(RETENTION * 7 / 5) + 2)
            rep["pruned_partitions"] = prune_old(s, cutoff)
        ck.unlink(missing_ok=True)
    rep.update(rows=total, curves=len(all_curves), coverage_rows=cov.height, seconds=round(time.perf_counter() - t0, 1))
    if cov.height:
        o = cov.filter((pl.col("segment_type") == "overall") & (pl.col("horizon") == "joint"))
        rep["overall"] = {f"{r['application']}|{r['filter']}|{r['level']}": round(r["coverage"], 4) for r in o.iter_rows(named=True)}
    _state(s, last_envelopes_run=rep)
    return rep


def _state(settings: Settings, **kv: Any) -> None:
    p = settings.data_dir / "state" / "conformal.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        st = {}
    st.update(kv)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_envelopes(ctx.settings, as_of=date.fromisoformat(p["as_of"]) if p.get("as_of") else ctx.as_of,
                        regions=p.get("regions"), emit_days=int(p.get("emit_days", EMIT_DAYS)),
                        radius_mode=str(p.get("radius_mode", RADIUS_MODE)),
                        parallel=int(p["parallel"]) if p.get("parallel") else None)
    ok = rep.get("rows", 0) > 0
    return JobResult(status="success" if ok else "partial",
                     rows_written={"conformal_envelopes": rep.get("rows", 0), "conformal_touch_curves": rep.get("curves", 0),
                                   "conformal_coverage": rep.get("coverage_rows", 0)},
                     watermark=rep.get("as_of"), message=json.dumps({k: v for k, v in rep.items() if k != "regions"}, default=str)[:4000])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--regions")
    ap.add_argument("--emit-days", type=int, default=EMIT_DAYS)
    ap.add_argument("--radius-mode", default=RADIUS_MODE)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rep = run_envelopes(as_of=date.fromisoformat(a.as_of) if a.as_of else None,
                        regions=a.regions.split(",") if a.regions else None, emit_days=a.emit_days,
                        write=not a.dry_run, radius_mode=a.radius_mode)
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
