"""Meta-labelling step 1b: add the parallel #1 / #5 passing features (announced "ready" in data/state/technicals_v3.json)
to the walk-forward datasets scratchpad/tech_v3_meta/ds/ds_h<H>.parquet (point-in-time: #1 percentiles as of the
origin, #5 breakouts known at their close within the last 7 calendar days; research builds scratchpad/tech_v3_b15/wf).
The previous datasets are kept as ds_h<H>.noextra.parquet. State meta.extra.

    .venv/Scripts/python.exe scripts/technicals_v3/meta/add_extra.py
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techmeta import features as F  # noqa: E402
from app.techmeta import state as ST  # noqa: E402

DSD = ROOT / "scratchpad" / "tech_v3_meta" / "ds"
V3 = ROOT / "scratchpad" / "tech_v3_b15" / "wf"


def main() -> None:
    s = get_settings()
    t0 = time.time()
    extra = [x for v in ST.parallel_features(s).values() for x in v["features"]]
    if not extra:
        print("no #1 / #5 features announced ready")
        return
    keys = pl.read_parquet(DSD / "ds_h20.parquet", columns=["security_id", "date", "direction"]).unique(["security_id", "date"])
    for H in (10, 40):
        k2 = pl.read_parquet(DSD / f"ds_h{H}.parquet", columns=["security_id", "date", "direction"])
        keys = pl.concat([keys, k2]).unique(["security_id", "date"])
    ids = sorted(keys["security_id"].unique().to_list())
    X = keys
    if any(x in F.B1_FEATURES for x in extra):
        X = F.join_factors(X, F.factors_history(V3, F.REGIONS, ids))
    if any(x in F.B5_FEATURES for x in extra):
        X = F.join_breakouts(X, F.breakouts_history(V3, F.REGIONS, ids))
    X = X.select("security_id", "date", *[pl.col(c).cast(pl.Float32) for c in extra])
    cov = {c: float(X[c].is_not_null().mean()) for c in extra}
    print("coverage", cov, "rows", X.height, round(time.time() - t0), flush=True)
    for H in (10, 20, 40):
        f = DSD / f"ds_h{H}.parquet"
        bak = DSD / f"ds_h{H}.noextra.parquet"
        if not bak.exists():
            shutil.copyfile(f, bak)
        D = pl.read_parquet(bak)
        D = D.join(X, on=["security_id", "date"], how="left")
        D.write_parquet(f)
        print(H, D.height, D.width, flush=True)
        del D
    ST.update(s, extra={"features": extra, "coverage": cov, "seconds": round(time.time() - t0)})


if __name__ == "__main__":
    main()
