"""Jobs of the Technicals v2 probability models (docs/TECHNICALS_V2_PLAN.md T2-T4, gate docs/TECHNICALS_V2_VALIDATION.md).

`technicals_probabilities` (nightly, after confluence_zones + conformal_envelopes), function `run`:
  for every security with a tech_setups row on the latest as_of: T2 two-barrier probabilities of its setup for H =
  5 / 10 / 20 / 40 / 60 sessions (+ the setup's own horizon), expected R after Backtest Lab costs and the break-even
  P(win); T3 P(hold | touch) of each confluence zone, the random-control probability and the edge vs random; T4
  percentile sub-scores within region x sector and the score's historical bucket stats. Writes tech_prob_setups,
  tech_prob_zones, tech_prob_scores (latest + month-end snapshots). Every row carries the gate verdict of its output
  (production / experimental) from tech_prob_gates.

`technicals_probabilities_fit` (weekly), function `run_fit`:
  1. point-in-time history of the gate universe (mvp_us + mvp_global EU / UK / JP): incremental weekly origins
     (app.techprob.history; params mode=seed imports a walk-forward build, mode=full rebuilds) -> tech_prob_setup_events,
     tech_prob_zone_events; T2 grid counts over every daily origin;
  2. T2 surface (all complete origins) -> tech_prob_surfaces;
  3. T3 calibrated classifiers (real zones + random controls; calibration on the last year, purged) -> model artifact
     data/models/technicals_v2/<fit_date>/zone_model.pkl;
  4. T4 score bucket table -> tech_prob_score_buckets;
  5. gate verdicts (data/reports/technicals_v2/gate.json, written by the T6 validation) -> tech_prob_gates.

    python -m app.techprob.job [--fit] [--mode seed|incremental|full] [--seed-dir DIR] [--as-of YYYY-MM-DD] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.techprob import barriers as B
from app.techprob import history as Hh
from app.techprob import score_cal as SC
from app.techprob import state as ST
from app.techprob import zone_model as M

log = get_logger("app.techprob.job")

FIT_REGIONS = ("US", "EU", "UK", "JP")
REGION_UNIVERSES = {"US": ("mvp_us",), "EU": ("mvp_global",), "UK": ("mvp_global",), "JP": ("mvp_global",)}
COSTS = {"enabled": True, "commission_bps": 1.0, "spread_model": "adv_buckets", "impact_coef": 0.1, "capital_usd": 1.0e7,
         "borrow_bps": 50.0, "htb_borrow_bps": 300.0}
NIGHTLY_HISTORY_DAYS = 600          # calendar days of bars per security (EWMA warm-up + 252-session vol regime + priors)
NIGHTLY_CHUNK = 1500
REFRESH_SESSIONS_DAYS = 100         # incremental fit: recompute origins of the last 100 days (their labels complete)
SURFACE_KEEP = 8
OUTPUTS = ("t2_h5", "t2_h10", "t2_h20", "t2_h40", "t2_h60", "t3_hold", "t3_edge", "t4_h5", "t4_h20", "t4_h60")


def model_root(settings: Settings | None = None) -> Path:
    p = (settings or get_settings()).data_dir / "models" / "technicals_v2"
    p.mkdir(parents=True, exist_ok=True)
    return p


def gate_file(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / "reports" / "technicals_v2" / "gate.json"


def load_gate(settings: Settings | None = None) -> dict[str, Any]:
    p = gate_file(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except ValueError:
        return {}


def gate_status(gate: dict[str, Any], output: str) -> str:
    """'production' when the output PASSED the T6 gate, else 'experimental' (also when no gate was run)."""
    g = (gate.get("outputs") or {}).get(output) or {}
    return "production" if g.get("verdict") == "pass" else "experimental"


# ------------------------------------------------------------------------------------------------ loaders


def _scan(table: str) -> pl.LazyFrame | None:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        lf = scan_table(table)
        lf.collect_schema()
        return lf
    except Exception as e:  # noqa: BLE001 - table not written yet
        log.info("%s unavailable: %s", table, e)
        return None


def latest_surface() -> tuple[B.Surface | None, str | None]:
    lf = _scan("tech_prob_surfaces")
    if lf is None:
        return None, None
    fd = lf.select(pl.col("fit_date").max()).collect().item()
    if fd is None:
        return None, None
    rows = lf.filter(pl.col("fit_date") == fd).collect().to_dicts()
    return B.surface_from_rows(rows), str(fd)


def latest_zone_model(settings: Settings | None = None) -> dict[str, Any] | None:
    root = model_root(settings)
    dirs = sorted([p for p in root.iterdir() if p.is_dir() and (p / "zone_model.pkl").exists()]) if root.exists() else []
    if not dirs:
        return None
    with open(dirs[-1] / "zone_model.pkl", "rb") as f:
        return pickle.load(f)  # noqa: S301 - our own artifact


def latest_buckets() -> tuple[pl.DataFrame, str | None]:
    lf = _scan("tech_prob_score_buckets")
    if lf is None:
        return pl.DataFrame(), None
    fd = lf.select(pl.col("fit_date").max()).collect().item()
    return (lf.filter(pl.col("fit_date") == fd).collect(), str(fd)) if fd else (pl.DataFrame(), None)


def latest_gates() -> dict[str, Any]:
    lf = _scan("tech_prob_gates")
    if lf is None:
        return {}
    fd = lf.select(pl.col("fit_date").max()).collect().item()
    if fd is None:
        return {}
    rows = lf.filter(pl.col("fit_date") == fd).collect().to_dicts()
    return {"outputs": {r["output"]: r for r in rows}, "fit_date": str(fd)}


def _load_bars(sids: list[str], start: date, end: date, settings: Settings) -> list[Hh.SecInput]:
    """Bars (any region) + USD traded value + realised earnings days for the nightly job."""
    from app.conformal import data as D  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.technicals.data import iter_bars, load_meta, load_price_panel  # noqa: PLC0415

    pan = load_price_panel(sids, start=start, end=end)
    if pan.is_empty():
        return []
    dv = (scan_table("prices_daily").filter((pl.col("date") >= start) & (pl.col("date") <= end) & pl.col("security_id").is_in(sids))
          .select("security_id", "date", (pl.col("close") * pl.col("usd_per_unit").fill_null(1.0) * pl.col("volume")).alias("dv"))
          .collect().unique(["security_id", "date"], keep="last"))
    pan = pan.join(dv, on=["security_id", "date"], how="left").sort(["security_id", "date"])
    meta = load_meta(sids)
    try:
        ev = D.earnings_days(sids, start, end, settings)
    except Exception as e:  # noqa: BLE001
        log.warning("earnings days unavailable: %s", e)
        ev = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date})
    evd = {str(k[0]): np.sort(np.array(g["date"].to_list(), dtype="datetime64[D]")) for k, g in ev.group_by(["security_id"])}
    dvm = {str(k[0]): g["dv"].to_numpy().astype(np.float64) for k, g in pan.select("security_id", "dv").group_by(["security_id"], maintain_order=True)}
    out = []
    for b in iter_bars(pan.drop("dv"), meta, min_bars=60):
        out.append(Hh.SecInput(b, dvm.get(b.security_id, np.full(b.n, np.nan)), evd.get(b.security_id, np.array([], dtype="datetime64[D]"))))
    return out


# ------------------------------------------------------------------------------------------------ T2 (live)


def horizon_variances(s2: float, J: float, earn_days_ahead: int | None) -> dict[int, float]:
    out = {}
    for H in B.HORIZONS:
        v = H * s2
        if earn_days_ahead is not None and 0 < earn_days_ahead <= H and J and J > 0:
            v += J
        out[H] = v
    return out


def setup_probabilities(setup: dict[str, Any], close: float, Vh: dict[int, float], region: str | None, vreg: int,
                        earn_days_ahead: int | None, surface: B.Surface, adv_usd: float | None, sigma_d: float,
                        target: float | None = None, stop: float | None = None, direction: int | None = None
                        ) -> list[dict[str, Any]]:
    """Per-horizon T2 outputs for the setup (or explicit target / stop barriers, e.g. a thesis)."""
    if target is not None and stop is not None:
        d = direction if direction in (1, -1) else (1 if target > close else -1)
        if d > 0 and not (stop < close < target):
            return []
        if d < 0 and not (target < close < stop):
            return []
        bar = (d, math.log(target / close), math.log(close / stop)) if d > 0 else (d, math.log(stop / close), math.log(close / target))
        dirn, x_up, x_dn = bar
        rr = x_up / x_dn if dirn > 0 else x_dn / x_up
    else:
        b = Hh._setup_barriers(setup, close)  # noqa: SLF001
        if b is None:
            return []
        dirn, x_up, x_dn, rr = b
    x_tgt = x_up if dirn > 0 else x_dn
    own = B.setup_horizon(x_tgt, lambda H: Vh.get(H))
    risk = x_dn if dirn > 0 else x_up
    rows = []
    for H in B.HORIZONS:
        v = Vh.get(H)
        if not v or v <= 0:
            continue
        sig = math.sqrt(v)
        a, b_ = x_up / sig, x_dn / sig
        earn = earn_days_ahead is not None and 0 < earn_days_ahead <= H
        r = surface.eval(region, vreg, earn, H, a, b_)
        if dirn > 0:
            pw, pl_ = r["p_up"], r["p_down"] + r["p_tie"]
        else:
            pw, pl_ = r["p_down"], r["p_up"] + r["p_tie"]
        pn = max(0.0, 1.0 - pw - pl_)
        cost = B.round_trip_cost(adv_usd, sigma_d, dirn < 0, H / 2.0, COSTS)
        cost_r = cost["round_trip"] / max(risk, 1e-6)
        mtm_r = r["mtm_neither_z"] * sig * dirn / max(risk, 1e-6)
        er = B.expected_r(pw, pl_, rr, cost_r, pn, mtm_r)
        pu_bm, pd_bm = B.bm_two_barrier(a, b_)
        rows.append({"horizon": H, "is_setup_horizon": H == own, "direction": dirn, "a": a, "b": b_, "sigma_h": sig,
                     "p_win": pw, "p_loss": pl_, "p_neither": pn, "expected_r": er["expected_r"],
                     "expected_r_mtm": er["expected_r_mtm"], "break_even_p": er["break_even_p"],
                     "break_even_p_costs": er["break_even_p_costs"], "cost_r": cost_r, "cost_round_trip": cost["round_trip"],
                     "reward_risk_close": rr, "earn_window": earn, "n_pool": r["n_pool"], "region_fallback": r["region_fallback"],
                     "clamped": r["clamped"], "p_win_naive": float(B.naive_rr(rr)),
                     "p_win_bm": float(pu_bm[0] if dirn > 0 else pd_bm[0])})
    return rows


# ------------------------------------------------------------------------------------------------ nightly


def _ctx_from_setup(s: dict[str, Any], atr: float) -> dict[str, Any]:
    return {"atr": atr, "sma50": s.get("sma50"), "sma200": s.get("sma200"), "structure_state": s.get("structure_state"),
            "rsi": s.get("rsi14")}


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_nightly(ctx.settings, ctx.as_of, ctx.params or {}, cancel=ctx.check_cancelled)
    ok = rep.get("setups", 0) > 0
    return JobResult(status="success" if ok and not rep.get("failed") else ("partial" if ok else "failed"),
                     rows_written={k: rep.get(k, 0) for k in ("tech_prob_setups", "tech_prob_zones", "tech_prob_scores")},
                     entities_ok=rep.get("securities", 0), entities_failed=rep.get("failed", 0),
                     watermark=rep.get("as_of"), message=json.dumps({k: v for k, v in rep.items() if k != "regions"}, default=str)[:4000])


def run_nightly(settings: Settings | None, as_of: date | None, params: dict[str, Any], cancel: Any = None,
                write: bool = True) -> dict[str, Any]:
    from app.conformal import envelope as V  # noqa: PLC0415
    from app.technicals.levels import month_end_retention  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    lf = _scan("tech_setups")
    if lf is None:
        return {"note": "tech_setups missing"}
    if as_of is not None:
        lf = lf.filter(pl.col("as_of_date") <= as_of)
    a_of = lf.select(pl.col("as_of_date").max()).collect().item()
    if a_of is None:
        return {"note": "tech_setups empty"}
    setups = lf.filter(pl.col("as_of_date") == a_of).collect()
    if params.get("tickers"):
        setups = setups.filter(pl.col("security_id").is_in(list(params["tickers"])))
    zones = _scan("confluence_zones")
    zones = zones.filter(pl.col("as_of_date") == a_of).collect() if zones is not None else pl.DataFrame()
    lv = _scan("tech_levels")
    vp = (lv.filter((pl.col("as_of_date") == a_of) & (pl.col("family") == "volume_profile")).select("security_id", "price").collect()
          if lv is not None else pl.DataFrame(schema={"security_id": pl.Utf8, "price": pl.Float64}))
    env = _scan("conformal_envelopes")
    envd: dict[str, dict[str, Any]] = {}
    if env is not None:
        e = (env.filter((pl.col("as_of_date") <= a_of) & (pl.col("as_of_date") >= a_of - timedelta(days=10)))
             .select("as_of_date", "security_id", "earn_days_ahead", "region").collect()
             .sort("as_of_date").unique("security_id", keep="last"))
        envd = {r["security_id"]: r for r in e.iter_rows(named=True)}
    surface, sfit = latest_surface()
    zm = latest_zone_model(s)
    buckets, bfit = latest_buckets()
    gates = latest_gates() or load_gate(s)
    from app.technicals.data import load_meta  # noqa: PLC0415

    sids = sorted(setups["security_id"].unique().to_list())
    meta = load_meta(sids)
    reg_of = {r["security_id"]: (r.get("region"), r.get("sector_id")) for r in meta.iter_rows(named=True)}
    s_by = {r["security_id"]: r for r in setups.iter_rows(named=True)}
    z_by = {str(k[0]): g for k, g in zones.group_by(["security_id"])} if not zones.is_empty() else {}
    vp_by = {str(k[0]): g["price"].to_numpy() for k, g in vp.group_by(["security_id"])} if not vp.is_empty() else {}
    now = datetime.now(UTC)
    t2_rows: list[dict[str, Any]] = []
    t3_rows: list[dict[str, Any]] = []
    vreg_by: dict[str, int] = {}
    z_feats: list[dict[str, float]] = []
    z_keys: list[tuple] = []
    failed = 0
    start = a_of - timedelta(days=NIGHTLY_HISTORY_DAYS)
    for c0 in range(0, len(sids), NIGHTLY_CHUNK):
        if cancel:
            cancel()
        chunk = sids[c0:c0 + NIGHTLY_CHUNK]
        for si in _load_bars(chunk, start, a_of, s):
            sid = si.bars.security_id
            try:
                b = si.bars
                srow = s_by.get(sid)
                if srow is None or b.n < 60:
                    continue
                if b.dates[-1] != np.datetime64(a_of, "D"):
                    continue                            # bars do not end on the setup date (stale / halted)
                lc = np.log(b.c)
                E = Hh.earnings_flags(b.dates, si.earn)
                r = np.r_[np.nan, np.diff(lc)]
                r[np.abs(np.nan_to_num(r)) > 1.0] = np.nan
                s2a = V.ewma_var(r[:, None], E[:, None])[:, 0]
                Ja = V.jump_var(r[:, None], E[:, None], s2a[:, None])[:, 0]
                s2 = float(s2a[-1])
                if not math.isfinite(s2) or s2 <= 0:
                    continue
                J = float(Ja[-1]) if math.isfinite(Ja[-1]) else 0.0
                vreg = B.vol_regime_last(s2a[np.isfinite(s2a)])
                vreg_by[sid] = vreg
                er = envd.get(sid) or {}
                eda = er.get("earn_days_ahead")
                eda = int(eda) if eda is not None else None
                region = er.get("region") or (reg_of.get(sid) or (None, None))[0]
                Vh = horizon_variances(s2, J, eda)
                adv = Hh._adv(si.dv_usd, b.n - 1)  # noqa: SLF001
                close = float(b.c[-1])
                base = {"as_of_date": a_of, "security_id": sid, "computed_at": now}
                if surface is not None:
                    for row in setup_probabilities(srow, close, Vh, region, vreg, eda, surface, adv, math.sqrt(s2)):
                        H = row["horizon"]
                        t2_rows.append({**base, **row, "bias": srow.get("bias"), "close": close, "entry": srow.get("entry"),
                                        "target1": srow.get("target1"), "invalidation": srow.get("invalidation"),
                                        "reward_risk": srow.get("reward_risk"), "vol_regime": B.VOL_REGIMES[vreg],
                                        "region_pool": region, "fit_date": sfit, "gate": gate_status(gates, f"t2_h{H}")})
                zg = z_by.get(sid)
                if zm is not None and zg is not None and zg.height:
                    st = {"s2": np.r_[s2a[:-1], s2], "vreg": np.full(b.n, vreg), "ke": np.full(b.n, eda if eda else 0)}
                    sd = math.sqrt(s2)
                    kh, mb = math.exp(Hh.HOLD_K * sd), math.exp(Hh.BREAK_M * sd)
                    atr = float(srow.get("atr14") or 0.0)
                    if atr <= 0:
                        continue
                    cx = _ctx_from_setup(srow, atr)
                    feats, keys = [], []
                    for z in zg.iter_rows(named=True):
                        side = 1 if z["side"] == "support" else -1
                        lo, hi = float(z["zone_low"]), float(z["zone_high"])
                        if not (lo > 0 and hi > lo):
                            continue
                        f = Hh.band_features(lo, hi, side, bool(z.get("in_zone")), b.n - 1, b, st, cx, srow,
                                             vp_by.get(sid, np.zeros(0)), kh, mb)
                        f.update(Hh.real_features(z))
                        f["vix_high"] = float(zm.get("vix_high_now", 0.0))
                        f["region_code"] = float(B.region_code(region))
                        feats.append(f)
                        keys.append((int(z["zone_id"]), z["side"], bool(z.get("in_zone")), float(z.get("distance_atr") or 0.0)))
                    for f, k in zip(feats, keys, strict=True):
                        z_feats.append(f)
                        z_keys.append((sid, *k))
            except Exception as e:  # noqa: BLE001
                failed += 1
                log.warning("technicals_probabilities %s failed: %s", sid, e)
    # ---- T3: one batched prediction for every zone of the run
    if zm is not None and z_feats:
        F = pl.DataFrame(z_feats)
        pr = zm["real"].predict(M.matrix(F, zm["real"].cols))
        pc = zm["ctrl"].predict(M.matrix(F, zm["ctrl"].cols))
        g_hold, g_edge = gate_status(gates, "t3_hold"), gate_status(gates, "t3_edge")
        for (sid, zid, side, inz, dist), p1, p0 in zip(z_keys, pr, pc, strict=True):
            fl = M.edge_flag(float(p1 - p0), zm.get("buckets") or [], g_edge == "production")
            t3_rows.append({"as_of_date": a_of, "security_id": sid, "computed_at": now, "zone_id": zid, "side": side,
                            "in_zone": inz, "distance_atr": dist, "p_hold": float(p1), "p_hold_ctrl": float(p0),
                            "edge": float(p1 - p0), "edge_bucket": fl["edge_bucket"], "edge_significant": fl["edge_significant"],
                            "edge_direction": fl["edge_direction"], "edge_ci_lo": (fl["edge_ci"] or [None, None])[0],
                            "edge_ci_hi": (fl["edge_ci"] or [None, None])[1], "edge_realised": fl.get("edge_realised"),
                            "model_kind": zm["real"].kind, "fit_date": zm.get("fit_date"), "gate": g_hold, "gate_edge": g_edge})
        del F, z_feats
    # ---- T4: percentiles within region x sector + bucket stats
    sc = setups.with_columns(pl.col("security_id").map_elements(lambda x: (reg_of.get(x) or ("NA", "NA"))[0] or "NA",
                                                                 return_dtype=pl.Utf8).alias("region"),
                             pl.col("security_id").map_elements(lambda x: (reg_of.get(x) or ("NA", "NA"))[1] or "NA",
                                                                 return_dtype=pl.Utf8).alias("sector_id"),
                             pl.lit(a_of).alias("date"))
    sc = SC.add_percentiles(sc).with_columns(pl.len().over(["region", "sector_id"]).alias("n_peers"))
    t4_rows = []
    btab = {}
    if not buckets.is_empty():
        for (col, h), g in buckets.group_by(["score_col", "horizon"]):
            btab[(col, int(h))] = g.sort("decile").to_dicts()
    for r in sc.iter_rows(named=True):
        row = {"as_of_date": a_of, "security_id": r["security_id"], "region": r["region"], "sector_id": r["sector_id"],
               "technical_score": r["technical_score"], "technical_score_pct": r["technical_score_pct"],
               **{f"pct_{k}": r.get(f"pct_{k}") for k in ("trend", "structure", "location", "rr", "momentum", "technical")},
               **{c: r.get(c) for c in SC.SUBS}, "n_peers": r["n_peers"], "fit_date": bfit, "computed_at": now}
        for h in SC.HORIZONS:
            bk = SC.lookup(r["technical_score"], btab.get(("technical_score", h), []))
            row[f"h{h}_decile"] = bk["decile"] if bk else None
            for k in ("median", "q25", "q75", "hit_rate", "n", "mean"):
                row[f"h{h}_{k}"] = bk.get(k) if bk else None
            row[f"h{h}_median_lo"], row[f"h{h}_median_hi"] = (bk.get("median_lo"), bk.get("median_hi")) if bk else (None, None)
            row[f"h{h}_hit_lo"], row[f"h{h}_hit_hi"] = (bk.get("hit_lo"), bk.get("hit_hi")) if bk else (None, None)
            row[f"h{h}_edge_significant"] = bool(bk.get("edge_significant")) if bk else False
            row[f"h{h}_gate"] = gate_status(gates, f"t4_h{h}")
        row["vol_regime"] = B.VOL_REGIMES[vreg_by[r["security_id"]]] if r["security_id"] in vreg_by else None
        t4_rows.append(row)
    rep: dict[str, Any] = {"as_of": str(a_of), "securities": len(sids), "setups": len({r["security_id"] for r in t2_rows}),
                           "zones": len(t3_rows), "failed": failed, "surface_fit": sfit, "zone_model_fit": (zm or {}).get("fit_date"),
                           "buckets_fit": bfit, "seconds": None}
    if write:
        from app.core.storage import read_table, write_table  # noqa: PLC0415

        for name, rows in (("tech_prob_setups", t2_rows), ("tech_prob_zones", t3_rows), ("tech_prob_scores", t4_rows)):
            if not rows:
                rep[name] = 0
                continue
            df = pl.DataFrame(rows, infer_schema_length=None)
            try:
                keep = month_end_retention(read_table(name), a_of)
            except Exception:  # noqa: BLE001 - first run
                keep = pl.DataFrame()
            if not keep.is_empty():
                df = pl.concat([keep.filter(pl.col("as_of_date") != a_of), df], how="diagonal_relaxed")
            rep[name] = write_table(df, name, mode="overwrite", settings=s)
    else:
        rep.update(tech_prob_setups=len(t2_rows), tech_prob_zones=len(t3_rows), tech_prob_scores=len(t4_rows))
        rep["_rows"] = {"t2": t2_rows, "t3": t3_rows, "t4": t4_rows}
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"technicals_probabilities": {k: v for k, v in rep.items() if k != "_rows"}})
    return rep


# ------------------------------------------------------------------------------------------------ weekly fit


def run_fit_job(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_fit(ctx.settings, ctx.as_of, ctx.params or {}, cancel=ctx.check_cancelled)
    ok = bool(rep.get("surface_rows"))
    return JobResult(status="success" if ok and not rep.get("failed") else ("partial" if ok else "failed"),
                     rows_written={k: v for k, v in rep.items() if k.startswith("tech_prob_") and isinstance(v, int)},
                     entities_ok=rep.get("securities", 0), entities_failed=len(rep.get("failed") or []),
                     watermark=rep.get("fit_date"), message=json.dumps(rep, default=str)[:4000])


def _events_max_date(table: str, region: str) -> date | None:
    lf = _scan(table)
    if lf is None:
        return None
    try:
        return lf.filter(pl.col("region") == region).select(pl.col("date").max()).collect().item()
    except Exception:  # noqa: BLE001
        return None


def _write_events(df: pl.DataFrame, table: str, region: str, origin_from: date | None, s: Settings, replace: bool) -> int:
    """Replace the region's rows with date >= origin_from (or the whole region partition when replace)."""
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    if df.is_empty():
        return 0
    df = df.with_columns(pl.lit(region).alias("region"), pl.lit(datetime.now(UTC)).alias("computed_at"))
    if not replace:
        try:
            old = read_table(table).filter(pl.col("region") == region)
        except Exception:  # noqa: BLE001
            old = pl.DataFrame()
        if not old.is_empty():
            if origin_from is not None:
                old = old.filter(pl.col("date") < origin_from)
            df = pl.concat([old, df], how="diagonal_relaxed")
    return write_table(df, table, mode="overwrite_partition", settings=s)


