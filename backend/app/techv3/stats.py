"""Statistics for the Technicals v3 gate: rank IC series, Newey-West t, decile spreads, monotonicity, BH / Holm
adjustment, block bootstrap (shared with app.techprob.calib), label permutation."""

from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

import numpy as np
import polars as pl

from app.techprob import calib as K

_N = NormalDist()
block_bootstrap = K.block_bootstrap
month_blocks = K.month_blocks


def nw_mean(x: np.ndarray, lags: int) -> tuple[float, float]:
    """Mean and Newey-West (Bartlett) standard error of a series."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = len(x)
    if n < 3:
        return (float(x.mean()) if n else float("nan")), float("nan")
    u = x - x.mean()
    s = (u @ u) / n
    for L in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - L / (lags + 1)) * (u[L:] @ u[:-L]) / n
    return float(x.mean()), float(math.sqrt(max(s, 0.0) / n))


def nw_lags(h: int, step_sessions: int = 5) -> int:
    """Overlap of h-session labels sampled every step_sessions: lags = ceil(h / step) (at least 1) + a small margin."""
    return max(1, math.ceil(h / step_sessions)) + 1


def t_and_p(mean: float, se: float) -> tuple[float | None, float | None]:
    if not (math.isfinite(mean) and math.isfinite(se)) or se <= 0:
        return None, None
    t = mean / se
    return t, 2.0 * (1.0 - _N.cdf(abs(t)))


def bh_adjust(p: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg adjusted p-values (FDR); NaN kept."""
    p = np.asarray(p, dtype=np.float64)
    out = np.full(p.shape, np.nan)
    ok = np.isfinite(p)
    q = p[ok]
    m = len(q)
    if not m:
        return out
    o = np.argsort(q)
    ranked = q[o] * m / np.arange(1, m + 1)
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    adj = np.empty(m)
    adj[o] = np.minimum(ranked, 1.0)
    out[ok] = adj
    return out


def holm_adjust(p: np.ndarray) -> np.ndarray:
    """Holm-Bonferroni adjusted p-values (FWER); NaN kept."""
    p = np.asarray(p, dtype=np.float64)
    out = np.full(p.shape, np.nan)
    ok = np.isfinite(p)
    q = p[ok]
    m = len(q)
    if not m:
        return out
    o = np.argsort(q)
    adj = np.maximum.accumulate(q[o] * (m - np.arange(m)))
    res = np.empty(m)
    res[o] = np.minimum(adj, 1.0)
    out[ok] = res
    return out


def ic_series(df: pl.DataFrame, x: str, y: str, by: list[str], min_names: int = 20) -> pl.DataFrame:
    """Per-group Spearman rank correlation of x and y (rows with both finite); groups with < min_names dropped."""
    d = df.filter(pl.col(x).is_not_null() & pl.col(y).is_not_null() & pl.col(x).is_not_nan() & pl.col(y).is_not_nan())
    d = d.with_columns(pl.col(x).rank("average").over(by).alias("_rx"), pl.col(y).rank("average").over(by).alias("_ry"))
    g = d.group_by(by).agg(pl.corr("_rx", "_ry").alias("ic"), pl.len().alias("n"))
    return g.filter((pl.col("n") >= min_names) & pl.col("ic").is_not_nan() & pl.col("ic").is_not_null()).sort(by)


def permute_within(df: pl.DataFrame, col: str, by: list[str], seed: int = 0) -> pl.DataFrame:
    """Label permutation: shuffle `col` across rows within each group (destroys cross-sectional information)."""
    return df.with_columns(pl.col(col).shuffle(seed=seed).over(by))


def ic_summary(ic: pl.DataFrame, h: int, sign: int = 1, date_col: str = "date") -> dict[str, Any]:
    """n-weighted cross-region IC per date, then the mean and its Newey-West t (lags from the overlap). sign aligns
    the IC with the factor's literature direction (reported IC is the RAW IC; t / p are for the raw mean)."""
    if ic.is_empty():
        return {"n_dates": 0, "mean_ic": None, "t_stat": None, "p_value": None}
    by_date = (ic.group_by(date_col).agg(((pl.col("ic") * pl.col("n")).sum() / pl.col("n").sum()).alias("ic"),
                                         pl.col("n").sum().alias("n")).sort(date_col))
    x = by_date["ic"].to_numpy()
    m, se = nw_mean(x, nw_lags(h))
    t, p = t_and_p(m, se)
    return {"n_dates": int(len(x)), "mean_ic": m, "nw_se": se, "t_stat": t, "p_value": p,
            "share_aligned": float(((np.sign(x) * sign) > 0).mean()) if len(x) else None,
            "mean_names": float(by_date["n"].mean()), "aligned_ic": m * sign}


def decile_table(score: np.ndarray, y: np.ndarray, dates: np.ndarray, n_q: int = 10) -> dict[str, Any]:
    """Per-date quantile buckets of `score` (pooled over dates): mean / median y per bucket, monotonicity
    (Spearman of bucket vs mean), top-bottom spread with a month-block bootstrap CI (date-level spreads)."""
    ok = np.isfinite(score) & np.isfinite(y)
    score, y, dates = score[ok], y[ok], dates[ok]
    if len(score) < n_q * 30:
        return {"buckets": [], "spearman": None, "spread": None}
    # per-date ranks -> buckets
    df = pl.DataFrame({"d": dates, "s": score, "y": y})
    df = df.with_columns(((pl.col("s").rank("average").over("d") - 0.5) / pl.len().over("d")).alias("_p"))
    df = df.with_columns((pl.col("_p") * n_q).floor().clip(0, n_q - 1).cast(pl.Int32).alias("q"))
    b = df.group_by("q").agg(pl.col("y").mean().alias("mean"), pl.col("y").median().alias("median"), pl.len().alias("n")).sort("q")
    rows = [{"bucket": int(r["q"]) + 1, "mean": float(r["mean"]), "median": float(r["median"]), "n": int(r["n"])}
            for r in b.iter_rows(named=True)]
    rho = None
    if len(rows) >= 3:
        a = np.array([r["bucket"] for r in rows], dtype=float)
        mm = np.array([r["mean"] for r in rows])
        rho = float(np.corrcoef(pl.Series(a).rank().to_numpy(), pl.Series(mm).rank().to_numpy())[0, 1])
    sp = (df.filter(pl.col("q").is_in([0, n_q - 1])).group_by("d", "q").agg(pl.col("y").mean())
          .pivot(on="q", index="d", values="y").drop_nulls())
    spread = None
    if sp.height > 10 and str(n_q - 1) in sp.columns and "0" in sp.columns:
        diff = (sp[str(n_q - 1)] - sp["0"]).to_numpy()
        dd = sp["d"].to_numpy().astype("datetime64[D]")
        blocks = month_blocks(dd)
        ci = block_bootstrap(lambda ix: float(diff[ix].mean()), blocks, B=300, seed=0)
        spread = {"mean": float(diff.mean()), "lo": ci["lo"], "hi": ci["hi"], "n_dates": int(len(diff))}
    return {"buckets": rows, "spearman": rho, "spread": spread}


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 3:
        return None
    ra = pl.Series(a[ok]).rank("average").to_numpy()
    rb = pl.Series(b[ok]).rank("average").to_numpy()
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])
