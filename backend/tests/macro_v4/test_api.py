"""macro v2 W4 API against the synthetic serving lake (the real W4 pipeline ran on it through the serving_small hook):
every macro-v4 operation is called with data and validated against contracts/api.yaml; scenario CRUD round trip."""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots
from app.core import config as config_mod
from app.core import schema as schema_mod
from tests.api_contract import validate


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("w4") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    snapshots.reset()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from app.api.serving import build_serving_db
    from tests.fixtures.serving_small import build_lake

    info = build_lake(s, n_securities=120)
    build_serving_db(s)
    from app.api.main import create_app

    yield {"client": TestClient(create_app()), "info": info, "settings": s}
    snapshots.reset()
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def call(env: dict[str, Any], method: str, tpl: str, path: dict[str, str] | None = None, params: Any = None,
         json: Any = None, status: int = 200) -> Any:
    url = tpl
    for k, v in (path or {}).items():
        url = url.replace("{" + k + "}", v)
    r = env["client"].request(method, url, params=params, json=json)
    assert r.status_code == status, f"{method} {url} -> {r.status_code}: {r.text[:600]}"
    if status == 204:
        return None
    body = r.json()
    validate(method, tpl, status, body)
    return body


def test_pipeline_counts(env):
    c = env["info"]["counts"]
    assert c["macro_factors"] > 1000 and c["macro_exposures"] > 1000 and c["regime_performance"] > 100
    assert c["macro_exposure_fits"] > 50


def test_factors_and_exposures(env):
    f = call(env, "GET", "/api/v1/macro/v4/factors", params={"frequency": "W", "history": 10})
    ids = {x["factor_id"] for x in f["factors"]}
    assert {"d2y", "d10y", "dcurve", "usd", "oil", "equity"} <= ids and f["data_status"] == "ok"
    assert any(s["state_id"] == "rate_cycle_fed" for s in f["states"])
    e = call(env, "GET", "/api/v1/macro/v4/exposures", params={"entity_type": "sector", "model": "joint", "weighting": "ew"})
    assert e["items"] and set(e["items"][0]["betas"]) <= {x["factor_id"] for x in e["factors"]}
    assert e["frequency"] == "W" and e["frequencies_available"] == ["W"]
    sec = call(env, "GET", "/api/v1/macro/v4/exposures",
               params={"entity_type": "security", "model": "univariate", "sort_factor": "d10y", "sort": "asc", "limit": 5})
    b = [i["betas"]["d10y"]["beta"] for i in sec["items"] if "d10y" in i["betas"]]
    assert b == sorted(b) and len(sec["items"]) <= 5
    sid = e["items"][0]["entity_id"]
    h = call(env, "GET", "/api/v1/macro/v4/exposures",
             params={"entity_type": "sector", "entity_id": [sid], "history": "true", "weighting": "ew"})
    assert h["history"] and len(h["history"][0]["points"]) > 3
    call(env, "GET", "/api/v1/macro/v4/exposures", params={"model": "joint", "sort_factor": "nope"}, status=422)
    call(env, "GET", "/api/v1/macro/v4/exposures", params={"entity_type": "nope"}, status=422)


def test_regime_performance(env):
    r = call(env, "GET", "/api/v1/macro/v4/regime-performance",
             params={"regime_type": "cycle_quadrant", "scope": "US", "weighting": "ew"})
    assert r["items"] and r["current_regime"] in r["regimes"]
    cell = next(iter(r["items"][0]["cells"].values()))
    assert cell["n"] >= 3
    call(env, "GET", "/api/v1/macro/v4/regime-performance",
         params={"regime_type": "rate_cycle", "scope": "fed", "entity_type": "factor", "timing": "next_month"})
    call(env, "GET", "/api/v1/macro/v4/regime-performance", params={"regime_type": "bad"}, status=422)
    rs = call(env, "GET", "/api/v1/macro/v4/regime-scopes")
    by = {t["regime_type"]: t for t in rs["regime_types"]}
    assert set(by) == {"cycle_quadrant", "hmm_regime", "rate_cycle", "inflation_regime"}
    assert {"fed", "ecb"} <= {x["scope"] for x in by["rate_cycle"]["scopes"]} and by["cycle_quadrant"]["default_scope"] == "US"
    assert all(x["states"] for t in rs["regime_types"] for x in t["scopes"])


