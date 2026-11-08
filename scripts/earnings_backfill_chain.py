"""Roadmap #6 (earnings) history backfill chain - run detached; idempotent and resumable (re-run continues: Yahoo
dates fetched within 7 days are skipped, the other steps overwrite / upsert).

  1. Yahoo earnings dates for mvp_us, mvp_global, eu_broad (+ JPX schedule)
  2. earnings_events (from the landed SEC submissions; no SEC requests) -> reactions -> expectations (backfill) -> signals
  3. Yahoo dates for the rest of the `full` universe (long; provider dates only), then step 2 again

Usage (from backend/):  ..\\.venv\\Scripts\\python.exe ..\\scripts\\earnings_backfill_chain.py [--skip-full]
Log: data/logs/roadmap_v2/e6_chain.log ; state: data/state/earnings.json (key backfill_chain)
"""

from __future__ import annotations

import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import get_settings  # noqa: E402
from app.earnings import common as C  # noqa: E402

LOG = ROOT / "data" / "logs" / "roadmap_v2" / "e6_chain.log"


def say(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}\n")


def step(name: str, fn, *a, **kw):
    t0 = time.perf_counter()
    say(f"start {name}")
    try:
        rep = fn(*a, **kw)
        say(f"end {name} ({time.perf_counter() - t0:.0f}s): {str(rep)[:1500]}")
        C.update_state("backfill_chain", {"last_step": name, "at": datetime.now(UTC).isoformat(timespec="seconds")})
        return rep
    except Exception:  # noqa: BLE001 - keep going with the next step
        say(f"FAILED {name}: {traceback.format_exc()[-2000:]}")
        return None


def analytics(s) -> None:
    from app.earnings.events import build_all
    from app.earnings.expectations import run_expectations
    from app.earnings.reactions import run_reactions
    from app.earnings.signals import run_signals

    step("earnings_events", build_all, s, {"refresh_days": 0})
    step("earnings_reactions", run_reactions, s, {})
    step("earnings_expectations", run_expectations, s, {"mode": "backfill", "since": "2016-01-01", "workers": 6, "max_minutes": 120})
    step("earnings_signals", run_signals, s, {})


def main() -> None:
    from app.earnings.fetch import run_fetch

    s = get_settings()
    say("chain start")
    if "--full-only" not in sys.argv:
        step("dates_priority", run_fetch, s, {"universes": "mvp_us,mvp_global,eu_broad", "max_minutes": 240})
        analytics(s)
    if "--skip-full" not in sys.argv:
        step("dates_full", run_fetch, s, {"universes": "full", "max_minutes": 600, "jpx": "false"})
        analytics(s)
    say("chain done")


if __name__ == "__main__":
    main()
