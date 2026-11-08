"""Section-level incremental publish of the serving snapshot (docs/STARTUP_SYNC_PLAN.md A3).

The full build (app.api.serving.build_serving_db, ~25-30 min at ~25k securities) stays the weekly refresh's step.
The startup sync rebuilds only the builder sections whose inputs changed and hot-swaps the result:

1. **Section map** (captured, not hand-written): every builder section of app.api.serving.SECTIONS is executed once
   on an empty lake through a recording connection. From its SQL we read the lake tables it reads (`lake.<t>`), the
   snapshot tables it writes (CREATE / INSERT / DELETE / UPDATE `snap.<t>`) and the snapshot tables of *other*
   sections it reads. Two captures with different as_of dates tell which sections depend on the as_of date, and a
   hash of the SQL is the section's code fingerprint. Cached in data/cache/ops/serving_section_map.json per builder
   source hash.
2. **Dirty sections** vs the published snapshot: a lake input written after the snapshot's build started, an extra
   input (app_state model promotions, GPR validation reports) newer than that, a changed code fingerprint, or a new
   as_of for an as_of-dependent section. Dirtiness propagates to sections reading a dirty section's tables and to
   every co-writer of a table.
3. **Publish**: nothing dirty -> no publish. Dirty sections costing more than FULL_BUILD_SHARE of the last full
   build -> the regular full build (job build_serving_db). Otherwise copy the CURRENT file, open the copy read-write
   with the serving build budget (serving_build_connection), re-run only the dirty sections plus `meta` (_meta,
   _inputs, _freshness, serving_sections), checkpoint, verify, flip CURRENT (the API hot-swaps on the pointer
   change, as for a full build), clean up old snapshots. Runs as job `serving_sections_publish` in the exclusive
   heavy slot.

Equality: an incremental publish produces the same rows as a full build for every table (tests/ops/
test_sync_publish.py builds both on a synthetic lake and compares every table with EXCEPT ALL both ways).
Every published snapshot is recorded (data/state/ops/sync_publish.json: build start, as_of, section hashes and
timings), also those of the full build (hook in build_serving_db).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.ops.paths import ops_state_dir, read_json, write_json_atomic

log = get_logger("app.ops.sync.publish")

CAPTURE_AS_OF = (date(2031, 7, 17), date(2031, 8, 21))
FULL_BUILD_SHARE = 0.6          # dirty sections above this share of the full build time -> full build
RECORD_KEEP = 12
#: non-lake inputs read by a section (resolved to files / mtimes)
EXTRA_INPUTS: dict[str, tuple[str, ...]] = {"ml_macro": ("app_state",), "events_models": ("gpr_reports",)}
#: rough seconds per section when no build timing is recorded yet (2026-10-26 full build)
DEFAULT_SECTION_S: dict[str, float] = {
    "reference": 6, "prices": 400, "fundamentals": 140, "scores": 36, "alt": 102, "technicals": 122, "ml_macro": 31,
    "macro_v2_w1": 6, "macro_v2_w2": 3, "macro_v3": 4, "macro_v4": 32, "sec": 119, "events_v2": 352,
    "events_models": 21, "cftc": 57, "cross_asset": 9, "earnings": 24, "smart_money": 54, "supply_chain": 6,
    "conformal": 25, "technicals_prob": 7, "technicals_meta": 8, "technicals_v3": 7, "meta": 35, "copy": 60}

_LAKE = re.compile(r'\blake\.(?:"([^"]+)"|(\w+))')
_SNAP = re.compile(r'\bsnap\.(?:"([^"]+)"|(\w+))')
_WRITE = re.compile(r'(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)|INSERT\s+INTO|DELETE\s+FROM|UPDATE|DROP\s+TABLE'
                    r'(?:\s+IF\s+EXISTS)?)\s+snap\.(?:"([^"]+)"|(\w+))', re.IGNORECASE)


def _names(rx: re.Pattern[str], sql: str) -> set[str]:
    return {a or b for a, b in rx.findall(sql)}


def record_path(settings: Settings | None = None) -> Path:
    return ops_state_dir(settings) / "sync_publish.json"


def map_path(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    return s.cache_dir / "ops" / "serving_section_map.json"


def builder_hash() -> str:
    """Hash of the builder code (serving.py + sections.py) and the contracts it reads."""
    import app.api.serving as sv
    import app.ops.sections as sc

    h = hashlib.sha1()
    for mod in (sv, sc):
        try:
            h.update(Path(mod.__file__).read_bytes())
        except OSError:
            pass
    s = get_settings()
    for name in ("sector_kpis.yaml", "scoring.yaml"):
        try:
            h.update((s.contracts_dir / name).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


class _Recorder:
    """DuckDB connection proxy that keeps every SQL statement per current section."""

    def __init__(self, con: Any):
        self._con = con
        self.cur: str | None = None
        self.sql: dict[str, list[str]] = {}

    def execute(self, sql: str, *a: Any, **k: Any) -> Any:
        self.sql.setdefault(self.cur or "?", []).append(sql)
        return self._con.execute(sql, *a, **k)

    def executemany(self, sql: str, *a: Any, **k: Any) -> Any:
        self.sql.setdefault(self.cur or "?", []).append(sql)
        return self._con.executemany(sql, *a, **k)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def _capture_once(settings: Settings, as_of: date) -> dict[str, list[str]]:
    import duckdb

    from app.api import serving as sv

    con = duckdb.connect(":memory:")
    try:
        con.execute("SET enable_progress_bar = false")
        inputs = sv.register_lake(con, settings, empty=True)
        con.execute("ATTACH ':memory:' AS snap")
        rec = _Recorder(con)
        con.execute("SET TimeZone = 'UTC'")
        b = sv._Builder(rec, "snap.", as_of, settings, inputs, True)
        for name in sv.SECTIONS:
            rec.cur = name
            getattr(b, name)()
        return rec.sql
    finally:
        con.close()


def section_map(settings: Settings | None = None, refresh: bool = False) -> dict[str, Any]:
    """{"hash", "sections": {name: {lake, writes, reads, as_of, sql_hash, extra}}} (cached per builder hash)."""
    from app.api import serving as sv

    s = settings or get_settings()
    bh = builder_hash()
    p = map_path(s)
    doc = read_json(p, {}) or {}
    if not refresh and doc.get("hash") == bh and set(doc.get("sections") or {}) == set(sv.SECTIONS):
        return doc
    t0 = time.perf_counter()
    a, b = (_capture_once(s, d) for d in CAPTURE_AS_OF)
    writes_of: dict[str, set[str]] = {n: set().union(*[_names(_WRITE, q) for q in a.get(n, [])]) for n in sv.SECTIONS}
    out: dict[str, Any] = {}
    for n in sv.SECTIONS:
        qa, qb = a.get(n, []), b.get(n, [])
        lake = set().union(*[_names(_LAKE, q) for q in qa]) if qa else set()
        snap = set().union(*[_names(_SNAP, q) for q in qa]) if qa else set()
        out[n] = {"lake": sorted(lake), "writes": sorted(writes_of[n]),
                  "reads": sorted(t for t in snap - writes_of[n]),
                  "as_of": qa != qb,
                  "sql_hash": hashlib.sha1("\n;\n".join(qa).encode()).hexdigest()[:16],
                  "extra": list(EXTRA_INPUTS.get(n, ()))}
    doc = {"hash": bh, "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
           "capture_s": round(time.perf_counter() - t0, 1), "sections": out}
    try:
        write_json_atomic(p, doc)
    except OSError:  # pragma: no cover
        pass
    log.info("serving section map captured in %.1fs (%d sections)", doc["capture_s"], len(out))
    return doc


# ------------------------------------------------------------------------------------------------ records


def read_records(settings: Settings | None = None) -> dict[str, Any]:
    return read_json(record_path(settings), {}) or {}


def record_build(run_id: str, started_at: datetime, as_of: date, mode: str, sections: dict[str, float] | None = None,
                 rebuilt: list[str] | None = None, settings: Settings | None = None) -> None:
    """Remember what a published snapshot contains (called by every publish, full or incremental)."""
    s = settings or get_settings()
    try:
        smap = section_map(s)
        hashes = {k: v["sql_hash"] for k, v in smap["sections"].items()}
    except Exception as e:  # noqa: BLE001 - a missing map only means "unknown code fingerprint" next time
        log.warning("section map unavailable for the publish record: %s", e)
        hashes = {}
    doc = read_records(s)
    snaps = dict(doc.get("snapshots") or {})
    snaps[run_id] = {"started_at": started_at.astimezone(UTC).isoformat(),   # full precision: compared with mtimes
                     "as_of": as_of.isoformat(), "mode": mode, "sql_hash": hashes,
                     "rebuilt": rebuilt, "published_at": datetime.now(UTC).isoformat(timespec="seconds")}
    for k in sorted(snaps, key=lambda r: snaps[r].get("published_at") or "")[:-RECORD_KEEP]:
        snaps.pop(k, None)
    timings = dict(doc.get("section_s") or {})
    for k, v in (sections or {}).items():
        if mode == "full" or k not in timings:
            timings[k] = round(float(v), 1)
    write_json_atomic(record_path(s), {"snapshots": snaps, "section_s": timings, "last": run_id})


def last_published_at(settings: Settings | None = None) -> datetime | None:
    """When the current CURRENT snapshot was published (publish record, else the pointer file's mtime)."""
    s = settings or get_settings()
    snap = current_snapshot(s)
    rec = (snap or {}).get("record") or {}
    if rec.get("published_at"):
        try:
            t = datetime.fromisoformat(rec["published_at"])
            return t if t.tzinfo else t.replace(tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(s.serving_pointer_path.stat().st_mtime, UTC)
    except OSError:
        return None


def _run_started(run_id: str) -> datetime | None:
    m = re.search(r"(\d{8}T\d{6})", run_id or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


def current_snapshot(settings: Settings | None = None) -> dict[str, Any] | None:
    """{path, run_id, as_of, started_at, record} of the published snapshot (None when none)."""
    import duckdb

    from app.core.storage import current_serving_db

    s = settings or get_settings()
    p = current_serving_db(s)
    if p is None:
        return None
    meta: dict[str, str] = {}
    try:
        con = duckdb.connect(str(p), read_only=True)
        try:
            meta = dict(con.execute("SELECT key, value FROM _meta").fetchall())
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        log.warning("snapshot meta unreadable (%s): %s", p.name, e)
    run_id = meta.get("run_id") or p.stem.removeprefix("serving_")
    rec = (read_records(s).get("snapshots") or {}).get(run_id)
    started = (datetime.fromisoformat(rec["started_at"]) if rec and rec.get("started_at") else _run_started(run_id))
    if started is None:
        try:
            started = datetime.fromtimestamp(p.stat().st_mtime, UTC)
        except OSError:
            started = None
    as_of = meta.get("as_of")
    return {"path": p, "run_id": run_id, "as_of": date.fromisoformat(as_of) if as_of else None,
            "started_at": started, "built_at": meta.get("built_at"), "record": rec}


def _extra_mtime(kind: str, settings: Settings) -> float | None:
    try:
        if kind == "app_state":
            p = settings.db_dir / "app_state.duckdb"
            return p.stat().st_mtime if p.exists() else None
        if kind == "gpr_reports":
            files = list((settings.data_dir / "logs" / "reports").glob("gpr_validation_*.json"))
            return max((f.stat().st_mtime for f in files), default=None)
    except OSError:
        return None
    return None


def dirty_sections(settings: Settings | None = None, new_as_of: date | None = None, snap: dict[str, Any] | None = None,
                   smap: dict[str, Any] | None = None, clock: Any = None) -> dict[str, str]:
    """{section: reason} of the builder sections to rebuild against the published snapshot (empty = up to date)."""
    from app.api import serving as sv
    from app.ops.sync.graph import TableClock

    s = settings or get_settings()
    snap = snap if snap is not None else current_snapshot(s)
    if snap is None:
        return {n: "no published snapshot" for n in sv.SECTIONS}
    smap = smap or section_map(s)
    secs = smap["sections"]
    clock = clock or TableClock(s)
    t0 = snap.get("started_at")
    t0s = t0.timestamp() if t0 else 0.0
    rec = snap.get("record") or {}
    hashes = rec.get("sql_hash") or {}
    dirty: dict[str, str] = {}
    code_changed = False
    if not hashes:
        try:
            import app.api.serving as svm

            code_changed = Path(svm.__file__).stat().st_mtime > t0s
        except OSError:
            code_changed = False
    as_of_changed = new_as_of is not None and snap.get("as_of") is not None and new_as_of != snap.get("as_of")
    for n in sv.SECTIONS:
        info = secs.get(n) or {}
        if hashes and hashes.get(n) and hashes.get(n) != info.get("sql_hash"):
            dirty[n] = "builder code of this section changed"
            continue
        if code_changed:
            dirty[n] = "builder code changed after the snapshot (no section fingerprints recorded)"
            continue
        if as_of_changed and info.get("as_of"):
            dirty[n] = f"as_of {snap.get('as_of')} -> {new_as_of}"
            continue
        for t in info.get("lake") or []:
            m = clock.mtime(t)
            if m is not None and m > t0s:
                dirty[n] = f"{t} changed {datetime.fromtimestamp(m, UTC):%Y-%m-%d %H:%M} UTC"
                break
        if n in dirty:
            continue
        for kind in info.get("extra") or []:
            m = _extra_mtime(kind, s)
            if m is not None and m > t0s:
                dirty[n] = f"{kind} changed"
                break
    # propagate: readers of a dirty section's tables, co-writers of a table
    writers: dict[str, set[str]] = {}
    for n in sv.SECTIONS:
        for t in (secs.get(n) or {}).get("writes") or []:
            writers.setdefault(t, set()).add(n)
    changed = True
    while changed:
        changed = False
        out_tables = {t for n in dirty for t in (secs.get(n) or {}).get("writes") or []}
        for n in sv.SECTIONS:
            if n in dirty:
                continue
            info = secs.get(n) or {}
            hit = sorted(set(info.get("reads") or []) & out_tables)
            co = sorted(t for t in info.get("writes") or [] if writers.get(t, set()) & set(dirty))
            if hit or co:
                dirty[n] = f"reads {hit[0]}" if hit else f"co-writes {co[0]}"
                changed = True
    return {n: dirty[n] for n in sv.SECTIONS if n in dirty}


def plan_publish(settings: Settings | None = None, new_as_of: date | None = None) -> dict[str, Any]:
    """{mode: none | incremental | full, dirty: {section: why}, est_s, full_s, snapshot}."""
    from app.api import serving as sv

    s = settings or get_settings()
    snap = current_snapshot(s)
    dirty = dirty_sections(s, new_as_of, snap)
    timings = {**DEFAULT_SECTION_S, **(read_records(s).get("section_s") or {})}
    full_s = sum(float(timings.get(n, 30)) for n in sv.SECTIONS) + float(timings.get("meta", 35))
    est = sum(float(timings.get(n, 30)) for n in dirty) + float(timings.get("meta", 35)) + float(timings.get("copy", 60))
    if snap is None:
        mode = "full"
    elif not dirty:
        mode = "none"
    elif est >= FULL_BUILD_SHARE * full_s:
        mode = "full"
    else:
        mode = "incremental"
    return {"mode": mode, "dirty": dirty, "est_s": round(est), "full_s": round(full_s),
            "snapshot": snap.get("run_id") if snap else None, "snapshot_as_of": _iso(snap.get("as_of")) if snap else None,
            "as_of": _iso(new_as_of)}


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, date) else (str(v) if v else None)


def default_as_of(settings: Settings | None = None) -> date:
    """The full build's as_of rule: latest non-index price date in the lake (<= today)."""
    import duckdb

    from app.api import serving as sv

    s = settings or get_settings()
    con = duckdb.connect(":memory:")
    try:
        con.execute("SET memory_limit = '1GB'")
        sv.register_lake(con, s, only=["prices_daily"])
        return sv._default_as_of(con)
    finally:
        con.close()


def incremental_publish(sections: list[str], as_of: date, run_id: str | None = None,
                        settings: Settings | None = None, publish: bool = True) -> dict[str, Any]:
    """Copy CURRENT, rebuild `sections` (+ meta) into the copy, verify, flip CURRENT."""
    import duckdb

    from app.api import serving as sv
    from app.core.storage import current_serving_db, publish_serving_db

    s = settings or get_settings()
    src = current_serving_db(s)
    if src is None:
        raise RuntimeError("no published snapshot to patch: run the full build")
    run_id = run_id or ("SI" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + os.urandom(2).hex().upper())
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    started = datetime.now(UTC)
    t0 = time.perf_counter()
    final = s.db_dir / f"serving_{run_id}.duckdb"
    tmp = s.db_dir / f"serving_{run_id}.duckdb.tmp"
    for p in (tmp, Path(str(tmp) + ".wal")):
        if p.exists():
            p.unlink()
    shutil.copyfile(src, tmp)
    copy_s = round(time.perf_counter() - t0, 1)
    order = [n for n in sv.SECTIONS if n in set(sections)]
    con = sv.serving_build_connection(s)
    timings: dict[str, float] = {}
    try:
        inputs = sv.register_lake(con, s)
        con.execute(f"ATTACH '{tmp.as_posix()}' AS snap")
        b = sv._Builder(con, "snap.", as_of, s, inputs, False)
        for name in order:
            b.section(name, getattr(b, name))
        b.section("meta", lambda: b.meta(run_id, datetime.now(UTC)))
        timings = dict(b.sections)
        con.execute("CHECKPOINT snap")
        con.execute("DETACH snap")
    finally:
        con.close()
    os.replace(tmp, final)
    vcon = duckdb.connect(str(final), read_only=True)
    try:
        got = vcon.execute("SELECT value FROM _meta WHERE key = 'run_id'").fetchone()
        n_sec = vcon.execute("SELECT count(*) FROM sec").fetchone()[0]
    finally:
        vcon.close()
    if not got or got[0] != run_id:
        raise RuntimeError(f"snapshot verification failed for {final.name}")
    if publish:
        publish_serving_db(final, s)
        sv.cleanup_snapshots(settings=s)
        sv.warm_catalogs(s)
        timings["copy"] = copy_s
        record_build(run_id, started, as_of, "incremental", timings, order, s)
    secs = round(time.perf_counter() - t0, 1)
    log.info("incremental snapshot %s published in %.1fs (copy %.1fs; sections %s; as_of %s; sec=%d)", final.name,
             secs, copy_s, ", ".join(order) or "meta only", as_of, n_sec)
    return {"run_id": run_id, "path": str(final), "seconds": secs, "copy_s": copy_s, "sections": order,
            "timings": timings, "as_of": as_of.isoformat(), "sec_rows": int(n_sec), "published": publish}


def run_job(ctx: Any) -> Any:
    """jobs.yaml `serving_sections_publish` (exclusive heavy slot). params: sections (list), as_of (date); without
    sections the dirty set is computed here."""
    from app.scheduler.base import JobResult

    p = ctx.params or {}
    s = ctx.settings
    as_of = date.fromisoformat(str(p["as_of"])) if p.get("as_of") else default_as_of(s)
    secs = list(p.get("sections") or [])
    if not secs:
        plan = plan_publish(s, as_of)
        if plan["mode"] == "none":
            return JobResult(status="success", message="no section changed: nothing to publish")
        secs = list(plan["dirty"])
    rep = incremental_publish(secs, as_of, ctx.run_id.replace("R", "SI", 1) if ctx.run_id else None, s)
    msg = (f"{Path(rep['path']).name} as_of={rep['as_of']} in {rep['seconds']}s (copy {rep['copy_s']}s); "
           f"sections: {', '.join(rep['sections'])}")
    ctx.log.info(msg)
    return JobResult(status="success", rows_written={"serving_snapshot": rep["sec_rows"]}, message=msg)
