"""Model Studio persistence in data/db/app_state.duckdb: `studies`, `study_runs` (owner: model-studio, API side).

The API process is the only writer. Run lifecycle comes from data/modeling/runs/<run_id>/status.json (written by
the engine in the API for sync runs, by the worker for async runs); `sync_run()` mirrors it into study_runs.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any

import duckdb

from app.api import app_state
from app.core.config import get_settings
from app.core.storage import ensure_duckdb_tables

from . import engine
from .registry import StudySpec

TABLES = ("studies", "study_runs", "peer_sets", "model_registry")  # model_registry: Studio v2 S3
_ensured: set[str] = set()
_lock = threading.Lock()


def _ensure(con: duckdb.DuckDBPyConnection) -> None:
    key = str(get_settings().app_state_db_path)
    if key in _ensured:
        return
    with _lock:
        ensure_duckdb_tables(con, TABLES)
        _ensured.add(key)


def _transact(fn: Any) -> Any:
    def wrapped(con: duckdb.DuckDBPyConnection) -> Any:
        _ensure(con)
        return fn(con)

    return app_state.transact(wrapped)


def _read(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with app_state.connect() as con:
        _ensure(con)
        return app_state.rows(con, sql, params)


def _ts(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return (v if v.tzinfo else v.replace(tzinfo=UTC)).astimezone(UTC).isoformat()
    return str(v)


def _j(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------------------------------------ studies


def _study_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"study_id": r["study_id"], "name": r["name"], "description": r.get("description"),
            "method": r["method"], "tags": _j(r.get("tags"), []) or [], "preset_id": r.get("preset_id"),
            "last_run_id": r.get("last_run_id"), "study": _j(r["spec"], {}),
            "schedule": r.get("schedule") or "none", "drift_threshold": r.get("drift_threshold"),
            "last_scheduled_at": _iso(r.get("last_scheduled_at")), "alert_id": r.get("alert_id"),
            "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])}


def list_studies() -> list[dict[str, Any]]:
    return [_study_out(r) for r in _read("SELECT * FROM studies ORDER BY updated_at DESC")]


def get_study(study_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM studies WHERE study_id = ?", [study_id])
    return _study_out(r[0]) if r else None


def create_study(name: str, study: StudySpec, description: str | None = None, tags: list[str] | None = None,
                 preset_id: str | None = None) -> dict[str, Any]:
    sid, ts = app_state.new_id("ST"), app_state.now()
    _transact(lambda con: con.execute(
        "INSERT INTO studies (study_id, name, description, spec, method, tags, preset_id, last_run_id, created_at, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        [sid, name, description, json.dumps(study.model_dump(mode="json")), study.method, json.dumps(tags or []),
         preset_id, ts, ts]))
    return get_study(sid)  # type: ignore[return-value]


def update_study(study_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if not con.execute("SELECT 1 FROM studies WHERE study_id = ?", [study_id]).fetchone():
            return False
        sets, params = [], []
        for k in ("name", "description"):
            if k in fields:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        if "tags" in fields:
            sets.append("tags = ?")
            params.append(json.dumps(fields["tags"] or []))
        if "study" in fields and fields["study"] is not None:
            st: StudySpec = fields["study"]
            sets += ["spec = ?", "method = ?"]
            params += [json.dumps(st.model_dump(mode="json")), st.method]
        for k in ("last_run_id", "schedule", "drift_threshold", "last_scheduled_at", "alert_id"):
            if k in fields:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        sets.append("updated_at = ?")
        params.append(app_state.now())
        con.execute(f"UPDATE studies SET {', '.join(sets)} WHERE study_id = ?", params + [study_id])
        return True

    return get_study(study_id) if _transact(fn) else None


def set_schedule(study_id: str, schedule: str, drift_threshold: float | None) -> dict[str, Any] | None:
    """Flag a study for scheduled re-runs; creates its studio_drift alert rule on first use."""
    st = get_study(study_id)
    if st is None:
        return None
    fields: dict[str, Any] = {"schedule": schedule}
    if drift_threshold is not None:
        fields["drift_threshold"] = drift_threshold
    thr = drift_threshold if drift_threshold is not None else (st.get("drift_threshold") or 0.2)
    params = {"study_id": study_id, "study_name": st["name"], "schedule": schedule, "drift_threshold": thr}
    if schedule == "none":
        if st.get("alert_id"):
            app_state.delete_alert(st["alert_id"])
        fields["alert_id"] = None
    elif st.get("alert_id") and app_state.get_alert(st["alert_id"]):
        app_state.update_alert(st["alert_id"], {"params": params, "enabled": True})
    else:
        fields["alert_id"] = app_state.create_alert(None, None, "studio_drift", params, 0)["alert_id"]
    return update_study(study_id, fields)


def due_studies(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    out = []
    for st in list_studies():
        sch = st.get("schedule") or "none"
        if sch == "none":
            continue
        last = _ts(st.get("last_scheduled_at"))
        gap = 6 if sch == "weekly" else 27
        if last is None or (now - last).total_seconds() >= gap * 86400 - 3600:
            out.append(st)
    return out


def add_alert_event(alert_id: str, message: str, payload: dict[str, Any], as_of: Any) -> None:
    eid = app_state.new_id("E")
    _transact(lambda con: con.execute(
        "INSERT INTO alert_events (event_id, alert_id, security_id, fired_at, as_of_date, message, payload, read) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, false)", [eid, alert_id, "", app_state.now(), as_of, message, json.dumps(payload, default=str)]))
    _transact(lambda con: con.execute("UPDATE alerts SET last_fired_at = ? WHERE alert_id = ?", [app_state.now(), alert_id]))


def previous_completed_run(study_id: str, exclude: str) -> dict[str, Any] | None:
    r = _read("SELECT run_id FROM study_runs WHERE study_id = ? AND status = 'completed' AND run_id <> ? "
              "ORDER BY finished_at DESC NULLS LAST LIMIT 1", [study_id, exclude])
    return r[0] if r else None


# ------------------------------------------------------------------------------------------------ peer sets


def _ps_out(r: dict[str, Any], with_members: bool = True) -> dict[str, Any]:
    out = {k: r.get(k) for k in ("peer_set_id", "name", "description", "source_run_id", "method", "n_members", "n_clusters")}
    out["created_at"] = _iso(r["created_at"])
    out["members"] = _j(r.get("members"), []) if with_members else []
    return out


def create_peer_set(name: str, description: str | None, run_id: str, method: str, members: list[dict[str, Any]]) -> dict[str, Any]:
    pid = app_state.new_id("PS")
    n_cl = len({m["cluster"] for m in members})
    _transact(lambda con: con.execute(
        "INSERT INTO peer_sets (peer_set_id, name, description, source_run_id, method, members, n_members, n_clusters, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [pid, name, description, run_id, method, json.dumps(members), len(members), n_cl, app_state.now()]))
    return get_peer_set(pid)  # type: ignore[return-value]


def get_peer_set(peer_set_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM peer_sets WHERE peer_set_id = ?", [peer_set_id])
    return _ps_out(r[0]) if r else None


def list_peer_sets() -> list[dict[str, Any]]:
    return [_ps_out(r) for r in _read("SELECT * FROM peer_sets ORDER BY created_at DESC")]


def delete_peer_set(peer_set_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        n = con.execute("SELECT count(*) FROM peer_sets WHERE peer_set_id = ?", [peer_set_id]).fetchone()[0]
        con.execute("DELETE FROM peer_sets WHERE peer_set_id = ?", [peer_set_id])
        return n > 0

    return _transact(fn)


def peer_set_members_for(peer_set_id: str, security_id: str) -> tuple[list[str], Any] | None:
    """Security ids of the security's cluster in the set (whole set if the security is not a member)."""
    ps = get_peer_set(peer_set_id)
    if ps is None:
        return None
    mem = ps["members"]
    cl = next((m["cluster"] for m in mem if m["security_id"] == security_id), None)
    ids = [m["security_id"] for m in mem if cl is None or m["cluster"] == cl]
    return ids, cl


