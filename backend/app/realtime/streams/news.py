"""Official press / statistics RSS and Polygon news (R1.2) -> raw.news.

RSS (official publishers only; parser = app.ingestion.news.rss_rows):
  Fed (press_all), ECB (press), BoE (news), BoJ (what's new), BLS (CPI, Employment Situation, PPI, JOLTS, ECI release
  feeds), Eurostat (dataset-update feed filtered to euro indicators), ONS (release calendar = latest
  releases). Each feed polls every `every_s` (central banks 120 s, statistics 300 s); entries dated in the future
  (> now + 5 min) are dropped. Key = item_id (sha1 of the normalised URL, same as news_items).

Polygon (`/v2/reference/news`, market-wide, newest first): one call per poll through PolygonClient (Bearer auth,
shared cross-process `polygon` bucket of 4 calls/min that the nightly jobs also use) -> 90 s on US hours, 300 s
otherwise, so the live path uses at most ~1/6 of the account budget. Rows via app.ingestion.news.polygon_news_rows.
"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope
from app.realtime.streams.base import StreamContext, StreamWorker
from app.realtime.streams.edgar import us_business_hours

log = get_logger(__name__)
LUX = ZoneInfo("Europe/Luxembourg")

#: source_id -> (url, countries, every_s, kind)
RSS_FEEDS: dict[str, tuple[str, list[str], float, str]] = {
    "rss_fed": ("https://www.federalreserve.gov/feeds/press_all.xml", ["US"], 120, "central_bank"),
    "rss_ecb": ("https://www.ecb.europa.eu/rss/press.html", ["EA"], 120, "central_bank"),
    "rss_boe": ("https://www.bankofengland.co.uk/rss/news", ["GB"], 120, "central_bank"),
    "rss_boj": ("https://www.boj.or.jp/en/rss/whatsnew.xml", ["JP"], 120, "central_bank"),
    "rss_bls_cpi": ("https://www.bls.gov/feed/cpi.rss", ["US"], 300, "statistics"),
    "rss_bls_empsit": ("https://www.bls.gov/feed/empsit.rss", ["US"], 300, "statistics"),
    "rss_bls_ppi": ("https://www.bls.gov/feed/ppi.rss", ["US"], 300, "statistics"),
    "rss_bls_jolts": ("https://www.bls.gov/feed/jolts.rss", ["US"], 600, "statistics"),
    "rss_bls_eci": ("https://www.bls.gov/feed/eci.rss", ["US"], 600, "statistics"),
    # rss_bea removed 2026-11-07 (A9): apps.bea.gov robots.txt `Disallow: /` (config/news_feeds.yaml rejects it too)
    "rss_eurostat": ("https://ec.europa.eu/eurostat/api/dissemination/catalogue/rss/en/statistics-update.rss", ["EA"],
                     300, "statistics"),
    "rss_ons": ("https://www.ons.gov.uk/releasecalendar?rss", ["GB"], 300, "statistics"),
}
PUBLISHER = {"rss_fed": "Federal Reserve", "rss_ecb": "ECB", "rss_boe": "Bank of England", "rss_boj": "Bank of Japan",
             "rss_bea": "BEA", "rss_eurostat": "Eurostat", "rss_ons": "ONS"}
#: Eurostat dataset codes that are euro indicators / headline macro (dataset-update feed is otherwise ~2k items)
EUROSTAT_KEEP = re.compile(r"^(PRC_HICP|UNE_RT|NAMQ_10_GDP|NAMQ_10_PE|STS_INPR|STS_TRTU|STS_INPP|STS_COPR|EI_BS|"
                           r"EI_BSCO|TEICP|TEILM|TEINA|TEIBS|TEIIS|EXT_ST_EA|EXT_ST_EU|GOV_10Q|JVS_Q|LC_LCI|PRC_IPI|"
                           r"BOP_EU6)", re.I)


def rss_items(source_id: str, content: bytes, countries: list[str], now: datetime) -> list[dict[str, Any]]:
    from app.ingestion.news import rss_rows  # noqa: PLC0415

    if source_id == "rss_eurostat":     # local (Luxembourg) timestamps without offset: own parser
        return eurostat_fallback(content, now)
    df = rss_rows(source_id, content, countries)
    out = []
    for r in df.iter_rows(named=True):
        ts = r["published_at"]
        if ts is None or ts > now + timedelta(minutes=5):
            continue
        if source_id == "rss_eurostat" and not EUROSTAT_KEEP.match((r["title"] or "").split(" ")[0]):
            continue
        out.append(r)
    return out


def eurostat_fallback(content: bytes, now: datetime) -> list[dict[str, Any]]:
    """The Eurostat feed uses 'YYYY-MM-DD HH:MM:SS.0' pubDates in Luxembourg local time (no offset)."""
    import feedparser  # noqa: PLC0415

    from app.ingestion.gdelt_gkg import item_id  # noqa: PLC0415

    out = []
    for e in feedparser.parse(content).entries:
        title, link = (e.get("title") or "").strip(), e.get("link")
        if not (title and link and EUROSTAT_KEEP.match(title.split(" ")[0])):
            continue
        raw = e.get("published") or e.get("updated") or ""
        try:
            ts = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=LUX).astimezone(UTC)
        except ValueError:
            continue
        if ts > now + timedelta(minutes=5):
            continue
        out.append({"item_id": item_id(link), "published_at": ts, "source_id": "rss_eurostat", "publisher": "Eurostat",
                    "url": link, "title": title, "snippet": None, "language": "en", "countries": ["EA"]})
    return out


def stable_url(url: str) -> str:
    """Scheme-insensitive URL (some official feeds, e.g. BoJ, alternate http:// and https:// between fetches)."""
    return re.sub(r"^http://", "https://", url.strip())


def news_envelope(r: dict[str, Any], kind: str, publisher: str | None = None) -> Envelope:
    from app.ingestion.gdelt_gkg import item_id  # noqa: PLC0415

    ts = r["published_at"]
    if r.get("url") and r["url"].startswith("http://"):
        r = {**r, "url": stable_url(r["url"]), "item_id": item_id(stable_url(r["url"]))}
    payload = {
        "item_id": r["item_id"], "source_id": r["source_id"], "publisher": publisher or r.get("publisher"),
        "title": r["title"], "url": r["url"], "snippet": r.get("snippet"), "language": r.get("language") or "en",
        "published_at": ts.isoformat().replace("+00:00", "Z"), "countries": r.get("countries") or [],
        "tickers": r.get("tickers") or [], "issuer_ids": r.get("issuer_ids") or [], "category": kind,
        "provider_sentiment": r.get("provider_sentiment"), "keywords": r.get("themes") or None,
    }
    return Envelope("raw.news", r["item_id"], payload, type=kind, ts_source=ts)


class RssStream(StreamWorker):
    """All curated RSS / Atom feeds (config/news_feeds.yaml, roadmap v3 N2.3); each feed has its own schedule.

    Polling through app.news_live.feeds.FeedPoller: conditional GET (ETag / If-Modified-Since), per-host politeness,
    exponential back-off, Retry-After. Without the config file the R1 official list (RSS_FEEDS) is used. Items whose
    id was already published are skipped before the bus (bus.seen), so a feed re-listing its entries costs nothing.
    """

    name = "rss_official"
    topic = "raw.news"
    source_id = "rss"
    interval_s = 5.0

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.poller: Any = None
        self.feeds: dict[str, Any] = {}
        self.feed_errors: dict[str, str] = {}
        self.feed_ok: dict[str, str] = {}
        self.stats.detail = "curated RSS / Atom feeds"

    def _specs(self, ctx: StreamContext) -> list[Any]:
        from app.news_live.feeds import FeedSpec, load_feeds  # noqa: PLC0415

        want = self.params.get("feeds")
        specs = self.params.get("specs") or load_feeds(ctx.settings)
        if not specs:   # no config: the R1 official list
            specs = [FeedSpec(id=k, url=u, publisher=PUBLISHER.get(k) or ("BLS" if k.startswith("rss_bls") else k),
                              kind=kind, tier=1, countries=cc, every_s=every, status="ok",
                              parser="eurostat" if k == "rss_eurostat" else "default")
                     for k, (u, cc, every, kind) in RSS_FEEDS.items()]
        if want:
            specs = [f for f in specs if f.id in set(want)]
        return specs

    async def setup(self, ctx: StreamContext) -> None:
        from app.news_live.feeds import FeedPoller  # noqa: PLC0415

        specs = self._specs(ctx)
        self.feeds = {f.id: f for f in specs}
        self.poller = FeedPoller(specs, ctx.settings, host_gap_s=float(self.params.get("host_gap_s", 5.0)),
                                 transport=self.params.get("transport"), persist=bool(self.params.get("persist", True)))
        self.stats.detail = f"{len(specs)} feeds"

    def fetch_due(self, now_dt: datetime, bus: Any = None) -> list[Envelope]:
        items = self.poller.poll_due(now_dt)
        if items and bus is not None and hasattr(bus, "seen"):
            known = bus.seen("raw.news", [r["item_id"] for r in items])
            items = [r for r in items if r["item_id"] not in known]
        st = self.poller.state
        self.feed_errors = {k: v.last_error for k, v in st.items() if v.consecutive_errors and v.last_error}
        self.feed_ok = {k: datetime.fromtimestamp(v.last_ok_at, UTC).isoformat(timespec="seconds")
                        for k, v in st.items() if v.last_ok_at}
        if self.feed_errors and len(self.feed_errors) == len(self.feeds) and len(self.feeds) > 1:
            raise RuntimeError(f"all RSS feeds failing: {list(self.feed_errors.items())[:2]}")
        self.stats.detail = (f"{len(self.feeds)} feeds, {len(self.feed_errors)} failing"
                             + (f" ({', '.join(sorted(self.feed_errors)[:6])})" if self.feed_errors else ""))
        out = []
        for r in items:
            spec = self.feeds.get(r["source_id"])
            e = news_envelope(r, r.get("category") or (spec.kind if spec else "news"), r.get("publisher"))
            e.payload.update(snippet=r.get("snippet"), tier=r.get("tier"),
                             published_estimated=bool(r.get("published_estimated")))
            if r.get("published_estimated"):
                e.ts_source = None   # undated entry: no publish -> bus latency for it
            out.append(e)
        return out

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        if self.poller is None or not self.poller.due():
            return []
        return await ctx.run_sync(self.fetch_due, ctx.clock(), ctx.bus)

    def feed_status(self) -> dict[str, Any]:
        return self.poller.status() if self.poller is not None else {}

    def close(self) -> None:
        if self.poller is not None:
            self.poller.close()


class PolygonNewsStream(StreamWorker):
    name = "polygon_news"
    topic = "raw.news"
    source_id = "polygon"
    interval_s = 90.0

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.pc: Any = None
        self.since: datetime | None = None
        self.stats.detail = "Polygon /v2/reference/news (shared 4/min budget)"

    def enabled(self, ctx: StreamContext) -> tuple[bool, str | None]:
        if not ctx.settings.secret("polygon_apikey"):
            return False, "missing POLYGON_APIKEY in .env"
        return True, None

    def interval(self, ctx: StreamContext) -> float:
        if "interval_s" in self.params:
            return float(self.params["interval_s"])
        return 90.0 if us_business_hours(ctx.clock()) else 300.0

    def fetch(self, now: datetime) -> list[dict[str, Any]]:
        from app.ingestion.news import polygon_news_rows  # noqa: PLC0415
        from app.ingestion.polygon_prices import PolygonClient  # noqa: PLC0415

        if self.pc is None:
            self.pc = PolygonClient()
        since = self.since or (now - timedelta(hours=6))
        j = self.pc.get("/v2/reference/news", {"published_utc.gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                               "order": "desc", "sort": "published_utc",
                                               "limit": int(self.params.get("limit", 100))})
        rows = polygon_news_rows(j.get("results") or [])
        out = list(rows.iter_rows(named=True))
        if out:
            newest = max(r["published_at"] for r in out)
            self.since = max(self.since or newest, newest - timedelta(minutes=30))
        return out

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        rows = await ctx.run_sync(self.fetch, ctx.clock())
        if rows and ctx.linker is not None:
            tick = await ctx.run_sync(ctx.linker.tickers, [t for r in rows for t in (r["tickers"] or [])])
            for r in rows:
                r["issuer_ids"] = list(dict.fromkeys(tick[t] for t in (r["tickers"] or []) if t in tick))
        return [news_envelope(r, "news") for r in rows]
