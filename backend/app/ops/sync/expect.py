"""Expected latest period per calendar, for the startup sync scan (docs/STARTUP_SYNC_PLAN.md A1).

Unlike the gap detector (which expects "the session before today"), the sync works to the minute:
* exchange sessions: the latest session whose close (+ a publish delay) has passed - a market that is still open, or
  closed less than the delay ago, is `not_yet_published` with the next expected time;
* ECB reference rates: TARGET business days, published ~16:00 CET;
* CFTC: the Tuesday report released Friday 15:30 ET;
* 13F: quarter ends whose 45-day filing deadline has passed;
* FINRA short interest: settlement dates whose publication (~8 sessions later, 18:00 ET) has passed;
* GDELT: a UTC day is complete at 00:00 UTC (+40 min for the last 15-min slot and the daily finalise).

Exchange times come from exchange_calendars (cached per MIC in this process; the slow ones such as XKRX take ~1-2
min once). Every helper degrades to a Mon-Fri rule instead of raising.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.ops import calendars as cal

ET = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Berlin")
GDELT_FINALISE_MIN = 40
FINRA_PUBLISH_SESSIONS = 8


def _xcal(mic: str) -> Any:
    try:
        from app.normalize.price_integrity import _xcal as xc  # noqa: PLC0415

        return xc(mic)
    except Exception:  # pragma: no cover - library missing
        return None


def _aware(now: datetime | None) -> datetime:
    now = now or datetime.now(UTC)
    return now if now.tzinfo else now.replace(tzinfo=UTC)


def session_times(mic: str, day: date) -> tuple[datetime, datetime] | None:
    """(open, close) in UTC of `day` on `mic`, None when it is not a session or the calendar is unknown."""
    c = _xcal(mic)
    if c is None:
        return None
    try:
        import pandas as pd  # noqa: PLC0415

        ts = pd.Timestamp(day)
        if not c.is_session(ts):
            return None
        return c.session_open(ts).to_pydatetime(), c.session_close(ts).to_pydatetime()
    except Exception:
        return None


def _fallback_close(day: date) -> datetime:
    return datetime.combine(day, time(21, 0), UTC)


def published_session(mic: str | None, now: datetime | None = None, delay_min: int = 30) -> date | None:
    """Latest session of `mic` whose close + delay_min is at or before now (Mon-Fri 21:00 UTC fallback)."""
    now = _aware(now)
    if mic and mic not in ("weekdays", "daily"):
        try:
            from app.normalize.price_integrity import last_closed_session  # noqa: PLC0415

            d = last_closed_session(mic, now, delay_min)
            if d is not None:
                return d
        except Exception:
            pass
    d = now.date()
    for _ in range(10):
        if d.weekday() < 5 and _fallback_close(d) + timedelta(minutes=delay_min) <= now:
            return d
        d -= timedelta(days=1)
    return None


def next_publication(mic: str | None, now: datetime | None = None, delay_min: int = 30) -> datetime | None:
    """When the next session's bar is expected (its close + delay_min), UTC."""
    now = _aware(now)
    for k in range(0, 15):
        day = now.date() + timedelta(days=k)
        st = session_times(mic, day) if mic and mic not in ("weekdays", "daily") else None
        if st is None:
            if mic and mic not in ("weekdays", "daily") and _xcal(mic) is not None:
                continue   # holiday / weekend on a known calendar
            if day.weekday() >= 5:
                continue
            close = _fallback_close(day)
        else:
            close = st[1]
        t = close + timedelta(minutes=delay_min)
        if t > now:
            return t
    return None


def market_state(mic: str | None, now: datetime | None = None) -> str | None:
    """'open (closes 20:00 UTC)' while a session runs, else None."""
    now = _aware(now)
    if not mic:
        return None
    st = session_times(mic, now.date())
    if st and st[0] <= now < st[1]:
        return f"market open (closes {st[1]:%H:%M} UTC)"
    return None


def sessions_between(mic: str | None, after: date | None, upto: date | None) -> list[date]:
    """Sessions in (after, upto]."""
    if upto is None:
        return []
    start = (after + timedelta(days=1)) if after else upto - timedelta(days=14)
    if start > upto:
        return []
    return cal.sessions(mic or "weekdays", start, upto)


# ------------------------------------------------------------------------------------------------ ECB / weekdays

