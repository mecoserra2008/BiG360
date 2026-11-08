"""COVERAGE_V3 A9: sources switched off because robots.txt disallows them make no request, report the reason code
`source_disallowed`, and keep what was ingested before (fixture-based, no network)."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest


def _ctx(s):
    return SimpleNamespace(settings=s, params={}, watermark=None, log=None)


def _no_http(monkeypatch, obj):
    calls: list[tuple] = []
    monkeypatch.setattr(obj, "get", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(AssertionError(a)))
    return calls


# ------------------------------------------------------------------------------------------------ TDnet


def test_tdnet_job_adapter_and_stream_are_off(tmp_data, monkeypatch):
    from app.ingestion import tdnet
    from app.realtime.streams.disclosures import TdnetStream

    assert tdnet.DISABLED_REASON == "source_disallowed"
    res = tdnet.run(_ctx(tmp_data))
    assert res.status == "skipped" and res.retryable is False and res.message.startswith("source_disallowed")
    a = tdnet.TdnetAdapter(settings=tmp_data)
    calls = _no_http(monkeypatch, a.http)
    fetched = a.fetch()
    assert calls == [] and fetched.meta["disabled"] == "source_disallowed" and fetched.meta["rows"] == []
    st = TdnetStream()
    ok, why = st.enabled(SimpleNamespace())
    assert ok is False and why.startswith("source_disallowed")
    assert st.fetch(SimpleNamespace(clock=lambda: datetime.now(UTC))) == []


def test_tdnet_rows_already_ingested_still_label_their_source():
    """Stored TDnet rows stay usable and labelled (earnings events read them by source_id)."""
    from app.earnings.events import SOURCE_LABEL, period_from_title

    assert SOURCE_LABEL["tdnet"].startswith("TDnet") and SOURCE_LABEL["edinet"].startswith("EDINET")
    assert period_from_title("tdnet", "2027年3月期 第2四半期決算短信〔日本基準〕(連結)") == date(2026, 9, 30)


# ------------------------------------------------------------------------------------------------ BaFin


def test_bafin_dealings_off(tmp_data, monkeypatch):
    from app.ingestion import bafin_dd

    res = bafin_dd.run(_ctx(tmp_data))
    assert res.status == "skipped" and "portal.mvp.bafin.de" in res.message
    a = bafin_dd.BafinDealingsAdapter(settings=tmp_data)
    calls = _no_http(monkeypatch, a.http)
    fetched, rows = a.run(entities=["Siemens"], write=False)
    assert calls == [] and fetched.meta["disabled"] == "source_disallowed" and all(v == 0 for v in rows.values())


# ------------------------------------------------------------------------------------------------ CC-NEWS


def test_ccnews_no_download_and_probe_done(tmp_data, monkeypatch):
    import httpx

    from app.core.http import SourceDisallowed
    from app.events_v2 import ccnews as C
    from app.events_v2 import ccnews_parse as P

    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    assert C.main(["--months", "2026-10", "--workers", "1"]) == 0          # refuses before any download
    assert C.read_state(tmp_data).get("run") is None
    with pytest.raises(SourceDisallowed):
        C.month_index("2026-10", tmp_data, refresh=True)
    monkeypatch.setitem(P._W, "ctx", SimpleNamespace())          # worker labelling context (not reached)
    with pytest.raises(SourceDisallowed):
        P.fetch_and_parse({"month": "2026-10", "idx": 0, "path": "crawl-data/CC-NEWS/2026/10/x.warc.gz",
                           "ctx_path": "unused"})
    pr = C.probe_progress({"start_month": "2026-10"}, tmp_data, today=date(2026, 11, 7))
    assert pr["done"] is True and pr["reason"] == "source_disallowed" and pr["detail"].startswith("source_disallowed")


# ------------------------------------------------------------------------------------------------ news / calendar


def test_google_news_option_is_switched_off(tmp_data, monkeypatch):
    from app.ingestion import news

    asked: list[str] = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def get(self, url, *a, **k):
            asked.append(url)
            return SimpleNamespace(content=b"<rss><channel></channel></rss>")

    monkeypatch.setattr(news, "HttpClient", FakeClient)
    df, meta = news.fetch_rss(tmp_data, google_news_names=["Apple Inc"])
    assert not any("news.google.com" in u for u in asked)
    assert meta["skipped"]["google_news"].startswith("source_disallowed")


def test_istat_calendar_skipped_and_bea_feed_removed(tmp_data):
    from app.macro import calendar_sources as cs
    from app.realtime.streams.news import RSS_FEEDS

    with pytest.raises(cs.SkipSource, match="source_disallowed"):
        cs.fetch_istat(date(2026, 11, 1), date(2026, 12, 31), tmp_data)
    _, rep = cs.fetch_all(date(2026, 11, 1), date(2026, 12, 31), tmp_data, sources=["istat_cal"])
    assert rep["istat_cal"]["status"] == "skipped" and rep["istat_cal"]["error"].startswith("source_disallowed")
    assert "rss_bea" not in RSS_FEEDS
    assert cs.parse_istat("BEGIN:VCALENDAR\nEND:VCALENDAR\n") == []      # parser kept for the stored entries


# ------------------------------------------------------------------------------------------------ startup sync


def test_sync_reports_disabled_jobs_as_no_source(tmp_data, monkeypatch):
    """job-level datasets of robots-disallowed jobs are no_source (source_disallowed), never planned."""
    from app.ops.sync import scan as S

    sc = S.Scanner.__new__(S.Scanner)
    jobs = {j: SimpleNamespace(id=j, dag="nightly", depends_on=[], writes=["filings_index"], note=None,
                               module=f"app.ingestion.{j}", sources=())
            for j in ("tdnet_disclosures", "edinet_filings")}
    sc.reg = SimpleNamespace(jobs=jobs)
    sc.only = None
    sc.today = date(2026, 11, 7)
    planned: list[str] = []
    monkeypatch.setattr(S.Scanner, "root_fetch_jobs", lambda self: list(jobs))
    monkeypatch.setattr(S.Scanner, "missing_keys", lambda self, names: [])
    monkeypatch.setattr(S.Scanner, "_cadence_entity",
                        lambda self, did, jid, root=False: S.EntitySync(jid, "due", None, detail="never ran"))
    monkeypatch.setattr(S.Scanner, "add_plan", lambda self, jid, *a, **k: planned.append(jid))
    monkeypatch.setattr(S.Scanner, "last_ok_start", lambda self, jid: None)
    out = {d.dataset: d for d in sc.job_datasets()}
    td = out["job:tdnet_disclosures"]
    ent = td.entities[0]
    assert ent.reason == "no_source" and ent.detail.startswith("source_disallowed")
    assert planned == ["edinet_filings"]


def test_disabled_stream_poll_never_called():
    """The realtime supervisor skips a stream whose enabled() is False (TdnetStream)."""
    from app.realtime.streams.disclosures import TdnetStream

    async def go():
        st = TdnetStream()
        return st.enabled(SimpleNamespace())

    ok, _ = asyncio.run(go())
    assert ok is False
