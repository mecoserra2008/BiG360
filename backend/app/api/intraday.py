"""Intraday news DuckDB for the API (roadmap v3 N3.3): read-only, hot-swapped when the hourly job publishes a new
file (data/db/INTRADAY pointer, written by app.news_live.intraday). The nightly snapshot is untouched; /news/* reads
merge bus (seconds old) + intraday (<= 7 days, hourly) + snapshot (older) by key.

    with intraday() as idb:          # None while no intraday file exists (snapshot + bus only)
        rows = idb.all("SELECT ... FROM nt_items ...", [..]) if idb else []
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from app.core.config import get_settings
from app.core.logsetup import get_logger

log = get_logger("app.api.intraday")

CHECK_S = 5.0
GRACE_S = 120.0


class IntradayDb:
    def __init__(self, path: Path, con: duckdb.DuckDBPyConnection):
        self.path = path
        self.con = con
        self.meta: dict[str, str] = {}
        try:
            self.meta = dict(con.execute("SELECT key, value FROM _meta").fetchall())
        except Exception:  # noqa: BLE001
            pass
        self.tables = {r[0] for r in con.execute("SELECT table_name FROM information_schema.tables").fetchall()}

    @property
    def built_at(self) -> str | None:
        return self.meta.get("built_at")

    def cursor(self) -> _Cur:
        return _Cur(self.con.cursor())


class _Cur:
    def __init__(self, cur: duckdb.DuckDBPyConnection):
        self.cur = cur
        self.cur.execute("SET TimeZone = 'UTC'")

    def all(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        self.cur.execute(sql, list(params or []))
        cols = [d[0] for d in self.cur.description]
        return [dict(zip(cols, r, strict=True)) for r in self.cur.fetchall()]

    def one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        r = self.all(sql, params)
        return r[0] if r else None

    def close(self) -> None:
        try:
            self.cur.close()
        except Exception:  # noqa: BLE001
            pass


class _Manager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._db: IntradayDb | None = None
        self._name: str | None = None
        self._checked = 0.0
        self._retired: list[tuple[float, duckdb.DuckDBPyConnection]] = []

    def get(self) -> IntradayDb | None:
        now = time.monotonic()
        if now - self._checked < CHECK_S:
            return self._db
        with self._lock:   # a concurrent first call waits here for the open instead of seeing "no file"
            if time.monotonic() - self._checked < CHECK_S:
                return self._db
            from app.news_live.intraday import current_intraday  # noqa: PLC0415

            p = current_intraday(get_settings())
            name = p.name if p else None
            if name == self._name:
                self._checked = now
                return self._db
            new = None
            if p is not None:
                try:
                    con = duckdb.connect(str(p), read_only=True)
                    con.execute("SET threads = 2")
                    new = IntradayDb(p, con)
                except Exception as e:  # noqa: BLE001 - half-written / locked: keep the previous one, retry soon
                    log.warning("intraday db %s not opened: %s", p, e)
                    self._checked = now - CHECK_S + 0.5
                    return self._db
            if self._db is not None:
                self._retired.append((now, self._db.con))
                log.info("intraday db switched %s -> %s", self._name, name)
            self._db, self._name = new, name
            keep = []
            for t, c in self._retired:
                if now - t > GRACE_S:
                    try:
                        c.close()
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    keep.append((t, c))
            self._retired = keep
            self._checked = now      # only once the (new) file is open: concurrent callers wait on the lock
            return self._db

    def reset(self) -> None:
        with self._lock:
            for _, c in self._retired:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            if self._db is not None:
                try:
                    self._db.con.close()
                except Exception:  # noqa: BLE001
                    pass
            self._db, self._name, self._retired, self._checked = None, None, [], 0.0


manager = _Manager()


@contextmanager
def intraday() -> Iterator[_Cur | None]:
    db = manager.get()
    if db is None:
        yield None
        return
    cur = db.cursor()
    try:
        yield cur
    finally:
        cur.close()


def status() -> dict[str, Any]:
    db = manager.get()
    if db is None:
        return {"available": False}
    return {"available": True, "file": db.path.name, "built_at": db.built_at,
            "window_start": db.meta.get("window_start"), "window_end": db.meta.get("window_end"),
            "counts": {k: int(v) for k, v in db.meta.items() if k.startswith("nt_") and v.isdigit()}}


_ = datetime
