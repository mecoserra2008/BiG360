"""T6 / T3: purged walk-forward of the zone hold model P(hold | touch) and its edge vs random control bands.

Inputs: scratchpad/tech_v2_models/wf/<region>/b*_zones.parquet (scripts/technicals_v2/wf_build.py); rows with a
touch within 20 sessions and a resolved / complete label.
Test blocks of two years 2010-11 ... 2024-25, 2026. Per block starting at Y0:
  calibration set = origins in [Y0 - 365 d, Y0 - 60 d); training set = origins < Y0 - 425 d (label windows are
  <= 30 sessions, so the 60-day gaps purge every overlap); training subsampled to <= 800k rows per model.
  Real-zone models (logit, hgb) on all features; control models (logit, hgb) on the band features of the random
  control bands. Baselines on real zones: climatology (training hold rate), the old zone strength mapped by
  isotonic regression, and the control model (band features only).
  Edge buckets: quintiles of the calibration-period predicted edge (p_real - p_ctrl), applied to the block.
Outputs: data/reports/technicals_v2/t3_oos.parquet, t3_summary.json. State key t3.

    .venv/Scripts/python.exe scripts/technicals_v2/wf_t3.py [--max-train 800000]
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techprob import barriers as B  # noqa: E402
from app.techprob import calib as K  # noqa: E402
from app.techprob import state as ST  # noqa: E402
from app.techprob import zone_model as M  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v2_models" / "wf"
REGIONS = ("US", "EU", "UK", "JP")
BLOCKS = [(2010, 2012), (2012, 2014), (2014, 2016), (2016, 2018), (2018, 2020), (2020, 2022), (2022, 2024), (2024, 2026), (2026, 2027)]
COVID = (np.datetime64("2020-02-15"), np.datetime64("2020-06-30"))
META = ["date", "is_control", "hold", "region", "vix_high", "vol_regime", "earn_in20", "side", "in_zone", "strength",
        "security_id"]


def load() -> tuple[dict[str, np.ndarray], np.ndarray]:
    cols = list(dict.fromkeys(META + M.REAL))
    mats, metas = [], []
    for rg in REGIONS:
        for f in sorted((WF / rg).glob("b*_zones.parquet")):
            df = pl.read_parquet(f, columns=[c for c in cols if c != "region_code"] + ["touched", "complete"])
            df = df.filter(pl.col("touched") & pl.col("complete")).with_columns(
                pl.col("region").map_elements(B.region_code, return_dtype=pl.Int64).alias("region_code"))
            mats.append(M.matrix(df, M.REAL))
            metas.append(df.select(META))
            del df
    meta = pl.concat(metas)
    X = np.vstack(mats)
    del mats
    gc.collect()
    out = {c: meta[c].to_numpy() for c in META if c not in ("date", "region", "security_id")}
    out["date"] = meta["date"].to_numpy().astype("datetime64[D]")
    out["region"] = meta["region"].to_numpy()
    out["security_id"] = meta["security_id"].to_numpy()
    return out, X


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-train", type=int, default=800_000)
    a = ap.parse_args()
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v2" / "wf_t3.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_t3")
    t00 = time.time()
    ST.update(s, t3={"status": "running", "step": "walk-forward"})
    meta, X = load()
    d = meta["date"]
    ctrl = meta["is_control"].astype(bool)
    y = meta["hold"].astype(np.float64)
    nb = len(M.BAND)
    log.info("rows %d (real %d, control %d), features %d (%.0fs)", len(y), (~ctrl).sum(), ctrl.sum(), X.shape[1], time.time() - t00)
    oos = []
    for bi, (y0, y1) in enumerate(BLOCKS):
        s0 = np.datetime64(f"{y0}-01-01")
        s1 = np.datetime64(f"{y1}-01-01")
        te = (d >= s0) & (d < s1)
        ca = (d >= s0 - np.timedelta64(365, "D")) & (d < s0 - np.timedelta64(60, "D"))
        tr = d < s0 - np.timedelta64(425, "D")
        if not te.any() or tr.sum() < 20000:
            continue
        res = {}
        for nm, msk_tr, msk_ca, cols, Xs in (("real", tr & ~ctrl, ca & ~ctrl, M.REAL, X), ("ctrl", tr & ctrl, ca & ctrl, M.BAND, X[:, :nb])):
            itr = np.nonzero(msk_tr)[0]
            itr = itr[M.subsample(len(itr), a.max_train, seed=bi)]
            ica = np.nonzero(msk_ca)[0]
            for kind in ("logit", "hgb"):
                t0 = time.time()
                res[(nm, kind)] = M.fit(kind, Xs[itr], y[itr], Xs[ica], y[ica], cols, seed=bi)
                log.info("block %d-%d %s %s: train %d cal %d (%.0fs)", y0, y1 - 1, nm, kind, len(itr), len(ica), time.time() - t0)
        # strength baseline: isotonic of hold on the old strength (train + cal, real zones)
        from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415

        itc = np.nonzero((tr | ca) & ~ctrl)[0]
        iso_s = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(meta["strength"][itc].astype(float), y[itc])
        base_rate = float(y[tr & ~ctrl].mean())
        for part, msk in (("real", te & ~ctrl), ("ctrl", te & ctrl)):
            ix = np.nonzero(msk)[0]
            if not ix.size:
                continue
            fr = {"idx": ix, "part": np.full(ix.size, part), "block": np.full(ix.size, y0)}
            for kind in ("logit", "hgb"):
                fr[f"p_real_{kind}"] = res[("real", kind)].predict(X[ix]) if part == "real" else np.full(ix.size, np.nan)
                fr[f"p_ctrl_{kind}"] = res[("ctrl", kind)].predict(X[ix, :nb])
            fr["p_clim"] = np.full(ix.size, base_rate)
            fr["p_strength"] = iso_s.predict(meta["strength"][ix].astype(float)) if part == "real" else np.full(ix.size, np.nan)
            # edge buckets from the calibration period
            ic = np.nonzero(ca & ~ctrl)[0]
            for kind in ("logit", "hgb"):
                e_cal = res[("real", kind)].predict(X[ic]) - res[("ctrl", kind)].predict(X[ic, :nb])
                edges = M.bucket_edges(e_cal)
                e_te = fr[f"p_real_{kind}"] - fr[f"p_ctrl_{kind}"]
                fr[f"bucket_{kind}"] = M.bucket_of(e_te, edges) if part == "real" else np.full(ix.size, -1)
            oos.append(pl.DataFrame({k: (np.asarray(v, dtype=np.float64) if k.startswith("p_") else v) for k, v in fr.items()}))
        log.info("block %d-%d done (%.0fs)", y0, y1 - 1, time.time() - t00)
        del res
        gc.collect()
    P = pl.concat(oos, how="vertical_relaxed")
    ix = P["idx"].to_numpy()
    P = P.with_columns(pl.Series("date", d[ix]), pl.Series("y", y[ix]), pl.Series("region", meta["region"][ix]),
                       pl.Series("vix_high", meta["vix_high"][ix]), pl.Series("vol_regime", meta["vol_regime"][ix]),
                       pl.Series("earn_in20", meta["earn_in20"][ix]), pl.Series("side", meta["side"][ix]),
                       pl.Series("in_zone", meta["in_zone"][ix]), pl.Series("security_id", meta["security_id"][ix]))
    rd = s.data_dir / "reports" / "technicals_v2"
    rd.mkdir(parents=True, exist_ok=True)
    P.drop("idx").write_parquet(rd / "t3_oos.parquet")
    summ = summarize(P)
    summ["seconds"] = round(time.time() - t00)
    summ["rows"] = {"real": int((~ctrl).sum()), "control": int(ctrl.sum())}
    (rd / "t3_summary.json").write_text(json.dumps(summ, indent=1, default=str), encoding="utf-8")
    ST.update(s, t3={"status": "walk_forward_done", "chosen": summ.get("chosen"), "summary_file": str(rd / "t3_summary.json")})
    log.info("done (%.0fs)", time.time() - t00)


def regimes(P: pl.DataFrame) -> dict[str, np.ndarray]:
    d = P["date"].to_numpy().astype("datetime64[D]")
    yr = d.astype("datetime64[Y]").astype(int) + 1970
    out = {"all": np.ones(P.height, bool), "covid_2020": (d >= COVID[0]) & (d <= COVID[1]), "year_2020": yr == 2020,
           "year_2022": yr == 2022, "vix_gt_25": P["vix_high"].to_numpy() > 0.5, "earnings_window": P["earn_in20"].to_numpy() > 0.5,
           "support": P["side"].to_numpy() > 0, "resistance": P["side"].to_numpy() < 0, "in_zone": P["in_zone"].to_numpy() > 0.5}
    for v, nm in enumerate(B.VOL_REGIMES):
        out[f"vol_{nm}"] = P["vol_regime"].to_numpy() == v
    for rg in REGIONS:
        out[f"region_{rg}"] = (P["region"] == rg).to_numpy()
    return out


def summarize(P: pl.DataFrame) -> dict:
    R = P.filter(pl.col("part") == "real")
    C = P.filter(pl.col("part") == "ctrl")
    yr, yc = R["y"].to_numpy(), C["y"].to_numpy()
    out: dict = {"methods": {}, "control_self": {}, "regimes": {}, "reliability": {}, "edge": {}, "buckets": {}}
    for nm, col in (("real_logit", "p_real_logit"), ("real_hgb", "p_real_hgb"), ("ctrl_model_logit", "p_ctrl_logit"),
                    ("ctrl_model_hgb", "p_ctrl_hgb"), ("climatology", "p_clim"), ("strength_isotonic", "p_strength")):
        p = R[col].to_numpy()
        out["methods"][nm] = K.summary(p, yr)
        out["reliability"][nm] = K.reliability(p, yr, 15)
    for kind in ("logit", "hgb"):
        out["control_self"][kind] = K.summary(C[f"p_ctrl_{kind}"].to_numpy(), yc)
    chosen = min(("logit", "hgb"), key=lambda k: out["methods"][f"real_{k}"]["logloss"])
    out["chosen"] = chosen
    rg = regimes(R)
    for kind in ("logit", "hgb"):
        p = R[f"p_real_{kind}"].to_numpy()
        for nm, m in rg.items():
            if m.sum() >= 500:
                out["regimes"].setdefault(kind, {})[nm] = {"n": int(m.sum()), "gap": K.gap(p[m], yr[m]), "ece": K.ece(p[m], yr[m])}
    blocks_r = K.month_blocks(R["date"].to_numpy())
    blocks_c = K.month_blocks(C["date"].to_numpy())
    for kind in ("logit", "hgb"):
        pc = R[f"p_ctrl_{kind}"].to_numpy()
        pr = R[f"p_real_{kind}"].to_numpy()
        dd = yr - pc
        out["edge"][kind] = {
            "realised_minus_ctrl_model": K.block_bootstrap(lambda ix, x=dd: float(x[ix].mean()), blocks_r, B=400),
            "pred_edge_mean": float((pr - pc).mean()),
            "hold_rate_real": float(yr.mean()), "hold_rate_ctrl": float(yc.mean()),
        }
        # raw real vs control rate difference (different feature mix): month-paired bootstrap
        mr = {m: (yr[blocks_r == m].sum(), (blocks_r == m).sum()) for m in np.unique(blocks_r)}
        mc = {m: (yc[blocks_c == m].sum(), (blocks_c == m).sum()) for m in np.unique(blocks_c)}
        months = np.array(sorted(set(mr) & set(mc)))
        hr = np.array([mr[m][0] for m in months]); nr = np.array([mr[m][1] for m in months])
        hc = np.array([mc[m][0] for m in months]); nc = np.array([mc[m][1] for m in months])
        out["edge"][kind]["raw_real_minus_ctrl_rate"] = K.block_bootstrap(
            lambda ix: float(hr[ix].sum() / nr[ix].sum() - hc[ix].sum() / nc[ix].sum()), np.arange(len(months)), B=400)
        bk = R[f"bucket_{kind}"].to_numpy()
        tab = []
        for b in range(M.N_BUCKETS):
            m = bk == b
            if m.sum() < 200:
                continue
            bs = K.block_bootstrap(lambda ix, x=dd[m]: float(x[ix].mean()), blocks_r[m], B=300)
            tab.append({"bucket": b, "n": int(m.sum()), "pred_edge": float((pr - pc)[m].mean()), "realised_edge": bs["est"],
                        "ci_lo": bs["lo"], "ci_hi": bs["hi"], "hold_rate": float(yr[m].mean()), "p_real": float(pr[m].mean()),
                        "p_ctrl": float(pc[m].mean()),
                        "significant": bool(bs["lo"] is not None and (bs["lo"] > 0 or bs["hi"] < 0))})
        out["buckets"][kind] = tab
    # Brier / log-loss differences vs baselines (chosen model), block bootstrap
    p = R[f"p_real_{chosen}"].to_numpy()
    for base in ("p_clim", "p_strength", f"p_ctrl_{chosen}"):
        q = R[base].to_numpy()
        diff = (p - yr) ** 2 - (q - yr) ** 2
        out.setdefault("brier_diff", {})[base] = K.block_bootstrap(lambda ix, x=diff: float(x[ix].mean()), blocks_r, B=300)
    return out


if __name__ == "__main__":
    main()
