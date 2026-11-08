"""Netherlands: AFM register "Financiële verslaggeving" (the Dutch OAM) -> ESEF annual reports (+ PDF metadata).

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (NL), verified live 2026-10-11.
  Export (XML, ~4 MB, all ~9.7k rows since 2007, no LEI column):
     https://www.afm.nl/export.aspx?type=e8825b05-4004-4301-b736-651e8c61053d&format=xml
     <vermelding><id>A2510-03613</id><datum>2/11/2026 12:35:22 PM</datum>  (Amsterdam local time, US format)
                 <uitgevende-instelling>Heineken N.V.</uitgevende-instelling><boekjaar>2025</boekjaar>
                 <filename>heia-2025-12-31-en (13)-A2510-03613.xbri</filename>
                 <objecttype_eng>Annual financial report | Half-yearly financial report | ...</objecttype_eng>
  Package download is two-step: details page .../financiele-verslaggeving/details?id=<id> carries an encrypted
  /downloadregisterfile.aspx?type=financiele-verslaggeving&enc=... link (session-free, but needs a browser UA).
Public register; keys: none. source_entity_id = LEI. Issuers are matched by register name (normalized, plus an
explicit map) or by the LEI embedded in the ESEF file name. knowledge_ts = register `datum` (Europe/Amsterdam -> UTC).
PIT: the earliest ESEF deposit per (report class, boekjaar) is kept; PDF-only periods are metadata rows.
"""

from __future__ import annotations

import html as _html
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from lxml import etree

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

EXPORT = "https://www.afm.nl/export.aspx?type=e8825b05-4004-4301-b736-651e8c61053d&format=xml"
DETAIL = "https://www.afm.nl/nl-nl/sector/registers/meldingenregisters/financiele-verslaggeving/details"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
_AMS = ZoneInfo("Europe/Amsterdam")
_TYPES = {"Annual financial report": ("AFR", "annual", "FY"),
          "Half-yearly financial report": ("HFR", "semiannual", "H1")}
_PKG = (".zip", ".xbri", ".xhtml", ".html", ".htm")

#: LEI -> exact AFM register names (checked against the export 2026-10-11). Avoids e.g. "Heineken Holding N.V." or
#: "Ferrari Group PLC" (different legal entities) matching the universe issuer.
AFM_NAMES: dict[str, tuple[str, ...]] = {
    "635400Z5LQ5F9OLVT688": ("Prosus N.V.",),
    "724500K5PTPSST86UQ23": ("Heineken N.V.",),
    "549300RIVY5EX8RCON76": ("Ferrari N.V.",),
    "MINO79WLOO247M1IL051": ("Airbus SE", "Airbus Group SE", "Airbus Group N.V."),
    "724500Y6DUVHQD6OXN27": ("ASML Holding N.V.",),
    "7245001I22ND6ZFHX623": ("ASM International N.V.",),
    "724500973ODKK3IFQ447": ("Adyen N.V.",),
    "549300NYKK9MWM7GGW15": ("ING Groep N.V.",),
    "724500C9GNBV20UYRX36": ("Koninklijke Ahold Delhaize N.V.",),
    "H1FJE8H61JGM1JSGM897": ("Koninklijke Philips N.V.",),
    "549300LKT9PW7ZIBDF31": ("Stellantis N.V.",),
    "724500TEM53I0U077B74": ("Wolters Kluwer N.V.",),
}
_SUFFIX = re.compile(r"\b(n\.?v\.?|s\.?e\.?|plc|b\.?v\.?|s\.?a\.?|ag|holding|groep|group|koninklijke)\b|[.,()]")


def norm_name(s: str | None) -> str:
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", (s or "").lower())).strip()


def parse_datum(s: str | None) -> datetime | None:
    """'2/11/2026 12:35:22 PM' (Amsterdam local) -> UTC datetime."""
    if not s:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=_AMS).astimezone(UTC)
        except ValueError:
            continue
    return None


def parse_export(content: bytes) -> list[dict[str, Any]]:
    root = etree.fromstring(content.lstrip(b"\xef\xbb\xbf"), parser=etree.XMLParser(huge_tree=True, recover=True))
    out = []
    for v in root.iter("vermelding"):
        rid = v.findtext("id")
        if not rid:
            continue  # nested <vermelding> summary text node
        out.append({"id": rid.strip(), "datum": (v.findtext("datum") or "").strip(),
                    "issuer": (v.findtext("uitgevende-instelling") or "").strip(),
                    "boekjaar": (v.findtext("boekjaar") or "").strip(),
                    "filename": (v.findtext("filename") or "").strip(),
                    "type": (v.findtext("objecttype_eng") or "").strip()})
    return out


