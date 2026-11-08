"""Technicals v3 economic check in the Backtest Lab engine (app.backtest._engine; costs on; deflated Sharpe + PBO).

#1 factors (per region US / EU / UK / JP, USD returns): monthly rebalance on the last weekly origin of each month,
   LIQUID names, sector-neutral quintiles of the SIGN-ALIGNED percentile (100 - pct for the negative-sign factors),
   equal weight; long-short (top - bottom quintile, gross 2) and long-only (top quintile, vs the equal-weight
   universe of the same eligible names); 2008-01 -> today (the walk-forward composite's period; factors also
   2005 -> in eval.json). Costs: 1 bp commission, ADV-bucket half spread, square-root impact on $10M, borrow 50 /
   300 bp, FX 2 bp. Turnover = mean one-way turnover per rebalance x 12.
#5 breakouts (US, up breakouts, H = 20): entry at the next close, exit at the close where the trade return reaches
   +k sigma_H (target), falls below the level (close back inside: stop = 1 - level / close), or after 20 sessions;
   max 100 positions x 1%. Variants: all, confirmed, unconfirmed, model top 40% (walk-forward OOS P(follow-through)
   above its own year's 60th percentile), random subset of the same size as "confirmed".
Output: data/reports/technicals_v3/econ.json (+ econ_returns.parquet). State key econ.

    .venv/Scripts/python.exe scripts/technicals_v3/econ.py [--regions US,EU,UK,JP] [--skip-breakouts]
"""

from __future__ import annotations

import argparse
import gc
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

from app.backtest import _engine as E  # noqa: E402
from app.backtest import _panel as BP  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.techv3 import factors as F  # noqa: E402
from app.techv3 import panel as P  # noqa: E402
from app.techv3 import state as ST  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v3_b15" / "wf"
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0, "fx_bps": 2.0}
T0 = date(2008, 1, 1)
LOG = logging.getLogger("econ")


def sanitize(daily: pl.DataFrame) -> pl.DataFrame:
    """Data-error guard (the same rule as the factors, app.techv3.factors.MAX_ABS_LOGRET): a daily |log return| > 1
    (unit flips, bad prints) is set to 0 and the price path rebuilt from the cleaned returns."""
    d = daily.sort(["entity_id", "date"]).with_columns((pl.col("px") / pl.col("px").shift(1).over("entity_id")).log().alias("_lr"))
    d = d.with_columns(pl.when(pl.col("_lr").abs() > 1.0).then(0.0).otherwise(pl.col("_lr")).fill_null(0.0).fill_nan(0.0).alias("_lr"))
    LOG.info("sanitize: %d of %d daily returns with |log r| > 1 set to 0", int(d.select((pl.col("px") / pl.col("px").shift(1)
             .over("entity_id")).log().abs().gt(1.0).sum()).item()), d.height)
    d = d.with_columns((pl.col("px").first().over("entity_id") * pl.col("_lr").cum_sum().over("entity_id").exp()).alias("px"))
    return d.drop("_lr")


def month_end_panel(rg: str, comp: pl.DataFrame) -> pl.DataFrame:
    files = sorted((WF / rg).glob("f_[0-9][0-9][0-9][0-9].parquet"))
    lf = pl.scan_parquet(files).select("security_id", "date", "sector_id", "liquid", "adv20", *[f"pct_{f}" for f in F.FACTORS])
    dates = lf.select(pl.col("date").unique()).collect()["date"].sort()
    d = dates.to_frame().with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m"))
    # the last WEEKLY origin of each month = the last session of the month's last ISO week that ends in the month
    wk = d.with_columns(pl.col("date").dt.strftime("%G-%V").alias("w")).group_by("w").agg(pl.col("date").max()).sort("date")
    wk = wk.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("m")).group_by("m").agg(pl.col("date").max()).sort("date")
    keep = wk["date"].filter(wk["date"] >= T0)
    df = lf.filter(pl.col("date").is_in(keep.implode())).collect()
    al = {f: (pl.col(f"pct_{f}") if F.SIGN[f] > 0 else 100.0 - pl.col(f"pct_{f}")) for f in F.FACTORS}
    df = df.with_columns([al[f].alias(f"s_{f}") for f in F.FACTORS])
    if comp is not None and comp.height:
        c = comp.filter(pl.col("region") == rg).select("security_id", "date", "comp_wf", "comp_all")
        df = df.join(c, on=["security_id", "date"], how="left")
    return df


