"""Sector Explorer (tag: sectors): sector/subsector summaries, KPI distributions, leaders, factor heatmap, scatter."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import (
    FACTORS,
    fnum,
    invalid,
    kpi_sets,
    metric_index,
    not_found,
    parse_regions,
    regions_query,
    rnd,
    screener_fields,
    taxonomy,
)
from app.api.routers.screener import ScreenerQuery, run_query, screener_row

router = APIRouter(tags=["sectors"], route_class=FastRoute)

Size = Literal["large", "mid", "small"]
VAL_COLS = ("pe_ttm", "ev_ebitda", "ev_sales", "pb", "fcf_yield", "dividend_yield", "earnings_yield")


def _scope(regs: list[str], size: str | None, sector_id: str | None = None, subsector_id: str | None = None) -> tuple[str, list[Any]]:
    conds, params = ["coalesce(is_active, true)"], []
    if regs:
        conds.append(f"region IN ({', '.join('?' for _ in regs)})")
        params += regs
    if size:
        conds.append("size_bucket = ?")
        params.append(size)
    if sector_id:
        conds.append("sector_id = ?")
        params.append(sector_id)
    if subsector_id:
        conds.append("subsector_id = ?")
        params.append(subsector_id)
    return " AND ".join(conds), params


def _wavg(col: str) -> str:
    return (f"coalesce(sum({col} * market_cap_usd) FILTER ({col} IS NOT NULL AND market_cap_usd > 0) / "
            f"NULLIF(sum(market_cap_usd) FILTER ({col} IS NOT NULL AND market_cap_usd > 0), 0), avg({col}))")


def _summaries(db: Db, where: str, params: list[Any]) -> dict[tuple[str, str | None], dict[str, Any]]:
    fsel = ", ".join(f"median(f_{f}) AS mf_{f}" for f in FACTORS)
    vsel = ", ".join(f"median({c}) FILTER ({c} > 0 OR '{c}' IN ('fcf_yield', 'dividend_yield', 'earnings_yield')) AS mv_{c}"
                     for c in VAL_COLS)
    rows = db.all(f"""SELECT sector_id, subsector_id, count(*)::INT AS n, sum(market_cap_usd) AS mcap,
                             {_wavg('chg_1m')} AS r1m, {_wavg('chg_ytd')} AS rytd, median(c_balanced) AS mc, {fsel}, {vsel}
                      FROM sec WHERE {where} AND sector_id IS NOT NULL
                      GROUP BY GROUPING SETS ((sector_id), (sector_id, subsector_id))""", params)
    return {(r["sector_id"], r["subsector_id"]): r for r in rows}


def _summary(sid: str, sub: str | None, r: dict[str, Any] | None, name: str) -> dict[str, Any]:
    r = r or {}
    return {"sector_id": sid, "subsector_id": sub, "name": name, "n_securities": int(r.get("n") or 0),
            "market_cap_usd": fnum(r.get("mcap")), "ret_1m": rnd(r.get("r1m")), "ret_ytd": rnd(r.get("rytd")),
            "median_factor_scores": {f: rnd(r.get(f"mf_{f}"), 4) for f in FACTORS},
            "median_valuation": {c: rnd(r.get(f"mv_{c}"), 6) for c in VAL_COLS},
            "median_composite": rnd(r.get("mc"), 4)}


def _tree(sums: dict[tuple[str, str | None], dict[str, Any]], only: str | None = None) -> list[dict[str, Any]]:
    tax = taxonomy()
    out = []
    for sid in tax["order"]:
        if only and sid != only:
            continue
        s = _summary(sid, None, sums.get((sid, None)), tax["sectors"][sid])
        s["subsectors"] = [_summary(sid, sub, sums.get((sid, sub)), meta["name"])
                           for sub, meta in tax["subsectors"].items() if meta["sector"] == sid]
        out.append(s)
    return out


@router.get("/sectors", operation_id="listSectors")
def list_sectors(region: str | None = None, regions: list[str] | None = regions_query(), size_bucket: Size | None = None,
                 db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    where, params = _scope(parse_regions(region, regions), size_bucket)
    return _tree(_summaries(db, where, params))


@router.get("/sectors/{sector_id}", operation_id="getSector")
def get_sector(sector_id: str, subsector_id: str | None = None, region: str | None = None,
               regions: list[str] | None = regions_query(), size_bucket: Size | None = None,
               db: Db = Depends(get_db)) -> dict[str, Any]:
    tax = taxonomy()
    if sector_id not in tax["sectors"]:
        raise not_found(f"unknown sector_id {sector_id!r}")
    if subsector_id and tax["subsectors"].get(subsector_id, {}).get("sector") != sector_id:
        raise not_found(f"subsector {subsector_id!r} is not in sector {sector_id!r}")
    regs = parse_regions(region, regions)
    where, params = _scope(regs, size_bucket, sector_id)
    sums = _summaries(db, where, params)
    summary = _tree(sums, sector_id)[0]
    if subsector_id:
        sub = next(s for s in summary["subsectors"] if s["subsector_id"] == subsector_id)
        summary = {**sub, "subsectors": []}
    subs = [subsector_id] if subsector_id else [k for k, v in tax["subsectors"].items() if v["sector"] == sector_id]
    sets = [ks_id for ks_id, ks in kpi_sets().items() if set(ks["subsectors"]) & set(subs)]
    if not subsector_id:
        # sector view: the KPI set covering the most companies first (it drives the badge and the 24-card cap)
        n_of = {s["subsector_id"]: s.get("n_securities") or 0 for s in summary.get("subsectors") or []}
        sets.sort(key=lambda k: -sum(n_of.get(x, 0) for x in set(kpi_sets()[k]["subsectors"]) & set(subs)))
    mi = metric_index()
    mids: list[str] = []
    for ks_id in sets:
        ks = kpi_sets()[ks_id]
        for m in list(ks.get("peer_metrics") or []) + [k["id"] for k in ks.get("kpis") or []]:
            if m in mi and m not in mids:
                mids.append(m)
    mids = mids[:24]
    swhere, sparams = _scope(regs, size_bucket, sector_id, subsector_id)
    dists = []
    if mids:
        qsel = ", ".join(f'quantile_cont("{mi[m]["column"]}", [0.1, 0.25, 0.5, 0.75, 0.9]) AS "q_{m}", '
                         f'count("{mi[m]["column"]}")::INT AS "n_{m}"' for m in mids)
        q = db.one(f"SELECT {qsel} FROM sec WHERE {swhere}", sparams) or {}
        vsel = ", ".join(f'"{mi[m]["column"]}" AS "v_{m}"' for m in mids)
        vals = db.all(f"SELECT security_id, ticker, {vsel} FROM sec WHERE {swhere} ORDER BY market_cap_usd DESC NULLS LAST LIMIT 500",
                      sparams)
        for m in mids:
            qs = q.get(f"q_{m}") or [None] * 5
            pts = sorted(({"security_id": v["security_id"], "ticker": v["ticker"], "value": fnum(v[f"v_{m}"])}
                          for v in vals if fnum(v[f"v_{m}"]) is not None), key=lambda x: -x["value"])
            meta = mi[m]
            dists.append({"metric_id": m, "label": meta["label"], "unit": meta["unit"], "higher_is_better": meta["higher_is_better"],
                          "target": fnum(meta.get("target")),
                          "n": int(q.get(f"n_{m}") or 0), "p10": rnd(qs[0]), "p25": rnd(qs[1]), "p50": rnd(qs[2]),
                          "p75": rnd(qs[3]), "p90": rnd(qs[4]), "values": pts})
    sq = ScreenerQuery(sector_ids=[sector_id], subsector_ids=[subsector_id] if subsector_id else [], regions=regs,
                       size_buckets=[size_bucket] if size_bucket else [], limit=10, sort_by="composite", sort_dir="desc",
                       filters=[{"field": "composite", "op": "not_null"}])
    lead = run_query(db, sq)
    lag = run_query(db, sq.model_copy(update={"sort_dir": "asc"}))
    heat = db.all(f"""SELECT subsector_id, {', '.join(f'median(f_{f}) AS f_{f}' for f in FACTORS)} FROM sec
                      WHERE {where} AND subsector_id IS NOT NULL GROUP BY 1 ORDER BY 1""", params)
    rs = db.all(f"""WITH r AS (SELECT issuer_id, arg_max(revenue_usd, fiscal_year) AS rev FROM rev_share
                               WHERE sector_id = ? {'AND subsector_id = ?' if subsector_id else ''} GROUP BY 1),
                    s AS (SELECT issuer_id, security_id, ticker FROM sec
                          QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY is_primary_security DESC,
                                                     market_cap_usd DESC NULLS LAST) = 1)
                    SELECT s.security_id, s.ticker, r.rev / sum(r.rev) OVER () AS share FROM r JOIN s USING (issuer_id)
                    ORDER BY share DESC LIMIT 15""", [sector_id] + ([subsector_id] if subsector_id else []))
    return {
        "summary": summary, "kpi_set_ids": sets, "kpi_distributions": dists,
        "leaders": [screener_row(r) for r in lead["rows"]], "laggards": [screener_row(r) for r in lag["rows"]],
        "factor_heatmap": [{"subsector_id": h["subsector_id"], "factor_id": f, "median_score": rnd(h[f"f_{f}"], 4)}
                           for h in heat for f in FACTORS],
        "revenue_share": [{"security_id": r["security_id"], "ticker": r["ticker"], "share": rnd(r["share"], 6) or 0.0} for r in rs],
        "data_status": "ok" if summary["n_securities"] else ("pending" if not db.snap.input_ok("securities_master") else "ok"),
    }


def _axis_sql(fid: str) -> str:
    if fid == "composite":
        return "c_balanced"
    f = screener_fields().get(fid)
    if f is None or f["type"] != "number" or f["sql"] in ("composite", "rank"):
        mi = metric_index().get(fid)
        if mi is None:
            raise invalid(f"unknown scatter axis {fid!r} (metric id, factor id or numeric screener field)")
        return f'"{mi["column"]}"'
    return f'"{f["sql"]}"'


@router.get("/sectors/{sector_id}/scatter", operation_id="getSectorScatter")
def get_sector_scatter(sector_id: str, x: str = Query(...), y: str = Query(...), subsector_id: str | None = None,
                       region: str | None = None, regions: list[str] | None = regions_query(),
                       db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    if sector_id not in taxonomy()["sectors"]:
        raise not_found(f"unknown sector_id {sector_id!r}")
    xs, ys = _axis_sql(x), _axis_sql(y)
    where, params = _scope(parse_regions(region, regions), None, sector_id, subsector_id)
    rows = db.all(f"""SELECT security_id, ticker, name, subsector_id, {xs} AS x, {ys} AS y, market_cap_usd FROM sec
                      WHERE {where} AND ({xs} IS NOT NULL OR {ys} IS NOT NULL)
                      ORDER BY market_cap_usd DESC NULLS LAST LIMIT 2000""", params)
    return [{"security_id": r["security_id"], "ticker": r["ticker"] or r["security_id"], "name": r["name"] or r["ticker"] or "",
             "subsector_id": r["subsector_id"], "x": fnum(r["x"]), "y": fnum(r["y"]), "market_cap_usd": fnum(r["market_cap_usd"])}
            for r in rows]
