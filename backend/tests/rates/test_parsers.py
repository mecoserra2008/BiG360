"""W3 parsers on recorded fixtures (tests/rates/fixtures, recorded 2026-10-31; no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.rates import auctions as A
from app.rates import bonds as B
from app.rates import sources as S
from app.rates import trace as T
from app.rates.common import tenor_months_from_label, to_bp100

F = Path(__file__).parent / "fixtures"


def _t(name: str, enc: str = "utf-8") -> str:
    return (F / name).read_text(encoding=enc)


def test_bp100_roundtrip():
    assert to_bp100(4.2512) == 42512 and to_bp100(-0.096) == -960 and to_bp100(None) is None
    assert to_bp100(float("nan")) is None and to_bp100("x") is None
    assert tenor_months_from_label("10Y") == 120 and tenor_months_from_label("3 Mo") == 3
    assert tenor_months_from_label("1J") == 12 and tenor_months_from_label("0.25") == 3


def test_ust_xml_nominal_and_real():
    pts = S.parse_ust_xml(_t("ust_par_202610.xml"), S.UST_NOMINAL)
    d1 = {m: v for d, m, v in pts if d == date(2026, 10, 1)}
    assert d1[1] == 4.06 and d1[120] == 5.24 and d1[360] == 5.61 and 2 in d1 and 1.5 not in d1
    assert len({d for d, _, _ in pts}) >= 20
    real = S.parse_ust_xml(_t("ust_real_202610.xml"), S.UST_REAL)
    r1 = {m: v for d, m, v in real if d == date(2026, 10, 1)}
    assert r1 == {60: 2.65, 84: 2.76, 120: 2.88, 240: 3.16, 360: 3.31}


def test_ecb_params_and_bundesbank():
    p = S.parse_ecb_params(_t("ecb_yc_params.csv"))
    assert {c for c, _ in p} == {"EA_AAA", "EA_ALL"} and all(len(v) == 6 for v in p.values())
    bb = S.parse_bundesbank_csv(_t("buba_svensson.csv"))
    assert bb[date(2026, 10, 30)] == {"BETA0": 3.97906, "BETA1": -1.54899, "BETA2": 25.7058, "BETA3": -26.35599,
                                      "TAU1": 1.48317, "TAU2": 1.58613}
    # English-locale export (',' separator, decimal point) parses to the same values
    en = "\n".join(line.replace(",", ".").replace(";", ",") for line in _t("buba_svensson.csv").splitlines())
    assert S.parse_bundesbank_csv(en)[date(2026, 10, 30)]["BETA0"] == 3.97906


def test_boe_workbook_spot_forward_and_short_end():
    content = (F / "boe_nominal_current.xlsx").read_bytes()
    out = S.parse_boe_workbook(content, (1, 6, 12, 24, 120, 360, 480))
    assert set(out) == {"spot", "forward"}
    d = date(2026, 10, 1)
    assert out["spot"][d][12] == pytest.approx(4.35001771, abs=1e-6) and out["forward"][d][120] > 0  # sheet "4. spot curve", 1y
    assert 1 in out["spot"][d]      # 1M only exists in the short-end sheet
    ois = S.parse_boe_workbook((F / "boe_ois_current.xlsx").read_bytes(), (6, 12, 24))
    assert ois["spot"][d][6] == pytest.approx(4.03385941, abs=1e-6)


def test_mof_bde_bpstat_snb():
    from app.ingestion.macro_eu import parse_bpstat
    from app.ingestion.macro_national import parse_mof_csv

    mof = parse_mof_csv((F / "mof_jgbcme.csv").read_bytes())
    assert dict(mof["10Y"])[date(2026, 10, 1)] == 3.092 and tenor_months_from_label("40Y") == 480
    es = S.parse_bde_csv(_t("bde_ti_1_3.csv", "latin-1"))
    assert dict(es["D_G0B1F0ZP"])[date(2026, 10, 28)] == 4.028 and date(2026, 10, 25) not in dict(es["D_G0B1F0ZP"])
    eur = S.parse_bde_csv(_t("bde_ti_1_7.csv", "latin-1"))
    assert dict(eur["D_DNBAD172"])[date(2026, 10, 29)] > 0
    pt = parse_bpstat(json.loads(_t("bpstat_pt_yields.json")))
    assert set(pt) == set(S.BPSTAT_PT)
    snb = S.parse_snb_spot_all(_t("snb_rendeiduebd.csv"))
    assert (date(2026, 9, 25), 12, 0.238) in snb and all(m % 12 == 0 for _, m, _ in snb)


def test_boc_rba_irs():
    from app.macro.cb.rates import parse_valet

    v = parse_valet(json.loads(_t("boc_bond_yields_all.json")))
    assert dict(v["BD.CDN.10YR.DQ.YLD"])[date(2026, 10, 27)] == 3.83
    f2 = S.parse_rba_table(_t("rba_f2.csv"))
    assert dict(f2["FCMYGBAG10D"])[date(2026, 10, 28)] == 5.208
    f17 = S.parse_rba_table(_t("rba_f17_yields.csv"))
    assert S._f17_months("FZCY25D") == 3 and S._f17_months("FZCY1000D") == 120 and "FZCY1000D" in f17
    irs = S.parse_ecb_irs(_t("ecb_irs.csv"))
    assert dict(irs["DE"])[date(2026, 9, 1)] == 3.473 and "IT" in irs


def test_fiscaldata_auctions():
    rows = [A.parse_fiscaldata_auction(r) for r in json.loads(_t("fiscaldata_auctions.json"))["data"]]
    rows = [r for r in rows if r]
    assert rows and all(r["agency"] == "us_treasury" and r["currency"] == "USD" for r in rows)
    tips = next(r for r in rows if r["security_id"] == "91282CRR4")
    assert tips["security_type"] == "TIPS" and tips["term_months"] == 60 and tips["maturity_date"] == date(2031, 10, 15)
    up = [A.parse_fiscaldata_upcoming(r) for r in json.loads(_t("fiscaldata_upcoming.json"))["data"]]
    assert all(u["status"] == "announced" for u in up if u)
    assert A.term_months("13-Week") == 3 and A.term_months("10-Year") == 120 and A.term_months("9-Year 10-Month") == 118
    df = A.to_frame(rows + [{**rows[0], "status": "announced", "high_yield_bp_x100": None}])
    assert df.filter(df["security_key"] == rows[0]["security_key"])["status"].to_list() == [rows[0]["status"]]


def test_dmo_gilt_issuance():
    xml = ('<Data><View_Gilt_Issuance_History INSTRUMENT_NAME="0 1/8% Index-linked Treasury Gilt 2019" '
           'ISIN_CODE="GB00BBDR7T29" ACTUAL_DATE="2013-08-21T00:00:00" ISSUANCE_TYPE="Outright" '
           'NOMINAL_ISSUED="1794.585000000000000000" ISSUE_CLEAN_PRICE="106.52" ISSUE_YIELD="-0.886797000000" '
           'INDEXATION_LAG="3 months" /></Data>')   # recorded row (dmo.gov.uk XmlDataReport D2.1E)
    r = A.parse_dmo_issuance(xml)[0]
    assert r["security_type"] == "Index-linked gilt" and r["coupon_bp_x100"] == 1250
    assert r["high_yield_bp_x100"] == -8868 and r["accepted_amount"] == pytest.approx(1794.585e6)
    assert A.gilt_coupon("4 1/8% Treasury Gilt 2027") == 4.125 and A.gilt_coupon("4.25% Treasury 2055") == 4.25


def test_nyfed_rates():
    from app.ingestion.macro_national import parse_nyfed_rates

    d = parse_nyfed_rates(json.loads(_t("nyfed_all_latest.json")))
    assert "percentRate" in d and "average30day" in d and "index" in d
    s = parse_nyfed_rates(json.loads(_t("nyfed_sofr_last3.json")))
    assert s["percentRate"][-1][1] > 0 and "volumeInBillions" in s


def test_firds_debt_parse_and_collapse():
    with open(F / "firds_fulins_d_sample.xml", "rb") as fh:
        rows = B.parse_firds_debt(fh, date(2026, 10, 31))
    assert {r["isin"] for r in rows} == {"XS1234567890", "DE000A1B2C3D"}        # certificates (CFI DE) dropped
    df = B.collapse_isins(rows)
    fx = df.filter(df["bond_id"] == "XS1234567890").to_dicts()[0]
    assert fx["coupon_type"] == "fixed" and fx["coupon_bp_x100"] == 32500 and fx["n_venues"] == 2
    assert fx["maturity_date"] == date(2031, 3, 15) and fx["seniority"] == "senior" and fx["security_type"] == "bond"
    fl = df.filter(df["bond_id"] == "DE000A1B2C3D").to_dicts()[0]
    assert fl["coupon_type"] == "floating" and fl["floating_reference"] == "EURI 3M" and fl["floating_spread_bp"] == 85
    assert fl["seniority"] == "subordinated"


def test_openfigi_description_and_rows():
    assert B.parse_figi_description("AAPL 3 1/4 08/08/29") == {"coupon_type": "fixed", "coupon": 3.25,
                                                               "maturity_date": date(2029, 8, 8)}
    assert B.parse_figi_description("AAPL Float 08/28/19")["coupon_type"] == "floating"
    assert B.parse_figi_description("T 0 01/15/30")["coupon_type"] == "zero"
    assert B.parse_figi_description("XYZ 5.5 PERP")["maturity_date"] is None
    data = json.loads(_t("openfigi_search_aapl.json"))["data"]
    rows = B.figi_rows(data, "I1", "AAPL", "Apple Inc.", date(2026, 10, 31))
    assert {r["figi"] for r in rows} == {"BBG004HST0K7", "BBG0191F26Z0", "BBG01TV6YQ66"}   # TRACE only
    assert all(r["issuer_id"] == "I1" and r["venue_mic"] == "TRACE" for r in rows)


def test_trace_records_long():
    recs = [{"tradeReportDate": "2026-10-30", "productCategory": "Investment Grade", "totalSecuritiesTraded": 5123,
             "advances": 2100, "declines": 2500, "unchanged": 300, "fiftyTwoWeekHigh": 12}]   # FINRA API record shape
    rows = T.parse_records("corporateAndAgencyDebtMarketBreadth", recs)
    assert {r["metric"] for r in rows} == {"totalSecuritiesTraded", "advances", "declines", "unchanged", "fiftyTwoWeekHigh"}
    assert rows[0]["category"] == "Investment Grade" and rows[0]["date"] == date(2026, 10, 30)
