"""Price bars across the full history: daily bars (serving `prices`, last 15 years) + weekly bars before that
(serving `prices_w`, built by app.api.serving). Used by /companies/{id}/prices and /instruments/{id}.

interval D: daily bars where they exist, the weekly compaction before (each bar says which: `daily_from`);
W / M: aggregated (first open, max high, min low, last close, sum volume) across both tables.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from app.api.deps import Db

#: payload cap: daily bars beyond this span are resampled weekly (ARCHITECTURE §12; raised from 5 y with prices_w)
MAX_DAILY_DAYS = 365 * 10 + 7

_HAS_W: dict[str, bool] = {}


def has_weekly(db: Db) -> bool:
    key = db.snap.run_id
    if key not in _HAS_W:
        try:
            db.columns("prices_w")
            _HAS_W[key] = True
        except Exception:
            _HAS_W[key] = False
    return _HAS_W[key]


def first_date(db: Db, sid: str) -> date | None:
    if has_weekly(db):
        d = db.scalar("SELECT min(date) FROM prices_w WHERE security_id = ?", [sid])
        if d is not None:
            return d
    return db.scalar("SELECT min(date) FROM prices WHERE security_id = ?", [sid])


def daily_from(db: Db, sid: str) -> date | None:
    return db.scalar("SELECT min(date) FROM prices WHERE security_id = ?", [sid])


def bars(db: Db, sid: str, start: date, end: date, interval: Literal["D", "W", "M"], adjusted: bool) -> list[dict[str, Any]]:
    """Rows {date, o, h, l, c, v, currency, n} ordered by date (n = trading days aggregated, 1 for daily bars)."""
    f = "adj_close / NULLIF(close, 0)" if adjusted else "1.0"
    c = "adj_close" if adjusted else "close"
    daily = (f"SELECT date, open * {f} AS o, high * {f} AS h, low * {f} AS l, {c} AS c, volume AS v, currency, 1 AS n "
             "FROM prices WHERE security_id = ? AND date BETWEEN ? AND ?")
    params: list[Any] = [sid, start, end]
    base = daily
    if has_weekly(db):
        wk = ("SELECT date, adj_open AS o, adj_high AS h, adj_low AS l, adj_close AS c, volume AS v, currency, n_days AS n"
              if adjusted else "SELECT date, open AS o, high AS h, low AS l, close AS c, volume AS v, currency, n_days AS n")
        base = f"{daily} UNION ALL {wk} FROM prices_w WHERE security_id = ? AND date BETWEEN ? AND ?"
        params += [sid, start, end]
    if interval == "D":
        return db.all(f"SELECT * FROM ({base}) ORDER BY date", params)
    unit = "week" if interval == "W" else "month"
    return db.all(f"""SELECT max(date) AS date, arg_min(o, date) AS o, max(h) AS h, min(l) AS l, arg_max(c, date) AS c,
                             sum(v) AS v, arg_max(currency, date) AS currency, sum(n)::INTEGER AS n
                      FROM ({base}) GROUP BY date_trunc('{unit}', date) ORDER BY 1""", params)
