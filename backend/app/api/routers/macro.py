"""Macro (tag: macro): regime model, country dashboard, series catalog & data. Also shared macro helpers (series
summaries, yield curves) used by the market overview."""

from __future__ import annotations

import bisect
import math
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api import data_integrity as DI
from app.api.deps import FastRoute, Db, get_db
from app.api.routers._common import fnum, iso, jload, label_for, not_found, rnd
from app.api.routers.companies import regime_snapshot

router = APIRouter(tags=["macro"], route_class=FastRoute)

MacroCategory = Literal["growth", "inflation", "rates", "credit", "fx", "commodities", "labor", "housing", "sentiment",
                        "liquidity", "trade", "fiscal", "other"]
CATEGORY_ORDER = ["growth", "inflation", "labor", "rates", "credit", "liquidity", "fx", "commodities", "housing",
                  "sentiment", "trade", "fiscal", "other"]
REGION_COUNTRY = {"US": "US", "CA": "CA", "EU": "EA", "UK": "GB", "JP": "JP", "KR": "KR", "CN": "CN", "HK": "HK",
                  "LATAM": "BR", "APAC": "JP", "OTHER": "US"}

#: sovereign curves: (curve_id, country, region, label, [(tenor, months, [series candidates])]); first candidate with data
#: wins (daily national sources first, then OECD / FRED monthly copies). Extended by macro-coverage (additive).
CURVES: list[tuple[str, str, str, str, list[tuple[str, int, list[str]]]]] = [
    ("UST", "US", "US", "US Treasury", [
        ("O/N", 0, ["fred:EFFR", "fred:SOFR"]), ("1M", 1, ["fred:DGS1MO", "ust:1M"]), ("3M", 3, ["fred:DGS3MO", "ust:3M"]),
        ("6M", 6, ["fred:DGS6MO", "ust:6M"]), ("1Y", 12, ["fred:DGS1", "ust:1Y"]), ("2Y", 24, ["fred:DGS2", "ust:2Y"]),
        ("3Y", 36, ["fred:DGS3", "ust:3Y"]), ("5Y", 60, ["fred:DGS5", "ust:5Y"]), ("7Y", 84, ["fred:DGS7", "ust:7Y"]),
        ("10Y", 120, ["fred:DGS10", "ust:10Y"]), ("20Y", 240, ["fred:DGS20", "ust:20Y"]), ("30Y", 360, ["fred:DGS30", "ust:30Y"])]),
    ("EA_AAA", "EA", "EU", "Euro area AAA", [
        ("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]),
        ("3M", 3, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"]), ("6M", 6, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_6M"]),
        ("1Y", 12, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_1Y"]), ("2Y", 24, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y"]),
        ("3Y", 36, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3Y"]), ("5Y", 60, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_5Y"]),
        ("7Y", 84, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_7Y"]), ("10Y", 120, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"]),
        ("15Y", 180, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_15Y"]), ("20Y", 240, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_20Y"]),
        ("30Y", 360, ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_30Y"])]),
    ("DE", "DE", "EU", "Germany Bund", [("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]), ("2Y", 24, ["bundesbank:BBSIS.D.R02XX"]),
                                        ("5Y", 60, ["bundesbank:BBSIS.D.R05XX"]),
                                        ("10Y", 120, ["bundesbank:BBSIS.D.R10XX", "fred:IRLTLT01DEM156N"]),
                                        ("30Y", 360, ["bundesbank:BBSIS.D.R30XX"])]),
    ("FR", "FR", "EU", "France OAT", [("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]), ("10Y", 120, ["eurostat:irt_lt_mcby_m.FR",
                                      "ecb:IRS.M.FR.L.L40.CI.0000.EUR.N.Z", "fred:IRLTLT01FRM156N"])]),
    ("IT", "IT", "EU", "Italy BTP", [("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]), ("10Y", 120, ["eurostat:irt_lt_mcby_m.IT",
                                     "ecb:IRS.M.IT.L.L40.CI.0000.EUR.N.Z", "fred:IRLTLT01ITM156N"])]),
    ("ES", "ES", "EU", "Spain Bono", [("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]), ("10Y", 120, ["eurostat:irt_lt_mcby_m.ES",
                                      "ecb:IRS.M.ES.L.L40.CI.0000.EUR.N.Z", "fred:IRLTLT01ESM156N"])]),
    ("NL", "NL", "EU", "Netherlands DSL", [("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]), ("10Y", 120, ["eurostat:irt_lt_mcby_m.NL",
                                           "ecb:IRS.M.NL.L.L40.CI.0000.EUR.N.Z", "fred:IRLTLT01NLM156N"])]),
    ("PT", "PT", "EU", "Portugal OT", [("O/N", 0, ["ecb:FM.D.U2.EUR.4F.KR.DFR.LEV", "fred:ECBDFR"]), ("2Y", 24, ["bpstat:12099454"]),
                                       ("10Y", 120, ["bpstat:12099459", "eurostat:irt_lt_mcby_m.PT", "fred:IRLTLT01PTM156N"])]),
    ("CH", "CH", "EU", "Swiss Confederation", [("O/N", 0, ["snb:snboffzisa.LZ", "bis:WS_CBPOL.D.CH"]),
                                               ("3M", 3, ["oecd:KEI.CHE.IR3TIB", "fred:IR3TIB01CHM156N"]),
                                               ("2Y", 24, ["snb:rendeiduebd.CHF.2J"]),
                                               ("10Y", 120, ["snb:rendeiduebd.CHF.10J", "fred:IRLTLT01CHM156N"])]),
    # A9 2026-11-07: zero-coupon gilt yields first (BoE yield-curve archive); the IADB par yields are frozen (robots)
    ("GB", "GB", "UK", "UK Gilt", [("O/N", 0, ["boe:IUDBEDR", "fred:IRSTCI01GBM156N"]),
                                   ("5Y", 60, ["boe:IUDSNZC", "boe:IUDSNPY"]),
                                   ("10Y", 120, ["boe:IUDMNZC", "boe:IUDMNPY", "fred:IRLTLT01GBM156N"]),
                                   ("20Y", 240, ["boe:IUDLNZC", "boe:IUDLNPY"])]),
    ("JP", "JP", "JP", "Japan JGB", [("O/N", 0, ["boj:FM01.STRDCLUCON", "fred:IRSTCI01JPM156N"]),
                                     ("3M", 3, ["oecd:KEI.JPN.IR3TIB", "fred:IR3TIB01JPM156N"]), ("2Y", 24, ["mof_jp:JGB.2Y"]),
                                     ("5Y", 60, ["mof_jp:JGB.5Y"]), ("10Y", 120, ["mof_jp:JGB.10Y", "fred:IRLTLT01JPM156N"]),
                                     ("20Y", 240, ["mof_jp:JGB.20Y"]), ("30Y", 360, ["mof_jp:JGB.30Y"])]),
    ("KR", "KR", "KR", "Korea KTB", [("O/N", 0, ["bok_ecos:722Y001.0101000", "bis:WS_CBPOL.D.KR", "fred:IRSTCI01KRM156N"]),
                                     ("3M", 3, ["bok_ecos:817Y002.010502000", "oecd:KEI.KOR.IR3TIB"]),
                                     ("2Y", 24, ["bok_ecos:817Y002.010195000"]),
                                     ("10Y", 120, ["bok_ecos:817Y002.010210000", "oecd:KEI.KOR.IRLT", "fred:IRLTLT01KRM156N"])]),
    ("CN", "CN", "CN", "China CGB", [("O/N", 0, ["bis:WS_CBPOL.D.CN", "akshare_cn:lpr_1y"]), ("3M", 3, ["akshare_cn:shibor_3m"]),
                                     ("2Y", 24, ["akshare_cn:cgb_2y"]), ("5Y", 60, ["akshare_cn:cgb_5y"]),
                                     ("10Y", 120, ["akshare_cn:cgb_10y", "oecd:KEI.CHN.IRLT"]), ("30Y", 360, ["akshare_cn:cgb_30y"])]),
    ("HK", "HK", "HK", "Hong Kong", [("O/N", 0, ["hkma:base_rate", "bis:WS_CBPOL.D.HK"]), ("3M", 3, ["hkma:hibor_3m"])]),
    ("IN", "IN", "APAC", "India G-Sec", [("O/N", 0, ["bis:WS_CBPOL.D.IN"]), ("3M", 3, ["oecd:KEI.IND.IR3TIB"]),
                                         ("10Y", 120, ["oecd:KEI.IND.IRLT"])]),
    ("AU", "AU", "APAC", "Australia ACGB", [("O/N", 0, ["rba:FIRMMCRTD", "bis:WS_CBPOL.D.AU"]), ("3M", 3, ["rba:FIRMMBAB90D"]),
                                            ("2Y", 24, ["rba:FCMYGBAG2D"]), ("5Y", 60, ["rba:FCMYGBAG5D"]),
                                            ("10Y", 120, ["rba:FCMYGBAG10D", "fred:IRLTLT01AUM156N"])]),
    ("CA", "CA", "CA", "Canada GoC", [("O/N", 0, ["boc:V39079", "bis:WS_CBPOL.D.CA"]), ("3M", 3, ["boc:TB.CDN.90D.MID"]),
                                      ("2Y", 24, ["boc:BD.CDN.2YR.DQ.YLD"]),
                                      ("10Y", 120, ["boc:BD.CDN.10YR.DQ.YLD", "fred:IRLTLT01CAM156N"])]),
    ("MX", "MX", "LATAM", "Mexico M-bono", [("O/N", 0, ["banxico:SF61745", "bis:WS_CBPOL.D.MX", "fred:IRSTCI01MXM156N"]),
                                            ("3M", 3, ["banxico:SF43783", "oecd:KEI.MEX.IR3TIB"]),
                                            ("10Y", 120, ["oecd:KEI.MEX.IRLT", "fred:IRLTLT01MXM156N"])]),
    ("BR", "BR", "LATAM", "Brazil", [("O/N", 0, ["bcb:432", "fred:IRSTCI01BRM156N"]), ("3M", 3, ["fred:INTGSTBRM193N"])]),
    ("CL", "CL", "LATAM", "Chile BCP", [("O/N", 0, ["bcch:F022.TPM.TIN.D001.NO.Z.D", "bis:WS_CBPOL.D.CL"]),
                                       ("3M", 3, ["oecd:KEI.CHL.IR3TIB"]),
                                       ("10Y", 120, ["oecd:KEI.CHL.IRLT", "fred:IRLTLT01CLM156N"])]),
    ("CO", "CO", "LATAM", "Colombia TES", [("O/N", 0, ["banrep:DF_CBR_DAILY_HIST.IRCBRM01", "bis:WS_CBPOL.D.CO"]),
                                           ("3M", 3, ["banrep:DF_IBR_DAILY_HIST.IRIBRM03.NR", "oecd:KEI.COL.IR3TIB"]),
                                           ("10Y", 120, ["banrep:DF_TES_MONTHLY_HIST.IRGBRY10", "oecd:KEI.COL.IRLT"])]),
    ("PE", "PE", "LATAM", "Peru", [("O/N", 0, ["bcrp:PD04722MM", "bis:WS_CBPOL.D.PE"]), ("10Y", 120, ["bcrp:PD31895MM"])]),
]
#: additional countries whose core series are appended (LIMIT 4 each) to the market overview key_macro (additive)
REGION_COUNTRIES_EXTRA: dict[str, list[str]] = {"EU": ["DE", "FR", "IT", "ES", "NL", "PT", "CH"],
                                                "LATAM": ["MX", "CL", "CO", "PE", "AR"], "APAC": ["AU", "IN", "TW"],
                                                "CN": ["HK"], "HK": ["CN"]}


