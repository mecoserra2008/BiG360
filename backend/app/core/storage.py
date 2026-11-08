"""Parquet lake + DuckDB helpers.

Layout: data/<contract.storage.path>/<col>=<val>/.../part-<uuid>.parquet  (Hive partitioning; partition columns are
NOT stored inside the files, they are re-attached on read with contract dtypes).

Writers (worker process only):
    write_table(df, "prices_daily")                 # contract default mode (usually upsert)
    write_table(df, "tech_levels", mode="overwrite")
Readers:
    scan_table("prices_daily").filter(...)            # Polars LazyFrame, typed per contract
    con = duckdb_connect(); register_views(con)       # one DuckDB view per contract table
API (read-only):
    serving_connection()                              # read-only connection to the current serving snapshot

Concurrency: one file lock per table (data/.locks/<table>.lock) serializes writers across processes; each partition is
swapped atomically (write to tmp dir, rename old away, rename tmp in, delete old) with retries for Windows file locks.
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
from filelock import FileLock

from .config import Settings, get_settings
from .logsetup import get_logger
from .schema import TableContract, conform, empty_frame, get_contract, load_contracts

log = get_logger(__name__)

_PART_SAFE = str.maketrans({"/": "_", "\\": "_", ":": "_", "=": "_", " ": "_"})


# ------------------------------------------------------------------------------------------------ paths


def table_dir(table: str | TableContract, settings: Settings | None = None) -> Path:
    c = table if isinstance(table, TableContract) else get_contract(table)
    s = settings or get_settings()
    return s.data_dir / c.path


def _part_value(v: Any) -> str:
    if v is None:
        return "__null__"
    return str(v).translate(_PART_SAFE)


def partition_dir(root: Path, cols: Sequence[str], values: Sequence[Any]) -> Path:
    p = root
    for c, v in zip(cols, values, strict=True):
        p = p / f"{c}={_part_value(v)}"
    return p


def _parquet_files(d: Path) -> list[Path]:
    if not d.exists():
        return []
    return [p for p in d.rglob("*.parquet") if not any(part.startswith((".tmp-", ".old-")) for part in p.parts)]


@contextmanager
def table_lock(table: str, settings: Settings | None = None, timeout: float = 600):
    s = settings or get_settings()
    s.locks_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(str(s.locks_dir / f"{table}.lock"), timeout=timeout):
        yield


def _retry_fs(fn, *args, attempts: int = 8, delay: float = 0.25):
    for i in range(attempts):
        try:
            return fn(*args)
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (2**i))


def _rmtree(p: Path) -> None:
    if p.exists():
        _retry_fs(shutil.rmtree, p)


# ------------------------------------------------------------------------------------------------ physical layout
# Contracts may declare a narrower physical type (`storage: float32`) and derived columns (`derived: [a, b]` = a * b,
# never stored), plus `storage.sort_by` / `storage.compression_level`. Writers go through physical_frame(); every
# reader (scan_table / view_sql / app.api.serving / exported Studio scripts via logical_select_sql) restores the
# logical contract types: float32 -> float64 rounded to 7 significant digits (float32 keeps ~7.2, so a source value
# with <= 7 significant digits round-trips exactly and anything else is within 5e-8 relative), derived columns
# recomputed (coalesced with a stored value while old-layout files exist).

_SIG_DIGITS = 7


def round_sig7(e: pl.Expr) -> pl.Expr:
    """Round a Float64 expression to 7 significant digits (inverse of a float32 narrowing, see module notes)."""
    k = pl.lit(float(_SIG_DIGITS - 1)) - e.abs().log10().floor()
    up = pl.lit(10.0).pow(k)
    dn = pl.lit(10.0).pow(-k)
    return (pl.when(e.is_null() | (e == 0) | e.is_infinite() | e.is_nan()).then(e)
            .when(k >= 0).then((e * up).round(0, mode="half_away_from_zero") / up)
            .otherwise((e / dn).round(0, mode="half_away_from_zero") * dn))


def sql_round_sig7(x: str) -> str:
    """DuckDB twin of round_sig7 (x: a DOUBLE SQL expression).

    round(x, k) with a per-row precision k is DuckDB's own round(x * 10^k) / 10^k (k >= 0) and
    round(x / 10^-k) * 10^-k (k < 0), i.e. the previous explicit CASE, but log10 is evaluated once instead of five
    times: 1.5-2x faster on the 98.5 M-row prices_daily lake view and bit-identical on all of it (refresh-pipeline fixes
    2026-10-26: 0 mismatches over close / open / adj_factor / volume)."""
    return (f"(CASE WHEN {x} IS NULL OR {x} = 0 OR isinf({x}) OR isnan({x}) THEN {x} "
            f"ELSE round({x}, ({_SIG_DIGITS - 1} - floor(log10(abs({x}))))::INTEGER) END)")


def _has_layout(c: TableContract) -> bool:
    return bool(c.narrowed_columns or c.derived_columns)


def _fold_product(cols: Sequence[str]) -> pl.Expr:
    e = pl.col(cols[0]).cast(pl.Float64)
    for x in cols[1:]:
        e = e * pl.col(x).cast(pl.Float64)
    return e


def physical_frame(df: pl.DataFrame, c: TableContract) -> pl.DataFrame:
    """Logical frame -> the frame written to Parquet: derived columns dropped (a missing stored factor is first
    back-solved from a materialized derived value, so merging old-layout rows loses nothing), narrowed columns cast,
    rows sorted by storage.sort_by."""
    der = c.derived_columns
    for name, facs in der.items():
        if name not in df.columns:
            continue
        for f in facs:
            if f not in df.columns:
                df = df.with_columns(pl.lit(None, pl.Float64).alias(f))
        for i, f in enumerate(facs):
            others = [x for j, x in enumerate(facs) if j != i]
            if not others or not df.get_column(f).null_count():
                continue
            prod = _fold_product(others)
            ok = (pl.col(f).is_null() & pl.col(name).is_not_null()
                  & pl.all_horizontal([pl.col(o).is_not_null() for o in others]) & (prod != 0))
            df = df.with_columns(pl.when(ok).then(pl.col(name).cast(pl.Float64) / prod).otherwise(pl.col(f)).alias(f))
    drop = [n for n in der if n in df.columns]
    if drop:
        df = df.drop(drop)
    narrow = {n: t for n, t in c.narrowed_columns.items() if n in df.columns}
    if narrow:
        df = df.with_columns([pl.col(n).cast(pl.Float32) for n in narrow])
    if c.sort_by and all(k in df.columns for k in c.sort_by) and df.height > 1:
        df = df.sort(list(c.sort_by))
    return df


def logical_lazy(lf: pl.LazyFrame, c: TableContract) -> pl.LazyFrame:
    """Physical scan -> logical contract types (narrowed columns widened + rounded, derived columns computed)."""
    if not _has_layout(c):
        return lf
    names = set(lf.collect_schema().names())
    widen = [round_sig7(pl.col(n).cast(pl.Float64)).alias(n) for n in c.narrowed_columns if n in names]
    if widen:
        lf = lf.with_columns(widen)
    der = []
    for name, facs in c.derived_columns.items():
        if all(f in names for f in facs):
            prod = _fold_product(facs)
            der.append((pl.coalesce(pl.col(name).cast(pl.Float64), prod) if name in names else prod).alias(name))
        elif name not in names:
            der.append(pl.lit(None, pl.Float64).alias(name))
    return lf.with_columns(der) if der else lf


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def logical_select_sql(c: TableContract, have: dict[str, str] | set[str] | None, src: str) -> str:
    """SELECT of every contract column with its logical type from a physical relation `src` whose columns are `have`
    ({name: duckdb type} or a set of names when types are unknown; None = no data -> typed empty select)."""
    exprs: dict[str, str] = {}
    typed = isinstance(have, dict)
    for col in c.columns:
        t = col.duckdb
        if have is None or col.derived:
            continue
        if col.name not in have:
            exprs[col.name] = f"CAST(NULL AS {t})"
        elif col.storage and col.storage != col.type:
            exprs[col.name] = sql_round_sig7(f"CAST({_q(col.name)} AS DOUBLE)")
        elif not typed or str(have[col.name]).upper() == t.upper():  # type: ignore[index]
            exprs[col.name] = _q(col.name)
        else:
            exprs[col.name] = f"TRY_CAST({_q(col.name)} AS {t})"
    out = []
    for col in c.columns:
        if have is None:
            out.append(f"CAST(NULL AS {col.duckdb}) AS {_q(col.name)}")
            continue
        if col.derived:
            facs = col.derived
            if all(f in have for f in facs):
                e = " * ".join(f"CAST({exprs[f]} AS DOUBLE)" for f in facs)
                if col.name in have:
                    e = f"coalesce(TRY_CAST({_q(col.name)} AS DOUBLE), {e})"
            elif col.name in have:
                e = f"TRY_CAST({_q(col.name)} AS DOUBLE)"
            else:
                e = "CAST(NULL AS DOUBLE)"
            out.append(f"{e} AS {_q(col.name)}")
        else:
            out.append(f"{exprs[col.name]} AS {_q(col.name)}")
    return f"SELECT {', '.join(out)} " + (f"FROM {src}" if have is not None else "WHERE FALSE")


_SCHEMA_CACHE: dict[tuple[str, float], dict[str, Any]] = {}


def _file_schema(f: Path) -> dict[str, Any]:
    try:
        key = (str(f), f.stat().st_mtime)
    except OSError:
        return {}
    got = _SCHEMA_CACHE.get(key)
    if got is None:
        got = dict(pl.read_parquet_schema(f))
        if len(_SCHEMA_CACHE) > 20000:
            _SCHEMA_CACHE.clear()
        _SCHEMA_CACHE[key] = got
    return got


def physical_columns(table: str | TableContract, settings: Settings | None = None) -> set[str]:
    """Union of the column names stored in a table's files plus its partition columns (footer reads, cached)."""
    c = table if isinstance(table, TableContract) else get_contract(table)
    out: set[str] = set(c.partition_by)
    for f in _parquet_files(table_dir(c, settings)):
        out.update(_file_schema(f))
    return out


