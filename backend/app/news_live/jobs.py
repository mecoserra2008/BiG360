"""Job entry points of the `hourly` DAG (contracts/jobs.yaml; roadmap v3 N3):

    news_hourly    app.news_live.jobs:hourly     fold closed hours of the hot store into the lake (+ XLM-R)
    news_finalise  app.news_live.jobs:finalise   daily finalise of complete UTC days (no-op until a day is due)
    news_intraday  app.news_live.jobs:intraday   rebuild the 7-day intraday serving DuckDB (hot swap in the API)

All three are idempotent; the runner's run-key idempotency is bypassed by the schedule entry (force), every run
looks at the inputs (fingerprints / checkpoints in data/state/news_live.json) and does only what changed.
"""

from __future__ import annotations

from datetime import date
from typing import Any


def _bool(x: Any, default: bool = True) -> bool:
    if x is None:
        return default
    return str(x).lower() not in ("0", "false", "no", "off")


def hourly(ctx):  # noqa: ANN001 - JobContext
    from app.news_live.compaction import run_hourly  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    cancel = getattr(ctx, "cancel_event", None)
    rep = run_hourly(ctx.settings, max_hours=int(p.get("max_hours", 30)), xlmr=_bool(p.get("xlmr")),
                     max_xlmr=int(p.get("max_xlmr", 1500)), stop=(cancel.is_set if cancel is not None else None))
    errs = [h for h in rep.get("hours", []) if h.get("error")] + \
        [d for d in rep.get("days", {}).values() if isinstance(d, dict) and d.get("error")]
    ok = len(rep.get("hours", [])) - len([h for h in rep.get("hours", []) if h.get("error")])
    rows = {"event_hourly_agg": sum(int(d.get("event_hourly_agg") or 0) for d in rep.get("days", {}).values()
                                    if isinstance(d, dict)),
            "sentiment_hourly": sum(int(d.get("sentiment_hourly") or 0) for d in rep.get("days", {}).values()
                                    if isinstance(d, dict))}
    status = "success" if not errs else ("partial" if ok else "failed")
    return JobResult(status=status, rows_written=rows, entities_ok=ok, entities_failed=len(errs),
                     message=str({k: rep.get(k) for k in ("message", "seconds", "xlmr_scored")}
                                 | {"hours": [f"{h.get('day')}T{h.get('hour'):02d}" for h in rep.get("hours", [])
                                              if h.get("hour") is not None][-30:]})[:2000])


def finalise(ctx):  # noqa: ANN001
    from app.news_live.compaction import run_finalise  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    days = [date.fromisoformat(x) for x in p["days"]] if p.get("days") else None
    cancel = getattr(ctx, "cancel_event", None)
    rep = run_finalise(ctx.settings, days=days, assemble=_bool(p.get("assemble")), sentiment=_bool(p.get("sentiment")),
                       stop=(cancel.is_set if cancel is not None else None), force=_bool(p.get("force"), False))
    if rep.get("held") or rep.get("already_running"):
        why = "held (data/state/ops/news_finalise.HOLD)" if rep.get("held") else f"already running: {rep['already_running']}"
        return JobResult(status="skipped", message=f"news finalise {why}")
    errs = [d for d in rep["days"].values() if d.get("error")]
    status = "success" if not errs else ("partial" if len(errs) < len(rep["days"]) else "failed")
    return JobResult(status=status, rows_written={"sentiment_label_daily": sum(
        int(d.get("sentiment_label_daily") or 0) for d in rep["days"].values())},
        entities_ok=len(rep["days"]) - len(errs), entities_failed=len(errs),
        message=str({"days": {k: {x: v.get(x) for x in ("assembly", "merge", "seconds", "error")}
                              for k, v in rep["days"].items()}, "retention": rep.get("retention"),
                     "deferred": rep.get("deferred")})[:2000])


def intraday(ctx):  # noqa: ANN001
    from app.news_live.intraday import build_intraday  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = build_intraday(ctx.settings, days=int(p.get("days", 7)))
    return JobResult(status="success", rows_written={k: int(v) for k, v in rep.items() if k.startswith("nt_")},
                     message=str(rep)[:2000])
