"""Event models (tag: events-models; event-models agent, plan B / C / E1). Reads the em_* serving tables built from
event_hotspots, spatial_spillovers, connectedness, event_irf, event_escalation, event_changepoints, gpr_index,
event_study_library and company_event_exposure. Every output is MODEL-BASED (model_based = true).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

import polars as pl

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import invalid, iso, jload, not_found, rnd
from app.events_models.event_study import expected_reaction  # imported at startup (statsmodels import ~10 s cold)

router = APIRouter(tags=["events-models"], route_class=FastRoute)

P = "/events/models"
_LAKE = ("event_hotspots", "spatial_spillovers", "connectedness", "event_irf", "event_escalation", "event_changepoints",
         "gpr_index", "event_study_library", "company_event_exposure")
GPR_LABEL = ("Model-based geopolitical-risk index from labelled GDELT coverage (share of geopolitical-theme items, "
             "base mean = 100), in the spirit of Caldara-Iacoviello (2022); not the official GPR.")


def _has(db: Db, table: str) -> bool:
    return bool(db.scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]))


def _status(db: Db, *tables: str) -> str:
    st = db.snap.status(*tables)
    return st


def _names(db: Db) -> tuple[dict[str, str], dict[str, str]]:
    if not _has(db, "ev2_dict"):
        return {}, {}
    rows = db.all("SELECT dict_type, code, name FROM ev2_dict WHERE dict_type IN ('country', 'sector')")
    return ({r["code"]: r["name"] for r in rows if r["dict_type"] == "country"},
            {r["code"]: r["name"] for r in rows if r["dict_type"] == "sector"})


def _f(v: Any, nd: int = 6) -> float | None:
    return rnd(v, nd)


# ------------------------------------------------------------------------------------------------ hotspots
@router.get(f"{P}/hotspots", operation_id="getEventsModelsHotspots")
def get_hotspots(week: date | None = None, theme: str = "all",
                 measure: Literal["attention", "tone", "intensity"] = "attention",
                 only_significant: bool = False, db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "event_hotspots")
    base = {"week": None, "theme": theme, "measure": measure, "weeks_available": [], "global_moran": None,
            "w_kind": None, "w_source": None, "summary": {"hot": 0, "cold": 0, "hh": 0, "ll": 0, "emerging": 0},
            "items": [], "model_based": True, "data_status": st}
    if not _has(db, "em_hotspots"):
        return base | {"data_status": "pending"}
    weeks = [r["week"] for r in db.all("SELECT DISTINCT week FROM em_hotspots WHERE theme = ? AND measure = ? "
                                       "ORDER BY week DESC LIMIT 104", [theme, measure])]
    if not weeks:
        if not db.scalar("SELECT count(*) FROM em_hotspots WHERE theme = ?", [theme]) and \
                db.scalar("SELECT count(*) FROM em_hotspots"):
            raise invalid(f"no hotspots for theme {theme!r} (theme id of /events/v2/taxonomy or 'all')")
        return base
    w = week or weeks[0]
    if week is not None:
        w = week - timedelta(days=week.weekday())
    rows = db.all("SELECT * FROM em_hotspots WHERE week = ? AND theme = ? AND measure = ? ORDER BY gi_z DESC",
                  [w, theme, measure])
    if not rows:
        raise not_found(f"no hotspot cross-section for week {w}")
    cn, _ = _names(db)
    items = []
    for r in rows:
        sig = r["hotspot"] != "ns" or r["lisa_quadrant"] != "ns"
        if only_significant and not sig:
            continue
        items.append({"iso2": r["iso2"], "name": cn.get(r["iso2"]), "value": _f(r["value"]), "n_items": int(r["n_items"]),
                      "lisa_i": _f(r["lisa_i"]), "lisa_p": _f(r["lisa_p"]), "lisa_quadrant": r["lisa_quadrant"],
                      "gi_z": _f(r["gi_z"]), "gi_p": _f(r["gi_p"]), "hotspot": r["hotspot"],
                      "emerging_class": r["emerging_class"] or "none", "n_weeks_hist": int(r["n_weeks_hist"] or 0)})
    r0 = rows[0]
    return base | {"week": iso(w), "weeks_available": [iso(x) for x in weeks],
                   "global_moran": {"moran_i": _f(r0["moran_i"]), "p_value": _f(r0["moran_p"]), "z": _f(r0["moran_z"]),
                                    "n_units": int(r0["n_units"])},
                   "w_kind": r0["w_kind"], "w_source": r0["w_source"], "items": items,
                   "summary": {"hot": sum(r["hotspot"].startswith("hot") for r in rows),
                               "cold": sum(r["hotspot"].startswith("cold") for r in rows),
                               "hh": sum(r["lisa_quadrant"] == "HH" for r in rows),
                               "ll": sum(r["lisa_quadrant"] == "LL" for r in rows),
                               "emerging": sum((r["emerging_class"] or "none") != "none" for r in rows)}}


# ------------------------------------------------------------------------------------------------ spillovers
def _est(r: dict[str, Any]) -> dict[str, Any]:
    return {"effect": r["effect"], "estimate": _f(r["estimate"]), "std_error": _f(r["std_error"]),
            "ci_low": _f(r["ci_low"]), "ci_high": _f(r["ci_high"]), "p_value": _f(r["p_value"])}


@router.get(f"{P}/spillovers", operation_id="getEventsModelsSpillovers")
def get_spillovers(response: Literal["ret", "vol", "macro_surprise"] = "ret", shock: str = "attention_all",
                   w_kind: str = "contiguity", as_of: date | None = None, limit: int = Query(10, ge=1, le=100),
                   db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "spatial_spillovers")
    base = {"as_of": None, "window_start": None, "window_end": None, "freq": None, "response": response, "shock": shock,
            "w_kind": w_kind, "w_source": None, "n_units": 0, "n_periods": 0, "models": [], "diagnostics": [],
            "transmitters": [], "receivers": [], "available": {"responses": [], "shocks": [], "w_kinds": [], "as_of": []},
            "model_based": True, "data_status": st}
    if not _has(db, "em_spillovers"):
        return base | {"data_status": "pending"}
    avail = db.all("SELECT list(DISTINCT response) AS r, list(DISTINCT shock) AS s, list(DISTINCT w_kind) AS w, "
                   "list(DISTINCT as_of ORDER BY as_of DESC) AS a FROM em_spillovers")[0]
    base["available"] = {"responses": sorted(avail["r"] or []), "shocks": sorted(avail["s"] or []),
                         "w_kinds": sorted(avail["w"] or []), "as_of": [iso(x) for x in (avail["a"] or [])][:52]}
    a = as_of or db.scalar("SELECT max(as_of) FROM em_spillovers WHERE response = ? AND shock = ? AND w_kind = ?",
                           [response, shock, w_kind])
    if a is None:
        if avail["s"] and shock not in avail["s"]:
            raise invalid(f"unknown shock {shock!r}; one of {sorted(avail['s'])}")
        return base
    rows = db.all("SELECT * FROM em_spillovers WHERE as_of = ? AND response = ? AND shock = ? AND w_kind = ?",
                  [a, response, shock, w_kind])
    if not rows:
        raise not_found(f"no spillover run for as_of {a}")
    cn, _ = _names(db)
    models = []
    for m in ("ols", "slx", "sar", "sem", "sdm"):
        mr = [r for r in rows if r["model"] == m and r["iso2"] == "*" and not r["effect"].startswith(("lm_", "rlm_"))]
        if mr:
            models.append({"model": m, "llf": _f(mr[0]["llf"]), "aic": _f(mr[0]["aic"]), "pseudo_r2": _f(mr[0]["pseudo_r2"]),
                           "params": [_est(r) for r in mr]})
    diag = [{"test": r["effect"], "statistic": _f(r["estimate"]), "p_value": _f(r["p_value"])}
            for r in rows if r["effect"].startswith(("lm_", "rlm_"))]
    tr = {}
    for r in rows:
        if r["model"] == "sdm" and r["effect"] in ("transmit", "receive"):
            tr.setdefault(r["iso2"], {"iso2": r["iso2"], "name": cn.get(r["iso2"]), "transmit": None, "receive": None})
            tr[r["iso2"]][r["effect"]] = _f(r["estimate"])
    nodes = list(tr.values())
    r0 = rows[0]
    return base | {"as_of": iso(a), "window_start": iso(r0["window_start"]), "window_end": iso(r0["window_end"]),
                   "freq": r0["freq"], "w_source": r0["w_source"], "n_units": int(r0["n_units"]),
                   "n_periods": int(r0["n_periods"]), "models": models, "diagnostics": diag,
                   "transmitters": sorted(nodes, key=lambda x: -abs(x["transmit"] or 0))[:limit],
                   "receivers": sorted(nodes, key=lambda x: -abs(x["receive"] or 0))[:limit]}


# ------------------------------------------------------------------------------------------------ connectedness
@router.get(f"{P}/connectedness", operation_id="getEventsModelsConnectedness")
def get_connectedness(universe: Literal["country_ret", "country_vol", "sector_ret", "sector_vol"] = "country_ret",
                      date_: date | None = Query(None, alias="date"), history_days: int = Query(1095, ge=30, le=6000),
                      pairs: int = Query(40, ge=0, le=500), db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "connectedness")
    base = {"universe": universe, "date": None, "total": None, "window": None, "horizon": None, "lags": None,
            "n_nodes": 0, "nodes": [], "pairs": [], "history": [], "model_based": True, "data_status": st}
    if not _has(db, "em_connectedness"):
        return base | {"data_status": "pending"}
    d = db.scalar("SELECT max(date) FROM em_connectedness WHERE universe = ? AND row_type = 'system'"
                  + (" AND date <= ?" if date_ else ""), [universe] + ([date_] if date_ else []))
    if d is None:
        return base
    sysr = db.one("SELECT * FROM em_connectedness WHERE universe = ? AND date = ? AND row_type = 'system'", [universe, d])
    cn, sn = _names(db)
    names = sn if universe.startswith("sector") else cn
    nodes = db.all("SELECT node, series_id, to_others, from_others, net FROM em_connectedness WHERE universe = ? AND "
                   "date = ? AND row_type = 'node' ORDER BY net DESC", [universe, d])
    pr = db.all("SELECT node, other, net FROM em_connectedness WHERE universe = ? AND date = (SELECT max(date) FROM "
                "em_connectedness WHERE universe = ? AND row_type = 'pair' AND date <= ?) AND row_type = 'pair' AND net > 0 "
                "ORDER BY net DESC LIMIT ?", [universe, universe, d, pairs])
    hist = db.all("SELECT date, total FROM em_connectedness WHERE universe = ? AND row_type = 'system' AND date BETWEEN ? "
                  "AND ? ORDER BY date", [universe, d - timedelta(days=history_days), d])
    return base | {"date": iso(d), "total": _f(sysr["total"]), "window": int(sysr["window"]),
                   "horizon": int(sysr["horizon"]), "lags": int(sysr["lags"]), "n_nodes": int(sysr["n_nodes"]),
                   "nodes": [{"node": r["node"], "name": names.get(r["node"]), "series_id": r["series_id"],
                              "to_others": _f(r["to_others"]), "from_others": _f(r["from_others"]), "net": _f(r["net"])}
                             for r in nodes],
                   "pairs": [{"from": r["node"], "to": r["other"], "net": _f(r["net"])} for r in pr],
                   "history": [{"date": iso(r["date"]), "total": _f(r["total"])} for r in hist]}


# ------------------------------------------------------------------------------------------------ irf
@router.get(f"{P}/irf", operation_id="getEventsModelsIrf")
def get_irf(response: Literal["ret", "vol"] = "ret", shock: str = "attention_all", model: Literal["lp", "svar"] = "lp",
            scope: str = "*", db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "event_irf")
    base = {"as_of": None, "window_start": None, "window_end": None, "response": response, "shock": shock,
            "model": model, "scope": scope, "w_kind": None, "scopes": [], "components": [], "model_based": True,
            "data_status": st}
    if not _has(db, "em_irf"):
        return base | {"data_status": "pending"}
    a = db.scalar("SELECT max(as_of) FROM em_irf WHERE response = ? AND shock = ? AND model = ?", [response, shock, model])
    if a is None:
        return base
    scopes = [r["scope"] for r in db.all("SELECT DISTINCT scope FROM em_irf WHERE as_of = ? AND model = ? AND response = ? "
                                         "AND shock = ? ORDER BY scope", [a, model, response, shock])]
    if scope not in scopes:
        raise not_found(f"no {model} IRF for scope {scope!r}; available {scopes}")
    rows = db.all("SELECT * FROM em_irf WHERE as_of = ? AND model = ? AND response = ? AND shock = ? AND scope = ? "
                  "ORDER BY component, h", [a, model, response, shock, scope])
    comps: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        comps.setdefault(r["component"], []).append({"h": int(r["h"]), "coef": _f(r["coef"]), "std_error": _f(r["std_error"]),
                                                     "ci_low": _f(r["ci_low"]), "ci_high": _f(r["ci_high"]),
                                                     "p_value": _f(r["p_value"]), "n_obs": int(r["n_obs"])})
    r0 = rows[0]
    return base | {"as_of": iso(a), "window_start": iso(r0["window_start"]), "window_end": iso(r0["window_end"]),
                   "w_kind": r0["w_kind"], "scopes": scopes,
                   "components": [{"component": k, "points": v} for k, v in comps.items()]}


# ------------------------------------------------------------------------------------------------ escalation
@router.get(f"{P}/escalation", operation_id="getEventsModelsEscalation")
def get_escalation(theme_group: str | None = None, iso2: list[str] | None = Query(None),
                   sort: Literal["p_escalation_7d", "p_escalation_30d", "branching_ratio", "expected_7d"] = "p_escalation_7d",
                   min_events: int = Query(100, ge=0), limit: int = Query(50, ge=1, le=1000),
                   db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "event_escalation")
    base = {"as_of": None, "window_start": None, "n_days": 0, "theme_groups": [], "items": [], "model_based": True,
            "data_status": st}
    if not _has(db, "em_escalation"):
        return base | {"data_status": "pending"}
    a = db.scalar("SELECT max(as_of) FROM em_escalation")
    if a is None:
        return base
    groups = [r["theme_group"] for r in db.all("SELECT DISTINCT theme_group FROM em_escalation WHERE as_of = ? ORDER BY 1", [a])]
    if theme_group and theme_group not in groups:
        raise invalid(f"unknown theme_group {theme_group!r}; one of {groups}")
    where, args = ["as_of = ?", "n_events >= ?"], [a, min_events]
    if theme_group:
        where.append("theme_group = ?")
        args.append(theme_group)
    if iso2:
        where.append(f"iso2 IN ({', '.join('?' for _ in iso2)})")
        args += [c.upper() for c in iso2]
    rows = db.all(f"SELECT * FROM em_escalation WHERE {' AND '.join(where)} ORDER BY {sort} DESC NULLS LAST, iso2 LIMIT ?",
                  args + [limit])
    cn, _ = _names(db)
    keys = ("mu", "alpha_self", "alpha_from_nbrs", "alpha_to_nbrs", "beta", "branching_ratio", "lambda_now", "expected_7d",
            "expected_30d", "threshold_7d", "threshold_30d", "p_escalation_7d", "p_escalation_30d")
    r0 = db.one("SELECT window_start, n_days FROM em_escalation WHERE as_of = ? LIMIT 1", [a])
    return base | {"as_of": iso(a), "window_start": iso(r0["window_start"]), "n_days": int(r0["n_days"]),
                   "theme_groups": groups,
                   "items": [{"iso2": r["iso2"], "name": cn.get(r["iso2"]), "theme_group": r["theme_group"],
                              "n_events": int(r["n_events"]), **{k: _f(r[k]) for k in keys},
                              "converged": bool(r["converged"])} for r in rows]}


# ------------------------------------------------------------------------------------------------ changepoints
@router.get(f"{P}/changepoints", operation_id="getEventsModelsChangepoints")
def get_changepoints(label_type: Literal["country", "sector", "issuer"] | None = None, label: str | None = None,
                     stream: Literal["attention", "tone"] | None = None, theme: str | None = None,
                     direction: Literal["up", "down", "flat"] | None = None, days: int = Query(30, ge=1, le=730),
                     min_shift: float = Query(0.0, ge=0, le=100), limit: int = Query(100, ge=1, le=2000),
                     db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "event_changepoints")
    if not _has(db, "em_changepoints"):
        return {"start": None, "end": None, "items": [], "model_based": True, "data_status": "pending"}
    end = db.scalar("SELECT max(date) FROM em_changepoints") or db.as_of
    start = end - timedelta(days=days - 1)
    where, args = ["date BETWEEN ? AND ?", "abs(coalesce(shift_sd, 0)) >= ?"], [start, end, min_shift]
    for col, v in (("label_type", label_type), ("stream", stream), ("theme", theme), ("direction", direction)):
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    if label:
        where.append("upper(label) = ?")
        args.append(label.upper())
    rows = db.all(f"SELECT * FROM em_changepoints WHERE {' AND '.join(where)} ORDER BY date DESC, abs(shift_sd) DESC NULLS LAST "
                  f"LIMIT ?", args + [limit])
    cn, sn = _names(db)
    return {"start": iso(start), "end": iso(end), "model_based": True, "data_status": st,
            "items": [{"label_type": r["label_type"], "label": r["label"],
                       "name": (cn if r["label_type"] == "country" else sn if r["label_type"] == "sector" else {}).get(r["label"]),
                       "theme": r["theme"], "stream": r["stream"], "date": iso(r["date"]), "cp_prob": _f(r["cp_prob"]),
                       "run_length_before": r["run_length_before"], "mean_before": _f(r["mean_before"]),
                       "mean_after": _f(r["mean_after"]), "shift_sd": _f(r["shift_sd"]), "direction": r["direction"],
                       "n_after": int(r["n_after"])} for r in rows]}


# ------------------------------------------------------------------------------------------------ GPR
@router.get(f"{P}/gpr", operation_id="getEventsModelsGpr")
def get_gpr(iso2: list[str] | None = Query(None), start: date | None = None, end: date | None = None,
            days: int = Query(365, ge=7, le=6000), top: int = Query(30, ge=0, le=300),
            db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "gpr_index")
    base = {"label": GPR_LABEL, "base_start": None, "base_end": None, "start": None, "end": None, "series": [],
            "latest": [], "validation": None, "model_based": True, "data_status": st}
    if not _has(db, "em_gpr"):
        return base | {"data_status": "pending"}
    e = end or db.scalar("SELECT max(date) FROM em_gpr")
    if e is None:
        return base
    s = start or (e - timedelta(days=days - 1))
    codes = [c.upper() for c in (iso2 or ["GLOBAL"])]
    cn, _ = _names(db)
    known = {r["iso2"] for r in db.all(f"SELECT DISTINCT iso2 FROM em_gpr WHERE iso2 IN ({', '.join('?' for _ in codes)})", codes)}
    missing = [c for c in codes if c not in known]
    if missing:
        raise not_found(f"no GPR series for {missing}")
    series = []
    for c in codes:
        rows = db.all("SELECT date, gpr, gpr_threats, gpr_acts, gpr_ma7, share, n_items, reference_gpr FROM em_gpr "
                      "WHERE iso2 = ? AND date BETWEEN ? AND ? ORDER BY date", [c, s, e])
        series.append({"iso2": c, "name": "Global" if c == "GLOBAL" else cn.get(c),
                       "points": [{"date": iso(r["date"]), "gpr": _f(r["gpr"]), "gpr_threats": _f(r["gpr_threats"]),
                                   "gpr_acts": _f(r["gpr_acts"]), "gpr_ma7": _f(r["gpr_ma7"]), "share": _f(r["share"]),
                                   "n_items": int(r["n_items"]), "reference_gpr": _f(r["reference_gpr"])} for r in rows]})
    lat = db.all("""SELECT l.iso2, l.date, l.gpr, l.gpr_ma7, p.gpr_ma7 AS prev FROM
                      (SELECT * FROM em_gpr WHERE date = ? AND iso2 <> 'GLOBAL') l
                      LEFT JOIN em_gpr p ON p.iso2 = l.iso2 AND p.date = l.date - INTERVAL 7 DAY
                    ORDER BY l.gpr_ma7 DESC NULLS LAST LIMIT ?""", [e, top])
    b = db.one("SELECT base_start, base_end FROM em_gpr LIMIT 1")
    val = db.one("SELECT report_date, report FROM em_gpr_validation ORDER BY report_date DESC LIMIT 1") \
        if _has(db, "em_gpr_validation") else None
    return base | {"base_start": iso(b["base_start"]), "base_end": iso(b["base_end"]), "start": iso(s), "end": iso(e),
                   "series": series,
                   "latest": [{"iso2": r["iso2"], "name": cn.get(r["iso2"]), "date": iso(r["date"]), "gpr": _f(r["gpr"]),
                               "gpr_ma7": _f(r["gpr_ma7"]),
                               "change_7d": _f(r["gpr_ma7"] - r["prev"]) if r["gpr_ma7"] is not None and r["prev"] is not None else None}
                              for r in lat],
                   "validation": ({"report_date": iso(val["report_date"]), **(jload(val["report"], {}) or {})} if val else None)}


# ------------------------------------------------------------------------------------------------ event studies
_CELL = ("shock_kind", "level", "theme", "iso2", "sector", "window", "n_events", "mean_car", "median_car", "std_error",
         "ci_low", "ci_high", "t_stat", "p_value", "mean_bhar", "share_negative", "first_event", "last_event", "benchmark")


def _cell(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: r[k] for k in _CELL}
    for k in ("mean_car", "median_car", "std_error", "ci_low", "ci_high", "t_stat", "p_value", "mean_bhar", "share_negative"):
        out[k] = _f(out[k])
    out["n_events"] = int(out["n_events"])
    out["first_event"], out["last_event"] = iso(out["first_event"]), iso(out["last_event"])
    return out


@router.get(f"{P}/event-studies", operation_id="getEventsModelsEventStudies")
def get_event_studies(theme: str | None = None, iso2: str | None = None, sector: str | None = None,
                      level: Literal["country_index", "sector_etf", "issuer"] | None = None,
                      shock_kind: Literal["attention", "neg_tone"] = "attention", window: str = "0,5",
                      min_n: int = Query(3, ge=2, le=1000), max_p: float = Query(1.0, gt=0, le=1),
                      sort: Literal["p_value", "mean_car", "n_events"] = "p_value", limit: int = Query(100, ge=1, le=2000),
                      db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "event_study_library")
    base = {"windows": [], "items": [], "expected": None, "model_based": True, "data_status": st}
    if not _has(db, "em_event_studies"):
        return base | {"data_status": "pending"}
    windows = [r["window"] for r in db.all('SELECT DISTINCT "window" FROM em_event_studies ORDER BY 1')]
    if windows and window not in windows:
        raise invalid(f"unknown window {window!r}; one of {windows}")
    where = ["shock_kind = ?", '"window" = ?', "n_events >= ?", "coalesce(p_value, 1) <= ?"]
    args: list[Any] = [shock_kind, window, min_n, max_p]
    for col, v in (("theme", theme), ("level", level)):
        if v:
            where.append(f"{col} = ?")
            args.append(v)
    if iso2:
        where.append("iso2 = ?")
        args.append(iso2.upper())
    if sector:
        where.append("sector = ?")
        args.append(sector.upper())
    order = {"p_value": "p_value ASC NULLS LAST", "mean_car": "abs(mean_car) DESC NULLS LAST", "n_events": "n_events DESC"}[sort]
    rows = db.all(f"SELECT * FROM em_event_studies WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ?", args + [limit])
    expected = None
    if theme:
        cand = db.all('SELECT * FROM em_event_studies WHERE theme = ? AND shock_kind = ? AND "window" = ?',
                      [theme, shock_kind, window])
        if cand:
            hit = expected_reaction(pl.DataFrame(cand, infer_schema_length=None), theme, iso2.upper() if iso2 else None,
                                    sector.upper() if sector else None, window, shock_kind, level, min_n)
            if hit:
                expected = _cell(hit) | {"match": hit["match"]}
    return base | {"windows": windows, "items": [_cell(r) for r in rows], "expected": expected}


# ------------------------------------------------------------------------------------------------ company
@router.get(P + "/company/{security_id}", operation_id="getEventsModelsCompany")
def get_company(security_id: str, history: int = Query(26, ge=1, le=104), db: Db = Depends(get_db)) -> dict[str, Any]:
    st = _status(db, "company_event_exposure")
    sec = db.one("SELECT security_id, issuer_id, name, ticker, country, sector_id FROM sec WHERE security_id = ?",
                 [security_id]) if _has(db, "sec") else None
    if sec is None:
        raise not_found(f"unknown security_id {security_id!r}")
    base = {"security": {k: sec.get(k) for k in ("security_id", "issuer_id", "name", "ticker", "country", "sector_id")},
            "as_of": None, "exposure_score": None, "event_pressure": None, "expected_impact": None, "n_countries": 0,
            "w_source": None, "top_countries": [], "top_shocks": [], "country_escalation": [], "history": [],
            "model_based": True, "data_status": st}
    if not _has(db, "em_exposure"):
        return base | {"data_status": "pending"}
    rows = db.all("SELECT * FROM em_exposure WHERE security_id = ? ORDER BY as_of DESC LIMIT ?", [security_id, history])
    if not rows:
        return base
    r = rows[0]
    cn, _ = _names(db)
    tc = [{"iso2": x["iso2"], "name": cn.get(x["iso2"]), "weight": _f(x["weight"]), "pressure": _f(x["pressure"])}
          for x in (jload(r["top_countries"], []) or [])]
    ts = [{"iso2": x["iso2"], "theme": x["theme"], "attention_z": _f(x.get("attention_z")),
           "expected_car": _f(x.get("expected_car")), "n": int(x.get("n") or 0)} for x in (jload(r["top_shocks"], []) or [])]
    esc = []
    if tc and _has(db, "em_escalation"):
        codes = [x["iso2"] for x in tc]
        esc = db.all(f"""SELECT iso2, theme_group, p_escalation_7d, p_escalation_30d, branching_ratio FROM em_escalation
                         WHERE as_of = (SELECT max(as_of) FROM em_escalation) AND iso2 IN ({', '.join('?' for _ in codes)})
                           AND theme_group IN ('geo', 'social', 'politics', 'all') ORDER BY iso2, theme_group""", codes)
    return base | {"as_of": iso(r["as_of"]), "exposure_score": _f(r["exposure_score"]), "event_pressure": _f(r["event_pressure"]),
                   "expected_impact": _f(r["expected_impact"]), "n_countries": int(r["n_countries"]), "w_source": r["w_source"],
                   "top_countries": tc, "top_shocks": ts,
                   "country_escalation": [{"iso2": x["iso2"], "theme_group": x["theme_group"],
                                           "p_escalation_7d": _f(x["p_escalation_7d"]),
                                           "p_escalation_30d": _f(x["p_escalation_30d"]),
                                           "branching_ratio": _f(x["branching_ratio"])} for x in esc],
                   "history": [{"as_of": iso(h["as_of"]), "exposure_score": _f(h["exposure_score"]),
                                "expected_impact": _f(h["expected_impact"])} for h in reversed(rows)]}
