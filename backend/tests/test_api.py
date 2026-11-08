from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import create_app


def test_health_and_meta(tmp_data):
    client = TestClient(create_app())
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] in ("ok", "degraded")
    tax = client.get("/api/v1/meta/taxonomy").json()
    assert len(tax["sectors"]) == 11
    assert all(sub["kpi_set_id"] for s in tax["sectors"] for sub in s["subsectors"])
    fac = client.get("/api/v1/meta/factors").json()
    assert fac["default_profile"] == "balanced"
    status = client.get("/api/v1/system/status").json()
    assert set(status["keys_present"]) >= {"fred", "polygon"}
    assert all(isinstance(v, bool) for v in status["keys_present"].values())


def test_all_contract_paths_implemented_and_problem_errors(tmp_data):
    app = create_app()
    assert app.state.n_stubs == 0  # every contracts/api.yaml operation has a router implementation
    client = TestClient(app)
    r = client.get("/api/v1/companies/SABC/overview")  # unknown security -> 404 Problem (no snapshot yet: empty)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 404
    assert client.get("/api/v1/does-not-exist").status_code == 404
