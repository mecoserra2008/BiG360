"""SEC EDGAR latest filings (R1.2) -> raw.filings.

Source: the EDGAR "current events" Atom feed (official, real time)
  https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&owner=include&count=100&output=atom
Each entry: title '<FORM> - <NAME> (<CIK>) (<Role>)' with role Issuer / Reporting / Filer / Subject / Filed by,
<updated> = EDGAR acceptance time (US/Eastern offset), summary 'Filed: <date> AccNo: <acc> Size: ...' plus the 8-K
items ('Item 2.02: Results of Operations ...'). One accession appears once per related entity (Form 4: Reporting +
Issuer; Schedule 13D/G: Filed by + Subject) -> grouped by accession; the company = Issuer / Subject / Filer entry.

Polling: every 30 s on US business hours (06:00-22:00 ET, Mon-Fri), 120 s otherwise; one request per poll through
the shared `sec_edgar` limiter (8 req/s across all processes) with the SEC User-Agent from .env. When none of the 100
returned accessions is known yet (burst, e.g. the 16:00-18:00 ET Form 4 wave) up to 4 more pages are read.
Kept forms: 8-K, 6-K, 10-K, 10-Q, 20-F, 40-F, 3/4/5, SC 13D/G and SCHEDULE 13D/G (+ amendments) of issuers the
identity layer resolves (universe issuers); with `universe_only=False` or an empty identity map everything is kept.
Key: accession number (= nightly filings_index natural key 'sec_edgar:<accession>').

A9 2026-11-07 (docs/COVERAGE_V3_PLAN.md, docs/it/ROBOTS_COMPLIANCE.md): www.sec.gov robots.txt has `Disallow: /cgi-bin`,
so the getcurrent Atom above is no longer requested (app.core.robots_policy blocks it). The stream reads the SEC's XBRL
RSS instead (https://www.sec.gov/Archives/edgar/xbrlrss.all.xml: up to 200 latest XBRL filings, updated every 10
minutes; /Archives/edgar is allowed): 10-K / 10-Q / 8-K / 20-F / 40-F / 6-K with XBRL, acceptance time in US/Eastern.
8-K item numbers (Item 2.02 drives the live earnings signal) come from each kept 8-K's SGML header
(/Archives/edgar/data/<cik>/<acc>/<acc>.hdr.sgml, allowed; one request per new 8-K). Forms 3/4/5 and Schedule
13D/G carry no XBRL: they are not in this feed and arrive with the nightly SEC jobs (daily index) instead of live.
Polling: 180 s on US business hours, 600 s otherwise (the feed itself refreshes every 10 minutes).
An 8-K is emitted only once its header was read: 8-Ks past the per-poll cap or whose header request failed wait in a
pending set and are retried on the next polls; after PENDING_MAX_AGE_S they are emitted without items, flagged
`items_unresolved` (live_filings keeps the first-seen row, so an early emit without items would lose Item 2.02 for
good). The feed holds the latest 200 XBRL filings only: when its oldest acceptance time is newer than the newest one
seen before, filings in between may be missing from the live path (they still arrive with the nightly SEC jobs); such
overflows are logged and counted (`overflows`, shown in the stream detail).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.logsetup import get_logger
from app.realtime.bus import Envelope
from app.realtime.streams.base import StreamContext, StreamWorker

log = get_logger(__name__)

ET = ZoneInfo("America/New_York")
FEED = "https://www.sec.gov/cgi-bin/browse-edgar"      # robots-disallowed (A9): used only if the policy allows it
XBRL_FEED = "https://www.sec.gov/Archives/edgar/xbrlrss.all.xml"
HDR_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nd}/{acc}.hdr.sgml"
MAX_HDR_PER_POLL = 40                                  # SGML headers fetched per poll (8-K items)
PENDING_MAX_AGE_S = 3600.0                             # an unresolved 8-K is emitted without items after this
DETAIL_XBRL = "EDGAR XBRL RSS (A9: /cgi-bin disallowed)"
_XF = {k: re.compile(rf"<edgar:{k}>(.*?)</edgar:{k}>", re.S)
       for k in ("companyName", "formType", "filingDate", "cikNumber", "accessionNumber", "acceptanceDatetime")}
_HDR_ITEM = re.compile(r"^<ITEMS>\s*(\d{1,2}\.\d{2})", re.M)
KEEP_BASE = ("8-K", "6-K", "10-K", "10-Q", "20-F", "40-F", "3", "4", "5", "SC 13D", "SC 13G", "SCHEDULE 13D",
             "SCHEDULE 13G", "10-KT", "10-QT")
COMPANY_ROLES = ("Issuer", "Subject", "Filer", "Subject Company")
_TITLE = re.compile(r"^(?P<form>.+?) - (?P<name>.*) \((?P<cik>\d{4,10})\) \((?P<role>[^)]+)\)\s*$")
_ACC = re.compile(r"AccNo:</b>\s*([0-9]{10}-[0-9]{2}-[0-9]{6})")
_FILED = re.compile(r"Filed:</b>\s*(\d{4}-\d{2}-\d{2})")
_ITEM = re.compile(r"Item (\d{1,2}\.\d{2})")


def base_form(form: str) -> str:
    return form.strip().upper().removesuffix("/A").strip()


def keep_form(form: str) -> bool:
    return base_form(form) in KEEP_BASE


def form_class(form: str) -> str:
    try:
        from app.ingestion.sec_index import form_class as fc  # noqa: PLC0415

        return fc(form)
    except Exception:  # noqa: BLE001  # pragma: no cover
        return "other"


def parse_atom(text: str) -> list[dict[str, Any]]:
    """Atom text -> one dict per accession (company entity + reporting owner), newest first."""
    import feedparser  # noqa: PLC0415

    feed = feedparser.parse(text)
    by_acc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for e in feed.entries:
        m = _TITLE.match(html.unescape(e.get("title", "")))
        summ = html.unescape(e.get("summary", "") or "")
        acc_m = _ACC.search(summ) or re.search(r"accession-number=([0-9-]{20})", e.get("id", ""))
        if not (m and acc_m):
            continue
        acc = acc_m.group(1)
        upd = e.get("updated") or ""
        try:
            ts = datetime.fromisoformat(upd).astimezone(UTC)
        except ValueError:
            ts = None
        filed = _FILED.search(summ)
        role = m.group("role").strip()
        d = by_acc.get(acc)
        if d is None:
            d = by_acc[acc] = {"accession": acc, "form_type": m.group("form").strip(), "accepted_at": ts,
                               "filed_date": filed.group(1) if filed else None,
                               "items": sorted(set(_ITEM.findall(summ))), "entities": []}
            order.append(acc)
        d["entities"].append({"cik": m.group("cik").zfill(10), "name": m.group("name").strip(), "role": role,
                              "url": e.get("link")})
        if not d["items"]:
            d["items"] = sorted(set(_ITEM.findall(summ)))
    out = []
    for acc in order:
        d = by_acc[acc]
        ents = d["entities"]
        comp = next((x for x in ents if x["role"] in COMPANY_ROLES), None) or ents[0]
        owner = next((x for x in ents if x["role"] in ("Reporting", "Filed by")), None)
        d["company"] = comp
        d["owner"] = owner if owner is not comp else None
        out.append(d)
    return out


def legacy_feed_allowed() -> bool:
    from app.core.robots_policy import find_block  # noqa: PLC0415

    return find_block(FEED) is None


def parse_xbrl_rss(text: str) -> list[dict[str, Any]]:
    """SEC XBRL RSS (xbrlrss.all.xml) -> one dict per accession in the parse_atom shape (company = the filer; no items,
    no reporting owner), newest acceptance first."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in re.findall(r"<item>(.*?)</item>", text, re.S):
        g = {k: (m.group(1).strip() if (m := rx.search(item)) else None) for k, rx in _XF.items()}
        acc, cik, form = g["accessionNumber"], g["cikNumber"], g["formType"]
        if not (acc and cik and form) or acc in seen:
            continue
        seen.add(acc)
        ts = None
        if g["acceptanceDatetime"]:
            try:   # US/Eastern wall time (matches the item pubDate 'EST/EDT')
                ts = datetime.strptime(g["acceptanceDatetime"], "%Y%m%d%H%M%S").replace(tzinfo=ET).astimezone(UTC)
            except ValueError:
                ts = None
        filed = None
        if g["filingDate"]:
            try:
                filed = datetime.strptime(g["filingDate"], "%m/%d/%Y").date().isoformat()
            except ValueError:
                filed = None
        lk = re.search(r"<link>(.*?)</link>", item, re.S)
        ent = {"cik": cik.zfill(10), "name": html.unescape(g["companyName"] or "").strip(), "role": "Filer",
               "url": lk.group(1).strip() if lk else None}
        out.append({"accession": acc, "form_type": html.unescape(form).strip(), "accepted_at": ts, "filed_date": filed,
                    "items": [], "entities": [ent], "company": ent, "owner": None})
    out.sort(key=lambda d: d["accepted_at"] or datetime.min.replace(tzinfo=UTC), reverse=True)
    return out


