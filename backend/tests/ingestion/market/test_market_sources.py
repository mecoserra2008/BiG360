"""ingest-market: GDELT parsing, FX, FRED/ALFRED, global macro parsers, news (recorded fixtures, no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import pytest

from app.core.schema import conform

FIX = Path(__file__).parent / "fixtures"


# ------------------------------------------------------------------------------------------------ GDELT


def test_fips_and_iso3_mapping():
    from app.ingestion.gdelt_events import FIPS_TO_ISO2, ISO3_TO_ISO2

    assert FIPS_TO_ISO2["UK"] == "GB" and FIPS_TO_ISO2["GM"] == "DE" and FIPS_TO_ISO2["CH"] == "CN"
    assert FIPS_TO_ISO2["JA"] == "JP" and FIPS_TO_ISO2["KS"] == "KR" and FIPS_TO_ISO2["PO"] == "PT"
    assert ISO3_TO_ISO2["USA"] == "US" and ISO3_TO_ISO2["BRA"] == "BR"


def test_slot_urls_and_lastupdate():
    from app.ingestion.gdelt_events import parse_lastupdate, slot_times, slot_url

    ts = slot_times(date(2026, 10, 9))
    assert len(ts) == 96 and ts[1].minute == 15
    assert slot_url(ts[0], "export").endswith("/20261009000000.export.CSV.zip")
    assert slot_url(ts[0], "gkg").endswith("/20261009000000.gkg.csv.zip")
    assert slot_url(ts[0], "translation.gkg").endswith("/20261009000000.translation.gkg.csv.zip")
    txt = ("123 abc http://data.gdeltproject.org/gdeltv2/20261010194500.export.CSV.zip\n"
           "456 def http://data.gdeltproject.org/gdeltv2/20261010194500.gkg.csv.zip\n")
    assert parse_lastupdate(txt) == datetime(2026, 10, 10, 19, 45, tzinfo=UTC)


def test_parse_events_filter_and_aggregate():
    from app.ingestion.gdelt_events import combine_aggregates, parse_events

    raw = (FIX / "gdelt_export_sample.tsv").read_bytes()
    n_lines = len(raw.decode().strip().splitlines())
    rows, agg = parse_events(raw)
    assert agg.get_column("events").sum() == n_lines  # aggregates count every event
    assert 0 < rows.height <= n_lines
    ok = (rows.get_column("num_articles") >= 3) | (rows.get_column("goldstein").abs() >= 5)
    assert ok.all()
    assert rows.get_column("event_root_code").str.len_chars().eq(2).all()
    cc = rows.get_column("action_country").drop_nulls()
    assert cc.str.len_chars().eq(2).all()
    rows = conform(rows.with_columns(pl.lit(datetime.now(UTC)).alias("ingested_at")), "gdelt_events")
    assert rows.get_column("year_month").unique().to_list() == ["2026-10"]
    assert set(rows.get_column("event_category").unique().to_list()) <= {
        "conflict", "protest", "sanctions_trade", "diplomacy", "economy", "disaster", "legal_regulatory", "health", "other"}
    ca = combine_aggregates([agg, agg])
    assert ca.get_column("events").sum() == 2 * n_lines
    assert {"goldstein_mean", "avg_tone_mean", "mentions", "cameo_root", "country"} <= set(ca.columns)


def _seed_dict():
    from app.ingestion.gdelt_gkg import SEED_ALIASES, norm_org_expr
    from app.normalize.prices import SEED_GLOBAL, SEED_US

    rows = [{"org": a, "issuer_id": None, "ticker": t, "display": n}
            for t, n in SEED_US + SEED_GLOBAL for a in [n, *SEED_ALIASES.get(t, [])]]
    df = pl.DataFrame(rows, schema={"org": pl.Utf8, "issuer_id": pl.Utf8, "ticker": pl.Utf8, "display": pl.Utf8})
    return df.with_columns(norm_org_expr(pl.col("org")).alias("org_norm")).filter(
        pl.col("org_norm").str.len_chars() >= 4).unique(subset=["org_norm"]).select("org_norm", "issuer_id", "ticker", "display")


def test_org_name_normalisation():
    from app.ingestion.gdelt_gkg import norm_org

    assert norm_org("Microsoft Corp.") == "microsoft"
    assert norm_org("The Coca-Cola Company") == "coca cola"
    assert norm_org("Toyota Motor Corporation") == "toyota motor"
    assert norm_org("Petrobras S.A.") == "petrobras"


def test_parse_gkg_matches_orgs_and_builds_news(tmp_data):
    from app.ingestion.gdelt_gkg import mentions_to_news, parse_gkg, write_news

    raw = (FIX / "gdelt_gkg_sample.tsv").read_bytes()
    m = parse_gkg(raw, _seed_dict())
    assert m.height >= 4
    assert set(m.get_column("display").unique().to_list()) & {"Microsoft", "Nvidia", "Apple", "Toyota Motor", "Petrobras"}
    assert m.get_column("tone").dtype == pl.Float64 and m.get_column("tone").null_count() == 0
    assert (m.get_column("language") == "en").all()
    for cs in m.get_column("countries").to_list():
        assert all(len(c) == 2 for c in cs)
    news = mentions_to_news(m)
    assert news.height >= 1
    assert news.get_column("item_id").str.len_chars().eq(16).all()
    news = conform(news, "news_items")
    assert news.get_column("source_id").unique().to_list() == ["gdelt_gkg"]
    n1 = write_news(news, tmp_data)
    assert n1 == news.height
    assert write_news(news, tmp_data) == 0  # idempotent; existing items are never rewritten


def test_url_normalisation_dedupes():
    from app.ingestion.gdelt_gkg import item_id, normalize_url

    a = "https://www.fool.com/investing/2026/10/09/apple-services/?utm_source=polygon"
    b = "https://fool.com/investing/2026/10/09/apple-services"
    assert normalize_url(a) == normalize_url(b) and item_id(a) == item_id(b)
    assert item_id("https://x.com/a?id=1") != item_id("https://x.com/a?id=2")


# ------------------------------------------------------------------------------------------------ FX


def test_ecb_parse_and_usd_cross():
    from app.ingestion.fx import ecb_to_usd, parse_ecb_hist_csv

    long = parse_ecb_hist_csv((FIX / "ecb_hist_sample.csv").read_text())
    assert "CYP" in long.get_column("currency").to_list()  # legacy currency kept while quoted
    fx = ecb_to_usd(long)
    r = {(x["date"], x["currency"]): x["usd_per_unit"] for x in fx.iter_rows(named=True)}
    d = date(2026, 10, 9)
    assert r[(d, "EUR")] == pytest.approx(1.1206)
    assert r[(d, "USD")] == 1.0
    assert r[(d, "JPY")] == pytest.approx(1.1206 / 177.34)
    assert r[(d, "KRW")] == pytest.approx(1.1206 / 1503.27)
    assert (d, "CYP") not in r  # N/A dropped


def test_combine_precedence_and_fill():
    from app.ingestion.fx import combine_and_fill, ecb_to_usd, parse_ecb_hist_csv, parse_fred_units_per_usd

    ecb = ecb_to_usd(parse_ecb_hist_csv((FIX / "ecb_hist_sample.csv").read_text())).filter(pl.col("date") > date(2020, 1, 1))
    fred = parse_fred_units_per_usd({"observations": [{"date": "2026-10-09", "value": "150.0"},
                                                      {"date": "2026-10-07", "value": "151.0"}]}, "JPY")
    out = conform(combine_and_fill([ecb, fred], until=date(2026, 10, 20)), "fx_rates")
    jpy = {x["date"]: x for x in out.filter(pl.col("currency") == "JPY").iter_rows(named=True)}
    assert jpy[date(2026, 10, 9)]["source_id"] == "ecb"  # ECB beats FRED on the same day
    assert jpy[date(2026, 10, 7)]["source_id"] == "fred"  # FRED fills a day ECB lacks
    assert jpy[date(2026, 10, 10)]["source_id"] == "filled"
    assert jpy[date(2026, 10, 14)]["source_id"] == "filled"  # 5-day max forward fill
    assert date(2026, 10, 15) not in jpy
    assert out.filter(pl.col("currency") == "USD").get_column("usd_per_unit").unique().to_list() == [1.0]


def test_gap_filler_parsers():
    from app.ingestion.fx import parse_bcra, parse_bcrp, parse_mindicador, parse_trm

    pen = parse_bcrp({"periods": [{"name": "01.Set.26", "values": ["3.368"]}, {"name": "02.Dic.25", "values": ["n.d."]}]})
    assert pen.row(0, named=True)["date"] == date(2026, 9, 1) and pen.height == 1
    assert pen.row(0, named=True)["usd_per_unit"] == pytest.approx(1 / 3.368)
    cop = parse_trm([{"valor": "3194.44", "vigenciadesde": "2026-10-10T00:00:00.000",
                      "vigenciahasta": "2026-10-13T00:00:00.000"}], until=date(2026, 10, 20))
    assert cop.height == 4 and cop.get_column("source_id").unique().to_list() == ["central_bank"]
    ars = parse_bcra({"results": [{"fecha": "2026-10-09", "detalle": [{"codigoMoneda": "USD", "tipoCotizacion": 1517.0}]}]})
    assert ars.row(0, named=True)["usd_per_unit"] == pytest.approx(1 / 1517.0)
    clp = parse_mindicador({"serie": [{"fecha": "2026-10-09T03:00:00.000Z", "valor": 981.61}]})
    assert clp.row(0, named=True)["date"] == date(2026, 10, 9)


# ------------------------------------------------------------------------------------------------ FRED / macro


def test_fred_vintages_pit_and_archive_artefact():
    from app.ingestion.fred import observations_rows

    obs = json.loads((FIX / "fred_obs_vintage.json").read_text())["observations"]
    df = conform(observations_rows("fred:GDPC1", obs, "Q", 30, vintages=True), "macro_observations")
    old = df.filter(pl.col("date") == date(1960, 1, 1)).sort("knowledge_date")
    # first ALFRED vintage of a 1960 observation is the archive start -> imputed release date, flagged estimated
    assert old.row(0, named=True)["knowledge_date"] == date(1960, 4, 30)
    assert old.row(0, named=True)["knowledge_date_estimated"] is True
    assert old.row(1, named=True)["knowledge_date"] == date(1992, 1, 29)
    assert old.row(1, named=True)["knowledge_date_estimated"] is False
    new = df.filter(pl.col("date") == date(2026, 4, 1)).sort("knowledge_date")
    assert new.get_column("knowledge_date").to_list() == [date(2026, 7, 30), date(2026, 8, 28)]
    assert not new.get_column("knowledge_date_estimated").any()
    # PIT as-of: value known on 2026-08-01 is the first estimate
    from app.normalize.pit import asof

    v = asof(df, date(2026, 8, 1), ["series_id", "date"]).filter(pl.col("date") == date(2026, 4, 1))
    assert v.item(0, "value") == pytest.approx(23500.1)


def test_fred_non_vintage_estimated_knowledge_date():
    from app.ingestion.fred import observations_rows, period_end

    obs = json.loads((FIX / "fred_obs_daily.json").read_text())["observations"]
    df = observations_rows("fred:DGS10", obs, "D", 1, vintages=False, today=date(2026, 10, 10))
    r = {x["date"]: x for x in df.iter_rows(named=True)}
    assert r[date(2026, 10, 7)]["knowledge_date"] == date(2026, 10, 8)
    assert r[date(2026, 10, 9)]["knowledge_date"] == date(2026, 10, 10)  # capped at retrieval date
    assert r[date(2026, 10, 8)]["value"] is None
    assert df.get_column("knowledge_date_estimated").all()
    assert period_end(date(2026, 2, 1), "M") == date(2026, 2, 28)
    assert period_end(date(2026, 10, 1), "Q") == date(2026, 12, 31)


def test_oas_archive_is_append_only(tmp_path):
    from app.ingestion.fred import merge_oas_archive

    p = tmp_path / "BAMLH0A0HYM2.parquet"
    first = pl.DataFrame({"date": [date(2023, 10, 2), date(2023, 10, 3)], "value": [4.1, 4.2]})
    merge_oas_archive(p, first)
    # FRED later only returns the last 3 years (oldest date gone) and revises one value
    later = pl.DataFrame({"date": [date(2023, 10, 3), date(2023, 10, 4)], "value": [4.25, 4.3]})
    out, st = merge_oas_archive(p, later)
    vals = dict(zip(out.get_column("date").to_list(), out.get_column("value").to_list(), strict=True))
    assert vals == {date(2023, 10, 2): 4.1, date(2023, 10, 3): 4.2, date(2023, 10, 4): 4.3}
    assert st["revised_rows"] == 1 and st["only_in_archive"] == 1
    assert out.filter(pl.col("date") == date(2023, 10, 3)).item(0, "last_value") == 4.25


def test_fred_catalog_excludes_dead_series():
    from app.ingestion.fred import CATALOG, CATALOG_BY_ID

    dead = {"NAPM", "NAPMNOI", "GOLDAMGBD228NLBM", "TEDRATE", "USSLIND", "OECDLOLITOAASTSAM", "IRLTLT01BRM156N"}
    assert not dead & set(CATALOG_BY_ID)
    assert len(CATALOG) >= 120
    assert {s.native for s in CATALOG if s.vintages} >= {"GDPC1", "PAYEMS", "CPIAUCSL"}
    assert all(s.native in CATALOG_BY_ID for s in CATALOG if s.native.startswith("BAML"))


def test_macro_global_parsers():
    from app.ingestion.macro_global import parse_bcb, parse_boe_csv, parse_jsonstat, parse_period, parse_sdmx_csv

    assert parse_period("2026-Q3") == date(2026, 7, 1)
    assert parse_period("2026-M08") == date(2026, 8, 1)
    assert parse_period("2026-08") == date(2026, 8, 1)
    assert parse_period("20261007") == date(2026, 10, 7)
    js = {"size": [1, 1, 2], "value": {"1": 6.4}, "dimension": {"time": {"category": {"index": {"2026-08": 0, "2026-09": 1}}}}}
    assert parse_jsonstat(js) == [(date(2026, 8, 1), None), (date(2026, 9, 1), 6.4)]
    assert parse_jsonstat({**js, "size": [0, 1, 2]}) == []
    assert parse_bcb([{"data": "17/09/2026", "valor": "13.75"}]) == [(date(2026, 9, 17), 13.75)]
    assert parse_boe_csv("DATE,IUDBEDR\n01 Sep 2026,3.75\n") == [(date(2026, 9, 1), 3.75)]
    csv_text = ('COUNTRY,TIME_PERIOD,OBS_VALUE,FULL_DESCRIPTION\nJPN,2026-M05,1.5,"multi\nline, text"\n'
                'CHN,2026-M05,0.8,x\n')
    out = parse_sdmx_csv(csv_text, ("COUNTRY",))
    assert out[("JPN",)] == [(date(2026, 5, 1), 1.5)] and out[("CHN",)] == [(date(2026, 5, 1), 0.8)]


# ------------------------------------------------------------------------------------------------ news


def test_polygon_news_rows(tmp_data):
    from app.ingestion.gdelt_gkg import write_news
    from app.ingestion.news import polygon_news_rows

    res = json.loads((FIX / "polygon_news_sample.json").read_text())["results"]
    df = conform(polygon_news_rows(res), "news_items")
    assert df.height == 2  # same article URL (utm/www variants) -> one item
    a1 = df.filter(pl.col("title") == "Apple's services keep growing").row(0, named=True)
    assert a1["knowledge_date"] == date(2026, 10, 9)  # 01:30Z = previous evening in New York
    assert a1["tickers"] == ["AAPL", "MSFT"] and a1["issuer_ids"] == []
    assert json.loads(a1["provider_sentiment"])[0]["sentiment"] == "positive"
    assert write_news(df, tmp_data) == 2


def test_doc_artlist_rows_dedupes_syndication():
    from app.ingestion.news import doc_artlist_rows

    payload = {"articles": [
        {"url": "https://a.com/x", "title": "Nvidia beats", "seendate": "20261010T064500Z", "domain": "a.com",
         "language": "English"},
        {"url": "https://b.com/y", "title": "NVIDIA  beats", "seendate": "20261010T070000Z", "domain": "b.com",
         "language": "English"},
        {"url": "https://c.com/z", "title": "Nvidia cae", "seendate": "20261010T070000Z", "domain": "c.com",
         "language": "Spanish"}]}
    df = conform(doc_artlist_rows("Nvidia", payload, None, "NVDA"), "news_items")
    assert df.height == 2 and set(df.get_column("language").to_list()) == {"en", "es"}
    assert df.get_column("entities").to_list()[0] == ["Nvidia"]


def test_rss_rows():
    from app.ingestion.news import rss_rows

    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Federal Reserve issues FOMC statement</title><link>https://www.federalreserve.gov/x.htm</link>
    <pubDate>Wed, 17 Sep 2026 18:00:00 GMT</pubDate><description>The Committee decided...</description></item>
    </channel></rss>"""
    df = conform(rss_rows("rss_fed", xml, ["US"]), "news_items")
    r = df.row(0, named=True)
    assert r["source_id"] == "rss_fed" and r["knowledge_date"] == date(2026, 9, 17) and r["issuer_ids"] == []


