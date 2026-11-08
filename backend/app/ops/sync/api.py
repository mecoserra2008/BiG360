"""Payloads of GET / POST /api/v1/system/sync (contracts/api.yaml SyncStatus, SyncRequestAccepted).

Read live from small state files (never the snapshot): data/state/ops/sync_status.json (the running / last sync),
sync_history.jsonl, sync_now.json (a pending request) and the supervisor's trigger block in
data/logs/app/supervisor.json. The header chip text is computed here so every client shows the same words.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings, get_settings
from app.ops.paths import app_logs_dir, read_json
from app.ops.sync import state as S

SKIP_REASONS = ("not_yet_published", "source_unavailable", "needs_key", "failed")


def _fmt_date(v: Any) -> str:
    return str(v)[:10] if v else "-"


def chip(st: dict[str, Any]) -> dict[str, Any]:
    """Header chip: running -> "Syncing data... 12/48"; done -> "Data up to date as of ..." or "3 skipped"."""
    state = st.get("state") or "never"
    pr = st.get("progress") or {}
    link = "/system/data-health#sync"
    if state in ("running", "scanning", "publishing"):
        if state == "scanning":
            return {"text": "Checking data...", "tone": "info", "link": link, "running": True}
        txt = "Publishing data..." if state == "publishing" else \
            f"Syncing data... {pr.get('jobs_done', 0)}/{pr.get('jobs_total', 0)}"
        return {"text": txt, "tone": "info", "link": link, "running": True}
    if state == "never":
        return {"text": None, "tone": None, "link": link, "running": False}
    counts = st.get("counts") or {}
    skipped = sum(int(counts.get(k, 0) or 0) for k in SKIP_REASONS)
    asof = (st.get("data_as_of") or {}).get("overall")
    if skipped:
        tone = "down" if int(counts.get("failed", 0) or 0) else "warn"
        return {"text": f"{skipped} skipped", "tone": tone, "link": link, "running": False,
                "title": f"Data as of {_fmt_date(asof)}; {skipped} dataset(s) not brought up to date (reasons on Data "
                         "health > Data sync)"}
    return {"text": f"Data up to date as of {_fmt_date(asof)}", "tone": "ok", "link": link, "running": False}


def _runs_by_dataset(jobs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for j in jobs:
        if j.get("status") in ("pending", None):
            continue
        if j.get("status") == "skipped" and j.get("trigger") == "derive":
            continue
        for d in j.get("datasets") or []:
            out.setdefault(d, []).append({"job": j.get("job"), "status": j.get("status"), "reason": j.get("reason"),
                                          "duration_s": j.get("duration_s"), "pass_no": j.get("pass_no")})
    return out


def sync_payload(settings: Settings | None = None, history: int = 10) -> dict[str, Any]:
    s = settings or get_settings()
    st = S.read_status(s) or {}
    sup = read_json(app_logs_dir(s) / "supervisor.json", {}) or {}
    trig = sup.get("sync") if isinstance(sup.get("sync"), dict) else None
    if trig is not None:
        hb = S.parse_ts(sup.get("heartbeat_at"))
        trig = dict(trig, supervisor_alive=bool(hb and (datetime.now(UTC) - hb).total_seconds() < 120))
    jobs = list(st.get("jobs") or [])
    runs = _runs_by_dataset(jobs)
    datasets = []
    for d in st.get("datasets") or []:
        datasets.append({**{k: d.get(k) for k in ("dataset", "label", "group", "kind", "reason", "data_as_of",
                                                  "expected_as_of", "next_expected", "detail", "keys", "retry_at",
                                                  "attempts", "jobs")},
                         "entities": [{k: e.get(k) for k in ("entity", "reason", "data_as_of", "expected_as_of",
                                                             "next_expected", "detail", "missing", "keys", "retry_at",
                                                             "attempts", "secondary")}
                                      for e in d.get("entities") or []],
                         "runs": runs.get(d.get("dataset"), [])})
    out = {
        "state": st.get("state") or "never", "run_id": st.get("run_id"), "trigger": st.get("trigger"),
        "phase": st.get("phase"), "result": st.get("result"), "started_at": st.get("started_at"),
        "heartbeat_at": st.get("heartbeat_at"), "finished_at": st.get("finished_at"), "seconds": st.get("seconds"),
        "as_of": st.get("as_of"), "data_as_of": st.get("data_as_of") or {}, "progress": st.get("progress") or {},
        "counts": st.get("counts") or {}, "datasets": datasets, "jobs": jobs, "publish": st.get("publish") or [],
        "ram": st.get("ram") or {}, "notes": st.get("notes") or [], "detail": st.get("detail"),
        "failed_jobs": st.get("failed_jobs") or [], "scheduler": trig, "request": S.pending_request(s),
        # coverage v3 A3: what the sync deferred (carried to the next one) and why, its budget, admission waits
        "deferred": st.get("deferred") or [], "caps": st.get("caps") or {}, "carried_in": st.get("carried_in") or [],
        "admission_waits": st.get("admission_waits") or [], "suspended_s": st.get("suspended_s"),
        "history": [{**{k: h.get(k) for k in ("run_id", "trigger", "result", "started_at", "finished_at", "seconds",
                                               "as_of", "counts", "failed_jobs")},
                     "deferred": len(h.get("deferred") or []), "cap_hit": ((h.get("caps") or {}).get("hit") or {}).get("reason")}
                    for h in S.read_history(s, history)],
    }
    out["chip"] = chip(out)
    return out


def request_payload(datasets: list[str] | None = None, settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    running = S.sync_running(s)
    sup = read_json(app_logs_dir(s) / "supervisor.json", {}) or {}
    hb = S.parse_ts(sup.get("heartbeat_at"))
    alive = bool(hb and (datetime.now(UTC) - hb).total_seconds() < 120)
    req = S.request_sync(s, by="api", datasets=datasets)
    if running:
        msg = "a sync is running; the request starts another one when it ends"
    elif alive and isinstance(sup.get("sync"), dict) and (sup["sync"] or {}).get("enabled"):
        msg = "sync requested: the supervisor starts it within a few seconds"
    else:
        msg = ("sync requested: no supervisor with the sync trigger is running (start BiG360, or run "
               "`python -m app.ops.sync run` in backend/)")
    return {"requested": True, "request": req, "running": running, "supervisor": alive, "message": msg}
