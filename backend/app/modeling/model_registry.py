"""Studio v2 S3 Model Registry (owner: model-studio; roadmap v3 item S): promote a run to a named, versioned model with
its spec hash, data snapshot, owner and monitoring baseline; monitor it (expanding OOS metric, feature / prediction
PSI); breaches append events to the model's studio_drift alert. Persistence: app_state.duckdb table model_registry
(contracts/schemas/model_registry.yaml). The API process writes it; the studio_scheduled worker job writes the
monitoring result through short, retried transactions like the study drift alerts."""

from __future__ import annotations

import getpass
import json
from datetime import UTC, date, datetime
from typing import Any

from app.api import app_state

from . import compare as C
from . import engine
from .store import _iso, _j, _read, _transact, _ts, add_alert_event

COLS = ("model_id", "name", "version", "run_id", "method", "spec", "spec_hash", "snapshot_id", "dataset_key", "owner",
        "description", "stage", "metrics", "baseline", "monitor", "psi_threshold", "metric_floor", "alert_id",
        "last_monitor", "last_monitored_at", "created_at", "updated_at")
JSON_COLS = ("spec", "metrics", "baseline", "last_monitor")


class RegistryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def out(r: dict[str, Any], full: bool = True) -> dict[str, Any]:
    o = {k: r.get(k) for k in COLS}
    for k in JSON_COLS:
        o[k] = _j(r.get(k), None)
    for k in ("created_at", "updated_at", "last_monitored_at"):
        o[k] = _iso(r.get(k))
    if not full:
        o.pop("baseline", None)
        o.pop("spec", None)
        lm = o.get("last_monitor") or {}
        o["last_monitor"] = {k: lm.get(k) for k in ("drift", "breaches", "metric", "prediction_psi")} if lm else None
    return o


def list_models(name: str | None = None) -> list[dict[str, Any]]:
    rows = _read("SELECT * FROM model_registry" + (" WHERE name = ?" if name else "") + " ORDER BY name, version DESC",
                 [name] if name else [])
    return [out(r, full=False) for r in rows]


def get_model(model_id: str) -> dict[str, Any] | None:
    r = _read("SELECT * FROM model_registry WHERE model_id = ?", [model_id])
    return out(r[0]) if r else None


def _alert_params(m: dict[str, Any]) -> dict[str, Any]:
    return {"model_id": m["model_id"], "model_name": m["name"], "version": m["version"], "monitor": m["monitor"],
            "psi_threshold": m["psi_threshold"], "metric_floor": m.get("metric_floor"), "source": "model_registry"}


def promote(run_id: str, name: str | None = None, owner: str | None = None, description: str | None = None,
            stage: str = "candidate", monitor: str = "none", psi_threshold: float = C.PSI_ALERT,
            metric_floor: float | None = None) -> dict[str, Any]:
    """Register a completed run as the next version of `name` (default: the run name / method)."""
    st = engine.read_status(run_id)
    env = engine.read_result(run_id)
    if st is None or env is None:
        raise RegistryError("not_found", f"run {run_id} not found")
    if st.get("status") != "completed" or not env.get("result"):
        raise RegistryError("not_completed", f"run {run_id} is {st.get('status')}: only completed runs can be promoted")
    spec = env.get("study") or {}
    nm = (name or spec.get("name") or env.get("method") or "model").strip()[:200]
    prev = _read("SELECT max(version) AS v FROM model_registry WHERE name = ?", [nm])
    ver = int((prev[0]["v"] if prev and prev[0]["v"] is not None else 0) + 1)
    rep = env.get("reproducibility") or {}
    now = app_state.now()
    m = {"model_id": app_state.new_id("MR"), "name": nm, "version": ver, "run_id": run_id, "method": env.get("method") or "",
         "spec": spec, "spec_hash": rep.get("spec_hash") or st.get("spec_hash") or "", "snapshot_id": rep.get("snapshot_id"),
         "dataset_key": rep.get("dataset_key"), "owner": (owner or _owner()).strip()[:120], "description": description,
         "stage": stage, "metrics": {k: v for k, v in ((env["result"].get("metrics") or {}).items())
                                     if isinstance(v, (int, float)) and not isinstance(v, bool)},
         "baseline": C.baseline(run_id), "monitor": monitor, "psi_threshold": float(psi_threshold),
         "metric_floor": metric_floor, "alert_id": None, "last_monitor": None, "last_monitored_at": None,
         "created_at": now, "updated_at": now}
    if monitor != "none":
        m["alert_id"] = app_state.create_alert(None, None, "studio_drift", _alert_params(m), 0)["alert_id"]
    if stage == "production":
        _demote_others(nm)
    _insert(m)
    return get_model(m["model_id"])  # type: ignore[return-value]


