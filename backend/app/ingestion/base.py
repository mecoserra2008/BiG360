"""Source adapter interface. Every data source (SEC, ESEF, EDINET, OpenDART, CVM, AKShare, Polygon, yfinance, FRED,
GDELT, ...) is one SourceAdapter subclass in app/ingestion/<source>.py, registered with @register_adapter.

Lifecycle (driven by the job module or by `adapter.run(...)`):

    discover()            -> DataFrame of entities the source can provide (DISCOVER_COLUMNS) — used by identity resolution
    fetch(since, entities)-> FetchResult: raw payloads landed under data/raw/<source_id>/<dataset>/dt=YYYY-MM-DD/
    normalize(fetched)    -> {table_name: DataFrame} conforming to contracts/schemas/<table_name>.yaml
    run()                 -> fetch + normalize + conform + write_table, returns rows per table

Rules for adapters:
  * Write only the tables listed in `produces`, and only the partitions of your own source_id where the table is shared.
  * Key rows by source-native ids (source_entity_id). Fill issuer_id/security_id ONLY if already resolvable via
    app.normalize.identity.lookup(); otherwise leave null — the identity_resolve job back-fills them.
  * Every fundamental/insider/news/filing row MUST carry knowledge_date (+ knowledge_date_estimated where applicable).
  * Never log or persist API key values. Use self.http (rate-limited, cached, retried) for all HTTP.
  * fetch() must be idempotent and resumable: land raw files first, then normalize from disk.
"""

from __future__ import annotations

import gzip
import json
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.http import HttpClient
from app.core.logsetup import get_logger
from app.core.ratelimit import RateLimit
from app.core.schema import conform
from app.core.storage import write_table

#: Columns returned by discover(). Only source_entity_id and name are required; others nullable strings.
DISCOVER_COLUMNS: dict[str, pl.DataType] = {
    "source_id": pl.Utf8(),
    "source_entity_id": pl.Utf8(),
    "name": pl.Utf8(),
    "name_local": pl.Utf8(),
    "country": pl.Utf8(),        # ISO2
    "cik": pl.Utf8(),
    "lei": pl.Utf8(),
    "isin": pl.Utf8(),
    "figi": pl.Utf8(),
    "ticker": pl.Utf8(),         # local symbol
    "mic": pl.Utf8(),
    "yf_ticker": pl.Utf8(),
    "edinet_code": pl.Utf8(),
    "jp_sec_code": pl.Utf8(),
    "dart_corp_code": pl.Utf8(),
    "kr_stock_code": pl.Utf8(),
    "cvm_code": pl.Utf8(),
    "cnpj": pl.Utf8(),
    "cn_code": pl.Utf8(),
    "industry_code": pl.Utf8(),  # SIC / TSE33 / KSIC / CSRC as given
    "industry_scheme": pl.Utf8(),  # sic | tse33 | ksic | csrc | cvm | yf_industry
    "is_active": pl.Boolean(),
}


class MissingApiKey(RuntimeError):
    pass


@dataclass
class FetchResult:
    source_id: str
    started_at: datetime
    finished_at: datetime | None = None
    raw_paths: list[Path] = field(default_factory=list)         # landed files (json.gz, zip, csv, parquet ...)
    entities_ok: list[str] = field(default_factory=list)
    entities_failed: dict[str, str] = field(default_factory=dict)  # entity -> error message (redacted)
    watermark: str | None = None                                  # new incremental cursor (ISO date/ts or opaque)
    n_requests: int = 0
    n_cache_hits: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.entities_failed and not self.entities_ok:
            return "failed"
        return "partial" if self.entities_failed else "success"


ADAPTERS: dict[str, type["SourceAdapter"]] = {}


def register_adapter(cls: type["SourceAdapter"]) -> type["SourceAdapter"]:
    if not getattr(cls, "source_id", None):
        raise TypeError(f"{cls.__name__} must define source_id")
    if cls.source_id in ADAPTERS and ADAPTERS[cls.source_id] is not cls:
        raise ValueError(f"duplicate adapter source_id {cls.source_id!r}")
    ADAPTERS[cls.source_id] = cls
    return cls


def get_adapter(source_id: str, **kwargs: Any) -> "SourceAdapter":
    return ADAPTERS[source_id](**kwargs)


