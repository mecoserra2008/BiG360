"""UI smoke suite (O2.3 regression guard): headless Chrome over every route / tab, both themes. Read-only.

    .venv\\Scripts\\python.exe scripts\\smoke_ui.py                       # UI auto-discovered, API auto-discovered
    .venv\\Scripts\\python.exe scripts\\smoke_ui.py --ui http://127.0.0.1:5196 --api http://127.0.0.1:8010
    .venv\\Scripts\\python.exe scripts\\smoke_ui.py --themes dark --no-tabs --route /macro/markets

Report: data/reports/smoke/<run>/smoke.html + smoke.json (screenshots of failed loads). The full guard (API sweep +
golden checks + this suite) is `python -m app.qa` from backend/, or the job qa_regression.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.qa.common import find_live_api  # noqa: E402
from app.qa.smoke import run_smoke  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ui", help="UI base URL")
    ap.add_argument("--api", help="API base URL (route ids are resolved through it)")
    ap.add_argument("--themes", default="dark,light")
    ap.add_argument("--no-tabs", action="store_true")
    ap.add_argument("--route", action="append", help="only these routes (repeatable)")
    a = ap.parse_args()

    def progress(i: int, n: int, r: dict) -> None:
        if r["status"] != "passed":
            label = r["route"] + (f" > {r['tab']}" if r.get("tab") else "")
            print(f"[{i}/{n}] {r['status']:7s} {label} [{r['theme']}] {r['load_ms']} ms: {'; '.join(r['issues'])[:240]}",
                  flush=True)

    res = run_smoke(api_url=a.api or find_live_api(), ui_url=a.ui, themes=tuple(t for t in a.themes.split(",") if t),
                    routes=a.route, tabs=not a.no_tabs, progress=progress)
    print(f"smoke {res['status']}: {res.get('counts') or res.get('message')}  report: {res.get('report_dir')}")
    return 1 if res["status"] == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
