"""app.normalize.identity: id hashing, normalization, registry (assign once / merge), offline build from fixture
sources (ADR linkage, A/H + ON/PN groups, multi-class US), lookups, SCD history, successions, universe."""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from app.core.storage import read_table
from app.normalize import identity as I
from app.normalize.identity_universe import is_valid_isin, kr_common_isin


# ------------------------------------------------------------------------------------------------ pure functions

def test_id_hashing_is_deterministic_and_well_formed():
    a = I.make_issuer_id("cik:0000320193")
    assert a == I.make_issuer_id("cik:0000320193") and a.startswith("I") and len(a) == 13
    assert set(a[1:]) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    s = I.make_security_id(a, "common")
    assert s.startswith("S") and I.make_listing_id(s, "xnas") == I.make_listing_id(s, "XNAS")


@pytest.mark.parametrize("t,v,exp", [
    ("cik", "320193", "0000320193"), ("ticker_mic", "brk.b@xnys", "BRK.B@XNYS"), ("dart_corp_code", "126380", "00126380"),
    ("kr_stock_code", "5930", "005930"), ("jp_sec_code", "72030", "7203"), ("hk_code", "700", "00700"),
    ("cvm_code", "009512", "9512"), ("cnpj", "33.000.167/0001-01", "33000167000101"), ("cn_code", "600519.SS", "SSE:600519"),
    ("cn_code", "sz000858", "SZSE:000858"), ("cn_code", "300750", "SZSE:300750"), ("isin", " us0378331005 ", "US0378331005"),
    ("name_country", "Apple Inc.@us", "APPLE@US"), ("cik", None, None), ("cik", "nan", None),
])
def test_normalize_id(t, v, exp):
    assert I.normalize_id(t, v) == exp


def test_normalize_name():
    assert I.normalize_name("Apple Inc.") == "APPLE"
    assert I.normalize_name("Toyota Motor Corporation") == "TOYOTA MOTOR"
    assert I.normalize_name("Petróleo Brasileiro S.A.") == "PETROLEO BRASILEIRO"
    assert I.normalize_name("The Holding Company") == "THE HOLDING COMPANY"
    assert I.normalize_name("") == "" and I.normalize_name(None) == ""


def test_isin_helpers():
    assert is_valid_isin("US0378331005") and is_valid_isin("KR7005930003") and not is_valid_isin("US0378331006")
    assert kr_common_isin("005930") == "KR7005930003" and kr_common_isin("005935") is None


def test_fuzzy_match_same_country_and_ambiguity():
    cands = pl.DataFrame({"issuer_id": ["I1", "I2", "I3", "I4"], "name": ["Galp Energia SGPS", "Galp Energia", "EDP SA", "Other Co"],
                          "country": ["PT", "ES", "PT", "PT"]})
    names = pl.DataFrame({"key": ["a", "b", "c"], "name": ["GALP ENERGIA, S.A.", "EDP - Energias", "EDP S.A."], "country": ["PT", "PT", "DE"]})
    out = I.fuzzy_match(names, cands)
    m = dict(zip(out.get_column("key"), out.get_column("issuer_id"), strict=True))
    assert m.get("a") == "I1" and "c" not in m and set(out.get_column("confidence").to_list()) == {0.85}


# ------------------------------------------------------------------------------------------------ fixture build

def _write_seeds(settings, rows: list[str], successions: str | None = None) -> None:
    d = settings.data_dir / "reference" / "seeds"
    d.mkdir(parents=True, exist_ok=True)
    (d / "mvp_global.csv").write_text("ticker,mic,yf_ticker,isin,name,country,region,adr_ticker,issuer_group,share_class,notes\n"
                                      + "\n".join(rows) + "\n", encoding="utf-8")
    if successions:
        (d / "issuer_successions.csv").write_text("predecessor,successor,effective_date,reason,note\n" + successions + "\n",
                                                  encoding="utf-8")


SEEDS = [
    "7203,XTKS,7203.T,,Toyota Motor,JP,JP,TM,,,",
    "PETR4,BVMF,PETR4.SA,,Petrobras (PN),BR,LATAM,PBR-A,petrobras,PN,",
    "PETR3,BVMF,PETR3.SA,,Petrobras (ON),BR,LATAM,PBR,petrobras,ON,",
    "0700,XHKG,0700.HK,,Tencent Holdings,CN,HK,,,,",
    "601318,XSHG,601318.SS,,Ping An (A),CN,CN,,pingan,A,",
    "2318,XHKG,2318.HK,,Ping An (H),CN,HK,,pingan,H,",
    "005930,XKRX,005930.KS,,Samsung Electronics,KR,KR,,,,",
    "BAP,XNYS,BAP,,Credicorp,PE,LATAM,,,,",
]


