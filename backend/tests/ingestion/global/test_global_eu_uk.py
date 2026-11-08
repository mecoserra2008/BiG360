"""EU / UK global adapters on recorded fixtures (no network): ESEF xBRL-JSON, FCA NSM DSH notices, BaFin CSV,
AFM export + detail, AMF metadata, Companies House missing-key path."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.schema import conform
from app.ingestion import afm, bafin_dd, fca_nsm
from app.ingestion._global_common import frame, run_adapter_job
from app.ingestion._xbrl_json import parse_period, parse_xbrl_json, split_concept
from app.ingestion.base import FetchResult

FX = Path(__file__).parent / "fixtures"


def test_xbrl_json_periods_and_concepts():
    assert parse_period("2023-01-01T00:00:00/2024-01-01T00:00:00") == (date(2023, 1, 1), date(2023, 12, 31))
    assert parse_period("2024-01-01T00:00:00") == (None, date(2023, 12, 31))
    assert split_concept("ifrs-full:Revenue") == ("ifrs-full", "Revenue")
    assert split_concept("asml:NetSystemSales") == ("other", "asml:NetSystemSales")
    assert split_concept("core:Turnover")[0] == "uk-core"


def test_esef_fixture_to_fundamentals_raw(tmp_data):
    doc = json.loads((FX / "esef_asml_2023.json").read_text())
    rows = parse_xbrl_json(doc, source_id="esef_xbrl_org", source_entity_id="724500Y6DUVHQD6OXN27",
                           filing_id="esef_xbrl_org:x", form_type="AFR", knowledge_date=date(2024, 2, 20),
                           report_period_end=date(2023, 12, 31))
    df = conform(frame(rows, "fundamentals_raw"), "fundamentals_raw")
    rev = df.filter((df["concept"] == "Revenue") & (df["period_end"] == date(2023, 12, 31)) & (df["dimensions_hash"] == ""))
    assert rev.height == 1 and rev["value"][0] == pytest.approx(27_558_500_000) and rev["unit"][0] == "EUR"
    assert rev["fiscal_period"][0] == "FY" and rev["taxonomy"][0] == "ifrs-full"
    assert df.filter(df["concept"] == "Assets")["period_type"].unique().to_list() == ["instant"]
    assert not df["knowledge_date_estimated"].any()


def test_pdmr_table_layout():
    rows = fca_nsm.parse_pdmr_notice((FX / "nsm_dsh_table_layout.html").read_bytes())
    assert len(rows) >= 4
    r0 = rows[0]
    assert r0["name"] == "Donal Murphy" and r0["lei"] == "213800UPS1QEE4TD3T45" and r0["isin"] == "IE0002424939"
    assert r0["date"] == date(2026, 10, 9) and r0["txn_type"] == "option_exercise" and r0["volume"] == 13786
    assert any(r["price"] == pytest.approx(64.4) and r["currency"] == "GBP" for r in rows)
    assert all(not r["is_open_market"] for r in rows)  # 'Outside a trading venue'


def test_pdmr_labelled_and_summary_layouts():
    lab = fca_nsm.parse_pdmr_notice((FX / "nsm_dsh_labelled_layout.html").read_bytes())
    assert lab and lab[0]["name"] and lab[0]["volume"] and lab[0]["price"] is not None and lab[0]["date"]
    summ = fca_nsm.parse_pdmr_notice((FX / "nsm_dsh_summary_layout.html").read_bytes())
    assert summ and all(r["date"] is not None for r in summ) and summ[0]["currency"] == "GBP"


def test_pdmr_helpers():
    assert fca_nsm.parse_price("1,234.5p") == (12.345, "GBP")
    assert fca_nsm.parse_price("(ii) £64.40 (€76.03)") == (64.4, "GBP")
    assert fca_nsm.parse_price("Nil")[0] == 0.0
    assert fca_nsm.classify_nature("Purchase of ordinary shares", "London Stock Exchange") == ("buy", True)
    assert fca_nsm.classify_nature("Sale of shares to cover income tax on vesting")[0] == "tax_withholding"
    assert fca_nsm.classify_nature("Purchase under dividend reinvestment plan (DRIP)", "XLON") == ("buy", False)


def test_bafin_csv_to_insiders(tmp_data):
    rows = bafin_dd.parse_bafin_csv((FX / "bafin_siemens.csv").read_bytes())
    fil, ins = bafin_dd.bafin_rows_to_tables(rows)
    df = conform(frame(ins, "insider_transactions"), "insider_transactions")
    assert df.height >= 3 and set(df["region"]) == {"EU"}
    sell = [r for r in ins if r["txn_type"] == "sell"][0]
    assert sell["shares"] == pytest.approx(160326.50 / 145.75) and sell["currency"] == "EUR"
    assert sell["knowledge_ts"].tzinfo is not None and sell["is_open_market"]
    assert sell["is_director"] and sell["source_entity_id"].startswith("DE")
    with pytest.raises(ValueError):
        bafin_dd.parse_bafin_csv(b"a;b;c\n1;2;3\n")


def test_afm_export_and_detail(tmp_data):
    exp = afm.parse_afm_export((FX / "afm_export.xml").read_bytes())
    assert len(exp) == 3 and exp[0]["lei"] == "724500XMW8FY9YNPRJ73" and exp[0]["transactiedatum"] == date(2026, 10, 8)
    assert afm.notice_date(exp[0]["meldingid"]) == date(2026, 10, 8)
    det = afm.parse_afm_detail((FX / "afm_detail.html").read_bytes())
    assert len(det) == 2 and det[0]["ISIN"].startswith("NL") and det[0]["Soort transactie"] == "Verkoop"
    assert afm.afm_txn(det[0]) == ("sell", False)  # OTC


def test_fca_nsm_normalize_end_to_end(tmp_data):
    a = fca_nsm.FcaNsmAdapter(settings=tmp_data)
    p = a.save_raw("dsh_html", "abc", (FX / "nsm_dsh_table_layout.html").read_bytes())
    fr = FetchResult("fca_nsm", started_at=datetime.now(UTC))
    fr.meta["docs"] = [{"disclosure_id": "abc", "publication_date": "2026-10-09T17:20:24Z", "lei": "213800UPS1QEE4TD3T45",
                        "company": "DCC", "download_link": "NSM/RNS/abc.html", "_kind": "dsh", "_raw_path": str(p)}]
    out = a.normalize(fr)
    fi, it = out["filings_index"], out["insider_transactions"]
    assert fi.height == 1 and fi["form_class"][0] == "insider" and fi["knowledge_date"][0] == date(2026, 10, 9)
    assert it.height >= 4 and set(it["form_type"]) == {"DSH"} and (it["knowledge_date"] >= it["transaction_date"]).all()


def test_missing_key_is_skipped(tmp_data):
    from app.ingestion.companies_house import CompaniesHouseAdapter

    tmp_data.companies_house_apikey = None
    res = run_adapter_job(CompaniesHouseAdapter, SimpleNamespace(settings=tmp_data, params={}, watermark=None))
    assert res.status == "skipped" and "missing key" in res.message
