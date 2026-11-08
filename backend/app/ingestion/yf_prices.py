"""yfinance daily bars (long-history backfill + all non-US listings + benchmarks) -> prices_staging and
corporate_actions_staging.

* Batched `yf.download(..., auto_adjust=False, actions=True, group_by="ticker")` (research: 200-500 tickers per call
  is safe; we default to 100) with one token from the machine-wide shared "yfinance" bucket per batch and a polite
  pause between batches. YFRateLimitError -> 60 s x attempt backoff (3 attempts), then the batch is recorded failed.
* Yahoo's `Close`/`Volume` are SPLIT-ADJUSTED; `Adj Close` adds dividends. We convert to RAW prints with the splits
  from the same download (exact, because each download window ends today), so staging is `price_basis=raw`.
  Dividends are converted the same way. `Adj Close` is kept for validation only.
* Quote currency: suffix table (app.normalize.prices.SUFFIX_MARKET); lines whose unit is ambiguous (.L, .JO, .TA,
  .IL and unknown suffixes) are checked once via `Ticker.fast_info.currency` and cached in data/state/yf_currency.json.
* Benchmarks with alternates (Portugal PSI: PSI20.LS / ^PSI20 / PSI.LS) are retried with the next symbol.

Job `prices_global_yf`: nightly = incremental per symbol (last stored bar - 3 d; full history only for symbols with no
stored bars), chunked + checkpointed per as_of (resumable), with a time budget (`max_minutes`) -> `run_incremental`.
Universe: reference listings (seed list before ref-identity has published), minus CN/TW/BR names covered by official
bulk sources (curated seeds kept). Manual backfill: `params.period="max"`.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.ratelimit import get_limiter
from app.core.storage import write_table
from app.ingestion.base import FetchResult, SourceAdapter, register_adapter
from app.normalize.prices import (
    BENCHMARK_BY_TICKER,
    BENCHMARKS,
    SUFFIX_MARKET,
    load_universe,
    market_for_yf,
    unadjust_split_basis,
    yf_suffix,
)

log = get_logger(__name__)

AMBIGUOUS_SUFFIXES = {".L", ".JO", ".TA", ".IL"}
#: suffixes priced from official whole-market files (prices_cn_baostock / prices_tw / prices_br_b3)
OFFICIAL_BULK_SUFFIXES = {".SS", ".SZ", ".TW", ".TWO", ".SA"}


def _curated_yf(settings: Settings | None) -> set[str]:
    """yf tickers of the curated seeds (mvp_global.csv): still fetched from Yahoo for cross-source reconciliation."""
    try:
        from app.normalize.identity_universe import load_global_seeds  # noqa: PLC0415

        return set(load_global_seeds(settings).get_column("yf_ticker").drop_nulls().to_list())
    except Exception:  # noqa: BLE001
        return set()
_FIELDS = {"Open": "open", "High": "high", "Low": "low", "Close": "close", "Adj Close": "adj_close", "Volume": "volume",
           "Dividends": "dividend", "Stock Splits": "split"}


def wide_to_long(pdf: Any, tickers: Sequence[str]) -> pl.DataFrame:
    """yf.download(group_by='ticker') pandas frame -> long polars [symbol, date, open..volume, dividend, split]."""
    import pandas as pd  # noqa: PLC0415

    if pdf is None or len(pdf) == 0:
        return pl.DataFrame()
    frames = []
    multi = isinstance(pdf.columns, pd.MultiIndex)
    lvl0 = set(pdf.columns.get_level_values(0)) if multi else set()
    for t in tickers:
        if multi:
            if t not in lvl0:
                continue
            sub = pdf[t]
        else:
            sub = pdf
        sub = sub.rename(columns=_FIELDS)
        keep = [c for c in _FIELDS.values() if c in sub.columns]
        sub = sub[keep].copy()
        if "close" not in sub.columns:
            continue
        sub = sub[sub["close"].notna()]
        if sub.empty:
            continue
        sub.index = pd.to_datetime(sub.index).tz_localize(None) if getattr(sub.index, "tz", None) else pd.to_datetime(sub.index)
        sub = sub.reset_index().rename(columns={sub.index.name or "index": "date", "Date": "date"})
        df = pl.from_pandas(sub)
        df = df.with_columns(pl.col("date").cast(pl.Date), pl.lit(t).alias("symbol"))
        frames.append(df)
    if not frames:
        return pl.DataFrame()
    out = pl.concat(frames, how="diagonal_relaxed")
    for c in ("open", "high", "low", "close", "adj_close", "volume", "dividend", "split"):
        if c not in out.columns:
            out = out.with_columns(pl.lit(None, pl.Float64).alias(c))
        else:
            out = out.with_columns(pl.col(c).cast(pl.Float64))
    return out


def long_to_staging(long: pl.DataFrame, currencies: dict[str, str], benchmark_symbols: set[str],
                    alias: dict[str, str] | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Long yfinance frame -> (prices_staging rows on RAW basis, corporate_actions_staging rows on RAW basis).
    `alias` renames fetched symbols to the canonical benchmark symbol (e.g. ^PSI20 -> PSI20.LS)."""
    if long.is_empty():
        return pl.DataFrame(), pl.DataFrame()
    if alias:
        long = long.with_columns(pl.col("symbol").replace(alias))
    now = datetime.now(UTC)
    splits = long.filter(pl.col("split").fill_null(0) > 0).select(
        "symbol", pl.col("date").alias("ex_date"), pl.col("split").alias("ratio"))
    raw = unadjust_split_basis(long, splits, key="symbol")
    ccy = pl.col("symbol").replace_strict(currencies, default=None, return_dtype=pl.Utf8)
    prices = raw.select(
        pl.lit("yfinance").alias("source_id"), pl.col("date").dt.year().cast(pl.Int16).alias("year"), "symbol", "date",
        "open", "high", "low", "close", "adj_close", "volume", pl.lit(None, pl.Float64).alias("vwap"),
        pl.lit(None, pl.Int64).alias("trades"), ccy.alias("currency"), pl.lit("raw").alias("price_basis"),
        pl.col("symbol").is_in(list(benchmark_symbols)).alias("is_benchmark"), pl.lit(now).alias("ingested_at"),
    ).filter(pl.col("close").is_not_null() & (pl.col("close") > 0)).unique(subset=["symbol", "date"], keep="last")
    # dividends -> raw basis with the same split factors (S(d) at the ex date)
    divs = raw.filter(pl.col("dividend").fill_null(0) > 0).select(
        "symbol", "date", (pl.col("dividend") * pl.col("__S")).alias("amount"))
    split_rows = splits.select(
        pl.lit("yfinance").alias("source_id"), "symbol", "ex_date", pl.lit("split").alias("action_type"),
        "ratio", pl.lit(None, pl.Float64).alias("amount"), pl.lit(None, pl.Utf8).alias("amount_basis"),
        pl.lit(None, pl.Utf8).alias("currency"))
    div_rows = divs.select(
        pl.lit("yfinance").alias("source_id"), "symbol", pl.col("date").alias("ex_date"),
        pl.lit("cash_dividend").alias("action_type"), pl.lit(None, pl.Float64).alias("ratio"), "amount",
        pl.lit("raw").alias("amount_basis"), ccy.alias("currency"))
    actions = pl.concat([split_rows, div_rows], how="vertical_relaxed").with_columns(
        pl.lit(now).alias("ingested_at")).unique(subset=["symbol", "ex_date", "action_type"], keep="last")
    return prices, actions


