"""API tests against a synthetic serving snapshot (tests/fixtures/serving_small.py): every contract operation is
called and its response validated against contracts/api.yaml."""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots
from app.core import config as config_mod
from app.core import schema as schema_mod
from tests.api_contract import operations, validate

N_SEC = 300


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("serving") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    snapshots.reset()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from app.api.serving import build_serving_db
    from tests.fixtures.serving_small import build_lake

    info = build_lake(s, n_securities=N_SEC)
    rep = build_serving_db(s)
    from app.api.main import create_app

    client = TestClient(create_app())
    yield {"client": client, "info": info, "report": rep, "settings": s}
    snapshots.reset()
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


CALLED: set[tuple[str, str]] = set()


def call(env: dict[str, Any], method: str, tpl: str, path_params: dict[str, str] | None = None,
         params: dict[str, Any] | None = None, json: Any = None, status: int = 200) -> Any:
    url = tpl
    for k, v in (path_params or {}).items():
        url = url.replace("{" + k + "}", quote(str(v), safe=""))
    r = env["client"].request(method, url, params=params, json=json)
    assert r.status_code == status, f"{method} {url} -> {r.status_code}: {r.text[:500]}"
    CALLED.add((method.upper(), tpl))
    if r.status_code == 204 or not r.content:
        return None
    if r.headers["content-type"].startswith(("text/csv", "text/plain")):
        return r.text
    body = r.json()
    validate(method, tpl, status, body)
    return body


@pytest.fixture(scope="module")
def ids(env):
    secs = env["info"]["securities"]
    us = next(x for x in secs[1:] if x["region"] == "US")
    return {"sid": secs[0]["security_id"], "us": us["security_id"], "sector": secs[0]["sector_id"],
            "subsector": secs[0]["subsector_id"]}


def test_snapshot_built(env):
    rep = env["report"]
    assert rep.published and rep.tables["sec"] == N_SEC
    assert rep.inputs["prices_daily"]["status"] == "ok"
    r = env["client"].get("/health").json()
    assert r["status"] in ("ok", "degraded") and r["serving_run_id"] == rep.run_id


def test_system(env):
    st = call(env, "GET", "/api/v1/system/status")
    assert st["serving_run_id"] == env["report"].run_id
    # the fixture's prices end on a fixed date (2026-10-09), so "fresh" vs "stale" depends on the wall clock
    assert any(f["table"] == "prices_daily" and f["status"] in ("fresh", "stale") for f in st["data_freshness"])
    assert any(f["region"] == "US" for f in st["data_freshness"] if f["table"] == "prices_daily")
    assert st["universe_counts"]["securities"] == N_SEC
    assert st["last_dag_runs"] and st["last_dag_runs"][0]["status"] == "failed"
    jobs = call(env, "GET", "/api/v1/system/jobs")
    assert any(j["job_id"] == "build_serving_db" for j in jobs)
    runs = call(env, "GET", "/api/v1/system/jobs/runs", params={"status": "partial"})
    assert runs["total"] == 1
    dq = call(env, "GET", "/api/v1/system/dq")
    assert len(dq) == 1 and not dq[0]["passed"]
    out = call(env, "POST", "/api/v1/system/jobs/{job_id}/trigger", {"job_id": "moat"}, json={"params": {"x": 1}}, status=202)
    assert out["job_id"] == "moat" and out["run_id"]
    call(env, "POST", "/api/v1/system/jobs/{job_id}/trigger", {"job_id": "nope"}, json={}, status=404)



def test_system_sync(env):
    """v1.41.0 startup sync (docs/STARTUP_SYNC_PLAN.md A): status before any sync, a sync-now request, a status file
    with reasons / jobs / chip."""
    s = env["settings"]
    from app.ops.sync import state as SS

    st = call(env, "GET", "/api/v1/system/sync")
    assert st["state"] in ("never", "idle") and st["chip"]["link"].endswith("#sync")
    out = call(env, "POST", "/api/v1/system/sync", json={"datasets": ["fx.rates"]}, status=202)
    assert out["requested"] and out["request"]["datasets"] == ["fx.rates"] and out["message"]
    assert SS.pending_request(s)["by"] == "api"
    SS.take_request(s)
    SS.write_status({"run_id": "S1", "state": "idle", "result": "partial", "trigger": "startup",
                     "finished_at": "2026-10-27T23:00:00+00:00", "data_as_of": {"overall": "2026-10-27"},
                     "counts": {"fresh": 1, "needs_key": 1}, "progress": {"jobs_done": 2, "jobs_total": 2},
                     "datasets": [{"dataset": "prices.KR", "label": "Prices Korea", "group": "prices", "kind": "coverage",
                                   "reason": "needs_key", "keys": ["KRX_OPENAPI_KEY"], "entities": [
                                       {"entity": "KR", "reason": "needs_key", "keys": ["KRX_OPENAPI_KEY"]}]},
                                  {"dataset": "fx.rates", "reason": "fresh", "entities": []}],
                     "jobs": [{"job": "fx_rates", "status": "success", "datasets": ["fx.rates"], "duration_s": 4.2,
                               "pass_no": 1, "trigger": "dataset"}]}, s)
    st = call(env, "GET", "/api/v1/system/sync", params={"history": 3})
    assert st["chip"]["text"] == "1 skipped" and st["chip"]["tone"] == "warn"
    fx = next(d for d in st["datasets"] if d["dataset"] == "fx.rates")
    assert fx["runs"][0]["job"] == "fx_rates" and fx["runs"][0]["duration_s"] == 4.2
    call(env, "GET", "/api/v1/system/sync", params={"history": 500}, status=422)
    SS.status_path(s).unlink()

def test_meta_and_search(env, ids):
    call(env, "GET", "/api/v1/meta/taxonomy")
    fm = call(env, "GET", "/api/v1/meta/factors")
    cv = fm["composite_validation"]   # coverage v3 A8: descriptive until a recorded out-of-sample gate passes
    assert cv["label"] == "descriptive" and cv["signal_wording"] is False and set(cv["profiles"]) == set(fm["profiles"])
    mm = call(env, "GET", "/api/v1/meta/metrics")
    assert any(m["id"] == "roic" for m in mm)
    res = call(env, "GET", "/api/v1/search", params={"q": "AAPL"})
    assert res[0]["ticker"] == "AAPL" and res[0]["isin"]
    res = call(env, "GET", "/api/v1/search", params={"q": "appl", "limit": 5})
    assert res and res[0]["ticker"] == "AAPL"
    res = call(env, "GET", "/api/v1/search", params={"q": "Apple"})
    assert res[0]["name"].startswith("Apple")


def test_field_availability(env, ids):
    """v1.42.0 (completeness C): documented gap reasons per region + per-security reasons."""
    fa = call(env, "GET", "/api/v1/meta/field-availability")
    codes = {c["code"] for c in fa["codes"]}
    assert {"not_yet_published", "no_source", "needs_key", "mapping_gap", "pipeline_bug", "stale"} <= codes
    if fa["fields"]:
        f, e = next(iter(fa["fields"].items()))
        assert e["reason"] is None or e["reason"]["sync_code"] in {"fresh", "due", "not_yet_published",
                                                                    "source_unavailable", "no_source", "needs_key",
                                                                    "failed"}
    hk = call(env, "GET", "/api/v1/meta/field-availability", params={"region": "HK", "fields": ["roic", "sector"],
                                                                      "include_coverage": True})
    assert hk["region"] == "HK" and set(hk["fields"]) <= {"roic", "sector"}
    ca = call(env, "GET", "/api/v1/companies/{security_id}/availability", {"security_id": ids["sid"]},
              params={"fields": ["roic", "si.pct_float", "pe_ttm", "no_such_field"], "all_fields": True})
    assert ca["security_id"] == ids["sid"] and set(ca["reasons"]) == {"roic", "si.pct_float", "pe_ttm", "no_such_field"}
    for r in ca["reasons"].values():
        assert r["code"] and r["message"] and isinstance(r["has_value"], bool)
    dflt = call(env, "GET", "/api/v1/companies/{security_id}/availability", {"security_id": ids["us"]})
    assert all(not r["has_value"] for r in dflt["reasons"].values())
    call(env, "GET", "/api/v1/companies/{security_id}/availability", {"security_id": "NOPE"}, status=404)


def test_market_macro(env):
    mo = call(env, "GET", "/api/v1/market/overview")
    assert mo["indices"] and mo["sector_performance"] and mo["regime"]
    # v1.30 / F21: liquid movers server-side by default (known ADV, cap or high ADV, USD price floor)
    mf = mo["movers_filter"]
    assert mf["liquid"] is True and mf["unknown_adv_passes"] is False and mf["limit"] == 25
    for m in mo["gainers"] + mo["losers"]:
        assert m["adv_usd_20d"] is not None and m["adv_usd_20d"] >= mf["min_adv_usd"]
        cap = m["security"]["market_cap_usd"]
        assert (cap is not None and cap >= mf["min_mcap_usd"]) or m["adv_usd_20d"] >= mf["unknown_mcap_min_adv_usd"]
    allm = call(env, "GET", "/api/v1/market/overview", params={"liquid": False, "movers_limit": 5})
    assert allm["gainers"] and len(allm["gainers"]) <= 5 and allm["movers_filter"]["unknown_adv_passes"] is True
    assert mo["yield_curves"] and mo["yield_curves"][0]["curve_id"] == "UST"
    assert mo["indices"][0]["spark_points"]
    mo2 = call(env, "GET", "/api/v1/market/overview", params={"regions": ["US", "EU"]})
    assert {i["region"] for i in mo2["indices"]} <= {"US", "EU"}
    call(env, "GET", "/api/v1/market/overview", params={"region": "XX"}, status=422)
    reg = call(env, "GET", "/api/v1/macro/regime", params={"scope": "US", "years": 5})
    assert reg["history"] and reg["regime_sector_returns"]
    # O2.2a (v1.29): explicit data-as-of per region, aligned 1D, labelled regime scope, credit spreads, true driver units
    assert mo["data_as_of"] and all(x["as_of"] and x["n_securities"] > 0 for x in mo["data_as_of"])
    assert isinstance(mo["dates_aligned"], bool) and mo["common_date"] <= mo["latest_date"]
    assert all("as_of" in i for i in mo["indices"]) and all("n_ret_1d" in r for r in mo["sector_performance"])
    assert all(r["n_ret_1d"] <= r["n_securities"] for r in mo["sector_performance"])
    assert mo["regime"]["scope"] == "US" and mo["regime"]["modelled"] is True and mo["regime_scope"]["fallback"] is False
    assert isinstance(mo["credit_spreads"], list)
    for c in mo["credit_spreads"]:
        assert c["percentile_method"] == "empirical" and c["spark_window_days"] == 365
    eu = call(env, "GET", "/api/v1/market/overview", params={"region": "EU"})
    assert eu["regime_scope"]["requested_scope"] == "EU" and eu["regime_scope"]["modelled"] is False
    if eu["regime"] is not None:  # labelled GLOBAL fallback, never an unlabelled US model
        assert eu["regime"]["fallback"] is True and eu["regime"]["scope"] == "GLOBAL"
    nm = call(env, "GET", "/api/v1/macro/regime", params={"scope": "KR"})
    assert nm["modelled"] is False and nm["status"] == "not_modelled" and nm["current"]["label"] == "not_modelled"
    assert nm["current"]["probabilities"] == {} and nm["inputs"] == []
    assert reg["modelled"] is True and reg["status"] == "modelled"
    assert all(i["unit"] != "z" and len(i["z_points"]) == len(i["points"]) for i in reg["inputs"])
    dash = call(env, "GET", "/api/v1/macro/dashboard", params={"country": "US"})
    assert dash["groups"] and dash["groups"][0]["series"][0]["scale"] in ("percent", "level")
    for ser in (x for g in dash["groups"] for x in g["series"]):  # O2.2a / F10: rate-of-change tiles
        assert ser["display_transform"] in ("level", "yoy", "qoq_saar", "mom_diff")
        if ser["series_id"] in ("fred:CPIAUCSL", "fred:CPILFESL"):
            assert ser["display_transform"] == "yoy" and ser["display_unit"] == "fraction"
        if ser["series_id"] == "fred:DGS10":
            assert ser["display_transform"] == "level"
    lst = call(env, "GET", "/api/v1/macro/series", params={"category": "rates"})
    assert any(x["series_id"] == "fred:DGS10" for x in lst)
    data = call(env, "GET", "/api/v1/macro/series/{series_id}", {"series_id": "fred:DGS10"},
                params={"transform": "diff", "from": "2025-01-01"})
    assert data["points"] and data["transform"] == "diff"
    call(env, "GET", "/api/v1/macro/series/{series_id}", {"series_id": "fred:NOPE"}, status=404)
    yoy = call(env, "GET", "/api/v1/macro/series/{series_id}", {"series_id": "fred:CPIAUCSL"}, params={"transform": "yoy"})
    assert any(p["value"] is not None for p in yoy["points"])


def test_sectors(env, ids):
    lst = call(env, "GET", "/api/v1/sectors")
    assert len(lst) == 11 and sum(s["n_securities"] for s in lst) == N_SEC
    det = call(env, "GET", "/api/v1/sectors/{sector_id}", {"sector_id": ids["sector"]})
    assert det["leaders"] and det["factor_heatmap"]
    assert det["kpi_distributions"] and det["kpi_distributions"][0]["values"]
    # v1.39.0: band metrics carry their target; the sector view lists the KPI set covering most companies first
    assert all("target" in x for x in det["kpi_distributions"])
    from app.api.routers._common import kpi_sets as _kpi_sets

    n_of = {s["subsector_id"]: s["n_securities"] for s in det["summary"].get("subsectors") or []}
    cnt = [sum(n_of.get(x, 0) for x in _kpi_sets()[k]["subsectors"]) for k in det.get("kpi_set_ids") or []]
    assert cnt == sorted(cnt, reverse=True)
    call(env, "GET", "/api/v1/sectors/{sector_id}", {"sector_id": ids["sector"]}, params={"subsector_id": ids["subsector"]})
    call(env, "GET", "/api/v1/sectors/{sector_id}", {"sector_id": "NOPE"}, status=404)
    sc = call(env, "GET", "/api/v1/sectors/{sector_id}/scatter", {"sector_id": ids["sector"]}, params={"x": "roic", "y": "value"})
    assert sc and sc[0]["x"] is not None
    call(env, "GET", "/api/v1/sectors/{sector_id}/scatter", {"sector_id": ids["sector"]}, params={"x": "bogus", "y": "value"},
         status=422)


def test_screener(env):
    fields = call(env, "GET", "/api/v1/screener/fields")
    assert any(f["id"] == "ml_expected_excess_3m" for f in fields)
    cols = ["isin", "ret_12m", "pe_ntm", "ev_ebit", "fcf_yield", "roic", "revenue_growth", "net_debt_ebitda", "dividend_yield",
            "ml_expected_excess_3m", "bogus_column"]
    res = call(env, "POST", "/api/v1/screener/query", json={"columns": cols, "limit": 1000})
    assert res["total"] == N_SEC and len(res["rows"]) == N_SEC
    comps = [r["composite"] for r in res["rows"] if r["composite"] is not None]
    assert comps == sorted(comps, reverse=True)
    row = res["rows"][0]
    assert set(cols) <= set(row["values"]) and row["values"]["bogus_column"] is None
    assert row["values"]["isin"] and row["ml_expected_excess_3m"] is not None
    custom = call(env, "POST", "/api/v1/screener/query",
                  json={"factor_weights": {"momentum": 3, "value": 1}, "regions": ["US"], "limit": 20,
                        "filters": [{"field": "market_cap_usd", "op": "gt", "value": 0},
                                    {"field": "sector_id", "op": "in", "value": ["TECH", "FINS", "HLTH", "INDU", "CDIS"]}]})
    assert custom["weights_used"] == {"momentum": 0.75, "value": 0.25}
    assert custom["composite_label"] == "descriptive" and res["composite_label"] == "descriptive"   # A8
    assert all(r["security"]["region"] == "US" for r in custom["rows"])
    top = custom["rows"][0]
    expect = 0.75 * top["factor_scores"]["momentum"] + 0.25 * top["factor_scores"]["value"]
    assert abs(top["composite"] - expect) < 1e-6 and top["grade"] in "ABCDE"
    asc = call(env, "POST", "/api/v1/screener/query", json={"sort_by": "chg_1d", "sort_dir": "asc", "limit": 5, "offset": 5})
    assert asc["offset"] == 5 and len(asc["rows"]) == 5
    call(env, "POST", "/api/v1/screener/query", json={"filters": [{"field": "nope", "op": "gt", "value": 1}]}, status=422)
    call(env, "POST", "/api/v1/screener/query", json={"factor_weights": {"nope": 1}}, status=422)
    csv = call(env, "POST", "/api/v1/screener/export", json={"columns": ["roic"], "limit": 5})
    assert csv.count("\n") == N_SEC + 1 and csv.startswith("rank,ticker")


