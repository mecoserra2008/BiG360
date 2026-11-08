"""W3.1-W3.5 parsers (recorded official responses in fixtures/) and derivations (no network)."""

from __future__ import annotations

import io
import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.macro.cb import balance_sheets as bs
from app.macro.cb import curve_sources as cs
from app.macro.cb import projections as pj
from app.macro.cb import rates as rt
from app.macro.cb import stance as st
from app.macro.cb.banks import BANKS, BY_CODE

FX = Path(__file__).parent / "fixtures"


def _txt(name: str) -> str:
    return (FX / name).read_text(encoding="utf-8")


# ----------------------------------------------------------------------------------------------- registry

def test_registry_complete_and_official():
    assert len(BANKS) == 17 and len({b.code for b in BANKS}) == 17
    for b in BANKS:
        assert b.policy and b.bis
        for kind, _ in b.policy:
            assert kind in {"fed_mid", "lake", "ecb", "riksbank", "norges", "bis", "lpr"}
        d = b.to_dict()
        assert d["policy_sources"][-1].startswith("bis:WS_CBPOL.D.")
        assert "yahoo" not in json.dumps(d).lower()
    assert BY_CODE["pboc"].target is None and BY_CODE["fed"].target == 2.0


# ----------------------------------------------------------------------------------------------- W3.1

def test_parse_bis_fixture():
    got = rt.parse_bis_csv(_txt("bis_cbpol.csv"))
    assert set(got) == {"US", "GB"} and all(len(v) >= 5 for v in got.values())
    assert all(isinstance(d, date) and 0 <= v < 20 for d, v in got["US"])


def test_parse_riksbank_and_valet_fixtures():
    rk = rt.parse_riksbank(json.loads(_txt("riksbank.json")))
    assert rk[0] == (date(2025, 9, 22), 2.0) and rk[-1][1] == 1.75
    va = rt.parse_valet(json.loads(_txt("valet.json")))
    assert set(va) == {"V80691344", "BD.CDN.2YR.DQ.YLD"}


def test_parse_lpr():
    j = {"records": [{"1Y": "3.00", "5Y": "3.50", "showDateCN": "2026-09-20"},
                     {"1Y": "3.10", "5Y": "3.60", "showDateCN": "2026-08-20"}]}
    got = rt.parse_lpr(j)
    assert got["1Y"] == [(date(2026, 8, 20), 3.1), (date(2026, 9, 20), 3.0)]


def test_fed_mid_and_merge_priority():
    fred = {"DFEDTARL": [(date(2008, 12, 16), 0.0), (date(2026, 9, 17), 3.75)],
            "DFEDTARU": [(date(2008, 12, 16), 0.25), (date(2026, 9, 17), 4.0)],
            "DFEDTAR": [(date(2008, 10, 29), 1.0), (date(2008, 12, 16), 1.0)]}
    mid = rt.fed_mid(fred)
    assert mid == [(date(2008, 10, 29), 1.0), (date(2008, 12, 16), 0.125), (date(2026, 9, 17), 3.875)]
    m = rt.merge_priority([("own", [(date(2020, 1, 1), 1.0), (date(2020, 6, 1), 1.5)]),
                           ("bis", [(date(2019, 1, 1), 0.5), (date(2020, 3, 1), 9.9), (date(2020, 9, 1), 2.0)])])
    assert [(d, v, n) for d, v, n in m] == [(date(2019, 1, 1), 0.5, "bis"), (date(2020, 1, 1), 1.0, "own"),
                                            (date(2020, 6, 1), 1.5, "own"), (date(2020, 9, 1), 2.0, "bis")]


def test_derive_decisions_attribution_holds_and_intermeeting():
    pairs = [(date(2026, 1, 1), 4.0), (date(2026, 3, 19), 4.25), (date(2026, 4, 10), 4.0), (date(2026, 6, 1), 4.0)]
    meetings = [date(2026, 2, 5), date(2026, 3, 18), date(2026, 5, 6), date(2026, 11, 5)]
    d = rt.derive_decisions(pairs, meetings, today=date(2026, 10, 1))
    by = {r["decision_date"]: r for r in d}
    assert by[date(2026, 3, 18)]["action"] == "hike" and by[date(2026, 3, 18)]["date_basis"] == "official_meeting"
    assert by[date(2026, 3, 18)]["effective_date"] == date(2026, 3, 19)
    assert by[date(2026, 4, 10)]["scheduled"] is False and by[date(2026, 4, 10)]["change_bp"] == -25.0
    assert by[date(2026, 2, 5)]["action"] == "hold" and by[date(2026, 5, 6)]["action"] == "hold"
    assert date(2026, 11, 5) not in by  # future meeting


