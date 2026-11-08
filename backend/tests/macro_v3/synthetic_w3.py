"""Synthetic central-bank monitor tables for API tests (macro-w3). Uses the real derivation functions (decisions,
rate cycle, implied paths on a synthetic forward curve, balance-sheet pace) so the API sees realistic shapes."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.storage import write_table
from app.macro.cb import rates as rt
from app.macro.cb.balance_sheets import add_pace
from app.macro.cb.banks import BANKS
from app.macro.cb.curves import FuncCurve, implied_path

UTC = timezone.utc


def _bdays(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def add_macro_w3(s: Settings, as_of: date) -> dict[str, Any]:
    now = datetime.now(UTC)
    today = date.today()
    start = today - timedelta(days=3 * 365)
    days = _bdays(start, today)
    counts: dict[str, Any] = {}
    rates, decs, meets, cyc, paths, metas, curves, stance, tone = [], [], [], [], [], [], [], [], []
    for k, (code, base) in enumerate((("fed", 4.0), ("ecb", 2.0), ("boe", 3.75))):
        # meetings every ~6 weeks, a hike at the 3rd-last past meeting
        mdates = [start + timedelta(days=42 * i + 10 + k) for i in range(0, 34)]
        past = [m for m in mdates if m <= today]
        hike = past[-3]
        pairs = [(start, base), (hike + timedelta(days=1), base + 0.25)]
        for m in mdates:
            meets.append({"bank": code, "meeting_date": m,
                          "scheduled_at": datetime(m.year, m.month, m.day, 18, tzinfo=UTC), "time_known": False,
                          "source_id": "synthetic", "name": "Policy meeting", "url": "https://example.org",
                          "sep": code == "fed" and m.month in (3, 6, 9, 12), "is_past": m <= today, "computed_at": now})
        for r in rt.derive_decisions(pairs, mdates, today):
            decs.append({"bank": code, "decision_date": r["decision_date"], "effective_date": r["effective_date"],
                         "rate_before": r["rate_before"], "rate_after": r["rate_after"], "change_bp": r["change_bp"],
                         "action": r["action"], "scheduled": r["scheduled"], "date_basis": r["date_basis"],
                         "vote": "6-3 (3 for higher)" if code == "boe" else None,
                         "vote_detail": json.dumps({"A": base, "B": base + 0.25}) if code == "boe" else None,
                         "vote_source": "boe:mpcvoting.xlsx" if code == "boe" else None,
                         "method_version": rt.METHOD_DECISIONS, "computed_at": now})
        for d in days:
            pr = rt._asof(pairs, d)
            rates.append({"bank": code, "date": d, "policy_rate": pr, "lower": pr - 0.125 if code == "fed" else None,
                          "upper": pr + 0.125 if code == "fed" else None, "deposit": pr, "lending": None, "mro": None,
                          "overnight": pr + 0.01, "bis_rate": pr, "source": "synthetic", "obs_date": d,
                          "computed_at": now})
        moves = [r for r in rt.derive_decisions(pairs, mdates, today) if r["change_bp"]]
        for d, ph, lcd, lbp in rt.cycle_phases(moves, days):
            cyc.append({"bank": code, "date": d, "phase": ph, "last_change_date": lcd, "last_change_bp": lbp,
                        "policy_rate": rt._asof(pairs, d), "method_version": rt.METHOD_CYCLE, "computed_at": now})
        # implied paths: rising forward curve, 12 curve dates
        pol = base + 0.25
        for j in range(12):
            d = today - timedelta(days=3 * j + 1)
            c = FuncCurve(lambda t, j=j: pol + 0.1 + 0.3 * t + 0.01 * j, max_t=5.0)
            tp = [(float(n), 0.1 * n) for n in range(1, 6)] if code == "fed" else None
            pts, meta = implied_path(c, d, pol, mdates, tp_pts=tp)
            for p in pts:
                paths.append({"bank": code, "as_of": d, "point_type": p.point_type, "label": p.label,
                              "target_date": p.target_date, "t_start_years": p.t_start, "t_end_years": p.t_end,
                              "implied_rate": p.implied, "implied_rate_tp_adj": p.implied_tp, "policy_rate": pol,
                              "change_bp": p.change_bp, "moves_priced": p.change_bp / 25, "incremental_bp": p.incremental_bp,
                              "basis_bp": meta["basis_bp"], "anchor": meta["anchor"], "change_1w_bp": 1.0,
                              "change_1m_bp": -2.0, "method_version": "implied_v1", "computed_at": now})
            if j == 0:
                for snap in ("latest", "1w", "1m", "3m", "6m", "1y"):
                    for t in (0.25, 0.5, 1.0, 2.0):
                        curves.append({"bank": code, "as_of": d, "snapshot": snap, "tenor_years": t,
                                       "zero_rate": c.zero(t), "forward_rate": c.inst_fwd(t), "computed_at": now})
        metas.append({"bank": code, "status": "ok", "as_of": today - timedelta(days=1), "source": "synthetic curve",
                      "method": "test", "caveats": json.dumps(["approximation"]), "meetings_resolved": True,
                      "tp_adjusted": code == "fed", "n_dates": 12, "method_version": "implied_v1", "computed_at": now})
        for i in range(24):
            m = date(today.year, today.month, 1) - timedelta(days=31 * i)
            m = date(m.year, m.month, 1)
            stance.append({"bank": code, "date": m, "policy_rate": pol, "cpi_yoy": 2.5, "core_yoy": 2.8,
                           "infl_taylor": 2.8, "infl_taylor_measure": "core CPI y/y", "expected_infl": 2.4,
                           "expected_infl_source": "AR(1) (model-based)", "real_rate_cpi": pol - 2.5,
                           "real_rate_core": pol - 2.8, "real_rate_expected": pol - 2.4, "output_gap": 0.5,
                           "output_gap_source": "synthetic", "r_star": 1.0 if code == "fed" else None,
                           "r_star_source": "nyfed:LW one-sided" if code == "fed" else None, "inflation_target": 2.0,
                           "taylor_1993": 5.45, "taylor_rstar": 4.45 if code == "fed" else None, "taylor_balanced": 5.7,
                           "method_version": "stance_v1", "computed_at": now})
        for i, m in enumerate(past[-6:]):
            tone.append({"bank": code, "date": m, "doc_type": "summary_minutes" if code == "boe" else "statement",
                         "url": "https://example.org/doc", "n_sentences": 20, "tone_lexicon": 0.1 * i - 0.2,
                         "tone_model": 0.05 * i - 0.1, "hawkish_share": 0.3, "dovish_share": 0.2,
                         "dissents": 1 if code == "fed" else None, "vote_text": "11-1" if code == "fed" else None,
                         "model_id": "mrince/CBRT-RoBERTa-HawkishDovish-Classifier", "method_version": "tone_v1",
                         "computed_at": now})
    for b in BANKS:
        if b.code not in ("fed", "ecb", "boe"):
            metas.append({"bank": b.code, "status": "not_available", "as_of": None, "source": None, "method": None,
                          "caveats": json.dumps(["No official curve."]), "meetings_resolved": False, "tp_adjusted": False,
                          "n_dates": 0, "method_version": "implied_v1", "computed_at": now})
    proj = []
    for rel, v in ((today - timedelta(days=120), 3.6), (today - timedelta(days=30), 3.9)):
        for yr in (today.year, today.year + 1):
            for stat, x in (("median", v), ("central_low", v - 0.2), ("central_high", v + 0.2)):
                proj.append({"bank": "fed", "release_date": rel, "exercise": f"SEP {rel}", "variable": "policy_rate",
                             "horizon": str(yr), "stat": stat, "value": x, "unit": "percent", "source": "fred:FEDTARMD",
                             "computed_at": now})
        proj.append({"bank": "fed", "release_date": rel, "exercise": f"SEP {rel}", "variable": "policy_rate",
                     "horizon": "longer_run", "stat": "median", "value": 3.0, "unit": "percent",
                     "source": "fred:FEDTARMDLR", "computed_at": now})
    bsr = [("fed", d, "total_assets", 7000.0 - 5 * i, "fred:WALCL") for i, d in
           enumerate(sorted({d - timedelta(days=d.weekday() - 2) for d in days if d.weekday() == 2}))]
    bsdf = add_pace(pl.DataFrame(bsr, schema={"bank": pl.Utf8, "date": pl.Date, "component": pl.Utf8,
                                              "value_bn": pl.Float64, "source": pl.Utf8}, orient="row"))
    bsdf = bsdf.with_columns(pl.lit("USD").alias("currency"), pl.lit("bs_v1").alias("method_version"),
                             pl.lit(now).alias("computed_at"))
    for name, rows in (("cb_policy_rates", rates), ("cb_decisions", decs), ("cb_meetings", meets),
                       ("cb_rate_cycle", cyc), ("cb_implied_paths", paths), ("cb_implied_meta", metas),
                       ("cb_curves", curves), ("cb_projections", proj), ("cb_stance", stance), ("cb_tone", tone)):
        counts[name] = write_table(pl.DataFrame(rows, infer_schema_length=None), name, settings=s)
    counts["cb_balance_sheets"] = write_table(bsdf, "cb_balance_sheets", settings=s)
    return counts
