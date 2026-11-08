"""Macro v2 wave 4 endpoints (tag: macro-v4; owner macro-w4): macro -> markets.

GET  /macro/v4/factors                     factor set (official inputs), latest values, current regime states
GET  /macro/v4/exposures                   rolling macro betas (joint / univariate / surprise) + PIT history
GET  /macro/v4/regime-performance          sector / factor / region returns by regime (mean, hit rate, t, n)
GET  /macro/v4/scenarios                   presets + saved scenarios;  POST creates (app_state macro_scenarios)
GET|PUT|DELETE /macro/v4/scenarios/{id}
POST /macro/v4/scenarios/run               run inline shocks or a saved / preset scenario_id
POST /macro/v4/scenarios/{id}/run          run a saved / preset scenario
GET  /macro/v4/episodes, /macro/v4/episodes/{episode_id}   historical episode replay: model vs realized
GET  /companies/{security_id}/macro        (companies router) field macro_v4 = company_macro_v4(): exposures,
                                           sector regime table, scenario impacts, episode replay
Everything model-based is labelled (model_based / method fields). Data: serving tables from
app.api.serving._Builder.macro_v4 (macro_factors, macro_exposures, macro_exposure_fits, regime_performance,
macro_episode_replay); saved scenarios: app.macro.markets.store (app_state.duckdb).
"""

from __future__ import annotations

import math
import threading
from typing import Any, Literal

import numpy as np
import polars as pl
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import invalid, iso, not_found, taxonomy
from app.macro.markets import scenarios as sc
from app.macro.markets import store

router = APIRouter(tags=["macro-v4"], route_class=FastRoute)

FACTOR_META: dict[str, dict[str, str]] = {
    "d2y": {"label": "2y Treasury yield", "unit": "pp", "per": "return per +100bp", "source": "fred:DGS2 (Fed H.15)"},
    "d10y": {"label": "10y Treasury yield", "unit": "pp", "per": "return per +100bp", "source": "fred:DGS10 (Fed H.15)"},
    "dcurve": {"label": "10y-2y curve", "unit": "pp", "per": "return per +100bp steepening", "source": "fred:T10Y2Y"},
    "dreal10": {"label": "10y real yield (TIPS)", "unit": "pp", "per": "return per +100bp", "source": "fred:DFII10 (Fed H.15)"},
    "dbe10": {"label": "10y breakeven inflation", "unit": "pp", "per": "return per +100bp", "source": "fred:T10YIE"},
    "dcredit": {"label": "Credit spread (Baa - 10y Treasury)", "unit": "pp", "per": "return per +100bp",
                "source": "fred:BAA10Y (Moody's via FRED)"},
    "usd": {"label": "Broad trade-weighted USD", "unit": "frac", "per": "elasticity (return per +1% / 1%)",
            "source": "fred:DTWEXBGS (Federal Reserve)"},
    "oil": {"label": "WTI crude oil", "unit": "frac", "per": "elasticity", "source": "fred:DCOILWTICO (EIA)"},
    "commodities": {"label": "IMF all-commodity index", "unit": "frac", "per": "elasticity", "source": "fred:PALLFNFINDEXM (IMF)"},
    "copper": {"label": "Copper (IMF)", "unit": "frac", "per": "elasticity", "source": "fred:PCOPPUSDM (IMF)"},
    "cash": {"label": "3m T-bill cash return", "unit": "frac", "per": "", "source": "fred:DGS3MO"},
    "equity": {"label": "S&P 500 (market beta)", "unit": "frac", "per": "elasticity", "source": "price lake IDX:^GSPC"},
    "equity_resid": {"label": "Equity shock not explained by macro", "unit": "frac", "per": "elasticity",
                     "source": "S&P 500 orthogonalised on the macro factors"},
    "surp_growth": {"label": "Growth surprise index", "unit": "idx100", "per": "return per +100 index points",
                    "source": "macro_surprise_index (model-based expectations)"},
    "surp_inflation": {"label": "Inflation surprise index", "unit": "idx100", "per": "return per +100 index points",
                       "source": "macro_surprise_index (model-based expectations)"},
    "surp_labor": {"label": "Labor surprise index", "unit": "idx100", "per": "return per +100 index points",
                   "source": "macro_surprise_index (model-based expectations)"},
}
MODEL_FACTORS = {"joint": sc.JOINT_Z, "univariate": ["d2y", "d10y", "dcurve", "dreal10", "dbe10", "dcredit", "usd",
                                                     "oil", "equity"],
                 "surprise": ["surp_growth", "surp_inflation", "surp_labor", "equity_resid"]}
EXPOSURE_METHOD = {
    "label": "Model-based: rolling weekly OLS betas (156 weeks, >= 52), Newey-West HAC(4) standard errors",
    "joint": "r = a + b'f + b_u u, f = (d2y, dcurve, dbe10, dcredit, usd, oil), u = S&P 500 orthogonalised on f "
             "(macro betas are total effects)",
    "univariate": "one regression per factor (total beta); equity = CAPM beta to the S&P 500",
    "surprise": "changes of the W2 surprise indices (growth / inflation / labor, home economy) + equity residual",
    "shrinkage": "securities: empirical Bayes toward the sector median, w = tau2 / (tau2 + se^2); aggregates unshrunk",
    "returns": "local-currency weekly total returns (price lake); portfolios cw = lagged market cap (2010+), ew",
}
REGIME_METHOD = {
    "label": "Historical monthly returns grouped by regime (not a forecast)",
    "excess": "return - 3m T-bill / 12 (fred:DGS3MO)", "relative": "return - benchmark (S&P 500 / regional index)",
    "timing": "contemporaneous = regime of the same month; next_month = regime of the previous month (publication lags "
              "make it only approximately investable)",
    "regimes": {"cycle_quadrant": "W1 cycle clock (US; GLOBAL = mean of US, EA, JP, GB, CN momenta)",
                "hmm_regime": "Gaussian HMM regimes (model-based)",
                "rate_cycle": "hiking / pausing / cutting (macro-w3 cb_rate_cycle)",
                "inflation_regime": "US CPI y/y buckets"},
}
ENTITY_TYPES = Literal["security", "sector", "subsector", "region", "factor", "etf", "index", "future"]
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


