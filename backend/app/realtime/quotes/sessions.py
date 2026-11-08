"""Market hours for the delayed quotes service.

Regular sessions (local exchange time) per MIC, exchange holidays from the session caches of app.ops.calendars
(data/cache/ops/sessions/<MIC>.json, built by the gap detector; read only here, never built: building some calendars
takes minutes). A MIC without a cached calendar falls back to its holiday proxy (XNAS -> XNYS, XSHE -> XSHG, Euronext
-> XETR, ...) and then to Mon-Fri. Lunch breaks are ignored (a poll at lunch simply returns the morning print).

Kinds:
* exchange  open..close on session days of the MIC's calendar;
* fx        24x5: Sunday 17:00 to Friday 17:00 New York time;
* globex    CME Globex: Sunday 18:00 to Friday 17:00 New York time, daily maintenance break 17:00-18:00.

`state(key, now)` -> MarketState(open, reason, opens_at, closes_at). A market counts as "open" for polling from the open
until `close + tail_min` (default 25 min: delayed feeds lag up to 20 min, so the official closing print arrives after
the bell) - `in_tail` marks that last stretch.
"""

from __future__ import annotations

import threading
import time as _time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.ops.paths import read_json

NY = ZoneInfo("America/New_York")

#: MIC -> (timezone, open, close). Regular continuous sessions (no auctions / extended hours).
HOURS: dict[str, tuple[str, time, time]] = {
    "XNYS": ("America/New_York", time(9, 30), time(16, 0)),
    "XNAS": ("America/New_York", time(9, 30), time(16, 0)),
    "XASE": ("America/New_York", time(9, 30), time(16, 0)),
    "ARCX": ("America/New_York", time(9, 30), time(16, 0)),
    "BATS": ("America/New_York", time(9, 30), time(16, 0)),
    "XXXX": ("America/New_York", time(9, 30), time(16, 0)),      # ICE US dollar index (US cash session)
    "XTSE": ("America/Toronto", time(9, 30), time(16, 0)),
    "XMEX": ("America/Mexico_City", time(8, 30), time(15, 0)),
    "BVMF": ("America/Sao_Paulo", time(10, 0), time(17, 0)),
    "XSGO": ("America/Santiago", time(9, 30), time(16, 0)),
    "XBUE": ("America/Argentina/Buenos_Aires", time(11, 0), time(17, 0)),
    "XBOG": ("America/Bogota", time(9, 30), time(16, 0)),
    "XLON": ("Europe/London", time(8, 0), time(16, 30)),
    "XDUB": ("Europe/Dublin", time(8, 0), time(16, 30)),
    "XLIS": ("Europe/Lisbon", time(8, 0), time(16, 30)),
    "XETR": ("Europe/Berlin", time(9, 0), time(17, 30)),
    "XFRA": ("Europe/Berlin", time(9, 0), time(17, 30)),
    "XPAR": ("Europe/Paris", time(9, 0), time(17, 30)),
    "XAMS": ("Europe/Amsterdam", time(9, 0), time(17, 30)),
    "XBRU": ("Europe/Brussels", time(9, 0), time(17, 30)),
    "XMIL": ("Europe/Rome", time(9, 0), time(17, 30)),
    "XMAD": ("Europe/Madrid", time(9, 0), time(17, 30)),
    "XSWX": ("Europe/Zurich", time(9, 0), time(17, 30)),
    "XWBO": ("Europe/Vienna", time(9, 0), time(17, 30)),
    "XSTO": ("Europe/Stockholm", time(9, 0), time(17, 30)),
    "XCSE": ("Europe/Copenhagen", time(9, 0), time(17, 0)),
    "XHEL": ("Europe/Helsinki", time(10, 0), time(18, 30)),
    "XOSL": ("Europe/Oslo", time(9, 0), time(16, 20)),
    "XICE": ("Atlantic/Reykjavik", time(9, 30), time(15, 30)),
    "XWAR": ("Europe/Warsaw", time(9, 0), time(17, 0)),
    "XPRA": ("Europe/Prague", time(9, 0), time(16, 25)),
    "XBUD": ("Europe/Budapest", time(9, 0), time(17, 0)),
    "XBSE": ("Europe/Bucharest", time(10, 0), time(17, 45)),
    "XATH": ("Europe/Athens", time(10, 0), time(17, 20)),
    "XLIT": ("Europe/Vilnius", time(10, 0), time(16, 0)),
    "XTAL": ("Europe/Tallinn", time(10, 0), time(16, 0)),
    "XTKS": ("Asia/Tokyo", time(9, 0), time(15, 30)),
    "XKRX": ("Asia/Seoul", time(9, 0), time(15, 30)),
    "XKOS": ("Asia/Seoul", time(9, 0), time(15, 30)),
    "XHKG": ("Asia/Hong_Kong", time(9, 30), time(16, 0)),
    "XSHG": ("Asia/Shanghai", time(9, 30), time(15, 0)),
    "XSHE": ("Asia/Shanghai", time(9, 30), time(15, 0)),
    "XTAI": ("Asia/Taipei", time(9, 0), time(13, 30)),
    "ROCO": ("Asia/Taipei", time(9, 0), time(13, 30)),
    "XNSE": ("Asia/Kolkata", time(9, 15), time(15, 30)),
    "XBOM": ("Asia/Kolkata", time(9, 15), time(15, 30)),
    "XASX": ("Australia/Sydney", time(10, 0), time(16, 0)),
    "XSES": ("Asia/Singapore", time(9, 0), time(17, 0)),
    "XIDX": ("Asia/Jakarta", time(9, 0), time(16, 0)),
    "XKLS": ("Asia/Kuala_Lumpur", time(9, 0), time(17, 0)),
    "XBKK": ("Asia/Bangkok", time(10, 0), time(16, 30)),
    "XJSE": ("Africa/Johannesburg", time(9, 0), time(17, 0)),
}

