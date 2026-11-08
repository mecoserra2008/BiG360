"""MOPS (Taiwan IFRS XBRL) adapter: recorded-fixture unit tests (+ one live smoke test marked network)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.core.http import CachedResponse
from app.ingestion import mops_tw as m

FIX = Path(__file__).parent / "fixtures"
IXBRL = (FIX / "mops_2330_2026Q2_trim.html").read_bytes()
XML13 = (FIX / "mops_2330_2013Q1_trim.xml").read_bytes()
T57 = (FIX / "mops_t57sb01_2330_114.html").read_bytes()
EMPTY_T57 = b"<html><title>" + m.T57_OK + b"</title></html>"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(m.time, "sleep", lambda s: None)


def test_seasons_helpers():
    assert m.season_range("2025Q3", "2026Q2") == ["2025Q3", "2025Q4", "2026Q1", "2026Q2"]
    assert m.season_end("2026Q2") == date(2026, 6, 30)
    assert m.latest_season(date(2026, 10, 11)) == "2026Q3"
    assert m.latest_season(date(2026, 2, 1)) == "2025Q4"
    assert m.parse_seasons_param("2013Q1-2013Q4,2020Q2", date(2026, 10, 11)) == [
        "2013Q1", "2013Q2", "2013Q3", "2013Q4", "2020Q2"]
    assert len(m.season_range("2013Q1", "2026Q2")) == 54


def test_parse_upload_list():
    up = m.parse_upload_list(T57)
    assert set(up) == {"2025Q1", "2025Q2", "2025Q3", "2025Q4"}
    assert up["2025Q1"]["ts_local"] == datetime(2025, 5, 15, 13, 40, 15)
    assert up["2025Q4"]["file"] == "202504_2330_AI1.pdf" and up["2025Q4"]["consolidated"]


def _by(facts, qname, start, end, dims=False):
    return [f for f in facts if f["qname"] == qname and f["start"] == start and f["end"] == end and bool(f["dims"]) == dims]


def test_ixbrl_scaling_sign_dims():
    facts = m.iter_facts(IXBRL)
    rev_q = _by(facts, "ifrs-full:Revenue", date(2026, 4, 1), date(2026, 6, 30))
    assert rev_q[0]["value"] == pytest.approx(1_270_380_250_000) and rev_q[0]["unit"] == "TWD"
    assert rev_q[0]["decimals"] == -3
    assert _by(facts, "ifrs-full:Revenue", date(2026, 1, 1), date(2026, 6, 30))[0]["value"] == pytest.approx(2_404_483_690_000)
    eps = [f for f in facts if f["qname"] == "ifrs-full:BasicEarningsLossPerShare" and f["end"] == date(2026, 6, 30)
           and f["start"] == date(2026, 4, 1)]
    assert eps[0]["value"] == pytest.approx(27.25) and eps[0]["unit"] == "TWD/shares"
    assert _by(facts, "ifrs-full:Assets", None, date(2026, 6, 30))[0]["value"] == pytest.approx(9_375_654_727_000)
    oei = {f["end"]: f["value"] for f in facts if f["qname"] == "ifrs-full:OtherEquityInterest" and not f["dims"]}
    assert oei[date(2026, 6, 30)] == pytest.approx(49_790_695_000)
    assert oei[date(2025, 12, 31)] == pytest.approx(-16_676_412_000)  # sign="-"
    dim = [f for f in facts if f["dims"]]
    assert dim and "ifrs-full:ComponentsOfEquityAxis" in dim[0]["dims"]


def test_plain_xbrl_2013():
    facts = m.iter_facts(XML13)
    pl_ = _by(facts, "ifrs-full:ProfitLoss", date(2013, 1, 1), date(2013, 3, 31))
    assert pl_[0]["value"] == pytest.approx(39_535_543_000)  # 2010 `ifrs` namespace -> ifrs-full
    assert _by(facts, "tifrs-bsci-ci:OperatingRevenue", date(2013, 1, 1), date(2013, 3, 31))[0]["value"] == pytest.approx(132_754_996_000)
    eps = [f for f in facts if f["qname"] == "ifrs-full:BasicEarningsLossPerShare" and f["end"] == date(2013, 3, 31)]
    assert eps[0]["value"] == pytest.approx(1.53) and eps[0]["unit"] == "TWD/shares"


class FakeHttp:
    """Answers the MOPS endpoints from fixtures. Season 2026Q2 exists; everything else is a MOPS error page."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.n_requests = self.n_cache_hits = 0
        self.headers: dict[str, str] = {}

    def get(self, url, params=None, **kw):
        self.calls.append((url, dict(params or {})))
        p = params or {}
        if url == m.DOC_LIST:
            body = T57 if p["year"] == "114" else (T57.replace(b"202502_2330", b"202602_2330")
                                                    .replace(b"114/08/14 13:36:58", b"115/08/13 14:05:00")
                                                    .replace(b"202501_2330", b"202601_2330")) if p["year"] == "115" else EMPTY_T57
            return CachedResponse(200, url, body)
        if url == "FileDownLoad" and p.get("fileName") == "tifrs-fr1-m1-ci-cr-2330-2026Q2.html":
            return CachedResponse(200, url, IXBRL)
        if url == "t164sb01":
            return CachedResponse(200, url, b"<html>no data</html>")
        return CachedResponse(200, url, b"<html><body>error</body></html>")

    def download(self, url, dest):
        raise AssertionError("bulk zip must not be used when the season has no upload record")


