"""Curated feed config + RSS / Atom parsing on recorded fixtures + the conditional-GET poller (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.news_live.feeds import (SNIPPET_MAX, FeedPoller, FeedSpec, clean_snippet, load_feeds, parse_feed,
                                 url_hash, url_key)

F = Path(__file__).with_name("fixtures")
NOW = datetime(2026, 10, 22, 22, 0, tzinfo=UTC)


def spec(fid: str = "rss_x", url: str = "https://feeds.example.com/a.xml", **kw) -> FeedSpec:
    return FeedSpec(id=fid, url=url, publisher=kw.pop("publisher", "Example"), status="ok", **kw)


def test_config_is_curated_and_complete():
    feeds = load_feeds(include_inactive=True)
    ids = [f.id for f in feeds]
    assert len(ids) == len(set(ids)) and len(feeds) >= 50
    ok = [f for f in feeds if f.status == "ok"]
    rejected = [f for f in feeds if f.status == "rejected"]
    assert len(ok) >= 40 and rejected
    for f in feeds:
        assert f.id.startswith("rss_") and f.url.startswith("https://") and f.terms, f.id
        assert f.kind in ("central_bank", "statistics", "regulator", "exchange", "publisher", "aggregator")
        assert 1 <= f.tier <= 3 and 30 <= f.every_s <= 900
    for f in rejected:
        assert f.reason, f"{f.id}: a rejected feed records why"
    for f in ok:
        assert f.robots in ("allowed", "no_robots"), f"{f.id}: robots verified"
    # the groups the plan asked for are covered
    pubs = {f.publisher for f in ok}
    for want in ("Federal Reserve", "ECB", "Bank of England", "Bank of Japan", "Bank of Canada",
                 "Reserve Bank of Australia", "Swiss National Bank", "Sveriges Riksbank", "Norges Bank",
                 "Banco Central do Brasil", "SEC", "ESMA", "FCA", "MarketWatch", "WSJ", "Financial Times",
                 "Nikkei Asia", "SCMP", "Handelsblatt", "Expansion", "Valor Economico", "Jornal de Negocios", "ECO"):
        assert want in pubs, want
    assert {f.id for f in load_feeds()} == {f.id for f in ok}          # active = ok (+ unverified)


@pytest.mark.parametrize("fid,fname,lang,n", [("rss_ft_home", "rss_ft_home.xml", "en", 8),
                                              ("rss_statcan", "rss_statcan.atom", "en", 8),
                                              ("rss_eco", "rss_eco.xml", "pt", 8),
                                              ("rss_wsj_markets", "rss_wsj_markets.xml", "en", 8)])
def test_recorded_feeds_parse_headline_snippet_only(fid, fname, lang, n):
    fs = {f.id: f for f in load_feeds(include_inactive=True)}
    items = parse_feed(fs[fid], (F / fname).read_bytes(), datetime.now(UTC) + timedelta(days=3650))
    assert len(items) == n
    for it in items:
        assert it["title"] and "<" not in it["title"] and it["url"].startswith("https://")
        assert it["snippet"] is None or (len(it["snippet"]) <= SNIPPET_MAX and "<" not in it["snippet"])
        assert it["language"] == lang and it["source_id"] == fid and it["tier"] == fs[fid].tier
        assert it["published_at"].tzinfo is not None and not it["published_estimated"]
        assert len(it["item_id"]) == 16
    assert [i["published_at"] for i in items] == sorted((i["published_at"] for i in items), reverse=True)


def test_parse_rules_future_undated_dupes_and_first_poll_age():
    rss = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
      <item><title>Old &amp; dated</title><link>http://ex.com/a</link><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate>
        <description><![CDATA[<p>Hello <b>world</b> {'x' * 600}</p>]]></description></item>
      <item><title>Fresh</title><link>https://ex.com/b?utm_source=rss</link><pubDate>Thu, 22 Oct 2026 21:30:00 GMT</pubDate></item>
      <item><title>Fresh again</title><link>https://ex.com/b?utm_source=rss</link><pubDate>Thu, 22 Oct 2026 21:31:00 GMT</pubDate></item>
      <item><title>From the future</title><link>https://ex.com/c</link><pubDate>Fri, 23 Oct 2026 10:00:00 GMT</pubDate></item>
      <item><title>No date</title><link>https://ex.com/d</link></item>
    </channel></rss>""".encode()
    items = parse_feed(spec(), rss, NOW)
    titles = [i["title"] for i in items]
    assert "From the future" not in titles and titles.count("Fresh") + titles.count("Fresh again") == 1
    old = next(i for i in items if i["title"] == "Old & dated")
    assert old["url"] == "https://ex.com/a"                       # scheme-stable item id
    assert old["snippet"].startswith("Hello world") and len(old["snippet"]) <= SNIPPET_MAX
    nd = next(i for i in items if i["title"] == "No date")
    assert nd["published_estimated"] and nd["published_at"] == NOW
    first = parse_feed(spec(), rss, NOW, max_age=timedelta(hours=48))
    assert "Old & dated" not in [i["title"] for i in first]       # first poll: no backlog flood
    assert clean_snippet(None) is None and clean_snippet("<br/>") is None


