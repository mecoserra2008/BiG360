"""E2/E3 streaming pipeline: GDELT 15-minute files -> labelled compact tables, one UTC day at a time.

Per day: 96 slots x 6 streams (export, mentions, gkg + their translingual twins) are downloaded INTO MEMORY by a
thread pool (shared `gdelt_files` rate limit, 4 req/s by default) and parsed by a process pool (cores - 4 workers,
app.events_v2.parse). Raw files never touch the disk. When a day is complete it is assembled on a writer thread while
the next day downloads (bounded memory: at most one parsed day waiting):

  1. dictionaries: domain / lang ids (append-only);
  2. event <-> article links: SOURCEURL and Mentions url hashes -> article issuer labels / themes on events;
  3. T1 `event_daily_agg` from ALL parsed events + all labelled articles (before any T2 filtering);
  4. T2 `gdelt_event_labels` (labelled AND salient events) and `gdelt_article_labels`;
  5. T3 `news_stories` / `story_members` (MinHash-LSH clustering of the day's titles, linked to stories of +-3 days);
  6. checkpoint in data/state/events_v2.json (per-day stats, bytes per tier).
Memory / CPU bounds (incident 2026-10-24: a full-day re-assembly queued every downloaded zip in the main process -
main_peak_rss up to 10-25 GB in the state file - and each pool worker ran Polars with one thread per core, so two
workers next to the API / realtime / CC-NEWS spun 160+ threads on 20 cores): at most `max_inflight` files are
downloaded-but-not-parsed at any time (default 4 x workers), `parse_threads` caps POLARS / OMP / MKL threads of the
pool workers, `mem_budget_mb` (optional) stops the day when the process tree exceeds it (day not checkpointed).
Nightly mode (`legacy=True`) also writes the pre-v2 outputs (gdelt_events, country_daily files, GKG org mentions,
news_items) from the same bytes, which replaces the old gdelt_events / gdelt_gkg jobs.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, as_completed, wait
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from filelock import FileLock

from app.core.config import Settings, get_settings
from app.core.http import HttpClient, HttpError
from app.core.logsetup import get_logger
from app.events_v2 import dicts, store
from app.events_v2 import taxonomy as tx
from app.events_v2.context import build_context
from app.events_v2.parse import empty_articles, empty_events, init_worker, parse_file, peak_rss_mb
from app.events_v2.stories import MEMBER_SCHEMA, WINDOW_DAYS, build_day_stories
from app.ingestion.gdelt_events import BASE, parse_lastupdate, slot_times, slot_url

log = get_logger(__name__)

STREAMS = ("export", "mentions", "gkg", "translation.export", "translation.mentions", "translation.gkg")
N_THEMES = len(tx.THEMES)


# ================================================================================================ state
def state_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.state_dir / "events_v2.json"


def update_state(fn: Any, settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(p) + ".lock", timeout=60):
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        fn(st)
        st["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, p)
    return st


def read_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ================================================================================================ download + parse
class Streamer:
    """Download (threads, rate-limited, in memory) + parse (process pool) of GDELT days."""

    def __init__(self, settings: Settings | None = None, workers: int | None = None, dl_threads: int = 8,
                 legacy: bool = False, ctx_refresh: bool = True, parse_threads: int | None = None,
                 max_inflight: int | None = None, max_tasks_per_child: int | None = None,
                 mem_budget_mb: float | None = None):
        self.s = settings or get_settings()
        self.workers = workers or max(2, (os.cpu_count() or 8) - 4)
        self.dl_threads = dl_threads
        #: files downloaded but not yet parsed (zip bytes held in this process); 0 = unbounded (pre-incident)
        self.max_inflight = max_inflight if max_inflight is not None else 4 * self.workers
        self.parse_threads = parse_threads
        self.max_tasks_per_child = max_tasks_per_child
        self.mem_budget_mb = mem_budget_mb
        self.over_budget = False
        self._env_saved: dict[str, str | None] = {}
        self.legacy = legacy
        self.ctx, self.ctx_path = build_context(self.s, refresh=ctx_refresh)
        self.http = HttpClient("gdelt_files", settings=self.s, max_retries=4, timeout=120, cache=False)
        self.ppool: ProcessPoolExecutor | None = None
        self.dpool: ThreadPoolExecutor | None = None
        self.dl_lock = threading.Lock()
        #: optional callable -> bool; while True no new download starts (backfill yields to the nightly stream)
        self.pause_check: Any = None
        #: optional callable -> bool; when True remaining downloads are skipped (job cancelled / timed out)
        self.stop_check: Any = None

    def __enter__(self) -> Streamer:
        if self.parse_threads:   # inherited by the spawned pool workers (they start on the first submit)
            n = str(int(self.parse_threads))
            for k in ("POLARS_MAX_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
                self._env_saved[k] = os.environ.get(k)
                os.environ[k] = n
        kw: dict[str, Any] = {}
        if self.max_tasks_per_child:
            import multiprocessing as mp  # noqa: PLC0415

            kw = {"max_tasks_per_child": int(self.max_tasks_per_child), "mp_context": mp.get_context("spawn")}
        self.ppool = ProcessPoolExecutor(self.workers, initializer=init_worker, initargs=(str(self.ctx_path),), **kw)
        self.dpool = ThreadPoolExecutor(self.dl_threads, thread_name_prefix="gdelt-dl")
        return self

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.dpool:
                self.dpool.shutdown(wait=True, cancel_futures=True)
            if self.ppool:
                self.ppool.shutdown(wait=True, cancel_futures=True)
            self.http.close()
        finally:
            for k, v in self._env_saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            self._env_saved = {}

    def _tree_mb(self) -> float:
        try:
            from app.ops.winsys import tree_memory_mb  # noqa: PLC0415

            return tree_memory_mb(os.getpid())
        except Exception:  # noqa: BLE001
            return 0.0

    def budget_exceeded(self) -> bool:
        """True (sticky) once this process + its pool use more than mem_budget_mb."""
        if getattr(self, "over_budget", False):
            return True
        b = getattr(self, "mem_budget_mb", None)
        if b and self._tree_mb() > b:
            log.warning("events_v2: memory budget %.0f MB exceeded - stopping the day (not checkpointed)", b)
            self.over_budget = True
        return bool(getattr(self, "over_budget", False))

    def latest_slot(self) -> datetime | None:
        try:
            return parse_lastupdate(self.http.get(f"{BASE}/lastupdate.txt", cache_ttl=0).text)
        except Exception as e:  # noqa: BLE001
            log.warning("gdelt lastupdate failed: %s", e)
            return None

    def _fetch(self, url: str) -> tuple[bytes | None, float]:
        while self.pause_check is not None and self.pause_check():
            time.sleep(5)  # file-level yield: nothing is lost, the download simply starts later
        stop = getattr(self, "stop_check", None)
        if stop is not None and stop():
            return None, 0.0  # cancelled: skip remaining downloads; run_days discards this (incomplete) day
        t0 = time.perf_counter()
        try:
            r = self.http.get(url, cache_ttl=0)
            return r.content, time.perf_counter() - t0
        except HttpError as e:
            if e.status in (404, 403):
                return None, time.perf_counter() - t0
            raise

    def _from_hot(self, ts: datetime, kind: str, out: dict[str, Any]) -> bool:
        """roadmap v3 N2.7: a 15-min file the live news service already parsed (data/realtime/gdelt_hot) is taken
        from the hot store instead of being downloaded again. Returns True when the file is covered."""
        if not getattr(self, "use_hot", True):
            return False
        try:
            from app.news_live.hot import load_slot_kind  # noqa: PLC0415

            got = load_slot_kind(ts, kind, self.legacy, self.s)
        except Exception as e:  # noqa: BLE001 - a damaged hot file just means "download it"
            log.debug("gdelt hot store read failed %s %s: %s", ts, kind, e)
            return False
        if got is None:
            return False
        status, res, old = got
        if status == "missing":   # the live path saw a 403 / 404: ask GDELT again (late files, outage redo)
            return False
        out["stats"]["hot_hits"] += 1
        out["files_ok"] += 1
        base = kind.split(".")[-1]
        if base == "gkg":
            out["articles"].append(res)
            if old is not None and old.height:
                out["legacy_gkg"].append(old)
        elif base == "export":
            out["events"].append(res)
            if old is not None:
                out["legacy_ev"].append(old)
        else:
            out["mentions"].append(res)
        return True

    def fetch_day(self, d: date, latest: datetime | None = None) -> dict[str, Any]:
        assert self.ppool and self.dpool
        t0 = time.perf_counter()
        jobs = [(ts, k, slot_url(ts, k)) for ts in slot_times(d) if latest is None or ts <= latest for k in STREAMS]
        out: dict[str, Any] = {"day": d, "files": len(jobs), "files_ok": 0, "files_missing": 0, "bytes_zip": 0,
                               "dl_thread_s": 0.0, "events": [], "mentions": [], "articles": [], "legacy_ev": [],
                               "legacy_gkg": [], "stats": Counter(), "errors": 0}
        todo = [(k, u) for ts, k, u in jobs if not self._from_hot(ts, k, out)]
        # incident 2026-10-24: bounded window - never more than `max_inflight` zips held in this process
        window = int(getattr(self, "max_inflight", 0) or 0) or max(1, len(todo))
        budget = getattr(self, "mem_budget_mb", None)
        it = iter(todo)
        dl: dict[Future, tuple[str, str]] = {}
        pf: set[Future] = set()
        t_dl: float | None = None
        peak = 0.0
        exhausted = False
        n_done = 0

        def top_up() -> None:
            nonlocal exhausted
            while not exhausted and len(dl) + len(pf) < window:
                if budget and n_done and self.budget_exceeded():
                    exhausted = True
                    return
                try:
                    k, u = next(it)
                except StopIteration:
                    exhausted = True
                    return
                dl[self.dpool.submit(self._fetch, u)] = (k, u)

        top_up()
        while dl or pf:
            done, _ = wait(list(dl) + list(pf), return_when=FIRST_COMPLETED)
            for f in done:
                if f in dl:
                    k, u = dl.pop(f)
                    try:
                        data, secs = f.result()
                    except Exception as e:  # noqa: BLE001
                        log.warning("gdelt download failed %s: %s", u.rsplit("/", 1)[-1], e)
                        out["errors"] += 1
                        continue
                    out["dl_thread_s"] += secs
                    if data is None:
                        out["files_missing"] += 1
                        continue
                    out["bytes_zip"] += len(data)
                    pf.add(self.ppool.submit(parse_file, k, data, self.legacy))
                    del data
                else:
                    pf.discard(f)
                    n_done += 1
                    peak = max(peak, self._take_parsed(f, out))
            top_up()
            if exhausted and not dl and t_dl is None:
                t_dl = time.perf_counter() - t0
        if getattr(self, "over_budget", False):
            out["over_budget"] = True
        out["wall_s"] = time.perf_counter() - t0
        out["download_wall_s"] = t_dl if t_dl is not None else out["wall_s"]
        out["worker_peak_rss_mb"] = peak
        return out

    @staticmethod
    def _take_parsed(f: Future, out: dict[str, Any]) -> float:
        try:
            kind, res, st, old = f.result()
        except Exception as e:  # noqa: BLE001
            log.warning("gdelt parse failed: %s", e)
            out["errors"] += 1
            return 0.0
        out["files_ok"] += 1
        base = kind.split(".")[-1]
        out["stats"][f"{base}_raw_bytes"] += st.get("raw_bytes", 0)
        out["stats"][f"{base}_rows"] += st.get("rows", 0)
        out["stats"]["cpu_s"] += st.get("cpu_s", 0.0)
        if base == "gkg":
            out["articles"].append(res)
            for x in ("prefiltered", "kept", "linked"):
                out["stats"][f"gkg_{x}"] += st.get(x, 0)
            if old is not None and old.height:
                out["legacy_gkg"].append(old)
        elif base == "export":
            out["events"].append(res)
            if old is not None:
                out["legacy_ev"].append(old)
        else:
            out["mentions"].append(res)
        return float(st.get("peak_rss_mb", 0.0) or 0.0)


# ================================================================================================ assembly helpers
def _bits_list(mask_col: str, n: int, with_all: bool = True) -> pl.Expr:
    parts = [pl.when((pl.col(mask_col).cast(pl.Int64) & (1 << b)) != 0).then(pl.lit(b, pl.Int8)) for b in range(n)]
    if with_all:
        parts = [pl.lit(-1, pl.Int8), *parts]
    return pl.concat_list(parts).list.drop_nulls()


def _union_conf(df: pl.DataFrame, key: str) -> pl.DataFrame:
    """[key, iid, conf] -> [key, issuer_ids, issuer_conf] (max conf per issuer, best first, <= 10)."""
    if df.is_empty():
        return pl.DataFrame(schema={key: pl.Int64, "issuer_ids": pl.List(pl.Int32), "issuer_conf": pl.List(pl.Int8)})
    g = df.group_by(key, "iid").agg(pl.col("conf").max()).sort([key, "conf"], descending=[False, True])
    return g.group_by(key, maintain_order=True).agg(pl.col("iid").head(10).cast(pl.Int32).alias("issuer_ids"),
                                                    pl.col("conf").head(10).cast(pl.Int8).alias("issuer_conf"))


def link_events(ev: pl.DataFrame, men: pl.DataFrame, arts: pl.DataFrame,
                sector_of: dict[int, int]) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Propagate article labels to events (SOURCEURL article: issuers + themes + sectors; Mentions articles: issuers).
    Returns (events with issuer_ids / issuer_conf / theme_mask / sector_mask, per-article n_events)."""
    a = arts.select("url_hash", "issuer_ids", "issuer_conf", pl.col("theme_mask").alias("__atm"),
                    pl.col("sector_mask").alias("__asm"))
    al = a.filter(pl.col("issuer_ids").list.len() > 0).explode(["issuer_ids", "issuer_conf"]).rename(
        {"issuer_ids": "iid", "issuer_conf": "conf"})
    src = ev.select("event_id", "url_hash").join(al, on="url_hash", how="inner").select("event_id", "iid", "conf")
    via_m = men.join(al, on="url_hash", how="inner").select("event_id", "iid", "conf") if men.height else src.head(0)
    links = _union_conf(pl.concat([src, via_m], how="vertical_relaxed"), "event_id")
    ev = ev.with_columns(pl.col("theme_mask").alias("cameo_mask"))
    ev = ev.join(a.select("url_hash", "__atm", "__asm"), on="url_hash", how="left").join(links, on="event_id", how="left")
    ism = []
    for ids in ev.get_column("issuer_ids").to_list():
        m = 0
        for i in ids or []:
            m |= sector_of.get(i, 0)
        ism.append(m)
    ev = ev.with_columns(
        (pl.col("theme_mask") | pl.col("__atm").fill_null(0)).alias("theme_mask"),
        (pl.lit(pl.Series("__ism", ism, pl.Int16)) | pl.col("__asm").fill_null(0)).cast(pl.Int16).alias("sector_mask"),
        pl.col("issuer_ids").fill_null(pl.lit([], pl.List(pl.Int32))),
        pl.col("issuer_conf").fill_null(pl.lit([], pl.List(pl.Int8)))).drop("__atm", "__asm")
    n_ev = pl.concat([ev.select("url_hash", "event_id"), men.select("url_hash", "event_id")] if men.height
                     else [ev.select("url_hash", "event_id")], how="vertical_relaxed").unique() \
        .group_by("url_hash").agg(pl.len().clip(0, 32767).cast(pl.Int16).alias("n_events"))
    return ev, n_ev


