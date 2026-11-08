"""Licensed market-data vendor adapter interface (O4 Stage B; docs/it/DATA_LICENSING.md section 4).

In a bank deployment the unlicensed price / fundamentals sources (Yahoo via yfinance, AKShare, BaoStock, exchange
web pages) are replaced by a licensed feed: Bloomberg (B-PIPE / Data License / Server API), LSEG (Refinitiv Data
Platform / DataScope Select), FactSet, or the bank's internal market-data service. Such a feed plugs in here and
writes the SAME staging tables as today's adapters, so everything downstream (prices_finalize identity / unit / FX
guards, corporate actions, valuation, scoring, ML, serving) is unchanged:

    vendor.eod_prices(...)         -> prices_staging            (contracts/schemas/prices_staging.yaml)
    vendor.corporate_actions(...)  -> corporate_actions_staging (contracts/schemas/corporate_actions_staging.yaml)
    vendor.fundamentals(...)       -> fundamentals_raw          (contracts/schemas/fundamentals_raw.yaml, point-in-time)
    vendor.resolve(...)            -> DISCOVER_COLUMNS frame    (identity: our ISIN / FIGI / ticker+MIC -> vendor symbol)

Contract notes for a real adapter:
* prices_staging.source_id / corporate_actions_staging.source_id are enums; rows of a new vendor use `other` until the
  vendor id is added to the enum (additive schema change, logged in docs/CONTRACT_CHANGES.md) together with its
  precedence in app.normalize.prices.
* fundamentals must be point-in-time: knowledge_date = the date the figure became known (vendor "as reported" /
  point-in-time history), never the period end. Estimated dates set knowledge_date_estimated = true.
* Prices are end-of-day closes in the quote currency as delivered (minor units such as GBp allowed); price_basis raw
  (unadjusted) is preferred, split_adjusted accepted.
* Credentials come from Settings / Windows Credential Manager / Key Vault, never from code; never log them.
* Licences: `licence` must describe the bank's actual entitlement (internal display, derived data, redistribution).

Implemented here:
* `MarketDataVendor` - the typing.Protocol a vendor implements (runtime-checkable).
* `FileDropVendor` - working adapter for vendor end-of-day files dropped in a folder (Bloomberg Data License /
  DataScope Select / FactSet bulk deliveries usually arrive as per-request CSV files over SFTP): reads CSV / Parquet
  with a column mapping.
* `BloombergVendor`, `LsegVendor`, `FactSetVendor`, `InternalServiceVendor` - skeletons naming the SDK / endpoint each
  would use; they raise NotImplementedError until the bank's entitlement and SDK are available.
* `ingest_prices(vendor, ...)` / `ingest_corporate_actions(...)` / `ingest_fundamentals(...)` - conform to the contract
  and write the staging table (the job body a jobs.yaml entry such as `prices_vendor` would call).
* `get_vendor(name)` - registry; BIG360_PRICE_VENDOR selects one (not wired into the scheduled DAGs yet).
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.schema import conform

log = get_logger("app.ingestion.vendors")

CAPABILITIES = frozenset({"eod_prices", "corporate_actions", "fundamentals", "reference", "fx"})


@dataclass(frozen=True)
class VendorLicence:
    """What the bank's entitlement allows. Shown in docs and on the System page licence panel (future)."""

    vendor: str
    terms_url: str | None = None
    commercial_use: bool = False
    internal_redistribution: bool = False   # may derived data be shown to other employees / systems?
    external_redistribution: bool = False
    attribution: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class InstrumentRef:
    """Our identifiers for one listing; a vendor maps them to its own symbol (resolve)."""

    security_id: str | None = None
    isin: str | None = None
    figi: str | None = None
    ticker: str | None = None
    mic: str | None = None
    currency: str | None = None
    vendor_symbol: str | None = None   # when already known (e.g. "AAPL US Equity", "AAPL.O", "AAPL-US")


