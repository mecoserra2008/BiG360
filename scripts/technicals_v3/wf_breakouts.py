"""Technicals v3 #5: are volume-confirmed breakouts followed through more often? (docs/TECHNICALS_V3_VALIDATION.md)

Inputs: scratchpad/tech_v3_b15/wf/<region>/bo_c*.parquet (events, features, first-passage labels) and ct_c*.parquet
(geometry-matched random controls), written by wf_build.py. Sample: LIQUID names, events 2005 -> today with complete
labels, H = 10 / 20 sessions, k = 1 sigma_H.

1. Rates: P(follow-through) overall, confirmed vs unconfirmed (pre-registered rule relvol50 >= 1.5 and clv >= 0.5),
   by confirmation-score quintile; vs the geometry-matched random-control base rate.
   Effect sizes with month-block bootstrap CIs: raw confirmed - unconfirmed; geometry-adjusted (strata b quintile x
   vol-ratio tercile x lookback x direction x region, weights = confirmed events); event - matched control.
2. Calibrated model, purged walk-forward: 2-year test blocks 2008 -> 2026; training events < block start - 425
   days, isotonic calibration year [start - 365 d, start - 60 d); geometry-only and full logistic (+ HGB candidate).
   ECE (15 quantile bins), Brier, log-loss vs climatology and the geometry baseline; Brier difference full - geometry
   with a month-block CI; regime calibration gaps (Covid 2020, 2022, VIX > 25, bear market, region, direction,
   lookback, confirmed). Leakage: the full model refitted on permuted labels -> out-of-sample skill ~ 0.
Outputs: data/reports/technicals_v3/bo_summary.json, bo_oos.parquet, reliability_bo_h{10,20}.svg. State key b5_eval.

    .venv/Scripts/python.exe scripts/technicals_v3/wf_breakouts.py
"""

from __future__ import annotations

import gc
import os
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techprob import calib as K  # noqa: E402
from app.techv3 import bo_model as M  # noqa: E402
from app.techv3 import panel as P  # noqa: E402
from app.techv3 import state as ST  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v3_b15" / "wf"
REGIONS = tuple(os.environ.get("TV3_REGIONS", "US,EU,UK,JP").split(","))
HS = (10, 20)
START = date(2005, 1, 1)
BLOCKS = [(date(y, 1, 1), date(y + 2, 1, 1)) for y in range(2008, 2027, 2)]
COVID = (date(2020, 2, 15), date(2020, 6, 30))
MAX_TRAIN = 700_000
LOG = logging.getLogger("wf_breakouts")


def load() -> tuple[pl.DataFrame, pl.DataFrame]:
    ev, ct = [], []
    keep_e = ["date", "security_id", "sector_id", "N", "direction", "close", "level", "sigma_d", "relvol20", "relvol50", "clv",
              "cmf20", "obv_slope20", "obv_confirm", "d_illiq", "gap_z", "gap_beyond", "ret_z", "range_z", "trend200",
              "prior20_z", "vol_ratio", "confirmed", "confirm_score", "liquid",
              *[f"{p}_{h}" for h in HS for p in ("b", "ret", "target", "out", "tau", "complete")]]
    for rg in REGIONS:
        fe = sorted((WF / rg).glob("bo_c*.parquet"))
        fc = sorted((WF / rg).glob("ct_c*.parquet"))
        if not fe:
            continue
        e = pl.concat([pl.read_parquet(f, columns=keep_e) for f in fe]).filter(pl.col("liquid") & (pl.col("date") >= START))
        ev.append(e.with_columns(pl.lit(rg).alias("region")))
        c = pl.concat([pl.read_parquet(f) for f in fc]).filter(pl.col("liquid") & (pl.col("date") >= START))
        if "event_date" not in c.columns:          # older builds: map the event row index through the region calendar
            cal = np.load(WF / rg / "market.npz", allow_pickle=True)["dates"].astype("datetime64[D]")
            c = c.with_columns(pl.Series("event_date", cal[c["event_t"].to_numpy()]).cast(pl.Date))
        ct.append(c.with_columns(pl.lit(rg).alias("region")))
    E = pl.concat(ev, how="diagonal_relaxed").with_columns(pl.col("security_id").cast(pl.Categorical))
    C = pl.concat(ct, how="diagonal_relaxed").with_columns(pl.col("security_id").cast(pl.Categorical))
    return E, C


