"""Peru: SMV SIMV "Información Financiera" (ASP.NET WebForms) -> structured financial statements with filing timestamps.

Research: docs/COMPANY_COVERAGE_OPTIONS.md §5 (PE), verified 2026-10-11. Keyless, public.
  https://www.smv.gob.pe/SIMV/Frm_InformacionFinanciera?data=A70181B60967D74090DCD93C4920AA1D769614EC12
  postbacks: (1) select company (cboDenominacionSocial = SMV company id, Credicorp 18049), (2) switch cboPeriodo to
  'A' (anual, autopostback) when needed, (3) search: cboTipo C (consolidated), cboPeriodo A | T (intermedio),
  cboAnio -1 / cboTrimestre -1 (= "TODOS"; sending the text "TODOS" gives HTTP 500), cbBuscar=Buscar.
  Result grid MainContent_grdInfoFinanciera: Denominación | Documento | N° Expediente | Fecha de Presentación
  ("31/03/2026 05:21:01 p.m.", Lima time) + link. "Estados Financieros" rows link Frm_DetalleInfoFinanciera.aspx
  (structured statements: grdBalanceGeneral, grdEstadoGP, grdEstadoFlujo, grdIntegrales; header
  "CONSOLIDADA | ANUAL AL 31 DE DICIEMBRE DEL 2025 ( EN MILES DE NUEVOS SOLES )"); the other rows are PDFs
  (ConsultasP8/documento.aspx?vidDoc=...). Credicorp files no XBRL instance with SMV, so the structured statements are
  the machine-readable source.
Facts: every statement line -> taxonomy 'other', concept 'smv:<grid>:<slug(label)>' (label kept, duplicates numbered),
values scaled to units (MILES -> x1000; per-share lines unscaled, unit <CCY>/shares). Unambiguous totals are ALSO
emitted as ifrs-full concepts (Assets, Liabilities, Equity, EquityAttributableToOwnersOfParent, ProfitLoss,
ProfitLossAttributableToOwnersOfParent, ProfitLossBeforeTax, ComprehensiveIncome, Basic/DilutedEarningsLossPerShare,
CashAndCashEquivalents) so the canonical chains pick them up; revenue is NOT mapped (bank / insurer "ingresos de
actividades ordinarias" is a gross total, not IFRS Revenue). Currency from the header (NUEVOS SOLES / SOLES -> PEN,
DOLARES -> USD; Credicorp switched USD -> PEN).
PIT: knowledge_ts = Fecha de Presentación (America/Lima -> UTC).
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import fact_row, fiscal_year_for, load_state, run_adapter_job, to_date
from app.ingestion._latam_xbrl import label_quarters
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import FetchResult, register_adapter

BASE = "https://www.smv.gob.pe"
FORM_URL = BASE + "/SIMV/Frm_InformacionFinanciera?data=A70181B60967D74090DCD93C4920AA1D769614EC12"
PE_TZ = ZoneInfo("America/Lima")
#: yf_ticker -> SMV company id (cboDenominacionSocial value), verified 2026-10-11.
SMV_IDS: dict[str, str] = {"BAP": "18049", "BVN": "50", "IFS": "116759"}  # BVN/IFS: exact names in SMV dropdown
#: Non-financial filers whose "Total de ingresos de actividades ordinarias" is IFRS Revenue (not a bank gross total).
SMV_REVENUE_COMPANIES: frozenset[str] = frozenset({"50"})
_REVENUE_LABELS = {"total_de_ingresos_de_actividades_ordinarias", "ingresos_de_actividades_ordinarias",
                   "total_ingresos_de_actividades_ordinarias"}
#: Verified presentation currency by report period (the SMV header is filer-typed and occasionally wrong: Credicorp's
#: 2010-Q4 and 2012-Q2 headers say NUEVOS SOLES while the figures are US$ thousands; Credicorp presents in PEN from
#: FY2014). company id -> [(last report period_end inclusive | None = open-ended, ISO currency)].
SMV_CCY: dict[str, list[tuple[date | None, str]]] = {"18049": [(date(2013, 12, 31), "USD"), (None, "PEN")]}
_MC = "ctl00$MainContent$"
_GRIDS = {"grdBalanceGeneral": "BS", "grdEstadoGP": "IS", "grdEstadoFlujo": "CF", "grdIntegrales": "OCI"}
_MONTHS = {"ENERO": 1, "FEBRERO": 2, "MARZO": 3, "ABRIL": 4, "MAYO": 5, "JUNIO": 6, "JULIO": 7, "AGOSTO": 8,
           "SETIEMBRE": 9, "SEPTIEMBRE": 9, "OCTUBRE": 10, "NOVIEMBRE": 11, "DICIEMBRE": 12}

#: (statement, normalised label) -> ifrs-full concept. Labels are matched exactly after slugging.
IFRS_MAP: dict[tuple[str, str], str] = {
    ("BS", "total_activo"): "Assets", ("BS", "total_de_activos"): "Assets", ("BS", "total_activos"): "Assets",
    ("BS", "total_pasivo"): "Liabilities", ("BS", "total_pasivos"): "Liabilities",
    ("BS", "total_de_pasivos"): "Liabilities",
    ("BS", "total_patrimonio"): "Equity", ("BS", "total_patrimonio_neto"): "Equity",
    ("BS", "total_de_patrimonio"): "Equity",
    ("BS", "total_patrimonio_neto_atribuible_a_la_matriz"): "EquityAttributableToOwnersOfParent",
    ("BS", "patrimonio_atribuible_a_los_propietarios_de_la_controladora"): "EquityAttributableToOwnersOfParent",
    ("BS", "efectivo_y_equivalentes_al_efectivo"): "CashAndCashEquivalents",
    ("IS", "resultado_neto_del_ejercicio"): "ProfitLoss",
    ("IS", "ganancia_perdida_neta_del_ejercicio"): "ProfitLoss",
    ("IS", "resultado_antes_de_interes_minoritario"): "ProfitLoss",  # pre-2013 SMV layout
    ("IS", "propietarios_de_la_controladora"): "ProfitLossAttributableToOwnersOfParent",
    ("IS", "resultado_neto_del_ejercicio_del_grupo_consolidable"): "ProfitLossAttributableToOwnersOfParent",
    ("IS", "resultado_antes_de_impuesto_a_las_ganancias"): "ProfitLossBeforeTax",
    ("IS", "ganancia_perdida_basica_por_accion_comun"): "BasicEarningsLossPerShare",
    ("IS", "ganancia_perdida_diluida_por_accion_comun"): "DilutedEarningsLossPerShare",
    ("OCI", "resultado_integral_total_del_ejercicio_neto_del_impuesto_a_la_renta"): "ComprehensiveIncome",
    ("OCI", "resultado_integral_total_del_ejercicio_neto"): "ComprehensiveIncome",
}


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", s)).strip("_")[:80]


def presentation_ts(s: str) -> datetime | None:
    """'31/03/2026 05:21:01 p.m.' (Lima) -> aware UTC datetime."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap])?\.?\s*m?", s or "", re.I)
    if not m:
        return None
    d, mo, y, hh, mi, ss, ap = m.groups()
    h = int(hh) % 12 + (12 if ap and ap.lower() == "p" else 0) if ap else int(hh)
    return datetime(int(y), int(mo), int(d), h, int(mi), int(ss or 0), tzinfo=PE_TZ).astimezone(UTC)


