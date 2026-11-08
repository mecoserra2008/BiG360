"""Model Studio dataset builder (owner: model-studio).

    from app.modeling.datasets import DatasetSpec, build_dataset, preview, variable_catalog
    spec = DatasetSpec.model_validate({...})
    ds = build_dataset(spec)            # Dataset(frame, meta, key, snapshot_id, cached)

* DatasetSpec is declarative (pydantic). Its normalized JSON (model_dump(mode="json")) is what the standalone core
  (`app.modeling._core`) consumes and what exported scripts embed.
* Reads the Parquet lake read-only through DuckDB views (`app.core.storage.view_sql`); never writes lake tables.
* Cache: data/modeling/cache/<sha1(spec json + snapshot id + core version)>.parquet (+ .json meta).
* Variable catalog: every usable variable with its table, frequency, units and coverage; cached per snapshot id.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal

import duckdb
import polars as pl
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import Settings, get_settings
from app.core.schema import get_contract, load_contracts
from app.core.storage import current_serving_db, duckdb_connect, view_sql

from . import _core
from ._core import LEAD_OPS, SOURCES, SpecError

NAME_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

TransformOp = Literal["lag", "lead", "diff", "log", "log_diff", "pct_change", "rolling_mean", "rolling_std",
                      "rolling_sum", "rolling_max", "rolling_min", "rolling_z", "drawdown", "fwd_sum", "fwd_compound",
                      "winsorize", "standardize", "cs_rank", "cs_zscore", "cs_demean", "binarize", "abs", "square",
                      "neg", "scale", "clip"]

TRANSFORM_DOCS: dict[str, str] = {
    "lag": "x[t-k] (k periods back, per entity)", "lead": "x[t+k] — LOOK-AHEAD: allowed only as a target",
    "diff": "x[t] - x[t-k]", "log": "ln(x), null for x <= 0", "log_diff": "ln(x[t]) - ln(x[t-k])",
    "pct_change": "x[t]/x[t-k] - 1", "rolling_mean": "trailing mean over `window` periods",
    "rolling_std": "trailing std over `window`", "rolling_sum": "trailing sum over `window`",
    "rolling_max": "trailing max over `window`", "rolling_min": "trailing min over `window`",
    "rolling_z": "(x - trailing mean) / trailing std over `window`",
    "drawdown": "x / trailing max(x, window) - 1 (use on a price level)",
    "fwd_sum": "sum of x over t+1..t+k — LOOK-AHEAD (targets only)",
    "fwd_compound": "prod(1+x) over t+1..t+k - 1 (forward return from periodic returns) — LOOK-AHEAD (targets only)",
    "winsorize": "clip to [lower, upper] quantiles; scope cross_section (per date, panel default) | all | entity",
    "standardize": "z-score; scope cross_section | all | entity", "cs_rank": "cross-sectional percentile rank 0..1 per date",
    "cs_zscore": "cross-sectional z-score per date", "cs_demean": "minus the cross-sectional mean per date",
    "binarize": "1 if x > threshold else 0", "abs": "|x|", "square": "x^2", "neg": "-x", "scale": "x * factor",
    "clip": "clip to [lower, upper] (absolute values)",
}


class Transform(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: TransformOp
    k: int | None = Field(default=None, ge=1, le=520, description="lag/lead/diff/pct_change/fwd_* periods")
    window: int | None = Field(default=None, ge=1, le=2600, description="rolling window (periods)")
    min_periods: int | None = Field(default=None, ge=1, le=2600)
    lower: float | None = None
    upper: float | None = None
    threshold: float | None = None
    factor: float | None = None
    scope: Literal["cross_section", "all", "entity"] | None = None

    @model_validator(mode="after")
    def _defaults(self) -> "Transform":
        if self.op in ("lag", "lead", "diff", "pct_change", "log_diff", "fwd_sum", "fwd_compound") and self.k is None:
            self.k = 1
        if self.op.startswith("rolling") or self.op == "drawdown":
            self.window = self.window or 12
        if self.op == "winsorize":
            self.lower = 0.01 if self.lower is None else self.lower
            self.upper = 0.99 if self.upper is None else self.upper
            if not (0 <= self.lower < self.upper <= 1):
                raise ValueError("winsorize quantiles must satisfy 0 <= lower < upper <= 1")
        return self


class EntityFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    security_ids: list[str] | None = None
    issuer_ids: list[str] | None = None
    tickers: list[str] | None = None
    universe: str | None = Field(default=None, description="universe_membership.universe_id (mvp_us, mvp_global, full, eu_broad, ...)")
    sectors: list[str] | None = None
    regions: list[str] | None = None
    countries: list[str] | None = None
    primary_only: bool = True


class Variable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="column name in the dataset (identifier)")
    source: str = Field(description="catalog source id: " + ", ".join(SOURCES))
    field: str = Field(description="column (wide sources) or metric/factor/series id (long sources)")
    agg: Literal["last", "first", "mean", "sum", "min", "max", "median", "count"] | None = Field(
        default=None, description="within-period aggregation for event sources (default last; sum for volumes)")
    entity_agg: Literal["mean", "median", "sum", "count", "min", "max"] | None = Field(
        default=None, description="cross-entity aggregation for time-series / group-level datasets (default mean)")
    entities: EntityFilter | None = Field(default=None, description="time-series datasets only: own entity filter")
    options: dict[str, Any] | None = Field(default=None, description="source options, e.g. {period_kind: TTM|FY, currency_basis: USD|LOCAL}")
    max_stale_days: int | None = Field(default=None, ge=1, description="PIT sources: null out values older than this")
    pre_transforms: list[Transform] = Field(default_factory=list, description="entity-level, before aggregation")
    transforms: list[Transform] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RX.match(v):
            raise ValueError("variable name must be an identifier ([A-Za-z_][A-Za-z0-9_]*, <= 63 chars)")
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        if v not in SOURCES and v != "bql":  # bql: roadmap v3 B1 (app.modeling.bql_source)
            raise ValueError(f"unknown source {v!r}; one of {sorted(SOURCES) + ['bql']}")
        return v

    @property
    def uses_future(self) -> bool:
        return any(t.op in LEAD_OPS for t in self.pre_transforms + self.transforms)


class MissingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ffill_limit: int | None = Field(default=None, ge=0, le=260, description="periods an event value is carried forward (default D5/W2/M1/Q1)")
    fill: Literal["none", "ffill", "zero", "mean"] = "none"
    drop: Literal["none", "any"] = Field(default="none", description="any = drop rows with a null in any column; methods always drop listwise on the columns they use")


class DatasetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["timeseries", "panel", "cross_section"] = "timeseries"
    frequency: Literal["D", "W", "M", "Q"] = "M"
    start: date | None = None
    end: date | None = None
    as_of: date | None = Field(default=None, description="cross_section: snapshot date (default end / today)")
    lookback: int = Field(default=13, ge=1, le=600, description="cross_section: periods of history for transforms")
    entities: EntityFilter = Field(default_factory=EntityFilter)
    entity_level: Literal["security", "sector", "region", "country"] = "security"
    variables: list[Variable] = Field(min_length=1, max_length=60)
    dummies: list[Literal["sector", "region", "country", "month", "quarter", "year"]] = Field(default_factory=list)
    interactions: list[list[str]] = Field(default_factory=list, description="pairs [a, b] -> column a_x_b")
    missing: MissingPolicy = Field(default_factory=MissingPolicy)
    macro_alignment: Literal["auto", "pit", "period"] = "auto"
    pit_universe: bool = Field(default=True, description="keep (entity, date) rows only while the entity is in the universe")
    max_entities: int = Field(default=3000, ge=1, le=20000)
    max_rows: int = Field(default=5_000_000, ge=10, le=20_000_000)

    @model_validator(mode="after")
    def _check(self) -> "DatasetSpec":
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError("variable names must be unique")
        for p in self.interactions:
            if len(p) != 2:
                raise ValueError("interactions are pairs [a, b]")
        if self.start and self.end and self.start > self.end:
            raise ValueError("start > end")
        return self

    def normalized(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def spec_hash(self) -> str:
        return hashlib.sha1(json.dumps(self.normalized(), sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ infrastructure


def modeling_dir(settings: Settings | None = None) -> Path:
    s = settings or get_settings()
    d = s.data_dir / "modeling"
    for sub in ("cache", "runs"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return d


def snapshot_id(settings: Settings | None = None) -> str:
    p = current_serving_db(settings)
    return p.stem.removeprefix("serving_") if p is not None else "nosnapshot"


def lake_connection(tables: list[str] | None = None, settings: Settings | None = None) -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with views over the requested lake tables (read-only use)."""
    s = settings or get_settings()
    con = duckdb_connect(None, settings=s)
    names = tables or sorted({src["table"] for src in SOURCES.values()} | set(_core.REFERENCE_TABLES)
                             | {"macro_series", "cftc_markets"})
    for t in names:
        con.execute(view_sql(get_contract(t), s))
    return con


