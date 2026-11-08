"""Quote sources for the delayed quotes service.

Yahoo (default, every region): the batched v7 quote endpoint
  https://query1.finance.yahoo.com/v7/finance/quote?symbols=A,B,...&formatted=false
called through yfinance's shared session (cookie + crumb handling, yfinance.data.YfData), <= `batch` symbols per
request, one token of the machine-wide shared `yfinance` bucket per request (the nightly price jobs use the same
bucket). Fields: regularMarketPrice / Change / ChangePercent / Time / PreviousClose, currency, marketState,
exchangeDataDelayedBy (minutes), exchangeTimezoneName, quoteType, dayHigh / dayLow / volume.

Polygon (US equities, optional): GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=... - included in paid
stock plans only. The service probes it at most once a day; a 401 / 403 (NOT_AUTHORIZED / NOT_ENTITLED) disables it
for the day with the reason "plan does not include snapshots". Calls take a token of the shared `polygon` bucket
(4/min for the whole account, shared with the nightly jobs and the live news stream), at most one call per poll.

Every quote is DELAYED (personal use; Yahoo 0-20 min depending on the exchange, Polygon 15 min on the Starter
plan) and labelled so; `delay_min` carries the source's stated delay when it gives one.
"""

from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logsetup import get_logger

log = get_logger(__name__)

YAHOO_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
POLYGON_SNAPSHOT = "https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers"


class SourceError(RuntimeError):
    """A source call failed; `retry_after_s` hints the back-off (429), `fatal` disables the source for the day."""

    def __init__(self, msg: str, *, status: int | None = None, retry_after_s: float | None = None, fatal: bool = False):
        super().__init__(msg)
        self.status = status
        self.retry_after_s = retry_after_s
        self.fatal = fatal


@dataclass
class Quote:
    symbol: str                      # source symbol (Yahoo / Polygon ticker)
    price: float
    source: str                      # yahoo | polygon
    change: float | None = None
    change_pct: float | None = None  # fraction (0.012 = +1.2%)
    prev_close: float | None = None
    currency: str | None = None
    market_state: str | None = None  # REGULAR | PRE | POST | CLOSED | PREPRE | POSTPOST (Yahoo); polygon: None
    quote_ts: float | None = None    # epoch seconds of the last trade / print
    delay_min: int | None = None
    exchange: str | None = None
    exchange_tz: str | None = None
    quote_type: str | None = None    # EQUITY | INDEX | CURRENCY | FUTURE | ETF ...
    day_high: float | None = None
    day_low: float | None = None
    volume: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def parse_yahoo(doc: dict[str, Any]) -> tuple[list[Quote], list[str]]:
    """v7 quote response -> (quotes, symbols without a usable price)."""
    res = ((doc or {}).get("quoteResponse") or {}).get("result") or []
    out: list[Quote] = []
    bad: list[str] = []
    for r in res:
        sym = r.get("symbol")
        px = _f(r.get("regularMarketPrice"))
        if not sym:
            continue
        if px is None or px <= 0:
            bad.append(sym)
            continue
        pct = _f(r.get("regularMarketChangePercent"))
        prev = _f(r.get("regularMarketPreviousClose"))
        chg = _f(r.get("regularMarketChange"))
        if chg is None and prev:
            chg = px - prev
        if pct is None and prev:
            pct = 100.0 * (px / prev - 1.0)
        ts = _f(r.get("regularMarketTime"))
        delay = r.get("exchangeDataDelayedBy")
        out.append(Quote(
            symbol=sym, price=px, source="yahoo", change=chg, change_pct=pct / 100.0 if pct is not None else None,
            prev_close=prev, currency=r.get("currency"), market_state=r.get("marketState"), quote_ts=ts,
            delay_min=int(delay) if isinstance(delay, (int, float)) else None,
            exchange=r.get("fullExchangeName") or r.get("exchange"), exchange_tz=r.get("exchangeTimezoneName"),
            quote_type=r.get("quoteType"), day_high=_f(r.get("regularMarketDayHigh")),
            day_low=_f(r.get("regularMarketDayLow")), volume=_f(r.get("regularMarketVolume")),
            extra={"source_name": r.get("quoteSourceName")}))
    return out, bad


def parse_polygon_snapshot(doc: dict[str, Any]) -> list[Quote]:
    """Polygon /v2/snapshot/.../tickers response -> quotes (last trade, else the minute bar / day close)."""
    out: list[Quote] = []
    for t in (doc or {}).get("tickers") or []:
        sym = t.get("ticker")
        last = (t.get("lastTrade") or {})
        minute = t.get("min") or {}
        day = t.get("day") or {}
        prev = (t.get("prevDay") or {}).get("c")
        px = _f(last.get("p")) or _f(minute.get("c")) or _f(day.get("c"))
        if not sym or px is None or px <= 0:
            continue
        ts_ns = last.get("t") or t.get("updated")
        ts = float(ts_ns) / 1e9 if ts_ns and float(ts_ns) > 1e12 else (float(ts_ns) / 1e3 if ts_ns else None)
        prev_f = _f(prev)
        chg = _f(t.get("todaysChange"))
        pct = _f(t.get("todaysChangePerc"))
        out.append(Quote(symbol=sym, price=px, source="polygon", change=chg if chg is not None else
                         (px - prev_f if prev_f else None),
                         change_pct=pct / 100.0 if pct is not None else ((px / prev_f - 1) if prev_f else None),
                         prev_close=prev_f, currency="USD", quote_ts=ts, delay_min=15, quote_type="EQUITY",
                         day_high=_f(day.get("h")), day_low=_f(day.get("l")), volume=_f(day.get("v"))))
    return out


