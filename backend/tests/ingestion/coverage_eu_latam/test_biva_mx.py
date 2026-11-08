"""coverage-B: BIVA (MX) adapter + taxonomy-free XBRL instance parser on recorded, trimmed fixtures (no network)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.ingestion import biva_mx
from app.ingestion._global_common import fact_row
from app.ingestion._latam_xbrl import instance_to_oim, label_quarters, quarter_of

FX = Path(__file__).parent / "fixtures"
GMEX = {"issuer_id": "ITESTGMEXICO1", "name": "Grupo Mexico", "country": "MX", "region": "LATAM", "lei": None,
        "leis": [], "isin": None, "yf_ticker": "GMEXICOB.MX", "yf_tickers": ["GMEXICOB.MX"],
        "tickers": ["GMEXICOB@XMEX"], "fye_month": None}
GFNORTE = {**GMEX, "issuer_id": "ITESTGFNORTE1", "name": "Grupo Financiero Banorte", "yf_ticker": "GFNORTEO.MX",
           "yf_tickers": ["GFNORTEO.MX"], "tickers": ["GFNORTEO@XMEX"]}


class _Resp:
    def __init__(self, payload, status=200):
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.status_code = status

    def json(self):
        return json.loads(self.content)


def _adapter(tmp_data, monkeypatch, downloads, issuers=(GMEX, GFNORTE)):
    a = biva_mx.BivaMxAdapter(settings=tmp_data, issuers=list(issuers))
    q = json.loads((FX / "biva_gmexico_q.json").read_text(encoding="utf-8"))
    ann = json.loads((FX / "biva_gfnorte_a.json").read_text(encoding="utf-8"))
    gm = (FX / "biva_gmexico_2025_4D_small.xbrl").read_bytes()
    gf = (FX / "biva_gfnorte_ra2025_small.xbrl").read_bytes()
    gf_urls = {biva_mx.BASE + x["url"] for r in ann["content"] for x in r["archivosXbrl"]}

    def fake_get(url, params=None, **kw):
        if url == biva_mx.LIST_API:
            emp, t = params["empresa"], params["tipoInformacion"]
            if emp == 1693 and t == 17:
                return _Resp(q)
            if emp == 1680 and t == 15:
                return _Resp(ann)
            return _Resp({"status": 404}, 404)
        downloads.append(url)
        return _Resp(gf if url in gf_urls else gm)

    monkeypatch.setattr(a.http, "get", fake_get)
    return a


def test_parse_tipo_and_timestamps():
    assert biva_mx.parse_tipo("Información trimestral 4D-2025", 17) == ("BIVA-Q4D", "quarterly", "Q4", date(2025, 12, 31))
    assert biva_mx.parse_tipo("Información financiera 3 - 2018", 17)[2:] == ("Q3", date(2018, 9, 30))
    assert biva_mx.parse_tipo("Reporte anual 2025", 15) == ("BIVA-AR", "annual", "FY", date(2025, 12, 31))
    assert biva_mx.parse_tipo("Otro documento", 17) is None
    # 1784626354000 = 2026-07-21 09:32:34 wall clock (Mexico City, UTC-6) -> 15:32:34 UTC
    assert biva_mx.publication_ts(1784626354000) == datetime(2026, 7, 21, 15, 32, 34, tzinfo=UTC)


def test_select_filings_flags_backfill():
    q = json.loads((FX / "biva_gmexico_q.json").read_text(encoding="utf-8"))["content"]
    fs = biva_mx.select_filings(q, 17, "GMEXICO")
    assert len(fs) == 5 and all(f["doc_url"].endswith(".xbrl") for f in fs)
    first = fs[0]
    assert first["fiscal_period"] == "Q1" and first["period_end"] == "2018-03-31" and first["backfilled"]
    assert not fs[-1]["backfilled"] and fs[-1]["native_key"].startswith("GMEXICO:")


def test_instance_parser_ifrs_full_and_units():
    doc = instance_to_oim((FX / "biva_gmexico_2025_4D_small.xbrl").read_bytes())
    concepts = {f["dimensions"]["concept"] for f in doc["facts"].values()}
    assert "ifrs-full:Revenue" in concepts and all(":" in c for c in concepts)
    units = {f["dimensions"]["unit"] for f in doc["facts"].values()}
    assert "iso4217:USD" in units
    # CMF 2012-style legacy IFRS namespace prefix 'ifrs' also maps to ifrs-full
    legacy = (b'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" '
              b'xmlns:ifrs="http://xbrl.ifrs.org/taxonomy/2011-03-25/ifrs" xmlns:iso4217="http://www.xbrl.org/2003/iso4217">'
              b'<xbrli:context id="c"><xbrli:entity><xbrli:identifier scheme="s">X</xbrli:identifier></xbrli:entity>'
              b'<xbrli:period><xbrli:startDate>2012-01-01</xbrli:startDate><xbrli:endDate>2012-12-31</xbrli:endDate>'
              b'</xbrli:period></xbrli:context><xbrli:unit id="u"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
              b'<ifrs:Revenue contextRef="c" unitRef="u" decimals="-3">100</ifrs:Revenue></xbrli:xbrl>')
    f = next(iter(instance_to_oim(legacy)["facts"].values()))
    assert f["dimensions"]["concept"] == "ifrs-full:Revenue" and f["dimensions"]["period"] == "2012-01-01/2012-12-31"


def test_label_quarters_and_ttm():
    assert [quarter_of(date(2025, m, 28), 12) for m in (3, 6, 9, 12)] == [1, 2, 3, 4]
    assert quarter_of(date(2025, 6, 30), 3) == 1  # FY ending March

    def row(s, e):
        return fact_row(source_id="biva_mx", source_entity_id="X", filing_id="f", form_type="Q", taxonomy="ifrs-full",
                        concept="Revenue", label=None, period_start=s, period_end=e, value=1.0, unit="MXN",
                        knowledge_date=date(2026, 7, 21), fiscal_period="FY" if (e - s).days > 340 else None)
    rows = label_quarters([row(date(2026, 4, 1), date(2026, 6, 30)), row(date(2025, 7, 1), date(2026, 6, 30)),
                           row(date(2025, 1, 1), date(2025, 9, 30)), row(date(2025, 1, 1), date(2025, 12, 31))], 12)
    assert [r["fiscal_period"] for r in rows] == ["Q2", None, "9M", "FY"]
    assert rows[1]["dimensions_hash"] and "TTM" in rows[1]["dimensions_json"]  # trailing 12 months kept out of FY
    assert rows[3]["dimensions_hash"] == ""


def test_biva_fetch_normalize_resume(tmp_data, monkeypatch):
    dl: list[str] = []
    a = _adapter(tmp_data, monkeypatch, dl)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["filings_index"] == 5 + 5 and rows["fundamentals_raw"] > 0
    t = a.normalize(fetched)
    fi, fr = t["filings_index"], t["fundamentals_raw"]
    assert set(fi["source_entity_id"]) == {"GMEXICO", "GFNORTE"}
    q4d = fi.filter(fi["form_type"] == "BIVA-Q4D").row(0, named=True)
    assert q4d["form_class"] == "quarterly" and q4d["fiscal_period"] == "Q4" and q4d["period_end"] == date(2025, 12, 31)
    assert q4d["knowledge_ts"].isoformat().startswith("2026-05-05T16:39:28") and q4d["issuer_id"] == "ITESTGMEXICO1"
    ar = fi.filter(fi["source_entity_id"] == "GFNORTE")
    assert (ar["form_class"] == "annual").all() and (ar["fiscal_period"] == "FY").all()
    rev = fr.filter((fr["filing_id"] == q4d["filing_id"]) & (fr["concept"] == "Revenue") & (fr["dimensions_hash"] == "")
                    & (fr["fiscal_period"] == "FY") & (fr["period_end"] == date(2025, 12, 31)))
    assert rev.height == 1 and rev["value"][0] == pytest.approx(18_176_711_000) and rev["unit"][0] == "USD"
    assert rev["taxonomy"][0] == "ifrs-full" and rev["knowledge_date"][0] == date(2026, 5, 5)
    q4 = fr.filter((fr["filing_id"] == q4d["filing_id"]) & (fr["concept"] == "Revenue") & (fr["fiscal_period"] == "Q4"))
    assert q4.height >= 1
    pl_ = fr.filter((fr["source_entity_id"] == "GFNORTE") & (fr["concept"] == "ProfitLoss")
                    & (fr["period_end"] == date(2025, 12, 31)))
    assert pl_.height >= 1 and pl_["unit"][0] == "MXN" and pl_["value"][0] == pytest.approx(58_788_385_250, rel=1e-6)
    assert (fr["knowledge_date"] >= fr["period_end"]).all()  # PIT
    n = len(dl)
    a2 = _adapter(tmp_data, monkeypatch, dl)
    f2 = a2.fetch()
    assert len(dl) == n and not f2.meta["filings"]


class _J:
    def __init__(self, p):
        self._p = p

    def json(self):
        return self._p


def test_entity_key_map_and_discovery(tmp_data, monkeypatch):
    a = biva_mx.BivaMxAdapter(settings=tmp_data)
    assert a.entity_key(GMEX) == "GMEXICO"
    assert {"GMEXICOB.MX", "KOFUBL.MX", "PE&OLES.MX", "TLEVISACPO.MX"} <= set(biva_mx.BIVA_IDS)
    assert biva_mx.clave_candidates("GAPB.MX")[:2] == ["GAPB", "GAP"]
    assert "TLEVISA" in biva_mx.clave_candidates("TLEVISACPO.MX")
    replies = {"NEWB": [{"id": 1, "clave": "NEWBX"}], "NEW": [{"id": 4242, "clave": "NEW"}, {"id": 9, "clave": "NEWER"}]}
    monkeypatch.setattr(a.http, "get", lambda url, params=None, **kw: _J(replies.get(params["clave"], [])))
    assert a.entity_key({"yf_ticker": "NEWB.MX", "yf_tickers": []}) == "NEW"  # exact clave match only
    assert a.entity_key({"yf_ticker": "ZZZQ.MX", "yf_tickers": []}) is None


@pytest.mark.network
def test_biva_live_listing(tmp_data):
    a = biva_mx.BivaMxAdapter(settings=tmp_data)
    fs = a.list_filings(GMEX)
    assert len(fs) >= 30 and any(f["form_type"] == "BIVA-Q4D" for f in fs)
