"""Technicals v3 #1 / #5 fixture: runs the REAL nightly jobs (app.techv3.job run_factors / run_breakouts) on the synthetic
lake, after writing a gate.json (composite PASS with members hi52 / maxret / ivol; P(follow-through) H = 20 pass) and
fitting the P(follow-through) model on the fixture's own breakout history; validation rows of tech_factor_eval are
synthetic (verdicts consistent with the gate file). Written through the contracts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import polars as pl

from app.core.config import Settings
from app.core.storage import write_table
from app.techv3 import build as BL
from app.techv3 import factors as F
from app.techv3 import job as J
from app.techv3 import panel as P

PASS = ("hi52", "maxret", "ivol")


def _gate(s: Settings) -> dict[str, Any]:
    g = {"generated_at": "2026-10-22T00:00:00+00:00",
         "factors": {f: {"verdict": "pass" if f in PASS else "fail"} for f in F.FACTORS},
         "composite": {"verdict": "pass", "members": list(PASS), "variant": "comp_wf"},
         "breakouts": {"p_follow_20": {"verdict": "pass"}, "p_follow_10": {"verdict": "fail"},
                       "confirmation_20": {"verdict": "fail"}, "base_rates": {"20:1": 0.21, "20:-1": 0.22, "55:1": 0.2, "55:-1": 0.23}}}
    p = J.report_dir(s) / "gate.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    return g


def _eval_rows(as_of: date) -> list[dict[str, Any]]:
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    rows = []
    for f in F.FACTORS:
        sign = F.SIGN[f]
        ali = 0.03 if f in PASS else 0.004
        for h in (5, 20, 60):
            for rg in ("ALL", "US", "EU", "UK", "JP"):
                rows.append({"eval_date": as_of, "eval_kind": "validation", "signal": f, "horizon": h, "region": rg,
                             "segment_type": "all", "segment": "all", "n_dates": 1100, "mean_names": 900.0,
                             "mean_ic": ali * sign, "aligned_ic": ali, "t_stat": (6.0 if f in PASS else 0.8) * sign,
                             "p_value": 1e-6 if f in PASS else 0.4, "p_bh": 1e-5 if f in PASS else 0.5,
                             "verdict": ("pass" if f in PASS else "fail") if (h == 20 and rg == "ALL") else None,
                             "computed_at": now})
    det = {"members": list(PASS), "variant": "comp_wf", "v2_ic_20": -0.012, "beats_v2": True, "n_cells_factor": 105,
           "generated_at": "2026-10-22T00:00:00+00:00", "period": "2008-2026", "universe": "liquid US / EU / UK / JP"}
    rows.append({"eval_date": as_of, "eval_kind": "validation", "signal": "composite", "horizon": 20, "region": "ALL",
                 "segment_type": "all", "segment": "all", "mean_ic": 0.035, "aligned_ic": 0.035, "t_stat": 5.3, "p_value": 1e-7,
                 "verdict": "pass", "detail": json.dumps(det), "computed_at": now})
    rows.append({"eval_date": as_of, "eval_kind": "validation", "signal": "bo_p20", "horizon": 20, "region": "ALL",
                 "segment_type": "all", "segment": "all", "verdict": "pass",
                 "detail": json.dumps({"ece": 0.008, "brier_full_minus_geo": -0.0004}), "computed_at": now})
    rows.append({"eval_date": as_of, "eval_kind": "validation", "signal": "bo_confirm", "horizon": 20, "region": "ALL",
                 "segment_type": "all", "segment": "all", "verdict": "fail",
                 "detail": json.dumps({"confirmed_rate": 0.24, "unconfirmed_rate": 0.21, "base_rate": 0.215, "control_rate": 0.22,
                                       "diff": 0.03, "diff_ci": [0.02, 0.04], "geo_diff": 0.002, "geo_ci": [-0.004, 0.008],
                                       "significant": False, "verdict": "fail", "n_events": 2000000}), "computed_at": now})
    return rows


def build_techv3(s: Settings, as_of: date) -> dict[str, int]:
    out: dict[str, int] = {}
    _gate(s)
    out["tech_factor_eval"] = write_table(pl.DataFrame(_eval_rows(as_of), infer_schema_length=None), "tech_factor_eval", settings=s)
    # P(follow-through) model fitted on the fixture's own full breakout history (the monthly fit's code path)
    con = P.lake(["prices_daily", "securities_master", "issuers"], s)
    frames = []
    try:
        for rg in ("US", "EU", "UK", "JP"):
            start = date(as_of.year - 10, 1, 1)
            secs = P.region_securities(con, rg, start, as_of, min_bars=120)
            if secs.is_empty():
                continue
            cal = P.region_calendar(con, rg, start, as_of)
            ch = P.load_chunk(con, rg, secs["security_id"].to_list(), cal, start, as_of)
            e, _c = BL.breakout_chunk(ch, 260, controls=False)
            if e.height:
                frames.append(e.with_columns(pl.lit(rg).alias("region")))
    finally:
        con.close()
    ev = pl.concat(frames, how="diagonal_relaxed")
    rep = J.fit_breakout_model(ev, as_of, s)
    out["bo_model_train"] = int((rep.get("H20") or {}).get("train", 0)) if isinstance(rep.get("H20"), dict) else 0
    fr = J.run_factors(s, as_of, {"mode": "incremental"})
    out["tech_factors"] = fr["rows"]
    br = J.run_breakouts(s, as_of, {})
    out["tech_breakouts"] = br["events"]
    out["tech_breakout_state"] = br["state_rows"]
    return out
