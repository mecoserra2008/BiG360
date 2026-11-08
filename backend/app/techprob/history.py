"""Point-in-time reconstruction of the Technicals layer at historical origins + realised outcomes (T2-T4 data).

Per security (own print sessions, split + dividend adjusted bars from prices_daily):

* Volatility: the conformal envelope's EWMA filter (lambda 0.94, earnings reaction days excluded) and earnings jump
  variance J (app.conformal.envelope), V(H) = H s2 + J 1[earnings day within H], vol regime (barriers.vol_regime).
  Earnings days are realised earnings_events reaction dates used as if pre-announced (as in the conformal gate).
* T2 grid counts: every daily origin adds its realised two-barrier outcomes on the whole (a, b) grid
  (barriers.accumulate).
* Weekly origins (last own session of each ISO week, from ORIGIN_START): levels from ONE build_levels over the full
  history, viewed point-in-time with levels.snapshot(at=i+1) (instances with start <= i+1 < end, series values that
  use bars <= i, touch counts on bars <= i; verified identical to a rebuild on truncated bars except the 0-4 bar
  refresh phase of volume-profile levels), weighted with level_weight_expr at the origin date (type_rank_score
  neutral: level_type_stats has no point-in-time history before 2026 - documented deviation), then the live
  confluence code: context(bars <= i), find_zones, build_setup.
  - setup rows: the setup, the score and sub-scores, barrier distances and the realised outcome per horizon,
    forward 5/20/60-session log returns (T4);
  - zone rows: zones within MAX_ZONE_ATR of the close that are touched within TOUCH_H sessions, with the T3
    features and the hold / break label, plus one random control band per real zone (same width, centre shifted
    by +-U(0.75, 2.5) ATR as in the level_type_backtest control), labelled by the same rule.

Hold / break (T3, sigma = sigma_d at the origin, multiplicative in price):
  touch    first session in (t, t + TOUCH_H] whose range reaches the zone's near edge (support: low <= zone_high;
           resistance: high >= zone_low); a zone that contains the origin close is touched at t;
  break    a CLOSE beyond the far edge by >= BREAK_M sigma (support: close < zone_low e^{-m sigma}) - the touch bar's
           close counts; checked before the hold test on each bar (conservative);
  hold     a reversal: a later bar's high >= zone_high e^{k sigma} (support; resistance: low <= zone_low e^{-k sigma})
           with k = HOLD_K, before any break, within RESOLVE_R sessions after the touch;
  label    hold = 1, else 0 (break, or unresolved after RESOLVE_R sessions).
"""

from __future__ import annotations

import math
import time
import zlib
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl
from numba import njit

from app.core.logsetup import get_logger
from app.techprob import barriers as B

log = get_logger("app.techprob.history")

ORIGIN_START = date(2008, 1, 1)
PRICE_START = date(2003, 1, 1)
HOLD_K = 1.5
BREAK_M = 0.5
TOUCH_H = 20
RESOLVE_R = 10
PRIOR_LOOKBACK = 252
MAX_ZONE_ATR = 8.0
CTRL_LO, CTRL_HI = 0.75, 2.5
MIN_BARS = 300
FWD = (5, 20, 60)


def families() -> list[str]:
    from app.technicals import taxonomy as T  # noqa: PLC0415

    return sorted({k.family for k in T.LEVEL_KEYS})


FAMILIES = families()


# ------------------------------------------------------------------------------------------------ kernels


@njit(cache=True)
def _resolve(h, l, c, lo, hi, side, tau, j_end, kh, mb):  # noqa: E741
    """Hold (1) / break (2) / unresolved (0) after a touch at bar tau, using bars (tau, j_end]."""
    if side == 1:
        hold_thr = hi * kh
        brk_thr = lo / mb
    else:
        hold_thr = lo / kh
        brk_thr = hi * mb
    for j in range(tau + 1, j_end + 1):
        if side == 1:
            if c[j] < brk_thr:
                return 2
            if h[j] >= hold_thr:
                return 1
        else:
            if c[j] > brk_thr:
                return 2
            if l[j] <= hold_thr:
                return 1
    return 0