def test_company(env, ids):
    sid = ids["us"]
    ov = call(env, "GET", "/api/v1/companies/{security_id}/overview", {"security_id": sid})
    assert ov["quote"] and ov["composite"]["score"] is not None and len(ov["factors"]) == 10
    assert ov["composite"]["label"] == "descriptive" and ov["composite"]["validated"] is False   # coverage v3 A8
    assert ov["factors"][0]["peer_median"] is not None and ov["key_metrics"] and ov["top_zone"]["targets"]
    assert ov["beta_1y"] is not None and ov["fx_to_usd"] == 1.0 and ov["data_status"] == "ok"
    call(env, "GET", "/api/v1/companies/{security_id}/overview", {"security_id": "SNOPE"}, status=404)
    call(env, "GET", "/api/v1/companies/{security_id}/overview", {"security_id": sid}, params={"profile_id": "x"}, status=422)
    px = call(env, "GET", "/api/v1/companies/{security_id}/prices", {"security_id": sid})
    assert px["interval"] == "D" and len(px["bars"]) > 1000 and px["events"]
    pw = call(env, "GET", "/api/v1/companies/{security_id}/prices", {"security_id": sid},
              params={"from": "2010-01-01", "adjusted": "false", "interval": "D"})
    assert pw["interval"] == "W"  # > 10y daily is auto-resampled
    assert px["first_date"] and px["daily_from"] and px["first_date"] <= px["bars"][0]["t"]
    pm = call(env, "GET", "/api/v1/companies/{security_id}/prices", {"security_id": sid}, params={"interval": "M"})
    assert 50 < len(pm["bars"]) < 70
    fin = call(env, "GET", "/api/v1/companies/{security_id}/financials", {"security_id": sid})
    assert len(fin["periods"]) == 10 and any(ln["item_id"] == "revenue" for ln in fin["lines"])
    assert any(ln["statement"] == "BS" and ln["values"][-1] is not None for ln in fin["lines"])
    usd = call(env, "GET", "/api/v1/companies/{security_id}/financials", {"security_id": sid},
               params={"period": "Q", "currency": "USD", "statement": "IS", "common_size": "true"})
    rev = next(ln for ln in usd["lines"] if ln["item_id"] == "revenue")
    assert usd["currency"] == "USD" and all(v == 1.0 for v in rev["values"] if v is not None)
    pit = call(env, "GET", "/api/v1/companies/{security_id}/financials", {"security_id": sid}, params={"as_of": "2020-06-30"})
    assert pit["periods"] and pit["periods"][-1]["period_end"] <= "2019-12-31"
    rat = call(env, "GET", "/api/v1/companies/{security_id}/ratios", {"security_id": sid}, params={"period": "TTM"})
    assert rat["metrics"] and rat["periods"]
    scs = call(env, "GET", "/api/v1/companies/{security_id}/scores", {"security_id": sid}, params={"history_days": 900})
    assert len(scs["history"]) > 20 and scs["metrics"] and scs["accounting_flags"]["altman_zone"]
    af = scs["accounting_flags"]  # O2.2a / F15: recalibrated model-based Beneish screen
    assert af["beneish_level"] in ("flag", "watch", "none", "not_applicable") and af["beneish"]["model_based"] is True
    assert af["beneish_flag_adjusted"] in (True, False, None)
    peers = call(env, "GET", "/api/v1/companies/{security_id}/peers", {"security_id": sid})
    assert peers["rows"][0]["is_self"] and peers["metrics"]
    call(env, "GET", "/api/v1/companies/{security_id}/peers", {"security_id": sid}, params={"level": "sector_global", "limit": 5})
    fc = call(env, "GET", "/api/v1/companies/{security_id}/forecast", {"security_id": sid})
    assert len(fc["scenarios"]) == 3 and fc["sensitivity_grid"] and fc["actual_years"]
    grid = fc["sensitivity_grid"]
    mid = grid["fair_value"][grid["wacc"].index(grid["base_wacc"])][grid["terminal_growth"].index(grid["base_terminal_growth"])]
    base = next(s for s in fc["scenarios"] if s["scenario"] == "base")["valuation"]["fair_value_per_share"]
    assert abs(mid - base) / base < 1e-6  # upstream grid (forecast_valuation.sensitivity_grid) used as-is
    assert fc["model"] == "dcf" and fc["fair_value_dcf"] and fc["valuation_confidence"] == 70.0
    assert fc["cost_of_capital"]["wacc"] == 0.085 and ov["cost_of_capital"]["erp"] == 0.05
    moat = call(env, "GET", "/api/v1/companies/{security_id}/moat", {"security_id": sid})
    assert len(moat["components"]) == 8 and moat["history"] and moat["roic_vs_wacc"] and moat["market_share_series"]
    assert moat["components"][0]["threshold"] == 55.0 and moat["roic_vs_wacc"][0]["hurdle"] == 0.08
    assert moat["moat_sources"] == [{"type": "scale", "strength": 70.0}]
    ins = call(env, "GET", "/api/v1/companies/{security_id}/insiders", {"security_id": sid})
    assert ins["coverage"] == "full"
    sen = call(env, "GET", "/api/v1/companies/{security_id}/sentiment", {"security_id": sid})
    assert sen["daily"] and sen["items"] and sen["lazy_prices"] and sen["items"][0]["relevance"] is not None
    call(env, "GET", "/api/v1/companies/{security_id}/events", {"security_id": sid}, params={"days": 120})
    tech = call(env, "GET", "/api/v1/companies/{security_id}/technicals", {"security_id": sid})
    assert tech["zones"] and tech["levels"] and tech["level_stats"] and tech["last_close"] > 0
    assert tech["setup"]["target2"] and tech["setup"]["score_components"]["trend"] == 90.0 and tech["indicators"]["rsi14"]
    assert tech["pivots"] and tech["type_reliability"] and tech["levels"][0]["frontend_type"]
    mdl = call(env, "GET", "/api/v1/companies/{security_id}/model", {"security_id": sid})
    assert mdl["model_type"] == "ensemble" and len(mdl["models"]) == 3 and mdl["models"][0]["is_production"]
    assert mdl["shap_top"] and mdl["history"] and mdl["expected_excess_return"] is not None and mdl["shap_unit"]
    call(env, "GET", "/api/v1/companies/{security_id}/model", {"security_id": sid}, params={"horizon": "1M"})
    mac = call(env, "GET", "/api/v1/companies/{security_id}/macro", {"security_id": sid})
    assert mac["betas"] and mac["regime_returns"] and mac["current_regime"]
    fl = call(env, "GET", "/api/v1/companies/{security_id}/filings", {"security_id": sid}, params={"form_class": "annual"})
    assert fl and all(f["form_class"] == "annual" for f in fl)


def test_company_events_linked(env):
    """At least one issuer has GDELT events linked: company events + securities[] on the event."""
    c = env["client"]
    for x in env["info"]["securities"]:
        ev = c.get(f"/api/v1/companies/{x['security_id']}/events", params={"days": 120}).json()
        if ev["events"]:
            assert ev["events"][0]["securities"] and ev["events"][0]["headline"]
            validate("GET", "/api/v1/companies/{security_id}/events", 200, ev)
            return
    pytest.fail("no linked events in fixture")


def test_instruments(env):
    lst = call(env, "GET", "/api/v1/instruments")
    ids_ = {x["security_id"] for x in lst["items"]}
    assert "IDX:^GSPC" in ids_ and lst["groups"] and lst["fx_note"]
    g = next(x for x in lst["items"] if x["security_id"] == "IDX:^GSPC")
    assert g["group"] == "equity_idx" and len(g["spark"]) > 40 and g["first_date"]
    assert call(env, "GET", "/api/v1/instruments", params={"type": "fx"})["items"] == []
    call(env, "GET", "/api/v1/instruments", params={"type": "bond"}, status=422)
    d = call(env, "GET", "/api/v1/instruments/{security_id}", {"security_id": "IDX:^GSPC"})
    assert [s["window"] for s in d["stats"]] == ["1Y", "5Y", "10Y", "Max"] and d["stats"][0]["ann_vol"] > 0
    assert d["rolling_vol"] and d["correlations"] and d["note"] is None
    call(env, "GET", "/api/v1/instruments/{security_id}", {"security_id": "IDX:NOPE"}, status=404)
    px = call(env, "GET", "/api/v1/instruments/{security_id}/prices", {"security_id": "IDX:^GSPC"}, params={"from": "1990-01-01", "interval": "M"})
    assert px["interval"] == "M" and px["first_date"] and 100 < len(px["bars"]) < 140
    call(env, "GET", "/api/v1/instruments/{security_id}/prices", {"security_id": "IDX:NOPE"}, status=404)


def test_instrument_helpers():
    from datetime import date as d_

    from app.api.routers.instruments import group_of, window_stats
    assert group_of("EURUSD=X", "fx") == "fx_majors" and group_of("EURGBP=X", "fx") == "fx_eur"
    assert group_of("GBPJPY=X", "fx") == "fx_minors" and group_of("USDBRL=X", "fx") == "fx_em"
    assert group_of("CL=F", "future") == "energy" and group_of("KC=F", "future") == "softs" and group_of("LE=F", "future") == "livestock"
    assert group_of("^VIX", "index") == "volatility" and group_of("^TNX", "index") == "rates_idx" and group_of("^GSPC", "index") == "equity_idx"
    assert group_of("GLD", "etf") == "etf_commodity" and group_of("EWZ", "etf", "iShares MSCI Brazil") == "etf_intl" and group_of("SPY", "etf") == "etf_us"
    st = window_stats([d_(2020, 1, 1), d_(2021, 1, 1), d_(2022, 1, 1)], [100.0, 50.0, 121.0], periods_per_year=1)
    assert abs(st["ann_return"] - 0.1) < 0.01 and abs(st["max_drawdown"] + 0.5) < 1e-9 and st["n"] == 3


def test_weekly_compaction(tmp_path):
    """prices_w aggregation semantics: first open, max high, min low, last close, sum volume, adjusted per day."""
    import duckdb
    con = duckdb.connect()
    con.execute("""CREATE TABLE d AS SELECT * FROM (VALUES
        ('S1', DATE '2001-01-02', 10.0, 12.0, 9.0, 11.0, 5.5, 100.0),
        ('S1', DATE '2001-01-03', 11.0, 15.0, 10.0, 14.0, 7.0, 50.0),
        ('S1', DATE '2001-01-05', 14.0, 14.5, 8.0, 12.0, 6.0, 25.0)) t(security_id, date, open, high, low, close, adj_close, volume)""")
    r = con.execute("""SELECT max(date), arg_min(open, date), max(high), min(low), arg_max(close, date), sum(volume),
                              arg_min(open * adj_close / close, date), min(low * adj_close / close)
                       FROM d GROUP BY security_id, date_trunc('week', date)""").fetchone()
    assert r == (__import__("datetime").date(2001, 1, 5), 10.0, 15.0, 8.0, 12.0, 175.0, 5.0, 4.0)


def test_insiders(env):
    feed = call(env, "GET", "/api/v1/insiders/feed", params={"days": 800, "min_value_usd": 0})
    assert feed["total"] > 0 and feed["items"]
    buys = call(env, "GET", "/api/v1/insiders/feed", params={"days": 800, "min_value_usd": 0, "txn_type": "buy", "region": "US"})
    assert all(i["transaction"]["txn_type"] == "buy" for i in buys["items"])
    assert any(i["transaction"]["cluster_id"] for i in buys["items"])
    assert all(i["transaction"]["features"] and i["transaction"]["anomaly_score"] is not None for i in buys["items"])
    call(env, "GET", "/api/v1/insiders/top-signals")
    call(env, "GET", "/api/v1/insiders/top-signals", params={"direction": "bearish", "limit": 5})
    cl = call(env, "GET", "/api/v1/insiders/clusters", params={"days": 60})
    assert cl and cl[0]["n_buyers"] >= 2


def test_events(env):
    feed = call(env, "GET", "/api/v1/events/feed", params={"days": 30})
    assert feed["items"] and feed["items"][0]["headline"]
    full = call(env, "GET", "/api/v1/events/feed", params={"days": 120, "min_articles": 0, "limit": 500})
    urls = [i["source_url"] for i in full["items"]]
    assert len(urls) == len(set(urls)) and full["total"] < 3000  # one row per article (fixture repeats 10% of URLs)
    assert not any(i["headline"][:1].isdigit() for i in full["items"] if i["headline"])
    call(env, "GET", "/api/v1/events/feed", params={"country": "US", "category": "conflict", "days": 90, "min_articles": 0})
    mp = call(env, "GET", "/api/v1/events/map", params={"days": 30})
    assert mp and all(len(m["country"]) == 2 for m in mp) and any(m["change_7d"] is not None for m in mp)
    tl = call(env, "GET", "/api/v1/events/timeline", params={"days": 60})
    assert tl
    att = call(env, "GET", "/api/v1/events/attention", params={"days": 14, "limit": 10})
    assert len(att) == 10


def test_events_v2_without_v2_tables(env):
    """events-v2 endpoints on a lake without the v2 tables: valid empty / pending payloads (data-backed coverage in
    tests/events_v2/test_api.py)."""
    tx = call(env, "GET", "/api/v1/events/v2/taxonomy")
    assert len(tx["themes"]) >= 35 and tx["data_status"] == "pending"
    mp = call(env, "GET", "/api/v1/events/v2/map", params={"days": 7, "theme": ["oil_gas"]})
    assert mp["countries"] == [] and mp["data_status"] == "pending"
    tl = call(env, "GET", "/api/v1/events/v2/timeline", params={"label_type": "country", "label_id": "US"})
    assert tl["series"][0]["points"] == []
    st = call(env, "GET", "/api/v1/events/v2/stories", params={"country": ["US"], "theme": ["inflation"]})
    assert st["total"] == 0 and st["items"] == []
    call(env, "GET", "/api/v1/events/v2/stories/{story_id}", {"story_id": "1"}, status=404)
    pn = call(env, "GET", "/api/v1/events/v2/labels/{label_type}/{label_id}", {"label_type": "sector", "label_id": "FINS"})
    assert pn["series"] == [] and pn["top_stories"] == []
    sh = call(env, "GET", "/api/v1/events/v2/shocks", params={"days": 7})
    assert sh["items"] == []
    ed = call(env, "GET", "/api/v1/events/v2/event-dates", params={"label_type": "country", "label_id": "GLOBAL"})
    assert ed["dates"] == []



def test_events_models_without_tables(env, ids):
    """event-models endpoints on a lake without the model tables: valid empty / pending payloads (data-backed coverage
    in tests/events_models/test_api.py)."""
    P = "/api/v1/events/models"
    h = call(env, "GET", f"{P}/hotspots")
    assert h["items"] == [] and h["model_based"] is True and h["data_status"] == "pending"
    assert call(env, "GET", f"{P}/spillovers")["models"] == []
    assert call(env, "GET", f"{P}/connectedness", params={"universe": "sector_vol"})["nodes"] == []
    assert call(env, "GET", f"{P}/irf")["components"] == []
    assert call(env, "GET", f"{P}/escalation")["items"] == []
    assert call(env, "GET", f"{P}/changepoints")["items"] == []
    assert call(env, "GET", f"{P}/gpr")["series"] == []
    assert call(env, "GET", f"{P}/event-studies", params={"theme": "sanctions"})["expected"] is None
    co = call(env, "GET", P + "/company/{security_id}", {"security_id": ids["sid"]})
    assert co["security"]["security_id"] == ids["sid"] and co["exposure_score"] is None
    call(env, "GET", P + "/company/{security_id}", {"security_id": "SNOPE"}, status=404)

def test_models(env, ids):
    runs = call(env, "GET", "/api/v1/models/runs")
    assert len(runs) == 7 and any(r["n_features"] for r in runs)
    call(env, "GET", "/api/v1/models/runs", params={"model_type": "ensemble", "horizon": "3M"})
    det = call(env, "GET", "/api/v1/models/runs/{model_run_id}", {"model_run_id": "M_ENS_3M"})
    assert det["decile_returns"] and det["fold_metrics"][0]["train_start"] and det["benchmark_series"]
    call(env, "GET", "/api/v1/models/runs/{model_run_id}", {"model_run_id": "nope"}, status=404)
    pr = call(env, "GET", "/api/v1/models/predictions", params={"horizon": "3M", "limit": 10})
    assert pr["total"] == N_SEC and pr["items"][0]["rank_pct"] >= pr["items"][-1]["rank_pct"]
    call(env, "GET", "/api/v1/models/predictions", params={"horizon": "3M", "region": "US", "date": "2026-10-09"})
    p = call(env, "POST", "/api/v1/models/runs/{model_run_id}/promote", {"model_run_id": "M_XGB_3M"})
    assert p["is_production"]
    tr = call(env, "POST", "/api/v1/models/train", json={"model_type": "xgboost_rank", "horizon": "3M"}, status=202)
    assert tr["job_id"] == "ml_train_xgb"
    call(env, "POST", "/api/v1/models/train", json={"model_type": "bogus", "horizon": "3M"}, status=422)


def test_watchlists_and_alerts(env, ids):
    wl = call(env, "POST", "/api/v1/watchlists", json={"name": "Core", "description": "test"}, status=201)
    wid = wl["watchlist_id"]
    assert call(env, "GET", "/api/v1/watchlists")[0]["watchlist_id"] == wid
    it = call(env, "POST", "/api/v1/watchlists/{watchlist_id}/items", {"watchlist_id": wid},
              json={"security_id": ids["sid"], "note": "n"}, status=201)
    assert it["security_id"] == ids["sid"]
    call(env, "POST", "/api/v1/watchlists/{watchlist_id}/items", {"watchlist_id": wid}, json={"security_id": ids["us"]}, status=201)
    call(env, "POST", "/api/v1/watchlists/{watchlist_id}/items", {"watchlist_id": wid}, json={"security_id": "SNOPE"}, status=404)
    det = call(env, "GET", "/api/v1/watchlists/{watchlist_id}", {"watchlist_id": wid})
    assert det["watchlist"]["item_count"] == 2 and len(det["rows"]) == 2
    upd = call(env, "PATCH", "/api/v1/watchlists/{watchlist_id}", {"watchlist_id": wid}, json={"name": "Core 2"})
    assert upd["name"] == "Core 2"
    scr = call(env, "POST", "/api/v1/screener/query", json={"universe_id": "watch"})
    assert scr["total"] == 2
    call(env, "DELETE", "/api/v1/watchlists/{watchlist_id}/items/{security_id}", {"watchlist_id": wid, "security_id": ids["us"]},
         status=204)
    a = call(env, "POST", "/api/v1/alerts", json={"security_id": ids["sid"], "rule_type": "price_cross",
                                                  "params": {"level": 100, "direction": "above"}}, status=201)
    call(env, "POST", "/api/v1/alerts", json={"watchlist_id": wid, "rule_type": "price_change_pct_1d", "params": {"pct": 0.05}},
         status=201)
    call(env, "POST", "/api/v1/alerts", json={"security_id": ids["sid"], "rule_type": "score_change",
                                              "params": {"mode": "level", "level": 70, "profile_id": "balanced"}}, status=201)
    call(env, "POST", "/api/v1/alerts", json={"security_id": ids["sid"], "rule_type": "price_cross", "params": {}}, status=422)
    # v1.39.0: payloads of the old watchlist editor are normalised (they were rejected) and names are resolved
    leg = call(env, "POST", "/api/v1/alerts", json={"security_id": ids["sid"], "rule_type": "price_change_pct_1d",
                                                    "params": {"pct_change_1d": -0.05, "direction": "below"}}, status=201)
    assert leg["params"]["pct"] == 0.05 and leg["params"]["direction"] == "down"
    assert leg["ticker"] == "AAPL" and leg["security_name"]
    sc = call(env, "POST", "/api/v1/alerts", json={"security_id": ids["sid"], "rule_type": "score_change",
                                                   "params": {"profile_id": "balanced", "level": 70, "direction": "above"}}, status=201)
    assert sc["params"]["mode"] == "level" and sc["params"]["direction"] == "up"
    al = call(env, "GET", "/api/v1/alerts")
    assert len(al) == 5
    assert next(x for x in al if x["watchlist_id"] == wid)["watchlist_name"] == "Core 2"
    for x in (leg, sc):
        call(env, "DELETE", "/api/v1/alerts/{alert_id}", {"alert_id": x["alert_id"]}, status=204)
    assert len(call(env, "GET", "/api/v1/alerts")) == 3
    au = call(env, "PATCH", "/api/v1/alerts/{alert_id}", {"alert_id": a["alert_id"]}, json={"enabled": False, "cooldown_days": 2})
    assert au["enabled"] is False and au["cooldown_days"] == 2
    # alert events are written by the worker (alerts_eval); simulate one
    from app.api import app_state

    app_state.transact(lambda con: con.execute(
        "INSERT INTO alert_events VALUES ('E1', ?, ?, now(), DATE '2026-10-09', 'crossed', '{}', false)", [a["alert_id"], ids["sid"]]))
    evs = call(env, "GET", "/api/v1/alerts/events", params={"unread_only": "true"})
    assert evs[0]["ticker"] == "AAPL" and evs[0]["alert_id"] == a["alert_id"]
    call(env, "POST", "/api/v1/alerts/events/mark-read", json={"event_ids": ["E1"]}, status=204)
    assert call(env, "GET", "/api/v1/alerts/events", params={"unread_only": "true"}) == []
    call(env, "DELETE", "/api/v1/alerts/{alert_id}", {"alert_id": a["alert_id"]}, status=204)
    call(env, "DELETE", "/api/v1/alerts/{alert_id}", {"alert_id": a["alert_id"]}, status=404)
    call(env, "DELETE", "/api/v1/watchlists/{watchlist_id}", {"watchlist_id": wid}, status=204)
    call(env, "GET", "/api/v1/watchlists/{watchlist_id}", {"watchlist_id": wid}, status=404)


