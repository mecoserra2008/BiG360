"""News desk: URL dedupe, MinHash story attach, GDELT gating, labels, latency metrics, hot files + restore, headline
linker, shared sentiment."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import polars as pl

from app.news_live import hot
from app.news_live.desk import LatencyBook, NewsDeskConsumer, StoryIndex, story_id_for
from app.realtime.bus import SqliteBus
from app.realtime.streams.base import StreamContext


class FakeSent:
    enabled = True

    def __init__(self):
        self.calls: list[list[str]] = []

    def score(self, texts):
        self.calls.append(list(texts))
        return [("negative", -0.7) if "falls" in t.lower() else ("positive", 0.6) for t in texts]


class FakeLinker:
    def issuer_meta(self, i):
        return {"I1": {"ticker": "AAPL", "security_id": "S1"}}.get(i, {})

    def securities_of(self, ids):
        return ["S1" for i in ids if i == "I1"]


def ctx_for(s, bus):
    return StreamContext(settings=s, bus=bus, out=asyncio.Queue(), stop=asyncio.Event(), linker=FakeLinker())


def publish_news(bus, key, title, url, source="rss_ft_home", pub=None, first=None, lang="en", **extra):
    now = datetime.now(UTC)
    p = {"item_id": key, "source_id": source, "publisher": extra.pop("publisher", "Financial Times"), "title": title,
         "url": url, "language": lang, "published_at": (pub or now - timedelta(seconds=30)).isoformat(),
         "first_seen_at": (first or now).isoformat(), "countries": extra.pop("countries", []),
         "themes": extra.pop("themes", []), "issuer_ids": extra.pop("issuer_ids", []), "tier": extra.pop("tier", 2),
         "category": "publisher", **extra}
    bus.publish("news", key, p, type="publisher", ts_source=pub)


def test_story_index_dedupe_attach_restore():
    ix = StoryIndex()
    t0 = time.time()
    a = ix.assign(11, "Fed holds rates steady as inflation cools further", t0, "ft")
    b = ix.assign(12, "Fed holds rates steady as inflation cools further - Reuters", t0 + 5, "reuters")
    c = ix.assign(11, "Fed holds rates steady as inflation cools further", t0 + 6, "ft")
    d = ix.assign(13, "Oil jumps as OPEC agrees surprise production cut", t0 + 7, "wsj")
    assert a["new"] and b["story_id"] == a["story_id"] and not b["new"] and b["n"] == 2
    assert c["dup_url"] and c["story_id"] == a["story_id"]
    assert d["story_id"] != a["story_id"] and d["new"]
    assert a["story_id"] == story_id_for(datetime.fromtimestamp(t0, UTC).date(), 11)   # lake-compatible id
    assert ix.assign(14, "Short", t0, "x") is None
    v, base = ix.velocity(a["story_id"], t0 + 10)
    assert v == 2 and base == 0.5
    # restore with fixed ids keeps the clustering
    ix2 = StoryIndex()
    ix2.assign(11, "Fed holds rates steady as inflation cools further", t0, "ft", fixed_sid=a["story_id"])
    e = ix2.assign(15, "Fed holds rates steady as inflation cools further, says Powell", t0 + 60, "ap")
    assert e["story_id"] == a["story_id"]


def test_latency_book_quantiles():
    lb = LatencyBook()
    now = time.time()
    for i in range(100):
        lb.add("rss_x", now, datetime.fromtimestamp(now - (i + 1), UTC), datetime.fromtimestamp(now - 0.5, UTC), 2)
    lb.add("rss_x", now, datetime.fromtimestamp(now - 30 * 3600, UTC), None, 2)     # stale item: not a latency
    snap = lb.snapshot(now)
    x = snap["sources"]["rss_x"]
    assert x["items_1h"] == 101 and x["n"] == 100 and x["tier"] == 2
    assert 49 <= x["publish_to_bus_s_p50"] <= 52 and 94 <= x["publish_to_bus_s_p95"] <= 97
    assert x["pipeline_s_p50"] == 0.5 and snap["overall"]["n"] == 100


def test_desk_pipeline_publish_gate_hot_files_and_restore(tmp_data):
    bus = SqliteBus(tmp_data.data_dir / "realtime" / "bus.sqlite")
    ctx = ctx_for(tmp_data, bus)
    desk = NewsDeskConsumer(linker=False, sentiment=False, flush_s=0.0, t_start=time.time() - 3600)
    asyncio.run(desk.setup(ctx))
    desk.sentiment = FakeSent()
    publish_news(bus, "n1", "Apple shares falls after weak iPhone sales in China", "https://ft.com/a",
                 issuer_ids=["I1"], tickers=[], countries=["US"], themes=["earnings"])
    publish_news(bus, "n2", "Apple shares falls after weak iPhone sales in China", "https://www.ft.com/a/?utm=1",
                 source="rss_ft_markets")                                         # same URL via another feed
    publish_news(bus, "n3", "Apple shares falls after weak iPhone sales in China - WSJ", "https://wsj.com/x",
                 source="rss_wsj_markets", publisher="WSJ")
    publish_news(bus, "n4", "Petrobras sobe na bolsa com alta do petroleo", "https://valor.globo.com/p", lang="pt",
                 source="rss_valor", publisher="Valor Economico", countries=["BR"])
    bus.publish("events", "gdelt:a:777", {"kind": "article", "url": "https://web.example/a", "title":
                "Local council approves new parking rules downtown", "domain": "web.example", "language": "en",
                "tone": -0.5, "themes": [], "countries": ["US"], "issuer_ids": [], "url_hash": 777,
                "published_at": datetime.now(UTC).isoformat(), "source_id": "gdelt_gkg"}, type="article")
    bus.publish("events", "gdelt:a:778", {"kind": "article", "url": "https://web.example/b", "title":
                "Apple supplier Foxconn expands India plant", "domain": "web.example", "language": "en",
                "tone": 1.0, "themes": [], "countries": ["IN"], "issuer_ids": ["I1"], "url_hash": 778,
                "published_at": datetime.now(UTC).isoformat(), "source_id": "gdelt_gkg"}, type="article")
    msgs = bus.read(["news", "events"])
    out = desk.handle_batch(ctx, msgs)
    items = [e for e in out if e.topic == "news.item"]
    keys = [e.key for e in items]
    assert keys == ["n1", "n3", "n4", "gdelt:a:778"]                   # n2 duplicate URL, unlinked GDELT not shown
    n1 = items[0].payload
    assert n1["tickers"] == ["AAPL"] and n1["security_ids"] == ["S1"] and n1["tier"] == 2
    assert n1["sentiment_score"] == -0.7 and n1["sentiment_model"] == "ProsusAI/finbert"
    assert n1["story_id"] == items[1].payload["story_id"] and items[1].payload["story_size"] == 2
    n4 = items[2].payload
    assert n4["sentiment_score"] is None and n4["language"] == "pt"     # non-English: XLM-R in the hourly job
    assert desk.sentiment.calls == [[n1["title"], items[1].payload["title"], items[3].payload["title"]]]
    stories = [e for e in out if e.topic == "news.story"]
    assert len(stories) == 1 and stories[0].payload["n_articles"] == 2 and stories[0].ckey == n1["story_id"]
    assert desk.counts["dup_url"] == 1 and desk.counts["gdelt_attached_only"] == 1
    m = desk.metrics()
    assert m["sources"]["rss_ft_home"]["n"] == 1 and 25 <= m["sources"]["rss_ft_home"]["publish_to_bus_s_p50"] <= 60
    assert m["sources"]["gdelt"]["items_1h"] == 1
    # hot files: items + assignments of the first-seen hour (all GDELT articles are assigned)
    now = datetime.now(UTC)
    desk.flush(ctx)
    it = pl.read_parquet(hot.items_path(now.date(), now.hour, tmp_data))
    asg = pl.read_parquet(hot.assign_path(now.date(), now.hour, tmp_data))
    assert sorted(it.get_column("item_key").to_list()) == sorted(keys)
    assert 777 in asg.get_column("url_hash").to_list() and asg.height == 5
    # restart: the index is rebuilt with the same ids
    desk2 = NewsDeskConsumer(linker=False, sentiment=False)
    asyncio.run(desk2.setup(ctx))
    assert desk2.counts["restored"] == 5
    bus.publish("news", "n5", {"item_id": "n5", "source_id": "rss_bbc_business", "publisher": "BBC", "title":
                "Apple shares falls after weak iPhone sales in China, analysts say", "url": "https://bbc.co.uk/z",
                "language": "en", "published_at": datetime.now(UTC).isoformat(), "tier": 3}, type="publisher")
    out2 = desk2.handle_batch(ctx, bus.read(["news"], after_id=msgs[-1].id))
    assert out2[0].payload["story_id"] == n1["story_id"] and out2[0].payload["story_size"] == 3
    bus.close()


def test_headline_linker_with_tiny_context():
    from app.events_v2.parse import WorkerContext
    from app.news_live.linker import HeadlineLinker

    aliases = pl.DataFrame({"org_norm": ["acme robotics", "acme", "target"], "issuer_ids": [[1], [1], [2]],
                            "conf": [0.95, 0.4, 0.9], "ambiguous": [False, True, False]},
                           schema={"org_norm": pl.Utf8, "issuer_ids": pl.List(pl.Int32), "conf": pl.Float64,
                                   "ambiguous": pl.Boolean})
    issuers = pl.DataFrame({"iid": [1, 2], "country": ["US", "US"], "sector_mask": [1, 4],
                            "name": ["Acme Robotics", "Target"], "name_norm": ["acme robotics", "target"],
                            "ticker": ["ACMR", "TGT"], "issuer_id": ["IACME", "ITGT"]},
                           schema={"iid": pl.Int32, "country": pl.Utf8, "sector_mask": pl.Int16, "name": pl.Utf8,
                                   "name_norm": pl.Utf8, "ticker": pl.Utf8, "issuer_id": pl.Utf8})
    L = HeadlineLinker(WorkerContext.build(aliases, issuers))
    assert L.issuer_ids(L.link("Acme Robotics shares jump after record orders")) == ["IACME"]
    assert L.issuer_ids(L.link("Analysts target higher oil prices")) == []          # common word, lower case
    assert L.issuer_ids(L.link("Target cuts its outlook as shoppers pull back")) == ["ITGT"]
    assert L.issuer_ids(L.link("Robot maker $ACMR wins defence contract")) == ["IACME"]   # cash-tag


def test_shared_sentiment_is_one_instance(tmp_data):
    from app.realtime.enrich import shared_sentiment

    a = shared_sentiment(tmp_data, enabled=False)
    b = shared_sentiment(tmp_data)
    assert a is b and not b.enabled and b.score(["x"]) == [(None, None)]
