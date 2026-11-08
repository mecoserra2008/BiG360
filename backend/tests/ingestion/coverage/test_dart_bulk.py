"""dart_bulk: DART 재무정보 일괄다운로드 adapter (recorded fixtures cut from the real 2026_2Q zips + detailSearch page)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from app.core.storage import read_table
from app.ingestion import dart_bulk
from app.ingestion._global_common import load_state

FIX = Path(__file__).parent / "fixtures"
ZIPS = ["2026_2Q_BS_20261008032921.zip", "2026_2Q_PL_20261008033244.zip", "2026_2Q_CF_20261008033534.zip"]
SEARCH_HTML = (FIX / "dart_bulk_detailsearch.html").read_text(encoding="utf-8")
LIST_HTML = "".join(
    f"<a onclick=\"download_ext002('2026','HY', '{n.split('_')[2]}', '{n}'); return false;\">다운로드</a>"
    for n in ZIPS + ["2026_2Q_CE_20261008033830.zip", "2025_4Q_PL_20261008030717.zip"])
SCOPE = ["005930", "006400", "105560"]  # 095570 (AJ네트웍스) is in the fixture zips but out of scope


def _adapter(tmp_data, search_html: str = SEARCH_HTML, **kw):
    a = dart_bulk.DartBulkAdapter(settings=tmp_data, **kw)
    for n in ZIPS:
        shutil.copy(FIX / f"dart_bulk_{n}", a.zips_dir / n)
    a.list_files = lambda: [f for f in dart_bulk.parse_list_html(LIST_HTML) if f["period"] == "2026_2Q"]

    def no_download(name, attempts=4):
        raise AssertionError(f"unexpected download {name}")

    a.download = no_download
    calls = []

    def search_page(*, ptype, ctype, start, end, page):
        calls.append((ptype, ctype, start, end, page))
        return search_html if (ptype, ctype) == ("A002", "P") else "<table></table>"

    a.search_page = search_page
    a.search_calls = calls
    return a


def test_parse_list_and_names():
    files = dart_bulk.parse_list_html(LIST_HTML)
    assert len(files) == 5
    f = dart_bulk.parse_zip_name("2025_4Q_PL_20261008030717.zip")
    assert f["period"] == "2025_4Q" and f["nominal_end"] == date(2025, 12, 31) and f["first_seen"] == date(2026, 10, 8)
    assert dart_bulk.parse_zip_name("2026_2Q_BS_20261008032921.zip")["nominal_end"] == date(2026, 6, 30)
    assert dart_bulk.concept_for("ifrs-full_Revenue", " 매출액") == ("ifrs-full_Revenue", False)
    assert dart_bulk.concept_for("ifrs_Revenue", "매출액") == ("ifrs_Revenue", False)
    assert dart_bulk.concept_for("entity00126362_Foo", "  매출액 ") == ("ifrs-full_Revenue", True)
    assert dart_bulk.concept_for("-표준계정코드 미사용-", "  기타  수익 ") == ("dart_label:기타 수익", False)
    assert dart_bulk.months_into_fy(date(2026, 6, 30), 3) == 3 and dart_bulk.months_into_fy(date(2026, 3, 31), 9) == 6
    assert dart_bulk.value_columns(["항목명", "당기 반기 3개월 ", "당기 반기 누적 ", "전기 반기 3개월 "]) == {"q": 1, "ytd": 2}


def test_parse_search_and_filing_index():
    rows, pages = dart_bulk.parse_search_html(SEARCH_HTML)
    assert pages == 1 and {r["corp_name"] for r in rows} == {"삼성전자", "삼성SDI", "KB금융"}
    sdi = next(r for r in rows if r["corp_name"] == "삼성SDI")
    assert sdi["rcp_no"] == "20260814003578" and sdi["rcept_dt"] == "2026-08-14" and sdi["kind"] == "A002"
    assert sdi["ym"] == "2026.06" and not sdi["is_amendment"]
    amended = {**sdi, "rcp_no": "20260902000111", "rcept_dt": "2026-09-02", "is_amendment": True,
               "report_nm": "[기재정정]반기보고서 (2026.06)"}
    idx = dart_bulk.build_filing_index([amended, sdi])
    hit = idx[(dart_bulk.norm_name("삼성SDI"), "A002", "2026.06")]
    assert hit["rcp_no"] == "20260814003578" and hit["latest_rcp_no"] == "20260902000111" and hit["n_versions"] == 2
    assert dart_bulk.norm_name("(주)삼성 SDI") == dart_bulk.norm_name("삼성SDI")


def test_run_writes_facts_and_filings(tmp_data):
    a = _adapter(tmp_data)
    fetched, rows = a.run(entities=SCOPE)
    assert rows["fundamentals_raw"] > 100 and rows["filings_index"] == 3
    assert fetched.meta["stats"]["2026_2Q"]["imputed"] == 0
    fr = read_table("fundamentals_raw", settings=tmp_data)
    assert set(fr["source_entity_id"].unique()) == set(SCOPE)
    assert set(fr["source_id"].unique()) == {"dart_bulk"} and set(fr["taxonomy"].unique()) == {"dart"}
    sdi = fr.filter((fr["source_entity_id"] == "006400") & (fr["concept"] == "ifrs-full_Revenue") & fr["consolidated"])
    h1 = sdi.filter(sdi["fiscal_period"] == "H1")
    assert h1["value"][0] == 7_345_161_306_669 and h1["period_start"][0] == date(2026, 1, 1)
    assert h1["period_end"][0] == date(2026, 6, 30) and h1["knowledge_date"][0] == date(2026, 8, 14)
    assert h1["filing_id"][0] == "dart_bulk:20260814003578" and not h1["knowledge_date_estimated"][0]
    assert h1["unit"][0] == "KRW" and h1["form_type"][0] == "11012" and h1["fiscal_year"][0] == 2026
    q2 = sdi.filter(sdi["fiscal_period"] == "Q2")
    assert q2["value"][0] == 3_768_808_123_247 and q2["period_start"][0] == date(2026, 4, 1)
    # separate statements carry the SeparateMember dimension (issuer also reports consolidated)
    sep = fr.filter((fr["source_entity_id"] == "006400") & (fr["concept"] == "ifrs-full_Revenue") & ~fr["consolidated"]
                    & (fr["fiscal_period"] == "H1"))
    assert sep["value"][0] == 6_163_551_086_271 and "SeparateMember" in sep["dimensions_json"][0]
    # balance sheet instants and cash-flow YTD
    ta = fr.filter((fr["source_entity_id"] == "005930") & (fr["concept"] == "ifrs-full_Assets") & fr["consolidated"])
    assert ta.height == 1 and ta["period_type"][0] == "instant" and ta["period_end"][0] == date(2026, 6, 30)
    assert ta["value"][0] > 4e14
    cfo = fr.filter((fr["source_entity_id"] == "005930")
                    & (fr["concept"] == "ifrs-full_CashFlowsFromUsedInOperatingActivities") & fr["consolidated"])
    assert cfo.height == 1 and cfo["fiscal_period"][0] == "H1" and cfo["period_start"][0] == date(2026, 1, 1)
    # financial-format statement (single YTD column) for KB
    kb = fr.filter((fr["source_entity_id"] == "105560") & (fr["concept"] == "ifrs-full_ProfitLoss") & fr["consolidated"])
    assert kb.filter(kb["fiscal_period"] == "H1")["value"][0] == 3_926_977_000_000
    fi = read_table("filings_index", settings=tmp_data)
    assert set(fi["form_class"].unique()) == {"semiannual"} and fi["has_xbrl"].sum() == 0
    assert set(fi["filing_id"]) == {"dart_bulk:20260814003699", "dart_bulk:20260814003578", "dart_bulk:20260814004200"}
    # only the markets of in-scope issuers are crawled (KOSPI), one window per report type present
    assert {c[1] for c in a.search_calls} == {"P"}
    st = load_state("dart_bulk", tmp_data)
    assert st["normalized"]["2026_2Q"]["files"] == sorted(ZIPS) and set(st["done"]) == set(ZIPS)
    # resume: nothing left to do for the same scope (no downloads, no re-normalization)
    b = _adapter(tmp_data)
    f2, rows2 = b.run(entities=SCOPE)
    assert f2.meta["periods"] == [] and rows2["fundamentals_raw"] == 0


def test_imputed_knowledge_date_when_no_filing_found(tmp_data):
    a = _adapter(tmp_data, search_html="<table></table>")
    fetched, rows = a.run(entities=["006400"])
    assert fetched.meta["stats"]["2026_2Q"]["imputed"] == 1
    fr = read_table("fundamentals_raw", settings=tmp_data)
    # statutory deadline: H1 end 2026-06-30 + 60 d = Sat 2026-08-29 -> next weekday (zip date 2026-10-08 does not cap)
    assert fr["knowledge_date_estimated"].all() and set(fr["knowledge_date"].unique()) == {date(2026, 8, 31)}
    assert set(fr["filing_id"].unique()) == {"dart_bulk:006400:2026H1:20260630"}
    fi = read_table("filings_index", settings=tmp_data)
    assert fi.height == 1 and "knowledge_date_estimated=true" in fi["items"][0].to_list()


def test_discover_from_landed_zip(tmp_data):
    a = _adapter(tmp_data)
    d = a.discover()
    assert {"005930", "006400", "105560", "095570"} <= set(d["source_entity_id"].to_list())
    assert d.filter(d["source_entity_id"] == "005930")["kr_stock_code"][0] == "005930"


@pytest.mark.network
def test_live_list():
    a = dart_bulk.DartBulkAdapter()
    files = a.list_files()
    assert len(files) >= 150 and any(f["period"] == "2015_4Q" for f in files)