# ------------------------------------------------------------------------------------------------ read


def scan_table(table: str, settings: Settings | None = None) -> pl.LazyFrame:
    """Lazy scan of a Parquet table, typed per contract. Empty (typed) frame if no data yet."""
    c = get_contract(table)
    if not c.is_parquet:
        raise ValueError(f"{table} is not a Parquet table")
    root = table_dir(c, settings)
    files = _parquet_files(root)
    if not files:
        return empty_frame(c).lazy()
    return _scan_files(files, c, root)


def _scan_files(files: list[Path], c: TableContract, root: Path) -> pl.LazyFrame:
    """Scan files, re-attaching partition columns from the directory names (robust across Polars versions)."""
    frames: list[pl.LazyFrame] = []
    by_dir: dict[tuple[Path, Any], list[Path]] = {}
    layout = _has_layout(c)
    for f in files:
        # files of one partition can differ in physical layout (float32 vs float64, derived columns present or not)
        # while a table migrates: scan each layout separately, concat relaxed
        sig = tuple(sorted((k, str(v)) for k, v in _file_schema(f).items())) if layout else None
        by_dir.setdefault((f.parent, sig), []).append(f)
    for (d, _sig), fs in by_dir.items():
        lf = pl.scan_parquet([str(x) for x in fs])
        if c.partition_by:
            rel = d.relative_to(root).parts
            kv = dict(part.split("=", 1) for part in rel if "=" in part)
            lf = lf.with_columns(
                [
                    (pl.lit(None) if kv.get(p) in (None, "__null__") else pl.lit(kv[p])).cast(c.column(p).polars).alias(p)
                    for p in c.partition_by
                ]
            )
        frames.append(lf)
    lf = pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]
    lf = logical_lazy(lf, c)
    names = lf.collect_schema().names()
    ordered = [n for n in c.column_names if n in names] + [n for n in names if n not in c.column_names]
    return lf.select(ordered)


