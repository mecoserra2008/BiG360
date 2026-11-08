"""Stream worker base (R1.2): a small async polling loop with heartbeats, exponential backoff and metrics.

A worker implements `poll(ctx) -> list[Envelope]` (blocking HTTP runs in a thread via `ctx.run_sync`) and optionally
`interval(ctx)` (seconds until the next poll; may depend on the clock, e.g. market hours or a release schedule).
Envelopes go into the supervisor's bounded queue (`ctx.out`): when the single bus writer falls behind, `put()`
blocks and the producers slow down (backpressure). Idempotent keys make re-polls and restarts harmless.

Metrics per stream (StreamStats): status, polls, errors / consecutive errors / last error, events published (new
keys only), duplicates, throughput over the last hour, last event's source and ingest time, lag = ingest - source
publish time (p50 / p95 / last), next poll time, restarts.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, SqliteBus

log = get_logger(__name__)


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class StreamStats:
    name: str
    kind: str = "stream"           # stream | consumer | maintenance
    status: str = "starting"       # starting | ok | idle | backoff | disabled | failed | stopped
    detail: str | None = None
    started_at: float = field(default_factory=time.time)
    last_poll_at: float | None = None
    last_ok_at: float | None = None
    next_poll_at: float | None = None
    polls: int = 0
    errors: int = 0
    consecutive_errors: int = 0
    last_error: str | None = None
    last_error_at: float | None = None
    events: int = 0
    duplicates: int = 0
    last_event_at: float | None = None          # ingest time of the newest published event
    last_event_source_ts: str | None = None     # source publish time of that event
    restarts: int = 0
    _recent: deque = field(default_factory=lambda: deque(maxlen=20000))   # ingest epochs of published events
    _lags: deque = field(default_factory=lambda: deque(maxlen=500))       # ingest - source seconds (fresh events)

    def on_poll(self, ok: bool, err: str | None = None) -> None:
        now = time.time()
        self.polls += 1
        self.last_poll_at = now
        if ok:
            self.last_ok_at = now
            self.consecutive_errors = 0
            if self.status in ("starting", "backoff"):
                self.status = "ok"
        else:
            self.errors += 1
            self.consecutive_errors += 1
            self.last_error = (err or "")[:300]
            self.last_error_at = now
            self.status = "backoff"

    def on_published(self, ts_source: datetime | None, ingest: float, new: bool) -> None:
        if not new:
            self.duplicates += 1
            return
        self.events += 1
        self._recent.append(ingest)
        self.last_event_at = ingest
        if ts_source is not None:
            self.last_event_source_ts = ts_source.astimezone(UTC).isoformat(timespec="seconds")
            lag = ingest - ts_source.timestamp()
            if -120 < lag < 6 * 3600:   # only fresh items say something about pipeline latency
                self._lags.append(max(lag, 0.0))

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        while self._recent and now - self._recent[0] > 3600:
            self._recent.popleft()
        lags = sorted(self._lags)

        def iso(x: float | None) -> str | None:
            return datetime.fromtimestamp(x, UTC).isoformat(timespec="seconds") if x else None

        return {
            "name": self.name, "kind": self.kind, "status": self.status, "detail": self.detail,
            "started_at": iso(self.started_at), "last_poll_at": iso(self.last_poll_at),
            "last_ok_at": iso(self.last_ok_at), "next_poll_at": iso(self.next_poll_at), "polls": self.polls,
            "errors": self.errors, "consecutive_errors": self.consecutive_errors, "last_error": self.last_error,
            "last_error_at": iso(self.last_error_at), "events_total": self.events, "duplicates": self.duplicates,
            "events_1h": len(self._recent), "throughput_per_min": round(len(self._recent) / 60.0, 3),
            "last_event_at": iso(self.last_event_at), "last_event_source_ts": self.last_event_source_ts,
            "freshness_s": round(now - self.last_event_at, 1) if self.last_event_at else None,
            "lag_s_last": round(self._lags[-1], 1) if self._lags else None,
            "lag_s_p50": round(statistics.median(lags), 1) if lags else None,
            "lag_s_p95": round(lags[min(len(lags) - 1, int(0.95 * len(lags)))], 1) if lags else None,
            "restarts": self.restarts,
        }


@dataclass
class StreamContext:
    settings: Settings
    bus: SqliteBus
    out: asyncio.Queue            # bounded queue of Envelope (backpressure)
    stop: asyncio.Event
    clock: Callable[[], datetime] = utcnow
    linker: Any = None
    wake: asyncio.Event | None = None     # set after each bus write (wakes consumers)
    sleep: Callable[[float], Any] | None = None

    async def run_sync(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kw)

    async def wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; returns True when stopping."""
        if self.sleep is not None:     # tests: fake clock
            await self.sleep(seconds)
            return self.stop.is_set()
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=max(0.0, seconds))
            return True
        except TimeoutError:
            return False


class StreamWorker:
    """Base class: subclasses set name / topic / source_id / interval_s and implement poll()."""

    name = "base"
    kind = "stream"
    topic = "raw.news"
    source_id = ""
    interval_s = 60.0
    max_backoff_s = 900.0

    def __init__(self, **params: Any):
        self.params = params
        self.stats = StreamStats(self.name, self.kind)

    def enabled(self, ctx: StreamContext) -> tuple[bool, str | None]:
        return True, None

    async def setup(self, ctx: StreamContext) -> None:
        return None

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        raise NotImplementedError

    def interval(self, ctx: StreamContext) -> float:
        return float(self.params.get("interval_s", self.interval_s))

    def backoff(self) -> float:
        n = self.stats.consecutive_errors
        base = max(5.0, min(float(self.params.get("interval_s", self.interval_s)), 60.0)) if n else 0.0
        return min(self.max_backoff_s, base * (2 ** max(0, n - 1))) * (0.75 + random.random() / 2)

    async def emit(self, ctx: StreamContext, items: list[Envelope]) -> None:
        for e in items:
            e.origin = e.origin or self.name
            await ctx.out.put(e)   # blocks when the writer is behind (backpressure)

    async def run(self, ctx: StreamContext) -> None:
        ok, why = self.enabled(ctx)
        if not ok:
            self.stats.status, self.stats.detail = "disabled", why
            log.info("realtime stream %s disabled: %s", self.name, why)
            return
        await self.setup(ctx)
        while not ctx.stop.is_set():
            try:
                items = await self.poll(ctx)
                self.stats.on_poll(True)
                if items:
                    await self.emit(ctx, items)
                delay = self.interval(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - keep the loop alive, back off
                self.stats.on_poll(False, f"{type(e).__name__}: {e}")
                delay = self.backoff()
                log.warning("realtime stream %s poll failed (%d in a row), retry in %.0fs: %s", self.name,
                            self.stats.consecutive_errors, delay, e)
            self.stats.next_poll_at = time.time() + delay
            if await ctx.wait(delay):
                break
        self.stats.status = "stopped"
