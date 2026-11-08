"""Hot store of the live news engine (roadmap v3 N2 / N3): small parquet files written by the realtime service and
folded into the lake by the hourly / daily jobs. Never raw GDELT files: parsed, labelled frames only.

    data/realtime/gdelt_hot/<YYYYMMDD>/<HHMMSS>.<kind>.parquet       events_v2 parse output of one 15-min file
    data/realtime/gdelt_hot/<YYYYMMDD>/<HHMMSS>.<kind>.legacy*.parquet  pre-v2 outputs from the same bytes
    data/realtime/gdelt_hot/<YYYYMMDD>/<HHMMSS>.json                 slot manifest {kind: {status, released, rows}}
    data/realtime/news_hot/<YYYYMMDD>/items_h<HH>.parquet            news.item rows first seen in that UTC hour
    data/realtime/news_hot/<YYYYMMDD>/assign_h<HH>.parquet           url_hash -> live story_id of that hour
    data/realtime/news_hot/<YYYYMMDD>/fold_h<HH>/*.parquet            outputs of the hourly fold (job news_hourly)

kind = export | mentions | gkg | translation.export | translation.mentions | translation.gkg; status ok (parsed) or
missing (GDELT answered 404/403 after the slot was due: the day has a hole there, like the nightly records it).
Writes are atomic (tmp + replace, retried while a reader holds the file on Windows). The GDELT slot store is the
cache that keeps the nightly `gdelt_stream`, the deep backfill and the gap filler from downloading a slot twice
(app.events_v2.pipeline.Streamer.fetch_day reads it first).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger(__name__)

GDELT_KINDS = ("export", "mentions", "gkg", "translation.export", "translation.mentions", "translation.gkg")


def realtime_dir(settings: Settings | None = None) -> Path:
    return (settings or get_settings()).data_dir / "realtime"


def gdelt_hot_dir(settings: Settings | None = None) -> Path:
    return realtime_dir(settings) / "gdelt_hot"


def news_hot_dir(settings: Settings | None = None) -> Path:
    return realtime_dir(settings) / "news_hot"


def stamp(slot: datetime) -> str:
    return slot.astimezone(UTC).strftime("%Y%m%d%H%M%S")


def slot_of(st: str) -> datetime:
    return datetime.strptime(st, "%Y%m%d%H%M%S").replace(tzinfo=UTC)


def day_dir(root: Path, d: date) -> Path:
    return root / f"{d:%Y%m%d}"


# ------------------------------------------------------------------------------------------------ atomic IO
def write_parquet_atomic(df: pl.DataFrame, path: Path, level: int = 3, attempts: int = 20) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{uuid.uuid4().hex[:8]}-{path.name}")
    df.write_parquet(tmp, compression="zstd", compression_level=level)
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return path.stat().st_size
        except PermissionError:  # Windows: a reader has the target open for a moment
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"could not replace {path}")


def write_json_atomic(obj: Any, path: Path, attempts: int = 20) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{uuid.uuid4().hex[:8]}-{path.name}")
    tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (i + 1))
    tmp.unlink(missing_ok=True)


def read_parquet(path: Path, attempts: int = 5) -> pl.DataFrame | None:
    for i in range(attempts):
        try:
            return pl.read_parquet(path)
        except FileNotFoundError:
            return None
        except (PermissionError, OSError) as e:
            if i == attempts - 1:
                log.warning("hot store: cannot read %s: %s", path, e)
                return None
            time.sleep(0.1 * (i + 1))
    return None


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------------------------------------ GDELT slots
def slot_paths(slot: datetime, kind: str, settings: Settings | None = None) -> dict[str, Path]:
    base = day_dir(gdelt_hot_dir(settings), slot.date()) / f"{stamp(slot)}.{kind}"
    return {"v2": Path(f"{base}.parquet"), "legacy_rows": Path(f"{base}.legacy_rows.parquet"),
            "legacy_agg": Path(f"{base}.legacy_agg.parquet"), "legacy": Path(f"{base}.legacy.parquet")}


def manifest_path(slot: datetime, settings: Settings | None = None) -> Path:
    return day_dir(gdelt_hot_dir(settings), slot.date()) / f"{stamp(slot)}.json"


def read_manifest(slot: datetime, settings: Settings | None = None) -> dict[str, Any]:
    return read_json(manifest_path(slot, settings))


def write_slot_kind(slot: datetime, kind: str, res: pl.DataFrame | None, old: Any, status: str = "ok",
                    released: datetime | None = None, settings: Settings | None = None,
                    stats: dict[str, Any] | None = None, legacy_done: bool = True) -> dict[str, Any]:
    """Persist one parsed 15-min file (or its confirmed absence) and update the slot manifest. legacy_done: the
    file was parsed with legacy=True (its pre-v2 output, possibly none, is stored)."""
    p = slot_paths(slot, kind, settings)
    rec: dict[str, Any] = {"status": status, "released": released.isoformat() if released else None,
                           "written_at": datetime.now(UTC).isoformat(timespec="seconds"), "rows": 0,
                           "legacy_done": bool(legacy_done)}
    if status == "ok" and res is not None:
        write_parquet_atomic(res, p["v2"])
        rec["rows"] = res.height
        if isinstance(old, tuple):          # export: (gdelt_events rows, country-daily aggregates)
            rows, agg = old
            write_parquet_atomic(rows, p["legacy_rows"])
            write_parquet_atomic(agg, p["legacy_agg"])
        elif isinstance(old, pl.DataFrame) and old.height:
            write_parquet_atomic(old, p["legacy"])
    if stats:
        rec["stats"] = {k: v for k, v in stats.items() if isinstance(v, (int, float))}
    mp = manifest_path(slot, settings)
    man = read_json(mp)
    man[kind] = rec
    write_json_atomic(man, mp)
    return rec


def load_slot_kind(slot: datetime, kind: str, legacy: bool, settings: Settings | None = None)         -> tuple[str, pl.DataFrame | None, Any] | None:
    """(status, v2 frame, legacy object or None) from the hot store, or None when the slot / kind is not cached (or
    the legacy output is needed but the file was not parsed with legacy=True)."""
    rec = read_manifest(slot, settings).get(kind)
    if not rec:
        return None
    if rec.get("status") == "missing":
        return "missing", None, None
    if legacy and not rec.get("legacy_done"):
        return None
    p = slot_paths(slot, kind, settings)
    res = read_parquet(p["v2"])
    if res is None:
        return None
    old: Any = None
    if legacy:
        if kind == "export" and p["legacy_rows"].exists():
            rows, agg = read_parquet(p["legacy_rows"]), read_parquet(p["legacy_agg"])
            if rows is None or agg is None:
                return None
            old = (rows, agg)
        elif kind.endswith("gkg") and p["legacy"].exists():
            old = read_parquet(p["legacy"])
            if old is None:
                return None
    return "ok", res, old


def day_slots(d: date, settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """{stamp: manifest} of the hot slots of UTC day d."""
    dd = day_dir(gdelt_hot_dir(settings), d)
    if not dd.exists():
        return {}
    return {f.stem: read_json(f) for f in sorted(dd.glob("*.json")) if not f.name.startswith(".")}


def slot_complete(man: dict[str, Any], kinds: tuple[str, ...] = GDELT_KINDS) -> bool:
    return all((man.get(k) or {}).get("status") in ("ok", "missing") for k in kinds)


# ------------------------------------------------------------------------------------------------ news hours
def items_path(d: date, hour: int, settings: Settings | None = None) -> Path:
    return day_dir(news_hot_dir(settings), d) / f"items_h{hour:02d}.parquet"


def assign_path(d: date, hour: int, settings: Settings | None = None) -> Path:
    return day_dir(news_hot_dir(settings), d) / f"assign_h{hour:02d}.parquet"


def fold_dir(d: date, hour: int, settings: Settings | None = None) -> Path:
    return day_dir(news_hot_dir(settings), d) / f"fold_h{hour:02d}"


def hot_days(root: Path) -> list[date]:
    if not root.exists():
        return []
    out = []
    for p in root.iterdir():
        if p.is_dir() and len(p.name) == 8 and p.name.isdigit():
            out.append(date(int(p.name[:4]), int(p.name[4:6]), int(p.name[6:])))
    return sorted(out)


def prune(root: Path, keep_after: date, protect: set[date] | None = None) -> list[str]:
    """Delete day directories older than keep_after (except protected days). Returns the removed day names."""
    gone = []
    for d in hot_days(root):
        if d < keep_after and d not in (protect or set()):
            shutil.rmtree(day_dir(root, d), ignore_errors=True)
            gone.append(d.isoformat())
    return gone


def hour_floor(ts: datetime) -> datetime:
    ts = ts.astimezone(UTC)
    return ts.replace(minute=0, second=0, microsecond=0)


def hours_between(a: datetime, b: datetime) -> list[datetime]:
    out = []
    t = hour_floor(a)
    while t <= b:
        out.append(t)
        t += timedelta(hours=1)
    return out
