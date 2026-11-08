"""Official macro release calendars and central-bank meeting dates (statistical offices / central banks only).

No commercial calendars are used. FRED release dates are handled by the caller and are not implemented here.
Each source has a pure parser ``parse_<source>(raw, ...)`` (no network) and a fetcher
``fetch_<source>(start, end, settings)`` that downloads through ``app.core.http.HttpClient`` (RateLimit 0.5/s,
6h cache) and keeps entries whose local date falls in ``[start, end]``. ``fetch_all`` runs every fetcher, never
raises, and returns ``(entries, report)``.

Time rule: when the source publishes a release time it is used (``time_known=True``); otherwise the documented
typical time of that publisher is applied and ``time_known=False``. ``scheduled_local`` is always tz-aware in the
publisher's timezone.

Sources (live verification on 2026-10-12; all passed unless stated):

bls_cal       BLS        https://www.bls.gov/schedule/news_release/bls.ics
              ICS, DTSTART;TZID=US-Eastern -> America/New_York, time from source. History: current + previous
              year (2025-2026 on 2026-10-12). Requires a descriptive UA with contact (browser-like or curl UAs get 403).
bea_cal       BEA        https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics
              ICS, DTSTART in UTC -> America/New_York, time from source. 2025 .. 2027.
fomc          Federal Reserve  https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
              HTML; decision day = last day of the meeting, 14:00 America/New_York (time_known=False);
              notation votes skipped. 2021 .. next year.
eurostat_cal  Eurostat   https://ec.europa.eu/eurostat/o/calendars/eventsJson?isEuroindicator=true&start=..&end=..
              JSON (the release-calendar page's FullCalendar feed), euro indicators only; events are all-day so
              11:00 Europe/Luxembourg with time_known=False. History from 2025-01 to end of the current year.
ecb_gc        ECB        future: https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html (HTML dl)
                         past:   https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html
              Decision day (Day 2 / single monetary-policy meeting; past = "Monetary policy decisions" press
              releases) at 14:15 Europe/Berlin (Frankfurt), time_known=False. History: 1999+ via yearly includes.
ecb_statcal   ECB        https://www.ecb.europa.eu/press/calendars/statscal/html/index.en.html
              HTML dl with "dd/mm/yyyy HH:MM" (CET, Europe/Berlin), time from source. ~1 month back .. ~10 months ahead.
ons_cal       ONS        https://api.beta.ons.gov.uk/v1/search/releases (release-type=type-upcoming|type-published,
              fromDate/toDate, limit/offset). JSON, release_date in UTC -> Europe/London; time_known only when the
              date is confirmed (finalised). History: full archive. Cancelled releases dropped.
boe_mpc       Bank of England  https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates
              HTML tables per year; announcement 12:00 Europe/London (time_known=False). Current + next year only.
boj_mpm       Bank of Japan    https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm (current/next year) and
                               https://www.boj.or.jp/en/mopo/mpmsche_minu/past.htm (2014+).
              HTML; decision day = last meeting day; no fixed announcement time -> 12:00 Asia/Tokyo, time_known=False.
statcan_cal   Statistics Canada  https://www150.statcan.gc.ca/n1/dai-quo/ssi/homepage/schedule-key_indicators-eng.json
              JSON (major economic releases, the feed behind "Release schedule - Major economic releases").
              The Daily: 08:30 America/Toronto (time_known=False). History: 2012 .. ~+6 months.
boc_rate      Bank of Canada  https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/
              HTML "Schedule for YYYY" tables; 09:45 America/Toronto (time_known=False). Current + next year only.
abs_cal       ABS        https://www.abs.gov.au/release-calendar/future-releases/{YYYYMM}/all
              HTML with <time datetime=UTC> -> Australia/Sydney (Canberra time), time from source.
              Future only (current month + 6 months).
rba_board     RBA        https://www.rba.gov.au/schedules-events/board-meeting-schedules.html (current + next year,
                         Monetary Policy Board column, decision = last day) and
                         https://www.rba.gov.au/media-releases/{year}/ ("... Monetary Policy Decision" releases, past).
              14:30 Australia/Sydney (time_known=False). Needs a non-browser UA (browser UAs get 403 from Akamai).
ibge_cal      IBGE       https://servicodados.ibge.gov.br/api/v3/calendario/?de=MM-DD-YYYY&ate=MM-DD-YYYY&qtd=..&page=..
              JSON; data_divulgacao is UTC ("12:00" = 09:00 Brasilia) -> America/Sao_Paulo, time from source.
              Full history.
bcb_copom     BCB        past:   https://www.bcb.gov.br/api/servico/sitebcb/copom/comunicados?quantidade=N
                         future: https://www.bcb.gov.br/api/servico/sitebcb/calendario/anual?lista=Reuniões do Copom
                                 &inicioAgenda='YYYY-MM-DD'&fimAgenda='YYYY-MM-DD'
              JSON; decision = last meeting day, 18:30 America/Sao_Paulo (time_known=False). 2021+ / next year.
insee_cal     INSEE      POST https://www.insee.fr/en/agenda-diffusion?q=*  (JSON body; the "Agenda de diffusion des
              indicateurs economiques" search API). embargo in UTC -> Europe/Paris, time from source.
              Upcoming only (~3 months).
destatis_cal  Destatis   https://www.destatis.de/SiteGlobals/Forms/Suche/Termine/EN/Terminsuche_Formular.html
              (paged HTML, ?gtp=1362242_list%3D{n}). Date only -> 08:00 Europe/Berlin (time_known=False).
              Upcoming only (~2 months).
istat_cal     ISTAT      https://calendar.google.com/calendar/ical/t49o0tm9m8f5c72l2gamob8djs%40group.calendar.google.com/public/basic.ics
              ICS published by ISTAT (linked as "Press releases ICal" from istat.it press-calendar page; hosted on
              Google Calendar). DTSTART UTC -> Europe/Rome, time from source. History: 2015 .. end of current year.
              DISABLED 2026-11-07 (A9): calendar.google.com robots.txt disallows it -> skipped (source_disallowed).
ine_cal       INE        https://www.ine.es/dynt3/Calendario/en/calendario.ics
              ICS, DTSTART;TZID=Europe/Madrid, time from source. Previous + current year (~1.6 MB).

Not implemented (optional, skipped): BoJ statistics release schedule (no machine-readable feed found;
/en/statistics/outline/release 404), MoF/e-Stat, Statistics Korea/BoK, SNB, Banxico.
"""

