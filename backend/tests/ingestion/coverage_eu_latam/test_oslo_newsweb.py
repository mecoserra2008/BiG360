"""coverage-B: Oslo Børs NewsWeb (NO OAM) ESEF adapter on recorded fixtures (no network)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import oslo_newsweb as on

FX = Path(__file__).parent / "fixtures"
DNB = {"issuer_id": "ITESTDNB00001", "name": "DNB Bank", "country": "NO", "region": "EU",
       "lei": "549300GKFG0RYRRQ1414", "leis": ["549300GKFG0RYRRQ1414"], "isin": "NO0010161896",
       "yf_ticker": "DNB.OL", "yf_tickers": ["DNB.OL"], "tickers": ["DNB@XOSL"], "fye_month": None}


class _Resp:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


def _adapter(tmp_data, monkeypatch, calls):
    a = on.OsloNewswebAdapter(settings=tmp_data, issuers=[DNB])
    fx = json.loads((FX / "oslo_dnb_messages.json").read_text(encoding="utf-8"))
    doc = json.loads((FX / "oslo_dnb_oim_small.json").read_text(encoding="utf-8"))

    def fake_get(url, params=None, headers=None, **kw):
        calls.append(url)
        assert headers and headers["Origin"] == "https://newsweb.oslobors.no"
        if url.endswith("/list"):
            assert params["issuer"] == "DNB" and params["category"] == on.CATEGORY_AFR
            return _Resp(fx["list"])
        if url.endswith("/message"):
            return _Resp({"data": {"message": fx["messages"][str(params["messageId"])]}})
        assert "/attachment?messageId=" in url
        return _Resp(b"PK-fake-package")

    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(a, "to_doc", lambda f, payload: doc)
    return a


def test_helpers():
    assert on.parse_ts("2026-03-11T13:00:25.656Z").isoformat().startswith("2026-03-11T13:00:25.656")
    assert on.period_hint("dnbbankasa_2025_12_31_1_nb_1_.zip") == "2025-12-31"
    att = on.pick_attachment([{"id": 1, "name": "annual_report.pdf"}, {"id": 2, "name": "dnb-2025-12-31-nb.zip"}])
    assert att["id"] == 2 and on.pick_attachment([{"id": 1, "name": "a.pdf"}]) is None
    assert on.OsloNewswebAdapter.issuer_sign(None, {**DNB, "leis": [], "lei": None}) == "DNB"  # XOSL ticker fallback


def test_list_one_per_year_earliest(tmp_data, monkeypatch):
    a = _adapter(tmp_data, monkeypatch, [])
    fs = a.list_filings(DNB)
    assert [f["knowledge_ts"][:10] for f in fs] == ["2024-03-14", "2025-03-19", "2026-03-11"]
    last = fs[-1]
    # Norwegian release (13:00:25) precedes the English one (13:00:28): earliest publication wins (PIT)
    assert last["native_key"].startswith("668027-") and last["knowledge_ts"].startswith("2026-03-11T13:00:25")
    assert last["doc_url"].startswith(on.BASE + "/attachment?messageId=668027&attachmentId=")
    assert last["language"] == "nb" and last["form_class"] == "annual" and last["fiscal_period"] == "FY"


def test_fetch_normalize_resume(tmp_data, monkeypatch):
    calls: list[str] = []
    a = _adapter(tmp_data, monkeypatch, calls)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["filings_index"] == 3 and rows["fundamentals_raw"] > 0
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    latest = fi.sort("knowledge_date").row(-1, named=True)
    assert latest["knowledge_date"] == date(2026, 3, 11) and latest["period_end"] == date(2025, 12, 31)
    assert latest["source_entity_id"] == DNB["lei"] and latest["issuer_id"] == "ITESTDNB00001"
    pl_ = fr.filter((fr["concept"] == "ProfitLoss") & (fr["period_end"] == date(2025, 12, 31)) & (fr["dimensions_hash"] == ""))
    assert pl_.height >= 1 and pl_["unit"][0] == "NOK"
    last = fr.filter(fr["filing_id"] == latest["filing_id"])  # (the one fixture doc is served for every filing)
    assert last.height and (last["knowledge_date"] >= last["period_end"]).all()
    n = sum("/attachment" in c for c in calls)
    a2 = _adapter(tmp_data, monkeypatch, calls)
    assert not a2.fetch().meta["filings"] and sum("/attachment" in c for c in calls) == n


@pytest.mark.network
def test_live_listing_dnb():
    fs = on.OsloNewswebAdapter(issuers=[DNB]).list_filings(DNB)
    assert any(f["period_hint"] == "2025-12-31" for f in fs)
