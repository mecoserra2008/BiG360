"""Hong Kong: HKEXnews announcements -> filings_index; HKEX Disclosure of Interests (DI) director / substantial
shareholder dealings -> insider_transactions (+ filings_index).

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §3.4-3.5 (live-tested 2026-10-10, no key).
  stock ids  https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json   [{"i":7609,"c":"00700","n":"TENCENT"}]
  titles     https://www1.hkexnews.hk/search/titleSearchServlet.do?...&stockId=<i>&fromDate=YYYYMMDD&toDate=..&rowRange=N&lang=E
             -> {"result": "<JSON string>"} items: NEWS_ID, DATE_TIME 'dd/mm/yyyy HH:MM' (HKT), TITLE, LONG_TEXT, FILE_LINK
  DI         https://di.hkex.com.hk/di/NSSrchCorpList.aspx?sa1=cl&scsd=dd/mm/yyyy&sced=dd/mm/yyyy&sc=<code>&src=MAIN&lang=EN
             -> link NSAllFormList.aspx?sa2=an&sid=<sid>&... -> table: Form Serial Number (DA20260819E00389 = filed
             2026-08-19) | Name | Reason for disclosure (SFC event code, e.g. 1101(L)) | No. of shares bought/sold/involved |
             Average price per share | No. of shares interested | % of issued voting shares | Date of relevant event
SFC event codes (Consolidated Codes, 3 Jul 2017): 1001/1101 purchased -> buy; 1201 sale -> sell; 1002/1102 given ->
grant; 1202 gift; 1303 exercised derivative rights -> option_exercise; everything else -> other.
PIT: announcements DATE_TIME (HKT); DI filing date from the form serial. source_entity_id = 5-digit HK code.
"""

from __future__ import annotations

import io
import json
import re
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, hkt_to_utc, insider_row, parse_num, run_adapter_job,
                                          safe_call, seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

STOCKS = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
TITLES = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
NEWS = "https://www1.hkexnews.hk"
DI = "https://di.hkex.com.hk/di/"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
DI_CODES = {"1001": "buy", "1101": "buy", "1201": "sell", "1002": "grant", "1102": "grant", "1202": "gift",
            "1303": "option_exercise"}


def hk5(code: str) -> str:
    c = code.upper().replace(".HK", "").strip()
    return c.zfill(5) if c.isdigit() else c


def _long(s: Any) -> float | None:
    """'1,682(L)' or '74,610(L) 0(S)' -> long-position number."""
    if s is None or (isinstance(s, float) and s != s):
        return None
    m = re.search(r"([\d,\.]+)\s*\(L\)", str(s))
    if m:
        return parse_num(m.group(1))
    if "(" not in str(s):
        return parse_num(s)
    return None


def parse_di_table(html: bytes | str) -> list[dict[str, Any]]:
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    try:
        tables = pd.read_html(io.StringIO(text))
    except ValueError:
        return []
    out = []
    for t in tables:
        first = [str(x) for x in t.iloc[0].tolist()] if len(t) else []
        if "Form Serial Number" not in first:
            continue
        t = t.iloc[1:]
        t.columns = first
        for _, r in t.iterrows():
            serial = str(r.get("Form Serial Number") or "")
            m = re.match(r"^([A-Z]{2})(\d{8})", serial)
            if not m:
                continue
            reason = str(r.get("Reason for disclosure") or "")
            code = re.match(r"(\d{4,5})", reason)
            price_raw = r.get("Average price per share")
            ccy = None
            if isinstance(price_raw, str):
                cm = re.search(r"\b([A-Z]{3})\b", price_raw)
                ccy = cm.group(1) if cm else None
            out.append({"serial": serial, "form": m.group(1), "filed": to_date(m.group(2)),
                        "name": str(r.get("Name of substantial shareholder / director / chief executive") or "").strip(),
                        "reason": reason, "code": code.group(1) if code else None,
                        "shares": _long(r.get("No. of shares bought / sold / involved")),
                        "price": parse_num(price_raw) if price_raw is not None and str(price_raw) != "nan" else None,
                        "currency": ccy or "HKD",
                        "held_after": _long(r.get("No. of shares interested (See *Notes above)")),
                        "event_date": to_date(str(r.get("Date of relevant event (dd/mm/yyyy)") or ""))})
    return out


def classify_news(title: str, long_text: str) -> tuple[str, str]:
    t = f"{title} {long_text}".lower()
    if "annual report" in t:
        return "ANNUAL_REPORT", "annual"
    if "interim report" in t:
        return "INTERIM_REPORT", "semiannual"
    if "quarterly report" in t:
        return "QUARTERLY_REPORT", "quarterly"
    if "results" in t:
        return "RESULTS", "current_report"
    if "prospectus" in t:
        return "PROSPECTUS", "prospectus"
    if "circular" in t or "proxy" in t:
        return "CIRCULAR", "proxy"
    return "ANNOUNCEMENT", "current_report"


