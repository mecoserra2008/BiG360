"""FastAPI application (owner: api).

Run (from backend/):  ..\\.venv\\Scripts\\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

contracts/api.yaml is the source of truth and is served verbatim as /openapi.json (Swagger UI at /docs). Routers in
app/api/routers/ implement it; any contract path without an implementation is auto-registered as a 501 stub (hidden
from /docs) so the frontend always gets a consistent Problem response.

Middleware (outer -> inner): request context (X-Request-ID, metrics, JSON access log; app.api.observability) -> CORS
(allow-list, BIG360_CORS_ORIGINS) -> [bearer auth, only when BIG360_AUTH=oidc; app.api.auth] -> GZip (>= 2 KB) ->
snapshot response cache (ETag/Last-Modified). Errors are application/problem+json (contracts/api.yaml Problem).

Probes (O4): GET /health = liveness (200 while the process serves; status ok | degraded), GET /ready = readiness
(503 until a snapshot is published and the worker heartbeat is fresh), GET /metrics = Prometheus text format.

App mode (O3): with BIG360_SERVE_UI=1 the built frontend is served at / with SPA history fallback
(app.api.static_ui); /api/v1, /docs, /openapi.json and /health are unchanged.
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.deps import FastJSONResponse, SnapshotCacheMiddleware, dq_blocking_cached, peek_snapshot, prime
from app.api.routers import ALL_ROUTERS
from app.core.config import get_settings
from app.core.logsetup import get_logger, setup_logging

API_PREFIX = "/api/v1"
PROBLEM = "application/problem+json"
log = get_logger("app.api")


def _problem(status: int, title: str, detail: str | None = None, errors: list[Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def _contract_spec() -> dict[str, Any] | None:
    p = get_settings().contracts_dir / "api.yaml"
    if not p.exists():
        return None
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def _register_contract_stubs(app: FastAPI, spec: dict[str, Any] | None) -> int:
    """Add 501 handlers for api.yaml operations without an implementation."""
    if not spec:
        return 0
    implemented: set[tuple[str, str]] = set()
    for r in app.routes:  # app-level routes (FastAPI >= 0.13x keeps included routers as nested objects)
        for m in getattr(r, "methods", None) or set():
            implemented.add((re.sub(r":\w+}", "}", getattr(r, "path", "")), m))
    for router in ALL_ROUTERS:
        for r in router.routes:
            for m in getattr(r, "methods", None) or set():
                implemented.add((re.sub(r":\w+}", "}", API_PREFIX + r.path), m))
    n = 0
    for path, ops in spec.get("paths", {}).items():
        for method, op in ops.items():
            if method.upper() not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                continue
            if (path, method.upper()) in implemented:
                continue
            op_id = op.get("operationId", f"{method}_{path}")

            async def _stub(request: Request, _op: str = op_id) -> JSONResponse:
                return _problem(501, "Not implemented", f"operation {_op} is defined in contracts/api.yaml but not implemented yet")

            app.add_api_route(path, _stub, methods=[method.upper()], include_in_schema=False, name=f"stub_{op_id}")
            n += 1
    return n


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(log_file=settings.api_log_file or None, service="api")
    spec = _contract_spec()
    contract_version = str(((spec or {}).get("info") or {}).get("version") or "")
    app = FastAPI(title="BiG Equity 360 API", version=__version__, docs_url="/docs", openapi_url="/openapi.json",
                  default_response_class=FastJSONResponse, root_path=settings.root_path)
    app.add_middleware(SnapshotCacheMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=2048)
    from app.api.auth import install_auth
    from app.api.observability import RequestContextMiddleware, readiness, render_metrics

    install_auth(app, settings)  # no-op unless BIG360_AUTH=oidc
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_origin_regex=settings.cors_origin_regex,
                       allow_credentials=settings.cors_allow_credentials, allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["ETag", "Last-Modified", "X-Serving-Run-Id", "X-Cache", "X-Request-ID"])
    app.add_middleware(RequestContextMiddleware, access=settings.access_log)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return _problem(exc.status_code, _phrase(exc.status_code), detail if exc.status_code < 500 else "internal error")

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(_: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": [str(x) for x in e.get("loc", [])], "msg": e.get("msg", "")} for e in exc.errors()]
        return _problem(422, "Validation error", "request failed validation", errors)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _problem(500, "Internal Server Error", f"{type(exc).__name__}")

    @app.get("/health", tags=["system"], operation_id="getHealth")
    async def health() -> dict[str, Any]:
        # perf pass: constant-time liveness on the event loop (never queued behind the sync threadpool): no DuckDB
        # query, no wait on the snapshot-switch lock, dq flag from a background-refreshed cache
        snap = peek_snapshot()
        blocking = dq_blocking_cached()
        return {
            "status": "ok" if (not snap.is_empty and not blocking) else "degraded",
            "version": __version__,
            "time": datetime.now(UTC).isoformat(),
            "serving_run_id": None if snap.is_empty else snap.run_id,
            "serving_built_at": snap.built_at.isoformat() if snap.built_at else None,
        }

    @app.get("/ready", tags=["system"], operation_id="getReady")
    def ready() -> JSONResponse:
        ok, body = readiness(settings)
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/metrics", tags=["system"], operation_id="getMetrics")
    def metrics() -> PlainTextResponse:
        if not settings.metrics_enabled:
            return _problem(404, "Not Found", "metrics disabled (BIG360_METRICS=0)")  # type: ignore[return-value]
        return PlainTextResponse(render_metrics(settings, __version__, contract_version),
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    for router in ALL_ROUTERS:
        app.include_router(router, prefix=API_PREFIX)

    threading.Thread(target=prime, name="api-prime", daemon=True).start()
    app.state.n_stubs = _register_contract_stubs(app, spec)
    from app.api.static_ui import install_ui, serve_ui_enabled, ui_dir_from_env

    ui_dir = ui_dir_from_env(settings)
    if ui_dir is not None:
        install_ui(app, ui_dir, fallback=_http_exc)
        log.info("serving the built UI from %s at /", ui_dir)
    elif serve_ui_enabled():
        log.warning("BIG360_SERVE_UI=1 but no built UI found (BIG360_UI_DIR / app_dist / frontend/dist_app)")
    if spec is not None:
        app.openapi = lambda: spec  # type: ignore[method-assign]  # serve the contract itself
    from app.api.warmup import start_warmup

    start_warmup(app)  # perf pass: low-priority replay of the main pages' calls (off under pytest / BIG360_API_WARMUP=0)
    return app


app = create_app()
