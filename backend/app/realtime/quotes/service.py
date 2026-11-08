"""Delayed quotes service: the polling loop (supervisor child `quotes`; `python -m app.cli realtime quotes`).

Cadence (per market key = exchange MIC / FX / GLOBEX, sessions.py):
* exchange open           every `open_every_s` (60 s; the spec allows 1-5 min)
* closing tail (+25 min)  every `tail_every_s` (300 s): the delayed closing print arrives after the bell
* FX / CME Globex         every `h24_every_s` (120 s) while open (24x5)
* closed                  idle with the reason (weekend / holiday / before open / after close) and the next open;
                          a security with no stored quote (new in a watchlist, viewed in the UI) still gets ONE
                          snapshot so the page can show the last delayed price with "market closed"
* on demand               ids the UI asks for (interest file) are fetched within ~5 s, whatever the market state
All due symbols of one tick go into as few Yahoo requests as possible (batches of 50). US equities use the Polygon
snapshot instead when the account plan includes it (probed at most daily; free plans do not).

Budgets / back-off: Yahoo <= `yahoo_per_hour` requests per sliding hour (default 400) on top of the machine-wide
shared `yfinance` token bucket; Polygon at most one call per `polygon_every_s` (120 s) through the shared 4/min bucket.
A failing source backs off 60 s x 2^(n-1) up to 15 min (HTTP 429: >= 5 min) and the status says until when.

Output: data/realtime/quotes.sqlite (last quote + intraday ticks, labelled delayed), bus topics `quotes.tick` (one
message per poll with the quotes that changed; key quotes:<epoch ms>) and `quotes.session` (market open / close
transitions), status file data/state/quotes_status.json (feeds: last poll, latency, items / hour, errors). The bus
topics are pruned to `bus_keep_h` (6 h) by this service and released for the realtime retention (lake offset).
Never writes prices_daily. Stop: data/state/quotes.STOP (supervisor), Ctrl+C / Ctrl+Break.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.realtime.quotes.sessions import FX, GLOBEX, REASON_TEXT, MarketState, SessionCalendar, state
from app.realtime.quotes.sources import PolygonSnapshot, Quote, SourceError, YahooQuotes, iso
from app.realtime.quotes.store import QuoteStore, read_interest
from app.realtime.quotes.universe import Target, Universe, build_universe

log = get_logger("app.realtime.quotes")

STATUS_FILE = "quotes_status.json"
US_MICS = {"XNYS", "XNAS", "XASE", "ARCX", "BATS"}
US_EQUITY_TYPES = {"common", "adr", "preferred", "etf", "reit"}
TOPIC_TICK = "quotes.tick"
TOPIC_SESSION = "quotes.session"


def status_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / STATUS_FILE


def stop_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "quotes.STOP"


def lock_for(settings: Settings | None = None) -> FileLock:
    s = settings or get_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(s.state_dir / "quotes.lock"), timeout=0)


def is_running(settings: Settings | None = None) -> bool:
    lk = lock_for(settings)
    try:
        lk.acquire(timeout=0)
    except Timeout:
        return True
    lk.release()
    return False


def read_status(settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        st = json.loads(status_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    st["alive"] = is_running(settings)
    return st


@dataclass
class FeedStats:
    """Per-feed health for the Data Health "Live feeds" panel."""

    name: str
    status: str = "starting"          # starting | ok | idle | backoff | disabled
    reason: str | None = None
    polls: int = 0
    requests: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_poll_at: float | None = None
    last_ok_at: float | None = None
    last_error: str | None = None
    last_error_at: float | None = None
    backoff_until: float | None = None
    _items: deque = field(default_factory=lambda: deque(maxlen=20000))      # (t, n)
    _reqs: deque = field(default_factory=lambda: deque(maxlen=5000))        # t of each request
    _lat: deque = field(default_factory=lambda: deque(maxlen=300))          # fetch round-trip ms
    _age: deque = field(default_factory=lambda: deque(maxlen=2000))         # fetched_at - quote_ts (s), open markets

    def ok(self, now: float, items: int, requests: int, ms: float, ages: list[float]) -> None:
        self.polls += 1
        self.requests += requests
        self.last_poll_at = self.last_ok_at = now
        self.consecutive_errors = 0
        self.backoff_until = None
        self.status, self.reason = "ok", None
        self._items.append((now, items))
        for _ in range(requests):
            self._reqs.append(now)
        self._lat.append(ms)
        self._age.extend(a for a in ages if 0 <= a < 6 * 3600)

    def fail(self, now: float, err: str, backoff_s: float, requests: int = 1) -> None:
        self.polls += 1
        self.requests += requests
        for _ in range(requests):
            self._reqs.append(now)
        self.errors += 1
        self.consecutive_errors += 1
        self.last_poll_at = now
        self.last_error, self.last_error_at = err[:300], now
        self.backoff_until = now + backoff_s
        self.status, self.reason = "backoff", f"source down / rate-limited: retry at {iso(self.backoff_until)}"

    def requests_1h(self, now: float) -> int:
        while self._reqs and now - self._reqs[0] > 3600:
            self._reqs.popleft()
        return len(self._reqs)

    def to_dict(self, now: float) -> dict[str, Any]:
        while self._items and now - self._items[0][0] > 3600:
            self._items.popleft()
        lat = sorted(self._lat)
        age = sorted(self._age)

        def pct(xs: list[float], q: float) -> float | None:
            return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1) if xs else None

        return {"name": self.name, "status": self.status, "reason": self.reason, "polls": self.polls,
                "requests": self.requests, "requests_1h": self.requests_1h(now), "errors": self.errors,
                "consecutive_errors": self.consecutive_errors, "last_poll_at": iso(self.last_poll_at),
                "last_ok_at": iso(self.last_ok_at), "last_error": self.last_error, "last_error_at": iso(self.last_error_at),
                "backoff_until": iso(self.backoff_until), "items_1h": sum(n for _, n in self._items),
                "latency_ms_p50": round(statistics.median(lat), 0) if lat else None, "latency_ms_p95": pct(lat, 0.95),
                "quote_age_s_p50": round(statistics.median(age), 0) if age else None, "quote_age_s_p95": pct(age, 0.95)}


class QuotesService:
    def __init__(self, settings: Settings | None = None, *, bus: Any = None, store: QuoteStore | None = None,
                 yahoo: YahooQuotes | None = None, polygon: PolygonSnapshot | None = None,
                 calendar: SessionCalendar | None = None, universe_fn: Any = None, interest_fn: Any = None,
                 clock: Any = time.time, use_lock: bool = True, **params: Any):
        self.s = settings or get_settings()
        self.clock = clock
        self.params = params
        self.open_every_s = float(params.get("open_every_s", os.environ.get("BIG360_QUOTES_EVERY_S", 60)))
        self.tail_every_s = float(params.get("tail_every_s", 300))
        self.h24_every_s = float(params.get("h24_every_s", 120))
        self.refresh_s = float(params.get("refresh_s", 600))
        self.tick_s = float(params.get("tick_s", 5))
        self.yahoo_per_hour = int(params.get("yahoo_per_hour", 400))
        self.polygon_every_s = float(params.get("polygon_every_s", 120))
        self.bus_keep_h = float(params.get("bus_keep_h", 6))
        self.snapshot_retry_s = float(params.get("snapshot_retry_s", 1800))
        self.bus = bus
        self.store = store or QuoteStore(settings=self.s)
        self.yahoo = yahoo or YahooQuotes(batch=int(params.get("batch", 50)))
        if polygon is None:
            key = self.s.secret("polygon_apikey") if hasattr(self.s, "secret") else None
            polygon = PolygonSnapshot(key)
        self.polygon = polygon
        self.cal = calendar or SessionCalendar(self.s)
        self.universe_fn = universe_fn or (lambda interest: build_universe(self.s, interest))
        self.interest_fn = interest_fn or (lambda: read_interest(self.s))
        self.use_lock = use_lock
        self._lock = lock_for(self.s)
        self.universe = Universe()
        self._universe_at = -1e18
        self._interest: set[str] = set()
        self.next_due: dict[str, float] = {}
        self.market_states: dict[str, MarketState] = {}
        self._last_reason: dict[str, str] = {}
        self._last_pub: dict[str, tuple[float, float | None]] = {}
        self._snap_tried: dict[str, float] = {}
        self._bad: dict[str, float] = {}           # symbol -> retry after (no price at the source)
        self._polygon_last = 0.0
        self.feeds = {"yahoo": FeedStats("yahoo"), "polygon": FeedStats("polygon")}
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.last_pruned = 0.0
        self.published = 0
        self.last_publish_at: float | None = None
        self.loop_errors = 0
        self.last_loop_error: str | None = None
        ok, why = self.polygon.available(self.clock())
        if not ok:
            self.feeds["polygon"].status, self.feeds["polygon"].reason = "disabled", why

    # ------------------------------------------------------------------ helpers
    def _bus(self) -> Any:
        if self.bus is None:
            from app.realtime.bus import SqliteBus  # noqa: PLC0415

            self.bus = SqliteBus(settings=self.s)
        return self.bus

    def refresh_universe(self, now: float, force: bool = False) -> bool:
        interest = set(self.interest_fn() or [])
        new_interest = interest - set(self.universe.targets) - self._interest
        if not force and not new_interest and now - self._universe_at < self.refresh_s:
            return False
        try:
            self.universe = self.universe_fn(sorted(interest))
        except Exception as e:  # noqa: BLE001 - keep the previous universe
            log.warning("quotes: universe refresh failed: %s", e)
            self.universe.errors = [f"refresh failed: {type(e).__name__}: {e}"[:200]]
        self._interest = interest
        self._universe_at = now
        return True

    def _backoff(self, feed: FeedStats, e: SourceError | Exception) -> float:
        n = feed.consecutive_errors + 1
        base = min(900.0, 60.0 * 2 ** (n - 1))
        ra = getattr(e, "retry_after_s", None)
        return max(base, float(ra)) if ra else base

    def _yahoo_budget_ok(self, now: float, n_requests: int) -> bool:
        return self.feeds["yahoo"].requests_1h(now) + n_requests <= self.yahoo_per_hour

    def market_cadence(self, st: MarketState) -> float:
        if st.key in (FX, GLOBEX):
            return self.h24_every_s
        return self.tail_every_s if st.in_tail else self.open_every_s

    # ------------------------------------------------------------------ one tick
    def due_targets(self, now: float) -> tuple[list[Target], list[dict[str, Any]]]:
        """Targets to fetch now + session transitions (quotes.session payloads)."""
        dt = datetime.fromtimestamp(now, UTC)
        by_mk = self.universe.by_market()
        due: dict[str, Target] = {}
        transitions: list[dict[str, Any]] = []
        stored = self.store.last(list(self.universe.targets)) if self.universe.targets else {}
        for mk, targets in by_mk.items():
            st = state(mk, dt, self.cal)
            self.market_states[mk] = st
            if self._last_reason.get(mk) != st.reason:
                if mk in self._last_reason:
                    transitions.append({**st.to_dict(), "n_targets": len(targets),
                                        "reason_text": REASON_TEXT.get(st.reason, st.reason)})
                self._last_reason[mk] = st.reason
                if st.open:
                    self.next_due[mk] = now   # poll at once on open
            if st.open and now >= self.next_due.get(mk, 0.0):
                for t in targets:
                    due[t.security_id] = t
                self.next_due[mk] = now + self.market_cadence(st)
            elif not st.open:
                for t in targets:   # one snapshot for securities without a stored quote
                    if t.security_id not in stored and now - self._snap_tried.get(t.security_id, -1e18) >= self.snapshot_retry_s:
                        due[t.security_id] = t
        for i in self._interest:   # viewed in the UI: fetch once soon even when the market is closed
            t = self.universe.targets.get(i)
            if t is not None and i not in stored and now - self._snap_tried.get(i, -1e18) >= 60:
                due[i] = t
        for i in due:
            self._snap_tried[i] = now
        return list(due.values()), transitions

    def fetch(self, targets: list[Target], now: float) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Fetch quotes for the targets -> (store rows, per-source report)."""
        rep: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        if not targets:
            return rows, rep
        by_sym: dict[str, list[Target]] = {}
        poly_targets: list[Target] = []
        pf = self.feeds["polygon"]
        p_ok, p_why = self.polygon.available(now)
        use_poly = p_ok and now - self._polygon_last >= self.polygon_every_s and not (pf.backoff_until and now < pf.backoff_until)
        for t in targets:
            if use_poly and t.polygon and t.market in US_MICS and (t.security_type or "") in US_EQUITY_TYPES:
                poly_targets.append(t)
            elif t.yahoo and self._bad.get(t.yahoo, 0) <= now:
                by_sym.setdefault(t.yahoo, []).append(t)
        if not p_ok and pf.status != "disabled":
            pf.status, pf.reason = "disabled", p_why
        # ---- Polygon (US equities, plan permitting)
        if poly_targets:
            self._polygon_last = now
            t0 = time.perf_counter()
            try:
                qs = self.polygon.fetch(sorted({t.polygon for t in poly_targets if t.polygon}), now)
                ms = (time.perf_counter() - t0) * 1000
                got = {q.symbol: q for q in qs}
                for t in poly_targets:
                    q = got.get(t.polygon or "")
                    if q is not None:
                        rows.append(self._row(t, q, now))
                    elif t.yahoo:
                        by_sym.setdefault(t.yahoo, []).append(t)   # not in the snapshot: Yahoo fallback
                pf.ok(now, len(qs), 1, ms, [now - q.quote_ts for q in qs if q.quote_ts])
                rep["polygon"] = {"requests": 1, "quotes": len(qs)}
            except SourceError as e:
                if e.fatal:
                    pf.status, pf.reason = "disabled", str(e)
                    pf.last_error, pf.last_error_at = str(e), now
                else:
                    pf.fail(now, str(e), self._backoff(pf, e))
                for t in poly_targets:
                    if t.yahoo:
                        by_sym.setdefault(t.yahoo, []).append(t)
                rep["polygon"] = {"error": str(e)}
        # ---- Yahoo (everything else)
        yf = self.feeds["yahoo"]
        syms = sorted(by_sym)
        if syms:
            n_req = -(-len(syms) // self.yahoo.batch)
            if yf.backoff_until and now < yf.backoff_until:
                rep["yahoo"] = {"skipped": "backoff", "until": iso(yf.backoff_until), "symbols": len(syms)}
                for i in [t.security_id for s in syms for t in by_sym[s]]:
                    self._snap_tried.pop(i, None)
            elif not self._yahoo_budget_ok(now, n_req):
                yf.status, yf.reason = "idle", f"hourly request budget reached ({self.yahoo_per_hour}/h)"
                rep["yahoo"] = {"skipped": "budget", "symbols": len(syms)}
                for i in [t.security_id for s in syms for t in by_sym[s]]:
                    self._snap_tried.pop(i, None)
            else:
                t0 = time.perf_counter()
                try:
                    qs, bad, n = self.yahoo.fetch(syms)
                    ms = (time.perf_counter() - t0) * 1000 / max(n, 1)
                    for q in qs:
                        for t in by_sym.get(q.symbol, []):
                            rows.append(self._row(t, q, now))
                    for b in bad:
                        self._bad[b] = now + 6 * 3600
                    open_mk = {k for k, st in self.market_states.items() if st.open and not st.in_tail}
                    ages = [now - q.quote_ts for q in qs if q.quote_ts and any(
                        t.market in open_mk for t in by_sym.get(q.symbol, []))]
                    yf.ok(now, len(qs), n, ms, ages)
                    rep["yahoo"] = {"requests": n, "quotes": len(qs), "no_price": len(bad)}
                except SourceError as e:
                    yf.fail(now, str(e), self._backoff(yf, e))
                    rep["yahoo"] = {"error": str(e)}
                    log.warning("quotes: yahoo failed (%d in a row), back-off until %s: %s", yf.consecutive_errors,
                                iso(yf.backoff_until), e)
        return rows, rep

    def _row(self, t: Target, q: Quote, now: float) -> dict[str, Any]:
        return {"security_id": t.security_id, "symbol": q.symbol, "source": q.source, "price": q.price,
                "change": q.change, "change_pct": q.change_pct, "prev_close": q.prev_close,
                "currency": q.currency or t.currency, "market": t.market, "market_state": q.market_state,
                "quote_ts": q.quote_ts, "fetched_at": now, "delay_min": q.delay_min, "exchange": q.exchange,
                "quote_type": q.quote_type, "day_high": q.day_high, "day_low": q.day_low, "volume": q.volume}

    def publish(self, rows: list[dict[str, Any]], transitions: list[dict[str, Any]], now: float) -> int:
        from app.realtime.bus import Envelope  # noqa: PLC0415

        changed = [r for r in rows if self._last_pub.get(r["security_id"]) != (r["price"], r.get("quote_ts"))]
        envs = []
        ts = datetime.fromtimestamp(now, UTC)
        if changed:
            compact = [{k: r.get(k) for k in ("security_id", "symbol", "price", "change", "change_pct", "prev_close",
                                               "currency", "market", "market_state", "quote_ts", "delay_min", "source")}
                       for r in changed]
            envs.append(Envelope(TOPIC_TICK, f"quotes:{int(now * 1000)}", {"quotes": compact, "n": len(compact),
                                                                          "fetched_at": iso(now), "delayed": True},
                                 type="tick", ts_source=ts))
        for tr in transitions:
            envs.append(Envelope(TOPIC_SESSION, f"session:{tr['market']}:{tr['reason']}:{int(now)}", tr,
                                 type="session", ts_source=ts))
        if not envs:
            return 0
        bus = self._bus()
        bus.publish_many(envs)
        for r in changed:
            self._last_pub[r["security_id"]] = (r["price"], r.get("quote_ts"))
        self.published += len(envs)
        self.last_publish_at = now
        return len(changed)

    def maintain_bus(self, now: float) -> None:
        """Release the quotes topics for the realtime retention (lake offset) and prune them to bus_keep_h."""
        if now - self.last_pruned < 600:
            return
        self.last_pruned = now
        bus = self._bus()
        try:
            head = bus.head_id()
            for t in (TOPIC_TICK, TOPIC_SESSION):
                if head:
                    bus.commit("lake", t, head)
                if hasattr(bus, "prune_topic"):
                    bus.prune_topic(t, self.bus_keep_h * (8 if t == TOPIC_SESSION else 1), keep_min=50)
        except Exception as e:  # noqa: BLE001
            log.debug("quotes: bus maintenance failed: %s", e)

    def tick(self) -> dict[str, Any]:
        now = self.clock()
        self.refresh_universe(now)
        targets, transitions = self.due_targets(now)
        rows, rep = self.fetch(targets, now)
        if rows:
            self.store.upsert(rows)
        try:
            self.store.set_markets([{**st.to_dict()} for st in self.market_states.values()])
        except Exception as e:  # noqa: BLE001
            log.debug("quotes: market state write failed: %s", e)
        n_pub = 0
        try:
            n_pub = self.publish(rows, transitions, now)
        except Exception as e:  # noqa: BLE001 - the store still has the quotes; the bus catches up next tick
            log.warning("quotes: bus publish failed: %s", e)
            for r in rows:
                self._last_pub.pop(r["security_id"], None)
        self.maintain_bus(now)
        if not any(st.open for st in self.market_states.values()) and self.feeds["yahoo"].status in ("ok", "starting"):
            self.feeds["yahoo"].status = "idle"
            self.feeds["yahoo"].reason = "all markets in the universe closed" if self.market_states else \
                "nothing to poll (empty universe)"
        elif self.feeds["yahoo"].status == "idle" and any(st.open for st in self.market_states.values()) \
                and self.feeds["yahoo"].reason != f"hourly request budget reached ({self.yahoo_per_hour}/h)":
            self.feeds["yahoo"].status, self.feeds["yahoo"].reason = "ok", None
        return {"due": len(targets), "rows": len(rows), "published": n_pub, "transitions": len(transitions), **rep}

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        now = self.clock()
        rss = None
        try:
            from app.ops.winsys import tree_memory_mb  # noqa: PLC0415

            rss = round(tree_memory_mb(os.getpid()))
        except Exception:  # noqa: BLE001
            pass
        markets = {}
        by_mk = self.universe.by_market()
        for mk, st in sorted(self.market_states.items()):
            markets[mk] = {**st.to_dict(), "n_targets": len(by_mk.get(mk, [])),
                           "reason_text": REASON_TEXT.get(st.reason, st.reason),
                           "next_poll_at": iso(self.next_due.get(mk)) if st.open else None,
                           "every_s": self.market_cadence(st) if st.open else None}
        try:
            store = self.store.stats()
        except Exception as e:  # noqa: BLE001
            store = {"error": str(e)[:200]}
        n_open = sum(1 for st in self.market_states.values() if st.open)
        return {
            "pid": os.getpid(), "host": socket.gethostname(), "rss_mb": rss,
            "started_at": iso(self.started_at), "heartbeat_at": iso(time.time()),
            "state": "ok" if n_open else "idle", "reason": None if n_open else (
                "all markets closed" if self.market_states else "nothing to poll"),
            "universe": self.universe.summary(), "markets": markets, "markets_open": n_open,
            "feeds": {k: f.to_dict(now) for k, f in self.feeds.items()}, "store": store,
            "bus": {"published": self.published, "last_publish_at": iso(self.last_publish_at),
                    "topics": [TOPIC_TICK, TOPIC_SESSION]},
            "cadence": {"open_every_s": self.open_every_s, "tail_every_s": self.tail_every_s,
                        "h24_every_s": self.h24_every_s, "yahoo_per_hour": self.yahoo_per_hour,
                        "polygon_every_s": self.polygon_every_s},
            "loop_errors": self.loop_errors, "last_loop_error": self.last_loop_error, "delayed": True,
        }

    def write_status(self) -> None:
        p = status_path(self.s)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.status(), indent=1, default=str), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:  # pragma: no cover - file in use: next tick
            log.debug("quotes status write failed: %s", e)

    # ------------------------------------------------------------------ loop
    def run(self, duration_s: float | None = None, install_signals: bool = True,
            exit_deadline_s: float | None = None) -> int:
        """The loop. Stop: quotes.STOP (seen within 0.5 s by a watcher thread, alert hygiene 2026-10-30), a signal,
        or `duration_s`. exit_deadline_s (CLI child): hard exit at the latest that long after the stop request."""
        if self.use_lock:
            try:
                self._lock.acquire(timeout=0)
            except Timeout:
                log.error("quotes service already running (%s)", self._lock.lock_file)
                return 2
        from app.ops.shutdown import ChildShutdown  # noqa: PLC0415

        sf = stop_file(self.s)
        sf.unlink(missing_ok=True)   # stale request from an earlier shutdown
        ctl = ChildShutdown("quotes", sf, self.stop_event.set, deadline_s=exit_deadline_s).start()
        if install_signals:
            for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    try:
                        signal.signal(sig, lambda *_: ctl.trigger("signal"))
                    except (ValueError, OSError):
                        pass
        t_end = time.monotonic() + duration_s if duration_s else None
        log.info("quotes service started (open every %.0fs, 24h markets every %.0fs, yahoo budget %d/h)",
                 self.open_every_s, self.h24_every_s, self.yahoo_per_hour)
        try:
            self.refresh_universe(self.clock(), force=True)
            while not self.stop_event.is_set():
                if sf.exists():
                    log.info("quotes.STOP present - stopping")
                    sf.unlink(missing_ok=True)
                    break
                try:
                    rep = self.tick()
                    if rep.get("rows"):
                        log.debug("quotes tick: %s", rep)
                except Exception as e:  # noqa: BLE001 - never break: log, count, keep looping
                    self.loop_errors += 1
                    self.last_loop_error = f"{type(e).__name__}: {e}"[:300]
                    log.exception("quotes tick failed")
                self.write_status()
                if t_end and time.monotonic() >= t_end:
                    break
                self.stop_event.wait(self.tick_s)
        finally:
            if exit_deadline_s:
                ctl.trigger("loop ended")   # arms the exit deadline for the cleanup below
            self.write_status()
            for closer in (self.store, self.bus):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                sf.unlink(missing_ok=True)
            except OSError:
                pass
            if self.use_lock:
                try:
                    self._lock.release(force=True)
                except Exception:  # noqa: BLE001
                    pass
            if not exit_deadline_s:
                ctl.close()          # in-process use: stop the watcher (the CLI child hard-exits next)
            log.info("quotes service stopped")
        return 0
