"""Curated RSS / Atom feeds (config/news_feeds.yaml) and a polite conditional-GET poller (roadmap v3 N2).

Only what a feed publishes for consumption is stored: headline, a summary snippet (<= 300 chars, HTML stripped), the
item URL, publisher, publication time and language. Article pages are never fetched.

Poller rules
* Each feed polls every `every_s` (60-120 s for publishers / central banks, longer for slow statistics feeds).
* Conditional GET: the feed's ETag / Last-Modified from the previous 200 are sent as If-None-Match /
  If-Modified-Since; a 304 costs no parsing. Validators survive restarts (data/state/news_feeds_state.json).
* Per-host politeness: at most one request per `host_gap_s` (default 5 s) to the same host, so many feeds of one
  publisher spread out.
* Back-off: an error doubles the feed's interval (cap 1 h, jitter); 429 / 503 honour Retry-After. A feed that fails
  for 24 h is reported (Data Health "Live news"), never removed automatically.
* Items: ids = news_items item_id (sha1 of the normalised URL, so the nightly news_ingest dedupes against them);
  entries dated more than 5 min in the future are dropped; on a feed's first poll only items of the last 48 h are
  emitted (no backlog flood).
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

KINDS = ("central_bank", "statistics", "regulator", "exchange", "publisher", "aggregator")
#: tier 1 = official (central banks, statistics offices, regulators, exchanges); 2 = wires / major financial press;
#: 3 = other publishers; 4 = GDELT web articles (set by the desk, not configured here)
TIER_LABEL = {1: "official", 2: "major", 3: "publisher", 4: "web"}
SNIPPET_MAX = 300
FIRST_POLL_MAX_AGE = timedelta(hours=48)
MAX_FUTURE = timedelta(minutes=5)
_TAG_RX = re.compile(r"<[^>]+>")
_WS_RX = re.compile(r"\s+")


def config_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).project_root / "config" / "news_feeds.yaml"


def state_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "news_feeds_state.json"


def verify_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "news_feeds_verify.json"


@dataclass
class FeedSpec:
    id: str
    url: str
    publisher: str
    kind: str = "publisher"
    tier: int = 3
    countries: list[str] = field(default_factory=list)
    language: str = "en"
    every_s: float = 120.0
    status: str = "ok"                 # ok | rejected | unverified
    reason: str | None = None          # why rejected / notes
    terms: str | None = None           # terms-of-use / RSS policy page
    robots: str | None = None          # allowed | disallowed | no_robots | unknown (verified value)
    rss_intended: bool = True          # the publisher offers this feed for consumption (RSS / Atom endpoint)
    title_filter: str | None = None    # optional regex the headline must match
    parser: str = "default"            # default | eurostat
    group: str | None = None           # display group (central banks, regulators, ...)

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc.lower()

    @property
    def active(self) -> bool:
        return self.status in ("ok", "unverified")

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in ("id", "url", "publisher", "kind", "tier", "countries", "language",
                                               "every_s", "status", "reason", "terms", "robots", "rss_intended",
                                               "group")} | {"host": self.host}


def load_feeds(settings: Settings | None = None, include_inactive: bool = False,
               path: Path | None = None) -> list[FeedSpec]:
    p = path or config_path(settings)
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except OSError:
        log.warning("news feeds config missing: %s", p)
        return []
    defaults = doc.get("defaults") or {}
    out: list[FeedSpec] = []
    seen: set[str] = set()
    for grp in doc.get("groups") or []:
        gname = grp.get("name")
        gdef = {k: v for k, v in grp.items() if k not in ("name", "feeds")}
        for f in grp.get("feeds") or []:
            d = {**defaults, **gdef, **f}
            fid = str(d["id"])
            if fid in seen:
                raise ValueError(f"duplicate feed id {fid} in {p}")
            seen.add(fid)
            spec = FeedSpec(id=fid, url=str(d["url"]), publisher=str(d.get("publisher") or fid),
                            kind=str(d.get("kind", "publisher")), tier=int(d.get("tier", 3)),
                            countries=[str(c) for c in d.get("countries") or []], language=str(d.get("language", "en")),
                            every_s=float(d.get("every_s", 120)), status=str(d.get("status", "unverified")),
                            reason=d.get("reason"), terms=d.get("terms"), robots=d.get("robots"),
                            rss_intended=bool(d.get("rss_intended", True)), title_filter=d.get("title_filter"),
                            parser=str(d.get("parser", "default")), group=gname)
            if spec.kind not in KINDS:
                raise ValueError(f"feed {fid}: unknown kind {spec.kind!r}")
            if include_inactive or spec.active:
                out.append(spec)
    return out


# ------------------------------------------------------------------------------------------------ parsing
def clean_snippet(s: str | None, max_chars: int = SNIPPET_MAX) -> str | None:
    if not s:
        return None
    t = _WS_RX.sub(" ", html.unescape(_TAG_RX.sub(" ", s))).strip()
    if not t:
        return None
    if len(t) > max_chars:
        cut = t[: max_chars - 1]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > max_chars * 0.6 else cut).rstrip(" ,;:.") + "…"
    return t


def clean_title(s: str | None) -> str:
    return _WS_RX.sub(" ", html.unescape(_TAG_RX.sub(" ", s or ""))).strip()


def url_key(url: str) -> str:
    """Python twin of app.events_v2.parse.url_key_expr (normalised URL key behind url_hash)."""
    k = url.strip().lower()
    k = re.sub(r"^[a-z]+://(www[0-9]*\.)?", "", k)
    k = re.sub(r"[?#].*$", "", k)
    return k.rstrip("/")


def url_hash(url: str | None) -> int | None:
    from app.events_v2.parse import hash64  # noqa: PLC0415

    return hash64(url_key(url)) if url else None


def _entry_time(e: Any) -> datetime | None:
    st = e.get("published_parsed") or e.get("updated_parsed") or e.get("created_parsed")
    if st:
        try:
            return datetime(*st[:6], tzinfo=UTC)
        except (TypeError, ValueError):
            return None
    return None


def parse_feed(spec: FeedSpec, content: bytes, now: datetime | None = None,
               max_age: timedelta | None = None) -> list[dict[str, Any]]:
    """Feed bytes -> item dicts (newest first). Undated entries get published_at = now (published_estimated)."""
    import feedparser  # noqa: PLC0415

    from app.ingestion.gdelt_gkg import item_id  # noqa: PLC0415

    now = now or datetime.now(UTC)
    if spec.parser == "eurostat":
        from app.realtime.streams.news import eurostat_fallback  # noqa: PLC0415

        rows = eurostat_fallback(content, now)
        for r in rows:
            r.update(source_id=spec.id, publisher=spec.publisher, category=spec.kind, tier=spec.tier,
                     published_estimated=False, snippet=None)
        return rows
    feed = feedparser.parse(content)
    rx = re.compile(spec.title_filter, re.I) if spec.title_filter else None
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in feed.entries:
        link = (e.get("link") or "").strip()
        title = clean_title(e.get("title"))
        if not (link and title) or not link.startswith(("http://", "https://")):
            continue
        if rx is not None and not rx.search(title):
            continue
        if link.startswith("http://"):   # some feeds alternate schemes between fetches: one stable item id
            link = "https://" + link[7:]
        ts = _entry_time(e)
        est = ts is None
        ts = ts or now
        if ts > now + MAX_FUTURE:
            continue
        if max_age is not None and now - ts > max_age:
            continue
        iid = item_id(link)
        if iid in seen:
            continue
        seen.add(iid)
        summ = e.get("summary") or e.get("description") or ""
        lang = (e.get("language") or feed.feed.get("language") or spec.language or "en")[:2].lower()
        out.append({"item_id": iid, "published_at": ts, "published_estimated": est, "source_id": spec.id,
                    "publisher": spec.publisher, "url": link, "title": title[:400],
                    "snippet": clean_snippet(summ), "language": lang or spec.language,
                    "countries": list(spec.countries), "category": spec.kind, "tier": spec.tier})
    out.sort(key=lambda r: r["published_at"], reverse=True)
    return out


# ------------------------------------------------------------------------------------------------ poller
@dataclass
class FeedState:
    etag: str | None = None
    last_modified: str | None = None
    next_at: float = 0.0
    polled: bool = False                # at least one successful 200/304 this lifetime (first-poll age rule)
    last_poll_at: float | None = None
    last_ok_at: float | None = None
    last_status: int | None = None
    last_error: str | None = None
    errors: int = 0
    consecutive_errors: int = 0
    polls: int = 0
    not_modified: int = 0
    items_last: int = 0
    new_last: int = 0
    items_total: int = 0
    first_error_at: float | None = None
    bytes_total: int = 0

    def to_dict(self) -> dict[str, Any]:
        def iso(x: float | None) -> str | None:
            return datetime.fromtimestamp(x, UTC).isoformat(timespec="seconds") if x else None

        return {"etag": self.etag, "last_modified": self.last_modified, "last_poll_at": iso(self.last_poll_at),
                "last_ok_at": iso(self.last_ok_at), "last_status": self.last_status, "last_error": self.last_error,
                "errors": self.errors, "consecutive_errors": self.consecutive_errors, "polls": self.polls,
                "not_modified": self.not_modified, "items_last": self.items_last, "new_last": self.new_last,
                "items_total": self.items_total, "failing_since": iso(self.first_error_at),
                "next_poll_at": iso(self.next_at) if self.next_at else None, "bytes_total": self.bytes_total}


def user_agent(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    contact = (s.sec_user_agent or "").split()[-1] if s.sec_user_agent else "local"
    return f"BiG-Equity-360/1.0 (+personal research RSS reader; {contact})"


def _robots_guard(request: Any) -> None:
    from app.core.robots_policy import guard  # noqa: PLC0415

    guard(str(request.url))


class FeedPoller:
    """Thread-safe conditional-GET poller over FeedSpecs. `poll_due()` fetches every due feed whose host is free."""

    def __init__(self, feeds: Iterable[FeedSpec], settings: Settings | None = None, host_gap_s: float = 5.0,
                 transport: Any = None, clock: Any = time.time, persist: bool = True, timeout: float = 20.0):
        self.s = settings or get_settings()
        self.feeds = {f.id: f for f in feeds}
        self.host_gap_s = host_gap_s
        self.clock = clock
        self.persist = persist
        self.state: dict[str, FeedState] = {k: FeedState() for k in self.feeds}
        self.host_next: dict[str, float] = {}
        self._lock = threading.Lock()
        self._saved_at = 0.0
        self._load_state()
        import httpx  # noqa: PLC0415

        self.client = httpx.Client(headers={"User-Agent": user_agent(self.s),
                                            "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                                                      "text/xml;q=0.8, */*;q=0.5"},
                                   timeout=timeout, follow_redirects=True, transport=transport,
                                   event_hooks={"request": [_robots_guard]})   # A9: robots deny list, every hop
        # spread the first polls (no thundering herd on start)
        now = self.clock()
        for i, k in enumerate(self.feeds):
            self.state[k].next_at = max(self.state[k].next_at, now + (i % 20) * 0.5)

    # ---- persistence (validators only)
    def _load_state(self) -> None:
        if not self.persist:
            return
        try:
            raw = json.loads(state_path(self.s).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k, v in (raw.get("feeds") or {}).items():
            if k in self.state and isinstance(v, dict):
                self.state[k].etag = v.get("etag")
                self.state[k].last_modified = v.get("last_modified")

    def save_state(self, force: bool = False) -> None:
        if not self.persist or (not force and self.clock() - self._saved_at < 60):
            return
        self._saved_at = self.clock()
        p = state_path(self.s)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {"updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "feeds": {k: v.to_dict() for k, v in self.state.items()}}
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except OSError:  # pragma: no cover - file in use: next time
            pass

    # ---- scheduling
    def due(self) -> list[FeedSpec]:
        now = self.clock()
        return [f for k, f in self.feeds.items() if self.state[k].next_at <= now]

    def _backoff(self, spec: FeedSpec, st: FeedState, retry_after: float | None = None) -> float:
        base = spec.every_s * (2 ** min(st.consecutive_errors, 6))
        delay = min(3600.0, max(base, retry_after or 0.0))
        return delay * (0.85 + random.random() * 0.3)

    def fetch(self, spec: FeedSpec) -> tuple[int, bytes | None, dict[str, str]]:
        """One conditional GET. Returns (status, body or None for 304, response headers lower-cased)."""
        st = self.state[spec.id]
        hdr: dict[str, str] = {}
        if st.etag:
            hdr["If-None-Match"] = st.etag
        if st.last_modified:
            hdr["If-Modified-Since"] = st.last_modified
        r = self.client.get(spec.url, headers=hdr)
        h = {k.lower(): v for k, v in r.headers.items()}
        if r.status_code == 304:
            return 304, None, h
        return r.status_code, r.content, h

    def poll_one(self, spec: FeedSpec, now_dt: datetime | None = None) -> list[dict[str, Any]]:
        st = self.state[spec.id]
        now = self.clock()
        st.last_poll_at = now
        st.polls += 1
        self.host_next[spec.host] = now + self.host_gap_s
        try:
            code, body, h = self.fetch(spec)
        except Exception as e:  # noqa: BLE001 - network: back off
            self._fail(spec, st, f"{type(e).__name__}: {e}"[:200])
            return []
        st.last_status = code
        if code == 304:
            st.not_modified += 1
            self._ok(spec, st)
            return []
        if code in (429, 503):
            ra = h.get("retry-after")
            secs = None
            if ra:
                try:
                    secs = float(ra)
                except ValueError:
                    try:
                        secs = (parsedate_to_datetime(ra) - datetime.now(UTC)).total_seconds()
                    except (TypeError, ValueError):
                        secs = None
            self._fail(spec, st, f"HTTP {code}", retry_after=secs)
            return []
        if code >= 400 or body is None:
            self._fail(spec, st, f"HTTP {code}")
            return []
        st.bytes_total += len(body)
        try:
            items = parse_feed(spec, body, now_dt, None if st.polled else FIRST_POLL_MAX_AGE)
        except Exception as e:  # noqa: BLE001 - malformed feed
            self._fail(spec, st, f"parse: {type(e).__name__}: {e}"[:200])
            return []
        st.etag = h.get("etag") or st.etag
        st.last_modified = h.get("last-modified") or st.last_modified
        if not st.last_modified and not st.etag:
            st.last_modified = None
        st.items_last = len(items)
        st.items_total += len(items)
        self._ok(spec, st)
        return items

    def _ok(self, spec: FeedSpec, st: FeedState) -> None:
        now = self.clock()
        st.polled = True
        st.last_ok_at = now
        st.consecutive_errors = 0
        st.first_error_at = None
        st.last_error = None
        st.next_at = now + spec.every_s * (0.9 + random.random() * 0.2)

    def _fail(self, spec: FeedSpec, st: FeedState, err: str, retry_after: float | None = None) -> None:
        now = self.clock()
        st.errors += 1
        st.consecutive_errors += 1
        st.last_error = err
        st.first_error_at = st.first_error_at or now
        st.next_at = now + self._backoff(spec, st, retry_after)

    def poll_due(self, now_dt: datetime | None = None, max_feeds: int = 50) -> list[dict[str, Any]]:
        """Poll due feeds (one per free host per call). Returns all parsed items (the caller dedupes by item_id)."""
        out: list[dict[str, Any]] = []
        now = self.clock()
        n = 0
        with self._lock:
            for spec in sorted(self.due(), key=lambda f: self.state[f.id].next_at):
                if self.host_next.get(spec.host, 0.0) > now:
                    continue
                out += self.poll_one(spec, now_dt)
                n += 1
                if n >= max_feeds:
                    break
            self.save_state()
        return out

    def status(self) -> dict[str, Any]:
        return {k: {**self.feeds[k].to_dict(), **v.to_dict()} for k, v in self.state.items()}

    def close(self) -> None:
        self.save_state(force=True)
        try:
            self.client.close()
        except Exception:  # noqa: BLE001
            pass


def http_date(ts: datetime) -> str:
    return format_datetime(ts.astimezone(UTC), usegmt=True)