def _import_seed(seed_dir: Path, s: Settings, regions: list[str] | tuple[str, ...] = FIT_REGIONS) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for rg in regions:
        d = seed_dir / rg
        if not d.exists():
            continue
        for kind, table in (("setups", "tech_prob_setup_events"), ("zones", "tech_prob_zone_events")):
            files = sorted(d.glob(f"b*_{kind}.parquet"))
            if not files:
                continue
            df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
            out[f"{table}:{rg}"] = _write_events(df, table, rg, None, s, replace=True)
            del df
    return out


def _seed_counts(seed_dir: Path, regions: list[str] | tuple[str, ...]) -> dict[str, B.GridCounts]:
    out: dict[str, B.GridCounts] = {}
    for rg in regions:
        gc = None
        for f in sorted((seed_dir / rg).glob("b*_counts.npz")):
            z = np.load(f)
            if gc is None:
                gc = B.GridCounts(rg, z["C"].astype(np.float64), z["N"].astype(np.float64))
            else:
                gc.C += z["C"]
                gc.N += z["N"]
        if gc is not None:
            out[rg] = gc
    return out


def run_fit(settings: Settings | None, as_of: date | None, params: dict[str, Any], cancel: Any = None) -> dict[str, Any]:
    from app.conformal import data as D  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415
    from app.technicals.parallel import pmap  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now(UTC).date()
    mode = str(params.get("mode", "incremental"))
    regions = list(params.get("regions") or FIT_REGIONS)
    workers = int(params.get("workers", 4))
    batch = int(params.get("batch", 20))
    rep: dict[str, Any] = {"as_of": str(as_of), "mode": mode, "regions": {}, "failed": []}
    seeded = False
    if mode == "seed":
        sd = Path(params.get("seed_dir") or (s.project_root / "scratchpad" / "tech_v2_models" / "wf"))
        rep["seed"] = _import_seed(sd, s, regions)
        seeded = True
    vix = D.vix_daily(date(2002, 1, 1), as_of, s)
    vd = np.array(vix["date"].to_list(), dtype="datetime64[D]")
    vv = vix["vix"].to_numpy()
    counts: dict[str, B.GridCounts] = {}
    total = 0
    if seeded:
        # seed: the walk-forward build already holds the grid counts of every daily origin -> no price reprocessing
        counts = _seed_counts(sd, regions)
        rep["seed_counts"] = {rg: float(gc.N.sum()) for rg, gc in counts.items()}
    for rg in ([] if seeded else regions):
        if cancel:
            cancel()
        meta = D.universe_ids(REGION_UNIVERSES[rg], s)
        items = Hh.eligible_ids(sorted(meta["security_id"].to_list()), rg, Hh.PRICE_START, as_of)
        last = _events_max_date("tech_prob_setup_events", rg)
        if mode == "full" or last is None:
            origin_from = None
        else:
            origin_from = last - timedelta(days=REFRESH_SESSIONS_DAYS)
        batches = [items[i:i + batch] for i in range(0, len(items), batch)]
        from functools import partial  # noqa: PLC0415

        res = pmap(partial(Hh.batch_task_ids, region=rg, vix_d=vd, vix_v=vv, start=Hh.PRICE_START, end=as_of,
                           origin_from=origin_from), batches, workers=workers, cancel=cancel,
                   mode="process" if len(batches) > 1 else "thread", chunk=1)
        gc = B.GridCounts.empty(rg)
        S_fr, Z_fr = [], []
        for r in res:
            gc.add(r["counts"])
            if r["setups"].height:
                S_fr.append(r["setups"])
            if r["zones"].height:
                Z_fr.append(r["zones"])
            rep["failed"] += r["failed"]
        counts[rg] = gc
        S_df = pl.concat(S_fr, how="diagonal_relaxed") if S_fr else pl.DataFrame()
        Z_df = pl.concat(Z_fr, how="diagonal_relaxed") if Z_fr else pl.DataFrame()
        n1 = _write_events(S_df, "tech_prob_setup_events", rg, origin_from, s, replace=origin_from is None and not seeded)
        n2 = _write_events(Z_df, "tech_prob_zone_events", rg, origin_from, s, replace=origin_from is None and not seeded)
        rep["regions"][rg] = {"securities": len(items), "origin_from": str(origin_from) if origin_from else None,
                              "setup_rows": n1, "zone_rows": n2}
        total += len(items)
        del res, S_fr, Z_fr, S_df, Z_df
    rep["securities"] = total
    fit_date = as_of
    rep["fit_date"] = str(fit_date)
    # ---- T2 surface
    q_end = int(B.quarter_index(np.array([np.datetime64(as_of, "D")]))[0]) + 1
    surf = B.fit_surface(counts, 0, q_end)
    srows = B.surface_rows(surf, fit_date)
    from app.core.storage import read_table  # noqa: PLC0415

    old = pl.DataFrame()
    try:
        old = read_table("tech_prob_surfaces")
    except Exception:  # noqa: BLE001
        pass
    sdf = pl.DataFrame(srows, infer_schema_length=None).with_columns(pl.lit(datetime.now(UTC)).alias("computed_at"))
    if not old.is_empty():
        keep_dates = sorted(set(old["fit_date"].to_list()) - {fit_date})[-(SURFACE_KEEP - 1):]
        sdf = pl.concat([old.filter(pl.col("fit_date").is_in(keep_dates)), sdf], how="diagonal_relaxed")
    rep["surface_rows"] = write_table(sdf, "tech_prob_surfaces", mode="overwrite", settings=s)
    gate = load_gate(s)
    # ---- T3 zone model
    rep["zone_model"] = fit_zone_model(s, fit_date, gate)
    # ---- T4 buckets
    rep["tech_prob_score_buckets"] = fit_score_buckets(s, fit_date, gate)
    # ---- gates
    rep["tech_prob_gates"] = write_gates(s, fit_date, gate)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    ST.update(s, jobs={"technicals_probabilities_fit": {k: v for k, v in rep.items() if k != "regions"}})
    return rep


