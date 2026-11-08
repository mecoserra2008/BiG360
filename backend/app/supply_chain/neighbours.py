"""STATISTICAL economic neighbours (job `supply_chain_neighbours`, weekly) -> company_neighbours.

Not disclosed relationships: a fallback layer for names without disclosed customers / suppliers, always labelled
"statistical" in the API / UI.

Method (docs/SUPPLY_CHAIN.md §statistical neighbours):
  * one security per issuer (primary security of the universe, else the one with most history), weekly log returns of
    adj_close (Friday closes, PIT: weeks ending <= as_of) over the last `weeks` (104) weeks; securities need
    >= `min_obs` (52) weekly returns;
  * return_corr   Pearson correlation of weekly returns (pairwise over the common window; missing weeks are
                  mean-filled after standardisation, i.e. they contribute 0 to the cross-product);
  * partial_corr  correlation of the residuals after an OLS of each security's returns on its region's equal-weight
                  market return (with hundreds of members the own-name contribution is negligible), i.e. the partial
                  correlation controlling for the market: co-movement beyond the common market factor;
  * per issuer the top `k` (10) neighbours by partial_corr with partial_corr >= `min_rho` (0.25), plus the top `k`
    by return_corr >= 0.5; blocks of 1,000 rows keep memory bounded (N x 1000 float32 per block).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

WEEKS = 104
MIN_OBS = 52
TOP_K = 10
MIN_PARTIAL = 0.3
MIN_CORR = 0.5
BLOCK = 1000


def weekly_returns(settings: Settings, as_of: date, weeks: int = WEEKS, universe: str = "full") -> tuple[pl.DataFrame, pl.DataFrame]:
    """(meta [security_id, issuer_id, region, sector_id], long weekly log returns [security_id, week, r])."""
    from app.modeling.datasets import lake_connection  # noqa: PLC0415

    con = lake_connection(["prices_daily", "universe_membership", "securities_master", "issuers"], settings)
    try:
        con.execute("SET memory_limit = '1500MB'; SET threads = 4")
        start = as_of - timedelta(weeks=weeks + 2)
        meta = con.execute("""
            SELECT DISTINCT u.security_id, s.issuer_id, coalesce(i.region, 'NA') AS region,
                   coalesce(i.sector_id, 'NA') AS sector_id, coalesce(s.is_primary_security, false) AS is_primary
            FROM universe_membership u JOIN securities_master s ON s.security_id = u.security_id
            JOIN issuers i ON i.issuer_id = s.issuer_id
            WHERE u.universe_id = ? AND u.valid_to IS NULL AND s.issuer_id IS NOT NULL""", [universe]).pl()
        if meta.is_empty():
            return meta, pl.DataFrame()
        con.register("_ids", meta.select("security_id").to_arrow())
        px = con.execute(f"""
            WITH d AS (
              SELECT p.security_id, p.date, coalesce(p.adj_close, p.close) AS px,
                     date_trunc('week', p.date) AS wk
              FROM prices_daily p JOIN _ids USING (security_id)
              WHERE p.date > DATE '{start.isoformat()}' AND p.date <= DATE '{as_of.isoformat()}'
                AND coalesce(p.adj_close, p.close) > 0),
            w AS (SELECT security_id, wk, arg_max(px, date) AS px FROM d GROUP BY 1, 2)
            SELECT security_id, wk AS week,
                   ln(px / lag(px) OVER (PARTITION BY security_id ORDER BY wk)) AS r
            FROM w""").pl()
    finally:
        con.close()
    px = px.filter(pl.col("r").is_not_null() & pl.col("r").is_finite() & (pl.col("r").abs() < 1.5))
    return meta, px


def _standardize(X: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Row-wise z-scores over observed cells; unobserved cells -> 0; rows scaled so dot / n_common ~ correlation."""
    n = mask.sum(axis=1, keepdims=True).clip(min=1)
    mu = np.where(mask, X, 0).sum(axis=1, keepdims=True) / n
    d = np.where(mask, X - mu, 0.0)
    sd = np.sqrt((d ** 2).sum(axis=1, keepdims=True) / n)
    sd[sd == 0] = np.nan
    Z = d / sd
    return np.nan_to_num(Z, nan=0.0).astype(np.float32)


def residualize(X: np.ndarray, mask: np.ndarray, groups: list[np.ndarray]) -> np.ndarray:
    """Residuals of each row on its group-mean factors (equal-weight factor returns; OLS per row, T x (1 + F))."""
    T = X.shape[1]
    facs = []
    for g in groups:   # g: integer group index per row
        F = np.zeros((g.max() + 1, T))
        for k in np.unique(g):
            rows = g == k
            m = mask[rows]
            F[k] = np.where(m, X[rows], 0).sum(axis=0) / m.sum(axis=0).clip(min=1)
        facs.append(F[g])                          # row's factor series
    R = np.zeros_like(X)
    for i in range(X.shape[0]):
        m = mask[i]
        if m.sum() < 10:
            continue
        A = np.column_stack([np.ones(m.sum())] + [f[i][m] for f in facs])
        coef, *_ = np.linalg.lstsq(A, X[i][m], rcond=None)
        R[i][m] = X[i][m] - A @ coef
    return R