class CurrencyCache:
    """yfinance quote-currency cache (data/state/yf_currency.json)."""

    def __init__(self, settings: Settings):
        self.path = settings.state_dir / "yf_currency.json"
        try:
            self.data: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def resolve(self, symbols: Sequence[str], lookup: bool = True) -> dict[str, str]:
        out: dict[str, str] = {}
        need = []
        for s in symbols:
            b = BENCHMARK_BY_TICKER.get(s)
            if b:
                out[s] = b.currency
            elif s in self.data:
                out[s] = self.data[s]
            elif yf_suffix(s) in AMBIGUOUS_SUFFIXES or ("." in s and yf_suffix(s) == ""):
                need.append(s)
            else:
                out[s] = market_for_yf(s).currency
        if need and lookup:
            import yfinance as yf  # noqa: PLC0415

            lim = get_limiter("yfinance")
            for s in need:
                lim.acquire()
                try:
                    c = yf.Ticker(s).fast_info["currency"]
                    if c:
                        self.data[s] = out[s] = str(c)
                except Exception as e:  # noqa: BLE001
                    log.debug("currency lookup %s failed: %s", s, e)
                if s not in out:
                    out[s] = market_for_yf(s).currency
            self.save()
        for s in need:
            out.setdefault(s, market_for_yf(s).currency)
        return out


