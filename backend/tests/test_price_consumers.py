"""Regression tests for price-series consumers after the long-history / instrument / float32 storage changes
(docs/PRICE_HISTORY_PLAN.md): gap-aware weekly returns, bounded scans, instruments kept out of equity pools and
cross-sections."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import duckdb
import polars as pl
import pytest

from app.core.storage import duckdb_connect, register_views, write_table
from tests.ml.test_labels_pit import _prices, _seed_universe, _trading_days

pytestmark = pytest.mark.usefixtures("tmp_data")


def test_macro_betas_weekly_returns_skip_gaps(tmp_data):
    from app.ml.macro_betas import _weekly_returns

    days = _trading_days(date(2020, 1, 6), 60)
    # a 6-week hole (suspension / stitch-guard drop) between bar 30 and bar 31
    days = days[:30] + [d + timedelta(days=42) for d in days[30:]]
    closes = [100.0 + i for i in range(len(days))]
    write_table(_prices("AAA", days, closes), "prices_daily", settings=tmp_data)
    w = _weekly_returns(["AAA"], tmp_data).sort("week")
    n_weeks = len({d - timedelta(days=d.weekday()) for d in days})
    # every week has a return except the first one and the first week after the hole (a 7-week move)
    assert w.height == n_weeks - 2
    hole_week = days[30] - timedelta(days=days[30].weekday())
    assert w.filter(pl.col("week") == hole_week).is_empty()
    assert w["ret"].abs().max() < 0.1


def test_tech_meta_puts_instruments_in_own_pool(tmp_data):
    from app.technicals.data import Bars, load_meta

    _seed_universe(tmp_data, ["EQ1"], sector=None)  # an unclassified US equity (sector null)
    now = datetime.now(UTC)
    from app.core.storage import read_table

    base = read_table("securities_master", settings=tmp_data)
    inst = base.with_columns(pl.lit("IDX:EURUSD=X").alias("security_id"), pl.lit("fx").alias("security_type"),
                             pl.lit("I:EURUSD=X").alias("issuer_id"), pl.lit(now).alias("updated_at"))
    write_table(pl.concat([base, inst]), "securities_master", settings=tmp_data, mode="overwrite")
    meta = load_meta(["EQ1", "IDX:EURUSD=X"])
    sec = dict(zip(meta["security_id"].to_list(), meta["sector_id"].to_list(), strict=True))
    assert sec["IDX:EURUSD=X"] == "_fx"
    assert sec["EQ1"] is None
    import numpy as np

    mk = lambda sid, s, r: Bars(sid, np.array([], dtype="datetime64[D]"), *(np.array([]) for _ in range(5)),  # noqa: E731
                                region=r, sector_id=s)
    assert mk("IDX:EURUSD=X", sec["IDX:EURUSD=X"], "FX").pool_key != mk("EQ1", sec["EQ1"], "US").pool_key


def test_ml_feature_price_block_bounded_scan_identical(tmp_data):
    from app.ml.features import PRICE_WARMUP_DAYS, _price_daily_block

    days = _trading_days(date(2010, 1, 4), 3000)  # ~11.5 y of history
    closes = [50.0 * (1.0 + 0.0004 * i) + (i % 7) for i in range(len(days))]
    write_table(_prices("AAA", days, closes), "prices_daily", settings=tmp_data)
    grid0 = days[-200]
    full = _price_daily_block(["AAA"], tmp_data)
    bounded = _price_daily_block(["AAA"], tmp_data, grid0 - timedelta(days=PRICE_WARMUP_DAYS))
    cols = [c for c in full.columns if c.startswith("f_")]
    a = full.filter(pl.col("date") >= grid0).select("date", *cols)
    b = bounded.filter(pl.col("date") >= grid0).select("date", *cols)
    assert a.height == b.height > 0
    for c in cols:
        assert (a[c] - b[c]).abs().max() < 1e-12, c


def test_alerts_last_two_bars_bounded(tmp_data):
    from app.scheduler.alerts import SqlDataProvider

    old = _trading_days(date(1990, 1, 2), 50)
    recent = _trading_days(date(2026, 9, 1), 20)
    write_table(_prices("AAA", old + recent, [10.0] * 50 + [20.0 + i for i in range(20)]), "prices_daily",
                settings=tmp_data)
    con = duckdb_connect(None, settings=tmp_data)
    register_views(con, ["prices_daily"], settings=tmp_data)
    r = SqlDataProvider(con).last_two_bars(["AAA"], recent[-1]).row(0, named=True)
    assert r["date"] == recent[-1] and r["close"] == 39.0 and r["prev_close"] == 38.0
    # a security whose last bar is decades old has no "latest bar" to alert on
    assert SqlDataProvider(con).last_two_bars(["AAA"], old[-1] + timedelta(days=5000)).is_empty()


def test_equity_only_sql_drops_instruments():
    from app.api.routers._common import equity_only_sql

    con = duckdb.connect()
    con.execute("CREATE TABLE sec AS SELECT * FROM (VALUES ('A', 'common'), ('B', NULL), ('C', 'adr'), ('D', 'fx'), "
                "('E', 'future'), ('F', 'index'), ('G', 'etf'), ('H', 'preferred')) t(security_id, security_type)")
    got = {r[0] for r in con.execute(f"SELECT security_id FROM sec s WHERE {equity_only_sql('s')}").fetchall()}
    assert got == {"A", "B", "C", "H"}


def test_tech_levels_stats_split_matches_full_lookup():
    from app.technicals.levels import attach_reliability, split_stats

    stats = pl.DataFrame({
        "security_id": ["A", "A", "B", "*TECH|US", "*TECH|US", "*", "*", "*_fx|FX"],
        "level_key": ["k1", "k2", "k1", "k1", "k3", "k1", "k4", "k1"],
        "score": [0.9, 0.8, 0.1, 0.6, 0.65, 0.5, 0.55, 0.3],
        "pool_key": ["TECH|US", "TECH|US", "TECH|US", "TECH|US", "TECH|US", "*", "*", "_fx|FX"],
    })
    df = pl.DataFrame({"level_key": ["k1", "k2", "k3", "k4", "k5"]})
    shared, own = split_stats(stats, ["A", "C"])
    assert set(own) == {"A"} and shared["security_id"].str.starts_with("*").all()
    for sid, pool in (("A", "TECH|US"), ("C", "TECH|US"), ("C", "_fx|FX")):
        sub = pl.concat([own[sid], shared]) if sid in own else shared
        assert attach_reliability(df, sub, sid, pool).equals(attach_reliability(df, stats, sid, pool))
