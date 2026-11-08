"""Hourly compaction of the live news hot store (roadmap v3 N3.1) and the daily finalise (N3.2).

Hourly fold (job news_hourly, `hourly` DAG, cron :10 every hour; also folds hours whose inputs changed since):
for every closed UTC hour h of the last 30 h whose inputs changed (fingerprint of the GDELT slot manifests and the
news hot files):
  * GDELT: the parsed slot frames of the hour (slots HH:00..HH:45) -> events <-> articles linking, dictionaries,
    `event_hourly_agg` (events_v2 day_aggregates over the hour), T2 rows (gdelt_event_labels / gdelt_article_labels
    keep rules), story members;
  * live news items of the hour (items_hHH.parquet, first-seen hour) -> story members (RSS / Polygon sources);
  * stories: members keep the story id the news desk assigned live (assign_hHH.parquet) - the same ids the terminal
    shows; members the desk did not see are clustered with the events_v2 rule (MinHash-LSH, Jaccard >= 0.6) and
    linked to the stories of +-3 days and of the day's earlier hours;
  * sentiment: English items carry the live FinBERT score; other languages are scored here with XLM-R (cached,
    <= `max_xlmr` per run) -> `sentiment_hourly` per issuer / sector / country / theme (+ all);
  * outputs of the hour -> data/realtime/news_hot/<day>/fold_hHH/ (idempotent rewrite), then the day's lake files
    are rebuilt from all its folded hours: PARTIAL day files (p-YYYYMMDD.parquet) of news_stories, story_members,
    gdelt_article_labels and gdelt_event_labels (app.events_v2.store.write_partial) and the day files of the new
    tables event_hourly_agg / sentiment_hourly.

Daily finalise (job news_finalise, same DAG; acts once per UTC day, >= 40 min after midnight):
  1. last fold of the day's hours;
  2. full-day GDELT re-assembly from the hot slots with the nightly pipeline (app.events_v2.pipeline.run_days,
     legacy outputs included, label `live`): re-clusters the whole day, writes the d- files of the five v2 tables,
     checkpoints the day in data/state/events_v2.json - so the nightly gdelt_stream, the deep backfill and the gap
     filler see it as done; files the live service missed are downloaded once here. Skipped when the nightly
     already processed the day;
  3. the day's live news members merged into the re-clustered stories (cross-source attach, idempotent anti-join);
     story sentiment (FinBERT / XLM-R per story, app.events_v2.story_job.score_sentiment);
  4. roll-ups: sentiment_hourly -> sentiment_label_daily (+ 30-day z); event_daily_agg is the full-day assembly of
     step 2 (+ T1 z-scores);
  5. partial files of the day removed; retention of the hot store (GDELT slots 2 days after finalise, news hours
     8 days); checkpoint in data/state/news_live.json.

Incident 2026-10-24 (docs/INCIDENT_2026-10-24.md): the finalise of 2026-10-23 (live service up only from ~12:00 UTC,
so ~290 of 576 GDELT files had to be downloaded) ran > 2 h twice, ~6 GB, until killed by the 180-min timeout: not a
deadlock - the main process was waiting on the parse pool while the two pool workers, each with a Polars thread per
core (80+ threads), crawled on a machine already at 85 % CPU and paging; every downloaded zip was queued in the main
process. Now:
  * step 2 runs with FINALISE_PARSE_WORKERS workers capped at FINALISE_PARSE_THREADS threads each, at most
    FINALISE_MAX_INFLIGHT files in memory, and a FINALISE_MEM_BUDGET_MB budget for the job's process tree (over it
    the day is not checkpointed and is retried later / left to the nightly);
  * a run marker (data/state/news_live_finalise.json) guards against overlapping runs: a live marker -> "already
    running" (skipped quickly, never queued behind); a marker whose pid is dead or older than FINALISE_STALE_S is
    stale and is taken over (a stale live process of ours is killed first);
  * per-day attempts: a day that failed / timed out FINALISE_MAX_ATTEMPTS times waits FINALISE_RETRY_AFTER_H
    before the next try, so one bad day never blocks the hourly DAG every hour;
  * data/state/ops/news_finalise.HOLD (operator switch) defers the finalise entirely.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.news_live import hot

log = get_logger(__name__)

FOLD_GRACE = timedelta(minutes=8)
LOOKBACK_H = 30
FINALISE_AFTER = timedelta(minutes=40)
FINALISE_PARSE_WORKERS = 2
FINALISE_PARSE_THREADS = 2
FINALISE_MAX_INFLIGHT = 8
FINALISE_MEM_BUDGET_MB = float(os.environ.get("BIG360_FINALISE_MEM_MB", "3072"))  # hard stop (tree); target ~2 GB
FINALISE_STALE_S = 50 * 60          # > the job timeout (40 min, contracts/jobs.yaml) + grace
FINALISE_MAX_ATTEMPTS = 3
FINALISE_RETRY_AFTER_H = 6.0
V2_PARTIAL_TABLES = ("news_stories", "story_members", "gdelt_article_labels", "gdelt_event_labels")
#: live source id -> events_v2 `source` dictionary code (that dictionary holds <= 15 codes: publishers -> domain ids)
EXISTING_SOURCE_CODES = {"rss_fed", "rss_ecb", "rss_sec", "polygon_news", "gdelt_doc", "yfinance_news", "sec_8k"}


# ================================================================================================ state
def state_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "news_live.json"


def read_state(settings: Settings | None = None) -> dict[str, Any]:
    return hot.read_json(state_path(settings))


def finalise_hold_path(settings: Settings | None = None) -> Path:
    """Present = the daily finalise is deferred (operator switch, e.g. during an incident or a manual re-run)."""
    return (settings or get_settings()).state_dir / "ops" / "news_finalise.HOLD"


def update_state(fn: Any, settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        st = hot.read_json(p)
        fn(st)
        st["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        hot.write_json_atomic(st, p)
    return st


def live_source_code(source_id: str | None, tier: int | None) -> str:
    s = source_id or ""
    if s in EXISTING_SOURCE_CODES:
        return s
    if s.startswith("polygon"):
        return "polygon_news"
    return "rss_official" if (tier or 3) == 1 else "rss_publisher"


# ================================================================================================ label maps
class Maps:
    """events_v2 dictionaries needed by the fold (issuer ids, names, sectors, countries, source / lang codes)."""

    def __init__(self, settings: Settings):
        from app.events_v2 import dicts  # noqa: PLC0415
        from app.events_v2.context import issuer_meta  # noqa: PLC0415

        self.s = settings
        im = issuer_meta(dicts.load_dict("issuer", settings))
        self.iid_of: dict[str, int] = dict(zip(im.get_column("issuer_id").to_list(), im.get_column("iid").to_list(),
                                               strict=True))
        self.issuer_of: dict[int, str] = {v: k for k, v in self.iid_of.items()}
        self.sector_of: dict[int, int] = {int(i): int(m or 0) for i, m in zip(
            im.get_column("iid").to_list(), im.get_column("sector_mask").to_list(), strict=True)}
        self.country_of: dict[int, str] = {int(i): c for i, c in zip(
            im.get_column("iid").to_list(), im.get_column("country").to_list(), strict=True) if c}
        self.names: dict[int, tuple[str, str]] = {int(r["iid"]): (r["name_norm"] or "", (r["ticker"] or "").lower())
                                                  for r in im.iter_rows(named=True)}

    def ids(self, kind: str, codes: list[str]) -> dict[str, int]:
        from app.events_v2 import dicts  # noqa: PLC0415

        return dicts.assign_ids(kind, codes, self.s)


# ================================================================================================ inputs
def hour_slots(d: date, h: int) -> list[datetime]:
    return [datetime(d.year, d.month, d.day, h, 15 * i, tzinfo=UTC) for i in range(4)]


def fingerprint(d: date, h: int, settings: Settings) -> str:
    parts: list[str] = []
    for s in hour_slots(d, h):
        p = hot.manifest_path(s, settings)
        if p.exists():
            st = p.stat()
            parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
    for p in (hot.items_path(d, h, settings), hot.assign_path(d, h, settings)):
        if p.exists():
            st = p.stat()
            parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
    if not parts:
        return ""          # nothing of this hour (the next hour's assignments alone do not make a fold)
    nxt = hot.assign_path(d, h + 1, settings) if h < 23 else hot.assign_path(d + timedelta(days=1), 0, settings)
    if nxt.exists():
        st = nxt.stat()
        parts.append(f"next:{st.st_mtime_ns}:{st.st_size}")
    return hashlib.sha1("|".join(parts).encode()).hexdigest()[:16]


def gdelt_hour(d: date, h: int, settings: Settings) -> dict[str, Any]:
    out: dict[str, Any] = {"articles": [], "events": [], "mentions": [], "slots": 0}
    for s in hour_slots(d, h):
        man = hot.read_manifest(s, settings)
        if not man:
            continue
        if any((man.get(k) or {}).get("status") == "ok" for k in ("export", "gkg")):
            out["slots"] += 1
        for k in hot.GDELT_KINDS:
            got = hot.load_slot_kind(s, k, False, settings)
            if got is None or got[0] != "ok" or got[1] is None:
                continue
            base = k.split(".")[-1]
            out["articles" if base == "gkg" else "events" if base == "export" else "mentions"].append(got[1])
    return out


def _concat(frames: list[pl.DataFrame]) -> pl.DataFrame | None:
    frames = [f for f in frames if f is not None and f.height]
    return pl.concat(frames, how="diagonal_relaxed") if frames else None


# ================================================================================================ members
def gdelt_members(arts: pl.DataFrame, src_id: int) -> pl.DataFrame:
    from app.events_v2.stories import MEMBER_SCHEMA  # noqa: PLC0415
    from app.nlp.gdelt_link import headline_from_url  # noqa: PLC0415

    titled = arts.with_columns(pl.coalesce(pl.col("title"), pl.col("url").map_elements(
        headline_from_url, return_dtype=pl.Utf8)).alias("title"))
    return titled.select([pl.col(c).cast(t) if c in titled.columns else pl.lit(None).cast(t).alias(c)
                          for c, t in MEMBER_SCHEMA.items() if c != "source_id"]
                         + [pl.lit(src_id, pl.Int8).alias("source_id")]).select(list(MEMBER_SCHEMA))


def news_members(items: pl.DataFrame, maps: Maps) -> pl.DataFrame:
    """Live RSS / Polygon items (news desk rows) -> MEMBER_SCHEMA (+ item_key)."""
    from app.events_v2 import taxonomy as tx  # noqa: PLC0415
    from app.events_v2.stories import MEMBER_SCHEMA  # noqa: PLC0415

    n = items.filter(pl.col("kind") == "news") if "kind" in items.columns else items
    if n.is_empty():
        return pl.DataFrame(schema={**MEMBER_SCHEMA, "item_key": pl.Utf8})
    srcs = {live_source_code(r["source_id"], r.get("tier")) for r in n.select("source_id", "tier").iter_rows(named=True)}
    src = maps.ids("source", sorted(srcs))
    dom = maps.ids("domain", [str(x).lower() for x in n.get_column("publisher").drop_nulls().unique().to_list()])
    lang = maps.ids("lang", n.get_column("language").drop_nulls().unique().to_list())
    rows = []
    for r in n.iter_rows(named=True):
        ids, confs = [], []
        for i in r.get("issuer_ids") or []:
            iid = maps.iid_of.get(i)
            if iid is not None and iid not in ids:
                ids.append(iid)
                confs.append(90 if r.get("linked_by") == "provider" else 80)
        tm = 0
        for t in r.get("themes") or []:
            th = tx.THEME_BY_ID.get(t)
            if th is not None:
                tm |= 1 << th.bit
        sm = tx.sector_mask(r.get("sectors") or []) | tx.theme_sector_mask(tm)
        for i in ids:
            sm |= maps.sector_of.get(i, 0)
        cs = [tx.COUNTRY_ID[c] for c in (r.get("countries") or []) if c in tx.COUNTRY_ID][: tx.MAX_COUNTRIES]
        if not cs:
            cs = [tx.COUNTRY_ID[c] for c in dict.fromkeys(maps.country_of.get(i) for i in ids) if c in tx.COUNTRY_ID][:3]
        fs = datetime.fromisoformat(str(r["first_seen_at"]).replace("Z", "+00:00"))
        rows.append({"url_hash": int(r["url_hash"]), "url": r["url"], "title": r["title"],
                     "minute": fs.hour * 60 + fs.minute, "domain_id": dom.get(str(r.get("publisher") or "").lower()),
                     "lang_id": lang.get(r.get("language")),
                     "source_id": src.get(live_source_code(r["source_id"], r.get("tier"))),
                     "tone_x100": None, "theme_mask": tm, "sector_mask": sm, "countries": cs or [tx.GLOBAL_ID],
                     "issuer_ids": ids, "issuer_conf": confs, "n_events": 0, "item_key": r["item_key"]})
    return pl.DataFrame(rows, schema={**MEMBER_SCHEMA, "item_key": pl.Utf8}).unique("url_hash", keep="first")


# ================================================================================================ sentiment
class XlmrScorer:
    """XLM-R (cardiffnlp/twitter-xlm-roberta-base-sentiment) for non-English live items, shared cache; lazy."""

    def __init__(self, settings: Settings, threads: int = 4, enabled: bool = True):
        self.s = settings
        self.threads = threads
        self.enabled = enabled
        self.scorer: Any = None
        self.error: str | None = None
        self.n = 0

    def score(self, texts: list[str]) -> list[float | None]:
        if not texts or not self.enabled:
            return [None] * len(texts)
        if self.scorer is None:
            try:
                from app.nlp import sentiment as sm  # noqa: PLC0415

                xx = sm.HFSentiment(sm.ML_MODEL, self.threads, 96)
                self.scorer = sm.SentimentScorer(self.s, threads=self.threads, batch_size=32, models={"xx": xx})
            except Exception as e:  # noqa: BLE001
                self.error, self.enabled = f"{type(e).__name__}: {e}"[:200], False
                return [None] * len(texts)
        try:
            df = self.scorer.score(texts, ["xx"] * len(texts))
        except Exception as e:  # noqa: BLE001
            self.error, self.enabled = f"{type(e).__name__}: {e}"[:200], False
            return [None] * len(texts)
        self.n += len(texts)
        return df.get_column("sentiment_score").to_list()


def _sent_rows(d: date, h: int, items: pl.DataFrame | None, arts: pl.DataFrame | None, maps: Maps,
               now: datetime) -> pl.DataFrame:
    """sentiment_hourly rows of one hour."""
    from app.events_v2 import taxonomy as tx  # noqa: PLC0415

    acc: dict[tuple[str, str], dict[str, Any]] = defaultdict(lambda: {"n": 0, "ns": 0, "sum": 0.0, "pos": 0, "neg": 0,
                                                                     "src": set(), "t1": 0, "an": 0, "at": 0.0,
                                                                     "atn": 0})

    def labels_of(r: dict[str, Any]) -> list[tuple[str, str]]:
        out = [("all", "*")]
        out += [("issuer", i) for i in (r.get("issuer_ids") or [])]
        out += [("sector", x) for x in (r.get("sectors") or [])]
        out += [("country", x) for x in (r.get("countries") or [])]
        out += [("theme", x) for x in (r.get("themes") or [])]
        return list(dict.fromkeys(out))

    if items is not None and items.height:
        for r in items.iter_rows(named=True):
            s = r.get("sentiment_score")
            for k in labels_of(r):
                a = acc[k]
                a["n"] += 1
                a["src"].add(r.get("publisher") or r.get("source_id"))
                if int(r.get("tier") or 4) == 1:
                    a["t1"] += 1
                if s is not None and not (isinstance(s, float) and math.isnan(s)):
                    a["ns"] += 1
                    a["sum"] += float(s)
                    a["pos"] += int(s > 0.2)
                    a["neg"] += int(s < -0.2)
    if arts is not None and arts.height:
        cid = {i: c for c, i in tx.COUNTRY_ID.items()}
        for tm, smk, cl, ids, tone in arts.select("theme_mask", "sector_mask", "countries", "issuer_ids",
                                                  "tone_x100").iter_rows():
            labs = [("all", "*")] + [("theme", t) for t in tx.mask_to_theme_ids(int(tm or 0))] \
                + [("sector", x) for x in tx.sectors_of_mask(int(smk or 0))] \
                + [("country", cid.get(int(c), "GLOBAL")) for c in (cl or [])] \
                + [("issuer", maps.issuer_of[int(i)]) for i in (ids or []) if int(i) in maps.issuer_of]
            for k in dict.fromkeys(labs):
                a = acc[k]
                a["an"] += 1
                if tone is not None:
                    a["at"] += tone / 100.0
                    a["atn"] += 1
    rows = []
    for (lt, lid), a in acc.items():
        rows.append({"date": d, "hour": h, "label_type": lt, "label_id": lid, "n_items": a["n"], "n_scored": a["ns"],
                     "sentiment_mean": (a["sum"] / a["ns"]) if a["ns"] else None,
                     "pos_share": (a["pos"] / a["ns"]) if a["ns"] else None,
                     "neg_share": (a["neg"] / a["ns"]) if a["ns"] else None,
                     "n_sources": min(32767, len(a["src"])) if a["n"] else None, "tier1_n": a["t1"],
                     "art_n": a["an"], "art_tone": (a["at"] / a["atn"]) if a["atn"] else None, "computed_at": now})
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, schema={"date": pl.Date, "hour": pl.Int8, "label_type": pl.Utf8, "label_id": pl.Utf8,
                                      "n_items": pl.Int32, "n_scored": pl.Int32, "sentiment_mean": pl.Float32,
                                      "pos_share": pl.Float32, "neg_share": pl.Float32, "n_sources": pl.Int16,
                                      "tier1_n": pl.Int32, "art_n": pl.Int32, "art_tone": pl.Float32,
                                      "computed_at": pl.Datetime("us", "UTC")})


# ================================================================================================ fold
def _read_items(d: date, h: int, settings: Settings) -> pl.DataFrame | None:
    df = hot.read_parquet(hot.items_path(d, h, settings))
    if df is None or not df.height:
        return None
    return df.unique("item_key", keep="last", maintain_order=True)


def _assignments(d: date, h: int, settings: Settings) -> dict[int, int]:
    out: dict[int, int] = {}
    hours = [(d, h)] + ([(d, h + 1)] if h < 23 else [(d + timedelta(days=1), 0)])
    for dd, hh in hours:
        df = hot.read_parquet(hot.assign_path(dd, hh, settings))
        if df is not None and df.height:
            for uh, sid in df.select("url_hash", "story_id").iter_rows():
                out.setdefault(int(uh), int(sid))
    return out


def earlier_stories(d: date, h: int, settings: Settings) -> pl.DataFrame | None:
    """[date, story_id, title, n_articles] of the members folded in hours < h of day d."""
    frames = []
    for hh in range(h):
        f = hot.read_parquet(hot.fold_dir(d, hh, settings) / "members.parquet")
        if f is not None and f.height:
            frames.append(f.select("story_id", "title"))
    if not frames:
        return None
    m = pl.concat(frames, how="vertical_relaxed")
    return m.group_by("story_id", maintain_order=True).agg(pl.col("title").first(), pl.len().cast(pl.Int32)
                                                           .alias("n_articles"))         .with_columns(pl.lit(d).alias("date")).select("date", "story_id", "title", "n_articles")


def fold_hour(d: date, h: int, settings: Settings, maps: Maps, xlmr: XlmrScorer | None = None,
              now: datetime | None = None, max_xlmr: int = 1500) -> dict[str, Any]:
    """Fold one UTC hour of the hot store into its fold directory (idempotent rewrite)."""
    from app.events_v2.pipeline import day_aggregates, event_salience_expr, keep_event_expr, link_events  # noqa: PLC0415
    from app.events_v2.store import read_range  # noqa: PLC0415
    from app.events_v2.stories import WINDOW_DAYS, build_day_stories  # noqa: PLC0415

    now = now or datetime.now(UTC)
    t0 = time.perf_counter()
    fd = hot.fold_dir(d, h, settings)
    rep: dict[str, Any] = {"day": d.isoformat(), "hour": h}
    g = gdelt_hour(d, h, settings)
    arts = _concat(g["articles"])
    ev = _concat(g["events"])
    men = _concat(g["mentions"])
    members: list[pl.DataFrame] = []
    outs: dict[str, pl.DataFrame] = {}
    if arts is not None or ev is not None:
        from app.events_v2.parse import empty_articles, empty_events  # noqa: PLC0415

        arts = (arts if arts is not None else empty_articles()).filter(pl.col("date") == d) \
            .sort("translated").unique("url_hash", keep="first")
        ev = (ev if ev is not None else empty_events()).filter(pl.col("date") == d).unique("event_id", keep="first")
        men = men.unique() if men is not None else pl.DataFrame(schema={"event_id": pl.Int64, "url_hash": pl.Int64})
        ev, n_ev = link_events(ev, men, arts, maps.sector_of)
        arts = arts.join(n_ev, on="url_hash", how="left").with_columns(pl.col("n_events").fill_null(0).cast(pl.Int16))
        ev_keep = ev.filter(keep_event_expr())
        dom = maps.ids("domain", arts.get_column("domain").drop_nulls().unique().to_list()
                       + ev_keep.get_column("domain").drop_nulls().unique().to_list())
        lang = maps.ids("lang", arts.get_column("lang").drop_nulls().unique().to_list())
        src = maps.ids("source", ["gdelt_gkg"])
        arts = arts.with_columns(
            pl.col("domain").replace_strict(dom, default=None, return_dtype=pl.Int32).alias("domain_id"),
            pl.col("lang").replace_strict(lang, default=None, return_dtype=pl.Int8).alias("lang_id"))
        agg = day_aggregates(d, ev, arts)
        if agg.height:
            outs["agg"] = agg.drop("attention_z", "tone_z", strict=False).with_columns(
                pl.lit(h, pl.Int8).alias("hour"), pl.lit(g["slots"], pl.Int8).alias("slots"),
                pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at"))
        if ev_keep.height:
            outs["events"] = ev_keep.with_columns(
                event_salience_expr().alias("salience"),
                pl.col("domain").replace_strict(dom, default=None, return_dtype=pl.Int32).alias("domain_id"))
        members.append(gdelt_members(arts, src["gdelt_gkg"]).with_columns(pl.lit(None, pl.Utf8).alias("item_key")))
        outs["articles"] = arts
    items = _read_items(d, h, settings)
    if items is not None:
        nm = news_members(items, maps)
        if nm.height:
            members.append(nm)
        # XLM-R for non-English items without a live (FinBERT) score
        if xlmr is not None and xlmr.enabled and "language" in items.columns:
            need = items.filter((pl.col("language") != "en") & pl.col("sentiment_score").is_null()).head(max_xlmr)
            if need.height:
                sc = xlmr.score(need.get_column("title").to_list())
                xs = pl.DataFrame({"item_key": need.get_column("item_key"),
                                   "__xs": pl.Series(sc, dtype=pl.Float64)})
                items = items.join(xs, on="item_key", how="left").with_columns(
                    pl.coalesce("sentiment_score", "__xs").alias("sentiment_score"),
                    pl.when(pl.col("sentiment_model").is_null() & pl.col("__xs").is_not_null())
                    .then(pl.lit("cardiffnlp/twitter-xlm-roberta-base-sentiment")).otherwise(pl.col("sentiment_model"))
                    .alias("sentiment_model")).drop("__xs")
                outs["item_sent"] = xs.filter(pl.col("__xs").is_not_null()).rename({"__xs": "sentiment_score"})
                rep["xlmr_scored"] = int(outs["item_sent"].height)
    mem = _concat(members)
    if mem is not None and mem.height:
        mem = mem.filter(pl.col("title").is_not_null()).unique("url_hash", keep="first", maintain_order=True)
        assign = _assignments(d, h, settings)
        sid = [assign.get(int(u)) for u in mem.get_column("url_hash").to_list()]
        mem = mem.with_columns(pl.Series("story_id", sid, pl.Int64))
        un = mem.filter(pl.col("story_id").is_null())
        rep["assigned_live"] = mem.height - un.height
        if un.height:
            # neighbours: finalised stories of +-3 days and the stories of this day's EARLIER folded hours (never the
            # day's partial file: a refold must not see its own previous output -> deterministic, idempotent)
            nb = read_range("news_stories", d - timedelta(days=WINDOW_DAYS), d + timedelta(days=WINDOW_DAYS),
                            settings, columns=["date", "story_id", "title", "n_articles"])
            prev = earlier_stories(d, h, settings)
            if prev is not None and prev.height:
                nb = pl.concat([nb, prev], how="vertical_relaxed")
            _st, smem = build_day_stories(d, un.drop("story_id"), nb, maps.names, min_neighbour_articles=1)
            if smem.height:
                mp = dict(zip(smem.get_column("url_hash").to_list(), smem.get_column("story_id").to_list(), strict=True))
                mem = mem.with_columns(pl.Series("story_id", [s if s is not None else mp.get(int(u)) for s, u in zip(
                    mem.get_column("story_id").to_list(), mem.get_column("url_hash").to_list(), strict=True)],
                    pl.Int64))
        outs["members"] = mem.filter(pl.col("story_id").is_not_null()).with_columns(pl.lit(d).alias("date"),
                                                                                     pl.lit(h, pl.Int8).alias("hour"))
    sent = _sent_rows(d, h, items, outs.get("articles"), maps, now)
    if sent.height:
        outs["sent"] = sent
    if items is not None:
        outs["items_sent"] = items.select([c for c in ("item_key", "url_hash", "sentiment_score", "sentiment_model",
                                                       "tier", "publisher", "source_id") if c in items.columns])
    # idempotent rewrite of the hour's fold outputs
    fd.mkdir(parents=True, exist_ok=True)
    for f in fd.glob("*.parquet"):
        if f.stem not in outs:
            f.unlink(missing_ok=True)
    for k, df in outs.items():
        hot.write_parquet_atomic(df, fd / f"{k}.parquet")
    rep.update({k: v.height for k, v in outs.items()}, slots=g["slots"],
               seconds=round(time.perf_counter() - t0, 2))
    hot.write_json_atomic(rep, fd / "fold.json")
    return rep


# ================================================================================================ day rebuild
def _fold_frames(d: date, name: str, settings: Settings) -> pl.DataFrame | None:
    root = hot.day_dir(hot.news_hot_dir(settings), d)
    frames = [hot.read_parquet(p) for p in sorted(root.glob(f"fold_h*/{name}.parquet"))] if root.exists() else []
    return _concat([f for f in frames if f is not None])


def story_sentiment(d: date, settings: Settings) -> pl.DataFrame | None:
    """url_hash -> live item sentiment of the day (FinBERT live + XLM-R from the folds)."""
    s = _fold_frames(d, "items_sent", settings)
    if s is None or "url_hash" not in s.columns:
        return None
    x = _fold_frames(d, "item_sent", settings)
    if x is not None:
        s = s.join(x.rename({"sentiment_score": "__x"}), on="item_key", how="left").with_columns(
            pl.coalesce("sentiment_score", "__x").alias("sentiment_score")).drop("__x")
    return s.filter(pl.col("sentiment_score").is_not_null()).select("url_hash", "sentiment_score")


def rebuild_day(d: date, settings: Settings, maps: Maps, now: datetime | None = None) -> dict[str, Any]:
    """Day lake files from all folded hours: p- partial files of the four v2 tables, d- files of the hourly tables."""
    from app.core.schema import get_contract  # noqa: PLC0415
    from app.events_v2 import store  # noqa: PLC0415
    from app.events_v2.stories import MEMBER_SCHEMA, build_day_stories  # noqa: PLC0415

    now = now or datetime.now(UTC)
    rep: dict[str, Any] = {"day": d.isoformat()}
    mem = _fold_frames(d, "members", settings)
    sizes = None
    if mem is not None and mem.height:
        mem = mem.unique("url_hash", keep="first", maintain_order=True)
        st, smem = build_day_stories(d, mem.select([*MEMBER_SCHEMA, "story_id"]), names=maps.names,
                                     fixed_col="story_id")
        sent = story_sentiment(d, settings)
        if sent is not None and st.height:
            ss = smem.select("url_hash", "story_id").join(sent, on="url_hash", how="inner") \
                .group_by("story_id").agg(pl.col("sentiment_score").mean().alias("__s"))
            st = st.join(ss, on="story_id", how="left").with_columns(
                pl.coalesce(pl.col("sentiment_x100").cast(pl.Float64), (pl.col("__s") * 100).round())
                .clip(-100, 100).cast(pl.Int8).alias("sentiment_x100"),
                pl.when(pl.col("__s").is_not_null()).then(pl.lit("live")).otherwise(pl.col("sentiment_model"))
                .alias("sentiment_model")).drop("__s")
        store.write_partial("news_stories", d, st, settings)
        store.write_partial("story_members", d, smem, settings)
        rep["news_stories"], rep["story_members"] = st.height, smem.height
        sizes = st.select("story_id", pl.col("n_articles").alias("__sz"))
    arts = _fold_frames(d, "articles", settings)
    if arts is not None and arts.height:
        arts = arts.unique("url_hash", keep="first", maintain_order=True)
        if mem is not None:
            arts = arts.drop("story_id", strict=False).join(mem.select("url_hash", "story_id"), on="url_hash",
                                                            how="left")
        else:
            arts = arts.with_columns(pl.lit(None, pl.Int64).alias("story_id"))
        if sizes is not None:
            arts = arts.join(sizes, on="story_id", how="left")
        else:
            arts = arts.with_columns(pl.lit(None, pl.Int32).alias("__sz"))
        arts = arts.with_columns(((pl.col("__sz").fill_null(1).cast(pl.Float64).log1p()
                                   + pl.col("n_events").cast(pl.Float64).log1p()
                                   + pl.col("tone_x100").cast(pl.Float64).abs().fill_null(0) / 500
                                   + pl.when(pl.col("issuer_ids").list.len() > 0).then(2.0).otherwise(0.0)) * 100)
                                 .round().clip(0, 32767).cast(pl.Int16).alias("salience"))
        cols = [c for c in get_contract("gdelt_article_labels").column_names if c in arts.columns]
        store.write_partial("gdelt_article_labels", d, arts.select(cols), settings)
        rep["gdelt_article_labels"] = arts.height
    ev = _fold_frames(d, "events", settings)
    if ev is not None and ev.height:
        ev = ev.unique("event_id", keep="last", maintain_order=True)
        cols = [c for c in get_contract("gdelt_event_labels").column_names if c in ev.columns]
        store.write_partial("gdelt_event_labels", d, ev.select(cols), settings)
        rep["gdelt_event_labels"] = ev.height
    agg = _fold_frames(d, "agg", settings)
    if agg is not None and agg.height:
        agg = agg.unique(["hour", "label_type", "label_id", "theme"], keep="last", maintain_order=True)
        store.write_day("event_hourly_agg", d, agg.with_columns(pl.lit(d).alias("date")), settings)
        rep["event_hourly_agg"] = agg.height
    sent = _fold_frames(d, "sent", settings)
    if sent is not None and sent.height:
        sent = sent.unique(["hour", "label_type", "label_id"], keep="last", maintain_order=True)
        store.write_day("sentiment_hourly", d, sent, settings)
        rep["sentiment_hourly"] = sent.height
    return rep


# ================================================================================================ hourly driver
def closed_hours(now: datetime, lookback_h: int = LOOKBACK_H) -> list[datetime]:
    end = hot.hour_floor(now - FOLD_GRACE) - timedelta(hours=1)
    return [end - timedelta(hours=i) for i in range(lookback_h - 1, -1, -1)]


def run_hourly(settings: Settings | None = None, now: datetime | None = None, max_hours: int = 30,
               xlmr: bool = True, max_xlmr: int = 1500, stop: Any = None) -> dict[str, Any]:
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    st = read_state(s)
    folded: dict[str, dict[str, str]] = st.get("folded") or {}
    finalised = set((st.get("finalised") or {}).keys())
    todo: list[tuple[date, int, str]] = []
    for hh in closed_hours(now):
        d, h = hh.date(), hh.hour
        if d.isoformat() in finalised:
            continue
        fp = fingerprint(d, h, s)
        if not fp:
            continue
        if (folded.get(d.isoformat()) or {}).get(str(h)) == fp:
            continue
        todo.append((d, h, fp))
    # a changed hour invalidates the story clustering of the day's later hours: refold them too (in order)
    first_changed: dict[date, int] = {}
    for d, h, _ in todo:
        first_changed[d] = min(first_changed.get(d, 24), h)
    have = {(d, h) for d, h, _ in todo}
    for d, h0 in first_changed.items():
        for hs in sorted(int(x) for x in (folded.get(d.isoformat()) or {})):
            if hs > h0 and (d, hs) not in have:
                fp = fingerprint(d, hs, s)
                if fp:
                    todo.append((d, hs, fp))
    todo = sorted(todo, key=lambda x: (x[0], x[1]))[-max_hours:]
    rep: dict[str, Any] = {"hours": [], "days": {}, "started_at": now.isoformat(timespec="seconds")}
    if not todo:
        rep["message"] = "nothing new to fold"
        return rep
    maps = Maps(s)
    scorer = XlmrScorer(s, enabled=xlmr)
    t0 = time.perf_counter()
    done: list[tuple[date, int, str]] = []
    for d, h, fp in todo:
        if stop is not None and stop():
            break
        try:
            r = fold_hour(d, h, s, maps, scorer, now, max_xlmr)
        except Exception as e:  # noqa: BLE001 - one bad hour must not block the others
            log.exception("news fold %s %02d failed", d, h)
            rep["hours"].append({"day": d.isoformat(), "hour": h, "error": f"{type(e).__name__}: {e}"[:300]})
            continue
        rep["hours"].append(r)
        done.append((d, h, fp))
    for d in sorted({d for d, _, _ in done}):
        try:
            rep["days"][d.isoformat()] = rebuild_day(d, s, maps, now)
        except Exception as e:  # noqa: BLE001
            log.exception("news day rebuild %s failed", d)
            rep["days"][d.isoformat()] = {"error": f"{type(e).__name__}: {e}"[:300]}

    def upd(stt: dict[str, Any]) -> None:
        f = stt.setdefault("folded", {})
        for d, h, fp in done:
            f.setdefault(d.isoformat(), {})[str(h)] = fp
        cut = (now - timedelta(days=10)).date().isoformat()
        for k in [k for k in f if k < cut]:
            f.pop(k, None)
        stt["last_hourly"] = {"at": now.isoformat(timespec="seconds"), "hours": len(done),
                              "seconds": round(time.perf_counter() - t0, 1), "xlmr_scored": scorer.n,
                              "xlmr_error": scorer.error}

    update_state(upd, s)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    rep["xlmr_scored"] = scorer.n
    return rep


# ================================================================================================ daily finalise
def _has_hot(d: date, settings: Settings) -> tuple[int, int]:
    """(GDELT hot files, news hot hours) of day d."""
    slots = hot.day_slots(d, settings)
    files = sum(1 for man in slots.values() for k in hot.GDELT_KINDS if (man.get(k) or {}).get("status") in ("ok", "missing"))
    nd = hot.day_dir(hot.news_hot_dir(settings), d)
    hours = len(list(nd.glob("items_h*.parquet"))) if nd.exists() else 0
    return files, hours


def merge_live_news(d: date, settings: Settings, maps: Maps) -> dict[str, int]:
    """Attach the day's live RSS / Polygon members to the (re-clustered) d- file stories (anti-join: idempotent)."""
    from app.events_v2 import store  # noqa: PLC0415
    from app.events_v2.stories import MEMBER_SCHEMA, STORY_SCHEMA, WINDOW_DAYS, build_day_stories, merge_story_rows  # noqa: PLC0415

    mem = _fold_frames(d, "members", settings)
    if mem is None or not mem.height:
        return {"items": 0, "new_members": 0}
    gd = maps.ids("source", ["gdelt_gkg"])["gdelt_gkg"]
    mem = mem.filter(pl.col("source_id") != gd).unique("url_hash", keep="first")
    have = store.read_range("story_members", d, d, settings)
    if have.height:
        mem = mem.join(have.select("url_hash"), on="url_hash", how="anti")
    if mem.is_empty():
        return {"items": 0, "new_members": 0}
    nb = store.read_range("news_stories", d - timedelta(days=WINDOW_DAYS), d + timedelta(days=WINDOW_DAYS), settings,
                          columns=["date", "story_id", "title", "n_articles"])
    new_st, new_m = build_day_stories(d, mem.select(list(MEMBER_SCHEMA)), nb, maps.names, min_neighbour_articles=1)
    cur = store.read_range("news_stories", d, d, settings)
    stories = pl.concat([cur.select(list(STORY_SCHEMA)), new_st.select(list(STORY_SCHEMA))], how="vertical_relaxed") \
        if cur.height else new_st
    if stories.height and stories.get_column("story_id").n_unique() < stories.height:
        stories = merge_story_rows(stories)
    store.write_day("news_stories", d, stories, settings)
    members = pl.concat([have, new_m.select(have.columns)], how="vertical_relaxed") if have.height else new_m
    store.write_day("story_members", d, members, settings)
    return {"items": mem.height, "new_members": new_m.height}


