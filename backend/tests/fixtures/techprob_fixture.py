"""Synthetic Technicals v2 T2-T4 tables (docs/TECHNICALS_V2_PLAN.md) for the API / serving tests: a T2 surface from the
driftless Brownian two-barrier formula (so probabilities are coherent), nightly setup / zone / score rows for the
first 60 securities (those with conformal envelopes), a T4 decile table and gate verdicts (T2 pass at 5 / 10 / 20,
fail at 40 / 60; T3 hold pass, edge fail; T4 fail). Written through the contracts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table
from app.techprob import barriers as B
from app.techprob import serve as TS


def _surface_rows(fit_date: date) -> list[dict[str, Any]]:
    A, Bb = np.meshgrid(B.GRID, B.GRID, indexing="ij")
    pu, pd = B.bm_two_barrier(A.ravel(), Bb.ravel())
    rows = []
    for r in (*B.REGIONS, B.ALL_REGION):
        for v in B.VOL_REGIMES:
            for e in (False, True):
                for H in B.HORIZONS:
                    rows.append({"fit_date": fit_date, "region": r, "vol_regime": v, "earn_window": e, "horizon": H,
                                 "n_origins": 100000 if r != "OTHER" else 0, "grid": B.GRID.tolist(), "p_up": pu.tolist(),
                                 "p_down": pd.tolist(), "p_tie": [0.0] * len(pu), "mtm_neither_z": [0.05] * len(pu),
                                 "computed_at": datetime(2026, 10, 10, 6, 0, tzinfo=UTC)})
    return rows


def build_techprob(s: Settings, as_of: date, secs: list[dict[str, Any]], last_close: dict[str, float],
                   seed: int = 7) -> dict[str, int]:
    rng = np.random.default_rng(seed + 23)
    now = datetime(2026, 10, 10, 6, 30, tzinfo=UTC)
    out: dict[str, int] = {}
    srows = _surface_rows(as_of)
    out["tech_prob_surfaces"] = write_table(pl.DataFrame(srows, infer_schema_length=None), "tech_prob_surfaces", settings=s)
    surf = B.surface_from_rows(srows)
    t2, t3, t4 = [], [], []
    for k, x in enumerate(secs[:60]):
        sid = x["security_id"]
        c = last_close[sid]
        sd = 0.012 + 0.0002 * (k % 20)
        setup = {"bias": "long", "entry": c * 0.985, "target1": c * 1.05, "invalidation": c * 0.95, "reward_risk": 2.0}
        Vh = {H: H * sd * sd for H in B.HORIZONS}
        bar = (1, float(np.log(1.05)), float(np.log(1 / 0.95)), float(np.log(1.05) / np.log(1 / 0.95)))
        for h in TS.horizon_rows(surf, c, bar, Vh, x["region"], 1, None, 5e7, sd, {}):
            t2.append({"as_of_date": as_of, "security_id": sid, **{k2: v for k2, v in h.items()}, "direction": 1,
                       "bias": setup["bias"], "close": c, "entry": setup["entry"], "target1": setup["target1"],
                       "invalidation": setup["invalidation"], "reward_risk": setup["reward_risk"], "reward_risk_close": bar[3],
                       "sigma_h": float(np.sqrt(Vh[h["horizon"]])), "cost_round_trip": 0.001, "vol_regime": "mid",
                       "region_pool": x["region"], "fit_date": as_of.isoformat(),
                       "gate": "production" if h["horizon"] <= 20 else "experimental", "computed_at": now})
        for z in range(6):
            side = "support" if z % 2 == 0 else "resistance"
            p1 = float(rng.uniform(0.45, 0.65))
            p0 = p1 - float(rng.normal(0.01, 0.02))
            sig = z == 0
            t3.append({"as_of_date": as_of, "security_id": sid, "zone_id": z + 1, "side": side, "in_zone": False,
                       "distance_atr": 1.5 * (z // 2 + 1) * (-1 if side == "support" else 1), "p_hold": p1, "p_hold_ctrl": p0,
                       "edge": p1 - p0, "edge_bucket": 4 if sig else 2, "edge_significant": sig,
                       "edge_direction": "better_than_random" if sig else "none", "edge_ci_lo": 0.01 if sig else -0.01,
                       "edge_ci_hi": 0.05 if sig else 0.012, "edge_realised": 0.03 if sig else 0.001, "model_kind": "hgb",
                       "fit_date": as_of.isoformat(), "gate": "production", "gate_edge": "experimental", "computed_at": now})
        row = {"as_of_date": as_of, "security_id": sid, "region": x["region"], "sector_id": x["sector_id"],
               "technical_score": 72.0, "technical_score_pct": float(rng.uniform(20, 90)), "pct_technical": float(rng.uniform(5, 95)),
               "score_trend": 90.0, "score_structure": 80.0, "score_location": 60.0, "score_rr": 50.0, "score_momentum": 70.0,
               "n_peers": 25, "vol_regime": "mid", "fit_date": as_of.isoformat(), "computed_at": now}
        for kk in ("trend", "structure", "location", "rr", "momentum"):
            row[f"pct_{kk}"] = float(rng.uniform(0, 100))
        for h in (5, 20, 60):
            row.update({f"h{h}_decile": 8, f"h{h}_median": 0.001 * h / 5, f"h{h}_q25": -0.02 * h / 20, f"h{h}_q75": 0.025 * h / 20,
                        f"h{h}_mean": 0.0012 * h / 5, f"h{h}_hit_rate": 0.52, f"h{h}_n": 41000, f"h{h}_median_lo": 0.0,
                        f"h{h}_median_hi": 0.002 * h / 5, f"h{h}_hit_lo": 0.51, f"h{h}_hit_hi": 0.53,
                        f"h{h}_edge_significant": False, f"h{h}_gate": "experimental"})
        t4.append(row)
    out["tech_prob_setups"] = write_table(pl.DataFrame(t2, infer_schema_length=None), "tech_prob_setups", settings=s)
    out["tech_prob_zones"] = write_table(pl.DataFrame(t3, infer_schema_length=None), "tech_prob_zones", settings=s)
    out["tech_prob_scores"] = write_table(pl.DataFrame(t4, infer_schema_length=None), "tech_prob_scores", settings=s)
    bk = []
    for h in (5, 20, 60):
        for d in range(1, 11):
            bk.append({"fit_date": as_of, "score_col": "technical_score", "horizon": h, "decile": d,
                       "score_lo": None if d == 1 else 10.0 * (d - 1) + 20, "score_hi": None if d == 10 else 10.0 * d + 20,
                       "n": 41000, "median": 0.0002 * (d - 5) * h / 5, "q25": -0.02, "q75": 0.02, "mean": 0.0003 * (d - 5),
                       "hit_rate": 0.5 + 0.002 * (d - 5), "median_lo": None, "median_hi": None, "hit_lo": None, "hit_hi": None,
                       "edge_significant": False, "ic_mean": 0.004, "ic_t": 0.8, "computed_at": now})
    out["tech_prob_score_buckets"] = write_table(pl.DataFrame(bk, infer_schema_length=None), "tech_prob_score_buckets", settings=s)
    gates = []
    for o in ("t2_h5", "t2_h10", "t2_h20", "t2_h40", "t2_h60", "t3_hold", "t3_edge", "t4_h5", "t4_h20", "t4_h60"):
        ok = o in ("t2_h5", "t2_h10", "t2_h20", "t3_hold")
        gates.append({"fit_date": as_of, "output": o, "verdict": "pass" if ok else "fail",
                      "shown_as": "production" if ok else "experimental", "ece": 0.012 if ok else 0.04,
                      "max_regime_gap": 0.03 if ok else 0.07, "beats_baselines": ok, "edge_significant": None,
                      "detail": json.dumps({"summary": "fixture gate"}), "validated_at": "2026-10-22T00:00:00Z", "computed_at": now})
    out["tech_prob_gates"] = write_table(pl.DataFrame(gates, infer_schema_length=None), "tech_prob_gates", settings=s)
    return out
