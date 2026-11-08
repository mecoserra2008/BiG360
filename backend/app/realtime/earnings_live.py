"""SEC 8-K Item 2.02 (results of operations) -> live earnings-release signal (roadmap #6). Consumer group `earnings`.

For each new 8-K / 8-K/A on `filings` with Item 2.02 and a linked issuer:
1. look up the issuer's pending MODEL-BASED expectations (earnings_expectations, primary model, status pending, latest
   period ending 4-130 days before the filing) for diluted EPS and revenue;
2. publish `signals` (type `earnings`, key earnings:<accession>, ckey earnings:<issuer>) with the release time, the
   session vs the US regular session, the expected period and the model expectation + 80% band.
The surprise itself needs the structured actual: the press-release exhibit is not XBRL-tagged, so the actual (and
the surprise, alert `earnings_surprise`) arrives with the 10-Q / 10-K XBRL in the nightly earnings_expectations run.
Cheap: one lake read per issuer, cached for `cache_s` (default 1 h); no HTTP.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer, ts_of
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)
LABEL = "Model-based expectation (not consensus)"


def is_earnings_release(p: dict[str, Any]) -> bool:
    form = (p.get("form_type") or "").upper()
    items = [str(x) for x in (p.get("items") or [])]
    return p.get("source_id") == "sec_edgar" and form in ("8-K", "8-K/A") and any(i.startswith("2.02") for i in items)


def pending_expectations(settings: Any, issuer_id: str, filed: date) -> dict[str, Any]:
    import polars as pl  # noqa: PLC0415

    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        x = (scan_table("earnings_expectations", settings)
             .filter((pl.col("issuer_id") == issuer_id) & pl.col("is_primary")
                     & (pl.col("period_end") <= filed - timedelta(days=4))
                     & (pl.col("period_end") >= filed - timedelta(days=130)))
             .select("item_id", "period_end", "fiscal_period", "fiscal_year", "model", "expectation", "lo80", "hi80",
                     "currency", "status")
             .collect())
    except Exception as e:  # noqa: BLE001 - table not built yet
        log.debug("earnings_expectations unavailable: %s", e)
        return {}
    if x.is_empty():
        return {}
    pe = x.get_column("period_end").max()
    x = x.filter(pl.col("period_end") == pe)
    out: dict[str, Any] = {"period_end": str(pe), "fiscal_period": x.get_column("fiscal_period").drop_nulls().head(1).to_list()[0]
                           if x.get_column("fiscal_period").drop_nulls().len() else None, "items": {}}
    for r in x.iter_rows(named=True):
        out["items"][r["item_id"]] = {"expectation": r["expectation"], "lo80": r["lo80"], "hi80": r["hi80"],
                                      "model": r["model"], "currency": r["currency"], "status": r["status"]}
    return out


def us_session(ts: datetime | None) -> str:
    from app.earnings.common import classify_session  # noqa: PLC0415

    return classify_session(ts, "America/New_York", "US")


class EarningsConsumer(Consumer):
    name = "earnings"
    group = "earnings"
    topics = ("filings",)
    batch = 200

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.cache_s = float(params.get("cache_s", 3600))
        self._cache: dict[tuple[str, date], tuple[float, dict[str, Any]]] = {}
        self.lookup = params.get("lookup")  # tests inject (issuer_id, filed) -> dict

    def _expect(self, ctx: StreamContext, issuer_id: str, filed: date) -> dict[str, Any]:
        if self.lookup is not None:
            return self.lookup(issuer_id, filed)
        k = (issuer_id, filed)
        hit = self._cache.get(k)
        if hit and time.time() - hit[0] < self.cache_s:
            return hit[1]
        v = pending_expectations(ctx.settings, issuer_id, filed)
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[k] = (time.time(), v)
        return v

    def handle_one(self, ctx: StreamContext, msg: Message) -> list[Envelope]:
        p = msg.payload
        if not is_earnings_release(p) or not p.get("issuer_id"):
            return []
        ts = ts_of(msg)
        filed = date.fromisoformat(p["filed_date"]) if p.get("filed_date") else (ts.date() if ts else ctx.clock().date())
        exp = self._expect(ctx, p["issuer_id"], filed)
        payload = {
            "kind": "earnings_release", "filing_id": p.get("filing_id"), "accession": p.get("accession"),
            "form_type": p.get("form_type"), "items": p.get("items"), "issuer_id": p.get("issuer_id"),
            "security_id": p.get("security_id"), "ticker": p.get("ticker"), "company": p.get("company"),
            "knowledge_ts": p.get("knowledge_ts"), "filed_date": str(filed), "session": us_session(ts), "url": p.get("url"),
            "period_end": exp.get("period_end"), "fiscal_period": exp.get("fiscal_period"),
            "expectations": exp.get("items") or {}, "label": LABEL,
            "note": "Official SEC 8-K Item 2.02 release. The model surprise is computed when the 10-Q / 10-K XBRL actual "
                    "lands (nightly).",
        }
        return [Envelope("signals", f"earnings:{p.get('accession') or p.get('filing_id')}", payload, type="earnings",
                         ts_source=ts, ckey=f"earnings:{p['issuer_id']}")]
