"""Chile: CMF (Comisión para el Mercado Financiero) per-issuer IFRS XBRL financial statements, quarterly 2009->.

Research: docs/COMPANY_COVERAGE_OPTIONS.md §5 (CL), verified 2026-10-11. Keyless, public.
  Filing log (PIT):  GET https://www.cmfchile.cl/institucional/inc/inf_financiera/ifrs/bitacora_ifrs.php
                         ?rut=<RUT>&tipoentidad=RVEMI&vig=VI&periodo=<YYYYMM>&tb=C
      rows "05/03/2025 00:32:00 | Primer Envio | Estados Financieros PDF, ..." (+ later re-sends, e.g.
      "10/04/2025 16:23:01 | Consistencia entre formatos PDF y XBRL | Estados Financieros XBRL").
  Documents:         POST https://www.cmfchile.cl/institucional/mercados/entidad.php?mercado=V&rut=<RUT>
                         &tipoentidad=RVEMI&vig=VI&control=svs&pestania=3   form mm=03|06|09|12, aa=YYYY, tipo=C,
                         tipo_norma=IFRS  -> link "Estados financieros (XBRL)" = safec_ifrs_verarchivo.php?auth=...
                         (zip with <rut>_<yyyymm>_C.xbrl; ifrs-full + cl-ci). ~half the POSTs answer 302 with an
                         empty body -> retried after 1.5-3 s. The auth link is per-response, so the POST happens at
                         download time (doc_url is the entity page + '#<period>').
PIT: knowledge_ts = "Primer Envio" timestamp (America/Santiago -> UTC): the date the statements (PDF/Excel) became
public. Later XBRL-only re-sends ("Consistencia entre formatos PDF y XBRL") do not change the knowledge date; a later
re-send of the statements themselves (PDF/Excel) moves knowledge_ts to that re-send and marks the filing as amended
(conservative: the XBRL we download is the latest version). Periods without a log entry are skipped (no POST).
Consolidated (tipo=C) statements only. Currency = fact unit (SQM reports USD).
"""

from __future__ import annotations

import re
import time
from datetime import UTC, date, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job, to_date
from app.ingestion._latam_xbrl import instance_to_oim, label_quarters
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import FetchResult, register_adapter

BASE = "https://www.cmfchile.cl"
ENTITY_URL = BASE + "/institucional/mercados/entidad.php"
BITACORA_URL = BASE + "/institucional/inc/inf_financiera/ifrs/bitacora_ifrs.php"
DOC_BASE = BASE + "/institucional/inc/inf_financiera/ifrs/"
CL_TZ = ZoneInfo("America/Santiago")
#: yf_ticker -> CMF RUT (without check digit), verified 2026-10-11.
CMF_RUTS: dict[str, str] = {
    "SQM-B.SN": "93007000",
    # universe additions 2026-10-11 (RUT verified on the CMF RVEMI entity page, razon social matches)
    "FALABELLA.SN": "90749000", "COPEC.SN": "90690000", "CENCOSUD.SN": "93834000", "CMPC.SN": "90222000",
    "LTM.SN": "89862200", "ENELCHILE.SN": "76536353",
}
#: Banks (Banco de Chile 97004000, Santander-Chile 97036000, BCI 97006000) are CMF tipoentidad=BANCO: their statements
#: follow the banking Compendio de Normas Contables and are not published as IFRS XBRL on the pestania=3 page, so they
#: are not mapped here (BCH/BSAC file 20-F with the SEC -> sec_fsds; BCI has no free structured source).
_FP = {3: ("Q1", "quarterly"), 6: ("Q2", "quarterly"), 9: ("Q3", "quarterly"), 12: ("FY", "annual")}
_END = {3: 31, 6: 30, 9: 30, 12: 31}
_TS = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})(?::(\d{2}))?")


class NoXbrl(RuntimeError):
    """The period's document list has no XBRL file (e.g. early IFRS years)."""


def parse_bitacora(html: str | bytes) -> list[dict[str, Any]]:
    """Filing-log rows -> [{'ts': aware datetime (UTC), 'reason': str, 'items': str}] (oldest first)."""
    s = BeautifulSoup(html, "lxml")
    out = []
    for tr in s.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        if len(cells) < 2:
            continue
        m = _TS.search(cells[0])
        if not m:
            continue
        d, mo, y, hh, mi, ss = m.groups()
        local = datetime(int(y), int(mo), int(d), int(hh), int(mi), int(ss or 0), tzinfo=CL_TZ)
        out.append({"ts": local.astimezone(UTC), "reason": cells[1], "items": cells[2] if len(cells) > 2 else ""})
    return sorted(out, key=lambda r: r["ts"])


def knowledge_from_log(rows: list[dict[str, Any]]) -> tuple[datetime, bool, datetime | None] | None:
    """-> (knowledge_ts, is_amendment, last_xbrl_change_ts) or None when there is no 'Primer Envio'."""
    first = next((r for r in rows if "primer env" in r["reason"].lower()), None)
    if first is None:
        return None
    kts, amended = first["ts"], False
    later = [r for r in rows if r["ts"] > first["ts"]]
    restated = [r for r in later if re.search(r"Estados Financieros (PDF|Excel)", r["items"], re.I)]
    if restated:
        kts, amended = restated[-1]["ts"], True
    xbrl = [r["ts"] for r in later if "xbrl" in r["items"].lower()]
    return kts, amended, (xbrl[-1] if xbrl else None)


