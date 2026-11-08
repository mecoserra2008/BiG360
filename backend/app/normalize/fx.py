"""FX conversion helpers over the `fx_rates` table (written by ingest-market). Owner: ref-identity.

Rules (ARCHITECTURE §8):
  * balance-sheet (INSTANT) items -> rate at period_end; if no fixing that day, the last fixing on or before it
    (at most `max_stale_days` old).
  * flow items (FY/Q/H/TTM durations) -> period average = arithmetic mean of the available daily rates in
    [period_start, period_end].
  * TTM in USD = sum of the discrete quarters' USD values (done by app.normalize.fundamentals, not here).
  * Sub-units: GBp/GBX = GBP/100, ZAc = ZAR/100, ILA = ILS/100.

Public API
    load_rates(currencies=None, start=None, end=None, settings=None) -> pl.DataFrame[date, currency, usd_per_unit, source_id]
    rate_on(currency, d, rates=None, max_stale_days=7) -> (usd_per_unit, source_id) | None
    period_average(currency, start, end, rates=None) -> (usd_per_unit, source_id) | None
    to_usd(value, currency, d=None, start=None, end=None, rates=None) -> float | None
    convert_frame(df, rates=None, *, value_col="value", currency_col="currency", start_col="period_start",
                  end_col="period_end", kind_col="period_kind", out_value="value_usd", max_stale_days=7) -> pl.DataFrame
        vectorized; adds value_usd, fx_rate, fx_method (period_end|period_average|none), fx_source.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.logsetup import get_logger

log = get_logger(__name__)

SUBUNITS: dict[str, tuple[str, float]] = {"GBP": ("GBP", 1.0), "GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01),
                                          "ZAc": ("ZAR", 0.01), "ZAC": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}
RATE_SCHEMA = {"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64, "source_id": pl.Utf8}


def _d(x: date | datetime | str | None) -> date | None:
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def base_currency(ccy: str | None) -> tuple[str | None, float]:
    """('GBp') -> ('GBP', 0.01); ('eur') -> ('EUR', 1.0)."""
    if not ccy:
        return None, 1.0
    if ccy in SUBUNITS:
        return SUBUNITS[ccy]
    return ccy.upper(), 1.0


def load_rates(currencies: list[str] | None = None, start: date | None = None, end: date | None = None,
               settings=None) -> pl.DataFrame:
    """Daily USD-per-unit fixings from fx_rates (USD synthesized as 1.0 when absent)."""
    from app.core.storage import scan_table

    lf = scan_table("fx_rates", settings).select("date", "currency", "usd_per_unit", "source_id")
    if currencies:
        lf = lf.filter(pl.col("currency").is_in([base_currency(c)[0] for c in currencies]))
    if start:
        lf = lf.filter(pl.col("date") >= start - timedelta(days=10))
    if end:
        lf = lf.filter(pl.col("date") <= end)
    return lf.collect().cast(RATE_SCHEMA).sort(["currency", "date"])  # type: ignore[arg-type]


def _rates(rates: pl.DataFrame | None, currencies: list[str] | None = None) -> pl.DataFrame:
    if rates is None:
        try:
            rates = load_rates(currencies)
        except Exception as e:  # noqa: BLE001
            log.warning("fx_rates unavailable: %s", e)
            rates = pl.DataFrame(schema=RATE_SCHEMA)
    return rates.select(list(RATE_SCHEMA)).cast(RATE_SCHEMA)  # type: ignore[arg-type]


def rate_on(currency: str, d: date | datetime | str, rates: pl.DataFrame | None = None,
            max_stale_days: int = 7) -> tuple[float, str] | None:
    """Last fixing on or before d (<= max_stale_days old). USD -> (1.0, 'identity')."""
    base, mult = base_currency(currency)
    if base is None:
        return None
    if base == "USD":
        return (1.0 * mult, "identity")
    dd = _d(d)
    r = _rates(rates, [base]).filter((pl.col("currency") == base) & (pl.col("date") <= dd)
                                     & (pl.col("date") >= dd - timedelta(days=max_stale_days)))
    if r.is_empty():
        return None
    row = r.sort("date").tail(1).row(0, named=True)
    return float(row["usd_per_unit"]) * mult, row["source_id"]


def period_average(currency: str, start: date | datetime | str, end: date | datetime | str,
                   rates: pl.DataFrame | None = None) -> tuple[float, str] | None:
    """Mean of daily fixings in [start, end]; source = most frequent source_id in the window."""
    base, mult = base_currency(currency)
    if base is None:
        return None
    if base == "USD":
        return (1.0 * mult, "identity")
    s, e = _d(start), _d(end)
    r = _rates(rates, [base]).filter((pl.col("currency") == base) & (pl.col("date") >= s) & (pl.col("date") <= e))
    if r.is_empty():
        return None
    src = r.group_by("source_id").len().sort("len", descending=True).item(0, "source_id")
    return float(r.get_column("usd_per_unit").mean()) * mult, src  # type: ignore[arg-type]


def to_usd(value: float | None, currency: str | None, d: date | None = None, start: date | None = None,
           end: date | None = None, rates: pl.DataFrame | None = None) -> float | None:
    """Scalar conversion: period average when start and end given, else the rate on d (or end)."""
    if value is None or currency is None:
        return None
    res = period_average(currency, start, end, rates) if (start and end) else rate_on(currency, d or end, rates)  # type: ignore[arg-type]
    return value * res[0] if res else None


def convert_frame(df: pl.DataFrame, rates: pl.DataFrame | None = None, *, value_col: str = "value",
                  currency_col: str = "currency", start_col: str = "period_start", end_col: str = "period_end",
                  kind_col: str | None = "period_kind", out_value: str = "value_usd",
                  max_stale_days: int = 7) -> pl.DataFrame:
    """Vectorized conversion. Rows with kind INSTANT (or no start) use the period-end rate; others the period
    average over [start, end] (falling back to the period-end rate if the window has no fixings).
    Rows with null currency (non-monetary) get value_usd null and fx_method 'none'."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, pl.Float64).alias(out_value), pl.lit(None, pl.Float64).alias("fx_rate"),
                               pl.lit(None, pl.Utf8).alias("fx_method"), pl.lit(None, pl.Utf8).alias("fx_source"))
    ccys = [c for c in df.get_column(currency_col).drop_nulls().unique().to_list()]
    rt = _rates(rates, ccys)
    base_map = {c: base_currency(c) for c in ccys}
    work = df.with_row_index("__r").with_columns(
        pl.col(currency_col).replace_strict({c: b[0] for c, b in base_map.items()}, default=None).alias("__base"),
        pl.col(currency_col).replace_strict({c: b[1] for c, b in base_map.items()}, default=1.0, return_dtype=pl.Float64).alias("__mult"),
        pl.col(end_col).cast(pl.Date).alias("__end"),
        (pl.col(start_col).cast(pl.Date) if start_col in df.columns else pl.lit(None, pl.Date)).alias("__start"),
    )
    is_inst = (pl.col(kind_col) == "INSTANT") if kind_col and kind_col in df.columns else pl.lit(False)
    work = work.with_columns((is_inst | pl.col("__start").is_null()).alias("__inst"))
    # USD identity rows
    usd = pl.DataFrame({"date": [date(1900, 1, 1)], "currency": ["USD"], "usd_per_unit": [1.0], "source_id": ["identity"]},
                       schema=RATE_SCHEMA)
    rt = pl.concat([rt.filter(pl.col("currency") != "USD"), usd]).sort(["currency", "date"])
    # ---- period-end rate (as-of backward with staleness tolerance; USD has no tolerance)
    pe = work.select("__r", "__base", "__end").filter(pl.col("__base").is_not_null()).sort("__end")
    pe = pe.join_asof(rt.rename({"currency": "__base", "date": "__rd"}).sort("__rd"), left_on="__end", right_on="__rd",
                      by="__base", strategy="backward", check_sortedness=False)
    pe = pe.with_columns(
        pl.when((pl.col("__base") == "USD") | ((pl.col("__end") - pl.col("__rd")).dt.total_days() <= max_stale_days))
        .then(pl.col("usd_per_unit")).otherwise(None).alias("__pe_rate"),
        pl.col("source_id").alias("__pe_src"),
    ).select("__r", "__pe_rate", "__pe_src")
    # ---- period average via cumulative sums
    cum = rt.with_columns(pl.col("usd_per_unit").cum_sum().over("currency").alias("__cs"),
                          pl.col("usd_per_unit").cum_count().over("currency").alias("__cn")).sort("date")
    cum_r = cum.rename({"currency": "__base", "date": "__cd"}).select("__base", "__cd", "__cs", "__cn", "source_id")
    av = work.filter(~pl.col("__inst") & pl.col("__base").is_not_null()).select("__r", "__base", "__start", "__end")
    if av.height:
        a_end = av.sort("__end").join_asof(cum_r.sort("__cd"), left_on="__end", right_on="__cd", by="__base",
                                           strategy="backward", check_sortedness=False)
        a_end = a_end.select("__r", pl.col("__cs").alias("__cs_e"), pl.col("__cn").alias("__cn_e"),
                             pl.col("source_id").alias("__av_src"))
        a_st = av.with_columns((pl.col("__start") - pl.duration(days=1)).alias("__sm1")).sort("__sm1").join_asof(
            cum_r.sort("__cd"), left_on="__sm1", right_on="__cd", by="__base", strategy="backward", check_sortedness=False)
        a_st = a_st.select("__r", pl.col("__cs").fill_null(0.0).alias("__cs_s"), pl.col("__cn").fill_null(0).alias("__cn_s"))
        avg = a_end.join(a_st, on="__r", how="left").with_columns(
            ((pl.col("__cs_e") - pl.col("__cs_s")) / (pl.col("__cn_e") - pl.col("__cn_s")).cast(pl.Float64)).alias("__av_rate"),
            (pl.col("__cn_e") - pl.col("__cn_s")).alias("__av_n"))
        avg = avg.with_columns(pl.when(pl.col("__av_n") > 0).then(pl.col("__av_rate")).otherwise(None).alias("__av_rate"))
        avg = avg.select("__r", "__av_rate", "__av_src")
    else:
        avg = pl.DataFrame(schema={"__r": pl.UInt32, "__av_rate": pl.Float64, "__av_src": pl.Utf8})
    out = work.join(pe, on="__r", how="left").join(avg, on="__r", how="left")
    usd_row = pl.col("__base") == "USD"
    method = (pl.when(pl.col("__base").is_null()).then(pl.lit("none"))
              .when(usd_row).then(pl.when(pl.col("__inst")).then(pl.lit("period_end")).otherwise(pl.lit("period_average")))
              .when(~pl.col("__inst") & pl.col("__av_rate").is_not_null()).then(pl.lit("period_average"))
              .when(pl.col("__pe_rate").is_not_null()).then(pl.lit("period_end"))
              .otherwise(pl.lit("none")))
    rate = (pl.when(usd_row).then(pl.lit(1.0))
            .when(~pl.col("__inst") & pl.col("__av_rate").is_not_null()).then(pl.col("__av_rate"))
            .otherwise(pl.col("__pe_rate"))) * pl.col("__mult")
    src = (pl.when(usd_row).then(pl.lit("identity"))
           .when(~pl.col("__inst") & pl.col("__av_rate").is_not_null()).then(pl.col("__av_src"))
           .otherwise(pl.col("__pe_src")))
    out = out.with_columns(method.alias("fx_method"), rate.alias("fx_rate"), src.alias("fx_source"))
    out = out.with_columns(
        pl.when(pl.col("fx_method") == "none").then(None).otherwise(pl.col("fx_rate")).alias("fx_rate"),
        pl.when(pl.col("fx_method") == "none").then(None).otherwise(pl.col("fx_source")).alias("fx_source"),
    ).with_columns((pl.col(value_col) * pl.col("fx_rate")).alias(out_value))
    drop = [c for c in out.columns if c.startswith("__")]
    return out.sort("__r").drop(drop)


def coverage(rates: pl.DataFrame | None = None, currencies: list[str] | None = None,
             start: date | None = None, end: date | None = None) -> dict[str, Any]:
    """Per currency: first/last fixing and count (dq check helper)."""
    r = _rates(rates, currencies)
    if start:
        r = r.filter(pl.col("date") >= start)
    if end:
        r = r.filter(pl.col("date") <= end)
    g = r.group_by("currency").agg(pl.col("date").min().alias("first"), pl.col("date").max().alias("last"), pl.len().alias("n"))
    return {row["currency"]: {"first": row["first"], "last": row["last"], "n": row["n"]} for row in g.iter_rows(named=True)}