def run_rank(panel: pl.DataFrame, sig: str, px: E.PricePanel, side: str) -> E.SimResult:
    p = panel.select(pl.col("security_id").alias("entity_id"), "date", pl.col(sig).cast(pl.Float64).alias("signal"),
                     pl.col("liquid").alias("eligible"), pl.col("sector_id").alias("group_sector"),
                     pl.col("adv20").cast(pl.Float64).alias("adv_usd"), pl.col("vol"))
    spec = {"construction": {"side": side, "selection": "quantile", "quantiles": 5, "weighting": "equal",
                             "neutralize": "sector", "execution_lag_days": 1, "min_names": 20}, "costs": COSTS}
    return E.simulate_rank(p, px, spec, costs_on=True, record=False)


def stats_of(sim: E.SimResult, px: E.PricePanel) -> tuple[dict, np.ndarray]:
    rf = px.rf[sim.start_idx:] if px.rf is not None else None
    m = E.perf_metrics(sim.ret, sim.dates, rf=rf)
    to = [r["turnover"] for r in sim.rebalances[1:]] if sim.rebalances else []
    m["turnover_annual"] = float(np.mean(to) * 12) if to else None
    m["cost_drag_ann"] = float(sim.cost.mean() * 252)
    m["gross_sharpe"] = None
    g = sim.gross_ret
    if g.std() > 0:
        m["gross_sharpe"] = float((g - (rf if rf is not None else 0)).mean() / g.std() * math.sqrt(252))
    keys = ("sharpe", "gross_sharpe", "cagr", "vol", "max_drawdown", "turnover_annual", "cost_drag_ann", "skew", "kurtosis")
    return {k: m.get(k) for k in keys}, sim.ret


def paired(a: np.ndarray, b: np.ndarray) -> dict:
    L = min(len(a), len(b))
    x = a[-L:] - b[-L:]
    sd = x.std(ddof=1)
    return {"ann_diff": float(x.mean() * 252), "t": float(x.mean() / (sd / math.sqrt(L))) if sd > 0 else None, "n_days": int(L)}


def dsr_pbo(rets: dict[str, np.ndarray]) -> dict:
    names = list(rets)
    L = min(len(v) for v in rets.values())
    M = np.column_stack([rets[n][-L:] for n in names])
    srs = [E.sharpe_stats(M[:, j])["sr"] or 0.0 for j in range(M.shape[1])]
    sr_var = float(np.var(srs, ddof=1)) if len(srs) > 1 else 0.0
    out = {"pbo": E.pbo_cscv(M, 10).get("pbo"), "n_trials": len(names), "dsr": {}}
    for j, n in enumerate(names):
        st = E.sharpe_stats(M[:, j])
        if st["sr"] is None:
            continue
        ds = E.deflated_sharpe(st["sr"], st["n"], st["skew"], st["kurtosis"], len(names), sr_var)
        out["dsr"][n] = {"dsr": ds["dsr"], "psr": ds["psr"], "sr_ann": st["sr"] * math.sqrt(252)}
    return out


