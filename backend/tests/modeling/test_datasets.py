"""Dataset builder: calendar, transforms (lag / lead / diff / pct_change / cs ops), PIT joins, universe masking,
aggregation, dummies / interactions, cache."""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl
import pytest

from app.modeling import _core
from app.modeling.datasets import DatasetSpec, build_dataset, preview
from app.modeling._core import SpecError
from tests.modeling.conftest import SECTORS, bdays, price, sid


def spec(**kw):
    return DatasetSpec.model_validate(kw)


def test_calendar_buckets():
    assert _core.bucket_of(date(2024, 2, 10), "M") == date(2024, 2, 29)
    assert _core.bucket_of(date(2024, 5, 2), "Q") == date(2024, 6, 30)
    assert _core.bucket_of(date(2024, 6, 15), "W") == date(2024, 6, 14)  # Saturday -> Friday of that week
    assert _core.bucket_of(date(2024, 6, 10), "W") == date(2024, 6, 14)
    assert _core.prev_bucket(date(2024, 3, 31), "M") == date(2024, 2, 29)
    assert _core.prev_bucket(date(2024, 3, 31), "Q") == date(2023, 12, 31)
    assert _core.make_grid(date(2024, 1, 15), date(2024, 4, 30), "M") == [date(2024, 1, 31), date(2024, 2, 29),
                                                                        date(2024, 3, 31), date(2024, 4, 30)]
    assert _core.make_grid(date(2024, 1, 1), date(2024, 12, 30), "Q")[-1] == date(2024, 9, 30)  # no partial quarter


def _month_end_prices(i: int) -> dict[date, float]:
    days = bdays(date(2015, 1, 1), date(2021, 12, 31))
    out: dict[date, float] = {}
    for k, d in enumerate(days):
        out[_core.bucket_of(d, "M")] = price(i, k)  # last business day of the month wins
    return out


def test_lag_lead_diff_pct_and_returns(mini_lake):
    ds = build_dataset(spec(kind="timeseries", frequency="M", start="2016-01-01", end="2020-12-31",
                            variables=[
                                {"name": "px", "source": "prices", "field": "adj_close", "entities": {"tickers": ["T2"]}},
                                {"name": "px_l1", "source": "prices", "field": "adj_close", "entities": {"tickers": ["T2"]},
                                 "transforms": [{"op": "lag", "k": 1}]},
                                {"name": "px_f2", "source": "prices", "field": "adj_close", "entities": {"tickers": ["T2"]},
                                 "transforms": [{"op": "lead", "k": 2}]},
                                {"name": "px_d3", "source": "prices", "field": "adj_close", "entities": {"tickers": ["T2"]},
                                 "transforms": [{"op": "diff", "k": 3}]},
                                {"name": "ret", "source": "prices", "field": "ret", "entities": {"tickers": ["T2"]}},
                                {"name": "fwd3", "source": "prices", "field": "ret", "entities": {"tickers": ["T2"]},
                                 "transforms": [{"op": "fwd_compound", "k": 3}]},
                            ]), use_cache=False)
    f = ds.frame
    mp = _month_end_prices(2)
    dates = f["date"].to_list()
    assert dates[0] == date(2016, 1, 31) and dates[-1] == date(2020, 12, 31) and len(dates) == 60
    px = f["px"].to_numpy()
    assert np.allclose(px, [mp[d] for d in dates])
    # lag uses warm-up history before the window: first row already has its lag
    assert f["px_l1"][0] == pytest.approx(mp[date(2015, 12, 31)])
    assert np.allclose(f["px_l1"].to_numpy()[1:], px[:-1])
    assert np.allclose(f["px_f2"].to_numpy()[:-2], px[2:])
    assert f["px_f2"][-1] is not None  # lead looks beyond the window end (data exists): flagged as future
    assert np.allclose(f["px_d3"].to_numpy()[3:], px[3:] - px[:-3])
    assert np.allclose(f["ret"].to_numpy()[1:], px[1:] / px[:-1] - 1)
    r = f["ret"].to_numpy()
    assert f["fwd3"][0] == pytest.approx((1 + r[1]) * (1 + r[2]) * (1 + r[3]) - 1)
    assert ds.meta["lead_variables"] == ["px_f2", "fwd3"]