def test_cache_etag_gzip_cors(env):
    c = env["client"]
    r1 = c.get("/api/v1/sectors")
    r2 = c.get("/api/v1/sectors")
    assert r2.headers["x-cache"] == "hit" and r1.headers["etag"] == r2.headers["etag"]
    assert r2.headers["x-serving-run-id"] == env["report"].run_id
    assert c.get("/api/v1/sectors", headers={"If-None-Match": r1.headers["etag"]}).status_code == 304
    assert r1.headers.get("content-encoding") == "gzip"
    pre = c.options("/api/v1/sectors", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert pre.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "/api/v1/system/status" not in str(c.get("/api/v1/system/status").headers.get("x-cache"))
    spec = c.get("/openapi.json").json()
    assert spec["info"]["version"] == "1.50.0"
    assert c.get("/docs").status_code == 200


def test_snapshot_flip_and_retention(env):
    """A second build flips CURRENT, the API switches without restart, and old snapshots are pruned to 3."""
    from app.api.serving import build_serving_db, snapshot_files

    old = env["client"].get("/health").json()["serving_run_id"]
    reps = [build_serving_db(env["settings"]) for _ in range(3)]
    snapshots._checked = 0.0  # skip the 1 s re-check throttle
    new = env["client"].get("/health").json()["serving_run_id"]
    assert new == reps[-1].run_id != old
    assert len(snapshot_files(env["settings"])) <= 4  # 3 kept + possibly one still held open by this process


def test_studio_endpoints(env):
    """Model Studio (v1.5): catalogs, preview, sync / async runs, cancel, export, compare, studies CRUD."""
    ts = {"kind": "timeseries", "frequency": "M", "start": "2019-01-01",
          "variables": [{"name": "ret", "source": "prices", "field": "ret", "entities": {"universe": "mvp_us"}},
                        {"name": "d10", "source": "macro", "field": "fred:DGS10", "transforms": [{"op": "diff"}]},
                        {"name": "d10_l1", "source": "macro", "field": "fred:DGS10",
                         "transforms": [{"op": "diff"}, {"op": "lag", "k": 1}]}]}
    cat = call(env, "GET", "/api/v1/studio/catalog/datasets")
    assert {i["source"] for i in cat["items"]} >= {"prices", "ratios", "macro", "factor_scores"}
    page = call(env, "GET", "/api/v1/studio/catalog/variables", params={"source": "macro", "q": "dgs10"})
    assert page["total"] >= 1 and page["items"][0]["field"] == "fred:DGS10"
    meths = call(env, "GET", "/api/v1/studio/catalog/methods")
    assert {"ols", "arima", "kmeans"} <= {m["id"] for m in meths}
    from app.modeling.registry import PRESETS_DIR

    pres = call(env, "GET", "/api/v1/studio/presets")
    assert len(pres) == len(list(PRESETS_DIR.glob("*.json"))) >= 60  # 22 + Studio v2 S3 recipes (v1.50)
    assert sum(1 for x in pres if x.get("method_note")) >= 40
    pv = call(env, "POST", "/api/v1/studio/datasets/preview", json={"dataset": ts, "n": 10})
    assert pv["n_rows"] > 12 and len(pv["rows"]) == 10 and pv["coverage"][0]["column"] == "ret"
    bad = call(env, "POST", "/api/v1/studio/datasets/preview", status=422,
               json={"dataset": {"kind": "panel", "variables": [{"name": "m", "source": "macro", "field": "fred:DGS10"}]}})
    assert bad["type"] == "urn:studio:dataset"
    study = {"name": "ret on rates", "method": "ols", "y": "ret", "x": ["d10", "d10_l1"], "dataset": ts,
             "params": {"cov_type": "HAC"}}
    r1 = call(env, "POST", "/api/v1/studio/runs", json={"study": study, "mode": "sync"})
    rid = r1["run"]["run_id"]
    assert r1["run"]["status"] == "completed" and r1["result"]["result"]["coefficients"]
    got = call(env, "GET", "/api/v1/studio/runs/{run_id}", {"run_id": rid})
    assert got["result"]["reproducibility"]["spec_hash"] == r1["result"]["reproducibility"]["spec_hash"]
    r = env["client"].get(f"/api/v1/studio/runs/{rid}/export.py")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/x-python") and "def build(" in r.text
    CALLED.add(("GET", "/api/v1/studio/runs/{run_id}/export.py"))
    look = dict(study, x=["fwd"], dataset=dict(ts, variables=ts["variables"] + [
        {"name": "fwd", "source": "prices", "field": "ret", "entities": {"universe": "mvp_us"},
         "transforms": [{"op": "fwd_compound", "k": 3}]}]))
    rej = call(env, "POST", "/api/v1/studio/runs", json={"study": look}, status=422)
    assert rej["type"] == "urn:studio:lookahead"
    xs = {"name": "factor clusters", "method": "kmeans", "x": ["q", "v"], "params": {"k_min": 2, "k_max": 4},
          "dataset": {"kind": "cross_section", "frequency": "M", "entities": {"universe": "mvp_us"},
                      "variables": [{"name": "q", "source": "factor_scores", "field": "quality"},
                                    {"name": "v", "source": "factor_scores", "field": "value"}]}}
    r2 = call(env, "POST", "/api/v1/studio/runs", json={"study": xs})
    assert r2["run"]["mode"] == "sync" and r2["result"]["result"]["clusters"]["n_clusters"] >= 2
    # S1 (v1.48): learned estimate + studio lane status (no lane in tests: worker fallback)
    est = call(env, "POST", "/api/v1/studio/estimate", json={"study": xs})
    assert est["path"] == "sync" and est["will_run"] == "sync" and est["total_s"] >= est["fit_s"] > 0
    assert r2["run"]["lane"] == "sync" and r2["run"]["estimate"]["path"] == "sync" and r2["preview"] is None
    ln = call(env, "GET", "/api/v1/studio/lane")
    assert ln["alive"] is False and ln["fallback"] == "worker" and set(ln["accuracy"]) == {"studio", "backtest"}
    # Phase B (v1.6): new plot kinds validate against the contract
    assert all("passed" in t for t in r1["result"]["result"]["tests"])
    dw = next(t for t in r1["result"]["result"]["tests"] if t["name"] == "Durbin-Watson")
    assert dw["passed"] == (1.5 <= dw["statistic"] <= 2.5)
    macro = {"kind": "timeseries", "frequency": "M", "macro_alignment": "period", "start": "2018-01-01",
             "variables": [{"name": "d10", "source": "macro", "field": "fred:DGS10", "agg": "mean", "transforms": [{"op": "diff"}]},
                           {"name": "d2", "source": "macro", "field": "fred:DGS2", "agg": "mean", "transforms": [{"op": "diff"}]}]}
    v = call(env, "POST", "/api/v1/studio/runs", json={"mode": "sync", "study": {
        "method": "var", "x": ["d10", "d2"], "params": {"lags": 1, "irf_periods": 6}, "dataset": macro}})
    assert {"irf", "fevd"} <= set(v["result"]["result"]["plots"])
    d = call(env, "POST", "/api/v1/studio/runs", json={"mode": "sync", "study": {
        "method": "did", "y": "ret", "params": {"design": "event_study", "leads": 3, "lags": 3, "treatment": {
            "mode": "attribute", "attribute": "region", "values": ["US"], "date": "2022-01-01"}},
        "dataset": {"kind": "panel", "frequency": "M", "start": "2021-01-01", "end": "2022-12-31", "entities": {"universe": "full"},
                    "variables": [{"name": "ret", "source": "prices", "field": "ret"}]}}})
    assert d["result"]["result"]["plots"]["event_path"]["kind"] == "event_path", d["run"]
    ms = call(env, "POST", "/api/v1/studio/runs", json={"mode": "sync", "study": {
        "method": "markov_switching", "y": "d10", "params": {"search_reps": 2}, "dataset": macro}})
    assert ms["result"]["result"]["plots"]["regime_prob"]["kind"] == "regime_prob", ms["run"]
    lst = call(env, "GET", "/api/v1/studio/runs", params={"limit": 50})
    row = next(x for x in lst if x["run_id"] == v["run"]["run_id"])
    assert row["title"] and row["n_obs"] and row["estimated_seconds"] is not None and row["sync_budget_s"] == 2.0
    # Phase C (v1.7): leaderboard, peer sets, scheduling
    lb = call(env, "POST", "/api/v1/studio/leaderboard", json={"dataset": ts, "y": "ret", "x": ["d10", "d10_l1"], "mode": "sync",
                                                               "cv": {"folds": 3}, "candidates": [{"name": "mean", "method": "mean"},
                                                                                                  {"name": "ols", "method": "ols"}]})
    assert lb["run"]["status"] == "completed", lb["run"]
    ps = call(env, "POST", "/api/v1/studio/runs/{run_id}/peer-set", {"run_id": r2["run"]["run_id"]},
              json={"name": "factor peers"}, status=201)
    lst_ps = call(env, "GET", "/api/v1/studio/peer-sets")
    assert ps["n_members"] > 10 and lst_ps[0]["peer_set_id"] == ps["peer_set_id"] and len(lst_ps[0]["members"]) == ps["n_members"]
    got_ps = call(env, "GET", "/api/v1/studio/peer-sets/{peer_set_id}", {"peer_set_id": ps["peer_set_id"]})
    member = got_ps["members"][0]["security_id"]
    peers = call(env, "GET", "/api/v1/companies/{security_id}/peers", {"security_id": member}, params={"peer_set_id": ps["peer_set_id"]})
    assert peers["level"] == "peer_set" and peers["peer_group_key"].startswith("peer_set:")
    call(env, "DELETE", "/api/v1/studio/peer-sets/{peer_set_id}", {"peer_set_id": ps["peer_set_id"]}, status=204)
    call(env, "GET", "/api/v1/studio/peer-sets/{peer_set_id}", {"peer_set_id": ps["peer_set_id"]}, status=404)
    q = call(env, "POST", "/api/v1/studio/runs", json={"study": study, "mode": "async"}, status=202)
    assert q["run"]["status"] == "queued" and q["result"] is None
    c = call(env, "DELETE", "/api/v1/studio/runs/{run_id}", {"run_id": q["run"]["run_id"]})
    assert c["status"] == "cancelled"
    runs = call(env, "GET", "/api/v1/studio/runs", params={"limit": 50})
    assert {rid, r2["run"]["run_id"], q["run"]["run_id"]} <= {x["run_id"] for x in runs}
    cmp_ = call(env, "POST", "/api/v1/studio/compare", json={"run_ids": [rid, r2["run"]["run_id"]]})
    assert cmp_["run_ids"] == [rid, r2["run"]["run_id"]] and any(m["metric"] == "n_obs" for m in cmp_["metrics"])
    assert cmp_["oos"]["skipped"]  # OLS / k-means write no out-of-fold artifact
    # Studio v2 S3 (v1.50): conformal option, compare with DM / Clark-West, lint, guided builder, Model Registry
    ridge = {"name": "ridge rates", "method": "regularized", "y": "ret", "x": ["d10", "d10_l1"], "dataset": ts,
             "params": {"model": "ridge", "cv": {"folds": 3, "conformal_alpha": 0.2}}}
    big = call(env, "POST", "/api/v1/studio/runs", json={"study": ridge, "mode": "sync"})
    small = call(env, "POST", "/api/v1/studio/runs", json={"study": dict(ridge, x=["d10"]), "mode": "sync"})
    assert big["result"]["result"]["metrics"]["conformal_coverage"] is not None
    c2 = call(env, "POST", "/api/v1/studio/compare", json={"run_ids": [small["run"]["run_id"], big["run"]["run_id"]]})
    t0 = c2["oos"]["tests"][0]
    assert c2["oos"]["benchmark_run_id"] == small["run"]["run_id"] and t0["nested"] is True and "cw_p" in t0
    assert len(c2["oos"]["equity"]) == 2 and c2["oos"]["equity"][0]["kind"] == "sign_of_prediction"
    li = call(env, "POST", "/api/v1/studio/lint", json={"study": look})
    assert li["ok"] is False and li["issues"][0]["code"] == "lookahead_regressor"
    sug = call(env, "POST", "/api/v1/studio/guide/suggest", json={
        "goal": "predict", "outcome": {"source": "macro", "field": "fred:DGS2"}, "outcome_kind": "change",
        "driver": {"source": "macro", "field": "fred:DGS10"}, "universe": None, "horizon": 1, "start": "2019-01-01"})
    assert sug["method"] == "regularized" and sug["study"]["params"]["cv"]["purge"] == 1 and sug["rationale"]
    call(env, "POST", "/api/v1/studio/guide/suggest", json={"goal": "nope", "outcome": {"source": "prices", "field": "ret"}}, status=422)
    pr = call(env, "POST", "/api/v1/studio/runs/{run_id}/promote", {"run_id": big["run"]["run_id"]},
              json={"name": "rates ridge", "monitor": "weekly", "owner": "qa"}, status=201)
    assert pr["version"] == 1 and pr["alert_id"] and pr["spec_hash"] and pr["baseline"]["features"]
    pr2 = call(env, "POST", "/api/v1/studio/runs/{run_id}/promote", {"run_id": big["run"]["run_id"]}, json={"name": "rates ridge"}, status=201)
    assert pr2["version"] == 2 and pr2["alert_id"] is None
    assert {m["model_id"] for m in call(env, "GET", "/api/v1/studio/registry")} >= {pr["model_id"], pr2["model_id"]}
    det = call(env, "GET", "/api/v1/studio/registry/{model_id}", {"model_id": pr["model_id"]})
    assert len(det["versions"]) == 2
    mon = call(env, "POST", "/api/v1/studio/registry/{model_id}/monitor", {"model_id": pr["model_id"]}, json={})
    assert mon["feature_psi"] and mon["metric"]["kind"] == "r2_oos" and mon["expanding"]
    up_ = call(env, "PATCH", "/api/v1/studio/registry/{model_id}", {"model_id": pr["model_id"]}, json={"stage": "production", "monitor": "none"})
    assert up_["stage"] == "production" and up_["alert_id"] is None
    call(env, "DELETE", "/api/v1/studio/registry/{model_id}", {"model_id": pr2["model_id"]}, status=204)
    call(env, "GET", "/api/v1/studio/registry/{model_id}", {"model_id": pr2["model_id"]}, status=404)
    call(env, "POST", "/api/v1/studio/runs/{run_id}/promote", {"run_id": "SRNOPE"}, json={}, status=404)
    st = call(env, "POST", "/api/v1/studio/studies", json={"name": "Rates study", "study": study, "tags": ["rates"]},
              status=201)
    sid_ = st["study_id"]
    assert any(x["study_id"] == sid_ for x in call(env, "GET", "/api/v1/studio/studies"))
    up = call(env, "PATCH", "/api/v1/studio/studies/{study_id}", {"study_id": sid_}, json={"name": "Renamed", "schedule": "weekly"})
    assert up["name"] == "Renamed" and up["schedule"] == "weekly" and up["alert_id"]
    al = next(a for a in call(env, "GET", "/api/v1/alerts") if a["alert_id"] == up["alert_id"])
    assert al["params"]["drift_threshold"] == 0.2 and al["params"]["schedule"] == "weekly"
    off = call(env, "PATCH", "/api/v1/studio/studies/{study_id}", {"study_id": sid_}, json={"schedule": "none"})
    assert off["alert_id"] is None and not any(a["alert_id"] == up["alert_id"] for a in call(env, "GET", "/api/v1/alerts"))
    r3 = call(env, "POST", "/api/v1/studio/runs", json={"study": study, "study_id": sid_})
    det = call(env, "GET", "/api/v1/studio/studies/{study_id}", {"study_id": sid_})
    assert det["study"]["last_run_id"] == r3["run"]["run_id"] and det["runs"][0]["run_id"] == r3["run"]["run_id"]
    call(env, "DELETE", "/api/v1/studio/studies/{study_id}", {"study_id": sid_}, status=204)
    call(env, "GET", "/api/v1/studio/studies/{study_id}", {"study_id": sid_}, status=404)
    call(env, "GET", "/api/v1/studio/runs/{run_id}", {"run_id": "SRNOPE"}, status=404)


def test_macro_v2_wave1(env):
    """macro-w1: heatmap / cycle / countries / themes / series / compare (fixture: tests/macro_v2/synthetic.py)."""
    hm = call(env, "GET", "/api/v1/macro/v2/heatmap")
    assert hm["data_status"] == "ok" and hm["cells"] and len(hm["indicators"]) >= 20
    us = {c["indicator"]: c for c in hm["cells"] if c["country"] == "US"}
    assert {"cpi_yoy", "yield_10y", "yield_2y", "curve_2s10s", "industrial_production"} <= set(us)
    assert us["cpi_yoy"]["official"] and us["cpi_yoy"]["source_url"].startswith("https://fred.stlouisfed.org/series/")
    assert us["cpi_yoy"]["units"] == "% y/y" and us["curve_2s10s"]["inputs"] == ["fred:DGS10", "fred:DGS2"]
    assert us["employment"]["has_vintages"] and us["employment"]["first_release_value"] is not None
    assert any(c["country"] == "DE" and c["indicator"] == "policy_rate" and c["title"].endswith("[euro area]") for c in hm["cells"])
    hm2 = call(env, "GET", "/api/v1/macro/v2/heatmap", params={"countries": "US,EA", "indicators": ["cpi_yoy"]})
    assert {c["country"] for c in hm2["cells"]} <= {"US", "EA"} and {c["indicator"] for c in hm2["cells"]} == {"cpi_yoy"}
    old = call(env, "GET", "/api/v1/macro/v2/heatmap", params={"as_of": hm["available_snapshots"][-1]})
    assert old["snapshot_date"] == hm["available_snapshots"][-1] and old["snapshot_date"] < hm["snapshot_date"]
    call(env, "GET", "/api/v1/macro/v2/heatmap", params={"countries": "XX"}, status=422)
    cy = call(env, "GET", "/api/v1/macro/v2/cycle", params={"countries": "US", "trail": 6})
    assert cy["countries"][0]["quadrant"] in ("recovery", "expansion", "slowdown", "contraction")
    assert len(cy["countries"][0]["trail"]) == 6 and "120-month" in cy["method"]
    cl = call(env, "GET", "/api/v1/macro/v2/countries")
    assert len(cl["countries"]) == 24 and next(c for c in cl["countries"] if c["country"] == "US")["n_cells"] >= 8
    page = call(env, "GET", "/api/v1/macro/v2/countries/{cc}", {"cc": "US"}, params={"years": 5})
    secs = {s["key"]: s for s in page["sections"]}
    assert secs["inflation"]["series"][0]["is_primary"] and secs["inflation"]["series"][0]["spark"]
    assert page["equity_vs_macro"]["equity_index"] == "IDX:^GSPC" and page["equity_vs_macro"]["points"]
    call(env, "GET", "/api/v1/macro/v2/countries/{cc}", {"cc": "ZZ"}, status=404)
    for th in ("inflation", "labor", "rates", "credit_conditions", "liquidity", "commodities_fx", "growth"):
        body = call(env, "GET", "/api/v1/macro/v2/themes/{theme}", {"theme": th}, params={"years": 3})
        assert body["panels"] and body["theme"] == th
    rates = call(env, "GET", "/api/v1/macro/v2/themes/{theme}", {"theme": "rates"}, params={"years": 2})
    ust = next(c for p in rates["panels"] if p["kind"] == "curve" for c in p["curves"] if c["curve_id"] == "UST")
    assert len(ust["matrix"]) == len(ust["dates"]) and len(ust["matrix"][0]) == len(ust["tenors"])
    liq = call(env, "GET", "/api/v1/macro/v2/themes/{theme}", {"theme": "liquidity"})
    assert next(p for p in liq["panels"] if p["kind"] == "derived")["series"][0]["available"]
    call(env, "GET", "/api/v1/macro/v2/themes/{theme}", {"theme": "nope"}, status=422)
    s = call(env, "GET", "/api/v1/macro/v2/series/{series_id}", {"series_id": "fred:PAYEMS"}, params={"transform": "yoy"})
    assert s["points"] and s["vintages"]["kind"] == "alfred" and s["vintages"]["revision_stats"]["all"]["native"]["n"] > 0
    assert s["provenance"]["official"] and s["provenance"]["source_page"].endswith("/PAYEMS") and s["releases"]
    for tf in ("level", "mom", "z", "canonical"):
        call(env, "GET", "/api/v1/macro/v2/series/{series_id}", {"series_id": "fred:CPIAUCSL"}, params={"transform": tf})
    call(env, "GET", "/api/v1/macro/v2/series/{series_id}", {"series_id": "fred:NOPE"}, status=404)
    cmp_ = call(env, "GET", "/api/v1/macro/v2/compare", params={"indicator": "cpi_yoy", "countries": ["US", "EA"]})
    assert [c["country"] for c in cmp_["countries"]] == ["US", "EA"] and cmp_["countries"][0]["points"]
    call(env, "GET", "/api/v1/macro/v2/compare", params={"indicator": "nope"}, status=422)


def test_macro_v2_wave2(env):
    """macro-w2: calendar / release detail / surprise / nowcast / recession (fixture: tests/macro_v2/synthetic_w2.py)."""
    from datetime import date, timedelta

    cal = call(env, "GET", "/api/v1/macro/v2/calendar",
               params={"from": (date.today() - timedelta(days=330)).isoformat(), "to": (date.today() + timedelta(days=60)).isoformat()})
    assert cal["data_status"] == "ok" and cal["expectation_label"] == "Model-based expectation (not consensus)"
    assert {i["kind"] for i in cal["items"]} == {"data", "policy_meeting"}
    assert cal["sources"] and [t["tier"] for t in cal["importance_tiers"]] == [1, 2, 3]
    assert all(s["actual"] is None for i in cal["items"] if i["status"] == "upcoming" for s in i["series"])
    up = [i for i in cal["items"] if i["status"] == "upcoming" and i["series"]]
    assert up and up[0]["series"][0]["expectation"]["model_based"] is True
    rel = [i for i in cal["items"] if i["status"] == "released" and any(s["surprise_z"] is not None for s in i["series"])]
    assert rel
    call(env, "GET", "/api/v1/macro/v2/calendar", params={"countries": "US", "importance": 1, "kind": "policy_meeting"})
    call(env, "GET", "/api/v1/macro/v2/calendar", params={"from": "2026-01-10", "to": "2025-01-01"}, status=422)
    det = call(env, "GET", "/api/v1/macro/v2/releases/{release_event_id}", {"release_event_id": rel[-1]["release_event_id"]})
    s0 = next(s for s in det["series"] if s["surprise_z"] is not None)
    assert s0["history"] and s0["expectations"] and all(x["model_based"] for x in s0["expectations"])
    assert any(h["signed_z"] is not None for h in s0["history"])
    assert {x["model"] for x in s0["expectations"]} >= {"sarima"}
    call(env, "GET", "/api/v1/macro/v2/releases/{release_event_id}", {"release_event_id": "RE_NOPE"}, status=404)
    sp = call(env, "GET", "/api/v1/macro/v2/surprise")
    assert sp["model_based"] and {x["country"] for x in sp["latest"]} >= {"US", "GLOBAL"} and sp["series"][0]["points"]
    call(env, "GET", "/api/v1/macro/v2/surprise", params={"countries": "US", "theme": "labor", "freq": "D"})
    nc = call(env, "GET", "/api/v1/macro/v2/nowcast/{cc}", {"cc": "US"})
    t0 = nc["targets"][0]
    ours = next(m for m in t0["models"] if m["model_id"] == "dfm_mq_us_v1")
    assert ours["model_based"] and not ours["official"] and ours["news"] and len(t0["models"]) == 3
    assert next(m for m in t0["models"] if m["model_id"] == "gdpnow_atlanta_fed")["official"]
    call(env, "GET", "/api/v1/macro/v2/nowcast/{cc}", {"cc": "ZZ"}, status=404)
    rc = call(env, "GET", "/api/v1/macro/v2/recession", params={"history_months": 12})
    us = next(c for c in rc["countries"] if c["country"] == "US")
    assert {r["method"] for r in us["rows"]} == {"nyfed_yield_curve_official", "yield_curve_probit", "sahm_realtime",
                                                  "hmm_regime", "oecd_cli_turning_point"}
    assert all(r["method_label"] for r in us["rows"]) and len(rc["methods"]) == 5


def test_macro_v4(env):
    """macro v2 W4 (macro-w4): data from the real W4 pipeline run on the synthetic lake (tests/macro_v4/synthetic_w4)."""
    f = call(env, "GET", "/api/v1/macro/v4/factors", params={"history": 4})
    assert f["data_status"] == "ok"
    e = call(env, "GET", "/api/v1/macro/v4/exposures", params={"entity_type": "sector", "weighting": "ew"})
    assert e["items"]
    call(env, "GET", "/api/v1/macro/v4/regime-performance", params={"weighting": "ew"})
    call(env, "GET", "/api/v1/macro/v4/regime-scopes")
    lst = call(env, "GET", "/api/v1/macro/v4/scenarios")
    assert lst["presets"]
    sc = call(env, "POST", "/api/v1/macro/v4/scenarios", json={"name": "t", "shocks": {"oil_pct": 20}}, status=201)
    call(env, "GET", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sc["scenario_id"]})
    call(env, "PUT", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sc["scenario_id"]}, json={"name": "t2"})
    r = call(env, "POST", "/api/v1/macro/v4/scenarios/{scenario_id}/run", {"scenario_id": sc["scenario_id"]},
             json={"weighting": "ew", "top_n": 3})
    assert r["data_status"] == "ok" and r["sectors"]
    call(env, "POST", "/api/v1/macro/v4/scenarios/run", json={"scenario_id": "preset_rates_up_100"})
    call(env, "DELETE", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sc["scenario_id"]}, status=204)
    eps = call(env, "GET", "/api/v1/macro/v4/episodes")
    call(env, "GET", "/api/v1/macro/v4/episodes/{episode_id}", {"episode_id": eps["episodes"][-1]["episode_id"]})
    sid = env["info"]["securities"][0]["security_id"]
    m = call(env, "GET", "/api/v1/companies/{security_id}/macro", {"security_id": sid})
    assert m["betas"] and m["macro_v4"]["data_status"] == "ok" and m["macro_v4"]["exposures"]["joint"]


