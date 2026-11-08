"""Job `story_cluster` (E4): non-GDELT news into the story layer + per-story sentiment.

1. Merge: news_items of the window's days from every non-GKG source (Polygon, RSS, GDELT DOC, yfinance, SEC 8-K)
   are hashed with the same URL key as GDELT, labelled (issuers from news_items.issuer_ids with their link
   confidence, countries, English headline theme rules, theme/issuer sectors) and clustered into the existing stories
   of +-3 days (MinHash-LSH, Jaccard >= 0.6) or into new stories. Items already in story_members are skipped, so the
   job is idempotent. (news_items rows with source_id gdelt_gkg are legacy duplicates of GKG titles and are skipped.)
2. Sentiment once per story: canonical title -> FinBERT (English) / XLM-R (other languages) through the shared
   app.nlp.sentiment scorer and its on-disk cache; stories without sentiment, most salient first, bounded by
   max_stories / max_minutes. Stored as sentiment_x100 (P(pos) - P(neg)) + sentiment_model.
params: days (window, default 3), sentiment (default true), max_stories (4000), max_minutes (20), threads (8),
        min_salience (0: score everything in salience order), start / end (explicit window, YYYY-MM-DD).
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table
from app.events_v2 import dicts, store
from app.events_v2 import taxonomy as tx
from app.events_v2.context import issuer_meta
from app.events_v2.parse import hash_series, url_key_expr
from app.events_v2.stories import MEMBER_SCHEMA, STORY_SCHEMA, WINDOW_DAYS, build_day_stories, merge_story_rows
from app.nlp.common import has_data

log = get_logger(__name__)


def news_members(d: date, s: Settings, imeta: pl.DataFrame) -> pl.DataFrame:
    """Non-GKG news_items published on UTC day d -> MEMBER_SCHEMA rows (+ source code column __src)."""
    if not has_data("news_items", s):
        return pl.DataFrame(schema=MEMBER_SCHEMA)
    n = (scan_table("news_items", s).filter(pl.col("year_month") == d.strftime("%Y-%m"))
         .filter((pl.col("source_id") != "gdelt_gkg") & (pl.col("published_at").dt.date() == d))
         .select("source_id", "published_at", "publisher", "url", "title", "language", "issuer_ids",
                 "link_confidence", "issuer_relevance", "countries", "gdelt_tone").collect())
    if n.is_empty():
        return pl.DataFrame(schema=MEMBER_SCHEMA)
    iid = dict(zip(imeta.get_column("issuer_id").to_list(), imeta.get_column("iid").to_list(), strict=True))
    cty_of = dict(zip(imeta.get_column("iid").to_list(), imeta.get_column("country").to_list(), strict=True))
    sec_of = dict(zip(imeta.get_column("iid").to_list(), imeta.get_column("sector_mask").to_list(), strict=True))
    src = dicts.assign_ids("source", n.get_column("source_id").unique().to_list(), s)
    dom = dicts.assign_ids("domain", n.get_column("publisher").drop_nulls().str.to_lowercase().unique().to_list(), s)
    lang = dicts.assign_ids("lang", n.get_column("language").drop_nulls().unique().to_list(), s)
    rows = []
    for r in n.iter_rows(named=True):
        ids, confs = [], []
        rel = r["issuer_relevance"] or r["link_confidence"] or []
        for k, x in enumerate(r["issuer_ids"] or []):
            if x in iid:
                c = rel[k] if k < len(rel) and rel[k] is not None else 0.8
                if c >= 0.5:
                    ids.append(iid[x])
                    confs.append(int(round(min(1.0, c) * 100)))
        title = r["title"] or ""
        tm = tx.title_theme_mask(title) if (r["language"] or "en") == "en" else 0
        sm = tx.theme_sector_mask(tm)
        for i in ids:
            sm |= int(sec_of.get(i) or 0)
        cs = [tx.COUNTRY_ID[c] for c in (r["countries"] or []) if c in tx.COUNTRY_ID][: tx.MAX_COUNTRIES]
        if not cs:
            cs = [tx.COUNTRY_ID[c] for c in dict.fromkeys(cty_of.get(i) for i in ids) if c in tx.COUNTRY_ID][:3]
        ts = r["published_at"]
        rows.append({"url": r["url"], "title": title, "minute": ts.hour * 60 + ts.minute,
                     "domain_id": dom.get((r["publisher"] or "").lower()), "lang_id": lang.get(r["language"]),
                     "source_id": src.get(r["source_id"]),
                     "tone_x100": None if r["gdelt_tone"] is None else int(round(r["gdelt_tone"] * 100)),
                     "theme_mask": tm, "sector_mask": sm, "countries": cs or [tx.GLOBAL_ID], "issuer_ids": ids,
                     "issuer_conf": confs, "n_events": 0})
    df = pl.DataFrame(rows, schema={k: v for k, v in MEMBER_SCHEMA.items() if k != "url_hash"})
    return df.with_columns(hash_series(df.select(url_key_expr(pl.col("url"))).to_series()).alias("url_hash")) \
        .select(list(MEMBER_SCHEMA)).unique("url_hash", keep="first")


def merge_day(d: date, s: Settings, imeta: pl.DataFrame, names: dict[int, tuple[str, str]]) -> dict[str, int]:
    mem = news_members(d, s, imeta)
    if mem.is_empty():
        return {"items": 0, "new_members": 0}
    have = store.read_range("story_members", d, d, s)
    if have.height:
        mem = mem.join(have.select("url_hash"), on="url_hash", how="anti")
    if mem.is_empty():
        return {"items": 0, "new_members": 0}
    nb = store.read_range("news_stories", d - timedelta(days=WINDOW_DAYS), d + timedelta(days=WINDOW_DAYS), s,
                          columns=["date", "story_id", "title", "n_articles"])
    new_st, new_m = build_day_stories(d, mem, nb, names)
    cur = store.read_range("news_stories", d, d, s)
    stories = pl.concat([cur.select(list(STORY_SCHEMA)), new_st.select(list(STORY_SCHEMA))], how="vertical_relaxed") \
        if cur.height else new_st
    if stories.get_column("story_id").n_unique() < stories.height:
        stories = merge_story_rows(stories)
    store.write_day("news_stories", d, stories, s)
    members = pl.concat([have, new_m.select(have.columns)], how="vertical_relaxed") if have.height else new_m
    store.write_day("story_members", d, members, s)
    return {"items": mem.height, "new_members": new_m.height}


def score_sentiment(days: list[date], s: Settings, max_stories: int = 4000, max_minutes: float = 20.0,
                    threads: int = 8, min_salience: float = 0.0) -> dict[str, Any]:
    from app.nlp.sentiment import SentimentScorer  # noqa: PLC0415

    # day files can differ in columns (days older than 180 d are written already compacted) -> align by name
    st = pl.concat([store.read_range("news_stories", d, d, s) for d in days], how="diagonal_relaxed")
    if "sentiment_x100" not in st.columns:
        st = st.with_columns(pl.lit(None, dtype=pl.Int8).alias("sentiment_x100"))
    todo = st.filter(pl.col("sentiment_x100").is_null() & (pl.col("salience") >= min_salience)) \
        .sort("salience", descending=True).head(max_stories)
    if todo.is_empty():
        return {"scored": 0}
    langs = dicts.load_dict("lang", s)
    lmap = dict(zip(langs.get_column("id").to_list(), langs.get_column("code").to_list(), strict=True)) if langs.height else {}
    scorer = SentimentScorer(s, threads=threads)
    t0 = time.perf_counter()
    out: list[pl.DataFrame] = []
    for chunk in todo.iter_slices(256):
        texts = chunk.get_column("title").to_list()
        lg = [lmap.get(x, "en") or "en" for x in chunk.get_column("lang_id").to_list()]
        res = scorer.score(texts, lg)
        out.append(chunk.select("story_id", "date").with_columns(
            (res.get_column("sentiment_score") * 100).round().cast(pl.Int8).alias("__s"),
            res.get_column("sentiment_model").alias("__m")))
        if time.perf_counter() - t0 > max_minutes * 60:
            break
    sc = pl.concat(out)
    for (dd,), part in sc.partition_by("date", as_dict=True).items():
        cur = store.read_range("news_stories", dd, dd, s)
        cur = cur.join(part.drop("date"), on="story_id", how="left").with_columns(
            pl.coalesce("sentiment_x100", "__s").alias("sentiment_x100"),
            pl.coalesce("sentiment_model", "__m").alias("sentiment_model")).drop("__s", "__m")
        store.write_day("news_stories", dd, cur, s)
    return {"scored": sc.height, "cache_hits": scorer.stats.get("cache_hits"), "model_scored": scorer.stats.get("scored"),
            "seconds": round(time.perf_counter() - t0, 1)}


def run_story_cluster(settings: Settings | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    p = params or {}
    end = date.fromisoformat(p["end"]) if p.get("end") else datetime.now(UTC).date()
    start = date.fromisoformat(p["start"]) if p.get("start") else end - timedelta(days=int(p.get("days", 3)) - 1)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    idict = dicts.load_dict("issuer", s)
    imeta = issuer_meta(idict)
    names = {int(r["iid"]): (r["name_norm"] or "", (r["ticker"] or "").lower()) for r in imeta.iter_rows(named=True)}
    rep: dict[str, Any] = {"days": [d.isoformat() for d in days], "merge": {}}
    for d in days:
        rep["merge"][d.isoformat()] = merge_day(d, s, imeta, names)
    if str(p.get("sentiment", "true")).lower() not in ("0", "false", "no"):
        rep["sentiment"] = score_sentiment(days, s, int(p.get("max_stories", 4000)), float(p.get("max_minutes", 20)),
                                           int(p.get("threads", 8)), float(p.get("min_salience", 0)))
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    rep = run_story_cluster(ctx.settings, ctx.params or {})
    items = sum(v["items"] for v in rep["merge"].values())
    return JobResult(status="success", rows_written={"news_stories": items, "story_members": items},
                     entities_ok=items, message=str({"merge_items": items, "sentiment": rep.get("sentiment")}))
