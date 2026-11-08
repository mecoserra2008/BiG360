"""Markets -> Cross-asset dashboard (roadmap v2 item #5; contract v1.23.0).

Serving tables (app.api.serving._Builder.cross_asset): xa_assets (registry), xa_signals / xa_latest (daily per-asset
signals incl. the CFTC positioning join), xa_regime (correlation eigen-concentration + risk-on/off composite), xa_corr
(matrices now / ~1m / ~1y), xa_curve (Yahoo futures term structures) and xa_contracts (coverage register).
Definitions: docs/CROSS_ASSET_DEFINITIONS.md. Futures curves are Yahoo market data with APPROXIMATE expiry dates;
trend_signal, curve_shape, value proxies, z-scores and the risk composite are MODEL-BASED; positioning is CFTC
(official) as of the report date of each row.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import polars as pl
from fastapi import APIRouter, Depends, Query

from app.api.deps import Db, get_db
from app.api.routers._common import invalid, iso, not_found, rnd

router = APIRouter(tags=["cross-asset"])

CURVE_SOURCE = ("Yahoo Finance contract-month futures (market data, not official); expiry dates are APPROXIMATE "
                "(exchange last-trading-day rules on the NYSE holiday calendar)")
CURVE_HISTORY_NOTE = ("Curve history is short: Yahoo serves only contracts that are still trading, so dates before the "
                      "nightly job started hold only contracts that were still live at backfill time - the ~1y-ago curve "
                      "contains far-dated contracts only and its front is missing.")
MODEL_NOTE = ("trend_signal, curve_shape, value proxies, z-scores and the risk-on/off composite are model-based "
              "transformations, not published statistics.")
POSITIONING_SOURCE = "CFTC Commitments of Traders (official)"
POSITIONING_NOTE = ("CFTC (official) positioning: disaggregated futures-only managed money for commodities, TFF "
                    "futures-only leveraged funds for financial futures; latest report released on or before each date "
                    "(point-in-time). USD/JPY uses the inverted yen future (6J).")
ASSET_CLASSES = [("commodity", "Commodities"), ("fx", "FX (official fixings)"), ("equity_index", "Equity indices"),
                 ("rates", "US Treasuries"), ("credit", "US credit"), ("crypto", "Crypto futures")]
_AC = dict(ASSET_CLASSES)
CARRY_LABEL = {
    "futures_roll": "Futures roll yield ln(F1/F2) annualised (curve, Yahoo)",
    "fx_rate_diff": "Policy-rate differential base - quote (official central-bank rates)",
    "bond_carry_rolldown": "Yield over 3m funding + 1y roll-down (official CMT curve)",
    "equity_div_funding": "Dividend yield (tracking ETF, proxy) - local policy rate",
    "credit_oas": "OAS (ICE BofA via FRED); expected default losses not deducted",
}
VALUE_LABEL = {
    "reversal_5y": "5-year reversal: ln(price 4.5-5.5y ago / price now) (model-based)",
    "fx_real_5y": "Minus the 5-year real appreciation of the base currency (official CPI; model-based)",
    "real_yield": "TIPS real yield, percent (FRED, official)",
    "oas_level": "OAS level, percent (FRED, official)",
}
#: heatmap columns: z field used for the colour (None = bounded score) and the raw value shown
SIGNALS: list[dict[str, Any]] = [
    {"id": "trend", "label": "Trend", "z_field": None, "raw_field": "trend_signal", "scale": "score", "model_based": True,
     "description": "Model-based trend-following signal in [-1, 1] (1/3/12m momentum scaled by 63-day vol)"},
    {"id": "momentum", "label": "Mom 12m", "z_field": "z_mom_12m", "raw_field": "mom_12m", "scale": "z", "model_based": False,
     "description": "12-month log return; colour = its 3-year time-series z-score"},
    {"id": "carry", "label": "Carry", "z_field": "z_carry", "raw_field": "carry", "scale": "z", "model_based": False,
     "description": "Annualised carry (method per asset); colour = 3-year z-score"},
    {"id": "value", "label": "Value", "z_field": "z_value", "raw_field": "value", "scale": "z", "model_based": True,
     "description": "Value proxy (method per asset); colour = 3-year z-score"},
    {"id": "curve", "label": "Curve slope", "z_field": "z_curve_slope", "raw_field": "curve_slope", "scale": "z",
     "model_based": False, "description": "OLS slope of ln F on years to expiry (+ = contango, Yahoo curve); colour = z"},
    {"id": "vol", "label": "Realised vol", "z_field": "z_rv", "raw_field": "rv_1m", "scale": "z", "model_based": False,
     "description": "21-day realised volatility (annualised); colour = 3-year z-score"},
    {"id": "iv_rv", "label": "IV / RV", "z_field": "z_iv_rv", "raw_field": "iv_rv_ratio", "scale": "z", "model_based": False,
     "description": "Implied over realised volatility; colour = 3-year z-score"},
    {"id": "positioning", "label": "Positioning", "z_field": "cot_z_3y", "raw_field": "cot_index_3y", "scale": "z",
     "model_based": False, "description": "CFTC (official) speculator net position: COT index 3y shown, colour = z 3y"},
]
HIST_FIELDS = ("price", "trend_signal", "mom_1m", "mom_3m", "mom_12m", "rv_1m", "rv_3m", "iv", "iv_rv_ratio", "carry",
               "carry_curve", "carry_12m", "carry_fundamental", "dividend_yield", "funding_rate", "curve_slope", "value",
               "real_rate_diff", "z_mom_12m", "z_carry", "z_value", "z_rv", "z_iv_rv", "z_curve_slope", "cot_index_3y",
               "cot_z_3y", "cot_net_pct_oi")
RISK_COMPONENTS = [
    ("comp_equity_trend", "S&P 500 3m momentum", 1, "z of the S&P 500 63-day log return"),
    ("comp_vix", "VIX level", -1, "-z of the VIX close"),
    ("comp_vix_term", "VIX term structure", -1, "-z of ln(VIX / VIX3M); inversion = stress"),
    ("comp_credit_rel", "HY vs Treasuries", 1, "z of the 63-day change of ln(HYG / IEF) total return"),
    ("comp_hy_oas", "HY OAS", -1, "-z of the ICE BofA US HY OAS (FRED, official; history from 2023-10)"),
    ("comp_copper_gold", "Copper / gold", 1, "z of the 63-day change of ln(copper / gold) front months"),
    ("comp_audjpy", "AUD/JPY", 1, "z of the 63-day AUD/JPY log return (official fixings)"),
    ("comp_usd", "Broad dollar", -1, "-z of the 63-day change of the Fed broad dollar index (official)"),
]
RISK_DEFINITION = ("MODEL-BASED risk-on / risk-off composite: mean of the available component z-scores (>= 4 of 8; each "
                   "z over 756 days, clipped to +-3, signed so that + = risk-on). risk_on > 0.5, risk_off < -0.5.")
CORR_DEFINITION = ("Eigen-concentration = largest eigenvalue / N of the rolling correlation matrix of daily log returns "
                   "over a fixed 12-asset basket (1/N = no common factor, 1 = everything moves together). Closes are "
                   "asynchronous across time zones, which biases cross-region correlations down.")
_CACHE: dict[Any, Any] = {}


def _cache(key: Any, fn):
    if key in _CACHE:
        return _CACHE[key]
    out = fn()
    if len(_CACHE) > 256:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def _r(v: Any, nd: int = 4) -> float | None:
    return rnd(v, nd)


def _status(db: Db, *tables: str) -> str:
    return db.snap.status(*tables)


def _registry(db: Db) -> list[dict[str, Any]]:
    return _cache(("reg", db.snap.run_id), lambda: db.all("SELECT * FROM xa_assets ORDER BY sort_order"))


def _asset_ref(a: dict[str, Any]) -> dict[str, Any]:
    return {"asset_id": a["asset_id"], "name": a["name"], "asset_class": a["asset_class"], "sector": a["sector"],
            "instrument_id": a["instrument_id"], "linked_ids": list(a["linked_ids"] or []), "futures_root": a["futures_root"],
            "root_name": a["root_name"], "iv_source": a["iv_source"], "cot_report_type": a["cot_report_type"],
            "cot_category": a["cot_category"]}


def _labels() -> dict[str, str]:
    return {"curve_source": CURVE_SOURCE, "curve_history": CURVE_HISTORY_NOTE, "model_note": MODEL_NOTE,
            "positioning": POSITIONING_NOTE, "fx_source": "FX prices from official fixings (ECB > FRED H.10 > central bank)",
            "ust_source": "US Treasuries: synthetic constant-maturity total-return index from official H.15 CMT yields"}


def _positioning(r: dict[str, Any]) -> dict[str, Any] | None:
    if not r.get("cot_market_code"):
        return None
    return {"market_code": r["cot_market_code"], "report_type": r["cot_report_type"], "category": r["cot_category"],
            "report_date": iso(r["cot_report_date"]), "release_date": iso(r["cot_knowledge_date"]),
            "inverted": bool(r["cot_inverted"]), "cot_index_3y": _r(r["cot_index_3y"], 3), "z_3y": _r(r["cot_z_3y"], 2),
            "net_pct_oi": _r(r["cot_net_pct_oi"], 4), "source": POSITIONING_SOURCE}


def _methods(r: dict[str, Any]) -> dict[str, Any]:
    cm, vm = r.get("carry_method"), r.get("value_method")
    return {"carry_method": cm, "carry_label": CARRY_LABEL.get(cm or "") if cm else None, "value_method": vm,
            "value_label": VALUE_LABEL.get(vm or "") if vm else None}


# ------------------------------------------------------------------------------------------------ cockpit


@router.get("/cross-asset/cockpit", operation_id="getCrossAssetCockpit")
def cockpit(asset_class: str | None = Query(None, pattern="^(commodity|fx|equity_index|rates|credit|crypto)$"),
            security_id: str | None = Query(None, max_length=60), db: Db = Depends(get_db)) -> dict[str, Any]:
    key = ("cockpit", db.snap.run_id, asset_class, security_id)

    def build() -> dict[str, Any]:
        reg = _registry(db)
        latest = {r["asset_id"]: r for r in db.all("SELECT * FROM xa_latest")}
        as_of = max((r["date"] for r in latest.values()), default=None)
        rows = []
        counts: dict[str, int] = {}
        for a in reg:
            r = latest.get(a["asset_id"])
            if r is None:
                continue
            if security_id and security_id not in (a["linked_ids"] or []):
                continue
            counts[a["asset_class"]] = counts.get(a["asset_class"], 0) + 1
            if asset_class and a["asset_class"] != asset_class:
                continue
            cells = [{"z": _r(r[s["z_field"]], 2) if s["z_field"] else None, "raw": _r(r[s["raw_field"]], 4)} for s in SIGNALS]
            rows.append({**_asset_ref(a), "date": iso(r["date"]),
                         "stale": bool(as_of and (as_of - r["date"]).days > 10), "price": _r(r["price"], 6),
                         "price_source": r["price_source"], "curve_shape": r["curve_shape"], **_methods(r),
                         "cells": cells, "positioning": _positioning(r)})
        pos = [x["positioning"] for x in rows if x["positioning"]]
        return {"as_of": iso(as_of), "signals": SIGNALS, "rows": rows,
                "asset_classes": [{"id": k, "label": lbl, "n": counts.get(k, 0)} for k, lbl in ASSET_CLASSES if counts.get(k)],
                "positioning": {"source": POSITIONING_SOURCE, "n_assets": len(pos),
                                "report_date": max((p["report_date"] for p in pos), default=None),
                                "release_date": max((p["release_date"] for p in pos), default=None)},
                "labels": _labels(), "data_status": _status(db, "cross_asset_signals")}

    return _cache(key, build)


# ------------------------------------------------------------------------------------------------ asset detail


def _asset(db: Db, asset_id: str) -> dict[str, Any]:
    a = next((x for x in _registry(db) if x["asset_id"] == asset_id), None)
    if a is None:
        raise not_found(f"unknown cross-asset id {asset_id!r}")
    return a


@router.get("/cross-asset/assets/{asset_id}", operation_id="getCrossAssetAsset")
def asset_detail(asset_id: str, from_: date | None = Query(None, alias="from"), to: date | None = None,
                 db: Db = Depends(get_db)) -> dict[str, Any]:
    a = _asset(db, asset_id)
    if from_ and to and from_ > to:
        raise invalid("from must be <= to")
    key = ("asset", db.snap.run_id, asset_id, from_, to)

    def build() -> dict[str, Any]:
        last = db.one("SELECT * FROM xa_latest WHERE asset_id = ?", [asset_id])
        end = to or (last["date"] if last else db.as_of)
        start = from_ or (end - timedelta(days=365 * 5 + 2))
        db.cur.execute(f"""SELECT date, curve_shape, {', '.join(HIST_FIELDS)} FROM xa_signals
                           WHERE asset_id = ? AND date BETWEEN ? AND ? ORDER BY date""", [asset_id, start, end])
        rows = db.cur.fetchall()
        hist: dict[str, list[Any]] = {f: [] for f in HIST_FIELDS}
        dates, shapes = [], []
        for row in rows:
            dates.append(iso(row[0]))
            shapes.append(row[1])
            for j, f in enumerate(HIST_FIELDS):
                v = row[j + 2]
                hist[f].append(None if v is None or v != v else round(v, 6 if f == "price" else 4))
        first = db.scalar("SELECT min(date) FROM xa_signals WHERE asset_id = ?", [asset_id])
        latest = None
        if last:
            latest = {f: _r(last[f], 6 if f == "price" else 4) for f in HIST_FIELDS} | {
                "date": iso(last["date"]), "curve_shape": last["curve_shape"], "carry_front_contract": last["carry_front_contract"],
                "carry_dte_front": last["carry_dte_front"], "carry_dte_next": last["carry_dte_next"], "curve_n": last["curve_n"],
                "iv_units": last["iv_units"], "price_source": last["price_source"], "mom_12_1": _r(last["mom_12_1"])}
        return {"asset": _asset_ref(a), "latest": latest, **_methods(last or {}), "positioning": _positioning(last or {}),
                "first_date": iso(first), "dates": dates, "curve_shape": shapes, "series": hist,
                "has_curve": bool(a["futures_root"]) and bool(db.scalar("SELECT count(*) FROM xa_curve WHERE root = ?",
                                                                        [a["futures_root"]])),
                "labels": _labels(), "data_status": "ok" if dates else _status(db, "cross_asset_signals")}

    return _cache(key, build)


# ------------------------------------------------------------------------------------------------ curves


def _coverage_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    out = {"served": 0, "missing": 0, "unchecked": 0, "expired": 0}
    for r in rows:
        out[r["status"]] = out.get(r["status"], 0) + 1
    return out


@router.get("/cross-asset/curves", operation_id="listCrossAssetCurves")
def list_curves(db: Db = Depends(get_db)) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        reg = {a["futures_root"]: a for a in _registry(db) if a["futures_root"]}
        cov: dict[str, list[dict[str, Any]]] = {}
        for r in db.all("SELECT root, status, root_name, asset_class, exchange FROM xa_contracts"):
            cov.setdefault(r["root"], []).append(r)
        last = {r["root"]: r for r in db.all("""
            SELECT root, max(date) AS last_date, min(date) AS first_date FROM xa_curve GROUP BY root""")}
        nlast = {r["root"]: r["n"] for r in db.all("""
            SELECT c.root, count(*) AS n FROM xa_curve c
            JOIN (SELECT root, max(date) AS d FROM xa_curve GROUP BY root) m ON m.root = c.root AND m.d = c.date
            GROUP BY c.root""")}
        lat = {r["futures_root"]: r for r in db.all("SELECT * FROM xa_latest WHERE futures_root IS NOT NULL")}
        items = []
        for root, a in reg.items():
            c = cov.get(root, [])
            meta = c[0] if c else {}
            sig = lat.get(root) or {}
            sign = a["curve_sign"] or 1
            items.append({"root": root, "name": a["root_name"] or meta.get("root_name") or root,
                          "futures_class": meta.get("asset_class"), "exchange": a["root_exchange"],
                          "asset_id": a["asset_id"], "asset_class": a["asset_class"],
                          "first_date": iso((last.get(root) or {}).get("first_date")),
                          "last_date": iso((last.get(root) or {}).get("last_date")), "n_contracts_last": nlast.get(root, 0),
                          "curve_shape": sig.get("curve_shape"), "curve_slope": _r(sig.get("curve_slope")),
                          "roll_yield": _r(sig["carry_curve"] * sign) if sig.get("carry_curve") is not None else None,
                          "coverage": _coverage_counts([x for x in c if x["status"] != "expired"])})
        return {"items": items, "labels": _labels(), "data_status": _status(db, "futures_curve", "futures_contracts")}

    return _cache(("curves", db.snap.run_id), build)


def _snapshot_points(db: Db, root: str, d: date | None) -> list[dict[str, Any]]:
    if d is None:
        return []
    return [{"contract": r["contract"], "contract_month": iso(r["contract_month"]), "symbol": r["symbol"],
             "expiry": iso(r["expiry"]), "days_to_expiry": r["days_to_expiry"], "close": _r(r["close"], 6),
             "volume": r["volume"]}
            for r in db.all("""SELECT contract, contract_month, symbol, expiry, days_to_expiry, close, volume FROM xa_curve
                               WHERE root = ? AND date = ? ORDER BY expiry""", [root, d])]


@router.get("/cross-asset/curves/{root}", operation_id="getCrossAssetCurve")
def curve(root: str, from_: date | None = Query(None, alias="from"), db: Db = Depends(get_db)) -> dict[str, Any]:
    root = root.upper()
    a = next((x for x in _registry(db) if x["futures_root"] == root), None)
    if a is None:
        raise not_found(f"unknown futures root {root!r}")
    return _cache(("curve", db.snap.run_id, root, from_), lambda: _curve(db, root, a, from_))


def _curve(db: Db, root: str, a: dict[str, Any], from_: date | None) -> dict[str, Any]:
    from app.analytics.cross_asset import FRONT_MAX_DTE, SHAPE_EPS, curve_metrics  # noqa: PLC0415

    last = db.scalar("SELECT max(date) FROM xa_curve WHERE root = ?", [root])
    snaps = []
    for label, back in (("latest", 0), ("1m", 30), ("1y", 365)):
        tgt = last - timedelta(days=back) if last else None
        d = db.scalar("SELECT max(date) FROM xa_curve WHERE root = ? AND date <= ?", [root, tgt]) if tgt else None
        pts = _snapshot_points(db, root, d)
        snaps.append({"label": label, "target_date": iso(tgt), "date": iso(d), "n_contracts": len(pts),
                      "front_days_to_expiry": pts[0]["days_to_expiry"] if pts else None, "points": pts})
    start = from_ or date(1990, 1, 1)
    raw = db.cur.execute("""SELECT root, date, contract, days_to_expiry, close FROM xa_curve
                            WHERE root = ? AND date >= ?""", [root, start]).pl()
    dates: list[str] = []
    hist: dict[str, list[Any]] = {"curve_slope": [], "roll_yield": [], "roll_yield_12m": [], "curve_n": [], "n_contracts": [],
                                  "front_close": [], "front_days_to_expiry": []}
    shapes: list[str | None] = []
    if raw.height:
        cm = curve_metrics(raw.with_columns(pl.col("days_to_expiry").cast(pl.Int16), pl.col("close").cast(pl.Float32)))
        front = (raw.filter(pl.col("days_to_expiry") >= 5).sort("date", "days_to_expiry")
                 .group_by("date", maintain_order=True)
                 .agg(pl.col("close").first().alias("front_close"), pl.col("days_to_expiry").first().alias("front_dte"),
                      pl.len().alias("n_contracts")))
        h = cm.join(front, on="date", how="left").sort("date")
        for r in h.iter_rows(named=True):
            dates.append(iso(r["date"]))
            s = r["curve_slope"]
            hist["curve_slope"].append(_r(s))
            hist["roll_yield"].append(_r(r["carry_curve"]))
            hist["roll_yield_12m"].append(_r(r["carry_12m"]))
            hist["curve_n"].append(r["curve_n"])
            hist["n_contracts"].append(r["n_contracts"])
            hist["front_close"].append(_r(r["front_close"], 6))
            hist["front_days_to_expiry"].append(r["front_dte"])
            shapes.append(None if s is None else "contango" if s > SHAPE_EPS else "backwardation" if s < -SHAPE_EPS else "flat")
    cov = db.all("""SELECT contract, contract_month, symbol, expiry, expiry_rule, status, first_date, last_date, n_bars,
                           last_close, last_checked, root_name, asset_class, exchange
                    FROM xa_contracts WHERE root = ? ORDER BY contract_month""", [root])
    cur = [c for c in cov if c["status"] != "expired"]
    meta = cov[0] if cov else {}
    return {
        "root": root, "name": a["root_name"] or meta.get("root_name") or root, "exchange": a["root_exchange"],
        "futures_class": meta.get("asset_class"), "expiry_rule": meta.get("expiry_rule"), "asset": _asset_ref(a),
        "last_date": iso(last), "snapshots": snaps, "dates": dates, "curve_shape": shapes, "series": hist,
        "coverage": [{"contract": c["contract"], "contract_month": iso(c["contract_month"]), "symbol": c["symbol"],
                      "expiry": iso(c["expiry"]), "status": c["status"], "first_date": iso(c["first_date"]),
                      "last_date": iso(c["last_date"]), "n_bars": c["n_bars"], "last_close": _r(c["last_close"], 6),
                      "last_checked": iso(c["last_checked"])} for c in cov],
        "coverage_counts": _coverage_counts(cur),
        "definitions": {"roll_yield": f"ln(F1 / F2) * 365 / (T2 - T1), nearest two contracts with >= 5 days to expiry, front "
                                      f"<= {FRONT_MAX_DTE} days (+ = backwardation); futures-root perspective (not sign-"
                                      "flipped for USD/X pairs)",
                        "curve_slope": "OLS slope of ln F on years to expiry, contracts 5-400 days (>= 3); + = contango",
                        "curve_shape": f"MODEL-BASED label: contango if slope > +{SHAPE_EPS:.0%}/yr, backwardation if < "
                                       f"-{SHAPE_EPS:.0%}/yr, else flat"},
        "labels": _labels(), "data_status": "ok" if last else _status(db, "futures_curve"),
    }


# ------------------------------------------------------------------------------------------------ regime


def _basket(db: Db) -> list[str]:
    b = db.scalar("SELECT basket FROM xa_regime WHERE basket IS NOT NULL ORDER BY date DESC LIMIT 1")
    if b:
        return [x for x in str(b).split(",") if x]
    from app.analytics.cross_asset import CORR_BASKET  # noqa: PLC0415

    return list(CORR_BASKET)


@router.get("/cross-asset/correlation", operation_id="getCrossAssetCorrelation")
def correlation(window: int = Query(120, ge=60, le=120), from_: date | None = Query(None, alias="from"),
                db: Db = Depends(get_db)) -> dict[str, Any]:
    if window not in (60, 120):
        raise invalid("window must be 60 or 120")

    def build() -> dict[str, Any]:
        names = _basket(db)
        reg = {a["asset_id"]: a for a in _registry(db)}
        idx = {n: i for i, n in enumerate(names)}
        mats = []
        rows = db.all("""SELECT snapshot, as_of, n_obs, asset_i, asset_j, corr FROM xa_corr WHERE "window" = ?""", [window])
        for snap in ("latest", "1m", "1y"):
            sr = [r for r in rows if r["snapshot"] == snap]
            if not sr:
                continue
            m: list[list[float | None]] = [[None] * len(names) for _ in names]
            for r in sr:
                if r["asset_i"] in idx and r["asset_j"] in idx:
                    m[idx[r["asset_i"]]][idx[r["asset_j"]]] = _r(r["corr"], 3)
            mats.append({"snapshot": snap, "as_of": iso(sr[0]["as_of"]), "n_obs": sr[0]["n_obs"], "values": m})
        start = from_ or date(1990, 1, 1)
        db.cur.execute("""SELECT date, eig1_share_60, eig1_share_120, avg_corr_60, avg_corr_120, eig1_z_120 FROM xa_regime
                          WHERE date >= ? AND (eig1_share_60 IS NOT NULL OR eig1_share_120 IS NOT NULL) ORDER BY date""",
                       [start])
        hist: dict[str, list[Any]] = {k: [] for k in ("eig1_share_60", "eig1_share_120", "avg_corr_60", "avg_corr_120", "eig1_z_120")}
        dates = []
        for row in db.cur.fetchall():
            dates.append(iso(row[0]))
            for j, k in enumerate(hist):
                v = row[j + 1]
                hist[k].append(None if v is None or v != v else round(v, 4))
        latest = None
        if dates:
            latest = {"date": dates[-1], **{k: v[-1] for k, v in hist.items()}}
        return {"window": window, "basket": [{"asset_id": n, "name": (reg.get(n) or {}).get("name") or n,
                                              "asset_class": (reg.get(n) or {}).get("asset_class")} for n in names],
                "n_assets": len(names), "min_share": round(1 / len(names), 4) if names else None, "matrices": mats,
                "dates": dates, "series": hist, "latest": latest, "definition": CORR_DEFINITION, "model_based": True,
                "data_status": _status(db, "cross_asset_regime", "cross_asset_corr")}

    return _cache(("corr", db.snap.run_id, window, from_), build)


@router.get("/cross-asset/risk", operation_id="getCrossAssetRisk")
def risk(from_: date | None = Query(None, alias="from"), db: Db = Depends(get_db)) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        comps = [c[0] for c in RISK_COMPONENTS]
        start = from_ or date(1990, 1, 1)
        db.cur.execute(f"""SELECT date, risk_composite, risk_regime, risk_n_components, {', '.join(comps)} FROM xa_regime
                           WHERE date >= ? AND risk_composite IS NOT NULL ORDER BY date""", [start])
        dates, comp, regime, ncomp = [], [], [], []
        ch: dict[str, list[Any]] = {c: [] for c in comps}
        for row in db.cur.fetchall():
            dates.append(iso(row[0]))
            comp.append(None if row[1] is None or row[1] != row[1] else round(row[1], 3))
            regime.append(row[2])
            ncomp.append(row[3])
            for j, c in enumerate(comps):
                v = row[4 + j]
                ch[c].append(None if v is None or v != v else round(v, 3))
        latest = None
        if dates:
            latest = {"date": dates[-1], "risk_composite": comp[-1], "risk_regime": regime[-1], "n_components": ncomp[-1],
                      "components": [{"id": c, "label": lbl, "sign": sg, "description": desc, "z": ch[c][-1]}
                                     for c, lbl, sg, desc in RISK_COMPONENTS]}
            # change vs ~1 month (21 rows) earlier
            if len(comp) > 21:
                latest["change_1m"] = round(comp[-1] - comp[-22], 3) if comp[-1] is not None and comp[-22] is not None else None
        return {"latest": latest, "dates": dates, "risk_composite": comp, "risk_regime": regime, "n_components": ncomp,
                "components": [{"id": c, "label": lbl, "sign": sg, "description": desc, "values": ch[c]}
                               for c, lbl, sg, desc in RISK_COMPONENTS],
                "thresholds": {"risk_on": 0.5, "risk_off": -0.5}, "definition": RISK_DEFINITION, "model_based": True,
                "data_status": _status(db, "cross_asset_regime")}

    return _cache(("risk", db.snap.run_id, from_), build)
