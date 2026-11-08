"""N1 CC-NEWS: WARC parsing (recorded fixture, no network), derived-only storage, dedupe against GDELT, gating /
repair, campaign state / resume, probe."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.events_v2 import ccnews as C
from app.events_v2 import ccnews_parse as P
from app.events_v2 import dicts, store
from app.events_v2 import hist_merge as H
from app.events_v2 import taxonomy as tx
from app.events_v2.parse import hash64, url_key_expr
from app.events_v2.stories import STORY_SCHEMA
from tests.events_v2 import helpers as Hh
from tests.events_v2.fixtures.make_ccnews_fixture import BODY_SENTINEL

FIX = Path(__file__).parent / "fixtures" / "ccnews_sample.warc.gz"
DAY = date(2026, 9, 15)
TODAY = date(2026, 10, 1)


@pytest.fixture()
def ctx():
    return C.label_ctx_from_worker(Hh.worker_ctx())


def _rows(ctx):
    rows, st = P.parse_file(str(FIX), ctx)
    return {r[0]: dict(zip(P.ROW_FIELDS, r, strict=True)) for r in rows}, st


# ------------------------------------------------------------------------------------------------ parsing
def test_url_key_and_hash_match_gdelt():
    urls = ["https://www.Reuters.com/markets/x/?utm=1#a", "http://www2.example.co.uk/a/b/", "HTTPS://WWW.FT.com/c"]
    keys = pl.DataFrame({"u": urls}).select(url_key_expr(pl.col("u"))).to_series().to_list()
    assert [P.url_key(u) for u in urls] == keys
    assert [P.url_hash(u) for u in urls] == [hash64(k) for k in keys]


def test_gzip_members_any_chunking_and_resume_offset():
    data = FIX.read_bytes()
    whole = P.GzipMembers().feed(data)
    assert len(whole) == 20                                     # warcinfo + 9 request/response pairs + 2 responses
    gz, got = P.GzipMembers(), []
    for i in range(0, len(data), 97):
        got += gz.feed(data[i:i + 97])
    assert got == whole and gz.consumed == len(data)
    # interrupted after a few members: resume from `consumed` with a fresh stream (HTTP Range) -> same records
    gz2 = P.GzipMembers()
    part = gz2.feed(data[:3000])
    off = gz2.consumed
    gz2.reset_partial()
    rest = gz2.feed(data[off:])
    assert part + rest == whole


def test_parse_fixture_fields_and_rules(ctx):
    rows, st = _rows(ctx)
    assert st["records"] == 20 and st["responses"] == 10 and st["html"] == 8 and st["non_html"] == 2
    assert st["stale"] == 1                                     # published 2.5 months before the crawl
    assert "https://www.example.com/local/bakery-award" not in rows   # no theme, no issuer -> forgotten
    nv = rows["https://www.example.com/markets/nvidia-record-revenue"]
    assert nv["title"] == "Nvidia shares jump after record quarterly revenue"   # og:title, no ' - Example News'
    assert nv["description"].startswith("Nvidia reported record data-center revenue")
    assert nv["lang"] == "en" and nv["ts_source"] == 0 and nv["ts"] == 1789453800
    assert nv["issuer_ids"] == [Hh.iid_of("INVDA")] and nv["issuer_conf"][0] >= 50
    assert nv["theme_mask"] & (1 << tx.THEME_BIT["earnings"]) and nv["countries"] == ["US"]
    fr = rows["https://www.lemonde-example.fr/economie/carrefour-acquisition"]
    assert fr["lang"] == "fr" and fr["issuer_ids"] == [Hh.iid_of("ICARR")] and fr["theme_mask"] == 0
    assert fr["ts"] == 1789448700                              # 07:05+02:00 -> 05:05 UTC
    de = rows["https://www.example.de/wirtschaft/chevron"]
    assert de["issuer_ids"] == [Hh.iid_of("ICVX")] and "Ölfelder" in de["title"]   # iso-8859-1 decoded
    eu = rows["https://news.example.org/world/eu-sanctions-russian-oil"]
    assert eu["ts_source"] == 1 and eu["countries"] == ["RU"]          # no page time -> WARC date
    assert eu["theme_mask"] & (1 << tx.THEME_BIT["sanctions"])
    for r in rows.values():
        assert BODY_SENTINEL not in (r["title"] or "") + (r["description"] or "")
        assert len(r["description"] or "") <= P.TEXT_MAX + 1


def test_free_text_link_rules(ctx):
    link = lambda t, d=None, lang="en": [i for i, _ in P.link_issuers(t, d, ctx, lang)]  # noqa: E731
    assert link("Chipotle shares slide after weak quarter") == [Hh.iid_of("ICMG")]
    # a low-confidence ambiguous alias ('delta air lines' is all dictionary words in the test alias table) needs a
    # strong cue: here the legal form right after the name
    assert link("Delta Air Lines adds flights to Lisbon") == []
    assert link("Delta Air Lines shares fall as fuel costs rise",
                "Shares of Delta Air Lines fell 3% on Tuesday.") == []
    assert link("Delta Air Lines Inc. shares fall as fuel costs rise") == [Hh.iid_of("IDAL")]
    assert link("Fans gather at the Chipotle Stadium before kick-off",
                "Thousands of fans and shares of the club...") == []                 # venue named after a company
    assert link("Hikers explore the Okavango delta this summer") == []           # ambiguous, no cue
    assert link("Record crowd at the Starbucks Reserve opening",
                "The starbucks reserve roastery opened downtown.") == []        # lower case in sentence-case text
    assert link("Shares of the chipmaker rose (NASDAQ: NVID) after the report") == [Hh.iid_of("INVDA")]
    assert link("S&P 500 closes at record high") == []                            # index names never link


# ------------------------------------------------------------------------------------------------ storage
def _setup_lake(s, gdelt_ready: bool = True):
    dicts.ensure_static(s)
    dicts.write_dict(Hh.issuer_dict(), s)
    dicts.assign_ids("lang", ["en", "fr", "de"], s)              # known languages (GDELT already assigned them)
    src = H.source_ids(s)
    # one GDELT story + member on DAY: same URL as fixture article 6, near-duplicate title of article 7
    g_url = "https://www.gdelt-dup.com/fed-holds-rates"
    g_hash = P.url_hash(g_url)
    sid = 4242
    st = pl.DataFrame([{"date": DAY, "story_id": sid, "title": "Fed holds rates steady as inflation cools",
                        "title_hash": 1, "lang_id": None, "first_ts": None, "last_ts": None, "n_articles": 1,
                        "n_sources": 1, "source_mask": 1, "domain_ids": [], "urls": [g_url], "tone_x100": -50,
                        "sentiment_x100": None, "sentiment_model": None, "theme_mask": 2, "sector_mask": 0,
                        "countries": [tx.COUNTRY_ID["US"]], "issuer_ids": [], "issuer_rel": [], "n_events": 0,
                        "salience": 1.0}])
    from datetime import UTC, datetime  # noqa: PLC0415

    st = st.with_columns(pl.lit(datetime(2026, 9, 15, 4, tzinfo=UTC)).alias("first_ts"),
                         pl.lit(datetime(2026, 9, 15, 4, tzinfo=UTC)).alias("last_ts"))
    store.write_day("news_stories", DAY, H._ensure_cols(st, STORY_SCHEMA), s)
    store.write_day("story_members", DAY, pl.DataFrame([{"date": DAY, "story_id": sid, "url_hash": g_hash,
                                                        "source_id": 0, "domain_id": None, "minute": 240,
                                                        "tone_x100": -50, "lang_id": None}]), s)
    if gdelt_ready:
        from app.events_v2.pipeline import update_state  # noqa: PLC0415

        update_state(lambda x: x.setdefault("days", {}).update(
            {d.isoformat(): {"files_ok": 576, "label": "test"} for d in (DAY, date(2026, 9, 14))}), s)
    return src, sid, g_hash


def _spool_fixture(s, ctx):
    rows, _ = P.parse_file(str(FIX), ctx)
    C.write_spool("2026-09", 0, rows, s)
    return rows


def test_finalize_derived_only_dedupe_and_story_layer(tmp_data, ctx):
    s = tmp_data
    src, sid, g_hash = _setup_lake(s)
    _spool_fixture(s, ctx)
    rep = H.finalize_ccnews_days(C.spool_days(s), s, C.read_spool_day, sentiment=False, today=TODAY)
    assert rep["url_dupes"] == 1 and rep["url_dupes_gdelt"] == 1
    arts = store.read_range(H.CC_TABLE, date(2026, 9, 1), date(2026, 9, 30), s)
    assert set(arts.columns) == set(H.CC_SCHEMA)           # derived columns only (no body / html column at all)
    dup = arts.filter(pl.col("url_hash") == g_hash).row(0, named=True)
    assert dup["story_id"] == sid and dup["dup_source_id"] == 0
    near = arts.filter(pl.col("url") == "https://www.another-paper.com/economy/fed-steady").row(0, named=True)
    assert near["story_id"] == sid and near["dup_source_id"] is None        # MinHash join into the GDELT story
    assert all(len(b) == 8 for b in arts.get_column("minhash_bands").to_list())
    day = store.read_range("news_stories", DAY, DAY, s)
    g = day.filter(pl.col("story_id") == sid).row(0, named=True)
    assert g["n_articles"] == 2 and g["source_mask"] & (1 << src[H.SRC_CCNEWS]) and g["source_mask"] & 1
    nv = day.filter(pl.col("title").str.starts_with("Nvidia")).row(0, named=True)
    assert nv["issuer_ids"] == [Hh.iid_of("INVDA")] and nv["source_mask"] == 1 << src[H.SRC_CCNEWS]
    mem = store.read_range("story_members", DAY, DAY, s)
    assert mem.filter(pl.col("source_id") == src[H.SRC_CCNEWS]).height == 4   # Nvidia, Carrefour, EU, near-dup
    assert not list((C.spool_dir(s)).rglob("*.parquet"))                    # spool deleted after merge
    # no body text anywhere in the data dir (every parquet string cell + state json)
    for f in s.data_dir.rglob("*.parquet"):
        df = pl.read_parquet(f)
        for c, t in df.schema.items():
            if t == pl.Utf8:
                assert not df.get_column(c).str.contains(BODY_SENTINEL).any(), (f, c)
            elif t == pl.List(pl.Utf8):
                assert not df.get_column(c).list.join(" ").str.contains(BODY_SENTINEL).any(), (f, c)
    for f in s.state_dir.rglob("*.json"):
        assert BODY_SENTINEL not in f.read_text(encoding="utf-8")
    # relink with the same rules is a no-op for links; stories keep their issuers
    rr = H.relink_ccnews(date(2026, 9, 14), date(2026, 9, 15), s, ctx)
    assert rr["changed"] == 0 and rr["dropped_articles"] == 0 and rr["stories_dropped"] == 0
    nv2 = store.read_range("news_stories", DAY, DAY, s).filter(pl.col("title").str.starts_with("Nvidia")).row(0, named=True)
    assert nv2["issuer_ids"] == [Hh.iid_of("INVDA")]
    # idempotent: a second finalize of the same spool adds nothing
    _spool_fixture(s, ctx)
    H.finalize_ccnews_days(C.spool_days(s), s, C.read_spool_day, sentiment=False, today=TODAY)
    assert store.read_range(H.CC_TABLE, date(2026, 9, 1), date(2026, 9, 30), s).height == arts.height
    assert store.read_range("news_stories", DAY, DAY, s).filter(pl.col("story_id") == sid)["n_articles"][0] == 2


def test_pending_gdelt_then_repair(tmp_data, ctx):
    s = tmp_data
    src, sid, _ = _setup_lake(s, gdelt_ready=False)
    _spool_fixture(s, ctx)
    rep = H.finalize_ccnews_days(C.spool_days(s), s, C.read_spool_day, sentiment=False, today=TODAY)
    assert rep["pending_gdelt_days"] >= 1
    day = store.read_range("news_stories", DAY, DAY, s)
    assert day.height == 1                                   # story layer untouched while GDELT has not done the day
    assert store.read_range(H.CC_TABLE, DAY, DAY, s).get_column("story_id").null_count() > 0
    from app.events_v2.pipeline import update_state  # noqa: PLC0415

    update_state(lambda x: x.setdefault("days", {}).update({DAY.isoformat(): {"files_ok": 576},
                                                            "2026-09-14": {"files_ok": 576}}), s)
    r = H.repair_ccnews(s, today=TODAY)
    assert r["pending_ready"] >= 1 and r["remerged"] >= 1
    assert store.read_range(H.CC_TABLE, DAY, DAY, s).get_column("story_id").null_count() == 0
    assert store.read_range("news_stories", DAY, DAY, s).height > 1
    # a later GDELT re-write of the day (redo) drops the ccnews rows -> repair re-attaches them
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    st0 = store.read_range("news_stories", DAY, DAY, s)
    store.write_day("news_stories", DAY, st0.filter(pl.col("story_id") == sid).with_columns(
        pl.lit(1, pl.Int16).alias("source_mask")), s)
    f = store.day_file("news_stories", DAY, s)
    os.utime(f, (time.time() + 600, time.time() + 600))
    r2 = H.repair_ccnews(s, today=TODAY)
    assert r2["lost"] >= 1
    after = store.read_range("news_stories", DAY, DAY, s)
    assert after.filter(pl.col("story_id") == sid)["n_articles"][0] == 2         # surviving story not re-counted
    assert after.filter(pl.col("title").str.starts_with("Nvidia"))["n_articles"].to_list() == [1]   # orphan re-attached once
    assert H.repair_ccnews(s, today=TODAY)["remerged"] == 0                        # stable afterwards
    assert store.read_range("news_stories", DAY, DAY, s).filter(
        (pl.col("source_mask").cast(pl.Int32) & (1 << src[H.SRC_CCNEWS])) != 0).height >= 1


def test_old_days_long_term_form(tmp_data, ctx):
    """Days older than 120 d: no story members for ccnews; older than 180 d: new stories pruned by the E6 rule."""
    s = tmp_data
    src, sid, _ = _setup_lake(s)
    _spool_fixture(s, ctx)
    H.finalize_ccnews_days(C.spool_days(s), s, C.read_spool_day, sentiment=False, today=date(2027, 6, 1))
    mem = store.read_range("story_members", DAY, DAY, s)
    assert mem.filter(pl.col("source_id") == src[H.SRC_CCNEWS]).is_empty()
    day = store.read_range("news_stories", DAY, DAY, s)
    kept = day.filter((pl.col("source_mask").cast(pl.Int32) & (1 << src[H.SRC_CCNEWS])) != 0)
    new = kept.filter(pl.col("story_id") != sid)                 # existing GDELT rows are never pruned here
    assert new.height >= 1 and new.get_column("urls").list.len().max() <= 1
    assert sid in day.get_column("story_id").to_list()


def test_sentiment_one_model_at_a_time(tmp_data, ctx):
    s = tmp_data
    src, _, _ = _setup_lake(s)
    _spool_fixture(s, ctx)
    H.finalize_ccnews_days(C.spool_days(s), s, C.read_spool_day, sentiment=False, today=TODAY)
    loaded: list[str] = []

    class Fake:
        def __init__(self, mid):
            loaded.append(mid)

        def predict(self, texts, bs):
            import numpy as np  # noqa: PLC0415

            return np.array([[0.1, 0.2, 0.7]] * len(texts))
    rep = H.score_story_sentiment([DAY], s, src[H.SRC_CCNEWS], model_factory=Fake)
    assert rep["scored"] >= 3 and len(loaded) == 2           # en (FinBERT) then xx (XLM-R), never both at once
    day = store.read_range("news_stories", DAY, DAY, s)
    cc = day.filter((pl.col("source_mask").cast(pl.Int32) & (1 << src[H.SRC_CCNEWS])) != 0)
    assert cc.get_column("sentiment_x100").null_count() == 0 and cc["sentiment_x100"][0] == 60
    arts = store.read_range(H.CC_TABLE, DAY, DAY, s)
    assert arts.filter(pl.col("story_id").is_in(cc["story_id"])).get_column("sentiment_x100").null_count() == 0
    # cached: a second pass scores nothing new
    assert H.score_story_sentiment([DAY], s, src[H.SRC_CCNEWS], model_factory=Fake)["scored"] == 0


def test_recluster_merges_cross_source_duplicates(tmp_data):
    s = tmp_data
    dicts.ensure_static(s)
    src = H.source_ids(s)
    from datetime import UTC, datetime  # noqa: PLC0415

    t0 = datetime(2026, 9, 15, 4, tzinfo=UTC)
    base = {"date": DAY, "title_hash": 1, "lang_id": None, "first_ts": t0, "last_ts": t0, "n_sources": 1,
            "domain_ids": [], "urls": [], "tone_x100": None, "sentiment_x100": None, "sentiment_model": None,
            "theme_mask": 0, "sector_mask": 0, "countries": [0], "issuer_ids": [], "issuer_rel": [], "n_events": 0,
            "salience": 1.0}
    rows = [{**base, "story_id": 1, "title": "Oil prices jump after OPEC surprise output cut", "n_articles": 5, "source_mask": 1},
            {**base, "story_id": 2, "title": "Oil prices jump after surprise OPEC output cut", "n_articles": 2,
             "source_mask": 1 << src[H.SRC_CCNEWS]},
            {**base, "story_id": 3, "title": "Oil prices jump after OPEC surprise output cut today", "n_articles": 1, "source_mask": 1},
            {**base, "story_id": 4, "title": "Apple 8-K: Results of operations (2.02)", "n_articles": 1,
             "source_mask": 1 << src[H.SRC_8K]}]
    store.write_day("news_stories", DAY, H._ensure_cols(pl.DataFrame(rows), STORY_SCHEMA), s)
    rep = H.recluster(DAY, DAY, s)
    day = store.read_range("news_stories", DAY, DAY, s)
    assert rep["merged"] >= 1 and 2 not in day.get_column("story_id").to_list()
    big = day.filter(pl.col("story_id") == 1).row(0, named=True)
    assert big["source_mask"] & (1 << src[H.SRC_CCNEWS]) and big["n_articles"] >= 7
    assert 3 in day.get_column("story_id").to_list() or big["n_articles"] == 8   # GDELT-only pair left alone
    assert 4 in day.get_column("story_id").to_list()                              # filing stories excluded


# ------------------------------------------------------------------------------------------------ campaign
def _patch_index(monkeypatch, tmp_path, n_files: int = 2):
    d = tmp_path / "warcs"
    d.mkdir(exist_ok=True)
    names = []
    for i in range(n_files):
        nm = f"CC-NEWS-2026091508142{i}-0000{i}.warc.gz"
        shutil.copy(FIX, d / nm)
        names.append(f"crawl-data/CC-NEWS/2026/09/{nm}")
    monkeypatch.setattr(C, "month_index", lambda ym, s, refresh=None: names if ym == "2026-09" else [])
    return d


def test_campaign_state_resume_stop_and_probe(tmp_data, ctx, monkeypatch, tmp_path):
    s = tmp_data
    _setup_lake(s)
    d = _patch_index(monkeypatch, tmp_path, 2)
    monkeypatch.setattr(C, "build_label_ctx", lambda s, max_age_h=72.0: ctx)
    base = ["--months", "2026-09", "--workers", "1", "--local-dir", str(d), "--no-sentiment"]
    assert C.main([*base, "--max-files", "1"]) == 0
    st = C.read_state(s)
    m = st["months"]["2026-09"]
    assert m["done"] == "0" and m["parsed"] == 1 and m["files"] == 2 and m["stats"]["records"] == 20
    assert st["run"]["status"] == "done" and st["days"][DAY.isoformat()]["story_layer"] == "merged"
    # resume: only the second file is parsed (records add up to 2 files, not 3)
    assert C.main(base) == 0
    m = C.read_state(s)["months"]["2026-09"]
    assert m["done"] == "0-1" and m["parsed"] == 2 and m["stats"]["records"] == 40
    # nothing left: no file parsed again
    assert C.main(base) == 0
    assert C.read_state(s)["months"]["2026-09"]["stats"]["records"] == 40
    # probe (Data Health progress bar)
    pr = C.probe_progress({"start_month": "2026-09"}, s, today=date(2026, 9, 20))
    assert pr["done_units"] == 2 and pr["total_units"] == 2 and pr["pct"] == 100.0 and pr["unit"] == "WARC files"
    from app.ops.tasks import probe  # noqa: PLC0415

    p2 = probe({"progress": {"probe": "ccnews_months", "start_month": "2026-09"}}, s)
    assert p2["done_units"] == 2 and p2["detail"]
    # STOP file: exits before touching the month
    C.stop_path(s).parent.mkdir(parents=True, exist_ok=True)
    C.stop_path(s).write_text("x")
    assert C.main([*base, "--redo"]) == 0
    assert C.read_state(s)["run"]["status"] == "stopped"


def test_yield_to_waiting_campaign(tmp_data, monkeypatch, tmp_path):
    s = tmp_data
    (s.state_dir / "ops").mkdir(parents=True, exist_ok=True)
    (s.state_dir / "ops" / "filler.json").write_text(json.dumps({
        "campaign_status": {"gdelt_e6": {"running": False, "progress": {"done": False}}},
        "waiting": {"campaign:gdelt_e6": "RAM cap"}}), encoding="utf-8")
    assert C._filler_waiting_for("gdelt_e6", s) is True
    assert C.main(["--months", "2026-09", "--yield-to", "gdelt_e6"]) == C.EXIT_BLOCKED
    (s.state_dir / "ops" / "filler.json").write_text(json.dumps({
        "campaign_status": {"gdelt_e6": {"running": True}}, "waiting": {}}), encoding="utf-8")
    assert C._filler_waiting_for("gdelt_e6", s) is False


def test_month_helpers():
    assert C.month_range("2026-08", "2026-10") == ["2026-10", "2026-09", "2026-08"]
    assert C.set_to_ranges({0, 1, 2, 5, 7, 8}) == "0-2,5,7-8"
    assert C.ranges_to_set("0-2,5,7-8") == {0, 1, 2, 5, 7, 8}
    assert C.month_closed("2026-09", date(2026, 10, 4)) and not C.month_closed("2026-09", date(2026, 10, 2))
