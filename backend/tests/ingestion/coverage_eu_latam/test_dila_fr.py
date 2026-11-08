"""coverage-B: DILA info-financière (FR OAM) adapter + PackageFilingAdapter base on recorded fixtures (no network)."""

from __future__ import annotations

import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app.ingestion import _arelle, dila_fr
from app.ingestion._oam_common import VERIFIED_LEIS
from app.ingestion.base import FetchResult

FX = Path(__file__).parent / "fixtures"
ISSUER = {"issuer_id": "ITESTDANONE01", "name": "Danone", "country": "FR", "region": "EU",
          "lei": "969500KMUQ2B6CBAF162", "leis": ["969500KMUQ2B6CBAF162"], "isin": "FR0000120644",
          "yf_ticker": "BN.PA", "yf_tickers": ["BN.PA"], "tickers": ["BN@XPAR"], "fye_month": None}


class _Resp:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


def _adapter(tmp_data, monkeypatch, n_downloads):
    # A9 2026-11-07: dila_fr is switched off (robots.txt disallows the API / package mirror); these tests exercise the
    # legacy fetch path, kept for the stored rows and a possible written permission, with the policy check bypassed
    monkeypatch.setattr(dila_fr, "disabled_note", lambda source_id: None)
    a = dila_fr.DilaFrAdapter(settings=tmp_data, issuers=[ISSUER])
    recs = json.loads((FX / "dila_danone_records.json").read_text(encoding="utf-8"))
    doc = json.loads((FX / "dila_danone_oim_small.json").read_text(encoding="utf-8"))

    def fake_get(url, params=None, **kw):
        if url == dila_fr.API:
            assert "969500KMUQ2B6CBAF162" in params["where"]
            return _Resp(recs)
        n_downloads.append(url)
        return _Resp(b"PK-fake-package")

    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(a, "to_doc", lambda f, payload: doc)
    return a


def test_dila_disabled_by_robots_policy(tmp_data, monkeypatch):
    """A9: the adapter makes no request and the job is skipped with reason source_disallowed."""
    from types import SimpleNamespace

    a = dila_fr.DilaFrAdapter(settings=tmp_data, issuers=[ISSUER])
    calls = []
    monkeypatch.setattr(a.http, "get", lambda *x, **k: calls.append(x))
    fetched, rows = a.run(write=False)
    assert calls == [] and fetched.meta["disabled"] == "source_disallowed" and fetched.meta["filings"] == []
    assert all(v == 0 for v in rows.values())
    res = dila_fr.run(SimpleNamespace(settings=tmp_data, params={}, watermark=None))
    assert res.status == "skipped" and res.message.startswith("source_disallowed") and res.retryable is False


def test_classify_and_select():
    recs = json.loads((FX / "dila_danone_records.json").read_text(encoding="utf-8"))["results"]
    fs = dila_fr.select_filings(recs)
    assert fs and all(f["form_class"] == "annual" and f["fiscal_period"] == "FY" for f in fs)
    years = [f["knowledge_ts"][:4] for f in fs]
    assert len(years) == len(set(years))  # one (earliest) deposit per year
    assert fs[-1]["suffix"] == ".xbri" and fs[-1]["knowledge_ts"].startswith("2026-03-04")
    assert dila_fr.classify({"informationdeposee_inf_stp_inf": '["002003"]'})[1] == "semiannual"
    assert dila_fr.classify({"informationdeposee_inf_stp_inf": '["006000"]', "subtype_of_information": "x"}) is None


def test_dila_fetch_normalize_resume(tmp_data, monkeypatch):
    dl: list[str] = []
    a = _adapter(tmp_data, monkeypatch, dl)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["filings_index"] >= 4 and rows["fundamentals_raw"] > 0
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    latest = fi.sort("knowledge_date").row(-1, named=True)
    assert latest["knowledge_ts"].isoformat().startswith("2026-03-04T08:11:55") or latest["knowledge_date"] == date(2026, 3, 4)
    assert latest["has_xbrl"] and latest["issuer_id"] == "ITESTDANONE01" and latest["source_entity_id"] == ISSUER["lei"]
    rev = fr.filter((fr["concept"] == "Revenue") & (fr["period_end"] == date(2025, 12, 31)) & (fr["dimensions_hash"] == ""))
    assert rev.height >= 1 and rev["value"][0] == pytest.approx(27_283_000_000, rel=0.02)
    assert rev["unit"][0] == "EUR" and rev["taxonomy"][0] == "ifrs-full" and rev["fiscal_period"][0] == "FY"
    assert (fr["issuer_id"] == "ITESTDANONE01").all() and not fr["knowledge_date_estimated"].any()
    # PIT: no fact is known before its period ends, knowledge_date = deposit date
    last = fr.filter(fr["filing_id"] == latest["filing_id"])
    assert last.height and (last["knowledge_date"] >= last["period_end"]).all()
    # resumable: a second run downloads nothing new
    n = len(dl)
    a2 = _adapter(tmp_data, monkeypatch, dl)
    f2 = a2.fetch()
    assert len(dl) == n and not f2.meta["filings"]


def test_arelle_failure_recorded(tmp_data, monkeypatch):
    dl: list[str] = []
    a = _adapter(tmp_data, monkeypatch, dl)

    def boom(f, payload):
        raise _arelle.ArelleError("no numeric facts", "log tail")

    monkeypatch.setattr(a, "to_doc", boom)
    fetched = a.fetch()
    assert fetched.entities_failed and fetched.meta["arelle_failures"]
    assert isinstance(fetched, FetchResult)


def test_pick_entry_unwraps_nested_and_bare(tmp_path):
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("pkg/reports/report.xhtml", "<html/>")
        z.writestr("pkg/META-INF/reportPackage.json", "{}")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as z:
        z.write(inner, "inner.zip")
    assert _arelle.pick_entry(outer, tmp_path).name == "inner.zip"
    bare = tmp_path / "bare.zip"
    with zipfile.ZipFile(bare, "w") as z:
        z.writestr("doc/report.xhtml", "<html/>" * 10)
        z.writestr("doc/lab.xml", "<x/>")
    assert _arelle.pick_entry(bare, tmp_path).name == "report.xhtml"


def test_verified_leis_shape():
    assert VERIFIED_LEIS["BNP.PA"][0] == "R0MUWSFPU8MPRO8K5P83"
    assert all(len(lei) == 20 and len(isin) == 12 for lei, isin in VERIFIED_LEIS.values())


@pytest.mark.network
def test_arelle_converts_real_package():
    """Needs the Arelle taxonomy cache or internet: converts a recorded ESEF package if present in the scratch dir."""
    p = Path(__file__).parent / "fixtures" / "esef_package_sample.zip"
    if not p.exists():
        pytest.skip("no sample package")
    doc = _arelle.to_xbrl_json(p)
    assert doc["facts"]
