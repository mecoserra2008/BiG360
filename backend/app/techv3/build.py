"""Per-region Technicals v3 pipeline shared by the research walk-forward (scripts/technicals_v3) and the nightly jobs.

  1. market pass   chunk by chunk: daily log returns, 20-day ADV, liquidity at t-1 -> equal-weight market and
                   region x sector daily returns of the LIQUID names (accumulated sums, so chunks never coexist)
  2. factor pass   chunk by chunk: every factor (app.techv3.factors), liquidity / size fields, optional forward
                   returns (labels, research only) and breakout events (app.techv3.breakouts) -> long frames
  3. cross-section percentiles within region x sector x date among the liquid names (every stored row gets the
                   percentile its value would have in that liquid cross-section); industry momentum and thin sectors
                   (< MIN_PEERS liquid names) are ranked within the region.

Liquidity (the "investable" filter): 20-day average dollar volume >= LIQ_FLOOR (USD) and close >= PRICE_FLOOR (USD).
Market cap is NOT available point-in-time outside the ~700-name mvp universe (valuation_daily), so size buckets use
the 60-day dollar volume: large >= 50M, mid 10-50M, small < 10M (liquid), micro = below the liquidity floor
(stored when ADV >= LOOSE_FLOOR, excluded from the main evaluation and reported separately).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from app.techv3 import breakouts as BO
from app.techv3 import factors as F
from app.techv3 import panel as P

LIQ_FLOOR = 1.0e6
LOOSE_FLOOR = 2.0e5
PRICE_FLOOR = 1.0
SIZE_LARGE, SIZE_MID = 5.0e7, 1.0e7
MIN_PEERS = 10
CHUNK = 600
PCT_COLS = tuple(f"pct_{f}" for f in F.FACTORS)


@dataclass
class Market:
    dates: np.ndarray
    rm: np.ndarray                                   # (T,) market daily log return (NaN when < MIN_SECTOR names)
    rs: dict[str, np.ndarray] = field(default_factory=dict)   # sector -> (T,)
    n_mkt: np.ndarray | None = None

    def level(self) -> np.ndarray:
        return F.index_level(self.rm)

    def sector_arrays(self, sectors: list[str]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(T, n) sector daily return, sector level, validity for a chunk's sector list."""
        T = len(self.dates)
        rs = np.full((T, len(sectors)), np.nan)
        for k, s in enumerate(sectors):
            x = self.rs.get(s)
            if x is not None:
                rs[:, k] = x
        valid = np.isfinite(rs)
        return rs, F.index_level(rs), valid