def test_derive_decisions_without_calendar():
    d = rt.derive_decisions([(date(2020, 1, 1), 1.0), (date(2020, 2, 3), 0.75)], [], today=date(2021, 1, 1))
    assert len(d) == 1 and d[0]["scheduled"] is None and d[0]["date_basis"] == "effective_date"


def test_cycle_phases():
    ch = [{"decision_date": date(2025, 1, 10), "change_bp": 25.0}, {"decision_date": date(2025, 3, 1), "change_bp": -25.0}]
    days = [date(2025, 1, 1), date(2025, 2, 1), date(2025, 4, 1), date(2025, 12, 1)]
    ph = [p for _, p, _, _ in rt.cycle_phases(ch, days)]
    assert ph == ["pausing", "hiking", "cutting", "pausing"]


def test_parse_boe_votes_synthetic_workbook():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Bank Rate Decisions"
    ws.append([None, None, None, "Monetary Policy Committee voting history - Bank Rate"])
    ws.append([None, None, "Current members", "A Member", "B Member", "C Member", "Past members", "D Past"])
    ws.append([None, datetime(2026, 9, 17), 0.0375, 0.0375, 0.04, 0.0375, None, None])
    ws.append([None, datetime(2001, 1, 11), 0.06, None, None, None, None, 0.0575])
    buf = io.BytesIO()
    wb.save(buf)
    v = rt.parse_boe_votes(buf.getvalue())
    assert v[0]["date"] == date(2026, 9, 17) and (v[0]["for"], v[0]["higher"], v[0]["lower"]) == (2, 1, 0)
    assert rt.vote_text(v[0]) == "2-1 (1 for higher)" and v[1]["lower"] == 1
    assert rt.vote_text({"for": 9, "higher": 0, "lower": 0}) == "9-0 (unanimous)"


# ----------------------------------------------------------------------------------------------- W3.2 sources

def test_parse_ecb_nss_and_snb_fixtures():
    nss = cs.parse_ecb_nss(_txt("ecb_yc_nss.csv"))
    assert nss and all(set(p) == {"BETA0", "BETA1", "BETA2", "BETA3", "TAU1", "TAU2"} for p in nss.values())
    snb = cs.parse_snb_spot(_txt("snb_spot.csv"))
    assert snb and all(t <= 10 for p in snb.values() for t, _ in p)


def test_parse_boe_ois_synthetic_workbook():
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "1. fwds, short end"
    ws.append([None, "UK instantaneous OIS forward curve, short end"])
    ws.append(["months:"] + list(range(1, 9)))
    ws.append(["years:"] + [m / 12 for m in range(1, 9)])
    ws.append([datetime(2026, 10, 1)] + [3.8 + 0.05 * m for m in range(8)])
    buf = io.BytesIO()
    wb.save(buf)
    got = cs.parse_boe_ois_fwd(buf.getvalue())
    pts = got[date(2026, 10, 1)]
    assert len(pts) == 8 and pts[0] == pytest.approx((1 / 12, 3.8))


def test_panel_ffill():
    p = cs._panel({"3M": [(date(2026, 9, 30), 2.0)], "2Y": [(date(2026, 10, 1), 3.0), (date(2026, 10, 9), 3.1)]},
                  date(2026, 10, 1), ffill={"3M": 7})
    assert p[date(2026, 10, 1)] == {"2Y": 3.0, "3M": 2.0} and "3M" not in p[date(2026, 10, 9)]


# ----------------------------------------------------------------------------------------------- W3.3 / W3.4 / W3.5

