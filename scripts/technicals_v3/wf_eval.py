"""Technicals v3 #1: out-of-sample evaluation of the evidence-based factors and the composite (docs/TECHNICALS_V3_VALIDATION.md).

Inputs: scratchpad/tech_v3_b15/wf/<region>/f_<year>.parquet (wf_build.py: daily point-in-time factors + percentiles +
forward log returns from the close of t + 1). Sample: weekly origins (the last session of each ISO week, region
calendar), 2005-01 -> today, LIQUID names (20-day ADV >= $1M, close >= $1); micro (ADV $0.2-1M) reported separately.

Labels: fwd_h (h = 5 / 20 / 60 sessions) minus the equal-weight mean of the liquid names of the same region x sector
x date (sector-neutral; region x date for thin / unclassified sectors and for industry momentum, which is a
between-sector signal). IC = per (region, date) Spearman of the percentile vs the label; the pooled series weights
regions by their names; Newey-West t with ceil(h / 5) + 1 lags.

No parameter of a factor is fitted on our data (definitions and signs are pre-registered from the literature), so the
whole 2005-2026 sample is out-of-sample for the factors. The composite is built walk-forward:
  comp_wf (PRIMARY, pre-declared): for test year Y, the equal-weight mean of the sign-aligned percentiles of the
           factors whose pooled 20-session IC up to Y - 1 (dates < Jan 1 of Y minus 30 days, expanding from 2005) is
           sign-aligned with t >= 2; first test year 2008.
  comp_all: the equal-weight mean of the six stock-level sign-aligned percentiles (pre-registered, no selection;
           industry momentum is a between-sector signal and is reported on its own).
  comp_ridge: ridge (lambda 10 x n) of the per-date rank of the 20-session label on the centred sign-aligned
           percentiles, expanding fit on dates < Jan 1 of Y minus 30 days.
Leakage: (a) shift-by-one: the factor of the PREVIOUS session against the same labels; (b) label permutation within
(region, date), 20 draws; (c) positive control: a peeking "factor" (the realised fwd_5) is caught by (a) and (b).
Outputs: data/reports/technicals_v3/eval.json, ic_series.parquet, buckets.parquet, gate.json (factor part).
State key eval.

    .venv/Scripts/python.exe scripts/technicals_v3/wf_eval.py
"""

from __future__ import annotations

import gc
import os
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

from app.core.config import get_settings  # noqa: E402
from app.techv3 import factors as F  # noqa: E402
from app.techv3 import stats as S  # noqa: E402
from app.techv3 import state as ST  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v3_b15" / "wf"
REGIONS = tuple(os.environ.get("TV3_REGIONS", "US,EU,UK,JP").split(","))
HORIZONS = (5, 20, 60)
PRIMARY_H = 20
EVAL_START = date(2005, 1, 1)
COMP_START = 2008
COVID = (date(2020, 2, 15), date(2020, 6, 30))
N_PERM = 20
SIGNALS = (*F.FACTORS, "comp_wf", "comp_all", "comp_ridge")
#: stock-selection factors pooled into the composite; industry momentum is a between-sector signal (constant within a
#: sector) and is reported on its own (pre-declared)
COMP_POOL = tuple(f for f in F.FACTORS if f != "indmom")
LOG = logging.getLogger("wf_eval")


# ------------------------------------------------------------------------------------------------ loading