from __future__ import annotations

import html as _html
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import orjson

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

CACHE_TTL = 6 * 3600
UTC = timezone.utc

TZ_NY = "America/New_York"
TZ_LUX = "Europe/Luxembourg"
TZ_FRA = "Europe/Berlin"
TZ_LON = "Europe/London"
TZ_TOK = "Asia/Tokyo"
TZ_TOR = "America/Toronto"
TZ_SYD = "Australia/Sydney"
TZ_SAO = "America/Sao_Paulo"
TZ_PAR = "Europe/Paris"
TZ_ROM = "Europe/Rome"
TZ_MAD = "Europe/Madrid"

_TZ_ALIASES = {"US-Eastern": TZ_NY, "US/Eastern": TZ_NY, "Eastern Standard Time": TZ_NY}

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
_MON_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
             "oct": 10, "nov": 11, "dec": 12}


def _month(tok: str) -> int | None:
    t = tok.strip().strip(".").lower()
    return MONTHS.get(t) or _MON_ABBR.get(t) or _MON_ABBR.get(t[:3])


@dataclass(frozen=True)
class CalEntry:
    source_id: str            # 'bls_cal','bea_cal','fomc',...
    native_key: str           # stable unique key within the source
    name: str                 # release / event title as published
    country: str              # ISO2, or 'EA' for euro area aggregates
    scheduled_local: datetime  # tz-aware datetime in the publisher's timezone
    tz: str                   # IANA tz
    time_known: bool          # True only if the time came from the source itself
    kind: str                 # 'data' | 'policy_meeting'
    publisher: str
    url: str | None = None
    reference_period: str | None = None


class SkipSource(Exception):
    """Raised by a fetcher when the requested window is outside what the source can serve."""


# --------------------------------------------------------------------------------------------- helpers

def _text(raw: str | bytes) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8-sig", errors="replace")
    return raw.lstrip("﻿")


def _strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _at(d: date, hh: int, mm: int, tz: str) -> datetime:
    return datetime.combine(d, dtime(hh, mm), tzinfo=ZoneInfo(tz))


def _utc_to(dt_utc: datetime, tz: str) -> datetime:
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(ZoneInfo(tz))


def _in_window(entries: Iterable[CalEntry], start: date, end: date) -> list[CalEntry]:
    out = [e for e in entries if start <= e.scheduled_local.date() <= end]
    out.sort(key=lambda e: (e.scheduled_local, e.name))
    return out


def _dedupe(entries: Iterable[CalEntry]) -> list[CalEntry]:
    seen: dict[str, CalEntry] = {}
    for e in entries:
        seen.setdefault(e.native_key, e)
    return list(seen.values())


def _contact(settings: Settings) -> str:
    try:
        return settings.sec_user_agent.split()[-1]
    except Exception:  # noqa: BLE001
        return "admin@example.com"


def _client(source_id: str, settings: Settings, headers: dict[str, str] | None = None):
    from app.core.http import HttpClient
    from app.core.ratelimit import RateLimit

    # Descriptive, non-browser UA with contact: BLS and RBA reject browser-like UAs, accept this one.
    h = {"User-Agent": f"BiG Equity 360 research {_contact(settings)}", "Accept": "*/*"}
    h.update(headers or {})
    return HttpClient(source_id, settings=settings, rate_limit=RateLimit(0.5, 1), headers=h, max_retries=2)


def _get(source_id: str, settings: Settings, url: str, params: dict[str, Any] | None = None,
         headers: dict[str, str] | None = None) -> bytes:
    with _client(source_id, settings, headers) as http:
        return http.get(url, params=params, cache_ttl=CACHE_TTL).content


# --------------------------------------------------------------------------------------------- ICS

def _ics_events(raw: str | bytes) -> list[dict[str, tuple[dict[str, str], str]]]:
    """Minimal RFC5545 reader: unfolds lines and returns VEVENTs as {NAME: (params, value)}."""
    text = _text(raw).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n[ \t]", "", text)  # unfold
    events: list[dict[str, tuple[dict[str, str], str]]] = []
    cur: dict[str, tuple[dict[str, str], str]] | None = None
    for line in text.split("\n"):
        if line == "BEGIN:VEVENT":
            cur = {}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        parts = head.split(";")
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v.strip('"')
        cur.setdefault(parts[0].upper(), (params, value))
    return events


def _ics_unescape(s: str) -> str:
    return s.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _ics_dt(prop: tuple[dict[str, str], str], default_tz: str) -> tuple[datetime, bool]:
    """Returns (aware datetime in default_tz, has_time)."""
    params, value = prop
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(d, dtime(0, 0), tzinfo=ZoneInfo(default_tz)), False
    fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
    naive = datetime.strptime(value.rstrip("Z"), fmt)
    if value.endswith("Z"):
        return naive.replace(tzinfo=UTC).astimezone(ZoneInfo(default_tz)), True
    tzid = params.get("TZID")
    tz = _TZ_ALIASES.get(tzid or "", tzid or default_tz)
    try:
        zi = ZoneInfo(tz)
    except Exception:  # noqa: BLE001
        zi = ZoneInfo(default_tz)
    return naive.replace(tzinfo=zi).astimezone(ZoneInfo(default_tz)), True


_REF_RE = re.compile(
    r"(?:(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"(?:\s+and\s+(?:Annual\s+)?)?(?:\s+\d{4})?"
    r"|(?:1st|2nd|3rd|4th|First|Second|Third|Fourth)\s+Quarter(?:\s+and\s+Year)?\s+\d{4}"
    r"|Q[1-4]\s+\d{4}|\d{4}\s*Q[1-4]|Year\s+\d{4})", re.I)


def _ref_from_title(title: str) -> str | None:
    """Reference period from 'Name, Period' style titles (BEA)."""
    if "," not in title:
        return None
    tail = title.rsplit(",", 1)[1].strip()
    tail = re.sub(r"\s*\(.*?\)\s*$", "", tail).strip()
    return tail if _REF_RE.search(tail) and re.search(r"\d{4}", tail) else None


