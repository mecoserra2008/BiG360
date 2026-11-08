"""Serving-layer data-integrity helpers (O2.2a; audit F06 / F07 / F08 / F09 / F10 / F15). Owner: api.

Pure read helpers over the serving snapshot used by the market / macro / company routers. They only ADD fields
(contracts/api.yaml 1.28.0, docs/CONTRACT_CHANGES.md):

* `region_data_as_of` / `region_asof_map`: explicit data-as-of per region (modal last-bar date of the region's
  equities) so cross-region views can say which session each column is (F06).
* `credit_spreads`: curated credit-spread panel (US IG / BBB / HY / CCC OAS, euro HY OAS, EM OAS from ICE BofA via FRED;
  Moody's Baa - 10Y labelled as a yield spread, not an OAS) with a calendar 1-month change, an EMPIRICAL percentile over
  the available window (FRED carries ~3 years of ICE data, so the window length is returned) and a fixed 1-year
  dated spark (F09).
* `display_fields`: rate-of-change transforms for macro tiles (CPI / core y/y, real GDP q/q SAAR, payrolls m/m change,
  industrial production y/y; rates stay levels), with the observation date and the spark window (F10).
* `regime_scope_info` / `regime_inputs`: which regime model actually answers a scope (own model, labelled GLOBAL
  fallback, or not modelled) and driver values with their true units plus model z-scores (F07 / F08).
* `beneish_detail`: Beneish components and the recalibrated model-based screen for one issuer (F15).
"""

from __future__ import annotations

import bisect
import math
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from app.api.routers._common import equity_only_sql, fnum, iso, rnd

# ------------------------------------------------------------------------------------------------ F06 data as-of


def region_data_as_of(db: Any, regions: list[str] | None = None, lookback_days: int = 21) -> list[dict[str, Any]]:
    """Per region: as_of (modal last-bar date of equities with a bar in the last `lookback_days`), latest (most
    recent date carried by >= 1% of them), counts and the share of names on as_of."""
    params: list[Any] = [db.as_of - timedelta(days=lookback_days)]
    rc = "TRUE"
    if regions:
        rc = f"s.region IN ({', '.join('?' for _ in regions)})"
        params += list(regions)
    rows = db.all(f"""SELECT s.region, s.px_date, count(*)::INT AS n FROM sec s
                      WHERE s.px_date >= ? AND {equity_only_sql('s')} AND {rc} AND s.region IS NOT NULL
                      GROUP BY 1, 2""", params)
    by: dict[str, list[tuple[date, int]]] = {}
    for r in rows:
        by.setdefault(r["region"], []).append((r["px_date"], int(r["n"])))
    out = []
    for reg in sorted(by):
        pts = by[reg]
        tot = sum(n for _, n in pts)
        mode_d, mode_n = max(pts, key=lambda x: (x[1], x[0]))
        latest = max((d for d, n in pts if n >= max(1, 0.01 * tot)), default=mode_d)
        n_latest = sum(n for d, n in pts if d == latest)
        out.append({"region": reg, "as_of": iso(mode_d), "latest": iso(latest), "n_securities": tot,
                    "n_at_as_of": mode_n, "share_at_as_of": round(mode_n / tot, 4) if tot else None,
                    "n_at_latest": n_latest, "stale_share": round(sum(n for d, n in pts if d < mode_d) / tot, 4) if tot else None})
    return out


def region_asof_map(items: list[dict[str, Any]]) -> dict[str, date]:
    return {x["region"]: date.fromisoformat(x["as_of"]) for x in items if x.get("as_of")}


def asof_case_sql(m: dict[str, date], col_region: str = "region") -> tuple[str, list[Any]]:
    """SQL expression -> the region's data_as_of date (NULL when unknown) and its parameters."""
    if not m:
        return "CAST(NULL AS DATE)", []
    whens = " ".join("WHEN ? THEN CAST(? AS DATE)" for _ in m)
    params: list[Any] = []
    for k, v in m.items():
        params += [k, v]
    return f"CASE {col_region} {whens} END", params


def alignment_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    ds = sorted({x["as_of"] for x in items if x.get("as_of")})
    return {"dates_aligned": len(ds) <= 1, "common_date": ds[0] if ds else None, "latest_date": ds[-1] if ds else None}


# ------------------------------------------------------------------------------------------------ F09 credit


