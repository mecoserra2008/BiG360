"""Snapshot export for data-level integration (O4 Stage C pattern 3: SQL Server / BI / risk systems).

Exports a fixed set of *published datasets* from the current serving snapshot (read-only; never blocks the API) as
SqlBulkCopy / BULK INSERT friendly CSV or as Parquet, with a manifest and SQL Server DDL:

    python -m app.ops.export --out D:\\exports\\big360 --format parquet
    python -m app.ops.export --out D:\\exports\\big360 --format csv --datasets securities,fundamentals
    python -m app.ops.export --out ... --prices-since 2025-01-01          # limit the 65M-row price history

Output (<out>/<run_id>/):
    <dataset>.csv | <dataset>.parquet    CSV: UTF-8 (no BOM), header, comma, RFC 4180 quoting, ISO dates,
                                         '.' decimal, empty field = NULL (BULK INSERT ... FORMAT = 'CSV')
    manifest.json                        snapshot run id / as-of, export contract version, row counts, columns, types
    create_tables.sql                    T-SQL CREATE TABLE for each dataset (schema [big360])

The published column lists below are the *export contract* (EXPORT_VERSION): columns are only added, never renamed
or removed within a major version - the same rule as contracts/api.yaml. The serving snapshot's internal tables
behind them are NOT a contract and may change between releases; integrate against the export or the REST API.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.ops.export")

EXPORT_VERSION = "1.0"


@dataclass(frozen=True)
class Dataset:
    name: str
    table: str
    columns: tuple[str, ...]
    key: tuple[str, ...]
    description: str
    date_col: str | None = None   # for --prices-since style filters


DATASETS: dict[str, Dataset] = {d.name: d for d in (
    Dataset("securities", "sec", (
        "security_id", "issuer_id", "ticker", "name", "isin", "figi", "lei", "cusip", "mic", "exchange_name", "country",
        "region", "sector_id", "subsector_id", "currency", "security_type", "is_active", "is_primary_security",
        "px_date", "close", "close_usd", "chg_1d", "chg_1m", "chg_ytd", "ret_12m", "vol_1y", "beta_1y", "adv_usd_20d",
        "market_cap_usd", "enterprise_value_usd", "pe_ttm", "pe_ntm", "pb", "ev_ebitda", "ev_ebit", "fcf_yield",
        "dividend_yield", "f_quality", "f_profitability", "f_growth", "f_value", "f_balance_sheet", "f_momentum",
        "f_insider", "f_sentiment", "f_macro", "f_moat", "c_balanced", "g_balanced", "rk_balanced", "moat_class",
        "moat_score", "fv_weighted", "fv_upside", "fv_currency", "dq_score", "score_date", "val_date",
    ), ("security_id",), "One row per security: identity, latest quote, valuation, factor / composite scores "
       "(0-100, model-based), moat, fair value (model-based)"),
    Dataset("prices_daily", "prices", (
        "security_id", "date", "open", "high", "low", "close", "adj_close", "adj_factor", "split_factor", "volume",
        "currency", "close_usd",
    ), ("security_id", "date"), "Daily bars (last 15 years; quote currency + USD close)", "date"),
    Dataset("fundamentals", "fund", (
        "issuer_id", "item_id", "fiscal_year", "fiscal_period", "period_kind", "period_start", "period_end", "value",
        "currency", "value_usd", "gaap", "derivation", "knowledge_date", "knowledge_date_estimated", "is_restatement",
        "dq_score",
    ), ("issuer_id", "item_id", "period_kind", "period_end", "knowledge_date"),
       "Canonical financial statement items, point-in-time (knowledge_date = filing date)", "knowledge_date"),
    Dataset("metrics", "metrics", (
        "issuer_id", "metric_id", "src", "kpi_set_id", "period_kind", "fiscal_year", "fiscal_period", "period_end",
        "value", "unit", "knowledge_date",
    ), ("issuer_id", "metric_id", "period_kind", "period_end"), "Ratios and sector KPIs per period", "knowledge_date"),
)}

_TSQL = [
    (r"^VARCHAR\[\]$|\[\]$|^STRUCT|^MAP|^JSON", "NVARCHAR(MAX)"),
    (r"^VARCHAR", "NVARCHAR(400)"), (r"^DOUBLE", "FLOAT"), (r"^FLOAT|^REAL", "REAL"), (r"^BIGINT|^HUGEINT", "BIGINT"),
    (r"^INTEGER", "INT"), (r"^SMALLINT", "SMALLINT"), (r"^TINYINT", "TINYINT"), (r"^BOOLEAN", "BIT"),
    (r"^DATE$", "DATE"), (r"^TIMESTAMP", "DATETIME2(3)"), (r"^DECIMAL", "DECIMAL(38,10)"),
]
_KEY_LEN = {"security_id": 32, "issuer_id": 32, "item_id": 80, "metric_id": 80, "period_kind": 16, "ticker": 40,
            "isin": 12, "figi": 12, "lei": 20, "cusip": 9, "mic": 10, "currency": 8, "country": 2, "region": 8}


def tsql_type(duck_type: str, col: str) -> str:
    for rx, t in _TSQL:
        if re.search(rx, duck_type.upper()):
            if t == "NVARCHAR(400)" and col in _KEY_LEN:
                return f"NVARCHAR({_KEY_LEN[col]})"
            if t == "NVARCHAR(400)" and col in ("name", "description", "exchange_name"):
                return "NVARCHAR(1000)"
            return t
    return "NVARCHAR(MAX)"


def _snapshot_path(s: Settings) -> Path:
    cur = s.serving_pointer_path
    if not cur.exists():
        raise FileNotFoundError(f"no serving snapshot published ({cur})")
    return s.db_dir / cur.read_text(encoding="utf-8").strip()


def export_snapshot(out: Path, fmt: str = "parquet", datasets: list[str] | None = None, prices_since: date | None = None,
                    since: date | None = None, settings: Settings | None = None, snapshot: Path | None = None,
                    memory_limit: str = "2GB", threads: int = 4) -> dict[str, Any]:
    s = settings or get_settings()
    if fmt not in ("csv", "parquet"):
        raise ValueError("format must be csv or parquet")
    names = datasets or list(DATASETS)
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        raise KeyError(f"unknown datasets {unknown}; known: {sorted(DATASETS)}")
    snap = snapshot or _snapshot_path(s)
    con = duckdb.connect(str(snap), read_only=True, config={"memory_limit": memory_limit, "threads": threads})
    try:
        meta = {}
        try:
            meta = dict(con.execute("SELECT key, value FROM _meta").fetchall())
        except duckdb.Error:
            pass
        run_id = str(meta.get("run_id") or snap.stem.removeprefix("serving_"))
        target = Path(out) / run_id
        target.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {
            "export_version": EXPORT_VERSION, "format": fmt, "snapshot_run_id": run_id,
            "snapshot_as_of": meta.get("as_of"), "snapshot_built_at": meta.get("built_at"),
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"), "datasets": {},
        }
        ddl = ["-- BiG Equity 360 snapshot export - SQL Server DDL (export contract %s)" % EXPORT_VERSION,
               "IF SCHEMA_ID(N'big360') IS NULL EXEC(N'CREATE SCHEMA big360');", ""]
        for n in names:
            d = DATASETS[n]
            have = dict(con.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = ?",
                                    [d.table]).fetchall())
            if not have:
                log.warning("export: table %s missing in %s - %s skipped", d.table, snap.name, n)
                manifest["datasets"][n] = {"status": "missing"}
                continue
            cols = [c for c in d.columns if c in have]
            missing = [c for c in d.columns if c not in have]
            sel = ", ".join(f'"{c}"' for c in cols)
            where = ""
            cut = prices_since if n == "prices_daily" else since
            if cut is not None and d.date_col:
                where = f" WHERE \"{d.date_col}\" >= DATE '{cut.isoformat()}'"
            order = ", ".join(f'"{c}"' for c in d.key if c in cols)
            q = f'SELECT {sel} FROM "{d.table}"{where}' + (f" ORDER BY {order}" if order else "")
            path = target / f"{n}.{fmt}"
            opts = "(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 500000)" if fmt == "parquet" else \
                "(FORMAT CSV, HEADER, DELIMITER ',', QUOTE '\"', DATEFORMAT '%Y-%m-%d', TIMESTAMPFORMAT '%Y-%m-%dT%H:%M:%S.%g')"
            con.execute(f"COPY ({q}) TO '{path.as_posix()}' {opts}")
            rows = con.execute(f'SELECT count(*) FROM "{d.table}"{where}').fetchone()[0]
            manifest["datasets"][n] = {
                "status": "ok", "file": path.name, "rows": int(rows), "bytes": path.stat().st_size,
                "key": [k for k in d.key if k in cols], "description": d.description,
                "columns": [{"name": c, "duckdb_type": have[c], "sqlserver_type": tsql_type(have[c], c)} for c in cols],
                "columns_not_in_snapshot": missing, "filter": where.strip() or None,
            }
            col_ddl = ",\n".join(f"    [{c}] {tsql_type(have[c], c)}{' NOT NULL' if c in d.key else ' NULL'}" for c in cols)
            pk = ", ".join(f"[{k}]" for k in d.key if k in cols)
            ddl += [f"CREATE TABLE big360.[{n}] (\n{col_ddl}" + (f",\n    CONSTRAINT [PK_big360_{n}] PRIMARY KEY ({pk})" if pk else "")
                    + "\n);", ""]
            log.info("export %s: %s rows -> %s", n, rows, path)
        if fmt == "csv":
            ddl += ["-- Load example (SQL Server 2017+):",
                    *[f"-- BULK INSERT big360.[{n}] FROM '<share>\\{run_id}\\{n}.csv' WITH (FORMAT = 'CSV', FIRSTROW = 2, "
                      "CODEPAGE = '65001', FIELDQUOTE = '\"', TABLOCK);" for n in names if manifest["datasets"].get(n, {}).get("status") == "ok"]]
        (target / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
        (target / "create_tables.sql").write_text("\n".join(ddl) + "\n", encoding="utf-8")
        manifest["path"] = str(target)
        return manifest
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    from app.core.logsetup import setup_logging

    ap = argparse.ArgumentParser(prog="app.ops.export", description="Export published datasets from the serving snapshot")
    ap.add_argument("--out", required=True)
    ap.add_argument("--format", default="parquet", choices=["parquet", "csv"])
    ap.add_argument("--datasets", default=",".join(DATASETS), help=f"comma list of {sorted(DATASETS)}")
    ap.add_argument("--prices-since", type=date.fromisoformat, default=None)
    ap.add_argument("--since", type=date.fromisoformat, default=None, help="knowledge_date filter for fundamentals / metrics")
    ap.add_argument("--memory-limit", default="2GB")
    a = ap.parse_args(argv)
    setup_logging(log_file="export.log", service="export")
    m = export_snapshot(Path(a.out), a.format, [x for x in a.datasets.split(",") if x], a.prices_since, a.since,
                        memory_limit=a.memory_limit)
    for n, d in m["datasets"].items():
        print(f"{n:14s} {d.get('status'):8s} {d.get('rows', 0):>12,} rows  {d.get('file', '')}")
    print(f"-> {m['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
