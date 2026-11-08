"""Macro v2 indicator standardization layer (W1.1). Jobs `macro_cells` (this module) -> macro_cells, macro_cell_history,
macro_revisions (via app.macro.revisions).

Canonical cells
---------------
A *cell* is one (country, indicator) pair, e.g. (DE, cpi_yoy). Candidates are the macro_series rows whose catalog
`indicator` list contains the indicator for that country (contracts/macro_catalog.yaml; every input is an official
source: central banks, statistical offices, Fed banks / FRED, Treasury, OECD, BIS, IMF, Eurostat, World Bank).
The primary series is the best candidate by (freshness status fresh < stale < discontinued, catalog rank, latest
observation) - the same rule as the macro_audit coverage matrix.

Canonical measure (`transform`):
  * yoy   - growth-rate indicators (CPI, core CPI, PPI, GDP y/y, IP, retail, house prices, money, credit, employment,
            REER) published as an index or a level: 100 * (x_t / x_{t-12 months} - 1), date-based 12-month lag;
  * qoq   - gdp_qoq from an index/level series: 100 * (x_t / x_{t-3 months} - 1);
  * level - everything published as a rate/percent already (fractions are x100), and diffusion/confidence indices.
  Daily/weekly series are averaged per calendar month for the history; the cell value is the latest daily print.
  curve_2s10s is derived = yield_10y - yield_2y (both primary series, percentage points). Euro-area members inherit
  the EA policy_rate / rate_3m / money_supply cells (single monetary policy; title suffixed '[euro area]',
  n_candidates = 0).

Per-cell statistics (all on the canonical measure, window = 10 years ending at the latest period):
  z_10y = (value - mean) / std (ddof 1, >= 8 points); percentile_10y = share of window values <= value (0..1);
  change_3m / 6m / 12m = value - canonical value at (period - k months) (as-of, backward); direction = sign of the
  3-month change (falls back to 6m, then 12m), 'flat' when |change| < 0.1 * window std.
  first_release_value / revision: for series with vintages (ALFRED real-time rows, or first-seen revisions recorded
  by the ingest reconcile step) the canonical value of the latest period as first published, computed point-in-time
  (e.g. y/y = first print of t over the t-12 value known on t's first release date); revision = value - first.

Official sources only (user rule): series of EXCLUDED_SOURCES (AKShare = scraped Eastmoney/Jin10 pages, not an
official API) are never used as macro v2 inputs, even though the catalog keeps them for the legacy dashboard.

Point-in-time: `build_cells(as_of)` only uses observation rows with knowledge_date <= as_of (latest vintage per
period as known then). The nightly job writes today's snapshot; on an empty table it backfills month-end snapshots
for the last BACKFILL_MONTHS months. The primary-series choice is re-evaluated at each as_of.
"""

from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table

log = get_logger(__name__)

METHOD_VERSION = "cells_v1"
BACKFILL_MONTHS = 36
WINDOW_YEARS = 10

#: the 24 coverage countries (docs/MACRO_V2_PLAN.md W1.3)
COUNTRIES: list[str] = ["US", "EA", "DE", "FR", "IT", "ES", "PT", "NL", "GB", "CH", "JP", "KR", "CN", "HK", "TW", "IN",
                        "AU", "CA", "BR", "MX", "CL", "CO", "PE", "AR"]
#: aggregates whose series are kept in macro_cell_history for the theme dashboards (commodities, OAS, G7 CLI ...)
AGGREGATES: list[str] = ["WLD", "G7", "EM"]
#: non-official (scraped commercial) sources excluded from every macro v2 computation
EXCLUDED_SOURCES: frozenset[str] = frozenset({"akshare_cn", "akshare"})

