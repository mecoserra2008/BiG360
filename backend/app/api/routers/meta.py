"""Reference metadata (tag: meta): taxonomy, factors, metric registry, security search."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import kpi_sets, metric_meta, metric_public, sec_ref, sec_ref_sql
from app.api.serving import contract_yaml

router = APIRouter(tags=["meta"], route_class=FastRoute)


@router.get("/meta/taxonomy", operation_id="getTaxonomy")
def get_taxonomy() -> dict[str, Any]:
    tax = contract_yaml("sector_taxonomy.yaml")
    set_of = {sub: ks_id for ks_id, ks in kpi_sets().items() for sub in ks["subsectors"]}
    sectors = []
    for sec in tax["sectors"]:
        subs = [
            {"id": s["id"], "name": s["name"], "kpi_set_id": set_of.get(s["id"]), "groups": s.get("groups", [])}
            for s in tax["subsectors"]
            if s["sector"] == sec["id"]
        ]
        sectors.append({"id": sec["id"], "name": sec["name"], "subsectors": subs})
    return {"sectors": sectors}


@router.get("/meta/factors", operation_id="getFactorMeta")
def get_factor_meta() -> dict[str, Any]:
    sc = contract_yaml("scoring.yaml")
    factors = [
        {"id": f["id"], "name": f["name"], "description": f.get("description"), "members": f.get("members", {})}
        for f in sc["factors"]
    ]
    profiles = {k: {f: w for f, w in v.items() if not f.startswith("_")} for k, v in sc["composite"]["profiles"].items()}
    # coverage v3 A8: composites are descriptive; "signal" wording only behind a passed out-of-sample gate
    from app.analytics.composite_gate import validation_meta  # noqa: PLC0415

    return {"factors": factors, "profiles": profiles, "default_profile": sc["composite"]["default_profile"],
            "composite_validation": validation_meta(list(profiles), sc["composite"]["default_profile"])}


@router.get("/meta/metrics", operation_id="listMetricMeta")
def list_metric_meta() -> list[dict[str, Any]]:
    return [metric_public(m) for m in metric_meta()]


@router.get("/search", operation_id="searchSecurities")
def search_securities(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50),
                      db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    """Ranking: exact ticker/ISIN > ticker prefix > name prefix > word prefix > substring > fuzzy (Jaro-Winkler);
    ties broken by market cap."""
    term = q.strip().lower()
    if not term:
        return []
    like = "%" + term.replace("%", "").replace("_", "\\_") + "%"
    rows = db.all(f"""
        WITH p AS (SELECT ? AS t, ? AS lk),
        c AS (
            SELECT s.*, p.t, p.lk, lower(s.ticker) AS lt, lower(coalesce(s.name, '')) AS ln,
                   jaro_winkler_similarity(p.t, lower(coalesce(s.name, ''))) AS jw_name,
                   jaro_winkler_similarity(p.t, lower(coalesce(s.ticker, ''))) AS jw_tic
            FROM sec s, p)
        SELECT {sec_ref_sql('c')},
            CASE WHEN lt = t THEN 100
                 WHEN lower(coalesce(isin, '')) = t THEN 98
                 WHEN split_part(lt, '.', 1) = t OR replace(lt, '-', '.') = t THEN 95
                 WHEN starts_with(lt, t) THEN 90 - least(len(lt) - len(t), 10) * 0.5
                 WHEN starts_with(ln, t) THEN 80
                 WHEN starts_with(lower(coalesce(name_local, '')), t) THEN 78
                 WHEN contains(ln, ' ' || t) THEN 70
                 WHEN contains(search_text, t) THEN 55
                 ELSE 50 * greatest(jw_name, jw_tic) END AS score,
            coalesce(is_active, true) AS act
        FROM c
        WHERE search_text LIKE lk ESCAPE '\' OR jw_name >= 0.88 OR (len(t) >= 2 AND jw_tic >= 0.9)
        ORDER BY score DESC, act DESC, coalesce(is_primary_security, false) DESC, market_cap_usd DESC NULLS LAST, ticker
        LIMIT ?""", [term, like, limit])
    return [sec_ref(r) for r in rows]


# ------------------------------------------------------------------------------------------------ data availability
# (completeness audit 2026-10-27, docs/DATA_COMPLETENESS.md; reason codes app.core.availability)

#: fields the company availability answer covers by default (the Company 360 header / overview / key stats)
DEFAULT_AVAILABILITY_FIELDS = (
    "px_last", "mkt_cap_usd", "pe_ttm", "pe_fwd", "pb", "ev_ebitda", "ev_ebit", "fcf_yield", "div_yld", "roic", "croic",
    "gross_margin", "net_debt_to_ebitda", "beta_1y", "ml.rank_pct_3m", "moat", "insider_signal", "sentiment",
    "si.pct_float", "si.days", "own.inst_own_pct", "sm.score", "eps_surprise", "cf.touch_20d", "tech.hi52_pct",
    "sector", "isin", "employees")


@router.get("/meta/field-availability", operation_id="getFieldAvailability")
def get_field_availability(region: str | None = None, fields: list[str] | None = Query(None),
                           include_coverage: bool = False) -> dict[str, Any]:
    """Region-level reason of every documented field gap (contracts/field_availability.yaml). With `region`, each
    field carries that region's reason (or none when the field is normally available there)."""
    from app.core import availability as AV  # noqa: PLC0415

    doc = AV.load()
    rules = doc.get("rules") or {}

    def pub(rid: str | None) -> dict[str, Any] | None:
        r = rules.get(rid) if rid else None
        return AV.make(r["reason"], r.get("note"), None, r.get("fix")).public() if r else None

    want = set(fields or [])
    out: dict[str, Any] = {}
    for f, e in (doc.get("fields") or {}).items():
        if want and f not in want:
            continue
        regs = e.get("regions") or {}
        if region:
            r = regs.get(region)
            if not r:
                continue
            item: dict[str, Any] = {"reason": pub(r.get("rule")), "group": e.get("group")}
            if include_coverage:
                item["coverage_pct"] = r.get("pct")
            out[f] = item
            continue
        if not e.get("rule") and not regs:
            continue
        item = {"group": e.get("group"), "reason": pub(e.get("rule")),
                "regions": {rg: pub(v.get("rule")) for rg, v in regs.items()}}
        if include_coverage:
            item["coverage"] = {**(e.get("coverage") or {}), "regions": {rg: v.get("pct") for rg, v in regs.items()}}
        out[f] = item
    return {"version": int(doc.get("version") or 1), "generated_at": doc.get("generated_at"),
            "serving_run_id": doc.get("serving_run_id"), "region": region,
            "codes": [{"code": c, "sync_code": s, "label": lab, "message": m} for c, (s, lab, m) in AV.REASONS.items()],
            "areas": {k: AV.make(v["reason"], v.get("note")).public() for k, v in (doc.get("areas") or {}).items()},
            "aliases": doc.get("aliases") or {}, "fields": out}


