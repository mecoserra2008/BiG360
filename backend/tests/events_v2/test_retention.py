"""E3 day-file store + retention / compaction correctness (salient-only T2, members dropped, singleton stories pruned,
idempotent, re-processed days re-open compacted months, T1 compaction keeps every row)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from app.events_v2 import retention, store
from app.events_v2 import taxonomy as tx
from app.events_v2.agg import run_agg
from app.events_v2.pipeline import recompute_z


def _events(d: date, n: int, seed: int) -> pl.DataFrame:
    rows = []
    for i in range(n):
        rows.append({"date": d, "event_id": seed * 100000 + i, "event_date": d, "cameo": 190, "cameo_root": 19, "quad": 4,
                     "goldstein_x10": -100 if i == 0 else -20, "tone_x100": -300, "n_mentions": i + 1, "n_sources": 1,
                     "n_articles": 1, "country": tx.COUNTRY_ID["UA"], "countries": [tx.COUNTRY_ID["UA"]],
                     "lat_x100": 1, "lon_x100": 1, "url_hash": seed * 100000 + i, "domain_id": 1,
                     "theme_mask": tx.theme_ids_to_mask(["conflict_military"]), "sector_mask": 0,
                     "issuer_ids": [5] if i == n - 1 else [], "issuer_conf": [90] if i == n - 1 else [],
                     "salience": i * 10, "translated": False})
    return pl.DataFrame(rows).with_columns(pl.col("countries").cast(pl.List(pl.Int16)),
                                           pl.col("issuer_ids").cast(pl.List(pl.Int32)),
                                           pl.col("issuer_conf").cast(pl.List(pl.Int8)))


def _articles(d: date, n: int, seed: int) -> pl.DataFrame:
    rows = [{"date": d, "minute": i, "url_hash": seed * 100000 + i, "domain_id": 1, "lang_id": 1,
             "tone_x100": 1500 if i == 1 else -100, "polarity_x10": 5,
             "theme_mask": tx.theme_ids_to_mask(["inflation"]), "sector_mask": 0, "countries": [tx.COUNTRY_ID["DE"]],
             "issuer_ids": [], "issuer_conf": [], "story_id": seed * 1000 + i, "n_events": 0, "salience": i}
            for i in range(n)]
    return pl.DataFrame(rows).with_columns(pl.col("countries").cast(pl.List(pl.Int16)),
                                           pl.col("issuer_ids").cast(pl.List(pl.Int32)),
                                           pl.col("issuer_conf").cast(pl.List(pl.Int8)))


def _stories(d: date, seed: int) -> pl.DataFrame:
    t0 = datetime(d.year, d.month, d.day, tzinfo=UTC)
    base = {"date": d, "title_hash": 1, "lang_id": 1, "first_ts": t0, "last_ts": t0, "source_mask": 1,
            "domain_ids": [1], "urls": ["https://x", "https://y"], "tone_x100": -100, "sentiment_x100": None,
            "sentiment_model": None, "theme_mask": 1, "n_events": 0}
    rows = []
    for i in range(30):
        rows.append({**base, "story_id": seed * 1000 + i, "title": f"story {i}", "n_articles": 3 if i == 0 else 1,
                     "n_sources": 2 if i == 0 else 1, "sector_mask": 4 if i == 2 else 0,
                     "countries": [tx.COUNTRY_ID["DE"]], "issuer_ids": [5] if i == 1 else [],
                     "issuer_rel": [80] if i == 1 else [], "salience": float(i)})
    return pl.DataFrame(rows).with_columns(pl.col("countries").cast(pl.List(pl.Int16)),
                                           pl.col("n_sources").cast(pl.Int16), pl.col("sector_mask").cast(pl.Int16),
                                           pl.col("issuer_ids").cast(pl.List(pl.Int32)),
                                           pl.col("issuer_rel").cast(pl.List(pl.Int8)),
                                           pl.col("domain_ids").cast(pl.List(pl.Int32)),
                                           pl.col("sentiment_x100").cast(pl.Int8),
                                           pl.col("sentiment_model").cast(pl.Utf8))


def _members(d: date, seed: int) -> pl.DataFrame:
    return pl.DataFrame({"date": [d] * 5, "story_id": [seed * 1000] * 5, "url_hash": [seed * 10 + i for i in range(5)],
                         "source_id": [0] * 5, "domain_id": [1] * 5, "minute": list(range(5)), "tone_x100": [1] * 5,
                         "lang_id": [1] * 5})


def test_store_day_files_and_compaction(tmp_data):
    d1, d2 = date(2026, 1, 5), date(2026, 1, 6)
    b = store.write_day("gdelt_event_labels", d1, _events(d1, 40, 1), tmp_data)
    store.write_day("gdelt_event_labels", d2, _events(d2, 40, 2), tmp_data)
    assert b > 0 and store.day_file("gdelt_event_labels", d1, tmp_data).exists()
    got = store.read_range("gdelt_event_labels", d1, d2, tmp_data)
    assert got.height == 80 and got.schema["event_id"] == pl.Int64
    from app.core.storage import scan_table

    assert scan_table("gdelt_event_labels", tmp_data).collect().height == 80        # Hive layout readable as a table
    store.write_day("gdelt_event_labels", d1, _events(d1, 10, 1), tmp_data)         # re-process -> replaced
    assert store.read_range("gdelt_event_labels", d1, d1, tmp_data).height == 10
    r = store.compact_month("gdelt_event_labels", "2026-01", None, tmp_data)
    assert r["rows_before"] == r["rows_after"] == 50 and store.is_compacted("gdelt_event_labels", "2026-01", tmp_data)
    store.write_day("gdelt_event_labels", d2, _events(d2, 5, 2), tmp_data)          # re-open compacted month
    assert store.read_range("gdelt_event_labels", d2, d2, tmp_data).height == 5
    assert store.read_range("gdelt_event_labels", d1, d2, tmp_data).height == 15


def test_retention_prunes_old_months_only(tmp_data):
    old, recent = date(2026, 1, 10), date(2026, 9, 30)
    for d, seed in ((old, 1), (recent, 2)):
        store.write_day("gdelt_event_labels", d, _events(d, 40, seed), tmp_data)
        store.write_day("gdelt_article_labels", d, _articles(d, 40, seed), tmp_data)
        store.write_day("news_stories", d, _stories(d, seed), tmp_data)
        store.write_day("story_members", d, _members(d, seed), tmp_data)
    cfg = dict(retention.DEFAULTS)
    rep = retention.run_retention(tmp_data, {"dry_run": "true"}, today=date(2026, 10, 12))
    assert rep["months"]["gdelt_event_labels"] == ["2026-01"]
    rep = retention.run_retention(tmp_data, {}, today=date(2026, 10, 12))
    ev = store.read_range("gdelt_event_labels", old, old, tmp_data)
    # top-10 per country/theme by salience + the issuer-linked one + the |goldstein| extreme
    ids = set(ev.get_column("event_id").to_list())
    assert {100000 + i for i in range(30, 40)} <= ids and 100000 in ids and 100039 in ids and len(ids) == 11
    assert store.read_range("gdelt_event_labels", recent, recent, tmp_data).height == 40   # recent untouched
    ar = store.read_range("gdelt_article_labels", old, old, tmp_data)
    assert set(ar.get_column("url_hash").to_list()) == {100000 + i for i in range(30, 40)} | {100001}  # + tone extreme
    assert store.read_range("story_members", old, old, tmp_data).is_empty()
    assert store.read_range("story_members", recent, recent, tmp_data).height == 5
    st = store.read_range("news_stories", old, old, tmp_data)
    kept = set(st.get_column("story_id").to_list())
    # >= 2 sources, issuer-linked (theme-derived sector bits alone do not qualify), + top-20 per (day, country/theme)
    assert kept == {1000, 1001} | {1000 + i for i in range(30 - cfg["k_story"], 30)}
    assert st.select(pl.col("urls").list.len().max()).item() == 1          # one representative URL kept
    assert store.is_compacted("gdelt_article_labels", "2026-01", tmp_data)
    again = retention.run_retention(tmp_data, {}, today=date(2026, 10, 12))
    assert not any(again["months"].values())               # idempotent


def test_t1_z_scores_and_compaction(tmp_data):
    start = date(2026, 1, 1)
    for k in range(40):
        d = start + timedelta(days=k)
        n = 100 if k < 39 else 1000
        df = pl.DataFrame({"date": [d], "label_type": ["country"], "label_id": [5], "theme": [-1], "ev_n": [n],
                           "ev_mentions": [n], "ev_articles": [n], "ev_conflict_n": [0], "ev_goldstein": [0.0],
                           "ev_tone": [-1.0], "art_n": [n + (k % 3)], "art_tone": [-1.0 - (k % 2) * 0.1 if k < 39 else -8.0],
                           "art_neg_share": [0.1], "attention_z": [None], "tone_z": [None]})
        store.write_day("event_daily_agg", d, df, tmp_data)
    recompute_z(start, start + timedelta(days=39), tmp_data)
    last = store.read_range("event_daily_agg", start + timedelta(days=39), start + timedelta(days=39), tmp_data).row(0, named=True)
    assert last["attention_z"] > 5 and last["tone_z"] < -5
    first = store.read_range("event_daily_agg", start, start, tmp_data).row(0, named=True)
    assert first["attention_z"] is None                   # not enough history
    rep = run_agg(tmp_data, days=7, full=True, compact=True)
    assert rep["compacted"] >= 1 and store.read_range("event_daily_agg", start, start + timedelta(days=39), tmp_data).height == 40


def test_in_flight_prune_matches_retention(tmp_data):
    from app.events_v2.retention import prune_day

    d = date(2026, 1, 10)
    st, ev, ar = prune_day(_stories(d, 1), _events(d, 40, 1), _articles(d, 40, 1))
    assert ev.height == 11 and ar.height == 11 and st.height == 22
