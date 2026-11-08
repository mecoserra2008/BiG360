"""Markets -> Positioning (CFTC): Commitments of Traders positioning across futures markets (contract v1.21.0).

Source: CFTC Commitments of Traders (official CFTC Public Reporting Environment). Serving tables cftc_markets,
cftc_series, cftc_latest, cftc_events (app.api.serving._Builder.cftc). COT index / z-scores are transforms of the
official data; extremes, crowding, divergence and the event study are MODEL-BASED heuristics (labelled).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api import pricebars
from app.api.deps import Db, get_db
from app.api.routers._common import invalid, iso, not_found, rnd

router = APIRouter(tags=["positioning"])

SOURCE = "CFTC Commitments of Traders (official; CFTC Public Reporting Environment)"
MODEL_NOTE = ("COT index = (net - min) / (max - min) over the trailing 52 / 156 weekly reports; extremes (COT index 3y "
              ">= 0.9 or <= 0.1), crowding (extreme and |z 3y| >= 1.5), divergence vs price and the event study are "
              "model-based heuristics, not CFTC data.")
FAMILIES = ("legacy", "disagg", "tff", "cit")
FAMILY_LABEL = {"legacy": "Legacy", "disagg": "Disaggregated", "tff": "Traders in Financial Futures", "cit": "Supplemental (CIT)"}
CATS: dict[str, tuple[str, ...]] = {
    "legacy": ("noncomm", "comm", "nonrept", "spec_hedger"),
    "disagg": ("m_money", "prod_merc", "swap", "other_rept", "nonrept", "spec_hedger"),
    "tff": ("lev_money", "asset_mgr", "dealer", "other_rept", "nonrept", "spec_hedger"),
    "cit": ("cit", "noncomm", "comm", "nonrept", "spec_hedger"),
}
HEADLINE = {"legacy": "noncomm", "disagg": "m_money", "tff": "lev_money", "cit": "cit"}
CAT_LABEL = {
    "noncomm": "Non-commercials", "comm": "Commercials", "prod_merc": "Producers / merchants", "swap": "Swap dealers",
    "m_money": "Managed money", "other_rept": "Other reportables", "dealer": "Dealers", "asset_mgr": "Asset managers",
    "lev_money": "Leveraged funds", "cit": "Index traders (CIT)", "tot_rept": "All reportables",
    "nonrept": "Non-reportables", "spec_hedger": "Speculators - hedgers",
}
SPEC_HEDGER_DEF = {"legacy": "non-commercials - commercials", "disagg": "managed money - (producers + swap dealers)",
                   "tff": "leveraged funds - dealers", "cit": "index traders - commercials"}
ASSET_CLASSES = [("energy", "Energy"), ("metals", "Metals"), ("ags", "Grains & oilseeds"), ("softs", "Softs"),
                 ("livestock", "Livestock & dairy"), ("rates", "Rates"), ("equity", "Equity indices & volatility"),
                 ("fx", "Currencies"), ("crypto", "Crypto"), ("other", "Other")]
_AC_ORDER = {a: i for i, (a, _) in enumerate(ASSET_CLASSES)}
METRICS = ("cot_index_3y", "cot_index_1y", "z_3y", "z_1y", "net_pct_oi", "pct_rank_3y")
_CACHE: dict[Any, Any] = {}


def _rt(fam: str, combined: bool) -> str:
    return "cit_comb" if fam == "cit" else f"{fam}_{'comb' if combined else 'fut'}"


def _cache(key: Any, fn):
    if key in _CACHE:
        return _CACHE[key]
    out = fn()
    if len(_CACHE) > 256:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def _status(db: Db) -> str:
    return db.snap.status("cftc_positions", "cftc_signals", "cftc_markets")


def _knowledge(db: Db, rt: str | None = None) -> dict[str, Any]:
    r = db.one("""SELECT max(report_date) AS rd FROM cftc_latest""" + (" WHERE report_type = ?" if rt else ""), [rt] if rt else [])
    rd = r["rd"] if r else None
    if rd is None:
        return {"report_date": None, "release_date": None, "release_ts": None, "release_estimated": None}
    k = db.one("""SELECT max(knowledge_date) AS kd, max(knowledge_ts) AS kts, bool_or(knowledge_date_estimated) AS est
                  FROM cftc_latest WHERE report_date = ?""", [rd])
    return {"report_date": iso(rd), "release_date": iso(k["kd"]) if k else None,
            "release_ts": iso(k["kts"]) if k else None, "release_estimated": k["est"] if k else None}


_ITEM_SQL = """
    SELECT m.market_code, m.short_name, m.market_name, m.exchange, m.asset_class, m.root, m.security_id,
           m.price_security_id, m.price_is_proxy, m.is_major, m.is_active, m.primary_report, m.report_types,
           m.latest_open_interest, m.contract_units,
           l.report_type, l.category, l.report_date, l.knowledge_date, l.net, l.net_pct_oi, l.chg_1w, l.chg_4w,
           l.cot_index_1y, l.cot_index_3y, l.z_1y, l.z_3y, l.pct_rank_3y, l.extreme, l.crowded, l.divergence_13w,
           l.divergence_flag, l.ret_13w, l.open_interest, sh.cot_index_3y AS spec_hedger_cot_index_3y
    FROM cftc_markets m
    JOIN cftc_latest l ON l.market_code = m.market_code AND l.report_type = {rt} AND l.category = {cat}
    LEFT JOIN cftc_latest sh ON sh.market_code = m.market_code AND sh.report_type = l.report_type
                             AND sh.category = 'spec_hedger'
