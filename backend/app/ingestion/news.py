"""News ingestion -> news_items (raw rows; sentiment_* columns are filled later by signals-alt). Job `news_ingest`.

Sources
* Polygon/Massive `/v2/reference/news` (US): publisher, tickers[], description, and `insights[]` (per-ticker
  provider sentiment + reasoning, stored in `provider_sentiment`). Market-wide mode (1 call = up to 1000 items) or
  per ticker; every call goes through the shared 5/min Polygon budget (app.ingestion.polygon_prices.PolygonClient).
* GDELT DOC 2.0 ArtList for a watchlist of <= 150 names (`"<name>"`, timespan 1d, maxrecords 75). The server allows
  one request every 5 s per IP and 429s aggressively (research §7.1) -> shared `gdelt_doc` bucket (1/5 s) plus our own
  backoff 15/30/60/90 s and a circuit breaker (stop after 4 consecutive names exhausted by 429s).
  Coverage health metric (research: DOC volumes collapsed early Oct-2026): one TimelineVolRaw call; `norm` = total
  articles GDELT monitored per day; health = last complete day / median of prior 30 days; < 0.5 -> degraded.
  Saved to data/state/gdelt_doc_health.json (history appended) and returned in the job message.
* Regulator / central-bank RSS (stable official feeds): Federal Reserve press releases, ECB press, SEC press releases
  (source_id rss_fed / rss_ecb / rss_sec; macro/regulatory news, issuer_ids = []).
  Google News RSS works (research §8) but its ToS discourage automation -> opt-in only (`google_news=True`), <= 1 query
  per 6 s.

Dedup: item_id = sha1(normalised URL)[:16]; existing item_ids are never rewritten (keeps sentiment columns).
PIT: published_at (UTC) is the knowledge time; knowledge_date = published_at in America/New_York for US sources,
UTC date otherwise.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.ratelimit import get_limiter
from app.ingestion.base import MissingApiKey
from app.ingestion.gdelt_gkg import item_id, write_news
from app.normalize.prices import SEED_GLOBAL, SEED_US, _table_has_data

log = get_logger(__name__)

NY = ZoneInfo("America/New_York")
DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
#: A9 2026-11-07: robots.txt of news.google.com disallows /rss/ -> the opt-in google_news option is off (robots_policy)
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"
RSS_FEEDS = {
    "rss_fed": ("https://www.federalreserve.gov/feeds/press_all.xml", ["US"]),
    "rss_ecb": ("https://www.ecb.europa.eu/rss/press.html", []),
    "rss_sec": ("https://www.sec.gov/news/pressreleases.rss", ["US"]),
}
_LANG = {"English": "en", "Spanish": "es", "French": "fr", "German": "de", "Portuguese": "pt", "Italian": "it",
         "Japanese": "ja", "Korean": "ko", "Chinese": "zh", "Russian": "ru", "Dutch": "nl", "Arabic": "ar",
         "Turkish": "tr", "Polish": "pl", "Swedish": "sv", "Indonesian": "id", "Hindi": "hi"}

NEWS_SCHEMA = {
    "item_id": pl.Utf8, "year_month": pl.Utf8, "published_at": pl.Datetime("us", "UTC"), "knowledge_date": pl.Date,
    "source_id": pl.Utf8, "publisher": pl.Utf8, "url": pl.Utf8, "title": pl.Utf8, "snippet": pl.Utf8,
    "language": pl.Utf8, "issuer_ids": pl.List(pl.Utf8), "link_confidence": pl.List(pl.Float64),
    "countries": pl.List(pl.Utf8), "themes": pl.List(pl.Utf8), "gdelt_tone": pl.Float64, "tickers": pl.List(pl.Utf8),
    "entities": pl.List(pl.Utf8), "provider_sentiment": pl.Utf8, "ingested_at": pl.Datetime("us", "UTC"),
}


def _frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=NEWS_SCHEMA) if rows else pl.DataFrame(schema=NEWS_SCHEMA)


def _ticker_issuers(tickers: list[str], settings: Settings | None = None) -> dict[str, str]:
    """polygon ticker -> issuer_id through app.normalize.identity (empty before ref-identity has published)."""
    if not tickers or not _table_has_data("identifier_map", settings):
        return {}
    try:
        from app.normalize.identity import lookup_many  # noqa: PLC0415

        res = lookup_many("polygon_ticker", tickers, min_confidence=0.9)
        if res.is_empty():
            res = lookup_many("ticker_mic", [f"{t}@XNYS" for t in tickers])
        return {r["input_value"].split("@")[0]: r["issuer_id"] for r in res.iter_rows(named=True) if r["issuer_id"]}
    except Exception as e:  # noqa: BLE001
        log.debug("identity lookup failed: %s", e)
        return {}


# ------------------------------------------------------------------------------------------------ Polygon


def polygon_news_rows(results: list[dict[str, Any]], issuer_map: dict[str, str] | None = None) -> pl.DataFrame:
    now = datetime.now(UTC)
    issuer_map = issuer_map or {}
    rows = []
    for r in results:
        url, title, pub = r.get("article_url"), r.get("title"), r.get("published_utc")
        if not (url and title and pub):
            continue
        ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).astimezone(UTC)
        tickers = [t for t in (r.get("tickers") or []) if t]
        iss = list(dict.fromkeys(issuer_map[t] for t in tickers if t in issuer_map))
        insights = r.get("insights") or []
        rows.append({
            "item_id": item_id(url), "year_month": ts.strftime("%Y-%m"), "published_at": ts,
            "knowledge_date": ts.astimezone(NY).date(), "source_id": "polygon_news",
            "publisher": (r.get("publisher") or {}).get("name"), "url": url, "title": title.strip(),
            "snippet": (r.get("description") or "")[:500] or None, "language": "en", "issuer_ids": iss,
            "link_confidence": [1.0] * len(iss), "countries": ["US"], "themes": r.get("keywords") or None,
            "gdelt_tone": None, "tickers": tickers or None, "entities": None,
            "provider_sentiment": orjson.dumps(insights).decode() if insights else None, "ingested_at": now,
        })
    return _frame(rows).unique(subset=["item_id"], keep="first", maintain_order=True)


def fetch_polygon_news(settings: Settings, tickers: list[str] | None, since: datetime, per_ticker_limit: int = 100,
                       market_pages: int = 2) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from app.ingestion.polygon_prices import PolygonClient  # noqa: PLC0415

    pc = PolygonClient(settings)
    out: list[dict[str, Any]] = []
    meta: dict[str, Any] = {"failed": {}}
    base = {"published_utc.gte": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "order": "desc", "sort": "published_utc"}
    if tickers:
        for t in tickers:
            try:
                out += pc.paginate("/v2/reference/news", {**base, "ticker": t, "limit": per_ticker_limit}, max_pages=1)
            except Exception as e:  # noqa: BLE001
                meta["failed"][t] = f"{type(e).__name__}: {e}"[:200]
    else:
        out += pc.paginate("/v2/reference/news", {**base, "limit": 1000}, max_pages=market_pages)
    meta["calls"] = pc.n_calls
    meta["429s"] = pc.n_429
    return out, meta


# ------------------------------------------------------------------------------------------------ GDELT DOC


class DocClient:
    def __init__(self, settings: Settings, max_attempts: int = 4):
        self.http = HttpClient("gdelt_doc", settings=settings, max_retries=0, timeout=60)
        self.limiter = get_limiter("gdelt_doc")
        self.max_attempts = max_attempts
        self.calls = 0
        self.n_429 = 0

    def get(self, params: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in range(self.max_attempts):
            self.calls += 1
            try:
                r = self.http.get(DOC_URL, {**params, "format": "json"}, cache_ttl=0, raise_for_status=False)
            except HttpError as e:
                log.warning("gdelt doc transport error: %s", e)
                time.sleep(10 * (attempt + 1))
                continue
            if r.status_code == 429 or (r.status_code == 200 and r.content[:6] == b"Please"):
                self.n_429 += 1
                wait = (15, 30, 60, 90)[min(attempt, 3)]
                self.limiter.penalize(wait)
                log.info("gdelt doc 429 (attempt %d); backing off %ds", attempt + 1, wait)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return None
            if not r.content.strip():
                return {}
            try:
                return r.json()
            except ValueError:  # textual error (e.g. keyword too short)
                log.info("gdelt doc non-JSON reply: %s", r.text[:120])
                return None
        raise TimeoutError("gdelt doc: 429 retries exhausted")


def doc_artlist_rows(name: str, payload: dict[str, Any], issuer_id: str | None, ticker: str | None) -> pl.DataFrame:
    now = datetime.now(UTC)
    rows = []
    seen_titles: set[str] = set()
    for a in payload.get("articles", []) or []:
        url, title, seen = a.get("url"), (a.get("title") or "").strip(), a.get("seendate")
        if not (url and title and seen):
            continue
        key = " ".join(title.lower().split())
        if key in seen_titles:  # syndicated duplicates (research: dedupe on normalised title)
            continue
        seen_titles.add(key)
        ts = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        rows.append({
            "item_id": item_id(url), "year_month": ts.strftime("%Y-%m"), "published_at": ts, "knowledge_date": ts.date(),
            "source_id": "gdelt_doc", "publisher": a.get("domain"), "url": url, "title": title, "snippet": None,
            "language": _LANG.get(a.get("language") or "", None), "issuer_ids": [issuer_id] if issuer_id else [],
            "link_confidence": [0.8] if issuer_id else [], "countries": None, "themes": None, "gdelt_tone": None,
            "tickers": [ticker] if ticker else None, "entities": [name], "provider_sentiment": None, "ingested_at": now,
        })
    return _frame(rows)


def watchlist(settings: Settings, limit: int = 150) -> list[tuple[str, str | None, str | None]]:
    """[(query name, issuer_id, ticker)] — issuers (short_name) when available, else the seed universe."""
    out: list[tuple[str, str | None, str | None]] = []
    if _table_has_data("issuers", settings):
        from app.core.storage import scan_table  # noqa: PLC0415

        iss = scan_table("issuers", settings).filter(pl.col("is_active")).select("issuer_id", "name", "short_name") \
            .collect()
        for r in iss.iter_rows(named=True):
            n = r["short_name"] or r["name"]
            if n and len(n) >= 3:
                out.append((n, r["issuer_id"], None))
    if not out:
        out = [(n, None, t) for t, n in SEED_US + SEED_GLOBAL]
    return out[:limit]


def doc_health(dc: DocClient, settings: Settings) -> dict[str, Any]:
    """Coverage health from TimelineVolRaw `norm` (total monitored articles/day)."""
    payload = dc.get({"query": '"stock market"', "mode": "TimelineVolRaw", "timespan": "1m"}) or {}
    pts = []
    for s in payload.get("timeline", []) or []:
        for d in s.get("data", []):
            pts.append((d["date"][:8], d.get("norm")))
    daily: dict[str, float] = {}
    for d, n in pts:
        if n is not None:
            daily[d] = max(daily.get(d, 0.0), float(n))
    days = sorted(daily)
    today = datetime.now(UTC).strftime("%Y%m%d")
    complete = [d for d in days if d < today]
    res: dict[str, Any] = {"checked_at": datetime.now(UTC).isoformat(), "days": len(days)}
    if complete:
        last = complete[-1]
        prior = [daily[d] for d in complete[:-1]][-30:]
        med = statistics.median(prior) if prior else None
        ratio = (daily[last] / med) if med else None
        res.update({"last_day": last, "last_norm": daily[last], "median_30d": med,
                    "ratio": round(ratio, 3) if ratio is not None else None,
                    "missing_days_30d": 30 - len([d for d in complete[-31:-1]]),
                    "status": "degraded" if (ratio is not None and ratio < 0.5) else "ok"})
    else:
        res["status"] = "unknown"
    p = settings.state_dir / "gdelt_doc_health.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        hist = json.loads(p.read_text(encoding="utf-8")).get("history", [])
    except (FileNotFoundError, ValueError):
        hist = []
    hist = (hist + [res])[-120:]
    p.write_text(json.dumps({"latest": res, "history": hist}, indent=1), encoding="utf-8")
    return res


def fetch_doc(settings: Settings, names: list[tuple[str, str | None, str | None]], timespan: str = "1d",
              maxrecords: int = 75, health: bool = True,
              max_seconds: float | None = 1800.0) -> tuple[pl.DataFrame, dict[str, Any]]:
    """ArtList per watchlist name. Stops early on the circuit breaker or when `max_seconds` is spent (the DOC API is
    the long pole: 6-20 s per call plus 429 backoff), recording `stopped_early` / `names_skipped`."""
    t_start = time.perf_counter()
    dc = DocClient(settings)
    meta: dict[str, Any] = {"ok": 0, "failed": {}, "stopped_early": False}
    if health:
        try:
            meta["health"] = doc_health(dc, settings)
        except Exception as e:  # noqa: BLE001
            meta["health"] = {"status": "error", "error": str(e)[:200]}
        if meta["health"].get("status") == "degraded":
            # the DOC index is collapsed (ratio < 0.5): per-name queries return ~nothing while costing 6-20 s + 429
            # backoff each (first nightly: 33 min for 0 items). The GKG raw files remain the primary news path.
            meta["stopped_early"] = True
            meta["names_skipped"] = len(names)
            meta["skipped_reason"] = "doc_index_degraded"
            meta["calls"], meta["429s"] = dc.calls, dc.n_429
            return _frame([]), meta
    frames = []
    consecutive = 0
    for i, (name, iss, tic) in enumerate(names):
        if max_seconds is not None and time.perf_counter() - t_start > max_seconds:
            meta["stopped_early"] = True
            meta["names_skipped"] = len(names) - i
            break
        try:
            payload = dc.get({"query": f'"{name}"', "mode": "ArtList", "maxrecords": maxrecords, "timespan": timespan,
                              "sort": "DateDesc"})
            consecutive = 0
            if payload:
                frames.append(doc_artlist_rows(name, payload, iss, tic))
            meta["ok"] += 1
        except TimeoutError as e:
            meta["failed"][name] = str(e)
            consecutive += 1
            if consecutive >= 4:  # circuit breaker: the IP is being throttled hard
                meta["stopped_early"] = True
                break
    meta["calls"], meta["429s"] = dc.calls, dc.n_429
    return (pl.concat(frames) if frames else _frame([])), meta


# ------------------------------------------------------------------------------------------------ RSS


def rss_rows(source_id: str, content: bytes, countries: list[str]) -> pl.DataFrame:
    import feedparser  # noqa: PLC0415

    now = datetime.now(UTC)
    feed = feedparser.parse(content)
    rows = []
    for e in feed.entries:
        url, title = e.get("link"), (e.get("title") or "").strip()
        st = e.get("published_parsed") or e.get("updated_parsed")
        if not (url and title and st):
            continue
        ts = datetime(*st[:6], tzinfo=UTC)
        summ = e.get("summary") or ""
        rows.append({
            "item_id": item_id(url), "year_month": ts.strftime("%Y-%m"), "published_at": ts,
            "knowledge_date": ts.astimezone(NY).date() if "US" in countries else ts.date(), "source_id": source_id,
            "publisher": source_id.removeprefix("rss_").upper(), "url": url, "title": title,
            "snippet": (" ".join(summ.split()))[:500] or None, "language": "en", "issuer_ids": [],
            "link_confidence": [], "countries": countries or None, "themes": None, "gdelt_tone": None, "tickers": None,
            "entities": None, "provider_sentiment": None, "ingested_at": now,
        })
    return _frame(rows)


def fetch_rss(settings: Settings, google_news_names: list[str] | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    frames, meta = [], {"ok": [], "failed": {}}
    for sid, (url, ccs) in RSS_FEEDS.items():
        src = "sec_edgar" if "sec.gov" in url else "rss"
        try:
            r = HttpClient(src, settings=settings, max_retries=2).get(url, cache_ttl=900)
            frames.append(rss_rows(sid, r.content, ccs))
            meta["ok"].append(sid)
        except Exception as e:  # noqa: BLE001
            meta["failed"][sid] = f"{type(e).__name__}: {e}"[:200]
    from app.core.robots_policy import find_block  # noqa: PLC0415

    gblock = find_block(GOOGLE_NEWS_RSS)
    if google_news_names and gblock is not None:  # A9 2026-11-07: robots.txt disallows /rss/ -> option off
        meta["skipped"] = {"google_news": f"source_disallowed: news.google.com {gblock.rule} (checked {gblock.checked})"}
        google_news_names = None
    if google_news_names:
        from app.core.ratelimit import RateLimit  # noqa: PLC0415

        gh = HttpClient("google_news", settings=settings, max_retries=1, rate_limit=RateLimit(1 / 6, 1))
        for n in google_news_names:
            try:
                r = gh.get(GOOGLE_NEWS_RSS, {"q": f'"{n}" when:1d', "hl": "en-US", "gl": "US",
                                                                   "ceid": "US:en"}, cache_ttl=1800)
                df = rss_rows("rss_google_news", r.content, [])
                frames.append(df.with_columns(pl.lit([n]).alias("entities")))
            except Exception as e:  # noqa: BLE001
                meta["failed"][f"google:{n}"] = str(e)[:120]
    return (pl.concat(frames) if frames else _frame([])), meta


# ------------------------------------------------------------------------------------------------ orchestration


def run_news(settings: Settings | None = None, polygon_tickers: list[str] | None = None, polygon: bool = True,
             doc: bool = True, doc_limit: int = 150, rss: bool = True, google_news: bool = False,
             since: datetime | None = None, write: bool = True, doc_max_seconds: float | None = 1800.0) -> dict[str, Any]:
    s = settings or get_settings()
    since = since or datetime.now(UTC) - timedelta(days=2)
    rep: dict[str, Any] = {}
    frames: list[pl.DataFrame] = []
    t0 = time.perf_counter()
    if polygon:
        try:
            res, meta = fetch_polygon_news(s, polygon_tickers, since)
            raw_dir = s.raw_dir / "polygon" / "news" / f"dt={datetime.now(UTC).date().isoformat()}"
            raw_dir.mkdir(parents=True, exist_ok=True)
            import gzip  # noqa: PLC0415

            (raw_dir / f"news_{datetime.now(UTC).strftime('%H%M%S')}.json.gz").write_bytes(gzip.compress(orjson.dumps(res)))
            tick = sorted({t for r in res for t in (r.get("tickers") or [])})
            df = polygon_news_rows(res, _ticker_issuers(tick, s))
            frames.append(df)
            rep["polygon"] = {"items": df.height, "with_insights": df.filter(pl.col("provider_sentiment").is_not_null()).height,
                              **meta, "seconds": round(time.perf_counter() - t0, 1)}
        except MissingApiKey as e:
            rep["polygon"] = {"skipped": str(e)}
    if doc:
        t1 = time.perf_counter()
        names = watchlist(s, doc_limit)
        df, meta = fetch_doc(s, names, max_seconds=doc_max_seconds)
        frames.append(df)
        rep["gdelt_doc"] = {"names": len(names), "items": df.height, **meta, "seconds": round(time.perf_counter() - t1, 1)}
    if rss:
        t2 = time.perf_counter()
        gnames = [n for n, _, _ in watchlist(s, 10)] if google_news else None
        df, meta = fetch_rss(s, gnames)
        frames.append(df)
        rep["rss"] = {"items": df.height, **meta, "seconds": round(time.perf_counter() - t2, 1)}
    allf = pl.concat([f for f in frames if f.height]) if any(f.height for f in frames) else _frame([])
    rep["items_total"] = allf.height
    if write:
        rep["rows_written"] = write_news(allf, s)
    rep["seconds_total"] = round(time.perf_counter() - t0, 1)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    since = datetime.fromisoformat(ctx.watermark).replace(tzinfo=UTC) - timedelta(hours=6) if ctx.watermark else None
    rep = run_news(ctx.settings, p.get("polygon_tickers"), doc=bool(p.get("doc", True)),
                   doc_limit=int(p.get("doc_limit", 150)), rss=bool(p.get("rss", True)),
                   google_news=bool(p.get("google_news", False)), since=since,
                   doc_max_seconds=float(p.get("doc_max_seconds", 1800)))
    failed = sum(len((rep.get(k) or {}).get("failed", {}) or {}) for k in ("polygon", "gdelt_doc", "rss"))
    return JobResult(status="partial" if failed else "success", rows_written={"news_items": rep.get("rows_written", 0)},
                     entities_failed=failed, watermark=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S"),
                     message=json.dumps({k: {kk: vv for kk, vv in v.items() if kk in ("items", "health", "429s", "stopped_early")}
                                         for k, v in rep.items() if isinstance(v, dict)}, default=str)[:1500])


_ = date
