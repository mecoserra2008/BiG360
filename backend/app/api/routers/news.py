"""News Terminal (roadmap v3 N4; tag news; owner news-live).

Reads merge three layers by key, newest first, so a row is never missing nor duplicated while it moves from one to
the next:
  1. the R1 bus (data/realtime/bus.sqlite, read-only): news.item / news.story of the last 72 h, seconds old;
  2. the intraday DuckDB (app.api.intraday, rebuilt hourly by job news_intraday): items, stories, members, hourly
     event and sentiment aggregates of the last 7 days;
  3. the nightly serving snapshot (ev2_* tables): older stories.
Story ids are decimal strings (int64 is not JS-safe). Sentiment is model-based (FinBERT / XLM-R); the impact
estimate comes from the event-study library (historical CARs of similar labelled shocks) and is labelled model-based.
Responses are never cached (deps._NO_CACHE_PREFIXES includes /api/v1/news).
"""

from __future__ import annotations

import json
import math
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, FastRoute, get_db
from app.api.intraday import intraday
from app.api.intraday import status as intraday_status
from app.api.routers._common import invalid, iso, not_found
from app.core.config import get_settings
from app.core.logsetup import get_logger

router = APIRouter(tags=["news"], route_class=FastRoute)
log = get_logger("app.api.news")
P = "/news"
MODEL_LABEL = "Model-based: historical average reaction of similar labelled events (event-study library), not a forecast"


def _ro_bus() -> Any:
    from app.api.routers.live import _ro_bus as rb  # noqa: PLC0415

    return rb()


