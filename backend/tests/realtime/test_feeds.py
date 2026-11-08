"""R1.2 feed parsers on recorded fixtures (tests/realtime/fixtures, recorded 2026-10-12 from the official sources)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from app.realtime.streams.disclosures import parse_dart, parse_hkex, parse_nsm, parse_tdnet
from app.realtime.streams.edgar import envelope, keep_form, parse_atom
from app.realtime.streams.news import rss_items

F = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 12, 23, 0, tzinfo=UTC)


def test_edgar_atom_groups_by_accession_and_extracts_items():
    rows = parse_atom((F / "edgar_current.atom").read_text(encoding="utf-8"))
    assert rows and len({r["accession"] for r in rows}) == len(rows)
    f4 = next(r for r in rows if r["form_type"] == "4")
    roles = {e["role"] for e in f4["entities"]}
    assert {"Reporting", "Issuer"} <= roles
    assert f4["company"]["role"] == "Issuer" and f4["owner"]["role"] == "Reporting"
    assert f4["accepted_at"].tzinfo is not None and len(f4["company"]["cik"]) == 10
    k8 = [r for r in rows if r["form_type"].startswith("8-K")]
    assert k8 and any(r["items"] for r in k8)
    assert all(it.count(".") == 1 for r in k8 for it in r["items"])
    env = envelope(f4, {"issuer_id": "IX", "security_id": "SX", "ticker": "T"})
    assert env.topic == "raw.filings" and env.key == f"sec_edgar:{f4['accession']}"
    assert env.payload["filing_id"] == env.key and env.payload["form_class"] == "insider"
    assert env.payload["issuer_id"] == "IX" and env.ts_source == f4["accepted_at"]
    assert keep_form("SCHEDULE 13G/A") and keep_form("8-K") and keep_form("4/A") and not keep_form("424B2")


def test_fca_nsm_search_parser():
    rows = parse_nsm(json.loads((F / "fca_nsm_search.json").read_text(encoding="utf-8")))
    assert len(rows) == 6
    r = rows[0]
    assert r["key"].startswith("NI-") and r["ts"].tzinfo is not None and r["url"].startswith("https://data.fca.org.uk/")
    assert r["company"] and not r["company"].endswith(";")


def test_tdnet_page_parser():
    rows = parse_tdnet((F / "tdnet_20261009.html").read_bytes(), date(2026, 10, 9))
    assert len(rows) == 100
    r = rows[0]
    assert r["ts"] is not None and r["ts"].tzinfo is not None and r["kdate"] == date(2026, 10, 9)
    assert r["url"].endswith(".pdf") and len(r["source_entity_id"]) in (4, 5) and r["language"] == "ja"
    assert {x["form_class"] for x in rows} <= {"current_report"}


def test_hkex_titles_parser():
    rows = parse_hkex(json.loads((F / "hkex_titles.json").read_text(encoding="utf-8")))
    assert len(rows) == 8
    r = rows[0]
    assert r["key"].startswith("news:") and r["ts"].tzinfo is not None and len(r["source_entity_id"]) == 5
    assert r["url"].startswith("https://www1.hkexnews.hk/")


def test_dart_list_parser():
    j = {"status": "000", "list": [
        {"corp_code": "00126380", "corp_name": "삼성전자", "stock_code": "005930", "corp_cls": "Y",
         "report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "rcept_no": "20261012000123", "rcept_dt": "20261012"},
        {"corp_code": "00164779", "corp_name": "SK하이닉스", "stock_code": "000660", "corp_cls": "Y",
         "report_nm": "분기보고서 (2026.09)", "rcept_no": "20261012000456", "rcept_dt": "20261012"}]}
    rows = parse_dart(j)
    assert [r["form_class"] for r in rows] == ["insider", "quarterly"]
    assert rows[0]["kdate"] == date(2026, 10, 12) and rows[0]["ts"] is None
    assert parse_dart({"status": "013"}) == []


def test_official_rss_parsers():
    fed = rss_items("rss_fed", (F / "rss_fed.xml").read_bytes(), ["US"], NOW)
    assert len(fed) >= 10 and all(r["published_at"] <= NOW for r in fed)
    assert all(r["item_id"] and r["url"].startswith("https://www.federalreserve.gov") for r in fed)
    bls = rss_items("rss_bls_cpi", (F / "rss_bls_cpi.xml").read_bytes(), ["US"], NOW)
    assert bls and "CPI" in bls[0]["title"].upper()
    ons = rss_items("rss_ons", (F / "rss_ons.xml").read_bytes(), ["GB"], NOW)
    assert ons
    # future-dated entries are dropped (PIT)
    assert rss_items("rss_ons", (F / "rss_ons.xml").read_bytes(), ["GB"], datetime(2020, 1, 1, tzinfo=UTC)) == []


def test_rss_keys_are_scheme_insensitive_and_eurostat_local_time():
    from app.realtime.streams.news import eurostat_fallback, news_envelope

    from app.ingestion.gdelt_gkg import item_id

    base = {"published_at": NOW, "source_id": "rss_boj", "title": "x"}
    a = news_envelope({**base, "url": "http://www.boj.or.jp/en/a.pdf", "item_id": item_id("http://www.boj.or.jp/en/a.pdf")},
                      "central_bank")
    b = news_envelope({**base, "url": "https://www.boj.or.jp/en/a.pdf",
                       "item_id": item_id("https://www.boj.or.jp/en/a.pdf")}, "central_bank")
    assert a.key == b.key == a.payload["item_id"] and a.payload["url"] == "https://www.boj.or.jp/en/a.pdf"
    xml = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title><item><title>PRC_HICP_MANR - "Dataset: '
           b'updated data"</title><link>https://ec.europa.eu/eurostat/x</link><pubDate>2026-10-12 11:00:00.0</pubDate>'
           b'</item><item><title>MIGR_ASY - x</title><link>https://ec.europa.eu/eurostat/y</link>'
           b'<pubDate>2026-10-12 11:00:00.0</pubDate></item></channel></rss>')
    rows = eurostat_fallback(xml, NOW)
    assert len(rows) == 1 and rows[0]["published_at"] == datetime(2026, 10, 12, 9, 0, tzinfo=UTC)   # CEST -> UTC
