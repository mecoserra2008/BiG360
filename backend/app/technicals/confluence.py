"""Confluence zones, trade setups and the technical score — nightly `confluence_zones` job (owner: technicals).

Per security, for the latest as-of date:

1. Take the active levels from tech_levels (already weighted: reliability x recency x touches x timeframe x volume
   node strength x role-flip, see weights.py). Levels at the *identical* price (the same pivot seen by several swing
   orders, floor R2 == Fibonacci R3, ...) are merged: weight = max + 0.25 x (sum - max), so one pivot is not counted 6x.
2. Weighted Gaussian KDE in ATR-normalized space x = (price - close) / ATR14, bandwidth 0.35 ATR, over |x| <= 12.
3. Zones = density peaks >= 15% of the highest peak, bounded where density falls to 50% of the peak, at the valley
   to a neighbouring peak, or at +-0.75 ATR from the peak. Levels whose type tests clearly worse than random for the
   stock (type_rank_score < 0.4) are left out. Evidence S = weight_sum x diversity, diversity = 1 + 0.2 x (distinct
   families - 1); strength = 100 x S / (S + 20) (S = 20 was the median zone of the first live run -> 50).
   Zones keep their member levels (keys, prices, weights) for explainability.
4. Each zone carries its own plan (support zone -> long: entry at the zone top, invalidation 0.5 ATR under the zone,
   target = nearest resistance zone center above price; mirrored for resistance zones).
5. tech_setups: trend (SMA50/200), market structure (ZigZag BOS/CHoCH), bias, the setup around the nearest strong
   support/resistance, R:R, and technical_score (0..100, long-side attractiveness):
       25% trend + 20% structure + 35% location vs zones + 10% reward/risk + 10% momentum (RSI not over-extended).
"""

from __future__ import annotations

import math
import time
from functools import partial
from dataclasses import dataclass
from datetime import UTC, date, datetime

import numpy as np
import orjson
import polars as pl

from app.core.logsetup import get_logger
from app.scheduler.base import JobContext, JobResult

from . import kernels as K
from .data import Bars

log = get_logger(__name__)


@dataclass(frozen=True)
class ZoneParams:
    bandwidth: float = 0.35
    max_atr: float = 12.0
    grid_step: float = 0.05
    min_peak_frac: float = 0.15
    bound_frac: float = 0.5
    max_half_width: float = 0.75
    min_type_score: float = 0.4
    strength_half: float = 20.0   # evidence S at which strength = 50 (median zone of the first live run)
    diversity_step: float = 0.2
    dup_extra: float = 0.25
    strong: float = 40.0
    stop_buffer_atr: float = 0.5


DEFAULT_ZP = ZoneParams()


# ------------------------------------------------------------------------------------------------ zones

def merge_identical(levels: pl.DataFrame, dup_extra: float = DEFAULT_ZP.dup_extra) -> pl.DataFrame:
    """Collapse levels at the identical price (rel 1e-7): weight = max + dup_extra x (sum - max). Keeps every key."""
    if levels.is_empty():
        return levels
    lv = levels.with_columns((pl.col("price") * 1e7).round(0).alias("_pk"))
    return (lv.group_by("_pk", maintain_order=True).agg(
        pl.col("price").first(),
        (pl.col("weight").max() + dup_extra * (pl.col("weight").sum() - pl.col("weight").max())).alias("weight"),
        pl.col("level_key"), pl.col("level_type"), pl.col("family"),
        pl.col("weight").alias("weights"),
    ).drop("_pk"))


