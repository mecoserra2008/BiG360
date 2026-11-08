"""Macro v2 thematic dashboards (W1.4) + shared serving-side helpers (transforms, series payloads) for the
/api/v1/macro/v2 router (app.api.routers.macro_v2).

Every series below comes from an official source (FRED = St. Louis Fed copies of BLS/BEA/Fed Board/ICE BofA-licensed
OAS; Chicago/Kansas City/St. Louis/Atlanta/Cleveland Fed; NY Fed ACM; ECB Data Portal; ONS; World Bank Pink Sheet;
Eurostat/OECD/BIS/IMF via the cells). Panel kinds:
  * series  - explicit series ids with a display transform (level | yoy | diff | mom), native observations
              (serving macro_obs); daily series are thinned to weekly (last obs per ISO week) beyond 3 years;
  * compare - one cell indicator across countries (canonical primary series, serving macro_cell_history);
  * curve   - sovereign curve history (month-end snapshots per tenor) for animation (curves from
              app.api.routers.macro.CURVES);
  * derived - computed from other series (US net liquidity = WALCL - TGA - ON RRP, USD millions).
"""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from app.macro.indicators import EXCLUDED_SOURCES

THEMES = ("inflation", "labor", "rates", "credit_conditions", "liquidity", "commodities_fx", "growth")
MAJOR = ["US", "EA", "GB", "JP", "CN", "CA", "AU", "CH", "KR", "IN", "BR", "MX"]
EA_CORE = ["EA", "DE", "FR", "IT", "ES", "NL", "PT"]


def _s(sid: str, label: str, tf: str = "level") -> dict[str, str]:
    return {"series_id": sid, "label": label, "transform": tf}


