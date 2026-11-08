"""O2.3 regression guard (app.qa): plausibility rules, schema checks, id resolution, golden evaluation, smoke judging,
state / ops alert. No network, no browser."""

from __future__ import annotations

import math
from datetime import date

from app.qa import api_sweep as A
from app.qa import golden as G
from app.qa import smoke as S
from app.qa.common import Resp, find_key, non_finite, overall_status, qa_alert, read_state, write_state

TODAY = date(2026, 10, 20)


class FakeClient:
    """GET-only stand-in for ApiClient: path -> (status, json)."""

    mode = "fake"
    base_url = None

    def __init__(self, routes: dict[str, tuple[int, object]]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, path: str, params=None) -> Resp:  # noqa: ANN001
        import json

        self.calls.append(path)
        st, body = self.routes.get(path, (404, {"detail": "nope"}))
        return Resp(st, "application/json", json.dumps(body), 1.0)


def test_future_dates_only_flag_as_of_keys():
    doc = {"as_of": "2026-10-19", "items": [{"px_date": "2026-11-30", "expected_date": "2027-01-15"}],
           "meta": {"data_as_of": "2026-10-21"}}
    out = A.future_dates(doc, TODAY)
    assert out == ["$.items[0].px_date=2026-11-30"]  # +1 day tolerance; forward-looking keys ignored


def test_non_finite_and_find_key():
    assert non_finite({"a": [1.0, math.nan], "b": {"c": math.inf}}) == ["$.a[1]", "$.b.c"]
    assert find_key({"rows": [{"x": None}, {"run_id": "R1"}]}, ("run_id",)) == "R1"


def test_schema_errors_resolve_components():
    spec = {"components": {"schemas": {"Row": {"type": "object", "required": ["id"],
                                               "properties": {"id": {"type": "string"}, "v": {"type": ["number", "null"]}}}}}}
    schema = {"type": "array", "items": {"$ref": "#/components/schemas/Row"}}
    assert A.schema_errors(spec, schema, [{"id": "a", "v": None}]) == []
    errs = A.schema_errors(spec, schema, [{"v": "x"}])
    assert any("'id' is a required property" in e for e in errs) and any("$[0].v" in e for e in errs)


def test_check_operation_resolves_ids_and_validates():
    spec = {"components": {"schemas": {}}, "paths": {}}
    client = FakeClient({
        "/api/v1/search": (200, [{"security_id": "S1", "ticker": "AAPL"}]),
        "/api/v1/companies/S1/overview": (200, {"as_of": "2026-10-19", "data_status": "ok", "x": 1}),
        "/api/v1/backtest/runs": (200, [{"run_id": "BR1", "status": "cancelled"}, {"run_id": "BR2", "status": "completed"}]),
        "/api/v1/backtest/runs/BR2": (500, {"detail": "boom"}),
    })
    res = A.Resolver(client)
    op = {"path": "/api/v1/companies/{security_id}/overview", "operation_id": "getOverview", "content_type": "application/json",
          "params": [{"name": "security_id", "in": "path", "required": True}], "schema": {"type": "object", "required": ["x"]}}
    r = A.check_operation(client, spec, op, res, TODAY)
    assert r.status == "passed" and r.url == "/api/v1/companies/S1/overview"
    bt = {"path": "/api/v1/backtest/runs/{run_id}", "operation_id": "backtestGetRun", "content_type": "application/json",
          "params": [{"name": "run_id", "in": "path", "required": True}], "schema": {"type": "object"}}
    r2 = A.check_operation(client, spec, bt, res, TODAY)
    assert r2.status == "failed" and r2.url.endswith("/BR2") and "HTTP 500" in r2.issues[0]  # completed run preferred
    none = {"path": "/api/v1/theses/{thesis_id}", "operation_id": "getThesis", "content_type": "application/json",
            "params": [{"name": "thesis_id", "in": "path", "required": True}], "schema": {"type": "object"}}
    assert A.check_operation(client, spec, none, res, TODAY).status == "skipped"


