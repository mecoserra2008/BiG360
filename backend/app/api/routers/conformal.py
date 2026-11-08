"""Conformal ellipsoids (tag conformal, contract v1.32.0; docs/CONFORMAL_TECHNICALS_PLAN.md P3). MODEL-BASED.

Scope is what passed the P4 gate (docs/CONFORMAL_VALIDATION.md):
  GET /conformal/envelope/{security_id}  marginal per-horizon "calibrated range" (qm_*) at 80/90/95% x 1/5/10/20
                                         sessions as prices; next-bar bands flagged experimental; pool coverage;
                                         + fan (v1.33.0, Technicals v2 T1): the marginal band at every horizon
                                         1..20 dated by exchange sessions, earnings date flagged (fan_info)
  GET /conformal/touch                   calibrated touch probabilities for given levels, or (no levels) the
                                         security's confluence zones and technical levels
  GET /conformal/peer-groups             peer-group joint-anomaly percentile, history, member attribution,
                                         break-away member, group definition (descriptive; not a trading signal)
  GET /conformal/pair-band               calibrated next-session band for any linear combination of group members
  GET /conformal/coverage                coverage audit (nightly rolling + P4 walk-forward) and the gate verdicts
The joint simultaneous envelope (b{h}_*) failed the gate and is never served. Serving tables: cf_* (app.api.serving
_Builder.conformal). Coverage is marginal over history: "calibrated to X% over history", never "X% chance today".
"""

from __future__ import annotations

import json
import math
from datetime import date as date_cls
from typing import Any

import duckdb
import numpy as np
from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, get_db
from app.api.routers._common import invalid, iso, not_found, sec_ref, sec_ref_sql
from app.conformal import serve as S
from app.core.config import get_settings

router = APIRouter(tags=["conformal"])

ENVELOPE_FILTER = {"plain": "", "aci": "+aci"}
COVERAGE_APPS = ("envelope", "nextbar", "peer", "envelope_wf", "nextbar_wf", "peer_wf")