def test_fetch_normalize_end_to_end(tmp_data):
    ad = m.MopsAdapter(settings=tmp_data, http=FakeHttp(), seasons="2026Q1-2026Q2")
    fetched, rows = ad.run(entities=["2330.TW"])
    assert fetched.meta["done"]["2330:2026Q2"]["member"] == "tifrs-fr1-m1-ci-cr-2330-2026Q2.html"
    assert (tmp_data.raw_dir / "mops" / "ixbrl" / "2026Q2" / "tifrs-fr1-m1-ci-cr-2330-2026Q2.html.gz").exists()
    # 2026Q1 has an upload record but no member -> scheduled for bulk; FakeHttp.download raises -> recorded failure
    assert "bulk:2026Q1" in fetched.entities_failed
    assert rows["filings_index"] == 1 and rows["fundamentals_raw"] > 30
    tables = ad.normalize(fetched)
    fi = tables["filings_index"].row(0, named=True)
    assert fi["filing_id"] == "mops:2330:2026Q2" and fi["fiscal_period"] == "Q2"
    assert fi["knowledge_date"] == date(2026, 8, 13)
    assert fi["knowledge_ts"].replace(tzinfo=UTC) == datetime(2026, 8, 13, 6, 5, tzinfo=UTC)
    fr = tables["fundamentals_raw"]
    rev = fr.filter((fr["concept"] == "Revenue") & (fr["period_start"] == date(2026, 4, 1)))
    assert rev.height == 1
    r = rev.row(0, named=True)
    assert (r["taxonomy"], r["source_entity_id"], r["unit"], r["fiscal_period"], r["fiscal_year"]) == (
        "ifrs-full", "2330.TW", "TWD", "Q2", 2026)
    assert r["value"] == pytest.approx(1.27038025e12) and r["consolidated"] is True
    assert r["knowledge_date_estimated"] is False
    # prior-year comparative carries this filing's knowledge date
    prior = fr.filter((fr["concept"] == "Revenue") & (fr["period_end"] == date(2025, 6, 30)))
    assert prior.height == 2 and set(prior["knowledge_date"].to_list()) == {date(2026, 8, 13)}
    ytd = fr.filter((fr["concept"] == "Revenue") & (fr["period_start"] == date(2026, 1, 1)))
    assert ytd["fiscal_period"].to_list() == ["H1"]
    # resumable: a second run does not refetch the landed member
    ad2 = m.MopsAdapter(settings=tmp_data, http=FakeHttp(), seasons="2026Q2")
    f2, rows2 = ad2.run(entities=["2330"], write=False)
    assert not [c for c in ad2.http.calls if c[0] == "FileDownLoad"]
    assert rows2["fundamentals_raw"] == rows["fundamentals_raw"]


def test_imputed_knowledge_date(tmp_data):
    class NoPit(FakeHttp):
        def get(self, url, params=None, **kw):
            if url == m.DOC_LIST:
                return CachedResponse(200, url, EMPTY_T57)
            return super().get(url, params, **kw)

    ad = m.MopsAdapter(settings=tmp_data, http=NoPit(), seasons="2026Q2")
    fetched, _ = ad.run(entities=["2330.TW"], write=False)
    t = ad.normalize(fetched)
    fi = t["filings_index"].row(0, named=True)
    assert fi["knowledge_date"] == date(2026, 8, 29) and fi["knowledge_ts"] is None  # Jun-30 + 60 d
    assert t["fundamentals_raw"]["knowledge_date_estimated"].all()


def test_t57_throttle_retry_and_reuse(tmp_data):
    class Throttled(FakeHttp):
        n = 0

        def get(self, url, params=None, **kw):
            if url == m.DOC_LIST:
                Throttled.n += 1
                if Throttled.n == 1:
                    return CachedResponse(200, url, "<html>查詢過量，請稍後再查詢!</html>".encode("big5"))
            return super().get(url, params, **kw)

    ad = m.MopsAdapter(settings=tmp_data, http=Throttled())
    up = ad.upload_list("2330", 2025, date(2026, 10, 11))
    assert Throttled.n == 2 and "2025Q2" in up
    assert ad.t57_path("2330", 114).exists()
    ad.upload_list("2330", 2025, date(2026, 10, 11))  # past year: landed page reused, no request
    assert Throttled.n == 2


@pytest.mark.network
def test_live_tsmc_member(tmp_data):
    ad = m.MopsAdapter(settings=tmp_data)
    got = ad.resolve_member("2330", "2026Q2", {})
    assert got is not None
    facts = m.iter_facts(got[1])
    assert _by(facts, "ifrs-full:Revenue", date(2026, 4, 1), date(2026, 6, 30))[0]["value"] == pytest.approx(1.27038025e12)
    assert "2025Q4" in ad.upload_list("2330", 2025, date(2026, 10, 11))
