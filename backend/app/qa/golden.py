"""Golden data checks: key stable / bounded facts read through the app's own API (golden.yaml next to this module)."""

from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from app.qa.common import ApiClient, parse_date, utc_now

GOLDEN_FILE = Path(__file__).with_name("golden.yaml")


def load_checks(path: Path | None = None) -> list[dict[str, Any]]:
    return list((yaml.safe_load((path or GOLDEN_FILE).read_text(encoding="utf-8")) or {}).get("checks") or [])


def dotted(o: Any, field: str) -> Any:
    for part in field.split("."):
        if not isinstance(o, dict):
            return None
        o = o.get(part)
    return o


class _Ctx:
    def __init__(self, client: ApiClient):
        self.client = client
        self.ids: dict[str, str | None] = {}
        self.docs: dict[str, Any] = {}

    def sid(self, ticker: str) -> str | None:
        if ticker not in self.ids:
            r = self.client.get("/api/v1/search", {"q": ticker, "limit": 5})
            rows = r.json if r.status == 200 and isinstance(r.json, list) else []
            hit = next((x for x in rows if str(x.get("ticker", "")).upper() == ticker.upper()), None)
            self.ids[ticker] = hit.get("security_id") if hit else None
        return self.ids[ticker]

    def doc(self, path: str, params: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        key = f"{path}?{sorted((params or {}).items())}"
        if key not in self.docs:
            r = self.client.get(path, params)
            self.docs[key] = (r.json, None) if r.status == 200 else (None, f"HTTP {r.status or r.error}")
        return self.docs[key]


def _observe(c: dict[str, Any], ctx: _Ctx) -> tuple[float | None, date | None, str | None]:
    """(value, observation date, error)."""
    kind = c["kind"]
    if kind in ("fin_line", "overview"):
        sid = ctx.sid(c["ticker"])
        if sid is None:
            return None, None, f"ticker {c['ticker']} not found by search"
        if kind == "overview":
            d, err = ctx.doc(f"/api/v1/companies/{sid}/overview")
            return (None, None, err) if err else (_num(dotted(d, c["field"])), parse_date(dotted(d, "as_of")), None)
        d, err = ctx.doc(f"/api/v1/companies/{sid}/financials",
                         {"statement": c.get("statement", "IS"), "period": c.get("period", "FY"), "limit": 12})
        if err:
            return None, None, err
        periods = d.get("periods") or []
        idx = next((i for i, p in enumerate(periods) if p.get("fiscal_year") == c["fiscal_year"]
                    and (p.get("fiscal_period") or "FY") == c.get("period", "FY")), None)
        line = next((ln for ln in d.get("lines") or [] if ln.get("item_id") == c["item"]), None)
        if idx is None or line is None:
            return None, None, f"no {c['item']} for FY{c['fiscal_year']}"
        vals = line.get("values") or []
        return (_num(vals[idx]) if idx < len(vals) else None), parse_date(periods[idx].get("period_end")), None
    if kind == "index_last":
        d, err = ctx.doc("/api/v1/market/overview", {"region": c["region"]} if c.get("region") else None)
        if err:
            return None, None, err
        row = next((x for x in d.get("indices") or [] if x.get("id") == c["index"]), None)
        if row is None:
            return None, None, f"index {c['index']} not in /market/overview"
        return _num(row.get("last")), parse_date(row.get("as_of")) or parse_date(d.get("as_of")), None
    if kind in ("macro_last", "macro_yoy"):
        d, err = ctx.doc(f"/api/v1/macro/series/{c['series_id']}")
        if err:
            return None, None, err
        pts = [(parse_date(p.get("date")), _num(p.get("value"))) for p in d.get("points") or []]
        pts = [(dt, v) for dt, v in pts if dt and v is not None]
        if not pts:
            return None, None, "no observations"
        last_d, last_v = pts[-1]
        if kind == "macro_last":
            return last_v, last_d, None
        base = next((v for dt, v in reversed(pts) if (dt.year, dt.month) == (last_d.year - 1, last_d.month)), None)
        if not base:
            return None, last_d, "no observation 12 months earlier"
        return last_v / base - 1.0, last_d, None
    return None, None, f"unknown kind {kind!r}"


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def evaluate(c: dict[str, Any], value: float | None, obs_date: date | None, today: date) -> tuple[str, str]:
    """(status passed | failed | warn, message) for one observed value against its expectation."""
    sev_bad = "warn" if c.get("severity") == "warn" else "failed"
    e = c.get("expect") or {}
    if value is None:
        return sev_bad, "no value"
    if "value" in e:
        exp = float(e["value"])
        tol = float(e.get("rel_tol", 0.01))
        dev = abs(value - exp) / abs(exp) if exp else abs(value)
        ok = dev <= tol
        msg = f"{value:,.6g} vs {exp:,.6g} (dev {dev:.3%}, tol {tol:.2%})"
    else:
        lo, hi = float(e.get("min", -math.inf)), float(e.get("max", math.inf))
        ok = lo <= value <= hi
        msg = f"{value:,.6g} in [{lo:,.6g}, {hi:,.6g}]" if ok else f"{value:,.6g} outside [{lo:,.6g}, {hi:,.6g}]"
    if not ok:
        return sev_bad, msg
    if c.get("max_age_days") and obs_date and (today - obs_date).days > int(c["max_age_days"]):
        return "warn", f"{msg}; observation {obs_date} older than {c['max_age_days']} days"
    return "passed", msg


def run_golden(client: ApiClient, checks: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    t0 = utc_now()
    today = t0.date()
    ctx = _Ctx(client)
    rows = []
    for c in checks if checks is not None else load_checks():
        try:
            value, obs, err = _observe(c, ctx)
        except Exception as e:  # noqa: BLE001 - one broken check never stops the suite
            value, obs, err = None, None, f"{type(e).__name__}: {e}"[:200]
        status, msg = evaluate(c, value, obs, today)
        if err:
            msg = f"{err}" if value is None else f"{msg} ({err})"
        rows.append({"id": c["id"], "description": c.get("description"), "status": status, "value": value,
                     "observation_date": obs.isoformat() if obs else None, "expect": c.get("expect"),
                     "severity": c.get("severity", "error"), "message": msg})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    status = "failed" if counts.get("failed") else "warn" if counts.get("warn") else "passed"
    return {"suite": "golden", "status": status, "started_at": t0.isoformat(), "finished_at": utc_now().isoformat(),
            "checks": len(rows), "counts": counts, "failed": counts.get("failed", 0), "results": rows}