# --------------------------------------------------------------------------------------------- US

BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_URL = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"


def parse_bls(raw: str | bytes) -> list[CalEntry]:
    out = []
    for ev in _ics_events(raw):
        if "DTSTART" not in ev or "SUMMARY" not in ev:
            continue
        dt, has_time = _ics_dt(ev["DTSTART"], TZ_NY)
        if not has_time:
            dt = _at(dt.date(), 8, 30, TZ_NY)
        name = _ics_unescape(ev["SUMMARY"][1])
        uid = ev.get("UID", ({}, ""))[1] or f"{name}|{dt.date()}"
        out.append(CalEntry("bls_cal", uid, name, "US", dt, TZ_NY, has_time, "data", "BLS",
                            "https://www.bls.gov/schedule/news_release/"))
    return _dedupe(out)


def fetch_bls(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_bls(_get("bls_cal", settings, BLS_URL)), start, end)


def parse_bea(raw: str | bytes) -> list[CalEntry]:
    out = []
    for ev in _ics_events(raw):
        if "DTSTART" not in ev or "SUMMARY" not in ev:
            continue
        dt, has_time = _ics_dt(ev["DTSTART"], TZ_NY)
        if not has_time:
            dt = _at(dt.date(), 8, 30, TZ_NY)
        name = _ics_unescape(ev["SUMMARY"][1])
        uid = ev.get("UID", ({}, ""))[1] or f"{name}|{dt.date()}"
        out.append(CalEntry("bea_cal", uid, name, "US", dt, TZ_NY, has_time, "data", "BEA",
                            "https://www.bea.gov/news/schedule", _ref_from_title(name)))
    return _dedupe(out)


def fetch_bea(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_bea(_get("bea_cal", settings, BEA_URL)), start, end)


def parse_fomc(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r"<h4><a id=\"\d+\">(\d{4}) FOMC Meetings", t)]
    heads.sort()
    out = []
    for i, (pos, year) in enumerate(heads):
        sec = t[pos: heads[i + 1][0] if i + 1 < len(heads) else len(t)]
        for m in re.finditer(r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<",
                             sec, re.S):
            mon_txt, day_txt = m.group(1).strip(), m.group(2).strip()
            dm = re.fullmatch(r"(\d{1,2})(?:-(\d{1,2}))?(\*)?", day_txt)
            if not dm:  # notation votes / unscheduled entries
                continue
            months = [_month(x) for x in mon_txt.split("/")]
            if not months or any(x is None for x in months):
                continue
            d1, d2 = int(dm.group(1)), int(dm.group(2) or dm.group(1))
            # 'Jan/Feb' + '31-1' -> Feb 1; plain month -> that month
            if len(months) > 1 and (dm.group(2) is None or d2 < d1):
                mon = months[-1]
            else:
                mon = months[0]
            dec = date(year, mon, d2)
            sep = bool(dm.group(3))
            name = "FOMC meeting (rate decision)" + (" + SEP" if sep else "")
            out.append(CalEntry("fomc", f"fomc|{dec.isoformat()}", name, "US", _at(dec, 14, 0, TZ_NY), TZ_NY, False,
                                "policy_meeting", "Federal Reserve", FOMC_URL,
                                f"{mon_txt} {day_txt.rstrip('*')}, {year}"))
    return _dedupe(out)


def fetch_fomc(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_fomc(_get("fomc", settings, FOMC_URL)), start, end)


# --------------------------------------------------------------------------------------------- Euro area

EUROSTAT_URL = "https://ec.europa.eu/eurostat/o/calendars/eventsJson"
EUROSTAT_PAGE = "https://ec.europa.eu/eurostat/news/release-calendar"


def parse_eurostat(raw: str | bytes, euro_indicators_only: bool = True) -> list[CalEntry]:
    data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode())
    out = []
    for x in data:
        if euro_indicators_only and not x.get("euroind"):
            continue
        s = str(x.get("start") or "")
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
        if not m:
            continue
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        # Feed marks events allDay with a nominal 'T11:00Z'; euro indicators are released at 11:00 CET/CEST.
        dt = _at(d, 11, 0, TZ_LUX)
        name = str(x.get("title") or "").strip()
        key = f"{x.get('recordid') or name}|{d.isoformat()}"
        out.append(CalEntry("eurostat_cal", key, name, "EA", dt, TZ_LUX, False, "data", "Eurostat", EUROSTAT_PAGE,
                            (str(x.get("period")) or None) if x.get("period") else None))
    return _dedupe(out)


def fetch_eurostat(start: date, end: date, settings: Settings) -> list[CalEntry]:
    out: list[CalEntry] = []
    with _client("eurostat_cal", settings) as http:
        y = start.year
        while y <= end.year:
            p = {"theme": 0, "category": 0, "keywords": "", "isEuroindicator": "true", "authorInclude": "",
                 "authorExclude": "", "start": f"{max(start, date(y, 1, 1)).isoformat()}T00:00:00+01:00",
                 "end": f"{min(end, date(y, 12, 31)).isoformat()}T23:59:59+01:00", "timeZone": TZ_LUX}
            r = http.get(EUROSTAT_URL, params=p, cache_ttl=CACHE_TTL)
            if r.content.strip():
                out.extend(parse_eurostat(r.content))
            y += 1
    return _in_window(_dedupe(out), start, end)


ECB_GC_URL = "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html"
ECB_MOPO_URL = "https://www.ecb.europa.eu/press/govcdec/mopo/{year}/html/index_include.en.html"
ECB_STAT_URL = "https://www.ecb.europa.eu/press/calendars/statscal/html/index.en.html"


def _ecb_gc_entry(d: date, name: str, url: str) -> CalEntry:
    return CalEntry("ecb_gc", f"ecb_gc|{d.isoformat()}", name, "EA", _at(d, 14, 15, TZ_FRA), TZ_FRA, False,
                    "policy_meeting", "ECB", url)


