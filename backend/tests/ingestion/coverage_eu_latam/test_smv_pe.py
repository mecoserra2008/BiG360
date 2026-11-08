"""coverage-B: SMV Peru adapter (ASP.NET grid -> Fecha de Presentación, structured statements) on trimmed fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.ingestion import smv_pe
from app.ingestion._global_common import frame

FX = Path(__file__).parent / "fixtures"
BAP = {"issuer_id": "ITESTBAP00001", "name": "Credicorp", "country": "PE", "region": "LATAM", "lei": None,
       "leis": [], "isin": None, "yf_ticker": "BAP", "yf_tickers": ["BAP"], "tickers": ["BAP@XNYS"], "fye_month": None}


def test_presentation_ts_and_header():
    assert smv_pe.presentation_ts("31/03/2026 05:21:01 p.m.") == datetime(2026, 3, 31, 22, 21, 1, tzinfo=UTC)
    assert smv_pe.presentation_ts("29/03/2004 11:16:27 a.m.") == datetime(2004, 3, 29, 16, 16, 27, tzinfo=UTC)
    assert smv_pe.presentation_ts("15/05/2026 12:05:00 a.m.").hour == 5  # 00:05 Lima
    h = smv_pe.parse_header("CONSOLIDADA | ANUAL AL 31 DE DICIEMBRE DEL 2012 ( EN MILES DE DÓLARES AMERICANOS )")
    assert h["period_end"] == date(2012, 12, 31) and h["currency"] == "USD" and h["scale"] == 1000 and h["annual"]
    h = smv_pe.parse_header("CONSOLIDADA | TRIMESTRE III AL 30 DE SEPTIEMBRE DEL 2025 ( EN MILES DE NUEVOS SOLES )")
    assert h["period_end"] == date(2025, 9, 30) and h["currency"] == "PEN" and not h["annual"]


def test_parse_grid():
    rows = smv_pe.parse_grid((FX / "smv_grid_annual.html").read_bytes())
    assert rows and all(r["ts"].tzinfo for r in rows)
    ef = [r for r in rows if r["document"] == "Estados Financieros"]
    assert ef and ef[0]["lead"] and ef[0]["lead"][0].isdigit() and "DetalleInfoFinanciera" in ef[0]["href"]


def test_statements_quarterly():
    info, rows = smv_pe.statements_to_rows((FX / "smv_detail_2025q3_small.html").read_bytes(), source_id="smv_pe",
                                           source_entity_id="18049", filing_id="smv_pe:x", form_type="SMV-EEFF-T",
                                           knowledge_date=date(2025, 11, 14))
    df = frame(rows, "fundamentals_raw")
    assert info["period_end"] == date(2025, 9, 30) and df.height > 10
    pl_q = df.filter((df["concept"] == "ProfitLoss") & (df["fiscal_period"] == "Q3") & (df["period_end"] == date(2025, 9, 30)))
    assert pl_q["value"][0] == pytest.approx(1_778_500_000, rel=1e-3) and pl_q["unit"][0] == "PEN"
    pl_9m = df.filter((df["concept"] == "ProfitLoss") & (df["fiscal_period"] == "9M") & (df["period_end"] == date(2025, 9, 30)))
    assert pl_9m["period_start"][0] == date(2025, 1, 1) and pl_9m["value"][0] == pytest.approx(5_457_768_000)
    assets = df.filter((df["concept"] == "Assets") & (df["taxonomy"] == "ifrs-full"))
    assert set(assets["period_end"]) == {date(2025, 9, 30), date(2024, 12, 31)} and assets["period_start"].is_null().all()
    assert (df.filter(df["taxonomy"] == "other")["concept"].str.starts_with("smv:")).all()
    assert (df["period_end"] <= date(2025, 9, 30)).all()


def test_check_period_guard():
    info = smv_pe.parse_header("CONSOLIDADA | TRIMESTRE I AL 31 DE MARZO DEL 2025 ( EN MILES DE NUEVOS SOLES )")
    smv_pe.check_period("2025/I/T", info)
    with pytest.raises(RuntimeError):
        smv_pe.check_period("2025/A", info)  # annual grid row resolved to an interim page (stale session link)
    with pytest.raises(RuntimeError):
        smv_pe.check_period("2025/II/T", info)


def test_smv_fetch_normalize(tmp_data, monkeypatch):
    a = smv_pe.SmvPeAdapter(settings=tmp_data, issuers=[BAP])
    detail = (FX / "smv_detail_2025q3_small.html").read_bytes()
    ts = datetime(2025, 11, 14, 4, 0, tzinfo=UTC)
    grid = [{"document": "Estados Financieros", "expediente": "2025999", "ts": ts, "lead": ["2025", "III"],
             "href": "Frm_DetalleInfoFinanciera.aspx?data=ABC"},
            {"document": "Estados Financieros y Dictamen", "expediente": "2025999", "ts": ts, "lead": ["2025", "III"],
             "href": "https://www.smv.gob.pe/ConsultasP8/documento.aspx?vidDoc={X}"}]

    def fake_search(company, periodo, on_page=None):
        assert company == "18049"
        rows = grid if periodo == "T" else []
        if on_page:
            on_page(rows)
        return rows

    gets: list[str] = []

    class _R:
        content = detail

    def fake_get(url, *a_, **kw):
        gets.append(url)
        return _R()

    monkeypatch.setattr(a, "_search", fake_search)
    monkeypatch.setattr(a.http, "get", fake_get)
    fetched, rows = a.run(write=True)
    assert fetched.status == "success" and rows["filings_index"] == 1 and rows["fundamentals_raw"] > 0
    assert gets == [smv_pe.BASE + "/SIMV/Frm_DetalleInfoFinanciera.aspx?data=ABC"]
    t = a.normalize(fetched)
    f = t["filings_index"].row(0, named=True)
    assert f["form_class"] == "quarterly" and f["fiscal_period"] == "Q3" and f["period_end"] == date(2025, 9, 30)
    assert f["knowledge_date"] == date(2025, 11, 14) and f["source_entity_id"] == "18049" and f["issuer_id"] == "ITESTBAP00001"
    fr = t["fundamentals_raw"]
    assert (fr["knowledge_date"] == date(2025, 11, 14)).all() and (fr["issuer_id"] == "ITESTBAP00001").all()
    # resumable: nothing re-downloaded
    a2 = smv_pe.SmvPeAdapter(settings=tmp_data, issuers=[BAP])
    monkeypatch.setattr(a2, "_search", fake_search)
    monkeypatch.setattr(a2.http, "get", fake_get)
    assert not a2.fetch().meta["filings"] and len(gets) == 1


@pytest.mark.network
def test_smv_live_listing(tmp_data):
    a = smv_pe.SmvPeAdapter(settings=tmp_data, max_new=0)
    rows = a._search("18049", "A")
    assert any(r["document"] == "Estados Financieros" for r in rows)


def test_presentation_currency_override():
    # Credicorp: filer-typed header says soles in 2010-Q4 / 2012-Q2, but it presented in USD until FY2013
    assert smv_pe.presentation_currency("18049", date(2010, 12, 31), "PEN") == "USD"
    assert smv_pe.presentation_currency("18049", date(2014, 3, 31), "PEN") == "PEN"
    assert smv_pe.presentation_currency("99999", date(2010, 12, 31), "PEN") == "PEN"
