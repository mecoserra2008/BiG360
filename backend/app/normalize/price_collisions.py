"""Ticker-collision price repair and price-identity checks (A6, docs/COVERAGE_V3_PLAN.md). Owner: ingest-market.

Incident (docs/audits/STATEMENTS_GAP_AUDIT_2026-11.md section 6.1): 59 small EU listings (Sofia XBUL, Cyprus XCYS, Malta
XMAL, Bratislava XBRA, Zagreb XZAG, Luxembourg XLUX) carried the Yahoo and Polygon bars of a US stock or fund with the
same bare ticker (CVS@XMAL = CVS Health, HON@XBRA = Honeywell, ETR@XBUL = Entergy, VIS@XZAG = a Vanguard ETF).

Root cause (fixed in app.normalize.prices, see `listing_key_expr`): no price source serves those venues, so their
listings have no yf_ticker / polygon_ticker, and the price universe used coalesce(yf_ticker, local_symbol) as the
Yahoo resolution key. The bare local ticker then matched the US Yahoo symbol, and Polygon grouped-daily tickers without
a polygon_ticker match fell back to the same key. load_universe kept one row per key, in an unordered row order, so for
31 of the 67 colliding pairs the US security itself left the price universe on some runs (HON, PG, SLB, MPC had no bars).

Repair (this module, run once after the fix; lake writes only for the affected security partitions):
    plan(settings)       contaminated prices_daily rows: every bar of a listing without a venue symbol whose close equals
                         the staged bar of the bare ticker on the same date (identity check against the US symbol's
                         series in prices_staging, plus the US twin's prices_daily series for the report)
    apply_plan(...)      backup -> data/backups/prices_collision_<date>.parquet (logical prices_daily rows, restorable
                         with write_table(df, "prices_daily")), delete under the prices_daily writer lock with the
                         price jobs' own partition writer (atomic swap per (region, year)), mark the listings with
                         reason mapping_gap (data/reference/seeds/price_mapping_gaps.csv), and request a full re-build of
                         the US twins (state force_full_securities, consumed by the next incremental prices_finalize)

    python -m app.normalize.price_collisions plan  [--out DIR]      # dry run: evidence + counts, no lake writes
    python -m app.normalize.price_collisions apply [--wait-min 60]  # refuses while a price job is running

Series removed before the repair (`removed_identity_evidence`): the first prices_finalize on the fixed resolver
(2026-11-06 23:19) dropped 40 colliding gap securities from the price universe (the US twin won the de-duplicated key)
and its incremental `delete` removed their bars, so no row backup exists for them. Their derived valuation_daily rows
still carry the US stock's price: a gap security without bars is marked mapping_gap too when those closes equal the
colliding symbol's staged close x the listing currency's FX rate on the same dates.

Derived rows (`purge_derived`, dry run unless --apply): price-derived tables written in upsert mode keep the rows built
from the colliding bars (valuation_latest = latest valuation_daily row per security, without a date bound, would show
CVS Health's price on CVS@XMAL indefinitely). Rows of the marked securities are backed up to
data/backups/prices_collision_derived_<date>/<table>.parquet and removed from the partitions that hold them, under each
table's writer lock, while none of the price / derived writer jobs runs:
    python -m app.normalize.price_collisions purge-derived [--apply] [--tables valuation_daily,...]

Twins (A6 review round 2, `restore_twins`): the repair is finished only when every US twin carries current bars again.
37 of the 67 twins had none after the first apply (they lost them while the collider held their key) and the forced
full re-build request was never served (two OOM-killed nightly runs). restore-twins re-builds the twins without current
bars (plus the open request) from their full staging history with prices_incremental.rebuild_securities - the price
job's own builder and partition writer under the prices_daily lock, no watermark moved - after backing up their stored
rows to data/backups/prices_collision_twins_<date>.parquet, and clears them from the request:
    python -m app.normalize.price_collisions restore-twins [--apply]
    python -m app.normalize.price_collisions verify        # read-only postconditions, exit 2 unless all hold

DQ (app.core.dq hook `price_identity`): `identity_collision_outcomes` (two securities with the same close on the same
dates: >= 15 shared sessions in the last 45 days, >= 90 % of the shorter series, >= 5 distinct prices),
`venue_gap_outcomes` (bars on a listing without any venue symbol), `derived_rows_without_venue_symbol` (recent
valuation_daily rows of such a listing) and `twins_without_bars` (a US twin of a marked listing without bars up to
7 days before the latest US bar; severity error).
"""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

#: price jobs that write prices_staging / prices_daily: the repair waits until none of them runs
PRICE_JOBS = ("prices_finalize", "prices_us_polygon", "prices_global_yf", "prices_kr_krx", "prices_cn_baostock",
              "prices_tw", "prices_br_b3", "corporate_actions", "backfill_prices")
#: sources whose staged symbols the old fallback resolved through the bare local ticker
COLLISION_SOURCES = ("polygon", "yfinance")
#: identity tolerance on the close (log ratio; float32 storage keeps ~7 significant digits)
MATCH_TOL = 0.005
MIN_OVERLAP = 5
MIN_MATCH_SHARE = 0.8
MAPPING_GAPS_FILE = "price_mapping_gaps.csv"
REASON = "mapping_gap"
#: DQ identity-collision thresholds
DQ_WINDOW_DAYS = 45
DQ_MIN_SHARED = 15
DQ_MIN_SHARE = 0.9
DQ_MIN_DISTINCT = 5
DQ_MAX_SAME_PRICE = 6  # (date, close) values shared by more securities are ignored (round prices, 0.01 penny lines)
#: removed-series identity (valuation close_usd vs staged close x FX): the FX fixing is the one prices_finalize used,
#: so the match is exact up to float32 storage; 1 % leaves room for a fixing revised after the valuation run
REMOVED_TOL = 0.01
#: price-derived tables in upsert / append mode: their rows of a security outlive its bars (overwrite-mode snapshot
#: tables such as tech_levels or tech_setups are re-written without them on their next run)
DERIVED_TABLES = ("valuation_daily", "conformal_envelopes", "tech_breakouts", "tech_factors")
#: their writer jobs: purge_derived waits until none of them (nor a price job) runs
DERIVED_JOBS = ("valuation_daily", "conformal_envelopes", "tech_breakouts", "tech_factors")


def _isin(col: str, values: Sequence[str]) -> pl.Expr:
    """is_in for lazy lake scans (app.analytics.shares_pit.isin_ids: a short plain list makes polars 1.44 read GBs)."""
    return pl.col(col).is_in(pl.Series(values=list(values), dtype=pl.Utf8).implode())


# ------------------------------------------------------------------------------------------------ candidates


def venue_gap_listings(listings: pl.DataFrame) -> pl.DataFrame:
    """Current primary listings that no price source can serve: no venue symbol (prices.listing_key_expr is null) and
    no Polygon ticker. [listing_id, security_id, mic, exchange_country, local_symbol]."""
    from app.normalize.prices import listing_key_expr  # noqa: PLC0415

    cur = listings.filter(pl.col("is_primary") & pl.col("valid_to").is_null())
    return (cur.filter(listing_key_expr().is_null() & pl.col("polygon_ticker").is_null())
            .select("listing_id", "security_id", "mic", "exchange_country", "local_symbol")
            .unique(subset=["security_id"], keep="first", maintain_order=True).sort("security_id"))


def collision_symbols(gaps: pl.DataFrame) -> pl.DataFrame:
    """(source_id, symbol, security_id) pairs the pre-fix resolver mapped onto the gap listings: Yahoo symbol == local
    ticker; Polygon ticker with '.' -> '-' == local ticker (the BRK.B -> BRK-B rule of resolve_symbols)."""
    loc = gaps.select("security_id", pl.col("local_symbol").alias("__loc"))
    yf = loc.select(pl.lit("yfinance").alias("source_id"), pl.col("__loc").alias("symbol"), "security_id")
    pg = pl.concat([loc.select(pl.lit("polygon").alias("source_id"), pl.col("__loc").alias("symbol"), "security_id"),
                    loc.filter(pl.col("__loc").str.contains("-")).select(
                        pl.lit("polygon").alias("source_id"), pl.col("__loc").str.replace_all("-", ".").alias("symbol"),
                        "security_id")], how="vertical")
    return pl.concat([yf, pg], how="vertical").unique(maintain_order=True)