def label_daily(d: date, settings: Settings, now: datetime | None = None) -> int:
    """sentiment_hourly (day d) -> sentiment_label_daily (+ sentiment_z vs the trailing 30 days)."""
    from app.events_v2 import store  # noqa: PLC0415

    now = now or datetime.now(UTC)
    h = store.read_range("sentiment_hourly", d, d, settings)
    if h.is_empty():
        return 0
    g = h.group_by("label_type", "label_id").agg(
        pl.col("n_items").sum().cast(pl.Int32), pl.col("n_scored").sum().cast(pl.Int32),
        ((pl.col("sentiment_mean").cast(pl.Float64) * pl.col("n_scored")).sum()
         / pl.col("n_scored").sum()).cast(pl.Float32).alias("sentiment_mean"),
        ((pl.col("pos_share").cast(pl.Float64) * pl.col("n_scored")).sum() / pl.col("n_scored").sum())
        .cast(pl.Float32).alias("pos_share"),
        ((pl.col("neg_share").cast(pl.Float64) * pl.col("n_scored")).sum() / pl.col("n_scored").sum())
        .cast(pl.Float32).alias("neg_share"),
        pl.col("tier1_n").sum().cast(pl.Int32), pl.col("art_n").sum().cast(pl.Int32),
        ((pl.col("art_tone").cast(pl.Float64) * pl.col("art_n")).sum() / pl.col("art_n").sum())
        .cast(pl.Float32).alias("art_tone"),
        (pl.col("n_items") > 0).sum().cast(pl.Int8).alias("hours"))
    hist = store.read_range("sentiment_label_daily", d - timedelta(days=30), d - timedelta(days=1), settings,
                            columns=["label_type", "label_id", "sentiment_mean"])
    if hist.height:
        z = hist.group_by("label_type", "label_id").agg(pl.col("sentiment_mean").cast(pl.Float64).mean().alias("__m"),
                                                         pl.col("sentiment_mean").cast(pl.Float64).std().alias("__s"),
                                                         pl.len().alias("__n"))
        g = g.join(z, on=["label_type", "label_id"], how="left").with_columns(
            pl.when((pl.col("__n") >= 7) & (pl.col("__s") > 1e-6))
            .then((pl.col("sentiment_mean") - pl.col("__m")) / pl.col("__s")).otherwise(None)
            .clip(-20, 20).cast(pl.Float32).alias("sentiment_z")).drop("__m", "__s", "__n")
    else:
        g = g.with_columns(pl.lit(None, pl.Float32).alias("sentiment_z"))
    g = g.with_columns(pl.lit(d).alias("date"), pl.lit(now).cast(pl.Datetime("us", "UTC")).alias("computed_at"))
    store.write_day("sentiment_label_daily", d, g, settings)
    return g.height


