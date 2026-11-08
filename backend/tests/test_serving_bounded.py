"""Serving build, refresh-pipeline fixes 2026-10-26: the bounded / re-planned builders return the same rows as the
queries they replace (ev2_stories split into single-day rows + multi-day aggregate, latest-per-key semi joins,
split dates without a window, sec_base without nested-loop joins), and the build connection carries the explicit
memory / thread / spill settings."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta

import duckdb
import pytest

from app.api import serving as sv

A = date(2026, 10, 23)

# the pre-2026-10-26 ev2_stories query (reference)
OLD_EV2 = """
    WITH g AS (
        SELECT story_id, min(date) AS first_date, max(date) AS last_date, min(first_ts) AS first_ts,
               max(last_ts) AS last_ts, arg_max(title, n_articles) AS title, arg_max(lang_id, n_articles) AS lang_id,
               sum(n_articles)::INTEGER AS n_articles, max(n_sources)::INTEGER AS n_sources, count(*)::INTEGER AS n_days,
               bit_or(source_mask) AS source_mask, arg_max(domain_ids, n_articles) AS domain_ids,
               arg_max(urls, n_articles) AS urls,
               sum(tone_x100 * n_articles) / nullif(sum(CASE WHEN tone_x100 IS NOT NULL THEN n_articles END), 0) / 100.0 AS tone,
               arg_max(sentiment_x100, n_articles) FILTER (WHERE sentiment_x100 IS NOT NULL) / 100.0 AS sentiment,
               arg_max(sentiment_model, n_articles) FILTER (WHERE sentiment_model IS NOT NULL) AS sentiment_model,
               bit_or(theme_mask) AS theme_mask, bit_or(sector_mask) AS sector_mask,
               list_distinct(flatten(list(countries))) AS countries,
               arg_max(issuer_ids, n_articles) AS issuer_ids, arg_max(issuer_rel, n_articles) AS issuer_rel,
               sum(n_events)::INTEGER AS n_events, max(salience)::DOUBLE AS salience
        FROM lake.news_stories WHERE date <= {A} AND date > {A} - INTERVAL 400 DAY GROUP BY story_id)
    SELECT * FROM g
    WHERE (last_date > {A} - INTERVAL 120 DAY AND (n_articles >= 2 OR len(issuer_ids) > 0 OR salience >= 2.2))
       OR len(issuer_ids) > 0 OR n_articles >= 5"""


@pytest.fixture()
def con(tmp_data):
    c = duckdb.connect(":memory:")
    inputs = sv.register_lake(c, tmp_data, empty=True)
    return c, inputs, tmp_data


def _stories(c: duckdb.DuckDBPyConnection, n: int = 3000, seed: int = 7) -> None:
    rnd = random.Random(seed)
    rows = []
    for sid in range(1, n + 1):
        days = 1 if rnd.random() < 0.8 else rnd.randint(2, 4)
        d0 = A - timedelta(days=rnd.randint(0, 420))
        for k in range(days):
            d = d0 + timedelta(days=k)
            na = rnd.choice([1, 1, 1, 2, 3, 7])          # distinct per story-day ties are irrelevant: one row per day
            rows.append((d.strftime("%Y-%m"), d, sid, f"title {sid} {k}", sid * 7919, rnd.randint(0, 3),
                         datetime(d.year, d.month, d.day, rnd.randint(0, 11)), datetime(d.year, d.month, d.day, 23),
                         na + k * 10, rnd.randint(1, 5), rnd.randint(0, 7), [rnd.randint(1, 50)], [f"u{sid}-{k}"],
                         rnd.choice([None, -150, 0, 230]), rnd.choice([None, -40, 12]),
                         rnd.choice([None, "finbert"]), rnd.randint(0, 255), rnd.randint(0, 7),
                         [rnd.randint(1, 9), rnd.randint(1, 9)], [] if rnd.random() < 0.7 else [rnd.randint(1, 30)],
                         [] , rnd.randint(0, 9), round(rnd.uniform(1.0, 4.0), 3)))
    c.execute("CREATE TABLE ns AS SELECT * FROM lake.news_stories LIMIT 0")
    c.executemany("INSERT INTO ns VALUES (" + ", ".join("?" * 23) + ")", rows)
    c.execute("UPDATE ns SET issuer_rel = list_transform(issuer_ids, x -> 50::TINYINT)")
    c.execute("CREATE OR REPLACE VIEW lake.news_stories AS SELECT * FROM ns")


def test_ev2_stories_split_matches_the_old_aggregate(con):
    c, inputs, s = con
    _stories(c)
    c.execute("CREATE TABLE sec AS SELECT 'S1' AS security_id, 'I1' AS issuer_id, true AS is_primary_security, "
              "1.0 AS market_cap_usd, 'T' AS ticker, 'N' AS name, 'X' AS sector_id, 'US' AS country")
    b = sv._Builder(c, "", A, s, inputs, True)
    b.events_v2()
    old = c.execute(f"SELECT * FROM ({OLD_EV2.format(A=sv._lit(A))}) ORDER BY story_id").fetchall()
    cols_old = [d[0] for d in c.description]
    new = c.execute("SELECT * FROM ev2_stories ORDER BY story_id").fetchall()
    cols_new = [d[0] for d in c.description]
    assert cols_new == cols_old
    assert len(new) == len(old) > 500
    for a, b_ in zip(old, new, strict=True):
        for x, y, name in zip(a, b_, cols_old, strict=True):
            if name == "countries":
                assert sorted(x) == sorted(y)
            elif isinstance(x, float) and y is not None:
                assert y == pytest.approx(x), name
            else:
                assert x == y, (name, x, y)
    # multi-day stories really went through the aggregate path
    assert c.execute("SELECT count(*) FROM ev2_stories WHERE n_days > 1").fetchone()[0] > 50


def test_latest_sql_equals_qualify_max(con):
    c, _, _ = con
    c.execute("""CREATE TABLE t AS SELECT 'S' || (i % 50) AS security_id, DATE '2026-01-01' + (i % 7)::INTEGER AS as_of_date,
                 i AS v FROM range(2000) r(i)""")
    old = c.execute("""SELECT * FROM t WHERE as_of_date <= DATE '2026-01-05'
        QUALIFY as_of_date = max(as_of_date) OVER (PARTITION BY security_id) ORDER BY security_id, v""").fetchall()
    new = c.execute(sv._latest_sql("*", "t", "as_of_date <= DATE '2026-01-05'", "security_id")
                    + " ORDER BY security_id, v").fetchall()
    assert old == new and len(new) > 50


def test_split_date_without_window(con):
    c, inputs, s = con
    # A: split on day 30 (factor 0.5 -> 1), B: no split, C: two splits (the last one counts)
    rows = []
    for sid, ch in (("A", [30]), ("B", []), ("C", [10, 50])):
        for i in range(80):
            sf = 1.0 / (2 ** sum(1 for x in ch if i < x))
            rows.append((sid, A - timedelta(days=80 - i), sf))
    c.execute("CREATE TABLE prices (security_id VARCHAR, date DATE, split_factor DOUBLE)")
    c.executemany("INSERT INTO prices VALUES (?, ?, ?)", rows)
    old = dict(c.execute(f"""SELECT security_id, max(date) FROM (
                SELECT security_id, date, split_factor,
                       lag(split_factor) OVER (PARTITION BY security_id ORDER BY date) AS prev_sf
                FROM prices WHERE date >= {sv._lit(A)} - INTERVAL 800 DAY)
            WHERE prev_sf IS NOT NULL AND abs(split_factor / NULLIF(prev_sf, 0) - 1) > 1e-6 GROUP BY 1""").fetchall())
    # the builder's _spl query, run on the same table
    sql = f"""
            WITH p AS (SELECT security_id, date, split_factor FROM prices WHERE date >= {sv._lit(A)} - INTERVAL 800 DAY),
            l AS (SELECT security_id, arg_max(split_factor, date) AS lsf FROM p GROUP BY 1),
            b AS (SELECT p.security_id, max(p.date) AS before FROM p JOIN l USING (security_id)
                  WHERE p.split_factor IS NOT NULL AND abs(p.split_factor / NULLIF(l.lsf, 0) - 1) > 1e-6 GROUP BY 1)
            SELECT p.security_id, min(p.date) AS last_split_date FROM p JOIN b USING (security_id)
            WHERE p.date > b.before AND p.split_factor IS NOT NULL GROUP BY 1"""
    assert dict(c.execute(sql).fetchall()) == old and set(old) == {"A", "C"}


def test_build_connection_settings(tmp_data, monkeypatch):
    monkeypatch.setattr(tmp_data, "serving_duckdb_memory_limit", "3GB", raising=False)
    monkeypatch.setattr(tmp_data, "serving_duckdb_threads", 3, raising=False)
    c = sv.serving_build_connection(tmp_data)
    got = dict(c.execute("SELECT name, value FROM duckdb_settings() WHERE name IN "
                         "('memory_limit', 'threads', 'temp_directory', 'preserve_insertion_order')").fetchall())
    assert got["threads"] == "3" and got["preserve_insertion_order"] == "false"
    assert 2.6 <= float(got["memory_limit"].split()[0]) <= 2.8      # DuckDB: "3GB" = 3e9 bytes = 2.79 GiB
    assert got["temp_directory"].replace("\\", "/").endswith("tmp/duckdb_serving")


def test_px_rank_from_the_ascending_window(con):
    """px_stats: rn (1 = latest bar) from the ascending window equals the old descending row_number()."""
    c, _, _ = con
    c.execute("""CREATE TABLE p AS SELECT 'S' || (i % 13) AS security_id, DATE '2025-01-01' + (i // 13)::INTEGER AS date,
                 random() AS x FROM range(5000) r(i)""")
    old = c.execute("""SELECT security_id, date, row_number() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                       FROM p ORDER BY 1, 2""").fetchall()
    new = c.execute("""SELECT security_id, date, count(*) OVER (PARTITION BY security_id) - row_number() OVER w + 1 AS rn
                       FROM p WINDOW w AS (PARTITION BY security_id ORDER BY date) ORDER BY 1, 2""").fetchall()
    assert old == new
