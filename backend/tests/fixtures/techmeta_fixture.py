"""Synthetic Technicals v3 #2 meta-labelling tables for the API / serving tests: a tiny fitted meta model (LR, T2
offset) saved as the artifact (data/models/technicals_v3/meta/<as_of>/meta_model.pkl, used for the query-time thesis
view), nightly tech_meta_setups rows for the first 40 securities (H = 10 / 20 / 40, scored by that model from their T2
rows) and gate verdicts (meta_h20 pass, the rest fail). Written through the contracts."""

from __future__ import annotations

import json
import pickle
from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import read_table, write_table
from app.techmeta import features as F
from app.techmeta import job as J
from app.techmeta import model as M


def tiny_model(H: int, seed: int = 0) -> M.MetaModel:
    """LR meta model on synthetic rows where the squeeze percentile and the peer z matter."""
    rng = np.random.default_rng(seed + H)
    cols = F.all_features()
    n = 6000
    X = rng.normal(0, 1, (n, len(cols)))
    pw = rng.uniform(0.25, 0.6, n)
    pl_ = (1 - pw) * rng.uniform(0.6, 0.9, n)
    X[:, cols.index("t2_logit_pw")] = M.logit(pw)
    X[:, cols.index("t2_logit_pl")] = M.logit(pl_)
    X[:, cols.index("c_rw_pct")] = rng.uniform(0, 1, n)
    zw = M.logit(pw) + 0.8 * (X[:, cols.index("c_rw_pct")] - 0.5) + 0.3 * X[:, cols.index("p_z_signed")]
    yw = (rng.uniform(size=n) < M.sigmoid(zw)).astype(float)
    yl = ((1 - yw) * (rng.uniform(size=n) < 0.8)).astype(float)
    w = np.ones(n)
    Lw = M.fit_learner("lr", X, yw, w, cols, "t2_logit_pw", params={"lam": 1e-3})
    Ll = M.fit_learner("lr", X, yl, w, cols, "t2_logit_pl", params={"lam": 1e-3})
    mm = M.MetaModel(H, "lr", cols, Lw, Ll, M.Calibrator("identity"), M.Calibrator("identity"), threshold=0.0, size_ref=0.008)
    mm.info = {"fit_date": "fixture", "H": H, "selected": "lr"}
    return mm


def build_techmeta(s: Settings, as_of: date, secs: list[dict[str, Any]], seed: int = 7) -> dict[str, int]:
    rng = np.random.default_rng(seed + 31)
    now = datetime(2026, 10, 10, 6, 45, tzinfo=UTC)
    models = {H: tiny_model(H, seed) for H in (10, 20, 40)}
    gate = {"generated_at": "2026-10-22T00:00:00Z", "outputs": {
        o: {"verdict": "pass" if o == "meta_h20" else "fail", "ece": 0.011 if o == "meta_h20" else 0.03,
            "max_regime_gap": 0.03, "beats_baselines": o == "meta_h20", "summary": "fixture gate"}
        for o in J.OUTPUTS}}
    art = {"fit_date": as_of.isoformat(), "models": models, "gate": gate, "extra": []}
    d = s.data_dir / "models" / "technicals_v3" / "meta" / as_of.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "meta_model.pkl", "wb") as fh:
        pickle.dump(art, fh)
    tp = read_table("tech_prob_setups", settings=s).filter(pl.col("horizon").is_in([10, 20, 40]))
    by = {r["security_id"]: r for r in secs}
    rows = []
    for sid in [x["security_id"] for x in secs[:40]]:
        sub = tp.filter(pl.col("security_id") == sid)
        x = by[sid]
        ctx = {c: float(rng.normal()) for c in F.all_features(F.CONTEXT)}
        ctx.update(c_rw_pct=float(rng.uniform()), e_days_ahead=float(rng.integers(1, 60)))
        for c in F.GROUPS["sector"]:
            ctx[c] = 1.0 if c in (f"r_{x['region']}", f"sec_{x['sector_id']}") else 0.0
        for h in sub.iter_rows(named=True):
            H = int(h["horizon"])
            pn = max(0.0, 1 - h["p_win"] - h["p_loss"])
            row = {"security_id": sid, "date": as_of, "direction": 1, "pw": h["p_win"], "pl": h["p_loss"], "pn": pn,
                   "exp_r_mtm": h["expected_r_mtm"], "expected_r": h["expected_r"], "cost_r": h["cost_r"], "a": h["a"], "b": h["b"],
                   "rr_close": h["reward_risk_close"], "entry": h["entry"], "close": h["close"], "sigma_d": h["sigma_h"] / np.sqrt(H),
                   "vol_regime": 1, "earn": False, "mtm_r": ((h["expected_r_mtm"] - h["expected_r"]) / pn) if pn > 1e-9 else 0.0,
                   "region": x["region"], "sector_id": x["sector_id"], "trend": "up", "structure_state": "bullish",
                   "score_trend": 90.0, "score_structure": 80.0, "score_location": 60.0, "score_momentum": 70.0, **ctx}
            sc = J.score_frame(models[H], F.base_columns(pl.DataFrame([row], infer_schema_length=None))).row(0, named=True)
            dom = x["region"] in F.REGIONS
            rows.append({"as_of_date": as_of, "security_id": sid, "horizon": H, "direction": 1, "region": x["region"],
                         "in_domain": dom, "p_win": sc["m_p_win"], "p_loss": sc["m_p_loss"], "p_neither": sc["m_p_neither"],
                         "expected_r": sc["m_expected_r"], "take": bool(sc["m_take"]), "kelly_fraction": sc["m_kelly"],
                         "size_rel": sc["m_size_rel"], "threshold": 0.0, "logit_adj": sc["m_logit_adj"], "t2_p_win": h["p_win"],
                         "t2_p_loss": h["p_loss"], "t2_expected_r": h["expected_r"], "t2_expected_r_mtm": h["expected_r_mtm"],
                         "reward_risk_close": h["reward_risk_close"], "cost_r": h["cost_r"], "mtm_r": row["mtm_r"],
                         "drivers": sc["m_drivers"], "features": json.dumps({"_direction": 1, **ctx}), "model_kind": "lr",
                         "fit_date": as_of.isoformat(), "gate": "production" if (H == 20 and dom) else "experimental",
                         "gate_decision": "experimental", "computed_at": now})
    out = {"tech_meta_setups": write_table(pl.DataFrame(rows, infer_schema_length=None), "tech_meta_setups", settings=s)}
    gr = [{"fit_date": as_of, "output": o, "verdict": g["verdict"], "shown_as": "production" if g["verdict"] == "pass" else "experimental",
           "ece": g["ece"], "max_regime_gap": g["max_regime_gap"], "beats_baselines": g["beats_baselines"], "economic_t": None,
           "summary": g["summary"], "detail": json.dumps(g), "validated_at": gate["generated_at"], "computed_at": now}
          for o, g in gate["outputs"].items()]
    out["tech_meta_gates"] = write_table(pl.DataFrame(gr, infer_schema_length=None), "tech_meta_gates", settings=s)
    return out