def factor_econ(regions: list[str], s, res: dict, allret: dict) -> None:
    comp_path = WF / "composite_weekly.parquet"
    comp = pl.read_parquet(comp_path) if comp_path.exists() else None
    for rg in regions:
        t0 = time.time()
        pan = month_end_panel(rg, comp)
        ids = sorted(pan.filter(pl.col("liquid"))["security_id"].unique().to_list())
        con = P.lake(["prices_daily", "macro_factors"], s, mem="900MB")
        try:
            daily = sanitize(BP._bp_load_prices(con, ids, date(2007, 1, 1), date.today(), "USD").filter(pl.col("px") > 0))  # noqa: SLF001
            px = E.price_panel_from_long(daily.select("entity_id", "date", "px"))
            px.rf = BP.rf_daily(con, px.dates)
        finally:
            con.close()
        vol = BP._bp_price_features(daily, [], None).select(pl.col("entity_id").alias("security_id"), "date", "vol")  # noqa: SLF001
        pan = pan.join(vol, on=["security_id", "date"], how="left")
        del daily, vol
        gc.collect()
        LOG.info("%s panel %d rows, px %d x %d (%.0fs)", rg, pan.height, px.T, len(px.ids), time.time() - t0)
        sigs = [f"s_{f}" for f in F.FACTORS] + [c for c in ("comp_wf", "comp_all") if c in pan.columns]
        rets: dict[str, np.ndarray] = {}
        out: dict = {"n_securities": len(ids), "configs": {}}
        # equal-weight universe (long-only benchmark): a constant signal -> 'all' selection, long only
        ew = pan.with_columns(pl.lit(1.0).alias("_one"))
        p = ew.select(pl.col("security_id").alias("entity_id"), "date", pl.col("_one").alias("signal"),
                      pl.col("liquid").alias("eligible"), pl.col("sector_id").alias("group_sector"),
                      pl.col("adv20").cast(pl.Float64).alias("adv_usd"), pl.col("vol"))
        sim = E.simulate_rank(p, px, {"construction": {"side": "long_only", "selection": "all", "weighting": "equal",
                                                       "execution_lag_days": 1, "min_names": 20}, "costs": COSTS}, record=False)
        st, r_ew = stats_of(sim, px)
        out["configs"]["universe_ew"] = st
        for sg in sigs:
            for side in ("long_short", "long_only"):
                try:
                    sim = run_rank(pan, sg, px, side)
                except ValueError as e:
                    LOG.info("%s %s %s: %s", rg, sg, side, e)
                    continue
                st, r = stats_of(sim, px)
                key = f"{sg.removeprefix('s_')}:{side}"
                if side == "long_only":
                    st["vs_universe"] = paired(r, r_ew)
                out["configs"][key] = st
                rets[key] = r
                allret[f"{rg}:{key}"] = (sim.dates, r)
            LOG.info("%s %s done (%.0fs)", rg, sg, time.time() - t0)
        out["overfit"] = dsr_pbo(rets)
        res["factors"][rg] = out
        del px, pan
        gc.collect()


