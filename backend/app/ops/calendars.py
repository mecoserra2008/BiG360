"""Expected-date calendars for the gap detector (O1.2).

    sessions("XNYS", start, end)        exchange sessions (exchange_calendars), cached on disk per MIC
    business_days(start, end)           Mon-Fri
    cftc_report_dates(start, now)       COT 'as of' Tuesdays whose Friday 15:30 ET release has passed
    expected_13f_period(now)            latest quarter end whose 45-day 13F deadline has passed
    finra_settlements(start, now)       mid-month / month-end settlement dates whose publication (+8 sessions) passed

exchange_calendars is slow to build for some markets (XKRX ~2 min on a busy machine because of the lunar holiday
rules), so session lists are cached in data/cache/ops/sessions/<MIC>.json (20 years back .. today + 400 d) and rebuilt
when older than 60 days or too short. Unknown MICs fall back to Mon-Fri.
"""

from __future__ import annotations

import bisect
import threading
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.paths import read_json, write_json_atomic

log = get_logger("app.ops.calendars")

CACHE_START = date(2000, 1, 1)
_MEM: dict[str, tuple[date, list[date]]] = {}
_LOCK = threading.Lock()
ET = ZoneInfo("America/New_York")


def business_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _cache_path(mic: str, settings: Settings | None = None):
    return (settings or get_settings()).cache_dir / "ops" / "sessions" / f"{mic}.json"


def _build(mic: str, end: date) -> list[date]:
    import exchange_calendars as xc  # noqa: PLC0415 - heavy import, only when the cache is cold

    cal = xc.get_calendar(mic, end=end.isoformat())  # default start = 20 years back (enough for gap detection)
    return [x.date() for x in cal.sessions]


def all_sessions(mic: str, settings: Settings | None = None) -> list[date]:
    """Sorted sessions (20 years back .. up to today + 400 d, or the last year the library has holidays for) of an
    exchange MIC; Mon-Fri fallback when the MIC is unknown. Rebuilt when the cache is older than 30 days."""
    today = datetime.now(UTC).date()
    with _LOCK:
        hit = _MEM.get(mic)
        if hit is not None and hit[0] == today:
            return hit[1]
    p = _cache_path(mic, settings)
    doc = read_json(p, {}) or {}
    days: list[date] | None = None
    if doc.get("sessions"):
        try:
            built = date.fromisoformat(str(doc.get("built", "1900-01-01"))[:10])
            got = [date.fromisoformat(x) for x in doc["sessions"]]
            if got and (today - built).days <= 30 and got[-1] >= today:
                days = got
        except ValueError:
            days = None
    if days is None:
        err: Exception | None = None
        # some calendars only carry holidays to the current year (XSHG): shorten the horizon instead of failing
        for end in (today + timedelta(days=400), date(today.year, 12, 31), today + timedelta(days=7)):
            try:
                days = _build(mic, end)
                write_json_atomic(p, {"mic": mic, "built": today.isoformat(), "sessions": [d.isoformat() for d in days]})
                log.info("calendar %s cached (%d sessions, to %s)", mic, len(days), end)
                break
            except Exception as e:  # unknown MIC / library error -> try a shorter horizon, then weekdays
                err = e
        if days is None:
            log.warning("calendar %s unavailable (%s): Mon-Fri fallback", mic, err)
            days = business_days(today - timedelta(days=7300), today + timedelta(days=400))
    with _LOCK:
        _MEM[mic] = (today, days)
    return days


def sessions(mic: str | None, start: date, end: date, settings: Settings | None = None) -> list[date]:
    if not mic or mic == "weekdays":
        return business_days(start, end)
    days = all_sessions(mic, settings)
    i, j = bisect.bisect_left(days, start), bisect.bisect_right(days, end)
    return days[i:j]


def last_session_on_or_before(mic: str | None, d: date, settings: Settings | None = None) -> date | None:
    s = sessions(mic, d - timedelta(days=20), d, settings)
    return s[-1] if s else None


def nth_session_after(mic: str | None, d: date, n: int, settings: Settings | None = None) -> date:
    s = sessions(mic, d + timedelta(days=1), d + timedelta(days=4 * n + 14), settings)
    return s[min(n, len(s)) - 1] if s else d + timedelta(days=n)


# ------------------------------------------------------------------------------------------------ CFTC / 13F / FINRA


def cftc_report_dates(start: date, now: datetime | None = None) -> list[date]:
    """COT report 'as of' Tuesdays in [start, ...] whose standard release (Friday 15:30 ET) has passed. Holiday weeks
    release one business day later; the detector's tolerance covers that."""
    now = (now or datetime.now(UTC)).astimezone(ET)
    d = start + timedelta(days=(1 - start.weekday()) % 7)  # first Tuesday >= start
    out = []
    while True:
        release = datetime.combine(d + timedelta(days=3), time(15, 30), ET)
        if release > now:
            break
        out.append(d)
        d += timedelta(days=7)
    return out


def quarter_end(d: date) -> date:
    q = (d.month - 1) // 3
    m = 3 * q + 3
    nxt = date(d.year + (m == 12), 1 if m == 12 else m + 1, 1)
    return nxt - timedelta(days=1)


def expected_13f_period(now: datetime | None = None) -> date:
    """Latest calendar quarter end whose 13F filing deadline (45 days after quarter end) has passed."""
    today = (now or datetime.now(UTC)).date()
    qe = quarter_end(today)
    for _ in range(6):
        if qe + timedelta(days=45) <= today:
            return qe
        qe = date(qe.year, qe.month - 2, 1) - timedelta(days=1) if qe.month > 3 else date(qe.year - 1, 12, 31)
    return qe


def _month_end(y: int, m: int) -> date:
    return date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)


def finra_settlements(start: date, now: datetime | None = None, settings: Settings | None = None) -> list[date]:
    """FINRA short-interest settlement dates (approx.: the session on/before the 15th and the month's last session)
    whose publication (~8 sessions later) has passed."""
    today = (now or datetime.now(UTC)).date()
    out: list[date] = []
    y, m = start.year, start.month
    while date(y, m, 1) <= today:
        for target in (date(y, m, 15), _month_end(y, m)):
            sd = last_session_on_or_before("XNYS", target, settings)
            if sd and sd >= start and nth_session_after("XNYS", sd, 8, settings) <= today:
                out.append(sd)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(set(out))