def scale_of(units: str | None, category: str | None) -> str:
    u = (units or "").lower()
    if "percent" in u or u.strip() in ("%", "pct", "% p.a.") or u.startswith("% "):
        return "percent"
    if u in ("fraction", "ratio"):
        return "fraction"
    if category in ("rates", "credit") and not u:
        return "percent"
    return "level"


def series_meta(m: dict[str, Any], last_date: Any = None) -> dict[str, Any]:
    return {"series_id": m["series_id"], "source_id": m.get("source_id") or "", "title": m.get("title") or m["series_id"],
            "country": m.get("country"), "category": m.get("category") or "other", "frequency": m.get("frequency") or "M",
            "units": m.get("units"), "last_observation_date": iso(last_date or m.get("last_observation_date")),
            "scale": scale_value(m)}


def scale_value(m: dict[str, Any]) -> str:
    """macro_series.scale (contract v2) mapped onto the API enum (percent | fraction | level); index -> level."""
    s = m.get("scale")
    if s in ("percent", "fraction", "level"):
        return s
    if s == "index":
        return "level"
    return scale_of(m.get("units"), m.get("category"))


def series_summaries(db: Db, metas: list[dict[str, Any]], spark_n: int = 60) -> list[dict[str, Any]]:
    if not metas:
        return []
    ids = [m["series_id"] for m in metas]
    ph = ", ".join("?" for _ in ids)
    stats = {r["series_id"]: r for r in db.all(f"""
        WITH o AS (SELECT series_id, date, value, row_number() OVER (PARTITION BY series_id ORDER BY date DESC) AS rn,
                          max(date) OVER (PARTITION BY series_id) AS ld
                   FROM macro_obs WHERE series_id IN ({ph}) AND value IS NOT NULL AND isfinite(value) AND date <= ?)
        SELECT series_id, max(ld) AS last_date, max(value) FILTER (rn = 1) AS last_value, max(value) FILTER (rn = 2) AS prev,
               arg_max(value, date) FILTER (date <= ld - INTERVAL 365 DAY) AS y1,
               avg(value) FILTER (date > ld - INTERVAL 1826 DAY) AS m5, stddev_samp(value) FILTER (date > ld - INTERVAL 1826 DAY) AS s5,
               list(value ORDER BY date) FILTER (rn <= {int(spark_n)}) AS spark,
               list(date ORDER BY date) FILTER (rn <= {int(spark_n)}) AS spark_dates
        FROM o GROUP BY 1""", ids + [db.as_of])}
    out = []
    for m in metas:
        st = stats.get(m["series_id"], {})
        lv, prev, y1 = fnum(st.get("last_value")), fnum(st.get("prev")), fnum(st.get("y1"))
        sd = fnum(st.get("s5"))
        o = series_meta(m, st.get("last_date"))
        o.update({"last_value": lv, "change_1p": None if lv is None or prev is None else rnd(lv - prev),
                  "change_1y": None if lv is None or y1 is None else rnd(lv - y1),
                  "zscore_5y": None if lv is None or not sd else rnd((lv - (fnum(st.get("m5")) or 0)) / sd, 4),
                  "spark": [fnum(x) for x in (st.get("spark") or []) if fnum(x) is not None],
                  "spark_points": [{"date": iso(d), "value": fnum(v)} for d, v in zip(st.get("spark_dates") or [],
                                                                                      st.get("spark") or [], strict=False)]})
        out.append(o)
    disp = DI.display_fields(db, metas)  # O2.2a / F10: rate-of-change display fields (additive)
    for o in out:
        o.update(disp.get(o["series_id"], {}))
    return out


