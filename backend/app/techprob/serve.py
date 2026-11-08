"""Query-time helpers for the Technicals v2 probability endpoints (T2-T4). MODEL-BASED; pure functions.

Labels follow the conformal convention: "calibrated on history" = historical frequency of the outcome for similar
setups / zones / scores over 2010-2026 (walk-forward), never "X% chance today". Outputs that failed the T6 gate are
returned with experimental = true; the UI hides them unless its experimental toggle is on.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

from app.techprob import barriers as B

VALIDATION_DOC = "docs/TECHNICALS_V2_VALIDATION.md"

LABELS = {
    "model_based": "Model-based, calibrated on history: frequencies of the outcome for similar setups over the 2010-2026 "
                   "walk-forward (mvp US + major EU / UK / JP names), not a statement about any single trade.",
    "setup": "Two-barrier path probabilities from the current close: P(win) = T1 reached before the invalidation within H "
             "sessions, P(loss) = invalidation first (or both on the same bar), P(neither) = neither by H. Empirical "
             "first-passage frequencies at the same distances in volatility units (conformal EWMA sigma incl. the earnings "
             "jump), pooled by region x vol regime x earnings window. Not Gaussian.",
    "expected_r": "Expected R = P(win) x R:R - P(loss) x 1 - round-trip costs (Backtest Lab cost model: commission, ADV "
                  "half spread, square-root impact, borrow for shorts), R:R measured from the close. The marked-to-market "
                  "variant adds P(neither) x the average R of paths that hit neither barrier by H.",
    "break_even": "Break-even P(win) = risk / (reward + risk): the hit rate at which a win-or-lose trade with this R:R "
                  "breaks even before costs.",
    "zone_hold": "P(hold | touch): historical share of touches of similar zones that reversed by >= 1.5 sigma before a close "
                 "beyond the far edge by >= 0.5 sigma within 10 sessions. Calibrated classifier (purged walk-forward, "
                 "isotonic). The same pipeline on random control bands gives P_control; edge = P(hold) - P_control.",
    "zone_touch": "P(touch within H): conformal touch-curve probability of the zone's near edge (calibrated within 0.2 pp).",
    "no_edge": "No reliable edge: in the walk-forward, zones like this did not hold significantly more (or less) often than "
               "random price bands with the same width, approach, trend and regime.",
    "score": "Historical outcomes of this score bucket: next 5 / 20 / 60-session excess log return vs the region's "
             "equal-weight universe (median, IQR, hit rate, n) over the 2010-2026 history; percentile sub-scores rank the "
             "original sub-scores within region x sector.",
    "score_no_edge": "No out-of-sample edge: in the walk-forward the score's rank IC and decile spread were not significant, "
                     "so the bucket statistics describe history only.",
    "experimental": "Experimental: this output failed the T6 validation gate (calibration / regime / baseline check); shown "
                    "only with the experimental toggle.",
}


def f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def r(v: Any, nd: int = 6) -> float | None:
    x = f(v)
    return None if x is None else round(x, nd)


def gate_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for g in rows:
        d = {}
        try:
            d = json.loads(g.get("detail") or "{}")
        except (TypeError, ValueError):
            d = {}
        out[g["output"]] = {"output": g["output"], "verdict": g.get("verdict") or "not_validated",
                            "experimental": (g.get("shown_as") or "experimental") != "production",
                            "ece": r(g.get("ece"), 5), "max_regime_gap": r(g.get("max_regime_gap"), 5),
                            "beats_baselines": g.get("beats_baselines"), "edge_significant": g.get("edge_significant"),
                            "summary": d.get("summary")}
    return out


def gate_of(idx: dict[str, dict[str, Any]], output: str) -> dict[str, Any]:
    return idx.get(output) or {"output": output, "verdict": "not_validated", "experimental": True, "ece": None,
                               "max_regime_gap": None, "beats_baselines": None, "edge_significant": None, "summary": None}


def surface_from_rows(rows: list[dict[str, Any]]) -> B.Surface | None:
    return B.surface_from_rows(rows)


def variances_from_envelope(env: dict[str, Any] | None) -> tuple[dict[int, float], int | None]:
    """V(H) for barriers.HORIZONS from a cf_envelopes row: v_5 / v_10 / v_20 as emitted, 40 / 60 = H sigma_d^2 (+ the
    earnings jump when an event is expected within H)."""
    if not env:
        return {}, None
    sd = f(env.get("sigma_d"))
    J = f(env.get("earn_jump_var")) or 0.0
    eda = env.get("earn_days_ahead")
    eda = int(eda) if eda is not None else None
    out: dict[int, float] = {}
    for H in B.HORIZONS:
        v = f(env.get(f"v_{H}")) if H in (5, 10, 20) else None
        if v is None and sd is not None:
            v = H * sd * sd + (J if eda is not None and 0 < eda <= H else 0.0)
        if v is not None and v > 0:
            out[H] = v
    return out, eda


def custom_barriers(close: float, target: float, stop: float, direction: str | None) -> tuple[int, float, float, float] | None:
    d = 1 if direction == "long" else (-1 if direction == "short" else (1 if target > close else -1))
    if d > 0 and not (stop < close < target):
        return None
    if d < 0 and not (target < close < stop):
        return None
    if d > 0:
        x_up, x_dn = math.log(target / close), math.log(close / stop)
        return d, x_up, x_dn, x_up / x_dn
    x_up, x_dn = math.log(stop / close), math.log(close / target)
    return d, x_up, x_dn, x_dn / x_up


def horizon_rows(surface: B.Surface, close: float, bar: tuple[int, float, float, float], Vh: dict[int, float],
                 region: str | None, vreg: int, eda: int | None, adv: float | None, sigma_d: float | None,
                 costs: dict[str, Any]) -> list[dict[str, Any]]:
    """T2 outputs per horizon for explicit barriers (the query-time path used for thesis target / stop)."""
    dirn, x_up, x_dn, rr = bar
    x_tgt = x_up if dirn > 0 else x_dn
    own = B.setup_horizon(x_tgt, lambda H: Vh.get(H))
    risk = x_dn if dirn > 0 else x_up
    out = []
    for H in B.HORIZONS:
        v = Vh.get(H)
        if not v:
            continue
        sig = math.sqrt(v)
        a, b = x_up / sig, x_dn / sig
        earn = eda is not None and 0 < eda <= H
        e = surface.eval(region, vreg, earn, H, a, b)
        pw, pl = (e["p_up"], e["p_down"] + e["p_tie"]) if dirn > 0 else (e["p_down"], e["p_up"] + e["p_tie"])
        pn = max(0.0, 1.0 - pw - pl)
        c = B.round_trip_cost(adv, sigma_d, dirn < 0, H / 2.0, costs)
        cost_r = c["round_trip"] / max(risk, 1e-6)
        er = B.expected_r(pw, pl, rr, cost_r, pn, e["mtm_neither_z"] * sig * dirn / max(risk, 1e-6))
        pu, pd = B.bm_two_barrier(a, b)
        out.append({"horizon": H, "is_setup_horizon": H == own, "p_win": pw, "p_loss": pl, "p_neither": pn,
                    "expected_r": er["expected_r"], "expected_r_mtm": er["expected_r_mtm"], "break_even_p": er["break_even_p"],
                    "break_even_p_costs": er["break_even_p_costs"], "cost_r": cost_r, "earn_window": earn,
                    "n_pool": e["n_pool"], "region_fallback": e["region_fallback"], "clamped": e["clamped"],
                    "p_win_naive": float(B.naive_rr(rr)), "p_win_bm": float(pu[0] if dirn > 0 else pd[0]), "a": a, "b": b})
    return out


def score_line(h: int, b: dict[str, Any]) -> str | None:
    """'Historically this score bucket -> ...' sentence for one horizon."""
    med, hit, n = f(b.get("median")), f(b.get("hit_rate")), b.get("n")
    if med is None or hit is None or not n:
        return None
    q25, q75 = f(b.get("q25")), f(b.get("q75"))
    iqr = f" (IQR {100 * (math.exp(q25) - 1):+.1f}% to {100 * (math.exp(q75) - 1):+.1f}%)" if q25 is not None and q75 is not None else ""
    return (f"Historically this score bucket -> next {h} sessions median excess {100 * (math.exp(med) - 1):+.2f}%{iqr}, "
            f"beat the region {100 * hit:.0f}% of the time, n = {int(n):,}")


def edge_label(edge_significant: bool, direction: str | None) -> str:
    if not edge_significant:
        return "no reliable edge"
    return "holds more often than random" if direction == "better_than_random" else "holds less often than random"


def np_float(x: Any) -> float | None:
    if isinstance(x, (np.floating, np.integer)):
        x = x.item()
    return f(x)