"""


def _auto_rt(combined: bool) -> tuple[str, str]:
    """SQL expressions (report type, headline category) for each market's primary report family."""
    suf = "comb" if combined else "fut"
    rt = f"(m.primary_report || '_{suf}')"
    cat = "(CASE m.primary_report WHEN 'disagg' THEN 'm_money' WHEN 'tff' THEN 'lev_money' ELSE 'noncomm' END)"
    return rt, cat


def _item(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "market_code": r["market_code"], "name": r["short_name"] or r["market_name"], "market_name": r["market_name"],
        "exchange": r["exchange"], "asset_class": r["asset_class"], "root": r["root"], "security_id": r["security_id"],
        "price_security_id": r["price_security_id"], "price_is_proxy": r["price_is_proxy"], "is_major": bool(r["is_major"]),
        "is_active": bool(r["is_active"]), "primary_report": r["primary_report"], "report_types": list(r["report_types"] or []),
        "report_type": r["report_type"], "category": r["category"], "category_label": CAT_LABEL.get(r["category"], r["category"]),
        "report_date": iso(r["report_date"]), "release_date": iso(r["knowledge_date"]), "net": r["net"],
        "net_pct_oi": rnd(r["net_pct_oi"], 4), "chg_1w": r["chg_1w"], "chg_4w": r["chg_4w"],
        "cot_index_1y": rnd(r["cot_index_1y"], 3), "cot_index_3y": rnd(r["cot_index_3y"], 3), "z_1y": rnd(r["z_1y"], 2),
        "z_3y": rnd(r["z_3y"], 2), "pct_rank_3y": rnd(r["pct_rank_3y"], 3), "extreme": r["extreme"],
        "crowded": r["crowded"], "divergence_13w": rnd(r["divergence_13w"], 2), "divergence_flag": r["divergence_flag"],
        "ret_13w": rnd(r["ret_13w"], 4), "open_interest": r["open_interest"],
        "spec_hedger_cot_index_3y": rnd(r["spec_hedger_cot_index_3y"], 3), "contract_units": r["contract_units"],
    }


def _items(db: Db, report: str, combined: bool, where: list[str], params: list[Any]) -> list[dict[str, Any]]:
    if report == "auto":
        rt, cat = _auto_rt(combined)
        sql = _ITEM_SQL.format(rt=rt, cat=cat)
        p: list[Any] = []
    else:
        sql = _ITEM_SQL.format(rt="?", cat="?")
        p = [_rt(report, combined), HEADLINE[report]]
    if where:
        sql += " WHERE " + " AND ".join(where)
    rows = db.all(sql, p + params)
    rows.sort(key=lambda r: (not r["is_major"], _AC_ORDER.get(r["asset_class"], 99), -(r["open_interest"] or 0)))
    return [_item(r) for r in rows]