@runtime_checkable
class MarketDataVendor(Protocol):
    """Interface of a licensed price / fundamentals vendor. Return frames with the contract column names of the
    target table (extra columns are rejected by conform); missing nullable columns are added as nulls."""

    vendor_id: str
    licence: VendorLicence

    def capabilities(self) -> frozenset[str]:
        """Subset of CAPABILITIES this adapter (and the bank's entitlement) supports."""
        ...

    def resolve(self, refs: Sequence[InstrumentRef]) -> pl.DataFrame:
        """Map our identifiers to vendor symbols. Columns: source_entity_id (vendor symbol), name, isin, figi,
        ticker, mic, country (ISO2) - a subset of app.ingestion.base.DISCOVER_COLUMNS."""
        ...

    def eod_prices(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        """End-of-day bars -> prices_staging columns: symbol, date, open, high, low, close, adj_close, volume, vwap,
        trades, currency, price_basis (raw | split_adjusted). source_id / year / ingested_at are filled by
        ingest_prices."""
        ...

    def corporate_actions(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        """Splits / dividends -> corporate_actions_staging columns: symbol, ex_date, action_type, ratio, amount,
        amount_basis, currency, record_date, pay_date, declared_date, details."""
        ...

    def fundamentals(self, entities: Sequence[str], since: date | None = None) -> pl.DataFrame:
        """Point-in-time reported figures -> fundamentals_raw columns (source_entity_id, filing_id, taxonomy 'other',
        concept = vendor field mnemonic, period_type, period_start, period_end, fiscal_period, value, unit,
        dimensions_hash '', knowledge_date, knowledge_date_estimated, fiscal_year)."""
        ...


# ------------------------------------------------------------------------------------------------ file drop
@dataclass
class FileDropVendor:
    """Vendor end-of-day files dropped in `root` (default data/vendor_drop/<vendor_id>/):

        prices/*.csv|*.parquet              one row per symbol x date
        corporate_actions/*.csv|*.parquet   optional
        fundamentals/*.csv|*.parquet        optional, point-in-time rows

    `column_map` maps vendor column names to contract names (e.g. {"PX_LAST": "close", "TICKER": "symbol",
    "DATE": "date", "CRNCY": "currency"}); unmapped vendor columns are dropped.
    """

    vendor_id: str = "file_drop"
    root: Path | None = None
    column_map: dict[str, str] = field(default_factory=dict)
    licence: VendorLicence = field(default_factory=lambda: VendorLicence(
        vendor="file drop", notes="Licence of whoever delivered the files - set per deployment"))
    price_basis: str = "raw"
    date_format: str | None = None

    def _dir(self, kind: str, settings: Settings | None = None) -> Path:
        base = self.root or ((settings or get_settings()).data_dir / "vendor_drop" / self.vendor_id)
        return Path(base) / kind

    def capabilities(self) -> frozenset[str]:
        caps = {"eod_prices"}
        for k in ("corporate_actions", "fundamentals"):
            if self._dir(k).is_dir():
                caps.add(k)
        return frozenset(caps)

    def _read(self, kind: str) -> pl.DataFrame:
        d = self._dir(kind)
        frames = []
        for p in sorted(d.glob("*")) if d.is_dir() else []:
            if p.suffix.lower() == ".csv":
                frames.append(pl.read_csv(p, infer_schema_length=10000, try_parse_dates=False))
            elif p.suffix.lower() == ".parquet":
                frames.append(pl.read_parquet(p))
        if not frames:
            return pl.DataFrame()
        df = pl.concat(frames, how="diagonal_relaxed")
        ren = {k: v for k, v in self.column_map.items() if k in df.columns}
        return df.rename(ren)

    def _dates(self, df: pl.DataFrame, cols: Sequence[str]) -> pl.DataFrame:
        for c in cols:
            if c in df.columns and df[c].dtype == pl.Utf8:
                df = df.with_columns(pl.col(c).str.to_date(self.date_format, strict=False))
        return df

    def resolve(self, refs: Sequence[InstrumentRef]) -> pl.DataFrame:
        return pl.DataFrame({"source_id": [self.vendor_id] * len(refs),
                             "source_entity_id": [r.vendor_symbol or r.ticker or r.isin for r in refs],
                             "isin": [r.isin for r in refs], "figi": [r.figi for r in refs],
                             "ticker": [r.ticker for r in refs], "mic": [r.mic for r in refs]})

    def eod_prices(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        df = self._dates(self._read("prices"), ["date"])
        if df.is_empty():
            return df
        keep = [c for c in ("symbol", "date", "open", "high", "low", "close", "adj_close", "volume", "vwap", "trades",
                            "currency", "price_basis") if c in df.columns]
        df = df.select(keep)
        if "price_basis" not in df.columns:
            df = df.with_columns(pl.lit(self.price_basis).alias("price_basis"))
        df = df.filter(pl.col("date").is_between(start, end))
        if symbols:
            df = df.filter(pl.col("symbol").is_in(list(symbols)))
        return df

    def corporate_actions(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        df = self._dates(self._read("corporate_actions"), ["ex_date", "record_date", "pay_date", "declared_date"])
        if df.is_empty():
            return df
        df = df.filter(pl.col("ex_date").is_between(start, end))
        return df.filter(pl.col("symbol").is_in(list(symbols))) if symbols else df

    def fundamentals(self, entities: Sequence[str], since: date | None = None) -> pl.DataFrame:
        df = self._dates(self._read("fundamentals"), ["period_start", "period_end", "knowledge_date"])
        if df.is_empty():
            return df
        if since is not None:
            df = df.filter(pl.col("knowledge_date") >= since)
        return df.filter(pl.col("source_entity_id").is_in(list(entities))) if entities else df


# ------------------------------------------------------------------------------------------------ skeletons
class _SkeletonVendor:
    vendor_id: ClassVar[str] = "skeleton"
    sdk: ClassVar[str] = ""
    licence: VendorLicence = VendorLicence(vendor="skeleton")

    def __init__(self, settings: Settings | None = None, **options: Any):
        self.settings = settings or get_settings()
        self.options = options

    def capabilities(self) -> frozenset[str]:
        return frozenset()

    def _ni(self, what: str) -> Any:
        raise NotImplementedError(f"{self.vendor_id}: {what} needs the bank's entitlement and {self.sdk} "
                                  "(docs/it/DATA_LICENSING.md section 4)")

    def resolve(self, refs: Sequence[InstrumentRef]) -> pl.DataFrame:
        return self._ni("resolve")

    def eod_prices(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        return self._ni("eod_prices")

    def corporate_actions(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        return self._ni("corporate_actions")

    def fundamentals(self, entities: Sequence[str], since: date | None = None) -> pl.DataFrame:
        return self._ni("fundamentals")


class BloombergVendor(_SkeletonVendor):
    """Bloomberg: Server API / B-PIPE via `blpapi` (HistoricalDataRequest PX_OPEN/PX_HIGH/PX_LOW/PX_LAST/PX_VOLUME,
    CRNCY; DVD_HIST_ALL, EQY_SPLIT_*), or Data License per-request files (use FileDropVendor with a column map).
    Symbology: '<ticker> <exch> Equity' or /isin/<ISIN>; FIGI maps 1:1 (OpenFIGI is Bloomberg's)."""

    vendor_id = "bloomberg"
    sdk = "blpapi (Bloomberg Server API / B-PIPE) or Data License"
    licence = VendorLicence(vendor="Bloomberg", terms_url="https://www.bloomberg.com/professional/support/",
                            commercial_use=True, notes="Per the bank's Bloomberg agreement (display vs non-display / "
                            "derived-data rights must cover a shared internal dashboard)")


class LsegVendor(_SkeletonVendor):
    """LSEG: Data Library for Python (`lseg-data`: get_history(fields TRDPRC_1/OPEN_PRC/HIGH_1/LOW_1/ACVOL_UNS)),
    Refinitiv Data Platform REST, or DataScope Select extractions (FileDropVendor). Symbology: RIC (AAPL.O)."""

    vendor_id = "lseg"
    sdk = "lseg-data (Data Library) / RDP REST / DataScope Select"
    licence = VendorLicence(vendor="LSEG", terms_url="https://www.lseg.com/en/policies", commercial_use=True,
                            notes="Per the bank's LSEG agreement")


class FactSetVendor(_SkeletonVendor):
    """FactSet: Prices API / Fundamentals API / Formula API (REST, OAuth client credentials). Symbology: ticker-region
    (AAPL-US) or FactSet Permanent ID."""

    vendor_id = "factset"
    sdk = "FactSet Prices / Fundamentals / Formula APIs"
    licence = VendorLicence(vendor="FactSet", terms_url="https://www.factset.com/terms", commercial_use=True,
                            notes="Per the bank's FactSet agreement")


class InternalServiceVendor(_SkeletonVendor):
    """The bank's own market-data service (golden source), typically REST / gRPC behind the API gateway with an Entra ID
    client-credentials token. Map its instrument master to ISIN / FIGI in resolve()."""

    vendor_id = "internal"
    sdk = "the bank's market-data service API"
    licence = VendorLicence(vendor="internal market-data service", commercial_use=True, internal_redistribution=True,
                            notes="Entitlements inherited from the bank's upstream vendor contracts")


VENDORS: dict[str, Any] = {"file_drop": FileDropVendor, "bloomberg": BloombergVendor, "lseg": LsegVendor,
                           "factset": FactSetVendor, "internal": InternalServiceVendor}


def get_vendor(name: str | None = None, settings: Settings | None = None, **options: Any) -> MarketDataVendor | None:
    name = (name or os.environ.get("BIG360_PRICE_VENDOR") or "").strip().lower()
    if not name or name == "none":
        return None
    cls = VENDORS.get(name)
    if cls is None:
        raise KeyError(f"unknown vendor {name!r}; known: {sorted(VENDORS)}")
    if cls is FileDropVendor:
        return FileDropVendor(**options)
    return cls(settings=settings, **options)


# ------------------------------------------------------------------------------------------------ ingest
def _staging_source_id(vendor: MarketDataVendor, table: str) -> str:
    from app.core.schema import get_contract  # noqa: PLC0415

    col = next((c for c in get_contract(table).columns if c.name == "source_id"), None)
    values = getattr(col, "values", None) or []
    return vendor.vendor_id if (not values or vendor.vendor_id in values) else "other"


def prices_frame(vendor: MarketDataVendor, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
    """Vendor bars conformed to prices_staging (not written)."""
    df = vendor.eod_prices(symbols, start, end)
    if df.is_empty():
        from app.core.schema import empty_frame  # noqa: PLC0415

        return empty_frame("prices_staging")
    now = datetime.now(UTC)
    df = df.with_columns(pl.lit(_staging_source_id(vendor, "prices_staging")).alias("source_id"),
                         pl.col("date").dt.year().cast(pl.Int16).alias("year"),
                         pl.lit(now).alias("ingested_at"))
    return conform(df, "prices_staging")


def ingest_prices(vendor: MarketDataVendor, symbols: Sequence[str], start: date, end: date,
                  settings: Settings | None = None, write: bool = True) -> int:
    from app.core.storage import write_table  # noqa: PLC0415

    df = prices_frame(vendor, symbols, start, end)
    log.info("vendor %s: %d price rows %s..%s", vendor.vendor_id, df.height, start, end)
    return write_table(df, "prices_staging", settings=settings) if write and df.height else df.height


def ingest_corporate_actions(vendor: MarketDataVendor, symbols: Sequence[str], start: date, end: date,
                             settings: Settings | None = None, write: bool = True) -> int:
    from app.core.storage import write_table  # noqa: PLC0415

    df = vendor.corporate_actions(symbols, start, end)
    if df.is_empty():
        return 0
    df = conform(df.with_columns(pl.lit(_staging_source_id(vendor, "corporate_actions_staging")).alias("source_id"),
                                 pl.lit(datetime.now(UTC)).alias("ingested_at")), "corporate_actions_staging")
    return write_table(df, "corporate_actions_staging", settings=settings) if write else df.height


def ingest_fundamentals(vendor: MarketDataVendor, entities: Sequence[str], since: date | None = None,
                        settings: Settings | None = None, write: bool = True) -> int:
    from app.core.storage import write_table  # noqa: PLC0415

    df = vendor.fundamentals(entities, since)
    if df.is_empty():
        return 0
    extra = {"source_id": pl.lit(f"vendor_{vendor.vendor_id}"), "ingested_at": pl.lit(datetime.now(UTC))}
    if "fiscal_year" not in df.columns:
        extra["fiscal_year"] = pl.col("period_end").dt.year().cast(pl.Int16)
    if "filing_id" not in df.columns:
        extra["filing_id"] = pl.lit(f"vendor_{vendor.vendor_id}:na")
    if "dimensions_hash" not in df.columns:
        extra["dimensions_hash"] = pl.lit("")
    if "knowledge_date_estimated" not in df.columns:
        extra["knowledge_date_estimated"] = pl.lit(False)
    if "taxonomy" not in df.columns:
        extra["taxonomy"] = pl.lit("other")
    df = conform(df.with_columns(**extra), "fundamentals_raw")
    return write_table(df, "fundamentals_raw", settings=settings) if write else df.height