PINK = "wb_pinksheet:"
THEME_PANELS: dict[str, dict[str, Any]] = {
    "inflation": {"title": "Inflation", "panels": [
        {"id": "cpi_compare", "kind": "compare", "title": "Headline CPI y/y", "indicator": "cpi_yoy", "countries": MAJOR},
        {"id": "core_compare", "kind": "compare", "title": "Core CPI y/y", "indicator": "core_cpi_yoy", "countries": MAJOR},
        {"id": "us_cpi_components", "kind": "series", "title": "US CPI components (BLS via FRED), % y/y", "series": [
            _s("fred:CPIAUCSL", "All items", "yoy"), _s("fred:CPILFESL", "Core (ex food & energy)", "yoy"),
            _s("fred:CPIUFDSL", "Food", "yoy"), _s("fred:CPIENGSL", "Energy", "yoy"),
            _s("fred:CUSR0000SACL1E", "Core goods", "yoy"), _s("fred:CUSR0000SASLE", "Core services", "yoy"),
            _s("fred:CUSR0000SAH1", "Shelter", "yoy"), _s("fred:CUSR0000SASL2RS", "Services ex rent of shelter", "yoy"),
            _s("fred:CPIMEDSL", "Medical care", "yoy")]},
        {"id": "us_core_measures", "kind": "series", "title": "US PCE and alternative core measures, % y/y", "series": [
            _s("fred:PCEPI", "PCE price index", "yoy"), _s("fred:PCEPILFE", "Core PCE", "yoy"),
            _s("fred:CORESTICKM159SFRBATL", "Sticky-price core CPI (Atlanta Fed)"),
            _s("fred:MEDCPIM158SFRBCLE", "Median CPI, % m/m saar (Cleveland Fed)")]},
        {"id": "ea_hicp_components", "kind": "series", "title": "Euro area HICP components (ECB), % y/y", "series": [
            _s("ecb:HICP.M.U2.N.000000.4D0.ANR", "All items"), _s("ecb:HICP.M.U2.N.XEF000.4D0.ANR", "Core"),
            _s("ecb:HICP.M.U2.N.NRGY00.4D0.ANR", "Energy"), _s("ecb:HICP.M.U2.N.FOOD00.4D0.ANR", "Food, alcohol & tobacco"),
            _s("ecb:HICP.M.U2.N.IGXE00.4D0.ANR", "Non-energy industrial goods"), _s("ecb:HICP.M.U2.N.SERV00.4D0.ANR", "Services")]},
        {"id": "uk_cpi_components", "kind": "series", "title": "UK CPI components (ONS), % y/y", "series": [
            _s("ons:D7G7", "All items"), _s("ons:DKO8", "Core"), _s("ons:D7NM", "Goods"), _s("ons:D7NN", "Services"),
            _s("ons:D7G8", "Food & non-alcoholic beverages"), _s("ons:D7GB", "Housing, water & fuels"),
            _s("ons:D7GE", "Transport")]},
        {"id": "breakevens_real", "kind": "series", "title": "US breakevens, real yields, expectations (%)", "series": [
            _s("fred:T5YIE", "5Y breakeven"), _s("fred:T10YIE", "10Y breakeven"), _s("fred:T5YIFR", "5y5y forward"),
            _s("fred:DFII5", "5Y TIPS real yield"), _s("fred:DFII10", "10Y TIPS real yield"),
            _s("fred:MICH", "Michigan 1Y inflation expectations")]},
        {"id": "ppi_compare", "kind": "compare", "title": "Producer prices y/y", "indicator": "ppi_yoy", "countries": MAJOR},
    ]},
    "labor": {"title": "Labor", "panels": [
        {"id": "us_payrolls", "kind": "series", "title": "US nonfarm payrolls (BLS via FRED)", "series": [
            _s("fred:PAYEMS", "Monthly change (thousands)", "diff"), _s("fred:PAYEMS", "% y/y", "yoy")]},
        {"id": "us_claims", "kind": "series", "title": "US jobless claims (DOL via FRED)", "series": [
            _s("fred:ICSA", "Initial claims"), _s("fred:CCSA", "Continuing claims")]},
        {"id": "us_jolts", "kind": "series", "title": "JOLTS (BLS via FRED)", "series": [
            _s("fred:JTSJOL", "Job openings (thousands)"), _s("fred:JTSJOR", "Openings rate %"),
            _s("fred:JTSQUR", "Quits rate %"), _s("fred:JTSHIR", "Hires rate %")]},
        {"id": "us_labor_extra", "kind": "series", "title": "US unemployment, participation, wages", "series": [
            _s("fred:UNRATE", "Unemployment rate %"), _s("fred:U6RATE", "U-6 %"), _s("fred:CIVPART", "Participation %"),
            _s("fred:SAHMREALTIME", "Sahm rule (real-time)"), _s("fred:CES0500000003", "Avg hourly earnings % y/y", "yoy")]},
        {"id": "unemployment_compare", "kind": "compare", "title": "Unemployment rate", "indicator": "unemployment",
         "countries": MAJOR},
        {"id": "employment_compare", "kind": "compare", "title": "Employment", "indicator": "employment", "countries": MAJOR},
    ]},
    "rates": {"title": "Rates and curves", "panels": [
        {"id": "policy_compare", "kind": "compare", "title": "Policy rates", "indicator": "policy_rate", "countries": MAJOR},
        {"id": "y10_compare", "kind": "compare", "title": "10Y government yields", "indicator": "yield_10y", "countries": MAJOR},
        {"id": "slope_compare", "kind": "compare", "title": "2s10s slope (pp)", "indicator": "curve_2s10s", "countries": MAJOR},
        {"id": "us_spreads", "kind": "series", "title": "US curve spreads (pp)", "series": [
            _s("fred:T10Y2Y", "10Y - 2Y"), _s("fred:T10Y3M", "10Y - 3M"), _s("fred:T10YFF", "10Y - fed funds")]},
        {"id": "us_real", "kind": "series", "title": "US real yields (TIPS, %)", "series": [
            _s("fred:DFII5", "5Y real"), _s("fred:DFII10", "10Y real")]},
        {"id": "term_premium", "kind": "series", "title": "Treasury term premia (%)", "series": [
            _s("nyfed:ACMTP10", "ACM 10Y (NY Fed)"), _s("nyfed:ACMTP05", "ACM 5Y (NY Fed)"),
            _s("nyfed:ACMTP02", "ACM 2Y (NY Fed)"), _s("nyfed:ACMRNY10", "ACM risk-neutral 10Y yield (NY Fed)"),
            _s("fred:THREEFYTP10", "Kim-Wright 10Y (Fed Board)")]},
        {"id": "curves", "kind": "curve", "title": "Sovereign curve history (month-end)",
         "curves": ["UST", "EA_AAA", "DE", "GB", "JP", "CA", "AU", "CN", "KR"]},
    ]},
    "credit_conditions": {"title": "Credit and financial conditions", "panels": [
        {"id": "oas", "kind": "series", "title": "Corporate OAS (ICE BofA via FRED, %)", "series": [
            _s("fred:BAMLH0A0HYM2", "US high yield"), _s("fred:BAMLH0A3HYC", "US CCC & lower"),
            _s("fred:BAMLC0A0CM", "US investment grade"), _s("fred:BAMLC0A4CBBB", "US BBB"),
            _s("fred:BAMLHE00EHYIOAS", "Euro high yield"), _s("fred:BAMLEMCBPIOAS", "EM corporate"),
            _s("fred:BAA10Y", "Moody's Baa - 10Y")]},
        {"id": "fci", "kind": "series", "title": "Financial conditions and stress (Fed banks)", "series": [
            _s("fred:NFCI", "Chicago Fed NFCI"), _s("fred:ANFCI", "Chicago Fed adjusted NFCI"),
            _s("fred:STLFSI4", "St. Louis Fed stress index"), _s("fred:KCFSI", "Kansas City Fed stress index")]},
        {"id": "ciss", "kind": "series", "title": "ECB composite indicator of systemic stress (CISS)", "series": [
            _s("ecb:CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX", "Euro area"), _s("ecb:CISS.D.DE.Z0Z.4F.EC.SS_CIN.IDX", "Germany"),
            _s("ecb:CISS.D.FR.Z0Z.4F.EC.SS_CIN.IDX", "France"), _s("ecb:CISS.D.IT.Z0Z.4F.EC.SS_CIN.IDX", "Italy"),
            _s("ecb:CISS.D.ES.Z0Z.4F.EC.SS_CIN.IDX", "Spain"), _s("ecb:CISS.D.GB.Z0Z.4F.EC.SS_CIN.IDX", "United Kingdom"),
            _s("ecb:CISS.D.US.Z0Z.4F.EC.SS_CIN.IDX", "United States"), _s("ecb:CISS.D.CN.Z0Z.4F.EC.SS_CIN.IDX", "China")]},
        {"id": "sloos", "kind": "series", "title": "Fed Senior Loan Officer Survey (net %)", "series": [
            _s("fred:DRTSCILM", "Tightening C&I, large/middle firms"), _s("fred:DRTSCIS", "Tightening C&I, small firms"),
            _s("fred:SUBLPDRCSC", "Tightening CRE construction"), _s("fred:DRTSCLCC", "Tightening credit cards"),
            _s("fred:DRSDCILM", "Stronger C&I demand, large/middle firms")]},
        {"id": "ecb_bls", "kind": "series", "title": "ECB Bank Lending Survey (net %)", "series": [
            _s("ecb:BLS.Q.U2.ALL.O.E.Z.B3.ST.S.WFNET", "Standards, enterprises (tightening)"),
            _s("ecb:BLS.Q.U2.ALL.Z.H.H.B3.ST.S.WFNET", "Standards, housing loans (tightening)"),
            _s("ecb:BLS.Q.U2.ALL.O.E.Z.B3.ZZ.D.WFNET", "Demand, enterprises"),
            _s("ecb:BLS.Q.U2.ALL.Z.H.H.B3.ZZ.D.WFNET", "Demand, housing loans")]},
        {"id": "credit_compare", "kind": "compare", "title": "Private credit", "indicator": "private_credit", "countries": MAJOR},
    ]},
    "liquidity": {"title": "Liquidity", "panels": [
        {"id": "fed_bs", "kind": "series", "title": "Federal Reserve balance sheet (H.4.1 via FRED)", "series": [
            _s("fred:WALCL", "Total assets (USD m)"), _s("fred:WRESBAL", "Reserve balances (USD m)"),
            _s("fred:WTREGEN", "Treasury General Account (USD m)"), _s("fred:RRPONTSYD", "Overnight reverse repo (USD bn)")]},
        {"id": "net_liquidity", "kind": "derived", "title": "US net liquidity = Fed assets - TGA - ON RRP (USD m)",
         "formula": "fred:WALCL - fred:WTREGEN - 1000 * fred:RRPONTSYD"},
        {"id": "ecb_bs", "kind": "series", "title": "Eurosystem balance sheet (ECB ILM)", "series": [
            _s("ecb:ILM.W.U2.C.T000000.Z5.Z01", "Total assets (EUR m)")]},
        {"id": "money_compare", "kind": "compare", "title": "Broad money", "indicator": "money_supply", "countries": MAJOR},
        {"id": "us_m2", "kind": "series", "title": "US M2 (Fed H.6)", "series": [_s("fred:M2SL", "M2 % y/y", "yoy")]},
    ]},
    "commodities_fx": {"title": "Commodities and FX", "panels": [
        {"id": "energy", "kind": "series", "title": "Energy (World Bank Pink Sheet, monthly)", "series": [
            _s(PINK + "brent", "Brent USD/bbl"), _s(PINK + "wti", "WTI USD/bbl"), _s(PINK + "natgas_eu", "Gas Europe USD/mmbtu"),
            _s(PINK + "natgas_us", "Gas US USD/mmbtu"), _s(PINK + "lng_japan", "LNG Japan USD/mmbtu"),
            _s(PINK + "coal_au", "Coal Australia USD/mt")]},
        {"id": "metals", "kind": "series", "title": "Metals (World Bank Pink Sheet, monthly)", "series": [
            _s(PINK + "copper", "Copper USD/mt"), _s(PINK + "aluminum", "Aluminum USD/mt"),
            _s(PINK + "iron_ore", "Iron ore USD/dmtu"), _s(PINK + "nickel", "Nickel USD/mt"), _s(PINK + "zinc", "Zinc USD/mt"),
            _s(PINK + "gold", "Gold USD/oz"), _s(PINK + "silver", "Silver USD/oz")]},
        {"id": "agri", "kind": "series", "title": "Agriculture (World Bank Pink Sheet, monthly)", "series": [
            _s(PINK + "wheat", "Wheat USD/mt"), _s(PINK + "maize", "Maize USD/mt"), _s(PINK + "soybeans", "Soybeans USD/mt")]},
        {"id": "daily_energy", "kind": "series", "title": "Daily spot energy (EIA via FRED)", "series": [
            _s("fred:DCOILBRENTEU", "Brent USD/bbl"), _s("fred:DCOILWTICO", "WTI USD/bbl"),
            _s("fred:DHHNGSP", "Henry Hub USD/mmbtu")]},
        {"id": "usd", "kind": "series", "title": "Trade-weighted USD (Fed H.10)", "series": [
            _s("fred:DTWEXBGS", "Broad"), _s("fred:DTWEXAFEGS", "Advanced foreign economies"),
            _s("fred:DTWEXEMEGS", "Emerging markets")]},
        {"id": "fx_usd", "kind": "series", "title": "FX rates (Fed H.10)", "series": [
            _s("fred:DEXUSEU", "USD per EUR"), _s("fred:DEXUSUK", "USD per GBP"), _s("fred:DEXJPUS", "JPY per USD"),
            _s("fred:DEXCHUS", "CNY per USD"), _s("fred:DEXKOUS", "KRW per USD"), _s("fred:DEXINUS", "INR per USD"),
            _s("fred:DEXBZUS", "BRL per USD"), _s("fred:DEXMXUS", "MXN per USD")]},
        {"id": "reer_compare", "kind": "compare", "title": "Real effective exchange rates (BIS), % y/y",
         "indicator": "reer", "countries": MAJOR},
    ]},
    "growth": {"title": "Growth", "panels": [
        {"id": "gdp_compare", "kind": "compare", "title": "Real GDP y/y", "indicator": "gdp_yoy", "countries": MAJOR},
        {"id": "ip_compare", "kind": "compare", "title": "Industrial production y/y", "indicator": "industrial_production",
         "countries": MAJOR},
        {"id": "retail_compare", "kind": "compare", "title": "Retail sales y/y", "indicator": "retail_sales", "countries": MAJOR},
        {"id": "pmi_compare", "kind": "compare", "title": "Business surveys (PMI proxy)", "indicator": "pmi_proxy",
         "countries": MAJOR},
        {"id": "us_activity", "kind": "series", "title": "US activity", "series": [
            _s("fred:GDPNOW", "Atlanta Fed GDPNow (% saar)"), _s("fred:CFNAIMA3", "Chicago Fed National Activity (3m)"),
            _s("fred:WEI", "NY Fed Weekly Economic Index"), _s("fred:INDPRO", "Industrial production % y/y", "yoy"),
            _s("fred:RRSFS", "Real retail sales % y/y", "yoy")]},
        {"id": "ea_activity", "kind": "compare", "title": "Euro area members: GDP y/y", "indicator": "gdp_yoy",
         "countries": EA_CORE},
    ]},
}