# ------------------------------------------------------------------------------------------------ clients
class YahooQuotes:
    name = "yahoo"

    def __init__(self, batch: int = 50, timeout: float = 20.0, limiter: Any = None, getter: Any = None):
        self.batch = batch
        self.timeout = timeout
        self._limiter = limiter
        self._getter = getter       # tests: (symbols) -> response dict
        self._data: Any = None

    def _limiter_(self) -> Any:
        if self._limiter is None:
            from app.core.ratelimit import get_limiter  # noqa: PLC0415

            self._limiter = get_limiter("yfinance")
        return self._limiter

    def _get(self, symbols: list[str]) -> dict[str, Any]:
        if self._getter is not None:
            return self._getter(symbols)
        if self._data is None:
            from yfinance.data import YfData  # noqa: PLC0415 - heavy import, only in the quotes child

            self._data = YfData()
        try:
            resp = self._data.get(YAHOO_URL, params={"symbols": ",".join(symbols), "formatted": "false"},
                                  timeout=self.timeout)
        except Exception as e:  # noqa: BLE001 - network / curl errors
            if type(e).__name__ == "YFRateLimitError":
                raise SourceError("yahoo: rate limited (HTTP 429)", status=429, retry_after_s=300) from e
            raise SourceError(f"yahoo: {type(e).__name__}: {e}"[:300]) from e
        status = getattr(resp, "status_code", 200)
        if status == 429:
            raise SourceError("yahoo: rate limited (HTTP 429)", status=429, retry_after_s=300)
        if status >= 400:
            raise SourceError(f"yahoo: HTTP {status}", status=status, retry_after_s=120 if status >= 500 else None)
        try:
            return resp.json()
        except ValueError as e:
            raise SourceError("yahoo: invalid JSON") from e

    def fetch(self, symbols: list[str]) -> tuple[list[Quote], list[str], int]:
        """-> (quotes, symbols without a price, requests made)."""
        quotes: list[Quote] = []
        bad: list[str] = []
        n = 0
        for i in range(0, len(symbols), self.batch):
            chunk = symbols[i:i + self.batch]
            self._limiter_().acquire()
            n += 1
            q, b = parse_yahoo(self._get(chunk))
            quotes += q
            got = {x.symbol for x in q}
            bad += b + [s for s in chunk if s not in got and s not in b]
        return quotes, bad, n


class PolygonSnapshot:
    name = "polygon"

    def __init__(self, api_key: str | None, limiter: Any = None, getter: Any = None, timeout: float = 20.0):
        self.api_key = api_key
        self._limiter = limiter
        self._getter = getter
        self.timeout = timeout
        self.disabled_reason: str | None = None if api_key else "POLYGON_API_KEY missing"
        self.disabled_until = 0.0

    def available(self, now: float | None = None) -> tuple[bool, str | None]:
        now = time.time() if now is None else now
        if not self.api_key:
            return False, "POLYGON_API_KEY missing"
        if self.disabled_reason and now < self.disabled_until:
            return False, self.disabled_reason
        return True, None

    def _get(self, tickers: list[str]) -> dict[str, Any]:
        if self._getter is not None:
            return self._getter(tickers)
        import httpx  # noqa: PLC0415

        if self._limiter is None:
            from app.core.ratelimit import get_limiter  # noqa: PLC0415

            self._limiter = get_limiter("polygon")
        self._limiter.acquire()
        try:
            r = httpx.get(POLYGON_SNAPSHOT, params={"tickers": ",".join(tickers), "apiKey": self.api_key},
                          timeout=self.timeout)
        except Exception as e:  # noqa: BLE001
            raise SourceError(f"polygon: {type(e).__name__}") from e   # never echo the URL (it carries the key)
        if r.status_code in (401, 403):
            raise SourceError("polygon: plan does not include snapshots (NOT_AUTHORIZED)", status=r.status_code,
                              fatal=True)
        if r.status_code == 429:
            raise SourceError("polygon: rate limited (HTTP 429)", status=429, retry_after_s=300)
        if r.status_code >= 400:
            raise SourceError(f"polygon: HTTP {r.status_code}", status=r.status_code)
        return r.json()

    def fetch(self, tickers: list[str], now: float | None = None) -> list[Quote]:
        ok, _why = self.available(now)
        if not ok or not tickers:
            return []
        try:
            doc = self._get(tickers)
        except SourceError as e:
            if e.fatal:
                self.disabled_reason = str(e)
                self.disabled_until = (time.time() if now is None else now) + 24 * 3600
            raise
        if (doc or {}).get("status") in ("NOT_AUTHORIZED", "NOT_ENTITLED"):
            self.disabled_reason = "polygon: plan does not include snapshots (NOT_AUTHORIZED)"
            self.disabled_until = (time.time() if now is None else now) + 24 * 3600
            raise SourceError(self.disabled_reason, fatal=True)
        return parse_polygon_snapshot(doc)


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
