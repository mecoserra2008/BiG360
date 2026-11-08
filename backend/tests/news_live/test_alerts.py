"""watchlist_news / story_velocity: pure evaluators, live consumer dedupe per story, max_per_day, untargeted bursts,
API validation, nightly no-op."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.news_live.alerts import news_triggers, validate
from app.realtime.alerts_live import AlertConsumer
from app.realtime.bus import Message
from app.realtime.streams.base import StreamContext

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def item(story="S1", sec="SEC1", tier=2, sent=-0.6, title="Acme recalls 2 million cars", key="k1"):
    return {"item_key": key, "story_id": story, "security_ids": [sec], "issuer_ids": ["I1"], "tier": tier,
            "sentiment_score": sent, "title": title, "publisher": "WSJ", "language": "en", "source_id": "rss_wsj",
            "published_at": "2026-10-22T14:59:00Z", "url": "https://wsj.com/x"}


def story(v=8, ratio=6.0, n_src=4, themes=("product_recall",), countries=("US",), issuers=("I1",), sid="S9"):
    return {"story_id": sid, "title": "Acme recall widens", "velocity_1h": v, "velocity_ratio": ratio,
            "baseline_per_h": 1.0, "n_articles": 12, "n_sources": n_src, "themes": list(themes),
            "countries": list(countries), "issuer_ids": list(issuers), "last_seen_at": "2026-10-22T14:59:00Z"}


def test_watchlist_news_triggers():
    r = {"rule_type": "watchlist_news"}
    t = news_triggers(r, validate("watchlist_news", {}), ["SEC1", "SEC2"], "news.item", "news", item(), {}, NOW)
    assert len(t) == 1 and t[0]["security_id"] == "SEC1" and t[0]["dedupe"] == "S1"
    assert t[0]["message"].startswith("news (negative): Acme recalls") and t[0]["as_of_date"].isoformat() == "2026-10-22"
    # issuer match through the security -> issuer map
    t = news_triggers(r, {}, ["SECX"], "news.item", "news", {**item(), "security_ids": []}, {"SECX": "I1"}, NOW)
    assert [x["security_id"] for x in t] == ["SECX"]
    assert news_triggers(r, validate("watchlist_news", {}), ["SEC1"], "news.item", "gdelt", item(tier=4), {}, NOW) == []
    assert news_triggers(r, {"min_abs_sentiment": 0.8}, ["SEC1"], "news.item", "news", item(), {}, NOW) == []
    assert news_triggers(r, {}, ["SEC1"], "news.story", "story", story(), {}, NOW) == []


def test_story_velocity_triggers():
    r = {"rule_type": "story_velocity"}
    p = validate("story_velocity", {})
    t = news_triggers(r, p, [], "news.story", "story", story(), {}, NOW)
    assert t[0]["security_id"] == "news:S9" and "8 articles in 60 min (6.0x baseline" in t[0]["message"]
    assert news_triggers(r, p, [], "news.story", "story", story(v=3), {}, NOW) == []
    assert news_triggers(r, p, [], "news.story", "story", story(ratio=2.0), {}, NOW) == []
    assert news_triggers(r, p, [], "news.story", "story", story(n_src=1), {}, NOW) == []
    assert news_triggers(r, {**p, "themes": ["sanctions"]}, [], "news.story", "story", story(), {}, NOW) == []
    t = news_triggers(r, p, ["SECA", "SECB"], "news.story", "story", story(), {"SECA": "I1", "SECB": "I2"}, NOW)
    assert [x["security_id"] for x in t] == ["SECA"]


def test_validate_params():
    assert validate("watchlist_news", {"tiers": [2, 1]})["tiers"] == [1, 2]
    with pytest.raises(ValueError):
        validate("watchlist_news", {"tiers": [9]})
    with pytest.raises(ValueError):
        validate("story_velocity", {"min_ratio": 0.5})


class Store:
    def __init__(self, rules, members=None):
        self.rules, self.members, self.rows = rules, members or {}, []

    def load(self):
        return self.rules, self.members, []

    def append(self, rows):
        self.rows += rows


def msg(i, topic, payload, typ):
    return Message(i, topic, f"{topic}:{i}", typ, "2026-10-22T14:59:00Z", NOW.timestamp(), payload)


def test_live_consumer_one_event_per_story_and_cap(tmp_data):
    rules = [{"alert_id": "A1", "security_id": None, "watchlist_id": "W1", "rule_type": "watchlist_news",
              "params": json.dumps({"max_per_day": 2}), "cooldown_days": 0},
             {"alert_id": "A2", "security_id": None, "watchlist_id": None, "rule_type": "story_velocity",
              "params": json.dumps({}), "cooldown_days": 0}]
    st = Store(rules, {"W1": ["SEC1"]})
    c = AlertConsumer(store=st)
    ctx = StreamContext(settings=tmp_data, bus=None, out=asyncio.Queue(), stop=asyncio.Event(), clock=lambda: NOW)
    msgs = [msg(1, "news.item", item(story="S1", key="a"), "news"),
            msg(2, "news.item", item(story="S1", key="b"), "news"),      # same story: no second event
            msg(3, "news.item", item(story="S2", key="c"), "news"),
            msg(4, "news.item", item(story="S3", key="d"), "news"),      # third story the same day: capped (2)
            msg(5, "news.story", story(), "story"),
            msg(6, "news.story", story(v=9), "story")]                    # same story again: no second event
    out = c.handle_batch(ctx, msgs)
    rows = st.rows
    assert sorted((r["alert_id"], json.loads(r["payload"])["dedupe"]) for r in rows) == \
        [("A1", "S1"), ("A1", "S2"), ("A2", "S9")]
    assert {e.topic for e in out} == {"alerts"} and len(out) == 3
    assert next(e for e in out if e.payload["rule_type"] == "story_velocity").payload["security_id"] == "news:S9"
    # a later batch with the same stories fires nothing
    assert c.handle_batch(ctx, [msg(7, "news.item", item(story="S1", key="e"), "news")]) == []


def test_nightly_evaluator_treats_news_rules_as_noop():
    from app.scheduler.alerts import EVALUATORS, RULE_TYPES

    for rt in ("watchlist_news", "story_velocity"):
        assert rt in RULE_TYPES and EVALUATORS[rt]({}, ["S1"], None, NOW.date()) == []
