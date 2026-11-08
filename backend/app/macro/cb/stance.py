"""W3.4 Policy stance (job cb_stance): real policy rates, Taylor-rule estimates (MODEL-BASED), official r*.

Monthly per bank (method ``stance_v1``):
  real_rate_cpi / real_rate_core   policy rate (month end) - headline / core inflation y/y (W1 canonical cells,
                                   official statistics; SE/NO headline from the OECD CPI series via FRED)
  expected_infl                    US: Cleveland Fed 1-year expected inflation (FRED EXPINF1YR, model-based by the
                                   Cleveland Fed); others: our AR(1) forecast of headline y/y 12 months ahead fitted on
                                   the trailing 120 months (model-based, labelled ``ar1_12m``)
  output_gap                       US: CBO (100 * (GDPC1 / GDPPOT - 1), quarterly); others: OECD Economic Outlook
                                   output gap (% of potential, annual; current/next year are OECD projections)
  r_star                           NY Fed official estimates: Laubach-Williams (US, one-sided) and Holston-Laubach-
                                   Williams (Canada, Euro Area); other banks: none published in machine-readable form
  Taylor rules (model-based):      pi = target measure (US core PCE y/y; others core CPI, else headline)
    taylor_1993    = 2 + pi + 0.5 (pi - target) + 0.5 gap            (Taylor 1993, r* = 2%)
    taylor_rstar   = r* + pi + 0.5 (pi - target) + 0.5 gap           (official r*, where available)
    taylor_balanced= r*|2 + pi + 0.5 (pi - target) + 1.0 gap          ("balanced approach" gap weight)
Inflation targets: app.macro.cb.banks (official mandates).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from .banks import BANKS
from .common import http_get, now_utc, read_table_safe, to_float, update_state, write

log = get_logger(__name__)
METHOD = "stance_v1"
START = date(2000, 1, 1)
LW_URL = "https://www.newyorkfed.org/medialibrary/media/research/economists/williams/data/Laubach_Williams_current_estimates.xlsx"
HLW_URL = ("https://www.newyorkfed.org/medialibrary/media/research/economists/williams/data/"
           "Holston_Laubach_Williams_current_estimates.xlsx")
OECD_GAP_URL = "https://sdmx.oecd.org/public/rest/data/OECD.ECO.MAD,DSD_EO@DF_EO,/.GAP.A"
OECD_ISO3 = {"US": "USA", "EA": "EA17", "GB": "GBR", "JP": "JPN", "CH": "CHE", "CA": "CAN", "AU": "AUS", "SE": "SWE",
             "NO": "NOR", "BR": "BRA", "MX": "MEX", "CL": "CHL", "CO": "COL", "PE": "PER", "IN": "IND", "KR": "KOR",
             "CN": "CHN"}
FRED_CPI_EXTRA = {"SE": "CPALTT01SEM659N", "NO": "CPALTT01NOM659N"}


def _m(d: date) -> date:
    return date(d.year, d.month, 1)


# =============================================================================================== parsers

def parse_lw(content: bytes) -> list[tuple[date, float, float]]:
    """LW 'data' sheet: Date, -, one-sided rstar, g, z, output gap, ... -> [(quarter start, rstar, gap)]."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    out = []
    for r in wb["data"].iter_rows(values_only=True):
        if r and hasattr(r[0], "year") and len(r) > 5:
            rs, gap = to_float(r[2]), to_float(r[5])
            if rs is not None:
                out.append((r[0].date() if hasattr(r[0], "date") else r[0], rs, gap))
    return out


