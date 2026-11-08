"""Forward-looking ML labels. Job `ml_labels` (weekly). Owner: ml. Writes `ml_labels` only (never `ml_features`,
to keep leakage impossible by construction: a feature pipeline bug can never see a label column).

Rebalance calendar: every ISO-week's last trading day (effectively "Friday", using the actual trading calendar so
holiday weeks don't produce a bad date) union every month's last trading day, derived from the `IDX:^GSPC`
calendar (ARCHITECTURE §7 / jobs.yaml note). `app.ml.features` imports `rebalance_dates()` from here so the two
tables share one date grid.

Label definition (ARCHITECTURE §7): forward return runs from close(D+1) to close(D+1+h) -- the first trading day
*strictly after* D is the entry (so nothing published on D's close itself, or needed to trade at D's close, leaks
into the label), and the exit is h trading days later **on that security's own trading calendar** (21/63/126 for
1M/3M/6M). `fwd_excess_sector` subtracts the equal-weighted mean forward return of the training universe within
the same (date, horizon, sector_id, region) bucket; `rank_label` is the cross-sectional percentile of
`fwd_excess_sector` within (date, horizon) over the whole universe (not sector-bucketed).

Survivorship / delisting (honest limitation, flagged per the brief): yfinance (our only price source for most of
the non-US universe, and the backfill source for pre-Polygon US history) does not carry delisted tickers, so a
name that leaves `universe_membership` without an `is_active=false` row in `securities_master` *before* delisting
is simply missing from our price history rather than correctly scored with a terminal return. For names we *do*
observe delisting for (`is_active=false`, `last_trade_date` set), the label uses the last available close as a
flat (zero-drift) continuation and sets `delisted_in_window=true`, per the contract -- this avoids survivorship
bias for the names we can see delist, but the broader survivorship gap for names we never ingested at all remains
and is recorded in `model_runs.notes` by `app.ml.train` for every run built on these labels.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table

log = get_logger(__name__)

MARKET_ID = "IDX:^GSPC"
HORIZONS: dict[str, int] = {"1M": 21, "3M": 63, "6M": 126}
DEFAULT_UNIVERSES = ("mvp_us", "mvp_global")


def rebalance_dates(settings: Settings | None = None, start: date | None = None, end: date | None = None) -> list[date]:
    s = settings or get_settings()
    cal = scan_table("prices_daily", s).filter(pl.col("security_id") == MARKET_ID).select("date").collect()
    if cal.is_empty():  # fallback: union of all trading dates if the benchmark itself is missing
        cal = scan_table("prices_daily", s).select("date").unique().collect()
    df = cal.with_columns(
        pl.col("date").dt.year().alias("y"), pl.col("date").dt.week().alias("w"),
        pl.col("date").dt.truncate("1mo").alias("m"),
    )
    fridays = df.group_by(["y", "w"]).agg(pl.col("date").max())["date"]
    month_ends = df.group_by("m").agg(pl.col("date").max())["date"]
    dates = pl.concat([fridays, month_ends]).unique().sort()
    if start:
        dates = dates.filter(dates >= start)
    if end:
        dates = dates.filter(dates <= end)
    return dates.to_list()


def _universe_frame(universe_ids: tuple[str, ...], settings: Settings) -> pl.DataFrame:
    """[security_id, valid_from, valid_to] union of the requested universes (membership intervals kept distinct
    per security so a name's PIT universe status at any rebalance date D can be checked without surviorship bias:
    a name that later left the universe still had rows while it was a member)."""
    um = read_table("universe_membership", settings=settings)
    um = um.filter(pl.col("universe_id").is_in(list(universe_ids)))
    return um.select("security_id", "valid_from", "valid_to").unique()


def _sector_region(settings: Settings, members: list[str] | None = None) -> pl.DataFrame:
    from app.normalize.prices import country_to_region  # noqa: PLC0415

    sec = read_table("securities_master", columns=["security_id", "issuer_id", "primary_listing_id"],
                     settings=settings)
    iss = read_table("issuers", columns=["issuer_id", "sector_id"], settings=settings)
    lst = read_table("listings", columns=["listing_id", "exchange_country"], settings=settings)
    px = scan_table("prices_daily", settings)
    if members is not None:  # W1 2026-10-31: the label universe only (all 99 M rows OOM-killed the 6 GB job)
        px = px.filter(pl.col("security_id").is_in(members))
    region = px.select("security_id", "region").unique().collect(engine="streaming")
    j = (sec.join(iss, on="issuer_id", how="left")
        .join(lst, left_on="primary_listing_id", right_on="listing_id", how="left")
        .join(region, on="security_id", how="left"))
    j = j.with_columns(
        pl.when(pl.col("region").is_null())
        .then(pl.col("exchange_country").map_elements(country_to_region, return_dtype=pl.Utf8))
        .otherwise(pl.col("region")).fill_null("OTHER").alias("region"),
        pl.col("sector_id").fill_null("UNKNOWN"),
    )
    # one row per security (W1 2026-10-31: a security with two primary-listing rows / two price regions produced
    # duplicate label keys - 7,656 rows in the weekly run of 10-31)
    return j.select("security_id", "sector_id", "region").unique("security_id", keep="first", maintain_order=True)


def _entry_exit(prices: pl.DataFrame, rebal: list[date], h: int) -> pl.DataFrame:
    """For every (security_id, D in rebal): entry = first trading row with date > D; exit = h rows later in that
    security's own sequence. Returns [security_id, date(=D), entry_date, entry_price, exit_date, exit_price,
    last_date, last_price, is_last_available] (exit_* null when the security's history doesn't reach that far)."""
    px = prices.sort(["security_id", "date"]).with_columns(
        pl.col("date").cum_count().over("security_id").alias("row_idx"))
    last = px.group_by("security_id").agg(pl.col("date").last().alias("last_date"),
                                          pl.col("adj_close").last().alias("last_price"),
                                          pl.col("row_idx").last().alias("last_idx"))
    reb = pl.DataFrame({"date": rebal}).with_columns((pl.col("date") + timedelta(days=1)).alias("__d1"))
    secs = px.select("security_id").unique()
    grid = secs.join(reb, how="cross")
    entry = (
        grid.sort(["security_id", "__d1"])
        .join_asof(px.select("security_id", pl.col("date").alias("entry_date"), "row_idx",
                             pl.col("adj_close").alias("entry_price")).sort(["security_id", "entry_date"]),
                  left_on="__d1", right_on="entry_date", by="security_id", strategy="forward")
    )
    exit_ = px.select("security_id", pl.col("row_idx").alias("__exit_idx"), pl.col("date").alias("exit_date"),
                      pl.col("adj_close").alias("exit_price"))
    out = entry.with_columns((pl.col("row_idx") + h).alias("__exit_idx")).join(
        exit_, on=["security_id", "__exit_idx"], how="left")
    out = out.join(last, on="security_id", how="left")
    return out.select("security_id", "date", "entry_date", "entry_price", "exit_date", "exit_price",
                      "last_date", "last_price", "last_idx", (pl.col("row_idx") + h).alias("target_idx"))


def compute_labels(as_of_end: date, settings: Settings, universe_ids: tuple[str, ...] = DEFAULT_UNIVERSES,
                   start: date | None = None) -> pl.DataFrame:
    """Full ml_labels frame for every horizon, rebalance dates in [start, as_of_end]."""
    uni = _universe_frame(universe_ids, settings)
    # `universe_membership` is a current-only snapshot at MVP (ref-identity has not back-filled historical
    # valid_from/valid_to intervals yet): if every row's valid_from is later than the label window, PIT interval
    # filtering would silently drop all history, so fall back to treating membership as a static cross-section
    # applied to the whole window. This is a known survivorship simplification (today's constituents applied
    # retroactively) -- flagged in `model_runs.notes` by `app.ml.train`, not hidden here.
    pit_membership_available = bool(uni.height) and uni["valid_from"].min() <= (start or date(1990, 1, 1))
    members = uni["security_id"].unique().to_list()
    sec_region = _sector_region(settings, members)
    prices = (scan_table("prices_daily", settings)
             .filter(pl.col("security_id").is_in(members))
             .select("security_id", "date", "adj_close").drop_nulls().collect(engine="streaming"))
    active = read_table("securities_master", columns=["security_id", "is_active", "last_trade_date"],
                        settings=settings)
    rebal = rebalance_dates(settings, start=start, end=as_of_end)
    frames = []
    now = datetime.now(UTC)
    for horizon, h in HORIZONS.items():
        ee = _entry_exit(prices, rebal, h)
        ee = ee.join(uni, on="security_id", how="inner")
        if pit_membership_available:
            ee = ee.filter((pl.col("date") >= pl.col("valid_from"))
                          & (pl.col("valid_to").is_null() | (pl.col("date") <= pl.col("valid_to"))))
        # a security with several membership intervals (two universes, or a re-entry such as SH8PZ5CTPJCAW
        # 10-10 / 10-23) must yield ONE label per (date, security): the PK of ml_labels is (date, security_id, horizon)
        ee = ee.drop("valid_from", "valid_to").unique(["security_id", "date"], keep="first", maintain_order=True)
        ee = ee.join(active.unique("security_id", keep="last"), on="security_id", how="left")
        has_exit = pl.col("exit_price").is_not_null()
        delisted_fallback = (~has_exit) & (pl.col("is_active") == False) & (pl.col("last_idx") < pl.col("target_idx"))  # noqa: E712
        unresolved = (~has_exit) & ~delisted_fallback
        fwd_return = (
            pl.when(has_exit).then(pl.col("exit_price") / pl.col("entry_price") - 1.0)
            .when(delisted_fallback).then(pl.col("last_price") / pl.col("entry_price") - 1.0)
            .otherwise(None)
        )
        label_end_date = (
            pl.when(has_exit).then(pl.col("exit_date"))
            .when(delisted_fallback).then(pl.col("last_date"))
            .otherwise(pl.col("date") + timedelta(days=1) + timedelta(days=round(h * 7 / 5)))
        )
        ee = ee.with_columns(
            pl.lit(horizon).alias("horizon"), fwd_return.alias("fwd_return"),
            label_end_date.cast(pl.Date).alias("label_end_date"),
            delisted_fallback.fill_null(False).alias("delisted_in_window"),
        )
        ee = ee.join(sec_region, on="security_id", how="left")
        sector_mean = ee.filter(pl.col("fwd_return").is_not_null()).group_by(
            ["date", "sector_id", "region"]).agg(pl.col("fwd_return").mean().alias("__sector_mean"))
        ee = ee.join(sector_mean, on=["date", "sector_id", "region"], how="left")
        ee = ee.with_columns((pl.col("fwd_return") - pl.col("__sector_mean")).alias("fwd_excess_sector"))
        ee = ee.with_columns(
            pl.when(pl.col("fwd_excess_sector").is_not_null())
            .then(pl.col("fwd_excess_sector").rank(method="average").over("date")
                 / pl.col("fwd_excess_sector").is_not_null().sum().over("date"))
            .otherwise(None).alias("rank_label")
        )
        ee = ee.with_columns(
            pl.when(pl.col("rank_label").is_not_null())
            .then((pl.col("rank_label") * 10).ceil().clip(1, 10).cast(pl.Int8))
            .otherwise(None).alias("decile")
        )
        frames.append(ee.select("date", "security_id", "horizon", "label_end_date", "fwd_return",
                                "fwd_excess_sector", "rank_label", "decile", "delisted_in_window"))
    out = pl.concat(frames, how="vertical_relaxed")
    return out.with_columns(pl.lit(now).alias("computed_at"))


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `ml_labels`. params: universe_ids, start (ISO date, default 2005-01-01), end (default ctx.as_of)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    start = date.fromisoformat(p["start"]) if p.get("start") else date(2005, 1, 1)
    end = date.fromisoformat(p["end"]) if p.get("end") else ctx.as_of
    universe_ids = tuple(p.get("universe_ids", DEFAULT_UNIVERSES))
    try:
        df = compute_labels(end, ctx.settings, universe_ids, start=start)
    except Exception as e:  # noqa: BLE001
        log.exception("ml_labels failed")
        return JobResult(status="failed", message=str(e))
    n = write_table(df, "ml_labels", settings=ctx.settings)
    realized = df.filter(pl.col("fwd_return").is_not_null()).height
    return JobResult(status="success", rows_written={"ml_labels": n}, entities_ok=df["security_id"].n_unique(),
                     watermark=end.isoformat(),
                     message=f"rows={n} realized={realized} delisted_flagged={df['delisted_in_window'].sum()}")
