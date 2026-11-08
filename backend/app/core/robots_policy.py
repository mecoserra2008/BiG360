"""Robots / terms deny list for automated collection (COVERAGE_V3 A9, docs/it/ROBOTS_COMPLIANCE.md).

Every HTTP request made through app.core.http (HttpClient / AsyncHttpClient, sync and streaming downloads) is checked
against `BLOCKS` before it leaves the process (cached responses included) and again on every redirect hop (httpx
request hook). A request to an enforced block raises
app.core.http.SourceDisallowed (an HttpError with status 451) and never reaches the network. Code that talks to a
source directly (raw httpx) must call `guard(url)` itself or attach `httpx_request_hook` to its client.

Reason code: `source_disallowed` (app.core.availability; contracts/field_availability.yaml reason_codes): the host's
robots.txt (or terms of use) does not allow automated collection, so the source is switched off; data ingested before
the switch stays in the lake, labelled with its source.

Entries with enforce=False are listed only (another work item owns the replacement, e.g. Taiwan MOPS -> item B6) and
are reported by `listed_only()`; they are not blocked here.

`/robots.txt` itself is always allowed (RFC 9309 2.2.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

REASON = "source_disallowed"


@dataclass(frozen=True)
class Block:
    host: str                 # exact host (lower case, no port)
    path_prefix: str          # "/" = whole host
    rule: str                 # the robots.txt / terms rule that applies
    checked: str              # date the rule was verified (YYYY-MM-DD)
    action: str               # what replaced the source
    enforce: bool = True
    #: the server answers paths case-insensitively (IIS): a case variant of a disallowed path is the same resource
    case_insensitive: bool = False
    owner: str = "A9"


BOE_ACTION = ("macro_national boe: gilt zero-coupon yields from the BoE yield-curve archive (/-/media, as W3), Bank "
              "Rate from the MPC voting record (/-/media mpcvoting.xlsx) + BIS WS_CBPOL, SONIA from FRED IUDSOIA; "
              "series without an allowed source (par yields, Bankstats monthly) frozen; cb_balance_sheets boe frozen")

BLOCKS: tuple[Block, ...] = (
    Block("www.release.tdnet.info", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "tdnet_disclosures job and the realtime tdnet stream disabled; JP earnings dates from the JPX schedule + "
          "Yahoo, JP statements from EDINET; TDnet rows ingested before 2026-11-07 stay (source_id tdnet)"),
    Block("release.tdnet.info", "/", "robots.txt `User-agent: *` / `Disallow: /` (same site as www.)", "2026-11-07",
          "as www.release.tdnet.info"),
    # IADB (Interactive Database): robots.txt disallows its CSV endpoint, its chart endpoint and /boeapps/iadb; the
    # whole /boeapps/database/ application is blocked here (IIS: paths are case-insensitive, so the lower-case
    # `_iadb-fromshowcolumns.asp` variant the old fetcher used is the same disallowed resource)
    Block("www.bankofengland.co.uk", "/boeapps/database/", "robots.txt `Disallow: "
          "/boeapps/database/_iadb-FromShowColumns.asp`, `Disallow: /boeapps/database/ShowChart.asp`", "2026-11-07",
          BOE_ACTION, case_insensitive=True),
    Block("www.bankofengland.co.uk", "/boeapps/iadb", "robots.txt `Disallow: /boeapps/iadb`", "2026-11-07",
          BOE_ACTION, case_insensitive=True),
    # ---- full scan 2026-11-07 (python -m app.ops.robots_compliance): further disallowed hosts / paths we requested
    Block("www.sec.gov", "/cgi-bin", "robots.txt `Disallow: /cgi-bin`", "2026-11-07",
          "realtime edgar stream: EDGAR getcurrent Atom -> SEC XBRL RSS (/Archives/edgar/xbrlrss.all.xml) + filing "
          "headers (.hdr.sgml) for the 8-K items; Forms 3/4/5 and Schedule 13D/G (no XBRL) arrive with the nightly "
          "daily-index jobs"),
    Block("apps.bea.gov", "/rss/", "robots.txt `User-agent: *` / `Disallow: /` (only /api/, /iTable/ ... allowed)",
          "2026-11-07", "BEA RSS removed from the realtime fallback list (config/news_feeds.yaml already rejected it); "
          "BEA releases via the macro release calendar (www.bea.gov schedule)"),
    Block("news.google.com", "/rss/", "robots.txt `User-agent: *` / `Disallow: /` (home / topics pages only)",
          "2026-11-07", "news_ingest google_news option (opt-in, off) switched off"),
    Block("calendar.google.com", "/calendar/", "robots.txt `Allow: /$`, `Disallow: /`", "2026-11-07",
          "istat_cal (ISTAT ICS hosted on Google Calendar; ISTAT publishes no other machine-readable calendar) "
          "disabled; stored entries kept"),
    Block("www.google.com", "/calendar/", "redirects to calendar.google.com (robots.txt `Disallow: /`)", "2026-11-07",
          "as calendar.google.com"),
    # AKShare requests these from inside the library (not through app.core.http): app.ingestion.macro_national never
    # calls the AKShare functions that read them (AK_DISALLOWED); listed here so the policy is complete
    Block("cdn.jin10.com", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "SHIBOR from CFETS / chinamoney ShiborHis instead of AKShare macro_china_shibor_all (Jin10)"),
    Block("datacenter.jin10.com", "/", "same site as cdn.jin10.com (Disallow: /)", "2026-11-07",
          "as cdn.jin10.com"),
    Block("data.commoncrawl.org", "/", "robots.txt `User-Agent: *` / `Disallow: /`", "2026-11-07",
          "CC-NEWS historical backfill (app.events_v2.ccnews, campaign ccnews_history) disabled; ccnews rows "
          "already merged into the story layer stay (source ccnews)"),
    Block("portal.mvp.bafin.de", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "eu_pdmr_bafin (BaFin directors' dealings) disabled; stored notices stay (source eu_pdmr_bafin); no other "
          "free machine-readable DE PDMR source"),
    Block("www.info-financiere.gouv.fr", "/api/", "robots.txt `Disallow: /api/`", "2026-11-07",
          "dila_fr (French OAM) disabled; FR ESEF annual reports continue from filings.xbrl.org (esef_xbrl_org); "
          "stored dila_fr filings stay"),
    Block("fr.ftp.opendatasoft.com", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "as www.info-financiere.gouv.fr (the DILA package mirror)"),
    # listed only: another item / the owner decides
    Block("mopsov.twse.com.tw", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "replaced by the TWSE / TPEx OpenAPI (item B6)", enforce=False, owner="B6"),
    Block("doc.twse.com.tw", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "MOPS document server (mops_tw); item B6 replaces MOPS with the TWSE / TPEx OpenAPI", enforce=False,
          owner="B6"),
    Block("query1.finance.yahoo.com", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "OWNER DECISION: Yahoo (yfinance) is a core price / fallback source chosen by the user and used by plan "
          "items B4-B8; labelled unofficial / personal use (docs/it/DATA_LICENSING.md 2.1); not switched off here",
          enforce=False, owner="user"),
    Block("query2.finance.yahoo.com", "/", "robots.txt `User-agent: *` / `Disallow: /`", "2026-11-07",
          "as query1.finance.yahoo.com", enforce=False, owner="user"),
)

#: jobs.yaml jobs whose only source is blocked above: they return `skipped` without a request and the startup sync
#: reports them as no_source (reason code source_disallowed) instead of planning them
DISABLED_JOBS: dict[str, str] = {
    "tdnet_disclosures": "www.release.tdnet.info",
    "eu_pdmr_bafin": "portal.mvp.bafin.de",
    "dila_fr_filings": "www.info-financiere.gouv.fr",
}
#: source ids (adapters / campaigns) switched off by this policy -> host
DISABLED_SOURCES: dict[str, str] = {
    "tdnet": "www.release.tdnet.info",
    "eu_pdmr_bafin": "portal.mvp.bafin.de",
    "dila_fr": "www.info-financiere.gouv.fr",
    "ccnews": "data.commoncrawl.org",
    "istat_cal": "calendar.google.com",
}


def _match(b: Block, host: str, path: str) -> bool:
    if host != b.host:
        return False
    if b.case_insensitive:
        return path.lower().startswith(b.path_prefix.lower())
    return path.startswith(b.path_prefix)


def find_block(url: str, *, include_listed: bool = False) -> Block | None:
    """The block that covers `url` (enforced ones only unless include_listed), else None."""
    try:
        sp = urlsplit(url)
    except ValueError:
        return None
    host = (sp.hostname or "").lower().rstrip(".")
    if not host:
        return None
    path = unquote(sp.path or "/") or "/"
    if path == "/robots.txt":
        return None
    for b in BLOCKS:
        if (b.enforce or include_listed) and _match(b, host, path):
            return b
    return None


def is_blocked(url: str) -> bool:
    return find_block(url) is not None


def guard(url: str) -> None:
    """Raise app.core.http.SourceDisallowed when `url` is covered by an enforced block (call before any network I/O;
    app.core.http does this for every request)."""
    b = find_block(url)
    if b is not None:
        from app.core.http import SourceDisallowed  # noqa: PLC0415 - http imports this module

        raise SourceDisallowed(url, b)


def httpx_request_hook(request: object) -> None:
    """httpx `event_hooks={"request": [httpx_request_hook]}` for code that builds its own httpx.Client: every request,
    redirect hops included, is checked against the deny list."""
    guard(str(getattr(request, "url", "")))


def listed_only() -> list[Block]:
    return [b for b in BLOCKS if not b.enforce]


def host_block(host: str) -> Block | None:
    """First enforced block of `host` (any path)."""
    return next((b for b in BLOCKS if b.enforce and b.host == host), None)


def disabled_note(source_id: str) -> str | None:
    """'source_disallowed: ...' message for a source switched off by this policy (DISABLED_SOURCES), else None."""
    host = DISABLED_SOURCES.get(source_id)
    b = host_block(host) if host else None
    if b is None:
        return None
    return (f"{REASON}: {b.host}{b.path_prefix if b.path_prefix != '/' else ''} {b.rule} (checked {b.checked}); "
            f"{b.action}")


def disabled_job_note(job_id: str) -> str | None:
    host = DISABLED_JOBS.get(job_id)
    b = host_block(host) if host else None
    if b is None:
        return None
    return f"{REASON}: {b.host} {b.rule} (checked {b.checked}); {b.action}"


def skipped_job(source_id: str) -> object | None:
    """JobResult(status=skipped, not retryable) for a job whose source is switched off, else None."""
    note = disabled_note(source_id)
    if note is None:
        return None
    from app.scheduler.base import JobResult  # noqa: PLC0415

    return JobResult(status="skipped", message=note, retryable=False)
