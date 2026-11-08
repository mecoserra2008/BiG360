"""France AMF BDIF directors' dealings ('DD' declarations) -> filings_index (metadata only, best effort).

Source research: docs/DATA_SOURCES_AMERICAS_EUROPE.md §5.5 (undocumented SPA backend, verified 2026-10-10).
  GET https://bdif.amf-france.org/back/api/v1/informations?typesInformation=DD&from=0&size=100
  -> {"total":..,"result":[{"numero":"2026DD1143419","datePublication":"2026-10-09T18:10:03+02:00",
                            "dateInformation":..,"documents":[{"path":"...pdf"}],"societes":[{"jeton":"RS..","raisonSociale":..}]}]}
Transaction details live only in the PDF (fixed AMF template); PDF parsing is deferred, so this adapter indexes the
declarations (who/when) for event studies and leaves insider_transactions to a future PDF parser.
PIT: knowledge_ts = datePublication. source_entity_id = 'amf:<societe jeton>' (no LEI/ISIN in the metadata).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.ratelimit import RateLimit
from app.ingestion._global_common import (filing_row, frame, load_state, run_adapter_job, safe_call, save_state,
                                          to_date)
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter

API = "https://bdif.amf-france.org/back/api/v1/informations"
DOC = "https://bdif.amf-france.org/back/api/v1/documents/"


@register_adapter
class AmfBdifAdapter(SourceAdapter):
    source_id = "eu_pdmr_amf"
    regions = ("EU",)
    produces = ("filings_index",)
    rate_limit = RateLimit(1.0, 1)

    def __init__(self, *args: Any, max_pages: int = 20, **kw: Any):
        super().__init__(*args, **kw)
        self.http._client.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        self.http._client.headers["Accept"] = "application/json"
        self.max_pages = int(max_pages)

    def discover(self) -> pl.DataFrame:
        return self.discover_frame([], self.source_id)

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        res = FetchResult(self.source_id, started_at=datetime.now(UTC))
        state = load_state(self.source_id, self.settings)
        since_d = to_date(since) or (to_date(state.get("watermark")) - timedelta(days=2) if state.get("watermark")
                                     else datetime.now(UTC).date() - timedelta(days=14))
        items: list[dict[str, Any]] = []

        def page(n: int) -> list[dict[str, Any]]:
            j = self.http.get(API, params={"typesInformation": "DD", "from": n * 100, "size": 100}, cache_ttl=1800).json()
            if "result" not in j:
                raise ValueError(f"AMF schema change: {list(j)[:5]}")
            return j["result"]

        for n in range(self.max_pages):
            got = safe_call(lambda n=n: page(n), res, f"page:{n}", self.log)
            if not got:
                break
            items += got
            last = to_date(got[-1].get("datePublication"))
            if last and last < since_d:
                break
        items = [x for x in items if (to_date(x.get("datePublication")) or date.min) >= since_d]
        if entities:
            ents = {e.upper() for e in entities}
            items = [x for x in items if any((s.get("jeton") or "").upper() in ents or
                                             (s.get("raisonSociale") or "").upper() in ents for s in x.get("societes") or [])]
        res.raw_paths.append(self.save_raw("informations", f"dd_{datetime.now(UTC):%H%M%S}", items))
        res.meta["items"] = items
        if items and not entities:
            res.watermark = max(to_date(x["datePublication"]) for x in items).isoformat()
            state["watermark"] = res.watermark
            save_state(self.source_id, state, self.settings)
        return res

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        rows = []
        for x in fetched.meta.get("items", []):
            ts = datetime.fromisoformat(x["datePublication"]).astimezone(UTC)
            soc = (x.get("societes") or [{}])[0]
            doc = (x.get("documents") or [{}])[0]
            rows.append(filing_row(
                source_id=self.source_id, native_key=x["numero"], source_entity_id=f"amf:{soc.get('jeton') or 'na'}",
                form_type="DD", form_class="insider", knowledge_date=ts.date(), knowledge_ts=ts,
                language=(x.get("langue") or "fr").lower()[:2], url=DOC + doc["path"] if doc.get("path") else None,
                items=[soc.get("raisonSociale") or ""]))
        return {"filings_index": frame(rows, "filings_index")}


def run(ctx: Any) -> Any:
    return run_adapter_job(AmfBdifAdapter, ctx)