def form_state(html: str | bytes) -> dict[str, str]:
    """ASP.NET form state (hidden fields, selected options, checked radios, text inputs)."""
    s = BeautifulSoup(html, "lxml")
    d: dict[str, str] = {}
    for i in s.find_all("input", type="hidden"):
        if i.get("name"):
            d[i["name"]] = i.get("value", "")
    for sel in s.find_all("select"):
        o = sel.find("option", selected=True) or sel.find("option")
        if o is not None and sel.get("name"):
            d[sel["name"]] = o.get("value", "")
    for i in s.find_all("input", type="radio"):
        if i.has_attr("checked") and i.get("name"):
            d[i["name"]] = i.get("value", "")
    for i in s.find_all("input", type="text"):
        if i.get("name"):
            d[i["name"]] = i.get("value", "")
    d.pop("ctl00$txtSearch", None)
    return d


def parse_grid(html: str | bytes) -> list[dict[str, Any]]:
    """Result grid -> [{'document', 'expediente', 'ts', 'href'}]."""
    s = BeautifulSoup(html, "lxml")
    g = s.find("table", id=lambda x: bool(x) and x.endswith("grdInfoFinanciera"))
    out = []
    if g is None:
        return out
    for tr in g.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        # with cboAnio/cboTrimestre = TODOS the grid gains leading Año (and Trimestre) columns
        i = next((k for k, c in enumerate(cells) if k >= 3 and re.match(r"\d{2}/\d{2}/\d{4}", c)), None)
        if i is None:
            continue
        ts = presentation_ts(cells[i])
        if ts is None:
            continue
        a = tr.find("a", href=True)
        out.append({"document": cells[i - 2], "expediente": cells[i - 1], "ts": ts, "lead": cells[:i - 3],
                    "href": a["href"].strip() if a else None})
    return out