def parse_hlw(content: bytes) -> dict[str, list[tuple[date, float]]]:
    """HLW 'HLW Estimates': header row 'Date', -, g(US,CA,EA), -, z(US,CA,EA), -, r*(US,CA,EA)."""
    import openpyxl  # noqa: PLC0415

    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb["HLW Estimates"]
    rows = list(ws.iter_rows(values_only=True))
    hdr_i = next((i for i, r in enumerate(rows) if r and r[0] == "Date"), None)
    sec_i = hdr_i - 1 if hdr_i else None
    if hdr_i is None:
        return {}
    sec = rows[sec_i]
    start = next((i for i, c in enumerate(sec) if isinstance(c, str) and "r*" in c), None)
    if start is None:
        return {}
    names = {"US": "US", "Canada": "CA", "Euro Area": "EA"}
    cols = {names[str(rows[hdr_i][j]).strip()]: j for j in range(start, min(start + 3, len(rows[hdr_i])))
            if str(rows[hdr_i][j]).strip() in names}
    out: dict[str, list[tuple[date, float]]] = {}
    for r in rows[hdr_i + 1:]:
        if not r or not hasattr(r[0], "year"):
            continue
        for cc, j in cols.items():
            v = to_float(r[j])
            if v is not None:
                out.setdefault(cc, []).append((r[0].date() if hasattr(r[0], "date") else r[0], v))
    return out


def parse_oecd_gap(text: str) -> dict[str, dict[int, float]]:
    inv = {v: k for k, v in OECD_ISO3.items()}
    out: dict[str, dict[int, float]] = {}
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        cc = inv.get(r.get("REF_AREA", ""))
        v = to_float(r.get("OBS_VALUE"))
        tp = (r.get("TIME_PERIOD") or "")[:4]
        if cc and v is not None and tp.isdigit():
            out.setdefault(cc, {})[int(tp)] = v
    return out


# =============================================================================================== models

def ar1_forecast(y: list[float], h: int = 12) -> float | None:
    """h-step forecast of an AR(1) with intercept fitted by OLS on y (monthly y/y inflation)."""
    a = np.asarray([v for v in y if v is not None and not np.isnan(v)], dtype=float)
    if a.size < 36:
        return None
    x, z = a[:-1], a[1:]
    X = np.column_stack([np.ones_like(x), x])
    c, phi = np.linalg.lstsq(X, z, rcond=None)[0]
    if not -0.99 <= phi <= 0.995:  # keep it stationary; re-fit the intercept for the clipped slope
        phi = float(np.clip(phi, -0.99, 0.995))
        c = float(np.mean(z - phi * x))
    f = a[-1]
    for _ in range(h):
        f = c + phi * f
    return float(f)


def taylor(pi: float | None, target: float | None, gap: float | None, rstar: float | None, gap_w: float = 0.5
           ) -> float | None:
    if pi is None or target is None or gap is None or rstar is None:
        return None
    return rstar + pi + 0.5 * (pi - target) + gap_w * gap


# =============================================================================================== build

def _cells(s: Settings) -> dict[tuple[str, str], dict[date, float]]:
    df = read_table_safe("macro_cell_history", s, columns=["country", "indicator", "date", "value", "is_primary"])
    out: dict[tuple[str, str], dict[date, float]] = {}
    if df is None:
        return out
    df = df.filter(pl.col("is_primary") & pl.col("indicator").is_in(["cpi_yoy", "core_cpi_yoy"]) &
                   pl.col("value").is_not_null())
    for cc, ind, d, v, _ in df.iter_rows():
        out.setdefault((cc, ind), {})[_m(d)] = v
    return out


def _yoy(pairs: list[tuple[date, float]]) -> dict[date, float]:
    m = {_m(d): v for d, v in pairs}
    return {d: (v / m[date(d.year - 1, d.month, 1)] - 1) * 100 for d, v in m.items()
            if date(d.year - 1, d.month, 1) in m and m[date(d.year - 1, d.month, 1)]}


