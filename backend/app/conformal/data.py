"""Point-in-time data access for the conformal jobs (reads only; DuckDB over the lake with a 1.5 GB memory limit).

Prices: prices_daily OHLC x adj_factor (split + dividend adjusted, local currency) on the region's trading calendar
(union of dates with >= 30% of the region's securities printing), as wide (T, N) float64 arrays; a security's
non-print days are NaN. Unadjusted closes are kept for price-level bands. One region (partition) at a time.

Earnings days: earnings_events reaction_date (plus release_date when the session is unknown) mapped issuer -> all
its securities. For past origins these realised dates are used as if the schedule had been known in advance
(companies pre-announce report dates; documented assumption). Upcoming dates at the as-of date come from
earnings_calendar (expected_date) and earnings_announcements (event_date >= as_of, knowledge_date <= as_of).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.conformal.data")

MAX_ABS_LOGRET = 1.0          # |log return| above this is treated as a data error (unit flip), not news
MVP_UNIVERSES = ("mvp_us", "mvp_global")


def lake(tables: list[str], settings: Settings | None = None, mem: str = "1500MB", threads: int = 4) -> Any:
    from app.modeling.datasets import lake_connection  # noqa: PLC0415

    con = lake_connection(tables, settings or get_settings())
    con.execute(f"SET memory_limit = '{mem}'; SET threads = {threads}")
    return con


@dataclass
class Panel:
    region: str
    dates: np.ndarray            # datetime64[D] (T,)
    ids: list[str]
    C: np.ndarray                # adjusted close (T, N)
    H: np.ndarray
    L: np.ndarray
    O: np.ndarray                # noqa: E741
    close_raw: np.ndarray        # unadjusted close
    dv: np.ndarray               # USD dollar volume
    meta: pl.DataFrame = field(default_factory=pl.DataFrame)   # security_id, issuer_id, sector_id, subsector_id

    @property
    def T(self) -> int:  # noqa: N802
        return len(self.dates)

    @property
    def N(self) -> int:  # noqa: N802
        return len(self.ids)

    def col(self) -> dict[str, int]:
        return {s: i for i, s in enumerate(self.ids)}

    def subset(self, ids: list[str]) -> Panel:
        m = self.col()
        ix = [m[s] for s in ids if s in m]
        keep = [self.ids[i] for i in ix]
        return Panel(self.region, self.dates, keep, self.C[:, ix], self.H[:, ix], self.L[:, ix], self.O[:, ix],
                     self.close_raw[:, ix], self.dv[:, ix], self.meta.filter(pl.col("security_id").is_in(keep)))


def universe_ids(universes: tuple[str, ...] | list[str], settings: Settings | None = None) -> pl.DataFrame:
    """Current members of the given universes with security / issuer metadata (sector is current-only, documented
    minor look-ahead in issuers)."""
    con = lake(["universe_membership", "securities_master", "issuers"], settings)
    try:
        ph = ", ".join("?" for _ in universes)
        return con.execute(f"""
            SELECT DISTINCT u.security_id, s.issuer_id, i.region AS issuer_region,
                   coalesce(i.sector_id, 'NA') AS sector_id, coalesce(i.subsector_id, 'NA') AS subsector_id
            FROM universe_membership u JOIN securities_master s ON s.security_id = u.security_id
            LEFT JOIN issuers i ON i.issuer_id = s.issuer_id
            WHERE u.universe_id IN ({ph}) AND u.valid_to IS NULL""", list(universes)).pl()
    finally:
        con.close()


def price_regions(settings: Settings | None = None) -> list[str]:
    s = settings or get_settings()
    from app.core.storage import table_dir  # noqa: PLC0415

    root = table_dir("prices_daily", s)
    return sorted(p.name.split("=", 1)[1] for p in root.glob("region=*") if p.is_dir() and p.name != "region=FX")


def load_panel(region: str, start: date, end: date, security_ids: list[str] | None = None,
               settings: Settings | None = None, min_share: float = 0.3, meta: pl.DataFrame | None = None) -> Panel | None:
    con = lake(["prices_daily"], settings)
    try:
        if security_ids is not None:
            con.register("_cf_ids", pl.DataFrame({"security_id": security_ids}).to_arrow())
            filt = "AND security_id IN (SELECT security_id FROM _cf_ids)"
        else:
            filt = ""
        df = con.execute(f"""
            SELECT security_id, date, open, high, low, close, coalesce(adj_factor, 1.0) AS f,
                   close * usd_per_unit * volume AS dv
            FROM prices_daily
            WHERE region = ? AND date BETWEEN ? AND ? AND close > 0 {filt}""", [region, start, end]).pl()
    finally:
        con.close()
    if df.is_empty():
        return None
    df = df.with_columns(pl.col("date").cast(pl.Date))
    nsec = df["security_id"].n_unique()
    cnt = df.group_by("date").len()
    keep_dates = cnt.filter(pl.col("len") >= max(1, int(min_share * min(nsec, cnt["len"].max())))).sort("date")["date"]
    df = df.filter(pl.col("date").is_in(keep_dates.implode()))
    ids = sorted(df["security_id"].unique().to_list())
    dates = np.array(keep_dates.to_list(), dtype="datetime64[D]")
    di = {d: i for i, d in enumerate(keep_dates.to_list())}
    ci = {s: i for i, s in enumerate(ids)}
    T, N = len(dates), len(ids)
    rows = np.array([di[d] for d in df["date"].to_list()], dtype=np.int64)
    cols = np.array([ci[s] for s in df["security_id"].to_list()], dtype=np.int64)

    def wide(col: str, adj: bool) -> np.ndarray:
        out = np.full((T, N), np.nan)
        v = df[col].cast(pl.Float64).to_numpy()
        if adj:
            v = v * df["f"].cast(pl.Float64).to_numpy()
        out[rows, cols] = v
        return out

    C = wide("close", True)
    O, H, L = wide("open", True), wide("high", True), wide("low", True)  # noqa: E741
    O = np.where(np.isfinite(O) & (O > 0), O, C)
    H = np.where(np.isfinite(H) & (H > 0), H, C)
    L = np.where(np.isfinite(L) & (L > 0), L, C)
    H = np.fmax(H, np.fmax(O, C))
    L = np.fmin(L, np.fmin(O, C))
    raw = wide("close", False)
    dv = wide("dv", False)
    m = meta.filter(pl.col("security_id").is_in(ids)) if meta is not None else pl.DataFrame({"security_id": ids})
    return Panel(region, dates, ids, C, H, L, O, raw, dv, m)


def log_returns(C: np.ndarray, max_abs: float = MAX_ABS_LOGRET) -> np.ndarray:
    """Close-to-close log returns on each security's own print days (a non-print day -> NaN; the next print's
    return spans the gap). |r| > max_abs -> NaN (data error)."""
    lc = np.log(C)
    T, N = lc.shape
    out = np.full((T, N), np.nan)
    last = np.full(N, np.nan)
    for t in range(T):
        x = lc[t]
        ok = np.isfinite(x)
        r = x - last
        out[t] = np.where(ok, r, np.nan)
        last = np.where(ok, x, last)
    out[np.abs(out) > max_abs] = np.nan
    return out


def ffill(X: np.ndarray, limit: int = 3) -> np.ndarray:
    """Forward-fill NaN along time up to `limit` rows."""
    out = X.copy()
    T = X.shape[0]
    age = np.zeros(X.shape[1:], dtype=np.int64)
    last = np.full(X.shape[1:], np.nan)
    for t in range(T):
        ok = np.isfinite(X[t])
        age = np.where(ok, 0, age + 1)
        last = np.where(ok, X[t], last)
        out[t] = np.where(ok, X[t], np.where(age <= limit, last, np.nan))
    return out


def earnings_days(ids: list[str], start: date, end: date, settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, date] realised earnings reaction days (and release days when the session is unknown)."""
    con = lake(["earnings_events", "securities_master"], settings)
    try:
        con.register("_cf_ids", pl.DataFrame({"security_id": ids}).to_arrow())
        df = con.execute("""
            WITH ev AS (
              SELECT issuer_id, coalesce(reaction_date, release_date) AS d FROM earnings_events
              WHERE release_date BETWEEN ? AND ?
              UNION
              SELECT issuer_id, release_date AS d FROM earnings_events
              WHERE release_date BETWEEN ? AND ? AND (session = 'unknown' OR reaction_date IS NULL))
            SELECT DISTINCT s.security_id, ev.d AS date
            FROM ev JOIN securities_master s ON s.issuer_id = ev.issuer_id
            WHERE s.security_id IN (SELECT security_id FROM _cf_ids) AND ev.d IS NOT NULL""",
                          [start, end, start, end]).pl()
    finally:
        con.close()
    return df.with_columns(pl.col("date").cast(pl.Date))