def fit_zone_model(s: Settings, fit_date: date, gate: dict[str, Any], max_train: int = 1_200_000) -> dict[str, Any]:
    lf = _scan("tech_prob_zone_events")
    if lf is None:
        return {"note": "no zone events"}
    cols = list(dict.fromkeys([*M.REAL, "date", "is_control", "hold", "touched", "complete", "region"]))
    have = lf.collect_schema().names()
    df = lf.select([c for c in cols if c in have and c != "region_code"]).filter(pl.col("touched") & pl.col("complete")).collect()
    if df.height < 20000:
        return {"note": f"too few zone events ({df.height})"}
    d = df["date"].to_numpy().astype("datetime64[D]")
    ctrl = df["is_control"].to_numpy().astype(bool)
    y = df["hold"].to_numpy().astype(np.float64)
    X = M.matrix(df, M.REAL)
    nb = len(M.BAND)
    del df
    end = np.datetime64(fit_date, "D")
    ca = (d >= end - np.timedelta64(365, "D")) & (d < end - np.timedelta64(45, "D"))
    tr = d < end - np.timedelta64(410, "D")
    kind = ((gate.get("t3") or {}).get("chosen")) or "hgb"
    out: dict[str, Any] = {"fit_date": str(fit_date), "kind": kind}
    models = {}
    for nm, msk_tr, msk_ca, cols_, Xs in (("real", tr & ~ctrl, ca & ~ctrl, M.REAL, X), ("ctrl", tr & ctrl, ca & ctrl, M.BAND, X[:, :nb])):
        itr = np.nonzero(msk_tr)[0]
        itr = itr[M.subsample(len(itr), max_train, seed=0)]
        ica = np.nonzero(msk_ca)[0]
        models[nm] = M.fit(kind, Xs[itr], y[itr], Xs[ica], y[ica], cols_)
        out[f"{nm}_train"] = int(len(itr))
        out[f"{nm}_cal"] = int(len(ica))
    vix_now = 0.0
    try:
        from app.conformal import data as D  # noqa: PLC0415

        vx = D.vix_daily(fit_date - timedelta(days=10), fit_date, s)
        vix_now = float(vx["vix"].to_numpy()[-1] > 25.0) if vx.height else 0.0
    except Exception:  # noqa: BLE001
        pass
    art = {"fit_date": str(fit_date), "real": models["real"], "ctrl": models["ctrl"],
           "buckets": ((gate.get("t3") or {}).get("buckets") or []), "gate": gate.get("outputs", {}).get("t3_hold"),
           "vix_high_now": vix_now, "params": {"hold_k": Hh.HOLD_K, "break_m": Hh.BREAK_M, "touch_h": Hh.TOUCH_H,
                                               "resolve_r": Hh.RESOLVE_R}}
    d_out = model_root(s) / fit_date.isoformat()
    d_out.mkdir(parents=True, exist_ok=True)
    with open(d_out / "zone_model.pkl", "wb") as f:
        pickle.dump(art, f)
    (d_out / "meta.json").write_text(json.dumps({k: v for k, v in out.items()}, indent=1, default=str), encoding="utf-8")
    # keep the last 6 fits
    dirs = sorted(p for p in model_root(s).iterdir() if p.is_dir())
    for p in dirs[:-6]:
        for x in p.iterdir():
            x.unlink()
        p.rmdir()
    return out