def read_table(table: str, columns: Iterable[str] | None = None, filters: pl.Expr | None = None,
               settings: Settings | None = None) -> pl.DataFrame:
    lf = scan_table(table, settings)
    if filters is not None:
        lf = lf.filter(filters)
    if columns is not None:
        lf = lf.select(list(columns))
    return lf.collect()


# ------------------------------------------------------------------------------------------------ write


def _write_partition_files(d: Path, df: pl.DataFrame, c: TableContract | None = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    level = None
    if c is not None:
        df = physical_frame(df, c)
        level = c.compression_level
    df.write_parquet(d / f"part-{uuid.uuid4().hex[:12]}.parquet", compression="zstd", compression_level=level,
                     statistics=True)


def _swap_in(tmp: Path, target: Path) -> None:
    old = target.parent / f".old-{target.name}-{uuid.uuid4().hex[:8]}"
    if target.exists():
        _retry_fs(target.rename, old)
    target.parent.mkdir(parents=True, exist_ok=True)
    _retry_fs(tmp.rename, target)
    _rmtree(old)


def _read_partition(d: Path, c: TableContract) -> pl.DataFrame | None:
    files = [p for p in d.glob("*.parquet")]
    if not files:
        return None
    if _has_layout(c) and len(files) > 1:  # files may differ in physical layout while a table migrates
        return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    return pl.read_parquet([str(f) for f in files])


def write_table(df: pl.DataFrame, table: str, mode: str | None = None, settings: Settings | None = None) -> int:
    """Validate against the contract and write. Returns number of rows written (input rows)."""
    c = get_contract(table)
    if not c.is_parquet:
        raise ValueError(f"{table} is stored in DuckDB ({c.path}); use the app_state helpers")
    mode = mode or c.write_mode
    if mode not in ("overwrite", "upsert", "append", "overwrite_partition"):
        raise ValueError(f"invalid mode {mode!r}")
    df = conform(df, c)
    if df.is_empty() and mode != "overwrite":
        return 0
    s = settings or get_settings()
    root = table_dir(c, s)
    pcols = list(c.partition_by)
    pk = list(c.primary_key)

    with table_lock(table, s):
        if mode == "overwrite":
            tmp_root = root.parent / f".tmp-{root.name}-{uuid.uuid4().hex[:8]}"
            if pcols:
                for key, part in df.partition_by(pcols, as_dict=True, maintain_order=True).items():
                    _write_partition_files(partition_dir(tmp_root, pcols, key), part.drop(pcols), c)
            else:
                _write_partition_files(tmp_root, df, c)
            _swap_in(tmp_root, root)
            log.info("write %s overwrite rows=%d", table, df.height)
            return df.height

        groups = df.partition_by(pcols, as_dict=True, maintain_order=True) if pcols else {(): df}
        for key, part in groups.items():
            pdir = partition_dir(root, pcols, key) if pcols else root
            new = part.drop(pcols) if pcols else part
            if mode == "append":
                _write_partition_files(pdir, new, c)
                continue
            if mode == "upsert":
                existing = _read_partition(pdir, c)
                if existing is not None and existing.height:
                    merged = pl.concat([existing, new], how="diagonal_relaxed")
                    # partition columns are constant within a partition and are not stored in the files, so the
                    # PK dedupe uses only the in-file key columns (fix: PKs that include a partition column).
                    file_pk = [k for k in pk if k not in pcols]
                    new = merged.unique(subset=file_pk, keep="last", maintain_order=True) if file_pk else merged
            tmp = pdir.parent / f".tmp-{pdir.name}-{uuid.uuid4().hex[:8]}"
            _write_partition_files(tmp, new, c)
            _swap_in(tmp, pdir)
        log.info("write %s %s rows=%d partitions=%d", table, mode, df.height, len(groups))
        return df.height


# ------------------------------------------------------------------------------------------------ duckdb


def duckdb_connect(path: str | Path | None = None, read_only: bool = False,
                   settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """Connection factory. path=None -> in-memory. Applies thread/memory settings."""
    s = settings or get_settings()
    if path is not None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path) if path else ":memory:", read_only=read_only)
    con.execute(f"SET threads = {int(s.duckdb_threads)}")
    con.execute(f"SET memory_limit = '{s.duckdb_memory_limit}'")
    tmp = s.data_dir / "tmp" / "duckdb"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    return con