# ------------------------------------------------------------------------------------------------ transforms


def _shift_months(d: date, k: int) -> date:
    m = d.month - 1 + k
    return date(d.year + m // 12, m % 12 + 1, min(d.day, 28))


def apply_transform(points: list[tuple[date, float | None]], transform: str, freq: str) -> list[tuple[date, float | None]]:
    """level | yoy (% vs 12 months earlier) | mom (% vs previous period) | diff (vs previous period) |
    z (vs the trailing 10y window, >= 24 points). Percent changes are in percent (not fractions)."""
    pts = [(d, v) for d, v in points if v is not None and math.isfinite(v)]
    if transform == "level" or not pts:
        return pts
    out: list[tuple[date, float | None]] = []
    if transform == "yoy":
        if freq in ("D", "W"):
            dates = [d for d, _ in pts]
            for d, v in pts:
                t = d - timedelta(days=365)
                i = bisect.bisect_right(dates, t) - 1
                b = pts[i][1] if i >= 0 and (t - dates[i]).days <= 10 else None
                out.append((d, None if not b else (v / b - 1) * 100))
        else:
            by = {d: v for d, v in pts}
            out = [(d, (v / by[p] - 1) * 100 if (p := _shift_months(d, -12)) in by and by[p] else None) for d, v in pts]
        return [(d, v) for d, v in out if v is not None]
    if transform in ("mom", "diff"):
        for (d0, v0), (d1, v1) in zip(pts, pts[1:], strict=False):
            if transform == "diff":
                out.append((d1, v1 - v0))
            elif v0:
                out.append((d1, (v1 / v0 - 1) * 100))
        return out
    if transform == "z":
        vals = [v for _, v in pts]
        win = {"D": 2520, "W": 520, "M": 120, "Q": 40, "A": 10}.get(freq, 120)
        for i, (d, v) in enumerate(pts):
            w = vals[max(0, i - win + 1): i + 1]
            if len(w) < 24 and freq not in ("Q", "A") or len(w) < 8:
                continue
            m = sum(w) / len(w)
            sd = math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) - 1))
            if sd > 0:
                out.append((d, (v - m) / sd))
        return out
    return pts


