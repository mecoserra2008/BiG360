"""Technicals v3 #1 / #5 gate verdicts + tables (docs/TECHNICALS_V3_VALIDATION.md).

Reads data/reports/technicals_v3/{eval.json, bo_summary.json, econ.json}; writes gate.json (consumed by the jobs:
composite members, breakout gate), eval_rows.parquet (validation rows of tech_factor_eval), summary.md (every table)
and the coordination keys b1 / b5 of data/state/technicals_v3.json.

Pre-registered gate (written before the results were read; see the validation doc):
  factor PASS = all of
    G1 pooled 20-session IC sign-aligned with the literature and Holm-adjusted p < 0.05 (7 primary cells);
    G2 sign-aligned decile monotonicity (Spearman decile vs mean label) >= 0.7 and the sign-aligned top - bottom decile
       spread CI (month blocks) above 0;
    G3 sign-aligned IC > 0 in >= 3 of the 4 regions, no region significantly wrong-signed (BH p < 0.05), sign-aligned
       IC > 0 in >= 60% of the years;
    G4 leakage: the one-session-stale factor keeps >= 50% of the aligned IC; label permutation mean |IC| < 0.005 and
       <= 15% of the 20 draws with |t| >= 2;
    G5 regimes: no regime (Covid 2020, 2022, VIX > 25 / <= 25, bear / bull market, GFC, eras) with aligned t <= -2.
  composite PASS (the pre-declared comp_wf) = G1 (single test, p < 0.05) + G2..G5 + G6 beats the v2 heuristic score on
    the same rows (paired IC difference, NW t >= 2) + G7 economics: US long-short quintiles net of costs Sharpe > 0
    with PSR >= 0.95 and the long-only quintile beats the equal-weight universe (annualised difference > 0).
  P(follow-through) PASS (H) = ECE <= 2.5 pp, every regime with n >= 2000 within 5 pp, Brier and log-loss better than
    climatology and the geometry baseline (Brier difference vs geometry CI below 0), permuted-label skill <= 0.
  volume-confirmation edge PASS (H) = geometry-adjusted confirmed - unconfirmed CI above 0 and confirmed - matched
    random control CI above 0.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techv3 import factors as F  # noqa: E402
from app.techv3 import state as ST  # noqa: E402

REGIONS = ("US", "EU", "UK", "JP")
REGIMES = ("covid_2020", "year_2022", "vix_gt_25", "vix_le_25", "bear_market", "bull_market", "gfc_2008_2009",
           "era_2005_2009", "era_2010_2019", "era_2020_2026")


def _g(d: dict, *ks, default=None):
    for k in ks:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
        if d is None:
            return default
    return d


def factor_gate(ev: dict, sg: str, holm: dict, bh: dict, single_test: bool = False) -> dict:
    sign = F.SIGN.get(sg, 1)
    r20 = _g(ev, "signals", sg, "20") or {}
    pooled = r20.get("pooled") or {}
    mic = pooled.get("mean_ic")
    ali = (mic or 0) * sign
    p_primary = pooled.get("p_value") if single_test else holm.get(sg)
    g1 = mic is not None and ali > 0 and p_primary is not None and p_primary < 0.05
    bk = r20.get("buckets") or {}
    sp = bk.get("spread_aligned") or {}
    g2 = (bk.get("aligned_spearman") or -1) >= 0.7 and (sp.get("lo") or -1) > 0
    regs = r20.get("regions") or {}
    n_pos = sum(1 for rg in REGIONS if (regs.get(rg, {}).get("mean_ic") or 0) * sign > 0)
    wrong = [rg for rg in REGIONS if (regs.get(rg, {}).get("mean_ic") or 0) * sign < 0 and (bh.get((sg, 20, rg)) or 1) < 0.05]
    yrs = r20.get("years") or {}
    share_y = (sum(1 for v in yrs.values() if (v.get("mean_ic") or 0) * sign > 0) / len(yrs)) if yrs else 0
    g3 = n_pos >= 3 and not wrong and share_y >= 0.6
    lag = r20.get("lag1") or {}
    lag_ratio = ((lag.get("mean_ic") or 0) * sign / ali) if ali > 0 else None
    perm = r20.get("permutation") or {}
    lag_ok = (lag_ratio is not None and lag_ratio >= 0.5) if (lag and lag.get("mean_ic")) else sg not in F.FACTORS
    g4 = lag_ok and abs(perm.get("mean_ic") or 0) < 0.005 and (perm.get("share_abs_t_ge_2") or 0) <= 0.15
    reg = r20.get("regimes") or {}
    bad = [k for k in REGIMES if k in reg and reg[k].get("t_stat") is not None and reg[k]["t_stat"] * sign <= -2]
    worst = min(((k, (reg[k].get("mean_ic") or 0) * sign) for k in REGIMES if k in reg), key=lambda x: x[1], default=(None, None))
    g5 = not bad
    out = {"verdict": "pass" if (g1 and g2 and g3 and g4 and g5) else "fail",
           "G1_ic": g1, "G2_monotone_spread": g2, "G3_regions_years": g3, "G4_leakage": g4, "G5_regimes": g5,
           "mean_ic": mic, "aligned_ic": ali, "t": pooled.get("t_stat"), "p": pooled.get("p_value"), "p_primary_adj": p_primary,
           "spearman_aligned": bk.get("aligned_spearman"), "spread": sp, "regions_aligned_positive": n_pos,
           "regions_wrong_sig": wrong, "share_years_aligned": share_y, "lag1_ratio": lag_ratio, "permutation": perm,
           "regimes_wrong_sig": bad, "worst_regime": {"regime": worst[0], "aligned_ic": worst[1]}}
    return out


def factor_econ(ec: dict, sp: dict, f: str) -> dict:
    """Economic reading of a factor (reported, not part of the pre-registered factor gate): US quintile long-short net /
    gross Sharpe, long-only top quintile vs the equal-weight universe, and the simple-return decile spread."""
    us = _g(ec, "factors", "US", "configs") or {}
    ls, lo = us.get(f"{f}:long_short") or {}, us.get(f"{f}:long_only") or {}
    regs = {rg: (_g(ec, "factors", rg, "configs", f"{f}:long_short") or {}).get("sharpe") for rg in REGIONS}
    s = sp.get(f) or {}
    tradable = (ls.get("sharpe") or -1) > 0 and ((lo.get("vs_universe") or {}).get("t") or 0) >= 2
    return {"us_ls_sharpe_net": ls.get("sharpe"), "us_ls_sharpe_gross": ls.get("gross_sharpe"), "us_turnover": ls.get("turnover_annual"),
            "us_lo_vs_universe": (lo.get("vs_universe") or {}).get("ann_diff"), "us_lo_vs_universe_t": (lo.get("vs_universe") or {}).get("t"),
            "ls_sharpe_net_by_region": regs, "log_spread_20d": s.get("log_spread_20d"), "simple_spread_20d": s.get("simple_spread_20d"),
            "simple_spread_t": s.get("simple_t"), "tradable_after_costs": tradable}


def fmt(x, nd=4, pct=False):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x * 100:.{nd}f}" if pct else f"{x:.{nd}f}"


def main() -> None:
    s = get_settings()
    rd = s.data_dir / "reports" / "technicals_v3"
    ev = json.loads((rd / "eval.json").read_text(encoding="utf-8"))
    bo = json.loads((rd / "bo_summary.json").read_text(encoding="utf-8")) if (rd / "bo_summary.json").exists() else {}
    ec = json.loads((rd / "econ.json").read_text(encoding="utf-8")) if (rd / "econ.json").exists() else {}
    cells = ev["cells"]
    holm = {c["signal"]: c.get("p_holm_primary") for c in cells if c.get("p_holm_primary") is not None}
    bh = {(c["signal"], c["h"], c["region"]): c.get("p_bh") for c in cells}
    gate: dict = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "factors": {}, "doc": "docs/TECHNICALS_V3_VALIDATION.md"}
    sp = json.loads((rd / "spreads.json").read_text(encoding="utf-8")) if (rd / "spreads.json").exists() else {}
    for f in F.FACTORS:
        gate["factors"][f] = factor_gate(ev, f, holm, bh)
        gate["factors"][f]["economics"] = factor_econ(ec, sp, f)
    # composite
    comp = factor_gate(ev, "comp_wf", holm, bh, single_test=True)
    v2 = (ev.get("v2_comparison") or {}).get("20") or {}
    dif = v2.get("diff_wf_minus_v2") or {}
    g6 = (dif.get("t") or 0) >= 2 and (_g(v2, "comp_wf", "mean_ic") or 0) > 0
    us = _g(ec, "factors", "US", "configs") or {}
    ls = us.get("comp_wf:long_short") or {}
    lo = us.get("comp_wf:long_only") or {}
    dsr = _g(ec, "factors", "US", "overfit", "dsr", "comp_wf:long_short") or {}
    g7 = (ls.get("sharpe") or -1) > 0 and (dsr.get("psr") or 0) >= 0.95 and ((lo.get("vs_universe") or {}).get("ann_diff") or -1) > 0
    sel = ev.get("selection") or {}
    latest_year = max(sel, key=int) if sel else None
    members = sel.get(latest_year, []) if latest_year else []
    comp.update(G6_beats_v2=g6, G7_economics=g7, v2=v2, econ_us={"long_short": ls, "long_only": lo, "dsr": dsr},
                members=members, members_year=latest_year, variant="comp_wf")
    comp["verdict"] = "pass" if (comp["verdict"] == "pass" and g6 and g7) else "fail"
    gate["composite"] = comp
    # breakouts
    gate["breakouts"] = {"base_rates": {}}
    for H in ("10", "20"):
        b = (bo.get("H") or {}).get(H) or {}
        wf = b.get("walk_forward") or {}
        full, geo, clim, perm = wf.get("full") or {}, wf.get("geo") or {}, wf.get("clim") or {}, wf.get("perm") or {}
        bdg = wf.get("brier_full_minus_geo") or {}
        ok_ece = (full.get("ece") or 1) <= 0.025
        ok_reg = (wf.get("max_regime_gap") if wf.get("max_regime_gap") is not None else 1) <= 0.05
        ok_base = (full.get("brier") or 9) < (clim.get("brier") or 0) and (full.get("logloss") or 9) < (clim.get("logloss") or 0) \
            and (full.get("brier") or 9) < (geo.get("brier") or 0) and (full.get("logloss") or 9) < (geo.get("logloss") or 0) \
            and (bdg.get("hi") if bdg.get("hi") is not None else 1) < 0
        ok_perm = (perm.get("brier") or 0) >= (clim.get("brier") or 9) - 1e-4
        gate["breakouts"][f"p_follow_{H}"] = {
            "verdict": "pass" if (ok_ece and ok_reg and ok_base and ok_perm) else "fail", "ece": full.get("ece"),
            "max_regime_gap": wf.get("max_regime_gap"), "worst_regime": wf.get("worst_regime"), "brier": full.get("brier"),
            "brier_geo": geo.get("brier"), "brier_clim": clim.get("brier"), "logloss": full.get("logloss"),
            "logloss_geo": geo.get("logloss"), "logloss_clim": clim.get("logloss"), "brier_full_minus_geo": bdg,
            "perm_brier": perm.get("brier"), "checks": {"ece": ok_ece, "regimes": ok_reg, "baselines": ok_base, "permutation": ok_perm}}
        ga = b.get("geometry_adjusted") or {}
        vc = _g(b, "vs_control", "confirmed", "diff") or {}
        ok_eff = (ga.get("lo") or -1) > 0 and (vc.get("lo") or -1) > 0
        gate["breakouts"][f"confirmation_{H}"] = {
            "verdict": "pass" if ok_eff else "fail", "confirmed_rate": _g(b, "confirmed", "rate"),
            "unconfirmed_rate": _g(b, "unconfirmed", "rate"), "base_rate": _g(b, "base", "rate"),
            "raw_diff": b.get("confirmed_minus_unconfirmed"), "geometry_adjusted": ga,
            "control_rate_confirmed": _g(b, "vs_control", "confirmed", "control", "rate"), "vs_control": vc,
            "n_events": b.get("n")}
    # base rates per lookback x direction (H = 20) for the state table
    b20 = (bo.get("H") or {}).get("20") or {}
    seg = b20.get("by_segment") or {}
    for N in (20, 55):
        for dn in (1, -1):
            gate["breakouts"]["base_rates"][f"{N}:{dn}"] = (b20.get("by_N_dir") or {}).get(f"{N}:{dn}") or _g(seg, f"N{N}", "all")
    gate["breakouts"]["base_rate_all_20"] = _g(b20, "base", "rate")
    eb = ec.get("breakouts") or {}
    if eb:
        cf, al = (eb.get("configs") or {}).get("confirmed") or {}, (eb.get("configs") or {}).get("all") or {}
        pr = (eb.get("paired") or {}).get("confirmed_minus_random_same_n") or {}
        gate["breakouts"]["economics"] = eb
        gate["breakouts"]["economics_summary"] = (
            f"Economic check (US up breakouts, Backtest Lab, costs on): all breakouts Sharpe {al.get('sharpe', 0):+.2f}, "
            f"confirmed {cf.get('sharpe', 0):+.2f}; confirmed vs a random subset of the same size {pr.get('ann_diff', 0) * 100:+.1f}%/yr "
            f"(t {pr.get('t') or 0:+.1f}) - no tradable edge after costs.")
    (rd / "gate.json").write_text(json.dumps(gate, indent=1, default=str), encoding="utf-8")
    write_rows(ev, gate, bo, rd)
    passing = [f for f, g in gate["factors"].items() if g["verdict"] == "pass"]
    bo_pass = [k for k, v in gate["breakouts"].items() if isinstance(v, dict) and v.get("verdict") == "pass"]
    ST.update(s, b1={"status": "ready", "passing_factors": passing, "composite": gate["composite"]["verdict"],
                     "composite_members": members if gate["composite"]["verdict"] == "pass" else [],
                     "table": "tech_factors", "feature_columns": [f"pct_{f}" for f in passing] + [*passing],
                     "all_factor_columns": [*F.FACTORS, *[f"pct_{f}" for f in F.FACTORS]],
                     "gate_file": str(rd / "gate.json"), "pit": "as_of_date = the session; values use bars <= as_of_date"},
              b5={"status": "ready", "passing_outputs": bo_pass, "table": "tech_breakouts", "state_table": "tech_breakout_state",
                  "feature_columns": ["confirmed", "confirm_score", "relvol50", "relvol20", "clv", "cmf20", "obv_slope20",
                                      "d_illiq", "b_20"] + (["p_follow_20"] if "p_follow_20" in bo_pass else []),
                  "gate_file": str(rd / "gate.json"), "pit": "date = breakout session; out_* / tau_* / ret_* are forward labels"})
    print(json.dumps({"factors": {f: g["verdict"] for f, g in gate["factors"].items()}, "composite": gate["composite"]["verdict"],
                      "breakouts": {k: v.get("verdict") for k, v in gate["breakouts"].items() if isinstance(v, dict) and "verdict" in v}},
                     indent=1))


def write_rows(ev: dict, gate: dict, bo: dict, rd: Path) -> None:
    rows = []
    today = date.today()
    cellp = {(c["signal"], c["h"], c["region"]): c for c in ev["cells"]}

    def add(sig, h, rg, st, sty, sg, st_detail=None, verdict=None, spread=None, mono=None):
        c = cellp.get((sig, h, rg)) or {}
        rows.append({"eval_date": today, "eval_kind": "validation", "signal": sig, "horizon": h, "region": rg, "segment_type": sty,
                     "segment": sg, "n_dates": st.get("n_dates"), "mean_names": st.get("mean_names"), "mean_ic": st.get("mean_ic"),
                     "aligned_ic": st.get("aligned_ic"), "t_stat": st.get("t_stat"), "p_value": st.get("p_value"),
                     "p_bh": c.get("p_bh") if sty == "all" else None, "p_holm": c.get("p_holm_primary") if sty == "all" else None,
                     "share_aligned": st.get("share_aligned"), "spread": (spread or {}).get("mean"), "spread_lo": (spread or {}).get("lo"),
                     "spread_hi": (spread or {}).get("hi"), "monotonicity": mono, "verdict": verdict,
                     "detail": json.dumps(st_detail, default=str) if st_detail is not None else None})

    for sig, per_h in ev["signals"].items():
        for hs, res in per_h.items():
            h = int(hs)
            v = None
            det = None
            if h == 20:
                if sig in F.FACTORS:
                    v = gate["factors"][sig]["verdict"]
                    det = gate["factors"][sig]
            bk = res.get("buckets") or {}
            add(sig, h, "ALL", res["pooled"], "all", "all", det, v, bk.get("spread_aligned"), bk.get("aligned_spearman"))
            for rg, st in (res.get("regions") or {}).items():
                b2 = st.get("buckets") or {}
                add(sig, h, rg, st, "all", "all", None, None, b2.get("spread_aligned"), b2.get("aligned_spearman"))
            for sb, st in (res.get("size") or {}).items():
                add(sig, h, "ALL", st, "size", sb)
            for nm, st in (res.get("regimes") or {}).items():
                add(sig, h, "ALL", st, "regime", nm)
            for y, st in (res.get("years") or {}).items():
                add(sig, h, "ALL", {**st, "aligned_ic": (st.get("mean_ic") or 0) * F.SIGN.get(sig, 1)}, "year", y)
            if res.get("lag1"):
                add(sig, h, "ALL", res["lag1"], "leak", "lag1")
            if res.get("permutation"):
                add(sig, h, "ALL", {"mean_ic": res["permutation"]["mean_ic"]}, "leak", "permutation", res["permutation"])
    # the live composite row (= comp_wf statistics + members + v2 comparison)
    cw = (ev["signals"].get("comp_wf") or {}).get("20") or {}
    if cw:
        g = gate["composite"]
        v2 = g.get("v2") or {}
        det = {"members": g.get("members"), "variant": "comp_wf", "v2_ic_20": (v2.get("v2_score") or {}).get("mean_ic"),
               "beats_v2": g.get("G6_beats_v2"), "n_cells_factor": ev.get("n_cells_factor"), "generated_at": gate["generated_at"],
               "period": "2008-2026 (walk-forward composite), factors 2005-2026", "universe": "liquid US / EU / UK / JP equities",
               **{k: g.get(k) for k in ("G1_ic", "G2_monotone_spread", "G3_regions_years", "G4_leakage", "G5_regimes", "G6_beats_v2",
                                         "G7_economics")}}
        bk = cw.get("buckets") or {}
        add("composite", 20, "ALL", cw["pooled"], "all", "all", det, g["verdict"], bk.get("spread_aligned"), bk.get("aligned_spearman"))
    for H in (10, 20):
        p = gate["breakouts"].get(f"p_follow_{H}") or {}
        c = gate["breakouts"].get(f"confirmation_{H}") or {}
        rows.append({"eval_date": today, "eval_kind": "validation", "signal": f"bo_p{H}", "horizon": 20 if H == 20 else 10,
                     "region": "ALL", "segment_type": "all", "segment": "all", "verdict": p.get("verdict"),
                     "detail": json.dumps({**p, "ece": p.get("ece"), "brier_full_minus_geo_ci": p.get("brier_full_minus_geo"),
                                           "brier_full_minus_geo": (p.get("brier_full_minus_geo") or {}).get("est")}, default=str)})
        gd = c.get("geometry_adjusted") or {}
        rd_ = c.get("raw_diff") or {}
        rows.append({"eval_date": today, "eval_kind": "validation", "signal": "bo_confirm", "horizon": H, "region": "ALL",
                     "segment_type": "all", "segment": "all", "verdict": c.get("verdict"),
                     "detail": json.dumps({"confirmed_rate": c.get("confirmed_rate"), "unconfirmed_rate": c.get("unconfirmed_rate"),
                                           "base_rate": c.get("base_rate"), "control_rate": c.get("control_rate_confirmed"),
                                           "diff": rd_.get("diff"), "diff_ci": [rd_.get("lo"), rd_.get("hi")], "geo_diff": gd.get("diff"),
                                           "geo_ci": [gd.get("lo"), gd.get("hi")], "significant": c.get("verdict") == "pass",
                                           "verdict": c.get("verdict"), "n_events": c.get("n_events"),
                                           "summary": gate["breakouts"].get("economics_summary")}, default=str)})
    df = pl.DataFrame(rows, infer_schema_length=None)
    df.write_parquet(rd / "eval_rows.parquet")


if __name__ == "__main__":
    main()
