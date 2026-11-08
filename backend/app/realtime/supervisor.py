"""Realtime supervisor (R1.2 / R1.6): runs the stream workers, the enrichment / alert consumers and the maintenance
loop in one asyncio event loop, with heartbeats, a restart policy, backpressure and graceful shutdown.

Start:  `python -m app.cli realtime start [--streams a,b] [--no-sentiment] [--duration S]` (foreground), or
        `python -m app.cli worker start --realtime` (a thread inside the worker process; default OFF).
Status: `python -m app.cli realtime status`, GET /api/v1/live/status, file data/state/realtime_status.json.

* Single instance: data/state/realtime.lock (OS file lock).
* Backpressure: producers put Envelopes into ONE bounded asyncio.Queue (`queue_max`, default 5000); a single writer
  task drains it in batches (<= 500 or 100 ms) into the bus. When the writer (SQLite) falls behind, producers block
  on put(). Consumers poll the bus in bounded batches and publish synchronously before committing offsets.
* Restart policy: a task that raises is restarted after 5 s x 2^(n-1) (max 5 min, jitter); more than 10 crashes in
  an hour marks it `failed` and parks it for 30 min before the next attempt. Disabled streams (missing key) stay off.
* Maintenance: status file every 5 s (bus stats every 30 s); hourly retention (bus.retention) + lake compaction
  (app.realtime.compact.compact) in a thread; reconcile with the nightly tables every 6 h.
* Shutdown (Ctrl+C / Ctrl+Break / SIGTERM on Windows, the realtime.STOP file, or stop()): stop event -> loops exit
  at their next await, tasks still running after `grace_s` are cancelled, the queue is drained into the bus (at most
  `drain_s`), workers closed (the GDELT parse child is killed), final status written, lock released.
  Alert hygiene 2026-10-30 (app.ops.shutdown): the loop runs on its own bounded default executor and is closed
  without asyncio.run()'s executor join (up to 300 s, then unbounded at interpreter exit: a cancelled to_thread call
  in an HTTP retry loop or waiting on the parse child kept the child alive past the supervisor's 60 s grace).
  The CLI child (`realtime start`) watches the STOP file every 0.5 s, arms an exit deadline (`exit_deadline_s`) at
  the stop request and ends with a hard exit after its cleanup (no thread / pool joins, no torch teardown).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import signal
import socket
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, SqliteBus, get_bus
from app.realtime.streams.base import StreamContext, StreamStats, utcnow

log = get_logger("app.realtime.supervisor")

STATUS_FILE = "realtime_status.json"
DEFAULT_CONSUMERS = ("enrich", "newsdesk", "insider", "changepoint", "alerts", "earnings", "activism")


class RealtimeAlreadyRunning(RuntimeError):
    pass


def status_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / STATUS_FILE


def lock_for(settings: Settings | None = None) -> FileLock:
    s = settings or get_settings()
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(s.state_dir / "realtime.lock"), timeout=0)


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


def consumer_registry() -> dict[str, Any]:
    from app.realtime.activism_live import ActivismConsumer  # noqa: PLC0415
    from app.realtime.alerts_live import AlertConsumer  # noqa: PLC0415
    from app.realtime.changepoint import ChangepointConsumer  # noqa: PLC0415
    from app.realtime.earnings_live import EarningsConsumer  # noqa: PLC0415
    from app.realtime.enrich import EnrichConsumer  # noqa: PLC0415
    from app.realtime.insider_live import InsiderConsumer  # noqa: PLC0415
    from app.news_live.desk import NewsDeskConsumer  # noqa: PLC0415

    return {c.name: c for c in (EnrichConsumer, NewsDeskConsumer, InsiderConsumer, ChangepointConsumer, AlertConsumer,
                                EarningsConsumer, ActivismConsumer)}


def stop_file(settings: Settings | None = None) -> Path:
    """roadmap v3 N2.6: graceful stop request from the ops supervisor (no shared console needed)."""
    return (settings or get_settings()).state_dir / "realtime.STOP"


class Supervisor:
    def __init__(self, settings: Settings | None = None, streams: list[str] | None = None,
                 consumers: list[str] | None = None, stream_params: dict[str, dict[str, Any]] | None = None,
                 consumer_params: dict[str, dict[str, Any]] | None = None, bus: SqliteBus | None = None,
                 queue_max: int = 5000, grace_s: float = 15.0, compact_every_s: float = 3600.0,
                 retention_every_s: float = 3600.0, status_every_s: float = 5.0, workers: list[Any] | None = None,
                 use_lock: bool = True, clock: Any = None, drain_s: float = 10.0, executor_workers: int = 16,
                 exit_deadline_s: float | None = None):
        from app.realtime.streams import registry  # noqa: PLC0415

        self.s = settings or get_settings()
        self.bus = bus or get_bus(self.s)   # type: ignore[assignment]
        self.queue_max = queue_max
        self.grace_s = grace_s
        self.drain_s = drain_s
        self.executor_workers = executor_workers
        #: CLI child only: hard exit this long after the stop request even when the cleanup blocks (app.ops.shutdown)
        self.exit_deadline_s = exit_deadline_s
        self._executor: ThreadPoolExecutor | None = None
        self._stop_requested = False
        self.shutdown_ctl: Any = None
        self.compact_every_s = compact_every_s
        self.retention_every_s = retention_every_s
        self.status_every_s = status_every_s
        self.use_lock = use_lock
        self.clock = clock or utcnow
        sp, cp = stream_params or {}, consumer_params or {}
        if workers is not None:
            self.workers = list(workers)
        else:
            reg = registry()
            names = streams if streams is not None else list(reg)
            unknown = set(names) - set(reg)
            if unknown:
                raise ValueError(f"unknown stream(s): {sorted(unknown)}; known: {sorted(reg)}")
            creg = consumer_registry()
            cnames = consumers if consumers is not None else list(DEFAULT_CONSUMERS)
            self.workers = [reg[n](**sp.get(n, {})) for n in names] + [creg[n](**cp.get(n, {})) for n in cnames]
        self.maint = StreamStats("maintenance", "maintenance")
        self.writer_stats = StreamStats("bus_writer", "maintenance")
        self._lock = lock_for(self.s)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._wakes: list[asyncio.Event] = []
        self._crashes: dict[str, deque] = {}
        self.started_at = time.time()
        self.last_compaction: dict[str, Any] | None = None
        self.last_retention: dict[str, Any] | None = None
        self.last_reconcile: dict[str, Any] | None = None
        self._bus_stats: dict[str, Any] | None = None
        self._bus_stats_at = 0.0
        self.queue: asyncio.Queue | None = None
        self.linker: Any = None

    # ------------------------------------------------------------------ lifecycle
    def acquire(self) -> None:
        if not self.use_lock:
            return
        try:
            self._lock.acquire(timeout=0)
        except Timeout as e:
            raise RealtimeAlreadyRunning(f"realtime supervisor already running ({self._lock.lock_file})") from e

    def release(self) -> None:
        if self.use_lock:
            try:
                self._lock.release(force=True)
            except Exception:  # noqa: BLE001
                pass

    def request_stop(self) -> None:
        self._stop_requested = True
        if self.loop is not None and self.stop_event is not None:
            try:
                self.loop.call_soon_threadsafe(self.stop_event.set)
            except RuntimeError:   # loop already closed
                pass

    # ------------------------------------------------------------------ tasks
    async def _writer(self, ctx: StreamContext, by_name: dict[str, Any]) -> None:
        q = self.queue
        assert q is not None
        while True:
            try:
                first = await asyncio.wait_for(q.get(), timeout=0.5)
            except TimeoutError:
                if ctx.stop.is_set() and q.empty():
                    return
                continue
            batch: list[Envelope] = [first]
            t0 = time.monotonic()
            while len(batch) < 500 and time.monotonic() - t0 < 0.1:
                try:
                    batch.append(q.get_nowait())
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.01)
                    if q.empty():
                        break
            for attempt in range(10):
                try:
                    ids = await asyncio.to_thread(self.bus.publish_many, batch)
                    break
                except Exception as e:  # noqa: BLE001 - SQLite busy etc.: retry, never drop
                    self.writer_stats.on_poll(False, f"{type(e).__name__}: {e}")
                    await asyncio.sleep(min(30.0, 0.5 * 2 ** attempt))
            else:
                log.error("bus writer: dropping %d envelopes after repeated failures", len(batch))
                continue
            now = time.time()
            self.writer_stats.on_poll(True)
            for e, i in zip(batch, ids, strict=True):
                w = by_name.get(e.origin or "")
                if w is not None:
                    w.stats.on_published(e.ts_source, now, i is not None)
                self.writer_stats.on_published(None, now, i is not None)
            for w in self._wakes:
                w.set()

    async def _supervise(self, w: Any, ctx: StreamContext) -> None:
        crashes = self._crashes.setdefault(w.name, deque(maxlen=50))
        n = 0
        while not ctx.stop.is_set():
            try:
                await w.run(ctx)
                if w.stats.status in ("disabled", "stopped") or ctx.stop.is_set():
                    return
                raise RuntimeError("worker loop returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                n += 1
                now = time.time()
                crashes.append(now)
                w.stats.restarts += 1
                w.stats.last_error = f"crash: {type(e).__name__}: {e}"[:300]
                w.stats.last_error_at = now
                recent = sum(1 for t in crashes if now - t < 3600)
                if recent > 10:
                    w.stats.status = "failed"
                    delay = 1800.0
                else:
                    w.stats.status = "backoff"
                    delay = min(300.0, 5.0 * 2 ** (n - 1)) * (0.75 + random.random() / 2)
                log.exception("realtime %s crashed (%d in the last hour); restart in %.0fs", w.name, recent, delay)
                if await ctx.wait(delay):
                    return

    def _status(self) -> dict[str, Any]:
        now = time.time()
        if self._bus_stats is None or now - self._bus_stats_at > 30:
            try:
                self._bus_stats = self.bus.stats()
            except Exception as e:  # noqa: BLE001
                self._bus_stats = {"error": str(e)[:200]}
            self._bus_stats_at = now
        streams = {w.name: w.stats.to_dict() for w in self.workers if w.stats.kind == "stream"}
        consumers = {w.name: {**w.stats.to_dict(), "group": getattr(w, "group", None),
                              "lag": ((self._bus_stats or {}).get("groups", {}).get(getattr(w, "group", ""), {})
                                      .get("lag_total"))}
                     for w in self.workers if w.stats.kind == "consumer"}
        metrics: dict[str, Any] = {}
        for w in self.workers:
            fn = getattr(w, "metrics", None)
            if callable(fn):
                try:
                    metrics[w.name] = fn()
                except Exception as e:  # noqa: BLE001
                    metrics[w.name] = {"error": str(e)[:200]}
            fs = getattr(w, "feed_status", None)
            if callable(fs):
                try:
                    metrics.setdefault("feeds", {}).update(fs())
                except Exception:  # noqa: BLE001
                    pass
        rss = None
        try:
            from app.ops.winsys import tree_memory_mb  # noqa: PLC0415

            rss = round(tree_memory_mb(os.getpid()))
        except Exception:  # noqa: BLE001
            pass
        return {
            "pid": os.getpid(), "host": socket.gethostname(), "rss_mb": rss, "metrics": metrics,
            "started_at": datetime.fromtimestamp(self.started_at, UTC).isoformat(timespec="seconds"),
            "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "streams": streams, "consumers": consumers,
            "maintenance": self.maint.to_dict(), "writer": self.writer_stats.to_dict(),
            "queue": {"size": self.queue.qsize() if self.queue else 0, "max": self.queue_max},
            "bus": self._bus_stats, "last_compaction": self.last_compaction, "last_retention": self.last_retention,
            "reconcile": self.last_reconcile,
        }

    def write_status(self) -> None:
        p = status_path(self.s)
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._status(), indent=1, default=str), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:  # pragma: no cover - Windows file in use: next tick
            log.debug("status write failed: %s", e)

    async def _maintenance(self, ctx: StreamContext) -> None:
        from app.realtime.compact import compact, reconcile  # noqa: PLC0415

        last_comp = time.time()          # first compaction after one interval (or on stop)
        last_ret = time.time() - self.retention_every_s + 300
        last_rec = time.time() - 6 * 3600 + 600
        self.maint.status = "ok"
        sf = stop_file(self.s)
        if sf.exists():
            sf.unlink(missing_ok=True)   # stale request from an earlier shutdown
        while not ctx.stop.is_set():
            if sf.exists():
                log.info("realtime.STOP present - stopping")
                sf.unlink(missing_ok=True)
                ctx.stop.set()
                break
            self.write_status()
            now = time.time()
            try:
                if now - last_ret >= self.retention_every_s:
                    last_ret = now
                    self.last_retention = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                           **(await asyncio.to_thread(self.bus.retention))}
                if now - last_comp >= self.compact_every_s:
                    last_comp = now
                    t0 = time.perf_counter()
                    rep = await asyncio.to_thread(compact, self.bus, self.s)
                    self.last_compaction = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                            "seconds": round(time.perf_counter() - t0, 1), "tables": rep}
                    log.info("realtime compaction: %s", rep)
                if now - last_rec >= 6 * 3600:
                    last_rec = now
                    self.last_reconcile = {"at": datetime.now(UTC).isoformat(timespec="seconds"),
                                           **(await asyncio.to_thread(reconcile, self.s))}
                self.maint.on_poll(True)
            except Exception as e:  # noqa: BLE001
                self.maint.on_poll(False, f"{type(e).__name__}: {e}")
                log.warning("realtime maintenance failed: %s", e)
            if await ctx.wait(self.status_every_s):
                break

    async def main(self, duration_s: float | None = None) -> None:
        from app.realtime.link import Linker  # noqa: PLC0415

        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        self.queue = asyncio.Queue(maxsize=self.queue_max)
        self.linker = Linker(self.s)
        base = StreamContext(settings=self.s, bus=self.bus, out=self.queue, stop=self.stop_event, clock=self.clock,
                             linker=self.linker)
        by_name = {w.name: w for w in self.workers}
        tasks: list[asyncio.Task] = []
        writer = asyncio.create_task(self._writer(base, by_name), name="bus_writer")
        maint = asyncio.create_task(self._maintenance(base), name="maintenance")
        for w in self.workers:
            ctx = base
            if w.stats.kind == "consumer":
                ev = asyncio.Event()
                self._wakes.append(ev)
                ctx = replace(base, wake=ev)
            tasks.append(asyncio.create_task(self._supervise(w, ctx), name=w.name))
        log.info("realtime supervisor started: %s", ", ".join(w.name for w in self.workers))
        if self._stop_requested:          # a stop request that arrived before the loop existed
            self.stop_event.set()
        try:
            if duration_s:
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=duration_s)
                except TimeoutError:
                    pass
            else:
                await self.stop_event.wait()
        finally:
            self.stop_event.set()
            if self.shutdown_ctl is not None:     # CLI child: exit deadline from the first stop request on
                self.shutdown_ctl.trigger("main loop stopping")
            log.info("realtime supervisor stopping (grace %.0fs)", self.grace_s)
            t_stop = time.monotonic()
            done, pending = await asyncio.wait(tasks, timeout=self.grace_s)
            for t in pending:
                t.cancel()
            if pending:   # bounded: a cancelled to_thread() returns at once, its thread is abandoned (see run())
                _, still = await asyncio.wait(pending, timeout=5.0)
                if still:
                    log.warning("realtime: %d task(s) did not finish after cancel: %s", len(still),
                                ", ".join(sorted(t.get_name() for t in still)))
            try:
                await asyncio.wait_for(writer, timeout=self.drain_s)   # drains the queue
            except TimeoutError:
                writer.cancel()
                log.warning("realtime: bus writer did not drain the queue within %.0fs (%d envelope(s) left)",
                            self.drain_s, self.queue.qsize() if self.queue else 0)
            maint.cancel()
            await asyncio.wait([maint], timeout=5.0)
            for w in self.workers:
                if w.stats.status not in ("disabled", "failed"):
                    w.stats.status = "stopped"
                close = getattr(w, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception as e:  # noqa: BLE001
                        log.debug("close %s: %s", w.name, e)
                flush = getattr(w, "flush", None)
                if callable(flush):
                    try:
                        flush(base)
                    except Exception as e:  # noqa: BLE001
                        log.debug("flush %s: %s", w.name, e)
            self.write_status()
            log.info("realtime supervisor stopped (%.1fs after the stop request)", time.monotonic() - t_stop)

    # ------------------------------------------------------------------ entry points
    def _run_loop(self, coro_fn: Any) -> None:
        """asyncio.run() with a bounded shutdown (alert hygiene 2026-10-30): an own default executor, and at the end
        remaining tasks are cancelled with a 5 s limit and the executor is shut down WITHOUT joining busy threads
        (asyncio.run() would wait up to 300 s in shutdown_default_executor, then the interpreter joins them again
        without a limit). Busy threads are logged; the CLI child then hard-exits (app.ops.shutdown.finish_child)."""
        from app.ops.shutdown import shutdown_executor  # noqa: PLC0415

        loop = asyncio.new_event_loop()
        ex = ThreadPoolExecutor(max_workers=self.executor_workers, thread_name_prefix="rt")
        loop.set_default_executor(ex)
        self._executor = ex
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(coro_fn())
        finally:
            try:
                rest = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in rest:
                    t.cancel()
                if rest:
                    loop.run_until_complete(asyncio.wait(rest, timeout=5.0))
                try:
                    loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), 5.0))
                except Exception:  # noqa: BLE001
                    pass
                left = shutdown_executor(ex, wait_s=2.0)
                if left:
                    log.warning("realtime: %d executor thread(s) still busy at shutdown (abandoned, no join): %s",
                                len(left), ", ".join(left[:12]))
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    def kill_children(self) -> int:
        """Stop the workers' child processes now (the GDELT parse pool): used at shutdown."""
        n = 0
        for w in self.workers:
            fn = getattr(w, "kill_children", None)
            if callable(fn):
                try:
                    n += int(fn() or 0)
                except Exception as e:  # noqa: BLE001
                    log.debug("kill_children %s: %s", w.name, e)
        return n

    def run(self, duration_s: float | None = None, install_signals: bool = True) -> int:
        """Foreground (CLI). Blocks until Ctrl+C / Ctrl+Break / SIGTERM, the STOP file or `duration_s`."""
        self.acquire()
        try:
            if install_signals:
                def on_signal(*_: Any) -> None:
                    if self.shutdown_ctl is not None:
                        self.shutdown_ctl.trigger("signal")
                    else:
                        self.request_stop()

                for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
                    sig = getattr(signal, name, None)
                    if sig is not None:
                        try:
                            signal.signal(sig, on_signal)
                        except (ValueError, OSError):
                            pass
            if self.exit_deadline_s:
                from app.ops.shutdown import ChildShutdown  # noqa: PLC0415

                self.shutdown_ctl = ChildShutdown("realtime", stop_file(self.s), self.request_stop,
                                                  deadline_s=self.exit_deadline_s).start()
            self._run_loop(lambda: self.main(duration_s))
            n = self.kill_children()
            if n:
                log.info("realtime: %d child process(es) killed at shutdown", n)
            return 0
        finally:
            self.release()

    def start_in_thread(self) -> threading.Thread:
        """Worker integration: own event loop in a daemon thread; stop with stop()."""
        self.acquire()

        def target() -> None:
            try:
                self._run_loop(self.main)
            except Exception:  # noqa: BLE001
                log.exception("realtime supervisor thread crashed")
            finally:
                self.kill_children()
                self.release()

        self._thread = threading.Thread(target=target, name="realtime-supervisor", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 60.0) -> None:
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout)


def parse_kv_params(items: list[str] | None) -> dict[str, dict[str, Any]]:
    """['edgar.interval_s=20', 'gdelt.translation=true'] -> {'edgar': {'interval_s': 20.0}, ...}."""
    out: dict[str, dict[str, Any]] = {}
    for it in items or []:
        k, _, v = it.partition("=")
        name, _, key = k.partition(".")
        val: Any = v
        if v.lower() in ("true", "false"):
            val = v.lower() == "true"
        else:
            try:
                val = float(v) if "." in v else int(v)
            except ValueError:
                val = v
        out.setdefault(name, {})[key] = val
    return out


_ = Envelope
