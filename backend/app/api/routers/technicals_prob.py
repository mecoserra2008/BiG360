"""Technicals v2 T2-T4 probability models (docs/TECHNICALS_V2_PLAN.md; gate docs/TECHNICALS_V2_VALIDATION.md; tag company,
contract v1.34.0). MODEL-BASED: "calibrated on history", never "X% chance today".

  GET /technicals/{security_id}/setup-probabilities   T2: P(T1 before invalidation within H) / P(loss) / P(neither) for
                                                      H = 5 / 10 / 20 / 40 / 60 sessions + the setup's own horizon,
                                                      expected R after costs, break-even P(win), naive R:R and Brownian
                                                      baselines; optional target + stop (e.g. a thesis) computed at
                                                      query time from the same surface
  GET /technicals/{security_id}/zones                 confluence zones + T3 P(hold | touch), random-control P, edge vs
                                                      random with its significance flag, conformal P(touch) and the
                                                      combined P(touch) x P(hold | touch)
  GET /technicals/{security_id}/score-calibration     T4: percentile sub-scores within region x sector and the score's
                                                      historical bucket stats (5 / 20 / 60 sessions) + edge flag
Every output carries its T6 gate (experimental = failed / not validated; the UI hides it unless toggled). Serving
tables tp_* (app.api.serving _Builder.technicals_prob), zones / setups (technicals), cf_* (conformal).
"""

from __future__ import annotations

from typing import Any, Literal

import duckdb
from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, get_db
from app.api.routers._common import invalid, iso, not_found, sec_ref, sec_ref_sql
from app.api.routers import technicals_meta as TM
from app.core.config import get_settings
from app.conformal import serve as CS
from app.techprob import barriers as B
from app.techprob import serve as S

router = APIRouter(tags=["company"])

COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}


