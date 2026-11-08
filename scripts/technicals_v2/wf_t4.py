"""T6 / T4: walk-forward of the technical score -> forward-outcome mapping (docs/TECHNICALS_V2_VALIDATION.md).

Inputs: scratchpad/tech_v2_models/wf/<region>/b*_setups.parquet (weekly PIT setups with the score, sub-scores and
forward 5 / 20 / 60-session log returns). Excess return = minus the equal-weight mean of the region's universe on
the same date. Per test year Y in 2010..2026: decile boundaries and bucket stats from origins < Y - 90 days, applied
to Y (OOS deciles); pooled OOS decile table, monotonicity, top-bottom spread CI, rank IC (t on non-overlapping
dates), regime splits. Scores: technical_score (as served) and technical_score_pct (percentile composite).
Outputs: data/reports/technicals_v2/t4_summary.json, t4_oos.parquet. State key t4.

    .venv/Scripts/python.exe scripts/technicals_v2/wf_t4.py
"""

from __future__ import annotations

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
from app.techprob import calib as K  # noqa: E402
from app.techprob import score_cal as SC  # noqa: E402
from app.techprob import state as ST  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v2_models" / "wf"
REGIONS = ("US", "EU", "UK", "JP")
YEARS = list(range(2010, 2027))
COVID = (np.datetime64("2020-02-15"), np.datetime64("2020-06-30"))
SCORES = ("technical_score", "technical_score_pct")


def load() -> pl.DataFrame:
    cols = ["security_id", "date", "region", "sector_id", "vix_high", "technical_score", *SC.SUBS, "fwd_5", "fwd_20", "fwd_60"]
    fr = [pl.read_parquet(f, columns=cols) for rg in REGIONS for f in sorted((WF / rg).glob("b*_setups.parquet"))]
    df = pl.concat(fr, how="diagonal_relaxed")
    df = SC.add_excess(df)
    return SC.add_percentiles(df)


