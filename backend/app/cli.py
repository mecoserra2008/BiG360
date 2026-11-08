"""BiG Equity 360 command line (owner: platform). Run from backend/:  ..\\.venv\\Scripts\\python.exe -m app.cli <cmd>

    doctor [--offline] [--json]                    environment check (.env keys PRESENT/MISSING only, disk, packages, network)
    jobs list [--dag D] [--json]                   registry with availability and last run
    jobs plan <dag|job...> [--universe mvp|full] [--dry-run] [--json]
    jobs run <job...> [--as-of D] [--since D] [--universe mvp|full] [--tickers T ...] [--param k=v ...]
                      [--dry-run] [--force] [--with-upstream] [--with-downstream] [--retries N] [--max-workers N]
    jobs run-dag <nightly|weekly|monthly|manual> [--as-of D] [--resume] [--force] [--dry-run] [--universe ...]
    jobs status [--limit N] [--job J] [--status S] [--dag-run ID] [--json]
    jobs enqueue <job> [--param k=v ...] [--include-downstream]     (what the API does; the worker picks it up)
    jobs queue                                     pending / processing queue requests
    run-job <job> [--as-of D] [--param k=v]        alias of `jobs run` (ARCHITECTURE §2)
    worker start [--once] [--no-schedule] [--realtime]   (--realtime also runs the realtime supervisor; default off)
    realtime start|status|compact|dlq|replay        real-time layer R1 (app.realtime.cli)
    dq run [--tables T ...] [--no-write]  |  dq summary [--json]
    alerts eval [--as-of D]
    serve api [--host H] [--port P] [--reload]
    db build-serving [--as-of D]                   runs job build_serving_db (app.api.serving) if present
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------------- helpers


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _kv(pairs: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--param expects k=v, got {p!r}")
        k, v = p.split("=", 1)
        try:
            out[k] = json.loads(v)
        except ValueError:
            out[k] = v
    return out


def _date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _job_params(args: argparse.Namespace) -> dict[str, Any]:
    p = _kv(getattr(args, "param", None))
    if getattr(args, "since", None):
        p["since"] = args.since
    if getattr(args, "universe", None):
        p["universe"] = args.universe
    if getattr(args, "tickers", None):
        p["tickers"] = [t for x in args.tickers for t in x.split(",") if t]
    return p


def _setup_cli_logging(verbose: bool = False) -> None:
    from app.core.logsetup import setup_logging

    setup_logging(level="DEBUG" if verbose else None, log_file="cli.log", service="cli")


# --------------------------------------------------------------------------------------------- jobs


def cmd_jobs_list(args: argparse.Namespace) -> int:
    from app.scheduler.runner import JobRegistry, JobRunStore, check_available

    reg = JobRegistry.load()
    runs = JobRunStore().read()
    last: dict[str, dict[str, Any]] = {}
    if runs.height:
        for r in runs.sort("started_at", descending=True).unique(subset=["job_id"], keep="first").iter_rows(named=True):
            last[r["job_id"]] = r
    rows = []
    for j in reg.jobs.values():
        if args.dag and j.dag != args.dag:
            continue
        ok, why = check_available(j)
        lr = last.get(j.id)
        rows.append({"job_id": j.id, "dag": j.dag, "owner": j.owner, "available": ok, "target": j.target,
                     "executor": j.executor, "depends_on": list(j.depends_on),
                     "last_status": lr["status"] if lr else None,
                     "last_started_at": lr["started_at"].isoformat() if lr else None})
    if args.json:
        _print_json(rows)
        return 0
    print(f"{'job_id':<26} {'dag':<8} {'owner':<20} {'avail':<6} {'exec':<8} {'last':<9} last_started_at")
    for r in rows:
        print(f"{r['job_id']:<26} {r['dag']:<8} {r['owner']:<20} {'yes' if r['available'] else 'no':<6} "
              f"{r['executor']:<8} {r['last_status'] or '-':<9} {r['last_started_at'] or '-'}")
    print(f"{len(rows)} jobs, {sum(r['available'] for r in rows)} available")
    return 0


def cmd_jobs_plan(args: argparse.Namespace) -> int:
    from app.scheduler.runner import JobRegistry, format_plan, next_run_times

    reg = JobRegistry.load()
    targets = args.target
    plan = reg.plan(targets[0] if len(targets) == 1 else targets, args.universe)
    if args.json:
        _print_json(plan)
        return 0
    print(format_plan(plan))
    if plan["target"] in reg.dags:
        nxt = next_run_times(reg).get(plan["target"])
        if nxt:
            print(f"next scheduled run: {nxt}")
    if args.dry_run:
        print("(dry run: nothing executed)")
    return 0


def _print_report(rep: Any, as_json: bool) -> int:
    if as_json:
        _print_json(rep.to_dict())
    else:
        print(rep.summary())
    return 0 if rep.status != "failed" else 1


def cmd_jobs_run(args: argparse.Namespace) -> int:
    from app.scheduler.runner import JobRegistry, JobRunner, RunOptions, format_plan

    reg = JobRegistry.load()
    jobs = args.job
    if args.dry_run:
        ids = list(jobs)
        for j in jobs:
            if args.with_upstream:
                ids += reg.upstream(j)
            if args.with_downstream:
                ids += reg.downstream(j)
        print(format_plan(reg.plan(list(dict.fromkeys(ids)), args.universe or "mvp")))
        print(f"params: {json.dumps(_job_params(args), default=str)}  as_of: {args.as_of or 'previous business day'}")
        print("(dry run: nothing executed)")
        return 0
    _setup_cli_logging(args.verbose)
    runner = JobRunner(reg)
    opts = RunOptions(as_of=_date(args.as_of), params=_job_params(args), trigger=args.trigger, force=args.force,
                      retries=args.retries, max_workers=args.max_workers)
    rep = runner.run_jobs(jobs, opts, with_upstream=args.with_upstream, with_downstream=args.with_downstream)
    return _print_report(rep, args.json)


def cmd_jobs_run_dag(args: argparse.Namespace) -> int:
    from app.scheduler.runner import JobRegistry, JobRunner, RunOptions, format_plan

    reg = JobRegistry.load()
    if args.dry_run:
        print(format_plan(reg.plan(args.dag, args.universe or "mvp")))
        print("(dry run: nothing executed)")
        return 0
    _setup_cli_logging(args.verbose)
    opts = RunOptions(as_of=_date(args.as_of), params=_job_params(args), trigger=args.trigger, force=args.force,
                      resume=args.resume, max_workers=args.max_workers, wait_for_dags=not args.no_wait)
    rep = JobRunner(reg).run_dag(args.dag, opts)
    return _print_report(rep, args.json)


def cmd_jobs_status(args: argparse.Namespace) -> int:
    from app.scheduler.runner import JobRunStore

    df = JobRunStore().recent(args.limit, args.job, args.status, args.dag_run)
    if args.json:
        _print_json(df.to_dicts())
        return 0
    if df.is_empty():
        print("no job runs recorded yet")
        return 0
    print(f"{'started_at (UTC)':<20} {'job_id':<26} {'status':<8} {'att':<3} {'dur_s':>8} {'rows':>9}  run_id / error")
    for r in df.iter_rows(named=True):
        dur = f"{r['duration_s']:.1f}" if r["duration_s"] is not None else "-"
        err = f"  {r['error_type']}: {(r['error_message'] or '')[:80]}" if r["error_type"] else ""
        print(f"{r['started_at'].strftime('%Y-%m-%d %H:%M:%S'):<20} {r['job_id']:<26} {r['status']:<8} "
              f"{r['attempt']:<3} {dur:>8} {r['rows_written'] if r['rows_written'] is not None else '-':>9}  {r['run_id']}{err}")
    return 0


def cmd_jobs_enqueue(args: argparse.Namespace) -> int:
    from app.core.jobqueue import JobNotFound, enqueue_job

    try:
        req = enqueue_job(args.job, params=_kv(args.param), include_downstream=args.include_downstream, trigger="manual")
    except JobNotFound:
        print(f"unknown job {args.job!r}", file=sys.stderr)
        return 1
    print(f"queued {args.job}: request_id={req['request_id']} run_id={req['run_id']} (the worker picks it up)")
    return 0


def cmd_jobs_queue(args: argparse.Namespace) -> int:
    from app.core.jobqueue import list_requests

    out = {st: list_requests(st) for st in ("pending", "processing")}
    if args.json:
        _print_json(out)
        return 0
    for st, reqs in out.items():
        print(f"{st}: {len(reqs)}")
        for r in reqs:
            print(f"  {r.get('requested_at')}  {r.get('kind')} {r.get('job_id') or r.get('dag')}  run_id={r.get('run_id')}")
    return 0


# --------------------------------------------------------------------------------------------- other commands


def cmd_worker_start(args: argparse.Namespace) -> int:
    from app.scheduler.worker import main as worker_main

    argv = []
    if args.once:
        argv.append("--once")
    if args.no_schedule:
        argv.append("--no-schedule")
    if getattr(args, "realtime", False):
        argv.append("--realtime")
    return worker_main(argv)


def cmd_dq_run(args: argparse.Namespace) -> int:
    from app.core.dq import blocking_failures, default_checks, run_checks
    from app.scheduler.base import new_run_id

    _setup_cli_logging(args.verbose)
    run_id = new_run_id()
    outcomes = run_checks(default_checks(tables=args.tables), run_id, write=not args.no_write)
    shown = [o for o in outcomes if not o.passed and o.check_id != "has_data"] if not args.all else outcomes
    no_data = sorted(o.table_name for o in outcomes if o.check_id == "has_data")
    for o in shown:
        print(f"{'PASS' if o.passed else 'FAIL'} {o.severity:<5} {o.table_name:<26} {o.check_id:<40} "
              f"metric={o.metric_value} thr={o.threshold}")
    errs = blocking_failures(outcomes)
    print(f"dq run {run_id}: {len(outcomes)} results, {len(errs)} blocking errors, "
          f"{sum(1 for o in outcomes if o.severity == 'warn' and not o.passed)} warnings, "
          f"{len(no_data)} tables without data{' (not written)' if args.no_write else ''}")
    return 1 if errs else 0


def cmd_dq_summary(args: argparse.Namespace) -> int:
    from app.core.dq import dq_summary

    summ = dq_summary()
    if args.json:
        _print_json(summ)
    else:
        print(f"run {summ['run_id']} at {summ['checked_at']}  blocking={summ['blocking']}  counts={summ['counts']}")
        for f in summ["failed"]:
            print(f"  {f['severity']:<5} {f['table_name']:<26} {f['check_id']:<40} metric={f['metric_value']}")
    return 0


def cmd_alerts_eval(args: argparse.Namespace) -> int:
    from app.scheduler.alerts import evaluate_and_store
    from app.scheduler.runner import default_as_of

    _setup_cli_logging(args.verbose)
    events = evaluate_and_store(_date(args.as_of) or default_as_of())
    print(f"{len(events)} alert events fired")
    for e in events:
        print(f"  {e['security_id']}  {e['message']}")
    return 0


def cmd_serve_api(args: argparse.Namespace) -> int:
    import uvicorn

    from app.core.config import get_settings

    s = get_settings()
    # O4: our own JSON access log (app.api.observability) replaces uvicorn's when BIG360_ACCESS_LOG is on;
    # proxy headers for a reverse proxy / gateway (root_path is applied by create_app; docs/CONFIGURATION.md)
    uvicorn.run("app.api.main:app", host=args.host or s.api_host, port=args.port or s.api_port, reload=args.reload,
                log_level="info", access_log=not s.access_log,
                proxy_headers=s.proxy_headers, forwarded_allow_ips=s.forwarded_allow_ips)
    return 0


def cmd_db_build_serving(args: argparse.Namespace) -> int:
    from app.scheduler.runner import JobRegistry, JobRunner, RunOptions, check_available

    reg = JobRegistry.load()
    spec = reg.jobs.get("build_serving_db")
    if spec is None:
        print("job build_serving_db is not in contracts/jobs.yaml", file=sys.stderr)
        return 1
    ok, why = check_available(spec)
    if not ok:
        print(f"serving builder not available yet: {why}", file=sys.stderr)
        return 1
    _setup_cli_logging(args.verbose)
    rep = JobRunner(reg).run_jobs(["build_serving_db"], RunOptions(as_of=_date(args.as_of), force=True))
    return _print_report(rep, False)


# --------------------------------------------------------------------------------------------- doctor

KEY_HOSTS: list[tuple[str, str]] = [
    ("SEC EDGAR", "https://www.sec.gov/files/company_tickers.json"),
    ("SEC data API", "https://data.sec.gov/submissions/CIK0000320193.json"),
    ("Polygon/Massive", "https://api.polygon.io/v1/marketstatus/now"),
    ("FRED", "https://api.stlouisfed.org/fred/"),
    ("Yahoo Finance", "https://query1.finance.yahoo.com/"),
    ("GDELT files", "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"),
    ("filings.xbrl.org", "https://filings.xbrl.org/api/filings?page[size]=1"),
    ("EDINET", "https://api.edinet-fsa.go.jp/api/v2/documents.json"),
    ("OpenDART", "https://opendart.fss.or.kr/"),
    ("ECB data", "https://data-api.ecb.europa.eu/"),
    ("OpenFIGI", "https://api.openfigi.com/v3/mapping"),
    ("GLEIF", "https://api.gleif.org/api/v1/lei-records?page[size]=1"),
    ("CVM", "https://dados.cvm.gov.br/"),
    ("Hugging Face", "https://huggingface.co/api/models/ProsusAI/finbert"),
]


class _Doctor:
    def __init__(self) -> None:
        self.rows: list[dict[str, str]] = []

    def add(self, level: str, area: str, msg: str) -> None:
        self.rows.append({"level": level, "area": area, "message": msg})

    def ok(self, area: str, msg: str) -> None:
        self.add("OK", area, msg)

    def warn(self, area: str, msg: str) -> None:
        self.add("WARN", area, msg)

    def fail(self, area: str, msg: str) -> None:
        self.add("FAIL", area, msg)


def _check_network(doc: _Doctor, timeout: float = 6.0) -> None:
    from concurrent.futures import ThreadPoolExecutor

    import httpx

    from app.core.config import get_settings

    ua = get_settings().sec_user_agent

    def probe(item: tuple[str, str]) -> tuple[str, str, int | None, str | None]:
        name, url = item
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False, headers={"User-Agent": ua}) as c:
                r = c.head(url) if "sec.gov" not in url else c.get(url, headers={"Range": "bytes=0-0"})
                if r.status_code == 405:
                    r = c.get(url)
                return name, url.split("/")[2], r.status_code, None
        except Exception as e:  # noqa: BLE001
            return name, url.split("/")[2], None, type(e).__name__

    with ThreadPoolExecutor(max_workers=8) as ex:
        for name, host, status, err in ex.map(probe, KEY_HOSTS):
            if status is None:
                doc.warn("network", f"{name} ({host}): unreachable ({err})")
            elif status >= 500:
                doc.warn("network", f"{name} ({host}): HTTP {status}")
            else:
                doc.ok("network", f"{name} ({host}): reachable (HTTP {status})")


def cmd_doctor(args: argparse.Namespace) -> int:
    import importlib.metadata as md
    import re

    from app.core import config as cfg

    doc = _Doctor()
    s = cfg.get_settings()
    # ---- python / venv
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    (doc.ok if sys.version_info >= (3, 13) else doc.warn)("python", f"Python {sys.version.split()[0]} ({'venv' if in_venv else 'NOT a venv'}: {sys.prefix})")
    # ---- .env keys (presence only, never values)
    env_path = s.project_root / ".env"
    (doc.ok if env_path.exists() else doc.warn)("env", f".env {'found' if env_path.exists() else 'missing'} at project root")
    required = {"fred_apikey", "polygon_apikey"}
    secret_names = list(dict.fromkeys(list(cfg.SECRET_FIELDS) + [
        n for n, f in type(s).model_fields.items() if "SecretStr" in str(f.annotation)]))
    for name in secret_names:
        present = s.secret(name) is not None
        label = name.upper()
        if present:
            doc.ok("env", f"{label}: PRESENT")
        elif name in required:
            doc.fail("env", f"{label}: MISSING (required for nightly ingestion)")
        else:
            doc.warn("env", f"{label}: MISSING (optional)")
    ua = s.sec_user_agent or ""
    if "@" in ua and "example.com" not in ua:
        doc.ok("env", "SEC_USER_AGENT: PRESENT (contains a contact e-mail)")
    else:
        doc.fail("env", "SEC_USER_AGENT: MISSING or placeholder (SEC requires a real contact e-mail)")
    # ---- data dir + disk
    try:
        s.ensure_dirs()
        probe = s.data_dir / ".doctor_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        doc.ok("data", f"data dir writable: {s.data_dir}")
    except OSError as e:
        doc.fail("data", f"data dir not writable: {s.data_dir} ({e})")
    try:
        du = shutil.disk_usage(s.data_dir)
        free_gb = du.free / 1e9
        lvl = doc.ok if free_gb >= 20 else (doc.warn if free_gb >= 5 else doc.fail)
        lvl("disk", f"free {free_gb:.1f} GB of {du.total / 1e9:.0f} GB on data drive (MVP needs ~5 GB, full ~50+ GB)")
    except OSError as e:
        doc.warn("disk", f"cannot read disk usage: {e}")
    # ---- packages
    req = Path(__file__).resolve().parents[1] / "requirements.txt"
    missing, found = [], 0
    if req.exists():
        for line in req.read_text(encoding="utf-8").splitlines():
            line = line.split("#", 1)[0].strip()
            if not line or line.startswith("-"):
                continue
            name = re.split(r"[\[<>=!~; ]", line, maxsplit=1)[0]
            try:
                md.version(name)
                found += 1
            except md.PackageNotFoundError:
                missing.append(name)
    if missing:
        doc.fail("packages", f"missing: {', '.join(missing)}  (pip install -r backend/requirements.txt)")
    else:
        doc.ok("packages", f"{found} requirements installed")
    # ---- contracts + jobs
    try:
        from app.core.schema import load_contracts
        from app.scheduler.runner import JobRegistry, check_available

        n = len(load_contracts())
        reg = JobRegistry.load()
        avail = sum(check_available(j)[0] for j in reg.jobs.values())
        doc.ok("contracts", f"{n} table contracts; jobs.yaml valid ({len(reg.jobs)} jobs, {avail} with modules available)")
    except Exception as e:  # noqa: BLE001
        doc.fail("contracts", f"contracts/jobs.yaml problem: {e}")
    # ---- worker / serving
    try:
        from app.core.storage import current_serving_db
        from app.scheduler.worker import read_heartbeat, worker_is_running

        running = worker_is_running(s)
        hb = read_heartbeat(s) or {}
        doc.add("OK" if running else "WARN", "worker",
                f"worker {'running (pid ' + str(hb.get('pid')) + ')' if running else 'not running'}"
                + (f"; next runs {hb.get('next_runs')}" if running and hb.get("next_runs") else ""))
        cur = current_serving_db(s)
        doc.add("OK" if cur else "WARN", "serving", f"serving snapshot: {cur.name if cur else 'none yet (API serves lake views)'}")
    except Exception as e:  # noqa: BLE001
        doc.warn("worker", f"status unknown: {e}")
    # ---- frontend tooling
    node, npm = shutil.which("node"), shutil.which("npm")
    (doc.ok if node and npm else doc.warn)("frontend", f"node {'found' if node else 'NOT found'}, npm {'found' if npm else 'NOT found'} on PATH")
    nm = s.project_root / "frontend" / "node_modules"
    (doc.ok if nm.exists() else doc.warn)("frontend", f"frontend/node_modules {'present' if nm.exists() else 'missing (run npm install in frontend/)'}")
    # ---- network
    if not args.offline:
        _check_network(doc)
    if args.json:
        _print_json(doc.rows)
    else:
        for r in doc.rows:
            print(f"[{r['level']:^4}] {r['area']:<10} {r['message']}")
        n_fail = sum(r["level"] == "FAIL" for r in doc.rows)
        n_warn = sum(r["level"] == "WARN" for r in doc.rows)
        print(f"doctor: {n_fail} fail, {n_warn} warn, {len(doc.rows) - n_fail - n_warn} ok")
    return 1 if any(r["level"] == "FAIL" for r in doc.rows) else 0


# --------------------------------------------------------------------------------------------- parser


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--as-of", help="data date YYYY-MM-DD (default: previous business day, scheduler tz)")
    p.add_argument("--since", help="incremental start date passed to the job as params.since")
    p.add_argument("--universe", choices=["mvp", "full"], help="params.universe (also selects runtime estimates)")
    p.add_argument("--tickers", nargs="+", help="params.tickers (space or comma separated)")
    p.add_argument("--param", action="append", metavar="K=V", help="extra job param (JSON value or string)")
    p.add_argument("--dry-run", action="store_true", help="print the plan, execute nothing")
    p.add_argument("--force", action="store_true", help="re-run even if this run key already succeeded")
    p.add_argument("--max-workers", type=int, help="parallel jobs (default Settings.worker_max_jobs)")
    p.add_argument("--trigger", default="manual", choices=["manual", "backfill", "schedule", "api", "dependency"])
    p.add_argument("--json", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m app.cli", description="BiG Equity 360 platform CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="environment check")
    p.add_argument("--offline", action="store_true", help="skip network reachability")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_doctor)

    jobs = sub.add_parser("jobs", help="job registry / runs").add_subparsers(dest="jobs_cmd", required=True)
    p = jobs.add_parser("list")
    p.add_argument("--dag")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs_list)
    p = jobs.add_parser("plan", help="print the execution plan of a DAG or job(s)")
    p.add_argument("target", nargs="+", help="dag name or job id(s)")
    p.add_argument("--universe", choices=["mvp", "full"], default="mvp")
    p.add_argument("--dry-run", action="store_true", help="(plans never execute; accepted for symmetry)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs_plan)
    p = jobs.add_parser("run", help="run job(s) now in this process")
    p.add_argument("job", nargs="+")
    _add_run_args(p)
    p.add_argument("--with-upstream", action="store_true")
    p.add_argument("--with-downstream", action="store_true")
    p.add_argument("--retries", type=int)
    p.set_defaults(fn=cmd_jobs_run)
    p = jobs.add_parser("run-dag", help="run a whole DAG now in this process")
    p.add_argument("dag")
    _add_run_args(p)
    p.add_argument("--resume", action="store_true", help="continue the latest run of this DAG")
    p.add_argument("--no-wait", action="store_true", help="do not wait for prerequisite DAGs (weekly waits for nightly)")
    p.set_defaults(fn=cmd_jobs_run_dag)
    p = jobs.add_parser("status", help="recent job_runs")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--job")
    p.add_argument("--status")
    p.add_argument("--dag-run")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs_status)
    p = jobs.add_parser("enqueue", help="queue a job for the worker (like the API trigger)")
    p.add_argument("job")
    p.add_argument("--param", action="append", metavar="K=V")
    p.add_argument("--include-downstream", action="store_true")
    p.set_defaults(fn=cmd_jobs_enqueue)
    p = jobs.add_parser("queue", help="show queued requests")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_jobs_queue)

    p = sub.add_parser("run-job", help="alias: jobs run <job>")
    p.add_argument("job", nargs="+")
    _add_run_args(p)
    p.add_argument("--with-upstream", action="store_true")
    p.add_argument("--with-downstream", action="store_true")
    p.add_argument("--retries", type=int)
    p.set_defaults(fn=cmd_jobs_run)

    w = sub.add_parser("worker").add_subparsers(dest="worker_cmd", required=True)
    p = w.add_parser("start")
    p.add_argument("--once", action="store_true")
    p.add_argument("--no-schedule", action="store_true")
    p.add_argument("--realtime", action="store_true", help="also run the realtime supervisor (R1) in this process")
    p.set_defaults(fn=cmd_worker_start)

    from app.realtime.cli import add_parser as _realtime_parser  # owner: realtime
    _realtime_parser(sub)

    d = sub.add_parser("dq").add_subparsers(dest="dq_cmd", required=True)
    p = d.add_parser("run")
    p.add_argument("--tables", nargs="+")
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--all", action="store_true", help="print passing checks too")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_dq_run)
    p = d.add_parser("summary")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_dq_summary)

    a = sub.add_parser("alerts").add_subparsers(dest="alerts_cmd", required=True)
    p = a.add_parser("eval")
    p.add_argument("--as-of")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_alerts_eval)

    sv = sub.add_parser("serve").add_subparsers(dest="serve_cmd", required=True)
    p = sv.add_parser("api")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(fn=cmd_serve_api)

    db = sub.add_parser("db").add_subparsers(dest="db_cmd", required=True)
    p = db.add_parser("build-serving")
    p.add_argument("--as-of")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_db_build_serving)
    return ap


def main(argv: list[str] | None = None) -> int:
    if os.name == "nt":  # box-drawing / non-ASCII names in a cp1252 console
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:
                pass
    args = build_parser().parse_args(argv)
    args.verbose = getattr(args, "verbose", False)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())

