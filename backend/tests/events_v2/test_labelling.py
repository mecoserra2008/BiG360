"""E1/E2 labelling on recorded GDELT rows (fixtures) and crafted GKG lines: issuer links + confidence rules, themes,
countries (FIPS -> ISO2, GLOBAL), sectors, event parsing, event <-> article propagation, T1 aggregates, keep rules."""

from __future__ import annotations

import io
import zipfile
from datetime import date

import polars as pl

from app.events_v2 import parse as P
from app.events_v2 import pipeline as PL
from app.events_v2 import taxonomy as tx
from tests.events_v2.helpers import fixture_bytes, gkg_line, iid_of, worker_ctx

CTX = worker_ctx()
DAY = "20261011120000"


def _gkg(lines: list[bytes], translated: bool = False) -> pl.DataFrame:
    out, st = P.parse_gkg(b"\n".join(lines) + b"\n", CTX, translated)
    assert st["rows"] == len(lines)
    return out


def test_alias_table_rules():
    a = CTX.aliases
    keys = set(a.get_column("org_norm").to_list())
    assert {"nvidia", "citigroup", "verizon communications", "delta air lines", "chipotle mexican grill"} <= keys
    byd = a.filter(pl.col("org_norm") == "byd").row(0, named=True)
    assert sorted(byd["issuer_ids"]) == [iid_of("IBYDA"), iid_of("IBYDH")]        # A+H pair linked to both
    fb = a.filter(pl.col("org_norm") == "facebook").row(0, named=True)
    assert fb["ambiguous"] and fb["conf"] < 0.5                                  # boilerplate brand needs context
    assert a.filter(pl.col("org_norm") == "nasdaq").row(0, named=True)["conf"] < 0.5


def test_links_need_context_or_business_themes():
    stocks = gkg_line(DAY, "https://x.com/markets/stocks-record", "US stocks hover near record on Wall Street",
                      "nvidia;citigroup;nasdaq;verizon communications;micron technology",
                      v1themes="ECON_STOCKMARKET;TAX_FNCACT", enhanced="ECON_STOCKMARKET,10;ECON_STOCKMARKET,90",
                      locations="1#United States#US#US##38#-97#US")
    gossip = gkg_line(DAY, "https://x.com/celebs/story", "Actor shares holiday photos with fans", "facebook;instagram",
                      v1themes="TAX_FNCACT_ACTOR", enhanced="", locations="")
    deal = gkg_line(DAY, "https://x.com/biz/starbucks-chipotle", "Starbucks explores takeover of Chipotle, FT reports",
                    "starbucks;chipotle mexican grill", v1themes="TAX_FNCACT_CEO", enhanced="",
                    locations="3#Seattle, Washington, United States#US#USWA##47.6#-122.3#1")
    meta = gkg_line(DAY, "https://x.com/tech/zuckerberg", "Zuckerberg unveils new Instagram features", "facebook",
                    v1themes="", enhanced="")
    out = _gkg([stocks, gossip, deal, meta])
    by_url = {u: r for u, r in zip(out.get_column("url").to_list(), out.iter_rows(named=True), strict=True)}
    s = by_url["https://x.com/markets/stocks-record"]
    assert {iid_of("INVDA"), iid_of("ICITI"), iid_of("IVZ"), iid_of("IMU")} <= set(s["issuer_ids"])
    assert iid_of("INDAQ") not in s["issuer_ids"]                     # ambiguous exchange name, not in the headline
    assert all(c >= 50 for c in s["issuer_conf"])
    assert "capital_markets" in tx.mask_to_theme_ids(s["theme_mask"])
    assert "FINS" in tx.sectors_of_mask(s["sector_mask"]) and "TECH" in tx.sectors_of_mask(s["sector_mask"])
    assert s["countries"] == [tx.COUNTRY_ID["US"]]
    assert "https://x.com/celebs/story" not in by_url                 # no theme, facebook boilerplate -> dropped
    d = by_url["https://x.com/biz/starbucks-chipotle"]
    assert set(d["issuer_ids"]) == {iid_of("ISBUX"), iid_of("ICMG")}
    assert "m_and_a" in tx.mask_to_theme_ids(d["theme_mask"])          # English headline rule
    assert d["countries"] == [tx.COUNTRY_ID["US"]]
    m = by_url["https://x.com/tech/zuckerberg"]                       # brand cue in the headline confirms Meta
    assert m["issuer_ids"] == [iid_of("IMETA")] and m["issuer_conf"][0] >= 90


