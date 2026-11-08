"""Korea DART "재무정보 일괄다운로드" (bulk K-IFRS financial statements, no API key) -> fundamentals_raw + filings_index.

Source research: docs/COMPANY_COVERAGE_OPTIONS.md §4.1 (live-verified 2026-10-11).
  list      POST https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/list.do      (HTML; ~172 zip names)
  download  GET  https://opendart.fss.or.kr/cmm/downloadFnlttZip.do?fl_nm=<YYYY>_<n>Q_<BS|PL|CF|CE>_<ts14>.zip
            FY2015 (2015_4Q) then 2016_1Q .. latest; nQ=4 is the annual (사업보고서) file. A browser User-Agent is
            required (otherwise an HTML error page is served). No Range support; ~10 KB/s per connection, so files are
            landed to data/raw/dart_bulk/zips/ first (verified with zipfile) and normalized from disk.
  content   cp949 TSV per statement variant (e.g. 02_손익계산서[_연결], 03_포괄손익계산서[_금융기타|_보험|_은행|_증권][_연결]).
            Columns: 재무제표종류, 종목코드 ([005930] / [null]), 회사명, 시장구분, 업종, 업종명, 결산월, 결산기준일,
            보고서종류, 통화, 항목코드, 항목명, then value columns '당기 <기간> 3개월' (discrete quarter), '당기 <기간> 누적'
            (YTD), '당기 <기간>말' (BS instant / financial-format YTD), '전기…' (comparatives; ignored). Full KRW units
            (Samsung SDI consolidated H1-2026 revenue 7,345,161,306,669). '- 연결' = consolidated, '- 별도' = separate.
            Values are the latest amended ones as of the zip timestamp (files are regenerated); the file has NO filing
            date. Members are streamed line by line (a member is up to ~30 MB of text).
  filings   (receipt dates, PIT) one provider per bulk period, chosen at run time:
            * opendart_api: GET https://opendart.fss.or.kr/api/list.json (pblntf_ty=A, pblntf_detail_ty A001 사업 /
              A002 반기 / A003 분기, corp_cls Y/K/N, windows <= 89 days, 100 rows/page). Used automatically as soon as
              OPENDART_APIKEY is set (presence checked only; the value is never logged or stored); calls count against
              the shared OpenDART daily cap (app.core.ratelimit.get_daily_quota). Rows carry stock_code -> exact join.
              Periods normalized with another provider are re-normalized once the key is present (upgrade).
            * dart_search (keyless): POST https://dart.fss.or.kr/dsab007/detailSearch.ax (HTML, 100 rows/page, windows
              <= 180 days, corporationType P/A/N; robots.txt allows it; project User-Agent). Rows give corp_code,
              current corp name, rcpNo (first 8 digits = receipt date) and report name '(YYYY.MM)'.
            Market-wide windows only for the most common period end of each report kind (and ends shared by >= 40
            companies); the few dozen non-December fiscal-year companies per period are looked up one by one
            (detailSearch textCrpCik = corp_code from the crosswalk, else textCrpNm = bulk name; list.json corp_code),
            every periodic kind at once, instead of widening the windows over a year of unrelated filings (keyless
            backfill: ~700 pages + ~1,770 lookups instead of ~5,900 pages). Windows are cached per period under
            raw/dart_bulk/filings[_api]/<period>.json.gz; a cached window is final up to the day before its crawl
            date, so only the open tail of a window is re-crawled.
  join      bulk row (stock code, name at filing time, report kind, period-end month) -> filing. The report kind is the
            bulk period's (4Q 사업 / 2Q 반기 / 1Q, 3Q 분기; e.g. REITs with 6-month fiscal years), then the
            months-into-FY kind; fiscal_period labels keep the months rule. Join order:
            stock_code (API rows) > corp_code via the crosswalk > unique normalized name of the same report kind/month
            (search names are CURRENT names, so renamed companies match through the crosswalk learned from recent
            periods). A name hit whose corp_code belongs to another stock code, or a receipt date before the period
            end / after the zip date, is rejected. knowledge_date of the filing = ORIGINAL receipt (rcept_dt); facts
            take the receipt of the latest version filed on or before the zip date (param amended_kd=latest, default:
            the bulk values may include that amendment; amendments after the crawl window are not seen - see
            values_as_of in filings_index items). Unmatched -> conservative statutory deadline (FSCMA Art. 159 / 160:
            annual 90 days + 5 business days extension, interim 60 days, next weekday), capped at the zip date (the zip
            already carried the values then), knowledge_date_estimated=True, basis kr_statutory_deadline.
  crosswalk raw/dart_bulk/crosswalk.parquet: kr_stock_code <-> dart_corp_code <-> issuer_id / security_id (securities
            master via identifier_map kr_stock_code). Sources by rank: OpenDART corpCode.xml / list.json (key) >
            identifier_map dart_corp_code > learned unique name match. discover() exposes dart_corp_code.
Concepts: 항목코드 as given when standard ('ifrs-full_*', 'ifrs_*', 'dart_*'); otherwise opendart's KR_LABEL_TO_ID
re-key when the label is standard and no standard row exists, else 'dart_label:<항목명>'. taxonomy 'dart'.
source_entity_id = 6-char KRX stock code (identity id_type kr_stock_code); issuer_id is filled at ingest when the
stock code resolves in identifier_map. Separate (별도) facts are emitted with consolidated=False and, when the issuer
also has consolidated statements for the period, the dimension ConsolidatedAndSeparateFinancialStatementsAxis=
SeparateMember (the PK has no consolidated column).
Scope: all_listed=True -> every KR stock code of the securities master (identifier_map kr_stock_code, open validity;
falls back to every code in the zips when the reference is missing); False -> the mvp_global seed; unset -> sticky
(state scope_mode, set by the first all_listed run). Explicit entities override both.
Resumable / chunked: per period the covered stock codes are kept in raw/dart_bulk/normalized/<period>.json (reset when
the zip set changes, on renormalize, or on the provider upgrade); only uncovered codes are normalized (chunk_codes
per pass over the zips) and each period is written on its own, replacing every existing row of the same (stock code,
period end) or primary key (no stale rows when a filing id / fiscal year changes); the kept rows of a partition are
streamed (sink_parquet), never loaded whole. Unmatched filings get the id <code>:<FY><fp>:<period end>. renormalize=True starts a generation
(state renorm_since) that later runs continue until every landed period has been redone. Before each period the run
stops (partial, resumable) when free RAM < min_free_gb or the run grew by more than ram_budget_gb. Measured on a
scratch copy (all ~2,650 issuers, 8 periods): ~20 s and ~0.5 M facts per period, peak 3.2 GB commit / 2.3 GB
working set, ~18 MB of parquet per fiscal-year partition.
Params: all_listed, statements ('BS,PL,CF'), periods ('2025_4Q,...'), max_periods (periods worked per run, newest
first; default 4 when all_listed, else all), workers (<=2 concurrent downloads), separate (True), filing_dates (True |
'cache' = cached windows only, no crawl | False), renormalize (False), offline (False: no list.do / downloads, landed
zips only), chunk_codes (800), amended_kd ('latest' | 'original'), min_free_gb (1.5), ram_budget_gb (4.0).
"""

from __future__ import annotations

import gc
import gzip
import hashlib
import io
import os
import re
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient
from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (chunked, fact_row, filing_row, fiscal_year_for, frame, load_state,
                                          parse_num, period_start_for, run_adapter_job, safe_call, save_state,
                                          seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter
from app.ingestion.opendart import KR_LABEL_TO_ID

LIST_URL = "https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/list.do"
DL_URL = "https://opendart.fss.or.kr/cmm/downloadFnlttZip.do"
SEARCH_URL = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
SEARCH_REFERER = "https://dart.fss.or.kr/dsab007/main.do"
API_BASE = "https://opendart.fss.or.kr/api"
#: the bulk download serves an HTML error page to non-browser clients (list.do / downloadFnlttZip.do only)
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36",
    "Referer": "https://opendart.fss.or.kr/disclosureinfo/fnltt/dwld/main.do",
}
ZIP_RE = re.compile(r"(\d{4})_([1-4])Q_(BS|PL|CF|CE)_(\d{14})\.zip")
REPORT_RE = re.compile(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)")
SEP_DIMS = {"ConsolidatedAndSeparateFinancialStatementsAxis": "SeparateMember"}
#: months into the fiscal year -> (report kind, DART reprt_code, YTD fiscal_period, discrete fiscal_period)
MONTHS = {3: ("A003", "11013", "Q1", "Q1"), 6: ("A002", "11012", "H1", "Q2"), 9: ("A003", "11014", "9M", "Q3"),
          12: ("A001", "11011", "FY", "Q4")}