# ------------------------------------------------------------------------------------------------ helpers


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _r(v: Any, nd: int = 6) -> float | None:
    x = _f(v)
    return None if x is None else round(x, nd)


def _pl(db: Db, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
    return db.cur.execute(sql, params or []).pl()


def _engine(db: Db) -> dict[str, Any]:
    """Per-snapshot cache: fits, factor history (W/M joint factors), security reference map."""
    key = f"{db.snap.path}:{db.snap.run_id}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit
    fits = _pl(db, "SELECT * FROM macro_exposure_fits")
    fac = _pl(db, """SELECT * FROM macro_factors WHERE frequency IN ('W', 'M')
                     AND factor_id IN ('d2y','dcurve','dbe10','dcredit','usd','oil','equity')""")
    ref = _pl(db, """SELECT security_id, ticker, name, sector_id, subsector_id, region, country, market_cap_usd,
                            u_mvp_us, u_mvp_global, u_full, security_type FROM sec""")
    refd = {r["security_id"]: r for r in ref.iter_rows(named=True)}
    uw = {u: {sid: float(r["market_cap_usd"]) for sid, r in refd.items() if r.get(f"u_{u}") and _f(r.get("market_cap_usd"))}
          for u in ("mvp_us", "mvp_global", "full")}
    eng = {"fits": fits, "fac": fac, "ref": refd, "uweights": uw, "prepared": sc.prepare(fits) if fits.height else None,
           "as_of": iso(fits["as_of_date"].max()) if fits.height else None}
    with _cache_lock:
        _cache.clear()
        _cache[key] = eng
    return eng


_TAX: dict[str, Any] = {}


def _taxonomy() -> dict[str, Any]:
    if not _TAX:
        _TAX.update(taxonomy())
    return _TAX


def entity_name(key: tuple[str, str, str], ref: dict[str, dict[str, Any]]) -> tuple[str | None, str]:
    et, eid, _ = key
    tax = _taxonomy()
    if et in ("security", "etf", "index", "future"):
        r = ref.get(eid) or {}
        return r.get("ticker"), r.get("name") or eid
    if et == "sector":
        if ":" in eid:
            rg, s = eid.split(":", 1)
            return eid, f"{rg} {tax['sectors'].get(s, s)}"
        return eid, tax["sectors"].get(eid, eid)
    if et == "subsector":
        return eid, (tax["subsectors"].get(eid) or {}).get("name", eid)
    if et == "factor":
        return eid, f"{eid.replace('_', ' ').title()} long-short"
    return eid, eid


def factors_out(ids: list[str]) -> list[dict[str, Any]]:
    return [{"factor_id": f, **FACTOR_META.get(f, {"label": f, "unit": "", "per": "", "source": ""})} for f in ids]


def _status(n: int) -> str:
    return "ok" if n else "pending"


# ------------------------------------------------------------------------------------------------ factors


@router.get("/macro/v4/factors", operation_id="getMacroV4Factors")
def get_factors(frequency: Literal["D", "W", "M"] = "W", history: int = Query(104, ge=0, le=2000),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    rows = db.all("""SELECT factor_id, date, value, level, unit, source, model_based FROM macro_factors
                     WHERE frequency = ? AND unit <> 'state' AND factor_id NOT LIKE 'surp_%'
                     QUALIFY row_number() OVER (PARTITION BY factor_id ORDER BY date DESC) <= ?
                     ORDER BY factor_id, date""", [frequency, max(history, 1)])
    by: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by.setdefault(r["factor_id"], []).append(r)
    items = []
    for fid, rs in by.items():
        last = rs[-1]
        items.append({"factor_id": fid, **FACTOR_META.get(fid, {"label": fid, "per": "", "source": last["source"]}),
                      "unit": last["unit"], "source": last["source"], "model_based": bool(last["model_based"]),
                      "last_date": iso(last["date"]), "last_change": _r(last["value"]), "last_level": _r(last["level"]),
                      "history": [{"date": iso(r["date"]), "value": _r(r["value"]), "level": _r(r["level"])}
                                  for r in rs] if history else []})
    surp = db.all("""SELECT factor_id, arg_max(level, date) AS level, max(date) AS date FROM macro_factors
                     WHERE frequency = 'W' AND factor_id LIKE 'surp_%' GROUP BY 1 ORDER BY 1""")
    states = db.all("""SELECT factor_id, arg_max(state, date) AS state, max(date) AS date, any_value(source) AS source,
                              bool_or(model_based) AS model_based
                       FROM macro_factors WHERE frequency = 'M' AND unit = 'state' GROUP BY 1 ORDER BY 1""")
    return {"frequency": frequency, "factors": items,
            "surprise": [{"factor_id": r["factor_id"], "level": _r(r["level"], 3), "date": iso(r["date"])} for r in surp],
            "states": [{"state_id": r["factor_id"], "state": r["state"], "date": iso(r["date"]), "source": r["source"],
                        "model_based": bool(r["model_based"])} for r in states],
            "data_status": _status(len(items))}


# ------------------------------------------------------------------------------------------------ exposures


@router.get("/macro/v4/exposures", operation_id="getMacroV4Exposures")
def get_exposures(entity_type: ENTITY_TYPES = "sector", model: Literal["joint", "univariate", "surprise"] = "joint",
                  weighting: Literal["cw", "ew", "none", "any"] = "any", entity_id: list[str] | None = Query(None),
                  sort_factor: str | None = None, sort: Literal["asc", "desc"] = "desc",
                  universe: Literal["mvp_us", "mvp_global", "full"] | None = None, sector_id: str | None = None,
                  region: str | None = None, limit: int = Query(200, ge=1, le=5000), history: bool = False,
                  db: Db = Depends(get_db)) -> dict[str, Any]:
    eng = _engine(db)
    ref = eng["ref"]
    if sort_factor is not None and sort_factor not in MODEL_FACTORS[model]:
        raise invalid(f"sort_factor must be one of {MODEL_FACTORS[model]}")
    conds, params = ["entity_type = ?", "model = ?"], [entity_type, model]
    if weighting != "any":
        conds.append("weighting = ?")
        params.append(weighting)
    if entity_id:
        conds.append(f"entity_id IN ({', '.join('?' for _ in entity_id)})")
        params += entity_id
    latest = db.scalar(f"SELECT max(as_of_date) FROM macro_exposures WHERE {' AND '.join(conds)}", params)
    total_sec: int | None = None
    if latest and entity_type == "security" and not entity_id:
        # rank in SQL (20k securities): filter by universe / sector / region, order by the sort factor, keep `limit`
        sf0 = sort_factor or MODEL_FACTORS[model][0]
        sconds, sparams = ["m.entity_type = 'security'", "m.model = ?", "m.factor_id = ?", "m.as_of_date = ?"],             [model, sf0, latest]
        if universe:
            sconds.append(f"s.u_{universe}")
        if sector_id:
            sconds.append("s.sector_id = ?")
            sparams.append(sector_id)
        if region:
            sconds.append("s.region = ?")
            sparams.append(region)
        where = " AND ".join(sconds)
        total_sec = int(db.scalar(f"SELECT count(*) FROM macro_exposures m JOIN sec s ON s.security_id = m.entity_id "
                                  f"WHERE {where}", sparams) or 0)
        ids = [r["entity_id"] for r in db.all(
            f"""SELECT m.entity_id FROM macro_exposures m JOIN sec s ON s.security_id = m.entity_id WHERE {where}
                ORDER BY m.beta {'DESC' if sort == 'desc' else 'ASC'} NULLS LAST LIMIT ?""", sparams + [limit])]
        conds.append(f"entity_id IN ({', '.join('?' for _ in ids) or 'NULL'})")
        params += ids
    rows = db.all(f"""SELECT * FROM macro_exposures WHERE {' AND '.join(conds)} AND as_of_date = ?""",
                  params + [latest]) if latest else []
    ents: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["entity_type"], r["entity_id"], r["weighting"])
        if entity_type == "security":
            rf = ref.get(r["entity_id"]) or {}
            if universe and not rf.get(f"u_{universe}"):
                continue
            if sector_id and rf.get("sector_id") != sector_id:
                continue
            if region and rf.get("region") != region:
                continue
        e = ents.get(k)
        if e is None:
            tk, nm = entity_name(k, ref)
            e = ents[k] = {"entity_type": k[0], "entity_id": k[1], "weighting": k[2], "ticker": tk, "name": nm,
                           "r2": _r(r["r2"], 4), "n_obs": int(r["n_obs"]), "scope": r["scope"], "betas": {}}
        e["betas"][r["factor_id"]] = {"beta": _r(r["beta"]), "beta_raw": _r(r["beta_raw"]), "se": _r(r["se"]),
                                      "t_stat": _r(r["t_stat"], 3), "shrink_weight": _r(r["shrink_weight"], 4),
                                      "peer_percentile": _r(r["peer_percentile"], 2)}
    items = list(ents.values())
    sf = sort_factor or MODEL_FACTORS[model][0]
    items.sort(key=lambda e: (e["betas"].get(sf, {}).get("beta") is None,
                              -(e["betas"].get(sf, {}).get("beta") or 0) if sort == "desc"
                              else (e["betas"].get(sf, {}).get("beta") or 0)))
    total = len(items) if total_sec is None else total_sec
    items = items[:limit]
    hist: list[dict[str, Any]] = []
    if history and entity_id:
        hrows = db.all(f"""SELECT entity_id, weighting, factor_id, as_of_date, beta, se FROM macro_exposures
                           WHERE {' AND '.join(conds)} ORDER BY entity_id, weighting, factor_id, as_of_date""", params)
        cur: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in hrows:
            cur.setdefault((r["entity_id"], r["weighting"], r["factor_id"]), []).append(
                {"date": iso(r["as_of_date"]), "beta": _r(r["beta"]), "se": _r(r["se"])})
        hist = [{"entity_id": k[0], "weighting": k[1], "factor_id": k[2], "points": v} for k, v in cur.items()]
    return {"as_of": iso(latest), "frequency": "W", "frequencies_available": ["W"],
            "entity_type": entity_type, "model": model, "factors": factors_out(MODEL_FACTORS[model]),
            "total": total, "items": items, "history": hist, "method": EXPOSURE_METHOD, "model_based": True,
            "data_status": _status(total)}


# ------------------------------------------------------------------------------------------------ regime performance


@router.get("/macro/v4/regime-scopes", operation_id="getMacroV4RegimeScopes")
def get_regime_scopes(db: Db = Depends(get_db)) -> dict[str, Any]:
    """Regime types, their valid scopes (incl. rate-cycle banks), states and current state - for UI selectors."""
    st = db.all("""SELECT factor_id, list(DISTINCT state ORDER BY state) AS states, arg_max(state, date) AS current,
                          max(date) AS last_date, min(date) AS first_date, any_value(source) AS source,
                          bool_or(model_based) AS model_based
                   FROM macro_factors WHERE frequency = 'M' AND unit = 'state' GROUP BY 1 ORDER BY 1""")
    perf = {(r["regime_type"], r["scope"]) for r in db.all(
        "SELECT DISTINCT regime_type, scope FROM regime_performance")}
    types: dict[str, list[dict[str, Any]]] = {t: [] for t in ("cycle_quadrant", "hmm_regime", "rate_cycle",
                                                              "inflation_regime")}
    for r in st:
        for t in types:
            if r["factor_id"].startswith(t + "_"):
                scope = r["factor_id"][len(t) + 1:]
                types[t].append({"scope": scope, "states": list(r["states"] or []), "current_state": r["current"],
                                 "first_date": iso(r["first_date"]), "last_date": iso(r["last_date"]),
                                 "source": r["source"], "model_based": bool(r["model_based"]),
                                 "has_performance": (t, scope) in perf})
    out = [{"regime_type": t, "label": REGIME_METHOD["regimes"][t], "default_scope":
            next((x["scope"] for x in v if x["has_performance"] and x["scope"] in ("US", "fed")), None)
            or next((x["scope"] for x in v if x["has_performance"]), None), "scopes": v} for t, v in types.items()]
    return {"regime_types": out, "data_status": _status(len(st))}



@router.get("/macro/v4/regime-performance", operation_id="getMacroV4RegimePerformance")
def get_regime_performance(regime_type: Literal["cycle_quadrant", "hmm_regime", "rate_cycle", "inflation_regime"] =
                           "cycle_quadrant", scope: str = "US",
                           entity_type: Literal["sector", "subsector", "region", "factor", "etf", "index"] = "sector",
                           weighting: Literal["cw", "ew", "none", "any"] = "cw",
                           timing: Literal["contemporaneous", "next_month"] = "contemporaneous",
                           metric: Literal["excess", "relative"] = "relative", entity_id: list[str] | None = Query(None),
                           include_regional: bool = False, db: Db = Depends(get_db)) -> dict[str, Any]:
    eng = _engine(db)
    if entity_type == "factor":
        weighting, metric = "none", "excess"
    elif entity_type in ("etf", "index"):
        weighting = "none"
    conds = ["regime_type = ?", "scope = ?", "entity_type = ?", "timing = ?", "metric = ?"]
    params: list[Any] = [regime_type, scope, entity_type, timing, metric]
    if weighting != "any":
        conds.append("weighting = ?")
        params.append(weighting)
    if entity_id:
        conds.append(f"entity_id IN ({', '.join('?' for _ in entity_id)})")
        params += entity_id
    elif entity_type == "sector" and not include_regional:
        conds.append("position(':' IN entity_id) = 0")
    rows = db.all(f"SELECT * FROM regime_performance WHERE {' AND '.join(conds)} ORDER BY entity_id, regime", params)
    regimes = sorted({r["regime"] for r in rows})
    current = next((r["regime"] for r in rows if r["is_current"]), None)
    if current is None:
        cur = db.one("""SELECT arg_max(state, date) AS s FROM macro_factors WHERE frequency = 'M' AND unit = 'state'
                        AND factor_id = ?""", [f"{regime_type}_{scope}"])
        current = cur["s"] if cur else None
    ents: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["entity_id"], r["weighting"])
        e = ents.get(k)
        if e is None:
            tk, nm = entity_name((entity_type, r["entity_id"], r["weighting"]), eng["ref"])
            e = ents[k] = {"entity_type": entity_type, "entity_id": r["entity_id"], "weighting": r["weighting"],
                           "ticker": tk, "name": nm, "benchmark": r["benchmark"], "cells": {}}
        e["cells"][r["regime"]] = {"mean": _r(r["mean"]), "median": _r(r["median"]), "ann_mean": _r(r["ann_mean"]),
                                   "vol": _r(r["vol"]), "hit_rate": _r(r["hit_rate"], 4), "t_stat": _r(r["t_stat"], 3),
                                   "n": int(r["n"]), "first_month": iso(r["first_month"]),
                                   "last_month": iso(r["last_month"]), "is_current": bool(r["is_current"])}
    items = list(ents.values())
    items.sort(key=lambda e: -((e["cells"].get(current) or {}).get("mean") or -1e9))
    mb = any(r["model_based"] for r in rows)
    return {"regime_type": regime_type, "scope": scope, "entity_type": entity_type, "weighting": weighting,
            "timing": timing, "metric": metric, "current_regime": current, "regimes": regimes, "items": items,
            "model_based": mb, "method": REGIME_METHOD, "data_status": _status(len(items))}


