"""Market overview (tag: market): benchmark indices, sector performance (cap-weighted), breadth, movers, macro regime,
key macro series and sovereign yield curves. One call per region set; heavy -> cached by the snapshot cache."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api import data_integrity as DI
from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import MACRO_SCOPE, equity_only_sql, fnum, iso, parse_regions, region_clause, regions_query, rnd, sec_ref, sec_ref_sql, taxonomy
from app.api.routers.companies import latest_regime, regime_snapshot
from app.api.routers.macro import REGION_COUNTRIES_EXTRA, REGION_COUNTRY, series_summaries, yield_curves

router = APIRouter(tags=["market"], route_class=FastRoute)

_FALLBACK_BENCHMARKS = [("^GSPC", "S&P 500", "US"), ("^STOXX50E", "EURO STOXX 50", "EU"), ("^N225", "Nikkei 225", "JP"),
                        ("^KS11", "KOSPI Composite", "KR"), ("^HSI", "Hang Seng", "HK"), ("000300.SS", "CSI 300", "CN"),
                        ("^BVSP", "Ibovespa", "LATAM"), ("^FTSE", "FTSE 100", "UK")]


def benchmarks() -> list[tuple[str, str, str, str]]:
    """(security_id, yf symbol, name, region) for index benchmarks."""
    try:
        from app.normalize.prices import BENCHMARKS

        return [(b.security_id, b.yf_ticker, b.name, b.region) for b in BENCHMARKS if b.kind == "index"]
    except Exception:
        return [(f"IDX:{t}", t, n, r) for t, n, r in _FALLBACK_BENCHMARKS]


def _wavg(col: str) -> str:
    return (f"coalesce(sum({col} * market_cap_usd) FILTER ({col} IS NOT NULL AND market_cap_usd > 0) / "
            f"NULLIF(sum(market_cap_usd) FILTER ({col} IS NOT NULL AND market_cap_usd > 0), 0), avg({col}))")


@router.get("/market/overview", operation_id="getMarketOverview")
def get_market_overview(region: str | None = None, regions: list[str] | None = regions_query(),
                        liquid: bool = True, min_adv_usd: float = Query(5e6, ge=0),
                        min_mcap_usd: float = Query(1e9, ge=0), min_price_usd: float = Query(2.0, ge=0),
                        unknown_mcap_adv_multiple: float = Query(4.0, ge=1), movers_limit: int = Query(25, ge=1, le=100),
                        db: Db = Depends(get_db)) -> dict[str, Any]:
    regs = parse_regions(region, regions)
    # O2.2a / F06: explicit data-as-of per region; 1D aggregates only use names on their region's latest session
    asof_items = DI.region_data_as_of(db, regs or None)
    amap = DI.region_asof_map(asof_items)
    if amap:
        av = ", ".join("(?, CAST(? AS DATE))" for _ in amap)
        ap: list[Any] = [x for k, v in amap.items() for x in (k, v)]
        acte = f"WITH __a(region, d) AS (VALUES {av}), x AS (SELECT s.*, CASE WHEN s.px_date >= __a.d THEN s.chg_1d END AS chg_1d_al, __a.d AS region_as_of FROM sec s LEFT JOIN __a USING (region))"
    else:
        ap = []
        acte = "WITH x AS (SELECT s.*, s.chg_1d AS chg_1d_al, CAST(NULL AS DATE) AS region_as_of FROM sec s)"
    bms = [b for b in benchmarks() if not regs or b[3] in regs]
    ids = [b[0] for b in bms]
    ph = ", ".join("?" for _ in ids) or "NULL"
    st = {r["security_id"]: r for r in db.all(f"SELECT * FROM px_stats WHERE security_id IN ({ph})", ids)}
    sp: dict[str, list[tuple[Any, float]]] = {}
    for r in db.all(f"""SELECT security_id, date, close FROM (
                            SELECT security_id, date, close, row_number() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                            FROM prices WHERE security_id IN ({ph})) WHERE rn <= 60 ORDER BY security_id, date""", ids):
        sp.setdefault(r["security_id"], []).append((r["date"], fnum(r["close"])))
    indices = []
    for sid, sym, name, reg in bms:
        s = st.get(sid)
        if s is None:
            continue
        pts = [(d, v) for d, v in sp.get(sid, []) if v is not None]
        indices.append({"id": sym, "name": name, "region": reg, "last": fnum(s.get("close")), "chg_1d": fnum(s.get("chg_1d")),
                        "chg_1m": fnum(s.get("chg_1m")), "chg_ytd": fnum(s.get("chg_ytd")), "currency": s.get("px_currency"),
                        "as_of": iso(s.get("px_date")),
                        "spark": [v for _, v in pts], "spark_points": [{"date": iso(d), "value": v} for d, v in pts]})
    rc, rp = region_clause(regs, "region")
    tax = taxonomy()
    sect = {r["sector_id"]: r for r in db.all(f"""{acte}
        SELECT sector_id, count(*)::INT AS n, sum(market_cap_usd) AS mcap, {_wavg('chg_1d_al')} AS r1d, {_wavg('chg_1m')} AS r1m,
               {_wavg('chg_3m')} AS r3m, {_wavg('chg_ytd')} AS rytd, median(c_balanced) AS mc,
               count(chg_1d_al)::INT AS n1d, min(region_as_of) AS d0, max(region_as_of) AS d1
        FROM x WHERE sector_id IS NOT NULL AND close IS NOT NULL AND {rc} GROUP BY 1""", ap + rp)}
    sector_perf = [{"sector_id": sid, "name": tax["sectors"][sid], "ret_1d": rnd(sect[sid]["r1d"]), "ret_1m": rnd(sect[sid]["r1m"]),
                    "ret_3m": rnd(sect[sid]["r3m"]), "ret_ytd": rnd(sect[sid]["rytd"]), "median_composite": rnd(sect[sid]["mc"], 4),
                    "market_cap_usd": fnum(sect[sid]["mcap"]), "n_securities": sect[sid]["n"],
                    "n_ret_1d": sect[sid]["n1d"],
                    "as_of": iso(sect[sid]["d1"]) if sect[sid]["d0"] == sect[sid]["d1"] else None,
                    "as_of_by_region": {k: iso(v) for k, v in amap.items() if not regs or k in regs}}
                   for sid in tax["order"] if sid in sect]
    br = db.one(f"""SELECT avg(CASE WHEN above_200d THEN 1.0 WHEN above_200d = false THEN 0.0 END) AS p200,
                           avg(CASE WHEN above_50d THEN 1.0 WHEN above_50d = false THEN 0.0 END) AS p50,
                           count(*) FILTER (chg_1d > 0)::INT AS adv, count(*) FILTER (chg_1d < 0)::INT AS dec,
                           count(*) FILTER (new_high_52w)::INT AS nh, count(*) FILTER (new_low_52w)::INT AS nl
                    FROM sec s WHERE px_date >= ? AND {equity_only_sql("s")} AND {rc}""", [db.as_of - timedelta(days=5)] + rp) or {}
    breadth = {"pct_above_200d": rnd(br.get("p200"), 4), "pct_above_50d": rnd(br.get("p50"), 4),
               "advancers": br.get("adv") or 0, "decliners": br.get("dec") or 0,
               "new_highs_52w": br.get("nh") or 0, "new_lows_52w": br.get("nl") or 0,
               "as_of_by_region": {k: iso(v) for k, v in amap.items() if not regs or k in regs}}
    # F21 (v1.30): liquid movers by default - KNOWN 20d ADV >= min_adv_usd, USD price >= min_price_usd and market cap
    # >= min_mcap_usd; a name without a known market cap (most JP / CN / KR listings lack share counts) qualifies only
    # with ADV >= unknown_mcap_adv_multiple x min_adv_usd. Unknown ADV never passes. liquid=false = legacy screen
    # (ADV >= $1m, unknown ADV passes).
    if liquid:
        mfilt = ("s.adv_usd_20d >= ? AND coalesce(s.close_usd, s.close * s.fx_to_usd) >= ? "
                 "AND (s.market_cap_usd >= ? OR (s.market_cap_usd IS NULL AND s.adv_usd_20d >= ?))")
        mfp: list[Any] = [min_adv_usd, min_price_usd, min_mcap_usd, min_adv_usd * unknown_mcap_adv_multiple]
    else:
        mfilt, mfp = "coalesce(s.adv_usd_20d, 1e7) >= 1e6", []
    movers = f"""{acte}
                 SELECT {sec_ref_sql('s')}, s.close, s.chg_1d_al AS chg_1d, s.volume_z, s.px_date, s.adv_usd_20d FROM x s
                 WHERE s.chg_1d_al IS NOT NULL AND s.px_date >= ? AND {mfilt}
                   AND {equity_only_sql("s")} AND {rc.replace('region', 's.region')}"""
    mp = ap + [db.as_of - timedelta(days=5)] + mfp + rp

    def mrow(r: dict[str, Any]) -> dict[str, Any]:
        return {"security": sec_ref(r), "close": fnum(r["close"]), "chg_1d": fnum(r["chg_1d"]) or 0.0,
                "volume_z": rnd(r["volume_z"], 3), "px_date": iso(r.get("px_date")),
                "adv_usd_20d": fnum(r.get("adv_usd_20d"))}

    gainers = [mrow(r) for r in db.all(movers + " ORDER BY chg_1d DESC LIMIT ?", mp + [movers_limit])]
    losers = [mrow(r) for r in db.all(movers + " ORDER BY chg_1d ASC LIMIT ?", mp + [movers_limit])]
    movers_filter = {"liquid": liquid, "min_adv_usd": min_adv_usd if liquid else 1e6,
                     "min_mcap_usd": min_mcap_usd if liquid else None, "min_price_usd": min_price_usd if liquid else None,
                     "unknown_mcap_min_adv_usd": min_adv_usd * unknown_mcap_adv_multiple if liquid else None,
                     "unknown_adv_passes": not liquid, "limit": movers_limit}
    scope = "US" if not regs or "US" in regs else MACRO_SCOPE.get(regs[0], regs[0])
    rinfo = DI.regime_scope_info(db, scope)  # F07: own model, labelled GLOBAL fallback, or not modelled
    regime = regime_snapshot(latest_regime(db, rinfo["scope"])) if rinfo["scope"] else None
    if regime is not None:
        regime.update({k: rinfo[k] for k in ("scope", "requested_scope", "modelled", "fallback", "model_id")})
    countries = [REGION_COUNTRY[r] for r in (regs or ["US"])]
    metas: list[dict[str, Any]] = []
    for c in countries:
        metas += db.all("""SELECT * FROM macro_series WHERE is_core AND (upper(country) = ? OR (? = 'US' AND country IS NULL))
                           ORDER BY category, series_id LIMIT 12""", [c, c])
    seen = set(countries)
    for r in regs:  # macro-coverage: headline series of the region's other countries (additive)
        for c in REGION_COUNTRIES_EXTRA.get(r, []):
            if c not in seen:
                seen.add(c)
                metas += db.all("""SELECT * FROM macro_series WHERE is_core AND upper(country) = ?
                                   ORDER BY category, series_id LIMIT 4""", [c])
    status = db.snap.status("prices_daily", "macro_observations")
    curves = yield_curves(db, regs)
    return {"as_of": iso(db.as_of), "region": ",".join(regs) if regs else None, "indices": indices,
            "data_as_of": asof_items, **DI.alignment_summary(asof_items), "regime_scope": rinfo,
            "credit_spreads": DI.credit_spreads(db, regs or None),
            "sector_performance": sector_perf, "breadth": breadth, "gainers": gainers, "losers": losers,
            "movers_filter": movers_filter, "regime": regime,
            "key_macro": series_summaries(db, metas), "yield_curves": curves, "data_status": status}
