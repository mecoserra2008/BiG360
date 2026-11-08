"""coverage-B: Nasdaq Nordic company-news (SE/DK/FI OAM) ESEF adapter on recorded fixtures (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.ingestion import nasdaq_nordic as nn

FX = Path(__file__).parent / "fixtures"
HM = {"issuer_id": "ITESTHM000001", "name": "H & M Hennes & Mauritz", "country": "SE", "region": "EU",
      "lei": "529900O5RR7R39FRDM42", "leis": ["529900O5RR7R39FRDM42"], "isin": "SE0000106270",
      "yf_ticker": "HM-B.ST", "yf_tickers": ["HM-B.ST"], "tickers": ["HM B@XSTO"], "fye_month": None}
VOLVO = {**HM, "issuer_id": "ITESTVOLVO001", "name": "Volvo", "lei": "549300HGV012CNC8JD22",
         "leis": ["549300HGV012CNC8JD22"], "yf_ticker": "VOLV-B.ST", "yf_tickers": ["VOLV-B.ST"]}


def _releases() -> dict:
    return json.loads((FX / "nasdaq_nordic_releases.json").read_text(encoding="utf-8"))


def _items(company: str, cat: str) -> list[dict]:
    return _releases().get(f"{company}|{cat}", {"results": {"item": []}})["results"]["item"]


class _Resp:
    def __init__(self, payload):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


def _adapter(tmp_data, monkeypatch, downloads, issuers):
    a = nn.NasdaqNordicAdapter(settings=tmp_data, issuers=issuers)
    rel = _releases()
    doc = json.loads((FX / "nasdaq_hm_oim_small.json").read_text(encoding="utf-8"))

    def fake_get(url, params=None, **kw):
        if url == nn.API:
            assert params["company"] in nn.COMPANY_NAMES.values() and params["limit"] == nn.PAGE
            return _Resp(rel.get(f"{params['company']}|{params['cnsCategory']}", {"count": 0, "results": {"item": []}}))
        downloads.append(url)
        return _Resp(b"PK-fake-package")

    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(a, "to_doc", lambda f, payload: doc)
    return a


def test_parse_ts_and_period_hint():
    assert nn.parse_ts("2026-03-26 07:10:00 +0000") == datetime(2026, 3, 26, 7, 10, tzinfo=UTC)
    assert nn.parse_ts("2026-03-26 08:10:00 +0100") == datetime(2026, 3, 26, 7, 10, tzinfo=UTC)
    assert nn.parse_ts("2026-03-26 07:10:00").tzinfo is UTC and nn.parse_ts("garbage") is None
    assert nn.period_hint("hm-2025-11-30-0-sv.zip") == "2025-11-30"
    assert nn.period_hint("eqnr20211231esma.zip") == "2021-12-31"
    assert nn.period_hint("Novo-AR-2022.zip") is None and nn.period_hint("01261125.pdf") is None


def test_select_hm_one_per_year_nov_fye():
    fs = nn.select_filings(_items("Hennes & Mauritz AB, H & M", nn.AFR))
    assert [f["knowledge_ts"][:10] for f in fs] == ["2023-03-30", "2024-03-27", "2025-03-27", "2026-03-26"]
    last = fs[-1]
    assert last["file_name"] == "hm-2025-11-30-0-sv.zip" and last["period_hint"] == "2025-11-30"
    assert last["form_class"] == "annual" and last["fiscal_period"] == "FY" and last["period_end"] is None
    assert last["doc_url"].startswith("https://attachment.news.eu.nasdaq.com/") and last["suffix"] == ".zip"


def test_select_nordea_correction_is_amendment_and_prefers_english():
    fs = nn.select_filings(_items("Nordea Bank Abp", nn.AFR))
    by = {f["knowledge_ts"][:10]: f for f in fs}
    assert sorted(by) == ["2024-02-26", "2025-02-24", "2025-04-02", "2026-02-23"]  # en/sv/fi same-day copies dropped
    assert not by["2025-02-24"]["is_amendment"] and by["2025-02-24"]["form_type"] == "AFR"
    corr = by["2025-04-02"]  # FY2024 re-publication: amendment with its own knowledge_ts and the period from its name
    assert corr["is_amendment"] and corr["form_type"] == "AFR/A" and corr["period_end"] == "2024-12-31"
    assert all(f["language"] == "en" for f in fs)


def test_other_category_needs_annual_headline_and_dated_package():
    items = _items("Volvo, AB", nn.OTHER)
    fs = nn.select_filings(items)
    assert fs and all(f["period_hint"] and f["file_name"].startswith("abvolvo-") for f in fs)
    fake = {**items[0], "headline": "Volvo share buyback", "disclosureId": 1}
    assert nn.pick_attachment(fake) is None


def test_fetch_normalize_resume(tmp_data, monkeypatch):
    dl: list[str] = []
    a = _adapter(tmp_data, monkeypatch, dl, [HM])
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["filings_index"] == 4 and rows["fundamentals_raw"] > 0
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    latest = fi.sort("knowledge_date").row(-1, named=True)
    assert latest["knowledge_date"] == date(2026, 3, 26) and latest["source_entity_id"] == HM["lei"]
    assert latest["period_end"] == date(2025, 11, 30) and latest["fiscal_year"] == 2025  # Nov FYE from xBRL facts
    assert latest["has_xbrl"] and latest["issuer_id"] == "ITESTHM000001"
    rev = fr.filter((fr["concept"] == "RevenueFromContractsWithCustomers") & (fr["period_end"] == date(2025, 11, 30)) & (fr["dimensions_hash"] == ""))
    assert rev.height >= 1 and rev["unit"][0] == "SEK" and rev["fiscal_period"][0] == "FY"
    assert rev["value"][0] == pytest.approx(228_285_000_000)
    last = fr.filter(fr["filing_id"] == latest["filing_id"])  # (the one fixture doc is served for every filing)
    assert (last["knowledge_date"] >= last["period_end"]).all() and not fr["knowledge_date_estimated"].any()
    n = len(dl)
    f2 = _adapter(tmp_data, monkeypatch, dl, [HM]).fetch()
    assert len(dl) == n and not f2.meta["filings"]


def test_unknown_company_is_a_listing_failure(tmp_data, monkeypatch):
    a = _adapter(tmp_data, monkeypatch, [], [{**HM, "lei": "X" * 20, "leis": ["X" * 20], "name": "Zzz Unknown"}])
    monkeypatch.setattr(a, "_query", lambda **p: {"count": 0, "results": {"item": []}})
    fetched = a.fetch()
    assert any(k.startswith("list:") for k in fetched.entities_failed) and not fetched.meta["filings"]


@pytest.mark.network
def test_live_listing_volvo():
    a = nn.NasdaqNordicAdapter(issuers=[VOLVO])
    fs = a.list_filings(VOLVO)
    assert any(f["period_hint"] == "2025-12-31" for f in fs)


def test_fix_zip_separators():
    import io
    import zipfile

    def _zip(names):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n in names:
                zi = zipfile.ZipInfo("x")
                zi.filename = n  # bypass the os.sep normalisation of ZipInfo() to store a raw backslash name
                z.writestr(zi, "<x/>")
        return buf.getvalue()

    raw = _zip([r"pkg\META-INF\taxonomyPackage.xml", r"pkg\reports\r.html"])
    good = _zip(["pkg/META-INF/taxonomyPackage.xml"])
    fixed = nn.fix_zip_separators(raw)
    names = [i.orig_filename for i in zipfile.ZipFile(io.BytesIO(fixed)).infolist()]
    assert names == ["pkg/META-INF/taxonomyPackage.xml", "pkg/reports/r.html"]
    assert nn.fix_zip_separators(good) == good and nn.fix_zip_separators(b"not a zip") == b"not a zip"
