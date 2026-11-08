"""Asia adapters on recorded / documented fixtures (no network): EDINET CSV, TDnet list, OpenDART JSON, BaoStock rows,
AKShare Sina/CNINFO records, HKEX DI table, JPX + KIND lists."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from app.core.schema import conform
from app.ingestion import baostock_cn, cn_akshare, edinet, hkex, jpx_list, kind_list, opendart, tdnet
from app.ingestion._global_common import frame, run_adapter_job

FX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------ EDINET
def test_edinet_context_resolution():
    dei = {"CurrentFiscalYearStartDateDEI": "2025-04-01", "CurrentFiscalYearEndDateDEI": "2026-03-31",
           "CurrentPeriodEndDateDEI": "2025-09-30"}
    sub = date(2025, 11, 10)
    assert edinet.resolve_context("CurrentYearDuration", dei, sub)[:3] == (date(2025, 4, 1), date(2026, 3, 31), "FY")
    assert edinet.resolve_context("Prior1YearInstant_NonConsolidatedMember", dei, sub)[1] == date(2025, 3, 31)
    assert edinet.resolve_context("InterimDuration", dei, sub)[:3] == (date(2025, 4, 1), date(2025, 9, 30), "H1")
    assert edinet.resolve_context("Prior1InterimDuration", dei, sub)[:2] == (date(2024, 4, 1), date(2024, 9, 30))
    assert edinet.resolve_context("CurrentQuarterDuration", dei, sub)[:2] == (date(2025, 7, 1), date(2025, 9, 30))
    assert edinet.resolve_context("Weird", dei, sub)[1] is None


def test_edinet_csv_zip_to_facts(tmp_data):
    files = edinet.read_csv_zip((FX / "edinet_S100YE2C_type5.zip").read_bytes())
    assert len(files) == 1  # auditor file skipped
    rows, dei = edinet.parse_edinet_csv(files[0][1], source_entity_id="E01777", filing_id="edinet:S100YE2C",
                                        form_type="120", knowledge_date=date(2026, 6, 18))
    assert dei["AccountingStandardsDEI"] == "IFRS"
    df = conform(frame(rows, "fundamentals_raw"), "fundamentals_raw")
    sales = df.filter((df["concept"] == "jpigp_cor:NetSalesIFRS") & (df["dimensions_hash"] == "")
                      & (df["period_end"] == date(2026, 3, 31)))
    assert sales.height == 1 and sales["value"][0] == 12_479_620_000_000 and sales["fiscal_year"][0] == 2026
    assert sales["period_start"][0] == date(2025, 4, 1) and sales["unit"][0] == "JPY" and sales["taxonomy"][0] == "jpigp"
    nonc = df.filter(df["concept"] == "jppfs_cor:NetSales")
    assert nonc.height >= 1 and not nonc["consolidated"].any()
    seg = df.filter(df["dimensions_hash"] != "")
    assert seg.height > 0 and seg["dimensions_json"].null_count() == 0
    hist = df.filter(df["concept"] == "jpcrp_cor:TotalAssetsIFRSSummaryOfBusinessResults")
    assert hist["period_end"].min() == date(2022, 3, 31)  # Prior4YearInstant


def test_edinet_missing_key_skipped(tmp_data):
    tmp_data.edinet_apikey = None
    res = run_adapter_job(edinet.EdinetAdapter, SimpleNamespace(settings=tmp_data, params={}, watermark=None))
    assert res.status == "skipped" and "EDINET_APIKEY" in res.message


# ------------------------------------------------------------------ TDnet
def test_tdnet_page_parse():
    rows = tdnet.parse_tdnet_page((FX / "tdnet_I_list_001_20261009.html").read_bytes(), date(2026, 10, 9))
    assert len(rows) == 6 and rows[0]["code5"] == "76030" and rows[0]["time"] == "19:30"
    assert rows[1]["xbrl"] and rows[1]["id"] == "140120261009549004"
    assert tdnet.classify_title("2027年2月期 第2四半期(中間期) 決算短信〔日本基準〕(連結)") == ("TANSHIN", "current_report", "H1")
    assert tdnet.classify_title("2026年3月期 決算短信〔IFRS〕(連結)")[2] == "FY"


# ------------------------------------------------------------------ OpenDART (documented response shapes)
FNLTT = [
    {"rcept_no": "20260515000123", "sj_div": "BS", "account_id": "ifrs-full_Assets", "account_nm": "자산총계",
     "thstrm_amount": "514,531,948,000,000", "currency": "KRW"},
    {"rcept_no": "20260515000123", "sj_div": "IS", "account_id": "ifrs-full_Revenue", "account_nm": "수익(매출액)",
     "thstrm_amount": "79,140,000,000,000", "thstrm_add_amount": "79,140,000,000,000", "currency": "KRW"},
    {"rcept_no": "20260515000123", "sj_div": "IS", "account_id": "dart_OperatingIncomeLoss", "account_nm": "영업이익",
     "thstrm_amount": "6,685,000,000,000", "currency": "KRW"},
    {"rcept_no": "20260515000123", "sj_div": "CF", "account_id": "-표준계정코드 미사용-", "account_nm": "배당금의 지급",
     "thstrm_amount": "-2,452,000,000,000", "currency": "KRW"},
    {"rcept_no": "20260515000123", "sj_div": "SCE", "account_id": "ifrs-full_Equity", "account_nm": "자본",
     "thstrm_amount": "1", "currency": "KRW"},
]


def test_opendart_statement_rows(tmp_data):
    rows = opendart.parse_fnltt_rows(FNLTT, corp_code="00126380", bsns_year=2026, reprt_code="11013", fs_div="CFS",
                                     knowledge_date=date(2026, 5, 15), filing_id="opendart:20260515000123")
    df = conform(frame(rows, "fundamentals_raw"), "fundamentals_raw")
    assert df.height == 4  # SCE skipped
    rev = df.filter(df["concept"] == "ifrs-full_Revenue")
    assert rev["period_start"][0] == date(2026, 1, 1) and rev["period_end"][0] == date(2026, 3, 31)
    assert rev["value"][0] == 79_140_000_000_000 and rev["taxonomy"][0] == "kifrs" and rev["consolidated"][0]
    assert df.filter(df["concept"] == "dart_label:배당금의 지급")["value"][0] < 0
    assert opendart.report_kind("반기보고서 (2026.06)") == ("11012", 2026)
    assert opendart.report_kind("분기보고서 (2025.09)") == ("11014", 2025)
    assert opendart.fy_period_end(2025, "11012")[1:] == (date(2025, 6, 30), date(2025, 4, 1))
    assert opendart.concept_of("", "매출액") == ("kifrs", "ifrs-full_Revenue")


def test_opendart_insiders(tmp_data):
    ele = [{"rcept_no": "20260901000456", "rcept_dt": "2026-09-01", "corp_code": "00126380", "repror": "홍길동",
            "isu_exctv_rgist_at": "등기임원", "isu_exctv_ofcps": "대표이사", "isu_main_shrholdr": "-",
            "sp_stock_lmp_cnt": "10,000", "sp_stock_lmp_irds_cnt": "2,000"}]
    maj = [{"rcept_no": "20260902000789", "rcept_dt": "2026-09-02", "corp_code": "00126380", "repror": "국민연금공단",
            "stkqy": "450,000,000", "stkqy_irds": "-1,000,000", "report_resn": "보유주식등의 수 변동"}]
    f1, i1 = opendart.elestock_rows(ele, "00126380")
    f2, i2 = opendart.majorstock_rows(maj, "00126380")
    df = conform(frame(i1 + i2, "insider_transactions"), "insider_transactions")
    assert df.height == 2 and set(df["region"]) == {"KR"}
    e = df.filter(df["form_type"] == "ELESTOCK")
    assert e["txn_type"][0] == "buy" and e["role_norm"][0] == "ceo" and e["is_director"][0]
    m = df.filter(df["form_type"] == "MAJORSTOCK")
    assert m["txn_type"][0] == "sell" and m["is_ten_pct_owner"][0] and not m["is_open_market"][0]
    assert conform(frame(f1 + f2, "filings_index"), "filings_index").height == 2


def test_opendart_missing_key_skipped(tmp_data):
    tmp_data.opendart_apikey = None
    res = run_adapter_job(opendart.OpenDartAdapter, SimpleNamespace(settings=tmp_data, params={}, watermark=None))
    assert res.status == "skipped" and "OPENDART_APIKEY" in res.message


# ------------------------------------------------------------------ BaoStock / AKShare
def test_baostock_rows(tmp_data):
    fields = ["code", "pubDate", "statDate", "roeAvg", "npMargin", "gpMargin", "netProfit", "epsTTM", "MBRevenue",
              "totalShare", "liqaShare"]
    rows = [["sh.600519", "2026-04-17", "2025-12-31", "0.344620", "0.505279", "0.911796", "85310324833.670000",
             "65.736665", "172054171890.910000", "1252270215.00", "1252270215.00"]]
    fil, facts = baostock_cn.bs_rows_to_facts(fields, rows)
    df = conform(frame(facts, "fundamentals_raw"), "fundamentals_raw")
    np_ = df.filter(df["concept"] == "baostock:netProfit")
    assert np_["knowledge_date"][0] == date(2026, 4, 17) and np_["unit"][0] == "CNY" and np_["fiscal_period"][0] == "FY"
    assert df.filter(df["concept"] == "baostock:totalShare")["period_type"][0] == "instant"
    assert fil[0]["source_entity_id"] == "SSE:600519" and fil[0]["form_class"] == "annual"
    assert baostock_cn.to_bs_code("300750.SZ") == "sz.300750" and baostock_cn.to_bs_code("600519") == "sh.600519"
    assert baostock_cn.cn_entity("sz.000858") == "SZSE:000858"


def test_akshare_sina_and_cninfo(tmp_data):
    recs = [
        {"报告日": "20251231", "营业总收入": "172054171890.91", "归属于母公司所有者的净利润": "82320067101.68",
         "基本每股收益": "65.66", "数据源": "定期报告", "是否审计": "是", "公告日期": "20260417", "币种": "CNY",
         "类型": "合并期末", "更新日期": "2026-04-16T15:54:02"},
        {"报告日": "20241231", "营业总收入": "174144000000", "归属于母公司所有者的净利润": "86228000000",
         "数据源": "定期报告", "公告日期": "20260417", "币种": "CNY", "类型": "合并期末"},  # restated comparative
        {"报告日": "20250930", "营业总收入": "nan", "归属于母公司所有者的净利润": "64626746712.0", "公告日期": "20251030",
         "币种": "CNY", "类型": "合并期末"},
    ]
    cninfo = {"年报": [{"公告标题": "贵州茅台2024年年度报告", "公告时间": "2025-04-03 16:30:00", "公告链接": "http://x?announcementId=123&a=1"},
                       {"公告标题": "贵州茅台2024年年度报告摘要", "公告时间": "2025-04-03 16:30:00", "公告链接": "http://x?announcementId=124"}]}
    orig = cn_akshare.cninfo_original_dates(cninfo)
    assert orig == {date(2024, 12, 31): date(2025, 4, 3)}
    fil, facts = cn_akshare.sina_rows_to_facts(recs, "利润表", "SSE:600519", today=date(2026, 10, 10), original_dates=orig)
    df = conform(frame(facts, "fundamentals_raw"), "fundamentals_raw")
    r25 = df.filter((df["period_end"] == date(2025, 12, 31)) & (df["concept"] == "营业总收入"))
    assert r25["knowledge_date"][0] == date(2026, 4, 17) and not r25["knowledge_date_estimated"][0]
    assert r25["period_start"][0] == date(2025, 1, 1) and r25["taxonomy"][0] == "cas_label"
    r24 = df.filter(df["period_end"] == date(2024, 12, 31))
    assert set(r24["knowledge_date"]) == {date(2025, 4, 3)} and r24["knowledge_date_estimated"].all()
    assert df.filter(df["concept"] == "基本每股收益")["unit"][0] == "CNY/shares"
    assert df.filter(df["period_end"] == date(2025, 9, 30))["fiscal_period"][0] == "9M"
    filings = cn_akshare.cninfo_rows_to_filings(cninfo["年报"], "SSE:600519", "年报")
    assert len(filings) == 1 and filings[0]["filing_id"] == "akshare:cninfo:123" and filings[0]["knowledge_ts"].hour == 8


def test_akshare_allowlist():
    with pytest.raises(ValueError):
        cn_akshare.ak_call("stock_zh_a_spot_em")


# ------------------------------------------------------------------ HKEX
def test_hkex_di_table(tmp_data):
    rows = hkex.parse_di_table((FX / "hkex_di_formlist_00700.html").read_bytes())
    assert len(rows) == 21
    r = rows[0]
    assert r["serial"] == "DA20260819E00389" and r["filed"] == date(2026, 8, 19) and r["code"] == "1316"
    assert r["shares"] == 1682 and r["held_after"] == 44172 and r["event_date"] == date(2026, 8, 18)
    assert hkex.hk5("0700.HK") == "00700"
    assert hkex.classify_news("Annual Report 2025", "Financial Statements/ESG Information") == ("ANNUAL_REPORT", "annual")


# ------------------------------------------------------------------ reference lists
def test_jpx_and_kind_parsers(tmp_path):
    df = pd.DataFrame({"日付": ["20260930"], "コード": ["7203"], "銘柄名": ["トヨタ自動車"], "市場・商品区分": ["プライム（内国株式）"],
                       "33業種コード": ["3700"], "33業種区分": ["輸送用機器"], "17業種コード": ["6"], "17業種区分": ["自動車・輸送機"],
                       "規模コード": ["1"], "規模区分": ["TOPIX Core30"]})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    out = jpx_list.parse_jpx_xlsx(buf.getvalue())
    assert out.row(0, named=True)["tse33_code"] == "3700" and out.row(0, named=True)["code"] == "7203"
    html = ("<table><tr><th>회사명</th><th>시장구분</th><th>종목코드</th><th>업종</th><th>주요제품</th><th>상장일</th>"
            "<th>결산월</th><th>대표자명</th><th>홈페이지</th><th>지역</th></tr><tr><td>삼성전자</td><td>유가증권</td>"
            "<td>005930</td><td>통신 및 방송 장비 제조업</td><td>반도체</td><td>1975-06-11</td><td>12월</td><td>x</td>"
            "<td>http://www.samsung.com</td><td>경기도</td></tr></table>").encode("euc-kr")
    k = kind_list.parse_kind(html)
    assert k.row(0, named=True)["stock_code"] == "005930" and k.row(0, named=True)["fy_end_month"] == "12"
