"""N1 official corporate news: SEC 8-K item mapping, story rows, idempotent story-layer merge, GDELT gating,
TDnet / HKEX announcements, probe."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl

from app.events_v2 import dicts, store
from app.events_v2 import filing_news as F
from app.events_v2 import hist_merge as H
from app.events_v2 import taxonomy as tx
from tests.events_v2 import helpers as Hh

T = tx.THEME_BIT


def test_item_mapping():
    head, tm, pol, rule = F.map_items(["2.02", "9.01"])
    assert head == "Results of operations and financial condition (2.02)" and tm == 1 << T["earnings"] and pol is None
    head, tm, pol, rule = F.map_items(["1.01", "2.03", "9.01"])
    assert tm == (1 << T["m_and_a"]) | (1 << T["capital_markets"]) and "(1.01)" in head and "(2.03)" in head
    _, tm, pol, rule = F.map_items(["1.03"])
    assert tm == 1 << T["bankruptcy_default"] and pol == -60 and rule.endswith(":1.03")
    _, _, pol, rule = F.map_items(["5.02", "4.02"])
    assert pol == -60 and rule.endswith(":4.02")                       # most negative item wins
    _, tm, pol, _ = F.map_items(["5.02"])
    assert tm == 0 and pol is None                                       # management change: no theme, neutral
    _, tm, _, _ = F.map_items(["12", "7"])                               # pre-2004 numbering: results
    assert tm == 1 << T["earnings"]
    assert F.norm_item("2.2") == "2.20" and F.norm_item(" 02 ") == "2" and F.norm_item("1.01") == "1.01"


def _sec_df() -> pl.DataFrame:
    rows = [
        {"cik": "0000000001", "accession": "0000000001-26-000001", "form": "8-K", "filing_date": date(2026, 9, 15),
         "acceptance_ts": datetime(2026, 9, 15, 20, 5, tzinfo=UTC), "report_date": None, "items": ["2.02", "9.01"],
         "url": "https://www.sec.gov/Archives/edgar/data/1/000000000126000001/nvda-8k.htm",
         "issuer_id": "INVDA", "name": "NVIDIA Corporation"},
        {"cik": "0000000002", "accession": "0000000002-26-000002", "form": "8-K", "filing_date": date(2026, 9, 15),
         "acceptance_ts": None, "report_date": None, "items": ["1.03"],
         "url": "https://www.sec.gov/Archives/edgar/data/2/x.htm", "issuer_id": "IDAL", "name": "Delta Air Lines, Inc."},
        {"cik": "0000000003", "accession": "0000000003-26-000003", "form": "8-K", "filing_date": date(2026, 9, 15),
         "acceptance_ts": None, "report_date": None, "items": ["9.01"],
         "url": "https://www.sec.gov/x3.htm", "issuer_id": "ICVX", "name": "Chevron"},          # exhibits only
        {"cik": "0000000004", "accession": "0000000004-03-000004", "form": "8-K", "filing_date": date(2003, 5, 1),
         "acceptance_ts": datetime(2003, 5, 1, 21, 0, tzinfo=UTC), "report_date": None, "items": ["12"],
         "url": "https://www.sec.gov/x4.htm", "issuer_id": "ICITI", "name": "Citigroup Inc."},
    ]
    return pl.DataFrame(rows, infer_schema_length=None)


def _maps(s):
    dicts.ensure_static(s)
    dicts.write_dict(Hh.issuer_dict(), s)
    idict = dicts.load_dict("issuer", s)
    iid = dict(zip(idict["code"].to_list(), idict["id"].to_list(), strict=True))
    sec = {int(i): int(m or 0) for i, m in idict.select("id", "sector_mask").iter_rows()}
    cty = {int(i): c for i, c in idict.select("id", "grp").iter_rows()}
    return iid, sec, cty


def test_sec_rows_to_story_layer_idempotent_and_gated(tmp_data):
    s = tmp_data
    iid, sec, cty = _maps(s)
    rows = F.build_sec(_sec_df(), iid, sec, cty)
    assert len(rows) == 3                                                # exhibit-only filing skipped
    nv = next(r for r in rows if r["issuer_id"] == "INVDA")
    assert nv["title"] == "NVIDIA Corporation 8-K: Results of operations and financial condition (2.02)"
    assert nv["theme_mask"] == 1 << T["earnings"] and nv["sentiment_x100"] == 0 and nv["sector_mask"]
    dl = next(r for r in rows if r["issuer_id"] == "IDAL")
    assert dl["sentiment_x100"] == -60 and dl["polarity_rule"].endswith("1.03")
    # GDELT has done 2026-09-15 only -> the 2003 filing merges directly (before GDELT 2.0), 2026 merges
    from app.events_v2.pipeline import update_state  # noqa: PLC0415

    update_state(lambda x: x.setdefault("days", {}).update({"2026-09-15": {"files_ok": 576}}), s)
    fn = F.filing_frame(rows, H.gdelt_days(s))
    assert fn.filter(pl.col("story_layer") == "merged").height == 3
    rep = F.merge_filings(fn, s)
    assert rep["stories"] == 3
    F.write_filing_news(fn, s)
    day = store.read_range("news_stories", date(2026, 9, 15), date(2026, 9, 15), s)
    src = H.source_ids(s)
    assert day.height == 2 and all(m == 1 << src[H.SRC_8K] for m in day["source_mask"].to_list())
    r = day.filter(pl.col("issuer_ids").list.first() == Hh.iid_of("INVDA")).row(0, named=True)
    assert r["issuer_rel"] == [100] and r["sentiment_model"] == F.RULE_NEUTRAL and r["urls"][0].endswith("nvda-8k.htm")
    assert r["first_ts"] == datetime(2026, 9, 15, 20, 5, tzinfo=UTC) and r["lang_id"] is not None
    d2 = day.filter(pl.col("issuer_ids").list.first() == Hh.iid_of("IDAL")).row(0, named=True)
    assert d2["sentiment_x100"] == -60 and d2["sentiment_model"] == F.RULE_8K
    old = store.read_range("news_stories", date(2003, 5, 1), date(2003, 5, 1), s)
    assert old.height == 1 and old["theme_mask"][0] == 1 << T["earnings"]
    # idempotent: re-running replaces the same story ids, no duplicates
    F.merge_filings(fn, s)
    assert store.read_range("news_stories", date(2026, 9, 15), date(2026, 9, 15), s).height == 2
    fnt = pl.read_parquet(list((s.data_dir / "normalized" / "filing_news").rglob("*.parquet")))
    assert fnt.height == 3 and set(fnt["source_id"]) == {"sec_8k"}


def test_pending_day_then_repair(tmp_data):
    s = tmp_data
    iid, sec, cty = _maps(s)
    rows = [r for r in F.build_sec(_sec_df(), iid, sec, cty) if r["issuer_id"] == "INVDA"]
    fn = F.filing_frame(rows, H.gdelt_days(s))                           # GDELT has not done the day yet
    assert fn["story_layer"].to_list() == ["pending_gdelt"]
    F.merge_filings(fn, s)
    F.write_filing_news(fn, s)
    assert store.read_range("news_stories", date(2026, 9, 15), date(2026, 9, 15), s).is_empty()
    from app.events_v2.pipeline import update_state  # noqa: PLC0415

    update_state(lambda x: x.setdefault("days", {}).update({"2026-09-15": {"files_ok": 576}}), s)
    rep = F.repair_filings(s)
    assert rep["pending"] == 1 and rep["stories"] == 1
    assert store.read_range("news_stories", date(2026, 9, 15), date(2026, 9, 15), s).height == 1
    fnt = pl.read_parquet(list((s.data_dir / "normalized" / "filing_news").rglob("*.parquet")))
    assert fnt["story_layer"].to_list() == ["merged"]


def test_exchange_announcements(tmp_data):
    from app.core.storage import write_table  # noqa: PLC0415

    s = tmp_data
    iid, sec, cty = _maps(s)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    base = {"source_entity_id": "x", "form_class": "current_report", "period_end": None, "fiscal_year": None,
            "fiscal_period": None, "is_amendment": False, "amends_filing_id": None, "language": None,
            "doc_path": None, "has_xbrl": False, "ingested_at": now}
    fi = pl.DataFrame([
        {**base, "filing_id": "tdnet:1", "source_id": "tdnet", "year": 2026, "issuer_id": "IBYDA", "form_type": "TANSHIN",
         "knowledge_date": date(2026, 9, 30), "knowledge_ts": datetime(2026, 9, 30, 6, tzinfo=UTC),
         "url": "https://www.release.tdnet.info/inbs/1.pdf", "items": ["2026年3月期 第2四半期決算短信", "xbrl=1"]},
        {**base, "filing_id": "hkex:2", "source_id": "hkex", "year": 2026, "issuer_id": "IBYDH", "form_type": "ANNOUNCEMENT",
         "knowledge_date": date(2026, 9, 30), "knowledge_ts": datetime(2026, 9, 30, 9, tzinfo=UTC),
         "url": "https://www1.hkexnews.hk/2.pdf", "items": ["Proposed acquisition of a battery maker", "Announcements"]},
        {**base, "filing_id": "hkex:3", "source_id": "hkex", "year": 2026, "issuer_id": "IBYDH", "form_type": "DI_CS",
         "knowledge_date": date(2026, 9, 30), "knowledge_ts": None, "url": None, "items": ["Disclosure of interests"]},
    ], infer_schema_length=None)
    write_table(fi, "filings_index", settings=s)
    meta = pl.DataFrame({"issuer_id": ["IBYDA", "IBYDH"], "name": ["BYD Company Limited"] * 2})
    rows = F.exchange_rows(s, date(2026, 1, 1), iid, sec, cty, meta)
    assert {r["filing_id"] for r in rows} == {"tdnet:1", "hkex:2"}       # DI_CS (no headline) skipped
    td = next(r for r in rows if r["source_id"] == "tdnet")
    assert td["theme_mask"] & (1 << T["earnings"]) and td["lang"] == "ja" and td["title"].startswith("BYD Company")
    hk = next(r for r in rows if r["source_id"] == "hkex")
    assert hk["theme_mask"] & (1 << T["m_and_a"]) and hk["sentiment_x100"] == 0
    st, _ = F.story_rows(F.filing_frame(rows, set()), s)
    src = H.source_ids(s)
    assert set(st["source_mask"].to_list()) == {1 << src[H.SRC_EXCH]}


def test_probe(tmp_data):
    s = tmp_data
    assert F.probe_progress({}, s)["done"] is False
    F.update_state(lambda x: x.update(backfill={"done_at": "2026-10-22T10:00:00+00:00", "since": "2001-01-01",
                                                "rows": 123, "pending_gdelt": 4}), s)
    p = F.probe_progress({}, s)
    assert p["done"] is True and p["done_units"] == 123 and p["last_run_at"].startswith("2026-10-22")
