"""Macro correctness audit. Job `macro_audit` -> data/logs/reports/macro_audit_<date>.json (+ coverage CSV).

Checks every macro_series (FRED + catalog sources):
  * frequency  - detected from the median spacing of the last observations vs the declared frequency;
  * scale      - declared scale (percent | fraction | index | level) vs the value range expected for its indicator
                 (e.g. a 'percent' CPI y/y series whose |values| are all < 0.3 is probably a fraction);
  * staleness  - days since the end of the last observed period vs release_lag_days + cadence + grace
                 (stale > 1x, discontinued > 3x the allowance + 180 days);
  * consistency- for each (region, indicator) with 2+ series, the latest common month is compared on a comparable
                 measure (percent levels, or y/y of index/level series whose default_transform is yoy) and
                 disagreements above the indicator tolerance are flagged;
  * PIT        - share of observed (vintage / first-seen) vs estimated knowledge dates per source;
  * coverage   - region x indicator matrix: best fresh series by catalog rank, else the gap reason.
`freshness_outcomes(con)` is the DQ hook (app.core.dq 'macro_freshness').
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table
from app.ingestion.macro_common import catalog_doc, derive_scale, load_catalog, period_end

log = get_logger(__name__)

CADENCE_DAYS = {"D": 7, "W": 14, "M": 31, "Q": 92, "A": 366}
GRACE_DAYS = {"D": 7, "W": 10, "M": 25, "Q": 45, "A": 120}
#: indicator -> (abs tolerance in the indicator's unit for cross-source checks, plausible (lo, hi) for percent values)
TOLERANCE: dict[str, float] = {
    "policy_rate": 0.30, "rate_3m": 0.35, "yield_2y": 0.30, "yield_10y": 0.30, "cpi_yoy": 0.35, "core_cpi_yoy": 0.35,
    "ppi_yoy": 0.8, "gdp_yoy": 0.6, "gdp_qoq": 0.4, "unemployment": 0.4, "industrial_production": 1.5,
    "retail_sales": 1.5, "money_supply": 1.0, "private_credit": 1.0, "house_prices": 1.5, "current_account": 0.8,
    "gov_debt": 3.0, "gov_deficit": 1.0, "employment": 1.0,
}
PLAUSIBLE: dict[str, tuple[float, float]] = {
    "policy_rate": (-1.5, 150), "rate_3m": (-1.5, 150), "yield_2y": (-2, 150), "yield_10y": (-2, 150),
    "cpi_yoy": (-30, 400), "core_cpi_yoy": (-30, 400), "ppi_yoy": (-60, 500), "unemployment": (0, 45),
    "gdp_yoy": (-40, 60), "gdp_qoq": (-40, 40), "gov_debt": (0, 400), "gov_deficit": (-60, 40),
    "current_account": (-40, 40),
}
PERCENT_INDICATORS = set(PLAUSIBLE)
#: region-level / global indicators excluded from per-country coverage counts (see catalog gaps '*')
GLOBAL_INDICATORS = {"commodities", "vix", "credit_spread", "lending_rate"}


def detect_frequency(dates: list[date]) -> str | None:
    if len(dates) < 4:
        return None
    ds = sorted(set(dates))[-40:]
    gaps = sorted((b - a).days for a, b in zip(ds, ds[1:], strict=False))
    g = gaps[len(gaps) // 2]
    if g <= 4:
        return "D"
    if g <= 10:
        return "W"
    if g <= 45:
        return "M"
    if g <= 135:
        return "Q"
    return "A"


def staleness(last: date | None, freq: str, lag: int, today: date) -> tuple[str, int | None, int]:
    allowance = int(lag) + CADENCE_DAYS.get(freq, 31) + GRACE_DAYS.get(freq, 25)
    if last is None:
        return "empty", None, allowance
    age = (today - period_end(last, freq)).days
    if age > 3 * allowance + 180:
        return "discontinued", age, allowance
    if age > allowance:
        return "stale", age, allowance
    return "fresh", age, allowance


def _latest_obs(settings: Settings) -> pl.DataFrame:
    """Latest-vintage value per (series_id, date)."""
    return (scan_table("macro_observations", settings)
            .select("series_id", "date", "value", "knowledge_date", "knowledge_date_estimated")
            .sort("knowledge_date").group_by("series_id", "date")
            .agg(pl.col("value").last(), pl.col("knowledge_date").last(),
                 pl.col("knowledge_date_estimated").last().alias("est"))
            .collect())


def _monthly(df: pl.DataFrame) -> pl.DataFrame:
    return (df.drop_nulls("value").with_columns(pl.col("date").dt.truncate("1mo").alias("m"))
            .group_by("m").agg(pl.col("value").mean()).sort("m"))


#: indicators whose comparable measure is a y/y rate (index/level series with a yoy transform qualify)
YOY_INDICATORS = {"cpi_yoy", "core_cpi_yoy", "ppi_yoy", "gdp_yoy", "industrial_production", "retail_sales", "house_prices",
                  "money_supply", "private_credit", "employment", "commodities"}


def comparable(obs: pl.DataFrame, meta: dict[str, Any]) -> pl.DataFrame | None:
    """Monthly (m, value) on a comparable measure: percent-level series as-is; yoy-transform series -> y/y %."""
    if obs.is_empty():
        return None
    scale = meta.get("scale") or derive_scale(meta.get("units"), meta.get("category"))
    tf = meta.get("default_transform") or "level"
    freq = meta.get("frequency") or "M"
    m = _monthly(obs)
    if scale in ("percent", "fraction") and tf in ("level", "diff"):
        return m.with_columns(pl.col("value") * (100.0 if scale == "fraction" else 1.0))
    if tf == "yoy" and scale in ("index", "level"):
        # date-based 12-month lag (a row shift would misalign around missing periods, e.g. US CPI Oct-2025)
        src = m if freq in ("D", "W") else obs.drop_nulls("value").select(pl.col("date").alias("m"), "value").sort("m")
        prev = src.with_columns(pl.col("m").dt.offset_by("12mo")).rename({"value": "prev"})
        y = (src.join(prev, on="m", how="inner").with_columns((pl.col("value") / pl.col("prev") * 100 - 100).alias("value"))
             .select("m", "value").drop_nulls())
        if freq == "Q":
            # expand quarterly to its 3 months so it can be compared with monthly series
            y = pl.concat([y.with_columns(pl.col("m").dt.offset_by(f"{k}mo")) for k in range(3)]).sort("m")
        return y.select("m", "value")
    return None


def run_audit(settings: Settings | None = None, write: bool = True, today: date | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    today = today or datetime.now(UTC).date()
    ms = read_table("macro_series", settings=s)
    obs = _latest_obs(s)
    doc = catalog_doc(s)
    cat_specs = {g.series_id: g for g in load_catalog(s)}
    from app.ingestion.fred import yaml_meta  # noqa: PLC0415

    fred_meta = {f"fred:{k}": v for k, v in yaml_meta(s).items()}
    from app.ingestion.fred import CATALOG_BY_ID as fred_catalog  # noqa: PLC0415, N812
    meta_by_id: dict[str, dict[str, Any]] = {r["series_id"]: r for r in ms.iter_rows(named=True)}
    by_series = {k[0]: g for k, g in obs.group_by("series_id")}

    retired: dict[str, str] = doc.get("retired") or {}
    # catalog `frozen` (A9: source stopped, no replacement): served with the normal staleness status, not flagged
    frozen: dict[str, Any] = doc.get("frozen") or {}
    series_rep: list[dict[str, Any]] = []
    stale, disc, freq_mismatch, scale_flags = [], [], [], []
    for sid, m in meta_by_id.items():
        o = by_series.get(sid, pl.DataFrame(schema=obs.schema)).drop_nulls("value").sort("date")
        dates = o.get_column("date").to_list()
        last = dates[-1] if dates else None
        st, age, allow = staleness(last, m.get("frequency") or "M", m.get("release_lag_days") or 0, today)
        det = detect_frequency(dates)
        spec = cat_specs.get(sid) or fred_meta.get(sid)
        ind = (m.get("indicator") or (spec.indicator if spec else None) or "")
        scale = m.get("scale") or derive_scale(m.get("units"), m.get("category"))
        vals = o.tail(36).get_column("value").to_list()
        flags: list[str] = []
        if det and det != (m.get("frequency") or "M") and not (det == "D" and m.get("frequency") == "W"):
            flags.append(f"frequency declared {m.get('frequency')} detected {det}")
            freq_mismatch.append(sid)
        prim = ind.split(",")[0] if ind else ""
        if vals and prim in PERCENT_INDICATORS and scale in ("percent", "fraction"):
            mx = max(abs(v) for v in vals)
            lo, hi = PLAUSIBLE[prim]
            if scale == "percent" and mx < 0.3 and prim not in ("policy_rate", "rate_3m", "yield_2y", "yield_10y",
                                                                 "gdp_qoq"):
                flags.append(f"scale percent but max|v|={mx:.3g} (fraction?)")
            vmin, vmax = min(vals), max(vals)
            if (vmin * (100 if scale == "fraction" else 1)) < lo or (vmax * (100 if scale == "fraction" else 1)) > hi:
                flags.append(f"values outside plausible range [{lo},{hi}]: {vmin:.4g}..{vmax:.4g}")
        if flags:
            scale_flags += [sid] if any("scale" in f or "plausible" in f for f in flags) else []
        if sid in retired:
            st = "retired"
        elif sid in frozen:   # not counted as stale / discontinued: stopped on purpose, history still served
            fz = frozen[sid] if isinstance(frozen[sid], dict) else {"reason": frozen[sid]}
            flags.append(f"frozen since {fz.get('since', '?')}: {fz.get('reason', 'source stopped')}")
        elif st == "stale":
            stale.append(sid)
        elif st == "discontinued":
            disc.append(sid)
        est_share = (o.get_column("est").mean() if o.height else None)
        series_rep.append({
            "series_id": sid, "source_id": m.get("source_id"), "country": m.get("country"),
            "indicator": ind or None, "category": m.get("category"), "frequency": m.get("frequency"),
            "detected_frequency": det, "scale": scale, "units": m.get("units"), "sa": m.get("seasonal_adjustment"),
            "n_obs": len(dates), "first": dates[0].isoformat() if dates else None,
            "last": last.isoformat() if last else None, "last_value": vals[-1] if vals else None,
            "status": st, "age_days": age, "allowance_days": allow, "estimated_knowledge_share": est_share,
            "flags": flags, "in_catalog": (sid in cat_specs or sid in fred_meta or sid in retired or sid in frozen
                                           or sid.removeprefix("fred:") in fred_catalog),
            "replaced_by": retired.get(sid), "frozen": frozen.get(sid),
            "measure": (spec.req.get("measure") if spec else None) or "",
            "rank": spec.rank if spec else None,
        })

    # ---- coverage matrix
    regions = [k for k, v in (doc.get("regions") or {}).items() if (v or {}).get("target", True)]
    indicators = list((doc.get("indicators") or {}).keys())
    status_by = {r["series_id"]: r for r in series_rep}
    cells: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    for sid, r in status_by.items():
        spec = cat_specs.get(sid) or fred_meta.get(sid)
        inds = (r["indicator"] or "").split(",") if r["indicator"] else []
        rank = spec.rank if spec else 5
        for ind in inds:
            cells[(r["country"] or "", ind)].append((rank, sid))
    gaps = doc.get("gaps") or {}
    matrix: dict[str, dict[str, Any]] = {}
    for reg in regions:
        row: dict[str, Any] = {}
        for ind in indicators:
            cands = sorted(cells.get((reg, ind), []))
            fresh = [sid for _, sid in cands if status_by[sid]["status"] == "fresh"]
            if fresh:
                row[ind] = {"ok": True, "series_id": fresh[0], "source": status_by[fresh[0]]["source_id"],
                            "fallbacks": [x for _, x in cands if x != fresh[0]],
                            "last": status_by[fresh[0]]["last"]}
            elif cands:
                best = cands[0][1]
                row[ind] = {"ok": False, "reason": f"{status_by[best]['status']} (last {status_by[best]['last']})",
                            "series_id": best}
            else:
                g = (gaps.get(reg) or {}).get(ind) or (gaps.get("*") or {}).get(ind)
                row[ind] = {"ok": False, "reason": g or "no source in catalog"}
        matrix[reg] = row

    # ---- cross-source consistency (latest common month), like-for-like measures only
    by_measure: dict[tuple[str, str, str], list[tuple[int, str]]] = defaultdict(list)
    for (reg, ind), cands in cells.items():
        for rank, sid in cands:
            m = status_by[sid]["measure"]
            by_measure[("WLD" if ind == "commodities" else reg, ind, m)].append((rank, sid))
    disagreements, comparisons = [], 0
    for (reg, ind, _m), cands in by_measure.items():
        if len(cands) < 2 or (ind not in TOLERANCE and ind != "commodities"):
            continue
        comp: dict[str, pl.DataFrame] = {}
        for _, sid in cands:
            if status_by[sid]["status"] in ("discontinued", "empty"):
                continue
            if ind not in YOY_INDICATORS and (meta_by_id[sid].get("scale") or "") in ("index", "level"):
                continue   # e.g. a GDP level series cannot be compared with a q/q rate
            c = comparable(by_series.get(sid, pl.DataFrame(schema=obs.schema)), meta_by_id[sid])
            if c is not None and c.height:
                comp[sid] = c.filter(pl.col("m") >= pl.lit(today - timedelta(days=800)))
        ids = sorted(comp)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                j = comp[a].join(comp[b], on="m", suffix="_b").drop_nulls()
                if j.is_empty():
                    continue
                comparisons += 1
                last = j.sort("m").tail(1).row(0, named=True)
                diff = last["value"] - last["value_b"]
                med = float((j.tail(12).get_column("value") - j.tail(12).get_column("value_b")).abs().median() or 0)
                tol = TOLERANCE.get(ind, 5.0)   # commodities: y/y % points
                if abs(diff) > tol or med > tol:
                    disagreements.append({"region": reg, "indicator": ind, "a": a, "b": b,
                                          "month": last["m"].isoformat(), "a_value": round(last["value"], 4),
                                          "b_value": round(last["value_b"], 4), "diff": round(diff, 4),
                                          "median_abs_diff_12m": round(med, 4), "tolerance": tol})

    # ---- PIT summary per source
    pit = (obs.join(ms.select("series_id", "source_id"), on="series_id", how="left")
           .group_by("source_id").agg(pl.len().alias("rows"), (~pl.col("est")).sum().alias("observed_rows"))
           .sort("source_id"))
    rep = {
        "generated_at": datetime.now(UTC).isoformat(), "as_of": today.isoformat(),
        "n_series": ms.height, "n_observations": int(scan_table("macro_observations", s).select(pl.len()).collect().item()),
        "by_source": dict(ms.group_by("source_id").len().sort("source_id").iter_rows()),
        "by_country": dict(ms.group_by("country").len().sort("country").iter_rows()),
        "stale": stale, "discontinued": disc, "retired": retired, "frozen": frozen,
        "frequency_mismatch": freq_mismatch, "scale_flags": scale_flags,
        "not_in_catalog": [r["series_id"] for r in series_rep if not r["in_catalog"]],
        "missing_key_gaps": sorted({f"{reg}:{ind}: {c['reason']}" for reg, row in matrix.items() for ind, c in row.items()
                                    if not c["ok"] and "key:" in str(c.get("reason", ""))}),
        "comparisons": comparisons, "disagreements": disagreements,
        "pit": [{"source_id": r[0], "rows": int(r[1]), "observed_share": round(r[2] / r[1], 4) if r[1] else None}
                for r in pit.iter_rows()],
        "coverage": {"country_cells": sum(1 for v in matrix.values() for i in v if i not in GLOBAL_INDICATORS),
                     "country_ok": sum(1 for v in matrix.values() for i, c in v.items() if c["ok"] and i not in GLOBAL_INDICATORS),
                     "cells": sum(len(v) for v in matrix.values()),
                     "ok": sum(1 for v in matrix.values() for c in v.values() if c["ok"]),
                     "matrix": matrix},
        "series": series_rep,
    }
    if write:
        d = s.logs_dir / "reports"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"macro_audit_{today.isoformat()}.json"
        p.write_bytes(orjson.dumps(rep, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS, default=str))
        with (d / f"macro_coverage_{today.isoformat()}.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["region", *indicators])
            for reg, row in matrix.items():
                w.writerow([reg] + [(f"OK {c['series_id']}" if c["ok"] else f"MISSING {c['reason']}")
                                    for c in (row[i] for i in indicators)])
        rep["report_path"] = str(p)
    return rep


# ------------------------------------------------------------------------------------------------ DQ hook


def freshness_outcomes(ctx: Any, max_stale_share: float = 0.10) -> list[Any]:
    """DQ hook 'macro_freshness': share of core / rank-1 macro series that are stale vs their expected cadence."""
    from app.core.dq import DqOutcome  # noqa: PLC0415

    if not ctx.has_data("macro_series") or not ctx.has_data("macro_observations"):
        return []
    rows = ctx.con.execute("""
        SELECT s.series_id, s.frequency, s.release_lag_days, s.is_core, max(o.date) AS last
        FROM macro_series s LEFT JOIN macro_observations o ON o.series_id = s.series_id AND o.value IS NOT NULL
        GROUP BY 1, 2, 3, 4""").fetchall()
    today = ctx.now.date()
    try:
        ranks = {g.series_id: g.rank for g in load_catalog(ctx.settings)}
    except Exception:  # noqa: BLE001
        ranks = {}
    try:  # catalog `retired` / `frozen` series (e.g. A9 2026-11-07 source_disallowed BoE IADB series) stop on purpose
        doc = catalog_doc(ctx.settings)
        retired = set(doc.get("retired") or {}) | set(doc.get("frozen") or {})
    except Exception:  # noqa: BLE001
        retired = set()
    stale, checked = [], 0
    for sid, freq, lag, core, last in rows:
        if not (core or ranks.get(sid) == 1) or sid in retired:
            continue
        checked += 1
        st, age, allow = staleness(last, freq or "M", int(lag or 0), today)
        if st != "fresh":
            stale.append({"series_id": sid, "status": st, "age_days": age, "allowance_days": allow})
    share = len(stale) / checked if checked else 0.0
    return [DqOutcome("macro_series", "macro_freshness", "warn", share <= max_stale_share,
                      round(share, 4) if math.isfinite(share) else None, max_stale_share,
                      {"checked": checked, "stale": stale[:60]})]


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_audit(ctx.settings)
    cov = rep["coverage"]
    return JobResult(status="success", rows_written={}, entities_ok=rep["n_series"],
                     entities_failed=len(rep["stale"]) + len(rep["discontinued"]),
                     watermark=datetime.now(UTC).date().isoformat(),
                     message=(f"series={rep['n_series']} obs={rep['n_observations']} coverage={cov['country_ok']}/{cov['country_cells']} "
                              f"stale={len(rep['stale'])} discontinued={len(rep['discontinued'])} "
                              f"disagreements={len(rep['disagreements'])} report={rep.get('report_path')}"))