def test_macro_v3(env):
    """macro v3 W3 (macro-w3): central-bank monitor (fixture: tests/macro_v3/synthetic_w3.py)."""
    ov = call(env, "GET", "/api/v1/macro/v3/central-banks")
    assert ov["data_status"] == "ok" and len(ov["banks"]) == 17
    fed = next(b for b in ov["banks"] if b["bank"] == "fed")
    assert fed["policy_rate"] is not None and fed["phase"] in ("hiking", "pausing", "cutting")
    assert fed["priced"]["label"].startswith("Approximation") and fed["priced"]["next_meeting"] is not None
    assert fed["stance"]["labels"]["taylor"].startswith("Model-based") and fed["tone"]["label"].startswith("Model-based")
    assert 0 < len(fed["tone_trend"]) <= 12 and fed["tone_trend"][-1]["date"] == fed["tone"]["date"]
    ip = call(env, "GET", "/api/v1/macro/v3/implied-paths", params={"banks": "fed,ecb,bcb"})
    assert [b["bank"] for b in ip["banks"]] == ["fed", "ecb", "bcb"]
    assert ip["banks"][0]["points"] and ip["banks"][0]["tp_adjusted"] and ip["banks"][2]["status"] == "not_available"
    d = call(env, "GET", "/api/v1/macro/v3/central-banks/{bank}", {"bank": "fed"}, params={"history_years": 3})
    assert d["rates"]["history"] and d["decisions"] and d["meetings"]["upcoming"] and d["rate_cycle"]["segments"]
    assert d["implied_path"]["points"] and d["implied_path"]["history"] and d["implied_path"]["curves"]
    assert d["projections"]["available"] and d["projections"]["vs_market"]
    assert d["stance"]["latest"] and d["balance_sheet"]["available"] and d["tone"]["history"]
    b = call(env, "GET", "/api/v1/macro/v3/central-banks/{bank}", {"bank": "boe"})
    assert any(x["vote"] for x in b["decisions"])
    call(env, "GET", "/api/v1/macro/v3/central-banks/{bank}", {"bank": "rbi"})
    call(env, "GET", "/api/v1/macro/v3/central-banks/{bank}", {"bank": "nope"}, status=404)