def breakout_econ(s, res: dict, allret: dict) -> None:
    rd = s.data_dir / "reports" / "technicals_v3"
    oos_p = rd / "bo_oos.parquet"
    fe = sorted((WF / "US").glob("bo_c*.parquet"))
    if not fe:
        return
    ev = pl.concat([pl.read_parquet(f, columns=["date", "security_id", "N", "direction", "close", "level", "sigma_d", "confirmed",
                                               "liquid", "complete_20"]) for f in fe])
    ev = ev.filter(pl.col("liquid") & (pl.col("direction") == 1) & (pl.col("N") == 20) & (pl.col("date") >= T0))
    if oos_p.exists():
        o = pl.read_parquet(oos_p).filter((pl.col("H") == 20) & (pl.col("region") == "US") & (pl.col("direction") == 1)
                                          & (pl.col("N") == 20)).select(pl.col("security_id").cast(pl.Utf8), "date", "p_full")
        o = o.with_columns((pl.col("p_full") >= pl.col("p_full").quantile(0.6).over(pl.col("date").dt.year())).alias("top"))
        ev = ev.join(o, on=["security_id", "date"], how="left")
    ev = ev.with_columns(((pl.col("sigma_d") * math.sqrt(20)).exp() - 1).alias("tp"),
                         (1 - pl.col("level") / pl.col("close")).clip(0.002, 0.5).alias("sl"))
    ids = sorted(ev["security_id"].unique().to_list())
    con = P.lake(["prices_daily", "macro_factors"], s, mem="900MB")
    try:
        daily = sanitize(BP._bp_load_prices(con, ids, date(2007, 6, 1), date.today(), "USD").filter(pl.col("px") > 0))  # noqa: SLF001
        px = E.price_panel_from_long(daily.select("entity_id", "date", "px"))
        px.rf = BP.rf_daily(con, px.dates)
    finally:
        con.close()
    feat = BP._bp_price_features(daily, [], None)  # noqa: SLF001
    attrs = feat.select("entity_id", "date", pl.col("adv_usd_20d").alias("adv_usd"), "vol")
    del daily, feat
    rng = np.random.default_rng(42)
    ev = ev.with_columns(pl.Series("rnd", rng.random(ev.height)))
    n_conf = int(ev["confirmed"].sum())
    variants = {"all": ev, "confirmed": ev.filter(pl.col("confirmed")), "unconfirmed": ev.filter(~pl.col("confirmed")),
                "random_same_n": ev.sort("rnd").head(n_conf)}
    if "top" in ev.columns:
        variants["model_top40"] = ev.filter(pl.col("top").fill_null(False))
    out: dict = {"universe": f"US liquid, up breakouts N=20, {T0} ->", "configs": {}, "events": {k: v.height for k, v in variants.items()}}
    rets = {}
    for nm, df in variants.items():
        e = df.select(pl.col("security_id").alias("entity_id"), pl.col("date").cast(pl.Date), pl.lit(1).alias("direction"),
                      pl.col("rnd").alias("strength"), pl.col("sl").alias("stop_loss"), pl.col("tp").clip(0.002, 3.0).alias("take_profit"))
        spec = {"event": {"hold_days": 20, "max_positions": 100, "position_weight": 0.01, "entry_lag_days": 1,
                          "allow_short": False, "max_gross": 1.0}, "costs": COSTS}
        sim = E.simulate_events(e, px, spec, attrs)
        rf = px.rf[sim.start_idx:] if px.rf is not None else None
        m = E.perf_metrics(sim.ret, sim.dates, rf=rf)
        tr = sim.trades
        st = {k: m.get(k) for k in ("sharpe", "cagr", "vol", "max_drawdown")}
        st.update(n_trades=len(tr), hit_rate=float(np.mean([t["return"] > 0 for t in tr])) if tr else None,
                  mean_trade_ret=float(np.mean([t["return"] for t in tr])) if tr else None,
                  exits={rs: sum(t["exit_reason"] == rs for t in tr) for rs in ("stop", "target", "time")},
                  cost_drag_ann=float(sim.cost.mean() * 252))
        out["configs"][nm] = st
        rets[nm] = sim.ret
        allret[f"BO:{nm}"] = (sim.dates, sim.ret)
        LOG.info("breakouts %s %s", nm, st)
    out["paired"] = {f"{a}_minus_{b}": paired(rets[a], rets[b]) for a, b in
                     (("confirmed", "all"), ("confirmed", "unconfirmed"), ("confirmed", "random_same_n"), ("model_top40", "all"))
                     if a in rets and b in rets}
    out["overfit"] = dsr_pbo(rets)
    res["breakouts"] = out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="US,EU,UK,JP")
    ap.add_argument("--skip-breakouts", action="store_true")
    ap.add_argument("--skip-factors", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v3" / "econ.log", level=logging.INFO, format="%(asctime)s %(message)s")
    t00 = time.time()
    rd = s.data_dir / "reports" / "technicals_v3"
    ST.update(s, econ={"status": "running"})
    prev = rd / "econ.json"
    res: dict = json.loads(prev.read_text(encoding="utf-8")) if prev.exists() else {}
    res.setdefault("factors", {})
    res.update(costs=COSTS, period=f"{T0} ->")
    allret: dict = {}
    if not a.skip_factors:
        factor_econ([x for x in a.regions.split(",") if x], s, res, allret)
    if not a.skip_breakouts:
        breakout_econ(s, res, allret)
    res["seconds"] = round(time.time() - t00)
    prev.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    if allret:
        pl.concat([pl.DataFrame({"config": k, "date": v[0], "ret": v[1]}) for k, v in allret.items()]).write_parquet(rd / "econ_returns.parquet")
    ST.update(s, econ={"status": "done", "file": str(prev)})
    LOG.info("done (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
