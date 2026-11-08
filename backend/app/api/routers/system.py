"""System & job status (contracts/api.yaml tag: system).

getSystemStatus, listJobs, listJobRuns, triggerJob, listDqResults. Job bookkeeping (job_runs, dq_results) is read live
from the ops tables (small) rather than from the snapshot, so the System page reflects the worker in real time.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import UTC, date, datetime
from typing import Any

import polars as pl
from fastapi import APIRouter, Body, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import __version__
from app.api.deps import FastRoute, Db
from app.api.routers._common import conflict, iso, not_found
from app.api.status_cache import BackgroundCache
from app.core.config import get_settings
from app.core.jobqueue import JobNotFound, enqueue_job, job_is_queued, job_is_running
from app.core.storage import read_table

router = APIRouter(tags=["system"], route_class=FastRoute)

_TTL: dict[str, tuple[float, Any]] = {}
_TTL_LOCK = threading.Lock()


def _cached(key: str, ttl: float, fn: Any) -> Any:
    now = time.monotonic()
    with _TTL_LOCK:
        hit = _TTL.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _TTL_LOCK:
        _TTL[key] = (now, val)
    return val


def _job_runs() -> pl.DataFrame:
    s = get_settings()

    def load() -> pl.DataFrame:
        try:
            return read_table("job_runs", settings=s)
        except Exception:
            from app.core.schema import empty_frame

            return empty_frame("job_runs")

    return _cached(f"job_runs:{s.data_dir}", 3.0, load)


def _registry() -> Any:
    from app.scheduler.runner import JobRegistry

    s = get_settings()
    return _cached(f"registry:{s.contracts_dir}", 60.0, lambda: JobRegistry.load(settings=s))


_RUN_FIELDS = ("run_id", "job_id", "dag_run_id", "trigger", "status", "attempt", "started_at", "finished_at",
               "duration_s", "rows_written", "entities_ok", "entities_failed", "error_message")


def _run_out(r: dict[str, Any]) -> dict[str, Any]:
    o = {k: iso(r.get(k)) for k in _RUN_FIELDS}
    o["attempt"] = int(o["attempt"] or 1)
    for k in ("rows_written", "entities_ok", "entities_failed"):
        o[k] = int(o[k]) if o[k] is not None else None
    return o


CADENCE = {"nightly": ("daily", 4), "weekly": ("weekly", 10), "monthly": ("monthly", 40), "manual": ("manual", None)}


def _profile_sla_days() -> int | None:
    """Freshness SLA of the active schedule profile (weekly 9, daily 3), as used by Data Health (guide 15: the System
    page judged the nightly tables on a fixed 4 days even under the weekly profile)."""
    try:
        from app.ops.schedule import active_profile, profile_spec  # noqa: PLC0415

        s = get_settings()
        v = profile_spec(active_profile(s)[0], s).get("sla_days")
        return int(v) if v is not None else None
    except Exception:  # noqa: BLE001 - schedule config unreadable: fixed tolerances
        return None


def _freshness(db: Db) -> list[dict[str, Any]]:
    reg = _registry()
    writers: dict[str, list[Any]] = {}
    for j in reg.jobs.values():
        for t in j.writes:
            writers.setdefault(t, []).append(j)
    runs = _job_runs()
    last_ok: dict[str, dict[str, Any]] = {}
    last_any: dict[str, dict[str, Any]] = {}
    if not runs.is_empty():
        for r in runs.sort("started_at", descending=True).iter_rows(named=True):
            last_any.setdefault(r["job_id"], r)
            if r["status"] in ("success", "partial"):
                last_ok.setdefault(r["job_id"], r)
    try:
        from app.core.dq import dq_summary

        dq_tables = _cached(f"dq:{get_settings().data_dir}", 10.0, lambda: dq_summary().get("tables", {}))
    except Exception:
        dq_tables = {}
    fresh = {(r["table_name"], r["region"]): r for r in db.all("SELECT * FROM _freshness")}
    inputs = db.snap.inputs
    tables = sorted(set(writers) | {t for t, _ in fresh} | set(inputs))
    today = datetime.now(UTC).date()
    out = []
    from app.core.schema import load_contracts
    from app.ops.health import is_optional_table

    contracts = load_contracts()
    sla = _profile_sla_days()
    for t in tables:
        c = contracts.get(t)
        if c is None or not c.is_parquet or t.endswith("_staging"):
            continue
        jobs = writers.get(t, [])
        dag = next((j.dag for j in jobs if j.dag != "manual"), jobs[0].dag if jobs else "manual")
        cadence, tol = CADENCE.get(dag, ("manual", None))
        if tol is not None and sla is not None:
            tol = max(tol, sla)   # profile-adjusted: never stricter than the refresh profile's SLA
        jr = max((last_ok[j.id] for j in jobs if j.id in last_ok), key=lambda r: r["finished_at"] or r["started_at"],
                 default=None)
        ja = max((last_any[j.id] for j in jobs if j.id in last_any), key=lambda r: r["started_at"], default=None)
        rows = [v for (tn, _), v in fresh.items() if tn == t] or [{"region": None, "last_date": None,
                                                                   "last_ingested_at": None, "row_count": None}]
        for fr in rows:
            last_date = fr["last_date"]
            ing = fr.get("last_ingested_at") or (jr["finished_at"] if jr else None)
            if jr and jr.get("finished_at") and (ing is None or jr["finished_at"] > ing):
                ing = jr["finished_at"]
            ing_d = ing.date() if isinstance(ing, datetime) else None
            # F14: forward-looking tables (calendars, expectations, scheduled events) carry future dates - their
            # cadence is judged on the last ingest / successful run, never on a negative "lag"
            forward = bool(last_date and last_date > today)
            ref = (ing_d or today) if forward else (last_date or ing_d)
            lag = (today - ref).days if ref else None
            n = fr.get("row_count")
            if n is None and t in inputs:
                n = inputs[t][0]
            if not n and inputs.get(t, (0, "empty"))[1] != "ok":
                status = "missing"
            elif tol is None or lag is None:
                status = "fresh"
            else:
                status = "fresh" if lag <= tol else "stale"
            # F14: the writer's latest run failed after the last success -> the table is not being refreshed
            if status == "fresh" and _last_run_failed(ja, jr):
                status = "stale"
            out.append({
                "table": t, "last_date": iso(last_date), "last_ingested_at": iso(ing), "lag_days": lag,
                "row_count": int(n) if n is not None else None, "status": status, "expected_cadence": cadence,
                "region": fr.get("region"), "job_id": jobs[0].id if jobs else None,
                "last_status": ja["status"] if ja else None, "dq_status": dq_tables.get(t),
                "optional": is_optional_table(t),
            })
    return out


def _last_run_failed(last_any: dict[str, Any] | None, last_ok: dict[str, Any] | None) -> bool:
    """True when the most recent run of the table's writer failed and no success came after it (F14)."""
    if not last_any or last_any.get("status") != "failed":
        return False
    if not last_ok:
        return True
    a, b = last_any.get("started_at"), last_ok.get("finished_at") or last_ok.get("started_at")
    return bool(a and b and a > b)


