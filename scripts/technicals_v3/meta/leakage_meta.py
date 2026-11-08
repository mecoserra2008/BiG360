"""Meta-labelling step 2b: structural leakage tests of the features (app.techmeta.leakage) on real securities.

  causal_bars     4 securities (US / US / EU / JP) x 2 cuts: bar-context features of every origin <= cut unchanged after
                  the future bars are replaced; the deliberately peeking variant must be caught
  causal_context  origins of those securities: the as-of joins ignore source rows known after the origin; the peeking
                  join (market state 3 days later) must be caught
Output data/reports/technicals_v3/meta/leakage.json. State meta.leakage.

    .venv/Scripts/python.exe scripts/technicals_v3/meta/leakage_meta.py
"""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

import polars as pl  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.techmeta import dataset as DS  # noqa: E402
from app.techmeta import leakage as LK  # noqa: E402
from app.techmeta import state as ST  # noqa: E402
from app.techprob import history as Hh  # noqa: E402

DSF = ROOT / "scratchpad" / "tech_v3_meta" / "ds" / "ds_h20.parquet"


def main() -> None:
    s = get_settings()
    rd = s.data_dir / "reports" / "technicals_v3" / "meta"
    rd.mkdir(parents=True, exist_ok=True)
    df = pl.read_parquet(DSF, columns=["security_id", "region", "date", "direction", "earn_days_ahead", "e_days_since"])
    cnt = df.group_by("security_id", "region").agg(pl.len().alias("n")).sort("n", descending=True)
    picks = []
    for rg, k in (("US", 2), ("EU", 1), ("JP", 1)):
        picks += [(r["security_id"], rg) for r in cnt.filter(pl.col("region") == rg).head(k).iter_rows(named=True)]
    out: dict = {"bars": [], "bars_peek": [], "context": None, "context_peek": None}
    for sid, rg in picks:
        si = Hh.load_inputs([sid], rg, Hh.PRICE_START, date(2026, 10, 21))
        if not si:
            continue
        out["bars"].append(LK.causal_bars(si[0]))
        out["bars_peek"].append(LK.causal_bars(si[0], peek=True))
    X = df.filter(pl.col("security_id").is_in([p[0] for p in picks]) & (pl.col("date") >= date(2017, 1, 1)))
    ctx = DS.Context(date(2016, 1, 1), date(2026, 10, 21), s)
    out["context"] = LK.causal_context(X, ctx)
    out["context_peek"] = LK.causal_context(X, ctx, peek=True)
    out["passed"] = (all(r["passed"] for r in out["bars"]) and out["context"]["passed"]
                     and not any(r["passed"] for r in out["bars_peek"]) and not out["context_peek"]["passed"])
    (rd / "leakage.json").write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    ST.update(s, leakage={"passed": out["passed"]})
    print(json.dumps({"passed": out["passed"], "bars": [r["passed"] for r in out["bars"]],
                      "bars_peek_caught": [not r["passed"] for r in out["bars_peek"]],
                      "context": out["context"]["passed"], "context_peek_caught": not out["context_peek"]["passed"]}))


if __name__ == "__main__":
    main()
