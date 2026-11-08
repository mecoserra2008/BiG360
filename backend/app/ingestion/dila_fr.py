"""France: DILA / AMF "info-financière" OpenDataSoft API (the French OAM feed) -> ESEF annual reports (URD / AFR).

Research: docs/COMPANY_COVERAGE_OPTIONS.md §3 (FR), verified 2026-10-11.
  GET https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records
      ?where=identificationsociete_iso_cd_lei="<LEI>" and (fichierdecontenu_inf_fic_nom like "*.zip" or ... "*.xbri")
      &order_by=informationdeposee_inf_dat_emt desc
  fields: informationdeposee_inf_dat_emt (deposit timestamp, UTC offset) = knowledge_ts, informationdeposee_inf_stp_inf
  (300001-7 URD, 002002/003000 AFR, 002003/004000 half-year), url_de_recuperation (ESEF zip / .xbri).
Licence Ouverte (open, commercial use OK). Keys: none. source_entity_id = LEI. Accounting standard IFRS (ifrs-full);
currency = fact unit. URDs are often deposited twice (FR + EN): the earliest deposit per year is kept (PIT).

DISABLED 2026-11-07 (docs/COVERAGE_V3_PLAN.md A9, docs/it/ROBOTS_COMPLIANCE.md): www.info-financiere.gouv.fr
robots.txt has `Disallow: /api/` (the search API) and the package mirror fr.ftp.opendatasoft.com is `Disallow: /`.
The job dila_fr_filings returns skipped (reason code source_disallowed) without any request; app.core.robots_policy
blocks both hosts. French ESEF annual reports keep coming from filings.xbrl.org (esef_xbrl_org); dila_fr filings
ingested before the switch stay in filings_index / fundamentals_raw (source_id dila_fr).
"""

from __future__ import annotations

import re
from typing import Any

from app.core.ratelimit import RateLimit
from app.core.robots_policy import disabled_note, skipped_job
from app.ingestion._global_common import run_adapter_job, to_date
from app.ingestion._oam_common import PackageFilingAdapter
from app.ingestion.base import register_adapter

API = "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records"
_FIELDS = ("informationdeposee_inf_dat_emt,informationdeposee_inf_stp_inf,subtype_of_information,"
           "informationdeposee_inf_tit_inf,url_de_recuperation,informationdeposee_inf_lng_inf,"
           "informationdeposee_inf_upg_inf_inf_upg_sts,uin_idt_uin,identificationsociete_iso_cd_lei")
_ANNUAL = {"300001", "300002", "300003", "300004", "300005", "300006", "300007", "002002", "003000"}
_HALF = {"002003", "004000"}
_LANG = {"Français": "fr", "Anglais": "en"}


def classify(rec: dict[str, Any]) -> tuple[str, str, str] | None:
    """-> (form_type, form_class, fiscal_period) or None for non-periodic documents."""
    codes = rec.get("informationdeposee_inf_stp_inf") or ""
    codes = set(codes if isinstance(codes, list) else re.findall(r"\d{6}", str(codes)))
    sub = (rec.get("subtype_of_information") or "").lower()
    if codes & _HALF or "half" in sub:
        return "HFR", "semiannual", "H1"
    if codes & _ANNUAL or "registration document" in sub or "annual financial report" in sub:
        return ("URD" if any(c.startswith("3000") for c in codes) or "registration" in sub else "AFR"), "annual", "FY"
    return None


def select_filings(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records -> filing dicts. Deposits of the same (form_class, deposit year) share a `group`: the base adapter
    converts the earliest one and falls back to the next only if it is broken (e.g. an empty zip)."""
    out: dict[tuple, dict[str, Any]] = {}
    for r in sorted(records, key=lambda x: x.get("informationdeposee_inf_dat_emt") or ""):
        url = r.get("url_de_recuperation") or ""
        if not url.lower().endswith((".zip", ".xbri")):
            continue
        cls = classify(r)
        kts = r.get("informationdeposee_inf_dat_emt")
        if cls is None or not kts:
            continue
        key = (cls[1], kts[:4])
        native = r.get("uin_idt_uin") or url.rsplit("/", 1)[-1].split(".")[0]
        out[(key, native)] = {
            "group": f"{key[0]}:{key[1]}",
            "native_key": str(native), "knowledge_ts": kts, "doc_url": url,
            "suffix": ".xbri" if url.lower().endswith(".xbri") else ".zip",
            "form_type": cls[0], "form_class": cls[1], "fiscal_period": cls[2], "period_end": None, "url": url,
            "language": _LANG.get(r.get("informationdeposee_inf_lng_inf") or ""),
            "is_amendment": (r.get("informationdeposee_inf_upg_inf_inf_upg_sts") or "NEW") != "NEW",
            "title": r.get("informationdeposee_inf_tit_inf"),
        }
    return sorted(out.values(), key=lambda f: f["knowledge_ts"])


@register_adapter
class DilaFrAdapter(PackageFilingAdapter):
    source_id = "dila_fr"
    regions = ("EU",)
    countries = ("FR",)
    base_url = "https://www.info-financiere.gouv.fr"
    rate_limit = RateLimit(1.0, 1)

    def fetch(self, since: Any = None, entities: Any = None) -> Any:
        note = disabled_note(self.source_id)
        if note:  # A9 2026-11-07: robots.txt disallows the API and the package mirror -> never request them
            from datetime import UTC, datetime  # noqa: PLC0415

            from app.ingestion.base import FetchResult  # noqa: PLC0415

            res = FetchResult(self.source_id, started_at=datetime.now(UTC))
            res.meta.update(filings=[], n_issuers=0, disabled="source_disallowed", note=note)
            return res
        return super().fetch(since=since, entities=entities)   # legacy path (tests; a written permission)

    def _query(self, where: str) -> list[dict[str, Any]]:
        j = self.http.get(API, params={"where": where, "order_by": "informationdeposee_inf_dat_emt desc",
                                       "limit": 100, "select": _FIELDS}, cache_ttl=6 * 3600).json()
        return j.get("results") or []

    def list_filings(self, issuer: dict[str, Any]) -> list[dict[str, Any]]:
        files = '(fichierdecontenu_inf_fic_nom like "*.zip" or fichierdecontenu_inf_fic_nom like "*.xbri")'
        recs: list[dict[str, Any]] = []
        for lei in issuer.get("leis") or [issuer.get("lei")]:
            recs += self._query(f'identificationsociete_iso_cd_lei="{lei}" and {files}')
        if not recs and issuer.get("isin"):
            recs = self._query(f'identificationsociete_iso_cd_isi="{issuer["isin"]}" and {files}')
        return [f for f in select_filings(recs) if (to_date(f["knowledge_ts"]) or to_date("1900-01-01")).year >= 2019]


def run(ctx: Any) -> Any:
    """Job dila_fr_filings: disabled (A9 2026-11-07, robots.txt `Disallow: /api/`) -> skipped, no request."""
    return skipped_job(DilaFrAdapter.source_id) or run_adapter_job(DilaFrAdapter, ctx)
