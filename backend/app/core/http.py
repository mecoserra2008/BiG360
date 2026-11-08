"""HTTP clients with per-source rate limiting, retries/backoff and an on-disk response cache.

    http = HttpClient("sec_edgar")                      # SEC User-Agent applied automatically
    r = http.get("https://data.sec.gov/submissions/CIK0000320193.json", cache_ttl=86400)
    data = r.json()

    async with AsyncHttpClient("gdelt_files") as ah:
        r = await ah.get(url)

Retries: connection errors, timeouts, 429 and 5xx -> exponential backoff with jitter (respects Retry-After).
Cache: only successful (2xx) GET responses, keyed by method+URL+params with secret params removed, stored under
data/cache/http/<source_id>/<aa>/<sha1>.bin (+ .json metadata). cache_ttl=None uses the default; 0 disables;
-1 caches forever (immutable resources like filed documents).
Secrets: query params named in SECRET_PARAMS are excluded from cache keys and redacted in logs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
import orjson

from . import robots_policy as _robots
from .config import Settings, get_settings
from .logsetup import get_logger, redact
from .ratelimit import RateLimit, TokenBucket, get_limiter

log = get_logger(__name__)

SECRET_PARAMS = {"api_key", "apikey", "apiKey", "crtfc_key", "token", "key", "Subscription-Key", "access_key", "subscription-key"}
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status, self.url = status, url
        super().__init__(f"HTTP {status} for {redact(url)}: {redact(body[:300])}")


class SourceDisallowed(HttpError):
    """Request to a source whose robots.txt / terms disallow automated collection (app.core.robots_policy, A9
    2026-11-07): raised before any network I/O; status 451, reason code `source_disallowed`."""

    reason = _robots.REASON

    def __init__(self, url: str, block: "_robots.Block"):
        self.block = block
        super().__init__(451, url, f"{_robots.REASON}: {block.host}{block.path_prefix} - {block.rule}")


def _guard(url: str) -> None:
    b = _robots.find_block(url)
    if b is not None:
        raise SourceDisallowed(url, b)


def _guard_request(request: httpx.Request) -> None:
    """httpx request hook: every request the client sends, redirect hops included, is checked (A9)."""
    _guard(str(request.url))


async def _aguard_request(request: httpx.Request) -> None:
    _guard(str(request.url))


@dataclass
class CachedResponse:
    """Minimal response object returned by both live and cached requests."""

    status_code: int
    url: str
    content: bytes
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False
    fetched_at: float = 0.0

    @property
    def text(self) -> str:
        enc = "utf-8"
        ctype = self.headers.get("content-type", "")
        if "charset=" in ctype:
            enc = ctype.split("charset=")[-1].split(";")[0].strip() or "utf-8"
        return self.content.decode(enc, errors="replace")

    def json(self) -> Any:
        return orjson.loads(self.content)


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any]:
    return {k: v for k, v in (params or {}).items() if k not in SECRET_PARAMS}


def cache_key(method: str, url: str, params: dict[str, Any] | None, body: bytes | None = None) -> str:
    h = hashlib.sha1()
    h.update(method.upper().encode())
    h.update(url.encode())
    h.update(urlencode(sorted(_clean_params(params).items()), doseq=True).encode())
    if body:
        h.update(body)
    return h.hexdigest()


class DiskCache:
    def __init__(self, root: Path):
        self.root = root

    def _paths(self, source_id: str, key: str) -> tuple[Path, Path]:
        d = self.root / source_id / key[:2]
        return d / f"{key}.bin", d / f"{key}.json"

    def get(self, source_id: str, key: str, ttl: int) -> CachedResponse | None:
        if ttl == 0:
            return None
        body_p, meta_p = self._paths(source_id, key)
        if not (body_p.exists() and meta_p.exists()):
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if ttl > 0 and time.time() - meta["fetched_at"] > ttl:
            return None
        return CachedResponse(meta["status_code"], meta["url"], body_p.read_bytes(), meta.get("headers", {}), True, meta["fetched_at"])

    def put(self, source_id: str, key: str, resp: CachedResponse) -> None:
        body_p, meta_p = self._paths(source_id, key)
        body_p.parent.mkdir(parents=True, exist_ok=True)
        tmp = body_p.with_suffix(".bin.tmp")
        tmp.write_bytes(resp.content)
        tmp.replace(body_p)
        keep = {k: v for k, v in resp.headers.items() if k.lower() in ("content-type", "etag", "last-modified")}
        meta_p.write_text(
            json.dumps({"status_code": resp.status_code, "url": redact(resp.url), "headers": keep, "fetched_at": resp.fetched_at}),
            encoding="utf-8",
        )


def _default_headers(source_id: str, settings: Settings) -> dict[str, str]:
    h = {"Accept-Encoding": "gzip, deflate"}
    if source_id.startswith("sec"):
        # SEC requires a descriptive User-Agent with contact e-mail; requests without it get 403.
        h["User-Agent"] = settings.sec_user_agent
    else:
        h["User-Agent"] = f"BiG-Equity-360/1.0 (+local research; {settings.sec_user_agent.split()[-1]})"
    return h


def _backoff(attempt: int, base: float, cap: float = 120.0, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(cap, float(retry_after))
        except ValueError:
            pass
    return min(cap, base * (2**attempt)) * (0.5 + random.random() / 2)


class _Base:
    def __init__(self, source_id: str, base_url: str = "", headers: dict[str, str] | None = None,
                 rate_limit: RateLimit | None = None, cache: bool = True, max_retries: int = 5,
                 backoff_base: float = 1.0, timeout: float | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.source_id = source_id
        self.base_url = base_url.rstrip("/")
        self.headers = {**_default_headers(source_id, self.settings), **(headers or {})}
        self.limiter: TokenBucket = get_limiter(source_id, rate_limit)
        self.cache = DiskCache(self.settings.cache_dir / "http") if cache else None
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.timeout = timeout or self.settings.http_timeout_s
        self.n_requests = 0
        self.n_cache_hits = 0

    def _url(self, url: str) -> str:
        return url if url.startswith("http") or not self.base_url else f"{self.base_url}/{url.lstrip('/')}"

    def _ttl(self, cache_ttl: int | None) -> int:
        if self.cache is None:
            return 0
        return self.settings.http_cache_default_ttl_s if cache_ttl is None else cache_ttl

    @staticmethod
    def _to_cached(r: httpx.Response) -> CachedResponse:
        return CachedResponse(r.status_code, str(r.url), r.content, dict(r.headers), False, time.time())


class HttpClient(_Base):
    """Synchronous client (use from worker threads)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._client = httpx.Client(headers=self.headers, timeout=self.timeout, follow_redirects=True, http2=False,
                                    event_hooks={"request": [_guard_request]})

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(self, method: str, url: str, params: dict[str, Any] | None = None, *, data: Any = None,
                json_body: Any = None, headers: dict[str, str] | None = None, cache_ttl: int | None = 0,
                raise_for_status: bool = True) -> CachedResponse:
        url = self._url(url)
        _guard(url)
        ttl = self._ttl(cache_ttl) if method.upper() == "GET" else 0
        body_bytes = orjson.dumps(json_body) if json_body is not None else None
        key = cache_key(method, url, params, body_bytes)
        if ttl and self.cache:
            hit = self.cache.get(self.source_id, key, ttl)
            if hit:
                self.n_cache_hits += 1
                return hit
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                self.n_requests += 1
                r = self._client.request(method, url, params=params, data=data, json=json_body, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                wait = _backoff(attempt, self.backoff_base)
                log.warning("%s %s transport error (%s); retry %d in %.1fs", self.source_id, redact(url), type(e).__name__, attempt + 1, wait)
                time.sleep(wait)
                continue
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = _backoff(attempt, self.backoff_base, retry_after=r.headers.get("retry-after"))
                if r.status_code == 429:
                    self.limiter.penalize(wait)
                log.warning("%s %s -> %d; retry %d in %.1fs", self.source_id, redact(str(r.url)), r.status_code, attempt + 1, wait)
                time.sleep(wait)
                continue
            resp = self._to_cached(r)
            if raise_for_status and r.status_code >= 400:
                raise HttpError(r.status_code, str(r.url), r.text)
            if ttl and self.cache and 200 <= r.status_code < 300:
                self.cache.put(self.source_id, key, resp)
            return resp
        raise HttpError(-1, url, f"retries exhausted: {last_exc!r}")

    def get(self, url: str, params: dict[str, Any] | None = None, *, cache_ttl: int | None = None, **kw: Any) -> CachedResponse:
        return self.request("GET", url, params, cache_ttl=cache_ttl, **kw)

    def post(self, url: str, *, json_body: Any = None, data: Any = None, **kw: Any) -> CachedResponse:
        return self.request("POST", url, json_body=json_body, data=data, **kw)

    def download(self, url: str, dest: Path, params: dict[str, Any] | None = None, chunk: int = 1 << 20) -> Path:
        """Stream a large file (e.g. SEC companyfacts.zip, GDELT zips) to disk atomically."""
        url = self._url(url)
        _guard(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire()
            try:
                self.n_requests += 1
                with self._client.stream("GET", url, params=params, timeout=max(self.timeout, 300)) as r:
                    if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                        time.sleep(_backoff(attempt, self.backoff_base, retry_after=r.headers.get("retry-after")))
                        continue
                    if r.status_code >= 400:
                        raise HttpError(r.status_code, url)
                    with open(tmp, "wb") as fh:
                        for b in r.iter_bytes(chunk):
                            fh.write(b)
                tmp.replace(dest)
                return dest
            except (httpx.TransportError, httpx.TimeoutException):
                time.sleep(_backoff(attempt, self.backoff_base))
        raise HttpError(-1, url, "download retries exhausted")


class AsyncHttpClient(_Base):
    """Async client (use inside asyncio for high-fanout sources such as GDELT files)."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._client = httpx.AsyncClient(headers=self.headers, timeout=self.timeout, follow_redirects=True,
                                         event_hooks={"request": [_aguard_request]})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "AsyncHttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def request(self, method: str, url: str, params: dict[str, Any] | None = None, *, data: Any = None,
                      json_body: Any = None, headers: dict[str, str] | None = None, cache_ttl: int | None = 0,
                      raise_for_status: bool = True) -> CachedResponse:
        url = self._url(url)
        _guard(url)
        ttl = self._ttl(cache_ttl) if method.upper() == "GET" else 0
        body_bytes = orjson.dumps(json_body) if json_body is not None else None
        key = cache_key(method, url, params, body_bytes)
        if ttl and self.cache:
            hit = self.cache.get(self.source_id, key, ttl)
            if hit:
                self.n_cache_hits += 1
                return hit
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self.limiter.acquire_async()
            try:
                self.n_requests += 1
                r = await self._client.request(method, url, params=params, data=data, json=json_body, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                last_exc = e
                await asyncio.sleep(_backoff(attempt, self.backoff_base))
                continue
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = _backoff(attempt, self.backoff_base, retry_after=r.headers.get("retry-after"))
                if r.status_code == 429:
                    self.limiter.penalize(wait)
                await asyncio.sleep(wait)
                continue
            resp = self._to_cached(r)
            if raise_for_status and r.status_code >= 400:
                raise HttpError(r.status_code, str(r.url), r.text)
            if ttl and self.cache and 200 <= r.status_code < 300:
                self.cache.put(self.source_id, key, resp)
            return resp
        raise HttpError(-1, url, f"retries exhausted: {last_exc!r}")

    async def get(self, url: str, params: dict[str, Any] | None = None, *, cache_ttl: int | None = None, **kw: Any) -> CachedResponse:
        return await self.request("GET", url, params, cache_ttl=cache_ttl, **kw)