def find_zones(levels: pl.DataFrame, close: float, atr: float, zp: ZoneParams = DEFAULT_ZP) -> list[dict]:
    """KDE confluence zones from a frame with price, weight, level_key, level_type, family."""
    if levels.is_empty() or not (atr > 0) or not (close > 0):
        return []
    lv = levels.filter(pl.col("weight").is_finite() & (pl.col("weight") > 0))
    if "type_rank_score" in lv.columns:  # drop level types that test clearly worse than random levels for this stock
        lv = lv.filter(pl.col("type_rank_score").is_null() | (pl.col("type_rank_score") >= zp.min_type_score))
    m = merge_identical(lv, zp.dup_extra)
    if m.is_empty():
        return []
    x = (m["price"].to_numpy() - close) / atr
    w = m["weight"].to_numpy().astype(np.float64)
    keep = np.abs(x) <= zp.max_atr
    if not keep.any():
        return []
    m = m.filter(pl.Series(keep))
    x, w = x[keep], w[keep]
    grid = np.arange(-zp.max_atr - 1.0, zp.max_atr + 1.0 + 1e-9, zp.grid_step)
    dens = K.weighted_kde(x, w, grid, zp.bandwidth)
    dmax = dens.max()
    if not dmax > 0:
        return []
    pk = np.flatnonzero((dens[1:-1] >= dens[:-2]) & (dens[1:-1] > dens[2:]) & (dens[1:-1] >= zp.min_peak_frac * dmax)) + 1
    keys = m["level_key"].to_list()
    types = m["level_type"].to_list()
    fams = m["family"].to_list()
    wlists = m["weights"].to_list()
    prices = m["price"].to_numpy()
    hw = int(round(zp.max_half_width / zp.grid_step))
    zones: list[dict] = []
    for j in pk:
        thr = zp.bound_frac * dens[j]
        lo = j
        while lo > max(0, j - hw) and dens[lo - 1] >= thr and not (dens[lo - 1] > dens[lo] and lo < j):
            lo -= 1
        hi = j
        while hi < min(grid.shape[0] - 1, j + hw) and dens[hi + 1] >= thr and not (dens[hi + 1] > dens[hi] and hi > j):
            hi += 1
        xl, xh = grid[lo] - zp.grid_step / 2, grid[hi] + zp.grid_step / 2
        mem = np.flatnonzero((x >= xl) & (x <= xh))
        if mem.size == 0:
            continue
        mk, mt, mf, mp, mw = [], [], [], [], []
        for i in mem:
            for k_, t_, f_, w_ in zip(keys[i], types[i], fams[i], wlists[i], strict=True):
                mk.append(k_); mt.append(t_); mf.append(f_); mp.append(float(prices[i])); mw.append(float(w_))  # noqa: E702
        wsum = float(w[mem].sum())
        nfam = len(set(mf))
        div = 1.0 + zp.diversity_step * (nfam - 1)
        ev = wsum * div
        strength = 100.0 * ev / (ev + zp.strength_half)
        center = close + grid[j] * atr
        zl, zh = close + xl * atr, close + xh * atr
        zones.append({
            "center": center, "zone_low": zl, "zone_high": zh, "density": float(dens[j] / dmax),
            "strength": strength, "distance_atr": float(grid[j]), "distance_pct": center / close - 1.0,
            "side": "support" if center < close else "resistance", "in_zone": bool(zl <= close <= zh),
            "member_level_keys": mk, "member_level_types": sorted(set(mt)), "member_families": sorted(set(mf)),
            "member_prices": mp, "member_weights": mw, "member_count": len(mk), "n_families": nfam, "weight_sum": wsum,
        })
    zones.sort(key=lambda z: -z["strength"])
    for i, z in enumerate(zones, 1):
        z["zone_id"] = i
        z["rank"] = i
    _attach_plans(zones, close, atr, zp)
    return zones