def thin_weekly(points: list[tuple[date, float | None]]) -> list[tuple[date, float | None]]:
    """Last observation per ISO week (daily series over long windows)."""
    out: dict[tuple[int, int], tuple[date, float | None]] = {}
    for d, v in points:
        out[d.isocalendar()[:2]] = (d, v)
    return sorted(out.values())


def rnd(v: float | None, n: int = 4) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), n)


def pts_json(points: list[tuple[date, float | None]]) -> list[dict[str, Any]]:
    return [{"date": d.isoformat(), "value": rnd(v, 6)} for d, v in points]


# ------------------------------------------------------------------------------------------------ serving queries


def native_points(db: Any, ids: list[str], start: date) -> dict[str, list[tuple[date, float | None]]]:
    if not ids:
        return {}
    ph = ", ".join("?" for _ in ids)
    rows = db.all(f"""SELECT series_id, date, value FROM macro_obs WHERE series_id IN ({ph}) AND date >= ? AND date <= ?
                      AND value IS NOT NULL ORDER BY series_id, date""", [*ids, start, db.as_of])
    out: dict[str, list[tuple[date, float | None]]] = defaultdict(list)
    for r in rows:
        out[r["series_id"]].append((r["date"], r["value"]))
    return out


def series_meta(db: Any, ids: list[str]) -> dict[str, dict[str, Any]]:
    if not ids:
        return {}
    ph = ", ".join("?" for _ in ids)
    return {r["series_id"]: r for r in db.all(f"SELECT * FROM macro_series WHERE series_id IN ({ph})", ids)}


