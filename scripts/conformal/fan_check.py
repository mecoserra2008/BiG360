"""Technicals v2 T1 (docs/TECHNICALS_V2_PLAN.md): walk-forward coverage of the MARGINAL calibrated band at EVERY horizon
h = 1..20 sessions (the chart fan), on the same P4 data / protocol as the m_ewma rows of scripts/conformal/wf_envelopes.py.

Per test year Y (2010 -> 2026), region (mvp US + mvp_global EU / UK / JP) and horizon h:
  score R_h = y_h^2 / V(h), V(h) = h s2_t (+ J_t when an earnings day falls in (t, t+h]) from the EWMA filter (lambda 0.94);
  radius = pooled (region x sector) rolling split-conformal quantile of the last 500 realised origins (lag h), separate
  earnings-event pool, origins >= Jan 1 of Y-1 only; plain radius (production).
Two radius variants are compared:
  per_h   - the radius calibrated at h itself (what the envelope job emits for the fan);
  interp  - the radius linearly interpolated in h between the calibrated 1 / 5 / 10 / 20 radii (the serving fallback for
            envelope rows written before the per-h fan existed).
Output: data/reports/conformal/fan_check.json (aggregated counts; no per-security data).

    .venv/Scripts/python.exe scripts/conformal/fan_check.py [--regions US,EU,UK,JP] [--out path.json]
"""

from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

import numpy as np  # noqa: E402

from app.conformal import data as D  # noqa: E402
from app.conformal import envelope as V  # noqa: E402
from app.conformal import validation as W  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.ops.winsys import memory_status  # noqa: E402

FAN = tuple(range(1, 21))
ANCHORS = (1, 5, 10, 20)
LEVELS = (80, 90, 95)


def wait_ram(min_gb: float = 3.0) -> None:
    while True:
        m = memory_status() or {}
        if m.get("avail_gb", 99) >= min_gb:
            return
        print(f"free RAM {m.get('avail_gb', 0):.1f} GB < {min_gb} GB: waiting", flush=True)
        time.sleep(60)


def interp_q(qa: dict[int, np.ndarray], h: int) -> np.ndarray:
    if h in qa:
        return qa[h]
    lo = max(a for a in ANCHORS if a < h)
    hi = min(a for a in ANCHORS if a > h)
    w = (h - lo) / (hi - lo)
    return (1 - w) * qa[lo] + w * qa[hi]