def test_live(env):
    """Real-time R1 (realtime agent): live overlay endpoints + SSE + status against a small hot store."""
    from datetime import UTC, datetime

    from app.api.routers import live as live_router
    from app.realtime.bus import SqliteBus, default_bus_path

    bus = SqliteBus(default_bus_path(env["settings"]))
    now = datetime.now(UTC)
    bus.publish("filings", "sec_edgar:0000000000-26-000001", {
        "filing_id": "sec_edgar:0000000000-26-000001", "source_id": "sec_edgar", "source_entity_id": "0000320193",
        "company": "Apple Inc.", "form_type": "8-K", "form_class": "current_report", "items": ["2.02"],
        "url": "https://www.sec.gov/x", "knowledge_ts": now.isoformat(), "filed_date": str(now.date()),
        "first_seen_at": now.isoformat(), "issuer_ids": []}, type="filing", ts_source=now)
    bus.publish("news", "n1", {"item_id": "n1", "source_id": "rss_fed", "category": "central_bank",
                               "publisher": "Federal Reserve", "title": "Fed statement", "url": "https://fed/x",
                               "published_at": now.isoformat(), "countries": ["US"], "themes": ["monetary_policy"]},
                type="central_bank", ts_source=now)
    bus.publish("events", "gdelt:a:1", {"kind": "article", "title": "Strike at port", "url": "https://x",
                                        "themes": ["labor"], "countries": ["US"], "issuer_ids": [],
                                        "published_at": now.isoformat(), "slot": "20261012120000"},
                type="article", ts_source=now)
    bus.publish("macro", "RE1|fred:CPIAUCSL", {"release_event_id": "RE1", "series_id": "fred:CPIAUCSL",
                                               "country": "US", "actual": 0.4, "expectation": 0.2, "surprise_z": 2.0,
                                               "scheduled_at": now.isoformat(), "captured_at": now.isoformat(),
                                               "importance": 1, "period": "2026-09-01"}, type="release", ts_source=now)
    bus.publish("alerts", "A1|S1|2026-10-12", {"event_id": "e1", "alert_id": "A1", "security_id": "S1",
                                               "rule_type": "new_filing", "as_of_date": "2026-10-12",
                                               "fired_at": now.isoformat(), "message": "new filing: 8-K - live",
                                               "payload": {"rule_type": "new_filing", "live": True}},
                type="new_filing", ts_source=now)
    live_router._bus = None
    f = call(env, "GET", "/api/v1/live/filings", params={"limit": 20})
    assert f["n_live"] == 1 and f["items"][0]["origin"] == "live" and f["items"][0]["items"] == ["2.02"]
    assert call(env, "GET", "/api/v1/live/filings", params={"source_id": "fca_nsm"})["n_live"] == 0
    n = call(env, "GET", "/api/v1/live/news", params={"category": "central_bank"})
    assert n["items"][0]["item_id"] == "n1"
    call(env, "GET", "/api/v1/live/news")
    e = call(env, "GET", "/api/v1/live/events", params={"theme": "labor"})
    assert e["items"][0]["kind"] == "article"
    m = call(env, "GET", "/api/v1/live/macro")
    assert m["items"][0]["surprise_z"] == 2.0 and m["items"][0]["model_based"] is True
    a = call(env, "GET", "/api/v1/live/alerts")
    assert any(x["event_id"] == "e1" and x["live"] for x in a["items"])
    st = call(env, "GET", "/api/v1/live/status")
    assert st["running"] is False and st["head_id"] == bus.head_id()
    with env["client"].stream("GET", "/api/v1/live/stream", params={"topics": "filings,macro", "last_event_id": 0,
                                                                     "max_events": 2}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: hello" in body and "event: filings" in body and "event: macro" in body and "event: news" not in body
    CALLED.add(("GET", "/api/v1/live/stream"))
    # workstream B (v1.40.0): delayed quotes + live feeds health
    import time as _time

    from app.api import live_quotes as LQ
    from app.realtime.quotes.store import QuoteStore, read_interest

    LQ.reset()
    q0 = call(env, "GET", "/api/v1/live/quotes", params={"set": "header"})
    assert q0["data_status"] == "pending" and q0["items"] == [] and q0["delayed"] is True and q0["service"]["state"]
    qs = QuoteStore(settings=env["settings"])
    qs.upsert([{"security_id": "IDX:^GSPC", "symbol": "^GSPC", "source": "yahoo", "price": 7722.6, "change": 45.8,
                "change_pct": 0.006, "prev_close": 7676.8, "currency": "USD", "market": "XNYS", "market_state": "REGULAR",
                "quote_ts": _time.time() - 60, "fetched_at": _time.time(), "delay_min": 0}])
    qs.set_markets([{"market": "XNYS", "open": True, "reason": "open", "opens_at": None, "closes_at": None}])
    qs.close()
    LQ.reset()
    q1 = call(env, "GET", "/api/v1/live/quotes", params={"ids": "^GSPC,NOPE"})
    assert q1["data_status"] == "ok" and q1["items"][0]["security_id"] == "IDX:^GSPC" and q1["items"][0]["market_open"]
    assert q1["missing"] == ["NOPE"] and "IDX:^GSPC" in read_interest(env["settings"])
    fd = call(env, "GET", "/api/v1/live/feeds")
    assert any(f["group"] == "quotes" for f in fd["feeds"]) and fd["quotes"]["running"] is False
    assert call(env, "GET", "/api/v1/live/status")["quotes"]["delayed"] is True
    LQ.reset()
    bus.close()
    live_router._bus = None


def test_news(env):
    """roadmap v3 N4 News Terminal (news-live): every /news operation answers its contract shape on the fixture snapshot
    (no intraday file: pending lists; the bus may hold the test_live messages)."""
    from app.api.intraday import manager

    manager.reset()
    t = call(env, "GET", "/api/v1/news/terminal", params={"limit": 50, "tier": "1,2,3,4"})
    assert t["data_status"] in ("ok", "pending") and isinstance(t["items"], list)
    st = call(env, "GET", "/api/v1/news/stories", params={"hours": 168, "sort": "recent"})
    assert isinstance(st["items"], list)
    sid = st["items"][0]["story_id"] if st["items"] else "1"
    call(env, "GET", "/api/v1/news/stories/{story_id}", {"story_id": sid}, status=200 if st["items"] else 404)
    call(env, "GET", "/api/v1/news/stories/{story_id}", {"story_id": "x"}, status=422)
    h = call(env, "GET", "/api/v1/news/hourly", params={"label_type": "country", "label_id": "US"})
    assert h["model_based"] is True
    f = call(env, "GET", "/api/v1/news/feeds")
    assert f["n_ok"] >= 40
    lh = call(env, "GET", "/api/v1/news/live/health")
    assert isinstance(lh["sources"], list) and lh["enabled"] in (True, False)


def test_positioning(env):
    lst = call(env, "GET", "/api/v1/positioning/markets")
    codes = {x["market_code"]: x for x in lst["items"]}
    assert set(codes) == {"13874A", "240743", "067651"} and lst["source"].startswith("CFTC")
    assert codes["13874A"]["report_type"] == "tff_fut" and codes["13874A"]["category"] == "lev_money"
    assert codes["067651"]["report_type"] == "disagg_fut" and codes["067651"]["category"] == "m_money"
    assert codes["067651"]["security_id"] == "IDX:CL=F" and lst["report_date"] and lst["release_date"] > lst["report_date"]
    assert {a["id"] for a in lst["asset_classes"]} == {"equity", "energy"}
    leg = call(env, "GET", "/api/v1/positioning/markets", params={"report": "legacy", "asset_class": "equity", "combined": "true"})
    assert {x["report_type"] for x in leg["items"]} == {"legacy_comb"} and len(leg["items"]) == 2
    assert [x["market_code"] for x in call(env, "GET", "/api/v1/positioning/markets", params={"security_id": "IDX:QM=F"})["items"]] == ["067651"]
    call(env, "GET", "/api/v1/positioning/markets", params={"asset_class": "bonds"}, status=422)
    hm = call(env, "GET", "/api/v1/positioning/heatmap", params={"report": "tff"})
    assert [c["id"] for c in hm["categories"]][:3] == ["lev_money", "asset_mgr", "dealer"] and len(hm["rows"]) == 2
    assert all(len(r["values"]) == len(hm["categories"]) for r in hm["rows"])
    assert all(v is None or 0 <= v <= 1 for r in hm["rows"] for v in r["values"])
    cr = call(env, "GET", "/api/v1/positioning/crowded")
    assert cr["model_based"] is True and cr["n_markets"] == 3
    assert all(x["extreme"] == "long" for x in cr["longs"]) and all(x["extreme"] == "short" for x in cr["shorts"])
    d = call(env, "GET", "/api/v1/positioning/markets/{market_code}", {"market_code": "240743"})
    assert d["report"] == "tff" and d["headline_category"] == "lev_money" and len(d["dates"]) == 220
    assert all(len(s["net"]) == len(d["dates"]) for s in d["series"]) and len(d["open_interest"]) == 220
    assert d["price_instrument"]["security_id"] == "IDX:^N225" and d["price_instrument"]["is_proxy"] is True
    assert sum(p is not None for p in d["price"]) > 150
    assert d["latest"] and {x["category"] for x in d["latest"]} >= {"lev_money", "spec_hedger"}
    d2 = call(env, "GET", "/api/v1/positioning/markets/{market_code}", {"market_code": "240743"},
              params={"report": "legacy", "combined": "true", "from": str(date.fromisoformat(d["dates"][-1]) - timedelta(days=365))})
    assert d2["report_type"] == "legacy_comb" and 50 <= len(d2["dates"]) <= 54
    call(env, "GET", "/api/v1/positioning/markets/{market_code}", {"market_code": "240743"}, params={"report": "disagg"}, status=404)
    call(env, "GET", "/api/v1/positioning/markets/{market_code}", {"market_code": "NOPE"}, status=404)


def test_earnings(env, ids):
    as_of = env["report"].as_of
    secs = env["info"]["securities"][:40]
    cal = call(env, "GET", "/api/v1/earnings/calendar", params={"from": str(as_of - timedelta(days=375)), "to": str(as_of + timedelta(days=20))})
    assert cal["data_status"] == "ok" and cal["total"] > 0
    # v1.30 / F30: paging + compact items (null fields dropped, SecurityRef required fields kept)
    pg = call(env, "GET", "/api/v1/earnings/calendar", params={"from": str(as_of - timedelta(days=375)), "to": str(as_of + timedelta(days=20)),
                                                               "limit": 3, "offset": 2, "compact": True})
    assert pg["offset"] == 2 and len(pg["items"]) <= 3 and pg["total"] == cal["total"] and pg["days"] == cal["days"]
    assert pg["next_offset"] == (5 if cal["total"] > 5 else None) and pg["truncated"] is (cal["total"] > 5)
    assert all(None not in x.values() and x["security"]["security_id"] for x in pg["items"])
    assert [x["date"] for x in pg["items"]] == [x["date"] for x in cal["items"][2:5]]
    kinds = {(x["kind"], x["status"]) for x in cal["items"]}
    assert ("earnings", "upcoming") in kinds and ("earnings", "released") in kinds and ("corporate", "past") in kinds
    up = [x for x in cal["items"] if x["kind"] == "earnings" and x["status"] == "upcoming"]
    assert {x["date_source"] for x in up} == {"model_projection", "provider_estimate", "company_announced"}
    assert all(x["source_label"] for x in up)
    assert any(x["eps_expected"] is not None for x in up)
    only = call(env, "GET", "/api/v1/earnings/calendar", params={"from": str(as_of), "to": str(as_of + timedelta(days=20)),
                                                                 "types": "earnings", "date_source": "model_projection"})
    assert only["items"] and all(x["date_source"] == "model_projection" and x["window_lo"] for x in only["items"])
    one = call(env, "GET", "/api/v1/earnings/calendar", params={"from": str(as_of), "to": str(as_of + timedelta(days=20)),
                                                                "security_ids": secs[0]["security_id"]})
    assert {x["security"]["security_id"] for x in one["items"]} == {secs[0]["security_id"]}
    reg = secs[0]["region"]
    assert all(x["security"]["region"] == reg for x in call(env, "GET", "/api/v1/earnings/calendar", params={"region": reg, "from": str(as_of), "to": str(as_of + timedelta(days=20))})["items"])
    call(env, "GET", "/api/v1/earnings/calendar", params={"types": "lunch"}, status=422)
    call(env, "GET", "/api/v1/earnings/calendar", params={"from": "2020-01-01", "to": "2026-01-01"}, status=422)
    co = call(env, "GET", "/api/v1/companies/{security_id}/earnings", {"security_id": secs[1]["security_id"]})
    assert co["next"] and co["next"][0]["date_source"] and co["history"] and len(co["history"]) == 8
    assert co["reaction_stats"]["n_reactions"] == 8 and co["reaction_stats"]["mean_abs_car_m1_p1"] == pytest.approx(0.02, abs=1e-6)
    h = co["history"][0]
    assert h["eps_expected"] is not None and h["eps_actual"] is not None and h["car_m1_p1"] is not None
    assert {e["model"] for e in co["expectations"]} >= {"sarima", "seasonal_naive"}
    assert any(e["upcoming"] for e in co["corporate_events"])
    assert "not consensus" in co["labels"]["expectation"]
    call(env, "GET", "/api/v1/companies/{security_id}/earnings", {"security_id": "SNOPE"}, status=404)
    se = call(env, "GET", "/api/v1/earnings/season")
    assert se["seasons"] and se["total"]["n_reported"] > 0 and se["by_region"] and se["history"]
    se2 = call(env, "GET", "/api/v1/earnings/season", params={"season": se["seasons"][-1], "region": reg})
    assert se2["region"] == reg
    call(env, "GET", "/api/v1/earnings/season", params={"season": "1999Q1"}, status=404)
    # alert rule types validate their params
    a = call(env, "POST", "/api/v1/alerts", json={"security_id": secs[0]["security_id"], "rule_type": "earnings_upcoming",
                                                 "params": {"days_ahead": 5}, "cooldown_days": 1}, status=201)
    assert a["params"]["days_ahead"] == 5
    call(env, "POST", "/api/v1/alerts", json={"security_id": secs[0]["security_id"], "rule_type": "earnings_surprise",
                                              "params": {"items": ["ebitda"]}}, status=422)
    call(env, "DELETE", "/api/v1/alerts/{alert_id}", {"alert_id": a["alert_id"]}, status=204)


def test_cross_asset(env):
    ck = call(env, "GET", "/api/v1/cross-asset/cockpit")
    rows = {r["asset_id"]: r for r in ck["rows"]}
    assert set(rows) == {"CL", "GC", "TTF", "SPX", "EURUSD", "USDJPY", "UST10Y", "US_HY", "BTC"} and ck["data_status"] == "ok"
    assert [s["id"] for s in ck["signals"]][:2] == ["trend", "momentum"] and all(len(r["cells"]) == len(ck["signals"]) for r in ck["rows"])
    assert ck["signals"][0]["scale"] == "score" and ck["signals"][0]["model_based"] is True
    # CFTC join: CL <- 067651 (disagg managed money), SPX <- 13874A (TFF leveraged funds); PIT on the release date
    cl, spx = rows["CL"]["positioning"], rows["SPX"]["positioning"]
    assert cl["market_code"] == "067651" and cl["report_type"] == "disagg_fut" and cl["category"] == "m_money"
    assert spx["market_code"] == "13874A" and spx["category"] == "lev_money" and spx["release_date"] <= rows["SPX"]["date"]
    assert rows["TTF"]["positioning"] is None and rows["EURUSD"]["positioning"] is None
    assert ck["positioning"]["n_assets"] == 2 and ck["positioning"]["source"].startswith("CFTC")
    assert rows["CL"]["carry_method"] == "futures_roll" and rows["CL"]["carry_label"] and rows["EURUSD"]["instrument_id"] == "IDX:EURUSD=X"
    assert "Yahoo" in ck["labels"]["curve_source"] and "APPROXIMATE" in ck["labels"]["curve_source"]
    fx = call(env, "GET", "/api/v1/cross-asset/cockpit", params={"asset_class": "fx"})
    assert {r["asset_id"] for r in fx["rows"]} == {"EURUSD", "USDJPY"} and {a["id"] for a in fx["asset_classes"]} >= {"fx", "commodity"}
    assert [r["asset_id"] for r in call(env, "GET", "/api/v1/cross-asset/cockpit", params={"security_id": "IDX:CL=F"})["rows"]] == ["CL"]
    call(env, "GET", "/api/v1/cross-asset/cockpit", params={"asset_class": "bonds"}, status=422)

    d = call(env, "GET", "/api/v1/cross-asset/assets/{asset_id}", {"asset_id": "CL"})
    assert d["asset"]["futures_root"] == "CL" and d["has_curve"] is True and 1 < len(d["dates"]) <= 600
    assert all(len(v) == len(d["dates"]) for v in d["series"].values()) and len(d["curve_shape"]) == len(d["dates"])
    assert d["latest"]["date"] == d["dates"][-1] and d["positioning"]["market_code"] == "067651"
    assert sum(v is not None for v in d["series"]["cot_index_3y"]) > 100
    d2 = call(env, "GET", "/api/v1/cross-asset/assets/{asset_id}", {"asset_id": "UST10Y"},
              params={"from": str(date.fromisoformat(d["dates"][-1]) - timedelta(days=90))})
    assert d2["has_curve"] is False and 50 <= len(d2["dates"]) <= 70 and d2["carry_method"] == "bond_carry_rolldown"
    call(env, "GET", "/api/v1/cross-asset/assets/{asset_id}", {"asset_id": "NOPE"}, status=404)

    cl_ = call(env, "GET", "/api/v1/cross-asset/curves")
    items = {x["root"]: x for x in cl_["items"]}
    assert items["CL"]["n_contracts_last"] >= 6 and items["GC"]["coverage"]["missing"] == 1 and items["ES"]["last_date"] is None
    cv = call(env, "GET", "/api/v1/cross-asset/curves/{root}", {"root": "cl"})
    snaps = {s["label"]: s for s in cv["snapshots"]}
    assert cv["root"] == "CL" and snaps["latest"]["n_contracts"] >= 6 and snaps["1m"]["date"] < snaps["latest"]["date"]
    assert snaps["1y"]["date"] is not None and snaps["1y"]["date"] <= snaps["1m"]["date"]
    pts = snaps["latest"]["points"]
    assert [p["days_to_expiry"] for p in pts] == sorted(p["days_to_expiry"] for p in pts)
    assert all(len(v) == len(cv["dates"]) for v in cv["series"].values()) and len(cv["dates"]) > 300
    k = next(i for i, s in enumerate(cv["series"]["curve_slope"]) if s is not None)
    assert cv["series"]["curve_slope"][k] == pytest.approx(0.03, abs=2e-3) and cv["curve_shape"][k] == "contango"
    assert cv["series"]["roll_yield"][k] == pytest.approx(-0.03, abs=2e-3)
    assert {c["status"] for c in cv["coverage"]} >= {"served", "expired"} and cv["coverage_counts"]["served"] >= 6
    assert "short" in cv["labels"]["curve_history"]
    gc = call(env, "GET", "/api/v1/cross-asset/curves/{root}", {"root": "GC"})
    assert gc["coverage_counts"]["missing"] == 1
    es = call(env, "GET", "/api/v1/cross-asset/curves/{root}", {"root": "ES"})
    assert es["last_date"] is None and es["snapshots"][0]["points"] == []
    call(env, "GET", "/api/v1/cross-asset/curves/{root}", {"root": "XX"}, status=404)

    co = call(env, "GET", "/api/v1/cross-asset/correlation")
    assert co["window"] == 120 and co["n_assets"] == 12 and [m["snapshot"] for m in co["matrices"]] == ["latest", "1m", "1y"]
    m0 = co["matrices"][0]["values"]
    assert len(m0) == 12 and all(len(r) == 12 for r in m0) and all(m0[i][i] == pytest.approx(1.0) for i in range(12))
    assert m0[0][1] > 0.4 and len(co["series"]["eig1_share_60"]) == len(co["dates"]) == 600
    assert call(env, "GET", "/api/v1/cross-asset/correlation", params={"window": 60})["window"] == 60
    call(env, "GET", "/api/v1/cross-asset/correlation", params={"window": 90}, status=422)

    rk = call(env, "GET", "/api/v1/cross-asset/risk")
    assert rk["model_based"] is True and len(rk["dates"]) == 600 and len(rk["latest"]["components"]) == 8
    assert rk["latest"]["risk_regime"] in ("risk_on", "neutral", "risk_off") and rk["thresholds"]["risk_on"] == 0.5
    assert all(len(c["values"]) == 600 for c in rk["components"])
    rk2 = call(env, "GET", "/api/v1/cross-asset/risk", params={"from": rk["dates"][-30]})
    assert len(rk2["dates"]) == 30


def test_cross_asset_studio_source(env):
    cat = call(env, "GET", "/api/v1/studio/catalog/datasets")
    src = next(s for s in cat["items"] if s["source"] == "cross_asset")
    assert src["pit_column"] == "knowledge_date" and src["n_variables"] > 0
    page = call(env, "GET", "/api/v1/studio/catalog/variables", params={"source": "cross_asset", "q": "CL:cot"})
    assert any(v["field"] == "CL:cot_index_3y" for v in page["items"])
    ds = {"kind": "timeseries", "frequency": "W",
          "variables": [{"name": "px", "source": "cross_asset", "field": "CL:price", "transforms": [{"op": "log_diff"}]},
                        {"name": "cot", "source": "cross_asset", "field": "CL:cot_index_3y"}]}
    pv = call(env, "POST", "/api/v1/studio/datasets/preview", json={"dataset": ds, "n": 5})
    assert pv["n_rows"] > 50 and {c["column"] for c in pv["coverage"]} >= {"px", "cot"}
    call(env, "POST", "/api/v1/studio/datasets/preview", status=422,
         json={"dataset": {"kind": "timeseries", "frequency": "W",
                           "variables": [{"name": "x", "source": "cross_asset", "field": "CL:nope"}]}})


def test_backtest_endpoints(env):
    """Backtesting lab (roadmap #4): catalog, validate, from-screen, sync / async runs, holdings, trades, export,
    compare, strategies (CRUD, run, paper schedule -> backtest_drift alert) and the weekly scheduled job."""
    from types import SimpleNamespace

    cat = call(env, "GET", "/api/v1/backtest/catalog")
    assert {s["source"] for s in cat["sources"]} >= {"factor_scores", "ml", "pead", "cross_asset", "cftc", "price"}
    assert cat["survivorship_caveat"].startswith("Survivorship") and len(cat["presets"]) >= 8
    assert any(r["regime_type"] == "hmm_regime" for r in cat["regimes"]) or cat["regimes"] == []
    spec = {"name": "Value LS", "universe": {"universe_id": "mvp_us"},
            "signal": {"variables": [{"name": "val", "source": "factor_scores", "field": "value"}]},
            "construction": {"side": "long_short", "quantiles": 3}, "robustness": {"permutations": 3}}
    v = call(env, "POST", "/api/v1/backtest/validate", json={"spec": spec})
    assert v["will_run"] in ("sync", "async") and v["spec_hash"]
    bad = call(env, "POST", "/api/v1/backtest/validate", status=422,
               json={"spec": {**spec, "signal": {"variables": [{"name": "x", "source": "nope", "field": "y"}]}}})
    assert bad["type"] == "urn:backtest:invalid_spec"
    la = call(env, "POST", "/api/v1/backtest/validate", status=422, json={"spec": {**spec, "signal": {"variables": [
        {"name": "x", "source": "factor_scores", "field": "value", "transforms": [{"op": "lead", "k": 1}]}]}}})
    assert "look-ahead" in la["errors"][0]["msg"]
    fs = call(env, "POST", "/api/v1/backtest/from-screen", json={
        "universe_id": "mvp_us", "filters": [{"field": "quality", "op": "gte", "value": 40},
                                             {"field": "fv_upside", "op": "gt", "value": 0}],
        "size_buckets": ["large"], "sort_by": "momentum", "sort_dir": "desc", "limit": 20})
    assert "fv_upside" in fs["dropped"] and fs["spec"]["construction"]["top_n"] == 20
    r1 = call(env, "POST", "/api/v1/backtest/runs", json={"spec": spec, "mode": "sync"})
    assert r1["run"]["status"] == "completed", r1["run"]
    res = r1["result"]
    assert res["metrics"]["full"]["n_days"] > 100 and "out_of_sample" in res["metrics"]
    assert res["robustness"]["permutation"]["n"] == 3 and res["robustness"]["dsr"]["n_trials"] == 1
    rid = r1["run"]["run_id"]
    got = call(env, "GET", "/api/v1/backtest/runs/{run_id}", {"run_id": rid})
    assert got["reproducibility"]["spec_hash"] == r1["run"]["spec_hash"] and got["reproducibility"]["plan_key"]
    # v1.30 / F30: series resolution on demand
    wk = call(env, "GET", "/api/v1/backtest/runs/{run_id}", {"run_id": rid}, params={"series": "weekly"})
    full_n, wk_n = len(got["result"]["series"]["dates"]), len(wk["result"]["series"]["dates"])
    assert 0 < wk_n < full_n and wk["result"]["series"]["dates"][-1] == got["result"]["series"]["dates"][-1]
    assert len(wk["result"]["series"]["nav"]) == wk_n and wk["result"]["metrics"] == got["result"]["metrics"]
    no = call(env, "GET", "/api/v1/backtest/runs/{run_id}", {"run_id": rid}, params={"series": "none"})
    assert no["result"]["series"]["dates"] == [] and no["result"]["series"]["nav"] == []
    h = call(env, "GET", "/api/v1/backtest/runs/{run_id}/holdings", {"run_id": rid})
    assert h["rows"] and h["date"] == h["dates"][-1]
    h0 = call(env, "GET", "/api/v1/backtest/runs/{run_id}/holdings", {"run_id": rid}, params={"date": h["dates"][0]})
    assert h0["date"] == h["dates"][0]
    t = call(env, "GET", "/api/v1/backtest/runs/{run_id}/trades", {"run_id": rid}, params={"limit": 10})
    assert t["kind"] == "rebalance" and len(t["rows"]) == 10 and t["total"] > 10
    r = env["client"].get(f"/api/v1/backtest/runs/{rid}/export.py")
    assert r.status_code == 200 and "def build_panel(" in r.text and "RESULT_JSON" in r.text
    CALLED.add(("GET", "/api/v1/backtest/runs/{run_id}/export.py"))
    ev = call(env, "POST", "/api/v1/backtest/runs", json={"mode": "sync", "spec": {
        "name": "CFTC contrarian", "mode": "event", "universe": {"kind": "assets"},
        "signal": {"variables": [{"name": "c", "source": "cftc", "field": "cot_index_3y"}]},
        "event": {"trigger": "cftc_extreme", "params": {"high": 0.8, "low": 0.2}, "hold_days": 10, "allow_short": True},
        "robustness": {"permutations": 2}}})
    assert ev["run"]["status"] == "completed" and ev["result"]["events"]["n_trades"] >= 1
    tr = call(env, "GET", "/api/v1/backtest/runs/{run_id}/trades", {"run_id": ev["run"]["run_id"]})
    assert tr["kind"] == "round_trip"
    q = call(env, "POST", "/api/v1/backtest/runs", json={"spec": spec, "mode": "async"}, status=202)
    assert q["run"]["status"] == "queued" and q["result"] is None
    c = call(env, "DELETE", "/api/v1/backtest/runs/{run_id}", {"run_id": q["run"]["run_id"]})
    assert c["status"] == "cancelled"
    lst = call(env, "GET", "/api/v1/backtest/runs", params={"limit": 50})
    assert {rid, q["run"]["run_id"]} <= {x["run_id"] for x in lst}
    cmp_ = call(env, "POST", "/api/v1/backtest/compare", json={"run_ids": [rid, ev["run"]["run_id"]]})
    assert len(cmp_["curves"]) == 2 and any(m["metric"] == "sharpe" for m in cmp_["metrics"])
    # strategies + multiple-testing counter + paper schedule
    st = call(env, "POST", "/api/v1/backtest/strategies", status=201,
              json={"name": "Value LS", "spec": spec, "tags": ["value"], "origin": {"kind": "screener"}})
    sid_ = st["strategy_id"]
    assert st["n_trials"] == 0 and st["schedule"] == "none"
    run_a = call(env, "POST", "/api/v1/backtest/strategies/{strategy_id}/run", {"strategy_id": sid_}, json={"mode": "sync"})
    assert run_a["run"]["strategy_id"] == sid_
    spec2 = {**spec, "construction": {"side": "long_short", "quantiles": 5},
             "robustness": {"permutations": 0, "sweep": {"construction.weighting": ["equal", "inverse_vol"]}}}
    run_b = call(env, "POST", "/api/v1/backtest/runs", json={"spec": spec2, "mode": "sync", "strategy_id": sid_})
    assert run_b["result"]["robustness"]["dsr"]["n_trials"] == 3  # 1 prior configuration + this 2-point sweep
    assert run_b["result"]["robustness"]["sweep"]["n"] == 2
    up = call(env, "PATCH", "/api/v1/backtest/strategies/{strategy_id}", {"strategy_id": sid_},
              json={"name": "Value LS v2", "schedule": "weekly", "drift_threshold": 0.25})
    assert up["schedule"] == "weekly" and up["alert_id"] and up["n_trials"] == 3 and up["n_oos_views"] == 2
    alerts = call(env, "GET", "/api/v1/alerts")
    assert any(a["alert_id"] == up["alert_id"] and a["rule_type"] == "backtest_drift" for a in alerts)
    det = call(env, "GET", "/api/v1/backtest/strategies/{strategy_id}", {"strategy_id": sid_})
    assert len(det["runs"]) == 2
    assert any(x["strategy_id"] == sid_ for x in call(env, "GET", "/api/v1/backtest/strategies"))
    from app.backtest import scheduled

    ctx = SimpleNamespace(settings=env["settings"], params={}, check_cancelled=lambda: None)
    jr = scheduled.run(ctx)
    assert jr.status == "success" and jr.entities_ok == 1, jr.message
    assert len(call(env, "GET", "/api/v1/backtest/strategies/{strategy_id}", {"strategy_id": sid_})["runs"]) == 3
    off = call(env, "PATCH", "/api/v1/backtest/strategies/{strategy_id}", {"strategy_id": sid_}, json={"schedule": "none"})
    assert off["alert_id"] is None
    call(env, "DELETE", "/api/v1/backtest/strategies/{strategy_id}", {"strategy_id": sid_}, status=204)
    call(env, "GET", "/api/v1/backtest/strategies/{strategy_id}", {"strategy_id": sid_}, status=404)
    call(env, "GET", "/api/v1/backtest/runs/{run_id}", {"run_id": "BRNOPE"}, status=404)


def test_smart_money(env):
    """Roadmap #7: overview lists, EU register, activism feed, sectors, company ownership (fixture: smart_money_fixture)."""
    ov = call(env, "GET", "/api/v1/smart-money/overview")
    assert ov["activist_count_60d"] >= len(ov["activist_feed"])  # v1.39.0: true count, the feed is capped
    assert ov["lag"] and ov["lag"]["period_end"] == "2026-06-30" and "filed by" in ov["lag"]["label"]
    assert ov["accumulation"] and ov["distribution"] and ov["most_shorted"] and ov["squeeze_watch"]
    acc = [r["flow_pct_out"] for r in ov["accumulation"] if r["flow_pct_out"] is not None]
    assert acc == sorted(acc, reverse=True)
    assert any(e["event_type"] == "switch_13g_to_13d" for e in ov["activist_feed"])
    assert ov["eu_register"]["n_positions"] > 0 and ov["eu_register"]["latest"]
    assert all(s["official"] for s in ov["sources"]) and "model-based" in ov["labels"]["model_based"].lower()
    assert ov["top_score"] and ov["top_score"][0]["score"] >= ov["bottom_score"][0]["score"]
    us = call(env, "GET", "/api/v1/smart-money/overview", params={"region": "UK", "limit": 5})
    assert all(r["security"]["region"] == "UK" for r in us["accumulation"] + us["most_shorted"])
    call(env, "GET", "/api/v1/smart-money/overview", params={"limit": 999}, status=422)
    eu = call(env, "GET", "/api/v1/smart-money/eu-shorts")
    assert eu["current"] and eu["total"] == len(eu["items"]) > 0 and len(eu["regulators"]) == 8
    assert {s["id"] for s in eu["skipped"]} == {"consob", "bafin", "dfsa", "fin_fsa"}
    assert all(i["position_pct"] >= 0.5 for i in eu["items"] if i["position_type"] == "individual")
    assert any(i["prev_pct"] is not None for i in eu["items"])
    h = eu["top_holders"][0]["holder_name"]
    byh = call(env, "GET", "/api/v1/smart-money/eu-shorts", params={"holder": h, "current": False, "regulator": "fca"})
    assert all(i["holder_name"] == h and i["regulator"] == "fca" for i in byh["items"])
    # guide 07: the holder filter ignores case (same holder filed in capitals)
    byu = call(env, "GET", "/api/v1/smart-money/eu-shorts", params={"holder": h.upper(), "current": False, "regulator": "fca"})
    assert byu["total"] == byh["total"]
    agg = call(env, "GET", "/api/v1/smart-money/eu-shorts", params={"position_type": "aggregate"})
    assert agg["items"] and all(i["holder_name"] == "(aggregate)" for i in agg["items"])
    call(env, "GET", "/api/v1/smart-money/eu-shorts", params={"regulator": "consob"}, status=422)
    ac = call(env, "GET", "/api/v1/smart-money/activism", params={"days": 400})
    types = {e["event_type"] for e in ac["items"]}
    assert "switch_13g_to_13d" in types and "new_13d" in types and "new_13g" not in types
    allt = call(env, "GET", "/api/v1/smart-money/activism",
                params={"days": 400, "event_types": "new_13g,amend_13g"})
    assert allt["items"] and {e["event_type"] for e in allt["items"]} <= {"new_13g", "amend_13g"}
    call(env, "GET", "/api/v1/smart-money/activism", params={"event_types": "nope"}, status=422)
    se = call(env, "GET", "/api/v1/smart-money/sectors")
    assert se["rows"] and all(r["region"] is None for r in se["rows"]) and "US" in se["regions"]
    assert sum(r["n_13f"] for r in se["rows"]) > 0
    se2 = call(env, "GET", "/api/v1/smart-money/sectors", params={"region": "US"})
    assert se2["rows"] and all(r["region"] == "US" for r in se2["rows"])
    sid = ov["accumulation"][0]["security"]["security_id"]
    co = call(env, "GET", "/api/v1/companies/{security_id}/ownership", {"security_id": sid})
    assert co["lag"] and co["holders"] and co["ownership_trend"] and co["short_interest"]
    assert [x["period_end"] for x in co["ownership_trend"]] == sorted(x["period_end"] for x in co["ownership_trend"])
    assert all(x["knowledge_date"] > x["period_end"] for x in co["ownership_trend"])   # PIT: filed after the quarter end
    assert {h_["status"] for h_ in co["holders"]} <= {"new", "added", "trimmed", "unchanged", "unknown"}
    assert any(h_["hedge_fund_like"] for h_ in co["holders"])
    eu_sid = next(i["security_id"] for i in eu["items"] if i["security_id"])
    co2 = call(env, "GET", "/api/v1/companies/{security_id}/ownership", {"security_id": eu_sid})
    assert co2["eu_shorts"]["current"] and co2["eu_shorts"]["notifications"] and co2["summary"]["eu_short_pct"]
    call(env, "GET", "/api/v1/companies/{security_id}/ownership", {"security_id": "SNOPE"}, status=404)
    # alert rule types validate their params
    for rt, params, bad in (("activist_13d", {"include_amendments": True}, None),
                            ("short_interest_spike", {"min_chg_pct": 0.3}, {"min_chg_pct": -1}),
                            ("eu_short_position", {"min_pct": 0.5}, {"min_pct": 500})):
        a = call(env, "POST", "/api/v1/alerts", json={"security_id": sid, "rule_type": rt, "params": params}, status=201)
        assert a["rule_type"] == rt
        if bad:
            call(env, "POST", "/api/v1/alerts", json={"security_id": sid, "rule_type": rt, "params": bad}, status=422)
        call(env, "DELETE", "/api/v1/alerts/{alert_id}", {"alert_id": a["alert_id"]}, status=204)


def test_smart_money_studio_source(env):
    cat = call(env, "GET", "/api/v1/studio/catalog/datasets")
    src = next(s for s in cat["items"] if s["source"] == "smart_money")
    assert src["n_variables"] > 0
    page = call(env, "GET", "/api/v1/studio/catalog/variables", params={"source": "smart_money", "q": "score"})
    assert any(v["field"] == "score_pct" for v in page["items"])


def test_theses_endpoints(env, ids):
    """Thesis tracker (roadmap #8, v1.26.0): every /theses operation, responses validated against the contract."""
    sid = ids["us"]
    dfl = call(env, "GET", "/api/v1/theses/subject-defaults", params={"subject_type": "security", "subject_id": sid})
    px = dfl["last_price"]
    call(env, "GET", "/api/v1/theses/subject-defaults", params={"subject_type": "sector", "subject_id": "NOPE"}, status=404)
    body = {"title": "API thesis", "subject_type": "security", "subject_id": sid, "direction": "long", "horizon": "6M",
            "conviction": 4, "entry_lo": px * 0.95, "entry_hi": px * 1.02, "target": px * 1.3, "stop": px * 0.8,
            "review_every_days": 14, "tags": ["api"], "key_drivers": ["margin expansion"],
            "kpis": [{"label": "Composite", "kind": "metric", "ref": "composite", "op": "gte", "threshold": 0},
                     {"label": "Momentum", "kind": "expr", "expr": "chg_3m * 100", "op": "gt", "threshold": -1000}],
            "catalysts": [{"label": "Capital markets day", "date": "2026-11-15", "kind": "corporate"}],
            "invalidation": [{"label": "Breaks 200d", "kind": "price_below", "level": px * 0.5}],
            "origin": {"kind": "company", "ref": sid}}
    t = call(env, "POST", "/api/v1/theses", json=body, status=201)
    tid = t["thesis_id"]
    assert t["alerts"] and all(a["rule_type"] for a in t["alerts"]) and t["pending_review"]
    call(env, "POST", "/api/v1/theses", json={**body, "target": px * 0.5}, status=422)
    lst = call(env, "GET", "/api/v1/theses", params={"status": "idea", "tag": "api"})
    assert lst["items"][0]["thesis_id"] == tid and lst["counts"]["idea"] >= 1
    assert any(a.get("thesis_id") == tid for a in call(env, "GET", "/api/v1/alerts"))
    call(env, "GET", "/api/v1/theses/{thesis_id}", {"thesis_id": tid})
    call(env, "GET", "/api/v1/theses/{thesis_id}", {"thesis_id": "THNOPE"}, status=404)
    up = call(env, "PATCH", "/api/v1/theses/{thesis_id}", {"thesis_id": tid}, json={"status": "active", "conviction": 5})
    assert up["status"] == "active" and up["conviction"] == 5
    st = call(env, "GET", "/api/v1/theses/{thesis_id}/status", {"thesis_id": tid})
    assert st["auto_status"] in ("on_track", "at_risk", "invalidated", "unknown") and len(st["kpis"]) == 2
    call(env, "POST", "/api/v1/theses/{thesis_id}/evaluate", {"thesis_id": tid})
    e = call(env, "POST", "/api/v1/theses/{thesis_id}/journal", {"thesis_id": tid},
             json={"body_md": "Note", "links": [{"kind": "url", "url": "https://example.org"}]}, status=201)
    call(env, "PATCH", "/api/v1/theses/{thesis_id}/journal/{entry_id}", {"thesis_id": tid, "entry_id": e["entry_id"]},
         json={"body_md": "Edited"})
    assert len(call(env, "GET", "/api/v1/theses/{thesis_id}/journal", {"thesis_id": tid})) >= 2
    call(env, "DELETE", "/api/v1/theses/{thesis_id}/journal/{entry_id}", {"thesis_id": tid, "entry_id": e["entry_id"]},
         status=204)
    call(env, "POST", "/api/v1/theses/{thesis_id}/reviews", {"thesis_id": tid}, json={"decision": "hold"}, status=201)
    rv = call(env, "GET", "/api/v1/theses/{thesis_id}/reviews", {"thesis_id": tid})
    assert any(r["status"] == "completed" for r in rv["items"]) and rv["snapshots"]
    call(env, "POST", "/api/v1/theses/{thesis_id}/snapshots", {"thesis_id": tid}, json={"note": "x"}, status=201)
    pf = call(env, "GET", "/api/v1/theses/{thesis_id}/performance", {"thesis_id": tid})
    assert pf["basis"] in ("local", "usd") and pf["series"]
    tl = call(env, "GET", "/api/v1/theses/{thesis_id}/timeline", {"thesis_id": tid})
    assert tl["bars"] and tl["markers"]
    call(env, "GET", "/api/v1/theses/{thesis_id}/evidence", {"thesis_id": tid})
    r = env["client"].get(f"/api/v1/theses/{tid}/export.md")
    assert r.status_code == 200 and r.text.startswith("# API thesis")
    CALLED.add(("GET", "/api/v1/theses/{thesis_id}/export.md"))
    cl = call(env, "PATCH", "/api/v1/theses/{thesis_id}", {"thesis_id": tid}, json={"status": "closed", "close_reason": "other"})
    assert cl["post_mortem_md"] and cl["outcome"]
    stats = call(env, "GET", "/api/v1/theses/stats")
    assert stats["n_closed"] >= 1
    m = call(env, "POST", "/api/v1/theses", json={"title": "Country view", "subject_type": "country", "subject_id": "US"},
             status=201)
    call(env, "GET", "/api/v1/theses/{thesis_id}/evidence", {"thesis_id": m["thesis_id"]})
    for x in (tid, m["thesis_id"]):
        call(env, "DELETE", "/api/v1/theses/{thesis_id}", {"thesis_id": x}, status=204)
    assert not [a for a in call(env, "GET", "/api/v1/alerts") if a.get("thesis_id") in (tid, m["thesis_id"])]


def test_supply_chain(env, ids):
    """Roadmap #9: network (depth 1-2, provenance, statistical layer), geography (direct / second-order / look-through
    + overlays), country risk, shock simulation (company / country, macro channel) and portfolio exposure."""
    sid = ids["sid"]
    net = call(env, "GET", "/api/v1/companies/{security_id}/network", {"security_id": sid}, params={"depth": 2})
    assert net["data_status"] == "ok" and net["center_key"] == env["info"]["securities"][0]["issuer_id"]
    types = {e["type"] for e in net["edges"]}
    assert {"disclosed_customer", "disclosed_supplier", "statistical"} <= types
    assert any(n["kind"] == "unresolved" for n in net["nodes"]) and any(n["depth"] == 2 for n in net["nodes"])
    assert all(len(e["provenance"]["snippet"] or "") <= 200 for e in net["edges"])
    m = net["metrics"]
    # PIT: the fixture's future-dated disclosure and the stale one never become edges of the center
    assert m["n_customers"] == 4 and m["n_suppliers"] == 2 and abs(m["top_customer_share"] - 0.22) < 1e-9
    assert abs(m["customer_hhi"] - (0.22 ** 2 + 0.13 ** 2 + 0.11 ** 2 + 0.10 ** 2)) < 1e-6
    d1 = call(env, "GET", "/api/v1/companies/{security_id}/network", {"security_id": sid},
              params={"depth": 1, "include_statistical": "false"})
    assert "statistical" not in {e["type"] for e in d1["edges"]} and len(d1["nodes"]) < len(net["nodes"])
    call(env, "GET", "/api/v1/companies/{security_id}/network", {"security_id": "SNOPE"}, status=404)
    geo = call(env, "GET", "/api/v1/companies/{security_id}/geography", {"security_id": sid})
    lt = [c["look_through"] or 0 for c in geo["countries"]]
    assert geo["countries"] and abs(sum(lt) - 1) < 1e-3 and any(c["second_order"] for c in geo["countries"])
    assert geo["coverage"]["level"] == "full" and geo["sectors"]
    cr = call(env, "GET", "/api/v1/supply-chain/country-risk")
    assert cr["data_status"] in ("ok", "pending")
    sh = call(env, "POST", "/api/v1/supply-chain/shock", json={"target_type": "company", "security_id": sid,
                                                              "magnitude": -0.2, "include_statistical": True,
                                                              "macro": {"scenario_id": "preset_oil_up_50"}})
    assert sh["model_based"] and sh["impacts"][0]["key"] == net["center_key"] and sh["impacts"][0]["channel"] == "seed"
    sup = [x for x in sh["impacts"] if x["channel"] == "demand"]
    assert sup and all(x["revenue_impact"] < 0 for x in sup) and all(abs(x["revenue_impact"]) <= 0.2 for x in sh["impacts"])
    assert sh["statistical"] and sh["macro"] is not None
    sc = call(env, "POST", "/api/v1/supply-chain/shock", json={"target_type": "country", "iso2": "CN", "magnitude": -0.1})
    assert sc["seed"]["type"] == "country"
    call(env, "POST", "/api/v1/supply-chain/shock", json={"target_type": "country"}, status=422)
    pe = call(env, "POST", "/api/v1/supply-chain/portfolio-exposure",
              json={"security_ids": [sid, ids["us"]], "basis": "look_through"})
    assert pe["coverage"]["n_items"] == 2 and pe["countries"]
    assert abs(sum(c["look_through"] or 0 for c in pe["countries"]) - 1) < 1e-3
    call(env, "POST", "/api/v1/supply-chain/portfolio-exposure", json={"watchlist_id": "WNOPE"}, status=404)


def test_data_ops_endpoints(env):
    """v1.28: scheduler / data-as-of / sections / ops alerts on /system/status, schedule profiles, Data Health."""
    st = call(env, "GET", "/api/v1/system/status")
    assert st["scheduler"]["profile"] == "weekly" and st["scheduler"]["enabled"] is False
    assert st["scheduler"]["next_runs"]["nightly"]
    assert any(a["id"] in ("worker_down", "scheduler_off") for a in st["ops_alerts"])
    assert st["data_as_of"] and st["data_as_of_overall"] == max(v for k, v in st["data_as_of"].items() if k in ("US", "EU", "UK", "JP"))
    assert any(s["section"].startswith("prices:") for s in st["serving_sections"])
    assert all(f["optional"] == f["table"].startswith("live_") for f in st["data_freshness"])
    sc = call(env, "GET", "/api/v1/system/schedule")
    assert sc["active"] == "weekly" and {p["id"] for p in sc["profiles"]} >= {"weekly", "daily", "intraday"}
    sc = call(env, "PUT", "/api/v1/system/schedule", json={"profile": "daily"})
    assert sc["active"] == "daily" and sc["source"] == "state"
    call(env, "PUT", "/api/v1/system/schedule", json={"profile": "hourly"}, status=422)
    call(env, "PUT", "/api/v1/system/schedule", json={"profile": "weekly"})
    dh = call(env, "GET", "/api/v1/system/data-health")
    assert dh["profile"]["active"] == "weekly" and len(dh["datasets"]) >= 20
    assert {c["id"] for c in dh["campaigns"]} >= {"gdelt_e6", "sec_13f_history"}
    assert dh["gaps"]["total"] == 0 and not dh["filler"]["alive"]


def test_regression_guard_endpoints(env):
    """v1.30 (O2.3): /system/qa (pending before any run), summary + ops alert after a failed run, Data Health qa."""
    from app.qa.common import state_path, write_state

    sp = state_path(env["settings"])
    if sp.exists():
        sp.unlink()
    qa = call(env, "GET", "/api/v1/system/qa")
    assert qa["status"] == "pending" and qa["suites"] == {} and qa["alert"] is None
    from datetime import UTC, datetime, timedelta

    t_fin = datetime.now(UTC) - timedelta(minutes=5)   # v1.43: results age out - a recent run raises the error alert
    write_state({"run_id": "QA20261020T000000Z", "status": "failed", "started_at": (t_fin - timedelta(minutes=5)).isoformat(),
                 "finished_at": t_fin.isoformat(), "api_base_url": None, "client": "in_process",
                 "suites": {"api": {"status": "failed", "failed": 2, "counts": {"passed": 140, "failed": 2}},
                            "golden": {"status": "passed", "failed": 0, "counts": {"passed": 12}},
                            "smoke": {"status": "skipped", "failed": 0, "message": "Chrome not found"}},
                 "issues": [{"suite": "api", "item": "getX", "url": "/api/v1/x", "detail": "HTTP 500"}],
                 "message": "failed: api failed, golden passed, smoke skipped"}, env["settings"])
    qa = call(env, "GET", "/api/v1/system/qa", params={"detail": True})
    assert qa["status"] == "failed" and qa["suites"]["api"]["failed"] == 2 and qa["issues"][0]["item"] == "getX"
    assert qa["alert"]["id"] == "qa_regression" and qa["alert"]["severity"] == "error" and isinstance(qa["details"], list)
    st = call(env, "GET", "/api/v1/system/status")
    assert any(a["id"] == "qa_regression" for a in st["ops_alerts"])
    from app.api.routers import system as sysr

    sysr._TTL.clear()  # data-health is cached 5 s
    dh = call(env, "GET", "/api/v1/system/data-health")
    assert dh["qa"]["status"] == "failed" and any(a["id"] == "qa_regression" for a in dh["alerts"])
    sp.unlink()


def test_probes_ready_metrics_request_id(env):
    """v1.31 (O4): /ready (503 without a live worker, 200 with one), /metrics (Prometheus text), X-Request-ID."""
    from filelock import FileLock

    from app.api.observability import reset_state_cache

    s = env["settings"]
    (s.state_dir / "worker.json").unlink(missing_ok=True)
    body = call(env, "GET", "/ready", status=503)
    assert body["status"] == "not_ready" and body["serving_run_id"]  # (an earlier test flips the snapshot)
    chk = {c["name"]: c for c in body["checks"]}
    assert chk["serving_snapshot"]["ok"] and not chk["worker_heartbeat"]["ok"] and chk["worker_heartbeat"]["required"]
    import json as _json
    from datetime import UTC, datetime

    (s.state_dir / "worker.json").write_text(_json.dumps({"pid": 1, "heartbeat_at": datetime.now(UTC).isoformat()}))
    lk = FileLock(str(s.state_dir / "worker.lock"), timeout=0)
    lk.acquire()
    try:
        body = call(env, "GET", "/ready")
        assert body["status"] == "ready" and body["worker_heartbeat_age_s"] is not None
        reset_state_cache()
        call(env, "GET", "/api/v1/companies/{security_id}/overview", {"security_id": env["info"]["securities"][0]["security_id"]})
        txt = call(env, "GET", "/metrics")
    finally:
        lk.release()
        (s.state_dir / "worker.json").unlink(missing_ok=True)
    assert "# TYPE big360_http_requests_total counter" in txt
    assert 'route="/api/v1/companies/{security_id}/overview"' in txt  # template, never the raw id
    assert "big360_http_request_duration_seconds_bucket" in txt and 'le="+Inf"' in txt
    assert "big360_serving_snapshot_present 1" in txt and "big360_worker_up 1" in txt
    assert 'big360_build_info{version=' in txt and 'contract_version="1.50.0"' in txt
    for line in txt.splitlines():  # every sample line is `name{labels} value`
        if line and not line.startswith("#"):
            name_labels, _, value = line.rpartition(" ")
            assert name_labels and value, line
            float(value.replace("+Inf", "inf"))
    r = env["client"].get("/health", headers={"X-Request-ID": "net-client-123"})
    assert r.headers["x-request-id"] == "net-client-123"
    r = env["client"].get("/health", headers={"X-Request-ID": "bad id <script>"})
    assert len(r.headers["x-request-id"]) == 32 and r.headers["x-request-id"] != "bad id <script>"


def test_conformal(env, ids):
    """v1.32.0 conformal P3: calibrated range (marginal bands only - the failed joint bands are never served), touch
    probabilities (auto + given levels, extrapolated horizons flagged), peer groups, pair / basket bands, coverage."""
    sid = ids["sid"]
    e = call(env, "GET", "/api/v1/conformal/envelope/{security_id}", {"security_id": sid})
    assert e["data_status"] == "ok" and e["model_based"] and len(e["bands"]) == 12
    b = {(x["horizon"], x["level"]): x for x in e["bands"]}
    assert b[(1, 80)]["hi"] < b[(1, 95)]["hi"] < b[(20, 95)]["hi"] and b[(5, 90)]["lo"] < e["last_close"] < b[(5, 90)]["hi"]
    # the fixture's joint b{h}_* bands are 30% wider: the served band must be the marginal one
    assert b[(1, 90)]["half_width_log"] < 0.012 * 2.0
    assert e["nextbar"]["experimental"] and len(e["nextbar"]["bands"]) == 9
    assert {c["source"] for c in e["coverage"]} == {"rolling", "walk_forward"}
    assert "over history" in e["labels"]["calibrated_range"] and "not 'X% chance today'" in e["labels"]["calibrated_range"]
    call(env, "GET", "/api/v1/conformal/envelope/{security_id}", {"security_id": "S000000NOPE"}, status=404)
    t = call(env, "GET", "/api/v1/conformal/touch", params={"security_id": sid})
    assert t["mode"] == "auto" and t["calibrated"] and t["levels"]
    z = [x for x in t["levels"] if x["source"] == "zone"]
    assert z and all(0 <= x["p_touch"] <= 1 for x in z) and len(z[0]["by_horizon"]) == 4
    ph = [h["p_touch"] for h in z[0]["by_horizon"]]
    assert ph == sorted(ph)                                                  # longer horizon, higher chance
    t2 = call(env, "GET", "/api/v1/conformal/touch", params={"security_id": sid, "levels": f"{e['last_close'] * 1.02},{e['last_close'] * 0.8}",
                                                              "horizon": 63})
    assert t2["mode"] == "levels" and t2["extrapolated"] and not t2["calibrated"] and "touch_extrapolated" in t2["labels"]
    assert t2["levels"][0]["p_touch"] > t2["levels"][1]["p_touch"]
    call(env, "GET", "/api/v1/conformal/touch", params={"security_id": sid, "levels": "abc"}, status=422)
    pg = call(env, "GET", "/api/v1/conformal/peer-groups", params={"security_id": sid})
    assert pg["data_status"] == "ok" and pg["groups"] and pg["detail"]["kind"] == "industry"
    d = pg["detail"]
    assert d["anomaly_90"] and d["score_pct"] == 0.97 and len(d["history"]) > 200 and d["members"]
    assert any(m["is_breakaway"] for m in d["members"]) or d["breakaway_member"]
    assert "not a trading signal" in pg["labels"]["peer"]
    lst = call(env, "GET", "/api/v1/conformal/peer-groups", params={"region": "US", "anomalies_only": True})
    assert lst["detail"] is None and all(g["anomaly_90"] for g in lst["groups"])
    call(env, "GET", "/api/v1/conformal/peer-groups", params={"group_id": "nope"}, status=404)
    call(env, "GET", "/api/v1/conformal/peer-groups", params={"kind": "nope"}, status=422)
    gid = d["group_id"]
    mem = [m["security_id"] for m in d["members"]]
    pb = call(env, "GET", "/api/v1/conformal/pair-band", params={"group_id": gid, "i": mem[0], "j": mem[1]})
    assert pb["mode"] == "pair" and pb["beta"] is not None and [x["level"] for x in pb["bands"]] == [90, 95]
    assert pb["bands"][0]["hi"] < pb["bands"][1]["hi"] and pb["realised"]["band_as_of"] < pb["realised"]["date"]
    mg = pb["marginal"]
    assert mg["n_cal"] == 43 and [x["level"] for x in mg["bands"]] == [80, 90, 95]
    assert mg["bands"][1]["hi"] - mg["bands"][1]["lo"] < pb["bands"][0]["hi"] - pb["bands"][0]["lo"]   # narrower than the joint projection
    bk = call(env, "GET", "/api/v1/conformal/pair-band", params={"group_id": gid, "weights": f"{mem[0]}:0.5,{mem[2]}:0.5"})
    assert bk["mode"] == "weights" and bk["sd"] > 0
    call(env, "GET", "/api/v1/conformal/pair-band", params={"group_id": gid}, status=422)
    call(env, "GET", "/api/v1/conformal/pair-band", params={"group_id": gid, "weights": "NOPE:1"}, status=422)
    call(env, "GET", "/api/v1/conformal/pair-band", params={"group_id": "nope", "i": mem[0], "j": mem[1]}, status=404)
    cv = call(env, "GET", "/api/v1/conformal/coverage")
    assert cv["data_status"] == "ok" and cv["rows"] and {g["verdict"] for g in cv["gates"]} >= {"pass", "fail"}
    assert any(f["segment"] == "US" and f["application"] == "peer" for f in cv["flags"])
    assert any(r["application"] == "envelope_wf" for r in cv["rows"]) and cv["validation_md"] is None
    cv2 = call(env, "GET", "/api/v1/conformal/coverage", params={"application": "peer", "include_doc": True})
    assert {r["application"] for r in cv2["rows"]} == {"peer"}
    call(env, "GET", "/api/v1/conformal/coverage", params={"application": "nope"}, status=422)
    # alert rule types validate through the API
    a = call(env, "POST", "/api/v1/alerts", json={"security_id": sid, "rule_type": "price_outside_calibrated_range",
                                                  "params": {}}, status=201)
    assert a["params"] == {"level": 95, "horizon": 1, "side": "any"}
    a2 = call(env, "POST", "/api/v1/alerts", json={"security_id": sid, "rule_type": "conformal_peer_anomaly",
                                                   "params": {"percentile": 0.97, "scope": "group"}}, status=201)
    call(env, "POST", "/api/v1/alerts", json={"security_id": sid, "rule_type": "conformal_peer_anomaly",
                                              "params": {"percentile": 0.1}}, status=422)
    for x in (a, a2):
        call(env, "DELETE", "/api/v1/alerts/{alert_id}", {"alert_id": x["alert_id"]}, status=204)


def test_conformal_fan_and_technicals_context(env, ids):
    """v1.33.0 Technicals v2 T1 / T5: the calibrated fan for every horizon 1..20 (per-h radii from the job, or
    interpolated for rows written before the fan) on exchange sessions with the earnings date flagged, and the
    volatility / regime / earnings context."""
    import json as _json
    from datetime import date as _date
    from datetime import timedelta as _td

    s = env["settings"]
    sid = ids["sid"]
    secs = env["info"]["securities"]
    # an exchange-calendar cache (O1 format) with a holiday 3 weekdays after the snapshot date
    # (API responses are cached per URL: the fan security must first be requested after the calendar cache exists)
    d0 = _date.fromisoformat(call(env, "GET", "/api/v1/conformal/envelope/{security_id}", {"security_id": secs[2]["security_id"]})["as_of"])
    days, d = [], d0 - _td(days=30)
    while len(days) < 80:
        d += _td(days=1)
        if d.weekday() < 5:
            days.append(d)
    after = [x for x in days if x > d0]
    holiday = after[2]
    cal_dir = s.cache_dir / "ops" / "sessions"
    cal_dir.mkdir(parents=True, exist_ok=True)
    for mic in ("XNYS", "XETR", "XLON", "XTKS", "XHKG", "XKRX", "XSHG", "XTSE", "BVMF", "XTAI"):
        (cal_dir / f"{mic}.json").write_text(_json.dumps({"mic": mic, "built": d0.isoformat(),
                                                           "sessions": [x.isoformat() for x in days if x != holiday]}))
    # secs[4]: even fixture index = per-h fan columns; not requested before (test_conformal's sid response is cached)
    e = call(env, "GET", "/api/v1/conformal/envelope/{security_id}", {"security_id": secs[4]["security_id"]})
    fan, info = e["fan"], e["fan_info"]
    assert [p["h"] for p in fan] == list(range(1, 21)) and info["source"] == "calibrated" and info["calendar"] != "weekdays"
    dates = [p["date"] for p in fan]
    assert dates == sorted(dates) and holiday.isoformat() not in dates and dates[0] > e["as_of"]
    assert all(_date.fromisoformat(x).weekday() < 5 for x in dates)
    lc = e["last_close"]
    for p in fan:
        assert p["lo_95"] <= p["lo_90"] <= p["lo_80"] < lc < p["hi_80"] <= p["hi_90"] <= p["hi_95"]
    assert all(b["hi_90"] > a["hi_90"] and b["lo_90"] < a["lo_90"] for a, b in zip(fan, fan[1:]))
    assert info["monotone"] and "not 'X% chance today'" in info["note"]
    b20 = next(b for b in e["bands"] if b["horizon"] == 20 and b["level"] == 90)
    assert fan[19]["hi_90"] > b20["hi"]                        # the fixture's per-h radii are 4% above qm_20 at h = 20
    ev = info["earnings"]
    assert ev and ev["source"] == "earnings_calendar" and ev["date_source"]
    assert [p["h"] for p in fan if p["earnings"]] == [ev["h"]] and dates[ev["h"] - 1] >= ev["date"]
    assert all(p["event_pool"] == (p["h"] >= ev["h"]) for p in fan) or e["earn_days_ahead"] is None
    # a row written before the fan existed: radius interpolated between the calibrated 1/5/10/20 radii
    e1 = call(env, "GET", "/api/v1/conformal/envelope/{security_id}", {"security_id": secs[1]["security_id"]})
    assert e1["fan_info"]["source"] == "interpolated" and len(e1["fan"]) == 20
    b5 = next(b for b in e1["bands"] if b["horizon"] == 5 and b["level"] == 90)
    assert abs(e1["fan"][4]["hi_90"] - b5["hi"]) < 1e-3 * b5["hi"]
    # no envelope row: empty fan, contract shape
    e_none = call(env, "GET", "/api/v1/conformal/envelope/{security_id}", {"security_id": secs[-1]["security_id"]})
    assert e_none["fan"] == [] and e_none["fan_info"]["source"] == "none"
    # T5 context
    c = call(env, "GET", "/api/v1/technicals/{security_id}/context", {"security_id": sid})
    assert c["data_status"] == "ok" and c["model_based"] and c["lookback_sessions"] == 504
    rw = c["range_width"]
    assert 0 <= rw["percentile"] <= 1 and rw["state"] in ("squeeze", "normal", "expansion") and rw["n"] > 400
    assert rw["width_log"] > 0 and rw["radius"] == pytest.approx(2.8, rel=1e-3)
    assert c["realised_vol"]["vol"] > 0 and 0 <= c["realised_vol"]["percentile"] <= 1
    assert c["atr"]["atr_pct"] > 0 and 0 <= c["atr"]["percentile"] <= 1
    assert 0 <= c["adx"]["adx"] <= 100 and c["adx"]["state"] in ("weak", "emerging", "trending", "strong")
    assert c["trend"]["daily"] in ("up", "down", "flat") and c["trend"]["alignment"] != "n/a"
    assert c["implied_vol"]["available"] is False and c["implied_vol"]["value"] is None and "n/a" in c["implied_vol"]["note"]
    er = c["earnings"]
    assert er["next_date"] and 1 <= er["sessions_ahead"] <= 20 and er["in_window"]
    assert er["typical_abs_move"] > 0 and er["n_events"] > 0
    assert {"range_width", "realised_vol", "implied_vol", "atr", "adx", "trend", "earnings"} <= set(c["notes"])
    call(env, "GET", "/api/v1/technicals/{security_id}/context", {"security_id": "S000000NOPE"}, status=404)


def test_technicals_probabilities(env, ids):
    """v1.34.0 Technicals v2 T2 / T3 / T4: setup probabilities (stored + custom thesis barriers), zones with the hold
    model and the edge flag, calibrated score; every output carries its gate (experimental = failed)."""
    sid = ids["sid"]
    sp = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid})
    assert sp["data_status"] == "ok" and sp["mode"] == "setup" and sp["model_based"]
    hz = {h["horizon"]: h for h in sp["horizons"]}
    assert sorted(hz) == [5, 10, 20, 40, 60] and sp["setup_horizon"] in hz
    for h in hz.values():
        assert abs(h["p_win"] + h["p_loss"] + h["p_neither"] - 1) < 1e-3
        assert h["break_even_p"] == pytest.approx(1 / (1 + sp["setup"]["reward_risk_close"]), abs=1e-3)
    assert hz[5]["p_neither"] > hz[60]["p_neither"]                      # longer horizon: fewer undecided paths
    assert not hz[20]["experimental"] and hz[60]["experimental"] and hz[60]["gate"] == "fail"
    assert {v["output"] for v in sp["validation"]} == {f"t2_h{h}" for h in (5, 10, 20, 40, 60)}
    lc = sp["last_close"]
    cu = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid},
              params={"target": lc * 1.10, "stop": lc * 0.97})
    assert cu["mode"] == "custom" and cu["setup"]["direction"] == "long" and cu["horizons"]
    ch = {h["horizon"]: h for h in cu["horizons"]}
    assert ch[20]["p_win"] < hz[20]["p_win"]                             # a farther target is reached less often
    sh = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid},
              params={"target": lc * 0.9, "stop": lc * 1.05, "direction": "short"})
    assert sh["setup"]["direction"] == "short" and sh["horizons"]
    call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid}, params={"target": lc * 1.1}, status=422)
    call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid},
         params={"target": lc * 0.9, "stop": lc * 0.8}, status=422)
    call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": "S000000NOPE"}, status=404)
    last = env["info"]["securities"][-1]["security_id"]                   # no tp rows: contract shape, pending / partial
    e0 = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": last})
    assert e0["horizons"] == [] and e0["data_status"] in ("pending", "partial")
    zr = call(env, "GET", "/api/v1/technicals/{security_id}/zones", {"security_id": sid})
    assert zr["data_status"] == "ok" and len(zr["zones"]) == 6 and zr["horizon"] == 20
    z1 = next(z for z in zr["zones"] if z["zone_id"] == 1)
    assert z1["p_hold"] is not None and z1["p_touch"] is not None and z1["p_touch_hold"] == pytest.approx(z1["p_touch"] * z1["p_hold"], abs=1e-3)
    assert z1["edge_significant"] and z1["edge_label"] != "no reliable edge" and z1["edge_experimental"]
    assert any(z["edge_label"] == "no reliable edge" for z in zr["zones"])
    assert not z1["experimental"] and "no_edge" in zr["labels"]
    zr5 = call(env, "GET", "/api/v1/technicals/{security_id}/zones", {"security_id": sid}, params={"horizon": 5})
    assert all(a["p_touch"] <= b["p_touch"] + 1e-9 for a, b in zip(zr5["zones"], zr["zones"]) if a["p_touch"] is not None)
    call(env, "GET", "/api/v1/technicals/{security_id}/zones", {"security_id": sid}, params={"horizon": 63}, status=422)
    sc = call(env, "GET", "/api/v1/technicals/{security_id}/score-calibration", {"security_id": sid})
    assert sc["data_status"] == "ok" and sc["technical_score"] == 72.0 and len(sc["sub_scores"]) == 5
    assert all(0 <= x["percentile"] <= 100 and x["raw"] is not None for x in sc["sub_scores"])
    b20 = next(b for b in sc["buckets"] if b["horizon"] == 20)
    assert b20["decile"] == 8 and b20["line"].startswith("Historically this score bucket") and b20["experimental"]
    assert [t["horizon"] for t in sc["tables"]] == [5, 20, 60] and len(sc["tables"][0]["deciles"]) == 10
    s0 = call(env, "GET", "/api/v1/technicals/{security_id}/score-calibration", {"security_id": last})
    assert s0["buckets"] == [] and s0["technical_score"] == 72.0


