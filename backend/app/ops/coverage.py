"""contracts/coverage.yaml loader + validation (O1.1).

    reg = load_coverage()          # {"defaults", "sources", "datasets": {id: spec}, "campaigns": {id: spec}, "known_gaps"}
    validate_coverage(reg, job_ids)  -> list of problems (tests / CLI)
    effective_sla(spec, profile_sla_days)
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from app.core.config import Settings, get_settings
from app.ops.paths import read_yaml

CHECKS = ("sessions", "live_news", "currency_dates", "macro_releases", "latest_ingest", "gdelt_days", "daily_counts", "latest_date",
          "cftc_weekly", "quarterly_13f", "finra_settlements", "sentinel_rows", "unpriced_securities",
          "conformal_coverage", "published_dates", "register_coverage")
FILL_KINDS = ("polygon_dates", "yf_refetch", "job", "gdelt_redo", "campaign", "dag", "none")
CAMPAIGN_KINDS = ("module", "job", "script", "prices_history", "gap_queue")
GAP_CLASSES = ("missing", "stale", "partial", "suspicious", "unfillable")


def _load(path: str) -> dict[str, Any]:
    from pathlib import Path

    doc = read_yaml(Path(path))
    d = dict(doc.get("defaults") or {})
    datasets: dict[str, dict[str, Any]] = {}
    for x in doc.get("datasets") or []:
        spec = {**{k: v for k, v in d.items() if k in ("priority", "sla", "max_attempts", "backoff_min",
                                                        "backoff_max_h", "lookback_days")}, **x}
        datasets[x["id"]] = spec
    return {"version": doc.get("version"), "defaults": d, "sources": dict(doc.get("sources") or {}),
            "datasets": datasets, "campaigns": {c["id"]: c for c in doc.get("campaigns") or []},
            "known_gaps": list(doc.get("known_gaps") or [])}


@lru_cache(maxsize=4)
def _cached(path: str, mtime: float) -> dict[str, Any]:
    return _load(path)


def load_coverage(settings: Settings | None = None) -> dict[str, Any]:
    p = (settings or get_settings()).contracts_dir / "coverage.yaml"
    try:
        mt = p.stat().st_mtime
    except OSError:
        return {"version": None, "defaults": {}, "sources": {}, "datasets": {}, "campaigns": {}, "known_gaps": []}
    return _cached(str(p), mt)


def validate_coverage(reg: dict[str, Any], job_ids: set[str] | None = None) -> list[str]:
    errs: list[str] = []
    for did, d in reg["datasets"].items():
        if d.get("check") not in CHECKS:
            errs.append(f"{did}: unknown check {d.get('check')!r}")
        fill = d.get("fill")
        if fill:
            if fill.get("kind") not in FILL_KINDS:
                errs.append(f"{did}: unknown fill kind {fill.get('kind')!r}")
            if fill.get("kind") == "campaign" and fill.get("campaign") not in reg["campaigns"]:
                errs.append(f"{did}: unknown campaign {fill.get('campaign')!r}")
            for j in fill.get("jobs") or []:
                if job_ids is not None and j.get("job") not in job_ids:
                    errs.append(f"{did}: unknown fill job {j.get('job')!r}")
        for k in ("live_job",):
            if job_ids is not None and d.get(k) and d[k] not in job_ids:
                errs.append(f"{did}: unknown {k} {d[k]!r}")
        src = (d.get("cost") or {}).get("source")
        if src and src not in reg["sources"]:
            errs.append(f"{did}: unknown cost source {src!r}")
    for cid, c in reg["campaigns"].items():
        if c.get("kind") not in CAMPAIGN_KINDS:
            errs.append(f"campaign {cid}: unknown kind {c.get('kind')!r}")
        if c.get("kind") == "job" and job_ids is not None and c.get("job") not in job_ids:
            errs.append(f"campaign {cid}: unknown job {c.get('job')!r}")
        if c.get("source") and c["source"] not in reg["sources"]:
            errs.append(f"campaign {cid}: unknown source {c['source']!r}")
    seen = set()
    for g in reg["known_gaps"]:
        if g.get("dataset") not in reg["datasets"]:
            errs.append(f"known gap {g.get('id')}: unknown dataset {g.get('dataset')!r}")
        if g.get("gap_class") not in GAP_CLASSES:
            errs.append(f"known gap {g.get('id')}: unknown class {g.get('gap_class')!r}")
        if g.get("id") in seen:
            errs.append(f"known gap {g.get('id')}: duplicate id")
        seen.add(g.get("id"))
    return errs


def effective_sla(spec: dict[str, Any], profile_sla_days: float | None) -> float:
    """SLA in sessions/days: the dataset's daily-profile SLA, widened to the profile's refresh period (a weekly
    profile tolerates ~a week of lag before a dataset counts as stale). Days -> sessions at 5/7 for session
    calendars."""
    base = float(spec.get("sla", 1))
    if not profile_sla_days:
        return base
    cal = spec.get("calendar")
    prof = float(profile_sla_days)
    if cal not in (None, "daily", "cftc", "13f", "finra", "macro_calendar"):
        prof = round(prof * 5 / 7)
    return max(base, prof)
