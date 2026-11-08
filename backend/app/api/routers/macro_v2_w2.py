"""Macro v2 wave 2 endpoints (tag: macro-v2; owner macro-w2): official release calendar, release detail, surprise
indices, GDP nowcasts, recession panel. Kept in their own module (registered next to routers.macro_v2) so the two macro
agents never edit the same file.

Every expectation is OUR model forecast: fields carry model_based = true and the label
"Model-based expectation (not consensus)". Data comes from the serving snapshot tables built by
app.api.serving._Builder.macro_v2_w2 (macro_releases, macro_release_events, macro_expectations, macro_surprises,
macro_surprise_index, macro_nowcasts, macro_recession).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import invalid, iso, jload, not_found
from app.macro.calendar import MODEL_LABEL

router = APIRouter(tags=["macro-v2"], route_class=FastRoute)

#: structured importance tiers (api 1.10.0): tier -> indicator families
IMPORTANCE_TIERS = [
    {"tier": 1, "label": "high", "includes": ["gdp", "cpi", "core_cpi", "hicp", "payrolls_employment", "unemployment",
                                             "policy_rate_decision"]},
    {"tier": 2, "label": "medium", "includes": ["industrial_production", "retail_sales", "ppi", "pce_prices",
                                               "pmi_proxy_business_confidence", "jobless_claims", "jolts"]},
    {"tier": 3, "label": "low", "includes": ["other"]},
]
IMPORTANCE_RULE = ("1 = GDP, CPI/core CPI/HICP, payrolls/employment, unemployment, policy-rate decisions; "
                   "2 = industrial production, retail sales, PPI, PCE prices, PMI proxies/business confidence, jobless "
                   "claims, JOLTS; 3 = other")
SURPRISE_METHOD = {
    "expectation": MODEL_LABEL, "z": "(actual - model expectation) / std of the series' past forecast errors (last 36, "
    "min 6; else std of past release-to-release changes), clipped to +-5; signed by economic direction",
    "index": "100 x sum(w_i * signed_z_i) / max(sum(w_i), 1) over releases in the last 365 days",
    "halflife_days": 45, "window_days": 365, "tier_weights": {"1": 1.0, "2": 0.6, "3": 0.3},
}
NOWCAST_METHOD = ("Ours: statsmodels DynamicFactorMQ (1 factor, AR(2), AR(1) idiosyncratic, mixed monthly/quarterly) on "
                  "point-in-time official monthly indicators; 80% interval from the smoothed prediction variance; news = "
                  "contribution of each updated series to the change since the previous nowcast. Benchmarks: published "
                  "Atlanta Fed GDPNow and New York Fed Staff Nowcast (official, reference only).")
RECESSION_METHODS = [
    ("nyfed_yield_curve_official", True, "NY Fed probit of NBER recession 12 months ahead on the 10y-3m spread "
     "(published allmonth.xls), dated at the spread month"),
    ("yield_curve_probit", False, "Our re-estimate of the same 3m10y probit per country (NBER for US; 2+ negative "
     "official GDP quarters elsewhere), in-sample fit"),
    ("sahm_realtime", True, "Real-time Sahm rule (FRED SAHMREALTIME), signal >= 0.5"),
    ("hmm_regime", False, "Our Gaussian HMM regime model, filtered P(contraction)"),
    ("oecd_cli_turning_point", True, "OECD CLI phase; signal = slowdown or below 100 after a confirmed peak "
     "(turning-point rule ours)"),
]


def _csv(v: list[str] | None) -> list[str]:
    out: list[str] = []
    for x in v or []:
        out += [p.strip().upper() for p in x.split(",") if p.strip()]
    return out


def _has(db: Db, table: str) -> bool:
    return bool(db.scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]))


def _status(db: Db, *tables: str) -> str:
    have = [t for t in tables if _has(db, t) and db.scalar(f'SELECT count(*) > 0 FROM "{t}"')]
    return "ok" if len(have) == len(tables) else ("partial" if have else "pending")


def _in(col: str, vals: list[str]) -> tuple[str, list[Any]]:
    return (f" AND {col} IN ({', '.join('?' for _ in vals)})", list(vals)) if vals else ("", [])


def _expectation(r: dict[str, Any] | None) -> dict[str, Any] | None:
    if not r or r.get("point") is None:
        return None
    return {"point": r["point"], "lo80": r.get("lo80"), "hi80": r.get("hi80"), "lo95": r.get("lo95"), "hi95": r.get("hi95"),
            "model": r["model"], "model_spec": r.get("model_spec"), "is_primary": bool(r.get("is_primary")),
            "model_based": True, "label": MODEL_LABEL, "forecast_made_at": iso(r["forecast_made_at"]),
            "data_cutoff": iso(r.get("data_cutoff")), "backfill": bool(r.get("backfill")), "version": r.get("version") or ""}


def _item(r: dict[str, Any], series: list[dict[str, Any]], today: date) -> dict[str, Any]:
    from datetime import UTC, datetime  # noqa: PLC0415

    rd = r["release_date"]
    sched = r["scheduled_at"]
    future = sched > datetime.now(UTC) if isinstance(sched, datetime) and sched.tzinfo else rd > today
    if future:  # hard guard: an upcoming release never shows an outcome
        for s in series:
            if s["actual"] is not None:
                s.update(actual=None, prior_revised=None, prior_revision=None, first_seen_at=None, surprise_z=None,
                         signed_z=None, status="pending")
    st = "upcoming" if future else ("released" if any(s["status"] == "released" for s in series) else "past")
    return {"release_event_id": r["release_event_id"], "release_name": r["release_name"], "publisher": r["publisher"],
            "source_id": r["source_id"], "country": r["country"], "kind": r["kind"], "scheduled_at": iso(r["scheduled_at"]),
            "scheduled_local": r["scheduled_local"], "scheduled_tz": r["scheduled_tz"], "release_date": iso(rd),
            "time_known": bool(r["time_known"]), "schedule_source": r["schedule_source"], "importance": int(r["importance"]),
            "reference_period": r.get("reference_period"), "url": r.get("url"), "status": st, "series": series}


def _series_rows(db: Db, rel_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """release_event_id -> linked series (outcome + primary model-based expectation + surprise)."""
    if not rel_ids:
        return {}
    ph = ", ".join("?" for _ in rel_ids)
    have_ex, have_su = _has(db, "macro_expectations"), _has(db, "macro_surprises")
    ex_join = ("""LEFT JOIN (SELECT * FROM macro_expectations WHERE is_primary
                   QUALIFY row_number() OVER (PARTITION BY release_event_id, series_id ORDER BY version DESC,
                                                         forecast_made_at DESC) = 1) x
                 ON x.release_event_id = e.release_event_id AND x.series_id = e.series_id
                    AND x.target_period IS NOT DISTINCT FROM e.period""" if have_ex else "")
    su_join = ("LEFT JOIN macro_surprises s ON s.release_event_id = e.release_event_id AND s.series_id = e.series_id"
               if have_su else "")
    ex_cols = ("x.point, x.lo80, x.hi80, x.lo95, x.hi95, x.model, x.model_spec, x.is_primary, x.forecast_made_at, "
               "x.data_cutoff, x.backfill, x.version" if have_ex else "NULL AS point")
    su_cols = "s.surprise_z, s.signed_z" if have_su else "NULL AS surprise_z, NULL AS signed_z"
    rows = db.all(f"""
        SELECT e.release_event_id, e.series_id, coalesce(m.title, e.series_id) AS title, e.theme, e.measure, e.period,
               e.status, e.actual, e.prior, e.prior_revised, e.prior_revision, e.first_seen_at,
               e.knowledge_date_estimated, {ex_cols}, {su_cols}
        FROM macro_release_events e
        LEFT JOIN macro_series m ON m.series_id = e.series_id
        {ex_join} {su_join}
        WHERE e.release_event_id IN ({ph}) ORDER BY e.series_id""", rel_ids)
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[r["release_event_id"]].append({
            "series_id": r["series_id"], "title": r["title"], "theme": r["theme"], "measure": r["measure"],
            "period": iso(r["period"]), "status": r["status"], "actual": r["actual"], "prior": r["prior"],
            "prior_revised": r["prior_revised"], "prior_revision": r["prior_revision"],
            "first_seen_at": iso(r["first_seen_at"]), "knowledge_date_estimated": r["knowledge_date_estimated"],
            "expectation": _expectation(r), "surprise_z": r.get("surprise_z"), "signed_z": r.get("signed_z")})
    return out


@router.get("/macro/v2/calendar", operation_id="getMacroV2Calendar")
def get_calendar(from_: date | None = Query(None, alias="from"), to: date | None = None,
                 countries: list[str] | None = Query(None), importance: int = Query(3, ge=1, le=3),
                 kind: Literal["data", "policy_meeting"] | None = None, include_inferred: bool = False,
                 db: Db = Depends(get_db)) -> dict[str, Any]:
    today = date.today()
    f = from_ or (today - timedelta(days=7))
    t = to or (today + timedelta(days=30))
    if t < f or (t - f).days > 400:
        raise invalid("'to' must be >= 'from' and the range at most 400 days")
    cs = _csv(countries)
    base = {"as_of": iso(db.as_of), "from": iso(f), "to": iso(t), "expectation_label": MODEL_LABEL,
            "importance_rule": IMPORTANCE_RULE, "importance_tiers": IMPORTANCE_TIERS}
    if not _has(db, "macro_releases"):
        return {**base, "sources": [], "items": [], "data_status": "pending"}
    sql = "SELECT * FROM macro_releases WHERE release_date BETWEEN ? AND ? AND importance <= ?"
    params: list[Any] = [f, t, importance]
    w, p = _in("country", cs)
    sql, params = sql + w, params + p
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if not include_inferred:
        sql += " AND schedule_source = 'official_calendar'"
    rels = db.all(sql + " ORDER BY scheduled_at, importance, country LIMIT 5000", params)
    ser = _series_rows(db, [r["release_event_id"] for r in rels])
    items = [_item(r, ser.get(r["release_event_id"], []), today) for r in rels]
    return {**base, "sources": sorted({r["source_id"] for r in rels}), "items": items,
            "data_status": _status(db, "macro_releases", "macro_release_events", "macro_expectations")}


@router.get("/macro/v2/releases/{release_event_id}", operation_id="getMacroV2Release")
def get_release(release_event_id: str, history: int = Query(24, ge=1, le=120), db: Db = Depends(get_db)) -> dict[str, Any]:
    if not _has(db, "macro_releases"):
        raise not_found(f"release {release_event_id} not found")
    r = db.one("SELECT * FROM macro_releases WHERE release_event_id = ?", [release_event_id])
    if r is None:
        raise not_found(f"release {release_event_id} not found")
    series = _series_rows(db, [release_event_id]).get(release_event_id, [])
    have_ex, have_su = _has(db, "macro_expectations"), _has(db, "macro_surprises")
    for s in series:
        s["expectations"] = [_expectation(x) for x in db.all(
            """SELECT * FROM macro_expectations WHERE release_event_id = ? AND series_id = ?
                 AND target_period IS NOT DISTINCT FROM ?
               QUALIFY version = max(version) OVER ()
               ORDER BY is_primary DESC, model""", [release_event_id, s["series_id"], s["period"]])] if have_ex else []
        su = db.one("SELECT error, error_std FROM macro_surprises WHERE release_event_id = ? AND series_id = ?",
                    [release_event_id, s["series_id"]]) if have_su else None
        s["error"], s["error_std"] = (su or {}).get("error"), (su or {}).get("error_std")
        hist = db.all("""
            SELECT e.release_event_id, e.release_date, e.period, e.actual, s.expectation, s.lo80, s.hi80, s.surprise_z,
                   s.signed_z, s.model
            FROM macro_release_events e LEFT JOIN macro_surprises s USING (release_event_id, series_id)
            WHERE e.series_id = ? AND e.status = 'released' AND e.release_date <= ?
            ORDER BY e.release_date DESC LIMIT ?""", [s["series_id"], r["release_date"], history]) if have_su else []
        s["history"] = [{**h, "release_date": iso(h["release_date"]), "period": iso(h["period"])} for h in reversed(hist)]
    return {"release": _item(r, series, date.today()), "series": series, "expectation_label": MODEL_LABEL,
            "method": SURPRISE_METHOD["z"]}


def _weekly(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not points:
        return points
    keep = [p for p in points if date.fromisoformat(p["date"]).weekday() == 4]
    if keep[-1:] != points[-1:]:
        keep.append(points[-1])
    return keep


@router.get("/macro/v2/surprise", operation_id="getMacroV2Surprise")
def get_surprise(countries: list[str] | None = Query(None), theme: Literal["all", "growth", "inflation", "labor"] = "all",
                 from_: date | None = Query(None, alias="from"), freq: Literal["D", "W"] = "W",
                 db: Db = Depends(get_db)) -> dict[str, Any]:
    cs = _csv(countries)
    base = {"as_of": iso(db.as_of), "theme": theme, "model_based": True,
            "label": "Surprises vs model-based expectations (not consensus)", "method": SURPRISE_METHOD}
    if not _has(db, "macro_surprise_index"):
        return {**base, "latest": [], "series": [], "recent": [], "data_status": "pending"}
    f = from_ or (date.today() - timedelta(days=3 * 365))
    w, p = _in("country", cs)
    latest = db.all(f"""SELECT country, theme, date, value, n_events, last_event_date FROM macro_surprise_index
                        WHERE theme = ? {w} QUALIFY row_number() OVER (PARTITION BY country ORDER BY date DESC) = 1
                        ORDER BY country""", [theme, *p])
    pts = db.all(f"SELECT country, date, value FROM macro_surprise_index WHERE theme = ? AND date >= ? {w} "
                 "ORDER BY country, date", [theme, f, *p])
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for x in pts:
        by[x["country"]].append({"date": iso(x["date"]), "value": x["value"]})
    series = [{"country": c, "theme": theme, "points": v if freq == "D" else _weekly(v)} for c, v in by.items()]
    w2, p2 = _in("s.country", [c for c in cs if c != "GLOBAL"])
    tf = "" if theme == "all" else " AND s.theme = ?"
    recent = db.all(f"""
        SELECT s.release_event_id, s.series_id, m.title, s.country, s.theme, s.release_date, s.actual, s.expectation,
               s.surprise_z, s.signed_z, s.importance
        FROM macro_surprises s LEFT JOIN macro_series m ON m.series_id = s.series_id
        WHERE s.release_date >= ? AND s.surprise_z IS NOT NULL {tf} {w2}
        ORDER BY abs(s.surprise_z) DESC LIMIT 25""",
                    [date.today() - timedelta(days=30), *([theme] if theme != "all" else []), *p2]) \
        if _has(db, "macro_surprises") else []
    for x in recent:
        x["release_date"] = iso(x["release_date"])
    return {**base, "latest": [{**x, "date": iso(x["date"]), "last_event_date": iso(x["last_event_date"])} for x in latest],
            "series": series, "recent": recent, "data_status": _status(db, "macro_surprise_index", "macro_surprises")}


@router.get("/macro/v2/nowcast/{cc}", operation_id="getMacroV2Nowcast")
def get_nowcast(cc: str, quarters: int = Query(4, ge=1, le=16), db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.macro.nowcast import PANELS  # noqa: PLC0415

    cc = cc.upper()
    if cc not in PANELS:
        raise not_found(f"no nowcast model for {cc} (available: {', '.join(PANELS)})")
    p = PANELS[cc]
    base = {"country": cc, "units": p.units, "method": NOWCAST_METHOD, "panel": [s for s, _ in p.monthly]}
    if not _has(db, "macro_nowcasts"):
        return {**base, "targets": [], "data_status": "pending"}
    tq = db.all("""SELECT DISTINCT target_period, target_label FROM macro_nowcasts WHERE country = ?
                   ORDER BY target_period DESC LIMIT ?""", [cc, quarters])
    targets = []
    gdp_sid = {"US": "fred:A191RL1Q225SBEA"}.get(cc)
    for t in tq:
        rows = db.all("""SELECT * FROM macro_nowcasts WHERE country = ? AND target_period = ? ORDER BY model_id, as_of""",
                      [cc, t["target_period"]])
        models: dict[str, dict[str, Any]] = {}
        for r in rows:
            m = models.setdefault(r["model_id"], {"model_id": r["model_id"], "label": r["label"],
                                                  "model_based": bool(r["model_based"]), "official": not r["model_based"],
                                                  "source": r["source"], "latest": None, "prev_nowcast": None,
                                                  "news": [], "history": []})
            pt = {"as_of": iso(r["as_of"]), "nowcast": r["nowcast"], "lo": r["lo"], "hi": r["hi"]}
            m["history"].append(pt)
            m["latest"] = pt
            m["prev_nowcast"] = r["prev_nowcast"]
            m["news"] = jload(r["news"], []) or []
        actual = None
        if gdp_sid or cc in ("EA", "GB", "JP"):
            sid = gdp_sid or {"EA": "ecb:MNA.Q.Y.I10.W2.S1.S1.B.B1GQ._Z._Z._Z.EUR.LR.G1", "GB": "ons:IHYQ",
                              "JP": "oecd:QNA.JPN.G1"}[cc]
            if _has(db, "macro_release_events"):
                actual = db.one("""SELECT actual, release_date FROM macro_release_events WHERE series_id = ? AND period = ?
                                   AND status = 'released' ORDER BY release_date LIMIT 1""", [sid, t["target_period"]])
        targets.append({"target_label": t["target_label"], "target_period": iso(t["target_period"]),
                        "actual": (actual or {}).get("actual"), "actual_release_date": iso((actual or {}).get("release_date")),
                        "models": sorted(models.values(), key=lambda m: (not m["model_based"], m["model_id"]))})
    return {**base, "targets": targets, "data_status": _status(db, "macro_nowcasts")}


@router.get("/macro/v2/recession", operation_id="getMacroV2Recession")
def get_recession(countries: list[str] | None = Query(None), history_months: int = Query(120, ge=0, le=600),
                  db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.macro.recession import LABELS  # noqa: PLC0415

    cs = _csv(countries)
    methods = [{"method": m, "label": LABELS[m], "official": off, "description": d} for m, off, d in RECESSION_METHODS]
    if not _has(db, "macro_recession"):
        return {"as_of": iso(db.as_of), "methods": methods, "countries": [], "data_status": "pending"}
    w, p = _in("country", cs)
    latest = db.all(f"""SELECT * FROM macro_recession WHERE TRUE {w}
                        QUALIFY row_number() OVER (PARTITION BY method, country ORDER BY date DESC) = 1
                        ORDER BY country, method""", p)
    since = date.today() - timedelta(days=31 * history_months)
    hist = db.all(f"""SELECT method, country, date, coalesce(probability, value) AS v FROM macro_recession
                      WHERE date >= ? {w} ORDER BY method, country, date""", [since, *p]) if history_months else []
    hp: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for h in hist:
        hp[(h["method"], h["country"])].append({"date": iso(h["date"]), "value": h["v"]})
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in latest:
        by[r["country"]].append({
            "method": r["method"], "country": r["country"], "date": iso(r["date"]), "probability": r["probability"],
            "value": r["value"], "signal": r["signal"], "threshold": r["threshold"], "horizon_months": r["horizon_months"],
            "method_label": r["method_label"], "official": bool(r["official"]), "source": r["source"],
            "details": jload(r["details"], None), "history": hp.get((r["method"], r["country"]), [])})
    order = {"US": 0, "EA": 1, "GLOBAL": 2}
    return {"as_of": iso(db.as_of), "methods": methods,
            "countries": [{"country": c, "rows": v} for c, v in sorted(by.items(), key=lambda kv: (order.get(kv[0], 9), kv[0]))],
            "data_status": _status(db, "macro_recession")}