def main() -> None:
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v2" / "wf_t4.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_t4")
    t00 = time.time()
    ST.update(s, t4={"status": "running", "step": "walk-forward"})
    df = load()
    log.info("rows %d (%.0fs)", df.height, time.time() - t00)
    d_all = df["date"].to_numpy().astype("datetime64[D]")
    out: dict = {"rows": df.height, "scores": {}, "saturation": {}}
    # saturation of the raw sub-scores vs percentiles
    for c in SC.SUBS:
        x = df[c].drop_nulls().to_numpy()
        p = df[f"pct_{c.removeprefix('score_')}"].drop_nulls().to_numpy()
        out["saturation"][c] = {"share_100": float((x >= 99.999).mean()), "share_0": float((x <= 1e-9).mean()),
                                "n_distinct": int(len(np.unique(np.round(x, 6)))), "pct_share_ge_95": float((p >= 95).mean())}
    oos_rows = []
    for sc in SCORES:
        res_s: dict = {}
        for h in SC.HORIZONS:
            e = f"exc_{h}"
            sub = df.filter(pl.col(e).is_not_null() & pl.col(sc).is_not_null())
            d = sub["date"].to_numpy().astype("datetime64[D]")
            x = sub[sc].to_numpy()
            y = sub[e].to_numpy()
            dec = np.full(len(x), -1)
            cal = []
            for Y in YEARS:
                te = (d >= np.datetime64(f"{Y}-01-01")) & (d < np.datetime64(f"{Y + 1}-01-01"))
                tr = d < np.datetime64(f"{Y}-01-01") - np.timedelta64(90, "D")
                if not te.any() or tr.sum() < 2000:
                    continue
                edges = SC.decile_edges(x[tr])
                dec[te] = SC.decile_of(x[te], edges)
                ktr = SC.decile_of(x[tr], edges)
                for b in range(SC.N_DEC):
                    mt, ms = ktr == b, te & (dec == b)
                    if mt.sum() >= 30 and ms.sum() >= 30:
                        cal.append({"year": Y, "decile": b + 1, "n_test": int(ms.sum()), "train_hit": float((y[tr][mt] > 0).mean()),
                                    "test_hit": float((y[ms] > 0).mean()), "train_median": float(np.median(y[tr][mt])),
                                    "test_median": float(np.median(y[ms]))})
            ok = dec >= 0
            tab, by_x, by_d = [], {}, {}
            blocks = K.month_blocks(d)
            for b in range(SC.N_DEC):
                m = ok & (dec == b)
                if m.sum() < 30:
                    continue
                yy = y[m]
                by_x[b + 1], by_d[b + 1] = yy, d[m]
                med = K.block_bootstrap(lambda ix, z=yy: float(np.median(z[ix])), blocks[m], B=200)
                hit = K.block_bootstrap(lambda ix, z=yy: float((z[ix] > 0).mean()), blocks[m], B=200)
                tab.append({"decile": b + 1, "n": int(m.sum()), "score_mean": float(x[m].mean()), "median": float(np.median(yy)),
                            "q25": float(np.quantile(yy, 0.25)), "q75": float(np.quantile(yy, 0.75)), "mean": float(yy.mean()),
                            "hit_rate": float((yy > 0).mean()), "median_ci": [med["lo"], med["hi"]], "hit_ci": [hit["lo"], hit["hi"]]})
            mono = SC.monotonicity(tab, by_x, by_d)
            ic_df = SC.rank_ic(sub.filter(pl.col("date") >= pl.date(2010, 1, 1)), sc, h)
            ic = SC.ic_summary(ic_df, h)
            cdf = pl.DataFrame(cal) if cal else pl.DataFrame()
            mapping_err = None
            if not cdf.is_empty():
                w = cdf["n_test"].to_numpy()
                mapping_err = {"hit_mae": float(np.average(np.abs(cdf["test_hit"] - cdf["train_hit"]), weights=w)),
                               "median_mae": float(np.average(np.abs(cdf["test_median"] - cdf["train_median"]), weights=w))}
            reg = {}
            yr = d.astype("datetime64[Y]").astype(int) + 1970
            vix = sub["vix_high"].cast(pl.Boolean).to_numpy()
            for nm, m in (("covid_2020", (d >= COVID[0]) & (d <= COVID[1])), ("year_2022", yr == 2022), ("vix_gt_25", vix)):
                icr = SC.ic_summary(SC.rank_ic(sub.filter(pl.Series(m)), sc, h), h)
                top, bot = ok & m & (dec == SC.N_DEC - 1), ok & m & (dec == 0)
                reg[nm] = {"ic": icr.get("mean_ic"), "t": icr.get("t_stat"), "n_dates": icr.get("n_dates"),
                           "top_minus_bottom_mean": float(y[top].mean() - y[bot].mean()) if top.any() and bot.any() else None}
            res_s[str(h)] = {"table": tab, "monotonicity": mono, "ic": ic, "mapping_error": mapping_err, "regimes": reg,
                             "edge_significant": SC.edge_verdict(ic, mono)}
            log.info("%s h=%d: IC %s t %s rho %s spread %s -> %s", sc, h, ic.get("mean_ic"), ic.get("t_stat"), mono.get("spearman"),
                     (mono.get("spread_ci") or {}).get("est"), res_s[str(h)]["edge_significant"])
            if sc == "technical_score":
                oos_rows.append(pl.DataFrame({"h": np.full(ok.sum(), h), "date": d[ok], "decile": dec[ok] + 1, "score": x[ok], "exc": y[ok]}))
        out["scores"][sc] = res_s
    rd = s.data_dir / "reports" / "technicals_v2"
    rd.mkdir(parents=True, exist_ok=True)
    if oos_rows:
        pl.concat(oos_rows).write_parquet(rd / "t4_oos.parquet")
    out["seconds"] = round(time.time() - t00)
    (rd / "t4_summary.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    ST.update(s, t4={"status": "walk_forward_done", "summary_file": str(rd / "t4_summary.json"),
                     "edge": {sc: {h: out["scores"][sc][str(h)]["edge_significant"] for h in SC.HORIZONS} for sc in SCORES}})
    log.info("done (%.0fs)", time.time() - t00)
    _ = d_all


if __name__ == "__main__":
    main()