def pager_targets(html: str | bytes) -> list[str]:
    """GridView pager postback arguments ('Page$2', ...) present in a result page."""
    return sorted(set(re.findall(r"grdInfoFinanciera(?:'|&#39;),(?:'|&#39;)(Page\$\d+)", html if isinstance(html, str)
                                 else html.decode("utf-8", "replace"))), key=lambda x: int(x.split("$")[1]))


def parse_header(text: str) -> dict[str, Any]:
    """'CONSOLIDADA | ANUAL AL 31 DE DICIEMBRE DEL 2025 ( EN MILES DE NUEVOS SOLES )' -> period/currency/scale."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().upper()
    m = re.search(r"AL\s+(\d{1,2})\s+DE\s+([A-Z]+)\s+(?:DEL?\s+)?(\d{4})", t)
    pe = date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))) if m and m.group(2) in _MONTHS else None
    ccy = "USD" if re.search(r"DOLAR", t) else ("PEN" if "SOLES" in t else ("EUR" if "EURO" in t else None))
    scale = 1e6 if "MILLONES" in t else (1e3 if "MILES" in t else 1.0)
    return {"period_end": pe, "currency": ccy, "scale": scale, "annual": "ANUAL" in t,
            "consolidated": "CONSOLIDAD" in t}


def _num(s: str) -> float | None:
    s = (s or "").strip().replace(",", "")
    if not s or s == "-":
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _col_period(head: str, pe: date, annual: bool, kind: str) -> tuple[date | None, date] | None:
    """Column header -> (start, end). Annual: '2025' / '2024'. Interim: '30/09/2025', '31/12/2024',
    'Del 01/07/2025 al 30/09/2025', 'Acumulado ...'."""
    h = head.strip()
    # spelled-out dates: '30 de Setiembre del 2025', 'Específico del 01 de Julio al 30 de Septiembre del 2025',
    # 'Acumulado del 01 de Enero al 30 de Septiembre del 2025'
    hu = unicodedata.normalize("NFKD", h).encode("ascii", "ignore").decode().upper()
    sp = [(int(d), _MONTHS[mo], int(y) if y else None) for d, mo, y in
          re.findall(r"(\d{1,2})\s+DE\s+([A-Z]+)(?:\s+(?:DEL?\s+)?(\d{4}))?", hu) if mo in _MONTHS]
    if sp:
        d2, m2, y2 = sp[-1]
        if y2 is None:
            return None
        end = date(y2, m2, d2)
        if len(sp) >= 2:
            d1, m1, y1 = sp[0]
            return date(y1 or (y2 if m1 <= m2 else y2 - 1), m1, d1), end
        return (None, end) if kind == "BS" else (date(end.year, 1, 1), end)
    ds = re.findall(r"(\d{2})/(\d{2})/(\d{4})", h)
    if len(ds) >= 2:
        (d1, m1, y1), (d2, m2, y2) = ds[0], ds[-1]
        return date(int(y1), int(m1), int(d1)), date(int(y2), int(m2), int(d2))
    if len(ds) == 1:
        d1, m1, y1 = ds[0]
        end = date(int(y1), int(m1), int(d1))
        return (None, end) if kind == "BS" else (date(end.year, 1, 1), end)
    m = re.fullmatch(r"(\d{4})", h)
    if m:
        y = int(m.group(1))
        end = date(y, pe.month, pe.day) if not (pe.month == 2 and pe.day == 29) else date(y, 2, 28)
        if kind == "BS":
            return None, end
        if annual:
            return date(y - 1, end.month, end.day) if end.month != 12 else date(y, 1, 1), end
        return date(y, 1, 1), end
    return None


def presentation_currency(company: str, report_pe: date | None, header_ccy: str | None) -> str | None:
    for until, ccy in SMV_CCY.get(company, []):
        if until is None or (report_pe is not None and report_pe <= until):
            return ccy
    return header_ccy


def statements_to_rows(html: str | bytes, *, source_id: str, source_entity_id: str, filing_id: str, form_type: str,
                       knowledge_date: date, fye_month: int | None = 12) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Detail page (Frm_DetalleInfoFinanciera) -> (header info, fundamentals_raw rows). source_entity_id is the SMV
    company id (used for the verified presentation-currency override)."""
    s = BeautifulSoup(html, "lxml")
    lab = s.find("span", id=lambda x: bool(x) and x.endswith("lblTrimestre")) or \
        s.find("span", id=lambda x: bool(x) and "lblBimestre" in x)
    info = parse_header(lab.get_text(" ", strip=True) if lab else s.get_text(" ", strip=True)[:3000])
    info["currency"] = presentation_currency(source_entity_id, info["period_end"], info["currency"])
    pe, ccy, scale = info["period_end"], info["currency"], info["scale"]
    rows: list[dict[str, Any]] = []
    if pe is None or ccy is None:
        return info, rows
    for gid, kind in _GRIDS.items():
        g = s.find("table", id=lambda x, gid=gid: bool(x) and x.endswith(gid))
        if g is None:
            continue
        trs = g.find_all("tr")
        if not trs:
            continue
        heads = [c.get_text(" ", strip=True) for c in trs[0].find_all(["th", "td"])]
        cols = [(i, _col_period(h, pe, info["annual"], kind)) for i, h in enumerate(heads) if i >= 2]
        seen: dict[str, int] = {}
        for tr in trs[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if len(cells) < 3 or not cells[0]:
                continue
            label = re.sub(r"\s+", " ", cells[0])
            sl = slug(label)
            seen[sl] = seen.get(sl, 0) + 1
            key = sl if seen[sl] == 1 else f"{sl}_{seen[sl]}"
            per_share = "por_accion" in sl
            ifrs = IFRS_MAP.get((kind, sl)) if seen[sl] == 1 else None
            if ifrs is None and kind == "IS" and seen[sl] == 1 and sl in _REVENUE_LABELS and                     source_entity_id in SMV_REVENUE_COMPANIES:
                ifrs = "Revenue"
            for i, per in cols:
                if per is None or i >= len(cells):
                    continue
                v = _num(cells[i])
                if v is None:
                    continue
                start, end = per
                if end > pe:
                    continue
                unit = f"{ccy}/shares" if per_share else ccy
                val = v if per_share else v * scale
                common = dict(source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id,
                              form_type=form_type, label=label, period_start=start, period_end=end, value=val,
                              unit=unit, knowledge_date=knowledge_date, fiscal_year=fiscal_year_for(end, fye_month),
                              fiscal_period=("FY" if start and 340 <= (end - start).days + 1 <= 380 else None),
                              consolidated=info["consolidated"])
                rows.append(fact_row(taxonomy="other", concept=f"smv:{kind}:{key}", **common))
                if ifrs and not (v == 0 and kind != "BS"):
                    rows.append(fact_row(taxonomy="ifrs-full", concept=ifrs, **common))
    return info, label_quarters(rows, fye_month)


@register_adapter
class SmvPeAdapter(PackageFilingAdapter):
    source_id = "smv_pe"
    regions = ("LATAM",)
    countries = ("PE",)
    base_url = BASE
    rate_limit = RateLimit(1.0, 1)
    min_year: ClassVar[int] = 2009
    _HDR: ClassVar[dict[str, str]] = {"Referer": FORM_URL, "Origin": BASE}

    max_prefetch: ClassVar[int] = 1000

    def fetch(self, since: Any = None, entities: Any = None) -> FetchResult:
        self._since = to_date(since) if since else None
        self._pages = {}
        return super().fetch(since=since, entities=entities)

    def entity_key(self, issuer: dict[str, Any]) -> str | None:
        for t in [issuer.get("yf_ticker"), *(issuer.get("yf_tickers") or [])]:
            if t in SMV_IDS:
                return SMV_IDS[t]
        return None

    def _post(self, data: dict[str, str]) -> bytes:
        r = self.http.post(FORM_URL, data=data, headers=self._HDR)
        return r.content

    def _search(self, company: str, periodo: str, on_page: Any = None) -> list[dict[str, Any]]:
        """Run the postback search; `on_page(rows)` is called right after each grid page is received, because the
        detail links (Frm_DetalleInfoFinanciera?data=...) resolve against the session's *current* result set."""
        html = self.http.get(FORM_URL, cache_ttl=0).content
        d = form_state(html)
        d.update({"__EVENTTARGET": _MC + "cboDenominacionSocial", _MC + "cboDenominacionSocial": company})
        html = self._post(d)
        d = form_state(html)
        if periodo == "A":
            d.update({"__EVENTTARGET": _MC + "cboPeriodo$1", _MC + "cboTipo": "C", _MC + "cboPeriodo": "A"})
            html = self._post(d)
            d = form_state(html)
        d.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "", _MC + "cboTipo": "C", _MC + "cboPeriodo": periodo,
                  _MC + "cboAnio": "-1", _MC + "cbBuscar": "Buscar"})
        if periodo == "T":
            d[_MC + "cboTrimestre"] = "-1"
        html = self._post(d)
        rows = parse_grid(html)
        if on_page:
            on_page(rows)
        for arg in pager_targets(html):  # GridView paging (if the result is long)
            d = form_state(html)
            d.update({"__EVENTTARGET": _MC + "grdInfoFinanciera", "__EVENTARGUMENT": arg})
            d.pop(_MC + "cbBuscar", None)
            page = parse_grid(self._post(d))
            if on_page:
                on_page(page)
            rows += page
        return rows

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        company = self.entity_key(issuer)
        if not company:
            return []
        done = load_state(self.source_id, self.settings).get("done", {})
        out: dict[str, dict[str, Any]] = {}
        self._pages: dict[str, bytes] = getattr(self, "_pages", {})
        self._since: date | None = getattr(self, "_since", None)

        def build(r: dict[str, Any], periodo: str, fc: str) -> dict[str, Any] | None:
            if not r["href"] or "DetalleInfoFinanciera" not in r["href"]:
                return None
            href = r["href"] if r["href"].startswith("http") else f"{BASE}/SIMV/{r['href'].lstrip('/')}"
            key = f"{company}:{r['expediente']}:{periodo}"
            return {"native_key": key, "knowledge_ts": r["ts"].isoformat(), "doc_url": href,
                    "suffix": ".html", "form_type": "SMV-EEFF-" + periodo, "form_class": fc,
                    "fiscal_period": "FY" if periodo == "A" else None, "period_end": None, "url": href,
                    "language": "es", "is_amendment": False, "expediente": r["expediente"],
                    "period_hint": "/".join(r.get("lead") or []) + f"/{periodo}"}

        for periodo, fc in (("A", "annual"), ("T", "quarterly")):
            def on_page(rows: list[dict[str, Any]], periodo: str = periodo, fc: str = fc) -> None:
                for r in rows:
                    f = build(r, periodo, fc)
                    if f is None or f["native_key"] in out:
                        continue
                    out[f["native_key"]] = f
                    kd = r["ts"].date()
                    if (f["native_key"] in done or f["native_key"] in self._pages or kd.year < self.min_year
                            or (self._since and kd < self._since) or len(self._pages) >= self.max_prefetch):
                        continue
                    self._pages[f["native_key"]] = self.http.get(f["doc_url"], cache_ttl=0,
                                                                 headers={"Referer": FORM_URL}).content
            self._search(company, periodo, on_page)
        fs = sorted(out.values(), key=lambda f: f["knowledge_ts"])
        first: set[str] = set()
        for f in fs:  # a later "Estados Financieros" for the same (year[, quarter]) is a re-filing
            h = f["period_hint"]
            f["is_amendment"] = h in first and h[:1].isdigit()
            first.add(h)
            prev = done.get(f["native_key"])
            if prev:  # period comes from the detail page at landing time; keep it for reparse runs
                f.update(period_end=prev.get("period_end"), fiscal_period=prev.get("fiscal_period"))
        return fs

    def download(self, filing: dict[str, Any]) -> bytes:
        payload = getattr(self, "_pages", {}).pop(filing["native_key"], None)
        if payload is None:
            raise RuntimeError("SMV detail page was not fetched with its search session (re-run picks it up)")
        return payload

    def to_doc(self, filing: dict[str, Any], payload: bytes) -> dict[str, Any]:
        # Kept as the landed raw "document": the statement HTML (decoded) + header info; facts are built in
        # facts_from_doc (needs filing id / knowledge date).
        info = parse_header(_header_text(payload))
        if info["period_end"] is None:
            raise RuntimeError("SMV detail page without a period header")
        check_period(filing.get("period_hint") or "", info)
        return {"documentInfo": {"source": "smv-html", "period_end": info["period_end"].isoformat(),
                                 "currency": info["currency"], "scale": info["scale"]},
                "html": payload.decode("utf-8", errors="replace"), "facts": {}}

    def _land(self, f: dict[str, Any]) -> dict[str, Any]:
        payload = self.download(f)
        doc = self.to_doc(f, payload)
        pe = date.fromisoformat(doc["documentInfo"]["period_end"])
        fp = f.get("fiscal_period") or ("FY" if f["form_class"] == "annual" else f"Q{(pe.month - 1) // 3 + 1}")
        if f["form_class"] == "quarterly" and pe.month == 12:
            fp = "Q4"
        safe_key = f["native_key"].replace("/", "_").replace(":", "_")[:120]
        path = self.save_raw("statements_html", safe_key, doc)
        self.log.info("smv_pe %s: period %s (%s)", f["native_key"], pe, doc["documentInfo"]["currency"])
        return {**f, "period_end": pe.isoformat(), "fiscal_period": fp, "_raw_path": str(path)}

    def facts_from_doc(self, filing: dict[str, Any], doc: dict[str, Any], issuer_fye: int | None,
                       fid: str, kd: date) -> list[dict[str, Any]]:
        _, rows = statements_to_rows(doc.get("html") or "", source_id=self.source_id,
                                     source_entity_id=filing["_entity"], filing_id=fid,
                                     form_type=filing.get("form_type") or "SMV-EEFF", knowledge_date=kd,
                                     fye_month=issuer_fye or 12)
        pe = to_date(filing.get("period_end"))
        return [r for r in rows if pe is None or r["period_end"] <= pe]


