"""CLI for the regression guard (run from backend/ with the project venv):

    python -m app.qa                       # all suites against the running API (+ UI smoke when Chrome / UI are up)
    python -m app.qa api golden            # only the API sweep and the golden checks
    python -m app.qa smoke --ui http://127.0.0.1:5196 --themes dark --no-tabs
    python -m app.qa --api http://127.0.0.1:8010 --no-write

Exit code: 0 passed / warn / skipped, 1 failed. Reports: data/reports/qa/<run>/qa.html (+ data/reports/smoke/<run>/).
"""

from __future__ import annotations

import argparse
import sys

from app.qa.regression import SUITES, run_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.qa", description="BiG Equity 360 regression guard (read-only)")
    ap.add_argument("suites", nargs="*", help="api, golden, smoke (default all)")
    ap.add_argument("--api", help="API base URL (default: first live of BIG360_QA_API_URL, :api_port, :8010, :8000)")
    ap.add_argument("--ui", help="UI base URL (default: BIG360_UI_URL, the API origin in app mode, localhost:5173)")
    ap.add_argument("--themes", default="dark,light", help="comma list (dark,light)")
    ap.add_argument("--no-tabs", action="store_true", help="smoke: do not click the tabs")
    ap.add_argument("--no-write", action="store_true", help="do not write reports / state (dry run)")
    ap.add_argument("-q", "--quiet", action="store_true")
    a = ap.parse_args(argv)
    bad = [x for x in a.suites if x not in SUITES]
    if bad:
        ap.error(f"unknown suite(s) {bad}; choose from {', '.join(SUITES)}")

    def progress(i: int, n: int, r) -> None:  # noqa: ANN001
        if a.quiet:
            return
        if isinstance(r, dict):
            label = r.get("route", "") + (f" > {r['tab']}" if r.get("tab") else "") + f" [{r.get('theme')}]"
            st, issues = r.get("status"), r.get("issues") or []
        else:
            label, st, issues = r.operation_id, r.status, r.issues
        if st != "passed":
            print(f"[{i}/{n}] {st:7s} {label}: {'; '.join(issues)[:240]}", flush=True)

    summary = run_all(suites=tuple(a.suites or SUITES), api_url=a.api, ui_url=a.ui,
                      themes=tuple(t for t in a.themes.split(",") if t), smoke_tabs=not a.no_tabs,
                      progress=progress, write=not a.no_write)
    print(summary["message"])
    for k, v in summary["suites"].items():
        print(f"  {k:7s} {v.get('status'):8s} {v.get('counts') or ''} {v.get('message') or ''}")
    if summary.get("report"):
        print(f"report: {summary['report']}")
    return 1 if summary["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
