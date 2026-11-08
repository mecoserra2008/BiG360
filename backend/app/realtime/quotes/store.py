"""Intraday "last quote" store: data/realtime/quotes.sqlite (WAL; one writer = the quotes service, readers = the API).

    last_quote   one row per security_id: the newest DELAYED quote (price, change vs the previous official close,
                 currency, market state, print time, fetch time, stated delay, source)
    quote_ticks  (security_id, quote_ts) -> price, kept `tick_days` (2) days: intraday path for sparklines
    markets      session state per market key (open / reason / next open) as last evaluated by the service

This is NOT the official price history: prices_daily (and the serving `prices`) only ever get the official daily close
from the nightly finalise. Interest file data/state/quotes_interest.json: security ids the UI asked for recently
(GET /live/quotes), polled for `INTEREST_TTL_S` even when they are in no watchlist.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings

INTEREST_TTL_S = 30 * 60
INTEREST_MAX = 200

_SCHEMA = """
CREATE TABLE IF NOT EXISTS last_quote (
    security_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL, price REAL NOT NULL, change REAL,
    change_pct REAL, prev_close REAL, currency TEXT, market TEXT, market_state TEXT, quote_ts REAL, fetched_at REAL NOT NULL,
    delay_min INTEGER, exchange TEXT, quote_type TEXT, day_high REAL, day_low REAL, volume REAL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS quote_ticks (
    security_id TEXT NOT NULL, quote_ts REAL NOT NULL, price REAL NOT NULL, PRIMARY KEY (security_id, quote_ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS markets (
    market TEXT PRIMARY KEY, open INTEGER NOT NULL, reason TEXT, opens_at TEXT, closes_at TEXT, updated_at REAL NOT NULL
) WITHOUT ROWID;
"""

COLS = ("security_id", "symbol", "source", "price", "change", "change_pct", "prev_close", "currency", "market",
        "market_state", "quote_ts", "fetched_at", "delay_min", "exchange", "quote_type", "day_high", "day_low", "volume")


def default_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / "realtime" / "quotes.sqlite"


def interest_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "quotes_interest.json"


class QuoteStore:
    def __init__(self, path: str | Path | None = None, *, read_only: bool = False, settings: Settings | None = None):
        self.path = Path(path) if path else default_path(settings)
        self.read_only = read_only
        self._local = threading.local()
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._con().executescript(_SCHEMA)

    def _con(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is not None:
            return con
        if self.read_only:
            con = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True, isolation_level=None,
                                  check_same_thread=False, timeout=5)
        else:
            con = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False, timeout=15)
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=5000")
        self._local.con = con
        return con

    # ---- write (quotes service)
    def upsert(self, rows: Sequence[dict[str, Any]], tick_days: float = 2.0) -> int:
        if not rows:
            return 0
        con = self._con()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.executemany(
                f"INSERT INTO last_quote ({','.join(COLS)}) VALUES ({','.join('?' * len(COLS))}) "
                f"ON CONFLICT(security_id) DO UPDATE SET {','.join(f'{c}=excluded.{c}' for c in COLS[1:])}",
                [tuple(r.get(c) for c in COLS) for r in rows])
            con.executemany("INSERT OR IGNORE INTO quote_ticks(security_id, quote_ts, price) VALUES (?, ?, ?)",
                            [(r["security_id"], r.get("quote_ts") or r["fetched_at"], r["price"]) for r in rows])
            con.execute("DELETE FROM quote_ticks WHERE quote_ts < ?", (time.time() - tick_days * 86400,))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return len(rows)

    def set_markets(self, states: Iterable[dict[str, Any]]) -> None:
        con = self._con()
        now = time.time()
        con.executemany("INSERT INTO markets(market, open, reason, opens_at, closes_at, updated_at) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(market) DO UPDATE SET open=excluded.open, reason=excluded.reason, "
                        "opens_at=excluded.opens_at, closes_at=excluded.closes_at, updated_at=excluded.updated_at",
                        [(s["market"], int(bool(s["open"])), s.get("reason"), s.get("opens_at"), s.get("closes_at"), now)
                         for s in states])

    # ---- read (API + service)
    def last(self, ids: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
        con = self._con()
        if ids is None:
            rows = con.execute(f"SELECT {','.join(COLS)} FROM last_quote").fetchall()
        else:
            ids = list(dict.fromkeys(ids))
            rows = []
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                rows += con.execute(f"SELECT {','.join(COLS)} FROM last_quote WHERE security_id IN "
                                    f"({','.join('?' * len(chunk))})", chunk).fetchall()
        return {r[0]: dict(zip(COLS, r, strict=True)) for r in rows}

    def markets(self) -> dict[str, dict[str, Any]]:
        try:
            rows = self._con().execute("SELECT market, open, reason, opens_at, closes_at, updated_at FROM markets").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {m: {"market": m, "open": bool(o), "reason": r, "opens_at": oa, "closes_at": ca, "updated_at": u}
                for m, o, r, oa, ca, u in rows}

    def ticks(self, security_id: str, since: float | None = None) -> list[tuple[float, float]]:
        q = "SELECT quote_ts, price FROM quote_ticks WHERE security_id = ?"
        args: list[Any] = [security_id]
        if since is not None:
            q += " AND quote_ts >= ?"
            args.append(since)
        return [(float(t), float(p)) for t, p in self._con().execute(q + " ORDER BY quote_ts", args).fetchall()]

    def stats(self) -> dict[str, Any]:
        con = self._con()
        n, last = con.execute("SELECT count(*), max(fetched_at) FROM last_quote").fetchone()
        nt = con.execute("SELECT count(*) FROM quote_ticks").fetchone()[0]
        return {"securities": int(n or 0), "ticks": int(nt or 0), "last_fetch": last}

    def close(self) -> None:
        con = getattr(self._local, "con", None)
        if con is not None:
            con.close()
            self._local.con = None


# ------------------------------------------------------------------------------------------------ interest file
def record_interest(ids: Iterable[str], settings: Settings | None = None, now: float | None = None) -> None:
    """API side: remember ids the UI asked quotes for (merged, pruned, atomic replace; best effort)."""
    now = time.time() if now is None else now
    ids = [i for i in ids if i]
    if not ids:
        return
    p = interest_path(settings)
    try:
        doc = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        doc = {}
    doc = {k: v for k, v in doc.items() if isinstance(v, (int, float)) and now - v < INTEREST_TTL_S}
    for i in ids:
        doc[i] = now
    if len(doc) > INTEREST_MAX:
        doc = dict(sorted(doc.items(), key=lambda kv: kv[1])[-INTEREST_MAX:])
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def read_interest(settings: Settings | None = None, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    try:
        doc = json.loads(interest_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [k for k, v in doc.items() if isinstance(v, (int, float)) and now - v < INTEREST_TTL_S]
