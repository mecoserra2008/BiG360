"""E1 API: every /events/models operation against a synthetic serving lake (rows written through the table
contracts), validated against contracts/api.yaml; 404 / 422 paths; serving tables present; pending when empty."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

import polars as pl
import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots
from app.core import config as config_mod
from app.core import schema as schema_mod
from tests.api_contract import validate

NOW = None


def _rows(s: Any) -> dict[str, Any]:
    from app.events_models.common import now_utc, write

    now = now_utc()
    wk = date(2026, 9, 28)
    codes = ["US", "CA", "MX", "DE", "FR", "GB", "CN", "JP"]
    hs = []
    for w in (wk - timedelta(days=7), wk):
        for i, c in enumerate(codes):
            hs.append({"year": w.year, "week": w, "theme": "all", "measure": "attention", "iso2": c, "value": float(i),
                       "n_items": 100 + i, "lisa_i": 0.1 * i, "lisa_p": 0.01 if i < 2 else 0.5,
                       "lisa_quadrant": "HH" if i < 2 else "ns", "gi_z": 3.0 - i, "gi_p": 0.002 if i < 2 else 0.4,
                       "hotspot": "hot99" if i < 2 else "ns", "emerging_class": "new_hot" if i < 2 else "none",
                       "n_weeks_hist": 2, "mk_tau": None, "mk_p": None, "moran_i": 0.3, "moran_p": 0.01, "moran_z": 2.5,
                       "n_units": len(codes), "w_kind": "contiguity", "w_source": "linkage", "input_hash": "x",
                       "computed_at": now})
    write(pl.DataFrame(hs), "event_hotspots", settings=s)
    a = date(2026, 10, 9)
    base = {"as_of": a, "window_start": date(2026, 9, 1), "window_end": a, "freq": "D", "response": "ret",
            "shock": "attention_all", "w_kind": "contiguity", "w_source": "linkage", "n_units": 8, "n_periods": 20,
            "llf": -100.0, "aic": 210.0, "pseudo_r2": 0.1, "computed_at": now}
    sp = [base | {"model": m, "effect": e, "iso2": "*", "estimate": 0.1, "std_error": 0.05, "ci_low": 0.0, "ci_high": 0.2,
                  "p_value": 0.04} for m, e in (("sdm", "rho"), ("sdm", "beta"), ("sdm", "theta"), ("sdm", "direct"),
                                               ("sdm", "indirect"), ("sdm", "total"), ("sem", "lambda"), ("ols", "beta"),
                                               ("ols", "lm_lag"))]
    sp += [base | {"model": "sdm", "effect": e, "iso2": c, "estimate": 0.01 * i, "std_error": None, "ci_low": None,
                   "ci_high": None, "p_value": None} for i, c in enumerate(codes) for e in ("transmit", "receive")]
    write(pl.DataFrame(sp, infer_schema_length=None), "spatial_spillovers", settings=s)
    cn = []
    for d in (date(2026, 9, 30), a):
        b = {"universe": "country_ret", "date": d, "window": 200, "horizon": 10, "lags": 1, "n_nodes": 3, "computed_at": now}
        cn.append(b | {"row_type": "system", "node": "*", "other": "*", "total": 70.0, "to_others": None,
                       "from_others": None, "net": None, "series_id": None})
        for c in ("US", "DE", "JP"):
            cn.append(b | {"row_type": "node", "node": c, "other": "*", "total": None, "to_others": 50.0,
                           "from_others": 40.0, "net": 10.0 if c == "US" else -5.0, "series_id": "IDX:x"})
        cn.append(b | {"row_type": "pair", "node": "US", "other": "JP", "total": None, "to_others": None,
                       "from_others": None, "net": 4.0, "series_id": None})
    write(pl.DataFrame(cn, infer_schema_length=None), "connectedness", settings=s)
    ir = [{"as_of": a, "window_start": date(2026, 9, 1), "window_end": a, "model": m, "response": "ret",
           "shock": "attention_all", "component": comp, "scope": sc, "h": h, "coef": -0.1, "std_error": 0.05,
           "ci_low": -0.2, "ci_high": 0.0, "p_value": 0.05, "n_obs": 300, "w_kind": "contiguity", "computed_at": now}
          for m, comp, sc in (("lp", "own", "*"), ("lp", "spatial", "*"), ("svar", "total", "*"), ("svar", "own", "US"))
          for h in range(4)]
    write(pl.DataFrame(ir), "event_irf", settings=s)
    es = [{"as_of": a, "iso2": c, "theme_group": g, "window_start": date(2026, 9, 1), "n_days": 40, "n_events": 1000,
           "mu": 10.0, "alpha_self": 0.5, "alpha_from_nbrs": 0.01, "alpha_to_nbrs": 0.02, "beta": 0.6,
           "branching_ratio": 0.55, "lambda_now": 30.0, "expected_7d": 200.0, "expected_30d": 800.0, "threshold_7d": 250.0,
           "threshold_30d": 900.0, "p_escalation_7d": 0.1 * i, "p_escalation_30d": 0.2, "llf": -500.0, "converged": True,
           "computed_at": now} for i, c in enumerate(codes) for g in ("geo", "all")]
    write(pl.DataFrame(es), "event_escalation", settings=s)
    cp = [{"label_type": "country", "label": c, "theme": "all", "stream": "attention", "date": a - timedelta(days=i),
           "cp_prob": 0.8, "run_length_before": 30, "mean_before": 5.0, "mean_after": 6.0, "shift_sd": 2.0, "direction": "up",
           "n_after": 3, "computed_at": now} for i, c in enumerate(codes)]
    write(pl.DataFrame(cp), "event_changepoints", settings=s)
    gp = [{"iso2": c, "date": a - timedelta(days=k), "gpr": 100.0 + k, "gpr_threats": 90.0, "gpr_acts": 110.0,
           "gpr_ma7": 100.0 + k, "share": 0.1, "n_items": 500, "reference_gpr": 120.0 if c == "GLOBAL" else None,
           "base_start": date(2026, 9, 1), "base_end": a, "model_based": True, "computed_at": now}
          for c in ("GLOBAL", "US", "UA") for k in range(20)]
    write(pl.DataFrame(gp), "gpr_index", mode="overwrite", settings=s)
    lib = [{"shock_kind": "attention", "level": lv, "theme": "conflict_military", "iso2": cc, "sector": "ALL", "window": "0,5",
            "n_events": n, "mean_car": car, "median_car": car, "std_error": 0.3, "ci_low": car - 0.6, "ci_high": car + 0.6,
            "t_stat": car / 0.3, "p_value": 0.01, "mean_bhar": car, "share_negative": 0.7, "first_event": date(2026, 9, 2),
            "last_event": a, "benchmark": "ETF:ACWI", "computed_at": now}
           for lv, cc, n, car in (("country_index", "ALL", 20, -1.2), ("country_index", "UA", 6, -2.5), ("country_index", "IL", 2, -9.0))]
    write(pl.DataFrame(lib), "event_study_library", mode="overwrite", settings=s)
    return {"as_of": a, "week": wk, "codes": codes, "now": now}


def _exposure(s: Any, sid: str, iid: str, info: dict[str, Any]) -> None:
    from app.events_models.common import write

    rows = [{"as_of": info["as_of"] - timedelta(days=7 * k), "security_id": sid, "issuer_id": iid, "country": "US",
             "sector_id": "TECH", "exposure_score": 50.0 + k, "event_pressure": 0.4, "expected_impact": -0.3,
             "n_countries": 2, "top_countries": json.dumps([{"iso2": "US", "weight": 0.7, "pressure": 0.3},
                                                            {"iso2": "CN", "weight": 0.3, "pressure": 0.9}]),
             "top_shocks": json.dumps([{"iso2": "CN", "theme": "trade_tariffs", "attention_z": 3.1, "expected_car": -0.8, "n": 9}]),
             "w_source": "geo_revenue", "computed_at": info["now"]} for k in range(3)]
    write(pl.DataFrame(rows), "company_event_exposure", settings=s)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    d = tmp_path_factory.mktemp("em") / "data"
    old = os.environ.get("BIG360_DATA_DIR")
    os.environ["BIG360_DATA_DIR"] = str(d)
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()
    snapshots.reset()
    s = config_mod.get_settings()
    s.ensure_dirs()
    from app.api.serving import build_serving_db
    from tests.fixtures.serving_small import build_lake

    build_lake(s, n_securities=30, years=4)
    empty = build_serving_db(s)
    from app.api.main import create_app

    client = TestClient(create_app())
    pending = {"hotspots": client.get("/api/v1/events/models/hotspots").json(),
               "gpr": client.get("/api/v1/events/models/gpr").json()}
    info = _rows(s)
    import duckdb

    con = duckdb.connect(str(empty.path), read_only=True)
    sid, iid = con.execute("SELECT security_id, issuer_id FROM sec ORDER BY security_id LIMIT 1").fetchone()
    con.close()
    _exposure(s, sid, iid, info)
    rd = s.data_dir / "logs" / "reports"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "gpr_validation_20261012.json").write_text(json.dumps({"validation": {"daily_global": {"level": {"pearson": 0.4}}}}))
    snapshots.reset()
    rep = build_serving_db(s)
    snapshots.reset()
    yield {"client": TestClient(create_app()), "info": info, "rep": rep, "sid": sid, "pending": pending}
    snapshots.reset()
    if old is None:
        os.environ.pop("BIG360_DATA_DIR", None)
    else:
        os.environ["BIG360_DATA_DIR"] = old
    config_mod.reset_settings_cache()
    schema_mod.load_contracts.cache_clear()


def call(env: dict[str, Any], tpl: str, path: dict[str, str] | None = None, params: Any = None, status: int = 200) -> Any:
    url = tpl
    for k, v in (path or {}).items():
        url = url.replace("{" + k + "}", v)
    r = env["client"].get(url, params=params)
    assert r.status_code == status, f"GET {url} -> {r.status_code}: {r.text[:600]}"
    body = r.json()
    validate("GET", tpl, status, body)
    return body


def test_pending_before_data(env):
    for k, b in env["pending"].items():
        validate("GET", f"/api/v1/events/models/{k}", 200, b)
        assert b["model_based"] is True


def test_serving_tables(env):
    t = env["rep"].tables
    for name in ("em_hotspots", "em_spillovers", "em_connectedness", "em_irf", "em_escalation", "em_changepoints",
                 "em_gpr", "em_gpr_validation", "em_event_studies", "em_exposure"):
        assert name in t
    assert t["em_hotspots"] > 0 and t["em_exposure"] == 3


def test_hotspots(env):
    h = call(env, "/api/v1/events/models/hotspots")
    assert h["week"] == env["info"]["week"].isoformat() and len(h["weeks_available"]) == 2
    assert h["items"][0]["hotspot"] == "hot99" and h["summary"]["hot"] == 2 and h["global_moran"]["moran_i"] == 0.3
    s = call(env, "/api/v1/events/models/hotspots", params={"only_significant": True, "week": "2026-09-23"})
    assert len(s["items"]) == 2 and s["week"] == "2026-09-21"
    call(env, "/api/v1/events/models/hotspots", params={"theme": "nope"}, status=422)
    call(env, "/api/v1/events/models/hotspots", params={"week": "2020-01-01"}, status=404)


def test_spillovers_connectedness_irf(env):
    sp = call(env, "/api/v1/events/models/spillovers")
    assert {m["model"] for m in sp["models"]} == {"sdm", "sem", "ols"}
    assert sp["diagnostics"][0]["test"] == "lm_lag" and sp["transmitters"][0]["iso2"] == env["info"]["codes"][-1]
    assert sp["available"]["shocks"] == ["attention_all"]
    call(env, "/api/v1/events/models/spillovers", params={"shock": "bogus"}, status=422)
    c = call(env, "/api/v1/events/models/connectedness")
    assert c["total"] == 70.0 and c["nodes"][0]["node"] == "US" and c["pairs"] == [{"from": "US", "to": "JP", "net": 4.0}]
    assert len(c["history"]) == 2
    i = call(env, "/api/v1/events/models/irf")
    assert {x["component"] for x in i["components"]} == {"own", "spatial"} and len(i["components"][0]["points"]) == 4
    sv = call(env, "/api/v1/events/models/irf", params={"model": "svar", "scope": "US"})
    assert sv["scopes"] == ["*", "US"]
    call(env, "/api/v1/events/models/irf", params={"model": "svar", "scope": "ZZ"}, status=404)


def test_escalation_changepoints_gpr(env):
    e = call(env, "/api/v1/events/models/escalation", params={"theme_group": "geo"})
    p7 = [x["p_escalation_7d"] for x in e["items"]]
    assert p7 == sorted(p7, reverse=True) and set(e["theme_groups"]) == {"all", "geo"}
    call(env, "/api/v1/events/models/escalation", params={"theme_group": "nope"}, status=422)
    cp = call(env, "/api/v1/events/models/changepoints", params={"days": 3})
    assert len(cp["items"]) == 3 and cp["items"][0]["direction"] == "up"
    g = call(env, "/api/v1/events/models/gpr", params={"iso2": ["GLOBAL", "UA"]})
    assert [x["iso2"] for x in g["series"]] == ["GLOBAL", "UA"] and g["series"][0]["points"][-1]["reference_gpr"] == 120.0
    assert g["validation"]["report_date"] == "2026-10-12" and "Model-based" in g["label"]
    call(env, "/api/v1/events/models/gpr", params={"iso2": ["XX"]}, status=404)


def test_event_studies_and_company(env):
    es = call(env, "/api/v1/events/models/event-studies", params={"theme": "conflict_military", "iso2": "IL"})
    assert es["expected"]["mean_car"] == -1.2 and es["expected"]["match"] == "theme"   # IL cell has n = 2 < min_n
    es2 = call(env, "/api/v1/events/models/event-studies", params={"theme": "conflict_military", "iso2": "UA"})
    assert es2["expected"]["mean_car"] == -2.5 and es2["items"][0]["iso2"] == "UA"
    call(env, "/api/v1/events/models/event-studies", params={"window": "9,9"}, status=422)
    co = call(env, "/api/v1/events/models/company/{security_id}", path={"security_id": env["sid"]})
    assert co["exposure_score"] == 50.0 and co["top_countries"][1]["iso2"] == "CN" and len(co["history"]) == 3
    assert co["country_escalation"] and co["top_shocks"][0]["theme"] == "trade_tariffs"
    call(env, "/api/v1/events/models/company/{security_id}", path={"security_id": "SNOPE"}, status=404)
