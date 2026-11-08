"""Perf pass: API warm-up request list + runner (app.api.warmup)."""

from __future__ import annotations

import asyncio
import re
from datetime import date

import yaml
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.api import warmup
from app.core.config import get_settings


def _templates() -> list[tuple[re.Pattern[str], str]]:
    spec = yaml.safe_load((get_settings().contracts_dir / "api.yaml").read_text(encoding="utf-8"))
    out = []
    for p, ops in spec["paths"].items():
        rx = re.compile("^" + re.sub(r"\\{[^}]+\\}", "[^/]+", re.escape(p)) + "$")
        for m in ops:
            out.append((rx, m.upper()))
    return out


def test_every_warm_request_is_a_contract_operation():
    tpls = _templates()
    reqs = warmup.warm_requests(date(2026, 10, 20))
    assert len(reqs) > 60
    for method, url, body in reqs:
        path = "/api/v1" + url.split("?", 1)[0]
        assert any(rx.match(path) and m == method for rx, m in tpls), f"{method} {path} not in contracts/api.yaml"
        assert (body is not None) == (method == "POST")
    # shell first, then the landing page
    assert reqs[0] == ("GET", "/watchlists", None) and reqs[1][1] == "/system/status"


def test_dates_follow_the_ui_rules():
    urls = [u for _, u, _ in warmup.warm_requests(date(2026, 10, 20))]
    assert "/macro/v2/calendar?from=2026-10-20&to=2026-12-04" in urls          # isoDaysFromNow(0..45)
    assert "/positioning/markets/067651?combined=false&from=2021-10-20" in urls  # rangeStart('5Y')
    assert any(u.startswith("/earnings/calendar?from=2026-10-19&to=2026-10-25") for u in urls)  # ISO week
    leap = [u for _, u, _ in warmup.warm_requests(date(2028, 2, 29))]
    assert "/positioning/markets/067651?combined=false&from=2023-03-01" in leap  # JS rolls 29 Feb over


def test_disabled_under_pytest(monkeypatch):
    monkeypatch.delenv("BIG360_API_WARMUP", raising=False)
    assert warmup.enabled() is False
    monkeypatch.setenv("BIG360_API_WARMUP", "0")
    assert warmup.enabled() is False


def _app(calls: list[str], status: int = 200) -> FastAPI:
    app = FastAPI()

    @app.api_route("/api/v1/{rest:path}", methods=["GET", "POST"])
    async def any_(rest: str) -> JSONResponse:
        calls.append(rest)
        if rest == "watchlists":
            return JSONResponse([{"watchlist_id": "W1"}])
        return JSONResponse({"ok": True}, status_code=status)

    return app


def test_run_once_is_sequential_adds_the_first_watchlist_and_stops_on_auth():
    calls: list[str] = []
    reqs = [("GET", "/watchlists", None), ("GET", "/system/status", None), ("POST", "/screener/query", "{}")]
    res = asyncio.run(warmup.run_once(_app(calls), requests=reqs))
    assert calls == ["watchlists", "watchlists/W1", "system/status", "screener/query"]
    assert res["ok"] == 4 and res["failed"] == 0 and res["stopped"] is None

    calls2: list[str] = []
    res2 = asyncio.run(warmup.run_once(_app(calls2, status=401), requests=reqs[1:]))
    assert calls2 == ["system/status"] and res2["stopped"] == "auth required"


def test_run_once_respects_the_budget():
    calls: list[str] = []
    res = asyncio.run(warmup.run_once(_app(calls), budget_s=-1.0, requests=[("GET", "/a", None)]))
    assert calls == [] and res["stopped"] == "budget"


def test_serving_connection_limits(monkeypatch):
    import duckdb

    from app.api import deps
    from types import SimpleNamespace as Settings

    con = duckdb.connect(":memory:")
    deps._apply_limits(con, Settings(api_duckdb_memory_limit="1GB", duckdb_threads=16))
    mem, thr = con.execute("SELECT current_setting('memory_limit'), current_setting('threads')").fetchone()
    assert thr == 8 and mem.replace(" ", "") in ("1.0GiB", "953.6MiB", "1GB", "1.0GB")
    con2 = duckdb.connect(":memory:")
    before = con2.execute("SELECT current_setting('memory_limit')").fetchone()[0]
    deps._apply_limits(con2, Settings(api_duckdb_memory_limit="1GB; DROP", duckdb_threads=4))  # rejected
    assert con2.execute("SELECT current_setting('memory_limit')").fetchone()[0] == before


def test_warm_imports_resolve():
    import importlib.util

    for m in warmup.WARM_IMPORTS:
        assert importlib.util.find_spec(m) is not None, m
