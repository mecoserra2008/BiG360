"""COVERAGE_V3 A9: the realtime EDGAR stream reads the SEC XBRL RSS (allowed) instead of the robots-disallowed
/cgi-bin getcurrent Atom; 8-K items come from the filing's SGML header. Recorded fixtures (2026-11-06), no network."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from app.core.robots_policy import find_block
from app.realtime.streams import edgar as E

F = Path(__file__).parent / "fixtures"


def test_parse_xbrl_rss_fixture():
    rows = E.parse_xbrl_rss((F / "edgar_xbrlrss_sample.xml").read_text(encoding="utf-8"))
    assert [r["form_type"] for r in rows] == ["10-Q", "8-K", "424B2", "20-F"]
    q = rows[0]
    assert q["accession"] == "0001193125-26-452129" and q["company"]["cik"] == "0001425355"
    # acceptanceDatetime is US/Eastern wall time: 17:30:32 EST = 22:30:32 UTC
    assert q["accepted_at"] == datetime(2026, 11, 6, 22, 30, 32, tzinfo=UTC) and q["filed_date"] == "2026-11-06"
    assert q["company"]["url"].endswith("-index.htm") and q["items"] == [] and q["owner"] is None
    env = E.envelope(rows[1], {"issuer_id": "I1", "security_id": "S1", "ticker": "PONO"})
    assert env.key == "sec_edgar:0001493152-26-050548" and env.payload["form_type"] == "8-K"
    assert env.payload["knowledge_ts"] == "2026-11-06T22:30:10Z"
    assert [E.keep_form(r["form_type"]) for r in rows] == [True, True, False, True]


def test_hdr_items_and_url():
    assert E.parse_hdr_items((F / "edgar_8k_sample.hdr.sgml").read_text(encoding="utf-8")) == ["1.01", "9.01"]
    assert E.parse_hdr_items("<ITEMS>2.02\n<ITEMS>9.01\n<ITEMS>2.02\n") == ["2.02", "9.01"]
    u = E.hdr_url("0002108164", "0001493152-26-050548")
    assert u == ("https://www.sec.gov/Archives/edgar/data/2108164/000149315226050548/"
                 "0001493152-26-050548.hdr.sgml")
    assert find_block(u) is None and find_block(E.XBRL_FEED) is None and find_block(E.FEED) is not None


def test_stream_uses_xbrl_feed_and_fetches_8k_headers_only():
    st = E.EdgarStream(universe_only=False)
    assert st.legacy is False and "XBRL RSS" in st.stats.detail
    rss = (F / "edgar_xbrlrss_sample.xml").read_text(encoding="utf-8")
    hdr = (F / "edgar_8k_sample.hdr.sgml").read_text(encoding="utf-8")
    calls: list[str] = []
    st.fetch_page = lambda start: (_ for _ in ()).throw(AssertionError("cgi-bin feed requested"))  # type: ignore
    st.fetch_xbrl = lambda: calls.append("rss") or rss  # type: ignore[method-assign]
    st.fetch_hdr = lambda cik, acc: calls.append(acc) or hdr  # type: ignore[method-assign]

    async def run_sync(fn, *a):
        return fn(*a)

    ctx = SimpleNamespace(run_sync=run_sync, linker=None, clock=lambda: datetime(2026, 11, 6, 22, 0, tzinfo=UTC))
    envs = asyncio.run(st.poll(ctx))
    assert calls == ["rss", "0001493152-26-050548"]          # one header request, for the 8-K only
    forms = {e.payload["form_type"]: e.payload for e in envs}
    assert set(forms) == {"10-Q", "8-K", "20-F"} and forms["8-K"]["items"] == ["1.01", "9.01"]
    assert asyncio.run(st.poll(ctx)) == [] and calls[-1] == "rss"   # known accessions are not re-emitted
    assert st.interval(ctx) == 180.0 and st.interval(SimpleNamespace(
        clock=lambda: datetime(2026, 11, 7, 12, 0, tzinfo=UTC))) == 600.0   # Saturday


def _ctx(clock):
    async def run_sync(fn, *a):
        return fn(*a)

    return SimpleNamespace(run_sync=run_sync, linker=None, clock=clock)


def test_header_failure_holds_the_8k_back_and_retries_next_poll():
    """A failed .hdr.sgml request must not emit the 8-K with items=None (live_filings keeps the first-seen row, so the
    Item 2.02 earnings signal would be lost): it waits and is retried on the next poll."""
    st = E.EdgarStream(universe_only=False)
    rss = (F / "edgar_xbrlrss_sample.xml").read_text(encoding="utf-8")
    hdr = (F / "edgar_8k_sample.hdr.sgml").read_text(encoding="utf-8")
    st.fetch_xbrl = lambda: rss  # type: ignore[method-assign]
    fail = {"on": True}
    calls: list[str] = []

    def fetch_hdr(cik, acc):
        calls.append(acc)
        if fail["on"]:
            raise RuntimeError("503")
        return hdr

    st.fetch_hdr = fetch_hdr  # type: ignore[method-assign]
    now = {"t": datetime(2026, 11, 6, 22, 31, tzinfo=UTC)}
    ctx = _ctx(lambda: now["t"])
    envs = asyncio.run(st.poll(ctx))
    assert {e.payload["form_type"] for e in envs} == {"10-Q", "20-F"}          # the 8-K is held back
    assert list(st._pending) == ["0001493152-26-050548"] and "8-K headers pending 1" in st.stats.detail
    fail["on"] = False
    now["t"] = datetime(2026, 11, 6, 22, 34, tzinfo=UTC)
    envs = asyncio.run(st.poll(ctx))                                          # nothing fresh, the pending 8-K retried
    assert [e.payload["form_type"] for e in envs] == ["8-K"] and envs[0].payload["items"] == ["1.01", "9.01"]
    assert "items_unresolved" not in envs[0].payload and not st._pending and calls == ["0001493152-26-050548"] * 2
    assert asyncio.run(st.poll(ctx)) == [] and st.stats.detail == E.DETAIL_XBRL


def test_unresolved_8k_is_emitted_flagged_after_max_age():
    st = E.EdgarStream(universe_only=False)
    st.fetch_xbrl = lambda: (F / "edgar_xbrlrss_sample.xml").read_text(encoding="utf-8")  # type: ignore[method-assign]
    st.fetch_hdr = lambda cik, acc: (_ for _ in ()).throw(RuntimeError("503"))  # type: ignore[method-assign]
    t0 = datetime(2026, 11, 6, 22, 31, tzinfo=UTC)
    now = {"t": t0}
    ctx = _ctx(lambda: now["t"])
    asyncio.run(st.poll(ctx))
    now["t"] = t0 + timedelta(seconds=E.PENDING_MAX_AGE_S - 1)
    assert asyncio.run(st.poll(ctx)) == []
    now["t"] = t0 + timedelta(seconds=E.PENDING_MAX_AGE_S)
    envs = asyncio.run(st.poll(ctx))
    assert [e.payload["form_type"] for e in envs] == ["8-K"]
    assert envs[0].payload["items"] is None and envs[0].payload["items_unresolved"] is True
    assert st.items_unresolved == 1 and not st._pending and "8-Ks without items 1" in st.stats.detail


def test_8ks_past_the_header_cap_carry_over(monkeypatch):
    monkeypatch.setattr(E, "MAX_HDR_PER_POLL", 2)
    st = E.EdgarStream(universe_only=False)
    rows = []
    for i in range(5):
        acc = f"0000000001-26-00000{i}"
        ent = {"cik": "0000000001", "name": f"C{i}", "role": "Filer", "url": None}
        rows.append({"accession": acc, "form_type": "8-K", "items": [], "filed_date": "2026-11-06", "entities": [ent],
                     "accepted_at": datetime(2026, 11, 6, 21, i, tzinfo=UTC), "company": ent, "owner": None})
    st.collect = lambda: [dict(r) for r in rows]  # type: ignore[method-assign]
    calls: list[str] = []
    st.fetch_hdr = lambda cik, acc: calls.append(acc) or "<ITEMS>2.02"  # type: ignore[method-assign]
    ctx = _ctx(lambda: datetime(2026, 11, 6, 21, 10, tzinfo=UTC))
    got = [e.payload["accession"] for e in asyncio.run(st.poll(ctx))]
    assert len(got) == 2 and len(st._pending) == 3
    got += [e.payload["accession"] for e in asyncio.run(st.poll(ctx))]
    got += [e.payload["accession"] for e in asyncio.run(st.poll(ctx))]
    assert sorted(got) == sorted(r["accession"] for r in rows) and len(calls) == 5 and not st._pending


def test_feed_overflow_is_counted():
    st = E.EdgarStream(universe_only=False)

    def row(acc, minute):
        ent = {"cik": "0000000002", "name": "X", "role": "Filer", "url": None}
        return {"accession": acc, "form_type": "10-Q", "items": [], "filed_date": "2026-11-06", "entities": [ent],
                "accepted_at": datetime(2026, 11, 6, 20, minute, tzinfo=UTC), "company": ent, "owner": None}

    feeds = [[row("a1", 1), row("a2", 2)], [row("a2", 2), row("a3", 3)], [row("a9", 9), row("a8", 8)]]
    st.collect = lambda: feeds.pop(0)  # type: ignore[method-assign]
    ctx = _ctx(lambda: datetime(2026, 11, 6, 20, 10, tzinfo=UTC))
    asyncio.run(st.poll(ctx))
    asyncio.run(st.poll(ctx))
    assert st.overflows == 0                     # the second feed reaches back to the first
    asyncio.run(st.poll(ctx))
    assert st.overflows == 1 and "feed overflows 1" in st.stats.detail
