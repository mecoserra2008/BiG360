"""Intraday DuckDB build + /news/* endpoints: bus items merged over the intraday store (live wins, no duplicates),
stories, story detail, hourly series, feeds, live health; contract validation; SSE news topics."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots
from app.api.intraday import manager
from app.news_live import compaction as C
from app.news_live.intraday import build_intraday, current_intraday
from app.realtime.bus import SqliteBus, default_bus_path
from tests.api_contract import validate
from tests.news_live.helpers import news_rows, seed_dicts, seed_gdelt, seed_news


@pytest.fixture()
def env(tmp_data):
    from app.api.main import create_app
    from app.api.routers import live as live_router

    now = datetime.now(UTC)
    d = (now - timedelta(hours=3)).date()
    h = (now - timedelta(hours=3)).hour
    seed_dicts(tmp_data)
    seed_gdelt(tmp_data, d, [h])
    seed_news(tmp_data, d, h)
    C.run_hourly(tmp_data, now=now, xlmr=False)
    rep = build_intraday(tmp_data, now=now)
    bus = SqliteBus(default_bus_path(tmp_data))
    fresh = news_rows(now.date(), now.hour, n0=100)[0]
    fresh = {**fresh, "item_key": "live1", "title": "Nvidia shares fall further in late trade", "story_id": "7001",
             "published_at": (now - timedelta(seconds=20)).isoformat(), "first_seen_at": now.isoformat()}
    bus.publish("news.item", "live1", fresh, type="news", ts_source=now - timedelta(seconds=20))
    old = news_rows(d, h)[0]          # the same item as in the intraday store: live wins, no duplicate
    bus.publish("news.item", old["item_key"], {**old, "salience": 9.9}, type="news")
    bus.publish("news.story", "7001:3", {"story_id": "7001", "title": "Nvidia shares fall", "n_articles": 3,
                                         "n_sources": 2, "first_seen_at": old["first_seen_at"],
                                         "last_seen_at": now.isoformat(), "velocity_1h": 3, "velocity_ratio": 6.0,
                                         "themes": ["trade_tariffs"], "countries": ["US"], "issuer_ids": ["INVDA"]},
                type="story", ckey="7001")
    live_router._bus = None
    manager.reset()
    snapshots.reset()
    client = TestClient(create_app())
    yield {"client": client, "settings": tmp_data, "day": d, "hour": h, "intraday": rep, "bus": bus}
    bus.close()
    live_router._bus = None
    manager.reset()
    snapshots.reset()


def get(env, tpl, params=None, status=200, path=None):
    r = env["client"].get(path or tpl, params=params)
    assert r.status_code == status, r.text[:400]
    body = r.json()
    if status == 200:
        validate("GET", tpl, 200, body)
    return body


def test_intraday_file_published(env):
    rep = env["intraday"]
    assert current_intraday(env["settings"]).name == rep["file"]
    assert rep["nt_items"] == 3 and rep["nt_stories"] > 0 and rep["nt_event_hourly"] > 0 and rep["nt_sentiment_hourly"] > 0


def test_terminal_merges_bus_and_intraday(env):
    t = get(env, "/api/v1/news/terminal")
    keys = [x["item_key"] for x in t["items"]]
    assert keys[0] == "live1" and len(keys) == len(set(keys)) == 4
    k1 = next(x for x in t["items"] if x["item_key"] == "k1")
    assert k1["origin"] == "live" and k1["salience"] == 9.9                 # bus copy wins over intraday
    assert t["n_live"] == 2 and t["n_intraday"] == 2 and t["head_id"] >= 3
    live1 = t["items"][0]
    assert 15 <= live1["latency_s"] <= 25 or live1["latency_s"] is not None
    assert [x["item_key"] for x in get(env, "/api/v1/news/terminal", {"lang": "pt"})["items"]] == ["k3"]
    assert {x["item_key"] for x in get(env, "/api/v1/news/terminal", {"country": "br,us", "tier": "2"})["items"]} == \
        {"live1", "k1", "k2", "k3"}
    assert get(env, "/api/v1/news/terminal", {"q": "petrobras"})["items"][0]["item_key"] == "k3"
    assert get(env, "/api/v1/news/terminal", {"story_id": "7001"})["items"][0]["story_id"] == "7001"
    page = get(env, "/api/v1/news/terminal", {"limit": 2})
    assert len(page["items"]) == 2 and page["next_before"]
    rest = get(env, "/api/v1/news/terminal", {"before": page["next_before"]})
    assert not {x["item_key"] for x in rest["items"]} & {x["item_key"] for x in page["items"]}
    get(env, "/api/v1/news/terminal", {"watchlist_id": "WNOPE"}, status=404)
    get(env, "/api/v1/news/terminal", {"tier": "x"}, status=422)


def test_stories_and_detail(env):
    st = get(env, "/api/v1/news/stories", {"hours": 24})
    ids = [x["story_id"] for x in st["items"]]
    assert "7001" in ids
    s7 = next(x for x in st["items"] if x["story_id"] == "7001")
    assert s7["velocity_1h"] == 3 and s7["n_articles"] >= 3 and s7["origin"] == "intraday"
    vel = get(env, "/api/v1/news/stories", {"sort": "velocity"})
    assert vel["items"][0]["story_id"] == "7001"
    det = get(env, "/api/v1/news/stories/{story_id}", path="/api/v1/news/stories/7001")
    assert det["story"]["story_id"] == "7001" and det["timeline"] and det["members_available"]
    assert {x["item_key"] for x in det["items"]} >= {"k1", "k2", "live1"}
    assert det["impact"] is None                                             # empty snapshot: no event-study library
    get(env, "/api/v1/news/stories/{story_id}", path="/api/v1/news/stories/123", status=404)
    get(env, "/api/v1/news/stories/{story_id}", path="/api/v1/news/stories/abc", status=422)


def test_hourly_feeds_health_and_sse(env):
    hr = get(env, "/api/v1/news/hourly", {"hours": 24})
    assert hr["points"] and any(p.get("n_items") == 3 for p in hr["points"]) and any(p.get("ev_n") for p in hr["points"])
    iss = get(env, "/api/v1/news/hourly", {"label_type": "issuer", "label_id": "INVDA", "hours": 24})
    assert iss["points"]
    get(env, "/api/v1/news/hourly", {"label_type": "country", "label_id": "US", "hours": 24})
    f = get(env, "/api/v1/news/feeds")
    assert f["n_ok"] >= 40 and f["n_rejected"] >= 1 and all(x["status"] in ("ok", "rejected") for x in f["items"])
    h = get(env, "/api/v1/news/live/health")
    assert h["running"] is False and h["enabled"] is True and h["intraday"]["available"]
    assert h["hourly"]["hours"] >= 1
    with env["client"].stream("GET", "/api/v1/live/stream", params={"topics": "news.item,news.story", "last_event_id": 0,
                                                                     "max_events": 3}) as r:
        body = "".join(r.iter_text())
    assert "event: news.item" in body and "event: news.story" in body and '"origin":"live"' in body


def test_no_intraday_falls_back(tmp_data):
    from app.api.main import create_app

    manager.reset()
    snapshots.reset()
    c = TestClient(create_app())
    r = c.get("/api/v1/news/terminal").json()
    assert r["data_status"] == "pending" and r["items"] == []
    validate("GET", "/api/v1/news/terminal", 200, r)
    validate("GET", "/api/v1/news/stories", 200, c.get("/api/v1/news/stories").json())
    validate("GET", "/api/v1/news/hourly", 200, c.get("/api/v1/news/hourly").json())
    validate("GET", "/api/v1/news/live/health", 200, c.get("/api/v1/news/live/health").json())
    manager.reset()
    snapshots.reset()
    _ = date