def parse_hdr_items(text: str) -> list[str]:
    """8-K item numbers from an EDGAR SGML header (.hdr.sgml '<ITEMS>2.02' lines)."""
    return sorted(set(_HDR_ITEM.findall(text or "")))


def hdr_url(cik: str, accession: str) -> str:
    return HDR_URL.format(cik=int(cik), acc_nd=accession.replace("-", ""), acc=accession)


def envelope(d: dict[str, Any], link: dict[str, Any] | None) -> Envelope:
    comp = d["company"]
    acc = d["accession"]
    form = d["form_type"]
    payload = {
        "filing_id": f"sec_edgar:{acc}", "source_id": "sec_edgar", "source_entity_id": comp["cik"],
        "accession": acc, "company": comp["name"], "form_type": form, "form_class": form_class(form),
        "is_amendment": form.upper().endswith("/A"), "items": d["items"] or None, "url": comp["url"],
        "knowledge_ts": d["accepted_at"].isoformat().replace("+00:00", "Z") if d["accepted_at"] else None,
        "filed_date": d["filed_date"], "country": "US", "region": "US",
        "reporting_owner": (d["owner"] or {}).get("name"), "reporting_cik": (d["owner"] or {}).get("cik"),
        "issuer_id": (link or {}).get("issuer_id"), "security_id": (link or {}).get("security_id"),
        "ticker": (link or {}).get("ticker"),
    }
    if d.get("items_unresolved"):
        payload["items_unresolved"] = True     # 8-K header never read: items unknown (not "no items")
    return Envelope("raw.filings", f"sec_edgar:{acc}", payload, type="filing", ts_source=d["accepted_at"])