def yield_curves(db: Db, regions: list[str]) -> list[dict[str, Any]]:
    curves = [c for c in CURVES if not regions or c[2] in regions]
    ids = sorted({s for c in curves for _, _, cands in c[4] for s in cands})
    if not ids:
        return []
    ph = ", ".join("?" for _ in ids)
    obs = {r["series_id"]: r for r in db.all(f"""
        WITH o AS (SELECT series_id, date, value FROM macro_obs WHERE series_id IN ({ph}) AND value IS NOT NULL AND date <= ?),
        l AS (SELECT series_id, max(date) AS d FROM o GROUP BY 1)
        SELECT o.series_id, max(l.d) AS d, arg_max(o.value, o.date) AS v,
               arg_max(o.value, o.date) FILTER (o.date <= l.d - INTERVAL 30 DAY) AS v1m,
               arg_max(o.value, o.date) FILTER (o.date <= l.d - INTERVAL 365 DAY) AS v1y
        FROM o JOIN l USING (series_id) GROUP BY 1""", ids + [db.as_of])}
    out = []
    for cid, country, region, label, tenors in curves:
        pts = []
        for tenor, months, cands in tenors:
            sid = next((s for s in cands if s in obs and obs[s].get("v") is not None), None)
            if sid is None:
                continue
            o = obs[sid]
            pts.append({"tenor": tenor, "tenor_months": months, "yield": fnum(o["v"]), "yield_1m_ago": fnum(o.get("v1m")),
                        "yield_1y_ago": fnum(o.get("v1y")), "series_id": sid, "as_of": iso(o["d"])})
        if len(pts) >= 2:
            for p in pts:
                p["is_policy_rate"] = p["tenor"] == "O/N"  # F12: policy / overnight rate, not a market tenor
            out.append({"curve_id": cid, "country": country, "region": region, "label": label,
                        "as_of": max(p["as_of"] for p in pts), "as_of_min": min(p["as_of"] for p in pts),
                        "n_market_tenors": sum(1 for p in pts if p["tenor"] != "O/N"), "scale": "percent", "points": pts})
    return out