def _filters(asset_class: str | None, active: bool | None, major: bool | None, security_id: str | None = None,
             q: str | None = None) -> tuple[list[str], list[Any]]:
    w: list[str] = []
    p: list[Any] = []
    if asset_class:
        if asset_class not in _AC_ORDER:
            raise invalid(f"unknown asset_class {asset_class!r}")
        w.append("m.asset_class = ?")
        p.append(asset_class)
    if active is not None:
        w.append("m.is_active = ?")
        p.append(active)
    if major is not None:
        w.append("m.is_major = ?")
        p.append(major)
    if security_id:
        w.append("(m.security_id = ? OR list_contains(m.linked_security_ids, ?) OR m.price_security_id = ?)")
        p += [security_id, security_id, security_id]
    if q:
        w.append("(lower(m.market_name) LIKE ? OR lower(m.short_name) LIKE ? OR lower(m.market_code) = ? OR lower(m.root) = ?)")
        ql = q.strip().lower()
        p += [f"%{ql}%", f"%{ql}%", ql, ql]
    return w, p


@router.get("/positioning/markets", operation_id="listPositioningMarkets")
def list_markets(asset_class: str | None = None, report: str = Query("auto", pattern="^(auto|legacy|disagg|tff|cit)$"),
                 combined: bool = False, active: bool | None = True, major: bool | None = None,
                 security_id: str | None = None, q: str | None = Query(None, max_length=80),
                 limit: int = Query(200, ge=1, le=500), offset: int = Query(0, ge=0),
                 db: Db = Depends(get_db)) -> dict[str, Any]:
    key = ("markets", db.snap.run_id, asset_class, report, combined, active, major, security_id, q, limit, offset)

    def build() -> dict[str, Any]:
        w, p = _filters(asset_class, active, major, security_id, q)
        items = _items(db, report, combined, w, p)
        w2, p2 = _filters(None, active, major, security_id, q)
        counts = {r["asset_class"]: r["n"] for r in db.all(
            "SELECT m.asset_class, count(*) AS n FROM cftc_markets m" + (" WHERE " + " AND ".join(w2) if w2 else "")
            + " GROUP BY 1", p2)}
        return {**_knowledge(db), "report": report, "combined": combined, "total": len(items),
                "items": items[offset:offset + limit],
                "asset_classes": [{"id": a, "label": lbl, "n": counts.get(a, 0)} for a, lbl in ASSET_CLASSES if counts.get(a)],
                "source": SOURCE, "model_note": MODEL_NOTE, "data_status": _status(db)}

    return _cache(key, build)


@router.get("/positioning/heatmap", operation_id="getPositioningHeatmap")
def heatmap(report: str = Query("legacy", pattern="^(legacy|disagg|tff|cit)$"), combined: bool = False,
            metric: str = Query("cot_index_3y", pattern="^(" + "|".join(METRICS) + ")$"), asset_class: str | None = None,
            major: bool | None = True, active: bool | None = True, db: Db = Depends(get_db)) -> dict[str, Any]:
    key = ("heatmap", db.snap.run_id, report, combined, metric, asset_class, major, active)

    def build() -> dict[str, Any]:
        rt = _rt(report, combined)
        cats = CATS[report]
        w, p = _filters(asset_class, active, major)
        ph = ", ".join("?" * len(cats))
        sql = f"""SELECT m.market_code, m.short_name, m.market_name, m.asset_class, m.is_major, m.latest_open_interest,
                         l.category, l.report_date, l.{metric} AS v, l.extreme, l.crowded
                  FROM cftc_markets m JOIN cftc_latest l ON l.market_code = m.market_code
                  WHERE l.report_type = ? AND l.category IN ({ph})""" + ("".join(f" AND {x}" for x in w))
        rows = db.all(sql, [rt, *cats, *p])
        by: dict[str, dict[str, Any]] = {}
        for r in rows:
            d = by.setdefault(r["market_code"], {"market_code": r["market_code"], "name": r["short_name"] or r["market_name"],
                                                  "asset_class": r["asset_class"], "is_major": bool(r["is_major"]),
                                                  "open_interest": r["latest_open_interest"], "report_date": iso(r["report_date"]),
                                                  "_v": {}, "_x": {}, "_c": {}})
            d["_v"][r["category"]] = rnd(r["v"], 3)
            d["_x"][r["category"]] = r["extreme"]
            d["_c"][r["category"]] = r["crowded"]
            if r["report_date"] and (d["report_date"] is None or iso(r["report_date"]) > d["report_date"]):
                d["report_date"] = iso(r["report_date"])
        out_rows = []
        for d in sorted(by.values(), key=lambda d: (_AC_ORDER.get(d["asset_class"], 99), not d["is_major"], -(d["open_interest"] or 0))):
            out_rows.append({k: v for k, v in d.items() if not k.startswith("_")} | {
                "values": [d["_v"].get(c) for c in cats], "extremes": [d["_x"].get(c) for c in cats],
                "crowded": [bool(d["_c"].get(c)) for c in cats]})
        return {**_knowledge(db, rt), "report": report, "report_type": rt, "metric": metric,
                "categories": [{"id": c, "label": CAT_LABEL[c]} for c in cats], "rows": out_rows,
                "spec_hedger_definition": SPEC_HEDGER_DEF[report], "source": SOURCE, "model_note": MODEL_NOTE,
                "data_status": _status(db)}

    return _cache(key, build)