def event_salience_expr() -> pl.Expr:
    s = (pl.col("n_mentions").cast(pl.Float64).log1p() + pl.col("n_sources").cast(pl.Float64).log1p()
         + pl.col("n_articles").cast(pl.Float64).log1p() + pl.col("goldstein_x10").cast(pl.Float64).abs().fill_null(0) / 50
         + pl.when(pl.col("issuer_ids").list.len() > 0).then(2.0).otherwise(0.0))
    return (s * 100).round().clip(0, 32767).cast(pl.Int16)


def keep_event_expr() -> pl.Expr:
    """T2 keep rule: issuer-linked, or a market-relevant CAMEO event type that is salient (>= 3 articles or
    |Goldstein| >= 7). Article themes label kept events but do not qualify an event on their own (they would keep
    ~55% of all events; this rule keeps ~22%)."""
    salient = (pl.col("n_articles") >= 3) | (pl.col("goldstein_x10").abs() >= 70)
    return (pl.col("issuer_ids").list.len() > 0) | ((pl.col("cameo_mask") != 0) & salient)


def day_aggregates(d: date, ev: pl.DataFrame, arts: pl.DataFrame) -> pl.DataFrame:
    """T1 rows for one day: country x theme, sector x theme, issuer x all-themes (theme -1 = all)."""
    ev = ev.with_columns(_bits_list("theme_mask", N_THEMES).alias("__th"),
                         _bits_list("sector_mask", len(tx.SECTORS), with_all=False).alias("__sec"))
    ar = arts.with_columns(_bits_list("theme_mask", N_THEMES).alias("__th"),
                           _bits_list("sector_mask", len(tx.SECTORS), with_all=False).alias("__sec"))
    ev_m = [pl.len().cast(pl.Int32).alias("ev_n"), pl.col("n_mentions").cast(pl.Int64).sum().cast(pl.Int32).alias("ev_mentions"),
            pl.col("n_articles").cast(pl.Int64).sum().cast(pl.Int32).alias("ev_articles"),
            (pl.col("quad") >= 3).sum().cast(pl.Int32).alias("ev_conflict_n"),
            (pl.col("goldstein_x10").cast(pl.Float64).mean() / 10).cast(pl.Float32).alias("ev_goldstein"),
            (pl.col("tone_x100").cast(pl.Float64).mean() / 100).cast(pl.Float32).alias("ev_tone")]
    ar_m = [pl.len().cast(pl.Int32).alias("art_n"),
            (pl.col("tone_x100").cast(pl.Float64).mean() / 100).cast(pl.Float32).alias("art_tone"),
            (pl.col("tone_x100") < -200).mean().cast(pl.Float32).alias("art_neg_share")]
    parts = []
    for lt, key, src_ev, src_ar in (
            ("country", "countries", ev.explode("countries").explode("__th"), ar.explode("countries").explode("__th")),
            ("sector", "__sec", ev.explode("__sec").explode("__th"), ar.explode("__sec").explode("__th")),
            ("issuer", "issuer_ids", ev.explode("issuer_ids").with_columns(pl.lit(-1, pl.Int8).alias("__th")),
             ar.explode("issuer_ids").with_columns(pl.lit(-1, pl.Int8).alias("__th")))):
        e = src_ev.filter(pl.col(key).is_not_null()).group_by(key, "__th").agg(ev_m) if src_ev.height else None
        a = src_ar.filter(pl.col(key).is_not_null()).group_by(key, "__th").agg(ar_m) if src_ar.height else None
        if e is None and a is None:
            continue
        if e is None:
            j = a
        elif a is None:
            j = e
        else:
            j = e.join(a, on=[key, "__th"], how="full", coalesce=True)
        parts.append(j.with_columns(pl.lit(lt).alias("label_type"), pl.col(key).cast(pl.Int32).alias("label_id"),
                                    pl.col("__th").cast(pl.Int8).alias("theme")).drop(key, "__th"))
    if not parts:
        return pl.DataFrame()
    out = pl.concat(parts, how="diagonal_relaxed")
    for c in ("ev_n", "ev_mentions", "ev_articles", "ev_conflict_n", "art_n"):
        if c not in out.columns:
            out = out.with_columns(pl.lit(0, pl.Int32).alias(c))
        out = out.with_columns(pl.col(c).fill_null(0).cast(pl.Int32))
    return out.with_columns(pl.lit(d).alias("date"), pl.lit(None, pl.Float32).alias("attention_z"),
                            pl.lit(None, pl.Float32).alias("tone_z"))


