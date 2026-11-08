"""Ops health summaries for the API (F01/F03/F13 + O1.5): scheduler state, per-region data as-of, ops alerts and the
Data Health page payload. Everything here is read-only and cheap (small state files, job_runs, data_gaps, a
25k-row query on the published snapshot), so GET /system/status stays fast.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.ops import schedule as sched
from app.ops.paths import ops_state_dir, read_json

#: tables that are optional / realtime: never make the header show "Error" (F13)
OPTIONAL_TABLE_PREFIXES = ("live_",)
OPTIONAL_TABLES = frozenset({"realtime_events"})
#: datasets whose staleness turns the header chip on
CRITICAL_DATASETS = ("prices.US", "prices.EU", "prices.UK", "prices.JP", "fx.rates", "macro.releases")
CORE_REGIONS = ("US", "EU", "UK", "JP")


def is_optional_table(table: str | None) -> bool:
    return bool(table) and (table.startswith(OPTIONAL_TABLE_PREFIXES) or table in OPTIONAL_TABLES)


def _iso(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _aware(t: Any) -> datetime | None:
    if t is None:
        return None
    if isinstance(t, str):
        try:
            t = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(t, date) and not isinstance(t, datetime):
        t = datetime(t.year, t.month, t.day, tzinfo=UTC)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ------------------------------------------------------------------------------------------------ data as-of


def region_as_of(rows: Iterable[tuple[Any, Any, Any]], min_share: float = 0.5) -> dict[str, str]:
    """rows (region, px_date, n securities). A region's data as-of = the latest date such that at least `min_share`
    of its recently priced securities have a bar on or after it (68 early US bars on top of 5,800 for the previous
    session -> the previous session)."""
    by: dict[str, list[tuple[date, int]]] = {}
    for reg, d, n in rows:
        if reg is None or d is None:
            continue
        by.setdefault(str(reg), []).append((d, int(n or 0)))
    out = {}
    for reg, xs in by.items():
        tot = sum(n for _, n in xs)
        if not tot:
            continue
        cum = 0
        for d, n in sorted(xs, reverse=True):
            cum += n
            if cum >= min_share * tot:
                out[reg] = d.isoformat() if isinstance(d, date) else str(d)
                break
    return out


def snapshot_region_as_of(query: Callable[[str], list[dict[str, Any]]], as_of: date | None) -> dict[str, str]:
    """Per-region as-of from the published snapshot (`sec.px_date`); {} when unavailable."""
    if as_of is None:
        return {}
    try:
        rows = query(f"""SELECT region, px_date, count(*) AS n FROM sec
                         WHERE px_date IS NOT NULL AND px_date >= DATE '{(as_of - timedelta(days=30)).isoformat()}'
                         GROUP BY 1, 2""")
    except Exception:
        return {}
    return region_as_of((r["region"], r["px_date"], r["n"]) for r in rows)


def overall_as_of(regions: dict[str, str]) -> str | None:
    """The date the header shows: the most recent core-region as-of (US/EU/UK/JP), else any region."""
    core = [regions[r] for r in CORE_REGIONS if r in regions]
    vals = core or list(regions.values())
    return max(vals) if vals else None


# ------------------------------------------------------------------------------------------------ scheduler


def scheduler_status(settings: Settings | None = None, hb: dict[str, Any] | None = None, registry: Any = None,
                     job_runs: pl.DataFrame | None = None, now: datetime | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    active, source = sched.active_profile(s)
    spec = sched.profile_spec(active, s)
    try:
        entries = sched.profile_entries(active, registry, s)
    except Exception:
        entries = []
    runs = job_runs if job_runs is not None else pl.DataFrame()
    completed = sched.completed_dag_runs(runs) if not runs.is_empty() else {}
    alive = bool(hb and hb.get("alive"))
    enabled = bool(alive and hb.get("scheduler_enabled"))
    computed = sched.next_runs_by_dag(entries, now)
    worker_next = (hb or {}).get("next_runs_by_dag") or {}
    next_runs = worker_next if (enabled and worker_next) else computed
    last_sched: dict[str, str] = {}
    first_seen = None
    if not runs.is_empty():
        first_seen = runs["started_at"].min()
        sch = runs.filter((pl.col("trigger") == "schedule") & pl.col("dag_run_id").is_not_null())
        for r in sch.sort("started_at").iter_rows(named=True):
            dag = str(r["dag_run_id"]).rsplit("-", 1)[-1]
            last_sched[dag] = r["started_at"].isoformat()
    seen = read_json(ops_state_dir(s) / "scheduler_seen.json", {}) or {}
    off_since = None
    if not enabled:
        cands = [_aware(seen.get("last_on_at"))] + [_aware(v) for v in last_sched.values()]
        cands = [c for c in cands if c]
        if cands:
            off_since = max(cands)
        elif hb and not alive:
            off_since = _aware(hb.get("heartbeat_at"))
        else:
            off_since = _aware(first_seen) or _aware((hb or {}).get("started_at"))
    off_hours = round((now - off_since).total_seconds() / 3600, 1) if off_since else None
    due = [e.id for e, _ in sched.catchup_due(entries, completed, now)] if entries else []
    reason = None
    if not alive:
        reason = "worker not running" if hb else "worker has never run"
    elif not enabled:
        reason = (hb or {}).get("scheduler_disabled_reason") or "worker started with --no-schedule"
    return {
        "enabled": enabled, "worker_alive": alive, "reason": reason, "profile": active, "profile_source": source,
        "profile_label": spec.get("label", active), "worker_profile": (hb or {}).get("schedule_profile"),
        "timezone": sched.load_config(s).get("timezone"), "period_days": spec.get("period_days"),
        "sla_days": spec.get("sla_days"), "next_runs": next_runs, "next_runs_computed": not (enabled and worker_next),
        "last_success": {d: _iso(v.get("finished_at")) for d, v in completed.items()},
        "last_success_trigger": {d: v.get("trigger") for d, v in completed.items()},
        "last_scheduled_run": last_sched, "off_since": _iso(off_since), "off_hours": off_hours,
        "catchup_due": due, "entries": [e.to_dict(now) for e in entries],
        "last_refresh": last_refresh(completed),
    }


#: DAG runs that refresh the data: the scheduled refreshes and the startup / interval sync (alert hygiene 2026-10-30)
REFRESH_DAGS = ("nightly", "weekly", "monthly", "sync")


def last_refresh(completed: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent completed refresh of any kind - a scheduled DAG run (nightly / weekly / monthly) or a sync
    run - from completed_dag_runs(): {at, dag, dag_run_id, trigger, status, jobs, jobs_failed}. None = never."""
    best: tuple[datetime, str, dict[str, Any]] | None = None
    for dag in REFRESH_DAGS:
        v = completed.get(dag) or {}
        t = _aware(v.get("finished_at"))
        if t and (best is None or t > best[0]):
            best = (t, dag, v)
    if best is None:
        return None
    t, dag, v = best
    return {"at": t.isoformat(), "dag": dag, "dag_run_id": v.get("dag_run_id"), "trigger": v.get("trigger"),
            "status": v.get("status"), "jobs": v.get("jobs"), "jobs_failed": v.get("jobs_failed")}


