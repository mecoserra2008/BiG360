"""Summarise the P4 raw results (data/reports/conformal/wf_env, wf_peer, econ.json) into markdown tables
(data/reports/conformal/summary.md) and walk-forward coverage rows for conformal_coverage (application *_wf).

    .venv/Scripts/python.exe scripts/conformal/report.py [--write-coverage]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.conformal import validation as W  # noqa: E402

OUT = W.report_dir()


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}"


def load(kind: str) -> pl.DataFrame:
    fs = sorted((OUT / "wf_env").glob(f"{kind}_*.parquet"))
    if not fs:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in fs], how="diagonal_relaxed")


def with_regimes(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        ((pl.col("year") == 2020) & pl.col("month").is_between(2, 6)).alias("covid"),
        (pl.col("year") == 2022).alias("rates22"))


def env_tables(df: pl.DataFrame, title: str, hz: tuple = (1, 5, 10, 20)) -> list[str]:
    lines = []
    if df.is_empty():
        return [f"_{title}: no results yet_"]
    df = with_regimes(df)
    for lvl in (90, 95):
        d = df.filter(pl.col("level") == lvl)
        ov = W.summarize(d, ["method", "mode"])
        yr = W.summarize(d, ["method", "mode", "year"])
        worst = yr.group_by("method", "mode").agg(pl.col("coverage").min().alias("worst_year"),
                                                   pl.col("year").sort_by("coverage").first().alias("worst_y"))
        cv = W.summarize(d.filter(pl.col("covid")), ["method", "mode"]).select("method", "mode", pl.col("coverage").alias("covid"))
        rt = W.summarize(d.filter(pl.col("rates22")), ["method", "mode"]).select("method", "mode", pl.col("coverage").alias("rates22"))
        hv = W.summarize(d.filter(pl.col("high_vix")), ["method", "mode"]).select("method", "mode", pl.col("coverage").alias("high_vix"))
        ev = W.summarize(d.filter(pl.col("event")), ["method", "mode"]).select("method", "mode", pl.col("coverage").alias("earn"))
        t = ov.join(worst, on=["method", "mode"], how="left").join(cv, on=["method", "mode"], how="left") \
            .join(rt, on=["method", "mode"], how="left").join(hv, on=["method", "mode"], how="left") \
            .join(ev, on=["method", "mode"], how="left").sort("w_proj")
        lines.append(f"\n**{title}, target {lvl}%** (coverage %, w_proj and per-horizon widths in log-return units)\n")
        hz_cols = " | ".join(f"cov h{h}" for h in hz)
        lines.append(f"| method | radius | n | joint cov | {hz_cols} | worst year | Covid 2020 | 2022 | VIX>25 | earnings window | w_proj | log-vol | unbounded |")
        lines.append("|" + "---|" * (12 + len(hz)))
        for r in t.iter_rows(named=True):
            hc = " | ".join(pct(r.get(f"coverage_{h}")) for h in (1, 5, 10, 20)[:len(hz)])
            lines.append(f"| {r['method']} | {r['mode']} | {int(r['n']):,} | {pct(r['coverage'])} | {hc} | "
                         f"{pct(r['worst_year'])} ({r['worst_y']}) | {pct(r['covid'])} | {pct(r['rates22'])} | {pct(r['high_vix'])} | "
                         f"{pct(r['earn'])} | {r['w_proj']:.4f} | {r['log_volume']:.3f} | {pct(r['share_unbounded'])} |")
    return lines


def marginal_tables(df: pl.DataFrame) -> list[str]:
    if df.is_empty():
        return []
    d0 = with_regimes(df.filter(pl.col("method").str.starts_with("m_") | (pl.col("method") == "atr_k2")))
    lines = []
    for lvl in (90, 95):
        d = d0.filter(pl.col("level") == lvl)
        ov = W.summarize(d, ["method", "mode"])
        cv = W.summarize(d.filter(pl.col("covid")), ["method", "mode"]).select(
            "method", "mode", *[pl.col(f"coverage_{h}").alias(f"cv{h}") for h in (1, 5, 10, 20)])
        hv = W.summarize(d.filter(pl.col("high_vix")), ["method", "mode"]).select(
            "method", "mode", *[pl.col(f"coverage_{h}").alias(f"hv{h}") for h in (1, 20)])
        r22 = W.summarize(d.filter(pl.col("rates22")), ["method", "mode"]).select(
            "method", "mode", *[pl.col(f"coverage_{h}").alias(f"r{h}") for h in (1, 20)])
        t = ov.join(cv, on=["method", "mode"], how="left").join(hv, on=["method", "mode"], how="left")
        t = t.join(r22, on=["method", "mode"], how="left").sort("width_20")
        lines.append(f"\n**marginal bands, target {lvl}%** (per-horizon coverage %, band width = 2 x half-width in log-return units; atr_k2 = heuristic 1.5/2/2.5 x ATR x sqrt(h), uncalibrated)\n")
        lines.append("| method | radius | cov h1 | cov h5 | cov h10 | cov h20 | width h1 | width h5 | width h10 | width h20 | Covid h1/h5/h10/h20 | 2022 h1/h20 | VIX>25 h1/h20 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in t.iter_rows(named=True):
            lines.append(f"| {r['method']} | {r['mode']} | " + " | ".join(pct(r[f"coverage_{h}"]) for h in (1, 5, 10, 20)) + " | "
                         + " | ".join(f"{r[f'width_{h}']:.4f}" for h in (1, 5, 10, 20)) + " | "
                         + "/".join(pct(r[f"cv{h}"]) for h in (1, 5, 10, 20)) + " | " + "/".join(pct(r[f"r{h}"]) for h in (1, 20))
                         + " | " + "/".join(pct(r[f"hv{h}"]) for h in (1, 20)) + " |")
    return lines


def env_by_region(df: pl.DataFrame, lvl: int = 90) -> list[str]:
    if df.is_empty():
        return []
    d = W.summarize(df.filter(pl.col("level") == lvl), ["method", "mode", "region"])
    piv = d.select("method", "mode", "region", "coverage").pivot(on="region", index=["method", "mode"], values="coverage")
    regs = [c for c in piv.columns if c not in ("method", "mode")]
    lines = [f"\n| method | radius | " + " | ".join(regs) + " |", "|" + "---|" * (2 + len(regs))]
    for r in piv.sort("method", "mode").iter_rows(named=True):
        lines.append(f"| {r['method']} | {r['mode']} | " + " | ".join(pct(r[x]) for x in regs) + " |")
    return lines


def env_by_year(df: pl.DataFrame, methods: list[tuple[str, str]], lvl: int = 90) -> list[str]:
    if df.is_empty():
        return []
    d = W.summarize(df.filter(pl.col("level") == lvl), ["method", "mode", "year"])
    years = sorted(d["year"].unique().to_list())
    lines = ["\n| method | " + " | ".join(str(y) for y in years) + " |", "|" + "---|" * (1 + len(years))]
    for m, mo in methods:
        x = d.filter((pl.col("method") == m) & (pl.col("mode") == mo))
        mp = dict(zip(x["year"].to_list(), x["coverage"].to_list(), strict=True))
        lines.append(f"| {m}/{mo} | " + " | ".join(pct(mp.get(y)) for y in years) + " |")
    return lines


def peer_tables() -> tuple[list[str], pl.DataFrame]:
    fs = [f for f in sorted((OUT / "wf_peer").glob("*.parquet")) if not f.name.startswith("signals_")]
    if not fs:
        return ["_peer walk-forward: no results yet_"], pl.DataFrame()
    df = pl.concat([pl.read_parquet(f) for f in fs], how="diagonal_relaxed")
    df = df.with_columns(((pl.col("year") == 2020) & pl.col("month").is_between(2, 6)).alias("covid"),
                         (pl.col("year") == 2022).alias("rates22"))

    def summ(d, by):
        return d.group_by(by).agg([pl.col(c).sum() for c in ("n", "cov", "cov_m", "n_m", "w_sum", "lv_sum", "n_w")]).with_columns(
            (pl.col("cov") / pl.col("n")).alias("coverage"), (pl.col("cov_m") / pl.col("n_m")).alias("member_cov"),
            (pl.col("w_sum") / pl.col("n_w")).alias("w_proj"), (pl.col("lv_sum") / pl.col("n_w")).alias("log_volume"),
            (1 - pl.col("n_w") / pl.col("n")).alias("unbounded"))

    lines = []
    for lvl in (90, 95):
        d = df.filter(pl.col("level") == lvl)
        ov = summ(d, ["filter", "mode"])
        yr = summ(d, ["filter", "mode", "year"]).group_by("filter", "mode").agg(
            pl.col("coverage").min().alias("worst_year"), pl.col("year").sort_by("coverage").first().alias("worst_y"))
        cv = summ(d.filter(pl.col("covid")), ["filter", "mode"]).select("filter", "mode", pl.col("coverage").alias("covid"))
        rt = summ(d.filter(pl.col("rates22")), ["filter", "mode"]).select("filter", "mode", pl.col("coverage").alias("rates22"))
        hv = summ(d.filter(pl.col("high_vix")), ["filter", "mode"]).select("filter", "mode", pl.col("coverage").alias("high_vix"))
        t = ov.join(yr, on=["filter", "mode"], how="left").join(cv, on=["filter", "mode"], how="left") \
            .join(rt, on=["filter", "mode"], how="left").join(hv, on=["filter", "mode"], how="left").sort("w_proj")
        lines.append(f"\n**B peer groups, target {lvl}%** (joint coverage of the N-member daily-return ellipsoid; member = per-coordinate coverage of its projection; w_proj in daily log-return units)\n")
        lines.append("| filter | radius | group-days | joint cov | member cov | worst year | Covid 2020 | 2022 | VIX>25 | w_proj | log-vol | unbounded |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for r in t.iter_rows(named=True):
            lines.append(f"| {r['filter']} | {r['mode']} | {int(r['n']):,} | {pct(r['coverage'])} | {pct(r['member_cov'])} | "
                         f"{pct(r['worst_year'])} ({r['worst_y']}) | {pct(r['covid'])} | {pct(r['rates22'])} | {pct(r['high_vix'])} | "
                         f"{r['w_proj']:.5f} | {r['log_volume']:.3f} | {pct(r['unbounded'])} |")
    for gk in ("industry", "supply_chain"):
        d = df.filter((pl.col("level") == 90) & (pl.col("group_kind") == gk))
        t = summ(d, ["filter", "mode"]).sort("w_proj")
        lines.append(f"\n_{gk} groups, 90%:_ " + "; ".join(f"{r['filter']}/{r['mode']} {pct(r['coverage'])} w={r['w_proj']:.5f}"
                                                     for r in t.iter_rows(named=True)))
    return lines, df


def wf_coverage_rows(env: pl.DataFrame, nb: pl.DataFrame, peer: pl.DataFrame) -> pl.DataFrame:
    rows = []
    now = datetime.now(UTC)
    asof = date.today()

    def add(app, flt, st, seg, hz, lvl, n, cov):
        if not n:
            return
        tgt = lvl / 100
        gap = 100 * (cov - tgt)
        rows.append({"as_of_date": asof, "application": app, "filter": flt, "segment_type": st, "segment": seg,
                     "horizon": hz, "level": str(lvl), "target": tgt, "n": int(n), "covered": int(round(cov * n)),
                     "coverage": cov, "gap_pp": gap,
                     "status": "thin" if n < 200 else ("ok" if abs(gap) <= 1.5 else ("under" if gap < 0 else "over")),
                     "window_start": date(2010, 1, 1), "window_end": date(2026, 10, 20), "model_based": True,
                     "computed_at": now})

    for app, df in (("envelope_wf", env), ("nextbar_wf", nb)):
        if df.is_empty():
            continue
        df = with_regimes(df)
        for lvl in (80, 90, 95):
            d = df.filter(pl.col("level") == lvl)
            for st, by, filt in (("overall", [], None), ("region", ["region"], None), ("sector", ["sector"], None),
                                 ("year", ["year"], None), ("vix", ["high_vix"], None), ("earnings", ["event"], None),
                                 ("regime", [], "covid"), ("regime", [], "rates22")):
                dd = d.filter(pl.col(filt)) if filt else d
                s = W.summarize(dd, ["method", "mode", *by])
                for r in s.iter_rows(named=True):
                    flt = r["method"] + ("+aci" if r["mode"] == "aci" else "")
                    seg = "all" if not by else str(r[by[0]])
                    if filt:
                        seg = {"covid": "covid_2020", "rates22": "rates_shock_2022"}[filt]
                    if st == "vix":
                        seg = "high_vix" if r["high_vix"] else "normal_vix"
                    if st == "earnings":
                        seg = "event" if r["event"] else "normal"
                    add(app, flt, st, seg, "joint", lvl, r["n"], r["coverage"])
                    if st == "overall" and app == "envelope_wf":
                        for h in (1, 5, 10, 20):
                            add(app, flt, st, seg, str(h), lvl, r["n"], r[f"coverage_{h}"])
    if not peer.is_empty():
        for lvl in (80, 90, 95):
            d = peer.filter(pl.col("level") == lvl)
            for st, by in (("overall", []), ("region", ["region"]), ("year", ["year"]), ("group_kind", ["group_kind"]),
                           ("vix", ["high_vix"])):
                g = d.group_by(["filter", "mode", *by]).agg(pl.col("n").sum(), pl.col("cov").sum())
                for r in g.iter_rows(named=True):
                    seg = "all" if not by else str(r[by[0]])
                    if st == "vix":
                        seg = "high_vix" if r["high_vix"] else "normal_vix"
                    add("peer_wf", r["filter"] + ("+aci" if r["mode"] == "aci" else ""), st, seg, "joint", lvl, r["n"], r["cov"] / r["n"])
    return pl.DataFrame(rows, infer_schema_length=None) if rows else pl.DataFrame()


def econ_tables() -> list[str]:
    f = OUT / "econ.json"
    if not f.exists():
        return ["_economic value: no results yet_"]
    e = json.loads(f.read_text(encoding="utf-8"))
    lines = [f"\nUniverse {e['universe']}, period {e['period']}, costs on (commission 1 bp + ADV-bucket half spread + "
             f"sqrt impact, borrow 50 / 300 bp), radius {e['radius_mode']}.\n"]
    for fam, title in (("i", "(i) breakout"), ("ii", "(ii) stops on 12-1 momentum"), ("iii", "(iii) peer break-away mean reversion")):
        d = e.get(fam) or {}
        if "note" in d:
            lines.append(f"_{title}: {d['note']}_")
            continue
        of = e.get(f"overfit_{fam}", {})
        lines.append(f"\n**{title}** (PBO across the {of.get('n_trials')} configurations: {of.get('pbo')})\n")
        lines.append("| config | events | trades | Sharpe | CAGR | vol | max DD | trade hit | mean trade | stops hit | cost drag/yr | DSR |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
        for k, v in sorted(d.items(), key=lambda kv: -(kv[1].get("sharpe") or -9)):
            ds = (of.get("dsr") or {}).get(k, {}).get("dsr")
            lines.append(f"| {k} | {v.get('n_events')} | {v.get('n_trades')} | {_f(v.get('sharpe'), 2)} | {pct(v.get('cagr'))} | "
                         f"{pct(v.get('vol'))} | {pct(v.get('max_drawdown'))} | {pct(v.get('trade_hit_rate'))} | "
                         f"{pct(v.get('mean_trade_ret'))} | {v.get('stops_hit')} | {pct(v.get('cost_drag_ann'))} | {_f(ds, 3)} |")
    return lines


PAIRS = [("i:band_marginal_h5_hold20", "i:naive_high20_hold20"), ("i:band_marginal_h5_hold20", "i:naive_high55_hold20"),
         ("i:band_marginal_h20_hold20", "i:naive_high20_hold20"), ("i:band_joint_h5_hold20", "i:naive_high20_hold20"),
         ("ii:mom_stop_band95", "ii:mom_stop_atr3"), ("ii:mom_stop_band90", "ii:mom_stop_atr2"),
         ("ii:mom_stop_band90", "ii:mom_stop_none"), ("iii:peer_breakaway_z2.5_hold5", "iii:naive_fade_z2.5_hold5"),
         ("iii:peer_breakaway_z3.0_hold5", "iii:naive_fade_z3.0_hold5"), ("iii:peer_breakaway_z2.5_hold10", "iii:naive_fade_z2.5_hold10")]


def paired_tables() -> list[str]:
    """Paired daily return differences (conformal config minus its baseline): annualised mean, Sharpe of the
    difference, t-stat and PSR(diff Sharpe > 0) (Bailey & Lopez de Prado)."""
    import math

    import numpy as np

    from app.backtest import _engine as E

    f = OUT / "econ_returns.parquet"
    if not f.exists():
        return []
    r = pl.read_parquet(f)
    lines = ["\n**paired comparisons** (daily return of the conformal configuration minus its baseline, costs on)\n",
             "| conformal | baseline | ann. diff | Sharpe(diff) | t | PSR(diff > 0) |", "|---|---|---|---|---|---|"]
    for a_, b_ in PAIRS:
        x = r.filter(pl.col("config") == a_).select("date", pl.col("ret").alias("a"))
        y = r.filter(pl.col("config") == b_).select("date", pl.col("ret").alias("b"))
        j = x.join(y, on="date", how="inner")
        if j.height < 100:
            continue
        d = (j["a"] - j["b"]).to_numpy()
        st = E.sharpe_stats(d)
        if st["sr"] is None:
            continue
        p_ = E.psr(st["sr"], st["n"], st["skew"], st["kurtosis"], 0.0)
        lines.append(f"| {a_} | {b_} | {100 * d.mean() * 252:+.2f}% | {st['sr'] * math.sqrt(252):+.2f} | "
                     f"{st['sr'] * math.sqrt(st['n']):+.2f} | {p_:.3f} |")
    return lines


def _f(x, k):
    return "-" if x is None else f"{x:.{k}f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write-coverage", action="store_true")
    a = ap.parse_args()
    env, nb = load("joint"), load("nextbar")
    md = ["# Conformal P4 raw summary (generated by scripts/conformal/report.py)\n"]
    envj = env.filter(~pl.col("method").str.starts_with("m_")) if env.height else env
    md += ["## A. joint 1/5/10/20 envelope"] + env_tables(envj, "A joint envelope")
    md += ["\n## A. marginal single-horizon bands (each horizon calibrated alone)"] + marginal_tables(env)
    md += ["\n### by region (90%)"] + env_by_region(envj)
    md += ["\n### by year (90%)"] + env_by_year(env, [("ewma", "plain"), ("ewma", "aci"), ("garch", "plain"), ("garch", "aci"),
                                                      ("gru", "plain"), ("gru", "aci"), ("static", "aci"), ("atr_cal", "aci"), ("uni_ewma", "aci")])
    md += ["\n## A. next bar (H, L, C)"] + env_tables(nb, "A next bar", hz=(1, 5, 10))
    md.append("\n(next-bar columns cov h1/h5/h10 = high / low / close)")
    pl_lines, peer = peer_tables()
    md += ["\n## B. peer groups"] + pl_lines
    md += ["\n## Economic value"] + econ_tables() + paired_tables()
    (OUT / "summary.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))
    if a.write_coverage:
        from app.core.storage import write_table  # noqa: PLC0415

        cov = wf_coverage_rows(env, nb, peer)
        if cov.height:
            n = write_table(cov, "conformal_coverage", mode="upsert")
            print(f"\nconformal_coverage: {n} walk-forward rows written")


if __name__ == "__main__":
    main()
