"""Technicals helpers (owner: api): tech_setups -> TradeSetup/indicators, level families -> frontend LevelType,
market-structure / swing pivots, per-type reliability from level_type_stats."""

from __future__ import annotations

from typing import Any

from app.api.routers._common import fnum, iso, jload, rnd


def frontend_types() -> dict[str, str]:
    try:
        from app.technicals.taxonomy import FRONTEND_TYPE_OF_FAMILY

        return dict(FRONTEND_TYPE_OF_FAMILY)
    except Exception:  # pragma: no cover
        return {}


_PREFIX_FAMILY = (("swing_", "swing_pivot"), ("ms_", "market_structure"), ("fibp_", "fib_pivot"), ("fib_", "fibonacci"),
                  ("avwap", "anchored_vwap"), ("floor_", "floor_pivot"), ("cam_", "camarilla"), ("prev_", "period_ohlc"),
                  ("cur_", "period_ohlc"), ("or_", "opening_range"), ("round", "round_number"), ("vp_", "volume_profile"),
                  ("sma_", "moving_average"), ("gap", "gap"))


def family_of(level_type: str | None, family: str | None) -> str | None:
    if family:
        return family
    lt = level_type or ""
    for pre, fam in _PREFIX_FAMILY:
        if lt.startswith(pre):
            return fam
    return "extreme" if lt.endswith(("_52w", "all_time_high")) else None


def setup_out(s: dict[str, Any] | None) -> dict[str, Any] | None:
    if not s:
        return None
    comps = jload(s.get("score_components"), {}) or {}
    return {
        "as_of": iso(s["as_of_date"]), "bias": s.get("bias"), "entry_low": fnum(s.get("entry_low")),
        "entry_high": fnum(s.get("entry_high")), "entry": fnum(s.get("entry")), "invalidation": fnum(s.get("invalidation")),
        "target1": fnum(s.get("target1")), "target2": fnum(s.get("target2")), "reward_risk": fnum(s.get("reward_risk")),
        "support_zone_id": s.get("support_zone_id"), "resistance_zone_id": s.get("resistance_zone_id"),
        "dist_support_atr": fnum(s.get("dist_support_atr")), "dist_resistance_atr": fnum(s.get("dist_resistance_atr")),
        "technical_score": fnum(s.get("technical_score")),
        "score_components": {k: fnum(s.get(f"score_{k}")) for k in ("trend", "structure", "location", "rr", "momentum")},
        "score_weights": {k: fnum(v) for k, v in (comps.get("weights") or {}).items()} if isinstance(comps, dict) else {},
        "trend": s.get("trend"), "structure_state": s.get("structure_state"),
        "last_break": None if not s.get("last_break_kind") else {
            "kind": s["last_break_kind"], "direction": s.get("last_break_dir"), "date": iso(s.get("last_break_date")),
            "price": fnum(s.get("last_break_price"))},
        "note": s.get("note"),
    }


def indicators_out(s: dict[str, Any] | None) -> dict[str, Any] | None:
    if not s:
        return None
    return {"rsi14": fnum(s.get("rsi14")), "sma50": fnum(s.get("sma50")), "sma200": fnum(s.get("sma200")),
            "atr14": fnum(s.get("atr14")), "trend": s.get("trend"), "structure_state": s.get("structure_state")}


def level_out(x: dict[str, Any], ft: dict[str, str]) -> dict[str, Any]:
    fam = family_of(x["level_type"], x.get("family"))
    return {"level_key": x["level_key"], "level_type": x["level_type"], "timeframe": x["timeframe"],
            "price": fnum(x["price"]) or 0.0, "anchor_date": iso(x["anchor_date"]), "side": x["side"],
            "distance_atr": fnum(x["distance_atr"]) or 0.0, "distance_pct": fnum(x["distance_pct"]) or 0.0,
            "touches": x.get("touches"), "type_rank_score": fnum(x.get("type_rank_score")), "family": fam,
            "frontend_type": ft.get(fam or ""), "structure_label": x.get("structure_label"), "still_valid": x.get("still_valid"),
            "weight": fnum(x.get("weight")), "known_from": iso(x.get("known_from")), "last_touch": iso(x.get("last_touch"))}


def pivot_out(x: dict[str, Any]) -> dict[str, Any]:
    return {"date": iso(x["anchor_date"]), "price": fnum(x["price"]) or 0.0, "kind": x["level_type"],
            "label": x.get("structure_label"), "side": x["side"], "timeframe": x["timeframe"]}


def stats_and_reliability(stats: list[dict[str, Any]], ft: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rel: dict[str, dict[str, float]] = {}
    out = []
    for x in stats:
        fam = family_of(x.get("level_type") or x["level_key"].split("@")[0], x.get("family"))
        typ = ft.get(fam or "")
        n = int(x["n_events"] or 0)
        out.append({"level_key": x["level_key"], "n_events": n, "hit_rate": fnum(x.get("hit_rate")),
                    "hit_rate_shrunk": fnum(x.get("hit_rate_shrunk")), "mean_reaction_atr": fnum(x.get("mean_reaction_atr")),
                    "break_rate": fnum(x.get("break_rate")), "expectancy_r": fnum(x.get("expectancy_r")), "rank": x.get("rank"),
                    "family": fam, "frontend_type": typ, "edge": fnum(x.get("edge")),
                    "hit_rate_support": fnum(x.get("hit_rate_support")), "hit_rate_resistance": fnum(x.get("hit_rate_resistance")),
                    "control_hit_rate_shrunk": fnum(x.get("control_hit_rate_shrunk"))})
        if typ and n:
            a = rel.setdefault(typ, {"n": 0, "hr": 0.0, "nh": 0, "edge": 0.0, "ne": 0})
            a["n"] += n
            hr, ed = fnum(x.get("hit_rate_shrunk")), fnum(x.get("edge"))
            if hr is not None:
                a["hr"] += n * hr
                a["nh"] += n
            if ed is not None:
                a["edge"] += n * ed
                a["ne"] += n
    reliability = [{"frontend_type": k, "n_events": int(v["n"]),
                    "hit_rate_shrunk": rnd(v["hr"] / v["nh"], 4) if v["nh"] else None,
                    "edge": rnd(v["edge"] / v["ne"], 4) if v["ne"] else None} for k, v in sorted(rel.items())]
    return out, reliability
