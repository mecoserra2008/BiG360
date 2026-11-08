"""Schedule 13D / 13G -> live activism signal (workstream B; consumer group `activism`).

For each new SC 13D / SCHEDULE 13D / SC 13G / SCHEDULE 13G (+ /A) on `filings` with a linked subject issuer, publish
`signals` (type `activism`, key activism:<accession>, ckey activism:<issuer>) with the filer (the EDGAR Atom entry
"Filed by"), the schedule (13D = active intent, 13G = passive), amendment flag and the event type used by the nightly
activist_13d alert rule:
  new_13d            first 13D of this filer on this subject
  switch_13g_to_13d  13D after a 13G of the same filer (activist_filings history)
  amend_13d          13D/A
  new_13g / amend_13g passive filings (shown, never alerted)
The filer history is read from the nightly activist_filings table (one cached lake read per subject issuer, 6 h).
Cheap: no HTTP. The live alert consumer turns new_13d / switch (and amend_13d on request) into activist_13d alerts with
the same idempotency key as the nightly evaluator (one event per alert / security / filing date).
"""

from __future__ import annotations

import time
from datetime import date
from typing import Any

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer, ts_of
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)


def schedule_of(form: str | None) -> str | None:
    f = (form or "").upper().replace("SCHEDULE", "SC").strip()
    if f.startswith("SC 13D"):
        return "13D"
    if f.startswith("SC 13G"):
        return "13G"
    return None


def event_type(form: str | None, prior_schedules: set[str]) -> str | None:
    sch = schedule_of(form)
    if sch is None:
        return None
    amend = (form or "").upper().endswith("/A")
    if sch == "13G":
        return "amend_13g" if amend else "new_13g"
    if amend:
        return "amend_13d"
    if "13D" in prior_schedules:
        return "amend_13d"
    if "13G" in prior_schedules:
        return "switch_13g_to_13d"
    return "new_13d"


def filer_history(settings: Any, subject_cik: str | None, filer_cik: str | None) -> set[str]:
    """Schedules (13D / 13G) this filer filed on this subject before (nightly activist_filings)."""
    import polars as pl  # noqa: PLC0415

    from app.core.storage import scan_table  # noqa: PLC0415

    if not subject_cik or not filer_cik:
        return set()
    try:
        lf = scan_table("activist_filings", settings)
        names = lf.collect_schema().names()
        sub_col = next((c for c in ("subject_cik", "subject_entity_id", "source_entity_id") if c in names), None)
        fil_col = next((c for c in ("filer_cik", "filer_entity_id") if c in names), None)
        if sub_col is None or fil_col is None or "form_type" not in names:
            return set()
        df = (lf.filter((pl.col(sub_col) == subject_cik) & (pl.col(fil_col) == filer_cik)).select("form_type")
              .collect())
    except Exception as e:  # noqa: BLE001 - table not built yet
        log.debug("activist_filings unavailable: %s", e)
        return set()
    return {s for s in (schedule_of(f) for f in df.get_column("form_type").to_list()) if s}


class ActivismConsumer(Consumer):
    name = "activism"
    group = "activism"
    topics = ("filings",)
    batch = 200

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.cache_s = float(params.get("cache_s", 6 * 3600))
        self.history = params.get("history")     # tests inject (subject_cik, filer_cik) -> set
        self._cache: dict[tuple[str, str], tuple[float, set[str]]] = {}
        self._seen: dict[tuple[str, str], set[str]] = {}   # live filings of this session (13G then 13D same day)

    def _hist(self, ctx: StreamContext, subject: str | None, filer: str | None) -> set[str]:
        if self.history is not None:
            return set(self.history(subject, filer))
        k = (subject or "", filer or "")
        hit = self._cache.get(k)
        if hit and time.time() - hit[0] < self.cache_s:
            return hit[1]
        v = filer_history(ctx.settings, subject, filer)
        if len(self._cache) > 5000:
            self._cache.clear()
        self._cache[k] = (time.time(), v)
        return v

    def handle_one(self, ctx: StreamContext, msg: Message) -> list[Envelope]:
        p = msg.payload
        if p.get("source_id") != "sec_edgar" or schedule_of(p.get("form_type")) is None or not p.get("issuer_id"):
            return []
        subject, filer = p.get("source_entity_id"), p.get("reporting_cik")
        k = (subject or "", filer or "")
        prior = self._hist(ctx, subject, filer) | self._seen.get(k, set())
        et = event_type(p.get("form_type"), prior)
        self._seen.setdefault(k, set()).add(schedule_of(p.get("form_type")) or "")
        ts = ts_of(msg)
        filed = p.get("filed_date") or (str(ts.date()) if ts else str(date.today()))
        payload = {
            "kind": "activism", "event_type": et, "schedule": schedule_of(p.get("form_type")),
            "is_amendment": (p.get("form_type") or "").upper().endswith("/A"), "filing_id": p.get("filing_id"),
            "accession": p.get("accession"), "form_type": p.get("form_type"), "issuer_id": p.get("issuer_id"),
            "security_id": p.get("security_id"), "ticker": p.get("ticker"), "company": p.get("company"),
            "subject_cik": subject, "filer_name": p.get("reporting_owner"), "filer_cik": filer,
            "knowledge_ts": p.get("knowledge_ts"), "filed_date": filed, "as_of_date": filed, "url": p.get("url"),
            "label": "Official SEC Schedule 13D / 13G (EDGAR real-time feed)",
        }
        return [Envelope("signals", f"activism:{p.get('accession') or p.get('filing_id')}", payload, type="activism",
                         ts_source=ts, ckey=f"activism:{p['issuer_id']}")]
