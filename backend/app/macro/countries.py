"""Macro v2 country pages (W1.3) for the 24 coverage countries.

Sections: growth, inflation, labor, rates (and curve), credit (money & credit, financial stress), housing, external,
fiscal, fx, plus `equity_vs_macro`: the country's main equity index (serving `prices`, monthly last adj. close, % y/y)
next to the cycle clock's growth and inflation momentum composites. Each section lists every series of the country
(official sources only; AKShare excluded upstream) with sparkline-ready canonical history from macro_cell_history
(primary cell series first), plus the section's heatmap cells.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from app.macro.indicators import CELL_INDICATORS, COUNTRIES
from app.macro.themes import cell_json, latest_cells, rnd

COUNTRY_META: dict[str, dict[str, Any]] = {
    "US": {"name": "United States", "currency": "USD", "equity_index": "IDX:^GSPC", "index_name": "S&P 500"},
    "EA": {"name": "Euro area", "currency": "EUR", "equity_index": "IDX:^STOXX50E", "index_name": "Euro Stoxx 50"},
    "DE": {"name": "Germany", "currency": "EUR", "equity_index": "IDX:^GDAXI", "index_name": "DAX"},
    "FR": {"name": "France", "currency": "EUR", "equity_index": "IDX:^FCHI", "index_name": "CAC 40"},
    "IT": {"name": "Italy", "currency": "EUR", "equity_index": "IDX:FTSEMIB.MI", "index_name": "FTSE MIB"},
    "ES": {"name": "Spain", "currency": "EUR", "equity_index": "IDX:^IBEX", "index_name": "IBEX 35"},
    "PT": {"name": "Portugal", "currency": "EUR", "equity_index": "IDX:PSI20.LS", "index_name": "PSI"},
    "NL": {"name": "Netherlands", "currency": "EUR", "equity_index": "IDX:^AEX", "index_name": "AEX"},
    "GB": {"name": "United Kingdom", "currency": "GBP", "equity_index": "IDX:^FTSE", "index_name": "FTSE 100"},
    "CH": {"name": "Switzerland", "currency": "CHF", "equity_index": "IDX:^SSMI", "index_name": "SMI"},
    "JP": {"name": "Japan", "currency": "JPY", "equity_index": "IDX:^N225", "index_name": "Nikkei 225"},
    "KR": {"name": "Korea", "currency": "KRW", "equity_index": "IDX:^KS11", "index_name": "KOSPI"},
    "CN": {"name": "China", "currency": "CNY", "equity_index": "IDX:000300.SS", "index_name": "CSI 300"},
    "HK": {"name": "Hong Kong", "currency": "HKD", "equity_index": "IDX:^HSI", "index_name": "Hang Seng"},
    "TW": {"name": "Taiwan", "currency": "TWD", "equity_index": "IDX:^TWII", "index_name": "TAIEX"},
    "IN": {"name": "India", "currency": "INR", "equity_index": "IDX:^BSESN", "index_name": "BSE Sensex"},
    "AU": {"name": "Australia", "currency": "AUD", "equity_index": "IDX:^AXJO", "index_name": "S&P/ASX 200"},
    "CA": {"name": "Canada", "currency": "CAD", "equity_index": "IDX:^GSPTSE", "index_name": "S&P/TSX"},
    "BR": {"name": "Brazil", "currency": "BRL", "equity_index": "IDX:^BVSP", "index_name": "Ibovespa"},
    "MX": {"name": "Mexico", "currency": "MXN", "equity_index": "IDX:^MXX", "index_name": "IPC"},
    "CL": {"name": "Chile", "currency": "CLP", "equity_index": None, "index_name": None},
    "CO": {"name": "Colombia", "currency": "COP", "equity_index": None, "index_name": None},
    "PE": {"name": "Peru", "currency": "PEN", "equity_index": None, "index_name": None},
    "AR": {"name": "Argentina", "currency": "ARS", "equity_index": "IDX:^MERV", "index_name": "MERVAL"},
}
assert set(COUNTRY_META) == set(COUNTRIES)

SECTIONS: list[tuple[str, str]] = [("growth", "Growth"), ("inflation", "Inflation"), ("labor", "Labor"),
                                   ("rates", "Rates and curve"), ("credit", "Money, credit and stress"),
                                   ("housing", "Housing"), ("external", "External"), ("fiscal", "Fiscal"), ("fx", "FX")]
#: indicator -> section (catalog indicators incl. the non-cell ones)
SECTION_OF_INDICATOR = {
    "gdp_qoq": "growth", "gdp_yoy": "growth", "industrial_production": "growth", "retail_sales": "growth",
    "pmi_proxy": "growth", "consumer_confidence": "growth", "cpi_yoy": "inflation", "core_cpi_yoy": "inflation",
    "ppi_yoy": "inflation", "unemployment": "labor", "employment": "labor", "policy_rate": "rates", "rate_3m": "rates",
    "yield_2y": "rates", "yield_10y": "rates", "yield_curve": "rates", "curve_2s10s": "rates", "money_supply": "credit",
    "private_credit": "credit", "credit_gap": "credit", "lending_rate": "credit", "credit_spread": "credit",
    "financial_stress": "credit", "vix": "credit", "house_prices": "housing", "building_permits": "housing",
    "current_account": "external", "trade_balance": "external", "gov_debt": "fiscal", "gov_deficit": "fiscal",
    "reer": "fx", "commodities": "external",
}
#: macro_series.category -> section for series without an indicator
SECTION_OF_CATEGORY = {"growth": "growth", "sentiment": "growth", "inflation": "inflation", "labor": "labor",
                       "rates": "rates", "credit": "credit", "liquidity": "credit", "housing": "housing",
                       "trade": "external", "commodities": "external", "fiscal": "fiscal", "fx": "fx", "other": "growth"}
IND_ORDER = {k: i for i, k in enumerate([*CELL_INDICATORS, "yield_curve", "credit_gap", "lending_rate", "credit_spread",
                                         "financial_stress", "building_permits", "trade_balance"])}
HEADLINE = ["gdp_yoy", "cpi_yoy", "unemployment", "policy_rate", "yield_10y"]


def countries_list(db: Any) -> list[dict[str, Any]]:
    cells = latest_cells(db)
    by: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for c in cells:
        by[c["country"]][c["indicator"]] = c
    cyc = {r["country"]: r for r in db.all("""SELECT * FROM macro_cycle WHERE date <= ?
                                             QUALIFY row_number() OVER (PARTITION BY country ORDER BY date DESC) = 1""",
                                          [db.as_of])}
    out = []
    for cc in COUNTRIES:
        m = COUNTRY_META[cc]
        cs = by.get(cc, {})
        cy = cyc.get(cc)
        out.append({"country": cc, "name": m["name"], "currency": m["currency"], "equity_index": m["equity_index"],
                    "n_cells": len(cs), "n_stale": sum(1 for c in cs.values() if c.get("status") != "fresh"),
                    "quadrant": (cy or {}).get("quadrant"),
                    "growth_momentum": rnd((cy or {}).get("growth_momentum")),
                    "inflation_momentum": rnd((cy or {}).get("inflation_momentum")),
                    "headline": [cell_json(cs[k]) for k in HEADLINE if k in cs]})
    return out


def _section(ind: str | None, category: str | None) -> str:
    return SECTION_OF_INDICATOR.get(ind or "") or SECTION_OF_CATEGORY.get(category or "other", "growth")


def equity_vs_macro(db: Any, cc: str, years: int) -> dict[str, Any]:
    m = COUNTRY_META[cc]
    cut = db.as_of - timedelta(days=int(365.25 * years) + 400)
    cyc = db.all("SELECT date, growth_momentum, inflation_momentum, quadrant FROM macro_cycle WHERE country = ? "
                 "AND date >= ? AND date <= ? ORDER BY date", [cc, cut, db.as_of])
    eq: dict[date, float] = {}
    if m["equity_index"]:
        # monthly last adjusted close across serving `prices` (15y daily) + `prices_w` (weekly before): years <= 40
        from app.api import pricebars  # noqa: PLC0415

        rows = pricebars.bars(db, m["equity_index"], cut, db.as_of, "M", adjusted=True)
        eq = {r["date"].replace(day=1): r["c"] for r in rows if r["c"]}
    yoy: dict[date, float] = {}
    for d, px in eq.items():
        p0 = eq.get(date(d.year - 1, d.month, 1))
        if p0:
            yoy[d] = (px / p0 - 1) * 100
    gm = {r["date"]: r for r in cyc}
    start = db.as_of - timedelta(days=int(365.25 * years))
    dates = sorted(d for d in set(yoy) | set(gm) if d >= start)
    pts = [{"date": d.isoformat(), "index_yoy": rnd(yoy.get(d)), "growth_momentum": rnd((gm.get(d) or {}).get("growth_momentum")),
            "inflation_momentum": rnd((gm.get(d) or {}).get("inflation_momentum")), "quadrant": (gm.get(d) or {}).get("quadrant")}
           for d in dates]
    pairs = [(p["index_yoy"], p["growth_momentum"]) for p in pts if p["index_yoy"] is not None and p["growth_momentum"] is not None]
    return {"equity_index": m["equity_index"], "index_name": m["index_name"], "points": pts,
            "corr_index_yoy_growth": rnd(_corr(pairs)), "n_overlap": len(pairs),
            "note": "Equity index % y/y (monthly, last adjusted close) vs the cycle clock growth/inflation momentum (z)"}


def _corr(pairs: list[tuple[float, float]]) -> float | None:
    n = len(pairs)
    if n < 12:
        return None
    mx = sum(a for a, _ in pairs) / n
    my = sum(b for _, b in pairs) / n
    sxy = sum((a - mx) * (b - my) for a, b in pairs)
    sx = sum((a - mx) ** 2 for a, _ in pairs) ** 0.5
    sy = sum((b - my) ** 2 for _, b in pairs) ** 0.5
    return sxy / (sx * sy) if sx and sy else None


def country_page(db: Any, cc: str, years: int = 10, spark_years: int = 5) -> dict[str, Any]:
    cc = cc.upper()
    meta = COUNTRY_META[cc]
    cut = db.as_of - timedelta(days=int(365.25 * max(years, spark_years)))
    rows = db.all("""SELECT h.series_id, h.measure, h.indicator, h.date, h.value, h.units, h.transform, h.frequency,
                            h.is_primary, m.title, m.source_id, m.category
                     FROM macro_cell_history h LEFT JOIN macro_series m USING (series_id)
                     WHERE h.country = ? AND h.date >= ? AND h.date <= ? ORDER BY h.series_id, h.measure, h.date""",
                  [cc, cut, db.as_of])
    series: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["series_id"], r["measure"])
        s = series.get(k)
        if s is None:
            s = series[k] = {"series_id": r["series_id"], "measure": r["measure"], "indicator": r["indicator"],
                             "title": r["title"] or ("10Y - 2Y slope" if r["indicator"] == "curve_2s10s" else r["series_id"]),
                             "source_id": r["source_id"] or "derived", "units": r["units"], "transform": r["transform"],
                             "frequency": r["frequency"], "is_primary": bool(r["is_primary"]), "points": [],
                             "_section": _section(r["indicator"], r["category"])}
        s["points"].append((r["date"], r["value"]))
    cells = {c["indicator"]: c for c in latest_cells(db, [cc])}
    sec_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in series.values():
        pts = s.pop("points")
        last = next(((d, v) for d, v in reversed(pts) if v is not None), (None, None))
        s["last_date"], s["last_value"] = (last[0].isoformat() if last[0] else None), rnd(last[1], 6)
        s["spark"] = [{"date": d.isoformat(), "value": rnd(v, 6)} for d, v in pts
                      if d >= db.as_of - timedelta(days=int(365.25 * spark_years))]
        sec_items[s.pop("_section")].append(s)
    sections = []
    for key, title in SECTIONS:
        items = sorted(sec_items.get(key, []), key=lambda s: (not s["is_primary"], IND_ORDER.get(s["indicator"] or "", 99),
                                                              s["series_id"]))
        sections.append({"key": key, "title": title,
                         "cells": [cell_json(c) for k, c in sorted(cells.items(), key=lambda kv: IND_ORDER.get(kv[0], 99))
                                   if SECTION_OF_INDICATOR.get(k) == key],
                         "series": items})
    cyc = db.all("SELECT * FROM macro_cycle WHERE country = ? AND date <= ? ORDER BY date DESC LIMIT 12", [cc, db.as_of])
    return {"country": cc, "name": meta["name"], "currency": meta["currency"], "as_of": db.as_of.isoformat(),
            "cycle": {"latest": _cyc(cyc[0]) if cyc else None, "trail": [_cyc(r) for r in reversed(cyc)]},
            "sections": sections, "equity_vs_macro": equity_vs_macro(db, cc, years),
            "data_status": "ok" if series else "pending"}


def _cyc(r: dict[str, Any]) -> dict[str, Any]:
    return {"date": r["date"].isoformat(), "growth_momentum": rnd(r.get("growth_momentum")),
            "inflation_momentum": rnd(r.get("inflation_momentum")), "quadrant": r.get("quadrant"),
            "n_growth": r.get("n_growth"), "n_inflation": r.get("n_inflation")}
