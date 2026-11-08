"""Nordics (SE / DK / FI): Nasdaq Nordic company-news API (the Nasdaq Stockholm / Copenhagen / Helsinki OAM feed)
-> ESEF annual reports attached to "Annual Financial Report" releases.

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (SE, DK/FI), verified 2026-10-11.
  GET https://api.news.eu.nasdaq.com/news/query.action?type=json&showAttachments=true&company=<name>
      &globalGroup=exchangeNotice&globalName=NordicAllMarkets&cnsCategory=<category>&limit=200&start=<offset>
  -> {"count": n, "results": {"item": [{disclosureId, company, cnsCategory, headline, language, published
      ("2026-03-20 07:30:00 +0000" = release time, knowledge_ts), messageUrl, attachment: [{fileName, mimetype,
      attachmentUrl}]}]}}. `limit` is capped at 200 (page with `start`); `company` must be the API's exact company name
  (e.g. "Volvo, AB", "Hennes & Mauritz AB, H & M") -> verified map below, fallback `freeText` search.
  ESEF zips are attached to cnsCategory "Annual Financial Report"; some issuers (Investor, Volvo FY2021-23) file them
  under "Other information disclosed according to the rules of the Exchange" (accepted only when the attachment name
  carries a period date, e.g. abvolvo-2023-12-31-en.zip).
Undocumented public API (ToS not reviewed); no key. source_entity_id = LEI. Language versions (en/sv/fi/da) are
separate releases, often carrying the same package: one filing per publication year is kept, the earliest release
(PIT), English preferred on ties. Re-publications of the same period >= 1 day later (corrections, e.g. Nordea FY2021
re-filed in May 2022, FY2024 "-1-" in April 2025) are kept as amendments (form_type AFR/A) with their own knowledge_ts.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import UTC, datetime
from typing import Any

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

API = "https://api.news.eu.nasdaq.com/news/query.action"
AFR = "Annual Financial Report"
OTHER = "Other information disclosed according to the rules of the Exchange"
CATEGORIES = (AFR, OTHER)
PAGE = 200

#: LEI -> exact Nasdaq news `company` name (verified 2026-10-11 against the API).
COMPANY_NAMES: dict[str, str] = {
    "213800T8PC8Q4FYJZR07": "Atlas Copco AB",
    "529900O5RR7R39FRDM42": "Hennes & Mauritz AB, H & M",
    "549300VEBQPHRZBKUX38": "Investor AB",
    "549300HGV012CNC8JD22": "Volvo, AB",
    "549300W9JLPW15XIFM52": "Ericsson, Telefonab. L M",
    "549300D2K6PKKKXVNN73": "A.P. Møller - Mærsk A/S",
    "529900X41C0BSLK67H70": "DSV A/S",
    "549300DAQ1CVT6CXN342": "Novo Nordisk A/S",
    "2138001CNF45JP5XZK38": "KONE Oyj",
    "549300A0JPRWG1KI7U06": "Nokia",
    "529900ODI3047E2LIV03": "Nordea Bank Abp",
}

_PKG_EXT = (".zip", ".xbri")
#: period-end date in a package name (month-end day): 2025-12-31, 2025_12_31, 20251231
_DATE_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(2[89]|3[01])(?!\d)")
#: annual-report wording (en/sv/da/fi/no) required for packages filed outside the "Annual Financial Report" category
_ANNUAL_RE = re.compile(r"annual|årsred|årsrap|aarsrap|vuosikerto|tilinpäätös|esef", re.I)


def parse_ts(s: str | None) -> datetime | None:
    """'2026-03-20 07:30:00 +0000' (or without offset -> assumed UTC; a late stamp is the PIT-safe side)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def period_hint(name: str) -> str | None:
    """Period end spelled in an ESEF package name ('atla-2025-12-31-1-en.zip' -> '2025-12-31')."""
    m = _DATE_RE.search(name or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def pick_attachment(item: dict[str, Any]) -> dict[str, Any] | None:
    """The ESEF package of a release: .zip/.xbri preferred, else a bare .xhtml (viewer pages excluded)."""
    atts = [a for a in item.get("attachment") or [] if a.get("attachmentUrl") and a.get("fileName")]
    pk = [a for a in atts if a["fileName"].lower().endswith(_PKG_EXT)]
    if item.get("cnsCategory") != AFR:  # other categories: only annual-report releases with an ESEF-named package
        if not _ANNUAL_RE.search(item.get("headline") or ""):
            return None
        pk = [a for a in pk if period_hint(a["fileName"])]
        return pk[0] if pk else None
    if pk:
        return next((a for a in pk if period_hint(a["fileName"])), pk[0])
    xh = [a for a in atts if a["fileName"].lower().endswith(".xhtml") and "viewer" not in a["fileName"].lower()]
    return xh[0] if xh else None


def fix_zip_separators(payload: bytes) -> bytes:
    """Re-pack a zip whose entry names use '\' separators (e.g. KONE FY2020: Arelle rejects them with
    tpe:invalidArchiveFormat) with '/' separators; any other payload is returned unchanged."""
    try:
        zin = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        return payload
    with zin:
        if not any("\\" in i.orig_filename for i in zin.infolist()):  # (.filename is normalised on Windows)
            return payload
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if not info.is_dir():
                    zout.writestr(info.orig_filename.replace("\\", "/"), zin.read(info))
        return buf.getvalue()


def select_filings(items: list[dict[str, Any]], min_year: int = 2019) -> list[dict[str, Any]]:
    """Release items -> filing dicts.

    One original annual ESEF package per publication year: the earliest release (PIT), English preferred on ties
    (language versions usually carry the same package). A later re-publication of the same period (package name
    with the same period date, published >= 1 day after the original, e.g. Nordea FY2021 re-filed 2022-05-05 after a
    broken schemaRef, Nordea FY2024 "-1-" 2025-04-02) is kept as an amendment with its own knowledge_ts."""
    cands = []
    for it in items:
        att = pick_attachment(it)
        kts = parse_ts(it.get("published") or it.get("releaseTime"))
        if att is None or kts is None or kts.year < min_year:
            continue
        cands.append((kts, it.get("language") != "en", it, att))
    out: list[dict[str, Any]] = []
    seen_years: set[int] = set()
    last_of_period: dict[str, datetime] = {}
    for kts, _, it, att in sorted(cands, key=lambda c: (c[0], c[1], str(c[2].get("disclosureId")))):
        ph = period_hint(att["fileName"])
        if ph and ph in last_of_period:
            if (kts - last_of_period[ph]).total_seconds() < 86400:
                continue  # same-day language version / duplicate
            amend = True
        elif kts.year in seen_years:
            continue
        else:
            amend = False
            seen_years.add(kts.year)
        if ph:
            last_of_period[ph] = kts
        name = att["fileName"].lower()
        out.append({
            "native_key": str(it.get("disclosureId")), "knowledge_ts": kts.isoformat(), "doc_url": att["attachmentUrl"],
            "suffix": ".xbri" if name.endswith(".xbri") else ".xhtml" if name.endswith(".xhtml") else ".zip",
            "form_type": "AFR/A" if amend else "AFR", "form_class": "annual", "fiscal_period": "FY",
            "period_end": ph if amend else None,
            "url": it.get("messageUrl") or att["attachmentUrl"], "language": it.get("language"),
            "is_amendment": amend, "title": it.get("headline"), "file_name": att["fileName"], "period_hint": ph,
            "category": it.get("cnsCategory"), "company": it.get("company"),
        })
    return out


@register_adapter
class NasdaqNordicAdapter(PackageFilingAdapter):
    source_id = "nasdaq_nordic"
    regions = ("EU",)
    countries = ("SE", "DK", "FI")
    base_url = "https://api.news.eu.nasdaq.com"
    rate_limit = RateLimit(1.0, 1)

    def _query(self, **params: Any) -> dict[str, Any]:
        p = {"type": "json", "showAttachments": "true", "globalGroup": "exchangeNotice",
             "globalName": "NordicAllMarkets", "limit": PAGE, **params}
        return self.http.get(API, params=p, cache_ttl=6 * 3600).json()

    def company_name(self, issuer: dict[str, Any]) -> str | None:
        for lei in issuer.get("leis") or [issuer.get("lei")]:
            if lei in COMPANY_NAMES:
                return COMPANY_NAMES[lei]
        # fallback: free-text search, the most frequent company whose name contains the issuer's first word
        word = re.sub(r"[^\w&]+", " ", issuer["name"]).split()[0].lower()
        items = (self._query(freeText=issuer["name"].split()[0], limit=100).get("results") or {}).get("item") or []
        counts: dict[str, int] = {}
        for it in items:
            c = it.get("company") or ""
            if word in c.lower():
                counts[c] = counts.get(c, 0) + 1
        return max(counts, key=counts.__getitem__) if counts else None

    def releases(self, company: str, category: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        start = 0
        while True:
            j = self._query(company=company, cnsCategory=category, start=start)
            items = (j.get("results") or {}).get("item") or []
            out += items
            last = parse_ts(items[-1].get("published")) if items else None
            if len(items) < PAGE or start + PAGE >= int(j.get("count") or 0) or (last and last.year < self.min_year):
                return out
            start += PAGE

    def to_doc(self, filing: dict[str, Any], payload: bytes) -> dict[str, Any]:
        if (filing.get("suffix") or ".zip") in (".zip", ".xbri"):
            payload = fix_zip_separators(payload)
        return super().to_doc(filing, payload)

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        company = self.company_name(issuer)
        if not company:
            raise LookupError(f"no Nasdaq news company name for {issuer['name']}")
        items: list[dict[str, Any]] = []
        for cat in CATEGORIES:
            items += self.releases(company, cat)
        return select_filings(items)


def run(ctx: Any) -> Any:
    return run_adapter_job(NasdaqNordicAdapter, ctx)