def finalise_day(d: date, settings: Settings, maps: Maps, now: datetime | None = None, assemble: bool = True,
                 sentiment: bool = True, stop: Any = None) -> dict[str, Any]:
    from app.events_v2 import store  # noqa: PLC0415
    from app.events_v2.pipeline import read_state as ev2_state  # noqa: PLC0415
    from app.events_v2.pipeline import recompute_z, run_days  # noqa: PLC0415

    now = now or datetime.now(UTC)
    t0 = time.perf_counter()
    rep: dict[str, Any] = {"day": d.isoformat()}
    files, hours = _has_hot(d, settings)
    rep["hot_files"], rep["hot_news_hours"] = files, hours
    # 1) last fold of the day
    scorer = XlmrScorer(settings, enabled=sentiment)
    for h in range(24):
        fp = fingerprint(d, h, settings)
        if fp:
            fold_hour(d, h, settings, maps, scorer, now)
    rep["rebuild"] = rebuild_day(d, settings, maps, now)
    # 2) full-day GDELT assembly (re-cluster) unless the nightly already did it
    rec = (ev2_state(settings).get("days") or {}).get(d.isoformat())
    if rec:
        rep["assembly"] = f"already checkpointed by {rec.get('label')}"
    elif assemble and files:
        from contextlib import nullcontext  # noqa: PLC0415

        from app.events_v2.pause import gdelt_priority  # noqa: PLC0415

        need_dl = files < 0.9 * 576
        with (gdelt_priority(settings, owner="news_finalise") if need_dl else nullcontext()):
            res = run_days([d], settings, legacy=True, label="live", workers=FINALISE_PARSE_WORKERS, dl_threads=4,
                           should_stop=stop, parse_threads=FINALISE_PARSE_THREADS,
                           max_inflight=FINALISE_MAX_INFLIGHT, max_tasks_per_child=60,
                           mem_budget_mb=FINALISE_MEM_BUDGET_MB or None)
        rep["need_download"] = int(max(0, 576 - files)) if need_dl else 0
        if res["reports"]:
            r0 = res["reports"][0]
            rep["assembly"] = {k: r0.get(k) for k in ("files", "files_ok", "files_missing", "hot_files", "errors",
                                                       "articles_kept", "events_kept", "stories", "assemble_s")}
            recompute_z(d, d, settings)
        else:
            rep["assembly"] = "not run (stopped or over the memory budget)"
            rep["error"] = rep["assembly"]
            return rep
    else:
        rep["assembly"] = "no GDELT hot data (left to the nightly gdelt_stream)"
    # 3) live news into the day's stories + story sentiment
    if (store.day_file("news_stories", d, settings)).exists():
        rep["merge"] = merge_live_news(d, settings, maps)
        for t in V2_PARTIAL_TABLES:
            store.delete_partial(t, d, settings)
        if sentiment:
            try:
                from app.events_v2.story_job import score_sentiment  # noqa: PLC0415

                rep["story_sentiment"] = score_sentiment([d], settings, max_stories=3000, max_minutes=8, threads=4)
            except Exception as e:  # noqa: BLE001
                rep["story_sentiment"] = {"error": f"{type(e).__name__}: {e}"[:200]}
    else:
        rep["merge"] = "no d- file yet: partial files kept until the day is assembled"
    # 4) roll-ups
    rep["sentiment_label_daily"] = label_daily(d, settings, now)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    return rep