def _sources(aapl_ticker: str = "AAPL") -> dict[str, pl.DataFrame]:
    sec = pl.DataFrame({
        "cik": ["0000320193", "0001652044", "0001652044", "0001094517", "0001119639", "0001119639", "0001001085", "0000034088"],
        "name": ["Apple Inc.", "Alphabet Inc.", "Alphabet Inc.", "TOYOTA MOTOR CORP/", "PETROBRAS - PETROLEO BRASILEIRO SA",
                 "PETROBRAS - PETROLEO BRASILEIRO SA", "CREDICORP LTD", "EXXON MOBIL CORP"],
        "ticker": [aapl_ticker, "GOOGL", "GOOG", "TM", "PBR", "PBR-A", "BAP", "XOMOLD"],
        "exchange": ["Nasdaq", "Nasdaq", "Nasdaq", "NYSE", "NYSE", "NYSE", "NYSE", "NYSE"],
        "mic": ["XNAS", "XNAS", "XNAS", "XNYS", "XNYS", "XNYS", "XNYS", "XNYS"],
        "sec_rank": [1, 2, 3, 50, 60, 61, 300, 9],
    }).with_columns(pl.col("sec_rank").cast(pl.Int32))
    poly = pl.DataFrame({
        "ticker": [aapl_ticker, "GOOGL", "GOOG", "TM", "PBR", "PBR.A", "BAP", "XOMOLD"],
        "name": ["Apple Inc.", "Alphabet Inc. Class A", "Alphabet Inc. Class C", "Toyota ADR", "Petrobras ADR", "Petrobras ADR pref",
                 "Credicorp", "Exxon old"],
        "primary_exchange": ["XNAS", "XNAS", "XNAS", "XNYS", "XNYS", "XNYS", "XNYS", "XNYS"],
        "type": ["CS", "CS", "CS", "ADRC", "ADRC", "ADRC", "CS", "CS"],
        "cik": ["0000320193", "0001652044", "0001652044", "0001094517", "0001119639", "0001119639", "0001001085", "0000034088"],
        "composite_figi": ["BBG000B9XRY4", "BBG009S39JX6", "BBG009S3NB30", None, None, None, None, None],
        "share_class_figi": ["BBG001S5N8V8", "BBG009S39JY5", "BBG009S3NB21", None, None, None, None, None],
        "currency_name": ["usd"] * 8,
    })
    grouped = pl.DataFrame({"ticker": [aapl_ticker, "GOOGL", "GOOG", "TM", "PBR", "PBR.A", "BAP", "XOMOLD"],
                            "close": [200.0, 150.0, 151.0, 180.0, 12.0, 11.0, 190.0, 100.0],
                            "volume": [5e7, 3e7, 2e7, 1e6, 1e7, 1e7, 3e5, 1e5], "vwap": [200.0] * 8})
    shares = pl.DataFrame({"cik": ["0000320193", "0001652044", "0000034088"], "shares": [1.5e10, 1.2e10, 4e9],
                           "shares_date": [date(2026, 6, 30)] * 3, "loc": ["US-CA", "US-CA", "US-TX"],
                           "entity_name": ["Apple", "Alphabet", "Exxon"], "shares_source": ["dei"] * 3})
    now = datetime.now(timezone.utc)
    subs = pl.DataFrame([
        {"cik": "0000320193", "sub_name": "Apple Inc.", "sic": "3571", "biz_country": "United States", "fye_mmdd": "09-27",
         "latest_annual_form": "10-K", "former_names": ["APPLE COMPUTER INC"], "tickers": ["AAPL"], "succession_form": False, "fetched_at": now},
        {"cik": "0001652044", "sub_name": "Alphabet Inc.", "sic": "7370", "biz_country": "United States", "fye_mmdd": "12-31",
         "latest_annual_form": "10-K", "former_names": [], "tickers": ["GOOGL", "GOOG"], "succession_form": False, "fetched_at": now},
        {"cik": "0001094517", "sub_name": "TOYOTA MOTOR CORP/", "sic": "3711", "biz_country": "Japan", "fye_mmdd": "03-31",
         "latest_annual_form": "20-F", "former_names": [], "tickers": ["TM"], "succession_form": False, "fetched_at": now},
        {"cik": "0001119639", "sub_name": "PETROBRAS", "sic": "1311", "biz_country": "Brazil", "fye_mmdd": "12-31",
         "latest_annual_form": "20-F", "former_names": [], "tickers": ["PBR", "PBR-A"], "succession_form": False, "fetched_at": now},
        {"cik": "0000034088", "sub_name": "EXXON MOBIL CORP", "sic": "2911", "biz_country": "United States", "fye_mmdd": "12-31",
         "latest_annual_form": "10-K", "former_names": [], "tickers": ["XOMOLD"], "succession_form": False, "fetched_at": now},
    ])
    yf = pl.DataFrame([
        {"yf_ticker": aapl_ticker, "industry": "Consumer Electronics", "sector": "Technology", "marketCap": 3.0e12, "currency": "USD",
         "financialCurrency": "USD", "longName": "Apple Inc."},
        {"yf_ticker": "GOOGL", "industry": "Internet Content & Information", "marketCap": 2.0e12, "currency": "USD", "financialCurrency": "USD"},
        {"yf_ticker": "7203.T", "industry": "Auto Manufacturers", "marketCap": 4.0e13, "currency": "JPY", "financialCurrency": "JPY"},
        {"yf_ticker": "PETR4.SA", "industry": "Oil & Gas Integrated", "marketCap": 4.0e11, "currency": "BRL", "financialCurrency": "BRL"},
        {"yf_ticker": "0700.HK", "industry": "Internet Content & Information", "marketCap": 4.0e12, "currency": "HKD", "financialCurrency": "CNY"},
        {"yf_ticker": "601318.SS", "industry": "Insurance - Life", "marketCap": 9.0e11, "currency": "CNY", "financialCurrency": "CNY"},
        {"yf_ticker": "005930.KS", "industry": "Consumer Electronics", "marketCap": 4.0e14, "currency": "KRW", "financialCurrency": "KRW"},
        {"yf_ticker": "BAP", "industry": "Banks - Regional", "marketCap": 1.5e10, "currency": "USD", "financialCurrency": "PEN"},
        {"yf_ticker": "XOMOLD", "industry": "Oil & Gas Integrated", "marketCap": 4.0e11, "currency": "USD", "financialCurrency": "USD"},
    ])
    fx = pl.DataFrame({"currency": ["USD", "JPY", "BRL", "HKD", "CNY", "KRW", "PEN"],
                       "usd_per_unit": [1.0, 0.0067, 0.18, 0.128, 0.14, 0.00073, 0.27]})
    edinet = pl.DataFrame({"edinet_code": ["E02144"], "listed": ["上場"], "fye": ["3月31日"], "name_ja": ["トヨタ自動車株式会社"],
                           "name_en": ["TOYOTA MOTOR CORPORATION"], "jp_sec_code": ["7203"]})
    jpx = pl.DataFrame({"jp_sec_code": ["7203"], "name_ja": ["トヨタ自動車"], "tse33_code": ["3700"]})
    hkex = pl.DataFrame({"hk_code": ["00700", "02318"], "name": ["TENCENT", "PING AN"], "board_lot": [100, 500],
                         "isin": ["KYG875721634", "CNE1000003X6"]})
    cvm_t = pl.DataFrame({"cnpj": ["33000167000101", "33000167000101"], "ticker": ["PETR4", "PETR3"]})
    cvm_c = pl.DataFrame({"cvm_code": ["9512"], "cnpj": ["33000167000101"], "name": ["PETROLEO BRASILEIRO S.A. PETROBRAS"],
                          "situation": ["ATIVO"], "sector": ["Petróleo e Gás"]})
    return {"sec_tickers": sec, "polygon_tickers": poly, "grouped": grouped, "sec_shares": shares, "submissions": subs,
            "yf_info": yf, "fx": fx, "edinet": edinet, "jpx": jpx, "hkex": hkex, "cvm_tickers": cvm_t, "cvm_cad": cvm_c}


