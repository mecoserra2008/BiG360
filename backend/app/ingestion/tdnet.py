"""Japan TDnet timely-disclosure list (kessan tanshin, guidance revisions, buybacks) -> filings_index.

Source research: docs/DATA_SOURCES_ASIA_MACRO_EVENTS.md §1.2 (live-tested 2026-10-10).
  https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html  (100 rows/page; ~31-day retention!)
  row: 時刻 | コード (5-digit) | 会社名 | 表題 (<a href="{id}.pdf">) | XBRL (<a href="{id}.zip">) | 上場取引所 | 更新履歴
Quarterly reports were abolished in Apr-2024, so Q1/Q3 numbers exist only in tanshin; this adapter indexes them
(metadata + PDF/XBRL links). Because of the short retention it must run daily (nightly DAG).
PIT: knowledge_ts = list date + 時刻 (JST, to the minute). Keys: none. source_entity_id = 4-char TSE code (jp_sec_code).

Kessan-tanshin XBRL -> fundamentals_raw (docs/COMPANY_COVERAGE_OPTIONS.md §4.5, verified 2026-10-11):
  https://www.release.tdnet.info/inbs/{xbrl}.zip (60-120 KB) = XBRLData/Summary/tse-*-ixbrl.htm (tse-ed-t: NetSales /
  SalesIFRS / OperatingIncome / ProfitAttributableToOwnersOfParent / NetIncomePerShare ...; contexts carry Result /
  Forecast / Upper / Lower members) + optional XBRLData/Attachment/*-ixbrl.htm statements (jppfs_cor / jpigp_cor QNames =
  canonical 'jp' chains). Inline-XBRL namespace http://www.xbrl.org/2008/inlineXBRL; periods from xbrli:context.
  concept = full QName ('tse-ed-t:NetSales', 'jppfs_cor:NetSales'; ifrs-full bare), taxonomy jppfs/jpigp/jpcrp/ifrs-full
  or 'other'. Company forecasts (Forecast/Upper/Lower members) -> dims {ResultForecast: ..} + is_forecast=true: they are
  never canonical (dimensional, and knowledge_date < period_end). Non-consolidated facts of a filing that also has
  consolidated facts carry dims {Consolidation: NonConsolidated}. knowledge_date = list date (JST), filing_id =
  'tdnet:<list id>' (same as the filings_index row). Q1-Q3 summary figures are YTD (the normalizer derives quarters).
  Retention ~31 days -> the nightly job lands every tanshin zip of the window (param xbrl_scope=all|universe|none,
  default all) under raw/tdnet/xbrl/dt=<list date>/ (skipped when already landed: resumable).

DISABLED 2026-11-07 (docs/COVERAGE_V3_PLAN.md A9, user decision "switch all"; docs/it/ROBOTS_COMPLIANCE.md):
www.release.tdnet.info robots.txt is `User-agent: *` / `Disallow: /`. The job `tdnet_disclosures` returns skipped
(reason code source_disallowed) without any request, the realtime `tdnet` stream is disabled, and
app.core.robots_policy blocks the host in app.core.http. Rows ingested before the switch stay in filings_index /
fundamentals_raw with source_id tdnet (and their release.tdnet.info document links). Japan now comes from the JPX
earnings schedule (jpx.co.jp, allowed: earnings dates) + Yahoo dates, and EDINET (statutory reports / statements).
The parsers below stay for those stored rows and the tests.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from bs4 import BeautifulSoup

from app.core.http import HttpError
from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (fact_row, filing_row, fiscal_year_for, frame, jst_to_utc, load_state,
                                          run_adapter_job, safe_call, save_state, seed_universe, to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

BASE = "https://www.release.tdnet.info/inbs/"
#: A9 2026-11-07: robots.txt `Disallow: /` -> no fetching (reason code source_disallowed)
DISABLED_REASON = "source_disallowed"
DISABLED_NOTE = ("source_disallowed: www.release.tdnet.info robots.txt `User-agent: *` / `Disallow: /` (checked "
                 "2026-11-07); TDnet fetching is off. JP earnings dates: JPX schedule + Yahoo; JP statements: EDINET. "
                 "Rows ingested before 2026-11-07 stay (source_id tdnet).")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"


def parse_tdnet_page(html: bytes | str, day: date) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find(id="main-list-table")
    if table is None:
        return []
    out = []
    for tr in table.find_all("tr"):
        def cell(cls: str) -> Any:
            return tr.find("td", class_=re.compile(cls))
        t, c, n, ti, x, pl_ = (cell(k) for k in ("kjTime", "kjCode", "kjName", "kjTitle", "kjXbrl", "kjPlace"))
        if not (t and c and ti):
            continue
        a = ti.find("a")
        xa = x.find("a") if x else None
        pdf = a.get("href") if a else None
        out.append({"date": day, "time": t.get_text(strip=True), "code5": c.get_text(strip=True),
                    "name": unicodedata.normalize("NFKC", n.get_text(strip=True)) if n else None,
                    "title": unicodedata.normalize("NFKC", ti.get_text(" ", strip=True)),
                    "pdf": pdf, "xbrl": xa.get("href") if xa else None,
                    "exchange": pl_.get_text(strip=True) if pl_ else None,
                    "id": re.sub(r"\.pdf$", "", pdf) if pdf else None})
    return out


def classify_title(title: str) -> tuple[str, str, str | None]:
    """-> (form_type, form_class, fiscal_period)"""
    t = title
    if "決算短信" in t:
        if re.search(r"第\s*1\s*四半期", t):
            fp = "Q1"
        elif re.search(r"第\s*2\s*四半期|中間", t):
            fp = "H1"
        elif re.search(r"第\s*3\s*四半期", t):
            fp = "Q3"
        else:
            fp = "FY"
        return ("TANSHIN_AMEND" if "訂正" in t else "TANSHIN"), "current_report", fp
    if "業績予想" in t or "配当予想" in t:
        return "FORECAST_REVISION", "current_report", None
    if "自己株式" in t:
        return "BUYBACK", "current_report", None
    return "TDNET", "current_report", None


# ------------------------------------------------------------------------------------------------ tanshin XBRL
IX_NS = {"ix": "http://www.xbrl.org/2008/inlineXBRL", "xbrli": "http://www.xbrl.org/2003/instance",
         "xbrldi": "http://xbrl.org/2006/xbrldi"}
_IX = "{http://www.xbrl.org/2008/inlineXBRL}"
_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XSI_NIL = "{http://www.w3.org/2001/XMLSchema-instance}nil"
TAXONOMY_OF_PREFIX = {"jppfs_cor": "jppfs", "jpcrp_cor": "jpcrp", "jpigp_cor": "jpigp", "ifrs-full": "ifrs-full"}
UNIT_MAP = {"JPY": "JPY", "JPYPerShares": "JPY/shares", "Shares": "shares", "shares": "shares", "Pure": "pure",
            "pure": "pure", "USD": "USD", "USDPerShares": "USD/shares"}
FORECAST_MEMBERS = {"ForecastMember", "UpperMember", "LowerMember"}
DROP_MEMBERS = {"ResultMember", "ConsolidatedMember", "NonConsolidatedMember"}
_DASHES = ("-", "－", "—", "―", "‐", "−")
META_NAMES = ("tse-ed-t:FiscalYearEnd", "tse-ed-t:AccountingStandards", "tse-ed-t:DocumentName",
              "tse-ed-t:QuarterlyPeriod", "tse-ed-t:SecuritiesCode", "tse-ed-t:FilingDate")


def _local(qname: str | None) -> str:
    return (qname or "").split(":", 1)[-1]


def _ix_number(el: Any) -> float | None:
    """ix:nonFraction -> float (scale, sign, ixt:numdotdecimal / numcommadecimal / zerodash); None for nil/blank."""
    if el.get(_XSI_NIL) in ("true", "1"):
        return None
    txt = unicodedata.normalize("NFKC", "".join(el.itertext())).strip()
    fmt = (el.get("format") or "").lower()
    if "zerodash" in fmt:
        return 0.0
    if not txt or txt in _DASHES:
        return None
    if "numcommadecimal" in fmt:
        txt = txt.replace(".", "").replace(" ", "").replace(",", ".")
    else:
        txt = txt.replace(",", "").replace(" ", "")
    try:
        v = float(txt) * (10 ** int(el.get("scale") or 0))
    except ValueError:
        return None
    return -v if el.get("sign") == "-" else v


def _contexts(root: Any) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for c in root.iter(f"{_XBRLI}context"):
        start = end = inst = None
        members: list[tuple[str, str]] = []
        for x in c.iter():
            tag = x.tag if isinstance(x.tag, str) else ""
            if tag.endswith("}startDate"):
                start = to_date(x.text)
            elif tag.endswith("}endDate"):
                end = to_date(x.text)
            elif tag.endswith("}instant"):
                inst = to_date(x.text)
            elif tag.endswith("}explicitMember"):
                members.append((x.get("dimension") or "", (x.text or "").strip()))
        out[c.get("id")] = {"start": start, "end": end or inst, "instant": end is None, "members": members}
    return out


def _units(root: Any) -> dict[str, str]:
    out = {}
    for u in root.iter(f"{_XBRLI}unit"):
        ms = [_local((m.text or "").strip()) for m in u.iter(f"{_XBRLI}measure")]
        if len(ms) == 2:
            out[u.get("id")] = f"{ms[0]}/{ms[1].lower()}"
        elif ms:
            out[u.get("id")] = ms[0]
    return out


def _unit_of(ref: str | None, units: dict[str, str]) -> str | None:
    if not ref:
        return None
    if ref in UNIT_MAP:
        return UNIT_MAP[ref]
    u = units.get(ref)
    if u and (re.fullmatch(r"[A-Z]{3}(/shares)?", u) or u in ("shares", "pure")):
        return u
    return None


def _fp_of(start: date | None, end: date, title_fp: str | None) -> str | None:
    if start is None:
        return None
    months = round((end - start).days / 30.4375)
    return {12: "FY", 9: "9M", 6: "H1"}.get(months) or ("Q1" if months == 3 and title_fp == "Q1" else None)


def parse_tanshin_zip(content: bytes, *, source_entity_id: str, filing_id: str, knowledge_date: date,
                      title_fp: str | None = None, form_type: str = "TANSHIN",
                      source_id: str = "tdnet") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Tanshin XBRL zip -> (fundamentals_raw rows, meta). Contexts/units are pooled across all iXBRL files of the
    package (the statement attachments reference contexts defined in another attachment)."""
    from lxml import etree

    zf = zipfile.ZipFile(io.BytesIO(content))
    roots = []
    for n in zf.namelist():
        if not re.search(r"ixbrl\.html?$", n):
            continue
        r = etree.fromstring(zf.read(n), etree.XMLParser(huge_tree=True, recover=True))
        if r is not None:
            roots.append(r)
    ctx: dict[str, dict[str, Any]] = {}
    units: dict[str, str] = {}
    for r in roots:
        ctx.update(_contexts(r))
        units.update(_units(r))
    meta: dict[str, Any] = {"files": len(roots)}
    raw: list[tuple[str, dict[str, Any], float, str, int | None]] = []
    for r in roots:
        for el in r.iter(f"{_IX}nonNumeric"):
            nm = el.get("name") or ""
            if nm in META_NAMES:
                meta.setdefault(_local(nm), unicodedata.normalize("NFKC", "".join(el.itertext())).strip()[:120])
        for el in r.iter(f"{_IX}nonFraction"):
            nm, c = el.get("name"), ctx.get(el.get("contextRef") or "")
            if not nm or c is None or c["end"] is None or nm.startswith("jpdei_cor:"):
                continue
            v = _ix_number(el)
            unit = _unit_of(el.get("unitRef"), units)
            if v is None or unit is None:
                continue
            dec = el.get("decimals") or ""
            raw.append((nm, c, v, unit, int(dec) if dec.lstrip("-").isdigit() else None))
    fye = to_date(meta.get("FiscalYearEnd"))
    if fye is None:
        fye = next((c["end"] for cid, c in ctx.items() if cid.startswith("CurrentYear") and c["end"]), None)
    meta["fye"] = fye
    has_cons = any(all(_local(m) != "NonConsolidatedMember" for _, m in c["members"]) for _, c, *_ in raw)
    out = []
    for nm, c, v, unit, dec in raw:
        prefix = nm.split(":", 1)[0]
        taxonomy = TAXONOMY_OF_PREFIX.get(prefix, "other")
        concept = _local(nm) if taxonomy == "ifrs-full" else nm
        mem = [_local(m) for _, m in c["members"]]
        nonc = "NonConsolidatedMember" in mem
        forecast = next((m for m in mem if m in FORECAST_MEMBERS), None)
        dims = {_local(d): _local(m) for d, m in c["members"] if _local(m) not in DROP_MEMBERS | FORECAST_MEMBERS}
        if forecast:
            dims["ResultForecast"] = forecast
        if nonc and has_cons:
            dims["Consolidation"] = "NonConsolidated"
        start = None if c["instant"] else c["start"]
        end = c["end"]
        row = fact_row(source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id,
                       form_type=form_type, taxonomy=taxonomy, concept=concept, label=None, period_start=start,
                       period_end=end, value=v, unit=unit, knowledge_date=knowledge_date,
                       fiscal_year=fiscal_year_for(end, fye.month if fye else None),
                       fiscal_period=_fp_of(start, end, title_fp), decimals=dec, dims=dims or None,
                       consolidated=not nonc)
        row["is_forecast"] = bool(forecast)
        out.append(row)
    meta["n_facts"] = len(out)
    meta["n_forecast"] = sum(1 for x in out if x["is_forecast"])
    return out, meta


