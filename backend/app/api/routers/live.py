"""Live overlay + push (R1.4; contracts/api.yaml tag `live`, owner: realtime).

GET /live/{filings,news,events,macro,alerts}: latest N items from the real-time hot store (data/realtime/bus.sqlite,
opened read-only) merged over the nightly serving snapshot by natural key (live wins; snapshot rows fill the rest
and carry origin=snapshot). GET /live/stream: Server-Sent Events of the public topics with heartbeats and
Last-Event-ID resume (ids are the bus' global message ids). GET /live/status: per-stream health from the supervisor's
status file (data/state/realtime_status.json) plus fresh hot-store stats.
The API never writes the bus; responses are not cached (deps._NO_CACHE_PREFIXES includes /api/v1/live).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, date, datetime
from typing import Any

import orjson
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.api.deps import Db, FastRoute, get_db
from app.core.config import get_settings
from app.core.logsetup import get_logger

router = APIRouter(tags=["live"], route_class=FastRoute)
log = get_logger("app.api.live")

PUBLIC_TOPICS = ("filings", "news", "events", "macro", "alerts", "signals", "news.item", "news.story", "quotes.tick",
                 "quotes.session")
DEFAULT_STREAM_TOPICS = ("filings", "news", "macro", "alerts")
_bus_lock = threading.Lock()
_bus: Any = None


def _ro_bus() -> Any:
    """Read-only bus, or None while the realtime layer has never run."""
    global _bus  # noqa: PLW0603
    with _bus_lock:
        if _bus is None:
            from app.realtime.bus import SqliteBus, default_bus_path  # noqa: PLC0415

            p = default_bus_path(get_settings())
            if not p.exists():
                return None
            _bus = SqliteBus(p, read_only=True)
        return _bus


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _ingest_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ------------------------------------------------------------------------------------------------ item shapes
def filing_item(p: dict[str, Any], live_id: int | None, first_seen: str | None, origin: str = "live") -> dict[str, Any]:
    kts = p.get("knowledge_ts")
    return {
        "live_id": live_id, "origin": origin, "filing_id": p.get("filing_id"), "source_id": p.get("source_id"),
        "source_entity_id": p.get("source_entity_id"), "issuer_id": p.get("issuer_id"),
        "security_id": p.get("security_id"), "ticker": p.get("ticker"), "company": p.get("company") or p.get("issuer_name"),
        "form_type": p.get("form_type"), "form_class": p.get("form_class"), "title": p.get("title"),
        "items": p.get("items"), "url": p.get("url"), "country": p.get("country"),
        "reporting_owner": p.get("reporting_owner"), "source_ts": _iso(kts),
        "knowledge_date": _iso(p.get("filed_date") or p.get("knowledge_date")), "first_seen_at": first_seen,
    }


def news_item(p: dict[str, Any], live_id: int | None, first_seen: str | None, origin: str = "live") -> dict[str, Any]:
    return {
        "live_id": live_id, "origin": origin, "item_id": p.get("item_id"), "source_id": p.get("source_id"),
        "category": p.get("category"), "publisher": p.get("publisher"), "title": p.get("title"), "url": p.get("url"),
        "language": p.get("language"), "published_at": _iso(p.get("published_at")), "first_seen_at": first_seen,
        "issuer_ids": p.get("issuer_ids") or [], "tickers": p.get("tickers") or [], "countries": p.get("countries") or [],
        "themes": p.get("themes") or [], "story_id": p.get("story_id"), "story_size": p.get("story_size"),
        "sentiment_label": p.get("sentiment_label"), "sentiment_score": p.get("sentiment_score"),
    }


def event_item(p: dict[str, Any], live_id: int | None, first_seen: str | None, key: str,
               origin: str = "live") -> dict[str, Any]:
    summary = None
    if p.get("kind") == "slot":
        summary = {k: p.get(k) for k in ("n_articles", "n_linked_articles", "n_events", "themes_articles",
                                         "themes_events", "countries")}
    return {
        "live_id": live_id, "origin": origin, "key": key, "kind": p.get("kind"), "title": p.get("title"),
        "url": p.get("url"), "domain": p.get("domain"), "language": p.get("language"), "tone": p.get("tone"),
        "themes": p.get("themes") or [], "countries": p.get("countries") or [], "issuer_ids": p.get("issuer_ids") or [],
        "story_id": p.get("story_id"), "story_size": p.get("story_size"), "sentiment_score": p.get("sentiment_score"),
        "published_at": _iso(p.get("published_at")), "first_seen_at": first_seen, "slot": p.get("slot"),
        "cameo": p.get("cameo"), "goldstein": p.get("goldstein"), "n_articles": p.get("n_articles"),
        "lat": p.get("lat"), "lon": p.get("lon"), "summary": summary,
    }


def macro_item(p: dict[str, Any], live_id: int | None, first_seen: str | None, origin: str = "live") -> dict[str, Any]:
    return {
        "live_id": live_id, "origin": origin, "release_event_id": p.get("release_event_id"),
        "series_id": p.get("series_id"), "country": p.get("country"), "release_name": p.get("release_name"),
        "publisher": p.get("publisher"), "importance": p.get("importance"), "theme": p.get("theme"),
        "period": _iso(p.get("period")), "measure": p.get("measure"), "actual": p.get("actual"),
        "prior": p.get("prior"), "prior_revised": p.get("prior_revised"), "expectation": p.get("expectation"),
        "lo80": p.get("lo80"), "hi80": p.get("hi80"), "model": p.get("model"), "surprise_z": p.get("surprise_z"),
        "signed_z": p.get("signed_z"), "scheduled_at": _iso(p.get("scheduled_at")),
        "captured_at": _iso(p.get("captured_at")) or first_seen, "capture_delay_s": p.get("capture_delay_s"),
        "model_based": True, "label": "Model-based expectation (not consensus)",
    }


def alert_item(p: dict[str, Any], live_id: int | None, origin: str = "live") -> dict[str, Any]:
    payload = p.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = None
    return {
        "live_id": live_id, "origin": origin, "event_id": p.get("event_id"), "alert_id": p.get("alert_id"),
        "security_id": p.get("security_id"), "rule_type": p.get("rule_type") or (payload or {}).get("rule_type"),
        "as_of_date": _iso(p.get("as_of_date")), "fired_at": _iso(p.get("fired_at")), "message": p.get("message"),
        "read": p.get("read"), "live": bool((payload or {}).get("live")), "payload": payload,
    }


def signal_item(p: dict[str, Any], live_id: int | None, first_seen: str | None, key: str) -> dict[str, Any]:
    sig = p.get("signal") or {}
    return {
        "live_id": live_id, "key": key, "kind": p.get("kind"), "issuer_id": p.get("issuer_id"),
        "security_id": p.get("security_id"), "ticker": p.get("ticker"), "series": p.get("series"),
        "score": sig.get("signal_score") if p.get("kind") == "insider" else p.get("p_change"),
        "first_seen_at": first_seen, "payload": p,
    }


def item_of(topic: str, m: Any) -> dict[str, Any]:
    fs = m.payload.get("first_seen_at") or _ingest_iso(m.ts_ingest)
    p = m.payload
    if topic == "news.item":   # roadmap v3 N4: News Terminal item (app.api.routers.news.term_item)
        from app.api.routers.news import term_item  # noqa: PLC0415

        return term_item(p, m.id, "live")
    if topic == "news.story":
        from app.api.routers.news import _live_story  # noqa: PLC0415

        return _live_story(p)
    if topic == "quotes.tick":   # workstream B: delayed quotes (app.realtime.quotes)
        return {"n": p.get("n"), "fetched_at": p.get("fetched_at"), "delayed": True,
                "quotes": [{**q, "quote_ts": _ingest_iso(q["quote_ts"]) if q.get("quote_ts") else None}
                           for q in p.get("quotes") or []]}
    if topic == "quotes.session":
        return {k: p.get(k) for k in ("market", "open", "in_tail", "reason", "reason_text", "opens_at", "closes_at",
                                      "n_targets")}
    if topic == "filings":
        return filing_item(p, m.id, fs)
    if topic == "news":
        return news_item(p, m.id, fs)
    if topic == "events":
        return event_item(p, m.id, fs, m.key)
    if topic == "macro":
        return macro_item(p, m.id, fs)
    if topic == "alerts":
        return alert_item(p, m.id)
    return signal_item(p, m.id, fs, m.key)


def _live(topic: str, n: int, where: dict[str, Any] | None = None, types: list[str] | None = None) -> list[Any]:
    bus = _ro_bus()
    if bus is None:
        return []
    try:
        return bus.latest(topic, n, where=where, types=types)
    except Exception as e:  # noqa: BLE001 - hot store unavailable -> snapshot only
        log.warning("live %s: hot store read failed: %s", topic, e)
        return []


def _envelope(items: list[dict[str, Any]], live_n: int, snap_status: str | None = None) -> dict[str, Any]:
    bus = _ro_bus()
    head = None
    if bus is not None:
        try:
            head = bus.head_id()
        except Exception:  # noqa: BLE001
            head = None
    return {"data_status": "ok" if items else "pending", "generated_at": _iso(datetime.now(UTC)), "head_id": head,
            "n_live": live_n, "n_snapshot": len(items) - live_n if len(items) >= live_n else 0,
            "snapshot_status": snap_status, "items": items}


def _sort_key(field: str) -> Any:
    return lambda x: (x.get(field) or x.get("first_seen_at") or "")


# ------------------------------------------------------------------------------------------------ REST
@router.get("/live/filings", operation_id="getLiveFilings")
def live_filings(limit: int = Query(50, ge=1, le=500), source_id: str | None = None, issuer_id: str | None = None,
                 security_id: str | None = None, form_class: str | None = None,
                 include_snapshot: bool = True, db: Db = Depends(get_db)) -> dict[str, Any]:
    where = {"source_id": source_id, "issuer_id": issuer_id, "security_id": security_id, "form_class": form_class}
    live = [item_of("filings", m) for m in _live("filings", limit, where)]
    seen = {x["filing_id"] for x in live}
    snap: list[dict[str, Any]] = []
    st = None
    if include_snapshot and not db.snap.is_empty:
        st = db.snap.status("filings_index")
        conds, args = ["1=1"], []
        for col, v in (("source_id", source_id), ("issuer_id", issuer_id), ("form_class", form_class)):
            if v:
                conds.append(f"{col} = ?")
                args.append(v)
        if security_id:
            conds.append("issuer_id IN (SELECT issuer_id FROM securities WHERE security_id = ?)")
            args.append(security_id)
        try:
            rows = db.all(f"SELECT * FROM filings WHERE {' AND '.join(conds)} ORDER BY knowledge_date DESC, "
                          f"knowledge_ts DESC NULLS LAST LIMIT ?", [*args, limit])
        except Exception as e:  # noqa: BLE001
            log.debug("snapshot filings unavailable: %s", e)
            rows = []
        for r in rows:
            if r["filing_id"] in seen:
                continue
            snap.append(filing_item({**r, "knowledge_ts": r.get("knowledge_ts")}, None, None, "snapshot"))
    items = sorted(live + snap, key=lambda x: (x.get("source_ts") or x.get("knowledge_date") or ""), reverse=True)
    return _envelope(items[:limit], min(len(live), limit), st)


@router.get("/live/news", operation_id="getLiveNews")
def live_news(limit: int = Query(50, ge=1, le=500), source_id: str | None = None, issuer_id: str | None = None,
              category: str | None = None, include_snapshot: bool = True, db: Db = Depends(get_db)) -> dict[str, Any]:
    live = [item_of("news", m) for m in _live("news", limit, {"source_id": source_id, "issuer_ids": issuer_id,
                                                               "category": category})]
    seen = {x["item_id"] for x in live}
    snap: list[dict[str, Any]] = []
    st = None
    if include_snapshot and not db.snap.is_empty and not category:
        st = db.snap.status("news_items")
        conds, args = ["1=1"], []
        if source_id:
            conds.append("source_id = ?")
            args.append(source_id)
        if issuer_id:
            conds.append("issuer_id = ?")
            args.append(issuer_id)
        try:
            rows = db.all(f"""SELECT item_id, any_value(source_id) AS source_id, any_value(publisher) AS publisher,
                    any_value(title) AS title, any_value(url) AS url, any_value(language) AS language,
                    max(published_at) AS published_at, list(issuer_id) AS issuer_ids,
                    any_value(sentiment_label) AS sentiment_label, any_value(sentiment_score) AS sentiment_score
                FROM news WHERE {' AND '.join(conds)} GROUP BY item_id ORDER BY published_at DESC LIMIT ?""",
                          [*args, limit])
        except Exception as e:  # noqa: BLE001
            log.debug("snapshot news unavailable: %s", e)
            rows = []
        snap = [news_item(r, None, None, "snapshot") for r in rows if r["item_id"] not in seen]
    items = sorted(live + snap, key=_sort_key("published_at"), reverse=True)
    return _envelope(items[:limit], min(len(live), limit), st)


@router.get("/live/events", operation_id="getLiveEvents")
def live_events(limit: int = Query(50, ge=1, le=500), kind: str | None = Query(None, pattern="^(article|event|slot)$"),
                issuer_id: str | None = None, theme: str | None = None, country: str | None = None) -> dict[str, Any]:
    types = [kind] if kind else ["article", "event"]
    live = [item_of("events", m) for m in _live("events", limit, {"issuer_ids": issuer_id, "themes": theme,
                                                                   "countries": country}, types)]
    return _envelope(live, len(live), None)


@router.get("/live/macro", operation_id="getLiveMacro")
def live_macro(limit: int = Query(50, ge=1, le=500), country: str | None = None, series_id: str | None = None,
               include_snapshot: bool = True, db: Db = Depends(get_db)) -> dict[str, Any]:
    live = [item_of("macro", m) for m in _live("macro", limit, {"country": country, "series_id": series_id})]
    seen = {(x["release_event_id"], x["series_id"]) for x in live}
    snap: list[dict[str, Any]] = []
    st = None
    if include_snapshot and not db.snap.is_empty:
        st = db.snap.status("macro_release_events")
        conds, args = ["e.status = 'released'", "e.release_date <= current_date"], []
        if country:
            conds.append("e.country = ?")
            args.append(country)
        if series_id:
            conds.append("e.series_id = ?")
            args.append(series_id)
        try:
            rows = db.all(f"""SELECT e.*, r.release_name, r.publisher, r.importance, s.expectation, s.lo80, s.hi80,
                    s.model, s.surprise_z, s.signed_z
                FROM macro_release_events e LEFT JOIN macro_releases r USING (release_event_id)
                LEFT JOIN macro_surprises s ON s.release_event_id = e.release_event_id AND s.series_id = e.series_id
                WHERE {' AND '.join(conds)} ORDER BY e.scheduled_at DESC LIMIT ?""", [*args, limit])
        except Exception as e:  # noqa: BLE001
            log.debug("snapshot macro unavailable: %s", e)
            rows = []
        for r in rows:
            if (r["release_event_id"], r["series_id"]) in seen:
                continue
            snap.append(macro_item({**r, "captured_at": None}, None, None, "snapshot"))
    items = sorted(live + snap, key=_sort_key("scheduled_at"), reverse=True)
    return _envelope(items[:limit], min(len(live), limit), st)


@router.get("/live/alerts", operation_id="getLiveAlerts")
def live_alerts(limit: int = Query(50, ge=1, le=500), unread_only: bool = False) -> dict[str, Any]:
    live = [item_of("alerts", m) for m in _live("alerts", limit)]
    by_id = {x["event_id"]: x for x in live}
    try:
        from app.api import app_state  # noqa: PLC0415

        for r in app_state.list_alert_events(unread_only, limit):
            it = alert_item(r, None, "app_state")
            if it["event_id"] in by_id:
                by_id[it["event_id"]]["read"] = it["read"]
            else:
                by_id[it["event_id"]] = it
    except Exception as e:  # noqa: BLE001 - app_state locked by the worker: live topic only
        log.debug("app_state alert events unavailable: %s", e)
    items = [x for x in by_id.values() if not (unread_only and x.get("read"))]
    items.sort(key=lambda x: x.get("fired_at") or "", reverse=True)
    return _envelope(items[:limit], min(len(live), limit), None)


# ------------------------------------------------------------------------------------------------ quotes / feeds
@router.get("/live/quotes", operation_id="getLiveQuotes")
def live_quotes(ids: str | None = Query(None, description="comma list: security ids, tickers or index / FX / futures "
                                                          "symbols (^GSPC, EURUSD=X, ES=F)"),
                set: str | None = Query(None, pattern="^(header)$"),  # noqa: A002
                record: bool = True, db: Db = Depends(get_db)) -> dict[str, Any]:
    """Last DELAYED quotes (workstream B). Requested ids are polled by the quotes service for the next 30 min."""
    from app.api import live_quotes as LQ  # noqa: PLC0415

    want = [x.strip() for x in (ids or "").split(",") if x.strip()]
    return LQ.quotes(want, set, db, record=record)


@router.get("/live/feeds", operation_id="getLiveFeeds")
def live_feeds() -> dict[str, Any]:
    """Per-feed health of the live fetchers (Data Health > Live feeds): last poll, latency, items / hour, errors."""
    from app.api import live_quotes as LQ  # noqa: PLC0415

    return LQ.feeds()


# ------------------------------------------------------------------------------------------------ status
@router.get("/live/status", operation_id="getLiveStatus")
def live_status() -> dict[str, Any]:
    from app.realtime.supervisor import read_status  # noqa: PLC0415

    st = read_status() or {}
    bus = _ro_bus()
    bus_stats = None
    if bus is not None:
        try:
            bus_stats = bus.stats()
        except Exception as e:  # noqa: BLE001
            bus_stats = {"error": str(e)[:200]}
    hb = st.get("heartbeat_at")
    hb_age = (datetime.now(UTC) - datetime.fromisoformat(hb)).total_seconds() if hb else None
    alive = bool(st.get("alive")) and hb_age is not None and hb_age < 60
    streams = []
    for kind in ("streams", "consumers"):
        for name, x in (st.get(kind) or {}).items():
            streams.append({"name": name, "kind": x.get("kind", kind[:-1]), "status": x.get("status") if alive
                            else ("stopped" if x.get("status") != "disabled" else "disabled"),
                            "detail": x.get("detail"), "events_total": x.get("events_total", 0),
                            "events_1h": x.get("events_1h", 0), "throughput_per_min": x.get("throughput_per_min", 0.0),
                            "last_event_at": x.get("last_event_at"), "last_event_source_ts": x.get("last_event_source_ts"),
                            "freshness_s": x.get("freshness_s"), "lag_s_p50": x.get("lag_s_p50"),
                            "lag_s_p95": x.get("lag_s_p95"), "lag_s_last": x.get("lag_s_last"),
                            "consumer_lag": x.get("lag"), "errors": x.get("errors", 0),
                            "consecutive_errors": x.get("consecutive_errors", 0), "last_error": x.get("last_error"),
                            "last_error_at": x.get("last_error_at"), "restarts": x.get("restarts", 0),
                            "last_poll_at": x.get("last_poll_at"), "next_poll_at": x.get("next_poll_at")})
    topics = [{"topic": t, **v} for t, v in sorted(((bus_stats or {}).get("topics") or {}).items())]
    groups = [{"group": g, "lag": v.get("lag_total"), "offsets": v.get("offsets")}
              for g, v in sorted(((bus_stats or {}).get("groups") or {}).items())]
    return {
        "data_status": "ok" if st else "pending", "running": alive, "pid": st.get("pid"),
        "started_at": st.get("started_at"), "heartbeat_at": hb, "heartbeat_age_s": round(hb_age, 1) if hb_age else None,
        "backend": (bus_stats or {}).get("backend", "sqlite"), "head_id": (bus_stats or {}).get("head_id"),
        "dead_letters": (bus_stats or {}).get("dead_letters"), "bus_bytes": (bus_stats or {}).get("bytes"),
        "queue": st.get("queue"), "streams": streams, "topics": topics, "groups": groups,
        "last_compaction": st.get("last_compaction"), "last_retention": st.get("last_retention"),
        "reconcile": st.get("reconcile"), "quotes": _quotes_summary(),
    }


def _quotes_summary() -> dict[str, Any]:
    from app.api import live_quotes as LQ  # noqa: PLC0415

    return LQ.quotes_summary()


# ------------------------------------------------------------------------------------------------ SSE
def _sse(event: str, data: Any, eid: int | None = None) -> bytes:
    head = f"id: {eid}\n" if eid is not None else ""
    return (head + f"event: {event}\ndata: " + orjson.dumps(data, default=str).decode() + "\n\n").encode()


@router.get("/live/stream", operation_id="streamLive")
def live_stream(request: Request, topics: str | None = None, last_event_id: int | None = Query(None, ge=0),
                      replay: int = Query(0, ge=0, le=500), types: str | None = None,
                      heartbeat_s: float = Query(15.0, ge=1.0, le=120.0),
                      max_events: int | None = Query(None, ge=1, le=100000),
                      timeout_s: float | None = Query(None, gt=0, le=86400)) -> StreamingResponse:
    want = [t for t in (topics.split(",") if topics else DEFAULT_STREAM_TOPICS) if t in PUBLIC_TOPICS] \
        or list(DEFAULT_STREAM_TOPICS)
    type_set = {t for t in (types or "").split(",") if t}
    hdr = request.headers.get("last-event-id")
    start_id = int(hdr) if hdr and hdr.strip().isdigit() else last_event_id

    async def gen() -> Any:
        bus = _ro_bus()
        last = start_id
        if last is None:
            head = bus.head_id() if bus is not None else 0
            last = head
            if replay and bus is not None:
                recent: list[Any] = []
                for t in want:
                    recent += bus.latest(t, replay)
                recent.sort(key=lambda m: m.id)
                recent = recent[-replay:]
                last = (recent[0].id - 1) if recent else head
        yield b"retry: 3000\n\n"
        yield _sse("hello", {"topics": want, "last_event_id": last, "server_time": _iso(datetime.now(UTC))})
        sent = 0
        t0 = hb = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            if bus is None:
                bus = _ro_bus()
            msgs = await asyncio.to_thread(bus.read, want, last, 200) if bus is not None else []
            for m in msgs:
                last = m.id
                if type_set and m.type not in type_set:
                    continue
                yield _sse(m.topic, {"id": m.id, "topic": m.topic, "type": m.type, "key": m.key,
                                     "ts_source": m.ts_source, "ts_ingest": _ingest_iso(m.ts_ingest),
                                     "sent_at": _iso(datetime.now(UTC)), "item": item_of(m.topic, m)}, m.id)
                sent += 1
                if max_events and sent >= max_events:
                    return
            now = time.monotonic()
            if timeout_s and now - t0 >= timeout_s:
                return
            if now - hb >= heartbeat_s:
                hb = now
                yield _sse("heartbeat", {"last_event_id": last, "server_time": _iso(datetime.now(UTC))})
            if not msgs:
                await asyncio.sleep(0.5)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