@dataclass
class Dataset:
    frame: pl.DataFrame
    meta: dict[str, Any]
    key: str
    snapshot_id: str
    cached: bool = False
    spec: dict[str, Any] = field(default_factory=dict)

    @property
    def entity_col(self) -> str | None:
        return "entity_id" if "entity_id" in self.frame.columns else None


def dataset_key(spec: DatasetSpec, snap: str) -> str:
    raw = json.dumps({"spec": spec.normalized(), "snapshot": snap, "core": _core.CORE_VERSION}, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def norm_key(norm: dict[str, Any], snap: str) -> str:
    """Cache key of a normalized dataset spec dict (== dataset_key for a DatasetSpec's normalized())."""
    raw = json.dumps({"spec": norm, "snapshot": snap, "core": _core.CORE_VERSION}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def core_build_cached(norm: dict[str, Any], con: duckdb.DuckDBPyConnection, check: Callable[[], None] | None = None,
                      settings: Settings | None = None) -> tuple[pl.DataFrame, dict[str, Any], bool]:
    """`_core.build` through the one dataset cache shared by Studio and backtests (S1.5): data/modeling/cache/
    <key>.parquet + .json, keyed like build_dataset. Returns (frame, meta, cached)."""
    s = settings or get_settings()
    snap = snapshot_id(s)
    key = norm_key(norm, snap)
    cdir = modeling_dir(s) / "cache"
    pq, mj = cdir / f"{key}.parquet", cdir / f"{key}.json"
    with _build_locks_guard:
        lock = _build_locks.setdefault(key, threading.Lock())
    with lock:
        if pq.exists() and mj.exists():
            try:
                return pl.read_parquet(pq), json.loads(mj.read_text(encoding="utf-8")), True
            except Exception:  # noqa: BLE001 - corrupt cache entry: rebuild
                pass
        frame, meta = _core.build(norm, con, check=check)
        _store(frame, meta, pq, mj, snap, key)
        return frame, meta, False


def _store(frame: pl.DataFrame, meta: dict[str, Any], pq: Path, mj: Path, snap: str, key: str) -> None:
    meta["coverage"] = coverage(frame, meta)
    meta["snapshot_id"] = snap
    meta["key"] = key
    tmp = pq.with_suffix(f".tmp{threading.get_ident()}")
    frame.write_parquet(tmp)
    tmp.replace(pq)
    mj.write_text(json.dumps(meta, default=str), encoding="utf-8")


def build_dataset(spec: DatasetSpec, settings: Settings | None = None, use_cache: bool = True,
                  check: Callable[[], None] | None = None) -> Dataset:
    s = settings or get_settings()
    snap = snapshot_id(s)
    key = dataset_key(spec, snap)
    cdir = modeling_dir(s) / "cache"
    pq, mj = cdir / f"{key}.parquet", cdir / f"{key}.json"
    with _build_locks_guard:
        lock = _build_locks.setdefault(key, threading.Lock())
    with lock:
        if use_cache and pq.exists() and mj.exists():
            try:
                return Dataset(pl.read_parquet(pq), json.loads(mj.read_text(encoding="utf-8")), key, snap, True,
                               spec.normalized())
            except Exception:  # corrupt cache entry: rebuild
                pass
        norm = spec.normalized()
        from . import bql_source

        if bql_source.has_bql(norm):  # roadmap v3 B1: BQL expressions on the serving snapshot (+ core variables)
            from app.bql.engine import serving_connection

            con = lake_connection(sorted(set(_core.tables_needed(
                {**norm, "variables": [v for v in norm["variables"] if v["source"] != "bql"]})) | set(_core.REFERENCE_TABLES)), s)
            scon = serving_connection()
            try:
                frame, meta = bql_source.build(norm, con, scon, check=check)
            finally:
                con.close()
                scon.close()
        else:
            con = lake_connection(_core.tables_needed(norm), s)
            try:
                frame, meta = _core.build(norm, con, check=check)
            finally:
                con.close()
        _store(frame, meta, pq, mj, snap, key)
        return Dataset(frame, meta, key, snap, False, norm)


def coverage(frame: pl.DataFrame, meta: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    cols = meta.get("variables", []) + meta.get("dummies", []) + meta.get("interactions", [])
    n = frame.height
    has_ent = "entity_id" in frame.columns
    for c in cols:
        nn = frame.filter(pl.col(c).is_not_null() & pl.col(c).is_not_nan()) if frame.schema[c].is_float() else \
            frame.filter(pl.col(c).is_not_null())
        out.append({
            "column": c, "n_non_null": nn.height, "pct_non_null": round(nn.height / n, 4) if n else 0.0,
            "first_date": nn["date"].min().isoformat() if nn.height else None,
            "last_date": nn["date"].max().isoformat() if nn.height else None,
            "n_entities": int(nn["entity_id"].n_unique()) if has_ent and nn.height else None,
            "uses_future": c in meta.get("lead_variables", []),
        })
    return out


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def summary_stats(frame: pl.DataFrame, cols: list[str]) -> list[dict[str, Any]]:
    out = []
    for c in cols:
        s = frame[c].cast(pl.Float64)
        s = s.filter(s.is_not_null() & s.is_not_nan())
        if s.len() == 0:
            out.append({"column": c, "n": 0, "mean": None, "std": None, "min": None, "p25": None, "median": None,
                        "p75": None, "max": None})
            continue
        out.append({"column": c, "n": s.len(), "mean": _f(s.mean()), "std": _f(s.std()), "min": _f(s.min()),
                    "p25": _f(s.quantile(0.25)), "median": _f(s.median()), "p75": _f(s.quantile(0.75)),
                    "max": _f(s.max())})
    return out


def jsonable_rows(frame: pl.DataFrame, limit: int) -> list[dict[str, Any]]:
    out = []
    for r in frame.head(limit).iter_rows(named=True):
        out.append({k: (v.isoformat() if isinstance(v, date) else (_f(v) if isinstance(v, float) else v))
                    for k, v in r.items()})
    return out


def preview(spec: DatasetSpec, n: int = 50, settings: Settings | None = None) -> dict[str, Any]:
    ds = build_dataset(spec, settings)
    cols = ds.meta["variables"] + ds.meta["dummies"] + ds.meta["interactions"]
    return {
        "dataset_key": ds.key, "snapshot_id": ds.snapshot_id, "cached": ds.cached,
        "kind": ds.meta["kind"], "frequency": ds.meta["frequency"], "macro_alignment": ds.meta["macro_alignment"],
        "start": ds.meta["start"], "end": ds.meta["end"], "n_rows": ds.meta["n_rows"],
        "n_entities": ds.meta["n_entities"], "n_periods": ds.meta["n_periods"],
        "columns": [{"name": c, "dtype": str(t)} for c, t in ds.frame.schema.items()],
        "rows": jsonable_rows(ds.frame, n), "stats": summary_stats(ds.frame, cols),
        "coverage": ds.meta["coverage"], "warnings": ds.meta["warnings"],
        "build_seconds": ds.meta.get("build_seconds"),
    }


# ------------------------------------------------------------------------------------------------ catalog

_catalog_cache: dict[str, Any] = {}
_catalog_lock = threading.Lock()


def _numeric_columns(source_id: str, described: list[tuple[str, str]]) -> list[tuple[str, str, str | None]]:
    """(column, description, unit) for numeric columns of a wide source, from the view's DESCRIBE output."""
    src = SOURCES[source_id]
    c = get_contract(src["table"])
    desc = {col.name: col.description or "" for col in c.columns}
    excl = set(src.get("exclude", []))
    num = ("DOUBLE", "FLOAT", "BIGINT", "INTEGER", "SMALLINT", "TINYINT", "BOOLEAN", "HUGEINT", "DECIMAL")
    out = [(n, desc.get(n, ""), None) for n, t in described if n not in excl and str(t).upper().startswith(num)]
    if source_id == "prices":
        out = [("ret", "Simple return over the period (from adj_close)", "fraction"),
               ("log_ret", "Log return over the period (from adj_close)", "log")] + out
    return out


def _source_meta(sid: str) -> dict[str, Any]:
    src = SOURCES[sid]
    return {"source": sid, "table": src["table"], "title": src["title"], "entity_type": src["entity"],
            "kind": src["kind"], "layout": src["layout"], "native_frequency": src["native_freq"],
            "date_column": src.get("date_col") or src.get("knowledge_col"),
            "pit_column": src.get("knowledge_col"), "key_column": src.get("key_col"),
            "options": src.get("options", {})}


CATALOG_SCHEMA = 3  # 3: + instruments (price-history)


def catalog_version() -> str:
    """Fingerprint of everything that shapes the catalog (source registry, core version, catalog schema):
    a code change invalidates the per-snapshot catalog cache."""
    raw = json.dumps({"s": SOURCES, "core": _core.CORE_VERSION, "schema": CATALOG_SCHEMA}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:10]


def build_catalog(settings: Settings | None = None) -> dict[str, Any]:
    """{sources: [...], variables: [...]} with coverage from the lake. ~1-3 s; cached per snapshot id."""
    s = settings or get_settings()
    snap = snapshot_id(s)
    with _catalog_lock:
        ckey = f"{snap}_{catalog_version()}"
        hit = _catalog_cache.get(ckey)
        if hit and time.monotonic() - hit["_t"] < 3600:
            return hit
        disk = modeling_dir(s) / "cache" / f"catalog_{ckey}.json"
        if disk.exists():
            try:
                cat = json.loads(disk.read_text(encoding="utf-8"))
                cat["_t"] = time.monotonic()
                _catalog_cache.clear()
                _catalog_cache[ckey] = cat
                return cat
            except ValueError:
                pass
        con = lake_connection(settings=s)
        try:
            cat = _compute_catalog(con)
        finally:
            con.close()
        cat["snapshot_id"] = snap
        disk.write_text(json.dumps(cat, default=str), encoding="utf-8")
        cat["_t"] = time.monotonic()
        _catalog_cache.clear()
        _catalog_cache[ckey] = cat
        return cat


def _iso(v: Any) -> str | None:
    return v.isoformat() if isinstance(v, date) else (str(v)[:10] if v else None)


_CFTC_CATALOG_METRICS = ("net", "net_pct_oi", "cot_index_3y", "cot_index_1y", "z_3y", "chg_1w")
_CFTC_HEADLINE = {"legacy": ("noncomm", "comm", "spec_hedger"), "disagg": ("m_money", "prod_merc", "swap", "spec_hedger"),
                  "tff": ("lev_money", "asset_mgr", "dealer", "spec_hedger")}


def _cftc_variables(con: duckdb.DuckDBPyConnection, sid: str, src: dict[str, Any]) -> list[dict[str, Any]]:
    """Catalog entries for the CFTC source: curated (major) active markets x their primary futures-only report x
    headline categories x common metrics. Any other market / report / category / metric is accepted by field."""
    try:
        rows = con.execute("""
            SELECT s.market_code, s.report_type, s.category, any_value(m.short_name), any_value(m.asset_class),
                   count(*), min(s.report_date), max(s.report_date)
            FROM cftc_signals s JOIN cftc_markets m USING (market_code)
            WHERE m.is_major AND m.is_active AND s.report_type = m.primary_report || '_fut'
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""").fetchall()
    except duckdb.Error:
        return []
    out = []
    for code, rt, cat, name, ac, n, d0, d1 in rows:
        if cat not in _CFTC_HEADLINE.get(rt.split("_")[0], ()):
            continue
        for metric in _CFTC_CATALOG_METRICS:
            out.append({"source": sid, "field": f"{code}:{rt}:{cat}:{metric}", "table": src["table"],
                        "title": f"{name or code} - {cat.replace('_', ' ')} {metric.replace('_', ' ')} (CFTC)",
                        "country": "US", "frequency": "W", "units": "contracts" if metric in ("net", "chg_1w") else None,
                        "category": f"positioning:{ac}", "entity_type": "macro", "n_obs": int(n or 0), "n_entities": None,
                        "first_date": _iso(d0), "last_date": _iso(d1), "pit": True})
    return out


_XA_CATALOG_METRICS = (("price", None), ("trend_signal", "score -1..1"), ("mom_12m", "log return"), ("carry", "fraction p.a."),
                       ("value", None), ("curve_slope", "per year"), ("rv_1m", "fraction p.a."), ("iv_rv_ratio", "ratio"),
                       ("z_carry", "z"), ("cot_index_3y", "index 0-1"), ("cot_z_3y", "z"))


def _cross_asset_variables(con: duckdb.DuckDBPyConnection, sid: str, src: dict[str, Any]) -> list[dict[str, Any]]:
    """Catalog entries for the cross-asset source: every asset x headline metrics with data. Any other metric of
    src['metrics'] is accepted by field ("<asset_id>:<metric>")."""
    cols = ", ".join(f"count({m})" for m, _ in _XA_CATALOG_METRICS)
    try:
        rows = con.execute(f"""SELECT asset_id, any_value(asset_class), count(*), min(date), max(date), {cols}
                               FROM cross_asset_signals GROUP BY 1 ORDER BY 1""").fetchall()
    except duckdb.Error:
        return []
    try:
        from app.analytics.cross_asset import build_assets  # noqa: PLC0415

        names = {a.asset_id: a.name for a in build_assets()}
    except Exception:  # noqa: BLE001 - names are cosmetic
        names = {}
    out = []
    for r in rows:
        aid, ac, n, d0, d1 = r[:5]
        for (metric, units), cnt in zip(_XA_CATALOG_METRICS, r[5:], strict=True):
            if not cnt:
                continue
            out.append({"source": sid, "field": f"{aid}:{metric}", "table": src["table"],
                        "title": f"{names.get(aid, aid)} - {metric.replace('_', ' ')}", "country": None, "frequency": "D",
                        "units": units, "category": f"cross_asset:{ac}", "entity_type": "macro", "n_obs": int(cnt or 0),
                        "n_entities": None, "first_date": _iso(d0), "last_date": _iso(d1), "pit": True})
    return out


def _compute_catalog(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    sources, variables = [], []
    for sid, src in SOURCES.items():
        meta = _source_meta(sid)
        t = src["table"]
        dcol = src.get("date_col") or src.get("period_col")
        ent = {"security": "security_id", "issuer": "issuer_id", "macro": "series_id", "factor": "security_id"}[src["entity"]]
        if src.get("field_split"):
            ent = src["key_col"]
        try:
            row = con.execute(f'SELECT count(*), count(DISTINCT "{ent}"), min("{dcol}"), max("{dcol}") FROM "{t}"').fetchone()
        except duckdb.Error:
            row = (0, 0, None, None)
        meta.update(rows=int(row[0] or 0), n_entities=int(row[1] or 0), first_date=_iso(row[2]), last_date=_iso(row[3]))
        if sid in ("cftc", "cross_asset"):
            got = (_cftc_variables if sid == "cftc" else _cross_asset_variables)(con, sid, src)
            variables += got
            meta["n_variables"] = len(got)
        elif sid == "macro":
            rows = con.execute(
                "SELECT o.series_id, any_value(m.title), any_value(m.country), any_value(CAST(m.frequency AS VARCHAR)), "
                "any_value(m.units), any_value(CAST(m.category AS VARCHAR)), count(*), count(DISTINCT o.date), "
                "min(o.date), max(o.date), any_value(m.source_id) "
                "FROM macro_observations o LEFT JOIN macro_series m USING (series_id) GROUP BY 1 ORDER BY 1").fetchall()
            for r in rows:
                variables.append({"source": sid, "field": r[0], "table": t, "title": r[1] or r[0], "country": r[2],
                                  "frequency": r[3] or "M", "units": r[4], "category": r[5], "entity_type": "macro",
                                  "n_obs": int(r[7] or 0), "n_entities": None, "first_date": _iso(r[8]),
                                  "last_date": _iso(r[9]), "pit": True})
            meta["n_variables"] = len(rows)
        elif src["layout"] == "long":
            unit_sel = "any_value(CAST(unit AS VARCHAR))" if t == "ratios" else "NULL"
            filt = "WHERE period_kind = 'TTM'" if t == "ratios" else ""
            rows = con.execute(
                f'SELECT "{src["key_col"]}", {unit_sel}, count(*), count(DISTINCT "{ent}"), min("{dcol}"), max("{dcol}") '
                f'FROM "{t}" {filt} GROUP BY 1 ORDER BY 1').fetchall()
            for r in rows:
                variables.append({"source": sid, "field": r[0], "table": t, "title": str(r[0]).replace("_", " "),
                                  "country": None, "frequency": src["native_freq"],
                                  "units": r[1] or ("score 0-100" if "scores" in t else None), "category": None,
                                  "entity_type": src["entity"], "n_obs": int(r[2] or 0), "n_entities": int(r[3] or 0),
                                  "first_date": _iso(r[4]), "last_date": _iso(r[5]),
                                  "pit": src["kind"] == "knowledge"})
            meta["n_variables"] = len(rows)
        else:
            described = [(r[0], r[1]) for r in con.execute(f"DESCRIBE \"{t}\"").fetchall()]
            cols = _numeric_columns(sid, described)
            have = {r[0] for r in described}
            sel = [c for c, _, _ in cols if c in have]
            counts: dict[str, int] = {}
            if sel and meta["rows"]:
                where = "WHERE period_kind = 'TTM' AND currency_basis = 'USD'" if t == "fundamentals_wide" else ""
                q = ", ".join(f'count("{c}")' for c in sel)
                vals = con.execute(f'SELECT {q} FROM "{t}" {where}').fetchone()
                counts = dict(zip(sel, [int(x or 0) for x in vals], strict=True))
            for c, desc, unit in cols:
                base = SOURCES[sid].get("derived", {}).get(c, c)
                n = counts.get(base, 0)
                if t == "fundamentals_wide" and n == 0:
                    continue
                variables.append({"source": sid, "field": c, "table": t, "title": desc or c.replace("_", " "),
                                  "country": None, "frequency": src["native_freq"], "units": unit, "category": None,
                                  "entity_type": src["entity"], "n_obs": n, "n_entities": meta["n_entities"],
                                  "first_date": meta["first_date"], "last_date": meta["last_date"],
                                  "pit": src["kind"] == "knowledge"})
            meta["n_variables"] = sum(1 for v in variables if v["source"] == sid)
        sources.append(meta)
    return {"sources": sources, "variables": variables, "transforms": TRANSFORM_DOCS, "instruments": _instruments(con)}


def _instruments(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Market instruments usable as `prices` entities (EntityFilter.security_ids / tickers): FX pairs, futures,
    indices (incl. volatility), ETFs, i.e. the synthetic IDX:/ETF: securities with daily history. FX pairs are Yahoo market
    rates (analysis only; conversions use the official fx_rates)."""
    try:
        rows = con.execute("""
            WITH px AS (SELECT security_id, min(date) AS d0, max(date) AS d1, count(*) AS n FROM prices_daily
                        WHERE security_id LIKE 'IDX:%' OR security_id LIKE 'ETF:%' GROUP BY 1)
            SELECT s.security_id, s.display_ticker, s.name, CAST(s.security_type AS VARCHAR), s.trading_currency,
                   px.d0, px.d1, px.n
            FROM securities_master s JOIN px USING (security_id)
            ORDER BY CAST(s.security_type AS VARCHAR), s.display_ticker""").fetchall()
    except duckdb.Error:
        return []
    return [{"security_id": r[0], "ticker": r[1], "name": r[2], "security_type": r[3], "currency": r[4],
             "first_date": _iso(r[5]), "last_date": _iso(r[6]), "n_obs": int(r[7] or 0)} for r in rows]


def list_variables(source: str | None = None, q: str | None = None, entity_type: str | None = None,
                   frequency: str | None = None, limit: int = 200, offset: int = 0,
                   settings: Settings | None = None) -> dict[str, Any]:
    cat = build_catalog(settings)
    items = cat["variables"]
    if source == "bql":  # roadmap v3 B1: BQL dictionary fields with history (field = mnemonic; any expression works)
        from .bql_source import catalog_variables

        items = catalog_variables(None)
    if source:
        items = [v for v in items if v["source"] == source]
    if entity_type:
        items = [v for v in items if v["entity_type"] == entity_type]
    if frequency:
        items = [v for v in items if v["frequency"] == frequency]
    if q:
        terms = q.lower().split()
        items = [v for v in items if all(t in f"{v['field']} {v['title']} {v.get('country') or ''}".lower() for t in terms)]
    return {"snapshot_id": cat["snapshot_id"], "total": len(items), "limit": limit, "offset": offset,
            "items": items[offset: offset + limit]}


def list_sources(settings: Settings | None = None) -> dict[str, Any]:
    cat = build_catalog(settings)
    items = list(cat["sources"])
    try:  # roadmap v3 B1: BQL expressions as a dataset source
        from .bql_source import catalog_source, catalog_variables

        items.append(catalog_source(len(catalog_variables(None))))
    except Exception:  # noqa: BLE001 - dictionary missing: the source is simply not offered
        pass
    return {"snapshot_id": cat["snapshot_id"], "items": items, "transforms": cat["transforms"],
            "universes": _universes(settings), "instruments": cat.get("instruments", [])}


def _universes(settings: Settings | None = None) -> list[str]:
    try:
        c = get_contract("universe_membership")
        return list(c.column("universe_id").values or [])
    except Exception:
        return []


__all__ = ["DatasetSpec", "Variable", "Transform", "EntityFilter", "MissingPolicy", "Dataset", "SpecError",
           "build_dataset", "preview", "build_catalog", "list_variables", "list_sources", "snapshot_id",
           "modeling_dir", "lake_connection", "dataset_key", "summary_stats", "jsonable_rows", "load_contracts"]
