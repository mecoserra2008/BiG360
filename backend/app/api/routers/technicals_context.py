"""Technicals v2 T5 (docs/TECHNICALS_V2_PLAN.md; contract v1.33.0, tag company): volatility / regime / earnings context
chips for the Company 360 Technicals tab.

  GET /technicals/{security_id}/context   calibrated 20d range-width percentile vs the stock's own last 2 years
                                          (squeeze / normal / expansion), realised vol 20d + percentile, implied vol
                                          (n/a: no per-stock source), ATR(14) percentile, ADX(14), daily / weekly trend
                                          alignment, next earnings (sessions ahead, event pool) + typical earnings move
                                          (median |CAR[-1,+1]|)
Computed at query time from the serving snapshot (prices, cf_envelopes, earn_events, earn_calendar) by
app.technicals.context. Range width is MODEL-BASED (calibrated band); everything is descriptive, not a signal.
"""

from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any

import duckdb
import numpy as np
from fastapi import APIRouter, Depends

from app.api import pricebars
from app.api.deps import Db, get_db
from app.api.routers._common import iso, not_found, sec_ref, sec_ref_sql
from app.conformal import serve as S
from app.core.config import get_settings
from app.technicals import context as C

router = APIRouter(tags=["company"])

HISTORY_DAYS = 1250            # calendar days of bars: 504-session lookback + EWMA / SMA / weekly warm-up
EARN_WINDOW = 20               # sessions: the calibrated range's longest horizon


def _rows(db: Db, sql: str, params: list[Any]) -> list[dict[str, Any]]:
    try:
        return db.all(sql, params)
    except duckdb.CatalogException:
        return []


def _f(v: Any) -> float | None:
    return S.f(v)


@router.get("/technicals/{security_id}/context", operation_id="getTechnicalsContext")
def get_technicals_context(security_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    sr = db.one(f"SELECT {sec_ref_sql('s')}, s.px_date AS _px_date, s.mic AS _mic, s.issuer_id AS _issuer "
                "FROM sec s WHERE s.security_id = ?", [security_id])
    if not sr:
        raise not_found(f"unknown security_id {security_id!r}")
    as_of: date = sr.get("_px_date") or db.as_of
    out: dict[str, Any] = {
        "security": sec_ref(sr), "as_of": iso(as_of), "last_close": None, "currency": sr.get("ref_currency") or None,
        "model_based": True, "lookback_sessions": C.LOOKBACK,
        "range_width": {"level": 90, "horizon": 20, "width_log": None, "width_pct": None, "percentile": None,
                        "state": "n/a", "n": 0, "radius": None, "median_width_log": None},
        "realised_vol": {"window": 20, "vol": None, "percentile": None, "state": "n/a", "n": 0},
        "implied_vol": {"available": False, "value": None, "source": None, "note": C.NOTES["implied_vol"]},
        "atr": {"window": 14, "atr": None, "atr_pct": None, "percentile": None, "state": "n/a", "n": 0},
        "adx": {"window": 14, "adx": None, "plus_di": None, "minus_di": None, "state": "n/a", "direction": None},
        "trend": {"daily": "n/a", "weekly": "n/a", "alignment": "n/a", "daily_close_vs_sma50": None,
                  "weekly_close_vs_sma30w": None},
        "earnings": {"next_date": None, "session": None, "date_source": None, "sessions_ahead": None, "in_window": False,
                     "event_pool": False, "typical_abs_move": None, "mean_abs_move": None, "n_events": 0,
                     "last_release": None},
        "notes": dict(C.NOTES), "data_status": "pending",
    }
    bars = [b for b in pricebars.bars(db, security_id, as_of - timedelta(days=HISTORY_DAYS), as_of, "D", True)
            if b.get("c") is not None and b["c"] > 0]
    if len(bars) < 30:
        out["data_status"] = "pending" if not bars else "partial"
        return out
    dates = [b["date"] for b in bars]
    c = np.array([float(b["c"]) for b in bars])
    h = np.array([float(b["h"] if b.get("h") is not None else b["c"]) for b in bars])
    lo = np.array([float(b["l"] if b.get("l") is not None else b["c"]) for b in bars])
    o = np.array([float(b["o"] if b.get("o") is not None else b["c"]) for b in bars])
    h, lo = np.fmax(h, c), np.fmin(lo, c)
    env = _rows(db, "SELECT * FROM cf_envelopes WHERE security_id = ?", [security_id])
    env = env[0] if env else None
    q90 = _f(env.get("qm_20_90")) if env else None
    iss = sr.get("_issuer")
    hist = _rows(db, """SELECT release_date, reaction_date, session, car_m1_p1 FROM earn_events
                        WHERE issuer_id = ? AND release_date <= ? ORDER BY release_date""", [iss, as_of]) if iss else []
    earn_days = [x["reaction_date"] or x["release_date"] for x in hist if x["release_date"] >= dates[0]]
    ctx = C.context(dates, o, h, lo, c, earn_days, q90)
    for k in ("range_width", "realised_vol", "atr", "adx", "trend"):
        out[k].update(ctx[k])
    out["last_close"] = round(float(c[-1]), 6)
    # earnings: next expected date, sessions ahead on the exchange calendar, typical move
    cars = [abs(float(x["car_m1_p1"])) for x in hist if _f(x.get("car_m1_p1")) is not None]
    e = out["earnings"]
    if cars:
        last = cars[-12:]
        e.update(typical_abs_move=round(statistics.median(last), 6), mean_abs_move=round(sum(last) / len(last), 6),
                 n_events=len(last), last_release=iso(hist[-1]["release_date"]))
    nxt = _rows(db, """SELECT expected_date, session, date_source FROM earn_calendar
                       WHERE issuer_id = ? AND expected_date > ? ORDER BY expected_date LIMIT 1""", [iss, as_of]) if iss else []
    if nxt:
        nd = nxt[0]["expected_date"]
        sess, _ = S.fan_sessions(as_of, 70, sr.get("_mic"), sr.get("ref_region"), get_settings())
        ahead = next((k + 1 for k, d in enumerate(sess) if d >= nd), None)
        e.update(next_date=iso(nd), session=nxt[0].get("session"), date_source=nxt[0].get("date_source"),
                 sessions_ahead=ahead, in_window=bool(ahead is not None and ahead <= EARN_WINDOW))
    if env is not None:
        e["event_pool"] = bool(env.get("earn_in_window"))
        if e["sessions_ahead"] is None and env.get("earn_days_ahead") is not None:
            e.update(sessions_ahead=int(env["earn_days_ahead"]), in_window=True)
    out["data_status"] = "ok" if out["range_width"]["percentile"] is not None else "partial"
    return out