def parse_ecb_gc(raw: str | bytes) -> list[CalEntry]:
    """Governing Council schedule page: keep the decision day of monetary policy meetings."""
    t = _text(raw)
    out = []
    for m in re.finditer(r"<dt>\s*(\d{2})/(\d{2})/(\d{4})\s*</dt>\s*<dd>(.*?)</dd>", t, re.S):
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        txt = _strip_tags(m.group(4))
        low = txt.lower()
        if "monetary policy meeting" not in low or "non-monetary" in low or "day 1" in low:
            continue
        out.append(_ecb_gc_entry(d, "ECB Governing Council monetary policy meeting (decision)", ECB_GC_URL))
    return _dedupe(out)


def parse_ecb_mopo(raw: str | bytes) -> list[CalEntry]:
    """Yearly include of 'Monetary policy decisions' press releases (past decision days)."""
    t = _text(raw)
    out = []
    for m in re.finditer(r'<dt isoDate="(\d{4})-(\d{2})-(\d{2})">(.*?)</dt>\s*<dd>(.*?)</dd>', t, re.S):
        title = _strip_tags(m.group(5)[:600])
        if "monetary policy decision" not in title.lower():
            continue
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        href = re.search(r'href="([^"]+\.en\.html)"', m.group(5))
        url = ("https://www.ecb.europa.eu" + href.group(1)) if href else ECB_GC_URL
        out.append(_ecb_gc_entry(d, "ECB Governing Council monetary policy meeting (decision)", url))
    return _dedupe(out)


def fetch_ecb_gc(start: date, end: date, settings: Settings) -> list[CalEntry]:
    out: list[CalEntry] = []
    today = date.today()
    with _client("ecb_gc", settings) as http:
        if end >= today - timedelta(days=60):
            out.extend(parse_ecb_gc(http.get(ECB_GC_URL, cache_ttl=CACHE_TTL).content))
        for y in range(start.year, min(end.year, today.year) + 1):
            try:
                out.extend(parse_ecb_mopo(http.get(ECB_MOPO_URL.format(year=y), cache_ttl=CACHE_TTL).content))
            except Exception as e:  # noqa: BLE001
                log.warning("ecb_gc: mopo %s failed: %s", y, e)
    return _in_window(_dedupe(out), start, end)


def parse_ecb_statcal(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    out = []
    for m in re.finditer(r"<dt>\s*(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?\s*</dt>\s*<dd>(.*?)</dd>", t, re.S):
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        has_time = m.group(4) is not None
        dt = _at(d, int(m.group(4)), int(m.group(5)), TZ_FRA) if has_time else _at(d, 10, 0, TZ_FRA)
        lines = [_strip_tags(x) for x in re.split(r"<br\s*/?>", m.group(6))]
        lines = [x for x in lines if x]
        if not lines:
            continue
        first = lines[0]
        ds = re.search(r"\(Dataset:\s*([^)]+)\)", first)
        name = re.sub(r"\s*\(Dataset:[^)]*\)", "", first).strip()
        ref = next((x.split(":", 1)[1].strip() for x in lines if x.lower().startswith("reference period")), None)
        key = f"{name}|{ds.group(1).strip() if ds else ''}|{ref or ''}|{d.isoformat()}"
        out.append(CalEntry("ecb_statcal", key, name, "EA", dt, TZ_FRA, has_time, "data", "ECB", ECB_STAT_URL, ref))
    return _dedupe(out)


def fetch_ecb_statcal(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_ecb_statcal(_get("ecb_statcal", settings, ECB_STAT_URL)), start, end)


# --------------------------------------------------------------------------------------------- UK

ONS_URL = "https://api.beta.ons.gov.uk/v1/search/releases"
BOE_URL = "https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates"


def parse_ons(raw: str | bytes) -> list[CalEntry]:
    data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode())
    out = []
    for r in data.get("releases") or []:
        desc = r.get("description") or {}
        if desc.get("cancelled"):
            continue
        rd = desc.get("release_date")
        if not rd:
            continue
        dt = _utc_to(datetime.fromisoformat(rd.replace("Z", "+00:00")), TZ_LON)
        title = str(desc.get("title") or "").strip()
        ref = None
        if ":" in title:
            tail = title.rsplit(":", 1)[1].strip()
            if re.search(r"\b(19|20)\d{2}\b", tail):
                ref = tail
        uri = r.get("uri") or ""
        out.append(CalEntry("ons_cal", uri or f"{title}|{dt.date()}", title, "GB", dt, TZ_LON,
                            bool(desc.get("finalised")), "data", "ONS",
                            ("https://www.ons.gov.uk" + uri) if uri else None, ref))
    return _dedupe(out)


def fetch_ons(start: date, end: date, settings: Settings) -> list[CalEntry]:
    out: list[CalEntry] = []
    today = date.today()
    spans = []
    if start < today:
        spans.append(("type-published", start, min(end, today)))
    if end >= today - timedelta(days=1):
        spans.append(("type-upcoming", max(start, today - timedelta(days=1)), end))
    with _client("ons_cal", settings) as http:
        for rtype, a, b in spans:
            offset, limit = 0, 1000
            while True:
                p = {"release-type": rtype, "fromDate": a.isoformat(), "toDate": b.isoformat(), "limit": limit,
                     "offset": offset, "sort": "release_date_asc"}
                r = http.get(ONS_URL, params=p, cache_ttl=CACHE_TTL)
                page = parse_ons(r.content)
                n = len(orjson.loads(r.content).get("releases") or [])
                out.extend(page)
                if n < limit or offset > 20000:
                    break
                offset += limit
    return _in_window(_dedupe(out), start, end)


