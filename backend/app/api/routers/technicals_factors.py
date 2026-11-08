"""Technicals v3 #1 / #5: evidence-based technical factors and volume-confirmed breakouts (docs/TECHNICALS_V3_IDEAS.md;
gate docs/TECHNICALS_V3_VALIDATION.md; tag company, contract v1.35.0).

  GET /technicals/{security_id}/factors    the seven literature factors (52-week-high proximity, 12-1 momentum, residual
                                           momentum, 1-month reversal, MAX, idiosyncratic volatility, industry momentum):
                                           raw value, percentile within region x sector, each factor's own out-of-sample
                                           rank IC (5 / 20 / 60 sessions, Newey-West t, BH-adjusted p) and gate verdict,
                                           plus the composite (only when it passed the gate)
  GET /technicals/{security_id}/breakout   the latest breakout of the last 20 sessions and its state (active / followed
                                           through / failed), volume confirmation + score, calibrated P(follow-through)
                                           (model-based) vs the geometry baseline and the base rate, the validation
                                           evidence (confirmed vs unconfirmed follow-through), recent events
Serving tables tv3_factors / tv3_eval / tv3_bo_state / tv3_bo_events (app.api.serving _Builder.technicals_v3).
"""

from __future__ import annotations

from typing import Any

import duckdb
from fastapi import APIRouter, Depends

from app.api.deps import Db, get_db
from app.api.routers._common import iso, not_found, sec_ref, sec_ref_sql
from app.techv3 import serve as S

router = APIRouter(tags=["company"])