def refresh_failures(job_runs: pl.DataFrame | None, now: datetime | None = None, lookback_days: int = 30,
                     registry: Any = None) -> list[dict[str, Any]]:
    """Jobs whose LATEST attempt failed, among the jobs that failed in a refresh run (nightly / weekly / monthly /
    sync DAG run) within `lookback_days` (alert hygiene 2026-10-30). The latest attempt is taken across every run of
    the job - nightly, weekly, sync, or a manual run of the job - so a job that failed in a nightly and succeeded in
    a later sync is resolved. Skipped and still-running attempts do not count. Each item: job_id, failed_at (latest
    failure), since (first failure of the current streak), dag_run_id, dag, error."""
    if job_runs is None or job_runs.is_empty() or "dag_run_id" not in job_runs.columns:
        return []
    now = now or datetime.now(UTC)
    df = job_runs.filter(pl.col("started_at") >= now - timedelta(days=lookback_days))
    if df.is_empty():
        return []
    is_refresh = pl.any_horizontal([pl.col("dag_run_id").fill_null("").str.ends_with(f"-{d}") for d in REFRESH_DAGS])
    cand = set(df.filter(is_refresh & (pl.col("status") == "failed")).get_column("job_id").to_list())
    if registry is not None:
        cand &= set(getattr(registry, "jobs", {}) or {})
    if not cand:
        return []
    att = df.filter(pl.col("job_id").is_in(sorted(cand)) & pl.col("status").is_in(["success", "partial", "failed"])) \
        .sort("started_at")
    out = []
    for jid, g in att.group_by("job_id", maintain_order=True):
        rows = g.to_dicts()
        last = rows[-1]
        if last["status"] != "failed":
            continue
        streak = last
        for r in reversed(rows):
            if r["status"] != "failed":
                break
            streak = r
        dag = str(last.get("dag_run_id") or "").rsplit("-", 1)[-1] if last.get("dag_run_id") else "manual"
        out.append({"job_id": jid[0] if isinstance(jid, tuple) else jid, "failed_at": _iso(last["started_at"]),
                    "since": _iso(streak["started_at"]), "dag_run_id": last.get("dag_run_id"), "dag": dag,
                    "trigger": last.get("trigger"), "error": (last.get("error_message") or "")[:200] or None})
    return sorted(out, key=lambda x: x["since"] or "")


