"""News desk (roadmap v3 N2.4 / N2.5): the per-item live news pipeline after the R1 enrich consumer.

Consumer group `newsdesk` over the public topics `news` (RSS / Polygon items, already enriched: themes, FinBERT for
English) and `events` (GDELT GKG articles, labelled by the events_v2 parsers). Per item:

  1. normalise: url_hash (= events_v2 url_hash of the normalised URL), headline, <= 300-char snippet, publisher,
     language, published_at (source time) and first_seen_at (bus ingest time of the raw message);
  2. dedupe: the same URL already seen in the window (another feed / re-listed entry) -> not published again;
     near-duplicate headlines are grouped in step 6, not dropped;
  3. issuer / entity linking: provider tickers / issuer ids when present, else the headline linker
     (app.news_live.linker: events_v2 alias table + context rules); securities and tickers via the R1 Linker;
  4. labels: countries (feed + issuers + GDELT), sectors (issuer sectors | theme -> sector map), themes (events_v2
     taxonomy; English headline rules), source tier (1 official .. 4 GDELT web);
  5. sentiment: FinBERT (the model the enrich consumer loaded - one instance per process, app.realtime.enrich
     .shared_sentiment) for English headlines still unscored, batched per consumer batch and cached; other languages
     are scored with XLM-R by the hourly job (RAM cap of the service);
  6. live story attach: MinHash-LSH (the events_v2 E4 rule: 24 permutations, 8 bands, Jaccard >= 0.6 on the cleaned
     headline tokens) against the stories of the last 72 h; story_id = events_v2 id of the first member
     (hash64('story:<day>:<url_hash>')) so the hourly fold keeps the same ids in the lake;
  7. publish `news.item` (key = item key) and, for stories with >= 2 members or an official source, `news.story`
     (key = story_id:n, ckey = story_id; throttled) on the R1 bus -> the SSE stream delivers within seconds.
GDELT articles are attached to stories but published as news.item only when issuer-linked, strongly toned
(|tone| >= 4) or part of a multi-source story (the terminal is not flooded with ~100k web articles a day).

Hot store (app.news_live.hot): every published item -> items_h<HH>.parquet of its first-seen hour, every story
assignment (all GDELT articles too) -> assign_h<HH>.parquet; flushed every `flush_s` (15 s). On start the story
index is rebuilt from the assignment files of the last 72 h (restart-safe ids).
Metrics (N2.5): per source publish -> bus latency (news.item publish time - source published_at, undated entries
excluded) and pipeline latency (news.item - raw first seen), p50 / p95 over the last 2 000 items, items in the last
hour, exposed through `metrics()` (realtime status file -> /news/live/health and /metrics).
"""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import Counter, defaultdict, deque
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)

WINDOW_S = 72 * 3600
MIN_JACCARD = 0.6
STORY_PUBLISH_MIN_GAP_S = 15.0
_SIZE_STEPS = {2, 3, 5, 8, 13, 20, 30, 50, 80, 120, 200, 300, 500, 1000}