def test_parse_mpd_fixture():
    rows = pj.parse_mpd(_txt("ecb_mpd.csv"), [date(2026, 9, 10)])
    assert rows and {r["variable"] for r in rows} <= {"hicp_inflation", "short_rate_assumption"}
    assert all(r["release_date"] == date(2026, 9, 10) and int(r["horizon"]) >= 2025 for r in rows)


def test_vintage_values_and_fed_rows():
    obs = {"FEDTARMD": [
        {"date": "2026-01-01", "value": "3.6", "realtime_start": "2025-12-10", "realtime_end": "2026-03-17"},
        {"date": "2026-01-01", "value": "3.9", "realtime_start": "2026-03-18", "realtime_end": "9999-12-31"},
        {"date": "2027-01-01", "value": "3.4", "realtime_start": "2025-12-10", "realtime_end": "9999-12-31"}]}
    assert pj.vintage_values(obs["FEDTARMD"], date(2026, 1, 5)) == {"2026": 3.6, "2027": 3.4}

    def fetch(sid):
        if sid in obs:
            return obs[sid]
        raise KeyError(sid)
    rows = pj.fed_rows(fetch)
    latest = [r for r in rows if r["release_date"] == date(2026, 3, 18)]
    assert {(r["horizon"], r["value"]) for r in latest} == {("2026", 3.9), ("2027", 3.4)}


def test_ar1_and_taylor():
    import numpy as np

    rng = np.random.default_rng(0)
    y = [3.0]
    for _ in range(119):  # AR(1) around 2% with phi 0.8
        y.append(2.0 + 0.8 * (y[-1] - 2.0) + rng.normal(0, 0.1))
    f = st.ar1_forecast(y, 12)
    assert f is not None and 1.7 < f < 2.3
    trend = [0.01 * i for i in range(60)]  # explosive fit -> clipped, still finite and near the last value
    assert abs(st.ar1_forecast(trend, 12) - trend[-1]) < 1.0
    assert st.ar1_forecast([1.0] * 10) is None
    # Taylor (1993): 2 + pi + 0.5 (pi - 2) + 0.5 gap ; pi = 3, gap = 1 -> 6.0
    assert st.taylor(3.0, 2.0, 1.0, 2.0) == pytest.approx(6.0)
    assert st.taylor(3.0, 2.0, 1.0, 0.5, gap_w=1.0) == pytest.approx(5.0)
    assert st.taylor(None, 2.0, 1.0, 2.0) is None


def test_parse_oecd_gap():
    txt = "REF_AREA,MEASURE,FREQ,TIME_PERIOD,OBS_VALUE\nUSA,GAP,A,2026,-0.23\nEA17,GAP,A,2026,0.4\nXXX,GAP,A,2026,9\n"
    assert st.parse_oecd_gap(txt) == {"US": {2026: -0.23}, "EA": {2026: 0.4}}


def test_balance_sheet_parsers_and_pace():
    import polars as pl

    ilm = bs.parse_ilm(_txt("ecb_ilm.csv"))
    assert {c for c, _, _ in ilm} <= {"total_assets", "securities_mp"} and ilm
    assert all(d.weekday() == 4 for _, d, _ in ilm)
    iadb = bs.parse_iadb("DATE,SERIES,VALUE\n07 Oct 2026,RPWB56A,649563\n30 Sep 2026,RPWB55A,99214\n")
    assert iadb[0] == ("reserves", date(2026, 10, 7), 649.563)
    boj = bs.parse_boj_json({"RESULTSET": [{"SERIES_CODE": "MABJMTA", "VALUES": {"SURVEY_DATES": [202608, 202609],
                                                                                  "VALUES": [6300000, 6250253]}}]})
    assert boj[-1] == ("total_assets", date(2026, 9, 1), 625025.3)
    df = pl.DataFrame({"bank": ["fed"] * 3, "component": ["total_assets"] * 3,
                       "date": [date(2026, 1, 7), date(2026, 4, 8), date(2026, 7, 8)], "value_bn": [100.0, 91.0, 82.0],
                       "source": ["x"] * 3})
    out = bs.add_pace(df)
    last = out.filter(pl.col("date") == date(2026, 7, 8)).row(0, named=True)
    assert last["change_13w_bn"] == pytest.approx(-9.0) and last["pace_annualized_bn"] == pytest.approx(-9.0 * 365 / 91)