def rate_ci(y: np.ndarray, d: np.ndarray, B: int = 300) -> dict:
    blocks = K.month_blocks(d)
    r = K.block_bootstrap(lambda ix: float(y[ix].mean()), blocks, B=B, seed=1)
    return {"rate": float(y.mean()), "lo": r["lo"], "hi": r["hi"], "n": int(len(y))}


def diff_ci(ya: np.ndarray, da: np.ndarray, yb: np.ndarray, db: np.ndarray, B: int = 300) -> dict:
    """Difference of two rates with a joint month-block bootstrap (blocks resampled once for both groups)."""
    ma, mb = K.month_blocks(da), K.month_blocks(db)
    months = np.union1d(np.unique(ma), np.unique(mb))
    ia = {m: np.nonzero(ma == m)[0] for m in months}
    ib = {m: np.nonzero(mb == m)[0] for m in months}
    rng = np.random.default_rng(3)
    est = float(ya.mean() - yb.mean())
    vals = []
    for _ in range(B):
        pick = rng.choice(months, len(months))
        a = np.concatenate([ia[m] for m in pick])
        b = np.concatenate([ib[m] for m in pick])
        if len(a) and len(b):
            vals.append(ya[a].mean() - yb[b].mean())
    v = np.array(vals)
    return {"diff": est, "lo": float(np.quantile(v, 0.025)), "hi": float(np.quantile(v, 0.975)),
            "p_le_0": float((v <= 0).mean()), "n_a": int(len(ya)), "n_b": int(len(yb))}


def stratified_effect(df: pl.DataFrame, H: int, B: int = 300) -> dict:
    """Geometry-adjusted confirmed - unconfirmed difference: within strata (b quintile x vol-ratio tercile x N x dir x
    region) differences weighted by the confirmed counts; month-block bootstrap of the whole statistic."""
    d = df.with_columns(
        pl.col(f"b_{H}").qcut(5, labels=[str(i) for i in range(5)], allow_duplicates=True).alias("bq"),
        pl.col("vol_ratio").qcut(3, labels=["0", "1", "2"], allow_duplicates=True).alias("vq"),
        pl.Series("mb", K.month_blocks(df["date"].to_numpy().astype("datetime64[D]")))).with_columns(
        pl.concat_str(["bq", "vq", pl.col("N").cast(pl.Utf8), pl.col("direction").cast(pl.Utf8), "region"], separator="|").alias("st"))
    y = (d[f"out_{H}"] == 0).cast(pl.Float64).to_numpy()
    cf = d["confirmed"].to_numpy()
    st = d["st"].to_numpy()
    mb = d["mb"].to_numpy()

    def stat(ix: np.ndarray) -> float:
        g = pl.DataFrame({"st": st[ix], "c": cf[ix], "y": y[ix]}).group_by("st", "c").agg(pl.col("y").mean(), pl.len())
        w = g.pivot(on="c", index="st", values=["y", "len"]).drop_nulls()
        if w.is_empty():
            return float("nan")
        cols = w.columns
        yt = [c for c in cols if c.startswith("y_") and c.endswith("true")][0]
        yf = [c for c in cols if c.startswith("y_") and c.endswith("false")][0]
        nt = [c for c in cols if c.startswith("len_") and c.endswith("true")][0]
        return float(((w[yt] - w[yf]) * w[nt]).sum() / w[nt].sum())

    est = stat(np.arange(len(y)))
    months = np.unique(mb)
    idx = {m: np.nonzero(mb == m)[0] for m in months}
    rng = np.random.default_rng(5)
    vals = []
    for _ in range(B):
        pick = rng.choice(months, len(months))
        v = stat(np.concatenate([idx[m] for m in pick]))
        if np.isfinite(v):
            vals.append(v)
    v = np.array(vals)
    return {"diff": est, "lo": float(np.quantile(v, 0.025)), "hi": float(np.quantile(v, 0.975)), "B": len(v)}