def xbrl_link(html: str) -> str | None:
    """'Estados financieros (XBRL)' link from a pestania=3 POST response; raises NoXbrl when documents exist but no
    XBRL; returns None when the response carries no document list at all (the flaky 302 case)."""
    links = re.findall(r'href="([^"]*safec_ifrs_verarchivo\.php[^"]*)"\s*>([^<]*)<', html)
    if not links:
        return None
    for href, label in links:
        if "xbrl" in label.lower():
            return DOC_BASE + href.replace("../inc/inf_financiera/ifrs/", "").replace("&amp;", "&")
    raise NoXbrl("no 'Estados financieros (XBRL)' document for this period")


@register_adapter
class CmfClAdapter(PackageFilingAdapter):
    source_id = "cmf_cl"
    regions = ("LATAM",)
    countries = ("CL",)
    base_url = BASE
    rate_limit = RateLimit(0.5, 1)
    min_year: ClassVar[int] = 2009
    first_year: ClassVar[int] = 2009
    post_retries: ClassVar[int] = 6

    def __init__(self, *args: Any, **kw: Any):
        super().__init__(*args, **kw)
        self._since: date | None = None

    def entity_key(self, issuer: dict[str, Any]) -> str | None:
        for t in [issuer.get("yf_ticker"), *(issuer.get("yf_tickers") or [])]:
            if t in CMF_RUTS:
                return CMF_RUTS[t]
        return None

    def fetch(self, since: Any = None, entities: Any = None) -> FetchResult:
        self._since = to_date(since) if since else None
        return super().fetch(since=since, entities=entities)

    def _periods(self) -> list[date]:
        today = datetime.now(UTC).date()
        lo = date(max(self.first_year, (self._since.year - 1) if self._since else self.first_year), 1, 1)
        out = []
        for y in range(lo.year, today.year + 1):
            for m in (3, 6, 9, 12):
                pe = date(y, m, _END[m])
                if (today - pe).days >= 20:
                    out.append(pe)
        return out

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        rut = self.entity_key(issuer)
        if not rut:
            return []
        out = []
        for pe in self._periods():
            per = f"{pe.year}{pe.month:02d}"
            # closed periods never change their log much: cache 30 days; recent ones 6 h
            ttl = 30 * 86400 if (datetime.now(UTC).date() - pe).days > 400 else 6 * 3600
            r = self.http.get(BITACORA_URL, params={"rut": rut, "tipoentidad": "RVEMI", "vig": "VI", "periodo": per,
                                                    "tb": "C"}, cache_ttl=ttl)
            k = knowledge_from_log(parse_bitacora(r.content))
            if k is None:
                continue
            kts, amended, xbrl_ts = k
            fp, fc = _FP[pe.month]
            page = f"{ENTITY_URL}?mercado=V&rut={rut}&tipoentidad=RVEMI&vig=VI&control=svs&pestania=3"
            out.append({"native_key": f"{rut}:{per}:C", "knowledge_ts": kts.isoformat(), "doc_url": f"{page}#{per}",
                        "suffix": ".zip", "form_type": "CMF-EEFF-" + ("A" if fc == "annual" else "Q"),
                        "form_class": fc, "fiscal_period": fp, "period_end": pe.isoformat(), "url": page,
                        "language": "es", "is_amendment": amended, "rut": rut, "period": per,
                        "xbrl_changed_ts": xbrl_ts.isoformat() if xbrl_ts else None})
        return out

    def download(self, filing: dict[str, Any]) -> bytes:
        per = filing["period"]
        url = f"{ENTITY_URL}?mercado=V&rut={filing['rut']}&tipoentidad=RVEMI&vig=VI&control=svs&pestania=3"
        form = {"mm": per[4:], "aa": per[:4], "tipo": "C", "tipo_norma": "IFRS"}
        link = None
        for attempt in range(self.post_retries):
            r = self.http.post(url, data=form, raise_for_status=False)
            link = xbrl_link(r.content.decode("utf-8", errors="replace")) if r.status_code == 200 else None
            if link:
                break
            time.sleep(1.5 * (attempt + 1))
        if not link:
            raise RuntimeError(f"CMF document list unavailable after {self.post_retries} POSTs ({per})")
        payload = self.http.get(link, cache_ttl=0).content
        if payload[:2] != b"PK" and b"xbrl" not in payload[:2000].lower():
            raise RuntimeError(f"CMF XBRL download is not a zip/instance ({len(payload)} bytes)")
        return payload

    def _land(self, f: dict[str, Any]) -> dict[str, Any]:
        try:
            return super()._land(f)
        except NoXbrl:
            self.log.info("cmf_cl %s: no XBRL document (metadata only)", f["native_key"])
            return {**f, "_raw_path": None, "no_xbrl": True}

    def to_doc(self, filing: dict[str, Any], payload: bytes) -> dict[str, Any]:
        return instance_to_oim(payload)

    def facts_from_doc(self, filing: dict[str, Any], doc: dict[str, Any], issuer_fye: int | None,
                       fid: str, kd: date) -> list[dict[str, Any]]:
        rows = super().facts_from_doc(filing, doc, issuer_fye or 12, fid, kd)
        pe = to_date(filing.get("period_end"))
        rows = [r for r in rows if pe is None or r["period_end"] <= pe]
        return label_quarters(rows, issuer_fye or 12)


def run(ctx: Any) -> Any:
    return run_adapter_job(CmfClAdapter, ctx)