def upcoming_earnings(ids: list[str], as_of: date, horizon_days: int = 40, settings: Settings | None = None) -> pl.DataFrame:
    """[security_id, date] expected earnings dates in (as_of, as_of + horizon_days] known at as_of."""
    con = lake(["earnings_calendar", "earnings_announcements", "securities_master"], settings)
    hi = as_of + timedelta(days=horizon_days)
    try:
        con.register("_cf_ids", pl.DataFrame({"security_id": ids}).to_arrow())
        df = con.execute("""
            WITH ev AS (
              SELECT issuer_id, expected_date AS d FROM earnings_calendar WHERE expected_date > ? AND expected_date <= ?
              UNION
              SELECT issuer_id, event_date AS d FROM earnings_announcements
              WHERE event_date > ? AND event_date <= ? AND knowledge_date <= ? AND issuer_id IS NOT NULL)
            SELECT DISTINCT s.security_id, ev.d AS date
            FROM ev JOIN securities_master s ON s.issuer_id = ev.issuer_id
            WHERE s.security_id IN (SELECT security_id FROM _cf_ids)""", [as_of, hi, as_of, hi, as_of]).pl()
    except Exception as e:  # noqa: BLE001 - calendar tables optional
        log.warning("upcoming earnings unavailable: %s", e)
        df = pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date})
    finally:
        con.close()
    return df.with_columns(pl.col("date").cast(pl.Date))