def matched_control(E: pl.DataFrame, C: pl.DataFrame, H: int, mask_expr: pl.Expr | None = None) -> dict:
    """Event success - the geometry-matched control success (each control carries its event's t index, so the pair
    is joined on (security, N, direction, event date))."""
    e = E if mask_expr is None else E.filter(mask_expr)
    e = e.filter(pl.col(f"complete_{H}"))
    c = C.filter(pl.col(f"complete_{H}"))
    ye, de = (e[f"out_{H}"] == 0).cast(pl.Float64).to_numpy(), e["date"].to_numpy().astype("datetime64[D]")
    if mask_expr is not None:
        # controls of the masked events: join through the event row key
        key = e.select("security_id", "N", "direction", "date").with_columns(pl.lit(True).alias("_k"))
        c = c.join(key.rename({"date": "event_date"}), on=["security_id", "N", "direction", "event_date"], how="inner")
    yc, dc = (c[f"out_{H}"] == 0).cast(pl.Float64).to_numpy(), c["date"].to_numpy().astype("datetime64[D]")
    if not len(yc):
        return {}
    return {"event": rate_ci(ye, de), "control": rate_ci(yc, dc), "diff": diff_ci(ye, de, yc, dc)}


def reliability_svg(rel: list[dict], path: Path, title: str) -> None:
    W, Hh, pad = 360, 360, 40
    mx = max(max(r["p_mean"] for r in rel), max(r["y_mean"] for r in rel)) * 1.1 if rel else 1
    sx = lambda v: pad + (W - 2 * pad) * v / mx  # noqa: E731
    sy = lambda v: Hh - pad - (Hh - 2 * pad) * v / mx  # noqa: E731
    pts = " ".join(f"{sx(r['p_mean']):.1f},{sy(r['y_mean']):.1f}" for r in rel)
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" font-family="sans-serif" font-size="11">'
           f'<rect width="100%" height="100%" fill="white"/><text x="{pad}" y="20">{title}</text>'
           f'<line x1="{sx(0)}" y1="{sy(0)}" x2="{sx(mx)}" y2="{sy(mx)}" stroke="#999" stroke-dasharray="4"/>'
           f'<polyline points="{pts}" fill="none" stroke="#2563eb" stroke-width="2"/>'
           + "".join(f'<circle cx="{sx(r["p_mean"]):.1f}" cy="{sy(r["y_mean"]):.1f}" r="3" fill="#2563eb"/>' for r in rel)
           + f'<text x="{W / 2 - 40}" y="{Hh - 8}">predicted</text><text x="4" y="{Hh / 2}">realised</text></svg>')
    path.write_text(svg, encoding="utf-8")


