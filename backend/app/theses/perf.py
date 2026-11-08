"""Thesis performance since inception vs benchmark and sector, and hit-rate stats across closed theses (roadmap #8).

Prices come from the shared price reader (app.api.pricebars.bars, adjusted daily closes from the serving snapshot).
FX convention: serving prices carry close_usd = close x official usd_per_unit (fx_rates); a USD series is the adjusted
close times close_usd / close of the same day. Returns are compared in the local currency when the subject, benchmark
and sector benchmark trade in the same currency, else all in USD (basis = local | usd).
Thesis return = direction sign x subject return (long +1, short -1, neutral +1 reported raw); excess = sign x
(subject - benchmark).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from statistics import mean, median
from typing import Any

from app.api import pricebars

MAX_POINTS = 600
FLAT_BAND = 0.005


def _d(v: Any) -> date | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def sign(direction: str | None) -> int:
    return -1 if direction == "short" else 1


def load_series(db: Any, sid: str | None, start: date, end: date, usd: bool = False) -> tuple[list[tuple[date, float]], str | None]:
    """[(date, adjusted close [USD])] on or after the last bar <= start (so the inception day is anchored)."""
    if not sid:
        return [], None
    anchor = db.scalar("SELECT max(date) FROM prices WHERE security_id = ? AND date <= ?", [sid, start]) or start
    rows = pricebars.bars(db, sid, anchor, end, "D", adjusted=True)
    rows = [r for r in rows if r.get("c") is not None and r["c"] > 0]
    cur = rows[-1]["currency"] if rows else None
    if not usd:
        return [(r["date"], float(r["c"])) for r in rows], cur
    fx = {r["date"]: r["fx"] for r in db.all("""SELECT date, close_usd / NULLIF(close, 0) AS fx FROM prices
                                               WHERE security_id = ? AND date BETWEEN ? AND ?""", [sid, anchor, end])}
    out, last = [], None
    for r in rows:
        f = fx.get(r["date"]) or last
        if f:
            out.append((r["date"], float(r["c"]) * float(f)))
            last = f
    return out, "USD"


def _ret(s: list[tuple[date, float]]) -> float | None:
    return s[-1][1] / s[0][1] - 1 if len(s) >= 2 and s[0][1] else (0.0 if len(s) == 1 else None)


def _ffill(dates: list[date], s: list[tuple[date, float]]) -> list[float | None]:
    out, j, last = [], 0, None
    for d in dates:
        while j < len(s) and s[j][0] <= d:
            last = s[j][1]
            j += 1
        out.append(last)
    return out


def max_drawdown(path: list[float]) -> float | None:
    if not path:
        return None
    peak, mdd = path[0], 0.0
    for v in path:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def performance(db: Any, t: dict[str, Any], end: date | None = None) -> dict[str, Any]:
    start = _d(t.get("inception_date")) or _d(t.get("created_at")) or db.as_of
    end = end or (_d(t.get("closed_at")) if t.get("status") == "closed" else None) or db.as_of
    end = max(end, start)
    ids = {"subject": t.get("price_id"), "benchmark": t.get("benchmark_id"), "sector": t.get("sector_benchmark_id")}
    loc = {k: load_series(db, v, start, end) for k, v in ids.items()}
    curs = {c for _, c in loc.values() if c}
    basis = "local" if len(curs) <= 1 else "usd"
    ser = loc if basis == "local" else {k: load_series(db, v, start, end, usd=True) for k, v in ids.items()}
    s = ser["subject"][0]
    sg = sign(t.get("direction"))
    out: dict[str, Any] = {"basis": basis, "currency": (next(iter(curs)) if basis == "local" and curs else "USD"),
                           "start_date": start.isoformat(), "end_date": end.isoformat(), "days": (end - start).days,
                           "direction": t.get("direction"), "sign": sg, "subject": None, "benchmark": None,
                           "sector": None, "thesis_return": None, "excess_vs_benchmark": None, "excess_vs_sector": None,
                           "max_drawdown": None, "series": [], "data_status": "pending" if not s else "ok"}
    rets: dict[str, float | None] = {}
    for k in ("subject", "benchmark", "sector"):
        x = ser[k][0]
        if ids[k] and x:
            rets[k] = _ret(x)
            out[k] = {"id": ids[k], "return": rets[k], "start_date": x[0][0].isoformat(), "end_date": x[-1][0].isoformat(),
                      "start_value": x[0][1], "end_value": x[-1][1], "currency": ser[k][1]}
        elif ids[k]:
            out[k] = {"id": ids[k], "return": None, "start_date": None, "end_date": None, "start_value": None,
                      "end_value": None, "currency": None}
    if not s:
        return out
    r_s = rets.get("subject")
    out["thesis_return"] = None if r_s is None else sg * r_s
    if rets.get("benchmark") is not None and r_s is not None:
        out["excess_vs_benchmark"] = sg * (r_s - rets["benchmark"])  # type: ignore[operator]
    if rets.get("sector") is not None and r_s is not None:
        out["excess_vs_sector"] = sg * (r_s - rets["sector"])  # type: ignore[operator]
    dates = [d for d, _ in s]
    base = s[0][1]
    path = [1 + sg * (v / base - 1) for _, v in s]
    out["max_drawdown"] = max_drawdown(path)
    cols = {"subject": [v / base * 100 for _, v in s]}
    for k in ("benchmark", "sector"):
        x = ser[k][0]
        if x:
            ff = _ffill(dates, x)
            b0 = next((v for v in ff if v), None)
            cols[k] = [None if v is None or not b0 else v / b0 * 100 for v in ff]
    step = max(1, len(dates) // MAX_POINTS + (1 if len(dates) % MAX_POINTS else 0)) if len(dates) > MAX_POINTS else 1
    idx = list(range(0, len(dates), step))
    if idx[-1] != len(dates) - 1:
        idx.append(len(dates) - 1)
    out["series"] = [{"date": dates[i].isoformat(), "subject": round(cols["subject"][i], 4),
                      "benchmark": None if "benchmark" not in cols or cols["benchmark"][i] is None else round(cols["benchmark"][i], 4),
                      "sector": None if "sector" not in cols or cols["sector"][i] is None else round(cols["sector"][i], 4)}
                     for i in idx]
    return out


def perf_summary(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in ("basis", "currency", "start_date", "end_date", "days", "thesis_return",
                                  "excess_vs_benchmark", "excess_vs_sector", "max_drawdown")}


# ------------------------------------------------------------------------------------------------ hit rates


def derive_outcome(ret: float | None) -> str | None:
    if ret is None:
        return None
    return "win" if ret > FLAT_BAND else "loss" if ret < -FLAT_BAND else "flat"


def closed_result(t: dict[str, Any], snaps: list[dict[str, Any]]) -> dict[str, Any]:
    """Return / excess of a closed thesis: from its close snapshot (performance captured at close), else from the
    inception / exit prices."""
    close = next((s for s in reversed(snaps) if s["kind"] == "close"), None)
    p = ((close or {}).get("extra") or {}).get("performance") or {}
    ret = p.get("thesis_return")
    if ret is None and t.get("inception_price") and t.get("exit_price"):
        ret = sign(t.get("direction")) * (float(t["exit_price"]) / float(t["inception_price"]) - 1)
    days = p.get("days")
    if days is None and t.get("inception_date") and t.get("closed_at"):
        days = (_d(t["closed_at"]) - _d(t["inception_date"])).days  # type: ignore[operator]
    return {"thesis_return": ret, "excess_vs_benchmark": p.get("excess_vs_benchmark"),
            "excess_vs_sector": p.get("excess_vs_sector"), "days": days,
            "outcome": t.get("outcome") or derive_outcome(ret)}


def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rets = [r["thesis_return"] for r in rows if r["thesis_return"] is not None]
    exc = [r["excess_vs_benchmark"] for r in rows if r["excess_vs_benchmark"] is not None]
    w = sum(1 for r in rows if r["outcome"] == "win")
    lo = sum(1 for r in rows if r["outcome"] == "loss")
    return {"n": len(rows), "wins": w, "losses": lo, "flat": sum(1 for r in rows if r["outcome"] == "flat"),
            "hit_rate": (w / (w + lo)) if (w + lo) else None, "avg_return": mean(rets) if rets else None,
            "median_return": median(rets) if rets else None, "avg_excess": mean(exc) if exc else None}


def hit_stats(theses: list[dict[str, Any]], snaps: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    closed = []
    for t in theses:
        if t["status"] != "closed":
            continue
        closed.append({**closed_result(t, snaps.get(t["thesis_id"], [])), "direction": t["direction"],
                       "conviction": t["conviction"], "subject_type": t["subject_type"],
                       "close_reason": t.get("close_reason")})
    days = [r["days"] for r in closed if r["days"] is not None]

    def group(key: str) -> list[dict[str, Any]]:
        vals = sorted({str(r[key]) for r in closed if r[key] is not None})
        return [{"key": v, **_agg([r for r in closed if str(r[key]) == v])} for v in vals]

    n = len(closed)
    return {"n_total": len(theses), "n_idea": sum(t["status"] == "idea" for t in theses),
            "n_active": sum(t["status"] == "active" for t in theses), "n_closed": n,
            "auto_status": {s: sum(1 for t in theses if t["status"] != "closed" and (t.get("auto_status") or "unknown") == s)
                            for s in ("on_track", "at_risk", "invalidated", "unknown")},
            "overall": _agg(closed), "avg_days_held": mean(days) if days else None,
            "target_hit_rate": (sum(r["close_reason"] == "target_hit" for r in closed) / n) if n else None,
            "stopped_rate": (sum(r["close_reason"] in ("stopped", "invalidated") for r in closed) / n) if n else None,
            "by_direction": group("direction"), "by_conviction": group("conviction"),
            "by_subject_type": group("subject_type"), "by_close_reason": group("close_reason")}


def next_review(start: date, every_days: int | None) -> date | None:
    return start + timedelta(days=int(every_days)) if every_days else None
