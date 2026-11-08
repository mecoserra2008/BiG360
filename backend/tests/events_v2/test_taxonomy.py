"""E1: theme bitmask, GKG / title / CAMEO theme rules, FIPS -> ISO2, GLOBAL rule, sector labels, dictionaries."""

from __future__ import annotations

import polars as pl
import pytest

from app.events_v2 import dicts
from app.events_v2 import taxonomy as tx


def test_theme_bits_dense_and_roundtrip():
    assert [t.bit for t in tx.THEMES] == list(range(len(tx.THEMES)))
    assert 35 <= len(tx.THEMES) <= 63
    ids = ["inflation", "sanctions", "oil_gas", "aviation_transport"]
    m = tx.theme_ids_to_mask(ids)
    assert m > 0 and set(tx.mask_to_theme_ids(m)) == set(ids)
    assert tx.mask_to_theme_ids(0) == [] and tx.mask_to_theme_ids(None) == []
    with pytest.raises(KeyError):
        tx.theme_ids_to_mask(["nope"])


def test_gkg_counted_rule_strong_vs_broad():
    df = pl.DataFrame({"e": [
        "ECON_BANKRUPTCY,10",                        # strong: one mention is enough
        "ARMEDCONFLICT,5",                           # broad: one mention is not enough
        "ARMEDCONFLICT,5;ECON_TAXATION,9;ARMEDCONFLICT,77",  # broad x2 -> conflict; taxation x1 -> no
        "NATURAL_DISASTER_FLOOD,3;NATURAL_DISASTER_HURRICANE,40",  # prefix family counts together
        "", None]})
    out = df.with_columns(tx.gkg_theme_mask_counted_expr("e").alias("m")).get_column("m").to_list()
    th = [tx.mask_to_theme_ids(x) for x in out]
    assert th[0] == ["bankruptcy_default"]
    assert th[1] == []
    assert th[2] == ["conflict_military"]
    assert th[3] == ["natural_disaster"]
    assert th[4] == [] and th[5] == []
    # translingual articles need 3 broad mentions
    df3 = pl.DataFrame({"e": ["ARMEDCONFLICT,5;ARMEDCONFLICT,77"]})
    assert df3.select(tx.gkg_theme_mask_counted_expr("e", 3)).item() == 0


def test_title_rules_word_bounded():
    assert "recall_safety" in tx.mask_to_theme_ids(tx.title_theme_mask("Ford recalls 120,000 vehicles over brake defect"))
    assert tx.title_theme_mask("He recalls his childhood in Ohio") == 0
    assert "oil_gas" in tx.mask_to_theme_ids(tx.title_theme_mask("Oil prices surge after OPEC+ cut"))
    assert "metals_mining" not in tx.mask_to_theme_ids(tx.title_theme_mask("Gold medal for local runner"))
    assert {"monetary_policy", "inflation"} <= set(tx.mask_to_theme_ids(tx.title_theme_mask("Fed signals rate cut as inflation cools")))
    df = pl.DataFrame({"t": ["Strike at port halts shipping", None]})
    m = df.select(tx.title_theme_mask_expr("t")).to_series().to_list()
    assert "supply_chain_shipping" in tx.mask_to_theme_ids(m[0]) and m[1] == 0


def test_cameo_rules():
    df = pl.DataFrame({"c": ["143", "1831", "0231", "190", "163", None]})
    out = [tx.mask_to_theme_ids(x) for x in df.select(tx.cameo_theme_mask_expr("c")).to_series().to_list()]
    assert "strikes" in out[0] and "protest_unrest" in out[0]
    assert "terrorism" in out[1] and "conflict_military" in out[1]
    assert out[2] == []
    assert out[3] == ["conflict_military"]
    assert "sanctions" in out[4]
    assert out[5] == []


