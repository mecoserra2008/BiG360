"""Spain: CNMV registry -> (1) annual ESEF reports (IFA) and (2) the semiannual IPP XBRL (CNMV's own taxonomy).

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (ES), verified live 2026-10-11 (coverage-B).

(1) Annual financial reports (IFA), ESEF FY2020 ->
    GET https://www.cnmv.es/Portal/Consultas/IFA/ListadoIFA.aspx?id=0&nif=<NIF>
    One row per audited report: Official Registration No., Financial Statements date (= period_end), Publication date
    (= knowledge_date; CNMV note: "date of the latest submission in replacement of the initial one", so a replaced
    report carries the later date - conservative, never look-ahead) and a "ZIP/Xbri file" link
    (webservices/verdocumento/ver?e=<token>) holding the ESEF report package -> Arelle -> xBRL-JSON -> ifrs-full facts
    (base PackageFilingAdapter path). Only rows with a ZIP/Xbri link are used.
(2) Semiannual public periodic information (IPP), H2-2008 ->
    GET https://www.cnmv.es/Portal/Consultas/IFI/ListaIFI.aspx?nif=<NIF>  -> rows (publication date, "I|II half-year of
    YYYY[-YYYY]") linking ../../aldia/detalleifialdia.aspx?nreg=<nreg>; the detail page links
    ../consultas/wuc/descargaxbrlipp.ashx?t={guid} = the XBRL instance. Parsed directly with lxml (no Arelle):
      * 2016-06-01 (H1-2016 -> H2-2018) and 2019-01-01 (H1-2019 ->) taxonomies: ipp_ge (general) / ipp_en (credit
        institutions); numeric line codes I<nnnn> with the same meaning in both versions (2016 P&L columns add an
        Importe/Porcentaje member: amounts kept, percentages skipped).
        Consolidated statements = ge I1000-I1699 (+ I8400-I8699 direct-method cash flow), en I1000-I1999. Column
        contexts (PeriodoActual/PeriodoCierreAnterior/AcumuladoActual/AcumuladoAnterior...) are dropped as dimensions;
        the "PeriodoCorriente*" columns (standalone H2 in the H2 report, zero-filled duplicates in H1) are skipped so
        the H2 report yields the FY cumulative figures (H2 = FY - H1 is derived by the normalizer).
      * 2008-01-01 taxonomy (H2-2008 .. H2-2015): ipp-gen / ipp-enc tuples mixing ifrs-gp 2005, ipp-gen/enc/com and
        es-be-fs (Banco de España) concepts; contexts *_dcc/_dpac/_icc/_ipac (consolidated current/prior duration and
        instant). Only the primary statements (Balance, CuentaPerdidasGanancias, EstadoIngresosGastosReconocidos,
        EstadoFlujosEfectivo tuples) are kept; first occurrence wins.
    Values are reported in units of currency (decimals=-3 means rounded to thousands; no extra scaling). Exact zeros
    are dropped (most are unfilled template cells, e.g. the indirect cash-flow block of direct-method filers).
    Quarterly rows (I/III quarter, 2009-2020) only carry a PDF inside the XBRL and are ignored.

Facts: taxonomy 'other'; concept 'cnmv_ipp:I1205' (general scheme 2019), 'cnmv_ipp_en:I1572' (banks 2019) or
'cnmv_ipp08:<prefix>.<Local>' (2008 taxonomy, e.g. 'cnmv_ipp08:ifrs-gp.RevenueTotalByNature'); label = taxonomy label
(2019) or the local name (2008); consolidated=True (individual/parent-company statements are skipped).

Keys: none. source_entity_id = LEI (NIF map below verified 2026-10-11 against the CNMV entity name on the IFI page; the
CNMV NIF spelling is inconsistent: some issuers only resolve with a dash, 'A-84236934'). WAF: malformed or bursty
requests get 403 'CVFE' -> class rate limit 1 request / 2 s, browser User-Agent, and a circuit breaker (one 3-minute
back-off, then the run stops requesting). The portal gives dates only (no time), so knowledge_ts is null.

Modes: default (incremental) lists every ESEF annual report but only IPP reports published in the last
`recent_days` (default 400); history=True walks the whole IPP list back to H2-2008 (backfill).
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from lxml import etree

from app.core.http import HttpError
from app.core.ratelimit import RateLimit
from app.ingestion._global_common import fact_row, fiscal_year_for, run_adapter_job, to_date
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

BASE = "https://www.cnmv.es"
IFA_URL = BASE + "/Portal/Consultas/IFA/ListadoIFA.aspx"
IFI_URL = BASE + "/Portal/Consultas/IFI/ListaIFI.aspx"
DETAIL_URL = BASE + "/portal/aldia/detalleifialdia.aspx"
XBRL_URL = BASE + "/portal/consultas/wuc/descargaxbrlipp.ashx"
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/129.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}

#: LEI -> (CNMV NIF as the portal resolves it, fiscal-year-end month override or None). Verified 2026-10-11: the IFI
#: page subtitle (lblSubtitulo) shows the expected legal name for each NIF.
NIF_BY_LEI: dict[str, tuple[str, int | None]] = {
    "549300EEJH4FEPDBBR25": ("A28015865", None),   # TELEFONICA, S.A.
    "9598004A3FTY3TEHHN09": ("A-84236934", None),  # AMADEUS IT GROUP, S.A. (undashed NIF returns an empty list)
    "7CUNS533WID6K7DGFI87": ("A08663619", None),   # CAIXABANK, S.A.
    "5QK37QC7NWOJ8D7WVQ45": ("A-48010615", None),  # IBERDROLA, S.A.
    "549300TTCXZOGZM2EY83": ("A-15075062", 1),     # INDUSTRIA DE DISEÑO TEXTIL, S.A. (FY ends 31 January)
    "5493006QMFDDMYWIAM13": ("A39000013", None),   # BANCO SANTANDER, S.A.
    "K8MS7FD7N5Z2WQ51AZ71": ("A48265169", None),   # BANCO BILBAO VIZCAYA ARGENTARIA, S.A.
}
NIF_BY_ISIN: dict[str, str] = {
    "ES0178430E18": "A28015865", "ES0109067019": "A-84236934", "ES0140609019": "A08663619",
    "ES0144580Y14": "A-48010615", "ES0148396007": "A-15075062", "ES0113900J37": "A39000013",
    "ES0113211835": "A48265169",
}

_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_LABELS_PATH = Path(__file__).with_name("cnmv_ipp_labels.json")
_LABELS: dict[str, dict[str, str]] | None = None

#: 2019 taxonomy: consolidated primary-statement code ranges per scheme
_CONS_RANGES = {"ge": ((1000, 1700), (8400, 8700)), "en": ((1000, 2000),)}
#: 2019 taxonomy column members (dropped as dimensions); PeriodoCorriente* (standalone half) members are skipped
_COLUMN_MEMBERS = {"PeriodoActualMiembro", "PeriodoCierreAnteriorMiembro", "AcumuladoActualMiembro",
                   "AcumuladoAnteriorMiembro", "PeriodoAnteriorMiembro"}
#: 2008 taxonomy: consolidated cumulative contexts (current / prior duration, current / prior instant)
_CONS_SUFFIX_2008 = {"dcc", "dpac", "icc", "ipac"}
_STATEMENTS_2008 = ("Balance", "CuentaPerdidasGanancias", "EstadoIngresosGastosReconocidos", "EstadoFlujosEfectivo")
_NS_PREFIX_2008 = (("xbrl.iasb.org/int/fr/ifrs/gp", "ifrs-gp"), ("cnmv.es/ipp/gen", "ipp-gen"),
                   ("cnmv.es/ipp/enc", "ipp-enc"), ("cnmv.es/ipp/com", "ipp-com"), ("cnmv.es/ipp/ase", "ipp-ase"),
                   ("bde.es/es/fr/ifrs", "es-be-fs"))

_HALF_RE = re.compile(r"\b(I{1,2})\s+(?:half-year|semestre)\b.*?\b(?:of|de)\s+(\d{4})(?:\s*-\s*(\d{4}))?", re.I | re.S)


class CnmvBlocked(RuntimeError):
    """The CNMV WAF keeps answering 403: stop requesting for this run."""


def ipp_labels() -> dict[str, dict[str, str]]:
    global _LABELS
    if _LABELS is None:
        try:
            _LABELS = json.loads(_LABELS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _LABELS = {"ge": {}, "en": {}}
    return _LABELS


def _txt(el: Any) -> str:
    return " ".join(el.get_text(" ").split()) if el is not None else ""


def _soup(html: str | bytes) -> Any:
    from bs4 import BeautifulSoup

    return BeautifulSoup(html, "lxml")


# ---------------------------------------------------------------------------------------------- list pages
def parse_ifa(html: str | bytes, nif: str) -> list[dict[str, Any]]:
    """IFA list page -> annual ESEF filing dicts (rows with a ZIP/Xbri link only)."""
    s = _soup(html)
    out = []
    for tr in s.select("table tr"):
        tds = {td.get("data-th", ""): td for td in tr.find_all("td")}
        if not tds:
            continue
        reg = _txt(next((v for k, v in tds.items() if k.lower().startswith(("official registration", "nº registro",
                                                                            "n.º registro", "número"))), None))
        fs = to_date(_txt(next((v for k, v in tds.items() if "statements date" in k.lower() or "fecha de las" in k.lower()
                                or "fecha cuentas" in k.lower()), None)))
        pub = to_date(_txt(next((v for k, v in tds.items() if "publication" in k.lower() or "publicación" in k.lower()
                                 or "registro" == k.lower()), None)))
        a = tr.find("a", id=re.compile(r"hlFicheroZIP$"))
        if a is None:
            a = next((x for k, v in tds.items() if "zip" in k.lower() for x in v.find_all("a", href=True)), None)
        if a is None or not a.get("href") or fs is None or pub is None:
            continue
        reg = re.sub(r"\D", "", reg) or pub.isoformat()
        out.append({
            "native_key": f"ifa:{nif.replace('-', '')}:{reg}", "knowledge_ts": None, "knowledge_date": pub.isoformat(),
            "doc_url": a["href"], "suffix": ".zip", "form_type": "AFR", "form_class": "annual", "fiscal_period": "FY",
            "period_end": fs.isoformat(), "url": f"{IFA_URL}?id=0&nif={nif}", "language": "es", "is_amendment": False,
            "kind": "esef",
        })
    return out


def parse_ifi(html: str | bytes, nif: str, fye_month: int | None = None) -> list[dict[str, Any]]:
    """IFI list page -> semiannual IPP filing dicts (doc_url = detail page; resolved to the XBRL at download)."""
    s = _soup(html)
    rows = []
    for tr in s.select("table tr"):
        a = tr.find("a", href=re.compile(r"detalleifialdia\.aspx\?nreg=\d+"))
        if a is None:
            continue
        nreg = re.search(r"nreg=(\d+)", a["href"]).group(1)
        pub = to_date(a.get_text(strip=True))
        tds = tr.find_all("td")
        kind = _txt(tds[1]) if len(tds) > 1 else ""
        m = _HALF_RE.search(kind)
        if pub is None or m is None:
            continue  # quarterly statements (PDF only) and unparseable rows
        half, y1, y2 = m.group(1).upper(), int(m.group(2)), int(m.group(3)) if m.group(3) else None
        fp = "H1" if half == "I" else "FY"
        rows.append({"nreg": nreg, "pub": pub, "fp": fp, "label": kind, "y1": y1, "y2": y2})
    out = []
    seen: set[tuple] = set()
    for r in sorted(rows, key=lambda x: (x["pub"], x["nreg"])):
        key = (r["fp"], r["y1"], r["y2"])
        out.append({
            "native_key": f"ipp:{nif.replace('-', '')}:{r['nreg']}", "knowledge_ts": None,
            "knowledge_date": r["pub"].isoformat(), "doc_url": f"{DETAIL_URL}?nreg={r['nreg']}", "suffix": ".xml",
            "form_type": "IPP-H1" if r["fp"] == "H1" else "IPP-H2", "form_class": "semiannual",
            "fiscal_period": r["fp"], "period_end": None, "url": f"{DETAIL_URL}?nreg={r['nreg']}", "language": "es",
            "is_amendment": key in seen, "title": r["label"], "kind": "ipp",
        })
        seen.add(key)
    return out


# ---------------------------------------------------------------------------------------------- IPP XBRL
def strip_large_text(payload: bytes, limit: int = 20000) -> bytes:
    """Drop elements with huge text (the embedded base64 PDF of the full report, often 15-30 MB)."""
    root = etree.fromstring(payload, etree.XMLParser(huge_tree=True, remove_blank_text=False))
    for el in list(root.iter()):
        if isinstance(el.tag, str) and el.text and len(el.text) > limit and el.getparent() is not None:
            el.getparent().remove(el)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")


def _contexts(root: Any) -> dict[str, dict[str, Any]]:
    out = {}
    for c in root.iter(_XBRLI + "context"):
        p = c.find(_XBRLI + "period")
        if p is None:
            continue
        inst, start, end = p.find(_XBRLI + "instant"), p.find(_XBRLI + "startDate"), p.find(_XBRLI + "endDate")
        members = [(m.text or "").strip().split(":")[-1] for m in c.iter(_XBRLDI + "explicitMember")]
        typed = len(list(c.iter(_XBRLDI + "typedMember")))
        scen = {etree.QName(e).localname: (e.text or "").strip() for e in c.iter() if isinstance(e.tag, str)
                and etree.QName(e).namespace == "http://www.cnmv.es/ipp/2008"}
        out[c.get("id")] = {
            "start": to_date(start.text) if start is not None else None,
            "end": to_date((inst if inst is not None else end).text),
            "members": members, "typed": typed, "scen": scen}
    return out


def _units(root: Any) -> dict[str, str]:
    out = {}
    for u in root.iter(_XBRLI + "unit"):
        m = u.find(_XBRLI + "measure")
        if m is None or not m.text:
            continue
        v = m.text.strip()
        out[u.get("id")] = v.split(":", 1)[1] if v.lower().startswith("iso4217:") else (
            "pure" if v.endswith(":pure") else ("shares" if v.endswith(":shares") else v))
    return out


def parse_ipp(payload: bytes) -> dict[str, Any]:
    """IPP XBRL instance -> compact doc {"documentInfo": {"cnmv_ipp": {...}}, "facts": {id: {...}}}.

    Each fact: value (float), decimals, label, dimensions {concept, period ('YYYY-MM-DD' or 'start/end'), unit}.
    Only consolidated primary-statement facts with a unit, non-zero values, one value per (concept, period)."""
    root = etree.fromstring(payload, etree.XMLParser(huge_tree=True))
    schema = ""
    for el in root:
        if isinstance(el.tag, str) and etree.QName(el).localname == "schemaRef":
            schema = el.get("{http://www.w3.org/1999/xlink}href") or ""
            break
    nsmap = {v: k for k, v in (root.nsmap or {}).items() if k}
    if "/ipp/en/" in schema or "ipp_en" in schema:
        scheme = "en"
    elif "/ipp/ge/" in schema or "ipp_ge" in schema:
        scheme = "ge"
    elif "ipp-enc" in schema or "ipp-ase" in schema:
        scheme = "2008-en"
    else:
        scheme = "2008-ge"
    ctx, units = _contexts(root), _units(root)
    labels = ipp_labels().get(scheme, {}) if scheme in ("ge", "en") else {}
    cur = [u for u in units.values() if re.fullmatch(r"[A-Z]{3}", u)]
    currency = max(set(cur), key=cur.count) if cur else "EUR"
    facts: dict[str, dict[str, Any]] = {}
    best: dict[tuple, str] = {}
    n = 0
    for el in root.iter():
        if not isinstance(el.tag, str) or el.get("unitRef") is None or el.get("contextRef") is None:
            continue
        c = ctx.get(el.get("contextRef"))
        unit = units.get(el.get("unitRef"))
        if c is None or unit is None or c["end"] is None:
            continue
        try:
            value = float((el.text or "").strip())
        except ValueError:
            continue
        if value == 0:
            continue
        q = etree.QName(el)
        if scheme in ("ge", "en"):
            code = q.localname
            if not re.fullmatch(r"I\d{4}", code) or not any(a <= int(code[1:]) < b for a, b in _CONS_RANGES[scheme]):
                continue
            # 2016-06-01 taxonomy: P&L columns carry a 2nd member Importe (amount) / Porcentaje (% of revenue: skip)
            mem = [x for x in c["members"] if x != "ImporteMiembro"]
            if c["typed"] or len(mem) > 1 or (mem and mem[0] not in _COLUMN_MEMBERS):
                continue
            concept = f"cnmv_ipp:{code}" if scheme == "ge" else f"cnmv_ipp_en:{code}"
            label = labels.get(code)
        else:
            suffix = el.get("contextRef").rsplit("_", 1)[-1]
            if suffix not in _CONS_SUFFIX_2008 and c["scen"].get("Apartado", "").lower() != "consolidado":
                continue
            if suffix not in _CONS_SUFFIX_2008:  # consolidated but standalone-half context (dcoc/dpoc)
                continue
            parent = el.getparent()
            pname = etree.QName(parent).localname if parent is not None and isinstance(parent.tag, str) else ""
            if not pname.startswith(_STATEMENTS_2008):
                continue
            prefix = next((p for frag, p in _NS_PREFIX_2008 if frag in (q.namespace or "")), nsmap.get(q.namespace, "x"))
            concept = f"cnmv_ipp08:{prefix}.{q.localname}"
            label = q.localname
        lab = (label or "").strip().lower()
        if "earningslosspershare" in concept.lower() or lab in ("básico", "diluido"):
            unit = f"{currency if unit in ('pure', 'shares') else unit}/shares"  # 2008 EPS facts use unit 'pure'
        elif unit in ("pure", "shares"):
            continue
        period = c["end"].isoformat() if c["start"] is None else f"{c['start'].isoformat()}/{c['end'].isoformat()}"
        key = (concept, period, unit)
        if key in best:
            continue  # first (non-zero) occurrence wins
        dec = el.get("decimals")
        fid = f"f{n}"
        n += 1
        best[key] = fid
        facts[fid] = {"value": value, "decimals": int(dec) if dec and dec.lstrip("-").isdigit() else None,
                      "label": label, "dimensions": {"concept": concept, "period": period, "unit": unit}}
    durs = [(to_date(f["dimensions"]["period"].split("/")[0]), to_date(f["dimensions"]["period"].split("/")[1]))
            for f in facts.values() if "/" in f["dimensions"]["period"]]
    pend = max((e for _, e in durs), default=None)
    pstart = min((s for s, e in durs if e == pend), default=None)
    return {"documentInfo": {"documentType": "cnmv-ipp", "cnmv_ipp": {
        "scheme": scheme, "schema": schema, "period_start": pstart.isoformat() if pstart else None,
        "period_end": pend.isoformat() if pend else None}}, "facts": facts}


def _ipp_fiscal_period(start: date | None, end: date, filing_fp: str | None, filing_end: date | None) -> str | None:
    if start is None:
        if filing_end and end == filing_end:
            return filing_fp
        return "FY"  # prior fiscal year-end balance (PeriodoCierreAnterior)
    days = (end - start).days + 1
    if 340 <= days <= 380:
        return "FY"
    if 170 <= days <= 195:
        return "H1"
    return None


def ipp_fact_rows(doc: dict[str, Any], *, source_id: str, source_entity_id: str, filing_id: str, form_type: str,
                  knowledge_date: date, fye_month: int | None, filing_fp: str | None) -> list[dict[str, Any]]:
    info = (doc.get("documentInfo") or {}).get("cnmv_ipp") or {}
    fend = to_date(info.get("period_end"))
    rows = []
    for f in (doc.get("facts") or {}).values():
        d = f.get("dimensions") or {}
        per = d.get("period") or ""
        if "/" in per:
            start, end = (to_date(x) for x in per.split("/", 1))
        else:
            start, end = None, to_date(per)
        if end is None or end > knowledge_date:
            continue
        rows.append(fact_row(
            source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id, form_type=form_type,
            taxonomy="other", concept=d["concept"], label=f.get("label"), period_start=start, period_end=end,
            value=float(f["value"]), unit=d["unit"], knowledge_date=knowledge_date,
            fiscal_year=fiscal_year_for(end, fye_month), fiscal_period=_ipp_fiscal_period(start, end, filing_fp, fend),
            decimals=f.get("decimals"), dims=None, consolidated=True))
    return rows


# ---------------------------------------------------------------------------------------------- adapter
@register_adapter
class CnmvEsAdapter(PackageFilingAdapter):
    source_id = "cnmv_es"
    regions = ("EU",)
    countries = ("ES",)
    base_url = BASE
    rate_limit = RateLimit(0.5, 1)
    min_year: ClassVar[int] = 2008
    #: seconds to back off after a WAF 403 before the single retry
    waf_backoff_s: ClassVar[float] = 180.0

    def __init__(self, *args: Any, history: Any = False, recent_days: int = 400, esef: Any = True, ipp: Any = True,
                 **kw: Any):
        super().__init__(*args, **kw)
        self.history = str(history).lower() in ("1", "true", "yes")
        self.recent_days = int(recent_days)
        self.do_esef = str(esef).lower() not in ("0", "false", "no")
        self.do_ipp = str(ipp).lower() not in ("0", "false", "no")
        self._blocked = False

    # ------------------------------------------------------------------ universe / ids
    def universe(self) -> list[dict[str, Any]]:
        out = []
        for i in super().universe():
            nif, fye = self.nif_for(i)
            out.append({**i, "fye_month": i.get("fye_month") or fye, "nif": nif})
        return out

    @staticmethod
    def nif_for(issuer: dict[str, Any]) -> tuple[str | None, int | None]:
        if issuer.get("nif"):
            return issuer["nif"], NIF_BY_LEI.get(issuer.get("lei") or "", (None, None))[1]
        for lei in [issuer.get("lei"), *(issuer.get("leis") or [])]:
            if lei in NIF_BY_LEI:
                return NIF_BY_LEI[lei]
        nif = NIF_BY_ISIN.get(issuer.get("isin") or "")
        return nif, None

    # ------------------------------------------------------------------ http (WAF-aware)
    def _get(self, url: str, cache_ttl: int = 0) -> Any:
        if self._blocked:
            raise CnmvBlocked("CNMV WAF blocked earlier in this run; not requesting")
        for attempt in range(2):
            try:
                return self.http.get(url, cache_ttl=cache_ttl, headers=BROWSER_HEADERS)
            except HttpError as e:
                if e.status != 403:
                    raise
                if attempt == 0:
                    self.log.warning("cnmv_es: 403 (WAF) on %s; backing off %.0fs", url[:90], self.waf_backoff_s)
                    time.sleep(self.waf_backoff_s)
                    continue
                self._blocked = True
                raise CnmvBlocked(f"CNMV WAF 403 twice ({url[:90]})") from e
        raise CnmvBlocked("unreachable")

    # ------------------------------------------------------------------ listing
    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        nif = issuer.get("nif") or self.nif_for(issuer)[0]
        if not nif:
            raise ValueError(f"no verified CNMV NIF for {issuer.get('name')} ({issuer.get('lei')})")
        out: list[dict[str, Any]] = []
        if self.do_esef:
            r = self._get(f"{IFA_URL}?id=0&nif={nif}", cache_ttl=6 * 3600)
            out += [f for f in parse_ifa(r.content, nif) if (to_date(f["period_end"]) or date(1900, 1, 1)).year >= 2020]
        if self.do_ipp:
            r = self._get(f"{IFI_URL}?nif={nif}", cache_ttl=6 * 3600)
            ipp = parse_ifi(r.content, nif, issuer.get("fye_month"))
            if not self.history:
                cut = datetime.now(UTC).date() - timedelta(days=self.recent_days)
                ipp = [f for f in ipp if to_date(f["knowledge_date"]) >= cut]
            out += ipp
        return out

    # ------------------------------------------------------------------ download / parse
    def download(self, filing: dict[str, Any]) -> bytes:
        if filing.get("kind") == "ipp":
            det = self._get(filing["doc_url"], cache_ttl=0)
            m = re.search(r"descargaxbrlipp\.ashx\?t=(\{[0-9a-fA-F-]{36}\})", det.text if hasattr(det, "text")
                          else det.content.decode("utf-8", "replace"))
            if not m:
                raise ValueError(f"no IPP XBRL link on {filing['doc_url']}")
            x = self._get(f"{XBRL_URL}?t={m.group(1)}", cache_ttl=0).content
            if b"<xbrli:xbrl" not in x[:4000] and b":xbrl" not in x[:4000]:
                raise ValueError(f"IPP download is not an XBRL instance ({len(x)} bytes)")
            return strip_large_text(x)
        data = self._get(filing["doc_url"], cache_ttl=0).content
        if data[:2] != b"PK":
            raise ValueError(f"ESEF download is not a zip/xbri package ({data[:40]!r})")
        return data

    def to_doc(self, filing: dict[str, Any], payload: bytes) -> dict[str, Any]:
        if filing.get("kind") == "ipp":
            doc = parse_ipp(payload)
            if not doc["facts"]:
                raise ValueError("IPP instance has no consolidated numeric facts")
            return doc
        return super().to_doc(filing, payload)

    def _land(self, f: dict[str, Any]) -> dict[str, Any]:
        if f.get("kind") != "ipp":
            return super()._land(f)
        t0 = time.time()
        payload = self.download(f)
        safe_key = f["native_key"].replace("/", "_").replace(":", "_")[:120]
        self.save_raw("packages", f"{safe_key}.xml", payload)  # stripped instance, gzip
        doc = self.to_doc(f, payload)
        info = doc["documentInfo"]["cnmv_ipp"]
        f = {**f, "period_end": info.get("period_end") or f.get("period_end"), "ipp_scheme": info.get("scheme")}
        path = self.save_raw("xbrl_json", safe_key, doc)
        self.log.info("cnmv_es %s: %d IPP facts (%s, %.1fs)", f["native_key"], len(doc["facts"]), info.get("scheme"),
                      time.time() - t0)
        return {**f, "_raw_path": str(path)}

    def facts_from_doc(self, filing: dict[str, Any], doc: dict[str, Any], issuer_fye: int | None,
                       fid: str, kd: date) -> list[dict[str, Any]]:
        if (doc.get("documentInfo") or {}).get("cnmv_ipp"):
            pe = to_date(filing.get("period_end"))
            fye = issuer_fye or (pe.month if pe and filing.get("fiscal_period") == "FY" else None)
            return ipp_fact_rows(doc, source_id=self.source_id, source_entity_id=filing["_entity"], filing_id=fid,
                                 form_type=filing.get("form_type") or "IPP", knowledge_date=kd, fye_month=fye,
                                 filing_fp=filing.get("fiscal_period"))
        return super().facts_from_doc(filing, doc, issuer_fye, fid, kd)


def run(ctx: Any) -> Any:
    return run_adapter_job(CnmvEsAdapter, ctx)