def delete_study(study_id: str) -> bool:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        found = con.execute("SELECT 1 FROM studies WHERE study_id = ?", [study_id]).fetchone() is not None
        con.execute("UPDATE study_runs SET study_id = NULL WHERE study_id = ?", [study_id])
        con.execute("DELETE FROM studies WHERE study_id = ?", [study_id])
        return found

    return _transact(fn)


# ------------------------------------------------------------------------------------------------ runs

_RUN_COLS = ("run_id", "study_id", "name", "method", "status", "mode", "spec", "spec_hash", "dataset_key",
             "snapshot_id", "seed", "metrics", "warnings_count", "error", "error_code", "estimate_s", "title", "n_obs", "created_at",
             "started_at", "finished_at", "duration_s")


def upsert_run(st: dict[str, Any], spec: dict[str, Any] | None = None) -> None:
    if spec is None:
        s = engine.read_spec(st["run_id"])
        spec = s.model_dump(mode="json") if s else {}
    row = {
        "run_id": st["run_id"], "study_id": st.get("study_id"), "name": st.get("name"), "method": st.get("method") or "",
        "status": st.get("status"), "mode": st.get("mode") or "async", "spec": json.dumps(spec),
        "spec_hash": st.get("spec_hash") or "", "dataset_key": st.get("dataset_key"), "snapshot_id": st.get("snapshot_id"),
        "seed": int(st.get("seed") or 0), "metrics": json.dumps(st.get("metrics") or {}),
        "warnings_count": st.get("warnings_count"), "error": st.get("error"), "error_code": st.get("error_code"),
        "estimate_s": st.get("estimate_s"), "title": st.get("title"), "n_obs": st.get("n_obs"), "created_at": _ts(st.get("created_at")) or app_state.now(),
        "started_at": _ts(st.get("started_at")), "finished_at": _ts(st.get("finished_at")),
        "duration_s": st.get("duration_s"),
    }

    def fn(con: duckdb.DuckDBPyConnection) -> None:
        con.execute("DELETE FROM study_runs WHERE run_id = ?", [row["run_id"]])
        con.execute(f"INSERT INTO study_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' for _ in _RUN_COLS)})",
                    [row[c] for c in _RUN_COLS])
        if row["study_id"]:
            con.execute("UPDATE studies SET last_run_id = ?, updated_at = ? WHERE study_id = ?",
                        [row["run_id"], app_state.now(), row["study_id"]])

    _transact(fn)


