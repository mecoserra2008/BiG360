"""Shared helpers for the signals-alt modules (insider, sentiment, Lazy Prices, GDELT linking). Owner: signals-alt.

* `security_map()`          issuer_id -> primary security_id / ticker / sector (from the reference tables)
* `resolve_entities()`      source-native keys (CIK, LEI, ...) -> issuer_id/security_id via app.normalize.identity
                            (re-runs cleanly: rows that already carry ids keep them)
* `load_prices()`           adjusted close panel (security_id, date, adj_close, dollar_volume_usd) for a set of ids
* `sector_benchmark()`      sector id -> sector ETF benchmark security id (prices_daily synthetic ids, SPY fallback)
* `ewm_by_group()`          irregular-time EWM (halflife in days) of a weighted sum, missing days = no information
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, table_dir

log = get_logger(__name__)

MARKET_BENCHMARK = "ETF:SPY"
SECTOR_ETF: dict[str, str] = {
    "TECH": "ETF:XLK", "COMM": "ETF:XLC", "CDIS": "ETF:XLY", "CSTA": "ETF:XLP", "HLTH": "ETF:XLV", "FINS": "ETF:XLF",
    "INDU": "ETF:XLI", "MATR": "ETF:XLB", "ENRG": "ETF:XLE", "UTIL": "ETF:XLU", "REAL": "ETF:XLRE",
}


def has_data(table: str, settings: Settings | None = None) -> bool:
    try:
        root = table_dir(table, settings)
    except KeyError:
        return False
    return root.exists() and any(root.rglob("*.parquet"))


def security_map(settings: Settings | None = None) -> pl.DataFrame:
    """One row per issuer: issuer_id, security_id (primary), ticker, sector_id, name, short_name, region, country."""
    s = settings or get_settings()
    schema = {"issuer_id": pl.Utf8, "security_id": pl.Utf8, "ticker": pl.Utf8, "sector_id": pl.Utf8,
              "name": pl.Utf8, "short_name": pl.Utf8, "region": pl.Utf8, "country": pl.Utf8}
    if not (has_data("issuers", s) and has_data("securities_master", s)):
        return pl.DataFrame(schema=schema)
    iss = scan_table("issuers", s).select("issuer_id", "name", "short_name", "sector_id", "region", "country").collect()
    sm = scan_table("securities_master", s).select(
        "security_id", "issuer_id", "display_ticker", "is_primary_security", "is_active").collect()
    sm = (sm.with_columns(pl.col("is_primary_security").fill_null(False), pl.col("is_active").fill_null(True))
          .sort(["issuer_id", "is_primary_security", "is_active"], descending=[False, True, True])
          .unique("issuer_id", keep="first"))
    out = iss.join(sm.select("issuer_id", "security_id", pl.col("display_ticker").alias("ticker")), on="issuer_id",
                   how="left")
    return out.select(list(schema)).cast(schema)  # type: ignore[arg-type]


def resolve_entities(df: pl.DataFrame, source_col: str = "source_id", value_col: str = "source_entity_id",
                     as_of: date | None = None) -> pl.DataFrame:
    """Fill issuer_id / security_id from source-native keys (identity layer). Missing identity -> unchanged."""
    if df.is_empty():
        return df
    try:
        from app.normalize import identity  # noqa: PLC0415

        keys = df.select(source_col, value_col).unique().with_columns(
            identity.entity_id_type_expr(source_col, value_col).alias("__idt"))
        keys = keys.filter(pl.col("__idt").is_not_null())
        if keys.is_empty():
            return df
        res = identity.resolve_frame(keys, value_col, id_type_col="__idt", as_of=as_of, min_confidence=0.9,
                                     out_cols=("issuer_id", "primary_security_id"))
        res = res.select(source_col, value_col, pl.col("issuer_id").alias("__iss"),
                         pl.col("primary_security_id").alias("__sec"))
    except Exception as e:  # noqa: BLE001 - identity layer not ready: keep native keys only
        log.warning("identity resolution unavailable (%s: %s)", type(e).__name__, e)
        return df
    out = df.join(res, on=[source_col, value_col], how="left")
    exprs = [pl.coalesce(pl.col("issuer_id"), pl.col("__iss")).alias("issuer_id") if "issuer_id" in df.columns
             else pl.col("__iss").alias("issuer_id"),
             pl.coalesce(pl.col("security_id"), pl.col("__sec")).alias("security_id") if "security_id" in df.columns
             else pl.col("__sec").alias("security_id")]
    out = out.with_columns(exprs).drop("__iss", "__sec")
    # securities with no primary flag: fall back to the issuer's mapped primary security
    sm = security_map()
    if sm.height:
        out = out.join(sm.select("issuer_id", pl.col("security_id").alias("__sec2")), on="issuer_id", how="left") \
            .with_columns(pl.coalesce(pl.col("security_id"), pl.col("__sec2")).alias("security_id")).drop("__sec2")
    return out


def load_prices(security_ids: Iterable[str] | None = None, start: date | None = None,
                settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, date, adj_close, close_usd, dollar_volume_usd] sorted by (security_id, date)."""
    lf = scan_table("prices_daily", settings).select("security_id", "date", "adj_close", "close", "close_usd",
                                                     "dollar_volume_usd")
    if security_ids is not None:
        lf = lf.filter(pl.col("security_id").is_in(list(set(security_ids))))
    if start is not None:
        lf = lf.filter(pl.col("date") >= start)
    df = lf.collect()
    return (df.with_columns(pl.coalesce("adj_close", "close").alias("adj_close"))
            .filter(pl.col("adj_close").is_not_null() & (pl.col("adj_close") > 0))
            .unique(["security_id", "date"], keep="last").sort(["security_id", "date"]).drop("close"))