def parse_boe(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    heads = [(m.start(), int(m.group(1)), m.group(2).strip().lower())
             for m in re.finditer(r"<h2>\s*(\d{4})\s+([a-z ]*?)dates\s*</h2>", t, re.I)]
    out = []
    for i, (pos, year, status) in enumerate(heads):
        sec = t[pos: heads[i + 1][0] if i + 1 < len(heads) else pos + 20000]
        sec = sec.split("</table>", 1)[0]
        for row in re.finditer(r"<tr>\s*<td>(.*?)</td>", sec, re.S):
            cell = _strip_tags(row.group(1))
            m = re.search(r"(\d{1,2})\s+([A-Za-z]+)", cell)
            if not m or not _month(m.group(2)):
                continue
            d = date(year, _month(m.group(2)), int(m.group(1)))
            name = "Bank of England MPC announcement" + ("" if "confirmed" in status or not status else f" ({status})")
            out.append(CalEntry("boe_mpc", f"boe_mpc|{d.isoformat()}", name, "GB", _at(d, 12, 0, TZ_LON), TZ_LON,
                                False, "policy_meeting", "Bank of England", BOE_URL))
    return _dedupe(out)


def fetch_boe(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_boe(_get("boe_mpc", settings, BOE_URL)), start, end)


# --------------------------------------------------------------------------------------------- Japan

BOJ_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
BOJ_PAST_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/past.htm"


def _boj_last_day(cell: str, year: int) -> date | None:
    """'Apr. 30 (Wed.), May 1 (Thurs.)' -> May 1; 'Jan. 22 (Thurs.), 23 (Fri.)' -> Jan 23."""
    cur_m: int | None = None
    last: tuple[int, int] | None = None
    for m in re.finditer(r"(?:([A-Z][a-z]+)\.?\s+)?(\d{1,2})\s*\(", cell):
        if m.group(1):
            mm = _month(m.group(1))
            if mm:
                cur_m = mm
        if cur_m:
            last = (cur_m, int(m.group(2)))
    if not last:
        return None
    try:
        return date(year, last[0], last[1])
    except ValueError:
        return None


def parse_boj(raw: str | bytes, url: str = BOJ_URL) -> list[CalEntry]:
    t = _text(raw)
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r'<h2 id="[^"]*">\s*(\d{4})\s*</h2>', t)]
    out = []
    for i, (pos, year) in enumerate(heads):
        sec = t[pos: heads[i + 1][0] if i + 1 < len(heads) else len(t)]
        sec = sec.split("</table>", 1)[0]
        for row in re.finditer(r"<tr>\s*<td[^>]*>(.*?)</td>", sec, re.S):
            cell = _strip_tags(row.group(1))
            d = _boj_last_day(cell, year)
            if not d:
                continue
            cell_clean = re.sub(r"\s*\[PDF[^\]]*\]", "", cell)
            out.append(CalEntry("boj_mpm", f"boj_mpm|{d.isoformat()}", "BoJ Monetary Policy Meeting (decision)", "JP",
                                _at(d, 12, 0, TZ_TOK), TZ_TOK, False, "policy_meeting", "Bank of Japan", url,
                                f"{cell_clean}, {year}"))
    return _dedupe(out)


def fetch_boj(start: date, end: date, settings: Settings) -> list[CalEntry]:
    out: list[CalEntry] = []
    today = date.today()
    with _client("boj_mpm", settings) as http:
        out.extend(parse_boj(http.get(BOJ_URL, cache_ttl=CACHE_TTL).content, BOJ_URL))
        if start.year < today.year:
            out.extend(parse_boj(http.get(BOJ_PAST_URL, cache_ttl=CACHE_TTL).content, BOJ_PAST_URL))
    return _in_window(_dedupe(out), start, end)


# --------------------------------------------------------------------------------------------- Canada

STATCAN_URL = "https://www150.statcan.gc.ca/n1/dai-quo/ssi/homepage/schedule-key_indicators-eng.json"
BOC_URL = "https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/"


def parse_statcan(raw: str | bytes) -> list[CalEntry]:
    data = orjson.loads(_text(raw).encode())
    out = []
    for x in data:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(x.get("date") or ""))
        if not m:
            continue
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        name = _html.unescape(str(x.get("title") or "")).strip()
        ref = _html.unescape(str(x.get("description") or "")).strip() or None
        u = x.get("url")
        url = ("https://www150.statcan.gc.ca/n1" + u) if u and u.startswith("/") else (u or None)
        out.append(CalEntry("statcan_cal", f"{name}|{d.isoformat()}", name, "CA", _at(d, 8, 30, TZ_TOR), TZ_TOR,
                            False, "data", "Statistics Canada", url, ref))
    return _dedupe(out)


def fetch_statcan(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_statcan(_get("statcan_cal", settings, STATCAN_URL)), start, end)


