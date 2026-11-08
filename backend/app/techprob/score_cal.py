"""T4: calibrated technical score (docs/TECHNICALS_V2_PLAN.md T4). MODEL-BASED, historical frequencies.

1. Percentile sub-scores: each saturating sub-score (trend, structure, location, R:R, momentum) and the composite
   technical_score become percentile ranks (mid-rank for ties, 0..100) within region x sector on the same date.
   They are ADDITIVE fields; the original 0..100 sub-scores are kept.
2. Score -> outcome mapping: absolute technical_score deciles (boundaries from the fitting history) -> next 5 / 20 /
   60-session excess return (log return minus the equal-weight mean of the same region's universe on the same
   origin date): median, IQR, hit rate (excess > 0), n, month-block bootstrap CIs.
3. Edge test: out-of-sample rank IC (per-date Spearman of score vs excess return), t-stat on non-overlapping
   origin dates (every k-th weekly date, k = ceil(h / 5)); monotonicity = Spearman(decile, mean excess) over the
   deciles plus the top-minus-bottom decile spread CI. edge_significant = IC t >= 2 and spread CI excludes 0
   (same sign as the IC) and Spearman(decile, mean) >= 0.7.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import polars as pl

from app.techprob import calib as K

HORIZONS = (5, 20, 60)
SUBS = ("score_trend", "score_structure", "score_location", "score_rr", "score_momentum")
WEIGHTS = {"score_trend": 0.25, "score_structure": 0.20, "score_location": 0.35, "score_rr": 0.10, "score_momentum": 0.10}
N_DEC = 10
IC_T_MIN = 2.0
MONO_MIN = 0.7


def add_excess(df: pl.DataFrame, horizons: tuple[int, ...] = HORIZONS) -> pl.DataFrame:
    exprs = [(pl.col(f"fwd_{h}") - pl.col(f"fwd_{h}").mean().over(["region", "date"])).alias(f"exc_{h}") for h in horizons]
    return df.with_columns(exprs)


def percentile_exprs(cols: tuple[str, ...] | list[str], by: list[str]) -> list[pl.Expr]:
    out = []
    for c in cols:
        n = pl.col(c).count().over(by)
        r = pl.col(c).rank(method="average").over(by)
        name = "pct_technical" if c == "technical_score" else f"pct_{c.removeprefix('score_')}"
        out.append(pl.when(n > 1).then(100.0 * (r - 0.5) / n).otherwise(50.0).alias(name))
    return out


def add_percentiles(df: pl.DataFrame, by: tuple[str, ...] = ("date", "region", "sector_id")) -> pl.DataFrame:
    """pct_trend .. pct_momentum, pct_technical (rank of the composite) and technical_score_pct (weighted mean of
    the percentile sub-scores)."""
    by_ = list(by)
    df = df.with_columns(percentile_exprs([*SUBS, "technical_score"], by_))
    comp = sum(WEIGHTS[c] * pl.col(f"pct_{c.removeprefix('score_')}") for c in SUBS)
    return df.with_columns(comp.alias("technical_score_pct"))


def decile_edges(x: np.ndarray, n: int = N_DEC) -> np.ndarray:
    q = np.quantile(x[np.isfinite(x)], np.linspace(0, 1, n + 1))
    q = np.maximum.accumulate(q)
    q[0], q[-1] = -np.inf, np.inf
    return q


def decile_of(x: np.ndarray, edges: np.ndarray) -> np.ndarray:
    return np.clip(np.searchsorted(edges, x, side="right") - 1, 0, len(edges) - 2)


def bucket_table(score: np.ndarray, exc: np.ndarray, dates: np.ndarray, edges: np.ndarray, B: int = 200,
                 seed: int = 0) -> list[dict[str, Any]]:
    ok = np.isfinite(score) & np.isfinite(exc)
    score, exc, dates = score[ok], exc[ok], dates[ok]
    k = decile_of(score, edges)
    blocks = K.month_blocks(dates)
    rows = []
    for b in range(len(edges) - 1):
        m = k == b
        if m.sum() < 30:
            rows.append({"decile": b + 1, "n": int(m.sum()), "score_lo": _fin(edges[b]), "score_hi": _fin(edges[b + 1])})
            continue
        x = exc[m]
        med = K.block_bootstrap(lambda ix, xx=x: float(np.median(xx[ix])), blocks[m], B=B, seed=seed)
        hit = K.block_bootstrap(lambda ix, xx=x: float((xx[ix] > 0).mean()), blocks[m], B=B, seed=seed + 1)
        rows.append({"decile": b + 1, "n": int(m.sum()), "score_lo": _fin(edges[b]), "score_hi": _fin(edges[b + 1]),
                     "median": float(np.median(x)), "q25": float(np.quantile(x, 0.25)), "q75": float(np.quantile(x, 0.75)),
                     "mean": float(x.mean()), "hit_rate": float((x > 0).mean()), "median_ci": [med["lo"], med["hi"]],
                     "hit_ci": [hit["lo"], hit["hi"]]})
    return rows


def _fin(x: float) -> float | None:
    return float(x) if math.isfinite(x) else None


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3:
        return None
    ra = pl.Series(a[ok]).rank("average").to_numpy()
    rb = pl.Series(b[ok]).rank("average").to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def rank_ic(df: pl.DataFrame, score_col: str, h: int, min_names: int = 20) -> pl.DataFrame:
    """Per (region, date) Spearman IC of score vs exc_h."""
    d = df.filter(pl.col(score_col).is_not_null() & pl.col(f"exc_{h}").is_not_null())
    d = d.with_columns(pl.col(score_col).rank("average").over(["region", "date"]).alias("_rs"),
                       pl.col(f"exc_{h}").rank("average").over(["region", "date"]).alias("_rr"))
    g = d.group_by(["region", "date"]).agg(pl.corr("_rs", "_rr").alias("ic"), pl.len().alias("n"))
    return g.filter((pl.col("n") >= min_names) & pl.col("ic").is_not_nan()).sort(["region", "date"])


def ic_summary(ic: pl.DataFrame, h: int) -> dict[str, Any]:
    """Mean IC and t-stat on non-overlapping dates (every k-th date per region, k = ceil(h / 5)); the date-level IC
    is first averaged across regions (n-weighted)."""
    if ic.is_empty():
        return {"n_dates": 0}
    by_date = ic.group_by("date").agg(((pl.col("ic") * pl.col("n")).sum() / pl.col("n").sum()).alias("ic")).sort("date")
    k = max(1, math.ceil(h / 5))
    x = by_date["ic"].to_numpy()
    sub = x[::k]
    t = float(sub.mean() / (sub.std(ddof=1) / math.sqrt(len(sub)))) if len(sub) > 2 and sub.std(ddof=1) > 0 else None
    return {"n_dates": int(len(x)), "mean_ic": float(x.mean()), "n_nonoverlap": int(len(sub)), "mean_ic_nonoverlap": float(sub.mean()),
            "t_stat": t, "share_positive": float((x > 0).mean())}


def monotonicity(table: list[dict[str, Any]], exc_by_dec: dict[int, np.ndarray] | None = None, dates_by_dec: dict | None = None,
                 B: int = 300) -> dict[str, Any]:
    rows = [r for r in table if r.get("mean") is not None]
    if len(rows) < 3:
        return {"spearman": None}
    rho = spearman(np.array([r["decile"] for r in rows], dtype=float), np.array([r["mean"] for r in rows]))
    out: dict[str, Any] = {"spearman": rho, "top_minus_bottom_mean": rows[-1]["mean"] - rows[0]["mean"],
                           "top_minus_bottom_median": rows[-1]["median"] - rows[0]["median"]}
    if exc_by_dec is not None and dates_by_dec is not None:
        top, bot = max(exc_by_dec), min(exc_by_dec)
        xt, xb = exc_by_dec[top], exc_by_dec[bot]
        mt, mb = K.month_blocks(dates_by_dec[top]), K.month_blocks(dates_by_dec[bot])
        months = np.intersect1d(np.unique(mt), np.unique(mb))
        st = np.array([xt[mt == m].mean() for m in months])
        sb = np.array([xb[mb == m].mean() for m in months])
        diff = st - sb
        out["spread_ci"] = K.block_bootstrap(lambda ix: float(diff[ix].mean()), np.arange(len(diff)), B=B)
    return out


def edge_verdict(ic: dict[str, Any], mono: dict[str, Any]) -> bool:
    t = ic.get("t_stat")
    sp = mono.get("spread_ci") or {}
    rho = mono.get("spearman")
    if t is None or rho is None or sp.get("lo") is None:
        return False
    same_sign = (t > 0 and sp["lo"] > 0 and rho >= MONO_MIN) or (t < 0 and sp["hi"] < 0 and rho <= -MONO_MIN)
    return bool(abs(t) >= IC_T_MIN and same_sign)


def lookup(score: float | None, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The bucket row of a live score from a fitted table (sorted by decile)."""
    if score is None or not math.isfinite(score) or not rows:
        return None
    for r in sorted(rows, key=lambda r: r["decile"]):
        hi = r.get("score_hi")
        if hi is None or score < hi:
            return r
    return sorted(rows, key=lambda r: r["decile"])[-1]
