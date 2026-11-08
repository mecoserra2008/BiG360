"""Meta-labelling step 4: gate report (docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling").

Reads data/reports/technicals_v3/meta/{meta_oos_h<H>.parquet, wf_h<H>.json, leakage.json, econ.json} and the latest
production fit (data/models/technicals_v3/meta/<fit>/meta.json, importance) and writes:
  summary.json   every metric (methods x H, regimes, bootstrap differences, reliability, expected R deciles,
                 decisions, ablation, permutation importance, permutation / peek tests, economics)
  gate.json      verdicts meta_h10/20/40 (calibration gate) and decision_h10/20/40 (economic check) -> tech_meta_gates
  summary.md     the generated tables (pasted into the validation doc section)
  reliability_meta_h<H>.svg

    .venv/Scripts/python.exe scripts/technicals_v3/meta/report_meta.py
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techmeta import state as ST  # noqa: E402
from app.techprob import calib as K  # noqa: E402

HS = (20, 10, 40)
METHODS = {"t2": ("pw", "pl"), "cf": ("pw_cf", "pl_cf"), "lr": ("pw_lr", "pl_lr"), "xgb": ("pw_xgb", "pl_xgb"), "sel": ("pw_sel", "pl_sel")}
COVID = (np.datetime64("2020-02-15"), np.datetime64("2020-06-30"))
GATE_REGIMES = ("covid_2020", "year_2020", "year_2022", "vix_gt_25")
ECE_MAX, GAP_MAX = 0.025, 0.05


def fnum(x, nd=4):
    return "n/a" if x is None or (isinstance(x, float) and not math.isfinite(x)) else f"{x:.{nd}f}"


def pp(x, nd=2):
    return "n/a" if x is None else f"{100 * x:+.{nd}f}"


def regimes(P: pl.DataFrame) -> dict[str, np.ndarray]:
    d = P["date"].to_numpy().astype("datetime64[D]")
    yr = P["date"].dt.year().to_numpy()
    vix = P["m_vix"].cast(pl.Float64).fill_null(np.nan).to_numpy()
    out = {"all": np.ones(P.height, bool), "covid_2020": (d >= COVID[0]) & (d <= COVID[1]), "year_2020": yr == 2020,
           "year_2022": yr == 2022, "vix_gt_25": vix > 25, "earnings_window": P["earn"].cast(pl.Boolean).to_numpy(),
           "long": P["direction"].to_numpy() > 0, "short": P["direction"].to_numpy() < 0}
    for v, nm in enumerate(("low", "mid", "high")):
        out[f"vol_{nm}"] = P["vol_regime"].to_numpy() == v
    for rg in ("US", "EU", "UK", "JP"):
        out[f"region_{rg}"] = (P["region"] == rg).to_numpy()
    return out


def svg_reliability(rel: dict[str, list], path: Path, title: str) -> None:
    W, Hh, m = 360, 360, 40
    sx = lambda p: m + p * (W - 2 * m)  # noqa: E731
    sy = lambda p: Hh - m - p * (Hh - 2 * m)  # noqa: E731
    cols = {"t2": "#8a8f98", "sel": "#2563eb"}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{Hh}" font-family="sans-serif" font-size="11">',
             f'<text x="{m}" y="20">{title}</text>',
             f'<line x1="{sx(0)}" y1="{sy(0)}" x2="{sx(1)}" y2="{sy(1)}" stroke="#ccc" stroke-dasharray="4"/>',
             f'<rect x="{m}" y="{m}" width="{W - 2 * m}" height="{Hh - 2 * m}" fill="none" stroke="#999"/>']
    for k, rows in rel.items():
        pts = " ".join(f"{sx(r['p_mean']):.1f},{sy(r['y_mean']):.1f}" for r in rows)
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{cols.get(k, "#000")}" stroke-width="1.5"/>')
        for r in rows:
            parts.append(f'<circle cx="{sx(r["p_mean"]):.1f}" cy="{sy(r["y_mean"]):.1f}" r="2.5" fill="{cols.get(k, "#000")}"/>')
    parts.append(f'<text x="{m}" y="{Hh - 10}">predicted P(win) (x) vs realised (y); grey = T2, blue = meta</text></svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def horizon_summary(P: pl.DataFrame, H: int, rd: Path) -> dict:
    yw = P["y_win"].cast(pl.Float64).to_numpy()
    yl = P["y_loss"].cast(pl.Float64).to_numpy()
    blocks = K.month_blocks(P["date"].to_numpy())
    out: dict = {"n": P.height, "years": sorted(set(P["year"].to_list())), "methods": {}, "regimes": {}, "diff": {}, "per_year": [],
                 "selected": P.group_by("year").agg(pl.col("selected").first()).sort("year").to_dicts()}
    for m, (cw, cl) in METHODS.items():
        pw = P[cw].cast(pl.Float64).to_numpy()
        pl_ = P[cl].cast(pl.Float64).to_numpy()
        out["methods"][m] = {"win": K.summary(pw, yw), "loss": K.summary(pl_, yl)}
    rg = regimes(P)
    for m in ("t2", "sel", "cf"):
        pw = P[METHODS[m][0]].cast(pl.Float64).to_numpy()
        pl_ = P[METHODS[m][1]].cast(pl.Float64).to_numpy()
        for nm, msk in rg.items():
            if msk.sum() < 500:
                continue
            out["regimes"].setdefault(m, {})[nm] = {"n": int(msk.sum()), "gap_win": K.gap(pw[msk], yw[msk]),
                                                    "gap_loss": K.gap(pl_[msk], yl[msk]), "ece_win": K.ece(pw[msk], yw[msk])}
    for a, b in (("sel", "t2"), ("sel", "cf"), ("lr", "t2"), ("xgb", "t2"), ("cf", "t2")):
        pa = P[METHODS[a][0]].cast(pl.Float64).to_numpy()
        pb = P[METHODS[b][0]].cast(pl.Float64).to_numpy()
        db = (pa - yw) ** 2 - (pb - yw) ** 2
        cl = lambda p: np.clip(p, 1e-6, 1 - 1e-6)  # noqa: E731
        dl = -(yw * np.log(cl(pa)) + (1 - yw) * np.log(1 - cl(pa))) + (yw * np.log(cl(pb)) + (1 - yw) * np.log(1 - cl(pb)))
        out["diff"][f"{a}_vs_{b}"] = {"brier": K.block_bootstrap(lambda ix, x=db: float(x[ix].mean()), blocks, B=400),
                                      "logloss": K.block_bootstrap(lambda ix, x=dl: float(x[ix].mean()), blocks, B=400)}
    for y in out["years"]:
        m_ = (P["year"] == y).to_numpy()
        row = {"year": y, "n": int(m_.sum()), "y_win": float(yw[m_].mean())}
        for m in ("t2", "cf", "sel"):
            p = P[METHODS[m][0]].cast(pl.Float64).to_numpy()[m_]
            row[f"brier_{m}"] = K.brier(p, yw[m_])
            row[f"mean_{m}"] = float(p.mean())
        row["take_share"] = float(P["take"].to_numpy()[m_].mean())
        row["real_r_all"] = float(P["real_r_mtm"].to_numpy()[m_].mean())
        tk = P["take"].to_numpy()[m_]
        row["real_r_take"] = float(P["real_r_mtm"].to_numpy()[m_][tk].mean()) if tk.any() else None
        out["per_year"].append(row)
    rel = {m: K.reliability(P[METHODS[m][0]].cast(pl.Float64).to_numpy(), yw, 15) for m in ("t2", "sel")}
    out["reliability"] = rel
    svg_reliability(rel, rd / f"reliability_meta_h{H}.svg", f"Meta P(win), H = {H} (walk-forward OOS)")
    # expected R deciles
    er = P["er_sel"].cast(pl.Float64).to_numpy()
    real = P["real_r_mtm"].cast(pl.Float64).to_numpy()
    q = np.quantile(er, np.linspace(0, 1, 11))
    k = np.clip(np.searchsorted(q, er, side="right") - 1, 0, 9)
    t2e = P["exp_r_mtm"].cast(pl.Float64).to_numpy()
    out["expected_r"] = {"deciles": [{"decile": j + 1, "n": int((k == j).sum()), "pred": float(er[k == j].mean()),
                                      "real": float(real[k == j].mean()), "t2_pred": float(t2e[k == j].mean())} for j in range(10)],
                         "corr_pred_real": float(np.corrcoef(er, real)[0, 1])}
    # decisions
    tk = P["take"].to_numpy()
    wk = (P["date"].to_numpy().astype("datetime64[D]").astype(np.int64) + 3) // 7

    def wt(x: np.ndarray, w: np.ndarray) -> dict:
        u, inv = np.unique(w, return_inverse=True)
        mm = np.bincount(inv, weights=x) / np.bincount(inv)
        return {"mean": float(x.mean()), "weekly_t": float(mm.mean() / (mm.std(ddof=1) / math.sqrt(len(mm)))) if len(mm) > 2 else None,
                "n": int(len(x))}
    diff_w = None
    if tk.any() and (~tk).any():
        ua, ia = np.unique(wk, return_inverse=True)
        allm = np.bincount(ia, weights=real) / np.bincount(ia)
        tm = np.full(len(ua), np.nan)
        cnt = np.bincount(ia[tk], minlength=len(ua))
        tm[cnt > 0] = (np.bincount(ia[tk], weights=real[tk], minlength=len(ua)) / np.maximum(cnt, 1))[cnt > 0]
        dd_ = tm - allm
        dd_ = dd_[np.isfinite(dd_)]
        diff_w = {"mean": float(dd_.mean()), "t": float(dd_.mean() / (dd_.std(ddof=1) / math.sqrt(len(dd_))))}
    out["decision"] = {"take_share": float(tk.mean()), "taken": wt(real[tk], wk[tk]) if tk.any() else None,
                       "skipped": wt(real[~tk], wk[~tk]) if (~tk).any() else None, "all": wt(real, wk),
                       "take_minus_all_weekly": diff_w, "thresholds": P.group_by("year").agg(pl.col("threshold").first()).sort("year").to_dicts(),
                       "mean_kelly_taken": float(P["kelly"].to_numpy()[tk].mean()) if tk.any() else None}
    return out


def tests_summary(wf: dict) -> dict:
    yrs = wf["years"]
    out: dict = {"ablation": {}, "perm_importance": {}, "label_perm": {}, "context_perm": {}, "peek": {}}
    for kind in ("lr", "xgb"):
        ab = [y.get("ablation", {}).get(kind) for y in yrs if y.get("ablation")]
        if ab:
            groups = sorted(ab[0])
            out["ablation"][kind] = {g: {"d_logloss_mean": float(np.mean([a[g]["d_logloss"] for a in ab])),
                                         "d_logloss_se": float(np.std([a[g]["d_logloss"] for a in ab], ddof=1) / math.sqrt(len(ab))),
                                         "years_worse_without": int(sum(a[g]["d_logloss"] > 0 for a in ab)), "n_years": len(ab)}
                                     for g in groups}
        pi = [y.get("perm_importance", {}).get(kind) for y in yrs if y.get("perm_importance")]
        if pi:
            out["perm_importance"][kind] = {g: float(np.mean([p[g] for p in pi])) for g in sorted(pi[0])}
        lp = [y["label_perm"][kind] for y in yrs if y.get("label_perm")]
        if lp:
            bss = [(r["brier_clim"] - r["brier"]) / r["brier_clim"] for r in lp]
            out["label_perm"][kind] = {"bss_mean": float(np.mean(bss)), "bss_max": float(np.max(bss)), "n_years": len(bss),
                                       "logloss_minus_clim_mean": float(np.mean([r["logloss"] - r["logloss_clim"] for r in lp]))}
        cp = [(y["context_perm"][kind], y.get("ablation", {}).get(kind)) for y in yrs if y.get("context_perm")]
        if cp:
            # honest log-loss of the same learner = any ablation row's logloss - d_logloss
            hon = [next(iter(ab.values()))["logloss"] - next(iter(ab.values()))["d_logloss"] for _, ab in cp if ab]
            val = [r["logloss"] - h for (r, _), h in zip(cp, hon, strict=False)]
            out["context_perm"][kind] = {"logloss_minus_cf_mean": float(np.mean([r["logloss"] - r["logloss_cf"] for r, _ in cp])),
                                         "logloss_minus_t2_mean": float(np.mean([r["logloss"] - r["logloss_t2"] for r, _ in cp])),
                                         "context_value_mean": float(np.mean(val)) if val else None,
                                         "context_value_se": float(np.std(val, ddof=1) / math.sqrt(len(val))) if len(val) > 1 else None,
                                         "years_context_helps": int(sum(v > 0 for v in val)), "n_years": len(cp)}
        pk = [y["peek"][kind] for y in yrs if y.get("peek")]
        if pk:
            out["peek"][kind] = {"logloss_gain_mean": float(np.mean([r["logloss_honest"] - r["logloss"] for r in pk])),
                                 "perm_importance_peek_mean": float(np.mean([r["perm_importance_peek"] for r in pk])),
                                 "caught": bool(np.mean([r["logloss_honest"] - r["logloss"] for r in pk]) > 0.01), "n_years": len(pk)}
    return out


def importance(s) -> dict:
    f = s.data_dir / "reports" / "technicals_v3" / "meta" / "importance.json"      # production fit of the e2e / seed run
    if f.exists():
        m = json.loads(f.read_text(encoding="utf-8"))
        return {"fit_date": m.get("fit_date"), "models": {H: {"selected": v.get("selected"), "importance": v.get("importance")}
                                                          for H, v in (m.get("models") or {}).items()}}
    root = s.data_dir / "models" / "technicals_v3" / "meta"
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "meta.json").exists()) if root.exists() else []
    if not dirs:
        return {}
    m = json.loads((dirs[-1] / "meta.json").read_text(encoding="utf-8"))
    return {"fit_date": m.get("fit_date"), "models": {H: {"selected": v.get("selected"), "importance": v.get("importance")}
                                                      for H, v in (m.get("models") or {}).items()}}


def main() -> None:
    s = get_settings()
    rd = s.data_dir / "reports" / "technicals_v3" / "meta"
    summ: dict = {"generated_at": datetime.now(UTC).isoformat(), "horizons": {}}
    for H in HS:
        f = rd / f"meta_oos_h{H}.parquet"
        if f.exists():
            P = pl.read_parquet(f)
            summ["horizons"][str(H)] = horizon_summary(P, H, rd)
            f0 = rd / "r2_noextra" / f"meta_oos_h{H}.parquet"
            if f0.exists():                     # value of the #1 / #5 features: same rows, round 2 without them
                Q = pl.read_parquet(f0, columns=["security_id", "date", "pw_sel"]).rename({"pw_sel": "pw_noextra"})
                J = P.join(Q, on=["security_id", "date"], how="inner")
                y = J["y_win"].cast(pl.Float64).to_numpy()
                a_, b_ = J["pw_sel"].cast(pl.Float64).to_numpy(), J["pw_noextra"].cast(pl.Float64).to_numpy()
                db = (a_ - y) ** 2 - (b_ - y) ** 2
                summ["horizons"][str(H)]["diff"]["sel_vs_noextra"] = {
                    "brier": K.block_bootstrap(lambda ix, x=db: float(x[ix].mean()), K.month_blocks(J["date"].to_numpy()), B=400),
                    "n": J.height, "brier_noextra": K.brier(b_, y), "logloss_noextra": K.logloss(b_, y)}
    wf20 = json.loads((rd / "wf_h20.json").read_text(encoding="utf-8")) if (rd / "wf_h20.json").exists() else {"years": []}
    summ["tests"] = tests_summary(wf20)
    summ["features"] = wf20.get("features")
    lk = json.loads((rd / "leakage.json").read_text(encoding="utf-8")) if (rd / "leakage.json").exists() else {}
    summ["leakage"] = {"passed": lk.get("passed"), "bars": [r["passed"] for r in lk.get("bars", [])],
                       "bars_peek_caught": [not r["passed"] for r in lk.get("bars_peek", [])],
                       "context": (lk.get("context") or {}).get("passed"), "context_peek_caught": not (lk.get("context_peek") or {}).get("passed", True)}
    econ = json.loads((rd / "econ.json").read_text(encoding="utf-8")) if (rd / "econ.json").exists() else {}
    summ["econ"] = econ
    summ["importance"] = importance(s)
    # ---- gate
    t = summ["tests"]
    tests_ok = (all(v["bss_mean"] <= 0.002 for v in t["label_perm"].values()) and bool(t["label_perm"])
                and all(v["caught"] for v in t["peek"].values()) and bool(t["peek"]) and bool(summ["leakage"]["passed"]))
    gate: dict = {"generated_at": summ["generated_at"], "outputs": {}, "tests_ok": tests_ok,
                  "criteria": {"ece_max": ECE_MAX, "regime_gap_max": GAP_MAX, "gate_regimes": list(GATE_REGIMES) + ["regions", "long / short",
                               "vol regimes", "earnings window (n >= 2000)"],
                               "baselines": "Brier and log-loss of meta P(win) below T2 alone and the context-free logistic model; "
                                            "Brier difference vs T2 month-block CI below 0",
                               "tests": "label permutation BSS <= 0.002 (mean over years, LR and XGB); shifted-feature peek caught; "
                                        "structural leakage tests pass (context permutation = diagnostic of the context's value)",
                               "decision": "meta_h passes AND US meta-filtered minus all setups paired t >= 2 AND no other region t < -1"}}
    for H, hs in summ["horizons"].items():
        sel, t2, cf = hs["methods"]["sel"], hs["methods"]["t2"], hs["methods"]["cf"]
        ece_w, ece_l = sel["win"]["ece"], sel["loss"]["ece"]
        gaps = []
        for nm, r in (hs["regimes"].get("sel") or {}).items():
            if nm == "all":
                continue
            if nm in GATE_REGIMES or (r["n"] >= 2000):
                gaps.append((nm, max(abs(r["gap_win"] or 0), abs(r["gap_loss"] or 0))))
        worst = max(gaps, key=lambda x: x[1]) if gaps else (None, None)
        bd = hs["diff"]["sel_vs_t2"]["brier"]
        point = (sel["win"]["brier"] < t2["win"]["brier"] and sel["win"]["logloss"] < t2["win"]["logloss"]
                 and sel["win"]["brier"] < cf["win"]["brier"] and sel["win"]["logloss"] < cf["win"]["logloss"])
        beats = point and bd.get("hi") is not None and bd["hi"] < 0
        ok = (ece_w <= ECE_MAX and ece_l <= ECE_MAX and worst[1] is not None and worst[1] <= GAP_MAX and beats and tests_ok)
        why = []
        if ece_w > ECE_MAX or ece_l > ECE_MAX:
            why.append(f"ECE {100 * ece_w:.2f} / {100 * ece_l:.2f} pp")
        if worst[1] is not None and worst[1] > GAP_MAX:
            why.append(f"{worst[0]} off by {100 * worst[1]:.1f} pp")
        if not beats:
            why.append(f"not better than the baselines (Brier {sel['win']['brier']:.5f} vs T2 {t2['win']['brier']:.5f} / context-free "
                       f"{cf['win']['brier']:.5f}; diff vs T2 CI [{fnum(bd.get('lo'), 5)}, {fnum(bd.get('hi'), 5)}])")
        if not tests_ok:
            why.append("permutation / leakage tests")
        gate["outputs"][f"meta_h{H}"] = {"verdict": "pass" if ok else "fail", "ece": ece_w, "ece_loss": ece_l,
                                         "max_regime_gap": worst[1], "worst_regime": worst[0], "beats_baselines": beats, "beats_point": point,
                                         "brier": sel["win"]["brier"], "brier_t2": t2["win"]["brier"], "brier_cf": cf["win"]["brier"],
                                         "logloss": sel["win"]["logloss"], "logloss_t2": t2["win"]["logloss"],
                                         "logloss_cf": cf["win"]["logloss"],
                                         "summary": "PASS" if ok else "FAIL: " + "; ".join(why)}
        pr = econ.get("paired") or {}
        us = pr.get(f"US_h{H}_meta_take_minus_all") or {}
        others = [pr.get(f"{rg}_h{H}_meta_take_minus_all", {}).get("t") for rg in ("EU", "UK", "JP")]
        dec_ok = ok and (us.get("t") or -9) >= 2 and all((x is None or x >= -1) for x in others)
        gate["outputs"][f"decision_h{H}"] = {"verdict": "pass" if dec_ok else ("fail" if econ else "not_validated"),
                                             "economic_t": us.get("t"), "ann_diff": us.get("ann_diff"),
                                             "summary": (f"meta take - all (US, H={H}): {fnum(us.get('ann_diff'), 4)}/yr, t {fnum(us.get('t'), 2)}; "
                                                         f"EU / UK / JP t {', '.join(fnum(x, 2) for x in others)}"
                                                         + ("" if ok else "; calibration gate failed"))}
    (rd / "summary.json").write_text(json.dumps(summ, indent=1, default=str), encoding="utf-8")
    (rd / "gate.json").write_text(json.dumps(gate, indent=1, default=str), encoding="utf-8")
    (rd / "summary.md").write_text(markdown(summ, gate), encoding="utf-8")
    ST.update(s, report={"generated_at": summ["generated_at"], "verdicts": {k: v["verdict"] for k, v in gate["outputs"].items()}})
    print(json.dumps({k: v["summary"] for k, v in gate["outputs"].items()}, indent=1))


def markdown(summ: dict, gate: dict) -> str:
    L: list[str] = []
    L.append("| H | meta P(win) ECE (pp) | P(loss) ECE | worst regime | Brier meta / T2 / context-free | log-loss meta / T2 / context-free | "
             "Brier meta - T2 [95% CI] | gate |")
    L.append("|---|---|---|---|---|---|---|---|")
    for H, hs in summ["horizons"].items():
        g = gate["outputs"][f"meta_h{H}"]
        bd = hs["diff"]["sel_vs_t2"]["brier"]
        L.append(f"| {H} | {100 * g['ece']:.2f} | {100 * g['ece_loss']:.2f} | {g['worst_regime']} {pp(g['max_regime_gap'], 1)} | "
                 f"{g['brier']:.5f} / {g['brier_t2']:.5f} / {g['brier_cf']:.5f} | {g['logloss']:.5f} / {g['logloss_t2']:.5f} / "
                 f"{g['logloss_cf']:.5f} | {bd['est']:+.5f} [{fnum(bd.get('lo'), 5)}, {fnum(bd.get('hi'), 5)}] | "
                 f"**{g['verdict'].upper()}** |")
    L.append("")
    L.append("| H | method | n | ECE win | gap win | Brier win | log-loss win | ECE loss | Brier loss |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for H, hs in summ["horizons"].items():
        for m, r in hs["methods"].items():
            L.append(f"| {H} | {m} | {r['win']['n']:,} | {100 * r['win']['ece']:.2f} | {pp(r['win']['gap'])} | {r['win']['brier']:.5f} | "
                     f"{r['win']['logloss']:.5f} | {100 * r['loss']['ece']:.2f} | {r['loss']['brier']:.5f} |")
    L.append("")
    L.append("Regime gaps (realised - predicted, pp, win / loss), H = 20:")
    L.append("")
    L.append("| regime | n | T2 | meta | context-free |")
    L.append("|---|---|---|---|---|")
    h20 = summ["horizons"].get("20") or {}
    for nm, r in (h20.get("regimes", {}).get("sel") or {}).items():
        t2r = h20["regimes"]["t2"].get(nm, {})
        cfr = h20["regimes"]["cf"].get(nm, {})
        L.append(f"| {nm} | {r['n']:,} | {pp(t2r.get('gap_win'), 1)} / {pp(t2r.get('gap_loss'), 1)} | {pp(r['gap_win'], 1)} / "
                 f"{pp(r['gap_loss'], 1)} | {pp(cfr.get('gap_win'), 1)} / {pp(cfr.get('gap_loss'), 1)} |")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    main()