# ------------------------------------------------------------------------------------------------ scenarios


class ScenarioIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    shocks: dict[str, float]
    options: dict[str, Any] | None = None


class ScenarioPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    shocks: dict[str, float] | None = None
    options: dict[str, Any] | None = None


class ScenarioRun(BaseModel):
    scenario_id: str | None = None
    shocks: dict[str, float] | None = None
    options: dict[str, Any] | None = None
    name: str | None = None
    universe: Literal["mvp_us", "mvp_global", "full"] = "full"
    region: str | None = None
    weighting: Literal["cw", "ew"] = "cw"
    top_n: int = Field(default=20, ge=1, le=200)
    watchlist_id: str | None = None
    security_ids: list[str] | None = Field(default=None, max_length=500)
    sectors: list[str] | None = Field(default=None, max_length=50, description="filter winners / losers (server-side)")
    subsectors: list[str] | None = Field(default=None, max_length=200, description="filter winners / losers")


def _check_options(o: dict[str, Any] | None) -> dict[str, Any]:
    o = dict(o or {})
    extra = set(o) - {"mode", "var_propagation", "var_horizon_months"}
    if extra:
        raise invalid(f"unknown option(s): {sorted(extra)}")
    if o.get("mode", "isolated") not in ("isolated", "conditional"):
        raise invalid("options.mode must be isolated or conditional")
    hz = o.get("var_horizon_months", 3)
    if not isinstance(hz, int) or not 1 <= hz <= 24:
        raise invalid("options.var_horizon_months must be an integer in 1..24")
    return o


