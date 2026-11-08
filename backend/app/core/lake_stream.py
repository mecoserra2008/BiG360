"""Bounded-memory Parquet writes for large derived tables (coverage v3 A1, 2026-11).

`storage.write_table` materialises whole partitions: an overwrite holds the complete new table in memory, an upsert
reads the target partition, merges and rewrites it. For tables whose partitions hold millions of rows this dominated
the jobs' memory and could not be reduced by smaller compute chunks:

* ratios_kpis: every ~flush_rows the upsert re-read and rewrote ratios/period_kind=TTM (18.7 M rows, ~3 GB in
  memory, ~42 s per flush): OOM at the 6 GB cap from 2026-11-03, and the oom_params retry (smaller chunks, more
  flushes) died the same way or ran into its 60 min timeout;
* tech_levels: from the first November run the month-end retention read the October snapshot (6.1 M rows) back,
  concatenated it with the new 6 M-row snapshot and conformed / wrote the 12 M-row result in one piece: OOM at 7 GB.

`StagedWrite` writes each computed chunk straight to Parquet in a staging directory next to the table root
(`.tmp-<table>-stage-<id>`; storage.cleanup_leftovers / the next StagedWrite of the table remove crash leftovers)
and commits once, with Polars' streaming engine doing every pass over existing rows:

* `commit_overwrite(retain=expr)`: rows of the current table matching `retain` are streamed into the stage, then the
  stage becomes the table (same atomic directory swap as write_table's overwrite);
* `commit_replace(key, ids)`: each partition holding rows of `ids` or staged rows is rewritten once as
  (existing rows whose `key` is not in ids) + (staged rows). This is exactly the previous incremental result
  "upsert the new rows, then drop the recomputed entities' rows the run did not produce" (ratios.drop_stale_rows /
  drop_stale_sector_rows), including entities whose partition changed.

Peak memory is one staged chunk plus the streaming engine's buffers, whatever the table size. A failed or
cancelled run leaves the table untouched (the stage is discarded) instead of half-upserted. Only tables without a
physical layout (storage-narrowed or derived columns) and without dynamic columns are supported.

Every existing row a commit writes again (retained month-end rows, other entities' rows of a rewritten partition) is
projected onto the CURRENT contract shape (contract order, conform's casts, missing nullable columns as nulls), like
the previous paths that conformed the read-back frame: after an additive contract change a directory never mixes
old-schema and new-schema files (an unpartitioned table is scanned as one multi-file scan, which rejects that).

Stage integrity (review 2026-11-07): storage.cleanup_leftovers (worker start) removes `.tmp-*` dirs by their own
mtime, which on NTFS changes only when a DIRECT child is added, i.e. no longer once a partitioned stage's partition
dirs exist. `add` therefore touches the stage root after every chunk, and every commit first checks that the stage
still holds exactly the rows / files `add` wrote (`verify`): a lost or partial stage raises StageIntegrityError and
leaves the table untouched instead of committing an empty / partial stage as a success.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterable
from pathlib import Path

import polars as pl

from . import storage as st
from .config import Settings, get_settings
from .logsetup import get_logger
from .schema import ContractViolation, TableContract, _cast_expr, conform, get_contract

log = get_logger(__name__)

#: staging dirs of a table older than this are crash leftovers (no job run lasts that long: timeouts <= 3 h)
STALE_STAGE_S = 6 * 3600


class StageIntegrityError(RuntimeError):
    """The staging dir no longer holds what add() wrote (removed by a leftover sweep, or files deleted)."""


def _files_by_partition(root: Path) -> dict[Path, list[Path]]:
    """{partition dir relative to root: parquet files} of a table (tmp / old swap dirs excluded)."""
    out: dict[Path, list[Path]] = {}
    if not root.exists():
        return out
    for f in root.rglob("*.parquet"):
        rel = f.relative_to(root)
        if any(part.startswith((".tmp-", ".old-")) for part in rel.parts):  # (the stage root itself is a .tmp- dir)
            continue
        out.setdefault(rel.parent, []).append(f)
    return {k: sorted(v) for k, v in out.items()}


def _scan(files: list[Path]) -> pl.LazyFrame:
    """Lazy scan of stored files as stored (one scan per file: files of a partition may differ in columns)."""
    frames = [pl.scan_parquet(str(f), hive_partitioning=False) for f in files]
    return pl.concat(frames, how="diagonal_relaxed") if len(frames) > 1 else frames[0]


def _rows_in(path: Path) -> int:
    return int(pl.scan_parquet(str(path), hive_partitioning=False).select(pl.len()).collect().item() or 0)


class StagedWrite:
    """Stage chunks of a table's new rows on disk, then commit them in one streaming pass (see module doc).

    with StagedWrite("ratios") as sw:
        for chunk in chunks:
            sw.add(chunk)                       # conformed + written immediately, nothing kept in memory
        sw.commit_replace("issuer_id", ids)     # or sw.commit_overwrite(retain=pl.col(...) ...)
    """

    def __init__(self, table: str | TableContract, settings: Settings | None = None) -> None:
        self.c = table if isinstance(table, TableContract) else get_contract(table)
        if not self.c.is_parquet:
            raise ValueError(f"{self.c.table} is not a Parquet table")
        if st._has_layout(self.c):
            raise ValueError(f"{self.c.table}: StagedWrite does not support storage-narrowed / derived columns")
        if self.c.dynamic is not None:
            raise ValueError(f"{self.c.table}: StagedWrite does not support dynamic columns")
        self.s = settings or get_settings()
        self.table = self.c.table
        self.root = st.table_dir(self.c, self.s)
        self.pcols = list(self.c.partition_by)
        self._prefix = f".tmp-{self.root.name}-stage-"
        self._merge_prefix = f".tmp-{self.root.name}-merge-"
        self._sweep()
        self.stage = self.root.parent / f"{self._prefix}{uuid.uuid4().hex[:10]}"
        self.rows = 0
        self.files = 0
        self.closed = False

    # -------------------------------------------------------------------------------------------- staging

    def _sweep(self) -> None:
        parent = self.root.parent
        if not parent.exists():
            return
        now = time.time()
        for p in [*parent.glob(f"{self._prefix}*"), *parent.glob(f"{self._merge_prefix}*")]:
            try:
                if p.is_dir() and now - p.stat().st_mtime > STALE_STAGE_S:
                    st._rmtree(p)
                    log.info("%s: removed stale staging dir %s", self.table, p.name)
            except OSError as e:  # pragma: no cover - Windows lock races
                log.warning("%s: cannot remove stale staging dir %s: %s", self.table, p, e)

    def add(self, df: pl.DataFrame) -> int:
        """Conform (contract validation, PK uniqueness within the chunk) and write the chunk to the stage."""
        if self.closed:
            raise RuntimeError("StagedWrite already committed / discarded")
        if df.is_empty():
            return 0
        df = conform(df, self.c)
        if self.pcols:
            for key, part in df.partition_by(self.pcols, as_dict=True, maintain_order=True).items():
                st._write_partition_files(st.partition_dir(self.stage, self.pcols, key), part.drop(self.pcols), self.c)
                self.files += 1
        else:
            st._write_partition_files(self.stage, df, self.c)
            self.files += 1
        self.rows += df.height
        self._touch()
        return df.height

    def _touch(self) -> None:
        """Refresh the stage root's mtime: leftover sweeps (storage.cleanup_leftovers, _sweep) judge staleness by it,
        and on NTFS writing into an existing partition subdir does not change the root's mtime."""
        try:
            os.utime(self.stage, None)
        except OSError as e:  # pragma: no cover - lock races; verify() still catches a lost stage
            log.warning("%s: cannot touch staging dir %s: %s", self.table, self.stage.name, e)

    def verify(self) -> None:
        """Raise StageIntegrityError unless the stage still holds exactly the files and rows add() wrote (footer row
        counts only). Called by both commits before anything is written; callers committing several tables can call
        it on every stage first, so a lost stage fails the run before any table is committed."""
        if self.closed:
            raise RuntimeError("StagedWrite already committed / discarded")
        if not self.rows:
            return
        if not self.stage.exists():
            raise StageIntegrityError(f"{self.table}: staging dir {self.stage.name} with {self.rows} rows is gone")
        files = [f for fs in _files_by_partition(self.stage).values() for f in fs]
        n = sum(_rows_in(f) for f in files)
        if n != self.rows or len(files) != self.files:
            raise StageIntegrityError(f"{self.table}: staging dir {self.stage.name} holds {n} rows in {len(files)} "
                                      f"files, add() wrote {self.rows} rows in {self.files} files")
        self._touch()  # the commit's own passes start from a fresh mtime

    def discard(self) -> None:
        if self.stage.exists():
            st._rmtree(self.stage)
        self.closed = True

    def __enter__(self) -> StagedWrite:
        return self

    def __exit__(self, *exc) -> None:
        if not self.closed:
            self.discard()

    # -------------------------------------------------------------------------------------------- commit

    def _sink(self, lf: pl.LazyFrame, out_dir: Path) -> int:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"part-{uuid.uuid4().hex[:12]}.parquet"
        lf.sink_parquet(str(path), compression="zstd", compression_level=self.c.compression_level, statistics=True)
        n = _rows_in(path)
        if not n:
            path.unlink()
        return n

    def _contract_shape(self, lf: pl.LazyFrame) -> pl.LazyFrame:
        """Stored rows -> the in-file shape add() writes: the contract's non-partition columns in contract order, cast
        as conform casts them, missing nullable columns added as nulls, non-contract columns dropped. Columns already
        of the contract type are selected as is (no cast: the scan keeps predicate / projection pushdown)."""
        schema = lf.collect_schema()
        probe = pl.DataFrame(schema=dict(schema))  # _cast_expr only reads the source dtype
        exprs: list[pl.Expr] = []
        missing: list[str] = []
        for col in self.c.columns:
            if col.name in self.pcols:
                continue
            if col.name in schema:
                exprs.append(pl.col(col.name) if schema[col.name] == col.polars else _cast_expr(probe, col))
            elif col.nullable:
                exprs.append(pl.lit(None, dtype=col.polars).alias(col.name))
            else:
                missing.append(col.name)
        if missing:
            raise ContractViolation(self.table, [f"stored rows lack required column(s) {missing}"])
        return lf.select(exprs)

    def commit_overwrite(self, retain: pl.Expr | None = None) -> int:
        """Replace the table by the staged rows plus the current rows matching `retain` (projected onto the current
        contract shape). Returns the table's row count after the commit."""
        self.verify()
        kept = 0
        with st.table_lock(self.table, self.s):
            if retain is not None:
                for rel, files in _files_by_partition(self.root).items():
                    kept += self._sink(self._contract_shape(_scan(files)).filter(retain), self.stage / rel)
            self.stage.mkdir(parents=True, exist_ok=True)
            st._swap_in(self.stage, self.root)
        self.closed = True
        log.info("write %s staged overwrite rows=%d (+%d retained) files=%d", self.table, self.rows, kept, self.files)
        return self.rows + kept

    def commit_replace(self, key: str, ids: Iterable[str]) -> dict[str, int]:
        """Every row of entities `ids` (column `key`) is replaced by the staged rows, in whatever partition it was.
        Partitions without staged rows and without rows of `ids` are not touched.
        Returns {"rows": staged rows, "replaced": existing rows of ids removed, "partitions": partitions rewritten}."""
        self.verify()
        ids = sorted({str(x) for x in ids if x is not None})
        hit = pl.col(key).is_in(ids).fill_null(False) if ids else pl.lit(False)
        staged = _files_by_partition(self.stage) if self.stage.exists() else {}
        replaced = rewritten = 0
        with st.table_lock(self.table, self.s):
            existing = _files_by_partition(self.root)
            for rel in sorted(set(existing) | set(staged), key=str):
                old_files, new_files = existing.get(rel, []), staged.get(rel, [])
                n_hit = 0
                if old_files and ids:
                    n_hit = int(_scan(old_files).filter(hit).select(pl.len()).collect(engine="streaming").item() or 0)
                if not new_files and not n_hit:
                    continue
                target = self.root / rel
                parts: list[pl.LazyFrame] = []
                if old_files:  # other entities' rows, in the current contract shape (one schema per partition)
                    parts.append(self._contract_shape(_scan(old_files).filter(~hit) if n_hit else _scan(old_files)))
                if new_files:
                    parts.append(self._contract_shape(_scan(new_files)))
                lf = pl.concat(parts, how="vertical") if len(parts) > 1 else parts[0]
                # merged partition is built OUTSIDE the table root (a `root/**/*.parquet` reader never sees it)
                tmp = self.root.parent / f"{self._merge_prefix}{uuid.uuid4().hex[:10]}"
                self._sink(lf, tmp)
                tmp.mkdir(parents=True, exist_ok=True)
                st._swap_in(tmp, target)
                replaced += n_hit
                rewritten += 1
        rows = self.rows
        self.discard()
        log.info("write %s staged replace rows=%d replaced=%d partitions=%d (key %s, %d ids)", self.table, rows,
                 replaced, rewritten, key, len(ids))
        return {"rows": rows, "replaced": replaced, "partitions": rewritten}


def distinct_values(table: str, column: str, settings: Settings | None = None) -> list:
    """Distinct values of one column of a table (streaming; cheap for low-cardinality columns such as dates)."""
    s = settings or get_settings()
    root = st.table_dir(get_contract(table), s)
    files = st._parquet_files(root)
    if not files:
        return []
    lf = _scan(files)
    if column not in lf.collect_schema().names():
        return []
    return lf.select(pl.col(column).unique()).collect(engine="streaming").to_series().drop_nulls().sort().to_list()
