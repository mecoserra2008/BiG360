"""Italy: eMarket Storage (Teleborsa/Spafid OAM) + 1Info (Computershare OAM, secondary) -> ESEF annual reports.

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (IT), verified live 2026-10-11.
  eMarket Storage (HTML list, Drupal view; no API, no LEI):
     GET https://www.emarketstorage.it/en/node/30?categoria=100&azienda=<id>&page=N   (100 = annual financial report)
     rows div.views-row > .azienda-wrapper[data-protocollo] + <time>MM/DD/YYYY - HH:MM</time> (Europe/Rome) +
     link /sites/default/files/xbrl/YYYY-MM/YYYYMMDD_<protocollo>.zip|.xbri (ESEF) or /comunicati/...pdf
     Separate (parent-only) and consolidated packages are filed side by side, in IT and EN: only consolidated /
     group packages are kept ("Bilancio d'esercizio", "financial statements of X S.p.A." are dropped).
  1Info (issuers not on eMarket, e.g. Eni):
     POST https://www.1info.it/PORTALE1INFO/API/Documenti (DataTables form: draw/start/length/columns[..] +
          SearchFilter[emittente][]=<ndg>, SearchFilter[oggetto]=ESEF) -> data[{dataStoccaggio (epoch s, storage
          time), dataEsercizio (epoch, FY end), oggetto, protocolCodeXbrl ("164624_oneinfo.xbri"), bilancio_consolidato}]
     GET  https://www.1info.it/PdfViewer/PdfShow.aspx?service=&type=documenti&year=<FY>&file=<protocolCodeXbrl>&download=1
Website terms (public regulated information); keys: none. source_entity_id = LEI. knowledge_ts = storage timestamp.
Issuer ids: no LEI search on either site -> verified id maps below (eMarket azienda ids from the company dropdown,
1Info ndg from /API/companies/documenti, 2026-10-11). Earliest consolidated ESEF deposit per year is kept (PIT).
"""

from __future__ import annotations

import html as _html
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

EMARKET = "https://www.emarketstorage.it"
EMARKET_LIST = f"{EMARKET}/en/node/30"
ONEINFO = "https://www.1info.it"
ONEINFO_API = f"{ONEINFO}/PORTALE1INFO/API/Documenti"
ONEINFO_DL = f"{ONEINFO}/PdfViewer/PdfShow.aspx"
_ROME = ZoneInfo("Europe/Rome")
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"

#: LEI -> eMarket Storage azienda id (company dropdown of /en/node/30, verified 2026-10-11)
EMARKET_IDS: dict[str, int] = {
    "WOCMU6HCI0OJWNPRZS33": 1003,  # Enel
    "549300X5UKJVE386ZB61": 236,   # Assicurazioni Generali
    "2W8N8UU78PMDQKZENC08": 142,   # Intesa Sanpaolo
    "549300TRUWO2CD2G5692": 198,   # UniCredit
}
#: LEI -> 1Info ndg (issuer id; /PORTALE1INFO/API/companies/documenti, verified 2026-10-11)
ONEINFO_NDG: dict[str, int] = {
    "BUCRF72VH5RBN7X3VL35": 117,   # Eni
}

_ROW = re.compile(r'data-protocollo="(\d+)".*?class="datetime">([^<]+)</time>.*?news-title"><a href="([^"]*)"[^>]*>'
                  r'(.*?)</a>', re.S)
_SEPARATE = re.compile(r"bilancio\s+d.\s*esercizio|bilancio\s+di\s+esercizio|financial statements of [\w .&-]+ s\.?p\.?a|"
                       r"separate financial statements|progetto di bilancio d.esercizio", re.I)
_CONSOL = re.compile(r"consolidat|gruppo|group", re.I)


def parse_rome(s: str) -> datetime | None:
    """'04/13/2026 - 18:11' (Europe/Rome, month first on the /en/ site) -> UTC."""
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*-\s*(\d{1,2}):(\d{2})", s or "")
    if not m:
        return None
    mo, d, y, hh, mm = (int(x) for x in m.groups())
    return datetime(y, mo, d, hh, mm, tzinfo=_ROME).astimezone(UTC)


def parse_emarket(page_html: str) -> list[dict[str, Any]]:
    out = []
    for blk in page_html.split('class="views-row"')[1:]:
        m = _ROW.search(blk)
        if not m:
            continue
        title = _html.unescape(re.sub(r"<[^>]+>", "", m.group(4))).strip()
        out.append({"protocollo": m.group(1), "ts": parse_rome(m.group(2)), "href": _html.unescape(m.group(3)),
                    "title": title})
    return out


def is_consolidated(title: str) -> bool:
    return bool(_CONSOL.search(title)) or not _SEPARATE.search(title)


def select_emarket(rows: list[dict[str, Any]], min_year: int = 2017) -> list[dict[str, Any]]:
    """Earliest consolidated ESEF package per deposit year; years with only PDFs -> one metadata row."""
    pkgs: dict[int, dict[str, Any]] = {}
    pdfs: dict[int, dict[str, Any]] = {}
    for r in sorted((r for r in rows if r["ts"]), key=lambda r: r["ts"]):
        if r["ts"].year < min_year:
            continue
        href = r["href"].lower()
        is_pkg = "/xbrl/" in href and href.endswith((".zip", ".xbri"))
        if is_pkg and not is_consolidated(r["title"]):
            continue
        if not is_pkg and not re.search(r"annual|relazione finanziaria|bilancio|report", r["title"], re.I):
            continue
        y = r["ts"].year
        bucket = pkgs if is_pkg else pdfs
        if y in bucket:
            continue
        url = (EMARKET + r["href"]) if r["href"].startswith("/") else (r["href"] or None)
        bucket[y] = {
            "native_key": f"em:{r['protocollo']}", "knowledge_ts": r["ts"].isoformat(),
            "doc_url": url if is_pkg else None, "suffix": ".xbri" if href.endswith(".xbri") else ".zip",
            "form_type": "AFR", "form_class": "annual", "fiscal_period": "FY", "period_end": None,
            "url": url or EMARKET_LIST, "language": "en" if re.search(r"\b(report|annual|financial)\b", r["title"], re.I)
            else "it", "is_amendment": False, "title": r["title"]}
    out = list(pkgs.values()) + [f for y, f in pdfs.items() if y not in pkgs]
    return sorted(out, key=lambda f: f["knowledge_ts"])


