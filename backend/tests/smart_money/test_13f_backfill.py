"""13F history backfill (roadmap #7): DuckDB staging + partition merge must equal the weekly job's Polars parser."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from app.core.schema import conform
from app.core.storage import read_table, write_table
from app.ingestion import sec_13f_backfill as B
from app.ingestion.sec_13f import parse_13f_zip
from tests.ingestion.us.test_sec_13f_finra import _info, _zip13f

FX = Path(__file__).parents[1] / "ingestion" / "us" / "fixtures"
COLS = ["filer_cik", "filer_name", "period_end", "cusip", "put_call", "shares", "value_usd", "filing_id",
        "knowledge_date", "issuer_name", "title_of_class", "figi", "amount_type"]


def _synthetic(tmp: Path) -> Path:
    subs = [
        {"ACCESSION_NUMBER": "A1", "FILING_DATE": "10-FEB-2022", "SUBMISSIONTYPE": "13F-HR", "CIK": "1", "PERIODOFREPORT": "31-DEC-2021"},
        {"ACCESSION_NUMBER": "B1", "FILING_DATE": "10-AUG-2026", "SUBMISSIONTYPE": "13F-HR", "CIK": "2", "PERIODOFREPORT": "30-JUN-2026"},
        {"ACCESSION_NUMBER": "B2", "FILING_DATE": "20-AUG-2026", "SUBMISSIONTYPE": "13F-HR/A", "CIK": "2", "PERIODOFREPORT": "30-JUN-2026"},
        {"ACCESSION_NUMBER": "B3", "FILING_DATE": "25-AUG-2026", "SUBMISSIONTYPE": "13F-HR/A", "CIK": "2", "PERIODOFREPORT": "30-JUN-2026"},
    ]
    covers = [
        {"ACCESSION_NUMBER": "A1", "ISAMENDMENT": "N", "AMENDMENTTYPE": "", "FILINGMANAGER_NAME": "OLD FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "B1", "ISAMENDMENT": "N", "AMENDMENTTYPE": "", "FILINGMANAGER_NAME": "NEW FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "B2", "ISAMENDMENT": "Y", "AMENDMENTTYPE": "RESTATEMENT", "FILINGMANAGER_NAME": "NEW FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "B3", "ISAMENDMENT": "Y", "AMENDMENTTYPE": "NEW HOLDINGS", "FILINGMANAGER_NAME": "NEW FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
    ]
    infos = [_info("A1", "037833100", 1500, 10), _info("A1", "037833100", 500, 5), _info("B1", "037833100", 999, 1),
             _info("B2", "037833100", 2700, 10), _info("B3", "02079K305", 1000, 4), _info("B3", "037833100", 270, 1, "Put")]
    p = tmp / "syn_form13f.zip"
    p.write_bytes(_zip13f(subs, covers, infos))
    return p


def _norm(df: pl.DataFrame) -> pl.DataFrame:
    return df.select(COLS).sort(["filer_cik", "period_end", "cusip", "put_call"])


def test_stage_equals_polars_parser(tmp_data, tmp_path):
    for z in (_synthetic(tmp_path), FX / "form13f_small.zip"):
        ref, _ = parse_13f_zip(z)
        out = tmp_path / f"{z.stem}.parquet"
        n, fil = B.stage_zip(z, out, tmp_data)
        got = pl.read_parquet(out)
        assert n == ref.height == got.height
        a, b = _norm(conform(ref, "institutional_holdings")), _norm(got.with_columns(pl.col("period_end").cast(pl.Date),
                                                                                      pl.col("knowledge_date").cast(pl.Date)))
        assert a.equals(b), (a.head(5), b.head(5))
        assert fil.height >= 1


def test_merge_dedupes_and_keeps_existing(tmp_data, tmp_path):
    z = _synthetic(tmp_path)
    ref, _ = parse_13f_zip(z)
    # an existing 2026 row for the same key (older filing) and one other key that must survive the merge
    old = ref.filter(pl.col("period_end").dt.year() == 2026).head(1).with_columns(
        pl.lit(12345.0).alias("shares"), pl.col("knowledge_date") - pl.duration(days=30), pl.lit("SX").alias("security_id"))
    keep = old.with_columns(pl.lit("999999999").alias("cusip"))
    write_table(pl.concat([old, keep]), "institutional_holdings", settings=tmp_data)
    out = tmp_path / "w.parquet"
    B.stage_zip(z, out, tmp_data)
    res = B.merge_staged({"01jun2026-31aug2026": out}, tmp_data)
    assert set(res) == {2021, 2026}
    t = read_table("institutional_holdings", settings=tmp_data)
    assert t.select("filer_cik", "period_end", "cusip", "put_call").is_duplicated().sum() == 0
    assert t.height == ref.height + 1
    k = old.row(0, named=True)
    row = t.filter((pl.col("cusip") == k["cusip"]) & (pl.col("put_call") == k["put_call"])
                   & (pl.col("filer_cik") == k["filer_cik"])).row(0, named=True)
    assert row["shares"] != 12345.0  # newer filing wins
    assert t.filter(pl.col("cusip") == "999999999").height == 1


def test_pick_windows():
    links = {"2023q1": "u", "2023q2": "u", "01jun2026-31aug2026": "u", "01mar2026-31may2026": "u"}
    assert B.pick_windows(links, {}, {"01jun2026-31aug2026"}) == ["2023q2", "01mar2026-31may2026"]
    assert B.pick_windows(links, {"datasets": ["2023Q1"]}, set()) == ["2023q1"]