def _build(settings, **kw):
    I.clear_cache()
    return I.build_reference(as_of=kw.pop("as_of", date(2026, 10, 9)), sources=kw.pop("sources", _sources()), settings=settings,
                             write_universe=True, us_size=kw.pop("us_size", 2), **kw)


def test_offline_build_links_and_lookups(tmp_data):
    _write_seeds(tmp_data, SEEDS)
    res = _build(tmp_data)
    iss = read_table("issuers")
    sm = read_table("securities_master")
    # Toyota: seed + ADR (via adr_ticker) in one issuer; ADR is its own security linked to the home share
    toyota = lookup = I.lookup("cik", "1094517")
    assert toyota is not None
    t = iss.filter(pl.col("issuer_id") == toyota["issuer_id"]).row(0, named=True)
    assert t["edinet_code"] == "E02144" and t["region"] == "JP" and t["subsector_id"] == "autos"
    assert t["classification_source"] == "yf_industry"
    adr = sm.filter((pl.col("issuer_id") == toyota["issuer_id"]) & (pl.col("security_type") == "adr")).row(0, named=True)
    prim = sm.filter((pl.col("issuer_id") == toyota["issuer_id"]) & pl.col("is_primary_security")).row(0, named=True)
    assert adr["underlying_security_id"] == prim["security_id"] and prim["display_ticker"] == "7203.T"
    # Petrobras: ON + PN home shares and 2 ADRs under one issuer, CVM code via FCA tickers
    pb = I.lookup("cvm_code", "9512")
    assert pb and pb["issuer_id"] == I.lookup("ticker_mic", "PBR@XNYS")["issuer_id"] == I.lookup("yf_ticker", "PETR3.SA")["issuer_id"]
    assert sm.filter(pl.col("issuer_id") == pb["issuer_id"]).height == 4
    # A/H shares grouped; HK ISIN from HKEX list
    pa = I.lookup("cn_code", "601318.SS")
    assert pa["issuer_id"] == I.lookup("hk_code", "2318")["issuer_id"]
    assert I.lookup("isin", "CNE1000003X6")["issuer_id"] == pa["issuer_id"]
    # Samsung: computed KR ISIN
    assert I.lookup("isin", "KR7005930003") is not None
    # Alphabet: one issuer, two classes, primary = higher dollar volume (GOOGL)
    g = I.lookup("cik", "1652044")
    gs = sm.filter(pl.col("issuer_id") == g["issuer_id"])
    assert gs.height == 2 and gs.filter(pl.col("is_primary_security")).item(0, "display_ticker") == "GOOGL"
    # seed that IS a US listing (BAP@XNYS) does not create a duplicate
    assert iss.filter(pl.col("name") == "Credicorp").height == 1
    # listing-level lookup + primary ids
    a = I.lookup("ticker_mic", "AAPL@XNAS")
    assert a["entity_level"] == "listing" and a["listing_id"] and a["primary_security_id"]
    assert I.lookup("figi", "nope") is None
    # universe
    um = read_table("universe_membership")
    assert set(um.get_column("universe_id").unique().to_list()) >= {"mvp_us", "mvp_global", "full"}
    us = um.filter(pl.col("universe_id") == "mvp_us").join(sm, on="security_id")
    assert set(us.get_column("display_ticker").to_list()) == {"AAPL", "GOOGL"}   # us_size=2, ADRs/20-F excluded
    assert um.filter(pl.col("universe_id") == "mvp_global").height == 6          # 8 seed rows -> 6 issuers (A/H, ON/PN)
    assert res["stats"]["taxonomy"]["classified_pct"] > 50