# ------------------------------------------------------------------------------------------------ evidence (pure)


def identity_evidence(stored: pl.DataFrame, staged: pl.DataFrame, pairs: pl.DataFrame,
                      tol: float = MATCH_TOL) -> pl.DataFrame:
    """Per (security_id, source_id) of the stored rows: how many of its bars equal the staged bar of a colliding symbol
    on the same date. stored [security_id, source_id, date, close, split_factor]; staged [source_id, symbol, date,
    close]; pairs [source_id, symbol, security_id]. A Yahoo stored close is split-unadjusted while the staged one is
    split-adjusted, so a bar also matches when close x split_factor (or / split_factor) equals the staged close.
    Returns [security_id, source_id, symbol, n_rows, n_overlap, n_match, match_share, contaminated]."""
    st = staged.join(pairs, on=["source_id", "symbol"], how="inner").select(
        "security_id", "source_id", "symbol", "date", pl.col("close").alias("__sc"))
    j = stored.select("security_id", "source_id", "date", "close", "split_factor").join(
        st, on=["security_id", "source_id", "date"], how="left")
    r = (pl.col("close") / pl.col("__sc")).log().abs()
    rs = (pl.col("close") * pl.col("split_factor").fill_null(1.0) / pl.col("__sc")).log().abs()
    rd = (pl.col("close") / pl.col("split_factor").fill_null(1.0) / pl.col("__sc")).log().abs()
    ok = pl.col("__sc").is_not_null() & (pl.col("__sc") > 0) & (pl.col("close") > 0)
    j = j.with_columns(ok.alias("__ov"), (ok & (pl.min_horizontal(r, rs, rd) <= tol)).fill_null(False).alias("__m"))
    # a date may join several colliding symbols (polygon BRK.B and BRK-B): a bar counts once
    per_bar = j.group_by("security_id", "source_id", "date").agg(
        pl.col("__ov").any(), pl.col("__m").any(), pl.col("symbol").filter(pl.col("__m")).first().alias("symbol"))
    ev = per_bar.group_by("security_id", "source_id").agg(
        pl.len().alias("n_rows"), pl.col("__ov").sum().alias("n_overlap"), pl.col("__m").sum().alias("n_match"),
        pl.col("symbol").drop_nulls().mode().first().alias("symbol"))
    ev = ev.with_columns((pl.col("n_match") / pl.col("n_overlap").clip(lower_bound=1)).alias("match_share"))
    # the overlap minimum applies per security: a 1-bar Yahoo tail next to a 518-bar matching Polygon series is the
    # same collision (QNC / ELMT / MINE 2026-10-30)
    strong = (pl.col("n_overlap") >= MIN_OVERLAP) & (pl.col("match_share") >= MIN_MATCH_SHARE)
    ev = ev.with_columns(((pl.col("match_share") >= MIN_MATCH_SHARE) & (pl.col("n_overlap") > 0)
                          & strong.any().over("security_id")).alias("contaminated"))
    return ev.select("security_id", "source_id", "symbol", "n_rows", "n_overlap", "n_match", "match_share",
                     "contaminated").sort("security_id", "source_id")


def twin_match(stored: pl.DataFrame, twins: pl.DataFrame, twin_px: pl.DataFrame, tol: float = MATCH_TOL) -> pl.DataFrame:
    """Share of a gap security's bars whose close equals its US twin's prices_daily close on the same date (report
    only: the twin itself lost bars while it was out of the universe). twins [security_id, twin_security_id];
    twin_px [security_id, date, close]."""
    if twins.is_empty() or twin_px.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "twin_security_id": pl.Utf8, "twin_overlap": pl.UInt32,
                                    "twin_match_share": pl.Float64})
    t = twin_px.rename({"security_id": "twin_security_id", "close": "__tc"})
    j = stored.select("security_id", "date", "close").join(twins, on="security_id", how="inner").join(
        t, on=["twin_security_id", "date"], how="inner")
    j = j.with_columns(((pl.col("close") / pl.col("__tc")).log().abs() <= tol).fill_null(False).alias("__m"))
    return j.group_by("security_id", "twin_security_id").agg(
        pl.len().cast(pl.UInt32).alias("twin_overlap"), pl.col("__m").mean().alias("twin_match_share"))


_REMOVED_SCHEMA = {"security_id": pl.Utf8, "source_id": pl.Utf8, "symbol": pl.Utf8, "n_rows": pl.UInt32,
                   "n_overlap": pl.UInt32, "n_match": pl.UInt32, "match_share": pl.Float64, "contaminated": pl.Boolean}


def removed_identity_evidence(val: pl.DataFrame, staged: pl.DataFrame, pairs: pl.DataFrame, fx: pl.DataFrame,
                              tol: float = REMOVED_TOL) -> pl.DataFrame:
    """Pure (module doc): gap securities whose bars are already gone, judged by their derived closes.
    val [security_id, date, close_usd, currency] (valuation_daily rows + the listing's quote currency); staged
    [source_id, symbol, date, close]; pairs [source_id, symbol, security_id] (collision_symbols); fx [currency, date,
    usd_per_unit] (fx_rates; the last fixing on or before the date; USD = 1). A bar matches when close_usd equals the
    staged close x usd_per_unit within `tol` (log ratio). Returns _REMOVED_SCHEMA, one row per security; contaminated
    = >= MIN_OVERLAP overlapping dates and >= MIN_MATCH_SHARE of them matching."""
    if val.is_empty() or staged.is_empty() or pairs.is_empty():
        return pl.DataFrame(schema=_REMOVED_SCHEMA)
    v = val.select("security_id", "date", "close_usd", pl.col("currency").cast(pl.Utf8)).with_row_index("__o")
    f = (fx.select(pl.col("currency").cast(pl.Utf8), pl.col("date").alias("__fd"), "usd_per_unit").drop_nulls()
         .sort("currency", "__fd"))
    if f.height:
        v = (v.sort("currency", "date").join_asof(f, left_on="date", right_on="__fd", by="currency",
                                                   strategy="backward", check_sortedness=False)
             .sort("__o").drop("__fd"))
    else:
        v = v.with_columns(pl.lit(None, pl.Float64).alias("usd_per_unit"))
    v = v.with_columns(pl.when(pl.col("currency") == "USD").then(pl.lit(1.0)).otherwise(pl.col("usd_per_unit"))
                       .alias("__fx")).drop("__o", "usd_per_unit")
    st = staged.join(pairs, on=["source_id", "symbol"], how="inner").select(
        "security_id", "source_id", "symbol", "date", pl.col("close").alias("__sc"))
    j = v.join(st, on=["security_id", "date"], how="left")
    ok = (pl.col("__sc") > 0) & (pl.col("__fx") > 0) & (pl.col("close_usd") > 0)
    hit = ok & ((pl.col("close_usd") / (pl.col("__sc") * pl.col("__fx"))).log().abs() <= tol)
    j = j.with_columns(ok.fill_null(False).alias("__ov"), hit.fill_null(False).alias("__m"))
    per_bar = j.group_by("security_id", "date").agg(
        pl.col("__ov").any(), pl.col("__m").any(), pl.col("symbol").filter(pl.col("__m")).first().alias("symbol"),
        pl.col("source_id").filter(pl.col("__m")).first().alias("source_id"))
    ev = per_bar.group_by("security_id").agg(
        pl.len().cast(pl.UInt32).alias("n_rows"), pl.col("__ov").sum().cast(pl.UInt32).alias("n_overlap"),
        pl.col("__m").sum().cast(pl.UInt32).alias("n_match"), pl.col("symbol").drop_nulls().mode().first().alias("symbol"),
        pl.col("source_id").drop_nulls().unique().sort().str.join("+").alias("source_id"))
    ev = ev.with_columns((pl.col("n_match") / pl.col("n_overlap").clip(lower_bound=1)).alias("match_share"))
    ev = ev.with_columns(((pl.col("n_overlap") >= MIN_OVERLAP) & (pl.col("match_share") >= MIN_MATCH_SHARE))
                         .alias("contaminated"))
    return ev.select(list(_REMOVED_SCHEMA)).cast(_REMOVED_SCHEMA).sort("security_id")  # type: ignore[arg-type]