@router.get("/positioning/crowded", operation_id="getPositioningCrowded")
def crowded(combined: bool = False, asset_class: str | None = None, major: bool | None = None,
            limit: int = Query(15, ge=1, le=100), db: Db = Depends(get_db)) -> dict[str, Any]:
    key = ("crowded", db.snap.run_id, combined, asset_class, major, limit)

    def build() -> dict[str, Any]:
        w, p = _filters(asset_class, True, major)
        items = _items(db, "auto", combined, w, p)
        longs = sorted([x for x in items if x["extreme"] == "long"], key=lambda x: (-(x["cot_index_3y"] or 0), -(x["z_3y"] or 0)))
        shorts = sorted([x for x in items if x["extreme"] == "short"], key=lambda x: ((x["cot_index_3y"] or 0), (x["z_3y"] or 0)))
        div = sorted([x for x in items if x["divergence_flag"]], key=lambda x: -abs(x["divergence_13w"] or 0))
        return {**_knowledge(db), "longs": longs[:limit], "shorts": shorts[:limit], "divergences": div[:limit],
                "n_markets": len(items),
                "definition": "Headline speculator category of each market's primary report (managed money for "
                              "physical commodities, leveraged funds for financials, non-commercials otherwise), "
                              "futures-only unless combined=true. " + MODEL_NOTE,
                "model_based": True, "source": SOURCE, "data_status": _status(db)}

    return _cache(key, build)


def _asof(dates: list[date], bars: list[dict[str, Any]]) -> list[float | None]:
    """Last close on or before each report date (bars sorted by date; stale beyond 10 days -> null)."""
    out: list[float | None] = []
    j, n = -1, len(bars)
    for d in dates:
        while j + 1 < n and bars[j + 1]["date"] <= d:
            j += 1
        if j >= 0 and (d - bars[j]["date"]).days <= 10:
            c = bars[j]["c"]
            out.append(rnd(c, 6) if c is not None else None)
        else:
            out.append(None)
    return out


def _extreme_episodes(dates: list[date], ext: list[str | None], cot: list[float | None]) -> list[dict[str, Any]]:
    eps: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for d, x, c in zip(dates, ext, cot, strict=True):
        if x and cur and cur["side"] == x:
            cur["end"] = iso(d)
            cur["weeks"] += 1
            if c is not None:
                cur["peak_cot_index"] = max(cur["peak_cot_index"], c) if x == "long" else min(cur["peak_cot_index"], c)
            continue
        if cur:
            eps.append(cur)
            cur = None
        if x:
            cur = {"side": x, "start": iso(d), "end": iso(d), "weeks": 1, "peak_cot_index": c if c is not None else (1.0 if x == "long" else 0.0)}
    if cur:
        eps.append(cur)
    return eps


