"""Belgium: FSMA STORI (the Belgian OAM) JSON API -> ESEF annual reports (+ half-year PDF metadata).

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (BE), verified live 2026-10-11.
  POST https://webapi.fsma.be/api/v1/en/stori/result
       {"isinCode": "<ISIN>", "publicationStart": "2017-01-01", "startRowIndex": 0, "pageSize": 2000}
       -> {"resultCount": n, "storiResultItems": [{companyName, lei, reportingTopicName ("Annual financial report" |
           "Half-yearly financial report" | ...), datePublication (Brussels local, publication by the issuer),
           dateReceived (deposit at FSMA), mainDocuments[{fileDataId, language, originalFileName, fileType, size}],
           attachments[...], isinCodes[...]}]}
       NB: without a sort the server's paging is unstable (duplicates across pages) -> one big page per issuer.
  GET  https://webapi.fsma.be/api/v1/en/stori/download?fileDataId=<id>   (ESEF .zip/.xbri, PDF, xhtml)
Public; keys: none. source_entity_id = LEI. knowledge_ts = datePublication (Europe/Brussels -> UTC; the moment the
regulated information was public); dateReceived is kept in the filing dict. Earliest publication per period is kept.
ESEF packages from FY2021 (filed 2022); earlier annual reports / all half-year reports are PDF -> metadata rows.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

API = "https://webapi.fsma.be/api/v1/en/stori"
RESULT = f"{API}/result"
DOWNLOAD = f"{API}/download"
_BRU = ZoneInfo("Europe/Brussels")
_TYPES = {"Annual financial report": ("AFR", "annual", "FY"),
          "Half-yearly financial report": ("HFR", "semiannual", "H1")}
_LANG_PREF = {"en": 0, "fr": 1, "nl": 2}


def parse_ts(s: str | None) -> datetime | None:
    """'2026-02-26T07:00:00' / '2026-07-30T07:00:28.43' (Brussels local) -> UTC."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=_BRU)).astimezone(UTC)


def pick_package(item: dict[str, Any]) -> dict[str, Any] | None:
    """ESEF report package among mainDocuments (zip/xbri), English first."""
    docs = [d for d in item.get("mainDocuments") or []
            if (d.get("fileType") or "").lower() in ("zip", "xbri")
            or (d.get("originalFileName") or "").lower().endswith((".zip", ".xbri"))]
    if not docs:
        return None
    return sorted(docs, key=lambda d: _LANG_PREF.get((d.get("language") or "").lower(), 9))[0]


def select_filings(items: list[dict[str, Any]], lei: str | None = None, min_year: int = 2017) -> list[dict[str, Any]]:
    pkgs: dict[tuple, dict[str, Any]] = {}
    meta: dict[tuple, dict[str, Any]] = {}
    seen: set[str] = set()
    for it in sorted(items, key=lambda x: x.get("datePublication") or ""):
        cls = _TYPES.get(it.get("reportingTopicName") or "")
        kts = parse_ts(it.get("datePublication")) or parse_ts(it.get("dateReceived"))
        if cls is None or kts is None or kts.year < min_year:
            continue
        if lei and it.get("lei") and it["lei"] != lei:
            continue
        pkg = pick_package(it)
        docs = it.get("mainDocuments") or []
        ref = pkg or (docs[0] if docs else None)
        if ref is None or ref["fileDataId"] in seen:
            continue
        seen.add(ref["fileDataId"])
        fn = ref.get("originalFileName") or ref.get("title") or ""
        m = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
        # annual reports are published in Q1 of the year after FY end; key on the publication year
        key = (cls[1], kts.year)
        bucket = pkgs if pkg else meta
        if key in bucket:
            continue
        suffix = "." + fn.lower().rsplit(".", 1)[-1] if pkg and "." in fn else ".zip"
        bucket[key] = {
            "native_key": ref["fileDataId"], "knowledge_ts": kts.isoformat(),
            "doc_url": f"{DOWNLOAD}?fileDataId={ref['fileDataId']}" if pkg else None,
            "suffix": suffix if suffix in (".zip", ".xbri") else ".zip",
            "form_type": cls[0], "form_class": cls[1], "fiscal_period": cls[2],
            "period_end": m.group(1) if (m and pkg) else None,
            "url": f"{DOWNLOAD}?fileDataId={ref['fileDataId']}", "language": (ref.get("language") or None),
            "is_amendment": False, "title": fn, "received": it.get("dateReceived"),
            "company": it.get("companyName")}
    out = list(pkgs.values()) + [f for k, f in meta.items() if k not in pkgs]
    return sorted(out, key=lambda f: f["knowledge_ts"])


@register_adapter
class FsmaBeAdapter(PackageFilingAdapter):
    source_id = "fsma_be"
    regions = ("EU",)
    countries = ("BE",)
    base_url = "https://webapi.fsma.be"
    rate_limit = RateLimit(1.0, 1)
    _hdr = {"Origin": "https://www.fsma.be", "Referer": "https://www.fsma.be/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/129.0 Safari/537.36"}

    def _search(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        q = {**body, "publicationStart": f"{self.min_year}-01-01", "startRowIndex": 0, "pageSize": 2000}
        j = self.http.post(RESULT, json_body=q, headers=self._hdr).json()
        items = j.get("storiResultItems") or []
        if len(items) < int(j.get("resultCount") or 0):
            self.log.warning("fsma_be %s: %d of %d items returned", body, len(items), j.get("resultCount"))
        return items

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        items = self._search({"isinCode": issuer["isin"]}) if issuer.get("isin") else []
        if not items:
            items = self._search({"companyName": issuer["name"]})
        return select_filings(items, issuer.get("lei"), self.min_year)

    def download(self, filing: dict[str, Any]) -> bytes:
        payload = self.http.get(filing["doc_url"], cache_ttl=0, headers=self._hdr).content
        if payload[:2] != b"PK":
            raise ValueError(f"STORI download is not a zip ({payload[:60]!r})")
        return payload


def run(ctx: Any) -> Any:
    return run_adapter_job(FsmaBeAdapter, ctx)