def test_technicals_meta_setup(env, ids):
    """v1.35.0 Technicals v3 #2 meta-labelled setups: stored meta view per horizon (10 / 20 / 40) with T2 base, take /
    skip, Kelly fraction and top drivers; gates per output; additive meta view on setup-probabilities (stored + custom
    thesis barriers computed at query time from the fitted artifact)."""
    sid = ids["sid"]
    ms = call(env, "GET", "/api/v1/technicals/{security_id}/meta-setup", {"security_id": sid})
    assert ms["data_status"] == "ok" and ms["model_based"] and ms["horizon"] == 20
    hz = {h["horizon"]: h for h in ms["horizons"]}
    assert sorted(hz) == [10, 20, 40]
    for h in hz.values():
        assert 0 < h["p_win"] < 1 and abs(h["p_win"] + h["p_loss"] + h["p_neither"] - 1) < 1e-3
        assert h["decision"] in ("take", "skip") and (h["kelly_fraction"] == 0) == (h["decision"] == "skip")
        assert 0 <= h["kelly_fraction"] <= 0.02 and len(h["drivers"]) == 3
        assert h["delta_p_win"] == pytest.approx(h["p_win"] - h["t2_p_win"], abs=2e-4)
        assert all(d["direction"] in ("raises", "lowers") and d["label"] for d in h["drivers"])
    assert hz[20]["gate"] == "pass" and not hz[20]["experimental"] and hz[40]["experimental"] and hz[20]["decision_experimental"]
    assert {v["output"] for v in ms["validation"]} == {f"{k}_h{h}" for k in ("meta", "decision") for h in (10, 20, 40)}
    assert ms["setup"]["direction"] == "long" and "meta" in ms["labels"]
    m40 = call(env, "GET", "/api/v1/technicals/{security_id}/meta-setup", {"security_id": sid}, params={"horizon": 40})
    assert m40["horizon"] == 40
    last = env["info"]["securities"][-1]["security_id"]
    e0 = call(env, "GET", "/api/v1/technicals/{security_id}/meta-setup", {"security_id": last})
    assert e0["horizons"] == [] and e0["data_status"] in ("pending", "partial")
    call(env, "GET", "/api/v1/technicals/{security_id}/meta-setup", {"security_id": "S000000NOPE"}, status=404)
    sp = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid})
    assert sp["meta"]["source"] == "stored" and [h["horizon"] for h in sp["meta"]["horizons"]] == [10, 20, 40]
    lc = sp["last_close"]
    cu = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid},
              params={"target": lc * 1.10, "stop": lc * 0.97})
    assert cu["meta"]["source"] == "custom" and {h["horizon"] for h in cu["meta"]["horizons"]} == {10, 20, 40}
    c20 = next(h for h in cu["meta"]["horizons"] if h["horizon"] == 20)
    t20 = next(h for h in cu["horizons"] if h["horizon"] == 20)
    assert c20["t2_p_win"] == pytest.approx(t20["p_win"], abs=1e-4) and 0 < c20["p_win"] < 1
    sh = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": sid},
              params={"target": lc * 0.9, "stop": lc * 1.05, "direction": "short"})
    assert sh["meta"] is None or sh["meta"]["source"] == "custom"
    e1 = call(env, "GET", "/api/v1/technicals/{security_id}/setup-probabilities", {"security_id": last})
    assert e1["meta"] is None


