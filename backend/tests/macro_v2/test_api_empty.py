"""macro v2 W1 endpoints answer with their contract shape (data_status pending) before any macro v2 data exists."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.deps import snapshots
from tests.api_contract import validate

PATHS = [("/api/v1/macro/v2/heatmap", "/api/v1/macro/v2/heatmap"), ("/api/v1/macro/v2/cycle", "/api/v1/macro/v2/cycle"),
         ("/api/v1/macro/v2/countries", "/api/v1/macro/v2/countries"),
         ("/api/v1/macro/v2/countries/DE", "/api/v1/macro/v2/countries/{cc}"),
         ("/api/v1/macro/v2/themes/inflation", "/api/v1/macro/v2/themes/{theme}"),
         ("/api/v1/macro/v2/themes/rates", "/api/v1/macro/v2/themes/{theme}"),
         ("/api/v1/macro/v2/compare?indicator=cpi_yoy", "/api/v1/macro/v2/compare")]


def test_empty_snapshot_shapes(tmp_data) -> None:  # noqa: ANN001
    snapshots.reset()
    from app.api.main import create_app

    c = TestClient(create_app())
    for url, tpl in PATHS:
        r = c.get(url)
        assert r.status_code == 200, (url, r.text[:300])
        body = r.json()
        validate("GET", tpl, 200, body)
        assert body["data_status"] == "pending", url
    assert c.get("/api/v1/macro/v2/series/fred%3ADGS10").status_code == 404
    snapshots.reset()