def _owner() -> str:
    try:
        return getpass.getuser() or "local"
    except Exception:  # noqa: BLE001
        return "local"


def _insert(m: dict[str, Any]) -> None:
    vals = [json.dumps(m[c], default=str) if c in JSON_COLS and m[c] is not None else m[c] for c in COLS]
    _transact(lambda con: con.execute(f"INSERT INTO model_registry ({', '.join(COLS)}) VALUES ({', '.join('?' for _ in COLS)})", vals))


def _demote_others(name: str) -> None:
    _transact(lambda con: con.execute("UPDATE model_registry SET stage = 'candidate', updated_at = ? WHERE name = ? AND stage = 'production'",
                                      [app_state.now(), name]))


def update_model(model_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    cur = get_model(model_id)
    if cur is None:
        return None
    f = {k: v for k, v in fields.items() if k in ("stage", "description", "owner", "monitor", "psi_threshold", "metric_floor")}
    if f.get("stage") == "production":
        _demote_others(cur["name"])
    nxt = {**cur, **f}
    if "monitor" in f or "psi_threshold" in f or "metric_floor" in f:
        if nxt["monitor"] == "none":
            if cur.get("alert_id"):
                app_state.delete_alert(cur["alert_id"])
            f["alert_id"] = None
        elif cur.get("alert_id") and app_state.get_alert(cur["alert_id"]):
            app_state.update_alert(cur["alert_id"], {"params": _alert_params(nxt), "enabled": True})
        else:
            f["alert_id"] = app_state.create_alert(None, None, "studio_drift", _alert_params(nxt), 0)["alert_id"]
    if f:
        f["updated_at"] = app_state.now()
        sets = ", ".join(f"{k} = ?" for k in f)
        _transact(lambda con: con.execute(f"UPDATE model_registry SET {sets} WHERE model_id = ?", list(f.values()) + [model_id]))
    return get_model(model_id)


def delete_model(model_id: str) -> bool:
    cur = get_model(model_id)
    if cur is None:
        return False
    if cur.get("alert_id"):
        app_state.delete_alert(cur["alert_id"])
    _transact(lambda con: con.execute("DELETE FROM model_registry WHERE model_id = ?", [model_id]))
    return True


def monitor(model_id: str, run_id: str | None = None, alert: bool = True) -> dict[str, Any] | None:
    """Evaluate the model on run_id (default: its promoted run) against the baseline; store the result; append a
    studio_drift alert event on a breach when the model has an alert."""
    m = get_model(model_id)
    if m is None:
        return None
    res = C.evaluate(m["baseline"] or {}, run_id or m["run_id"], m["psi_threshold"], m.get("metric_floor"))
    now = app_state.now()
    _transact(lambda con: con.execute("UPDATE model_registry SET last_monitor = ?, last_monitored_at = ?, updated_at = ? WHERE model_id = ?",
                                      [json.dumps(res, default=str), now, now, model_id]))
    if alert and res["drift"] and m.get("alert_id"):
        add_alert_event(m["alert_id"], f"Model '{m['name']}' v{m['version']} drifted: " + "; ".join(res["breaches"][:6]),
                        {"model_id": model_id, "run_id": res["run_id"], "breaches": res["breaches"],
                         "metric": res["metric"], "prediction_psi": res["prediction_psi"]}, date.today())
    return {**res, "model_id": model_id}


def due_models(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(UTC)
    outl = []
    for r in _read("SELECT model_id, monitor, last_monitored_at FROM model_registry WHERE monitor <> 'none' AND stage <> 'archived'"):
        last = _ts(r.get("last_monitored_at"))
        gap = 6 if r["monitor"] == "weekly" else 27
        if last is None or (now - last).total_seconds() >= gap * 86400 - 3600:
            outl.append(r["model_id"])
    return outl


def monitoring_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """The promoted StudySpec with an open end date, so a scheduled re-run sees the data that arrived since."""
    s = json.loads(json.dumps(spec))
    ds = s.get("dataset") or {}
    if ds.get("end"):
        ds["end"] = None
    s["name"] = (s.get("name") or "model") + " [monitor]"
    return s