def _rows(db: Db, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    try:
        return db.all(sql, params or [])
    except duckdb.CatalogException:
        return []


def _sec(db: Db, security_id: str) -> dict[str, Any]:
    r = db.one(f"SELECT {sec_ref_sql('s')}, s.px_date AS _px_date FROM sec s WHERE s.security_id = ?", [security_id])
    if not r:
        raise not_found(f"unknown security_id {security_id!r}")
    return r


def _status(db: Db, ok: bool, *tables: str) -> str:
    if ok:
        return "ok"
    return "pending" if db.snap.status(*tables) == "pending" else "partial"


def _gates(db: Db) -> dict[str, dict[str, Any]]:
    return S.gate_index(_rows(db, "SELECT * FROM tp_gates"))


def _validation(gates: dict[str, dict[str, Any]], outputs: list[str]) -> list[dict[str, Any]]:
    return [S.gate_of(gates, o) for o in outputs]


def _env(db: Db, security_id: str) -> dict[str, Any] | None:
    rows = _rows(db, "SELECT * FROM cf_envelopes WHERE security_id = ?", [security_id])
    return rows[0] if rows else None


# ------------------------------------------------------------------------------------------------ T2


def _horizon_out(h: dict[str, Any], gates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    g = S.gate_of(gates, f"t2_h{int(h['horizon'])}")
    return {"horizon": int(h["horizon"]), "is_setup_horizon": bool(h.get("is_setup_horizon")),
            "p_win": S.r(h.get("p_win"), 4), "p_loss": S.r(h.get("p_loss"), 4), "p_neither": S.r(h.get("p_neither"), 4),
            "expected_r": S.r(h.get("expected_r"), 4), "expected_r_mtm": S.r(h.get("expected_r_mtm"), 4),
            "break_even_p": S.r(h.get("break_even_p"), 4), "break_even_p_costs": S.r(h.get("break_even_p_costs"), 4),
            "cost_r": S.r(h.get("cost_r"), 4), "earn_window": bool(h.get("earn_window")),
            "n_pool": int(h["n_pool"]) if h.get("n_pool") is not None else None,
            "region_fallback": bool(h.get("region_fallback")), "clamped": bool(h.get("clamped")),
            "p_win_naive": S.r(h.get("p_win_naive"), 4), "p_win_bm": S.r(h.get("p_win_bm"), 4),
            "a": S.r(h.get("a"), 4), "b": S.r(h.get("b"), 4), "gate": g["verdict"], "experimental": g["experimental"]}


@router.get("/technicals/{security_id}/setup-probabilities", operation_id="getTechnicalsSetupProbabilities")
def get_setup_probabilities(security_id: str,
                            target: float | None = Query(None, gt=0, description="Custom target price (with stop), e.g. a thesis target"),
                            stop: float | None = Query(None, gt=0, description="Custom stop / invalidation price (with target)"),
                            direction: Literal["long", "short"] | None = Query(None, description="Custom barrier direction (default: target above the close = long)"),
                            db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    if (target is None) != (stop is None):
        raise invalid("target and stop must be given together")
    gates = _gates(db)
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "last_close": None, "currency": sr.get("ref_currency") or None,
        "model_based": True, "mode": "custom" if target is not None else "setup", "setup": None, "setup_horizon": None,
        "horizons": [], "pool": None, "validation": _validation(gates, [f"t2_h{h}" for h in B.HORIZONS]),
        "labels": {k: S.LABELS[k] for k in ("model_based", "setup", "expected_r", "break_even", "experimental")},
        "validation_doc": S.VALIDATION_DOC, "data_status": "pending", "meta": None}
    rows = _rows(db, "SELECT * FROM tp_setups WHERE security_id = ? ORDER BY horizon", [security_id])
    st = _rows(db, "SELECT bias, entry, target1, invalidation, reward_risk, last_close FROM setups WHERE security_id = ?", [security_id])
    srow = st[0] if st else {}
    if target is None:
        if not rows:
            out["data_status"] = _status(db, False, "tech_prob_setups")
            if srow:
                out["setup"] = {"source": "setup", "bias": srow.get("bias"), "direction": None, "entry": S.r(srow.get("entry")),
                                "target": S.r(srow.get("target1")), "stop": S.r(srow.get("invalidation")),
                                "reward_risk": S.r(srow.get("reward_risk"), 4), "reward_risk_close": None}
            return out
        r0 = rows[0]
        out.update(as_of=iso(r0["as_of_date"]), last_close=S.r(r0.get("close")))
        out["setup"] = {"source": "setup", "bias": r0.get("bias"), "direction": "long" if int(r0["direction"]) > 0 else "short",
                        "entry": S.r(r0.get("entry")), "target": S.r(r0.get("target1")), "stop": S.r(r0.get("invalidation")),
                        "reward_risk": S.r(r0.get("reward_risk"), 4), "reward_risk_close": S.r(r0.get("reward_risk_close"), 4)}
        out["horizons"] = [_horizon_out(h, gates) for h in rows]
        out["pool"] = {"region": r0.get("region_pool"), "vol_regime": r0.get("vol_regime"), "fit_date": r0.get("fit_date")}
        out["meta"] = TM.meta_view(db, security_id, get_settings().data_dir, None)       # v1.35.0 (additive)
    else:
        env = _env(db, security_id)
        close = S.f((env or {}).get("last_close")) or (S.f(rows[0].get("close")) if rows else None) or S.f(srow.get("last_close"))
        surf = S.surface_from_rows(_rows(db, "SELECT * FROM tp_surfaces"))
        if close is None or surf is None:
            out["data_status"] = _status(db, False, "tech_prob_surfaces", "conformal_envelopes")
            return out
        bar = S.custom_barriers(close, float(target), float(stop), direction)
        if bar is None:
            raise invalid("target and stop must lie on opposite sides of the last close (long: stop < close < target)")
        if rows:
            Vh = {int(h["horizon"]): float(h["sigma_h"]) ** 2 for h in rows if S.f(h.get("sigma_h"))}
            eda = None
            if env and env.get("earn_days_ahead") is not None:
                eda = int(env["earn_days_ahead"])
            vreg = B.VOL_REGIMES.index(rows[0]["vol_regime"]) if rows[0].get("vol_regime") in B.VOL_REGIMES else 1
            region = rows[0].get("region_pool") or (env or {}).get("region") or sr.get("ref_region")
            # sigma_h of the stored rows already contains the earnings jump of its own window
        else:
            Vh, eda = S.variances_from_envelope(env)
            vreg = 1
            region = (env or {}).get("region") or sr.get("ref_region")
        if not Vh:
            out["data_status"] = _status(db, False, "conformal_envelopes")
            return out
        px = _rows(db, "SELECT adv_usd_20d FROM px_stats WHERE security_id = ?", [security_id])
        adv = S.f(px[0]["adv_usd_20d"]) if px else None
        sd = S.f((env or {}).get("sigma_d"))
        hz = S.horizon_rows(surf, close, bar, Vh, region, vreg, eda, adv, sd, COSTS)
        d, _, _, rr = bar
        out.update(as_of=iso((env or {}).get("as_of_date") or (rows[0]["as_of_date"] if rows else None)), last_close=S.r(close))
        out["setup"] = {"source": "custom", "bias": None, "direction": "long" if d > 0 else "short", "entry": S.r(close),
                        "target": S.r(target), "stop": S.r(stop), "reward_risk": S.r(rr, 4), "reward_risk_close": S.r(rr, 4)}
        out["horizons"] = [_horizon_out(h, gates) for h in hz]
        out["pool"] = {"region": region, "vol_regime": B.VOL_REGIMES[vreg], "fit_date": surf.meta.get("fit_date")}
        sc = _rows(db, "SELECT trend, structure_state, score_trend, score_structure, score_location, score_momentum FROM setups "
                       "WHERE security_id = ?", [security_id])
        out["meta"] = TM.meta_view(db, security_id, get_settings().data_dir, {     # v1.35.0 (additive): query-time meta
            "horizons": [h for h in hz if h["horizon"] in (10, 20, 40)], "bar": bar, "close": close, "sigma_d": sd, "vreg": vreg,
            "sector": sr.get("ref_sector_id"), "scores": sc[0] if sc else None})
    own = [h["horizon"] for h in out["horizons"] if h["is_setup_horizon"]]
    out["setup_horizon"] = own[0] if own else None
    out["data_status"] = "ok" if out["horizons"] else "partial"
    return out


# ------------------------------------------------------------------------------------------------ T3


@router.get("/technicals/{security_id}/zones", operation_id="getTechnicalsZones")
def get_zones(security_id: str, horizon: int = Query(20, ge=1, le=20, description="Touch horizon in sessions (calibrated 1..20)"),
              db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    gates = _gates(db)
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "last_close": None, "currency": sr.get("ref_currency") or None,
        "model_based": True, "horizon": horizon, "zones": [], "validation": _validation(gates, ["t3_hold", "t3_edge"]),
        "labels": {k: S.LABELS[k] for k in ("model_based", "zone_hold", "zone_touch", "no_edge", "experimental")},
        "validation_doc": S.VALIDATION_DOC, "data_status": "pending"}
    zones = _rows(db, """SELECT as_of_date, zone_id, rank, side, zone_low, zone_high, center, strength, distance_atr, in_zone,
                                member_families, member_count, n_families, last_close FROM zones WHERE security_id = ?
                         ORDER BY rank""", [security_id])
    if not zones:
        out["data_status"] = _status(db, False, "confluence_zones")
        return out
    out["as_of"] = iso(zones[0]["as_of_date"])
    tz = {int(z["zone_id"]): z for z in _rows(db, "SELECT * FROM tp_zones WHERE security_id = ?", [security_id])
          if z.get("as_of_date") == zones[0]["as_of_date"]}
    env = _env(db, security_id)
    idx = {}
    if env is not None:
        idx = CS.curve_index(_rows(db, "SELECT pool_key, horizon, side, kind, n, probs, u FROM cf_touch_curves "
                                       "WHERE region = ? OR pool_key = ?", [env.get("region"), env.get("pool_key")]))
    lc = S.f((env or {}).get("last_close")) or S.f(zones[0].get("last_close"))
    out["last_close"] = S.r(lc)
    g_hold, g_edge = S.gate_of(gates, "t3_hold"), S.gate_of(gates, "t3_edge")
    for z in zones:
        lo, hi = S.f(z["zone_low"]), S.f(z["zone_high"])
        t = tz.get(int(z["zone_id"]))
        p_touch = None
        if lo is not None and hi is not None and lc:
            inside = bool(z.get("in_zone")) or lo <= lc <= hi
            if inside:
                p_touch = 1.0
            elif env is not None and idx:
                p_touch = CS.touch(hi if hi < lc else lo, env, horizon, idx)["p_touch"]
        p_hold = S.f(t.get("p_hold")) if t else None
        sig = bool(t.get("edge_significant")) if t else False
        direction = t.get("edge_direction") if t else None
        out["zones"].append({
            "zone_id": int(z["zone_id"]), "rank": int(z["rank"]) if z.get("rank") is not None else None, "side": z["side"],
            "zone_low": S.r(lo), "zone_high": S.r(hi), "center": S.r(z.get("center")), "strength": S.r(z.get("strength"), 2),
            "distance_atr": S.r(z.get("distance_atr"), 3), "in_zone": bool(z.get("in_zone")),
            "member_families": list(z.get("member_families") or []), "member_count": int(z.get("member_count") or 0),
            "p_touch": S.r(p_touch, 4), "p_hold": S.r(p_hold, 4), "p_hold_ctrl": S.r(t.get("p_hold_ctrl"), 4) if t else None,
            "p_touch_hold": S.r(p_touch * p_hold, 4) if p_touch is not None and p_hold is not None else None,
            "edge": S.r(t.get("edge"), 4) if t else None, "edge_significant": sig, "edge_direction": direction,
            "edge_ci": [S.r(t.get("edge_ci_lo"), 4), S.r(t.get("edge_ci_hi"), 4)] if t and t.get("edge_ci_lo") is not None else None,
            "edge_label": S.edge_label(sig, direction) if t else None,
            "gate": g_hold["verdict"], "experimental": g_hold["experimental"], "edge_experimental": g_edge["experimental"]})
    out["data_status"] = "ok" if tz else "partial"
    return out


# ------------------------------------------------------------------------------------------------ T4


@router.get("/technicals/{security_id}/score-calibration", operation_id="getTechnicalsScoreCalibration")
def get_score_calibration(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    gates = _gates(db)
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "model_based": True, "technical_score": None, "technical_score_pct": None,
        "pct_technical": None, "cross_section": "region x sector", "n_peers": None, "sub_scores": [], "buckets": [],
        "tables": [], "validation": _validation(gates, ["t4_h5", "t4_h20", "t4_h60"]),
        "labels": {k: S.LABELS[k] for k in ("model_based", "score", "score_no_edge", "experimental")},
        "validation_doc": S.VALIDATION_DOC, "data_status": "pending"}
    rows = _rows(db, "SELECT * FROM tp_scores WHERE security_id = ?", [security_id])
    if not rows:
        st = _rows(db, "SELECT as_of_date, technical_score, score_trend, score_structure, score_location, score_rr, "
                       "score_momentum FROM setups WHERE security_id = ?", [security_id])
        if st:
            s0 = st[0]
            out.update(as_of=iso(s0["as_of_date"]), technical_score=S.r(s0.get("technical_score"), 3))
            out["sub_scores"] = [{"name": k, "raw": S.r(s0.get(f"score_{k}"), 3), "percentile": None}
                                 for k in ("trend", "structure", "location", "rr", "momentum")]
        out["data_status"] = _status(db, False, "tech_prob_scores")
        return out
    r = rows[0]
    out.update(as_of=iso(r["as_of_date"]), technical_score=S.r(r.get("technical_score"), 3),
               technical_score_pct=S.r(r.get("technical_score_pct"), 3), pct_technical=S.r(r.get("pct_technical"), 3),
               n_peers=int(r["n_peers"]) if r.get("n_peers") is not None else None)
    out["sub_scores"] = [{"name": k, "raw": S.r(r.get(f"score_{k}"), 3), "percentile": S.r(r.get(f"pct_{k}"), 3)}
                         for k in ("trend", "structure", "location", "rr", "momentum")]
    tabs = _rows(db, "SELECT * FROM tp_score_buckets WHERE score_col = 'technical_score' ORDER BY horizon, decile")
    for h in (5, 20, 60):
        g = S.gate_of(gates, f"t4_h{h}")
        b = {"median": r.get(f"h{h}_median"), "q25": r.get(f"h{h}_q25"), "q75": r.get(f"h{h}_q75"),
             "hit_rate": r.get(f"h{h}_hit_rate"), "n": r.get(f"h{h}_n")}
        out["buckets"].append({
            "horizon": h, "decile": r.get(f"h{h}_decile"), "n": int(r[f"h{h}_n"]) if r.get(f"h{h}_n") is not None else None,
            "median": S.r(b["median"], 5), "q25": S.r(b["q25"], 5), "q75": S.r(b["q75"], 5), "mean": S.r(r.get(f"h{h}_mean"), 5),
            "hit_rate": S.r(b["hit_rate"], 4),
            "median_ci": [S.r(r.get(f"h{h}_median_lo"), 5), S.r(r.get(f"h{h}_median_hi"), 5)] if r.get(f"h{h}_median_lo") is not None else None,
            "hit_ci": [S.r(r.get(f"h{h}_hit_lo"), 4), S.r(r.get(f"h{h}_hit_hi"), 4)] if r.get(f"h{h}_hit_lo") is not None else None,
            "edge_significant": bool(r.get(f"h{h}_edge_significant")), "line": S.score_line(h, b),
            "gate": g["verdict"], "experimental": g["experimental"]})
        t = [x for x in tabs if int(x["horizon"]) == h]
        if t:
            out["tables"].append({"horizon": h, "ic_mean": S.r(t[0].get("ic_mean"), 4), "ic_t": S.r(t[0].get("ic_t"), 3),
                                  "edge_significant": bool(t[0].get("edge_significant")),
                                  "deciles": [{"decile": int(x["decile"]), "score_lo": S.r(x.get("score_lo"), 3),
                                               "score_hi": S.r(x.get("score_hi"), 3), "n": int(x["n"]),
                                               "median": S.r(x.get("median"), 5), "q25": S.r(x.get("q25"), 5),
                                               "q75": S.r(x.get("q75"), 5), "hit_rate": S.r(x.get("hit_rate"), 4)} for x in t]})
    out["data_status"] = "ok" if any(b["median"] is not None for b in out["buckets"]) else "partial"
    return out
