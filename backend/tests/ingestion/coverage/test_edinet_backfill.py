"""EDINET backfill mode: chunked walk over the daily document list with a resumable cursor (recorded type=5 CSV zip
from tests/ingestion/global/fixtures)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from app.ingestion import edinet
from app.ingestion._global_common import load_state

ZIP = Path(__file__).parents[1] / "global" / "fixtures" / "edinet_S100YE2C_type5.zip"
DOC = {"docID": "S100YE2C", "docTypeCode": "120", "edinetCode": "E01777", "secCode": "72030", "csvFlag": "1",
       "xbrlFlag": "1", "withdrawalStatus": "0", "submitDateTime": "2026-06-18 15:00", "periodEnd": "2026-03-31"}


def _patch(monkeypatch, calls: list[date]):
    def day_list(self, d):
        calls.append(d)
        return [DOC] if d == date(2016, 6, 18) else []

    class R:
        content = ZIP.read_bytes()

    monkeypatch.setattr(edinet.EdinetAdapter, "day_list", day_list)
    monkeypatch.setattr(edinet.EdinetAdapter, "_universe", lambda self, e: ({"E01777"}, {"72030"}))
    import app.core.http as H

    monkeypatch.setattr(H.HttpClient, "get", lambda self, *a, **k: R())


def test_backfill_chunks_and_resume(tmp_data, monkeypatch):
    from pydantic import SecretStr

    tmp_data.edinet_apikey = SecretStr("x")  # never sent anywhere: HTTP is patched
    calls: list[date] = []
    _patch(monkeypatch, calls)
    ctx = SimpleNamespace(settings=tmp_data, params={"mode": "backfill", "backfill_start": "2016-06-10",
                                                     "until": "2016-06-29", "chunk_days": 7, "max_chunks": 2})
    res = edinet.run(ctx)
    assert res.status == "success" and "in progress" in res.message
    assert calls[0] == date(2016, 6, 10) and calls[-1] == date(2016, 6, 23) and len(calls) == 14
    st = load_state("edinet", tmp_data)
    assert st["backfill_cursor"] == "2016-06-24" and "watermark" not in st  # incremental watermark untouched
    assert res.rows_written["fundamentals_raw"] > 10 and res.rows_written["filings_index"] == 1
    calls.clear()
    ctx.params = {"mode": "backfill", "backfill_start": "2016-06-10", "until": "2016-06-29", "chunk_days": 7}
    res2 = edinet.run(ctx)
    assert calls[0] == date(2016, 6, 24) and calls[-1] == date(2016, 6, 29) and "complete" in res2.message
    assert load_state("edinet", tmp_data)["backfill_cursor"] == "2016-06-30"


def test_backfill_job_registered():
    import yaml
    from app.core.config import get_settings

    jobs = yaml.safe_load((get_settings().contracts_dir / "jobs.yaml").read_text(encoding="utf-8"))["jobs"]
    j = next(x for x in jobs if x["id"] == "edinet_backfill")
    assert j["module"] == "app.ingestion.edinet" and j["params"]["mode"] == "backfill" and j["dag"] == "manual"