def _dag_runs() -> list[dict[str, Any]]:
    runs = _job_runs()
    if runs.is_empty():
        return []
    df = runs.filter(pl.col("dag_run_id").is_not_null())
    out = []
    for dag in ("nightly", "weekly", "monthly", "manual"):
        sub = df.filter(pl.col("dag_run_id").str.ends_with(f"-{dag}"))
        if sub.is_empty():
            continue
        last = sub.sort("started_at", descending=True)["dag_run_id"][0]
        g = sub.filter(pl.col("dag_run_id") == last).sort("started_at", descending=True).unique("job_id", keep="first")
        st = g["status"].to_list()
        status = ("running" if "running" in st else "failed" if "failed" in st else
                  "partial" if "partial" in st else "success" if "success" in st else "skipped")
        out.append({"dag": dag, "dag_run_id": last, "status": status, "started_at": iso(g["started_at"].min()),
                    "finished_at": None if status == "running" else iso(g["finished_at"].max()),
                    "jobs_total": g.height, "jobs_failed": int(sum(1 for x in st if x == "failed"))})
    return out


def _disk_usage_gb() -> float | None:
    """Walking data/ can take seconds on a large lake: computed in a background thread, cached 5 min (None until ready)."""
    key = f"du:{get_settings().data_dir}"
    hit = _TTL.get(key)
    if hit and time.monotonic() - hit[0] < 300:
        return hit[1]
    if not _DU_RUNNING.is_set():
        _DU_RUNNING.set()

        def du() -> None:
            total = 0
            try:
                for dirpath, _, files in os.walk(get_settings().data_dir):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
                with _TTL_LOCK:
                    _TTL[key] = (time.monotonic(), round(total / 1024**3, 3))
            finally:
                _DU_RUNNING.clear()

        threading.Thread(target=du, name="du", daemon=True).start()
    return hit[1] if hit else None


