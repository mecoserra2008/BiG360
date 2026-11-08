"""Job `events_retention` (E3): tiered retention + compaction of the events-v2 tables.

For every month that ended more than `t2_days` (default 180) ago and still has day files:
  * gdelt_event_labels / gdelt_article_labels -> salient-only: per day, the top `k_country` (10) rows per country
    label, top `k_issuer` (5) per linked issuer, top `k_theme` (10) per theme bit, top `k_sector` (10) per sector bit
    (by salience), plus extremes (|Goldstein| >= 9, |tone| >= 10). Everything else is already in event_daily_agg.
  * story_members -> dropped (stories keep up to 3 representative URLs).
  * news_stories -> kept if the story is issuer-linked (link confidence >= 0.5; carries the issuer's sector), has >= 2 distinct sources (over its days), or is
    in the top `k_story` (20) per (day, country) or per (day, theme) by salience; others are dropped; kept rows
    keep one representative URL.
  * the surviving rows are rewritten as one month file, sorted by (label, date), ZSTD level 12.
event_daily_agg (T1) is never pruned (compacted by events_agg).
Re-running is safe: months without day files are skipped; a re-processed day re-opens its month (day file) and the
next run re-applies the rules.
params: t2_days, k_country, k_issuer, k_theme, k_sector, k_story, dry_run.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.events_v2 import store
from app.events_v2 import taxonomy as tx

DEFAULTS = {"t2_days": 180, "k_country": 10, "k_issuer": 5, "k_theme": 10, "k_sector": 10, "k_story": 20}


def _bits(col: str, n: int) -> pl.Expr:
    return pl.concat_list([pl.when((pl.col(col).cast(pl.Int64) & (1 << b)) != 0).then(pl.lit(b, pl.Int16))
                           for b in range(n)]).list.drop_nulls()


def salient_keys(df: pl.DataFrame, key: str, cfg: dict[str, int], extreme: pl.Expr | None = None) -> pl.DataFrame:
    """Distinct `key` values of rows that are top-k per (date, label) for any label type, or extreme."""
    base = df.select("date", key, "salience", "countries", "issuer_ids", "theme_mask", "sector_mask")
    picks = []
    for col, k in (("countries", cfg["k_country"]), ("issuer_ids", cfg["k_issuer"])):
        x = base.select("date", key, "salience", col).explode(col).drop_nulls(col)
        picks.append(x.filter(pl.col("salience").rank("ordinal", descending=True).over("date", col) <= k).select(key))
    for col, n, k in (("theme_mask", len(tx.THEMES), cfg["k_theme"]), ("sector_mask", len(tx.SECTORS), cfg["k_sector"])):
        x = base.select("date", key, "salience", _bits(col, n).alias("__b")).explode("__b").drop_nulls("__b")
        picks.append(x.filter(pl.col("salience").rank("ordinal", descending=True).over("date", "__b") <= k).select(key))
    if extreme is not None:
        picks.append(df.filter(extreme).select(key))
    return pl.concat(picks).unique()


def prune_events(df: pl.DataFrame, cfg: dict[str, int]) -> pl.DataFrame:
    ext = (pl.col("goldstein_x10").cast(pl.Int32).abs() >= 90) | (pl.col("tone_x100").cast(pl.Int32).abs() >= 1000)
    keep = salient_keys(df, "event_id", cfg, ext)
    return df.join(keep, on="event_id", how="semi")


def prune_articles(df: pl.DataFrame, cfg: dict[str, int]) -> pl.DataFrame:
    ext = pl.col("tone_x100").cast(pl.Int32).abs() >= 1000
    keep = salient_keys(df, "url_hash", cfg, ext)
    return df.join(keep, on="url_hash", how="semi")


def prune_stories(df: pl.DataFrame, cfg: dict[str, int], story_sizes: pl.DataFrame | None = None) -> pl.DataFrame:
    """Old-story rule (coordinator, 2026-10-12): keep a story-day row only if the story is linked to an issuer and
    hence its sector (issuer_ids carry links with confidence >= 0.5), OR it has >= 2 distinct sources (max over its days),
    OR it is in the top `k_story` per (day, country) or per (day, theme) by salience. Everything else is dropped
    (its counts already live in event_daily_agg). Kept rows keep only their first representative URL."""
    sizes = story_sizes if story_sizes is not None else df.group_by("story_id").agg(pl.col("n_sources").max().alias("__src"))
    if "__src" not in sizes.columns:
        sizes = sizes.rename({sizes.columns[1]: "__src"})
    df = df.join(sizes.select("story_id", "__src"), on="story_id", how="left")
    k = cfg["k_story"]
    top_c = (df.select("date", "story_id", "salience", "countries").explode("countries")
             .filter(pl.col("salience").rank("ordinal", descending=True).over("date", "countries") <= k)
             .select("story_id", "date"))
    top_t = (df.select("date", "story_id", "salience", _bits("theme_mask", len(tx.THEMES)).alias("__b")).explode("__b")
             .drop_nulls("__b").filter(pl.col("salience").rank("ordinal", descending=True).over("date", "__b") <= k)
             .select("story_id", "date"))
    # sector links: a story's issuer-derived sectors exist only when issuer_ids is non-empty; theme-derived sector bits
    # (oil_gas -> ENRG, ...) are not links and would keep ~60% of all stories, so they qualify via top-k per theme
    keep = ((pl.col("issuer_ids").list.len() > 0)
            | (pl.max_horizontal(pl.col("__src").fill_null(0), pl.col("n_sources")) >= 2))
    out = pl.concat([df.filter(keep).select("story_id", "date"), top_c, top_t]).unique()
    return (df.join(out, on=["story_id", "date"], how="semi").drop("__src")
            .with_columns(pl.col("urls").list.head(1)))


def prune_day(d_stories: pl.DataFrame, ev: pl.DataFrame, arts: pl.DataFrame, cfg: dict[str, int] | None = None,
              story_sizes: pl.DataFrame | None = None) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """In-flight long-term form of one day (deep backfill of days older than the T2 window): salient-only events /
    articles and pruned stories, so old days are written once, already compacted (story_members are not written)."""
    c = cfg or dict(DEFAULTS)
    return (prune_stories(d_stories, c, story_sizes) if d_stories.height else d_stories,
            prune_events(ev, c) if ev.height else ev, prune_articles(arts, c) if arts.height else arts)


def run_retention(settings: Settings | None = None, params: dict[str, Any] | None = None,
                  today: date | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    cfg = {k: int((params or {}).get(k, v)) for k, v in DEFAULTS.items()}
    dry = str((params or {}).get("dry_run", "false")).lower() in ("1", "true", "yes")
    today = today or datetime.now(UTC).date()
    cutoff = today - timedelta(days=cfg["t2_days"])
    rep: dict[str, Any] = {"cutoff": cutoff.isoformat(), "months": {}}

    def old_months(table: str) -> list[str]:
        out = []
        for ym in store.months(table, s):
            y, m = int(ym[:4]), int(ym[5:7])
            last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            if last < cutoff and any(store.month_dir(table, ym, s).glob("d-*.parquet")):
                out.append(ym)
        return out

    for table, fn in (("gdelt_event_labels", lambda df: prune_events(df, cfg)),
                      ("gdelt_article_labels", lambda df: prune_articles(df, cfg)),
                      ("news_stories", None), ("story_members", "drop")):
        for ym in old_months(table):
            if dry:
                rep["months"].setdefault(table, []).append(ym)
                continue
            if fn == "drop":
                r = store.compact_month(table, ym, lambda df: df.head(0), s)
            elif table == "news_stories":
                y, m = int(ym[:4]), int(ym[5:7])
                lo = date(y, m, 1)
                hi = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
                # sources over the story's whole life (stories straddling month ends)
                sizes = store.read_range("news_stories", lo - timedelta(days=3), hi + timedelta(days=3), s,
                                         columns=["story_id", "n_sources"]) \
                    .group_by("story_id").agg(pl.col("n_sources").max().alias("__src"))
                r = store.compact_month(table, ym, lambda df, sz=sizes: prune_stories(df, cfg, sz), s)
            else:
                r = store.compact_month(table, ym, fn, s)
            rep["months"].setdefault(table, []).append({"month": ym, **r})
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_retention(ctx.settings, ctx.params or {})
    n = sum(len(v) for v in rep["months"].values())
    rows = {t: sum(x.get("rows_after", 0) for x in v if isinstance(x, dict)) for t, v in rep["months"].items()}
    return JobResult(status="success", rows_written=rows, entities_ok=n, message=str(
        {"cutoff": rep["cutoff"], "months": {t: len(v) for t, v in rep["months"].items()}}))
