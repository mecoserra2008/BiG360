"""T6 gate decision + raw report for Technicals v2 (docs/TECHNICALS_V2_VALIDATION.md).

Reads data/reports/technicals_v2/{t2,t3,t4}_summary.json, t3_oos.parquet, leakage.json, econ.json and writes
  gate.json      verdict per output (pass -> production, fail -> experimental) + the T3 edge-bucket table and the T4
                 edge flags consumed by the weekly fit (app.techprob.job.load_gate)
  summary.md     generated tables (ECE / gaps / Brier / log-loss per method and horizon, regimes, edge, deciles, econ)
  reliability_*.svg  reliability diagrams (predicted vs realised per quantile bin)
Gate (per output): ECE <= 2.5 pp; no regime (n >= 2000) more than 5 pp off (calibration-in-the-large); better Brier
AND log-loss than every baseline. T3 edge and T4 additionally need a significant out-of-sample edge (else the API
says 'no reliable edge' / 'no out-of-sample edge').

    .venv/Scripts/python.exe scripts/technicals_v2/report.py
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techprob import calib as K  # noqa: E402
from app.techprob import state as ST  # noqa: E402
from app.techprob import zone_model as M  # noqa: E402

ECE_MAX = 0.025
REGIME_MAX = 0.05
REGIME_MIN_N = 2000
HS = (5, 10, 20, 40, 60)
REGIME_KEYS = ("covid_2020", "year_2020", "year_2022", "vix_gt_25", "earnings_window", "no_earnings", "vol_low", "vol_mid",
               "vol_high", "region_US", "region_EU", "region_UK", "region_JP", "long", "short", "support", "resistance", "in_zone")


def load(rd: Path, name: str) -> dict:
    p = rd / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def svg_reliability(rows_by_label: dict[str, list[dict]], title: str) -> str:
    W, H, pad = 360, 360, 40
    colors = ["#2563eb", "#dc2626", "#16a34a", "#9333ea", "#ea580c"]

    def xy(p: float, y: float) -> tuple[float, float]:
        return pad + p * (W - 2 * pad), H - pad - y * (H - 2 * pad)

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H + 20 * len(rows_by_label)}" font-family="sans-serif" font-size="11">',
           f'<text x="{W / 2}" y="16" text-anchor="middle" font-size="12">{title}</text>',
           f'<rect x="{pad}" y="{pad}" width="{W - 2 * pad}" height="{H - 2 * pad}" fill="none" stroke="#999"/>']
    x0, y0 = xy(0, 0)
    x1, y1 = xy(1, 1)
    out.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y1}" stroke="#bbb" stroke-dasharray="4 3"/>')
    for t in (0, 0.25, 0.5, 0.75, 1.0):
        x, _ = xy(t, 0)
        _, y = xy(0, t)
        out.append(f'<text x="{x}" y="{H - pad + 14}" text-anchor="middle">{t:.2f}</text>')
        out.append(f'<text x="{pad - 6}" y="{y + 4}" text-anchor="end">{t:.2f}</text>')
    out.append(f'<text x="{W / 2}" y="{H - 6}" text-anchor="middle">predicted</text>')
    out.append(f'<text x="12" y="{H / 2}" transform="rotate(-90 12 {H / 2})" text-anchor="middle">realised</text>')
    for k, (lab, rows) in enumerate(rows_by_label.items()):
        c = colors[k % len(colors)]
        pts = [xy(r["p_mean"], r["y_mean"]) for r in rows]
        if pts:
            out.append('<polyline fill="none" stroke="{}" stroke-width="1.5" points="{}"/>'.format(c, " ".join(f"{a:.1f},{b:.1f}" for a, b in pts)))
            out += [f'<circle cx="{a:.1f}" cy="{b:.1f}" r="2.5" fill="{c}"/>' for a, b in pts]
        out.append(f'<text x="{pad}" y="{H + 14 + 20 * k}" fill="{c}">{lab}</text>')
    out.append("</svg>")
    return "\n".join(out)


def pct(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{nd}f}"


def main() -> None:
    s = get_settings()
    rd = s.data_dir / "reports" / "technicals_v2"
    t2, t3, t4 = load(rd, "t2_summary.json"), load(rd, "t3_summary.json"), load(rd, "t4_summary.json")
    leak, econ = load(rd, "leakage.json"), load(rd, "econ.json")
    gate: dict = {"generated_at": datetime.now(UTC).isoformat(), "thresholds": {"ece_max": ECE_MAX, "regime_max": REGIME_MAX,
                                                                                 "regime_min_n": REGIME_MIN_N},
                  "outputs": {}, "t2": {}, "t3": {}, "t4": {}, "leakage": leak.get("passed")}
    md = ["# Technicals v2 - generated validation tables", "", f"Generated {gate['generated_at']} by scripts/technicals_v2/report.py.", ""]
    leak_ok = bool(leak.get("passed")) and all((leak.get("passed") or {}).values())
    # ------------------------------------------------------------------ T2
    if t2:
        meth = t2["methods"]
        cand = [m for m in ("grid", "gbm") if m in meth]
        score = {m: np.mean([meth[m][str(H)]["win"]["ece"] + meth[m][str(H)]["brier3"] for H in (5, 10, 20) if str(H) in meth[m]]) for m in cand}
        chosen = min(score, key=score.get) if score else "grid"
        if chosen != "grid":
            gate["t2"]["note"] = "the GBM candidate scored better but production serves the grid surface; see doc"
        gate["t2"]["chosen"] = "grid"
        gate["t2"]["selection_score"] = score
        md += ["## T2 two-barrier setup probabilities (walk-forward 2010-2026, OOS setups)", "",
               "| method | H | n | ECE win (pp) | gap win (pp) | ECE loss (pp) | gap loss (pp) | ECE neither (pp) | Brier (3-class) | log-loss (3-class) |",
               "|---|---|---|---|---|---|---|---|---|---|"]
        for m in ("grid", "gbm", "bm", "naive"):
            for H in [*HS, "all"]:
                r = meth.get(m, {}).get(str(H))
                if r:
                    md.append(f"| {m} | {H} | {r['win']['n']:,} | {pct(r['win']['ece'], 2)} | {pct(r['win']['gap'], 2)} | {pct(r['loss']['ece'], 2)} | "
                              f"{pct(r['loss']['gap'], 2)} | {pct(r['neither']['ece'], 2)} | {r['brier3']:.4f} | {r['logloss3']:.4f} |")
        md += ["", "### T2 regimes (grid, calibration-in-the-large gap realised - predicted, pp)", "",
               "| regime | " + " | ".join(f"H={H} win / loss (n)" for H in HS) + " |", "|---|" + "---|" * len(HS)]
        reg = t2["regimes"].get("grid", {})
        for nm in [k for k in REGIME_KEYS if k in reg]:
            cells = []
            for H in HS:
                x = reg[nm].get(str(H))
                cells.append(f"{pct(x['gap_win'])} / {pct(x['gap_loss'])} ({x['n']:,})" if x else "-")
            md.append(f"| {nm} | " + " | ".join(cells) + " |")
        for H in HS:
            g = meth.get("grid", {}).get(str(H))
            if not g:
                continue
            worst, worst_nm = 0.0, None
            for nm, v in reg.items():
                x = v.get(str(H))
                if x and x["n"] >= REGIME_MIN_N and nm != "all":
                    w = max(abs(x["gap_win"] or 0), abs(x["gap_loss"] or 0))
                    if w > worst:
                        worst, worst_nm = w, nm
            base = {b: meth.get(b, {}).get(str(H)) for b in ("bm", "naive")}
            beats = all(bb and g["brier3"] < bb["brier3"] and g["logloss3"] < bb["logloss3"] for bb in base.values())
            ece = max(g["win"]["ece"], g["loss"]["ece"])
            ok = ece <= ECE_MAX and worst <= REGIME_MAX and beats and leak_ok
            bd = (t2.get("brier_diff") or {})
            gate["outputs"][f"t2_h{H}"] = {
                "verdict": "pass" if ok else "fail", "ece": ece, "ece_win": g["win"]["ece"], "ece_loss": g["loss"]["ece"],
                "max_regime_gap": worst, "worst_regime": worst_nm, "beats_baselines": beats,
                "brier": g["brier3"], "logloss": g["logloss3"], "brier_bm": (base["bm"] or {}).get("brier3"),
                "brier_naive": (base["naive"] or {}).get("brier3"), "logloss_bm": (base["bm"] or {}).get("logloss3"),
                "logloss_naive": (base["naive"] or {}).get("logloss3"),
                "brier_diff_vs_bm": (bd.get("grid_vs_bm") or {}).get(str(H)), "brier_diff_vs_naive": (bd.get("grid_vs_naive") or {}).get(str(H)),
                "reliability_win": (t2.get("reliability", {}).get("grid", {}).get(str(H)) or {}).get("win"),
                "summary": f"ECE {pct(ece, 2)} pp, worst regime {worst_nm} {pct(worst, 1)} pp, Brier {g['brier3']:.4f} vs BM "
                           f"{(base['bm'] or {}).get('brier3', float('nan')):.4f} / naive {(base['naive'] or {}).get('brier3', float('nan')):.4f}"}
            rel = {m: (t2.get("reliability", {}).get(m, {}).get(str(H)) or {}).get("win") or [] for m in ("grid", "gbm", "bm", "naive")}
            (rd / f"reliability_t2_h{H}.svg").write_text(svg_reliability(rel, f"T2 P(win) reliability, H={H}"), encoding="utf-8")
        md += ["", "### T2 expected R calibration (grid; deciles of predicted E[R] vs realised R, after costs)", ""]
        for H, e in (t2.get("expected_r") or {}).items():
            md.append(f"H={H}: share E[R]>0 {pct(e['share_positive'])}%, realised R | E[R]>0 = {e['real_r_pos']:.4f}, | E[R]<=0 = "
                      f"{e['real_r_neg']:.4f}, all {e['real_r_all']:.4f}")
            md.append("")
            md.append("| decile | n | pred E[R] | realised R | pred E[R] mtm | realised R mtm |")
            md.append("|---|---|---|---|---|---|")
            for d in e["deciles"]:
                md.append(f"| {d['decile']} | {d['n']:,} | {d['pred']:.3f} | {d['real']:.3f} | {d['pred_mtm']:.3f} | {d['real_mtm']:.3f} |")
            md.append("")
    # ------------------------------------------------------------------ T3
    if t3:
        kind = t3.get("chosen", "hgb")
        gate["t3"]["chosen"] = kind
        P = pl.read_parquet(rd / "t3_oos.parquet").filter(pl.col("part") == "real")
        pr, pc = P[f"p_real_{kind}"].to_numpy(), P[f"p_ctrl_{kind}"].to_numpy()
        y = P["y"].to_numpy()
        dts = P["date"].to_numpy().astype("datetime64[D]")
        pe = pr - pc
        edges = M.bucket_edges(pe)
        tab = M.bucket_stats(pe, y, pc, pr, dts, edges, B_=300)
        gate["t3"]["buckets"] = tab
        mm = t3["methods"]
        md += ["## T3 zone hold model P(hold | touch) (walk-forward 2010-2026, real zones)", "",
               "| model | n | mean p | hold rate | ECE (pp) | gap (pp) | Brier | log-loss |", "|---|---|---|---|---|---|---|---|"]
        for nm, r in mm.items():
            md.append(f"| {nm} | {r['n']:,} | {pct(r['mean_p'])} | {pct(r['mean_y'])} | {pct(r['ece'], 2)} | {pct(r['gap'], 2)} | {r['brier']:.4f} | {r['logloss']:.4f} |")
        for k2, r in (t3.get("control_self") or {}).items():
            md.append(f"| control model on control bands ({k2}) | {r['n']:,} | {pct(r['mean_p'])} | {pct(r['mean_y'])} | {pct(r['ece'], 2)} | {pct(r['gap'], 2)} | {r['brier']:.4f} | {r['logloss']:.4f} |")
        reg = t3["regimes"].get(kind, {})
        md += ["", f"### T3 regimes ({kind})", "", "| regime | n | gap (pp) | ECE (pp) |", "|---|---|---|---|"]
        worst, worst_nm = 0.0, None
        for nm, x in reg.items():
            md.append(f"| {nm} | {x['n']:,} | {pct(x['gap'], 2)} | {pct(x['ece'], 2)} |")
            if nm != "all" and x["n"] >= REGIME_MIN_N and abs(x["gap"] or 0) > worst:
                worst, worst_nm = abs(x["gap"]), nm
        r = mm[f"real_{kind}"]
        beats = all(r["brier"] < mm[b]["brier"] and r["logloss"] < mm[b]["logloss"] for b in ("climatology", "strength_isotonic"))
        ok = r["ece"] <= ECE_MAX and worst <= REGIME_MAX and beats and leak_ok
        gate["outputs"]["t3_hold"] = {"verdict": "pass" if ok else "fail", "ece": r["ece"], "max_regime_gap": worst, "worst_regime": worst_nm,
                                      "beats_baselines": beats, "brier": r["brier"], "logloss": r["logloss"],
                                      "brier_climatology": mm["climatology"]["brier"], "brier_strength": mm["strength_isotonic"]["brier"],
                                      "reliability": t3["reliability"].get(f"real_{kind}"),
                                      "summary": f"ECE {pct(r['ece'], 2)} pp, worst regime {worst_nm} {pct(worst, 1)} pp, Brier {r['brier']:.4f} "
                                                 f"vs climatology {mm['climatology']['brier']:.4f} / old strength {mm['strength_isotonic']['brier']:.4f}"}
        ed = t3["edge"][kind]
        rm = ed["realised_minus_ctrl_model"]
        bctrl = (t3.get("brier_diff") or {}).get(f"p_ctrl_{kind}") or {}
        # the edge output is the per-zone bucket flag: it passes when the level features add skill over the random-
        # control model (Brier difference CI < 0) and every bucket's predicted edge is within 2.5 pp of its realised
        # edge; the AVERAGE edge (rm) is reported as is (it may be negative: zones hold less often than random bands)
        cal_ok = all(abs((b.get("pred_edge") or 0) - (b.get("realised_edge") or 0)) <= ECE_MAX for b in tab if b.get("realised_edge") is not None)
        any_sig = any(b.get("significant") for b in tab)
        edge_ok = (bctrl.get("hi") is not None and bctrl["hi"] < 0) and cal_ok and any_sig and leak_ok
        gate["outputs"]["t3_edge"] = {"verdict": "pass" if edge_ok else "fail", "edge_significant": bool(edge_ok),
                                      "realised_edge": rm, "raw_rate_diff": ed.get("raw_real_minus_ctrl_rate"), "bucket_calibrated": cal_ok,
                                      "brier_diff_vs_ctrl_model": bctrl, "beats_baselines": bool(bctrl.get("hi") is not None and bctrl["hi"] < 0),
                                      "summary": f"realised edge vs the random-control model {pct(rm['est'], 2)} pp "
                                                 f"[{pct(rm.get('lo'), 2)}, {pct(rm.get('hi'), 2)}]"}
        md += ["", "### T3 edge vs random control bands", "",
               f"- realised hold - P_control (real zones, OOS): {pct(rm['est'], 2)} pp, 95% CI [{pct(rm.get('lo'), 2)}, {pct(rm.get('hi'), 2)}] (month blocks)",
               f"- raw hold rate real - control bands: {pct(ed['raw_real_minus_ctrl_rate']['est'], 2)} pp, CI [{pct(ed['raw_real_minus_ctrl_rate'].get('lo'), 2)}, {pct(ed['raw_real_minus_ctrl_rate'].get('hi'), 2)}]",
               f"- hold rate real {pct(ed['hold_rate_real'])}%, control {pct(ed['hold_rate_ctrl'])}%; Brier(real model) - Brier(control model on real zones): "
               f"{bctrl.get('est', float('nan')):.5f} [{bctrl.get('lo') or float('nan'):.5f}, {bctrl.get('hi') or float('nan'):.5f}]", "",
               "| predicted-edge bucket | n | pred edge (pp) | realised edge (pp) | 95% CI | hold rate | significant |", "|---|---|---|---|---|---|---|"]
        for b in tab:
            if b.get("realised_edge") is None:
                continue
            md.append(f"| {b['bucket'] + 1} | {b['n']:,} | {pct(b['pred_edge'], 2)} | {pct(b['realised_edge'], 2)} | [{pct(b['ci_lo'], 2)}, {pct(b['ci_hi'], 2)}] | "
                      f"{pct(b['hold_rate'])}% | {b['significant']} |")
        rel = {nm: t3["reliability"].get(nm) or [] for nm in (f"real_{kind}", "climatology", "strength_isotonic", f"ctrl_model_{kind}")}
        (rd / "reliability_t3_hold.svg").write_text(svg_reliability(rel, "T3 P(hold | touch) reliability"), encoding="utf-8")
    # ------------------------------------------------------------------ T4
    if t4:
        sc = t4["scores"]["technical_score"]
        gate["t4"]["edge"], gate["t4"]["ic"] = {}, {}
        md += ["", "## T4 technical score -> forward excess return (walk-forward deciles 2010-2026)", ""]
        sat = t4.get("saturation") or {}
        md += ["Sub-score saturation (share of origins at exactly 100 / 0; distinct values):", "",
               "| sub-score | at 100 | at 0 | distinct | percentile >= 95 |", "|---|---|---|---|---|"]
        for c, v in sat.items():
            md.append(f"| {c} | {pct(v['share_100'])}% | {pct(v['share_0'])}% | {v['n_distinct']} | {pct(v['pct_share_ge_95'])}% |")
        for scn, block in t4["scores"].items():
            for h in ("5", "20", "60"):
                r = block.get(h)
                if not r:
                    continue
                ic, mono = r["ic"], r["monotonicity"]
                sp = mono.get("spread_ci") or {}
                md += ["", f"### {scn}, {h} sessions: IC {ic.get('mean_ic', float('nan')):.4f} (t {ic.get('t_stat') or float('nan'):.2f}, "
                           f"{ic.get('n_nonoverlap')} non-overlapping dates), Spearman(decile, mean) {mono.get('spearman') or float('nan'):.2f}, "
                           f"top-bottom {pct(sp.get('est'), 2)}% [{pct(sp.get('lo'), 2)}, {pct(sp.get('hi'), 2)}], edge_significant {r['edge_significant']}",
                       "", "| decile | n | score | median | IQR | hit rate | median CI |", "|---|---|---|---|---|---|---|"]
                for d in r["table"]:
                    md.append(f"| {d['decile']} | {d['n']:,} | {d['score_mean']:.1f} | {pct(d['median'], 2)}% | {pct(d['q25'], 2)}% .. {pct(d['q75'], 2)}% | "
                              f"{pct(d['hit_rate'])}% | [{pct(d['median_ci'][0], 2)}, {pct(d['median_ci'][1], 2)}] |")
                me = r.get("mapping_error") or {}
                md.append(f"\nMapping error (train-period bucket stats vs the test year): hit-rate MAE {pct(me.get('hit_mae'), 2)} pp, "
                          f"median MAE {pct(me.get('median_mae'), 3)} pp. Regimes: " +
                          "; ".join(f"{k}: IC {v['ic'] if v['ic'] is None else round(v['ic'], 4)}, top-bottom {pct(v['top_minus_bottom_mean'], 2)}%"
                                    for k, v in r["regimes"].items()))
        for h in ("5", "20", "60"):
            r = sc.get(h)
            if not r:
                continue
            me = r.get("mapping_error") or {}
            # the bucket mapping must be calibrated AND beat the unconditional baseline (an out-of-sample edge)
            ok = (me.get("hit_mae") or 1) <= 0.03 and bool(r["edge_significant"]) and leak_ok
            gate["t4"]["edge"][f"technical_score:{h}"] = bool(r["edge_significant"])
            gate["t4"]["ic"][f"technical_score:{h}"] = r["ic"]
            gate["outputs"][f"t4_h{h}"] = {"verdict": "pass" if ok else "fail", "ece": me.get("hit_mae"), "edge_significant": bool(r["edge_significant"]),
                                           "beats_baselines": bool(r["edge_significant"]), "ic": r["ic"], "monotonicity": r["monotonicity"],
                                           "summary": f"bucket hit-rate mapping MAE {pct(me.get('hit_mae'), 2)} pp; IC {r['ic'].get('mean_ic', 0):.4f} "
                                                      f"(t {r['ic'].get('t_stat') or 0:.2f}); edge {'significant' if r['edge_significant'] else 'not significant'}"}
    # ------------------------------------------------------------------ leakage + econ
    if leak:
        md += ["", "## Leakage", "", f"passed: {leak.get('passed')}", ""]
        for c in leak.get("causal", []):
            md.append(f"- causal {c['region']} {c['security_id']}: passed {c['passed']} (" + ", ".join(f"{x['cut_date']}: {len(x['violations'])} violations" for x in c["cuts"]) + ")")
        for c in leak.get("causal_peek", []):
            md.append(f"- peeking feature {c['security_id']}: caught {c['caught']} ({c['violations'][:1]})")
        for k2, v in (leak.get("permutation") or {}).items():
            md.append(f"- permutation {k2}: Brier skill {v['brier_skill']:.4f} (true labels {v.get('true_label_brier_skill', float('nan')):.4f}), n_test {v['n_test']:,}")
        icp = (leak.get("ic_permutation") or {}).get("technical_score_h20") or {}
        md.append(f"- T4 IC vs permuted returns (20d): {icp.get('mean_ic')}; true IC {(leak.get('ic_true') or {}).get('mean_ic')}")
    if econ:
        md += ["", "## Economic check (Backtest Lab engine, costs on, US mvp)", "",
               "| config | Sharpe | CAGR | max DD | trades | hit rate | mean trade | exits stop / target / time | DSR |", "|---|---|---|---|---|---|---|---|---|"]
        for k2, v in econ.get("configs", {}).items():
            ds = (econ.get("dsr") or {}).get(k2, {})
            md.append(f"| {k2} | {v['sharpe']:.2f} | {pct(v['cagr'])}% | {pct(v['max_drawdown'])}% | {v['n_trades']:,} | {pct(v['trade_hit_rate'])}% | "
                      f"{pct(v['mean_trade_ret'], 2)}% | {v['exits_stop']} / {v['exits_target']} / {v['exits_time']} | {ds.get('dsr', float('nan')):.2f} |")
        md += ["", f"PBO over {len(econ.get('configs', {}))} configs: {econ.get('pbo')}", "", "| paired difference | ann. diff | t |", "|---|---|---|"]
        for k2, v in (econ.get("paired") or {}).items():
            md.append(f"| {k2} | {pct(v['ann_diff'], 2)}% | {v['t'] if v['t'] is None else round(v['t'], 2)} |")
    (rd / "gate.json").write_text(json.dumps(gate, indent=1, default=str), encoding="utf-8")
    (rd / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    ST.update(s, t6={"gate": {k: v["verdict"] for k, v in gate["outputs"].items()}, "report": str(rd / "summary.md")})
    print(json.dumps({k: v["verdict"] for k, v in gate["outputs"].items()}, indent=1))


if __name__ == "__main__":
    main()