#: (series_id, label, region, spread_type, segment)
CREDIT_SPREADS: list[tuple[str, str, str, str, str]] = [
    ("fred:BAMLC0A0CM", "US investment grade OAS (ICE BofA)", "US", "oas", "ig"),
    ("fred:BAMLC0A4CBBB", "US BBB OAS (ICE BofA)", "US", "oas", "bbb"),
    ("fred:BAMLH0A0HYM2", "US high yield OAS (ICE BofA)", "US", "oas", "hy"),
    ("fred:BAMLH0A3HYC", "US CCC & lower OAS (ICE BofA)", "US", "oas", "ccc"),
    ("fred:BAMLHE00EHYIOAS", "Euro high yield OAS (ICE BofA)", "EU", "oas", "hy"),
    ("fred:BAMLEMCBPIOAS", "EM corporate OAS (ICE BofA)", "EM", "oas", "em"),
    ("fred:BAMLEMHBHYCRPIOAS", "EM high yield corporate OAS (ICE BofA)", "EM", "oas", "em_hy"),
    ("fred:BAMLEMIBHGCRPIOAS", "EM high grade corporate OAS (ICE BofA)", "EM", "oas", "em_ig"),
    ("fred:BAA10Y", "Moody's Baa yield minus 10Y Treasury (yield spread, not OAS)", "US", "yield_spread", "baa"),
]


def _value_at(dates: list[date], vals: list[float], d: date) -> tuple[date, float] | tuple[None, None]:
    i = bisect.bisect_right(dates, d) - 1
    return (dates[i], vals[i]) if i >= 0 else (None, None)


def _empirical_pct(window: list[float], x: float) -> float | None:
    if len(window) < 20:
        return None
    s = sorted(window)
    lo = bisect.bisect_left(s, x)
    hi = bisect.bisect_right(s, x)
    return round((lo + hi) / 2 / len(s), 4)


def series_stats(dates: list[date], vals: list[float], pct_years: int = 10, spark_days: int = 365) -> dict[str, Any]:
    """Level, 1-observation / calendar 1M / 1Y changes, empirical percentile over the last `pct_years` (or the
    available window, returned in years) and a dated spark over a fixed calendar window."""
    if not dates:
        return {}
    ld, lv = dates[-1], vals[-1]
    d1m, v1m = _value_at(dates, vals, ld - timedelta(days=30))
    d1y, v1y = _value_at(dates, vals, ld - timedelta(days=365))
    start = ld - timedelta(days=int(pct_years * 365.25))
    i0 = bisect.bisect_left(dates, start)
    win = vals[i0:]
    span = (ld - dates[i0]).days / 365.25 if i0 < len(dates) else 0.0
    s0 = bisect.bisect_left(dates, ld - timedelta(days=spark_days))
    return {"as_of": iso(ld), "last": lv, "change_1d": rnd(lv - vals[-2]) if len(vals) > 1 else None,
            "change_1m": None if v1m is None else rnd(lv - v1m), "change_1m_base_date": iso(d1m),
            "change_1y": None if v1y is None else rnd(lv - v1y),
            "percentile": _empirical_pct(win, lv), "percentile_window_years": round(span, 2),
            "percentile_method": "empirical",
            "spark_window_days": spark_days,
            "spark_points": [{"date": iso(d), "value": v} for d, v in zip(dates[s0:], vals[s0:], strict=False)]}


def credit_spreads(db: Any, regions: list[str] | None = None) -> list[dict[str, Any]]:
    want = [c for c in CREDIT_SPREADS if not regions or c[2] in regions or c[2] == "EM" or "US" in (regions or [])]
    ids = [c[0] for c in want]
    if not ids:
        return []
    ph = ", ".join("?" for _ in ids)
    rows = db.all(f"""SELECT series_id, date, value FROM macro_obs WHERE series_id IN ({ph}) AND value IS NOT NULL
                      AND isfinite(value) AND date <= ? AND date >= ? ORDER BY series_id, date""",
                  ids + [db.as_of, db.as_of - timedelta(days=int(10 * 365.25) + 40)])
    ser: dict[str, tuple[list[date], list[float]]] = {}
    for r in rows:
        d, v = ser.setdefault(r["series_id"], ([], []))
        d.append(r["date"])
        v.append(float(r["value"]))
    out = []
    for sid, label, region, kind, seg in want:
        if sid not in ser:
            continue
        st = series_stats(*ser[sid])
        out.append({"series_id": sid, "label": label, "region": region, "spread_type": kind, "segment": seg,
                    "unit": "percent", **st})
    return out


# ------------------------------------------------------------------------------------------------ F10 macro tiles