def test_fips_to_iso2():
    assert tx.fips_to_iso2("UK") == "GB"
    assert tx.fips_to_iso2("CH") == "CN"      # FIPS CH = China (not Switzerland)
    assert tx.fips_to_iso2("SZ") == "CH"
    assert tx.fips_to_iso2("GM") == "DE"
    assert tx.fips_to_iso2("RS") == "RU"
    assert tx.fips_to_iso2("JA") == "JP" and tx.fips_to_iso2("KS") == "KR"
    assert tx.fips_to_iso2(None) is None and tx.fips_to_iso2("ZZ") is None
    assert tx.COUNTRIES[0] == tx.GLOBAL and tx.COUNTRY_ID["GLOBAL"] == 0
    assert all(len(c) == 2 for c in tx.COUNTRIES[1:]) and len(set(tx.COUNTRIES)) == len(tx.COUNTRIES)


def test_global_rule():
    assert tx.countries_with_global([]) == ["GLOBAL"]
    assert tx.countries_with_global([("US", 3), ("CN", 2)]) == ["US", "CN"]
    five = [("US", 5), ("CN", 4), ("DE", 3), ("JP", 2), ("FR", 1)]
    assert tx.countries_with_global(five) == ["US", "CN", "DE", "GLOBAL"]
    oil = tx.theme_ids_to_mask(["oil_gas"])
    assert tx.countries_with_global([("SA", 2), ("RU", 2), ("US", 1)], oil)[-1] == "GLOBAL"   # global theme, no dominant
    assert tx.countries_with_global([("SA", 5), ("RU", 1)], oil) == ["SA", "RU"]               # dominant country


def test_sector_labels_from_themes():
    m = tx.theme_ids_to_mask(["oil_gas", "monetary_policy", "conflict_military"])
    assert set(tx.sectors_of_mask(tx.theme_sector_mask(m))) == {"ENRG", "FINS"}   # conflict labels no sector
    df = pl.DataFrame({"m": [m, 0]})
    assert df.select(tx.theme_sector_mask_expr("m")).to_series().to_list() == [tx.theme_sector_mask(m), 0]
    assert tx.sector_mask(["TECH", "XXX"]) == 1 and tx.sectors_of_mask(tx.sector_mask(["UTIL", "REAL"])) == ["UTIL", "REAL"]


def test_static_dictionary_rows_cover_all_maps():
    df = dicts.static_rows()
    by = df.group_by("dict_type").len()
    n = dict(zip(by["dict_type"].to_list(), by["len"].to_list(), strict=True))
    assert n["country"] == len(tx.COUNTRIES) and n["sector"] == len(tx.SECTORS) and n["theme"] == len(tx.THEMES)
    gk = df.filter(pl.col("dict_type") == "gkg_theme")
    assert gk.height > 80
    row = gk.filter(pl.col("code") == "ENV_OIL").row(0, named=True)
    assert tx.mask_to_theme_ids(row["theme_mask"]) == ["oil_gas"] and tx.sectors_of_mask(row["sector_mask"]) == ["ENRG"]
    assert gk.filter(pl.col("code") == "NATURAL_DISASTER*").height == 1


def test_dictionaries_persist_append_only(tmp_data):
    dicts.ensure_static(tmp_data)
    a = dicts.assign_ids("domain", ["b.com", "a.com"], tmp_data)
    b = dicts.assign_ids("domain", ["c.com", "a.com"], tmp_data)
    assert a["a.com"] == b["a.com"] and a["b.com"] == b["b.com"] and b["c.com"] == max(b.values())
    src = dicts.load_dict("source", tmp_data)
    assert dict(zip(src["code"], src["id"], strict=True))["gdelt_gkg"] == 0
    dicts.ensure_static(tmp_data)  # idempotent, keeps dynamic dictionaries
    assert dicts.load_dict("domain", tmp_data).height == 3


def test_norm_org_matches_python_and_polars():
    names = ["The Coca-Cola Company", "Société Générale S.A.", "Amazon.com, Inc.", "PT Bank Central Asia Tbk",
             "BYD Company Limited", "Alibaba Group Holding Ltd"]
    pl_out = pl.DataFrame({"n": names}).select(dicts.norm_org_expr(pl.col("n"))).to_series().to_list()
    assert pl_out == [dicts.norm_org(n) for n in names]
    assert pl_out[:4] == ["coca cola", "societe generale", "amazon", "bank central asia"]
    assert pl_out[4] == "byd" and pl_out[5] == "alibaba"
