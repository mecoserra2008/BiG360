"""Static consistency checks across all YAML contracts. These guard the parallel build: if an agent edits a contract
in a way that breaks cross-references, this suite fails."""

from __future__ import annotations

import ast
import re

import pytest
import yaml

from app.core.config import get_settings
from app.core.schema import check_contract, load_contracts

C = get_settings().contracts_dir


def _y(name: str):
    return yaml.safe_load((C / name).read_text(encoding="utf-8"))


REQUIRED_TABLES = {
    "securities_master", "issuers", "listings", "fx_rates", "prices_daily", "corporate_actions", "filings_index",
    "fundamentals_raw", "fundamentals_canonical", "fundamentals_wide", "ratios", "sector_kpis", "peer_scores",
    "factor_scores", "composite_scores", "moat", "forecasts", "insider_transactions", "insider_signals", "news_items",
    "sentiment_daily", "gdelt_events", "gdelt_company_daily", "macro_series", "macro_observations", "macro_regimes",
    "macro_betas", "tech_levels", "confluence_zones", "ml_features", "ml_predictions", "model_runs", "job_runs",
}


def test_all_required_tables_present():
    missing = REQUIRED_TABLES - set(load_contracts())
    assert not missing, missing


@pytest.mark.parametrize("name", sorted(load_contracts()))
def test_contract_is_consistent(name):
    assert check_contract(load_contracts()[name]) == []


def test_canonical_items_unique_and_derivations_reference_known_ids():
    doc = _y("canonical_line_items.yaml")
    ids = [i["id"] for i in doc["items"]]
    assert len(ids) == len(set(ids)), "duplicate canonical item ids"
    assert 80 <= len(ids) <= 130
    funcs = {"coalesce", "nz", "abs", "min", "max"}
    for item in doc["items"]:
        if item.get("derive"):
            names = {n.id for n in ast.walk(ast.parse(item["derive"], mode="eval")) if isinstance(n, ast.Name)}
            unknown = names - set(ids) - funcs
            assert not unknown, f"{item['id']}: unknown refs {unknown}"
        assert item["statement"] in {"IS", "BS", "CF", "NOTE"}
        assert item["period"] in {"duration", "instant"}


def test_taxonomy_and_kpi_sets_cover_every_subsector_once():
    tax = _y("sector_taxonomy.yaml")
    kpis = _y("sector_kpis.yaml")
    sectors = {s["id"] for s in tax["sectors"]}
    subs = [s["id"] for s in tax["subsectors"]]
    assert len(subs) == len(set(subs))
    assert all(s["sector"] in sectors for s in tax["subsectors"])
    covered = [sub for ks in kpis["kpi_sets"] for sub in ks["subsectors"]]
    assert sorted(covered) == sorted(subs), set(subs) ^ set(covered)
    valid = set(subs) | {None}
    assert set(tax["yf_industry"].values()) <= valid
    assert {r[2] for r in tax["sic_ranges"]} <= valid
    assert {v["default"] for v in tax["tse33"].values()} <= valid
    assert set(tax["ksic"]["divisions"].values()) <= valid
    assert set(tax["csrc"]["codes"].values()) | set(tax["csrc"]["letters"].values()) <= valid
    assert set(tax["overrides"].values()) <= valid


def test_kpi_quality_weights_sum_to_one_and_reference_known_metrics():
    kpis = _y("sector_kpis.yaml")
    generic = {m["id"] for m in kpis["generic_metrics"]} | {m["id"] for m in kpis["accounting_scores"]}
    valuation = set(_y("scoring.yaml")["valuation_metrics"])
    for ks in kpis["kpi_sets"]:
        local = {k["id"] for k in ks["kpis"]}
        assert abs(sum(ks["quality_weights"].values()) - 1.0) < 1e-6, ks["id"]
        for m in ks["quality_weights"]:
            assert m in generic | local, f"{ks['id']}: quality metric {m} unknown"
        for m in ks["peer_metrics"]:
            assert m in generic | local | valuation, f"{ks['id']}: peer metric {m} unknown"
        for m in ks["excluded_generic"]:
            assert m in generic, f"{ks['id']}: excluded {m} unknown"


def test_scoring_profiles_and_factor_members():
    sc = _y("scoring.yaml")
    kpis = _y("sector_kpis.yaml")
    factor_ids = {f["id"] for f in sc["factors"]}
    all_metrics = (
        {m["id"] for m in kpis["generic_metrics"]}
        | {m["id"] for m in kpis["accounting_scores"]}
        | {k["id"] for ks in kpis["kpi_sets"] for k in ks["kpis"]}
        | set(sc["valuation_metrics"]) | set(sc["market_metrics"]) | set(sc["macro_metrics"]) | set(sc["other_metrics"])
    )
    set_ids = {ks["id"] for ks in kpis["kpi_sets"]}
    for f in sc["factors"]:
        members = f.get("members", {})
        if members:
            assert abs(sum(members.values()) - 1.0) < 1e-6, f["id"]
            assert set(members) <= all_metrics, (f["id"], set(members) - all_metrics)
        for ks, ov in (f.get("overrides") or {}).items():
            assert ks in set_ids, ks
            assert abs(sum(ov.values()) - 1.0) < 1e-6, (f["id"], ks)
            assert set(ov) <= all_metrics, (f["id"], ks, set(ov) - all_metrics)
    for pid, prof in sc["composite"]["profiles"].items():
        w = {k: v for k, v in prof.items() if not k.startswith("_")}
        if w:
            assert set(w) <= factor_ids, pid
            assert abs(sum(w.values()) - 1.0) < 1e-6, pid


def test_jobs_dag_is_acyclic_and_writes_known_tables():
    from app.scheduler.base import load_job_registry, topo_order

    reg = load_job_registry()
    ids = {j["id"] for j in reg["jobs"]}
    tables = set(load_contracts())
    for j in reg["jobs"]:
        assert set(j.get("depends_on", [])) <= ids, j["id"]
        assert set(j.get("writes", [])) <= tables, (j["id"], set(j.get("writes", [])) - tables)
        assert re.match(r"^app\.[a-z0-9_.]+$", j["module"]), j["module"]
    for dag in reg["dags"]:
        topo_order(dag)  # raises on cycle


def test_api_contract_parses_and_refs_resolve():
    spec = _y("api.yaml")
    assert spec["openapi"].startswith("3.1")
    schemas = set(spec["components"]["schemas"])
    params = set(spec["components"]["parameters"])
    text = (C / "api.yaml").read_text(encoding="utf-8")
    for ref in re.findall(r"#/components/schemas/(\w+)", text):
        assert ref in schemas, ref
    for ref in re.findall(r"#/components/parameters/(\w+)", text):
        assert ref in params, ref
    op_ids = [op["operationId"] for p in spec["paths"].values() for op in p.values()]
    assert len(op_ids) == len(set(op_ids))