#: MIC -> MIC whose cached holiday calendar is used (app.ops.calendars caches only a few)
HOLIDAY_PROXY: dict[str, str] = {
    "XNAS": "XNYS", "XASE": "XNYS", "ARCX": "XNYS", "BATS": "XNYS", "XXXX": "XNYS",
    "XSHE": "XSHG", "XKOS": "XKRX", "ROCO": "XTAI",
    "XFRA": "XETR", "XPAR": "XETR", "XAMS": "XETR", "XBRU": "XETR", "XMIL": "XETR", "XMAD": "XETR", "XLIS": "XETR",
    "XWBO": "XETR", "XDUB": "XLON",
}

#: timezone -> MIC (a listing / Yahoo quote without a known MIC)
TZ_MIC: dict[str, str] = {tz: mic for mic, (tz, _o, _c) in reversed(list(HOURS.items()))}
TZ_MIC.update({"America/New_York": "XNYS", "America/Chicago": "XNYS", "Europe/Berlin": "XETR", "Asia/Shanghai": "XSHG",
               "Asia/Seoul": "XKRX", "Asia/Taipei": "XTAI", "Asia/Kolkata": "XNSE", "Europe/London": "XLON"})

FX = "FX"
GLOBEX = "GLOBEX"


@dataclass
class MarketState:
    key: str
    open: bool                       # poll now (open, or within the closing tail)
    in_tail: bool = False            # after the bell, waiting for the delayed closing print
    reason: str = ""                 # open | closed_weekend | closed_holiday | before_open | after_close | ...
    opens_at: datetime | None = None   # next open (UTC) when closed
    closes_at: datetime | None = None  # close (UTC) when open

    def to_dict(self) -> dict[str, Any]:
        def iso(x: datetime | None) -> str | None:
            return x.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") if x else None

        return {"market": self.key, "open": self.open, "in_tail": self.in_tail, "reason": self.reason,
                "opens_at": iso(self.opens_at), "closes_at": iso(self.closes_at)}


