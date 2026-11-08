"""Shared helpers for the earnings package: labels, issuer / primary-security metadata, sessions, dates."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table

log = get_logger("app.earnings")

LABEL_EXPECTATION = "Model-based expectation (not consensus)"
LABEL_SURPRISE = "Model-based surprise (not consensus)"
LABEL_PROVIDER = "Provider estimate (Yahoo)"
LABEL_PROJECTION = "Model-based projection (reporting cadence)"
LABEL_COMPANY = "Company-announced (exchange schedule)"

DATE_SOURCE_LABEL = {"company_announced": LABEL_COMPANY, "provider_estimate": LABEL_PROVIDER,
                     "model_projection": LABEL_PROJECTION}

#: regular session (local open, local close) by exchange time zone; default 09:00-17:00
SESSION_HOURS: dict[str, tuple[time, time]] = {
    "America/New_York": (time(9, 30), time(16, 0)), "America/Toronto": (time(9, 30), time(16, 0)),
    "America/Sao_Paulo": (time(10, 0), time(17, 0)), "America/Mexico_City": (time(8, 30), time(15, 0)),
    "America/Santiago": (time(9, 30), time(16, 0)), "America/Lima": (time(9, 0), time(16, 0)),
    "Asia/Tokyo": (time(9, 0), time(15, 30)), "Asia/Seoul": (time(9, 0), time(15, 30)),
    "Asia/Hong_Kong": (time(9, 30), time(16, 0)), "Asia/Shanghai": (time(9, 30), time(15, 0)),
    "Asia/Taipei": (time(9, 0), time(13, 30)), "Europe/London": (time(8, 0), time(16, 30)),
    "Australia/Sydney": (time(10, 0), time(16, 0)), "Asia/Singapore": (time(9, 0), time(17, 0)),
}
REGION_TZ = {"US": "America/New_York", "CA": "America/Toronto", "LATAM": "America/Sao_Paulo", "UK": "Europe/London",
             "EU": "Europe/Paris", "JP": "Asia/Tokyo", "KR": "Asia/Seoul", "CN": "Asia/Shanghai",
             "HK": "Asia/Hong_Kong", "APAC": "Asia/Singapore", "OTHER": "UTC"}
FISCAL_PERIODS = ("Q1", "Q2", "Q3", "Q4", "H1", "H2", "FY")


def now_utc() -> datetime:
    return datetime.now(UTC)


def tz_of(tz: str | None, region: str | None = None) -> ZoneInfo:
    try:
        return ZoneInfo(tz or REGION_TZ.get(region or "", "UTC"))
    except Exception:  # noqa: BLE001 - unknown zone name
        return ZoneInfo(REGION_TZ.get(region or "", "UTC"))


def local_dt(ts: datetime | None, tz: str | None, region: str | None = None) -> datetime | None:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(tz_of(tz, region))


def classify_session(ts: datetime | None, tz: str | None, region: str | None = None) -> str:
    """pre_market / intraday / post_market by the listing's local regular session; unknown without a time."""
    lt = local_dt(ts, tz, region)
    if lt is None:
        return "unknown"
    o, c = SESSION_HOURS.get(str(tz_of(tz, region)), (time(9, 0), time(17, 0)))
    t = lt.time()
    if t < o:
        return "pre_market"
    if t >= c:
        return "post_market"
    return "intraday"


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def on_or_after_weekday(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def reaction_day(release_date: date, session: str) -> date:
    """Weekday approximation of the first session reflecting the release (holidays resolved against the security's
    own trading days in app.earnings.reactions)."""
    return next_weekday(release_date) if session == "post_market" else on_or_after_weekday(release_date)


def month_end(y: int, m: int) -> date:
    if m == 12:
        return date(y, 12, 31)
    return date(y, m + 1, 1) - timedelta(days=1)


def add_months_end(d: date, months: int) -> date:
    """Month-end `months` after d's month (d assumed to be a month end)."""
    k = d.year * 12 + (d.month - 1) + months
    return month_end(k // 12, k % 12 + 1)


def is_month_end(d: date) -> bool:
    return (d + timedelta(days=1)).day == 1


def next_period_end(last: date, months: int, prev_year: date | None = None) -> date:
    """Next fiscal period end after `last`: month-end arithmetic for month-end reporters, otherwise the same period a
    year earlier + 364 days (52/53-week fiscal years), else last + 91 * months / 3 days."""
    if is_month_end(last):
        return add_months_end(last, months)
    if prev_year is not None:
        return prev_year + timedelta(days=364)
    return last + timedelta(days=round(30.4 * months))


def calendar_quarter_end_before(d: date, min_gap_days: int = 10) -> date:
    """Latest calendar quarter end at least `min_gap_days` before d (period guess for provider-only dates)."""
    x = d - timedelta(days=min_gap_days)
    q_m = ((x.month - 1) // 3) * 3  # months completed before this quarter
    if q_m == 0:
        return date(x.year - 1, 12, 31)
    return month_end(x.year, q_m)


def season_of(period_end: date) -> str:
    return f"{period_end.year}Q{(period_end.month - 1) // 3 + 1}"


# ------------------------------------------------------------------------------------------------ metadata


def issuer_meta(settings: Settings | None = None) -> pl.DataFrame:
    """One row per issuer: region, country, sector, cik, fye, currency + primary security (ticker, yf ticker, tz).
    Primary = is_primary_security, else the first common share."""
    s = settings or get_settings()
    iss = (scan_table("issuers", s)
           .select("issuer_id", "name", "region", "country", "sector_id", "subsector_id", "cik",
                   "fiscal_year_end_mmdd", "reporting_currency", "is_active", "updated_at")
           .sort("updated_at").unique("issuer_id", keep="last").drop("updated_at").collect())
    sm = (scan_table("securities_master", s)
          .select("security_id", "issuer_id", "security_type", "display_ticker", "primary_listing_id",
                  "is_primary_security", "updated_at")
          .sort("updated_at").unique("security_id", keep="last").drop("updated_at")
          .filter(~pl.col("security_type").is_in(["fx", "future", "index", "etf"]).fill_null(False))
          .collect())
    sm = (sm.with_columns(pl.when(pl.col("is_primary_security").fill_null(False)).then(0)
                          .when(pl.col("security_type") == "common").then(1).otherwise(2).alias("_r"))
          .sort("issuer_id", "_r", "security_id").unique("issuer_id", keep="first").drop("_r"))
    li = (scan_table("listings", s).select("listing_id", "yf_ticker", "timezone", "local_symbol", "mic", "valid_to")
          .filter(pl.col("valid_to").is_null()).drop("valid_to").unique("listing_id").collect())
    sm = sm.join(li, left_on="primary_listing_id", right_on="listing_id", how="left")
    out = iss.join(sm.drop("security_type"), on="issuer_id", how="left")
    return out.rename({"display_ticker": "ticker"})


def universe_securities(universes: list[str], settings: Settings | None = None) -> set[str]:
    s = settings or get_settings()
    um = (scan_table("universe_membership", s).filter(pl.col("universe_id").is_in(universes)
                                                       & pl.col("valid_to").is_null())
          .select("security_id").unique().collect())
    return set(um.get_column("security_id").to_list())


# ------------------------------------------------------------------------------------------------ state


def state_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.data_dir / "state" / "earnings.json"


def load_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def update_state(key: str, value: Any, settings: Settings | None = None) -> None:
    p = state_path(settings)
    st = load_state(settings)
    st[key] = value
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def write(df: pl.DataFrame, table: str, settings: Settings | None = None, mode: str | None = None) -> int:
    from app.core.schema import get_contract  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415

    c = get_contract(table)
    df = df.with_columns([pl.lit(None, dtype=col.polars).alias(col.name) for col in c.columns
                          if col.name not in df.columns])
    df = df.select([pl.col(col.name).cast(col.polars, strict=False) for col in c.columns])
    return write_table(df, table, mode=mode, settings=settings or get_settings())
