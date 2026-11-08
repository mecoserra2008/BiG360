"""coverage-B: AFM financial-reporting register (NL OAM) adapter on recorded fixtures (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import afm_nl

FX = Path(__file__).parent / "fixtures"
ISSUER = {"issuer_id": "ITESTHEIA0001", "name": "Heineken", "country": "NL", "region": "EU",
          "lei": "724500K5PTPSST86UQ23", "leis": ["724500K5PTPSST86UQ23"], "isin": "NL0000009165",
          "yf_ticker": "HEIA.AS", "yf_tickers": ["HEIA.AS"], "tickers": [], "fye_month": None}


class _Resp:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else payload.encode()


def _rows():
    return afm_nl.parse_export((FX / "afm_nl_export.xml").read_bytes())


def test_parse_match_select():
    rows = _rows()
    assert rows and all(r["id"] and r["datum"] for r in rows)
    mine = afm_nl.match_rows(rows, ISSUER)
    assert mine and {r["issuer"] for r in mine} == {"Heineken N.V."}  # not Heineken Holding / Ferrari Group
    fs = afm_nl.select_filings(mine)
    pk = [f for f in fs if f["doc_url"]]
    assert pk and all(f["form_class"] == "annual" for f in pk)
    last = pk[-1]
    assert last["native_key"] == "A2510-03613" and last["suffix"] == ".xbri" and last["period_end"] == "2025-12-31"
    # 2/11/2026 12:35:22 PM Amsterdam (CET, UTC+1) -> 11:35:22 UTC
    assert last["knowledge_ts"].startswith("2026-02-11T11:35:22")
    # half-year reports are PDF -> metadata only
    assert any(f["form_class"] == "semiannual" and f["doc_url"] is None for f in fs)
    keys = [(f["form_class"], f["boekjaar"]) for f in fs]
    assert len(keys) == len(set(keys))
    assert afm_nl.parse_datum("7/30/2026 4:44:03 PM").isoformat().startswith("2026-07-30T14:44:03")  # CEST


def test_download_link():
    link = afm_nl.download_link((FX / "afm_nl_details.html").read_text(encoding="utf-8"))
    assert link.startswith("https://www.afm.nl/downloadregisterfile.aspx?type=financiele-verslaggeving&enc=")


def test_fetch_normalize_resume(tmp_data, monkeypatch):
    calls: list[str] = []
    doc = json.loads((FX / "afm_heia_oim_small.json").read_text(encoding="utf-8"))

    def make():
        a = afm_nl.AfmNlAdapter(settings=tmp_data, issuers=[ISSUER])

        def fake_get(url, params=None, **kw):
            calls.append(url)
            if url == afm_nl.EXPORT:
                return _Resp((FX / "afm_nl_export.xml").read_bytes())
            if url.startswith(afm_nl.DETAIL):
                return _Resp((FX / "afm_nl_details.html").read_bytes())
            assert "downloadregisterfile" in url
            return _Resp(b"PK\x03\x04fake")

        monkeypatch.setattr(a.http, "get", fake_get)
        monkeypatch.setattr(a, "to_doc", lambda f, payload: doc)
        return a

    a = make()
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["fundamentals_raw"] > 0
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    latest = fi.filter(fi["has_xbrl"]).sort("knowledge_date").row(-1, named=True)
    assert latest["knowledge_date"] == date(2026, 2, 11) and latest["source_entity_id"] == ISSUER["lei"]
    assert latest["issuer_id"] == "ITESTHEIA0001" and latest["period_end"] == date(2025, 12, 31)
    assert fi.filter(~fi["has_xbrl"]).height >= 1  # PDF half-years as metadata rows
    rev = fr.filter((fr["concept"] == "Revenue") & (fr["period_end"] == date(2025, 12, 31)) & (fr["dimensions_hash"] == ""))
    assert rev.height >= 1 and rev["value"][0] == pytest.approx(34_257_000_000)
    last = fr.filter(fr["filing_id"] == latest["filing_id"])  # (same fake doc is served for every filing)
    assert last.height and (last["knowledge_date"] >= last["period_end"]).all()
    n = sum("downloadregisterfile" in c for c in calls)
    assert n >= 1
    f2 = make().fetch()  # resumable: nothing downloaded again
    assert sum("downloadregisterfile" in c for c in calls) == n and not any(f.get("doc_url") for f in f2.meta["filings"])