# ------------------------------------------------------------------------------------------------ endpoints


@router.get("/macro/regime", operation_id="getMacroRegime")
def get_macro_regime(scope: Literal["US", "GLOBAL", "EU", "JP", "CN", "UK", "KR", "HK", "CA", "LATAM", "APAC"] = "US",
                     years: int = Query(20, ge=1, le=60), db: Db = Depends(get_db)) -> dict[str, Any]:
    model = db.one("SELECT model_id, max(date) AS d FROM macro_regimes WHERE region_scope = ? GROUP BY 1 ORDER BY 2 DESC LIMIT 1",
                   [scope])
    if model is None:  # F07: never a fabricated label for a scope without its own model
        info = DI.regime_scope_info(db, scope)
        return {"model_id": "", "scope": scope, "modelled": False, "status": "not_modelled",
                "message": f"No regime model for {scope}; modelled scopes: {', '.join(info['available_scopes']) or 'none'}",
                "available_scopes": info["available_scopes"],
                "current": {"date": iso(db.as_of), "label": "not_modelled", "probabilities": {}, "modelled": False,
                            "scope": scope, "requested_scope": scope, "fallback": False, "model_id": None},
                "history": [], "inputs": [], "regime_sector_returns": [], "data_status": "pending"}
    rows = db.all("""SELECT * FROM macro_regimes WHERE model_id = ? AND date >= ?
                     QUALIFY row_number() OVER (PARTITION BY date_trunc('month', date) ORDER BY date DESC) = 1
                     ORDER BY date""", [model["model_id"], db.as_of - timedelta(days=int(years * 365.25))])
    latest = db.one("SELECT * FROM macro_regimes WHERE model_id = ? ORDER BY date DESC LIMIT 1", [model["model_id"]])
    inputs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        snap = jload(r.get("inputs_snapshot"), {}) or {}
        if isinstance(snap, dict):
            for k, v in snap.items():
                fv = fnum(v)
                if fv is not None or v is None:
                    inputs[k].append({"date": iso(r["date"]), "value": fv})
    rsr = db.all("""SELECT s.sector_id, replace(b.factor_id, 'regime_', '') AS regime_label, avg(b.beta) AS m
                    FROM macro_betas b JOIN sec s USING (security_id)
                    WHERE b.factor_id LIKE 'regime_%' AND s.sector_id IS NOT NULL AND b.beta IS NOT NULL
                    GROUP BY 1, 2 ORDER BY 1, 2""")
    labels = {"expansion", "slowdown", "contraction", "recovery", "stagflation"}
    cur = regime_snapshot(latest) or {}
    cur.update({"scope": scope, "requested_scope": scope, "modelled": True, "fallback": False, "model_id": model["model_id"]})
    # F08: drivers carry their TRUE unit (raw model inputs) plus the model's z-scores (fit standardization)
    drv = [{**x, "label": label_for(x["id"])} for x in DI.regime_inputs(scope, dict(inputs))]
    return {
        "model_id": model["model_id"], "scope": scope, "modelled": True, "status": "modelled", "current": cur,
        "available_scopes": DI.regime_scope_info(db, scope)["available_scopes"],
        "history": [regime_snapshot(r) for r in rows],
        "inputs": drv,
        "regime_sector_returns": [{"sector_id": r["sector_id"], "regime_label": r["regime_label"],
                                   "mean_excess_return": rnd(r["m"]) or 0.0} for r in rsr if r["regime_label"] in labels],
        "data_status": "ok",
    }