def series_items(db: Any, items: list[dict[str, str]], years: int) -> list[dict[str, Any]]:
    start = db.as_of - timedelta(days=int(365.25 * years) + 400)
    ids = sorted({i["series_id"] for i in items})
    meta = series_meta(db, ids)
    data = native_points(db, ids, start)
    cut = db.as_of - timedelta(days=int(365.25 * years))
    out = []
    for it in items:
        m = meta.get(it["series_id"])
        freq = (m or {}).get("frequency") or "M"
        pts = apply_transform(data.get(it["series_id"], []), it.get("transform", "level"), freq)
        pts = [(d, v) for d, v in pts if d >= cut]
        if freq in ("D", "W") and years > 3:
            pts = thin_weekly(pts)
        last = next(((d, v) for d, v in reversed(pts) if v is not None), (None, None))
        out.append({"series_id": it["series_id"], "label": it.get("label") or (m or {}).get("title") or it["series_id"],
                    "title": (m or {}).get("title"), "source_id": (m or {}).get("source_id"),
                    "units": _units(m, it.get("transform", "level")), "transform": it.get("transform", "level"),
                    "frequency": freq, "last_date": last[0].isoformat() if last[0] else None, "last_value": rnd(last[1], 6),
                    "available": bool(pts), "points": pts_json(pts)})
    return out