_DU_RUNNING = threading.Event()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _status_payload() -> tuple[tuple[dict[str, Any], list[str], dict[str, str]], str | None]:
    """compute() of the status cache (background thread): (payload, section errors, section as-of), tagged with the
    snapshot run id it was computed on."""
    from app.api.deps import current_snapshot  # noqa: PLC0415

    db = Db(current_snapshot())
    try:
        return _compute_status(db), db.snap.run_id
    finally:
        db.close()


#: sections whose value is spread into the payload (fallback = these keys of the previous payload)
_OPS_KEYS = {"scheduler": None, "data_as_of": {}, "data_as_of_overall": None, "serving_sections": [], "ops_alerts": []}
_RUNTIME_KEYS = {"pauses": [], "heavy_slot": None, "admission_waits": []}


def _section_reuse_s() -> float:
    """How long a failing section may keep serving its last good value (BIG360_STATUS_SECTION_MAX_REUSE_S, default
    600 s); past that it is served empty (with the error) so stale freshness / alerts never pass for current."""
    return _env_float("BIG360_STATUS_SECTION_MAX_REUSE_S", 600.0)


def _compute_status(db: Db) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    """The heavy part of GET /system/status: snapshot aggregates, job_runs, dq, job registry, state files. Each section
    degrades on its own - to its last good value while that is younger than _section_reuse_s(), else an empty
    value - and is listed in the errors; `section_as_of` keeps when each section's served value was computed (a
    reused value carries its original time). Coverage v3 A7: one invalid contracts/jobs.yaml used to fail the whole
    response with HTTP 500; the review fix keeps the fallback from freezing a broken section indefinitely."""
    s = get_settings()
    snap = db.snap
    prev = _STATUS.peek(_status_key())
    prev_v: dict[str, Any] = prev.value[0] if prev is not None else {}
    prev_at: dict[str, str] = (prev.value[2] if prev is not None and len(prev.value) > 2 else {}) or {}
    errors: list[str] = []
    sec_at: dict[str, str] = {}
    now = datetime.now(UTC)
    max_reuse = _section_reuse_s()

    def reusable(name: str) -> bool:
        at = prev_at.get(name)
        if not at:
            return False
        try:
            return (now - datetime.fromisoformat(at)).total_seconds() <= max_reuse
        except ValueError:
            return False

    def section(name: str, fn: Any, default: Any, spread: dict[str, Any] | None = None) -> Any:
        """`spread`: the section returns several top-level keys (with these defaults) instead of one value."""
        try:
            v = fn()
            sec_at[name] = now.isoformat()
            return v
        except Exception as e:  # noqa: BLE001 - the System page must always render
            errors.append(f"{name}: {type(e).__name__}: {e}"[:300])
            if reusable(name):
                sec_at[name] = prev_at[name]   # the reused value keeps its original computation time
                if spread is not None:
                    return {k: prev_v.get(k, d) for k, d in spread.items()}
                return prev_v.get(name, default)
            return dict(spread) if spread is not None else default

    def counts() -> dict[str, int]:
        c = db.one("""SELECT count(*)::INT AS securities, count(DISTINCT issuer_id)::INT AS issuers,
                              count(*) FILTER (u_mvp_us)::INT AS mvp_us, count(*) FILTER (u_mvp_global)::INT AS mvp_global,
                              count(*) FILTER (u_full)::INT AS full, count(*) FILTER (close IS NOT NULL)::INT AS with_prices,
                              count(*) FILTER (fund_period_end IS NOT NULL)::INT AS with_fundamentals,
                              count(*) FILTER (c_balanced IS NOT NULL)::INT AS with_composite FROM sec""") or {}
        return {k: int(v or 0) for k, v in c.items()}

    try:
        from app.scheduler.worker import read_heartbeat

        hb = read_heartbeat(s)
    except Exception:
        hb = None
    try:
        from app.core.dq import dq_summary

        dq = _cached(f"dqs:{s.data_dir}", 10.0, dq_summary)
        dq_out = {k: dq.get(k) for k in ("run_id", "checked_at", "counts", "blocking")}
    except Exception:
        dq_out = None
    out = {
        **_snapshot_identity(snap),
        "data_freshness": section("data_freshness", lambda: _freshness(db), []),
        "last_dag_runs": section("last_dag_runs", _dag_runs, []),
        "keys_present": s.keys_present(),  # booleans only — never key values
        "universe_counts": section("universe_counts", counts, {}),
        "disk_usage_gb": _disk_usage_gb(),
        "worker": None if hb is None else {
            "alive": bool(hb.get("alive")), "heartbeat_at": hb.get("heartbeat_at"), "pid": hb.get("pid"),
            "active": hb.get("active") or [], "next_runs": hb.get("next_runs") or {}},
        "dq": dq_out,
        "supervisor": _supervisor(),
        # scheduler / data as-of / serving sections / ops alerts: one section (a broken job registry used to blank the
        # alerts with HTTP 200 - the tray then re-toasted every alert on the next good poll)
        **section("ops_status", lambda: _ops_status(db, hb), {}, spread=_OPS_KEYS),
        # coverage v3 A2 / A4: pause flags (reason, expiry), the >= 7 GB heavy slot and the admission waits
        **section("runtime", lambda: _runtime_status(s), {}, spread=_RUNTIME_KEYS),
    }
    return out, errors, sec_at


