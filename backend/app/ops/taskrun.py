"""Child-process entry for gap-filler tasks: `python -m app.ops.taskrun <spec.json>`.

Every fill reuses the live ingestion code through the JobRunner (job_runs bookkeeping, job locks, process
executor, retries) with trigger=backfill, so backfill and live refresh produce identical data. The spec is a JSON
file (no command-line length limits for symbol lists). Kinds:

    jobs            [{"job": id, "params": {...}}, ...] run in order (stops at the first failure)
    yf_refetch      {"region": R, "dates": [...]}: Yahoo re-fetch of the region's symbols over the smallest period
                    covering the dates (prices_global_yf period mode), then prices_finalize for the region
    polygon_dates   {"dates": [...]}: Polygon grouped daily for those dates, then prices_finalize (US)
    prices_history  {"max_symbols": n, "region": R?}: full Yahoo history for securities without any bar
    detect          gap detection only ({"datasets": [...]})

After the work, `detect_after` datasets are re-checked so filled gaps resolve immediately. The result is written
to spec["result_path"]: {"status": ok|failed|blocked|skipped|deferred, "outcomes": {...}, "units": n, "message": str}.
Exit codes: 0 ok, 1 failed, 3 blocked (job already running elsewhere: not an attempt) or deferred (coverage v3 A2:
the runner deferred a step - RAM floor / heavy slot; result status "deferred": not an attempt, the filler backs off),
4 skipped (missing key).
Steps that succeeded are remembered per task for STEP_MEMO_TTL_H (read_memo): a retry runs only what is left.

Stop (incident 2026-10-24): CTRL+BREAK / SIGINT set the runner's stop event (no further jobs; a running process
job gets 60 s, then its Job Object is killed) instead of the default console handler, which killed this process
mid-flight (exit 0xC0000005) and left its job process orphaned. The filler's Job Object reaps any leftover.
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.core.logsetup import get_logger, setup_logging

log = get_logger("app.ops.taskrun")

STOP = threading.Event()


def _install_stop_handlers() -> None:
    def handler(signum: int, _frame: Any) -> None:
        log.info("taskrun: signal %s - stopping after the running job", signum)
        STOP.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):  # pragma: no cover - not the main thread
                pass


def _period_for(dates: list[date], today: date) -> str:
    oldest = min(dates) if dates else today
    age = (today - oldest).days
    for lim, p in ((25, "1mo"), (85, "3mo"), (170, "6mo"), (350, "1y"), (700, "2y"), (1800, "5y")):
        if age <= lim:
            return p
    return "max"


def region_yf_symbols(region: str, days: int = 45, settings: Any = None) -> list[str]:
    """Yahoo tickers of the securities that had bars in `region` during the last `days` days (primary listing)."""
    from app.core.storage import duckdb_connect, register_views

    s = settings or get_settings()
    con = duckdb_connect(None, settings=s)
    try:
        con.execute("SET memory_limit = '1GB'")
        con.execute("SET threads = 4")
        register_views(con, ["prices_daily", "listings"], settings=s)
        rows = con.execute(f"""
            WITH ids AS (SELECT DISTINCT security_id FROM prices_daily
                         WHERE region = '{region}' AND date >= current_date - INTERVAL {int(days)} DAY)
            SELECT l.yf_ticker FROM listings l JOIN ids USING (security_id)
            WHERE l.yf_ticker IS NOT NULL AND (l.valid_to IS NULL OR l.valid_to >= current_date)
            QUALIFY row_number() OVER (PARTITION BY l.security_id ORDER BY l.is_primary DESC NULLS LAST) = 1
            ORDER BY 1""").fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def unpriced_symbols(max_symbols: int, region: str | None = None, settings: Any = None) -> tuple[list[str], list[str]]:
    """(yf tickers, regions) of snapshot securities without any daily bar."""
    import duckdb

    from app.core.storage import current_serving_db

    s = settings or get_settings()
    p = current_serving_db(s)
    if p is None:
        return [], []
    con = duckdb.connect(str(p), read_only=True)
    try:
        cols = {r[0] for r in con.execute("DESCRIBE sec").fetchall()}
        tcol = "yf_ticker" if "yf_ticker" in cols else None
        if tcol is None:
            return [], []
        where = f" AND region = '{region}'" if region else ""
        rows = con.execute(f"SELECT {tcol}, region FROM sec WHERE close IS NULL AND {tcol} IS NOT NULL{where} "
                           f"ORDER BY coalesce(u_full, false) DESC, {tcol} LIMIT {int(max_symbols)}").fetchall()
    finally:
        con.close()
    return [r[0] for r in rows], sorted({r[1] for r in rows if r[1]})


#: coverage v3 A2 review 2026-11-07: steps of a task that succeeded are remembered this long (data/state/ops/tasks/
#: <task>.steps.json), so the retry after a deferral (RAM floor / heavy slot), a stop or a failed later step does not
#: repeat them - e.g. a Yahoo / Polygon re-download in front of a prices_finalize that was deferred. Cleared when the
#: task ends ok; the TTL bounds how long a staged fetch is trusted.
STEP_MEMO_TTL_H = 12.0


def step_key(job: str, params: dict[str, Any]) -> str:
    import hashlib  # noqa: PLC0415

    return f"{job}:{hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:12]}"


def memo_path(task_id: str, settings: Any = None) -> Path:
    from app.ops.tasks import safe_name, task_dir  # noqa: PLC0415

    return task_dir(settings) / f"{safe_name(task_id)}.steps.json"


def read_memo(task_id: str | None, settings: Any = None, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{step_key: {job, finished_at, rows}} of the task's steps that succeeded within STEP_MEMO_TTL_H."""
    if not task_id:
        return {}
    try:
        doc = json.loads(memo_path(task_id, settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = now or datetime.now(UTC)
    out = {}
    for k, v in (doc.get("steps") or {}).items():
        try:
            at = datetime.fromisoformat(str((v or {}).get("finished_at")))
        except ValueError:
            continue
        if now - (at if at.tzinfo else at.replace(tzinfo=UTC)) <= timedelta(hours=STEP_MEMO_TTL_H):
            out[k] = v
    return out


def _write_memo(task_id: str, steps: dict[str, dict[str, Any]], settings: Any = None) -> None:
    from app.ops.paths import write_json_atomic  # noqa: PLC0415

    try:
        write_json_atomic(memo_path(task_id, settings), {"task_id": task_id, "steps": steps})
    except OSError as e:  # pragma: no cover - the retry then repeats the step, as before
        log.warning("taskrun: step memo of %s not written: %s", task_id, e)


def clear_memo(task_id: str | None, settings: Any = None) -> None:
    if task_id:
        try:
            memo_path(task_id, settings).unlink(missing_ok=True)
        except OSError:  # pragma: no cover - expires after STEP_MEMO_TTL_H anyway
            pass


def run_jobs(steps: list[dict[str, Any]], trigger: str = "backfill", task_id: str | None = None,
             settings: Any = None) -> tuple[str, dict[str, Any], int]:
    """Run the steps in order -> (status ok | failed | blocked | deferred | skipped, outcomes, units).

    deferred: the runner deferred a step (RAM floor / heavy slot / start budget; coverage v3 A2) - not an attempt, the
    filler backs off and retries; blocked: the job already runs elsewhere. With a task_id, steps that succeeded in an
    earlier attempt of the same task (read_memo, same job and params) are not run again."""
    from app.scheduler.runner import JobRegistry, JobRunner, RunOptions

    reg = JobRegistry.load()
    runner = JobRunner(reg, stop_event=STOP)
    outcomes: dict[str, Any] = {}
    units = 0
    status = "ok"
    memo = read_memo(task_id, settings)
    for st in steps:
        if STOP.is_set():
            status = "failed"
            outcomes["_stopped"] = {"status": "skipped", "reason": "stopped"}
            break
        job, params = st["job"], dict(st.get("params") or {})
        key = step_key(job, params)
        if key in memo:
            at = memo[key].get("finished_at")
            outcomes[job] = {"status": "success", "reason": "done_earlier", "rows": 0, "error_type": None,
                             "message": f"succeeded in an earlier attempt of this task at {at}: not run again"}
            log.info("taskrun %s -> done earlier (%s): skipped", job, at)
            continue
        # coverage v3 A2: filler work is opportunistic - below the RAM floor (or behind the heavy slot) it is deferred,
        # never started in spill mode; the filler retries the task later ("deferred": no attempt counted)
        o = runner.run_jobs([job], RunOptions(trigger=trigger, params=params, force=True, ram_defer=True)).outcomes[job]
        outcomes[job] = {"status": o.status, "reason": o.reason, "message": (o.message or "")[:500],
                         "rows": o.rows_written, "error_type": o.error_type}
        log.info("taskrun %s -> %s %s %s", job, o.status, o.reason or "", (o.message or "")[:200])
        if o.status in ("success", "partial") and task_id and st is not steps[-1]:
            # a partial fetch counts as done too: detect_after decides what is still missing once the task ends ok
            memo[key] = {"job": job, "status": o.status, "rows": o.rows_written,
                         "finished_at": datetime.now(UTC).isoformat(timespec="seconds")}
            _write_memo(task_id, memo, settings)
        if o.status == "skipped" and o.reason == "deferred":
            return "deferred", outcomes, units
        if o.status == "skipped" and o.reason == "already_running":
            return "blocked", outcomes, units
        if o.status == "skipped" and o.reason == "job_skipped" and "key" in (o.message or "").lower():
            return "skipped", outcomes, units
        if o.status == "failed":
            status = "failed"
            break
    if status == "ok":
        clear_memo(task_id, settings)
    return status, outcomes, units


def execute(spec: dict[str, Any]) -> dict[str, Any]:
    kind = spec.get("kind", "jobs")
    today = datetime.now(UTC).date()
    trigger = spec.get("trigger", "backfill")
    tid = spec.get("task_id")   # coverage v3 A2 review: the step memo of this task (read_memo)
    units = 0
    if kind == "jobs":
        status, outcomes, units = run_jobs(spec.get("jobs") or [], trigger, tid)
    elif kind == "yf_refetch":
        region = spec["region"]
        dates = [date.fromisoformat(d) for d in spec.get("dates") or []]
        syms = spec.get("symbols") or region_yf_symbols(region)
        if not syms:
            return {"status": "failed", "message": f"no Yahoo symbols for region {region}", "units": 0}
        period = _period_for(dates, today)
        units = len(syms)
        status, outcomes, _ = run_jobs([
            {"job": "prices_global_yf", "params": {"symbols": syms, "period": period, "batch_size": 250}},
            {"job": "prices_finalize", "params": {"regions": [region]}},
        ], trigger, tid)
        outcomes["_plan"] = {"region": region, "symbols": len(syms), "period": period, "dates": spec.get("dates")}
    elif kind == "polygon_dates":
        dates = sorted(spec.get("dates") or [])
        units = len(dates)
        status, outcomes, _ = run_jobs([
            {"job": "prices_us_polygon", "params": {"dates": dates}},
            {"job": "prices_finalize", "params": {"regions": ["US"]}},
        ], trigger, tid)
    elif kind == "prices_history":
        syms, regions = unpriced_symbols(int(spec.get("max_symbols", 2000)), spec.get("region"))
        if not syms:
            return {"status": "ok", "message": "no unpriced securities with a Yahoo ticker", "units": 0}
        units = len(syms)
        steps = [{"job": "prices_global_yf", "params": {"symbols": syms, "period": "max", "batch_size": int(spec.get("batch", 250))}}]
        if regions:
            steps.append({"job": "prices_finalize", "params": {"regions": regions}})
        status, outcomes, _ = run_jobs(steps, trigger, tid)
    elif kind == "detect":
        from app.ops.gaps import detect

        rep = detect(datasets=spec.get("datasets"), run_id=spec.get("task_id"))
        return {"status": "ok" if not rep.errors else "failed", "message": rep.summary(), "units": 0,
                "outcomes": {"errors": rep.errors}}
    else:
        return {"status": "failed", "message": f"unknown task kind {kind!r}", "units": 0}
    res = {"status": status, "outcomes": outcomes, "units": units}
    if status in ("deferred", "blocked"):   # why it could not run: the filler shows / backs off on it
        why = next((o.get("message") for o in outcomes.values() if isinstance(o, dict)
                    and o.get("reason") in ("deferred", "already_running")), None)
        res["message"] = (why or status)[:500]
    if spec.get("detect_after") and status == "ok":
        try:
            from app.ops.gaps import detect

            rep = detect(datasets=list(spec["detect_after"]), run_id=spec.get("task_id"))
            res["detect"] = rep.summary()
        except Exception as e:  # pragma: no cover - the next full detection resolves it anyway
            res["detect_error"] = f"{type(e).__name__}: {e}"
    return res


#: deferred shares exit 3 with blocked (a filler that predates the `deferred` outcome keeps treating it as blocked:
#: no attempt); the filler tells them apart by the result file's status
EXIT = {"ok": 0, "failed": 1, "blocked": 3, "skipped": 4, "deferred": 3}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    setup_logging(log_file="filler_tasks.log", service="filler_task")
    _install_stop_handlers()
    t0 = time.monotonic()
    try:
        res = execute(spec)
    except Exception as e:
        log.exception("taskrun %s crashed", spec.get("task_id"))
        res = {"status": "failed", "message": f"{type(e).__name__}: {e}"[:1000], "units": 0}
    res["seconds"] = round(time.monotonic() - t0, 1)
    if spec.get("result_path"):
        Path(spec["result_path"]).write_text(json.dumps(res, default=str, indent=1), encoding="utf-8")
    return EXIT.get(res.get("status", "failed"), 1)


if __name__ == "__main__":
    raise SystemExit(main())