class SessionCalendar:
    """Session-day lookups from the cached calendars (re-read at most every `ttl_s`)."""

    def __init__(self, settings: Settings | None = None, ttl_s: float = 6 * 3600, days: dict[str, set[date]] | None = None):
        self.s = settings
        self.ttl_s = ttl_s
        self._days: dict[str, tuple[float, set[date] | None]] = {}
        self._fixed = days            # tests: MIC -> session days
        self._lock = threading.Lock()

    def _load(self, mic: str) -> set[date] | None:
        if self._fixed is not None:
            return self._fixed.get(mic)
        now = _time.time()
        with self._lock:
            hit = self._days.get(mic)
            if hit and now - hit[0] < self.ttl_s:
                return hit[1]
        s = self.s or get_settings()
        doc = read_json(s.cache_dir / "ops" / "sessions" / f"{mic}.json", {}) or {}
        days: set[date] | None = None
        try:
            if doc.get("sessions"):
                days = {date.fromisoformat(x) for x in doc["sessions"]}
        except ValueError:
            days = None
        with self._lock:
            self._days[mic] = (now, days)
        return days

    def is_session(self, mic: str, d: date) -> bool:
        for m in (mic, HOLIDAY_PROXY.get(mic)):
            if not m:
                continue
            days = self._load(m)
            if days:
                lo, hi = min(days), max(days)
                if lo <= d <= hi:
                    return d in days
        return d.weekday() < 5


def market_key(mic: str | None, timezone: str | None = None, quote_type: str | None = None) -> str:
    """Session key of a quote target: FX, GLOBEX or an exchange MIC."""
    qt = (quote_type or "").lower()
    if qt == "fx" or mic == "XOFF":
        return FX
    if qt == "future" or mic == "XCME":
        return GLOBEX
    if mic and mic in HOURS:
        return mic
    if timezone and timezone in TZ_MIC:
        return TZ_MIC[timezone]
    return "XNYS"


def _fx_open(now: datetime, globex: bool) -> tuple[bool, str, datetime | None, datetime | None]:
    t = now.astimezone(NY)
    wd = t.weekday()   # Mon 0 .. Sun 6
    start_h = 18 if globex else 17
    week_open = (wd == 6 and t.time() >= time(start_h, 0)) or wd in (0, 1, 2, 3) or (wd == 4 and t.time() < time(17, 0))
    if not week_open:
        days_to_sun = (6 - wd) % 7
        nxt = datetime.combine(t.date() + timedelta(days=days_to_sun), time(start_h, 0), NY)
        return False, "closed_weekend", nxt, None
    if globex and time(17, 0) <= t.time() < time(18, 0):
        return False, "daily_break", datetime.combine(t.date(), time(18, 0), NY), None
    # close of the current trading day (17:00 NY), Friday close for the weekend
    close_day = t.date() if t.time() < time(17, 0) else t.date() + timedelta(days=1)
    return True, "open", None, datetime.combine(close_day, time(17, 0), NY)


def state(key: str, now: datetime, cal: SessionCalendar, tail_min: float = 25.0) -> MarketState:
    if key in (FX, GLOBEX):
        ok, why, nxt, cls = _fx_open(now, key == GLOBEX)
        return MarketState(key, ok, False, why, nxt, cls)
    tz_name, o, c = HOURS.get(key, HOURS["XNYS"])
    tz = ZoneInfo(tz_name)
    loc = now.astimezone(tz)
    d = loc.date()
    open_dt = datetime.combine(d, o, tz)
    close_dt = datetime.combine(d, c, tz)
    tail_end = close_dt + timedelta(minutes=tail_min)
    if cal.is_session(key, d):
        if open_dt <= loc < close_dt:
            return MarketState(key, True, False, "open", None, close_dt)
        if close_dt <= loc < tail_end:
            return MarketState(key, True, True, "closing_print", None, close_dt)
        if loc < open_dt:
            return MarketState(key, False, False, "before_open", open_dt, None)
    # next session's open
    nd = d + timedelta(days=1)
    for _ in range(15):
        if cal.is_session(key, nd):
            break
        nd += timedelta(days=1)
    nxt = datetime.combine(nd, o, tz)
    if not cal.is_session(key, d):
        why = "closed_weekend" if d.weekday() >= 5 else "closed_holiday"
    else:
        why = "after_close"
    return MarketState(key, False, False, why, nxt, None)


REASON_TEXT = {
    "open": "market open", "closing_print": "after the close, waiting for the delayed closing print",
    "before_open": "market not open yet", "after_close": "market closed for the day",
    "closed_weekend": "weekend", "closed_holiday": "exchange holiday", "daily_break": "daily maintenance break",
}
