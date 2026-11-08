"""Shared helpers for the central-bank monitor: official-source HTTP, lake readers, small utilities."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)
UTC = timezone.utc
STATE_FILE = "macro_w3.json"

# Rate limits: ~1 request/s per official host (the jobs make a few dozen requests per night).
_CLIENTS: dict[str, Any] = {}


def contact(settings: Settings | None = None) -> str:
    s = settings or get_settings()
    try:
        return s.sec_user_agent.split()[-1]
    except Exception:  # noqa: BLE001
        return "admin@example.com"


def client(source_id: str, settings: Settings | None = None, browser: bool = False, timeout: float = 90):
    """HttpClient for an official source. Descriptive UA with contact (BLS/RBA/Akamai hosts reject browser UAs);
    ``browser=True`` for hosts that only answer browser-like UAs (BoE IADB, chinamoney)."""
    from app.core.http import HttpClient  # noqa: PLC0415
    from app.core.ratelimit import RateLimit  # noqa: PLC0415

    s = settings or get_settings()
    key = f"{source_id}|{browser}"
    if key not in _CLIENTS:
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) BiG-Equity-360 research" if browser
              else f"BiG Equity 360 research {contact(s)}")
        _CLIENTS[key] = HttpClient(f"cb_{source_id}", settings=s, rate_limit=RateLimit(1.0, 2),
                                   headers={"User-Agent": ua, "Accept": "*/*"}, max_retries=2, timeout=timeout)
    return _CLIENTS[key]


def http_get(source_id: str, url: str, params: dict[str, Any] | None = None, ttl: int = 6 * 3600,
             settings: Settings | None = None, browser: bool = False, timeout: float = 90) -> bytes:
    return client(source_id, settings, browser, timeout).get(url, params=params, cache_ttl=ttl).content


def http_post(source_id: str, url: str, settings: Settings | None = None, browser: bool = True,
              data: Any = None) -> bytes:
    return client(source_id, settings, browser).post(url, data=data).content


def to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if s in ("", ".", "NA", "N/A", "-", "--", "#N/A", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ------------------------------------------------------------------------------------------------- lake readers

def lake_series(series_ids: Iterable[str], settings: Settings | None = None,
                start: date | None = None) -> dict[str, list[tuple[date, float]]]:
    """Latest-revised observations per series from macro_observations (max knowledge_date per date)."""
    from app.core.storage import scan_table  # noqa: PLC0415

    ids = sorted(set(series_ids))
    if not ids:
        return {}
    try:
        lf = scan_table("macro_observations", settings).filter(pl.col("series_id").is_in(ids))
        if start:
            lf = lf.filter(pl.col("date") >= start)
        df = (lf.select("series_id", "date", "value", "knowledge_date").drop_nulls("value")
              .sort("knowledge_date").group_by(["series_id", "date"]).agg(pl.col("value").last())
              .sort("series_id", "date").collect())
    except Exception as e:  # noqa: BLE001
        log.warning("cb: lake read failed: %s", e)
        return {}
    out: dict[str, list[tuple[date, float]]] = {}
    for sid, d, v in df.iter_rows():
        out.setdefault(sid, []).append((d, float(v)))
    return out


def read_table_safe(table: str, settings: Settings | None = None, **kw: Any) -> pl.DataFrame | None:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        return read_table(table, settings=settings, **kw)
    except Exception:  # noqa: BLE001
        return None


def write(df: pl.DataFrame, table: str, settings: Settings | None = None, mode: str | None = None) -> int:
    from app.core.storage import write_table  # noqa: PLC0415

    return write_table(df, table, mode=mode, settings=settings)


def now_utc() -> datetime:
    return datetime.now(UTC)


def jdump(o: Any) -> str:
    return json.dumps(o, default=str, separators=(",", ":"))


# ------------------------------------------------------------------------------------------------- step series

def step_value(pairs: list[tuple[date, float]], d: date) -> float | None:
    """Value of a step series (sorted (date, value)) in force on d (last obs <= d)."""
    lo, hi = 0, len(pairs)
    while lo < hi:
        mid = (lo + hi) // 2
        if pairs[mid][0] <= d:
            lo = mid + 1
        else:
            hi = mid
    return pairs[lo - 1][1] if lo else None


def business_days(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


# ------------------------------------------------------------------------------------------------- state file

def update_state(patch: dict[str, Any], settings: Settings | None = None) -> None:
    s = settings or get_settings()
    p = Path(s.state_dir) / STATE_FILE
    try:
        cur = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        cur = {}
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(cur.get(k), dict):
            cur[k].update(v)
        else:
            cur[k] = v
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur, indent=1, default=str), encoding="utf-8")