def match_rows(rows: list[dict[str, Any]], issuer: dict[str, Any]) -> list[dict[str, Any]]:
    leis = [x for x in (issuer.get("leis") or [issuer.get("lei")]) if x]
    names = {n for lei in leis for n in AFM_NAMES.get(lei, ())}
    want = norm_name(issuer.get("name"))
    out = []
    for r in rows:
        if names:
            ok = r["issuer"] in names
        else:
            ok = bool(want) and norm_name(r["issuer"]) == want
        ok = ok or any(lei in r["filename"].upper() for lei in leis)
        if ok:
            out.append(r)
    return out


def select_filings(rows: list[dict[str, Any]], min_year: int = 2017) -> list[dict[str, Any]]:
    """Register rows of one issuer -> filing dicts: earliest ESEF package per (class, boekjaar); PDF-only periods
    become metadata rows (doc_url None)."""
    pkgs: dict[tuple, dict[str, Any]] = {}
    pdfs: dict[tuple, dict[str, Any]] = {}
    for r in sorted(rows, key=lambda x: parse_datum(x["datum"]) or datetime.max.replace(tzinfo=UTC)):
        cls = _TYPES.get(r["type"])
        kts = parse_datum(r["datum"])
        if cls is None or kts is None or kts.year < min_year:
            continue
        fn = r["filename"].lower()
        is_pkg = fn.endswith(_PKG)
        key = (cls[1], r["boekjaar"] or str(kts.year))
        bucket = pkgs if is_pkg else pdfs
        if key in bucket:
            continue
        m = re.search(r"(\d{4}-\d{2}-\d{2})", r["filename"])
        suffix = "." + fn.rsplit(".", 1)[-1] if is_pkg else None
        bucket[key] = {
            "native_key": r["id"], "knowledge_ts": kts.isoformat(), "doc_url": f"{DETAIL}?id={r['id']}" if is_pkg else None,
            "suffix": ".xhtml" if suffix in (".html", ".htm") else suffix, "form_type": cls[0], "form_class": cls[1],
            "fiscal_period": cls[2], "period_end": m.group(1) if (m and is_pkg) else None,
            "url": f"{DETAIL}?id={r['id']}", "language": "en" if re.search(r"[-_ ]en\b", fn) else None,
            "is_amendment": False, "title": r["filename"], "boekjaar": r["boekjaar"]}
    out = list(pkgs.values()) + [f for k, f in pdfs.items() if k not in pkgs]
    return sorted(out, key=lambda f: f["knowledge_ts"])


_LINK = re.compile(r'href="(/?downloadregisterfile\.aspx\?[^"]+)"', re.I)


def download_link(detail_html: str) -> str | None:
    m = _LINK.search(detail_html)
    if not m:
        return None
    return "https://www.afm.nl/" + _html.unescape(m.group(1)).lstrip("/")


@register_adapter
class AfmNlAdapter(PackageFilingAdapter):
    source_id = "afm_nl"
    regions = ("EU",)
    countries = ("NL",)
    base_url = "https://www.afm.nl"
    rate_limit = RateLimit(1.0, 1)

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self._export: list[dict[str, Any]] | None = None

    def _hdr(self) -> dict[str, str]:
        return {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9,nl;q=0.8"}

    def export_rows(self) -> list[dict[str, Any]]:
        if self._export is None:
            r = self.http.get(EXPORT, cache_ttl=6 * 3600, headers=self._hdr())
            self._export = parse_export(r.content)
            if not self._export:
                raise ValueError("AFM financial-reporting export empty / schema change")
        return self._export

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        return select_filings(match_rows(self.export_rows(), issuer), self.min_year)

    def download(self, filing: dict[str, Any]) -> bytes:
        det = self.http.get(filing["doc_url"], cache_ttl=24 * 3600, headers=self._hdr())
        link = download_link(det.content.decode("utf-8", "replace"))
        if not link:
            raise ValueError(f"AFM details page without download link: {filing['native_key']}")
        payload = self.http.get(link, cache_ttl=0, headers=self._hdr()).content
        if payload[:2] != b"PK" and filing.get("suffix") in (".zip", ".xbri"):
            raise ValueError(f"AFM download is not a zip ({payload[:60]!r})")
        return payload


def run(ctx: Any) -> Any:
    return run_adapter_job(AfmNlAdapter, ctx)
