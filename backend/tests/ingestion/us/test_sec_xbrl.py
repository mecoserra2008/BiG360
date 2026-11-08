"""sec_xbrl: companyfacts -> fundamentals_raw, tag registry, restatement versions / is_latest, partition-safe upsert."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.core.schema import conform
from app.core.storage import read_table
from app.ingestion.sec_xbrl import (
    PK,
    VERSION_KEY,
    canonical_tags,
    companyfacts_to_frame,
    fiscal_year_end_row,
    tag_coverage,
    upsert_fundamentals_raw,
)

FX = Path(__file__).parent / "fixtures"
KEEP = {"us-gaap": {"RevenueFromContractWithCustomerExcludingAssessedTax", "Assets", "AccountsPayableCurrent",
                    "IncomeTaxesPaidNet"},
        "ifrs-full": {"Revenue", "Assets", "ProfitLoss"}, "dei": {"EntityCommonStockSharesOutstanding"}}


def _doc(name: str) -> dict:
    return json.loads((FX / name).read_text(encoding="utf-8"))


def test_canonical_tags_parses_formulas_and_prefixes(tmp_data):
    tags = canonical_tags(tmp_data.contracts_dir)
    assert "Revenues" in tags["us-gaap"] and "Revenue" in tags["ifrs-full"]
    assert "EntityCommonStockSharesOutstanding" in tags["dei"]          # 'dei:' prefix routed to dei
    assert "AmortizationOfIntangibleAssets" in tags["us-gaap"]          # from 'Depreciation + AmortizationOfIntangibleAssets'
    assert "HeldToMaturitySecurities" in tags["us-gaap"]                # inside nz(...)
    assert "nz" not in tags["us-gaap"] and "coalesce" not in tags["us-gaap"]
    assert "IncreaseDecreaseInOperatingCapital" in tags["us-gaap"]      # us_gaap_negate


def test_aapl_facts_conform_and_versions():
    df = conform(companyfacts_to_frame(_doc("companyfacts_aapl.json"), KEEP), "fundamentals_raw")
    assert df.height > 50
    assert df.select(PK).is_duplicated().sum() == 0
    assert set(df["taxonomy"]) == {"us-gaap", "dei"}
    assert "SomeNonCanonicalTag" not in set(df["concept"])
    assert (df["source_entity_id"] == "0000320193").all() and df["filing_id"].str.starts_with("sec_edgar:").all()
    inst = df.filter(pl.col("concept") == "Assets")
    assert (inst["period_type"] == "instant").all() and inst["period_start"].is_null().all()
    # exactly one latest version per fact
    lat = df.group_by(VERSION_KEY).agg(pl.col("is_latest").sum().alias("n"))
    assert (lat["n"] == 1).all()
    # a fact reported in several filings keeps every version with its own knowledge_date
    multi = df.group_by(VERSION_KEY).agg(pl.len().alias("n"), pl.col("knowledge_date").n_unique().alias("kd")).filter(pl.col("n") > 1)
    assert multi.height > 0
    one = df.join(multi.select(VERSION_KEY), on=VERSION_KEY, nulls_equal=True)
    latest = one.filter(pl.col("is_latest"))
    older = one.filter(~pl.col("is_latest"))
    j = older.join(latest.select(VERSION_KEY + [pl.col("knowledge_date").alias("kd_latest")]), on=VERSION_KEY, nulls_equal=True)
    assert (j["knowledge_date"] <= j["kd_latest"]).all()


def test_dedup_same_fact_multiple_filings_and_exact_duplicates():
    doc = {"cik": 999, "facts": {"us-gaap": {"Assets": {"label": "Assets", "units": {"USD": [
        {"end": "2024-12-31", "val": 100, "accn": "0000000999-25-000001", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01"},
        # exact duplicate inside the same filing; the copy with a frame must be kept
        {"end": "2024-12-31", "val": 100, "accn": "0000000999-25-000001", "fy": 2024, "fp": "FY", "form": "10-K", "filed": "2025-02-01", "frame": "CY2024Q4I"},
        # same fact restated in a later filing
        {"end": "2024-12-31", "val": 105, "accn": "0000000999-26-000002", "fy": 2025, "fp": "FY", "form": "10-K", "filed": "2026-02-01"},
        # missing 'filed' -> dropped
        {"end": "2024-12-31", "val": 1, "accn": "0000000999-26-000003", "fy": 2025, "fp": "FY", "form": "10-K"},
    ]}}}}}
    df = conform(companyfacts_to_frame(doc, None), "fundamentals_raw")
    assert df.height == 2
    first = df.filter(pl.col("filing_id") == "sec_edgar:0000000999-25-000001").row(0, named=True)
    second = df.filter(pl.col("filing_id") == "sec_edgar:0000000999-26-000002").row(0, named=True)
    assert first["frame"] == "CY2024Q4I" and first["is_latest"] is False and first["knowledge_date"] == date(2025, 2, 1)
    assert second["value"] == 105 and second["is_latest"] is True and second["fiscal_year"] == 2025


def test_ifrs_20f_filer():
    df = conform(companyfacts_to_frame(_doc("companyfacts_pbr.json"), KEEP), "fundamentals_raw")
    assert "ifrs-full" in set(df["taxonomy"])
    rev = df.filter((pl.col("taxonomy") == "ifrs-full") & (pl.col("concept") == "Revenue"))
    assert rev.height > 0 and rev["unit"].is_in(["USD", "BRL"]).all()


def test_empty_bulk_member_and_cik_override():
    assert companyfacts_to_frame({}, KEEP).height == 0
    d = _doc("companyfacts_aapl.json")
    d["cik"] = None
    df = companyfacts_to_frame(d, KEEP, cik="0000320193")
    assert df.height > 0 and (df["source_entity_id"] == "0000320193").all()


def test_tag_coverage():
    df = companyfacts_to_frame(_doc("companyfacts_aapl.json"), KEEP)
    cov = tag_coverage(df, KEEP)
    assert cov["us-gaap"]["n_seen"] >= 2 and "Revenue" in cov["ifrs-full"]["never_seen"]


def test_fiscal_year_end_row():
    subs = {"fiscalYearEnd": "0927", "filings": {"recent": {
        "accessionNumber": ["0000320193-25-000079", "0000320193-26-000001"], "form": ["10-K", "8-K"],
        "reportDate": ["2025-09-27", "2026-01-01"], "filingDate": ["2025-10-31", "2026-01-02"]}}}
    r = fiscal_year_end_row("320193", subs)
    assert r["concept"] == "CurrentFiscalYearEndDate" and r["value"] == 927.0 and r["unit"] == "MMDD"
    assert r["filing_id"] == "sec_edgar:0000320193-25-000079" and r["knowledge_date"] == date(2025, 10, 31)
    conform(pl.DataFrame([r]), "fundamentals_raw")
    assert fiscal_year_end_row("1", {"fiscalYearEnd": "", "filings": {"recent": {}}}) is None


@pytest.mark.usefixtures("tmp_data")
def test_upsert_twice_is_idempotent(tmp_data):
    df = companyfacts_to_frame(_doc("companyfacts_aapl.json"), KEEP)
    n1 = upsert_fundamentals_raw(df, tmp_data)
    n2 = upsert_fundamentals_raw(df, tmp_data)        # second write hits existing partitions (core upsert bug path)
    out = read_table("fundamentals_raw", settings=tmp_data)
    assert n1 == n2 == df.height == out.height
    assert out.select(PK).is_duplicated().sum() == 0