class SourceAdapter(ABC):
    # ---- class-level declaration (override in subclasses) -------------------------------------------
    source_id: ClassVar[str] = ""
    #: regions covered (values of _conventions.region) — informational, used by status page
    regions: ClassVar[tuple[str, ...]] = ()
    #: contract tables this adapter writes
    produces: ClassVar[tuple[str, ...]] = ()
    rate_limit: ClassVar[RateLimit | None] = None   # None -> app.core.ratelimit.DEFAULT_LIMITS[source_id]
    #: settings attribute names of required secrets, e.g. ("polygon_apikey",)
    requires_keys: ClassVar[tuple[str, ...]] = ()
    base_url: ClassVar[str] = ""
    #: default HTTP cache TTL for this source (seconds; -1 forever, 0 off)
    cache_ttl: ClassVar[int] = 6 * 3600

    def __init__(self, settings: Settings | None = None, http: HttpClient | None = None):
        self.settings = settings or get_settings()
        self.log = get_logger(f"app.ingestion.{self.source_id}")
        self.http = http or HttpClient(self.source_id, base_url=self.base_url, rate_limit=self.rate_limit, settings=self.settings)

    # ---- required interface ----------------------------------------------------------------------------
    @abstractmethod
    def discover(self) -> pl.DataFrame:
        """Entities available from this source (DISCOVER_COLUMNS subset). Cheap; may be cached by the source."""

    @abstractmethod
    def fetch(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None) -> FetchResult:
        """Download new raw data since the watermark (None = full backfill) for the given source_entity_ids
        (None = all in the universe). Must land raw payloads via save_raw() before returning."""

    @abstractmethod
    def normalize(self, fetched: FetchResult) -> dict[str, pl.DataFrame]:
        """Turn landed raw payloads into contract tables. Keys must be a subset of `produces`."""

    # ---- helpers ------------------------------------------------------------------------------------------
    def check_keys(self) -> None:
        missing = [k for k in self.requires_keys if self.settings.secret(k) is None]
        if missing:
            raise MissingApiKey(f"{self.source_id}: missing {', '.join(k.upper() for k in missing)} in .env")

    def key(self, name: str) -> str:
        """Plain secret value for building a request. Never log the return value."""
        v = self.settings.secret(name)
        if v is None:
            raise MissingApiKey(f"{self.source_id}: missing {name.upper()}")
        return v

    def raw_dir(self, dataset: str, day: date | None = None) -> Path:
        d = day or datetime.now(UTC).date()
        p = self.settings.raw_dir / self.source_id / dataset / f"dt={d.isoformat()}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def save_raw(self, dataset: str, name: str, content: bytes | str | dict | list, day: date | None = None,
                 compress: bool = True) -> Path:
        """Land a raw payload. dict/list -> JSON. Compressed with gzip unless already binary-compressed."""
        d = self.raw_dir(dataset, day)
        if isinstance(content, (dict, list)):
            data, ext = orjson.dumps(content), ".json"
        elif isinstance(content, str):
            data, ext = content.encode("utf-8"), ""
        else:
            data, ext = content, ""
        path = d / f"{name}{ext}{'.gz' if compress else ''}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(gzip.compress(data, compresslevel=5) if compress else data)
        tmp.replace(path)
        return path

    @staticmethod
    def load_raw(path: Path) -> bytes:
        data = path.read_bytes()
        return gzip.decompress(data) if path.suffix == ".gz" else data

    @classmethod
    def load_raw_json(cls, path: Path) -> Any:
        return json.loads(cls.load_raw(path))

    @staticmethod
    def discover_frame(rows: list[dict[str, Any]], source_id: str) -> pl.DataFrame:
        """Build a discover() frame with the standard columns (missing -> null)."""
        df = pl.DataFrame(rows) if rows else pl.DataFrame(schema={"source_entity_id": pl.Utf8(), "name": pl.Utf8()})
        exprs = []
        for col, dt in DISCOVER_COLUMNS.items():
            if col == "source_id":
                exprs.append(pl.lit(source_id, dtype=pl.Utf8()).alias(col))
            elif col in df.columns:
                exprs.append(pl.col(col).cast(dt))
            else:
                exprs.append(pl.lit(None, dtype=dt).alias(col))
        return df.select(exprs)

    def run(self, since: date | datetime | str | None = None, entities: Sequence[str] | None = None,
            write: bool = True) -> tuple[FetchResult, dict[str, int]]:
        self.check_keys()
        fetched = self.fetch(since=since, entities=entities)
        fetched.n_requests = self.http.n_requests
        fetched.n_cache_hits = self.http.n_cache_hits
        tables = self.normalize(fetched)
        unknown = set(tables) - set(self.produces)
        if unknown:
            raise ValueError(f"{self.source_id}.normalize returned tables not in produces: {sorted(unknown)}")
        rows: dict[str, int] = {}
        for name, df in tables.items():
            df = conform(df, name)
            rows[name] = write_table(df, name, settings=self.settings) if write else df.height
        fetched.finished_at = datetime.now(UTC)
        self.log.info("%s run: status=%s rows=%s requests=%d cache_hits=%d", self.source_id, fetched.status, rows,
                      fetched.n_requests, fetched.n_cache_hits)
        return fetched, rows
