"""Korea FSS OpenDART -> filings_index + fundamentals_raw (K-IFRS statements) + insider_transactions (executive and
5% major-holder ownership reports).

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §2.1 (endpoint reachable; key required).
  corpCode.xml                       zip -> CORPCODE.xml (corp_code 8, corp_name, stock_code 6, modify_date)
  list.json?corp_code&bgn_de&end_de&pblntf_ty=A&page_count=100   periodic reports (rcept_no, rcept_dt, report_nm)
  fnlttSinglAcntAll.json?corp_code&bsns_year&reprt_code&fs_div=CFS|OFS   full statements (account_id = XBRL element)
  fnlttMultiAcnt.json?corp_code=<<=100 comma list>&bsns_year&reprt_code  key accounts, many companies (backfill)
  elestock.json?corp_code            executive holdings history  ("Korean Form 4")
  majorstock.json?corp_code          5% large-holding reports
  status: 000 ok, 013 no data, 020 quota exceeded, 010/011/100 key problems.
reprt_code: 11013 Q1, 11012 H1, 11014 Q3, 11011 annual. Amounts are strings with commas; KRW full units.
Quota: 20,000 calls/day per key, tracked in data/state/opendart_quota.json (DailyQuota, reset at 00:00 KST).
PIT: knowledge_date = rcept_dt (= rcept_no[:8]); date only (assume next KRX session).
Concepts: account_id as reported ('ifrs-full_Revenue', 'dart_OperatingIncomeLoss'); non-standard accounts and
fnlttMultiAcnt rows (no account_id) are stored as 'dart_label:<account_nm>' unless the label maps 1:1 to a standard
DART/IFRS element (KR_LABEL_TO_ID). taxonomy: 'kifrs' for ifrs-full_*, 'dart' otherwise. Accounting standard K-IFRS;
consolidated=True for fs_div=CFS.
Key: OPENDART_APIKEY (adapter reports 'missing key' and is skipped when absent).
"""

from __future__ import annotations