def test_translated_rows_need_business_context_and_more_mentions():
    t1 = gkg_line(DAY, "https://y.de/a", "Kinder spielen im Park", "nvidia", v1themes="SOC_GENERAL",
                  enhanced="ARMEDCONFLICT,5;ARMEDCONFLICT,40", translation="srclc:deu;eng:GT-DEU 1.0")
    t2 = gkg_line(DAY, "https://y.de/b", "Nvidia-Aktie steigt", "nvidia corporation", v1themes="ECON_STOCKMARKET",
                  enhanced="ECON_STOCKMARKET,3", translation="srclc:deu;eng:GT-DEU 1.0")
    out = _gkg([t1, t2], translated=True)
    urls = out.get_column("url").to_list()
    assert "https://y.de/a" not in urls                    # 2 broad mentions < 3 for translingual, link lacks context
    r = out.filter(pl.col("url") == "https://y.de/b").row(0, named=True)
    assert r["issuer_ids"] == [iid_of("INVDA")] and r["lang"] == "de" and r["translated"]


def test_countries_fallback_and_global():
    no_loc = gkg_line(DAY, "https://x.com/a", "Citigroup quarterly profit beats estimates", "citigroup",
                      v1themes="ECON_EARNINGSREPORT", enhanced="ECON_EARNINGSREPORT,1")
    many = gkg_line(DAY, "https://x.com/b", "Oil prices jump as OPEC+ cuts output", "",
                    v1themes="ENV_OIL", enhanced="ENV_OIL,1;ENV_OIL,50;ECON_OILPRICE,3",
                    locations=";".join(f"1#{n}#{f}#{f}##0#0#{f}" for n, f in
                                       (("Saudi Arabia", "SA"), ("Russia", "RS"), ("United States", "US"),
                                        ("Iran", "IR"), ("Iraq", "IZ"))))
    nothing = gkg_line(DAY, "https://x.com/c", "Inflation cools worldwide", "", v1themes="ECON_INFLATION",
                       enhanced="ECON_INFLATION,2")
    out = _gkg([no_loc, many, nothing])
    rows = {r["url"]: r for r in out.iter_rows(named=True)}
    assert rows["https://x.com/a"]["countries"] == [tx.COUNTRY_ID["US"]]          # issuer country fallback
    many_c = [tx.COUNTRIES[i] for i in rows["https://x.com/b"]["countries"]]
    assert many_c[-1] == "GLOBAL" and len(many_c) == 4 and set(many_c[:3]) <= {"SA", "RU", "US", "IR", "IQ"}
    assert rows["https://x.com/c"]["countries"] == [tx.GLOBAL_ID]
    assert "ENRG" in tx.sectors_of_mask(rows["https://x.com/b"]["sector_mask"])


def test_recorded_gkg_rows_invariants():
    out, st = P.parse_gkg(fixture_bytes("gkg"), CTX, False)
    assert st["rows"] >= 30 and 0 < st["kept"] <= st["prefiltered"] <= st["rows"]
    assert out.filter((pl.col("theme_mask") == 0) & (pl.col("issuer_ids").list.len() == 0)).is_empty()
    assert out.get_column("url_hash").n_unique() == out.height
    assert out.select(pl.col("countries").list.len().min()).item() >= 1
    assert all(0 <= c < len(tx.COUNTRIES) for cs in out.get_column("countries").to_list() for c in cs)
    assert not out.filter(pl.col("issuer_ids").list.contains(iid_of("IMETA"))).height   # 'facebook' boilerplate
    stocks = out.filter(pl.col("title").str.contains("US stocks hover"))
    assert stocks.height == 1 and iid_of("INVDA") in stocks.row(0, named=True)["issuer_ids"]
    assert out.schema["tone_x100"] == pl.Int16 and out.schema["theme_mask"] == pl.Int64


def test_recorded_events_and_mentions():
    ev = P.parse_events(fixture_bytes("export"))
    assert ev.height >= 100 and ev.get_column("event_id").n_unique() == ev.height
    raw = fixture_bytes("export").split(b"\n")[0].split(b"\t")
    r0 = ev.filter(pl.col("event_id") == int(raw[0])).row(0, named=True)
    assert r0["cameo"] == int(raw[26]) and r0["quad"] == int(raw[29])
    assert r0["goldstein_x10"] == round(float(raw[30]) * 10)
    assert r0["n_mentions"] == int(raw[31]) and r0["tone_x100"] == round(float(raw[34]) * 100)
    fips = raw[53].decode()
    exp = tx.COUNTRY_ID.get(tx.fips_to_iso2(fips) or "", None)
    if exp is not None:
        assert r0["country"] == exp
    assert ev.select(pl.col("countries").list.len().min()).item() >= 1
    men = P.parse_mentions(fixture_bytes("mentions"))
    assert men.height > 0 and set(men.columns) == {"event_id", "url_hash"}


def test_parse_file_roundtrip_zip_and_legacy():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.export.CSV", fixture_bytes("export"))
    kind, res, st, old = P.parse_file("export", buf.getvalue(), legacy=True)
    assert kind == "export" and res.height == st["rows"] and st["raw_bytes"] > 0
    rows, agg = old                                   # legacy gdelt_events parser ran on the same bytes
    assert rows.height > 0 and agg.height > 0


