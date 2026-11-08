"""T6 step 1: point-in-time history build for the Technicals v2 walk-forward (docs/TECHNICALS_V2_VALIDATION.md).

Universe = the conformal gate universe: mvp US (mvp_us) + the mvp_global EU / UK / JP names; prices 2003 -> as-of.
Per region (one prices_daily partition at a time) and batches of securities (spawned worker processes, streamed
results): app.techprob.history.batch_task -> T2 grid counts (all daily origins), weekly PIT setups and touched
zones (+ random controls). Outputs (resumable: finished batches are skipped):

    scratchpad/tech_v2_models/wf/<region>/b<k>_{setups,zones}.parquet, b<k>_counts.npz

State: data/state/technicals_v2.json key t6.build; log data/logs/technicals_v2/wf_build.log.

    .venv/Scripts/python.exe scripts/technicals_v2/wf_build.py [--regions US,EU,UK,JP] [--workers 4] [--batch 20]
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.conformal import data as D  # noqa: E402
from app.conformal.validation import REGIONS  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.techprob import history as Hh  # noqa: E402
from app.techprob import state as ST  # noqa: E402

END = date(2026, 10, 21)
OUT = ROOT / "scratchpad" / "tech_v2_models" / "wf"


def save(res: dict, d: Path, k: int) -> None:
    for kind in ("setups", "zones"):
        x = res[kind]
        if isinstance(x, list):
            x = pl.DataFrame(x, infer_schema_length=None) if x else pl.DataFrame()
        if x.height:
            x.write_parquet(d / f"b{k:03d}_{kind}.parquet")
    gc = res["counts"]
    np.savez_compressed(d / f"b{k:03d}_counts.npz", C=gc.C.astype(np.float32), N=gc.N.astype(np.float32))
    (d / f"b{k:03d}.done").write_text(f"{res['n']} {len(res['failed'])} {res['seconds']:.0f}", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="US,EU,UK,JP")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--batch", type=int, default=20)
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "technicals_v2"
    logd.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logd / "wf_build.log", level=logging.INFO, format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_build")
    vix = D.vix_daily(date(2002, 1, 1), END, s)
    vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
    vv = vix["vix"].to_numpy()
    for rg in a.regions.split(","):
        d = OUT / rg
        d.mkdir(parents=True, exist_ok=True)
        meta = D.universe_ids(REGIONS[rg], s)
        ids = sorted(meta["security_id"].to_list())
        t0 = time.time()
        items = Hh.eligible_ids(ids, rg, Hh.PRICE_START, END)          # workers load their own batch (memory-lean)
        batches = [items[i:i + a.batch] for i in range(0, len(items), a.batch)]
        todo = [k for k in range(len(batches)) if not (d / f"b{k:03d}.done").exists()]
        log.info("%s: %d securities with >= %d bars in %d batches (%d to do), loaded in %.0fs", rg, len(items),
                 Hh.MIN_BARS, len(batches), len(todo), time.time() - t0)
        ST.update(s, t6={"build": {**(ST.read(s).get("t6", {}).get("build") or {}),
                                   rg: {"securities": len(items), "batches": len(batches), "todo": len(todo), "status": "running"}}})
        if todo:
            with ProcessPoolExecutor(max_workers=a.workers, mp_context=mp.get_context("spawn")) as ex:
                pend = {}
                it = iter(todo)
                for k in it:
                    pend[ex.submit(Hh.batch_task_ids, batches[k], rg, vd, vv, Hh.PRICE_START, END)] = k
                    if len(pend) >= a.workers + 1:
                        break
                while pend:
                    done, _ = wait(list(pend), return_when=FIRST_COMPLETED)
                    for f in done:
                        k = pend.pop(f)
                        res = f.result()
                        save(res, d, k)
                        log.info("%s batch %d/%d: %d secs, %d setups, %d zones, failed %s, %.0fs", rg, k + 1, len(batches),
                                 res["n"], len(res["setups"]), len(res["zones"]), res["failed"], res["seconds"])
                        del f
                        del res
                        nxt = next(it, None)
                        if nxt is not None:
                            pend[ex.submit(Hh.batch_task_ids, batches[nxt], rg, vd, vv, Hh.PRICE_START, END)] = nxt
        n_done = sum((d / f"b{k:03d}.done").exists() for k in range(len(batches)))
        ST.update(s, t6={"build": {**(ST.read(s).get("t6", {}).get("build") or {}),
                                   rg: {"securities": len(items), "batches": len(batches), "done": n_done,
                                        "status": "done" if n_done == len(batches) else "partial",
                                        "seconds": round(time.time() - t0)}}})
        log.info("%s finished (%.0fs)", rg, time.time() - t0)
        del items, batches


if __name__ == "__main__":
    main()
