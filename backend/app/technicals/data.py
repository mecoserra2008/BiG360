"""Price/metadata loading for the technicals package (owner: technicals; reads only).

Prices: `prices_daily` (contract owner ingest-market), adjusted with adj_factor (OHLC) and split_factor (volume).
Research fallback (`source="staging"`): `prices_staging` yfinance raw bars adjusted by adj_close/close, keyed by the
source symbol — used only by research_report.py when prices_daily is still empty; never written anywhere.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np
import polars as pl

from app.core.logsetup import get_logger
from app.core.storage import scan_table

log = get_logger(__name__)

PRICE_COLS = ["security_id", "date", "open", "high", "low", "close", "volume"]
#: securities_master types that are market instruments, not equities (price_history: IDX:/ETF: synthetic ids)
INSTRUMENT_TYPES = ("fx", "future", "index", "etf")


@dataclass
class Bars:
    security_id: str
    dates: np.ndarray            # datetime64[D], ascending, unique
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray                # noqa: E741
    c: np.ndarray
    v: np.ndarray
    ath_prior: float = float("nan")          # max adjusted high before dates[0] (for all_time_high)
    earnings: np.ndarray = field(default_factory=lambda: np.array([], dtype="datetime64[D]"))
    region: str | None = None
    sector_id: str | None = None

    @property
    def n(self) -> int:
        return int(self.c.shape[0])

    @property
    def pool_key(self) -> str:
        return f"{self.sector_id or 'NA'}|{self.region or 'NA'}"

    def truncate(self, as_of: date) -> Bars:
        """Bars with date <= as_of (point-in-time view)."""
        k = int(np.searchsorted(self.dates, np.datetime64(as_of, "D"), side="right"))
        return Bars(self.security_id, self.dates[:k], self.o[:k], self.h[:k], self.l[:k], self.c[:k], self.v[:k],
                    self.ath_prior, self.earnings, self.region, self.sector_id)


def _clean_ohlc(df: pl.DataFrame) -> pl.DataFrame:
    """Fill missing O/H/L with close, enforce low <= open/close <= high, drop non-positive closes."""
    c = pl.col("close")
    df = df.filter(c.is_not_null() & (c > 0))
    df = df.with_columns(
        pl.when(pl.col("open") > 0).then(pl.col("open")).otherwise(c).alias("open"),
        pl.when(pl.col("high") > 0).then(pl.col("high")).otherwise(c).alias("high"),
        pl.when(pl.col("low") > 0).then(pl.col("low")).otherwise(c).alias("low"),
        pl.col("volume").fill_null(0.0).clip(lower_bound=0.0).alias("volume"),
    )
    return df.with_columns(
        pl.max_horizontal("high", "open", "close").alias("high"),
        pl.min_horizontal("low", "open", "close").alias("low"),
    )


def prices_source() -> str:
    """'prices_daily' when it has rows, else 'staging'."""
    try:
        n = scan_table("prices_daily").select(pl.len()).collect().item()
    except Exception:  # noqa: BLE001
        n = 0
    return "prices_daily" if n else "staging"


def load_price_panel(security_ids: Sequence[str] | None = None, start: date | None = None, end: date | None = None,
                     source: str = "prices_daily") -> pl.DataFrame:
    """Adjusted daily OHLCV panel sorted by (security_id, date) with columns PRICE_COLS + region."""
    if source == "prices_daily":
        lf = scan_table("prices_daily")
        if start is not None:
            lf = lf.filter(pl.col("date") >= start)
        if end is not None:
            lf = lf.filter(pl.col("date") <= end)
        if security_ids is not None:
            lf = lf.filter(pl.col("security_id").is_in(list(security_ids)))
        f = pl.col("adj_factor").fill_null(1.0)
        f = pl.when(f > 0).then(f).otherwise(1.0)
        sf = pl.col("split_factor").fill_null(1.0)
        sf = pl.when(sf > 0).then(sf).otherwise(1.0)
        lf = lf.select(
            "security_id", "date", "region",
            (pl.col("open") * f).alias("open"), (pl.col("high") * f).alias("high"),
            (pl.col("low") * f).alias("low"), (pl.col("close") * f).alias("close"),
            (pl.col("volume") / sf).alias("volume"),
        )
        df = lf.collect()
    elif source == "staging":
        lf = scan_table("prices_staging").filter(pl.col("source_id") == "yfinance")
        if start is not None:
            lf = lf.filter(pl.col("date") >= start)
        if end is not None:
            lf = lf.filter(pl.col("date") <= end)
        if security_ids is not None:
            lf = lf.filter(pl.col("symbol").is_in(list(security_ids)))
        f = pl.when((pl.col("adj_close") > 0) & (pl.col("close") > 0)).then(pl.col("adj_close") / pl.col("close")).otherwise(1.0)
        df = (lf.sort("ingested_at").unique(subset=["symbol", "date"], keep="last")
              .select(pl.col("symbol").alias("security_id"), "date",
                      (pl.col("open") * f).alias("open"), (pl.col("high") * f).alias("high"),
                      (pl.col("low") * f).alias("low"), (pl.col("close") * f).alias("close"),
                      pl.col("volume"))
              .collect())
        df = df.with_columns(pl.col("security_id").map_elements(region_from_symbol, return_dtype=pl.Utf8).alias("region"))
    else:
        raise ValueError(f"unknown price source {source!r}")
    df = _clean_ohlc(df)
    return df.unique(subset=["security_id", "date"], keep="last").sort(["security_id", "date"])


_SUFFIX_REGION = {
    "L": "UK", "T": "JP", "KS": "KR", "KQ": "KR", "HK": "HK", "SS": "CN", "SZ": "CN", "SA": "LATAM", "MX": "LATAM",
    "SN": "LATAM", "TO": "CA", "V": "CA", "AX": "APAC", "NS": "APAC", "BO": "APAC", "TW": "APAC", "SI": "APAC",
    "PA": "EU", "DE": "EU", "F": "EU", "AS": "EU", "MI": "EU", "MC": "EU", "SW": "EU", "ST": "EU", "CO": "EU",
    "HE": "EU", "OL": "EU", "BR": "EU", "LS": "EU", "VI": "EU", "IR": "EU",
}


def region_from_symbol(sym: str) -> str:
    if sym.startswith("^"):
        return "INDEX"
    if "." in sym:
        return _SUFFIX_REGION.get(sym.rsplit(".", 1)[1], "OTHER")
    return "US"


def load_meta(security_ids: Sequence[str] | None = None) -> pl.DataFrame:
    """security_id, issuer_id, region, sector_id from reference tables (empty frame if not built yet)."""
    try:
        sm = scan_table("securities_master")
        has_type = "security_type" in sm.collect_schema().names()
        sm = sm.select("security_id", "issuer_id", *(["security_type"] if has_type else [])).collect()
        iss = scan_table("issuers").select("issuer_id", "region", "sector_id").collect()
    except Exception:  # noqa: BLE001
        return pl.DataFrame(schema={"security_id": pl.Utf8, "issuer_id": pl.Utf8, "region": pl.Utf8, "sector_id": pl.Utf8})
    df = sm.join(iss, on="issuer_id", how="left")
    if has_type:
        # market instruments (FX pairs, futures, indices incl. volatility indices, ETFs) get their own reliability
        # pool ("_fx|FX", "_future|US", ...) instead of sharing "NA|<region>" with unclassified equities
        df = df.with_columns(
            pl.when(pl.col("security_type").is_in(list(INSTRUMENT_TYPES)) & pl.col("sector_id").is_null())
            .then(pl.lit("_") + pl.col("security_type")).otherwise(pl.col("sector_id")).alias("sector_id")
        ).drop("security_type")
    if security_ids is None:
        return df
    ids = list(security_ids)
    out = df.filter(pl.col("security_id").is_in(ids))
    # provisional price keys ("P:<yf ticker>") written before identity resolution: map through listings.yf_ticker
    prov = [s for s in ids if s.startswith("P:")]
    if prov:
        try:
            li = scan_table("listings").select("security_id", "yf_ticker").filter(pl.col("yf_ticker").is_not_null()).collect()
        except Exception:  # noqa: BLE001
            li = pl.DataFrame(schema={"security_id": pl.Utf8, "yf_ticker": pl.Utf8})
        pm = (pl.DataFrame({"prov_id": prov}).with_columns(pl.col("prov_id").str.slice(2).alias("yf_ticker"))
              .join(li.unique(subset=["yf_ticker"]), on="yf_ticker", how="inner")
              .join(df, on="security_id", how="inner")
              .drop("security_id", "yf_ticker").rename({"prov_id": "security_id"}))
        out = pl.concat([out, pm.select(out.columns)], how="vertical_relaxed")
    return out


def load_earnings_dates(meta: pl.DataFrame) -> dict[str, np.ndarray]:
    """security_id -> sorted datetime64[D] of periodic-report knowledge dates (annual/semiannual/quarterly)."""
    if meta.is_empty():
        return {}
    try:
        fi = (scan_table("filings_index")
              .filter(pl.col("form_class").is_in(["annual", "semiannual", "quarterly"]) & pl.col("issuer_id").is_not_null())
              .select("issuer_id", "knowledge_date").unique().collect())
    except Exception:  # noqa: BLE001
        return {}
    if fi.is_empty():
        return {}
    j = meta.select("security_id", "issuer_id").join(fi, on="issuer_id", how="inner")
    out: dict[str, np.ndarray] = {}
    for (sid,), g in j.group_by(["security_id"]):
        out[str(sid)] = np.sort(g["knowledge_date"].to_numpy().astype("datetime64[D]"))
    return out


def load_ath_prior(security_ids: Sequence[str], before: date, source: str = "prices_daily") -> dict[str, float]:
    """Max adjusted high strictly before `before` per security (for all_time_high when only a window is loaded)."""
    if source == "prices_daily":
        lf = scan_table("prices_daily").filter((pl.col("date") < before) & pl.col("security_id").is_in(list(security_ids)))
        f = pl.col("adj_factor").fill_null(1.0)
        lf = lf.group_by("security_id").agg((pl.coalesce(pl.col("high"), pl.col("close")) * f).max().alias("ath"))
    else:
        lf = scan_table("prices_staging").filter(
            (pl.col("source_id") == "yfinance") & (pl.col("date") < before) & pl.col("symbol").is_in(list(security_ids)))
        f = pl.when((pl.col("adj_close") > 0) & (pl.col("close") > 0)).then(pl.col("adj_close") / pl.col("close")).otherwise(1.0)
        lf = lf.group_by(pl.col("symbol").alias("security_id")).agg((pl.coalesce(pl.col("high"), pl.col("close")) * f).max().alias("ath"))
    df = lf.collect()
    return {r[0]: float(r[1]) for r in df.iter_rows() if r[1] is not None}


def iter_bars(panel: pl.DataFrame, meta: pl.DataFrame | None = None, earnings: dict[str, np.ndarray] | None = None,
              ath_prior: dict[str, float] | None = None, min_bars: int = 60) -> Iterator[Bars]:
    """Split a sorted panel into Bars objects (securities with fewer than min_bars bars are skipped)."""
    m = {}
    if meta is not None and not meta.is_empty():
        m = {r["security_id"]: r for r in meta.iter_rows(named=True)}
    if panel.is_empty():
        return
    for (sid,), g in panel.group_by(["security_id"], maintain_order=True):
        if g.height < min_bars:
            continue
        sid = str(sid)
        mr = m.get(sid, {})
        region = mr.get("region") or (g["region"][0] if "region" in g.columns else None)
        yield Bars(
            security_id=sid,
            dates=g["date"].to_numpy().astype("datetime64[D]"),
            o=g["open"].to_numpy().astype(np.float64), h=g["high"].to_numpy().astype(np.float64),
            l=g["low"].to_numpy().astype(np.float64), c=g["close"].to_numpy().astype(np.float64),
            v=g["volume"].to_numpy().astype(np.float64),
            ath_prior=(ath_prior or {}).get(sid, float("nan")),
            earnings=(earnings or {}).get(sid, np.array([], dtype="datetime64[D]")),
            region=region, sector_id=mr.get("sector_id"),
        )


def _price_stats_duckdb(since: date | None) -> pl.DataFrame | None:
    """security_id, last, bars of prices_daily via DuckDB over the year partitions >= since.year (A1 2026-11: the
    Polars group_by over the lake committed ~1.7 GB for 24k securities - kept by the allocator for the rest of the
    job - DuckDB ~0.1 GB). Same rows as the Polars path: physical security_id / date are the logical values (no
    storage narrowing), in-flight .tmp- / .old- swap dirs are skipped as in scan_table. None = not available."""
    from app.core.storage import _parquet_files, duckdb_connect, table_dir

    try:
        root = table_dir("prices_daily")
        files = []
        for f in _parquet_files(root):
            kv = dict(x.split("=", 1) for x in f.relative_to(root).parent.parts if "=" in x)
            y = kv.get("year")
            if since is not None and y not in (None, "__null__") and y.lstrip("-").isdigit() and int(y) < since.year:
                continue
            files.append(f.as_posix().replace("'", "''"))
        if not files:
            return pl.DataFrame(schema={"security_id": pl.Utf8, "last": pl.Date, "bars": pl.UInt32})
        con = duckdb_connect()
        try:
            con.execute("SET memory_limit = '1GB'")
            con.execute("SET preserve_insertion_order = false")
            where = f"WHERE date >= DATE '{since.isoformat()}'" if since is not None else ""
            lst = "[" + ", ".join(f"'{f}'" for f in files) + "]"
            return con.sql(f"SELECT security_id, max(date) AS last, count(DISTINCT date) AS bars "
                           f"FROM read_parquet({lst}, hive_partitioning = false, union_by_name = true) {where} "
                           f"GROUP BY security_id").pl()
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 - fall back to the Polars path
        log.warning("list_securities: DuckDB path unavailable (%s: %s); using Polars", type(e).__name__, e)
        return None


def list_securities(source: str = "prices_daily", min_last_date: date | None = None, since: date | None = None,
                    min_bars: int = 0) -> list[str]:
    """Securities with prices: optionally trading on/after min_last_date and with >= min_bars bars since `since`."""
    df = _price_stats_duckdb(since) if source == "prices_daily" else None
    if df is None:
        if source == "prices_daily":
            lf = scan_table("prices_daily").select("security_id", "date")
        else:
            lf = scan_table("prices_staging").filter(pl.col("source_id") == "yfinance").select(pl.col("symbol").alias("security_id"), "date")
        if since is not None:
            lf = lf.filter(pl.col("date") >= since)
        df = lf.group_by("security_id").agg(pl.col("date").max().alias("last"), pl.col("date").n_unique().alias("bars")).collect()
    if min_last_date is not None:
        df = df.filter(pl.col("last") >= min_last_date)
    if min_bars:
        df = df.filter(pl.col("bars") >= min_bars)
    return sorted(df["security_id"].drop_nulls().to_list())


def years_before(d: date, years: float) -> date:
    return d - timedelta(days=int(round(365.25 * years)))
