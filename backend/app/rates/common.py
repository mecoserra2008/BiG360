"""Shared helpers for app.rates: integer bp x 100 conversion, polite official-source HTTP, the state file, frames."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.rates")
STATE_FILE = "rates.json"
_CLIENTS: dict[str, Any] = {}

#: per-host politeness (requests / second, burst); every host gets <= 1 request per second
RATE = {"default": (1.0, 1), "openfigi": (5 / 60.0, 1), "fiscaldata": (2.0, 2), "firds": (0.5, 1),
        "riksbank": (0.08, 1)}

CURVE_SCHEMA = {"date": pl.Date, "curve_id": pl.Utf8, "tenor_months": pl.Int16, "type": pl.Utf8, "kind": pl.Utf8,
                "value_bp_x100": pl.Int32, "source": pl.Utf8}


def to_bp100(pct: float | None) -> int | None:
    """Percent -> integer basis points x 100 (4.2512 -> 42512); None / NaN / inf -> None."""
    if pct is None:
        return None
    try:
        f = float(pct)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or abs(f) > 200000:
        return None
    return int(round(f * 10000))


def from_bp100(v: int | None) -> float | None:
    return None if v is None else v / 10000.0


def contact(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    try:
        return s.sec_user_agent.split()[-1]
    except Exception:  # noqa: BLE001
        return "research"


def client(source_id: str, settings: Settings | None = None, browser: bool = False, timeout: float = 120,
           headers: dict[str, str] | None = None):
    """HttpClient per official source: descriptive User-Agent with contact (browser-like only for hosts that answer
    nothing else, e.g. BoE media files), <= 1 request / second, retries with back-off, on-disk cache."""
    from app.core.http import HttpClient  # noqa: PLC0415
    from app.core.ratelimit import RateLimit  # noqa: PLC0415

    s = settings or get_settings()
    key = f"{source_id}|{browser}"
    if key not in _CLIENTS:
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) BiG-Equity-360 research" if browser
              else f"BiG Equity 360 research {contact(s)}")
        per_s, burst = RATE.get(source_id, RATE["default"])
        _CLIENTS[key] = HttpClient(f"rates_{source_id}", settings=s, rate_limit=RateLimit(per_s, burst),
                                   headers={"User-Agent": ua, "Accept": "*/*", **(headers or {})}, max_retries=3,
                                   timeout=timeout)
    return _CLIENTS[key]


def http_get(source_id: str, url: str, params: dict[str, Any] | None = None, ttl: int = 6 * 3600,
             settings: Settings | None = None, browser: bool = False, timeout: float = 120) -> bytes:
    return client(source_id, settings, browser, timeout).get(url, params=params, cache_ttl=ttl).content


def http_post_json(source_id: str, url: str, body: Any, settings: Settings | None = None,
                   headers: dict[str, str] | None = None) -> Any:
    c = client(source_id, settings, headers={"Content-Type": "application/json", **(headers or {})})
    return c.post(url, json_body=body).json()


def now_utc() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------------------------------ state


def state_path(settings: Settings | None = None) -> Path:
    return Path((settings or get_settings()).state_dir) / STATE_FILE


def read_state(settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return {}


def update_state(patch: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    """Deep-merge one level: dict values merge into existing dicts."""
    cur = read_state(settings)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k].update(v)
        else:
            cur[k] = v
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)
    return cur


# ------------------------------------------------------------------------------------------------ frames


def curve_rows(curve_id: str, kind: str, source: str, typ: str,
               points: Iterable[tuple[date, int, float | None]]) -> list[dict[str, Any]]:
    """(date, tenor_months, percent) -> yield_curves rows (nulls dropped)."""
    out = []
    for d, m, v in points:
        bp = to_bp100(v)
        if bp is None or d is None:
            continue
        out.append({"date": d, "curve_id": curve_id, "tenor_months": int(m), "type": typ, "kind": kind,
                    "value_bp_x100": bp, "source": source})
    return out


def rows_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=CURVE_SCHEMA)
    return pl.DataFrame(rows, schema=CURVE_SCHEMA)


def tenor_months_from_label(lab: str) -> int | None:
    """'1M' / '3 Mo' / '10Y' / '10 Yr' / '0.25' (years) -> months."""
    s = str(lab).strip().upper().replace(" ", "")
    for suf, mult in (("MONTHS", 1), ("MONTH", 1), ("MO", 1), ("M", 1), ("YEARS", 12), ("YEAR", 12), ("YR", 12),
                      ("Y", 12), ("J", 12)):
        if s.endswith(suf):
            try:
                v = float(s[: -len(suf)])
            except ValueError:
                return None
            return int(round(v * mult))
    try:
        return int(round(float(s) * 12))
    except ValueError:
        return None