def walk_forward(E: pl.DataFrame, H: int, vix_hi: set, bear: set, log: logging.Logger) -> tuple[pl.DataFrame, dict]:
    df = E.filter(pl.col(f"complete_{H}")).sort("date")
    D = M.design(df, H)
    Xg_all = M.matrix(D, M.GEOMETRY)
    Xf_all = M.matrix(D, M.FULL)
    y = (df[f"out_{H}"] == 0).cast(pl.Float64).to_numpy()
    d = df["date"].to_numpy().astype("datetime64[D]")
    preds = {k: np.full(len(y), np.nan) for k in ("geo", "full", "hgb", "clim", "perm")}
    coefs = []
    for b0, b1 in BLOCKS:
        s0, s1 = np.datetime64(b0, "D"), np.datetime64(b1, "D")
        te = (d >= s0) & (d < s1)
        if not te.any():
            continue
        tr = d < s0 - np.timedelta64(425, "D")
        ca = (d >= s0 - np.timedelta64(365, "D")) & (d < s0 - np.timedelta64(60, "D"))
        itr = np.nonzero(tr)[0]
        itr = itr[M.subsample(len(itr), MAX_TRAIN, seed=b0.year)]
        ica = np.nonzero(ca)[0]
        if len(itr) < 5000:
            continue
        mg = M.fit(Xg_all[itr], y[itr], Xg_all[ica], y[ica], M.GEOMETRY)
        mf = M.fit(Xf_all[itr], y[itr], Xf_all[ica], y[ica], M.FULL)
        preds["geo"][te] = mg.predict(Xg_all[te])
        preds["full"][te] = mf.predict(Xf_all[te])
        preds["clim"][te] = float(y[itr].mean())
        coefs.append({"block": f"{b0.year}-{b1.year - 1}", **M.coefficients(mf)})
        hsub = itr[M.subsample(len(itr), 300_000, seed=7)]
        mh = M.fit(Xf_all[hsub], y[hsub], Xf_all[ica], y[ica], M.FULL, kind="hgb")
        preds["hgb"][te] = mh.predict(Xf_all[te])
        # leakage: permuted labels (across month blocks too)
        rng = np.random.default_rng(b0.year)
        yp = y[itr][rng.permutation(len(itr))]
        ycp = y[ica][rng.permutation(len(ica))]
        mp = M.fit(Xf_all[itr], yp, Xf_all[ica], ycp, M.FULL)
        preds["perm"][te] = mp.predict(Xf_all[te])
        log.info("H%d block %s: train %d cal %d test %d", H, b0, len(itr), len(ica), int(te.sum()))
    ok = np.isfinite(preds["full"])
    oos = df.filter(pl.Series(ok)).select("date", "security_id", "region", "N", "direction", "confirmed", "confirm_score",
                                          f"b_{H}", f"ret_{H}", "vol_ratio").with_columns(
        pl.Series("y", y[ok]), *[pl.Series(f"p_{k}", v[ok]) for k, v in preds.items()], pl.lit(H).alias("H"))
    res: dict = {"n_oos": int(ok.sum()), "coefficients": coefs}
    yy = y[ok]
    dd = d[ok]
    for k in ("clim", "geo", "full", "hgb", "perm"):
        res[k] = K.summary(preds[k][ok], yy)
    blocks = K.month_blocks(dd)
    pf, pg, pc_ = preds["full"][ok], preds["geo"][ok], preds["clim"][ok]
    res["brier_full_minus_geo"] = K.block_bootstrap(lambda ix: float(np.mean((pf[ix] - yy[ix]) ** 2) - np.mean((pg[ix] - yy[ix]) ** 2)),
                                                    blocks, B=300)
    res["brier_full_minus_clim"] = K.block_bootstrap(lambda ix: float(np.mean((pf[ix] - yy[ix]) ** 2) - np.mean((pc_[ix] - yy[ix]) ** 2)),
                                                     blocks, B=300)
    res["brier_geo_minus_clim"] = K.block_bootstrap(lambda ix: float(np.mean((pg[ix] - yy[ix]) ** 2) - np.mean((pc_[ix] - yy[ix]) ** 2)),
                                                    blocks, B=300)
    res["reliability_full"] = K.reliability(pf, yy)
    # regimes
    dts = oos["date"]
    regs = {"covid_2020": (dts >= COVID[0]) & (dts <= COVID[1]), "year_2020": dts.dt.year() == 2020,
            "year_2022": dts.dt.year() == 2022, "vix_gt_25": dts.is_in(list(vix_hi)),
            "bear_market": pl.Series([(r, x) in bear for r, x in zip(oos["region"].to_list(), dts.to_list(), strict=True)])}
    for rg in REGIONS:
        regs[f"region_{rg}"] = oos["region"] == rg
    for dn, nm in ((1, "up"), (-1, "down")):
        regs[f"dir_{nm}"] = oos["direction"] == dn
    for N in (20, 55):
        regs[f"N_{N}"] = oos["N"] == N
    regs["confirmed"] = oos["confirmed"]
    regs["unconfirmed"] = ~oos["confirmed"]
    res["regimes"] = {}
    for nm, m in regs.items():
        mm = m.to_numpy().astype(bool)
        if mm.sum() >= 500:
            res["regimes"][nm] = {"n": int(mm.sum()), "gap": float(yy[mm].mean() - pf[mm].mean()), "ece": K.ece(pf[mm], yy[mm]),
                                  "rate": float(yy[mm].mean())}
    big = {k: v for k, v in res["regimes"].items() if v["n"] >= 2000}
    res["max_regime_gap"] = max((abs(v["gap"]) for v in big.values()), default=None)
    res["worst_regime"] = max(big, key=lambda k: abs(big[k]["gap"])) if big else None
    # predicted-probability buckets (decision value): realised follow-through by full-model quintile
    q = np.quantile(pf, [0.2, 0.4, 0.6, 0.8])
    kq = np.searchsorted(q, pf)
    res["by_pred_quintile"] = [{"q": int(i + 1), "n": int((kq == i).sum()), "pred": float(pf[kq == i].mean()),
                                "realised": float(yy[kq == i].mean()), "ret_mean": float(np.nanmean(oos[f"ret_{H}"].to_numpy()[kq == i]))}
                               for i in range(5)]
    return oos, res


