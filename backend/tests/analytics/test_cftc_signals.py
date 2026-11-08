"""CFTC positioning signal math (COT index, z-scores, flows, spec-hedger spread, flags, divergence, event study)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.analytics import cftc_signals as cs


def _positions(n: int = 200, rt: str = "disagg_fut", code: str = "067651") -> pl.DataFrame:
    d0 = date(2020, 1, 7)
    rows = []
    for i in range(n):
        d = d0 + timedelta(weeks=i)
        mm = int(1000 * np.sin(i / 10.0)) + (5000 if i == n - 1 else 0)  # last week: blow-out long
        for cat, lo, sh in (("m_money", 10_000 + mm, 10_000), ("prod_merc", 20_000, 21_000 + mm), ("swap", 5_000, 5_000)):
            rows.append({"report_type": rt, "market_code": code, "report_date": d, "category": cat, "long_pos": lo,
                         "short_pos": sh, "open_interest": 100_000, "knowledge_date": d + timedelta(days=3)})
    return pl.DataFrame(rows)


def test_net_frame_spec_hedger():
    net = cs.net_frame(_positions(10).lazy())
    sh = net.filter(pl.col("category") == "spec_hedger").sort("report_date")
    mm = net.filter(pl.col("category") == "m_money").sort("report_date")
    hedge = net.filter(pl.col("category").is_in(["prod_merc", "swap"])).group_by("report_date").agg(pl.col("net").sum()).sort("report_date")
    assert sh.height == 10
    assert (sh.get_column("net") == mm.get_column("net") - hedge.get_column("net")).all()


def test_cot_index_z_and_flags():
    sig = cs.compute_signals(cs.net_frame(_positions().lazy()), now=datetime(2026, 1, 1, tzinfo=UTC))
    mm = sig.filter(pl.col("category") == "m_money").sort("report_date")
    net = mm.get_column("net").to_numpy().astype(float)
    last = mm.row(-1, named=True)
    # COT index 3y = (x - min) / (max - min) over the trailing 156 reports
    w = net[-156:]
    assert last["cot_index_3y"] == pytest.approx((w[-1] - w.min()) / (w.max() - w.min()), abs=1e-6)
    assert last["cot_index_3y"] == pytest.approx(1.0)
    assert last["z_3y"] == pytest.approx((w[-1] - w.mean()) / w.std(ddof=1), rel=1e-5)
    assert last["extreme"] == "long" and last["crowded"] is True
    assert last["pct_rank_3y"] == pytest.approx(1.0)
    assert last["chg_1w"] == net[-1] - net[-2] and last["chg_4w"] == net[-1] - net[-5]
    assert last["net_pct_oi"] == pytest.approx(net[-1] / 100_000, rel=1e-6)
    # min-period rules: no 3y index before 78 reports, no 1y before 26
    assert mm.get_column("cot_index_3y")[:77].null_count() == 77
    assert mm.get_column("cot_index_1y")[:25].null_count() == 25 and mm.get_column("cot_index_1y")[25] is not None
    # bounded
    ci = sig.get_column("cot_index_3y").drop_nulls()
    assert ci.min() >= 0 and ci.max() <= 1
    # spec_hedger moves with the speculators here
    assert sig.filter(pl.col("category") == "spec_hedger").sort("report_date").row(-1, named=True)["extreme"] == "long"


def test_rolling_rank_groups_do_not_leak():
    x = np.array([1.0, 2.0, 3.0, 0.0, 10.0, 5.0])
    gid = np.array([0, 0, 0, 1, 1, 1])
    r = cs._rolling_rank(x, gid, 3, 2)
    assert np.isnan(r[0]) and r[1] == 1.0 and r[2] == 1.0
    assert np.isnan(r[3]) and r[4] == 1.0 and r[5] == 0.5


def test_returns_and_divergence():
    pos = _positions(220)
    end = pos.get_column("report_date").max()
    days = pl.date_range(date(2019, 1, 1), end, "1d", eager=True)
    # mild noise, then the price falls over the last ~13 weeks while managed money piles in -> bullish divergence
    i = np.arange(days.len())
    px = 100.0 + 2.0 * np.sin(i / 17.0) - np.where(i >= days.len() - 91, (i - (days.len() - 91)) * 0.4, 0.0)
    prices = pl.DataFrame({"security_id": "IDX:CL=F", "date": days, "adj_close": px})
    markets = pl.DataFrame({"market_code": ["067651"], "price_security_id": ["IDX:CL=F"]})
    sig = cs.compute_signals(cs.net_frame(pos.lazy()), prices, markets)
    mm = sig.filter(pl.col("category") == "m_money").sort("report_date")
    last = mm.row(-1, named=True)
    rd = last["report_date"]
    p_now = prices.filter(pl.col("date") <= rd).get_column("adj_close")[-1]
    p_13 = prices.filter(pl.col("date") <= mm.get_column("report_date")[-14]).get_column("adj_close")[-1]
    assert last["ret_13w"] == pytest.approx(np.log(p_now / p_13), rel=1e-5)
    assert last["divergence_13w"] is not None and last["divergence_13w"] > 0


def test_event_study_entry_after_release():
    # forward return enters at the first close strictly after the knowledge date
    pdates = np.array(["2024-01-05", "2024-01-08", "2024-01-15", "2024-02-05"], dtype="datetime64[D]")
    plog = np.log(np.array([100.0, 110.0, 120.0, 130.0]))
    fr = cs.forward_returns([date(2024, 1, 5)], pdates, plog, 1)
    assert fr[0] == pytest.approx(np.log(120 / 110))


def test_events_cooldown():
    cot = np.array([0.5, 0.95, 0.5, 0.95, *([0.5] * 14), 0.95])
    assert cs._events(cot, "long") == [1, 18]
    assert cs._events(1 - cot, "short") == [1, 18]