def _sql_str(p: Path | str) -> str:
    return str(Path(p).as_posix()).replace("'", "''")


def view_sql(c: TableContract, settings: Settings | None = None) -> str:
    """CREATE VIEW statement for a Parquet contract table (typed empty view if no files yet)."""
    root = table_dir(c, settings)
    has_files = bool(_parquet_files(root))
    if not has_files:
        cols = ", ".join(f"CAST(NULL AS {col.duckdb}) AS \"{col.name}\"" for col in c.columns)
        return f'CREATE OR REPLACE VIEW "{c.table}" AS SELECT {cols} WHERE FALSE'
    glob = _sql_str(root) + "/**/*.parquet" if c.partition_by else _sql_str(root) + "/*.parquet"
    opts = ["union_by_name = true"]
    if c.partition_by:
        types = ", ".join(f"'{p}': {c.column(p).duckdb}" for p in c.partition_by)
        opts += ["hive_partitioning = true", f"hive_types = {{{types}}}"]
    src = f"read_parquet('{glob}', {', '.join(opts)})"
    if _has_layout(c):  # narrowed / derived columns: explicit logical select
        return f"CREATE OR REPLACE VIEW \"{c.table}\" AS {logical_select_sql(c, physical_columns(c, settings), src)}"
    return f"CREATE OR REPLACE VIEW \"{c.table}\" AS SELECT * FROM {src}"