# ================================================================================================ assemble + write
def assemble_day(fetched: dict[str, Any], streamer: Streamer, write: bool = True,
                 prune_before: date | None = None) -> dict[str, Any]:
    """prune_before: days older than this are written directly in their long-term form (retention rules applied
    in-flight, no story_members) - used by the deep backfill so old days are never stored and then deleted."""
    s = streamer.s
    d: date = fetched["day"]
    t0 = time.perf_counter()
    arts = pl.concat(fetched["articles"], how="vertical_relaxed") if fetched["articles"] else empty_articles()
    arts = arts.filter(pl.col("date") == d).sort("translated").unique("url_hash", keep="first")  # EN wins
    ev = pl.concat(fetched["events"], how="vertical_relaxed") if fetched["events"] else empty_events()
    ev = ev.filter(pl.col("date") == d).unique("event_id", keep="first")
    men = pl.concat(fetched["mentions"], how="vertical_relaxed").unique() if fetched["mentions"] else \
        pl.DataFrame(schema={"event_id": pl.Int64, "url_hash": pl.Int64})
    sector_of = dict(zip(streamer.ctx.issuers.get_column("iid").to_list(),
                         streamer.ctx.issuers.get_column("sector_mask").to_list(), strict=True))
    ev, n_ev = link_events(ev, men, arts, {k: int(v or 0) for k, v in sector_of.items()})
    arts = arts.join(n_ev, on="url_hash", how="left").with_columns(pl.col("n_events").fill_null(0).cast(pl.Int16))
    # ---- dictionaries
    dom = dicts.assign_ids("domain", arts.get_column("domain").drop_nulls().unique().to_list()
                           + ev.filter(keep_event_expr()).get_column("domain").drop_nulls().unique().to_list(), s)
    lang = dicts.assign_ids("lang", arts.get_column("lang").drop_nulls().unique().to_list(), s)
    src = dicts.assign_ids("source", ["gdelt_gkg"], s)
    arts = arts.with_columns(pl.col("domain").replace_strict(dom, default=None, return_dtype=pl.Int32).alias("domain_id"),
                             pl.col("lang").replace_strict(lang, default=None, return_dtype=pl.Int8).alias("lang_id"))
    # ---- T1
    agg = day_aggregates(d, ev, arts)
    # ---- T3 stories (GDELT titles; slug headline fallback)
    from app.nlp.gdelt_link import headline_from_url  # noqa: PLC0415

    titled = arts.with_columns(pl.coalesce(pl.col("title"), pl.col("url").map_elements(
        headline_from_url, return_dtype=pl.Utf8)).alias("title"))
    members = titled.select([pl.col(c) if c in titled.columns else pl.lit(None).cast(t).alias(c)
                             for c, t in MEMBER_SCHEMA.items() if c != "source_id"]
                            + [pl.lit(src["gdelt_gkg"], pl.Int8).alias("source_id")]).select(list(MEMBER_SCHEMA))
    nb = store.read_range("news_stories", d - timedelta(days=WINDOW_DAYS), d + timedelta(days=WINDOW_DAYS), s,
                          columns=["date", "story_id", "title", "n_articles"])
    nb = nb.filter(pl.col("date") != d)
    t_st = time.perf_counter()
    stories, smem = build_day_stories(d, members, nb, streamer.ctx.names)
    t_st = time.perf_counter() - t_st
    if smem.height:
        smap = smem.select("url_hash", "story_id")
        ssize = stories.select("story_id", pl.col("n_articles").alias("__sz"))
        arts = arts.join(smap, on="url_hash", how="left").join(ssize, on="story_id", how="left")
    else:
        arts = arts.with_columns(pl.lit(None, pl.Int64).alias("story_id"), pl.lit(None, pl.Int32).alias("__sz"))
    arts = arts.with_columns(((pl.col("__sz").fill_null(1).cast(pl.Float64).log1p()
                               + pl.col("n_events").cast(pl.Float64).log1p()
                               + pl.col("tone_x100").cast(pl.Float64).abs().fill_null(0) / 500
                               + pl.when(pl.col("issuer_ids").list.len() > 0).then(2.0).otherwise(0.0)) * 100)
                             .round().clip(0, 32767).cast(pl.Int16).alias("salience"))
    # ---- T2
    ev_keep = ev.filter(keep_event_expr()).with_columns(
        event_salience_expr().alias("salience"),
        pl.col("domain").replace_strict(dom, default=None, return_dtype=pl.Int32).alias("domain_id"))
    rep: dict[str, Any] = {
        "day": d.isoformat(), "files": fetched["files"], "files_ok": fetched["files_ok"],
        "files_missing": fetched["files_missing"], "errors": fetched["errors"],
        "hot_files": int(fetched["stats"].get("hot_hits", 0)),
        "gb_zip": round(fetched["bytes_zip"] / 1e9, 4),
        "gb_raw": round(sum(v for k, v in fetched["stats"].items() if k.endswith("raw_bytes")) / 1e9, 4),
        "events_parsed": ev.height, "events_kept": ev_keep.height,
        "gkg_rows": fetched["stats"].get("gkg_rows", 0), "gkg_prefiltered": fetched["stats"].get("gkg_prefiltered", 0),
        "articles_kept": arts.height, "articles_linked": int(arts.filter(pl.col("issuer_ids").list.len() > 0).height),
        "events_linked": int(ev_keep.filter(pl.col("issuer_ids").list.len() > 0).height),
        "mentions_rows": men.height, "agg_rows": agg.height, "stories": stories.height if stories.height else 0,
        "story_members": smem.height if smem.height else 0,
        "wall_s": round(fetched["wall_s"], 1), "download_wall_s": round(fetched["download_wall_s"], 1),
        "worker_cpu_s": round(fetched["stats"].get("cpu_s", 0.0), 1), "stories_s": round(t_st, 1),
        "worker_peak_rss_mb": fetched.get("worker_peak_rss_mb"), "main_peak_rss_mb": peak_rss_mb(),
    }
    if prune_before is not None and d < prune_before:
        from app.events_v2.retention import prune_day  # noqa: PLC0415

        rep["bytes_unpruned_rows"] = {"news_stories": stories.height, "gdelt_event_labels": ev_keep.height,
                                      "gdelt_article_labels": arts.height, "story_members": smem.height}
        stories, ev_keep, arts = prune_day(stories, ev_keep, arts)
        smem = smem.head(0)
        rep["pruned_in_flight"] = True
    if write:
        b: dict[str, int] = {}
        b["event_daily_agg"] = store.write_day("event_daily_agg", d, agg, s) if agg.height else 0
        b["gdelt_event_labels"] = store.write_day("gdelt_event_labels", d, ev_keep.select(
            [c for c in store.get_contract("gdelt_event_labels").column_names if c in ev_keep.columns]), s)
        b["gdelt_article_labels"] = store.write_day("gdelt_article_labels", d, arts.select(
            [c for c in store.get_contract("gdelt_article_labels").column_names if c in arts.columns]), s)
        b["news_stories"] = store.write_day("news_stories", d, stories, s) if stories.height else 0
        b["story_members"] = store.write_day("story_members", d, smem, s) if smem.height else 0
        rep["bytes"] = b
        if streamer.legacy:
            rep["legacy"] = write_legacy(fetched, s)
    rep["assemble_s"] = round(time.perf_counter() - t0, 1)
    return rep