import calendar
import io
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl
from lxml import etree

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (DailyQuota, chunked, fact_row, filing_row, frame, insider_row, load_state,
                                          parse_num, run_adapter_job, safe_call, save_state, seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

REPRT = {"11013": ("Q1", 3), "11012": ("H1", 6), "11014": ("Q3", 9), "11011": ("FY", 12)}
KR_LABEL_TO_ID = {
    "매출액": "ifrs-full_Revenue", "수익(매출액)": "ifrs-full_Revenue", "영업수익": "ifrs-full_Revenue",
    "매출원가": "ifrs-full_CostOfSales", "매출총이익": "ifrs-full_GrossProfit", "영업이익": "dart_OperatingIncomeLoss",
    "영업이익(손실)": "dart_OperatingIncomeLoss", "법인세차감전 순이익": "ifrs-full_ProfitLossBeforeTax",
    "법인세비용차감전순이익(손실)": "ifrs-full_ProfitLossBeforeTax", "당기순이익": "ifrs-full_ProfitLoss",
    "당기순이익(손실)": "ifrs-full_ProfitLoss", "자산총계": "ifrs-full_Assets", "부채총계": "ifrs-full_Liabilities",
    "자본총계": "ifrs-full_Equity", "유동자산": "ifrs-full_CurrentAssets", "유동부채": "ifrs-full_CurrentLiabilities",
    "비유동자산": "ifrs-full_NoncurrentAssets", "비유동부채": "ifrs-full_NoncurrentLiabilities",
    "자본금": "ifrs-full_IssuedCapital", "이익잉여금": "ifrs-full_RetainedEarnings",
}
OK, NODATA, QUOTA = "000", "013", "020"


class DartStatusError(RuntimeError):
    pass


def fy_period_end(bsns_year: int, reprt_code: str, fye_month: int = 12) -> tuple[date, date, date]:
    """-> (fiscal year start, period end of the report, quarter start) for a Dec (or other) fiscal year end."""
    months = REPRT[reprt_code][1]
    fy_start = date(bsns_year if fye_month == 12 else bsns_year - 1, fye_month % 12 + 1, 1)
    m = (fy_start.month - 1 + months - 1) % 12 + 1
    y = fy_start.year + (fy_start.month - 1 + months - 1) // 12
    end = date(y, m, calendar.monthrange(y, m)[1])
    qm = (m - 3) % 12 + 1
    qy = y if m > 2 else y - 1
    q_start = date(qy, qm, 1)
    return fy_start, end, q_start


def concept_of(account_id: str | None, account_nm: str | None) -> tuple[str, str]:
    aid = (account_id or "").strip()
    nm = re.sub(r"\s+", " ", (account_nm or "").strip())
    if aid and not aid.startswith("-") and re.match(r"^(ifrs|dart|entity)", aid):
        return ("kifrs" if aid.startswith("ifrs") else "dart"), aid
    if nm in KR_LABEL_TO_ID:
        cid = KR_LABEL_TO_ID[nm]
        return ("kifrs" if cid.startswith("ifrs") else "dart"), cid
    return "dart", f"dart_label:{nm}"


def parse_fnltt_rows(rows: list[dict[str, Any]], *, corp_code: str, bsns_year: int, reprt_code: str, fs_div: str,
                     knowledge_date: date, filing_id: str, fye_month: int = 12,
                     source_id: str = "opendart") -> list[dict[str, Any]]:
    """fnlttSinglAcntAll / fnlttMultiAcnt rows -> fundamentals_raw rows (current-period amounts only)."""
    fp, _months = REPRT[reprt_code]
    fy_start, end, q_start = fy_period_end(bsns_year, reprt_code, fye_month)
    out = []
    for r in rows:
        sj = (r.get("sj_div") or "").upper()
        if sj not in ("BS", "IS", "CIS", "CF"):
            continue
        if r.get("fs_div") and r["fs_div"] != fs_div:
            continue
        taxonomy, concept = concept_of(r.get("account_id"), r.get("account_nm"))
        ccy = (r.get("currency") or "KRW").strip() or "KRW"
        base = dict(source_id=source_id, source_entity_id=corp_code, filing_id=filing_id, form_type=reprt_code,
                    taxonomy=taxonomy, concept=concept, label=(r.get("account_nm") or "").strip() or None, unit=ccy,
                    knowledge_date=knowledge_date, fiscal_year=bsns_year, consolidated=fs_div == "CFS")
        cur = parse_num(r.get("thstrm_amount"))
        add = parse_num(r.get("thstrm_add_amount"))
        if sj == "BS":
            if cur is not None:
                out.append(fact_row(**base, period_start=None, period_end=end, value=cur, fiscal_period=None))
        elif sj in ("IS", "CIS"):
            if fp == "FY":
                if cur is not None:
                    out.append(fact_row(**base, period_start=fy_start, period_end=end, value=cur, fiscal_period="FY"))
            else:
                if cur is not None:
                    qfp = {"Q1": "Q1", "H1": "Q2", "Q3": "Q3"}[fp]
                    out.append(fact_row(**base, period_start=q_start, period_end=end, value=cur, fiscal_period=qfp))
                if add is not None and fp != "Q1":
                    out.append(fact_row(**base, period_start=fy_start, period_end=end, value=add,
                                        fiscal_period="H1" if fp == "H1" else "9M"))
        else:  # CF is cumulative YTD
            if cur is not None:
                out.append(fact_row(**base, period_start=fy_start, period_end=end, value=cur,
                                    fiscal_period={"Q1": "Q1", "H1": "H1", "Q3": "9M", "FY": "FY"}[fp]))
    return out


def report_kind(report_nm: str) -> tuple[str, int] | None:
    """'사업보고서 (2025.12)' -> ('11011', 2025); '분기보고서 (2026.03)' -> ('11013', 2026)."""
    m = re.search(r"(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)", report_nm or "")
    if not m:
        return None
    kind, y, mo = m.group(1), int(m.group(2)), int(m.group(3))
    if kind == "사업":
        return "11011", y
    if kind == "반기":
        return "11012", y
    return ("11013" if mo <= 4 else "11014"), y


def elestock_rows(rows: list[dict[str, Any]], corp_code: str, source_id: str = "opendart") -> tuple[list, list]:
    fil, ins = [], []
    for i, r in enumerate(rows):
        rno = r.get("rcept_no") or ""
        kd = to_date(r.get("rcept_dt")) or to_date(rno[:8])
        chg = parse_num(r.get("sp_stock_lmp_irds_cnt"))
        if kd is None or not chg:
            continue
        fid = f"{source_id}:{rno}"
        fil.append(filing_row(source_id=source_id, native_key=rno, source_entity_id=corp_code, form_type="ELESTOCK",
                              form_class="insider", knowledge_date=kd, language="ko",
                              url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}"))
        title = " ".join(x for x in (r.get("isu_exctv_rgist_at"), r.get("isu_exctv_ofcps")) if x and x != "-")
        main = (r.get("isu_main_shrholdr") or "").strip()
        ins.append(insider_row(
            source_id=source_id, filing_id=fid, line=f"ele{i}", region="KR", source_entity_id=corp_code,
            insider_name=r.get("repror") or "?", officer_title=title or None,
            is_director="등기" in (r.get("isu_exctv_rgist_at") or "") and "미등기" not in (r.get("isu_exctv_rgist_at") or ""),
            is_officer=bool(r.get("isu_exctv_ofcps") and r["isu_exctv_ofcps"] != "-"),
            is_ten_pct_owner=bool(main and main != "-"), transaction_date=kd, knowledge_date=kd,
            txn_code="KR_ELESTOCK", txn_type="buy" if chg > 0 else "sell", is_open_market=True, shares=abs(chg),
            shares_owned_after=parse_num(r.get("sp_stock_lmp_cnt")), currency="KRW", form_type="ELESTOCK"))
    return fil, ins


def majorstock_rows(rows: list[dict[str, Any]], corp_code: str, source_id: str = "opendart") -> tuple[list, list]:
    fil, ins = [], []
    for i, r in enumerate(rows):
        rno = r.get("rcept_no") or ""
        kd = to_date(r.get("rcept_dt")) or to_date(rno[:8])
        chg = parse_num(r.get("stkqy_irds"))
        fid = f"{source_id}:{rno}"
        if kd is None:
            continue
        fil.append(filing_row(source_id=source_id, native_key=rno, source_entity_id=corp_code, form_type="MAJORSTOCK",
                              form_class="holdings", knowledge_date=kd, language="ko",
                              url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}",
                              items=[(r.get("report_resn") or "")[:200]]))
        if not chg:
            continue
        ins.append(insider_row(
            source_id=source_id, filing_id=fid, line=f"maj{i}", region="KR", source_entity_id=corp_code,
            insider_name=r.get("repror") or "?", officer_title="5% holder", is_director=False, is_officer=False,
            is_ten_pct_owner=True, role_norm="owner", transaction_date=kd, knowledge_date=kd, txn_code="KR_MAJORSTOCK",
            txn_type="buy" if chg > 0 else "sell", is_open_market=False, shares=abs(chg),
            shares_owned_after=parse_num(r.get("stkqy")), currency="KRW", form_type="MAJORSTOCK"))
    return fil, ins


@register_adapter
class OpenDartAdapter(SourceAdapter):
    source_id = "opendart"
    regions = ("KR",)
    produces = ("filings_index", "fundamentals_raw", "insider_transactions")
    requires_keys = ("opendart_apikey",)
    base_url = "https://opendart.fss.or.kr/api"
    rate_limit = RateLimit(3.0, 3)

    def __init__(self, *args: Any, backfill_years: str | int | None = None, insiders: bool = True,
                 daily_limit: int = 20000, **kw: Any):
        super().__init__(*args, **kw)
        self.quota = DailyQuota(self.source_id, int(daily_limit), self.settings)
        self.backfill_years = [int(y) for y in str(backfill_years).split(",")] if backfill_years else []
        self.insiders = str(insiders).lower() not in ("0", "false")

    # ------------------------------------------------------------------ api
    def api(self, endpoint: str, params: dict[str, Any], cache_ttl: int = 0) -> Any:
        if not self.quota.take(1):
            raise DartStatusError("020 local daily quota reached")
        r = self.http.get(endpoint, params={**params, "crtfc_key": self.key("opendart_apikey")}, cache_ttl=cache_ttl)
        if endpoint.endswith(".xml") and r.content[:2] == b"PK":
            return r.content
        j = r.json()
        st = str(j.get("status", ""))
        if st == NODATA:
            return None
        if st != OK:
            raise DartStatusError(f"{st} {j.get('message')}")
        return j

    def corp_codes(self) -> pl.DataFrame:
        content = self.api("corpCode.xml", {}, cache_ttl=86400)
        zf = zipfile.ZipFile(io.BytesIO(content))
        root = etree.fromstring(zf.read(zf.namelist()[0]))
        rows = [{"corp_code": e.findtext("corp_code"), "corp_name": e.findtext("corp_name"),
                 "stock_code": (e.findtext("stock_code") or "").strip() or None, "modify_date": e.findtext("modify_date")}
                for e in root.iter("list")]
        self.save_raw("corpcode", "CORPCODE", rows)
        return pl.DataFrame(rows)

    def discover(self) -> pl.DataFrame:
        df = self.corp_codes().filter(pl.col("stock_code").is_not_null())
        rows = [{"source_entity_id": r["corp_code"], "dart_corp_code": r["corp_code"], "kr_stock_code": r["stock_code"],
                 "name": r["corp_name"], "name_local": r["corp_name"], "country": "KR", "is_active": True}
                for r in df.iter_rows(named=True)]
        return self.discover_frame(rows, self.source_id)

    # ------------------------------------------------------------------ fetch
    def _targets(self, entities: Sequence[str] | None) -> list[str]:
        cc = self.corp_codes()
        by_stock = {r["stock_code"]: r["corp_code"] for r in cc.iter_rows(named=True) if r["stock_code"]}
        wanted = list(entities) if entities else seed_universe(self.settings, countries=["KR"]).get_column("ticker").to_list()
        out = []
        for e in wanted:
            e = e.split(".")[0]
            code = e if len(e) == 8 else by_stock.get(e.zfill(6))
            if code and code not in out:
                out.append(code)
        return out

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        today = datetime.now(UTC).date()
        since_d = to_date(since) or (to_date(state["watermark"]) - timedelta(days=3) if state.get("watermark")
                                     else today - timedelta(days=400))
        corps = safe_call(lambda: self._targets(entities), res, "corpCode", self.log) or []
        statements: list[dict[str, Any]] = []
        holdings: list[dict[str, Any]] = []
        for corp in corps:
            def one(corp=corp) -> None:
                lst = self.api("list.json", {"corp_code": corp, "bgn_de": since_d.strftime("%Y%m%d"),
                                             "end_de": today.strftime("%Y%m%d"), "pblntf_ty": "A", "page_count": 100},
                               cache_ttl=3600) or {}
                for x in lst.get("list", []):
                    k = report_kind(x.get("report_nm", ""))
                    if not k or "철" in (x.get("rm") or ""):
                        continue
                    reprt, year = k
                    for fs_div in ("CFS", "OFS"):
                        j = self.api("fnlttSinglAcntAll.json", {"corp_code": corp, "bsns_year": year, "reprt_code": reprt,
                                                                 "fs_div": fs_div}, cache_ttl=-1)
                        if j and j.get("list"):
                            p = self.save_raw("fnltt", f"{corp}_{year}_{reprt}_{fs_div}", j["list"])
                            statements.append({"corp": corp, "year": year, "reprt": reprt, "fs_div": fs_div,
                                               "rcept_no": x["rcept_no"], "rcept_dt": x["rcept_dt"],
                                               "report_nm": x["report_nm"], "_raw_path": str(p)})
                            break  # CFS preferred; OFS only when no consolidated statements
                if self.insiders:
                    for ep in ("elestock.json", "majorstock.json"):
                        j = self.api(ep, {"corp_code": corp}, cache_ttl=6 * 3600)
                        if j and j.get("list"):
                            holdings.append({"corp": corp, "kind": ep.split(".")[0],
                                             "_raw_path": str(self.save_raw(ep.split(".")[0], corp, j["list"]))})
            safe_call(one, res, corp, self.log)
        for year in self.backfill_years:
            for reprt in REPRT:
                for chunk in chunked(corps, 100):
                    def multi(chunk=chunk, year=year, reprt=reprt) -> None:
                        j = self.api("fnlttMultiAcnt.json", {"corp_code": ",".join(chunk), "bsns_year": year,
                                                             "reprt_code": reprt}, cache_ttl=-1)
                        if j and j.get("list"):
                            statements.append({"multi": True, "year": year, "reprt": reprt,
                                               "_raw_path": str(self.save_raw("multi", f"{year}_{reprt}_{chunk[0]}", j["list"]))})
                    safe_call(multi, res, f"multi:{year}:{reprt}:{chunk[0]}", self.log)
        res.meta.update(statements=statements, holdings=holdings, quota_used=self.quota.used())
        if not entities:
            res.watermark = today.isoformat()
            state["watermark"] = res.watermark
            save_state(self.source_id, state, self.settings)
        return res

    # ------------------------------------------------------------------ normalize
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        from pathlib import Path

        fil: list[dict[str, Any]] = []
        facts: list[dict[str, Any]] = []
        ins: list[dict[str, Any]] = []
        for s in fetched.meta.get("statements", []):
            rows = self.load_raw_json(Path(s["_raw_path"]))
            if s.get("multi"):
                by_corp: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
                for r in rows:
                    by_corp.setdefault((r["corp_code"], r["rcept_no"], r.get("fs_div", "CFS")), []).append(r)
                for (corp, rno, fs_div), rr in by_corp.items():
                    kd = to_date(rno[:8])
                    fid = f"{self.source_id}:{rno}"
                    facts += parse_fnltt_rows(rr, corp_code=corp, bsns_year=s["year"], reprt_code=s["reprt"],
                                              fs_div=fs_div, knowledge_date=kd, filing_id=fid)
                    fil.append(self._filing(corp, rno, kd, s["year"], s["reprt"], None))
                continue
            kd = to_date(s["rcept_dt"]) or to_date(s["rcept_no"][:8])
            fid = f"{self.source_id}:{s['rcept_no']}"
            facts += parse_fnltt_rows(rows, corp_code=s["corp"], bsns_year=s["year"], reprt_code=s["reprt"],
                                      fs_div=s["fs_div"], knowledge_date=kd, filing_id=fid)
            fil.append(self._filing(s["corp"], s["rcept_no"], kd, s["year"], s["reprt"], s.get("report_nm")))
        for h in fetched.meta.get("holdings", []):
            rows = self.load_raw_json(Path(h["_raw_path"]))
            f, i = (elestock_rows if h["kind"] == "elestock" else majorstock_rows)(rows, h["corp"], self.source_id)
            fil += f
            ins += i
        return {"filings_index": frame(fil, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw"),
                "insider_transactions": frame(ins, "insider_transactions")}

    def _filing(self, corp: str, rno: str, kd: date, year: int, reprt: str, report_nm: str | None) -> dict[str, Any]:
        fp = REPRT[reprt][0]
        _, end, _ = fy_period_end(year, reprt)
        return filing_row(source_id=self.source_id, native_key=rno, source_entity_id=corp, form_type=reprt,
                          form_class={"FY": "annual", "H1": "semiannual"}.get(fp, "quarterly"), knowledge_date=kd,
                          period_end=end, fiscal_year=year, fiscal_period=fp,
                          is_amendment=bool(report_nm and "정정" in report_nm), language="ko",
                          url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}", has_xbrl=True,
                          items=["accounting_standard=K-IFRS", f"report_nm={report_nm or ''}"])


def run(ctx: Any) -> Any:
    return run_adapter_job(OpenDartAdapter, ctx)
