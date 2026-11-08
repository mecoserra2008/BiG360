"""Thesis tracker persistence in data/db/app_state.duckdb (owner: theses).

Tables (contracts/schemas): theses, thesis_journal, thesis_reviews, thesis_snapshots, plus the thesis-tagged rows of
`alerts` (column thesis_id, alerts v6) and their `alert_events`. Tables / new columns are created from the contracts on
first use (app.core.storage.ensure_duckdb_tables), like the Studio and backtest stores. Every operation is one short
transaction through app.api.app_state (the API is the main writer; the nightly thesis_eval job writes auto_status,
alert events, journal entries and snapshots the same way).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb

from app.api import app_state
from app.core.config import get_settings
from app.core.storage import ensure_duckdb_tables

TABLES = ("theses", "thesis_journal", "thesis_reviews", "thesis_snapshots", "alerts", "alert_events")
JSON_COLS = ("key_drivers", "catalysts", "kpis", "invalidation", "tags", "origin", "status_reasons")
#: columns a client may set (create / update); the rest are managed here
EDITABLE = ("title", "subject_type", "subject_id", "subject_label", "security_id", "price_id", "price_currency",
            "direction", "horizon", "conviction", "status", "thesis_md", "key_drivers", "catalysts", "kpis", "entry_lo",
            "entry_hi", "target", "stop", "invalidation", "scenario_id", "study_id", "studio_run_id",
            "backtest_strategy_id", "backtest_run_id", "benchmark_id", "sector_benchmark_id", "tags", "origin",
            "review_every_days", "next_review_date", "inception_date", "inception_price", "horizon_date", "opened_at",
            "closed_at", "exit_price", "close_reason", "outcome", "post_mortem_md")
HORIZON_DAYS = {"1M": 30, "3M": 91, "6M": 182, "12M": 365, "24M": 730, "36M+": 1095}
_ensured: set[str] = set()
_lock = threading.Lock()


def _ensure(con: duckdb.DuckDBPyConnection) -> None:
    key = str(get_settings().app_state_db_path)
    if key in _ensured:
        return
    with _lock:
        ensure_duckdb_tables(con, TABLES)
        _ensured.add(key)


def reset_cache() -> None:
    """Tests: forget which app_state files were migrated."""
    _ensured.clear()


def _transact(fn: Any) -> Any:
    def wrapped(con: duckdb.DuckDBPyConnection) -> Any:
        _ensure(con)
        return fn(con)

    return app_state.transact(wrapped)


def _read(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with app_state.connect() as con:
        _ensure(con)
        return app_state.rows(con, sql, params)


def _j(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return str(v)


def _date(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    d = datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _db_value(col: str, v: Any) -> Any:
    if col in JSON_COLS:
        return None if v is None else json.dumps(v, default=str)
    if col in ("next_review_date", "inception_date", "horizon_date"):
        return _date(v)
    if col in ("opened_at", "closed_at"):
        return _ts(v)
    return v


def thesis_out(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    for c in JSON_COLS:
        out[c] = _j(r.get(c), {} if c == "origin" else [])
    for c in ("next_review_date", "inception_date", "horizon_date", "opened_at", "closed_at", "evaluated_at",
              "created_at", "updated_at"):
        out[c] = iso(r.get(c))
    if out.get("origin") == {}:
        out["origin"] = None
    return out


# ------------------------------------------------------------------------------------------------ theses


def list_theses(status: str | None = None, subject_type: str | None = None) -> list[dict[str, Any]]:
    conds, params = [], []
    if status:
        conds.append("status = ?")
        params.append(status)
    if subject_type:
        conds.append("subject_type = ?")
        params.append(subject_type)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    return [thesis_out(r) for r in _read(f"SELECT * FROM theses{where} ORDER BY updated_at DESC", params)]


def get_thesis(thesis_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM theses WHERE thesis_id = ?", [thesis_id])
    return thesis_out(r[0]) if r else None


def create_thesis(fields: dict[str, Any]) -> dict[str, Any]:
    tid, ts = app_state.new_id("TH"), app_state.now()
    cols = ["thesis_id", "created_at", "updated_at"] + [c for c in EDITABLE if c in fields]
    vals = [tid, ts, ts] + [_db_value(c, fields[c]) for c in EDITABLE if c in fields]
    _transact(lambda con: con.execute(
        f"INSERT INTO theses ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals))
    return get_thesis(tid)  # type: ignore[return-value]


def update_thesis(thesis_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if not con.execute("SELECT 1 FROM theses WHERE thesis_id = ?", [thesis_id]).fetchone():
            return False
        sets, params = [], []
        for c in EDITABLE:
            if c in fields:
                sets.append(f"{c} = ?")
                params.append(_db_value(c, fields[c]))
        sets.append("updated_at = ?")
        params.append(app_state.now())
        con.execute(f"UPDATE theses SET {', '.join(sets)} WHERE thesis_id = ?", params + [thesis_id])
        return True

    return get_thesis(thesis_id) if _transact(fn) else None


def set_evaluation(thesis_id: str, auto_status: str, reasons: list[dict[str, Any]], when: datetime | None = None) -> None:
    _transact(lambda con: con.execute(
        "UPDATE theses SET auto_status = ?, status_reasons = ?, evaluated_at = ? WHERE thesis_id = ?",
        [auto_status, json.dumps(reasons, default=str), when or app_state.now(), thesis_id]))


def delete_thesis(thesis_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        found = con.execute("SELECT 1 FROM theses WHERE thesis_id = ?", [thesis_id]).fetchone() is not None
        con.execute("DELETE FROM alert_events WHERE alert_id IN (SELECT alert_id FROM alerts WHERE thesis_id = ?)",
                    [thesis_id])
        con.execute("DELETE FROM alerts WHERE thesis_id = ?", [thesis_id])
        for t in ("thesis_journal", "thesis_reviews", "thesis_snapshots", "theses"):
            con.execute(f"DELETE FROM {t} WHERE thesis_id = ?", [thesis_id])
        return found

    return _transact(fn)


# ------------------------------------------------------------------------------------------------ journal


def _entry_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"entry_id": r["entry_id"], "thesis_id": r["thesis_id"], "entry_date": iso(r["entry_date"]),
            "kind": r["kind"], "body_md": r["body_md"], "links": _j(r.get("links"), []) or [],
            "created_at": iso(r["created_at"]), "updated_at": iso(r["updated_at"])}


def list_journal(thesis_id: str) -> list[dict[str, Any]]:
    return [_entry_out(r) for r in _read("SELECT * FROM thesis_journal WHERE thesis_id = ? "
                                         "ORDER BY entry_date DESC, created_at DESC", [thesis_id])]


def get_entry(entry_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM thesis_journal WHERE entry_id = ?", [entry_id])
    return _entry_out(r[0]) if r else None


def add_entry(thesis_id: str, body_md: str, kind: str = "note", entry_date: Any = None,
              links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    eid, ts = app_state.new_id("TJ"), app_state.now()
    d = _date(entry_date) or ts.date()
    _transact(lambda con: con.execute(
        "INSERT INTO thesis_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [eid, thesis_id, d, kind, body_md, json.dumps(links or [], default=str), ts, ts]))
    return get_entry(eid)  # type: ignore[return-value]


def update_entry(entry_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if not con.execute("SELECT 1 FROM thesis_journal WHERE entry_id = ?", [entry_id]).fetchone():
            return False
        sets, params = ["updated_at = ?"], [app_state.now()]
        if "body_md" in fields:
            sets.append("body_md = ?")
            params.append(fields["body_md"])
        if "entry_date" in fields:
            sets.append("entry_date = ?")
            params.append(_date(fields["entry_date"]))
        if "kind" in fields:
            sets.append("kind = ?")
            params.append(fields["kind"])
        if "links" in fields:
            sets.append("links = ?")
            params.append(json.dumps(fields["links"] or [], default=str))
        con.execute(f"UPDATE thesis_journal SET {', '.join(sets)} WHERE entry_id = ?", params + [entry_id])
        return True

    return get_entry(entry_id) if _transact(fn) else None


def delete_entry(entry_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        n = con.execute("SELECT count(*) FROM thesis_journal WHERE entry_id = ?", [entry_id]).fetchone()[0]
        con.execute("DELETE FROM thesis_journal WHERE entry_id = ?", [entry_id])
        return n > 0

    return _transact(fn)


# ------------------------------------------------------------------------------------------------ reviews


def _review_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"review_id": r["review_id"], "thesis_id": r["thesis_id"], "due_date": iso(r["due_date"]),
            "status": r["status"], "completed_at": iso(r.get("completed_at")), "decision": r.get("decision"),
            "conviction_after": r.get("conviction_after"), "notes_md": r.get("notes_md"),
            "snapshot_id": r.get("snapshot_id"), "created_at": iso(r["created_at"])}


def list_reviews(thesis_id: str) -> list[dict[str, Any]]:
    return [_review_out(r) for r in _read("SELECT * FROM thesis_reviews WHERE thesis_id = ? "
                                          "ORDER BY due_date DESC, created_at DESC", [thesis_id])]


def get_review(review_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM thesis_reviews WHERE review_id = ?", [review_id])
    return _review_out(r[0]) if r else None


def schedule_review(thesis_id: str, due: date | None) -> dict[str, Any] | None:
    """Keep exactly one scheduled review (due) for the thesis; due None removes the pending one."""
    rid, ts = app_state.new_id("TR"), app_state.now()

    def fn(con: duckdb.DuckDBPyConnection) -> str | None:
        con.execute("DELETE FROM thesis_reviews WHERE thesis_id = ? AND status = 'scheduled'", [thesis_id])
        if due is None:
            return None
        con.execute("INSERT INTO thesis_reviews (review_id, thesis_id, due_date, status, created_at) "
                    "VALUES (?, ?, ?, 'scheduled', ?)", [rid, thesis_id, due, ts])
        return rid

    out = _transact(fn)
    return get_review(out) if out else None


def pending_review(thesis_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM thesis_reviews WHERE thesis_id = ? AND status = 'scheduled' ORDER BY due_date LIMIT 1",
              [thesis_id])
    return _review_out(r[0]) if r else None


def complete_review(thesis_id: str, decision: str, notes_md: str | None, conviction_after: int | None,
                    snapshot_id: str | None, review_id: str | None = None) -> dict[str, Any]:
    """Complete the pending review (or record an unscheduled one)."""
    ts = app_state.now()
    pend = get_review(review_id) if review_id else pending_review(thesis_id)
    rid = pend["review_id"] if pend and pend["status"] == "scheduled" else app_state.new_id("TR")

    def fn(con: duckdb.DuckDBPyConnection) -> None:
        if pend and pend["review_id"] == rid:
            con.execute("UPDATE thesis_reviews SET status = 'completed', completed_at = ?, decision = ?, "
                        "conviction_after = ?, notes_md = ?, snapshot_id = ? WHERE review_id = ?",
                        [ts, decision, conviction_after, notes_md, snapshot_id, rid])
        else:
            con.execute("INSERT INTO thesis_reviews VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?)",
                        [rid, thesis_id, ts.date(), ts, decision, conviction_after, notes_md, snapshot_id, ts])

    _transact(fn)
    return get_review(rid)  # type: ignore[return-value]


# ------------------------------------------------------------------------------------------------ snapshots


def _snap_out(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r.get(k) for k in ("snapshot_id", "thesis_id", "kind", "price", "price_usd", "benchmark_price",
                                 "sector_price", "composite_score", "grade", "auto_status")}
    out.update({"taken_at": iso(r["taken_at"]), "as_of": iso(r.get("as_of")), "price_date": iso(r.get("price_date")),
                "kpi_values": _j(r.get("kpi_values"), []) or [], "extra": _j(r.get("extra"), {}) or {}})
    return out


def add_snapshot(thesis_id: str, kind: str, values: dict[str, Any]) -> dict[str, Any]:
    sid, ts = app_state.new_id("TS"), app_state.now()
    _transact(lambda con: con.execute(
        "INSERT INTO thesis_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [sid, thesis_id, kind, ts, _date(values.get("as_of")), _date(values.get("price_date")), values.get("price"),
         values.get("price_usd"), values.get("benchmark_price"), values.get("sector_price"),
         values.get("composite_score"), values.get("grade"), json.dumps(values.get("kpi_values") or [], default=str),
         values.get("auto_status"), json.dumps(values.get("extra") or {}, default=str)]))
    r = _read("SELECT * FROM thesis_snapshots WHERE snapshot_id = ?", [sid])
    return _snap_out(r[0])


def list_snapshots(thesis_id: str) -> list[dict[str, Any]]:
    return [_snap_out(r) for r in _read("SELECT * FROM thesis_snapshots WHERE thesis_id = ? ORDER BY taken_at",
                                        [thesis_id])]


# ------------------------------------------------------------------------------------------------ thesis alerts


def _rule_key(rule_type: str, security_id: str | None, params: dict[str, Any]) -> str:
    return json.dumps([rule_type, security_id or "", params], sort_keys=True, default=str)


def thesis_alerts(thesis_id: str | None = None) -> list[dict[str, Any]]:
    if thesis_id is None:
        rows = _read("SELECT * FROM alerts WHERE thesis_id IS NOT NULL ORDER BY created_at")
    else:
        rows = _read("SELECT * FROM alerts WHERE thesis_id = ? ORDER BY created_at", [thesis_id])
    return [app_state._alert_out(r) for r in rows]


def sync_alerts(thesis_id: str, rules: list[dict[str, Any]], enabled: bool = True) -> dict[str, int]:
    """Replace the thesis-tagged rules by `rules` [{rule_type, security_id, params, cooldown_days}]. Rules whose
    (rule_type, security_id, params) is unchanged keep their alert_id (and fired-event history); others are deleted
    (with their events) or inserted. `enabled` = thesis is not closed."""
    ts = app_state.now()

    def fn(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
        cur = app_state.rows(con, "SELECT alert_id, rule_type, security_id, params FROM alerts WHERE thesis_id = ?",
                             [thesis_id])
        have = {_rule_key(r["rule_type"], r["security_id"], _j(r["params"], {})): r["alert_id"] for r in cur}
        want = {_rule_key(r["rule_type"], r.get("security_id"), r["params"]): r for r in rules}
        gone = [aid for k, aid in have.items() if k not in want]
        if gone:
            ph = ", ".join("?" for _ in gone)
            con.execute(f"DELETE FROM alert_events WHERE alert_id IN ({ph})", gone)
            con.execute(f"DELETE FROM alerts WHERE alert_id IN ({ph})", gone)
        added = 0
        for k, r in want.items():
            if k in have:
                continue
            con.execute("INSERT INTO alerts (alert_id, security_id, watchlist_id, rule_type, params, enabled, "
                        "cooldown_days, last_fired_at, created_at, thesis_id) VALUES (?, ?, NULL, ?, ?, ?, ?, NULL, ?, ?)",
                        [app_state.new_id("A"), r.get("security_id"), r["rule_type"], json.dumps(r["params"], default=str),
                         enabled, int(r.get("cooldown_days", 5)), ts, thesis_id])
            added += 1
        con.execute("UPDATE alerts SET enabled = ? WHERE thesis_id = ?", [enabled, thesis_id])
        return {"kept": len(have) - len(gone), "added": added, "removed": len(gone)}

    return _transact(fn)


def thesis_events(thesis_id: str, limit: int = 500) -> list[dict[str, Any]]:
    rows = _read("""SELECT e.*, a.rule_type FROM alert_events e JOIN alerts a USING (alert_id)
                    WHERE a.thesis_id = ? ORDER BY e.as_of_date DESC, e.fired_at DESC LIMIT ?""", [thesis_id, limit])
    out = []
    for r in rows:
        out.append({"event_id": r["event_id"], "alert_id": r["alert_id"], "rule_type": r["rule_type"],
                    "security_id": r["security_id"], "fired_at": iso(r["fired_at"]), "as_of_date": iso(r["as_of_date"]),
                    "message": r["message"], "payload": _j(r.get("payload"), None), "read": bool(r["read"])})
    return out


def add_alert_event(alert_id: str, security_id: str, as_of: date, message: str, payload: dict[str, Any]) -> bool:
    """Append one event (idempotent per alert_id x security_id x as_of_date); sets alerts.last_fired_at."""
    ts = app_state.now()

    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if con.execute("SELECT 1 FROM alert_events WHERE alert_id = ? AND security_id = ? AND as_of_date = ?",
                       [alert_id, security_id, as_of]).fetchone():
            return False
        con.execute("INSERT INTO alert_events (event_id, alert_id, security_id, fired_at, as_of_date, message, payload, "
                    "read) VALUES (?, ?, ?, ?, ?, ?, ?, false)",
                    [app_state.new_id("E"), alert_id, security_id, ts, as_of, message, json.dumps(payload, default=str)])
        con.execute("UPDATE alerts SET last_fired_at = ? WHERE alert_id = ?", [ts, alert_id])
        return True

    return _transact(fn)


def last_event_dates(alert_ids: list[str]) -> dict[str, date]:
    if not alert_ids:
        return {}
    ph = ", ".join("?" for _ in alert_ids)
    return {r["alert_id"]: _date(r["d"]) for r in _read(  # type: ignore[misc]
        f"SELECT alert_id, max(as_of_date) AS d FROM alert_events WHERE alert_id IN ({ph}) GROUP BY 1", alert_ids)}


def horizon_date(inception: date | None, horizon: str) -> date | None:
    return inception + timedelta(days=HORIZON_DAYS.get(horizon, 365)) if inception else None
