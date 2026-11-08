"""N1 historical news -> the events-v2 story layer (news_stories / story_members), shared by CC-NEWS and filing news.

* Sources (event_labels source dictionary, bit = id in news_stories.source_mask): `ccnews` (Common Crawl news),
  `sec_8k` (SEC 8-K items, app.events_v2.filing_news), `exchange_ann` (TDnet / HKEX announcements).
* Gating: a day joins the story layer only once GDELT has checkpointed it (data/state/events_v2.json `days`) or it
  is before GDELT 2.0 (2015-02-19). The GDELT writer replaces whole days, so merging earlier would be lost; rows of
  not-yet-ready days stay in their source table (ccnews_articles.story_id null / filing_news.story_id null) and are
  merged by `repair()` (nightly job news_filings, monthly news_recluster).
* Dedupe (CC-NEWS): URL hash against story_members (any source) and gdelt_article_labels of d-1..d+1 -> the article
  points at the existing story (dup_source_id = that member's source); otherwise the article's title is clustered
  (app.events_v2.stories, MinHash-LSH Jaccard >= 0.6) with the day's other new articles and linked to existing
  stories of d-3..d+3 (same-day singletons included), else it opens a new story.
* Retention (same compact format as E6): story_members of CC-NEWS / filing items only for days < 120 d old; days
  older than the T2 window (180 d) get their NEW stories pruned with the events_retention story rule (existing GDELT
  rows are never removed here); ccnews_articles older than 120 d are salient-only (top-k per label per day, any
  issuer-linked article in the top-k per issuer) - the long-term form, written directly.
* Writes are batched per month: a month that already has a compacted month file is rewritten once (compact_month
  with a transform), otherwise day files are replaced (store.write_day). Serialised by data/.locks/news_hist_merge.lock.
* recluster(): monthly cross-source re-cluster: per day, canonical titles of stories from different source families
  (GDELT vs CC-NEWS vs RSS / Polygon / DOC) with Jaccard >= 0.6 are merged into the biggest story (ids remapped in
  news_stories, story_members, ccnews_articles over the window). Filing-only stories (templated titles) are excluded.
"""

from __future__ import annotations

import gc
import math
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.events_v2 import dicts, store
from app.events_v2 import taxonomy as tx
from app.events_v2.stories import (MEMBER_SCHEMA, N_BANDS, N_PERM, STORY_SCHEMA, WINDOW_DAYS, build_day_stories,
                                   cluster_titles, hash_title, merge_story_rows, minhash_signatures, salience_expr,
                                   token_set)

log = get_logger(__name__)

SRC_CCNEWS, SRC_8K, SRC_EXCH = "ccnews", "sec_8k", "exchange_ann"
HIST_SOURCES = (SRC_CCNEWS, SRC_8K, SRC_EXCH)
GDELT_START = date(2015, 2, 19)
MEMBER_DAYS = 120
T2_DAYS = 180
CC_TABLE = "ccnews_articles"
CC_SCHEMA: dict[str, pl.DataType] = {
    "date": pl.Date, "minute": pl.Int16, "ts_source": pl.Int8, "url_hash": pl.Int64, "url": pl.Utf8,
    "domain_id": pl.Int32, "lang_id": pl.Int8, "title": pl.Utf8, "description": pl.Utf8, "theme_mask": pl.Int64,
    "sector_mask": pl.Int16, "countries": pl.List(pl.Int16), "issuer_ids": pl.List(pl.Int32),
    "issuer_conf": pl.List(pl.Int8), "story_id": pl.Int64, "dup_source_id": pl.Int8,
    "minhash_bands": pl.List(pl.Int32), "salience": pl.Int16, "sentiment_x100": pl.Int8}
SMEM_COLS = ["date", "story_id", "url_hash", "source_id", "domain_id", "minute", "tone_x100", "lang_id"]
ART_RET = {"k_country": 10, "k_issuer": 5, "k_theme": 10, "k_sector": 10, "k_story": 20}


def merge_lock(s: Settings) -> FileLock:
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(s.locks_dir / "news_hist_merge.lock"), timeout=3600)


def source_ids(s: Settings) -> dict[str, int]:
    return dicts.assign_ids("source", list(HIST_SOURCES), s)


def gdelt_days(s: Settings) -> set[str]:
    from app.events_v2.pipeline import read_state  # noqa: PLC0415

    return {k for k, v in (read_state(s).get("days") or {}).items() if isinstance(v, dict) and (v.get("files_ok") or 0) > 0}


def day_ready(d: date, gd: set[str]) -> bool:
    return d < GDELT_START or d.isoformat() in gd


def _today() -> date:
    return datetime.now(UTC).date()


# ================================================================================================ table IO
def load_days(table: str, start: date, end: date, s: Settings, columns: list[str] | None = None) -> pl.DataFrame:
    return store.read_range(table, start, end, s, columns=columns)


def has_month_file(table: str, ym: str, s: Settings) -> bool:
    return store.month_file(table, ym, s).exists()


def flush_days(table: str, frames: dict[date, pl.DataFrame], s: Settings) -> int:
    """Replace whole days of `table` with `frames` (full day content). Month-batched (see module doc)."""
    by_month: dict[str, dict[date, pl.DataFrame]] = defaultdict(dict)
    for d, f in frames.items():
        by_month[d.strftime("%Y-%m")][d] = f
    n = 0
    for ym, fr in by_month.items():
        if has_month_file(table, ym, s):
            days = list(fr)
            parts = [store._prepare(table, f) for f in fr.values() if f.height]

            def transform(df: pl.DataFrame, days: list[date] = days, parts: list[pl.DataFrame] = parts) -> pl.DataFrame:
                keep = df.filter(~pl.col("date").is_in(days))
                if not parts:
                    return keep
                new = pl.concat(parts, how="diagonal_relaxed")
                cols = list(dict.fromkeys([*keep.columns, *new.columns]))
                return pl.concat([keep, new], how="diagonal_relaxed").select(cols)
            r = store.compact_month(table, ym, transform, s)
            n += r.get("bytes_after", 0)
        else:
            for d, f in fr.items():
                n += store.write_day(table, d, f, s)
    return n


# ================================================================================================ helpers
_C1, _C2 = np.uint64(0x9E3779B97F4A7C15), np.uint64(0xC2B2AE3D27D4EB4F)


def minhash_bands(titles: list[str | None]) -> list[list[int] | None]:
    """8 MinHash-LSH band keys (3 rows each of the 24-permutation signature, folded to int32) per title."""
    sets = [token_set(hash_title(t)) if t else frozenset() for t in titles]
    if not sets:
        return []
    sig = minhash_signatures(sets)
    r = N_PERM // N_BANDS
    out = np.zeros((len(sets), N_BANDS), dtype=np.int32)
    with np.errstate(over="ignore"):
        for b in range(N_BANDS):
            x = sig[:, b * r]
            for j in range(1, r):
                x = x ^ (sig[:, b * r + j] * (_C1 if j == 1 else _C2))
            out[:, b] = (x >> np.uint64(32)).astype(np.uint32).view(np.int32)
    return [None if not st else [int(v) for v in out[i]] for i, st in enumerate(sets)]