def _attach_plans(zones: list[dict], close: float, atr: float, zp: ZoneParams) -> None:
    sup = sorted([z for z in zones if z["side"] == "support"], key=lambda z: -z["center"])  # nearest first
    res = sorted([z for z in zones if z["side"] == "resistance"], key=lambda z: z["center"])
    for z in zones:
        if z["side"] == "support":
            entry = min(z["zone_high"], close)
            inval = z["zone_low"] - zp.stop_buffer_atr * atr
            tgt = next((r["center"] for r in res if r["center"] > max(entry, z["zone_high"])), None)
            z.update(entry=entry, invalidation=inval, target=tgt,
                     reward_risk=(tgt - entry) / (entry - inval) if tgt is not None and entry > inval else None)
        else:
            entry = max(z["zone_low"], close)
            inval = z["zone_high"] + zp.stop_buffer_atr * atr
            tgt = next((s["center"] for s in sup if s["center"] < min(entry, z["zone_low"])), None)
            z.update(entry=entry, invalidation=inval, target=tgt,
                     reward_risk=(entry - tgt) / (inval - entry) if tgt is not None and inval > entry else None)


# ------------------------------------------------------------------------------------------------ context + setup

def context(b: Bars, zigzag_mult: float = 3.0) -> dict:
    """Trend / momentum / market-structure context from the bars (last bar = as-of)."""
    atr = K.atr_wilder(b.h, b.l, b.c, 14)
    rsi = K.rsi_wilder(b.c, 14)
    s50 = K.sma_prior(b.c, 50)   # [t] = mean of bars < t -> index n = mean of the last 50 bars
    s200 = K.sma_prior(b.c, 200)
    n = b.n
    close = float(b.c[-1])
    sma50 = float(s50[n])
    sma200 = float(s200[n])
    slope50 = float(s50[n] - s50[n - 20]) if n > 70 else float("nan")
    pidx, ppx, pdir, pconf = K.zigzag(b.h, b.l, atr, zigzag_mult)
    bb, bp, bk, bd, bi, trend_arr = K.structure_breaks(b.c, pidx, ppx, pdir, pconf)
    st = int(trend_arr[-1]) if trend_arr.size else 0
    ctx = {
        "close": close, "atr": float(atr[-1]), "rsi": float(rsi[-1]), "sma50": sma50, "sma200": sma200, "slope50": slope50,
        "structure_state": {1: "bullish", -1: "bearish"}.get(st, "range"),
        "last_break_kind": None, "last_break_dir": None, "last_break_date": None, "last_break_price": None,
    }
    if bb.size:
        ctx.update(last_break_kind="bos" if bk[-1] == 1 else "choch", last_break_dir="up" if bd[-1] == 1 else "down",
                   last_break_date=b.dates[bb[-1]].astype("datetime64[D]").item(), last_break_price=float(bp[-1]))
    ctx["pivots"] = [(b.dates[i].astype("datetime64[D]").item(), float(px), "high" if d == 1 else "low")
                     for i, px, d in zip(pidx[-12:], ppx[-12:], pdir[-12:], strict=True)]
    return ctx


def _nz(x: float | None) -> bool:
    return x is not None and isinstance(x, float) and math.isfinite(x)


def trend_score(c: dict) -> tuple[float, str]:
    conds = []
    if _nz(c["sma200"]):
        conds += [c["close"] > c["sma200"]]
        if _nz(c["sma50"]):
            conds += [c["sma50"] > c["sma200"]]
    if _nz(c["sma50"]):
        conds += [c["close"] > c["sma50"]]
    if _nz(c["slope50"]):
        conds += [c["slope50"] > 0]
    if not conds:
        return 50.0, "sideways"
    s = 100.0 * sum(conds) / len(conds)
    return s, ("up" if s >= 75 else "down" if s <= 25 else "sideways")


def momentum_score(rsi: float) -> float:
    if not _nz(rsi):
        return 50.0
    return float(np.clip(100.0 - 4.0 * max(0.0, rsi - 65.0) - 2.0 * max(0.0, 40.0 - rsi), 0.0, 100.0))


def _nearest(zones: list[dict], side: str, strong: float) -> dict | None:
    cand = [z for z in zones if z["side"] == side]
    if not cand:
        return None
    strong_c = [z for z in cand if z["strength"] >= strong]
    pool = strong_c or [max(cand, key=lambda z: z["strength"])]
    return min(pool, key=lambda z: abs(z["distance_atr"]))


