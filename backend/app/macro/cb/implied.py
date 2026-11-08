"""W3.2 job cb_curves_implied: market-implied policy paths from official curves (approximation, labelled).

For every bank with an official curve (curve_sources.LOADERS) and every curve date in the window (default 760 days):
implied policy rate at each upcoming official meeting (where the short end resolves meetings) and at 3/6/12/24m
horizons, cumulative change vs the policy rate in force on that date (bp and moves of 25bp), incremental move per
meeting, change of each implied point vs 1 week / 1 month earlier, and (US) the term-premium-adjusted path using
NY Fed ACM term premia. Method ``implied_v1`` (app.macro.cb.curves docstring). Tables: cb_implied_paths (history),
cb_curves (zero/forward display curves: latest, 1w ago, 1m ago), cb_implied_meta (per bank: source, method,
caveats, status; banks without an official curve are listed as not_available).
"""

from __future__ import annotations

import json
import warnings
from datetime import date, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from . import curve_sources as csrc
from .banks import BANKS
from .common import now_utc, read_table_safe, step_value, update_state, write
from .curves import STEP_BP, implied_path

log = get_logger(__name__)
METHOD = "implied_v1"
WINDOW_DAYS = 760
SNAPSHOTS = (("latest", 0), ("1w", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365))
DISPLAY_TENORS = [i / 12 for i in range(1, 25)] + [2.25 + 0.25 * i for i in range(32)]


def _policy_pairs(s: Settings) -> dict[str, list[tuple[date, float]]]:
    df = read_table_safe("cb_policy_rates", s, columns=["bank", "date", "policy_rate"])
    out: dict[str, list[tuple[date, float]]] = {}
    if df is None:
        return out
    for b, d, v in df.drop_nulls("policy_rate").sort("bank", "date").iter_rows():
        out.setdefault(b, []).append((d, v))
    return out


def _meetings(s: Settings) -> dict[str, list[date]]:
    df = read_table_safe("cb_meetings", s, columns=["bank", "meeting_date"])
    out: dict[str, list[date]] = {}
    if df is not None:
        for b, d in df.iter_rows():
            out.setdefault(b, []).append(d)
    return {k: sorted(set(v)) for k, v in out.items()}