@njit(cache=True)
def zone_outcome(h, l, c, lo, hi, side, in_zone, i, H, R, kh, mb):  # noqa: E741
    """(touch, tau, outcome, complete) for a band [lo, hi] at origin i (see module docstring)."""
    n = c.shape[0]
    tau = -1
    if in_zone:
        tau = i
    else:
        near = hi if side == 1 else lo
        for j in range(i + 1, min(n, i + H + 1)):
            if (side == 1 and l[j] <= near) or (side == -1 and h[j] >= near):
                tau = j
                break
        if tau < 0:
            return 0, -1, 0, 1 if i + H < n else 0
        brk = lo / mb if side == 1 else hi * mb
        if (side == 1 and c[tau] < brk) or (side == -1 and c[tau] > brk):
            return 1, tau, 2, 1
    j_end = min(n - 1, tau + R)
    res = _resolve(h, l, c, lo, hi, side, tau, j_end, kh, mb)
    complete = 1 if (res > 0 or tau + R <= n - 1) else 0
    return 1, tau, res, complete


@njit(cache=True)
def prior_band_stats(h, l, c, lo, hi, i, lookback, R, kh, mb):  # noqa: E741
    """Distinct touches of [lo, hi] on bars (i - lookback, i] and, of the tests resolved by bar i, how many held."""
    j0 = max(1, i - lookback + 1)
    touches = 0
    resolved = 0
    holds = 0
    prev = False
    for j in range(j0, i + 1):
        tch = l[j] <= hi and h[j] >= lo
        if tch and not prev:
            touches += 1
            s = 0
            if c[j - 1] > hi:
                s = 1
            elif c[j - 1] < lo:
                s = -1
            if s != 0:
                brk = lo / mb if s == 1 else hi * mb
                if (s == 1 and c[j] < brk) or (s == -1 and c[j] > brk):
                    res = 2
                else:
                    res = _resolve(h, l, c, lo, hi, s, j, min(i, j + R), kh, mb)
                if res > 0:
                    resolved += 1
                    if res == 1:
                        holds += 1
        prev = tch
    return touches, resolved, holds


# ------------------------------------------------------------------------------------------------ inputs


@dataclass
class SecInput:
    bars: Any                          # app.technicals.data.Bars
    dv_usd: np.ndarray                 # (n,) USD traded value per session
    earn: np.ndarray = field(default_factory=lambda: np.array([], dtype="datetime64[D]"))


def earnings_flags(dates: np.ndarray, ev: np.ndarray) -> np.ndarray:
    """(n,) bool: first own session >= each earnings date (within 4 calendar days)."""
    E = np.zeros(len(dates), dtype=bool)
    if ev.size == 0 or len(dates) == 0:
        return E
    k = np.searchsorted(dates, ev, side="left")
    ok = k < len(dates)
    k, evv = k[ok], ev[ok]
    ok2 = (dates[k] - evv) <= np.timedelta64(4, "D")
    E[k[ok2]] = True
    return E


def vol_state(lc: np.ndarray, E: np.ndarray) -> dict[str, np.ndarray]:
    """EWMA variance, jump variance, earnings offsets, V(H) for barriers.HORIZONS (and TOUCH_H), vol regime."""
    from app.conformal import envelope as V  # noqa: PLC0415

    r = np.r_[np.nan, np.diff(lc)]
    r[np.abs(np.nan_to_num(r)) > 1.0] = np.nan
    s2 = V.ewma_var(r[:, None], E[:, None])[:, 0]
    J = V.jump_var(r[:, None], E[:, None], s2[:, None])[:, 0]
    ke = V.first_event_offset(E[:, None], B.HMAX)[:, 0].astype(np.int64)
    Hs = np.array(B.HORIZONS)
    ein = (ke[:, None] > 0) & (ke[:, None] <= Hs[None, :])
    Vh = s2[:, None] * Hs[None, :] + np.where(ein, J[:, None], 0.0)
    return {"r": r, "s2": s2, "J": J, "ke": ke, "V": Vh, "earn_in": ein, "vreg": B.vol_regime(s2)}