def _validate(shocks: dict[str, Any], options: dict[str, Any] | None) -> tuple[dict[str, float], dict[str, Any]]:
    try:
        sh = sc.validate_shocks(shocks)
    except ValueError as e:
        raise invalid(str(e)) from e
    return sh, _check_options(options)


def _scenario_out(s: dict[str, Any]) -> dict[str, Any]:
    return {"scenario_id": s["scenario_id"], "name": s["name"], "description": s.get("description"),
            "shocks": s["shocks"], "options": s.get("options") or {}, "preset": bool(s.get("preset")),
            "created_at": iso(s.get("created_at")), "updated_at": iso(s.get("updated_at"))}


def _find(scenario_id: str) -> dict[str, Any]:
    for p in sc.PRESETS:
        if p["scenario_id"] == scenario_id:
            return p
    s = store.get_scenario(scenario_id)
    if s is None:
        raise not_found(f"unknown scenario_id {scenario_id!r}")
    return s


@router.get("/macro/v4/scenarios", operation_id="listMacroV4Scenarios")
def list_scenarios() -> dict[str, Any]:
    return {"presets": [_scenario_out(p) for p in sc.PRESETS],
            "saved": [_scenario_out(s) for s in store.list_scenarios()],
            "shock_fields": [{"field": k, "label": v} for k, v in sc.SHOCK_FIELDS.items()]}


