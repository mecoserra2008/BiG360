"""R1.4 SSE endpoint: a test client streams events published while connected, heartbeats, Last-Event-ID resume,
topic / type filters, and the live overlay without a serving snapshot."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.deps import snapshots


def parse_sse(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        ev: dict = {}
        for line in block.splitlines():
            if line.startswith(":") or not line.strip():
                continue
            k, _, v = line.partition(": ")
            ev[k] = v
        if "event" in ev:
            ev["data"] = json.loads(ev["data"]) if "data" in ev else None
            out.append(ev)
    return out


@pytest.fixture()
def live_env(tmp_data):
    from app.api.main import create_app
    from app.api.routers import live as live_router
    from app.realtime.bus import SqliteBus, default_bus_path

    snapshots.reset()
    bus = SqliteBus(default_bus_path(tmp_data))
    live_router._bus = None
    yield {"bus": bus, "client": TestClient(create_app())}
    bus.close()
    live_router._bus = None
    snapshots.reset()


def _filing(bus, i: int) -> int:
    return bus.publish("filings", f"sec_edgar:acc-{i}", {"filing_id": f"sec_edgar:acc-{i}", "source_id": "sec_edgar",
                                                          "form_type": "8-K", "form_class": "current_report"},
                       type="filing")


def test_stream_delivers_events_published_while_connected(live_env):
    bus, c = live_env["bus"], live_env["client"]
    _filing(bus, 0)                       # before connecting: not delivered (stream starts at the head)

    def later():
        time.sleep(2.5)                   # > heartbeat_s: at least one heartbeat first
        _filing(bus, 1)
        bus.publish("news", "n1", {"item_id": "n1", "title": "x"}, type="news")   # other topic: filtered
        _filing(bus, 2)

    threading.Thread(target=later, daemon=True).start()
    t0 = time.time()
    with c.stream("GET", "/api/v1/live/stream", params={"topics": "filings", "max_events": 2, "timeout_s": 15,
                                                        "heartbeat_s": 1}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers.get("content-encoding") is None          # not gzip-buffered
        body = "".join(r.iter_text())
    evs = parse_sse(body)
    data = [e for e in evs if e["event"] == "filings"]
    assert [e["data"]["key"] for e in data] == ["sec_edgar:acc-1", "sec_edgar:acc-2"]
    assert all(int(e["id"]) == e["data"]["id"] for e in data)
    assert data[0]["data"]["item"]["filing_id"] == "sec_edgar:acc-1" and data[0]["data"]["item"]["origin"] == "live"
    assert evs[0]["event"] == "hello" and any(e["event"] == "heartbeat" for e in evs)
    assert time.time() - t0 < 10


def test_last_event_id_resume_and_replay(live_env):
    bus, c = live_env["bus"], live_env["client"]
    ids = [_filing(bus, i) for i in range(5)]
    with c.stream("GET", "/api/v1/live/stream", params={"topics": "filings", "max_events": 2},
                  headers={"Last-Event-ID": str(ids[2])}) as r:
        evs = [e for e in parse_sse("".join(r.iter_text())) if e["event"] == "filings"]
    assert [int(e["id"]) for e in evs] == ids[3:]
    with c.stream("GET", "/api/v1/live/stream", params={"topics": "filings", "replay": 2, "max_events": 2}) as r:
        evs = [e for e in parse_sse("".join(r.iter_text())) if e["event"] == "filings"]
    assert [int(e["id"]) for e in evs] == ids[3:]
    with c.stream("GET", "/api/v1/live/stream", params={"topics": "filings", "last_event_id": 0, "types": "nope",
                                                        "timeout_s": 1}) as r:
        evs = [e for e in parse_sse("".join(r.iter_text())) if e["event"] == "filings"]
    assert evs == []


def test_live_rest_without_snapshot(live_env):
    bus, c = live_env["bus"], live_env["client"]
    _filing(bus, 7)
    r = c.get("/api/v1/live/filings").json()
    assert r["n_live"] == 1 and r["items"][0]["filing_id"] == "sec_edgar:acc-7" and r["data_status"] == "ok"
    assert c.get("/api/v1/live/filings").headers.get("x-cache") is None          # never cached
    st = c.get("/api/v1/live/status").json()
    assert st["running"] is False and st["topics"][0]["topic"] == "filings"
    assert c.get("/api/v1/live/events").json()["items"] == []
