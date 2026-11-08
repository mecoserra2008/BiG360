"""Online enrichment (R1.3): raw.* topics -> public topics (filings, news, events, macro).

EnrichConsumer (group `enrich`), per message:
* labelling / linking: issuer resolution for filings missing it (identity layer via Linker), Polygon tickers, issuer
  name / ticker / primary security; market themes from the headline (app.events_v2.taxonomy.title_theme_mask) for
  news and filings titles; GDELT items arrive already labelled by the events_v2 parsers;
* story clustering (incremental): `LiveStories` assigns every news / GDELT-article headline to a story of the last
  48 h when the token-set Jaccard of the cleaned titles >= 0.6 (the events_v2 E4 rule and helpers), else opens a new
  story; story_id = 'LS' + 12 hex of the first member key; payload gets story_id / story_size / story_first_seen;
* sentiment (cached models): English headlines of news and issuer-linked GDELT articles are scored with FinBERT
  (app.nlp.sentiment.SentimentScorer, lazily loaded, 2 threads) behind a realtime-private on-disk cache
  (data/realtime/sentiment_<model>.parquet; the nightly cache file is read but never rewritten from here). If the
  model cannot be loaded the step is skipped (sentiment fields null) and reported in the consumer detail;
* first_seen_at = the raw message's bus ingest time (PIT knowledge time) is carried into the public payload.
Keys are unchanged (natural keys), so the public topics stay idempotent.
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope, Message
from app.realtime.consumers import Consumer, ts_of
from app.realtime.streams.base import StreamContext

log = get_logger(__name__)

PUBLIC_OF = {"raw.filings": "filings", "raw.news": "news", "raw.events": "events", "raw.macro": "macro"}


# ================================================================================================ stories
class LiveStories:
    """Incremental near-duplicate clustering of headlines over a sliding window (inverted token index)."""

    def __init__(self, window_s: float = 48 * 3600, min_jaccard: float = 0.6, max_stories: int = 200_000):
        self.window_s = window_s
        self.min_j = min_jaccard
        self.max_stories = max_stories
        self.stories: dict[str, dict[str, Any]] = {}
        self.index: dict[str, set[str]] = defaultdict(set)
        self.order: deque[tuple[float, str]] = deque()

    @staticmethod
    def tokens(title: str) -> frozenset[str]:
        from app.events_v2.stories import hash_title, token_set  # noqa: PLC0415

        return token_set(hash_title(title))

    def _expire(self, now: float) -> None:
        while self.order and (now - self.order[0][0] > self.window_s or len(self.stories) > self.max_stories):
            t, sid = self.order.popleft()
            st = self.stories.get(sid)
            if st is None or st["last"] > t:   # touched since; re-queued with its newer time
                continue
            for tok in st["tokens"]:
                s = self.index.get(tok)
                if s is not None:
                    s.discard(sid)
                    if not s:
                        self.index.pop(tok, None)
            self.stories.pop(sid, None)

    def assign(self, key: str, title: str | None, now: float, source: str | None = None) -> dict[str, Any] | None:
        if not title:
            return None
        toks = self.tokens(title)
        if len(toks) < 3:
            return None
        self._expire(now)
        cand: dict[str, int] = defaultdict(int)
        for t in toks:
            for sid in self.index.get(t, ()):
                cand[sid] += 1
        best, best_j = None, 0.0
        need = math.ceil(self.min_j * len(toks) / (1 + self.min_j))   # |A&B| >= j/(1+j) * (|A|+|B|) lower bound
        for sid, n in cand.items():
            if n < need:
                continue
            st = self.stories[sid]
            j = n / len(toks | st["tokens"]) if st["tokens"] else 0.0
            if j >= self.min_j and j > best_j:
                best, best_j = sid, j
        if best is None:
            best = "LS" + hashlib.sha1(key.encode()).hexdigest()[:12]
            self.stories[best] = {"tokens": toks, "first": now, "last": now, "n": 0, "title": title,
                                  "sources": set()}
            for t in toks:
                self.index[t].add(best)
        st = self.stories[best]
        st["n"] += 1
        st["last"] = now
        if source:
            st["sources"].add(source)
        self.order.append((now, best))
        return {"story_id": best, "story_size": st["n"], "story_sources": len(st["sources"]),
                "story_first_seen": datetime.fromtimestamp(st["first"], UTC).isoformat(timespec="seconds"),
                "story_title": st["title"], "story_similarity": round(best_j, 3) if best_j else 1.0}


# ================================================================================================ sentiment
class LiveSentiment:
    """FinBERT (English) with a realtime-private cache; disabled gracefully when the model cannot load."""

    def __init__(self, settings: Any, threads: int = 2, enabled: bool = True):
        self.s = settings
        self.threads = threads
        self.enabled = enabled
        self.scorer: Any = None
        self.error: str | None = None
        self.n_scored = 0
        self._lock = threading.RLock()

    def _load(self) -> Any:
        if self.scorer is not None or not self.enabled:
            return self.scorer
        try:
            import numpy as np  # noqa: PLC0415
            import polars as pl  # noqa: PLC0415

            from app.nlp import sentiment as sm  # noqa: PLC0415

            class _Cache(sm.SentimentCache):   # read the nightly cache, write a realtime-private file
                def __init__(self, model_id: str, settings: Any = None):
                    super().__init__(model_id, settings)
                    s = settings
                    self.path = Path(s.data_dir) / "realtime" / ("sentiment_" + re.sub(r"[^A-Za-z0-9]+", "_",
                                                                                         model_id) + ".parquet")
                    if self.path.exists():
                        df = pl.read_parquet(self.path)
                        self._d.update({k: (a, b, c) for k, a, b, c in df.iter_rows()})
                    self._since_flush = 0

                def flush(self) -> None:   # rewrite only the realtime file, at most every 50 new keys
                    self._since_flush += len(self._new)
                    self._d.update(self._new)
                    self._new = {}
                    if self._since_flush < 50:
                        return
                    self._since_flush = 0
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    keys = list(self._d)[-200_000:]
                    arr = np.array([self._d[k] for k in keys], dtype=np.float64)
                    df = pl.DataFrame({"key": keys, "p_neg": arr[:, 0], "p_neu": arr[:, 1], "p_pos": arr[:, 2]})
                    tmp = self.path.with_suffix(".tmp")
                    df.write_parquet(tmp)
                    tmp.replace(self.path)

            en = sm.HFSentiment(sm.EN_MODEL, self.threads, 96)
            scorer = sm.SentimentScorer(self.s, threads=self.threads, batch_size=32, models={"en": en})
            scorer.caches = {"en": _Cache(en.model_id, self.s)}
            en.load()
            self.scorer = scorer
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"[:200]
            self.enabled = False
            log.warning("realtime sentiment disabled: %s", self.error)
        return self.scorer

    def score(self, texts: list[str]) -> list[tuple[str | None, float | None]]:
        if not texts:
            return []
        with self._lock:   # one model instance shared by the enrich and news desk consumers (threads)
            sc = self._load()
            if sc is None:
                return [(None, None)] * len(texts)
            df = sc.score(texts, ["en"] * len(texts))
            self.n_scored += len(texts)
        return list(zip(df["sentiment_label"].to_list(), df["sentiment_score"].to_list(), strict=True))


_SHARED: dict[str, LiveSentiment] = {}
_SHARED_LOCK = threading.Lock()


def shared_sentiment(settings: Any, enabled: bool = True) -> LiveSentiment:
    """roadmap v3 N2.4: the realtime service loads FinBERT ONCE; every consumer gets this instance."""
    key = str(getattr(settings, "data_dir", ""))
    with _SHARED_LOCK:
        if key not in _SHARED:
            _SHARED[key] = LiveSentiment(settings, enabled=enabled)
        elif not enabled:
            _SHARED[key].enabled = False
        return _SHARED[key]


def title_themes(title: str | None) -> list[str]:
    if not title:
        return []
    try:
        from app.events_v2 import taxonomy as tx  # noqa: PLC0415

        return tx.mask_to_theme_ids(int(tx.title_theme_mask(title) or 0))
    except Exception:  # noqa: BLE001  # pragma: no cover
        return []


def _ascii_share(s: str) -> float:
    return sum(1 for c in s if ord(c) < 128) / max(1, len(s))


# ================================================================================================ consumer
class EnrichConsumer(Consumer):
    name = "enrich"
    group = "enrich"
    topics = ("raw.filings", "raw.news", "raw.events", "raw.macro")
    batch = 500

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.stories = LiveStories()
        self.sentiment: LiveSentiment | None = None
        self.max_sent_events = int(params.get("max_sentiment_per_batch", 300))

    async def setup(self, ctx: StreamContext) -> None:
        self.sentiment = shared_sentiment(ctx.settings, enabled=bool(self.params.get("sentiment", True)))
        if self.sentiment.enabled and self.params.get("preload", True):
            import asyncio  # noqa: PLC0415

            await asyncio.to_thread(self.sentiment._load)   # load FinBERT before the first event (cold start)

    def handle_batch(self, ctx: StreamContext, msgs: list[Message]) -> list[Envelope]:
        out: list[tuple[Message, dict[str, Any]]] = []
        # 1) linking for filings that the producer could not resolve
        need = [(m.payload.get("source_id"), m.payload.get("source_entity_id")) for m in msgs
                if m.topic == "raw.filings" and not m.payload.get("issuer_id")]
        links = ctx.linker.resolve_many(need) if (need and ctx.linker is not None) else {}
        to_score: list[tuple[int, str]] = []
        n_sent_ev = 0
        for m in msgs:
            p = dict(m.payload)
            p["first_seen_at"] = m.ingest_dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
            if m.topic == "raw.filings":
                if not p.get("issuer_id"):
                    lk = links.get((p.get("source_id"), p.get("source_entity_id")))
                    if lk:
                        p.update({k: lk.get(k) for k in ("issuer_id", "security_id", "ticker")})
                p["issuer_ids"] = [p["issuer_id"]] if p.get("issuer_id") else []
                if ctx.linker is not None and p.get("issuer_id"):
                    meta = ctx.linker.issuer_meta(p["issuer_id"])
                    p.setdefault("ticker", meta.get("ticker"))
                    p["issuer_name"] = meta.get("name")
                p["themes"] = title_themes(p.get("title") if p.get("language") in (None, "en") else None)
            elif m.topic == "raw.news":
                if ctx.linker is not None and p.get("issuer_ids"):
                    p["securities"] = ctx.linker.securities_of(p["issuer_ids"])
                p["themes"] = sorted(set(title_themes(p.get("title"))))
                st = self.stories.assign(m.key, p.get("title"), m.ts_ingest, p.get("source_id"))
                if st:
                    p.update(st)
                if (p.get("language") or "en") == "en" and p.get("title"):
                    to_score.append((len(out), p["title"]))
            elif m.topic == "raw.events":
                if p.get("kind") == "article":
                    st = self.stories.assign(m.key, p.get("title"), m.ts_ingest, p.get("domain"))
                    if st:
                        p.update(st)
                    if p.get("issuer_ids") and p.get("title") and (p.get("language") or "en") == "en" \
                            and _ascii_share(p["title"]) > 0.9 and n_sent_ev < self.max_sent_events:
                        to_score.append((len(out), p["title"]))
                        n_sent_ev += 1
                    if ctx.linker is not None and p.get("issuer_ids"):
                        p["securities"] = ctx.linker.securities_of(p["issuer_ids"])
            out.append((m, p))
        if to_score and self.sentiment is not None:
            res = self.sentiment.score([t for _, t in to_score])
            for (i, _), (lab, sc) in zip(to_score, res, strict=True):
                out[i][1]["sentiment_label"] = lab
                out[i][1]["sentiment_score"] = round(sc, 4) if sc is not None else None
                out[i][1]["sentiment_model"] = "ProsusAI/finbert" if lab else None
        if self.sentiment is not None:
            self.stats.detail = (f"stories in window {len(self.stories.stories)}, sentiment "
                                 + (f"on ({self.sentiment.n_scored} scored)" if self.sentiment.enabled
                                    else f"off ({self.sentiment.error or 'disabled'})"))
        return [Envelope(PUBLIC_OF[m.topic], m.key, p, type=m.type, ts_source=ts_of(m), ckey=m.ckey) for m, p in out]


_ = time
