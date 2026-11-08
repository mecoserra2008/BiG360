"""T6 economic check of the T2 setup probabilities in the Backtest Lab engine (app.backtest._engine, costs on, deflated
Sharpe + PBO). US mvp, weekly point-in-time long setups 2010 -> 2026-10 (scratchpad wf build), OOS T2 predictions
(data/reports/technicals_v2/t2_oos.parquet, walk-forward grid estimator).

Simple setup-following strategy: every weekly long setup is an event; entry at the next close (lag 1), exit at the
close on which the trade return reaches +take_profit (T1) or -stop_loss (invalidation) - CLOSE-based, the engine has
no intraday barriers -, or after H sessions; max 100 positions x 1%. Variants (same engine, same costs):
  all          every long setup, random order (seeded)
  rr15         naive R:R rule: R:R (from the close) >= 1.5, random order
  er_pos       expected R > 0 (grid OOS, after costs), random order       <- the filter under test
  er_pos_rank  expected R > 0, ordered by expected R
  er_01        expected R > 0.1, random order
for H in (10, 20). Paired differences er_pos - all and er_pos - rr15 (annualised, t), DSR per config, PBO (CSCV).
Output: data/reports/technicals_v2/econ.json (+ econ_returns.parquet). State key t6.econ.

    .venv/Scripts/python.exe scripts/technicals_v2/econ.py
"""

from __future__ import annotations

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
from app.conformal import data as D  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.techprob import state as ST  # noqa: E402

T0 = date(2010, 1, 1)
END = date(2026, 10, 21)
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}
MAXPOS = 100


def run(ev: pl.DataFrame, px: E.PricePanel, attrs: pl.DataFrame, hold: int) -> E.SimResult:
    spec = {"event": {"hold_days": hold, "max_positions": MAXPOS, "position_weight": 1.0 / MAXPOS, "entry_lag_days": 1,
                      "allow_short": False, "max_gross": 1.0}, "costs": COSTS}
    return E.simulate_events(ev, px, spec, attrs)


def stats(sim: E.SimResult, rf: np.ndarray | None) -> tuple[dict, np.ndarray, list]:
    d = list(sim.dates)
    k = np.array([x >= T0 for x in d])
    ret = sim.ret[k]
    dd = [x for x, kk in zip(d, k, strict=True) if kk]
    r_rf = rf[sim.start_idx:][k] if rf is not None else None
    m = E.perf_metrics(ret, dd, rf=r_rf)
    tr = [t for t in sim.trades if t["entry_date"] >= T0]
    m["n_trades"] = len(tr)
    m["trade_hit_rate"] = float(np.mean([t["return"] > 0 for t in tr])) if tr else None
    m["mean_trade_ret"] = float(np.mean([t["return"] for t in tr])) if tr else None
    for rs in ("stop", "target", "time"):
        m[f"exits_{rs}"] = sum(t["exit_reason"] == rs for t in tr)
    m["cost_drag_ann"] = float(sim.cost[k].mean() * 252)
    keys = ("sharpe", "cagr", "vol", "max_drawdown", "n_trades", "trade_hit_rate", "mean_trade_ret", "exits_stop", "exits_target",
            "exits_time", "cost_drag_ann", "skew", "kurtosis")
    return {k2: m.get(k2) for k2 in keys}, ret, dd


def paired(a: np.ndarray, b: np.ndarray) -> dict:
    L = min(len(a), len(b))
    x = a[-L:] - b[-L:]
    sd = x.std(ddof=1)
    return {"ann_diff": float(x.mean() * 252), "t": float(x.mean() / (sd / math.sqrt(L))) if sd > 0 else None, "n_days": int(L)}


