"""P4 validation gate, application B: walk-forward 2010 -> 2026 of the peer-group joint ellipsoids.

Per test year Y and group: members = current definition members with <= 2% missing and <= 20% zero returns over
[Jan Y-6, Dec Y] (>= 20 members, one calendar); rows = sessions where every member printed. Filters are fit on the
pretrain block [Jan Y-6, Jan Y-1) and frozen; the GCN-GRU is one shared model per 3-year block trained on all groups'
blocks ending Jan 1 of (first test year - 1). Calibration = rolling 500 scores, origins >= Jan Y-1 (post-freeze),
re-sorted every 5 sessions; ACI restarts each year. Graph: top-5 partial correlations on the pretrain block plus
supply-chain edges known by then (PIT).

Filters: ewma (dense), ewma_lr (diag + rank 4), factor (PCA rank 4, static), static_lw, garch_dcc, gcn_gru; baseline
uni (per-member split conformal at alpha / N on r_i^2 / sigma_ewma^2, Bonferroni box). Aggregates ->
data/reports/conformal/wf_peer/<region>_<filter>.parquet; break-away signals (|z_cond| >= 2) for the economic test ->
wf_peer/signals_<region>_<filter>.parquet.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.conformal import data as D
from app.conformal import peer_groups as PG
from app.conformal.filters import EWMAFilter
from app.conformal.scores import Cov, decompose, log_unit_ball, logdet, mahalanobis, quantile_from_sorted
from app.conformal.validation import report_dir

START = date(2003, 1, 1)
END = date(2026, 10, 20)
TEST_YEARS = list(range(2010, 2027))
FILTERS = ("ewma", "ewma_lr", "factor", "static_lw", "garch_dcc", "gcn_gru", "uni")
SIGNAL_FILTERS = ("ewma", "ewma_lr", "gcn_gru", "factor")
LEVELS = (80, 90, 95)
ALPHAS = (0.2, 0.1, 0.05)
WARM = 60
COLS = ("n", "cov", "cov_m", "n_m", "w_sum", "lv_sum", "n_w")
KEYS = ("filter", "mode", "level", "region", "group_kind", "year", "month", "high_vix")


def yidx(dates: np.ndarray, y: int) -> int:
    return int(np.searchsorted(dates, np.datetime64(f"{y}-01-01")))


class GroupYear:
    """Rows / members of one group for one test year."""

    def __init__(self, X: np.ndarray, dates: np.ndarray, sids: list[str], issuers: list[str], subs: list[str], y: int,
                 kind: str):
        self.y = y
        p0, c0, t0, t1 = yidx(dates, y - 6), yidx(dates, y - 1), yidx(dates, y), yidx(dates, y + 1)
        Xw = X[p0:t1]
        keep_c, _ = PG.complete_rows(Xw)
        self.sids = [s for s, k in zip(sids, keep_c, strict=True) if k]
        self.issuers = [s for s, k in zip(issuers, keep_c, strict=True) if k]
        self.subs = [s for s, k in zip(subs, keep_c, strict=True) if k]
        self.kind = kind
        Xw = Xw[:, keep_c]
        ok = np.isfinite(Xw).all(1)
        self.X = Xw[ok]
        self.dates = dates[p0:t1][ok]
        self.pre_end = int(np.searchsorted(self.dates, dates[c0])) if c0 < len(dates) else 0
        self.test0 = int(np.searchsorted(self.dates, np.datetime64(f"{y}-01-01")))
        self.valid = len(self.sids) >= PG.N_MIN and self.pre_end >= 500 and self.test0 > self.pre_end + 100 and len(self.X) > self.test0 + 20


def emit_filter(gy: GroupYear, name: str, A: np.ndarray, gcn: Any = None) -> tuple[np.ndarray, Cov] | None:
    """Fit on the pretrain block, emit from (pre_end - WARM) to the end; returns rows aligned to gy.X[e0:]."""
    X = gy.X
    pre = slice(0, gy.pre_end)
    e0 = max(0, gy.pre_end - WARM)
    if name == "gcn_gru":
        if gcn is None:
            return None
        from app.conformal.learned import emit_gcn_gru  # noqa: PLC0415

        sd = np.nanstd(X[pre], axis=0)
        scale = np.array([gcn["scales"].get(s, sd[i]) for i, s in enumerate(gy.sids)])
        scale = np.where(np.isfinite(scale) & (scale > 0), scale, 0.02)
        # the recurrent state is started 250 sessions before the emission window (warm-up, not scored)
        s0 = max(0, e0 - 250)
        mu, cov = emit_gcn_gru(gcn["model"], X[s0:], A, scale)
        k = e0 - s0
        return mu[k:], cov.take(slice(k, None))
    if name == "uni":
        name = "ewma"
    f = PG.make_group_filter(name, A)
    f.fit(X[pre])
    if isinstance(f, EWMAFilter):
        # EWMA: warm the state on the pretrain block, then emit the window
        mu, cov = f.emit(X)
        return mu[e0:], cov.take(slice(e0, None))
    mu, cov = f.emit(X[e0:]) if name in ("factor", "static_lw", "factor_vs") else f.emit(X)
    if name in ("factor", "static_lw", "factor_vs"):
        return mu, cov
    return mu[e0:], cov.take(slice(e0, None))


def evaluate_group_year(gy: GroupYear, name: str, A: np.ndarray, region: str, vix_high: dict, agg: dict,
                        signals: list | None, gcn: Any = None) -> None:
    res = emit_filter(gy, name, A, gcn)
    if res is None:
        return
    mu, cov = res
    e0 = max(0, gy.pre_end - WARM)
    X = gy.X[e0:]
    dts = gy.dates[e0:]
    T, N = X.shape
    R = np.full(T, np.nan)
    R[:-1] = mahalanobis(X[1:] - mu[:-1], cov.take(slice(0, T - 1)))
    test = np.arange(gy.test0 - e0 - 1, T - 1)          # law at t (decision) for the test day t + 1
    test = test[test >= 0]
    cal0 = gy.pre_end - e0 + 0
    dg = cov.diag()
    vx = np.array([vix_high.get(d, False) for d in dts[test + 1].astype("datetime64[D]").tolist()])
    yrs = dts[test + 1].astype("datetime64[Y]").astype(int) + 1970
    mon = dts[test + 1].astype("datetime64[M]").astype(int) % 12 + 1
    if name == "uni":
        Rm = np.full((T, N), np.nan)
        Rm[:-1] = (X[1:] - mu[:-1]) ** 2 / dg[:-1]
        for mode in ("plain", "aci"):
            lev = np.array(ALPHAS) / N
            for j, lvl in enumerate(LEVELS):
                a_t = lev[j]
                cov_j, cov_m, w, lvs = [], [], [], []
                for t in test:
                    lo = max(cal0, t - 500)
                    blk = Rm[lo:t]
                    if blk.shape[0] < 100:
                        cov_j.append(np.nan)
                        cov_m.append(np.full(N, np.nan))
                        w.append(np.nan)
                        lvs.append(np.nan)
                        continue
                    s = np.sort(blk, axis=0)
                    n = s.shape[0]
                    a_use = a_t if mode == "aci" else lev[j]
                    k = int(np.ceil((n + 1) * (1 - a_use))) if a_use > 0 else n + 1
                    q = s[min(max(k, 1), n) - 1] if k <= n else np.full(N, np.inf)
                    c = Rm[t] <= q
                    cov_j.append(float(c.all()))
                    cov_m.append(c.astype(float))
                    hw_b = np.sqrt(q * dg[t])
                    w.append(float((2 * hw_b).mean()))
                    with np.errstate(divide="ignore"):
                        lvs.append(float(np.log(2 * hw_b).mean()))
                    if mode == "aci":     # ACI on the joint miss, level split equally over the N coordinates
                        a_t = a_t + 0.005 * (ALPHAS[j] - float(not c.all())) / N
                cj, cm, ww, lv = np.array(cov_j), np.array(cov_m), np.array(w), np.array(lvs)
                fin = np.isfinite(ww)                 # unbounded boxes (index > n at alpha / N) excluded from widths
                _agg(agg, (name, mode, lvl, region, gy.kind), yrs, mon, vx,
                     {"cov": cj, "cov_m": np.nansum(cm, 1), "n_m": np.isfinite(cm).sum(1).astype(float),
                      "w_sum": np.where(fin, ww, 0.0), "lv_sum": np.where(fin & np.isfinite(lv), lv, 0.0),
                      "n_w": fin.astype(float)}, np.isfinite(cj))
        return
    from app.conformal.protocol import Calibrator  # noqa: PLC0415

    cal = Calibrator(R, start=cal0, window=500, lag=0, recal_every=5, min_n=100)
    ld = logdet(cov.take(test))
    for mode in ("plain", "aci"):
        lev = np.array(ALPHAS, dtype=float)
        qs = np.full((len(test), 3), np.nan)
        for i, t in enumerate(test):
            srt = cal.sorted_at(int(t))
            if len(srt) < 100:
                continue
            a_use = lev if mode == "aci" else np.array(ALPHAS)
            qs[i] = [float(quantile_from_sorted(srt, a)) if a > 0 else np.inf for a in a_use]
            if mode == "aci" and np.isfinite(R[t]):
                lev = lev + 0.005 * (np.array(ALPHAS) - (R[t] > qs[i]).astype(float))
        for j, lvl in enumerate(LEVELS):
            q = qs[:, j]
            ok = ~np.isnan(q) & np.isfinite(R[test])      # q = inf (ACI alpha_t <= 0): unbounded, covered
            hw = np.sqrt(np.maximum(q, 0)[:, None] * dg[test])
            r = X[test + 1] - mu[test]
            cm = (np.abs(r) <= hw).astype(float)
            with np.errstate(divide="ignore", invalid="ignore"):
                lv = log_unit_ball(N) / N + 0.5 * np.log(q) + ld / (2 * N)
            _agg(agg, (name, mode, lvl, region, gy.kind), yrs, mon, vx,
                 {"cov": (R[test] <= q).astype(float), "cov_m": cm.sum(1), "n_m": np.full(len(test), float(N)),
                  "w_sum": (2 * hw).mean(1), "lv_sum": lv, "n_w": np.isfinite((2 * hw).mean(1)).astype(float)}, ok)
            if signals is not None and mode == "plain" and lvl == 90:
                dec = decompose(r, cov.take(test))
                zc, zm = dec["z_cond"], dec["z_marg"]
                hit = np.abs(zc) >= 2.0
                ii, jj = np.nonzero(hit & ok[:, None])
                for a_, b_ in zip(ii, jj, strict=True):
                    signals.append((gy.gid, dts[test[a_] + 1].astype("datetime64[D]").astype(object), gy.sids[b_], float(zc[a_, b_]), float(zm[a_, b_]),
                                    float(R[test[a_]]), float(q[a_]), float(r[a_, b_]), float(np.mean(r[a_]))))


def _agg(agg: dict, base: tuple, yrs: np.ndarray, mon: np.ndarray, vx: np.ndarray, vals: dict[str, np.ndarray], ok: np.ndarray) -> None:
    for y, m, v in set(zip(yrs.tolist(), mon.tolist(), vx.tolist(), strict=True)):
        sel = ok & (yrs == y) & (mon == m) & (vx == v)
        if not sel.any():
            continue
        k = base + (int(y), int(m), bool(v))
        cur = agg.setdefault(k, np.zeros(len(COLS)))
        cur[0] += sel.sum()
        for i, c in enumerate(COLS[1:], 1):
            cur[i] += float(np.nansum(np.where(np.isfinite(vals[c][sel]), vals[c][sel], 0.0)))


def agg_frame(agg: dict) -> pl.DataFrame:
    if not agg:
        return pl.DataFrame()
    keys = list(agg)
    d = {n: [k[i] for k in keys] for i, n in enumerate(KEYS)}
    arr = np.vstack([agg[k] for k in keys])
    for i, c in enumerate(COLS):
        d[c] = arr[:, i]
    return pl.DataFrame(d)


def region_inputs(region: str, grp: pl.DataFrame) -> tuple[np.ndarray, np.ndarray, dict[str, int]] | None:
    ids = grp.filter(pl.col("region_bloc") == region)["security_id"].unique().to_list()
    pan = D.load_panel(region, START, END, ids)
    if pan is None:
        return None
    return D.log_returns(pan.C), pan.dates, pan.col()


def group_years(region: str, grp: pl.DataFrame, Rall: np.ndarray, dates: np.ndarray, col: dict[str, int],
                years: list[int] = TEST_YEARS):
    sc, _ = PG._links(END, None)
    for gid, g in grp.filter(pl.col("region_bloc") == region).group_by("group_id"):
        g = g.sort("member_rank")
        sids = [x for x in g["security_id"].to_list() if x in col]
        if len(sids) < PG.N_MIN:
            continue
        sub = g.filter(pl.col("security_id").is_in(sids)).sort("member_rank")
        X = Rall[:, [col[x] for x in sub["security_id"].to_list()]]
        for y in years:
            gy = GroupYear(X, dates, sub["security_id"].to_list(), sub["issuer_id"].to_list(),
                           sub["subsector_id"].fill_null("NA").to_list(), y, g["group_kind"][0])
            gy.gid = gid[0]
            if not gy.valid:
                continue
            kd = gy.dates[gy.pre_end - 1].astype("datetime64[D]").astype(object)
            A = PG.group_graph(gy.X[:gy.pre_end], gy.sids, gy.issuers, gy.subs, gy.kind, sc, kd)
            yield gy, A


def out_dir() -> Path:
    p = report_dir() / "wf_peer"
    p.mkdir(parents=True, exist_ok=True)
    return p


def now() -> float:
    return time.perf_counter()