@register_adapter
class HkexAdapter(SourceAdapter):
    source_id = "hkex"
    regions = ("HK",)
    produces = ("filings_index", "insider_transactions")
    rate_limit = RateLimit(1.0, 1)

    def __init__(self, *args: Any, di: bool = True, news: bool = True, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = BROWSER_UA
        self.di = str(di).lower() not in ("0", "false")
        self.news = str(news).lower() not in ("0", "false")

    def stock_ids(self) -> dict[str, dict[str, Any]]:
        j = self.http.get(STOCKS, cache_ttl=86400).json()
        return {x["c"]: x for x in j}

    def discover(self) -> pl.DataFrame:
        rows = [{"source_entity_id": c, "name": x.get("n"), "country": "HK", "ticker": c, "mic": "XHKG",
                 "yf_ticker": f"{c[1:] if c.startswith('0') else c}.HK", "is_active": True}
                for c, x in self.stock_ids().items()]
        return self.discover_frame(rows, self.source_id)

    def _news(self, stock_id: int, since: date, until: date) -> list[dict[str, Any]]:
        params = dict(sortDir=0, sortByOptions="DateTime", category=0, market="SEHK", stockId=stock_id, documentType=-1,
                      fromDate=since.strftime("%Y%m%d"), toDate=until.strftime("%Y%m%d"), title="", searchType=0,
                      t1code=-2, t2Gcode=-2, t2code=-2, rowRange=500, lang="E")
        j = self.http.get(TITLES, params=params, cache_ttl=3600).json()
        res = j.get("result")
        if res in (None, "", "null"):
            return []
        items = json.loads(res) if isinstance(res, str) else res
        if items and "NEWS_ID" not in items[0]:
            raise ValueError(f"HKEXnews schema change: {list(items[0])[:6]}")
        return items

    def _di(self, code5: str, since: date, until: date) -> bytes | None:
        sd, ed = since.strftime("%d/%m/%Y"), until.strftime("%d/%m/%Y")
        r = self.http.get(DI + "NSSrchCorpList.aspx", params={"sa1": "cl", "scsd": sd, "sced": ed, "sc": str(int(code5)),
                                                              "src": "MAIN", "lang": "EN"}, cache_ttl=6 * 3600)
        m = re.search(r'href="(NSAllFormList\.aspx\?[^"]+)"', r.text)
        if not m:
            return None
        r2 = self.http.get(urljoin(DI, m.group(1).replace("&amp;", "&")), cache_ttl=6 * 3600)
        return r2.content

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        today = datetime.now(UTC).date()
        since_d = to_date(since) or (today - timedelta(days=365 if entities else 30))
        codes = [hk5(e) for e in (entities or seed_universe(self.settings, countries=None, regions=["HK"]).get_column(
            "ticker").to_list())]
        ids = safe_call(self.stock_ids, res, "stock_ids", self.log) or {}
        landed = []
        for c in codes:
            def one(c=c) -> None:
                item: dict[str, Any] = {"code": c}
                if self.news and c in ids:
                    item["news"] = self._news(ids[c]["i"], since_d, today)
                    item["news_path"] = str(self.save_raw("titlesearch", c, item["news"]))
                if self.di:
                    html = self._di(c, since_d, today)
                    if html:
                        item["di_path"] = str(self.save_raw("di", c, html))
                landed.append(item)
            safe_call(one, res, c, self.log)
        res.meta["items"] = [{k: v for k, v in x.items() if k != "news"} for x in landed]
        res.meta["since"] = since_d.isoformat()
        res.watermark = today.isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        fil, ins = [], []
        since = to_date(fetched.meta.get("since"))
        for it in fetched.meta.get("items", []):
            c = it["code"]
            if it.get("news_path"):
                for n in self.load_raw_json(Path(it["news_path"])):
                    d = to_date(n.get("DATE_TIME"))
                    if d is None:
                        continue
                    ftype, fclass = classify_news(n.get("TITLE") or "", n.get("LONG_TEXT") or "")
                    fil.append(filing_row(
                        source_id=self.source_id, native_key=f"news:{n['NEWS_ID']}", source_entity_id=c, form_type=ftype,
                        form_class=fclass, knowledge_date=d, knowledge_ts=hkt_to_utc(d, (n.get("DATE_TIME") or "")[11:16]),
                        language="en", url=NEWS + n["FILE_LINK"] if n.get("FILE_LINK") else None,
                        items=[(n.get("TITLE") or "")[:200], (n.get("LONG_TEXT") or "")[:200]]))
            if it.get("di_path"):
                for i, r in enumerate(parse_di_table(self.load_raw(Path(it["di_path"])))):
                    if r["filed"] is None or (since and r["filed"] < since):
                        continue
                    fid = f"{self.source_id}:di:{r['serial']}"
                    is_dir = r["form"].startswith("D")
                    fil.append(filing_row(source_id=self.source_id, native_key=f"di:{r['serial']}", source_entity_id=c,
                                          form_type=f"DI_{r['form']}", form_class="insider", knowledge_date=r["filed"],
                                          language="en", items=[r["reason"]]))
                    if not r["shares"]:
                        continue
                    txn = DI_CODES.get(r["code"] or "", "other")
                    ins.append(insider_row(
                        source_id=self.source_id, filing_id=fid, line=0, region="HK", source_entity_id=c,
                        insider_name=r["name"], officer_title="director/chief executive" if is_dir else "substantial shareholder",
                        is_director=is_dir, is_officer=False, is_ten_pct_owner=not is_dir,
                        role_norm="director" if is_dir else "owner", transaction_date=r["event_date"] or r["filed"],
                        knowledge_date=r["filed"], txn_code=r["reason"], txn_type=txn,
                        is_open_market=txn in ("buy", "sell") and bool(r["price"]), shares=r["shares"],
                        price=r["price"], currency=r["currency"], shares_owned_after=r["held_after"],
                        form_type=f"DI_{r['form']}"))
        return {"filings_index": frame(fil, "filings_index"), "insider_transactions": frame(ins, "insider_transactions")}


def run(ctx: Any) -> Any:
    return run_adapter_job(HkexAdapter, ctx)
