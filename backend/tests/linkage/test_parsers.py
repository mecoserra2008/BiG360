"""Linkage parsers on recorded fixtures (IMF IMTS, BIS LBS, UN Comtrade preview) and synthetic FSDS / ICIO inputs."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import polars as pl
import pytest

FX = Path(__file__).parent / "fixtures"
ISO3 = {"DEU": "DE", "USA": "US", "CHN": "CN", "FRA": "FR", "JPN": "JP", "GBR": "GB"}


def test_imf_imts_csv():
    from app.linkage.trade import parse_imf_csv

    df = parse_imf_csv((FX / "imf_imts_deu.csv").read_bytes(), ISO3)
    assert set(df["reporter_iso2"]) == {"DE"}
    assert set(df["partner_iso2"]) == {"US", "CN"}  # G001 aggregate dropped (IMTS has no W00 rows for this key)
    assert set(df["flow"]) == {"X", "M"} and set(df["valuation"]) == {"FOB", "CIF"}
    imp = df.filter((pl.col("partner_iso2") == "CN") & (pl.col("flow") == "M") & (pl.col("year") == 2022))
    assert imp["value_usd"].item() == pytest.approx(137102429250.0)


def test_comtrade_preview_json():
    from app.linkage.trade import parse_comtrade

    j = json.loads((FX / "comtrade_deu_2023.json").read_text())
    df = parse_comtrade(j, {276: "DE"}, {0: "W0", 842: "US", 156: "CN", 490: "TW", 251: "FR"})
    assert df.height == 10
    assert set(df["partner_iso2"]) == {"W0", "US", "CN", "TW", "FR"}
    assert (df["value_usd"] > 0).all() and set(df["flow"]) == {"M", "X"}
    w = df.filter((pl.col("partner_iso2") == "W0") & (pl.col("flow") == "X"))["value_usd"].item()
    assert w > df.filter((pl.col("partner_iso2") == "US") & (pl.col("flow") == "X"))["value_usd"].item()


def test_bis_lbs_csv_drops_aggregates():
    from app.linkage.banking import parse_bis_csv

    df = parse_bis_csv((FX / "bis_lbs_claims.csv").read_bytes(), "lbs_claims")
    assert df.height > 0
    assert set(df["counterparty_iso2"]) <= {"DE", "JP"}  # 5J / PU dropped
    assert set(df["reporter_iso2"]) <= {"US", "GB"}
    assert df["period_end"].dt.month().is_in([3, 6, 9, 12]).all()
    q1 = df.filter(pl.col("period") == "2025-Q1").sort("reporter_iso2")
    assert str(q1["period_end"][0]) == "2025-03-31"


def _fsds_zip(tmp_path: Path) -> Path:
    sub = ("adsh\tcik\tname\tsic\tcountryba\tcountryinc\tform\tperiod\tfy\tfp\tfye\tfiled\n"
           "0001-25-1\t320193\tAPPLE INC\t3571\tUS\tUS\t10-K\t20250930\t2025\tFY\t0930\t20251031\n"
           "0002-25-1\t999\tOTHER\t1000\tUS\tUS\t10-K\t20250930\t2025\tFY\t0930\t20251031\n")
    rows = [
        ("0001-25-1", "RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap/2024", "20250930", "4", "USD", "", "", "1000"),
        ("0001-25-1", "RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap/2024", "20250930", "4", "USD", "Geographical=US;", "", "400"),
        ("0001-25-1", "RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap/2024", "20250930", "4", "USD", "Geographical=CN;", "", "200"),
        ("0001-25-1", "RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap/2024", "20250930", "4", "USD", "Geographical=Europe;", "", "300"),
        ("0001-25-1", "RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap/2024", "20250930", "4", "USD", "Geographical=RestOfAsiaPacific;", "", "100"),
        ("0001-25-1", "RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap/2024", "20250930", "4", "USD", "BusinessSegments=X;Geographical=US;", "", "50"),
        ("0001-25-1", "ConcentrationRiskPercentage1", "us-gaap/2024", "20250930", "4", "pure", "ConcentrationRiskByBenchmark=Revenue;ConcentrationRiskByType=CustomerConcentrationRisk;MajorCustomers=WalmartInc;", "", "0.12"),
        ("0001-25-1", "Revenues", "0001-25-1", "20250930", "4", "USD", "Geographical=US;", "", "1"),  # custom -> dropped
        ("0002-25-1", "NetIncomeLoss", "us-gaap/2024", "20250930", "4", "USD", "", "", "5"),
    ]
    num = "adsh\ttag\tversion\tddate\tqtrs\tuom\tsegments\tcoreg\tvalue\tfootnote\n" + "".join(
        "\t".join(r) + "\t\n" for r in rows)
    z = tmp_path / "2025q4.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("sub.txt", sub)
        zf.writestr("num.txt", num)
    return z


def test_fsds_extract_and_normalize(tmp_data, tmp_path):
    from app.linkage.georev import extract_zip, normalize_fsds

    work = tmp_path / "w"
    work.mkdir()
    sub, num = extract_zip(_fsds_zip(tmp_path), work)
    assert set(sub["adsh"]) == {"0001-25-1"}
    assert num.filter(pl.col("segments").str.contains("Geographical")).height == 5  # custom-tag row dropped
    assert num.filter(pl.col("segments").fill_null("") == "").height == 1  # the revenue total
    out = normalize_fsds(num, sub, tmp_data)
    rev = out.filter(pl.col("measure") == "revenue").sort("member_raw")
    assert rev.height == 4  # multi-axis row excluded
    by = {r["member_raw"]: r for r in rev.iter_rows(named=True)}
    assert by["US"]["member_type"] == "country" and by["US"]["iso2"] == "US"
    assert by["US"]["share"] == pytest.approx(0.4) and by["US"]["denominator"] == pytest.approx(1000)
    assert by["Europe"]["member_type"] == "region" and by["Europe"]["region"] == "EUROPE"
    assert by["RestOfAsiaPacific"]["member_type"] == "region"
    assert str(by["CN"]["knowledge_date"]) == "2025-10-31" and by["CN"]["filing_id"] == "sec_edgar:0001-25-1"
    assert by["CN"]["entity_id"] == "0000320193" and by["CN"]["qtrs"] == 4
    assert str(by["CN"]["period_start"]) == "2024-10-01"


def test_icio_aggregation_to_sectors():
    from app.linkage.iotables import aggregate_year

    cols = ["V1", "USA_C26", "USA_K", "CHN_C26", "CHN_K", "USA_HFCE", "CHN_GFCF", "OUT"]
    rows = [["USA_C26", 1, 2, 3, 4, 10, 0, 20], ["USA_K", 5, 6, 0, 0, 1, 0, 12], ["CHN_C26", 7, 0, 8, 0, 0, 9, 24],
            ["CHN_K", 0, 0, 0, 1, 0, 0, 1], ["TLS", 1, 1, 1, 1, 0, 0, 0], ["VA", 1, 1, 1, 1, 0, 0, 0],
            ["OUT", 20, 12, 24, 1, 0, 0, 0]]
    buf = io.StringIO()
    buf.write(",".join(cols) + "\n")
    for r in rows:
        buf.write(",".join(str(x) for x in r) + "\n")
    df = aggregate_year(buf.getvalue().encode(), {"USA": "US", "CHN": "CN"})
    get = lambda a, b, c, d: df.filter((pl.col("src_iso2") == a) & (pl.col("src_sector") == b)  # noqa: E731
                                      & (pl.col("dst_iso2") == c) & (pl.col("dst_sector") == d))["value_usd_m"].sum()
    assert get("US", "TECH", "CN", "TECH") == 3
    assert get("CN", "TECH", "US", "TECH") == 7
    assert get("US", "TECH", "US", "FD") == 10 and get("CN", "TECH", "CN", "FD") == 9
    assert get("US", "FINS", "US", "TECH") == 5
    assert set(df["src_sector"]) <= {"TECH", "FINS"}  # TLS / VA / OUT rows excluded


def test_text_customer_extraction():
    from app.linkage.customers import decamel, extract_text_customers, is_generic

    txt = ("For 2025, Samsung Electronics Co., Ltd. accounted for approximately 11% of total net sales and Taiwan "
           "Semiconductor Manufacturing Company Limited accounted for 10% of total net sales. Sales to MMC totaled 17% "
           "of FCX's consolidated revenues in 2024. No single customer accounted for more than 10% of sales. "
           "One customer represented approximately 32% of our revenue. In 2021, Russia and Ukraine accounted for "
           "approximately 1% of our net sales.")
    got = {n: s for n, s, _ in extract_text_customers(txt)}
    assert got == {"Samsung Electronics Co., Ltd": pytest.approx(0.11),
                   "Taiwan Semiconductor Manufacturing Company Limited": pytest.approx(0.10), "MMC": pytest.approx(0.17)}
    assert decamel("WalmartInc") == "Walmart Inc" and decamel("UnitedStatesGovernment") == "United States Government"
    for g in ("Customer A", "Industrial", "Private Sector", "Inc", "Germany and Norway", "SignificantCustomers"):
        assert is_generic(decamel(g)), g
    for ok in ("Walmart Inc", "Paccar", "Navistar"):
        assert not is_generic(ok), ok
