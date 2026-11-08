"""app.normalize.taxonomy: precedence, national code mappings, size rule, coverage."""

from __future__ import annotations

import polars as pl

from app.normalize import taxonomy as T


def test_mappers():
    assert T.map_yf("Software—Infrastructure") == "software_infrastructure"
    assert T.map_yf("Semiconductors") == "semiconductors"
    assert T.map_sic("3674") == (True, "semiconductors")
    assert T.map_sic(6770) == (True, None)                    # blank checks excluded
    assert T.map_sic("abc") == (False, None)
    assert T.map_tse33("3700") == "autos" and T.map_tse33("50") == "agri_products"
    assert T.map_ksic("26110") == "semiconductors"           # 4-digit override
    assert T.map_ksic("반도체 제조업") == "semiconductors"      # KRX KIND industry name
    assert T.map_csrc("C27") == "pharma" and T.map_csrc("J 金融业") == "capital_markets_exchanges" and T.map_csrc("J66") == "banks_regional"
    assert T.map_cvm("Petróleo e Gás") == "integrated_oil_gas"
    assert T.sector_of("banks_global") == "FINS" and "banks" in T.subsector_groups("banks_regional")


def test_precedence_override_yf_sic_native():
    base = {"issuer_id": "I1", "name": "X", "cik": None, "yf_industry": None, "sic_code": None, "native_industry_code": None}
    # contract override by ticker beats everything
    assert T.classify({**base, "yf_industry": "Software - Application"}, {"override_keys": ["ticker:MSFT@XNAS"]})[1:] == (
        "software_infrastructure", "override")
    assert T.classify({**base, "yf_industry": "Auto Manufacturers", "sic_code": "7372"})[1:] == ("autos", "yf_industry")
    assert T.classify({**base, "sic_code": "7372"})[1:] == ("software_application", "sic")
    assert T.classify({**base, "native_industry_code": "3250"}, {"native_scheme": "tse33"})[1:] == ("pharma", "tse33")
    assert T.classify({**base, "sic_code": "6770"}) == (None, None, "sic")
    assert T.classify(base) == (None, None, "unknown")
    # HK override written with 4-digit code matches 5-digit keys
    assert T.classify(base, {"override_keys": ["ticker:0700@XHKG"]})[1] == "internet_platforms"
    # manual seeds override
    assert T.classify(base, {"override_keys": ["ticker:PRX@XAMS"]}, manual={"ticker:PRX@XAMS": "diversified_holdings"})[1:] == (
        "diversified_holdings", "manual")


def test_bank_size_rule():
    b = {"issuer_id": "I1", "name": "Some Regional Bank", "sic_code": "6022"}
    assert T.classify(b)[1] == "banks_regional"
    assert T.classify(b, total_assets_usd=6e11)[1] == "banks_global"
    assert T.classify({**b, "name": "Barclays PLC"})[1] == "banks_global"   # G-SIB list


def test_classify_issuers_and_coverage():
    df = pl.DataFrame({"issuer_id": ["A", "B", "C"], "name": ["a", "b", "c"], "region": ["US", "US", "JP"], "is_active": [True] * 3,
                       "cik": [None, None, None], "yf_industry": ["Banks - Diversified", None, None],
                       "sic_code": [None, "2834", None], "native_industry_code": [None, None, None]})
    out, cov = T.classify_issuers(df)
    assert out.get_column("subsector_id").to_list()[1] == "pharma"
    assert cov["n"] == 3 and cov["classified"] == 2 and cov["by_region_pct"]["JP"] == 0.0