def _snapshot_identity(snap: Any) -> dict[str, Any]:
    """Cheap (no query): version + the serving snapshot's id / build time / as-of."""
    return {"version": __version__, "serving_run_id": None if snap.is_empty else snap.run_id,
            "serving_built_at": iso(snap.built_at), "serving_as_of": None if snap.is_empty else iso(snap.as_of)}


def _fallback_status(snap: Any) -> dict[str, Any]:
    """Cold cache past the bounded wait (or the first computation failed): contract shape, nothing heavy read."""
    return {**_snapshot_identity(snap), "data_freshness": [], "last_dag_runs": [],
            "keys_present": get_settings().keys_present(), "universe_counts": {}, "disk_usage_gb": None, "worker": None,
            "dq": None, "supervisor": None, "scheduler": None, "data_as_of": {}, "data_as_of_overall": None,
            "serving_sections": [], "ops_alerts": []}


def _status_key() -> str:
    return str(get_settings().data_dir)


#: coverage v3 A7: /system/status is computed off the request path (app.api.status_cache). BIG360_STATUS_TTL_S
#: (default 10 s; <= 0 = compute inline on every request), BIG360_STATUS_COLD_WAIT_S (default 10 s: how long the very
#: first request of the process waits for the first payload before it gets the light "warming" payload).
_STATUS = BackgroundCache("system-status", _status_payload)


def reset_status_cache() -> None:
    """Tests / after a data-dir switch: drop the cached /system/status payload."""
    _STATUS.reset()


