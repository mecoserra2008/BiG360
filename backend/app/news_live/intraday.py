"""Intraday serving DuckDB (roadmap v3 N3.3): job news_intraday, hourly after the fold.

The nightly serving snapshot (data/db/serving_<run>.duckdb, rebuilt by build_serving_db) is never touched. This
job writes a SMALL second database with the last `days` (7) of live news so the API serves fresh stories, hourly
aggregates and hourly sentiment without a snapshot rebuild:

    data/db/intraday_<YYYYmmddTHHMMSS>.duckdb   (built as .tmp, renamed when complete)
    data/db/INTRADAY                            pointer file (name of the current file; atomic replace)

Tables
* nt_items             live news items (news hot store items_hHH, all kinds) with the latest sentiment (live FinBERT
                       or XLM-R from the fold) and the lake story id (finalised days: the re-clustered id; today: the
                       live id = the partial-day id) - the News Terminal history beyond the bus' 72 h;
* nt_stories           stories of the window aggregated over their days (d- files, else p- partial files)
                       exactly like the snapshot's ev2_stories (same columns);
* nt_members           story_members of the window with domain codes (story timelines);
* nt_event_hourly      event_hourly_agg; nt_sentiment_hourly: sentiment_hourly;
* nt_dict / nt_issuer  events_v2 dictionaries (countries, sectors, themes, sources, languages, used domains) and
                       issuer id -> security / ticker / name;
* _meta                built_at, window, counts.
The API (app.api.intraday) re-opens the newest file read-only within seconds (hot swap; files older than the two
newest are deleted when no reader holds them).
"""

from __future__ import annotations

import os
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.news_live import hot

log = get_logger(__name__)

POINTER = "INTRADAY"
KEEP = 2


def pointer_path(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).db_dir / POINTER


def current_intraday(settings: Settings | None = None) -> Path | None:
    s = settings or get_settings()
    try:
        name = pointer_path(s).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    p = s.db_dir / name
    return p if name and p.exists() else None


def _day_files(table: str, days: list[date], settings: Settings) -> list[str]:
    """Per day: the d- file, else the p- partial file; months already compacted contribute their m- file."""
    from app.events_v2 import store  # noqa: PLC0415

    out: list[str] = []
    months: set[str] = set()
    for d in days:
        f = store.day_file(table, d, settings)
        pf = store.partial_file(table, d, settings)
        if f.exists():
            out.append(str(f))
        elif pf.exists():
            out.append(str(pf))
        months.add(d.strftime("%Y-%m"))
    for ym in months:
        mf = store.month_file(table, ym, settings)
        if mf.exists():
            out.append(str(mf))
    return out


def _items(days: list[date], settings: Settings) -> pl.DataFrame | None:
    frames = []
    for d in days:
        root = hot.day_dir(hot.news_hot_dir(settings), d)
        if not root.exists():
            continue
        xs = [hot.read_parquet(p) for p in sorted(root.glob("fold_h*/item_sent.parquet"))]
        xs = [x for x in xs if x is not None and x.height]
        x = pl.concat(xs, how="diagonal_relaxed").unique("item_key", keep="last") if xs else None
        for p in sorted(root.glob("items_h*.parquet")):
            df = hot.read_parquet(p)
            if df is None or not df.height:
                continue
            if x is not None:
                df = df.join(x.rename({"sentiment_score": "__x"}), on="item_key", how="left").with_columns(
                    pl.coalesce("sentiment_score", "__x").alias("sentiment_score"),
                    pl.when(pl.col("sentiment_model").is_null() & pl.col("__x").is_not_null())
                    .then(pl.lit("cardiffnlp/twitter-xlm-roberta-base-sentiment"))
                    .otherwise(pl.col("sentiment_model")).alias("sentiment_model")).drop("__x")
            frames.append(df)
    if not frames:
        return None
    df = pl.concat(frames, how="diagonal_relaxed").unique("item_key", keep="last", maintain_order=True)
    for c, t in (("published_at", pl.Datetime("us", "UTC")), ("first_seen_at", pl.Datetime("us", "UTC"))):
        if c in df.columns and df.schema[c] == pl.Utf8:
            df = df.with_columns(pl.col(c).str.to_datetime(time_zone="UTC", strict=False).alias(c))
    for c in ("issuer_ids", "security_ids", "tickers", "countries", "sectors", "themes"):
        if c not in df.columns:
            df = df.with_columns(pl.lit([], pl.List(pl.Utf8)).alias(c))
        elif df.schema[c] == pl.List(pl.Null):
            df = df.with_columns(pl.col(c).cast(pl.List(pl.Utf8)))
    for c in ("snippet", "story_title", "sentiment_label", "sentiment_model", "domain", "linked_by", "story_id"):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Utf8).alias(c))
    for c in ("tone", "sentiment_score", "salience"):
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Float64).alias(c))
        else:
            df = df.with_columns(pl.col(c).cast(pl.Float64))
    return df


