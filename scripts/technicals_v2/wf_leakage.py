"""T6 leakage tests on real data (docs/TECHNICALS_V2_VALIDATION.md):

1. causal invariance of the point-in-time history (app.techprob.leakage.causal_history): every feature of an origin
   before a cut must be unchanged when all bars after the cut are replaced; the deliberately peeking variant (a zone
   feature reading the close 5 sessions ahead) must be caught;
2. label permutation: the T3 classifier (logit / hgb) and the T2 GBM candidate refitted on permuted labels must have
   ~0 out-of-sample Brier skill (the same pipeline on the true labels for reference);
3. T4: rank IC of the score vs permuted forward excess returns ~0.
Output: data/reports/technicals_v2/leakage.json. State key t6.leakage.

    .venv/Scripts/python.exe scripts/technicals_v2/wf_leakage.py
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.conformal import data as D  # noqa: E402
from app.conformal.validation import REGIONS  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.techprob import barriers as B  # noqa: E402
from app.techprob import history as Hh  # noqa: E402
from app.techprob import leakage as L  # noqa: E402
from app.techprob import score_cal as SC  # noqa: E402
from app.techprob import state as ST  # noqa: E402
from app.techprob import zone_model as M  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v2_models" / "wf"


def main() -> None:
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v2" / "wf_leakage.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_leakage")
    t00 = time.time()
    ST.update(s, t6={"leakage": {"status": "running"}})
    out: dict = {"causal": [], "causal_peek": [], "permutation": {}, "ic_permutation": {}}
    vix = D.vix_daily(date(2002, 1, 1), date(2026, 10, 21), s)
    vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
    vv = vix["vix"].to_numpy()
    for rg, k in (("US", 0), ("US", 250), ("EU", 3), ("UK", 1), ("JP", 2)):
        ids = sorted(D.universe_ids(REGIONS[rg], s)["security_id"].to_list())
        items = Hh.load_inputs(ids[k:k + 3], rg, Hh.PRICE_START, date(2026, 10, 21), s)
        if not items:
            continue
        si = max(items, key=lambda x: x.bars.n)
        r = L.causal_history(si, rg, cuts=(0.7, 0.9), vix_d=vd, vix_v=vv)
        out["causal"].append({"security_id": si.bars.security_id, "region": rg, **r})
        log.info("causal %s %s passed=%s", rg, si.bars.security_id, r["passed"])
        if len(out["causal_peek"]) < 2:
            rp = L.causal_history(si, rg, cuts=(0.7,), vix_d=vd, vix_v=vv, peek=True)
            out["causal_peek"].append({"security_id": si.bars.security_id, "region": rg, "caught": not rp["passed"],
                                       "violations": rp["cuts"][0]["violations"][:5]})
            log.info("peek caught=%s", not rp["passed"])
    # ---- T3 permutation (subsample of real zones)
    cols = list(dict.fromkeys(["date", "is_control", "hold", "touched", "complete", "region", *[c for c in M.REAL if c != "region_code"]]))
    fr = []
    for rg in ("US", "EU", "UK", "JP"):
        for f in sorted((WF / rg).glob("b*_zones.parquet"))[:6]:
            fr.append(pl.read_parquet(f, columns=cols).filter(pl.col("touched") & pl.col("complete") & ~pl.col("is_control")))
    Z = pl.concat(fr).with_columns(pl.col("region").map_elements(B.region_code, return_dtype=pl.Int64).alias("region_code"))
    Z = Z.sample(n=min(300_000, Z.height), seed=1)
    X = M.matrix(Z, M.REAL)
    y = Z["hold"].to_numpy().astype(float)
    dts = Z["date"].to_numpy().astype("datetime64[D]")
    for kind in ("logit", "hgb"):
        perm = L.permutation_skill(X, y, dts, kind=kind)
        order = np.argsort(dts, kind="stable")
        Xo, yo, do = X[order], y[order], dts[order]
        cut = do[int(len(yo) * 0.7)]
        tr, te = do < cut - np.timedelta64(60, "D"), do >= cut
        m = M.fit(kind, Xo[tr], yo[tr], None, None, M.REAL)
        p = m.predict(Xo[te])
        true_skill = 1 - np.mean((p - yo[te]) ** 2) / np.mean((yo[tr].mean() - yo[te]) ** 2)
        out["permutation"][f"t3_{kind}"] = {**perm, "true_label_brier_skill": float(true_skill)}
        log.info("t3 %s permutation %s true %.4f", kind, perm, true_skill)
    # ---- T2 GBM candidate permutation (binary win at H = 20)
    S = pl.concat([pl.read_parquet(f) for rg in ("US", "EU", "UK", "JP") for f in sorted((WF / rg).glob("b*_setups.parquet"))[:8]],
                  how="diagonal_relaxed").filter((pl.col("direction") != 0) & (pl.col("out_20") >= 0))
    sig = S["v_20"].sqrt().to_numpy()
    Xs = np.column_stack([np.log(np.clip(S["x_up"].to_numpy() / sig, 1e-3, None)), np.log(np.clip(S["x_dn"].to_numpy() / sig, 1e-3, None)),
                          S["vol_regime"].to_numpy().astype(float), S["earn_20"].cast(pl.Float64).to_numpy(),
                          S["direction"].to_numpy().astype(float), S["score_trend"].fill_null(50).to_numpy()]).astype(np.float32)
    d = S["direction"].to_numpy()
    o = S["out_20"].to_numpy()
    yw = np.where(d > 0, o == 1, o == 2).astype(float)
    perm = L.permutation_skill(Xs, yw, S["date"].to_numpy().astype("datetime64[D]"), kind="hgb")
    out["permutation"]["t2_gbm_win20"] = perm
    log.info("t2 permutation %s", perm)
    # ---- T4 IC permutation
    T = pl.concat([pl.read_parquet(f, columns=["security_id", "date", "region", "sector_id", "technical_score", "fwd_20"])
                   for rg in ("US", "EU", "UK", "JP") for f in sorted((WF / rg).glob("b*_setups.parquet"))], how="diagonal_relaxed")
    T = SC.add_excess(T, (20,))
    out["ic_permutation"]["technical_score_h20"] = L.ic_permutation(T, "technical_score", 20)
    out["ic_true"] = SC.ic_summary(SC.rank_ic(T.filter(pl.col("date") >= pl.date(2010, 1, 1)), "technical_score", 20), 20)
    out["passed"] = {
        "causal": all(c["passed"] for c in out["causal"]),
        "peek_caught": all(c["caught"] for c in out["causal_peek"]),
        "permutation": all(abs(v["brier_skill"]) < 0.005 for v in out["permutation"].values()),
        "ic_permutation": abs(out["ic_permutation"]["technical_score_h20"].get("mean_ic") or 0) < 0.005,
    }
    out["seconds"] = round(time.time() - t00)
    rd = s.data_dir / "reports" / "technicals_v2"
    (rd / "leakage.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    ST.update(s, t6={"leakage": {"status": "done", "passed": out["passed"]}})
    log.info("done %s (%.0fs)", out["passed"], time.time() - t00)


if __name__ == "__main__":
    main()