def _units(m: dict[str, Any] | None, tf: str) -> str | None:
    if tf == "yoy":
        return "% y/y"
    if tf == "mom":
        return "% m/m"
    if tf == "z":
        return "z"
    return (m or {}).get("units")


def compare_items(db: Any, indicator: str, countries: list[str], years: int) -> list[dict[str, Any]]:
    """One indicator across countries: canonical primary history + latest cell (serving macro_cell_history/macro_cells)."""
    if not countries:
        return []
    ph = ", ".join("?" for _ in countries)
    cut = db.as_of - timedelta(days=int(365.25 * years))
    rows = db.all(f"""SELECT country, series_id, date, value, units, frequency FROM macro_cell_history
                      WHERE indicator = ? AND is_primary AND country IN ({ph}) AND date >= ? AND date <= ?
                      ORDER BY country, date""", [indicator, *countries, cut, db.as_of])
    cells = {r["country"]: r for r in latest_cells(db, countries, [indicator])}
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by[r["country"]].append(r)
    out = []
    for cc in countries:
        rr = by.get(cc, [])
        c = cells.get(cc)
        out.append({"country": cc, "series_id": (c or {}).get("series_id") or (rr[0]["series_id"] if rr else None),
                    "units": (c or {}).get("units") or (rr[0]["units"] if rr else None),
                    "frequency": (c or {}).get("frequency") or (rr[0]["frequency"] if rr else None),
                    "available": bool(rr), "latest": cell_json(c) if c else None,
                    "points": [{"date": r["date"].isoformat(), "value": rnd(r["value"], 6)} for r in rr]})
    return out