@register_adapter
class YFinancePricesAdapter(SourceAdapter):
    source_id = "yfinance"
    regions = ("US", "CA", "LATAM", "UK", "EU", "JP", "KR", "CN", "HK", "APAC", "OTHER")
    produces = ("prices_staging", "corporate_actions_staging")

    def __init__(self, settings: Settings | None = None, batch_size: int = 100, pause_s: float = 2.0, **kw: Any):
        super().__init__(settings, **kw)
        self.batch_size = batch_size
        self.pause_s = pause_s
        self.n_downloads = 0

    def discover(self) -> pl.DataFrame:
        uni = load_universe(self.settings)
        rows = [{"source_entity_id": r["yf_ticker"], "name": r["name"] or r["yf_ticker"], "yf_ticker": r["yf_ticker"],
                 "country": r["country"]} for r in uni.iter_rows(named=True) if r["yf_ticker"]]
        return self.discover_frame(rows, self.source_id)

    def universe_symbols(self) -> list[str]:
        uni = load_universe(self.settings)
        # markets with an official whole-market daily source (BaoStock / TWSE / TPEx / B3) are only cross-checked on
        # Yahoo for the curated seeds; the price-coverage names there never hit the (throttled) Yahoo endpoint
        curated = _curated_yf(self.settings)
        return [s for s in uni.get_column("yf_ticker").drop_nulls().unique(maintain_order=True).to_list()
                if not (s in BENCHMARK_BY_TICKER and s != BENCHMARK_BY_TICKER[s].yf_ticker)  # alternates on demand
                and not (yf_suffix(s) in OFFICIAL_BULK_SUFFIXES and s not in curated and s not in BENCHMARK_BY_TICKER)]

    def _download(self, batch: list[str], period: str | None, start: date | None) -> Any:
        import yfinance as yf  # noqa: PLC0415

        lim = get_limiter("yfinance")
        for attempt in range(3):
            lim.acquire()
            self.n_downloads += 1
            try:
                kw: dict[str, Any] = dict(interval="1d", auto_adjust=False, actions=True, group_by="ticker",
                                          threads=True, progress=False, repair=False, timeout=30)
                if start is not None:
                    kw["start"] = start.isoformat()
                else:
                    kw["period"] = period or "max"
                return yf.download(batch, **kw)
            except Exception as e:  # noqa: BLE001 - YFRateLimitError and friends
                name = type(e).__name__
                wait = 60.0 * (attempt + 1) if "RateLimit" in name else 5.0 * (attempt + 1)
                log.warning("yfinance batch of %d failed (%s); retry in %.0fs", len(batch), name, wait)
                lim.penalize(wait)
                time.sleep(wait)
        return None

    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None,
              period: str | None = None, start: date | None = None) -> FetchResult:
        """`start` (exact first date) overrides `since` (watermark; window starts 7 days before it)."""
        fr = FetchResult(self.source_id, datetime.now(UTC))
        if isinstance(since, str):
            since = date.fromisoformat(since[:10])
        if isinstance(since, datetime):
            since = since.date()
        if start is None:
            start = (since - timedelta(days=7)) if since else None
        symbols = list(dict.fromkeys(entities or self.universe_symbols()))
        cc = CurrencyCache(self.settings)
        currencies = cc.resolve(symbols)
        fr.meta.update({"symbols": len(symbols), "period": period if start is None else f"start={start}"})
        tag = datetime.now(UTC).strftime("%H%M%S")
        alias: dict[str, str] = {}
        for i in range(0, len(symbols), self.batch_size):
            batch = symbols[i:i + self.batch_size]
            t0 = time.perf_counter()
            pdf = self._download(batch, period or ("max" if start is None else None), start)
            long = wide_to_long(pdf, batch) if pdf is not None else pl.DataFrame()
            got = set(long.get_column("symbol").unique().to_list()) if long.height else set()
            # benchmark alternates (e.g. PSI20.LS -> ^PSI20)
            for s in [x for x in batch if x not in got and x in BENCHMARK_BY_TICKER]:
                for alt in BENCHMARK_BY_TICKER[s].alternates:
                    pdf2 = self._download([alt], period or ("max" if start is None else None), start)
                    l2 = wide_to_long(pdf2, [alt]) if pdf2 is not None else pl.DataFrame()
                    if l2.height:
                        long = pl.concat([long, l2], how="diagonal_relaxed") if long.height else l2
                        alias[alt] = s
                        got.add(s)
                        fr.meta.setdefault("benchmark_alternates", {})[s] = alt
                        break
            for s in batch:
                (fr.entities_ok.append(s) if s in got else fr.entities_failed.__setitem__(s, "no data"))
            if long.height:
                buf = long.write_ipc(None, compression="zstd").getvalue()
                fr.raw_paths.append(self.save_raw("daily_bars", f"batch_{tag}_{i // self.batch_size:04d}", buf,
                                                  compress=False))
            log.info("yfinance batch %d: %d/%d symbols, %d rows in %.1fs", i // self.batch_size, len(got & set(batch)),
                     len(batch), long.height, time.perf_counter() - t0)
            if i + self.batch_size < len(symbols):
                time.sleep(self.pause_s + random.random())
        fr.meta["currencies"] = currencies
        fr.meta["alias"] = alias
        if fr.entities_ok:
            fr.watermark = datetime.now(UTC).date().isoformat()
        return fr

    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        frames = [pl.read_ipc(p) for p in fetched.raw_paths]
        if not frames:
            return {}
        long = pl.concat(frames, how="diagonal_relaxed")
        bms = {b.yf_ticker for b in BENCHMARKS}
        prices, actions = long_to_staging(long, fetched.meta.get("currencies", {}), bms, fetched.meta.get("alias"))
        # O2.2a / F05: Yahoo serves the in-progress session as a daily bar; never stage it as a final close
        from app.normalize.price_integrity import drop_unfinished_bars  # noqa: PLC0415

        prices, n_live = drop_unfinished_bars(prices)
        if n_live:
            fetched.meta["unfinished_bars_dropped"] = n_live
            log.info("yfinance: dropped %d bars of sessions still open at fetch time", n_live)
        out = {"prices_staging": prices}
        if actions.height:
            out["corporate_actions_staging"] = actions
        return out