def weekly_origins(dates: np.ndarray, start: date, min_i: int = MIN_BARS) -> np.ndarray:
    """Indices of the last own session of each ISO week (Mon-based weeks), from `start`."""
    if len(dates) < 2:
        return np.zeros(0, dtype=np.int64)
    wk = (dates.astype("datetime64[D]").astype(np.int64) + 3) // 7
    last = np.r_[wk[1:] != wk[:-1], True]
    idx = np.nonzero(last & (dates >= np.datetime64(start, "D")))[0]
    return idx[idx >= min_i]


def _adv(dv: np.ndarray, i: int, n: int = 20) -> float | None:
    x = dv[max(0, i - n + 1): i + 1]
    x = x[np.isfinite(x)]
    return float(x.mean()) if x.size else None


# ------------------------------------------------------------------------------------------------ zone features


def band_features(lo: float, hi: float, side: int, in_zone: bool, i: int, b: Any, st: dict[str, np.ndarray],
                  ctx: dict[str, Any], setup: dict[str, Any], vp_px: np.ndarray, kh: float, mb: float) -> dict[str, float]:
    """Non-type T3 features of a price band at origin i (shared by real zones and random controls)."""
    close = float(b.c[i])
    sd = float(math.sqrt(st["s2"][i]))
    atr = float(ctx["atr"])
    lc = np.log(b.c)
    center = 0.5 * (lo + hi)
    if side == 1:
        dist = max(0.0, math.log(close / hi)) / sd
    else:
        dist = max(0.0, math.log(lo / close)) / sd
    r5 = (lc[i] - lc[i - 5]) / (sd * math.sqrt(5.0)) if i >= 5 else 0.0
    r20 = (lc[i] - lc[i - 20]) / (sd * math.sqrt(20.0)) if i >= 20 else 0.0
    sma50, sma200 = ctx.get("sma50"), ctx.get("sma200")
    has50 = sma50 is not None and math.isfinite(sma50)
    has200 = sma200 is not None and math.isfinite(sma200)
    struct = {"bullish": 1.0, "bearish": -1.0}.get(ctx.get("structure_state"), 0.0)
    rsi = ctx.get("rsi")
    rsi = rsi if rsi is not None and math.isfinite(rsi) else 50.0
    tch, res, hol = prior_band_stats(b.h, b.l, b.c, lo, hi, i, PRIOR_LOOKBACK, RESOLVE_R, kh, mb)
    vpd = float(np.min(np.abs(vp_px - center)) / atr) if vp_px.size and atr > 0 else 10.0
    return {
        "side": float(side), "in_zone": float(in_zone), "dist_sigma": dist, "width_sigma": math.log(hi / lo) / sd,
        "approach5": -side * r5, "approach20": -side * r20,
        "trend_signed": side * ((setup.get("score_trend") or 50.0) - 50.0) / 50.0, "struct_signed": side * struct,
        "rsi_signed": side * (rsi - 50.0) / 50.0,
        "ma50_dist_atr": (center - sma50) / atr if has50 and atr > 0 else 0.0,
        "ma200_dist_atr": (center - sma200) / atr if has200 and atr > 0 else 0.0,
        "ma50_in": float(has50 and lo <= sma50 <= hi), "ma200_in": float(has200 and lo <= sma200 <= hi),
        "close_ma200": math.log(close / sma200) / sd if has200 else 0.0,
        "vp_dist_atr": min(vpd, 10.0), "prior_touches": float(tch), "prior_resolved": float(res),
        "prior_hold_rate": (hol + 1.0) / (res + 2.0), "vol_regime": float(st["vreg"][i]),
        "earn_in20": float(0 < st["ke"][i] <= TOUCH_H), "sigma_d": sd,
    }


REAL_ONLY = ["n_families", "member_count", "n_types", "log_weight_sum", "strength", "density", *[f"fam_{f}" for f in FAMILIES]]


def real_features(z: dict[str, Any]) -> dict[str, float]:
    fams = set(z.get("member_families") or [])
    out = {f"fam_{f}": float(f in fams) for f in FAMILIES}
    out.update(n_families=float(z.get("n_families") or 0), member_count=float(z.get("member_count") or 0),
               n_types=float(len(z.get("member_level_types") or [])), log_weight_sum=math.log1p(float(z.get("weight_sum") or 0.0)),
               strength=float(z.get("strength") or 0.0), density=float(z.get("density") or 0.0))
    return out


# ------------------------------------------------------------------------------------------------ per security


