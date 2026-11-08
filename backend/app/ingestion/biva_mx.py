"""Mexico: BIVA "banco de información" JSON API -> CNBV quarterly / annual XBRL (ifrs_mx taxonomy, ifrs-full concepts).

Research: docs/COMPANY_COVERAGE_OPTIONS.md §5 (MX), verified 2026-10-11. Keyless, public; BIVA republishes every CNBV/STIV
filing, so BMV-listed issuers are covered too.
  GET https://www.biva.mx/emisoras/banco-informacion/claves?clave=GMEX -> [{"id":1693,"clave":"GMEXICO"}]
  GET https://www.biva.mx/emisoras/banco-informacion?size=100&page=0&empresa=<id>&tipoInformacion=17   (15 = annual)
      content[]: idDocumento, fechaPublicacion (epoch ms), tipoDocumento ("Información trimestral 4D-2025",
      "Información financiera 3 - 2018", annual: "... 2025"), archivosXbrl[{url:"/storage/...xbrl", extension}]
Quarter labels: 1..4 = quarterly report (YTD + 3-month durations); "4D" = audited Q4 re-filing (Apr/May) -> both are
form_class 'quarterly', fiscal_period 'Q4' (form_type BIVA-Q4D distinguishes them); type 15 = annual report
(form_class 'annual', FY; GFNORTE only has these: 3 years of selected ifrs-full figures).
PIT: knowledge_ts = fechaPublicacion. The epoch is BIVA's wall-clock Mexico City time stored as if UTC (publication
hours cluster at 09:00 local), so it is shifted to UTC with America/Mexico_City -> never earlier than the real time.
Filings published before BIVA started (2018 Q1-Q2) carry the BIVA back-fill date (2018-10-23, flagged
`backfilled=True`): it is the date the document became public on BIVA, i.e. a late but valid conservative knowledge date.
Parsing: the instance is parsed directly (`_latam_xbrl.instance_to_oim`, no taxonomy download). Currency = fact unit
(GMEXICO reports USD, the rest MXN).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import run_adapter_job, to_date
from app.ingestion._latam_xbrl import instance_to_oim, label_quarters
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

BASE = "https://www.biva.mx"
LIST_API = f"{BASE}/emisoras/banco-informacion"
CLAVE_API = f"{BASE}/emisoras/banco-informacion/claves"
TYPE_QUARTERLY, TYPE_ANNUAL = 17, 15
MX_TZ = ZoneInfo("America/Mexico_City")
BACKFILL_DATE = date(2018, 10, 23)

#: yf_ticker -> (BIVA clave, BIVA empresa id), verified 2026-10-11 via the /claves endpoint.
BIVA_IDS: dict[str, tuple[str, int]] = {
    "GMEXICOB.MX": ("GMEXICO", 1693),
    "GFNORTEO.MX": ("GFNORTE", 1680),
    "CEMEXCPO.MX": ("CEMEX", 1786),
    "WALMEX.MX": ("WALMEX", 1656),
    "AMXB.MX": ("AMX", 1653),
    "FEMSAUBD.MX": ("FEMSA", 1782),
    # universe additions (ref-identity, 2026-10-11): exact clave matches from /claves, verified 2026-10-11
    "KOFUBL.MX": ("KOF", 1776), "GAPB.MX": ("GAP", 1784), "ASURB.MX": ("ASUR", 1687), "BIMBOA.MX": ("BIMBO", 1716),
    "GFINBURO.MX": ("GFINBUR", 1671), "AC.MX": ("AC", 1672), "ORBIA.MX": ("ORBIA", 1698), "OMAB.MX": ("OMA", 1913),
    "GRUMAB.MX": ("GRUMA", 1711), "PE&OLES.MX": ("PE&OLES", 1735), "KIMBERA.MX": ("KIMBER", 1781),
    "TLEVISACPO.MX": ("TLEVISA", 1765),
}


def clave_candidates(yf_ticker: str) -> list[str]:
    """'GAPB.MX' -> ['GAPB', 'GAP', 'GA']: BMV series suffixes (A/B/O/L/UBL/UBD/CPO/...) stripped one char at a time."""
    root = yf_ticker.split(".")[0].upper()
    out = [root]
    for suf in ("CPO", "UBL", "UBD", "UB"):
        if root.endswith(suf) and len(root) > len(suf) + 1:
            out.append(root[: -len(suf)])
    while len(root) > 3:
        root = root[:-1]
        out.append(root)
    return list(dict.fromkeys(out))
_Q_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def parse_tipo(tipo: str, info_type: int) -> tuple[str, str, str, date] | None:
    """tipoDocumento -> (form_type, form_class, fiscal_period, period_end) or None."""
    t = (tipo or "").strip()
    if info_type == TYPE_ANNUAL:
        m = re.search(r"(\d{4})\s*$", t)
        return ("BIVA-AR", "annual", "FY", date(int(m.group(1)), 12, 31)) if m else None
    m = re.search(r"\b([1-4])\s*(D?)\s*-\s*(\d{4})\s*$", t)
    if not m:
        return None
    q, audited, y = int(m.group(1)), m.group(2) == "D", int(m.group(3))
    return (f"BIVA-Q{q}{'D' if audited else ''}", "quarterly", f"Q{q}", date(y, *_Q_END[q]))


def publication_ts(epoch_ms: int | float) -> datetime:
    """BIVA fechaPublicacion (Mexico City wall clock encoded as UTC epoch) -> real UTC timestamp."""
    wall = datetime.fromtimestamp(epoch_ms / 1000, UTC).replace(tzinfo=None)
    return wall.replace(tzinfo=MX_TZ).astimezone(UTC)


def select_filings(content: list[dict[str, Any]], info_type: int, clave: str) -> list[dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in content:
        cls = parse_tipo(r.get("tipoDocumento") or "", info_type)
        if cls is None or not r.get("fechaPublicacion"):
            continue
        xb = next((a["url"] for a in r.get("archivosXbrl") or [] if str(a.get("extension", "")).lower() == "xbrl"), None)
        kts = publication_ts(r["fechaPublicacion"])
        doc_id = r.get("idDocumento") or r.get("id")
        native = f"{clave}:{doc_id}"
        pe = cls[3]
        rec = {"native_key": native, "knowledge_ts": kts.isoformat(), "doc_url": f"{BASE}{xb}" if xb else None,
               "suffix": ".xbrl", "form_type": cls[0], "form_class": cls[1], "fiscal_period": cls[2],
               "period_end": pe.isoformat(), "url": f"{BASE}{xb}" if xb else f"{BASE}/emisoras/empresas/{clave}",
               "language": "es", "is_amendment": False, "title": r.get("tipoDocumento"),
               "backfilled": kts.date() <= BACKFILL_DATE and (kts.date() - pe).days > 120}
        out.setdefault(native, rec)
    return sorted(out.values(), key=lambda f: f["knowledge_ts"])


@register_adapter
class BivaMxAdapter(PackageFilingAdapter):
    source_id = "biva_mx"
    regions = ("LATAM",)
    countries = ("MX",)
    base_url = BASE
    rate_limit = RateLimit(1.0, 1)
    min_year: ClassVar[int] = 2018

    def __init__(self, *args: Any, annual_reports: str = "fallback", **kw: Any):
        """annual_reports: 'fallback' (type-15 annual report only when an issuer has no quarterly XBRL, e.g. GFNORTE),
        'all' or 'none'."""
        super().__init__(*args, **kw)
        self.annual_reports = str(annual_reports).lower()

    def _ids(self, issuer: dict[str, Any]) -> tuple[str, int] | None:
        for t in [issuer.get("yf_ticker"), *(issuer.get("yf_tickers") or [])]:
            if t in BIVA_IDS:
                return BIVA_IDS[t]
        return self._discover(issuer)

    def _discover(self, issuer: dict[str, Any]) -> tuple[str, int] | None:
        """Unmapped .MX issuer: exact clave match on /claves for the ticker root with series suffixes stripped
        (confidence: exact clave match only; ambiguous/no match -> None and the issuer is reported as unmapped)."""
        cache = getattr(self, "_disc", None)
        if cache is None:
            cache = self._disc = {}
        for t in [issuer.get("yf_ticker"), *(issuer.get("yf_tickers") or [])]:
            if not t or not t.upper().endswith(".MX"):
                continue
            if t in cache:
                return cache[t]
            hit = None
            for cand in clave_candidates(t):
                try:
                    res = self.http.get(CLAVE_API, params={"clave": cand}, cache_ttl=7 * 86400).json() or []
                except Exception:  # noqa: BLE001
                    break
                exact = [r for r in res if str(r.get("clave", "")).upper() == cand]
                if len(exact) == 1:
                    hit = (exact[0]["clave"], int(exact[0]["id"]))
                    self.log.info("biva_mx: discovered %s -> %s (exact clave)", t, hit)
                    break
            cache[t] = hit
            if hit:
                return hit
        return None

    def entity_key(self, issuer: dict[str, Any]) -> str | None:
        ids = self._ids(issuer)
        return ids[0] if ids else None

    def _list(self, empresa: int, info_type: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(20):
            r = self.http.get(LIST_API, params={"size": 100, "page": page, "empresa": empresa,
                                                "tipoInformacion": info_type},
                              cache_ttl=6 * 3600, raise_for_status=False)
            if r.status_code == 404:  # issuer has no document of this type
                return out
            if r.status_code >= 400:
                raise RuntimeError(f"BIVA list HTTP {r.status_code} empresa={empresa} tipo={info_type}")
            j = r.json()
            out += j.get("content") or []
            if page + 1 >= int(j.get("totalPages") or 1):
                break
        return out

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        ids = self._ids(issuer)
        if not ids:
            return []
        clave, empresa = ids
        fs = select_filings(self._list(empresa, TYPE_QUARTERLY), TYPE_QUARTERLY, clave)
        if self.annual_reports == "all" or (self.annual_reports == "fallback" and not any(f["doc_url"] for f in fs)):
            fs += select_filings(self._list(empresa, TYPE_ANNUAL), TYPE_ANNUAL, clave)
        return fs

    def to_doc(self, filing: dict[str, Any], payload: bytes) -> dict[str, Any]:
        return instance_to_oim(payload)

    def facts_from_doc(self, filing: dict[str, Any], doc: dict[str, Any], issuer_fye: int | None,
                       fid: str, kd: date) -> list[dict[str, Any]]:
        rows = super().facts_from_doc({**filing, "period_end": filing.get("period_end")}, doc, issuer_fye or 12, fid, kd)
        pe = to_date(filing.get("period_end"))
        # PIT guard: drop anything dated after the report period (none expected; protects against mis-tagged contexts)
        rows = [r for r in rows if pe is None or r["period_end"] <= pe]
        return label_quarters(rows, issuer_fye or 12)


def run(ctx: Any) -> Any:
    return run_adapter_job(BivaMxAdapter, ctx)