def build_stance(s: Settings | None = None, write_out: bool = True) -> dict[str, Any]:
    from .rates import fetch_fred  # noqa: PLC0415

    s = s or get_settings()
    rep: dict[str, Any] = {}
    cells = _cells(s)
    fred = {}
    try:
        fred = fetch_fred(["PCEPILFE", "GDPC1", "GDPPOT", "EXPINF1YR", *FRED_CPI_EXTRA.values()], s)
    except Exception as e:  # noqa: BLE001
        rep["fred"] = f"failed: {e}"
    for cc, sid in FRED_CPI_EXTRA.items():
        if fred.get(sid):
            cells[(cc, "cpi_yoy")] = {_m(d): v for d, v in fred[sid]}
    core_pce = _yoy(fred.get("PCEPILFE", []))
    exp_us = {_m(d): v for d, v in fred.get("EXPINF1YR", [])}
    gdp, pot = dict(fred.get("GDPC1", [])), dict(fred.get("GDPPOT", []))
    cbo_gap = {d: (gdp[d] / pot[d] - 1) * 100 for d in gdp if d in pot and pot[d]}
    lw, hlw, oecd = [], {}, {}
    try:
        lw = parse_lw(http_get("nyfed_rstar", LW_URL, ttl=24 * 3600, settings=s))
    except Exception as e:  # noqa: BLE001
        rep["lw"] = f"failed: {e}"
    try:
        hlw = parse_hlw(http_get("nyfed_rstar", HLW_URL, ttl=24 * 3600, settings=s))
    except Exception as e:  # noqa: BLE001
        rep["hlw"] = f"failed: {e}"
    try:
        oecd = parse_oecd_gap(http_get("oecd", OECD_GAP_URL, {"startPeriod": "1999", "format": "csvfilewithlabels"},
                                       ttl=20 * 3600, settings=s, timeout=180).decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        rep["oecd_gap"] = f"failed: {e}"
    rstar: dict[str, dict[date, tuple[float, str]]] = {
        "US": {d: (v, "nyfed:LW one-sided") for d, v, _ in lw}}
    for cc, pairs in hlw.items():
        if cc != "US":
            rstar[cc] = {d: (v, "nyfed:HLW") for d, v in pairs}
    rates = read_table_safe("cb_policy_rates", s, columns=["bank", "date", "policy_rate"])
    if rates is None:
        return {"error": "cb_policy_rates missing (run cb_rates first)"}
    pm = (rates.drop_nulls("policy_rate").with_columns(pl.col("date").dt.truncate("1mo").alias("m"))
          .sort("date").group_by(["bank", "m"]).agg(pl.col("policy_rate").last()))
    pmap: dict[str, dict[date, float]] = {}
    for b, m, v in pm.iter_rows():
        pmap.setdefault(b, {})[m] = v
    now = now_utc()
    rows = []
    for b in BANKS:
        cc = b.country
        pol = pmap.get(b.code, {})
        cpi = cells.get((cc, "cpi_yoy"), {})
        core = cells.get((cc, "core_cpi_yoy"), {})
        months = sorted(m for m in pol if m >= START)
        cpi_hist = sorted(cpi.items())
        for m in months:
            p = pol[m]
            c, k = _last(cpi, m), _last(core, m)
            if cc == "US":
                pi, pim = _last(core_pce, m), "core PCE y/y"
            else:
                pi, pim = (k, "core CPI y/y") if k is not None else (c, "headline CPI y/y")
            if cc == "US" and _last(exp_us, m, 2) is not None:
                ex, exs = _last(exp_us, m, 2), "Cleveland Fed 1y expected inflation (FRED EXPINF1YR, model-based)"
            else:
                hist = [v for d, v in cpi_hist if d <= m][-120:]
                ex, exs = ar1_forecast(hist), "AR(1) on headline CPI y/y, 12m ahead, trailing 120m (model-based)"
                if ex is None:
                    exs = None
            if cc == "US":
                q = date(m.year, (m.month - 1) // 3 * 3 + 1, 1)
                gap = _last(cbo_gap, q, 6)
                gsrc = "CBO potential GDP (FRED GDPPOT) vs real GDP (GDPC1)" if gap is not None else None
            else:
                gap = oecd.get(cc, {}).get(m.year)
                gsrc = "OECD Economic Outlook output gap (annual; current/next year projected)" if gap is not None else None
            rs = _last(rstar.setdefault(cc, {}), m, 9)
            rsv, rss = (rs[0], rs[1]) if rs else (None, None)
            tgt = b.target
            rows.append({"bank": b.code, "date": m, "policy_rate": p, "cpi_yoy": c, "core_yoy": k,
                         "infl_taylor": pi, "infl_taylor_measure": pim if pi is not None else None,
                         "expected_infl": ex, "expected_infl_source": exs,
                         "real_rate_cpi": None if c is None else p - c, "real_rate_core": None if k is None else p - k,
                         "real_rate_expected": None if ex is None else p - ex,
                         "output_gap": gap, "output_gap_source": gsrc, "r_star": rsv, "r_star_source": rss,
                         "inflation_target": tgt, "taylor_1993": taylor(pi, tgt, gap, 2.0),
                         "taylor_rstar": taylor(pi, tgt, gap, rsv),
                         "taylor_balanced": taylor(pi, tgt, gap, rsv if rsv is not None else 2.0, 1.0)})
    schema = {"bank": pl.Utf8, "date": pl.Date, "policy_rate": pl.Float64, "cpi_yoy": pl.Float64, "core_yoy": pl.Float64,
              "infl_taylor": pl.Float64, "infl_taylor_measure": pl.Utf8, "expected_infl": pl.Float64,
              "expected_infl_source": pl.Utf8, "real_rate_cpi": pl.Float64, "real_rate_core": pl.Float64,
              "real_rate_expected": pl.Float64, "output_gap": pl.Float64, "output_gap_source": pl.Utf8,
              "r_star": pl.Float64, "r_star_source": pl.Utf8, "inflation_target": pl.Float64,
              "taylor_1993": pl.Float64, "taylor_rstar": pl.Float64, "taylor_balanced": pl.Float64}
    df = pl.DataFrame(rows, schema=schema).with_columns(pl.lit(METHOD).alias("method_version"),
                                                        pl.lit(now).alias("computed_at"))
    rep["rows"] = df.height
    rep["latest"] = {r["bank"]: {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()
                                 if k in ("date", "policy_rate", "real_rate_cpi", "taylor_1993", "taylor_rstar", "r_star")}
                     for r in df.sort("date").group_by("bank").last().to_dicts()}
    if write_out:
        rep["written"] = write(df, "cb_stance", s)
    return rep


_KEYS: dict[int, tuple[Any, list[date]]] = {}


def _last(m: dict[date, Any], d: date, max_months: int = 3) -> Any:
    """Latest value at or before d within max_months (sorted keys cached per dict)."""
    from bisect import bisect_right  # noqa: PLC0415

    ent = _KEYS.get(id(m))
    if ent is None or ent[0] is not m or len(ent[1]) != len(m):
        ent = (m, sorted(m))
        _KEYS[id(m)] = ent
    ks = ent[1]
    i = bisect_right(ks, d) - 1
    if i < 0:
        return None
    best = ks[i]
    if (d.year - best.year) * 12 + d.month - best.month > max_months:
        return None
    return m[best]


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = build_stance(ctx.settings)
    bad = [k for k in ("fred", "lw", "hlw", "oecd_gap") if str(rep.get(k, "")).startswith("failed")]
    update_state({"items": {"W3.4": "done"}, "last_runs": {"cb_stance": {
        "at": now_utc().isoformat(timespec="seconds"), "rows": rep.get("rows"), "failed": bad}}}, ctx.settings)
    return JobResult(status="failed" if "error" in rep else ("partial" if bad else "success"),
                     rows_written={"cb_stance": rep.get("written", 0)}, entities_ok=len(rep.get("latest", {})),
                     entities_failed=len(bad), message=json.dumps(rep, default=str)[:2000])