def main() -> None:
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v3" / "wf_breakouts.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    t00 = time.time()
    rd = s.data_dir / "reports" / "technicals_v3"
    rd.mkdir(parents=True, exist_ok=True)
    ST.update(s, b5_eval={"status": "running"})
    E, C = load()
    LOG.info("events %d controls %d (%.0fs)", E.height, C.height, time.time() - t00)
    vix = P.vix_daily(date(2004, 1, 1), date.today(), s)
    vix_hi = set(vix.filter(pl.col("vix") > 25)["date"].to_list())
    from app.techv3 import factors as F  # noqa: PLC0415

    bear = set()
    for rg in REGIONS:
        f = WF / rg / "market.npz"
        if f.exists():
            z = np.load(f, allow_pickle=True)
            lv = F.index_level(z["rm"])
            r = lv - F.shift(lv, 252)
            for x in pl.Series(z["dates"][np.isfinite(r) & (r < 0)].astype("datetime64[D]")).cast(pl.Date).to_list():
                bear.add((rg, x))
    out: dict = {"events": E.height, "controls": C.height, "rule": "confirmed = relvol50 >= 1.5 and clv >= 0.5", "H": {}}
    oos_all = []
    for H in HS:
        e = E.filter(pl.col(f"complete_{H}"))
        y = (e[f"out_{H}"] == 0).cast(pl.Float64).to_numpy()
        d = e["date"].to_numpy().astype("datetime64[D]")
        cf = e["confirmed"].to_numpy()
        res: dict = {"n": e.height, "base": rate_ci(y, d), "fail_rate": float((e[f"out_{H}"] == 1).mean()),
                     "neither_rate": float((e[f"out_{H}"] == 2).mean()), "confirmed_share": float(cf.mean())}
        res["confirmed"] = rate_ci(y[cf], d[cf])
        res["unconfirmed"] = rate_ci(y[~cf], d[~cf])
        res["confirmed_minus_unconfirmed"] = diff_ci(y[cf], d[cf], y[~cf], d[~cf])
        res["geometry_adjusted"] = stratified_effect(e, H)
        res["b_median"] = {"confirmed": float(np.median(e.filter(pl.col("confirmed"))[f"b_{H}"].to_numpy())),
                           "unconfirmed": float(np.median(e.filter(~pl.col("confirmed"))[f"b_{H}"].to_numpy()))}
        res["vs_control"] = {"all": matched_control(E, C, H), "confirmed": matched_control(E, C, H, pl.col("confirmed")),
                             "unconfirmed": matched_control(E, C, H, ~pl.col("confirmed"))}
        sc = e["confirm_score"].to_numpy()
        qs = np.quantile(sc[np.isfinite(sc)], [0.2, 0.4, 0.6, 0.8])
        kq = np.searchsorted(qs, sc)
        res["by_score_quintile"] = [{"q": int(i + 1), **rate_ci(y[kq == i], d[kq == i], B=100)} for i in range(5)]
        res["by_segment"] = {}
        for nm, m in {"N20": e["N"] == 20, "N55": e["N"] == 55, "up": e["direction"] == 1, "down": e["direction"] == -1,
                      **{rg: e["region"] == rg for rg in REGIONS}}.items():
            mm = m.to_numpy()
            res["by_segment"][nm] = {"all": float(y[mm].mean()), "confirmed": float(y[mm & cf].mean()) if (mm & cf).any() else None,
                                     "unconfirmed": float(y[mm & ~cf].mean()), "n": int(mm.sum()),
                                     "diff": diff_ci(y[mm & cf], d[mm & cf], y[mm & ~cf], d[mm & ~cf], B=150) if (mm & cf).sum() > 500 else None}
        res["by_N_dir"] = {f"{N}:{dn}": float(y[(e["N"] == N).to_numpy() & (e["direction"] == dn).to_numpy()].mean())
                           for N in (20, 55) for dn in (1, -1)}
        # mean H-session return (dir-signed, log) - the economic counterpart of the label
        rr = e[f"ret_{H}"].to_numpy()
        res["mean_ret"] = {"confirmed": float(np.nanmean(rr[cf])), "unconfirmed": float(np.nanmean(rr[~cf]))}
        LOG.info("H%d rates done (%.0fs)", H, time.time() - t00)
        oos, wf = walk_forward(E, H, vix_hi, bear, LOG)
        res["walk_forward"] = wf
        reliability_svg(wf["reliability_full"], rd / f"reliability_bo_h{H}.svg", f"P(follow-through) H={H}: reliability (OOS)")
        oos_all.append(oos)
        out["H"][str(H)] = res
        gc.collect()
    pl.concat(oos_all, how="diagonal_relaxed").write_parquet(rd / "bo_oos.parquet")
    out["seconds"] = round(time.time() - t00)
    (rd / "bo_summary.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    ST.update(s, b5_eval={"status": "done", "file": str(rd / "bo_summary.json")})
    LOG.info("done (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
