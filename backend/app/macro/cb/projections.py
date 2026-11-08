"""W3.3 What the bank says: official projections (job cb_projections).

  Fed   Summary of Economic Projections (SEP) via FRED/ALFRED vintages (official St. Louis Fed copies of the Fed's SEP
        tables): median / central tendency / range for the federal funds rate (year-end midpoint), PCE inflation,
        core PCE, unemployment, real GDP growth; longer-run medians. One row per SEP release x variable x year x stat.
  ECB   Eurosystem/ECB staff macroeconomic projections (ECB Data Portal MPD, euro area U2): HICP, HICP excl. energy &
        food, real GDP, unemployment, and the technical assumptions for short-term rates (3M EURIBOR, market-based) and
        10y yields. Exercise codes W(inter, March)/G (spring, June)/S(ummer, September)/A(utumn, December) + year.
        ECB staff projections do not include a policy-rate path (conditioning assumptions are market-based).
  BoE   MPR projections are not published in a stable machine-readable form -> not covered (gap).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from .common import http_get, now_utc, read_table_safe, to_float, update_state, write

log = get_logger(__name__)

FED_SERIES = {
    # variable: {stat: series}
    "policy_rate": {"median": "FEDTARMD", "central_low": "FEDTARCTL", "central_high": "FEDTARCTH",
                    "range_low": "FEDTARRL", "range_high": "FEDTARRH"},
    "pce_inflation": {"median": "PCECTPIMD", "central_low": "PCECTPICTL", "central_high": "PCECTPICTH"},
    "core_pce_inflation": {"median": "JCXFEMD", "central_low": "JCXFECTL", "central_high": "JCXFECTH"},
    "unemployment": {"median": "UNRATEMD", "central_low": "UNRATECTL", "central_high": "UNRATECTH"},
    "gdp_growth": {"median": "GDPC1MD", "central_low": "GDPC1CTL", "central_high": "GDPC1CTH"},
}
FED_LR = {"policy_rate": "FEDTARMDLR", "pce_inflation": "PCECTPIMDLR", "unemployment": "UNRATEMDLR",
          "gdp_growth": "GDPC1MDLR"}
ECB_ITEMS = {"HIC": "hicp_inflation", "HEF": "core_hicp_inflation", "HEX": "hicp_ex_energy_inflation",
             "YER": "gdp_growth", "URX": "unemployment", "STN": "short_rate_assumption",
             "LTN": "long_rate_assumption"}
ECB_MPD_URL = "https://data-api.ecb.europa.eu/service/data/MPD/A.U2.{items}..."
EX_MONTH = {"W": 3, "G": 6, "S": 9, "A": 12}
EX_NAME = {"W": "March (winter) ECB staff", "G": "June (spring) Eurosystem staff",
           "S": "September (summer) ECB staff", "A": "December (autumn) Eurosystem staff"}

SCHEMA = {"bank": pl.Utf8, "release_date": pl.Date, "exercise": pl.Utf8, "variable": pl.Utf8, "horizon": pl.Utf8,
          "stat": pl.Utf8, "value": pl.Float64, "unit": pl.Utf8, "source": pl.Utf8}


# =============================================================================================== Fed (ALFRED)

def vintage_values(obs: list[dict[str, Any]], release: date) -> dict[str, float]:
    """{year: value} in force at ``release`` from ALFRED rows (date, value, realtime_start, realtime_end)."""
    out: dict[str, float] = {}
    for o in obs:
        rs, re_ = date.fromisoformat(o["realtime_start"]), date.fromisoformat(o["realtime_end"])
        v = to_float(o.get("value"))
        if rs <= release <= re_ and v is not None:
            out[o["date"][:4]] = v
    return out


def fed_rows(fetch: Any) -> list[dict[str, Any]]:
    """``fetch(series_id) -> ALFRED observation list`` (vintages)."""
    med = fetch("FEDTARMD")
    releases = sorted({date.fromisoformat(o["realtime_start"]) for o in med})
    rows: list[dict[str, Any]] = []
    cache: dict[str, list[dict[str, Any]]] = {"FEDTARMD": med}
    for var, stats in FED_SERIES.items():
        for stat, sid in stats.items():
            try:
                obs = cache.get(sid) or fetch(sid)
            except Exception as e:  # noqa: BLE001
                log.warning("cb_projections: %s failed: %s", sid, e)
                continue
            for r in releases:
                for yr, v in vintage_values(obs, r).items():
                    if int(yr) < r.year:  # past years drop out of later SEPs
                        continue
                    rows.append({"bank": "fed", "release_date": r, "exercise": f"SEP {r.isoformat()}",
                                 "variable": var, "horizon": yr, "stat": stat, "value": v, "unit": "percent",
                                 "source": f"fred:{sid}"})
    for var, sid in FED_LR.items():
        try:
            obs = fetch(sid)
        except Exception:  # noqa: BLE001
            continue
        for o in obs:  # longer-run series are dated at the release date
            v = to_float(o.get("value"))
            if v is None:
                continue
            r = date.fromisoformat(o["date"])
            rows.append({"bank": "fed", "release_date": r, "exercise": f"SEP {r.isoformat()}", "variable": var,
                         "horizon": "longer_run", "stat": "median", "value": v, "unit": "percent",
                         "source": f"fred:{sid}"})
    return rows


# =============================================================================================== ECB (MPD)

def parse_mpd(text: str, meetings: list[date] | None = None) -> list[dict[str, Any]]:
    rows = []
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        item, ex = r.get("PD_ITEM", ""), r.get("PD_SEAS_EX", "")
        v = to_float(r.get("OBS_VALUE"))
        if item not in ECB_ITEMS or v is None or len(ex) != 3 or ex[0] not in EX_MONTH:
            continue
        if r.get("SERIES_DENOM") not in ("A", "F", "I") or r.get("PD_ORIGIN", "0000") != "0000":
            continue  # annual growth / percentage / interest-rate level only
        yr = 2000 + int(ex[1:])
        hz = (r.get("TIME_PERIOD") or "")[:4]
        if not hz.isdigit() or int(hz) < yr - 1:  # exercises also carry history; keep (prior year, projections)
            continue
        rel = _release_date(yr, EX_MONTH[ex[0]], meetings or [])
        rows.append({"bank": "ecb", "release_date": rel, "exercise": f"{EX_NAME[ex[0]]} {yr}", "variable": ECB_ITEMS[item],
                     "horizon": r.get("TIME_PERIOD", "")[:4], "stat": "baseline", "value": v, "unit": "percent",
                     "source": f"ecb:MPD.{r.get('KEY', '').split('.', 1)[-1]}"})
    return rows


def _release_date(year: int, month: int, meetings: list[date]) -> date:
    """Projections are published with the Governing Council decision in the exercise month (first one found)."""
    cand = [m for m in meetings if m.year == year and m.month == month]
    return min(cand) if cand else date(year, month, 1)


def build_projections(s: Settings | None = None, write_out: bool = True) -> dict[str, Any]:
    from app.ingestion.fred import FredClient  # noqa: PLC0415

    s = s or get_settings()
    rep: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    try:
        fc = FredClient(s)
        fr = fed_rows(lambda sid: fc.observations(sid, vintages=True))
        rows += fr
        rep["fed_rows"] = len(fr)
    except Exception as e:  # noqa: BLE001
        rep["fed"] = f"failed: {e}"
    meet = read_table_safe("cb_meetings", s)
    em = sorted(meet.filter(pl.col("bank") == "ecb").get_column("meeting_date").to_list()) if meet is not None else []
    try:
        raw = http_get("ecb", ECB_MPD_URL.format(items="+".join(ECB_ITEMS)), {"startPeriod": "2015", "format": "csvdata"},
                       ttl=12 * 3600, settings=s)
        er = parse_mpd(raw.decode("utf-8", errors="replace"), em)
        rows += er
        rep["ecb_rows"] = len(er)
    except Exception as e:  # noqa: BLE001
        rep["ecb"] = f"failed: {e}"
    df = pl.DataFrame(rows, schema=SCHEMA).unique(["bank", "release_date", "variable", "horizon", "stat"], keep="last")
    df = df.with_columns(pl.lit(now_utc()).alias("computed_at"))
    rep["rows"] = df.height
    if write_out:
        rep["written"] = write(df, "cb_projections", s)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = build_projections(ctx.settings)
    bad = [k for k in ("fed", "ecb") if str(rep.get(k, "")).startswith("failed")]
    update_state({"items": {"W3.3": "done"}, "last_runs": {"cb_projections": {
        "at": now_utc().isoformat(timespec="seconds"), "rows": rep["rows"], "failed": bad}}}, ctx.settings)
    return JobResult(status="partial" if bad else "success", rows_written={"cb_projections": rep.get("written", 0)},
                     entities_ok=2 - len(bad), entities_failed=len(bad), message=json.dumps(rep, default=str)[:2000])
