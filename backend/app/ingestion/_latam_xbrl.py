"""Direct (taxonomy-free) XBRL 2.1 instance parser for the LATAM official-filing adapters (biva_mx, cmf_cl, smv_pe).

Why not Arelle: the Mexican CNBV `ifrs_mx` entry points (taxonomiasxbrl.cnbv.gob.mx) and the CMF `cl-ci` schemas are
remote and slow / flaky to resolve; the instances themselves already carry everything we need (concept QName, context
period + dimensions, unit, decimals). `instance_to_oim()` turns an instance (bytes, or a zip holding one) into the same
OIM xBRL-JSON-like document `_xbrl_json.parse_xbrl_json` reads, so the downstream conventions are identical:
  * ifrs-full concepts (any IFRS taxonomy year, prefix 'ifrs-full' or legacy 'ifrs') -> 'ifrs-full:<Local>'
    (stored as taxonomy='ifrs-full', concept='<Local>'); local extensions keep their own prefix (taxonomy='other');
  * periods are written as plain inclusive dates ('2025-01-01/2025-12-31', '2025-12-31');
  * units 'iso4217:USD', 'iso4217:MXN/xbrli:shares' (norm_unit -> 'USD', 'MXN/shares').
`label_quarters()` adds Q1..Q4 / 9M fiscal_period labels to quarterly durations (parse_xbrl_json only labels FY / H1).
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from typing import Any

import orjson
from lxml import etree

from app.ingestion._arelle import ArelleError
from app.ingestion._global_common import dimensions_hash, dimensions_json

XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"
_SKIP_NS = {XBRLI, "http://www.xbrl.org/2003/linkbase", "http://www.w3.org/1999/xlink"}


def _ifrs_full(ns: str) -> bool:
    n = ns.rstrip("/").lower()
    return "xbrl.ifrs.org/taxonomy/" in n and (n.endswith("/ifrs-full") or n.endswith("/ifrs"))


def pick_instance(payload: bytes) -> bytes:
    """The XBRL instance inside `payload` (zip -> largest .xbrl / instance-looking .xml), else payload itself."""
    if payload[:2] != b"PK":
        return payload
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        cands = [n for n in names if n.lower().endswith(".xbrl")]
        if not cands:  # CMF 2009-2013 zips: <rut>_<yyyymm>_C.xml is the instance, linkbases are *-label.xml etc.
            cands = [n for n in names if n.lower().endswith(".xml") and b"xbrli:xbrl" in z.read(n)[:4000]]
        if not cands:
            raise ArelleError(f"no XBRL instance in zip ({len(names)} files)")
        cands.sort(key=lambda n: -z.getinfo(n).file_size)
        return z.read(cands[0])


def _qn(text: str | None, nsmap: dict[str | None, str]) -> str:
    """Normalise a QName string (dimension / member) so IFRS prefixes become 'ifrs-full'."""
    t = (text or "").strip()
    if ":" not in t:
        return t
    p, local = t.split(":", 1)
    ns = nsmap.get(p)
    return f"ifrs-full:{local}" if ns and _ifrs_full(ns) else t


def _measure(el: etree._Element) -> str:
    t = (el.text or "").strip()
    if ":" in t:
        p, local = t.split(":", 1)
        ns = el.nsmap.get(p, "")
        if "iso4217" in ns:
            return f"iso4217:{local}"
        return f"xbrli:{local}" if local in ("shares", "pure") else t
    return t


def instance_to_oim(payload: bytes) -> dict[str, Any]:
    """XBRL instance bytes (or zip) -> {'documentInfo': {...}, 'facts': {id: {'value', 'decimals', 'dimensions'}}}."""
    data = pick_instance(payload)
    try:
        root = etree.fromstring(data, parser=etree.XMLParser(huge_tree=True, recover=True, resolve_entities=False))
    except etree.XMLSyntaxError as e:  # pragma: no cover - recover=True rarely raises
        raise ArelleError(f"XBRL instance not parseable: {e}") from e
    if root is None:
        raise ArelleError("empty XBRL instance")
    ctx: dict[str, tuple[str, dict[str, str]]] = {}
    for c in root.iter(f"{{{XBRLI}}}context"):
        per = c.find(f"{{{XBRLI}}}period")
        if per is None:
            continue
        inst = per.findtext(f"{{{XBRLI}}}instant")
        if inst:
            p = inst.strip()[:10]
        else:
            s, e = per.findtext(f"{{{XBRLI}}}startDate"), per.findtext(f"{{{XBRLI}}}endDate")
            if not (s and e):
                continue  # 'forever'
            p = f"{s.strip()[:10]}/{e.strip()[:10]}"
        dims: dict[str, str] = {}
        for m in c.iter(f"{{{XBRLDI}}}explicitMember"):
            dims[_qn(m.get("dimension"), m.nsmap)] = _qn(m.text, m.nsmap)
        for m in c.iter(f"{{{XBRLDI}}}typedMember"):
            dims[_qn(m.get("dimension"), m.nsmap)] = " ".join(t.strip() for t in m.itertext() if t.strip())
        ctx[c.get("id")] = (p, dims)
    units: dict[str, str] = {}
    for u in root.iter(f"{{{XBRLI}}}unit"):
        div = u.find(f"{{{XBRLI}}}divide")
        if div is not None:
            num = div.find(f"{{{XBRLI}}}unitNumerator/{{{XBRLI}}}measure")
            den = div.find(f"{{{XBRLI}}}unitDenominator/{{{XBRLI}}}measure")
            if num is not None and den is not None:
                units[u.get("id")] = f"{_measure(num)}/{_measure(den)}"
            continue
        ms = u.findall(f"{{{XBRLI}}}measure")
        if len(ms) == 1:
            units[u.get("id")] = _measure(ms[0])
    facts: dict[str, Any] = {}
    entity = None
    for el in root:
        if not isinstance(el.tag, str):
            continue
        q = etree.QName(el)
        if q.namespace in _SKIP_NS or q.namespace is None:
            continue
        cref, uref = el.get("contextRef"), el.get("unitRef")
        if not cref or not uref or cref not in ctx or uref not in units:
            continue
        if el.get("{http://www.w3.org/2001/XMLSchema-instance}nil") in ("true", "1"):
            continue
        txt = (el.text or "").strip()
        if not txt:
            continue
        prefix = "ifrs-full" if _ifrs_full(q.namespace) else (el.prefix or "ext")
        period, dims = ctx[cref]
        dec = el.get("decimals")
        try:
            decimals: Any = int(dec) if dec not in (None, "INF") else None
        except ValueError:
            decimals = None
        facts[f"f{len(facts)}"] = {"value": txt, "decimals": decimals,
                                   "dimensions": {"concept": f"{prefix}:{q.localname}", "period": period,
                                                  "unit": units[uref], **dims}}
    for ident in root.iter(f"{{{XBRLI}}}identifier"):
        entity = f"{ident.get('scheme')}:{(ident.text or '').strip()}"
        break
    if not facts:
        raise ArelleError("no numeric facts in XBRL instance")
    return {"documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json", "entity": entity,
                             "source": "lxml-instance"}, "facts": facts}


def quarter_of(end: date, fye_month: int | None) -> int:
    """Fiscal quarter (1..4) of a period ending in `end` for a fiscal year ending in `fye_month` (default Dec)."""
    fm = fye_month or 12
    return ((end.month - fm - 1) % 12) // 3 + 1


TTM_DIM = {"period_basis": "TTM"}


def label_quarters(rows: list[dict[str, Any]], fye_month: int | None) -> list[dict[str, Any]]:
    """Fill fiscal_period on quarter (~3 months -> Q1..Q4), 9-month (9M) and other YTD durations.

    Twelve-month durations that do NOT end at the fiscal year end (Mexican quarterly reports carry 'últimos doce
    meses' figures, e.g. 2025-07-01..2026-06-30) are trailing-twelve-month numbers, not fiscal years: they get
    fiscal_period=None and a synthetic dimension period_basis=TTM so they never enter the undimensioned FY series."""
    for r in rows:
        s, e = r.get("period_start"), r.get("period_end")
        if s is None or e is None:
            continue
        days = (e - s).days + 1
        if 340 <= days <= 380 and quarter_of(e, fye_month) != 4:
            dims = {**(orjson.loads(r["dimensions_json"]) if r.get("dimensions_json") else {}), **TTM_DIM}
            r.update(fiscal_period=None, dimensions_hash=dimensions_hash(dims), dimensions_json=dimensions_json(dims))
            continue
        if r.get("fiscal_period"):
            continue
        if 80 <= days <= 100:
            r["fiscal_period"] = f"Q{quarter_of(e, fye_month)}"
        elif 260 <= days <= 285:
            r["fiscal_period"] = "9M"
        elif 170 <= days <= 195:
            r["fiscal_period"] = "H1" if quarter_of(e, fye_month) == 2 else "YTD"
    return rows
