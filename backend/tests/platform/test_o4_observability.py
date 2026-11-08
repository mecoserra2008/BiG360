"""O4: JSON log fields (request id, service, extra fields), metrics registry and route labels."""

from __future__ import annotations

import json
import logging

from app.api import observability as obs
from app.core import logsetup


def test_json_formatter_request_id_and_fields(monkeypatch):
    monkeypatch.setattr(logsetup, "_service", "api")
    tok = logsetup.request_id_var.set("rid-123")
    try:
        rec = logging.LogRecord("app.api.access", logging.INFO, __file__, 1, "GET %s", ("/x",), None)
        rec.fields = {"status": 200, "route": "/x", "msg": "must not override"}
        out = json.loads(logsetup.JsonFormatter().format(rec))
    finally:
        logsetup.request_id_var.reset(tok)
    assert out["request_id"] == "rid-123" and out["service"] == "api"
    assert out["status"] == 200 and out["route"] == "/x" and out["msg"] == "GET /x"
    rec2 = logging.LogRecord("x", logging.INFO, __file__, 1, "plain", (), None)
    assert "request_id" not in json.loads(logsetup.JsonFormatter().format(rec2))


def test_registry_histogram():
    m = obs.MetricsRegistry()
    m.observe("GET", "/a", 200, 0.004)
    m.observe("GET", "/a", 200, 0.3)
    m.observe("GET", "/a", 500, None)  # counted, not timed (SSE)
    req, hist, *_ = m.snapshot()
    assert req[("GET", "/a", "200")] == 2 and req[("GET", "/a", "500")] == 1
    h = hist[("GET", "/a")]
    i005 = obs.LATENCY_BUCKETS.index(0.005)
    i05 = obs.LATENCY_BUCKETS.index(0.5)
    assert h[i005] == 1 and h[i05] == 2 and h[len(obs.LATENCY_BUCKETS)] == 2
    assert abs(h[len(obs.LATENCY_BUCKETS) + 1] - 0.304) < 1e-9


def test_route_label_bounded():
    assert obs.route_label({"path": "/api/v1/companies/SABC/overview"}, 200) == "/api/v1/companies/{security_id}/overview"
    assert obs.route_label({"path": "/api/v1/sectors"}, 200) == "/api/v1/sectors"
    assert obs.route_label({"path": "/api/v1/does-not-exist/123"}, 404) == "unmatched"
    assert obs.route_label({"path": "/assets/index-abc.js"}, 200) == "static"


def test_label_escaping_and_numbers():
    assert obs._labels(a='x"y\\z\n') == '{a="x\\"y\\\\z\\n"}'
    assert obs._num(None) == "NaN" and obs._num(float("inf")) == "+Inf" and obs._num(3.0) == "3" and obs._num(0.25) == "0.25"