def retention(settings: Settings, now: datetime, finalised: set[str]) -> dict[str, Any]:
    """GDELT hot slots: kept 2 days after the day was finalised (or checkpointed by the nightly), 10 days at most;
    news hot hours (items, assignments, fold outputs): 8 days (the intraday window is 7)."""
    import shutil  # noqa: PLC0415

    from app.events_v2.pipeline import read_state as ev2_state  # noqa: PLC0415

    today = now.date()
    ev2 = set((ev2_state(settings).get("days") or {}).keys())
    root = hot.gdelt_hot_dir(settings)
    removed_g = []
    for d in hot.hot_days(root):
        done = d.isoformat() in finalised or d.isoformat() in ev2
        if (done and d < today - timedelta(days=2)) or d < today - timedelta(days=10):
            shutil.rmtree(hot.day_dir(root, d), ignore_errors=True)
            removed_g.append(d.isoformat())
    removed_n = hot.prune(hot.news_hot_dir(settings), today - timedelta(days=8))
    return {"gdelt_hot_removed": removed_g, "news_hot_removed": removed_n}


# ---------------------------------------------------------------------------------------------- run guard + attempts
def run_marker_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).state_dir / "news_live_finalise.json"


def acquire_run_marker(settings: Settings, now: datetime | None = None) -> str | None:
    """Write our run marker. Returns a description of the LIVE run holding it (-> skip), None when we own it now
    (absent, ours, or stale: dead pid / older than FINALISE_STALE_S - a stale live process of ours is killed)."""
    from app.ops import winsys  # noqa: PLC0415

    now = now or datetime.now(UTC)
    p = run_marker_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=30):
        cur = hot.read_json(p) if p.exists() else {}
        pid = int(cur.get("pid") or 0)
        if pid and pid != os.getpid():
            try:
                started = datetime.fromisoformat(str(cur.get("started_at")))
            except ValueError:
                started = None
            age = (datetime.now(UTC) - started).total_seconds() if started else None
            alive = winsys.pid_running(pid)        # a zombie stuck in its exit does no work: stale
            if alive and age is not None and age < FINALISE_STALE_S:
                return f"pid {pid} since {cur.get('started_at')}"
            if alive:
                cmd = winsys.process_cmdline(pid) or ""
                if "multiprocessing" in cmd or "app." in cmd:   # our job process outlived its timeout: kill it
                    log.warning("news finalise: stale run pid %s (%.0f min old) still alive - killing its tree", pid,
                                (age or 0) / 60)
                    winsys.kill_tree(pid)
            else:
                log.warning("news finalise: stale run marker (pid %s dead, started %s) - taking over", pid,
                            cur.get("started_at"))
        hot.write_json_atomic({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                               "now": now.isoformat(timespec="seconds")}, p)
    return None