def build_setup(zones: list[dict], c: dict, zp: ZoneParams = DEFAULT_ZP) -> dict:
    """TradeSetup + technical score (see module docstring)."""
    close, atr = c["close"], c["atr"]
    s_trend, trend = trend_score(c)
    s_struct = {"bullish": 100.0, "bearish": 0.0}.get(c["structure_state"], 50.0)
    sup = _nearest(zones, "support", zp.strong)
    res = _nearest(zones, "resistance", zp.strong)

    def dist_below(z):  # close -> top of support (0 when inside)
        return max(0.0, (close - z["zone_high"]) / atr) if z else None

    def dist_above(z):
        return max(0.0, (z["zone_low"] - close) / atr) if z else None

    d_s, d_r = dist_below(sup), dist_above(res)
    a = (sup["strength"] / 100.0) * math.exp(-d_s / 2.0) if sup else 0.0
    bq = (res["strength"] / 100.0) * math.exp(-d_r / 2.0) if res else 0.0
    s_loc = float(np.clip(50.0 + 50.0 * (a - bq), 0.0, 100.0))
    s_mom = momentum_score(c["rsi"])
    direction = 0.55 * s_trend + 0.45 * s_struct
    bias = "long" if direction >= 60 else "short" if direction <= 35 else "neutral"

    res_sorted = sorted([z for z in zones if z["side"] == "resistance"], key=lambda z: z["center"])
    sup_sorted = sorted([z for z in zones if z["side"] == "support"], key=lambda z: -z["center"])
    out: dict = {"bias": bias, "trend": trend, "entry_low": None, "entry_high": None, "entry": None, "invalidation": None,
                 "target1": None, "target2": None, "reward_risk": None}
    note_tgt = ""
    if bias in ("long", "neutral") and sup:
        entry = min(sup["zone_high"], close)
        inval = sup["zone_low"] - zp.stop_buffer_atr * atr
        ups = [z for z in res_sorted if z["center"] > max(entry, sup["zone_high"]) and z["strength"] >= 0.5 * zp.strong]
        t1 = ups[0]["center"] if ups else None
        t2 = ups[1]["center"] if len(ups) > 1 else None
        if t1 is None:
            t1 = entry + 3.0 * atr
            note_tgt = " (no overhead zone: 3 ATR projection)"
        out.update(entry_low=sup["zone_low"], entry_high=min(sup["zone_high"], close), entry=entry, invalidation=inval,
                   target1=t1, target2=t2, reward_risk=(t1 - entry) / (entry - inval) if entry > inval else None)
        anchor = sup
    elif bias == "short" and res:
        entry = max(res["zone_low"], close)
        inval = res["zone_high"] + zp.stop_buffer_atr * atr
        dns = [z for z in sup_sorted if z["center"] < min(entry, res["zone_low"]) and z["strength"] >= 0.5 * zp.strong]
        t1 = dns[0]["center"] if dns else entry - 3.0 * atr
        t2 = dns[1]["center"] if len(dns) > 1 else None
        if not dns:
            note_tgt = " (no zone below: 3 ATR projection)"
        out.update(entry_low=max(res["zone_low"], close), entry_high=res["zone_high"], entry=entry, invalidation=inval,
                   target1=t1, target2=t2, reward_risk=(entry - t1) / (inval - entry) if inval > entry else None)
        anchor = res
    else:
        anchor = None
    rr = out["reward_risk"]
    s_rr = float(np.clip((rr or 0.0) / 4.0, 0.0, 1.0) * 100.0) if bias != "short" else 0.0
    score = 0.25 * s_trend + 0.20 * s_struct + 0.35 * s_loc + 0.10 * s_rr + 0.10 * s_mom
    out.update(
        technical_score=float(score), score_trend=s_trend, score_structure=s_struct, score_location=s_loc,
        score_rr=s_rr, score_momentum=s_mom,
        support_zone_id=sup["zone_id"] if sup else None, resistance_zone_id=res["zone_id"] if res else None,
        support_low=sup["zone_low"] if sup else None, support_high=sup["zone_high"] if sup else None,
        resistance_low=res["zone_low"] if res else None, resistance_high=res["zone_high"] if res else None,
        dist_support_atr=d_s, dist_resistance_atr=d_r,
        dist_support_pct=(sup["zone_high"] / close - 1.0) if sup else None,
        dist_resistance_pct=(res["zone_low"] / close - 1.0) if res else None,
    )
    comp = {
        "weights": {"trend": 0.25, "structure": 0.20, "location": 0.35, "rr": 0.10, "momentum": 0.10},
        "inputs": {"close": close, "atr14": atr, "sma50": c["sma50"], "sma200": c["sma200"], "slope50_20d": c["slope50"],
                   "rsi14": c["rsi"], "structure_state": c["structure_state"],
                   "support": {"zone_id": out["support_zone_id"], "strength": sup["strength"] if sup else None, "dist_atr": d_s},
                   "resistance": {"zone_id": out["resistance_zone_id"], "strength": res["strength"] if res else None, "dist_atr": d_r}},
    }
    out["score_components"] = orjson.dumps(comp, option=orjson.OPT_SERIALIZE_NUMPY).decode()
    out["note"] = _note(out, anchor, c, note_tgt)
    return out


