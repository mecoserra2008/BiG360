"""P4 walk-forward for application A (detached, resumable). Logs to data/logs/conformal/wf_envelopes.log,
results to data/reports/conformal/wf_env/<kind>_<region>_<method>.parquet (existing files are skipped).

    .venv/Scripts/python.exe scripts/conformal/wf_envelopes.py [--regions US,EU,UK,JP] [--skip-gru]
"""

from __future__ import annotations

import argparse
import gc
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402

from app.conformal import validation as W  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.ops.winsys import memory_status  # noqa: E402

FIT_YEARS = [2009, 2012, 2015, 2018, 2021, 2024]


def wait_ram(log: logging.Logger, min_gb: float = 3.0) -> None:
    while True:
        m = memory_status() or {}
        if m.get("avail_gb", 99) >= min_gb:
            return
        log.info("free RAM %.1f GB < %.1f GB: waiting", m.get("avail_gb", 0), min_gb)
        time.sleep(60)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="US,EU,UK,JP")
    ap.add_argument("--skip-gru", action="store_true")
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "conformal"
    logd.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logd / "wf_envelopes.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("wf_env")
    out = W.report_dir(s) / "wf_env"
    out.mkdir(parents=True, exist_ok=True)
    regions = a.regions.split(",")
    cache = W.cache_dir(s)
    gru_files = {r: cache / f"gru_V_{r}.npy" for r in regions}
    if not a.skip_gru and not all(f.exists() for f in gru_files.values()):
        wait_ram(log, 4.0)
        t0 = time.time()
        preps = {r: W.prepare(r, s) for r in regions}
        preps = {r: p for r, p in preps.items() if p is not None}
        log.info("gru: prepared %s in %.0fs", {r: len(p.ids) for r, p in preps.items()}, time.time() - t0)
        Vg = W.run_gru_region_outputs(preps, FIT_YEARS, log_fn=log.info)
        for r, v in Vg.items():
            np.save(gru_files[r], v.astype(np.float32))
        log.info("gru outputs saved (%.0fs)", time.time() - t0)
        del preps, Vg
        gc.collect()
    for r in regions:
        todo = [m for m in W.JOINT_METHODS if not (out / f"joint_{r}_{m}.parquet").exists()]
        todo_nb = [m for m in W.NB_METHODS if not (out / f"nextbar_{r}_{m}.parquet").exists()]
        if not todo and not todo_nb:
            continue
        wait_ram(log)
        t0 = time.time()
        p = W.prepare(r, s)
        if p is None:
            continue
        log.info("%s: prepared N=%d T=%d (%.0fs)", r, len(p.ids), len(p.dates), time.time() - t0)
        c: dict = {}
        if gru_files[r].exists():
            c["gru_V"] = np.load(gru_files[r]).astype(np.float64)
        for m in todo:
            if m == "gru" and "gru_V" not in c:
                continue
            t1 = time.time()
            agg = W.Agg()
            try:
                W.evaluate_joint(p, m, c, agg)
            except Exception:  # noqa: BLE001
                log.exception("%s %s joint failed", r, m)
                continue
            agg.frame(W.KEY_NAMES).write_parquet(out / f"joint_{r}_{m}.parquet")
            log.info("%s joint %s done (%.0fs)", r, m, time.time() - t1)
        for m in todo_nb:
            t1 = time.time()
            agg = W.Agg()
            try:
                W.evaluate_nextbar(p, m, c, agg)
            except Exception:  # noqa: BLE001
                log.exception("%s %s nextbar failed", r, m)
                continue
            agg.frame(W.KEY_NAMES).write_parquet(out / f"nextbar_{r}_{m}.parquet")
            log.info("%s nextbar %s done (%.0fs)", r, m, time.time() - t1)
        del p, c
        gc.collect()
    log.info("wf_envelopes finished")


if __name__ == "__main__":
    main()
