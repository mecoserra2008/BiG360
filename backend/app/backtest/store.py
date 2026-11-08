"""Backtesting lab persistence in data/db/app_state.duckdb: `backtest_strategies`, `backtest_runs` (owner: backtest).

The API process is the only writer (plus the weekly backtest_scheduled job, through short retried transactions like
studio_scheduled). Run lifecycle comes from data/backtest/runs/<run_id>/status.json; `sync_run()` mirrors it into
backtest_runs and, on completion, folds the run into its strategy's multiple-testing counter.
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

from . import runs
from .spec import BacktestSpec

TABLES = ("backtest_strategies", "backtest_runs")
MAX_TRIAL_SHARPES = 400
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


# ------------------------------------------------------------------------------------------------ strategies


def _strategy_out(r: dict[str, Any]) -> dict[str, Any]:
    return {"strategy_id": r["strategy_id"], "name": r["name"], "description": r.get("description"), "mode": r["mode"],
            "tags": _j(r.get("tags"), []) or [], "origin": _j(r.get("origin"), None), "last_run_id": r.get("last_run_id"),
            "spec": _j(r["spec"], {}), "schedule": r.get("schedule") or "none", "drift_threshold": r.get("drift_threshold"),
            "last_scheduled_at": _iso(r.get("last_scheduled_at")), "alert_id": r.get("alert_id"),
            "n_trials": int(r.get("n_trials") or 0), "n_oos_views": int(r.get("n_oos_views") or 0),
            "created_at": _iso(r["created_at"]), "updated_at": _iso(r["updated_at"])}


def list_strategies() -> list[dict[str, Any]]:
    return [_strategy_out(r) for r in _read("SELECT * FROM backtest_strategies ORDER BY updated_at DESC")]


def get_strategy(strategy_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM backtest_strategies WHERE strategy_id = ?", [strategy_id])
    return _strategy_out(r[0]) if r else None


def create_strategy(name: str, spec: BacktestSpec, description: str | None = None, tags: list[str] | None = None,
                    origin: dict[str, Any] | None = None) -> dict[str, Any]:
    sid, ts = app_state.new_id("BS"), app_state.now()
    _transact(lambda con: con.execute(
        "INSERT INTO backtest_strategies (strategy_id, name, description, spec, mode, tags, origin, last_run_id, schedule, "
        "drift_threshold, n_trials, trials, n_oos_views, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'none', NULL, 0, '[]', 0, ?, ?)",
        [sid, name, description, json.dumps(spec.normalized()), spec.mode, json.dumps(tags or []),
         json.dumps(origin) if origin else None, ts, ts]))
    return get_strategy(sid)  # type: ignore[return-value]


def update_strategy(strategy_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        if not con.execute("SELECT 1 FROM backtest_strategies WHERE strategy_id = ?", [strategy_id]).fetchone():
            return False
        sets, params = [], []
        for k in ("name", "description"):
            if k in fields:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        if "tags" in fields:
            sets.append("tags = ?")
            params.append(json.dumps(fields["tags"] or []))
        if fields.get("spec") is not None:
            sp: BacktestSpec = fields["spec"]
            sets += ["spec = ?", "mode = ?"]
            params += [json.dumps(sp.normalized()), sp.mode]
        for k in ("last_run_id", "schedule", "drift_threshold", "last_scheduled_at", "alert_id"):
            if k in fields:
                sets.append(f"{k} = ?")
                params.append(fields[k])
        sets.append("updated_at = ?")
        params.append(app_state.now())
        con.execute(f"UPDATE backtest_strategies SET {', '.join(sets)} WHERE strategy_id = ?", params + [strategy_id])
        return True

    return get_strategy(strategy_id) if _transact(fn) else None


def delete_strategy(strategy_id: str) -> bool:
    st = get_strategy(strategy_id)
    if st and st.get("alert_id"):
        app_state.delete_alert(st["alert_id"])

    def fn(con: duckdb.DuckDBPyConnection) -> bool:
        found = con.execute("SELECT 1 FROM backtest_strategies WHERE strategy_id = ?", [strategy_id]).fetchone() is not None
        con.execute("UPDATE backtest_runs SET strategy_id = NULL WHERE strategy_id = ?", [strategy_id])
        con.execute("DELETE FROM backtest_strategies WHERE strategy_id = ?", [strategy_id])
        return found

    return _transact(fn)


def set_schedule(strategy_id: str, schedule: str, drift_threshold: float | None) -> dict[str, Any] | None:
    """Flag a strategy as a weekly paper strategy; creates its backtest_drift alert rule on first use."""
    st = get_strategy(strategy_id)
    if st is None:
        return None
    fields: dict[str, Any] = {"schedule": schedule}
    if drift_threshold is not None:
        fields["drift_threshold"] = drift_threshold
    thr = drift_threshold if drift_threshold is not None else (st.get("drift_threshold") or 0.3)
    params = {"strategy_id": strategy_id, "strategy_name": st["name"], "schedule": schedule, "drift_threshold": thr}
    if schedule == "none":
        if st.get("alert_id"):
            app_state.delete_alert(st["alert_id"])
        fields["alert_id"] = None
    elif st.get("alert_id") and app_state.get_alert(st["alert_id"]):
        app_state.update_alert(st["alert_id"], {"params": params, "enabled": True})
    else:
        fields["alert_id"] = app_state.create_alert(None, None, "backtest_drift", params, 0)["alert_id"]
    return update_strategy(strategy_id, fields)


def due_strategies(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    out = []
    for st in list_strategies():
        if (st.get("schedule") or "none") != "weekly":
            continue
        last = _ts(st.get("last_scheduled_at"))
        if last is None or (now - last).total_seconds() >= 6 * 86400 - 3600:
            out.append(st)
    return out


# ------------------------------------------------------------------------------------------------ multiple testing


def prior_trials(strategy_id: str | None, spec_hash: str) -> dict[str, Any]:
    """Trials recorded for the strategy under OTHER spec hashes: {n_trials, sharpes} (input of the deflated Sharpe)."""
    if not strategy_id:
        return {"n_trials": 0, "sharpes": []}
    r = _read("SELECT trials FROM backtest_strategies WHERE strategy_id = ?", [strategy_id])
    trials = _j(r[0]["trials"], []) if r else []
    other = [t for t in trials if t.get("hash") != spec_hash]
    return {"n_trials": int(sum(int(t.get("n") or 1) for t in other)),
            "sharpes": [x for t in other for x in (t.get("sharpes") or []) if isinstance(x, (int, float))]}


def record_trial(strategy_id: str, spec_hash: str, n: int, sharpes: list[float], oos_viewed: bool) -> None:
    def fn(con: duckdb.DuckDBPyConnection) -> None:
        r = con.execute("SELECT trials, n_oos_views FROM backtest_strategies WHERE strategy_id = ?", [strategy_id]).fetchone()
        if not r:
            return
        trials = [t for t in _j(r[0], []) if t.get("hash") != spec_hash]
        trials.append({"hash": spec_hash, "n": max(1, int(n)), "sharpes": [float(x) for x in sharpes][:64]})
        while sum(len(t["sharpes"]) for t in trials) > MAX_TRIAL_SHARPES and len(trials) > 1:
            trials[0]["sharpes"] = []  # keep the count, drop the oldest Sharpe samples
            if all(not t["sharpes"] for t in trials[:-1]):
                break
        n_total = sum(int(t["n"]) for t in trials)
        con.execute("UPDATE backtest_strategies SET trials = ?, n_trials = ?, n_oos_views = ?, updated_at = ? "
                    "WHERE strategy_id = ?", [json.dumps(trials), n_total, int(r[1] or 0) + (1 if oos_viewed else 0),
                                              app_state.now(), strategy_id])

    _transact(fn)


# ------------------------------------------------------------------------------------------------ runs

_RUN_COLS = ("run_id", "strategy_id", "name", "bt_mode", "status", "mode", "spec", "spec_hash", "plan_key", "snapshot_id",
             "seed", "metrics", "warnings_count", "n_trials", "scheduled", "error", "error_code", "estimate_s",
             "created_at", "started_at", "finished_at", "duration_s")


def upsert_run(st: dict[str, Any], spec: dict[str, Any] | None = None) -> None:
    if spec is None:
        doc = runs.read_spec(st["run_id"]) or {}
        spec = doc.get("spec") or {}
    row = {"run_id": st["run_id"], "strategy_id": st.get("strategy_id"), "name": st.get("name"),
           "bt_mode": st.get("bt_mode") or "rank", "status": st.get("status"), "mode": st.get("mode") or "async",
           "spec": json.dumps(spec), "spec_hash": st.get("spec_hash") or "", "plan_key": st.get("plan_key"),
           "snapshot_id": st.get("snapshot_id"), "seed": int(st.get("seed") or 0), "metrics": json.dumps(st.get("metrics") or {}),
           "warnings_count": st.get("warnings_count"), "n_trials": st.get("n_trials"), "scheduled": bool(st.get("scheduled")),
           "error": st.get("error"), "error_code": st.get("error_code"), "estimate_s": st.get("estimate_s"),
           "created_at": _ts(st.get("created_at")) or app_state.now(), "started_at": _ts(st.get("started_at")),
           "finished_at": _ts(st.get("finished_at")), "duration_s": st.get("duration_s")}

    def fn(con: duckdb.DuckDBPyConnection) -> None:
        con.execute("DELETE FROM backtest_runs WHERE run_id = ?", [row["run_id"]])
        con.execute(f"INSERT INTO backtest_runs ({', '.join(_RUN_COLS)}) VALUES ({', '.join('?' for _ in _RUN_COLS)})",
                    [row[c] for c in _RUN_COLS])
        if row["strategy_id"]:
            con.execute("UPDATE backtest_strategies SET last_run_id = ?, updated_at = ? WHERE strategy_id = ?",
                        [row["run_id"], app_state.now(), row["strategy_id"]])

    _transact(fn)
    if row["strategy_id"] and row["status"] == "completed" and not st.get("_trial_recorded"):
        record_trial(row["strategy_id"], row["spec_hash"], _grid_size(spec), list(st.get("trial_sharpes") or []),
                     bool(st.get("has_oos")))


def _grid_size(spec: dict[str, Any]) -> int:
    n = 1
    for v in ((spec.get("robustness") or {}).get("sweep") or {}).values():
        n *= max(1, len(v))
    return n


def _live(st: dict[str, Any]) -> dict[str, Any]:
    from app.modeling.lane import live_fields

    return live_fields(st)


def run_out(st: dict[str, Any]) -> dict[str, Any]:
    """Public BacktestRunStatus shape from a status.json dict or a backtest_runs row."""
    return {"run_id": st["run_id"], "strategy_id": st.get("strategy_id"), "name": st.get("name"),
            "bt_mode": st.get("bt_mode") or "rank", "status": st.get("status"), "mode": st.get("mode") or "async",
            "created_at": _iso(st.get("created_at")), "started_at": _iso(st.get("started_at")),
            "finished_at": _iso(st.get("finished_at")), "duration_s": st.get("duration_s"),
            "estimate_s": st.get("estimate_s"), "sync_budget_s": runs.SYNC_BUDGET_S, "error": st.get("error"),
            "error_code": st.get("error_code"), "spec_hash": st.get("spec_hash"), "plan_key": st.get("plan_key"),
            "snapshot_id": st.get("snapshot_id"), "seed": st.get("seed"), "metrics": _j(st.get("metrics"), {}) or {},
            "warnings_count": st.get("warnings_count"), "n_trials": st.get("n_trials"),
            "scheduled": bool(st.get("scheduled")), **_live(st)}


def sync_run(run_id: str) -> dict[str, Any] | None:
    """status.json -> backtest_runs (when changed); re-enqueue lost async runs. Returns the status dict."""
    st = runs.read_status(run_id)
    if st is None:
        r = _read("SELECT * FROM backtest_runs WHERE run_id = ?", [run_id])
        return r[0] if r else None
    st = runs.requeue_if_stale(st)
    cur = _read("SELECT status, finished_at FROM backtest_runs WHERE run_id = ?", [run_id])
    if not cur or cur[0]["status"] != st.get("status") or (st.get("finished_at") and cur[0]["finished_at"] is None):
        try:
            upsert_run(st)
        except duckdb.Error:  # app_state briefly locked: the next poll syncs
            pass
    return st


def list_runs(strategy_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    sql = "SELECT * FROM backtest_runs" + (" WHERE strategy_id = ?" if strategy_id else "") + " ORDER BY created_at DESC LIMIT ?"
    rows = _read(sql, ([strategy_id] if strategy_id else []) + [limit])
    out = []
    for r in rows:
        if r["status"] in ("queued", "running"):
            out.append(run_out(sync_run(r["run_id"]) or r))
        else:
            out.append(run_out(r))
    return out


def previous_completed_run(strategy_id: str, exclude: str) -> dict[str, Any] | None:
    r = _read("SELECT run_id FROM backtest_runs WHERE strategy_id = ? AND status = 'completed' AND run_id <> ? "
              "ORDER BY finished_at DESC NULLS LAST LIMIT 1", [strategy_id, exclude])
    return r[0] if r else None


def add_alert_event(alert_id: str, message: str, payload: dict[str, Any], as_of: Any) -> None:
    eid = app_state.new_id("E")
    _transact(lambda con: con.execute(
        "INSERT INTO alert_events (event_id, alert_id, security_id, fired_at, as_of_date, message, payload, read) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, false)", [eid, alert_id, "", app_state.now(), as_of, message,
                                                json.dumps(payload, default=str)]))
    _transact(lambda con: con.execute("UPDATE alerts SET last_fired_at = ? WHERE alert_id = ?", [app_state.now(), alert_id]))


COMPARE_SERIES = 600


def compare(run_ids: list[str]) -> dict[str, Any]:
    """Side-by-side metrics + NAV curves (downsampled) + robustness headline for several completed runs."""
    out_runs, metrics, curves = [], {}, []
    for rid in run_ids:
        st = sync_run(rid)
        if st is None:
            continue
        env = runs.read_result(rid) or {}
        res = env.get("result") or {}
        out_runs.append(run_out(st))
        for k, v in (res.get("flat_metrics") or {}).items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                metrics.setdefault(k, {})[rid] = v
        ser = res.get("series") or {}
        d, nav = ser.get("dates") or [], ser.get("nav") or []
        step = max(1, len(d) // COMPARE_SERIES)
        curves.append({"run_id": rid, "name": st.get("name"), "dates": d[::step], "nav": nav[::step]})
    ids = [r["run_id"] for r in out_runs]
    rows = [{"metric": k, "values": {rid: v.get(rid) for rid in ids}} for k, v in sorted(metrics.items())]
    return {"run_ids": ids, "runs": out_runs, "metrics": rows, "curves": curves}