#: indicator -> (label, group, canonical transform for index/level inputs, good direction for colouring: +1 higher is
#: better for growth, -1 lower is better, 0 neutral)
CELL_INDICATORS: dict[str, dict[str, Any]] = {
    "gdp_qoq": {"label": "GDP q/q", "group": "growth", "tf": "qoq", "good": 1},
    "gdp_yoy": {"label": "GDP y/y", "group": "growth", "tf": "yoy", "good": 1},
    "industrial_production": {"label": "Industrial production y/y", "group": "growth", "tf": "yoy", "good": 1},
    "retail_sales": {"label": "Retail sales y/y", "group": "growth", "tf": "yoy", "good": 1},
    "pmi_proxy": {"label": "Business survey (PMI proxy)", "group": "growth", "tf": "level", "good": 1},
    "consumer_confidence": {"label": "Consumer confidence", "group": "growth", "tf": "level", "good": 1},
    "cpi_yoy": {"label": "CPI y/y", "group": "inflation", "tf": "yoy", "good": -1},
    "core_cpi_yoy": {"label": "Core CPI y/y", "group": "inflation", "tf": "yoy", "good": -1},
    "ppi_yoy": {"label": "PPI y/y", "group": "inflation", "tf": "yoy", "good": -1},
    "unemployment": {"label": "Unemployment rate", "group": "labor", "tf": "level", "good": -1},
    "employment": {"label": "Employment", "group": "labor", "tf": "yoy", "good": 1},
    "policy_rate": {"label": "Policy rate", "group": "rates", "tf": "level", "good": 0},
    "rate_3m": {"label": "3M rate", "group": "rates", "tf": "level", "good": 0},
    "yield_2y": {"label": "2Y yield", "group": "rates", "tf": "level", "good": 0},
    "yield_10y": {"label": "10Y yield", "group": "rates", "tf": "level", "good": 0},
    "curve_2s10s": {"label": "2s10s slope", "group": "rates", "tf": "level", "good": 1, "derived": True},
    "private_credit": {"label": "Private credit", "group": "money_credit", "tf": "yoy", "good": 0},
    "money_supply": {"label": "Money supply", "group": "money_credit", "tf": "yoy", "good": 0},
    "house_prices": {"label": "House prices y/y", "group": "housing", "tf": "yoy", "good": 0},
    "current_account": {"label": "Current account % GDP", "group": "external", "tf": "level", "good": 1},
    "gov_deficit": {"label": "Fiscal balance % GDP", "group": "fiscal", "tf": "level", "good": 1},
    "gov_debt": {"label": "Government debt % GDP", "group": "fiscal", "tf": "level", "good": -1},
    "reer": {"label": "Real effective FX y/y", "group": "fx", "tf": "yoy", "good": 0},
}
YOY_LIKE = {k for k, v in CELL_INDICATORS.items() if v["tf"] == "yoy"}
#: euro-area members inherit these EA cells when they have no own series (n_candidates = 0, title '[euro area]')
EURO_MEMBERS = ("DE", "FR", "IT", "ES", "PT", "NL")
INHERIT_FROM_EA = ("policy_rate", "rate_3m", "money_supply")
STATUS_ORDER = {"fresh": 0, "stale": 1, "discontinued": 2, "empty": 3}
MONTHS_BACK = {"D": 1, "W": 1, "M": 1, "Q": 3, "A": 12}
TOL_DAYS = {"D": 40, "W": 40, "M": 40, "Q": 100, "A": 380}

OBS_COLS = ["series_id", "date", "value", "knowledge_date", "knowledge_date_estimated"]


# ------------------------------------------------------------------------------------------------ metadata


@dataclass
class SeriesInfo:
    series_id: str
    source_id: str
    title: str
    country: str
    indicators: list[str]
    category: str
    frequency: str
    units: str
    scale: str
    tf: str
    lag: int
    rank: int
    measure: str
    retired: bool


