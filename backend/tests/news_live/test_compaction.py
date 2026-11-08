"""Hourly fold (idempotent, live story ids kept, partial-day files), daily finalise (merge, roll-ups, partial
cleanup, checkpoints) and the GDELT hot store read by Streamer.fetch_day (no double download)."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import polars as pl

from app.events_v2 import store
from app.news_live import compaction as C
from app.news_live import hot
from tests.news_live.helpers import seed_dicts, seed_gdelt, seed_news

D = date(2026, 10, 20)


def _digest(s) -> dict[str, str]:
    out = {}
    for t in ("news_stories", "story_members", "gdelt_article_labels", "gdelt_event_labels"):
        f = store.partial_file(t, D, s)
        if f.exists():
            df = pl.read_parquet(f)
            key = [c for c in ("story_id", "url_hash", "event_id") if c in df.columns][:1]
            out[t] = hashlib.sha1(df.sort(key).drop([c for c in df.columns if c.endswith("_at")], strict=False)
                                  .write_ndjson().encode()).hexdigest()
    for t in ("event_hourly_agg", "sentiment_hourly"):
        df = store.read_range(t, D, D, s).drop("computed_at", strict=False)
        out[t] = hashlib.sha1(df.sort(df.columns[:5]).write_ndjson().encode()).hexdigest()
    return out


def test_hourly_fold_idempotent_partial_files(tmp_data):
    seed_dicts(tmp_data)
    g = seed_gdelt(tmp_data, D, [10, 11])
    seed_news(tmp_data, D, 10)
    now = datetime(2026, 10, 20, 12, 20, tzinfo=UTC)
    rep = C.run_hourly(tmp_data, now=now, xlmr=False)
    assert sorted((h["day"], h["hour"]) for h in rep["hours"]) == [("2026-10-20", 10), ("2026-10-20", 11)]
    day = rep["days"]["2026-10-20"]
    assert day["gdelt_article_labels"] == g["articles"] and day["event_hourly_agg"] > 0 and day["sentiment_hourly"] > 0
    st = store.read_partial("news_stories", D, tmp_data)
    sm = store.read_partial("story_members", D, tmp_data)
    assert sm.get_column("url_hash").n_unique() == sm.height              # unique members
    live = st.filter(pl.col("story_id") == 7001)
    assert live.height == 1 and live["n_articles"][0] == 2               # the desk's live story id is kept
    assert live["sentiment_x100"][0] == -50 and live["sentiment_model"][0] == "live"
    assert store.read_range("news_stories", D, D, tmp_data).is_empty()   # read_range ignores partial files
    agg = store.read_range("event_hourly_agg", D, D, tmp_data)
    assert set(agg.get_column("hour").to_list()) == {10, 11} and set(agg.get_column("slots").to_list()) == {4}
    sh = store.read_range("sentiment_hourly", D, D, tmp_data)
    allrow = sh.filter((pl.col("hour") == 10) & (pl.col("label_type") == "all"))
    assert allrow["n_items"][0] == 3 and allrow["n_scored"][0] == 2 and abs(allrow["sentiment_mean"][0] + 0.5) < 1e-6
    assert sh.filter((pl.col("label_type") == "issuer") & (pl.col("label_id") == "INVDA")).height >= 1
    # idempotent: a second run folds nothing, files unchanged
    before = _digest(tmp_data)
    rep2 = C.run_hourly(tmp_data, now=now, xlmr=False)
    assert rep2["hours"] == [] and _digest(tmp_data) == before
    # new input in hour 10 -> only hour 10 refolds; still no duplicates
    seed_news(tmp_data, D, 10)                        # rewritten (new mtime), same rows
    rep3 = C.run_hourly(tmp_data, now=now, xlmr=False)
    assert [h["hour"] for h in rep3["hours"]] == [10, 11]          # later hours of the day refold too
    assert _digest(tmp_data) == before
    st = C.read_state(tmp_data)
    assert set(st["folded"]["2026-10-20"]) == {"10", "11"} and st["last_hourly"]["hours"] == 2


def test_finalise_merges_live_news_rolls_up_and_cleans(tmp_data):
    seed_dicts(tmp_data)
    seed_gdelt(tmp_data, D, [10])
    seed_news(tmp_data, D, 10)
    now = datetime(2026, 10, 21, 1, 0, tzinfo=UTC)
    C.run_hourly(tmp_data, now=now, xlmr=False)
    # the nightly gdelt_stream already assembled the day: d- files of the GDELT stories exist
    gst = store.read_partial("news_stories", D, tmp_data).filter(pl.col("story_id") != 7001)
    gsm = store.read_partial("story_members", D, tmp_data).filter(~pl.col("url_hash").is_in([9001, 9002, 9003]))
    store.write_day("news_stories", D, gst, tmp_data)
    store.write_day("story_members", D, gsm, tmp_data)
    from app.events_v2.pipeline import update_state

    update_state(lambda s: s.setdefault("days", {}).update({D.isoformat(): {"label": "nightly", "files_ok": 24}}),
                 tmp_data)
    rep = C.run_finalise(tmp_data, now=now, days=[D], sentiment=False)
    r = rep["days"][D.isoformat()]
    assert r["assembly"].startswith("already checkpointed") and r["merge"]["items"] == 3
    for t in C.V2_PARTIAL_TABLES:
        assert not store.partial_file(t, D, tmp_data).exists(), t
    mem = store.read_range("story_members", D, D, tmp_data)
    assert {9001, 9002, 9003} <= set(mem.get_column("url_hash").to_list())
    assert mem.get_column("url_hash").n_unique() == mem.height
    lab = store.read_range("sentiment_label_daily", D, D, tmp_data)
    assert lab.filter(pl.col("label_type") == "all")["n_items"][0] == 3
    assert D.isoformat() in C.read_state(tmp_data)["finalised"]
    # re-run: idempotent (nothing new merged), finalised days are not folded again
    rep2 = C.run_finalise(tmp_data, now=now, days=[D], sentiment=False)
    assert rep2["days"][D.isoformat()]["merge"]["items"] == 0
    assert C.run_hourly(tmp_data, now=now, xlmr=False)["hours"] == []
    # automatic selection: finalised days are skipped
    assert C.run_finalise(tmp_data, now=now)["days"] == {}


def test_streamer_reads_hot_slots_instead_of_downloading(tmp_data):
    from app.events_v2.pipeline import Streamer

    seed_dicts(tmp_data)
    seed_gdelt(tmp_data, D, [0])
    st = object.__new__(Streamer)
    st.s, st.legacy, st.use_hot = tmp_data, False, True
    st.pause_check = st.stop_check = None

    asked: list[str] = []

    def no_download(url):
        assert ".translation." in url, f"downloaded {url}"      # only the files the live path saw as missing
        asked.append(url)
        return None, 0.0

    st._fetch = no_download
    st.ppool = st.dpool = ThreadPoolExecutor(1)
    try:
        latest = datetime(2026, 10, 20, 0, 45, tzinfo=UTC)
        out = st.fetch_day(D, latest)
    finally:
        st.dpool.shutdown()
    assert out["files"] == 24 and out["stats"]["hot_hits"] == 12 and len(asked) == 12
    assert out["files_ok"] == 12 and out["files_missing"] == 12     # missing in the hot store -> asked again (404)
    assert len(out["articles"]) == 4 and len(out["events"]) == 4 and len(out["mentions"]) == 4
    # legacy runs (nightly gdelt_stream) need the legacy outputs: files parsed without them are downloaded
    st.legacy = True
    assert hot.load_slot_kind(datetime(2026, 10, 20, 0, 0, tzinfo=UTC), "gkg", True, tmp_data) is not None
    hot.write_slot_kind(datetime(2026, 10, 20, 0, 0, tzinfo=UTC), "gkg", pl.DataFrame({"x": [1]}), None, "ok", None,
                        tmp_data, legacy_done=False)
    assert hot.load_slot_kind(datetime(2026, 10, 20, 0, 0, tzinfo=UTC), "gkg", True, tmp_data) is None


def test_retention_and_hot_prune(tmp_data):
    for d in (date(2026, 10, 1), date(2026, 10, 18), date(2026, 10, 21)):
        (hot.day_dir(hot.gdelt_hot_dir(tmp_data), d)).mkdir(parents=True)
        (hot.day_dir(hot.news_hot_dir(tmp_data), d)).mkdir(parents=True)
    rep = C.retention(tmp_data, datetime(2026, 10, 22, 12, tzinfo=UTC), {"2026-10-18"})
    assert rep["gdelt_hot_removed"] == ["2026-10-01", "2026-10-18"] and rep["news_hot_removed"] == ["2026-10-01"]
    assert hot.hot_days(hot.gdelt_hot_dir(tmp_data)) == [date(2026, 10, 21)]


def test_closed_hours_window():
    hs = C.closed_hours(datetime(2026, 10, 20, 12, 5, tzinfo=UTC))
    assert hs[-1] == datetime(2026, 10, 20, 10, tzinfo=UTC) and len(hs) == C.LOOKBACK_H   # 11:00 not closed + 8 min
    hs = C.closed_hours(datetime(2026, 10, 20, 12, 10, tzinfo=UTC))
    assert hs[-1] == datetime(2026, 10, 20, 11, tzinfo=UTC)
    _ = timedelta