def test_technicals_factors_breakout(env, ids):
    """v1.36.0 Technicals v3 #1 / #5: evidence-based factors computed by the REAL nightly job on the fixture lake
    (percentiles within region x sector, IC + gate per factor, composite of the passing members) and the breakout state
    with the calibrated P(follow-through) of a model fitted on the fixture's own history."""
    sid = ids["sid"]
    f = call(env, "GET", "/api/v1/technicals/{security_id}/factors", {"security_id": sid})
    assert f["data_status"] == "ok" and f["region"] == "US" and f["region_validated"] is True
    by = {x["id"]: x for x in f["factors"]}
    assert set(by) == {"hi52", "mom", "rmom", "strev", "maxret", "ivol", "indmom"}
    for x in by.values():
        assert x["percentile"] is None or 0 <= x["percentile"] <= 100
        assert [c["horizon"] for c in x["ic"]] == [5, 20, 60] and x["reference"]
        if x["percentile"] is not None:
            exp = x["percentile"] if x["sign"] > 0 else 100 - x["percentile"]
            assert x["aligned_percentile"] == pytest.approx(exp, abs=0.02)
    assert not by["hi52"]["experimental"] and by["hi52"]["badge"]["tone"] == "up" and by["hi52"]["verdict"] == "pass"
    assert by["mom"]["experimental"] and by["mom"]["badge"]["label"] == "no reliable edge"
    assert by["ivol"]["sign"] == -1 and by["maxret"]["sign"] == -1
    c = f["composite"]
    assert c["verdict"] == "pass" and c["members"] == ["hi52", "maxret", "ivol"] and 0 <= c["value"] <= 100
    assert f["validation"]["n_cells_tested"] == 105 and "hi52" in f["validation"]["passing"]
    call(env, "GET", "/api/v1/technicals/{security_id}/factors", {"security_id": "S000000NOPE"}, status=404)
    # find a security with a breakout state in the fixture
    rows = [r for r in (call(env, "GET", "/api/v1/technicals/{security_id}/breakout", {"security_id": x["security_id"]})
                        for x in env["info"]["securities"][:60]) if r["state"] != "none"]
    assert rows, "no breakout state in the fixture"
    b = rows[0]
    assert b["data_status"] == "ok" and b["state"] in ("active", "followed_through", "failed") and b["direction"] in (1, -1)
    assert b["lookback"] in (20, 55) and b["sessions_since"] is not None and 0 <= b["sessions_since"] <= 20
    assert b["p_follow"] is not None and 0 < b["p_follow"] < 1 and b["gate"] == "pass" and not b["experimental"]
    assert b["evidence"]["verdict"] == "fail" and b["evidence"]["significant"] is False and b["recent"]
    assert all(e["outcome"] in ("success", "fail", "neither", "open") for e in b["recent"])
    call(env, "GET", "/api/v1/technicals/{security_id}/breakout", {"security_id": "S000000NOPE"}, status=404)