def flag_matrix(dates: np.ndarray, ids: list[str], ev: pl.DataFrame) -> np.ndarray:
    """(T, N) bool: True on the first calendar session >= each event date."""
    E = np.zeros((len(dates), len(ids)), dtype=bool)
    if ev.is_empty():
        return E
    ci = {s: i for i, s in enumerate(ids)}
    d = np.array(ev["date"].to_list(), dtype="datetime64[D]")
    k = np.searchsorted(dates, d, side="left")
    c = np.array([ci.get(s, -1) for s in ev["security_id"].to_list()])
    ok = (c >= 0) & (k < len(dates)) & (k >= 0)
    # only keep events whose session is within 3 calendar days of the event date (else outside the panel calendar)
    ok &= (dates[np.minimum(k, len(dates) - 1)] - d) <= np.timedelta64(4, "D")
    E[k[ok], c[ok]] = True
    return E


def vix_daily(start: date, end: date, settings: Settings | None = None) -> pl.DataFrame:
    """[date, vix] official FRED VIXCLS."""
    con = lake(["macro_observations"], settings)
    try:
        df = con.execute("""SELECT date, value AS vix FROM macro_observations WHERE series_id = 'fred:VIXCLS'
                            AND date BETWEEN ? AND ? AND value IS NOT NULL""", [start, end]).pl()
    finally:
        con.close()
    if df.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "vix": pl.Float64})
    return df.with_columns(pl.col("date").cast(pl.Date)).group_by("date").agg(pl.col("vix").last()).sort("date")


def macro_states(settings: Settings | None = None) -> pl.DataFrame:
    """Monthly W4 regime states [regime_type, scope, month, state] (macro_factors, model-based where flagged)."""
    from app.macro.markets import factors as fx  # noqa: PLC0415

    try:
        fac = fx.load_factors(settings or get_settings(), "M")
        st = fx.states_long(fac)
    except Exception as e:  # noqa: BLE001
        log.warning("macro states unavailable: %s", e)
        return pl.DataFrame(schema={"regime_type": pl.Utf8, "scope": pl.Utf8, "month": pl.Date, "state": pl.Utf8})
    return st.select("regime_type", "scope", pl.col("date").cast(pl.Date).dt.truncate("1mo").alias("month"),
                     pl.col("state").cast(pl.Utf8))
