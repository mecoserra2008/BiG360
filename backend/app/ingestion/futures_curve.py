"""Futures term structures from Yahoo Finance contract-month tickers -> futures_curve + futures_contracts
(roadmap v2 item #5a; docs/CROSS_ASSET_DEFINITIONS.md, docs/ROADMAP_V2_PLAN.md section 5).

Symbol format (probed 2026-10-19): ``{ROOT}{month code}{yy}.{exchange}``. Month codes are F G H J K M N Q U V X Z, and
the exchange suffix is one of NYM (NYMEX), CMX (COMEX), CBT (CBOT), CME or NYB (ICE US softs). Examples: CLZ26.NYM,
GCZ26.CMX, ZNZ26.CBT, ESZ26.CME, KCZ26.NYB, 6EZ26.CME, BTCZ26.CME. Formats tried without success: CLZ26 (no suffix),
CLZ2026.NYM.

* Yahoo serves only contracts that are still trading. Expired contracts (CLV26, CLZ24, ESU26 ...) return nothing, so a
  backfill gives each live contract's own history (CL deferred months back to 2017/2018, ES quarterlies back to 2021,
  livestock and Treasuries only a few months). Older dates therefore hold deferred contracts only. The nightly job
  accumulates the full front of the curve from then on.
* Listed months follow each root's exchange cycle (ROOTS: `cycle` plus `serial` nearby calendar months). The job asks
  for the next `n` listed contracts. A listed contract that Yahoo does not serve is recorded as `missing` in
  futures_contracts and never interpolated.
* Yahoo labels the Globex Sunday-evening session with the Sunday date (a partial bar). Weekend-dated bars and bars after
  the as-of session are dropped. The latest bar's volume can repeat the previous day's value; the next run re-fetches
  a one-month overlap and upserts the corrected value.
* expiry is an APPROXIMATE last trading day from the exchange rule (EXPIRY_RULES, documented per rule). The NYSE
  holiday calendar (exchange_calendars XNYS, extended to 2033) is the business-day proxy for CME/ICE US.
* Pacing: batches of `batch` symbols (yfinance issues one chart request per symbol), one token of the machine-wide
  "yfinance" bucket per batch, plus a pause. If a batch comes back completely empty, a canary (SPY) separates throttling
  (cool-down 1/2/4/8 min, then retry) from symbols that are really missing. Symbols left empty are retried one by one at
  the end of the run.

Jobs: `futures_curve` (nightly, incremental: 1-month overlap; full history for contracts with no stored bars) and
`futures_curve_backfill` (manual, period=max). Detached CLI (from backend/):

    python -m app.ingestion.futures_curve backfill [--roots CL,GC]   # period=max for the current contract set
    python -m app.ingestion.futures_curve incremental [--roots ...]
    python -m app.ingestion.futures_curve status

State: data/state/roadmap_v2.json key "item5a" (shared file, guarded by a file lock). Logs: data/logs/roadmap_v2/.
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.ingestion.futures_curve")

MONTH_CODES = "FGHJKMNQUVXZ"
CODE_MONTH = {c: i + 1 for i, c in enumerate(MONTH_CODES)}
STATE_FILE = "roadmap_v2.json"
STATE_KEY = "item5a"
SOURCE = "yahoo"
MIN_DTE_ROLL = 5  # contracts within 5 calendar days of expiry are skipped by the curve analytics (delivery noise)


# ------------------------------------------------------------------------------------------------ registry


@dataclass(frozen=True)
class FutRoot:
    root: str
    name: str
    exchange: str            # Yahoo suffix
    asset_class: str         # energy | metals | grains | softs | livestock | rates | equity_index | fx | crypto
    cycle: str               # listed month codes
    n: int                   # contracts tracked (next n listed)
    rule: str                # key of EXPIRY_RULES
    serial: int = 0          # extra consecutive nearby calendar months listed outside the cycle

    @property
    def front_security_id(self) -> str:
        """prices_daily id of the continuous front-month instrument (Yahoo `=F`)."""
        return f"IDX:{self.root}=F"

    def symbol(self, year: int, month: int) -> str:
        return f"{self.root}{MONTH_CODES[month - 1]}{year % 100:02d}.{self.exchange}"


ALL = MONTH_CODES
QTR = "HMUZ"
#: tracked roots: every continuous =F future already priced (price_coverage_lists.FUTURES + price_history_lists
#: FUTURES_EXTRA) minus micro / mini duplicates (MGC, SIL, QM) and LBR / LBS (Yahoo serves no lumber contract months).
ROOTS: tuple[FutRoot, ...] = (
    FutRoot("CL", "WTI crude oil", "NYM", "energy", ALL, 12, "cl"),
    FutRoot("BZ", "Brent crude oil (NYMEX BZ)", "NYM", "energy", ALL, 12, "brent"),
    FutRoot("NG", "Henry Hub natural gas", "NYM", "energy", ALL, 12, "ng"),
    FutRoot("RB", "RBOB gasoline", "NYM", "energy", ALL, 12, "last_bd_prev"),
    FutRoot("HO", "NY Harbor ULSD (heating oil)", "NYM", "energy", ALL, 12, "last_bd_prev"),
    FutRoot("TTF", "Dutch TTF natural gas (NYMEX)", "NYM", "energy", ALL, 6, "ttf"),
    FutRoot("GC", "Gold", "CMX", "metals", "GJMQVZ", 8, "metal", serial=3),
    FutRoot("SI", "Silver", "CMX", "metals", "HKNUZ", 6, "metal", serial=3),
    FutRoot("HG", "Copper", "CMX", "metals", ALL, 8, "metal"),
    FutRoot("PL", "Platinum", "NYM", "metals", "FJNV", 4, "metal", serial=3),
    FutRoot("PA", "Palladium", "NYM", "metals", "HMUZ", 4, "metal", serial=3),
    FutRoot("ALI", "Aluminum (COMEX)", "CMX", "metals", ALL, 6, "metal"),
    FutRoot("HRC", "US Midwest HRC steel", "CMX", "metals", ALL, 6, "last_bd"),
    FutRoot("ZC", "Corn", "CBT", "grains", "HKNUZ", 6, "grain"),
    FutRoot("ZW", "Chicago SRW wheat", "CBT", "grains", "HKNUZ", 6, "grain"),
    FutRoot("KE", "KC HRW wheat", "CBT", "grains", "HKNUZ", 5, "grain"),
    FutRoot("ZS", "Soybeans", "CBT", "grains", "FHKNQUX", 8, "grain"),
    FutRoot("ZL", "Soybean oil", "CBT", "grains", "FHKNQUVZ", 8, "grain"),
    FutRoot("ZM", "Soybean meal", "CBT", "grains", "FHKNQUVZ", 8, "grain"),
    FutRoot("ZO", "Oats", "CBT", "grains", "HKNUZ", 4, "grain"),
    FutRoot("ZR", "Rough rice", "CBT", "grains", "FHKNUX", 4, "grain"),
    FutRoot("KC", "Coffee C", "NYB", "softs", "HKNUZ", 5, "kc"),
    FutRoot("SB", "Sugar No. 11", "NYB", "softs", "HKNV", 6, "last_bd_prev"),
    FutRoot("CC", "Cocoa", "NYB", "softs", "HKNUZ", 5, "cc"),
    FutRoot("CT", "Cotton No. 2", "NYB", "softs", "HKNVZ", 5, "ct"),
    FutRoot("OJ", "FCOJ-A orange juice", "NYB", "softs", "FHKNUX", 4, "oj"),
    FutRoot("LE", "Live cattle", "CME", "livestock", "GJMQVZ", 6, "last_bd"),
    FutRoot("HE", "Lean hogs", "CME", "livestock", "GJKMNQVZ", 6, "he"),
    FutRoot("GF", "Feeder cattle", "CME", "livestock", "FHJKQUVX", 6, "gf"),
    FutRoot("DC", "Class III milk", "CME", "livestock", ALL, 6, "dc"),
    FutRoot("ZT", "2-Year T-Note", "CBT", "rates", QTR, 2, "last_bd"),
    FutRoot("ZF", "5-Year T-Note", "CBT", "rates", QTR, 2, "last_bd"),
    FutRoot("ZN", "10-Year T-Note", "CBT", "rates", QTR, 2, "ust_long"),
    FutRoot("ZB", "30-Year T-Bond", "CBT", "rates", QTR, 3, "ust_long"),
    FutRoot("ES", "E-mini S&P 500", "CME", "equity_index", QTR, 5, "equity"),
    FutRoot("NQ", "E-mini Nasdaq-100", "CME", "equity_index", QTR, 4, "equity"),
    FutRoot("RTY", "E-mini Russell 2000", "CME", "equity_index", QTR, 3, "equity"),
    FutRoot("YM", "E-mini Dow", "CBT", "equity_index", QTR, 3, "equity"),
    FutRoot("6E", "Euro FX", "CME", "fx", QTR, 6, "fx", serial=3),
    FutRoot("6J", "Japanese yen", "CME", "fx", QTR, 6, "fx", serial=3),
    FutRoot("6B", "British pound", "CME", "fx", QTR, 6, "fx", serial=3),
    FutRoot("BTC", "Bitcoin (CME)", "CME", "crypto", ALL, 5, "crypto"),
    FutRoot("ETH", "Ether (CME)", "CME", "crypto", ALL, 4, "crypto"),
)
ROOT_BY_ID: dict[str, FutRoot] = {r.root: r for r in ROOTS}


# ------------------------------------------------------------------------------------------------ business days / expiry


@lru_cache(maxsize=1)
def _holidays() -> frozenset[date]:
    """NYSE full-day holidays 2000-2033 (proxy for CME / ICE US business days); empty if exchange_calendars fails."""
    try:
        import exchange_calendars as xcals  # noqa: PLC0415
        import pandas as pd  # noqa: PLC0415

        cal = xcals.get_calendar("XNYS", start="2000-01-01", end="2033-12-31")
        sessions = {d.date() for d in cal.sessions}
        days = pd.bdate_range("2000-01-01", "2033-12-31")
        return frozenset(d.date() for d in days if d.date() not in sessions)
    except Exception as e:  # noqa: BLE001
        log.warning("exchange_calendars unavailable (%s): weekdays only", e)
        return frozenset()


def is_bd(d: date) -> bool:
    return d.weekday() < 5 and d not in _holidays()


def bd_before(d: date, n: int) -> date:
    """The n-th business day strictly before d."""
    k = 0
    while k < n:
        d -= timedelta(days=1)
        if is_bd(d):
            k += 1
    return d


def bd_on_or_before(d: date) -> date:
    while not is_bd(d):
        d -= timedelta(days=1)
    return d


def nth_bd(year: int, month: int, n: int) -> date:
    """n-th business day of the month (1 = first)."""
    d = date(year, month, 1) - timedelta(days=1)
    k = 0
    while k < n:
        d += timedelta(days=1)
        if is_bd(d):
            k += 1
    return d


def nth_last_bd(year: int, month: int, n: int) -> date:
    """n-th last business day of the month (1 = last)."""
    d = date(year, month, calendar.monthrange(year, month)[1]) + timedelta(days=1)
    k = 0
    while k < n:
        d -= timedelta(days=1)
        if is_bd(d):
            k += 1
    return d


def _add_months(year: int, month: int, k: int) -> tuple[int, int]:
    m = year * 12 + (month - 1) + k
    return m // 12, m % 12 + 1


def _weekday_n(year: int, month: int, weekday: int, n: int) -> date:
    """n-th given weekday (0 = Monday) of the month; n = -1 means the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year, month, calendar.monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _exp_cl(y: int, m: int) -> date:
    py, pm = _add_months(y, m, -1)
    d25 = date(py, pm, 25)
    return bd_before(d25, 3 if is_bd(d25) else 4)


