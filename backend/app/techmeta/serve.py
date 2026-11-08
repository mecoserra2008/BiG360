"""Query-time helpers for the meta-labelled setup endpoints (GET /technicals/{id}/meta-setup and the thesis view of
GET /technicals/{id}/setup-probabilities). MODEL-BASED; "calibrated on history", never "X% chance today"."""

from __future__ import annotations

import json
import math
import os
import pickle
import threading
from pathlib import Path
from typing import Any

import numpy as np

VALIDATION_DOC = "docs/TECHNICALS_V3_VALIDATION.md#meta-labelling"

LABELS = {
    "model_based": "Model-based, calibrated on history: the share of similar setups (same T2 probability, geometry and "
                   "context) that reached T1 before the invalidation in the 2013-2026 purged walk-forward (mvp US + major "
                   "EU / UK / JP names). Not a statement about this trade.",
    "meta": "Context-adjusted (meta-labelling): a secondary model re-estimates the chance that the trade setup wins, starting "
            "from the T2 two-barrier probability and adjusting it for market regime, risk gauge, VIX, peer-group break-aways, "
            "insider / short-interest / smart-money flow, earnings proximity, squeeze state, ADX and trend alignment.",
    "decision": "Take / skip: take when the context-adjusted expected R (after costs, time exit marked to market) exceeds a "
                "threshold chosen on the training history only. Suggested size: a quarter-Kelly fraction of equity at risk "
                "per 1R (capped at 2%), from the context-adjusted outcome probabilities.",
    "drivers": "Drivers: the largest contributions to the adjustment vs T2 (logit units; positive = raises the chance of T1 "
               "first). Linear model: coefficient x standardised value; boosted trees: TreeSHAP.",
    "experimental": "Experimental: this output did not pass the meta-labelling validation gate (or the security is outside "
                    "the validated universe); shown only with the experimental toggle.",
}

FEATURE_LABELS = {
    "c_rw_pct": "Range-width percentile (squeeze)", "c_rv_pct": "Realised-vol percentile", "c_atr_pct": "ATR percentile",
    "c_adx": "ADX(14)", "c_di_signed": "+DI vs -DI (with the setup)", "c_trend_d": "Daily trend (with the setup)",
    "c_trend_w": "Weekly trend (with the setup)", "c_align": "Daily / weekly trend aligned (with the setup)",
    "c_sma50": "Distance to the 50d average (with the setup)", "c_ret5": "5-session move (with the setup)",
    "c_ret20": "20-session move (with the setup)",
    "m_vix": "VIX level", "m_vix_pct": "VIX 1-year percentile", "m_vix_chg20": "VIX 20d change", "m_risk": "Risk-on / off gauge",
    "m_risk_chg20": "Risk gauge 20d change", "m_eig1": "Cross-asset correlation (eigenvalue share)",
    "p_z_signed": "Peer break-away z (with the setup)", "p_absz": "Peer break-away |z|", "p_ratio": "Peer-group anomaly ratio",
    "p_days": "Days since a peer break-away",
    "i_net_signed": "Insider net buying 90d (with the setup)", "i_buyers": "Insider buyers 90d", "i_sellers": "Insider sellers 90d",
    "i_cluster_signed": "Insider cluster buy (with the setup)", "si_pct": "Short interest % of shares",
    "si_z_signed": "Short interest z (with the setup)", "si_dtc": "Days to cover",
    "sm_score_signed": "Smart-money score (with the setup)", "sm_inst_signed": "Institutional flow z (with the setup)",
    "e_days_ahead": "Sessions to earnings", "e_days_since": "Sessions since earnings", "e_post5": "Post-earnings window",
    "g_log_a": "Distance to the upper barrier (sigma)", "g_log_b": "Distance to the lower barrier (sigma)",
    "g_rr": "Reward:risk from the close", "g_dir": "Direction (long / short)", "g_entry_gap": "Limit-entry gap (sigma)",
    "g_vol_ann": "Annualised volatility", "g_vol_regime": "Vol regime", "g_earn_in_h": "Earnings inside the horizon",
    "s_score_trend": "Setup trend score", "s_score_structure": "Setup structure score", "s_score_location": "Setup location score",
    "s_score_momentum": "Setup momentum score", "s_trend_signed": "Setup trend (with the setup)",
    "s_struct_signed": "Market structure (with the setup)",
    "t2_logit_pw": "T2 P(win) (recalibration)", "t2_logit_pl": "T2 P(loss) (recalibration)", "t2_pn": "T2 P(neither)",
    "t2_er_mtm": "T2 expected R", "t2_cost_r": "Costs in R",
}
for _p, _lab in (("m_cyc_", "Macro cycle: "), ("m_hmm_", "Macro HMM regime: "), ("m_rate_", "Central bank: ")):
    for _s in ("expansion", "slowdown", "contraction", "recovery", "hiking", "cutting", "pausing"):
        FEATURE_LABELS[f"{_p}{_s}"] = f"{_lab}{_s}"
for _r in ("US", "EU", "UK", "JP"):
    FEATURE_LABELS[f"r_{_r}"] = f"Region {_r}"
for _s in ("COMM", "CDIS", "CSTA", "ENRG", "FINS", "HLTH", "INDU", "MATR", "REAL", "TECH", "UTIL"):
    FEATURE_LABELS[f"sec_{_s}"] = f"Sector {_s}"


def feature_label(name: str) -> str:
    base, _, suf = name.partition("__")
    lab = FEATURE_LABELS.get(base) or (base[2:].replace("_", " ") if base.startswith("x_") else base)
    return f"{lab} (missing)" if suf == "missing" else lab


def f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def r(v: Any, nd: int = 6) -> float | None:
    x = f(v)
    return None if x is None else round(x, nd)


