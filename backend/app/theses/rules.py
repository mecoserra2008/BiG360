"""Linked alert rules generated from a thesis (roadmap #8). Pure: thesis dict + subject context -> rule specs
[{rule_type, security_id, params, cooldown_days}] that app.theses.store.sync_alerts writes with alerts.thesis_id.

Existing alerts_eval evaluators cover price bands (price_cross on price_id), score / grade (score_change on the balanced
composite), earnings (earnings_upcoming / earnings_surprise), security event shocks (sentiment_shift,
price_change_pct_1d), smart money (activist_13d, short_interest_spike for US, eu_short_position for EU / UK, insider
cluster buys), macro (macro_surprise / regime_change for country and macro-theme subjects). The thesis_* rules are
evaluated by the thesis_eval job (app.theses.evaluate): KPI breaches, review reminders, status changes, dated
catalysts, CFTC positioning extremes (cross-asset subjects) and events-v2 shocks (issuer / country / sector).
"""

from __future__ import annotations

from typing import Any

MACRO_THEMES = {"growth", "inflation", "labor"}
EU_REGIONS = {"EU", "UK"}


def parse_macro_subject(subject_id: str) -> tuple[str | None, str]:
    """'US:inflation' -> ('US', 'inflation'); 'inflation' -> (None, 'inflation')."""
    if ":" in subject_id:
        cc, th = subject_id.split(":", 1)
        return (cc.upper() or None), th.lower()
    return None, subject_id.lower()


def _band_rules(t: dict[str, Any]) -> list[dict[str, Any]]:
    pid = t.get("price_id")
    if not pid:
        return []
    d = t.get("direction") or "long"
    out: list[tuple[str, float | None, str]] = []
    if d == "short":
        out += [("target", t.get("target"), "below"), ("stop", t.get("stop"), "above")]
    else:  # long / neutral: target above, stop below
        out += [("target", t.get("target"), "above"), ("stop", t.get("stop"), "below")]
    out += [("entry_hi", t.get("entry_hi"), "below"), ("entry_lo", t.get("entry_lo"), "above")]
    rules = []
    for band, lvl, direction in out:
        if lvl is None:
            continue
        rules.append({"rule_type": "price_cross", "security_id": pid, "cooldown_days": 1,
                      "params": {"level": float(lvl), "direction": direction, "metric": "close",
                                 "thesis_id": t["thesis_id"], "band": band}})
    return rules


def build_rules(t: dict[str, Any], ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """ctx: {region, country, futures_root} of the subject (from app.theses.evaluate.subject_info)."""
    ctx = ctx or {}
    tid = t["thesis_id"]
    st, sid = t["subject_type"], t["subject_id"]
    sec = t.get("security_id") if st == "security" else None
    tag = {"thesis_id": tid}
    rules = _band_rules(t)

    def add(rule_type: str, params: dict[str, Any], security_id: str | None = None, cooldown: int = 5) -> None:
        rules.append({"rule_type": rule_type, "security_id": security_id, "params": {**params, **tag},
                      "cooldown_days": cooldown})

    if st == "security" and sec:
        add("score_change", {"profile_id": "balanced", "mode": "delta", "delta": 10.0, "direction": "any",
                             "lookback_days": 7}, sec)
        add("earnings_upcoming", {"days_ahead": 7}, sec, 1)
        add("earnings_surprise", {"threshold_z": 2.0, "items": ["eps_diluted", "revenue"]}, sec, 1)
        add("sentiment_shift", {"threshold_z": 2.5}, sec)
        add("price_change_pct_1d", {"pct": 0.07, "direction": "any"}, sec, 1)
        add("activist_13d", {"include_amendments": False, "include_switch": True}, sec, 1)
        add("insider_buy", {"min_value_usd": 250000.0, "cluster_only": True}, sec)
        region = ctx.get("region")
        if region == "US":
            add("short_interest_spike", {"min_chg_pct": 0.25, "min_si_pct": 0.0}, sec)
        if region in EU_REGIONS:
            add("eu_short_position", {"min_pct": 0.5, "include_increases": True, "min_increase_pp": 0.1}, sec)
        add("thesis_event_shock", {"label_type": "issuer", "label_id": sec, "threshold_z": 3.0}, sec, 3)
    elif st == "country":
        cc = sid.upper()
        add("macro_surprise", {"countries": [cc], "threshold_z": 2.0, "max_importance": 2})
        add("regime_change", {"countries": [cc], "kind": "any", "level": 0.5})
        add("thesis_event_shock", {"label_type": "country", "label_id": cc, "threshold_z": 3.0}, None, 3)
    elif st == "macro_theme":
        cc, theme = parse_macro_subject(sid)
        p: dict[str, Any] = {"threshold_z": 2.0, "max_importance": 2}
        if theme in MACRO_THEMES:
            p["themes"] = [theme]
        if cc:
            p["countries"] = [cc]
        add("macro_surprise", p)
        add("regime_change", {"kind": "any", "level": 0.5, **({"countries": [cc]} if cc else {})})
        if cc:
            add("thesis_event_shock", {"label_type": "country", "label_id": cc, "threshold_z": 3.0}, None, 3)
    elif st == "sector":
        add("thesis_event_shock", {"label_type": "sector", "label_id": sid.upper(), "threshold_z": 3.0}, None, 3)
    elif st == "cross_asset" and ctx.get("futures_root"):
        add("thesis_positioning", {"asset_id": sid, "lo": 0.1, "hi": 0.9}, None, 5)

    for k in t.get("kpis") or []:
        add("thesis_kpi", {"kpi_id": k["kpi_id"], "label": k.get("label") or k["kpi_id"]}, sec, 5)
    if any(c.get("date") and c.get("kind") != "earnings" and not c.get("done") for c in t.get("catalysts") or []):
        add("thesis_catalyst", {"days_ahead": 3}, sec, 1)
    if t.get("review_every_days"):
        add("thesis_review", {"every_days": int(t["review_every_days"])}, sec, 1)
    add("thesis_status", {}, sec, 1)
    return rules
