"""COVERAGE_V3 A9: robots.txt deny list (app.core.robots_policy), its enforcement in app.core.http, and the compliance
scanner (app.ops.robots_compliance). Fixture-based, no network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.core import robots_policy as RP
from app.core.http import AsyncHttpClient, HttpClient, HttpError, SourceDisallowed
from app.ops import robots_compliance as RC

ROOT = Path(__file__).resolve().parents[3]


# ------------------------------------------------------------------------------------------------ policy


@pytest.mark.parametrize("url", [
    "https://www.release.tdnet.info/inbs/I_list_001_20261106.html",
    "https://release.tdnet.info/inbs/140120261008000001.pdf",
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-FromShowColumns.asp?csv.x=yes",
    "https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?SeriesCodes=IUDBEDR",   # IIS case
    "https://www.bankofengland.co.uk/BOEAPPS/IADB/Index.asp",
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom",
    "https://apps.bea.gov/rss/rss.xml",
    "https://news.google.com/rss/search?q=x",
    "https://calendar.google.com/calendar/ical/x%40group.calendar.google.com/public/basic.ics",
    "https://data.commoncrawl.org/crawl-data/CC-NEWS/2026/10/warc.paths.gz",
    "https://portal.mvp.bafin.de/database/DealingsInfo/sucheForm.do?emittentName=SAP",
    "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets/flux-amf-new-prod/records",
    "https://fr.ftp.opendatasoft.com/datadila/INFOFI/136/8888/01/FC136619656_20260319.zip",
])
def test_blocked_urls(url):
    b = RP.find_block(url)
    assert b is not None and b.enforce and RP.is_blocked(url)


@pytest.mark.parametrize("url", [
    "https://www.bankofengland.co.uk/-/media/boe/files/statistics/yield-curves/latest-yield-curve-data.zip",
    "https://www.bankofengland.co.uk/-/media/boe/files/monetary-policy-summary-and-minutes/mpcvoting.xlsx",
    "https://www.bankofengland.co.uk/rss/news",
    "https://www.sec.gov/Archives/edgar/xbrlrss.all.xml",
    "https://www.sec.gov/Archives/edgar/data/320193/000032019326000001/0000320193-26-000001.hdr.sgml",
    "https://www.info-financiere.gouv.fr/",                     # only /api/ is disallowed
    "https://apps.bea.gov/api/data",                           # /api/ is allowed by BEA's robots.txt
    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html",
    "https://api.edinet-fsa.go.jp/api/v2/documents.json",
    "https://www.release.tdnet.info/robots.txt",              # robots.txt itself is always allowed (RFC 9309)
    "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-shibor/ShiborHis",
    "not a url",
])
def test_allowed_urls(url):
    assert RP.find_block(url) is None


def test_listed_only_entries_are_not_enforced():
    listed = {b.host: b for b in RP.listed_only()}
    assert {"mopsov.twse.com.tw", "doc.twse.com.tw", "query1.finance.yahoo.com", "query2.finance.yahoo.com"} <= set(listed)
    assert listed["mopsov.twse.com.tw"].owner == "B6" and listed["query1.finance.yahoo.com"].owner == "user"
    assert RP.find_block("https://mopsov.twse.com.tw/server-java/t164sb01") is None
    assert RP.find_block("https://mopsov.twse.com.tw/server-java/t164sb01", include_listed=True).owner == "B6"
    assert RP.find_block("https://query2.finance.yahoo.com/v8/finance/chart/AAPL") is None


def test_disabled_sources_and_jobs_resolve_to_enforced_blocks():
    for sid in RP.DISABLED_SOURCES:
        note = RP.disabled_note(sid)
        assert note and note.startswith("source_disallowed: ") and "checked 2026-11-07" in note
    for jid in RP.DISABLED_JOBS:
        assert RP.disabled_job_note(jid).startswith("source_disallowed: ")
    assert RP.disabled_note("sec_edgar") is None and RP.disabled_job_note("edinet_filings") is None
    res = RP.skipped_job("eu_pdmr_bafin")
    assert res.status == "skipped" and res.retryable is False and "portal.mvp.bafin.de" in res.message
    assert RP.skipped_job("edinet") is None


# ------------------------------------------------------------------------------------------------ http enforcement


def _mock_client(cl, seen):
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "redirect.example.org":
            return httpx.Response(302, headers={"location": "https://portal.mvp.bafin.de/database/DealingsInfo/"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /\n")

    from app.core.http import _guard_request  # noqa: PLC0415

    cl._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                              event_hooks={"request": [_guard_request]})


def test_http_client_refuses_blocked_urls_before_any_io(tmp_data):
    seen: list[str] = []
    cl = HttpClient("a9_test", settings=tmp_data, cache=False, max_retries=0)
    _mock_client(cl, seen)
    with pytest.raises(SourceDisallowed) as ei:
        cl.get("https://www.release.tdnet.info/inbs/I_list_001_20261106.html")
    e = ei.value
    assert isinstance(e, HttpError) and e.status == 451 and e.reason == "source_disallowed" and seen == []
    with pytest.raises(SourceDisallowed):
        cl.download("https://data.commoncrawl.org/crawl-data/CC-NEWS/2026/10/warc.paths.gz", tmp_data.data_dir / "x")
    assert seen == []
    # robots.txt of a blocked host stays fetchable (the audit reads it)
    assert cl.get("https://www.release.tdnet.info/robots.txt").status_code == 200 and len(seen) == 1
    # a redirect hop into a blocked host is refused too (request hook)
    with pytest.raises(SourceDisallowed):
        cl.get("https://redirect.example.org/start")
    assert seen[-1] == "https://redirect.example.org/start"
    cl.close()


def test_async_client_refuses_blocked_urls(tmp_data):
    import asyncio

    async def go():
        cl = AsyncHttpClient("a9_test", settings=tmp_data, cache=False, max_retries=0)
        try:
            with pytest.raises(SourceDisallowed):
                await cl.get("https://news.google.com/rss/search", {"q": "x"})
        finally:
            await cl.aclose()

    asyncio.run(go())


# ------------------------------------------------------------------------------------------------ scanner


ROBOTS = """# comment
User-agent: Googlebot
Disallow: /