KIND_OF_REPORT = {"사업": "A001", "반기": "A002", "분기": "A003"}
#: report kind of a bulk period ('2025_4Q' = 사업보고서 file, '2026_2Q' = 반기, 1Q / 3Q = 분기). It wins over the
#: months-into-FY guess, e.g. REITs with 6-month fiscal years (결산월 06, closing Jun + Dec: the Dec report is a 사업보고서)
BULK_KIND = {4: "A001", 2: "A002", 1: "A003", 3: "A003"}
ALL_KINDS = "A"  # per-company lookups ask for every periodic report kind at once
FORM_CLASS = {"A001": "annual", "A002": "semiannual", "A003": "quarterly"}
#: an (end, kind) shared by fewer companies than this (and not the most common end of its kind) is looked up per
#: company (corp_code from the crosswalk, else the bulk name) instead of widening the market-wide windows
RARE_MAX = 40
FILING_FP = {3: "Q1", 6: "H1", 9: "Q3", 12: "FY"}
MARKET_CODE = {"유가증권시장상장법인": "P", "코스닥시장상장법인": "A", "코넥스시장상장법인": "N", "기타법인": "E"}
MARKET_MIC = {"P": "XKRX", "A": "XKOS", "N": "XKON"}
#: market letter (detailSearch corporationType) <-> OpenDART list.json corp_cls
API_CORP_CLS = {"P": "Y", "A": "K", "N": "N", "E": "E"}
API_CLS_MARKET = {v: k for k, v in API_CORP_CLS.items()}
SEARCH_MAX_DAYS = 180   # detailSearch rejects long date ranges
API_MAX_DAYS = 89       # list.json without corp_code: at most 3 months
#: days after the period end covered by the filing-date crawl, per report kind
WINDOW_LAG = {"A001": 150, "A002": 120, "A003": 120}
#: FSCMA (자본시장법) Art. 159: annual report within 90 days of the FY end, an extension notice adds up to 5 business
#: days; Art. 160: half-year / quarterly reports within 45 days (60 days for first-time consolidated filers -> 60 used).
#: A deadline on a weekend moves to the next weekday (Civil Act Art. 161); public holidays are not modelled (the
#: interim deadline carries 15 days of slack, the annual one the 5-day extension).
KR_ANNUAL_DAYS, KR_ANNUAL_EXT_BDAYS, KR_INTERIM_DAYS = 90, 5, 60
PROVIDER_API, PROVIDER_SEARCH, PROVIDER_NONE = "opendart_api", "dart_search", "none"
#: crosswalk source -> rank (a higher rank replaces a lower one)
XW_RANK = {"opendart_corpcode": 3, "opendart_list": 3, "identifier_map": 2, "name_match": 1}
XW_SCHEMA = {"kr_stock_code": pl.Utf8, "dart_corp_code": pl.Utf8, "corp_name": pl.Utf8, "bulk_name": pl.Utf8,
             "market": pl.Utf8, "src": pl.Utf8, "confidence": pl.Float64, "learned_period": pl.Utf8,
             "issuer_id": pl.Utf8, "security_id": pl.Utf8, "updated_at": pl.Utf8}


class DartBulkError(RuntimeError):
    pass


class DartQuotaExceeded(DartBulkError):
    pass


# ------------------------------------------------------------------------------------------------ pure helpers
def parse_zip_name(name: str) -> dict[str, Any] | None:
    m = ZIP_RE.fullmatch(name)
    if not m:
        return None
    y, q, stmt, ts = int(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    end = date(y, q * 3, 30 if q in (2, 3) else 31)
    return {"name": name, "period": f"{y}_{q}Q", "year": y, "q": q, "stmt": stmt, "ts": ts,
            "nominal_end": end, "first_seen": date(int(ts[:4]), int(ts[4:6]), int(ts[6:8]))}


def parse_list_html(html: str) -> list[dict[str, Any]]:
    out, seen = [], set()
    for n in ZIP_RE.finditer(html):
        if n.group(0) not in seen:
            seen.add(n.group(0))
            out.append(parse_zip_name(n.group(0)))
    return [x for x in out if x]


def norm_name(s: str | None) -> str:
    s = re.sub(r"\(주\)|㈜|주식회사|\(株\)", "", s or "")
    return re.sub(r"[\s·.,\-()]+", "", s).upper()


def norm_code(v: Any) -> str | None:
    s = str(v or "").strip().upper().split(".")[0]
    if not s:
        return None
    return s.zfill(6) if s.isdigit() else s


def concept_for(code: str | None, label: str | None) -> tuple[str, bool]:
    """-> (concept, label_mapped). Standard ids kept as given; else KR_LABEL_TO_ID re-key; else dart_label:<label>."""
    aid = (code or "").strip()
    nm = re.sub(r"\s+", " ", (label or "").strip())
    if re.match(r"^(ifrs-full_|ifrs_|dart_)\w+", aid):
        return aid, False
    if nm in KR_LABEL_TO_ID:
        return KR_LABEL_TO_ID[nm], True
    return f"dart_label:{nm}", False


def value_columns(header: list[str]) -> dict[str, int]:
    """Classify current-period value columns: q = '당기 … 3개월', ytd = '당기 … 누적', cur = other '당기…' (first)."""
    out: dict[str, int] = {}
    for i, h in enumerate(header):
        h = h.strip()
        if not h.startswith("당기"):
            continue
        k = "q" if "3개월" in h else "ytd" if "누적" in h else "cur"
        out.setdefault(k, i)
    return out


def months_into_fy(end: date, fye_month: int) -> int:
    return ((end.month - fye_month) % 12) or 12


def bulk_kind(period: str) -> str | None:
    m = re.fullmatch(r"\d{4}_([1-4])Q", period or "")
    return BULK_KIND[int(m.group(1))] if m else None


def reprt_code(kind: str, months: int) -> str:
    """DART reprt_code of a report kind (분기: Q1 11013 / Q3 11014 by months into the fiscal year)."""
    if kind == "A001":
        return "11011"
    if kind == "A002":
        return "11012"
    return "11013" if months <= 3 else "11014"


_NEED = ("재무제표종류", "종목코드", "회사명", "결산월", "결산기준일", "통화", "항목코드", "항목명")


def iter_zip_rows(path: Path, codes: set[str] | None = None):
    """Yield dict rows (one per line item) from every TSV in a bulk zip, current-period columns only. Members are
    decoded as a stream (no full-text copy); with `codes` other companies' lines are skipped before splitting."""
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".txt"):
                continue
            with zf.open(info) as raw:
                fh = io.TextIOWrapper(raw, encoding="cp949", errors="replace", newline="\n")
                first = fh.readline()
                if not first:
                    continue
                header = [h.strip() for h in first.rstrip("\r\n").split("\t")]
                idx = {h: i for i, h in enumerate(header)}
                if any(k not in idx for k in _NEED):
                    continue
                vcols = value_columns(header)
                ci, n = idx["종목코드"], len(header)
                i_kind, i_name, i_fye, i_end = idx["재무제표종류"], idx["회사명"], idx["결산월"], idx["결산기준일"]
                i_ccy, i_item, i_label = idx["통화"], idx["항목코드"], idx["항목명"]
                i_mkt, i_ind = idx.get("시장구분"), idx.get("업종")
                for line in fh:
                    if codes is not None:
                        head = line.split("\t", ci + 1)
                        if len(head) <= ci or head[ci].strip().strip("[]").strip() not in codes:
                            continue
                    if not line.strip():
                        continue
                    r = line.rstrip("\r\n").split("\t")
                    if len(r) < n - 2:
                        continue
                    r += [""] * (n - len(r))
                    code = r[ci].strip().strip("[]").strip()
                    if not re.fullmatch(r"[0-9A-Z]{6}", code):
                        continue
                    kind = r[i_kind].strip()
                    yield {
                        "code": code, "name": r[i_name].strip(), "market": r[i_mkt].strip() if i_mkt is not None else "",
                        "industry": r[i_ind].strip() if i_ind is not None else "",
                        "fye": r[i_fye].strip(), "end": r[i_end].strip(), "ccy": r[i_ccy].strip() or "KRW",
                        "item": r[i_item].strip(), "label": r[i_label],
                        "consolidated": kind.endswith("연결") or "- 연결" in kind,
                        "vals": {k: r[i] for k, i in vcols.items()},
                    }


def zip_codes(path: Path) -> set[str]:
    """Every stock code present in one bulk zip (one streamed pass, no row dicts)."""
    out: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".txt"):
                continue
            with zf.open(info) as raw:
                fh = io.TextIOWrapper(raw, encoding="cp949", errors="replace", newline="\n")
                header = [h.strip() for h in fh.readline().rstrip("\r\n").split("\t")]
                if "종목코드" not in header:
                    continue
                ci = header.index("종목코드")
                for line in fh:
                    head = line.split("\t", ci + 1)
                    if len(head) > ci:
                        c = head[ci].strip().strip("[]").strip()
                        if re.fullmatch(r"[0-9A-Z]{6}", c):
                            out.add(c)
    return out


def parse_search_html(html: str) -> tuple[list[dict[str, Any]], int]:
    """detailSearch.ax result page -> (rows, total pages)."""
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        mc = re.search(r"openCorpInfoNew\('(\d{8})'[^>]*>\s*(.*?)\s*</a>", tr, re.S)
        mr = re.search(r"rcpNo=(\d{14})\"[^>]*>(.*?)</a>", tr, re.S)
        md = re.search(r"<td>\s*(\d{4})\.(\d{2})\.(\d{2})\s*</td>", tr)
        if not (mc and mr):
            continue
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", mr.group(2))).strip()
        mk = REPORT_RE.search(title)
        if not mk:
            continue
        rcp = mr.group(1)
        mm = re.search(r'class="tagCom_(\w+)"', tr)
        rows.append({"corp_code": mc.group(1), "corp_name": re.sub(r"<[^>]+>", "", mc.group(2)).strip(), "rcp_no": rcp,
                     "rcept_dt": (f"{md.group(1)}-{md.group(2)}-{md.group(3)}" if md else
                                  f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}"),
                     "report_nm": title, "kind": KIND_OF_REPORT[mk.group(1)], "ym": f"{mk.group(2)}.{mk.group(3)}",
                     "is_amendment": title.startswith("["), "market": mm.group(1) if mm else None})
    m = re.search(r"\[(\d+)/(\d+)\]\s*\[총", html)
    return rows, int(m.group(2)) if m else 1


