"""Technicals v3 #2 meta-labelled setups (gate docs/TECHNICALS_V3_VALIDATION.md "Meta-labelling"; tag company, contract
v1.35.0). MODEL-BASED: "calibrated on history", never "X% chance today".

  GET /technicals/{security_id}/meta-setup   context-adjusted (meta) P(T1 before the invalidation within H) / P(loss) /
                                             P(neither), meta expected R after costs, take / skip at the in-sample
                                             threshold, quarter-Kelly risk fraction (capped), relative size and the top
                                             drivers of the adjustment vs T2, for H = 10 / 20 / 40, with the T2 base
                                             alongside; every horizon carries its gate (meta_h*: calibration gate;
                                             decision_h*: economic check of take / skip and size).
The thesis view (custom target / stop) is an additive `meta` field of GET /technicals/{id}/setup-probabilities
(app.api.routers.technicals_prob). Serving tables tm_setups / tm_gates (app.api.serving _Builder.technicals_meta).
"""

from __future__ import annotations

from typing import Any

import duckdb
from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, get_db
from app.api.routers._common import iso, not_found, sec_ref, sec_ref_sql
from app.techmeta import serve as MS

router = APIRouter(tags=["company"])

OUTPUTS = ("meta_h10", "meta_h20", "meta_h40", "decision_h10", "decision_h20", "decision_h40")


def _rows(db: Db, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    try:
        return db.all(sql, params or [])
    except duckdb.CatalogException:
        return []


def gates(db: Db) -> dict[str, dict[str, Any]]:
    return MS.gate_index(_rows(db, "SELECT * FROM tm_gates"))


def horizon_out(h: dict[str, Any], gi: dict[str, dict[str, Any]], source: str = "stored") -> dict[str, Any]:
    H = int(h["horizon"])
    g, gd = MS.gate_of(gi, f"meta_h{H}"), MS.gate_of(gi, f"decision_h{H}")
    dom = bool(h.get("in_domain", True))
    if source == "stored":
        exp = (h.get("gate") or "experimental") != "production"
        dexp = (h.get("gate_decision") or "experimental") != "production"
    else:
        exp = g["experimental"] or not dom
        dexp = gd["experimental"] or exp
    pw, t2 = MS.f(h.get("p_win")), MS.f(h.get("t2_p_win"))
    take = h.get("take")
    return {"horizon": H, "p_win": MS.r(pw, 4), "p_loss": MS.r(h.get("p_loss"), 4), "p_neither": MS.r(h.get("p_neither"), 4),
            "expected_r": MS.r(h.get("expected_r"), 4), "take": None if take is None else bool(take),
            "decision": None if take is None else ("take" if take else "skip"),
            "kelly_fraction": MS.r(h.get("kelly_fraction"), 5), "size_rel": MS.r(h.get("size_rel"), 3),
            "threshold": MS.r(h.get("threshold"), 4) if MS.f(h.get("threshold")) is not None else None,
            "t2_p_win": MS.r(t2, 4), "t2_p_loss": MS.r(h.get("t2_p_loss"), 4), "t2_expected_r": MS.r(h.get("t2_expected_r"), 4),
            "delta_p_win": MS.r(pw - t2, 4) if pw is not None and t2 is not None else None,
            "drivers": MS.drivers(h.get("drivers"), 3), "model_kind": h.get("model_kind"), "in_domain": dom,
            "gate": g["verdict"], "experimental": exp, "decision_gate": gd["verdict"], "decision_experimental": dexp}


@router.get("/technicals/{security_id}/meta-setup", operation_id="getTechnicalsMetaSetup")
def get_meta_setup(security_id: str, horizon: int = Query(20, description="Preferred horizon (10 / 20 / 40); all are returned"),
                   db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = db.one(f"SELECT {sec_ref_sql('s')} FROM sec s WHERE s.security_id = ?", [security_id])
    if not sr:
        raise not_found(f"unknown security_id {security_id!r}")
    gi = gates(db)
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "model_based": True, "setup": None,
        "horizon": horizon if horizon in (10, 20, 40) else 20, "horizons": [], "fit_date": None,
        "validation": [MS.gate_of(gi, o) for o in OUTPUTS], "labels": dict(MS.LABELS), "validation_doc": MS.VALIDATION_DOC,
        "data_status": "pending"}
    rows = _rows(db, "SELECT * FROM tm_setups WHERE security_id = ? ORDER BY horizon", [security_id])
    if not rows:
        out["data_status"] = "pending" if db.snap.status("tech_meta_setups") == "pending" else "partial"
        return out
    r0 = rows[0]
    st = _rows(db, "SELECT entry, target1, invalidation FROM setups WHERE security_id = ?", [security_id])
    s0 = st[0] if st else {}
    out.update(as_of=iso(r0["as_of_date"]), fit_date=r0.get("fit_date"))
    out["setup"] = {"direction": "long" if int(r0["direction"]) > 0 else "short", "reward_risk_close": MS.r(r0.get("reward_risk_close"), 4),
                    "entry": MS.r(s0.get("entry")), "target": MS.r(s0.get("target1")), "stop": MS.r(s0.get("invalidation")),
                    "cost_r": MS.r(r0.get("cost_r"), 4)}
    out["horizons"] = [horizon_out(h, gi) for h in rows]
    out["data_status"] = "ok"
    return out


def meta_view(db: Db, security_id: str, data_dir: Any, custom: dict[str, Any] | None) -> dict[str, Any] | None:
    """Additive `meta` field of setup-probabilities: stored rows (mode setup) or query-time scoring of the custom
    barriers (thesis) with today's context features. None when no meta model output exists."""
    gi = gates(db)
    rows = _rows(db, "SELECT * FROM tm_setups WHERE security_id = ? ORDER BY horizon", [security_id])
    val = [MS.gate_of(gi, o) for o in OUTPUTS]
    if custom is None:
        if not rows:
            return None
        return {"source": "stored", "fit_date": rows[0].get("fit_date"), "horizons": [horizon_out(h, gi) for h in rows],
                "validation": val, "note": MS.LABELS["meta"]}
    if not rows:
        return None
    art = MS.artifact(data_dir)
    if art is None:
        return None
    r0 = rows[0]
    try:
        res = MS.custom_meta(art, r0.get("features"), custom["horizons"], custom["bar"], custom["close"], custom.get("sigma_d"),
                             custom.get("vreg", 1), r0.get("region"), custom.get("sector"), custom.get("scores"))
    except Exception:  # noqa: BLE001 - the meta view is optional; never fail the T2 response
        return None
    hz = []
    for h in res:
        h.update(in_domain=bool(r0.get("in_domain", True)))
        hz.append(horizon_out(h, gi, source="custom"))
    if not hz:
        return None
    return {"source": "custom", "fit_date": art.get("fit_date"), "horizons": hz, "validation": val, "note": MS.LABELS["meta"]}
