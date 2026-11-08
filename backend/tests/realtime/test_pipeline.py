"""R1.2-R1.6 end to end without network: a fake stream -> bounded queue -> bus -> enrich -> alerts / insider /
change points -> lake compaction (idempotent, first-seen knowledge_ts); restart policy; online BOCPD."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, date, datetime

import numpy as np
import polars as pl

from app.realtime.alerts_live import AlertConsumer, triggers_for
from app.realtime.bus import Envelope, SqliteBus
from app.realtime.changepoint import ChangepointConsumer, MiniBOCPD
from app.realtime.compact import compact
from app.realtime.enrich import EnrichConsumer, LiveStories
from app.realtime.streams.base import StreamContext, StreamWorker
from app.realtime.supervisor import Supervisor

NOW = datetime(2026, 10, 12, 20, 0, tzinfo=UTC)


class FakeFilings(StreamWorker):
    name = "fake_filings"
    interval_s = 0.2

    def __init__(self, **kw):
        super().__init__(**kw)
        self.n = 0

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        self.n += 1
        if self.n > 2:          # re-polls return the same items -> deduped by key
            return [self._env(0)]
        return [self._env(self.n - 1)]

    def _env(self, i: int) -> Envelope:
        return Envelope("raw.filings", f"sec_edgar:acc-{i}", {
            "filing_id": f"sec_edgar:acc-{i}", "source_id": "sec_edgar", "source_entity_id": "0000000001",
            "accession": f"acc-{i}", "company": "Acme", "form_type": "8-K", "form_class": "current_report",
            "items": ["2.02"], "issuer_id": "I1", "security_id": "S1", "filed_date": "2026-10-12",
            "title": "Acme results: profit warning", "language": "en"}, type="filing", ts_source=NOW)


class FakeNews(StreamWorker):
    name = "fake_news"
    interval_s = 0.2

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        t = ["Fed raises rates by 25 basis points in surprise move", "Fed raises rates by 25 basis points, surprise move"]
        return [Envelope("raw.news", f"n{i}", {"item_id": f"n{i}", "source_id": "rss_fed", "title": x,
                                                "published_at": NOW.isoformat(), "language": "en", "issuer_ids": []},
                         type="central_bank", ts_source=NOW) for i, x in enumerate(t)]


class Crashy(StreamWorker):
    name = "crashy"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.runs = 0

    async def run(self, ctx):    # crashes the whole loop (not just a poll) -> supervisor restart policy
        self.runs += 1
        raise RuntimeError("boom")


class MemStore:
    def __init__(self, rules):
        self.rules, self.rows = rules, []

    def load(self):
        return self.rules, {"W1": ["S1"]}, []

    def append(self, rows):
        self.rows += rows


def test_pipeline_end_to_end(tmp_data):
    bus = SqliteBus(tmp_data.data_dir / "realtime" / "bus.sqlite")
    store = MemStore([{"alert_id": "A1", "rule_type": "new_filing", "params": "{}", "security_id": None,
                       "watchlist_id": "W1", "enabled": True, "cooldown_days": 0}])
    crashy = Crashy()
    sup = Supervisor(tmp_data, bus=bus, use_lock=False, status_every_s=0.5, grace_s=5,
                     workers=[FakeFilings(), FakeNews(), crashy, EnrichConsumer(sentiment=False),
                              AlertConsumer(store=store), ChangepointConsumer()])
    asyncio.run(sup.main(duration_s=4.0))
    filings = bus.read(["filings"])
    assert [m.key for m in filings] == ["sec_edgar:acc-0", "sec_edgar:acc-1"]           # deduped re-polls
    p = filings[0].payload
    assert p["first_seen_at"] and p["issuer_ids"] == ["I1"] and isinstance(p["themes"], list)
    news = bus.read(["news"])
    assert len(news) == 2 and news[0].payload["story_id"] == news[1].payload["story_id"]   # same story
    assert news[1].payload["story_size"] == 2
    alerts = bus.read(["alerts"])
    assert len(alerts) == 1 and alerts[0].payload["security_id"] == "S1"                  # one per (alert, sec, day)
    assert len(store.rows) == 1 and '"live": true' in store.rows[0]["payload"]
    assert crashy.runs >= 1 and crashy.stats.restarts >= 1
    st = sup._status()
    assert st["streams"]["fake_filings"]["events_total"] == 2 and st["streams"]["fake_filings"]["duplicates"] >= 1
    assert st["consumers"]["enrich"]["events_total"] == 4
    assert (tmp_data.state_dir / "realtime_status.json").exists()
    # lake compaction: idempotent, knowledge_ts = first seen
    rep = compact(bus, tmp_data)
    assert rep["live_filings"]["rows_written"] == 2 and rep["live_news"]["rows_written"] == 2
    bus.seek("lake", "filings", 0)                 # replay everything: no duplicates written
    rep2 = compact(bus, tmp_data)
    assert rep2["live_filings"]["messages"] == 2 and rep2["live_filings"]["rows_written"] == 0
    from app.core.storage import read_table

    lf = read_table("live_filings", settings=tmp_data)
    assert lf.height == 2 and lf["filing_id"].n_unique() == 2
    first_seen = datetime.fromisoformat(p["first_seen_at"].replace("Z", "+00:00"))
    got = lf.filter(pl.col("filing_id") == "sec_edgar:acc-0")["knowledge_ts"][0]
    assert abs((got - first_seen).total_seconds()) < 0.01 and lf["source_ts"][0] == NOW
    bus.close()


def test_alert_trigger_rules():
    from app.realtime.bus import Message

    m = Message(1, "macro", "RE1|fred:CPI", "release", None, NOW.timestamp(), {
        "release_event_id": "RE1", "series_id": "fred:CPI", "country": "US", "importance": 1, "theme": "inflation",
        "actual": 0.4, "prior": 0.3, "expectation": 0.2, "surprise_z": 2.5, "scheduled_at": "2026-10-14T12:30:00Z",
        "release_name": "CPI"})
    rel = triggers_for({"rule_type": "macro_release", "params": {"countries": ["US"]}}, ["macro"], m, {})
    assert rel[0]["security_id"] == "macro:US" and rel[0]["as_of_date"] == date(2026, 10, 14)
    sur = triggers_for({"rule_type": "macro_surprise", "params": {"threshold_z": 2}}, ["macro"], m, {})
    assert "not consensus" in sur[0]["message"]
    assert triggers_for({"rule_type": "macro_surprise", "params": {"threshold_z": 3}}, ["macro"], m, {}) == []
    ins = Message(2, "signals", "insider:acc", "insider", None, NOW.timestamp(), {
        "issuer_id": "I1", "security_id": None, "has_open_market_buy": True, "as_of_date": "2026-10-12",
        "signal": {"net_buy_value_usd_30d": 2e6, "cluster_buy": False}})
    assert triggers_for({"rule_type": "insider_buy", "params": {"min_value_usd": 1e6}}, ["S9"], ins, {"S9": "I1"})
    assert triggers_for({"rule_type": "insider_buy", "params": {"min_value_usd": 1e6, "cluster_only": True}}, ["S9"],
                        ins, {"S9": "I1"}) == []


def test_live_stories_incremental():
    st = LiveStories(window_s=100)
    a = st.assign("k1", "Oil prices jump as OPEC agrees deeper output cuts", 0)
    b = st.assign("k2", "Oil prices jump as OPEC agrees deeper output cuts - Reuters", 10)
    c = st.assign("k3", "Apple unveils new iPhone at September event", 20)
    assert a["story_id"] == b["story_id"] != c["story_id"] and b["story_size"] == 2
    d = st.assign("k4", "Oil prices jump as OPEC agrees deeper output cuts", 500)   # window expired -> new story
    assert d["story_id"] != a["story_id"] and len(st.stories) == 1


def test_bocpd_detects_level_shift():
    rng = np.random.default_rng(0)
    det = MiniBOCPD(hazard_lambda=100)
    xs = np.r_[rng.normal(3.0, 0.2, 80), rng.normal(5.0, 0.2, 30)]
    probs = [det.update(float(x))["p_cp_recent"] for x in xs]
    assert max(probs[10:78]) < 0.5                       # quiet regime
    assert max(probs[80:84]) > 0.5                       # shift detected within a few steps
    assert det.update(5.0)["map_run_length"] < 40


def test_changepoint_consumer_fires_once(tmp_data):
    from app.realtime.bus import Message

    cp = ChangepointConsumer(min_run=10, cooldown_s=0)
    asyncio.run(cp.setup(StreamContext(tmp_data, None, asyncio.Queue(), asyncio.Event())))  # type: ignore[arg-type]
    ctx = StreamContext(tmp_data, None, asyncio.Queue(), asyncio.Event())  # type: ignore[arg-type]
    rng = np.random.default_rng(1)
    fired = []
    for i in range(60):
        n = int(math.expm1(rng.normal(4.0, 0.1))) if i < 45 else int(math.expm1(rng.normal(6.0, 0.1)))
        msg = Message(i + 1, "events", f"gdelt:slot:{i:04d}", "slot", None, 0.0,
                      {"kind": "slot", "slot": f"2026101200{i:04d}", "n_articles": n, "n_linked_articles": 0,
                       "themes_articles": {"energy": n}})
        fired += cp.handle_one(ctx, msg)
    names = {e.payload["series"] for e in fired}
    assert "theme:energy" in names and all(e.payload["direction"] == "up" for e in fired if e.payload["series"] == "theme:energy")
    again = cp.handle_one(ctx, Message(99, "events", "gdelt:slot:0001", "slot", None, 0.0,
                                       {"kind": "slot", "slot": "20261012000001", "themes_articles": {}}))
    assert again == []                                   # replayed slot is skipped
    assert (tmp_data.data_dir / "realtime" / "changepoint_state.pkl").exists()
