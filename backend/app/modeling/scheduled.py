"""`studio_scheduled` job (weekly DAG, owner: model-studio): re-run studies flagged schedule=weekly|monthly, compare
with the previous completed run and append a `studio_drift` alert event when metrics or coefficients degrade; then
(Studio v2 S3) monitor Model Registry entries with monitor=weekly|monthly: re-run the promoted spec on the current
snapshot and append a studio_drift event when a feature / prediction PSI or the expanding OOS metric breaches.

Like the platform's alerts_eval, this worker job writes app_state (study_runs rows, studies.last_scheduled_at and
alert_events) through short, retried transactions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

HIGHER_BETTER = ("r2", "adj_r2", "r2_within", "pseudo_r2", "auc", "oos_auc", "oos_r2", "oos_r2_oos", "silhouette",
                 "avg_r2", "oos_rank_ic", "accuracy")
LOWER_BETTER = ("oos_rmse", "oos_mae", "rmse_in_sample", "brier", "oos_brier", "oos_logloss", "oos_relative_rmse")


def detect_drift(old: dict[str, Any], new: dict[str, Any], threshold: float = 0.2) -> dict[str, Any]:
    """Compare two result envelopes. Metrics: relative degradation > threshold in the bad direction.
    Coefficients: |delta estimate| > 3 x max(se_old, se_new) for terms present in both."""
    mo, mn = (old.get("result") or {}).get("metrics") or {}, (new.get("result") or {}).get("metrics") or {}
    issues, changes = [], []
    for k in set(mo) & set(mn):
        a, b = mo[k], mn[k]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or isinstance(a, bool):
            continue
        rel = (b - a) / abs(a) if a else 0.0
        changes.append({"metric": k, "old": a, "new": b, "rel_change": rel})
        if (k in HIGHER_BETTER and rel < -threshold) or (k in LOWER_BETTER and rel > threshold):
            issues.append(f"{k} {a:.4g} -> {b:.4g} ({rel:+.0%})")
    co = {c["term"]: c for c in (old.get("result") or {}).get("coefficients") or []}
    coef_shifts = []
    for c in (new.get("result") or {}).get("coefficients") or []:
        o = co.get(c["term"])
        if not o or o.get("estimate") is None or c.get("estimate") is None:
            continue
        se = max(o.get("std_error") or 0, c.get("std_error") or 0)
        if se and abs(c["estimate"] - o["estimate"]) > 3 * se:
            coef_shifts.append({"term": c["term"], "old": o["estimate"], "new": c["estimate"]})
    if coef_shifts:
        issues.append("coefficient shifts > 3 SE: " + ", ".join(x["term"] for x in coef_shifts[:8]))
    return {"degraded": bool(issues), "issues": issues, "metric_changes": changes, "coef_shifts": coef_shifts}


def run(ctx: Any) -> Any:
    from app.modeling import engine, store
    from app.modeling.registry import StudySpec
    from app.scheduler.base import JobResult

    s = ctx.settings
    done = failed = alerts = 0
    msgs = []
    for st in store.due_studies(datetime.now(UTC)):
        ctx.check_cancelled()
        try:
            spec = StudySpec.model_validate(st["study"])
            prep = engine.prepare(spec, "async", st["study_id"], s)
            out = engine.execute(prep["run_id"], s, external_check=ctx.check_cancelled)
            store.upsert_run(out, spec.model_dump(mode="json"))
            store.update_study(st["study_id"], {"last_scheduled_at": datetime.now(UTC)})
            if out["status"] != "completed":
                failed += 1
                msgs.append(f"{st['name']}: {out.get('error')}")
                continue
            done += 1
            prev = store.previous_completed_run(st["study_id"], prep["run_id"])
            if prev is None:
                continue
            drift = detect_drift(engine.read_result(prev["run_id"]) or {}, engine.read_result(prep["run_id"]) or {},
                                 float(st.get("drift_threshold") or 0.2))
            if drift["degraded"] and st.get("alert_id"):
                store.add_alert_event(st["alert_id"], f"Study '{st['name']}' drifted: " + "; ".join(drift["issues"]),
                                      {"study_id": st["study_id"], "run_id": prep["run_id"], "previous_run_id": prev["run_id"],
                                       **drift}, date.today())
                alerts += 1
        except Exception as e:  # noqa: BLE001 - one study must not stop the others
            failed += 1
            msgs.append(f"{st.get('name')}: {type(e).__name__}: {e}")
    mon = mon_alerts = 0
    try:  # Studio v2 S3: Model Registry monitoring (re-run on the current snapshot, PSI / expanding OOS metric)
        from app.modeling import model_registry as MR

        due = MR.due_models()
    except Exception as e:  # noqa: BLE001 - registry table missing on an old app_state
        due, msgs = [], msgs + [f"registry: {type(e).__name__}: {e}"]
    for mid in due:
        ctx.check_cancelled()
        try:
            m = MR.get_model(mid)
            spec = StudySpec.model_validate(MR.monitoring_spec(m["spec"]))
            prep = engine.prepare(spec, "async", None, s)
            out = engine.execute(prep["run_id"], s, external_check=ctx.check_cancelled)
            store.upsert_run(out, spec.model_dump(mode="json"))
            if out["status"] != "completed":
                failed += 1
                msgs.append(f"model {m['name']} v{m['version']}: {out.get('error')}")
                continue
            r = MR.monitor(mid, prep["run_id"])
            mon += 1
            mon_alerts += int(bool(r and r["drift"] and m.get("alert_id")))
        except Exception as e:  # noqa: BLE001 - one model must not stop the others
            failed += 1
            msgs.append(f"model {mid}: {type(e).__name__}: {e}")
    return JobResult(status="partial" if failed else "success", entities_ok=done + mon, entities_failed=failed,
                     message=f"{done} studies re-run, {alerts} drift alerts; {mon} registry models monitored, "
                             f"{mon_alerts} model drift alerts" + (f"; failures: {msgs[:5]}" if msgs else ""),
                     retryable=False)
