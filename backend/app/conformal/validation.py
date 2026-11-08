"""P4 validation gate, application A (docs/CONFORMAL_VALIDATION.md): walk-forward 2010 -> 2026 of the conformal
price envelope on mvp US + the major EU / UK / JP names, against baselines. Results are aggregated counts written to
data/reports/conformal/wf_env/<region>_<method>.parquet (resumable: existing files are skipped).

Protocol per test year Y (Algorithm 1 with refits):
  * fitted filters (garch, static, factor) are fit on origins whose outcomes end before Jan 1 of Y-1 (20-session
    purge), the GRU head on a 6-year block before its 3-year test block (refit 2009 / 2012 / ... );
  * calibration = pooled (region x sector) rolling window of the last 500 realised origins, restricted to origins
    >= Jan 1 of Y-1 (scores of the FROZEN model only), separate earnings-event pool, lag 20 (joint) / 1 (next bar);
  * test = origins in Y; ACI (gamma 0.005) restarts at alpha each year.
Methods (joint 1/5/10/20 vector): ewma, garch, static (per-security 4x4 covariance of the pretrain vectors), factor
(rank-1 + diagonal of the same), gru (learned horizon head); baselines atr_k2 (2 x ATR14 x sqrt(h) box, not
calibrated), atr_cal (ATR box with a conformally calibrated multiple), uni_static / uni_ewma (per-horizon split
conformal at alpha/4, Bonferroni box; static / EWMA scale). Next bar (H, L, C): ewma, garch, static, atr_cal, uni_ewma.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.conformal import data as D
from app.conformal import envelope as V
from app.conformal.scores import log_unit_ball, to_lowrank
from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.conformal.validation")

START = date(2003, 1, 1)
END = date(2026, 10, 20)
TEST_YEARS = list(range(2010, 2027))
REGIONS = {"US": ("mvp_us",), "EU": ("mvp_global",), "UK": ("mvp_global",), "JP": ("mvp_global",)}
JOINT_METHODS = ("ewma", "garch", "static", "factor", "gru", "atr_k2", "atr_cal", "uni_static", "uni_ewma",
                 "m_ewma", "m_garch", "m_gru", "m_atr", "m_static")
# m_*: MARGINAL single-horizon bands (each horizon calibrated alone at alpha; the chart-band use case): coverage per
# horizon is the target, the 'joint' column is then just the share of origins with all four inside.
NB_METHODS = ("ewma", "garch", "static", "atr_cal", "uni_ewma")
ALPHAS = V.ALPHAS
LEVELS = (80, 90, 95)
COVID = (np.datetime64("2020-02-15"), np.datetime64("2020-06-30"))
RATES = (np.datetime64("2022-01-01"), np.datetime64("2022-12-31"))


def report_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "reports" / "conformal"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).project_root / "scratchpad" / "conformal" / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------------------------------------------------------------------------------ data prep


@dataclass
class Prep:
    region: str
    dates: np.ndarray
    ids: list[str]
    sectors: list[str]
    a: V.EnvArrays
    E: np.ndarray
    ke: np.ndarray
    Y: np.ndarray            # (T, N, 4)
    Xn: np.ndarray           # (T, N, 3)
    atr: np.ndarray          # ATR14 / close (log-return units), known at t
    pool: np.ndarray
    pool_names: list[str]
    vix_high: np.ndarray     # (T,) VIX > 25 at origin
    close_raw: np.ndarray
    dv: np.ndarray


def atr_pct(a: V.EnvArrays, n: int = 14) -> np.ndarray:
    """Wilder ATR on adjusted bars divided by the close (log-return units), NaN-aware."""
    c = np.exp(a.lc)
    h, l = np.exp(a.lh), np.exp(a.ll)  # noqa: E741
    pc = np.vstack([c[:1], c[:-1]])
    tr = np.fmax(h - l, np.fmax(np.abs(h - pc), np.abs(l - pc)))
    T, N = tr.shape
    out = np.full((T, N), np.nan)
    atr = np.nanmean(tr[:n], axis=0)
    for t in range(T):
        x = tr[t]
        ok = np.isfinite(x)
        atr = np.where(ok & ~np.isfinite(atr), x, atr)          # late listings: start the average at the first bar
        atr = np.where(ok, (atr * (n - 1) + np.where(ok, x, 0.0)) / n, atr)
        out[t] = atr
    seen = np.cumsum(np.isfinite(tr), axis=0)
    out[seen < n] = np.nan
    return out / c


def prepare(region: str, settings: Settings | None = None, start: date = START, end: date = END) -> Prep | None:
    s = settings or get_settings()
    meta = D.universe_ids(REGIONS[region], s)
    pan = D.load_panel(region, start, end, meta["security_id"].to_list(), s, meta=meta)
    if pan is None:
        return None
    keep = np.isfinite(pan.C).sum(0) >= 500
    pan = pan.subset([x for x, k in zip(pan.ids, keep, strict=True) if k])
    ev = D.earnings_days(pan.ids, start, end, s)
    E = D.flag_matrix(pan.dates, pan.ids, ev)
    a = V.make_arrays(pan.C, pan.H, pan.L, pan.O, E)
    ke = V.first_event_offset(E, 20)
    m = meta.unique("security_id")
    sec = dict(zip(m["security_id"].to_list(), m["sector_id"].to_list(), strict=True))
    sectors = [sec.get(x) or "NA" for x in pan.ids]
    pool, names = V.pool_index(V.pool_keys(region, sectors))
    vix = D.vix_daily(start, end, s)
    vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
    k = np.clip(np.searchsorted(vd, pan.dates, side="right") - 1, 0, len(vd) - 1)
    vh = vix["vix"].to_numpy()[k] > 25.0
    return Prep(region, pan.dates, pan.ids, sectors, a, E, ke, V.forward_targets(a), V.nextbar_targets(a),
                atr_pct(a), pool, names, vh, pan.close_raw, pan.dv)


def year_bounds(dates: np.ndarray, y: int) -> tuple[int, int]:
    yrs = dates.astype("datetime64[Y]").astype(int) + 1970
    idx = np.nonzero(yrs == y)[0]
    return (int(idx[0]), int(idx[-1]) + 1) if idx.size else (-1, -1)


# ------------------------------------------------------------------------------------------------ method outputs


@dataclass
class JointOut:
    """For origins in [lo, hi): score R (T, N), per-horizon scale^2 S (T, N, 4) so that the joint set's
    per-horizon half-width is sqrt(q * S) (ellipsoids) - or, for boxes, per-horizon scores Rh (T, N, 4) and a kind."""
    kind: str                       # ellipsoid | box_max | bonferroni
    R: np.ndarray | None = None
    S: np.ndarray | None = None
    logdet: np.ndarray | None = None
    Rh: np.ndarray | None = None
    unit: np.ndarray | None = None  # box: per-horizon unit width (T, N, 4) (half-width = q * unit) / bonferroni: sqrt scale


def _ellipsoid_from_V(Y: np.ndarray, Vh: np.ndarray) -> JointOut:
    R = V.nested_score(Y, 0.0, Vh)
    w = np.maximum(np.diff(Vh, axis=-1, prepend=0.0), 1e-14)
    return JointOut("ellipsoid", R=R, S=Vh, logdet=np.log(w).sum(-1))


def _ellipsoid_dense(Y: np.ndarray, Sig: np.ndarray) -> JointOut:
    """Sig (N, 4, 4) per security (static over the slice) -> scores for every row."""
    T, N, K = Y.shape
    Sb = np.broadcast_to(Sig[None], (T, N, K, K))
    R = V.mahal_small(Y, Sb)
    S = np.broadcast_to(np.diagonal(Sig, axis1=1, axis2=2)[None], (T, N, K)).copy()
    ld = np.broadcast_to(np.linalg.slogdet(Sig)[1][None], (T, N)).copy()
    return JointOut("ellipsoid", R=R, S=S, logdet=ld)


def static_cov(Y: np.ndarray, rows: slice, min_n: int = 250) -> np.ndarray:
    """Per-security 4x4 covariance (mean 0) of the forward vectors in `rows` (origins whose outcome ends inside)."""
    Z = Y[rows]
    ok = np.isfinite(Z).all(-1)
    Zs = np.where(ok[..., None], Z, 0.0)
    n = ok.sum(0)
    S = np.einsum("tni,tnj->nij", Zs, Zs) / np.maximum(n, 1)[:, None, None]
    glob = np.nanmedian(np.where((n >= min_n)[:, None, None], S, np.nan), axis=0)
    bad = n < min_n
    S[bad] = glob
    return S + 1e-10 * np.eye(Y.shape[-1])


def joint_method(p: Prep, name: str, fit_end: int, lo: int, hi: int, cache: dict[str, Any]) -> JointOut:
    """Outputs for origins [lo, hi) of a model frozen on data before fit_end (origin index)."""
    a, ke, Y = p.a, p.ke, p.Y[lo:hi]
    if name.startswith("m_"):
        base = {"m_ewma": "ewma", "m_garch": "garch", "m_gru": "gru", "m_static": "uni_static", "m_atr": "atr_cal"}[name]
        o = joint_method(p, base, fit_end, lo, hi, cache)
        if o.kind == "ellipsoid":
            return JointOut("marginal", Rh=Y * Y / np.maximum(o.S, 1e-14), unit=np.sqrt(o.S))
        if o.kind == "box_max":
            return JointOut("marginal", Rh=o.Rh ** 2, unit=o.unit)
        return JointOut("marginal", Rh=o.Rh, unit=o.unit)
    if name in ("ewma", "uni_ewma"):
        if "ewma" not in cache:
            s2 = V.ewma_var(a.r, a.E)
            cache["ewma"] = V.horizon_V(s2, V.jump_var(a.r, a.E, s2), ke)
        Vh = cache["ewma"][lo:hi]
        if name == "ewma":
            return _ellipsoid_from_V(Y, Vh)
        return JointOut("bonferroni", Rh=Y * Y / np.maximum(Vh, 1e-14), unit=np.sqrt(Vh))
    if name == "garch":
        gp = V.garch_fit(a.r, a.E, slice(0, fit_end))
        s2 = V.garch_var(a.r, a.E, gp)
        Vh = V.horizon_V(s2, V.jump_var(a.r, a.E, s2), ke, garch=gp)[lo:hi]
        return _ellipsoid_from_V(Y, Vh)
    if name in ("static", "factor"):
        f0 = max(0, fit_end - 5 * 252)
        Sig = static_cov(p.Y, slice(f0, max(f0 + 1, fit_end - 20)))
        if name == "factor":
            c = to_lowrank(Sig, 1)
            Sig = c.to_dense()
        return _ellipsoid_dense(Y, Sig)
    if name == "uni_static":
        f0 = max(0, fit_end - 5 * 252)
        sd = np.nanstd(a.r[f0:fit_end], axis=0)
        sd = np.where(np.isfinite(sd) & (sd > 0), sd, np.nanmedian(sd))
        Vh = (sd ** 2)[None, :, None] * np.array(V.HORIZONS, dtype=float)[None, None, :]
        Vh = np.broadcast_to(Vh, Y.shape)
        return JointOut("bonferroni", Rh=Y * Y / Vh, unit=np.sqrt(Vh))
    if name in ("atr_k2", "atr_cal"):
        unit = p.atr[lo:hi][..., None] * np.sqrt(np.array(V.HORIZONS, dtype=float))
        return JointOut("box_max", Rh=np.abs(Y) / unit, unit=unit)
    if name == "gru":
        Vg = cache.get("gru_V")
        if Vg is None:
            raise KeyError("gru outputs not available")
        return _ellipsoid_from_V(Y, Vg[lo:hi])
    raise ValueError(name)


# ------------------------------------------------------------------------------------------------ evaluation


class Agg:
    def __init__(self) -> None:
        self.rows: dict[tuple, np.ndarray] = {}

    COLS = ("n", "cov", "cov_1", "cov_5", "cov_10", "cov_20", "w_sum", "hw_1", "hw_5", "hw_10", "hw_20", "lv_sum", "n_w")

    def add(self, key: tuple, vals: dict[str, np.ndarray], mask: np.ndarray) -> None:
        cur = self.rows.get(key)
        if cur is None:
            cur = self.rows[key] = np.zeros(len(self.COLS))
        for i, c in enumerate(self.COLS):
            v = vals.get(c)
            if v is None:
                continue
            vv = v[mask]
            cur[i] += float(np.nansum(vv)) if c != "n" else float(mask.sum())

    def frame(self, names: tuple[str, ...]) -> pl.DataFrame:
        if not self.rows:
            return pl.DataFrame()
        keys = list(self.rows)
        d = {n: [k[i] for k in keys] for i, n in enumerate(names)}
        arr = np.vstack([self.rows[k] for k in keys])
        for i, c in enumerate(self.COLS):
            d[c] = arr[:, i]
        return pl.DataFrame(d)


def _segments(p: Prep, tt: np.ndarray) -> dict[str, np.ndarray]:
    d = p.dates[tt]
    return {"year": (d.astype("datetime64[Y]").astype(int) + 1970), "month": d.astype("datetime64[M]").astype(int) % 12 + 1,
            "vix": p.vix_high[tt], "covid": (d >= COVID[0]) & (d <= COVID[1]), "rates22": (d >= RATES[0]) & (d <= RATES[1])}


def evaluate_joint(p: Prep, name: str, cache: dict[str, Any], agg: Agg, years: list[int] = TEST_YEARS,
                   lag: int = 20, window: int = 500) -> None:
    hz = np.array(V.HORIZONS, dtype=float)
    for y in years:
        t0, t1 = year_bounds(p.dates, y)
        c0, _ = year_bounds(p.dates, y - 1)
        if t0 < 0 or c0 < 0:
            continue
        fit_end = c0
        if name == "gru":
            if "gru_V" not in cache:
                continue
        lo, hi = c0, t1
        out = joint_method(p, name, fit_end, lo, hi, cache)
        T, N = hi - lo, len(p.ids)
        ev = p.ke[lo:hi] > 0
        tt_loc = np.arange(t0 - lo, t1 - lo)
        Yw = p.Y[lo:hi]
        if out.kind == "ellipsoid":
            R = out.R
            pc = V.PoolCal(R, p.pool, ev, lag=lag, window=window, min_n=200).run(t0 - lo, t1 - lo)
            for mode, src in (("plain", pc.q), ("aci", pc.q_aci)):
                for j, lvl in enumerate(LEVELS):
                    q = np.where(ev[tt_loc], src[tt_loc][:, p.pool, 1, j], src[tt_loc][:, p.pool, 0, j])
                    Rt = R[tt_loc]
                    ok = np.isfinite(Rt) & ~np.isnan(q)          # q = inf (ACI alpha_t <= 0): unbounded set, covered
                    hw = np.sqrt(np.maximum(q, 0)[..., None] * out.S[tt_loc])
                    vals = {"cov": (Rt <= q).astype(float)}
                    for k, h in enumerate(V.HORIZONS):
                        vals[f"cov_{h}"] = (np.abs(Yw[tt_loc][..., k]) <= hw[..., k]).astype(float)
                        vals[f"hw_{h}"] = hw[..., k]
                    vals["w_sum"] = (2 * hw).mean(-1)
                    with np.errstate(divide="ignore", invalid="ignore"):
                        vals["lv_sum"] = log_unit_ball(4) / 4 + 0.5 * np.log(q) + out.logdet[tt_loc] / 8
                    vals["n_w"] = np.isfinite(vals["w_sum"]).astype(float)
                    _add(agg, p, name, mode, lvl, tt_loc + lo, vals, ok)
        elif out.kind in ("box_max", "bonferroni", "marginal"):
            Rh = out.Rh
            if name == "atr_k2":
                for j, lvl in enumerate(LEVELS):
                    kk = {80: 1.5, 90: 2.0, 95: 2.5}[lvl]      # conventional multiples, not calibrated
                    hw = kk * out.unit[tt_loc]
                    _box_vals(agg, p, name, "fixed", lvl, tt_loc, lo, Yw, hw)
                continue
            if out.kind == "box_max":
                Rj = np.nanmax(Rh, -1)
                Rj[~np.isfinite(Rh).all(-1)] = np.nan
                pc = V.PoolCal(Rj, p.pool, ev, lag=lag, window=window, min_n=200).run(t0 - lo, t1 - lo)
                for mode, src in (("plain", pc.q), ("aci", pc.q_aci)):
                    for j, lvl in enumerate(LEVELS):
                        q = np.where(ev[tt_loc], src[tt_loc][:, p.pool, 1, j], src[tt_loc][:, p.pool, 0, j])
                        hw = q[..., None] * out.unit[tt_loc]
                        _box_vals(agg, p, name, mode, lvl, tt_loc, lo, Yw, hw)
            else:   # bonferroni: per-horizon split conformal at alpha / K on Y_h^2 / V_h (marginal: at alpha)
                K = len(V.HORIZONS)
                div = 1 if out.kind == "marginal" else K
                qs = {}
                for k, h in enumerate(V.HORIZONS):
                    evh = (p.ke[lo:hi] > 0) & (p.ke[lo:hi] <= h)
                    pc = V.PoolCal(Rh[..., k], p.pool, evh, lag=h, window=window, min_n=200,
                                   alphas=tuple(a / div for a in ALPHAS)).run(t0 - lo, t1 - lo)
                    qs[k] = (pc, evh)
                for mode in ("plain", "aci"):
                    for j, lvl in enumerate(LEVELS):
                        hw = np.empty((len(tt_loc), N, K))
                        for k in range(K):
                            pc, evh = qs[k]
                            src = pc.q if mode == "plain" else pc.q_aci
                            q = np.where(evh[tt_loc], src[tt_loc][:, p.pool, 1, j], src[tt_loc][:, p.pool, 0, j])
                            hw[..., k] = np.sqrt(np.maximum(q, 0)) * out.unit[tt_loc][..., k]
                        _box_vals(agg, p, name, mode, lvl, tt_loc, lo, Yw, hw)
    del hz


def _box_vals(agg: Agg, p: Prep, name: str, mode: str, lvl: int, tt_loc: np.ndarray, lo: int, Yw: np.ndarray,
              hw: np.ndarray) -> None:
    Yt = Yw[tt_loc]
    cov_h = np.abs(Yt) <= hw
    ok = np.isfinite(Yt).all(-1) & ~np.isnan(hw).any(-1)
    vals = {"cov": cov_h.all(-1).astype(float)}
    for k, h in enumerate(V.HORIZONS):
        vals[f"cov_{h}"] = cov_h[..., k].astype(float)
        vals[f"hw_{h}"] = hw[..., k]
    vals["w_sum"] = (2 * hw).mean(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        vals["lv_sum"] = np.log(2 * hw).mean(-1)
    vals["n_w"] = np.isfinite(vals["w_sum"]).astype(float)
    _add(agg, p, name, mode, lvl, tt_loc + lo, vals, ok)


KEY_NAMES = ("method", "mode", "level", "region", "sector", "year", "month", "high_vix", "event")


def _add(agg: Agg, p: Prep, name: str, mode: str, lvl: int, tt: np.ndarray, vals: dict[str, np.ndarray], ok: np.ndarray,
         ev: np.ndarray | None = None) -> None:
    """Aggregate per (year, month, high_vix, sector, event) with one bincount per value column."""
    seg = _segments(p, tt)
    ev = (p.ke[tt] > 0) if ev is None else ev
    secs = sorted(set(p.sectors))
    si = np.array([secs.index(x) for x in p.sectors])
    T, N = ok.shape
    ym = (seg["year"] - 2000) * 12 + (seg["month"] - 1)                       # (T,)
    key = ((ym[:, None] * 2 + seg["vix"][:, None].astype(int)) * len(secs) + si[None, :]) * 2 + ev.astype(int)
    k = key[ok]
    uk, inv = np.unique(k, return_inverse=True)
    sums = {}
    for c in Agg.COLS:
        if c == "n":
            sums[c] = np.bincount(inv, minlength=len(uk)).astype(float)
        elif c in vals:
            v = np.asarray(vals[c], dtype=np.float64)[ok]
            sums[c] = np.bincount(inv, weights=np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0), minlength=len(uk))
    for i, kv in enumerate(uk):
        e_ = kv % 2
        r = kv // 2
        sx = r % len(secs)
        r //= len(secs)
        hv = r % 2
        ymv = r // 2
        y, m = 2000 + ymv // 12, ymv % 12 + 1
        keyt = (name, mode, lvl, p.region, secs[sx], int(y), int(m), bool(hv), bool(e_))
        cur = agg.rows.get(keyt)
        if cur is None:
            cur = agg.rows[keyt] = np.zeros(len(Agg.COLS))
        for j, c in enumerate(Agg.COLS):
            if c in sums:
                cur[j] += sums[c][i]


# ------------------------------------------------------------------------------------------------ next bar


def nb_method(p: Prep, name: str, fit_end: int, lo: int, hi: int, cache: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    """(R or box scores, (mu, var-per-coordinate / unit)) for origins [lo, hi)."""
    a = p.a
    if name in ("ewma", "uni_ewma"):
        if "nb_ewma" not in cache:
            s2 = V.ewma_var(a.r, a.E)
            cache["nb_ewma"] = V.nextbar_filter(a, s2, V.jump_var(a.r, a.E, s2))
        return cache["nb_ewma"][0][lo:hi], cache["nb_ewma"][1][lo:hi]
    if name == "garch":
        gp = V.garch_fit(a.r, a.E, slice(0, fit_end))
        s2 = V.garch_var(a.r, a.E, gp)
        mu, S = V.nextbar_filter(a, s2, V.jump_var(a.r, a.E, s2))
        return mu[lo:hi], S[lo:hi]
    if name == "static":
        f0 = max(0, fit_end - 5 * 252)
        Z = p.Xn[f0:fit_end - 1]
        ok = np.isfinite(Z).all(-1)
        cnt = np.maximum(ok.sum(0), 1)
        mu = np.where(ok[..., None], Z, 0).sum(0) / cnt[:, None]
        dz = np.where(ok[..., None], Z - mu, 0)
        S = np.einsum("tni,tnj->nij", dz, dz) / cnt[:, None, None] + 1e-10 * np.eye(3)
        T = hi - lo
        return np.broadcast_to(mu[None], (T,) + mu.shape).copy(), np.broadcast_to(S[None], (T,) + S.shape).copy()
    if name == "atr_cal":
        unit = p.atr[lo:hi]
        f0 = max(0, fit_end - 5 * 252)
        m = np.nanmedian(p.Xn[f0:fit_end - 1] / p.atr[f0:fit_end - 1][..., None], axis=(0, 1))
        return m[None, None, :] * unit[..., None], unit
    raise ValueError(name)


def evaluate_nextbar(p: Prep, name: str, cache: dict[str, Any], agg: Agg, years: list[int] = TEST_YEARS,
                     window: int = 500) -> None:
    for y in years:
        t0, t1 = year_bounds(p.dates, y)
        c0, _ = year_bounds(p.dates, y - 1)
        if t0 < 0 or c0 < 0:
            continue
        lo, hi = c0, t1
        mu, S = nb_method(p, name, c0, lo, hi, cache)
        X = p.Xn[lo:hi]
        ev = np.zeros((hi - lo, len(p.ids)), dtype=bool)
        ev[:-1] = p.E[lo + 1:hi]
        tt = np.arange(t0 - lo, t1 - lo)
        r = X - mu
        if name in ("ewma", "garch", "static"):
            R = V.mahal_small(r, S)
            diag = np.diagonal(S, axis1=-2, axis2=-1)
            pc = V.PoolCal(R, p.pool, ev, lag=1, window=window, min_n=200).run(t0 - lo, t1 - lo)
            for mode, src in (("plain", pc.q), ("aci", pc.q_aci)):
                for j, lvl in enumerate(LEVELS):
                    q = np.where(ev[tt], src[tt][:, p.pool, 1, j], src[tt][:, p.pool, 0, j])
                    hw = np.sqrt(np.maximum(q, 0)[..., None] * diag[tt])
                    ok = np.isfinite(R[tt]) & ~np.isnan(q)
                    vals = {"cov": (R[tt] <= q).astype(float)}
                    for k, h in enumerate((1, 5, 10)):           # slots reused: h=high, l=low, c=close
                        vals[f"cov_{h}"] = (np.abs(r[tt][..., k]) <= hw[..., k]).astype(float)
                        vals[f"hw_{h}"] = hw[..., k]
                    vals["w_sum"] = (2 * hw).mean(-1)
                    sld = np.linalg.slogdet(S[tt] + 1e-14 * np.eye(3))[1]
                    with np.errstate(divide="ignore", invalid="ignore"):
                        vals["lv_sum"] = log_unit_ball(3) / 3 + 0.5 * np.log(q) + sld / 6
                    vals["n_w"] = np.isfinite(vals["w_sum"]).astype(float)
                    _add_nb(agg, p, name, mode, lvl, tt + lo, vals, ok)
        else:
            unit = S if name == "atr_cal" else None
            if name == "atr_cal":
                Rh = np.abs(r) / unit[..., None]
            else:   # uni_ewma
                if "nb_ewma" not in cache:
                    nb_method(p, "ewma", c0, lo, hi, cache)
                mu2, S2 = cache["nb_ewma"][0][lo:hi], cache["nb_ewma"][1][lo:hi]
                r = X - mu2
                d2 = np.diagonal(S2, axis1=-2, axis2=-1)
                Rh = r * r / d2
            if name == "atr_cal":
                Rj = np.nanmax(Rh, -1)
                Rj[~np.isfinite(Rh).all(-1)] = np.nan
                pc = V.PoolCal(Rj, p.pool, ev, lag=1, window=window, min_n=200).run(t0 - lo, t1 - lo)
                for mode, src in (("plain", pc.q), ("aci", pc.q_aci)):
                    for j, lvl in enumerate(LEVELS):
                        q = np.where(ev[tt], src[tt][:, p.pool, 1, j], src[tt][:, p.pool, 0, j])
                        hw = q[..., None] * unit[tt][..., None] * np.ones(3)
                        _nb_box(agg, p, name, mode, lvl, tt, lo, r, hw)
            else:
                pcs = [V.PoolCal(Rh[..., k], p.pool, ev, lag=1, window=window, min_n=200,
                                 alphas=tuple(a_ / 3 for a_ in ALPHAS)).run(t0 - lo, t1 - lo) for k in range(3)]
                for mode in ("plain", "aci"):
                    for j, lvl in enumerate(LEVELS):
                        hw = np.empty((len(tt), len(p.ids), 3))
                        for k in range(3):
                            src = pcs[k].q if mode == "plain" else pcs[k].q_aci
                            q = np.where(ev[tt], src[tt][:, p.pool, 1, j], src[tt][:, p.pool, 0, j])
                            hw[..., k] = np.sqrt(np.maximum(q, 0) * d2[tt][..., k])
                        _nb_box(agg, p, name, mode, lvl, tt, lo, r, hw)


def _nb_box(agg: Agg, p: Prep, name: str, mode: str, lvl: int, tt: np.ndarray, lo: int, r: np.ndarray, hw: np.ndarray) -> None:
    rt = r[tt]
    cov_h = np.abs(rt) <= hw
    ok = np.isfinite(rt).all(-1) & ~np.isnan(hw).any(-1)
    vals = {"cov": cov_h.all(-1).astype(float)}
    for k, h in enumerate((1, 5, 10)):
        vals[f"cov_{h}"] = cov_h[..., k].astype(float)
        vals[f"hw_{h}"] = hw[..., k]
    vals["w_sum"] = (2 * hw).mean(-1)
    with np.errstate(divide="ignore", invalid="ignore"):
        vals["lv_sum"] = np.log(2 * hw).mean(-1)
    vals["n_w"] = np.isfinite(vals["w_sum"]).astype(float)
    _add_nb(agg, p, name, mode, lvl, tt + lo, vals, ok)


def _add_nb(agg: Agg, p: Prep, name: str, mode: str, lvl: int, tt: np.ndarray, vals: dict[str, np.ndarray], ok: np.ndarray) -> None:
    nxt = np.minimum(tt + 1, len(p.dates) - 1)
    _add(agg, p, name, mode, lvl, tt, vals, ok, ev=p.E[nxt])


# ------------------------------------------------------------------------------------------------ GRU head (A)


def gru_features(p: Prep) -> tuple[np.ndarray, np.ndarray]:
    """Per-security features known at t (B=N, T, 8) and the EWMA base variance (N, T)."""
    a = p.a
    s2 = V.ewma_var(a.r, a.E)
    sd = np.sqrt(s2)
    sd_prev = np.vstack([sd[:1], sd[:-1]])
    rng = (a.lh - a.ll)
    gap = np.abs(a.lo - np.vstack([a.lc[:1], a.lc[:-1]]))
    seg = np.zeros(p.ke.shape + (4,))
    bounds = [(1, 1), (2, 5), (6, 10), (11, 20)]
    for k, (b0, b1) in enumerate(bounds):
        seg[..., k] = ((p.ke >= b0) & (p.ke <= b1)).astype(float)
    with np.errstate(invalid="ignore", divide="ignore"):
        F = np.stack([a.r / sd_prev, np.abs(a.r) / sd_prev, rng / sd_prev, gap / sd_prev], -1)
    F = np.clip(np.nan_to_num(F, nan=0.0), -10, 10)
    F = np.concatenate([F, seg], -1)
    return np.moveaxis(F, 0, 1), s2.T


def run_gru_region_outputs(preps: dict[str, Prep], fit_years: list[int], log_fn: Any = None, epochs: int = 12) -> dict[str, np.ndarray]:
    """Train the shared horizon GRU per refit block on all regions' securities; emit V (T, N, 4) per region, stitched
    so that each test year uses the model of its block (block test years fy+1 .. fy+3, fit < Jan 1 of fy)."""
    from app.conformal.learned import HorizonGRU  # noqa: PLC0415

    out = {r: np.full(p.Y.shape, np.nan) for r, p in preps.items()}
    feats = {r: gru_features(p) for r, p in preps.items()}
    for fy in fit_years:
        Fs, Ys, Bs, Vs = [], [], [], []
        for r, p in preps.items():
            c0, _ = year_bounds(p.dates, fy)
            if c0 < 0:
                continue
            f0 = max(0, c0 - 6 * 252)
            F, base = feats[r]
            sl = slice(f0, c0)
            Y = np.moveaxis(p.Y[sl], 0, 1)
            valid = np.isfinite(Y).all(-1) & np.isfinite(base[:, sl])
            valid[:, -21:] = False                      # purge: outcomes must end before the fit date
            Fs.append(F[:, sl])
            Ys.append(Y)
            Bs.append(base[:, sl])
            Vs.append(valid)
        Tm = min(x.shape[1] for x in Fs)
        F = np.concatenate([x[:, -Tm:] for x in Fs])
        Y = np.concatenate([x[:, -Tm:] for x in Ys])
        Bv = np.concatenate([x[:, -Tm:] for x in Bs])
        Vm = np.concatenate([x[:, -Tm:] for x in Vs])
        m = HorizonGRU(epochs=epochs).fit(F.astype(np.float32), Y, np.nan_to_num(Bv, nan=1e-4), Vm, log=log_fn)
        for r, p in preps.items():
            Fr, base = feats[r]
            S = m.emit(Fr, np.nan_to_num(base, nan=1e-4))          # (N, T, 4, 4)
            Vr = np.moveaxis(np.diagonal(S, axis1=-2, axis2=-1), 0, 1)   # nested -> V(h) on the diagonal
            Vr = np.where(np.isfinite(base.T)[..., None], Vr, np.nan)
            for ty in (fy + 1, fy + 2, fy + 3):
                c0, _ = year_bounds(p.dates, ty - 1)
                _, t1 = year_bounds(p.dates, ty)
                if c0 >= 0 and t1 > 0:
                    out[r][c0:t1] = Vr[c0:t1]
        if log_fn:
            log_fn(f"gru block fit<{fy}-01-01 done: {m.meta}")
    return out


def summarize(df: pl.DataFrame, by: list[str]) -> pl.DataFrame:
    g = df.group_by(by).agg([pl.col(c).sum() for c in Agg.COLS])
    return g.with_columns((pl.col("cov") / pl.col("n")).alias("coverage"),
                          *[(pl.col(f"cov_{h}") / pl.col("n")).alias(f"coverage_{h}") for h in V.HORIZONS],
                          (pl.col("w_sum") / pl.col("n_w")).alias("w_proj"),
                          (pl.col("lv_sum") / pl.col("n_w")).alias("log_volume"),
                          (1 - pl.col("n_w") / pl.col("n")).alias("share_unbounded"),
                          *[(2 * pl.col(f"hw_{h}") / pl.col("n_w")).alias(f"width_{h}") for h in V.HORIZONS]).sort(by)


def t_now() -> float:
    return time.perf_counter()


def fmt_pct(x: float | None) -> str:
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{100 * x:.1f}%"
