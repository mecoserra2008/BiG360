"""Meta-labelling step 1: point-in-time feature build for every weekly v2 setup (docs/TECHNICALS_V3_VALIDATION.md,
section "Meta-labelling"). Detached + resumable (finished batches are skipped).

Inputs:
  scratchpad/tech_v2_models/wf/<region>/b*_setups.parquet    v2 weekly PIT setups (primary model) 2008 -> 2026-10
  data/reports/technicals_v2/t2_oos.parquet                  v2 T2 walk-forward OOS probabilities + triple-barrier labels
  lake: prices_daily (bars), earnings, cross_asset_regime, macro_factors (W4 states), FRED VIX, insider_transactions,
        si_signals, smart_money_signals, conformal peer groups; data/reports/conformal/wf_peer signal files
Outputs (scratchpad/tech_v3_meta/ds/):
  <region>/b<k>_bar.parquet        bar-context features per (security, origin)
  feat_<region>.parquet            all features per (security, origin)
  ds_h<H>.parquet                  H = 10 / 20 / 40: T2 rows x features + labels + average-uniqueness weights
State: data/state/technicals_v3.json key meta.build; log data/logs/technicals_v3/meta/build.log.

    .venv/Scripts/python.exe scripts/technicals_v3/meta/build_dataset.py [--regions US,EU,UK,JP] [--batch 30]
"""

from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techmeta import dataset as DS  # noqa: E402
from app.techmeta import state as ST  # noqa: E402
from app.techprob import history as Hh  # noqa: E402

END = date(2026, 10, 21)
WF = ROOT / "scratchpad" / "tech_v2_models" / "wf"
OUT = ROOT / "scratchpad" / "tech_v3_meta" / "ds"
HS = (10, 20, 40)
SETUP_COLS = DS.SETUP_COLS
T2_COLS = ["security_id", "date", "region", "H", "direction", "a", "b", "rr_close", "vol_regime", "earn", "out", "tau",
           "x_up", "x_dn", "pw_grid", "pl_grid", "pn_grid", "mtm_z_grid", "cost_r", "exp_r", "exp_r_mtm", "real_r", "real_r_mtm",
           "y_win", "y_loss", "y_nei"]


def setups(rg: str) -> pl.DataFrame:
    fr = [pl.read_parquet(f, columns=SETUP_COLS) for f in sorted((WF / rg).glob("b*_setups.parquet"))]
    S = pl.concat(fr, how="diagonal_relaxed").filter(pl.col("direction") != 0)
    return S.unique(["security_id", "date"], keep="last").sort(["security_id", "date"])


def bar_batch(ids: list[str], rg: str, S: pl.DataFrame) -> pl.DataFrame:
    """Bar-context features of one batch (app.techmeta.dataset.bar_rows: the same code as the weekly refit / nightly)."""
    return DS.bar_rows(Hh.load_inputs(ids, rg, Hh.PRICE_START, END), S.select("security_id", "date", "direction"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="US,EU,UK,JP")
    ap.add_argument("--batch", type=int, default=30)
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "technicals_v3" / "meta"
    logd.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logd / "build.log", level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("meta_build")
    t00 = time.time()
    ST.update(s, build={"status": "running", "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
    OUT.mkdir(parents=True, exist_ok=True)
    ctx = DS.Context(date(2008, 1, 1), END, s, peer_source="wf")
    log.info("market %d days, macro %d rows, peer signals %d, peer members %d, insider coverage %s (%.0fs)",
             ctx.mk.height, ctx.mm.height, ctx.peer.height, len(ctx.members), ctx.icov, time.time() - t00)
    for rg in a.regions.split(","):
        fo = OUT / f"feat_{rg}.parquet"
        if fo.exists():
            log.info("%s features exist, skipped", rg)
            continue
        d = OUT / rg
        d.mkdir(parents=True, exist_ok=True)
        S = setups(rg)
        ids = sorted(S["security_id"].unique().to_list())
        batches = [ids[i:i + a.batch] for i in range(0, len(ids), a.batch)]
        for k, bt in enumerate(batches):
            if (d / f"b{k:03d}.done").exists():
                continue
            t0 = time.time()
            df = bar_batch(bt, rg, S.filter(pl.col("security_id").is_in(bt)))
            if df.height:
                df.write_parquet(d / f"b{k:03d}_bar.parquet")
            (d / f"b{k:03d}.done").write_text(str(df.height), encoding="utf-8")
            log.info("%s batch %d/%d: %d rows (%.0fs)", rg, k + 1, len(batches), df.height, time.time() - t0)
            del df
            gc.collect()
        bars = pl.concat([pl.read_parquet(f) for f in sorted(d.glob("b*_bar.parquet"))], how="diagonal_relaxed")
        X = ctx.join(S.join(bars, on=["security_id", "date"], how="inner"), ids)
        X.write_parquet(fo)
        log.info("%s: %d feature rows, %d columns (%.0fs)", rg, X.height, X.width, time.time() - t00)
        ST.update(s, build={rg: {"rows": X.height, "securities": len(ids), "status": "done"}})
        del X, bars, S
        gc.collect()
    # ---- per horizon datasets
    for H in HS:
        fo = OUT / f"ds_h{H}.parquet"
        if fo.exists():
            continue
        parts = []
        for rg in a.regions.split(","):
            fx = OUT / f"feat_{rg}.parquet"
            if not fx.exists():
                continue
            T = (pl.scan_parquet(ROOT / "data" / "reports" / "technicals_v2" / "t2_oos.parquet")
                 .filter((pl.col("region") == rg) & (pl.col("H") == H)).select(T2_COLS).collect())
            X = pl.read_parquet(fx).drop("region", "direction")
            J = T.join(X, on=["security_id", "date"], how="inner")
            parts.append(J)
            del T, X
        D_ = pl.concat(parts, how="diagonal_relaxed")
        del parts
        D_ = DS.finalize(D_.rename({"pw_grid": "pw", "pl_grid": "pl", "pn_grid": "pn"}), H)
        D_.write_parquet(fo)
        log.info("ds H=%d: %d rows x %d cols, mean uniqueness %.3f (%.0fs)", H, D_.height, D_.width, float(D_["w_uniq"].mean()),
                 time.time() - t00)
        ST.update(s, build={f"ds_h{H}": {"rows": D_.height, "cols": D_.width}})
        del D_
        gc.collect()
    ST.update(s, build={"status": "done", "seconds": round(time.time() - t00)})
    log.info("build finished (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
