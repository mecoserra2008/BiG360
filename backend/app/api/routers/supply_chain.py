"""Supply-chain & revenue-geography map (roadmap v2 item #9; contract tag supply-chain; docs/SUPPLY_CHAIN.md).

Serving tables (app.api.serving._Builder.supply_chain): sc_edges (disclosed supplier -> customer edges with
provenance), sc_metrics (centrality / concentration / geography / exposure-weighted country risk per issuer),
sc_exposure (direct + second-order + look-through country / sector exposure), sc_country_risk (model-based country risk
overlay: events, escalation, recession probability, surprises, GPR) and sc_neighbours (STATISTICAL return-correlation
neighbours, not disclosed).

Labels: disclosed edges come from filings (10-K / 20-F / 40-F text rules, XBRL MajorCustomers); statistical
neighbours are return co-movement; country risk, look-through exposure and shock propagation are model-based.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Literal

import duckdb
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from app.api.deps import Db, get_db
from app.api.routers._common import invalid, iso, not_found, sec_ref, sec_ref_sql
from app.core.logsetup import get_logger
from app.supply_chain import contagion as C

router = APIRouter(tags=["supply-chain"])
log = get_logger("app.api.supply_chain")

LABELS = {
    "disclosed": "Disclosed: customer / supplier relationships stated in annual reports (10-K / 20-F / 40-F text rules, "
                 "XBRL major-customer facts); provenance = filing, rule and sentence snippet",
    "statistical": "Statistical: economic neighbours from weekly return co-movement (correlation / partial correlation "
                   "controlling for the market), NOT disclosed relationships",
    "model_based": "Model-based: look-through exposure, country risk score and shock propagation are our estimates",
    "country_risk": "Model-based country risk 0-100: percentile mix of event pressure (0.30), Hawkes escalation "
                    "probability (0.20), recession probability (0.30), negative macro surprises (0.10) and our GPR (0.10)",
    "look_through": "Look-through = (1 - disclosed customer share) x own revenue geography + sum over customers of "
                    "share x customer geography",
    "shock": C.MODEL_LABEL,
}
COVERAGE_NOTE = {
    "full": "Geographic revenue and disclosed supply-chain links available",
    "partial": "Only one of geographic revenue / disclosed links available",
    "sparse": "No disclosed links or geographic revenue: home country and statistical neighbours only",
}
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {}


def _f(v: Any, nd: int = 6) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if x == x and abs(x) != float("inf") else None


def _rows(db: Db, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Query that tolerates a snapshot built before the supply-chain tables existed (-> [])."""
    try:
        return db.all(sql, params or [])
    except duckdb.CatalogException:
        return []


def _ready(db: Db) -> bool:
    return bool(_rows(db, "SELECT 1 FROM sc_metrics LIMIT 1"))


def _sec_of(db: Db, security_id: str) -> dict[str, Any]:
    r = db.one(f"SELECT {sec_ref_sql('s')} FROM sec s WHERE s.security_id = ?", [security_id])
    if not r:
        raise not_found(f"unknown security_id {security_id!r}")
    return r


def _node_info(db: Db, issuer_ids: list[str]) -> dict[str, dict[str, Any]]:
    """issuer -> {security_id, ticker, name, country, sector_id, region, score, metrics...}."""
    ids = sorted({i for i in issuer_ids if i})
    if not ids:
        return {}
    ph = ",".join("?" * len(ids))
    sec = db.all(f"""SELECT issuer_id, security_id, ticker, name, country, sector_id, region, market_cap_usd FROM sec
                     WHERE issuer_id IN ({ph})
                     QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY market_cap_usd DESC NULLS LAST,
                                                security_id) = 1""", ids)
    out = {r["issuer_id"]: dict(r) for r in sec}
    for r in _rows(db, f"""SELECT issuer_id, security_id, event_pressure, country_risk, coverage_level, pagerank,
                                  top_customer_share, n_customers, n_suppliers FROM sc_metrics
                           WHERE issuer_id IN ({ph})""", ids):
        o = out.setdefault(r["issuer_id"], {"issuer_id": r["issuer_id"]})
        o.update({k: v for k, v in r.items() if k != "security_id"})
        o.setdefault("security_id", r["security_id"])
    sids = [o["security_id"] for o in out.values() if o.get("security_id")]
    if sids:
        sp = ",".join("?" * len(sids))
        sc = {r["security_id"]: r["score"] for r in _rows(db, f"""SELECT security_id, score FROM composite_latest
                                                                  WHERE profile_id = 'balanced' AND security_id IN ({sp})""", sids)}
        for o in out.values():
            o["score"] = sc.get(o.get("security_id"))
    return out