def write_legacy(fetched: dict[str, Any], s: Settings) -> dict[str, Any]:
    """Pre-v2 outputs from the same parse (replaces the old gdelt_events / gdelt_gkg jobs in the nightly DAG)."""
    from app.core.storage import write_table  # noqa: PLC0415
    from app.ingestion.gdelt_events import combine_aggregates  # noqa: PLC0415
    from app.ingestion.gdelt_gkg import mentions_to_news, write_news  # noqa: PLC0415

    out: dict[str, Any] = {}
    d: date = fetched["day"]
    if fetched["legacy_ev"]:
        rows = pl.concat([r[0] for r in fetched["legacy_ev"]]).unique(subset=["global_event_id"], keep="last")
        agg = combine_aggregates([r[1] for r in fetched["legacy_ev"]])
        if rows.height:
            out["gdelt_events"] = write_table(rows.with_columns(pl.lit(datetime.now(UTC)).alias("ingested_at")),
                                              "gdelt_events", settings=s)
        for (dd,), part in agg.partition_by("date", as_dict=True).items() if agg.height else []:
            od = s.raw_dir / "gdelt_events" / "country_daily" / f"dt={dd.isoformat()}"
            od.mkdir(parents=True, exist_ok=True)
            for old in od.glob("*.parquet"):
                old.unlink()
            part.write_parquet(od / "part-0.parquet")
    if fetched["legacy_gkg"]:
        mentions = pl.concat(fetched["legacy_gkg"], how="vertical_relaxed")
        for (dd,), part in mentions.with_columns(pl.col("published_at").dt.date().alias("__d")) \
                .partition_by("__d", as_dict=True).items():
            od = s.raw_dir / "gdelt_gkg" / "org_mentions" / f"dt={dd.isoformat()}"
            od.mkdir(parents=True, exist_ok=True)
            for old in od.glob("*.parquet"):
                old.unlink()
            part.drop("__d").write_parquet(od / "part-0.parquet", compression="zstd")
        out["org_mention_rows"] = mentions.height
        out["news_items"] = write_news(mentions_to_news(mentions), s)
    out["day"] = d.isoformat()
    return out


