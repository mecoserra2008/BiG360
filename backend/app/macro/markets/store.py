"""Saved macro scenarios (app_state.duckdb table macro_scenarios; contracts/schemas/macro_scenarios.yaml).

Same access pattern as app.api.app_state (open briefly, one transaction, close) but kept in the macro-w4 package so the
shared app_state module is not edited; the table is created / migrated from its contract on first use."""

from __future__ import annotations

import json
import secrets
import threading
from datetime import UTC, datetime
from typing import Any

import duckdb

from app.core.config import get_settings
from app.core.storage import connect_app_state, ensure_duckdb_tables

TABLE = "macro_scenarios"
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ensured: set[str] = set()
_lock = threading.Lock()


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


def _out(r: tuple[Any, ...], cols: list[str]) -> dict[str, Any]:
    d = dict(zip(cols, r, strict=True))
    for k in ("shocks", "options"):
        v = d.get(k)
        d[k] = json.loads(v) if isinstance(v, str) else (v or ({} if k == "options" else {}))
    d["preset"] = False
    return d


def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    con = _connect()
    try:
        cur = con.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        return [_out(r, cols) for r in cur.fetchall()]
    finally:
        con.close()


def list_scenarios() -> list[dict[str, Any]]:
    return _rows(f"SELECT * FROM {TABLE} ORDER BY created_at")


def get_scenario(scenario_id: str) -> dict[str, Any] | None:
    r = _rows(f"SELECT * FROM {TABLE} WHERE scenario_id = ?", [scenario_id])
    return r[0] if r else None


def create_scenario(name: str, description: str | None, shocks: dict[str, Any], options: dict[str, Any] | None
                    ) -> dict[str, Any]:
    sid = "X" + datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "".join(secrets.choice(_B32) for _ in range(6))
    now = datetime.now(UTC)
    con = _connect()
    try:
        con.execute(f"INSERT INTO {TABLE} (scenario_id, name, description, shocks, options, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [sid, name, description, json.dumps(shocks), json.dumps(options or {}), now, now])
    finally:
        con.close()
    return get_scenario(sid)  # type: ignore[return-value]


def update_scenario(scenario_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    if get_scenario(scenario_id) is None:
        return None
    sets, params = [], []
    for k in ("name", "description", "shocks", "options"):
        if k in fields:
            sets.append(f"{k} = ?")
            params.append(json.dumps(fields[k]) if k in ("shocks", "options") else fields[k])
    sets.append("updated_at = ?")
    params.append(datetime.now(UTC))
    con = _connect()
    try:
        con.execute(f"UPDATE {TABLE} SET {', '.join(sets)} WHERE scenario_id = ?", params + [scenario_id])
    finally:
        con.close()
    return get_scenario(scenario_id)


def delete_scenario(scenario_id: str) -> bool:
    con = _connect()
    try:
        n = con.execute(f"SELECT count(*) FROM {TABLE} WHERE scenario_id = ?", [scenario_id]).fetchone()[0]
        con.execute(f"DELETE FROM {TABLE} WHERE scenario_id = ?", [scenario_id])
    finally:
        con.close()
    return bool(n)