@router.get("/system/status", operation_id="getSystemStatus")
def get_system_status() -> dict[str, Any]:
    """Served from the background status cache: never blocks on the heavy reads (only a cold cache waits, bounded).
    `as_of` = when the payload was computed; `cache` = its age / state / last refresh error. The snapshot identity
    fields (serving_run_id, serving_built_at, serving_as_of) are always the live snapshot's; `cache.snapshot_run_id`
    says which snapshot the rest was computed on (a snapshot switch triggers an immediate background refresh)."""
    from app.api.deps import peek_snapshot  # noqa: PLC0415

    _STATUS.ttl_s = _env_float("BIG360_STATUS_TTL_S", 10.0)
    _STATUS.cold_wait_s = _env_float("BIG360_STATUS_COLD_WAIT_S", 10.0)
    snap = peek_snapshot()
    e, meta = _STATUS.get(_status_key(), tag=snap.run_id)
    payload, errors, sec_at = (e.value if e is not None else (_fallback_status(snap), [], {}))
    out = dict(payload)
    out.update(_snapshot_identity(snap))
    out["as_of"] = meta["computed_at"]
    # review fix: a payload with failed sections is 'partial' (not 'fresh'): those sections are their last good value
    # (section_as_of says from when) or empty - clients must not read them as current (tray: no alert diffing)
    state = "partial" if errors and meta["state"] in ("fresh", "stale") else meta["state"]
    out["cache"] = {**meta, "state": state, "section_errors": errors, "section_as_of": sec_at}
    return out


def _runtime_status(s: Any) -> dict[str, Any]:
    from app.ops.pause import runtime_status  # noqa: PLC0415

    return runtime_status(s)


def _supervisor() -> dict[str, Any] | None:
    """Incident 2026-10-24: supervisor restart counts / reasons, restart alerts, reaper metrics."""
    try:
        from app.ops.health import supervisor_summary

        return supervisor_summary(get_settings())
    except Exception:  # noqa: BLE001 - the System page must always render
        return None


def _ops_status(db: Db, hb: dict[str, Any] | None) -> dict[str, Any]:
    """v1.28 (F01 / F03 / F13): scheduler on/off + profile + next / last run per DAG, per-region data as-of of the
    snapshot, per-section staleness and ops alerts. RAISES when a required part fails (job registry, scheduler, ops
    alerts): the caller's section("ops_status") then serves the last good block and reports the error. Swallowing it
    returned scheduler None and no alerts with HTTP 200, which read as "all clear" (review A7_A8)."""
    from app.ops.health import ops_alerts, overall_as_of, scheduler_status, snapshot_region_as_of

    out: dict[str, Any] = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in _OPS_KEYS.items()}
    s = get_settings()
    snap = db.snap
    regions = {} if snap.is_empty else snapshot_region_as_of(db.all, snap.as_of)
    out["data_as_of"] = regions
    out["data_as_of_overall"] = overall_as_of(regions) or (None if snap.is_empty else iso(snap.as_of))
    try:
        out["serving_sections"] = [
            {"section": r["section"], "group": r["grp"], "data_as_of": iso(r["data_as_of"]), "status": r["status"],
             "source_job": r["source_job"], "last_job_status": r["last_job_status"], "detail": r["detail"]}
            for r in db.all("SELECT * FROM serving_sections ORDER BY grp DESC, section")]
    except Exception:  # noqa: BLE001
        out["serving_sections"] = []  # snapshot built before O1
    sch = _cached(f"sched:{s.data_dir}", 5.0, lambda: scheduler_status(s, hb, _registry(), _job_runs()))
    out["scheduler"] = sch
    from app.ops.gaps import coverage_status
    from app.ops.health import read_sync_status, supervisor_summary
    from app.ops.paths import ops_state_dir, read_json

    # alert hygiene 2026-10-30: dataset alerts follow the sync's reason codes; refresh failures are per job
    # (latest attempt across nightly / weekly / sync / manual runs)
    out["ops_alerts"] = ops_alerts(sch, coverage_status(s), read_json(ops_state_dir(s) / "filler.json", {}) or {},
                                   _dag_runs(), None, supervisor=supervisor_summary(s),
                                   sync=read_sync_status(s), job_runs=_job_runs(), registry=_registry())
    try:  # v1.30 (O2.3): regression guard failed / warned on its latest run (stale results age out, v1.43)
        from app.qa.common import qa_alert

        qa = qa_alert(s)
        if qa and not any(a.get("id") == "qa_regression" for a in out["ops_alerts"]):
            out["ops_alerts"].append(qa)
    except Exception:  # noqa: BLE001
        pass
    try:  # 2026-10-31: ref_universe blanked > 1 % of the issuers' sectors in its latest run
        from app.ops.health import sector_loss_alert

        sl = sector_loss_alert(s)
        if sl:
            out["ops_alerts"].append(sl)
    except Exception:  # noqa: BLE001
        pass
    return out