def test_ids_assigned_once_and_ticker_change_history(tmp_data):
    _write_seeds(tmp_data, SEEDS)
    _build(tmp_data)
    before = read_table("issuers").select("issuer_id", "name").sort("issuer_id")
    sec_before = read_table("securities_master").select("security_id").sort("security_id")
    aapl = I.lookup("cik", "320193")
    # next day: Apple trades under a new symbol
    _build(tmp_data, sources=_sources("APLE"), as_of=date(2026, 10, 10))
    assert read_table("issuers").select("issuer_id", "name").sort("issuer_id").equals(before)
    assert read_table("securities_master").select("security_id").sort("security_id").equals(sec_before)
    new = I.lookup("ticker_mic", "APLE@XNAS")
    assert new["issuer_id"] == aapl["issuer_id"] and new["listing_id"] == I.lookup("ticker_mic", "APLE@XNAS")["listing_id"]
    assert I.lookup("ticker_mic", "AAPL@XNAS") is None                                  # closed yesterday
    assert I.lookup("ticker_mic", "AAPL@XNAS", as_of=date(2026, 10, 1))["issuer_id"] == aapl["issuer_id"]
    lst = read_table("listings").filter(pl.col("listing_id") == new["listing_id"])
    assert lst.height == 2 and lst.filter(pl.col("valid_to").is_not_null()).item(0, "local_symbol") == "AAPL"