def build_intraday(settings: Settings | None = None, now: datetime | None = None, days: int = 7,
                   publish: bool = True) -> dict[str, Any]:
    from app.events_v2 import dicts  # noqa: PLC0415
    from app.events_v2.context import issuer_meta  # noqa: PLC0415

    s = settings or get_settings()
    now = now or datetime.now(UTC)
    t0 = time.perf_counter()
    window = [(now - timedelta(days=i)).date() for i in range(days - 1, -1, -1)]
    s.db_dir.mkdir(parents=True, exist_ok=True)
    name = f"intraday_{now:%Y%m%dT%H%M%S}.duckdb"
    final = s.db_dir / name
    tmp = final.with_suffix(".duckdb.tmp")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    rep: dict[str, Any] = {"file": name, "window": [window[0].isoformat(), window[-1].isoformat()]}
    try:
        con.execute("SET TimeZone = 'UTC'")
        lo = window[0].isoformat()

        def create_from_files(table: str, files: list[str], sql_tpl: str, empty_sql: str) -> int:
            if files:
                lst = "[" + ", ".join("'" + f.replace("'", "''") + "'" for f in files) + "]"
                con.execute(sql_tpl.format(src=f"read_parquet({lst}, union_by_name = true, hive_partitioning = false)",
                                           lo=lo))
            else:
                con.execute(empty_sql)
            return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

        rep["nt_stories"] = create_from_files("nt_stories", _day_files("news_stories", window, s), """
            CREATE TABLE nt_stories AS
            SELECT story_id, min(date) AS first_date, max(date) AS last_date, min(first_ts) AS first_ts,
                   max(last_ts) AS last_ts, arg_max(title, n_articles) AS title, arg_max(lang_id, n_articles) AS lang_id,
                   sum(n_articles)::INTEGER AS n_articles, max(n_sources)::INTEGER AS n_sources,
                   count(*)::INTEGER AS n_days, bit_or(source_mask) AS source_mask,
                   arg_max(domain_ids, n_articles) AS domain_ids, arg_max(urls, n_articles) AS urls,
                   sum(tone_x100 * n_articles) / nullif(sum(CASE WHEN tone_x100 IS NOT NULL THEN n_articles END), 0)
                       / 100.0 AS tone,
                   arg_max(sentiment_x100, n_articles) FILTER (WHERE sentiment_x100 IS NOT NULL) / 100.0 AS sentiment,
                   arg_max(sentiment_model, n_articles) FILTER (WHERE sentiment_model IS NOT NULL) AS sentiment_model,
                   bit_or(theme_mask) AS theme_mask, bit_or(sector_mask) AS sector_mask,
                   list_distinct(flatten(list(countries))) AS countries,
                   arg_max(issuer_ids, n_articles) AS issuer_ids, arg_max(issuer_rel, n_articles) AS issuer_rel,
                   sum(n_events)::INTEGER AS n_events, max(salience)::DOUBLE AS salience
            FROM {src} WHERE date >= DATE '{lo}' GROUP BY story_id""",
            """CREATE TABLE nt_stories (story_id BIGINT, first_date DATE, last_date DATE, first_ts TIMESTAMPTZ,
                   last_ts TIMESTAMPTZ, title VARCHAR, lang_id TINYINT, n_articles INTEGER, n_sources INTEGER,
                   n_days INTEGER, source_mask SMALLINT, domain_ids INTEGER[], urls VARCHAR[], tone DOUBLE,
                   sentiment DOUBLE, sentiment_model VARCHAR, theme_mask BIGINT, sector_mask SMALLINT,
                   countries SMALLINT[], issuer_ids INTEGER[], issuer_rel TINYINT[], n_events INTEGER, salience DOUBLE)""")
        rep["nt_members"] = create_from_files("nt_members", _day_files("story_members", window, s), """
            CREATE TABLE nt_members AS SELECT story_id, url_hash, date, minute, source_id, domain_id, tone_x100, lang_id
            FROM {src} WHERE date >= DATE '{lo}'""",
            """CREATE TABLE nt_members (story_id BIGINT, url_hash BIGINT, date DATE, minute SMALLINT, source_id TINYINT,
                   domain_id INTEGER, tone_x100 SMALLINT, lang_id TINYINT)""")
        rep["nt_event_hourly"] = create_from_files("nt_event_hourly", _day_files("event_hourly_agg", window, s), """
            CREATE TABLE nt_event_hourly AS SELECT * EXCLUDE (year_month) FROM (SELECT *, NULL AS year_month FROM {src})
            WHERE date >= DATE '{lo}'""",
            """CREATE TABLE nt_event_hourly (date DATE, hour TINYINT, label_type VARCHAR, label_id INTEGER, theme TINYINT,
                   ev_n INTEGER, ev_mentions INTEGER, ev_articles INTEGER, ev_conflict_n INTEGER, ev_goldstein FLOAT,
                   ev_tone FLOAT, art_n INTEGER, art_tone FLOAT, art_neg_share FLOAT, slots TINYINT,
                   computed_at TIMESTAMPTZ)""")
        rep["nt_sentiment_hourly"] = create_from_files("nt_sentiment_hourly", _day_files("sentiment_hourly", window, s), """
            CREATE TABLE nt_sentiment_hourly AS SELECT * EXCLUDE (year_month) FROM (SELECT *, NULL AS year_month FROM {src})
            WHERE date >= DATE '{lo}'""",
            """CREATE TABLE nt_sentiment_hourly (date DATE, hour TINYINT, label_type VARCHAR, label_id VARCHAR,
                   n_items INTEGER, n_scored INTEGER, sentiment_mean FLOAT, pos_share FLOAT, neg_share FLOAT,
                   n_sources SMALLINT, tier1_n INTEGER, art_n INTEGER, art_tone FLOAT, computed_at TIMESTAMPTZ)""")
        items = _items(window, s)
        if items is not None:
            con.register("_items", items.to_arrow())
            con.execute("""CREATE TABLE nt_items AS
                SELECT i.* REPLACE (coalesce(CAST(m.story_id AS VARCHAR), i.story_id) AS story_id)
                FROM _items i LEFT JOIN (SELECT url_hash, any_value(story_id) AS story_id FROM nt_members GROUP BY 1) m
                  ON m.url_hash = i.url_hash""")
            con.unregister("_items")
            con.execute("""CREATE OR REPLACE TABLE nt_items AS
                SELECT i.* REPLACE (coalesce(s.n_articles, i.story_size) AS story_size,
                                    coalesce(s.n_sources, i.story_sources) AS story_sources)
                FROM nt_items i LEFT JOIN nt_stories s ON CAST(s.story_id AS VARCHAR) = i.story_id
                ORDER BY coalesce(i.published_at, i.first_seen_at) DESC""")
        else:
            con.execute("""CREATE TABLE nt_items (item_key VARCHAR, url_hash BIGINT, kind VARCHAR, source_id VARCHAR,
                tier INTEGER, category VARCHAR, publisher VARCHAR, domain VARCHAR, title VARCHAR, snippet VARCHAR,
                url VARCHAR, language VARCHAR, published_at TIMESTAMPTZ, published_estimated BOOLEAN,
                first_seen_at TIMESTAMPTZ, issuer_ids VARCHAR[], linked_by VARCHAR, security_ids VARCHAR[],
                tickers VARCHAR[], countries VARCHAR[], sectors VARCHAR[], themes VARCHAR[], tone DOUBLE,
                sentiment_score DOUBLE, sentiment_label VARCHAR, sentiment_model VARCHAR, story_id VARCHAR,
                story_size INTEGER, story_sources INTEGER, story_first_seen VARCHAR, story_title VARCHAR,
                salience DOUBLE)""")
        rep["nt_items"] = int(con.execute("SELECT count(*) FROM nt_items").fetchone()[0])
        # dictionaries + issuers
        try:
            lab = dicts.load_dict("country", s)
            frames = [dicts.load_dict(t, s) for t in ("country", "sector", "theme", "source", "lang")]
            dom = dicts.load_dict("domain", s)
            used = set(con.execute("SELECT DISTINCT domain_id FROM nt_members WHERE domain_id IS NOT NULL").pl()
                       .get_column("domain_id").to_list())
            used |= set(con.execute("SELECT DISTINCT unnest(domain_ids) AS d FROM nt_stories").pl().get_column("d")
                        .drop_nulls().to_list())
            frames.append(dom.filter(pl.col("id").is_in(list(used))))
            dd = pl.concat([f.select("dict_type", "id", "code", "name", "grp", "theme_mask", "sector_mask")
                            for f in frames if f.height], how="vertical_relaxed")
            con.register("_dict", dd.to_arrow())
            con.execute("CREATE TABLE nt_dict AS SELECT * FROM _dict")
            con.unregister("_dict")
            _ = lab
        except Exception as e:  # noqa: BLE001
            log.warning("intraday: dictionaries unavailable: %s", e)
            con.execute("CREATE TABLE nt_dict (dict_type VARCHAR, id INTEGER, code VARCHAR, name VARCHAR, grp VARCHAR, "
                        "theme_mask BIGINT, sector_mask SMALLINT)")
        try:
            from app.nlp.common import security_map  # noqa: PLC0415

            im = issuer_meta(dicts.load_dict("issuer", s)).select("iid", "issuer_id", "name", "ticker", "country")
            sm = security_map(s).select("issuer_id", "security_id", pl.col("ticker").alias("__t"), "sector_id",
                                        pl.col("name").alias("__n"))
            iss = im.join(sm, on="issuer_id", how="left").with_columns(
                pl.coalesce("__t", "ticker").alias("ticker"), pl.coalesce("__n", "name").alias("name")).drop("__t", "__n")
            con.register("_iss", iss.to_arrow())
            con.execute("CREATE TABLE nt_issuer AS SELECT * FROM _iss")
            con.unregister("_iss")
        except Exception as e:  # noqa: BLE001
            log.warning("intraday: issuer map unavailable: %s", e)
            con.execute("CREATE TABLE nt_issuer (iid INTEGER, issuer_id VARCHAR, name VARCHAR, ticker VARCHAR, "
                        "country VARCHAR, security_id VARCHAR, sector_id VARCHAR)")
        con.execute("CREATE TABLE _meta (key VARCHAR, value VARCHAR)")
        meta = {"built_at": now.isoformat(timespec="seconds"), "window_start": window[0].isoformat(),
                "window_end": window[-1].isoformat(), **{k: str(v) for k, v in rep.items() if k.startswith("nt_")}}
        con.executemany("INSERT INTO _meta VALUES (?, ?)", list(meta.items()))
        con.execute("CHECKPOINT")
    finally:
        con.close()
        del con
        import gc  # noqa: PLC0415

        gc.collect()   # release DuckDB's instance cache for the file before it is renamed / opened read-only
    os.replace(tmp, final)
    rep["bytes"] = final.stat().st_size
    if publish:
        p = pointer_path(s)
        ptmp = p.with_suffix(".tmp")
        ptmp.write_text(name, encoding="utf-8")
        os.replace(ptmp, p)
        rep["removed"] = cleanup(s, keep=name)
    rep["seconds"] = round(time.perf_counter() - t0, 2)
    return rep


def cleanup(settings: Settings, keep: str) -> list[str]:
    files = sorted(settings.db_dir.glob("intraday_*.duckdb"), key=lambda p: p.name, reverse=True)
    removed = []
    for f in files[KEEP:]:
        if f.name == keep:
            continue
        try:
            f.unlink()
            removed.append(f.name)
        except OSError:   # still open by an API reader: next run
            pass
    for f in settings.db_dir.glob("intraday_*.duckdb.tmp"):
        if time.time() - f.stat().st_mtime > 3600:
            f.unlink(missing_ok=True)
    return removed