def parse_boc(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    out = []
    for cap in re.finditer(r"Schedule for\s+(\d{4})", t):
        year = int(cap.group(1))
        sec = t[cap.end(): cap.end() + 20000].split("</table>", 1)[0]
        for row in re.finditer(r"<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>", sec, re.S):
            cell, pubs = _strip_tags(row.group(1)), _strip_tags(row.group(2))
            m = re.match(r"([A-Za-z]+)\s+(\d{1,2})", cell)
            if not m or not _month(m.group(1)) or "interest rate" not in pubs.lower():
                continue
            d = date(year, _month(m.group(1)), int(m.group(2)))
            href = re.search(r'href="([^"]+)"', row.group(2))
            out.append(CalEntry("boc_rate", f"boc_rate|{d.isoformat()}", "Bank of Canada interest rate announcement",
                                "CA", _at(d, 9, 45, TZ_TOR), TZ_TOR, False, "policy_meeting", "Bank of Canada",
                                href.group(1) if href else BOC_URL,
                                "with Monetary Policy Report" if "monetary policy report" in pubs.lower() else None))
    return _dedupe(out)


def fetch_boc(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_boc(_get("boc_rate", settings, BOC_URL)), start, end)


# --------------------------------------------------------------------------------------------- Australia

ABS_URL = "https://www.abs.gov.au/release-calendar/future-releases/{ym}/all"
RBA_URL = "https://www.rba.gov.au/schedules-events/board-meeting-schedules.html"
RBA_MR_URL = "https://www.rba.gov.au/media-releases/{year}/"


def parse_abs(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    out = []
    blocks = re.split(r'<div class="exportable-element">', t)[1:]
    for b in blocks:
        tm = re.search(r'<time datetime="([^"]+)"', b)
        nm = re.search(r'event-name">(.*?)</h3>', b, re.S)
        if not tm or not nm:
            continue
        dt = _utc_to(datetime.fromisoformat(tm.group(1).replace("Z", "+00:00")), TZ_SYD)
        name = _strip_tags(nm.group(1))
        rp = re.search(r'reference-period-value">(.*?)</span>', b, re.S)
        ref = _strip_tags(rp.group(1)) if rp else None
        out.append(CalEntry("abs_cal", f"{name}|{ref or ''}|{dt.date().isoformat()}", name, "AU", dt, TZ_SYD, True,
                            "data", "ABS", "https://www.abs.gov.au/release-calendar/future-releases", ref))
    return _dedupe(out)


def fetch_abs(start: date, end: date, settings: Settings) -> list[CalEntry]:
    today = date.today()
    first = max(date(start.year, start.month, 1), date(today.year, today.month, 1))
    if end < date(today.year, today.month, 1):
        raise SkipSource("ABS release calendar only lists the current month and ~6 months ahead")
    out: list[CalEntry] = []
    with _client("abs_cal", settings) as http:
        y, m = first.year, first.month
        n = 0
        while date(y, m, 1) <= end and n < 8:
            r = http.get(ABS_URL.format(ym=f"{y}{m:02d}"), cache_ttl=CACHE_TTL, raise_for_status=False)
            if r.status_code == 200:
                page = parse_abs(r.content)
                if not page and n > 0:
                    break
                out.extend(page)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
            n += 1
    return _in_window(_dedupe(out), start, end)


def _rba_entry(d: date, url: str) -> CalEntry:
    return CalEntry("rba_board", f"rba_board|{d.isoformat()}", "RBA Monetary Policy Board decision", "AU",
                    _at(d, 14, 30, TZ_SYD), TZ_SYD, False, "policy_meeting", "RBA", url)


def parse_rba(raw: str | bytes) -> list[CalEntry]:
    """Board meeting schedule page (Monetary Policy Board column, decision = last meeting day)."""
    t = _text(raw)
    out = []
    for tbl in re.finditer(r"<table.*?</table>", t, re.S):
        tb = tbl.group(0)
        cap = re.search(r"<caption[^>]*>.*?(\d{4}).*?</caption>", tb, re.S)
        if not cap:
            continue
        year = int(cap.group(1))
        for row in re.finditer(r"<tr>\s*<th[^>]*scope=\"row\"[^>]*>.*?</th>\s*<td>(.*?)</td>", tb, re.S):
            cell = _strip_tags(row.group(1))
            m = re.search(r"(\d{1,2})\s*(?:[^\d\w]+\s*(\d{1,2}))?\s+([A-Za-z]+)\s*$", cell)
            if not m or not _month(m.group(3)):
                continue
            # '30 March - 1 April' style: take last day/month pair
            pairs = re.findall(r"(\d{1,2})\s+([A-Za-z]+)", cell)
            day, mon = int(m.group(2) or m.group(1)), _month(m.group(3))
            if pairs:
                day, mon = int(pairs[-1][0]), _month(pairs[-1][1]) or mon
            out.append(_rba_entry(date(year, mon, day), RBA_URL))
    return _dedupe(out)


def parse_rba_media(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    out = []
    for art in re.finditer(r"<article.*?</article>", t, re.S):
        a = art.group(0)
        hl = re.search(r'itemprop="headline">(.*?)</span>', a, re.S)
        tm = re.search(r'<time datetime="(\d{4})-(\d{2})-(\d{2})', a)
        if not hl or not tm or "monetary policy decision" not in _strip_tags(hl.group(1)).lower():
            continue
        href = re.search(r'href="([^"]+)"', a)
        url = ("https://www.rba.gov.au" + href.group(1)) if href and href.group(1).startswith("/") else RBA_URL
        out.append(_rba_entry(date(int(tm.group(1)), int(tm.group(2)), int(tm.group(3))), url))
    return _dedupe(out)


def fetch_rba(start: date, end: date, settings: Settings) -> list[CalEntry]:
    today = date.today()
    out: list[CalEntry] = []
    with _client("rba_board", settings) as http:
        out.extend(parse_rba(http.get(RBA_URL, cache_ttl=CACHE_TTL).content))
        for y in range(start.year, min(end.year, today.year) + 1):
            try:
                out.extend(parse_rba_media(http.get(RBA_MR_URL.format(year=y), cache_ttl=CACHE_TTL).content))
            except Exception as e:  # noqa: BLE001
                log.warning("rba_board: media releases %s failed: %s", y, e)
    return _in_window(_dedupe(out), start, end)


# --------------------------------------------------------------------------------------------- Brazil

IBGE_URL = "https://servicodados.ibge.gov.br/api/v3/calendario/"
BCB_COMUNICADOS_URL = "https://www.bcb.gov.br/api/servico/sitebcb/copom/comunicados"
BCB_CAL_URL = "https://www.bcb.gov.br/api/servico/sitebcb/calendario/anual"


def parse_ibge(raw: str | bytes) -> list[CalEntry]:
    data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode())
    out = []
    for it in data.get("items") or []:
        s = str(it.get("data_divulgacao") or "")
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})(?:\s+(\d{2}):(\d{2}))?", s)
        if not m:
            continue
        d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if m.group(4):
            # API times are UTC: IPCA etc. show 12:00 = 09:00 Brasilia (official release hour).
            dt = _utc_to(datetime.combine(d, dtime(int(m.group(4)), int(m.group(5))), tzinfo=UTC), TZ_SAO)
            known = True
        else:
            dt, known = _at(d, 9, 0, TZ_SAO), False
        name = str(it.get("titulo") or "").strip()
        alias = it.get("alias_produto")
        url = f"https://www.ibge.gov.br/estatisticas/todos-os-produtos-estatisticas.html?q={alias}" if alias else None
        out.append(CalEntry("ibge_cal", str(it.get("id") or f"{name}|{d.isoformat()}"), name, "BR", dt, TZ_SAO, known,
                            "data", "IBGE", url, None))
    return _dedupe(out)


def fetch_ibge(start: date, end: date, settings: Settings) -> list[CalEntry]:
    out: list[CalEntry] = []
    with _client("ibge_cal", settings) as http:
        a = start
        while a <= end:
            b = min(end, date(a.year, 12, 31))
            page = 1
            while True:
                p = {"de": a.strftime("%m-%d-%Y"), "ate": b.strftime("%m-%d-%Y"), "qtd": 500, "page": page}
                r = http.get(IBGE_URL, params=p, cache_ttl=CACHE_TTL)
                out.extend(parse_ibge(r.content))
                meta = orjson.loads(r.content)
                if not meta.get("nextPage") or page >= int(meta.get("totalPages") or 1) or page > 50:
                    break
                page += 1
            a = b + timedelta(days=1)
    return _in_window(_dedupe(out), start, end)


