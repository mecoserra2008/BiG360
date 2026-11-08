"""News alert rules (roadmap v3 N4.3), evaluated live by app.realtime.alerts_live.AlertConsumer on news.item /
news.story (pure functions; the nightly alerts_eval treats them as no-ops).

watchlist_news {tiers?: [1, 2, 3] (source tiers, default 1-3: official, major press, publishers; add 4 for GDELT web
    articles), min_abs_sentiment?: 0.0, languages?: [..], sources?: [feed / source ids], max_per_day?: 10}
    a news item linked to a target security (alert security_id, or every member of its watchlist). One event per
    (alert, security, story): later articles of the same story do not fire again; at most max_per_day per security.
story_velocity {min_articles?: 5 (articles in the last 60 min), min_ratio?: 3.0 (vs the story's own baseline rate,
    floored at 0.5 / h, so a brand-new story needs >= 5 articles within the hour), min_sources?: 2, themes?: [..],
    countries?: [..]}
    a burst of articles on one story. With a security / watchlist target only stories linked to the targets fire;
    without a target (allowed for this rule) any story matching the filters fires as security_id 'news:<story_id>'.
    One event per (alert, target, story).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

NEWS_RULE_TYPES = ("watchlist_news", "story_velocity")
#: rule types that may be created without a security / watchlist target
UNTARGETED = ("story_velocity",)
ET = ZoneInfo("America/New_York")


def _date(ts: Any) -> date | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _in(v: Any, allowed: Any) -> bool:
    return not allowed or v in set(allowed)


def _hits(item_secs: list[str], item_issuers: list[str], targets: list[str], sec_issuer: dict[str, str]) -> list[str]:
    out = []
    iss = set(item_issuers or [])
    for sid in targets:
        if sid in set(item_secs or []) or (sec_issuer.get(sid) and sec_issuer[sid] in iss):
            out.append(sid)
    return out


def news_triggers(rule: dict[str, Any], params: dict[str, Any], targets: list[str], topic: str, mtype: str,
                  p: dict[str, Any], sec_issuer: dict[str, str], ingest: datetime) -> list[dict[str, Any]]:
    """One rule x one news.item / news.story payload -> trigger dicts {security_id, as_of_date, message, payload,
    dedupe} (dedupe = story id: the per-story idempotency key)."""
    rt = rule["rule_type"]
    out: list[dict[str, Any]] = []
    if rt == "watchlist_news" and topic == "news.item":
        tiers = params.get("tiers") or [1, 2, 3]
        if int(p.get("tier") or 4) not in {int(x) for x in tiers}:
            return []
        if not (_in(p.get("language"), params.get("languages")) and _in(p.get("source_id"), params.get("sources"))):
            return []
        s = p.get("sentiment_score")
        mins = float(params.get("min_abs_sentiment") or 0.0)
        if mins > 0 and (s is None or abs(float(s)) < mins):
            return []
        for sid in _hits(p.get("security_ids") or [], p.get("issuer_ids") or [], targets, sec_issuer):
            d = _date(p.get("published_at")) or ingest.astimezone(ET).date()
            tone = "" if s is None else (" (positive)" if s > 0.2 else " (negative)" if s < -0.2 else "")
            out.append({"security_id": sid, "as_of_date": d, "dedupe": str(p.get("story_id") or p.get("item_key")),
                        "message": f"news{tone}: {str(p.get('title') or '')[:180]} - {p.get('publisher') or ''}".strip(),
                        "payload": {"story_id": p.get("story_id"), "item_key": p.get("item_key"), "url": p.get("url"),
                                    "publisher": p.get("publisher"), "tier": p.get("tier"),
                                    "sentiment_score": s, "published_at": p.get("published_at")}})
    elif rt == "story_velocity" and topic == "news.story":
        v = int(p.get("velocity_1h") or 0)
        ratio = float(p.get("velocity_ratio") or 0.0)
        if v < int(params.get("min_articles") or 5) or ratio < float(params.get("min_ratio") or 3.0):
            return []
        if int(p.get("n_sources") or 0) < int(params.get("min_sources") or 2):
            return []
        if params.get("themes") and not set(params["themes"]) & set(p.get("themes") or []):
            return []
        if params.get("countries") and not set(params["countries"]) & set(p.get("countries") or []):
            return []
        d = _date(p.get("last_seen_at")) or ingest.astimezone(ET).date()
        base = {"story_id": p.get("story_id"), "velocity_1h": v, "baseline_per_h": p.get("baseline_per_h"),
                "velocity_ratio": ratio, "n_articles": p.get("n_articles"), "n_sources": p.get("n_sources")}
        msg = (f"story burst: {v} articles in 60 min ({ratio:.1f}x baseline, {p.get('n_sources')} sources): "
               f"{str(p.get('title') or '')[:160]}")
        if targets:
            secs: list[str] = []
            if sec_issuer:
                iss = set(p.get("issuer_ids") or [])
                secs = [s for s in targets if sec_issuer.get(s) in iss]
            for sid in secs:
                out.append({"security_id": sid, "as_of_date": d, "dedupe": str(p.get("story_id")), "message": msg,
                            "payload": base})
        else:
            out.append({"security_id": f"news:{p.get('story_id')}", "as_of_date": d, "dedupe": str(p.get("story_id")),
                        "message": msg, "payload": base})
    return out


def validate(rule_type: str, p: dict[str, Any]) -> dict[str, Any]:
    """Defaults + light validation (used by the alerts API). Raises ValueError."""
    p = dict(p or {})
    if rule_type == "watchlist_news":
        tiers = p.get("tiers") or [1, 2, 3]
        if not isinstance(tiers, list) or not all(int(x) in (1, 2, 3, 4) for x in tiers):
            raise ValueError("watchlist_news: tiers must be a list of 1..4")
        p["tiers"] = sorted({int(x) for x in tiers})
        p["min_abs_sentiment"] = float(p.get("min_abs_sentiment") or 0.0)
        if not 0 <= p["min_abs_sentiment"] <= 1:
            raise ValueError("watchlist_news: min_abs_sentiment must be in [0, 1]")
        p["max_per_day"] = int(p.get("max_per_day") or 10)
        for k in ("languages", "sources"):
            if k in p and (not isinstance(p[k], list) or not all(isinstance(x, str) for x in p[k])):
                raise ValueError(f"watchlist_news: {k} must be a list of strings")
    elif rule_type == "story_velocity":
        p["min_articles"] = int(p.get("min_articles") or 5)
        p["min_ratio"] = float(p.get("min_ratio") or 3.0)
        p["min_sources"] = int(p.get("min_sources") or 2)
        if p["min_articles"] < 2 or p["min_ratio"] < 1 or p["min_sources"] < 1:
            raise ValueError("story_velocity: min_articles >= 2, min_ratio >= 1, min_sources >= 1")
        for k in ("themes", "countries"):
            if k in p and (not isinstance(p[k], list) or not all(isinstance(x, str) for x in p[k])):
                raise ValueError(f"story_velocity: {k} must be a list of strings")
    return p
