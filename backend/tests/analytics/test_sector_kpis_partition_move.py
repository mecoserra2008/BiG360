"""2026-10-31: an issuer whose sector changed must not keep its old sector_kpis rows in the previous sector_id
partition (cross-partition duplicate PKs failed dq_checks sector_kpis/pk_unique after the ref_universe sector flip)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from app.analytics.ratios import drop_stale_sector_rows
from app.core.schema import conform
from app.core.storage import read_table, write_table


def _rows(iid: str, sector: str, run: str) -> pl.DataFrame:
    df = pl.DataFrame({"issuer_id": [iid, iid], "kpi_id": ["k1", "k2"], "period_end": [date(2025, 12, 31)] * 2,
                       "period_kind": ["FY", "FY"], "knowledge_date": [date(2026, 2, 1)] * 2, "sector_id": [sector] * 2,
                       "subsector_id": ["x"] * 2, "kpi_set_id": ["s"] * 2, "fiscal_year": [2025] * 2,
                       "fiscal_period": ["FY"] * 2, "unit": ["ratio"] * 2, "inputs_complete": [True] * 2,
                       "computed_at": [datetime.now(UTC)] * 2, "run_id": [run] * 2})
    return conform(df, "sector_kpis", strict=False)


def test_moved_issuer_rows_leave_the_old_sector_partition(tmp_data):
    write_table(pl.concat([_rows("A", "HLTH", "R1"), _rows("B", "HLTH", "R1")]), "sector_kpis", settings=tmp_data)
    new = _rows("A", "INDU", "R2")
    write_table(new, "sector_kpis", settings=tmp_data)
    assert read_table("sector_kpis", settings=tmp_data).filter(pl.col("issuer_id") == "A").height == 4   # duplicated
    n = drop_stale_sector_rows(new.select("issuer_id", "sector_id").unique(), tmp_data)
    out = read_table("sector_kpis", settings=tmp_data)
    assert n == 2
    assert out.filter(pl.col("issuer_id") == "A")["sector_id"].unique().to_list() == ["INDU"]
    assert out.filter(pl.col("issuer_id") == "B").height == 2                                            # untouched