def display_transform(meta: dict[str, Any]) -> str:
    """level | yoy | qoq_saar | mom_diff for a macro series (rates and already-y/y series stay levels)."""
    scale = (meta.get("scale") or "").lower()
    units = (meta.get("units") or "").lower()
    cat = (meta.get("category") or "").lower()
    freq = (meta.get("frequency") or "M").upper()
    dt = (meta.get("default_transform") or "level").lower()
    if scale in ("percent", "fraction") or "percent" in units or units.startswith("%"):
        return "level"
    if cat == "labor" and freq in ("M", "W") and ("thousands of persons" in units or "persons" in units):
        return "mom_diff"
    if cat == "growth" and freq == "Q" and scale != "index":
        return "qoq_saar"
    if cat in ("inflation", "growth", "housing", "trade") and freq in ("M", "Q"):
        return "yoy"
    if dt == "yoy" and freq in ("M", "Q", "W"):
        return "yoy"
    return "level"


_DISPLAY = {"level": ("level", None), "yoy": ("fraction", "y/y"), "qoq_saar": ("fraction", "q/q SAAR"),
            "mom_diff": ("level", "m/m change")}


def _apply(dates: list[date], vals: list[float], how: str) -> tuple[list[date], list[float]]:
    if how == "level":
        return dates, vals
    od: list[date] = []
    ov: list[float] = []
    if how == "yoy":
        for d, v in zip(dates, vals, strict=False):
            b_d, b_v = _value_at(dates, vals, d - timedelta(days=362))
            if b_d is not None and (d - b_d).days <= 400 and (d - b_d).days >= 330 and b_v:
                od.append(d)
                ov.append(v / b_v - 1)
        return od, ov
    for i in range(1, len(vals)):
        if how == "qoq_saar" and vals[i - 1] > 0 and vals[i] > 0:
            od.append(dates[i])
            ov.append((vals[i] / vals[i - 1]) ** 4 - 1)
        elif how == "mom_diff":
            od.append(dates[i])
            ov.append(vals[i] - vals[i - 1])
    return od, ov


