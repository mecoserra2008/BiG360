"""coverage-B: CNMV Spain adapter (IFA annual ESEF + semiannual IPP XBRL) on recorded, trimmed fixtures (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.core.http import HttpError
from app.ingestion import cnmv_es

FX = Path(__file__).parent / "fixtures"
TEF = {"issuer_id": "ITESTTEF01", "name": "Telefonica", "country": "ES", "region": "EU",
       "lei": "549300EEJH4FEPDBBR25", "leis": ["549300EEJH4FEPDBBR25"], "isin": "ES0178430E18",
       "yf_ticker": "TEF.MC", "yf_tickers": ["TEF.MC"], "tickers": ["TEF@XMAD"], "fye_month": None}
OIM_SMALL = {"documentInfo": {}, "facts": {
    "r1": {"value": "35120000000", "decimals": -6, "dimensions": {
        "concept": "ifrs-full:Revenue", "entity": "lei:549300EEJH4FEPDBBR25", "unit": "iso4217:EUR",
        "period": "2025-01-01T00:00:00/2026-01-01T00:00:00"}},
    "r2": {"value": "-4152000000", "decimals": -6, "dimensions": {
        "concept": "ifrs-full:ProfitLoss", "entity": "lei:549300EEJH4FEPDBBR25", "unit": "iso4217:EUR",
        "period": "2025-01-01T00:00:00/2026-01-01T00:00:00"}}}}


def _b(name: str) -> bytes:
    return (FX / name).read_bytes()


class _Resp:
    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode("utf-8", "replace")


def _adapter(tmp_data, monkeypatch, calls, **kw):
    a = cnmv_es.CnmvEsAdapter(settings=tmp_data, issuers=[TEF], **kw)

    def fake_get(url, params=None, cache_ttl=None, headers=None, **k):
        calls.append(url)
        assert headers and "Mozilla" in headers["User-Agent"]  # browser UA (WAF)
        if "ListadoIFA" in url:
            assert "nif=A28015865" in url
            return _Resp(_b("cnmv_tef_ifa.html"))
        if "ListaIFI" in url:
            return _Resp(_b("cnmv_tef_ifi.html"))
        if "detalleifialdia" in url:
            return _Resp(_b("cnmv_ifi_detail.html"))
        if "descargaxbrlipp" in url:
            return _Resp(_b("cnmv_ipp_tef_h2_2025_small.xml"))
        if "verdocumento" in url:
            return _Resp(b"PK\x03\x04fake-esef-package")
        raise AssertionError(url)

    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(cnmv_es.PackageFilingAdapter, "to_doc", lambda self, f, payload: OIM_SMALL)
    return a


def test_parse_list_pages():
    ifa = cnmv_es.parse_ifa(_b("cnmv_tef_ifa.html"), "A28015865")
    assert [f["period_end"] for f in ifa] == ["2025-12-31", "2024-12-31"]  # the 1986 row has no ZIP/Xbri link
    assert ifa[0]["knowledge_date"] == "2026-02-24" and ifa[0]["doc_url"].startswith(
        "https://www.cnmv.es/webservices/verdocumento/ver?e=") and "%2f" in ifa[0]["doc_url"]  # token kept encoded
    assert ifa[0]["native_key"] == "ifa:A28015865:20868" and ifa[0]["form_class"] == "annual"
    ipp = cnmv_es.parse_ifi(_b("cnmv_tef_ifi.html"), "A28015865")
    assert [(f["knowledge_date"], f["fiscal_period"]) for f in ipp] == [
        ("2009-02-26", "FY"), ("2026-02-24", "FY"), ("2026-07-29", "H1")]  # quarterly row skipped
    assert all(f["form_class"] == "semiannual" and f["doc_url"].endswith(f["native_key"].rsplit(":", 1)[1])
               for f in ipp)
    inditex = cnmv_es.parse_ifi(b'<table><tr><td><a href="../../aldia/detalleifialdia.aspx?nreg=2026036830">'
                                b'11/03/2026</a></td><td>II   half-year  of 2025-2026  individual and consolidated'
                                b'</td></tr></table>', "A-15075062")
    assert inditex[0]["fiscal_period"] == "FY" and inditex[0]["native_key"] == "ipp:A15075062:2026036830"


def test_parse_ipp_2019_general():
    doc = cnmv_es.parse_ipp(cnmv_es.strip_large_text(_b("cnmv_ipp_tef_h2_2025_small.xml")))
    info = doc["documentInfo"]["cnmv_ipp"]
    assert info["scheme"] == "ge" and info["period_end"] == "2025-12-31" and info["period_start"] == "2025-01-01"
    rows = cnmv_es.ipp_fact_rows(doc, source_id="cnmv_es", source_entity_id="L", filing_id="cnmv_es:x",
                                 form_type="IPP-H2", knowledge_date=date(2026, 2, 24), fye_month=None, filing_fp="FY")
    by = {(r["concept"], r["period_end"], r["period_start"]): r for r in rows}
    rev = by[("cnmv_ipp:I1205", date(2025, 12, 31), date(2025, 1, 1))]
    assert rev["value"] == pytest.approx(35_119_611_000) and rev["unit"] == "EUR" and rev["fiscal_period"] == "FY"
    assert rev["taxonomy"] == "other" and rev["consolidated"] and rev["dimensions_hash"] == ""
    assert "cifra de negocios" in rev["label"] and rev["fiscal_year"] == 2025
    assert by[("cnmv_ipp:I1300", date(2025, 12, 31), date(2025, 1, 1))]["value"] == pytest.approx(-4_318_112_000)
    assert by[("cnmv_ipp:I1100", date(2025, 12, 31), None)]["fiscal_period"] == "FY"
    assert by[("cnmv_ipp:I8435", date(2025, 12, 31), date(2025, 1, 1))]["value"] == pytest.approx(9_721_905_000)
    assert by[("cnmv_ipp:I1290", date(2025, 12, 31), date(2025, 1, 1))]["unit"] == "EUR/shares"
    concepts = {r["concept"] for r in rows}
    assert "cnmv_ipp:I0205" not in concepts            # individual (parent-company) statement skipped
    assert "cnmv_ipp:I2210" not in concepts            # breakdown table outside the primary statements
    assert "cnmv_ipp:I1435" not in concepts            # unfilled indirect cash flow (zeros) dropped
    assert not any(r["period_start"] == date(2025, 7, 1) for r in rows)  # standalone-H2 column skipped
    assert all(r["knowledge_date"] >= r["period_end"] for r in rows)


def test_parse_ipp_2019_bank_and_2008():
    doc = cnmv_es.parse_ipp(_b("cnmv_ipp_san_h2_2019_small.xml"))
    assert doc["documentInfo"]["cnmv_ipp"]["scheme"] == "en"
    f = {(x["dimensions"]["concept"], x["dimensions"]["period"]): x for x in doc["facts"].values()}
    assert f[("cnmv_ipp_en:I1150", "2019-12-31")]["value"] == pytest.approx(1_522_695_732_000)
    assert f[("cnmv_ipp_en:I1572", "2019-01-01/2019-12-31")]["value"] == pytest.approx(6_515_309_000)
    old = cnmv_es.parse_ipp(_b("cnmv_ipp_tef_h2_2015_small.xml"))
    assert old["documentInfo"]["cnmv_ipp"]["scheme"] == "2008-ge"
    g = {(x["dimensions"]["concept"], x["dimensions"]["period"]): x for x in old["facts"].values()}
    assert g[("cnmv_ipp08:ifrs-gp.RevenueTotalByNature", "2015-01-01/2015-12-31")]["value"] == pytest.approx(
        47_218_758_000)
    eps = g[("cnmv_ipp08:ifrs-gp.BasicEarningsLossPerShare", "2015-01-01/2015-12-31")]
    assert eps["value"] == pytest.approx(0.51) and eps["dimensions"]["unit"] == "EUR/shares"
    # P&L depreciation (negative) wins over the cash-flow add-back; the individual statement is never used
    assert g[("cnmv_ipp08:ifrs-gp.DepreciationAndAmortisation", "2015-01-01/2015-12-31")]["value"] < 0
    assert not any("Nacional" in c for c, _ in g)


def test_fetch_normalize_resume(tmp_data, monkeypatch):
    calls: list[str] = []
    a = _adapter(tmp_data, monkeypatch, calls, history=True)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and not fetched.entities_failed
    assert rows["filings_index"] == 5 and rows["fundamentals_raw"] > 0
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    assert set(fi["form_class"]) == {"annual", "semiannual"} and (fi["source_entity_id"] == TEF["lei"]).all()
    assert (fi["issuer_id"] == "ITESTTEF01").all() and fi["knowledge_ts"].is_null().all()
    ipp = fr.filter((fr["concept"] == "cnmv_ipp:I1205") & (fr["period_end"] == date(2025, 12, 31)))
    assert ipp.height >= 1 and ipp["knowledge_date"].min() == date(2026, 2, 24)
    esef = fr.filter(fr["concept"] == "Revenue")
    assert esef.height and esef["taxonomy"][0] == "ifrs-full" and esef["value"][0] == pytest.approx(35.12e9)
    # PIT (the fakes serve the same document for every filing, so check the filings it really belongs to)
    real = fr.filter(fr["filing_id"].is_in(["cnmv_es:ifa:A28015865:20868", "cnmv_es:ipp:A28015865:2026028810"]))
    assert real.height and (real["knowledge_date"] >= real["period_end"]).all()
    assert fr.filter(fr["filing_id"] == "cnmv_es:ipp:A28015865:2009021368").is_empty()  # future periods dropped
    st = json.loads((tmp_data.data_dir / "state" / "cnmv_es.json").read_text(encoding="utf-8"))
    assert len(st["done"]) == 5
    n = len(calls)
    a2 = _adapter(tmp_data, monkeypatch, calls, history=True)
    f2 = a2.fetch()
    assert not f2.meta["filings"] and all("verdocumento" not in u and "descarga" not in u for u in calls[n:])


def test_incremental_skips_old_ipp(tmp_data, monkeypatch):
    calls: list[str] = []
    a = _adapter(tmp_data, monkeypatch, calls, esef=False, recent_days=100000)
    assert len(a.list_filings(a.universe()[0])) == 3
    a = _adapter(tmp_data, monkeypatch, calls, esef=False, recent_days=1)
    assert a.list_filings({**TEF, "nif": "A28015865"}) == []


def test_waf_circuit_breaker(tmp_data, monkeypatch):
    a = cnmv_es.CnmvEsAdapter(settings=tmp_data, issuers=[TEF])
    a.waf_backoff_s = 0
    n = []

    def deny(url, **k):
        n.append(url)
        raise HttpError(403, url, "CVFE")

    monkeypatch.setattr(a.http, "get", deny)
    with pytest.raises(cnmv_es.CnmvBlocked):
        a.list_filings(a.universe()[0])
    with pytest.raises(cnmv_es.CnmvBlocked):
        a.list_filings(a.universe()[0])
    assert len(n) == 2  # one retry, then no more requests


def test_nif_map():
    assert cnmv_es.CnmvEsAdapter.nif_for({"lei": "9598004A3FTY3TEHHN09"}) == ("A-84236934", None)
    assert cnmv_es.CnmvEsAdapter.nif_for({"lei": "549300TTCXZOGZM2EY83"}) == ("A-15075062", 1)
    assert cnmv_es.CnmvEsAdapter.nif_for({"lei": None, "isin": "ES0113900J37"})[0] == "A39000013"
    assert cnmv_es.ipp_labels()["en"]["I1150"] == "TOTAL ACTIVO"


@pytest.mark.network
def test_live_ifi_list():
    a = cnmv_es.CnmvEsAdapter(esef=False, history=True)
    fs = a.list_filings({**TEF, "nif": "A28015865"})
    assert len(fs) >= 35 and min(f["knowledge_date"] for f in fs) <= "2009-03-01"


def test_parse_ipp_2016_amount_vs_percentage_columns():
    doc = cnmv_es.parse_ipp(_b("cnmv_ipp_tef_h2_2016_small.xml"))
    f = {(x["dimensions"]["concept"], x["dimensions"]["period"]): x["value"] for x in doc["facts"].values()}
    assert doc["documentInfo"]["cnmv_ipp"]["scheme"] == "ge"
    assert f[("cnmv_ipp:I1205", "2016-01-01/2016-12-31")] == pytest.approx(52_036_241_000)  # Importe column
    assert f[("cnmv_ipp:I1100", "2016-12-31")] == pytest.approx(123_640_778_000)
    assert all(v > 1000 or v < -1000 for (c, _), v in f.items() if c != "cnmv_ipp:I1290")  # no % column values