def load_meta(settings: Settings | None = None, countries: list[str] | None = None) -> dict[str, SeriesInfo]:
    """macro_series rows (target countries + aggregates) enriched with catalog rank / measure / retired flag."""
    from app.ingestion.macro_common import (  # noqa: PLC0415
        catalog_doc,
        derive_scale,
        frozen_label,
        frozen_series,
        load_catalog,
    )

    s = settings or get_settings()
    want = set(countries or COUNTRIES) | set(AGGREGATES)
    ms = read_table("macro_series", settings=s)
    cat = {g.series_id: g for g in load_catalog(s)}
    doc = catalog_doc(s)
    retired = set((doc.get("retired") or {}).keys())
    # catalog `frozen` (A9: source stopped, no replacement): still candidates with their stored history, even if an
    # earlier retirement left a '[discontinued' title suffix
    frozen = frozen_series(s)
    out: dict[str, SeriesInfo] = {}
    for m in ms.iter_rows(named=True):
        cc = (m.get("country") or "").upper()
        if cc not in want:
            continue
        sid = m["series_id"]
        if (m.get("source_id") or sid.split(":", 1)[0]) in EXCLUDED_SOURCES:
            continue
        g = cat.get(sid)
        inds = [x for x in (m.get("indicator") or (g.indicator if g else "") or "").split(",") if x]
        scale = m.get("scale") or (g.scale_eff if g else derive_scale(m.get("units"), m.get("category")))
        out[sid] = SeriesInfo(
            series_id=sid, source_id=m.get("source_id") or sid.split(":", 1)[0],
            title=(f"{g.title} {frozen_label(frozen[sid])}" if sid in frozen and g else m.get("title") or sid),
            country=cc, indicators=inds, category=m.get("category") or "other", frequency=m.get("frequency") or "M",
            units=m.get("units") or "", scale=scale or "level", tf=m.get("default_transform") or "level",
            lag=int(m.get("release_lag_days") or 0), rank=(g.rank if g else 5),
            measure=str((g.req.get("measure") if g else "") or ""), retired=sid in retired or (sid not in frozen
                                                                 and "[discontinued" in (m.get("title") or "")))
    return out


def load_obs(ids: list[str], settings: Settings | None = None) -> pl.DataFrame:
    """Every observation row (all vintages) of `ids`."""
    s = settings or get_settings()
    return (scan_table("macro_observations", s).filter(pl.col("series_id").is_in(ids)).select(OBS_COLS)
            .drop_nulls("date").collect())


def pit_latest(obs: pl.DataFrame, as_of: date | None = None) -> pl.DataFrame:
    """Latest vintage per (series_id, date) known at as_of (+ first-release value/date and number of vintages)."""
    o = obs if as_of is None else obs.filter(pl.col("knowledge_date") <= as_of)
    return (o.sort("knowledge_date").group_by("series_id", "date", maintain_order=False)
            .agg(pl.col("value").last(), pl.col("knowledge_date").last(),
                 pl.col("knowledge_date_estimated").last().alias("est"),
                 pl.col("value").first().alias("first_value"), pl.col("knowledge_date").first().alias("first_knowledge"),
                 pl.col("knowledge_date").n_unique().alias("n_vintages"))
            .sort("series_id", "date"))


# ------------------------------------------------------------------------------------------------ transforms


def canonical_how(indicator: str | None, scale: str, tf: str) -> str:
    """'yoy' | 'qoq' | 'level' | 'level100' (fraction -> percent) for a series measured as `indicator`."""
    if scale == "fraction":
        return "level100"
    if scale in ("index", "level"):
        if indicator == "gdp_qoq":
            return "qoq"
        if indicator in YOY_LIKE:
            return "yoy"
        if indicator is None and tf in ("yoy", "log_diff", "pct_change"):
            return "yoy"
    return "level"


def monthly(df: pl.DataFrame) -> pl.DataFrame:
    """Daily/weekly (date, value) -> calendar-month mean dated on the 1st."""
    return (df.drop_nulls("value").with_columns(pl.col("date").dt.truncate("1mo"))
            .group_by("date").agg(pl.col("value").mean()).sort("date"))


def to_canonical(df: pl.DataFrame, how: str, freq: str) -> pl.DataFrame:
    """(date, value) native -> (date, value) canonical at the native period (monthly for D/W)."""
    d = df.select("date", "value").drop_nulls("value").filter(pl.col("value").is_finite()).sort("date")
    if freq in ("D", "W"):
        d = monthly(d)
    if d.is_empty():
        return d
    if how == "level":
        return d
    if how == "level100":
        return d.with_columns(pl.col("value") * 100.0)
    k = "12mo" if how == "yoy" else "3mo"
    prev = d.with_columns(pl.col("date").dt.offset_by(k)).rename({"value": "prev"})
    return (d.join(prev, on="date", how="inner")
            .with_columns(pl.when(pl.col("prev") > 0).then((pl.col("value") / pl.col("prev") - 1.0) * 100.0)
                          .otherwise(None).alias("value"))
            .select("date", "value").drop_nulls("value"))


