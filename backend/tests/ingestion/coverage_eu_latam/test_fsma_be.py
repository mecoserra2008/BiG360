"""coverage-B: FSMA STORI (BE OAM) adapter on recorded fixtures (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.ingestion import fsma_be

FX = Path(__file__).parent / "fixtures"
ISSUER = {"issuer_id": "ITESTUCB00001", "name": "UCB", "country": "BE", "region": "EU",
          "lei": "2138008J191VLSGY5A09", "leis": ["2138008J191VLSGY5A09"], "isin": "BE0003739530",
          "yf_ticker": "UCB.BR", "yf_tickers": ["UCB.BR"], "tickers": [], "fye_month": None}


class _Resp:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


def _items():
    return json.loads((FX / "fsma_ucb_result.json").read_text(encoding="utf-8"))


def test_select():
    fs = fsma_be.select_filings(_items()["storiResultItems"], ISSUER["lei"])
    pk = [f for f in fs if f["doc_url"]]
    assert [f["period_end"] for f in pk] == ["2021-12-31", "2022-12-31", "2023-12-31", "2024-12-31", "2025-12-31"]
    last = pk[-1]
    assert last["suffix"] == ".xbri" and last["language"] == "en"  # English package preferred
    assert last["native_key"] == "4fece3cc-dc46-4f19-9e72-33e333cde384"
    assert last["knowledge_ts"].startswith("2026-02-26T06:00")  # 07:00 Brussels (CET)
    assert last["doc_url"].endswith("fileDataId=4fece3cc-dc46-4f19-9e72-33e333cde384")
    semi = [f for f in fs if f["form_class"] == "semiannual"]
    assert semi and all(f["doc_url"] is None for f in semi)  # PDF half-years -> metadata only
    assert fsma_be.select_filings(_items()["storiResultItems"], "OTHERLEI000000000000") == []


def test_fetch_normalize(tmp_data, monkeypatch):
    a = fsma_be.FsmaBeAdapter(settings=tmp_data, issuers=[ISSUER])
    doc = json.loads((FX / "afm_heia_oim_small.json").read_text(encoding="utf-8"))
    posts, gets = [], []

    def fake_post(url, json_body=None, **kw):
        posts.append(json_body)
        assert url == fsma_be.RESULT and json_body["isinCode"] == "BE0003739530"
        return _Resp(_items())

    def fake_get(url, params=None, **kw):
        gets.append(url)
        return _Resp(b"PK\x03\x04fake")

    monkeypatch.setattr(a.http, "post", fake_post)
    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(a, "to_doc", lambda f, payload: doc)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and len(gets) == 5 and rows["fundamentals_raw"] > 0
    fi = a.normalize(fetched)["filings_index"]
    latest = fi.filter(fi["has_xbrl"]).sort("knowledge_date").row(-1, named=True)
    assert latest["knowledge_date"] == date(2026, 2, 26) and latest["source_entity_id"] == ISSUER["lei"]
    assert latest["form_class"] == "annual" and latest["fiscal_period"] == "FY"


def test_download_rejects_non_zip(tmp_data, monkeypatch):
    a = fsma_be.FsmaBeAdapter(settings=tmp_data, issuers=[ISSUER])
    monkeypatch.setattr(a.http, "get", lambda url, **kw: _Resp(b"<html>error</html>"))
    try:
        a.download({"doc_url": "x"})
    except ValueError as e:
        assert "not a zip" in str(e)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")
