"""Markets -> Instruments: FX pairs, commodity / financial futures, indices (equity, volatility, rates) and ETFs
(security_type fx | future | index | etf; synthetic ids IDX:<yf> / ETF:<yf>), priced from Yahoo market series.

FX POLICY: these are market series (charts, Studio). Official FX for conversions stays ECB / Fed / central banks.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends, Query

from app.api import pricebars
from app.api.deps import Db, get_db
from app.api.routers._common import fnum, iso, not_found, rnd

router = APIRouter(tags=["instruments"])

TYPES = ("fx", "future", "index", "etf")
SOURCE = "Yahoo Finance market series (daily; weekly bars before the 15-year daily window)"
FX_NOTE = "Market series (Yahoo); official FX for conversions remains ECB / Fed / central banks."

_MAJORS = {"EURUSD", "GBPUSD", "USDJPY", "USDCHF", "AUDUSD", "NZDUSD", "USDCAD"}
_G10 = {"USD", "EUR", "JPY", "GBP", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK", "DKK"}
_FUT = {
    "energy": {"CL", "BZ", "NG", "RB", "HO", "TTF", "QM", "MCL"},
    "metals": {"GC", "SI", "HG", "PL", "PA", "ALI", "HRC", "MGC", "SIL"},
    "ags": {"ZC", "ZW", "ZS", "ZL", "ZM", "ZO", "ZR", "KE"},
    "softs": {"KC", "SB", "CC", "CT", "OJ", "LBR"},
    "livestock": {"LE", "HE", "GF", "DC"},
    "rates_fut": {"ZB", "ZN", "ZF", "ZT"},
    "equity_fut": {"ES", "NQ", "YM", "RTY"},
    "fx_fut": {"6E", "6B", "6J"},
    "crypto_fut": {"BTC", "ETH"},
}
_VOL_IDX = {"^VIX", "^VIX9D", "^VIX3M", "^VIX6M", "^VIX1Y", "^VVIX", "^VXN", "^VXD", "^OVX", "^GVZ", "^SKEW", "^MOVE",
            "^INDIAVIX", "^AXVI", "^RVX", "^VXEEM"}
_RATE_IDX = {"^TNX", "^FVX", "^IRX", "^TYX"}
_ETF = {
    "etf_commodity": {"GLD", "IAU", "SLV", "SGOL", "SIVR", "PALL", "PPLT", "USO", "BNO", "UNG", "UGA", "CORN", "WEAT", "DBA",
                      "DBB", "DBC", "DBE", "DBO", "DBP", "DJP", "GSG", "COMT", "BCI", "USCI", "PDBC", "CPER"},
    "etf_bond": {"AGG", "BND", "BNDX", "BWX", "BIL", "EMB", "FLOT", "GOVT", "HYG", "IEF", "IEI", "JNK", "LQD", "MBB", "MUB",
                 "SHV", "SHY", "TIP", "TLT", "VCIT", "VCSH"},
    "etf_currency": {"FXB", "FXE", "FXY", "UUP"},
    "etf_crypto": {"ETHA", "IBIT"},
    "etf_vol": {"VIXY"},
    "etf_sector": {"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY", "KBE", "KRE", "XBI", "IBB",
                   "ITA", "ITB", "IGV", "JETS", "LIT", "OIH", "SMH", "SOXX", "TAN", "URA", "XHB", "XME", "XOP", "XRT", "ICLN",
                   "GDX", "GDXJ", "IYR", "VNQ", "ARKK", "KWEB"},
    "etf_intl": {"AAXJ", "ACWI", "EEM", "EFA", "EZU", "FEZ", "FXI", "IEMG", "ILF", "INDA", "VEA", "VGK", "VT", "VWO", "VXUS",
                 "VNM", "ASHR", "EPI", "MCHI"},
}

GROUPS: list[tuple[str, str, str]] = [  # (group_id, label, type)
    ("fx_majors", "FX majors", "fx"), ("fx_minors", "FX minors (G10 crosses)", "fx"), ("fx_eur", "EUR crosses", "fx"),
    ("fx_em", "FX emerging markets", "fx"),
    ("energy", "Energy", "future"), ("metals", "Metals", "future"), ("ags", "Grains & oilseeds", "future"),
    ("softs", "Softs", "future"), ("livestock", "Livestock & dairy", "future"), ("rates_fut", "Treasury futures", "future"),
    ("equity_fut", "Equity index futures", "future"), ("fx_fut", "Currency futures", "future"), ("crypto_fut", "Crypto futures", "future"),
    ("volatility", "Volatility", "index"), ("equity_idx", "Equity indices", "index"), ("rates_idx", "US Treasury yields", "index"),
    ("other_idx", "Dollar & commodity indices", "index"),
    ("etf_us", "ETFs: US equity & factors", "etf"), ("etf_sector", "ETFs: sectors & themes", "etf"),
    ("etf_intl", "ETFs: international & countries", "etf"), ("etf_bond", "ETFs: bonds", "etf"),
    ("etf_commodity", "ETFs: commodities", "etf"), ("etf_currency", "ETFs: currencies", "etf"), ("etf_crypto", "ETFs: crypto", "etf"),
    ("etf_vol", "ETFs: volatility", "etf"),
]
_GROUP_LABEL = {g: lbl for g, lbl, _ in GROUPS}
_ANCHORS = ["IDX:^GSPC", "IDX:DX-Y.NYB", "IDX:^TNX", "IDX:GC=F", "IDX:CL=F", "IDX:^VIX", "IDX:EURUSD=X"]


def group_of(ticker: str, typ: str, name: str | None = None) -> str:
    """Browser group for an instrument (pure; unit-tested)."""
    t = ticker.upper()
    if typ == "fx" or t.endswith("=X"):
        p = t.replace("=X", "")
        if p in _MAJORS:
            return "fx_majors"
        base, quote = p[:3], p[3:6]
        if base == "EUR":
            return "fx_eur"
        if base in _G10 and quote in _G10:
            return "fx_minors"
        return "fx_em"
    if typ == "future" or t.endswith("=F"):
        root = t.replace("=F", "")
        for g, roots in _FUT.items():
            if root in roots:
                return g
        return "energy" if "OIL" in (name or "").upper() else "metals"
    if typ == "etf":
        for g, ts in _ETF.items():
            if t in ts:
                return g
        n = (name or "").upper()
        if "MSCI" in n and "MSCI USA" not in n:
            return "etf_intl"
        return "etf_us"
    if t in _VOL_IDX or "VOLATILITY" in (name or "").upper() or t.startswith("^VIX"):
        return "volatility"
    if t in _RATE_IDX:
        return "rates_idx"
    if t in ("DX-Y.NYB", "^SPGSCI", "^DJCI", "^BCOM"):
        return "other_idx"
    return "equity_idx"


def _type_of(sid: str, st: str | None) -> str:
    if st in TYPES:
        return st  # type: ignore[return-value]
    if sid.startswith("ETF:"):
        return "etf"
    t = sid.split(":", 1)[-1]
    return "fx" if t.endswith("=X") else "future" if t.endswith("=F") else "index"


def _universe(db: Db) -> list[dict[str, Any]]:
    rows = db.all("""
        SELECT p.security_id, s.display_ticker, s.name, s.security_type, s.trading_currency,
               p.px_date, p.close, p.px_currency, p.chg_1d, p.chg_1w, p.chg_1m, p.chg_ytd, p.ret_12m, p.vol_1y
        FROM px_stats p LEFT JOIN securities s USING (security_id)
        WHERE s.security_type IN ('fx', 'future', 'index', 'etf')
           OR p.security_id LIKE 'IDX:%' OR p.security_id LIKE 'ETF:%'""")
    out = []
    for r in rows:
        sid = r["security_id"]
        typ = _type_of(sid, r.get("security_type"))
        tk = r.get("display_ticker") or sid.split(":", 1)[-1]
        out.append({**r, "ticker": tk, "type": typ, "group": group_of(tk, typ, r.get("name"))})
    return out


_CACHE: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}


def _item(r: dict[str, Any], spark: list[float] | None = None, first: date | None = None) -> dict[str, Any]:
    return {"security_id": r["security_id"], "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
            "type": r["type"], "group": r["group"], "group_label": _GROUP_LABEL.get(r["group"], r["group"]),
            "currency": r.get("px_currency") or r.get("trading_currency"), "last": rnd(r.get("close"), 6),
            "last_date": iso(r.get("px_date")), "chg_1d": rnd(r.get("chg_1d"), 6), "chg_1w": rnd(r.get("chg_1w"), 6),
            "chg_1m": rnd(r.get("chg_1m"), 6), "chg_ytd": rnd(r.get("chg_ytd"), 6), "chg_1y": rnd(r.get("ret_12m"), 6),
            "vol_1y": rnd(r.get("vol_1y"), 6), "first_date": iso(first), "spark": spark or []}


@router.get("/instruments", operation_id="listInstruments")
def list_instruments(type: str | None = Query(None, pattern="^(fx|future|index|etf)$"), group: str | None = None,
                     db: Db = Depends(get_db)) -> dict[str, Any]:
    key = (db.snap.run_id, type, group)
    if key in _CACHE:
        return _CACHE[key]
    uni = [r for r in _universe(db) if (type is None or r["type"] == type) and (group is None or r["group"] == group)]
    ids = [r["security_id"] for r in uni]
    spark: dict[str, list[float]] = {}
    first: dict[str, date] = {}
    if ids:
        ph = ", ".join("?" * len(ids))
        for s in db.all(f"""SELECT security_id, list(c ORDER BY wk) AS c FROM (
                               SELECT security_id, date_trunc('week', date) AS wk, arg_max(adj_close, date) AS c
                               FROM prices WHERE security_id IN ({ph}) AND date > ? GROUP BY 1, 2) GROUP BY 1""",
                        [*ids, db.as_of - timedelta(days=365)]):
            spark[s["security_id"]] = [round(float(x), 6) for x in (s["c"] or []) if x is not None and math.isfinite(x)]
        fd_sql = f"SELECT security_id, min(date) AS d FROM prices WHERE security_id IN ({ph}) GROUP BY 1"
        if pricebars.has_weekly(db):
            fd_sql = (f"SELECT security_id, min(d) AS d FROM ({fd_sql} UNION ALL SELECT security_id, min(date) FROM prices_w "
                      f"WHERE security_id IN ({ph}) GROUP BY 1) GROUP BY 1")
        for f in db.all(fd_sql, ids * (2 if pricebars.has_weekly(db) else 1)):
            first[f["security_id"]] = f["d"]
    order = {g: i for i, (g, _, _) in enumerate(GROUPS)}
    uni.sort(key=lambda r: (order.get(r["group"], 99), r["ticker"]))
    items = [_item(r, spark.get(r["security_id"]), first.get(r["security_id"])) for r in uni]
    present = {r["group"] for r in uni}
    out = {"as_of": iso(db.as_of), "groups": [{"group": g, "label": lbl, "type": t, "n": sum(1 for r in uni if r["group"] == g)}
                                              for g, lbl, t in GROUPS if g in present],
           "items": items, "source": SOURCE, "fx_note": FX_NOTE,
           "data_status": "ok" if items else ("pending" if not db.snap.input_ok("prices_daily") else "ok")}
    if len(_CACHE) > 64:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def window_stats(dates: list[date], closes: list[float], periods_per_year: float = 52.0) -> dict[str, Any]:
    """Annualized return (CAGR), annualized vol of log returns, max drawdown on a close series (pure; unit-tested)."""
    c = np.asarray(closes, dtype=float)
    ok = np.isfinite(c) & (c > 0)
    c = c[ok]
    d = [x for x, k in zip(dates, ok, strict=True) if k]
    if len(c) < 3:
        return {"start": iso(d[0]) if d else None, "ann_return": None, "ann_vol": None, "max_drawdown": None, "n": int(len(c))}
    years = max((d[-1] - d[0]).days / 365.25, 1e-9)
    lr = np.diff(np.log(c))
    peak = np.maximum.accumulate(c)
    return {"start": iso(d[0]), "ann_return": float((c[-1] / c[0]) ** (1 / years) - 1),
            "ann_vol": float(np.std(lr, ddof=1) * math.sqrt(periods_per_year)),
            "max_drawdown": float((c / peak - 1).min()), "n": int(len(c))}


def _weekly_closes(db: Db, ids: list[str], start: date) -> dict[str, dict[date, float]]:
    if not ids:
        return {}
    ph = ", ".join("?" * len(ids))
    out: dict[str, dict[date, float]] = {i: {} for i in ids}
    for r in db.all(f"""SELECT security_id, date_trunc('week', date)::DATE AS wk, arg_max(adj_close, date) AS c FROM prices
                        WHERE security_id IN ({ph}) AND date >= ? GROUP BY 1, 2""", [*ids, start]):
        if r["c"] is not None:
            out[r["security_id"]][r["wk"]] = float(r["c"])
    return out


def _corr(a: dict[date, float], b: dict[date, float]) -> tuple[float | None, int]:
    ks = sorted(set(a) & set(b))
    if len(ks) < 20:
        return None, len(ks)
    x = np.diff(np.log([a[k] for k in ks]))
    y = np.diff(np.log([b[k] for k in ks]))
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 20 or np.std(x[m]) == 0 or np.std(y[m]) == 0:
        return None, int(m.sum())
    return float(np.corrcoef(x[m], y[m])[0, 1]), int(m.sum())


@router.get("/instruments/{security_id}", operation_id="getInstrument")
def get_instrument(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    uni = {r["security_id"]: r for r in _universe(db)}
    r = uni.get(security_id)
    if r is None:
        raise not_found(f"unknown instrument {security_id!r}")
    first = pricebars.first_date(db, security_id)
    wk = pricebars.bars(db, security_id, first or date(1900, 1, 1), db.as_of, "W", True)
    dts = [b["date"] for b in wk if b["c"] is not None]
    cls = [float(b["c"]) for b in wk if b["c"] is not None]
    stats = []
    for label, yrs in (("1Y", 1), ("5Y", 5), ("10Y", 10), ("Max", None)):
        cut = db.as_of - timedelta(days=round(365.25 * yrs)) if yrs else date(1900, 1, 1)
        i0 = next((i for i, d in enumerate(dts) if d >= cut), len(dts))
        st = window_stats(dts[i0:], cls[i0:]) if i0 < len(dts) else window_stats([], [])
        if yrs and dts and dts[0] > cut + timedelta(days=30):
            st = {**st, "ann_return": None, "ann_vol": None, "max_drawdown": None}  # not enough history for the window
        stats.append({"window": label, **{k: (rnd(v, 6) if isinstance(v, float) else v) for k, v in st.items()}})
    # rolling 1y (52-week) vol of weekly log returns, downsampled to <= ~1,500 points
    roll: list[dict[str, Any]] = []
    if len(cls) > 53:
        lr = np.diff(np.log(np.asarray(cls)))
        step = max(1, len(lr) // 1500)
        for i in range(52, len(lr) + 1, step):
            w = lr[i - 52:i]
            if np.isfinite(w).all():
                roll.append({"date": iso(dts[i]), "vol": round(float(np.std(w, ddof=1) * math.sqrt(52)), 6)})
    peers = [x["security_id"] for x in uni.values() if x["group"] == r["group"] and x["security_id"] != security_id][:5]
    related = [x for x in dict.fromkeys(peers + [a for a in _ANCHORS if a in uni]) if x != security_id][:10]
    wc = _weekly_closes(db, [security_id, *related], db.as_of - timedelta(days=365 * 10 + 7))
    cut3 = db.as_of - timedelta(days=365 * 3)
    corr = []
    for o in related:
        c10, n10 = _corr(wc.get(security_id, {}), wc.get(o, {}))
        c3, n3 = _corr({k: v for k, v in wc.get(security_id, {}).items() if k >= cut3}, {k: v for k, v in wc.get(o, {}).items() if k >= cut3})
        x = uni[o]
        corr.append({"security_id": o, "ticker": x["ticker"], "name": x.get("name") or x["ticker"], "group": x["group"],
                     "corr_3y": rnd(c3, 4), "n_3y": n3, "corr_10y": rnd(c10, 4), "n_10y": n10})
    return {**_item(r, None, first), "daily_from": iso(pricebars.daily_from(db, security_id)), "source": SOURCE,
            "note": FX_NOTE if r["type"] == "fx" else None, "stats": stats, "rolling_vol": roll, "correlations": corr,
            "correlation_method": "Pearson correlation of weekly log returns (last 3 and 10 years)",
            "data_status": "ok" if wk else "pending"}


@router.get("/instruments/{security_id}/prices", operation_id="getInstrumentPrices")
def get_instrument_prices(security_id: str, from_: date | None = Query(None, alias="from"), to: date | None = None,
                          interval: str = Query("D", pattern="^(D|W|M)$"), db: Db = Depends(get_db)) -> dict[str, Any]:
    if not db.scalar("SELECT 1 FROM px_stats WHERE security_id = ?", [security_id]):
        raise not_found(f"unknown instrument {security_id!r}")
    end = to or db.as_of
    start = from_ or (end - timedelta(days=365 * 5))
    iv = interval
    if iv == "D" and (end - start).days > pricebars.MAX_DAILY_DAYS:
        iv = "W"
    rows = pricebars.bars(db, security_id, start, end, iv, True)  # type: ignore[arg-type]
    cur = db.scalar("SELECT px_currency FROM px_stats WHERE security_id = ?", [security_id])
    return {"security_id": security_id, "currency": cur or "", "adjusted": True, "interval": iv,
            "bars": [{"t": iso(b["date"]), "o": rnd(b["o"]), "h": rnd(b["h"]), "l": rnd(b["l"]), "c": rnd(b["c"]) or 0.0,
                      "v": fnum(b["v"])} for b in rows if b["c"] is not None],
            "first_date": iso(pricebars.first_date(db, security_id)), "daily_from": iso(pricebars.daily_from(db, security_id)),
            "events": [], "data_status": "ok" if rows else "pending"}