def _dt(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _isoz(v: Any) -> str | None:
    d = _dt(v)
    return d.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if d else None


def _lst(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v]
    return [str(x) for x in v if x is not None]


def _f(v: Any, nd: int = 4) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if math.isfinite(x) else None


# ------------------------------------------------------------------------------------------------ item shape
def term_item(p: dict[str, Any], live_id: int | None, origin: str) -> dict[str, Any]:
    pub, fs = _dt(p.get("published_at")), _dt(p.get("first_seen_at"))
    lat = (fs - pub).total_seconds() if (pub and fs and not p.get("published_estimated")) else None
    return {
        "live_id": live_id, "origin": origin, "item_key": str(p.get("item_key") or ""), "kind": p.get("kind") or "news",
        "source_id": p.get("source_id"), "tier": int(p.get("tier") or 4), "category": p.get("category"),
        "publisher": p.get("publisher"), "title": p.get("title") or "", "snippet": p.get("snippet"),
        "url": p.get("url"), "language": p.get("language"), "published_at": _isoz(pub),
        "first_seen_at": _isoz(fs), "latency_s": round(lat, 1) if lat is not None and -600 < lat < 7 * 86400 else None,
        "tickers": _lst(p.get("tickers")), "security_ids": _lst(p.get("security_ids")),
        "issuer_ids": _lst(p.get("issuer_ids")), "countries": _lst(p.get("countries")),
        "sectors": _lst(p.get("sectors")), "themes": _lst(p.get("themes")),
        "sentiment": _f(p.get("sentiment_score")), "sentiment_model": p.get("sentiment_model"),
        "tone": _f(p.get("tone"), 2), "salience": _f(p.get("salience"), 3),
        "story_id": str(p["story_id"]) if p.get("story_id") is not None else None,
        "story_size": int(p.get("story_size") or 1), "story_sources": int(p.get("story_sources") or 1),
        "story_first_seen": _isoz(p.get("story_first_seen")),
    }


def _sort_ts(x: dict[str, Any]) -> str:
    return x.get("first_seen_at") or x.get("published_at") or ""


# ------------------------------------------------------------------------------------------------ filters
class Filters:
    def __init__(self, tickers: str | None, security_id: str | None, watchlist_id: str | None, country: str | None,
                 sector: str | None, theme: str | None, tier: str | None, lang: str | None, kind: str | None,
                 q: str | None, story_id: str | None):
        self.tickers = {t.strip().upper() for t in (tickers or "").split(",") if t.strip()}
        self.secs: set[str] = {security_id} if security_id else set()
        if watchlist_id:
            from app.api import app_state  # noqa: PLC0415

            if app_state.get_watchlist(watchlist_id) is None:
                raise not_found(f"watchlist {watchlist_id} not found")
            self.secs |= {r["security_id"] for r in app_state.watchlist_items(watchlist_id)}
            self.watchlist = True
        else:
            self.watchlist = False
        self.country = {c.strip().upper() for c in (country or "").split(",") if c.strip()}
        self.sector = {c.strip().upper() for c in (sector or "").split(",") if c.strip()}
        self.theme = {c.strip() for c in (theme or "").split(",") if c.strip()}
        try:
            self.tier = {int(x) for x in (tier or "").split(",") if x.strip()}
        except ValueError as e:
            raise invalid("tier must be a comma list of 1..4") from e
        self.lang = {c.strip().lower() for c in (lang or "").split(",") if c.strip()}
        self.kind = kind if kind in ("news", "gdelt") else None
        self.q = (q or "").strip().lower() or None
        self.story = story_id

    @property
    def any(self) -> bool:
        return bool(self.tickers or self.secs or self.watchlist or self.country or self.sector or self.theme
                    or self.tier or self.lang or self.kind or self.q or self.story)

    def ok(self, it: dict[str, Any]) -> bool:
        if self.watchlist and not self.secs:
            return False
        if self.tickers and not ({t.upper() for t in it["tickers"]} & self.tickers):
            return False
        if self.secs and not (set(it["security_ids"]) & self.secs):
            return False
        if self.country and not ({c.upper() for c in it["countries"]} & self.country):
            return False
        if self.sector and not ({c.upper() for c in it["sectors"]} & self.sector):
            return False
        if self.theme and not (set(it["themes"]) & self.theme):
            return False
        if self.tier and it["tier"] not in self.tier:
            return False
        if self.lang and (it["language"] or "").lower() not in self.lang:
            return False
        if self.kind and it["kind"] != self.kind:
            return False
        if self.story and it["story_id"] != self.story:
            return False
        return not (self.q and self.q not in (it["title"] or "").lower() and self.q not in (it["publisher"] or "").lower())

    def sql(self) -> tuple[str, list[Any]]:
        w, a = ["1=1"], []
        if self.watchlist and not self.secs:
            return "1=0", []
        if self.tickers:
            w.append("list_has_any(list_transform(tickers, x -> upper(x)), ?)")
            a.append(sorted(self.tickers))
        if self.secs:
            w.append("list_has_any(security_ids, ?)")
            a.append(sorted(self.secs))
        for col, vals in (("countries", self.country), ("sectors", self.sector)):
            if vals:
                w.append(f"list_has_any(list_transform({col}, x -> upper(x)), ?)")
                a.append(sorted(vals))
        if self.theme:
            w.append("list_has_any(themes, ?)")
            a.append(sorted(self.theme))
        if self.tier:
            w.append(f"tier IN ({', '.join('?' for _ in self.tier)})")
            a += sorted(self.tier)
        if self.lang:
            w.append(f"lower(language) IN ({', '.join('?' for _ in self.lang)})")
            a += sorted(self.lang)
        if self.kind:
            w.append("kind = ?")
            a.append(self.kind)
        if self.story:
            w.append("story_id = ?")
            a.append(self.story)
        if self.q:
            w.append("(lower(title) LIKE ? OR lower(publisher) LIKE ?)")
            a += [f"%{self.q}%", f"%{self.q}%"]
        return " AND ".join(w), a


def _filters(tickers: str | None = None, security_id: str | None = None, watchlist_id: str | None = None,
             country: str | None = None, sector: str | None = None, theme: str | None = None,
             tier: str | None = None, lang: str | None = None,
             kind: Literal["news", "gdelt", "all"] | None = None, q: str | None = Query(None, max_length=120),
             story_id: str | None = None) -> Filters:
    return Filters(tickers, security_id, watchlist_id, country, sector, theme, tier, lang, kind, q, story_id)


def live_items(f: Filters, limit: int, before: datetime | None = None) -> list[dict[str, Any]]:
    bus = _ro_bus()
    if bus is None:
        return []
    try:
        where = {"story_id": f.story} if f.story else None
        n = limit if not f.any else min(20000, limit * 8)
        msgs = bus.latest("news.item", n, where=where)
    except Exception as e:  # noqa: BLE001
        log.warning("news: bus read failed: %s", e)
        return []
    out = []
    for m in msgs:
        it = term_item(m.payload, m.id, "live")
        if before is not None and (_dt(_sort_ts(it)) or before) >= before:
            continue
        if f.ok(it):
            out.append(it)
        if len(out) >= limit:
            break
    return out


def intraday_items(f: Filters, limit: int, before: datetime | None = None) -> list[dict[str, Any]]:
    with intraday() as idb:
        if idb is None:
            return []
        w, a = f.sql()
        if before is not None:
            w += " AND coalesce(first_seen_at, published_at) < ?"
            a.append(before)
        try:
            rows = idb.all(f"""SELECT * FROM nt_items WHERE {w}
                               ORDER BY coalesce(first_seen_at, published_at) DESC LIMIT ?""", [*a, limit])
        except Exception as e:  # noqa: BLE001
            log.warning("news: intraday read failed: %s", e)
            return []
    return [term_item(r, None, "intraday") for r in rows]


# ------------------------------------------------------------------------------------------------ terminal
@router.get(f"{P}/terminal", operation_id="getNewsTerminal")
def get_terminal(limit: int = Query(1000, ge=1, le=6000), before: datetime | None = None,
                 f: Filters = Depends(_filters)) -> dict[str, Any]:
    t0 = time.perf_counter()
    bf = _dt(before) if before else None
    live = live_items(f, limit, bf)
    seen = {x["item_key"] for x in live}
    older = [x for x in intraday_items(f, limit, bf) if x["item_key"] not in seen]
    items = sorted(live + older, key=_sort_ts, reverse=True)[:limit]
    bus = _ro_bus()
    head = None
    if bus is not None:
        try:
            head = bus.head_id()
        except Exception:  # noqa: BLE001
            head = None
    last = items[-1] if items else None
    ist = intraday_status()
    return {"data_status": "ok" if items else "pending", "generated_at": _isoz(datetime.now(UTC)), "head_id": head,
            "n_live": sum(1 for x in items if x["origin"] == "live"),
            "n_intraday": sum(1 for x in items if x["origin"] == "intraday"),
            "intraday_built_at": ist.get("built_at"),
            "next_before": _sort_ts(last) if last and len(items) >= limit else None,
            "query_ms": round((time.perf_counter() - t0) * 1000, 1), "items": items}


# ------------------------------------------------------------------------------------------------ stories
class Dicts:
    def __init__(self, rows: list[dict[str, Any]]):
        self.country = {r["id"]: r["code"] for r in rows if r["dict_type"] == "country"}
        self.sector = {r["id"]: r["code"] for r in rows if r["dict_type"] == "sector"}
        self.theme = {r["id"]: r["code"] for r in rows if r["dict_type"] == "theme"}
        self.source = {r["id"]: r["code"] for r in rows if r["dict_type"] == "source"}
        self.lang = {r["id"]: r["code"] for r in rows if r["dict_type"] == "lang"}
        self.domain = {r["id"]: r["code"] for r in rows if r["dict_type"] == "domain"}
        if not self.theme:
            from app.events_v2 import taxonomy as tx  # noqa: PLC0415

            self.theme = {t.bit: t.id for t in tx.THEMES}
            self.sector = {i: s for i, (s, _) in enumerate(tx.SECTORS)}
            self.country = dict(enumerate(tx.COUNTRIES))

    def bits(self, m: dict[int, str], mask: Any) -> list[str]:
        v = int(mask or 0)
        return [code for b, code in sorted(m.items()) if (v >> b) & 1]


def _dicts(idb: Any, db: Db | None) -> Dicts:
    rows: list[dict[str, Any]] = []
    if idb is not None:
        try:
            rows = idb.all("SELECT dict_type, id, code FROM nt_dict")
        except Exception:  # noqa: BLE001
            rows = []
    if not rows and db is not None and not db.snap.is_empty:
        try:
            rows = db.all("SELECT dict_type, id, code FROM ev2_dict") + \
                [{"dict_type": "domain", **r} for r in db.all("SELECT id, code FROM ev2_domains")]
        except Exception:  # noqa: BLE001
            rows = []
    return Dicts(rows)


def _issuers(idb: Any, db: Db | None, iids: set[int]) -> dict[int, dict[str, Any]]:
    if not iids:
        return {}
    ids = sorted(iids)
    ph = ", ".join("?" for _ in ids)
    out: dict[int, dict[str, Any]] = {}
    if idb is not None:
        try:
            for r in idb.all(f"SELECT iid, issuer_id, security_id, ticker, name FROM nt_issuer WHERE iid IN ({ph})", ids):
                out[int(r["iid"])] = r
        except Exception:  # noqa: BLE001
            pass
    missing = [i for i in ids if i not in out]
    if missing and db is not None and not db.snap.is_empty:
        try:
            for r in db.all(f"SELECT iid, issuer_id, security_id, ticker, name FROM ev2_issuer WHERE iid IN "
                            f"({', '.join('?' for _ in missing)})", missing):
                out[int(r["iid"])] = r
        except Exception:  # noqa: BLE001
            pass
    return out


def story_out(r: dict[str, Any], dc: Dicts, iss: dict[int, dict[str, Any]], origin: str) -> dict[str, Any]:
    secs = []
    for iid, rel in zip(r.get("issuer_ids") or [], r.get("issuer_rel") or [], strict=False):
        x = iss.get(int(iid))
        if x:
            secs.append({"security_id": x.get("security_id"), "issuer_id": x.get("issuer_id"), "ticker": x.get("ticker"),
                         "name": x.get("name"), "relevance": round((rel or 0) / 100, 2)})
    return {
        "story_id": str(r["story_id"]), "origin": origin, "title": r.get("title") or "",
        "first_ts": _isoz(r.get("first_ts")), "last_ts": _isoz(r.get("last_ts")),
        "n_articles": int(r.get("n_articles") or 0), "n_sources": int(r.get("n_sources") or 0),
        "n_events": int(r.get("n_events") or 0),
        "sources": dc.bits(dc.source, r.get("source_mask")),
        "domains": [dc.domain[d] for d in (r.get("domain_ids") or []) if d in dc.domain],
        "urls": list(r.get("urls") or []), "tone": _f(r.get("tone")), "sentiment": _f(r.get("sentiment")),
        "sentiment_model": r.get("sentiment_model"), "lang": dc.lang.get(r.get("lang_id")),
        "themes": dc.bits(dc.theme, r.get("theme_mask")), "sectors": dc.bits(dc.sector, r.get("sector_mask")),
        "countries": [dc.country[c] for c in (r.get("countries") or []) if c in dc.country],
        "securities": secs, "salience": _f(r.get("salience"), 3) or 0.0,
        "velocity_1h": None, "velocity_ratio": None,
    }


def _live_story(p: dict[str, Any]) -> dict[str, Any]:
    return {"story_id": str(p.get("story_id")), "origin": "live", "title": p.get("title") or "",
            "first_ts": _isoz(p.get("first_seen_at")), "last_ts": _isoz(p.get("last_seen_at")),
            "n_articles": int(p.get("n_articles") or 0), "n_sources": int(p.get("n_sources") or 0), "n_events": 0,
            "sources": [], "domains": list(p.get("sources") or [])[:8], "urls": [], "tone": _f(p.get("tone_mean")),
            "sentiment": _f(p.get("sentiment_mean")), "sentiment_model": "live", "lang": None,
            "themes": list(p.get("themes") or []), "sectors": [], "countries": list(p.get("countries") or []),
            "securities": [], "issuer_ids": list(p.get("issuer_ids") or []), "salience": None,
            "velocity_1h": p.get("velocity_1h"), "velocity_ratio": p.get("velocity_ratio")}


@router.get(f"{P}/stories", operation_id="getNewsStories")
def get_stories(hours: int = Query(24, ge=1, le=168), sort: Literal["salience", "velocity", "recent", "size"] = "salience",
                limit: int = Query(100, ge=1, le=500), country: str | None = None, sector: str | None = None,
                theme: str | None = None, security_id: str | None = None,
                db: Db = Depends(get_db)) -> dict[str, Any]:
    since = datetime.now(UTC) - timedelta(hours=hours)
    out: dict[str, dict[str, Any]] = {}
    vel: dict[str, dict[str, Any]] = {}
    bus = _ro_bus()
    if bus is not None:
        try:
            for m in bus.latest("news.story", 3000):
                sid = str(m.payload.get("story_id"))
                if sid not in vel:
                    vel[sid] = m.payload
        except Exception as e:  # noqa: BLE001
            log.debug("news.story read failed: %s", e)
    with intraday() as idb:
        dc = _dicts(idb, db)
        rows: list[dict[str, Any]] = []
        if idb is not None:
            try:
                rows = idb.all("SELECT * FROM nt_stories WHERE last_ts >= ? ORDER BY salience DESC LIMIT 4000", [since])
            except Exception as e:  # noqa: BLE001
                log.warning("nt_stories read failed: %s", e)
        iss = _issuers(idb, db, {int(i) for r in rows for i in (r.get("issuer_ids") or [])})
        for r in rows:
            out[str(r["story_id"])] = story_out(r, dc, iss, "intraday")
    if not out and not db.snap.is_empty:   # no intraday file yet: snapshot stories
        try:
            rows = db.all("SELECT * FROM ev2_stories WHERE last_ts >= ? ORDER BY salience DESC LIMIT 2000", [since])
            iss = _issuers(None, db, {int(i) for r in rows for i in (r.get("issuer_ids") or [])})
            for r in rows:
                out[str(r["story_id"])] = story_out(r, dc, iss, "snapshot")
        except Exception as e:  # noqa: BLE001
            log.debug("ev2_stories unavailable: %s", e)
    for sid, p in vel.items():
        last = _dt(p.get("last_seen_at"))
        if last is None or last < since:
            continue
        if sid in out:
            st = out[sid]
            st["velocity_1h"], st["velocity_ratio"] = p.get("velocity_1h"), p.get("velocity_ratio")
            st["n_articles"] = max(st["n_articles"], int(p.get("n_articles") or 0))
            st["last_ts"] = max(st["last_ts"] or "", _isoz(last) or "")
        elif int(p.get("n_articles") or 0) >= 2:
            out[sid] = _live_story(p)
    items = list(out.values())
    if country:
        cc = {c.upper() for c in country.split(",")}
        items = [x for x in items if cc & {c.upper() for c in x["countries"]}]
    if sector:
        ss = {c.upper() for c in sector.split(",")}
        items = [x for x in items if ss & set(x["sectors"])]
    if theme:
        th = set(theme.split(","))
        items = [x for x in items if th & set(x["themes"])]
    if security_id:
        items = [x for x in items if any(s.get("security_id") == security_id for s in x["securities"])]
    key = {"salience": lambda x: (x["salience"] or 0.0, x["n_articles"]),
           "velocity": lambda x: (x["velocity_1h"] or 0, x["velocity_ratio"] or 0.0),
           "recent": lambda x: x["last_ts"] or "", "size": lambda x: (x["n_articles"], x["n_sources"])}[sort]
    items.sort(key=key, reverse=True)
    return {"data_status": "ok" if items else "pending", "generated_at": _isoz(datetime.now(UTC)), "hours": hours,
            "sort": sort, "items": items[:limit]}


def _price_moves(db: Db, secs: list[dict[str, Any]], since: datetime | None) -> list[dict[str, Any]]:
    """Price move of linked securities since the story's first publication (latest close vs the last close before)."""
    if db.snap.is_empty or not secs or since is None:
        return []
    out = []
    d0 = since.date()
    for s in secs[:8]:
        sid = s.get("security_id")
        if not sid:
            continue
        try:
            base = db.one("SELECT date, close FROM prices WHERE security_id = ? AND date < ? ORDER BY date DESC LIMIT 1",
                          [sid, d0])
            last = db.one("SELECT date, close FROM prices WHERE security_id = ? ORDER BY date DESC LIMIT 1", [sid])
        except Exception:  # noqa: BLE001
            base = last = None
        mv = None
        if base and last and base.get("close") and last.get("close") and last["date"] >= base["date"]:
            mv = (float(last["close"]) / float(base["close"]) - 1.0) if float(base["close"]) else None
        out.append({**{k: s.get(k) for k in ("security_id", "ticker", "name", "relevance")},
                    "base_date": iso(base["date"]) if base else None, "base_close": _f(base.get("close")) if base else None,
                    "last_date": iso(last["date"]) if last else None, "last_close": _f(last.get("close")) if last else None,
                    "move_pct": _f(mv, 5),
                    "note": None if mv is not None else "no price before / after the first publication in the snapshot"})
    return out


def _related_filings(db: Db, issuer_ids: list[str], since: datetime | None) -> list[dict[str, Any]]:
    if not issuer_ids:
        return []
    lo = (since or datetime.now(UTC)) - timedelta(days=3)
    out: dict[str, dict[str, Any]] = {}
    bus = _ro_bus()
    if bus is not None:
        try:
            for m in bus.latest("filings", 400):
                p = m.payload
                if p.get("issuer_id") in issuer_ids:
                    out[str(p.get("filing_id"))] = {"filing_id": p.get("filing_id"), "origin": "live",
                                                    "form_type": p.get("form_type"), "title": p.get("title"),
                                                    "url": p.get("url"), "ticker": p.get("ticker"),
                                                    "knowledge_ts": _isoz(p.get("knowledge_ts") or p.get("first_seen_at"))}
        except Exception:  # noqa: BLE001
            pass
    if not db.snap.is_empty:
        try:
            ph = ", ".join("?" for _ in issuer_ids)
            for r in db.all(f"""SELECT filing_id, form_type, title, url, knowledge_date, knowledge_ts FROM filings
                                WHERE issuer_id IN ({ph}) AND knowledge_date >= ? ORDER BY knowledge_date DESC LIMIT 20""",
                            [*issuer_ids, lo.date()]):
                out.setdefault(str(r["filing_id"]), {"filing_id": r["filing_id"], "origin": "snapshot",
                                                     "form_type": r.get("form_type"), "title": r.get("title"),
                                                     "url": r.get("url"), "ticker": None,
                                                     "knowledge_ts": _isoz(r.get("knowledge_ts")) or iso(r.get("knowledge_date"))})
        except Exception as e:  # noqa: BLE001
            log.debug("filings unavailable: %s", e)
    return sorted(out.values(), key=lambda x: x.get("knowledge_ts") or "", reverse=True)[:15]


def _related_events(countries: list[str], themes: list[str]) -> list[dict[str, Any]]:
    bus = _ro_bus()
    if bus is None or not (countries or themes):
        return []
    out = []
    try:
        for m in bus.latest("events", 1500, types=["event"]):
            p = m.payload
            if countries and p.get("country") not in countries:
                continue
            if themes and not set(themes) & set(p.get("themes") or []):
                continue
            out.append({"key": m.key, "cameo": p.get("cameo"), "country": p.get("country"),
                        "themes": p.get("themes") or [], "goldstein": p.get("goldstein"), "tone": p.get("tone"),
                        "n_articles": p.get("n_articles"), "url": p.get("url"), "salience": p.get("salience"),
                        "published_at": _isoz(p.get("published_at"))})
    except Exception:  # noqa: BLE001
        return []
    out.sort(key=lambda x: x.get("salience") or 0.0, reverse=True)
    return out[:10]


def _impact(db: Db, themes: list[str], countries: list[str], sectors: list[str],
            linked: bool = False) -> dict[str, Any] | None:
    """Issuer-level cells only for stories linked to a security; otherwise the index / sector ETF reaction."""
    if db.snap.is_empty or not themes:
        return None
    try:
        import polars as pl  # noqa: PLC0415

        from app.events_models.event_study import expected_reaction  # noqa: PLC0415

        cand = db.all('SELECT * FROM em_event_studies WHERE theme IN ({}) AND shock_kind = ? AND "window" = ?'.format(
            ", ".join("?" for _ in themes)), [*themes, "attention", "0,5"])
    except Exception as e:  # noqa: BLE001
        log.debug("event study library unavailable: %s", e)
        return None
    if not cand:
        return None
    lib = pl.DataFrame(cand, infer_schema_length=None)
    best = None
    levels = [None] if linked else ["country_index", "sector_etf"]
    for th in themes:
        iso2 = next((c for c in countries if c and c != "GLOBAL" and len(c) == 2), None)
        for lv in levels:
            hit = expected_reaction(lib, th, iso2, sectors[0] if sectors else None, "0,5", "attention", lv, 3)
            if hit and (best is None or (hit.get("n_events") or 0) > (best.get("n_events") or 0)):
                best = hit
    if not best:
        return None
    return {"model_based": True, "label": MODEL_LABEL, "theme": best.get("theme"), "iso2": best.get("iso2"),
            "sector": best.get("sector"), "level": best.get("level"), "window": best.get("window"),
            "match": best.get("match"), "n_events": int(best.get("n_events") or 0),
            "mean_car": _f(best.get("mean_car"), 5), "ci_low": _f(best.get("ci_low"), 5),
            "ci_high": _f(best.get("ci_high"), 5), "p_value": _f(best.get("p_value"), 4),
            "share_negative": _f(best.get("share_negative"), 3), "benchmark": best.get("benchmark"),
            "unit": "percent"}  # em_event_studies stores CAR / CI x 100 (event_study._aggregate)


@router.get(P + "/stories/{story_id}", operation_id="getNewsStory")
def get_story(story_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    try:
        sid = int(story_id)
    except ValueError as ex:
        raise invalid("story_id must be an integer string") from ex
    story: dict[str, Any] | None = None
    members: list[dict[str, Any]] = []
    with intraday() as idb:
        dc = _dicts(idb, db)
        if idb is not None:
            r = idb.one("SELECT * FROM nt_stories WHERE story_id = ?", [sid])
            if r:
                story = story_out(r, dc, _issuers(idb, db, {int(i) for i in (r.get("issuer_ids") or [])}), "intraday")
                members = idb.all("SELECT date, minute, source_id, domain_id, lang_id FROM nt_members WHERE story_id = ? "
                                  "ORDER BY date, minute", [sid])
        if story is None and not db.snap.is_empty:
            try:
                r = db.one("SELECT * FROM ev2_stories WHERE story_id = ?", [sid])
            except Exception:  # noqa: BLE001
                r = None
            if r:
                story = story_out(r, dc, _issuers(None, db, {int(i) for i in (r.get("issuer_ids") or [])}), "snapshot")
                try:
                    members = db.all("SELECT date, minute, source_id, domain_id, lang_id FROM ev2_members "
                                     "WHERE story_id = ? ORDER BY date, minute", [sid])
                except Exception:  # noqa: BLE001
                    members = []
    f = Filters(None, None, None, None, None, None, None, None, None, None, story_id)
    items = live_items(f, 300)
    seen = {x["item_key"] for x in items}
    items += [x for x in intraday_items(f, 300) if x["item_key"] not in seen]
    items.sort(key=_sort_ts)
    bus = _ro_bus()
    live_st = None
    if bus is not None:
        try:
            got = bus.latest("news.story", 1, where={"story_id": story_id})
            live_st = got[0].payload if got else None
        except Exception:  # noqa: BLE001
            live_st = None
    if story is None and live_st is None and not items:
        raise not_found(f"story {story_id} not in the live window, the intraday window or the snapshot")
    if story is None:
        story = _live_story(live_st) if live_st else {
            "story_id": story_id, "origin": "live", "title": items[0]["title"], "first_ts": _sort_ts(items[0]),
            "last_ts": _sort_ts(items[-1]), "n_articles": len(items),
            "n_sources": len({x["publisher"] for x in items}), "n_events": 0, "sources": [], "domains": [],
            "urls": [x["url"] for x in items[:3] if x["url"]], "tone": None, "sentiment": None,
            "sentiment_model": None, "lang": None, "themes": sorted({t for x in items for t in x["themes"]}),
            "sectors": sorted({t for x in items for t in x["sectors"]}),
            "countries": sorted({t for x in items for t in x["countries"]}), "securities": [], "salience": None,
            "velocity_1h": None, "velocity_ratio": None}
    if live_st:
        story["velocity_1h"], story["velocity_ratio"] = live_st.get("velocity_1h"), live_st.get("velocity_ratio")
        story["n_articles"] = max(story["n_articles"], int(live_st.get("n_articles") or 0))
    # securities: story issuers + every item's securities
    secs = {s["security_id"]: s for s in story.get("securities") or [] if s.get("security_id")}
    for x in items:
        for k, sec in enumerate(x["security_ids"]):
            secs.setdefault(sec, {"security_id": sec, "ticker": x["tickers"][k] if k < len(x["tickers"]) else None,
                                  "name": None, "relevance": None})
    issuer_ids = sorted({i for x in items for i in x["issuer_ids"]}
                        | {s.get("issuer_id") for s in story.get("securities") or [] if s.get("issuer_id")})
    first = _dt(story.get("first_ts")) or (_dt(_sort_ts(items[0])) if items else None)
    # timeline: articles per hour (members of the lake + live items not yet folded)
    hours: dict[str, int] = {}
    seen_m = 0
    for m in members:
        h = f"{iso(m['date'])}T{int(m['minute'] or 0) // 60:02d}:00:00Z"
        hours[h] = hours.get(h, 0) + 1
        seen_m += 1
    if not members:
        for x in items:
            t = _dt(_sort_ts(x))
            if t:
                h = t.strftime("%Y-%m-%dT%H:00:00Z")
                hours[h] = hours.get(h, 0) + 1
    srcs: dict[str, int] = {}
    for m in members:
        dname = dc.domain.get(m.get("domain_id")) or dc.source.get(m.get("source_id")) or "unknown"
        srcs[dname] = srcs.get(dname, 0) + 1
    for x in items:
        if not members:
            srcs[x["publisher"] or "unknown"] = srcs.get(x["publisher"] or "unknown", 0) + 1
    return {"data_status": "ok", "story": story,
            "timeline": [{"ts": k, "articles": v} for k, v in sorted(hours.items())],
            "sources": [{"domain": k, "articles": v} for k, v in sorted(srcs.items(), key=lambda kv: -kv[1])][:40],
            "items": items[-200:],
            "securities": _price_moves(db, list(secs.values()), first),
            "filings": _related_filings(db, issuer_ids, first),
            "events": _related_events([c for c in story.get("countries") or [] if c != "GLOBAL"],
                                      story.get("themes") or []),
            "impact": _impact(db, story.get("themes") or [], story.get("countries") or [], story.get("sectors") or [],
                              linked=bool(secs)),
            "members_available": bool(members)}


# ------------------------------------------------------------------------------------------------ hourly series
@router.get(f"{P}/hourly", operation_id="getNewsHourly")
def get_hourly(label_type: Literal["all", "country", "sector", "issuer", "theme"] = "all", label_id: str = "*",
               hours: int = Query(72, ge=1, le=168), db: Db = Depends(get_db)) -> dict[str, Any]:
    """Hourly sentiment (sentiment_hourly) and GDELT attention (event_hourly_agg) of one label, last N hours."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    sent: list[dict[str, Any]] = []
    att: list[dict[str, Any]] = []
    with intraday() as idb:
        if idb is not None:
            sl = label_id if label_type != "all" else "*"
            sent = idb.all("""SELECT date, hour, n_items, n_scored, sentiment_mean, pos_share, neg_share, n_sources,
                                     tier1_n, art_n, art_tone FROM nt_sentiment_hourly
                              WHERE label_type = ? AND label_id = ? ORDER BY date, hour""", [label_type, sl])
            dc = _dicts(idb, db)
            lab_int: int | None = None
            theme_bit = -1
            lt = label_type
            if label_type == "country":
                lab_int = next((k for k, v in dc.country.items() if v == label_id.upper()), None)
            elif label_type == "sector":
                lab_int = next((k for k, v in dc.sector.items() if v == label_id.upper()), None)
            elif label_type == "issuer":
                r = idb.one("SELECT iid FROM nt_issuer WHERE issuer_id = ? OR security_id = ?", [label_id, label_id])
                lab_int = int(r["iid"]) if r else None
            elif label_type == "theme":
                theme_bit = next((k for k, v in dc.theme.items() if v == label_id), -2)
                lt, lab_int = "country", 0   # theme series: GLOBAL country rows of that theme (all countries)
            if label_type == "all":
                att = idb.all("""SELECT date, hour, sum(ev_n)::INTEGER AS ev_n, sum(art_n)::INTEGER AS art_n,
                                        avg(art_tone) AS art_tone, max(slots) AS slots FROM nt_event_hourly
                                 WHERE label_type = 'country' AND theme = -1 GROUP BY 1, 2 ORDER BY 1, 2""")
            elif lab_int is not None:
                att = idb.all("""SELECT date, hour, ev_n, art_n, art_tone, slots FROM nt_event_hourly
                                 WHERE label_type = ? AND label_id = ? AND theme = ? ORDER BY date, hour""",
                              [lt, lab_int, theme_bit])

    def ts(r: dict[str, Any]) -> str:
        d = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"]))
        return f"{d.isoformat()}T{int(r['hour']):02d}:00:00Z"

    lo = since.strftime("%Y-%m-%dT%H:00:00Z")
    points: dict[str, dict[str, Any]] = {}
    for r in sent:
        t = ts(r)
        if t >= lo:
            points.setdefault(t, {"ts": t})
            points[t].update(n_items=int(r["n_items"] or 0), n_scored=int(r["n_scored"] or 0),
                             sentiment=_f(r["sentiment_mean"]), pos_share=_f(r["pos_share"], 3),
                             neg_share=_f(r["neg_share"], 3), tier1_n=int(r["tier1_n"] or 0))
    for r in att:
        t = ts(r)
        if t >= lo:
            points.setdefault(t, {"ts": t})
            points[t].update(ev_n=int(r["ev_n"] or 0), art_n=int(r["art_n"] or 0), art_tone=_f(r["art_tone"], 3),
                             slots=int(r["slots"] or 0) if r.get("slots") is not None else None)
    pts = [points[k] for k in sorted(points)]
    return {"data_status": "ok" if pts else "pending", "label_type": label_type, "label_id": label_id, "hours": hours,
            "model_based": True, "points": pts}


# ------------------------------------------------------------------------------------------------ live health
def _feeds_verified() -> dict[str, dict[str, Any]]:
    from app.news_live.feeds import verify_path  # noqa: PLC0415

    try:
        doc = json.loads(verify_path(get_settings()).read_text(encoding="utf-8"))
        return {r["id"]: r for r in doc.get("feeds") or []}
    except (OSError, ValueError):
        return {}


def _feeds_state() -> dict[str, dict[str, Any]]:
    from app.news_live.feeds import state_path  # noqa: PLC0415

    try:
        return json.loads(state_path(get_settings()).read_text(encoding="utf-8")).get("feeds") or {}
    except (OSError, ValueError):
        return {}


@router.get(f"{P}/feeds", operation_id="getNewsFeeds")
def get_feeds() -> dict[str, Any]:
    from app.news_live.feeds import TIER_LABEL, load_feeds  # noqa: PLC0415

    ver, live = _feeds_verified(), _feeds_state()
    items = []
    for f in load_feeds(get_settings(), include_inactive=True):
        v, lv = ver.get(f.id) or {}, live.get(f.id) or {}
        items.append({**f.to_dict(), "tier_label": TIER_LABEL.get(f.tier), "verified": v.get("verified"),
                      "verified_at": v.get("checked_at"), "http_status": v.get("http_status"),
                      "entries": v.get("entries"), "newest_age_h": v.get("newest_age_h"),
                      "verify_reason": v.get("reason"), "robots_checked": v.get("robots"),
                      "etag": v.get("etag"), "last_modified": v.get("last_modified"),
                      "last_poll_at": lv.get("last_poll_at"), "last_ok_at": lv.get("last_ok_at"),
                      "last_error": lv.get("last_error"), "consecutive_errors": lv.get("consecutive_errors"),
                      "polls": lv.get("polls"), "not_modified": lv.get("not_modified"),
                      "items_total": lv.get("items_total"), "failing_since": lv.get("failing_since")})
    return {"data_status": "ok" if items else "pending", "n_ok": sum(1 for x in items if x["status"] == "ok"),
            "n_rejected": sum(1 for x in items if x["status"] == "rejected"), "items": items}


@router.get(f"{P}/live/health", operation_id="getNewsLiveHealth")
def get_live_health() -> dict[str, Any]:
    from app.news_live.compaction import read_state  # noqa: PLC0415
    from app.ops.paths import app_logs_dir, read_json  # noqa: PLC0415
    from app.ops.supervisor import live_news_enabled  # noqa: PLC0415
    from app.realtime.supervisor import read_status  # noqa: PLC0415

    s = get_settings()
    st = read_status(s) or {}
    hb = st.get("heartbeat_at")
    hb_age = (datetime.now(UTC) - datetime.fromisoformat(hb)).total_seconds() if hb else None
    running = bool(st.get("alive")) and hb_age is not None and hb_age < 60
    met = st.get("metrics") or {}
    desk = met.get("newsdesk") or {}
    gd = met.get("gdelt") or {}
    feeds_live = met.get("feeds") or _feeds_state()
    lat = desk.get("sources") or {}
    sources = []
    for name, x in sorted(lat.items()):
        sources.append({"source": name, "tier": x.get("tier"), "items_1h": int(x.get("items_1h") or 0),
                        "last_item_at": x.get("last_item_at"),
                        "publish_to_bus_s_p50": x.get("publish_to_bus_s_p50"),
                        "publish_to_bus_s_p95": x.get("publish_to_bus_s_p95"),
                        "pipeline_s_p50": x.get("pipeline_s_p50"), "pipeline_s_p95": x.get("pipeline_s_p95"),
                        "n": int(x.get("n") or 0)})
    failing = []
    for fid, x in sorted(feeds_live.items()):
        if (x.get("consecutive_errors") or 0) > 0:
            failing.append({"feed": fid, "last_error": x.get("last_error"), "failing_since": x.get("failing_since"),
                            "consecutive_errors": x.get("consecutive_errors"), "last_ok_at": x.get("last_ok_at")})
    polls = [x.get("last_poll_at") for x in feeds_live.values() if x.get("last_poll_at")]
    streams = {k: {"status": v.get("status") if running else "stopped", "detail": v.get("detail"),
                   "last_poll_at": v.get("last_poll_at"), "errors": v.get("errors"), "last_error": v.get("last_error"),
                   "events_1h": v.get("events_1h"), "lag_s_p50": v.get("lag_s_p50"), "lag_s_p95": v.get("lag_s_p95")}
               for k, v in (st.get("streams") or {}).items() if k in ("gdelt", "rss_official", "polygon_news")}
    sup = (read_json(app_logs_dir(s) / "supervisor.json", {}) or {}).get("children", {}).get("realtime") or {}
    nl = read_state(s)
    return {
        "data_status": "ok" if st else "pending", "running": running, "enabled": live_news_enabled(s),
        "heartbeat_at": hb, "pid": st.get("pid"), "rss_mb": st.get("rss_mb"), "ram_cap_mb": sup.get("ram_cap_mb"),
        "supervisor_state": sup.get("state"), "restarts": sup.get("restarts"),
        "overall": desk.get("overall") or {}, "sources": sources, "counts": desk.get("counts") or {},
        "gdelt": {"last_slot": gd.get("last_slot"), "release_lag_s_p50": gd.get("release_lag_s_p50"),
                  "release_lag_s_p95": gd.get("release_lag_s_p95"), "files": gd.get("files") or {}},
        "streams": streams, "feeds_total": len(feeds_live), "feeds_failing": failing,
        "last_feed_poll_at": max(polls) if polls else None,
        "hourly": nl.get("last_hourly"), "finalised": sorted((nl.get("finalised") or {}).keys())[-7:],
        "intraday": intraday_status(), "generated_at": _isoz(datetime.now(UTC)),
    }