def first_release_canonical(obs_all: pl.DataFrame, how: str, freq: str, base_tol: float | None = 0.05) -> pl.DataFrame:
    """(date, first_value) of the canonical measure as first published (point-in-time; see module doc).
    Base guard (yoy/qoq): when the first print of t and the t-k value it is compared with sit on different index
    bases - their first/latest level ratios differ by more than base_tol (a rebase between the two vintages) - the
    pair is dropped instead of producing a spurious growth rate."""
    o = obs_all.drop_nulls("value").sort("knowledge_date")
    if o.is_empty() or freq in ("D", "W"):
        return pl.DataFrame(schema={"date": pl.Date, "first_value": pl.Float64})
    first = o.group_by("date").agg(pl.col("value").first().alias("v"), pl.col("knowledge_date").first().alias("k"))
    if how in ("level", "level100"):
        f = 100.0 if how == "level100" else 1.0
        return first.select("date", (pl.col("v") * f).alias("first_value")).sort("date")
    k = "12mo" if how == "yoy" else "3mo"
    left = first.with_columns(pl.col("date").dt.offset_by(f"-{k}").alias("pdate")).sort("k")
    right = o.select(pl.col("date").alias("pdate"), pl.col("knowledge_date").alias("k"), pl.col("value").alias("pv"))
    with warnings.catch_warnings():  # by-group sortedness cannot be verified by polars; both sides are sorted on k
        warnings.simplefilter("ignore")
        j = left.join_asof(right.sort("k"), on="k", by="pdate", strategy="backward")
    if base_tol is not None:
        lat = o.group_by("date").agg(pl.col("value").last().alias("lv"))
        j = (j.join(lat, on="date", how="left").join(lat.rename({"date": "pdate", "lv": "plv"}), on="pdate", how="left")
             .filter(((pl.col("v") / pl.col("lv")) / (pl.col("pv") / pl.col("plv")) - 1.0).abs() <= base_tol))
    return (j.with_columns(pl.when(pl.col("pv") > 0).then((pl.col("v") / pl.col("pv") - 1.0) * 100.0)
                           .otherwise(None).alias("first_value"))
            .select("date", "first_value").drop_nulls().sort("date"))


def units_label(info: SeriesInfo | None, how: str) -> str:
    if how == "yoy":
        return "% y/y"
    if how == "qoq":
        return "% q/q"
    if info is None:
        return "pp"
    if how == "level100":
        return "percent"
    return info.units or info.scale


# ------------------------------------------------------------------------------------------------ statistics


def _asof_value(hist: pl.DataFrame, target: date, tol_days: int) -> float | None:
    h = hist.filter((pl.col("date") <= target) & (pl.col("date") >= target - timedelta(days=tol_days)))
    return None if h.is_empty() else float(h.get_column("value")[-1])