def _rows(db: Db, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    """Query tolerating a snapshot built before the cf_* tables existed (-> [])."""
    try:
        return db.all(sql, params or [])
    except duckdb.CatalogException:
        return []


def _sec(db: Db, security_id: str) -> dict[str, Any]:
    r = db.one(f"SELECT {sec_ref_sql('s')}, s.px_date AS _px_date, s.mic AS _mic FROM sec s WHERE s.security_id = ?",
               [security_id])
    if not r:
        raise not_found(f"unknown security_id {security_id!r}")
    return r


def _envelope_row(db: Db, security_id: str) -> dict[str, Any] | None:
    rows = _rows(db, "SELECT * FROM cf_envelopes WHERE security_id = ?", [security_id])
    return rows[0] if rows else None


def _r(v: Any, nd: int = 6) -> float | None:
    x = S.f(v)
    return None if x is None else round(x, nd)


def _status(db: Db, ok: bool, *tables: str) -> str:
    if ok:
        return "ok"
    return "pending" if db.snap.status(*tables) == "pending" else "partial"


# ------------------------------------------------------------------------------------------------ envelope


def _pool_coverage(db: Db, row: dict[str, Any]) -> list[dict[str, Any]]:
    flt = f"{row.get('filter') or 'ewma'}{ENVELOPE_FILTER.get(row.get('radius_mode') or 'plain', '')}"
    hz = [f"m{h}" for h in S.HORIZONS]
    rows = _rows(db, f"""SELECT 'rolling' AS source, segment, horizon, level, coverage, n, gap_pp, status, window_start,
                                window_end FROM cf_coverage
                         WHERE application = 'envelope' AND filter = ? AND segment_type = 'region' AND segment = ?
                           AND horizon IN ({', '.join('?' * len(hz))})
                         UNION ALL
                         SELECT 'walk_forward', segment, 'm' || horizon, level, coverage, n, gap_pp, status, window_start,
                                window_end FROM cf_coverage
                         WHERE application = 'envelope_wf' AND filter = 'm_ewma' AND segment_type = 'overall'
                           AND horizon IN ('1', '5', '10', '20')""", [flt, row.get("region"), *hz])
    out = []
    for r in rows:
        out.append({"source": r["source"], "segment": r["segment"], "horizon": int(str(r["horizon"]).lstrip("m")),
                    "level": int(r["level"]), "coverage": _r(r["coverage"], 4), "n": int(r["n"] or 0),
                    "gap_pp": _r(r["gap_pp"], 2), "status": r["status"], "window_start": iso(r["window_start"]),
                    "window_end": iso(r["window_end"])})
    return sorted(out, key=lambda x: (x["source"] != "rolling", x["horizon"], x["level"]))


def _fan(db: Db, sr: dict[str, Any], row: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fan (Technicals v2 T1): calibrated marginal band at every horizon 1..20, dated by exchange sessions, plus the
    expected earnings date when it falls inside the window."""
    info: dict[str, Any] = {"source": "none", "calendar": None, "horizons": len(S.FAN_HORIZONS), "earnings": None,
                            "monotone": True, "note": S.LABELS["fan"]}
    d0 = row.get("as_of_date")
    if not isinstance(d0, date_cls):
        return [], info
    days, calname = S.fan_sessions(d0, len(S.FAN_HORIZONS), sr.get("_mic"), row.get("region") or sr.get("ref_region"),
                                   get_settings())
    pts, src = S.fan_points(row, days)
    info.update(source=src, calendar=calname)
    if not pts:
        return [], info
    # earnings: the provider / company calendar when it has a date inside the window, else the envelope's own flag
    ev = None
    cal = _rows(db, """SELECT expected_date, session, date_source FROM earn_calendar
                       WHERE security_id = ? AND expected_date > ? ORDER BY expected_date LIMIT 1""", [sr["ref_security_id"], d0])
    if cal and cal[0]["expected_date"] <= days[-1]:
        ed = cal[0]["expected_date"]
        h = next(k + 1 for k, d in enumerate(days) if d >= ed)
        ev = {"date": iso(ed), "h": h, "session": cal[0].get("session"), "date_source": cal[0].get("date_source"),
              "source": "earnings_calendar"}
    elif row.get("earn_days_ahead") is not None and 0 < int(row["earn_days_ahead"]) <= len(days):
        h = int(row["earn_days_ahead"])
        ev = {"date": days[h - 1].isoformat(), "h": h, "session": None, "date_source": None, "source": "envelope"}
    info["earnings"] = ev
    out = []
    for p in pts:
        q = {k: (_r(v) if isinstance(v, float) else v) for k, v in p.items()}
        q["earnings"] = bool(ev and p["h"] == ev["h"])
        out.append(q)
    hw = [math.log(p["hi_90"] / p["lo_90"]) for p in pts]
    info["monotone"] = all(b >= a - 1e-12 for a, b in zip(hw, hw[1:], strict=False))
    return out, info


@router.get("/conformal/envelope/{security_id}", operation_id="getConformalEnvelope")
def get_envelope(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    row = _envelope_row(db, security_id)
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": None, "price_as_of": iso(sr.get("_px_date")), "stale": False,
        "last_close": None, "currency": sr.get("ref_currency") or None, "model_based": True, "filter": None,
        "radius_mode": None, "pool_key": None, "region": sr.get("ref_region"), "sigma_d": None,
        "earn_in_window": False, "earn_days_ahead": None, "q_pool_kind": None, "n_cal": None,
        "horizons": list(S.HORIZONS), "levels": list(S.LEVELS), "bands": [],
        "nextbar": {"experimental": True, "note": S.LABELS["nextbar"], "bands": []},
        "coverage": [], "labels": {k: S.LABELS[k] for k in ("calibrated_range", "model_based", "nextbar", "fan")},
        "fan": [], "fan_info": {"source": "none", "calendar": None, "horizons": len(S.FAN_HORIZONS), "earnings": None,
                                "monotone": True, "note": S.LABELS["fan"]},
        "validation_doc": S.VALIDATION_DOC, "data_status": "pending",
    }
    if row is None:
        out["data_status"] = _status(db, False, "conformal_envelopes")
        return out
    px = sr.get("_px_date")
    out.update({
        "as_of": iso(row["as_of_date"]), "stale": bool(px and row["as_of_date"] and px > row["as_of_date"]),
        "last_close": _r(row["last_close"]), "filter": row.get("filter"), "radius_mode": row.get("radius_mode"),
        "pool_key": row.get("pool_key"), "region": row.get("region") or out["region"], "sigma_d": _r(row.get("sigma_d")),
        "earn_in_window": bool(row.get("earn_in_window")),
        "earn_days_ahead": int(row["earn_days_ahead"]) if row.get("earn_days_ahead") is not None else None,
        "q_pool_kind": row.get("q_pool_kind"), "n_cal": int(row["q_n_cal"]) if row.get("q_n_cal") is not None else None,
        "bands": [{k: (_r(v) if isinstance(v, float) else v) for k, v in b.items()} for b in S.marginal_bands(row)],
        "coverage": _pool_coverage(db, row),
    })
    out["nextbar"]["bands"] = [{**b, "lo": _r(b["lo"]), "hi": _r(b["hi"])} for b in S.nextbar_bands(row)]
    out["fan"], out["fan_info"] = _fan(db, sr, row)
    out["data_status"] = "ok" if out["bands"] else "partial"
    return out


# ------------------------------------------------------------------------------------------------ touch


def _parse_levels(raw: list[str] | None) -> list[float]:
    out: list[float] = []
    for chunk in raw or []:
        for x in str(chunk).split(","):
            x = x.strip()
            if not x:
                continue
            try:
                v = float(x)
            except ValueError as e:
                raise invalid(f"levels: {x!r} is not a number") from e
            if not (v > 0 and math.isfinite(v)):
                raise invalid("levels must be positive prices")
            out.append(v)
    if len(out) > 50:
        raise invalid("at most 50 levels")
    return out


def _auto_levels(db: Db, security_id: str, last_close: float, max_levels: int) -> list[dict[str, Any]]:
    """Confluence zones (near edge) + still-valid technical levels nearest to the close."""
    out: list[dict[str, Any]] = []
    for z in _rows(db, "SELECT zone_id, side, zone_low, zone_high, rank, strength FROM zones WHERE security_id = ? "
                       "ORDER BY rank LIMIT 12", [security_id]):
        lo, hi = S.f(z["zone_low"]), S.f(z["zone_high"])
        if lo is None or hi is None:
            continue
        inside = lo <= last_close <= hi
        price = last_close if inside else (hi if hi < last_close else lo)
        out.append({"price": price, "source": "zone", "kind": z["side"], "zone_id": int(z["zone_id"]), "level_key": None,
                    "label": f"#{int(z['rank'])} {z['side']} zone {lo:.6g}-{hi:.6g}", "zone_low": lo, "zone_high": hi,
                    "inside": inside})
    for x in _rows(db, """SELECT level_key, level_type, timeframe, price, side FROM tech_levels
                          WHERE security_id = ? AND coalesce(still_valid, true) AND price > 0
                          ORDER BY abs(distance_atr) LIMIT ?""", [security_id, max_levels]):
        out.append({"price": S.f(x["price"]), "source": "level", "kind": x["side"], "zone_id": None,
                    "level_key": x["level_key"], "label": x["level_key"], "zone_low": None, "zone_high": None,
                    "inside": False})
    return [o for o in out if o["price"]]


@router.get("/conformal/touch", operation_id="getConformalTouch")
def get_touch(security_id: str, levels: list[str] | None = Query(None, description="Price levels (repeat or comma-separate); omitted = automatic (zones + technical levels)"),
              horizon: int = Query(20, ge=1, le=S.MAX_TOUCH_HORIZON), max_levels: int = Query(12, ge=1, le=40),
              db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = _sec(db, security_id)
    lv = _parse_levels(levels)
    row = _envelope_row(db, security_id)
    labels = {"touch": S.LABELS["touch"], "model_based": S.LABELS["model_based"]}
    if horizon not in S.HORIZONS:
        labels["touch_extrapolated"] = S.LABELS["touch_extrapolated"]
    out: dict[str, Any] = {"security": sec_ref(sr), "as_of": None, "last_close": None, "currency": sr.get("ref_currency") or None,
                           "horizon": horizon, "calibrated": horizon in S.HORIZONS, "extrapolated": horizon > 20,
                           "mode": "levels" if lv else "auto", "levels": [], "labels": labels,
                           "validation_doc": S.VALIDATION_DOC, "data_status": "pending"}
    if row is None or S.f(row.get("last_close")) is None:
        out["data_status"] = _status(db, False, "conformal_envelopes")
        return out
    lc = float(row["last_close"])
    out.update({"as_of": iso(row["as_of_date"]), "last_close": _r(lc)})
    idx = S.curve_index(_rows(db, "SELECT pool_key, horizon, side, kind, n, probs, u FROM cf_touch_curves "
                                  "WHERE region = ? OR pool_key = ?", [row.get("region"), row.get("pool_key")]))
    items = ([{"price": v, "source": "input", "kind": None, "zone_id": None, "level_key": None, "label": None,
               "zone_low": None, "zone_high": None, "inside": False} for v in lv]
             if lv else _auto_levels(db, security_id, lc, max_levels))
    res = []
    for it in items:
        t = S.touch(it["price"], row, horizon, idx)
        by_h = [{"horizon": h, "p_touch": _r(S.touch(it["price"], row, h, idx)["p_touch"], 4)} for h in S.HORIZONS]
        if it.get("inside"):
            t["p_touch"] = 1.0
            by_h = [{"horizon": h, "p_touch": 1.0} for h in S.HORIZONS]
        res.append({**{k: it[k] for k in ("source", "kind", "zone_id", "level_key", "label")},
                    "price": _r(it["price"]), "zone_low": _r(it.get("zone_low")), "zone_high": _r(it.get("zone_high")),
                    "inside": bool(it.get("inside")), "side": t["side"], "distance_pct": _r(t["distance_pct"]),
                    "u": _r(t["u"], 4), "p_touch": _r(t["p_touch"], 4), "by_horizon": by_h, "curve": t["curve"]})
    out["levels"] = res
    out["data_status"] = "ok" if (not res or any(r["p_touch"] is not None for r in res)) and idx else "partial"
    return out


# ------------------------------------------------------------------------------------------------ peer groups


def _ticker_map(db: Db, ids: list[str]) -> dict[str, dict[str, Any]]:
    ids = sorted({i for i in ids if i})
    if not ids:
        return {}
    rows = db.all(f"SELECT security_id, ticker, name FROM sec WHERE security_id IN ({', '.join('?' * len(ids))})", ids)
    return {r["security_id"]: r for r in rows}


def _ref(tk: dict[str, dict[str, Any]], sid: str | None) -> dict[str, Any] | None:
    if not sid:
        return None
    r = tk.get(sid) or {}
    return {"security_id": sid, "ticker": r.get("ticker") or sid, "name": r.get("name")}


def _summary(g: dict[str, Any], s: dict[str, Any] | None, tk: dict[str, dict[str, Any]]) -> dict[str, Any]:
    s = s or {}
    return {"group_id": g["group_id"], "label": g["label"], "kind": g["group_kind"], "region": g["region_bloc"],
            "n_members": int(g["n_members"] or 0), "sector_id": g.get("sector_id"), "subsector_id": g.get("subsector_id"),
            "as_of": iso(s.get("as_of_date")), "n_scored": int(s["n_members"]) if s.get("n_members") is not None else None,
            "score": _r(s.get("score"), 4), "score_pct": _r(s.get("score_pct"), 4),
            "anomaly_90": s.get("anomaly_90"), "anomaly_95": s.get("anomaly_95"), "group_move": _r(s.get("group_move")),
            "top_member": _ref(tk, s.get("top_member")), "top_share": _r(s.get("top_share"), 4),
            "breakaway_member": _ref(tk, s.get("breakaway_member")), "breakaway_z": _r(s.get("breakaway_z"), 3)}


@router.get("/conformal/peer-groups", operation_id="getConformalPeerGroups")
def get_peer_groups(security_id: str | None = None, group_id: str | None = None, region: str | None = None,
                    sector_id: str | None = None, subsector_id: str | None = None, kind: str | None = None,
                    anomalies_only: bool = False, limit: int = Query(60, ge=1, le=400),
                    history_days: int = Query(365, ge=5, le=366), db: Db = Depends(get_db)) -> dict[str, Any]:
    if kind and kind not in ("industry", "supply_chain"):
        raise invalid("kind must be industry or supply_chain")
    if security_id:
        _sec(db, security_id)
    # one row per group: label / kind / region + the modal sector of the members
    groups = _rows(db, """SELECT group_id, any_value(version) AS version, any_value(label) AS label,
                                 any_value(group_kind) AS group_kind, any_value(region_bloc) AS region_bloc,
                                 count(*) AS n_members, mode(sector_id) AS sector_id, mode(subsector_id) AS subsector_id,
                                 any_value(built_as_of) AS built_as_of,
                                 list(security_id) AS members FROM cf_peer_groups GROUP BY group_id""")
    out: dict[str, Any] = {"as_of": None, "version": None, "filter": None, "radius_mode": None, "groups": [],
                           "detail": None, "labels": {k: S.LABELS[k] for k in ("peer", "pair", "model_based")},
                           "validation_doc": S.VALIDATION_DOC, "data_status": "pending"}
    if not groups:
        out["data_status"] = _status(db, False, "conformal_peer_groups", "conformal_peer_scores")
        if group_id:
            raise not_found(f"unknown group_id {group_id!r}")
        return out
    latest = {r["group_id"]: r for r in _rows(db, """SELECT * FROM cf_peer_scores
        QUALIFY row_number() OVER (PARTITION BY group_id ORDER BY as_of_date DESC) = 1""")}
    out["version"] = groups[0]["version"]
    if latest:
        any_s = max(latest.values(), key=lambda r: r["as_of_date"])
        out.update({"as_of": iso(any_s["as_of_date"]), "filter": any_s.get("filter"), "radius_mode": any_s.get("radius_mode")})
    if group_id and group_id not in {g["group_id"] for g in groups}:
        raise not_found(f"unknown group_id {group_id!r}")
    sel = groups
    if security_id:
        sel = [g for g in sel if security_id in (g["members"] or [])]
    if region:
        sel = [g for g in sel if g["region_bloc"] == region.upper()]
    if sector_id:
        sel = [g for g in sel if g["sector_id"] == sector_id]
    if subsector_id:
        sel = [g for g in sel if g["subsector_id"] == subsector_id]
    if kind:
        sel = [g for g in sel if g["group_kind"] == kind]
    if anomalies_only:
        sel = [g for g in sel if (latest.get(g["group_id"]) or {}).get("anomaly_90")]
    sel = sorted(sel, key=lambda g: (-(S.f((latest.get(g["group_id"]) or {}).get("score_pct")) or -1.0), g["group_id"]))
    sel = sel[:limit]
    tk_ids = [x for g in sel for x in ((latest.get(g["group_id"]) or {}).get("top_member"),
                                         (latest.get(g["group_id"]) or {}).get("breakaway_member"))]
    focus = group_id
    if not focus and security_id and sel:
        # industry group first (the security's own subsector), then the supply-chain group it belongs to
        focus = sorted(sel, key=lambda g: (g["group_kind"] != "industry", g["group_id"]))[0]["group_id"]
    gmap = {g["group_id"]: g for g in groups}
    if focus:
        tk_ids += list(gmap[focus]["members"] or [])
    tk = _ticker_map(db, [x for x in tk_ids if x])
    out["groups"] = [_summary(g, latest.get(g["group_id"]), tk) for g in sel]
    if focus:
        out["detail"] = _detail(db, gmap[focus], latest.get(focus), tk, history_days, security_id)
    out["data_status"] = "ok" if latest else "partial"
    return out


def _detail(db: Db, g: dict[str, Any], s: dict[str, Any] | None, tk: dict[str, dict[str, Any]], history_days: int,
            focus_security: str | None) -> dict[str, Any]:
    gid = g["group_id"]
    defs = _rows(db, "SELECT * FROM cf_peer_groups WHERE group_id = ? ORDER BY member_rank", [gid])
    hist = _rows(db, f"""SELECT as_of_date, score, score_pct, q_90, q_95, anomaly_90, anomaly_95, group_move
                         FROM cf_peer_scores WHERE group_id = ?
                           AND as_of_date > (SELECT max(as_of_date) FROM cf_peer_scores) - INTERVAL {int(history_days)} DAY
                         ORDER BY as_of_date""", [gid])
    mem = _rows(db, "SELECT * FROM cf_peer_members WHERE group_id = ? ORDER BY as_of_date DESC, contrib DESC", [gid])
    last_d = mem[0]["as_of_date"] if mem else None
    today = [m for m in mem if m["as_of_date"] == last_d]
    s = s or {}
    members = []
    for m in today:
        r = tk.get(m["security_id"]) or {}
        members.append({"security_id": m["security_id"], "ticker": r.get("ticker") or m["security_id"], "name": r.get("name"),
                        "r": _r(m.get("r")), "z_marg": _r(m.get("z_marg"), 3), "z_cond": _r(m.get("z_cond"), 3),
                        "contrib": _r(m.get("contrib"), 4), "contrib_share": _r(m.get("contrib_share"), 4),
                        "is_breakaway": m["security_id"] == s.get("breakaway_member"),
                        "is_top": m["security_id"] == s.get("top_member"),
                        "sd_next": _r(math.sqrt(max((S.f(m.get("d_next")) or 0.0) + sum((S.f(m.get(f"l{k}_next")) or 0.0) ** 2
                                                                                    for k in range(1, 5)), 0.0)))})
    return {
        **_summary(g, s, tk),
        "definition": {"version": g["version"], "built_as_of": iso(g.get("built_as_of")), "kind": g["group_kind"],
                       "label": g["label"], "region": g["region_bloc"],
                       "method": ("industry: the most liquid names of one subsector in one listing region"
                                  if g["group_kind"] == "industry" else
                                  "supply chain: a hub, its disclosed customers / suppliers and statistical neighbours "
                                  "(same listing region)"),
                       "members": [{"security_id": d["security_id"], "ticker": (tk.get(d["security_id"]) or {}).get("ticker") or d["security_id"],
                                    "name": (tk.get(d["security_id"]) or {}).get("name"), "member_rank": int(d["member_rank"]),
                                    "sector_id": d.get("sector_id"), "subsector_id": d.get("subsector_id"),
                                    "n_neighbours": len(d.get("nbr_ids") or [])} for d in defs]},
        "history": [{"date": iso(h["as_of_date"]), "score": _r(h["score"], 4), "score_pct": _r(h["score_pct"], 4),
                     "q_90": _r(h["q_90"], 4), "q_95": _r(h["q_95"], 4), "anomaly_90": h.get("anomaly_90"),
                     "anomaly_95": h.get("anomaly_95"), "group_move": _r(h.get("group_move"))} for h in hist],
        "members": members, "members_as_of": iso(last_d), "q_next_90": _r(s.get("q_next_90"), 4),
        "q_next_95": _r(s.get("q_next_95"), 4), "q_90": _r(s.get("q_90"), 4), "q_95": _r(s.get("q_95"), 4),
        "focus_security_id": focus_security,
    }


# ------------------------------------------------------------------------------------------------ pair / basket band


def _parse_weights(raw: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        sid, _, w = part.rpartition(":")
        if not sid:
            raise invalid(f"weights: {part!r} must be SECURITY_ID:weight")
        try:
            out[sid] = out.get(sid, 0.0) + float(w)
        except ValueError as e:
            raise invalid(f"weights: {w!r} is not a number") from e
    if not out or not any(abs(v) > 0 for v in out.values()):
        raise invalid("weights: at least one non-zero weight")
    return out


@router.get("/conformal/pair-band", operation_id="getConformalPairBand")
def get_pair_band(group_id: str, weights: str | None = Query(None, description="SECURITY_ID:weight,... (basket / spread)"),
                  i: str | None = Query(None, description="pair leg 1 (long)"), j: str | None = Query(None, description="pair leg 2 (short)"),
                  beta: float | None = Query(None, description="hedge ratio for i - beta x j (default: minimum-variance beta under the predicted covariance)"),
                  db: Db = Depends(get_db)) -> dict[str, Any]:
    if not weights and not (i and j):
        raise invalid("give weights=SID:w,... or a pair i & j")
    if i and j and i == j:
        raise invalid("i and j must differ")
    g = _rows(db, "SELECT any_value(label) AS label FROM cf_peer_groups WHERE group_id = ?", [group_id])
    if not g or g[0]["label"] is None:
        raise not_found(f"unknown group_id {group_id!r}")
    mem = _rows(db, "SELECT * FROM cf_peer_members WHERE group_id = ? ORDER BY as_of_date DESC", [group_id])
    sc = {r["as_of_date"]: r for r in _rows(db, """SELECT as_of_date, q_next_90, q_next_95 FROM cf_peer_scores
                                                   WHERE group_id = ? ORDER BY as_of_date DESC LIMIT 5""", [group_id])}
    out: dict[str, Any] = {"group_id": group_id, "label": g[0]["label"], "as_of": None, "mode": "weights" if weights else "pair",
                           "weights": [], "beta": None, "mean": None, "sd": None, "bands": [], "realised": None, "marginal": None,
                           "units": "log return, next session", "labels": {"pair": S.LABELS["pair"], "model_based": S.LABELS["model_based"]},
                           "data_status": "pending"}
    if not mem:
        out["data_status"] = _status(db, False, "conformal_peer_members")
        return out
    dates = sorted({m["as_of_date"] for m in mem}, reverse=True)
    cur = [m for m in mem if m["as_of_date"] == dates[0]]
    ids, mu, d, L = S.law(cur)
    pos = {s: k for k, s in enumerate(ids)}
    if weights:
        w = _parse_weights(weights)
    else:
        assert i and j
        for x in (i, j):
            if x not in pos:
                raise invalid(f"{x!r} is not a scored member of {group_id}")
        b = beta if beta is not None else S.hedge_beta(pos[i], pos[j], d, L)
        out["beta"] = _r(b, 4)
        w = {i: 1.0, j: -b}
    miss = [s for s in w if s not in pos]
    if miss:
        raise invalid(f"not scored members of {group_id}: {', '.join(sorted(miss))}")
    a = np.zeros(len(ids))
    for s, v in w.items():
        a[pos[s]] = v
    m, var = S.combo_moments(a, mu, d, L)
    q = sc.get(dates[0]) or {}
    tk = _ticker_map(db, list(w))
    out.update({"as_of": iso(dates[0]), "mean": _r(m, 6), "sd": _r(math.sqrt(var), 6),
                "weights": [{"security_id": s, "ticker": (tk.get(s) or {}).get("ticker") or s, "weight": _r(v, 6)} for s, v in w.items()]})
    for lvl, qv in ((90, S.f(q.get("q_next_90"))), (95, S.f(q.get("q_next_95")))):
        bd = S.band(m, var, qv)
        if bd:
            out["bands"].append({"level": lvl, "lo": _r(bd[0]), "hi": _r(bd[1]), "lo_pct": _r(math.exp(bd[0]) - 1),
                                 "hi_pct": _r(math.exp(bd[1]) - 1), "q": _r(qv, 4)})
    # today's realised combination vs the band emitted at the previous session's close
    if len(dates) > 1:
        prev = [x for x in mem if x["as_of_date"] == dates[1]]
        pids, pmu, pd_, pL = S.law(prev)
        ppos = {s: k for k, s in enumerate(pids)}
        rr = {x["security_id"]: S.f(x.get("r")) for x in cur}
        if all(s in ppos and rr.get(s) is not None for s in w):
            pa = np.zeros(len(pids))
            for s, v in w.items():
                pa[ppos[s]] = v
            pm, pv = S.combo_moments(pa, pmu, pd_, pL)
            val = float(sum(v * (rr[s] or 0.0) for s, v in w.items()))
            pq = sc.get(dates[1]) or {}
            b90, b95 = S.band(pm, pv, S.f(pq.get("q_next_90"))), S.band(pm, pv, S.f(pq.get("q_next_95")))
            out["realised"] = {"date": iso(dates[0]), "value": _r(val), "band_as_of": iso(dates[1]),
                               "inside_90": None if b90 is None else bool(b90[0] <= val <= b90[1]),
                               "inside_95": None if b95 is None else bool(b95[0] <= val <= b95[1]),
                               "z": _r((val - pm) / math.sqrt(pv), 3) if pv > 0 else None}
    # marginal band for THIS combination: split-conformal quantile of its own past standardised moves
    by_date: dict[Any, list[dict[str, Any]]] = {}
    for x in mem:
        by_date.setdefault(x["as_of_date"], []).append(x)
    hist = S.combo_abs_z(by_date, w)
    calib = [z for t, z in hist if t != dates[0]]          # today's move is reported, not used to calibrate
    out["marginal"] = {"n_cal": len(calib), "window_start": iso(hist[0][0]) if hist else None, "bands": []}
    for lvl in (80, 90, 95):
        qz = S.conformal_abs(calib, lvl)
        if qz is None:
            continue
        lo, hi = m - qz * math.sqrt(var), m + qz * math.sqrt(var)
        out["marginal"]["bands"].append({"level": lvl, "lo": _r(lo), "hi": _r(hi), "lo_pct": _r(math.exp(lo) - 1),
                                         "hi_pct": _r(math.exp(hi) - 1), "q_abs_z": _r(qz, 4)})
    out["labels"]["marginal"] = S.LABELS["pair_marginal"]
    out["data_status"] = "ok" if out["bands"] else "partial"
    return out


# ------------------------------------------------------------------------------------------------ coverage


def _cov_row(r: dict[str, Any]) -> dict[str, Any]:
    return {"application": r["application"], "filter": r["filter"], "segment_type": r["segment_type"],
            "segment": r["segment"], "horizon": r["horizon"], "level": int(r["level"]), "target": _r(r["target"], 4),
            "coverage": _r(r["coverage"], 4), "gap_pp": _r(r["gap_pp"], 2), "n": int(r["n"] or 0), "status": r["status"],
            "as_of": iso(r["as_of_date"]), "window_start": iso(r["window_start"]), "window_end": iso(r["window_end"])}


def _production() -> dict[str, Any] | None:
    try:
        p = get_settings().data_dir / "state" / "conformal.json"
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return None
    prod = st.get("production")
    return prod if isinstance(prod, dict) else None


def _validation_md() -> str | None:
    try:
        p = get_settings().project_root / S.VALIDATION_DOC
        return p.read_text(encoding="utf-8") if p.exists() else None
    except OSError:
        return None


@router.get("/conformal/coverage", operation_id="getConformalCoverage")
def get_coverage(application: str | None = None, include_doc: bool = False, db: Db = Depends(get_db)) -> dict[str, Any]:
    if application and application not in COVERAGE_APPS:
        raise invalid(f"application must be one of {', '.join(COVERAGE_APPS)}")
    where, prm = "segment_type IN ('overall', 'region', 'group_kind')", []
    if application:
        where += " AND application = ?"
        prm.append(application)
    rows = _rows(db, f"SELECT * FROM cf_coverage WHERE {where} ORDER BY application, filter, segment_type, segment, "
                     "horizon, level", prm)
    flags = _rows(db, "SELECT * FROM cf_coverage WHERE status = 'under' AND n >= 2000 AND application NOT LIKE '%_wf' "
                      "AND (segment_type = 'overall' AND gap_pp < -1.5 OR gap_pp < -5) ORDER BY gap_pp LIMIT 50")
    as_of = max((r["as_of_date"] for r in rows if not str(r["application"]).endswith("_wf")), default=None)
    return {"as_of": iso(as_of), "rows": [_cov_row(r) for r in rows], "flags": [_cov_row(r) for r in flags],
            "gates": S.GATES, "production": _production(), "validation_doc": S.VALIDATION_DOC,
            "validation_md": _validation_md() if include_doc else None,
            "labels": {"model_based": S.LABELS["model_based"]},
            "data_status": "ok" if rows else _status(db, False, "conformal_coverage")}