def _setup_barriers(s: dict[str, Any], close: float) -> tuple[int, float, float, float] | None:
    """(direction +1 long / -1 short, x_up, x_dn, reward_risk from the close) or None."""
    t1, inv = s.get("target1"), s.get("invalidation")
    if s.get("entry") is None or t1 is None or inv is None or not (close > 0 and t1 > 0 and inv > 0):
        return None
    if s.get("bias") == "short":
        if not (t1 < close < inv):
            return None
        x_up, x_dn = math.log(inv / close), math.log(close / t1)
        return -1, x_up, x_dn, x_dn / x_up
    if not (inv < close < t1):
        return None
    x_up, x_dn = math.log(t1 / close), math.log(close / inv)
    return 1, x_up, x_dn, x_up / x_dn


def process_security(si: SecInput, region: str, vix_d: np.ndarray, vix_v: np.ndarray, gc: B.GridCounts | None,
                     origin_start: date = ORIGIN_START, origin_from: date | None = None, zones: bool = True,
                     counts_from: date | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Adds the security's daily origins to `gc` (when given) and returns (setup rows, zone rows) for weekly origins
    >= max(origin_start, origin_from)."""
    from app.technicals.confluence import build_setup, context, find_zones  # noqa: PLC0415
    from app.technicals.levels import build_levels, snapshot  # noqa: PLC0415
    from app.technicals.weights import level_weight_expr  # noqa: PLC0415

    b = si.bars
    n = b.n
    if n < MIN_BARS:
        return [], []
    lc, lh, ll = np.log(b.c), np.log(b.h), np.log(b.l)
    E = earnings_flags(b.dates, si.earn)
    st = vol_state(lc, E)
    U, D, Z = B.path_extremes(lc, lh, ll, B.HMAX)
    if gc is not None:
        use = np.isfinite(st["s2"])
        if counts_from is not None:
            use &= b.dates >= np.datetime64(counts_from, "D")
        B.accumulate(gc, U, D, Z, st["V"], st["vreg"], st["earn_in"], B.quarter_index(b.dates), use)
    o0 = max(origin_start, origin_from) if origin_from else origin_start
    origins = weekly_origins(b.dates, o0)
    if not origins.size:
        return [], []
    ls = build_levels(b)
    vk = np.clip(np.searchsorted(vix_d, b.dates, side="right") - 1, 0, max(len(vix_d) - 1, 0)) if len(vix_d) else None
    seed = zlib.crc32(b.security_id.encode("utf-8")) & 0x7FFFFFFF
    setups: list[dict[str, Any]] = []
    zrows: list[dict[str, Any]] = []
    for i in origins:
        i = int(i)
        s2i = st["s2"][i]
        if not np.isfinite(s2i) or s2i <= 0:
            continue
        d = b.dates[i].astype(object)
        lv = snapshot(ls, b, at=i + 1)
        if lv.is_empty():
            continue
        lv = lv.with_columns(pl.lit(None, dtype=pl.Float64).alias("type_rank_score"))
        lv = lv.with_columns(level_weight_expr(d).alias("weight"))
        ctx = context(b.truncate(d))
        if not (ctx["atr"] > 0):
            continue
        zl = find_zones(lv, ctx["close"], ctx["atr"])
        s = build_setup(zl, ctx)
        close = float(b.c[i])
        sd = math.sqrt(s2i)
        vix_high = bool(vix_v[vk[i]] > 25.0) if vk is not None and len(vix_v) else False
        row: dict[str, Any] = {
            "security_id": b.security_id, "date": d, "region": region, "sector_id": b.sector_id or "NA", "close": close,
            "atr": float(ctx["atr"]), "sigma_d": sd, "vol_regime": int(st["vreg"][i]), "vix_high": vix_high,
            "earn_days_ahead": int(st["ke"][i]), "adv_usd": _adv(si.dv_usd, i), "bias": s["bias"], "trend": s["trend"],
            "structure_state": ctx["structure_state"], "entry": s["entry"], "invalidation": s["invalidation"],
            "target1": s["target1"], "reward_risk": s["reward_risk"], "technical_score": s["technical_score"],
            "score_trend": s["score_trend"], "score_structure": s["score_structure"], "score_location": s["score_location"],
            "score_rr": s["score_rr"], "score_momentum": s["score_momentum"], "n_zones": len(zl),
        }
        for h, H in enumerate(B.HORIZONS):
            row[f"v_{H}"] = float(st["V"][i, h])
            row[f"earn_{H}"] = bool(st["earn_in"][i, h])
        for H in FWD:
            row[f"fwd_{H}"] = float(Z[i, H - 1]) if np.isfinite(Z[i, H - 1]) else None
        bar = _setup_barriers(s, close)
        row["direction"] = bar[0] if bar else 0
        if bar:
            dirn, x_up, x_dn, rr_c = bar
            row.update(x_up=x_up, x_dn=x_dn, rr_close=rr_c)
            for h, H in enumerate(B.HORIZONS):
                oc = B.outcomes(U[i:i + 1], D[i:i + 1], np.array([x_up]), np.array([x_dn]), H)[0]
                tu, td = B.passage_bars(U[i:i + 1], D[i:i + 1], np.array([x_up]), np.array([x_dn]), H)
                row[f"out_{H}"] = int(oc)
                row[f"tau_{H}"] = int(min(tu[0], td[0]))
                row[f"z_{H}"] = float(Z[i, H - 1]) if np.isfinite(Z[i, H - 1]) else None
        setups.append(row)
        if not zones:
            continue
        # ---- zones
        kh, mb = math.exp(HOLD_K * sd), math.exp(BREAK_M * sd)
        vp_px = lv.filter(pl.col("family") == "volume_profile")["price"].to_numpy()
        rng = np.random.default_rng((seed + 7919 * i) & 0x7FFFFFFF)
        base = {"security_id": b.security_id, "date": d, "region": region, "sector_id": b.sector_id or "NA",
                "vix_high": float(vix_high)}
        for z in zl:
            if abs(z["distance_atr"]) > MAX_ZONE_ATR:
                continue
            for is_ctrl in (False, True):
                lo, hi = float(z["zone_low"]), float(z["zone_high"])
                if not (lo > 0 and hi > lo):
                    break                                 # zone reaching non-positive prices: skip it (and its control)
                if is_ctrl:
                    sh = (1.0 if rng.random() < 0.5 else -1.0) * rng.uniform(CTRL_LO, CTRL_HI) * ctx["atr"]
                    lo, hi = lo + sh, hi + sh
                    if lo <= 0:
                        continue
                    side = 1 if 0.5 * (lo + hi) < close else -1
                else:
                    side = 1 if z["side"] == "support" else -1
                inz = bool(lo <= close <= hi)
                tch, tau, oc, comp = zone_outcome(b.h, b.l, b.c, lo, hi, side, inz, i, TOUCH_H, RESOLVE_R, kh, mb)
                if not tch and comp:
                    continue                          # untouched within TOUCH_H: not part of P(hold | touch)
                fr = band_features(lo, hi, side, inz, i, b, st, ctx, s, vp_px, kh, mb)
                fr.update(base)
                fr.update(is_control=is_ctrl, zone_id=int(z["zone_id"]), zone_low=lo, zone_high=hi, touched=bool(tch),
                          tau=int(tau - i) if tau >= 0 else None, outcome=int(oc), complete=bool(comp), hold=bool(oc == 1))
                if is_ctrl:
                    fr.update({k: 0.0 for k in REAL_ONLY})
                else:
                    fr.update(real_features(z))
                zrows.append(fr)
    return setups, zrows


def batch_task(items: list[SecInput], region: str, vix_d: np.ndarray, vix_v: np.ndarray, with_counts: bool = True,
               origin_from: date | None = None, zones: bool = True, counts_from: date | None = None) -> dict[str, Any]:
    """Worker unit (picklable): a batch of securities of one region -> summed grid counts + rows."""
    t0 = time.perf_counter()
    gc = B.GridCounts.empty(region) if with_counts else None
    S: list[dict[str, Any]] = []
    Zr: list[dict[str, Any]] = []
    failed = []
    for si in items:
        try:
            s, z = process_security(si, region, vix_d, vix_v, gc, origin_from=origin_from, zones=zones, counts_from=counts_from)
            S += s
            Zr += z
        except Exception as e:  # noqa: BLE001
            log.warning("techprob history %s failed: %s", si.bars.security_id, e)
            failed.append(si.bars.security_id)
    return {"counts": gc, "setups": S, "zones": Zr, "failed": failed, "n": len(items), "seconds": time.perf_counter() - t0}


def batch_task_ids(ids: list[str], region: str, vix_d: np.ndarray, vix_v: np.ndarray, start: date, end: date,
                   with_counts: bool = True, origin_from: date | None = None, zones: bool = True) -> dict[str, Any]:
    """Memory-lean worker unit: loads its own securities from the lake and returns polars frames (not dicts)."""
    t0 = time.perf_counter()
    items = load_inputs(ids, region, start, end)
    gc = B.GridCounts.empty(region) if with_counts else None
    S: list[pl.DataFrame] = []
    Zr: list[pl.DataFrame] = []
    failed = []
    for si in items:
        try:
            s, z = process_security(si, region, vix_d, vix_v, gc, origin_from=origin_from, zones=zones)
            if s:
                S.append(pl.DataFrame(s, infer_schema_length=None))
            if z:
                Zr.append(pl.DataFrame(z, infer_schema_length=None))
        except Exception as e:  # noqa: BLE001
            log.warning("techprob history %s failed: %s", si.bars.security_id, e)
            failed.append(si.bars.security_id)
    return {"counts": gc, "setups": pl.concat(S, how="diagonal_relaxed") if S else pl.DataFrame(),
            "zones": pl.concat(Zr, how="diagonal_relaxed") if Zr else pl.DataFrame(), "failed": failed, "n": len(items),
            "seconds": time.perf_counter() - t0}


def eligible_ids(security_ids: list[str], region: str, start: date, end: date, min_bars: int = MIN_BARS) -> list[str]:
    """Securities of `security_ids` with >= min_bars positive closes in the region partition (sorted)."""
    from app.core.storage import scan_table  # noqa: PLC0415

    if not security_ids:
        return []
    df = (scan_table("prices_daily").filter((pl.col("region") == region) & (pl.col("date") >= start) & (pl.col("date") <= end)
                                            & pl.col("security_id").is_in(security_ids) & (pl.col("close") > 0))
          .group_by("security_id").agg(pl.col("date").n_unique().alias("n")).collect())
    return sorted(df.filter(pl.col("n") >= min_bars)["security_id"].to_list())


# ------------------------------------------------------------------------------------------------ loading


def load_inputs(security_ids: list[str], region: str, start: date, end: date, settings: Any = None) -> list[SecInput]:
    """Bars (adjusted OHLCV on own prints, region partition only) + USD traded value + earnings days."""
    from app.conformal import data as D  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.technicals.data import iter_bars, load_meta, load_price_panel  # noqa: PLC0415

    if not security_ids:
        return []
    pan = load_price_panel(security_ids, start=start, end=end)
    if "region" in pan.columns:
        pan = pan.filter(pl.col("region") == region)
    dv = (scan_table("prices_daily").filter((pl.col("region") == region) & (pl.col("date") >= start) & (pl.col("date") <= end)
                                            & pl.col("security_id").is_in(security_ids))
          .select("security_id", "date", (pl.col("close") * pl.col("usd_per_unit").fill_null(1.0) * pl.col("volume")).alias("dv"))
          .collect().unique(["security_id", "date"], keep="last"))
    pan = pan.join(dv, on=["security_id", "date"], how="left").sort(["security_id", "date"])
    meta = load_meta(security_ids)
    ev = D.earnings_days(security_ids, start, end + timedelta(days=120), settings)
    evd: dict[str, np.ndarray] = {}
    for (sid,), g in ev.group_by(["security_id"]):
        evd[str(sid)] = np.sort(np.array(g["date"].to_list(), dtype="datetime64[D]"))
    out = []
    dvm = {str(k[0]): g["dv"].to_numpy().astype(np.float64) for k, g in pan.select("security_id", "dv").group_by(["security_id"], maintain_order=True)}
    for b in iter_bars(pan.drop("dv"), meta, min_bars=MIN_BARS):
        b.region = region
        out.append(SecInput(b, dvm.get(b.security_id, np.full(b.n, np.nan)), evd.get(b.security_id, np.array([], dtype="datetime64[D]"))))
    return out