def test_scenarios_crud_and_run(env):
    lst = call(env, "GET", "/api/v1/macro/v4/scenarios")
    assert any(p["scenario_id"] == "preset_rates_up_100" for p in lst["presets"])
    created = call(env, "POST", "/api/v1/macro/v4/scenarios", json={
        "name": "Test +50bp & USD +5%", "shocks": {"rates_10y_bp": 50, "usd_pct": 5},
        "options": {"mode": "conditional"}}, status=201)
    sid = created["scenario_id"]
    assert call(env, "GET", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sid})["name"].startswith("Test")
    upd = call(env, "PUT", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sid},
               json={"name": "Renamed", "options": {"mode": "isolated", "var_propagation": True}})
    assert upd["name"] == "Renamed" and upd["options"]["var_propagation"] is True
    assert sid in {x["scenario_id"] for x in call(env, "GET", "/api/v1/macro/v4/scenarios")["saved"]}
    run = call(env, "POST", "/api/v1/macro/v4/scenarios/{scenario_id}/run", {"scenario_id": sid}, json={"top_n": 5, "weighting": "ew"})
    assert run["var"] is not None and run["var"]["path"] and run["data_status"] == "ok"
    assert len(run["winners"]) <= 5 and run["sectors"]
    sec0 = next(x["sector_id"] for x in run["winners"] + run["losers"] if x["sector_id"])
    flt = call(env, "POST", "/api/v1/macro/v4/scenarios/run", json={"scenario_id": sid, "sectors": [sec0], "top_n": 50})
    assert flt["winners"] and all(x["sector_id"] == sec0 for x in flt["winners"] + flt["losers"])
    assert flt["n_securities"] < run["n_securities"]
    up = call(env, "POST", "/api/v1/macro/v4/scenarios/run", json={"scenario_id": "preset_rates_up_100", "top_n": 3, "weighting": "ew"})
    dn = call(env, "POST", "/api/v1/macro/v4/scenarios/run", json={"shocks": {"rates_2y_bp": -100, "rates_10y_bp": -100},
                                                                  "top_n": 3, "weighting": "ew"})
    up_s = {(x["entity_id"], x["weighting"]): x["impact"] for x in up["sectors"]}
    for x in dn["sectors"]:          # symmetric in log space: (1 + up) * (1 + down) = 1
        u = up_s[(x["entity_id"], x["weighting"])]
        assert abs((1 + u) * (1 + x["impact"]) - 1) < 1e-6
        assert x["impact_lo"] <= x["impact"] <= x["impact_hi"]
    fv = {v["factor_id"]: v["value"] for v in up["factor_vector"]["first_round"]}
    assert fv["d2y"] == pytest.approx(1.0) and fv["d10y"] == pytest.approx(1.0) and fv["dcurve"] == pytest.approx(0.0)
    call(env, "POST", "/api/v1/macro/v4/scenarios/run", json={"shocks": {"bogus": 1}}, status=422)
    call(env, "POST", "/api/v1/macro/v4/scenarios/run", json={}, status=422)
    call(env, "POST", "/api/v1/macro/v4/scenarios", json={"name": "x", "shocks": {"usd_pct": 1},
                                                          "options": {"mode": "nope"}}, status=422)
    call(env, "PUT", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": "preset_rates_up_100"},
         json={"name": "x"}, status=422)
    call(env, "DELETE", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sid}, status=204)
    call(env, "GET", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sid}, status=404)
    call(env, "DELETE", "/api/v1/macro/v4/scenarios/{scenario_id}", {"scenario_id": sid}, status=404)


def test_episodes_and_company(env):
    eps = call(env, "GET", "/api/v1/macro/v4/episodes")
    ids = [e["episode_id"] for e in eps["episodes"]]
    assert "hiking_2022" in ids and "gfc_2008" in ids
    d = call(env, "GET", "/api/v1/macro/v4/episodes/{episode_id}", {"episode_id": "hiking_2022"}, params={"weighting": "ew"})
    assert d["factor_moves"]["d2y"] is not None and d["sectors"]["items"]
    call(env, "GET", "/api/v1/macro/v4/episodes/{episode_id}", {"episode_id": "nope"}, status=404)
    sid = env["info"]["securities"][0]["security_id"]
    m = call(env, "GET", "/api/v1/companies/{security_id}/macro", {"security_id": sid})["macro_v4"]
    assert m["data_status"] == "ok" and m["exposures"]["joint"] and m["scenarios"] and m["episodes"]
    assert any(s["scenario_id"] == "preset_rates_up_100" for s in m["scenarios"])
    assert all(e["exposures"] is not None for e in [m]) and m["sector_regimes"] is not None
    assert any(e["sector"] is not None for e in m["episodes"])
    call(env, "GET", "/api/v1/companies/{security_id}/macro", {"security_id": "SNOPE"}, status=404)


def test_screener_macro_columns(env):
    flds = env["client"].get("/api/v1/screener/fields").json()
    ids = {f["id"] for f in (flds["fields"] if isinstance(flds, dict) else flds)}
    assert {"macro_beta_rates", "macro_impact_rates_down_100bp"} <= ids
    r = env["client"].post("/api/v1/screener/query", json={
        "columns": ["ticker", "macro_beta_rates", "macro_impact_rates_down_100bp"], "sort_by": "macro_impact_rates_down_100bp",
        "sort_dir": "desc", "filters": [{"field": "macro_beta_rates", "op": "not_null"}], "limit": 10})
    assert r.status_code == 200, r.text[:500]
    validate("POST", "/api/v1/screener/query", 200, r.json())
