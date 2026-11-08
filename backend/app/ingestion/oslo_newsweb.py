"""Norway: Oslo Børs NewsWeb API (the Norwegian OAM) -> ESEF annual reports attached to category 1001 releases.

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (NO), verified 2026-10-11 (GET works as well as the site's POST, with
Origin/Referer https://newsweb.oslobors.no):
  GET https://api3.oslo.oslobors.no/v1/newsreader/list?issuer=<SYMBOL>&fromDate=2017-01-01&toDate=<today>&category=1001
      -> data.messages[] {messageId, title, publishedTime (UTC, ms), numbAttachments, correctionForMessageId,
         issuerSign, category[{id: 1001, category_en: "ANNUAL FINANCIAL REPORT"}]}
  GET …/message?messageId=<id>   -> data.message {publishedTime, attachments[{id, name}], ...}
  GET …/attachment?messageId=<id>&attachmentId=<id>   -> the file (ESEF zip, e.g. dnbbankasa-2025-12-31-1-nb.zip)
Undocumented public API (ToS not reviewed); no key. source_entity_id = LEI; issuer symbol = the XOSL ticker.
English and Norwegian releases (and corrections) are separate messages: one ESEF package per publication year is
kept, the earliest-published message carrying one (PIT). Message bodies are immutable -> cached for 30 days.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

BASE = "https://api3.oslo.oslobors.no/v1/newsreader"
HEADERS = {"Origin": "https://newsweb.oslobors.no", "Referer": "https://newsweb.oslobors.no/",
           "Accept": "application/json"}
CATEGORY_AFR = 1001

#: LEI -> NewsWeb issuer symbol (verified 2026-10-11); fallback = XOSL ticker from the universe.
ISSUER_SIGNS: dict[str, str] = {
    "549300GKFG0RYRRQ1414": "DNB",
    "OW6OFBNCKXC4US5C7523": "EQNR",
}

#: period-end date in a package name (month-end day): 2025-12-31, 2025_12_31, 20251231
_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(2[89]|3[01])(?!\d)")
_PKG_EXT = (".zip", ".xbri")
_LANG_RE = re.compile(r"[-_ ](en|nb|no|nn)(?:[-_ .]|$)", re.I)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)


def period_hint(name: str) -> str | None:
    m = _DATE_RE.search(name or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def pick_attachment(attachments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """ESEF package among a message's attachments (.zip/.xbri, period-dated name preferred; else bare .xhtml)."""
    atts = [a for a in attachments or [] if a.get("id") is not None and a.get("name")]
    pk = [a for a in atts if a["name"].lower().endswith(_PKG_EXT)]
    if pk:
        return next((a for a in pk if period_hint(a["name"])), pk[0])
    xh = [a for a in atts if a["name"].lower().endswith(".xhtml")]
    return xh[0] if xh else None


def to_filing(msg: dict[str, Any], att: dict[str, Any]) -> dict[str, Any]:
    mid = msg.get("messageId") or msg.get("id")
    name = att["name"].lower()
    kts = parse_ts(msg.get("publishedTime"))
    m = _LANG_RE.search(att["name"])
    lang = {"en": "en", "nn": "nn"}.get(m.group(1).lower(), "nb") if m else None
    return {
        "native_key": f"{mid}-{att['id']}", "knowledge_ts": kts.isoformat() if kts else None,
        "doc_url": f"{BASE}/attachment?messageId={mid}&attachmentId={att['id']}",
        "suffix": ".xbri" if name.endswith(".xbri") else ".xhtml" if name.endswith(".xhtml") else ".zip",
        "form_type": "AFR", "form_class": "annual", "fiscal_period": "FY", "period_end": None,
        "url": f"https://newsweb.oslobors.no/message/{mid}", "language": lang,
        "is_amendment": bool(msg.get("correctionForMessageId")), "title": msg.get("title"), "file_name": att["name"],
        "period_hint": period_hint(att["name"]),
    }


@register_adapter
class OsloNewswebAdapter(PackageFilingAdapter):
    source_id = "oslo_newsweb"
    regions = ("EU",)
    countries = ("NO",)
    base_url = BASE
    rate_limit = RateLimit(1.0, 1)
    min_list_year: int = 2019

    def _get(self, path: str, params: dict[str, Any], ttl: int) -> dict[str, Any]:
        return self.http.get(f"{BASE}/{path}", params=params, headers=HEADERS, cache_ttl=ttl).json()

    def issuer_sign(self, issuer: dict[str, Any]) -> str | None:
        for lei in issuer.get("leis") or [issuer.get("lei")]:
            if lei in ISSUER_SIGNS:
                return ISSUER_SIGNS[lei]
        return next((t.split("@")[0] for t in issuer.get("tickers") or [] if t.endswith("@XOSL")), None)

    def messages(self, sign: str) -> list[dict[str, Any]]:
        j = self._get("list", {"issuer": sign, "fromDate": f"{self.min_list_year}-01-01",
                               "toDate": date.today().isoformat(), "category": CATEGORY_AFR}, 6 * 3600)
        return (j.get("data") or {}).get("messages") or []

    def message(self, mid: Any) -> dict[str, Any]:
        return (self._get("message", {"messageId": mid}, 30 * 86400).get("data") or {}).get("message") or {}

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        sign = self.issuer_sign(issuer)
        if not sign:
            raise LookupError(f"no NewsWeb issuer symbol for {issuer['name']}")
        msgs = [m for m in self.messages(sign) if (m.get("issuerSign") or sign) == sign and m.get("numbAttachments")]
        msgs.sort(key=lambda m: m.get("publishedTime") or "")
        out: list[dict[str, Any]] = []
        seen_years: set[str] = set()
        seen_periods: set[str] = set()
        for m in msgs:
            year = (m.get("publishedTime") or "")[:4]
            if not year or year in seen_years:
                continue
            full = self.message(m.get("messageId") or m.get("id"))
            att = pick_attachment(full.get("attachments") or [])
            if att is None:
                continue
            f = to_filing({**m, **full}, att)
            if f["period_hint"] and f["period_hint"] in seen_periods:
                continue
            seen_years.add(year)
            if f["period_hint"]:
                seen_periods.add(f["period_hint"])
            out.append(f)
        return out

    def download(self, filing: dict[str, Any]) -> bytes:
        return self.http.get(filing["doc_url"], headers=HEADERS, cache_ttl=0).content


def run(ctx: Any) -> Any:
    return run_adapter_job(OsloNewswebAdapter, ctx)