def select_oneinfo(data: list[dict[str, Any]], min_year: int = 2017) -> list[dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for d in sorted((d for d in data if d.get("dataStoccaggio") and d.get("protocolCodeXbrl")),
                    key=lambda d: d["dataStoccaggio"]):
        code = d["protocolCodeXbrl"]
        if not code.lower().endswith((".zip", ".xbri")) or d.get("bilancio_consolidato") == 0:
            continue
        kts = datetime.fromtimestamp(int(d["dataStoccaggio"]), UTC)
        fy = datetime.fromtimestamp(int(d.get("dataEsercizio") or d["dataStoccaggio"]), UTC).year
        if kts.year < min_year or fy in out:
            continue
        out[fy] = {
            "native_key": f"1i:{code.rsplit('.', 1)[0]}", "knowledge_ts": kts.isoformat(),
            "doc_url": f"{ONEINFO_DL}?service=&type=documenti&year={fy}&file={code}&download=1",
            "suffix": "." + code.lower().rsplit(".", 1)[-1], "form_type": "AFR", "form_class": "annual",
            "fiscal_period": "FY", "period_end": None, "url": f"{ONEINFO}/PORTALE1INFO/Documenti",
            "language": None, "is_amendment": False, "title": d.get("oggetto")}
    return sorted(out.values(), key=lambda f: f["knowledge_ts"])


def oneinfo_form(ndg: int, start: int = 0, length: int = 100) -> dict[str, str]:
    """DataTables server-side form body (the API 500s without the columns[...] block)."""
    f = {"draw": "1", "start": str(start), "length": str(length), "order[0][column]": "2", "order[0][dir]": "desc",
         "search[value]": "", "search[regex]": "false",
         "SearchFilter[emittente][]": str(ndg), "SearchFilter[oggetto]": "ESEF"}
    for i, c in enumerate(["", "mittente", "dataStoccaggio", "oggetto", "", ""]):
        f.update({f"columns[{i}][data]": c, f"columns[{i}][name]": "", f"columns[{i}][searchable]": "true",
                  f"columns[{i}][orderable]": "true" if c else "false", f"columns[{i}][search][value]": "",
                  f"columns[{i}][search][regex]": "false"})
    return f


@register_adapter
class EmarketItAdapter(PackageFilingAdapter):
    source_id = "emarket_it"
    regions = ("EU",)
    countries = ("IT",)
    base_url = EMARKET
    rate_limit = RateLimit(1.0, 1)
    max_pages = 8
    _hdr = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9,it;q=0.8"}

    def _ids(self, issuer: dict[str, Any]) -> tuple[int | None, int | None]:
        leis = [x for x in (issuer.get("leis") or [issuer.get("lei")]) if x]
        em = next((EMARKET_IDS[x] for x in leis if x in EMARKET_IDS), None)
        oi = next((ONEINFO_NDG[x] for x in leis if x in ONEINFO_NDG), None)
        return em, oi

    def _emarket_rows(self, azienda: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(self.max_pages):
            r = self.http.get(EMARKET_LIST, params={"categoria": 100, "azienda": azienda, "page": page},
                              cache_ttl=6 * 3600, headers=self._hdr)
            got = parse_emarket(r.content.decode("utf-8", "replace"))
            if not got or (rows and got[0]["protocollo"] == rows[0]["protocollo"]):
                break
            rows += got
            if min((g["ts"] for g in got if g["ts"]), default=None) and \
                    min(g["ts"] for g in got if g["ts"]).year < self.min_year:
                break
        return rows

    def _oneinfo_rows(self, ndg: int) -> list[dict[str, Any]]:
        hdr = {**self._hdr, "X-Requested-With": "XMLHttpRequest", "Origin": ONEINFO,
               "Referer": f"{ONEINFO}/PORTALE1INFO/Documenti", "Accept": "application/json, text/javascript, */*"}
        j = self.http.post(ONEINFO_API, data=oneinfo_form(ndg), headers=hdr).json()
        return [d for d in j.get("data") or [] if d.get("ndg") == ndg]

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        em, oi = self._ids(issuer)
        out: list[dict[str, Any]] = []
        if em is not None:
            out = select_emarket(self._emarket_rows(em), self.min_year)
        if oi is not None and not any(f["doc_url"] for f in out):
            out = select_oneinfo(self._oneinfo_rows(oi), self.min_year)
        if em is None and oi is None:
            raise LookupError(f"no eMarket / 1Info id for {issuer.get('name')} ({issuer.get('lei')})")
        return out

    def download(self, filing: dict[str, Any]) -> bytes:
        hdr = dict(self._hdr)
        if filing["native_key"].startswith("1i:"):
            hdr["Referer"] = f"{ONEINFO}/PORTALE1INFO/Documenti"
        payload = self.http.get(filing["doc_url"], cache_ttl=0, headers=hdr).content
        if payload[:2] != b"PK":
            raise ValueError(f"download is not a zip ({payload[:60]!r})")
        return payload


def run(ctx: Any) -> Any:
    return run_adapter_job(EmarketItAdapter, ctx)