# ------------------------------------------------------------------------------------------------ plan


@dataclass
class CollisionPlan:
    gaps: pl.DataFrame                      # venue-gap listings (all, with or without bars)
    evidence: pl.DataFrame                  # per (security, source) identity evidence
    rows: pl.DataFrame                      # logical prices_daily rows to remove
    twins: pl.DataFrame                     # [security_id, twin_security_id] US securities sharing the bare ticker
    twin_report: pl.DataFrame
    leftover: pl.DataFrame                  # bars of gap listings not explained by a colliding symbol (kept)
    #: gap securities without bars whose derived closes are a colliding symbol's (removed before the repair)
    removed: pl.DataFrame = field(default_factory=lambda: pl.DataFrame(schema=_REMOVED_SCHEMA))
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def removed_contaminated(self) -> pl.DataFrame:
        r = self.removed
        return r.filter(pl.col("contaminated")) if r.height and "contaminated" in r.columns else r.head(0)

    def partitions(self) -> dict[tuple[str, int], int]:
        if self.rows.is_empty():
            return {}
        g = self.rows.group_by("region", "year").len()
        return {(r[0], int(r[1])): int(r[2]) for r in g.iter_rows()}

    def summary(self) -> dict[str, Any]:
        ev = self.evidence
        bad = ev.filter(pl.col("contaminated")) if ev.height else ev
        rem = self.removed_contaminated()
        return {
            "removed_earlier_securities": rem.height,
            "removed_earlier_by_mic": (self.gaps.filter(pl.col("security_id").is_in(rem.get_column("security_id")
                                                                                   .implode()))
                                       .group_by("mic").len().sort("mic").rows() if rem.height else []),
            "venue_gap_listings": self.gaps.height,
            "securities_with_bars": ev.get_column("security_id").n_unique() if ev.height else 0,
            "contaminated_securities": bad.get_column("security_id").n_unique() if bad.height else 0,
            "contaminated_series": bad.height,
            "rows_to_remove": self.rows.height,
            "leftover_rows": self.leftover.height,
            "partitions": {f"{k[0]}/{k[1]}": v for k, v in sorted(self.partitions().items())},
            "us_twins": self.twins.height,
            "by_mic": (self.gaps.filter(pl.col("security_id").is_in(bad.get_column("security_id").implode()))
                       .group_by("mic").len().sort("mic").rows() if bad.height else []),
        }


def _scan_staged(s: Settings, pairs: pl.DataFrame) -> pl.DataFrame:
    from app.normalize.prices_incremental import scan_partitions  # noqa: PLC0415

    syms = pairs.get_column("symbol").unique().to_list()
    lf = scan_partitions("prices_staging", s, {"source_id": lambda v: v in COLLISION_SOURCES})
    return (lf.filter(_isin("symbol", syms)).select("source_id", "symbol", "date", "close")
            .collect(engine="streaming").unique(subset=["source_id", "symbol", "date"], keep="last"))


def _derived_closes(s: Settings, security_ids: Sequence[str]) -> pl.DataFrame:
    """[security_id, date, close_usd] of valuation_daily (the derived table that carries a close) for these ids."""
    from app.core.storage import scan_table  # noqa: PLC0415

    empty = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "close_usd": pl.Float64})
    ids = list(security_ids)
    if not ids:
        return empty
    try:
        return (scan_table("valuation_daily", s).filter(_isin("security_id", ids), pl.col("close_usd") > 0)
                .select("security_id", "date", "close_usd").collect().unique(subset=["security_id", "date"], keep="last"))
    except Exception:  # noqa: BLE001 - no valuation table yet
        return empty


def _fx_rates(s: Settings, currencies: Sequence[str], lo: date, hi: date) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    cur = sorted({c for c in currencies if c and c != "USD"})
    if not cur:
        return pl.DataFrame(schema={"currency": pl.Utf8, "date": pl.Date, "usd_per_unit": pl.Float64})
    return (scan_table("fx_rates", s).filter(pl.col("currency").is_in(cur), pl.col("date") >= lo - timedelta(days=10),
                                             pl.col("date") <= hi)
            .select("currency", "date", "usd_per_unit").collect().unique(subset=["currency", "date"], keep="last"))


def plan(settings: Settings | None = None, securities: Sequence[str] | None = None) -> CollisionPlan:
    """Read-only: contaminated rows + evidence (see module doc). `securities`: restrict to these gap securities."""
    from app.core.storage import read_table, scan_table  # noqa: PLC0415
    from app.normalize.prices import listing_key_expr  # noqa: PLC0415

    s = settings or get_settings()
    lst = read_table("listings", settings=s)
    gaps = venue_gap_listings(lst)
    if securities:
        gaps = gaps.filter(pl.col("security_id").is_in(list(securities)))
    sids = gaps.get_column("security_id").to_list()
    stored = (scan_table("prices_daily", s).filter(_isin("security_id", sids)).collect()
              if sids else pl.DataFrame())
    with_bars = set(stored.get_column("security_id").unique().to_list()) if stored.height else set()
    # gap securities without bars that still carry derived closes: bars removed before this repair (module doc)
    val = _derived_closes(s, [x for x in sids if x not in with_bars])
    cand = with_bars | set(val.get_column("security_id").unique().to_list())
    pairs = collision_symbols(gaps.filter(pl.col("security_id").is_in(sorted(cand))))
    staged = (_scan_staged(s, pairs) if pairs.height
              else pl.DataFrame(schema={"source_id": pl.Utf8, "symbol": pl.Utf8, "date": pl.Date, "close": pl.Float64}))
    # US twins: current primary US listings whose key is the gap listing's bare ticker
    cur = lst.filter(pl.col("is_primary") & pl.col("valid_to").is_null())
    us = cur.filter(pl.col("exchange_country") == "US").select(listing_key_expr().alias("__k"),
                                                                pl.col("security_id").alias("twin_security_id"))
    twins = (gaps.select("security_id", pl.col("local_symbol").alias("__k")).join(us, on="__k", how="inner")
             .select("security_id", "twin_security_id").unique(maintain_order=True))
    removed = pl.DataFrame(schema=_REMOVED_SCHEMA)
    if val.height:
        ccy = lst.select("listing_id", pl.col("quote_currency").alias("currency"))
        v = val.join(gaps.select("security_id", "listing_id").join(ccy, on="listing_id", how="left").drop("listing_id"),
                     on="security_id", how="left")
        fx = _fx_rates(s, v.get_column("currency").drop_nulls().unique().to_list(), v.get_column("date").min(),
                       v.get_column("date").max())
        removed = removed_identity_evidence(v, staged, pairs, fx)
    if stored.is_empty():
        empty_ev = pl.DataFrame(schema={"security_id": pl.Utf8, "source_id": pl.Utf8, "contaminated": pl.Boolean})
        return CollisionPlan(gaps, empty_ev, stored, twins, pl.DataFrame(), stored, removed)
    ev = identity_evidence(stored, staged, pairs)
    bad = ev.filter(pl.col("contaminated")).select("security_id", "source_id")
    # only bars that are the colliding symbol's own staged bars are removed; anything else is reported, not touched
    st_keys = staged.join(pairs, on=["source_id", "symbol"], how="inner").select("security_id", "source_id", "date").unique()
    cand_rows = stored.join(bad, on=["security_id", "source_id"], how="semi")
    rows = cand_rows.join(st_keys, on=["security_id", "source_id", "date"], how="semi")
    leftover = stored.join(rows.select("security_id", "date"), on=["security_id", "date"], how="anti")
    twin_px = (scan_table("prices_daily", s).filter(_isin("security_id", twins.get_column("twin_security_id")
                                                         .unique().to_list()))
               .select("security_id", "date", "close").collect() if twins.height else pl.DataFrame())
    rep = twin_match(stored, twins, twin_px)
    return CollisionPlan(gaps, ev, rows, twins, rep, leftover, removed)


# ------------------------------------------------------------------------------------------------ apply


def running_price_jobs(settings: Settings | None = None) -> list[str]:
    from app.core.jobqueue import job_is_running  # noqa: PLC0415

    return [j for j in PRICE_JOBS if job_is_running(j, settings)]