def parse_api_list(j: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """OpenDART list.json page -> (periodic-report rows in the parse_search_html shape + stock_code, total pages).
    status 013 = no data. Withdrawn reports (rm contains 철) are skipped."""
    st = str(j.get("status", ""))
    if st == "013":
        return [], 0
    if st != "000":
        raise DartBulkError(f"OpenDART list.json status {st}: {str(j.get('message'))[:80]}")
    rows = []
    for x in j.get("list") or []:
        nm = " ".join((x.get("report_nm") or "").split())
        mk = REPORT_RE.search(nm)
        rno = str(x.get("rcept_no") or "")
        if not mk or len(rno) != 14 or "철" in (x.get("rm") or ""):
            continue
        d = str(x.get("rcept_dt") or rno[:8])
        rows.append({"corp_code": str(x.get("corp_code") or "").zfill(8), "corp_name": (x.get("corp_name") or "").strip(),
                     "stock_code": norm_code(x.get("stock_code")), "rcp_no": rno,
                     "rcept_dt": f"{d[:4]}-{d[4:6]}-{d[6:8]}", "report_nm": nm, "kind": KIND_OF_REPORT[mk.group(1)],
                     "ym": f"{mk.group(2)}.{mk.group(3)}", "is_amendment": nm.startswith("["),
                     "market": API_CLS_MARKET.get(str(x.get("corp_cls") or ""))})
    return rows, int(j.get("total_page") or 1)


def build_filing_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """(norm corp name, kind, 'YYYY.MM') -> earliest receipt (original filing) + latest rcpNo (name-only view)."""
    idx: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in sorted(rows, key=lambda x: x["rcp_no"]):
        k = (norm_name(r["corp_name"]), r["kind"], r["ym"])
        if k not in idx:
            idx[k] = {**r, "latest_rcp_no": r["rcp_no"], "n_versions": 1}
        else:
            idx[k]["latest_rcp_no"] = r["rcp_no"]
            idx[k]["n_versions"] += 1
    return idx


# ------------------------------------------------------------------------------------------------ dates
def next_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def add_business_days(d: date, n: int) -> date:
    for _ in range(n):
        d = next_weekday(d + timedelta(days=1))
    return d


def kr_statutory_deadline(end: date, months: int, kind: str | None = None) -> date:
    """Conservative FSCMA filing deadline of the periodic report to `end` (annual = 사업보고서: kind A001, else
    12 months into the fiscal year)."""
    if (kind == "A001") if kind else months == 12:
        return add_business_days(next_weekday(end + timedelta(days=KR_ANNUAL_DAYS)), KR_ANNUAL_EXT_BDAYS)
    return next_weekday(end + timedelta(days=KR_INTERIM_DAYS))


def statutory_knowledge_date(end: date, months: int, first_seen: date | None, kind: str | None = None) -> date:
    """Approximate knowledge date of an unmatched filing: the statutory deadline, capped at the zip date (the zip
    already carried the values then), never before the day after the period end."""
    kd = kr_statutory_deadline(end, months, kind)
    if first_seen is not None and kd > first_seen:
        kd = first_seen
    return max(kd, end + timedelta(days=1))


# ------------------------------------------------------------------------------------------------ crawl windows
def plan_windows(targets: Iterable[tuple[date, int | str]], markets: Iterable[str], today: date,
                 max_days: int) -> list[tuple[str, str, date, date]]:
    """(period end, report kind | months into the FY) targets -> [(report kind, market, start, end)] market-wide
    receipt-date windows (<= max_days)."""
    by_kind: dict[str, list[date]] = {}
    for end, k in targets:
        kind = k if isinstance(k, str) else (MONTHS[k][0] if k in MONTHS else None)
        if kind in WINDOW_LAG:
            by_kind.setdefault(kind, []).append(end)
    out = []
    for ptype in sorted(by_kind):
        ends = by_kind[ptype]
        s, e = min(ends) + timedelta(days=1), min(max(ends) + timedelta(days=WINDOW_LAG[ptype]), today)
        while s <= e:
            e2 = min(e, s + timedelta(days=max_days - 1))
            for ct in sorted(set(markets) - {"E"}):  # 기타법인 (unlisted filers) is huge and irrelevant
                out.append((ptype, ct, s, e2))
            s = e2 + timedelta(days=1)
    return out


def split_targets(entries: Iterable[tuple[str, str, str, date, str]],
                  rare_max: int = RARE_MAX) -> tuple[set[tuple[date, str]], list[tuple[str, str, str, date, str]]]:
    """(code, bulk name, market letter, period end, kind) per company-period -> (market-wide targets {(end, kind)},
    per-company lookups). The most common end of each kind, and any end shared by >= rare_max companies, is crawled
    market-wide; the rest (non-December fiscal years: a few dozen per period) would widen those windows to a year or
    more of unrelated filings, so they are looked up one company at a time (~2 KB answers)."""
    by: dict[tuple[date, str], list[tuple[str, str, str, date, str]]] = {}
    for e in entries:
        by.setdefault((e[3], e[4]), []).append(e)
    top: dict[str, tuple[date, str]] = {}
    for k, v in by.items():
        if k[1] not in top or len(v) > len(by[top[k[1]]]) or (len(v) == len(by[top[k[1]]]) and k > top[k[1]]):
            top[k[1]] = k
    main: set[tuple[date, str]] = set()
    rare: list[tuple[str, str, str, date, str]] = []
    for k, v in by.items():
        if k == top[k[1]] or len(v) >= rare_max:
            main.add(k)
        else:
            rare += v
    return main, sorted(rare)


def window_cover(start: date, end: date, cached: list[dict[str, Any]], today: date) -> tuple[list[tuple[date, date]], list[dict[str, Any]]]:
    """Gaps of [start, end] not covered by cached windows (same kind / market) + the cached rows inside the covered part.
    A cached window is final up to the day before its crawl (receipts are never back-dated); a window crawled
    today or yesterday is reused whole."""
    ivs: list[tuple[date, date]] = []
    rows: list[dict[str, Any]] = []
    for w in cached:
        ws, we, wc = to_date(w["start"]), to_date(w["end"]), to_date(w["crawled"])
        if ws is None or we is None or wc is None:
            continue
        fe = we if wc >= today - timedelta(days=1) else min(we, wc - timedelta(days=1))
        lo, hi = max(ws, start), min(fe, end)
        if lo > hi:
            continue
        ivs.append((lo, hi))
        for r in w.get("rows") or []:
            d = to_date(r.get("rcept_dt"))
            if d is not None and lo <= d <= hi:
                rows.append(r)
    gaps, cur = [], start
    for lo, hi in sorted(ivs):
        if lo > cur:
            gaps.append((cur, lo - timedelta(days=1)))
        cur = max(cur, hi + timedelta(days=1))
    if cur <= end:
        gaps.append((cur, end))
    return gaps, rows


# ------------------------------------------------------------------------------------------------ filing resolution
class FilingResolver:
    """Joins bulk (stock code, name, period end) to the receipt of its periodic report (see module docstring)."""

    def __init__(self, rows: Iterable[dict[str, Any]], *, first_seen: date | None,
                 crosswalk: dict[str, dict[str, Any]] | None = None, provider: str = PROVIDER_SEARCH,
                 amended_kd: str = "latest"):
        self.first_seen, self.provider, self.amended_kd = first_seen, provider, amended_kd
        self.crosswalk = crosswalk if crosswalk is not None else {}
        self.claimed: dict[str, str] = {}
        for sc, x in self.crosswalk.items():
            if x.get("dart_corp_code"):
                self.claimed.setdefault(x["dart_corp_code"], sc)
        self.by_corp: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.by_stock: dict[tuple[str, str, str], tuple[str, str, str]] = {}
        self.by_name: dict[tuple[str, str, str], set[str]] = {}
        seen: set[str] = set()
        for r in sorted(rows, key=lambda x: x["rcp_no"]):
            if r["rcp_no"] in seen:
                continue
            seen.add(r["rcp_no"])
            k = (r["corp_code"], r["kind"], r["ym"])
            e = self.by_corp.get(k)
            if e is None:
                e = self.by_corp[k] = {"corp_code": r["corp_code"], "corp_name": r["corp_name"], "kind": r["kind"],
                                       "ym": r["ym"], "versions": [], "market": r.get("market")}
            e["versions"].append((r["rcp_no"], to_date(r["rcept_dt"]), r.get("report_nm") or ""))
            e["corp_name"] = r["corp_name"] or e["corp_name"]
            if r.get("stock_code"):
                self.by_stock[(r["stock_code"], r["kind"], r["ym"])] = k
            self.by_name.setdefault((norm_name(r["corp_name"]), r["kind"], r["ym"]), set()).add(r["corp_code"])
        self.learned: dict[str, dict[str, Any]] = {}
        self.stats: dict[str, int] = {}

    def _count(self, k: str) -> None:
        self.stats[k] = self.stats.get(k, 0) + 1

    def _find(self, code: str, name: str, end: date, kind: str) -> tuple[dict[str, Any] | None, str | None, str]:
        ym = f"{end.year}.{end.month:02d}"
        k = self.by_stock.get((code, kind, ym))
        if k:
            return self.by_corp[k], "stock_code", ""
        corp = (self.crosswalk.get(code) or {}).get("dart_corp_code")
        if corp and (corp, kind, ym) in self.by_corp:
            return self.by_corp[(corp, kind, ym)], "corp_code", ""
        corps = self.by_name.get((norm_name(name), kind, ym)) or set()
        if len(corps) > 1:
            return None, None, "ambiguous_name"
        if corps:
            corp = next(iter(corps))
            owner = self.claimed.get(corp)
            if owner is not None and owner != code:
                return None, None, "name_hit_claimed_by_other_code"
            return self.by_corp[(corp, kind, ym)], "name", ""
        return None, None, "no_filing_found"

    def resolve(self, code: str, name: str, end: date, months: int, fy: int,
                kinds: Sequence[str | None] | None = None) -> dict[str, Any]:
        """kinds: report kinds to try in order (default: the months-into-FY kind); the first is assumed when
        nothing matches (statutory deadline of that kind)."""
        tried = [k for k in dict.fromkeys(list(kinds or []) + [MONTHS[months][0]]) if k]
        e, basis, reason, kind = None, None, "no_filing_found", tried[0]
        for kd_ in tried:
            e, basis, why = self._find(code, name, end, kd_)
            if e is not None:
                orig_d = e["versions"][0][1]
                if orig_d is None or orig_d <= end:
                    e, why = None, "receipt_not_after_period_end"
                elif self.first_seen is not None and orig_d > self.first_seen:
                    e, why = None, "receipt_after_zip_date"
            if e is not None:
                kind = kd_
                break
            if reason == "no_filing_found":
                reason = why
        if e is None:
            self._count(f"unmatched:{reason}")
            kd = statutory_knowledge_date(end, months, self.first_seen, kind)
            return {"kd_facts": kd, "kd_filing": kd, "native": f"{code}:{fy}{FILING_FP[months]}:{end:%Y%m%d}", "est": True,
                    "basis": "kr_statutory_deadline", "reason": reason, "entry": None, "corp_code": None,
                    "kind": kind}
        self._count(f"join:{basis}")
        if kind != tried[0]:
            self._count("join_kind_fallback")
        if basis == "name" and code not in self.crosswalk and code not in self.learned:
            self.learned[code] = {"dart_corp_code": e["corp_code"], "corp_name": e["corp_name"], "bulk_name": name,
                                  "market": e.get("market")}
            self.claimed.setdefault(e["corp_code"], code)
        versions = e["versions"]
        chosen = versions[0]
        if self.amended_kd == "latest":
            ok = [v for v in versions if v[1] is not None and (self.first_seen is None or v[1] <= self.first_seen)]
            chosen = ok[-1] if ok else versions[0]
        if chosen[0] != versions[0][0]:
            self._count("facts_kd_from_amendment")
        return {"kd_facts": chosen[1], "kd_filing": versions[0][1], "native": versions[0][0], "est": False,
                "basis": basis, "reason": None, "entry": e, "corp_code": e["corp_code"], "chosen_rcp_no": chosen[0],
                "kind": kind}


def normalize_period(zip_paths: dict[str, Path], *, codes: set[str] | None, resolver: FilingResolver, period: str,
                     source_id: str = "dart_bulk", separate: bool = True,
                     issuer_of: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Landed BS/PL/CF zips of one bulk period -> (fundamentals_raw rows, filings_index rows, stats). The filing's
    report kind is the bulk period's (BULK_KIND) when its receipt is found under it, else the months-into-FY kind;
    fiscal_period labels of the facts stay months-into-FY based."""
    pkind = bulk_kind(period)
    raw: list[tuple] = []  # (code, name, end, fye, consolidated, stmt, concept, label, label_mapped, ccy, kind, value, market)
    for stmt, path in zip_paths.items():
        if stmt not in ("BS", "PL", "CF"):
            continue
        for r in iter_zip_rows(path, codes):
            end = to_date(r["end"])
            try:
                fye = int(r["fye"])
            except ValueError:
                continue
            if end is None or not 1 <= fye <= 12:
                continue
            months = months_into_fy(end, fye)
            if months not in MONTHS:
                continue
            concept, mapped = concept_for(r["item"], r["label"])
            label = re.sub(r"\s+", " ", r["label"].strip()) or None
            v = r["vals"]
            if stmt == "BS":
                val = parse_num(v.get("cur"))
                if val is not None:
                    raw.append((r["code"], r["name"], end, fye, r["consolidated"], "BS", concept, label, mapped, r["ccy"],
                                "i", val, r["market"]))
                continue
            ytd = parse_num(v["ytd"]) if "ytd" in v else parse_num(v.get("cur"))
            if ytd is not None:
                raw.append((r["code"], r["name"], end, fye, r["consolidated"], stmt, concept, label, mapped, r["ccy"],
                            "y", ytd, r["market"]))
            q = parse_num(v.get("q"))
            if q is not None and months != 3 and stmt == "PL":
                raw.append((r["code"], r["name"], end, fye, r["consolidated"], stmt, concept, label, mapped, r["ccy"],
                            "q", q, r["market"]))
    # label-mapped rows must not shadow a standard-id row of the same fact
    std = {(x[0], x[2], x[4], x[6], x[10]) for x in raw if not x[8]}
    raw = [x for x in raw if not x[8] or (x[0], x[2], x[4], x[6], x[10]) not in std]
    has_cons = {(x[0], x[2]) for x in raw if x[4]}
    issuer_of = issuer_of or {}
    facts: list[dict[str, Any]] = []
    filings: dict[tuple[str, date], dict[str, Any]] = {}
    for x in raw:
        code, name, end, fye, cons, stmt, concept, label, _m, ccy, kind, val, market = x
        if not cons and (code, end) in has_cons and not separate:
            continue
        months = months_into_fy(end, fye)
        rkind, _reprt, ytd_fp, q_fp = MONTHS[months]
        fy = fiscal_year_for(end, fye)
        fk = (code, end)
        if fk not in filings:
            ref = resolver.resolve(code, name, end, months, fy, kinds=[pkind, rkind])
            filings[fk] = {**ref, "reprt": reprt_code(ref["kind"], months), "fy": fy, "fp": FILING_FP[months],
                           "end": end, "code": code, "market": MARKET_CODE.get(market)}
        f = filings[fk]
        base = dict(source_id=source_id, source_entity_id=code, filing_id=f"{source_id}:{f['native']}", form_type=f["reprt"],
                    taxonomy="dart", concept=concept, label=label, unit=ccy or "KRW", knowledge_date=f["kd_facts"],
                    fiscal_year=fy, consolidated=cons, knowledge_date_estimated=f["est"],
                    dims=None if cons or (code, end) not in has_cons else SEP_DIMS)
        if kind == "i":
            row = fact_row(**base, period_start=None, period_end=end, value=val, fiscal_period=None)
        elif kind == "y":
            row = fact_row(**base, period_start=period_start_for(end, months), period_end=end, value=val,
                           fiscal_period=ytd_fp)
        else:
            row = fact_row(**base, period_start=period_start_for(end, 3), period_end=end, value=val, fiscal_period=q_fp)
        row["issuer_id"] = issuer_of.get(code)
        facts.append(row)
    del raw
    values_as_of = resolver.first_seen.isoformat() if resolver.first_seen else None
    fil_rows = []
    for f in filings.values():
        e = f["entry"]
        items = [f"bulk_period={period}", "accounting_standard=K-IFRS"]
        if values_as_of:
            items.append(f"values_as_of={values_as_of}")
        if f["est"]:
            items += ["knowledge_date_estimated=true", f"knowledge_date_basis={f['basis']}",
                      f"unmatched_reason={f['reason']}"]
        else:
            items += [f"report_nm={e['versions'][0][2]}", f"corp_code={e['corp_code']}",
                      f"latest_rcp_no={e['versions'][-1][0]}", f"n_versions={len(e['versions'])}",
                      f"kd_provider={resolver.provider}", f"join={f['basis']}"]
            if f["kd_facts"] != f["kd_filing"]:
                items.append(f"facts_knowledge_date={f['kd_facts'].isoformat()}")
                items.append(f"facts_rcp_no={f['chosen_rcp_no']}")
        row = filing_row(
            source_id=source_id, native_key=f["native"], source_entity_id=f["code"], form_type=f["reprt"],
            form_class=FORM_CLASS[f["kind"]], knowledge_date=f["kd_filing"], period_end=f["end"], fiscal_year=f["fy"],
            fiscal_period=f["fp"], is_amendment=False, language="ko", has_xbrl=False, items=items,
            url=None if f["est"] else f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={f['native']}")
        row["issuer_id"] = issuer_of.get(f["code"])
        fil_rows.append(row)
    stats = {"companies": len({f["code"] for f in filings.values()}), "filings": len(filings),
             "filing_dates_found": sum(1 for f in filings.values() if not f["est"])}
    stats["imputed"] = stats["filings"] - stats["filing_dates_found"]
    return facts, fil_rows, stats


def replace_rows(df: pl.DataFrame, table: str, keys: pl.DataFrame, candidates: Iterable[tuple[Any, ...]],
                 settings: Settings) -> int:
    """Write `df` to `table`, first dropping every stored row whose key columns (`keys`, e.g. source_entity_id +
    period_end) match, in the partitions `df` touches plus `candidates` (partition-value tuples that may hold stale
    rows). The bulk zip is the full truth for one company-period, so a changed filing id, a removed line or a moved
    fiscal year leaves nothing behind. Same writer lock and atomic partition swap as app.core.storage.write_table
    (and app.normalize.identity._rewrite_partitions)."""
    from app.core import storage as stg
    from app.core.schema import conform, empty_frame, get_contract

    c = get_contract(table)
    if list(df.schema.items()) != list(empty_frame(c).schema.items()):
        df = conform(df, c)  # frames built by frame() are already conformed + validated: no second copy
    root = stg.table_dir(c, settings)
    pcols = list(c.partition_by)
    sep = "\x1f"
    key_of = pl.concat_str([pl.col(k).cast(pl.Utf8).fill_null("") for k in keys.columns], separator=sep)
    keyset = keys.unique().select(key_of.alias("k")).get_column("k").to_list()
    # a stored row whose primary key equals a new row's is superseded too, whatever its keys (filings_index: PK
    # filing_id only, partitioned by knowledge year -> the same filing may sit in another year partition)
    pk_cols = [k for k in c.primary_key if k not in pcols]
    pk_of = pl.concat_str([pl.col(k).cast(pl.Utf8).fill_null("") for k in pk_cols], separator=sep) if pk_cols else None
    pkset = (df.select(pk_of.alias("k")).get_column("k").unique().to_list()
             if pk_of is not None and not set(keys.columns) <= set(pk_cols) and df.height else [])
    # one physical schema for every file of a rewritten partition (readers scan the files of a partition together)
    target = stg.physical_frame(empty_frame(c), c).drop(pcols).schema
    groups = {tuple(k): v for k, v in df.partition_by(pcols, as_dict=True, maintain_order=True).items()} if df.height else {}
    parts = set(groups) | {tuple(x) for x in candidates}
    with stg.table_lock(table, settings):
        for key in sorted(parts, key=lambda t: tuple(str(v) for v in t)):
            pdir = stg.partition_dir(root, pcols, key)
            new = groups.get(key)
            files = sorted(str(f) for f in pdir.glob("*.parquet")) if pdir.exists() else []
            if not files and new is None:
                continue
            kept = None
            if files:
                lfs = []
                for f in files:  # per file: an older file may lack a column or carry another dtype
                    lf = pl.scan_parquet(f)
                    have = lf.collect_schema()
                    lfs.append(lf.select([pl.col(n).cast(t) if n in have else pl.lit(None, t).alias(n)
                                          for n, t in target.items()]))
                lf = pl.concat(lfs, how="vertical") if len(lfs) > 1 else lfs[0]
                hit = key_of.is_in(pl.Series(keyset, dtype=pl.Utf8).implode())
                if pkset:
                    hit = hit | pk_of.is_in(pl.Series(pkset, dtype=pl.Utf8).implode())
                if new is None and not lf.filter(hit).select(pl.len()).collect().item():
                    continue  # nothing stale here
                kept = lf.filter(~hit)
            tmp = pdir.parent / f".tmp-{pdir.name}-{uuid.uuid4().hex[:8]}"
            tmp.mkdir(parents=True, exist_ok=True)
            if kept is not None:  # streamed: the partition is never materialized in memory
                kept.sink_parquet(tmp / f"part-{uuid.uuid4().hex[:12]}.parquet", compression="zstd",
                                  compression_level=c.compression_level, statistics=True)
            if new is not None and new.height:
                stg._write_partition_files(tmp, new.drop(pcols), c)
            stg._swap_in(tmp, pdir)
    return df.height


def _bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def _tri(v: Any) -> bool | None:
    if v is None or str(v).strip().lower() in ("", "none", "null", "auto"):
        return None
    return _bool(v)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@register_adapter
class DartBulkAdapter(SourceAdapter):
    source_id = "dart_bulk"
    regions = ("KR",)
    produces = ("filings_index", "fundamentals_raw")
    base_url = "https://opendart.fss.or.kr"
    rate_limit = RateLimit(1.0, 2)
    cache_ttl = 6 * 3600

    def __init__(self, settings: Settings | None = None, http: HttpClient | None = None, *,
                 all_listed: Any = None, statements: str = "BS,PL,CF", periods: str | Sequence[str] | None = None,
                 max_periods: Any = None, workers: Any = 2, separate: Any = True, filing_dates: Any = True,
                 renormalize: Any = False, offline: Any = False, chunk_codes: Any = 800, amended_kd: str = "latest",
                 min_free_gb: Any = 1.5, ram_budget_gb: Any = 4.0, **_: Any):
        settings = settings or get_settings()
        http = http or HttpClient(self.source_id, base_url=self.base_url, headers=BROWSER_HEADERS,
                                  rate_limit=self.rate_limit, settings=settings, timeout=120)
        super().__init__(settings=settings, http=http)
        self.all_listed_param = _tri(all_listed)
        self.all_listed = bool(self.all_listed_param)
        self.statements = [s.strip().upper() for s in str(statements).split(",") if s.strip()]
        self.periods = ([p.strip() for p in periods.split(",") if p.strip()] if isinstance(periods, str)
                        else list(periods or []))
        self.max_periods_param = int(max_periods) if max_periods not in (None, "", "None") else None
        self.workers = max(1, min(2, int(workers)))
        self.separate = _bool(separate)
        fd = str(filing_dates).strip().lower()
        self.filing_dates: str | bool = "cache" if fd == "cache" else _bool(filing_dates)
        self.renormalize = _bool(renormalize)
        self.offline = _bool(offline)
        self.chunk_codes = max(50, int(chunk_codes))
        self.amended_kd = "original" if str(amended_kd).strip().lower() == "original" else "latest"
        self.min_free_gb = float(min_free_gb)
        self.ram_budget_gb = float(ram_budget_gb)
        self._dl_client: httpx.Client | None = None
        self._search_http: HttpClient | None = None
        self._api_http: HttpClient | None = None
        self._xw: dict[str, dict[str, Any]] | None = None
        self._ref: dict[str, Any] | None = None
        self.last_uncrawled = 0
        self._mem_base: float | None = None

    @property
    def max_periods(self) -> int | None:
        mp = self.max_periods_param
        return mp if mp is not None else (4 if self.all_listed else None)

    # ------------------------------------------------------------------ paths / state
    @property
    def root(self) -> Path:
        p = self.settings.raw_dir / self.source_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def zips_dir(self) -> Path:
        p = self.root / "zips"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def landed(self, name: str) -> Path | None:
        p = self.zips_dir / name
        return p if p.exists() and p.stat().st_size > 0 else None

    def sidecar_path(self, period: str) -> Path:
        return self.root / "normalized" / f"{period}.json"

    def load_sidecar(self, period: str) -> dict[str, Any] | None:
        p = self.sidecar_path(period)
        try:
            return orjson.loads(p.read_bytes()) if p.exists() else None
        except (OSError, ValueError):
            return None

    def save_sidecar(self, period: str, obj: dict[str, Any]) -> None:
        p = self.sidecar_path(period)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(orjson.dumps(obj))
        tmp.replace(p)

    def api_key_present(self) -> bool:
        """Presence of OPENDART_APIKEY only (the value is never logged, stored or returned)."""
        return self.settings.secret("opendart_apikey") is not None

    # ------------------------------------------------------------------ http
    def list_files(self) -> list[dict[str, Any]]:
        r = self.http.post(LIST_URL, data={})
        files = parse_list_html(r.text)
        if not files:
            raise DartBulkError("bulk list.do returned no zip names")
        return files

    def _client(self) -> httpx.Client:
        if self._dl_client is None:
            self._dl_client = httpx.Client(headers=BROWSER_HEADERS, follow_redirects=True,
                                           timeout=httpx.Timeout(120.0, read=300.0))
        return self._dl_client

    def download(self, name: str, attempts: int = 4) -> Path:
        """Stream one bulk zip to zips/<name> (atomic, verified). ~10 KB/s server throttle -> minutes per file."""
        dest = self.zips_dir / name
        if self.landed(name):
            return dest
        tmp = dest.with_name(dest.name + ".part")
        last: Exception | None = None
        for attempt in range(attempts):
            self.http.limiter.acquire()
            self.http.n_requests += 1
            t0 = time.time()
            try:
                with self._client().stream("GET", DL_URL, params={"fl_nm": name}) as r:
                    ctype = r.headers.get("content-type", "")
                    if r.status_code != 200 or not ctype.startswith("application"):
                        raise DartBulkError(f"{name}: HTTP {r.status_code} content-type {ctype!r}")
                    expected = int(r.headers.get("content-length") or 0)
                    with open(tmp, "wb") as fh:
                        for chunk in r.iter_bytes(1 << 16):
                            fh.write(chunk)
                got = tmp.stat().st_size
                if expected and got != expected:
                    raise DartBulkError(f"{name}: short download {got}/{expected}")
                with zipfile.ZipFile(tmp) as zf:
                    bad = zf.testzip()
                    if bad:
                        raise DartBulkError(f"{name}: corrupt member {bad}")
                tmp.replace(dest)
                self.log.info("dart_bulk landed %s (%d bytes, %.0fs)", name, got, time.time() - t0)
                info = parse_zip_name(name)
                if info:  # drop superseded versions of the same period/statement
                    for old in self.zips_dir.glob(f"{info['period']}_{info['stmt']}_*.zip"):
                        if old.name != name:
                            old.unlink(missing_ok=True)
                return dest
            except (httpx.HTTPError, zipfile.BadZipFile, DartBulkError, OSError) as e:
                last = e
                wait = min(300, 30 * 2 ** attempt)
                self.log.warning("dart_bulk download %s failed (%s); retry %d in %ds", name, e, attempt + 1, wait)
                time.sleep(wait)
        tmp.unlink(missing_ok=True)
        raise DartBulkError(f"{name}: download failed: {last}")

    def _search(self) -> HttpClient:
        """detailSearch client: the project User-Agent (app.core.http default headers), same 1 req/s bucket."""
        if self._search_http is None:
            self._search_http = HttpClient(self.source_id, base_url="https://dart.fss.or.kr", rate_limit=self.rate_limit,
                                           settings=self.settings, timeout=60, cache=False)
        return self._search_http

    def search_page(self, *, ptype: str, ctype: str, start: date, end: date, page: int,
                    company: str | None = None) -> str:
        """One detailSearch page. company 'cik:<corp_code>' / 'nm:<name>' filters one company (ptype 'A' = every
        periodic report kind, ctype '' = every market)."""
        kinds = ["A001", "A002", "A003"] if ptype == ALL_KINDS else [ptype]
        cik = company[4:] if company and company.startswith("cik:") else ""
        nm = company[3:] if company and company.startswith("nm:") else ""
        data = {"currentPage": page, "maxResults": 100, "maxLinks": 10, "sort": "date", "series": "asc",
                "startDate": start.strftime("%Y%m%d"), "endDate": end.strftime("%Y%m%d"), "publicType": kinds,
                "corporationType": ctype, "textCrpNm": nm, "textCrpCik": cik}
        r = self._search().post(SEARCH_URL, data=data, headers={"Referer": SEARCH_REFERER,
                                                                "X-Requested-With": "XMLHttpRequest"})
        return r.text

    def crawl_filings(self, *, ptype: str, ctype: str, start: date, end: date, max_pages: int = 300,
                      company: str | None = None) -> list[dict[str, Any]]:
        rows, page, pages = [], 1, 1
        kw = {"company": company} if company else {}
        while page <= min(pages, max_pages):
            got, pages = parse_search_html(self.search_page(ptype=ptype, ctype=ctype, start=start, end=end, page=page,
                                                            **kw))
            rows += got
            if not got:
                break
            page += 1
        return rows

    def _api(self) -> HttpClient:
        if self._api_http is None:  # shared 'opendart' bucket (3 req/s) and project User-Agent
            self._api_http = HttpClient("opendart", base_url=API_BASE, settings=self.settings, timeout=60, cache=False)
        return self._api_http

    def api_page(self, *, ptype: str, ctype: str, start: date, end: date, page: int,
                 company: str | None = None) -> dict[str, Any]:
        """One list.json page. Counts against the shared OpenDART daily cap; never logs the key. company
        'cik:<corp_code>' = one company (no 3-month window limit then), ptype 'A' = every periodic report kind."""
        from app.core.ratelimit import get_daily_quota

        q = get_daily_quota("opendart")
        if q is not None and not q.try_consume(1):
            raise DartQuotaExceeded("OpenDART daily quota reached")
        key = self.settings.secret("opendart_apikey")
        if not key:
            raise DartBulkError("OPENDART_APIKEY not set")
        params: dict[str, Any] = {"crtfc_key": key, "bgn_de": start.strftime("%Y%m%d"),
                                  "end_de": end.strftime("%Y%m%d"), "pblntf_ty": "A", "last_reprt_at": "N",
                                  "sort": "date", "sort_mth": "asc", "page_no": page, "page_count": 100}
        if ptype != ALL_KINDS:
            params["pblntf_detail_ty"] = ptype
        if company and company.startswith("cik:"):
            params["corp_code"] = company[4:]
        elif ctype:
            params["corp_cls"] = API_CORP_CLS.get(ctype, ctype)
        r = self._api().get("list.json", params=params, cache_ttl=0)
        return r.json()

    def api_crawl(self, *, ptype: str, ctype: str, start: date, end: date, max_pages: int = 300,
                  company: str | None = None) -> list[dict[str, Any]]:
        rows, page, pages = [], 1, 1
        kw = {"company": company} if company else {}
        while page <= min(pages, max_pages):
            got, pages = parse_api_list(self.api_page(ptype=ptype, ctype=ctype, start=start, end=end, page=page, **kw))
            rows += got
            if not got:
                break
            page += 1
        return rows

    def filing_rows_for_period(self, period: str, targets: set[tuple[date, int | str]], markets: set[str], *,
                               rare: Sequence[tuple[str, str, str, date, str]] = (), provider: str = PROVIDER_SEARCH,
                               crawl: bool = True) -> list[dict[str, Any]]:
        """Receipt rows covering one bulk period: market-wide windows for `targets` + one lookup per `rare` company
        (split_targets), from the cached windows plus a crawl of the uncovered gaps (cached under
        raw/dart_bulk/filings[_api]/<period>.json.gz, saved after every window so an interrupted crawl resumes)."""
        today = datetime.now(UTC).date()
        api = provider == PROVIDER_API
        cache_p = self.root / ("filings_api" if api else "filings") / f"{period}.json.gz"
        cached = self.load_raw_json(cache_p) if cache_p.exists() else None
        windows: list[dict[str, Any]] = list((cached or {}).get("windows", []))
        fetch: Callable[..., list[dict[str, Any]]] = self.api_crawl if api else self.crawl_filings
        rows: list[dict[str, Any]] = []
        dirty = False
        self.last_uncrawled = 0
        plan: list[tuple[str, str, date, date, str | None]] = [
            (*w, None) for w in plan_windows(targets, markets, today, API_MAX_DAYS if api else SEARCH_MAX_DAYS)]
        xw = self.crosswalk() if rare else {}
        for code, name, mk, end, kind in rare:
            s, e = end + timedelta(days=1), min(end + timedelta(days=WINDOW_LAG.get(kind, 150)), today)
            if s > e:
                continue
            corp = (xw.get(code) or {}).get("dart_corp_code")
            if api and not corp:
                continue  # list.json filters one company by corp_code only (corpCode.xml fills the crosswalk)
            mw = [w for w in windows if w["ptype"] == kind and w["ctype"] == mk and not w.get("company")]
            gaps, got_rows = window_cover(s, e, mw, today) if mw else ([(s, e)], [])
            if not gaps:  # already inside a market-wide window of its market
                rows += got_rows
                continue
            plan.append((ALL_KINDS, "", s, e, f"cik:{corp}" if corp else f"nm:{name}"))
        for ptype, ctype, s, e, company in dict.fromkeys(plan):
            same = [w for w in windows if w["ptype"] == ptype and w["ctype"] == ctype and w.get("company") == company]
            gaps, got_rows = window_cover(s, e, same, today)
            rows += got_rows
            for gs, ge in gaps:
                if not crawl:
                    self.last_uncrawled += 1
                    continue
                got = fetch(ptype=ptype, ctype=ctype, start=gs, end=ge, **({"company": company} if company else {}))
                w = {"ptype": ptype, "ctype": ctype, "start": gs.isoformat(), "end": ge.isoformat(),
                     "crawled": today.isoformat(), "rows": got}
                if company:
                    w["company"] = company
                windows.append(w)
                rows += got
                dirty = True
                cache_p.parent.mkdir(parents=True, exist_ok=True)
                self.save_raw_to(cache_p, {"period": period, "provider": provider, "windows": windows})
        if not dirty and cached is None:
            cache_p.parent.mkdir(parents=True, exist_ok=True)
            self.save_raw_to(cache_p, {"period": period, "provider": provider, "windows": windows})
        return rows

    def save_raw_to(self, path: Path, obj: Any) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(gzip.compress(orjson.dumps(obj), compresslevel=5))
        tmp.replace(path)

    # ------------------------------------------------------------------ reference / crosswalk
    def reference(self) -> dict[str, Any]:
        """KR rows of identifier_map: listed stock codes -> (issuer_id, security_id); dart_corp_code by issuer."""
        if self._ref is not None:
            return self._ref
        ref: dict[str, Any] = {"codes": set(), "issuer": {}, "security": {}, "corp_by_issuer": {}}
        try:
            from app.core.storage import scan_table

            im = (scan_table("identifier_map", self.settings)
                  .filter(pl.col("id_type").is_in(["kr_stock_code", "dart_corp_code"]) & pl.col("valid_to").is_null()
                          & (pl.col("confidence").fill_null(1.0) >= 0.9))
                  .select("id_type", "id_value", "issuer_id", "security_id").collect())
            for r in im.iter_rows(named=True):
                if r["id_type"] == "kr_stock_code":
                    c = norm_code(r["id_value"])
                    if c:
                        ref["codes"].add(c)
                        if r["issuer_id"]:
                            ref["issuer"][c] = r["issuer_id"]
                        if r["security_id"]:
                            ref["security"][c] = r["security_id"]
                elif r["issuer_id"] and r["id_value"]:
                    ref["corp_by_issuer"][r["issuer_id"]] = str(r["id_value"]).zfill(8)
        except Exception as e:  # noqa: BLE001 - no reference yet (fresh install / tests): seed / zip scope
            self.log.info("dart_bulk: identifier_map unavailable (%s)", type(e).__name__)
        self._ref = ref
        return ref

    def crosswalk_path(self) -> Path:
        return self.root / "crosswalk.parquet"

    def crosswalk(self) -> dict[str, dict[str, Any]]:
        if self._xw is not None:
            return self._xw
        xw: dict[str, dict[str, Any]] = {}
        p = self.crosswalk_path()
        if p.exists():
            try:
                for r in pl.read_parquet(p).iter_rows(named=True):
                    xw[r["kr_stock_code"]] = r
            except Exception as e:  # noqa: BLE001
                self.log.warning("dart_bulk crosswalk unreadable (%s): rebuilt", e)
        ref = self.reference()
        for code, iid in ref["issuer"].items():
            corp = ref["corp_by_issuer"].get(iid)
            if corp:
                self._xw_put(xw, code, {"dart_corp_code": corp}, "identifier_map", 1.0)
        if self.api_key_present():
            try:
                from app.normalize.identity_universe import fetch_dart_corpcodes

                cc = fetch_dart_corpcodes(self.settings)
                for r in (cc.iter_rows(named=True) if cc is not None and cc.height else []):
                    code = norm_code(r.get("kr_stock_code"))
                    if code and r.get("dart_corp_code"):
                        self._xw_put(xw, code, {"dart_corp_code": r["dart_corp_code"], "corp_name": r.get("corp_name")},
                                     "opendart_corpcode", 1.0)
            except Exception as e:  # noqa: BLE001
                self.log.warning("dart_bulk: corpCode.xml unavailable (%s)", type(e).__name__)
        for code, x in xw.items():
            x["issuer_id"] = ref["issuer"].get(code) or x.get("issuer_id")
            x["security_id"] = ref["security"].get(code) or x.get("security_id")
        self._xw = xw
        return xw

    @staticmethod
    def _xw_put(xw: dict[str, dict[str, Any]], code: str, vals: dict[str, Any], src: str, conf: float,
                period: str | None = None) -> bool:
        cur = xw.get(code)
        if cur is not None and XW_RANK.get(cur.get("src") or "", 0) > XW_RANK[src]:
            return False
        if cur is not None and cur.get("src") == src and cur.get("dart_corp_code") == vals.get("dart_corp_code"):
            return False
        row = {k: None for k in XW_SCHEMA} | (cur or {}) | {k: v for k, v in vals.items() if v is not None}
        row.update(kr_stock_code=code, src=src, confidence=conf, updated_at=_now_iso())
        if period:
            row["learned_period"] = period
        xw[code] = row
        return True

    def save_crosswalk(self) -> None:
        if self._xw is None:
            return
        rows = [{k: r.get(k) for k in XW_SCHEMA} for r in self._xw.values()]
        df = pl.DataFrame(rows, schema=XW_SCHEMA) if rows else pl.DataFrame(schema=XW_SCHEMA)
        p = self.crosswalk_path()
        tmp = p.with_name(p.name + ".tmp")
        df.sort("kr_stock_code").write_parquet(tmp)
        tmp.replace(p)

    # ------------------------------------------------------------------ scope
    def resolve_all_listed(self, state: dict[str, Any]) -> bool:
        if self.all_listed_param is not None:
            return self.all_listed_param
        return state.get("scope_mode") == "all_listed"

    def scope_codes(self, entities: Sequence[str] | None) -> set[str] | None:
        """Stock codes in scope; None = every code in the zips (all_listed without a securities master)."""
        if entities:
            return {c for c in (norm_code(e) for e in entities) if c}
        if self.all_listed:
            codes = set(self.reference()["codes"])
            if not codes:
                return None
            return codes | self.seed_codes()
        return self.seed_codes()

    def seed_codes(self) -> set[str]:
        wanted = seed_universe(self.settings, countries=["KR"]).get_column("ticker").to_list()
        return {c for c in (norm_code(e) for e in wanted) if c}

    @staticmethod
    def scope_hash(codes: set[str] | None) -> str:
        return "all" if codes is None else hashlib.sha1(",".join(sorted(codes)).encode()).hexdigest()[:12]

    def covered_codes(self, period: str, names: list[str], state: dict[str, Any], token: str | None,
                      key_present: bool) -> set[str] | str:
        """Stock codes of `period` already normalized from this zip set ('*' = every code)."""
        side = self.load_sidecar(period)
        upgrade = key_present and self.filing_dates is True
        if side is not None:
            if side.get("files") != names:
                return set()
            if token and (side.get("at") or "") < token:
                return set()
            if upgrade and side.get("provider") != PROVIDER_API:
                return set()
            if self.filing_dates is True and side.get("dates_complete") is False:
                return set()  # cache-only / failed crawl last time: the receipt dates can now be completed
            cov = side.get("codes")
            return "*" if cov == "*" else set(cov or [])
        legacy = (state.get("normalized") or {}).get(period) or {}
        if legacy.get("files") != names or (token and (legacy.get("at") or "") < token) or upgrade:
            return set()
        sc = legacy.get("scope")
        if sc == "all":
            return "*"
        seed = self.seed_codes()
        return seed if sc == self.scope_hash(seed) else set()

    @staticmethod
    def _private_gb() -> float | None:
        try:
            from app.ops import winsys

            d = winsys.process_memory_detail(os.getpid()) or {}
            v = d.get("private_mb") or d.get("rss_mb")
            return None if v is None else v / 1024
        except Exception:  # noqa: BLE001
            return None

    def _ram_ok(self) -> tuple[bool, str | None]:
        """Checked before every period: machine free RAM floor and this run's own growth (baseline = run start,
        so an in-process run inside a large worker is not penalised for the worker's memory)."""
        try:
            from app.ops import winsys

            m = winsys.memory_status() or {}
            if m.get("avail_gb") is not None and m.get("total_gb") and m["avail_gb"] < self.min_free_gb:
                return False, f"free RAM {m['avail_gb']:.1f} GB < min_free_gb {self.min_free_gb}"
            used = self._private_gb()
            base = getattr(self, "_mem_base", None)
            if used is not None and base is not None and used - base > self.ram_budget_gb:
                return False, f"run grew {used - base:.1f} GB > ram_budget_gb {self.ram_budget_gb}"
        except Exception:  # noqa: BLE001 - unknown memory never blocks
            pass
        return True, None

    # ------------------------------------------------------------------ interface
    def discover(self) -> pl.DataFrame:
        zips = sorted(self.zips_dir.glob("*_PL_*.zip"), key=lambda p: p.name, reverse=True)
        rows: dict[str, dict[str, Any]] = {}
        xw = self.crosswalk()
        if zips:
            for r in iter_zip_rows(zips[0]):
                if r["code"] not in rows:
                    rows[r["code"]] = {"source_entity_id": r["code"], "kr_stock_code": r["code"], "ticker": r["code"],
                                       "name": r["name"], "name_local": r["name"], "country": "KR",
                                       "dart_corp_code": (xw.get(r["code"]) or {}).get("dart_corp_code"),
                                       "industry_code": r["industry"] or None, "industry_scheme": "ksic",
                                       "mic": MARKET_MIC.get(MARKET_CODE.get(r["market"], "")), "is_active": True}
        return self.discover_frame(list(rows.values()), self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        state.setdefault("done", {})
        state.setdefault("normalized", {})
        self.all_listed = self.resolve_all_listed(state) if not entities else bool(self.all_listed_param)
        if self.renormalize and not state.get("renorm_since"):
            state["renorm_since"] = _now_iso()
            save_state(self.source_id, {**load_state(self.source_id, self.settings),
                                        "renorm_since": state["renorm_since"]}, self.settings)
        token = state.get("renorm_since")
        files = None if self.offline else safe_call(self.list_files, res, "list.do", self.log)
        if files is None:  # offline (param or list.do failure): whatever is landed
            files = [f for f in (parse_zip_name(p.name) for p in self.zips_dir.glob("*.zip")) if f]
        files = [f for f in files if f["stmt"] in self.statements]
        by_period: dict[str, dict[str, dict[str, Any]]] = {}
        for f in files:
            by_period.setdefault(f["period"], {})[f["stmt"]] = f
        since_d = to_date(since)
        periods = sorted(by_period, key=lambda p: by_period[p][next(iter(by_period[p]))]["nominal_end"], reverse=True)
        if self.periods:
            periods = [p for p in periods if p in self.periods]
        key_present = self.api_key_present()
        if not self.periods and since_d:
            recent = {p for p in periods
                      if next(iter(by_period[p].values()))["nominal_end"] >= since_d - timedelta(days=450)}
            if key_present and self.filing_dates is True:
                # the key upgrades history too: periods normalized without the API stay eligible whatever the
                # watermark (newest first, max_periods per run)
                recent |= {p for p in periods if p not in recent
                           and (self.load_sidecar(p) or {}).get("provider", PROVIDER_API) != PROVIDER_API}
            periods = [p for p in periods if p in recent]
        codes = self.scope_codes(entities)
        sh = self.scope_hash(codes)
        work: list[tuple[str, list[str] | None]] = []
        unlanded = []
        for p in periods:
            names = sorted(f["name"] for f in by_period[p].values())
            missing = [n for n in names if not self.landed(n)]
            if missing and self.offline:
                unlanded.append(p)
                continue
            cov = self.covered_codes(p, names, state, token, key_present)
            if cov == "*":
                todo: list[str] | None = []
            elif codes is None:
                todo = None  # every code of the zips minus the covered ones (resolved after landing)
            else:
                todo = sorted(codes - cov)
            if missing or todo is None or todo:
                work.append((p, todo))
        remaining = len(work)
        if self.max_periods:
            work = work[:self.max_periods]
        res.meta.update(periods=[], scope_hash=sh, remaining_periods=remaining - len(work), all_listed=self.all_listed,
                        kd_provider=PROVIDER_API if key_present and self.filing_dates is True else
                        PROVIDER_SEARCH if self.filing_dates else PROVIDER_NONE, unlanded_periods=unlanded)
        for p, todo in work:
            pending = [f["name"] for f in by_period[p].values() if not self.landed(f["name"])]
            if pending and not self.offline:
                with ThreadPoolExecutor(self.workers) as ex:
                    list(ex.map(lambda n: safe_call(lambda: self.download(n), res, n, self.log), pending))
            landed = {s: self.landed(f["name"]) for s, f in by_period[p].items()}
            for s, path in landed.items():
                if path:
                    state["done"][by_period[p][s]["name"]] = {"bytes": path.stat().st_size}
            save_state(self.source_id, {**load_state(self.source_id, self.settings), "done": state["done"]}, self.settings)
            if not all(landed.values()):
                res.entities_failed.setdefault(p, "incomplete statement set")
                continue
            names = sorted(f["name"] for f in by_period[p].values())
            if todo == [] and pending:  # zips replaced underneath a covered entry: everything is due again
                todo = None if codes is None else sorted(codes)
            first_seen = max(f["first_seen"] for f in by_period[p].values())
            res.meta["periods"].append({"period": p, "files": {s: str(x) for s, x in landed.items()}, "names": names,
                                        "first_seen": first_seen.isoformat(), "todo": todo})
            res.entities_ok.append(p)
        if res.meta["remaining_periods"] == 0 and not res.entities_failed:
            res.watermark = datetime.now(UTC).date().isoformat()
        res.meta["entities"] = list(entities) if entities else None
        res.meta["renorm_since"] = token
        res.meta["all_periods"] = {p: sorted(f["name"] for f in by_period[p].values()) for p in by_period}
        return res

    # ------------------------------------------------------------------ normalize
    def _provider_rows(self, p: dict[str, Any], targets: set[tuple[date, int | str]], markets: set[str],
                       fetched: FetchResult, rare: Sequence[tuple[str, str, str, date, str]] = ()
                       ) -> tuple[list[dict[str, Any]], str, bool]:
        """-> (receipt rows, provider, complete). complete=False (cache-only run, skipped windows or a failed crawl)
        makes the next crawling run redo the period."""
        if not self.filing_dates:
            return [], PROVIDER_NONE, False
        crawl = self.filing_dates is True
        if crawl and self.api_key_present():
            try:
                rows = self.filing_rows_for_period(p["period"], targets, markets, rare=rare, provider=PROVIDER_API)
                return rows, PROVIDER_API, True
            except Exception as e:  # noqa: BLE001 - quota / API outage: keyless search this time, upgrade later
                from app.core.logsetup import redact

                fetched.meta.setdefault("api_fallback", {})[p["period"]] = redact(f"{type(e).__name__}: {e}")[:200]
                self.log.warning("dart_bulk %s: OpenDART list.json unavailable (%s); keyless search", p["period"],
                                 type(e).__name__)
        self.last_uncrawled = 0
        rows = safe_call(lambda: self.filing_rows_for_period(p["period"], targets, markets, rare=rare,
                                                             provider=PROVIDER_SEARCH, crawl=crawl),
                         fetched, f"filings:{p['period']}", self.log)
        return rows or [], PROVIDER_SEARCH, rows is not None and not self.last_uncrawled

    def normalize_one(self, p: dict[str, Any], fetched: FetchResult) -> tuple[dict[str, pl.DataFrame], dict[str, Any]]:
        """One bulk period (only its `todo` stock codes) -> tables + stats; filing dates resolved first."""
        paths = {s: Path(x) for s, x in p["files"].items()}
        todo = p.get("todo")
        if todo is None:
            codes_all: set[str] = set()
            for s in ("PL", "BS", "CF"):
                if s in paths:
                    codes_all |= zip_codes(paths[s])
            side = self.load_sidecar(p["period"]) or {}
            if side.get("files") == p["names"] and side.get("codes") not in (None, "*"):
                codes_all -= set(side["codes"])
            todo = sorted(codes_all)
            p["todo"], p["all_codes"] = todo, True
        first_seen = to_date(p["first_seen"])
        pkind = bulk_kind(p["period"])
        markets: set[str] = set()
        entries: dict[tuple[str, date], tuple[str, str, str, date, str]] = {}
        want = set(todo)
        for stmt in ("PL", "BS", "CF"):
            if stmt not in paths:
                continue
            for r in iter_zip_rows(paths[stmt], want):
                end, fye = to_date(r["end"]), int(r["fye"]) if r["fye"].isdigit() else 12
                if end and 1 <= fye <= 12 and months_into_fy(end, fye) in MONTHS:
                    mk = MARKET_CODE.get(r["market"], "E")
                    markets.add(mk)
                    if (r["code"], end) not in entries:
                        entries[(r["code"], end)] = (r["code"], r["name"], mk, end,
                                                     pkind or MONTHS[months_into_fy(end, fye)][0])
            break
        if self.all_listed and not fetched.meta.get("entities"):
            markets |= {"P", "A"}  # a company's market at filing time may differ from today's (KOSDAQ -> KOSPI moves)
        targets, rare = split_targets(entries.values())
        rows, provider, complete = (self._provider_rows(p, targets, markets, fetched, rare) if entries
                                    else ([], PROVIDER_NONE, True))
        xw = self.crosswalk()
        if provider == PROVIDER_API:
            for r in rows:
                if r.get("stock_code") and r.get("corp_code"):
                    self._xw_put(xw, r["stock_code"], {"dart_corp_code": r["corp_code"], "corp_name": r["corp_name"],
                                                       "market": r.get("market")}, "opendart_list", 1.0, p["period"])
        resolver = FilingResolver(rows, first_seen=first_seen, crosswalk=xw, provider=provider,
                                  amended_kd=self.amended_kd)
        issuer_of = self.reference()["issuer"]
        frames, fil_rows, stats = [], [], {"companies": 0, "filings": 0, "filing_dates_found": 0, "imputed": 0}
        for chunk in chunked(todo, self.chunk_codes):
            f_rows, i_rows, st = normalize_period(paths, codes=set(chunk), resolver=resolver, period=p["period"],
                                                  source_id=self.source_id, separate=self.separate, issuer_of=issuer_of)
            if f_rows:
                frames.append(frame(f_rows, "fundamentals_raw"))
            fil_rows += i_rows
            for k, v in st.items():
                stats[k] += v
            del f_rows
        for code, x in resolver.learned.items():
            self._xw_put(xw, code, x, "name_match", 0.9, p["period"])
            xw[code]["issuer_id"] = issuer_of.get(code)
            xw[code]["security_id"] = self.reference()["security"].get(code)
        facts = pl.concat(frames, how="vertical_relaxed") if frames else frame([], "fundamentals_raw")
        del frames
        stats.update({"facts": facts.height, "codes_todo": len(todo), "kd_provider": provider,
                      "dates_complete": complete, "filing_rows": len(rows),
                      **{k: v for k, v in sorted(resolver.stats.items())}})
        return {"fundamentals_raw": facts, "filings_index": frame(fil_rows, "filings_index")}, stats

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        facts, fil = [], []
        stats_all = {}
        for p in fetched.meta.get("periods", []):
            tables, stats = self.normalize_one(p, fetched)
            facts.append(tables["fundamentals_raw"])
            fil.append(tables["filings_index"])
            stats_all[p["period"]] = stats
            self.log.info("dart_bulk %s: %s", p["period"], stats)
        fetched.meta["stats"] = stats_all
        return {"fundamentals_raw": pl.concat(facts, how="vertical_relaxed") if facts else frame([], "fundamentals_raw"),
                "filings_index": pl.concat(fil, how="vertical_relaxed") if fil else frame([], "filings_index")}

    def write_period(self, tables: dict[str, pl.DataFrame]) -> dict[str, int]:
        """Replace-write one period: every stored row of the same (stock code, period end) is superseded."""
        out = {}
        facts, fil = tables["fundamentals_raw"], tables["filings_index"]
        keys = pl.concat([facts.select("source_entity_id", "period_end"), fil.select("source_entity_id", "period_end")],
                         how="vertical_relaxed").unique()
        ends = keys.get_column("period_end").unique().to_list()
        years = {d.year for d in ends if d is not None}
        out["fundamentals_raw"] = replace_rows(facts, "fundamentals_raw", keys,
                                               [(self.source_id, y + k) for y in years for k in (0, 1)], self.settings)
        out["filings_index"] = replace_rows(fil, "filings_index", keys,
                                            [(self.source_id, y + k) for y in years for k in (0, 1, 2)], self.settings)
        return out

    def run(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None,
            write: bool = True) -> tuple[FetchResult, dict[str, int]]:
        """fetch, then normalize + write period by period (each period is a resumable unit)."""
        rows = {"fundamentals_raw": 0, "filings_index": 0}
        self._mem_base = self._private_gb()
        try:
            self.check_keys()
            fetched = self.fetch(since=since, entities=entities)
            stats_all: dict[str, Any] = {}
            fetched.meta["stats"] = stats_all
            done_periods = []
            for i, p in enumerate(fetched.meta.get("periods", [])):
                ok, why = self._ram_ok()
                if not ok:
                    left = [x["period"] for x in fetched.meta["periods"][i:]]
                    fetched.meta["stopped"] = why
                    for x in left:
                        fetched.entities_failed[x] = f"deferred: {why}"
                        if x in fetched.entities_ok:
                            fetched.entities_ok.remove(x)
                    fetched.watermark = None
                    self.log.warning("dart_bulk: stopped before %s (%s); resumable", p["period"], why)
                    break
                t0 = time.time()
                tables, stats = self.normalize_one(p, fetched)
                if write:
                    got = self.write_period(tables)
                    for k, v in got.items():
                        rows[k] += v
                else:
                    for k in rows:
                        rows[k] += tables[k].height
                del tables
                gc.collect()
                stats["seconds"] = round(time.time() - t0, 1)
                mem = self._private_gb()
                if mem is not None:
                    stats["private_gb_after"] = round(mem, 2)
                if write:
                    self._mark_done(p, stats)
                    self.save_crosswalk()
                stats_all[p["period"]] = stats
                done_periods.append(p["period"])
                self.log.info("dart_bulk %s: %s", p["period"], stats)
            if write:
                self._finish_run(fetched)
            fetched.n_requests = self.http.n_requests + sum(h.n_requests for h in (self._search_http, self._api_http) if h)
            fetched.n_cache_hits = self.http.n_cache_hits
        finally:
            if self._dl_client is not None:
                self._dl_client.close()
                self._dl_client = None
        fetched.finished_at = datetime.now(UTC)
        self.log.info("%s run: status=%s rows=%s periods=%s", self.source_id, fetched.status, rows, done_periods)
        return fetched, rows

    def _mark_done(self, p: dict[str, Any], stats: dict[str, Any]) -> None:
        side = self.load_sidecar(p["period"]) or {}
        prev = side.get("codes") if side.get("files") == p["names"] else []
        token = load_state(self.source_id, self.settings).get("renorm_since")
        if token and (side.get("at") or "") < token:
            prev = []
        if side.get("provider") != stats["kd_provider"] and stats["kd_provider"] == PROVIDER_API:
            prev = []
        if self.filing_dates is True and side.get("dates_complete") is False:
            prev = []
        prev_set = set() if prev in (None, "*") else set(prev)
        cov = prev_set | set(p["todo"] or [])
        ref_codes = self.reference()["codes"]
        complete = bool(stats.get("dates_complete")) and (not prev_set or side.get("dates_complete") is not False)
        self.save_sidecar(p["period"], {"period": p["period"], "files": p["names"],
                                        "codes": "*" if p.get("all_codes") else sorted(cov),
                                        "provider": stats["kd_provider"], "dates_complete": complete,
                                        "at": _now_iso(), "stats": stats})
        st = load_state(self.source_id, self.settings)
        scope = ("all" if p.get("all_codes") else "all_listed" if ref_codes and ref_codes <= cov
                 else self.scope_hash(cov))
        st.setdefault("normalized", {})[p["period"]] = {"files": p["names"], "scope": scope, "at": _now_iso(),
                                                        "n_codes": len(cov), "stats": stats}
        save_state(self.source_id, st, self.settings)

    def _finish_run(self, fetched: FetchResult) -> None:
        st = load_state(self.source_id, self.settings)
        if self.all_listed and self.all_listed_param and not fetched.meta.get("entities"):
            st["scope_mode"] = "all_listed"
        token = st.get("renorm_since")
        if token:
            pending = []
            for period, names in (fetched.meta.get("all_periods") or {}).items():
                side = self.load_sidecar(period)
                if not side or side.get("files") != names or (side.get("at") or "") < token:
                    pending.append(period)
            if not pending:
                st.pop("renorm_since", None)
            else:
                st["renorm_pending"] = len(pending)
        if fetched.watermark:
            st["watermark"] = fetched.watermark
        save_state(self.source_id, st, self.settings)


def run(ctx: Any) -> Any:
    return run_adapter_job(DartBulkAdapter, ctx)
