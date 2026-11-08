"""sec_index: master.idx parsing, filings_index rows, PIT rules (offline, recorded fixture)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.core.schema import conform
from app.ingestion.sec_index import (
    base_form,
    build_index_rows,
    cik10,
    form_class,
    merge_preserving,
    parse_acceptance_utc,
    parse_header_acceptance,
    parse_master_idx,
    submissions_meta,
    submissions_recent_by_acc,
)

FX = Path(__file__).parent / "fixtures"


def _entries():
    return parse_master_idx((FX / "master.20261009.idx").read_text(encoding="latin-1"), date(2026, 10, 9))


def test_parse_master_idx():
    e = _entries()
    assert len(e) >= 15
    r = e[0]
    assert set(r) == {"cik", "name", "form", "filed", "accession", "index_date"}
    assert len(r["cik"]) == 10 and r["cik"].isdigit()
    assert all(len(x["accession"]) == 20 for x in e)
    assert {x["form"] for x in e} >= {"10-K", "4", "8-K", "424B2"}


def test_parse_master_idx_pipe_in_name():
    txt = "hdr\n-----\n123|ACME | CO|8-K|20261009|edgar/data/123/0000000123-26-000001.txt\n"
    (r,) = parse_master_idx(txt)
    assert r["name"] == "ACME | CO" and r["form"] == "8-K" and r["cik"] == "0000000123"


def test_build_index_rows_filters_and_conforms():
    e = _entries()
    df = conform(build_index_rows(e, set(), {}), "filings_index")
    assert "424B2" not in set(df["form_type"])            # structured notes are not indexed by default
    assert df["filing_id"].is_unique().all()                # joint filings collapsed to one row per accession
    assert set(df["form_class"]) <= {"annual", "insider", "current_report", "holdings"}
    allf = build_index_rows(e, set(), {}, all_forms=True)
    assert "424B2" in set(allf["form_type"])


def test_delayed_dissemination_uses_index_date():
    e = _entries()
    df = build_index_rows(e, set(), {}, all_forms=True)
    old = df.filter(pl.col("filing_id").str.contains("-25-"))
    assert old.height >= 1
    # 'Date Filed' in 2025 but first disseminated in the 2026-10-09 daily index -> knowledge_date = index date
    assert (old["knowledge_date"] == date(2026, 10, 9)).all()
    assert (old["year"] == 2026).all()


def test_joint_filing_prefers_issuer_cik():
    lines = [
        {"cik": "0000000001", "name": "OWNER JOHN", "form": "4", "filed": "20261009", "accession": "0000000001-26-000001", "index_date": None},
        {"cik": "0000320193", "name": "APPLE", "form": "4", "filed": "20261009", "accession": "0000000001-26-000001", "index_date": None},
    ]
    df = build_index_rows(lines, {"0000320193"}, {})
    assert df.height == 1 and df["source_entity_id"][0] == "0000320193"


def test_submissions_enrichment():
    subs = {"filings": {"recent": {
        "accessionNumber": ["0000320193-26-000010"], "filingDate": ["2026-10-09"], "reportDate": ["2026-09-27"],
        "acceptanceDateTime": ["2026-10-09T20:32:43.000Z"], "form": ["10-K"], "items": [""],
        "primaryDocument": ["aapl-20260927.htm"], "isXBRL": [1]}}}
    recent = submissions_recent_by_acc(subs)
    lines = [{"cik": "0000320193", "name": "APPLE", "form": "10-K", "filed": "20261009",
              "accession": "0000320193-26-000010", "index_date": date(2026, 10, 9)}]
    # fetched the same evening: the submissions value is taken as is (workstream B acceptance-time rule)
    df = conform(build_index_rows(lines, set(), recent, now=datetime(2026, 10, 9, 22, 0, tzinfo=UTC)), "filings_index")
    r = df.row(0, named=True)
    assert r["period_end"] == date(2026, 9, 27)
    assert r["knowledge_ts"] == datetime(2026, 10, 9, 20, 32, 43, tzinfo=UTC)
    # fetched two weeks later EDGAR serves it late by the ET offset (00:32:43Z): corrected back to 20:32:43Z
    late = submissions_recent_by_acc({"filings": {"recent": {**subs["filings"]["recent"],
                                                             "acceptanceDateTime": ["2026-10-10T00:32:43.000Z"]}}})
    df2 = conform(build_index_rows(lines, set(), late, now=datetime(2026, 10, 24, 7, 0, tzinfo=UTC)), "filings_index")
    assert df2.row(0, named=True)["knowledge_ts"] == datetime(2026, 10, 9, 20, 32, 43, tzinfo=UTC)
    assert r["url"].endswith("/000032019326000010/aapl-20260927.htm")
    assert r["has_xbrl"] is True and r["form_class"] == "annual" and r["is_amendment"] is False


def test_merge_preserving_keeps_doc_path_and_issuer_cik():
    lines = [{"cik": "0000000099", "name": "X", "form": "4", "filed": "20261009", "accession": "0000000099-26-000001", "index_date": None}]
    new = build_index_rows(lines, set(), {})
    prev = new.with_columns(pl.lit("raw/sec_text/x/full.txt.gz").alias("doc_path"),
                            pl.lit("0000320193").alias("source_entity_id"))
    out = merge_preserving(new, prev)
    assert out["doc_path"][0] == "raw/sec_text/x/full.txt.gz"
    assert out["source_entity_id"][0] == "0000320193"


def test_helpers():
    assert cik10(320193) == "0000320193" and cik10("0000320193") == "0000320193"
    assert base_form("10-K/A") == "10-K" and form_class("10-K/A") == "annual"
    assert form_class("SCHEDULE 13G/A") == "holdings" and form_class("DEF 14A") == "proxy"
    assert form_class("6-K") == "current_report" and form_class("4/A") == "insider"
    # SGML header acceptance time is US/Eastern; the submissions API value is corrected (late by the ET offset once
    # settled; no fetch context = settled)
    assert parse_header_acceptance("20261009163243") == datetime(2026, 10, 9, 20, 32, 43, tzinfo=UTC)
    assert parse_header_acceptance("20260115163243") == datetime(2026, 1, 15, 21, 32, 43, tzinfo=UTC)
    assert parse_acceptance_utc("2026-10-09T02:30:32.000Z") == datetime(2026, 10, 8, 22, 30, 32, tzinfo=UTC)


def test_submissions_meta_region():
    pbr = {"name": "PETROBRAS", "fiscalYearEnd": "1231", "stateOfIncorporationDescription": "Brazil",
           "addresses": {"business": {"isForeignLocation": 1, "country": "Brazil"}}}
    assert submissions_meta(pbr)["region"] == "LATAM"
    aapl = {"name": "Apple", "addresses": {"business": {"isForeignLocation": 0, "stateOrCountry": "CA"}}}
    assert submissions_meta(aapl)["region"] == "US"