@router.get("/system/jobs", operation_id="listJobs")
def list_jobs() -> list[dict[str, Any]]:
    from app.scheduler.runner import job_definitions

    defs = _cached(f"jobdefs:{get_settings().data_dir}", 3.0, job_definitions)
    out = []
    for d in defs:
        o = dict(d)
        if o.get("last_run"):
            o["last_run"] = _run_out(o["last_run"])
        out.append(o)
    return out


@router.get("/system/jobs/runs", operation_id="listJobRuns")
def list_job_runs(job_id: str | None = None,
                  status: str | None = Query(None, pattern="^(running|success|partial|failed|skipped)$"),
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    df = _job_runs()
    if job_id:
        df = df.filter(pl.col("job_id") == job_id)
    if status:
        df = df.filter(pl.col("status") == status)
    df = df.sort("started_at", descending=True)
    page = df.slice(offset, limit)
    return {"total": df.height, "limit": limit, "offset": offset, "items": [_run_out(r) for r in page.iter_rows(named=True)]}


class JobTriggerRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    include_downstream: bool = False


def trigger(job_id: str, params: dict[str, Any], include_downstream: bool = False) -> JSONResponse:
    if job_is_running(job_id):
        raise conflict(f"job {job_id} is already running")
    if job_is_queued(job_id) and not params:
        raise conflict(f"job {job_id} is already queued")
    try:
        req = enqueue_job(job_id, params=params, include_downstream=include_downstream)
    except JobNotFound as e:
        raise not_found(f"unknown job {job_id!r}") from e
    return JSONResponse({"run_id": req["run_id"], "job_id": job_id}, status_code=202)


@router.post("/system/jobs/{job_id}/trigger", operation_id="triggerJob", status_code=202)
def trigger_job(job_id: str, body: JobTriggerRequest | None = Body(None)) -> JSONResponse:
    b = body or JobTriggerRequest()
    return trigger(job_id, b.params, b.include_downstream)


@router.get("/system/dq", operation_id="listDqResults")
def list_dq_results(table_name: str | None = None, failed_only: bool = True,
                    limit: int = Query(50, ge=1, le=500)) -> list[dict[str, Any]]:
    try:
        df = _cached(f"dqres:{get_settings().data_dir}", 5.0, lambda: read_table("dq_results"))
    except Exception:
        return []
    if df.is_empty():
        return []
    latest = df.sort("checked_at", descending=True)["run_id"][0]
    df = df.filter(pl.col("run_id") == latest)
    if table_name:
        df = df.filter(pl.col("table_name") == table_name)
    if failed_only:
        df = df.filter(~pl.col("passed"))
    order = {"error": 0, "warn": 1, "info": 2}
    rows = sorted(df.iter_rows(named=True), key=lambda r: (order.get(r["severity"], 3), r["table_name"], r["check_id"]))
    return [{"run_id": r["run_id"], "table_name": r["table_name"], "partition_key": r["partition_key"],
             "check_id": r["check_id"], "severity": r["severity"], "passed": bool(r["passed"]),
             "metric_value": r["metric_value"], "threshold": r["threshold"], "checked_at": iso(r["checked_at"])}
            for r in rows[:limit]]


def _today() -> date:
    return datetime.now(UTC).date()