def us_business_hours(now: datetime) -> bool:
    t = now.astimezone(ET)
    return t.weekday() < 5 and 6 <= t.hour < 22


class EdgarStream(StreamWorker):
    name = "edgar"
    topic = "raw.filings"
    source_id = "sec_edgar"
    interval_s = 30.0

    def __init__(self, **params: Any):
        super().__init__(**params)
        self.http: Any = None
        self.universe_only = bool(params.get("universe_only", True))
        self.count = int(params.get("count", 100))
        self.max_pages = int(params.get("max_pages", 5))
        self._known: set[str] = set()
        self._pending: dict[str, tuple[dict[str, Any], Any, float]] = {}   # 8-Ks waiting for their header (XBRL)
        self._newest_ts: datetime | None = None     # newest acceptance time seen in the XBRL feed
        self.overflows = 0                           # polls whose feed did not reach back to the previous poll
        self.items_unresolved = 0                    # 8-Ks emitted without items after PENDING_MAX_AGE_S
        self.legacy = legacy_feed_allowed()
        self.stats.detail = "EDGAR current events Atom" if self.legacy else DETAIL_XBRL

    def interval(self, ctx: StreamContext) -> float:
        if "interval_s" in self.params:
            return float(self.params["interval_s"])
        if not self.legacy:
            return 180.0 if us_business_hours(ctx.clock()) else 600.0
        return 30.0 if us_business_hours(ctx.clock()) else 120.0

    def _client(self) -> Any:
        if self.http is None:
            from app.core.http import HttpClient  # noqa: PLC0415

            self.http = HttpClient("sec_edgar", cache=False, max_retries=2, timeout=20)
        return self.http

    def fetch_page(self, start: int) -> str:
        return self._client().get(FEED, {"action": "getcurrent", "datea": "", "dateb": "", "company": "", "type": "",
                                         "owner": "include", "start": str(start), "count": str(self.count),
                                         "output": "atom"}, cache_ttl=0).text

    def fetch_xbrl(self) -> str:
        return self._client().get(XBRL_FEED, cache_ttl=0).text

    def fetch_hdr(self, cik: str, accession: str) -> str:
        return self._client().get(hdr_url(cik, accession), cache_ttl=0).text

    def add_items(self, rows: list[dict[str, Any]]) -> set[str]:
        """8-K items from the SGML header of each 8-K (XBRL RSS carries none), at most MAX_HDR_PER_POLL headers.
        Returns the accessions whose header was read (items may legitimately be empty); the others (cap reached,
        request failed) keep items == [] and are retried by the caller."""
        n = 0
        ok: set[str] = set()
        for r in rows:
            if base_form(r["form_type"]) != "8-K":
                continue
            if r["items"]:
                ok.add(r["accession"])
                continue
            if n >= MAX_HDR_PER_POLL:
                break
            n += 1
            try:
                r["items"] = parse_hdr_items(self.fetch_hdr(r["company"]["cik"], r["accession"]))
                ok.add(r["accession"])
            except Exception as e:  # noqa: BLE001 - one header must not stop the poll
                log.warning("edgar: header %s failed (retried next poll): %s", r["accession"], e)
        return ok

    def _check_overflow(self, rows: list[dict[str, Any]]) -> None:
        ts = [r["accepted_at"] for r in rows if r["accepted_at"] is not None]
        if not ts:
            return
        oldest, newest = min(ts), max(ts)
        if self._newest_ts is not None and oldest > self._newest_ts:
            self.overflows += 1
            log.warning("edgar: XBRL RSS overflow: the feed starts at %s, the previous poll ended at %s; filings in "
                        "between are not live (nightly SEC jobs)", oldest.isoformat(), self._newest_ts.isoformat())
        self._newest_ts = max(newest, self._newest_ts or newest)

    def _detail(self) -> str:
        extra = [f"{k} {v}" for k, v in (("8-K headers pending", len(self._pending)), ("feed overflows", self.overflows),
                                          ("8-Ks without items", self.items_unresolved)) if v]
        return DETAIL_XBRL + (f"; {', '.join(extra)}" if extra else "")

    def collect(self) -> list[dict[str, Any]]:
        if not self.legacy:   # A9: XBRL RSS (one page of up to 200 filings)
            return parse_xbrl_rss(self.fetch_xbrl())
        out: list[dict[str, Any]] = []
        for page in range(self.max_pages):
            rows = parse_atom(self.fetch_page(page * self.count))
            out += rows
            if not rows or any(r["accession"] in self._known for r in rows) or not self._known:
                break
        return out

    async def poll(self, ctx: StreamContext) -> list[Envelope]:
        rows = await ctx.run_sync(self.collect)
        if not self.legacy:
            self._check_overflow(rows)
        fresh = [r for r in rows if r["accession"] not in self._known and keep_form(r["form_type"])]
        self._known |= {r["accession"] for r in rows}
        if len(self._known) > 20000:
            self._known = {r["accession"] for r in rows} | set(self._pending)
        if not fresh and not self._pending:
            return []
        kept = []
        if fresh:
            links: dict[tuple[str, str], Any] = {}
            if ctx.linker is not None:
                links = await ctx.run_sync(ctx.linker.resolve_many, [("sec_edgar", r["company"]["cik"]) for r in fresh])
            filt = self.universe_only and ctx.linker is not None and ctx.linker.available()
            for r in fresh:
                link = links.get(("sec_edgar", r["company"]["cik"]))
                if filt and not link:
                    continue
                kept.append((r, link))
        if self.legacy:
            return [envelope(r, link) for r, link in kept]
        # XBRL RSS: an 8-K waits (pending, oldest first) until its header gives the items
        now = ctx.clock().timestamp()
        out = [envelope(r, link) for r, link in kept if base_form(r["form_type"]) != "8-K"]
        for r, link in kept:
            if base_form(r["form_type"]) == "8-K":
                self._pending.setdefault(r["accession"], (r, link, now))
        if self._pending:
            done = await ctx.run_sync(self.add_items, [r for r, _, _ in self._pending.values()])
            for acc in list(self._pending):
                r, link, first = self._pending[acc]
                if acc not in done:
                    if now - first < PENDING_MAX_AGE_S:
                        continue
                    r["items_unresolved"] = True
                    self.items_unresolved += 1
                    log.warning("edgar: 8-K %s emitted without items (header unresolved for %.0f s)", acc, now - first)
                del self._pending[acc]
                out.append(envelope(r, link))
        self.stats.detail = self._detail()
        return out
