"""Coverage report for the price-coverage workstream: securities with prices + history depth per market (MIC / synthetic
benchmark prefix), from prices_daily joined to listings. `python -m app.ingestion.price_coverage_report [--save KEY]`."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import polars as pl

from app.core.storage import read_table, scan_table


def coverage_by_market() -> dict[str, dict[str, Any]]:
    pd = (scan_table("prices_daily").select("security_id", "listing_id", "date")
          .group_by("security_id", "listing_id").agg(pl.col("date").min().alias("d0"), pl.col("date").max().alias("d1"),
                                                      pl.len().alias("n")).collect())
    lst = read_table("listings").select("listing_id", "mic").unique(subset=["listing_id"])
    sm = read_table("securities_master").select("security_id", "is_active")
    x = (pd.join(lst, on="listing_id", how="left").join(sm, on="security_id", how="left")
         .with_columns(pl.coalesce(pl.col("mic"), pl.col("security_id").str.split(":").list.first()).alias("mkt")))
    g = x.group_by("mkt").agg(
        pl.len().alias("securities"), pl.col("is_active").eq(False).sum().alias("inactive"),
        pl.col("d0").min().alias("first_date"), pl.col("d0").median().alias("median_first_date"),
        pl.col("n").median().alias("median_bars"), pl.col("d1").max().alias("last_date")).sort("securities",
                                                                                                descending=True)
    return {r["mkt"]: {k: (str(v) if k.endswith("date") else v) for k, v in r.items() if k != "mkt"}
            for r in g.iter_rows(named=True)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default=None, help="store under tasks.report.<KEY> in data/state/price_coverage.json")
    a = ap.parse_args(argv)
    rep = coverage_by_market()
    if a.save:
        from app.ingestion.price_coverage import update_state  # noqa: PLC0415

        update_state("report", None, **{a.save: rep})
    print(json.dumps(rep, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