def _region_px_date(db: Db, region: str | None) -> Any:
    if not region:
        return None
    return db.scalar("SELECT max(px_date) FROM sec WHERE region = ? AND coalesce(u_full, false)", [region])


@router.get("/companies/{security_id}/availability", operation_id="getCompanyAvailability")
def get_company_availability(security_id: str, fields: list[str] | None = Query(None), all_fields: bool = False,
                             db: Db = Depends(get_db)) -> dict[str, Any]:
    """Per-security reason for each requested field that is missing (all_fields=true: for every requested field):
    region rules of contracts/field_availability.yaml + this security's context (stale prices, fundamentals scope,
    plausibility flags)."""
    from app.api.routers._common import iso, not_found  # noqa: PLC0415
    from app.bql import fields as BF  # noqa: PLC0415
    from app.core import availability as AV  # noqa: PLC0415

    r = db.one("SELECT * FROM sec WHERE security_id = ?", [security_id])
    if r is None:
        raise not_found(f"unknown security_id {security_id!r}")
    flags = {row["metric_id"] for row in db.all(
        "SELECT metric_id FROM metric_latest WHERE issuer_id = ? AND metric_id IN ('roic_flag', 'croic_flag') AND value >= 1",
        [r.get("issuer_id")])} if r.get("issuer_id") else set()
    has_fund = r.get("fund_period_end") is not None
    in_scope = bool(r.get("u_mvp_us") or r.get("u_mvp_global") or r.get("pe_ttm") is not None
                    or r.get("m_roic") is not None)
    ctx = {"region": r.get("region"), "security_type": r.get("security_type"), "px_date": r.get("px_date"),
           "region_px_date": _region_px_date(db, r.get("region")), "has_fundamentals": has_fund,
           "in_fund_scope": in_scope, "flags": flags,
           # full-universe scores / ML (2026-10-30): the security's own explained gaps (scoring_full, ml_predict_full)
           "score_missing": r.get("score_missing"), "score_null": bool(r.get("cr_balanced")),
           "ml": {h: (r.get(f"ml_reason_{h}"), r.get(f"ml_detail_{h}")) for h in ("1M", "3M", "6M")
                  if r.get(f"ml_status_{h}") == "no_prediction"}}
    doc = AV.load()
    out: dict[str, Any] = {}
    for f in (fields or list(DEFAULT_AVAILABILITY_FIELDS)):
        fd = BF.resolve(f)
        val = None
        known = False
        if fd is not None and fd.table == "sec" and fd.column and "{" not in fd.column:
            val, known = r.get(fd.column), fd.column in r
        elif fd is not None and fd.table and fd.column and "{" not in fd.column and fd.key in ("security_id", "issuer_id"):
            key = security_id if fd.key == "security_id" else r.get("issuer_id")
            try:
                val = db.scalar(f'SELECT "{fd.column}" FROM "{fd.table}" WHERE "{fd.key}" = ? LIMIT 1', [key])
                known = True
            except Exception:  # noqa: BLE001 - table absent in this snapshot
                known = False
        if known and val is not None and not all_fields:
            continue
        reason = AV.field_reason(fd.mnemonic if fd else f, {**ctx, "group": fd.group if fd else None}, doc=doc)
        item = reason.public()
        item["field"] = fd.mnemonic if fd else f
        item["has_value"] = bool(known and val is not None)
        out[f] = item
    return {"security_id": security_id, "region": r.get("region"), "as_of": iso(db.as_of), "px_date": iso(r.get("px_date")),
            "has_fundamentals": has_fund, "reasons": out}
