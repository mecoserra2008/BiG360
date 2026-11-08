"""T6 / T2: walk-forward reliability of the two-barrier setup probabilities (docs/TECHNICALS_V2_VALIDATION.md).

Inputs: scratchpad/tech_v2_models/wf/<region>/b*_{counts.npz, setups.parquet} (scripts/technicals_v2/wf_build.py).
Per test year Y in 2010..2026:
  grid    barriers.fit_surface on all origins of quarters < Q(Y) - 1 (one-quarter purge: 60-session paths of the
          last training origins end before Jan 1 of Y), pooled region x vol regime x earnings window, shrinkage
          m = 500, monotone smoothing; evaluated at each OOS setup's standardised barrier distances;
  gbm     candidate: XGBoost multiclass (neither / up / down / tie) on the setups of years <= Y-2 (+ purge), features
          (log a, log b, vol regime, earnings window, region, VIX>25, direction, trend / structure sub-scores),
          isotonic calibration of P(win) / P(loss) / P(neither) on year Y-1 (purged), renormalised;
  bm      driftless Brownian two-barrier formula at (a, b) (continuous monitoring);
  naive   'R:R only': P(win) = 1 / (1 + R:R), P(loss) = 1 - P(win), P(neither) = 0.
Outputs: data/reports/technicals_v2/t2_oos.parquet (one row per OOS setup x horizon), t2_summary.json.
State: data/state/technicals_v2.json key t2.

    .venv/Scripts/python.exe scripts/technicals_v2/wf_t2.py [--no-gbm]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
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

WF = ROOT / "scratchpad" / "tech_v2_models" / "wf"
REGIONS = ("US", "EU", "UK", "JP")
YEARS = list(range(2010, 2027))
COVID = (np.datetime64("2020-02-15"), np.datetime64("2020-06-30"))
METHODS = ("grid", "gbm", "bm", "naive")
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}


def report_dir() -> Path:
    p = get_settings().data_dir / "reports" / "technicals_v2"
    p.mkdir(parents=True, exist_ok=True)
    return p


def load_counts() -> dict[str, B.GridCounts]:
    out = {}
    for rg in REGIONS:
        files = sorted((WF / rg).glob("b*_counts.npz"))
        if not files:
            continue
        gc = None
        for f in files:
            z = np.load(f)
            if gc is None:
                gc = B.GridCounts(rg, z["C"].astype(np.float64), z["N"].astype(np.float64))
            else:
                gc.C += z["C"]
                gc.N += z["N"]
        out[rg] = gc
    return out


def load_setups() -> pl.DataFrame:
    fr = []
    for rg in REGIONS:
        for f in sorted((WF / rg).glob("b*_setups.parquet")):
            fr.append(pl.read_parquet(f))
    return pl.concat(fr, how="diagonal_relaxed") if fr else pl.DataFrame()


def long_frame(S: pl.DataFrame) -> pl.DataFrame:
    """One row per setup x horizon with a complete outcome."""
    parts = []
    base = ["security_id", "date", "region", "sector_id", "direction", "x_up", "x_dn", "rr_close", "vol_regime", "vix_high",
            "sigma_d", "adv_usd", "score_trend", "score_structure", "technical_score", "close", "entry", "invalidation",
            "target1"]
    S = S.filter(pl.col("direction") != 0)
    for H in B.HORIZONS:
        f = S.select(*base, pl.lit(H).alias("H"), pl.col(f"v_{H}").alias("v"), pl.col(f"earn_{H}").alias("earn"),
                     pl.col(f"out_{H}").alias("out"), pl.col(f"z_{H}").alias("z"), pl.col(f"tau_{H}").alias("tau"))
        parts.append(f.filter(pl.col("out") >= 0))
    L = pl.concat(parts)
    sig = pl.col("v").sqrt()
    return L.with_columns((pl.col("x_up") / sig).alias("a"), (pl.col("x_dn") / sig).alias("b"),
                          pl.col("date").dt.year().alias("year"))


def dir_map(direction: np.ndarray, up: np.ndarray, dn: np.ndarray, tie: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    long = direction > 0
    pw = np.where(long, up, dn)
    pl_ = np.where(long, dn, up) + tie
    pn = np.clip(1.0 - pw - pl_, 0.0, 1.0)
    return pw, pl_, pn


def gbm_features(df: pl.DataFrame) -> np.ndarray:
    return np.column_stack([
        np.log(np.clip(df["a"].to_numpy(), 1e-3, None)), np.log(np.clip(df["b"].to_numpy(), 1e-3, None)),
        df["vol_regime"].to_numpy().astype(float), df["earn"].cast(pl.Float64).to_numpy(),
        np.array([B.region_code(r) for r in df["region"].to_list()], dtype=float), df["vix_high"].cast(pl.Float64).to_numpy(),
        df["direction"].to_numpy().astype(float), df["score_trend"].fill_null(50).to_numpy(),
        df["score_structure"].fill_null(50).to_numpy()])


def gbm_fit_predict(train: pl.DataFrame, cal: pl.DataFrame, test: pl.DataFrame) -> np.ndarray | None:
    import xgboost as xgb  # noqa: PLC0415
    from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415

    if train.height < 5000 or cal.height < 1000 or test.height == 0:
        return None
    if train.height > 1_000_000:
        train = train.sample(n=1_000_000, seed=0)
    m = xgb.XGBClassifier(objective="multi:softprob", num_class=4, n_estimators=250, max_depth=4, learning_rate=0.08,
                          subsample=0.8, colsample_bytree=0.9, min_child_weight=20, tree_method="hist", n_jobs=4,
                          eval_metric="mlogloss", verbosity=0)
    m.fit(gbm_features(train), train["out"].to_numpy())

    def mapped(df: pl.DataFrame) -> np.ndarray:
        P = m.predict_proba(gbm_features(df))
        return np.column_stack(dir_map(df["direction"].to_numpy(), P[:, 1], P[:, 2], P[:, 3]))

    Pc, Pt = mapped(cal), mapped(test)
    yc = cal["out"].to_numpy()
    dc = cal["direction"].to_numpy()
    Yc = np.column_stack([np.where(dc > 0, yc == 1, yc == 2), np.where(dc > 0, (yc == 2) | (yc == 3), (yc == 1) | (yc == 3)), yc == 0])
    out = np.zeros_like(Pt)
    for j in range(3):
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(Pc[:, j], Yc[:, j].astype(float))
        out[:, j] = iso.predict(Pt[:, j])
    s = out.sum(1, keepdims=True)
    return out / np.where(s > 0, s, 1.0)


def multiclass_scores(P: np.ndarray, Y: np.ndarray) -> dict[str, float]:
    Pc = np.clip(P, K.EPS, 1.0)
    Pc = Pc / Pc.sum(1, keepdims=True)
    return {"brier3": float(np.mean(((P - Y) ** 2).sum(1))), "logloss3": float(-np.mean(np.log((Pc * Y).sum(1))))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gbm", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v2" / "wf_t2.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_t2")
    t00 = time.time()
    ST.update(s, t2={"status": "running", "step": "walk-forward"})
    counts = load_counts()
    S = load_setups()
    L = long_frame(S)
    log.info("counts %s; setups %d; setup x horizon rows %d", list(counts), S.height, L.height)
    del S
    preds = []
    for Y in YEARS:
        te = L.filter(pl.col("year") == Y)
        if te.is_empty():
            continue
        qy = int(B.quarter_index(np.array([np.datetime64(f"{Y}-01-01")]))[0])
        surf = B.fit_surface(counts, 0, qy - B.PURGE_QUARTERS)
        rc = np.array([B.REGIONS.index(r) if r in B.REGIONS and r in counts else len(B.REGIONS) for r in te["region"].to_list()])
        cols = {}
        P_grid = np.zeros((te.height, 4))
        for H in B.HORIZONS:
            m = (te["H"] == H).to_numpy()
            if not m.any():
                continue
            sub = te.filter(pl.Series(m))
            P_grid[m] = surf.eval_many(rc[m], sub["vol_regime"].to_numpy(), sub["earn"].to_numpy(), H,
                                       sub["a"].to_numpy(), sub["b"].to_numpy())
        d = te["direction"].to_numpy()
        cols["grid"] = dir_map(d, P_grid[:, 0], P_grid[:, 1], P_grid[:, 2])
        mtm = P_grid[:, 3]
        pu, pd = B.bm_two_barrier(te["a"].to_numpy(), te["b"].to_numpy())
        cols["bm"] = dir_map(d, pu, pd, np.zeros_like(pu))
        pw = B.naive_rr(te["rr_close"].to_numpy())
        cols["naive"] = (pw, 1 - pw, np.zeros_like(pw))
        if not a.no_gbm:
            cut_tr = np.datetime64(f"{Y - 1}-01-01") - np.timedelta64(100, "D")
            cut_cal = np.datetime64(f"{Y}-01-01") - np.timedelta64(100, "D")
            tr = L.filter(pl.col("date") < cut_tr)
            ca = L.filter((pl.col("date") >= np.datetime64(f"{Y - 1}-01-01")) & (pl.col("date") < cut_cal))
            G = gbm_fit_predict(tr, ca, te)
            if G is not None:
                cols["gbm"] = (G[:, 0], G[:, 1], G[:, 2])
        out = te.select("security_id", "date", "region", "H", "direction", "a", "b", "rr_close", "vol_regime", "vix_high",
                        "earn", "out", "z", "tau", "sigma_d", "adv_usd", "x_up", "x_dn", "technical_score")
        for mth, (w, l_, n_) in cols.items():
            out = out.with_columns(pl.Series(f"pw_{mth}", w), pl.Series(f"pl_{mth}", l_), pl.Series(f"pn_{mth}", n_))
        out = out.with_columns(pl.Series("mtm_z_grid", mtm))
        preds.append(out)
        log.info("year %d: %d rows, methods %s (%.0fs)", Y, out.height, list(cols), time.time() - t00)
    P = pl.concat(preds, how="diagonal_relaxed")
    # expected R (grid) with Backtest Lab costs
    risk = np.where(P["direction"].to_numpy() > 0, P["x_dn"].to_numpy(), P["x_up"].to_numpy())
    cost = B.round_trip_cost_vec(P["adv_usd"].cast(pl.Float64).fill_null(np.nan).to_numpy(), P["sigma_d"].to_numpy(),
                                 P["direction"].to_numpy() < 0, P["H"].to_numpy() / 2.0, COSTS)
    cost_r = cost / np.maximum(risk, 1e-6)
    rr = P["rr_close"].to_numpy()
    sigH = P["x_up"].to_numpy() / np.maximum(P["a"].to_numpy(), 1e-9)          # sqrt(V(H))
    mtm_r = P["mtm_z_grid"].to_numpy() * sigH * P["direction"].to_numpy() / np.maximum(risk, 1e-6)
    er = P["pw_grid"].to_numpy() * rr - P["pl_grid"].to_numpy() - cost_r
    er_mtm = er + P["pn_grid"].to_numpy() * mtm_r
    out_ = P["out"].to_numpy()
    dr = P["direction"].to_numpy()
    yw = np.where(dr > 0, out_ == 1, out_ == 2)
    yl = np.where(dr > 0, (out_ == 2) | (out_ == 3), (out_ == 1) | (out_ == 3))
    real_r = np.where(yw, rr, np.where(yl, -1.0, 0.0)) - cost_r
    zr = P["z"].fill_null(0.0).to_numpy() * dr / np.maximum(risk, 1e-6)
    real_r_mtm = np.where(yw, rr, np.where(yl, -1.0, zr)) - cost_r
    P = P.with_columns(pl.Series("cost_r", cost_r), pl.Series("exp_r", er), pl.Series("exp_r_mtm", er_mtm),
                       pl.Series("real_r", real_r), pl.Series("real_r_mtm", real_r_mtm), pl.Series("y_win", yw),
                       pl.Series("y_loss", yl), pl.Series("y_nei", out_ == 0))
    rd = report_dir()
    P.write_parquet(rd / "t2_oos.parquet")
    summ = summarize(P)
    summ["seconds"] = round(time.time() - t00)
    (rd / "t2_summary.json").write_text(json.dumps(summ, indent=1, default=str), encoding="utf-8")
    ST.update(s, t2={"status": "walk_forward_done", "rows": P.height, "summary_file": str(rd / "t2_summary.json")})
    log.info("done %.0fs", time.time() - t00)


def regimes(P: pl.DataFrame) -> dict[str, np.ndarray]:
    d = P["date"].to_numpy().astype("datetime64[D]")
    yr = P["date"].dt.year().to_numpy()
    out = {"all": np.ones(P.height, bool), "covid_2020": (d >= COVID[0]) & (d <= COVID[1]), "year_2020": yr == 2020,
           "year_2022": yr == 2022, "vix_gt_25": P["vix_high"].cast(pl.Boolean).to_numpy(),
           "earnings_window": P["earn"].cast(pl.Boolean).to_numpy(), "no_earnings": ~P["earn"].cast(pl.Boolean).to_numpy(),
           "long": P["direction"].to_numpy() > 0, "short": P["direction"].to_numpy() < 0}
    for v, nm in enumerate(B.VOL_REGIMES):
        out[f"vol_{nm}"] = P["vol_regime"].to_numpy() == v
    for rg in REGIONS:
        out[f"region_{rg}"] = (P["region"] == rg).to_numpy()
    return out


def summarize(P: pl.DataFrame) -> dict:
    res: dict = {"methods": {}, "regimes": {}, "reliability": {}, "expected_r": {}}
    blocks = K.month_blocks(P["date"].to_numpy())
    Hs = P["H"].to_numpy()
    yw, yl, yn = P["y_win"].to_numpy().astype(float), P["y_loss"].to_numpy().astype(float), P["y_nei"].to_numpy().astype(float)
    Y3 = np.column_stack([yw, yl, yn])
    have = [m for m in METHODS if f"pw_{m}" in P.columns]
    for mth in have:
        pw, pl_, pn = (P[f"{c}_{mth}"].to_numpy() for c in ("pw", "pl", "pn"))
        ok = np.isfinite(pw)
        for H in [*B.HORIZONS, "all"]:
            m = ok & ((Hs == H) if H != "all" else np.ones_like(ok))
            if m.sum() < 100:
                continue
            r = {"win": K.summary(pw[m], yw[m]), "loss": K.summary(pl_[m], yl[m]), "neither": K.summary(pn[m], yn[m]),
                 **multiclass_scores(np.column_stack([pw[m], pl_[m], pn[m]]), Y3[m])}
            res["methods"].setdefault(mth, {})[str(H)] = r
            if H != "all":
                res["reliability"].setdefault(mth, {})[str(H)] = {"win": K.reliability(pw[m], yw[m], 15),
                                                                  "loss": K.reliability(pl_[m], yl[m], 15)}
        rg = regimes(P)
        for nm, msk in rg.items():
            for H in B.HORIZONS:
                m = ok & msk & (Hs == H)
                if m.sum() < 200:
                    continue
                res["regimes"].setdefault(mth, {}).setdefault(nm, {})[str(H)] = {
                    "n": int(m.sum()), "gap_win": K.gap(pw[m], yw[m]), "gap_loss": K.gap(pl_[m], yl[m]),
                    "ece_win": K.ece(pw[m], yw[m]), "ece_loss": K.ece(pl_[m], yl[m])}
        # bootstrap of the Brier difference vs the baselines (grid / gbm vs bm / naive), H=20
    for best in [m for m in ("grid", "gbm") if m in have]:
        for base in ("bm", "naive"):
            for H in B.HORIZONS:
                m = (Hs == H) & np.isfinite(P[f"pw_{best}"].to_numpy())
                if m.sum() < 500:
                    continue
                pb = np.column_stack([P[f"{c}_{best}"].to_numpy()[m] for c in ("pw", "pl", "pn")])
                pz = np.column_stack([P[f"{c}_{base}"].to_numpy()[m] for c in ("pw", "pl", "pn")])
                y = Y3[m]
                diff = ((pb - y) ** 2).sum(1) - ((pz - y) ** 2).sum(1)
                bs = K.block_bootstrap(lambda ix, dd=diff: float(dd[ix].mean()), blocks[m], B=300)
                res.setdefault("brier_diff", {}).setdefault(f"{best}_vs_{base}", {})[str(H)] = bs
    # expected R calibration: deciles of predicted E[R] vs realised R (grid)
    for H in B.HORIZONS:
        m = Hs == H
        er, rr = P["exp_r"].to_numpy()[m], P["real_r"].to_numpy()[m]
        erm, rrm = P["exp_r_mtm"].to_numpy()[m], P["real_r_mtm"].to_numpy()[m]
        if m.sum() < 500:
            continue
        q = np.quantile(er, np.linspace(0, 1, 11))
        k = np.clip(np.searchsorted(q, er, side="right") - 1, 0, 9)
        res["expected_r"][str(H)] = {
            "deciles": [{"decile": int(j + 1), "n": int((k == j).sum()), "pred": float(er[k == j].mean()), "real": float(rr[k == j].mean()),
                         "pred_mtm": float(erm[k == j].mean()), "real_mtm": float(rrm[k == j].mean())} for j in range(10) if (k == j).any()],
            "share_positive": float((er > 0).mean()),
            "real_r_pos": float(rr[er > 0].mean()) if (er > 0).any() else None,
            "real_r_neg": float(rr[er <= 0].mean()) if (er <= 0).any() else None,
            "real_r_all": float(rr.mean())}
    return res


if __name__ == "__main__":
    main()