def wait_for_price_jobs(settings: Settings | None = None, wait_min: float = 60.0, poll_s: float = 30.0) -> list[str]:
    """Wait (up to wait_min) until no price job runs; returns the jobs still running (empty = clear)."""
    end = time.monotonic() + wait_min * 60
    while True:
        busy = running_price_jobs(settings)
        if not busy or time.monotonic() >= end:
            return busy
        log.info("price_collisions: waiting for %s", busy)
        time.sleep(poll_s)


def backup_path(settings: Settings, day: date | None = None, prefix: str = "prices_collision") -> Path:
    d = settings.data_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{prefix}_{(day or datetime.now(UTC).date()).strftime('%Y%m%d')}"
    p = d / f"{stem}.parquet"
    i = 2
    while p.exists():
        p = d / f"{stem}-{i}.parquet"
        i += 1
    return p


def write_backup(pl_: CollisionPlan, settings: Settings | None = None, path: Path | None = None) -> Path:
    """Logical prices_daily rows (contract columns; restorable with write_table(df, 'prices_daily')) + a JSON sidecar
    with the evidence. Verified by re-reading before anything is deleted."""
    from app.core.schema import get_contract  # noqa: PLC0415

    s = settings or get_settings()
    p = path or backup_path(s)
    c = get_contract("prices_daily")
    df = pl_.rows.select([x for x in c.column_names if x in pl_.rows.columns])
    tmp = p.with_suffix(".parquet.tmp")
    df.write_parquet(tmp, compression="zstd", statistics=True)
    tmp.replace(p)
    back = pl.read_parquet(p)
    if back.height != df.height:
        raise RuntimeError(f"backup verification failed: {back.height} != {df.height} rows in {p}")
    side = {"created_at": pl_.created_at.isoformat(), "summary": pl_.summary(),
            "evidence": pl_.evidence.filter(pl.col("contaminated")).to_dicts(),
            "twins": pl_.twin_report.to_dicts() if pl_.twin_report.height else [],
            "restore": "write_table(pl.read_parquet(path), 'prices_daily') (upsert by security_id, date)"}
    p.with_suffix(".json").write_text(json.dumps(side, default=str, indent=1), encoding="utf-8")
    return p


def delete_rows(rows: pl.DataFrame, settings: Settings | None = None) -> dict[str, int]:
    """Remove `rows` ([security_id, date, region, year, ...]) from prices_daily: only the (region, year) partitions that
    hold them are re-written, under the table writer lock, with the price jobs' own physical writer + atomic swap
    (app.normalize.prices_incremental._merge_write). Returns {region/year: rows removed}."""
    from app.core.schema import conform, get_contract  # noqa: PLC0415
    from app.core.storage import (_rmtree, _scan_files, _swap_in, _write_partition_files, partition_dir,  # noqa: PLC0415
                                  table_dir, table_lock)

    s = settings or get_settings()
    if rows.is_empty():
        return {}
    c = get_contract("prices_daily")
    root = table_dir(c, s)
    pcols = list(c.partition_by)
    out: dict[str, int] = {}
    with table_lock("prices_daily", s):
        for (region, year), part in rows.partition_by(["region", "year"], as_dict=True, maintain_order=True).items():
            pdir = partition_dir(root, pcols, [region, year])
            files = list(pdir.glob("*.parquet")) if pdir.exists() else []
            if not files:
                continue
            old = _scan_files(files, c, root).collect()
            keys = part.select("security_id", "date").unique()
            keep = old.join(keys, on=["security_id", "date"], how="anti")
            n = old.height - keep.height
            if not n:
                continue
            if keep.is_empty():
                _rmtree(pdir)
            else:
                df = conform(keep.select(c.column_names), c).sort("security_id", "date")
                tmp = pdir.parent / f".tmp-{pdir.name}-{uuid.uuid4().hex[:8]}"
                _write_partition_files(tmp, df.drop(pcols), c)
                _swap_in(tmp, pdir)
            out[f"{region}/{year}"] = n
    log.info("price_collisions: removed %d rows from %d partitions", sum(out.values()), len(out))
    return out


def mapping_gaps_path(settings: Settings) -> Path:
    return settings.data_dir / "reference" / "seeds" / MAPPING_GAPS_FILE


_GAP_HEADER = (
    "# Listings without a venue price symbol (reason mapping_gap; A6 ticker-collision repair, app.normalize.price_collisions).\n"
    "# Their bars were a US ticker's (removed; backup in the `detail` column). Re-fetch once a source serves the venue:\n"
    "# set listings.yf_ticker (or add the venue to a price adapter) and the yahoo_price_history campaign picks them up.\n"
)
_GAP_COLS = ["security_id", "listing_id", "mic", "local_symbol", "reason", "refetch", "rows_removed", "detail", "marked_at"]


def read_mapping_gaps(settings: Settings | None = None) -> pl.DataFrame:
    p = mapping_gaps_path(settings or get_settings())
    if not p.exists():
        return pl.DataFrame(schema={c: pl.Utf8 for c in _GAP_COLS})
    return pl.read_csv(p, comment_prefix="#", infer_schema_length=0)


def mark_mapping_gaps(pl_: CollisionPlan, removed_by_sid: dict[str, int], backup: Path | None,
                      settings: Settings | None = None) -> int:
    """Upsert one row per venue-gap listing that carried colliding bars: reason mapping_gap, refetch=1. Listings
    whose bars were already gone (pl_.removed) are added only when not marked yet: an existing row keeps its
    rows_removed / backup detail."""
    s = settings or get_settings()
    ev = pl_.evidence
    bad = ev.filter(pl.col("contaminated")) if ev.height and "contaminated" in ev.columns else ev.head(0)
    rem = pl_.removed_contaminated()
    if bad.is_empty() and rem.is_empty():
        return 0
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    old = read_mapping_gaps(s)
    parts: list[pl.DataFrame] = []
    if bad.height:
        sym = bad.group_by("security_id").agg(pl.col("symbol").drop_nulls().unique().sort().str.join("/").alias("__sym"),
                                              pl.col("source_id").unique().sort().str.join("+").alias("__src"))
        rel = backup.relative_to(s.data_dir).as_posix() if backup is not None else ""
        parts.append(pl_.gaps.join(sym, on="security_id", how="inner").with_columns(
            pl.lit(REASON).alias("reason"), pl.lit("1").alias("refetch"),
            pl.col("security_id").replace_strict({k: str(v) for k, v in removed_by_sid.items()}, default="0")
            .alias("rows_removed"),
            (pl.lit("no Yahoo / Polygon symbol for ") + pl.col("mic") + pl.lit(" listing; ") + pl.col("__src")
             + pl.lit(" bars of US ticker ") + pl.col("__sym") + pl.lit(" removed; backup ") + pl.lit(rel)).alias("detail"),
            pl.lit(now).alias("marked_at")).select(_GAP_COLS))
    if rem.height:
        done = set(old.get_column("listing_id").to_list()) if old.height else set()
        for p in parts:
            done |= set(p.get_column("listing_id").to_list())
        parts.append(pl_.gaps.join(rem, on="security_id", how="inner").filter(~pl.col("listing_id").is_in(sorted(done)))
                     .with_columns(
            pl.lit(REASON).alias("reason"), pl.lit("1").alias("refetch"), pl.lit("").alias("rows_removed"),
            (pl.lit("no Yahoo / Polygon symbol for ") + pl.col("mic") + pl.lit(" listing; its bars were US ticker ")
             + pl.col("symbol").fill_null("?") + pl.lit(" (") + pl.col("source_id").fill_null("?")
             + pl.lit("; valuation_daily close_usd = staged close x FX on ") + pl.col("n_match").cast(pl.Utf8)
             + pl.lit(" of ") + pl.col("n_overlap").cast(pl.Utf8)
             + pl.lit(" dates), removed from prices_daily by prices_finalize before this repair (left the price "
                      "universe); no row backup")).alias("detail"),
            pl.lit(now).alias("marked_at")).select(_GAP_COLS))
    new = pl.concat(parts, how="vertical") if parts else pl.DataFrame(schema={c: pl.Utf8 for c in _GAP_COLS})
    if new.is_empty():
        return 0
    old = old.select([c for c in _GAP_COLS if c in old.columns]) if old.height else old
    merged = pl.concat([old.filter(~pl.col("listing_id").is_in(new.get_column("listing_id").implode())), new],
                       how="diagonal_relaxed").sort("mic", "local_symbol") if old.height else new.sort("mic", "local_symbol")
    p = mapping_gaps_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        f.write(_GAP_HEADER)
        merged.select(_GAP_COLS).write_csv(f)
    tmp.replace(p)
    return new.height


