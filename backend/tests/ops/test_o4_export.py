"""O4: snapshot export (app.ops.export) - published datasets, manifest, SQL Server DDL, CSV conventions."""

from __future__ import annotations

import json
from datetime import date

import duckdb
import polars as pl

from app.ops import export as ex


def _snapshot(path):
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE _meta (key VARCHAR, value VARCHAR)")
    con.execute("INSERT INTO _meta VALUES ('run_id', 'RTEST1'), ('as_of', '2026-10-19'), ('built_at', '2026-10-20T00:00:00Z')")
    con.execute("CREATE TABLE sec (security_id VARCHAR, issuer_id VARCHAR, ticker VARCHAR, name VARCHAR, isin VARCHAR, "
                "is_active BOOLEAN, px_date DATE, close DOUBLE, c_balanced DOUBLE, internal_only VARCHAR)")
    con.execute("INSERT INTO sec VALUES ('S1', 'I1', 'AAA', 'Alpha, Inc. \"A\"', 'US0000000001', true, DATE '2026-10-19', "
                "10.5, 71.2, 'x'), ('S2', 'I2', 'BBB', 'Beta', NULL, false, NULL, NULL, NULL, 'y')")
    con.execute("CREATE TABLE prices (security_id VARCHAR, date DATE, open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, "
                "adj_close DOUBLE, adj_factor DOUBLE, split_factor DOUBLE, volume DOUBLE, currency VARCHAR, close_usd DOUBLE)")
    con.execute("INSERT INTO prices SELECT 'S1', DATE '2026-01-01' + CAST(i AS INTEGER), 1, 1, 1, 1, 1, 1, 1, 100, 'USD', 1 FROM range(300) t(i)")
    con.close()


def test_export_csv_and_parquet(tmp_path):
    snap = tmp_path / "serving_RTEST1.duckdb"
    _snapshot(snap)
    m = ex.export_snapshot(tmp_path / "out", "csv", ["securities", "prices_daily", "fundamentals"],
                           prices_since=date(2026, 10, 1), snapshot=snap)
    d = m["datasets"]
    assert m["snapshot_run_id"] == "RTEST1" and m["snapshot_as_of"] == "2026-10-19"
    assert d["securities"]["rows"] == 2 and "internal_only" not in [c["name"] for c in d["securities"]["columns"]]
    assert "figi" in d["securities"]["columns_not_in_snapshot"]
    assert d["prices_daily"]["rows"] == 300 - 273 and d["fundamentals"]["status"] == "missing"
    out = tmp_path / "out" / "RTEST1"
    df = pl.read_csv(out / "securities.csv")
    assert df["name"].to_list() == ['Alpha, Inc. "A"', "Beta"] and df["isin"][1] is None
    assert (out / "securities.csv").read_text(encoding="utf-8").splitlines()[1].count("2026-10-19") == 1
    sql = (out / "create_tables.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE big360.[securities]" in sql and "[security_id] NVARCHAR(32) NOT NULL" in sql
    assert "PRIMARY KEY ([security_id], [date])" in sql and "BULK INSERT big360.[prices_daily]" in sql
    assert json.loads((out / "manifest.json").read_text(encoding="utf-8"))["export_version"] == ex.EXPORT_VERSION
    m2 = ex.export_snapshot(tmp_path / "pq", "parquet", ["securities"], snapshot=snap)
    assert pl.read_parquet(tmp_path / "pq" / "RTEST1" / "securities.parquet").height == m2["datasets"]["securities"]["rows"]


def test_tsql_types():
    assert ex.tsql_type("VARCHAR", "security_id") == "NVARCHAR(32)"
    assert ex.tsql_type("VARCHAR[]", "aliases") == "NVARCHAR(MAX)"
    assert ex.tsql_type("DOUBLE", "close") == "FLOAT" and ex.tsql_type("BOOLEAN", "x") == "BIT"
    assert ex.tsql_type("TIMESTAMP WITH TIME ZONE", "t") == "DATETIME2(3)" and ex.tsql_type("SMALLINT", "y") == "SMALLINT"