@router.post("/macro/v4/scenarios", operation_id="createMacroV4Scenario", status_code=201)
def create_scenario(body: ScenarioIn) -> JSONResponse:
    sh, opt = _validate(body.shocks, body.options)
    s = store.create_scenario(body.name, body.description, sh, opt)
    return JSONResponse(_scenario_out(s), status_code=201)


@router.get("/macro/v4/scenarios/{scenario_id}", operation_id="getMacroV4Scenario")
def get_scenario(scenario_id: str) -> dict[str, Any]:
    return _scenario_out(_find(scenario_id))


@router.put("/macro/v4/scenarios/{scenario_id}", operation_id="updateMacroV4Scenario")
def update_scenario(scenario_id: str, body: ScenarioPatch) -> dict[str, Any]:
    if scenario_id.startswith("preset_"):
        raise invalid("preset scenarios are read-only")
    fields = body.model_dump(exclude_unset=True)
    if "shocks" in fields or "options" in fields:
        cur = _find(scenario_id)
        sh, opt = _validate(fields.get("shocks") or cur["shocks"], fields.get("options", cur.get("options")))
        fields["shocks"], fields["options"] = sh, opt
    s = store.update_scenario(scenario_id, fields)
    if s is None:
        raise not_found(f"unknown scenario_id {scenario_id!r}")
    return _scenario_out(s)


@router.delete("/macro/v4/scenarios/{scenario_id}", operation_id="deleteMacroV4Scenario", status_code=204)
def delete_scenario(scenario_id: str) -> JSONResponse:
    if scenario_id.startswith("preset_"):
        raise invalid("preset scenarios are read-only")
    if not store.delete_scenario(scenario_id):
        raise not_found(f"unknown scenario_id {scenario_id!r}")
    return JSONResponse(None, status_code=204)


def _impact_row(r: dict[str, Any], ref: dict[str, dict[str, Any]]) -> dict[str, Any]:
    k = (r["entity_type"], r["entity_id"], r["weighting"])
    tk, nm = entity_name(k, ref)
    rf = ref.get(r["entity_id"]) or {}
    return {"entity_type": k[0], "entity_id": k[1], "weighting": k[2], "ticker": tk, "name": nm,
            "sector_id": rf.get("sector_id"), "region": rf.get("region"),
            "impact": _r(r["impact"]), "impact_lo": _r(r["impact_lo"]), "impact_hi": _r(r["impact_hi"]),
            "first_round": _r(r["first_round"]), "surprise_part": _r(r["surprise_part"])}


def _agg(frame: pl.DataFrame, w: dict[str, float]) -> dict[str, Any] | None:
    f = frame.filter(pl.col("entity_id").is_in(list(w)) & pl.col("impact").is_not_null())
    if f.is_empty():
        return None
    ww = np.array([w[e] for e in f["entity_id"].to_list()], dtype=float)
    if ww.sum() <= 0:
        return None
    ww = ww / ww.sum()
    return {"impact": _r(float(ww @ f["impact"].to_numpy())), "impact_lo": _r(float(ww @ f["impact_lo"].to_numpy())),
            "impact_hi": _r(float(ww @ f["impact_hi"].to_numpy())), "n": f.height}