def bank_paths(cs: csrc.CurveSet, policy: list[tuple[date, float]], meetings: list[date], step_bp: float,
               dates: list[date] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    last_meta: dict[str, Any] = {}
    tp_dates = sorted(cs.tp) if cs.tp else []
    for d in sorted(dates or cs.curves):
        cur = cs.curves.get(d)
        pr = step_value(policy, d)
        if cur is None or pr is None:
            continue
        tp = None
        if tp_dates:
            i = max((k for k in tp_dates if k <= d), default=None)
            if i is not None and (d - i).days <= 45:
                tp = cs.tp[i]
        try:
            pts, meta = implied_path(cur, d, pr, meetings if cs.meetings_ok else [], cs.horizons,
                                     cs.effective_lag_days, tp)
        except Exception as e:  # noqa: BLE001
            log.debug("implied %s %s failed: %s", cs.bank, d, e)
            continue
        last_meta = meta
        for p in pts:
            rows.append({"bank": cs.bank, "as_of": d, "point_type": p.point_type, "label": p.label,
                         "target_date": p.target_date, "t_start_years": round(p.t_start, 6),
                         "t_end_years": round(p.t_end, 6), "implied_rate": round(p.implied, 4),
                         "implied_rate_tp_adj": None if p.implied_tp is None else round(p.implied_tp, 4),
                         "policy_rate": pr, "change_bp": round(p.change_bp, 2),
                         "moves_priced": round(p.change_bp / step_bp, 3),
                         "incremental_bp": None if p.incremental_bp is None else round(p.incremental_bp, 2),
                         "basis_bp": meta["basis_bp"], "anchor": meta["anchor"]})
    return rows, last_meta


def add_changes(df: pl.DataFrame) -> pl.DataFrame:
    """change_1w_bp / change_1m_bp: implied_rate now minus the same point (bank, point_type, label) on the latest
    curve date <= as_of - 7 / - 30 days."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Float64).alias("change_1w_bp"),
                               pl.lit(None, dtype=pl.Float64).alias("change_1m_bp"))
    base = df.select("bank", "point_type", "label", "as_of", "implied_rate").sort("as_of")
    out = df.sort("as_of")
    for col, days in (("change_1w_bp", 7), ("change_1m_bp", 30)):
        key = out.with_columns((pl.col("as_of") - pl.duration(days=days)).alias("_k")).sort("_k")
        prev = base.rename({"as_of": "_k", "implied_rate": "_prev"})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # sorted on _k; by-group sortedness follows
            j = key.join_asof(prev.sort("_k"), on="_k", by=["bank", "point_type", "label"], strategy="backward",
                              tolerance=f"{6 if days == 7 else 10}d")
        out = j.with_columns(((pl.col("implied_rate") - pl.col("_prev")) * 100).round(2).alias(col)).drop("_k", "_prev")
    return out


def display_curve(cs: csrc.CurveSet, d: date, snap: str) -> list[dict[str, Any]]:
    cur = cs.curves[d]
    mt = getattr(cur, "max_t", 10.0)
    out = []
    for t in DISPLAY_TENORS:
        if t > min(mt, 10.0) + 1e-9:
            break
        try:
            out.append({"bank": cs.bank, "as_of": d, "snapshot": snap, "tenor_years": round(t, 4),
                        "zero_rate": round(cur.zero(t), 4), "forward_rate": round(cur.inst_fwd(t), 4)})
        except Exception:  # noqa: BLE001
            continue
    return out


def build_implied(s: Settings | None = None, today: date | None = None, window_days: int = WINDOW_DAYS,
                  banks: list[str] | None = None, write_out: bool = True) -> dict[str, Any]:
    s = s or get_settings()
    today = today or now_utc().date()
    now = now_utc()
    start = today - timedelta(days=window_days)
    policy, meetings = _policy_pairs(s), _meetings(s)
    rows: list[dict[str, Any]] = []
    curve_rows: list[dict[str, Any]] = []
    meta_rows: list[dict[str, Any]] = []
    rep: dict[str, Any] = {"window": [start.isoformat(), today.isoformat()], "banks": {}}
    for b in BANKS:
        if banks and b.code not in banks:
            continue
        if b.code not in csrc.LOADERS:
            meta_rows.append({"bank": b.code, "status": "not_available", "as_of": None,
                              "source": None, "method": None,
                              "caveats": json.dumps([csrc.NOT_AVAILABLE.get(b.code, "No official curve.")]),
                              "meetings_resolved": False, "tp_adjusted": False, "n_dates": 0})
            rep["banks"][b.code] = "not_available"
            continue
        try:
            cs = csrc.load(b.code, s, start)
        except Exception as e:  # noqa: BLE001
            log.warning("cb_curves_implied: %s loader failed: %s", b.code, e)
            meta_rows.append({"bank": b.code, "status": "failed", "as_of": None, "source": None, "method": None,
                              "caveats": json.dumps([f"loader failed: {type(e).__name__}: {str(e)[:200]}"]),
                              "meetings_resolved": False, "tp_adjusted": False, "n_dates": 0})
            rep["banks"][b.code] = f"failed: {e}"
            continue
        r, meta = bank_paths(cs, policy.get(b.code, []), meetings.get(b.code, []), b.step_bp)
        rows += r
        dates = sorted(cs.curves)
        latest = max((x["as_of"] for x in r), default=None)
        if latest:
            for snap, lag in SNAPSHOTS:
                cand = [d for d in dates if d <= latest - timedelta(days=lag)]
                if cand:
                    curve_rows += display_curve(cs, cand[-1], snap)
        stale = latest is None or (today - latest).days > 10
        meta_rows.append({"bank": b.code, "status": ("stale" if stale and latest else "ok") if latest else "no data",
                          "as_of": latest, "source": cs.source, "method": cs.method,
                          "caveats": json.dumps(cs.caveats + ([f"stub anchor: {meta.get('anchor')}"] if meta else [])),
                          "meetings_resolved": cs.meetings_ok, "tp_adjusted": bool(cs.tp), "n_dates": len(dates)})
        rep["banks"][b.code] = {"dates": len(dates), "rows": len(r), "latest": str(latest), "meta": meta}
    schema = {"bank": pl.Utf8, "as_of": pl.Date, "point_type": pl.Utf8, "label": pl.Utf8, "target_date": pl.Date,
              "t_start_years": pl.Float64, "t_end_years": pl.Float64, "implied_rate": pl.Float64,
              "implied_rate_tp_adj": pl.Float64, "policy_rate": pl.Float64, "change_bp": pl.Float64,
              "moves_priced": pl.Float64, "incremental_bp": pl.Float64, "basis_bp": pl.Float64, "anchor": pl.Utf8}
    df = add_changes(pl.DataFrame(rows, schema=schema))
    df = df.with_columns(pl.lit(METHOD).alias("method_version"), pl.lit(now).alias("computed_at"))
    cdf = pl.DataFrame(curve_rows, schema={"bank": pl.Utf8, "as_of": pl.Date, "snapshot": pl.Utf8,
                                           "tenor_years": pl.Float64, "zero_rate": pl.Float64,
                                           "forward_rate": pl.Float64}).with_columns(pl.lit(now).alias("computed_at"))
    mdf = pl.DataFrame(meta_rows, schema={"bank": pl.Utf8, "status": pl.Utf8, "as_of": pl.Date, "source": pl.Utf8,
                                          "method": pl.Utf8, "caveats": pl.Utf8, "meetings_resolved": pl.Boolean,
                                          "tp_adjusted": pl.Boolean, "n_dates": pl.Int32}
                       ).with_columns(pl.lit(METHOD).alias("method_version"), pl.lit(now).alias("computed_at"))
    rep.update(rows=df.height, curve_rows=cdf.height)
    if write_out:
        rep["written"] = {"cb_implied_paths": write(df, "cb_implied_paths", s), "cb_curves": write(cdf, "cb_curves", s),
                          "cb_implied_meta": write(mdf, "cb_implied_meta", s)}
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    """Job cb_curves_implied (nightly, after cb_rates). params: window_days, banks."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = build_implied(ctx.settings, window_days=int(p.get("window_days", WINDOW_DAYS)), banks=p.get("banks"))
    bad = [k for k, v in rep["banks"].items() if isinstance(v, str) and v.startswith("failed")]
    update_state({"items": {"W3.2": "done"}, "last_runs": {"cb_curves_implied": {
        "at": now_utc().isoformat(timespec="seconds"), "rows": rep["rows"], "failed": bad}}}, ctx.settings)
    return JobResult(status="partial" if bad else "success", rows_written=rep.get("written", {}),
                     entities_ok=len(rep["banks"]) - len(bad), entities_failed=len(bad),
                     message=json.dumps({"rows": rep["rows"], "failed": bad})[:2000])


_ = STEP_BP