@register_adapter
class TdnetAdapter(SourceAdapter):
    source_id = "tdnet"
    regions = ("JP",)
    produces = ("filings_index", "fundamentals_raw")
    rate_limit = RateLimit(1.0, 2)

    def __init__(self, *args: Any, all_listed: bool = True, xbrl_scope: str = "all", max_xbrl: int = 5000, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = BROWSER_UA
        self.all_listed = str(all_listed).lower() not in ("0", "false")
        self.xbrl_scope = str(xbrl_scope).lower()  # all | universe | none
        self.max_xbrl = int(max_xbrl)

    def _universe_codes(self) -> set[str]:
        return {str(t)[:4] for t in seed_universe(self.settings, countries=["JP"]).get_column("ticker").to_list()}

    def xbrl_path(self, r: dict[str, Any]):
        return self.settings.raw_dir / self.source_id / "xbrl" / f"dt={r['date'].isoformat()}" / f"{r['id']}.zip"

    def land_xbrl(self, r: dict[str, Any]) -> str:
        """Download the tanshin XBRL zip once (raw/tdnet/xbrl/dt=<list date>/<id>.zip); returns the path."""
        path = self.xbrl_path(r)
        if path.exists() and path.stat().st_size > 0:
            return str(path)
        resp = self.http.get(BASE + r["xbrl"], cache_ttl=0)
        if not resp.content.startswith(b"PK"):
            raise ValueError(f"not a zip: {resp.content[:80]!r}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".zip.tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(path)
        return str(path)

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def day_rows(self, d: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        ttl = 1800 if (datetime.now(UTC).date() - d).days <= 2 else 7 * 86400
        for page in range(1, 40):
            try:
                r = self.http.get(f"{BASE}I_list_{page:03d}_{d:%Y%m%d}.html", cache_ttl=ttl)
            except HttpError as e:
                if e.status == 404:
                    break
                raise
            got = parse_tdnet_page(r.content, d)
            if page == 1:
                self.save_raw("lists", f"I_list_{d:%Y%m%d}_p{page:03d}", r.content)
            elif got:
                self.save_raw("lists", f"I_list_{d:%Y%m%d}_p{page:03d}", r.content)
            rows += got
            if len(got) < 100:
                break
        return rows

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        if DISABLED_REASON:  # A9 2026-11-07: robots.txt disallows the host -> never request it
            res.meta.update(rows=[], disabled=DISABLED_REASON, note=DISABLED_NOTE)
            return res
        state = load_state(self.source_id, self.settings)
        today = datetime.now(UTC).date() + timedelta(days=1)  # JST is ahead of UTC
        floor = today - timedelta(days=31)
        since_d = to_date(since) or (to_date(state["watermark"]) - timedelta(days=2) if state.get("watermark") else floor)
        since_d = max(since_d, floor)
        if entities:
            uni = {e[:4] for e in entities}
        elif self.all_listed:
            uni = None
        else:
            uni = set(seed_universe(self.settings, countries=["JP"]).get_column("ticker").to_list())
        rows: list[dict[str, Any]] = []
        d = since_d
        while d <= today:
            got = safe_call(lambda d=d: self.day_rows(d), res, f"day:{d}", self.log) or []
            rows += [r for r in got if uni is None or r["code5"][:4] in uni]
            d += timedelta(days=1)
        res.meta["rows"] = rows
        # tanshin XBRL packages (31-day retention: land them all)
        if self.xbrl_scope != "none":
            ucodes = self._universe_codes() if self.xbrl_scope == "universe" else None
            n = 0
            for r in rows:
                if not (r.get("xbrl") and r.get("id") and classify_title(r["title"])[0].startswith("TANSHIN")):
                    continue
                if ucodes is not None and r["code5"][:4] not in ucodes:
                    continue
                if n >= self.max_xbrl:
                    res.meta["xbrl_truncated"] = True
                    break
                n += 1
                p = safe_call(lambda r=r: self.land_xbrl(r), res, f"xbrl:{r['id']}", self.log)
                if p:
                    r["_xbrl_path"] = p
            res.meta["n_xbrl"] = sum(1 for r in rows if r.get("_xbrl_path"))
        if not entities:
            res.watermark = (today - timedelta(days=1)).isoformat()
            state["watermark"] = res.watermark
            save_state(self.source_id, state, self.settings)
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        out = []
        facts: list[dict[str, Any]] = []
        for r in fetched.meta.get("rows", []):
            if not r.get("id"):
                continue
            ftype, fclass, fp = classify_title(r["title"])
            code = r["code5"][:4] if len(r["code5"]) == 5 and r["code5"].endswith("0") else r["code5"]
            if r.get("_xbrl_path"):
                try:
                    rows, _ = parse_tanshin_zip(Path(r["_xbrl_path"]).read_bytes(), source_entity_id=code,
                                                filing_id=f"{self.source_id}:{r['id']}", knowledge_date=r["date"],
                                                title_fp=fp, form_type=ftype, source_id=self.source_id)
                    facts += rows
                except Exception as e:  # noqa: BLE001
                    fetched.entities_failed[f"parse:{r['id']}"] = f"{type(e).__name__}: {e}"[:300]
            out.append(filing_row(
                source_id=self.source_id, native_key=r["id"], source_entity_id=code, form_type=ftype, form_class=fclass,
                knowledge_date=r["date"], knowledge_ts=jst_to_utc(r["date"], r["time"]), fiscal_period=fp,
                is_amendment="訂正" in r["title"], language="ja", url=BASE + r["pdf"], has_xbrl=bool(r.get("xbrl")),
                items=[r["title"][:300], f"xbrl={BASE + r['xbrl']}" if r.get("xbrl") else "xbrl=", f"exchange={r.get('exchange') or ''}"]))
        return {"filings_index": frame(out, "filings_index"), "fundamentals_raw": frame(facts, "fundamentals_raw")}


def run(ctx: Any) -> Any:
    """Job tdnet_disclosures: disabled (A9 2026-11-07, robots.txt `Disallow: /`) -> skipped, no request."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    if DISABLED_REASON:
        return JobResult(status="skipped", message=DISABLED_NOTE, retryable=False)
    return run_adapter_job(TdnetAdapter, ctx)  # pragma: no cover - kept for the record; the source is disallowed