def _live(st: dict[str, Any]) -> dict[str, Any]:
    from app.modeling.lane import live_fields

    return live_fields(st)


def run_out(st: dict[str, Any]) -> dict[str, Any]:
    """Public StudioRunStatus shape from a status.json dict or a study_runs row."""
    return {"run_id": st["run_id"], "study_id": st.get("study_id"), "name": st.get("name"),
            "method": st.get("method") or "", "status": st.get("status"), "mode": st.get("mode") or "async",
            "created_at": _iso(st.get("created_at")), "started_at": _iso(st.get("started_at")),
            "finished_at": _iso(st.get("finished_at")), "duration_s": st.get("duration_s"),
            "estimate_s": st.get("estimate_s"), "estimated_seconds": st.get("estimate_s"),
            "sync_budget_s": engine.SYNC_BUDGET_S, "title": st.get("title"), "n_obs": st.get("n_obs"),
            "error": st.get("error"), "error_code": st.get("error_code"),
            "spec_hash": st.get("spec_hash"), "dataset_key": st.get("dataset_key"), "snapshot_id": st.get("snapshot_id"),
            "seed": st.get("seed"), "metrics": _j(st.get("metrics"), {}) or {},
            "warnings_count": st.get("warnings_count"), **_live(st)}


def sync_run(run_id: str) -> dict[str, Any] | None:
    """status.json -> study_runs (when changed); re-enqueue lost async runs. Returns the status dict."""
    st = engine.read_status(run_id)
    if st is None:
        r = _read("SELECT * FROM study_runs WHERE run_id = ?", [run_id])
        return r[0] if r else None
    st = engine.requeue_if_stale(st)
    cur = _read("SELECT status, finished_at FROM study_runs WHERE run_id = ?", [run_id])
    if not cur or cur[0]["status"] != st.get("status") or (st.get("finished_at") and cur[0]["finished_at"] is None):
        try:
            upsert_run(st)
        except duckdb.Error:  # app_state briefly locked: the next poll syncs
            pass
    return st


def list_runs(study_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM study_runs" + (" WHERE study_id = ?" if study_id else "") + " ORDER BY created_at DESC LIMIT ?"
    rows = _read(sql, ([study_id] if study_id else []) + [limit])
    out = []
    for r in rows:
        if r["status"] in ("queued", "running"):
            st = sync_run(r["run_id"]) or r
            out.append(run_out(st))
        else:
            out.append(run_out(r))
    return out


def compare(run_ids: list[str]) -> dict[str, Any]:
    runs, metrics, coefs = [], {}, {}
    for rid in run_ids:
        st = sync_run(rid)
        if st is None:
            continue
        env = engine.read_result(rid) or {}
        res = env.get("result") or {}
        ro = run_out(st)
        ro["title"] = ro.get("title") or res.get("title")
        ro["n_obs"] = ro.get("n_obs") or (res.get("summary") or {}).get("n_obs")
        runs.append(ro)
        for k, v in (res.get("metrics") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                metrics.setdefault(k, {})[rid] = v
        for c in res.get("coefficients") or []:
            term = c["term"] + (f" [q={c['quantile']}]" if c.get("quantile") is not None else "")
            coefs.setdefault(term, {})[rid] = {"estimate": c.get("estimate"), "std_error": c.get("std_error"),
                                               "p_value": c.get("p_value")}
    ids = [r["run_id"] for r in runs]
    m_rows = [{"metric": k, "values": {rid: v.get(rid) for rid in ids}} for k, v in sorted(metrics.items())]
    c_rows = [{"term": k, "values": {rid: v.get(rid) for rid in ids}} for k, v in coefs.items()]
    return {"run_ids": ids, "runs": runs, "metrics": m_rows, "coefficients": c_rows}
