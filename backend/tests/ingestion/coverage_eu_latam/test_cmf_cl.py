"""coverage-B: CMF Chile adapter (filing log -> PIT timestamp, flaky POST retry, XBRL zip) on trimmed fixtures."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.ingestion import cmf_cl

FX = Path(__file__).parent / "fixtures"
SQM = {"issuer_id": "ITESTSQM00001", "name": "Sociedad Quimica y Minera de Chile", "country": "CL", "region": "LATAM",
       "lei": None, "leis": [], "isin": None, "yf_ticker": "SQM-B.SN", "yf_tickers": ["SQM", "SQM-B.SN"],
       "tickers": ["SQM-B@XSGO"], "fye_month": None}
EMPTY_LOG = "<html><body><table><tr><td>Fecha</td><td>Motivo</td></tr></table></body></html>"


class _Resp:
    def __init__(self, payload: bytes | str, status: int = 200):
        self.content = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.status_code = status


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("93007000_202412_C.xbrl", (FX / "cmf_sqm_202412_small.xbrl").read_bytes())
        z.writestr("93007000_202412_C-label.xml", "<x/>")
    return buf.getvalue()


def test_bitacora_and_knowledge():
    rows = cmf_cl.parse_bitacora((FX / "cmf_bitacora_202412.html").read_bytes())
    assert len(rows) == 3 and rows[0]["reason"].lower().startswith("primer env")
    kts, amended, xbrl_ts = cmf_cl.knowledge_from_log(rows)
    # 05/03/2025 00:32 Santiago (UTC-3 in March) -> 03:32 UTC; XBRL-only re-send keeps the first-send date
    assert kts == datetime(2025, 3, 5, 3, 32, tzinfo=UTC) and not amended
    assert xbrl_ts is not None and xbrl_ts.date() == date(2025, 4, 10)
    restated = rows + [{"ts": datetime(2025, 6, 1, tzinfo=UTC), "reason": "Reenvio", "items": "Estados Financieros PDF"}]
    k2 = cmf_cl.knowledge_from_log(restated)
    assert k2[0] == datetime(2025, 6, 1, tzinfo=UTC) and k2[1]
    assert cmf_cl.knowledge_from_log([]) is None


def test_xbrl_link():
    html = (FX / "cmf_docs_202412.html").read_text(encoding="utf-8")
    link = cmf_cl.xbrl_link(html)
    assert link.startswith(cmf_cl.DOC_BASE + "safec_ifrs_verarchivo.php?auth=") and "&amp;" not in link
    assert cmf_cl.xbrl_link("<html>302 redirect page</html>") is None
    with pytest.raises(cmf_cl.NoXbrl):
        cmf_cl.xbrl_link('<a href="../inc/inf_financiera/ifrs/safec_ifrs_verarchivo.php?auth=x">Estados financieros (PDF)</a>')


def test_cmf_fetch_normalize_resume(tmp_data, monkeypatch):
    a = cmf_cl.CmfClAdapter(settings=tmp_data, issuers=[SQM])
    a.first_year = 2024
    log = (FX / "cmf_bitacora_202412.html").read_text(encoding="utf-8")
    docs = (FX / "cmf_docs_202412.html").read_text(encoding="utf-8")
    posts: list[dict] = []
    downloads: list[str] = []

    def fake_get(url, params=None, **kw):
        if url == cmf_cl.BITACORA_URL:
            assert params["rut"] == "93007000"
            return _Resp(log if params["periodo"] == "202412" else EMPTY_LOG)
        downloads.append(url)
        return _Resp(_zip())

    def fake_post(url, data=None, **kw):
        posts.append(data)
        return _Resp("<html>empty</html>" if len(posts) == 1 else docs)  # first POST = the flaky 302 case

    monkeypatch.setattr(a.http, "get", fake_get)
    monkeypatch.setattr(a.http, "post", fake_post)
    monkeypatch.setattr(cmf_cl.time, "sleep", lambda s: None)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["filings_index"] == 1 and rows["fundamentals_raw"] > 0
    assert len(posts) == 2 and posts[-1] == {"mm": "12", "aa": "2024", "tipo": "C", "tipo_norma": "IFRS"}
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    f = fi.row(0, named=True)
    assert f["filing_id"] == "cmf_cl:93007000:202412:C" and f["form_class"] == "annual" and f["fiscal_period"] == "FY"
    assert f["knowledge_date"] == date(2025, 3, 5) and f["has_xbrl"] and f["issuer_id"] == "ITESTSQM00001"
    rev = fr.filter((fr["concept"] == "Revenue") & (fr["dimensions_hash"] == "") & (fr["period_end"] == date(2024, 12, 31)))
    assert rev["value"][0] == pytest.approx(4_528_761_000) and rev["unit"][0] == "USD" and rev["taxonomy"][0] == "ifrs-full"
    assert (fr.filter(fr["taxonomy"] == "other")["concept"].str.starts_with("cl-ci:")).any()
    # resumable
    a2 = cmf_cl.CmfClAdapter(settings=tmp_data, issuers=[SQM])
    a2.first_year = 2024
    monkeypatch.setattr(a2.http, "get", fake_get)
    monkeypatch.setattr(a2.http, "post", fake_post)
    n = len(downloads)
    assert not a2.fetch().meta["filings"] and len(downloads) == n


def test_no_xbrl_is_metadata_only(tmp_data, monkeypatch):
    a = cmf_cl.CmfClAdapter(settings=tmp_data, issuers=[SQM])
    a.first_year = 2024
    log = (FX / "cmf_bitacora_202412.html").read_text(encoding="utf-8")
    monkeypatch.setattr(a.http, "get", lambda url, params=None, **kw: _Resp(
        log if params and params.get("periodo") == "202412" else EMPTY_LOG))
    monkeypatch.setattr(a.http, "post", lambda url, data=None, **kw: _Resp(
        '<a href="../inc/inf_financiera/ifrs/safec_ifrs_verarchivo.php?auth=x">Estados financieros (PDF)</a>'))
    fetched = a.fetch()
    t = a.normalize(fetched)
    assert t["filings_index"].height == 1 and not t["filings_index"]["has_xbrl"][0] and t["fundamentals_raw"].height == 0


@pytest.mark.network
def test_cmf_live_log(tmp_data):
    a = cmf_cl.CmfClAdapter(settings=tmp_data)
    r = a.http.get(cmf_cl.BITACORA_URL, params={"rut": "93007000", "tipoentidad": "RVEMI", "vig": "VI",
                                                "periodo": "202412", "tb": "C"})
    assert cmf_cl.knowledge_from_log(cmf_cl.parse_bitacora(r.content))[0].date() == date(2025, 3, 5)