_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}


def check_period(hint: str, info: dict[str, Any]) -> None:
    """Guard against a detail page that does not belong to the grid row (session-bound links): the row's Año
    (and Trimestre) must match the statement header."""
    parts = hint.split("/")
    pe = info["period_end"]
    if not parts or not parts[0].isdigit():
        return
    year, periodo = int(parts[0]), parts[-1]
    q = _ROMAN.get(parts[1]) if len(parts) > 2 else None
    ok = pe.year == year and (info["annual"] if periodo == "A" else not info["annual"])
    if ok and q is not None:
        ok = (pe.month - 1) // 3 + 1 == q
    if not ok:
        raise RuntimeError(f"SMV detail page period {pe} ({'annual' if info['annual'] else 'interim'}) does not "
                           f"match grid row {hint}")


def _header_text(payload: bytes) -> str:
    s = BeautifulSoup(payload, "lxml")
    lab = s.find("span", id=lambda x: bool(x) and x.endswith("lblTrimestre")) or \
        s.find("span", id=lambda x: bool(x) and "lblBimestre" in x)
    return lab.get_text(" ", strip=True) if lab else s.get_text(" ", strip=True)[:3000]


def run(ctx: Any) -> Any:
    return run_adapter_job(SmvPeAdapter, ctx)
