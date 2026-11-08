"""Global events (tag: events): GDELT feed, country map, timeline, company attention.

`action_country` is ISO 3166-1 alpha-2 (GDELT's FIPS 10-4 geo codes are converted at ingest by
app.ingestion.gdelt_events; values that still look like FIPS-only codes are mapped here defensively).
`headline` is derived from the source URL slug (GDELT events carry no title).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import fnum, iso, parse_regions, region_clause, regions_query, rnd, sec_ref, sec_ref_sql

router = APIRouter(tags=["events"], route_class=FastRoute)

Category = Literal["conflict", "protest", "sanctions_trade", "diplomacy", "economy", "disaster", "legal_regulatory",
                   "health", "other"]


def headline_from_url(url: str | None) -> str | None:
    """Slug-derived headline (single implementation shared with the gdelt_company_daily linker)."""
    from app.nlp.gdelt_link import headline_from_url as _h  # noqa: PLC0415

    return _h(url)


_NUMERIC_PREFIX_RE = re.compile(r"^(?:\d{5,}\.|(?:19|20)\d{2} \d{1,2}(?!\d))")


def clean_headline(h: str | None, url: str | None) -> str | None:
    """Stored headlines written before the slug cleaner was fixed may still read '26620982.pentagon ...' or
    '2026 10 08 police ...' -> re-derive from the URL."""
    if not h or _NUMERIC_PREFIX_RE.match(h) or re.search(r"\.(s?html?|php)\b", h, re.I):
        return headline_from_url(url) or h
    return h


def _iso2(code: str | None) -> str | None:
    if not code:
        return None
    if len(code) == 2:
        return code.upper()
    try:
        from app.ingestion.gdelt_events import FIPS_TO_ISO2

        return FIPS_TO_ISO2.get(code.upper(), code.upper())
    except Exception:
        return code.upper()


def securities_for_issuers(db: Db, issuer_ids: set[str]) -> dict[str, dict[str, str]]:
    if not issuer_ids:
        return {}
    ids = sorted(issuer_ids)
    rows = db.all(f"""SELECT issuer_id, security_id, ticker FROM sec WHERE issuer_id IN ({', '.join('?' for _ in ids)})
                      QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY is_primary_security DESC,
                                                 market_cap_usd DESC NULLS LAST) = 1""", ids)
    return {r["issuer_id"]: {"security_id": r["security_id"], "ticker": r["ticker"]} for r in rows}


def event_out(e: dict[str, Any], secs: dict[str, dict[str, str]]) -> dict[str, Any]:
    iids = list(e.get("issuer_ids") or [])
    return {
        "global_event_id": int(e["global_event_id"]), "event_date": iso(e["event_date"]), "date_added": iso(e.get("date_added")),
        "event_code": e.get("event_code") or "", "event_root_code": e.get("event_root_code") or "",
        "event_category": e.get("event_category"), "quad_class": int(e.get("quad_class") or 0), "goldstein": fnum(e.get("goldstein")),
        "num_articles": int(e.get("num_articles") or 0), "avg_tone": fnum(e.get("avg_tone")),
        "actor1_name": e.get("actor1_name"), "actor2_name": e.get("actor2_name"), "action_country": _iso2(e.get("action_country")),
        "lat": fnum(e.get("action_lat")), "lon": fnum(e.get("action_lon")), "source_url": e.get("source_url"),
        "sector_ids": list(e.get("sector_ids") or []), "issuer_ids": iids,
        "headline": clean_headline(e.get("headline"), e.get("source_url")),
        "securities": [secs[i] for i in iids if i in secs],
    }


def _event_filters(db: Db, days: int, country: str | None, category: str | None, sector_id: str | None,
                   min_articles: int | None = None) -> tuple[str, list[Any]]:
    conds = ["event_date > ?"]
    params: list[Any] = [db.as_of - timedelta(days=days)]
    if country:
        conds.append("action_country = ?")
        params.append(country.upper())
    if category:
        conds.append("event_category = ?")
        params.append(category)
    if sector_id:
        conds.append("list_contains(sector_ids, ?)")
        params.append(sector_id)
    if min_articles:
        conds.append("num_articles >= ?")
        params.append(min_articles)
    return " AND ".join(conds), params


_URL_KEY_SQL = (r"rtrim(lower(regexp_replace(regexp_replace(source_url, '^[a-zA-Z]+://(www[0-9]*\.)?', ''), "
                r"'[?#].*$', '')), '/')")


@router.get("/events/feed", operation_id="getEventFeed")
def get_event_feed(country: str | None = None, category: Category | None = None, sector_id: str | None = None,
                   days: int = Query(7, ge=1, le=120), min_articles: int = Query(5, ge=0),
                   limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                   db: Db = Depends(get_db)) -> dict[str, Any]:
    where, params = _event_filters(db, days, country, category, sector_id, min_articles)
    # GDELT codes one article as several events (actor pairs / event codes): keep one row per source article
    # (URL without scheme / www / query / trailing slash), the most-covered one.
    dedup = f"""SELECT * FROM gdelt_events WHERE {where}
                QUALIFY row_number() OVER (PARTITION BY coalesce({_URL_KEY_SQL}, CAST(global_event_id AS VARCHAR))
                                           ORDER BY num_articles DESC, event_date DESC, global_event_id) = 1"""
    rows = db.all(f"""SELECT *, count(*) OVER () AS _total FROM ({dedup})
                      ORDER BY num_articles DESC, event_date DESC, global_event_id LIMIT ? OFFSET ?""", params + [limit, offset])
    total = int(rows[0]["_total"]) if rows else int(db.scalar(f"SELECT count(*) FROM ({dedup})", params) or 0)
    secs = securities_for_issuers(db, {i for e in rows for i in (e.get("issuer_ids") or [])})
    return {"total": total, "limit": limit, "offset": offset, "items": [event_out(e, secs) for e in rows],
            "data_status": "ok" if db.snap.input_ok("gdelt_events") else "pending"}


# A day counts as ingested when its global event count is >= this share of the busiest day in the last 14 days.
_COVERAGE_MIN_SHARE = 0.2


def _covered_windows(db: Db, a: Any) -> tuple[bool, bool]:
    """(current 7d window fully ingested, prior 7d window fully ingested). GDELT is back-filled sparsely, so a prior
    week with ~1% of the daily volume would turn change_7d into a meaningless +16,000%; we then report null."""
    rows = db.all("""SELECT event_date AS d, count(*) AS n FROM gdelt_events
                     WHERE event_date > ? AND event_date <= ? GROUP BY 1""", [a - timedelta(days=14), a])
    counts = {r["d"]: int(r["n"]) for r in rows}
    if not counts:
        return False, False
    floor = _COVERAGE_MIN_SHARE * max(counts.values())
    # windows match the SQL: current = (a-7, a], prior = (a-14, a-7]; day `a` itself may still be partially ingested
    def ok(lo: int, hi: int) -> bool:
        return all(counts.get(a - timedelta(days=k), 0) >= floor for k in range(lo, hi))
    return ok(1, 7), ok(7, 14)


@router.get("/events/map", operation_id="getEventMap")
def get_event_map(days: int = Query(7, ge=1, le=120), category: Category | None = None,
                  db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    cat = "AND event_category = ?" if category else ""
    a = db.as_of
    params: list[Any] = [a - timedelta(days=7), a - timedelta(days=7), a - timedelta(days=14),
                         a - timedelta(days=max(days, 14))] + ([category] if category else []) + [a - timedelta(days=days)]
    rows = db.all(f"""
        WITH e AS (SELECT * FROM gdelt_events WHERE action_country IS NOT NULL),
        cur7 AS (SELECT action_country, count(*) FILTER (event_date > ?) AS n7,
                        count(*) FILTER (event_date <= ? AND event_date > ?) AS p7
                 FROM e WHERE event_date > ? {cat} GROUP BY 1),
        w AS (SELECT * FROM e WHERE event_date > ? {cat}),
        cats AS (SELECT action_country, event_category, count(*) AS n FROM w WHERE event_category IS NOT NULL GROUP BY 1, 2)
        SELECT w.action_country AS country, count(*)::INT AS events, sum(num_articles)::BIGINT AS articles,
               avg(avg_tone) AS avg_tone, avg(goldstein) AS avg_goldstein,
               avg(CASE WHEN event_category = 'conflict' THEN 1.0 ELSE 0.0 END) AS conflict_share,
               avg(action_lat) AS lat, avg(action_lon) AS lon,
               (SELECT list(event_category ORDER BY n DESC) FROM cats c WHERE c.action_country = w.action_country) AS cats,
               max(c7.n7) AS n7, max(c7.p7) AS p7
        FROM w LEFT JOIN cur7 c7 ON c7.action_country = w.action_country
        GROUP BY 1 ORDER BY events DESC""", params + ([category] if category else []))
    cur_ok, prior_ok = _covered_windows(db, a)
    out = []
    for r in rows:
        p7, n7 = r.get("p7") or 0, r.get("n7") or 0
        if not (cur_ok and prior_ok):  # change vs a partially-ingested window is meaningless (e.g. +16,000%)
            p7 = 0
        out.append({"country": r["country"], "events": r["events"], "articles": int(r["articles"] or 0),
                    "avg_tone": rnd(r["avg_tone"], 4), "avg_goldstein": rnd(r["avg_goldstein"], 4),
                    "conflict_share": rnd(r["conflict_share"], 4), "top_categories": list(r.get("cats") or [])[:3],
                    "lat": rnd(r["lat"], 4), "lon": rnd(r["lon"], 4),
                    "change_7d": rnd((n7 - p7) / p7, 4) if p7 else None})
    return out


@router.get("/events/timeline", operation_id="getEventTimeline")
def get_event_timeline(country: str | None = None, sector_id: str | None = None, days: int = Query(90, ge=1, le=120),
                       db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    where, params = _event_filters(db, days, country, None, sector_id)
    rows = db.all(f"""SELECT event_date AS date, count(*)::INT AS events, sum(num_articles)::BIGINT AS articles,
                             avg(avg_tone) AS avg_tone, avg(goldstein) AS avg_goldstein
                      FROM gdelt_events WHERE {where} GROUP BY 1 ORDER BY 1""", params)
    return [{"date": iso(r["date"]), "events": r["events"], "articles": int(r["articles"] or 0),
             "avg_tone": rnd(r["avg_tone"], 4), "avg_goldstein": rnd(r["avg_goldstein"], 4)} for r in rows]


@router.get("/events/attention", operation_id="getCompanyAttention")
def get_company_attention(days: int = Query(7, ge=1, le=365), region: str | None = None,
                          regions: list[str] | None = regions_query(), limit: int = Query(25, ge=1, le=500),
                          db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    regs = parse_regions(region, regions)
    rc, rp = region_clause(regs)
    rows = db.all(f"""
        WITH g AS (SELECT issuer_id, sum(mentions)::BIGINT AS mentions, avg(avg_tone) AS avg_tone,
                          arg_max(attention_z, date) AS attention_z
                   FROM gdelt_company_daily WHERE date > ? GROUP BY 1),
        s AS (SELECT * FROM sec QUALIFY row_number() OVER (PARTITION BY issuer_id ORDER BY is_primary_security DESC,
                                                            market_cap_usd DESC NULLS LAST) = 1)
        SELECT {sec_ref_sql('s')}, g.mentions, g.avg_tone, g.attention_z, s.chg_1d
        FROM g JOIN s USING (issuer_id) WHERE {rc}
        ORDER BY g.attention_z DESC NULLS LAST, g.mentions DESC LIMIT ?""", [db.as_of - timedelta(days=days)] + rp + [limit])
    return [{"security": sec_ref(r), "mentions": int(r["mentions"] or 0), "attention_z": rnd(r["attention_z"], 4),
             "avg_tone": rnd(r["avg_tone"], 4), "chg_1d": fnum(r["chg_1d"])} for r in rows]