def _provenance(e: dict[str, Any]) -> dict[str, Any]:
    return {"filing_id": e.get("filing_id"), "form_type": e.get("form_type"), "snippet": e.get("snippet"),
            "knowledge_date": iso(e.get("knowledge_date")), "period_end": iso(e.get("period_end")),
            "first_knowledge_date": iso(e.get("first_knowledge_date")), "rules": list(e.get("rules") or []),
            "sources": list(e.get("sources") or []), "n_disclosures": int(e.get("n_disclosures") or 0)}


def _metrics_out(m: dict[str, Any] | None) -> dict[str, Any] | None:
    if not m:
        return None
    keys = ("n_customers", "n_customers_resolved", "n_suppliers", "n_suppliers_resolved", "n_unweighted",
            "n_neighbours_stat", "n_countries")
    out = {k: int(m.get(k) or 0) for k in keys}
    for k in ("disclosed_customer_share", "top_customer_share", "customer_hhi", "pagerank", "betweenness",
              "degree_centrality", "geo_hhi", "top_country_share", "domestic_share", "country_risk",
              "country_risk_look_through", "event_pressure", "recession_prob", "geo_confidence"):
        out[k] = _f(m.get(k))
    for k in ("top_customer_key", "top_customer_name", "top_country", "geo_source", "geo_method", "geo_source_id",
              "coverage_level"):
        out[k] = m.get(k)
    out["geo_period_end"] = iso(m.get("geo_period_end"))
    out["geo_knowledge_date"] = iso(m.get("geo_knowledge_date"))
    return out


def _coverage(m: dict[str, Any] | None, n_stat: int = 0) -> dict[str, Any]:
    lvl = (m or {}).get("coverage_level") or "sparse"
    return {"level": lvl, "has_disclosed_links": bool(m and (m.get("n_customers") or m.get("n_suppliers"))),
            "has_geo_revenue": bool(m and m.get("geo_source") == "geo_revenue"),
            "n_neighbours_stat": int((m or {}).get("n_neighbours_stat") or n_stat),
            "geo_method": (m or {}).get("geo_method"), "note": COVERAGE_NOTE[lvl]}