_TARGET_FIXED = {(1, 1), (5, 1), (12, 25), (12, 26)}


def _easter(y: int) -> date:
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = ((h + m - 7 * n + 114) % 31) + 1
    return date(y, month, day)


def target_day(d: date) -> bool:
    if d.weekday() >= 5 or (d.month, d.day) in _TARGET_FIXED:
        return False
    e = _easter(d.year)
    return d not in (e - timedelta(days=2), e + timedelta(days=1))


def ecb_expected(now: datetime | None = None, delay_min: int = 30) -> date:
    """Latest TARGET day whose ECB reference rates (16:00 CET) + delay are out."""
    now = _aware(now)
    d = now.date()
    for _ in range(12):
        pub = datetime.combine(d, time(16, 0), CET) + timedelta(minutes=delay_min)
        if target_day(d) and pub <= now:
            return d
        d -= timedelta(days=1)
    return d


def next_ecb(now: datetime | None = None, delay_min: int = 30) -> datetime:
    now = _aware(now)
    d = now.date()
    for _ in range(12):
        pub = datetime.combine(d, time(16, 0), CET) + timedelta(minutes=delay_min)
        if target_day(d) and pub > now:
            return pub.astimezone(UTC)
        d += timedelta(days=1)
    return now + timedelta(days=1)


# ------------------------------------------------------------------------------------------------ CFTC / 13F / FINRA


def cftc_expected(now: datetime | None = None) -> date | None:
    now = _aware(now)
    got = cal.cftc_report_dates(now.date() - timedelta(days=21), now)
    return got[-1] if got else None


def next_cftc(now: datetime | None = None) -> datetime:
    now = _aware(now).astimezone(ET)
    d = now.date()
    for _ in range(9):
        if d.weekday() == 4:
            t = datetime.combine(d, time(15, 30), ET)
            if t > now:
                return t.astimezone(UTC)
        d += timedelta(days=1)
    return (now + timedelta(days=7)).astimezone(UTC)


def expected_13f(now: datetime | None = None) -> date:
    return cal.expected_13f_period(_aware(now))


def next_13f(now: datetime | None = None) -> datetime:
    """Deadline of the first quarter end not yet expected (45 days after it)."""
    now = _aware(now)
    exp = expected_13f(now)
    nxt_q = cal.quarter_end(exp + timedelta(days=1))
    return datetime.combine(nxt_q + timedelta(days=45), time(22, 0), UTC)


def finra_expected(now: datetime | None = None) -> date | None:
    now = _aware(now)
    got = finra_published(now.date() - timedelta(days=75), now)
    return got[-1] if got else None


def _finra_pub(sd: date) -> datetime:
    return datetime.combine(cal.nth_session_after("XNYS", sd, FINRA_PUBLISH_SESSIONS), time(18, 0), ET).astimezone(UTC)


def finra_settlement_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        for target in (date(y, m, 15), date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)):
            sd = cal.last_session_on_or_before("XNYS", target)
            if sd and start <= sd <= end:
                out.append(sd)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return sorted(set(out))


def finra_published(start: date, now: datetime | None = None) -> list[date]:
    """Settlement dates in [start, today] whose publication (+8 sessions, 18:00 ET) has passed."""
    now = _aware(now)
    return [sd for sd in finra_settlement_dates(start, now.date()) if _finra_pub(sd) <= now]


def next_finra(now: datetime | None = None) -> datetime | None:
    now = _aware(now)
    for sd in finra_settlement_dates(now.date() - timedelta(days=30), now.date() + timedelta(days=45)):
        t = _finra_pub(sd)
        if t > now:
            return t
    return None


# ------------------------------------------------------------------------------------------------ GDELT


def gdelt_expected(now: datetime | None = None) -> date:
    now = _aware(now).astimezone(UTC)
    cut = datetime.combine(now.date(), time(0, 0), UTC) + timedelta(minutes=GDELT_FINALISE_MIN)
    return now.date() - timedelta(days=1 if now >= cut else 2)


def next_gdelt(now: datetime | None = None) -> datetime:
    now = _aware(now).astimezone(UTC)
    t = datetime.combine(now.date(), time(0, 0), UTC) + timedelta(minutes=GDELT_FINALISE_MIN)
    return t if t > now else t + timedelta(days=1)
