"""Synthetic conformal tables (docs/CONFORMAL_TECHNICALS_PLAN.md P3) for the API / serving tests: envelopes (two
origins for the first 60 securities), touch curves (region-wide pools), two US peer groups with a year of scores and
the two latest member sessions, and coverage audit rows (rolling + walk-forward). Written through the contracts."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta
from statistics import NormalDist
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings
from app.core.storage import write_table

Q = {80: 1.70, 90: 2.80, 95: 3.95}          # marginal radii (squared z units), slightly above chi2(1)
NB_Q = {80: 4.8, 90: 6.4, 95: 8.0}
HZ = (1, 5, 10, 20)


def _bday(d: date, n: int) -> date:
    return np.busday_offset(np.datetime64(d, "D"), n, roll="backward").astype(object)


def build_conformal(s: Settings, as_of: date, secs: list[dict[str, Any]], last_close: dict[str, float],
                    seed: int = 7) -> dict[str, int]:
    rng = np.random.default_rng(seed + 11)
    now = datetime(2026, 10, 10, 6, 0, tzinfo=UTC)
    out: dict[str, int] = {}
    # ------------------------------------------------------------------ envelopes
    env = []
    for k, x in enumerate(secs[:60]):
        for origin in (_bday(as_of, -1), as_of):
            lc = last_close[x["security_id"]] * (0.99 if origin != as_of else 1.0)
            sd = 0.012 + 0.0002 * (k % 20)
            ev = k % 7 == 3
            r: dict[str, Any] = {"as_of_date": origin, "security_id": x["security_id"], "region": x["region"],
                                 "pool_key": f"{x['region']}|*", "sector_id": x["sector_id"], "filter": "ewma",
                                 "radius_mode": "plain", "model_based": True, "last_close": lc, "sigma_d": sd,
                                 "earn_jump_var": 4 * sd * sd, "earn_days_ahead": 7 if ev else None,
                                 "earn_in_window": ev, "q_n_cal": 9000, "q_pool_kind": "event" if ev else "normal",
                                 "nb_earn": False, "computed_at": now}
            for h in HZ:
                r[f"mu_{h}"] = 0.0
                r[f"v_{h}"] = h * sd * sd + (4 * sd * sd if ev and h >= 10 else 0.0)
                for lvl, q in Q.items():
                    r[f"qm_{h}_{lvl}"] = q
                    hw = math.sqrt(q * h * sd * sd) * 1.3          # the (failed) joint set is wider: never served
                    r[f"b{h}_{lvl}_lo"], r[f"b{h}_{lvl}_hi"] = lc * math.exp(-hw), lc * math.exp(hw)
            for lvl, q in NB_Q.items():
                hw = math.sqrt(q) * sd
                r[f"nb_q_{lvl}"] = q
                r[f"nb_h_{lvl}_lo"], r[f"nb_h_{lvl}_hi"] = lc * math.exp(0.008 - hw * 0.8), lc * math.exp(0.008 + hw * 0.8)
                r[f"nb_l_{lvl}_lo"], r[f"nb_l_{lvl}_hi"] = lc * math.exp(-0.008 - hw * 0.8), lc * math.exp(-0.008 + hw * 0.8)
                r[f"nb_c_{lvl}_lo"], r[f"nb_c_{lvl}_hi"] = lc * math.exp(-hw), lc * math.exp(hw)
            if k % 2 == 0:      # Technicals v2 T1 fan: per-h variance + radii (odd k: rows written before the fan)
                r["fan_v"] = [h * sd * sd + (4 * sd * sd if ev and h >= 7 else 0.0) for h in range(1, 21)]
                for lvl, q in Q.items():
                    r[f"fan_qm_{lvl}"] = [q * (1.0 + 0.002 * h) for h in range(1, 21)]
            if origin != as_of:
                r["y_1"] = math.log(last_close[x["security_id"]] / lc)
            env.append(r)
    out["conformal_envelopes"] = write_table(pl.DataFrame(env, infer_schema_length=None), "conformal_envelopes",
                                             mode="overwrite", settings=s)
    # ------------------------------------------------------------------ touch curves (region-wide pools)
    probs = [round(0.01 * i, 2) for i in range(1, 100)]
    nd = NormalDist()
    curves = []
    for reg in sorted({x["region"] for x in secs}):
        for h in HZ:
            for side in ("up", "down"):
                for kind in ("normal", "event"):
                    scale = 1.05 + 0.02 * math.log(h) + (0.15 if kind == "event" else 0.0)
                    u = [scale * nd.inv_cdf((1 + p) / 2) for p in probs]
                    curves.append({"as_of_date": as_of, "pool_key": f"{reg}|*", "region": reg, "horizon": h,
                                   "side": side, "kind": kind, "n": 5000, "probs": probs, "u": u, "computed_at": now})
    out["conformal_touch_curves"] = write_table(pl.DataFrame(curves, infer_schema_length=None), "conformal_touch_curves",
                                                mode="overwrite", settings=s)
    # ------------------------------------------------------------------ peer groups (US)
    us = [x for x in secs if x["region"] == "US"]
    groups = {f"ind:US:{us[0]['subsector_id']}": ("industry", f"{us[0]['subsector_id'].replace('_', ' ')} (US)", us[:24]),
              f"sc:US:{us[5]['security_id']}": ("supply_chain", f"links around {us[5]['ticker']} (US)", us[5:27])}
    version = "20261001"
    grows, srows, mrows = [], [], []
    days = [_bday(as_of, -k) for k in range(259, -1, -1)]
    for gi, (gid, (kind, label, mem)) in enumerate(groups.items()):
        n = len(mem)
        for rank, x in enumerate(mem, 1):
            nb = [m["security_id"] for m in mem if m is not x][:3]
            grows.append({"version": version, "group_id": gid, "security_id": x["security_id"], "issuer_id": x["issuer_id"],
                          "region_bloc": "US", "group_kind": kind, "label": label, "sector_id": x["sector_id"],
                          "subsector_id": x["subsector_id"], "member_rank": rank, "n_members": n,
                          "built_as_of": as_of - timedelta(days=20), "nbr_ids": nb, "nbr_w": [0.5] * len(nb),
                          "computed_at": now})
        q90, q95, q80 = n * 1.45, n * 1.7, n * 1.25
        for t, d in enumerate(days):
            last = t == len(days) - 1
            pct = 0.97 if (last and gi == 0) else float(rng.uniform(0, 1))
            score = float(n * (0.6 + 1.3 * pct))
            srows.append({"as_of_date": d, "group_id": gid, "year": d.year, "version": version, "filter": "gcn_gru",
                          "radius_mode": "aci", "model_based": True, "region_bloc": "US", "n_members": n, "score": score,
                          "score_pct": pct, "q_80": q80, "q_90": q90, "q_95": q95, "q_aci_90": q90 * 1.01,
                          "aci_alpha_90": 0.1, "anomaly_90": score > q90, "anomaly_95": score > q95,
                          "group_move": float(rng.normal(0, 0.01)), "top_member": mem[t % n]["security_id"],
                          "top_share": 0.3, "breakaway_member": mem[(t + 1) % n]["security_id"],
                          "breakaway_z": float(rng.normal(0, 2)) if not last else 3.4, "q_next_90": q90,
                          "q_next_95": q95, "computed_at": now})
        for d in days[-45:]:                       # member laws: 45 sessions (marginal pair-band calibration)
            z = rng.normal(0, 1, n)
            if d == days[-1]:
                z[0] = 3.4
            contrib = z * z
            for k, x in enumerate(mem):
                mrows.append({"as_of_date": d, "group_id": gid, "security_id": x["security_id"], "year": d.year,
                              "r": float(z[k] * 0.015), "z_marg": float(z[k]), "z_cond": float(z[k] * 1.1),
                              "contrib": float(contrib[k]), "contrib_share": float(contrib[k] / contrib.sum()),
                              "mu_next": 0.0, "d_next": 1.2e-4, "l1_next": 0.009, "l2_next": 0.002 * (k % 3 - 1),
                              "l3_next": 0.0, "l4_next": 0.0, "computed_at": now})
    out["conformal_peer_groups"] = write_table(pl.DataFrame(grows, infer_schema_length=None), "conformal_peer_groups",
                                               mode="overwrite", settings=s)
    out["conformal_peer_scores"] = write_table(pl.DataFrame(srows, infer_schema_length=None), "conformal_peer_scores",
                                               mode="overwrite", settings=s)
    out["conformal_peer_members"] = write_table(pl.DataFrame(mrows, infer_schema_length=None), "conformal_peer_members",
                                                mode="overwrite", settings=s)
    # ------------------------------------------------------------------ coverage audit
    cov = []

    def add(app: str, flt: str, st: str, seg: str, hz: str, lvl: int, c: float, n: int, d: date) -> None:
        tgt = lvl / 100
        gap = 100 * (c - tgt)
        cov.append({"as_of_date": d, "application": app, "filter": flt, "segment_type": st, "segment": seg,
                    "horizon": hz, "level": str(lvl), "target": tgt, "n": n, "covered": int(c * n), "coverage": c,
                    "gap_pp": gap, "status": "thin" if n < 200 else ("ok" if abs(gap) <= 1.5 else ("under" if gap < 0 else "over")),
                    "window_start": d - timedelta(days=365), "window_end": d - timedelta(days=30), "model_based": True,
                    "computed_at": now})
    for lvl in Q:
        for h in HZ:
            add("envelope", "ewma", "overall", "all", f"m{h}", lvl, lvl / 100 + 0.002, 400000, as_of)
            add("envelope", "ewma", "overall", "all", str(h), lvl, min(0.999, lvl / 100 + 0.08), 400000, as_of)
            for reg in ("US", "EU", "JP"):
                add("envelope", "ewma", "region", reg, f"m{h}", lvl, lvl / 100 - 0.003, 90000, as_of)
            add("envelope_wf", "m_ewma", "overall", "all", str(h), lvl, lvl / 100 - 0.002, 2400000, as_of)
        add("envelope", "ewma", "overall", "all", "joint", lvl, lvl / 100 + 0.012, 400000, as_of)
        add("nextbar", "ewma+aci", "overall", "all", "joint", lvl, lvl / 100, 400000, as_of)
        add("peer", "gcn_gru", "overall", "all", "joint", lvl, lvl / 100 - 0.008, 60000, as_of)
        add("peer_wf", "gcn_gru+aci", "overall", "all", "joint", lvl, lvl / 100 + 0.004, 707115, as_of)
    add("peer", "gcn_gru", "region", "US", "joint", 90, 0.84, 9000, as_of)       # a flagged segment (> 5 pp under)
    out["conformal_coverage"] = write_table(pl.DataFrame(cov, infer_schema_length=None), "conformal_coverage",
                                            mode="overwrite", settings=s)
    return out
