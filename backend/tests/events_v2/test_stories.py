"""E4 story clustering: title normalisation, SimHash fingerprint, MinHash-LSH + Jaccard near-duplicates, day stories,
cross-day linking (72 h) and story-row merging."""

from __future__ import annotations

from datetime import date

import polars as pl

from app.events_v2 import stories as S
from app.events_v2 import taxonomy as tx
from app.events_v2.parse import hash64


def test_hash_title_strips_publisher_suffix():
    assert S.hash_title("Fed holds rates steady as inflation cools - Reuters") == \
        S.hash_title("Fed holds rates steady as inflation cools | CNBC")
    assert S.hash_title("Short - Title") == "short - title"           # too short to strip
    assert S.hash_title(None) == ""


def test_simhash_fingerprint():
    a = S.simhash(S.hash_title("Apple unveils new iPhone 17 at Cupertino event"))
    assert a == S.simhash(S.hash_title("Apple unveils new iPhone 17 at Cupertino event - The Verge"))
    b = S.simhash(S.hash_title("Samsung cuts memory output as prices slump"))
    assert S.hamming(a, b) > 10 and S.simhash("") == 0
    hs = S.simhash_many([S.hash_title("alpha beta gamma"), "", S.hash_title("alpha beta gamma")])
    assert hs[0] == hs[2] != 0 and hs[1] == 0
    assert -(2**63) <= a < 2**63


def test_cluster_titles_jaccard():
    titles = ["Fed holds rates steady as inflation cools",
              "Fed holds interest rates steady as inflation cools",
              "Israel strikes Gaza as ceasefire talks stall",
              "Israel strikes Gaza as truce talks stall",
              "Oil prices surge after OPEC+ cut",
              "Gold prices surge after Fed cut",
              "Fed holds rates steady as inflation cools - Reuters"]
    lab = S.cluster_titles([S.hash_title(t) for t in titles])
    assert lab[0] == lab[1] == lab[6]
    assert lab[2] == lab[3]
    assert len({lab[0], lab[2], lab[4], lab[5]}) == 4
    cjk = S.cluster_titles([S.hash_title("日本銀行が金利を据え置き"), S.hash_title("日本銀行が金利を据え置き決定")])
    assert cjk[0] == cjk[1]


def _members(rows: list[tuple]) -> pl.DataFrame:
    out = []
    for url, title, minute, dom, iss, theme, cty in rows:
        out.append({"url_hash": hash64(url), "url": url, "title": title, "minute": minute, "domain_id": dom,
                    "lang_id": 1, "source_id": 0, "tone_x100": -250, "theme_mask": theme,
                    "sector_mask": tx.sector_mask(["FINS"]) if iss else 0, "countries": [cty],
                    "issuer_ids": [iss] if iss else [], "issuer_conf": [95] if iss else [], "n_events": 1})
    return pl.DataFrame(out, schema=S.MEMBER_SCHEMA)


def test_build_day_stories_and_cross_day_link():
    d = date(2026, 10, 11)
    oil = tx.theme_ids_to_mask(["oil_gas"])
    m = _members([
        ("https://a.com/1", "Citigroup profit jumps on trading - Reuters", 60, 1, 7, 0, tx.COUNTRY_ID["US"]),
        ("https://b.com/1", "Citigroup profit jumps on trading", 75, 2, 7, 0, tx.COUNTRY_ID["US"]),
        ("https://c.com/1", "Citigroup profit jumps on strong trading", 90, 3, 0, 0, tx.COUNTRY_ID["GB"]),
        ("https://d.com/1", "Oil prices surge after OPEC+ cut", 30, 4, 0, oil, tx.GLOBAL_ID),
        ("https://e.com/1", "", 30, 5, 0, oil, tx.GLOBAL_ID),                       # untitled -> not a story member
    ])
    names = {7: ("citigroup", "c")}
    st, mem = S.build_day_stories(d, m, None, names)
    assert st.height == 2 and mem.height == 4
    citi = st.filter(pl.col("title").str.contains("Citigroup")).row(0, named=True)
    assert citi["n_articles"] == 3 and citi["n_sources"] == 3 and citi["issuer_ids"] == [7]
    assert citi["issuer_rel"] == [100]                       # named in the canonical headline
    assert citi["title"] == "Citigroup profit jumps on trading - Reuters" or citi["title"].startswith("Citigroup profit")
    assert citi["countries"][0] == tx.COUNTRY_ID["US"] and len(citi["urls"]) == 3
    assert citi["first_ts"].hour == 1 and citi["last_ts"].minute == 30
    oilst = st.filter(pl.col("title").str.contains("Oil")).row(0, named=True)
    assert oilst["countries"] == [tx.GLOBAL_ID] and tx.mask_to_theme_ids(oilst["theme_mask"]) == ["oil_gas"]
    assert set(mem.get_column("story_id").to_list()) == set(st.get_column("story_id").to_list())
    # deterministic ids (idempotent re-runs)
    st2, _ = S.build_day_stories(d, m, None, names)
    assert sorted(st2.get_column("story_id").to_list()) == sorted(st.get_column("story_id").to_list())
    # next day: same story continues under the same id
    d2 = date(2026, 10, 12)
    m2 = _members([("https://f.com/9", "Citigroup profit jumps on trading, shares rise", 10, 6, 7, 0, tx.COUNTRY_ID["US"])])
    nb = st.select("date", "story_id", "title", "n_articles")
    st3, _ = S.build_day_stories(d2, m2, nb, names)
    assert st3.get_column("story_id").to_list() == [citi["story_id"]]
    st4, _ = S.build_day_stories(d2, _members([("https://g.com/1", "Volcano erupts in Iceland", 5, 7, 0, 0,
                                                 tx.COUNTRY_ID["IS"])]), nb, names)
    assert st4.get_column("story_id").to_list()[0] not in set(st.get_column("story_id").to_list())


def test_merge_story_rows():
    d = date(2026, 10, 11)
    m = _members([("https://a.com/1", "Bank of Japan holds rates", 60, 1, 0, 2, tx.COUNTRY_ID["JP"]),
                  ("https://b.com/2", "Tesla recalls 100,000 cars", 61, 2, 9, 4, tx.COUNTRY_ID["US"])])
    st, _ = S.build_day_stories(d, m, None, {})
    same = st.with_columns(pl.lit(42, pl.Int64).alias("story_id"))
    merged = S.merge_story_rows(same)
    r = merged.row(0, named=True)
    assert merged.height == 1 and r["n_articles"] == 2 and r["theme_mask"] == 6 and r["issuer_ids"] == [9]
    assert set(r["countries"]) == {tx.COUNTRY_ID["JP"], tx.COUNTRY_ID["US"]}
    assert S.compression_ratio(10, 4) == 2.5 and S.compression_ratio(1, 0) is None
