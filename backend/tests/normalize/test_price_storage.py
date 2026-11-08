"""Price-history workstream: physical storage layout of prices_daily / prices_staging (float32 narrowing read back with
7-significant-digit rounding, derived columns, sorting) + FX / commodity instrument registration."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pyarrow.parquet as pq

from app.core.schema import get_contract
from app.core.storage import (
    duckdb_connect,
    physical_frame,
    read_table,
    register_views,
    round_sig7,
    sql_round_sig7,
    table_dir,
    write_table,
)


def _daily(n_days: int = 30) -> pl.DataFrame:
    rng = random.Random(7)
    rows = []
    d0 = date(2024, 1, 2)
    for sid, ccy, u in (("S000000000002", "JPY", 0.0066713), ("S000000000001", "USD", 1.0)):
        for i in range(n_days):
            c = round(rng.uniform(10, 5000), 2)
            vol = float(rng.randint(0, 5_000_000))
            af = 0.987654321
            rows.append({"security_id": sid, "listing_id": "L" + sid[1:], "date": d0 + timedelta(days=i),
                         "region": "US", "year": 2024, "open": c * 0.99, "high": round(c * 1.01, 3), "low": c * 0.98,
                         "close": c, "volume": vol, "vwap": None, "trades": None, "adj_factor": af, "split_factor": 1.0,
                         "adj_close": c * af, "currency": ccy, "close_usd": c * u, "dollar_volume_usd": c * u * vol,
                         "source_id": "yfinance", "dq_flags": None, "ingested_at": datetime.now(UTC),
                         "usd_per_unit": u})
    return pl.DataFrame(rows)


def test_round_sig7_restores_float32_decimals():
    rng = np.random.default_rng(1)
    vals = np.concatenate([np.round(rng.uniform(0.0001, 1e6, 20000), d) for d in (2, 3, 4)])
    vals = np.array([float(f"{v:.7g}") for v in vals])  # <= 7 significant digits
    back = pl.Series(vals.astype(np.float32).astype(np.float64)).to_frame("x").select(round_sig7(pl.col("x")))["x"]
    assert (back.to_numpy() == vals).all()
    # values with more digits stay within 5e-8 relative
    raw = rng.uniform(0.001, 1e9, 20000)
    b2 = pl.Series(raw.astype(np.float32).astype(np.float64)).to_frame("x").select(round_sig7(pl.col("x")))["x"]
    assert (np.abs(b2.to_numpy() - raw) / raw).max() < 1e-6
    # DuckDB twin gives identical results
    con = duckdb_connect()
    con.register("t", pl.DataFrame({"x": vals.astype(np.float32).astype(np.float64)}).to_arrow())
    got = np.array([r[0] for r in con.execute(f"SELECT {sql_round_sig7('x')} FROM t").fetchall()])
    assert (got == vals).all()
    assert con.execute(f"SELECT {sql_round_sig7('CAST(NULL AS DOUBLE)')}, {sql_round_sig7('0.0::DOUBLE')}").fetchone() \
        == (None, 0.0)


def test_prices_daily_physical_layout_and_logical_reads(tmp_data):
    df = _daily()
    write_table(df, "prices_daily", mode="overwrite_partition")
    files = list(table_dir("prices_daily").rglob("*.parquet"))
    assert len(files) == 1
    sch = pq.read_schema(files[0])
    names = set(sch.names)
    assert not names & {"adj_close", "close_usd", "dollar_volume_usd"}  # derived: not stored
    assert str(sch.field("close").type) == "float" and str(sch.field("adj_factor").type) == "double"
    phys = pl.read_parquet(files[0])
    assert phys.select("security_id", "date").equals(phys.select("security_id", "date").sort("security_id", "date"))
    # polars reader: logical types, exact 2-decimal closes, derived columns recomputed
    back = read_table("prices_daily").sort("security_id", "date")
    exp = df.sort("security_id", "date")
    assert back.schema["close"] == pl.Float64 and back.columns[:len(get_contract("prices_daily").columns)]
    assert (back["close"] == exp["close"]).all()
    assert (back["adj_close"] == back["close"] * back["adj_factor"]).all()
    assert ((back["close_usd"] - exp["close_usd"]).abs() / exp["close_usd"]).max() < 1e-7
    assert ((back["high"] - exp["high"]).abs() / exp["high"]).max() < 1e-7
    # DuckDB view == polars reader
    con = duckdb_connect()
    register_views(con, ["prices_daily"])
    dk = con.execute("SELECT security_id, date, close, adj_close, close_usd, dollar_volume_usd FROM prices_daily "
                     "ORDER BY 1, 2").pl()
    assert (dk["close"] == back["close"]).all() and (dk["adj_close"] == back["adj_close"]).all()
    assert (dk["dollar_volume_usd"].fill_null(-1) == back["dollar_volume_usd"].fill_null(-1)).all()


def test_mixed_layout_partition_reads_and_upserts(tmp_data):
    """An old-layout file (float64, derived columns stored, no usd_per_unit) next to a new one: both readers work and an
    upsert back-solves usd_per_unit so close_usd survives the rewrite."""
    df = _daily(10)
    old = df.filter(pl.col("security_id") == "S000000000002").drop("usd_per_unit")
    d = table_dir("prices_daily") / "region=US" / "year=2024"
    d.mkdir(parents=True)
    old.drop("region", "year").write_parquet(d / "part-old.parquet")
    write_table(df.filter(pl.col("security_id") == "S000000000001"), "prices_daily", mode="append")
    back = read_table("prices_daily")
    assert back.height == 20 and back["close_usd"].null_count() == 0
    con = duckdb_connect()
    register_views(con, ["prices_daily"])
    assert con.execute("SELECT count(*), count(close_usd), count(adj_close) FROM prices_daily").fetchone() == (20, 20, 20)
    con.close()  # Windows: release file handles before the partition swap
    # upsert rewrites the partition in the new layout without losing close_usd of the old rows
    write_table(df.filter(pl.col("security_id") == "S000000000001").head(1), "prices_daily", mode="upsert")
    files = list(d.glob("*.parquet"))
    assert len(files) == 1 and "close_usd" not in pq.read_schema(files[0]).names
    back2 = read_table("prices_daily").sort("security_id", "date")
    assert back2["close_usd"].null_count() == 0
    exp = df.sort("security_id", "date")
    assert ((back2["close_usd"] - exp["close_usd"]).abs() / exp["close_usd"]).max() < 1e-7


def test_physical_frame_staging_sorted_float32():
    c = get_contract("prices_staging")
    st = pl.DataFrame({"source_id": ["yfinance"] * 3, "year": [2024] * 3, "symbol": ["B", "A", "A"],
                       "date": [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 2)], "close": [1.5, 2.25, 3.125],
                       "adj_close": [1.0, 2.0, 3.0], "volume": [1.0, 2.0, 3.0], "price_basis": ["raw"] * 3,
                       "ingested_at": [datetime.now(UTC)] * 3})
    out = physical_frame(st, c)
    assert out["symbol"].to_list() == ["A", "A", "B"] and out.schema["close"] == pl.Float32
    assert out.schema["volume"] == pl.Float64


def test_fx_and_commodity_instruments_registered():
    from app.ingestion.price_history_lists import UNAVAILABLE, history_instruments
    from app.normalize.identity import benchmark_reference_rows
    from app.normalize.prices import BENCHMARK_BY_TICKER

    inst = history_instruments()
    kinds = {k for *_x, k, _c in [(r[0], r[4], r[5]) for r in inst]}
    assert {"fx", "future", "index_ext", "etf"} <= kinds
    assert not UNAVAILABLE & {r[0] for r in inst}
    b = BENCHMARK_BY_TICKER["EURUSD=X"]
    assert b.security_id == "IDX:EURUSD=X" and b.currency == "USD" and b.region == "FX"
    assert BENCHMARK_BY_TICKER["USDJPY=X"].currency == "JPY"
    assert BENCHMARK_BY_TICKER["ZL=F"].kind == "future" and BENCHMARK_BY_TICKER["^VXN"].kind == "index_ext"
    _iss, secs, lsts, _idm = benchmark_reference_rows(datetime.now(UTC))
    sec = {s["security_id"]: s for s in secs}
    lst = {x["security_id"]: x for x in lsts}
    assert sec["IDX:EURUSD=X"]["security_type"] == "fx" and lst["IDX:EURUSD=X"]["mic"] == "XOFF"
    assert lst["IDX:USDJPY=X"]["exchange_country"] == "JP"
    assert sec["IDX:ZL=F"]["security_type"] == "future" and sec["IDX:^VXN"]["security_type"] == "index"
    assert "fx" in (get_contract("securities_master").column("security_type").values or ())


def test_fx_listings_get_fx_region(tmp_data):
    from app.normalize.identity import benchmark_reference_rows
    from app.normalize.prices import load_listings

    now = datetime.now(UTC)
    _iss, secs, lsts, _idm = benchmark_reference_rows(now)
    keep = {"IDX:EURUSD=X", "IDX:CL=F"}
    write_table(pl.DataFrame([x for x in lsts if x["security_id"] in keep]), "listings", mode="overwrite")
    sm = [dict(x, is_active=True) for x in secs if x["security_id"] in keep]
    write_table(pl.DataFrame(sm), "securities_master", mode="overwrite")
    lst = load_listings()
    reg = dict(zip(lst["security_id"].to_list(), lst["region"].to_list(), strict=True))
    assert reg == {"IDX:EURUSD=X": "FX", "IDX:CL=F": "US"}


def test_compact_table_rewrites_old_layout_and_dedupes(tmp_data):
    from app.ingestion.price_history import compact_table

    df = _daily(5)
    old = df.drop("usd_per_unit")
    d = table_dir("prices_daily") / "region=US" / "year=2024"
    d.mkdir(parents=True)
    old.drop("region", "year").write_parquet(d / "part-a.parquet")
    old.drop("region", "year").head(3).write_parquet(d / "part-b.parquet")  # duplicate PK rows
    rep = compact_table("prices_daily")
    assert rep["duplicates_removed"] == 3 and rep["after"]["files"] == 1 and rep["after"]["rows"] == 10
    back = read_table("prices_daily").sort("security_id", "date")
    exp = df.sort("security_id", "date")
    assert ((back["close_usd"] - exp["close_usd"]).abs() / exp["close_usd"]).max() < 1e-7
    assert ((back["usd_per_unit"] - exp["usd_per_unit"]).abs() / exp["usd_per_unit"]).max() < 1e-7
    assert (back["close"] == exp["close"]).all()


def test_stitch_guard_drops_inconsistent_yahoo_prehistory():
    from app.normalize.prices import SOURCE_PRIORITY, _stitch_guard

    d0 = date(2004, 1, 1)
    rows = []
    for i in range(60):
        d = d0 + timedelta(days=i)
        # A: Yahoo consistent with TWSE; B: Yahoo 20% off (unpublished adjustment)
        for sid, k in (("A", 1.0), ("B", 0.8)):
            rows.append({"security_id": sid, "date": d, "source_id": "yfinance", "close": 100.0 * k})
            if i >= 30:  # official coverage starts at day 30
                rows.append({"security_id": sid, "date": d, "source_id": "twse", "close": 100.0})
    res = pl.DataFrame(rows).with_columns(pl.col("source_id").replace_strict(SOURCE_PRIORITY, default=9).alias("__p"))
    out, rep = _stitch_guard(res)
    assert rep["securities_junction_bad"] == 1 and rep["yahoo_rows_dropped"] == 30
    b = out.filter((pl.col("security_id") == "B") & (pl.col("source_id") == "yfinance"))
    assert b["date"].min() == d0 + timedelta(days=30)  # pre-history dropped, overlap kept
    assert out.filter((pl.col("security_id") == "A") & (pl.col("source_id") == "yfinance")).height == 60