def test_successions_seed_and_merge_alias(tmp_data):
    _write_seeds(tmp_data, SEEDS)
    _build(tmp_data)
    old = I.lookup("cik", "34088")
    assert old is not None
    # Exxon re-registers under a new CIK with a new ticker; the seed links predecessor -> successor
    src = _sources()
    src["sec_tickers"] = pl.concat([src["sec_tickers"], pl.DataFrame({
        "cik": ["0002115436"], "name": ["ExxonMobil Holdings Corp"], "ticker": ["XOM"], "exchange": ["NYSE"], "mic": ["XNYS"],
        "sec_rank": [8]}).with_columns(pl.col("sec_rank").cast(pl.Int32))])
    src["polygon_tickers"] = pl.concat([src["polygon_tickers"], pl.DataFrame({
        "ticker": ["XOM"], "name": ["ExxonMobil"], "primary_exchange": ["XNYS"], "type": ["CS"], "cik": ["0002115436"],
        "composite_figi": [None], "share_class_figi": [None], "currency_name": ["usd"]})])
    _write_seeds(tmp_data, SEEDS, successions="cik:0000034088,cik:0002115436,2026-06-01,holding_company_reorganization,test")
    _build(tmp_data, sources=src, as_of=date(2026, 10, 10))
    new = I.lookup("cik", "2115436")
    assert new["issuer_id"] == old["issuer_id"]                     # one issuer spans both CIKs, old id survives
    assert I.lookup("cik", "34088")["issuer_id"] == old["issuer_id"]


def test_registry_merge_keeps_oldest(tmp_data):
    reg = I.Registry(tmp_data)
    a = reg.resolve(["cik:1"], "cik:1", date(2020, 1, 1))
    b = reg.resolve(["lei:X"], "lei:X", date(2021, 1, 1))
    assert a != b
    c = reg.resolve(["cik:1", "lei:X"], "lei:X", date(2022, 1, 1))
    assert c == a and reg.survivor(b) == a and (b, a) in reg.merges
    reg.save(date(2022, 1, 1))
    assert I.Registry(tmp_data).survivor(b) == a


def test_resolve_frame_and_merge_rows(tmp_data):
    _write_seeds(tmp_data, SEEDS)
    _build(tmp_data)
    df = pl.DataFrame({"source_id": ["sec_edgar", "sec_edgar", "x"], "source_entity_id": ["0000320193", "0009999999", "7203.T"],
                       "issuer_id": [None, None, None]}, schema_overrides={"issuer_id": pl.Utf8})
    df = df.with_columns(pl.col("source_id").replace_strict({"sec_edgar": "cik", "x": "yf_ticker"}).alias("t"))
    out = I.resolve_frame(df, "source_entity_id", id_type_col="t")
    assert out.get_column("issuer_id").null_count() == 1 and out.height == 3
    iid = out.item(0, "issuer_id")
    n = I.merge_identifier_rows([{"id_type": "lei", "id_value": "HWUPKR0MPOU8FGXBT394", "entity_level": "issuer",
                                  "issuer_id": iid, "source_id": "manual", "confidence": 1.0}], settings=tmp_data)
    assert n == 1 and I.lookup("lei", "hwupkr0mpou8fgxbt394")["issuer_id"] == iid
    # weaker assertion never replaces an authoritative mapping
    I.merge_identifier_rows([{"id_type": "lei", "id_value": "HWUPKR0MPOU8FGXBT394", "entity_level": "issuer",
                              "issuer_id": "IOTHER", "source_id": "fuzzy", "confidence": 0.85}], settings=tmp_data)
    assert I.lookup("lei", "HWUPKR0MPOU8FGXBT394")["issuer_id"] == iid


def test_lookup_on_empty_install(tmp_data):
    I.clear_cache()
    assert I.lookup("cik", "320193") is None
    assert I.lookup_many("cik", ["1", "2"]).is_empty()
    with pytest.raises(ValueError):
        I.lookup("bogus", "x")


def test_entity_id_type_routing():
    df = pl.DataFrame({"source_id": ["esef_xbrl_org", "esef_xbrl_org", "fca_nsm", "baostock", "sec_edgar", "yfinance_fund", "hkex", "tdnet"],
                       "source_entity_id": ["724500K5PTPSST86UQ23", "05531038", "PY6ZZQWO2IZFZC3IOL08", "SSE:600519", "0000320193",
                                            "7203.T", "00700", "7203"]})
    assert df.with_columns(I.entity_id_type_expr().alias("t")).get_column("t").to_list() == [
        "lei", "uk_company_number", "lei", "cn_code", "cik", "yf_ticker", "hk_code", "jp_sec_code"]
