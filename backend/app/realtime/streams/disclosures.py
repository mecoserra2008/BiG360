"""Recent regulatory disclosures outside the US (R1.2) -> raw.filings. Parsers / classifiers are reused from the
nightly adapters (app.ingestion.fca_nsm / tdnet / hkex / opendart); natural keys match the nightly filings_index ids.

| stream | source (official) | poll | key / filing_id |
|---|---|---|---|
| fca_nsm | FCA National Storage Mechanism search API (POST, newest 50, latest_flag=Y) | 60 s 06-20 London, else 300 s | fca_nsm:<disclosure_id> |
| tdnet | TSE TDnet list page 1 of today (JST, newest 100) | 60 s 07-22 JST Mon-Fri, else 300 s | tdnet:<list id> |
| hkex | HKEXnews title search, all SEHK stocks, yesterday..today (HKT, newest 100) | 60 s 06-23 HKT, else 300 s | hkex:news:<NEWS_ID> |
| dart | OpenDART list.json, all corporations, today (KST, newest 100); key required, 20k/day quota | 120 s 07-20 KST Mon-Fri, else 600 s | opendart:<rcept_no> |

Universe: items whose entity resolves through the identity layer are kept (all items when the identity map is
empty or `universe_only=False`). Every source uses its shared rate limiter (1 req/s; DART 3 req/s + daily quota).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope
from app.realtime.streams.base import StreamContext, StreamWorker

log = get_logger(__name__)

BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
LON, JST, HKT, KST = ZoneInfo("Europe/London"), ZoneInfo("Asia/Tokyo"), ZoneInfo("Asia/Hong_Kong"), ZoneInfo("Asia/Seoul")


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z") if ts else None


def _hours(now: datetime, tz: ZoneInfo, lo: int, hi: int, weekdays: bool = True) -> bool:
    t = now.astimezone(tz)
    return (not weekdays or t.weekday() < 5) and lo <= t.hour < hi


class _Disclosures(StreamWorker):
    topic = "raw.filings"
    region = ""
    country = ""
    tz: ZoneInfo = ZoneInfo("UTC")
    hours = (0, 24)
    fast_s, slow_s = 60.0, 300.0

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.http: Any = None
        self.universe_only = bool(params.get("universe_only", True))
        self._known: set[str] = set()

    def interval(self, ctx: StreamContext) -> float:
        if "interval_s" in self.params:
            return float(self.params["interval_s"])
        return self.fast_s if _hours(ctx.clock(), self.tz, *self.hours) else self.slow_s

    def client(self) -> Any:
        if self.http is None:
            from app.core.http import HttpClient  # noqa: PLC0415

            self.http = HttpClient(self.source_id, cache=False, max_retries=2, timeout=30,
                                   headers={"User-Agent": BROWSER_UA})
        return self.http

    def fetch(self, ctx: StreamContext) -> list[dict[str, Any]]:  # -> normalized rows (see `row()`)
        raise NotImplementedError

    @staticmethod
    def row(*, key: str, source_entity_id: str, company: str | None, form_type: str, form_class: str, title: str | None,
            url: str | None, ts: datetime | None, kdate: date | None = None, language: str | None = None,
            extra: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"key": key, "source_entity_id": source_entity_id, "company": company, "form_type": form_type,
                "form_class": form_class, "title": title, "url": url, "ts": ts, "kdate": kdate,
                "language": language, **(extra or {})}

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        rows = await ctx.run_sync(self.fetch, ctx)
        fresh = [r for r in rows if r["key"] not in self._known]
        self._known |= {r["key"] for r in rows}
        if len(self._known) > 50000:
            self._known = {r["key"] for r in rows}
        if not fresh:
            return []
        links: dict[tuple[str, str], Any] = {}
        filt = False
        if ctx.linker is not None:
            links = await ctx.run_sync(ctx.linker.resolve_many, [(self.source_id, r["source_entity_id"]) for r in fresh])
            filt = self.universe_only and ctx.linker.available()
        out = []
        for r in fresh:
            link = links.get((self.source_id, r["source_entity_id"]))
            if filt and not link:
                continue
            fid = f"{self.source_id}:{r['key']}"
            payload = {
                "filing_id": fid, "source_id": self.source_id, "source_entity_id": r["source_entity_id"],
                "company": r["company"], "form_type": r["form_type"], "form_class": r["form_class"],
                "title": r["title"], "items": [r["title"]] if r["title"] else None, "url": r["url"],
                "knowledge_ts": _iso(r["ts"]), "filed_date": (r["kdate"] or (r["ts"].astimezone(self.tz).date()
                                                                             if r["ts"] else None)),
                "language": r["language"], "country": self.country, "region": self.region,
                "issuer_id": (link or {}).get("issuer_id"), "security_id": (link or {}).get("security_id"),
                "ticker": (link or {}).get("ticker"),
                **{k: v for k, v in r.items() if k.startswith("x_")},
            }
            if payload["filed_date"] is not None:
                payload["filed_date"] = str(payload["filed_date"])
            out.append(Envelope("raw.filings", fid, payload, type="filing", ts_source=r["ts"]))
        return out


# ------------------------------------------------------------------------------------------------ FCA NSM
def parse_nsm(j: dict[str, Any]) -> list[dict[str, Any]]:
    from app.ingestion.fca_nsm import ARTEFACTS  # noqa: PLC0415

    hh = ((j.get("hits") or {}).get("hits")) if isinstance(j, dict) else None
    if hh is None:
        raise ValueError(f"NSM schema change: keys={list(j)[:5] if isinstance(j, dict) else type(j)}")
    out = []
    for h in hh:
        d = h.get("_source") or {}
        key = d.get("disclosure_id") or d.get("seq_id")
        if not key or not d.get("publication_date"):
            continue
        ts = datetime.fromisoformat(d["publication_date"].replace("Z", "+00:00")).astimezone(UTC)
        tc = d.get("type_code") or ""
        fclass = {"DSH": "insider", "ACS": "annual", "IR": "semiannual", "PFT": "prospectus", "PDI": "prospectus",
                  "HOL": "holdings", "RSV": "holdings"}.get(tc, "current_report")
        link = d.get("download_link") or d.get("html_link") or d.get("json_link")
        company = (d.get("company") or "").strip().strip(";").strip() or None
        out.append(_Disclosures.row(
            key=key, source_entity_id=d.get("lei") or f"name:{company or ''}", company=company, form_type=tc or "NSM",
            form_class=fclass, title=d.get("headline") or d.get("type"), url=(ARTEFACTS + link) if link else None,
            ts=ts, language="en", extra={"x_category": d.get("category_group"), "x_type": d.get("type")}))
    return out


class FcaNsmStream(_Disclosures):
    name = "fca_nsm"
    source_id = "fca_nsm"
    region, country, tz, hours = "UK", "GB", LON, (6, 20)

    def fetch(self, ctx: StreamContext) -> list[dict[str, Any]]:
        from app.ingestion.fca_nsm import SEARCH_URL  # noqa: PLC0415

        body = {"from": 0, "size": int(self.params.get("size", 50)), "sort": "submitted_date", "sortorder": "desc",
                "criteriaObj": {"criteria": [{"name": "latest_flag", "value": "Y"}], "dateCriteria": None}}
        return parse_nsm(self.client().post(SEARCH_URL, json_body=body).json())


# ------------------------------------------------------------------------------------------------ TDnet
def parse_tdnet(html: bytes | str, day: date) -> list[dict[str, Any]]:
    from app.ingestion.tdnet import BASE, classify_title, parse_tdnet_page  # noqa: PLC0415

    out = []
    for r in parse_tdnet_page(html, day):
        if not r.get("id"):
            continue
        ftype, fclass, fp = classify_title(r["title"])
        code = r["code5"][:4] if len(r["code5"]) == 5 and r["code5"].endswith("0") else r["code5"]
        try:
            hh, mm = (int(x) for x in r["time"].split(":")[:2])
            ts = datetime(day.year, day.month, day.day, hh, mm, tzinfo=JST).astimezone(UTC)
        except (ValueError, AttributeError):
            ts = None
        out.append(_Disclosures.row(key=r["id"], source_entity_id=code, company=r.get("name"), form_type=ftype,
                                    form_class=fclass, title=r["title"], url=BASE + r["pdf"] if r.get("pdf") else None,
                                    ts=ts, kdate=day, language="ja",
                                    extra={"x_fiscal_period": fp, "x_exchange": r.get("exchange"),
                                           "x_xbrl": (BASE + r["xbrl"]) if r.get("xbrl") else None}))
    return out


class TdnetStream(_Disclosures):
    """Disabled 2026-11-07 (COVERAGE_V3 A9): www.release.tdnet.info robots.txt `Disallow: /` -> source_disallowed.
    JP disclosures keep arriving through EDINET (nightly) and the JPX earnings schedule; no TDnet request is made."""

    name = "tdnet"
    source_id = "tdnet"
    region, country, tz, hours = "JP", "JP", JST, (7, 22)

    def enabled(self, ctx: StreamContext) -> tuple[bool, str | None]:
        from app.ingestion.tdnet import DISABLED_NOTE, DISABLED_REASON  # noqa: PLC0415

        if DISABLED_REASON:
            return False, DISABLED_NOTE
        return True, None  # pragma: no cover - the source is disallowed

    def fetch(self, ctx: StreamContext) -> list[dict[str, Any]]:
        from app.ingestion.tdnet import BASE, DISABLED_REASON  # noqa: PLC0415

        if DISABLED_REASON:  # belt and braces: enabled() already keeps the stream off
            return []
        day = ctx.clock().astimezone(JST).date()
        out: list[dict[str, Any]] = []
        for page in range(1, int(self.params.get("max_pages", 3)) + 1):
            r = self.client().get(f"{BASE}I_list_{page:03d}_{day:%Y%m%d}.html", cache_ttl=0, raise_for_status=False)
            if r.status_code == 404:
                break
            if r.status_code >= 400:
                from app.core.http import HttpError  # noqa: PLC0415

                raise HttpError(r.status_code, r.url)
            rows = parse_tdnet(r.content, day)
            out += rows
            if len(rows) < 100 or any(x["key"] in self._known for x in rows) or not self._known:
                break
        return out


# ------------------------------------------------------------------------------------------------ HKEXnews
def parse_hkex(j: dict[str, Any]) -> list[dict[str, Any]]:
    from app.ingestion.hkex import NEWS, classify_news, hk5  # noqa: PLC0415

    res = j.get("result") if isinstance(j, dict) else None
    if res in (None, "", "null"):
        return []
    items = json.loads(res) if isinstance(res, str) else res
    if items and "NEWS_ID" not in items[0]:
        raise ValueError(f"HKEXnews schema change: {list(items[0])[:6]}")
    out = []
    for n in items:
        try:
            ts = datetime.strptime(n.get("DATE_TIME") or "", "%d/%m/%Y %H:%M").replace(tzinfo=HKT).astimezone(UTC)
        except ValueError:
            ts = None
        codes = [c.strip() for c in (n.get("STOCK_CODE") or "").replace("<br/>", ",").split(",") if c.strip()]
        ftype, fclass = classify_news(n.get("TITLE") or "", n.get("LONG_TEXT") or "")
        title = " ".join((n.get("TITLE") or "").split())
        out.append(_Disclosures.row(
            key=f"news:{n['NEWS_ID']}", source_entity_id=hk5(codes[0]) if codes else "", company=(n.get("STOCK_NAME") or "")
            .split("<br/>")[0].strip() or None, form_type=ftype, form_class=fclass, title=title,
            url=NEWS + n["FILE_LINK"] if n.get("FILE_LINK") else None, ts=ts, language="en",
            extra={"x_category": (n.get("LONG_TEXT") or "").strip() or None,
                   "x_codes": [hk5(c) for c in codes] if len(codes) > 1 else None}))
    return out


class HkexStream(_Disclosures):
    name = "hkex"
    source_id = "hkex"
    region, country, tz, hours = "HK", "HK", HKT, (6, 23)

    def fetch(self, ctx: StreamContext) -> list[dict[str, Any]]:
        from app.ingestion.hkex import TITLES  # noqa: PLC0415

        now = ctx.clock().astimezone(HKT)
        p = dict(sortDir=0, sortByOptions="DateTime", category=0, market="SEHK", stockId=-1, documentType=-1,
                 fromDate=(now.date() - timedelta(days=1)).strftime("%Y%m%d"), toDate=now.strftime("%Y%m%d"),
                 title="", searchType=0, t1code=-2, t2Gcode=-2, t2code=-2, rowRange=int(self.params.get("rows", 100)),
                 lang="E")
        return parse_hkex(self.client().get(TITLES, params=p, cache_ttl=0).json())


# ------------------------------------------------------------------------------------------------ OpenDART
def parse_dart(j: dict[str, Any]) -> list[dict[str, Any]]:
    st = str(j.get("status", ""))
    if st == "013":
        return []
    if st != "000":
        raise RuntimeError(f"DART status {st} {j.get('message')}")
    out = []
    for r in j.get("list") or []:
        rno = r.get("rcept_no")
        if not rno:
            continue
        try:
            kd = datetime.strptime(r.get("rcept_dt") or rno[:8], "%Y%m%d").date()
        except ValueError:
            kd = None
        nm = " ".join((r.get("report_nm") or "").split())
        fclass = ("annual" if "사업보고서" in nm else "semiannual" if "반기보고서" in nm else "quarterly"
                  if "분기보고서" in nm else "insider" if ("임원" in nm and "소유" in nm) else "holdings"
                  if "대량보유" in nm else "current_report")
        out.append(_Disclosures.row(
            key=rno, source_entity_id=r.get("corp_code") or "", company=r.get("corp_name"), form_type="DART",
            form_class=fclass, title=nm, url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}", ts=None, kdate=kd,
            language="ko", extra={"x_stock_code": r.get("stock_code") or None, "x_corp_cls": r.get("corp_cls")}))
    return out


class DartStream(_Disclosures):
    name = "dart"
    source_id = "opendart"
    region, country, tz, hours = "KR", "KR", KST, (7, 20)
    fast_s, slow_s = 120.0, 600.0

    def enabled(self, ctx: StreamContext) -> tuple[bool, str | None]:
        if not ctx.settings.secret("opendart_apikey"):
            return False, "missing OPENDART_APIKEY in .env"
        return True, None

    def fetch(self, ctx: StreamContext) -> list[dict[str, Any]]:
        from app.core.ratelimit import get_daily_quota  # noqa: PLC0415

        q = get_daily_quota("opendart")
        if q is not None and not q.try_consume(1):
            raise RuntimeError("OpenDART daily quota reached")
        day = ctx.clock().astimezone(KST).strftime("%Y%m%d")
        j = self.client().get("https://opendart.fss.or.kr/api/list.json",
                              params={"crtfc_key": ctx.settings.secret("opendart_apikey"), "bgn_de": day, "end_de": day,
                                      "page_count": 100, "page_no": 1}, cache_ttl=0).json()
        return parse_dart(j)
