"""Shared helpers for app.linkage: paths, resumable state, HTTP clients, ISO country code helpers."""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings

log = logging.getLogger("app.linkage")

#: aggregates / pseudo-codes that must never be treated as countries
NOT_COUNTRIES = {"EU", "EA", "EZ", "UN", "XX", "ZZ", "W0", "W00", "1W", "5J", "5A", "5R"}


def settings_of(settings: Settings | None = None) -> Settings:
    return settings or get_settings()


def raw_dir(name: str, settings: Settings | None = None) -> Path:
    d = settings_of(settings).raw_dir / "linkage" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def tmp_dir(name: str, settings: Settings | None = None) -> Path:
    d = settings_of(settings).data_dir / "tmp" / "linkage" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path(name: str, settings: Settings | None = None) -> Path:
    s = settings_of(settings)
    s.state_dir.mkdir(parents=True, exist_ok=True)
    return s.state_dir / f"linkage_{name}.json"


def load_state(name: str, settings: Settings | None = None) -> dict[str, Any]:
    p = state_path(name, settings)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_state(name: str, state: dict[str, Any], settings: Settings | None = None) -> None:
    p = state_path(name, settings)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def now_utc() -> datetime:
    return datetime.now(UTC)


def today() -> date:
    return now_utc().date()


def http(source_id: str, settings: Settings | None = None, **kw: Any):
    from app.core.http import HttpClient  # noqa: PLC0415

    return HttpClient(source_id, settings=settings_of(settings), **kw)


def is_iso2(code: Any) -> bool:
    return isinstance(code, str) and len(code) == 2 and code.isalpha() and code.isupper() and code not in NOT_COUNTRIES


@lru_cache(maxsize=1)
def _iso3_static() -> dict[str, str]:
    from app.ingestion.gdelt_events import ISO3_TO_ISO2  # noqa: PLC0415

    return dict(ISO3_TO_ISO2)


def iso3_to_iso2_map(settings: Settings | None = None) -> dict[str, str]:
    """ISO3 -> ISO2 from geo_countries (Natural Earth) when built, else the static GDELT table; plus quirks."""
    out = dict(_iso3_static())
    try:
        from app.core.storage import read_table  # noqa: PLC0415

        g = read_table("geo_countries", columns=["iso2", "iso3"], settings=settings)
        out.update({a: b for b, a in g.select("iso2", "iso3").iter_rows() if a and b})
    except Exception:  # noqa: BLE001 - table not built yet
        pass
    out.update({"XKX": "XK", "KOS": "XK", "UVK": "XK", "TWN": "TW", "ROM": "RO", "ZAR": "CD", "TMP": "TL"})
    return out


def write(df: pl.DataFrame, table: str, settings: Settings | None = None, mode: str | None = None) -> int:
    from app.core.schema import conform  # noqa: PLC0415
    from app.core.storage import write_table  # noqa: PLC0415

    if df.is_empty():
        return 0
    return write_table(conform(df, table), table, mode=mode, settings=settings_of(settings))


def update_progress(item: str, status: str, note: str | None = None, settings: Settings | None = None,
                    **extra: Any) -> None:
    """data/state/linkage.json: per plan item status + one-line notes (resume protocol)."""
    p = settings_of(settings).state_dir / "linkage.json"
    try:
        st = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        st = {}
    st.setdefault("items", {})[item] = status
    if note:
        st.setdefault("item_notes", {})[item] = note
    if extra:
        st.setdefault("runs", {})[item] = {**extra, "at": now_utc().isoformat(timespec="seconds")}
    st["updated"] = now_utc().isoformat(timespec="seconds")
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)
