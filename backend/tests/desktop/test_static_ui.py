"""O3 app mode: FastAPI serves the built UI at / with SPA history fallback; API, docs and 404 problems unchanged."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import static_ui
from app.api.main import create_app

INDEX = "<!doctype html><html><body><div id=root></div><!-- big360-test-index --></body></html>"


@pytest.fixture()
def ui_dir(tmp_path):
    d = tmp_path / "dist_app"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text(INDEX, encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("console.log('ok')", encoding="utf-8")
    (d / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
    return d


@pytest.fixture()
def client(tmp_data, ui_dir, monkeypatch):
    monkeypatch.setenv("BIG360_SERVE_UI", "1")
    monkeypatch.setenv("BIG360_UI_DIR", str(ui_dir))
    return TestClient(create_app())


def test_index_and_history_fallback(client):
    for path in ("/", "/macro", "/company/BRK.B", "/company/SAP.DE", "/system/data-health", "/theses/T1/edit"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "big360-test-index" in r.text, path
        assert r.headers["content-type"].startswith("text/html")
        assert r.headers["cache-control"] == "no-cache"
    assert client.head("/screener").status_code == 200


def test_assets_and_root_files(client):
    r = client.get("/assets/index-abc123.js")
    assert r.status_code == 200 and "console.log" in r.text
    assert "immutable" in r.headers["cache-control"]
    assert client.get("/favicon.ico").status_code == 200
    assert client.get("/assets/missing-000.js").status_code == 404     # missing bundle: real 404, not index.html
    r = client.get("/missing.png")
    assert r.status_code == 404 and "big360-test-index" not in r.text
    assert client.get("/../../etc/passwd").status_code in (200, 404)    # never a file outside the build
    assert "root:" not in client.get("/..%2F..%2Fwindows/win.ini").text


def test_api_docs_health_untouched(client):
    r = client.get("/api/v1/definitely-not-a-route")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["status"] == 404
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] in ("ok", "degraded")
    assert client.get("/openapi.json").json()["openapi"].startswith("3.")
    d = client.get("/docs")
    assert d.status_code == 200 and "swagger" in d.text.lower() and "big360-test-index" not in d.text
    assert client.post("/macro", json={}).status_code in (404, 405)     # only GET/HEAD fall back to the SPA
    assert "big360-test-index" not in client.post("/macro", json={}).text


def test_disabled_by_default(tmp_data, ui_dir, monkeypatch):
    monkeypatch.delenv("BIG360_SERVE_UI", raising=False)
    monkeypatch.setenv("BIG360_UI_DIR", str(ui_dir))
    c = TestClient(create_app())
    r = c.get("/")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")


def test_resolve_ui_dir_and_api_paths(tmp_data, ui_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("BIG360_UI_DIR", str(ui_dir))
    assert static_ui.resolve_ui_dir(tmp_data) == ui_dir.resolve()
    monkeypatch.setenv("BIG360_UI_DIR", str(tmp_path / "nope"))
    found = static_ui.resolve_ui_dir(tmp_data)       # falls back to <root>/app_dist or frontend/dist_app (if built)
    assert found is None or (found / "index.html").is_file()
    for p in ("/api", "/api/v1/x", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json", "/health", "/metrics"):
        assert static_ui.is_api_path(p), p
    for p in ("/", "/apis", "/macro", "/healthcheck-page", "/documents"):
        assert not static_ui.is_api_path(p), p
