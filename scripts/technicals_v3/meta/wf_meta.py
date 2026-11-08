"""Meta-labelling step 2: purged walk-forward of the meta model (docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling").

Input: scratchpad/tech_v3_meta/ds/ds_h<H>.parquet (build_dataset.py). For every test year Y = 2013 .. 2026 and
horizon H (20 primary; 10 / 40 secondary):

  fit block     origins < Jan 1 (Y-1) - embargo         (embargo = 1.5 x H sessions in calendar days + 7 >= H)
  inner val     the fit block's last year [Jan 1 (Y-2), fit end), inner train < Jan 1 (Y-2) - embargo
                -> hyperparameters (LR L2 penalty, XGB depth + early-stopped tree count) chosen ONLY here
  cal block     [Jan 1 (Y-1), Jan 1 Y - embargo)  -> Platt vs isotonic (time split of the block), the take / skip
                threshold on meta E[R] and the Kelly size reference (in-sample decisions)
  test          origins in year Y (labels complete)

Models per target (win = T1 before the invalidation within H; loss = invalidation first or same bar):
  lr     all features, T2 offset          xgb   all features, T2 base margin
  cf     context-free logistic (T2 + geometry + setup features only, T2 offset) = the "no context" baseline
  sel    lr or xgb, chosen per year on the calibration block's held-out half (in-sample)
Plus (H = 20, win target): drop-one-group ablation for lr and xgb (hyperparameters of the full model), permutation
importance per group on the test year, label-permutation test, context-permutation test, shifted-feature (peek) test.
Samples weighted by average uniqueness (AFML ch. 4).

Outputs: data/reports/technicals_v3/meta/meta_oos_h<H>.parquet, wf_h<H>.json (per-year fit records), ablation /
tests in wf_h20_tests.json. Resumable per (H, year) via scratchpad/tech_v3_meta/wf/. State key meta.wf.

    .venv/Scripts/python.exe scripts/technicals_v3/meta/wf_meta.py [--h 20,10,40] [--no-tests]
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techmeta import features as F  # noqa: E402
from app.techmeta import model as M  # noqa: E402
from app.techmeta import state as ST  # noqa: E402

DS = ROOT / "scratchpad" / "tech_v3_meta" / "ds"
WK = ROOT / "scratchpad" / "tech_v3_meta" / "wf_r3"          # r2 = without the #1 / #5 extras (kept)
YEARS = list(range(2013, 2027))
SEED = 7


def embargo_days(H: int) -> int:
    return int(math.ceil(1.5 * H * 7 / 5)) + 7


def report_dir() -> Path:
    p = get_settings().data_dir / "reports" / "technicals_v3" / "meta"
    p.mkdir(parents=True, exist_ok=True)
    return p


def extra_features() -> list[str]:
    ex = []
    for k, v in ST.parallel_features().items():
        ex += [str(x) for x in v["features"]]
    return ex


def load(H: int, extra: list[str]) -> tuple[pl.DataFrame, list[str], list[str]]:
    df = pl.read_parquet(DS / f"ds_h{H}.parquet")
    df = df.filter(pl.col("out") >= 0)
    cols = [c for c in F.all_features(extra=extra) if c in df.columns]
    cf = [c for c in F.all_features(F.CONTEXT_FREE) if c in df.columns]
    return df.sort(["date", "security_id"]), cols, cf


def masks(d: np.ndarray, Y: int, H: int) -> dict[str, np.ndarray]:
    e = np.timedelta64(embargo_days(H), "D")
    y0 = np.datetime64(f"{Y}-01-01")
    y1 = np.datetime64(f"{Y - 1}-01-01")
    y2 = np.datetime64(f"{Y - 2}-01-01")
    fit = d < y1 - e
    return {"fit": fit, "cal": (d >= y1) & (d < y0 - e), "test": (d >= y0) & (d < np.datetime64(f"{Y + 1}-01-01")),
            "inner_val": fit & (d >= y2), "inner_train": d < y2 - e}


def fit_pair(kind: str, X: np.ndarray, Yw: np.ndarray, Yl: np.ndarray, w: np.ndarray, cols: list[str], m: dict[str, np.ndarray],
             d: np.ndarray, params: dict[str, dict] | None = None) -> dict:
    out: dict = {"kind": kind}
    fit, cal = m["fit"], m["cal"]
    iv, it = m["inner_val"][fit], m["inner_train"][fit]
    for tgt, y, base in (("win", Yw, "t2_logit_pw"), ("loss", Yl, "t2_logit_pl")):
        lk = "lr" if kind in ("lr", "cf") else "xgb"
        p = (params or {}).get(tgt)
        L = M.fit_learner(lk, X[fit], y[fit], w[fit], cols, base, inner_val=None if p else iv, inner_train=None if p else it, params=p)
        mc = L.margin(X[cal])
        cal_, ci = M.fit_calibrator(mc, y[cal], w[cal], d[cal])
        out[tgt] = {"learner": L, "cal": cal_, "cal_info": ci,
                    "params": {k: v for k, v in L.params.items() if k != "inner_logloss"}, "inner": L.params.get("inner_logloss")}
    return out


def predict_pair(fp: dict, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    mm = M.MetaModel(0, fp["kind"], [], fp["win"]["learner"], fp["loss"]["learner"], fp["win"]["cal"], fp["loss"]["cal"])
    r = mm.predict(X)
    return r["p_win"], r["p_loss"]


def ll(p: np.ndarray, y: np.ndarray) -> float:
    return M.wlogloss(p, y)


def run_h(H: int, extra: list[str], tests: bool, log: logging.Logger) -> None:
    rd = report_dir()
    wk = WK / f"h{H}"
    wk.mkdir(parents=True, exist_ok=True)
    df, cols, cf = load(H, extra)
    log.info("H=%d: %d rows, %d features (%d context-free), extra %s", H, df.height, len(cols), len(cf), extra)
    d = df["date"].to_numpy().astype("datetime64[D]")
    X = F.matrix(df, cols)
    Xcf = X[:, [cols.index(c) for c in cf]]
    Yw = df["y_win"].cast(pl.Float64).to_numpy()
    Yl = df["y_loss"].cast(pl.Float64).to_numpy()
    w = df["w_uniq"].cast(pl.Float64).to_numpy()
    rr = df["rr_close"].to_numpy()
    cost = df["cost_r"].to_numpy()
    mtm = df["mtm_r"].to_numpy()
    real = df["real_r_mtm"].to_numpy()
    week = M.week_index(d)
    groups = {g: [c for c in F.GROUPS[g] if c in cols] for g in F.CONTEXT}
    if extra:
        groups[F.EXTRA_GROUP] = [x for x in F.all_features([F.EXTRA_GROUP], extra=extra) if x in cols]
    rng = np.random.default_rng(SEED)
    for Y in YEARS:
        fo = wk / f"y{Y}.parquet"
        if fo.exists():
            continue
        t0 = time.time()
        m = masks(d, Y, H)
        if m["test"].sum() < 500 or m["fit"].sum() < 20000:
            continue
        rec: dict = {"year": Y, "n_fit": int(m["fit"].sum()), "n_cal": int(m["cal"].sum()), "n_test": int(m["test"].sum())}
        te = m["test"]
        out = df.filter(pl.Series(te)).select("security_id", "date", "region", "sector_id", "direction", "rr_close", "cost_r",
                                                "mtm_r", "real_r_mtm", "y_win", "y_loss", "y_nei", "pw", "pl", "pn", "exp_r_mtm",
                                                "vol_regime", "earn", "w_uniq", "x_up", "x_dn", "a", "b", "m_vix", "tau")
        fits = {}
        for kind, XX, cc in (("lr", X, cols), ("xgb", X, cols), ("cf", Xcf, cf)):
            fp = fit_pair(kind, XX, Yw, Yl, w, cc, m, d)
            fits[kind] = fp
            pw, pl_ = predict_pair(fp, XX[te])
            out = out.with_columns(pl.Series(f"pw_{kind}", pw), pl.Series(f"pl_{kind}", pl_))
            rec[kind] = {t: {"params": fp[t]["params"], "cal": fp[t]["cal_info"], "inner": fp[t]["inner"]} for t in ("win", "loss")}
        # in-sample selection lr vs xgb: held-out half of the calibration block (win target)
        hl = {k: min(fits[k]["win"]["cal_info"]["half_logloss"].values()) for k in ("lr", "xgb")}
        sel = min(hl, key=lambda k: hl[k])
        rec["selected"] = sel
        rec["selection_score"] = hl
        out = out.with_columns(pl.col(f"pw_{sel}").alias("pw_sel"), pl.col(f"pl_{sel}").alias("pl_sel"))
        # decisions (in-sample: calibration block)
        ca = m["cal"]
        pwc, plc = predict_pair(fits[sel], X[ca])
        erc = M.expected_r(pwc, plc, rr[ca], cost[ca], mtm[ca])
        thr, tinfo = M.choose_threshold(erc, real[ca], week[ca])
        kc = M.kelly_fraction(pwc, plc, rr[ca], mtm[ca], erc)
        take_c = erc > thr
        ref = float(np.median(kc[take_c & (kc > 0)])) if (take_c & (kc > 0)).any() else None
        rec.update(threshold=thr, threshold_info=tinfo, size_ref=ref)
        pwt, plt_ = out["pw_sel"].to_numpy(), out["pl_sel"].to_numpy()
        ert = M.expected_r(pwt, plt_, rr[te], cost[te], mtm[te])
        kt = M.kelly_fraction(pwt, plt_, rr[te], mtm[te], ert)
        out = out.with_columns(pl.Series("er_sel", ert), pl.Series("take", ert > thr), pl.Series("kelly", np.where(ert > thr, kt, 0.0)),
                               pl.Series("size_rel", np.where(ert > thr, np.clip(kt / ref, 0, 3), 0.0) if ref else np.where(ert > thr, 1.0, 0.0)),
                               pl.lit(thr).alias("threshold"), pl.lit(sel).alias("selected"), pl.lit(Y).alias("year"))
        for kind in ("lr", "xgb", "cf"):
            out = out.with_columns(pl.Series(f"er_{kind}", M.expected_r(out[f"pw_{kind}"].to_numpy(), out[f"pl_{kind}"].to_numpy(),
                                                                       rr[te], cost[te], mtm[te])))
        # ---- H = 20: ablations, permutation importance, permutation / leakage tests (win target)
        if tests and H == 20:
            yt = Yw[te]
            base_ll = {k: ll(fits[k]["win"]["cal"](fits[k]["win"]["learner"].margin(X[te])), yt) for k in ("lr", "xgb")}
            rec["ablation"] = {}
            rec["perm_importance"] = {}
            for kind in ("lr", "xgb"):
                L = fits[kind]["win"]["learner"]
                pr = {"lam": L.params["lam"]} if kind == "lr" else {k: L.params[k] for k in ("max_depth", "min_child_weight", "n_trees")}
                for g, gc_ in groups.items():
                    if not gc_:
                        continue
                    keep = [c for c in cols if c not in gc_]
                    ix = [cols.index(c) for c in keep]
                    Lg = M.fit_learner(kind, X[m["fit"]][:, ix], Yw[m["fit"]], w[m["fit"]], keep, "t2_logit_pw", params=pr)
                    cg, _ = M.fit_calibrator(Lg.margin(X[ca][:, ix]), Yw[ca], w[ca], d[ca])
                    pg = cg(Lg.margin(X[te][:, ix]))
                    rec["ablation"].setdefault(kind, {})[g] = {"logloss": ll(pg, yt), "brier": float(np.mean((pg - yt) ** 2)),
                                                               "d_logloss": ll(pg, yt) - base_ll[kind]}
                    # permutation importance of the group on the test year (full model, columns shuffled jointly)
                    Xp = X[te].copy()
                    pi = rng.permutation(Xp.shape[0])
                    jj = [cols.index(c) for c in gc_]
                    Xp[:, jj] = Xp[pi][:, jj]
                    pp = fits[kind]["win"]["cal"](L.margin(Xp))
                    rec["perm_importance"].setdefault(kind, {})[g] = ll(pp, yt) - base_ll[kind]
                    del Xp
            # label permutation: refit lr / xgb on permuted labels (fit + cal blocks), skill vs climatology on test
            rec["label_perm"] = {}
            for kind in ("lr", "xgb"):
                L = fits[kind]["win"]["learner"]
                pr = {"lam": L.params["lam"]} if kind == "lr" else {k: L.params[k] for k in ("max_depth", "min_child_weight", "n_trees")}
                yp = Yw.copy()
                tr_ix = np.nonzero(m["fit"] | m["cal"])[0]
                yp[tr_ix] = yp[tr_ix][rng.permutation(len(tr_ix))]
                Lp = M.fit_learner(kind, X[m["fit"]], yp[m["fit"]], w[m["fit"]], cols, "t2_logit_pw", params=pr)
                cp, _ = M.fit_calibrator(Lp.margin(X[ca]), yp[ca], w[ca], d[ca])
                pp = cp(Lp.margin(X[te]))
                clim = float(np.average(yp[m["fit"] | m["cal"]]))
                rec["label_perm"][kind] = {"brier": float(np.mean((pp - yt) ** 2)), "brier_clim": float(np.mean((clim - yt) ** 2)),
                                           "logloss": ll(pp, yt), "logloss_clim": ll(np.full_like(yt, clim), yt)}
            # context permutation: context columns shuffled jointly across rows (fit + cal + test) -> skill over cf ~ 0
            ctx_ix = [cols.index(c) for g in groups.values() for c in g]
            Xc = X.copy()
            pi = rng.permutation(Xc.shape[0])
            Xc[:, ctx_ix] = Xc[pi][:, ctx_ix]
            rec["context_perm"] = {}
            for kind in ("lr", "xgb"):
                L = fits[kind]["win"]["learner"]
                pr = {"lam": L.params["lam"]} if kind == "lr" else {k: L.params[k] for k in ("max_depth", "min_child_weight", "n_trees")}
                Lp = M.fit_learner(kind, Xc[m["fit"]], Yw[m["fit"]], w[m["fit"]], cols, "t2_logit_pw", params=pr)
                cp, _ = M.fit_calibrator(Lp.margin(Xc[ca]), Yw[ca], w[ca], d[ca])
                pp = cp(Lp.margin(Xc[te]))
                rec["context_perm"][kind] = {"logloss": ll(pp, yt), "logloss_cf": ll(out["pw_cf"].to_numpy(), yt),
                                             "logloss_t2": ll(out["pw"].to_numpy(), yt), "brier": float(np.mean((pp - yt) ** 2))}
            del Xc
            # shifted-feature (peek) test: the NEXT weekly origin's 5-session return = the next 5 sessions after this origin
            if "c_ret5" in cols:
                peek = (df.select("security_id", "date", "c_ret5", "g_dir")
                        .with_columns((pl.col("c_ret5") * pl.col("g_dir")).alias("_raw"))
                        .with_columns(pl.col("_raw").shift(-1).over("security_id", order_by="date").alias("_next"))
                        .with_columns((pl.col("_next") * pl.col("g_dir")).alias("peek"))["peek"].cast(pl.Float64).fill_null(np.nan).to_numpy())
                Xk = np.column_stack([X, peek])
                ck = [*cols, "peek_fwd5"]
                rec["peek"] = {}
                for kind in ("lr", "xgb"):
                    L = fits[kind]["win"]["learner"]
                    pr = {"lam": L.params["lam"]} if kind == "lr" else {k: L.params[k] for k in ("max_depth", "min_child_weight", "n_trees")}
                    Lk = M.fit_learner(kind, Xk[m["fit"]], Yw[m["fit"]], w[m["fit"]], ck, "t2_logit_pw", params=pr)
                    ckc, _ = M.fit_calibrator(Lk.margin(Xk[ca]), Yw[ca], w[ca], d[ca])
                    pk = ckc(Lk.margin(Xk[te]))
                    Xkp = Xk[te].copy()
                    Xkp[:, -1] = Xkp[rng.permutation(Xkp.shape[0]), -1]
                    pkp = ckc(Lk.margin(Xkp))
                    rec["peek"][kind] = {"logloss": ll(pk, yt), "logloss_honest": base_ll[kind],
                                         "perm_importance_peek": ll(pkp, yt) - ll(pk, yt)}
                del Xk
        out.write_parquet(fo)
        with open(wk / f"y{Y}.pkl", "wb") as fh:
            pickle.dump(rec, fh)
        log.info("H=%d year %d: fit %d cal %d test %d, selected %s, thr %s (%.0fs)", H, Y, rec["n_fit"], rec["n_cal"], rec["n_test"],
                 sel, thr, time.time() - t0)
        ST.update(None, wf={f"h{H}": {"last_year": Y, "status": "running"}})
        del fits, out
        gc.collect()
    parts = sorted(wk.glob("y*.parquet"))
    P = pl.concat([pl.read_parquet(f) for f in parts], how="diagonal_relaxed")
    P.write_parquet(rd / f"meta_oos_h{H}.parquet")
    recs = []
    for f in sorted(wk.glob("y*.pkl")):
        with open(f, "rb") as fh:
            recs.append(pickle.load(fh))
    (rd / f"wf_h{H}.json").write_text(json.dumps({"H": H, "features": cols, "context_free": cf, "embargo_days": embargo_days(H),
                                                  "extra": extra, "years": recs}, indent=1, default=str), encoding="utf-8")
    ST.update(None, wf={f"h{H}": {"status": "done", "rows": P.height, "years": len(recs)}})
    log.info("H=%d done: %d OOS rows", H, P.height)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--h", default="20,10,40")
    ap.add_argument("--no-tests", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "technicals_v3" / "meta"
    logd.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logd / "wf_meta.log", level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_meta")
    extra = extra_features()
    ST.update(s, wf={"status": "running", "extra": extra})
    for H in [int(x) for x in a.h.split(",")]:
        run_h(H, extra, not a.no_tests, log)
    ST.update(s, wf={"status": "done"})


if __name__ == "__main__":
    main()