def request_full_rebuild(security_ids: Sequence[str], settings: Settings | None = None) -> int:
    """Ask the next incremental prices_finalize to re-build these securities from their full staging history (state
    key prices_incremental.FORCE_FULL_KEY). Used for the US twins, which lost bars while out of the universe."""
    from app.normalize import prices_incremental as PI  # noqa: PLC0415

    s = settings or get_settings()
    ids = sorted({x for x in security_ids if x})
    if not ids:
        return 0
    st = PI.load_state(s)
    cur = list(st.get(PI.FORCE_FULL_KEY) or [])
    # request order is kept: an incremental run takes the oldest FORCE_FULL_MAX_PER_RUN first
    st[PI.FORCE_FULL_KEY] = cur + [x for x in ids if x not in set(cur)]
    PI.save_state(s, st)
    return len(set(ids) - set(cur))


# ------------------------------------------------------------------------------------------------ twins (postcondition)


def marked_twins(settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, twin_security_id, twin_ticker] for the listings marked mapping_gap: the current primary US
    listings whose resolution key is the marked listing's bare ticker (the securities that lost bars while the
    colliding listing held their key)."""
    from app.core.storage import read_table  # noqa: PLC0415
    from app.normalize.prices import listing_key_expr  # noqa: PLC0415

    s = settings or get_settings()
    mg = read_mapping_gaps(s)
    empty = pl.DataFrame(schema={"security_id": pl.Utf8, "twin_security_id": pl.Utf8, "twin_ticker": pl.Utf8})
    if mg.is_empty():
        return empty
    mg = mg.filter(pl.col("reason") == REASON) if "reason" in mg.columns else mg
    lst = read_table("listings", settings=s)
    cur = lst.filter(pl.col("is_primary") & pl.col("valid_to").is_null())
    us = cur.filter(pl.col("exchange_country") == "US").select(listing_key_expr().alias("__k"),
                                                                pl.col("security_id").alias("twin_security_id"))
    return (mg.select("security_id", pl.col("local_symbol").alias("__k")).join(us, on="__k", how="inner")
            .select("security_id", "twin_security_id", pl.col("__k").alias("twin_ticker"))
            .unique(maintain_order=True).sort("twin_ticker", "twin_security_id"))


def twin_bar_status(twin_ids: Sequence[str], settings: Settings | None = None,
                    stale_days: int = 7) -> pl.DataFrame:
    """[security_id, n_bars, last_date, ok]: ok = bars up to within `stale_days` of the latest US session stored."""
    from app.normalize.prices_incremental import scan_partitions  # noqa: PLC0415

    s = settings or get_settings()
    ids = sorted(set(twin_ids))
    schema = {"security_id": pl.Utf8, "n_bars": pl.UInt32, "last_date": pl.Date, "ok": pl.Boolean}
    if not ids:
        return pl.DataFrame(schema=schema)
    lf = scan_partitions("prices_daily", s, {"region": lambda v: v == "US"})
    try:
        last_us = lf.select(pl.col("date").max()).collect().item()
        got = (lf.filter(_isin("security_id", ids)).group_by("security_id")
               .agg(pl.len().cast(pl.UInt32).alias("n_bars"), pl.col("date").max().alias("last_date")).collect())
    except Exception:  # noqa: BLE001 - no US partition yet
        last_us, got = None, pl.DataFrame(schema={k: v for k, v in schema.items() if k != "ok"})
    out = pl.DataFrame({"security_id": ids}).join(got, on="security_id", how="left").with_columns(
        pl.col("n_bars").fill_null(0).cast(pl.UInt32))
    lim = (last_us - timedelta(days=stale_days)) if last_us is not None else None
    ok = (pl.col("n_bars") > 0) & (pl.col("last_date") >= lim if lim is not None else pl.lit(True))
    return out.with_columns(ok.fill_null(False).alias("ok")).select(list(schema))


def restore_twins(settings: Settings | None = None, apply: bool = False, wait_min: float = 60.0,
                  security_ids: Sequence[str] | None = None) -> dict[str, Any]:
    """A6 review postcondition: every US twin must carry bars again. Re-builds the twins without current bars (or
    `security_ids`) from their full staging history with prices_incremental.rebuild_securities (the price job's own
    builder + partition writer under the prices_daily lock; no watermark moves) and clears them from the forced
    full re-build request. Dry run unless apply. Before writing: waits until no price job runs, and backs up the
    stored rows of those ids to data/backups/prices_collision_twins_<date>.parquet (restorable with write_table)."""
    from app.core.storage import scan_table  # noqa: PLC0415
    from app.normalize import prices_incremental as PI  # noqa: PLC0415

    s = settings or get_settings()
    tw = marked_twins(s)
    req = list(PI.load_state(s).get(PI.FORCE_FULL_KEY) or [])
    pool = sorted(set(tw.get_column("twin_security_id").to_list()) | set(req))
    before = twin_bar_status(pool, s)
    ids = sorted(set(security_ids)) if security_ids is not None else \
        sorted(set(before.filter(~pl.col("ok")).get_column("security_id").to_list()) | set(req))
    base: dict[str, Any] = {"twins": tw.get_column("twin_security_id").n_unique() if tw.height else 0,
                            "requested_full_rebuild": len(req), "without_current_bars": int((~before["ok"]).sum()),
                            "to_rebuild": len(ids)}
    if not apply or not ids:
        return {"status": "dry_run" if ids else "nothing_to_do", **base, "ids": ids}
    busy = wait_for_price_jobs(s, wait_min)
    if busy:
        return {"status": "busy", "running": busy, **base}
    from app.core.schema import get_contract  # noqa: PLC0415

    old = scan_table("prices_daily", s).filter(_isin("security_id", ids)).collect()
    bpath = backup_path(s, prefix="prices_collision_twins")
    cols = [x for x in get_contract("prices_daily").column_names if x in old.columns]
    tmp = bpath.with_suffix(".parquet.tmp")
    old.select(cols).write_parquet(tmp, compression="zstd", statistics=True)
    tmp.replace(bpath)
    if pl.read_parquet(bpath).height != old.height:
        raise RuntimeError(f"backup verification failed: {bpath}")
    busy = running_price_jobs(s)   # a job may have started while the backup was written
    if busy:
        return {"status": "busy", "running": busy, "backup": str(bpath), **base}
    rep = PI.rebuild_securities(s, ids, write=True)
    after = twin_bar_status(pool, s)
    bad = after.filter(~pl.col("ok"))
    return {"status": "applied" if bad.is_empty() else "incomplete", "backup": str(bpath), "rows_before": old.height,
            **base, "rebuild": {k: rep[k] for k in ("rows_written", "no_output", "not_in_universe", "by_region")},
            "twins_without_current_bars_after": bad.get_column("security_id").to_list(),
            "requested_full_rebuild_left": len(PI.load_state(s).get(PI.FORCE_FULL_KEY) or [])}


def apply_plan(pl_: CollisionPlan, settings: Settings | None = None, wait_min: float = 60.0,
               backup: Path | None = None) -> dict[str, Any]:
    """Backup -> delete -> mark -> request twin re-builds. Refuses (status busy) while a price job runs. Without rows
    to remove, listings whose bars were removed earlier (pl_.removed) are still marked (status marked)."""
    s = settings or get_settings()
    busy = wait_for_price_jobs(s, wait_min)
    if busy:
        return {"status": "busy", "running": busy}
    if pl_.rows.is_empty():
        rem = pl_.removed_contaminated()
        if rem.is_empty():
            return {"status": "nothing_to_do", **pl_.summary()}
        marked = mark_mapping_gaps(pl_, {}, None, s)
        tw = pl_.twins.filter(pl.col("security_id").is_in(rem.get_column("security_id").implode())) \
            if pl_.twins.height else pl_.twins
        forced = request_full_rebuild(tw.get_column("twin_security_id").to_list() if tw.height else [], s)
        return {"status": "marked", "rows_removed": 0, "listings_marked": marked,
                "twins_full_rebuild_requested": forced, **_twin_postcondition(tw, s), **pl_.summary()}
    bpath = write_backup(pl_, s, backup)
    busy = running_price_jobs(s)  # a job may have started while the backup was written
    if busy:
        return {"status": "busy", "running": busy, "backup": str(bpath)}
    removed = delete_rows(pl_.rows, s)
    by_sid = {r[0]: int(r[1]) for r in pl_.rows.group_by("security_id").len().iter_rows()}
    marked = mark_mapping_gaps(pl_, by_sid, bpath, s)
    forced = request_full_rebuild(pl_.twins.get_column("twin_security_id").to_list() if pl_.twins.height else [], s)
    return {"status": "applied", "backup": str(bpath), "removed": removed, "rows_removed": sum(removed.values()),
            "listings_marked": marked, "twins_full_rebuild_requested": forced,
            **_twin_postcondition(pl_.twins, s), **pl_.summary()}


def _twin_postcondition(twins: pl.DataFrame, settings: Settings) -> dict[str, Any]:
    """A6 review: the repair is not finished while a US twin has no current bars (restore-twins re-builds them)."""
    ids = twins.get_column("twin_security_id").unique().to_list() if twins.height else []
    st = twin_bar_status(ids, settings)
    bad = st.filter(~pl.col("ok")).get_column("security_id").to_list() if st.height else []
    return {"twins_without_current_bars": len(bad), "twins_without_current_bars_ids": bad[:100],
            "next_step": "python -m app.normalize.price_collisions restore-twins --apply" if bad else None}


# ------------------------------------------------------------------------------------------------ derived rows


def marked_securities(settings: Settings | None = None) -> list[str]:
    """Security ids marked mapping_gap by this repair (data/reference/seeds/price_mapping_gaps.csv)."""
    mg = read_mapping_gaps(settings)
    if mg.is_empty():
        return []
    mg = mg.filter(pl.col("reason") == REASON) if "reason" in mg.columns else mg
    return sorted(set(mg.get_column("security_id").drop_nulls().to_list()))


def derived_rows(security_ids: Sequence[str], settings: Settings | None = None,
                 tables: Sequence[str] = DERIVED_TABLES) -> dict[str, pl.DataFrame]:
    """{table: logical rows of these securities} for the price-derived tables (read-only)."""
    from app.core.storage import scan_table  # noqa: PLC0415

    s = settings or get_settings()
    ids = list(security_ids)
    out: dict[str, pl.DataFrame] = {}
    for t in tables:
        try:
            out[t] = scan_table(t, s).filter(_isin("security_id", ids)).collect() if ids else pl.DataFrame()
        except Exception as e:  # noqa: BLE001 - a table that does not exist yet has nothing to purge
            log.info("price_collisions: %s unreadable (%s)", t, type(e).__name__)
            out[t] = pl.DataFrame()
    return out


def delete_security_rows(table: str, security_ids: Sequence[str], settings: Settings | None = None) -> dict[str, int]:
    """Remove every row of `security_ids` from `table`: only the partition directories that hold them are re-written,
    under the table writer lock (app.core.storage.table_lock, the lock write_table takes), with storage's physical
    writer + atomic swap. Returns {partition dir (relative): rows removed}."""
    from app.core.schema import conform, get_contract  # noqa: PLC0415
    from app.core.storage import (_parquet_files, _rmtree, _scan_files, _swap_in, _write_partition_files,  # noqa: PLC0415
                                  table_dir, table_lock)

    s = settings or get_settings()
    ids = sorted(set(security_ids))
    if not ids:
        return {}
    c = get_contract(table)
    root = table_dir(c, s)
    pcols = list(c.partition_by)
    out: dict[str, int] = {}
    with table_lock(table, s):
        by_dir: dict[Path, list[Path]] = {}
        for f in _parquet_files(root):
            by_dir.setdefault(f.parent, []).append(f)
        for pdir, files in sorted(by_dir.items()):
            # cheap pre-check on the id column: only partitions that hold these securities are read in full
            if not _files_hold(files, ids):
                continue
            old = _scan_files(files, c, root).collect()
            keep = old.filter(~pl.col("security_id").is_in(ids))
            n = old.height - keep.height
            if not n:
                continue
            if keep.is_empty():
                _rmtree(pdir)
            else:
                df = conform(keep.select([x for x in c.column_names if x in keep.columns]), c)
                tmp = pdir.parent / f".tmp-{pdir.name}-{uuid.uuid4().hex[:8]}"
                _write_partition_files(tmp, df.drop([p for p in pcols if p in df.columns]), c)
                _swap_in(tmp, pdir)
            out[pdir.relative_to(root).as_posix() or "."] = n
    log.info("price_collisions: %s: removed %d rows of %d securities", table, sum(out.values()), len(ids))
    return out


def _files_hold(files: Sequence[Path], ids: Sequence[str]) -> bool:
    """True when any of these parquet files has a row of `ids` (security_id column only; unreadable -> True)."""
    for f in files:
        try:
            if pl.scan_parquet(f).filter(_isin("security_id", ids)).select(pl.len()).collect().item():
                return True
        except Exception:  # noqa: BLE001 - let the full read decide
            return True
    return False


def _derived_backup_dir(settings: Settings, day: date | None = None) -> Path:
    d = settings.data_dir / "backups"
    stem = f"prices_collision_derived_{(day or datetime.now(UTC).date()).strftime('%Y%m%d')}"
    p, i = d / stem, 2
    while p.exists():
        p = d / f"{stem}-{i}"
        i += 1
    return p


def purge_derived(security_ids: Sequence[str] | None = None, settings: Settings | None = None, apply: bool = False,
                  tables: Sequence[str] = DERIVED_TABLES, wait_min: float = 60.0,
                  backup_dir: Path | None = None) -> dict[str, Any]:
    """Rows built from the colliding bars in the price-derived tables (module doc). Default scope: the securities
    marked mapping_gap. Dry run unless apply: then backup (logical rows per table, restorable with
    write_table(pl.read_parquet(f), table), verified by re-reading) -> delete_security_rows per table, refused while a
    price job or a writer of these tables runs."""
    from app.core.jobqueue import job_is_running  # noqa: PLC0415
    from app.core.schema import get_contract  # noqa: PLC0415

    s = settings or get_settings()
    ids = sorted(set(security_ids)) if security_ids is not None else marked_securities(s)
    rows = derived_rows(ids, s, tables)
    summ = {t: {"rows": df.height, "securities": df.get_column("security_id").n_unique() if df.height else 0}
            for t, df in rows.items()}
    base = {"securities": len(ids), "tables": summ, "rows_total": sum(v["rows"] for v in summ.values())}
    if not apply:
        return {"status": "dry_run", **base}
    if not base["rows_total"]:
        return {"status": "nothing_to_do", **base}

    def busy_jobs() -> list[str]:
        return [j for j in (*PRICE_JOBS, *DERIVED_JOBS) if job_is_running(j, s)]

    end = time.monotonic() + wait_min * 60
    busy = busy_jobs()
    while busy and time.monotonic() < end:
        log.info("price_collisions purge_derived: waiting for %s", busy)
        time.sleep(30)
        busy = busy_jobs()
    if busy:
        return {"status": "busy", "running": busy, **base}
    bdir = backup_dir or _derived_backup_dir(s)
    bdir.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for t, df in rows.items():
        if not df.height:
            continue
        cols = [x for x in get_contract(t).column_names if x in df.columns]
        p = bdir / f"{t}.parquet"
        tmp = p.with_suffix(".parquet.tmp")
        df.select(cols).write_parquet(tmp, compression="zstd", statistics=True)
        tmp.replace(p)
        if pl.read_parquet(p).height != df.height:
            raise RuntimeError(f"backup verification failed for {t}: {p}")
        files[t] = str(p)
    (bdir / "manifest.json").write_text(json.dumps(
        {"created_at": datetime.now(UTC).isoformat(), "securities": ids, "tables": summ, "files": files,
         "restore": "write_table(pl.read_parquet(file), table) (upsert by the table's primary key)"},
        default=str, indent=1), encoding="utf-8")
    busy = busy_jobs()  # a job may have started while the backup was written
    if busy:
        return {"status": "busy", "running": busy, "backup_dir": str(bdir), **base}
    removed = {t: sum(delete_security_rows(t, ids, s).values()) for t, df in rows.items() if df.height}
    return {"status": "applied", "backup_dir": str(bdir), "removed": removed, **base}


# ------------------------------------------------------------------------------------------------ DQ (hook price_identity)


def identity_pairs(window: pl.DataFrame, min_shared: int = DQ_MIN_SHARED, min_share: float = DQ_MIN_SHARE,
                   min_distinct: int = DQ_MIN_DISTINCT, max_same: int = DQ_MAX_SAME_PRICE) -> pl.DataFrame:
    """Pure: pairs of securities whose closes are identical on the same dates. window [security_id, date, close].
    Returns [a, b, shared, n_a, n_b, share, distinct] sorted by shared desc."""
    w = window.filter(pl.col("close") > 0).with_columns(pl.col("close").round(6).alias("__c")).unique(
        subset=["security_id", "date"], keep="last")
    n = w.group_by("security_id").agg(pl.len().alias("n"))
    grp = w.group_by("date", "__c").agg(pl.col("security_id").sort().alias("ids"))
    grp = grp.filter((pl.col("ids").list.len() >= 2) & (pl.col("ids").list.len() <= max_same))
    if grp.is_empty():
        return pl.DataFrame(schema={"a": pl.Utf8, "b": pl.Utf8, "shared": pl.UInt32, "n_a": pl.UInt32, "n_b": pl.UInt32,
                                    "share": pl.Float64, "distinct": pl.UInt32})
    e = grp.with_columns(pl.col("ids").alias("a")).explode("a").explode("ids").rename({"ids": "b"}).filter(
        pl.col("a") < pl.col("b"))
    pr = e.group_by("a", "b").agg(pl.len().cast(pl.UInt32).alias("shared"), pl.col("__c").n_unique().cast(pl.UInt32)
                                  .alias("distinct"))
    pr = (pr.join(n.rename({"security_id": "a", "n": "n_a"}), on="a").join(n.rename({"security_id": "b", "n": "n_b"}),
                                                                          on="b")
          .with_columns((pl.col("shared") / pl.min_horizontal("n_a", "n_b")).alias("share")))
    return (pr.filter((pl.col("shared") >= min_shared) & (pl.col("share") >= min_share)
                      & (pl.col("distinct") >= min_distinct))
            .select("a", "b", "shared", pl.col("n_a").cast(pl.UInt32), pl.col("n_b").cast(pl.UInt32), "share", "distinct")
            .sort("shared", descending=True))


def identity_collision_outcomes(ctx: Any) -> list[Any]:
    """dq hook `price_identity`: identical price series across securities + bars on listings without a venue symbol."""
    from app.core.dq import DqOutcome  # noqa: PLC0415

    t = "prices_daily"
    if not ctx.has_data(t):
        return []
    out = []
    win = ctx.con.execute(
        f"SELECT security_id, date, close FROM {t} WHERE date >= current_date - INTERVAL {DQ_WINDOW_DAYS} DAY "
        "AND close > 0").pl()
    # market instruments (IDX: / ETF: ids, e.g. SI=F and SIL=F quote the same silver contract) are not listings
    win = win.filter(~pl.col("security_id").str.contains(r"^(IDX|ETF):")) if win.height else win
    pairs = identity_pairs(win) if win.height else pl.DataFrame()
    n_sec = len(set(pairs.get_column("a").to_list()) | set(pairs.get_column("b").to_list())) if pairs.height else 0
    meta = _ticker_meta(ctx)
    ex = []
    for r in pairs.head(15).iter_rows(named=True):
        ex.append({"a": meta.get(r["a"], r["a"]), "b": meta.get(r["b"], r["b"]), "shared": r["shared"],
                   "share": round(float(r["share"]), 3)})
    out.append(DqOutcome(t, "price_identity_collision", "warn", n_sec == 0, float(n_sec), 0.0,
                         {"pairs": pairs.height, "examples": ex, "window_days": DQ_WINDOW_DAYS,
                          "rule": f">= {DQ_MIN_SHARED} identical closes on the same dates, >= {DQ_MIN_SHARE:.0%} of the "
                                  f"shorter series, >= {DQ_MIN_DISTINCT} distinct prices"}))
    out.extend(venue_gap_outcomes(ctx))
    return out


def _ticker_meta(ctx: Any) -> dict[str, str]:
    try:
        from app.core.storage import scan_table  # noqa: PLC0415

        sm = scan_table("securities_master", ctx.settings).select("security_id", "display_ticker").collect()
        return dict(sm.iter_rows())
    except Exception:  # noqa: BLE001 - examples only
        return {}


def venue_gap_outcomes(ctx: Any) -> list[Any]:
    """Securities whose primary listing has no venue symbol but that carry recent bars (a collision symptom)."""
    from app.core.dq import DqOutcome  # noqa: PLC0415
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        gaps = venue_gap_listings(read_table("listings", settings=ctx.settings))
    except Exception as e:  # noqa: BLE001
        return [DqOutcome("prices_daily", "bars_without_venue_symbol", "info", True, None, None,
                          {"reason": f"listings unreadable: {type(e).__name__}"})]
    if gaps.is_empty():
        return [DqOutcome("prices_daily", "bars_without_venue_symbol", "warn", True, 0.0, 0.0, {"gap_listings": 0})]
    ids = ",".join(f"'{x}'" for x in gaps.get_column("security_id").to_list())
    rows = ctx.con.execute(
        f"SELECT security_id, count(*) FROM prices_daily WHERE date >= current_date - INTERVAL {DQ_WINDOW_DAYS} DAY "
        f"AND security_id IN ({ids}) GROUP BY 1").fetchall()
    meta = dict(gaps.select("security_id", pl.concat_str([pl.col("local_symbol"), pl.col("mic")], separator="@"))
                .iter_rows())
    ex = sorted(meta.get(r[0], r[0]) for r in rows)[:20]
    out = [DqOutcome("prices_daily", "bars_without_venue_symbol", "warn", not rows, float(len(rows)), 0.0,
                     {"gap_listings": gaps.height, "with_bars": len(rows), "examples": ex,
                      "fix": "python -m app.normalize.price_collisions plan / apply"})]
    out.extend(_derived_gap_outcomes(ctx, gaps, meta))
    out.extend(twin_outcomes(ctx))
    return out


def twin_outcomes(ctx: Any) -> list[Any]:
    """A6 review postcondition `twins_without_bars`: every US twin of a listing marked mapping_gap carries bars up to
    within 7 days of the latest US bar (the repair removed the collider's copies; the twin itself lost bars while the
    collider held its key and is restored by restore-twins / the forced full re-build)."""
    from app.core.dq import DqOutcome  # noqa: PLC0415

    try:
        tw = marked_twins(ctx.settings)
    except Exception as e:  # noqa: BLE001
        return [DqOutcome("prices_daily", "twins_without_bars", "info", True, None, None,
                          {"reason": f"mapping gaps / listings unreadable: {type(e).__name__}"})]
    if tw.is_empty() or not ctx.has_data("prices_daily"):
        return [DqOutcome("prices_daily", "twins_without_bars", "error", True, 0.0, 0.0, {"twins": tw.height})]
    ids = sorted(set(tw.get_column("twin_security_id").to_list()))
    lit = ",".join("'" + x.replace("'", "''") + "'" for x in ids)
    rows = ctx.con.execute(
        f"SELECT security_id, count(*), max(date) FROM prices_daily WHERE security_id IN ({lit}) GROUP BY 1").fetchall()
    last_us = ctx.scalar("SELECT max(date) FROM prices_daily WHERE region = 'US'")
    lim = (last_us - timedelta(days=7)) if last_us is not None else None
    have = {r[0]: r[2] for r in rows}
    bad = [x for x in ids if x not in have or (lim is not None and have[x] is not None and have[x] < lim)]
    tick = dict(tw.select("twin_security_id", "twin_ticker").iter_rows())
    try:
        from app.normalize import prices_incremental as PI  # noqa: PLC0415

        queued = len(PI.load_state(ctx.settings).get(PI.FORCE_FULL_KEY) or [])
    except Exception:  # noqa: BLE001
        queued = None
    return [DqOutcome("prices_daily", "twins_without_bars", "error", not bad, float(len(bad)), 0.0,
                      {"twins": len(ids), "without_bars": sum(1 for x in bad if x not in have),
                       "stale": sum(1 for x in bad if x in have), "examples": sorted(tick.get(x, x) for x in bad)[:20],
                       "forced_full_queued": queued,
                       "fix": "python -m app.normalize.price_collisions restore-twins --apply"})]


def _derived_gap_outcomes(ctx: Any, gaps: pl.DataFrame, meta: dict[str, str]) -> list[Any]:
    """Recent valuation_daily closes of listings without a venue symbol: rows derived from colliding bars that outlive
    them (valuation_latest keeps the latest row per security)."""
    from app.core.dq import DqOutcome  # noqa: PLC0415
    from app.core.storage import scan_table  # noqa: PLC0415

    if not ctx.has_data("valuation_daily"):
        return []
    lo = ctx.now.date() - timedelta(days=DQ_WINDOW_DAYS)
    v = (scan_table("valuation_daily", ctx.settings)
         .filter(pl.col("date") >= lo, pl.col("close_usd").is_not_null(),
                 _isin("security_id", gaps.get_column("security_id").to_list()))
         .group_by("security_id").agg(pl.len().alias("n")).collect())
    ex = sorted(meta.get(x, x) for x in v.get_column("security_id").to_list())[:20]
    return [DqOutcome("valuation_daily", "derived_rows_without_venue_symbol", "warn", v.is_empty(), float(v.height),
                      0.0, {"securities": v.height, "rows": int(v.get_column("n").sum()) if v.height else 0,
                            "examples": ex,
                            "fix": "python -m app.normalize.price_collisions purge-derived --apply"})]


def verify(settings: Settings | None = None) -> dict[str, Any]:
    """Read-only postconditions of the whole repair: no bars and no derived rows on listings without a venue symbol
    (the marked ones), every US twin with current bars, and the forced full re-build request drained. ok = all four."""
    from app.core.storage import read_table, scan_table  # noqa: PLC0415
    from app.normalize import prices_incremental as PI  # noqa: PLC0415

    s = settings or get_settings()
    gaps = venue_gap_listings(read_table("listings", settings=s))
    gids = gaps.get_column("security_id").to_list()
    bars = (scan_table("prices_daily", s).filter(_isin("security_id", gids)).select(pl.len()).collect().item()
            if gids else 0)
    derived = {t: df.height for t, df in derived_rows(marked_securities(s), s).items()}
    tw = marked_twins(s)
    st = twin_bar_status(tw.get_column("twin_security_id").to_list(), s)
    bad = st.filter(~pl.col("ok"))
    tick = dict(tw.select("twin_security_id", "twin_ticker").iter_rows()) if tw.height else {}
    queued = len(PI.load_state(s).get(PI.FORCE_FULL_KEY) or [])
    ok = bars == 0 and not any(derived.values()) and bad.is_empty() and queued == 0
    return {"ok": ok, "gap_listings": gaps.height, "bars_on_gap_listings": bars, "derived_rows_of_marked": derived,
            "twins": st.height, "twins_without_current_bars": bad.height,
            "twins_without_current_bars_tickers": sorted(tick.get(x, x) for x in bad.get_column("security_id").to_list()),
            "forced_full_queued": queued}


# ------------------------------------------------------------------------------------------------ CLI


def _write_plan_files(pl_: CollisionPlan, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    pl_.evidence.write_csv(out / "evidence.csv")
    if pl_.twin_report.height:
        pl_.twin_report.write_csv(out / "twins.csv")
    pl_.gaps.write_csv(out / "venue_gap_listings.csv")
    if pl_.removed.height:
        pl_.removed.write_csv(out / "removed_earlier_evidence.csv")
    if pl_.rows.height:
        pl_.rows.group_by("security_id", "source_id").agg(pl.len().alias("rows"), pl.col("date").min().alias("from"),
                                                          pl.col("date").max().alias("to")).sort("security_id") \
            .write_csv(out / "rows_by_series.csv")
    (out / "summary.json").write_text(json.dumps(pl_.summary(), default=str, indent=1), encoding="utf-8")


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("1", "true", "yes")


def run(ctx: Any) -> Any:
    """jobs.yaml `price_collision_repair` (manual): params apply (default false = dry run, evidence in the report),
    securities (comma-separated gap security ids), wait_min, purge_derived (true: also the derived-row purge, dry run
    unless apply; scope = the listings marked mapping_gap)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    sids = p.get("securities")
    if isinstance(sids, str):
        sids = [x.strip() for x in sids.split(",") if x.strip()]
    pl_ = plan(ctx.settings, sids)
    apply = _truthy(p.get("apply", "false"))
    wait = float(p.get("wait_min", 60))
    res: dict[str, Any] = apply_plan(pl_, ctx.settings, wait_min=wait) if apply else {"dry_run": True, **pl_.summary()}
    if _truthy(p.get("purge_derived", "false")) and res.get("status") != "busy":
        res["derived"] = purge_derived(None, ctx.settings, apply=apply, wait_min=wait)
    if _truthy(p.get("restore_twins", "false")) and res.get("status") != "busy":
        res["twins"] = restore_twins(ctx.settings, apply=apply, wait_min=wait)
    ok = ("applied", "nothing_to_do", "marked", None)
    subs = [res.get(k) or {} for k in ("derived", "twins")]
    st = ("success" if res.get("status") in ok and all(x.get("status") not in ("busy", "incomplete") for x in subs)
          else "failed")
    busy = res.get("status") == "busy" or any(x.get("status") == "busy" for x in subs)
    return JobResult(status=st, rows_written={"prices_daily": int(res.get("rows_removed") or 0)},  # rows removed
                     message=json.dumps(res, default=str)[:2000], retryable=busy)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.normalize.price_collisions")
    ap.add_argument("cmd", choices=["plan", "apply", "purge-derived", "restore-twins", "verify"])
    ap.add_argument("--out", help="plan: directory for evidence files")
    ap.add_argument("--securities", help="comma-separated gap security ids (default: all / purge: the marked ones)")
    ap.add_argument("--wait-min", type=float, default=60.0)
    ap.add_argument("--apply", action="store_true",
                    help="purge-derived / restore-twins: back up and write (default: dry run)")
    ap.add_argument("--tables", help=f"purge-derived: comma-separated subset of {','.join(DERIVED_TABLES)}")
    a = ap.parse_args(argv)
    s = get_settings()
    sids = [x.strip() for x in a.securities.split(",")] if a.securities else None
    if a.cmd == "restore-twins":
        res = restore_twins(s, apply=a.apply, wait_min=a.wait_min, security_ids=sids)
        print(json.dumps(res, default=str, indent=1))
        return 0 if res.get("status") in ("dry_run", "applied", "nothing_to_do") else 2
    if a.cmd == "verify":
        res = verify(s)
        print(json.dumps(res, default=str, indent=1))
        return 0 if res.get("ok") else 2
    if a.cmd == "purge-derived":
        tabs = [x.strip() for x in a.tables.split(",")] if a.tables else list(DERIVED_TABLES)
        bad = sorted(set(tabs) - set(DERIVED_TABLES))
        if bad:
            ap.error(f"not a price-derived table: {bad}")
        res = purge_derived(sids, s, apply=a.apply, tables=tabs, wait_min=a.wait_min)
        print(json.dumps(res, default=str, indent=1))
        return 0 if res.get("status") in ("dry_run", "applied", "nothing_to_do") else 2
    p = plan(s, sids)
    if a.out:
        _write_plan_files(p, Path(a.out))
    if a.cmd == "plan":
        print(json.dumps(p.summary(), default=str, indent=1))
        return 0
    res = apply_plan(p, s, wait_min=a.wait_min)
    print(json.dumps(res, default=str, indent=1))
    return 0 if res.get("status") in ("applied", "nothing_to_do", "marked") else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