def run_region(region: str, agg: dict, s) -> dict:
    t0 = time.time()
    meta = D.universe_ids(W.REGIONS[region], s)
    pan = D.load_panel(region, W.START, W.END, meta["security_id"].to_list(), s, meta=meta)
    if pan is None:
        return {"region": region, "note": "no prices"}
    keep = np.isfinite(pan.C).sum(0) >= 500
    pan = pan.subset([x for x, k in zip(pan.ids, keep, strict=True) if k])
    ev = D.earnings_days(pan.ids, W.START, W.END, s)
    E = D.flag_matrix(pan.dates, pan.ids, ev)
    a = V.make_arrays(pan.C, pan.H, pan.L, pan.O, E)
    ke = V.first_event_offset(E, 20)
    m = meta.unique("security_id")
    sec = dict(zip(m["security_id"].to_list(), m["sector_id"].to_list(), strict=True))
    pool, _ = V.pool_index(V.pool_keys(region, [sec.get(x) or "NA" for x in pan.ids]))
    s2 = V.ewma_var(a.r, a.E)
    J = V.jump_var(a.r, a.E, s2)
    vix = D.vix_daily(W.START, W.END, s)
    vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
    kv = np.clip(np.searchsorted(vd, pan.dates, side="right") - 1, 0, len(vd) - 1)
    high_vix = vix["vix"].to_numpy()[kv] > 25.0
    dates = pan.dates
    years = dates.astype("datetime64[Y]").astype(int) + 1970
    covid = (dates >= W.COVID[0]) & (dates <= W.COVID[1])
    T, N = a.lc.shape
    # anchor radii per (h, level) at every test origin, for the interpolation variant
    q_anchor: dict[int, dict[int, np.ndarray]] = {lvl: {} for lvl in LEVELS}
    order = list(ANCHORS) + [h for h in FAN if h not in ANCHORS]
    hw_sum = defaultdict(float)
    for h in order:
        Y = V.forward_targets(a, (h,))[..., 0]
        Vh = V.horizon_V(s2, J, ke, (h,))[..., 0]
        R = Y * Y / np.maximum(Vh, 1e-14)
        evh = (ke > 0) & (ke <= h)
        qh = {lvl: np.full((T, N), np.nan, dtype=np.float32) for lvl in LEVELS}
        for y in W.TEST_YEARS:
            t_0, t_1 = W.year_bounds(dates, y)
            c0, _ = W.year_bounds(dates, y - 1)
            if t_0 < 0 or c0 < 0:
                continue
            pc = V.PoolCal(R[c0:t_1], pool, evh[c0:t_1], lag=h, window=500, min_n=200).run(t_0 - c0, t_1 - c0)
            tl = np.arange(t_0 - c0, t_1 - c0)
            for j, lvl in enumerate(LEVELS):
                q = np.where(evh[t_0:t_1], pc.q[tl][:, pool, 1, j], pc.q[tl][:, pool, 0, j])
                qh[lvl][t_0:t_1] = q
        for lvl in LEVELS:
            if h in ANCHORS:
                q_anchor[lvl][h] = qh[lvl]
            variants = {"per_h": qh[lvl]}
            if h not in ANCHORS:
                variants["interp"] = interp_q(q_anchor[lvl], h)
            for var, q in variants.items():
                ok = np.isfinite(R) & np.isfinite(q) & (years[:, None] >= W.TEST_YEARS[0])
                cov = (R <= q) & ok
                segs = {"all": ok, "covid": ok & covid[:, None], "high_vix": ok & high_vix[:, None],
                        "event": ok & evh, "normal": ok & ~evh}
                for sg, msk in segs.items():
                    k = (var, h, lvl, sg)
                    agg[k][0] += int(msk.sum())
                    agg[k][1] += int(cov[msk].sum())
                for y in W.TEST_YEARS:
                    msk = ok & (years == y)[:, None]
                    k = (var, h, lvl, f"y{y}")
                    agg[k][0] += int(msk.sum())
                    agg[k][1] += int(cov[msk].sum())
                if var == "per_h":
                    hw = np.sqrt(np.maximum(np.where(ok, q, 0.0), 0.0) * np.where(ok, Vh, 0.0))
                    hw_sum[(h, lvl, "sum")] += float(hw.sum())
                    hw_sum[(h, lvl, "n")] += float(ok.sum())
        del Y, Vh, R, evh, qh
        gc.collect()
    for (h, lvl, kind), v in list(hw_sum.items()):
        if kind == "sum":
            agg[("hw", h, lvl, "all")][0] += int(hw_sum[(h, lvl, "n")])
            agg[("hw", h, lvl, "all")][2] += v
    return {"region": region, "N": N, "T": T, "seconds": round(time.time() - t0, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="US,EU,UK,JP")
    ap.add_argument("--out")
    a = ap.parse_args()
    s = get_settings()
    out = Path(a.out) if a.out else W.report_dir(s) / "fan_check.json"
    agg: dict = defaultdict(lambda: [0, 0, 0.0])
    info = []
    for rg in a.regions.split(","):
        wait_ram()
        r = run_region(rg, agg, s)
        info.append(r)
        print(r, flush=True)
        gc.collect()
    rows = []
    for (var, h, lvl, seg), (n, c, x) in sorted(agg.items(), key=lambda kv: (str(kv[0][0]), kv[0][1], kv[0][2], str(kv[0][3]))):
        if var == "hw":
            rows.append({"variant": "half_width", "h": h, "level": lvl, "segment": seg, "n": n, "mean_half_width_log": x / n if n else None})
        else:
            rows.append({"variant": var, "h": h, "level": lvl, "segment": seg, "n": n, "covered": c, "coverage": c / n if n else None})
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"regions": info, "protocol": __doc__.split("Output:")[0].strip(), "rows": rows}, indent=1),
                   encoding="utf-8")
    # console summary
    by = {(r["variant"], r["h"], r["level"], r["segment"]): r for r in rows}
    print("h | per_h 80/90/95 | interp 80/90/95 | per_h worst year 90 | covid 90 | VIX>25 90 | event 90")
    for h in FAN:
        def c(var: str, lvl: int, seg: str = "all") -> str:
            r = by.get((var, h, lvl, seg))
            return f"{100 * r['coverage']:.1f}" if r and r.get("coverage") is not None else "-"
        yrs = [(r["segment"], r["coverage"]) for k, r in by.items() if k[0] == "per_h" and k[1] == h and k[2] == 90
               and str(k[3]).startswith("y") and r["n"] > 0]
        wy = min(yrs, key=lambda x: x[1]) if yrs else ("-", float("nan"))
        print(f"{h} | {c('per_h', 80)}/{c('per_h', 90)}/{c('per_h', 95)} | {c('interp', 80)}/{c('interp', 90)}/{c('interp', 95)}"
              f" | {100 * wy[1]:.1f} ({wy[0][1:]}) | {c('per_h', 90, 'covid')} | {c('per_h', 90, 'high_vix')} | {c('per_h', 90, 'event')}")
    print("written", out)


if __name__ == "__main__":
    main()