def _dt(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _iso(ts: datetime | float | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        ts = datetime.fromtimestamp(ts, UTC)
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def story_id_for(day: date, url_hash: int) -> int:
    from app.events_v2.parse import hash64  # noqa: PLC0415

    return int(hash64(f"story:{day.isoformat()}:{int(url_hash)}"))


# ================================================================================================ story index
class StoryIndex:
    """Incremental MinHash-LSH clustering of headlines over a sliding window (ids stable across restarts)."""

    def __init__(self, window_s: float = WINDOW_S, min_jaccard: float = MIN_JACCARD):
        from app.events_v2.stories import N_BANDS, N_PERM  # noqa: PLC0415

        self.window_s = window_s
        self.min_j = min_jaccard
        self.rows = N_PERM // N_BANDS
        self.n_bands = N_BANDS
        self.stories: dict[int, dict[str, Any]] = {}
        self.buckets: list[dict[bytes, set[int]]] = [defaultdict(set) for _ in range(N_BANDS)]
        self.urls: dict[int, tuple[int, float]] = {}       # url_hash -> (story_id, seen at)
        self.order: deque[tuple[float, int]] = deque()

    @staticmethod
    def tokens(title: str) -> frozenset[str]:
        from app.events_v2.stories import hash_title, token_set  # noqa: PLC0415

        return token_set(hash_title(title))

    def _bands(self, toks: frozenset[str]) -> list[bytes]:
        from app.events_v2.stories import minhash_signatures  # noqa: PLC0415

        sig = minhash_signatures([toks])[0]
        r = self.rows
        return [np.ascontiguousarray(sig[b * r:(b + 1) * r]).tobytes() for b in range(self.n_bands)]

    def _expire(self, now: float) -> None:
        while self.order and now - self.order[0][0] > self.window_s:
            t, sid = self.order.popleft()
            st = self.stories.get(sid)
            if st is None or st["last"] > t:
                continue
            for b, key in enumerate(st["bands"]):
                s = self.buckets[b].get(key)
                if s is not None:
                    s.discard(sid)
                    if not s:
                        self.buckets[b].pop(key, None)
            self.stories.pop(sid, None)
        if len(self.urls) > 400_000:
            cut = now - self.window_s
            self.urls = {k: v for k, v in self.urls.items() if v[1] >= cut}

    def match(self, toks: frozenset[str], bands: list[bytes]) -> tuple[int | None, float]:
        cands: set[int] = set()
        for b, key in enumerate(bands):
            cands |= self.buckets[b].get(key, set())
        best, best_j, best_n = None, 0.0, -1
        for sid in cands:
            st = self.stories.get(sid)
            if st is None:
                continue
            j = len(toks & st["tokens"]) / max(1, len(toks | st["tokens"]))
            if j >= self.min_j and (st["n"] > best_n or (st["n"] == best_n and j > best_j)):
                best, best_j, best_n = sid, j, st["n"]
        return best, best_j

    def assign(self, url_hash: int, title: str | None, ts: float, source: str | None = None,
               fixed_sid: int | None = None) -> dict[str, Any] | None:
        """-> {story_id, new, dup_url, n, similarity} or None for headlines too short to cluster."""
        self._expire(ts)
        prev = self.urls.get(url_hash)
        if prev is not None and prev[0] in self.stories:
            st = self.stories[prev[0]]
            return {"story_id": prev[0], "new": False, "dup_url": True, "n": st["n"], "similarity": 1.0}
        if not title:
            return None
        toks = self.tokens(title)
        if len(toks) < 3 and fixed_sid is None:
            return None
        bands = self._bands(toks)
        sid, j = (fixed_sid, 1.0) if fixed_sid is not None else self.match(toks, bands)
        new = sid is None or sid not in self.stories
        if sid is None:
            sid = story_id_for(datetime.fromtimestamp(ts, UTC).date(), url_hash)
        if new:
            self.stories[sid] = {"tokens": toks, "bands": bands, "title": title, "first": ts, "last": ts, "n": 0,
                                 "sources": set(), "times": deque(maxlen=512), "issuers": Counter(),
                                 "countries": Counter(), "themes": Counter(), "sent": [], "tone": [],
                                 "published_at": 0.0, "published_n": 0, "official": False}
            for b, key in enumerate(bands):
                self.buckets[b][key].add(sid)
        st = self.stories[sid]
        st["n"] += 1
        st["last"] = max(st["last"], ts)
        st["times"].append(ts)
        if source:
            st["sources"].add(source)
        self.urls[url_hash] = (sid, ts)
        self.order.append((ts, sid))
        return {"story_id": sid, "new": new, "dup_url": False, "n": st["n"], "similarity": round(j, 3)}

    def velocity(self, sid: int, now: float, window_s: float = 3600.0) -> tuple[int, float]:
        """(articles in the last window, baseline articles per window before it: the story's own earlier rate,
        floored at 0.5 so a brand-new story needs a real burst)."""
        st = self.stories.get(sid)
        if st is None:
            return 0, 0.5
        recent = sum(1 for t in st["times"] if now - t <= window_s)
        older = [t for t in st["times"] if now - t > window_s]
        if older:
            span = max(window_s, (now - window_s) - min(older))
            base = len(older) / (span / window_s)
        else:
            base = 0.0
        return recent, max(0.5, base)


# ================================================================================================ metrics
class LatencyBook:
    def __init__(self, maxlen: int = 2000):
        self.pub: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=maxlen))
        self.pipe: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=maxlen))
        self.seen: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=20000))
        self.last_item: dict[str, float] = {}
        self.tier: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, source: str, now: float, published: datetime | None, first_seen: datetime | None,
            tier: int | None = None) -> None:
        with self._lock:
            self.seen[source].append(now)
            self.last_item[source] = now
            if tier is not None:
                self.tier[source] = tier
            if published is not None:
                lag = now - published.timestamp()
                if -120 < lag < 6 * 3600:          # fresh items only say something about latency
                    self.pub[source].append(max(0.0, lag))
            if first_seen is not None:
                lag = now - first_seen.timestamp()
                if -5 < lag < 3600:
                    self.pipe[source].append(max(0.0, lag))

    @staticmethod
    def q(vals: Any, p: float) -> float | None:
        v = sorted(vals)
        if not v:
            return None
        return round(v[min(len(v) - 1, int(p * len(v)))], 2) if p != 0.5 else round(statistics.median(v), 2)

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        out: dict[str, Any] = {}
        allpub: list[float] = []
        allpipe: list[float] = []
        with self._lock:
            for src in sorted(set(self.seen) | set(self.pub)):
                sn = self.seen.get(src) or deque()
                while sn and now - sn[0] > 3600:
                    sn.popleft()
                pub, pipe = list(self.pub.get(src) or []), list(self.pipe.get(src) or [])
                allpub += pub
                allpipe += pipe
                out[src] = {"items_1h": len(sn), "tier": self.tier.get(src),
                            "last_item_at": _iso(self.last_item.get(src)),
                            "publish_to_bus_s_p50": self.q(pub, 0.5), "publish_to_bus_s_p95": self.q(pub, 0.95),
                            "pipeline_s_p50": self.q(pipe, 0.5), "pipeline_s_p95": self.q(pipe, 0.95), "n": len(pub)}
        return {"sources": out, "overall": {"publish_to_bus_s_p50": self.q(allpub, 0.5),
                                            "publish_to_bus_s_p95": self.q(allpub, 0.95),
                                            "pipeline_s_p50": self.q(allpipe, 0.5),
                                            "pipeline_s_p95": self.q(allpipe, 0.95), "n": len(allpub)}}