def _copom_entry(d: date, name: str, ref: str | None) -> CalEntry:
    return CalEntry("bcb_copom", f"bcb_copom|{d.isoformat()}", name, "BR", _at(d, 18, 30, TZ_SAO), TZ_SAO, False,
                    "policy_meeting", "BCB", "https://www.bcb.gov.br/controleinflacao/copom", ref)


def parse_bcb_comunicados(raw: str | bytes) -> list[CalEntry]:
    data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode())
    out = []
    for c in data.get("conteudo") or []:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(c.get("dataReferencia") or ""))
        if not m:
            continue
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        n = c.get("nro_reuniao") or c.get("nroReuniao")
        out.append(_copom_entry(d, f"Copom meeting {n} (Selic decision)" if n else "Copom meeting (Selic decision)",
                                str(c.get("titulo") or "").strip() or None))
    return _dedupe(out)


def parse_bcb_calendar(raw: str | bytes) -> list[CalEntry]:
    """Calendar lists each meeting day; group consecutive days, decision = last day (two-day meetings)."""
    data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode())
    days = []
    for c in data.get("conteudo") or []:
        if "copom" not in str(c.get("evento") or "").lower():
            continue
        s = str(c.get("dataEvento") or "")
        try:
            dtu = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            continue
        days.append(dtu.astimezone(ZoneInfo(TZ_SAO)).date())
    days = sorted(set(days))
    out, group = [], []
    for d in days + [None]:  # type: ignore[list-item]
        if group and (d is None or (d - group[-1]).days > 1):
            last = group[-1] if len(group) > 1 else group[0] + timedelta(days=1)  # 1-day listing -> day 2 = decision
            out.append(_copom_entry(last, "Copom meeting (Selic decision)",
                                    f"{group[0].isoformat()}..{last.isoformat()}"))
            group = []
        if d is not None:
            group.append(d)
    return _dedupe(out)


def fetch_bcb(start: date, end: date, settings: Settings) -> list[CalEntry]:
    today = date.today()
    out: list[CalEntry] = []
    with _client("bcb_copom", settings, {"Accept": "application/json"}) as http:
        if start <= today:
            n = max(10, int((today - start).days / 45) + 10)
            r = http.get(BCB_COMUNICADOS_URL, params={"quantidade": n}, cache_ttl=CACHE_TTL)
            out.extend(parse_bcb_comunicados(r.content))
        if end >= today:
            p = {"lista": "Reuniões do Copom", "inicioAgenda": f"'{today.isoformat()}'",
                 "fimAgenda": f"'{end.isoformat()}'"}
            r = http.get(BCB_CAL_URL, params=p, cache_ttl=CACHE_TTL)
            past_dates = {e.scheduled_local.date() for e in out}
            for e in parse_bcb_calendar(r.content):
                if e.scheduled_local.date() not in past_dates:
                    out.append(e)
    return _in_window(_dedupe(out), start, end)


# --------------------------------------------------------------------------------------------- EU national offices

INSEE_URL = "https://www.insee.fr/en/agenda-diffusion"
INSEE_PAGE = "https://www.insee.fr/en/information/1405540"
DESTATIS_URL = "https://www.destatis.de/SiteGlobals/Forms/Suche/Termine/EN/Terminsuche_Formular.html"
ISTAT_ICS_URL = ("https://calendar.google.com/calendar/ical/t49o0tm9m8f5c72l2gamob8djs%40group.calendar.google.com"
                 "/public/basic.ics")
INE_ICS_URL = "https://www.ine.es/dynt3/Calendario/en/calendario.ics"


def parse_insee(raw: str | bytes) -> list[CalEntry]:
    data = orjson.loads(raw if isinstance(raw, bytes) else raw.encode())
    out = []
    for d in data.get("documents") or []:
        emb = d.get("embargo")
        if not emb:
            continue
        dt = _utc_to(datetime.fromisoformat(emb.replace("Z", "+00:00")), TZ_PAR)
        fc = ((d.get("famille") or {}).get("facetteConjoncture") or {})
        name = (fc.get("libelleEn") or fc.get("libelleFr") or "").strip()
        if not name:
            continue
        ref = d.get("periodeReferenceEn") or d.get("periodeReference")
        out.append(CalEntry("insee_cal", str(d.get("id") or f"{name}|{dt.date()}"), name, "FR", dt, TZ_PAR, True,
                            "data", "INSEE", INSEE_PAGE, ref))
    return _dedupe(out)


def fetch_insee(start: date, end: date, settings: Settings) -> list[CalEntry]:
    body = {"q": "*", "start": 0, "sortFields": [{"field": "dateEmbargo_dt", "order": "asc"}], "filters": [],
            "rows": 1000, "facetsQuery": []}
    with _client("insee_cal", settings, {"Accept": "application/json"}) as http:
        r = http.post(INSEE_URL + "?q=*", json_body=body)
    return _in_window(parse_insee(r.content), start, end)


def parse_destatis(raw: str | bytes) -> list[CalEntry]:
    t = _text(raw)
    out = []
    for blk in re.split(r'<div\s+class="c-result c-result--event-preview', t)[1:]:
        hm = re.search(r'<h3 class="c-result__heading">(.*?)</h3>', blk, re.S)
        dm = re.search(r"Date of issue</strong>:\s*(\d{4})\.(\d{2})\.(\d{2})\s*(\(deadline\))?", blk)
        if not hm or not dm:
            continue
        head = re.sub(r"<a [^>]*><sup>.*?</sup></a>", "", hm.group(1), flags=re.S)
        name = _strip_tags(head).replace(" — ", " ").replace("— ", "").strip()
        name = re.sub(r"\s*—\s*", " ", name)
        d = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        rp = re.search(r"Reporting period</strong>:\s*([^<]+)<", blk)
        ref = _strip_tags(rp.group(1)) if rp else None
        ics = re.search(r'href="([^"]+\.ics)', blk)
        key = ics.group(1) if ics else f"{name}|{ref or ''}|{d.isoformat()}"
        if dm.group(4):
            name = f"{name} (deadline)"
        url = ("https://www.destatis.de/" + ics.group(1)) if ics else DESTATIS_URL
        out.append(CalEntry("destatis_cal", key, name, "DE", _at(d, 8, 0, TZ_FRA), TZ_FRA, False, "data", "Destatis",
                            url, ref))
    return _dedupe(out)


