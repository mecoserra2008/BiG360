"""P4 economic value of the conformal sets in the Backtest Lab engine (app.backtest._engine Python API, costs on,
deflated Sharpe + PBO). US mvp universe, test period 2011-01-01 .. 2026-10-20, USD (adj_close x official FX).

  (i)   breakout-on-exit: the h-session move leaves the 90% conformal band (marginal or joint-projection band, EWMA
        envelope, walk-forward calibrated) vs a naive N-day-high breakout; long only, entry next close.
  (ii)  stops: monthly 12-1 momentum (top 25) with band stops (lower edge of the 80/90/95% 20-session band) vs ATR
        stops (2 / 3 x ATR14) vs no stop.
  (iii) peer-group anomaly mean reversion: fade the member that broke away (|z_cond| >= thr on a day the group's
        joint score exceeded its 90% radius) vs a naive fade of any |own z| >= thr move; long-short, hold 5 / 10.
Output: data/reports/conformal/econ.json (+ per-config daily returns econ_returns.parquet).

    .venv/Scripts/python.exe scripts/conformal/econ.py [--radius aci|plain]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.backtest import _engine as E  # noqa: E402
from app.backtest import _panel as BP  # noqa: E402
from app.conformal import data as D  # noqa: E402
from app.conformal import envelope as V  # noqa: E402
from app.conformal import validation as W  # noqa: E402
from app.core.config import get_settings  # noqa: E402

T0 = date(2011, 1, 1)
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}


def band_arrays(p: W.Prep, mode: str) -> dict[str, np.ndarray]:
    """Walk-forward EWMA envelope: per origin t the 3-level half-widths of the marginal per-horizon bands (qm) and of
    the joint-set projections, (T, N, 4, 3) each; NaN outside test years."""
    T, N = len(p.dates), len(p.ids)
    hm = np.full((T, N, 4, 3), np.nan)
    hj = np.full((T, N, 4, 3), np.nan)
    s2 = V.ewma_var(p.a.r, p.a.E)
    Vh = V.horizon_V(s2, V.jump_var(p.a.r, p.a.E, s2), p.ke)
    Rj = V.nested_score(p.Y, 0.0, Vh)
    Rm = p.Y * p.Y / np.maximum(Vh, 1e-14)
    for y in range(2011, 2027):
        t0, t1 = W.year_bounds(p.dates, y)
        c0, _ = W.year_bounds(p.dates, y - 1)
        if t0 < 0 or c0 < 0:
            continue
        ev = p.ke[c0:t1] > 0
        pc = V.PoolCal(Rj[c0:t1], p.pool, ev, lag=20, window=500, min_n=200).run(t0 - c0, t1 - c0)
        src = pc.q_aci if mode == "aci" else pc.q
        tl = np.arange(t0 - c0, t1 - c0)
        for j in range(3):
            q = np.where(ev[tl], src[tl][:, p.pool, 1, j], src[tl][:, p.pool, 0, j])
            hj[t0:t1, :, :, j] = np.sqrt(np.maximum(q, 0)[..., None] * Vh[t0:t1])
        for k, h in enumerate(V.HORIZONS):
            evh = (p.ke[c0:t1] > 0) & (p.ke[c0:t1] <= h)
            pcm = V.PoolCal(Rm[c0:t1, :, k], p.pool, evh, lag=h, window=500, min_n=200).run(t0 - c0, t1 - c0)
            srcm = pcm.q_aci if mode == "aci" else pcm.q
            for j in range(3):
                q = np.where(evh[tl], srcm[tl][:, p.pool, 1, j], srcm[tl][:, p.pool, 0, j])
                hm[t0:t1, :, k, j] = np.sqrt(np.maximum(q, 0) * Vh[t0:t1, :, k])
    return {"marginal": hm, "joint": hj}


def events_frame(rows: list[tuple]) -> pl.DataFrame:
    cols = ["entity_id", "date", "direction", "strength", "stop_loss"]
    if not rows:
        return pl.DataFrame(schema={"entity_id": pl.Utf8, "date": pl.Date, "direction": pl.Int64, "strength": pl.Float64,
                                    "stop_loss": pl.Float64})
    return pl.DataFrame(rows, schema=cols, orient="row").with_columns(pl.col("date").cast(pl.Date))


def run_events(ev: pl.DataFrame, px: E.PricePanel, attrs: pl.DataFrame, hold: int, maxpos: int, short: bool = False) -> E.SimResult:
    spec = {"event": {"hold_days": hold, "max_positions": maxpos, "position_weight": 1.0 / maxpos, "entry_lag_days": 1,
                      "allow_short": short, "max_gross": 1.0}, "costs": COSTS}
    return E.simulate_events(ev, px, spec, attrs)


def stats(sim: E.SimResult, rf: np.ndarray | None) -> dict:
    d = [x for x in sim.dates]
    k = np.array([x >= T0 for x in d])
    ret = sim.ret[k]
    dd = [x for x, kk in zip(d, k, strict=True) if kk]
    r_rf = rf[sim.start_idx:][k] if rf is not None else None
    m = E.perf_metrics(ret, dd, rf=r_rf)
    tr = [t for t in sim.trades if t["entry_date"] >= T0]
    m["n_trades"] = len(tr)
    m["trade_hit_rate"] = float(np.mean([t["return"] > 0 for t in tr])) if tr else None
    m["mean_trade_ret"] = float(np.mean([t["return"] for t in tr])) if tr else None
    m["stops_hit"] = sum(t["exit_reason"] == "stop" for t in tr)
    m["cost_drag_ann"] = float(sim.cost[k].mean() * 252)
    return {k2: m.get(k2) for k2 in ("sharpe", "cagr", "vol", "max_drawdown", "n_trades", "trade_hit_rate",
                                      "mean_trade_ret", "stops_hit", "cost_drag_ann", "skew", "kurtosis")}, ret, dd


def family_overfit(rets: dict[str, np.ndarray], rf: np.ndarray | None = None) -> dict:
    names = list(rets)
    L = min(len(v) for v in rets.values())
    M = np.column_stack([rets[n][-L:] for n in names])
    srs = []
    for j in range(M.shape[1]):
        st = E.sharpe_stats(M[:, j])
        srs.append(st["sr"] if st["sr"] is not None else 0.0)
    sr_var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    out = {"pbo": E.pbo_cscv(M, 10).get("pbo"), "n_trials": len(names), "dsr": {}}
    for j, n in enumerate(names):
        st = E.sharpe_stats(M[:, j])
        if st["sr"] is None:
            continue
        ds = E.deflated_sharpe(st["sr"], st["n"], st["skew"], st["kurtosis"], len(names), sr_var)
        out["dsr"][n] = {"dsr": ds["dsr"], "psr": ds["psr"], "sr_ann": st["sr"] * math.sqrt(252)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius", default="aci")
    ap.add_argument("--peer-filter", default="ewma_lr")
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "conformal"
    logging.basicConfig(filename=logd / "econ.log", level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("econ")
    t00 = time.time()
    p = W.prepare("US", s)
    log.info("prepared US N=%d", len(p.ids))
    bands = band_arrays(p, a.radius)
    log.info("bands done (%.0fs)", time.time() - t00)
    # engine price panel (USD) + liquidity attributes
    con = D.lake(["prices_daily", "macro_factors"], s)
    try:
        daily = BP._bp_load_prices(con, p.ids, date(2009, 1, 1), date(2026, 10, 20), "USD")
        px = E.price_panel_from_long(daily.select("entity_id", "date", "px"))
        px.rf = BP.rf_daily(con, px.dates)
    finally:
        con.close()
    feat = BP._bp_price_features(daily, [], None)
    attrs = feat.select("entity_id", "date", pl.col("adv_usd_20d").alias("adv_usd"), "vol")
    dts = p.dates
    ids = p.ids
    lc, lh = p.a.lc, p.a.lh
    res: dict = {"radius_mode": a.radius, "universe": f"mvp_us ({len(ids)})", "period": f"{T0} .. 2026-10-20",
                 "costs": COSTS}
    rets: dict[str, dict[str, np.ndarray]] = {"i": {}, "ii": {}, "iii": {}}
    allret = {}

    def record(fam: str, name: str, ev: pl.DataFrame, hold: int, maxpos: int, short: bool = False) -> None:
        sim = run_events(ev, px, attrs, hold, maxpos, short)
        st, r, dd = stats(sim, px.rf)
        st["n_events"] = ev.height
        res.setdefault(fam, {})[name] = st
        rets[fam][name] = r
        allret[f"{fam}:{name}"] = (dd, r)
        log.info("%s %s %s", fam, name, st)

    # ------------------------------------------------------------ (i) breakouts
    t_ok = np.array([d >= np.datetime64(T0) for d in dts])
    for btype in ("marginal", "joint"):
        for k, h in ((1, 5), (3, 20)):
            hw = bands[btype][:, :, k, 1]                  # 90% band, horizon h, emitted at origin t - h
            mv = np.full_like(lc, np.nan)
            mv[h:] = lc[h:] - lc[:-h]
            lim = np.full_like(lc, np.nan)
            lim[h:] = hw[:-h]
            exc = (mv > lim) & np.isfinite(lim)
            fresh = exc & ~np.vstack([np.zeros((1, exc.shape[1]), bool), exc[:-1]])
            ii, jj = np.nonzero(fresh & t_ok[:, None])
            rows = [(ids[j], dts[i].astype(object), 1, float(mv[i, j] / lim[i, j]), None) for i, j in zip(ii, jj, strict=True)]
            ev = events_frame(rows)
            for hold in (10, 20):
                record("i", f"band_{btype}_h{h}_hold{hold}", ev, hold, 50)
    for N in (20, 55):
        hi = np.full_like(lh, np.nan)
        for t in range(N, len(dts)):
            hi[t] = np.nanmax(lh[t - N:t], axis=0)
        exc = lc > hi
        fresh = exc & ~np.vstack([np.zeros((1, exc.shape[1]), bool), exc[:-1]])
        ii, jj = np.nonzero(fresh & t_ok[:, None])
        ret_n = np.full_like(lc, np.nan)
        ret_n[N:] = lc[N:] - lc[:-N]
        rows = [(ids[j], dts[i].astype(object), 1, float(ret_n[i, j]), None) for i, j in zip(ii, jj, strict=True)]
        ev = events_frame(rows)
        for hold in (10, 20):
            record("i", f"naive_high{N}_hold{hold}", ev, hold, 50)

    # ------------------------------------------------------------ (ii) stops on 12-1 momentum
    months = dts.astype("datetime64[M]")
    first = np.nonzero(np.concatenate([[True], months[1:] != months[:-1]]) & t_ok)[0]
    mom = np.full_like(lc, np.nan)
    mom[252:] = lc[231:-21] - lc[:-252]
    base_rows = []
    for i in first:
        m = mom[i]
        ok = np.isfinite(m)
        if ok.sum() < 50:
            continue
        top = np.argsort(-np.where(ok, m, -np.inf))[:25]
        for j in top:
            base_rows.append((i, j))
    stop_sets = {"none": lambda i, j: None,
                 "atr2": lambda i, j: float(min(0.9, 2 * p.atr[i, j])),
                 "atr3": lambda i, j: float(min(0.9, 3 * p.atr[i, j]))}
    for lvl_i, lvl in enumerate((80, 90, 95)):
        stop_sets[f"band{lvl}"] = (lambda li: (lambda i, j: float(min(0.9, 1 - math.exp(-bands["marginal"][i, j, 3, li])))
                                               if np.isfinite(bands["marginal"][i, j, 3, li]) else None))(lvl_i)
    for nm, fn in stop_sets.items():
        rows = [(ids[j], dts[i].astype(object), 1, float(mom[i, j]), fn(i, j)) for i, j in base_rows]
        record("ii", f"mom_stop_{nm}", events_frame(rows), 21, 25)

    # ------------------------------------------------------------ (iii) peer-group break-away mean reversion
    sig_f = W.report_dir(s) / "wf_peer" / f"signals_US_{a.peer_filter}.parquet"
    if sig_f.exists():
        sg = pl.read_parquet(sig_f).filter(pl.col("date") >= T0)
        sg = sg.filter(pl.col("security_id").is_in(ids))
        s2 = V.ewma_var(p.a.r, p.a.E)
        z_own = p.a.r / np.sqrt(np.vstack([s2[:1], s2[:-1]]))
        for thr in (2.5, 3.0):
            a_ev = sg.filter((pl.col("score") > pl.col("q90")) & (pl.col("z_cond").abs() >= thr))
            a_ev = a_ev.sort(pl.col("z_cond").abs(), descending=True).unique(["group_id", "date"], keep="first")
            a_ev = a_ev.unique(["security_id", "date"], keep="first")
            ev = a_ev.select(pl.col("security_id").alias("entity_id"), pl.col("date").cast(pl.Date),
                             (-pl.col("z_cond").sign()).cast(pl.Int64).alias("direction"),
                             pl.col("z_cond").abs().alias("strength"), pl.lit(None, pl.Float64).alias("stop_loss"))
            ii, jj = np.nonzero((np.abs(z_own) >= thr) & t_ok[:, None] & np.isfinite(z_own))
            nv = events_frame([(ids[j], dts[i].astype(object), -int(np.sign(z_own[i, j])), float(abs(z_own[i, j])), None)
                               for i, j in zip(ii, jj, strict=True)])
            for hold in (5, 10):
                record("iii", f"peer_breakaway_z{thr}_hold{hold}", ev, hold, 40, short=True)
                record("iii", f"naive_fade_z{thr}_hold{hold}", nv, hold, 40, short=True)
    else:
        res["iii"] = {"note": f"missing {sig_f.name} (run scripts/conformal/wf_peers.py first)"}
    for fam in ("i", "ii", "iii"):
        if rets[fam]:
            res[f"overfit_{fam}"] = family_overfit(rets[fam])
    res["seconds"] = round(time.time() - t00, 1)
    out = W.report_dir(s)
    (out / "econ.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    frames = [pl.DataFrame({"config": k, "date": v[0], "ret": v[1]}) for k, v in allret.items()]
    if frames:
        pl.concat(frames).write_parquet(out / "econ_returns.parquet")
    log.info("econ finished (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