def cell_stats(hist: pl.DataFrame, value: float | None, period: date | None, freq: str,
               window_years: int = WINDOW_YEARS) -> dict[str, Any]:
    """z-score / percentile vs the trailing window, k-month changes and direction (see module doc)."""
    out: dict[str, Any] = {"z_10y": None, "percentile_10y": None, "n_hist": 0, "change_3m": None, "change_6m": None,
                           "change_12m": None, "direction": None, "window_mean": None, "window_std": None}
    if value is None or period is None or hist.is_empty() or not math.isfinite(value):
        return out
    start = period - timedelta(days=int(365.25 * window_years))
    w = hist.filter((pl.col("date") > start) & (pl.col("date") <= period)).get_column("value")
    n = w.len()
    out["n_hist"] = int(n)
    min_n = 5 if freq == "A" else 8
    if n >= min_n:
        mean, sd = float(w.mean()), float(w.std(ddof=1) or 0.0)
        out["window_mean"], out["window_std"] = mean, sd
        if sd > 1e-12:
            out["z_10y"] = (value - mean) / sd
        out["percentile_10y"] = float((w <= value).sum()) / n
    tol = TOL_DAYS.get(freq, 40)
    for k in (3, 6, 12):
        if freq == "A" and k < 12:
            continue
        if freq == "Q" and k == 3:
            tgt = _month_shift(period, -3)
        else:
            tgt = _month_shift(period, -k)
        pv = _asof_value(hist, tgt, tol)
        if pv is not None:
            out[f"change_{k}m"] = value - pv
    ch = next((out[f"change_{k}m"] for k in (3, 6, 12) if out[f"change_{k}m"] is not None), None)
    if ch is not None:
        thr = 0.1 * (out["window_std"] or 0.0)
        out["direction"] = "flat" if abs(ch) <= thr else ("up" if ch > 0 else "down")
    return out


def _month_shift(d: date, k: int) -> date:
    m = d.month - 1 + k
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, 28))


def period_label(d: date | None, freq: str) -> str | None:
    if d is None:
        return None
    if freq == "Q":
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    if freq == "A":
        return str(d.year)
    if freq == "M":
        return f"{d.year}-{d.month:02d}"
    return d.isoformat()


# ------------------------------------------------------------------------------------------------ selection


def select_candidates(meta: dict[str, SeriesInfo], latest: pl.DataFrame, as_of: date) -> dict[tuple[str, str], list[str]]:
    """(country, indicator) -> candidate series ids ordered best first."""
    from app.ingestion.macro_audit import staleness  # noqa: PLC0415

    last = dict(latest.drop_nulls("value").group_by("series_id").agg(pl.col("date").max()).iter_rows())
    cand: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for sid, m in meta.items():
        if m.retired or m.country not in COUNTRIES:
            continue
        for ind in m.indicators:
            if ind not in CELL_INDICATORS:
                continue
            ld = last.get(sid)
            st = staleness(ld, m.frequency, m.lag, as_of)[0] if ld else "empty"
            if st == "empty":
                continue
            cand.setdefault((m.country, ind), []).append((STATUS_ORDER.get(st, 3), m.rank, -(ld.toordinal()), sid))
    return {k: [x[-1] for x in sorted(v)] for k, v in cand.items()}


# ------------------------------------------------------------------------------------------------ build


CELL_SCHEMA: dict[str, Any] = {
    "as_of_date": pl.Date, "country": pl.Utf8, "indicator": pl.Utf8, "label": pl.Utf8, "group": pl.Utf8,
    "series_id": pl.Utf8, "inputs": pl.List(pl.Utf8), "source_id": pl.Utf8, "title": pl.Utf8, "units": pl.Utf8,
    "transform": pl.Utf8, "frequency": pl.Utf8, "period": pl.Date, "period_label": pl.Utf8, "value": pl.Float64,
    "release_date": pl.Date, "release_date_estimated": pl.Boolean, "change_3m": pl.Float64, "change_6m": pl.Float64,
    "change_12m": pl.Float64, "direction": pl.Utf8, "z_10y": pl.Float64, "percentile_10y": pl.Float64,
    "n_hist": pl.Int32, "first_release_value": pl.Float64, "revision": pl.Float64, "has_vintages": pl.Boolean,
    "status": pl.Utf8, "age_days": pl.Int32, "rank": pl.Int16, "n_candidates": pl.Int16, "good_direction": pl.Int8,
    "method_version": pl.Utf8, "computed_at": pl.Datetime("us", "UTC"),
}
HIST_SCHEMA: dict[str, Any] = {
    "series_id": pl.Utf8, "measure": pl.Utf8, "country": pl.Utf8, "indicator": pl.Utf8, "date": pl.Date,
    "value": pl.Float64, "transform": pl.Utf8, "units": pl.Utf8, "frequency": pl.Utf8, "is_primary": pl.Boolean,
    "computed_at": pl.Datetime("us", "UTC"),
}