# ================================================================================================ T1 z-scores
def recompute_z(start: date, end: date, settings: Settings | None = None, window: int = 90,
                min_obs: int = 10) -> int:
    """attention_z / tone_z for T1 rows in [start, end] vs each label's trailing `window` observations."""
    s = settings or get_settings()
    hist = store.read_range("event_daily_agg", start - timedelta(days=int(window * 1.5)), end, s)
    if hist.is_empty():
        return 0
    keys = ["label_type", "label_id", "theme"]
    h = hist.sort([*keys, "date"]).with_columns(
        (pl.col("ev_n") + pl.col("art_n")).cast(pl.Float64).log1p().alias("__x"),
        pl.coalesce(pl.col("art_tone"), pl.col("ev_tone")).cast(pl.Float64).alias("__t"))
    for col, out in (("__x", "attention_z"), ("__t", "tone_z")):
        mean = pl.col(col).shift(1).rolling_mean(window, min_samples=min_obs).over(keys)
        std = pl.col(col).shift(1).rolling_std(window, min_samples=min_obs).over(keys)
        h = h.with_columns(((pl.col(col) - mean) / pl.when(std > 1e-9).then(std).otherwise(None))
                           .clip(-50, 50).cast(pl.Float32).alias(out))
    h = h.drop("__x", "__t").filter((pl.col("date") >= start) & (pl.col("date") <= end))
    n = 0
    for (dd,), part in h.partition_by("date", as_dict=True).items():
        store.write_day("event_daily_agg", dd, part, s)
        n += part.height
    return n