def main() -> None:
    s = get_settings()
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v2" / "econ.log", level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("econ")
    t00 = time.time()
    ST.update(s, t6={"econ": {"status": "running"}})
    rd = s.data_dir / "reports" / "technicals_v2"
    P = pl.read_parquet(rd / "t2_oos.parquet").filter((pl.col("region") == "US") & (pl.col("direction") > 0))
    ids = sorted(P["security_id"].unique().to_list())
    con = D.lake(["prices_daily", "macro_factors"], s)
    try:
        daily = BP._bp_load_prices(con, ids, date(2009, 1, 1), END, "USD")  # noqa: SLF001
        px = E.price_panel_from_long(daily.select("entity_id", "date", "px"))
        px.rf = BP.rf_daily(con, px.dates)
    finally:
        con.close()
    feat = BP._bp_price_features(daily, [], None)  # noqa: SLF001
    attrs = feat.select("entity_id", "date", pl.col("adv_usd_20d").alias("adv_usd"), "vol")
    log.info("panel %d x %d (%.0fs)", px.T, len(px.ids), time.time() - t00)
    res: dict = {"universe": f"mvp_us ({len(ids)})", "period": f"{T0} .. {END}", "costs": COSTS, "max_positions": MAXPOS, "configs": {}}
    rets: dict[str, np.ndarray] = {}
    allret = {}
    rng = np.random.default_rng(42)
    for H in (10, 20):
        sub = P.filter(pl.col("H") == H).with_columns(
            ((pl.col("x_up")).exp() - 1).alias("tp"), (1 - (-pl.col("x_dn")).exp()).alias("sl"))
        sub = sub.with_columns(pl.Series("rnd", rng.random(sub.height)))
        variants = {
            "all": (sub, "rnd"), "rr15": (sub.filter(pl.col("rr_close") >= 1.5), "rnd"),
            "er_pos": (sub.filter(pl.col("exp_r") > 0), "rnd"), "er_pos_rank": (sub.filter(pl.col("exp_r") > 0), "exp_r"),
            "er_01": (sub.filter(pl.col("exp_r") > 0.1), "rnd")}
        for nm, (df, scol) in variants.items():
            ev = df.select(pl.col("security_id").alias("entity_id"), pl.col("date").cast(pl.Date), pl.lit(1).alias("direction"),
                           pl.col(scol).alias("strength"), pl.col("sl").clip(0.002, 0.9).alias("stop_loss"),
                           pl.col("tp").clip(0.002, 5.0).alias("take_profit"))
            sim = run(ev, px, attrs, H)
            st, r, dd = stats(sim, px.rf)
            st["n_events"] = ev.height
            st["oos_mean_exp_r"] = float(df["exp_r"].mean()) if df.height else None
            st["oos_mean_real_r"] = float(df["real_r"].mean()) if df.height else None
            key = f"h{H}_{nm}"
            res["configs"][key] = st
            rets[key] = r
            allret[key] = (dd, r)
            log.info("%s %s (%.0fs)", key, st, time.time() - t00)
        for a, b in (("er_pos", "all"), ("er_pos", "rr15"), ("er_pos_rank", "all"), ("er_01", "all")):
            res.setdefault("paired", {})[f"h{H}_{a}_minus_{b}"] = paired(rets[f"h{H}_{a}"], rets[f"h{H}_{b}"])
    names = list(rets)
    L = min(len(v) for v in rets.values())
    M = np.column_stack([rets[n][-L:] for n in names])
    srs = [E.sharpe_stats(M[:, j])["sr"] or 0.0 for j in range(M.shape[1])]
    sr_var = float(np.var(srs, ddof=1))
    res["pbo"] = E.pbo_cscv(M, 10).get("pbo")
    res["dsr"] = {}
    for j, n in enumerate(names):
        st = E.sharpe_stats(M[:, j])
        if st["sr"] is None:
            continue
        ds = E.deflated_sharpe(st["sr"], st["n"], st["skew"], st["kurtosis"], len(names), sr_var)
        res["dsr"][n] = {"dsr": ds["dsr"], "psr": ds["psr"], "sr_ann": st["sr"] * math.sqrt(252)}
    res["seconds"] = round(time.time() - t00)
    (rd / "econ.json").write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    pl.concat([pl.DataFrame({"config": k, "date": v[0], "ret": v[1]}) for k, v in allret.items()]).write_parquet(rd / "econ_returns.parquet")
    ST.update(s, t6={"econ": {"status": "done", "file": str(rd / "econ.json")}})
    log.info("done (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
