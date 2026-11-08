"""sec_13f (holdings, amendments, value units) and finra_si (short interest, fails-to-deliver), offline."""

from __future__ import annotations

import io
import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.core.schema import conform
from app.ingestion.finra_si import (
    business_days_after,
    candidate_settlement_dates,
    ftd_period_end,
    parse_finra_si,
    parse_ftd_zip,
    resolve_security_ids,
)
from app.ingestion.sec_13f import dataset_links, parse_13f_zip

FX = Path(__file__).parent / "fixtures"


# ----------------------------------------------------------------------------------------------- 13F

def test_13f_fixture_conforms():
    h, f = parse_13f_zip(FX / "form13f_small.zip")
    df = conform(h, "institutional_holdings")
    assert df.height > 10
    assert df.select("filer_cik", "period_end", "cusip", "put_call").is_duplicated().sum() == 0
    assert set(df["put_call"]) <= {"none", "put", "call"}
    assert df["filing_id"].str.starts_with("sec_edgar:").all() and (df["value_usd"] >= 0).all()
    assert f.height >= 1 and set(f["form"]) <= {"13F-HR", "13F-HR/A"}


def _tsv(rows: list[dict]) -> str:
    return pl.DataFrame(rows).write_csv(separator="\t", quote_style="never")


def _zip13f(subs, covers, infos) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SUBMISSION.tsv", _tsv(subs))
        z.writestr("COVERPAGE.tsv", _tsv(covers))
        z.writestr("INFOTABLE.tsv", _tsv(infos))
    return buf.getvalue()


def _info(acc, cusip, value, sh, putcall=""):
    return {"ACCESSION_NUMBER": acc, "INFOTABLE_SK": "1", "NAMEOFISSUER": "APPLE INC", "TITLEOFCLASS": "COM",
            "CUSIP": cusip, "FIGI": "", "VALUE": str(value), "SSHPRNAMT": str(sh), "SSHPRNAMTTYPE": "SH", "PUTCALL": putcall}