def test_url_hash_matches_events_v2_url_key():
    import polars as pl

    from app.events_v2.parse import hash_series, url_key_expr

    urls = ["https://www.Example.com/News/Story-1/?utm=x#frag", "http://www2.example.com/a/b/", "https://ex.com/x"]
    keys = pl.DataFrame({"u": urls}).select(url_key_expr(pl.col("u"))).to_series()
    assert keys.to_list() == [url_key(u) for u in urls]
    assert hash_series(keys).to_list() == [url_hash(u) for u in urls]


class Server:
    def __init__(self, body: bytes):
        self.body = body
        self.calls: list[httpx.Request] = []
        self.mode = "ok"

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        if self.mode == "429":
            return httpx.Response(429, headers={"Retry-After": "600"})
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, content=self.body, headers={"ETag": '"v1"', "Last-Modified": "Thu, 22 Oct 2026 21:00:00 GMT"})


def test_poller_conditional_get_politeness_and_backoff(tmp_data):
    body = (F / "rss_ft_home.xml").read_bytes()
    srv = Server(body)
    clock = [1000.0]
    a = spec("rss_a", "https://feeds.example.com/a.xml", every_s=60)
    b = spec("rss_b", "https://feeds.example.com/b.xml", every_s=60)       # same host as a
    c = spec("rss_c", "https://other.example.org/c.xml", every_s=60)
    p = FeedPoller([a, b, c], tmp_data, host_gap_s=5.0, transport=httpx.MockTransport(srv.handler),
                   clock=lambda: clock[0], persist=True)
    clock[0] += 30
    newest = max(i["published_at"] for i in parse_feed(a, body, datetime.now(UTC) + timedelta(days=3650)))
    items = p.poll_due(newest + timedelta(hours=1))
    hosts = [r.url.host for r in srv.calls]
    assert hosts.count("feeds.example.com") == 1 and hosts.count("other.example.org") == 1   # one per host per round
    assert items and all(i["source_id"] in ("rss_a", "rss_c") for i in items)
    clock[0] += 6                                                            # host gap passed: b now
    p.poll_due()
    assert srv.calls[-1].url.path == "/b.xml"
    clock[0] += 120                                                          # every_s passed: conditional GETs
    n = len(srv.calls)
    assert p.poll_due() == [] and len(srv.calls) > n
    clock[0] += 6                                                            # second feed of the shared host
    assert p.poll_due() == []
    assert srv.calls[-1].headers.get("if-none-match") == '"v1"'
    assert srv.calls[-1].headers.get("if-modified-since") == "Thu, 22 Oct 2026 21:00:00 GMT"
    st = p.status()
    assert st["rss_a"]["not_modified"] >= 1 and st["rss_a"]["last_status"] == 304
    srv.mode = "429"
    clock[0] += 200
    for _ in range(3):
        p.poll_due()
        clock[0] += 6
    st = p.status()
    failing = [k for k, v in st.items() if v["consecutive_errors"]]
    assert failing and all(st[k]["last_error"] == "HTTP 429" for k in failing)
    assert all(p.state[k].next_at - clock[0] >= 400 for k in failing)       # Retry-After 600 honoured
    p.close()
    # validators survive a restart
    p2 = FeedPoller([a, b, c], tmp_data, transport=httpx.MockTransport(srv.handler), persist=True)
    assert p2.state["rss_a"].etag == '"v1"'
    p2.close()