def fit_score_buckets(s: Settings, fit_date: date, gate: dict[str, Any]) -> int:
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    lf = _scan("tech_prob_setup_events")
    if lf is None:
        return 0
    df = lf.select("security_id", "date", "region", "sector_id", "technical_score", *SC.SUBS,
                   *[f"fwd_{h}" for h in SC.HORIZONS]).collect()
    df = SC.add_percentiles(SC.add_excess(df))
    rows = []
    edge = ((gate.get("t4") or {}).get("edge")) or {}
    stats = ((gate.get("t4") or {}).get("ic")) or {}
    for col in ("technical_score",):
        for h in SC.HORIZONS:
            sub = df.filter(pl.col(f"exc_{h}").is_not_null() & pl.col(col).is_not_null())
            if sub.height < 1000:
                continue
            x = sub[col].to_numpy()
            edges = SC.decile_edges(x)
            tab = SC.bucket_table(x, sub[f"exc_{h}"].to_numpy(), sub["date"].to_numpy().astype("datetime64[D]"), edges)
            es = bool(edge.get(f"{col}:{h}", False))
            ic = stats.get(f"{col}:{h}") or {}
            for r in tab:
                rows.append({"fit_date": fit_date, "score_col": col, "horizon": h, "decile": r["decile"], "score_lo": r.get("score_lo"),
                             "score_hi": r.get("score_hi"), "n": r["n"], "median": r.get("median"), "q25": r.get("q25"),
                             "q75": r.get("q75"), "mean": r.get("mean"), "hit_rate": r.get("hit_rate"),
                             "median_lo": (r.get("median_ci") or [None, None])[0], "median_hi": (r.get("median_ci") or [None, None])[1],
                             "hit_lo": (r.get("hit_ci") or [None, None])[0], "hit_hi": (r.get("hit_ci") or [None, None])[1],
                             "edge_significant": es, "ic_mean": ic.get("mean_ic"), "ic_t": ic.get("t_stat"),
                             "computed_at": datetime.now(UTC)})
    if not rows:
        return 0
    new = pl.DataFrame(rows, infer_schema_length=None)
    try:
        old = read_table("tech_prob_score_buckets")
        keep = sorted(set(old["fit_date"].to_list()) - {fit_date})[-(SURFACE_KEEP - 1):]
        new = pl.concat([old.filter(pl.col("fit_date").is_in(keep)), new], how="diagonal_relaxed")
    except Exception:  # noqa: BLE001
        pass
    return write_table(new, "tech_prob_score_buckets", mode="overwrite", settings=s)


