"""Macro source parsers on recorded live responses (fixtures captured 2026-10-11; no network)."""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import macro_eu, macro_intl, macro_national
from app.ingestion.macro_common import (
    combine_keys,
    match_key,
    parse_boe_csv_multi,
    parse_boj_multi,
    parse_jsonstat_multi,
    parse_period,
    parse_sdmx_keyed,
    single_dim_groups,
    to_float,
)

FIX = Path(__file__).parent / "fixtures"


def txt(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def js(name: str):  # noqa: ANN201
    return json.loads(txt(name))


def last(pairs):  # noqa: ANN001, ANN201
    return [p for p in pairs if p[1] is not None][-1]


# ------------------------------------------------------------------------------------------------ common


@pytest.mark.parametrize(("s", "d"), [
    ("2026-Q3", date(2026, 7, 1)), ("2026Q3", date(2026, 7, 1)), ("2026-M08", date(2026, 8, 1)),
    ("2026M08", date(2026, 8, 1)), ("2026-08", date(2026, 8, 1)), ("20261007", date(2026, 10, 7)),
    ("202609", date(2026, 9, 1)), ("2026", date(2026, 1, 1)), ("2026 AUG", date(2026, 8, 1)),
    ("Sep.2026", date(2026, 9, 1)), ("Ago.2026", date(2026, 8, 1)), ("Set.2026", date(2026, 9, 1)),
    ("2026-S2", date(2026, 7, 1)), ("2026-06-01/P3M", date(2026, 6, 1)), ("2026-10-08T00:00:00", date(2026, 10, 8)),
])
def test_parse_period(s: str, d: date) -> None:
    assert parse_period(s) == d


def test_to_float_missing_markers() -> None:
    assert [to_float(x) for x in ("1.5", "n.d.", "..", ":", "-", "", None, "1,234.5", "…")] == \
        [1.5, None, None, None, None, None, None, 1234.5, None]


def test_combine_match_and_group_keys() -> None:
    assert combine_keys(["M.US", "M.GB"]) == "M.US+GB"
    assert combine_keys(["A..C", "B..C"]) == "A+B..C"
    data = {"M.US.X": [1], "M.GB.Y": [2]}
    assert match_key(data, "M.GB.Y") == [2] and match_key(data, "M..Y") == [2] and match_key(data, "Q..Y") is None

    class S:
        def __init__(self, k: str) -> None:
            self.req = {"key": k}

    groups = single_dim_groups([S("D.U2.A"), S("D.DE.A"), S("M.U2.B"), S("D.FR.A")])
    assert sorted(len(g) for g in groups) == [1, 3]


# ------------------------------------------------------------------------------------------------ SDMX family


def test_ecb_csvdata_keys_and_missing_rows() -> None:
    yc = parse_sdmx_keyed(txt("ecb_yc_aaa_spot.csv"))
    assert last(yc["B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"]) == (date(2026, 10, 8), pytest.approx(3.51918971))
    ciss = parse_sdmx_keyed(txt("ecb_ciss_daily_with_missing.csv"))
    de = ciss["D.DE.Z0Z.4F.EC.SS_CIN.IDX"]
    assert de[-1] == (date(2026, 10, 9), None)   # empty status-M row kept as null, skipped by consumers
    assert last(de)[0] == date(2026, 10, 8)


def test_bis_csv_attributes_before_time_period() -> None:
    # BIS puts series attributes (UNIT_MEASURE, TITLE...) between the dimensions and TIME_PERIOD -> ndims cut
    pol = parse_sdmx_keyed(txt("bis_cbpol_m.csv"), 2)
    assert last(pol["M.US"]) == (date(2026, 9, 1), 3.875) and last(pol["M.BR"])[1] == 13.75
    tc = parse_sdmx_keyed(txt("bis_total_credit.csv"), 7)
    assert last(tc["Q.US.P.A.M.770.A"]) == (date(2026, 1, 1), 139.7)
    eer = parse_sdmx_keyed(txt("bis_eer.csv"), 4)
    assert any(k.startswith("M.N.B.") for k in eer)


def test_imf_csv_with_multiline_attributes() -> None:
    d = parse_sdmx_keyed(txt("imf_cpi_v21_with_attrs.csv"), 5)
    assert last(d["IND.CPI._T.YOY_PCH_PA_PT.M"]) == (date(2026, 7, 1), pytest.approx(4.44121915820029))


def test_oecd_csvfile() -> None:
    d = parse_sdmx_keyed(txt("oecd_cli_csvfile.csv"), 9)
    assert match_key(d, "JPN.M.CCICP.IX._Z.AA.IX._Z.H")


def test_bundesbank_sdmx_csv_semicolon() -> None:
    d = parse_sdmx_keyed(txt("bundesbank_bbsis_bund_2y10y.csv"), 16)
    r10 = d["D.I.ZST.ZI.EUR.S1311.B.A604.R10XX.R.A.A._Z._Z.A"]
    assert last(r10) == (date(2026, 10, 9), 3.53)
    assert (date(2026, 10, 4), None) in d["D.I.ZST.ZI.EUR.S1311.B.A604.R02XX.R.A.A._Z._Z.A"]   # '.' = missing


def test_abs_and_ine_cl_sdmx_csv() -> None:
    a = parse_sdmx_keyed(txt("abs_sdmx_lf_unemp.csv"), 6)
    assert a["M13.3.1599.20.AUS.M"][0] == (date(2026, 5, 1), pytest.approx(4.39240934))
    c = parse_sdmx_keyed(txt("ine_cl_sdmx_data.csv"), 4)
    assert "_T.M.TDD.AS" in c and all(v is not None for _, v in c["_T.M.TDD.AS"])


def test_banrep_generic_xml() -> None:
    d = macro_national.parse_sdmx_generic_xml((FIX / "banrep_sdmx_genericdata.xml").read_bytes())
    assert last(d["CO.IRCBRM01._Z._Z._Z.PA.D"]) == (date(2026, 10, 9), 12.25)


# ------------------------------------------------------------------------------------------------ Europe


def test_eurostat_jsonstat_multi_geo() -> None:
    d = parse_jsonstat_multi(js("eurostat_hicp_minr_multigeo.json"))
    assert last(d["EA21"]) == (date(2026, 9, 1), 3.8)
    assert last(d["DE"])[1] == 3.3 and last(d["PT"])[1] == 3.6
    q = parse_jsonstat_multi(js("eurostat_namq_10_gdp_multigeo.json"))
    assert last(q["EA21"]) == (date(2026, 4, 1), 0.6)


def test_snb_insee_ine_bpstat() -> None:
    rows = macro_eu.parse_snb_csv(txt("snb_rendeiduebd_conf_yields.csv"))
    assert (date(2026, 9, 22), {"D0": "CHF", "D1": "10J"}, 0.564) in rows
    assert macro_eu.parse_insee_xml((FIX / "insee_cpi_yoy_011814632.xml").read_bytes())["011814632"][-1] == \
        (date(2026, 9, 1), 3.0)
    assert macro_eu.parse_ine_es(js("ine_es_ipc290750_cpi_yoy.json"))[-1] == (date(2026, 9, 1), 4.9)
    bp = macro_eu.parse_bpstat(js("bpstat_pt_tb_yields_2y10y.json"))
    assert bp["12099454"][-1] == (date(2026, 10, 5), 3.24) and bp["12099459"][-1] == (date(2026, 10, 5), 4.0)


def test_snb_dimsel() -> None:
    from app.ingestion.macro_common import GSeries

    mk = lambda d: GSeries("x", "snb", "x", "t", "CH", "rates", "D", "percent", "level", 1, req={"dims": d})  # noqa: E731
    assert macro_eu.snb_dimsel([mk({"D0": "CHF", "D1": "2J"}), mk({"D0": "CHF", "D1": "10J"})]) == "D0(CHF),D1(2J,10J)"


# ------------------------------------------------------------------------------------------------ international


def test_worldbank_v2() -> None:
    d = macro_intl.parse_worldbank(js("wb_v2_indicator_gdp_growth.json"))
    assert d["USA"][-1][0] == date(2025, 1, 1) and d["EMU"] == d["XC"]


def test_pinksheet_layout(tmp_path: Path) -> None:
    """Name row + units row, 'YYYYMmm' periods, U+2026 = n/a, 'Mismatch Details' first sheet, ' **' suffixes."""
    import csv

    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Mismatch Details"
    ws = wb.create_sheet("Monthly Prices")
    for row in csv.reader(io.StringIO(txt("wb_pinksheet_monthly_prices.csv"))):
        ws.append([None if c == "" else (float(c) if c.replace(".", "", 1).isdigit() else c) for c in row])
    ws.append(["2026M10", "…", "…"])
    buf = io.BytesIO()
    wb.save(buf)
    d = macro_intl.parse_pinksheet(buf.getvalue())
    assert d["Crude oil, Brent"][-2] == (date(2026, 9, 1), 116.8)
    assert d["Crude oil, Brent"][-1] == (date(2026, 10, 1), None)
    assert "Coal, South African" in d and "Gold" in d


# ------------------------------------------------------------------------------------------------ national


def test_ust_valet_statcan() -> None:
    u = macro_national.parse_ust_csv(txt("ustreasury_par_yield_curve.csv"))
    assert max(u["10 Yr"]) == (date(2026, 10, 9), 5.24) and max(u["2 Yr"])[1] == 4.8
    v = macro_national.parse_valet(js("boc_valet_observations.json"))
    assert last(v["V39079"]) == (date(2026, 10, 8), 2.25) and last(v["BD.CDN.10YR.DQ.YLD"])[1] == 3.93
    s = macro_national.parse_statcan(js("statcan_wds_vectors_latestN.json"))
    assert last(s["2062815"]) == (date(2026, 9, 1), 6.5) and last(s["41690973"]) == (date(2026, 8, 1), 169.8)


def test_sidra_bcrp_bcra_datos_ar() -> None:
    assert macro_national.parse_sidra(js("ibge_sidra_values.json"))[-1] == (date(2026, 4, 1), 0.5)
    assert macro_national.parse_bcrp(js("bcrp_series_monthly.json"))[-1] == (date(2026, 9, 1), 4.25)
    assert last(macro_national.parse_bcrp(js("bcrp_series_monthly.json"), col=3))[1] == pytest.approx(4.8, abs=0.01)
    bcra = js("bcra_v4_monetarias_variable.json")["results"][0]["detalle"]
    assert bcra[0] == {"fecha": "2026-10-08", "valor": 22.375}
    ar = js("datosgobar_series.json")
    assert ar["data"][0][0] == "2025-08-01" and len(ar["data"][0]) == 3


def test_ons_boe_boj_mof() -> None:
    assert macro_national.parse_ons(js("ons_timeseries_d7g7.json"), "M")[-1] == (date(2026, 8, 1), 3.1)
    b = parse_boe_csv_multi(txt("boe_iadb_multi.csv"))
    assert last(b["IUDBEDR"]) == (date(2026, 10, 8), 3.75)
    assert b["LPMVQJW"][-1][1] is None   # monthly code: blank on daily rows -> the fetcher drops nulls, dates -> month
    q = parse_boj_multi(js("boj_getdatacode_tankan.json"))
    assert q["TK99F1000601GCQ01000"][-1] == (date(2026, 3, 1), 24.0)   # YYYYQQ: 202603 = Q3 (fetcher -> 2026-07-01)
    m = macro_national.parse_mof_csv((FIX / "mof_jgbcme.csv").read_bytes())
    assert m["10Y"][-1] == (date(2026, 10, 8), 3.089) and m["2Y"][-1][1] == 1.936


def test_rba_censtatd_cbc_akshare_periods() -> None:
    r = macro_national.parse_rba_csv(txt("rba_f2-data.csv"))
    assert r["FCMYGBAG10D"][0] == (date(2026, 9, 30), 5.344)
    assert macro_national.parse_cn_period("2026年08月份") == date(2026, 8, 1)
    assert macro_national.parse_cn_period("2026年第1-2季度") == date(2026, 4, 1)
    assert macro_national.parse_cn_period("2026年第1季度") == date(2026, 1, 1)
    assert macro_national.parse_cn_period("202607") == date(2026, 7, 1)
    assert macro_national.parse_cn_period("2026-09-20") == date(2026, 9, 20)


# ------------------------------------------------------------------------------------------------ fetchers end-to-end
# (fake HTTP client: exercises grouping, filtering and period conventions without the network)


class FakeResp:
    def __init__(self, payload: object = None, text: str = "", content: bytes = b"") -> None:
        self._p, self.text, self.content, self.status_code = payload, text, content or text.encode(), 200

    def json(self) -> object:
        return self._p


class FakeHttp:
    def __init__(self, fn) -> None:  # noqa: ANN001
        self.fn, self.n_requests, self.calls = fn, 0, []

    def get(self, url: str, params: dict | None = None, **kw: object) -> FakeResp:
        self.n_requests += 1
        self.calls.append((url, params))
        return self.fn(url, params or {})

    def close(self) -> None:
        pass


def _runner(tmp_data, handler):  # noqa: ANN001, ANN202
    from app.ingestion.macro_common import MacroRunner

    r = MacroRunner("test", tmp_data, date(2020, 1, 1))
    fake = FakeHttp(handler)
    r.http = lambda src, **kw: fake  # type: ignore[method-assign]
    return r, fake


def _spec(sid: str, src: str, freq: str, **req: object):  # noqa: ANN202
    from app.ingestion.macro_common import GSeries

    return GSeries(sid, src, sid, sid, "XX", "rates", freq, "percent", "level", 5, req=dict(req))


def test_censtatd_fetcher_filters_and_quarter_end_months(tmp_data) -> None:  # noqa: ANN001
    payload = {"dataSet": [
        {"freq": "M", "period": "202608", "sv": "CC_CM_1920", "svDesc": "Year-on-year % change", "figure": 1.7},
        {"freq": "M", "period": "202608", "sv": "CC_CM_1920", "svDesc": "Index", "figure": 111.0},
        {"freq": "Q", "period": "202606", "sv": "CON", "figure": 4.3}]}
    r, _ = _runner(tmp_data, lambda u, p: FakeResp(payload))
    cpi = _spec("censtatd:cpi", "censtatd", "M", table="510-60001",
                filters={"sv": "CC_CM_1920", "svDesc": "Year-on-year % change", "freq": "M"})
    gdp = _spec("censtatd:gdp", "censtatd", "Q", table="510-60001", filters={"sv": "CON", "freq": "Q"})
    macro_national.fetch_censtatd(r, [cpi, gdp])
    _, obs = r.finalize()
    got = {(x["series_id"], x["date"]): x["value"] for x in obs.iter_rows(named=True)}
    assert got == {("censtatd:cpi", date(2026, 8, 1)): 1.7, ("censtatd:gdp", date(2026, 4, 1)): 4.3}


def test_hkma_fetcher_records(tmp_data) -> None:  # noqa: ANN001
    payload = js("hkma_hk-interbank-ir-daily.json")
    r, fake = _runner(tmp_data, lambda u, p: FakeResp(payload))
    sp = _spec("hkma:hibor_3m", "hkma", "D", path="x/hk-interbank-ir-daily", field="ir_3m", date_field="end_of_day")
    macro_national.fetch_hkma(r, [sp])
    assert r.report["ok"]["hkma:hibor_3m"] >= 1 and fake.n_requests == 1


def test_boe_fetcher_never_requests_the_iadb(tmp_data) -> None:  # noqa: ANN001
    """A9 2026-11-07 (replaces the IADB CSV fetcher test): BoE robots.txt disallows the IADB, so an IADB-only code is
    frozen without any request (switched routes: tests/ingestion/macro/test_a9_boe_shibor.py)."""
    r, fake = _runner(tmp_data, lambda u, p: FakeResp(text=txt("boe_iadb_multi.csv")))
    m = _spec("boe:LPMVQJW", "boe", "M", code="LPMVQJW")
    macro_national.fetch_boe(r, [m])
    assert fake.n_requests == 0
    assert r.report["skipped_series"]["boe:LPMVQJW"].startswith("source_disallowed")


def test_value_factor_shift_and_actual_only(tmp_data) -> None:  # noqa: ANN001
    r, _ = _runner(tmp_data, lambda u, p: FakeResp())
    frac = _spec("x:frac", "x", "M", value_factor=100)
    r.add(frac, [(date(2026, 8, 1), 0.132)])
    roll = _spec("x:roll", "x", "M", shift_months=2)
    r.add(roll, [(date(2026, 6, 1), 9.6)])   # INE Chile: Jun-Aug rolling quarter labelled June -> August
    weo = _spec("x:weo", "x", "A", actual_only=True)
    r.add(weo, [(date(2024, 1, 1), 1.0), (date(r.today.year, 1, 1), 2.0)])
    _, obs = r.finalize()
    got = {(x["series_id"], x["date"]): x["value"] for x in obs.iter_rows(named=True)}
    assert got[("x:frac", date(2026, 8, 1))] == pytest.approx(13.2)
    assert got[("x:roll", date(2026, 8, 1))] == 9.6
    assert ("x:weo", date(r.today.year, 1, 1)) not in got and got[("x:weo", date(2024, 1, 1))] == 1.0