# ================================================================================================ driver
def run_days(days: list[date], settings: Settings | None = None, legacy: bool = False, label: str = "nightly",
             workers: int | None = None, dl_threads: int = 8, write: bool = True, stop_file: Path | None = None,
             on_day: Any = None, should_pause: Any = None, streamer: Streamer | None = None,
             prune_before: date | None = None, should_stop: Any = None, parse_threads: int | None = None,
             max_inflight: int | None = None, max_tasks_per_child: int | None = None,
             mem_budget_mb: float | None = None) -> dict[str, Any]:
    """Process `days` in the given order with download/assembly pipelining. Checkpoints each day in the state.
    Pass an entered `streamer` to reuse its process pool / context across calls (backfill batches)."""
    s = settings or get_settings()
    reports: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    from contextlib import nullcontext  # noqa: PLC0415

    cm = nullcontext(streamer) if streamer is not None else Streamer(
        s, workers=workers, dl_threads=dl_threads, legacy=legacy, parse_threads=parse_threads,
        max_inflight=max_inflight, max_tasks_per_child=max_tasks_per_child, mem_budget_mb=mem_budget_mb)
    with cm as st:
        latest = st.latest_slot()
        writer = ThreadPoolExecutor(1, thread_name_prefix="ev2-writer")
        pending: Future | None = None

        def finish(fut: Future) -> None:
            rep = fut.result()
            reports.append(rep)
            if write:
                def upd(stt: dict[str, Any]) -> None:
                    runs = stt.setdefault("days", {})
                    runs[rep["day"]] = {k: v for k, v in rep.items() if k != "day"} | {"label": label}
                update_state(upd, s)
            if on_day:
                on_day(rep)

        try:
            for d in days:
                if stop_file is not None and stop_file.exists():
                    log.info("events_v2: stop file present, stopping before %s", d)
                    break
                if should_stop is not None and should_stop():
                    log.info("events_v2: cancellation requested, stopping before %s", d)
                    break
                while should_pause is not None and should_pause():
                    time.sleep(30)
                if latest is not None and datetime(d.year, d.month, d.day, tzinfo=UTC) > latest:
                    continue
                if should_stop is not None and streamer is None:
                    st.stop_check = should_stop
                fetched = st.fetch_day(d, latest)
                if should_stop is not None and should_stop():
                    log.info("events_v2: cancelled while fetching %s - day discarded (not checkpointed)", d)
                    break
                if fetched.get("over_budget"):
                    log.warning("events_v2: memory budget exceeded while fetching %s - day discarded "
                                "(not checkpointed)", d)
                    break
                if pending is not None:
                    finish(pending)
                pending = writer.submit(assemble_day, fetched, st, write, prune_before)
            if pending is not None:
                finish(pending)
        finally:
            writer.shutdown(wait=True)
    secs = time.perf_counter() - t0
    return {"days": [r["day"] for r in reports], "reports": reports, "seconds": round(secs, 1),
            "days_per_hour": round(len(reports) / secs * 3600, 2) if secs and reports else None}


