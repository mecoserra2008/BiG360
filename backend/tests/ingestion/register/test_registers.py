"""Coverage v2 W2: register parsers (FIRDS, ISO 10383, ESMA RM list, EDGAR index), register builders and the
universe mapping, on recorded fixtures (no network)."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date
from pathlib import Path

import polars as pl
import pytest

from app.ingestion import firds as F
from app.ingestion import regulatory_register as R
from app.ingestion.sec_index import parse_master_idx

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "register"
TODAY = date(2026, 10, 31)


def _firds_zip(tmp_path: Path) -> Path:
    p = tmp_path / "FULINS_E_20261031_01of01.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("FULINS_E_20261031_01of01.xml", (FIX / "firds_fulins_e_sample.xml").read_bytes())
    return p


@pytest.fixture()
def mics() -> pl.DataFrame:
    esma = F.parse_esma_rm(json.loads((FIX / "esma_upreg_mir.json").read_text())["response"]["docs"])
    return F.parse_mic_csv((FIX / "iso10383_sample.csv").read_bytes(), esma)


def test_parse_firds_zip_keeps_equity_venue_records(tmp_path):
    df = F.parse_firds_zip(_firds_zip(tmp_path))
    assert df.height == 9                      # the bond (CFI D*) is dropped, the DR (ED) is kept
    sap = df.filter(pl.col("isin") == "DE0007164600").sort("venue_mic")
    assert sap["venue_mic"].to_list() == ["FRAA", "XETA", "XGAT"]
    xeta = sap.filter(pl.col("venue_mic") == "XETA").row(0, named=True)
    assert xeta["issuer_request"] is True and xeta["issuer_lei"] == "529900D6BF99LW9R2E68"
    assert xeta["admission_approved"] == date(1988, 4, 3) and xeta["relevant_venue"] == "FRAA"
    assert xeta["relevant_authority"] == "DE" and xeta["cfi"] == "ESVUFB"
    shell = df.filter(pl.col("isin") == "GB00BP6MXD84").row(0, named=True)
    assert shell["termination"] == date(9999, 12, 31)          # open-ended termination date


def test_mic_registry_classification(mics):
    rm = dict(zip(mics["mic"].to_list(), mics["is_rm"].to_list(), strict=True))
    assert rm["XETA"] and rm["FRAA"] and rm["XPAR"] and rm["XLON"]
    assert not rm["XETR"] and not rm["XFRA"] and not rm["ALXP"] and not rm["AIMX"] and not rm["XGAT"]
    assert rm["XZAG"]                                           # ISO says OTHR, ESMA lists it as a regulated market
    assert F.parse_esma_rm([{"ae_micLeiEsmaId": "OLDX", "ae_status": "Withdrawn"}]) == set()


def test_rm_admissions_issuer_request_live_equity(tmp_path, mics):
    df = F.parse_firds_zip(_firds_zip(tmp_path))
    eu = F.rm_admissions(df, mics, TODAY, countries=F.EEA)
    assert set(eu["issuer_lei"].unique().to_list()) == {"529900D6BF99LW9R2E68", "529900S21EQ1BO4ESM68"}
    # MTF (ALXP), terminated (XAMS 2024), depositary receipt (ED) and the non-requested XGAT line are out
    assert "XGAT" not in eu["venue_mic"].to_list() and "969500GROWTHCO000001" not in eu["issuer_lei"].to_list()
    loose = F.rm_admissions(df, mics, TODAY, countries=F.EEA, issuer_request_only=False)
    assert loose.height >= eu.height
    uk = F.rm_admissions(df, mics, TODAY, countries=frozenset({"GB"}))
    assert uk["issuer_lei"].to_list() == ["21380068P1DRHMJ8KU70"]
    aim = R.aim_admissions(df, TODAY)
    assert aim["issuer_lei"].to_list() == ["2138AIMCOAIMCO000001"]


def test_build_firds_register_with_gleif(tmp_path, mics):
    df = F.parse_firds_zip(_firds_zip(tmp_path))
    adm = F.rm_admissions(df, mics, TODAY, countries=F.EEA)
    gleif = pl.DataFrame({"lei": ["529900D6BF99LW9R2E68"], "legal_name": ["SAP SE"], "country": ["DE"],
                          "jurisdiction": ["DE"], "registered_as": ["HRB 719915"]})
    reg = R.build_firds_register(adm, "esma_rm", "EU", gleif, TODAY, "ESMA FULINS_E 20261031")
    sap = reg.filter(pl.col("issuer_key") == "529900D6BF99LW9R2E68").row(0, named=True)
    assert sap["issuer_name"] == "SAP SE" and sap["company_number"] == "HRB 719915" and sap["country"] == "DE"
    assert sap["instruments"] == ["DE0007164600"] and set(sap["venues"]) == {"XETA", "FRAA"}
    assert sap["primary_venue"] == "FRAA" and sap["first_admission"] == date(1988, 4, 3)
    assert sap["status"] == "active" and sap["market"] == "EU" and sap["competent_authority"] == "DE"
    tot = reg.filter(pl.col("issuer_key") == "529900S21EQ1BO4ESM68").row(0, named=True)
    assert tot["issuer_name"] == "TOTALENERGIES SE"            # no GLEIF record -> FIRDS instrument name


def test_us_register_from_edgar_index():
    ents = [e for e in parse_master_idx((FIX / "edgar_master_sample.idx").read_text()) if e["form"] in
            R.PERIODIC_FORMS | R.FUND_FORMS | R.NEW_REGISTRANT_FORMS]
    per = R.periodic_summary(pl.DataFrame([{"cik": e["cik"], "form": e["form"], "filed": e["filed"]} for e in ents])
                             .with_columns(pl.col("filed").str.to_date()))
    tk = pl.DataFrame({"cik": ["0000320193", "0001000001", "0001000002", "0001000003", "0001000004", "0009999999"],
                       "name": ["Apple Inc.", "Some Fund", "Newco", "FPI plc", "Oldco", "Pink Sheet Co"],
                       "ticker": ["AAPL", "SFND", "NEWC", "FPIY", "OLDC", "PINK"],
                       "exchange": ["Nasdaq", "NYSE", "Nasdaq", "NYSE", "NYSE", "OTC"],
                       "mic": ["XNAS", "XNYS", "XNAS", "XNYS", "XNYS", "OTCM"], "sec_rank": [0, 1, 2, 3, 4, 5]})
    reg = R.build_us(tk, per, TODAY, "test")
    st = dict(zip(reg["cik"].to_list(), zip(reg["status"].to_list(), reg["status_reason"].to_list(), strict=True), strict=True))
    assert st["0000320193"] == ("active", None)
    assert st["0001000003"][0] == "active"
    assert st["0001000001"][1].startswith("investment company")
    assert st["0001000002"][1].startswith("new registrant")
    assert st["0001000004"][1].startswith("no periodic report since")
    assert st["0009999999"][1].startswith("OTC quotation only")
    aapl = reg.filter(pl.col("cik") == "0000320193").row(0, named=True)
    assert aapl["filer_category"] == "domestic" and aapl["last_annual_form"] == "10-K"
    assert aapl["last_interim_filed"] == date(2026, 8, 1) and aapl["venues"] == ["XNAS"]
    fpi = reg.filter(pl.col("cik") == "0001000003").row(0, named=True)
    assert fpi["filer_category"] == "foreign_private_20f"


def test_quarters_back():
    assert R.quarters_back(date(2026, 10, 31), 3) == [(2026, 2), (2026, 3), (2026, 4)]
    assert R.quarters_back(date(2026, 2, 1), 2) == [(2025, 4), (2026, 1)]


def test_resolve_to_universe_cik_lei_isin():
    reg = pl.DataFrame([
        {**{k: None for k in R.REGISTER_SCHEMA}, "market": "US", "register": "sec_listed", "issuer_key": "0000320193",
         "issuer_name": "Apple", "cik": "0000320193", "status": "active", "as_of": TODAY},
        {**{k: None for k in R.REGISTER_SCHEMA}, "market": "EU", "register": "esma_rm", "issuer_key": "LEI_SAP",
         "issuer_name": "SAP", "lei": "LEI_SAP", "instruments": ["DE0007164600"], "status": "active", "as_of": TODAY},
        {**{k: None for k in R.REGISTER_SCHEMA}, "market": "EU", "register": "esma_rm", "issuer_key": "LEI_X",
         "issuer_name": "X", "lei": "LEI_X", "instruments": ["FR0000000001", "FR0000000002"], "status": "active",
         "as_of": TODAY},
        {**{k: None for k in R.REGISTER_SCHEMA}, "market": "UK", "register": "lse_aim", "issuer_key": "LEI_NEW",
         "issuer_name": "New", "lei": "LEI_NEW", "instruments": ["GB0000000001"], "status": "active", "as_of": TODAY},
    ], schema=R.REGISTER_SCHEMA)
    idm = pl.DataFrame({"id_type": ["cik", "lei", "isin"], "id_value": ["0000320193", "LEI_SAP", "FR0000000002"],
                        "issuer_id": ["I_AAPL", "I_SAP", "I_X"], "confidence": [1.0, 1.0, 1.0],
                        "valid_to": [None, None, None]}, schema_overrides={"valid_to": pl.Date})
    secs = pl.DataFrame({"security_id": ["S_AAPL", "S_SAP", "S_X", "S_X2"], "issuer_id": ["I_AAPL", "I_SAP", "I_X", "I_X"],
                         "is_primary_security": [True, True, True, False], "is_active": [True, True, True, True]})
    out = R.resolve_to_universe(reg, idm, secs)
    got = {r["issuer_key"]: (r["issuer_id"], r["security_id"], r["match_method"]) for r in out.iter_rows(named=True)}
    assert got["0000320193"] == ("I_AAPL", "S_AAPL", "cik")
    assert got["LEI_SAP"] == ("I_SAP", "S_SAP", "lei")
    assert got["LEI_X"] == ("I_X", "S_X", "isin")
    assert got["LEI_NEW"] == (None, None, "none")


def test_carry_removed_keeps_leavers():
    base = {k: None for k in R.REGISTER_SCHEMA}
    prev = pl.DataFrame([{**base, "market": "EU", "register": "esma_rm", "issuer_key": "A", "issuer_name": "A",
                          "status": "active", "as_of": date(2026, 10, 24)},
                         {**base, "market": "EU", "register": "esma_rm", "issuer_key": "B", "issuer_name": "B",
                          "status": "active", "as_of": date(2026, 10, 24)}], schema=R.REGISTER_SCHEMA)
    new = prev.filter(pl.col("issuer_key") == "A").with_columns(pl.lit(TODAY).alias("as_of"))
    out = R.carry_removed(new, prev, TODAY)
    b = out.filter(pl.col("issuer_key") == "B").row(0, named=True)
    assert b["status"] == "removed" and b["as_of"] == TODAY and "not in the register" in b["status_reason"]
    old = out.with_columns(pl.lit(date(2025, 1, 1)).alias("as_of"))
    assert R.carry_removed(new, old.filter(pl.col("issuer_key") == "B"), TODAY).height == 1   # expired leaver dropped


def test_pick_listing_prefers_home_and_wider_types():
    recs = [{"marketSector": "Equity", "securityType": "Preference", "exchCode": "GR", "ticker": "STO3"},
            {"marketSector": "Equity", "securityType": "Preference", "exchCode": "GF", "ticker": "STO3"},
            {"marketSector": "Equity", "securityType": "Preference", "exchCode": "GY", "ticker": "STO3"}]
    ex, rec, cands = R.pick_listing(recs, "DE", "XETA", "EU")
    assert ex == "GY" and cands == ["STO3.DE", "STO3.F"]
    recs2 = [{"marketSector": "Equity", "securityType": "Common Stock", "exchCode": "GD", "ticker": "RKB"}]
    ex2, _, c2 = R.pick_listing(recs2, "DE", "DUSA", "EU")
    assert ex2 == "GD" and c2[0] == "RKB.F" and "RKB.DU" in c2
    trust = [{"marketSector": "Equity", "securityType": "Closed-End Fund", "exchCode": "LN", "ticker": "ATST"},
             {"marketSector": "Equity", "securityType": "Open-End Fund", "exchCode": "LN", "ticker": "XXX"}]
    ex3, rec3, c3 = R.pick_listing(trust, "GB", "XLON", "UK")
    assert ex3 == "LN" and rec3["ticker"] == "ATST" and c3 == ["ATST.L"]
    assert R.pick_listing([{"marketSector": "Equity", "securityType": "Common Stock", "exchCode": "BU", "ticker": "SO"}],
                          "BG", "XBUL", "EU") is None


def test_no_price_seed_uses_local_exchange_ticker():
    from app.normalize.identity_figi import _job_key

    r = {**{k: None for k in R.REGISTER_SCHEMA}, "market": "EU", "register": "esma_rm", "issuer_key": "LEI_HR",
         "issuer_name": "Podravka d.d.", "lei": "LEI_HR", "instruments": ["HRPODRRA0004"], "country": "HR",
         "source_file": "ESMA"}
    figi = {_job_key({"idType": "ID_ISIN", "idValue": "HRPODRRA0004"}): [
        {"exchCode": "XF", "ticker": "PODRHRK"}, {"exchCode": "CZ", "ticker": "PODR"}]}
    s = R.no_price_seed(r, figi, "XZAG")
    assert s["ticker"] == "PODR" and s["mic"] == "XZAG" and s["yf_ticker"] is None and s["region"] == "EU"
    assert s["coverage_source"].endswith(":no_price") and s["lei"] == "LEI_HR"


def test_us_records_keep_texas_listed_stocks():
    """Massive has no primary_exchange for TXSE-listed stocks: the SEC exchange MIC keeps them in the universe."""
    from app.normalize.identity import _us_records

    sec = pl.DataFrame({"cik": ["0001276187", "0000320193"], "name": ["Energy Transfer LP", "Apple Inc."],
                        "ticker": ["ET", "AAPL"], "exchange": ["TXSE", "Nasdaq"], "mic": ["XTXS", "XNAS"],
                        "sec_rank": [50, 0]})
    poly = pl.DataFrame({"ticker": ["ET", "AAPL", "ZZZ"], "name": ["Energy Transfer", "Apple", "Nowhere"],
                         "primary_exchange": [None, "XNAS", None], "type": ["CS", "CS", "CS"], "cik": [None, None, None],
                         "composite_figi": [None, None, None], "share_class_figi": [None, None, None],
                         "currency_name": ["usd", "usd", "usd"]}, schema_overrides={"primary_exchange": pl.Utf8})
    recs = _us_records(sec, poly, pl.DataFrame())
    got = {r["local_symbol"]: (r["mic"], r["cik"]) for r in recs}
    assert got == {"ET": ("XTXS", "0001276187"), "AAPL": ("XNAS", "0000320193")}


def test_us_non_equity_only_issuers_are_excluded():
    base = {k: None for k in R.REGISTER_SCHEMA}
    reg = pl.DataFrame([{**base, "market": "US", "register": "sec_listed", "issuer_key": "A", "issuer_name": "Utility Sub",
                         "tickers": ["EAI"], "status": "active", "as_of": TODAY},
                        {**base, "market": "US", "register": "sec_listed", "issuer_key": "B", "issuer_name": "Bank",
                         "tickers": ["TCBI", "TCBI-PB"], "status": "active", "as_of": TODAY},
                        {**base, "market": "US", "register": "sec_listed", "issuer_key": "C", "issuer_name": "Unknown",
                         "tickers": ["NEWX"], "status": "active", "as_of": TODAY}], schema=R.REGISTER_SCHEMA)
    out = R.apply_listing_types(reg, {"EAI": "SP", "TCBI": "CS", "TCBI-PB": "PFD"})
    st = dict(zip(out["issuer_key"].to_list(), out["status"].to_list(), strict=True))
    assert st == {"A": "excluded", "B": "active", "C": "active"}
    assert out.filter(pl.col("issuer_key") == "A")["status_reason"][0].startswith("no listed common equity")
