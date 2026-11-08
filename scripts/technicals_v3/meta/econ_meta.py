"""Meta-labelling step 3: economic check in the Backtest Lab engine (app.backtest._engine, costs on, deflated Sharpe +
PBO). Weekly v2 setups 2013 -> 2026-10 of the gate universe with the walk-forward OOS meta predictions
(data/reports/technicals_v3/meta/meta_oos_h<H>.parquet).

Setup-following strategy (as the v2 economic check): every selected setup is an event; entry at the next close
(lag 1), exit at the close on which the trade return reaches T1 (+take_profit) or the invalidation (-stop_loss) -
close-based, the engine has no intraday barriers - or after H sessions. Long setups; max positions per region
(US 100 x 1%, EU 50 x 2%, UK 15 x 6.7%, JP 20 x 5%). Variants per region x H (same engine, same costs):
  all          every long setup (random order)                         <- the primary model as-is
  t2_er        T2 expected R (marked to market) > 0 (the v2 filter)
  cf_take      context-free logistic expected R > 0 (meta model without context)
  meta_take    meta take (expected R > the in-sample threshold)       <- meta-filtered
  meta_sized   meta take, position weight x size_rel (quarter Kelly)   <- meta-sized
+ US H = 20 long + short: all_ls / meta_take_ls (borrow costs on).
Paired daily-return differences vs `all` (annualised, t), deflated Sharpe over all configurations, PBO (CSCV), hit
rate, turnover, by year. Output data/reports/technicals_v3/meta/econ.json (+ econ_returns.parquet). State meta.econ.

    .venv/Scripts/python.exe scripts/technicals_v3/meta/econ_meta.py [--h 20,10,40]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.backtest import _engine as E  # noqa: E402
from app.backtest import _panel as BP  # noqa: E402
from app.conformal import data as D  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.techmeta import state as ST  # noqa: E402

T0 = date(2013, 1, 1)
END = date(2026, 10, 21)
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}
MAXPOS = {"US": 100, "EU": 50, "UK": 15, "JP": 20}


def sim(ev: pl.DataFrame, px: E.PricePanel, attrs: pl.DataFrame, hold: int, maxpos: int, short: bool = False) -> E.SimResult:
    spec = {"event": {"hold_days": hold, "max_positions": maxpos, "position_weight": 1.0 / maxpos, "entry_lag_days": 1,
                      "allow_short": short, "max_gross": 1.0}, "costs": COSTS}
    return E.simulate_events(ev, px, spec, attrs)


def stats(s: E.SimResult, rf: np.ndarray | None) -> tuple[dict, np.ndarray, list]:
    d = list(s.dates)
    k = np.array([x >= T0 for x in d])
    ret = s.ret[k]
    dd = [x for x, kk in zip(d, k, strict=True) if kk]
    m = E.perf_metrics(ret, dd, rf=rf[s.start_idx:][k] if rf is not None else None)
    tr = [t for t in s.trades if t["entry_date"] >= T0]
    yrs = max((dd[-1] - dd[0]).days / 365.25, 1e-9) if dd else 1.0
    m.update(n_trades=len(tr), trade_hit_rate=float(np.mean([t["return"] > 0 for t in tr])) if tr else None,
             mean_trade_ret=float(np.mean([t["return"] for t in tr])) if tr else None,
             turnover_ann=float(sum(2 * abs(t["weight"]) for t in tr) / yrs), cost_drag_ann=float(s.cost[k].mean() * 252),
             avg_gross=float(s.gross_exp[k].mean()), **{f"exits_{r}": sum(t["exit_reason"] == r for t in tr) for r in ("stop", "target", "time")})
    yr = {}
    ya = np.array([x.year for x in dd])
    for y in sorted(set(ya.tolist())):
        yr[str(y)] = float(np.prod(1 + ret[ya == y]) - 1)
    m["by_year"] = yr
    keys = ("sharpe", "cagr", "vol", "max_drawdown", "n_trades", "trade_hit_rate", "mean_trade_ret", "turnover_ann",
            "cost_drag_ann", "avg_gross", "exits_stop", "exits_target", "exits_time", "skew", "kurtosis", "by_year")
    return {k2: m.get(k2) for k2 in keys}, ret, dd


def paired(a: np.ndarray, b: np.ndarray) -> dict:
    L = min(len(a), len(b))
    x = a[-L:] - b[-L:]
    sd = x.std(ddof=1)
    return {"ann_diff": float(x.mean() * 252), "t": float(x.mean() / (sd / math.sqrt(L))) if sd > 0 else None, "n_days": int(L)}


def events(df: pl.DataFrame, strength: str, weight: float | None = None) -> pl.DataFrame:
    long = pl.col("direction") > 0
    cols = [pl.col("security_id").alias("entity_id"), pl.col("date").cast(pl.Date), pl.col("direction").cast(pl.Int8),
            pl.col(strength).cast(pl.Float64).alias("strength"),
            pl.when(long).then(1 - (-pl.col("x_dn")).exp()).otherwise(pl.col("x_up").exp() - 1).clip(0.002, 0.9).alias("stop_loss"),
            pl.when(long).then(pl.col("x_up").exp() - 1).otherwise(1 - (-pl.col("x_dn")).exp()).clip(0.002, 5.0).alias("take_profit")]
    if weight is not None:
        cols.append((pl.col("size_rel").cast(pl.Float64) * weight).clip(0.0, 3 * weight).alias("weight"))
    return df.select(cols)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--h", default="20,10,40")
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "technicals_v3" / "meta"
    logging.basicConfig(filename=logd / "econ.log", level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("econ_meta")
    t00 = time.time()
    rd = s.data_dir / "reports" / "technicals_v3" / "meta"
    ST.update(s, econ={"status": "running"})
    HS = [int(x) for x in a.h.split(",")]
    P = {H: pl.read_parquet(rd / f"meta_oos_h{H}.parquet") for H in HS}
    res: dict = {"period": f"{T0} .. {END}", "costs": COSTS, "max_positions": MAXPOS, "configs": {}, "paired": {}}
    rets: dict[str, np.ndarray] = {}
    allret: dict[str, tuple] = {}
    rng = np.random.default_rng(42)
    for rg in ("US", "EU", "UK", "JP"):
        ids = sorted(set().union(*[set(P[H].filter(pl.col("region") == rg)["security_id"].unique().to_list()) for H in HS]))
        if not ids:
            continue
        con = D.lake(["prices_daily", "macro_factors", "fx_rates"], s)
        try:
            daily = BP._bp_load_prices(con, ids, date(2012, 1, 1), END, "USD")  # noqa: SLF001
            px = E.price_panel_from_long(daily.select("entity_id", "date", "px"))
            px.rf = BP.rf_daily(con, px.dates)
        finally:
            con.close()
        feat = BP._bp_price_features(daily, [], None)  # noqa: SLF001
        attrs = feat.select("entity_id", "date", pl.col("adv_usd_20d").alias("adv_usd"), "vol")
        del daily, feat
        mp = MAXPOS[rg]
        log.info("%s panel %d x %d (%.0fs)", rg, px.T, len(px.ids), time.time() - t00)
        for H in HS:
            sub = P[H].filter((pl.col("region") == rg) & (pl.col("date") >= T0))
            sub = sub.with_columns(pl.Series("rnd", rng.random(sub.height)))
            lg = sub.filter(pl.col("direction") > 0)
            variants = {"all": events(lg, "rnd"), "t2_er": events(lg.filter(pl.col("exp_r_mtm") > 0), "rnd"),
                        "cf_take": events(lg.filter(pl.col("er_cf") > 0), "rnd"),
                        "meta_take": events(lg.filter(pl.col("take")), "rnd"),
                        "meta_sized": events(lg.filter(pl.col("take")), "rnd", weight=1.0 / mp)}
            for nm, ev in variants.items():
                key = f"{rg}_h{H}_{nm}"
                st, r, dd = stats(sim(ev, px, attrs, H, mp), px.rf)
                st["n_events"] = ev.height
                res["configs"][key] = st
                rets[key] = r
                allret[key] = (dd, r)
                log.info("%s sharpe %.2f cagr %.3f trades %s (%.0fs)", key, st["sharpe"] or float("nan"), st["cagr"] or float("nan"),
                         st["n_trades"], time.time() - t00)
            for v in ("t2_er", "cf_take", "meta_take", "meta_sized"):
                res["paired"][f"{rg}_h{H}_{v}_minus_all"] = paired(rets[f"{rg}_h{H}_{v}"], rets[f"{rg}_h{H}_all"])
            res["paired"][f"{rg}_h{H}_meta_take_minus_t2_er"] = paired(rets[f"{rg}_h{H}_meta_take"], rets[f"{rg}_h{H}_t2_er"])
            res["paired"][f"{rg}_h{H}_meta_take_minus_cf_take"] = paired(rets[f"{rg}_h{H}_meta_take"], rets[f"{rg}_h{H}_cf_take"])
            if rg == "US" and H == 20:
                for nm, df in (("all_ls", sub), ("meta_take_ls", sub.filter(pl.col("take")))):
                    key = f"US_h20_{nm}"
                    st, r, dd = stats(sim(events(df, "rnd"), px, attrs, H, mp, short=True), px.rf)
                    res["configs"][key] = st
                    rets[key] = r
                    allret[key] = (dd, r)
                res["paired"]["US_h20_meta_take_ls_minus_all_ls"] = paired(rets["US_h20_meta_take_ls"], rets["US_h20_all_ls"])
        del px, attrs
        ST.update(s, econ={"status": "running", "done_region": rg})
    names = list(rets)
    L = min(len(v) for v in rets.values())
    Mx = np.column_stack([rets[n][-L:] for n in names])
    srs = [E.sharpe_stats(Mx[:, j])["sr"] or 0.0 for j in range(Mx.shape[1])]
    sr_var = float(np.var(srs, ddof=1))
    res["n_configs"] = len(names)
    res["pbo"] = E.pbo_cscv(Mx, 10).get("pbo")
    res["pbo_us_h20"] = E.pbo_cscv(np.column_stack([rets[n][-L:] for n in names if n.startswith("US_h20_") and not n.endswith("_ls")]), 10).get("pbo")
    res["dsr"] = {}
    for j, n in enumerate(names):
        st = E.sharpe_stats(Mx[:, j])
        if st["sr"] is None:
            continue
        ds = E.deflated_sharpe(st["sr"], st["n"], st["skew"], st["kurtosis"], len(names), sr_var)
        res["dsr"][n] = {"dsr": ds["dsr"], "psr": ds["psr"], "sr_ann": st["sr"] * math.sqrt(252)}
    res["seconds"] = round(time.time() - t00)
    (rd / "econ.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    pl.concat([pl.DataFrame({"config": k, "date": v[0], "ret": v[1]}) for k, v in allret.items()]).write_parquet(rd / "econ_returns.parquet")
    ST.update(s, econ={"status": "done", "configs": len(names), "seconds": res["seconds"]})
    log.info("done (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
