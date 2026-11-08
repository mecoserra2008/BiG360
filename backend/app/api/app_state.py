"""User state in data/db/app_state.duckdb (owner: api): watchlists, watchlist_items, alerts (+ model_promotions).
`alert_events` rows are written by the worker's alerts_eval job (platform); the API only reads them and flips `read`.
The worker's alerts_eval also updates `alerts.last_fired_at` (agreed exception).

DuckDB allows one read-write *process* per file, so every operation opens the file briefly, runs one short
transaction and closes it (`connect_app_state` retries while the worker holds the lock). Within the API process all
threads share DuckDB's instance cache for the same path, so concurrent requests do not conflict with each other.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, TypeVar

import duckdb

from app.core.config import get_settings
from app.core.storage import connect_app_state, ensure_duckdb_tables

TABLES = ("watchlists", "watchlist_items", "alerts", "alert_events", "model_promotions")
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ensured: set[str] = set()
_ensure_lock = threading.Lock()
T = TypeVar("T")


def new_id(prefix: str) -> str:
    return prefix + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "".join(secrets.choice(_B32) for _ in range(6))


def now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def connect() -> Iterator[duckdb.DuckDBPyConnection]:
    s = get_settings()
    con = connect_app_state(read_only=False, attempts=25, delay=0.1, settings=s)
    try:
        key = str(s.app_state_db_path)
        if key not in _ensured:
            with _ensure_lock:
                ensure_duckdb_tables(con, TABLES)
                _ensured.add(key)
        con.execute("SET TimeZone = 'UTC'")
        yield con
    finally:
        con.close()


def transact(fn: Callable[[duckdb.DuckDBPyConnection], T], attempts: int = 5) -> T:
    """Run fn(con) inside BEGIN/COMMIT; retried on DuckDB transaction conflicts."""
    last: Exception | None = None
    for i in range(attempts):
        with connect() as con:
            try:
                con.execute("BEGIN TRANSACTION")
                out = fn(con)
                con.execute("COMMIT")
                return out
            except duckdb.TransactionException as e:
                last = e
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
            except Exception:
                try:
                    con.execute("ROLLBACK")
                except duckdb.Error:
                    pass
                raise
        time.sleep(0.05 * (2 ** i))
    assert last is not None
    raise last


def rows(con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]


def read(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with connect() as con:
        return rows(con, sql, params)


# ------------------------------------------------------------------------------------------------ watchlists

_WL_SQL = """SELECT w.watchlist_id, w.name, w.description, w.created_at, w.updated_at,
                    (SELECT count(*) FROM watchlist_items i WHERE i.watchlist_id = w.watchlist_id)::INTEGER AS item_count
             FROM watchlists w"""


def list_watchlists() -> list[dict[str, Any]]:
    return read(_WL_SQL + " ORDER BY w.created_at")


def get_watchlist(watchlist_id: str) -> dict[str, Any] | None:
    r = read(_WL_SQL + " WHERE w.watchlist_id = ?", [watchlist_id])
    return r[0] if r else None


def create_watchlist(name: str, description: str | None) -> dict[str, Any]:
    wid, ts = new_id("W"), now()
    transact(lambda con: con.execute("INSERT INTO watchlists VALUES (?, ?, ?, ?, ?)", [wid, name, description, ts, ts]))
    return get_watchlist(wid)  # type: ignore[return-value]


def update_watchlist(watchlist_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    def fn(con: duckdb.DuckDBPyConnection) -> int:
        if not con.execute("SELECT 1 FROM watchlists WHERE watchlist_id = ?", [watchlist_id]).fetchone():
            return 0
        sets, params = [], []
        for k in ("name", "description"):
            if k in fields:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        sets.append("updated_at = ?")
        params.append(now())
        con.execute(f"UPDATE watchlists SET {', '.join(sets)} WHERE watchlist_id = ?", params + [watchlist_id])
        return 1

    return get_watchlist(watchlist_id) if transact(fn) else None


def delete_watchlist(watchlist_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        found = con.execute("SELECT 1 FROM watchlists WHERE watchlist_id = ?", [watchlist_id]).fetchone() is not None
        con.execute("DELETE FROM watchlist_items WHERE watchlist_id = ?", [watchlist_id])
        con.execute("DELETE FROM watchlists WHERE watchlist_id = ?", [watchlist_id])
        return found

    return transact(fn)


def watchlist_items(watchlist_id: str) -> list[dict[str, Any]]:
    return read("SELECT * FROM watchlist_items WHERE watchlist_id = ? ORDER BY added_at", [watchlist_id])


def all_watch_security_ids() -> list[str]:
    return [r["security_id"] for r in read("SELECT DISTINCT security_id FROM watchlist_items")]


def add_item(watchlist_id: str, security_id: str, note: str | None) -> dict[str, Any] | None:
    ts = now()

    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if not con.execute("SELECT 1 FROM watchlists WHERE watchlist_id = ?", [watchlist_id]).fetchone():
            return False
        con.execute("DELETE FROM watchlist_items WHERE watchlist_id = ? AND security_id = ?", [watchlist_id, security_id])
        con.execute("INSERT INTO watchlist_items VALUES (?, ?, ?, ?)", [watchlist_id, security_id, note, ts])
        con.execute("UPDATE watchlists SET updated_at = ? WHERE watchlist_id = ?", [ts, watchlist_id])
        return True

    if not transact(fn):
        return None
    return {"watchlist_id": watchlist_id, "security_id": security_id, "note": note, "added_at": ts}


def remove_item(watchlist_id: str, security_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        n = con.execute("SELECT count(*) FROM watchlist_items WHERE watchlist_id = ? AND security_id = ?",
                        [watchlist_id, security_id]).fetchone()[0]
        con.execute("DELETE FROM watchlist_items WHERE watchlist_id = ? AND security_id = ?", [watchlist_id, security_id])
        con.execute("UPDATE watchlists SET updated_at = ? WHERE watchlist_id = ?", [now(), watchlist_id])
        return n > 0

    return transact(fn)


# ------------------------------------------------------------------------------------------------ alerts


def _alert_out(r: dict[str, Any]) -> dict[str, Any]:
    r = dict(r)
    try:
        r["params"] = json.loads(r["params"]) if isinstance(r.get("params"), str) else (r.get("params") or {})
    except ValueError:
        r["params"] = {}
    return r


def list_alerts() -> list[dict[str, Any]]:
    return [_alert_out(r) for r in read("SELECT * FROM alerts ORDER BY created_at")]


def get_alert(alert_id: str) -> dict[str, Any] | None:
    r = read("SELECT * FROM alerts WHERE alert_id = ?", [alert_id])
    return _alert_out(r[0]) if r else None


def create_alert(security_id: str | None, watchlist_id: str | None, rule_type: str, params: dict[str, Any],
                 cooldown_days: int) -> dict[str, Any]:
    aid, ts = new_id("A"), now()
    transact(lambda con: con.execute(
        "INSERT INTO alerts (alert_id, security_id, watchlist_id, rule_type, params, enabled, cooldown_days, "
        "last_fired_at, created_at) VALUES (?, ?, ?, ?, ?, true, ?, NULL, ?)",
        [aid, security_id, watchlist_id, rule_type, json.dumps(params), cooldown_days, ts]))
    return get_alert(aid)  # type: ignore[return-value]


def update_alert(alert_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if not con.execute("SELECT 1 FROM alerts WHERE alert_id = ?", [alert_id]).fetchone():
            return False
        sets, params = [], []
        if "enabled" in fields:
            sets.append("enabled = ?")
            params.append(bool(fields["enabled"]))
        if "params" in fields:
            sets.append("params = ?")
            params.append(json.dumps(fields["params"]))
        if "cooldown_days" in fields:
            sets.append("cooldown_days = ?")
            params.append(int(fields["cooldown_days"]))
        if sets:
            con.execute(f"UPDATE alerts SET {', '.join(sets)} WHERE alert_id = ?", params + [alert_id])
        return True

    return get_alert(alert_id) if transact(fn) else None


def delete_alert(alert_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        n = con.execute("SELECT count(*) FROM alerts WHERE alert_id = ?", [alert_id]).fetchone()[0]
        con.execute("DELETE FROM alerts WHERE alert_id = ?", [alert_id])
        return n > 0

    return transact(fn)


def list_alert_events(unread_only: bool, limit: int) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alert_events" + (" WHERE NOT read" if unread_only else "") + " ORDER BY fired_at DESC LIMIT ?"
    out = []
    for r in read(sql, [limit]):
        try:
            r["payload"] = json.loads(r["payload"]) if isinstance(r.get("payload"), str) else r.get("payload")
        except ValueError:
            r["payload"] = None
        out.append(r)
    return out


def mark_events_read(event_ids: list[str]) -> int:
    if not event_ids:
        return 0

    def fn(con: duckdb.DuckDBPyConnection) -> int:
        ph = ", ".join("?" for _ in event_ids)
        con.execute(f"UPDATE alert_events SET read = true WHERE event_id IN ({ph})", list(event_ids))
        return len(event_ids)

    return transact(fn)


# ------------------------------------------------------------------------------------------------ model promotions


def promote_model(model_run_id: str, model_type: str, horizon: str | None) -> dict[str, Any]:
    ts = now()

    def fn(con: duckdb.DuckDBPyConnection) -> None:
        con.execute("DELETE FROM model_promotions WHERE model_type = ? AND horizon = ?", [model_type, horizon or ""])
        con.execute("INSERT INTO model_promotions VALUES (?, ?, ?, ?, ?)", [model_run_id, model_type, horizon or "", ts, "api"])

    transact(fn)
    return {"model_run_id": model_run_id, "model_type": model_type, "horizon": horizon, "promoted_at": ts}


def model_promotions() -> list[dict[str, Any]]:
    s = get_settings()
    if not s.app_state_db_path.exists():
        return []
    return read("SELECT * FROM model_promotions")
