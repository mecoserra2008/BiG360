"""UK Companies House REST API -> filings_index (accounts filing history) + discover (SIC codes, status).

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §6.1. Auth: HTTP Basic with the API key as user name and an
empty password (key never in URLs/logs). 600 requests / 5 min. Optional key COMPANIES_HOUSE_APIKEY: without it the
adapter raises MissingApiKey and the job reports status 'skipped' with 'missing key'.
  GET /company/{number}                                  profile (sic_codes, company_status, accounts.next_due ...)
  GET /company/{number}/filing-history?category=accounts  items[]: transaction_id, date, type (AA), description,
                                                          action_date (period end), links.document_metadata
Listed-PLC group accounts are better sourced from FCA NSM ESEF packages (fca_nsm adapter); many PLC accounts here are
PDFs, so no fundamentals are parsed from Companies House (iXBRL small-company accounts are out of MVP scope).
PIT: knowledge_date = filing 'date'. source_entity_id = 8-char company number (uk_company_number).
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import filing_row, frame, run_adapter_job, safe_call, to_date
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

BASE = "https://api.company-information.service.gov.uk"


def ch_number(v: str) -> str:
    u = v.strip().upper()
    return u.zfill(8) if u.isdigit() else u


@register_adapter
class CompaniesHouseAdapter(SourceAdapter):
    source_id = "companies_house"
    regions = ("UK",)
    produces = ("filings_index",)
    requires_keys = ("companies_house_apikey",)
    base_url = BASE
    rate_limit = RateLimit(1.8, 5)

    def _auth(self) -> dict[str, str]:
        tok = base64.b64encode(f"{self.key('companies_house_apikey')}:".encode()).decode()
        return {"Authorization": f"Basic {tok}"}

    def _get(self, path: str, params: dict[str, Any] | None = None, ttl: int = 86400) -> Any:
        return self.http.get(path, params=params, headers=self._auth(), cache_ttl=ttl).json()

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def profiles(self, numbers: Sequence[str]) -> pl.DataFrame:
        self.check_keys()
        rows = []
        for n in numbers:
            p = self._get(f"/company/{ch_number(n)}")
            rows.append({"source_entity_id": p.get("company_number"), "name": p.get("company_name"), "country": "GB",
                         "industry_code": ",".join(p.get("sic_codes") or []), "industry_scheme": "uk_sic",
                         "is_active": p.get("company_status") == "active"})
        return self.discover_frame(rows, self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        self.check_keys()
        if not entities:
            res.meta["message"] = "no UK company numbers given (pass entities=...; seed has no company numbers)"
            return res
        items = []
        for n in entities:
            num = ch_number(n)

            def one(num=num) -> None:
                j = self._get(f"/company/{num}/filing-history", {"category": "accounts", "items_per_page": 100}, ttl=6 * 3600)
                self.save_raw("filing_history", num, j)
                for it in j.get("items") or []:
                    items.append({**it, "_company": num})
            safe_call(one, res, num, self.log)
        res.meta["items"] = items
        res.meta["since"] = str(since) if since else None
        res.watermark = datetime.now(UTC).date().isoformat()
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        since = to_date(fetched.meta.get("since"))
        rows = []
        for it in fetched.meta.get("items", []):
            kd = to_date(it.get("date"))
            if kd is None or (since and kd < since):
                continue
            pe = to_date(it.get("action_date"))
            doc = ((it.get("links") or {}).get("document_metadata"))
            rows.append(filing_row(source_id=self.source_id, native_key=it["transaction_id"], source_entity_id=it["_company"],
                                   form_type=it.get("type") or "AA", form_class="annual", knowledge_date=kd, period_end=pe,
                                   fiscal_year=pe.year if pe else None, fiscal_period="FY", language="en", url=doc,
                                   items=[it.get("description") or ""]))
        return {"filings_index": frame(rows, "filings_index")}


def run(ctx: Any) -> Any:
    return run_adapter_job(CompaniesHouseAdapter, ctx)
