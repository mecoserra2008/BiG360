"""Job `events_agg` (E3 T1): recompute attention_z / tone_z of event_daily_agg and compact T1 months.

params: days (default 7: recompute the last N days), full (true: every day in T1, month by month - run after a
backfill so newest-first batches converge), compact (default true: merge T1 day files of months older than 35 days
into one sorted month file; T1 is kept forever, nothing is pruned).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core.config import Settings, get_settings
from app.events_v2 import store
from app.events_v2.pipeline import recompute_z


def t1_range(settings: Settings | None = None) -> tuple[date, date] | None:
    ms = store.months("event_daily_agg", settings)
    if not ms:
        return None
    first = date.fromisoformat(ms[0] + "-01")
    last_m = date.fromisoformat(ms[-1] + "-01")
    df = store.read_range("event_daily_agg", first, last_m + timedelta(days=31), settings, columns=["date"])
    if df.is_empty():
        return None
    return df.get_column("date").min(), df.get_column("date").max()


def run_agg(settings: Settings | None = None, days: int = 7, full: bool = False, compact: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    # refresh-pipeline fixes 2026-10-26: repair months whose files overlap / hold duplicate keys before anything reads
    # them (a d- file left next to its month copy by an interrupted write must never be recomputed twice)
    healed = store.heal_overlaps("event_daily_agg", s)
    rng = t1_range(s)
    if rng is None:
        return {"rows": 0, "compacted": 0, "healed": healed}
    lo, hi = rng
    n = 0
    if full:
        d = lo
        while d <= hi:  # month chunks keep memory bounded (history window is read per chunk)
            e = min(hi, date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1))
            n += recompute_z(d, e, s)
            d = e + timedelta(days=1)
    else:
        n += recompute_z(max(lo, hi - timedelta(days=days - 1)), hi, s)
    comp = 0
    if compact:
        cutoff = (datetime.now(UTC).date() - timedelta(days=35)).strftime("%Y-%m")
        for ym in store.months("event_daily_agg", s):
            if ym < cutoff and not store.is_compacted("event_daily_agg", ym, s):
                store.compact_month("event_daily_agg", ym, None, s)
                comp += 1
    return {"rows": n, "compacted": comp, "range": [lo.isoformat(), hi.isoformat()], "healed": healed}


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_agg(ctx.settings, days=int(p.get("days", 7)), full=str(p.get("full", "false")).lower() in ("1", "true", "yes"),
                  compact=str(p.get("compact", "true")).lower() not in ("0", "false", "no"))
    return JobResult(status="success", rows_written={"event_daily_agg": rep["rows"]}, message=str(rep))