def fetch_destatis(start: date, end: date, settings: Settings) -> list[CalEntry]:
    out: list[CalEntry] = []
    with _client("destatis_cal", settings) as http:
        r = http.get(DESTATIS_URL, params={"nn": "1426424"}, cache_ttl=CACHE_TTL)
        out.extend(parse_destatis(r.content))
        pages = [int(x) for x in re.findall(r"gtp=1362242_list%253D(\d+)", r.text)]
        for pg in range(2, min(max(pages or [1]), 30) + 1):
            rp = http.get(DESTATIS_URL, params={"gtp": f"1362242_list%3D{pg}"}, cache_ttl=CACHE_TTL)
            page = parse_destatis(rp.content)
            if not page:
                break
            out.extend(page)
    return _in_window(_dedupe(out), start, end)


def parse_istat(raw: str | bytes) -> list[CalEntry]:
    out = []
    for ev in _ics_events(raw):
        if "DTSTART" not in ev or "SUMMARY" not in ev:
            continue
        dt, has_time = _ics_dt(ev["DTSTART"], TZ_ROM)
        if not has_time:
            dt = _at(dt.date(), 10, 0, TZ_ROM)
        summary = _ics_unescape(ev["SUMMARY"][1])
        name, _, ref = summary.partition("|")
        name, ref = name.strip(), ref.strip() or None
        uid = ev.get("UID", ({}, ""))[1] or f"{summary}|{dt.date()}"
        out.append(CalEntry("istat_cal", uid, name, "IT", dt, TZ_ROM, has_time, "data", "ISTAT",
                            "https://www.istat.it/en/information-and-services-for-users/journalists/press-releases/"
                            "press-calendar/", ref))
    return _dedupe(out)


def fetch_istat(start: date, end: date, settings: Settings) -> list[CalEntry]:
    """Disabled 2026-11-07 (A9, docs/it/ROBOTS_COMPLIANCE.md): the ICS is hosted on calendar.google.com, whose
    robots.txt disallows everything but the home page, and istat.it publishes no other machine-readable calendar
    (verified 2026-11-07) -> skipped with reason source_disallowed; stored istat_cal entries are kept."""
    from app.core.robots_policy import disabled_note  # noqa: PLC0415

    note = disabled_note("istat_cal")
    if note:
        raise SkipSource(note)
    return _in_window(parse_istat(_get("istat_cal", settings, ISTAT_ICS_URL)), start, end)  # pragma: no cover


def parse_ine(raw: str | bytes) -> list[CalEntry]:
    out = []
    for ev in _ics_events(raw):
        if "DTSTART" not in ev or "SUMMARY" not in ev:
            continue
        dt, has_time = _ics_dt(ev["DTSTART"], TZ_MAD)
        if not has_time:
            dt = _at(dt.date(), 9, 0, TZ_MAD)
        summary = _ics_unescape(ev["SUMMARY"][1])
        m = re.match(r"(.*?)\.\s+([^.]*\d{4})\s*$", summary)
        name, ref = (m.group(1).strip(), m.group(2).strip()) if m else (summary, None)
        uid = ev.get("UID", ({}, ""))[1] or f"{summary}|{dt.date()}"
        out.append(CalEntry("ine_cal", uid, name, "ES", dt, TZ_MAD, has_time, "data", "INE",
                            "https://www.ine.es/en/daco/daco41/calen_en.htm", ref))
    return _dedupe(out)


def fetch_ine(start: date, end: date, settings: Settings) -> list[CalEntry]:
    return _in_window(parse_ine(_get("ine_cal", settings, INE_ICS_URL)), start, end)


# --------------------------------------------------------------------------------------------- registry

SOURCES: dict[str, Callable[[date, date, Settings], list[CalEntry]]] = {
    "bls_cal": fetch_bls,
    "bea_cal": fetch_bea,
    "fomc": fetch_fomc,
    "eurostat_cal": fetch_eurostat,
    "ecb_gc": fetch_ecb_gc,
    "ecb_statcal": fetch_ecb_statcal,
    "ons_cal": fetch_ons,
    "boe_mpc": fetch_boe,
    "boj_mpm": fetch_boj,
    "statcan_cal": fetch_statcan,
    "boc_rate": fetch_boc,
    "abs_cal": fetch_abs,
    "rba_board": fetch_rba,
    "ibge_cal": fetch_ibge,
    "bcb_copom": fetch_bcb,
    "insee_cal": fetch_insee,
    "destatis_cal": fetch_destatis,
    "istat_cal": fetch_istat,
    "ine_cal": fetch_ine,
}


def fetch_all(start: date, end: date, settings: Settings | None = None,
              sources: Iterable[str] | None = None) -> tuple[list[CalEntry], dict]:
    """Run each fetcher (never raises). Report: {source_id: {status, n, error, seconds}}."""
    settings = settings or get_settings()
    wanted = list(sources) if sources is not None else list(SOURCES)
    entries: list[CalEntry] = []
    report: dict[str, dict[str, Any]] = {}
    for sid in wanted:
        t0 = time.monotonic()
        fn = SOURCES.get(sid)
        if fn is None:
            report[sid] = {"status": "skipped", "n": 0, "error": "unknown source", "seconds": 0.0}
            continue
        try:
            got = fn(start, end, settings)
            entries.extend(got)
            report[sid] = {"status": "ok", "n": len(got), "error": None, "seconds": round(time.monotonic() - t0, 2)}
        except SkipSource as e:
            report[sid] = {"status": "skipped", "n": 0, "error": str(e), "seconds": round(time.monotonic() - t0, 2)}
        except Exception as e:  # noqa: BLE001
            log.warning("calendar source %s failed: %r", sid, e)
            report[sid] = {"status": "failed", "n": 0, "error": f"{type(e).__name__}: {str(e)[:300]}",
                           "seconds": round(time.monotonic() - t0, 2)}
    return entries, report