def _rows(db: Db, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    try:
        return db.all(sql, params or [])
    except duckdb.CatalogException:
        return []


def _sec(db: Db, security_id: str) -> dict[str, Any]:
    r = db.one(f"SELECT {sec_ref_sql('s')} FROM sec s WHERE s.security_id = ?", [security_id])
    if not r:
        raise not_found(f"unknown security_id {security_id!r}")
    return r


def _status(db: Db, ok: bool, *tables: str) -> str:
    if ok:
        return "ok"
    return "pending" if db.snap.status(*tables) == "pending" else "partial"


def _eval(db: Db) -> list[dict[str, Any]]:
    return _rows(db, "SELECT * FROM tv3_eval")


@router.get("/technicals/{security_id}/factors", operation_id="getTechnicalsFactors")
def get_factors(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    ev = _eval(db)
    idx = S.eval_index(ev)
    region = sr.get("ref_region")
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "region": region, "sector_id": None, "cross_section": "region x sector",
        "n_peers": None, "liquid": None, "size_bucket": None, "adv20_usd": None,
        "region_validated": region in S.GATE_REGIONS, "factors": [], "composite": S.composite_item(None, idx),
        "validation": _validation(idx, ev), "labels": dict(S.LABELS), "validation_doc": S.VALIDATION_DOC,
        "data_status": "pending"}
    rows = _rows(db, "SELECT * FROM tv3_factors WHERE security_id = ?", [security_id])
    if not rows:
        out["data_status"] = _status(db, False, "tech_factors")
        return out
    row = rows[0]
    region = row.get("region") or region
    out.update(as_of=iso(row["as_of_date"]), region=region, sector_id=row.get("sector_id"),
               cross_section="region x sector" if row.get("pct_scope") == "sector" else "region",
               n_peers=int(row["n_peers"]) if row.get("n_peers") is not None else None, liquid=bool(row.get("liquid")),
               size_bucket=row.get("size_bucket"), adv20_usd=S.r(row.get("adv20_usd"), 0),
               region_validated=region in S.GATE_REGIONS)
    out["factors"] = S.factor_items(row, idx, region)
    out["composite"] = S.composite_item(row, idx)
    out["data_status"] = "ok" if any(x["percentile"] is not None for x in out["factors"]) else "partial"
    return out


def _validation(idx: dict[tuple, dict[str, Any]], ev: list[dict[str, Any]]) -> dict[str, Any]:
    top = idx.get(("composite", 20, "ALL", "all", "all")) or {}
    d = S.detail(top)
    n_cells = d.get("n_cells_factor")
    return {"generated_at": d.get("generated_at"), "n_cells_tested": n_cells,
            "multiple_testing": "Benjamini-Hochberg over every factor x horizon x region cell; Holm over the seven primary "
                                "(pooled, 20-session) cells",
            "passing": sorted({x["signal"] for x in ev if x.get("verdict") == "pass" and x.get("eval_kind") == "validation"
                               and not str(x["signal"]).startswith("bo_")}),
            "period": d.get("period"), "universe": d.get("universe")}


@router.get("/technicals/{security_id}/breakout", operation_id="getTechnicalsBreakout")
def get_breakout(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    idx = S.eval_index(_eval(db))
    g = idx.get(("bo_p20", 20, "ALL", "all", "all")) or {}
    gv = g.get("verdict") if g.get("verdict") in ("pass", "fail") else "not_validated"
    eff = S.detail(idx.get(("bo_confirm", 20, "ALL", "all", "all")))
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "model_based": True, "state": "none", "direction": None, "lookback": None,
        "event_date": None, "sessions_since": None, "level": None, "target": None, "last_close": None, "confirmed": None,
        "confirm_score": None, "relvol50": None, "clv": None, "p_follow": None, "p_geo": None, "base_rate": None,
        "horizon": 20, "gate": gv, "experimental": gv != "pass",
        "evidence": _evidence(eff, idx), "recent": [], "labels": {k: S.LABELS[k] for k in ("breakout", "confirmation", "experimental")},
        "validation_doc": S.VALIDATION_DOC, "data_status": "pending"}
    st = _rows(db, "SELECT * FROM tv3_bo_state WHERE security_id = ?", [security_id])
    recent = _rows(db, """SELECT date, direction, lookback, confirmed, confirm_score, p_follow_20, out_20, level, close
                          FROM tv3_bo_events WHERE security_id = ? ORDER BY date DESC LIMIT 10""", [security_id])
    out["recent"] = [{"date": iso(x["date"]), "direction": int(x["direction"]), "lookback": int(x["lookback"]),
                      "confirmed": bool(x["confirmed"]), "confirm_score": S.r(x.get("confirm_score"), 1),
                      "p_follow": S.r(x.get("p_follow_20"), 4), "outcome": x.get("out_20"), "level": S.r(x.get("level"), 4)}
                     for x in recent]
    if not st:
        out["data_status"] = _status(db, bool(recent), "tech_breakout_state", "tech_breakouts")
        return out
    s0 = st[0]
    out.update(as_of=iso(s0["as_of_date"]), state=s0["state"], direction=s0.get("direction"), lookback=s0.get("lookback"),
               event_date=iso(s0.get("event_date")), sessions_since=s0.get("sessions_since"), level=S.r(s0.get("level"), 4),
               target=S.r(s0.get("target_20"), 4), last_close=S.r(s0.get("last_close"), 4),
               confirmed=None if s0.get("confirmed") is None else bool(s0["confirmed"]),
               confirm_score=S.r(s0.get("confirm_score"), 1), relvol50=S.r(s0.get("relvol50"), 3), clv=S.r(s0.get("clv"), 3),
               p_follow=S.r(s0.get("p_follow_20"), 4), p_geo=S.r(s0.get("p_geo_20"), 4), base_rate=S.r(s0.get("base_rate_20"), 4))
    out["data_status"] = "ok"
    return out


def _evidence(eff: dict[str, Any], idx: dict[tuple, dict[str, Any]]) -> dict[str, Any]:
    p = S.detail(idx.get(("bo_p20", 20, "ALL", "all", "all")))
    return {"confirmed_rate": S.r(eff.get("confirmed_rate"), 4), "unconfirmed_rate": S.r(eff.get("unconfirmed_rate"), 4),
            "control_rate": S.r(eff.get("control_rate"), 4), "base_rate": S.r(eff.get("base_rate"), 4),
            "diff": S.r(eff.get("diff"), 4), "diff_ci": eff.get("diff_ci"),
            "geometry_adjusted_diff": S.r(eff.get("geo_diff"), 4), "geometry_adjusted_ci": eff.get("geo_ci"),
            "significant": bool(eff.get("significant")), "verdict": eff.get("verdict") or "not_validated",
            "ece": S.r(p.get("ece"), 4), "brier_vs_geometry": S.r(p.get("brier_full_minus_geo"), 6),
            "n_events": eff.get("n_events"), "summary": eff.get("summary")}