# ================================================================================================ network
@router.get("/companies/{security_id}/network", operation_id="getCompanyNetwork")
def company_network(security_id: str, depth: int = Query(1, ge=1, le=2),
                    include_statistical: bool = True,
                    statistical_method: Literal["return_corr", "partial_corr"] = "return_corr",
                    min_confidence: float = Query(0.0, ge=0.0, le=1.0),
                    max_nodes: int = Query(120, ge=10, le=400), db: Db = Depends(get_db)) -> dict[str, Any]:
    r = _sec_of(db, security_id)
    ref = sec_ref(r)
    center = r["ref_issuer_id"]
    if not center:
        raise not_found(f"security {security_id!r} has no issuer")
    m = (_rows(db, "SELECT * FROM sc_metrics WHERE issuer_id = ?", [center]) or [None])[0]
    edges: dict[tuple[str, str], dict[str, Any]] = {}
    depth_of: dict[str, int] = {center: 0}
    frontier = [center]
    for d in (1, 2)[:depth]:
        if not frontier:
            break
        ph = ",".join("?" * len(frontier))
        rows = _rows(db, f"""SELECT * FROM sc_edges WHERE (supplier_key IN ({ph}) OR customer_key IN ({ph}))
                             AND confidence >= ? ORDER BY confidence DESC, coalesce(share_of_supplier_revenue,
                             share_of_customer_purchases, 0) DESC""", frontier + frontier + [min_confidence])
        nxt = []
        for e in rows:
            k = (e["supplier_key"], e["customer_key"])
            if k in edges:
                continue
            if d == 2 and len(depth_of) >= max_nodes:
                break
            edges[k] = e
            for nd in k:
                if nd not in depth_of:
                    depth_of[nd] = d
                    if not nd.startswith(("name:", "cik:")):
                        nxt.append(nd)
        frontier = nxt
    stat: list[dict[str, Any]] = []
    if include_statistical:
        stat = _rows(db, """SELECT neighbour_issuer_id, rho, rank, n_obs, method, same_sector FROM sc_neighbours
                            WHERE issuer_id = ? AND method = ? ORDER BY rank LIMIT 10""", [center, statistical_method])
        for s in stat:
            depth_of.setdefault(s["neighbour_issuer_id"], 1)
    info = _node_info(db, [k for k in depth_of if not k.startswith(("name:", "cik:"))])
    roles: dict[str, set[str]] = {}
    for (a, b) in edges:
        if b == center:
            roles.setdefault(a, set()).add("supplier")
        if a == center:
            roles.setdefault(b, set()).add("customer")
    for s in stat:
        roles.setdefault(s["neighbour_issuer_id"], set()).add("neighbour")
    names_disc = {}
    for e in edges.values():
        names_disc.setdefault(e["supplier_key"], e.get("supplier_name"))
        names_disc.setdefault(e["customer_key"], e.get("customer_name"))
    nodes = []
    for k, d in sorted(depth_of.items(), key=lambda kv: (kv[1], kv[0])):
        o = info.get(k, {})
        rl = roles.get(k, set())
        role = "center" if k == center else (next(iter(rl)) if len(rl) == 1 else "mixed" if rl else "indirect")
        unresolved = k.startswith(("name:", "cik:"))
        nodes.append({"key": k, "issuer_id": None if unresolved else k, "security_id": o.get("security_id"),
                      "ticker": o.get("ticker"), "name": o.get("name") or names_disc.get(k) or k.split(":", 1)[-1],
                      "country": o.get("country"), "sector_id": o.get("sector_id"), "region": o.get("region"),
                      "kind": "unresolved" if unresolved else "issuer", "depth": d, "role": role,
                      "score": _f(o.get("score"), 2), "event_pressure": _f(o.get("event_pressure"), 4),
                      "country_risk": _f(o.get("country_risk"), 2), "coverage_level": o.get("coverage_level"),
                      "market_cap_usd": _f(o.get("market_cap_usd"), 0)})
    eout = [{"source": e["supplier_key"], "target": e["customer_key"], "type": e["edge_type"],
             "share_of_supplier_revenue": _f(e.get("share_of_supplier_revenue"), 4),
             "share_of_customer_purchases": _f(e.get("share_of_customer_purchases"), 4), "rho": None,
             "confidence": _f(e.get("confidence"), 3) or 0.0, "stale": bool(e.get("stale")),
             "provenance": _provenance(e)} for e in edges.values()]
    for s in stat:
        eout.append({"source": center, "target": s["neighbour_issuer_id"], "type": "statistical",
                     "share_of_supplier_revenue": None, "share_of_customer_purchases": None, "rho": _f(s["rho"], 4),
                     "confidence": _f(abs(s["rho"]), 3) or 0.0, "stale": False,
                     "provenance": {"filing_id": None, "form_type": None,
                                    "snippet": f"{s['method']} of weekly returns over {s['n_obs']} weeks (rank {s['rank']})",
                                    "knowledge_date": None, "period_end": None, "first_knowledge_date": None,
                                    "rules": [s["method"]], "sources": ["prices"], "n_disclosures": 0}})
    ready = _ready(db)
    return {"security": ref, "as_of": iso(db.as_of), "center_key": center, "depth": depth, "nodes": nodes,
            "edges": eout, "metrics": _metrics_out(m), "coverage": _coverage(m, len(stat)), "labels": LABELS,
            "data_status": "ok" if ready else "pending"}


# ================================================================================================ geography
def _risk_map(db: Db) -> dict[str, dict[str, Any]]:
    return {r["iso2"]: r for r in _rows(db, "SELECT * FROM sc_country_risk")}