def test_13f_amendments_and_units():
    subs = [
        {"ACCESSION_NUMBER": "A1", "FILING_DATE": "10-FEB-2022", "SUBMISSIONTYPE": "13F-HR", "CIK": "1", "PERIODOFREPORT": "31-DEC-2021"},
        {"ACCESSION_NUMBER": "B1", "FILING_DATE": "10-AUG-2026", "SUBMISSIONTYPE": "13F-HR", "CIK": "2", "PERIODOFREPORT": "30-JUN-2026"},
        {"ACCESSION_NUMBER": "B2", "FILING_DATE": "20-AUG-2026", "SUBMISSIONTYPE": "13F-HR/A", "CIK": "2", "PERIODOFREPORT": "30-JUN-2026"},
        {"ACCESSION_NUMBER": "B3", "FILING_DATE": "25-AUG-2026", "SUBMISSIONTYPE": "13F-HR/A", "CIK": "2", "PERIODOFREPORT": "30-JUN-2026"},
        {"ACCESSION_NUMBER": "C1", "FILING_DATE": "12-AUG-2026", "SUBMISSIONTYPE": "13F-NT", "CIK": "3", "PERIODOFREPORT": "30-JUN-2026"},
    ]
    covers = [
        {"ACCESSION_NUMBER": "A1", "ISAMENDMENT": "N", "AMENDMENTTYPE": "", "FILINGMANAGER_NAME": "OLD FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "B1", "ISAMENDMENT": "N", "AMENDMENTTYPE": "", "FILINGMANAGER_NAME": "NEW FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "B2", "ISAMENDMENT": "Y", "AMENDMENTTYPE": "RESTATEMENT", "FILINGMANAGER_NAME": "NEW FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "B3", "ISAMENDMENT": "Y", "AMENDMENTTYPE": "NEW HOLDINGS", "FILINGMANAGER_NAME": "NEW FUND", "REPORTTYPE": "13F HOLDINGS REPORT"},
        {"ACCESSION_NUMBER": "C1", "ISAMENDMENT": "N", "AMENDMENTTYPE": "", "FILINGMANAGER_NAME": "NOTICE", "REPORTTYPE": "13F NOTICE"},
    ]
    infos = [
        _info("A1", "037833100", 1500, 10), _info("A1", "037833100", 500, 5),           # two lines, same CUSIP (thousands era)
        _info("B1", "037833100", 999, 1), _info("B1", "594918104", 50, 1),
        _info("B2", "037833100", 2700, 10),                                                # restatement replaces B1
        _info("B3", "02079K305", 1000, 4), _info("B3", "037833100", 270, 1, "Put"),       # new holdings added
    ]
    h, _ = parse_13f_zip(_zip13f(subs, covers, infos))
    h = conform(h, "institutional_holdings")
    a = h.filter(pl.col("filer_cik") == "0000000001").row(0, named=True)
    assert a["shares"] == 15 and a["value_usd"] == 2_000_000          # summed + thousands -> dollars (pre-2023)
    b = h.filter(pl.col("filer_cik") == "0000000002")
    assert set(b["filing_id"]) == {"sec_edgar:B2", "sec_edgar:B3"}     # B1 superseded by the restatement
    assert "594918104" not in set(b["cusip"])
    aapl = b.filter((pl.col("cusip") == "037833100") & (pl.col("put_call") == "none")).row(0, named=True)
    assert aapl["value_usd"] == 2700 and aapl["knowledge_date"] == date(2026, 8, 20)
    assert b.filter(pl.col("put_call") == "put").height == 1
    assert h.filter(pl.col("filer_cik") == "0000000003").height == 0   # notices carry no holdings


def test_13f_dataset_links():
    html = ('<a href="/files/datastandardsinnovation/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">a</a>'
            '<a href="/files/structureddata/data/form-13f-data-sets/2023q4_form13f.zip">b</a>')
    links = dataset_links(html)
    assert links["01jun2026-31aug2026"].startswith("https://www.sec.gov/files/datastandardsinnovation/")
    assert "2023q4" in links


# ----------------------------------------------------------------------------------------------- FINRA / FTD

def test_finra_si_parse_and_provisional_ids(tmp_data):
    df = parse_finra_si((FX / "shrt20260915.csv").read_bytes(), date(2026, 9, 28), False)
    df = resolve_security_ids(df)                  # identity layer empty in tmp_data -> provisional ids
    out = conform(df, "short_interest")
    aapl = out.filter(pl.col("symbol") == "AAPL").row(0, named=True)
    assert aapl["security_id"] == "finra:AAPL" and aapl["security_id_resolved"] is False
    assert aapl["settlement_date"] == date(2026, 9, 15) and aapl["knowledge_date"] == date(2026, 9, 28)
    assert aapl["short_interest_shares"] > 1e6 and aapl["days_to_cover"] > 0
    assert (out["knowledge_date"] > out["settlement_date"]).all()


def test_ftd_parse_skips_trailer():
    df = conform(parse_ftd_zip((FX / "cnsfails202609a.zip").read_bytes(), "cnsfails202609a", date(2026, 9, 29), False),
                 "fails_to_deliver")
    assert df.height >= 5 and not df["cusip"].str.contains("Trailer").any()
    assert (df["settlement_date"] <= date(2026, 9, 15)).all()
    assert df.filter(pl.col("symbol") == "AAPL").height >= 1


def test_settlement_candidates_and_dates():
    c = candidate_settlement_dates(date(2026, 9, 1), date(2026, 10, 10))
    assert date(2026, 9, 15) in c and date(2026, 9, 30) in c and all(d.weekday() < 5 for d in c)
    assert business_days_after(date(2026, 9, 15), 8) == date(2026, 9, 25)
    assert ftd_period_end("cnsfails202609a") == date(2026, 9, 15)
    assert ftd_period_end("cnsfails202602b") == date(2026, 2, 28)


@pytest.mark.parametrize("val", ["1,234", "1234"])
def test_finra_number_parsing(val):
    csv = ('"accountingYearMonthNumber"|"symbolCode"|"issueName"|"issuerServicesGroupExchangeCode"|"marketClassCode"|'
           '"currentShortPositionQuantity"|"previousShortPositionQuantity"|"stockSplitFlag"|"averageDailyVolumeQuantity"|'
           '"daysToCoverQuantity"|"revisionFlag"|"changePercent"|"changePreviousNumber"|"settlementDate"\n'
           f'"20260915"|"XYZ"|"Xyz Inc"|"A"|"NYSE"|"{val}"|"1"||"10"|"1.00"||"0"|"0"|"2026-09-15"\n')
    df = parse_finra_si(csv.encode(), date(2026, 9, 25), True)
    assert df["short_interest_shares"][0] == 1234 and df["knowledge_date_estimated"][0] is True
