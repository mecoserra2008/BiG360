"""Minimal OIM xBRL-JSON (https://xbrl.org/2021/xbrl-json) fact parser shared by the ESEF (filings.xbrl.org) and
FCA NSM (UK ESEF/UKSEF '_ixbrl.json') adapters.

Conventions (match ingest-us / canonical_line_items.yaml):
  * ifrs-full concepts are stored WITHOUT prefix ('Revenue') with taxonomy='ifrs-full';
  * FRC UK taxonomies ('core', 'uk-core', 'frs-core', 'bus', 'uk-bus') -> taxonomy 'uk-core', concept 'prefix:Local';
  * company extensions and anything else -> taxonomy 'other', concept 'prefix:Local' (raw tag always kept).
  * xBRL-JSON period ends are exclusive midnight instants ('2024-01-01T00:00:00' == end of 2023-12-31).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from app.ingestion._global_common import fact_row, fiscal_year_for

_CORE_DIMS = {"concept", "entity", "period", "unit", "language", "noteId"}
UK_PREFIXES = {"core", "uk-core", "frs-core", "bus", "uk-bus", "frs-102", "uk-gaap"}


def _parse_point(s: str) -> date:
    s = s.strip()
    if "T" in s:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            return (dt - timedelta(days=1)).date()
        return dt.date()
    return date.fromisoformat(s[:10])


def parse_period(p: str) -> tuple[date | None, date]:
    if "/" in p:
        a, b = p.split("/", 1)
        start_dt = datetime.fromisoformat(a.strip().replace("Z", "+00:00")) if "T" in a else datetime.fromisoformat(a.strip())
        return start_dt.date(), _parse_point(b)
    return None, _parse_point(p)


def norm_unit(u: str | None) -> str | None:
    if not u:
        return None
    def one(x: str) -> str:
        x = x.split(":", 1)[-1]
        return {"pure": "pure", "shares": "shares"}.get(x, x.upper() if len(x) == 3 else x)
    if "/" in u:
        n, d = u.split("/", 1)
        return f"{one(n)}/{one(d)}"
    return one(u)


def split_concept(qname: str) -> tuple[str, str]:
    """-> (taxonomy enum value, stored concept string)."""
    if ":" not in qname:
        return "other", qname
    prefix, local = qname.split(":", 1)
    if prefix in ("ifrs-full", "ifrs"):
        return "ifrs-full", local
    if prefix in UK_PREFIXES:
        return "uk-core", qname
    return "other", qname


def fiscal_period_of(start: date | None, end: date) -> str | None:
    if start is None:
        return None
    days = (end - start).days + 1
    if 340 <= days <= 380:
        return "FY"
    if 170 <= days <= 195:
        return "H1" if end.month in (6, 9) and start.month in (1, 4) else None
    return None


def numeric_facts(doc: dict[str, Any]) -> dict[str, Any]:
    """Copy of the document keeping only numeric facts (with a unit) — used to land compact raw payloads."""
    facts = {k: v for k, v in (doc.get("facts") or {}).items() if (v.get("dimensions") or {}).get("unit")}
    return {"documentInfo": doc.get("documentInfo", {}), "facts": facts}


def parse_xbrl_json(doc: dict[str, Any], *, source_id: str, source_entity_id: str, filing_id: str, form_type: str,
                    knowledge_date: date, report_period_end: date | None = None) -> list[dict[str, Any]]:
    fye_month = report_period_end.month if report_period_end else None
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for fact in (doc.get("facts") or {}).values():
        dims = dict(fact.get("dimensions") or {})
        unit = norm_unit(dims.get("unit"))
        val = fact.get("value")
        if unit is None or val is None:
            continue
        try:
            value = float(val)
        except (TypeError, ValueError):
            continue
        qname = dims.get("concept")
        period = dims.get("period")
        if not qname or not period:
            continue
        try:
            start, end = parse_period(period)
        except ValueError:
            continue
        extra = {k: str(v) for k, v in dims.items() if k not in _CORE_DIMS}
        taxonomy, concept = split_concept(qname)
        key = (concept, start, end, unit, tuple(sorted(extra.items())))
        if key in seen:
            continue
        seen.add(key)
        dec = fact.get("decimals")
        rows.append(fact_row(
            source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id, form_type=form_type,
            taxonomy=taxonomy, concept=concept, label=None, period_start=start, period_end=end, value=value, unit=unit,
            knowledge_date=knowledge_date, fiscal_year=fiscal_year_for(end, fye_month),
            fiscal_period=fiscal_period_of(start, end),
            decimals=int(dec) if isinstance(dec, (int, float)) and abs(dec) < 30 else None,
            dims=extra or None, consolidated=True))  # ESEF mandates tagging of consolidated statements only
    return rows