def stored_last_dates(settings: Settings | None = None) -> dict[str, date]:
    """Last stored yfinance bar date per symbol in prices_staging ({} if the table is missing/unreadable)."""
    try:
        from app.core.storage import scan_table  # noqa: PLC0415

        df = (scan_table("prices_staging", settings).filter(pl.col("source_id") == "yfinance")
              .group_by("symbol").agg(pl.col("date").max().alias("last")).collect())
        return dict(zip(df.get_column("symbol").to_list(), df.get_column("last").to_list(), strict=True))
    except Exception as e:  # noqa: BLE001
        log.warning("yfinance: could not read stored history (%s); treating all symbols as new", e)
        return {}


def plan_chunks(symbols: Sequence[str], last: dict[str, date], today: date, overlap_days: int = 3,
                chunk_size: int = 2000, max_gap_days: int = 7) -> list[tuple[date | None, list[str]]]:
    """Group symbols into (start, symbols) download chunks. start = last stored date - overlap (None = full history
    for symbols without any stored bar). Symbols are sorted by start; a chunk is cut at `chunk_size` or when the start
    drifts more than `max_gap_days` from the chunk's first (earliest) start, so a stale symbol never widens the
    window of thousands of fresh ones."""
    with_hist = sorted((max(last[s] - timedelta(days=overlap_days), date(1970, 1, 1)), s) for s in symbols if s in last)
    out: list[tuple[date | None, list[str]]] = []
    cur: list[str] = []
    cur_start: date | None = None
    for st, s in with_hist:
        st = min(st, today)
        if cur and (len(cur) >= chunk_size or (st - cur_start).days > max_gap_days):  # type: ignore[operator]
            out.append((cur_start, cur))
            cur = []
        if not cur:
            cur_start = st
        cur.append(s)
    if cur:
        out.append((cur_start, cur))
    new = [s for s in symbols if s not in last]
    out += [(None, new[i:i + chunk_size // 4]) for i in range(0, len(new), max(1, chunk_size // 4))]
    return out


class Checkpoint:
    """Per-run progress (data/state/yf_prices/nightly_<as_of>.json): symbols attempted are skipped on a resume/retry."""

    def __init__(self, settings: Settings, as_of: date | str):
        self.path = settings.state_dir / "yf_prices" / f"nightly_{str(as_of)[:10]}.json"
        try:
            st = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            st = {}
        self.ok: set[str] = set(st.get("ok", []))
        self.failed: dict[str, str] = dict(st.get("failed", {}))

    @property
    def done(self) -> set[str]:
        return self.ok | set(self.failed)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ok": sorted(self.ok), "failed": self.failed,
                                   "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}), encoding="utf-8")
        tmp.replace(self.path)


def run(ctx, period: str | None = None):  # noqa: ANN001 - JobContext
    """Job prices_global_yf. Nightly (no `period`): incremental + checkpointed, see run_incremental.
    params: period ('max', '5y', ... = explicit window for all symbols), symbols, batch_size, max_minutes."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    period = p.get("period", period)
    if not period:
        return run_incremental(ctx)
    ad = YFinancePricesAdapter(ctx.settings, batch_size=int(p.get("batch_size", 100)))
    fr = ad.fetch(entities=p.get("symbols"), period=period)
    tables = ad.normalize(fr)
    rows = {k: write_table(v, k, settings=ctx.settings) for k, v in tables.items()}
    return JobResult(status=fr.status, rows_written=rows, entities_ok=len(fr.entities_ok),
                     entities_failed=len(fr.entities_failed), watermark=fr.watermark or ctx.watermark,
                     http_requests=ad.n_downloads,
                     message=f"symbols={fr.meta.get('symbols')} failed={list(fr.entities_failed)[:10]}")


def run_incremental(ctx):  # noqa: ANN001 - JobContext
    """Nightly mode: per symbol only (last stored date - 3 d) .. today; full history only for symbols with no stored
    bars. Curated seeds + benchmarks first. Chunks of ~2000 symbols (multi-ticker batches of `batch_size`) are written
    to staging and checkpointed as they finish, so a timeout / cancellation / time budget loses at most one chunk and
    a retry or `run-dag --resume` (same as_of) continues where it stopped. Official-bulk markets (CN/TW/BR) are skipped
    except curated seeds (universe_symbols)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    s = ctx.settings
    t0 = time.monotonic()
    budget_s = float(p.get("max_minutes", 110)) * 60
    ad = YFinancePricesAdapter(s, batch_size=int(p.get("batch_size", 250)))
    symbols = list(dict.fromkeys(p.get("symbols") or ad.universe_symbols()))
    ck = Checkpoint(s, ctx.as_of)
    todo = [x for x in symbols if x not in ck.done]
    last = stored_last_dates(s)
    today = datetime.now(UTC).date()
    prio = _curated_yf(s) | set(BENCHMARK_BY_TICKER)
    chunk_size = int(p.get("chunk_size", 2000))
    chunks = (plan_chunks([x for x in todo if x in prio], last, today, chunk_size=chunk_size)
              + plan_chunks([x for x in todo if x not in prio], last, today, chunk_size=chunk_size))
    log.info("yfinance nightly: %d symbols, %d already done this run (as_of %s), %d to do in %d chunks "
             "(%d without stored history)", len(symbols), len(symbols) - len(todo), ctx.as_of, len(todo), len(chunks),
             sum(len(c) for st, c in chunks if st is None))
    rows: dict[str, int] = {}
    cancel = getattr(ctx, "cancel_event", None)
    stopped = None
    for k, (start, chunk) in enumerate(chunks):
        if cancel is not None and cancel.is_set():
            stopped = "cancelled"
            break
        if time.monotonic() - t0 > budget_s:
            stopped = f"time budget {budget_s / 60:.0f} min reached"
            break
        fr = ad.fetch(entities=chunk, start=start, period="max" if start is None else None)
        for t, v in ad.normalize(fr).items():
            rows[t] = rows.get(t, 0) + write_table(v, t, settings=s)
        ck.ok.update(fr.entities_ok)
        ck.failed.update({x: str(e)[:80] for x, e in fr.entities_failed.items()})
        ck.save()
        log.info("yfinance chunk %d/%d (start=%s): %d ok, %d no data; elapsed %.1f min", k + 1, len(chunks), start,
                 len(fr.entities_ok), len(fr.entities_failed), (time.monotonic() - t0) / 60)
    deferred = [x for x in symbols if x not in ck.done]
    n_ok, n_fail = len(ck.ok & set(symbols)), len(set(ck.failed) & set(symbols))
    status = "failed" if not n_ok else ("partial" if (deferred or n_fail) else "success")
    msg = (f"symbols={len(symbols)} ok={n_ok} no_data={n_fail} deferred={len(deferred)} "
           f"minutes={(time.monotonic() - t0) / 60:.1f}" + (f" stopped: {stopped}" if stopped else "")
           + f" failed={sorted(set(ck.failed) & set(symbols))[:10]}")
    return JobResult(status=status, rows_written=rows, entities_ok=n_ok, entities_failed=n_fail,
                     watermark=today.isoformat() if n_ok else ctx.watermark, http_requests=ad.n_downloads,
                     message=msg)


_ = (orjson, SUFFIX_MARKET)