User-agent: *
Allow: /api/public
Disallow: /api/
Disallow: /*.pdf$
Disallow: /private
Crawl-delay: 5

User-agent: BiG-Equity-360
User-agent: other
Disallow: /mine-only
"""


def test_rfc9309_evaluation():
    # our product token has its own group -> only its rules apply
    assert RC.evaluate(ROBOTS, "/api/x") == (True, None)
    assert RC.evaluate(ROBOTS, "/mine-only/a")[0] is False
    # the '*' group for any other agent: longest match wins, Allow wins a tie, wildcards and '$'
    star = "nobody"
    assert RC.evaluate(ROBOTS, "/api/data", star) == (False, "Disallow: /api/")
    assert RC.evaluate(ROBOTS, "/api/public/x", star) == (True, "Allow: /api/public")
    assert RC.evaluate(ROBOTS, "/doc/a.pdf", star)[0] is False and RC.evaluate(ROBOTS, "/doc/a.pdfx", star)[0]
    assert RC.evaluate(ROBOTS, "/private%7Edir", star)[0] is False
    assert RC.evaluate(ROBOTS, "/robots.txt", star) == (True, None)
    assert RC.evaluate(None, "/anything") == (True, None)            # no robots.txt (4xx) = no restrictions
    tie = "User-agent: *\nDisallow: /a\nAllow: /a\n"
    assert RC.evaluate(tie, "/a/b")[0] is True
    g, which = RC.select_group(RC.parse_robots(ROBOTS), "nobody")
    assert which == "*" and g.crawl_delay == 5
    assert RC.deeper_disallows(ROBOTS, "/api", "nobody") == ["/api/"]
    assert RC.deeper_disallows(ROBOTS, "/", "nobody") == []


def test_enumeration_code_state_and_library(tmp_path):
    app = tmp_path / "backend" / "app" / "ingestion"
    app.mkdir(parents=True)
    (app / "src_a.py").write_text('''"""Docstring https://docstring.example.org/never is not a request."""
BASE = "https://data.example.org/api/v1/"
URL = f"https://files.example.org/dl/{name}.zip"
TERMS = "https://www.example.org/legal/terms"
NS = "http://www.w3.org/1999/xhtml"
''', encoding="utf-8")
    st = tmp_path / "data" / "state"
    st.mkdir(parents=True)
    (st / "oam.json").write_text(json.dumps({"done": {"k": {
        "doc_url": "https://mirror.example.org/pkg/2026/03/file.zip", "url": "https://view.example.org/doc/1"}}}),
        encoding="utf-8")
    refs = RC.enumerate_refs(tmp_path)
    got = {(r.host, r.path, r.kind) for r in refs}
    assert ("data.example.org", "/api/v1/", "request") in got
    assert ("files.example.org", "/dl/", "request") in got
    assert ("www.example.org", "/legal/terms", "link") in got
    assert ("www.w3.org", "/1999/xhtml", "non_request") in got
    assert ("mirror.example.org", "/pkg/2026/", "request") in got          # data-driven download (state doc_url)
    assert not any(r.host in ("docstring.example.org", "view.example.org") for r in refs)
    assert any(r.host == "query2.finance.yahoo.com" and r.origin.startswith("library:yfinance") for r in refs)
    hosts = RC.hosts_to_check(refs)
    assert "www.w3.org" not in hosts and hosts["data.example.org"] == "https"


def test_report_verdicts_actions_and_doc_markers(tmp_path):
    out = tmp_path / "scan"
    (out / "robots").mkdir(parents=True)
    (out / "robots" / "a.example.org.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / "robots" / "www.release.tdnet.info.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (out / "robots" / "b.example.org.txt").write_text("User-agent: *\nDisallow: /api/private\n", encoding="utf-8")
    idx = {"a.example.org": {"status_class": "ok", "checked_at": "2026-11-07T01:00:00+00:00"},
           "www.release.tdnet.info": {"status_class": "ok", "checked_at": "2026-11-07T01:00:00+00:00"},
           "b.example.org": {"status_class": "ok", "checked_at": "2026-11-07T01:00:00+00:00"},
           "c.example.org": {"status_class": "no_robots"}, "d.example.org": {"status_class": "unreachable"}}
    (out / "robots_index.json").write_text(json.dumps(idx), encoding="utf-8")
    refs = [RC.Ref("https", "a.example.org", "/x", "o:1"), RC.Ref("https", "www.release.tdnet.info", "/inbs/", "o:2"),
            RC.Ref("https", "b.example.org", "/api/", "o:3"), RC.Ref("https", "c.example.org", "/", "o:4"),
            RC.Ref("https", "d.example.org", "/q", "o:5"), RC.Ref("https", "e.example.org", "/t", "o:6", "link")]
    v = {x.host: x for x in RC.evaluate_refs(refs, out)}
    assert v["a.example.org"].verdict == "disallowed" and v["c.example.org"].verdict == "allowed"
    assert v["b.example.org"].verdict == "partial" and v["d.example.org"].verdict == "unknown"
    assert v["e.example.org"].verdict == "not_requested"
    table = RC.render_table(list(v.values()))
    lines = {ln.split(" | ")[0].strip("| "): ln for ln in table.splitlines()[2:]}
    assert "e.example.org" not in lines
    assert "blocked at runtime (app.core.robots_policy)" in lines["www.release.tdnet.info"]
    assert lines["a.example.org"].endswith("| review |") and "| none |" in lines["c.example.org"]
    assert RC.ALLOWED_DEEPER in lines["b.example.org"]
    doc = tmp_path / "doc.md"
    doc.write_text(f"# T\n\n{RC.DOC_START}\nold\n{RC.DOC_END}\n\ntail\n", encoding="utf-8")
    assert RC.write_doc_table(doc, table, "stamp")
    txt = doc.read_text(encoding="utf-8")
    assert "old" not in txt and txt.endswith("tail\n") and table in txt and "(stamp)" in txt
    nodoc = tmp_path / "nodoc.md"
    nodoc.write_text("no markers", encoding="utf-8")
    assert RC.write_doc_table(nodoc, table) is False


def test_compliance_doc_lists_every_policy_host():
    doc = (ROOT / "docs" / "it" / "ROBOTS_COMPLIANCE.md").read_text(encoding="utf-8")
    assert RC.DOC_START in doc and RC.DOC_END in doc
    for b in RP.BLOCKS:
        if b.host in ("release.tdnet.info", "www.google.com", "datacenter.jin10.com"):   # aliases, not requested
            continue
        assert f"| {b.host} |" in doc, b.host


def test_httpx_request_hook_for_own_clients():
    seen: list[str] = []
    tr = httpx.MockTransport(lambda req: seen.append(str(req.url)) or httpx.Response(200, text="ok"))
    with httpx.Client(transport=tr, event_hooks={"request": [RP.httpx_request_hook]}) as c:
        assert c.get("https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html").text == "ok"
        with pytest.raises(SourceDisallowed):
            c.get("https://www.release.tdnet.info/inbs/I_list_001_20261106.html")
    assert len(seen) == 1