def _exp_dc(y: int, m: int) -> date:
    ny, nm = _add_months(y, m, 1)
    d = date(ny, nm, 5)
    while d.weekday() != 2:   # USDA Class III announcement: the Wednesday on or before the 5th of the next month
        d -= timedelta(days=1)
    return bd_before(d, 1)


#: rule key -> (description, function(year, month) -> approximate last trading day)
EXPIRY_RULES: dict[str, tuple[str, Any]] = {
    "cl": ("NYMEX CL: 3 business days before the 25th of the month before delivery (4 if the 25th is not a business day)", _exp_cl),
    "brent": ("NYMEX BZ: last business day of the second month before the contract month",
              lambda y, m: nth_last_bd(*_add_months(y, m, -2), 1)),
    "ng": ("NYMEX NG: 3 business days before the first calendar day of the contract month",
           lambda y, m: bd_before(date(y, m, 1), 3)),
    "last_bd_prev": ("Last business day of the month before the contract month (RB, HO, SB)",
                     lambda y, m: nth_last_bd(*_add_months(y, m, -1), 1)),
    "ttf": ("NYMEX TTF: 2 business days before the first calendar day of the contract month (approximation)",
            lambda y, m: bd_before(date(y, m, 1), 2)),
    "metal": ("COMEX / NYMEX metals: third last business day of the contract month", lambda y, m: nth_last_bd(y, m, 3)),
    "last_bd": ("Last business day of the contract month (HRC, LE, ZT, ZF)", lambda y, m: nth_last_bd(y, m, 1)),
    "grain": ("CBOT grains: business day before the 15th calendar day of the contract month",
              lambda y, m: bd_before(date(y, m, 15), 1)),
    "kc": ("ICE Coffee C: one business day before the last notice day (8 business days before the last business day)",
           lambda y, m: nth_last_bd(y, m, 9)),
    "cc": ("ICE Cocoa: one business day before the last notice day (11 business days before the last business day)",
           lambda y, m: nth_last_bd(y, m, 12)),
    "ct": ("ICE Cotton No. 2: 17 business days from the end of the contract month", lambda y, m: nth_last_bd(y, m, 17)),
    "oj": ("ICE FCOJ-A: 14th business day before the last business day of the contract month",
           lambda y, m: nth_last_bd(y, m, 15)),
    "he": ("CME lean hogs: 10th business day of the contract month", lambda y, m: nth_bd(y, m, 10)),
    "gf": ("CME feeder cattle: last Thursday of the contract month (approximation; November exceptions ignored)",
           lambda y, m: bd_on_or_before(_weekday_n(y, m, 3, -1))),
    "dc": ("CME Class III milk: business day before the USDA announcement (Wednesday on or before the 5th of the next month)", _exp_dc),
    "ust_long": ("CBOT ZN / ZB: 7th business day before the last business day of the contract month",
                 lambda y, m: nth_last_bd(y, m, 8)),
    "equity": ("CME equity index: third Friday of the contract month", lambda y, m: bd_on_or_before(_weekday_n(y, m, 4, 3))),
    "fx": ("CME FX: 2 business days before the third Wednesday of the contract month",
           lambda y, m: bd_before(_weekday_n(y, m, 2, 3), 2)),
    "crypto": ("CME BTC / ETH: last Friday of the contract month", lambda y, m: bd_on_or_before(_weekday_n(y, m, 4, -1))),
}