def release_run_marker(settings: Settings) -> None:
    p = run_marker_path(settings)
    try:
        with FileLock(str(p) + ".lock", timeout=30):
            cur = hot.read_json(p) if p.exists() else {}
            if int(cur.get("pid") or 0) == os.getpid():
                p.unlink()
    except Exception:  # noqa: BLE001 - a leftover marker is stale-detected next time
        pass


def note_attempt(settings: Settings, d: date, now: datetime, error: str | None = None) -> None:
    def upd(stt: dict[str, Any]) -> None:
        a = stt.setdefault("finalise_attempts", {}).setdefault(d.isoformat(), {"n": 0})
        if error is None:
            a["n"] = int(a.get("n") or 0) + 1
            a["last_at"] = now.isoformat(timespec="seconds")
        else:
            a["last_error"] = error[:300]

    update_state(upd, settings)


def attempt_backoff_h(st: dict[str, Any], d: date, now: datetime) -> float:
    """Hours to wait before day d may be tried again (0 = due)."""
    a = (st.get("finalise_attempts") or {}).get(d.isoformat()) or {}
    if int(a.get("n") or 0) < FINALISE_MAX_ATTEMPTS or not a.get("last_at"):
        return 0.0
    try:
        last = datetime.fromisoformat(a["last_at"])
    except ValueError:
        return 0.0
    left = FINALISE_RETRY_AFTER_H - (now - last).total_seconds() / 3600
    return round(left, 2) if left > 0 else 0.0