def latest_cells(db: Any, countries: list[str] | None = None, indicators: list[str] | None = None,
                 as_of: date | None = None) -> list[dict[str, Any]]:
    """macro_cells rows of the latest snapshot <= as_of (default: serving as_of)."""
    a = as_of or db.as_of
    snap = db.scalar("SELECT max(as_of_date) FROM macro_cells WHERE as_of_date <= ?", [a])
    if snap is None:
        return []
    conds, params = ["as_of_date = ?"], [snap]
    if countries:
        conds.append(f"country IN ({', '.join('?' for _ in countries)})")
        params += countries
    if indicators:
        conds.append(f"indicator IN ({', '.join('?' for _ in indicators)})")
        params += indicators
    return db.all(f"SELECT * FROM macro_cells WHERE {' AND '.join(conds)} ORDER BY country, indicator", params)


def source_link(source_id: str | None, series_id: str | None) -> str | None:
    """Official page (FRED series page) or the official API / file endpoint from the catalog `sources` map."""
    src = source_id or (series_id or "").split(":", 1)[0]
    if src == "fred" and series_id:
        return f"https://fred.stlouisfed.org/series/{series_id.split(':', 1)[1]}"
    from app.ingestion.macro_common import catalog_doc  # noqa: PLC0415 - cached by file mtime

    return ((catalog_doc().get("sources") or {}).get(src) or {}).get("url")


def cell_json(r: dict[str, Any]) -> dict[str, Any]:
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {"country": r["country"], "indicator": r["indicator"], "label": r.get("label"), "group": r.get("group"),
            "series_id": r["series_id"], "inputs": list(r.get("inputs") or []), "source_id": r.get("source_id"),
            "title": r.get("title"), "units": r.get("units"), "transform": r.get("transform"),
            "frequency": r.get("frequency"), "period": iso(r.get("period")), "period_label": r.get("period_label"),
            "value": rnd(r.get("value"), 6), "release_date": iso(r.get("release_date")),
            "release_date_estimated": r.get("release_date_estimated"), "change_3m": rnd(r.get("change_3m")),
            "change_6m": rnd(r.get("change_6m")), "change_12m": rnd(r.get("change_12m")), "direction": r.get("direction"),
            "z_10y": rnd(r.get("z_10y")), "percentile_10y": rnd(r.get("percentile_10y")), "n_hist": r.get("n_hist"),
            "first_release_value": rnd(r.get("first_release_value"), 6), "revision": rnd(r.get("revision"), 6),
            "has_vintages": bool(r.get("has_vintages")), "status": r.get("status"), "age_days": r.get("age_days"),
            "good_direction": r.get("good_direction"), "as_of_date": iso(r.get("as_of_date")),
            "source_url": source_link(r.get("source_id"), r.get("series_id")),
            "official": (r.get("source_id") or "") not in EXCLUDED_SOURCES}


