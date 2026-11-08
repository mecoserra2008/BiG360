"""Regression-guard orchestration: job `qa_regression` and `python -m app.qa`.

Runs the API contract sweep and the golden checks against the running API (or an in-process client over the current
serving snapshot when no API is up), then the UI smoke suite when Chrome and the UI are reachable. Writes
data/reports/qa/<run>/{qa.json, qa.html} (+ the smoke report under data/reports/smoke/<run>/) and the summary to
data/state/ops/qa_regression.json, which the System / Data Health pages show and which raises the `qa_regression`
ops alert on failure.

Alert hygiene 2026-10-30:
* triggers: after the weekly refresh (as before, all suites) AND after every startup / interval sync that published
  a snapshot (app.ops.sync.run, params suites=auto): the API sweep + golden checks every time, the UI smoke at most
  once a day (SMOKE_MIN_INTERVAL_H) and never while a heavy job holds the heavy slot or free RAM < SMOKE_MIN_FREE_GB
  (plan_suites);
* the state file is MERGED per suite: a run that did not include a suite keeps that suite's previous result with its
  own run id / time, so results age out (app.qa.common.suite_views / qa_alert: superseded_by, stale, age_h).
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.qa.common import (
    ApiClient,
    find_live_api,
    merged_status,
    read_state,
    reports_dir,
    run_stamp,
    suite_views,
    utc_now,
    write_json,
    write_state,
)

log = get_logger(__name__)
SUITES = ("api", "golden", "smoke")
SMOKE_MIN_INTERVAL_H = 24.0
SMOKE_MIN_FREE_GB = 4.0
RUNS_KEEP = 20


def plan_suites(settings: Settings | None = None, now: Any = None, state: dict[str, Any] | None = None,
                free_gb: float | None = None, heavy: list[str] | None = None) -> tuple[tuple[str, ...], str | None]:
    """suites=auto (post-sync): ("api", "golden") always, + "smoke" when the last UI smoke is at least
    SMOKE_MIN_INTERVAL_H old, no other heavy / exclusive job holds the heavy slot and free RAM >= SMOKE_MIN_FREE_GB.
    Returns (suites, why the smoke was left out or None)."""
    s = settings or get_settings()
    now = now or utc_now()
    st = state if state is not None else (read_state(s) or {})
    sm = (st.get("suites") or {}).get("smoke") or {}
    last = sm.get("finished_at") or (st.get("finished_at") if sm else None)
    from app.qa.common import _ts  # noqa: PLC0415

    t = _ts(last) if sm.get("status") not in (None, "skipped") else None
    if t is not None and now - t < timedelta(hours=SMOKE_MIN_INTERVAL_H):
        return ("api", "golden"), (f"UI smoke ran {(now - t).total_seconds() / 3600:.1f} h ago "
                                   f"(at most once every {SMOKE_MIN_INTERVAL_H:g} h)")
    if heavy is None:
        try:
            from app.ops import heavy_slot as HS  # noqa: PLC0415

            heavy = sorted({h.get("job_id") for h in HS.holders("heavy", s) + HS.holders("exclusive", s)
                            if h.get("job_id") and h.get("job_id") != "qa_regression"})
        except Exception:  # noqa: BLE001
            heavy = []
    if heavy:
        return ("api", "golden"), f"UI smoke skipped: heavy job running ({', '.join(heavy[:4])})"
    if free_gb is None:
        try:
            from app.ops import winsys  # noqa: PLC0415

            free_gb = (winsys.memory_status() or {}).get("avail_gb")
        except Exception:  # noqa: BLE001
            free_gb = None
    if free_gb is not None and float(free_gb) < SMOKE_MIN_FREE_GB:
        return ("api", "golden"), f"UI smoke skipped: free RAM {float(free_gb):.1f} GB < {SMOKE_MIN_FREE_GB:g} GB"
    return ("api", "golden", "smoke"), None


def merge_state(prev: dict[str, Any] | None, summary: dict[str, Any]) -> dict[str, Any]:
    """New state = this run's suites + the previous results of the suites it did not run (or only skipped), each
    tagged with its own run_id / finished_at; issues likewise; a short run history."""
    prev = prev or {}
    p_suites = {k: {**v, "run_id": v.get("run_id") or prev.get("run_id"),
                    "finished_at": v.get("finished_at") or prev.get("finished_at")}
                for k, v in (prev.get("suites") or {}).items()}
    p_issues = [{**i, "run_id": i.get("run_id") or prev.get("run_id")} for i in prev.get("issues") or []]
    suites = dict(p_suites)
    carried = set(p_suites)
    for k, v in (summary.get("suites") or {}).items():
        old = p_suites.get(k)
        if v.get("status") == "skipped" and old and old.get("status") != "skipped":
            suites[k] = {**old, "last_skip": {"run_id": summary["run_id"], "at": summary.get("finished_at"),
                                              "message": v.get("message")}}
            continue
        suites[k] = {**v, "run_id": summary["run_id"], "finished_at": summary.get("finished_at")}
        carried.discard(k)
    issues = [i for i in p_issues if i.get("suite") in carried] + \
        [{**i, "run_id": summary["run_id"]} for i in summary.get("issues") or []]
    runs = ([{"run_id": summary["run_id"], "finished_at": summary.get("finished_at"),
              "status": summary.get("status"), "trigger": summary.get("trigger"),
              "suites": sorted((summary.get("suites") or {}).keys())}] + list(prev.get("runs") or []))[:RUNS_KEEP]
    out = {**summary, "suites": suites, "issues": issues[:50], "runs": runs, "run_status": summary.get("status")}
    out["status"] = merged_status(suite_views(out))
    return out


def _summ(r: dict[str, Any]) -> dict[str, Any]:
    keep = ("status", "failed", "counts", "message", "operations", "checks", "loads_total", "routes", "served_version",
            "contract_version", "client", "ui_url", "report_dir", "latency_ms", "load_ms")
    return {k: r.get(k) for k in keep if r.get(k) is not None}


def top_issues(results: dict[str, dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in (results.get("api") or {}).get("results") or []:
        if r["status"] == "failed":
            out.append({"suite": "api", "item": r["operation_id"], "url": r.get("url"), "detail": "; ".join(r["issues"])[:300]})
    for r in (results.get("golden") or {}).get("results") or []:
        if r["status"] == "failed":
            out.append({"suite": "golden", "item": r["id"], "detail": r["message"]})
    for r in (results.get("smoke") or {}).get("loads") or []:
        if r["status"] == "failed":
            item = r["route"] + (f" › {r['tab']}" if r.get("tab") else "") + f" ({r['theme']})"
            out.append({"suite": "smoke", "item": item, "detail": "; ".join(r["issues"])[:300]})
    return out[:limit]


def run_all(settings: Settings | None = None, suites: tuple[str, ...] = SUITES, api_url: str | None = None,
            ui_url: str | None = None, themes: tuple[str, ...] = ("dark", "light"), smoke_tabs: bool = True,
            progress: Any = None, write: bool = True, trigger: str = "manual",
            skipped: dict[str, str] | None = None) -> dict[str, Any]:
    """Run `suites`; `skipped` = {suite: reason} for suites the caller left out on purpose (recorded, the previous
    result of that suite is kept by merge_state)."""
    from app.qa.common import overall_status  # noqa: PLC0415

    s = settings or get_settings()
    t0 = utc_now()
    stamp = run_stamp(t0)
    base = api_url or find_live_api(s)
    results: dict[str, dict[str, Any]] = {}
    client = ApiClient(base)
    try:
        if "api" in suites:
            from app.qa.api_sweep import run_sweep

            results["api"] = run_sweep(client, s, progress=progress)
        if "golden" in suites:
            from app.qa.golden import run_golden

            results["golden"] = run_golden(client)
    finally:
        client.close()
    if "smoke" in suites:
        from app.qa.smoke import run_smoke

        if base is None and ui_url is None:
            results["smoke"] = {"suite": "smoke", "status": "skipped", "failed": 0,
                                "message": "no running API / UI (in-process API sweep only)"}
        else:
            results["smoke"] = run_smoke(s, api_url=base, ui_url=ui_url, themes=themes, tabs=smoke_tabs,
                                         progress=progress)
    status = overall_status([r.get("status") for r in results.values()])
    summary = {
        "run_id": f"QA{stamp}", "status": status, "started_at": t0.isoformat(), "finished_at": utc_now().isoformat(),
        "api_base_url": base, "client": "live" if base else "in_process", "trigger": trigger,
        "suites": {k: _summ(v) for k, v in results.items()},
        "issues": top_issues(results),
        "message": f"{status}: " + ", ".join(f"{k} {v.get('status')}" for k, v in results.items())
                   + "".join(f"; {k} not run: {why}" for k, why in (skipped or {}).items()),
        "skipped": dict(skipped or {}),
    }
    if write:
        d = reports_dir("qa", s) / stamp
        write_json(d / "qa.json", {"summary": summary, **results})
        (d / "qa.html").write_text(html_summary(summary, results), encoding="utf-8")
        summary["report"] = str(d / "qa.html")
        write_json(reports_dir("qa", s) / "latest.json", summary)
        write_state(merge_state(read_state(s), summary), s)
    return summary


def html_summary(summary: dict[str, Any], results: dict[str, dict[str, Any]]) -> str:
    def esc(x: Any) -> str:
        return html.escape(str(x if x is not None else ""))

    parts = [f"<h1>Regression guard: {esc(summary['status'])}</h1><p>{esc(summary['run_id'])} · {esc(summary['started_at'])}"
             f" · API {esc(summary.get('api_base_url') or 'in-process')}</p>"]
    for k, r in results.items():
        parts.append(f"<h2>{esc(k)}: {esc(r.get('status'))}</h2><p>{esc(json.dumps(r.get('counts') or {}))} "
                     f"{esc(r.get('message') or '')}</p>")
        rows = []
        if k == "api":
            for x in r.get("results") or []:
                if x["status"] != "passed":
                    rows.append((x["status"], x["operation_id"], x.get("url") or x["path"], "; ".join(x["issues"])))
        elif k == "golden":
            rows = [(x["status"], x["id"], x.get("observation_date") or "", x["message"]) for x in r.get("results") or []]
        elif k == "smoke":
            for x in r.get("loads") or []:
                if x["status"] != "passed":
                    rows.append((x["status"], x["route"] + (f" › {x['tab']}" if x.get("tab") else ""), x["theme"],
                                 "; ".join(x["issues"])))
        if rows:
            parts.append("<table>" + "".join(
                f'<tr class="{esc(a)}"><td>{esc(a)}</td><td>{esc(b)}</td><td>{esc(c)}</td><td>{esc(d)}</td></tr>'
                for a, b, c, d in rows) + "</table>")
    css = ("body{font:13px system-ui;margin:16px;background:#fff;color:#111}table{border-collapse:collapse;width:100%}"
           "td{border:1px solid #ddd;padding:3px 6px;vertical-align:top}tr.failed td{background:#fde8e8}"
           "tr.warn td{background:#fff6dd}@media (prefers-color-scheme: dark){body{background:#111;color:#eee}"
           "td{border-color:#333}tr.failed td{background:#3a1414}tr.warn td{background:#3a2e0e}}")
    return f'<!doctype html><html><head><meta charset="utf-8"><title>Regression guard</title><style>{css}</style></head><body>{"".join(parts)}</body></html>'


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `qa_regression` (after the refresh, soft-depends on alerts_eval). params: suites (list), themes (list),
    smoke_tabs (bool), api_url / ui_url (override discovery). A failed suite -> job status partial (the run itself
    worked; the ops alert carries the failure) so downstream jobs are never skipped because of it."""
    from app.scheduler.base import JobResult

    p = ctx.params or {}
    skipped: dict[str, str] = {}
    if p.get("suites") == "auto":    # alert hygiene 2026-10-30: post-sync guard (API + golden; smoke once a day)
        suites, why = plan_suites(ctx.settings)
        if why:
            skipped["smoke"] = why
            log.info("qa_regression (auto): %s", why)
    else:
        suites = tuple(p.get("suites") or SUITES)
    themes = tuple(p.get("themes") or ("dark", "light"))
    summary = run_all(ctx.settings, suites=suites, api_url=p.get("api_url"), ui_url=p.get("ui_url"), themes=themes,
                      smoke_tabs=bool(p.get("smoke_tabs", True)), trigger=str(p.get("trigger") or ctx_trigger(ctx)),
                      skipped=skipped)
    n_failed = sum(int((v or {}).get("failed") or 0) for v in summary["suites"].values())
    status = {"passed": "success", "warn": "success", "skipped": "skipped", "failed": "partial"}[summary["status"]]
    return JobResult(status=status, rows_written={}, entities_ok=len(summary["suites"]), entities_failed=n_failed,
                     watermark=ctx.as_of.isoformat(), message=summary["message"][:500], retryable=False)


def ctx_trigger(ctx: Any) -> str:
    """refresh (a scheduled DAG run) | sync | manual, from the job context's DAG run id."""
    rid = str(getattr(ctx, "dag_run_id", None) or "")
    if rid.endswith("-sync"):
        return "sync"
    return "refresh" if rid else "manual"


def latest_report_path(settings: Settings | None = None) -> Path | None:
    d = reports_dir("qa", settings)
    runs = sorted(x for x in d.iterdir() if x.is_dir())
    return runs[-1] / "qa.json" if runs else None
