"""Global Events v2 (tag: events-v2; events-v2 agent, plan E7). Reads the ev2_* serving tables built from the
labelled GDELT / news tables (T1 event_daily_agg, T3 news_stories / story_members, dictionaries event_labels).

Labels: country = ISO2 or GLOBAL; sector = taxonomy sector id (TECH, FINS, ...); issuer = security_id or issuer_id.
Themes: market-theme ids of app.events_v2.taxonomy (GET /events/v2/taxonomy). Multi-theme filters match ANY theme;
map / timeline sums over several themes count an event once per matching theme.
story_id is returned as a decimal string (int64 is not JS-safe).
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import invalid, iso, not_found, rnd

router = APIRouter(tags=["events-v2"], route_class=FastRoute)

LabelType = Literal["country", "sector", "issuer"]
P = "/events/v2"
_TABLES = ("event_daily_agg", "news_stories")


# ------------------------------------------------------------------------------------------------ dictionaries
class Dicts:
    def __init__(self, db: Db):
        rows = db.all("SELECT dict_type, id, code, name, grp, sector_mask FROM ev2_dict")
        self.country = {r["id"]: (r["code"], r["name"]) for r in rows if r["dict_type"] == "country"}
        self.country_id = {r["code"]: r["id"] for r in rows if r["dict_type"] == "country"}
        self.sector = {r["id"]: (r["code"], r["name"]) for r in rows if r["dict_type"] == "sector"}
        self.sector_bit = {r["code"]: r["id"] for r in rows if r["dict_type"] == "sector"}
        self.theme = {r["id"]: (r["code"], r["name"], r["grp"], r["sector_mask"]) for r in rows if r["dict_type"] == "theme"}
        self.theme_bit = {r["code"]: r["id"] for r in rows if r["dict_type"] == "theme"}
        self.source = {r["id"]: r["code"] for r in rows if r["dict_type"] == "source"}
        self.lang = {r["id"]: r["code"] for r in rows if r["dict_type"] == "lang"}
        if not self.theme:  # empty snapshot: fall back to the static taxonomy
            from app.events_v2 import taxonomy as tx  # noqa: PLC0415

            self.theme = {t.bit: (t.id, t.name, t.group, tx.sector_mask(t.sectors)) for t in tx.THEMES}
            self.theme_bit = {t.id: t.bit for t in tx.THEMES}
            self.sector = {i: s for i, s in enumerate(tx.SECTORS)}
            self.sector_bit = {s: i for i, (s, _) in enumerate(tx.SECTORS)}
            self.country = {i: (c, tx.country_name(c)) for i, c in enumerate(tx.COUNTRIES)}
            self.country_id = {c: i for i, c in enumerate(tx.COUNTRIES)}

    def themes_of(self, mask: int | None) -> list[str]:
        m = int(mask or 0)
        return [v[0] for b, v in sorted(self.theme.items()) if (m >> b) & 1]

    def sectors_of(self, mask: int | None) -> list[str]:
        m = int(mask or 0)
        return [v[0] for b, v in sorted(self.sector.items()) if (m >> b) & 1]

    def theme_mask(self, themes: list[str] | None) -> int:
        m = 0
        for t in themes or []:
            if t not in self.theme_bit:
                raise invalid(f"unknown theme {t!r} (see GET /api/v1/events/v2/taxonomy)")
            m |= 1 << self.theme_bit[t]
        return m


def _window(db: Db, start: date | None, end: date | None, days: int) -> tuple[date, date]:
    e = end or db.as_of
    s = start or (e - timedelta(days=days - 1))
    if s > e:
        raise invalid("start must be <= end")
    return s, e


def _label(db: Db, dc: Dicts, label_type: str, label_id: str) -> dict[str, Any]:
    """-> {type, id, name, label_int, security?}. 404 when unknown."""
    if label_type == "country":
        code = label_id.upper()
        if code not in dc.country_id:
            raise not_found(f"unknown country {label_id!r} (ISO2 or GLOBAL)")
        return {"type": "country", "id": code, "name": dc.country[dc.country_id[code]][1], "label_int": dc.country_id[code],
                "security": None}
    if label_type == "sector":
        code = label_id.upper()
        if code not in dc.sector_bit:
            raise not_found(f"unknown sector {label_id!r}")
        return {"type": "sector", "id": code, "name": dc.sector[dc.sector_bit[code]][1], "label_int": dc.sector_bit[code],
                "security": None}
    if label_type == "issuer":
        r = db.one("""SELECT i.iid, i.issuer_id, i.security_id, i.ticker, i.name FROM ev2_issuer i
                      WHERE i.security_id = ? OR i.issuer_id = ?
                         OR i.issuer_id = (SELECT issuer_id FROM sec WHERE security_id = ? LIMIT 1) LIMIT 1""",
                   [label_id, label_id, label_id])
        if not r:
            raise not_found(f"unknown security / issuer {label_id!r}")
        return {"type": "issuer", "id": r["security_id"] or r["issuer_id"], "name": r["name"] or r["ticker"] or label_id,
                "label_int": int(r["iid"]),
                "security": {"security_id": r["security_id"], "issuer_id": r["issuer_id"], "ticker": r["ticker"],
                             "name": r["name"]}}
    raise invalid("label_type must be country, sector or issuer")


def _label_out(lb: dict[str, Any]) -> dict[str, Any]:
    return {k: lb[k] for k in ("type", "id", "name", "security")}


def _f(x: Any, nd: int = 4) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else round(v, nd)


def _securities(db: Db, iids: set[int]) -> dict[int, dict[str, Any]]:
    if not iids:
        return {}
    ids = sorted(iids)
    rows = db.all(f"SELECT iid, issuer_id, security_id, ticker, name FROM ev2_issuer WHERE iid IN ({', '.join('?' for _ in ids)})",
                  ids)
    return {int(r["iid"]): r for r in rows}


def _domains(db: Db, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    ids_l = sorted(i for i in ids if i is not None)
    if not ids_l:
        return {}
    rows = db.all(f"SELECT id, code FROM ev2_domains WHERE id IN ({', '.join('?' for _ in ids_l)})", ids_l)
    return {int(r["id"]): r["code"] for r in rows}


def _story_out(r: dict[str, Any], dc: Dicts, secs: dict[int, dict[str, Any]], doms: dict[int, str]) -> dict[str, Any]:
    sm = int(r.get("source_mask") or 0)
    sec_list = []
    for iid, rel in zip(r.get("issuer_ids") or [], r.get("issuer_rel") or [], strict=False):
        x = secs.get(int(iid))
        if x:
            sec_list.append({"security_id": x["security_id"], "issuer_id": x["issuer_id"], "ticker": x["ticker"],
                             "name": x["name"], "relevance": round((rel or 0) / 100, 2)})
    return {
        "story_id": str(r["story_id"]), "title": r["title"] or "", "first_ts": iso(r["first_ts"]), "last_ts": iso(r["last_ts"]),
        "first_date": iso(r["first_date"]), "last_date": iso(r["last_date"]), "n_articles": int(r["n_articles"] or 0),
        "n_sources": int(r["n_sources"] or 0), "n_days": int(r["n_days"] or 1), "n_events": int(r["n_events"] or 0),
        "sources": [v for b, v in sorted(dc.source.items()) if (sm >> b) & 1],
        "domains": [doms[d] for d in (r.get("domain_ids") or []) if d in doms],
        "urls": list(r.get("urls") or []), "tone": _f(r.get("tone")), "sentiment": _f(r.get("sentiment")),
        "sentiment_model": r.get("sentiment_model"), "lang": dc.lang.get(r.get("lang_id")),
        "themes": dc.themes_of(r.get("theme_mask")), "sectors": dc.sectors_of(r.get("sector_mask")),
        "countries": [dc.country[c][0] for c in (r.get("countries") or []) if c in dc.country],
        "securities": sec_list, "salience": _f(r.get("salience"), 3) or 0.0,
    }


def _stories_out(db: Db, rows: list[dict[str, Any]], dc: Dicts) -> list[dict[str, Any]]:
    secs = _securities(db, {int(i) for r in rows for i in (r.get("issuer_ids") or [])})
    doms = _domains(db, {d for r in rows for d in (r.get("domain_ids") or [])})
    return [_story_out(r, dc, secs, doms) for r in rows]


def _status(db: Db) -> str:
    return db.snap.status(*_TABLES)


# ------------------------------------------------------------------------------------------------ taxonomy
@router.get(f"{P}/taxonomy", operation_id="getEventsV2Taxonomy")
def get_taxonomy(db: Db = Depends(get_db)) -> dict[str, Any]:
    dc = Dicts(db)
    return {
        "coverage": coverage(db),
        "themes": [{"id": v[0], "name": v[1], "group": v[2] or "", "sectors": dc.sectors_of(v[3])}
                   for _, v in sorted(dc.theme.items())],
        "sectors": [{"id": v[0], "name": v[1]} for _, v in sorted(dc.sector.items())],
        "countries": [{"id": v[0], "name": v[1]} for _, v in sorted(dc.country.items())],
        "data_status": _status(db),
    }


# ------------------------------------------------------------------------------------------------ map
MAX_DAYS = 2600  # ~7 years (2020+ backfill); long windows are aggregated weekly
WEEKLY_AFTER = 400  # auto weekly buckets for spans longer than this (days)
_COV: dict[str, dict[str, Any]] = {}


def coverage(db: Db) -> dict[str, Any]:
    """First / last date with labelled coverage (country, all themes), cached per snapshot."""
    k = db.snap.run_id
    if k not in _COV:
        r = db.one("SELECT min(date) AS f, max(date) AS l, count(DISTINCT date) AS n FROM ev2_agg "
                   "WHERE label_type = 'country' AND theme = -1") or {}
        _COV[k] = {"first_date": iso(r.get("f")), "last_date": iso(r.get("l")), "n_days": int(r.get("n") or 0)}
    return _COV[k]


def _bucket(freq: str | None, span_days: int) -> str:
    return "week" if (freq == "week" or (freq is None and span_days > WEEKLY_AFTER)) else "day"


@router.get(f"{P}/map", operation_id="getEventsV2Map")
def get_map(start: date | None = None, end: date | None = None, days: int = Query(7, ge=1, le=MAX_DAYS),
            theme: list[str] | None = Query(None), slider_days: int = Query(90, ge=7, le=MAX_DAYS),
            slider_freq: Literal["day", "week"] | None = Query(None, description="default: week when slider_days > 400"),
            db: Db = Depends(get_db)) -> dict[str, Any]:
    dc = Dicts(db)
    s, e = _window(db, start, end, days)
    tmask = dc.theme_mask(theme)
    th = [dc.theme_bit[t] for t in theme] if theme else [-1]
    ph = ", ".join("?" for _ in th)
    rows = db.all(f"""
        SELECT label_id, sum(ev_n)::BIGINT AS events, sum(art_n)::BIGINT AS articles, sum(ev_mentions)::BIGINT AS mentions,
               sum(ev_conflict_n)::BIGINT AS conflict, sum(ev_goldstein * ev_n) / nullif(sum(CASE WHEN ev_goldstein IS NOT NULL THEN ev_n END), 0) AS goldstein,
               sum(coalesce(art_tone, ev_tone) * (art_n + ev_n)) / nullif(sum(CASE WHEN coalesce(art_tone, ev_tone) IS NOT NULL THEN art_n + ev_n END), 0) AS tone,
               max(attention_z) AS attention_z, arg_max(attention_z, date) AS attention_z_last, min(tone_z) AS tone_z_min
        FROM ev2_agg WHERE label_type = 'country' AND theme IN ({ph}) AND date BETWEEN ? AND ?
        GROUP BY 1""", [*th, s, e])
    vmax = max((math.log1p((r["events"] or 0) + (r["articles"] or 0)) for r in rows), default=0) or 1
    out = []
    for r in rows:
        code, name = dc.country.get(int(r["label_id"]), (None, None))
        if code is None:
            continue
        ev = int(r["events"] or 0)
        out.append({"country": code, "name": name, "events": ev, "articles": int(r["articles"] or 0),
                    "mentions": int(r["mentions"] or 0), "conflict_share": _f((r["conflict"] or 0) / ev if ev else None),
                    "goldstein": _f(r["goldstein"]), "tone": _f(r["tone"]), "attention_z": _f(r["attention_z"]),
                    "attention_z_last": _f(r["attention_z_last"]), "tone_z_min": _f(r["tone_z_min"]),
                    "intensity": _f(math.log1p(ev + int(r["articles"] or 0)) / vmax)})
    out.sort(key=lambda x: -(x["events"] + x["articles"]))
    s2 = e - timedelta(days=slider_days - 1)
    sf = _bucket(slider_freq, slider_days)
    bk = "date" if sf == "day" else "date_trunc('week', date)::DATE"
    tl = db.all(f"""
        SELECT {bk} AS date, sum(ev_n)::BIGINT AS events, sum(art_n)::BIGINT AS articles FROM ev2_agg
        WHERE label_type = 'country' AND theme IN ({ph}) AND date BETWEEN ? AND ? GROUP BY 1 ORDER BY 1""", [*th, s2, e])
    return {"start": iso(s), "end": iso(e), "themes": dc.themes_of(tmask), "countries": out, "timeline_freq": sf,
            "coverage": coverage(db),
            "timeline": [{"date": iso(r["date"]), "events": int(r["events"] or 0), "articles": int(r["articles"] or 0)}
                         for r in tl],
            "data_status": _status(db)}


# ------------------------------------------------------------------------------------------------ timeline
def _series(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"date": iso(r["date"]), "events": int(r["events"] or 0), "articles": int(r["articles"] or 0),
             "mentions": int(r["mentions"] or 0), "tone": _f(r["tone"]), "goldstein": _f(r["goldstein"]),
             "attention_z": _f(r["attention_z"]), "tone_z": _f(r["tone_z"])} for r in rows]


@router.get(f"{P}/timeline", operation_id="getEventsV2Timeline")
def get_timeline(label_type: LabelType, label_id: str, theme: list[str] | None = Query(None),
                 start: date | None = None, end: date | None = None, days: int = Query(90, ge=1, le=MAX_DAYS),
                 freq: Literal["day", "week"] = "day", top_themes: int = Query(6, ge=0, le=41),
                 db: Db = Depends(get_db)) -> dict[str, Any]:
    dc = Dicts(db)
    lb = _label(db, dc, label_type, label_id)
    s, e = _window(db, start, end, days)
    dc.theme_mask(theme)
    if theme:
        bits = [dc.theme_bit[t] for t in theme]
    elif label_type == "issuer" or top_themes == 0:
        bits = [-1]
    else:
        tops = db.all("""SELECT theme, sum(art_n + ev_n) AS n FROM ev2_agg WHERE label_type = ? AND label_id = ?
                         AND theme >= 0 AND date BETWEEN ? AND ? GROUP BY 1 ORDER BY 2 DESC LIMIT ?""",
                      [label_type, lb["label_int"], s, e, top_themes])
        bits = [-1] + [int(r["theme"]) for r in tops]
    bucket = "date" if freq == "day" else "date_trunc('week', date)::DATE"
    series = []
    for b in bits:
        rows = db.all(f"""
            SELECT {bucket} AS date, sum(ev_n) AS events, sum(art_n) AS articles, sum(ev_mentions) AS mentions,
                   sum(coalesce(art_tone, ev_tone) * (art_n + ev_n)) / nullif(sum(CASE WHEN coalesce(art_tone, ev_tone) IS NOT NULL THEN art_n + ev_n END), 0) AS tone,
                   sum(ev_goldstein * ev_n) / nullif(sum(CASE WHEN ev_goldstein IS NOT NULL THEN ev_n END), 0) AS goldstein,
                   {'arg_max(attention_z, date)' if freq == 'week' else 'max(attention_z)'} AS attention_z,
                   {'arg_max(tone_z, date)' if freq == 'week' else 'max(tone_z)'} AS tone_z
            FROM ev2_agg WHERE label_type = ? AND label_id = ? AND theme = ? AND date BETWEEN ? AND ?
            GROUP BY 1 ORDER BY 1""", [label_type, lb["label_int"], b, s, e])
        tid = "all" if b < 0 else dc.theme[b][0]
        series.append({"theme": tid, "theme_name": "All themes" if b < 0 else dc.theme[b][1], "points": _series(rows)})
    return {"label": _label_out(lb), "start": iso(s), "end": iso(e), "freq": freq, "series": series,
            "data_status": _status(db)}


# ------------------------------------------------------------------------------------------------ stories
def _story_filters(db: Db, dc: Dicts, s: date, e: date, country: list[str] | None, sector: str | None,
                   security_id: str | None, theme: list[str] | None, tone_min: float | None, tone_max: float | None,
                   min_salience: float | None, min_articles: int | None, min_relevance: float, q: str | None,
                   ) -> tuple[str, list[Any]]:
    conds = ["last_date >= ?", "first_date <= ?"]
    params: list[Any] = [s, e]
    if country:
        ids = []
        for c in country:
            if c.upper() not in dc.country_id:
                raise invalid(f"unknown country {c!r}")
            ids.append(dc.country_id[c.upper()])
        conds.append("list_has_any(countries, ?)")
        params.append(ids)
    if sector:
        if sector.upper() not in dc.sector_bit:
            raise invalid(f"unknown sector {sector!r}")
        conds.append("(sector_mask & ?) <> 0")
        params.append(1 << dc.sector_bit[sector.upper()])
    if theme:
        conds.append("(theme_mask & ?) <> 0")
        params.append(dc.theme_mask(theme))
    if security_id:
        lb = _label(db, dc, "issuer", security_id)
        conds.append("story_id IN (SELECT story_id FROM ev2_story_issuer WHERE iid = ? AND rel >= ?)")
        params += [lb["label_int"], int(round(min_relevance * 100))]
    if tone_min is not None:
        conds.append("tone >= ?")
        params.append(tone_min)
    if tone_max is not None:
        conds.append("tone <= ?")
        params.append(tone_max)
    if min_salience is not None:
        conds.append("salience >= ?")
        params.append(min_salience)
    if min_articles:
        conds.append("n_articles >= ?")
        params.append(min_articles)
    if q:
        conds.append("lower(title) LIKE ?")
        params.append(f"%{q.lower()}%")
    return " AND ".join(conds), params


_SORT = {"salience": "salience DESC, last_ts DESC", "recent": "last_ts DESC, salience DESC",
         "articles": "n_articles DESC, salience DESC", "tone_neg": "tone ASC NULLS LAST, salience DESC",
         "tone_pos": "tone DESC NULLS LAST, salience DESC"}


@router.get(f"{P}/stories", operation_id="getEventsV2Stories")
def get_stories(country: list[str] | None = Query(None), sector: str | None = None,
                security_id: str | None = Query(None, description="security_id or issuer_id"),
                theme: list[str] | None = Query(None), tone_min: float | None = None, tone_max: float | None = None,
                min_salience: float | None = None, min_articles: int | None = Query(None, ge=1),
                min_relevance: float = Query(0.3, ge=0, le=1), q: str | None = None,
                start: date | None = None, end: date | None = None, days: int = Query(7, ge=1, le=MAX_DAYS),
                sort: Literal["salience", "recent", "articles", "tone_neg", "tone_pos"] = "salience",
                limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    dc = Dicts(db)
    s, e = _window(db, start, end, days)
    where, params = _story_filters(db, dc, s, e, country, sector, security_id, theme, tone_min, tone_max, min_salience,
                                   min_articles, min_relevance, q)
    # top-N + a separate count: a count(*) OVER () window materialises every matching row (~5 s on multi-year windows)
    # long windows: find the cut-off of the primary sort key on one narrow column first, then materialise only the
    # wide rows above it (multi-year "All" ranges: ~1 s -> ~40 ms)
    key = {"salience": "salience", "articles": "n_articles"}.get(sort)
    w2, p2 = where, params
    if key:
        thr = db.scalar(f"SELECT min({key}) FROM (SELECT {key} FROM ev2_stories WHERE {where} ORDER BY {key} DESC "
                        "LIMIT ?)", params + [limit + offset])
        if thr is not None:
            w2, p2 = f"{where} AND {key} >= ?", params + [thr]
    rows = db.all(f"""SELECT * FROM ev2_stories WHERE {w2}
                      ORDER BY {_SORT[sort]}, story_id LIMIT ? OFFSET ?""", p2 + [limit, offset])
    total = int(db.scalar(f"SELECT count(*) FROM ev2_stories WHERE {where}", params) or 0)
    return {"total": total, "limit": limit, "offset": offset, "start": iso(s), "end": iso(e),
            "items": _stories_out(db, rows, dc), "data_status": _status(db)}


@router.get(P + "/stories/{story_id}", operation_id="getEventsV2Story")
def get_story(story_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    try:
        sid = int(story_id)
    except ValueError as ex:
        raise invalid("story_id must be an integer string") from ex
    r = db.one("SELECT * FROM ev2_stories WHERE story_id = ?", [sid])
    if not r:
        raise not_found(f"story {story_id} not in the serving window")
    dc = Dicts(db)
    story = _stories_out(db, [r], dc)[0]
    mem = db.all("""SELECT date, minute, source_id, domain_id, tone_x100, lang_id FROM ev2_members WHERE story_id = ?
                    ORDER BY date, minute""", [sid])
    doms = _domains(db, {m["domain_id"] for m in mem})
    src: dict[str, int] = {}
    hours: dict[str, int] = {}
    langs: dict[str, int] = {}
    days: dict[str, int] = {}
    for m in mem:
        dname = doms.get(m["domain_id"]) or dc.source.get(m["source_id"]) or "unknown"
        src[dname] = src.get(dname, 0) + 1
        h = f"{iso(m['date'])}T{int(m['minute']) // 60:02d}:00:00Z"
        hours[h] = hours.get(h, 0) + 1
        lg = dc.lang.get(m["lang_id"]) or "unknown"
        langs[lg] = langs.get(lg, 0) + 1
        days[iso(m["date"])] = days.get(iso(m["date"]), 0) + 1
    if not mem:  # singleton / old story: members pruned -> use the story row
        for d in story["domains"]:
            src[d] = src.get(d, 0) + 1
        days[story["first_date"]] = story["n_articles"]
    return {"story": story,
            "sources": [{"domain": k, "articles": v} for k, v in sorted(src.items(), key=lambda kv: -kv[1])][:50],
            "timeline": [{"ts": k, "articles": v} for k, v in sorted(hours.items())],
            "days": [{"date": k, "articles": v} for k, v in sorted(days.items())],
            "languages": [{"lang": k, "articles": v} for k, v in sorted(langs.items(), key=lambda kv: -kv[1])],
            "members_available": bool(mem), "data_status": _status(db)}


# ------------------------------------------------------------------------------------------------ shocks
def _shock_rows(db: Db, dc: Dicts, s: date, e: date, label_type: str | None, label_int: int | None, theme_bits: list[int],
                min_z: float, limit: int) -> list[dict[str, Any]]:
    conds = ["date BETWEEN ? AND ?", "theme IN (" + ", ".join("?" for _ in theme_bits) + ")",
             "(attention_z >= ? OR abs(tone_z) >= ?)",
             "(art_n + ev_n) >= CASE WHEN label_type = 'issuer' THEN 5 ELSE 20 END"]
    params: list[Any] = [s, e, *theme_bits, min_z, min_z]
    if label_type:
        conds.append("label_type = ?")
        params.append(label_type)
    if label_int is not None:
        conds.append("label_id = ?")
        params.append(label_int)
    rows = db.all(f"""SELECT date, label_type, label_id, theme, attention_z, tone_z, ev_n, art_n,
                             coalesce(art_tone, ev_tone) AS tone
                      FROM ev2_agg WHERE {' AND '.join(conds)}
                      ORDER BY greatest(coalesce(attention_z, 0), abs(coalesce(tone_z, 0))) DESC, date DESC LIMIT ?""",
                  params + [limit])
    iids = {int(r["label_id"]) for r in rows if r["label_type"] == "issuer"}
    secs = _securities(db, iids)
    out = []
    for r in rows:
        lt, li = r["label_type"], int(r["label_id"])
        if lt == "country":
            code, name = dc.country.get(li, (str(li), str(li)))
            lab = {"type": lt, "id": code, "name": name, "security": None}
        elif lt == "sector":
            code, name = dc.sector.get(li, (str(li), str(li)))
            lab = {"type": lt, "id": code, "name": name, "security": None}
        else:
            x = secs.get(li) or {}
            lab = {"type": lt, "id": x.get("security_id") or x.get("issuer_id") or str(li),
                   "name": x.get("name") or x.get("ticker") or str(li),
                   "security": {k: x.get(k) for k in ("security_id", "issuer_id", "ticker", "name")} if x else None}
        az, tz = _f(r["attention_z"]), _f(r["tone_z"])
        kind = "attention" if (az or 0) >= min_z and (az or 0) >= abs(tz or 0) else ("tone_drop" if (tz or 0) < 0 else "tone_jump")
        tb = int(r["theme"])
        out.append({"date": iso(r["date"]), "label": lab, "theme": "all" if tb < 0 else dc.theme[tb][0], "kind": kind,
                    "attention_z": az, "tone_z": tz, "tone": _f(r["tone"]), "events": int(r["ev_n"] or 0),
                    "articles": int(r["art_n"] or 0)})
    return out


@router.get(f"{P}/shocks", operation_id="getEventsV2Shocks")
def get_shocks(label_type: LabelType | None = None, theme: list[str] | None = Query(None),
               start: date | None = None, end: date | None = None, days: int = Query(7, ge=1, le=MAX_DAYS),
               min_z: float = Query(2.5, ge=0.5, le=20), limit: int = Query(50, ge=1, le=500),
               db: Db = Depends(get_db)) -> dict[str, Any]:
    dc = Dicts(db)
    s, e = _window(db, start, end, days)
    dc.theme_mask(theme)
    bits = [dc.theme_bit[t] for t in theme] if theme else [-1]
    return {"start": iso(s), "end": iso(e), "min_z": min_z,
            "items": _shock_rows(db, dc, s, e, label_type, None, bits, min_z, limit), "data_status": _status(db)}


# ------------------------------------------------------------------------------------------------ label panel
@router.get(P + "/labels/{label_type}/{label_id}", operation_id="getEventsV2LabelPanel")
def get_label_panel(label_type: LabelType, label_id: str, days: int = Query(90, ge=7, le=MAX_DAYS),
                    stories: int = Query(10, ge=0, le=50),
                    freq: Literal["day", "week"] | None = Query(None, description="default: week when days > 400"),
                    db: Db = Depends(get_db)) -> dict[str, Any]:
    dc = Dicts(db)
    lb = _label(db, dc, label_type, label_id)
    e = db.as_of
    s = e - timedelta(days=days - 1)
    lf = _bucket(freq, days)
    if lf == "day":
        rows = db.all("""SELECT date, ev_n AS events, art_n AS articles, ev_mentions AS mentions,
                                coalesce(art_tone, ev_tone) AS tone, ev_goldstein AS goldstein, attention_z, tone_z
                         FROM ev2_agg WHERE label_type = ? AND label_id = ? AND theme = -1 AND date BETWEEN ? AND ?
                         ORDER BY date""", [label_type, lb["label_int"], s, e])
    else:
        rows = db.all("""SELECT date_trunc('week', date)::DATE AS date, sum(ev_n)::BIGINT AS events, sum(art_n)::BIGINT AS articles,
                                sum(ev_mentions)::BIGINT AS mentions,
                                sum(coalesce(art_tone, ev_tone) * (art_n + ev_n)) / nullif(sum(CASE WHEN coalesce(art_tone, ev_tone)
                                    IS NOT NULL THEN art_n + ev_n END), 0) AS tone,
                                avg(ev_goldstein) AS goldstein, max(attention_z) AS attention_z, min(tone_z) AS tone_z
                         FROM ev2_agg WHERE label_type = ? AND label_id = ? AND theme = -1 AND date BETWEEN ? AND ?
                         GROUP BY 1 ORDER BY 1""", [label_type, lb["label_int"], s, e])
    ser = _series(rows)
    themes = []
    if label_type != "issuer":
        th = db.all("""SELECT theme, sum(art_n)::BIGINT AS articles, sum(ev_n)::BIGINT AS events FROM ev2_agg
                       WHERE label_type = ? AND label_id = ? AND theme >= 0 AND date BETWEEN ? AND ?
                       GROUP BY 1 ORDER BY sum(art_n + ev_n) DESC LIMIT 12""", [label_type, lb["label_int"], s, e])
        tot = sum(int(r["articles"] or 0) + int(r["events"] or 0) for r in th) or 1
        themes = [{"theme": dc.theme[int(r["theme"])][0], "name": dc.theme[int(r["theme"])][1],
                   "articles": int(r["articles"] or 0), "events": int(r["events"] or 0),
                   "share": _f((int(r["articles"] or 0) + int(r["events"] or 0)) / tot)} for r in th]
    top: list[dict[str, Any]] = []
    if stories:
        if label_type == "issuer":
            srows = db.all("""SELECT st.* FROM ev2_stories st JOIN ev2_story_issuer si USING (story_id)
                              WHERE si.iid = ? AND si.rel >= 30 AND st.last_date BETWEEN ? AND ?
                              ORDER BY st.salience DESC LIMIT ?""", [lb["label_int"], e - timedelta(days=29), e, stories])
        elif label_type == "country":
            srows = db.all("""SELECT * FROM ev2_stories WHERE list_contains(countries, ?) AND last_date BETWEEN ? AND ?
                              ORDER BY salience DESC LIMIT ?""", [lb["label_int"], e - timedelta(days=6), e, stories])
        else:
            srows = db.all("""SELECT * FROM ev2_stories WHERE (sector_mask & ?) <> 0 AND last_date BETWEEN ? AND ?
                              ORDER BY salience DESC LIMIT ?""", [1 << lb["label_int"], e - timedelta(days=6), e, stories])
        top = _stories_out(db, srows, dc)
    last = ser[-1] if ser else None
    if lf == "week" and rows:  # "latest" stays the last daily row
        lr = db.all("""SELECT date, ev_n AS events, art_n AS articles, ev_mentions AS mentions,
                              coalesce(art_tone, ev_tone) AS tone, ev_goldstein AS goldstein, attention_z, tone_z
                       FROM ev2_agg WHERE label_type = ? AND label_id = ? AND theme = -1 AND date BETWEEN ? AND ?
                       ORDER BY date DESC LIMIT 1""", [label_type, lb["label_int"], s, e])
        last = _series(lr)[0] if lr else last
    return {"label": _label_out(lb), "as_of": iso(e), "latest": last, "series": ser, "themes": themes, "freq": lf,
            "top_stories": top, "shocks": _shock_rows(db, dc, s, e, label_type, lb["label_int"], [-1], 2.5, 20),
            "data_status": _status(db)}


# ------------------------------------------------------------------------------------------------ event dates
@router.get(f"{P}/event-dates", operation_id="getEventsV2EventDates")
def get_event_dates(label_type: LabelType, label_id: str, theme: str | None = None,
                    kind: Literal["attention", "tone_drop", "tone_jump", "any"] = "attention",
                    min_z: float = Query(2.0, ge=0.5, le=20), min_gap_days: int = Query(5, ge=0, le=365),
                    start: date | None = None, end: date | None = None, days: int = Query(1095, ge=7, le=4000),
                    max_n: int = Query(200, ge=1, le=2000), db: Db = Depends(get_db)) -> dict[str, Any]:
    """Event dates for Model Studio event studies: days where the label's attention z (or tone z) crosses min_z,
    de-clustered so consecutive hits within min_gap_days keep only the strongest."""
    dc = Dicts(db)
    lb = _label(db, dc, label_type, label_id)
    s, e = _window(db, start, end, days)
    tb = -1
    if theme:
        dc.theme_mask([theme])
        tb = dc.theme_bit[theme]
    cond = {"attention": "attention_z >= ?", "tone_drop": "tone_z <= -?", "tone_jump": "tone_z >= ?",
            "any": "(attention_z >= ? OR abs(tone_z) >= ?)"}[kind]
    zp = [min_z, min_z] if kind == "any" else [min_z]
    rows = db.all(f"""SELECT date, attention_z, tone_z, ev_n, art_n FROM ev2_agg
                      WHERE label_type = ? AND label_id = ? AND theme = ? AND date BETWEEN ? AND ? AND {cond}
                      ORDER BY date""", [label_type, lb["label_int"], tb, s, e, *zp])

    def strength(r: dict[str, Any]) -> float:
        if kind == "attention":
            return float(r["attention_z"] or 0)
        if kind in ("tone_drop", "tone_jump"):
            return abs(float(r["tone_z"] or 0))
        return max(float(r["attention_z"] or 0), abs(float(r["tone_z"] or 0)))

    picked: list[dict[str, Any]] = []
    for r in rows:
        if picked and min_gap_days and (r["date"] - picked[-1]["date"]).days < min_gap_days:
            if strength(r) > strength(picked[-1]):
                picked[-1] = r
            continue
        picked.append(r)
    picked = sorted(picked, key=strength, reverse=True)[:max_n]
    picked.sort(key=lambda r: r["date"])
    return {"label": _label_out(lb), "theme": theme or "all", "kind": kind, "min_z": min_z, "min_gap_days": min_gap_days,
            "start": iso(s), "end": iso(e),
            "method": "event_daily_agg z-scores vs the label's trailing 90 observations; hits within min_gap_days "
                      "de-clustered to the strongest",
            "dates": [{"date": iso(r["date"]), "attention_z": _f(r["attention_z"]), "tone_z": _f(r["tone_z"]),
                       "events": int(r["ev_n"] or 0), "articles": int(r["art_n"] or 0)} for r in picked],
            "data_status": _status(db)}


_ = rnd
