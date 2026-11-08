"""Inline XBRL (iXBRL 1.0 / 1.1) numeric fact parser -> fundamentals_raw rows (no Arelle, no network).

Used by the Companies House bulk accounts adapter (app.ingestion.ch_accounts): the Accounts Data Product members are
one XHTML iXBRL document each (FRS 102 / FRS 105 small-company accounts, FRS 101 / UK-adopted IFRS accounts of larger
companies). Only what the canonical mapping needs is parsed:
  * ix:nonFraction facts (name, contextRef, unitRef, decimals, scale, format, sign, xsi:nil);
  * xbrli:context (instant / startDate-endDate, explicit / typed dimension members), xbrli:unit (measures, divide).
Concept namespaces are resolved by URI, not by the document's prefix:
  * IFRS Foundation taxonomies (.../ifrs-full)          -> taxonomy 'ifrs-full', concept 'Local' (as ESEF / SEC IFRS);
  * FRC taxonomies (xbrl.frc.org.uk .../core, .../bus,
    .../countries, ... and the legacy uk-gaap 2009)     -> taxonomy 'uk-core', concept 'uk-core:Local' / 'uk-bus:Local'
                                                           (the FRC 'core' namespace is the FRS 101 / 102 / 105 one);
  * anything else (company extensions)                  -> taxonomy 'other', concept 'prefix:Local'.
Number formats: ixt num-dot-decimal / numdotdecimal / numcommadot, num-comma-decimal / numcommadecimal /
numdotcomma, zerodash / fixed-zero / numdash, plus a tolerant default (digits, '.', ',' and spaces). The displayed
value is multiplied by 10^scale and negated when sign="-". Facts in the same context with the same concept, unit and
dimensions are de-duplicated (iXBRL repeats facts across statements and notes).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

IX_NS = ("http://www.xbrl.org/2013/inlineXBRL", "http://www.xbrl.org/2008/inlineXBRL")
XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

_NUM_RE = re.compile(r"[^0-9.,\- ]")


class IxbrlError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def taxonomy_of(uri: str, prefix: str | None, local: str) -> tuple[str, str]:
    """(taxonomy enum value, stored concept) for a concept QName."""
    u = (uri or "").lower()
    if "ifrs" in u and u.rstrip("/").endswith("ifrs-full"):
        return "ifrs-full", local
    if "xbrl.frc.org.uk" in u or "xbrl.org/uk/" in u:
        if "/bus" in u or "business" in u or u.endswith("/cd"):
            return "uk-core", f"uk-bus:{local}"
        if "/core" in u or "/gaap" in u or "frs" in u:
            return "uk-core", f"uk-core:{local}"
        return "uk-core", f"uk-{(prefix or 'frc').lower()}:{local}"
    if "fasb.org/us-gaap" in u:
        return "us-gaap", local
    return "other", f"{prefix or 'ext'}:{local}"


def parse_number(text: str, fmt: str | None) -> float | None:
    """Displayed iXBRL value -> float (before scale / sign). None when not numeric."""
    f = (fmt or "").rsplit(":", 1)[-1].lower().replace("-", "")
    t = (text or "").strip()
    if f in ("zerodash", "fixedzero", "numdash", "nocontent", "fixedempty") or t in ("-", "—", "–", ""):
        return 0.0 if f or t in ("-", "—", "–") else None
    t = t.replace(" ", " ").replace("(", "").replace(")", "")
    t = _NUM_RE.sub("", t).strip()
    if not t:
        return None
    if f in ("numcommadecimal", "numdotcomma", "numspacecomma"):  # 1.234,56 / 1 234,56
        t = t.replace(".", "").replace(" ", "").replace(",", ".")
    elif f in ("numdotdecimal", "numcommadot", "numspacedot", "numunitdecimal"):
        t = t.replace(",", "").replace(" ", "")
    else:  # no / unknown format: commas are thousands separators unless they look decimal (1,5)
        t = t.replace(" ", "")
        if "," in t and "." not in t and re.fullmatch(r"-?\d+,\d{1,2}", t):
            t = t.replace(",", ".")
        else:
            t = t.replace(",", "")
    t = t.lstrip("-")
    try:
        return float(t)
    except ValueError:
        return None


def _text(el: Any) -> str:
    """Displayed text of an ix element, excluding nested ix:exclude content."""
    parts = []
    if el.text:
        parts.append(el.text)
    for ch in el:
        if _local(ch.tag) == "exclude" and _ns(ch.tag) in IX_NS:
            pass
        else:
            parts.append(_text(ch))
        if ch.tail:
            parts.append(ch.tail)
    return "".join(parts)


def _date(s: str | None) -> date | None:
    if not s:
        return None
    s = s.strip()
    try:
        d = date.fromisoformat(s[:10])
    except ValueError:
        return None
    if "T" in s and s[11:19] == "00:00:00" and len(s) > 10:  # end-of-period instants written as next midnight
        return d - timedelta(days=1)
    return d


def dims_hash(dims: dict[str, str] | None) -> str:
    if not dims:
        return ""
    return hashlib.sha1(json.dumps(dims, sort_keys=True).encode()).hexdigest()[:16]


def parse_ixbrl(content: bytes) -> dict[str, Any]:
    """iXBRL document -> {"facts": [...], "entity": scheme/id of the first context, "period_end": latest duration end,
    "doc_type": hints}. Each fact: concept, taxonomy, value, unit, decimals, period_start, period_end, dims."""
    from lxml import etree

    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(content, parser)
    except etree.XMLSyntaxError as e:  # pragma: no cover - recover=True rarely raises
        raise IxbrlError(str(e)) from e
    if root is None:
        raise IxbrlError("empty document")
    contexts: dict[str, dict[str, Any]] = {}
    units: dict[str, str] = {}
    entity = None
    for ctx in root.iter(f"{{{XBRLI}}}context"):
        cid = ctx.get("id")
        ident = ctx.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
        if entity is None and ident is not None and ident.text:
            entity = {"scheme": ident.get("scheme"), "id": ident.text.strip()}
        per = ctx.find(f"{{{XBRLI}}}period")
        start = end = None
        if per is not None:
            inst = per.findtext(f"{{{XBRLI}}}instant")
            if inst:
                end = _date(inst)
            else:
                start = _date(per.findtext(f"{{{XBRLI}}}startDate"))
                end = _date(per.findtext(f"{{{XBRLI}}}endDate"))
        dims: dict[str, str] = {}
        for seg in list(ctx.iter(f"{{{XBRLDI}}}explicitMember")) + list(ctx.iter(f"{{{XBRLDI}}}typedMember")):
            dim = seg.get("dimension") or ""
            val = (seg.text or "").strip() if _local(seg.tag) == "explicitMember" else "".join(seg.itertext()).strip()
            dims[dim] = val
        if cid and end:
            contexts[cid] = {"start": start, "end": end, "dims": dims}
    for u in root.iter(f"{{{XBRLI}}}unit"):
        uid = u.get("id")
        div = u.find(f"{{{XBRLI}}}divide")

        def meas(node: Any) -> str:
            ms = [(m.text or "").strip().rsplit(":", 1)[-1] for m in node.iter(f"{{{XBRLI}}}measure")]
            return "*".join(m for m in ms if m)

        if div is not None:
            num = div.find(f"{{{XBRLI}}}unitNumerator")
            den = div.find(f"{{{XBRLI}}}unitDenominator")
            units[uid] = f"{meas(num) if num is not None else ''}/{meas(den) if den is not None else ''}"
        else:
            units[uid] = meas(u)
    facts: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for ns in IX_NS:
        for el in root.iter(f"{{{ns}}}nonFraction"):
            name = el.get("name") or ""
            ctx = contexts.get(el.get("contextRef") or "")
            if not name or ctx is None:
                continue
            if el.get(f"{{{XSI}}}nil") in ("true", "1"):
                continue
            prefix, _, local = name.rpartition(":")
            uri = el.nsmap.get(prefix or None) or ""
            tax, concept = taxonomy_of(uri, prefix or None, local)
            v = parse_number(_text(el), el.get("format"))
            if v is None:
                continue
            try:
                scale = int(el.get("scale") or 0)
            except ValueError:
                scale = 0
            v = v * (10 ** scale)
            if el.get("sign") == "-":
                v = -v
            unit = units.get(el.get("unitRef") or "", "") or "pure"
            unit = {"pure": "pure", "shares": "shares"}.get(unit.lower(), unit)
            dec = el.get("decimals")
            try:
                decimals = None if dec in (None, "INF") else int(dec)
            except ValueError:
                decimals = None
            key = (concept, el.get("contextRef"), unit)
            if key in seen:
                continue
            seen.add(key)
            facts.append({"concept": concept, "taxonomy": tax, "value": v, "unit": unit, "decimals": decimals,
                          "period_start": ctx["start"], "period_end": ctx["end"], "dims": ctx["dims"] or None})
    ends = [f["period_end"] for f in facts if f["period_start"] is not None and not f["dims"]]
    return {"facts": facts, "entity": entity, "period_end": max(ends) if ends else None,
            "n_contexts": len(contexts), "n_units": len(units)}


def ixbrl_rows(parsed: dict[str, Any], *, source_id: str, source_entity_id: str, filing_id: str, form_type: str,
               knowledge_date: date, knowledge_date_estimated: bool = False, report_period_end: date | None = None,
               consolidated: bool | None = True) -> list[dict[str, Any]]:
    """parse_ixbrl output -> fundamentals_raw rows (fiscal_year / fiscal_period from the report period)."""
    from app.ingestion._global_common import fact_row, fiscal_year_for
    from app.ingestion._xbrl_json import fiscal_period_of

    pe = report_period_end or parsed.get("period_end")
    rows = []
    for f in parsed["facts"]:
        end, start = f["period_end"], f["period_start"]
        fy = fiscal_year_for(pe) if pe else end.year
        if pe and end > pe + timedelta(days=7):  # forward-dated facts (budgets) are not reported actuals
            continue
        fp = fiscal_period_of(start, end) if start else ("FY" if pe and end == pe else None)
        rows.append(fact_row(source_id=source_id, source_entity_id=source_entity_id, filing_id=filing_id,
                             form_type=form_type, taxonomy=f["taxonomy"], concept=f["concept"], label=None,
                             period_start=start, period_end=end, value=f["value"], unit=f["unit"],
                             knowledge_date=knowledge_date, fiscal_year=fy if end == pe else end.year,
                             fiscal_period=fp, decimals=f["decimals"], dims=f["dims"], consolidated=consolidated,
                             knowledge_date_estimated=knowledge_date_estimated))
    return rows
