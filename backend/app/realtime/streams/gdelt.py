"""GDELT 2.0 15-minute updates (R1.2, roadmap v3 N2.2) -> GDELT hot slot store + raw.events.

Every `interval_s` (60 s) the worker reads lastupdate.txt and lastupdate-translation.txt (2 small requests on the
shared `gdelt_files` bucket). For every new 15-min slot (at most `max_backfill_slots` behind, so a restart after a
short outage catches up) it downloads the six files of the slot - export, mentions, gkg and their translingual
twins - INTO MEMORY and parses them with the events_v2 parsers (app.events_v2.parse.parse_file, legacy outputs
included) in ONE recycled child process (`max_tasks_per_child` files, then a fresh process: the parse's transient
memory never accumulates in the realtime service; the child is also shut down after each poll that parsed files).
The labelling context is the cached events_v2 pickle
(data/state/events_v2/worker_ctx.pkl, refreshed by build_context when older than 20 h).

Outputs
* hot store (app.news_live.hot): the parsed frames of every file + a slot manifest. They are folded into the lake
  hourly (job news_hourly) and finalised per day (job news_finalise), and the nightly gdelt_stream / deep backfill /
  gap filler read them instead of downloading the slot again (Streamer.fetch_day).
* raw.events (unchanged contract): `article` (labelled GKG articles with a title, issuer-linked first, cap
  `max_articles`), `event` (salient market-relevant CAMEO events, cap `max_events`) and the `slot` summary that feeds
  the online change-point detector.
ts_source = GDELT's publication time of the slot files (HTTP Last-Modified). Metrics: release -> hot store lag per
file (`release_lag_s` p50 / p95 in the stream detail and the news metrics file).
Translation files are often published a few minutes after the English ones: a slot's translation kinds are retried
on every poll for `translation_wait_min` (30) minutes, then recorded as missing.
"""

from __future__ import annotations

import os
import statistics
import time
from collections import Counter, deque
from datetime import UTC, datetime, timedelta
from typing import Any

import polars as pl

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope
from app.realtime.streams.base import StreamContext, StreamWorker

log = get_logger(__name__)

EN_KINDS = ("export", "mentions", "gkg")
TR_KINDS = ("translation.export", "translation.mentions", "translation.gkg")


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


#: polars threads in the parse child: the realtime service must stay light next to the app (measured: 2.4 CPU-s per
#: GKG file v2 + 16 CPU-s for the legacy outputs with 2 threads; unbounded threads only add contention)
PARSE_THREADS = "2"


def _init_live_parser(ctx_path: str) -> None:
    """Parse child initializer: below-normal CPU priority (Windows), then the events_v2 worker context."""
    import signal  # noqa: PLC0415
    import sys  # noqa: PLC0415

    # alert hygiene 2026-10-30 / incident 2026-10-24: the parse child shares the realtime child's console; a console
    # CTRL+C / CTRL+BREAK must never run the default handler's ExitProcess in it (the 10-24 stuck-in-exit pattern).
    # Its lifetime belongs to the parent (pool shutdown / kill, the realtime Job Object).
    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, signal.SIG_IGN)
            except (ValueError, OSError):  # pragma: no cover
                pass
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            k = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL_PRIORITY_CLASS
        except Exception:  # noqa: BLE001  # pragma: no cover
            pass
    from app.events_v2.parse import init_worker  # noqa: PLC0415

    init_worker(ctx_path)


