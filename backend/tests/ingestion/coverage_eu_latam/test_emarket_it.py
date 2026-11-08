"""coverage-B: eMarket Storage + 1Info (IT OAMs) adapter on recorded fixtures (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import emarket_it

FX = Path(__file__).parent / "fixtures"
ENEL = {"issuer_id": "ITESTENEL0001", "name": "Enel", "country": "IT", "region": "EU",
        "lei": "WOCMU6HCI0OJWNPRZS33", "leis": ["WOCMU6HCI0OJWNPRZS33"], "isin": "IT0003128367",
        "yf_ticker": "ENEL.MI", "yf_tickers": ["ENEL.MI"], "tickers": [], "fye_month": None}
ENI = {**ENEL, "issuer_id": "ITESTENI00001", "name": "Eni", "lei": "BUCRF72VH5RBN7X3VL35",
       "leis": ["BUCRF72VH5RBN7X3VL35"], "isin": "IT0003132476", "yf_ticker": "ENI.MI", "yf_tickers": ["ENI.MI"]}


class _Resp:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


def test_parse_and_select_emarket():
    rows = emarket_it.parse_emarket((FX / "emarket_enel_afr.html").read_text(encoding="utf-8"))
    assert len(rows) >= 10 and all(r["ts"] for r in rows)
    fs = emarket_it.select_emarket(rows)
    pk = [f for f in fs if f["doc_url"]]
    last = pk[-1]
    # consolidated group package (IT, 18:08 Rome) beats the EN copy (18:11) and the parent-only S.p.A. packages
    assert last["native_key"] == "em:181935" and "consolidato" in last["title"]
    assert last["knowledge_ts"].startswith("2026-04-13T16:08")  # 18:08 CEST
    assert last["doc_url"] == "https://www.emarketstorage.it/sites/default/files/xbrl/2026-04/20260413_181935.zip"
    assert not emarket_it.is_consolidated("Relazione e Bilancio di esercizio di Enel S.p.A. al 31 dicembre 2025")
    assert not emarket_it.is_consolidated("Report and financial statements of Enel S.p.A. at December 31, 2025")
    assert emarket_it.is_consolidated("Bilanci e Relazioni 2025 - Progetto di Bilancio (ESEF)")
    assert emarket_it.is_consolidated("Relazione Annuale Integrata e Bilancio Consolidato 2025 Gruppo Generali")


def test_select_oneinfo():
    data = json.loads((FX / "oneinfo_eni_esef.json").read_text(encoding="utf-8"))["data"]
    fs = emarket_it.select_oneinfo(data)
    assert [f["native_key"] for f in fs][-1] == "1i:164624_oneinfo" and len(fs) == 5
    last = fs[-1]
    assert last["suffix"] == ".xbri" and "year=2025" in last["doc_url"] and "file=164624_oneinfo.xbri" in last["doc_url"]
    assert last["knowledge_ts"].startswith("2026-03-23T18:59")
    form = emarket_it.oneinfo_form(117)
    assert form["SearchFilter[emittente][]"] == "117" and form["columns[2][data]"] == "dataStoccaggio"


@pytest.mark.parametrize("issuer", [ENEL, ENI])
def test_fetch_normalize(tmp_data, monkeypatch, issuer):
    a = emarket_it.EmarketItAdapter(settings=tmp_data, issuers=[issuer])
    doc = json.loads((FX / "afm_heia_oim_small.json").read_text(encoding="utf-8"))
    html = (FX / "emarket_enel_afr.html").read_bytes()
    oneinfo = json.loads((FX / "oneinfo_eni_esef.json").read_text(encoding="utf-8"))
    dl: list[str] = []

    def fake_get(url, params=None, **kw):
        if url == emarket_it.EMARKET_LIST:
            assert params["azienda"] == 1003
            return _Resp(html if params["page"] == 0 else b"<html></html>")
        dl.append(url)
        return _Resp(b"PK\x03\x04fake")

    def fake_post(url, data=None, **kw):
        assert url == emarket_it.ONEINFO_API and data["SearchFilter[emittente][]"] == "117"
        return _Resp(oneinfo)

    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(a.http, "post", fake_post)
    monkeypatch.setattr(a, "to_doc", lambda f, payload: doc)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and dl and rows["fundamentals_raw"] > 0
    fi = a.normalize(fetched)["filings_index"]
    latest = fi.filter(fi["has_xbrl"]).sort("knowledge_date").row(-1, named=True)
    assert latest["source_entity_id"] == issuer["lei"] and latest["issuer_id"] == issuer["issuer_id"]
    assert latest["knowledge_date"] == (date(2026, 4, 13) if issuer is ENEL else date(2026, 3, 23))


def test_unknown_issuer_fails_cleanly(tmp_data):
    a = emarket_it.EmarketItAdapter(settings=tmp_data, issuers=[{**ENEL, "lei": "X" * 20, "leis": ["X" * 20]}])
    with pytest.raises(LookupError):
        a.list_filings(a.universe()[0])