def write_gates(s: Settings, fit_date: date, gate: dict[str, Any]) -> int:
    from app.core.storage import read_table, write_table  # noqa: PLC0415

    rows = []
    for o in OUTPUTS:
        g = (gate.get("outputs") or {}).get(o) or {}
        rows.append({"fit_date": fit_date, "output": o, "verdict": g.get("verdict") or "not_validated",
                     "shown_as": "production" if g.get("verdict") == "pass" else "experimental",
                     "ece": g.get("ece"), "max_regime_gap": g.get("max_regime_gap"), "beats_baselines": g.get("beats_baselines"),
                     "edge_significant": g.get("edge_significant"), "detail": json.dumps(g, default=str),
                     "validated_at": gate.get("generated_at"), "computed_at": datetime.now(UTC)})
    new = pl.DataFrame(rows, infer_schema_length=None)
    try:
        old = read_table("tech_prob_gates")
        keep = sorted(set(old["fit_date"].to_list()) - {fit_date})[-(SURFACE_KEEP - 1):]
        new = pl.concat([old.filter(pl.col("fit_date").is_in(keep)), new], how="diagonal_relaxed")
    except Exception:  # noqa: BLE001
        pass
    return write_table(new, "tech_prob_gates", mode="overwrite", settings=s)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit", action="store_true")
    ap.add_argument("--mode", default="incremental")
    ap.add_argument("--seed-dir")
    ap.add_argument("--as-of")
    ap.add_argument("--regions")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--tickers")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    as_of = date.fromisoformat(a.as_of) if a.as_of else None
    if a.fit:
        p = {"mode": a.mode, "workers": a.workers}
        if a.seed_dir:
            p["seed_dir"] = a.seed_dir
        if a.regions:
            p["regions"] = a.regions.split(",")
        rep = run_fit(None, as_of, p)
    else:
        p = {"tickers": a.tickers.split(",")} if a.tickers else {}
        rep = run_nightly(None, as_of, p, write=not a.dry_run)
        rep.pop("_rows", None)
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