def test_pit_ratios_latest_known_period(mini_lake):
    ds = build_dataset(spec(kind="panel", frequency="M", start="2020-01-01", end="2020-10-31",
                            entities={"tickers": ["T1"]},
                            variables=[{"name": "roe", "source": "ratios", "field": "roe"}]), use_cache=False)
    got = dict(zip(ds.frame["date"].to_list(), ds.frame["roe"].to_list(), strict=True))
    exp = {date(2020, 1, 31): None, date(2020, 2, 29): 0.10, date(2020, 3, 31): 0.10, date(2020, 4, 30): 0.10,
           date(2020, 5, 31): 0.12, date(2020, 6, 30): 0.12,  # revision of the older period is ignored
           date(2020, 7, 31): 0.12, date(2020, 8, 31): 0.15, date(2020, 9, 30): 0.16, date(2020, 10, 31): 0.16}
    for d, v in exp.items():
        assert got[d] == (pytest.approx(v) if v is not None else None), d


def test_macro_pit_vs_period_alignment(mini_lake):
    pit = build_dataset(spec(kind="timeseries", frequency="M", start="2020-01-01", end="2020-06-30",
                             macro_alignment="pit", variables=[{"name": "m", "source": "macro", "field": "test:M"}]),
                        use_cache=False)
    per = build_dataset(spec(kind="timeseries", frequency="M", start="2020-01-01", end="2020-06-30",
                             macro_alignment="period", variables=[{"name": "m", "source": "macro", "field": "test:M"}]),
                        use_cache=False)
    # obs dated 2020-01-01 has index 60; published 2020-02-15, revised to 999 on 2020-04-20
    p = dict(zip(pit.frame["date"].to_list(), pit.frame["m"].to_list(), strict=True))
    assert p[date(2020, 1, 31)] == 59.0          # Dec-2019 obs (pub 2020-01-15) is the latest known
    assert p[date(2020, 2, 29)] == 60.0          # Jan-2020 obs published 2020-02-15
    assert p[date(2020, 4, 30)] == 62.0          # Mar obs (pub 2020-04-15) is newer than the Jan revision
    q = dict(zip(per.frame["date"].to_list(), per.frame["m"].to_list(), strict=True))
    assert q[date(2020, 1, 31)] == 999.0         # period alignment: Jan obs, latest vintage
    assert q[date(2020, 3, 31)] == 62.0
    assert pit.meta["macro_alignment"] == "pit" and per.meta["macro_alignment"] == "period"


def test_universe_membership_is_point_in_time(mini_lake):
    ds = build_dataset(spec(kind="panel", frequency="M", start="2018-01-01", end="2021-06-30",
                            entities={"universe": "mvp_us"},
                            variables=[{"name": "ret", "source": "prices", "field": "ret"}]), use_cache=False)
    f = ds.frame
    s5 = f.filter(pl.col("entity_id") == sid(5))["date"]
    s6 = f.filter(pl.col("entity_id") == sid(6))["date"]
    assert s5.max() <= date(2019, 12, 31)          # exited (valid_to)
    assert s6.min() >= date(2020, 1, 31)           # later addition (valid_from beyond the initial build)
    assert f.filter(pl.col("entity_id") == sid(1))["date"].min() == date(2018, 1, 31)  # initial members: since inception
    assert set(f["entity_label"].unique()) == {f"T{i}" for i in SECTORS}


def test_cross_sectional_transforms_and_dummies(mini_lake):
    ds = build_dataset(spec(kind="panel", frequency="M", start="2019-01-01", end="2019-12-31",
                            entities={"universe": "mvp_us"}, dummies=["sector"], interactions=[["v", "r"]],
                            variables=[{"name": "v", "source": "factor_scores", "field": "value"},
                                       {"name": "v_rank", "source": "factor_scores", "field": "value",
                                        "transforms": [{"op": "cs_rank"}]},
                                       {"name": "v_dm", "source": "factor_scores", "field": "value",
                                        "transforms": [{"op": "cs_demean"}]},
                                       {"name": "r", "source": "ratios", "field": "roe"}]), use_cache=False)
    f = ds.frame
    g = f.group_by("date").agg(pl.col("v_rank").min().alias("lo"), pl.col("v_rank").max().alias("hi"),
                               pl.col("v_dm").sum().alias("s"))
    assert (g["lo"] == 0).all() and (g["hi"] == 1).all()
    assert np.allclose(g["s"].to_numpy(), 0, atol=1e-9)
    assert "sector_TECH" in f.columns and "sector_FINS" not in f.columns  # first category dropped
    assert f.filter(pl.col("entity_id") == sid(1))["sector_TECH"].unique().to_list() == [1.0]
    assert np.allclose(f["v_x_r"].drop_nulls().to_numpy(), (f["v"] * f["r"]).drop_nulls().to_numpy())