def _ensure_cols(df: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    adds = [pl.lit(None, dtype=t).alias(c) for c, t in schema.items() if c not in df.columns]
    df = df.with_columns(adds) if adds else df
    return df.select([pl.col(c).cast(t) for c, t in schema.items()])


def article_salience(df: pl.DataFrame, size_col: str = "__sz") -> pl.Expr:
    return ((pl.col(size_col).fill_null(1).cast(pl.Float64).log1p()
             + pl.when(pl.col("issuer_ids").list.len() > 0).then(2.0).otherwise(0.0)) * 100
            ).round().clip(0, 32767).cast(pl.Int16)


def prune_articles_salient(df: pl.DataFrame) -> pl.DataFrame:
    """Long-term ccnews_articles form: top-k per (day, country / issuer / theme bit / sector bit) by salience."""
    from app.events_v2.retention import salient_keys  # noqa: PLC0415

    if df.is_empty():
        return df
    keep = salient_keys(df, "url_hash", ART_RET, None)
    return df.join(keep, on="url_hash", how="semi")


def keep_new_stories(day: pl.DataFrame, existing_ids: set[int]) -> pl.DataFrame:
    """events_retention story rule applied to stories NOT already present (existing rows always kept)."""
    from app.events_v2.retention import DEFAULTS, prune_stories  # noqa: PLC0415

    if day.is_empty():
        return day
    pr = prune_stories(day, dict(DEFAULTS))
    keep_ids = set(pr.get_column("story_id").to_list()) | existing_ids
    out = day.filter(pl.col("story_id").is_in(list(keep_ids)))
    new_pruned = out.filter(~pl.col("story_id").is_in(list(existing_ids)))
    if new_pruned.height:  # pruned stories keep one representative URL (prune_stories rule)
        out = pl.concat([out.filter(pl.col("story_id").is_in(list(existing_ids))),
                         new_pruned.with_columns(pl.col("urls").list.head(1))], how="vertical_relaxed")
    return out


def day_blocks(days: list[date], s: Settings, block_days: int = 7) -> list[tuple[str, list[date]]]:
    """Newest-first blocks: a whole month when news_stories already has a compacted month file (one rewrite per
    month), else blocks of `block_days` (bounded memory: GDELT day files are ~80k stories per day)."""
    by_month: dict[str, list[date]] = defaultdict(list)
    for d in set(days):
        by_month[d.strftime("%Y-%m")].append(d)
    out: list[tuple[str, list[date]]] = []
    for ym in sorted(by_month, reverse=True):
        md = sorted(by_month[ym], reverse=True)
        if has_month_file("news_stories", ym, s):
            out.append((ym, sorted(md)))
            continue
        for i in range(0, len(md), block_days):
            out.append((ym, sorted(md[i:i + block_days])))
    return out


# ================================================================================================ CC-NEWS
def spool_to_articles(sp: pl.DataFrame, s: Settings) -> pl.DataFrame:
    """Spool rows (ccnews.SPOOL_SCHEMA) -> CC_SCHEMA rows with dictionary ids (story fields null)."""
    if sp.is_empty():
        return pl.DataFrame(schema=CC_SCHEMA)
    sp = sp.sort("ts").unique("url_hash", keep="first", maintain_order=True)
    dom = dicts.assign_ids("domain", sp.get_column("domain").drop_nulls().unique().to_list(), s)
    # the lang dictionary is int8 (<= 127 ids, shared with GDELT): only languages seen >= 5 times a day get a new id
    known = set(dicts.load_dict("lang", s).get_column("code").to_list()) if dicts.load_dict("lang", s).height else set()
    lc = sp.group_by("lang").len()
    langs = [x for x, n in lc.iter_rows() if x and re.fullmatch(r"[a-z]{2}", x) and (x in known or n >= 5)]
    lang = dicts.assign_ids("lang", langs, s)
    cty = tx.COUNTRY_ID
    countries = [[cty[c] for c in (cs or []) if c in cty][: tx.MAX_COUNTRIES] or [tx.GLOBAL_ID]
                 for cs in sp.get_column("countries").to_list()]
    dt = pl.from_epoch(pl.col("ts"), time_unit="s")
    return sp.with_columns(
        dt.dt.date().alias("date"), (dt.dt.hour().cast(pl.Int16) * 60 + dt.dt.minute().cast(pl.Int16)).alias("minute"),
        pl.col("domain").replace_strict(dom, default=None, return_dtype=pl.Int32).alias("domain_id"),
        pl.col("lang").replace_strict(lang, default=None, return_dtype=pl.Int8).alias("lang_id"),
        pl.Series("countries", countries, pl.List(pl.Int16)),
        pl.lit(None, pl.Int64).alias("story_id"), pl.lit(None, pl.Int8).alias("dup_source_id"),
        pl.lit(None, pl.List(pl.Int32)).alias("minhash_bands"), pl.lit(None, pl.Int16).alias("salience"),
        pl.lit(None, pl.Int8).alias("sentiment_x100"),
    ).pipe(_ensure_cols, CC_SCHEMA)


def _names(s: Settings) -> dict[int, tuple[str, str]]:
    from app.events_v2.context import issuer_meta  # noqa: PLC0415

    im = issuer_meta(dicts.load_dict("issuer", s))
    return {int(r["iid"]): (r["name_norm"] or "", (r["ticker"] or "").lower()) for r in im.iter_rows(named=True)}


class DayWindow:
    """In-memory news_stories / story_members / gdelt url hashes for a block of days (+-3 day margin)."""

    def __init__(self, days: list[date], s: Settings):
        self.s = s
        d0, d1 = min(days), max(days)
        lo, hi = d0 - timedelta(days=WINDOW_DAYS), d1 + timedelta(days=WINDOW_DAYS)
        st = load_days("news_stories", d0, d1, s)  # full rows only for the block's own days
        self.stories: dict[date, pl.DataFrame] = {k[0]: v for k, v in st.partition_by("date", as_dict=True).items()} \
            if st.height else {}
        slim = ["date", "story_id", "title", "n_articles"]
        for a, b in ((lo, d0 - timedelta(days=1)), (d1 + timedelta(days=1), hi)):
            x = load_days("news_stories", a, b, s, columns=slim)
            for k, v in (x.partition_by("date", as_dict=True).items() if x.height else []):
                self.stories[k[0]] = v
        del st
        mem = load_days("story_members", lo, hi, s)
        self.members: dict[date, pl.DataFrame] = {k[0]: v for k, v in mem.partition_by("date", as_dict=True).items()} \
            if mem.height else {}
        ga = load_days("gdelt_article_labels", lo, hi, s, columns=["date", "url_hash", "story_id"])
        self.gart: dict[date, pl.DataFrame] = {k[0]: v for k, v in ga.partition_by("date", as_dict=True).items()} \
            if ga.height else {}
        self.dirty_st: set[date] = set()
        self.dirty_mem: set[date] = set()

    def day(self, d: date) -> pl.DataFrame:
        return self.stories.get(d, pl.DataFrame(schema=STORY_SCHEMA))

    def neighbours(self, d: date) -> pl.DataFrame:
        parts = [self.stories[x].select("date", "story_id", "title", "n_articles")
                 for x in (d + timedelta(days=k) for k in range(-WINDOW_DAYS, WINDOW_DAYS + 1)) if x in self.stories]
        if not parts:
            return pl.DataFrame(schema={"date": pl.Date, "story_id": pl.Int64, "title": pl.Utf8, "n_articles": pl.Int32})
        nb = pl.concat(parts, how="vertical_relaxed")
        # same-day singletons may attract new articles (link_neighbours only considers stories with >= 2 members)
        return nb.with_columns(pl.when(pl.col("date") == d).then(pl.max_horizontal(pl.col("n_articles"), pl.lit(2)))
                               .otherwise(pl.col("n_articles")).cast(pl.Int32).alias("n_articles"))

    def url_index(self, d: date, exclude_src: int | None = None) -> pl.DataFrame:
        """[url_hash, __sid, __src] of existing members (other sources) / GDELT articles on d-1..d+1."""
        parts = []
        for x in (d - timedelta(days=1), d, d + timedelta(days=1)):
            if x in self.members:
                m = self.members[x]
                if exclude_src is not None:
                    m = m.filter(pl.col("source_id") != exclude_src)
                parts.append(m.select("url_hash", pl.col("story_id").alias("__sid"),
                                      pl.col("source_id").cast(pl.Int8).alias("__src")))
            if x in self.gart:
                parts.append(self.gart[x].select("url_hash", pl.col("story_id").alias("__sid"),
                                                 pl.lit(0, pl.Int8).alias("__src")))
        if not parts:
            return pl.DataFrame(schema={"url_hash": pl.Int64, "__sid": pl.Int64, "__src": pl.Int8})
        return pl.concat(parts, how="vertical_relaxed").unique("url_hash", keep="first")


def merge_ccnews_day(d: date, arts: pl.DataFrame, win: DayWindow, src_id: int, names: dict[int, tuple[str, str]],
                     today: date | None = None, gdelt_src_id: int = 0) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Unmerged ccnews articles of day d (story_id null) -> story layer (updates `win`). Returns the day's
    ccnews_articles rows with story_id / dup_source_id / salience / minhash bands set (+ long-term pruning)."""
    today = today or _today()
    rep: dict[str, Any] = {"articles": arts.height}
    todo = arts.filter(pl.col("story_id").is_null())
    done = arts.filter(pl.col("story_id").is_not_null())
    rep["unmerged"] = todo.height
    if todo.height:
        idx = win.url_index(d, exclude_src=src_id)
        todo = todo.join(idx, on="url_hash", how="left")
        dup = todo.filter(pl.col("__sid").is_not_null())
        rep["url_dupes"] = dup.height
        rep["url_dupes_gdelt"] = int(dup.filter(pl.col("__src") == gdelt_src_id).height)
        dup = dup.with_columns(pl.col("__sid").alias("story_id"), pl.col("__src").alias("dup_source_id")).drop("__sid", "__src")
        new = todo.filter(pl.col("__sid").is_null()).drop("__sid", "__src")
        cur = win.day(d)
        before_ids = set(cur.get_column("story_id").to_list()) if cur.height else set()
        gd_ids = set(cur.filter((pl.col("source_mask").cast(pl.Int32) & 1) != 0).get_column("story_id").to_list()) \
            if cur.height else set()
        if new.height:
            mem = new.select("url_hash", "url", "title", "minute", "domain_id", "lang_id",
                             pl.lit(src_id, pl.Int8).alias("source_id"), pl.lit(None, pl.Int16).alias("tone_x100"),
                             "theme_mask", "sector_mask", "countries", "issuer_ids", "issuer_conf",
                             pl.lit(0, pl.Int16).alias("n_events")).pipe(_ensure_cols, MEMBER_SCHEMA)
            new_st, new_m = build_day_stories(d, mem, win.neighbours(d), names)
            day = pl.concat([cur.select([c for c in STORY_SCHEMA if c in cur.columns]) if cur.height else cur,
                             new_st.select(list(STORY_SCHEMA))], how="diagonal_relaxed") if cur.height else new_st
            day = _ensure_cols(day, STORY_SCHEMA)
            if day.get_column("story_id").n_unique() < day.height:
                day = merge_story_rows(day)
            day = day.with_columns(salience_expr().alias("salience"))
            if d < today - timedelta(days=T2_DAYS):
                day = keep_new_stories(day, before_ids)
            win.stories[d] = day
            win.dirty_st.add(d)
            smap = new_m.select("url_hash", pl.col("story_id").alias("__sid"))
            new = new.join(smap, on="url_hash", how="left").with_columns(pl.col("__sid").alias("story_id")).drop("__sid")
            merged_into = set(new.get_column("story_id").drop_nulls().to_list())
            rep["new_stories"] = len(merged_into - before_ids)
            rep["joined_existing"] = int(new.filter(pl.col("story_id").is_in(list(before_ids))).height)
            rep["joined_gdelt_story"] = int(new.filter(pl.col("story_id").is_in(list(gd_ids))).height)
            if d >= today - timedelta(days=MEMBER_DAYS) and new_m.height:
                m0 = win.members.get(d)
                nm = new_m.select([c for c in SMEM_COLS if c in new_m.columns])
                win.members[d] = pl.concat([m0, nm], how="diagonal_relaxed").unique("url_hash", keep="first") \
                    if m0 is not None and m0.height else nm
                win.dirty_mem.add(d)
        out = pl.concat([x for x in (done, dup.select(done.columns), new.select(done.columns)) if x.height],
                        how="vertical_relaxed") if (done.height or dup.height or new.height) else done
    else:
        out = done
    # salience (story size) + MinHash band keys
    day = win.day(d)
    sizes = day.select("story_id", pl.col("n_articles").alias("__sz")) if day.height else \
        pl.DataFrame(schema={"story_id": pl.Int64, "__sz": pl.Int32})
    out = out.join(sizes.unique("story_id"), on="story_id", how="left").with_columns(
        article_salience(out).alias("salience")).drop("__sz")
    miss = out.filter(pl.col("minhash_bands").is_null())
    if miss.height:
        bands = pl.Series("__b", minhash_bands(miss.get_column("title").to_list()), pl.List(pl.Int32))
        mb = miss.select("url_hash").with_columns(bands)
        out = out.join(mb, on="url_hash", how="left").with_columns(
            pl.coalesce("minhash_bands", "__b").alias("minhash_bands")).drop("__b")
    if d < today - timedelta(days=MEMBER_DAYS):
        n0 = out.height
        out = prune_articles_salient(out)
        rep["articles_pruned"] = n0 - out.height
    rep["stored"] = out.height
    rep["linked"] = int(out.filter(pl.col("issuer_ids").list.len() > 0).height)
    day = win.day(d)
    rep["cc_stories"] = int(day.filter((pl.col("source_mask").cast(pl.Int32) & (1 << src_id)) != 0).height)         if day.height and "source_mask" in day.columns else 0
    return _ensure_cols(out, CC_SCHEMA), rep


def finalize_ccnews_days(days: list[date], s: Settings, read_spool: Callable[[date, Settings], tuple[pl.DataFrame, list[Path]]],
                         log: Any = None, sentiment: bool = True, max_sentiment_per_day: int = 1500,
                         today: date | None = None) -> dict[str, Any]:
    """Spooled CC-NEWS days -> ccnews_articles (+ story layer for GDELT-ready days) -> story sentiment."""
    from app.events_v2.ccnews import update_state  # noqa: PLC0415

    lg = log or log_default
    today = today or _today()
    src = source_ids(s)
    gd = gdelt_days(s)
    names = _names(s)
    rep: dict[str, Any] = {"days": 0, "spool_rows": 0, "new_articles": 0, "stored": 0, "pending_gdelt_days": 0,
                           "url_dupes": 0, "url_dupes_gdelt": 0, "joined_gdelt_story": 0, "new_stories": 0,
                           "linked": 0, "per_day": {}}
    touched: list[date] = []
    for ym, mdays in day_blocks(days, s):
        with merge_lock(s):
            existing = load_days(CC_TABLE, mdays[0], mdays[-1], s)
            ex = {k[0]: v for k, v in existing.partition_by("date", as_dict=True).items()} if existing.height else {}
            ready = [d for d in mdays if day_ready(d, gd)]
            win = DayWindow(ready, s) if ready else None
            out_frames: dict[date, pl.DataFrame] = {}
            spool_files: list[Path] = []
            for d in mdays:
                sp, files = read_spool(d, s)
                spool_files += files
                new = spool_to_articles(sp, s)
                old = _ensure_cols(ex[d], CC_SCHEMA) if d in ex else pl.DataFrame(schema=CC_SCHEMA)
                if old.height:
                    new = new.join(old.select("url_hash"), on="url_hash", how="anti")
                arts = pl.concat([old, new], how="vertical_relaxed") if old.height else new
                drep: dict[str, Any] = {"spool": sp.height, "new": new.height}
                rep["spool_rows"] += sp.height
                rep["new_articles"] += new.height
                if win is not None and d in ready:
                    arts, r2 = merge_ccnews_day(d, arts, win, src[SRC_CCNEWS], names, today)
                    drep.update(r2)
                    for k in ("url_dupes", "url_dupes_gdelt", "joined_gdelt_story", "new_stories", "linked"):
                        rep[k] += int(r2.get(k) or 0)
                    drep["story_layer"] = "merged"
                else:
                    drep["story_layer"] = "pending_gdelt"
                    rep["pending_gdelt_days"] += 1
                drep["stored"] = arts.height
                rep["stored"] += arts.height
                out_frames[d] = arts
                rep["per_day"][d.isoformat()] = drep
            b_art = flush_days(CC_TABLE, out_frames, s)
            b_st = flush_days("news_stories", {d: win.stories[d] for d in win.dirty_st}, s) if win else 0
            b_m = flush_days("story_members", {d: win.members[d] for d in win.dirty_mem}, s) if win else 0
            for f in spool_files:
                try:
                    f.unlink()
                except OSError:
                    pass
            for d in mdays:
                p = spool_files[0].parent.parent / d.isoformat() if spool_files else None
                if p is not None and p.exists() and not any(p.iterdir()):
                    p.rmdir()
            now = datetime.now(UTC).isoformat(timespec="seconds")

            def upd(st: dict[str, Any], mdays: list[date] = mdays, b: tuple[int, int, int] = (b_art, b_st, b_m)) -> None:
                dd = st.setdefault("days", {})
                for d in mdays:
                    r = rep["per_day"][d.isoformat()]
                    prev = dd.get(d.isoformat()) or {}
                    rec = {k: r.get(k) for k in ("stored", "story_layer", "url_dupes", "url_dupes_gdelt",
                                                 "joined_gdelt_story", "new_stories", "linked", "cc_stories")}
                    rec["spool_rows"] = int(prev.get("spool_rows") or 0) + int(r.get("spool") or 0)
                    rec["merged_at"] = now
                    dd[d.isoformat()] = rec
                st.setdefault("bytes_written", Counter())
                bw = Counter(st["bytes_written"])
                bw.update({"ccnews_articles": b[0], "news_stories": b[1], "story_members": b[2]})
                st["bytes_written"] = dict(bw)
            update_state(upd, s)
            touched += [d for d in mdays if d in ready]
            del win, ex, existing, out_frames
            gc.collect()
        rep["days"] += len(mdays)
        lg.info("finalize %s: %d days (%d ready), stored %d", ym, len(mdays), len(ready), sum(
            rep["per_day"][d.isoformat()]["stored"] for d in mdays))
    if sentiment and touched:
        rep["sentiment"] = score_story_sentiment(touched, s, src[SRC_CCNEWS], max_sentiment_per_day, log=lg)
    return rep


class _Log:
    def info(self, *a: Any) -> None:
        log.info(*a)

    def warning(self, *a: Any) -> None:
        log.warning(*a)


log_default = _Log()


# ================================================================================================ sentiment
class MonthCache:
    """Per-month sentiment cache shard (data/cache/nlp/sentiment/ccnews/<model>/<YYYY-MM>.parquet)."""

    def __init__(self, model_id: str, ym: str, s: Settings):
        self.path = s.cache_dir / "nlp" / "sentiment" / "ccnews" / re.sub(r"[^A-Za-z0-9]+", "_", model_id) / f"{ym}.parquet"
        self.d: dict[str, tuple[float, float, float]] = {}
        self.new: dict[str, tuple[float, float, float]] = {}
        if self.path.exists():
            df = pl.read_parquet(self.path)
            self.d = {k: (a, b, c) for k, a, b, c in df.iter_rows()}

    def get(self, k: str) -> tuple[float, float, float] | None:
        return self.d.get(k) or self.new.get(k)

    def put(self, k: str, p: Any) -> None:
        self.new[k] = (float(p[0]), float(p[1]), float(p[2]))

    def flush(self) -> None:
        if not self.new:
            return
        self.d.update(self.new)
        self.new = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(self.d)
        arr = np.array([self.d[k] for k in keys], dtype=np.float32)
        tmp = self.path.with_suffix(".tmp")
        pl.DataFrame({"key": keys, "p_neg": arr[:, 0], "p_neu": arr[:, 1], "p_pos": arr[:, 2]}).write_parquet(tmp)
        tmp.replace(self.path)


def score_titles(part: pl.DataFrame, model: Any, model_id: str, s: Settings) -> tuple[pl.DataFrame, dict[str, int]]:
    """[date, story_id, title] -> [date, story_id, __s, __m] through the per-month cache shards + `model`."""
    from app.nlp.sentiment import text_key  # noqa: PLC0415

    out: list[pl.DataFrame] = []
    stats: Counter = Counter()
    for (ym,), mp in part.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("__ym")).partition_by(
            "__ym", as_dict=True).items():
        cache = MonthCache(model_id, ym, s)
        texts = mp.get_column("title").to_list()
        keys = [text_key(x or "") for x in texts]
        need: dict[str, int] = {}
        for i, k in enumerate(keys):
            if cache.get(k) is None and texts[i]:
                need.setdefault(k, i)
        if need:
            ks = list(need)
            probs = model.predict([texts[need[k]] for k in ks], 64)
            for k, p in zip(ks, probs, strict=True):
                cache.put(k, p)
            cache.flush()
            stats["model_scored"] += len(ks)
        stats["cache_hits"] += len(keys) - len(need)
        sc = []
        for k in keys:
            p = cache.get(k)
            sc.append(None if p is None else int(round((p[2] - p[0]) * 100)))
        out.append(mp.select("date", "story_id").with_columns(pl.Series("__s", sc, pl.Int8),
                                                              pl.lit(model_id).alias("__m")))
    return (pl.concat(out) if out else pl.DataFrame(schema={"date": pl.Date, "story_id": pl.Int64, "__s": pl.Int8,
                                                            "__m": pl.Utf8})), dict(stats)


def _route_child(todo_path: str, out_path: str, model_id: str, threads: int) -> None:
    import json as _json  # noqa: PLC0415

    from app.nlp.sentiment import HFSentiment  # noqa: PLC0415

    s = get_settings()
    part = pl.read_parquet(todo_path)
    r, st = score_titles(part, HFSentiment(model_id, threads=threads, max_length=64), model_id, s)
    r.write_parquet(out_path)
    Path(out_path + ".json").write_text(_json.dumps(st), encoding="utf-8")


def _score_in_child(part: pl.DataFrame, model_id: str, threads: int, s: Settings) -> tuple[pl.DataFrame, dict[str, int]]:
    import json as _json  # noqa: PLC0415
    import multiprocessing as mp  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    d = s.data_dir / "tmp" / "ccnews" / "work"
    d.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    todo_p, out_p = d / f"sent-{tag}-todo.parquet", d / f"sent-{tag}-out.parquet"
    part.write_parquet(todo_p)
    try:
        pr = mp.get_context("spawn").Process(target=_route_child, args=(str(todo_p), str(out_p), model_id, threads))
        pr.start()
        pr.join(3 * 3600)          # incident 2026-10-24: never wait forever on a child
        if pr.is_alive():
            pr.kill()
            pr.join(30)
            raise RuntimeError(f"sentiment child for {model_id} timed out (killed)")
        if pr.exitcode != 0 or not out_p.exists():
            raise RuntimeError(f"sentiment child for {model_id} failed (exit {pr.exitcode})")
        return pl.read_parquet(out_p), _json.loads(Path(str(out_p) + ".json").read_text(encoding="utf-8"))
    finally:
        for f in (todo_p, out_p, Path(str(out_p) + ".json")):
            try:
                f.unlink()
            except OSError:
                pass


def score_story_sentiment(days: list[date], s: Settings, src_bit: int, max_per_day: int = 1500, threads: int = 4,
                          log: Any = None, model_factory: Any = None) -> dict[str, Any]:
    """FinBERT (en) / XLM-R (other) sentiment for unscored stories carrying `src_bit`, most salient first, one model
    in memory at a time, batched, cached per month. Written back to news_stories and copied to ccnews_articles."""
    from app.nlp.sentiment import EN_MODEL, ML_MODEL  # noqa: PLC0415

    lg = log or log_default
    t0 = time.perf_counter()
    langs = dicts.load_dict("lang", s)
    lmap = dict(zip(langs.get_column("id").to_list(), langs.get_column("code").to_list(), strict=True)) if langs.height else {}
    todo_parts = []
    for d in sorted(set(days)):
        st = load_days("news_stories", d, d, s, columns=["date", "story_id", "title", "lang_id", "source_mask",
                                                          "salience", "sentiment_x100"])
        if st.is_empty() or "sentiment_x100" not in st.columns:
            continue
        t = (st.filter(((pl.col("source_mask").cast(pl.Int32) & (1 << src_bit)) != 0) & pl.col("sentiment_x100").is_null())
             .sort("salience", descending=True).head(max_per_day))
        if t.height:
            todo_parts.append(t.select("date", "story_id", "title", "lang_id"))
    if not todo_parts:
        return {"scored": 0}
    todo = pl.concat(todo_parts).with_columns(
        pl.col("lang_id").replace_strict(lmap, default="en", return_dtype=pl.Utf8).fill_null("en").alias("__lang"))
    results: list[pl.DataFrame] = []
    stats: Counter = Counter()
    for route, model_id in (("en", EN_MODEL), ("xx", ML_MODEL)):
        part = todo.filter((pl.col("__lang") == "en") if route == "en" else (pl.col("__lang") != "en"))
        if part.is_empty():
            continue
        part = part.select("date", "story_id", "title")
        if model_factory is not None:  # in-process (tests)
            r, st = score_titles(part, model_factory(model_id), model_id, s)
        else:  # one model per child process: its memory goes back to the OS when the child exits
            r, st = _score_in_child(part, model_id, threads, s)
        results.append(r)
        stats.update(st)
    if not results:
        return {"scored": 0}
    res = pl.concat(results)
    with merge_lock(s):
        frames: dict[date, pl.DataFrame] = {}
        art_frames: dict[date, pl.DataFrame] = {}
        for (d,), part in res.partition_by("date", as_dict=True).items():
            cur = load_days("news_stories", d, d, s)
            cur = cur.join(part.drop("date"), on="story_id", how="left").with_columns(
                pl.coalesce("sentiment_x100", "__s").alias("sentiment_x100"),
                pl.coalesce("sentiment_model", "__m").alias("sentiment_model")).drop("__s", "__m")
            frames[d] = cur
            ca = load_days(CC_TABLE, d, d, s)
            if ca.height:
                sm = cur.select("story_id", pl.col("sentiment_x100").alias("__s")).unique("story_id")
                art_frames[d] = ca.join(sm, on="story_id", how="left").with_columns(
                    pl.coalesce("sentiment_x100", "__s").alias("sentiment_x100")).drop("__s")
        flush_days("news_stories", frames, s)
        flush_days(CC_TABLE, art_frames, s)
    out = {"scored": res.height, **dict(stats), "seconds": round(time.perf_counter() - t0, 1)}
    lg.info("sentiment: %s", out)
    return out


# ================================================================================================ filing items
def merge_story_rows_days(rows: pl.DataFrame, members: pl.DataFrame | None, s: Settings,
                          today: date | None = None) -> dict[str, int]:
    """Pre-built story rows (one story per filing, STORY_SCHEMA + date) -> news_stories: rows with the same story_id
    on that day are replaced (idempotent); members (SMEM_COLS) only for days < 120 d old."""
    today = today or _today()
    if rows.is_empty():
        return {"days": 0, "stories": 0}
    rows = _ensure_cols(rows, STORY_SCHEMA).unique(["story_id", "date"], keep="first")
    if members is not None and members.height:
        members = members.unique(["url_hash", "date"], keep="first")
    out_st: dict[date, pl.DataFrame] = {}
    out_m: dict[date, pl.DataFrame] = {}
    by_day = {k[0]: v for k, v in rows.partition_by("date", as_dict=True).items()}
    mem_day = {k[0]: v for k, v in members.partition_by("date", as_dict=True).items()} if members is not None and members.height else {}
    lo, hi = min(by_day), max(by_day)
    yms = {d.strftime("%Y-%m") for d in by_day}
    if len(yms) == 1 and has_month_file("news_stories", next(iter(yms)), s):
        # compacted month: replace inside the month rewrite (one copy of the month in memory)
        ym = next(iter(yms))
        new_all = store._prepare("news_stories", rows)
        ids = rows.get_column("story_id").to_list()

        def transform(df: pl.DataFrame, new_all: pl.DataFrame = new_all, ids: list[int] = ids) -> pl.DataFrame:
            keep = df.filter(~pl.col("story_id").is_in(ids))
            cols = list(dict.fromkeys([*keep.columns, *new_all.columns]))
            return pl.concat([keep, new_all], how="diagonal_relaxed").select(cols)
        with merge_lock(s):
            store.compact_month("news_stories", ym, transform, s)
        return {"days": len(by_day), "stories": rows.height}
    with merge_lock(s):
        cur_all = load_days("news_stories", lo, hi, s)
        cur = {k[0]: v for k, v in cur_all.partition_by("date", as_dict=True).items()} if cur_all.height else {}
        mem_all = load_days("story_members", max(lo, today - timedelta(days=MEMBER_DAYS)), hi, s) \
            if hi >= today - timedelta(days=MEMBER_DAYS) else pl.DataFrame()
        mem_cur = {k[0]: v for k, v in mem_all.partition_by("date", as_dict=True).items()} if mem_all.height else {}
        for d, new in by_day.items():
            c = cur.get(d)
            ids = new.get_column("story_id").to_list()
            if c is not None and c.height:
                keep = c.filter(~pl.col("story_id").is_in(ids))
                out_st[d] = pl.concat([keep, new.select([x for x in keep.columns if x in new.columns])],
                                      how="diagonal_relaxed")
            else:
                out_st[d] = new
            if d >= today - timedelta(days=MEMBER_DAYS) and d in mem_day:
                m0 = mem_cur.get(d)
                nm = mem_day[d].select(SMEM_COLS)
                out_m[d] = pl.concat([m0.filter(~pl.col("story_id").is_in(ids)), nm], how="diagonal_relaxed") \
                    if m0 is not None and m0.height else nm
        flush_days("news_stories", out_st, s)
        if out_m:
            flush_days("story_members", out_m, s)
    return {"days": len(out_st), "stories": rows.height}


# ================================================================================================ re-cluster
def recluster(start: date, end: date, s: Settings, min_jaccard: float = 0.6) -> dict[str, Any]:
    """Cross-source merge of near-duplicate stories per day (see module doc)."""
    src = source_ids(s)
    fil_mask = (1 << src[SRC_8K]) | (1 << src[SRC_EXCH])
    gdelt_mask = 0b11
    rep = {"days": 0, "stories": 0, "merged": 0}
    mapping: dict[int, int] = {}
    with merge_lock(s):
        st = load_days("news_stories", start, end, s)
        if st.is_empty():
            return rep
        frames = {k[0]: v for k, v in st.partition_by("date", as_dict=True).items()}
        for d, df in frames.items():
            cand = df.filter((pl.col("source_mask").cast(pl.Int32) & ~fil_mask) != 0)
            if cand.height < 2:
                continue
            fam = (pl.when((pl.col("source_mask").cast(pl.Int32) & gdelt_mask) != 0).then(1).otherwise(0)
                   + pl.when((pl.col("source_mask").cast(pl.Int32) & (1 << src[SRC_CCNEWS])) != 0).then(2).otherwise(0)
                   + pl.when((pl.col("source_mask").cast(pl.Int32) & ~(gdelt_mask | (1 << src[SRC_CCNEWS]) | fil_mask)) != 0)
                   .then(4).otherwise(0))
            cand = cand.with_columns(fam.alias("__fam"))
            titles = cand.get_column("title").to_list()
            lab = cluster_titles([hash_title(t) for t in titles], min_jaccard)
            groups: dict[int, list[int]] = defaultdict(list)
            for i, g in enumerate(lab):
                groups[g].append(i)
            ids = cand.get_column("story_id").to_list()
            fams = cand.get_column("__fam").to_list()
            nart = cand.get_column("n_articles").to_list()
            for idx in groups.values():
                if len(idx) < 2:
                    continue
                f_or = 0
                distinct = set()
                for i in idx:
                    f_or |= fams[i]
                    distinct.add(fams[i])
                if len(distinct) < 2:  # same source family only -> leave the original clustering alone
                    continue
                keep = max(idx, key=lambda i: (nart[i], -ids[i]))
                for i in idx:
                    if ids[i] != ids[keep]:
                        mapping[ids[i]] = ids[keep]
            rep["days"] += 1
            rep["stories"] += df.height
        if not mapping:
            return rep

        def root(x: int) -> int:
            seen = 0
            while x in mapping and seen < 50:
                x = mapping[x]
                seen += 1
            return x
        mp = pl.DataFrame({"story_id": list(mapping), "__new": [root(v) for v in mapping.values()]},
                          schema={"story_id": pl.Int64, "__new": pl.Int64})
        out: dict[date, pl.DataFrame] = {}
        for d, df in frames.items():
            hit = df.join(mp, on="story_id", how="inner")
            if hit.is_empty():
                continue
            df2 = df.join(mp, on="story_id", how="left").with_columns(
                pl.coalesce("__new", "story_id").alias("story_id")).drop("__new")
            out[d] = merge_story_rows(_ensure_cols(df2, STORY_SCHEMA)).with_columns(salience_expr().alias("salience"))
        flush_days("news_stories", out, s)
        rep["merged"] = len(mapping)
        for table in ("story_members", CC_TABLE):
            t = load_days(table, start - timedelta(days=WINDOW_DAYS), end + timedelta(days=WINDOW_DAYS), s)
            if t.is_empty():
                continue
            ch: dict[date, pl.DataFrame] = {}
            for (d,), df in t.partition_by("date", as_dict=True).items():
                if df.join(mp, on="story_id", how="semi").is_empty():
                    continue
                ch[d] = df.join(mp, on="story_id", how="left").with_columns(
                    pl.coalesce("__new", "story_id").alias("story_id")).drop("__new")
            flush_days(table, ch, s)
    return rep


# ================================================================================================ repair / retention
def repair_ccnews(s: Settings, max_days: int = 400, today: date | None = None,
                  force_days: list[date] | None = None) -> dict[str, Any]:
    """(a) merge ccnews days left pending (GDELT not ready at finalize time) once GDELT has them; (b) re-merge days
    whose ccnews stories vanished (a later GDELT re-write of the day replaced the story-day file)."""
    from app.events_v2.ccnews import read_state, update_state  # noqa: PLC0415

    st = read_state(s)
    gd = gdelt_days(s)
    cand: list[date] = []
    lost: list[date] = []
    src = source_ids(s)
    bit = 1 << src[SRC_CCNEWS]
    for k, rec in (st.get("days") or {}).items():
        d = date.fromisoformat(k)
        if rec.get("story_layer") == "pending_gdelt" and day_ready(d, gd):
            cand.append(d)
            continue
        if rec.get("story_layer") != "merged" or not rec.get("cc_stories"):
            continue
        f = store.day_file("news_stories", d, s)
        mf = store.month_file("news_stories", d.strftime("%Y-%m"), s)
        mt = max([x.stat().st_mtime for x in (f, mf) if x.exists()] or [0])
        try:
            merged = datetime.fromisoformat(rec.get("merged_at")).timestamp()
        except (TypeError, ValueError):
            merged = 0
        if mt > merged + 120:
            lost.append(d)
    verified: list[date] = []
    days_rec = st.get("days") or {}
    for d in lost[:max_days]:
        sm = load_days("news_stories", d, d, s, columns=["source_mask"])
        have = 0 if sm.is_empty() else sm.filter((pl.col("source_mask").cast(pl.Int32) & bit) != 0).height
        if have < int((days_rec.get(d.isoformat()) or {}).get("cc_stories") or 0):
            verified.append(d)
    verified += [d for d in (force_days or []) if d not in verified]  # re-attach orphans of these days too
    todo = sorted(set(cand + verified), reverse=True)[:max_days]
    rep: dict[str, Any] = {"pending_ready": len(cand), "lost": len(verified), "remerged": 0}
    if not todo:
        return rep
    names = _names(s)
    by_month: dict[str, list[date]] = defaultdict(list)
    for d in todo:
        by_month[d.strftime("%Y-%m")].append(d)
    for ym, mdays in by_month.items():
        with merge_lock(s):
            win = DayWindow(mdays, s)
            frames = {}
            reps: dict[date, dict[str, Any]] = {}
            for d in mdays:
                a = load_days(CC_TABLE, d, d, s)
                if a.is_empty():
                    continue
                if d in verified:  # stories lost: re-attach only the orphans (articles whose story is gone)
                    have_ids = win.day(d).get_column("story_id").to_list() if win.day(d).height else []
                    # URL duplicates may point at a GDELT story of the adjacent day (dedupe looks at d-1..d+1)
                    orphan = (pl.col("story_id").is_not_null() & pl.col("dup_source_id").is_null()
                              & ~pl.col("story_id").is_in(have_ids))
                    orph_hash = a.filter(orphan).get_column("url_hash").to_list()
                    a = a.with_columns(pl.when(orphan).then(None).otherwise(pl.col("story_id")).alias("story_id"),
                                       pl.when(orphan).then(None).otherwise(pl.col("dup_source_id")).alias("dup_source_id"))
                    if d in win.members and orph_hash:
                        win.members[d] = win.members[d].filter(~pl.col("url_hash").is_in(orph_hash))
                        win.dirty_mem.add(d)
                frames[d], reps[d] = merge_ccnews_day(d, _ensure_cols(a, CC_SCHEMA), win, src[SRC_CCNEWS], names, today)
            flush_days(CC_TABLE, frames, s)
            flush_days("news_stories", {d: win.stories[d] for d in win.dirty_st}, s)
            flush_days("story_members", {d: win.members[d] for d in win.dirty_mem}, s)
        now = datetime.now(UTC).isoformat(timespec="seconds")

        def upd(stt: dict[str, Any], mdays: list[date] = mdays, reps: dict[date, dict[str, Any]] = reps) -> None:
            for d in mdays:
                r = stt.setdefault("days", {}).setdefault(d.isoformat(), {})
                r.update(story_layer="merged", merged_at=now, repaired=True)
                for k in ("url_dupes", "url_dupes_gdelt", "joined_gdelt_story", "new_stories", "linked", "cc_stories"):
                    if d in reps and k in reps[d]:
                        r[k] = reps[d][k]
        update_state(upd, s)
        rep["remerged"] += len(mdays)
    return rep


def prune_ccnews_members(s: Settings, today: date | None = None, lookback_days: int = 75) -> dict[str, int]:
    """Drop CC-NEWS story_members rows older than 120 d (story level only after that; events_retention drops all
    members at 180 d) and compact finished ccnews_articles months older than 120 d into their salient-only form."""
    today = today or _today()
    src = source_ids(s)
    cut = today - timedelta(days=MEMBER_DAYS)
    rep = {"member_days": 0, "members_dropped": 0, "article_months": 0}
    with merge_lock(s):
        start = cut - timedelta(days=lookback_days)
        mem = load_days("story_members", start, cut - timedelta(days=1), s)
        if mem.height:
            ch: dict[date, pl.DataFrame] = {}
            for (d,), df in mem.partition_by("date", as_dict=True).items():
                n = df.filter(pl.col("source_id") == src[SRC_CCNEWS]).height
                if n:
                    ch[d] = df.filter(pl.col("source_id") != src[SRC_CCNEWS])
                    rep["members_dropped"] += n
            flush_days("story_members", ch, s)
            rep["member_days"] = len(ch)
        for ym in store.months(CC_TABLE, s):
            y, m = int(ym[:4]), int(ym[5:7])
            last = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
            if last >= cut or not any(store.month_dir(CC_TABLE, ym, s).glob("d-*.parquet")):
                continue
            store.compact_month(CC_TABLE, ym, prune_articles_salient, s)
            rep["article_months"] += 1
    return rep


def run(ctx):  # noqa: ANN001 - JobContext (job news_recluster, monthly)
    """Monthly: cross-source re-cluster of the last `months` (2) full months + ccnews repair + 120-day retention."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    s = ctx.settings
    today = _today()
    end = date.fromisoformat(p["end"]) if p.get("end") else today - timedelta(days=1)
    start = date.fromisoformat(p["start"]) if p.get("start") else (end.replace(day=1) - timedelta(days=31 * int(p.get("months", 2)))).replace(day=1)
    rep: dict[str, Any] = {"repair": repair_ccnews(s), "retention": prune_ccnews_members(s)}
    rc = {"days": 0, "merged": 0, "stories": 0}
    d = start
    while d <= end:  # month blocks keep memory bounded
        e = min(end, (d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1))
        r = recluster(d, e, s)
        for k in rc:
            rc[k] += int(r.get(k) or 0)
        d = e + timedelta(days=1)
    rep["recluster"] = rc
    return JobResult(status="success", rows_written={"news_stories": rc["merged"]}, entities_ok=rc["days"],
                     message=str(rep))


_ = math


# ================================================================================================ relink
def relink_ccnews(start: date, end: date, s: Settings, ctx: Any = None) -> dict[str, int]:
    """Re-run the free-text issuer linker (current rules) over stored ccnews_articles of [start, end]: links /
    sector masks are replaced; articles left without any label are dropped ('label, then forget'); CC-NEWS-only stories
    get their issuers re-derived from their articles (max confidence x sqrt(member share), 100 when named in the
    canonical title is not recomputed); CC-NEWS-only stories without any label left are dropped with their members."""
    from app.events_v2 import ccnews as C  # noqa: PLC0415
    from app.events_v2 import ccnews_parse as P  # noqa: PLC0415

    ctx = ctx or C.build_label_ctx(s)
    langs = dicts.load_dict("lang", s)
    lmap = dict(zip(langs.get_column("id").to_list(), langs.get_column("code").to_list(), strict=True)) if langs.height else {}
    bit = 1 << source_ids(s)[SRC_CCNEWS]
    sec_of = ctx.issuer_sector
    rep = {"days": 0, "articles": 0, "changed": 0, "dropped_articles": 0, "stories_updated": 0, "stories_dropped": 0}
    d = start
    while d <= end:
        e = min(end, d + timedelta(days=6))
        with merge_lock(s):
            arts = load_days(CC_TABLE, d, e, s)
            if arts.height:
                ids, confs = [], []
                for t, ds, lid in arts.select("title", "description", "lang_id").iter_rows():
                    links = P.link_issuers(t, ds, ctx, lmap.get(lid))
                    ids.append([i for i, _ in links])
                    confs.append([c for _, c in links])
                new = arts.with_columns(pl.Series("__ids", ids, pl.List(pl.Int32)), pl.Series("__cf", confs, pl.List(pl.Int8)))
                rep["changed"] += int(new.filter(pl.col("__ids") != pl.col("issuer_ids")).height)
                sm = [int(tx.theme_sector_mask(tm)) | _or(sec_of.get(i, 0) for i in ii)
                      for tm, ii in new.select("theme_mask", "__ids").iter_rows()]
                new = (new.with_columns(pl.col("__ids").alias("issuer_ids"), pl.col("__cf").alias("issuer_conf"),
                                        pl.Series("sector_mask", sm, pl.Int16)).drop("__ids", "__cf"))
                keep = new.filter((pl.col("theme_mask") != 0) | (pl.col("issuer_ids").list.len() > 0))
                rep["dropped_articles"] += new.height - keep.height
                rep["articles"] += arts.height
                frames = {k[0]: v for k, v in keep.partition_by("date", as_dict=True).items()}
                for day in {x for x in arts.get_column("date").to_list()} - set(frames):
                    frames[day] = keep.head(0)
                # CC-NEWS-only stories: issuers from their (kept) articles
                st = load_days("news_stories", d, e, s)
                st_frames: dict[date, pl.DataFrame] = {}
                mem_frames: dict[date, pl.DataFrame] = {}
                if st.height:
                    agg = (keep.filter(pl.col("story_id").is_not_null()).select("story_id", "issuer_ids", "issuer_conf")
                           .explode(["issuer_ids", "issuer_conf"]).drop_nulls("issuer_ids")
                           .group_by("story_id", "issuer_ids").agg(pl.len().alias("__k"), pl.col("issuer_conf").max()))
                    n_art = keep.group_by("story_id").len().rename({"len": "__n"})
                    agg = agg.join(n_art, on="story_id").with_columns(
                        (pl.col("issuer_conf").cast(pl.Float64) * (pl.col("__k") / pl.col("__n")).sqrt()).round()
                        .clip(0, 100).cast(pl.Int8).alias("__rel")).sort(["story_id", "__rel"], descending=[False, True])
                    agg = agg.group_by("story_id", maintain_order=True).agg(
                        pl.col("issuer_ids").head(10).cast(pl.Int32).alias("__ii"), pl.col("__rel").head(10).alias("__rr"))
                    cc_only = pl.col("source_mask").cast(pl.Int32) == bit
                    st2 = st.join(agg, on="story_id", how="left").with_columns(
                        pl.when(cc_only).then(pl.col("__ii").fill_null(pl.lit([], pl.List(pl.Int32)))).otherwise(pl.col("issuer_ids")).alias("issuer_ids"),
                        pl.when(cc_only).then(pl.col("__rr").fill_null(pl.lit([], pl.List(pl.Int8)))).otherwise(pl.col("issuer_rel")).alias("issuer_rel")
                    ).drop("__ii", "__rr")
                    alive = set(keep.get_column("story_id").drop_nulls().to_list())
                    drop = st2.filter(cc_only & (pl.col("theme_mask") == 0) & (pl.col("issuer_ids").list.len() == 0)
                                      | (cc_only & ~pl.col("story_id").is_in(list(alive))))
                    drop_ids = drop.get_column("story_id").to_list()
                    st2 = st2.filter(~pl.col("story_id").is_in(drop_ids)).with_columns(salience_expr().alias("salience"))
                    rep["stories_dropped"] += len(drop_ids)
                    rep["stories_updated"] += int(st.filter(cc_only).height) - len(drop_ids)
                    st_frames = {k[0]: v for k, v in st2.partition_by("date", as_dict=True).items()}
                    gone = new.join(keep.select("url_hash", "date"), on=["url_hash", "date"], how="anti") \
                        .get_column("url_hash").to_list()
                    if drop_ids or gone:
                        mem = load_days("story_members", d, e, s)
                        if mem.height:
                            mem_frames = {k[0]: v.filter(~pl.col("story_id").is_in(drop_ids)
                                                         & ~pl.col("url_hash").is_in(gone))
                                          for k, v in mem.partition_by("date", as_dict=True).items()}
                flush_days(CC_TABLE, frames, s)
                if st_frames:
                    flush_days("news_stories", st_frames, s)
                if mem_frames:
                    flush_days("story_members", mem_frames, s)
                rep["days"] += len(frames)
        d = e + timedelta(days=1)
    rep["recount"] = recount_from_members(start, end, s)
    counts = cc_story_counts(start, end, s)

    def upd(stt: dict[str, Any]) -> None:
        for k, rec in (stt.get("days") or {}).items():
            if start.isoformat() <= k <= end.isoformat():
                rec["cc_stories"] = counts.get(k, 0)
    C.update_state(upd, s)
    return rep


def recount_from_members(start: date, end: date, s: Settings) -> dict[str, int]:
    """n_articles / n_sources of story-days := their story_members (complete inside the member window); story-days
    without members are left as they are; salience recomputed; duplicate member rows dropped."""
    rep = {"days": 0, "changed": 0}
    d = start
    while d <= end:
        e = min(end, d + timedelta(days=6))
        with merge_lock(s):
            mem = load_days("story_members", d, e, s)
            if mem.height:
                n0 = mem.height
                mem = mem.unique(["date", "url_hash"], keep="first")
                cnt = mem.group_by("date", "story_id").agg(
                    pl.len().cast(pl.Int32).alias("__n"),
                    pl.col("domain_id").n_unique().clip(1, 32767).cast(pl.Int16).alias("__s"))
                st = load_days("news_stories", d, e, s)
                st2 = st.join(cnt, on=["date", "story_id"], how="left").with_columns(
                    pl.coalesce("__n", "n_articles").alias("n_articles"),
                    pl.coalesce("__s", "n_sources").alias("n_sources")).drop("__n", "__s")
                old = st.select("date", "story_id", pl.col("n_articles").alias("__o"))
                rep["changed"] += int(st2.join(old, on=["date", "story_id"]).filter(
                    pl.col("n_articles") != pl.col("__o")).height)
                st2 = st2.with_columns(salience_expr().alias("salience"))
                flush_days("news_stories", {k[0]: v for k, v in st2.partition_by("date", as_dict=True).items()}, s)
                if mem.height < n0:
                    flush_days("story_members", {k[0]: v for k, v in mem.partition_by("date", as_dict=True).items()}, s)
                rep["days"] += st2.get_column("date").n_unique()
        d = e + timedelta(days=1)
    return rep


def cc_story_counts(start: date, end: date, s: Settings) -> dict[str, int]:
    """Stories carrying the ccnews bit per day (the repair's expected count)."""
    bit = 1 << source_ids(s)[SRC_CCNEWS]
    st = load_days("news_stories", start, end, s, columns=["date", "source_mask"])
    if st.is_empty():
        return {}
    g = st.filter((pl.col("source_mask").cast(pl.Int32) & bit) != 0).group_by("date").len()
    return {d.isoformat(): int(n) for d, n in g.iter_rows()}


def _or(xs: Any) -> int:
    m = 0
    for x in xs:
        m |= int(x or 0)
    return m
