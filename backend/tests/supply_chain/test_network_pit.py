"""Roadmap #9 network analytics: point-in-time edge build, look-through exposure, concentration metrics."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.supply_chain.network import _pct_rank, build_edges, exposures, metrics

AS_OF = date(2026, 10, 15)


def _link(lt, src, dst, share, kd, pe, conf=0.9, dst_name=None):
    return {"link_type": lt, "source": "text_10k", "src_entity_id": f"cik-{src}", "src_issuer_id": src,
            "src_name": f"{src} Corp", "dst_key": dst or f"name:{dst_name.lower()}", "dst_issuer_id": dst,
            "dst_name": dst_name or f"{dst} Inc", "dst_kind": "company", "share": share, "confidence": conf if dst else 0.0,
            "period_end": pe, "knowledge_date": kd, "filing_id": f"f-{src}-{kd}", "form_type": "10-K", "rule": "c_name_pct",
            "rule_conf": 0.9, "snippet": "x" * 250}


REF = pl.DataFrame({"issuer_id": ["S", "C1", "C2", "P"], "display": ["Sup", "Cust1", "Cust2", "Parts"],
                    "country": ["US", "CN", "DE", "JP"], "region": ["US", "CN", "EU", "JP"],
                    "sector_id": ["TECH", "RETAIL", "AUTO", "TECH"], "security_id": ["s1", "s2", "s3", "s4"],
                    "in_universe": [True, True, True, True]})


def _links():
    return pl.DataFrame([
        _link("customer", "S", "C1", 0.30, date(2026, 2, 1), date(2025, 12, 31)),
        _link("customer", "S", "C1", 0.25, date(2025, 2, 1), date(2024, 12, 31)),          # older restated: latest wins
        _link("customer", "S", "C2", 0.20, date(2026, 2, 1), date(2025, 12, 31)),
        _link("customer", "S", None, 0.10, date(2026, 2, 1), date(2025, 12, 31), dst_name="Acme Retail"),
        _link("customer", "S", "P", 0.40, date(2026, 11, 1), date(2026, 9, 30)),            # future filing: excluded
        _link("customer", "S", "C2", 0.50, date(2021, 2, 1), date(2020, 12, 31)),           # outside lookback
        _link("supplier", "C1", "S", 0.15, date(2026, 3, 1), date(2025, 12, 31)),           # C1 discloses S as supplier
        _link("supplier", "S", "P", None, date(2026, 2, 1), date(2025, 12, 31)),            # S buys from P
    ], infer_schema_length=None)


def test_build_edges_point_in_time_and_merge():
    e = build_edges(_links(), REF, AS_OF)
    pairs = {(r["supplier_key"], r["customer_key"]): r for r in e.iter_rows(named=True)}
    assert set(pairs) == {("S", "C1"), ("S", "C2"), ("S", "name:acme retail"), ("P", "S")}
    sc1 = pairs[("S", "C1")]
    assert sc1["edge_type"] == "both" and sc1["share_of_supplier_revenue"] == pytest.approx(0.30)
    assert sc1["share_of_customer_purchases"] == pytest.approx(0.15) and sc1["n_disclosures"] == 3
    assert sc1["knowledge_date"] == date(2026, 3, 1) and sc1["first_knowledge_date"] == date(2025, 2, 1)
    assert pairs[("S", "C2")]["share_of_supplier_revenue"] == pytest.approx(0.20)   # the 2020 disclosure is outside the window
    assert pairs[("S", "name:acme retail")]["confidence"] == pytest.approx(0.9 * 0.4)
    assert pairs[("P", "S")]["edge_type"] == "disclosed_supplier" and pairs[("P", "S")]["customer_name"] == "Sup"
    assert all(len(r["snippet"]) <= 200 for r in pairs.values())
    # earlier as_of: only what was known then
    e0 = build_edges(_links(), REF, date(2025, 6, 1))
    assert {(r["supplier_key"], r["customer_key"]) for r in e0.iter_rows(named=True)} == {("S", "C1")}
    assert e0["share_of_supplier_revenue"][0] == pytest.approx(0.25)


def test_look_through_exposure_and_metrics():
    e = build_edges(_links(), REF, AS_OF)
    geo = {"S": {"US": 0.6, "CN": 0.4}, "C1": {"CN": 0.5, "FR": 0.5}}
    exp, info = exposures(REF, e, geo, AS_OF)
    s = exp.filter((pl.col("issuer_id") == "S") & (pl.col("dim") == "country"))
    lt = dict(zip(s["key"], s["look_through"], strict=True))
    so = dict(zip(s["key"], s["second_order"], strict=True))
    S = 0.30 + 0.20                                   # weighted (resolved) customers; Acme has no issuer
    assert so["FR"] == pytest.approx(0.30 * 0.5) and so["DE"] == pytest.approx(0.20)   # C2: home-country fallback
    assert lt["US"] == pytest.approx((1 - S) * 0.6)
    assert lt["CN"] == pytest.approx((1 - S) * 0.4 + 0.30 * 0.5)
    assert sum(lt.values()) == pytest.approx(1.0)
    sec = exp.filter((pl.col("issuer_id") == "S") & (pl.col("dim") == "sector"))
    assert dict(zip(sec["key"], sec["second_order"], strict=True))["AUTO"] == pytest.approx(0.20)
    risk = pl.DataFrame({"iso2": ["US", "CN"], "risk_score": [20.0, 80.0], "event_pressure": [0.1, 1.0],
                         "recession_prob": [0.1, 0.3]})
    nb = pl.DataFrame({"issuer_id": ["S", "S"], "method": ["partial_corr", "return_corr"]})
    m = metrics(REF, e, info, {"S": {"period_end": date(2025, 12, 31), "knowledge_date": date(2026, 2, 1)}}, risk, nb, AS_OF)
    r = m.filter(pl.col("issuer_id") == "S").row(0, named=True)
    assert r["n_customers"] == 3 and r["n_customers_resolved"] == 2 and r["n_suppliers"] == 1
    assert r["top_customer_share"] == pytest.approx(0.30) and r["top_customer_key"] == "C1"
    assert r["customer_hhi"] == pytest.approx(0.30 ** 2 + 0.20 ** 2 + 0.10 ** 2)
    assert r["country_risk"] == pytest.approx(0.6 * 20 + 0.4 * 80)
    assert r["coverage_level"] == "full" and r["n_neighbours_stat"] == 1 and r["geo_hhi"] == pytest.approx(0.52)
    assert m.filter(pl.col("issuer_id") == "C2")["coverage_level"][0] == "partial"


def test_pct_rank():
    assert _pct_rank({"a": 1.0, "b": 3.0, "c": None}) == {"a": 0.25, "b": 0.75}
    assert _pct_rank({"a": 1.0, "b": 3.0}, higher_is_risk=False) == {"a": 0.75, "b": 0.25}