def test_timeseries_entity_aggregation(mini_lake):
    ds = build_dataset(spec(kind="timeseries", frequency="M", start="2017-01-01", end="2017-12-31",
                            variables=[{"name": "tech", "source": "prices", "field": "ret", "entity_agg": "mean",
                                        "entities": {"universe": "mvp_us", "sectors": ["TECH"]}},
                                       {"name": "t1", "source": "prices", "field": "ret", "entities": {"tickers": ["T1"]}},
                                       {"name": "t2", "source": "prices", "field": "ret", "entities": {"tickers": ["T2"]}},
                                       {"name": "t3", "source": "prices", "field": "ret", "entities": {"tickers": ["T3"]}}]),
                       use_cache=False)
    f = ds.frame
    assert np.allclose(f["tech"].to_numpy(), ((f["t1"] + f["t2"] + f["t3"]) / 3).to_numpy())


def test_group_level_panel_and_cross_section(mini_lake):
    ds = build_dataset(spec(kind="panel", frequency="Q", start="2016-01-01", end="2019-12-31", entity_level="sector",
                            entities={"universe": "mvp_us"},
                            variables=[{"name": "ret", "source": "prices", "field": "ret"}]), use_cache=False)
    assert set(ds.frame["entity_id"].unique()) == {"TECH", "FINS"}
    xs = build_dataset(spec(kind="cross_section", frequency="M", as_of="2021-06-15", entities={"universe": "mvp_us"},
                            variables=[{"name": "roe", "source": "ratios", "field": "roe"},
                                       {"name": "mom", "source": "prices", "field": "adj_close",
                                        "transforms": [{"op": "pct_change", "k": 6}]}]), use_cache=False)
    assert xs.frame["date"].unique().to_list() == [date(2021, 5, 31)]
    assert xs.frame.height == 5  # S5 left the universe in 2019


def test_cache_and_preview(mini_lake):
    sp = spec(kind="timeseries", frequency="W", start="2020-01-01", end="2020-03-31",
              variables=[{"name": "d", "source": "macro", "field": "test:D", "agg": "mean"}])
    a = build_dataset(sp)
    b = build_dataset(sp)
    assert b.cached and a.key == b.key
    sp2 = spec(kind="timeseries", frequency="W", start="2020-01-01", end="2020-04-30",
               variables=[{"name": "d", "source": "macro", "field": "test:D", "agg": "mean"}])
    assert build_dataset(sp2).key != a.key
    pv = preview(sp, n=5)
    assert len(pv["rows"]) == 5 and pv["stats"][0]["n"] == pv["n_rows"] and pv["coverage"][0]["pct_non_null"] == 1.0


def test_spec_errors(mini_lake):
    with pytest.raises(SpecError):
        build_dataset(spec(kind="panel", variables=[{"name": "m", "source": "macro", "field": "test:M"}]))
    with pytest.raises(SpecError):
        build_dataset(spec(kind="timeseries", variables=[{"name": "m", "source": "macro", "field": "nope:X"}]))
    with pytest.raises(SpecError):
        build_dataset(spec(kind="timeseries", entities={"tickers": ["T1"]},
                           variables=[{"name": "x", "source": "prices", "field": "not_a_column"}]))
    with pytest.raises(ValueError):
        spec(kind="timeseries", variables=[{"name": "bad name", "source": "macro", "field": "test:M"}])
    with pytest.raises(ValueError):
        spec(kind="timeseries", variables=[{"name": "a", "source": "nosuch", "field": "x"}])