def expiry(root: str | FutRoot, year: int, month: int) -> date:
    r = root if isinstance(root, FutRoot) else ROOT_BY_ID[root]
    return EXPIRY_RULES[r.rule][1](year, month)


def parse_contract(code: str) -> tuple[int, int]:
    """'Z26' -> (2026, 12)."""
    return 2000 + int(code[1:]), CODE_MONTH[code[0]]


def listed_contracts(r: FutRoot, as_of: date, n: int | None = None) -> list[dict[str, Any]]:
    """The next n contracts of r's listing cycle (plus `serial` nearby calendar months) that expire on/after as_of."""
    n = n or r.n
    out: list[dict[str, Any]] = []
    y, m = _add_months(as_of.year, as_of.month, -1)
    first_live: tuple[int, int] | None = None
    for _ in range(12 * 10):
        exp = expiry(r, y, m)
        if exp >= as_of:
            if first_live is None:
                first_live = (y, m)
            k = (y * 12 + m) - (first_live[0] * 12 + first_live[1])
            if MONTH_CODES[m - 1] in r.cycle or k < r.serial:
                out.append({"root": r.root, "contract_month": date(y, m, 1), "contract": f"{MONTH_CODES[m - 1]}{y % 100:02d}",
                            "symbol": r.symbol(y, m), "expiry": exp})
                if len(out) >= n:
                    break
        y, m = _add_months(y, m, 1)
    return out