def sector_benchmark(sector_id: str | None) -> str:
    return SECTOR_ETF.get(sector_id or "", MARKET_BENCHMARK)


def ewm_by_group(df: pl.DataFrame, group: str, date_col: str, num_col: str, den_col: str, halflife_days: float,
                 out: str, weight_out: str | None = None) -> pl.DataFrame:
    """Irregular-time EWM of num/den per group: each row's contribution decays by 0.5**(dt/halflife).
    Rows must be one per (group, date). Missing days add no information (they never pull the mean to 0)."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, pl.Float64).alias(out))
    df = df.sort([group, date_col])
    g = df.get_column(group).to_numpy()
    d = df.get_column(date_col).cast(pl.Int32).to_numpy().astype(np.float64)
    num = df.get_column(num_col).fill_null(0.0).to_numpy().astype(np.float64)
    den = df.get_column(den_col).fill_null(0.0).to_numpy().astype(np.float64)
    res = np.full(len(df), np.nan)
    wts = np.zeros(len(df))
    lam = math.log(2.0) / halflife_days
    sn = sd = 0.0
    prev_g = None
    prev_d = 0.0
    for i in range(len(df)):
        if g[i] != prev_g:
            sn = sd = 0.0
            prev_g = g[i]
        else:
            w = math.exp(-lam * (d[i] - prev_d))
            sn *= w
            sd *= w
        sn += num[i]
        sd += den[i]
        prev_d = d[i]
        res[i] = sn / sd if sd > 1e-12 else np.nan
        wts[i] = sd
    df = df.with_columns(pl.Series(out, res).fill_nan(None))
    return df.with_columns(pl.Series(weight_out, wts)) if weight_out else df


def rolling_z(df: pl.DataFrame, group: str, col: str, window: int, out: str, min_periods: int = 10) -> pl.DataFrame:
    """z-score of `col` vs the group's own trailing `window` rows (excluding the current row).
    `df` must already be sorted by (group, date)."""
    mean = pl.col(col).shift(1).rolling_mean(window, min_samples=min_periods).over(group)
    std = pl.col(col).shift(1).rolling_std(window, min_samples=min_periods).over(group)
    return df.with_columns(((pl.col(col) - mean) / pl.when(std > 1e-9).then(std).otherwise(None)).alias(out))