def run_payload(db: Db, shocks: dict[str, Any], options: dict[str, Any] | None, body: ScenarioRun,
                meta: dict[str, Any]) -> dict[str, Any]:
    eng = _engine(db)
    fits, ref = eng["fits"], eng["ref"]
    sh, opt = _validate(shocks, options)
    if fits.is_empty():
        return {"scenario": meta | {"shocks": sh, "options": opt}, "as_of": None, "model_label": sc.MODEL_LABEL,
                "model_based": True, "factor_vector": {"first_round": [], "total": []}, "var": None, "notes": [],
                "band": "", "universe": [], "sectors": [], "regions": [], "factors": [], "instruments_top": [],
                "instruments_bottom": [], "winners": [], "losers": [], "n_securities": 0, "watchlist": None,
                "securities": [], "data_status": "pending"}
    try:
        res = sc.run_scenario(fits, sh, opt, eng["fac"], prepared=eng["prepared"])
    except ValueError as e:
        raise invalid(str(e)) from e
    fr = res["frame"].with_columns(pl.col(pl.Float64).fill_nan(None))
    rs, g = res["resolved"], res["gamma"]
    sec = fr.filter(pl.col("entity_type") == "security")
    secset, subset = set(body.sectors or []), set(body.subsectors or [])
    in_u = {s: r for s, r in ref.items() if r.get(f"u_{body.universe}") and (not body.region or r.get("region") == body.region)
            and (not secset or r.get("sector_id") in secset) and (not subset or r.get("subsector_id") in subset)}
    secu = sec.filter(pl.col("entity_id").is_in(list(in_u)) & pl.col("impact").is_not_null()).sort("impact")
    rows = lambda df: [_impact_row(r, ref) for r in df.iter_rows(named=True)]  # noqa: E731
    universes = []
    for u in ("mvp_us", "mvp_global", "full"):
        a = _agg(sec, eng["uweights"][u])
        if a:
            universes.append({"universe_id": u, "weighting": "cw", **a})
    wl = None
    if body.watchlist_id:
        from app.api import app_state  # noqa: PLC0415

        w = app_state.get_watchlist(body.watchlist_id)
        if w is None:
            raise not_found(f"unknown watchlist_id {body.watchlist_id!r}")
        ids = [i["security_id"] for i in app_state.watchlist_items(body.watchlist_id)]
        m = sec.filter(pl.col("entity_id").is_in(ids))
        wl = {"watchlist_id": body.watchlist_id, "name": w["name"], "weighting": "ew",
              **(_agg(m, dict.fromkeys(ids, 1.0)) or {"impact": None, "impact_lo": None, "impact_hi": None, "n": 0}),
              "items": rows(m.sort("impact"))}
    sel = rows(sec.filter(pl.col("entity_id").is_in(body.security_ids))) if body.security_ids else []
    sectors = fr.filter((pl.col("entity_type") == "sector") & (pl.col("weighting") == body.weighting)
                        & ~pl.col("entity_id").str.contains(":")).sort("impact", descending=True)
    regions = fr.filter((pl.col("entity_type") == "region") & (pl.col("weighting") == body.weighting)).sort(
        "impact", descending=True)
    facs = fr.filter(pl.col("entity_type") == "factor").sort("impact", descending=True)
    inst = fr.filter(pl.col("entity_type").is_in(["etf", "index"])).sort("impact", descending=True)
    n = body.top_n
    return {"scenario": meta | {"shocks": sh, "options": opt | {k: v for k, v in rs["options"].items() if k not in opt}},
            "as_of": eng["as_of"], "model_label": sc.MODEL_LABEL, "model_based": True,
            "factor_vector": {"first_round": sc.factor_vector_out(rs["z"], g),
                              "total": sc.factor_vector_out(rs["z_total"], g)},
            "var": rs["var"], "notes": rs["notes"],
            "band": "90% band from the beta HAC covariance (estimation uncertainty only); aggregates of securities "
                    "average member bands",
            "universe": universes, "sectors": rows(sectors), "regions": rows(regions), "factors": rows(facs),
            "instruments_top": rows(inst.head(n)), "instruments_bottom": rows(inst.tail(n).reverse()),
            "winners": rows(secu.tail(n).reverse()), "losers": rows(secu.head(n)), "n_securities": secu.height,
            "watchlist": wl, "securities": sel, "data_status": "ok"}


@router.post("/macro/v4/scenarios/run", operation_id="runMacroV4Scenario")
def run_scenario(body: ScenarioRun, db: Db = Depends(get_db)) -> dict[str, Any]:
    if body.scenario_id:
        s = _find(body.scenario_id)
        meta = {"scenario_id": s["scenario_id"], "name": s["name"], "preset": bool(s.get("preset"))}
        return run_payload(db, body.shocks or s["shocks"], body.options if body.options is not None else s.get("options"),
                           body, meta)
    if not body.shocks:
        raise invalid("provide scenario_id or shocks")
    return run_payload(db, body.shocks, body.options, body, {"scenario_id": None, "name": body.name or "Custom scenario",
                                                             "preset": False})


@router.post("/macro/v4/scenarios/{scenario_id}/run", operation_id="runMacroV4SavedScenario")
def run_saved_scenario(scenario_id: str, body: ScenarioRun | None = None, db: Db = Depends(get_db)) -> dict[str, Any]:
    b = body or ScenarioRun()
    b.scenario_id = scenario_id
    return run_scenario(b, db)


# ------------------------------------------------------------------------------------------------ episodes


def _episode_moves(db: Db, episode_id: str) -> dict[str, float | None]:
    rows = db.all("""SELECT entity_id, realized FROM macro_episode_replay WHERE episode_id = ? AND entity_type =
                     'macro_factor'""", [episode_id])
    return {r["entity_id"]: _r(r["realized"]) for r in rows}


def _fit_stats(model: np.ndarray, real: np.ndarray) -> dict[str, Any]:
    ok = np.isfinite(model) & np.isfinite(real)
    n = int(ok.sum())
    if n < 3:
        return {"n": n, "corr": None, "mae": None, "sign_hit_rate": None, "mean_model": None, "mean_realized": None}
    m, r = model[ok], real[ok]
    c = np.corrcoef(m, r)[0, 1] if np.std(m) > 0 and np.std(r) > 0 else np.nan
    return {"n": n, "corr": _r(c, 4), "mae": _r(np.mean(np.abs(m - r)), 5),
            "sign_hit_rate": _r(np.mean(np.sign(m) == np.sign(r)), 4), "mean_model": _r(np.mean(m), 5),
            "mean_realized": _r(np.mean(r), 5)}


