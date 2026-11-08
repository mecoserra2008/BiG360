"""Earnings & corporate-events calendar (roadmap #6; contract tag earnings).

Serving tables (app.api.serving._Builder.earnings): earn_events (released earnings + reaction + primary model
expectation / surprise), earn_calendar (upcoming, labelled by date source), earn_expectations, corp_events,
earn_season, earn_pead. Labels: provider dates = "provider estimate"; cadence dates = "model-based projection";
expectations / surprises = "Model-based expectation (not consensus)".
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api import app_state
from app.api.deps import Db, get_db
from app.api.routers._common import (
    equity_only_sql,
    fnum,
    invalid,
    iso,
    not_found,
    parse_regions,
    regions_query,
    rnd,
    sec_ref,
    sec_ref_sql,
)

router = APIRouter(tags=["earnings"])

LABEL_EXPECTATION = "Model-based expectation (not consensus)"
LABELS = {
    "official_release": "Official earnings release (filing / exchange disclosure time)",
    "official_filing": "Official periodic report filing (may lag the first public release)",
    "provider": "Provider date (Yahoo)",
    "company_announced": "Company-announced (exchange schedule)",
    "provider_estimate": "Provider estimate (Yahoo)",
    "model_projection": "Model-based projection (reporting cadence)",
    "expectation": LABEL_EXPECTATION,
}
MODEL_NOTE = ("Expectations are model-based (Foster seasonal SARIMA / seasonal naive on the company's own first-reported "
              "quarterly series, plus the analytics forecast path), NOT analyst consensus. Reactions are market-model "
              "abnormal returns vs the country index; the baseline move is the realised-volatility expectation (no "
              "option-implied move).")
CORP_GROUPS = {
    "dividend": ("dividend_ex", "dividend_pay", "special_dividend", "stock_dividend"),
    "split": ("split", "spinoff", "rights"),
    "corporate": ("mna_agreement", "mna_completed", "exec_change", "financing", "equity_issue", "other_material",
                  "agm_results", "agm_notice", "buyback", "guidance_revision", "board_meeting", "general_meeting"),
}
EVENT_TYPES = ("earnings",) + tuple(CORP_GROUPS)
MAX_RANGE_DAYS = 400


def _status(db: Db) -> str:
    return db.snap.status("earnings_events", "earnings_calendar")


def _types(types: str | None) -> set[str]:
    if not types:
        return set(EVENT_TYPES)
    out = {t.strip() for t in types.split(",") if t.strip()}
    bad = out - set(EVENT_TYPES)
    if bad:
        raise invalid(f"unknown event type(s) {sorted(bad)}; expected {', '.join(EVENT_TYPES)}")
    return out


def _target_ids(universe: str | None, watchlist_id: str | None, security_ids: str | None) -> list[str] | None:
    ids: set[str] | None = None
    if watchlist_id:
        if watchlist_id == "all":
            ids = set(app_state.all_watch_security_ids())
        else:
            if app_state.get_watchlist(watchlist_id) is None:
                raise not_found(f"unknown watchlist_id {watchlist_id!r}")
            ids = {x["security_id"] for x in app_state.watchlist_items(watchlist_id)}
    if security_ids:
        extra = {x.strip() for x in security_ids.split(",") if x.strip()}
        ids = extra if ids is None else (ids | extra)
    return sorted(ids) if ids is not None else None


def _sec_filter(universe: str | None, ids: list[str] | None, regs: list[str], sector: str | None,
                alias: str = "s") -> tuple[str, list[Any]]:
    w: list[str] = [equity_only_sql(alias)]
    p: list[Any] = []
    if universe:
        w.append(f"{alias}.security_id IN (SELECT security_id FROM universe WHERE universe_id = ?)")
        p.append(universe)
    if ids is not None:
        w.append(f"{alias}.security_id IN ({', '.join('?' for _ in ids) or 'NULL'})")
        p += ids
    if regs:
        w.append(f"{alias}.region IN ({', '.join('?' for _ in regs)})")
        p += regs
    if sector:
        w.append(f"{alias}.sector_id = ?")
        p.append(sector)
    return " AND ".join(w), p


def _earn_item(r: dict[str, Any], upcoming: bool) -> dict[str, Any]:
    if upcoming:
        return {
            "kind": "earnings", "event_type": "earnings", "status": "upcoming", "date": iso(r["expected_date"]),
            "ts": iso(r["expected_ts"]), "session": r["session"], "date_source": r["date_source"],
            "source_label": r["source_label"], "window_lo": iso(r["window_lo"]), "window_hi": iso(r["window_hi"]),
            "period_end": iso(r["period_end"]), "fiscal_period": r["fiscal_period"], "security": sec_ref(r),
            "eps_expected": rnd(r["eps_expected"], 4), "eps_lo80": rnd(r["eps_lo80"], 4), "eps_hi80": rnd(r["eps_hi80"], 4),
            "rev_expected": fnum(r["rev_expected"]), "currency": r.get("rev_currency"),
            "eps_actual": None, "eps_surprise_pct": None, "eps_surprise_z": None, "rev_actual": None,
            "rev_surprise_pct": None, "car_m1_p1": None, "title": None, "amount": None, "ratio": None, "url": None,
        }
    return {
        "kind": "earnings", "event_type": "earnings", "status": "released", "date": iso(r["release_date"]),
        "ts": iso(r["release_ts"]), "session": r["session"], "date_source": r["date_source"],
        "source_label": r["source_label"], "window_lo": None, "window_hi": None, "period_end": iso(r["period_end"]),
        "fiscal_period": r["fiscal_period"], "security": sec_ref(r), "eps_expected": rnd(r["eps_expected"], 4),
        "eps_lo80": rnd(r["eps_lo80"], 4), "eps_hi80": rnd(r["eps_hi80"], 4), "rev_expected": fnum(r["rev_expected"]),
        "currency": r.get("eps_currency"), "eps_actual": rnd(r["eps_actual"], 4),
        "eps_surprise_pct": rnd(r["eps_surprise_pct"], 4), "eps_surprise_z": rnd(r["eps_surprise_z"], 3),
        "rev_actual": fnum(r["rev_actual"]), "rev_surprise_pct": rnd(r["rev_surprise_pct"], 4),
        "car_m1_p1": rnd(r["car_m1_p1"], 4), "title": r["form_type"], "amount": None, "ratio": None, "url": r["url"],
    }


def _corp_item(r: dict[str, Any], as_of: date) -> dict[str, Any]:
    return {
        "kind": "corporate", "event_type": r["event_type"], "status": "upcoming" if r["event_date"] > as_of else "past",
        "date": iso(r["event_date"]), "ts": iso(r["event_ts"]), "session": None, "date_source": None,
        "source_label": r["source_label"], "window_lo": None, "window_hi": None, "period_end": None, "fiscal_period": None,
        "security": sec_ref(r), "eps_expected": None, "eps_lo80": None, "eps_hi80": None, "rev_expected": None,
        "currency": r["currency"], "eps_actual": None, "eps_surprise_pct": None, "eps_surprise_z": None, "rev_actual": None,
        "rev_surprise_pct": None, "car_m1_p1": None, "title": r["title"], "amount": rnd(r["amount"], 6),
        "ratio": rnd(r["ratio"], 6), "url": r["url"],
    }


@router.get("/earnings/calendar", operation_id="getEarningsCalendar")
def get_calendar(from_: date | None = Query(None, alias="from"), to: date | None = None,
                 universe: str | None = Query(None, pattern="^[a-z0-9_]{1,40}$"),
                 watchlist_id: str | None = None, security_ids: str | None = Query(None, max_length=4000),
                 region: str | None = None, regions: list[str] | None = regions_query(), sector: str | None = None,
                 types: str | None = None, date_source: str | None = None,
                 limit: int = Query(3000, ge=1, le=20000), offset: int = Query(0, ge=0), compact: bool = False,
                 db: Db = Depends(get_db)) -> dict[str, Any]:
    """Earnings (upcoming + released) and corporate events in [from, to]; filters by universe, watchlist, region,
    sector, event type (earnings, dividend, split, corporate) and date source."""
    start = from_ or (db.as_of - timedelta(days=3))
    end = to or (start + timedelta(days=35))
    if end < start:
        raise invalid("'to' must be on or after 'from'")
    if (end - start).days > MAX_RANGE_DAYS:
        raise invalid(f"date range is limited to {MAX_RANGE_DAYS} days")
    tset = _types(types)
    dsrc = {x.strip() for x in date_source.split(",")} if date_source else None
    ids = _target_ids(universe, watchlist_id, security_ids)
    regs = parse_regions(region, regions)
    where, params = _sec_filter(universe, ids, regs, sector)
    items: list[dict[str, Any]] = []
    as_of = db.as_of
    if "earnings" in tset:
        for r in db.all(f"""SELECT {sec_ref_sql('s')}, c.* FROM earn_calendar c JOIN sec s ON s.security_id = c.security_id
                            WHERE c.expected_date BETWEEN ? AND ? AND {where} ORDER BY c.expected_date""",
                        [start, end, *params]):
            if dsrc is None or r["date_source"] in dsrc:
                items.append(_earn_item(r, True))
        for r in db.all(f"""SELECT {sec_ref_sql('s')}, e.* FROM earn_events e JOIN sec s ON s.security_id = e.security_id
                            WHERE e.release_date BETWEEN ? AND ? AND {where} ORDER BY e.release_date""",
                        [start, end, *params]):
            if dsrc is None or r["date_source"] in dsrc:
                items.append(_earn_item(r, False))
    ctypes = [t for g in tset if g in CORP_GROUPS for t in CORP_GROUPS[g]]
    if ctypes:
        ph = ", ".join("?" for _ in ctypes)
        for r in db.all(f"""SELECT {sec_ref_sql('s')}, c.* FROM corp_events c JOIN sec s ON s.security_id = c.security_id
                            WHERE c.event_date BETWEEN ? AND ? AND c.event_type IN ({ph}) AND {where}
                            ORDER BY c.event_date""", [start, end, *ctypes, *params]):
            items.append(_corp_item(r, as_of))
    items.sort(key=lambda x: (x["date"], x["kind"] != "earnings", -(x["security"]["market_cap_usd"] or 0)))
    days: dict[str, dict[str, Any]] = defaultdict(lambda: {"n_earnings": 0, "n_dividends": 0, "n_other": 0})
    for it in items:
        d = days[it["date"]]
        if it["kind"] == "earnings":
            d["n_earnings"] += 1
        elif it["event_type"] in CORP_GROUPS["dividend"]:
            d["n_dividends"] += 1
        else:
            d["n_other"] += 1
    total = len(items)
    page = items[offset:offset + limit]
    if compact:  # v1.30 (F30): drop null fields (required SecurityRef fields are kept)
        page = [_compact_item(it) for it in page]
    nxt = offset + limit if offset + limit < total else None
    return {"from": iso(start), "to": iso(end), "as_of": iso(as_of), "total": total, "truncated": nxt is not None,
            "offset": offset, "next_offset": nxt,
            "items": page, "days": [{"date": k, **v} for k, v in sorted(days.items())],
            "labels": LABELS, "model_note": MODEL_NOTE, "data_status": _status(db)}


_SEC_REQUIRED = ("security_id", "issuer_id", "ticker", "name", "region", "country", "currency")


def _compact_item(it: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in it.items() if v is not None}
    sec = it.get("security") or {}
    out["security"] = {k: v for k, v in sec.items() if v is not None or k in _SEC_REQUIRED}
    return out


def _hist_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "period_end": iso(r["period_end"]), "fiscal_period": r["fiscal_period"], "fiscal_year": r["fiscal_year"],
        "period_inferred": bool(r["period_inferred"]), "release_date": iso(r["release_date"]), "release_ts": iso(r["release_ts"]),
        "session": r["session"], "reaction_date": iso(r["trade_reaction_date"] or r["reaction_date"]),
        "date_source": r["date_source"], "source_id": r["source_id"], "source_label": r["source_label"],
        "form_type": r["form_type"], "url": r["url"], "report_filing_date": iso(r["report_filing_date"]),
        "provider_date": iso(r["provider_date"]),
        "car_m1_p1": rnd(r["car_m1_p1"], 5), "car_0_p5": rnd(r["car_0_p5"], 5), "car_p1_p20": rnd(r["car_p1_p20"], 5),
        "bhar_m1_p1": rnd(r["bhar_m1_p1"], 5), "bhar_0_p5": rnd(r["bhar_0_p5"], 5), "bhar_p1_p20": rnd(r["bhar_p1_p20"], 5),
        "ret_m1_p1": rnd(r["ret_m1_p1"], 5), "baseline_move": rnd(r["baseline_move_m1_p1"], 5),
        "move_ratio": rnd(r["move_ratio"], 3), "gap_pct": rnd(r["gap_pct"], 5), "gap_filled": r["gap_filled"],
        "drift_continuation": r["drift_continuation"], "benchmark_id": r["benchmark_id"],
        "eps_model": r["eps_model"], "eps_expected": rnd(r["eps_expected"], 4), "eps_lo80": rnd(r["eps_lo80"], 4),
        "eps_hi80": rnd(r["eps_hi80"], 4), "eps_actual": rnd(r["eps_actual"], 4), "eps_surprise_pct": rnd(r["eps_surprise_pct"], 4),
        "eps_surprise_z": rnd(r["eps_surprise_z"], 3), "rev_model": r["rev_model"], "rev_expected": fnum(r["rev_expected"]),
        "rev_lo80": fnum(r["rev_lo80"]), "rev_hi80": fnum(r["rev_hi80"]), "rev_actual": fnum(r["rev_actual"]),
        "rev_surprise_pct": rnd(r["rev_surprise_pct"], 4), "rev_surprise_z": rnd(r["rev_surprise_z"], 3),
        "currency": r["eps_currency"],
    }


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def vals(k: str) -> list[float]:
        return [x[k] for x in rows if x.get(k) is not None]

    def mean(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 5) if xs else None

    def median(xs: list[float]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        n = len(s)
        return round(s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2, 5)

    def rate(k: str) -> float | None:
        xs = [x[k] for x in rows if x.get(k) is not None]
        return round(sum(1 for v in xs if v) / len(xs), 4) if xs else None

    c1 = vals("car_m1_p1")
    eps = vals("eps_surprise_z")
    return {"n_events": len(rows), "n_reactions": len(c1), "mean_abs_car_m1_p1": mean([abs(v) for v in c1]),
            "median_car_m1_p1": median(c1), "pct_positive_car_m1_p1": round(sum(v > 0 for v in c1) / len(c1), 4) if c1 else None,
            "mean_car_p1_p20": mean(vals("car_p1_p20")), "median_move_ratio": median(vals("move_ratio")),
            "gap_fill_rate": rate("gap_filled"), "drift_continuation_rate": rate("drift_continuation"),
            "pct_beat_eps": round(sum(v > 0 for v in eps) / len(eps), 4) if eps else None,
            "mean_abs_eps_surprise_z": mean([abs(v) for v in eps])}


#: sections of the company earnings answer whose emptiness is explained (api.yaml EarningsCompany.availability)
_NO_STATEMENT_CODES = ("no_source", "needs_key", "source_unavailable")


def earnings_availability(region: str | None, has_px: bool, has_fund: bool, hist: list[dict[str, Any]],
                          nxt: list[dict[str, Any]], exp: list[dict[str, Any]], n_reactions: int,
                          active: bool = True) -> dict[str, Any]:
    """Why each section is empty for this company (COVERAGE_V2 W1: replaces the old universe-wide 'Not covered'):
    a specific reason code + message per empty section, None where the section has data."""
    from app.core import availability as AV  # noqa: PLC0415

    def r(code: str, msg: str) -> dict[str, Any]:
        return AV.make(code, msg).public()

    out: dict[str, Any] = {"next": None, "history": None, "reactions": None, "expectations": None}
    if not hist:
        out["history"] = r("insufficient_data", "No earnings release recorded for this company yet: no regulatory "
                                                "filing, exchange disclosure or provider date matched this listing")
    if not nxt:
        if not active:
            out["next"] = r("not_applicable", "Inactive or delisted: no upcoming earnings")
        elif not hist:
            out["next"] = r("insufficient_data", "No date announced and no release history to project the reporting "
                                                 "cadence from")
        else:
            out["next"] = r("not_yet_published", "No upcoming date announced yet (the projected date has passed: "
                                                 "awaiting the release)")
    if hist and not n_reactions:
        if not has_px:
            out["reactions"] = r("no_source", "No price history for this listing")
        else:
            out["reactions"] = r("insufficient_data", "Reactions need about 250 sessions of prices before a release "
                                                      "(market-model estimation window) and the [-1,+1] window to end")
    if not exp:
        if not has_fund:
            rr = AV.region_reason("eps_surprise", region)
            out["expectations"] = (rr.public() if rr is not None and rr.code in _NO_STATEMENT_CODES else
                                   r("insufficient_data", "No quarterly or half-yearly statements loaded for this "
                                                          "company: model expectations need its reported revenue / EPS"))
        else:
            out["expectations"] = r("insufficient_data", "Statements are loaded, but no quarterly / half-yearly revenue "
                                                         "or EPS with the same period a year earlier (annual-only "
                                                         "reporting or too short a history)")
    return out


@router.get("/companies/{security_id}/earnings", operation_id="getCompanyEarnings")
def get_company_earnings(security_id: str, history: int = Query(40, ge=1, le=120),
                         db: Db = Depends(get_db)) -> dict[str, Any]:
    """Next earnings date (with source label), release history with price reactions and model expectation vs actual,
    reaction statistics, expectation rows per model, corporate events and the latest PEAD signal."""
    r = db.one(f"SELECT {sec_ref_sql('s')}, s.issuer_id FROM sec s WHERE s.security_id = ?", [security_id])
    if r is None:
        raise not_found(f"unknown security_id {security_id!r}")
    iid = r["issuer_id"]
    nxt = [_earn_item(x | {k: v for k, v in r.items() if k.startswith("ref_")}, True) for x in db.all(
        "SELECT * FROM earn_calendar WHERE issuer_id = ? ORDER BY expected_date", [iid])]
    hist_rows = db.all("SELECT * FROM earn_events WHERE issuer_id = ? ORDER BY period_end DESC LIMIT ?", [iid, history])
    hist = [_hist_row(x) for x in hist_rows]
    exp = [{"item_id": x["item_id"], "period_end": iso(x["period_end"]), "fiscal_period": x["fiscal_period"],
            "fiscal_year": x["fiscal_year"], "model": x["model"], "model_spec": x["model_spec"],
            "is_primary": bool(x["is_primary"]), "expectation": fnum(x["expectation"]), "lo80": fnum(x["lo80"]),
            "hi80": fnum(x["hi80"]), "lo95": fnum(x["lo95"]), "hi95": fnum(x["hi95"]), "sigma": fnum(x["sigma"]),
            "n_train": x["n_train"], "currency": x["currency"], "eps_basis": x["eps_basis"], "derived_q4": x["derived_q4"],
            "origin_date": iso(x["origin_date"]), "release_date": iso(x["release_date"]), "actual": fnum(x["actual"]),
            "actual_knowledge_date": iso(x["actual_knowledge_date"]), "surprise": fnum(x["surprise"]),
            "surprise_pct": rnd(x["surprise_pct"], 4), "surprise_z": rnd(x["surprise_z"], 3), "status": x["status"]}
           for x in db.all("""SELECT * FROM earn_expectations WHERE issuer_id = ? AND period_end >= (
                                  SELECT coalesce(min(period_end), DATE '1900-01-01') FROM (
                                      SELECT period_end FROM earn_events WHERE issuer_id = ? ORDER BY period_end DESC LIMIT ?))
                              ORDER BY item_id, period_end, model""", [iid, iid, history])]
    corp = [{"event_id": x["event_id"], "event_type": x["event_type"], "date": iso(x["event_date"]), "ts": iso(x["event_ts"]),
             "title": x["title"], "amount": rnd(x["amount"], 6), "currency": x["currency"], "ratio": rnd(x["ratio"], 6),
             "source_label": x["source_label"], "url": x["url"], "upcoming": x["event_date"] > db.as_of}
            for x in db.all("""SELECT * FROM corp_events WHERE issuer_id = ? AND event_type <> 'dividend_pay'
                               ORDER BY event_date DESC LIMIT 200""", [iid])]
    p = db.one("SELECT * FROM earn_pead WHERE issuer_id = ? ORDER BY signal_date DESC LIMIT 1", [iid])
    pead = None if p is None else {
        "period_end": iso(p["period_end"]), "signal_date": iso(p["signal_date"]), "sue": rnd(p["sue"], 3),
        "rev_surprise_z": rnd(p["rev_surprise_z"], 3), "ear": rnd(p["ear"], 5), "ear_z": rnd(p["ear_z"], 3),
        "pead_score": rnd(p["pead_score"], 3), "decile": p["decile"]}
    stats = _stats(hist)
    ctx = db.one("SELECT * FROM sec WHERE security_id = ?", [security_id]) or {}
    avail = earnings_availability(ctx.get("region"), ctx.get("px_date") is not None, ctx.get("fund_period_end") is not None,
                                  hist, nxt, exp, int(stats.get("n_reactions") or 0),
                                  active=ctx.get("is_active") is not False)
    return {"security": sec_ref(r), "as_of": iso(db.as_of), "next": nxt, "history": hist, "reaction_stats": stats,
            "expectations": exp, "corporate_events": corp, "pead": pead, "labels": LABELS, "model_note": MODEL_NOTE,
            "availability": avail,
            "data_status": _status(db) if (hist or nxt) else "pending" if not db.snap.input_ok("earnings_events") else "ok"}


def _season_row(r: dict[str, Any]) -> dict[str, Any]:
    return {"season": r["season"], "region": r["region"], "sector_id": r["sector_id"], "n_expected": r["n_expected"],
            "n_reported": r["n_reported"], "share_reported": rnd(r["share_reported"], 4), "n_upcoming_7d": r["n_upcoming_7d"],
            "n_eps_surprise": r["n_eps_surprise"], "pct_beat_eps": rnd(r["pct_beat_eps"], 4),
            "median_eps_surprise_z": rnd(r["median_eps_surprise_z"], 3), "n_rev_surprise": r["n_rev_surprise"],
            "pct_beat_rev": rnd(r["pct_beat_rev"], 4), "median_rev_surprise_pct": rnd(r["median_rev_surprise_pct"], 4),
            "n_reactions": r["n_reactions"], "median_car_m1_p1": rnd(r["median_car_m1_p1"], 5),
            "reaction_breadth": rnd(r["reaction_breadth"], 4), "median_abs_move_ratio": rnd(r["median_abs_move_ratio"], 3)}


@router.get("/earnings/season", operation_id="getEarningsSeason")
def get_season(season: str | None = Query(None, pattern=r"^\d{4}Q[1-4]$"), region: str | None = None,
               db: Db = Depends(get_db)) -> dict[str, Any]:
    """Earnings-season dashboard: share reported, model surprises (not consensus) and reaction breadth by region and
    sector, season history, biggest surprises and the next 7 days."""
    seasons = [x["season"] for x in db.all("SELECT DISTINCT season FROM earn_season ORDER BY season DESC")]
    regs = parse_regions(region, None)
    reg = regs[0] if regs else "ALL"
    if not seasons:
        return {"season": season or "", "seasons": [], "region": reg, "as_of": iso(db.as_of), "total": None,
                "by_region": [], "by_sector": [], "history": [], "top_surprises": [], "upcoming": [],
                "labels": LABELS, "model_note": MODEL_NOTE, "data_status": "pending"}
    if season is None:
        # the most recent season with >= 25% reported (else the latest)
        best = db.one("""SELECT season FROM earn_season WHERE region = 'ALL' AND sector_id = 'ALL' AND share_reported >= 0.25
                         ORDER BY season DESC LIMIT 1""")
        season = best["season"] if best else seasons[0]
    elif season not in seasons:
        raise not_found(f"unknown season {season!r}")
    tot = db.one("SELECT * FROM earn_season WHERE season = ? AND region = ? AND sector_id = 'ALL'", [season, reg])
    by_region = [_season_row(x) for x in db.all("""SELECT * FROM earn_season WHERE season = ? AND sector_id = 'ALL'
                                                   AND region <> 'ALL' ORDER BY n_expected DESC""", [season])]
    by_sector = [_season_row(x) for x in db.all("""SELECT * FROM earn_season WHERE season = ? AND region = ? AND sector_id <> 'ALL'
                                                   ORDER BY n_expected DESC""", [season, reg])]
    history = [_season_row(x) for x in db.all("""SELECT * FROM earn_season WHERE region = ? AND sector_id = 'ALL'
                                                 ORDER BY season""", [reg])]
    y, q = int(season[:4]), int(season[-1])
    lo = date(y, 3 * q - 2, 1)
    hi = (date(y + (q == 4), (3 * q) % 12 + 1, 1)) - timedelta(days=1)
    rw, rp = ("s.region = ?", [reg]) if reg != "ALL" else ("TRUE", [])
    tops = db.all(f"""SELECT {sec_ref_sql('s')}, e.* FROM earn_events e JOIN sec s ON s.security_id = e.security_id
                      WHERE e.period_end BETWEEN ? AND ? AND e.eps_surprise_z IS NOT NULL AND {rw}
                      ORDER BY abs(e.eps_surprise_z) * ln(1 + coalesce(s.market_cap_usd, 1e8) / 1e8) DESC LIMIT 30""",
                  [lo, hi, *rp])
    top = [{"security": sec_ref(x), "period_end": iso(x["period_end"]), "fiscal_period": x["fiscal_period"],
            "release_date": iso(x["release_date"]), "eps_surprise_z": rnd(x["eps_surprise_z"], 3),
            "eps_surprise_pct": rnd(x["eps_surprise_pct"], 4), "rev_surprise_pct": rnd(x["rev_surprise_pct"], 4),
            "car_m1_p1": rnd(x["car_m1_p1"], 5)} for x in tops]
    up = db.all(f"""SELECT {sec_ref_sql('s')}, c.* FROM earn_calendar c JOIN sec s ON s.security_id = c.security_id
                    WHERE c.expected_date BETWEEN ? AND ? AND {rw} AND {equity_only_sql('s')}
                    ORDER BY c.expected_date, s.market_cap_usd DESC NULLS LAST LIMIT 60""",
                [db.as_of, db.as_of + timedelta(days=7), *rp])
    return {"season": season, "seasons": seasons, "region": reg, "as_of": iso(db.as_of),
            "total": _season_row(tot) if tot else None, "by_region": by_region, "by_sector": by_sector,
            "history": history, "top_surprises": top, "upcoming": [_earn_item(x, True) for x in up],
            "labels": LABELS, "model_note": MODEL_NOTE, "data_status": _status(db)}
