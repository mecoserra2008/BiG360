"""Data Ops endpoints (contracts/api.yaml tag system, v1.28): schedule profiles and the Data Health page.

getSchedule (GET /system/schedule), setSchedule (PUT /system/schedule), getDataHealth (GET /system/data-health);
v1.41 startup sync: getSyncStatus (GET /system/sync), requestSync (POST /system/sync).
State is read live from small ops files (data/state/schedule.json, data/state/ops/*.json, ops/data_gaps, job_runs),
never from the snapshot; /api/v1/system is excluded from the response cache.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from pydantic import BaseModel

from app.api.deps import FastRoute
from app.api.routers._common import invalid
from app.api.routers.system import _cached, _dag_runs, _job_runs, _registry
from app.core.config import get_settings

router = APIRouter(tags=["system"], route_class=FastRoute)


def _heartbeat() -> dict[str, Any] | None:
    try:
        from app.scheduler.worker import read_heartbeat

        return read_heartbeat(get_settings())
    except Exception:
        return None


def schedule_payload() -> dict[str, Any]:
    from app.ops.schedule import completed_dag_runs, schedule_summary

    s = get_settings()
    runs = _job_runs()
    comp = completed_dag_runs(runs) if not runs.is_empty() else {}
    return schedule_summary(s, _heartbeat(), _registry(), comp)


@router.get("/system/schedule", operation_id="getSchedule")
def get_schedule() -> dict[str, Any]:
    return schedule_payload()


class ScheduleUpdate(BaseModel):
    profile: str


@router.put("/system/schedule", operation_id="setSchedule")
def set_schedule(body: ScheduleUpdate = Body(...)) -> dict[str, Any]:
    from app.ops.schedule import ScheduleError, set_active_profile

    try:
        set_active_profile(body.profile, get_settings(), by="api")
    except ScheduleError as e:
        raise invalid(str(e)) from e
    return schedule_payload()


@router.get("/system/data-health", operation_id="getDataHealth")
def get_data_health() -> dict[str, Any]:
    from app.ops.health import data_health

    s = get_settings()

    def build() -> dict[str, Any]:
        out = data_health(s, _job_runs(), _heartbeat(), _registry(), _dag_runs())
        # v1.30 (O2.3): regression-guard summary + its ops alert
        qa = qa_payload(False)
        out["qa"] = qa
        if qa.get("alert") and not any(a.get("id") == "qa_regression" for a in out.get("alerts") or []):
            out.setdefault("alerts", []).append(qa["alert"])
        return out

    return _cached(f"datahealth:{s.data_dir}", 5.0, build)


def qa_payload(detail: bool = False) -> dict[str, Any]:
    """Latest regression-guard summary (data/state/ops/qa_regression.json); status pending when it never ran."""
    try:
        from app.qa.common import qa_alert, read_state

        st = read_state(get_settings())
    except Exception:  # noqa: BLE001 - the System page must always render
        st, qa_alert = None, None  # type: ignore[assignment]
    if not st:
        return {"run_id": None, "status": "pending", "suites": {}, "issues": [], "alert": None,
                "message": "regression guard has not run yet (job qa_regression or python -m app.qa)"}
    out = {k: st.get(k) for k in ("run_id", "status", "started_at", "finished_at", "api_base_url", "client", "message",
                                  "report", "suites", "issues", "trigger", "run_status", "skipped")}
    # v1.43 (alert hygiene 2026-10-30): per-suite run / age / stale / superseded_by; the merged status counts a stale
    # failure as warn; the alert ages out (app.qa.common.qa_alert)
    from app.qa.common import merged_status, suite_views, utc_now  # noqa: PLC0415

    now = utc_now()
    vs = suite_views(st, now)
    out["suites"] = {k: {kk: vv for kk, vv in v.items() if kk != "expired"} for k, v in vs.items()}
    if vs:
        out["status"] = merged_status(vs)
    fin = st.get("finished_at")
    try:
        from datetime import datetime  # noqa: PLC0415

        out["age_h"] = round((now - datetime.fromisoformat(str(fin))).total_seconds() / 3600, 1) if fin else None
    except (TypeError, ValueError):
        out["age_h"] = None
    out["stale"] = bool(vs) and all(v.get("stale") for v in vs.values())
    out["runs"] = list(st.get("runs") or [])[:10]
    out["issues"] = [{"status": "failed", **{k: v for k, v in i.items() if k != "run_id"}} for i in out.get("issues") or []]
    out["alert"] = qa_alert(state=st, now=now) if qa_alert else None
    if detail:
        out["details"] = _qa_details()
    return out


def _qa_details() -> list[dict[str, Any]]:
    import json

    try:
        from app.qa.regression import latest_report_path

        p = latest_report_path(get_settings())
        rep = json.loads(p.read_text(encoding="utf-8")) if p else {}
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for r in (rep.get("api") or {}).get("results") or []:
        if r.get("status") in ("failed", "warn"):
            out.append({"suite": "api", "item": r["operation_id"], "status": r["status"], "url": r.get("url"),
                        "detail": "; ".join(r.get("issues") or [])[:400]})
    for r in (rep.get("golden") or {}).get("results") or []:
        if r.get("status") in ("failed", "warn"):
            out.append({"suite": "golden", "item": r["id"], "status": r["status"], "url": None, "detail": r.get("message")})
    for r in (rep.get("smoke") or {}).get("loads") or []:
        if r.get("status") in ("failed", "warn"):
            item = r["route"] + (f" > {r['tab']}" if r.get("tab") else "") + f" ({r.get('theme')})"
            out.append({"suite": "smoke", "item": item, "status": r["status"], "url": r.get("route"),
                        "detail": "; ".join(r.get("issues") or [])[:400]})
    return out[:500]


@router.get("/system/qa", operation_id="getQaReport")
def get_qa_report(detail: bool = False) -> dict[str, Any]:
    return qa_payload(detail)


# ------------------------------------------------------------------------------------------------ startup sync (v1.41)


class SyncRequest(BaseModel):
    datasets: list[str] | None = None


@router.get("/system/sync", operation_id="getSyncStatus")
def get_sync_status(history: int = 10) -> dict[str, Any]:
    """Startup sync (docs/STARTUP_SYNC_PLAN.md A): the running / last sync - dataset reasons, jobs, publishes,
    peak RAM, header chip - plus the supervisor trigger and recent runs."""
    from app.ops.sync.api import sync_payload

    if history < 0 or history > 100:
        raise invalid("history must be between 0 and 100")
    return sync_payload(get_settings(), history=history)


@router.post("/system/sync", operation_id="requestSync", status_code=202)
def request_sync(body: SyncRequest | None = Body(default=None)) -> dict[str, Any]:
    """Ask for a sync now (the supervisor starts it within a few seconds; a running sync is never doubled)."""
    from app.ops.sync.api import request_payload

    ds = list((body.datasets if body else None) or [])
    if any(not isinstance(x, str) or len(x) > 64 for x in ds) or len(ds) > 100:
        raise invalid("datasets: up to 100 dataset ids")
    return request_payload(ds or None, get_settings())