def topk_pairs(Z: np.ndarray, n_common: np.ndarray | None, k: int, min_rho: float, min_obs: int,
               mask: np.ndarray) -> list[tuple[int, int, float, int]]:
    """[(i, j, rho, n_obs)] top-k per row (j != i), computed in row blocks."""
    out = []
    N = Z.shape[0]
    M = mask.astype(np.float32)
    for a in range(0, N, BLOCK):
        b = min(N, a + BLOCK)
        C = Z[a:b] @ Z.T                                  # sum of z products over common observed weeks
        nc = M[a:b] @ M.T                                 # common observed weeks
        with np.errstate(divide="ignore", invalid="ignore"):
            rho = np.where(nc >= min_obs, C / nc, np.nan)
        for r in range(b - a):
            rho[r, a + r] = np.nan
        rho = np.nan_to_num(rho, nan=-9.0)
        kk = min(k, N - 1)
        idx = np.argpartition(-rho, kk, axis=1)[:, :kk]
        for r in range(b - a):
            for j in idx[r]:
                v = float(rho[r, j])
                if v >= min_rho:
                    out.append((a + r, int(j), min(v, 1.0), int(nc[r, j])))
    return out


def compute_neighbours(meta: pl.DataFrame, rets: pl.DataFrame, as_of: date, k: int = TOP_K,
                       min_obs: int = MIN_OBS) -> pl.DataFrame:
    if meta.is_empty() or rets.is_empty():
        return pl.DataFrame()
    cnt = rets.group_by("security_id").len()
    meta = meta.join(cnt, on="security_id", how="inner").filter(pl.col("len") >= min_obs)
    # one security per issuer: primary first, then most observations
    meta = meta.sort(["is_primary", "len"], descending=True).unique("issuer_id", keep="first").sort("security_id")
    if meta.height < 3:
        return pl.DataFrame()
    weeks = sorted(rets["week"].unique().to_list())
    wi = {w: n for n, w in enumerate(weeks)}
    si = {s: n for n, s in enumerate(meta["security_id"].to_list())}
    r = rets.filter(pl.col("security_id").is_in(list(si)))
    X = np.zeros((len(si), len(weeks)), dtype=np.float64)
    mask = np.zeros_like(X, dtype=bool)
    rows = np.array([si[x] for x in r["security_id"].to_list()])
    cols = np.array([wi[x] for x in r["week"].to_list()])
    X[rows, cols] = r["r"].to_numpy()
    mask[rows, cols] = True
    reg = meta["region"].to_list()
    sec = meta["sector_id"].to_list()
    g_reg = np.array([{v: n for n, v in enumerate(sorted(set(reg)))}[v] for v in reg])
    Zc = _standardize(X, mask)
    Zp = _standardize(residualize(X, mask, [g_reg]), mask)
    del X
    ids, sids = meta["issuer_id"].to_list(), meta["security_id"].to_list()
    out = []
    for method, Z, mr in (("partial_corr", Zp, MIN_PARTIAL), ("return_corr", Zc, MIN_CORR)):
        pairs = topk_pairs(Z, None, k, mr, min_obs, mask)
        pairs.sort(key=lambda t: (t[0], -t[2]))
        rank: dict[int, int] = {}
        for i, j, rho, n in pairs:
            rank[i] = rank.get(i, 0) + 1
            out.append({"as_of": as_of, "issuer_id": ids[i], "security_id": sids[i], "neighbour_issuer_id": ids[j],
                        "neighbour_security_id": sids[j], "method": method, "rho": round(rho, 5), "rank": rank[i],
                        "n_obs": n, "window_weeks": len(weeks), "same_sector": sec[i] == sec[j],
                        "same_region": reg[i] == reg[j]})
    return pl.DataFrame(out, infer_schema_length=None)


def run_neighbours(settings: Settings | None = None, as_of: date | None = None, universe: str = "full",
                   weeks: int = WEEKS) -> dict[str, Any]:
    from app.core.schema import conform  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415

    s = settings or get_settings()
    t0 = time.perf_counter()
    as_of = as_of or datetime.now().date()
    meta, rets = weekly_returns(s, as_of, weeks, universe)
    df = compute_neighbours(meta, rets, as_of)
    if df.is_empty():
        return {"rows": 0, "note": "no price panel"}
    df = df.with_columns(pl.lit("statistical").alias("layer"), pl.lit(datetime.now().astimezone()).alias("computed_at"))
    n = write_table(conform(df, "company_neighbours"), "company_neighbours", mode="overwrite", settings=s)
    return {"rows": n, "issuers": df["issuer_id"].n_unique(), "as_of": str(as_of),
            "by_method": dict(df.group_by("method").len().iter_rows()), "seconds": round(time.perf_counter() - t0, 1)}


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = run_neighbours(ctx.settings, as_of=date.fromisoformat(p["as_of"]) if p.get("as_of") else None,
                         universe=str(p.get("universe", "full")), weeks=int(p.get("weeks", WEEKS)))
    return JobResult(status="success" if rep.get("rows") else "partial",
                     rows_written={"company_neighbours": rep.get("rows", 0)}, message=str(rep))