def drivers(js: Any, k: int = 3) -> list[dict[str, Any]]:
    try:
        rows = json.loads(js) if isinstance(js, str) else (js or [])
    except (TypeError, ValueError):
        rows = []
    out = []
    for d in rows[:k]:
        c = f(d.get("contribution"))
        out.append({"feature": d.get("feature"), "label": d.get("label") or feature_label(str(d.get("feature"))),
                    "group": d.get("group"), "value": r(d.get("value"), 4), "contribution": r(c, 4),
                    "direction": "raises" if (c or 0) > 0 else "lowers"})
    return out


def gate_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for g in rows:
        out[g["output"]] = {"output": g["output"], "verdict": g.get("verdict") or "not_validated",
                            "experimental": (g.get("shown_as") or "experimental") != "production", "ece": r(g.get("ece"), 5),
                            "max_regime_gap": r(g.get("max_regime_gap"), 5), "beats_baselines": g.get("beats_baselines"),
                            "edge_significant": None, "summary": g.get("summary")}
    return out


def gate_of(idx: dict[str, dict[str, Any]], output: str) -> dict[str, Any]:
    return idx.get(output) or {"output": output, "verdict": "not_validated", "experimental": True, "ece": None,
                               "max_regime_gap": None, "beats_baselines": None, "edge_significant": None, "summary": None}


# ------------------------------------------------------------------------------------------------ query-time model

_lock = threading.Lock()
_cache: dict[str, Any] = {}


def artifact(data_dir: Path) -> dict[str, Any] | None:
    """Latest fitted meta model (data/models/technicals_v3/meta/<fit_date>/meta_model.pkl), cached by path + mtime."""
    root = data_dir / "models" / "technicals_v3" / "meta"
    if not root.exists():
        return None
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and (p / "meta_model.pkl").exists())
    if not dirs:
        return None
    pth = dirs[-1] / "meta_model.pkl"
    key = f"{pth}:{os.path.getmtime(pth)}"
    with _lock:
        if _cache.get("key") != key:
            with open(pth, "rb") as fh:
                _cache["art"] = pickle.load(fh)  # noqa: S301 - our own artifact
            _cache["key"] = key
        return _cache["art"]


def custom_meta(art: dict[str, Any], features_js: Any, horizon_rows: list[dict[str, Any]], bar: tuple[int, float, float, float],
                close: float, sigma_d: float | None, vreg: int, region: str | None, sector: str | None,
                setup_scores: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Meta view for explicit barriers (thesis target / stop): today's stored context features + the custom geometry and
    its T2 probabilities (computed by the caller) -> the fitted model. Rows per horizon present in both."""
    import polars as pl  # noqa: PLC0415

    from app.techmeta import features as F  # noqa: PLC0415
    from app.techmeta import job as J  # noqa: PLC0415

    try:
        ctx = json.loads(features_js) if isinstance(features_js, str) else dict(features_js or {})
    except (TypeError, ValueError):
        ctx = {}
    d, x_up, x_dn, rr = bar
    sc = setup_scores or {}
    out = []
    for h in horizon_rows:
        H = int(h["horizon"])
        mm = (art.get("models") or {}).get(H)
        if mm is None or not ctx or not sigma_d:
            continue
        pn = max(0.0, 1.0 - h["p_win"] - h["p_loss"])
        mtm_r = ((h["expected_r_mtm"] - h["expected_r"]) / pn) if pn > 1e-9 and h.get("expected_r_mtm") is not None else 0.0
        row = {"security_id": "q", "date": None, "direction": d, "pw": h["p_win"], "pl": h["p_loss"], "pn": pn,
               "exp_r_mtm": h.get("expected_r_mtm") if h.get("expected_r_mtm") is not None else h.get("expected_r"),
               "expected_r": h.get("expected_r"), "cost_r": h.get("cost_r") or 0.0, "a": h["a"], "b": h["b"], "rr_close": rr,
               "entry": close, "close": close, "sigma_d": sigma_d, "vol_regime": vreg, "earn": bool(h.get("earn_window")),
               "mtm_r": mtm_r, "region": region or "OTHER", "sector_id": sector or "NA", "trend": sc.get("trend"),
               "structure_state": sc.get("structure_state"),
               **{f"score_{k}": f(sc.get(f"score_{k}")) for k in ("trend", "structure", "location", "momentum")}}
        # directional context was signed by the stored setup's direction: re-sign for the custom direction
        sd = ctx.get("_direction") or d
        for k, v in ctx.items():
            if k.startswith("_"):
                continue
            row[k] = (v * d / sd) if (v is not None and k in SIGNED) else v
        D = F.base_columns(pl.DataFrame([row], infer_schema_length=None))
        sc_ = J.score_frame(mm, D).row(0, named=True)
        out.append({"horizon": H, "p_win": sc_["m_p_win"], "p_loss": sc_["m_p_loss"], "p_neither": sc_["m_p_neither"],
                    "expected_r": sc_["m_expected_r"], "take": bool(sc_["m_take"]), "kelly_fraction": sc_["m_kelly"],
                    "size_rel": sc_["m_size_rel"], "drivers": sc_["m_drivers"], "t2_p_win": h["p_win"], "model_kind": mm.kind,
                    "threshold": mm.threshold})
    return out


SIGNED = {"c_di_signed", "c_trend_d", "c_trend_w", "c_align", "c_sma50", "c_ret5", "c_ret20", "p_z_signed", "i_net_signed",
          "i_cluster_signed", "si_z_signed", "sm_score_signed", "sm_inst_signed"}


def np_float(x: Any) -> float | None:
    if isinstance(x, (np.floating, np.integer)):
        x = float(x)
    return f(x)
