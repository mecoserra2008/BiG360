"""Consumer base (R1.3): a consumer group over bus topics with at-least-once processing.

Loop: poll(group, topics, batch) -> handle_batch(msgs) in a worker thread -> publish the outputs (synchronously, so
they are durable) -> commit the offsets. A message whose handler raises is nacked: it is retried with backoff and,
after `max_attempts`, dead-lettered (the offset moves past it). Messages before the failing one in the same batch
are committed; nothing after it is (ordering per group is preserved). Idle consumers wait on the supervisor's wake
event (set after every bus write) or `idle_s`.
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.streams.base import StreamContext, StreamStats

log = get_logger(__name__)


def ts_of(msg: Message) -> datetime | None:
    if not msg.ts_source:
        return None
    try:
        return datetime.fromisoformat(msg.ts_source.replace("Z", "+00:00"))
    except ValueError:
        return None


class HandlerError(RuntimeError):
    def __init__(self, index: int, cause: BaseException):
        super().__init__(f"message #{index}: {type(cause).__name__}: {cause}")
        self.index = index
        self.cause = cause


class Consumer:
    name = "consumer"
    group = "consumer"
    topics: tuple[str, ...] = ()
    batch = 200
    max_attempts = 5
    idle_s = 2.0

    def __init__(self, **params: Any):
        self.params = params
        self.stats = StreamStats(self.name, "consumer")
        self.stats.status = "starting"

    def enabled(self, ctx: StreamContext) -> tuple[bool, str | None]:
        return True, None

    async def setup(self, ctx: StreamContext) -> None:
        return None

    # ---- override one of these
    def handle_one(self, ctx: StreamContext, msg: Message) -> list[Envelope]:
        return []

    def handle_batch(self, ctx: StreamContext, msgs: list[Message]) -> list[Envelope]:
        """Default: per message; raises HandlerError(i) on the first failing message (outputs of 0..i-1 attached)."""
        out: list[Envelope] = []
        for i, m in enumerate(msgs):
            try:
                out += self.handle_one(ctx, m)
            except Exception as e:  # noqa: BLE001
                err = HandlerError(i, e)
                err.partial = out  # type: ignore[attr-defined]
                raise err from e
        return out

    def on_idle(self, ctx: StreamContext) -> list[Envelope]:
        """Periodic hook when nothing is pending (flush state, time-based outputs)."""
        return []

    # ---- loop
    async def _publish(self, ctx: StreamContext, envs: list[Envelope]) -> None:
        if not envs:
            return
        for e in envs:
            e.origin = e.origin or self.name
        ids = await asyncio.to_thread(ctx.bus.publish_many, envs)
        import time as _t  # noqa: PLC0415

        now = _t.time()
        for e, i in zip(envs, ids, strict=True):
            self.stats.on_published(e.ts_source, now, i is not None)
        if ctx.wake is not None:
            ctx.wake.set()

    async def run(self, ctx: StreamContext) -> None:
        ok, why = self.enabled(ctx)
        if not ok:
            self.stats.status, self.stats.detail = "disabled", why
            return
        await self.setup(ctx)
        self.stats.status = "ok"
        while not ctx.stop.is_set():
            msgs = await asyncio.to_thread(ctx.bus.poll, self.group, list(self.topics), self.batch)
            if not msgs:
                self.stats.status = "idle" if self.stats.status != "backoff" else self.stats.status
                try:
                    await self._publish(ctx, await asyncio.to_thread(self.on_idle, ctx))
                except Exception as e:  # noqa: BLE001
                    log.warning("consumer %s idle hook failed: %s", self.name, e)
                if ctx.wake is not None:
                    ctx.wake.clear()
                    try:
                        await asyncio.wait_for(ctx.wake.wait(), timeout=self.idle_s)
                    except TimeoutError:
                        pass
                elif await ctx.wait(self.idle_s):
                    break
                continue
            try:
                out = await asyncio.to_thread(self.handle_batch, ctx, msgs)
                await self._publish(ctx, out)
                await asyncio.to_thread(ctx.bus.commit_many, self.group, msgs)
                self.stats.on_poll(True)
                self.stats.status = "ok"
            except HandlerError as he:
                done, bad = msgs[:he.index], msgs[he.index]
                await self._publish(ctx, getattr(he, "partial", []))
                if done:
                    await asyncio.to_thread(ctx.bus.commit_many, self.group, done)
                dead = await asyncio.to_thread(ctx.bus.nack, self.group, bad, str(he.cause), self.max_attempts)
                self.stats.on_poll(False, str(he))
                if not dead:
                    n = ctx.bus.attempts(self.group, bad.id) if hasattr(ctx.bus, "attempts") else 1
                    delay = min(300.0, 2.0 * (2 ** max(0, n - 1))) * (0.75 + random.random() / 2)
                    log.warning("consumer %s: message %s failed (attempt %d), retry in %.0fs: %s", self.name, bad.id,
                                n, delay, he.cause)
                    if await ctx.wait(delay):
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - publish/commit failures: retry the whole batch later
                self.stats.on_poll(False, f"{type(e).__name__}: {e}")
                log.warning("consumer %s batch failed: %s", self.name, e)
                if await ctx.wait(10.0):
                    break
        self.stats.status = "stopped"
