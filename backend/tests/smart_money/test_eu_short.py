"""EU / UK net short-position parsers on recorded register files (reduced copies of the live downloads made
2026-10-19), knowledge-date rules and the contract."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from app.core.schema import conform
from app.ingestion import eu_short as E

FX = Path(__file__).parent / "fixtures"


def rb(name: str) -> bytes:
    return (FX / name).read_bytes()


def test_fca_individual_and_aggregates():
    ind = E.parse_fca_individual(rb("fca_individual.xlsx"))
    assert len(ind) == 15 and all(r["position_type"] == "individual" and r["regulator"] == "fca" for r in ind)
    assert ind[0]["holder_name"] == "AlphaGen Capital Limited" and ind[0]["isin"] == "GB00B09LQS34"
    assert ind[0]["position_pct"] == 0 and ind[0]["below_threshold"]          # closing notification
    assert any(not r["below_threshold"] and r["position_pct"] >= 0.5 for r in ind)
    cur = E.parse_fca_aggregate(rb("fca_agg_current.csv"), True, "u")
    assert cur[0] == {**cur[0], "holder_name": E.AGG, "position_type": "aggregate", "issuer_name": "3I GROUP PLC",
                      "position_pct": 1.1, "position_date": date(2026, 10, 6), "in_current_list": True}
    assert cur[1]["position_pct"] == 0.2 and not cur[1]["below_threshold"]   # aggregates below 0.5% are still published
    hist = E.parse_fca_aggregate(rb("fca_agg_hist.csv"), False, "u")
    assert hist and not any(r["in_current_list"] for r in hist)


def test_amf_publication_dates_and_lei():
    rows = E.parse_amf(rb("amf.csv"), "u")
    assert len(rows) == 24
    r = rows[0]
    assert r["holder_name"] == "12 WEST CAPITAL MANAGEMENT LP" and r["holder_lei"] == "549300CTKKI0FKNCD259"
    assert r["isin"] == "FR0000054470" and r["position_pct"] == 0.35 and r["below_threshold"]
    assert r["position_date"] == date(2015, 10, 6) and r["publication_date"] == date(2015, 10, 7)
    assert r["in_current_list"] is False                                      # has an end of publication date


def test_cnmv_sheets():
    sheets = json.loads(rb("cnmv_sheets.json"))
    rows = E.parse_cnmv_sheets(sheets)
    assert rows and all(r["issuer_lei"] for r in rows)
    cur = [r for r in rows if r["in_current_list"]]
    assert cur and all(r["publication_date"] == date(2026, 10, 16) for r in cur)
    assert all(r["_file_date"] == date(2026, 10, 16) for r in rows)
    assert {r["isin"][:2] for r in rows} == {"ES"}


def test_afm_cbi_fsma_fi_ftno():
    afm = E.parse_afm(rb("afm_current.csv"), True, "u")
    assert afm[0]["holder_name"] == "Bridgewater Associates, LP" and afm[0]["position_pct"] == 0.72
    assert afm[0]["position_date"] == date(2026, 10, 15)
    hist = E.parse_afm(rb("afm_hist.csv"), False, "u")
    assert "Québec" in hist[0]["holder_name"]                                # cp1252 decoded
    cbi = E.parse_cbi(rb("cbi.xlsx"))
    assert cbi and all(r["regulator"] == "cbi" for r in cbi)
    r = next(x for x in cbi if x["holder_name"].startswith("Millennium") and x["position_date"] == date(2026, 5, 28))
    assert abs(r["position_pct"] - 1.05) < 1e-9                               # fractions -> percent
    fs = E.parse_fsma(rb("fsma.csv"))
    assert fs[0]["holder_name"] == "PERTENTO PARTNERS LLP" and fs[0]["position_pct"] == 0.95
    assert fs[0]["position_date"] == date(2026, 10, 15) and fs[0]["in_current_list"]
    se = E.parse_fi_se(rb("fi_se_hist.ods"), False, "u")
    lt = [x for x in se if x["position_pct"] is None]
    assert lt and all(x["below_threshold"] for x in lt)                        # '<0,5'
    se_cur = E.parse_fi_se(rb("fi_se_current.ods"), True, "u")
    assert se_cur and all(x["position_pct"] >= 0.5 for x in se_cur)
    no = E.parse_ftno(json.loads(rb("ftno.json")))
    agg = [x for x in no if x["position_type"] == "aggregate"]
    ind = [x for x in no if x["position_type"] == "individual"]
    assert agg and ind and all(x["holder_name"] == E.AGG for x in agg)
    # a holder that leaves the active list is recorded as a below-threshold row on that date
    assert any(x["below_threshold"] and x["position_pct"] is None for x in ind)


def test_knowledge_dates_and_frame():
    rows = (E.parse_amf(rb("amf.csv"), "u")[:3] + E.parse_cnmv_sheets(json.loads(rb("cnmv_sheets.json")))[:3]
            + E.parse_fi_se(rb("fi_se_current.ods"), True, "u")[:2])
    df = E.to_frame(rows, date(2026, 10, 19), datetime(2026, 10, 19, tzinfo=UTC))
    df = conform(df, "eu_short_positions")
    amf = df.filter(pl.col("regulator") == "amf").row(0, named=True)
    assert amf["knowledge_date"] == amf["publication_date"] and not amf["knowledge_date_estimated"]
    est = df.filter(pl.col("regulator") != "amf")
    assert est["knowledge_date_estimated"].all()
    assert (est["knowledge_date"] >= est["position_date"]).all()             # never before the position
    assert (est["knowledge_date"] <= date(2026, 10, 19)).all()                # never after the download
    cn = df.filter(pl.col("regulator") == "cnmv")
    assert (cn["knowledge_date"] <= date(2026, 10, 16)).all()                 # capped by the CNMV file date
    fi = df.filter(pl.col("regulator") == "fi_se").row(0, named=True)
    assert fi["knowledge_date"] == min(E.add_bdays(fi["position_date"], 1), date(2026, 10, 19))


def test_dedupe_prefers_current_list():
    a = E.row("afm", "H", "X", "NL0000000001", 0.6, date(2026, 10, 1), current=False, url="u")
    b = E.row("afm", "H", "X", "NL0000000001", 0.7, date(2026, 10, 1), current=True, url="u")
    df = E.to_frame([b, a], date(2026, 10, 19), datetime(2026, 10, 19, tzinfo=UTC))
    assert df.height == 1 and df["position_pct"][0] == 0.7


def test_row_validation():
    assert E.row("fca", "H", "X", "not-an-isin", 0.6, date(2026, 1, 1)) is None
    assert E.row("fca", None, "X", "GB00B09LQS34", 0.6, date(2026, 1, 1)) is None   # individual needs a holder
    assert E._num("0,95") == 0.95 and E._num("<0,5") is None and E._num("1.2%") == 1.2
    assert E.add_bdays(date(2026, 10, 16), 1) == date(2026, 10, 19)                  # Friday -> Monday
