"""Alerts (tag: alerts): rule CRUD (app_state.duckdb) and fired events (written by the worker's alerts_eval job)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api import app_state
from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import FACTORS, fnum, invalid, iso, not_found
from app.api.serving import profiles

router = APIRouter(tags=["alerts"], route_class=FastRoute)

RuleType = Literal["price_cross", "zone_touch", "score_change", "insider_buy", "sentiment_shift", "new_filing",
                   "model_decile", "price_change_pct_1d", "macro_release", "macro_surprise", "regime_change",
                   "earnings_upcoming", "earnings_surprise",
                   "activist_13d", "short_interest_spike", "eu_short_position",
                   "price_outside_calibrated_range", "conformal_peer_anomaly", "watchlist_news", "story_velocity"]
#: macro-w2 rule types: country-level, no security / watchlist target (fired as security_id "macro:<CC>")
MACRO_RULES = {"macro_release", "macro_surprise", "regime_change"}
MACRO_THEMES = {"growth", "inflation", "labor"}
EARNINGS_DATE_SOURCES = {"company_announced", "provider_estimate", "model_projection"}
EARNINGS_ITEMS = {"eps_diluted", "revenue"}
FORM_CLASSES = {"annual", "semiannual", "quarterly", "current_report", "insider", "holdings", "proxy", "prospectus", "other"}
#: direction spellings sent by older clients (the watchlist editor used above / below for every rule type)
_DIRECTION_ALIAS = {"above": "up", "below": "down", "crosses": "any", "either": "any"}


def validate_params(rule_type: str, p: dict[str, Any]) -> dict[str, Any]:
    """Light validation per rule (contracts/schemas/alerts.yaml); returns params with defaults filled in."""
    p = dict(p or {})

    def need_num(k: str) -> float:
        v = fnum(p.get(k))
        if v is None:
            raise invalid(f"{rule_type}: param {k!r} (number) is required")
        return v

    def enum(k: str, allowed: set[str], default: str) -> None:
        v = p.get(k, default)
        if v not in allowed:
            raise invalid(f"{rule_type}: param {k!r} must be one of {sorted(allowed)}")
        p[k] = v

    if rule_type == "price_cross":
        need_num("level")
        enum("direction", {"above", "below"}, "above")
    elif rule_type == "zone_touch":
        p["zone_rank_max"] = int(fnum(p.get("zone_rank_max")) or 3)
        enum("side", {"support", "resistance", "any"}, "any")
    elif rule_type == "score_change":
        if p.get("direction") in _DIRECTION_ALIAS:
            p["direction"] = _DIRECTION_ALIAS[p["direction"]]
        if "mode" not in p and p.get("level") is not None and p.get("delta") is None:
            p["mode"] = "level"                       # {level, direction} without a mode = a level crossing
        if not p.get("factor_id") and not p.get("profile_id"):
            p["profile_id"] = "balanced"
        if p.get("factor_id") and p["factor_id"] not in FACTORS:
            raise invalid(f"score_change: unknown factor_id {p['factor_id']!r}")
        if p.get("profile_id") and p["profile_id"] not in profiles():
            raise invalid(f"score_change: unknown profile_id {p['profile_id']!r}")
        enum("mode", {"delta", "level"}, "delta")
        need_num("level" if p["mode"] == "level" else "delta")
        enum("direction", {"up", "down", "any"}, "any")
    elif rule_type == "insider_buy":
        p["min_value_usd"] = fnum(p.get("min_value_usd")) or 0.0
        p["cluster_only"] = bool(p.get("cluster_only", False))
    elif rule_type == "sentiment_shift":
        need_num("threshold_z")
    elif rule_type == "new_filing":
        fc = p.get("form_classes") or ["annual", "quarterly", "current_report"]
        if not isinstance(fc, list) or not set(fc) <= FORM_CLASSES:
            raise invalid(f"new_filing: form_classes must be a subset of {sorted(FORM_CLASSES)}")
        p["form_classes"] = fc
    elif rule_type == "model_decile":
        enum("horizon", {"1M", "3M", "6M"}, "3M")
        d = int(fnum(p.get("min_decile")) or 9)
        if not 1 <= d <= 10:
            raise invalid("model_decile: min_decile must be in 1..10")
        p["min_decile"] = d
    elif rule_type == "price_change_pct_1d":
        if p.get("pct") is None and p.get("pct_change_1d") is not None:
            p["pct"] = p.pop("pct_change_1d")           # older watchlist editor payload
        if p.get("direction") in _DIRECTION_ALIAS:
            p["direction"] = _DIRECTION_ALIAS[p["direction"]]
        if fnum(p.get("pct")) is not None:
            p["pct"] = abs(float(fnum(p.get("pct"))))   # magnitude; the sign is carried by direction
        v = need_num("pct")
        if v <= 0:
            raise invalid("price_change_pct_1d: pct must be > 0 (fraction, e.g. 0.05 = 5%)")
        enum("direction", {"up", "down", "any"}, "any")
    elif rule_type == "earnings_upcoming":
        d = int(fnum(p.get("days_ahead")) or 3)
        if not 0 <= d <= 60:
            raise invalid("earnings_upcoming: days_ahead must be in 0..60")
        p["days_ahead"] = d
        ds = p.get("date_sources")
        if ds is not None and (not isinstance(ds, list) or not set(ds) <= EARNINGS_DATE_SOURCES):
            raise invalid(f"earnings_upcoming: date_sources must be a subset of {sorted(EARNINGS_DATE_SOURCES)}")
    elif rule_type == "earnings_surprise":
        p["threshold_z"] = abs(fnum(p.get("threshold_z")) or 2.0)
        it = p.get("items") or ["eps_diluted", "revenue"]
        if not isinstance(it, list) or not set(it) <= EARNINGS_ITEMS:
            raise invalid(f"earnings_surprise: items must be a subset of {sorted(EARNINGS_ITEMS)}")
        p["items"] = it
    elif rule_type == "activist_13d":
        p["include_amendments"] = bool(p.get("include_amendments", False))
        p["include_switch"] = bool(p.get("include_switch", True))
    elif rule_type == "short_interest_spike":
        p["min_chg_pct"] = fnum(p.get("min_chg_pct")) if p.get("min_chg_pct") is not None else 0.25
        p["min_si_pct"] = fnum(p.get("min_si_pct")) or 0.0
        if p["min_chg_pct"] is None or not 0 < p["min_chg_pct"] <= 100:
            raise invalid("short_interest_spike: min_chg_pct must be > 0 (fraction, 0.25 = +25%)")
        if not 0 <= p["min_si_pct"] < 1.5:
            raise invalid("short_interest_spike: min_si_pct must be a fraction of shares outstanding in [0, 1.5)")
    elif rule_type == "eu_short_position":
        p["min_pct"] = fnum(p.get("min_pct")) or 0.5
        p["include_increases"] = bool(p.get("include_increases", True))
        p["min_increase_pp"] = fnum(p.get("min_increase_pp")) or 0.1
        if not 0 < p["min_pct"] <= 100 or p["min_increase_pp"] <= 0:
            raise invalid("eu_short_position: min_pct must be in (0, 100] percent and min_increase_pp > 0")
    elif rule_type == "price_outside_calibrated_range":
        lvl = int(fnum(p.get("level")) or 95)
        h = int(fnum(p.get("horizon")) or 1)
        if lvl not in (80, 90, 95):
            raise invalid("price_outside_calibrated_range: level must be 80, 90 or 95")
        if h not in (1, 5, 10, 20):
            raise invalid("price_outside_calibrated_range: horizon must be 1, 5, 10 or 20 sessions")
        p["level"], p["horizon"] = lvl, h
        enum("side", {"up", "down", "any"}, "any")
    elif rule_type == "conformal_peer_anomaly":
        v = fnum(p.get("percentile"))
        v = 0.95 if v is None else v
        if v > 1:  # accept 95 as 95%
            v = v / 100.0
        if not 0.5 <= v < 1:
            raise invalid("conformal_peer_anomaly: percentile must be a fraction in [0.5, 1) (e.g. 0.95)")
        p["percentile"] = v
        enum("scope", {"group", "member", "any"}, "any")
    elif rule_type in ("watchlist_news", "story_velocity"):
        from app.news_live.alerts import validate  # noqa: PLC0415

        try:
            p = validate(rule_type, p)
        except ValueError as e:
            raise invalid(str(e)) from e
    elif rule_type in MACRO_RULES:
        for k in ("countries", "series_ids", "themes", "methods"):
            if k in p and (not isinstance(p[k], list) or not all(isinstance(x, str) for x in p[k])):
                raise invalid(f"{rule_type}: param {k!r} must be a list of strings")
        if rule_type in ("macro_release", "macro_surprise"):
            imp = int(fnum(p.get("max_importance")) or (1 if rule_type == "macro_release" else 3))
            if not 1 <= imp <= 3:
                raise invalid(f"{rule_type}: max_importance must be in 1..3")
            p["max_importance"] = imp
        if rule_type == "macro_surprise":
            p["threshold_z"] = abs(fnum(p.get("threshold_z")) or 2.0)
            if p.get("themes") and not set(p["themes"]) <= MACRO_THEMES:
                raise invalid(f"macro_surprise: themes must be a subset of {sorted(MACRO_THEMES)}")
        if rule_type == "regime_change":
            enum("kind", {"cycle_quadrant", "recession_probability", "any"}, "any")
            lv = fnum(p.get("level"))
            p["level"] = 0.5 if lv is None else lv
            if not 0 < p["level"] < 1:
                raise invalid("regime_change: level must be a probability in (0, 1)")
    return p


class AlertCreate(BaseModel):
    security_id: str | None = None
    watchlist_id: str | None = None
    rule_type: RuleType
    params: dict[str, Any]
    cooldown_days: int = Field(5, ge=0, le=365)


class AlertUpdate(BaseModel):
    enabled: bool | None = None
    params: dict[str, Any] | None = None
    cooldown_days: int | None = Field(None, ge=0, le=365)


def _names(db: Db | None, alerts: list[dict[str, Any]]) -> dict[str, Any]:
    """Display names for the targets (v1.39.0): security ticker / name from the snapshot, watchlist names."""
    ids = sorted({a["security_id"] for a in alerts if a.get("security_id")})
    secs: dict[str, tuple[Any, Any]] = {}
    if ids and db is not None and not db.snap.is_empty:
        try:
            secs = {r["security_id"]: (r["ticker"], r["name"]) for r in db.all(
                f"SELECT security_id, ticker, name FROM sec WHERE security_id IN ({', '.join('?' for _ in ids)})", ids)}
        except Exception:  # noqa: BLE001 - names are cosmetic; never fail the rule list
            secs = {}
    wls: dict[str, Any] = {}
    for wid in {a["watchlist_id"] for a in alerts if a.get("watchlist_id")}:
        w = app_state.get_watchlist(wid)
        if w is not None:
            wls[wid] = w.get("name")
    return {"sec": secs, "wl": wls}


def _alert_out(a: dict[str, Any], names: dict[str, Any] | None = None) -> dict[str, Any]:
    sec = (names or {}).get("sec", {}).get(a.get("security_id") or "", (None, None))
    return {"alert_id": a["alert_id"], "security_id": a.get("security_id"), "watchlist_id": a.get("watchlist_id"),
            "rule_type": a["rule_type"], "params": a.get("params") or {}, "enabled": bool(a["enabled"]),
            "cooldown_days": int(a["cooldown_days"]), "last_fired_at": iso(a.get("last_fired_at")),
            "created_at": iso(a["created_at"]), "thesis_id": a.get("thesis_id"),
            "ticker": sec[0], "security_name": sec[1],
            "watchlist_name": (names or {}).get("wl", {}).get(a.get("watchlist_id") or "")}


@router.get("/alerts", operation_id="listAlerts")
def list_alerts(db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    rows = app_state.list_alerts()
    names = _names(db, rows)
    return [_alert_out(a, names) for a in rows]


@router.post("/alerts", operation_id="createAlert", status_code=201)
def create_alert(body: AlertCreate, db: Db = Depends(get_db)) -> JSONResponse:
    if not body.security_id and not body.watchlist_id and body.rule_type not in MACRO_RULES             and body.rule_type != "story_velocity":
        raise invalid("an alert needs a security_id or a watchlist_id")
    if body.watchlist_id and app_state.get_watchlist(body.watchlist_id) is None:
        raise invalid(f"unknown watchlist {body.watchlist_id!r}")
    params = validate_params(body.rule_type, body.params)
    a = app_state.create_alert(body.security_id, body.watchlist_id, body.rule_type, params, body.cooldown_days)
    return JSONResponse(_alert_out(a, _names(db, [a])), status_code=201)


@router.get("/alerts/events", operation_id="listAlertEvents")
def list_alert_events(unread_only: bool = False, limit: int = Query(50, ge=1, le=500),
                      db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    evs = app_state.list_alert_events(unread_only, limit)
    ids = sorted({e["security_id"] for e in evs})
    tick = {}
    if ids:
        tick = {r["security_id"]: r["ticker"] for r in db.all(
            f"SELECT security_id, ticker FROM sec WHERE security_id IN ({', '.join('?' for _ in ids)})", ids)}
    return [{"event_id": e["event_id"], "alert_id": e["alert_id"], "security_id": e["security_id"],
             "ticker": tick.get(e["security_id"]),
             "target": e["security_id"] if str(e["security_id"]).startswith(("macro:", "thesis:")) else "security", "fired_at": iso(e["fired_at"]), "as_of_date": iso(e["as_of_date"]),
             "message": e["message"], "payload": e.get("payload"), "read": bool(e["read"])} for e in evs]


class MarkRead(BaseModel):
    event_ids: list[str]


@router.post("/alerts/events/mark-read", operation_id="markAlertEventsRead", status_code=204)
def mark_alert_events_read(body: MarkRead) -> Response:
    app_state.mark_events_read(body.event_ids)
    return Response(status_code=204)


@router.patch("/alerts/{alert_id}", operation_id="updateAlert")
def update_alert(alert_id: str, body: AlertUpdate, db: Db = Depends(get_db)) -> dict[str, Any]:
    cur = app_state.get_alert(alert_id)
    if cur is None:
        raise not_found(f"unknown alert {alert_id!r}")
    fields = body.model_dump(exclude_unset=True, exclude_none=True)
    if "params" in fields:
        fields["params"] = validate_params(cur["rule_type"], fields["params"])
    a = app_state.update_alert(alert_id, fields)
    if a is None:
        raise not_found(f"unknown alert {alert_id!r}")
    return _alert_out(a, _names(db, [a]))


@router.delete("/alerts/{alert_id}", operation_id="deleteAlert", status_code=204)
def delete_alert(alert_id: str) -> Response:
    if not app_state.delete_alert(alert_id):
        raise not_found(f"unknown alert {alert_id!r}")
    return Response(status_code=204)