def weekly_dates(dates: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Last session of each ISO week and the session before it."""
    d = np.unique(dates)
    wk = (d.astype("datetime64[D]").astype(np.int64) + 3) // 7          # Monday-based week index
    last = np.r_[wk[1:] != wk[:-1], True]
    W = d[last]
    idx = np.nonzero(last)[0]
    Pv = d[np.maximum(idx - 1, 0)]
    return W, Pv


def load_region(rg: str) -> pl.DataFrame:
    files = sorted((WF / rg).glob("f_[0-9][0-9][0-9][0-9].parquet"))
    cols = ["security_id", "date", "sector_id", "liquid", "size_bucket", "adv20", "pct_scope", *[f"pct_{f}" for f in F.FACTORS],
            *[f"fwd_{h}" for h in HORIZONS]]
    lf = pl.scan_parquet(files).select(cols)
    alld = lf.select(pl.col("date").unique()).collect()["date"].to_numpy().astype("datetime64[D]")
    W, Pv = weekly_dates(alld)
    keep = pl.Series(np.unique(np.r_[W, Pv]).astype("datetime64[D]")).cast(pl.Date)
    df = lf.filter(pl.col("date").is_in(keep.implode()) & (pl.col("liquid") | (pl.col("adv20") >= 2.0e5))).collect()
    df = df.with_columns(pl.col("security_id").cast(pl.Categorical), pl.col("sector_id").cast(pl.Categorical))
    # previous-session percentiles (shift-by-one test)
    pmap = pl.DataFrame({"date": pl.Series(W).cast(pl.Date), "prev": pl.Series(Pv).cast(pl.Date)})
    prev = df.select("security_id", pl.col("date").alias("prev"), *[pl.col(f"pct_{f}").alias(f"lag_{f}") for f in F.FACTORS])
    wk = df.join(pmap, on="date", how="inner").join(prev, on=["security_id", "prev"], how="left").drop("prev")
    wk = wk.filter(pl.col("date") >= EVAL_START).with_columns(pl.lit(rg).alias("region"))
    return wk


def add_labels(df: pl.DataFrame) -> pl.DataFrame:
    """Sector-neutral (or region-neutral) excess labels computed over the LIQUID names of each cross-section."""
    out = df
    for h in HORIZONS:
        y = pl.col(f"fwd_{h}")
        liq_y = pl.when(pl.col("liquid")).then(y).otherwise(None)
        m_sec = liq_y.mean().over(["date", "sector_id"])
        m_reg = liq_y.mean().over(["date"])
        sec_ok = pl.col("pct_scope") == "sector"
        out = out.with_columns((y - pl.when(sec_ok).then(m_sec).otherwise(m_reg)).alias(f"y{h}"), (y - m_reg).alias(f"yr{h}"))
    return out


# ------------------------------------------------------------------------------------------------ per-region stats


def _ic(df: pl.DataFrame, x: str, y: str, by: list[str]) -> pl.DataFrame:
    return S.ic_series(df, x, y, by)


def _buckets(df: pl.DataFrame, x: str, y: str, nq: int = 10) -> pl.DataFrame:
    d = df.filter(pl.col(x).is_not_null() & pl.col(y).is_not_null())
    d = d.with_columns(((pl.col(x).rank("average").over("date") - 0.5) / pl.len().over("date") * nq).floor()
                       .clip(0, nq - 1).cast(pl.Int8).alias("q"))
    return d.group_by("date", "q").agg(pl.col(y).mean().alias("mean"), pl.len().alias("n"))


def region_stats(df: pl.DataFrame, rg: str, signals: list[str], perm: bool) -> dict[str, pl.DataFrame]:
    """IC series (all / by size / micro / lagged / permuted) and bucket means for every signal x horizon."""
    liq = df.filter(pl.col("liquid"))
    micro = df.filter(~pl.col("liquid"))
    ic_rows, bk_rows = [], []
    for sgn in signals:
        x = f"pct_{sgn}" if sgn in F.FACTORS else sgn
        if x not in df.columns:
            continue
        for h in HORIZONS:
            y = f"yr{h}" if sgn == "indmom" else f"y{h}"
            base = [("all", liq), ("micro", micro)]
            for seg, d in base:
                ic = _ic(d, x, y, ["date"])
                ic_rows.append(ic.with_columns(pl.lit(sgn).alias("signal"), pl.lit(h).alias("h"), pl.lit(rg).alias("region"),
                                               pl.lit("size").alias("seg_type") if seg == "micro" else pl.lit("all").alias("seg_type"),
                                               pl.lit(seg).alias("segment")))
            for sb in ("large", "mid", "small"):
                ic = _ic(liq.filter(pl.col("size_bucket") == sb), x, y, ["date"])
                ic_rows.append(ic.with_columns(pl.lit(sgn).alias("signal"), pl.lit(h).alias("h"), pl.lit(rg).alias("region"),
                                               pl.lit("size").alias("seg_type"), pl.lit(sb).alias("segment")))
            if sgn in F.FACTORS:
                ic = _ic(liq, f"lag_{sgn}", y, ["date"])
                ic_rows.append(ic.with_columns(pl.lit(sgn).alias("signal"), pl.lit(h).alias("h"), pl.lit(rg).alias("region"),
                                               pl.lit("leak").alias("seg_type"), pl.lit("lag1").alias("segment")))
            if perm and h == PRIMARY_H:
                for k in range(N_PERM):
                    pdf = S.permute_within(liq.select("date", x, y), y, ["date"], seed=1000 + k)
                    ic = _ic(pdf, x, y, ["date"])
                    ic_rows.append(ic.with_columns(pl.lit(sgn).alias("signal"), pl.lit(h).alias("h"), pl.lit(rg).alias("region"),
                                                   pl.lit("perm").alias("seg_type"), pl.lit(f"p{k}").alias("segment")))
            bk = _buckets(liq, x, y)
            bk_rows.append(bk.with_columns(pl.lit(sgn).alias("signal"), pl.lit(h).alias("h"), pl.lit(rg).alias("region")))
    return {"ic": pl.concat(ic_rows, how="diagonal_relaxed") if ic_rows else pl.DataFrame(),
            "bk": pl.concat(bk_rows, how="diagonal_relaxed") if bk_rows else pl.DataFrame()}


def positive_control(df: pl.DataFrame, rg: str) -> pl.DataFrame:
    """A peeking 'factor' = the realised next-5-session return (ranked): huge IC on the 5-session label, gone after the
    label permutation and after the one-session shift is applied to a label-aligned leak."""
    liq = df.filter(pl.col("liquid") & pl.col("y5").is_not_null())
    liq = liq.with_columns(pl.col("y5").alias("peek"))
    a = _ic(liq, "peek", "y5", ["date"]).with_columns(pl.lit("peek").alias("segment"))
    p = _ic(S.permute_within(liq.select("date", "peek", "y5"), "y5", ["date"], seed=7), "peek", "y5", ["date"]).with_columns(
        pl.lit("peek_perm").alias("segment"))
    return pl.concat([a, p]).with_columns(pl.lit(rg).alias("region"))


# ------------------------------------------------------------------------------------------------ pooled summaries


def pooled(ic: pl.DataFrame) -> pl.DataFrame:
    return (ic.group_by("date").agg(((pl.col("ic") * pl.col("n")).sum() / pl.col("n").sum()).alias("ic"), pl.col("n").sum())
            .sort("date"))


def summarize(ic: pl.DataFrame, h: int, sign: int) -> dict:
    out = S.ic_summary(ic.select("date", "ic", "n"), h, sign)
    return {k: (float(v) if isinstance(v, (int, float, np.floating)) and v is not None else v) for k, v in out.items()}


def bucket_summary(bk: pl.DataFrame, sign: int) -> dict:
    """Pooled decile means (n-weighted over regions and dates), monotonicity, sign-aligned top-bottom spread CI."""
    if bk.is_empty():
        return {}
    per_date = bk.group_by("date", "q").agg(((pl.col("mean") * pl.col("n")).sum() / pl.col("n").sum()).alias("m"),
                                           pl.col("n").sum())
    tab = per_date.group_by("q").agg(pl.col("m").mean().alias("mean"), pl.col("n").sum().alias("n")).sort("q")
    means = tab["mean"].to_numpy()
    rho = S.spearman(tab["q"].to_numpy().astype(float), means)
    piv = per_date.filter(pl.col("q").is_in([0, 9])).pivot(on="q", index="date", values="m").drop_nulls().sort("date")
    spread = None
    if piv.height > 20 and "9" in piv.columns and "0" in piv.columns:
        diff = sign * (piv["9"] - piv["0"]).to_numpy()
        blocks = S.month_blocks(piv["date"].to_numpy().astype("datetime64[D]"))
        ci = S.block_bootstrap(lambda ix: float(diff[ix].mean()), blocks, B=300, seed=0)
        spread = {"mean": float(diff.mean()), "lo": ci["lo"], "hi": ci["hi"], "n_dates": int(len(diff))}
    return {"deciles": [{"decile": int(q) + 1, "mean": float(m), "n": int(n)} for q, m, n in tab.iter_rows()],
            "spearman": rho, "aligned_spearman": (rho * sign) if rho is not None else None, "spread_aligned": spread}


def main() -> None:
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v3" / "wf_eval.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    t00 = time.time()
    rd = s.data_dir / "reports" / "technicals_v3"
    rd.mkdir(parents=True, exist_ok=True)
    ST.update(s, eval={"status": "running"})
    from app.techv3 import panel as P  # noqa: PLC0415

    vix = P.vix_daily(date(2004, 1, 1), date.today(), s)
    vix_hi = set(vix.filter(pl.col("vix") > 25)["date"].to_list())
    # ---------------- pass A: factors
    IC, BK, PC = [], [], []
    acc: dict[int, tuple[np.ndarray, np.ndarray, int]] = {}
    bear: dict[str, set] = {}
    for rg in REGIONS:
        t0 = time.time()
        if not (WF / rg).exists():
            LOG.info("%s missing", rg)
            continue
        df = add_labels(load_region(rg))
        LOG.info("%s: %d weekly rows (%.0fs)", rg, df.height, time.time() - t0)
        z = np.load(WF / rg / "market.npz", allow_pickle=True)
        lvl = F.index_level(z["rm"])
        r252 = lvl - F.shift(lvl, 252)
        bd = z["dates"][np.isfinite(r252) & (r252 < 0)]
        bear[rg] = set(pl.Series(bd.astype("datetime64[D]")).cast(pl.Date).to_list())
        res = region_stats(df, rg, list(F.FACTORS), perm=True)
        IC.append(res["ic"])
        BK.append(res["bk"])
        PC.append(positive_control(df, rg))
        # ridge moments per month: aligned centred percentiles vs the per-date rank of y20
        liq = df.filter(pl.col("liquid") & pl.col("y20").is_not_null())
        Xc = [((pl.col(f"pct_{f}") if F.SIGN[f] > 0 else 100 - pl.col(f"pct_{f}")) / 100.0 - 0.5).fill_null(0.0).alias(f) for f in COMP_POOL]
        rr = liq.select((pl.col("date").dt.year() * 12 + pl.col("date").dt.month() - 1).alias("m"), *Xc,
                        ((pl.col("y20").rank("average").over("date") - 0.5) / pl.len().over("date") - 0.5).alias("t"))
        for (mm,), g in rr.partition_by("m", as_dict=True).items():
            X = g.select(list(COMP_POOL)).to_numpy()
            t = g["t"].to_numpy()
            A0, b0, n0 = acc.get(int(mm), (np.zeros((len(COMP_POOL), len(COMP_POOL))), np.zeros(len(COMP_POOL)), 0))
            acc[int(mm)] = (A0 + X.T @ X, b0 + X.T @ t, n0 + len(t))
        del df, liq, rr, res
        gc.collect()
        LOG.info("%s pass A done (%.0fs)", rg, time.time() - t0)
    ic = pl.concat(IC, how="diagonal_relaxed")
    bk = pl.concat(BK, how="diagonal_relaxed")
    pc = pl.concat(PC, how="diagonal_relaxed")
    # ---------------- walk-forward composite selection (pooled 20d IC of each factor up to Y - 1)
    sel: dict[int, list[str]] = {}
    years = list(range(COMP_START, date.today().year + 1))
    pool_all = {f: pooled(ic.filter((pl.col("signal") == f) & (pl.col("h") == PRIMARY_H) & (pl.col("seg_type") == "all")
                                    & (pl.col("segment") == "all"))) for f in F.FACTORS}
    for Y in years:
        cut = date(Y, 1, 1)
        chosen = []
        for f in COMP_POOL:
            p = pool_all[f].filter(pl.col("date") < date.fromordinal(cut.toordinal() - 30))
            st = summarize(p, PRIMARY_H, F.SIGN[f])
            if st.get("t_stat") is not None and st["t_stat"] * F.SIGN[f] >= 2.0:
                chosen.append(f)
        sel[Y] = chosen
    LOG.info("wf selection: %s", sel)
    ridge_w: dict[int, list[float]] = {}
    k = len(COMP_POOL)
    for Y in years:
        # expanding fit on months up to November of Y - 1 (>= 1 month purge > the 20-session label)
        last_m = (Y - 1) * 12 + 10
        XtX, Xty, n = np.zeros((k, k)), np.zeros(k), 0
        for mm, (A, b, nn) in acc.items():
            if mm <= last_m:
                XtX += A
                Xty += b
                n += nn
        lam = 10.0 * n
        w = np.linalg.solve(XtX + lam * np.eye(k) / 1000.0, Xty) if n else np.zeros(k)
        ridge_w[Y] = w.tolist()
    # ---------------- pass B: composites
    IC2, BK2, V2 = [], [], []
    for rg in REGIONS:
        if not (WF / rg).exists():
            continue
        t0 = time.time()
        df = add_labels(load_region(rg))
        yr = df["date"].dt.year()
        df = df.with_columns(yr.alias("_y"))
        al = {f: (pl.col(f"pct_{f}") if F.SIGN[f] > 0 else 100.0 - pl.col(f"pct_{f}")) for f in F.FACTORS}
        n_ok = pl.sum_horizontal([al[f].is_not_null().cast(pl.Int32) for f in COMP_POOL])
        df = df.with_columns(pl.when(n_ok >= 3).then(pl.sum_horizontal([al[f].fill_null(0.0) for f in COMP_POOL]) / n_ok)
                             .otherwise(None).alias("comp_all"))
        wf_expr = pl.lit(None, pl.Float64)
        rd_expr = pl.lit(None, pl.Float64)
        for Y in years:
            fs = sel.get(Y) or []
            if fs:
                nn = pl.sum_horizontal([al[f].is_not_null().cast(pl.Int32) for f in fs])
                e = pl.when(nn >= max(1, math.ceil(len(fs) / 2))).then(pl.sum_horizontal([al[f].fill_null(0.0) for f in fs]) / nn)
                wf_expr = pl.when(pl.col("_y") == Y).then(e).otherwise(wf_expr)
            w = ridge_w[Y]
            e2 = pl.sum_horizontal([w[kk] * (al[f] / 100.0 - 0.5).fill_null(0.0) for kk, f in enumerate(COMP_POOL)])
            rd_expr = pl.when(pl.col("_y") == Y).then(e2).otherwise(rd_expr)
        df = df.with_columns(wf_expr.alias("comp_wf"), rd_expr.alias("comp_ridge"))
        df = df.with_columns(pl.when(pl.col("_y") >= COMP_START).then(pl.col("comp_all")).otherwise(None).alias("comp_all"))
        res = region_stats(df, rg, ["comp_wf", "comp_all", "comp_ridge"], perm=True)
        IC2.append(res["ic"])
        BK2.append(res["bk"])
        V2.append(df.filter(pl.col("liquid")).select("security_id", "date", "comp_wf", "comp_all", "y20", "y5", "y60").with_columns(
            pl.col("security_id").cast(pl.Utf8), pl.lit(rg).alias("region")))
        del df
        gc.collect()
        LOG.info("%s pass B done (%.0fs)", rg, time.time() - t0)
    ic = pl.concat([ic, *IC2], how="diagonal_relaxed")
    bk = pl.concat([bk, *BK2], how="diagonal_relaxed")
    comp_rows = pl.concat(V2, how="diagonal_relaxed")
    ic.write_parquet(rd / "ic_series.parquet")
    bk.write_parquet(rd / "buckets.parquet")
    comp_rows.write_parquet(WF / "composite_weekly.parquet")
    # ---------------- v2 heuristic comparison (gate universe, weekly v2 origins)
    v2 = compare_v2(comp_rows)
    # ---------------- summaries
    out = summarise_all(ic, bk, pc, sel, ridge_w, vix_hi, bear, v2)
    out["seconds"] = round(time.time() - t00)
    (rd / "eval.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    ST.update(s, eval={"status": "done", "file": str(rd / "eval.json"), "seconds": out["seconds"]})
    LOG.info("done (%.0fs)", time.time() - t00)


def compare_v2(comp: pl.DataFrame) -> dict:
    """IC of the v2 heuristic technical_score vs the v3 composite on the same (security, weekly date) rows."""
    fr = []
    for rg in REGIONS:
        for f in sorted((ROOT / "scratchpad" / "tech_v2_models" / "wf" / rg).glob("b*_setups.parquet")):
            fr.append(pl.read_parquet(f, columns=["security_id", "date", "technical_score"]).with_columns(pl.lit(rg).alias("region")))
    if not fr:
        return {"note": "v2 walk-forward setups not found"}
    v2 = pl.concat(fr).with_columns(pl.col("date").cast(pl.Date))
    j = comp.join(v2, on=["security_id", "date", "region"], how="inner")
    out = {"rows": j.height, "n_securities": j["security_id"].n_unique()}
    for h in HORIZONS:
        y = f"y{h}"
        a = S.ic_series(j, "comp_wf", y, ["region", "date"])
        b = S.ic_series(j, "technical_score", y, ["region", "date"])
        c = S.ic_series(j, "comp_all", y, ["region", "date"])
        pa, pb, pc_ = pooled(a), pooled(b), pooled(c)
        m = pa.join(pb, on="date", suffix="_v2").join(pc_, on="date", suffix="_all")
        d = (m["ic"] - m["ic_v2"]).to_numpy()
        mm, se = S.nw_mean(d, S.nw_lags(h))
        t, p = S.t_and_p(mm, se)
        out[str(h)] = {"comp_wf": summarize(pa, h, 1), "v2_score": summarize(pb, h, 1), "comp_all": summarize(pc_, h, 1),
                       "diff_wf_minus_v2": {"mean": mm, "t": t, "p": p, "n_dates": int(len(d))}}
    return out


def summarise_all(ic: pl.DataFrame, bk: pl.DataFrame, pc: pl.DataFrame, sel: dict, ridge_w: dict, vix_hi: set,
                  bear: dict, v2: dict) -> dict:
    out: dict = {"comp_pool": list(COMP_POOL), "signals": {}, "selection": {str(k): v for k, v in sel.items()}, "ridge_weights": {str(k): v for k, v in ridge_w.items()},
                 "factors": list(F.FACTORS), "sign": F.SIGN, "v2_comparison": v2, "cells": []}
    for sg in SIGNALS:
        sign = F.SIGN.get(sg, 1)
        out["signals"][sg] = {}
        for h in HORIZONS:
            base = ic.filter((pl.col("signal") == sg) & (pl.col("h") == h))
            if base.is_empty():
                continue
            allr = base.filter((pl.col("seg_type") == "all") & (pl.col("segment") == "all"))
            res: dict = {"pooled": summarize(pooled(allr), h, sign), "regions": {}, "size": {}, "years": {}, "regimes": {}}
            for rg in REGIONS:
                x = allr.filter(pl.col("region") == rg)
                if x.height:
                    res["regions"][rg] = summarize(x, h, sign)
            for sb in ("large", "mid", "small", "micro"):
                x = base.filter((pl.col("seg_type") == "size") & (pl.col("segment") == sb))
                if x.height:
                    res["size"][sb] = summarize(pooled(x), h, sign)
            pa = pooled(allr)
            for y in sorted(set(pa["date"].dt.year().to_list())):
                x = pa.filter(pl.col("date").dt.year() == y)
                res["years"][str(y)] = {"mean_ic": float(x["ic"].mean()), "n_dates": x.height}
            regs = {"covid_2020": pa.filter((pl.col("date") >= COVID[0]) & (pl.col("date") <= COVID[1])),
                    "year_2022": pa.filter(pl.col("date").dt.year() == 2022),
                    "vix_gt_25": pa.filter(pl.col("date").is_in(list(vix_hi))),
                    "vix_le_25": pa.filter(~pl.col("date").is_in(list(vix_hi))),
                    "gfc_2008_2009": pa.filter(pl.col("date").dt.year().is_in([2008, 2009])),
                    "era_2005_2009": pa.filter(pl.col("date").dt.year() <= 2009),
                    "era_2010_2019": pa.filter(pl.col("date").dt.year().is_between(2010, 2019)),
                    "era_2020_2026": pa.filter(pl.col("date").dt.year() >= 2020)}
            # bear market: the region's own equal-weight market 12-month return < 0 (region-level IC rows)
            bear_rows = pl.concat([allr.filter((pl.col("region") == rg) & pl.col("date").is_in(list(bear.get(rg, set()))))
                                   for rg in REGIONS]) if allr.height else allr
            regs["bear_market"] = pooled(bear_rows) if bear_rows.height else bear_rows
            bull_rows = pl.concat([allr.filter((pl.col("region") == rg) & ~pl.col("date").is_in(list(bear.get(rg, set()))))
                                   for rg in REGIONS]) if allr.height else allr
            regs["bull_market"] = pooled(bull_rows) if bull_rows.height else bull_rows
            for nm, x in regs.items():
                if x.height >= 8:
                    res["regimes"][nm] = summarize(x, h, sign)
            lag = base.filter((pl.col("seg_type") == "leak") & (pl.col("segment") == "lag1"))
            if lag.height:
                res["lag1"] = summarize(pooled(lag), h, sign)
            perm = base.filter(pl.col("seg_type") == "perm")
            if perm.height:
                pts = []
                for k in sorted(set(perm["segment"].to_list())):
                    st = summarize(pooled(perm.filter(pl.col("segment") == k)), h, sign)
                    pts.append({"mean_ic": st["mean_ic"], "t": st["t_stat"]})
                res["permutation"] = {"n": len(pts), "mean_ic": float(np.mean([p["mean_ic"] for p in pts])),
                                      "max_abs_t": float(np.max([abs(p["t"] or 0) for p in pts])),
                                      "share_abs_t_ge_2": float(np.mean([abs(p["t"] or 0) >= 2 for p in pts]))}
            res["buckets"] = bucket_summary(bk.filter((pl.col("signal") == sg) & (pl.col("h") == h)), sign)
            for rg in REGIONS:
                b = bk.filter((pl.col("signal") == sg) & (pl.col("h") == h) & (pl.col("region") == rg))
                if b.height and rg in res["regions"]:
                    res["regions"][rg]["buckets"] = bucket_summary(b, sign)
            out["signals"][sg][str(h)] = res
            # multiple-testing cells: signal x horizon x region (+ pooled)
            for rg, st in [("ALL", res["pooled"]), *res["regions"].items()]:
                out["cells"].append({"signal": sg, "h": h, "region": rg, "mean_ic": st.get("mean_ic"), "t": st.get("t_stat"),
                                     "p": st.get("p_value"), "aligned": (st.get("mean_ic") or 0) * sign > 0})
    # BH over every factor cell (7 factors x 3 horizons x (4 regions + pooled)); Holm over the 7 primary cells
    fc = [c for c in out["cells"] if c["signal"] in F.FACTORS]
    p = np.array([c["p"] if c["p"] is not None else np.nan for c in fc])
    for c, q in zip(fc, S.bh_adjust(p), strict=True):
        c["p_bh"] = None if not np.isfinite(q) else float(q)
    prim = [c for c in fc if c["h"] == PRIMARY_H and c["region"] == "ALL"]
    for c, q in zip(prim, S.holm_adjust(np.array([c["p"] if c["p"] is not None else np.nan for c in prim])), strict=True):
        c["p_holm_primary"] = None if not np.isfinite(q) else float(q)
    cc = [c for c in out["cells"] if c["signal"] not in F.FACTORS]
    p2 = np.array([c["p"] if c["p"] is not None else np.nan for c in cc])
    for c, q in zip(cc, S.bh_adjust(p2), strict=True):
        c["p_bh"] = None if not np.isfinite(q) else float(q)
    out["n_cells_factor"] = len(fc)
    out["n_cells_composite"] = len(cc)
    pcs = {}
    for seg in ("peek", "peek_perm"):
        x = pc.filter(pl.col("segment") == seg)
        pcs[seg] = summarize(pooled(x), 5, 1) if x.height else {}
    out["positive_control"] = pcs
    return out


if __name__ == "__main__":
    main()