def test_bql_endpoints(env, ids):
    """v1.37.0 roadmap v3 B1 BQL-lite: run (cross-section, time series, grouped), explain, fields, export, resolve,
    saved queries (screen as a universe), Model Studio source bql and the Backtest Lab signal source bql."""
    r = call(env, "POST", "/api/v1/bql", json={"query": "get(px_last, chg_pct(px, 1M), roic, sector) for(['AAPL US']) "})
    assert r["mode"] == "cross_section" and r["n_rows"] == 1 and r["rows"][0][1] == "AAPL"
    assert r["units"]["px_last"] == "price" and r["units"]["chg_pct(px,1M)"] == "pct"
    ts = call(env, "POST", "/api/v1/bql", json={"query": "get(px_last, f.quality) for(members('mvp_us')) "
                                                         "with(dates=range(-6M, 0D), freq=M) sort(px_last desc) limit(3)"})
    assert ts["mode"] == "timeseries" and ts["dates"]["freq"] == "M" and len({x[0] for x in ts["rows"]}) == 3
    g = call(env, "POST", "/api/v1/bql", json={"query": "get(chg_pct_1m, mkt_cap_usd) for(members('full')) group(region).agg(median)"})
    assert g["grouped"] and [c["name"] for c in g["columns"]][:2] == ["region", "n"]
    pit = call(env, "POST", "/api/v1/bql", json={"query": "get(px_last, roic) for(['AAPL US']) with(as_of=-3M)"})
    assert pit["as_of"] < r["as_of"]
    again = call(env, "POST", "/api/v1/bql", json={"query": "get(px_last, chg_pct(px, 1M), roic, sector) for(['AAPL US'])"})
    assert again["cached"]
    bad = call(env, "POST", "/api/v1/bql", status=422, json={"query": "get(px_last; DROP TABLE sec) for(['AAPL'])"})
    assert bad["title"] == "Invalid BQL query" and bad["errors"][0]["loc"][0] == "query"
    call(env, "POST", "/api/v1/bql", status=422, json={"query": "get(nope_field) for(['AAPL US'])"})
    ex = call(env, "POST", "/api/v1/bql/explain", json={"query": "get(px_last) for(['AAPL US']) where(name != 'zzz')"})
    assert "zzz" not in ex["sql"] and ex["params"] and ex["ast"]["get"][0]["expr"] == {"name": "px_last"}
    call(env, "POST", "/api/v1/bql/explain", status=422, json={"query": "get("})
    fp = call(env, "GET", "/api/v1/bql/fields", params={"q": "hi52", "limit": 5})
    assert fp["total"] >= 3 and len(fp["items"]) <= 5 and fp["counts"]["fields"] > 700 and fp["functions"]
    assert any(f["mnemonic"] == "tech.hi52_pct" for f in call(env, "GET", "/api/v1/bql/fields", params={"q": "tech.hi52"})["items"])
    csv_text = call(env, "POST", "/api/v1/bql/export", json={"query": "get(px_last) for(['AAPL US'])", "format": "csv"})
    assert csv_text.splitlines()[0] == "security_id,ticker,name,px_last"
    rx = env["client"].post("/api/v1/bql/export", json={"query": "get(px_last) for(['AAPL US'])", "format": "xlsx"})
    assert rx.status_code == 200 and rx.content[:2] == b"PK"
    rj = env["client"].post("/api/v1/bql/export", json={"query": "get(px_last) for(['AAPL US'])", "format": "json"})
    assert rj.status_code == 200 and rj.json()["rows"][0]["ticker"] == "AAPL"
    call(env, "POST", "/api/v1/bql/export", status=422, json={"query": "get(px) for(sector('Gadgets'))", "format": "csv"})
    res = call(env, "GET", "/api/v1/bql/resolve", params={"q": "AAPL US Equity"})
    assert res["security"]["security_id"] == ids["sid"] and res["asset_class"] == "Equity" and res["market"] == "US"
    assert call(env, "GET", "/api/v1/bql/resolve", params={"q": "ZZZZ US"})["security"] is None
    # saved queries + a saved screen used as a universe
    s1 = call(env, "POST", "/api/v1/bql/saved", status=201,
              json={"name": "Big US", "query": "get(mkt_cap_usd) for(members('mvp_us')) sort(mkt_cap_usd desc) limit(5)",
                    "kind": "screen", "tags": ["size"]})
    assert s1["kind"] == "screen" and s1["tags"] == ["size"]
    call(env, "POST", "/api/v1/bql/saved", status=409, json={"name": "big us", "query": "get(px) for(['AAPL'])"})
    call(env, "POST", "/api/v1/bql/saved", status=422, json={"name": "broken", "query": "get(px for(['AAPL'])"})
    lst = call(env, "GET", "/api/v1/bql/saved")
    assert any(x["query_id"] == s1["query_id"] for x in lst)
    assert call(env, "GET", "/api/v1/bql/saved", params={"kind": "query"}) == []
    scr = call(env, "POST", "/api/v1/bql", json={"query": "get(mkt_cap_usd) for(members('screen:Big US'))"})
    assert scr["n_rows"] == 5
    up = call(env, "PUT", "/api/v1/bql/saved/{query_id}", {"query_id": s1["query_id"]}, json={"description": "top 5"})
    assert up["description"] == "top 5"
    assert call(env, "GET", "/api/v1/bql/saved/{query_id}", {"query_id": s1["query_id"]})["name"] == "Big US"
    call(env, "DELETE", "/api/v1/bql/saved/{query_id}", {"query_id": s1["query_id"]}, status=204)
    call(env, "GET", "/api/v1/bql/saved/{query_id}", {"query_id": s1["query_id"]}, status=404)
    call(env, "PUT", "/api/v1/bql/saved/{query_id}", {"query_id": "QNOPE"}, json={"description": "x"}, status=404)
    call(env, "DELETE", "/api/v1/bql/saved/{query_id}", {"query_id": "QNOPE"}, status=404)
    # Model Studio dataset source "bql" (catalog + preview)
    cat = call(env, "GET", "/api/v1/studio/catalog/datasets")
    assert any(s["source"] == "bql" for s in cat["items"])
    bv = call(env, "GET", "/api/v1/studio/catalog/variables", params={"source": "bql", "q": "chg_pct", "limit": 5})
    assert bv["total"] > 0 and all(v["source"] == "bql" for v in bv["items"])
    pv = call(env, "POST", "/api/v1/studio/datasets/preview", json={"n": 5, "dataset": {
        "kind": "panel", "frequency": "M", "entities": {"universe": "mvp_us"},
        "start": (env["report"].as_of - timedelta(days=400)).isoformat(),
        "variables": [{"name": "mom_rank", "source": "bql", "field": "cs_rank(chg_pct(px, 3M))"},
                      {"name": "val", "source": "factor_scores", "field": "value"}]}})
    cov = {c["column"]: c for c in pv["coverage"]}
    assert pv["n_rows"] > 50 and cov["mom_rank"]["n_non_null"] > 0 and "val" in cov
    # Backtest Lab signal "bql" (cross-sectional, PIT per rebalance)
    bspec = {"name": "BQL momentum", "universe": {"universe_id": "mvp_us"},
             "signal": {"variables": [{"name": "m", "source": "bql", "field": "cs_zscore(chg_pct(px, 3M))"}]},
             "construction": {"side": "long_short", "quantiles": 3}, "robustness": {"permutations": 2}}
    br = call(env, "POST", "/api/v1/backtest/runs", json={"spec": bspec, "mode": "sync"})
    assert br["run"]["status"] == "completed", br["run"]
    assert any("BQL expression" in n for n in br["result"]["notes"])


def test_rates_and_debt(env, ids):
    """W3 rates & debt (rates-w3; fixture tests/fixtures/rates_fixture.py)."""
    cl = call(env, "GET", "/api/v1/rates/curves")
    assert cl["n_with_data"] >= 8 and any(r["source"].startswith("US TreasuryDirect") for r in cl["rejected_sources"])
    ea = next(x for x in cl["items"] if x["curve_id"] == "EA_AAA")
    assert ea["status"] == "ok" and set(ea["types"]) == {"spot", "par", "forward"} and ea["first_date"] < ea["last_date"]
    assert call(env, "GET", "/api/v1/rates/curves", params={"group": "lt10"})["items"][0]["group"] == "lt10"
    c = call(env, "GET", "/api/v1/rates/curves/{curve_id}", {"curve_id": "EA_AAA"},
             params={"type": "spot", "compare": "1m,1y", "history_tenors": "24,120"})
    assert [s["label"] for s in c["snapshots"]][:1] == ["latest"] and len(c["snapshots"]) == 3
    assert len(c["snapshots"][0]["points"]) == 19 and c["params"]["model"] == "svensson"
    assert [h["tenor_months"] for h in c["history"]] == [24, 120] and c["frames"]["dates"]
    assert len(c["frames"]["values"][0]) == len(c["frames"]["tenors"])
    call(env, "GET", "/api/v1/rates/curves/{curve_id}", {"curve_id": "US_TSY"}, params={"frames": "W", "from": "2025-01-01"})
    call(env, "GET", "/api/v1/rates/curves/{curve_id}", {"curve_id": "US_TSY"}, params={"type": "spot"}, status=422)
    call(env, "GET", "/api/v1/rates/curves/{curve_id}", {"curve_id": "NOPE"}, status=404)
    m = call(env, "GET", "/api/v1/rates/monitor")
    us = next(x for x in m["countries"] if x["country"] == "US")
    assert us["s2s10_bp"] is not None and us["spread_vs_bund_bp"] is not None and us["chg_1m_bp"] is not None
    assert any(r["country"] == "US" and r["tenor_months"] == 120 for r in m["real_vs_nominal"])
    assert {x["label"] for x in m["money_market"]} >= {"SOFR", "EUR STR", "SONIA"} and m["lt10"]
    se = call(env, "GET", "/api/v1/rates/series", params={"ids": "US_TSY:120:par", "spreads": "US_TSY:120:par-DE_BUND:120:par"})
    assert se["series"][0]["unit"] == "percent" and se["series"][1]["unit"] == "bp" and se["series"][1]["points"]
    call(env, "GET", "/api/v1/rates/series", params={"ids": "bad id"}, status=422)
    sv = call(env, "GET", "/api/v1/rates/sovereign")
    assert sv["auctions_upcoming"] and sv["us_10y_auctions"] and any(d["country"] == "IT" for d in sv["debt"]) and sv["target2"]
    cr = call(env, "GET", "/api/v1/credit/overview", params={"years": 2})
    assert any(x["group"] == "rating" for x in cr["series"]) and cr["issuance_us_treasury"] and cr["maturity_wall"]
    assert cr["trace_status"] == "needs_key"
    w = call(env, "GET", "/api/v1/credit/maturity-walls")
    assert [b["bucket"] for b in w["wall"]][:6] == ["y1", "y2", "y3", "y4", "y5", "after5"] and w["sectors"]
    sid = env["info"]["securities"][0]["security_id"]
    d = call(env, "GET", "/api/v1/companies/{security_id}/debt", {"security_id": sid})
    assert d["latest"]["interest_coverage"] and d["maturity_wall"] and d["bonds"] and d["data_status"] == "ok"
    assert abs(d["latest"]["avg_cost_of_debt"] - 0.045 * 1.10 / 1.075) < 1e-4   # interest / average of two year-ends
    call(env, "GET", "/api/v1/companies/{security_id}/debt", {"security_id": ids["us"]})
    call(env, "GET", "/api/v1/companies/{security_id}/debt", {"security_id": "SNOPE"}, status=404)
    # coverage v3 A7: ticker URLs (/company/AAPL) resolve to the security id instead of 404
    assert env["info"]["securities"][0]["ticker"] == "AAPL"
    for tic in ("AAPL", "aapl", " AAPL "):
        dt = call(env, "GET", "/api/v1/companies/{security_id}/debt", {"security_id": tic})
        assert dt["security_id"] == sid and dt["issuer_id"] == d["issuer_id"] and dt["history"] == d["history"]
    jp = next((x for x in env["info"]["securities"] if x["ticker"].endswith(".T")), None)
    if jp is not None:
        for tic in (jp["ticker"], jp["ticker"].split(".")[0]):   # 7203.T and its alias 7203
            assert call(env, "GET", "/api/v1/companies/{security_id}/debt", {"security_id": tic})["security_id"] == jp["security_id"]
    call(env, "GET", "/api/v1/companies/{security_id}/debt", {"security_id": "NOPE.XX"}, status=404)


def test_all_operations_covered(env):
    # runs last in this module (pytest keeps definition order)
    missing = sorted(set(operations()) - CALLED - {("GET", "/health")})
    assert not missing, f"operations never exercised: {missing}"


def test_empty_snapshot_shapes(tmp_data):
    """Before the first build every endpoint answers with its contract shape (pending), never 501/500."""
    snapshots.reset()
    from app.api.main import create_app

    c = TestClient(create_app())
    for path in ["/api/v1/market/overview", "/api/v1/sectors", "/api/v1/macro/regime", "/api/v1/insiders/feed",
                 "/api/v1/events/feed", "/api/v1/models/predictions", "/api/v1/system/status", "/api/v1/macro/dashboard"]:
        r = c.get(path)
        assert r.status_code == 200, path
        tpl = path
        validate("GET", tpl, 200, r.json())
    r = c.post("/api/v1/screener/query", json={})
    assert r.status_code == 200 and r.json()["data_status"] == "pending"
    snapshots.reset()
