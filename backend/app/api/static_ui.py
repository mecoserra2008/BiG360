"""Built frontend served by FastAPI (O3 app mode: no Node at runtime).

Enabled only when BIG360_SERVE_UI=1 (the supervisor sets it for the API child with --serve-ui / --app), so the dev
workflow (uvicorn without the flag + Vite on :5173) is unchanged.

    BIG360_SERVE_UI=1                 serve the UI at /
    BIG360_UI_DIR=<dir>               built UI (index.html + assets/); default: <root>/app_dist (installed app), then
                                      <root>/frontend/dist_app (`npm run build:app`)

Routing: /api/v1, /docs, /redoc, /openapi.json, /health and /metrics are untouched. /assets/* are the hashed bundles
(immutable cache). Any other GET/HEAD that no route answers (404) returns a file of the build root when it exists
(favicon.ico, icons) or index.html (SPA history fallback, e.g. /company/BRK.B or /system/data-health). Missing static
files (known extensions) stay a 404; every 404 under the API prefixes keeps the problem+json body.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import FileResponse, Response
from starlette.staticfiles import StaticFiles

from app.core.config import Settings, get_settings

API_PREFIXES = ("/api", "/docs", "/redoc", "/health", "/metrics")
API_FILES = ("/openapi.json",)
STATIC_EXT = {"js", "mjs", "css", "map", "png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "woff", "woff2", "ttf",
              "otf", "eot", "json", "txt", "webmanifest", "xml", "wasm", "mp4", "pdf"}
TRUE = {"1", "true", "yes", "on"}


def serve_ui_enabled() -> bool:
    return os.environ.get("BIG360_SERVE_UI", "").strip().lower() in TRUE


def resolve_ui_dir(settings: Settings | None = None) -> Path | None:
    """First directory with an index.html among BIG360_UI_DIR, <root>/app_dist, <root>/frontend/dist_app."""
    s = settings or get_settings()
    env = os.environ.get("BIG360_UI_DIR", "").strip()
    cands = [Path(env)] if env else []
    cands += [s.project_root / "app_dist", s.project_root / "frontend" / "dist_app"]
    for c in cands:
        if (c / "index.html").is_file():
            return c.resolve()
    return None


def ui_dir_from_env(settings: Settings | None = None) -> Path | None:
    return resolve_ui_dir(settings) if serve_ui_enabled() else None


def is_api_path(path: str) -> bool:
    if path in API_FILES:
        return True
    return any(path == p or path.startswith(p + "/") for p in API_PREFIXES)


class _ImmutableStatic(StaticFiles):
    """Vite emits content-hashed file names under assets/: cache them for a year."""

    async def get_response(self, path: str, scope: Any) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def spa_response(request: Request, ui_dir: Path) -> Response | None:
    """Response for a request no route matched, or None (= keep the 404)."""
    if request.method not in ("GET", "HEAD"):
        return None
    path = request.url.path
    if is_api_path(path):
        return None
    rel = path.lstrip("/")
    if rel:
        try:
            f = (ui_dir / rel).resolve()
        except (OSError, ValueError):
            return None
        if f.is_relative_to(ui_dir) and f.is_file():
            return FileResponse(f, headers={"Cache-Control": "public, max-age=3600"})
        last = rel.rsplit("/", 1)[-1]
        if "." in last and last.rsplit(".", 1)[-1].lower() in STATIC_EXT:
            return None  # a missing static file is a real 404 (not index.html with a wrong MIME type)
    return FileResponse(ui_dir / "index.html", media_type="text/html", headers={"Cache-Control": "no-cache"})


def install_ui(app: FastAPI, ui_dir: Path,
               fallback: Callable[[Request, Exception], Awaitable[Response]]) -> None:
    """Mount /assets and route unmatched GETs to the SPA. `fallback` renders every other 404 (problem+json)."""
    ui_dir = ui_dir.resolve()
    assets = ui_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", _ImmutableStatic(directory=str(assets)), name="ui-assets")

    async def _not_found(request: Request, exc: Exception) -> Response:
        resp = spa_response(request, ui_dir)
        if resp is not None:
            return resp
        return await fallback(request, exc)

    app.add_exception_handler(404, _not_found)
    app.state.ui_dir = str(ui_dir)