def run_finalise(settings: Settings | None = None, now: datetime | None = None, days: list[date] | None = None,
                 assemble: bool = True, sentiment: bool = True, stop: Any = None, force: bool = False) -> dict[str, Any]:
    """Finalise complete UTC days (see the module doc). force=True ignores the HOLD switch and the per-day attempt
    back-off (manual re-runs); the run marker is always honoured."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    hold = finalise_hold_path(s)
    if hold.exists() and not force:   # operator switch - the finalise is deferred (no work, no blocking)
        log.warning("news finalise held by %s: skipping", hold)
        return {"days": {}, "held": True, "retention": None}
    guard = acquire_run_marker(s, now)
    if guard is not None:
        log.warning("news finalise already running (%s): skipping", guard)
        return {"days": {}, "already_running": guard, "retention": None}
    try:
        return _run_finalise(s, now, days, assemble, sentiment, stop, force)
    finally:
        release_run_marker(s)


def _run_finalise(s: Settings, now: datetime, days: list[date] | None, assemble: bool, sentiment: bool, stop: Any,
                  force: bool) -> dict[str, Any]:
    st = read_state(s)
    fin = st.get("finalised") or {}
    if days is None:
        days = []
        for i in range(1, 11):
            d = (now - timedelta(days=i)).date()
            if d.isoformat() in fin:
                continue
            if now < datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(days=1) + FINALISE_AFTER:
                continue
            files, hours = _has_hot(d, s)
            if files or hours:
                days.append(d)
        days = sorted(days)
    rep: dict[str, Any] = {"days": {}}
    if not force:
        due = []
        for d in days:
            wait_h = attempt_backoff_h(st, d, now)
            if wait_h:
                rep.setdefault("deferred", {})[d.isoformat()] = (
                    f"{(st.get('finalise_attempts') or {}).get(d.isoformat(), {}).get('n')} failed attempts: "
                    f"next try in {wait_h:.1f} h")
                log.warning("news finalise %s deferred: %s", d, rep["deferred"][d.isoformat()])
            else:
                due.append(d)
        days = due
    maps = Maps(s) if days else None
    for d in days:
        if stop is not None and stop():
            break
        note_attempt(s, d, now)          # counted at the start: a run killed by the timeout still counts
        try:
            r = finalise_day(d, s, maps, now, assemble=assemble, sentiment=sentiment, stop=stop)
        except Exception as e:  # noqa: BLE001
            log.exception("news finalise %s failed", d)
            rep["days"][d.isoformat()] = {"error": f"{type(e).__name__}: {e}"[:300]}
            note_attempt(s, d, now, error=rep["days"][d.isoformat()]["error"])
            continue
        rep["days"][d.isoformat()] = r
        if r.get("error"):
            note_attempt(s, d, now, error=str(r["error"]))
        if isinstance(r.get("merge"), dict) or r.get("assembly") == "no GDELT hot data (left to the nightly gdelt_stream)":
            def upd(stt: dict[str, Any], d=d, r=r) -> None:
                (stt.get("finalise_attempts") or {}).pop(d.isoformat(), None)
                stt.setdefault("finalised", {})[d.isoformat()] = {
                    "at": now.isoformat(timespec="seconds"), "assembly": r.get("assembly"), "merge": r.get("merge"),
                    "sentiment_label_daily": r.get("sentiment_label_daily"), "seconds": r.get("seconds")}
                f = stt["finalised"]
                for k in sorted(f)[:-60]:
                    f.pop(k, None)
            update_state(upd, s)
    rep["retention"] = retention(s, now, set((read_state(s).get("finalised") or {}).keys()))
    return rep


def _json(x: Any) -> str:
    return json.dumps(x, default=str)[:4000]


_ = (Counter, os)
