"""One freshness verdict per dataset for Data Health and the ops alerts (alert hygiene 2026-10-30).

Before: the ops alerts judged staleness with the gap detector's own SLA rule (coverage_status.json: lag > SLA),
while the startup sync judged the same datasets with calendars and an attempt ledger. On 2026-10-30 they
disagreed: health said "Macro ingestion, FINRA short interest stale beyond SLA", the sync said macro.ingest `fresh`
(33 sources, every group pulled within 20 h; 4 sources simply have no newer observations at their publisher) and
short_interest `not_yet_published` (FINRA has not published the next settlement; attempt 3, retry with back-off).

Now there is one rule, here, and both Data Health and the ops alerts use it:

* the verdict is the sync's per-dataset reason code (data/state/ops/sync_status.json: fresh, due,
  not_yet_published, source_unavailable, no_source, needs_key, failed) whenever the sync scanned within
  SYNC_AUTHORITATIVE_H; otherwise (sync disabled / never ran / app closed for long) the gap detector's verdict,
  coded `stale` when it says the dataset is beyond its SLA (`failed` when its check errored);
* the gap detector's measurement (data as-of, lag and the profile-adjusted SLA, in the dataset's unit: sessions or
  days) is the clock for `due` and `source_unavailable`. It is a measurement only - its own fresh / stale opinion
  never raises an alert while the sync has a verdict;
* alert when:  failed  |  due with lag > SLA  |  source_unavailable with lag > SLA  |  stale (fallback verdict);
  never for fresh, not_yet_published, needs_key, no_source - those show their reason (detail, next expected date,
  retry time, missing key names) instead.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

#: a sync status older than this is not a verdict any more (the sync runs every 30 min while the app is open)
SYNC_AUTHORITATIVE_H = 6.0
ALERT_ALWAYS = frozenset({"failed", "stale"})
ALERT_PAST_SLA = frozenset({"due", "source_unavailable"})
QUIET = frozenset({"fresh", "not_yet_published", "needs_key", "no_source"})


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def sync_scanned_at(sync: dict[str, Any] | None) -> datetime | None:
    """When the sync status' dataset reasons were computed (running: last heartbeat; idle: the end of the run)."""
    if not sync or not sync.get("datasets"):
        return None
    if sync.get("state") in ("running", "scanning", "publishing"):
        return _ts(sync.get("heartbeat_at")) or _ts(sync.get("started_at"))
    return _ts(sync.get("finished_at")) or _ts(sync.get("heartbeat_at")) or _ts(sync.get("started_at"))


def sync_authoritative(sync: dict[str, Any] | None, now: datetime | None = None) -> bool:
    t = sync_scanned_at(sync)
    now = now or datetime.now(UTC)
    return bool(t and now - t <= timedelta(hours=SYNC_AUTHORITATIVE_H))


def past_sla(det: dict[str, Any]) -> bool | None:
    lag, sla = det.get("lag"), det.get("sla")
    if lag is None or sla is None:
        return None
    try:
        return float(lag) > float(sla)
    except (TypeError, ValueError):
        return None


def verdict(det: dict[str, Any] | None, syn: dict[str, Any] | None, use_sync: bool) -> dict[str, Any]:
    """{reason, source, alerting, status, detail fields} for one dataset (see module doc)."""
    det = det or {}
    dstat = det.get("status") or "unknown"
    if use_sync and syn and syn.get("reason"):
        reason, source = str(syn["reason"]), "sync"
    elif dstat in ("stale", "missing"):
        reason, source = "stale", "detector"
    elif dstat == "error":
        reason, source = "failed", "detector"
    elif dstat in ("fresh", "partial"):
        reason, source = "fresh", "detector"
    else:
        reason, source = None, None
    over = past_sla(det)
    if reason in ALERT_ALWAYS:
        alerting = True
    elif reason in ALERT_PAST_SLA:
        alerting = bool(over)
    else:
        alerting = False
    if alerting:
        status = "missing" if dstat == "missing" else "stale"
    elif reason in ("fresh", "not_yet_published"):
        status = "partial" if dstat == "partial" else "fresh"
    elif reason in ("due", "source_unavailable", "needs_key", "no_source"):
        status = dstat if dstat in ("fresh", "partial") else "partial"
    elif dstat == "error":
        status = "error"
    else:
        status = dstat if dstat in ("fresh", "partial", "unknown") else "unknown"
    s = syn or {}
    return {"reason": reason, "reason_source": source, "alerting": alerting, "status": status,
            "detector_status": dstat, "past_sla": over,
            "reason_detail": s.get("detail") if source == "sync" else det.get("detail"),
            "next_expected": s.get("next_expected") if source == "sync" else None,
            "retry_at": s.get("retry_at") if source == "sync" else None,
            "attempts": s.get("attempts") if source == "sync" else None,
            "keys": list(s.get("keys") or []) if source == "sync" else []}


def dataset_views(cov: dict[str, Any] | None, sync: dict[str, Any] | None, now: datetime | None = None,
                  labels: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """dataset id -> detector fields (label, data_as_of, expected_as_of, lag, sla, detail) + verdict()."""
    now = now or datetime.now(UTC)
    det_all = (cov or {}).get("datasets") or {}
    use_sync = sync_authoritative(sync, now)
    syn_all = {d.get("dataset"): d for d in ((sync or {}).get("datasets") or []) if d.get("dataset")}
    out: dict[str, dict[str, Any]] = {}
    for did in list(det_all) + [k for k in syn_all if k not in det_all and not str(k).startswith("job:")]:
        det = det_all.get(did) or {}
        syn = syn_all.get(did)
        v = verdict(det, syn, use_sync)
        label = det.get("label") or (labels or {}).get(did) or (syn or {}).get("label") or did
        out[did] = {"dataset": did, "label": label, "data_as_of": det.get("data_as_of") or (syn or {}).get("data_as_of"),
                    "expected_as_of": det.get("expected_as_of") or (syn or {}).get("expected_as_of"),
                    "lag": det.get("lag"), "sla": det.get("sla"), "detail": det.get("detail"), **v}
    return out


def describe(v: dict[str, Any]) -> str:
    """One line for an alert: label, reason, as-of, lag / SLA (and the retry time for source_unavailable)."""
    bits = [f"{v.get('label')}: {(v.get('reason') or '?').replace('_', ' ')}"]
    if v.get("data_as_of"):
        bits.append(f"as of {str(v['data_as_of'])[:10]}")
    if v.get("lag") is not None and v.get("sla") is not None:
        bits.append(f"lag {v['lag']:g} / SLA {v['sla']:g}" if isinstance(v["lag"], (int, float)) and
                    isinstance(v["sla"], (int, float)) else f"lag {v['lag']} / SLA {v['sla']}")
    if v.get("reason") == "source_unavailable" and v.get("retry_at"):
        bits.append(f"retry {str(v['retry_at'])[:16]}")
    if v.get("reason") == "failed" and v.get("attempts"):
        bits.append(f"{v['attempts']} attempt(s)")
    return " · ".join(bits)