def legacy_done(d: date, settings: Settings | None = None) -> bool:
    """True when gdelt_stream wrote the pre-v2 outputs (gdelt_events, org mentions, news_items) for UTC day d."""
    rec = read_state(settings).get("days", {}).get(d.isoformat()) or {}
    leg = rec.get("legacy") or {}
    return bool(leg) and rec.get("files_ok", 0) > 0


def default_days(settings: Settings | None = None) -> list[date]:
    """Nightly: the last complete UTC day (and the day before, to pick up late translingual slots)."""
    y = datetime.now(UTC).date() - timedelta(days=1)
    return [y]


def watermark_days(settings: Settings | None = None, end: date | None = None, max_days: int = 3,
                   lookback: int = 14) -> list[date]:
    """Days after the stream watermark (newest checkpointed day) up to `end` (default: last complete UTC day),
    oldest first, at most `max_days` per run (the rest is caught up by the next run; each day is checkpointed)."""
    end = end or default_days(settings)[-1]
    done = set(read_state(settings).get("days", {}))
    recent = [date.fromisoformat(x) for x in done if x <= end.isoformat()]
    wm = max(recent) if recent else None
    start = end - timedelta(days=lookback - 1)
    if wm is not None and wm >= start:
        start = wm + timedelta(days=1)
    todo = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    return [d for d in todo if d.isoformat() not in done][:max(1, max_days)]


