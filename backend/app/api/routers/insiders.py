"""Insider Radar (tag: insiders): filing feed, top signals, cluster buys.

Cluster buys are islands of open-market purchases of one security with gaps <= 30 days between consecutive buys and
>= 2 distinct insiders (precomputed as insider_txn.cluster_id in the serving snapshot).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import fnum, iso, parse_regions, region_clause, regions_query, sec_ref, sec_ref_sql
from app.api.routers.companies import insider_signal_out, insider_txn_out, txn_anomaly_scores

router = APIRouter(tags=["insiders"], route_class=FastRoute)

TxnType = Literal["buy", "sell", "grant", "option_exercise", "tax_withholding", "gift", "conversion", "other"]
Role = Literal["ceo", "cfo", "coo", "chair", "president", "director", "officer_other", "owner", "other"]


@router.get("/insiders/feed", operation_id="getInsiderFeed")
def get_insider_feed(region: str | None = None, regions: list[str] | None = regions_query(), sector_id: str | None = None,
                     txn_type: TxnType | None = None, open_market_only: bool = True, min_value_usd: float = 100000,
                     role: Role | None = None, days: int = Query(30, ge=1, le=1825), limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0), db: Db = Depends(get_db)) -> dict[str, Any]:
    regs = parse_regions(region, regions)
    rc, params = region_clause(regs)
    src = "insider_feed" if days <= 400 else "insider_txn"
    conds = [rc, "t.knowledge_date > ?"]
    params.append(db.as_of - timedelta(days=days))
    if sector_id:
        conds.append("s.sector_id = ?")
        params.append(sector_id)
    if txn_type:
        conds.append("t.txn_type = ?")
        params.append(txn_type)
    if open_market_only:
        conds.append("t.is_open_market")
    if min_value_usd:
        conds.append("coalesce(t.value_usd, 0) >= ?")
        params.append(min_value_usd)
    if role:
        conds.append("t.role_norm = ?")
        params.append(role)
    rows = db.all(f"""SELECT {sec_ref_sql('s')}, s.insider_signal, t.*, count(*) OVER () AS _total
                      FROM {src} t JOIN sec s ON s.security_id = t.sec_id WHERE {' AND '.join(conds)}
                      ORDER BY t.knowledge_date DESC, t.value_usd DESC NULLS LAST, t.txn_id LIMIT ? OFFSET ?""",
                  params + [limit, offset])
    total = int(rows[0]["_total"]) if rows else int(db.scalar(
        f"SELECT count(*) FROM {src} t JOIN sec s ON s.security_id = t.sec_id WHERE {' AND '.join(conds)}", params) or 0)
    an = txn_anomaly_scores(db, sorted({r["ref_security_id"] for r in rows}))
    return {"total": total, "limit": limit, "offset": offset,
            "items": [{"security": sec_ref(r), "transaction": insider_txn_out(r, an), "signal_score": fnum(r.get("insider_signal"))}
                      for r in rows],
            "data_status": "ok" if db.snap.input_ok("insider_transactions") else "pending"}


@router.get("/insiders/top-signals", operation_id="getInsiderTopSignals")
def get_insider_top_signals(region: str | None = None, regions: list[str] | None = regions_query(),
                            sector_id: str | None = None, direction: Literal["bullish", "bearish"] = "bullish",
                            limit: int = Query(25, ge=1, le=200), db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    regs = parse_regions(region, regions)
    rc, params = region_clause(regs)
    conds = [rc, "i.signal_score IS NOT NULL", "i.signal_score > 50" if direction == "bullish" else "i.signal_score < 50"]
    if sector_id:
        conds.append("s.sector_id = ?")
        params.append(sector_id)
    order = "DESC" if direction == "bullish" else "ASC"
    rows = db.all(f"""SELECT {sec_ref_sql('s')}, s.c_balanced, i.* FROM insider_latest i JOIN sec s USING (security_id)
                      WHERE {' AND '.join(conds)} ORDER BY i.signal_score {order}, abs(coalesce(i.net_buy_value_usd_90d, 0)) DESC
                      LIMIT ?""", params + [limit])
    return [{"security": sec_ref(r), "signal": insider_signal_out(r), "composite": fnum(r.get("c_balanced"))} for r in rows]


@router.get("/insiders/clusters", operation_id="getInsiderClusters")
def get_insider_clusters(days: int = Query(30, ge=1, le=1825), region: str | None = None,
                         regions: list[str] | None = regions_query(), db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    regs = parse_regions(region, regions)
    rc, params = region_clause(regs)
    rows = db.all(f"""
        WITH c AS (SELECT cluster_id, sec_id, count(DISTINCT insider_id)::INT AS n_buyers, sum(coalesce(value_usd, 0)) AS total,
                          min(transaction_date) AS first_date, max(transaction_date) AS last_date,
                          list(DISTINCT insider_name) AS insiders
                   FROM insider_txn WHERE cluster_id IS NOT NULL AND txn_type = 'buy' AND is_open_market
                   GROUP BY 1, 2 HAVING max(transaction_date) > ?)
        SELECT {sec_ref_sql('s')}, c.* FROM c JOIN sec s ON s.security_id = c.sec_id
        WHERE {rc} AND c.n_buyers >= 2 ORDER BY c.last_date DESC, c.total DESC LIMIT 200""",
                  [db.as_of - timedelta(days=days)] + params)
    return [{"security": sec_ref(r), "n_buyers": r["n_buyers"], "total_value_usd": fnum(r["total"]) or 0.0,
             "first_date": iso(r["first_date"]), "last_date": iso(r["last_date"]), "insiders": sorted(r["insiders"] or []),
             "cluster_id": r["cluster_id"]} for r in rows]
