"""Saved BQL queries (app_state.duckdb table bql_saved; contracts/schemas/bql_saved.yaml).

Same access pattern as app.api.app_state (open briefly, one short statement, close); the table is created / migrated
from its contract on first use, so the shared app_state module is not edited."""

from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime
from typing import Any

import duckdb

from app.core.config import get_settings
from app.core.storage import connect_app_state, ensure_duckdb_tables

TABLE = "bql_saved"
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ensured: set[str] = set()
_lock = threading.Lock()
COLS = ("query_id", "name", "description", "query", "kind", "tags", "created_at", "updated_at", "last_run_at")


def _connect() -> duckdb.DuckDBPyConnection:
    s = get_settings()
    con = connect_app_state(read_only=False, attempts=25, delay=0.1, settings=s)
    key = str(s.app_state_db_path)
    if key not in _ensured:
        with _lock:
            ensure_duckdb_tables(con, (TABLE,))
            _ensured.add(key)
    con.execute("SET TimeZone = 'UTC'")
    return con


def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    con = _connect()
    try:
        cur = con.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        return [{**dict(zip(cols, r, strict=True))} for r in cur.fetchall()]
    finally:
        con.close()


def _out(r: dict[str, Any]) -> dict[str, Any]:
    return {k: (list(r[k]) if k == "tags" and r.get(k) is not None else r.get(k)) for k in COLS}


def list_saved(kind: str | None = None) -> list[dict[str, Any]]:
    if kind:
        return [_out(r) for r in _rows(f"SELECT * FROM {TABLE} WHERE kind = ? ORDER BY lower(name)", [kind])]
    return [_out(r) for r in _rows(f"SELECT * FROM {TABLE} ORDER BY lower(name)")]


def get_saved(query_id: str) -> dict[str, Any] | None:
    r = _rows(f"SELECT * FROM {TABLE} WHERE query_id = ?", [query_id])
    return _out(r[0]) if r else None


def find_by_name(name: str, kind: str | None = None) -> dict[str, Any] | None:
    sql = f"SELECT * FROM {TABLE} WHERE (lower(name) = lower(?) OR query_id = ?)"
    params: list[Any] = [name, name]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    r = _rows(sql + " ORDER BY updated_at DESC LIMIT 1", params)
    return _out(r[0]) if r else None


def name_taken(name: str, exclude_id: str | None = None) -> bool:
    r = _rows(f"SELECT query_id FROM {TABLE} WHERE lower(name) = lower(?)", [name])
    return any(x["query_id"] != exclude_id for x in r)


def create_saved(name: str, query: str, kind: str = "query", description: str | None = None,
                 tags: list[str] | None = None) -> dict[str, Any]:
    qid = "Q" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "".join(secrets.choice(_B32) for _ in range(6))
    now = datetime.now(UTC)
    con = _connect()
    try:
        con.execute(f"INSERT INTO {TABLE} (query_id, name, description, query, kind, tags, created_at, updated_at, "
                    "last_run_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    [qid, name, description, query, kind, tags or [], now, now])
    finally:
        con.close()
    return get_saved(qid)  # type: ignore[return-value]


def update_saved(query_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    if get_saved(query_id) is None:
        return None
    sets, params = [], []
    for k in ("name", "description", "query", "kind", "tags"):
        if k in fields and fields[k] is not None:
            sets.append(f"{k} = ?")
            params.append(fields[k])
    sets.append("updated_at = ?")
    params.append(datetime.now(UTC))
    con = _connect()
    try:
        con.execute(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE query_id = ?", params + [query_id])
    finally:
        con.close()
    return get_saved(query_id)


def touch_run(query_id: str) -> None:
    con = _connect()
    try:
        con.execute(f"UPDATE {TABLE} SET last_run_at = ? WHERE query_id = ?", [datetime.now(UTC), query_id])
    finally:
        con.close()


def delete_saved(query_id: str) -> bool:
    con = _connect()
    try:
        n = con.execute(f"SELECT count(*) FROM {TABLE} WHERE query_id = ?", [query_id]).fetchone()[0]
        con.execute(f"DELETE FROM {TABLE} WHERE query_id = ?", [query_id])
    finally:
        con.close()
    return bool(n)