def _latest_native(g: pl.DataFrame) -> tuple[date | None, float | None, date | None, bool]:
    g2 = g.drop_nulls("value").filter(pl.col("value").is_finite())
    if g2.is_empty():
        return None, None, None, True
    r = g2.row(-1, named=True)
    return r["date"], float(r["value"]), r["knowledge_date"], bool(r["est"])


def _cell_row(as_of: date, cc: str, ind: str, sid: str, info: SeriesInfo, g: pl.DataFrame, obs_all: pl.DataFrame | None,
              n_cand: int, now: datetime) -> tuple[dict[str, Any], pl.DataFrame]:
    from app.ingestion.macro_audit import staleness  # noqa: PLC0415

    how = canonical_how(ind, info.scale, info.tf)
    hist = to_canonical(g, how, info.frequency)
    ld, lv, lk, lest = _latest_native(g)
    if info.frequency in ("D", "W") and how in ("level", "level100") and lv is not None:
        period, value = ld, lv * (100.0 if how == "level100" else 1.0)
        rel = lk
    elif hist.height:
        period, value = hist.get_column("date")[-1], float(hist.get_column("value")[-1])
        rr = g.filter(pl.col("date") == period)
        rel = rr.get_column("knowledge_date")[-1] if rr.height else lk
        lest = bool(rr.get_column("est")[-1]) if rr.height else lest
    else:
        period, value, rel = None, None, None
    st = cell_stats(hist, value, period, "M" if info.frequency in ("D", "W") else info.frequency)
    status, age, _ = staleness(ld, info.frequency, info.lag, as_of) if ld else ("empty", None, 0)
    has_v = bool(g.height and g.get_column("n_vintages").max() > 1)
    first = rev = None
    if has_v and obs_all is not None and period is not None:
        fr = first_release_canonical(obs_all, how, info.frequency)
        f = fr.filter(pl.col("date") == period)
        if f.height:
            first = float(f.get_column("first_value")[0])
            rev = value - first if value is not None else None
    cdef = CELL_INDICATORS[ind]
    row = {"as_of_date": as_of, "country": cc, "indicator": ind, "label": cdef["label"], "group": cdef["group"],
           "series_id": sid, "inputs": [sid], "source_id": info.source_id, "title": info.title,
           "units": units_label(info, how), "transform": how.replace("level100", "level"), "frequency": info.frequency,
           "period": period, "period_label": period_label(period, info.frequency), "value": value, "release_date": rel,
           "release_date_estimated": lest, "change_3m": st["change_3m"], "change_6m": st["change_6m"],
           "change_12m": st["change_12m"], "direction": st["direction"], "z_10y": st["z_10y"],
           "percentile_10y": st["percentile_10y"], "n_hist": st["n_hist"], "first_release_value": first,
           "revision": rev, "has_vintages": has_v, "status": status, "age_days": age, "rank": info.rank,
           "n_candidates": n_cand, "good_direction": cdef["good"], "method_version": METHOD_VERSION, "computed_at": now}
    return row, hist


def _spread_row(as_of: date, cc: str, a: dict[str, Any], b: dict[str, Any], ha: pl.DataFrame, hb: pl.DataFrame,
                now: datetime) -> tuple[dict[str, Any], pl.DataFrame] | None:
    """curve_2s10s = 10Y - 2Y (pp)."""
    if a.get("value") is None or b.get("value") is None:
        return None
    hist = (ha.join(hb.rename({"value": "v2"}), on="date", how="inner")
            .select("date", (pl.col("value") - pl.col("v2")).alias("value")).sort("date"))
    period = min(a["period"], b["period"])
    value = a["value"] - b["value"]
    st = cell_stats(hist, value, period, "M")
    worst = max((a["status"], b["status"]), key=lambda x: STATUS_ORDER.get(x, 3))
    row = {**a, "indicator": "curve_2s10s", "label": CELL_INDICATORS["curve_2s10s"]["label"],
           "series_id": a["series_id"], "inputs": [a["series_id"], b["series_id"]],
           "title": f"{a['title']} minus {b['title']}", "units": "pp", "transform": "level",
           "period": period, "period_label": period_label(period, "D" if a["frequency"] == "D" else a["frequency"]),
           "value": value, "release_date": max([x for x in (a["release_date"], b["release_date"]) if x], default=None),
           "change_3m": st["change_3m"], "change_6m": st["change_6m"], "change_12m": st["change_12m"],
           "direction": st["direction"], "z_10y": st["z_10y"], "percentile_10y": st["percentile_10y"],
           "n_hist": st["n_hist"], "first_release_value": None, "revision": None, "has_vintages": False,
           "status": worst, "n_candidates": 1, "good_direction": 1, "computed_at": now}
    return row, hist


