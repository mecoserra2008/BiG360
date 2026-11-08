"""Table contracts: load contracts/schemas/*.yaml and validate / conform Polars DataFrames against them.

Usage:
    from app.core.schema import get_contract, conform, validate
    c = get_contract("prices_daily")
    df = conform(df, "prices_daily")      # casts, orders, fills nullable columns; raises ContractViolation
    errors = validate(df, "prices_daily") # list[str], never raises
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from .config import get_settings

# ------------------------------------------------------------------------------------------------ types

_SCALAR_POLARS: dict[str, pl.DataType] = {
    "string": pl.Utf8(),
    "enum": pl.Utf8(),
    "json": pl.Utf8(),
    "bool": pl.Boolean(),
    "int8": pl.Int8(),
    "int16": pl.Int16(),
    "int32": pl.Int32(),
    "int64": pl.Int64(),
    "float32": pl.Float32(),
    "float64": pl.Float64(),
    "date": pl.Date(),
    "datetime": pl.Datetime("us", "UTC"),
}

_SCALAR_DUCKDB: dict[str, str] = {
    "string": "VARCHAR",
    "enum": "VARCHAR",
    "json": "VARCHAR",
    "bool": "BOOLEAN",
    "int8": "TINYINT",
    "int16": "SMALLINT",
    "int32": "INTEGER",
    "int64": "BIGINT",
    "float32": "FLOAT",
    "float64": "DOUBLE",
    "date": "DATE",
    "datetime": "TIMESTAMP WITH TIME ZONE",
}

_LIST_RE = re.compile(r"^list<(\w+)>$")
VALID_WRITE_MODES = {"overwrite", "upsert", "append", "overwrite_partition"}
VALID_ZONES = {"reference", "normalized", "features", "scores", "ml", "ops", "app_state"}


class ContractViolation(ValueError):
    """Raised when a DataFrame does not satisfy its table contract."""

    def __init__(self, table: str, errors: list[str]):
        self.table, self.errors = table, errors
        super().__init__(f"{table}: " + "; ".join(errors[:20]) + (" ..." if len(errors) > 20 else ""))


def polars_dtype(type_str: str) -> pl.DataType:
    m = _LIST_RE.match(type_str)
    if m:
        return pl.List(_SCALAR_POLARS[m.group(1)])
    try:
        return _SCALAR_POLARS[type_str]
    except KeyError as e:
        raise ValueError(f"unknown contract type {type_str!r}") from e


def duckdb_type(type_str: str) -> str:
    m = _LIST_RE.match(type_str)
    if m:
        return f"{_SCALAR_DUCKDB[m.group(1)]}[]"
    try:
        return _SCALAR_DUCKDB[type_str]
    except KeyError as e:
        raise ValueError(f"unknown contract type {type_str!r}") from e


# ------------------------------------------------------------------------------------------------ model


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    nullable: bool
    description: str = ""
    values: tuple[str, ...] | None = None  # enum values
    #: physical Parquet type when narrower than the logical `type` (e.g. float32 for float64 prices); readers cast back
    #: and round to 7 significant digits (app.core.storage)
    storage: str | None = None
    #: computed on read as the left-fold product of these columns and never stored (e.g. adj_close = close*adj_factor)
    derived: tuple[str, ...] | None = None

    @property
    def polars(self) -> pl.DataType:
        return polars_dtype(self.type)

    @property
    def duckdb(self) -> str:
        return duckdb_type(self.type)


@dataclass(frozen=True)
class DynamicColumns:
    source: str
    pattern: str
    type: str
    nullable: bool = True
    allowed: frozenset[str] | None = None  # explicit names when the registry is loadable

    @property
    def prefix(self) -> str:
        return self.pattern.split("{", 1)[0]

    def matches(self, name: str) -> bool:
        if self.allowed is not None:
            return name in self.allowed
        return bool(self.prefix) and name.startswith(self.prefix)


@dataclass(frozen=True)
class TableContract:
    table: str
    version: int
    zone: str
    owner: str
    description: str
    path: str
    format: str
    partition_by: tuple[str, ...]
    write_mode: str
    primary_key: tuple[str, ...]
    columns: tuple[Column, ...]
    dynamic: DynamicColumns | None = None
    pit: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)
    sort_by: tuple[str, ...] = ()            # storage.sort_by: row order inside every written file
    compression_level: int | None = None     # storage.compression_level: zstd level (None = writer default)

    @property
    def derived_columns(self) -> dict[str, tuple[str, ...]]:
        return {c.name: c.derived for c in self.columns if c.derived}

    @property
    def narrowed_columns(self) -> dict[str, str]:
        return {c.name: c.storage for c in self.columns if c.storage and c.storage != c.type}

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def column(self, name: str) -> Column:
        for c in self.columns:
            if c.name == name:
                return c
        if self.dynamic and self.dynamic.matches(name):
            return Column(name, self.dynamic.type, self.dynamic.nullable)
        raise KeyError(f"{self.table}: no column {name!r}")

    @property
    def is_parquet(self) -> bool:
        return self.format == "parquet"

    def polars_schema(self, extra: list[str] | None = None) -> dict[str, pl.DataType]:
        sch = {c.name: c.polars for c in self.columns}
        for name in extra or []:
            sch[name] = self.column(name).polars
        return sch


def _load_dynamic(spec: dict[str, Any], root: Path) -> DynamicColumns:
    allowed: frozenset[str] | None = None
    src = root / spec["source"]
    if src.name == "canonical_line_items.yaml" and src.exists():
        items = yaml.safe_load(src.read_text(encoding="utf-8")).get("items", [])
        allowed = frozenset(i["id"] for i in items)
    return DynamicColumns(
        source=spec["source"],
        pattern=spec.get("pattern", "{id}"),
        type=spec.get("type", "float64"),
        nullable=bool(spec.get("nullable", True)),
        allowed=allowed,
    )


def parse_contract(doc: dict[str, Any], project_root: Path) -> TableContract:
    storage = doc.get("storage", {})
    cols = tuple(
        Column(
            name=c["name"],
            type=c["type"],
            nullable=bool(c.get("nullable", True)),
            description=c.get("description", ""),
            values=tuple(str(v) for v in c["values"]) if c.get("values") is not None else None,
            storage=c.get("storage"),
            derived=tuple(c["derived"]) if c.get("derived") else None,
        )
        for c in doc["columns"]
    )
    return TableContract(
        table=doc["table"],
        version=int(doc.get("version", 1)),
        zone=doc.get("zone", ""),
        owner=str(doc.get("owner", "")),
        description=str(doc.get("description", "")).strip(),
        path=storage.get("path", doc["table"]),
        format=storage.get("format", "parquet"),
        partition_by=tuple(storage.get("partition_by", []) or []),
        write_mode=storage.get("write_mode", "upsert"),
        primary_key=tuple(doc.get("primary_key", []) or []),
        columns=cols,
        dynamic=_load_dynamic(doc["dynamic_columns"], project_root) if doc.get("dynamic_columns") else None,
        pit=doc.get("pit"),
        raw=doc,
        sort_by=tuple(storage.get("sort_by", []) or []),
        compression_level=storage.get("compression_level"),
    )


def check_contract(c: TableContract) -> list[str]:
    """Static consistency checks of a contract (used by tests)."""
    errs: list[str] = []
    names = c.column_names
    if len(names) != len(set(names)):
        errs.append("duplicate column names")
    for col in c.columns:
        try:
            polars_dtype(col.type)
        except ValueError as e:
            errs.append(f"{col.name}: {e}")
        if col.type == "enum" and not col.values:
            errs.append(f"{col.name}: enum without values")
    for k in c.primary_key:
        if k not in names:
            errs.append(f"primary key column {k!r} not in columns")
        elif c.column(k).nullable and k not in ("period_start", "valid_to"):
            # nullable PK parts are allowed only for explicit open-ended fields
            errs.append(f"primary key column {k!r} is nullable")
    for p in c.partition_by:
        if p not in names:
            errs.append(f"partition column {p!r} not in columns")
    for col in c.columns:
        if col.storage and (col.storage not in ("float32",) or col.type != "float64"):
            errs.append(f"{col.name}: storage {col.storage!r} only supported as float32 for float64 columns")
        for f in col.derived or ():
            if f not in names or c.column(f).derived:
                errs.append(f"{col.name}: derived factor {f!r} must be a stored column")
        if col.derived and (col.type != "float64" or col.name in c.primary_key or col.name in c.partition_by):
            errs.append(f"{col.name}: derived columns must be non-key float64")
    for k in c.sort_by:
        if k not in names:
            errs.append(f"sort_by column {k!r} not in columns")
    if c.write_mode not in VALID_WRITE_MODES:
        errs.append(f"invalid write_mode {c.write_mode!r}")
    if c.zone not in VALID_ZONES:
        errs.append(f"invalid zone {c.zone!r}")
    if c.format not in ("parquet", "duckdb_table"):
        errs.append(f"invalid format {c.format!r}")
    return errs


@lru_cache(maxsize=1)
def load_contracts(schemas_dir: str | None = None) -> dict[str, TableContract]:
    settings = get_settings()
    d = Path(schemas_dir) if schemas_dir else settings.schemas_dir
    out: dict[str, TableContract] = {}
    for f in sorted(d.glob("*.yaml")):
        if f.name.startswith("_"):
            continue
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        c = parse_contract(doc, settings.project_root)
        if c.table != f.stem:
            raise ValueError(f"{f.name}: table name {c.table!r} != file stem")
        out[c.table] = c
    return out


def get_contract(table: str) -> TableContract:
    try:
        return load_contracts()[table]
    except KeyError as e:
        raise KeyError(f"no contract for table {table!r}") from e


# ------------------------------------------------------------------------------------------------ validation


def _extra_columns(df: pl.DataFrame, c: TableContract) -> tuple[list[str], list[str]]:
    known = set(c.column_names)
    dynamic, unknown = [], []
    for name in df.columns:
        if name in known:
            continue
        (dynamic if c.dynamic and c.dynamic.matches(name) else unknown).append(name)
    return dynamic, unknown


def validate(df: pl.DataFrame, table: str | TableContract, check_pk: bool = True) -> list[str]:
    """Return a list of contract violations (empty list == valid). Does not cast."""
    c = table if isinstance(table, TableContract) else get_contract(table)
    errs: list[str] = []
    dynamic, unknown = _extra_columns(df, c)
    if unknown:
        errs.append(f"unknown columns {unknown}")
    for col in list(c.columns) + [c.column(n) for n in dynamic]:
        if col.name not in df.columns:
            if not col.nullable:
                errs.append(f"missing required column {col.name!r}")
            continue
        s = df.get_column(col.name)
        if s.dtype != col.polars and s.dtype != pl.Null:
            errs.append(f"{col.name}: dtype {s.dtype} != {col.polars}")
        if not col.nullable and s.null_count() > 0:
            errs.append(f"{col.name}: {s.null_count()} nulls in non-nullable column")
        if col.values is not None and s.dtype in (pl.Utf8, pl.String):
            uniq = s.drop_nulls().unique()  # computed once: unique() order is not stable between calls
            bad = uniq.filter(~uniq.is_in(list(col.values)))
            if bad.len():
                errs.append(f"{col.name}: values not in enum {bad.head(5).to_list()}")
    if check_pk and c.primary_key and all(k in df.columns for k in c.primary_key) and df.height:
        dup = df.select(list(c.primary_key)).is_duplicated().sum()
        if dup:
            errs.append(f"{dup} rows with duplicate primary key {list(c.primary_key)}")
    return errs


def conform(df: pl.DataFrame, table: str | TableContract, strict: bool = True) -> pl.DataFrame:
    """Cast to contract dtypes, add missing nullable columns as nulls, order columns, then validate.

    Raises ContractViolation on: unknown columns, missing non-nullable columns, failed casts, nulls in
    non-nullable columns, invalid enum values, duplicate primary keys.
    """
    c = table if isinstance(table, TableContract) else get_contract(table)
    dynamic, unknown = _extra_columns(df, c)
    if unknown:
        raise ContractViolation(c.table, [f"unknown columns {unknown}"])
    exprs: list[pl.Expr] = []
    errs: list[str] = []
    for col in c.columns:
        if col.name in df.columns:
            exprs.append(_cast_expr(df, col))
        elif col.nullable:
            exprs.append(pl.lit(None, dtype=col.polars).alias(col.name))
        else:
            errs.append(f"missing required column {col.name!r}")
    for name in sorted(dynamic):
        exprs.append(_cast_expr(df, c.column(name)))
    if errs:
        raise ContractViolation(c.table, errs)
    try:
        out = df.select(exprs)
    except Exception as e:  # polars raises InvalidOperationError / ComputeError on strict casts
        raise ContractViolation(c.table, [f"cast failed: {e}"]) from e
    errs = validate(out, c)
    if errs and strict:
        raise ContractViolation(c.table, errs)
    return out


def _cast_expr(df: pl.DataFrame, col: Column) -> pl.Expr:
    src = df.schema[col.name]
    target = col.polars
    e = pl.col(col.name)
    if src == target:
        return e
    if isinstance(target, pl.Datetime) and isinstance(src, pl.Datetime):
        # naive timestamps are assumed UTC; tz-aware are converted
        e = e.dt.replace_time_zone("UTC") if src.time_zone is None else e.dt.convert_time_zone("UTC")
        return e.dt.cast_time_unit("us").alias(col.name)
    if isinstance(target, pl.Datetime) and src in (pl.Utf8, pl.String):
        return e.str.to_datetime(time_unit="us", time_zone="UTC").alias(col.name)
    if isinstance(target, pl.Date) and src in (pl.Utf8, pl.String):
        return e.str.to_date().alias(col.name)
    if isinstance(target, pl.Date) and isinstance(src, pl.Datetime):
        return e.dt.date().alias(col.name)
    return e.cast(target, strict=True).alias(col.name)


def empty_frame(table: str | TableContract) -> pl.DataFrame:
    c = table if isinstance(table, TableContract) else get_contract(table)
    return pl.DataFrame(schema=c.polars_schema())
