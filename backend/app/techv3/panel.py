"""Point-in-time data access for Technicals v3 (reads only; DuckDB over the lake with a small memory limit).

Region panels are loaded in CHUNKS of securities on the region's trading calendar (dates on which >= 30% of the
region's equities printed), so a large region (US: ~6,700 securities x 25 years) never sits in memory at once.
Prices: prices_daily OHLC x adj_factor (split + dividend adjusted, local currency), volume / split_factor (shares),
dollar volume = raw close x usd_per_unit x raw volume (USD), close_usd = raw close x usd_per_unit (price floor).
Equities only: securities_master.security_type in (common, adr). Sector: issuers.sector_id (current classification;
documented look-ahead in the classification, as in every gate of this project), 'NA' when unclassified.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings

EQUITY_TYPES = ("common", "adr")
GATE_REGIONS = ("US", "EU", "UK", "JP")
ALL_REGIONS = ("US", "EU", "UK", "JP", "CN", "KR", "HK", "APAC", "LATAM", "CA")


def lake(tables: list[str], settings: Settings | None = None, mem: str = "1000MB", threads: int = 2) -> Any:
    from app.modeling.datasets import lake_connection  # noqa: PLC0415

    con = lake_connection(tables, settings or get_settings())
    con.execute(f"SET memory_limit = '{mem}'; SET threads = {threads}")
    return con


def region_securities(con: Any, region: str, start: date, end: date, min_bars: int = 60) -> pl.DataFrame:
    """Equities with prices in the region over [start, end]: security_id, sector_id, n_bars."""
    ph = ", ".join(f"'{t}'" for t in EQUITY_TYPES)
    return con.execute(f"""
        WITH p AS (SELECT security_id, count(*) AS n_bars FROM prices_daily
                   WHERE region = ? AND date BETWEEN ? AND ? AND close > 0 GROUP BY 1)
        SELECT p.security_id, coalesce(i.sector_id, 'NA') AS sector_id, p.n_bars
        FROM p JOIN securities_master s ON s.security_id = p.security_id
        LEFT JOIN issuers i ON i.issuer_id = s.issuer_id
        WHERE CAST(s.security_type AS VARCHAR) IN ({ph}) AND p.n_bars >= ?
        ORDER BY p.security_id""", [region, start, end, min_bars]).pl()


def region_calendar(con: Any, region: str, start: date, end: date, min_share: float = 0.3) -> np.ndarray:
    ph = ", ".join(f"'{t}'" for t in EQUITY_TYPES)
    df = con.execute(f"""
        SELECT p.date, count(*) AS n FROM prices_daily p JOIN securities_master s ON s.security_id = p.security_id
        WHERE p.region = ? AND p.date BETWEEN ? AND ? AND p.close > 0 AND CAST(s.security_type AS VARCHAR) IN ({ph})
        GROUP BY 1 ORDER BY 1""", [region, start, end]).pl()
    if df.is_empty():
        return np.array([], dtype="datetime64[D]")
    n = df["n"].to_numpy()
    # rolling reference = the max count over the trailing 250 dates (the universe grows over time)
    ref = pl.Series(n).rolling_max(250, min_samples=1).to_numpy()
    keep = n >= np.maximum(1, (min_share * ref).astype(int))
    return np.array(df["date"].cast(pl.Date).to_list(), dtype="datetime64[D]")[keep]


@dataclass
class Chunk:
    ids: list[str]
    dates: np.ndarray        # (T,) datetime64[D]
    O: np.ndarray            # noqa: E741  (T, n) adjusted
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    V: np.ndarray            # split-adjusted shares
    DV: np.ndarray           # USD dollar volume
    CU: np.ndarray           # raw close in USD


def load_chunk(con: Any, region: str, ids: list[str], cal: np.ndarray, start: date, end: date,
               dtype: Any = np.float64) -> Chunk:
    con.register("_tv3_ids", pl.DataFrame({"security_id": ids}).to_arrow())
    try:
        df = con.execute("""
            SELECT security_id, date, open, high, low, close, volume, coalesce(adj_factor, 1.0) AS f,
                   coalesce(split_factor, 1.0) AS sf, coalesce(usd_per_unit, 1.0) AS fx
            FROM prices_daily WHERE region = ? AND date BETWEEN ? AND ? AND close > 0
              AND security_id IN (SELECT security_id FROM _tv3_ids)""", [region, start, end]).pl()
    finally:
        con.unregister("_tv3_ids")
    T, n = len(cal), len(ids)
    out = {k: np.full((T, n), np.nan, dtype=dtype) for k in ("O", "H", "L", "C", "V", "DV", "CU")}
    if df.is_empty() or not T:
        return Chunk(ids, cal, **out)
    df = df.with_columns(pl.col("date").cast(pl.Date)).unique(["security_id", "date"], keep="last")
    d = df["date"].to_numpy().astype("datetime64[D]")
    rows = np.searchsorted(cal, d)
    ok = (rows < T) & (cal[np.minimum(rows, T - 1)] == d)
    ci = {s: i for i, s in enumerate(ids)}
    cols = np.array([ci[s] for s in df["security_id"].to_list()], dtype=np.int64)
    rows, cols = rows[ok], cols[ok]

    def v(c: str) -> np.ndarray:
        return df[c].cast(pl.Float64).to_numpy()[ok]

    f = v("f")
    f = np.where(np.isfinite(f) & (f > 0), f, 1.0)
    sf = v("sf")
    sf = np.where(np.isfinite(sf) & (sf > 0), sf, 1.0)
    fx = v("fx")
    c = v("close")
    o, h, lo, vol = v("open"), v("high"), v("low"), v("volume")
    o = np.where(np.isfinite(o) & (o > 0), o, c)
    h = np.where(np.isfinite(h) & (h > 0), h, c)
    lo = np.where(np.isfinite(lo) & (lo > 0), lo, c)
    h = np.fmax(h, np.fmax(o, c))
    lo = np.fmin(lo, np.fmin(o, c))
    vol = np.where(np.isfinite(vol) & (vol >= 0), vol, 0.0)
    out["C"][rows, cols] = c * f
    out["O"][rows, cols] = o * f
    out["H"][rows, cols] = h * f
    out["L"][rows, cols] = lo * f
    out["V"][rows, cols] = vol / sf
    out["DV"][rows, cols] = c * fx * vol
    out["CU"][rows, cols] = c * fx
    return Chunk(ids, cal, **out)


def vix_daily(start: date, end: date, settings: Settings | None = None) -> pl.DataFrame:
    from app.conformal import data as D  # noqa: PLC0415

    return D.vix_daily(start, end, settings)


def universe_members(universes: tuple[str, ...], settings: Settings | None = None) -> set[str]:
    from app.conformal import data as D  # noqa: PLC0415

    try:
        return set(D.universe_ids(universes, settings)["security_id"].to_list())
    except Exception:  # noqa: BLE001
        return set()