# ------------------------------------------------------------------------------------------------ KRX / BaoStock / B3


def test_krx_and_baostock_rows():
    from app.ingestion.baostock_prices import bs_rows, yf_to_bs
    from app.ingestion.krx_prices import krx_rows

    k = conform(krx_rows({"OutBlock_1": [{"ISU_CD": "005930", "TDD_CLSPRC": "61,000", "TDD_OPNPRC": "60,500",
                                          "TDD_HGPRC": "61,200", "TDD_LWPRC": "60,100", "ACC_TRDVOL": "12,345,678"}]},
                         ".KS", date(2026, 10, 8)), "prices_staging")
    assert k.row(0, named=True)["symbol"] == "005930.KS" and k.row(0, named=True)["close"] == 61000.0
    assert yf_to_bs("600519.SS") == "sh.600519" and yf_to_bs("000858.SZ") == "sz.000858" and yf_to_bs("AAPL") is None
    b = conform(bs_rows("600519.SS", [["2026-10-09", "sh.600519", "1500", "1510", "1490", "1505", "100", "150500", "3", "1"],
                                      ["2026-10-08", "sh.600519", "1500", "1500", "1500", "1500", "0", "0", "3", "0"]],
                        ["date", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag", "tradestatus"]),
                "prices_staging")
    assert b.height == 1 and b.row(0, named=True)["vwap"] == pytest.approx(1505.0)


def test_cotahist_parser():
    from app.ingestion.b3_cotahist import parse_cotahist

    def rec(cod, tpmerc, bdi, close_cents, fat="0000001"):
        line = [" "] * 245
        def put(a, b, v):
            v = str(v)
            line[a - 1:b] = list(v.rjust(b - a + 1, "0") if v.isdigit() else v.ljust(b - a + 1))
        put(1, 2, "01"); put(3, 10, "20261009"); put(11, 12, bdi); put(13, 24, cod); put(25, 27, tpmerc)
        for a, b in ((57, 69), (70, 82), (83, 95), (96, 108), (109, 121)):
            put(a, b, close_cents)
        put(148, 152, "00123"); put(153, 170, "000000000000100000"); put(211, 217, fat)
        return "".join(line)
    text = "\n".join([("00COTAHIST" + " " * 235), rec("PETR4", "010", "02", "3712"), rec("PETR4T", "020", "02", "3700"),
                      rec("ABCB11", "010", "12", "10050", "0001000"), ("99COTAHIST" + " " * 235)])
    df = conform(parse_cotahist(text), "prices_staging")
    r = {x["symbol"]: x for x in df.iter_rows(named=True)}
    assert set(r) == {"PETR4.SA", "ABCB11.SA"}
    assert r["PETR4.SA"]["close"] == pytest.approx(37.12) and r["PETR4.SA"]["trades"] == 123
    assert r["ABCB11.SA"]["close"] == pytest.approx(0.1005)  # FATCOT 1000


def test_cotahist_recorded_daily_file():
    from app.ingestion.b3_cotahist import parse_cotahist

    text = (FIX / "cotahist_sample.txt").read_text(encoding="latin-1")
    df = conform(parse_cotahist(text), "prices_staging")
    syms = set(df.get_column("symbol").to_list())
    assert {"PETR4.SA", "VALE3.SA", "ITUB4.SA"} <= syms and len(syms) == 3  # options (TPMERC != 010) dropped
    vale = df.filter(pl.col("symbol") == "VALE3.SA").row(0, named=True)
    assert vale["date"] == date(2026, 10, 9) and vale["open"] == pytest.approx(68.16)
    assert vale["low"] <= vale["close"] <= vale["high"]