def curve_history(db: Any, curve_ids: list[str], years: int) -> list[dict[str, Any]]:
    """Month-end curve snapshots: {curve_id, label, tenors[{tenor, months, series_id}], dates[], matrix[date][tenor]}."""
    from app.api.routers.macro import CURVES  # noqa: PLC0415 - single source of curve definitions

    curves = [c for c in CURVES if c[0] in curve_ids]
    ids = sorted({s for c in curves for _, _, cands in c[4] for s in cands})
    if not ids:
        return []
    ph = ", ".join("?" for _ in ids)
    cut = db.as_of - timedelta(days=int(365.25 * years) + 40)
    rows = db.all(f"""SELECT series_id, date_trunc('month', date)::DATE AS m, arg_max(value, date) AS v
                      FROM macro_obs WHERE series_id IN ({ph}) AND date >= ? AND date <= ? AND value IS NOT NULL
                      GROUP BY 1, 2 ORDER BY 2""", [*ids, cut, db.as_of])
    by: dict[str, dict[date, float]] = defaultdict(dict)
    for r in rows:
        by[r["series_id"]][r["m"]] = r["v"]
    out = []
    for cid, cc, _region, label, tenors in curves:
        use = []
        for tenor, months, cands in tenors:
            if months == 0:
                continue  # O/N policy point is not a market tenor
            sid = max(cands, key=lambda s: len(by.get(s, {})))
            if by.get(sid):
                use.append((tenor, months, sid))
        if len(use) < 2:
            continue
        dates = sorted({d for _, _, s in use for d in by[s]})
        matrix = [[rnd(by[s].get(d)) for _, _, s in use] for d in dates]
        out.append({"curve_id": cid, "country": cc, "label": label,
                    "tenors": [{"tenor": t, "months": m, "series_id": s} for t, m, s in use],
                    "dates": [d.isoformat() for d in dates], "matrix": matrix})
    return out


def derived_series(db: Any, formula: str, years: int) -> dict[str, Any]:
    """Only the documented liquidity identity: A - B - k * C on the dates where all inputs exist (weekly)."""
    a, b, c = "fred:WALCL", "fred:WTREGEN", "fred:RRPONTSYD"
    start = db.as_of - timedelta(days=int(365.25 * years))
    d = native_points(db, [a, b, c], start)
    bd, cd = dict(d.get(b, [])), d.get(c, [])
    cdates = [x for x, _ in cd]
    pts = []
    for dt, va in d.get(a, []):
        vb = bd.get(dt)
        i = bisect.bisect_right(cdates, dt) - 1  # ON RRP is daily: last value <= the H.4.1 Wednesday
        vc = cd[i][1] if i >= 0 and (dt - cdates[i]).days <= 7 else None
        if va is not None and vb is not None and vc is not None:
            pts.append((dt, va - vb - 1000.0 * vc))
    return {"series_id": "derived:us_net_liquidity", "label": "US net liquidity", "formula": formula,
            "units": "Millions of Dollars", "frequency": "W", "inputs": [a, b, c], "available": bool(pts),
            "last_date": pts[-1][0].isoformat() if pts else None, "last_value": rnd(pts[-1][1], 2) if pts else None,
            "points": pts_json(pts)}


def theme_payload(db: Any, theme: str, years: int, countries: list[str] | None = None) -> dict[str, Any]:
    spec = THEME_PANELS[theme]
    panels = []
    for p in spec["panels"]:
        base = {"id": p["id"], "kind": p["kind"], "title": p["title"]}
        if p["kind"] == "series":
            base["series"] = series_items(db, p["series"], years)
        elif p["kind"] == "compare":
            base["indicator"] = p["indicator"]
            base["series"] = compare_items(db, p["indicator"], countries or p["countries"], years)
        elif p["kind"] == "curve":
            base["curves"] = curve_history(db, p["curves"], years)
        elif p["kind"] == "derived":
            base["series"] = [derived_series(db, p["formula"], years)]
        panels.append(base)
    return {"theme": theme, "title": spec["title"], "as_of": db.as_of.isoformat(), "years": years, "panels": panels}