def display_fields(db: Any, metas: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{series_id: additive display fields} for macro tiles."""
    if not metas:
        return {}
    ids = [m["series_id"] for m in metas]
    ph = ", ".join("?" for _ in ids)
    rows = db.all(f"""SELECT series_id, date, value FROM macro_obs WHERE series_id IN ({ph}) AND value IS NOT NULL
                      AND isfinite(value) AND date <= ? AND date >= ? ORDER BY series_id, date""",
                  ids + [db.as_of, db.as_of - timedelta(days=int(11 * 365.25))])
    ser: dict[str, tuple[list[date], list[float]]] = {}
    for r in rows:
        d, v = ser.setdefault(r["series_id"], ([], []))
        d.append(r["date"])
        v.append(float(r["value"]))
    out: dict[str, dict[str, Any]] = {}
    for m in metas:
        sid = m["series_id"]
        how = display_transform(m)
        d, v = ser.get(sid, ([], []))
        if how in ("yoy", "qoq_saar") and v and min(v) <= 0:
            how = "level"   # zero-centred / signed indices (CFNAI, diffusion indexes): a growth rate is meaningless
        unit, label = _DISPLAY[how]
        if how == "level":
            unit = "percent" if (m.get("scale") or "") == "percent" else ("fraction" if m.get("scale") == "fraction" else "level")
        td, tv = _apply(d, v, how)
        freq = (m.get("frequency") or "M").upper()
        spark_days = 365 if freq in ("D", "W") else (int(5 * 365.25) if freq == "M" else int(10 * 365.25))
        st = series_stats(td, tv, pct_years=10, spark_days=spark_days) if td else {}
        out[sid] = {"display_transform": how, "display_label": label, "display_unit": unit,
                    "display_value": rnd(st.get("last"), 8) if st else None,
                    "display_change_1p": rnd(tv[-1] - tv[-2], 8) if len(tv) > 1 else None,
                    "observation_date": st.get("as_of") if st else iso(d[-1] if d else None),
                    "change_1m": st.get("change_1m") if st else None,
                    "percentile_10y": st.get("percentile") if st else None,
                    "percentile_window_years": st.get("percentile_window_years") if st else None,
                    "spark_window_days": spark_days,
                    "display_spark_points": st.get("spark_points", []) if st else []}
    return out


# ------------------------------------------------------------------------------------------------ F07 / F08 regime


def regime_scope_info(db: Any, requested: str) -> dict[str, Any]:
    """Which regime model answers `requested`: own model, a labelled GLOBAL fallback, or none."""
    scopes = {r["region_scope"]: r["model_id"] for r in db.all(
        "SELECT region_scope, arg_max(model_id, date) AS model_id FROM macro_regimes GROUP BY 1")}
    if requested in scopes:
        return {"requested_scope": requested, "scope": requested, "model_id": scopes[requested], "modelled": True,
                "fallback": False, "available_scopes": sorted(scopes)}
    if "GLOBAL" in scopes:
        return {"requested_scope": requested, "scope": "GLOBAL", "model_id": scopes["GLOBAL"], "modelled": False,
                "fallback": True, "available_scopes": sorted(scopes)}
    return {"requested_scope": requested, "scope": None, "model_id": None, "modelled": False, "fallback": False,
            "available_scopes": sorted(scopes)}


@lru_cache(maxsize=8)
def _hmm_standardization(scope: str) -> tuple[tuple[str, ...], tuple[float, ...], tuple[float, ...]] | None:
    """(features, mu, sd) persisted by app.ml.macro_regime.fit_regime (models_store/macro_hmm_<scope>/latest.joblib)."""
    try:
        from app.core.config import get_settings  # noqa: PLC0415
        from app.ml.macro_regime import load_fit  # noqa: PLC0415

        fit = load_fit(scope, get_settings())
        if fit is None:
            return None
        return tuple(fit.feature_names), tuple(float(x) for x in fit.mu), tuple(float(x) for x in fit.sd)
    except Exception:  # noqa: BLE001 - model artefact missing / unreadable -> history z-scores
        return None


def regime_input_units(scope: str) -> dict[str, dict[str, Any]]:
    """feature -> {series_id, transform, unit} from app.ml.macro_regime (the model's own input definitions)."""
    try:
        from app.ml import macro_regime as MR  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {}
    unit_of = {"yoy": "fraction", "log_diff": "fraction", "level": "level", "diff": "level"}
    explicit = (getattr(MR, "REGION_UNITS", {}) or {}).get(scope) or {}
    out = {}
    for f, (sid, tr) in (MR.REGION_SERIES.get(scope) or {}).items():
        unit = unit_of.get(tr, "level")
        if tr == "level" and f in ("curve", "credit"):
            unit = "percent"
        if f in ("vol", "stress"):
            unit = "index"
        unit = explicit.get(f, unit)  # regional scopes (F27) declare their true units
        out[f] = {"series_id": sid, "transform": tr, "unit": unit}
    return out


def regime_inputs(scope: str, inputs: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Driver series with their TRUE unit (the stored values are raw model inputs, not z-scores) plus the model's
    z-score of each point: (x - mu) / sd with the fit's persisted standardization (fallback: the returned history)."""
    std = _hmm_standardization(scope)
    units = regime_input_units(scope)
    out = []
    for k, pts in inputs.items():
        vals = [p["value"] for p in pts if p.get("value") is not None]
        mu = sd = None
        source = None
        if std and k in std[0]:
            i = std[0].index(k)
            mu, sd, source = std[1][i], std[2][i], "model_fit"
        elif len(vals) >= 24:
            mu = sum(vals) / len(vals)
            sd = math.sqrt(sum((x - mu) ** 2 for x in vals) / (len(vals) - 1))
            source = "returned_history"
        zp = [{"date": p["date"], "value": None if p.get("value") is None or not sd else round((p["value"] - mu) / sd, 4)}
              for p in pts]
        u = units.get(k, {})
        out.append({"id": k, "unit": u.get("unit", "level"), "transform": u.get("transform"),
                    "series_id": u.get("series_id"), "points": pts, "z_points": zp,
                    "latest_value": vals[-1] if vals else None,
                    "latest_z": next((z["value"] for z in reversed(zp) if z["value"] is not None), None),
                    "z_basis": source})
    return out


# ------------------------------------------------------------------------------------------------ F15 Beneish


def beneish_detail(db: Any, issuer_id: str | None, sector_id: str | None, m_reported: float | None,
                   signals: dict[str, Any] | None = None) -> dict[str, Any]:
    """Recalibrated Beneish screen for one issuer from the stored components (ratios metric ids beneish_<index>)."""
    from app.analytics import beneish as B  # noqa: PLC0415

    comp: dict[str, Any] = {}
    period = None
    if issuer_id:
        ids = list(B.COMPONENT_METRIC_IDS)
        ph = ", ".join("?" for _ in ids)
        try:
            rows = db.all(f"SELECT metric_id, value, period_end, period_kind FROM metric_latest "
                          f"WHERE issuer_id = ? AND metric_id IN ({ph})", [issuer_id, *ids])
        except Exception:  # noqa: BLE001 - empty snapshot
            rows = []
        for r in rows:
            comp[r["metric_id"].removeprefix("beneish_")] = fnum(r["value"])
            period = period or {"period_end": iso(r["period_end"]), "period_kind": r["period_kind"]}
    a = B.assess(comp, sector_id=sector_id, is_financial=sector_id == "FINS", m_reported=m_reported, signals=signals)
    a["period"] = period
    a["components_available"] = bool(comp)
    return a
