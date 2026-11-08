"""Synthetic supply-chain inputs (roadmap #9) for the serving fixture: company_links (XBRL / text customer edges,
supplier edges disclosed by the customer, an unresolved counterparty, a stale and a future-dated disclosure),
company_geo_revenue (annual country breakdowns, one restated later), company_neighbours (statistical), then the real
analytics (app.supply_chain.network.run_network) build sc_edges / sc_exposure / sc_metrics / country_risk on them.
Called from tests.fixtures.serving_small.build_lake."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table

COUNTRIES = ["US", "CN", "DE", "JP", "GB", "MX", "TW", "FR", "BR", "IN"]


def build_supply_chain(s: Settings, as_of: date, secs: list[dict[str, Any]], seed: int = 7) -> dict[str, int]:
    rng = np.random.default_rng(seed + 99)
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    base = [secs[0]] + [x for x in secs[1:] if x["region"] == "US"][:30] + [x for x in secs if x["region"] in ("EU", "JP")][:10]
    out: dict[str, int] = {}
    links = []

    def link(src: dict[str, Any], dst: dict[str, Any] | None, lt: str, share: float | None, kd: date, pe: date,
             source: str = "text_10k", rule: str = "c_name_pct", name: str | None = None, conf: float = 0.85) -> None:
        dst_iid = dst["issuer_id"] if dst else None
        nm = name or (dst["name"] if dst else "Unknown Counterparty")
        links.append({"link_type": lt, "source": source, "src_entity_id": f"{1000 + src['i']:010d}",
                      "src_issuer_id": src["issuer_id"], "src_name": src["name"],
                      "dst_key": dst_iid or f"name:{nm.lower()}", "dst_issuer_id": dst_iid, "dst_name": nm,
                      "dst_norm": nm.lower(), "dst_kind": "company", "share": share, "n_mentions": None,
                      "weight": share, "confidence": conf if dst_iid else 0.0, "period_end": pe, "knowledge_date": kd,
                      "filing_id": f"sec_edgar:0000{src['i']:06d}-{kd.year % 100:02d}-000001", "form_type": "10-K",
                      "rule": rule, "rule_conf": 0.9, "match_method": "exact" if dst_iid else "none",
                      "match_confidence": 0.97 if dst_iid else 0.0, "metric": "revenue" if lt == "customer" else "purchases",
                      "snippet": f"Sales to {nm} accounted for {round((share or 0) * 100)}% of our net sales.",
                      "rules_version": "v2.1", "evidence": None, "ingested_at": now})

    fy = date(as_of.year - 1, 12, 31)
    kd = date(as_of.year, 2, 20)
    center = base[0]
    # the center sells to 4 customers (one unresolved), buys from 2 suppliers (disclosed by the center)
    for k, sh in zip(range(1, 4), (0.22, 0.13, 0.11), strict=True):
        link(center, base[k], "customer", sh, kd, fy, source="xbrl_fsds" if k == 1 else "text_10k",
             rule="xbrl_major_customers" if k == 1 else "c_name_pct", conf=0.97)
    link(center, None, "customer", 0.10, kd, fy, name="Acme Distribution Partners")
    for k in (4, 5):
        link(center, base[k], "supplier", 0.3 if k == 4 else None, kd, fy, rule="s_sole" if k == 4 else "s_include")
    # second-degree: customers have their own customers / suppliers
    for k in range(1, 4):
        for j in rng.choice(np.arange(6, len(base)), size=3, replace=False):
            link(base[k], base[int(j)], "customer", float(rng.uniform(0.1, 0.3)), kd, fy)
    for k in range(6, len(base) - 1):
        if rng.uniform() < 0.5:
            link(base[k], base[k + 1], "customer", float(rng.uniform(0.1, 0.25)), kd, fy)
    # stale disclosure (period 4 years ago) and a future-dated one (must be ignored by the PIT build)
    link(base[5], base[9], "customer", 0.2, date(as_of.year - 4, 2, 20), date(as_of.year - 5, 12, 31))
    link(base[6], base[10], "customer", 0.4, as_of + timedelta(days=30), fy)
    out["company_links"] = write_table(pl.DataFrame(links, infer_schema_length=None), "company_links", mode="overwrite",
                                       settings=s)
    geo = []
    for n, x in enumerate(base[:30]):
        cs = list(rng.choice(COUNTRIES[1:], size=3, replace=False))
        home = x.get("country") or "US"
        vals = {home: float(rng.uniform(40, 70))} | {c: float(rng.uniform(5, 20)) for c in cs if c != home}
        tot = sum(vals.values())
        for restated, kdate in ((False, kd), (True, kd + timedelta(days=200))) if n == 0 else ((False, kd),):
            if restated and kdate > as_of:
                continue
            for c, v in vals.items():
                vv = v * (1.1 if restated and c == home else 1.0)
                geo.append({"source_id": "sec_fsds", "entity_id": f"{1000 + x['i']:010d}", "issuer_id": x["issuer_id"],
                            "name": x["name"], "filing_id": f"sec_edgar:geo-{x['i']}-{int(restated)}", "form_type": "10-K",
                            "measure": "revenue", "concept": "Revenues", "period_start": date(fy.year, 1, 1),
                            "period_end": fy, "qtrs": 4, "fiscal_period": "FY", "member_raw": c, "member_type": "country",
                            "iso2": c, "region": None, "value": vv, "currency": "USD", "denominator": tot, "share": vv / tot,
                            "n_countries": len(vals), "company_country": home, "knowledge_date": kdate,
                            "method": "xbrl_dimensional", "confidence": 1.0, "ingested_at": now})
        # a rest-of-world member exercises the region allocation path
        geo.append({"source_id": "sec_fsds", "entity_id": f"{1000 + x['i']:010d}", "issuer_id": x["issuer_id"],
                    "name": x["name"], "filing_id": f"sec_edgar:geo-{x['i']}-0", "form_type": "10-K", "measure": "revenue",
                    "concept": "Revenues", "period_start": date(fy.year, 1, 1), "period_end": fy, "qtrs": 4,
                    "fiscal_period": "FY", "member_raw": "Other", "member_type": "other", "iso2": None, "region": None,
                    "value": 1.0, "currency": "USD", "denominator": tot, "share": None, "n_countries": 4,
                    "company_country": home, "knowledge_date": kd, "method": "xbrl_dimensional", "confidence": 1.0,
                    "ingested_at": now})
    out["company_geo_revenue"] = write_table(pl.DataFrame(geo, infer_schema_length=None), "company_geo_revenue",
                                             mode="overwrite", settings=s)
    nb = []
    for x in base[:30]:
        others = [y for y in base if y["issuer_id"] != x["issuer_id"]]
        for method in ("return_corr", "partial_corr"):
            for r, y in enumerate(others[:3], start=1):
                nb.append({"as_of": as_of, "issuer_id": x["issuer_id"], "security_id": x["security_id"],
                           "neighbour_issuer_id": y["issuer_id"], "neighbour_security_id": y["security_id"],
                           "method": method, "rho": 0.8 - 0.1 * r if method == "return_corr" else 0.5 - 0.05 * r,
                           "rank": r, "n_obs": 104, "window_weeks": 104, "same_sector": x["sector_id"] == y["sector_id"],
                           "same_region": x["region"] == y["region"], "layer": "statistical", "computed_at": now})
    out["company_neighbours"] = write_table(pl.DataFrame(nb, infer_schema_length=None), "company_neighbours",
                                            mode="overwrite", settings=s)
    from app.supply_chain.network import run_network  # noqa: PLC0415

    rep = run_network(s, as_of=as_of)
    for k in ("sc_edges", "sc_exposure", "sc_metrics", "country_risk"):
        out[k] = int(rep.get(k) or 0)
    return out
