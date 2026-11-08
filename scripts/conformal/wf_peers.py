"""P4 walk-forward for application B (detached, resumable). Log: data/logs/conformal/wf_peers.log; results:
data/reports/conformal/wf_peer/. GCN-GRU block models: data/models/conformal/wf/gcn_block_<b>.npz (+ .json scales).

    .venv/Scripts/python.exe scripts/conformal/wf_peers.py [--regions US,EU,...] [--filters ewma,gcn_gru] [--epochs 20]
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.conformal import data as D  # noqa: E402
from app.conformal import peer_groups as PG  # noqa: E402
from app.conformal import validation_peer as VP  # noqa: E402
from app.conformal.learned import GCNGRUModel, GraphSeq, train_gcn_gru  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.ops.winsys import memory_status  # noqa: E402

BLOCKS = [2010, 2013, 2016, 2019, 2022, 2025]


def wait_ram(log: logging.Logger, min_gb: float = 3.0) -> None:
    while (memory_status() or {}).get("avail_gb", 99) < min_gb:
        log.info("waiting for RAM")
        time.sleep(60)


def block_of(y: int) -> int:
    return max(b for b in BLOCKS if b <= y)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions")
    ap.add_argument("--filters", default=",".join(VP.FILTERS))
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--budget-min", type=float, default=25.0, help="training time budget per block")
    a = ap.parse_args()
    s = get_settings()
    logd = s.data_dir / "logs" / "conformal"
    logd.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=logd / "wf_peers.log", level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("wf_peers")
    grp = PG.load_groups(s)
    regions = a.regions.split(",") if a.regions else sorted(grp["region_bloc"].unique().to_list())
    filters = a.filters.split(",")
    out = VP.out_dir()
    mdir = PG.model_dir(s) / "wf"
    mdir.mkdir(parents=True, exist_ok=True)
    wait_ram(log, 4.0)
    t0 = time.time()
    data = {}
    for r in sorted(grp["region_bloc"].unique().to_list()):
        x = VP.region_inputs(r, grp)
        if x is not None:
            data[r] = x
    log.info("loaded returns for %s (%.0fs)", list(data), time.time() - t0)

    # ---------------------------------------------------------------- GCN-GRU block models
    if "gcn_gru" in filters:
        for b in BLOCKS:
            f = mdir / f"gcn_block_{b}.npz"
            if f.exists():
                continue
            seqs, scales = [], {}
            for r, (Rall, dates, col) in data.items():
                for gy, A in VP.group_years(r, grp, Rall, dates, col, years=[b]):
                    Xp = gy.X[:gy.pre_end]
                    seqs.append(GraphSeq(X=Xp, A=A))
                    sd = np.nanstd(Xp, axis=0)
                    for sid, v in zip(gy.sids, sd, strict=True):
                        scales[sid] = float(v)
            log.info("gcn block %d: %d group sequences", b, len(seqs))
            if not seqs:
                continue
            m = train_gcn_gru(seqs, epochs=a.epochs, log=log.info, time_budget_s=a.budget_min * 60)
            m.save(str(f))
            f.with_suffix(".json").write_text(json.dumps({"scales": scales, "meta": m.meta}), encoding="utf-8")
            log.info("gcn block %d trained: %s", b, m.meta)
            del seqs
            gc.collect()
    gcns = {}
    for b in BLOCKS:
        f = mdir / f"gcn_block_{b}.npz"
        if f.exists():
            gcns[b] = {"model": GCNGRUModel.load(str(f)),
                       "scales": json.loads(f.with_suffix(".json").read_text(encoding="utf-8"))["scales"]}

    # ---------------------------------------------------------------- evaluation
    vix = D.vix_daily(VP.START, VP.END)
    vd = vix["date"].to_list()
    vv = vix["vix"].to_list()
    vix_high = {}
    prev = 20.0
    allb = np.busday_offset(np.datetime64("2003-01-01"), np.arange(0, 6200), roll="forward")
    j = 0
    for d in allb:
        dd = d.astype(object)
        while j < len(vd) and vd[j] < dd:      # VIX known at the previous close
            prev = vv[j]
            j += 1
        vix_high[dd] = prev > 25.0
    for r in regions:
        if r not in data:
            continue
        Rall, dates, col = data[r]
        for flt in filters:
            fo = out / f"{r}_{flt}.parquet"
            if fo.exists():
                continue
            t1 = time.time()
            agg: dict = {}
            sig: list | None = [] if flt in VP.SIGNAL_FILTERS else None
            n = 0
            for gy, A in VP.group_years(r, grp, Rall, dates, col):
                g = gcns.get(block_of(gy.y)) if flt == "gcn_gru" else None
                if flt == "gcn_gru" and g is None:
                    continue
                try:
                    VP.evaluate_group_year(gy, flt, A, r, vix_high, agg, sig, g)
                    n += 1
                except Exception:  # noqa: BLE001
                    log.exception("%s %s %s %d failed", r, flt, gy.gid, gy.y)
            VP.agg_frame(agg).write_parquet(fo)
            if sig is not None:
                pl.DataFrame(sig, schema=["group_id", "date", "security_id", "z_cond", "z_marg", "score", "q90", "r", "group_move"],
                             orient="row").write_parquet(out / f"signals_{r}_{flt}.parquet")
            log.info("%s %s: %d group-years (%.0fs)", r, flt, n, time.time() - t1)
            gc.collect()
    log.info("wf_peers finished")


if __name__ == "__main__":
    main()
