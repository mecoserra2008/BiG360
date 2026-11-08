"""Canonical-tag hints and mapping coverage for the global sources (ingest-global).

fundamentals_raw keeps the raw concept only (the contract forbids canonical mapping at ingest). This module answers
"which canonical item would this raw concept feed?" using contracts/canonical_line_items.yaml (us_gaap/ifrs/jp/kr/cn/
br_cvm chains) plus contracts/canonical_line_items_ext_global.yaml (yfinance/baostock/kr_label chains). It is used for
the live-run coverage report and is safe for app.normalize.fundamentals to reuse.

    hint("edinet", "jppfs", "jppfs_cor:NetSales")      -> "revenue"
    coverage(df)                                        -> per-namespace share of core items present per entity
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

import polars as pl
import yaml

from app.core.config import get_settings

#: (source_id, taxonomy) -> namespace in the canonical yaml files
def namespace_for(source_id: str, taxonomy: str) -> str | None:
    if taxonomy == "ifrs-full":
        return "ifrs"
    if source_id == "edinet" or taxonomy in ("jppfs", "jpcrp", "jpigp"):
        return "jp"
    if source_id == "opendart" or taxonomy in ("kifrs", "dart"):
        return "kr"
    if taxonomy == "cas_label":
        return "cn"
    if taxonomy == "cvm_account":
        return "br_cvm"
    if taxonomy == "yfinance":
        return "yfinance"
    if source_id == "baostock":
        return "baostock"
    return None


GAAP_OF_NAMESPACE = {"ifrs": "IFRS", "jp": "J-GAAP/JMIS(IFRS)", "kr": "K-IFRS", "cn": "CAS", "br_cvm": "BR-IFRS (CVM)",
                     "yfinance": "yfinance (any)", "baostock": "CAS (BaoStock)"}


def _components(entry: str) -> list[str]:
    """'A + B' -> [A, B]; 'X - nz(Y)' -> [X, Y]; plain -> [entry]."""
    parts = re.split(r"\s[+\-]\s", entry)
    out = []
    for p in parts:
        p = re.sub(r"^(nz|coalesce)\((.*)\)$", r"\2", p.strip())
        out.append(p.strip())
    return [p for p in out if p]


@lru_cache(maxsize=1)
def load_chains() -> dict[str, dict[str, str]]:
    """namespace -> {concept: canonical item id} (first chain wins on collisions)."""
    s = get_settings()
    base = yaml.safe_load((s.contracts_dir / "canonical_line_items.yaml").read_text(encoding="utf-8"))
    out: dict[str, dict[str, str]] = {}
    for it in base["items"]:
        for ns in ("ifrs", "jp", "kr", "cn", "br_cvm"):
            for entry in it.get(ns) or []:
                for c in _components(str(entry)):
                    out.setdefault(ns, {}).setdefault(c, it["id"])
                    if ns == "cn":  # labels are matched after removing whitespace / full-width punctuation
                        out[ns].setdefault(_norm_cn(c), it["id"])
    ext_p = s.contracts_dir / "canonical_line_items_ext_global.yaml"
    if ext_p.exists():
        ext = yaml.safe_load(ext_p.read_text(encoding="utf-8"))
        for item_id, chains in (ext.get("items") or {}).items():
            for ns, entries in chains.items():
                target = "kr" if ns == "kr_label" else ns
                for entry in entries:
                    for c in _components(str(entry)):
                        out.setdefault(target, {}).setdefault(c, item_id)
    return out


@lru_cache(maxsize=1)
def core_items() -> tuple[str, ...]:
    s = get_settings()
    base = yaml.safe_load((s.contracts_dir / "canonical_line_items.yaml").read_text(encoding="utf-8"))
    return tuple(i["id"] for i in base["items"] if i.get("core"))


def _norm_cn(label: str) -> str:
    return re.sub(r"[\s（）()、，,：:]", "", label)


def hint(source_id: str, taxonomy: str, concept: str) -> str | None:
    ns = namespace_for(source_id, taxonomy)
    if ns is None:
        return None
    chains = load_chains().get(ns, {})
    if concept in chains:
        return chains[concept]
    if ns == "cn":
        return chains.get(_norm_cn(concept))
    return None


def coverage(df: pl.DataFrame) -> list[dict[str, Any]]:
    """Per namespace: entities, mean share of core items with >=1 raw concept (non-dimensional, consolidated)."""
    if df.is_empty():
        return []
    d = df.filter((pl.col("dimensions_hash") == "") & (pl.col("consolidated").fill_null(True)))
    d = d.select("source_id", "taxonomy", "concept", "source_entity_id").unique()
    rows = d.to_dicts()
    per: dict[str, dict[str, set[str]]] = {}
    for r in rows:
        ns = namespace_for(r["source_id"], r["taxonomy"])
        if ns is None:
            continue
        item = hint(r["source_id"], r["taxonomy"], r["concept"])
        ent = per.setdefault(ns, {}).setdefault(r["source_entity_id"], set())
        if item:
            ent.add(item)
    core = set(core_items())
    # items that only exist as derivations (no tags in any namespace) are excluded from the denominator
    derivable_only = {"ebit", "ebitda", "net_debt", "invested_capital", "fcf", "total_debt", "cash_and_st_investments",
                      "net_income_common"}
    out = []
    for ns, ents in sorted(per.items()):
        denom = core - (derivable_only if ns not in ("yfinance",) else set())
        shares = [len(items & denom) / len(denom) for items in ents.values()]
        missing: dict[str, int] = {}
        for items in ents.values():
            for m in denom - items:
                missing[m] = missing.get(m, 0) + 1
        out.append({"namespace": ns, "gaap": GAAP_OF_NAMESPACE.get(ns, ns), "entities": len(ents),
                    "core_items": len(denom), "mean_coverage": round(sum(shares) / len(shares), 3) if shares else 0.0,
                    "most_missing": sorted(missing, key=lambda k: -missing[k])[:6]})
    return out
