"""Shared API dependencies (owner: api).

* `Snapshot` — the live serving snapshot: one read-only DuckDB connection to the file named in data/db/CURRENT, plus
  its metadata (run id, built_at, as_of, which lake inputs were empty). CURRENT is re-checked at most once per second;
  when it changes a new connection is opened and the old one is closed after a grace period (in-flight cursors finish).
  Before the first build an in-memory *empty* snapshot (all serving tables, zero rows) is served so every endpoint
  answers with its contract shape and `data_status: "pending"`.
* `get_db()` — FastAPI dependency yielding a `Db` (per-request cursor + query helpers + snapshot info).
* `SnapshotCacheMiddleware` — ASGI middleware: in-memory LRU response cache keyed by (snapshot id, method, path,
  query, body) for snapshot-backed endpoints; adds ETag / Last-Modified / X-Serving-Run-Id and answers 304 on
  If-None-Match. Mutable endpoints (system, watchlists, alerts, model train/promote) are never cached.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import duckdb

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import current_serving_db

log = get_logger("app.api.deps")

CHECK_INTERVAL_S = 1.0
GRACE_S = 120.0


# ------------------------------------------------------------------------------------------------ snapshot


@dataclass
class Snapshot:
    run_id: str
    path: Path | None
    con: duckdb.DuckDBPyConnection
    built_at: datetime | None
    as_of: date
    inputs: dict[str, tuple[int, str]] = field(default_factory=dict)
    opened_at: float = field(default_factory=time.monotonic)

    @property
    def is_empty(self) -> bool:
        return self.path is None

    def input_ok(self, table: str) -> bool:
        return self.inputs.get(table, (0, "empty"))[1] == "ok"

    def status(self, *tables: str) -> str:
        """'ok' if every input table has data, 'pending' if none has, else 'partial'."""
        oks = [self.input_ok(t) for t in tables]
        if not oks or all(oks):
            return "ok"
        return "pending" if not any(oks) else "partial"

    @property
    def last_modified(self) -> str:
        return format_datetime(self.built_at or datetime.now(UTC), usegmt=True)


def _load_meta(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, str], dict[str, tuple[int, str]]]:
    meta = dict(con.execute("SELECT key, value FROM _meta").fetchall())
    inputs = {r[0]: (int(r[1]), r[2]) for r in con.execute("SELECT table_name, row_count, status FROM _inputs").fetchall()}
    return meta, inputs


_MEM_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*$", re.IGNORECASE)


def _apply_limits(con: duckdb.DuckDBPyConnection, settings: Settings) -> None:
    """Perf pass: bounded DuckDB worker pool and memory (the buffer pool keeps snapshot blocks it has read; without a
    cap it defaults to 80 % of RAM, and the old full-table warm-up kept the API at ~7 GB). BIG360_API_DUCKDB_MEMORY
    (default 4GB; empty / 0 = DuckDB default)."""
    con.execute(f"SET threads = {max(2, min(8, int(settings.duckdb_threads)))}")
    mem = str(getattr(settings, "api_duckdb_memory_limit", "") or "").strip()
    if mem and mem != "0":
        if _MEM_RE.match(mem):
            con.execute(f"SET memory_limit = '{mem}'")
        else:
            log.warning("ignoring BIG360_API_DUCKDB_MEMORY=%r (expected e.g. 4GB)", mem)


def _open_snapshot(path: Path | None, settings: Settings) -> Snapshot:
    if path is None:
        from app.api.serving import empty_snapshot

        con = empty_snapshot(settings)
        con.execute("SET TimeZone = 'UTC'")
        meta, inputs = _load_meta(con)
        return Snapshot("empty", None, con, None, date.fromisoformat(meta["as_of"]), inputs)
    con = duckdb.connect(str(path), read_only=True)
    con.execute("SET TimeZone = 'UTC'")
    _apply_limits(con, settings)
    meta, inputs = _load_meta(con)
    built = datetime.fromisoformat(meta["built_at"]) if meta.get("built_at") else None
    # warm-up: app.api.warmup replays the main pages' calls (targeted, low priority) after open / switch
    return Snapshot(meta.get("run_id", path.stem.removeprefix("serving_")), path, con, built,
                    date.fromisoformat(meta["as_of"]), inputs)


class _SnapshotManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None
        self._key: tuple[str, str] | None = None   # (data_dir, current file name)
        self._checked = 0.0
        self._retired: list[tuple[float, duckdb.DuckDBPyConnection]] = []

    def get(self, wait: bool = True) -> Snapshot:
        """Current snapshot. wait=False (liveness probe): never blocks on the switch lock; while another thread is
        opening a new snapshot the previous one is returned."""
        now = time.monotonic()
        snap = self._snap
        if snap is not None and now - self._checked < CHECK_INTERVAL_S:
            return snap
        s = get_settings()
        p = current_serving_db(s)
        key = (str(s.data_dir), p.name if p else "")
        if snap is not None and key == self._key:
            self._checked = now
            return snap
        if not wait and snap is not None:
            if not self._lock.acquire(blocking=False):
                return snap
            self._lock.release()
        with self._lock:
            if self._snap is not None and key == self._key:
                self._checked = now
                return self._snap
            try:
                new = _open_snapshot(p, s)
            except Exception as e:  # corrupt / half-deleted file: keep serving the previous snapshot
                log.error("cannot open serving snapshot %s: %s", p, e)
                if self._snap is not None:
                    self._checked = now
                    return self._snap
                new = _open_snapshot(None, s)
                key = (str(s.data_dir), "")
            if self._snap is not None:
                self._retired.append((now, self._snap.con))
                log.info("serving snapshot switched %s -> %s", self._snap.run_id, new.run_id)
            self._snap, self._key, self._checked = new, key, now
            self._close_retired(now)
            return new

    def _close_retired(self, now: float) -> None:
        keep = []
        for t, con in self._retired:
            if now - t > GRACE_S:
                try:
                    con.close()
                except Exception:
                    pass
            else:
                keep.append((t, con))
        self._retired = keep

    def reset(self) -> None:
        """Tests: drop all connections (so temp dirs can be deleted)."""
        with self._lock:
            for _, con in self._retired:
                try:
                    con.close()
                except Exception:
                    pass
            if self._snap is not None:
                try:
                    self._snap.con.close()
                except Exception:
                    pass
            self._snap, self._key, self._retired, self._checked = None, None, [], 0.0
        response_cache.clear()


snapshots = _SnapshotManager()


def current_snapshot() -> Snapshot:
    return snapshots.get()


def peek_snapshot() -> Snapshot:
    """Liveness-probe variant of current_snapshot(): does not wait while a snapshot switch holds the lock."""
    return snapshots.get(wait=False)


DQ_TTL_S = 30.0
_dq_state: dict[str, Any] = {"at": float("-inf"), "blocking": False, "running": False}
_dq_lock = threading.Lock()


def _refresh_dq() -> None:
    try:
        from app.core.dq import dq_summary

        val = bool(dq_summary().get("blocking"))
    except Exception:  # noqa: BLE001
        val = None
    with _dq_lock:
        if val is not None:
            _dq_state["blocking"] = val
        _dq_state["at"] = time.monotonic()
        _dq_state["running"] = False


def dq_blocking_cached() -> bool:
    """Last known dq `blocking` flag for /health, never computed on the caller's thread: dq_summary() reads the
    whole dq_results table, so it is refreshed in a background thread at most every DQ_TTL_S (False until known)."""
    now = time.monotonic()
    with _dq_lock:
        start = now - _dq_state["at"] > DQ_TTL_S and not _dq_state["running"]
        if start:
            _dq_state["running"] = True
        val = bool(_dq_state["blocking"])
    if start:
        threading.Thread(target=_refresh_dq, name="health-dq", daemon=True).start()
    return val


def prime() -> None:
    """Pay one-time costs before the first request: DuckDB's first parameterized query in a process lazily imports
    numpy/pandas (~2 s on Windows), and the snapshot is opened. The page-level warm-up is app.api.warmup."""
    try:
        duckdb.connect(":memory:").execute("SELECT ?::INTEGER", [1]).fetchall()
        current_snapshot()
    except Exception as e:  # pragma: no cover
        log.info("API prime skipped: %s", e)
    try:  # roadmap v3 N4: open the intraday news DuckDB before the first News Terminal request (first paint < 1 s)
        from app.api.intraday import manager

        manager.get()
    except Exception as e:  # pragma: no cover
        log.info("intraday warm-up skipped: %s", e)
    try:  # W4.7: macro catalog search (Studio variable catalog) warm before the first request
        from app.modeling.datasets import build_catalog

        if not current_snapshot().is_empty:
            build_catalog()
    except Exception as e:  # pragma: no cover
        log.info("catalog warm-up skipped: %s", e)


# ------------------------------------------------------------------------------------------------ query helper


def _clean(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


class Db:
    """Per-request handle: a cursor on the snapshot connection plus small query helpers (rows as dicts)."""

    def __init__(self, snap: Snapshot):
        self.snap = snap
        self.cur = snap.con.cursor()
        self.cur.execute("SET TimeZone = 'UTC'")

    @property
    def as_of(self) -> date:
        return self.snap.as_of

    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        self.cur.execute(sql, list(params or []))
        cols = [d[0] for d in self.cur.description]
        return [{c: _clean(v) for c, v in zip(cols, row, strict=True)} for row in self.cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        self.cur.execute(sql, list(params or []))
        r = self.cur.fetchone()
        return _clean(r[0]) if r else None

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self.cur.execute(f'DESCRIBE "{table}"').fetchall()]

    def close(self) -> None:
        try:
            self.cur.close()
        except Exception:
            pass


def get_db() -> Iterator[Db]:
    """FastAPI dependency."""
    db = Db(current_snapshot())
    try:
        yield db
    finally:
        db.close()


# ------------------------------------------------------------------------------------------------ response cache


class _LRU:
    def __init__(self, max_items: int = 1024, max_bytes: int = 256 * 1024 * 1024, ttl_s: float = 600.0):
        self.max_items, self.max_bytes, self.ttl_s = max_items, max_bytes, ttl_s
        self._d: OrderedDict[str, tuple[float, int, list[tuple[bytes, bytes]], bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: str) -> tuple[int, list[tuple[bytes, bytes]], bytes] | None:
        with self._lock:
            v = self._d.get(key)
            if v is None or time.monotonic() - v[0] > self.ttl_s:
                if v is not None:
                    self._drop(key)
                self.misses += 1
                return None
            self._d.move_to_end(key)
            self.hits += 1
            return v[1], v[2], v[3]

    def put(self, key: str, status: int, headers: list[tuple[bytes, bytes]], body: bytes) -> None:
        if len(body) > self.max_bytes // 8:
            return
        with self._lock:
            if key in self._d:
                self._drop(key)
            self._d[key] = (time.monotonic(), status, headers, body)
            self._bytes += len(body)
            while self._d and (len(self._d) > self.max_items or self._bytes > self.max_bytes):
                self._drop(next(iter(self._d)))

    def _drop(self, key: str) -> None:
        v = self._d.pop(key, None)
        if v is not None:
            self._bytes -= len(v[3])

    def clear(self) -> None:
        with self._lock:
            self._d.clear()
            self._bytes = 0


response_cache = _LRU()

_NO_CACHE_PREFIXES = ("/api/v1/system", "/api/v1/watchlists", "/api/v1/alerts", "/api/v1/models/train",
                      "/api/v1/studio", "/api/v1/backtest", "/api/v1/macro/v4/scenarios",
                      "/api/v1/live",  # realtime R1: hot store + SSE, never cached / buffered
                      "/api/v1/news",  # roadmap v3 N4: News Terminal (bus + hourly intraday DuckDB)
                      "/api/v1/theses",  # roadmap #8: thesis tracker (app_state)
                      "/api/v1/bql")  # roadmap v3 B1: BQL (own result cache; saved queries in app_state)
#: GET paths whose payload also depends on app_state (saved macro scenarios) -> never cached (macro-w4)
_NO_CACHE_SUFFIXES = ("/macro",)
_CACHE_POST = ("/api/v1/screener/query", "/api/v1/screener/export")


def _cacheable(method: str, path: str) -> bool:
    if not path.startswith("/api/v1/") or path.startswith(_NO_CACHE_PREFIXES):
        return False
    if method == "GET":
        return not path.endswith("/promote") and not (path.startswith("/api/v1/companies/")
                                                       and path.endswith(_NO_CACHE_SUFFIXES))
    return method == "POST" and path in _CACHE_POST


class SnapshotCacheMiddleware:
    """Pure ASGI middleware (cheap on the hot path). See module docstring."""

    def __init__(self, app: Any):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not _cacheable(scope["method"], scope["path"]):
            await self.app(scope, receive, send)
            return
        body = b""
        if scope["method"] == "POST":
            chunks = []
            more = True
            while more:
                msg = await receive()
                chunks.append(msg.get("body", b""))
                more = msg.get("more_body", False)
            body = b"".join(chunks)
            sent = False

            async def receive() -> dict[str, Any]:  # type: ignore[no-redef]
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}

        if b'"watch"' in body:  # universe 'watch' depends on mutable app_state watchlists
            await self.app(scope, receive, send)
            return
        snap = current_snapshot()
        h = hashlib.sha1()
        for part in (str(snap.path or get_settings().data_dir), snap.run_id, scope["method"], scope["path"],
                     scope.get("query_string", b"").decode("latin-1")):
            h.update(part.encode() + b"\x00")
        h.update(body)
        key = h.hexdigest()
        etag = f'W/"{snap.run_id}-{key[:16]}"'.encode()
        extra = [(b"etag", etag), (b"last-modified", snap.last_modified.encode()),
                 (b"x-serving-run-id", snap.run_id.encode()), (b"cache-control", b"no-cache")]
        req_headers = dict(scope.get("headers") or [])
        inm = req_headers.get(b"if-none-match")
        hit = response_cache.get(key)
        if hit is not None:
            status, headers, content = hit
            if inm and etag in [x.strip() for x in inm.split(b",")]:
                await send({"type": "http.response.start", "status": 304, "headers": extra})
                await send({"type": "http.response.body", "body": b""})
                return
            await send({"type": "http.response.start", "status": status,
                        "headers": headers + extra + [(b"x-cache", b"hit")]})
            await send({"type": "http.response.body", "body": content})
            return

        state: dict[str, Any] = {"status": 0, "headers": [], "chunks": []}

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
                state["headers"] = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"content-length"]
                if message["status"] == 200:
                    message = dict(message)
                    message["headers"] = list(message.get("headers", [])) + extra + [(b"x-cache", b"miss")]
            elif message["type"] == "http.response.body" and state["status"] == 200:
                state["chunks"].append(message.get("body", b""))
                if not message.get("more_body", False):
                    content = b"".join(state["chunks"])
                    hdrs = state["headers"] + [(b"content-length", str(len(content)).encode())]
                    response_cache.put(key, 200, hdrs, content)
            await send(message)

        await self.app(scope, receive, send_wrapper)


# ------------------------------------------------------------------------------------------------ fast JSON routes

import functools  # noqa: E402
from decimal import Decimal  # noqa: E402

import orjson  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from starlette.responses import Response  # noqa: E402


def _orjson_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if isinstance(o, bytes):
        return o.decode("utf-8", "replace")
    raise TypeError(f"not JSON serializable: {type(o).__name__}")


class FastJSONResponse(Response):
    """orjson rendering (dates/datetimes ISO, NaN/inf -> null, Decimal -> float)."""

    media_type = "application/json"

    def render(self, content: Any) -> bytes:
        return orjson.dumps(content, default=_orjson_default,
                            option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY)


class FastRoute(APIRoute):
    """Route whose (sync) endpoint result is rendered directly with orjson, skipping FastAPI's jsonable_encoder /
    response_model passes (responses are shaped to contracts/api.yaml by the routers and validated in tests)."""

    def __init__(self, path: str, endpoint: Any, **kw: Any):
        status = kw.get("status_code") or 200
        orig = endpoint

        @functools.wraps(orig)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            out = orig(*args, **kwargs)
            if isinstance(out, Response):
                return out
            return FastJSONResponse(out, status_code=status)

        kw["response_model"] = None
        super().__init__(path, wrapped, **kw)