def test_golden_evaluate_value_range_and_age():
    c = {"id": "x", "expect": {"value": 100.0, "rel_tol": 0.01}}
    assert G.evaluate(c, 100.5, None, TODAY)[0] == "passed"
    assert G.evaluate(c, 103.0, None, TODAY)[0] == "failed"
    assert G.evaluate({**c, "severity": "warn"}, 103.0, None, TODAY)[0] == "warn"
    rng = {"id": "y", "expect": {"min": 1, "max": 7}, "max_age_days": 14}
    assert G.evaluate(rng, 4.2, date(2026, 10, 15), TODAY)[0] == "passed"
    assert G.evaluate(rng, 4.2, date(2026, 9, 1), TODAY)[0] == "warn"
    assert G.evaluate(rng, 9.0, date(2026, 10, 15), TODAY)[0] == "failed"
    assert G.evaluate(rng, None, None, TODAY)[0] == "failed"


def test_golden_file_is_well_formed():
    checks = G.load_checks()
    assert len(checks) >= 8 and len({c["id"] for c in checks}) == len(checks)
    for c in checks:
        assert c["kind"] in ("fin_line", "overview", "index_last", "macro_last", "macro_yoy")
        e = c["expect"]
        assert ("value" in e) or ("min" in e and "max" in e)
        float(e.get("value", e.get("min")))  # numeric (YAML 1.1 parses 7.0e9 as a string)


def test_golden_fin_line_reads_the_fiscal_year():
    client = FakeClient({
        "/api/v1/search": (200, [{"security_id": "S1", "ticker": "AAPL"}]),
        "/api/v1/companies/S1/financials": (200, {"periods": [{"fiscal_year": 2023, "fiscal_period": "FY", "period_end": "2023-09-30"},
                                                              {"fiscal_year": 2024, "fiscal_period": "FY", "period_end": "2024-09-28"}],
                                                  "lines": [{"item_id": "revenue", "values": [383285e6, 391035e6]}]}),
    })
    out = G.run_golden(client, [{"id": "rev", "kind": "fin_line", "ticker": "AAPL", "item": "revenue", "fiscal_year": 2024,
                                 "expect": {"value": 391035000000, "rel_tol": 0.005}}])
    assert out["status"] == "passed" and out["results"][0]["observation_date"] == "2024-09-28"


def test_smoke_routes_and_judge(tmp_path):
    app = tmp_path / "App.tsx"
    app.write_text("""children: [
      { index: true, element: <Navigate to="/macro" replace /> },
      { path: 'market', element: <Navigate to="/macro/markets" replace /> },
      { path: 'macro', element: <MacroCockpit /> },
      { path: 'company/:id', element: <Company360 /> },
      { path: 'theses/:id', element: <ThesisPage /> },
      { path: '*', element: <NotFound /> },""", encoding="utf-8")
    pats = S.app_routes(app)
    assert pats == ["/macro", "/company/:id", "/theses/:id"]
    cfg = {"params": {"/company/:id": {"fixed": "AAPL"}, "/theses/:id": {"api": "/api/v1/theses", "keys": ["thesis_id"]}},
           "extra_routes": ["/company/7203.T"], "allow_empty": ["/watchlist"], "allow_empty_text": ["No disclosed (customers|suppliers)"],
           "max_load_ms": 8000}
    routes, skipped = S.resolve_routes(pats, cfg, FakeClient({"/api/v1/theses": (200, [])}))
    assert routes == ["/macro", "/company/AAPL", "/company/7203.T"] and skipped[0]["route"] == "/theses/:id"
    st = S.PageState()
    ok = {"text": "Macro", "sw": 1440, "cw": 1440, "msw": 1200, "mcw": 1200}
    assert S.judge("/macro", ok, st, 2000, cfg) == ("passed", [])
    status, issues = S.judge("/macro", {**ok, "empties": ["Network :: No disclosed customers", "Peers :: No data"]}, st, 2000, cfg)
    assert status == "failed" and issues == ["empty panel: Peers :: No data"]
    assert S.judge("/watchlist", {**ok, "empties": ["No data"]}, st, 2000, cfg)[0] == "passed"
    assert S.judge("/macro", ok, st, 9000, cfg)[0] == "failed"
    assert S.judge("/macro", {**ok, "msw": 1236, "mcw": 1230}, st, 100, cfg)[1] == ["horizontal overflow (main 1236 > 1230 px)"]
    assert S.judge("/macro", {**ok, "text": ""}, st, 100, cfg)[1][0].startswith("blank page")
    st.console.append("console.error: boom")
    assert S.judge("/macro", ok, st, 100, cfg)[0] == "failed"
    assert S.judge("/macro", {**ok, "pending": ["x"]}, S.PageState(), 100, cfg)[0] == "warn"