def _overlay(rk: dict[str, Any] | None) -> dict[str, Any] | None:
    if not rk:
        return None
    return {"risk_score": _f(rk.get("risk_score"), 2), "event_pressure": _f(rk.get("event_pressure"), 4),
            "p_escalation_30d": _f(rk.get("p_escalation_30d"), 4), "hotspot": rk.get("hotspot"),
            "emerging_class": rk.get("emerging_class"), "gpr": _f(rk.get("gpr"), 2),
            "recession_prob": _f(rk.get("recession_prob"), 4), "recession_method": rk.get("recession_method"),
            "recession_official": rk.get("recession_official"), "surprise_index": _f(rk.get("surprise_index"), 2),
            "cycle_quadrant": rk.get("cycle_quadrant"), "n_components": int(rk.get("n_components") or 0)}


def _via(v: Any) -> list[dict[str, Any]]:
    try:
        xs = json.loads(v) if isinstance(v, str) else (v or [])
    except ValueError:
        return []
    return [{"key": x.get("key"), "name": x.get("name"), "share": _f(x.get("share"), 4), "weight": _f(x.get("weight"), 4)}
            for x in xs]


@router.get("/companies/{security_id}/geography", operation_id="getCompanyGeography")
def company_geography(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = _sec_of(db, security_id)
    ref = sec_ref(r)
    iid = r["ref_issuer_id"]
    m = (_rows(db, "SELECT * FROM sc_metrics WHERE issuer_id = ?", [iid]) or [None])[0]
    rows = _rows(db, "SELECT * FROM sc_exposure WHERE issuer_id = ? ORDER BY dim, look_through DESC", [iid])
    rk = _risk_map(db)
    countries, sectors = [], []
    for x in rows:
        o = {"direct": _f(x["direct"], 5), "second_order": _f(x["second_order"], 5), "look_through": _f(x["look_through"], 5),
             "n_paths": int(x["n_paths"] or 0), "via": _via(x["via"]), "source": x["source"]}
        if x["dim"] == "country":
            countries.append({"iso2": x["key"], **o, "overlay": _overlay(rk.get(x["key"]))})
        else:
            sectors.append({"sector_id": x["key"], **o})
    if not countries and ref.get("country"):
        countries = [{"iso2": ref["country"], "direct": 1.0, "second_order": None, "look_through": 1.0, "n_paths": 0,
                      "via": [], "source": "home_country", "overlay": _overlay(rk.get(ref["country"]))}]
    countries.sort(key=lambda c: -(c["look_through"] or c["direct"] or 0))
    return {"security": ref, "as_of": iso(db.as_of), "countries": countries, "sectors": sectors,
            "metrics": _metrics_out(m), "coverage": _coverage(m), "labels": LABELS,
            "data_status": "ok" if rows else "pending" if not _ready(db) else "partial"}


@router.get("/supply-chain/country-risk", operation_id="getSupplyChainCountryRisk")
def country_risk(db: Db = Depends(get_db)) -> dict[str, Any]:
    rk = _risk_map(db)
    out = [{"iso2": k, **(_overlay(v) or {})} for k, v in sorted(rk.items())]
    as_of = next(iter(rk.values()))["as_of"] if rk else None
    return {"as_of": iso(as_of), "countries": out, "label": LABELS["country_risk"],
            "data_status": "ok" if out else "pending"}


# ================================================================================================ shock
class MacroOverlay(BaseModel):
    scenario_id: str | None = None
    shocks: dict[str, float] | None = None
    options: dict[str, Any] | None = None


class ShockRequest(BaseModel):
    target_type: Literal["company", "country"]
    security_id: str | None = None
    iso2: str | None = Field(default=None, min_length=2, max_length=2)
    magnitude: float = Field(default=-0.10, ge=-1.0, le=1.0, description="revenue shock as a fraction (-0.10 = -10%)")
    damping: float = Field(default=C.DEFAULTS["damping"], ge=0.0, le=1.0)
    hops: int = Field(default=C.DEFAULTS["hops"], ge=1, le=4)
    demand_channel: bool = True
    supply_channel: bool = True
    supply_factor: float = Field(default=C.DEFAULTS["supply_factor"], ge=0.0, le=1.0)
    default_share: float = Field(default=C.DEFAULTS["default_share"], ge=0.0, le=0.5)
    include_statistical: bool = False
    include_home_country: bool = Field(default=False, description="country shock: also seed companies without a "
                                       "geographic revenue disclosure (100% home country assumed)")
    macro: MacroOverlay | None = None
    top_n: int = Field(default=30, ge=1, le=200)


def _graph(db: Db) -> dict[str, Any]:
    key = f"{db.snap.path}:{db.snap.run_id}"
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit
    ed = [C.Edge(e["supplier_key"], e["customer_key"], e["s_rev"], e["s_pur"], float(e["confidence"] or 0.0))
          for e in _rows(db, """SELECT supplier_key, customer_key, share_of_supplier_revenue AS s_rev,
                                       share_of_customer_purchases AS s_pur, confidence FROM sc_edges WHERE NOT stale""")]
    expo: dict[str, dict[str, float]] = {}
    disclosed: set[str] = set()
    for x in _rows(db, "SELECT issuer_id, key, direct, source FROM sc_exposure WHERE dim = 'country' AND direct > 0"):
        expo.setdefault(x["issuer_id"], {})[x["key"]] = float(x["direct"])
        if x["source"] == "geo_revenue":
            disclosed.add(x["issuer_id"])
    names = {}
    for e in _rows(db, "SELECT supplier_key, supplier_name, customer_key, customer_name FROM sc_edges"):
        names.setdefault(e["supplier_key"], e["supplier_name"])
        names.setdefault(e["customer_key"], e["customer_name"])
    g = {"edges": ed, "exposure": expo, "disclosed": disclosed, "names": names}
    with _cache_lock:
        _cache.clear()
        _cache[key] = g
    return g


def _macro_channel(db: Db, mo: MacroOverlay, security_ids: list[str]) -> tuple[dict[str, dict[str, float]], dict[str, Any]]:
    from app.api.routers import macro_v4 as M  # noqa: PLC0415
    from app.macro.markets import scenarios as S  # noqa: PLC0415

    eng = M._engine(db)
    if mo.scenario_id:
        sdef = M._find(mo.scenario_id)
        shocks, opts, name = (mo.shocks or sdef["shocks"]), (mo.options if mo.options is not None else sdef.get("options")), sdef["name"]
    elif mo.shocks:
        shocks, opts, name = mo.shocks, mo.options, "Custom macro shocks"
    else:
        raise invalid("macro overlay needs scenario_id or shocks")
    sh, opt = M._validate(shocks, opts)
    meta = {"name": name, "shocks": sh, "as_of": eng["as_of"], "label": S.MODEL_LABEL, "n_matched": 0}
    if eng["fits"].is_empty():
        return {}, meta | {"note": "macro betas not available in this snapshot"}
    res = S.run_scenario(eng["fits"], sh, opt, eng["fac"], prepared=eng["prepared"])
    fr = res["frame"]
    sec = fr.filter((fr["entity_type"] == "security") & fr["entity_id"].is_in(security_ids))
    out = {r["entity_id"]: {"impact": r["impact"], "lo": r["impact_lo"], "hi": r["impact_hi"]}
           for r in sec.iter_rows(named=True)}
    meta["n_matched"] = len(out)
    return out, meta


@router.post("/supply-chain/shock", operation_id="runSupplyChainShock")
def shock(body: ShockRequest, db: Db = Depends(get_db)) -> dict[str, Any]:
    g = _graph(db)
    notes: list[str] = []
    if body.target_type == "company":
        if not body.security_id:
            raise invalid("security_id is required for a company shock")
        r = _sec_of(db, body.security_id)
        seed_key = r["ref_issuer_id"]
        seeds = {seed_key: body.magnitude}
        seed = {"type": "company", "key": seed_key, "security_id": body.security_id, "name": r["ref_name"],
                "iso2": None, "n_seeds": 1}
    else:
        if not body.iso2:
            raise invalid("iso2 is required for a country shock")
        cc = body.iso2.upper()
        expo = g["exposure"] if body.include_home_country else {k: v for k, v in g["exposure"].items()
                                                                 if k in g["disclosed"]}
        seeds = C.country_seeds(expo, cc, body.magnitude)
        if not body.include_home_country:
            notes.append("country shock seeds: companies with a geographic revenue disclosure only "
                         "(include_home_country adds home-country-only companies at 100%)")
        seed = {"type": "country", "key": cc, "security_id": None, "name": cc, "iso2": cc, "n_seeds": len(seeds)}
        if not seeds:
            notes.append(f"no issuer with direct revenue exposure to {cc} in the snapshot")
    res = C.propagate(g["edges"], seeds, damping=body.damping, hops=body.hops, demand=body.demand_channel,
                      supply=body.supply_channel, supply_factor=body.supply_factor, default_share=body.default_share)
    ranked = sorted(res.impact.items(), key=lambda kv: -abs(kv[1]))
    top = [k for k, v in ranked if abs(v) > 1e-9][:body.top_n]
    stat_imp: dict[str, float] = {}
    if body.include_statistical and body.target_type == "company":
        nb = _rows(db, """SELECT issuer_id, neighbour_issuer_id, rho FROM sc_neighbours
                          WHERE issuer_id = ? AND method = 'return_corr'""", [seed["key"]])
        stat_imp = C.statistical_channel([(x["issuer_id"], x["neighbour_issuer_id"], float(x["rho"])) for x in nb],
                                         seeds, body.damping)
    keys = set(top) | set(stat_imp) | ({seed["key"]} if body.target_type == "company" else set())
    info = _node_info(db, [k for k in keys if not k.startswith(("name:", "cik:"))])
    macro_imp: dict[str, dict[str, float]] = {}
    macro_meta = None
    if body.macro is not None:
        sids = [o["security_id"] for o in info.values() if o.get("security_id")]
        macro_imp, macro_meta = _macro_channel(db, body.macro, sids)

    def node(k: str) -> dict[str, Any]:
        o = info.get(k, {})
        mi = macro_imp.get(o.get("security_id") or "", {})
        return {"key": k, "issuer_id": None if k.startswith(("name:", "cik:")) else k, "security_id": o.get("security_id"),
                "ticker": o.get("ticker"), "name": o.get("name") or g["names"].get(k) or k.split(":", 1)[-1],
                "country": o.get("country"), "sector_id": o.get("sector_id"),
                "macro_impact": _f(mi.get("impact"), 5), "macro_impact_lo": _f(mi.get("lo"), 5),
                "macro_impact_hi": _f(mi.get("hi"), 5)}

    impacts = []
    for k in top:
        p = C.path_to(res, k)
        impacts.append({**node(k), "revenue_impact": _f(res.impact[k], 6) or 0.0,
                        "by_hop": [_f(v, 6) or 0.0 for v in res.by_hop.get(k, [])], "first_hop": res.first_hop.get(k, 0),
                        "channel": res.channel.get(k, "seed"), "path": [g["names"].get(x) or (info.get(x) or {}).get("name") or x
                                                                        for x in p], "path_keys": p})
    statistical = [{**node(k), "rho_impact": _f(v, 6) or 0.0} for k, v in sorted(stat_imp.items(), key=lambda kv: -abs(kv[1]))]
    n_assumed = len(res.assumed)
    if n_assumed:
        notes.append(f"{n_assumed} edges without a disclosed share used the default share {body.default_share:.0%}")
    return {"model_label": C.MODEL_LABEL, "model_based": True, "as_of": iso(db.as_of), "seed": seed,
            "params": {"magnitude": body.magnitude, "damping": body.damping, "hops": body.hops,
                       "demand_channel": body.demand_channel, "supply_channel": body.supply_channel,
                       "supply_factor": body.supply_factor, "default_share": body.default_share},
            "seed_macro": node(seed["key"]) if body.target_type == "company" and body.macro is not None else None,
            "impacts": impacts, "statistical": statistical, "n_affected": sum(1 for v in res.impact.values() if abs(v) > 1e-9),
            "macro": macro_meta, "notes": notes, "method": [
                "x(0) = seed revenue shock (company) or magnitude x direct revenue share in the country",
                "demand channel: supplier j moves by s_rev(j, c) x customer move; supply channel: customer c moves by "
                "supply_factor x s_pur(j, c) x supplier move; edge confidence multiplies the transmission",
                "hop h effect = damping^(h-1) x M^h x(0); total = sum over hops (clipped to the seed size)",
                "macro channel (optional): W4 factor betas, equity-return estimate, reported separately"],
            "data_status": "ok" if g["edges"] or g["exposure"] else "pending"}


# ================================================================================================ portfolio exposure
class PortfolioExposureRequest(BaseModel):
    watchlist_id: str | None = None
    security_ids: list[str] | None = Field(default=None, max_length=500)
    weights: dict[str, float] | None = None
    basis: Literal["direct", "look_through"] = "direct"


@router.post("/supply-chain/portfolio-exposure", operation_id="getPortfolioSupplyChainExposure")
def portfolio_exposure(body: PortfolioExposureRequest, db: Db = Depends(get_db)) -> dict[str, Any]:
    name = None
    if body.watchlist_id:
        from app.api import app_state  # noqa: PLC0415

        w = app_state.get_watchlist(body.watchlist_id)
        if w is None:
            raise not_found(f"unknown watchlist_id {body.watchlist_id!r}")
        name = w["name"]
        sids = [i["security_id"] for i in app_state.watchlist_items(body.watchlist_id)]
    elif body.security_ids:
        sids = list(dict.fromkeys(body.security_ids))
    else:
        raise invalid("provide watchlist_id or security_ids")
    wts = {s: float((body.weights or {}).get(s, 1.0)) for s in sids}
    if any(v < 0 for v in wts.values()):
        raise invalid("weights must be >= 0")
    ref = {r["security_id"]: r for r in db.all(f"""SELECT security_id, issuer_id, ticker, name, country, sector_id
                                                    FROM sec WHERE security_id IN ({','.join('?' * len(sids))})""", sids)} \
        if sids else {}
    iids = {s: ref[s]["issuer_id"] for s in sids if s in ref and ref[s]["issuer_id"]}
    expo: dict[str, dict[str, dict[str, float]]] = {}
    via: dict[str, dict[str, list[dict[str, Any]]]] = {}
    sect: dict[str, dict[str, float]] = {}
    if iids:
        ph = ",".join("?" * len(set(iids.values())))
        for x in _rows(db, f"SELECT * FROM sc_exposure WHERE issuer_id IN ({ph})", sorted(set(iids.values()))):
            if x["dim"] == "country":
                d = expo.setdefault(x["issuer_id"], {"direct": {}, "look_through": {}})
                if x["direct"]:
                    d["direct"][x["key"]] = float(x["direct"])
                if x["look_through"]:
                    d["look_through"][x["key"]] = float(x["look_through"])
                via.setdefault(x["issuer_id"], {})[x["key"]] = _via(x["via"])
            elif x["look_through"]:
                sect.setdefault(x["issuer_id"], {})[x["key"]] = float(x["look_through"])
    met = {r["issuer_id"]: r for r in _rows(db, f"""SELECT * FROM sc_metrics WHERE issuer_id IN
                                                    ({','.join('?' * len(set(iids.values())))})""",
                                            sorted(set(iids.values())))} if iids else {}
    # home-country fallback for names without exposure rows
    for s, i in iids.items():
        if i not in expo and ref[s].get("country"):
            expo[i] = {"direct": {ref[s]["country"]: 1.0}, "look_through": {ref[s]["country"]: 1.0}}
    iw: dict[str, float] = {}
    for s, i in iids.items():
        iw[i] = iw.get(i, 0.0) + wts.get(s, 0.0)
    agg_d = C.aggregate_exposure(iw, expo, "direct")
    agg_l = C.aggregate_exposure(iw, expo, "look_through")
    rk = _risk_map(db)
    tot_w = sum(v for i, v in iw.items() if i in expo) or 1.0
    contrib: dict[str, list[dict[str, Any]]] = {}
    for s, i in iids.items():
        for c, v in (expo.get(i, {}).get(body.basis) or {}).items():
            contrib.setdefault(c, []).append({"security_id": s, "ticker": ref[s]["ticker"], "name": ref[s]["name"],
                                              "weight": round(wts.get(s, 0.0) / tot_w * v, 6)})
    countries = []
    for c in sorted(set(agg_d) | set(agg_l), key=lambda k: -(agg_l.get(k, 0) if body.basis == "look_through" else agg_d.get(k, 0))):
        countries.append({"iso2": c, "direct": _f(agg_d.get(c), 5), "look_through": _f(agg_l.get(c), 5),
                          "contributors": sorted(contrib.get(c, []), key=lambda x: -x["weight"])[:8],
                          "overlay": _overlay(rk.get(c))})
    agg = agg_l if body.basis == "look_through" else agg_d
    num = sum(w * float(rk[c]["risk_score"]) for c, w in agg.items() if c in rk and rk[c].get("risk_score") is not None)
    den = sum(w for c, w in agg.items() if c in rk and rk[c].get("risk_score") is not None)
    sw = sum(iw.get(i, 0.0) for i in sect) or 1.0
    sectors: dict[str, float] = {}
    for i, d in sect.items():
        for k, v in d.items():
            sectors[k] = sectors.get(k, 0.0) + iw.get(i, 0.0) / sw * v
    # counterparties shared across holdings (customers / suppliers), weighted by holding weight
    cp: dict[str, dict[str, Any]] = {}
    if iids:
        ph = ",".join("?" * len(set(iids.values())))
        ids = sorted(set(iids.values()))
        inv = {}
        for s, i in iids.items():
            inv.setdefault(i, []).append(s)
        for e in _rows(db, f"""SELECT * FROM sc_edges WHERE NOT stale AND (supplier_key IN ({ph}) OR customer_key IN ({ph}))""",
                       ids + ids):
            for holder, other, rel, share in ((e["supplier_key"], e["customer_key"], "customer", e["share_of_supplier_revenue"]),
                                              (e["customer_key"], e["supplier_key"], "supplier", e["share_of_customer_purchases"])):
                if holder not in iw:
                    continue
                o = cp.setdefault(other, {"key": other, "name": e["customer_name"] if rel == "customer" else e["supplier_name"],
                                          "relation": set(), "holders": [], "exposure": 0.0})
                o["relation"].add(rel)
                o["holders"].append(next((ref[s]["ticker"] for s in inv.get(holder, [])), holder))
                o["exposure"] += iw[holder] / (sum(iw.values()) or 1.0) * float(share or 0.0)
    shared = sorted(({"key": v["key"], "name": v["name"], "relation": "both" if len(v["relation"]) > 1 else next(iter(v["relation"])),
                      "holders": sorted(set(v["holders"])), "n_holders": len(set(v["holders"])),
                      "weighted_share": _f(v["exposure"], 5)} for v in cp.values()),
                    key=lambda x: (-x["n_holders"], -(x["weighted_share"] or 0)))[:30]
    items = []
    for s in sids:
        r = ref.get(s)
        i = iids.get(s)
        mm = met.get(i) if i else None
        items.append({"security_id": s, "ticker": r["ticker"] if r else None, "name": r["name"] if r else None,
                      "issuer_id": i, "country": r["country"] if r else None, "weight": _f(wts.get(s), 6),
                      "country_risk": _f((mm or {}).get("country_risk"), 2),
                      "top_country": (mm or {}).get("top_country") or (r or {}).get("country"),
                      "coverage_level": (mm or {}).get("coverage_level") or ("sparse" if r else None),
                      "geo_source": (mm or {}).get("geo_source") or ("home_country" if r else None)})
    cov = {"n_items": len(sids), "n_resolved": len(iids),
           "n_geo_revenue": sum(1 for i in iids.values() if (met.get(i) or {}).get("geo_source") == "geo_revenue"),
           "n_with_links": sum(1 for i in iids.values() if (met.get(i) or {}).get("n_customers") or (met.get(i) or {}).get("n_suppliers")),
           "n_home_country_only": sum(1 for i in iids.values() if (met.get(i) or {}).get("geo_source") != "geo_revenue")}
    return {"as_of": iso(db.as_of), "watchlist_id": body.watchlist_id, "name": name, "basis": body.basis,
            "countries": countries, "sectors": [{"sector_id": k, "look_through": _f(v, 5)} for k, v in
                                                sorted(sectors.items(), key=lambda kv: -kv[1])],
            "portfolio_country_risk": _f(num / den, 2) if den > 0 else None, "shared_counterparties": shared,
            "items": items, "coverage": cov, "labels": LABELS,
            "data_status": "ok" if _ready(db) else "pending"}