@router.get("/positioning/markets/{market_code}", operation_id="getPositioningMarket")
def market_detail(market_code: str, report: str | None = Query(None, pattern="^(legacy|disagg|tff|cit)$"),
                  combined: bool = False, from_: date | None = Query(None, alias="from"), to: date | None = None,
                  db: Db = Depends(get_db)) -> dict[str, Any]:
    m = db.one("SELECT * FROM cftc_markets WHERE market_code = ?", [market_code])
    if not m:
        raise not_found(f"unknown CFTC market {market_code!r}")
    key = ("detail", db.snap.run_id, market_code, report, combined, from_, to)
    return _cache(key, lambda: _detail(db, m, report, combined, from_, to))


def _detail(db: Db, m: dict[str, Any], report: str | None, combined: bool, from_: date | None, to: date | None) -> dict[str, Any]:
    rts = list(m["report_types"] or [])
    fams = [f for f in FAMILIES if any(rt.startswith(f + "_") for rt in rts)]
    fam = report or m["primary_report"]
    if fam not in fams:
        if report:
            raise not_found(f"{FAMILY_LABEL[report]} report not published for market {m['market_code']}")
        fam = fams[0] if fams else "legacy"
    rt = _rt(fam, combined)
    if rt not in rts:
        rt = _rt(fam, not combined)
    cats = CATS[fam]
    start = from_ or date(1900, 1, 1)
    end = to or date(2100, 1, 1)
    fields = ("net", "long_pos", "short_pos", "spread_pos", "net_pct_oi", "cot_index_1y", "cot_index_3y", "z_3y",
              "traders_long", "traders_short", "extreme")
    mfields = ("open_interest", "traders_total", "conc_net_4_long", "conc_net_4_short", "conc_net_8_long", "conc_net_8_short",
               "conc_gross_4_long", "conc_gross_4_short")
    key = [m["market_code"], rt, start, end]
    # narrow selects + tuples (dict conversion of every serving column dominated the endpoint time)
    db.cur.execute(f"""SELECT report_date, category, {', '.join(fields)} FROM cftc_series
                       WHERE market_code = ? AND report_type = ? AND report_date BETWEEN ? AND ?
                         AND category IN ({', '.join('?' * len(cats))})""", [*key, *cats])
    crow = db.cur.fetchall()
    db.cur.execute(f"""SELECT report_date, {', '.join(mfields)} FROM cftc_series
                       WHERE market_code = ? AND report_type = ? AND report_date BETWEEN ? AND ? AND category = 'tot_rept'""", key)
    mrow = db.cur.fetchall()
    dates = sorted({r[0] for r in crow} | {r[0] for r in mrow})
    di = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    mk: dict[str, list[Any]] = {k: [None] * n for k in mfields}
    for r in mrow:
        i = di[r[0]]
        for j, k in enumerate(mfields, start=1):
            v = r[j]
            mk[k][i] = (round(v, 4) if v == v else None) if isinstance(v, float) else v
    by: dict[str, dict[str, list[Any]]] = {c: {f: [None] * n for f in fields} for c in cats}
    nd = {"net_pct_oi": 4, "cot_index_1y": 3, "cot_index_3y": 3, "z_3y": 2}
    digits = [nd.get(f) for f in fields]
    for r in crow:
        i = di[r[0]]
        b = by[r[1]]
        for j, f in enumerate(fields):
            v = r[j + 2]
            dg = digits[j]
            if dg is not None and v is not None:
                v = round(v, dg) if v == v else None
            b[f][i] = v
    latest: dict[str, dict[str, Any]] = {}
    last_row = None
    if dates:
        for r in db.all("SELECT * FROM cftc_series WHERE market_code = ? AND report_type = ? AND report_date = ?",
                        [m["market_code"], rt, dates[-1]]):
            latest[r["category"]] = r
            if r["category"] == "tot_rept" or last_row is None:
                last_row = r
    bars: list[dict[str, Any]] = []
    price_sid = m["price_security_id"]
    if price_sid and dates:
        bars = pricebars.bars(db, price_sid, dates[0] - timedelta(days=14), dates[-1], "W", True)
    price = _asof(dates, bars) if bars else [None] * n
    ticker = None
    if price_sid:
        s = db.one("SELECT display_ticker, name FROM securities WHERE security_id = ?", [price_sid])
        ticker = {"security_id": price_sid, "ticker": (s or {}).get("display_ticker") or price_sid.split(":", 1)[-1],
                  "name": (s or {}).get("name"), "is_proxy": bool(m["price_is_proxy"])}
    lat = []
    for c in cats:
        r = latest.get(c)
        if not r:
            continue
        lat.append({"category": c, "label": CAT_LABEL[c], "report_date": iso(r["report_date"]), "net": r["net"],
                    "long": r["long_pos"], "short": r["short_pos"], "spread": r["spread_pos"],
                    "net_pct_oi": rnd(r["net_pct_oi"], 4), "chg_1w": r["chg_1w"], "chg_4w": r["chg_4w"],
                    "chg_13w": r["chg_13w"], "flow_z": rnd(r["flow_z"], 2), "cot_index_1y": rnd(r["cot_index_1y"], 3),
                    "cot_index_3y": rnd(r["cot_index_3y"], 3), "z_1y": rnd(r["z_1y"], 2), "z_3y": rnd(r["z_3y"], 2),
                    "pct_rank_3y": rnd(r["pct_rank_3y"], 3), "extreme": r["extreme"], "crowded": r["crowded"],
                    "divergence_13w": rnd(r["divergence_13w"], 2), "divergence_26w": rnd(r["divergence_26w"], 2),
                    "divergence_flag": r["divergence_flag"], "traders_long": r["traders_long"],
                    "traders_short": r["traders_short"]})
    head = HEADLINE[fam]
    episodes = _extreme_episodes(dates, by[head]["extreme"], by[head]["cot_index_3y"]) if head in by else []
    ev = db.all("""SELECT * FROM cftc_events WHERE market_code = ? AND report_type = ? ORDER BY category, signal, horizon_w""",
                [m["market_code"], rt.replace("_comb", "_fut")])
    know = {"report_date": iso(dates[-1]) if dates else None,
            "release_date": iso(last_row["knowledge_date"]) if last_row else None,
            "release_ts": iso(last_row["knowledge_ts"]) if last_row else None,
            "release_estimated": last_row["knowledge_date_estimated"] if last_row else None}
    return {
        "market": {"market_code": m["market_code"], "name": m["short_name"] or m["market_name"], "market_name": m["market_name"],
                   "market_and_exchange": m["market_and_exchange"], "exchange": m["exchange"], "asset_class": m["asset_class"],
                   "commodity_group": m["commodity_group"], "commodity_subgroup": m["commodity_subgroup"],
                   "contract_units": m["contract_units"], "root": m["root"], "security_id": m["security_id"],
                   "is_major": bool(m["is_major"]), "is_active": bool(m["is_active"]), "primary_report": m["primary_report"],
                   "first_report_date": iso(m["first_report_date"]), "last_report_date": iso(m["last_report_date"]),
                   "mapping_method": m["mapping_method"], "linked_security_ids": list(m["linked_security_ids"] or [])},
        "report": fam, "report_label": FAMILY_LABEL[fam], "report_type": rt, "combined": rt.endswith("_comb"),
        "families_available": fams, "report_types_available": rts,
        "categories": [{"id": c, "label": CAT_LABEL[c]} for c in cats], "headline_category": head,
        "spec_hedger_definition": SPEC_HEDGER_DEF[fam],
        "dates": [iso(d) for d in dates], **{k: v for k, v in mk.items()}, "price": price, "price_instrument": ticker,
        "series": [{"category": c, "label": CAT_LABEL[c], "net": by[c]["net"], "long": by[c]["long_pos"],
                    "short": by[c]["short_pos"], "spread": by[c]["spread_pos"], "net_pct_oi": by[c]["net_pct_oi"],
                    "cot_index_1y": by[c]["cot_index_1y"], "cot_index_3y": by[c]["cot_index_3y"], "z_3y": by[c]["z_3y"],
                    "traders_long": by[c]["traders_long"], "traders_short": by[c]["traders_short"]} for c in cats],
        "latest": lat, "extremes": episodes,
        "event_study": [{k: (iso(v) if isinstance(v, date) else (rnd(v, 5) if isinstance(v, float) else v))
                         for k, v in e.items()} | {"label": CAT_LABEL.get(e["category"], e["category"])} for e in ev],
        **know, "source": SOURCE, "model_note": MODEL_NOTE, "data_status": "ok" if dates else _status(db),
    }
