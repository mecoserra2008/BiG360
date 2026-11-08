"""Technicals v3 research build (docs/TECHNICALS_V3_VALIDATION.md): point-in-time factor panel (#1) and breakout events
with labels + random controls (#5) for one region, 2004 -> today, from prices_daily (reads only).

Resumable: every chunk of securities writes its own files under scratchpad/tech_v3_b15/wf/<region>/ and is skipped
on a re-run; the cross-section pass (percentiles within region x sector x date) runs per year once all chunks exist.
State: data/state/technicals_v3.json key build.<region>; log data/logs/technicals_v3/wf_build_<region>.log.

    .venv/Scripts/python.exe scripts/technicals_v3/wf_build.py --region UK [--chunk 400]
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techv3 import build as BL  # noqa: E402
from app.techv3 import panel as P  # noqa: E402
from app.techv3 import state as ST  # noqa: E402

WF = ROOT / "scratchpad" / "tech_v3_b15" / "wf"
LOAD_START = date(2001, 1, 1)
OUT_START = date(2004, 1, 1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", required=True)
    ap.add_argument("--chunk", type=int, default=400)
    ap.add_argument("--end")
    a = ap.parse_args()
    s = get_settings()
    rg = a.region
    end = date.fromisoformat(a.end) if a.end else date.today()
    (s.data_dir / "logs" / "technicals_v3").mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=s.data_dir / "logs" / "technicals_v3" / f"wf_build_{rg}.log", level=logging.INFO,
                        format="%(asctime)s %(message)s")
    log = logging.getLogger("wf_build")
    out = WF / rg
    out.mkdir(parents=True, exist_ok=True)
    t00 = time.time()
    ST.update(s, build={rg: {"status": "running", "started": time.strftime("%Y-%m-%d %H:%M:%S")}})
    con = P.lake(["prices_daily", "securities_master", "issuers"], s, mem="900MB", threads=2)
    try:
        secs = P.region_securities(con, rg, LOAD_START, end, min_bars=126)
        cal = P.region_calendar(con, rg, LOAD_START, end)
        log.info("%s: %d securities, %d sessions %s..%s", rg, secs.height, len(cal), cal[0], cal[-1])
        mfile = out / "market.npz"
        if mfile.exists():
            z = np.load(mfile, allow_pickle=True)
            mkt = BL.Market(z["dates"], z["rm"], dict(z["rs"].item()), z["n_mkt"])
            log.info("market loaded")
        else:
            mkt = BL.market_pass(con, rg, secs, cal, LOAD_START, end, chunk=a.chunk)
            np.savez(mfile, dates=mkt.dates, rm=mkt.rm, rs=np.array(mkt.rs, dtype=object), n_mkt=mkt.n_mkt)
            log.info("market pass done (%.0fs)", time.time() - t00)
        out_from = int(np.searchsorted(cal, np.datetime64(OUT_START, "D")))
        ids = secs["security_id"].to_list()
        sec_of = dict(zip(ids, secs["sector_id"].to_list(), strict=True))
        keep = P.universe_members(("mvp_us", "mvp_global"), s)
        n_chunks = (len(ids) + a.chunk - 1) // a.chunk
        for k in range(n_chunks):
            ff, fb, fc = out / f"f_c{k:03d}.parquet", out / f"bo_c{k:03d}.parquet", out / f"ct_c{k:03d}.parquet"
            done = out / f"c{k:03d}.done"
            if done.exists():
                continue
            t0 = time.time()
            part = ids[k * a.chunk:(k + 1) * a.chunk]
            ch = P.load_chunk(con, rg, part, cal, LOAD_START, end)
            df = BL.factor_chunk(ch, [sec_of[x] for x in part], mkt, out_from, labels=True, keep_all=keep)
            df.write_parquet(ff, compression="zstd")
            n_f = df.height
            del df
            gc.collect()
            e, c = BL.breakout_chunk(ch, out_from, seed=k)
            if e.height:
                e.with_columns(pl.Series("sector_id", [sec_of[x] for x in e["security_id"].to_list()])).write_parquet(fb, compression="zstd")
            if c.height:
                c.write_parquet(fc, compression="zstd")
            done.write_text(json.dumps({"factor_rows": n_f, "events": e.height, "controls": c.height}), encoding="utf-8")
            log.info("chunk %d/%d: %d securities, %d factor rows, %d events, %d controls (%.0fs)", k + 1, n_chunks, len(part),
                     n_f, e.height, c.height, time.time() - t0)
            ST.update(s, build={rg: {"status": "running", "chunks_done": k + 1, "chunks": n_chunks}})
            del ch, e, c
            gc.collect()
    finally:
        con.close()
    # cross-section per year
    files = sorted(out.glob("f_c*.parquet"))
    years = range(OUT_START.year, end.year + 1)
    for y in years:
        fy = out / f"f_{y}.parquet"
        if fy.exists():
            continue
        t0 = time.time()
        lf = pl.scan_parquet(files).filter(pl.col("date").dt.year() == y)
        df = lf.collect()
        if df.is_empty():
            continue
        df = BL.add_percentiles(df, rg).with_columns(BL.size_bucket_expr().alias("size_bucket"))
        df.sort(["security_id", "date"]).write_parquet(fy, compression="zstd")
        log.info("year %d: %d rows (%.0fs)", y, df.height, time.time() - t0)
        del df
        gc.collect()
    ST.update(s, build={rg: {"status": "done", "seconds": round(time.time() - t00), "dir": str(out)}})
    log.info("done (%.0fs)", time.time() - t00)


if __name__ == "__main__":
    main()