def source_family(source_id: str | None) -> str:
    """Latency / volume grouping: gdelt | polygon | rss feed id."""
    s = source_id or "unknown"
    if s.startswith("gdelt"):
        return "gdelt"
    if s.startswith("polygon"):
        return "polygon"
    return s


# ================================================================================================ consumer
class NewsDeskConsumer(Consumer):
    name = "newsdesk"
    group = "newsdesk"
    topics = ("news", "events")
    batch = 500
    idle_s = 2.0

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.index = StoryIndex(float(params.get("window_s", WINDOW_S)))
        self.lat = LatencyBook()
        self.linker: Any = None
        self.sentiment: Any = None
        self.flush_s = float(params.get("flush_s", 15.0))
        self.hot = bool(params.get("hot", True))
        self._items: dict[tuple[date, int], list[dict[str, Any]]] = defaultdict(list)
        self._assign: dict[tuple[date, int], list[dict[str, Any]]] = defaultdict(list)
        self._dirty: set[tuple[str, date, int]] = set()
        self._flushed_at = time.time()
        self.counts = Counter()
        self.tier_of: dict[str, int] = {}
        self.wctx: Any = None
        self.min_gdelt_tone = float(params.get("min_gdelt_tone", 4.0))
        self.t_start = float(params.get("t_start", time.time()))

    # ------------------------------------------------------------------ setup
    async def setup(self, ctx: StreamContext) -> None:
        import asyncio  # noqa: PLC0415

        await asyncio.to_thread(self._setup_sync, ctx)

    def _setup_sync(self, ctx: StreamContext) -> None:
        try:
            from app.news_live.feeds import load_feeds  # noqa: PLC0415

            self.tier_of = {f.id: f.tier for f in load_feeds(ctx.settings, include_inactive=True)}
        except Exception as e:  # noqa: BLE001
            log.warning("newsdesk: feeds config unavailable: %s", e)
        if self.params.get("linker", True):
            try:
                import pickle  # noqa: PLC0415

                from app.events_v2.context import state_dir  # noqa: PLC0415
                from app.news_live.linker import HeadlineLinker  # noqa: PLC0415

                p = state_dir(ctx.settings) / "worker_ctx.pkl"
                if p.exists():
                    with open(p, "rb") as fh:
                        self.wctx = pickle.load(fh)  # noqa: S301 - own file
                    self.linker = HeadlineLinker(self.wctx)
            except Exception as e:  # noqa: BLE001
                log.warning("newsdesk: headline linker unavailable: %s", e)
        if self.params.get("sentiment", True):
            from app.realtime.enrich import shared_sentiment  # noqa: PLC0415

            self.sentiment = shared_sentiment(ctx.settings)
        if self.hot:
            self.restore(ctx)

    def restore(self, ctx: StreamContext, now: float | None = None) -> int:
        """Rebuild the story index from the hot assignment files of the last 72 h (ids preserved)."""
        import polars as pl  # noqa: PLC0415

        from app.news_live import hot  # noqa: PLC0415

        now = now or time.time()
        start = datetime.fromtimestamp(now - self.index.window_s, UTC)
        frames = []
        for h in hot.hours_between(start, datetime.fromtimestamp(now, UTC)):
            df = hot.read_parquet(hot.assign_path(h.date(), h.hour, ctx.settings))
            if df is not None and df.height:
                frames.append(df)
                if h >= hot.hour_floor(datetime.fromtimestamp(now, UTC)) - timedelta(hours=1):
                    self._assign[(h.date(), h.hour)] = df.to_dicts()   # keep appending to the open hour
        n = 0
        if frames:
            df = pl.concat(frames, how="diagonal_relaxed").sort("ts")
            for r in df.iter_rows(named=True):
                if self.index.assign(int(r["url_hash"]), r.get("title"), float(r["ts"]), r.get("source"),
                                     fixed_sid=int(r["story_id"])) is not None:
                    n += 1
        for h in hot.hours_between(datetime.fromtimestamp(now, UTC) - timedelta(hours=1),
                                   datetime.fromtimestamp(now, UTC)):
            df = hot.read_parquet(hot.items_path(h.date(), h.hour, ctx.settings))
            if df is not None and df.height:
                self._items[(h.date(), h.hour)] = df.to_dicts()
        self.counts["restored"] = n
        return n

    # ------------------------------------------------------------------ helpers
    def tier(self, source_id: str | None, kind: str) -> int:
        if kind == "gdelt":
            return 4
        if source_id in self.tier_of:
            return int(self.tier_of[source_id])
        if (source_id or "").startswith("polygon"):
            return 2
        if (source_id or "").startswith("rss_"):
            return 1
        return 3

    def _link(self, title: str | None, language: str | None) -> tuple[list[str], list[int]]:
        if self.linker is None or not title:
            return [], []
        links = self.linker.link(title, language)
        return self.linker.issuer_ids(links), [i for i, _ in links]

    def _issuer_labels(self, ctx: StreamContext, issuer_ids: list[str]) -> tuple[list[str], list[str], list[str], int]:
        """(tickers, security_ids, countries, sector mask) of linked issuers."""
        tick, secs, ctys, smask = [], [], [], 0
        if ctx.linker is not None:
            for i in issuer_ids:
                m = ctx.linker.issuer_meta(i)
                if m.get("ticker"):
                    tick.append(m["ticker"])
                if m.get("security_id"):
                    secs.append(m["security_id"])
        if self.linker is not None:
            rev = {v: k for k, v in self.linker.issuer_id.items()} if not hasattr(self, "_rev") else self._rev
            self._rev = rev
            for i in issuer_ids:
                iid = rev.get(i)
                if iid is None:
                    continue
                c = self.linker.country.get(iid)
                if c:
                    ctys.append(c)
                smask |= self.linker.sector_mask.get(iid, 0)
        return list(dict.fromkeys(tick)), list(dict.fromkeys(secs)), list(dict.fromkeys(ctys)), smask

    def build_item(self, ctx: StreamContext, m: Message) -> dict[str, Any] | None:
        from app.events_v2 import taxonomy as tx  # noqa: PLC0415
        from app.news_live.feeds import clean_snippet, url_hash  # noqa: PLC0415

        p = m.payload
        gd = m.topic == "events"
        if gd and p.get("kind") != "article":
            return None
        url = p.get("url")
        title = p.get("title")
        if not url or not title:
            return None
        uh = p.get("url_hash")
        uh = int(uh) if uh is not None else url_hash(url)
        source = p.get("source_id") or ("gdelt_gkg" if gd else "unknown")
        lang = (p.get("language") or "en")[:2]
        published = _dt(p.get("published_at"))
        if gd:   # GDELT: the slot stamp is the END of the 15-min window; GDELT's file release time is the publication
            from app.realtime.consumers import ts_of  # noqa: PLC0415

            rel = ts_of(m)
            if rel is not None and (published is None or rel < published or published > m.ingest_dt):
                published = rel
        first_seen = _dt(p.get("first_seen_at")) or m.ingest_dt
        issuer_ids = list(p.get("issuer_ids") or [])
        linked_by = "provider" if issuer_ids else None
        if not issuer_ids and not gd:
            issuer_ids, _ = self._link(title, lang)
            linked_by = "headline" if issuer_ids else None
        tick, secs, ictys, ism = self._issuer_labels(ctx, issuer_ids)
        tickers = list(dict.fromkeys([*(p.get("tickers") or []), *tick]))[:8]
        themes = list(p.get("themes") or [])
        tmask = 0
        for t in themes:
            th = tx.THEME_BY_ID.get(t)
            if th is not None:
                tmask |= 1 << th.bit
        smask = tx.theme_sector_mask(tmask) | ism
        sectors = list(dict.fromkeys([*(p.get("sectors") or []), *tx.sectors_of_mask(smask)]))
        countries = [c for c in dict.fromkeys([*(p.get("countries") or []), *ictys]) if c][:6]
        kind = "gdelt" if gd else "news"
        tier = int(p.get("tier") or self.tier(source, kind))
        return {
            "item_key": m.key, "url_hash": uh, "kind": kind, "source_id": source, "tier": tier,
            "category": p.get("category") or ("web" if gd else "news"),
            "publisher": p.get("publisher") or p.get("domain") or source, "domain": p.get("domain"),
            "title": title[:400], "snippet": clean_snippet(p.get("snippet")) if not gd else None, "url": url,
            "language": lang, "published_at": _iso(published) if published else None,
            "published_estimated": bool(p.get("published_estimated")),
            "first_seen_at": _iso(first_seen), "issuer_ids": issuer_ids, "linked_by": linked_by,
            "security_ids": secs or list(p.get("securities") or []), "tickers": tickers, "countries": countries,
            "sectors": sectors, "themes": themes, "tone": p.get("tone"),
            "sentiment_score": p.get("sentiment_score"), "sentiment_label": p.get("sentiment_label"),
            "sentiment_model": p.get("sentiment_model"), "bus_raw_id": m.id,
        }

    @staticmethod
    def salience(it: dict[str, Any], n: int, n_sources: int) -> float:
        s = math.log1p(n) + math.log1p(n_sources)
        s += abs(it.get("sentiment_score") or 0.0) + min(1.0, abs(it.get("tone") or 0.0) / 10.0)
        s += {1: 1.0, 2: 0.6, 3: 0.3}.get(int(it.get("tier") or 4), 0.0)
        s += 0.8 if it.get("issuer_ids") else 0.0
        return round(s, 3)

    def story_payload(self, sid: int, now: float) -> dict[str, Any]:
        st = self.index.stories[sid]
        v, base = self.index.velocity(sid, now)
        sent = [x for x in st["sent"] if x is not None]
        return {"story_id": str(sid), "title": st["title"], "n_articles": st["n"], "n_sources": len(st["sources"]),
                "sources": sorted(st["sources"])[:12], "first_seen_at": _iso(st["first"]),
                "last_seen_at": _iso(st["last"]), "velocity_1h": v, "baseline_per_h": round(base, 3),
                "velocity_ratio": round(v / base, 2) if base else None,
                "issuer_ids": [k for k, _ in st["issuers"].most_common(8)],
                "countries": [k for k, _ in st["countries"].most_common(6)],
                "themes": [k for k, _ in st["themes"].most_common(6)],
                "sentiment_mean": round(float(np.mean(sent)), 4) if sent else None,
                "tone_mean": round(float(np.mean(st["tone"])), 3) if st["tone"] else None,
                "official": bool(st["official"])}

    # ------------------------------------------------------------------ batch
    def handle_batch(self, ctx: StreamContext, msgs: list[Message]) -> list[Envelope]:
        now = time.time()
        items: list[tuple[Message, dict[str, Any]]] = []
        for m in msgs:
            try:
                it = self.build_item(ctx, m)
            except Exception as e:  # noqa: BLE001 - a malformed item must not stop the desk
                log.warning("newsdesk: item %s skipped: %s", m.key, e)
                self.counts["errors"] += 1
                continue
            if it is not None:
                items.append((m, it))
        # URL duplicates (already in the window or earlier in this batch) are not scored
        batch_urls: set[int] = set()
        dup: set[int] = set()
        for i, (_, it) in enumerate(items):
            prev = self.index.urls.get(it["url_hash"])
            if it["url_hash"] in batch_urls or (prev is not None and prev[0] in self.index.stories):
                dup.add(i)
            batch_urls.add(it["url_hash"])
        # sentiment for English headlines still unscored (one batched call)
        if self.sentiment is not None:
            todo = [i for i, (_, it) in enumerate(items) if i not in dup and it["sentiment_score"] is None
                    and it["language"] == "en" and (it["kind"] == "news" or it["issuer_ids"])]
            if todo:
                res = self.sentiment.score([items[i][1]["title"] for i in todo])
                for i, (lab, sc) in zip(todo, res, strict=True):
                    if lab is not None:
                        items[i][1].update(sentiment_label=lab, sentiment_score=round(sc, 4),
                                           sentiment_model="ProsusAI/finbert")
        out: list[Envelope] = []
        touched: set[int] = set()
        for m, it in items:
            ts = (_dt(it["first_seen_at"]) or m.ingest_dt).timestamp()
            a = self.index.assign(it["url_hash"], it["title"], ts, it["publisher"])
            if a is None:   # too short to cluster: its own singleton story id (not indexed)
                sid = story_id_for(datetime.fromtimestamp(ts, UTC).date(), it["url_hash"])
                a = {"story_id": sid, "new": True, "dup_url": False, "n": 1, "similarity": 1.0}
            sid = int(a["story_id"])
            hk = (datetime.fromtimestamp(ts, UTC).date(), datetime.fromtimestamp(ts, UTC).hour)
            if not a["dup_url"]:
                self._assign[hk].append({"url_hash": it["url_hash"], "story_id": sid, "title": it["title"], "ts": ts,
                                         "source": it["publisher"], "item_key": it["item_key"], "kind": it["kind"]})
                self._dirty.add(("assign", *hk))
            if a["dup_url"]:
                self.counts["dup_url"] += 1
                continue
            st = self.index.stories.get(sid)
            if st is not None:
                for i in it["issuer_ids"]:
                    st["issuers"][i] += 1
                for c in it["countries"]:
                    st["countries"][c] += 1
                for t in it["themes"]:
                    st["themes"][t] += 1
                if it["sentiment_score"] is not None:
                    st["sent"].append(it["sentiment_score"])
                if it["tone"] is not None:
                    st["tone"].append(float(it["tone"]))
                if it["tier"] == 1:
                    st["official"] = True
            n = st["n"] if st else 1
            n_src = len(st["sources"]) if st else 1
            it.update(story_id=str(sid), story_size=n, story_sources=n_src,
                      story_first_seen=_iso(st["first"]) if st else it["first_seen_at"],
                      story_title=st["title"] if st else it["title"], story_similarity=a["similarity"],
                      salience=self.salience(it, n, n_src))
            if it["kind"] == "gdelt" and not (it["issuer_ids"] or abs(it.get("tone") or 0.0) >= self.min_gdelt_tone
                                              or n_src >= 2):
                self.counts["gdelt_attached_only"] += 1
                if sid in self.index.stories and n >= 2:
                    touched.add(sid)
                continue
            published = _dt(it["published_at"]) if not it["published_estimated"] else None
            out.append(Envelope("news.item", it["item_key"], it, type=it["kind"], ts_source=published, ckey=None))
            # latency only for items published while the service runs (the first poll's backlog is not "live")
            live_pub = published if (published is not None and published.timestamp() >= self.t_start) else None
            self.lat.add(source_family(it["source_id"]), now, live_pub,
                         _dt(it["first_seen_at"]) if live_pub is not None else None, it["tier"])
            self._items[hk].append(it)
            self._dirty.add(("items", *hk))
            self.counts["items"] += 1
            if sid in self.index.stories:
                touched.add(sid)
        for sid in touched:
            st = self.index.stories[sid]
            if st["n"] < 2 and not st["official"]:
                continue
            due = (st["n"] in _SIZE_STEPS and st["n"] != st["published_n"]) or \
                  (now - st["published_at"] >= STORY_PUBLISH_MIN_GAP_S and st["n"] != st["published_n"])
            if not due:
                continue
            st["published_at"], st["published_n"] = now, st["n"]
            pl_ = self.story_payload(sid, now)
            out.append(Envelope("news.story", f"{sid}:{st['n']}", pl_, type="story",
                                ts_source=datetime.fromtimestamp(st["first"], UTC), ckey=str(sid)))
            self.counts["story_updates"] += 1
        if self.hot and time.time() - self._flushed_at >= self.flush_s:
            self.flush(ctx)
        self.stats.detail = (f"{self.counts['items']} items, {len(self.index.stories)} stories in 72 h, "
                             f"dup urls {self.counts['dup_url']}, story updates {self.counts['story_updates']}, "
                             f"linker {'on' if self.linker else 'off'}, sentiment "
                             f"{'on' if (self.sentiment and self.sentiment.enabled) else 'off'}")
        return out

    def on_idle(self, ctx: StreamContext) -> list[Envelope]:
        if self.hot and self._dirty and time.time() - self._flushed_at >= self.flush_s:
            self.flush(ctx)
        return []

    def flush(self, ctx: StreamContext) -> None:
        """Rewrite the hot hour files that changed (current + previous hour stay in memory)."""
        import polars as pl  # noqa: PLC0415

        from app.news_live import hot  # noqa: PLC0415

        self._flushed_at = time.time()
        for kind, d, h in sorted(self._dirty, key=lambda x: (x[1], x[2])):
            rows = (self._items if kind == "items" else self._assign).get((d, h)) or []
            if not rows:
                continue
            path = hot.items_path(d, h, ctx.settings) if kind == "items" else hot.assign_path(d, h, ctx.settings)
            try:
                hot.write_parquet_atomic(pl.DataFrame(rows, infer_schema_length=None), path)
            except Exception as e:  # noqa: BLE001 - retried at the next flush
                log.warning("newsdesk: hot write %s failed: %s", path.name, e)
                continue
            self._dirty.discard((kind, d, h))
        cut = hot.hour_floor(datetime.now(UTC)) - timedelta(hours=1)
        for buf in (self._items, self._assign):
            for k in [k for k in buf if datetime(k[0].year, k[0].month, k[0].day, k[1], tzinfo=UTC) < cut]:
                if not any(x[1:] == k for x in self._dirty):
                    buf.pop(k, None)

    def metrics(self) -> dict[str, Any]:
        snap = self.lat.snapshot()
        return {**snap, "counts": dict(self.counts), "stories_window": len(self.index.stories),
                "linker": self.linker is not None,
                "sentiment": bool(self.sentiment and self.sentiment.enabled)}
