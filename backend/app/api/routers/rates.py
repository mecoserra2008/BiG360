"""Rates & debt endpoints (tags rates / credit; owner rates-w3; docs/COVERAGE_V2_PLAN.md W3).

Official sovereign curves (yield_curves, integer bp x 100 in the lake -> percent here), the multi-country Curve
monitor (2s10s / 5s30s, spreads vs Bund / UST, real vs nominal, money-market fixings, delayed live yields), generic
curve-point / spread series, sovereign debt (auctions, debt / GDP, deficits, TARGET balances), credit (ICE BofA OAS
by rating / maturity / EM sector, Moody's Aaa / Baa, issuance, FINRA TRACE aggregates when keyed) and issuer debt
derived from filings (Company 360 Debt panel, sector maturity walls). Serving tables built by
app.api.serving._Builder.rates. Licences: ICE BofA / Moody's series are FRED personal-use (labelled); the
refinancing metrics are MODEL-BASED (labelled).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, FastRoute, get_db
from app.api.routers._common import invalid, iso, not_found

router = APIRouter(route_class=FastRoute)

LABELS = {
    "units": "Rates in percent (stored as integer basis points x 100); spreads in basis points",
    "official": "Official sources (Treasuries, central banks, debt agencies, ECB / Eurostat / OECD / IMF)",
    "ice": "ICE BofA indices via FRED: copyrighted, personal use only (licence ICE Data for bank use)",
    "moodys": "Moody's seasoned yields via FRED: copyrighted, personal use only",
    "model_based": "MODEL-BASED: refinancing rate = local 5y government yield + ICE BofA IG / HY OAS proxy",
    "live": "Delayed quote (Yahoo; not for trading; personal use)",
    "euribor": "EURIBOR (EMMI) as republished by Banco de Espana: EMMI terms of use apply",
}
COMPARE = {"1d": 1, "1w": 7, "1m": 30, "3m": 91, "6m": 182, "1y": 365, "3y": 1095, "5y": 1826}
MONEY_SERIES = (
    ("nyfed_rates:SOFR", "SOFR", "US"), ("nyfed_rates:EFFR", "EFFR", "US"), ("nyfed_rates:OBFR", "OBFR", "US"),
    ("nyfed_rates:TGCR", "TGCR", "US"), ("nyfed_rates:BGCR", "BGCR", "US"), ("nyfed_rates:SOFR30A", "SOFR 30d avg", "US"),
    ("fred:SOFR", "SOFR (FRED)", "US"), ("fred:EFFR", "EFFR (FRED)", "US"),
    ("ecb:EST.B.EU000A2X2A25.WT", "EUR STR", "EA"), ("ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "ECB deposit facility", "EA"),
    ("boe:IUDSOIA", "SONIA", "GB"), ("boe:IUDBEDR", "BoE Bank Rate", "GB"), ("boj:FM01.STRDCLUCON", "TONA (call rate)", "JP"),
    ("boc:AVG.INTWO", "CORRA", "CA"), ("fred:DFEDTARU", "Fed funds target (upper)", "US"),
)
LIVE_IDS = (("IDX:^IRX", "US 13-week bill", 3), ("IDX:^FVX", "US 5y", 60), ("IDX:^TNX", "US 10y", 120),
            ("IDX:^TYX", "US 30y", 360))
OAS_SERIES = (
    ("fred:BAMLC0A0CM", "rating", "US IG (master)"), ("fred:BAMLC0A1CAAA", "rating", "AAA"),
    ("fred:BAMLC0A2CAA", "rating", "AA"), ("fred:BAMLC0A3CA", "rating", "A"), ("fred:BAMLC0A4CBBB", "rating", "BBB"),
    ("fred:BAMLH0A0HYM2", "rating", "US HY (master)"), ("fred:BAMLH0A1HYBB", "rating", "BB"),
    ("fred:BAMLH0A2HYB", "rating", "B"), ("fred:BAMLH0A3HYC", "rating", "CCC & lower"),
    ("fred:BAMLC1A0C13Y", "maturity", "1-3y"), ("fred:BAMLC2A0C35Y", "maturity", "3-5y"),
    ("fred:BAMLC3A0C57Y", "maturity", "5-7y"), ("fred:BAMLC4A0C710Y", "maturity", "7-10y"),
    ("fred:BAMLC7A0C1015Y", "maturity", "10-15y"), ("fred:BAMLC8A0C15PY", "maturity", "15y+"),
    ("fred:BAMLEMFSFCRPIOAS", "sector", "EM financials"), ("fred:BAMLEMNSNFCRPIOAS", "sector", "EM non-financials"),
    ("fred:BAMLEMCBPIOAS", "region", "EM corporate"), ("fred:BAMLEMRACRPIASIAOAS", "region", "EM Asia"),
    ("fred:BAMLEMRECRPIEMEAOAS", "region", "EM EMEA"), ("fred:BAMLEMRLCRPILAOAS", "region", "EM Latin America"),
    ("fred:BAMLHE00EHYIOAS", "region", "Euro HY"), ("fred:BAMLEMHBHYCRPIOAS", "region", "EM HY"),
    ("fred:BAMLC0A0CMEY", "yield", "US IG effective yield"), ("fred:BAMLC0A4CBBBEY", "yield", "BBB effective yield"),
    ("fred:BAMLH0A0HYM2EY", "yield", "US HY effective yield"), ("fred:BAMLHE00EHYIEY", "yield", "Euro HY effective yield"),
    ("fred:DAAA", "moodys", "Moody's Aaa yield"), ("fred:DBAA", "moodys", "Moody's Baa yield"),
    ("fred:BAA10Y", "moodys", "Moody's Baa - 10y UST"), ("fred:AAA10Y", "moodys", "Moody's Aaa - 10y UST"),
)
_SID = re.compile(r"^[A-Z0-9_]{2,20}:\d{1,3}:(par|spot|forward|yield)$")


def _has(db: Db, table: str) -> bool:
    return bool(db.scalar("SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [table]))


def _n(db: Db, table: str) -> int:
    return int(db.scalar(f'SELECT count(*) FROM "{table}"') or 0) if _has(db, table) else 0


def _pct(v: Any) -> float | None:
    return None if v is None else round(float(v) / 10000.0, 4)


def _r(v: Any, nd: int = 4) -> float | None:
    return None if v is None else round(float(v), nd)


def _status(*counts: int) -> str:
    have = [c for c in counts if c]
    return "ok" if len(have) == len(counts) else ("partial" if have else "pending")


def _registry(db: Db) -> dict[str, dict[str, Any]]:
    if not _has(db, "rates_registry"):
        return {}
    return {r["curve_id"]: r for r in db.all("SELECT * FROM rates_registry")}


def _meta_out(r: dict[str, Any], m: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    mm = m or {}
    fd = [x["first_date"] for x in mm.values() if x.get("first_date")]
    ld = [x["last_date"] for x in mm.values() if x.get("last_date")]
    return {"curve_id": r["curve_id"], "country": r["country"], "label": r["label"], "kind": r["kind"],
            "types": [t for t in str(r["types"] or "").split(",") if t],
            "tenors": [int(t) for t in str(r["tenors"] or "").split(",") if t],
            "source_id": r["source_id"], "source": r["source"], "frequency": r["frequency"],
            "history_start": r["history_start"], "group": r["grp"], "licence": r["licence"], "notes": r["notes"] or None,
            "publish": r["publish"], "monitor_type": r["monitor_type"], "is_country_benchmark": bool(r["is_country_benchmark"]),
            "first_date": iso(min(fd)) if fd else None, "last_date": iso(max(ld)) if ld else None,
            "n_dates": max((int(x.get("n_dates") or 0) for x in mm.values()), default=0),
            "status": "ok" if ld else "no_data"}


# =============================================================================================== curves


@router.get("/rates/curves", operation_id="listRatesCurves", tags=["rates"])
def list_curves(group: str | None = Query(None, description="sovereign | money_market | lt10"),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.rates.auctions import REJECTED as AR  # noqa: PLC0415
    from app.rates.sources import REJECTED as SR  # noqa: PLC0415

    reg = _registry(db)
    meta: dict[str, dict[str, dict[str, Any]]] = {}
    if _has(db, "yc_meta"):
        for m in db.all("SELECT * FROM yc_meta"):
            meta.setdefault(m["curve_id"], {})[m["type"]] = m
    items = [_meta_out(r, meta.get(cid)) for cid, r in reg.items() if group is None or r["grp"] == group]
    items.sort(key=lambda x: (x["group"] != "sovereign", x["group"], x["country"], x["curve_id"]))
    rejected = [{"source": k, "reason": v} for k, v in {**SR, **AR}.items()]
    n_ok = sum(1 for x in items if x["status"] == "ok")
    return {"items": items, "n_curves": len(items), "n_with_data": n_ok, "rejected_sources": rejected,
            "labels": LABELS, "data_status": "ok" if n_ok else ("partial" if items else "pending")}


def _nearest_date(db: Db, cid: str, typ: str, d: date) -> date | None:
    v = db.scalar("SELECT max(date) FROM yc WHERE curve_id = ? AND type = ? AND date <= ?", [cid, typ, d])
    return v if isinstance(v, date) else (date.fromisoformat(v) if v else None)


def _points(db: Db, cid: str, typ: str, d: date) -> list[dict[str, Any]]:
    rows = db.all("SELECT tenor_months, value_bp_x100 FROM yc WHERE curve_id = ? AND type = ? AND date = ? "
                  "ORDER BY tenor_months", [cid, typ, d])
    return [{"tenor_months": int(r["tenor_months"]), "value": _pct(r["value_bp_x100"])} for r in rows]


def _parse_date(v: str | None, name: str) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError as e:
        raise invalid(f"{name} must be YYYY-MM-DD") from e


@router.get("/rates/curves/{curve_id}", operation_id="getRatesCurve", tags=["rates"])
def get_curve(curve_id: str, type: str | None = Query(None, description="par | spot | forward | yield"),  # noqa: A002
              date_: str | None = Query(None, alias="date", description="Anchor date (default: latest)"),
              compare: str = Query("1w,1m,1y", description="Comparison snapshots: 1d,1w,1m,3m,6m,1y,3y,5y or ISO dates"),
              history_tenors: str | None = Query(None, description="Tenors (months) for the history panel, comma-separated"),
              from_: str | None = Query(None, alias="from"), to: str | None = Query(None),
              frames: str = Query("M", pattern="^(M|W|none)$", description="Animation frames: month-end (M), week-end (W) or none"),
              db: Db = Depends(get_db)) -> dict[str, Any]:
    reg = _registry(db)
    r = reg.get(curve_id)
    if r is None:
        raise not_found(f"unknown curve {curve_id!r}")
    types = [t for t in str(r["types"]).split(",") if t]
    typ = type or r["monitor_type"] or types[0]
    if typ not in types:
        raise invalid(f"type must be one of {types}")
    meta = {m["type"]: m for m in db.all("SELECT * FROM yc_meta WHERE curve_id = ?", [curve_id])} if _has(db, "yc_meta") else {}
    out: dict[str, Any] = {"curve": _meta_out(r, meta), "type": typ, "snapshots": [], "history": [], "frames": None,
                           "params": None, "labels": LABELS}
    last = _nearest_date(db, curve_id, typ, date.today() + timedelta(days=1))
    if last is None:
        out["data_status"] = "pending"
        return out
    anchor = _parse_date(date_, "date") or last
    d0 = _nearest_date(db, curve_id, typ, anchor)
    if d0 is None:
        raise invalid(f"no {curve_id} curve on or before {anchor}")
    out["snapshots"].append({"label": "latest" if d0 == last else d0.isoformat(), "date": iso(d0),
                             "points": _points(db, curve_id, typ, d0)})
    for c in [x.strip() for x in compare.split(",") if x.strip()][:8]:
        if c in COMPARE:
            want = d0 - timedelta(days=COMPARE[c])
        else:
            want = _parse_date(c, "compare")
        dx = _nearest_date(db, curve_id, typ, want)
        if dx is not None and dx != d0:
            out["snapshots"].append({"label": c, "date": iso(dx), "points": _points(db, curve_id, typ, dx)})
    tenors_avail = [int(t["tenor_months"]) for t in db.all(
        "SELECT DISTINCT tenor_months FROM yc WHERE curve_id = ? AND type = ? ORDER BY 1", [curve_id, typ])]
    ht = [int(x) for x in (history_tenors or "").split(",") if x.strip().isdigit()] or \
        [t for t in (24, 60, 120, 360) if t in tenors_avail] or tenors_avail[:2]
    f = _parse_date(from_, "from") or (d0 - timedelta(days=5 * 365))
    t_ = _parse_date(to, "to") or d0
    span = (t_ - f).days
    weekly = span > 3 * 365
    for ten in ht[:6]:
        if weekly:
            rows = db.all("""SELECT max(date) AS date, arg_max(value_bp_x100, date) AS v FROM yc
                             WHERE curve_id = ? AND type = ? AND tenor_months = ? AND date BETWEEN ? AND ?
                             GROUP BY date_trunc('week', date) ORDER BY 1""", [curve_id, typ, ten, f, t_])
        else:
            rows = db.all("""SELECT date, value_bp_x100 AS v FROM yc WHERE curve_id = ? AND type = ? AND tenor_months = ?
                             AND date BETWEEN ? AND ? ORDER BY date""", [curve_id, typ, ten, f, t_])
        out["history"].append({"tenor_months": ten, "frequency": "W" if weekly else "D",
                               "points": [{"date": iso(x["date"]), "value": _pct(x["v"])} for x in rows]})
    if frames != "none":
        unit = "month" if frames == "M" else "week"
        fr_from = _parse_date(from_, "from") or date.fromisoformat(str(iso(meta.get(typ, {}).get("first_date") or f)))
        rows = db.all(f"""WITH e AS (SELECT max(date) AS d FROM yc WHERE curve_id = ? AND type = ? AND date BETWEEN ? AND ?
                                     GROUP BY date_trunc('{unit}', date))
                          SELECT y.date, y.tenor_months, y.value_bp_x100 FROM yc y JOIN e ON y.date = e.d
                          WHERE y.curve_id = ? AND y.type = ? ORDER BY y.date, y.tenor_months""",
                      [curve_id, typ, fr_from, t_, curve_id, typ])
        dates: list[Any] = []
        idx = {t: i for i, t in enumerate(tenors_avail)}
        vals: list[list[float | None]] = []
        for x in rows:
            if not dates or dates[-1] != x["date"]:
                dates.append(x["date"])
                vals.append([None] * len(tenors_avail))
            i = idx.get(int(x["tenor_months"]))
            if i is not None:
                vals[-1][i] = _pct(x["value_bp_x100"])
        if len(dates) > 700:   # keep payloads bounded (~50 years of month-ends)
            step = len(dates) // 700 + 1
            dates, vals = dates[::step] + ([dates[-1]] if (len(dates) - 1) % step else []), \
                vals[::step] + ([vals[-1]] if (len(vals) - 1) % step else [])
        out["frames"] = {"frequency": frames, "tenors": tenors_avail, "dates": [iso(d) for d in dates], "values": vals}
    if _has(db, "yc_params"):
        p = db.one("SELECT * FROM yc_params WHERE curve_id = ? AND date <= ? ORDER BY date DESC LIMIT 1", [curve_id, d0])
        if p:
            out["params"] = {"date": iso(p["date"]), "model": p["model"], "beta0": _r(p["beta0"], 6), "beta1": _r(p["beta1"], 6),
                             "beta2": _r(p["beta2"], 6), "beta3": _r(p["beta3"], 6), "tau1": _r(p["tau1"], 6),
                             "tau2": _r(p["tau2"], 6), "source": p["source"]}
    out["data_status"] = "ok"
    return out


# =============================================================================================== monitor


def _value_at(db: Db, cid: str, typ: str, ten: int, d: date) -> tuple[date | None, float | None]:
    r = db.one("""SELECT date, value_bp_x100 FROM yc WHERE curve_id = ? AND type = ? AND tenor_months = ? AND date <= ?
                  ORDER BY date DESC LIMIT 1""", [cid, typ, ten, d])
    return (r["date"], _pct(r["value_bp_x100"])) if r else (None, None)


def _bp(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else round((a - b) * 100, 1)


@router.get("/rates/monitor", operation_id="getRatesMonitor", tags=["rates"])
def get_monitor(tenor: int = Query(120, ge=1, le=480, description="Tenor (months) for the spread / change columns"),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    reg = _registry(db)
    n_yc = _n(db, "yc")
    countries: list[dict[str, Any]] = []
    bench = {r["country"]: r for r in reg.values() if r["is_country_benchmark"]}
    base = {}
    for cc in ("DE", "US"):
        rb = bench.get(cc)
        if rb and n_yc:
            base[cc] = _value_at(db, rb["curve_id"], rb["monitor_type"], tenor, date.today())[1]
    for cc, r in sorted(bench.items()):
        if not n_yc:
            break
        cid, typ = r["curve_id"], r["monitor_type"]
        last = _nearest_date(db, cid, typ, date.today())
        if last is None:
            continue
        vals = {t: v for t, v in ((p["tenor_months"], p["value"]) for p in _points(db, cid, typ, last))}
        cur = vals.get(tenor)
        ch = {}
        for lab, days in (("1d", 1), ("1w", 7), ("1m", 30), ("1y", 365)):
            ch[lab] = _bp(cur, _value_at(db, cid, typ, tenor, last - timedelta(days=days))[1])
        ytd = _value_at(db, cid, typ, tenor, date(last.year - 1, 12, 31))[1]
        countries.append({"country": cc, "curve_id": cid, "label": r["label"], "type": typ, "date": iso(last),
                          "y2": vals.get(24), "y5": vals.get(60), "y10": vals.get(120), "y30": vals.get(360),
                          "value": cur, "tenor_months": tenor, "s2s10_bp": _bp(vals.get(120), vals.get(24)),
                          "s5s30_bp": _bp(vals.get(360), vals.get(60)),
                          "spread_vs_bund_bp": _bp(cur, base.get("DE")) if cc != "DE" else None,
                          "spread_vs_ust_bp": _bp(cur, base.get("US")) if cc != "US" else None,
                          "chg_1d_bp": ch["1d"], "chg_1w_bp": ch["1w"], "chg_1m_bp": ch["1m"], "chg_1y_bp": ch["1y"],
                          "chg_ytd_bp": _bp(cur, ytd), "frequency": r["frequency"]})
    real: list[dict[str, Any]] = []
    for cc, nom, typ, rl in (("US", "US_TSY", "par", "US_TSY_REAL"), ("GB", "GB_GILT", "spot", "GB_GILT_REAL")):
        if not n_yc:
            break
        d = _nearest_date(db, rl, typ, date.today())
        if d is None:
            continue
        rv = {p["tenor_months"]: p["value"] for p in _points(db, rl, typ, d)}
        nv = {p["tenor_months"]: p["value"] for p in _points(db, nom, typ, d)}
        for t in sorted(set(rv) & set(nv)):
            if t in (60, 120, 240, 360):
                real.append({"country": cc, "nominal_curve": nom, "real_curve": rl, "date": iso(d), "tenor_months": t,
                             "nominal": nv[t], "real": rv[t], "breakeven_bp": _bp(nv[t], rv[t])})
    lt10 = []
    if _has(db, "rates_country"):
        for x in db.all("SELECT * FROM rates_country WHERE curve_id LIKE '%_LT10' ORDER BY country"):
            lt10.append({"country": x["country"], "curve_id": x["curve_id"], "date": iso(x["date"]), "y10": _r(x["y10"] * 100 if x["y10"] is not None else None),
                         "spread_vs_bund_bp": _bp(x["y10"] * 100 if x["y10"] is not None else None, base.get("DE"))})
    money = _money(db)
    live = _live(db)
    return {"as_of": max((c["date"] for c in countries), default=None), "tenor_months": tenor, "countries": countries,
            "real_vs_nominal": real, "lt10": lt10, "money_market": money, "live": live, "labels": LABELS,
            "data_status": _status(len(countries), len(money))}


def _money(db: Db) -> list[dict[str, Any]]:
    if not _has(db, "macro_obs"):
        return []
    ids = [s for s, _, _ in MONEY_SERIES]
    q = ", ".join("?" for _ in ids)
    rows = db.all(f"""WITH x AS (SELECT series_id, date, value, row_number() OVER (PARTITION BY series_id ORDER BY date DESC) AS rk
                                 FROM macro_obs WHERE series_id IN ({q}) AND value IS NOT NULL)
                      SELECT series_id, max(date) FILTER (rk = 1) AS date, max(value) FILTER (rk = 1) AS value,
                             max(value) FILTER (rk = 2) AS prev, max(value) FILTER (rk = 22) AS m1
                      FROM x WHERE rk <= 22 GROUP BY 1""", ids)
    by = {r["series_id"]: r for r in rows}
    out = []
    seen_lab: set[str] = set()
    for sid, lab, cc in MONEY_SERIES:
        r = by.get(sid)
        if r is None or r["value"] is None:
            continue
        base_lab = lab.split(" (")[0]
        if base_lab in seen_lab and "(FRED)" in lab:
            continue
        seen_lab.add(base_lab)
        out.append({"series_id": sid, "label": lab, "country": cc, "date": iso(r["date"]), "value": _r(r["value"]),
                    "chg_1d_bp": _bp(r["value"], r["prev"]), "chg_1m_bp": _bp(r["value"], r["m1"])})
    if _has(db, "yc"):
        for ten in (1, 3, 6, 12):
            r = db.one("""SELECT date, value_bp_x100 FROM yc WHERE curve_id = 'EA_EURIBOR' AND tenor_months = ?
                          ORDER BY date DESC LIMIT 2""", [ten])
            if r:
                out.append({"series_id": f"EA_EURIBOR:{ten}:yield", "label": f"EURIBOR {ten}M", "country": "EA",
                            "date": iso(r["date"]), "value": _pct(r["value_bp_x100"]), "chg_1d_bp": None, "chg_1m_bp": None})
    return out


def _live(db: Db) -> dict[str, Any]:
    try:
        from app.api.live_quotes import quotes  # noqa: PLC0415

        q = quotes([i for i, _, _ in LIVE_IDS], None, db, record=True)
    except Exception as e:  # noqa: BLE001 - the delayed quotes are advisory
        return {"data_status": "pending", "reason": f"{type(e).__name__}", "items": [], "label": LABELS["live"]}
    by = {it["id"]: it for it in q.get("items") or []}
    items = []
    for i, lab, ten in LIVE_IDS:
        it = by.get(i)
        if it is None:
            continue
        items.append({"id": i, "label": lab, "tenor_months": ten, "value": _r(it.get("price")),
                      "change_bp": _r((it.get("change") or 0) * 100, 1) if it.get("change") is not None else None,
                      "quote_ts": it.get("quote_ts"), "market_open": bool(it.get("market_open")), "stale": bool(it.get("stale"))})
    return {"data_status": "ok" if items else "pending", "reason": q.get("reason"), "items": items, "label": LABELS["live"]}


# =============================================================================================== series / spreads


def _sid(s: str) -> tuple[str, int, str]:
    s = s.strip()
    if not _SID.match(s):
        raise invalid(f"series id {s!r} must be CURVE:TENOR_MONTHS:TYPE (e.g. US_TSY:120:par)")
    a, b, c = s.split(":")
    return a, int(b), c


@router.get("/rates/series", operation_id="getRatesSeries", tags=["rates"])
def get_series(ids: str = Query("", description="Curve points CURVE:TENOR:TYPE, comma-separated (max 8)"),
               spreads: str = Query("", description="Spreads A-B of two curve points (bp), comma-separated (max 6)"),
               from_: str | None = Query(None, alias="from"), to: str | None = Query(None),
               db: Db = Depends(get_db)) -> dict[str, Any]:
    f = _parse_date(from_, "from") or date.today() - timedelta(days=5 * 365)
    t = _parse_date(to, "to") or date.today()
    want = [x for x in ids.split(",") if x.strip()][:8]
    sps = [x for x in spreads.split(",") if x.strip()][:6]
    if not want and not sps:
        raise invalid("give ids or spreads")
    weekly = (t - f).days > 3 * 365

    def load(cid: str, ten: int, typ: str) -> dict[date, float]:
        if weekly:
            rows = db.all("""SELECT max(date) AS date, arg_max(value_bp_x100, date) AS v FROM yc WHERE curve_id = ? AND type = ?
                             AND tenor_months = ? AND date BETWEEN ? AND ? GROUP BY date_trunc('week', date)""", [cid, typ, ten, f, t])
        else:
            rows = db.all("""SELECT date, value_bp_x100 AS v FROM yc WHERE curve_id = ? AND type = ? AND tenor_months = ?
                             AND date BETWEEN ? AND ?""", [cid, typ, ten, f, t])
        return {x["date"]: x["v"] / 10000.0 for x in rows if x["v"] is not None}
    series = []
    for s in want:
        cid, ten, typ = _sid(s)
        d = load(cid, ten, typ)
        series.append({"id": s, "kind": "level", "unit": "percent",
                       "points": [{"date": iso(k), "value": round(v, 4)} for k, v in sorted(d.items())]})
    for sp in sps:
        if "-" not in sp:
            raise invalid(f"spread {sp!r} must be A-B")
        a, b = sp.split("-", 1)
        da, dbb = load(*_sid(a)), load(*_sid(b))
        if weekly:   # align weekly buckets on the ISO week
            ka = {(k.isocalendar()[0], k.isocalendar()[1]): (k, v) for k, v in da.items()}
            kb = {(k.isocalendar()[0], k.isocalendar()[1]): v for k, v in dbb.items()}
            pts = [(ka[w][0], (ka[w][1] - kb[w]) * 100) for w in sorted(set(ka) & set(kb))]
        else:
            pts = [(k, (da[k] - dbb[k]) * 100) for k in sorted(set(da) & set(dbb))]
        series.append({"id": sp, "kind": "spread", "unit": "bp",
                       "points": [{"date": iso(k), "value": round(v, 2)} for k, v in pts]})
    return {"from": iso(f), "to": iso(t), "frequency": "W" if weekly else "D", "series": series, "labels": LABELS,
            "data_status": "ok" if any(s["points"] for s in series) else "pending"}


# =============================================================================================== sovereign


def _obs_latest(db: Db, like: str) -> list[dict[str, Any]]:
    if not _has(db, "macro_obs"):
        return []
    return db.all("""SELECT series_id, date, value FROM macro_obs WHERE series_id LIKE ? AND value IS NOT NULL
                     QUALIFY row_number() OVER (PARTITION BY series_id ORDER BY date DESC) = 1""", [like])


@router.get("/rates/sovereign", operation_id="getRatesSovereign", tags=["rates"])
def get_sovereign(db: Db = Depends(get_db)) -> dict[str, Any]:
    today = date.today()
    up, recent, us10 = [], [], []
    if _has(db, "sov_auctions"):
        for r in db.all("""SELECT * FROM sov_auctions WHERE status = 'announced' AND auction_date >= ?
                           ORDER BY auction_date, agency LIMIT 60""", [today - timedelta(days=1)]):
            up.append(_auction(r))
        for r in db.all("""SELECT * FROM sov_auctions WHERE status = 'results' AND auction_date >= ?
                           ORDER BY auction_date DESC LIMIT 60""", [today - timedelta(days=45)]):
            recent.append(_auction(r))
        for r in db.all("""SELECT auction_date, high_yield_bp_x100, bid_to_cover, indirect_share FROM sov_auctions
                           WHERE agency = 'us_treasury' AND security_type = 'Note' AND term_months = 120 AND status = 'results'
                             AND auction_date >= ? ORDER BY auction_date""", [today - timedelta(days=10 * 365)]):
            us10.append({"date": iso(r["auction_date"]), "high_yield": _pct(r["high_yield_bp_x100"]),
                         "bid_to_cover": _r(r["bid_to_cover"], 3), "indirect_share": _r(r["indirect_share"], 4)})
    debt = []
    for r in _obs_latest(db, "eurostat:gov_10q_ggdebt.%"):
        debt.append({"country": r["series_id"].split(".")[1], "measure": "debt_gdp", "source": "eurostat",
                     "date": iso(r["date"]), "value": _r(r["value"], 2)})
    for r in _obs_latest(db, "eurostat:gov_10q_ggnfa.%"):
        debt.append({"country": r["series_id"].split(".")[1], "measure": "deficit_gdp", "source": "eurostat",
                     "date": iso(r["date"]), "value": _r(r["value"], 2)})
    for r in _obs_latest(db, "imf:WEO.%.GGXWDG_NGDP"):
        debt.append({"country": r["series_id"].split(".")[1], "measure": "debt_gdp", "source": "imf_weo",
                     "date": iso(r["date"]), "value": _r(r["value"], 2)})
    for r in _obs_latest(db, "imf:WEO.%.GGXCNL_NGDP"):
        debt.append({"country": r["series_id"].split(".")[1], "measure": "deficit_gdp", "source": "imf_weo",
                     "date": iso(r["date"]), "value": _r(r["value"], 2)})
    tgt = []
    if _has(db, "macro_obs"):
        for r in db.all("""SELECT series_id, date, value FROM macro_obs WHERE series_id LIKE 'ecb:TGB.M.%' AND date >= ?
                           ORDER BY series_id, date""", [today - timedelta(days=10 * 365)]):
            tgt.append({"country": r["series_id"].split(".")[2], "date": iso(r["date"]), "value_eur_mn": _r(r["value"], 1)})
    return {"auctions_upcoming": up, "auctions_recent": recent, "us_10y_auctions": us10, "debt": debt,
            "target2": tgt, "labels": LABELS, "data_status": _status(len(up) + len(recent), len(debt))}


def _auction(r: dict[str, Any]) -> dict[str, Any]:
    return {"agency": r["agency"], "country": r["country"], "auction_date": iso(r["auction_date"]),
            "security_id": r["security_id"], "security_type": r["security_type"], "security_term": r["security_term"],
            "term_months": r["term_months"], "status": r["status"], "issue_date": iso(r["issue_date"]),
            "maturity_date": iso(r["maturity_date"]), "coupon": _pct(r["coupon_bp_x100"]),
            "high_yield": _pct(r["high_yield_bp_x100"]), "offering_amount": _r(r["offering_amount"], 0),
            "accepted_amount": _r(r["accepted_amount"], 0), "bid_to_cover": _r(r["bid_to_cover"], 3),
            "indirect_share": _r(r["indirect_share"], 4), "currency": r["currency"], "method": r["method"]}


# =============================================================================================== credit


@router.get("/credit/overview", operation_id="getCreditOverview", tags=["credit"])
def get_credit(years: int = Query(5, ge=1, le=40), db: Db = Depends(get_db)) -> dict[str, Any]:
    today = date.today()
    f = today - timedelta(days=int(years * 365.25))
    series = []
    if _has(db, "macro_obs"):
        ids = [s for s, _, _ in OAS_SERIES]
        q = ", ".join("?" for _ in ids)
        hist: dict[str, list[dict[str, Any]]] = {}
        for r in db.all(f"""SELECT series_id, max(date) AS date, arg_max(value, date) AS v FROM macro_obs
                            WHERE series_id IN ({q}) AND date >= ? AND value IS NOT NULL
                            GROUP BY series_id, date_trunc('week', date) ORDER BY 1, 2""", [*ids, f]):
            hist.setdefault(r["series_id"], []).append({"date": iso(r["date"]), "value": _r(r["v"], 3)})
        last = {r["series_id"]: r for r in db.all(f"""SELECT series_id, date, value,
                    lag(value, 5) OVER w AS w1, lag(value, 21) OVER w AS m1, lag(value, 252) OVER w AS y1
                    FROM macro_obs WHERE series_id IN ({q}) AND value IS NOT NULL AND date >= ?
                    WINDOW w AS (PARTITION BY series_id ORDER BY date)
                    QUALIFY row_number() OVER (PARTITION BY series_id ORDER BY date DESC) = 1""", [*ids, today - timedelta(days=400)])}
        for sid, grp, lab in OAS_SERIES:
            r = last.get(sid)
            if r is None:
                continue
            series.append({"series_id": sid, "group": grp, "label": lab, "date": iso(r["date"]), "value": _r(r["value"], 3),
                           "unit": "percent", "chg_1w_bp": _bp(r["value"], r["w1"]), "chg_1m_bp": _bp(r["value"], r["m1"]),
                           "chg_1y_bp": _bp(r["value"], r["y1"]),
                           "licence": LABELS["moodys"] if grp == "moodys" else LABELS["ice"],
                           "history": hist.get(sid, [])})
    eu = []
    if _has(db, "bond_issuance_m"):
        for r in db.all("""SELECT month, currency, sum(n) AS n, sum(amount) AS amount FROM bond_issuance_m
                           WHERE currency IN ('EUR', 'USD', 'GBP') AND month >= ? GROUP BY 1, 2 ORDER BY 1, 2""", [f]):
            eu.append({"month": iso(r["month"]), "currency": r["currency"], "n": int(r["n"]), "amount": _r(r["amount"], 0)})
    us = []
    if _has(db, "sov_auctions"):
        for r in db.all("""SELECT CAST(date_trunc('month', auction_date) AS DATE) AS month, security_type,
                                  sum(accepted_amount) AS amount, count(*) AS n FROM sov_auctions
                           WHERE agency = 'us_treasury' AND status = 'results' AND auction_date >= ? GROUP BY 1, 2 ORDER BY 1, 2""", [f]):
            us.append({"month": iso(r["month"]), "security_type": r["security_type"], "n": int(r["n"]),
                       "amount": _r(r["amount"], 0)})
    trace = []
    if _has(db, "credit_stats"):
        for r in db.all("""SELECT dataset, category, metric, date, value FROM credit_stats
                           QUALIFY row_number() OVER (PARTITION BY dataset, category, metric ORDER BY date DESC) = 1
                           ORDER BY dataset, category, metric LIMIT 300"""):
            trace.append({"dataset": r["dataset"], "category": r["category"], "metric": r["metric"], "date": iso(r["date"]),
                          "value": _r(r["value"], 4)})
    walls = _walls(db, "ALL", "ALL")
    return {"series": series, "issuance_eu_firds": eu, "issuance_us_treasury": us, "trace": trace,
            "trace_status": "ok" if trace else "needs_key", "maturity_wall": walls, "labels": LABELS,
            "data_status": _status(len(series), len(eu) + len(us))}


def _walls(db: Db, region: str, sector: str) -> list[dict[str, Any]]:
    if not _has(db, "debt_walls"):
        return []
    rows = db.all("SELECT * FROM debt_walls WHERE region = ? AND sector_id = ? ORDER BY bucket", [region, sector])
    order = ["y1", "y2", "y3", "y4", "y5", "after5", "total"]
    rows.sort(key=lambda r: order.index(r["bucket"]) if r["bucket"] in order else 99)
    return [{"bucket": r["bucket"], "amount_usd": _r(r["amount_usd"], 0), "share": _r(r["share"], 4),
             "n_issuers": int(r["n_issuers"] or 0), "n_issuers_debt": r.get("n_issuers_debt"),
             "total_debt_usd": _r(r.get("total_debt_usd"), 0), "wavg_cost_of_debt": _r(r.get("wavg_cost_of_debt"), 5),
             "median_interest_coverage": _r(r.get("median_interest_coverage"), 3),
             "median_net_debt_to_ebitda": _r(r.get("median_net_debt_to_ebitda"), 3),
             "refi_share_24m": _r(r.get("refi_share_24m"), 4), "floating_share": _r(r.get("floating_share"), 4),
             "refi_cost_24m_usd": _r(r.get("refi_cost_24m_usd"), 0), "as_of": iso(r["as_of"])} for r in rows]


@router.get("/credit/maturity-walls", operation_id="getCreditMaturityWalls", tags=["credit"])
def get_walls(region: str = Query("ALL"), sector_id: str = Query("ALL"), db: Db = Depends(get_db)) -> dict[str, Any]:
    wall = _walls(db, region, sector_id)
    sectors = []
    if _has(db, "debt_walls"):
        for r in db.all("""SELECT * FROM debt_walls WHERE region = ? AND bucket = 'total' AND sector_id <> 'ALL'
                           ORDER BY total_debt_usd DESC NULLS LAST""", [region]):
            sectors.append({"sector_id": r["sector_id"], "total_debt_usd": _r(r["total_debt_usd"], 0),
                            "maturing_usd": _r(r["amount_usd"], 0), "n_issuers": int(r["n_issuers"] or 0),
                            "n_issuers_debt": r.get("n_issuers_debt"), "wavg_cost_of_debt": _r(r["wavg_cost_of_debt"], 5),
                            "median_interest_coverage": _r(r["median_interest_coverage"], 3),
                            "median_net_debt_to_ebitda": _r(r["median_net_debt_to_ebitda"], 3),
                            "refi_share_24m": _r(r["refi_share_24m"], 4), "floating_share": _r(r["floating_share"], 4),
                            "refi_cost_24m_usd": _r(r["refi_cost_24m_usd"], 0)})
    regions = [r["region"] for r in db.all("SELECT DISTINCT region FROM debt_walls ORDER BY 1")] if _has(db, "debt_walls") else []
    return {"region": region, "sector_id": sector_id, "wall": wall, "sectors": sectors, "regions": regions,
            "labels": LABELS, "data_status": "ok" if wall else "pending"}


# =============================================================================================== company debt


_DEBT_COLS = ("total_debt", "debt_current", "debt_noncurrent", "cash", "net_debt", "ebitda", "ebit", "interest_expense",
              "mat_y1", "mat_y2", "mat_y3", "mat_y4", "mat_y5", "mat_after5", "mat_y2_5", "fixed_debt", "floating_debt",
              "refi_cost_24m")
_RATIO_COLS = ("avg_cost_of_debt", "interest_coverage", "ebitda_interest_coverage", "net_debt_to_ebitda",
               "debt_to_ebitda", "floating_share", "weighted_avg_rate", "refi_share_24m", "refi_market_rate", "wam_years",
               "refi_rate_gap_bp")


def _debt_row(r: dict[str, Any]) -> dict[str, Any]:
    out = {"period_end": iso(r["period_end"]), "period_kind": r["period_kind"], "knowledge_date": iso(r["knowledge_date"]),
           "currency": r["currency"], "fx_to_usd": _r(r["fx_to_usd"], 6), "mat_source": r["mat_source"],
           "mat_period_end": iso(r.get("mat_period_end")), "n_bonds_listed": r.get("n_bonds_listed")}
    out.update({c: _r(r.get(c), 0) for c in _DEBT_COLS})
    out.update({c: _r(r.get(c), 5) for c in _RATIO_COLS})
    return out


@router.get("/companies/{security_id}/debt", operation_id="getCompanyDebt", tags=["company"])
def get_company_debt(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    from app.api.routers.companies import resolve_sec  # noqa: PLC0415

    # coverage v3 A7: a ticker / ISIN URL (/company/AAPL, /company/7203.T) resolves like the other Company 360 tabs
    s = resolve_sec(db, security_id)
    iid = s.get("issuer_id")
    hist = [_debt_row(r) for r in db.all("SELECT * FROM company_debt WHERE issuer_id = ? ORDER BY period_end", [iid])] \
        if _has(db, "company_debt") and iid else []
    latest = hist[-1] if hist else None
    bonds = []
    if _has(db, "bonds_iss") and iid:
        for b in db.all("""SELECT * FROM bonds_iss WHERE issuer_id = ? AND (maturity_date IS NULL OR maturity_date >= ?)
                           ORDER BY maturity_date NULLS LAST LIMIT 300""", [iid, date.today()]):
            bonds.append({"bond_id": b["bond_id"], "source": b["source"], "isin": b["isin"], "figi": b["figi"],
                          "name": b["full_name"] or b["short_name"], "currency": b["currency"], "coupon_type": b["coupon_type"],
                          "coupon": _pct(b["coupon_bp_x100"]), "floating_reference": b["floating_reference"],
                          "floating_spread_bp": _r(b["floating_spread_bp"], 1), "maturity_date": iso(b["maturity_date"]),
                          "issue_amount": _r(b["issue_amount"], 0), "issue_amount_currency": b["issue_amount_currency"],
                          "seniority": b["seniority"], "venue": b["venue_mic"], "security_type": b["security_type"]})
    sector = s.get("sector_id")
    region = s.get("region")
    peer = None
    if sector and _has(db, "debt_walls"):
        reg_key = region if region in {"US", "EU", "UK", "JP", "CA", "KR", "CN", "HK"} else "ALL"
        w = db.one("SELECT * FROM debt_walls WHERE region = ? AND sector_id = ? AND bucket = 'total'", [reg_key, sector]) \
            or db.one("SELECT * FROM debt_walls WHERE region = 'ALL' AND sector_id = ? AND bucket = 'total'", [sector])
        if w:
            peer = {"region": w["region"], "sector_id": w["sector_id"], "n_issuers_debt": w.get("n_issuers_debt"),
                    "wavg_cost_of_debt": _r(w["wavg_cost_of_debt"], 5),
                    "median_interest_coverage": _r(w["median_interest_coverage"], 3),
                    "median_net_debt_to_ebitda": _r(w["median_net_debt_to_ebitda"], 3),
                    "refi_share_24m": _r(w["refi_share_24m"], 4), "floating_share": _r(w["floating_share"], 4)}
    wall = None
    if latest and latest["mat_source"] not in (None, "none"):
        wall = [{"bucket": b, "amount": latest[f"mat_{b}"]} for b in ("y1", "y2", "y3", "y4", "y5", "after5")]
        if latest.get("mat_y2_5") is not None:
            wall.append({"bucket": "y2_5", "amount": latest["mat_y2_5"]})
    reasons = []
    if not hist:
        reasons.append("no debt items in the issuer's filings (fundamentals_canonical)")
    elif not wall:
        reasons.append("no contractual maturity schedule tagged in the filings (US GAAP LongTermDebtMaturities* / IFRS MaturityAxis)")
    if not bonds:
        reasons.append("no listed bonds found (FIRDS by LEI / OpenFIGI TRACE)")
    return {"security_id": s["security_id"], "issuer_id": iid, "latest": latest, "history": hist, "maturity_wall": wall,
            "bonds": bonds, "sector_peer": peer, "reasons": reasons, "labels": LABELS,
            "data_status": "ok" if hist and wall else ("partial" if hist or bonds else "pending")}


_ = re