class GdeltStream(StreamWorker):
    name = "gdelt"
    topic = "raw.events"
    source_id = "gdelt_files"
    interval_s = 60.0

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.http: Any = None
        self.pool: Any = None
        self.ctx_path: Any = None
        self.ctx_mtime: float | None = None
        self.ctx_checked = 0.0
        self.local_ctx: Any = None
        self._iid: dict[int, str] = {}
        self.max_articles = int(params.get("max_articles", 1500))
        self.max_events = int(params.get("max_events", 300))
        self.max_backfill_slots = int(params.get("max_backfill_slots", 8))
        self.translation = bool(params.get("translation", True))
        self.translation_wait = timedelta(minutes=float(params.get("translation_wait_min", 30)))
        self.missing_after = timedelta(minutes=float(params.get("missing_after_min", 45)))
        self.in_child = bool(params.get("parse_in_child", True))
        self.max_tasks_per_child = int(params.get("max_tasks_per_child", 24))
        self.hot = bool(params.get("hot", True))
        self.done: set[str] = set()                    # slots whose English part was emitted
        self.first_seen: dict[str, float] = {}         # slot stamp -> when we first saw it in lastupdate
        self.release_lags: deque[float] = deque(maxlen=400)
        self.files = Counter()
        self.last_slot: datetime | None = None
        self.stats.detail = "GDELT 2.0 export + mentions + GKG (+ translingual) -> hot store, events_v2 labelling"

    # ------------------------------------------------------------------ setup
    def client(self) -> Any:
        if self.http is None:
            from app.core.http import HttpClient  # noqa: PLC0415

            self.http = HttpClient("gdelt_files", cache=False, max_retries=3, timeout=120)
        return self.http

    def ensure_context(self, ctx: StreamContext) -> None:
        """Cached events_v2 labelling context (rebuilt by build_context when > 20 h old) + issuer id map."""
        now = time.time()
        if self.ctx_path is not None and now - self.ctx_checked < 6 * 3600:
            return
        self.ctx_checked = now
        from app.events_v2 import dicts  # noqa: PLC0415
        from app.events_v2.context import build_context, issuer_meta, state_dir  # noqa: PLC0415

        p = state_dir(ctx.settings) / "worker_ctx.pkl"
        if not p.exists() or now - p.stat().st_mtime > 20 * 3600:
            wctx, p = build_context(ctx.settings, refresh=False)
            if not self.in_child:
                self.local_ctx = wctx
        mt = p.stat().st_mtime
        if mt != self.ctx_mtime:
            self.ctx_path, self.ctx_mtime = p, mt
            self._recycle_pool()
            if not self.in_child:
                import pickle  # noqa: PLC0415

                with open(p, "rb") as fh:
                    self.local_ctx = pickle.load(fh)  # noqa: S301 - own file
        try:
            im = issuer_meta(dicts.load_dict("issuer", ctx.settings))
            self._iid = dict(zip(im.get_column("iid").to_list(), im.get_column("issuer_id").to_list(), strict=True))
        except Exception as e:  # noqa: BLE001
            log.warning("gdelt live: issuer dictionary unavailable: %s", e)

    def _recycle_pool(self, kill: bool = False) -> int:
        """Drop the parse pool. kill=True (shutdown): its process is killed at once - a running parse is abandoned,
        so neither the parse thread waiting on .result() nor the interpreter's pool join at exit can block."""
        pool, self.pool = self.pool, None
        if pool is None:
            return 0
        if kill:
            from app.ops.shutdown import kill_process_pool  # noqa: PLC0415

            return kill_process_pool(pool)
        pool.shutdown(wait=False, cancel_futures=True)
        return 0

    def kill_children(self) -> int:
        return self._recycle_pool(kill=True)

    def _pool(self) -> Any:
        if self.pool is None:
            import multiprocessing as mp  # noqa: PLC0415
            import os  # noqa: PLC0415
            from concurrent.futures import ProcessPoolExecutor  # noqa: PLC0415

            os.environ["POLARS_MAX_THREADS"] = str(self.params.get("parse_threads", PARSE_THREADS))  # inherited by the child
            self.pool = ProcessPoolExecutor(1, mp_context=mp.get_context("spawn"), initializer=_init_live_parser,
                                            initargs=(str(self.ctx_path),), max_tasks_per_child=self.max_tasks_per_child)
        return self.pool

    def parse(self, kind: str, data: bytes) -> tuple[Any, dict[str, Any], Any]:
        from app.events_v2 import parse as ps  # noqa: PLC0415

        if self.in_child:
            try:
                _k, res, st, old = self._pool().submit(ps.parse_file, kind, data, True).result(timeout=600)
            except Exception:
                self._recycle_pool()          # broken pool (child killed): fresh one next time
                raise
            return res, st, old
        ps._CTX = self.local_ctx  # in-process (tests)
        _k, res, st, old = ps.parse_file(kind, data, True)
        return res, st, old

    # ------------------------------------------------------------------ fetch
    def latest(self, translation: bool = False) -> datetime | None:
        from app.ingestion.gdelt_events import BASE, parse_lastupdate  # noqa: PLC0415

        name = "lastupdate-translation.txt" if translation else "lastupdate.txt"
        return parse_lastupdate(self.client().get(f"{BASE}/{name}", cache_ttl=0).text)

    def _get(self, url: str) -> tuple[bytes | None, datetime | None]:
        """-> (content, Last-Modified = GDELT's publication time of the file); (None, None) on 403 / 404."""
        from email.utils import parsedate_to_datetime  # noqa: PLC0415

        from app.core.http import HttpError  # noqa: PLC0415

        try:
            r = self.client().get(url, cache_ttl=0)
        except HttpError as e:
            if e.status in (403, 404):
                return None, None
            raise
        lm = {k.lower(): v for k, v in (r.headers or {}).items()}.get("last-modified")
        try:
            pub = parsedate_to_datetime(lm).astimezone(UTC) if lm else None
        except (TypeError, ValueError):
            pub = None
        return r.content, pub

    def process_kind(self, ctx: StreamContext, slot: datetime, kind: str, now: datetime, fresh: bool = True) \
            -> tuple[str, Any, datetime | None]:
        """Download + parse + persist one file. Returns (status ok | missing | pending, v2 frame, released)."""
        from app.ingestion.gdelt_events import slot_url  # noqa: PLC0415
        from app.news_live import hot  # noqa: PLC0415

        data, pub = self._get(slot_url(slot, kind))
        if data is None:
            wait = self.translation_wait if kind.startswith("translation.") else self.missing_after
            if now - slot > wait:
                if self.hot:
                    hot.write_slot_kind(slot, kind, None, None, "missing", None, ctx.settings)
                self.files["missing"] += 1
                return "missing", None, None
            return "pending", None, None
        res, st, old = self.parse(kind, data)
        self.files["parsed"] += 1
        if self.hot:
            hot.write_slot_kind(slot, kind, res, old, "ok", pub, ctx.settings, st)
        if pub is not None and fresh:   # catch-up slots (after a restart) say nothing about the live latency
            lag = (datetime.now(UTC) - pub).total_seconds()
            if -60 < lag < 3600:
                self.release_lags.append(max(0.0, lag))
        return "ok", res, pub

    def slot_todo(self, slot: datetime, ctx: StreamContext) -> list[str]:
        from app.news_live import hot  # noqa: PLC0415

        man = hot.read_manifest(slot, ctx.settings) if self.hot else {}
        kinds = list(EN_KINDS) + (list(TR_KINDS) if self.translation else [])
        return [k for k in kinds if (man.get(k) or {}).get("status") not in ("ok", "missing")]

    def collect(self, ctx: StreamContext) -> list[Envelope]:
        self.ensure_context(ctx)
        latest = self.latest()
        if latest is None:
            return []
        lat_tr = None
        if self.translation:
            try:
                lat_tr = self.latest(translation=True)
            except Exception as e:  # noqa: BLE001 - translation index down: English continues
                log.debug("gdelt translation lastupdate failed: %s", e)
        now = ctx.clock()
        slots = [latest - timedelta(minutes=15 * i) for i in range(self.max_backfill_slots, -1, -1)]
        out: list[Envelope] = []
        from app.news_live import hot  # noqa: PLC0415

        for s in slots:
            st = hot.stamp(s)
            todo = self.slot_todo(s, ctx)
            if not todo and st in self.done:
                continue
            if not todo:   # complete in the hot store from a previous run (restart): nothing to emit again
                self.done.add(st)
                continue
            frames: dict[str, Any] = {}
            pubs: list[datetime] = []
            for k in todo:
                if k.startswith("translation.") and (lat_tr is None or s > lat_tr) and now - s <= self.translation_wait:
                    continue  # not announced yet: retry next poll
                try:
                    status, res, pub = self.process_kind(ctx, s, k, now, fresh=s >= latest - timedelta(minutes=15))
                except Exception as e:  # noqa: BLE001 - one bad file must not block the slot
                    log.warning("gdelt live %s %s failed: %s", st, k, e)
                    self.files["errors"] += 1
                    continue
                if status == "ok":
                    frames[k] = res
                    if pub is not None:
                        pubs.append(pub)
            arts = [frames[k] for k in ("gkg", "translation.gkg") if k in frames]
            evs = [frames[k] for k in ("export", "translation.export") if k in frames]
            first = st not in self.done and "gkg" in frames
            art = pl.concat(arts, how="vertical_relaxed") if arts else None
            ev = pl.concat(evs, how="vertical_relaxed") if evs else None
            if art is not None or ev is not None:
                try:
                    out += self.envelopes(s, art, ev, max(pubs) if pubs else None, summary=first)
                except Exception as e:  # noqa: BLE001 - the hot files are written; never lose the slot's other output
                    log.warning("gdelt live %s envelopes failed: %s", st, e)
                    self.files["errors"] += 1
            if first or not [k for k in self.slot_todo(s, ctx) if k in EN_KINDS]:
                self.done.add(st)
            self.last_slot = max(self.last_slot or s, s)
        if self.files["parsed"] != getattr(self, "_parsed_seen", 0):
            # the parse child's transient memory never accumulates in the service: a fresh process per poll that
            # parsed files (spawn + context load ~ 3-5 s, once per 15-min slot)
            self._parsed_seen = self.files["parsed"]
            self._recycle_pool()
        # bounded memory: forget slots older than the backfill window
        cutoff = hot.stamp(latest - timedelta(minutes=15 * (self.max_backfill_slots + 4)))
        self.done = {x for x in self.done if x >= cutoff}
        lags = sorted(self.release_lags)
        p50 = statistics.median(lags) if lags else None
        p95 = lags[min(len(lags) - 1, int(0.95 * len(lags)))] if lags else None
        self.stats.detail = (f"last slot {self.last_slot:%H:%M} UTC; files parsed {self.files['parsed']}, missing "
                             f"{self.files['missing']}, errors {self.files['errors']}; release->hot p50 "
                             f"{p50 if p50 is None else round(p50)} s p95 {p95 if p95 is None else round(p95)} s"
                             if self.last_slot else self.stats.detail)
        return out

    def metrics(self) -> dict[str, Any]:
        lags = sorted(self.release_lags)
        return {"files": dict(self.files), "last_slot": _iso(self.last_slot) if self.last_slot else None,
                "release_lag_s_p50": round(statistics.median(lags), 1) if lags else None,
                "release_lag_s_p95": round(lags[min(len(lags) - 1, int(0.95 * len(lags)))], 1) if lags else None,
                "n": len(lags), "pid": os.getpid()}

    # ------------------------------------------------------------------ envelopes (unchanged contract)
    def envelopes(self, slot: datetime, art: pl.DataFrame | None, ev: pl.DataFrame | None,
                  published: datetime | None = None, summary: bool = True) -> list[Envelope]:
        from app.events_v2 import taxonomy as tx  # noqa: PLC0415

        cid = {i: c for c, i in tx.COUNTRY_ID.items()}
        out: list[Envelope] = []
        th_art: Counter = Counter()
        th_ev: Counter = Counter()
        cty: Counter = Counter()
        n_linked = 0
        stamp = slot.strftime("%Y%m%d%H%M%S")
        src_ts = published or slot
        if art is not None and art.height:
            a = art.filter(pl.col("title").is_not_null()).with_columns(
                (pl.col("issuer_ids").list.len() > 0).alias("__l"),
                pl.col("tone_x100").cast(pl.Int32).abs().fill_null(0).alias("__t"))
            for m in art.get_column("theme_mask").to_list():
                for t in tx.mask_to_theme_ids(int(m or 0)):
                    th_art[t] += 1
            for cl in art.get_column("countries").to_list():
                for c in cl or []:
                    cty[cid.get(int(c), "GLOBAL")] += 1
            n_linked = int(a.get_column("__l").sum() or 0)
            a = a.sort(["__l", "__t"], descending=[True, True]).head(self.max_articles)
            for r in a.iter_rows(named=True):
                iss = [self._iid.get(int(i)) for i in (r["issuer_ids"] or [])]
                conf = list(r["issuer_conf"] or [])
                ts = slot
                if r.get("date") and r.get("minute") is not None:
                    d = r["date"]
                    ts = datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(minutes=int(r["minute"]))
                out.append(Envelope("raw.events", f"gdelt:a:{r['url_hash']}", {
                    "kind": "article", "url": r["url"], "title": r["title"], "domain": r["domain"],
                    "language": r["lang"], "translated": bool(r["translated"]), "url_hash": int(r["url_hash"]),
                    "tone": (r["tone_x100"] / 100.0) if r["tone_x100"] is not None else None,
                    "themes": tx.mask_to_theme_ids(int(r["theme_mask"] or 0)),
                    "sectors": tx.sectors_of_mask(int(r["sector_mask"] or 0)),
                    "countries": [cid.get(int(c), "GLOBAL") for c in (r["countries"] or [])],
                    "issuer_ids": [i for i in iss if i], "issuer_conf": [int(c) for c in conf[:len(iss)]],
                    "published_at": _iso(ts), "slot": stamp, "source_id": "gdelt_gkg",
                }, type="article", ts_source=src_ts))
        n_ev = 0
        if ev is not None and ev.height:
            n_ev = ev.height
            for m in ev.get_column("theme_mask").to_list():
                for t in tx.mask_to_theme_ids(int(m or 0)):
                    th_ev[t] += 1
            sal = ev.filter((pl.col("theme_mask") != 0)
                            & ((pl.col("n_articles") >= 3) | (pl.col("goldstein_x10").cast(pl.Int16).abs() >= 70)))
            sal = sal.with_columns((pl.col("n_articles").cast(pl.Float64).log1p()
                                    + pl.col("n_sources").cast(pl.Float64).log1p()
                                    + pl.col("goldstein_x10").cast(pl.Float64).abs() / 50).alias("__s")) \
                .sort("__s", descending=True).unique("event_id", keep="first", maintain_order=True).head(self.max_events)
            for r in sal.iter_rows(named=True):
                out.append(Envelope("raw.events", f"gdelt:e:{r['event_id']}", {
                    "kind": "event", "event_id": r["event_id"], "cameo": r["cameo"], "quad": r["quad"],
                    "goldstein": r["goldstein_x10"] / 10.0 if r["goldstein_x10"] is not None else None,
                    "tone": r["tone_x100"] / 100.0 if r["tone_x100"] is not None else None,
                    "n_articles": r["n_articles"], "n_sources": r["n_sources"], "n_mentions": r["n_mentions"],
                    "country": cid.get(int(r["country"]), "GLOBAL") if r["country"] is not None else None,
                    "countries": [cid.get(int(c), "GLOBAL") for c in (r["countries"] or [])],
                    "lat": r["lat_x100"] / 100.0 if r["lat_x100"] is not None else None,
                    "lon": r["lon_x100"] / 100.0 if r["lon_x100"] is not None else None,
                    "url": r["url"], "domain": r["domain"], "themes": tx.mask_to_theme_ids(int(r["theme_mask"] or 0)),
                    "event_date": str(r["event_date"]) if r["event_date"] else None, "salience": round(r["__s"], 3) if r["__s"] is not None else None,
                    "published_at": _iso(slot), "slot": stamp, "source_id": "gdelt_events", "title": None,
                }, type="event", ts_source=src_ts))
        if summary:
            out.append(Envelope("raw.events", f"gdelt:slot:{stamp}", {
                "kind": "slot", "slot": stamp, "published_at": _iso(slot),
                "n_articles": int(art.height) if art is not None else 0, "n_linked_articles": n_linked,
                "n_events": n_ev, "themes_articles": dict(th_art), "themes_events": dict(th_ev),
                "countries": dict(cty.most_common(40)), "gdelt_published_at": _iso(src_ts),
            }, type="slot", ts_source=src_ts))
        return out

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        return await ctx.run_sync(self.collect, ctx)

    def close(self) -> None:
        self._recycle_pool(kill=True)
        if self.http is not None:
            try:
                self.http.close()
            except Exception:  # noqa: BLE001
                pass