def _liquid(DV: np.ndarray, CU: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    adv20 = F.roll_mean(DV, 20, 10)
    adv60 = F.roll_mean(DV, 60, 30)
    cuf = F.ffill(CU)
    with np.errstate(invalid="ignore"):
        liq = (adv20 >= LIQ_FLOOR) & (cuf >= PRICE_FLOOR)
    return adv20, adv60, liq


def market_pass(con: Any, region: str, secs: pl.DataFrame, cal: np.ndarray, start: date, end: date,
                chunk: int = CHUNK, check: Any = None) -> Market:
    T = len(cal)
    tot = np.zeros(T)
    cnt = np.zeros(T)
    s_tot: dict[str, np.ndarray] = {}
    s_cnt: dict[str, np.ndarray] = {}
    ids = secs["security_id"].to_list()
    sec_of = dict(zip(ids, secs["sector_id"].to_list(), strict=True))
    for c0 in range(0, len(ids), chunk):
        if check:
            check()
        part = ids[c0:c0 + chunk]
        ch = P.load_chunk(con, region, part, cal, start, end)
        r = F.log_returns(ch.C)
        _a20, _a60, liq = _liquid(ch.DV, ch.CU)
        lp = F.shift(liq.astype(np.float64), 1) > 0.5
        rc = np.clip(r, -F.RET_CLIP, F.RET_CLIP)
        m = np.isfinite(rc) & lp
        z = np.where(m, rc, 0.0)
        tot += z.sum(1)
        cnt += m.sum(1)
        sl = np.array([sec_of[s] for s in part], dtype=object)
        for g in np.unique(sl):
            k = sl == g
            s_tot.setdefault(g, np.zeros(T))
            s_cnt.setdefault(g, np.zeros(T))
            s_tot[g] += z[:, k].sum(1)
            s_cnt[g] += m[:, k].sum(1)
        del ch, r, liq, lp, rc, m, z
    with np.errstate(invalid="ignore", divide="ignore"):
        rm = np.where(cnt >= F.MIN_SECTOR, tot / np.maximum(cnt, 1), np.nan)
        rs = {g: np.where(s_cnt[g] >= F.MIN_SECTOR, s_tot[g] / np.maximum(s_cnt[g], 1), np.nan) for g in s_tot}
    rs.pop("NA", None)            # unclassified names: market only (no meaningful 'sector')
    return Market(cal, rm, rs, cnt)


def factor_chunk(ch: P.Chunk, sectors: list[str], mkt: Market, out_from: int, labels: bool = True,
                 keep_all: set[str] | None = None) -> pl.DataFrame:
    """Long frame [security_id, date, sector_id, factors..., adv20, adv60, close_usd, liquid (+ fwd_5/20/60)] for
    rows >= out_from with a price and (ADV >= LOOSE_FLOOR or the id in keep_all)."""
    r = F.log_returns(ch.C)
    rs, ls, lsv = mkt.sector_arrays(sectors)
    fac = F.compute(ch.C, r, mkt.rm, rs, mkt.level(), np.isfinite(mkt.rm), ls, lsv)
    adv20, adv60, liq = _liquid(ch.DV, ch.CU)
    alive = np.isfinite(F.ffill(ch.C))
    keep = alive.copy()
    with np.errstate(invalid="ignore"):
        loose = adv20 >= LOOSE_FLOOR
    if keep_all:
        ka = np.array([s in keep_all for s in ch.ids])
        loose = loose | ka[None, :]
    keep &= loose
    keep[:out_from] = False
    t_i, j_i = np.nonzero(keep)
    cols: dict[str, Any] = {"security_id": np.asarray(ch.ids, dtype=object)[j_i], "date": ch.dates[t_i],
                            "sector_id": np.asarray(sectors, dtype=object)[j_i]}
    for k in F.FACTORS:
        cols[k] = fac[k][t_i, j_i].astype(np.float32)
    cols["adv20"] = adv20[t_i, j_i].astype(np.float32)
    cols["adv60"] = adv60[t_i, j_i].astype(np.float32)
    cols["close_usd"] = F.ffill(ch.CU)[t_i, j_i].astype(np.float32)
    cols["liquid"] = liq[t_i, j_i]
    if labels:
        for h, y in F.forward_returns(ch.C).items():
            cols[f"fwd_{h}"] = y[t_i, j_i].astype(np.float32)
    df = pl.DataFrame({k: (v.tolist() if v.dtype == object else v) for k, v in cols.items()})
    return _nan_to_null(df.with_columns(pl.col("date").cast(pl.Date)))


def _nan_to_null(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns([pl.col(c).fill_nan(None) for c, t in df.schema.items() if t in (pl.Float32, pl.Float64)])


def breakout_chunk(ch: P.Chunk, out_from: int, seed: int = 0, controls: bool = True) -> tuple[pl.DataFrame, pl.DataFrame]:
    r = F.log_returns(ch.C)
    ev, ct = BO.events_for_chunk(ch.O, ch.H, ch.L, ch.C, ch.V, ch.DV, r, ch.dates, ch.ids, seed=seed, controls=controls,
                                 t_min=out_from)
    if not ev:
        return pl.DataFrame(), pl.DataFrame()
    adv20, _a60, liq = _liquid(ch.DV, ch.CU)
    ev["adv20"] = adv20[ev["t"], ev["j"]].astype(np.float32)
    ev["liquid"] = liq[ev["t"], ev["j"]]
    e = _frame(ev)
    if ct:
        ct["event_date"] = ch.dates[ct["event_t"]]
    c = _frame(ct).with_columns(pl.col("event_date").cast(pl.Date)) if ct else pl.DataFrame()
    if c.height:
        c = c.with_columns(pl.Series("liquid", liq[ct["t"], ct["j"]]))
    return e, c


def _frame(d: dict[str, np.ndarray]) -> pl.DataFrame:
    out = {}
    for k, v in d.items():
        if k in ("t", "j"):
            continue
        if v.dtype == object:
            out[k] = v.tolist()
        elif v.dtype == np.float64:
            out[k] = v.astype(np.float32)
        else:
            out[k] = v
    return _nan_to_null(pl.DataFrame(out).with_columns(pl.col("date").cast(pl.Date)))


# ------------------------------------------------------------------------------------------------ cross-section


def group_percentiles(keys: np.ndarray, values: np.ndarray, ref: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Percentile 0..100 of each value within its group's REFERENCE set (mid-rank for ties): 100 (left + right) /
    (2 n_ref) where left / right = number of reference values < / <= the value (for a reference member this is the
    usual (rank - 0.5) / n). keys: int64 group codes. Returns (pct, n_ref)."""
    n = len(values)
    pct = np.full(n, np.nan)
    nref = np.zeros(n, dtype=np.int64)
    ok = np.isfinite(values)
    order = np.argsort(keys, kind="stable")
    ks = keys[order]
    bounds = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1], True])
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        ix = order[a:b]
        v = values[ix]
        rv = np.sort(v[ref[ix] & ok[ix]])
        m = len(rv)
        if m == 0:
            continue
        good = ok[ix]
        lft = np.searchsorted(rv, v[good], "left")
        rgt = np.searchsorted(rv, v[good], "right")
        pct[ix[good]] = 100.0 * (lft + rgt) / (2.0 * m)
        nref[ix] = m
    return pct, nref


def add_percentiles(df: pl.DataFrame, region: str, min_peers: int = MIN_PEERS) -> pl.DataFrame:
    """pct_<factor> within region x sector x date among the liquid names (region x date for indmom, for 'NA' and for
    sectors with < min_peers liquid names); n_peers, pct_scope."""
    if df.is_empty():
        return df
    d = df["date"].to_numpy().astype("datetime64[D]").astype(np.int64)
    sec = df["sector_id"].fill_null("NA").to_numpy().astype(str)
    _u, sec_code = np.unique(sec, return_inverse=True)
    k_sec = d * 1000 + sec_code.astype(np.int64)
    k_reg = d * 1000 + 999
    liq = df["liquid"].fill_null(False).to_numpy()
    out = {}
    # peers per sector group (liquid names with a hi52 value - the most available factor - as the reference count)
    _p, n_sec = group_percentiles(k_sec, np.where(liq, 0.0, np.nan), liq)
    sector_ok = (n_sec >= min_peers) & (sec != "NA")
    for f in F.FACTORS:
        v = df[f].cast(pl.Float64).to_numpy()
        p_reg, _ = group_percentiles(k_reg, v, liq)
        if f == "indmom":
            out[f"pct_{f}"] = p_reg
            continue
        p_sec, _ = group_percentiles(k_sec, v, liq)
        out[f"pct_{f}"] = np.where(sector_ok, p_sec, p_reg)
    out["n_peers"] = np.where(sector_ok, n_sec, 0)
    _p2, n_reg = group_percentiles(k_reg, np.where(liq, 0.0, np.nan), liq)
    out["n_peers"] = np.where(sector_ok, n_sec, n_reg).astype(np.int32)
    out["pct_scope"] = np.where(sector_ok, "sector", "region")
    df = df.with_columns([pl.Series(k, v.astype(np.float32) if v.dtype == np.float64 else v) for k, v in out.items()]
                         ).with_columns(pl.lit(region).alias("region"))
    return _nan_to_null(df)


def size_bucket_expr() -> pl.Expr:
    return (pl.when(~pl.col("liquid")).then(pl.lit("micro"))
            .when(pl.col("adv60") >= SIZE_LARGE).then(pl.lit("large"))
            .when(pl.col("adv60") >= SIZE_MID).then(pl.lit("mid"))
            .otherwise(pl.lit("small")))


def composite(df: pl.DataFrame, factors: list[str] | tuple[str, ...], name: str = "composite") -> pl.DataFrame:
    """Equal-weight mean of the SIGN-ALIGNED percentiles of `factors` (100 - pct for factors whose literature sign
    is negative); null when fewer than half of them are available."""
    if not factors:
        return df.with_columns(pl.lit(None, pl.Float32).alias(name))
    ex = [(pl.col(f"pct_{f}") if F.SIGN[f] > 0 else 100.0 - pl.col(f"pct_{f}")) for f in factors]
    n_ok = pl.sum_horizontal([e.is_not_null().cast(pl.Int32) for e in ex])
    s = pl.sum_horizontal([e.fill_null(0.0) for e in ex])
    need = max(1, math.ceil(len(factors) / 2))
    return df.with_columns(pl.when(n_ok >= need).then(s / n_ok).otherwise(None).cast(pl.Float32).alias(name))