def _fmt(x: float | None) -> str:
    if x is None or not math.isfinite(x):
        return "n/a"
    return f"{x:,.4g}" if abs(x) < 1000 else f"{x:,.0f}"


def _note(s: dict, z: dict | None, c: dict, extra: str) -> str:
    ctxt = f"Trend {s['trend']}, structure {c['structure_state']}"
    if c.get("last_break_kind"):
        ctxt += f" (last {c['last_break_kind'].upper()} {c['last_break_dir']} {c['last_break_date']})"
    if z is None or s["entry"] is None:
        return f"No actionable zone. {ctxt}."
    top = ", ".join(sorted(set(z["member_level_keys"]), key=z["member_level_keys"].index)[:4])
    verb = "Buy" if s["bias"] != "short" else "Sell"
    rr = f"{s['reward_risk']:.1f}" if s["reward_risk"] is not None else "n/a"
    return (f"{s['bias'].capitalize()}: {verb} {_fmt(s['entry_low'])}-{_fmt(s['entry_high'])} (zone #{z['zone_id']}, strength "
            f"{z['strength']:.0f}: {top}), invalidation {_fmt(s['invalidation'])}, T1 {_fmt(s['target1'])}{extra}, "
            f"R:R {rr}. {ctxt}.")


# ------------------------------------------------------------------------------------------------ per security + job

def confluence_for_security(levels: pl.DataFrame, b: Bars, as_of: date, zp: ZoneParams = DEFAULT_ZP) -> tuple[pl.DataFrame, dict]:
    c = context(b)
    zones = find_zones(levels, c["close"], c["atr"], zp)
    setup = build_setup(zones, c, zp)
    sid = b.security_id
    now = datetime.now(UTC)
    zdf = pl.DataFrame([{k: v for k, v in z.items()} for z in zones]) if zones else pl.DataFrame()
    if not zdf.is_empty():
        zdf = zdf.with_columns(pl.lit(as_of).alias("as_of_date"), pl.lit(sid).alias("security_id"),
                               pl.lit(c["atr"]).alias("atr14"), pl.lit(c["close"]).alias("last_close"),
                               pl.lit(now).alias("computed_at"))
    row = {"as_of_date": as_of, "security_id": sid, "last_close": c["close"], "atr14": c["atr"], "rsi14": c["rsi"],
           "sma50": c["sma50"], "sma200": c["sma200"], "structure_state": c["structure_state"],
           "last_break_kind": c["last_break_kind"], "last_break_dir": c["last_break_dir"],
           "last_break_date": c["last_break_date"], "last_break_price": c["last_break_price"], "computed_at": now, **setup}
    for k in ("rsi14", "sma50", "sma200"):
        if row[k] is not None and not math.isfinite(row[k]):
            row[k] = None
    return zdf, row