def register_views(con: duckdb.DuckDBPyConnection, tables: Iterable[str] | None = None,
                   settings: Settings | None = None) -> list[str]:
    """Create/refresh one view per Parquet contract table. Returns the registered table names."""
    contracts = load_contracts()
    names = list(tables) if tables is not None else [n for n, c in contracts.items() if c.is_parquet]
    done = []
    for n in names:
        c = contracts[n]
        if not c.is_parquet:
            continue
        con.execute(view_sql(c, settings))
        done.append(n)
    return done


def current_serving_db(settings: Settings | None = None) -> Path | None:
    """Path of the current serving snapshot (data/db/<name from CURRENT>), or None if not built yet."""
    s = settings or get_settings()
    ptr = s.serving_pointer_path
    if not ptr.exists():
        return None
    name = ptr.read_text(encoding="utf-8").strip()
    p = s.db_dir / name
    return p if name and p.exists() else None


def publish_serving_db(built: Path, settings: Settings | None = None) -> None:
    """Atomically point CURRENT at a freshly built snapshot (written by the build_serving_db job)."""
    s = settings or get_settings()
    tmp = s.serving_pointer_path.with_suffix(".tmp")
    tmp.write_text(built.name, encoding="utf-8")
    _retry_fs(tmp.replace, s.serving_pointer_path)


def serving_connection(settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """Read-only connection for the API. Falls back to an in-memory connection with Parquet views
    when no serving snapshot exists yet (development mode)."""
    s = settings or get_settings()
    p = current_serving_db(s)
    if p is not None:
        return duckdb_connect(p, read_only=True, settings=s)
    con = duckdb_connect(None, settings=s)
    register_views(con, settings=s)
    return con


# ------------------------------------------------------------------------------------------------ app_state / maintenance


def duckdb_table_ddl(table: str | TableContract) -> str:
    """CREATE TABLE IF NOT EXISTS statement for a contract table (used for app_state.duckdb tables)."""
    c = table if isinstance(table, TableContract) else get_contract(table)
    cols = [f'"{col.name}" {col.duckdb}{"" if col.nullable else " NOT NULL"}' for col in c.columns]
    if c.primary_key:
        cols.append("PRIMARY KEY (" + ", ".join(f'"{k}"' for k in c.primary_key) + ")")
    return f'CREATE TABLE IF NOT EXISTS "{c.table}" (' + ", ".join(cols) + ")"


def ensure_duckdb_tables(con: duckdb.DuckDBPyConnection, tables: Iterable[str]) -> None:
    """Create missing DuckDB tables from their contracts and add contract columns missing in existing tables."""
    for t in tables:
        c = get_contract(t)
        con.execute(duckdb_table_ddl(c))
        have = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [c.table]).fetchall()}
        for col in c.columns:
            if col.name not in have and col.nullable:
                con.execute(f'ALTER TABLE "{c.table}" ADD COLUMN "{col.name}" {col.duckdb}')


def connect_app_state(read_only: bool = False, attempts: int = 30, delay: float = 1.0,
                      settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """Open data/db/app_state.duckdb, retrying while another process holds the write lock.

    DuckDB allows one read-write process per file: the API should open it per request (or briefly) and close it,
    so the worker's alerts_eval job can get in. Raises the last duckdb.IOException after `attempts`.
    """
    s = settings or get_settings()
    s.db_dir.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for i in range(attempts):
        try:
            return duckdb.connect(str(s.app_state_db_path), read_only=read_only)
        except duckdb.IOException as e:  # "Could not set lock on file ..."
            last = e
            time.sleep(min(delay * (1.5**i), 10.0))
    assert last is not None
    raise last


def cleanup_leftovers(settings: Settings | None = None, older_than_s: float = 3600) -> int:
    """Remove `.tmp-*` / `.old-*` directories left by a crash mid-write (ARCHITECTURE §14). Run at worker start.

    Only entries older than `older_than_s` are removed, so a concurrent writer's in-flight tmp dir is never touched.
    """
    s = settings or get_settings()
    n = 0
    now = time.time()
    for zone in ("reference", "normalized", "features", "scores", "ml", "ops"):
        root = s.data_dir / zone
        if not root.exists():
            continue
        for p in list(root.rglob(".tmp-*")) + list(root.rglob(".old-*")):
            try:
                if not p.exists() or now - p.stat().st_mtime < older_than_s:
                    continue
                if p.is_dir():
                    _rmtree(p)
                else:
                    p.unlink()
                n += 1
            except OSError as e:
                log.warning("cleanup_leftovers: cannot remove %s: %s", p, e)
    return n