def test_event_article_propagation_aggregates_and_keep():
    arts = _gkg([gkg_line(DAY, "https://x.com/markets/stocks-record", "Starbucks explores takeover of Chipotle",
                          "starbucks;chipotle mexican grill", v1themes="ECON_STOCKMARKET",
                          enhanced="ECON_STOCKMARKET,1", locations="1#United States#US#US##38#-97#US")])
    uh = arts.get_column("url_hash")[0]
    ev = pl.DataFrame({
        "event_id": [1, 2, 3, 4], "date": [date(2026, 10, 11)] * 4, "event_date": [date(2026, 10, 11)] * 4,
        "cameo": [143, 42, 190, 42], "cameo_root": [14, 4, 19, 4], "quad": [3, 1, 4, 1],
        "goldstein_x10": [-65, 19, -100, 19], "tone_x100": [-300, 100, -800, 50], "n_mentions": [10, 2, 30, 1],
        "n_sources": [1, 1, 3, 1], "n_articles": [5, 1, 10, 1],
        "country": [tx.COUNTRY_ID["FR"], tx.COUNTRY_ID["US"], tx.COUNTRY_ID["UA"], tx.COUNTRY_ID["DE"]],
        "countries": [[tx.COUNTRY_ID["FR"]], [tx.COUNTRY_ID["US"]], [tx.COUNTRY_ID["UA"]], [tx.COUNTRY_ID["DE"]]],
        "lat_x100": [1, 2, 3, 4], "lon_x100": [1, 2, 3, 4], "url_hash": [11, uh, 33, 44],
        "url": ["a", "b", "c", "d"], "domain": ["a.com"] * 4, "theme_mask": [0, 0, 0, 0], "translated": [False] * 4,
    }).with_columns(pl.col("countries").cast(pl.List(pl.Int16)), *[pl.col(c).cast(t) for c, t in P._EMPTY_EV.items()
                                                                  if c not in ("countries",)])
    ev = ev.with_columns(pl.Series("theme_mask", [tx.theme_ids_to_mask(["strikes", "protest_unrest"]), 0,
                                                  tx.theme_ids_to_mask(["conflict_military"]), 0], pl.Int64))
    men = pl.DataFrame({"event_id": [4], "url_hash": [uh]})
    sec = {iid_of("ISBUX"): tx.sector_mask(["CDIS"]), iid_of("ICMG"): tx.sector_mask(["CDIS"])}
    ev2, n_ev = PL.link_events(ev, men, arts, sec)
    r = {x["event_id"]: x for x in ev2.iter_rows(named=True)}
    assert set(r[2]["issuer_ids"]) == {iid_of("ISBUX"), iid_of("ICMG")}      # SOURCEURL article
    assert set(r[4]["issuer_ids"]) == {iid_of("ISBUX"), iid_of("ICMG")}      # via Mentions
    assert "capital_markets" in tx.mask_to_theme_ids(r[2]["theme_mask"])      # source-article theme propagated
    assert r[2]["cameo_mask"] == 0 and "CDIS" in tx.sectors_of_mask(r[2]["sector_mask"])
    assert n_ev.filter(pl.col("url_hash") == uh).get_column("n_events").item() == 2
    kept = ev2.filter(PL.keep_event_expr()).get_column("event_id").sort().to_list()
    assert kept == [1, 2, 3, 4]   # 1: strike >= 3 articles, 3: conflict, 2/4: issuer-linked
    arts2 = arts.join(n_ev, on="url_hash", how="left")
    agg = PL.day_aggregates(date(2026, 10, 11), ev2, arts2)
    c = agg.filter((pl.col("label_type") == "country") & (pl.col("theme") == -1))
    fr = c.filter(pl.col("label_id") == tx.COUNTRY_ID["FR"]).row(0, named=True)
    assert fr["ev_n"] == 1 and fr["ev_conflict_n"] == 1 and fr["art_n"] == 0
    us = c.filter(pl.col("label_id") == tx.COUNTRY_ID["US"]).row(0, named=True)
    assert us["ev_n"] == 1 and us["art_n"] == 1
    iss = agg.filter(pl.col("label_type") == "issuer")
    assert set(iss.get_column("label_id").to_list()) == {iid_of("ISBUX"), iid_of("ICMG")}
    assert iss.filter(pl.col("label_id") == iid_of("ISBUX")).row(0, named=True)["ev_n"] == 2
    ua = agg.filter((pl.col("label_type") == "country") & (pl.col("label_id") == tx.COUNTRY_ID["UA"])
                    & (pl.col("theme") == tx.THEME_BIT["conflict_military"]))
    assert ua.height == 1
