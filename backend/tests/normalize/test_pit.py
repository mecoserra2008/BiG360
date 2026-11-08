"""app.normalize.pit: as-of snapshots, as-of joins, lag-rule imputation; PIT leakage guard."""

from __future__ import annotations

from datetime import date

import polars as pl

from app.normalize.pit import asof, asof_join, impute_knowledge_date, lag_days


def _versions() -> pl.DataFrame:
    return pl.DataFrame({
        "issuer_id": ["A", "A", "A", "B"],
        "item_id": ["revenue"] * 4,
        "period_end": [date(2023, 12, 31)] * 4,
        "knowledge_date": [date(2024, 2, 10), date(2024, 11, 5), date(2025, 2, 9), date(2024, 3, 1)],
        "value": [100.0, 103.0, 104.0, 7.0],
    })


def test_asof_picks_latest_known_version():
    keys = ["issuer_id", "item_id", "period_end"]
    v = _versions()
    s1 = asof(v, date(2024, 6, 30), keys)
    assert sorted(s1.get_column("value").to_list()) == [7.0, 100.0]
    s2 = asof(v, "2025-03-01", keys)
    assert s2.filter(pl.col("issuer_id") == "A").item(0, "value") == 104.0
    assert asof(v, date(2024, 1, 1), keys).is_empty()
    assert isinstance(asof(v.lazy(), date(2025, 1, 1), keys), pl.LazyFrame)


def test_pit_no_leakage_when_future_rows_appended():
    """Appending rows known after D must not change the as-of-D snapshot (byte-identical)."""
    keys = ["issuer_id", "item_id", "period_end"]
    v = _versions()
    before = asof(v, date(2024, 12, 31), keys).sort(keys)
    future = pl.DataFrame({"issuer_id": ["A", "C"], "item_id": ["revenue", "revenue"],
                           "period_end": [date(2023, 12, 31), date(2023, 12, 31)],
                           "knowledge_date": [date(2025, 6, 1), date(2025, 1, 2)], "value": [999.0, 1.0]})
    after = asof(pl.concat([v, future]), date(2024, 12, 31), keys).sort(keys)
    assert before.equals(after)


def test_asof_ties_broken_by_input_order():
    v = pl.DataFrame({"k": ["a", "a"], "knowledge_date": [date(2024, 1, 1)] * 2, "value": [1, 2]})
    assert asof(v, date(2024, 1, 2), ["k"]).item(0, "value") == 2


def test_asof_join_panel():
    left = pl.DataFrame({"issuer_id": ["A", "A", "A"], "date": [date(2024, 1, 1), date(2024, 3, 1), date(2025, 3, 1)]})
    right = _versions().filter(pl.col("issuer_id") == "A").select("issuer_id", "knowledge_date", "value")
    out = asof_join(left, right, "date", ["issuer_id"])
    assert out.get_column("value").to_list() == [None, 100.0, 104.0]


def test_impute_knowledge_date_lag_rules():
    df = pl.DataFrame({
        "period_end": [date(2024, 3, 31), date(2024, 12, 31), date(2024, 6, 30), date(2024, 12, 31), date(2024, 9, 30)],
        "period_kind": ["Q", "FY", "H", "FY", "Q"],
        "region": ["US", "US", "EU", "JP", "US"],
        "knowledge_date": [None, None, None, None, date(2024, 10, 20)],
    }, schema_overrides={"knowledge_date": pl.Date})
    out = impute_knowledge_date(df)
    kd = out.get_column("knowledge_date").to_list()
    assert kd[0] == date(2024, 5, 15)      # US quarterly +45
    assert kd[1] == date(2025, 3, 16)      # US annual +75
    assert kd[2] == date(2024, 8, 29)      # non-US half +60
    assert kd[3] == date(2025, 4, 30)      # non-US annual +120
    assert kd[4] == date(2024, 10, 20)     # observed date kept
    assert out.get_column("knowledge_date_estimated").to_list() == [True, True, True, True, False]
    assert lag_days("US", "FY") == 75 and lag_days("KR", "Q") == 60