def run(ctx):  # noqa: ANN001 - JobContext (job gdelt_stream)
    from app.events_v2.pause import gdelt_priority  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    if p.get("days"):
        days = [date.fromisoformat(x) for x in p["days"]]
    else:
        days = watermark_days(ctx.settings, max_days=int(p.get("max_days", 3)))
    if not days:
        return JobResult(status="success", rows_written={}, message="up to date (watermark >= last complete UTC day)")
    legacy = str(p.get("legacy", "true")).lower() not in ("0", "false", "no")
    cancel = getattr(ctx, "cancel_event", None)
    log.info("gdelt_stream: days %s (backfill paused while running)", [d.isoformat() for d in days])
    with gdelt_priority(ctx.settings):  # the detached deep backfill yields GDELT bandwidth until we finish
        res = run_days(days, ctx.settings, legacy=legacy, label="nightly",
                       workers=int(p["workers"]) if p.get("workers") else None,
                       should_stop=(cancel.is_set if cancel is not None else None))
        if res["reports"]:
            got = [date.fromisoformat(r["day"]) if isinstance(r["day"], str) else r["day"] for r in res["reports"]]
            recompute_z(min(got), max(got), ctx.settings)
    reps = res["reports"]
    ok = sum(r["files_ok"] for r in reps)
    miss = sum(r["files_missing"] + r["errors"] for r in reps)
    status = "success" if ok and not miss else ("partial" if ok else "failed")
    rows = {"gdelt_event_labels": sum(r["events_kept"] for r in reps),
            "gdelt_article_labels": sum(r["articles_kept"] for r in reps),
            "news_stories": sum(r["stories"] for r in reps), "event_daily_agg": sum(r["agg_rows"] for r in reps)}
    for r in reps:
        for k, v in (r.get("legacy") or {}).items():
            if isinstance(v, int) and k in ("gdelt_events", "news_items"):
                rows[k] = rows.get(k, 0) + v
    msg = {k: res[k] for k in ("days", "seconds")} | {
        "events_kept": rows["gdelt_event_labels"], "articles_kept": rows["gdelt_article_labels"],
        "gb_zip": round(sum(r["gb_zip"] for r in reps), 3)}
    return JobResult(status=status, rows_written=rows, entities_ok=ok, entities_failed=miss,
                     http_requests=sum(r["files"] for r in reps), watermark=max(r["day"] for r in reps) if reps else None,
                     message=str(msg))


_ = math
