"""Day-file store for the events-v2 tables (E3).

Layout (per contract, partition_by [year_month]):
    <table>/year_month=YYYY-MM/d-YYYYMMDD.parquet   one file per processed UTC day (atomic replace -> idempotent)
    <table>/year_month=YYYY-MM/m-YYYYMM.parquet     compacted month (events_retention), sorted by (label, date)
    <table>/year_month=YYYY-MM/p-YYYYMMDD.parquet   roadmap v3 N3: PARTIAL day folded hourly from the live hot store
                                                    (replaced by the d- file when the day is finalised; read_range
                                                    and compaction ignore it, lake scans / serving see it)
Re-processing a day whose month is already compacted first removes that day's rows from the month file.
Files are ZSTD; rows are sorted by the table's label key so dictionary / RLE encoding compresses them well.
The partition column is not stored in the files (Hive layout); scan_table / DuckDB re-attach it.

Day-replace semantics (refresh-pipeline fixes 2026-10-26). Every writer goes through write_day / compact_month, which
REPLACE whole days (an upsert by date, partition-safe); nothing appends rows. A day may briefly live in both a d- file
and the m- file (a Windows reader holding the month file open made os.replace / unlink fail between the two steps:
the 2026-05-22..31 event_daily_agg duplicates). The rule that keeps that harmless: **a d- file is authoritative for
its day**, so read_range and compact_month drop month-file rows of days that have a d- file, compactions de-duplicate
on the contract primary key, and heal_overlaps() repairs a month in place (events_agg runs it every night).
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.schema import conform, get_contract
from app.core.storage import table_dir, table_lock

log = get_logger(__name__)

SORT_KEYS: dict[str, list[str]] = {
    "gdelt_event_labels": ["country", "date", "cameo", "event_id"],
    "gdelt_article_labels": ["date", "theme_mask", "url_hash"],
    "event_daily_agg": ["label_type", "label_id", "theme", "date"],
    "news_stories": ["date", "salience"],
    "story_members": ["date", "story_id", "url_hash"],
    "event_hourly_agg": ["label_type", "label_id", "theme", "date", "hour"],
    "sentiment_hourly": ["label_type", "label_id", "date", "hour"],
    "sentiment_label_daily": ["label_type", "label_id", "date"],
}
DAY_LEVEL = 6
MONTH_LEVEL = 12


def month_dir(table: str, ym: str, settings: Settings | None = None) -> Path:
    return table_dir(table, settings) / f"year_month={ym}"


def day_file(table: str, d: date, settings: Settings | None = None) -> Path:
    return month_dir(table, d.strftime("%Y-%m"), settings) / f"d-{d:%Y%m%d}.parquet"


def partial_file(table: str, d: date, settings: Settings | None = None) -> Path:
    return month_dir(table, d.strftime("%Y-%m"), settings) / f"p-{d:%Y%m%d}.parquet"


def write_partial(table: str, d: date, df: pl.DataFrame, settings: Settings | None = None) -> int:
    """Replace the PARTIAL (hourly-folded) rows of day d (p- file). Empty frame -> file removed."""
    s = settings or get_settings()
    f = partial_file(table, d, s)
    with table_lock(table, s):
        if df.height:
            df = _without_day_keys(table, d, df, s)   # a d- file is authoritative for its keys (W1 2026-10-31)
        if not df.height:
            f.unlink(missing_ok=True)
            return 0
        return _write_atomic(_prepare(table, df), f, DAY_LEVEL)


def _without_day_keys(table: str, d: date, df: pl.DataFrame, s: Settings) -> pl.DataFrame:
    """Rows of a PARTIAL frame whose primary key is not in day d's d- file. Lake scans and the serving build read
    p- and d- files side by side, so an overlap is a duplicate key (dq_checks pk_unique on news_stories /
    story_members, 2026-10-31: story_cluster merged Polygon / RSS items into a d- file of the still-live day while
    news_hourly had folded the same stories into the p- file)."""
    f = day_file(table, d, s)
    pk = [k for k in _file_pk(table) if k in df.columns]
    if not f.exists() or not pk:
        return df
    try:
        have = pl.read_parquet(f, columns=pk)
    except Exception:  # noqa: BLE001 - unreadable d- file: keep the partial as is
        return df
    return df.join(have.unique(), on=pk, how="anti", nulls_equal=True) if have.height else df


def _trim_partial(table: str, d: date, keys: pl.DataFrame, s: Settings) -> int:
    """Drop from day d's p- file the rows whose primary key is in `keys` (the new d- content). Returns rows dropped.
    Caller holds the table lock."""
    pf = partial_file(table, d, s)
    pk = [k for k in _file_pk(table) if k in keys.columns]
    if not pf.exists() or not pk or not keys.height:
        return 0
    p = pl.read_parquet(pf)
    if not all(k in p.columns for k in pk):
        return 0
    keep = p.join(keys.select(pk).unique(), on=pk, how="anti", nulls_equal=True)
    n = p.height - keep.height
    if n:
        if keep.height:
            _write_atomic(keep, pf, DAY_LEVEL)
        else:
            _retry(pf.unlink)
        log.info("%s %s: %d partial rows superseded by the d- file", table, d, n)
    return n


def heal_partial_overlaps(table: str, settings: Settings | None = None) -> dict[str, int]:
    """Repair days whose p- partial shares primary keys with the d- file (written before the 2026-10-31 guard)."""
    s = settings or get_settings()
    days = rows = 0
    root = table_dir(table, s)
    if not root.exists():
        return {"days": 0, "rows_removed": 0}
    pk = _file_pk(table)
    with table_lock(table, s):
        for pf in sorted(root.glob("year_month=*/p-*.parquet")):
            n = pf.stem[2:]
            try:
                d = date(int(n[0:4]), int(n[4:6]), int(n[6:8]))
            except ValueError:
                continue
            f = day_file(table, d, s)
            if not f.exists():
                continue
            k = _trim_partial(table, d, pl.read_parquet(f, columns=pk), s)
            if k:
                days += 1
                rows += k
    return {"days": days, "rows_removed": rows}


def delete_partial(table: str, d: date, settings: Settings | None = None) -> bool:
    s = settings or get_settings()
    f = partial_file(table, d, s)
    with table_lock(table, s):
        if f.exists():
            f.unlink()
            return True
    return False


def read_partial(table: str, d: date, settings: Settings | None = None) -> pl.DataFrame:
    f = partial_file(table, d, settings)
    if not f.exists():
        from app.core.schema import empty_frame  # noqa: PLC0415

        return empty_frame(get_contract(table)).drop("year_month", strict=False)
    return pl.read_parquet(f)


def month_file(table: str, ym: str, settings: Settings | None = None) -> Path:
    return month_dir(table, ym, settings) / f"m-{ym.replace('-', '')}.parquet"


def _retry(fn: Callable[[], object], attempts: int = 8, delay: float = 0.25) -> None:
    """Windows: a reader (DuckDB lake scan, API) holding a file open makes os.replace / unlink fail with
    PermissionError for a moment - retry with back-off (~1 min in total) before giving up."""
    for i in range(attempts):
        try:
            fn()
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (2 ** i))


def _write_atomic(df: pl.DataFrame, path: Path, level: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tmp-{uuid.uuid4().hex[:8]}-{path.name}")
    df.write_parquet(tmp, compression="zstd", compression_level=level, statistics=True)
    try:
        _retry(lambda: os.replace(tmp, path))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path.stat().st_size


def _file_pk(table: str) -> list[str]:
    """Primary-key columns stored in the files (the partition column year_month is not stored)."""
    c = get_contract(table)
    return [k for k in c.primary_key if k not in c.partition_by]


def _file_day(f: Path) -> date | None:
    n = f.stem
    if n.startswith("d-") and len(n) >= 10:
        try:
            return date(int(n[2:6]), int(n[6:8]), int(n[8:10]))
        except ValueError:
            return None
    return None


def dedupe_pk(table: str, df: pl.DataFrame) -> pl.DataFrame:
    """Drop duplicate primary keys (last row wins, order kept)."""
    pk = [k for k in _file_pk(table) if k in df.columns]
    if not pk or df.height == 0:
        return df
    return df.unique(subset=pk, keep="last", maintain_order=True)


def _prepare(table: str, df: pl.DataFrame) -> pl.DataFrame:
    c = get_contract(table)
    if "year_month" not in df.columns and "date" in df.columns:
        df = df.with_columns(pl.col("date").dt.strftime("%Y-%m").alias("year_month"))
    df = conform(df, c)
    keys = [k for k in SORT_KEYS.get(table, []) if k in df.columns]
    if keys:
        df = df.sort(keys, descending=[k == "salience" for k in keys], nulls_last=True)
    return df.drop([p for p in c.partition_by if p in df.columns])


def _purge_day_from_month(table: str, d: date, s: Settings) -> None:
    mf = month_file(table, d.strftime("%Y-%m"), s)
    if not mf.exists():
        return
    m = pl.read_parquet(mf)
    if "date" in m.columns and m.filter(pl.col("date") == d).height:
        rest = m.filter(pl.col("date") != d)
        if rest.height:
            _write_atomic(rest, mf, MONTH_LEVEL)
        else:
            _retry(mf.unlink)


def write_day(table: str, d: date, df: pl.DataFrame, settings: Settings | None = None) -> int:
    """Replace the day's rows of `table` (day file; also purged from a compacted month file). Returns bytes written.

    Upsert by date: the day's content is replaced, never appended. The d- file is written FIRST and the month file
    purged second, so a failure in between leaves the authoritative d- file plus a stale month copy that readers
    ignore (module doc) instead of duplicate rows. Rows with a duplicate primary key are refused by conform()."""
    s = settings or get_settings()
    out = _prepare(table, df) if df.height else df
    with table_lock(table, s):
        f = day_file(table, d, s)
        if not df.height:
            _purge_day_from_month(table, d, s)
            if f.exists():
                _retry(f.unlink)
            return 0
        n = _write_atomic(out, f, DAY_LEVEL)
        _purge_day_from_month(table, d, s)
        _trim_partial(table, d, out, s)   # no key in both the d- and the p- file (lake scans read both)
        return n


def read_range(table: str, start: date, end: date, settings: Settings | None = None,
               columns: list[str] | None = None) -> pl.DataFrame:
    """Rows with start <= date <= end (reads only the touched month dirs)."""
    s = settings or get_settings()
    c = get_contract(table)
    files: list[Path] = []
    month_files: list[str] = []
    d_days: list[date] = []          # every d- file day of the touched months (authoritative over the month file)
    ym = date(start.year, start.month, 1)
    while ym <= end:
        md = month_dir(table, ym.strftime("%Y-%m"), s)
        if md.exists():
            for f in md.glob("*.parquet"):
                n = f.stem
                if n.startswith("d-"):
                    fd = _file_day(f)
                    if fd is None:
                        continue
                    d_days.append(fd)
                    if not (start <= fd <= end):
                        continue
                elif n.startswith("m-"):
                    month_files.append(str(f))
                else:
                    continue
                files.append(f)
        ym = date(ym.year + (ym.month == 12), ym.month % 12 + 1, 1)
    if not files:
        from app.core.schema import empty_frame  # noqa: PLC0415

        e = empty_frame(c)
        return e.select(columns) if columns else e
    overlap = bool(month_files) and bool(d_days)
    lf = pl.scan_parquet([str(f) for f in files], allow_missing_columns=True,
                         include_file_paths="__file" if overlap else None)
    lf = lf.filter((pl.col("date") >= start) & (pl.col("date") <= end))
    if overlap:  # a day with a d- file: the month-file copy (stale, left by an interrupted write) is ignored
        is_month = pl.col("__file").str.contains(r"(^|[\\/])m-\d{6}\.parquet$")
        lf = lf.filter(~(is_month & pl.col("date").is_in(d_days))).drop("__file")
    if columns:
        lf = lf.select([x for x in columns if x != "year_month"])
    out = lf.collect()
    if table in DEDUPE_ON_READ and (not columns or all(k in out.columns for k in _file_pk(table))):
        out = dedupe_pk(table, out)
    return out


#: small tables whose readers (events models: pandas re-indexing) must never see a duplicate primary key, even from
#: a month file written before the 2026-10-26 fix
DEDUPE_ON_READ = frozenset({"event_daily_agg"})


def compact_month(table: str, ym: str, transform: Callable[[pl.DataFrame], pl.DataFrame] | None = None,
                  settings: Settings | None = None) -> dict[str, int]:
    """Merge all files of a month into m-YYYYMM.parquet (optionally pruned by `transform`), sorted by label key."""
    s = settings or get_settings()
    md = month_dir(table, ym, s)
    if not md.exists():
        return {"rows_before": 0, "rows_after": 0, "bytes_before": 0, "bytes_after": 0}
    with table_lock(table, s):
        files = sorted(f for f in md.glob("*.parquet") if f.name.startswith(("d-", "m-")))   # never p- partials
        if not files:
            return {"rows_before": 0, "rows_after": 0, "bytes_before": 0, "bytes_after": 0}
        before = sum(f.stat().st_size for f in files)
        df = _month_content(table, files)
        n0 = df.height
        if transform is not None:
            df = transform(df)
        df = dedupe_pk(table, df)
        keys = [k for k in SORT_KEYS.get(table, []) if k in df.columns]
        if keys:
            df = df.sort(keys, descending=[k == "salience" for k in keys], nulls_last=True)
        mf = month_file(table, ym, s)
        after = _write_atomic(df, mf, MONTH_LEVEL) if df.height else 0
        # the merged month file now holds the d- files' days; a d- file that cannot be removed (reader holds it)
        # stays authoritative for its day and has the same content, so nothing is duplicated
        for f in files:
            if f != mf and f.exists():
                try:
                    _retry(f.unlink)
                except PermissionError:
                    log.warning("compact %s %s: %s still open by a reader; kept (authoritative copy)", table, ym, f.name)
        if not df.height and mf.exists():
            _retry(mf.unlink)
        return {"rows_before": n0, "rows_after": df.height, "bytes_before": before, "bytes_after": after}


def _month_content(table: str, files: list[Path]) -> pl.DataFrame:
    """Rows of one month dir's d- / m- files: month-file rows of days that have a d- file are dropped (the d- file is
    authoritative), then duplicate primary keys are removed (d- files win: they are read last)."""
    d_days = [d for d in (_file_day(f) for f in files) if d is not None]
    parts: list[pl.DataFrame] = []
    for f in sorted(files, key=lambda p: (not p.name.startswith("m-"), p.name)):   # month file first
        x = pl.read_parquet(f)
        if f.name.startswith("m-") and d_days and "date" in x.columns:
            x = x.filter(~pl.col("date").is_in(d_days))
        parts.append(x)
    df = pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame()
    return dedupe_pk(table, df)


def heal_overlaps(table: str, settings: Settings | None = None, months_: list[str] | None = None) -> dict[str, int]:
    """Repair months whose files overlap (a d- file day also in the m- file) or whose month file holds duplicate
    primary keys: the month is re-compacted with the d- file rule. Returns {"months_healed", "rows_removed"}."""
    s = settings or get_settings()
    pk = _file_pk(table)
    healed = removed = 0
    for ym in (months_ if months_ is not None else months(table, s)):
        md = month_dir(table, ym, s)
        mf = month_file(table, ym, s)
        if not mf.exists():
            continue
        d_days = [d for d in (_file_day(f) for f in md.glob("d-*.parquet")) if d is not None]
        m = pl.scan_parquet(mf)
        cols = m.collect_schema().names()
        bad = 0
        if d_days and "date" in cols:
            bad += int(m.filter(pl.col("date").is_in(d_days)).select(pl.len()).collect().item())
        if pk and all(k in cols for k in pk):
            n = m.select(pl.len()).collect().item()
            bad += int(n - m.select(pk).unique().select(pl.len()).collect().item())
        if bad:
            compact_month(table, ym, None, s)
            healed += 1
            removed += bad
            log.warning("healed %s %s: %d overlapping / duplicate rows removed", table, ym, bad)
    return {"months_healed": healed, "rows_removed": removed}


def table_bytes(table: str, settings: Settings | None = None) -> int:
    root = table_dir(table, settings)
    return sum(f.stat().st_size for f in root.rglob("*.parquet")) if root.exists() else 0


def months(table: str, settings: Settings | None = None) -> list[str]:
    root = table_dir(table, settings)
    if not root.exists():
        return []
    return sorted(p.name.split("=", 1)[1] for p in root.glob("year_month=*") if p.is_dir())


def is_compacted(table: str, ym: str, settings: Settings | None = None) -> bool:
    md = month_dir(table, ym, settings)
    return md.exists() and not any(md.glob("d-*.parquet")) and any(md.glob("m-*.parquet"))


V2_TABLES = ("gdelt_event_labels", "gdelt_article_labels", "event_daily_agg", "news_stories", "story_members")
