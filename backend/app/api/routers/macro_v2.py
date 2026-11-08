"""Macro v2 (tag: macro-v2). Wave 1 (macro-w1): heatmap, cycle clock, countries, themes, series explorer, compare.
Wave 2 (macro-w2) endpoints (calendar, releases, surprise, nowcast, recession) may be added to this module as separate
functions. All data comes from the serving snapshot tables built by app.api.serving (macro_cells, macro_cell_history,
macro_cycle, macro_revisions, macro_vintages, macro_obs, macro_series).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import invalid, iso, not_found
from app.macro.countries import COUNTRY_META, countries_list, country_page
from app.macro.cycle import METHOD_VERSION as CYCLE_METHOD
from app.macro.indicators import CELL_INDICATORS, COUNTRIES, METHOD_VERSION as CELLS_METHOD
from app.macro.revisions import revision_stats
from app.macro.themes import THEMES, apply_transform, cell_json, compare_items, latest_cells, pts_json, rnd, theme_payload

router = APIRouter(tags=["macro-v2"], route_class=FastRoute)

ThemeId = Literal["inflation", "labor", "rates", "credit_conditions", "liquidity", "commodities_fx", "growth"]
CYCLE_METHOD_TEXT = (
    "Growth momentum = mean of z-scored 6-month changes of the GDP y/y (or q/q), industrial production, retail sales "
    "and PMI-proxy cells; inflation momentum = same for CPI, core CPI and PPI. z = change / trailing 120-month std "
    "(>= 36 months), clipped to +-3; composites smoothed with a 3-month moving average. Quadrants: recovery (G>0, "
    "I<=0), expansion (G>0, I>0), slowdown (G<=0, I>0), contraction (G<=0, I<=0). Inputs are official series on "
    "their canonical measure; history uses latest revised data.")


def _csv(v: list[str] | None) -> list[str]:
    out: list[str] = []
    for x in v or []:
        out += [p.strip() for p in x.split(",") if p.strip()]
    return out


def _countries(v: list[str] | None) -> list[str]:
    cs = [c.upper() for c in _csv(v)]
    bad = [c for c in cs if c not in COUNTRY_META]
    if bad:
        raise invalid(f"unknown countries {bad}; allowed: {COUNTRIES}")
    return cs


def _indicators(v: list[str] | None) -> list[str]:
    inds = _csv(v)
    bad = [i for i in inds if i not in CELL_INDICATORS]
    if bad:
        raise invalid(f"unknown indicators {bad}; allowed: {list(CELL_INDICATORS)}")
    return inds


def _indicator_meta(keys: list[str] | None = None) -> list[dict[str, Any]]:
    return [{"key": k, "label": v["label"], "group": v["group"], "good_direction": v["good"],
             "transform": v["tf"]} for k, v in CELL_INDICATORS.items() if not keys or k in keys]


# ------------------------------------------------------------------------------------------------ heatmap


@router.get("/macro/v2/heatmap", operation_id="getMacroV2Heatmap")
def get_heatmap(countries: list[str] | None = Query(None), indicators: list[str] | None = Query(None),
                as_of: date | None = None, db: Db = Depends(get_db)) -> dict[str, Any]:
    cs, inds = _countries(countries), _indicators(indicators)
    a = min(as_of or db.as_of, db.as_of)
    snap = db.scalar("SELECT max(as_of_date) FROM macro_cells WHERE as_of_date <= ?", [a])
    rows = latest_cells(db, cs or None, inds or None, a) if snap else []
    want_c = cs or COUNTRIES
    want_i = inds or list(CELL_INDICATORS)
    by_c: dict[str, int] = {}
    for r in rows:
        by_c[r["country"]] = by_c.get(r["country"], 0) + 1
    snaps = [iso(r["d"]) for r in db.all("SELECT DISTINCT as_of_date AS d FROM macro_cells ORDER BY 1 DESC LIMIT 60")]
    return {"as_of": iso(a), "snapshot_date": iso(snap), "method_version": CELLS_METHOD,
            "countries": [{"country": c, "name": COUNTRY_META[c]["name"]} for c in want_c],
            "indicators": _indicator_meta(want_i), "cells": [cell_json(r) for r in rows],
            "coverage": {"n_cells": len(rows), "n_possible": len(want_c) * len(want_i), "by_country": by_c},
            "available_snapshots": snaps, "data_status": "ok" if rows else "pending"}


# ------------------------------------------------------------------------------------------------ cycle clock


def _cyc_point(r: dict[str, Any]) -> dict[str, Any]:
    return {"date": iso(r["date"]), "growth_momentum": rnd(r.get("growth_momentum")),
            "inflation_momentum": rnd(r.get("inflation_momentum")), "quadrant": r.get("quadrant")}


@router.get("/macro/v2/cycle", operation_id="getMacroV2Cycle")
def get_cycle(countries: list[str] | None = Query(None), trail: int = Query(12, ge=1, le=120),
              db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.api.routers._common import jload  # noqa: PLC0415

    cs = _countries(countries) or COUNTRIES
    ph = ", ".join("?" for _ in cs)
    rows = db.all(f"""SELECT * FROM macro_cycle WHERE country IN ({ph}) AND date <= ?
                      QUALIFY row_number() OVER (PARTITION BY country ORDER BY date DESC) <= ? ORDER BY country, date""",
                  [*cs, db.as_of, trail])
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["country"], []).append(r)
    out, counts = [], {q: 0 for q in ("recovery", "expansion", "slowdown", "contraction")}
    for cc in cs:
        rr = by.get(cc) or []
        last = rr[-1] if rr else None
        if last and last.get("quadrant"):
            counts[last["quadrant"]] += 1
        out.append({"country": cc, "name": COUNTRY_META[cc]["name"],
                    **({"date": iso(last["date"]), "growth_momentum": rnd(last.get("growth_momentum")),
                        "inflation_momentum": rnd(last.get("inflation_momentum")), "quadrant": last.get("quadrant"),
                        "angle_deg": rnd(last.get("angle_deg"), 2), "magnitude": rnd(last.get("magnitude")),
                        "n_growth": last.get("n_growth"), "n_inflation": last.get("n_inflation"),
                        "inputs": jload(last.get("inputs"), {}) or {}} if last else
                       {"date": None, "growth_momentum": None, "inflation_momentum": None, "quadrant": None,
                        "angle_deg": None, "magnitude": None, "n_growth": 0, "n_inflation": 0, "inputs": {}}),
                    "trail": [_cyc_point(r) for r in rr]})
    return {"as_of": iso(db.as_of), "method_version": CYCLE_METHOD, "method": CYCLE_METHOD_TEXT,
            "quadrant_counts": counts, "countries": out, "data_status": "ok" if rows else "pending"}


# ------------------------------------------------------------------------------------------------ countries


@router.get("/macro/v2/countries", operation_id="listMacroV2Countries")
def get_countries(db: Db = Depends(get_db)) -> dict[str, Any]:
    items = countries_list(db)
    return {"as_of": iso(db.as_of), "countries": items,
            "data_status": "ok" if any(i["n_cells"] for i in items) else "pending"}


@router.get("/macro/v2/countries/{cc}", operation_id="getMacroV2Country")
def get_country(cc: str, years: int = Query(10, ge=1, le=40), db: Db = Depends(get_db)) -> dict[str, Any]:
    if cc.upper() not in COUNTRY_META:
        raise not_found(f"unknown country {cc!r}; allowed: {COUNTRIES}")
    return country_page(db, cc.upper(), years)


# ------------------------------------------------------------------------------------------------ themes


@router.get("/macro/v2/themes/{theme}", operation_id="getMacroV2Theme")
def get_theme(theme: ThemeId, years: int = Query(10, ge=1, le=40), countries: list[str] | None = Query(None),
              db: Db = Depends(get_db)) -> dict[str, Any]:
    assert theme in THEMES
    payload = theme_payload(db, theme, years, _countries(countries) or None)
    n = sum(1 for p in payload["panels"] for s in (p.get("series") or p.get("curves") or [])
            if s.get("available", True))
    payload["data_status"] = "ok" if n else "pending"
    return payload


# ------------------------------------------------------------------------------------------------ series explorer


def _provenance(m: dict[str, Any]) -> dict[str, Any]:
    from app.ingestion.macro_common import catalog_doc  # noqa: PLC0415

    src = m.get("source_id") or ""
    sources = (catalog_doc().get("sources") or {})
    url = (sources.get(src) or {}).get("url")
    native = m.get("native_code") or m["series_id"].split(":", 1)[-1]
    page = f"https://fred.stlouisfed.org/series/{native}" if src == "fred" else None
    return {"source_id": src, "native_code": native, "source_endpoint": url, "source_page": page,
            "official": src not in ("akshare_cn", "akshare"),
            "seasonal_adjustment": m.get("seasonal_adjustment"), "release_lag_days": m.get("release_lag_days"),
            "updated_at": iso(m.get("updated_at"))}


@router.get("/macro/v2/series/{series_id:path}", operation_id="getMacroV2Series")
def get_series(series_id: str, transform: Literal["level", "yoy", "mom", "z", "canonical"] = "level",
               from_: date | None = Query(None, alias="from"), to: date | None = None, vintages: bool = True,
               db: Db = Depends(get_db)) -> dict[str, Any]:
    m = db.one("SELECT * FROM macro_series WHERE series_id = ?", [series_id])
    if m is None:
        raise not_found(f"unknown macro series {series_id!r}")
    end = min(to or db.as_of, db.as_of)
    start = from_ or date(1900, 1, 1)
    freq = m.get("frequency") or "M"
    lead = start - timedelta(days=4000) if transform == "z" else (start - timedelta(days=400) if transform != "level" else start)
    rows = db.all("SELECT date, value FROM macro_obs WHERE series_id = ? AND date BETWEEN ? AND ? ORDER BY date",
                  [series_id, lead, end])
    native = [(r["date"], r["value"]) for r in rows]
    canon_rows = db.all("""SELECT measure, indicator, date, value, units, transform, is_primary FROM macro_cell_history
                           WHERE series_id = ? AND date BETWEEN ? AND ? ORDER BY measure, date""", [series_id, start, end])
    canon: dict[str, dict[str, Any]] = {}
    for r in canon_rows:
        c = canon.setdefault(r["measure"], {"measure": r["measure"], "indicator": r["indicator"], "units": r["units"],
                                            "transform": r["transform"], "is_primary": bool(r["is_primary"]), "points": []})
        c["points"].append({"date": iso(r["date"]), "value": rnd(r["value"], 6)})
    if transform == "canonical":
        first = next(iter(canon.values()), None)
        points = first["points"] if first else []
        units = first["units"] if first else m.get("units")
    else:
        pts = [(d, v) for d, v in apply_transform(native, transform, freq) if d >= start]
        points = pts_json(pts)
        units = {"yoy": "% y/y", "mom": "% m/m", "z": "z"}.get(transform, m.get("units"))
    vint = None
    revs = db.all("SELECT * FROM macro_revisions WHERE series_id = ? ORDER BY date", [series_id]) if vintages else []
    if revs:
        sel = [r for r in revs if start <= r["date"] <= end]
        vint = {"kind": revs[0]["vintage_kind"], "transform": revs[0]["transform"],
                "first_release": [{"date": iso(r["date"]), "value": rnd(r["first_value"], 6),
                                   "value_tf": rnd(r["first_value_tf"], 6), "release_date": iso(r["first_knowledge_date"]),
                                   "release_date_estimated": r["first_estimated"]} for r in sel],
                "latest": [{"date": iso(r["date"]), "value": rnd(r["latest_value"], 6),
                            "value_tf": rnd(r["latest_value_tf"], 6), "vintage_date": iso(r["latest_knowledge_date"]),
                            "n_vintages": r["n_vintages"]} for r in sel],
                "revision_stats": {"5y": _rs(revs, db.as_of, 5), "10y": _rs(revs, db.as_of, 10), "all": _rs(revs, None, 0)}}
    rel = db.all("""SELECT date, max(knowledge_date) AS k, min(knowledge_date) AS k0, count(*) AS n FROM macro_vintages
                    WHERE series_id = ? GROUP BY 1 ORDER BY 1 DESC LIMIT 24""", [series_id])
    cells = [cell_json(c) for c in db.all("""SELECT * FROM macro_cells WHERE (series_id = ? OR list_contains(inputs, ?))
                 AND as_of_date = (SELECT max(as_of_date) FROM macro_cells WHERE as_of_date <= ?)""",
                                          [series_id, series_id, db.as_of])]
    return {"meta": {"series_id": series_id, "title": m.get("title"), "country": m.get("country"),
                     "category": m.get("category"), "indicator": m.get("indicator"), "frequency": freq,
                     "units": m.get("units"), "scale": m.get("scale"), "default_transform": m.get("default_transform"),
                     "is_core": bool(m.get("is_core")), "last_observation_date": iso(m.get("last_observation_date"))},
            "provenance": _provenance(m), "transform": transform, "units": units, "points": points,
            "canonical": list(canon.values()), "vintages": vint,
            "releases": [{"date": iso(r["date"]), "first_release_date": iso(r["k0"]), "latest_vintage_date": iso(r["k"]),
                          "n_vintages": r["n"]} for r in rel],
            "cells": cells, "data_status": "ok" if points else "pending"}


def _rs(revs: list[dict[str, Any]], as_of: date | None, years: int) -> dict[str, Any]:
    since = (as_of - timedelta(days=int(365.25 * years))) if as_of and years else None
    st = revision_stats(revs, since)
    return {k: ({kk: rnd(vv, 6) if isinstance(vv, float) else vv for kk, vv in v.items()} if isinstance(v, dict) else v)
            for k, v in st.items()}


# ------------------------------------------------------------------------------------------------ compare


@router.get("/macro/v2/compare", operation_id="getMacroV2Compare")
def get_compare(indicator: str, countries: list[str] | None = Query(None), years: int = Query(10, ge=1, le=40),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    if indicator not in CELL_INDICATORS:
        raise invalid(f"unknown indicator {indicator!r}; allowed: {list(CELL_INDICATORS)}")
    cs = _countries(countries) or COUNTRIES
    items = compare_items(db, indicator, cs, years)
    for it in items:
        it["name"] = COUNTRY_META[it["country"]]["name"]
    return {"indicator": indicator, **_indicator_meta([indicator])[0], "as_of": iso(db.as_of), "years": years,
            "countries": items, "data_status": "ok" if any(i["available"] for i in items) else "pending"}