# ------------------------------------------------------------------------------------------------ supervisor


def supervisor_summary(settings: Settings | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    """Incident 2026-10-24: restart counts / reasons per child, restart-storm alerts and the orphan reaper's metrics
    from data/logs/app/supervisor.json (written every 10 s by the supervisor). None when there is no supervisor."""
    from app.ops.paths import app_logs_dir  # noqa: PLC0415

    s = settings or get_settings()
    now = now or datetime.now(UTC)
    st = read_json(app_logs_dir(s) / "supervisor.json", {}) or {}
    if not st:
        return None
    hb = _aware(st.get("heartbeat_at"))
    children = {}
    for name, c in (st.get("children") or {}).items():
        children[name] = {"state": c.get("state"), "pid": c.get("pid"), "restarts": c.get("restarts", 0),
                          "restarts_30m": c.get("restarts_30m"), "ready": c.get("ready"),
                          "health_fail": c.get("health_fail"), "rss_mb": c.get("rss_mb"),
                          "started_at": c.get("started_at"), "last_exit": c.get("last_exit"),
                          "restart_reasons": (c.get("restart_reasons") or [])[:5], "alert": c.get("alert"),
                          "job_object": c.get("job_object")}
    rp = st.get("reaper") or {}
    return {"alive": bool(hb and now - hb < timedelta(minutes=2)), "pid": st.get("pid"),
            "heartbeat_at": st.get("heartbeat_at"), "started_at": st.get("started_at"),
            "restarts_total": st.get("restarts_total", sum(int(c.get("restarts") or 0) for c in children.values())),
            "children": children,
            "reaper": {"last_run_at": rp.get("last_run_at"), "runs": rp.get("runs", 0),
                       "leaked_processes": rp.get("leaked_now", 0), "killed_total": rp.get("killed_total", 0),
                       "freed_mb_total": rp.get("freed_mb_total", 0), "last_killed": (rp.get("last_killed") or [])[:5],
                       "stray_api_killed_total": rp.get("stray_api_killed_total", 0),
                       "stray_apis": rp.get("stray_apis") or [], "zombies": rp.get("zombies") or [],
                       "zombie_mb": rp.get("zombie_mb", 0), "exiting": rp.get("exiting") or [],
                       "error": rp.get("error")},
            "alerts": st.get("alerts") or []}


def supervisor_alerts(sup: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not sup or not sup.get("alive"):
        return out
    for name, c in (sup.get("children") or {}).items():
        a = c.get("alert")
        if a:
            out.append(_alert(f"restart_storm_{name}", "error", f"{name} restart loop",
                              f"{name} restarted {a.get('restarts_30m')} times in 30 min (last: {a.get('last_reason')}). "
                              "See data/logs/app/supervisor.stdout.log.", a.get("since"), "/system/data-health"))
    rp = sup.get("reaper") or {}
    last = (rp.get("last_killed") or [None])[0]
    if last and _aware(last.get("at")) and datetime.now(UTC) - _aware(last["at"]) < timedelta(hours=6):
        out.append(_alert("leaked_processes", "warn", "Leaked processes reaped",
                          f"The supervisor killed {len(last.get('pids') or [])} orphaned process(es) "
                          f"(~{last.get('freed_mb', 0)} MB) at {last.get('at')}; total {rp.get('killed_total', 0)}.",
                          last.get("at"), "/system/data-health"))
    if rp.get("zombies"):   # only processes in their exit for >= reaper.ZOMBIE_MIN_S (a normal teardown never alerts)
        zs = rp["zombies"]
        who = ", ".join(f"{z['pid']}" + (f" ({z['owner']})" if z.get("owner") else "") for z in zs[:5])
        out.append(_alert("zombie_processes", "warn", "Processes stuck in exit",
                          f"{len(zs)} process(es) of ours are stuck in their exit holding ~{rp.get('zombie_mb', 0)} MB "
                          f"(pids {who}); they cannot be killed - they finish on "
                          "their own or with a reboot.", zs[0].get("first_seen"), "/system/data-health"))
    if rp.get("leaked_processes"):
        out.append(_alert("leaked_processes_now", "warn", "Leaked processes running",
                          f"{rp['leaked_processes']} orphaned process(es) of ours are running (reaped within 5 min).",
                          rp.get("last_run_at"), "/system/data-health"))
    return out


# ------------------------------------------------------------------------------------------------ alerts


def _alert(aid: str, severity: str, title: str, detail: str, since: Any = None, link: str | None = None) -> dict[str, Any]:
    return {"id": aid, "severity": severity, "title": title, "detail": detail, "since": _iso(since), "link": link}


def sector_loss_alert(settings: Settings | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    """2026-10-31: the latest ref_universe run blanked the sector of more than 1 % of the classified issuers
    (app.normalize.identity.sector_loss; the run itself keeps previous classifications, so this means a source /
    taxonomy regression). Ages out after 8 days (a newer nightly run replaces the record)."""
    try:
        from app.normalize.identity import read_sector_loss

        rec = read_sector_loss(settings)
    except Exception:  # noqa: BLE001
        return None
    if not rec or rec.get("passed", True):
        return None
    at = _aware(rec.get("checked_at"))
    now = now or datetime.now(UTC)
    if at is None or now - at > timedelta(days=8):
        return None
    by = ", ".join(f"{k} {v}" for k, v in list((rec.get("by_country") or {}).items())[:5])
    return _alert("sector_loss", "error", "Issuers lost their sector",
                  f"ref_universe run {rec.get('run_id') or '-'}: {rec.get('lost')} of {rec.get('prev_classified')} "
                  f"classified issuers ({float(rec.get('rate') or 0):.1%}) came out without a sector "
                  f"(threshold {float(rec.get('threshold') or 0.01):.0%}; {by or 'no breakdown'}).", at,
                  "/system/data-health")


def ops_alerts(sch: dict[str, Any], cov: dict[str, Any] | None = None, filler: dict[str, Any] | None = None,
               last_dag_runs: list[dict[str, Any]] | None = None, disk_free_gb: float | None = None,
               now: datetime | None = None, supervisor: dict[str, Any] | None = None,
               sync: dict[str, Any] | None = None, job_runs: pl.DataFrame | None = None, registry: Any = None,
               views: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Ops alerts. Alert hygiene 2026-10-30:
    * dataset alerts come from app.ops.freshness (the sync's reason codes; the gap detector only measures the lag):
      failed, due / source_unavailable past the SLA, or `stale` when no recent sync verdict exists - never
      not_yet_published / needs_key / no_source / fresh;
    * `refresh_failed` names the jobs whose LATEST attempt failed (nightly, weekly, sync or manual runs), instead of
      the whole last nightly DAG run being "failed" forever (replaces `nightly_failed`);
    * `refresh_overdue` measures the most recent refresh of any kind (scheduled DAG run or sync)."""
    from app.ops import freshness as F  # noqa: PLC0415

    now = now or datetime.now(UTC)
    out: list[dict[str, Any]] = supervisor_alerts(supervisor)
    if not sch.get("worker_alive"):
        out.append(_alert("worker_down", "error", "Worker not running",
                          "No scheduled refresh and no queued job runs until the worker / supervisor is started.",
                          sch.get("off_since"), "/system"))
    elif not sch.get("enabled"):
        hrs = sch.get("off_hours") or 0
        out.append(_alert("scheduler_off", "error" if hrs > 24 else "warn", "Scheduler off",
                          f"{sch.get('reason') or 'scheduler disabled'}; off for {hrs / 24:.1f} days. Scheduled "
                          f"refreshes ({sch.get('profile')}) will not run.", sch.get("off_since"), "/system"))
    period = float(sch.get("period_days") or 1)
    lr = sch.get("last_refresh") or None
    if lr is None:   # older callers: the nightly DAG only
        t = _aware((sch.get("last_success") or {}).get("nightly"))
        lr = {"at": _iso(t), "dag": "nightly"} if t else None
    last_t = _aware((lr or {}).get("at"))
    if last_t and now - last_t > timedelta(days=period + 1):
        days = (now - last_t).total_seconds() / 86400
        out.append(_alert("refresh_overdue", "error", "Refresh overdue",
                          f"Last completed refresh ({lr.get('dag')}) {days:.1f} days ago (profile {sch.get('profile')}: "
                          f"every {period:g} d; the startup sync counts too).", last_t, "/system"))
    if sch.get("catchup_due"):
        out.append(_alert("catchup_due", "warn", "Missed scheduled run",
                          f"Due for catch-up: {', '.join(sch['catchup_due'])} (runs as soon as the worker is up).",
                          None, "/system"))
    vs = views if views is not None else F.dataset_views(cov, sync, now)
    bad = [v for v in vs.values() if v.get("alerting")]
    crit = [v for v in bad if v["dataset"] in CRITICAL_DATASETS]
    other = [v for v in bad if v["dataset"] not in CRITICAL_DATASETS]
    if crit:
        out.append(_alert("sla_critical", "error", "Core data stale beyond SLA",
                          "; ".join(F.describe(v) for v in crit)[:600], None, "/system/data-health"))
    if other:
        failing = any(v.get("reason") == "failed" for v in other)
        out.append(_alert("sla_other", "warn",
                          f"{len(other)} dataset(s) {'failing or stale' if failing else 'stale'} beyond SLA",
                          "; ".join(F.describe(v) for v in other)[:600], None, "/system/data-health"))
    fails = refresh_failures(job_runs, now, registry=registry)
    if fails:
        names = ", ".join(f"{f['job_id']} ({f['dag']}{', ' + f['error'][:80] if f.get('error') else ''})"
                          for f in fails[:8])
        out.append(_alert("refresh_failed", "warn", f"{len(fails)} refresh job(s) failing",
                          f"Latest attempt failed: {names}{' ...' if len(fails) > 8 else ''}. "
                          f"Last refresh {(lr or {}).get('dag') or '-'} {str((lr or {}).get('at') or '-')[:16]}.",
                          fails[0].get("since"), "/system"))
    if disk_free_gb is not None and disk_free_gb < 25:
        out.append(_alert("disk_low", "error" if disk_free_gb < 15 else "warn", "Low disk space",
                          f"{disk_free_gb:.1f} GB free on the data drive (filler pauses below 15 GB).", None,
                          "/system/data-health"))
    f = filler or {}
    hb = _aware(f.get("heartbeat_at"))
    if hb is None or now - hb > timedelta(minutes=5):
        out.append(_alert("filler_down", "info", "Gap filler not running",
                          "Background gap filling is off (start it with the supervisor).", hb, "/system/data-health"))
    else:
        gov = f.get("governor") or {}
        pz = f.get("pause") or (gov.get("sample") or {}).get("pause") or {}
        if gov.get("state") == "paused":
            # coverage v3 A4: the pause flag's reason, owner and expiry, not only "paused by user"
            extra = ""
            if pz.get("active"):
                extra = (f" Flag filler.PAUSE: {pz.get('reason') or 'no reason given'}"
                         f"{' (by ' + str(pz['by']) + ')' if pz.get('by') else ''}, expires "
                         f"{str(pz.get('until') or '-')[:16].replace('T', ' ')} UTC"
                         f"{' (legacy file: 7 days after it was written)' if pz.get('legacy') else ''}.")
            out.append(_alert("filler_paused", "info", "Gap filler paused", "; ".join(gov.get("reasons") or []) + extra,
                              pz.get("since") if pz.get("active") else None, "/system/data-health"))
        if pz.get("expired"):
            at = str(pz.get("expired_at") or "-")[:16].replace("T", " ")
            out.append(_alert("pause_expired", "info", "Expired pause flag ignored",
                              f"data/state/ops/filler.PAUSE expired {at} UTC ({pz.get('reason') or 'no reason'}): the "
                              "gap filler runs again. Remove the file.", pz.get("expired_at"), "/system/data-health"))
        # coverage v3 A2 review: filler tasks the runner kept deferring (RAM floor / heavy slot) are parked, not hidden
        parked = sorted(((k, v) for k, v in (f.get("deferrals") or {}).items() if (v or {}).get("parked")),
                        key=lambda kv: str(kv[1].get("first_at") or ""))
        if parked:
            names = "; ".join(f"{v.get('label') or k}: deferred {v.get('n')} times ({str(v.get('reason') or '')[:100]})"
                              for k, v in parked[:4])
            out.append(_alert("filler_deferred", "warn", f"{len(parked)} gap-filler task(s) parked after repeated "
                              "deferrals", f"{names}{' ...' if len(parked) > 4 else ''}. The RAM floor or the heavy "
                              "slot kept them from starting; they retry once a day.", parked[0][1].get("first_at"),
                              "/system/data-health"))
    return out


# ------------------------------------------------------------------------------------------------ data health


def _source_status(job_runs: pl.DataFrame, registry: Any, reg_cov: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    jobs_by_src: dict[str, set[str]] = {}
    if registry is not None:
        for j in registry.jobs.values():
            for src in j.sources:
                jobs_by_src.setdefault(src, set()).add(j.id)
    for d in reg_cov.get("datasets", {}).values():
        src = (d.get("cost") or {}).get("source")
        if src and d.get("live_job"):
            jobs_by_src.setdefault(src, set()).add(d["live_job"])
    out: dict[str, dict[str, Any]] = {}
    recent = job_runs.filter(pl.col("started_at") >= now - timedelta(days=14)) if not job_runs.is_empty() else job_runs
    for src, jobs in jobs_by_src.items():
        sub = recent.filter(pl.col("job_id").is_in(sorted(jobs)) & pl.col("status").is_in(["success", "partial", "failed"])) \
            if not recent.is_empty() else recent
        if sub.is_empty():
            out[src] = {"status": "idle", "jobs": sorted(jobs), "runs_14d": 0}
            continue
        sub = sub.sort("started_at", descending=True)
        sts = sub["status"].to_list()
        streak = 0
        for x in sts:
            if x == "failed":
                streak += 1
            else:
                break
        ok = sub.filter(pl.col("status") != "failed")
        bad = sub.filter(pl.col("status") == "failed")
        out[src] = {
            "status": "failing" if streak >= 3 else ("degraded" if streak >= 1 or bad.height > sub.height / 3 else "ok"),
            "jobs": sorted(jobs), "runs_14d": sub.height, "failures_14d": bad.height, "fail_streak": streak,
            "last_ok_at": _iso(ok["finished_at"][0]) if ok.height else None,
            "last_fail_at": _iso(bad["started_at"][0]) if bad.height else None,
            "last_error": (bad["error_message"][0] or "")[:300] if bad.height else None,
        }
    return out


def data_health(settings: Settings | None = None, job_runs: pl.DataFrame | None = None, hb: dict[str, Any] | None = None,
                registry: Any = None, last_dag_runs: list[dict[str, Any]] | None = None,
                now: datetime | None = None, max_items: int = 200) -> dict[str, Any]:
    from app.ops import freshness as F
    from app.ops.coverage import load_coverage
    from app.ops.gaps import OPEN_STATUSES, coverage_status, read_gaps
    from app.ops.winsys import disk_free_gb

    s = settings or get_settings()
    now = now or datetime.now(UTC)
    reg = load_coverage(s)
    cov = coverage_status(s)
    sync = read_sync_status(s)
    views = F.dataset_views(cov, sync, now, {k: v.get("label", k) for k, v in reg["datasets"].items()})
    runs = job_runs if job_runs is not None else pl.DataFrame()
    filler = read_json(ops_state_dir(s) / "filler.json", {}) or {}
    sch = scheduler_status(s, hb, registry, runs, now)
    gaps = read_gaps(s)
    last_status: dict[str, str] = {}
    if not runs.is_empty():
        for r in runs.sort("started_at", descending=True).unique("job_id", keep="first").iter_rows(named=True):
            last_status[r["job_id"]] = r["status"]
    open_by_ds: dict[str, int] = {}
    if not gaps.is_empty():
        for r in gaps.filter(pl.col("status").is_in(list(OPEN_STATUSES))).group_by("dataset").len().iter_rows():
            open_by_ds[r[0]] = int(r[1])
    cds = cov.get("datasets") or {}
    datasets = []
    for did, spec in reg["datasets"].items():
        c = cds.get(did) or {}
        v = views.get(did) or F.verdict(c, None, False)
        datasets.append({
            "dataset": did, "label": spec.get("label", did), "group": spec.get("group", "other"),
            # alert hygiene 2026-10-30: one verdict (app.ops.freshness) - status / alerting follow the sync's reason
            "status": v["status"], "data_as_of": c.get("data_as_of"), "expected_as_of": c.get("expected_as_of"),
            "lag": c.get("lag"), "sla": c.get("sla"), "detail": c.get("detail"), "priority": spec.get("priority"),
            "live_job": spec.get("live_job"), "last_job_status": last_status.get(spec.get("live_job") or ""),
            "open_gaps": open_by_ds.get(did, 0),
            **{k: v.get(k) for k in ("reason", "reason_source", "reason_detail", "next_expected", "retry_at",
                                     "attempts", "keys", "alerting", "detector_status")}})
    days = sorted({d for c in cds.values() for d in (c.get("cells") or {})})[-21:]
    heat = [{"dataset": d["dataset"], "label": d["label"], "group": d["group"],
             "cells": [((cds.get(d["dataset"]) or {}).get("cells") or {}).get(x) for x in days]}
            for d in datasets if (cds.get(d["dataset"]) or {}).get("cells")]
    by_status: dict[str, int] = {}
    by_class: dict[str, int] = {}
    per_ds: dict[str, dict[str, int]] = {}
    items: list[dict[str, Any]] = []
    if not gaps.is_empty():
        for r in gaps.iter_rows(named=True):
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1
            if r["status"] not in ("filled", "ignored"):
                by_class[r["gap_class"]] = by_class.get(r["gap_class"], 0) + 1
            per_ds.setdefault(r["dataset"], {}).setdefault(r["status"], 0)
            per_ds[r["dataset"]][r["status"]] += 1
        live = gaps.filter(~pl.col("status").is_in(["filled", "ignored"])).sort(["priority", "first_seen"])
        for r in live.head(max_items).iter_rows(named=True):
            items.append({k: _iso(r.get(k)) for k in ("gap_id", "dataset", "entity", "gap_class", "status", "period_start",
                                                      "period_end", "expected", "actual", "priority", "fill_kind", "note",
                                                      "attempts", "last_error", "next_attempt_at", "first_seen", "last_seen")})
    labels = {k: v.get("label", k) for k, v in reg["datasets"].items()}
    gap_ds = [{"dataset": k, "label": labels.get(k, k), **{st: v.get(st, 0) for st in
                                                           ("open", "queued", "filling", "failed", "unfillable", "manual", "filled")}}
              for k, v in sorted(per_ds.items())]
    camps = []
    cst_all = filler.get("campaign_status") or {}
    for cid, c in reg["campaigns"].items():
        st = cst_all.get(cid) or {}
        pr = st.get("progress") or {}
        if not pr and c.get("progress"):
            from app.ops.tasks import probe

            pr = probe(c, s)
        eta_h = pr.get("eta_h")
        status = ("running" if st.get("running") else "done" if pr.get("done") else
                  "backoff" if st.get("next_attempt_at") and _aware(st["next_attempt_at"]) and _aware(st["next_attempt_at"]) > now
                  else "waiting")
        camps.append({"id": cid, "label": c.get("label", cid), "kind": c.get("kind"), "status": status,
                      "progress_pct": pr.get("pct"), "done_units": pr.get("done_units"), "total_units": pr.get("total_units"),
                      "unit": pr.get("unit"), "rate_per_h": pr.get("rate_per_h"), "eta_h": eta_h,
                      "eta_at": (now + timedelta(hours=float(eta_h))).isoformat(timespec="minutes") if eta_h else None,
                      "detail": pr.get("detail"), "last_error": st.get("last_error"), "last_success_at": st.get("last_success_at"),
                      "priority": c.get("priority"), "source": c.get("source")})
    fhb = _aware(filler.get("heartbeat_at"))
    gov = filler.get("governor") or {}
    budgets = filler.get("budgets") or {}
    if not budgets:
        from app.ops.budgets import RateBudgets

        budgets = RateBudgets.load(s, reg["sources"]).snapshot()
    src_status = _source_status(runs, registry, reg, now) if not runs.is_empty() else {}
    sources = []
    for name in sorted(set(budgets) | set(src_status)):
        b, st = budgets.get(name) or {}, src_status.get(name) or {}
        sources.append({"source": name, "unit": b.get("unit"), "per_hour": b.get("per_hour"), "used_1h": b.get("used_1h"),
                        "pct": b.get("pct"), "status": st.get("status", "idle"), "runs_14d": st.get("runs_14d", 0),
                        "failures_14d": st.get("failures_14d", 0), "fail_streak": st.get("fail_streak", 0),
                        "last_ok_at": st.get("last_ok_at"), "last_fail_at": st.get("last_fail_at"),
                        "last_error": st.get("last_error"), "jobs": st.get("jobs", []), "note": b.get("note")})
    failures = []
    if not runs.is_empty():
        fr = runs.filter((pl.col("status") == "failed") & (pl.col("started_at") >= now - timedelta(days=7))) \
            .sort("started_at", descending=True).head(50)
        failures = [{"run_id": r["run_id"], "job_id": r["job_id"], "trigger": r["trigger"], "started_at": _iso(r["started_at"]),
                     "error_type": r.get("error_type"), "error_message": (r.get("error_message") or "")[:400]}
                    for r in fr.iter_rows(named=True)]
    breaches = [{"dataset": d["dataset"], "label": d["label"], "lag": d["lag"], "sla": d["sla"],
                 "data_as_of": d["data_as_of"], "expected_as_of": d["expected_as_of"], "reason": d["reason"],
                 "reason_detail": d["reason_detail"]}
                for d in datasets if d.get("alerting")]
    disk = disk_free_gb(s.data_dir)
    sup = supervisor_summary(s, now)
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "profile": {"active": sch["profile"], "sla_days": sch["sla_days"], "period_days": sch["period_days"]},
        "detector": {"generated_at": cov.get("generated_at"), "seconds": cov.get("seconds"), "errors": cov.get("errors") or {}},
        "datasets": datasets, "heatmap": {"days": days, "rows": heat},
        "gaps": {"by_status": by_status, "by_class": by_class, "by_dataset": gap_ds, "items": items,
                 "total": int(gaps.height)},
        "campaigns": camps,
        "filler": {"alive": bool(fhb and now - fhb < timedelta(minutes=5)), "pid": filler.get("pid"),
                   "heartbeat_at": filler.get("heartbeat_at"), "state": gov.get("state"), "reasons": gov.get("reasons") or [],
                   "sample": gov.get("sample") or {}, "config": gov.get("config") or {},
                   "running": [{"task_id": k, **v} for k, v in (filler.get("running") or {}).items()],
                   "waiting": [{"task_id": k, "reason": v} for k, v in (filler.get("waiting") or {}).items()],
                   "recent": (filler.get("recent") or [])[:30], "keep_awake": filler.get("keep_awake") or [],
                   # coverage v3 A4: the pause flag read live (reason, by, since, until, expired, legacy)
                   "pause": _filler_pause(s, now)},
        "sources": sources, "failures": failures, "sla_breaches": breaches,
        "alerts": ops_alerts(sch, cov, filler, last_dag_runs, disk, now, sup, sync=sync, job_runs=runs,
                             registry=registry, views=views) + [a for a in (sector_loss_alert(s, now),) if a],
        "disk_free_gb": round(disk, 1) if disk is not None else None,
        "supervisor": sup,
        "freshness": {"source": "sync" if F.sync_authoritative(sync, now) else "detector",
                      "sync_scanned_at": _iso(F.sync_scanned_at(sync)), "sync_run_id": (sync or {}).get("run_id"),
                      "authoritative_h": F.SYNC_AUTHORITATIVE_H},
        "last_refresh": sch.get("last_refresh"),
        "refresh_failures": refresh_failures(runs, now, registry=registry),
    }


def _filler_pause(settings: Settings, now: datetime | None = None) -> dict[str, Any] | None:
    """filler.PAUSE state (app.ops.pause) or None when there is no flag file; never raises."""
    try:
        from app.ops.pause import filler_pause  # noqa: PLC0415

        st = filler_pause(settings, now, warn=False)
        return st.to_dict() if st.present else None
    except Exception:  # noqa: BLE001
        return None


def read_sync_status(settings: Settings | None = None) -> dict[str, Any] | None:
    """The startup sync's status doc (data/state/ops/sync_status.json) or None; never raises."""
    try:
        from app.ops.sync.state import read_status  # noqa: PLC0415

        return read_status(settings) or None
    except Exception:  # noqa: BLE001
        return None