def _conf_task(item: tuple[Bars, pl.DataFrame], as_of: date):
    b, lv = item
    try:
        return confluence_for_security(lv, b, as_of)
    except Exception as e:  # noqa: BLE001
        log.warning("confluence %s failed: %s", b.security_id, e)
        return None


def run(ctx: JobContext) -> JobResult:
    """Nightly confluence_zones + tech_setups from the tech_levels snapshot of ctx.as_of."""
    from app.core.schema import get_contract
    from app.core.storage import read_table, scan_table, write_table

    from .data import iter_bars, load_meta, load_price_panel, years_before
    from .levels import month_end_retention
    from .parallel import pmap
    from .weights import level_weight_expr

    t0 = time.perf_counter()
    lf = scan_table("tech_levels").filter(pl.col("as_of_date") <= ctx.as_of)
    as_of = lf.select(pl.col("as_of_date").max()).collect().item()
    if as_of is None:
        return JobResult(status="success", message="tech_levels is empty")
    levels = lf.filter(pl.col("as_of_date") == as_of).collect()
    if ctx.params.get("tickers"):
        levels = levels.filter(pl.col("security_id").is_in(list(ctx.params["tickers"])))
    if levels["weight"].null_count() == levels.height:
        levels = levels.with_columns(level_weight_expr(as_of).alias("weight"))
    source = str(ctx.params.get("source", "prices_daily"))
    sids = sorted(levels["security_id"].unique().to_list())
    by_sid = {k[0]: g for k, g in levels.group_by(["security_id"])}
    meta = load_meta(sids)
    workers = ctx.params.get("workers")
    zparts: list[pl.DataFrame] = []
    srows: list[dict] = []
    failed = 0
    for i in range(0, len(sids), 1000):
        ctx.check_cancelled()
        chunk = sids[i:i + 1000]
        panel = load_price_panel(chunk, start=years_before(as_of, 2.5), end=as_of, source=source)
        items = [(b, by_sid[b.security_id]) for b in iter_bars(panel, meta, min_bars=60)]
        for r in pmap(partial(_conf_task, as_of=as_of), items, workers=workers, cancel=ctx.check_cancelled):
            if r is None:
                failed += 1
                continue
            if not r[0].is_empty():
                zparts.append(r[0])
            srows.append(r[1])
    zc = get_contract("confluence_zones").column_names
    sc = get_contract("tech_setups").column_names
    written = {}
    if zparts:
        z = pl.concat(zparts, how="diagonal_relaxed")
        z = z.select([c for c in zc if c in z.columns])
        keep = month_end_retention(read_table("confluence_zones"), as_of)
        if not keep.is_empty():
            z = pl.concat([keep.filter(pl.col("as_of_date") != as_of), z], how="diagonal_relaxed")
        written["confluence_zones"] = write_table(z, "confluence_zones", mode="overwrite")
    if srows:
        s = pl.DataFrame(srows, infer_schema_length=None)
        s = s.select([c for c in sc if c in s.columns])
        keep = month_end_retention(read_table("tech_setups"), as_of)
        if not keep.is_empty():
            s = pl.concat([keep.filter(pl.col("as_of_date") != as_of), s], how="diagonal_relaxed")
        written["tech_setups"] = write_table(s, "tech_setups", mode="overwrite")
    ctx.log.info("confluence: %d securities, %d zones in %.1fs", len(srows), sum(p.height for p in zparts), time.perf_counter() - t0)
    return JobResult(status="partial" if failed else "success", rows_written=written, entities_ok=len(srows), entities_failed=failed)