def last_session(today: date | None = None) -> date:
    """Last completed weekday session before today (the default as-of of a manual run)."""
    d = (today or datetime.now(UTC).date()) - timedelta(days=1)
    while not is_bd(d):
        d -= timedelta(days=1)
    return d


# ------------------------------------------------------------------------------------------------ state


def _state_path(s: Settings) -> Path:
    return s.data_dir / "state" / STATE_FILE


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = _state_path(settings or get_settings())
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")).get(STATE_KEY, {})
    except ValueError:
        return {}


def update_state(settings: Settings | None = None, **fields: Any) -> dict[str, Any]:
    """Merge fields into roadmap_v2.json["item5a"] (other keys of the shared file are preserved)."""
    p = _state_path(settings or get_settings())
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        st = doc.setdefault(STATE_KEY, {})
        for k, v in fields.items():
            if isinstance(v, dict) and isinstance(st.get(k), dict):
                st[k].update(v)
            else:
                st[k] = v
        st["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
        return st


# ------------------------------------------------------------------------------------------------ fetch


def _canary_ok() -> bool:
    from app.ingestion.price_history import _canary_ok as ph_canary  # noqa: PLC0415

    return ph_canary()


def _download(symbols: Sequence[str], period: str) -> pl.DataFrame:
    """One yf.download call -> long [symbol, date, open, high, low, close, volume] (empty on error)."""
    import logging  # noqa: PLC0415

    import yfinance as yf  # noqa: PLC0415

    from app.core.ratelimit import get_limiter  # noqa: PLC0415
    from app.ingestion.yf_prices import wide_to_long  # noqa: PLC0415

    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    get_limiter("yfinance").acquire()
    try:
        pdf = yf.download(list(symbols), period=period, interval="1d", progress=False, threads=False,
                          group_by="ticker", auto_adjust=False, actions=False, timeout=30)
    except Exception as e:  # noqa: BLE001
        log.warning("yf.download failed (%d symbols): %s", len(symbols), e)
        return pl.DataFrame()
    long = wide_to_long(pdf, list(symbols))
    if long.is_empty():
        return long
    return long.select("symbol", "date", "open", "high", "low", "close", "volume")


def fetch_symbols(symbols: Sequence[str], period: str, batch: int = 8, pause: float = 4.0,
                  max_cooldowns: int = 4, sleep=time.sleep, downloader=None, canary=None,
                  should_stop=None) -> tuple[pl.DataFrame, set[str]]:
    """Fetch symbols in paced batches. Returns (bars, unchecked) where unchecked = symbols whose batch stayed throttled.
    Empty symbols are retried one by one at the end (after a canary check)."""
    downloader = downloader or _download
    canary = canary or _canary_ok
    frames: list[pl.DataFrame] = []
    got: set[str] = set()
    unchecked: set[str] = set()

    def _run(chunk: Sequence[str]) -> pl.DataFrame:
        for attempt in range(max_cooldowns + 1):
            df = downloader(chunk, period)
            if not df.is_empty() or canary():
                return df
            if attempt == max_cooldowns:
                break
            wait = 60.0 * 2 ** attempt
            log.warning("empty batch and canary failed (throttled?): cool-down %.0fs", wait)
            sleep(wait)
        unchecked.update(chunk)
        return pl.DataFrame()

    syms = list(dict.fromkeys(symbols))
    for i in range(0, len(syms), batch):
        if should_stop and should_stop():
            unchecked.update(syms[i:])
            break
        chunk = syms[i:i + batch]
        df = _run(chunk)
        if not df.is_empty():
            frames.append(df)
            got.update(df.get_column("symbol").unique().to_list())
        sleep(pause)
    retry = [s for s in syms if s not in got and s not in unchecked]
    if retry:
        log.info("retrying %d empty symbols one by one", len(retry))
        for s in retry:
            if should_stop and should_stop():
                break
            df = _run([s])
            if not df.is_empty():
                frames.append(df)
                got.add(s)
            sleep(max(1.5, pause / 2))
    bars = pl.concat(frames, how="vertical_relaxed") if frames else pl.DataFrame()
    return bars, unchecked


def bars_to_rows(bars: pl.DataFrame, contracts: pl.DataFrame, as_of: date, now: datetime | None = None) -> pl.DataFrame:
    """Long Yahoo bars + contract metadata -> futures_curve rows (weekend / post-as_of bars dropped)."""
    if bars.is_empty():
        return pl.DataFrame()
    now = now or datetime.now(UTC)
    meta = contracts.select("symbol", "root", "contract_month", "contract", "expiry")
    df = bars.join(meta, on="symbol", how="inner").filter(
        (pl.col("date").dt.weekday() <= 5) & (pl.col("date") <= as_of)
        & pl.col("close").is_not_null() & pl.col("close").is_finite() & (pl.col("close") > 0)
        & (pl.col("date") <= pl.col("expiry")))
    for c in ("open", "high", "low"):
        df = df.with_columns(pl.when(pl.col(c).is_finite() & (pl.col(c) > 0)).then(pl.col(c)).otherwise(None).alias(c))
    return df.select(
        "root", "contract_month", "date", "contract", "symbol", "expiry",
        (pl.col("expiry") - pl.col("date")).dt.total_days().cast(pl.Int16).alias("days_to_expiry"),
        pl.col("open").cast(pl.Float32), pl.col("high").cast(pl.Float32), pl.col("low").cast(pl.Float32),
        pl.col("close").cast(pl.Float32),
        pl.when(pl.col("volume").is_finite()).then(pl.col("volume")).otherwise(None).round(0).cast(pl.Int64).alias("volume"),
        pl.lit(SOURCE).alias("source"), pl.col("date").alias("knowledge_date"), pl.lit(now).alias("ingested_at"),
    ).unique(subset=["root", "contract_month", "date"], keep="last").sort("root", "contract_month", "date")


# ------------------------------------------------------------------------------------------------ run


def contract_frame(as_of: date, roots: Iterable[str] | None = None) -> pl.DataFrame:
    rows = []
    for r in ROOTS:
        if roots and r.root not in set(roots):
            continue
        for c in listed_contracts(r, as_of):
            rows.append({**c, "root_name": r.name, "asset_class": r.asset_class, "exchange": r.exchange,
                         "expiry_rule": EXPIRY_RULES[r.rule][0]})
    return pl.DataFrame(rows)


def _stored_symbols(s: Settings) -> set[str]:
    from app.core.storage import scan_table, table_dir  # noqa: PLC0415

    if not table_dir("futures_curve", s).exists():
        return set()
    try:
        return set(scan_table("futures_curve", s).select("symbol").unique().collect().get_column("symbol").to_list())
    except Exception:  # noqa: BLE001
        return set()


def _coverage(contracts: pl.DataFrame, as_of: date, unchecked: set[str], s: Settings) -> pl.DataFrame:
    """futures_contracts rows for the requested contracts (+ previously registered ones now expired)."""
    from app.core.storage import read_table, scan_table, table_dir  # noqa: PLC0415

    now = datetime.now(UTC)
    stats = pl.DataFrame(schema={"symbol": pl.Utf8, "first_date": pl.Date, "last_date": pl.Date, "n_bars": pl.Int32,
                                 "last_close": pl.Float32})
    if table_dir("futures_curve", s).exists():
        stats = (scan_table("futures_curve", s).select("symbol", "date", "close").sort("date")
                 .group_by("symbol").agg(pl.col("date").min().alias("first_date"), pl.col("date").max().alias("last_date"),
                                         pl.len().cast(pl.Int32).alias("n_bars"), pl.col("close").last().alias("last_close"))
                 .collect())
    cur = contracts.join(stats, on="symbol", how="left").with_columns(
        pl.when(pl.col("symbol").is_in(list(unchecked)) & pl.col("n_bars").is_null()).then(pl.lit("unchecked"))
        .when(pl.col("n_bars").is_null()).then(pl.lit("missing"))
        .when(pl.col("last_date") < as_of - timedelta(days=10)).then(pl.lit("missing"))
        .otherwise(pl.lit("served")).alias("status"),
        pl.lit(as_of).alias("last_checked"), pl.lit(now).alias("updated_at"))
    prev = read_table("futures_contracts", settings=s) if table_dir("futures_contracts", s).exists() else pl.DataFrame()
    if not prev.is_empty():
        old = prev.filter(~pl.col("symbol").is_in(cur.get_column("symbol").to_list()) & (pl.col("expiry") < as_of)
                          & (pl.col("status") != "expired"))
        if old.height:
            old = old.drop("first_date", "last_date", "n_bars", "last_close").join(stats, on="symbol", how="left") \
                .with_columns(pl.lit("expired").alias("status"), pl.lit(now).alias("updated_at"))
            cur = pl.concat([cur, old.select(cur.columns)], how="vertical_relaxed")
    return cur


def run_fetch(mode: str = "incremental", as_of: date | None = None, roots: Iterable[str] | None = None,
              settings: Settings | None = None, batch: int = 8, pause: float = 4.0, run_id: str | None = None,
              should_stop=None) -> dict[str, Any]:
    """Fetch the current contract set and upsert futures_curve / futures_contracts.
    mode incremental: period 1mo for stored contracts, max for contracts without stored bars; backfill: max for all."""
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    as_of = as_of or last_session()
    roots = [r.upper() for r in roots] if roots else None
    t0 = time.time()
    contracts = contract_frame(as_of, roots)
    syms = contracts.get_column("symbol").to_list()
    stored = _stored_symbols(s) if mode == "incremental" else set()
    groups = {"max": [x for x in syms if x not in stored], "1mo": [x for x in syms if x in stored]}
    update_state(s, steps={"futures_curve_" + mode: {"status": "running", "as_of": str(as_of), "contracts": len(syms),
                                                     "started_at": datetime.now(UTC).isoformat(timespec="seconds")}})
    rows_written = 0
    unchecked: set[str] = set()
    n_bars = 0
    for period, group in groups.items():
        if not group:
            continue
        log.info("futures_curve %s: %d contracts period=%s", mode, len(group), period)
        bars, unc = fetch_symbols(group, period, batch=batch, pause=pause, should_stop=should_stop)
        unchecked |= unc
        rows = bars_to_rows(bars, contracts, as_of)
        n_bars += rows.height
        if rows.height:
            rows_written += write_table(rows, "futures_curve", settings=s)
    cov = _coverage(contracts, as_of, unchecked, s)
    write_table(cov, "futures_contracts", settings=s)
    summary = (cov.filter(pl.col("status") != "expired").sort("root", "contract_month").group_by("root", maintain_order=True)
               .agg(pl.col("contract").filter(pl.col("status") == "served").alias("served"),
                    pl.col("contract").filter(pl.col("status") == "missing").alias("missing"),
                    pl.col("contract").filter(pl.col("status") == "unchecked").alias("unchecked")))
    coverage = {r["root"]: {k: r[k] for k in ("served", "missing", "unchecked")} for r in summary.to_dicts()}
    res = {"status": "done", "mode": mode, "as_of": str(as_of), "contracts": len(syms),
           "served": int((cov.get_column("status") == "served").sum()),
           "missing": int((cov.get_column("status") == "missing").sum()), "unchecked": len(unchecked),
           "rows_written": rows_written, "minutes": round((time.time() - t0) / 60, 1),
           "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "run_id": run_id}
    update_state(s, steps={"futures_curve_" + mode: res}, coverage=coverage)
    log.info("futures_curve %s done: %s", mode, res)
    return res


def run(ctx):  # -> JobResult  (job futures_curve, nightly incremental)
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    res = run_fetch("incremental", as_of=ctx.as_of, roots=p.get("roots"), settings=ctx.settings,
                    batch=int(p.get("batch", 8)), pause=float(p.get("pause", 4.0)), run_id=ctx.run_id,
                    should_stop=ctx.cancel_event.is_set)
    status = "success" if res["unchecked"] == 0 else "partial"
    return JobResult(status=status, rows_written={"futures_curve": res["rows_written"]}, entities_ok=res["served"],
                     entities_failed=res["missing"] + res["unchecked"], watermark=res["as_of"],
                     message=f"{res['served']} served, {res['missing']} missing, {res['unchecked']} unchecked")


def run_backfill_job(ctx):  # -> JobResult  (job futures_curve_backfill, manual)
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    res = run_fetch("backfill", as_of=ctx.as_of, roots=p.get("roots"), settings=ctx.settings,
                    batch=int(p.get("batch", 8)), pause=float(p.get("pause", 4.0)), run_id=ctx.run_id,
                    should_stop=ctx.cancel_event.is_set)
    return JobResult(status="success" if res["unchecked"] == 0 else "partial",
                     rows_written={"futures_curve": res["rows_written"]}, entities_ok=res["served"],
                     entities_failed=res["missing"] + res["unchecked"], watermark=res["as_of"])


# ------------------------------------------------------------------------------------------------ read helpers


def curve_snapshot(root: str, on: date | None = None, settings: Settings | None = None,
                   min_dte: int = 0) -> pl.DataFrame:
    """Term structure of `root` on the last stored date <= on (all contracts with a bar that day), nearest first."""
    from app.core.storage import scan_table  # noqa: PLC0415

    lf = scan_table("futures_curve", settings).filter(pl.col("root") == root)
    if on is not None:
        lf = lf.filter(pl.col("date") <= on)
    last = lf.select(pl.col("date").max()).collect().item()
    if last is None:
        return pl.DataFrame()
    return lf.filter((pl.col("date") == last) & (pl.col("days_to_expiry") >= min_dte)).sort("expiry").collect()


# ------------------------------------------------------------------------------------------------ CLI


def _stop_file(s: Settings) -> Path:
    return s.data_dir / "state" / "roadmap_v2_item5a.STOP"


def main(argv: list[str] | None = None) -> int:
    from app.core.logsetup import setup_logging  # noqa: PLC0415

    ap = argparse.ArgumentParser(prog="python -m app.ingestion.futures_curve")
    ap.add_argument("cmd", choices=["backfill", "incremental", "status", "contracts"])
    ap.add_argument("--roots", default=None, help="comma-separated roots (default: all)")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--pause", type=float, default=4.0)
    a = ap.parse_args(argv)
    setup_logging(log_file="roadmap_v2/item5a.log")
    s = get_settings()
    roots = [x for x in (a.roots or "").split(",") if x] or None
    as_of = date.fromisoformat(a.as_of) if a.as_of else None
    if a.cmd == "status":
        print(json.dumps(load_state(s), indent=1, default=str))
        return 0
    if a.cmd == "contracts":
        print(contract_frame(as_of or last_session(), roots).select("root", "contract", "symbol", "expiry"))
        return 0
    stop = _stop_file(s)
    res = run_fetch(a.cmd, as_of=as_of, roots=roots, settings=s, batch=a.batch, pause=a.pause,
                    should_stop=stop.exists)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