@router.get("/macro/v4/episodes", operation_id="listMacroV4Episodes")
def list_episodes(db: Db = Depends(get_db)) -> dict[str, Any]:
    out = []
    for e in sc.episodes_out():
        mv = _episode_moves(db, e["episode_id"])
        cov = db.one("""SELECT count(*) FILTER (realized IS NOT NULL) AS n_real, count(*) AS n FROM macro_episode_replay
                        WHERE episode_id = ? AND entity_type <> 'macro_factor'""", [e["episode_id"]]) or {}
        out.append(e | {"factor_moves": mv, "missing_factors": [k for k, v in mv.items() if v is None],
                        "n_entities": int(cov.get("n") or 0), "n_realized": int(cov.get("n_real") or 0)})
    return {"episodes": out, "model_label": sc.MODEL_LABEL,
            "method": "factor moves = official level changes over the window (pp; log for USD / oil / equity), applied "
                      "to CURRENT joint betas; realized = price-lake total return over the window",
            "data_status": _status(sum(e["n_entities"] for e in out))}


@router.get("/macro/v4/episodes/{episode_id}", operation_id="getMacroV4Episode")
def get_episode(episode_id: str, universe: Literal["mvp_us", "mvp_global", "full"] = "full",
                weighting: Literal["cw", "ew"] = "cw", top_n: int = Query(20, ge=1, le=200),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    ep = sc.EPISODE_BY_ID.get(episode_id)
    if ep is None:
        raise not_found(f"unknown episode_id {episode_id!r}")
    eng = _engine(db)
    ref = eng["ref"]
    df = _pl(db, "SELECT * FROM macro_episode_replay WHERE episode_id = ? AND entity_type <> 'macro_factor'",
             [episode_id]).with_columns(pl.col(pl.Float64).fill_nan(None))

    def rows(d: pl.DataFrame) -> list[dict[str, Any]]:
        out = []
        for r in d.iter_rows(named=True):
            k = (r["entity_type"], r["entity_id"], r["weighting"])
            tk, nm = entity_name(k, ref)
            out.append({"entity_type": k[0], "entity_id": k[1], "weighting": k[2], "ticker": tk, "name": nm,
                        "model_implied": _r(r["model_implied"]), "model_implied_macro": _r(r["model_implied_macro"]),
                        "model_lo": _r(r["model_lo"]), "model_hi": _r(r["model_hi"]), "realized": _r(r["realized"])})
        return out

    def block(d: pl.DataFrame) -> dict[str, Any]:
        return {"stats": _fit_stats(d["model_implied"].fill_null(np.nan).to_numpy(),
                                    d["realized"].fill_null(np.nan).to_numpy()) if d.height else _fit_stats(
                                        np.array([]), np.array([])),
                "items": rows(d.sort("model_implied", descending=True, nulls_last=True))}

    sectors = df.filter((pl.col("entity_type") == "sector") & (pl.col("weighting") == weighting)
                        & ~pl.col("entity_id").str.contains(":"))
    regions = df.filter((pl.col("entity_type") == "region") & (pl.col("weighting") == weighting))
    facs = df.filter(pl.col("entity_type") == "factor")
    inst = df.filter(pl.col("entity_type").is_in(["etf", "index"]))
    sec = df.filter((pl.col("entity_type") == "security")
                    & pl.col("entity_id").is_in([s for s, r in ref.items() if r.get(f"u_{universe}")]))
    sec_sorted = sec.filter(pl.col("model_implied").is_not_null()).sort("model_implied")
    mv = _episode_moves(db, episode_id)
    return {"episode": {"episode_id": ep.episode_id, "name": ep.name, "start": ep.start.isoformat(),
                        "end": ep.end.isoformat(), "rationale": ep.rationale},
            "factor_moves": mv, "missing_factors": [k for k, v in mv.items() if v is None],
            "as_of_betas": eng["as_of"], "model_label": sc.MODEL_LABEL, "model_based": True,
            "sectors": block(sectors), "regions": block(regions), "factors": block(facs), "instruments": block(inst),
            "securities": {"stats": _fit_stats(sec["model_implied"].fill_null(np.nan).to_numpy(),
                                               sec["realized"].fill_null(np.nan).to_numpy()),
                           "winners": rows(sec_sorted.tail(top_n).reverse()), "losers": rows(sec_sorted.head(top_n))},
            "data_status": _status(df.height)}


# ------------------------------------------------------------------------------------------------ company macro tab


def company_macro_v4(db: Db, security_id: str) -> dict[str, Any] | None:
    """CompanyMacro.macro_v4 payload (GET /companies/{security_id}/macro, app.api.routers.companies); None when the
    security is unknown."""
    eng = _engine(db)
    r = db.one("SELECT security_id, ticker, name, sector_id, subsector_id, region, country FROM sec WHERE security_id = ?",
               [security_id])
    if r is None:
        return None
    rows = db.all("""SELECT * FROM macro_exposures WHERE entity_type = 'security' AND entity_id = ?
                     QUALIFY as_of_date = max(as_of_date) OVER ()""", [security_id])
    srows = db.all("""SELECT * FROM macro_exposures WHERE entity_type = 'sector' AND entity_id = ? AND weighting = 'cw'
                      QUALIFY as_of_date = max(as_of_date) OVER ()""", [r["sector_id"] or ""])
    srows_by = {(x["model"], x["factor_id"]): x for x in srows}

    def expo(model: str) -> list[dict[str, Any]]:
        out = []
        for f in MODEL_FACTORS[model]:
            x = next((y for y in rows if y["model"] == model and y["factor_id"] == f), None)
            if x is None:
                continue
            s = srows_by.get((model, f))
            out.append({"factor_id": f, **{k: v for k, v in FACTOR_META.get(f, {}).items() if k in ("label", "unit", "per")},
                        "beta": _r(x["beta"]), "beta_raw": _r(x["beta_raw"]), "se": _r(x["se"]),
                        "t_stat": _r(x["t_stat"], 3), "shrink_weight": _r(x["shrink_weight"], 4),
                        "peer_percentile": _r(x["peer_percentile"], 2), "sector_beta": _r(s["beta"]) if s else None})
        return out

    meta_m = {m: next(({"r2": _r(y["r2"], 4), "n_obs": int(y["n_obs"]), "scope": y["scope"]}
                       for y in rows if y["model"] == m), None) for m in MODEL_FACTORS}
    regimes = []
    if r["sector_id"]:
        for rt, scope in (("cycle_quadrant", "US"), ("cycle_quadrant", "GLOBAL"), ("hmm_regime", "US"),
                          ("rate_cycle", "fed"), ("inflation_regime", "US")):
            cells = db.all("""SELECT regime, mean, hit_rate, t_stat, n, is_current FROM regime_performance
                              WHERE regime_type = ? AND scope = ? AND entity_type = 'sector' AND entity_id = ?
                                AND weighting = 'cw' AND timing = 'contemporaneous' AND metric = 'relative'
                              ORDER BY regime""", [rt, scope, r["sector_id"]])
            if cells:
                regimes.append({"regime_type": rt, "scope": scope,
                                "current_regime": next((c["regime"] for c in cells if c["is_current"]), None),
                                "cells": [{"regime": c["regime"], "mean": _r(c["mean"]), "hit_rate": _r(c["hit_rate"], 4),
                                           "t_stat": _r(c["t_stat"], 3), "n": int(c["n"]),
                                           "is_current": bool(c["is_current"])} for c in cells]})
    fits = eng["fits"]
    scen: list[dict[str, Any]] = []
    mine = fits.filter(((pl.col("entity_type") == "security") & (pl.col("entity_id") == security_id))
                       | ((pl.col("entity_type") == "sector") & (pl.col("entity_id") == (r["sector_id"] or ""))
                          & (pl.col("weighting") == "cw")) | (pl.col("model") == "gamma"))
    if mine.filter(pl.col("entity_type") == "security").height:
        try:
            saved = store.list_scenarios()
        except Exception:  # noqa: BLE001 - app_state locked: presets only
            saved = []
        for s in sc.PRESETS + saved:
            try:
                res = sc.run_scenario(mine, s["shocks"], (s.get("options") or {}) | {"var_propagation": False,
                                                                                    "mode": "isolated"}, None)
            except ValueError:
                continue
            fr = res["frame"]
            me = fr.filter(pl.col("entity_type") == "security").row(0, named=True)
            sct = fr.filter(pl.col("entity_type") == "sector")
            scen.append({"scenario_id": s["scenario_id"], "name": s["name"], "preset": bool(s.get("preset")),
                         "impact": _r(me["impact"]), "impact_lo": _r(me["impact_lo"]), "impact_hi": _r(me["impact_hi"]),
                         "sector_impact": _r(sct["impact"][0]) if sct.height else None})
    eps = db.all("""SELECT episode_id, model_implied, model_lo, model_hi, realized FROM macro_episode_replay
                    WHERE entity_type = 'security' AND entity_id = ?""", [security_id])
    sect = {e["episode_id"]: e for e in db.all(       # cap-weighted sector portfolio, else equal-weighted
        """SELECT episode_id, weighting, model_implied, model_lo, model_hi, realized FROM macro_episode_replay
           WHERE entity_type = 'sector' AND entity_id = ? AND model_implied IS NOT NULL
           QUALIFY row_number() OVER (PARTITION BY episode_id ORDER BY weighting = 'cw' DESC) = 1""",
        [r["sector_id"] or ""])}
    names = {e.episode_id: e for e in sc.EPISODES}
    episodes = [{"episode_id": e["episode_id"], "name": names[e["episode_id"]].name,
                 "start": names[e["episode_id"]].start.isoformat(), "end": names[e["episode_id"]].end.isoformat(),
                 "model_implied": _r(e["model_implied"]), "model_lo": _r(e["model_lo"]), "model_hi": _r(e["model_hi"]),
                 "realized": _r(e["realized"]),
                 "sector": ({"entity_id": r["sector_id"], "weighting": sect[e["episode_id"]]["weighting"],
                             "model_implied": _r(sect[e["episode_id"]]["model_implied"]),
                             "model_lo": _r(sect[e["episode_id"]]["model_lo"]),
                             "model_hi": _r(sect[e["episode_id"]]["model_hi"]),
                             "realized": _r(sect[e["episode_id"]]["realized"])}
                            if e["episode_id"] in sect else None)} for e in eps if e["episode_id"] in names]
    episodes.sort(key=lambda e: e["start"])
    return {"security_id": security_id, "ticker": r["ticker"], "name": r["name"], "sector_id": r["sector_id"],
            "region": r["region"], "as_of": iso(rows[0]["as_of_date"]) if rows else None,
            "exposures": {"joint": expo("joint"), "univariate": expo("univariate"), "surprise": expo("surprise")},
            "fit": meta_m, "sector_regimes": regimes, "scenarios": scen, "episodes": episodes,
            "model_label": sc.MODEL_LABEL, "model_based": True, "method": EXPOSURE_METHOD,
            "data_status": "ok" if rows else "pending"}
