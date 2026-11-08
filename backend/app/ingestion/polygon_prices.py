"""Polygon / Massive free tier: US grouped-daily bars (whole US market per call) -> prices_staging.

Free-tier rules (docs/DATA_SOURCES_AMERICAS_EUROPE.md §3): 5 calls/min per ACCOUNT across api.massive.com and
api.polygon.io, 2 years of history, end-of-day data. Every Polygon call in this code base goes through
`PolygonClient`, which uses the process- AND machine-wide shared token bucket `get_limiter("polygon")`
(app.core.ratelimit SharedTokenBucket: 4/min, state persisted in data/state/ratelimit/polygon.json so the
worker, CLI and successive runs share one budget) plus its own 429 handling (drain the shared bucket for 60 s x
attempt, retry up to 4 times). The key is sent as `Authorization: Bearer` (never in URLs or logs).

Job `prices_us_polygon`: one grouped-daily call per missing XNYS session (watermark = last loaded session,
default look-back 10 sessions, `params.dates` / `params.days` override). Bars are stored RAW (`adjusted=false`);
splits/dividends are applied by app.normalize.prices. Grouped-daily includes warrants/units/rights/preferreds;
they are kept in staging (cheap) and dropped by symbol resolution against the universe.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.core.ratelimit import get_limiter
from app.core.storage import scan_table, write_table
from app.ingestion.base import FetchResult, MissingApiKey, SourceAdapter, register_adapter
from app.normalize.prices import _table_has_data, last_completed_session, trading_sessions

log = get_logger(__name__)

HOSTS = ("https://api.massive.com", "https://api.polygon.io")


class PolygonAuthError(RuntimeError):
    pass


class PolygonClient:
    """Rate-limited Polygon/Massive REST client (Bearer auth, shared 5/min budget, 429 backoff, host fallback)."""

    def __init__(self, settings: Settings | None = None, http: HttpClient | None = None,
                 penalty_s: float = 60.0, max_attempts: int = 4):
        self.settings = settings or get_settings()
        key = self.settings.secret("polygon_apikey")
        if not key:
            raise MissingApiKey("polygon: missing POLYGON_APIKEY in .env")
        self._headers = {"Authorization": f"Bearer {key}"}
        # max_retries=0: the HttpClient returns 429/5xx to us so the shared bucket can be drained explicitly
        self.http = http or HttpClient("polygon", max_retries=0, settings=self.settings)
        self.limiter = get_limiter("polygon")
        self.penalty_s = penalty_s
        self.max_attempts = max_attempts
        self.n_calls = 0
        self.n_429 = 0
        self._host_idx = 0

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            url = path_or_url if path_or_url.startswith("http") else HOSTS[self._host_idx] + path_or_url
            try:
                self.n_calls += 1
                r = self.http.get(url, params, headers=self._headers, cache_ttl=0, raise_for_status=False)
            except HttpError as e:  # transport failure on this host -> try the other one
                last = e
                self._host_idx = (self._host_idx + 1) % len(HOSTS)
                log.warning("polygon transport error; switching host (attempt %d)", attempt + 1)
                continue
            if r.status_code == 429:
                self.n_429 += 1
                wait = self.penalty_s * (attempt + 1)
                self.limiter.penalize(wait)  # every process now waits; the shared bucket refills after `wait`
                log.warning("polygon 429 (attempt %d); shared bucket drained for %.0fs", attempt + 1, wait)
                last = HttpError(429, url)
                continue
            if r.status_code in (401, 403):
                body = r.json() if r.content[:1] == b"{" else {}
                raise PolygonAuthError(f"polygon {r.status_code}: {body.get('status')} {body.get('message', '')[:120]}")
            if r.status_code >= 500:
                last = HttpError(r.status_code, url)
                time.sleep(min(30.0, 5.0 * (attempt + 1)))
                continue
            if r.status_code >= 400:
                raise HttpError(r.status_code, url, r.text[:300])
            return r.json()
        raise last or RuntimeError("polygon: retries exhausted")

    def paginate(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
        """Follow `next_url` (each page = 1 call against the 5/min budget)."""
        out: list[dict[str, Any]] = []
        page = self.get(path, params)
        out.extend(page.get("results") or [])
        n = 1
        while page.get("next_url") and n < max_pages:
            page = self.get(page["next_url"])
            out.extend(page.get("results") or [])
            n += 1
        return out

    def grouped_daily(self, day: date, adjusted: bool = False, include_otc: bool = False) -> dict[str, Any]:
        return self.get(f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
                        {"adjusted": str(adjusted).lower(), "include_otc": str(include_otc).lower()})


def grouped_to_rows(payload: dict[str, Any], day: date) -> pl.DataFrame:
    """Grouped-daily JSON -> prices_staging rows (raw basis, USD)."""
    res = payload.get("results") or []
    if not res:
        return pl.DataFrame()
    df = pl.DataFrame(res, infer_schema_length=None)
    cols = {"T": "symbol", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap", "n": "trades"}
    for k in cols:
        if k not in df.columns:
            df = df.with_columns(pl.lit(None).alias(k))
    df = df.select([pl.col(k).alias(v) for k, v in cols.items()])
    return df.with_columns(
        pl.lit("polygon").alias("source_id"), pl.lit(day).alias("date"), pl.lit(day.year, pl.Int16).alias("year"),
        pl.col("volume").cast(pl.Float64), pl.col("trades").cast(pl.Int64),
        *[pl.col(c).cast(pl.Float64) for c in ("open", "high", "low", "close", "vwap")],
        pl.lit(None, pl.Float64).alias("adj_close"), pl.lit("USD").alias("currency"), pl.lit("raw").alias("price_basis"),
        pl.lit(False).alias("is_benchmark"), pl.lit(datetime.now(UTC)).alias("ingested_at"),
    ).filter(pl.col("symbol").is_not_null() & pl.col("close").is_not_null()).unique(subset=["symbol"], keep="first")


def loaded_polygon_dates(settings: Settings | None = None) -> set[date]:
    s = settings or get_settings()
    if not _table_has_data("prices_staging", s):
        return set()
    lf = scan_table("prices_staging", s).filter(pl.col("source_id") == "polygon").select("date").unique()
    return set(lf.collect().get_column("date").to_list())


@register_adapter
class PolygonPricesAdapter(SourceAdapter):
    source_id = "polygon"
    regions = ("US",)
    produces = ("prices_staging",)
    requires_keys = ("polygon_apikey",)
    cache_ttl = 0

    def __init__(self, settings: Settings | None = None, http: HttpClient | None = None,
                 client: PolygonClient | None = None):
        super().__init__(settings, http)
        self._client = client

    @property
    def client(self) -> PolygonClient:
        if self._client is None:
            self._client = PolygonClient(self.settings, self.http if self.http.max_retries == 0 else None)
        return self._client

    def discover(self) -> pl.DataFrame:
        """Symbols seen in the latest grouped-daily file (no extra API call)."""
        if not _table_has_data("prices_staging", self.settings):
            return self.discover_frame([], self.source_id)
        lf = scan_table("prices_staging", self.settings).filter(pl.col("source_id") == "polygon")
        last = lf.select(pl.col("date").max()).collect().item()
        syms = lf.filter(pl.col("date") == last).select("symbol").collect().get_column("symbol").to_list()
        return self.discover_frame([{"source_entity_id": t, "name": t, "ticker": t, "country": "US"} for t in syms],
                                   self.source_id)

    def target_dates(self, since: date | None = None, days: int = 10, today: date | None = None,
                     skip_loaded: bool = True) -> list[date]:
        """Missing XNYS sessions in (since, last completed session]; at most `days` most recent ones when since=None.
        Free tier: nothing older than ~2 years."""
        end = last_completed_session("XNYS", today)
        floor = end - timedelta(days=730)
        start = max(since + timedelta(days=1), floor) if since else end - timedelta(days=int(days * 1.6) + 7)
        sess = sorted(trading_sessions("XNYS", start, end) or [])
        if since is None:
            sess = sess[-days:]
        if skip_loaded:
            have = loaded_polygon_dates(self.settings)
            sess = [d for d in sess if d not in have]
        return sess

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None,
              dates: Sequence[date] | None = None, days: int = 10) -> FetchResult:
        fr = FetchResult(self.source_id, datetime.now(UTC))
        if isinstance(since, str):
            since = date.fromisoformat(since[:10])
        if isinstance(since, datetime):
            since = since.date()
        todo = list(dates) if dates else self.target_dates(since, days)
        fr.meta["dates_requested"] = [d.isoformat() for d in todo]
        for d in todo:
            t0 = time.perf_counter()
            try:
                payload = self.client.grouped_daily(d)
            except PolygonAuthError as e:
                fr.entities_failed[d.isoformat()] = str(e)
                break  # auth problems will not fix themselves
            except Exception as e:  # noqa: BLE001
                fr.entities_failed[d.isoformat()] = f"{type(e).__name__}: {e}"[:300]
                continue
            n = int(payload.get("resultsCount") or len(payload.get("results") or []))
            if n == 0:
                fr.meta.setdefault("empty_dates", []).append(d.isoformat())
                continue
            fr.raw_paths.append(self.save_raw("grouped_daily", f"grouped_{d.isoformat()}", payload, day=d))
            fr.entities_ok.append(d.isoformat())
            log.info("polygon grouped %s: %d tickers in %.1fs", d, n, time.perf_counter() - t0)
        if fr.entities_ok:
            fr.watermark = max(fr.entities_ok)
        fr.meta["polygon_calls"] = self.client.n_calls
        fr.meta["polygon_429"] = self.client.n_429
        return fr

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        frames = []
        for p in fetched.raw_paths:
            d = date.fromisoformat(p.name.split("_", 1)[1][:10])
            frames.append(grouped_to_rows(self.load_raw_json(p), d))
        frames = [f for f in frames if f.height]
        if not frames:
            return {}
        return {"prices_staging": pl.concat(frames, how="vertical_relaxed")}


def run(ctx):  # noqa: ANN001 - JobContext
    """Job prices_us_polygon."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    ad = PolygonPricesAdapter(ctx.settings)
    try:
        ad.check_keys()
    except MissingApiKey as e:
        return JobResult(status="skipped", message=str(e))
    dates = [date.fromisoformat(x) for x in p["dates"]] if p.get("dates") else None
    since = date.fromisoformat(ctx.watermark[:10]) if (ctx.watermark and not dates and not p.get("days")) else None
    fr = ad.fetch(since=since, dates=dates, days=int(p.get("days", 10)))
    tables = ad.normalize(fr)
    rows = {k: write_table(v, k, settings=ctx.settings) for k, v in tables.items()}
    return JobResult(status=fr.status if (fr.entities_ok or fr.entities_failed) else "success", rows_written=rows,
                     entities_ok=len(fr.entities_ok), entities_failed=len(fr.entities_failed),
                     watermark=fr.watermark or ctx.watermark, http_requests=ad.client.n_calls,
                     message=f"dates={len(fr.entities_ok)} empty={len(fr.meta.get('empty_dates', []))} "
                             f"429s={ad.client.n_429}")