def _series_for_country(db: Db, country: str, core_only: bool = False) -> list[dict[str, Any]]:
    core = "AND is_core" if core_only else ""
    return db.all(f"""SELECT * FROM macro_series WHERE (upper(country) = ? OR (? = 'US' AND country IS NULL AND source_id = 'fred'))
                      {core} ORDER BY is_core DESC, category, series_id""", [country.upper(), country.upper()])


@router.get("/macro/dashboard", operation_id="getMacroDashboard")
def get_macro_dashboard(country: str = "US", db: Db = Depends(get_db)) -> dict[str, Any]:
    metas = _series_for_country(db, country)
    sums = series_summaries(db, metas)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sums:
        groups[s["category"]].append(s)
    return {"country": country.upper(),
            "groups": [{"category": c, "series": groups[c]} for c in CATEGORY_ORDER if groups.get(c)],
            "data_status": "ok" if sums else ("pending" if not db.snap.input_ok("macro_observations") else "ok")}


@router.get("/macro/series", operation_id="listMacroSeries")
def list_macro_series(category: MacroCategory | None = None, country: str | None = None, q: str | None = None,
                      db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    conds, params = ["TRUE"], []
    if category:
        conds.append("category = ?")
        params.append(category)
    if country:
        conds.append("upper(country) = ?")
        params.append(country.upper())
    if q:
        conds.append("(lower(title) LIKE ? OR lower(series_id) LIKE ?)")
        params += [f"%{q.lower()}%"] * 2
    rows = db.all(f"""SELECT m.*, (SELECT max(date) FROM macro_obs o WHERE o.series_id = m.series_id) AS ld
                      FROM macro_series m WHERE {' AND '.join(conds)} ORDER BY is_core DESC, category, series_id""", params)
    return [series_meta(r, r.get("ld")) for r in rows]


def _transform(points: list[tuple[date, float | None]], transform: str, freq: str) -> list[tuple[date, float | None]]:
    if transform == "level":
        return points
    out: list[tuple[date, float | None]] = []
    if transform == "yoy":
        dates = [d for d, _ in points]
        for d, v in points:
            target = d - timedelta(days=365)
            i = bisect.bisect_right(dates, target + timedelta(days=3)) - 1
            base = points[i][1] if i >= 0 and (target - dates[i]).days <= 40 else None
            out.append((d, None if v is None or not base else v / base - 1))
        return out
    prev = None
    for d, v in points:
        if transform == "diff":
            out.append((d, None if v is None or prev is None else v - prev))
        elif transform == "pct_change":
            out.append((d, None if v is None or not prev else v / prev - 1))
        elif transform == "log_diff":
            out.append((d, None if v is None or prev is None or v <= 0 or prev <= 0 else math.log(v / prev)))
        prev = v
    return out[1:] if out else out


@router.get("/macro/series/{series_id:path}", operation_id="getMacroSeries")
def get_macro_series(series_id: str, from_: date | None = Query(None, alias="from"), to: date | None = None,
                     transform: Literal["level", "diff", "pct_change", "yoy", "log_diff"] = "level",
                     db: Db = Depends(get_db)) -> dict[str, Any]:
    meta = db.one("SELECT * FROM macro_series WHERE series_id = ?", [series_id])
    if meta is None:
        raise not_found(f"unknown macro series {series_id!r}")
    end = to or db.as_of
    start = from_ or date(1900, 1, 1)
    lead = start - timedelta(days=400) if transform != "level" else start
    rows = db.all("SELECT date, value FROM macro_obs WHERE series_id = ? AND date BETWEEN ? AND ? ORDER BY date",
                  [series_id, lead, end])
    pts = _transform([(r["date"], fnum(r["value"])) for r in rows], transform, meta.get("frequency") or "M")
    last = rows[-1]["date"] if rows else None
    return {"meta": series_meta(meta, last), "transform": transform,
            "points": [{"date": iso(d), "value": rnd(v, 8)} for d, v in pts if d >= start]}
