"""Technicals v3 #1 supplement: rank IC vs mean-return economics. For each factor, the sign-aligned top - bottom decile
spread of the 20-session forward return in LOG units (the gate's label) and in SIMPLE (arithmetic) units, sector-
demeaned, pooled over weekly origins 2005 -> (liquid names, US / EU / UK / JP). A rank IC is invariant to the log /
simple choice; a mean spread is not: for volatility-linked factors (IVOL, MAX) the log spread contains the variance
drag (E[log r] ~ E[r] - sigma^2 / 2) of the high-volatility decile. Output data/reports/technicals_v3/spreads.json.

    .venv/Scripts/python.exe scripts/technicals_v3/spreads.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts" / "technicals_v3"))

import numpy as np  # noqa: E402
import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techv3 import factors as F  # noqa: E402

import wf_eval as W  # noqa: E402


def main() -> None:
    s = get_settings()
    out: dict = {}
    acc: dict = {f: {"log": [], "simple": [], "n": 0} for f in F.FACTORS}
    for rg in W.REGIONS:
        df = W.load_region(rg).filter(pl.col("liquid") & pl.col("fwd_20").is_not_null())
        # data-error guard: |20-session log return| > 1.5 (-78% / +348%) are almost always unit flips / bad prints
        df = df.filter(pl.col("fwd_20").abs() <= 1.5).with_columns((pl.col("fwd_20").exp() - 1).alias("s20"))
        for c in ("fwd_20", "s20"):
            m_sec = pl.col(c).mean().over(["date", "sector_id"])
            m_reg = pl.col(c).mean().over("date")
            df = df.with_columns((pl.col(c) - pl.when(pl.col("pct_scope") == "sector").then(m_sec).otherwise(m_reg)).alias(f"d_{c}"))
        for f in F.FACTORS:
            x = f"pct_{f}"
            d = df.filter(pl.col(x).is_not_null()).with_columns(((pl.col(x).rank("average").over("date") - 0.5) / pl.len().over("date") * 10)
                                                                 .floor().clip(0, 9).cast(pl.Int32).alias("q"))
            g = d.filter(pl.col("q").is_in([0, 9])).group_by("date", "q").agg(pl.col("d_fwd_20").mean(), pl.col("d_s20").mean())
            p = g.pivot(on="q", index="date", values=["d_fwd_20", "d_s20"]).drop_nulls()
            cols = p.columns
            hi_l = next(c for c in cols if c.startswith("d_fwd_20") and c.endswith("9"))
            lo_l = next(c for c in cols if c.startswith("d_fwd_20") and c.endswith("0"))
            hi_s = next(c for c in cols if c.startswith("d_s20") and c.endswith("9"))
            lo_s = next(c for c in cols if c.startswith("d_s20") and c.endswith("0"))
            sg = F.SIGN[f]
            acc[f]["log"].append(sg * (p[hi_l] - p[lo_l]).to_numpy())
            acc[f]["simple"].append(sg * (p[hi_s] - p[lo_s]).to_numpy())
        del df
    for f, a in acc.items():
        lg, sm = np.concatenate(a["log"]), np.concatenate(a["simple"])
        out[f] = {"log_spread_20d": float(lg.mean()), "simple_spread_20d": float(sm.mean()),
                  "simple_t": float(sm.mean() / (sm.std(ddof=1) / np.sqrt(len(sm) / 4))), "n_region_dates": int(len(sm))}
    (s.data_dir / "reports" / "technicals_v3" / "spreads.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