def build_cells(as_of: date | None = None, settings: Settings | None = None, meta: dict[str, SeriesInfo] | None = None,
                obs: pl.DataFrame | None = None, countries: list[str] | None = None,
                with_history: bool = True) -> tuple[pl.DataFrame, pl.DataFrame]:
    """-> (macro_cells rows for as_of, macro_cell_history rows). History covers every series of the target countries
    and aggregates (cells' primaries flagged is_primary) - only when with_history."""
    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    now = datetime.now(UTC)
    meta = meta if meta is not None else load_meta(s, countries)
    if obs is None:
        obs = load_obs(list(meta), s)
    latest = pit_latest(obs, as_of)
    by_sid = {k[0]: g for k, g in latest.group_by("series_id")}
    obs_by = {k[0]: g for k, g in obs.filter(pl.col("knowledge_date") <= as_of).group_by("series_id")} \
        if obs.height else {}
    cands = select_candidates(meta, latest, as_of)
    rows: list[dict[str, Any]] = []
    hists: dict[tuple[str, str], pl.DataFrame] = {}
    primaries: set[tuple[str, str]] = set()
    for (cc, ind), ids in sorted(cands.items()):
        if countries and cc not in countries:
            continue
        sid = ids[0]
        g = by_sid.get(sid)
        if g is None:
            continue
        row, hist = _cell_row(as_of, cc, ind, sid, meta[sid], g, obs_by.get(sid), len(ids), now)
        rows.append(row)
        hists[(cc, ind)] = hist
        primaries.add((sid, ind))
    by_cell = {(r["country"], r["indicator"]): r for r in rows}
    for cc in countries or COUNTRIES:
        a, b = by_cell.get((cc, "yield_10y")), by_cell.get((cc, "yield_2y"))
        if a and b:
            sp = _spread_row(as_of, cc, a, b, hists[(cc, "yield_10y")], hists[(cc, "yield_2y")], now)
            if sp:
                rows.append(sp[0])
                hists[(cc, "curve_2s10s")] = sp[1]
    have = {(r["country"], r["indicator"]) for r in rows}
    for cc in EURO_MEMBERS:  # single monetary policy: members inherit the euro-area cell (catalog gaps say so)
        for ind in INHERIT_FROM_EA:
            ea = by_cell.get(("EA", ind))
            if ea and (cc, ind) not in have and (not countries or cc in countries):
                rows.append({**ea, "country": cc, "title": f"{ea['title']} [euro area]", "n_candidates": 0})
    cells = pl.DataFrame(rows, schema=CELL_SCHEMA) if rows else pl.DataFrame(schema=CELL_SCHEMA)
    if not with_history:
        return cells, pl.DataFrame(schema=HIST_SCHEMA)
    hrows: list[pl.DataFrame] = []
    for sid, info in meta.items():
        g = by_sid.get(sid)
        if g is None:
            continue
        measures = [i for i in info.indicators] or [None]
        for ind in measures:
            how = canonical_how(ind, info.scale, info.tf)
            h = to_canonical(g, how, info.frequency)
            if h.is_empty():
                continue
            hrows.append(h.with_columns(
                pl.lit(sid).alias("series_id"), pl.lit(ind or f"tf:{how.replace('level100', 'level')}").alias("measure"),
                pl.lit(info.country).alias("country"), pl.lit(ind).cast(pl.Utf8).alias("indicator"),
                pl.lit(how.replace("level100", "level")).alias("transform"), pl.lit(units_label(info, how)).alias("units"),
                pl.lit(info.frequency).alias("frequency"), pl.lit((sid, ind) in primaries).alias("is_primary"),
                pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at")))
    for (cc, ind), h in hists.items():
        if ind == "curve_2s10s" and h.height:
            r = by_cell[(cc, "yield_10y")]
            hrows.append(h.with_columns(
                pl.lit(f"derived:{cc}.curve_2s10s").alias("series_id"), pl.lit("curve_2s10s").alias("measure"),
                pl.lit(cc).alias("country"), pl.lit("curve_2s10s").alias("indicator"), pl.lit("level").alias("transform"),
                pl.lit("pp").alias("units"), pl.lit("M" if r["frequency"] in ("D", "W") else r["frequency"]).alias("frequency"),
                pl.lit(True).alias("is_primary"), pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at")))
    hist_df = (pl.concat([h.select(list(HIST_SCHEMA)) for h in hrows]).cast(HIST_SCHEMA)  # type: ignore[arg-type]
               if hrows else pl.DataFrame(schema=HIST_SCHEMA))
    return cells, hist_df


def month_ends(as_of: date, n: int) -> list[date]:
    out = []
    d = date(as_of.year, as_of.month, 1) - timedelta(days=1)
    for _ in range(n):
        out.append(d)
        d = date(d.year, d.month, 1) - timedelta(days=1)
    return sorted(out)


def run_cells(settings: Settings | None = None, as_of: date | None = None, backfill: bool | None = None,
              write: bool = True) -> dict[str, Any]:
    s = settings or get_settings()
    as_of = as_of or datetime.now(UTC).date()
    t0 = time.perf_counter()
    meta = load_meta(s)
    obs = load_obs(list(meta), s)
    rep: dict[str, Any] = {"series": len(meta), "obs_rows": obs.height, "as_of": as_of.isoformat()}
    cells, hist = build_cells(as_of, s, meta, obs)
    rep.update(cells=cells.height, history_rows=hist.height, seconds_today=round(time.perf_counter() - t0, 1))
    frames = [cells]
    if backfill is None:
        try:
            backfill = read_table("macro_cells", columns=["as_of_date"], settings=s).height == 0
        except Exception:  # noqa: BLE001 - table missing
            backfill = True
    if backfill:
        for d in month_ends(as_of, BACKFILL_MONTHS):
            c, _ = build_cells(d, s, meta, obs, with_history=False)
            frames.append(c)
        rep["backfilled_snapshots"] = BACKFILL_MONTHS
    all_cells = pl.concat(frames)
    from app.macro.revisions import build_revisions  # noqa: PLC0415

    revs = build_revisions(meta, obs)
    rep["revision_rows"] = revs.height
    if write:
        rep["rows_macro_cells"] = write_table(all_cells, "macro_cells", mode="upsert", settings=s)
        rep["rows_macro_cell_history"] = write_table(hist, "macro_cell_history", mode="overwrite", settings=s)
        rep["rows_macro_revisions"] = write_table(revs, "macro_revisions", mode="overwrite", settings=s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    rep["countries_with_cells"] = cells.get_column("country").n_unique() if cells.height else 0
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job macro_cells. as_of = params.as_of or the runner's data date (ctx.as_of = previous session for nightly, the
    same date the serving snapshot filters on). params: backfill (bool; default = table empty)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    as_of = date.fromisoformat(p["as_of"]) if p.get("as_of") else getattr(ctx, "as_of", None)
    rep = run_cells(ctx.settings, as_of, p.get("backfill"))
    log.info("macro_cells: %s", rep)
    return JobResult(status="success" if rep["cells"] else "failed",
                     rows_written={"macro_cells": rep.get("rows_macro_cells", 0),
                                   "macro_cell_history": rep.get("rows_macro_cell_history", 0),
                                   "macro_revisions": rep.get("rows_macro_revisions", 0)},
                     entities_ok=rep["cells"], watermark=rep["as_of"], message=str(rep)[:2000])