def test_read_only_post_allowlist():
    assert S.READ_ONLY_POSTS.search("/api/v1/screener/query")
    assert S.READ_ONLY_POSTS.search("/api/v1/macro/v4/scenarios/SC1/run")
    assert not S.READ_ONLY_POSTS.search("/api/v1/watchlists")
    assert not S.READ_ONLY_POSTS.search("/api/v1/alerts/events/mark-read")


def test_state_and_ops_alert(tmp_data):
    assert read_state(tmp_data) is None and qa_alert(tmp_data) is None
    write_state({"status": "passed", "suites": {}, "finished_at": "2026-10-20T01:00:00+00:00"}, tmp_data)
    assert qa_alert(tmp_data) is None
    write_state({"status": "failed", "finished_at": "2026-10-20T01:00:00+00:00",
                 "suites": {"api": {"status": "failed", "failed": 3}, "golden": {"status": "passed", "failed": 0}}}, tmp_data)
    from datetime import UTC, datetime

    # 2026-10-30: results age out (tests/ops/test_alert_hygiene.py) - judged one hour after the run here
    a = qa_alert(tmp_data, now=datetime(2026, 10, 20, 2, tzinfo=UTC))
    assert a["id"] == "qa_regression" and a["severity"] == "error" and "api: failed (3 failed, 1.0 h ago)" in a["detail"]
    assert qa_alert(tmp_data, now=datetime(2026, 10, 30, 2, tzinfo=UTC))["severity"] == "info"   # 10 days later: stale
    assert overall_status(["passed", "skipped"]) == "passed"
    assert overall_status(["skipped", "skipped"]) == "skipped"
    assert overall_status(["passed", "warn"]) == "warn" and overall_status(["warn", "failed"]) == "failed"


def test_backtest_series_thinning():
    from app.api.routers.backtest import thin_series

    sr = {"dates": ["2026-01-05", "2026-01-06", "2026-01-09", "2026-01-12", "2026-01-13"], "nav": [1, 2, 3, 4, 5],
          "drawdown": [0, 0, 0, 0, 0], "bench_nav": None}
    wk = thin_series(sr, "weekly")
    assert wk["dates"] == ["2026-01-09", "2026-01-13"] and wk["nav"] == [3, 5] and wk["bench_nav"] is None
    no = thin_series(sr, "none")
    assert no["dates"] == [] and no["nav"] == [] and no["bench_nav"] is None


def test_f14_last_run_failed_rule():
    from datetime import UTC, datetime

    from app.api.routers.system import _last_run_failed

    t = lambda h: datetime(2026, 10, 20, h, tzinfo=UTC)  # noqa: E731
    ok = {"status": "success", "started_at": t(1), "finished_at": t(2)}
    assert _last_run_failed({"status": "failed", "started_at": t(3)}, ok) is True
    assert _last_run_failed({"status": "failed", "started_at": t(0)}, ok) is False
    assert _last_run_failed({"status": "failed", "started_at": t(3)}, None) is True
    assert _last_run_failed(ok, ok) is False and _last_run_failed(None, ok) is False
