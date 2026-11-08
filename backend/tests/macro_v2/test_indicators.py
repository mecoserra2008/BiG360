"""Macro v2 W1 computations on synthetic data: canonical transforms, cell statistics, PIT first release, selection,
cycle quadrants, revisions (no network)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.macro import cycle as cyc
from app.macro.indicators import (
    CELL_INDICATORS,
    COUNTRIES,
    SeriesInfo,
    build_cells,
    canonical_how,
    cell_stats,
    first_release_canonical,
    month_ends,
    period_label,
    pit_latest,
    select_candidates,
    to_canonical,
)
from app.macro.revisions import revision_stats, series_revisions, vintage_series


def months(start: date, n: int, step: int = 1) -> list[date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m += step
        while m > 12:
            m -= 12
            y += 1
    return out


def frame(dates: list[date], values: list[float]) -> pl.DataFrame:
    return pl.DataFrame({"date": dates, "value": values}, schema={"date": pl.Date, "value": pl.Float64})


def info(sid: str, ind: str, freq: str = "M", scale: str = "index", cc: str = "US", rank: int = 1,
         lag: int = 15) -> SeriesInfo:
    return SeriesInfo(series_id=sid, source_id=sid.split(":")[0], title=sid, country=cc, indicators=[ind],
                      category="growth", frequency=freq, units="index", scale=scale, tf="level", lag=lag, rank=rank,
                      measure="", retired=False)


def obs_rows(sid: str, dates: list[date], values: list[float], lag_days: int = 15, est: bool = True) -> list[tuple]:
    return [(sid, d, v, d + timedelta(days=lag_days + 30), est) for d, v in zip(dates, values, strict=True)]


def obs_frame(rows: list[tuple]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema={"series_id": pl.Utf8, "date": pl.Date, "value": pl.Float64,
                                      "knowledge_date": pl.Date, "knowledge_date_estimated": pl.Boolean}, orient="row")


# ------------------------------------------------------------------------------------------------ transforms


def test_canonical_how_rules() -> None:
    assert canonical_how("cpi_yoy", "index", "level") == "yoy"
    assert canonical_how("cpi_yoy", "percent", "level") == "level"
    assert canonical_how("gdp_qoq", "level", "level") == "qoq"
    assert canonical_how("policy_rate", "fraction", "level") == "level100"
    assert canonical_how("pmi_proxy", "index", "level") == "level"
    assert canonical_how(None, "index", "yoy") == "yoy"
    assert {v["tf"] for v in CELL_INDICATORS.values()} <= {"yoy", "qoq", "level"}


def test_to_canonical_yoy_qoq_monthly_mean() -> None:
    d = months(date(2020, 1, 1), 30)
    v = [100 * 1.02 ** (i / 12) for i in range(30)]
    y = to_canonical(frame(d, v), "yoy", "M")
    assert y.height == 18 and y.get_column("value").to_list()[-1] == pytest.approx(2.0, abs=1e-9)
    q = months(date(2020, 1, 1), 8, 3)
    g = to_canonical(frame(q, [100, 101, 102.01, 103.0301, 104, 105, 106, 107]), "qoq", "Q")
    assert g.get_column("value").to_list()[0] == pytest.approx(1.0)
    daily = [date(2026, 1, 1) + timedelta(days=i) for i in range(59)]
    m = to_canonical(frame(daily, [1.0 if x.month == 1 else 3.0 for x in daily]), "level", "D")
    assert m.get_column("value").to_list() == [1.0, 3.0]
    assert to_canonical(frame(d[:3], [0.01, 0.02, 0.03]), "level100", "M").get_column("value").to_list() == \
        pytest.approx([1.0, 2.0, 3.0])


def test_yoy_uses_date_lag_not_row_shift() -> None:
    d = months(date(2020, 1, 1), 26)
    del d[20]  # a missing month (e.g. a shutdown) must not misalign the 12-month lag
    v = [100 + i for i in range(25)]
    y = to_canonical(frame(d, v), "yoy", "M")
    by = dict(zip(y.get_column("date").to_list(), y.get_column("value").to_list(), strict=True))
    assert date(2021, 9, 1) not in by and date(2022, 2, 1) in by  # Sep-2021 has no base month; Feb-2022 does


# ------------------------------------------------------------------------------------------------ statistics


def test_cell_stats_zscore_percentile_changes() -> None:
    d = months(date(2014, 1, 1), 150)
    v = [float(i % 10) for i in range(150)]
    v[-1] = 20.0
    st = cell_stats(frame(d, v), 20.0, d[-1], "M")
    w = np.array(v[-120:])
    assert st["n_hist"] == 120
    assert st["z_10y"] == pytest.approx((20 - w.mean()) / w.std(ddof=1))
    assert st["percentile_10y"] == pytest.approx(1.0)
    assert st["change_3m"] == pytest.approx(20.0 - v[-4]) and st["change_12m"] == pytest.approx(20.0 - v[-13])
    assert st["direction"] == "up"
    flat = cell_stats(frame(d, [5.0] * 149 + [5.0]), 5.0, d[-1], "M")
    assert flat["z_10y"] is None and flat["direction"] == "flat"
    assert cell_stats(frame(d[:3], [1.0, 2.0, 3.0]), 3.0, d[2], "M")["z_10y"] is None  # < 8 points


def test_cell_stats_quarterly_and_annual() -> None:
    q = months(date(2010, 1, 1), 60, 3)
    st = cell_stats(frame(q, [float(i) for i in range(60)]), 59.0, q[-1], "Q")
    assert st["change_3m"] == pytest.approx(1.0) and st["change_6m"] == pytest.approx(2.0)
    a = [date(2000 + i, 1, 1) for i in range(20)]
    sa = cell_stats(frame(a, [float(i) for i in range(20)]), 19.0, a[-1], "A")
    assert sa["change_3m"] is None and sa["change_12m"] == pytest.approx(1.0) and sa["direction"] == "up"


def test_period_label_and_month_ends() -> None:
    assert period_label(date(2026, 4, 1), "Q") == "2026Q2" and period_label(date(2026, 8, 1), "M") == "2026-08"
    assert month_ends(date(2026, 10, 12), 2) == [date(2026, 8, 31), date(2026, 9, 30)]


# ------------------------------------------------------------------------------------------------ PIT / vintages


def test_first_release_yoy_is_point_in_time() -> None:
    # t-12 = 100 first published, later revised to 101; t first published at 105 -> first-release y/y = 5% (not 3.96%)
    o = obs_frame([("x", date(2025, 1, 1), 100.0, date(2025, 2, 15), False),
                   ("x", date(2025, 1, 1), 101.0, date(2026, 3, 1), False),
                   ("x", date(2026, 1, 1), 105.0, date(2026, 2, 15), False)])
    fr = first_release_canonical(o, "yoy", "M")
    assert fr.filter(pl.col("date") == date(2026, 1, 1)).item(0, "first_value") == pytest.approx(5.0)
    lat = pit_latest(o)
    assert lat.filter(pl.col("date") == date(2025, 1, 1)).item(0, "value") == 101.0
    early = pit_latest(o, date(2026, 2, 20))
    assert early.filter(pl.col("date") == date(2025, 1, 1)).item(0, "value") == 100.0  # PIT as-of


def test_revisions_table_and_stats() -> None:
    d = months(date(2020, 1, 1), 30)
    rows = obs_rows("fred:PAYEMS", d, [100.0 + i for i in range(30)], est=False)
    rows += [("fred:PAYEMS", x, 100.0 + i + 0.5, x + timedelta(days=90), False) for i, x in enumerate(d)]
    o = obs_frame(rows)
    assert vintage_series(o) == ["fred:PAYEMS"]
    r = series_revisions("fred:PAYEMS", o, info("fred:PAYEMS", "employment", scale="level"))
    assert r.height == 30 and set(r.get_column("vintage_kind")) == {"alfred"}
    assert r.get_column("revision").to_list() == pytest.approx([0.5] * 30)
    assert r.get_column("transform")[0] == "yoy" and r.drop_nulls("revision_tf").height == 18
    st = revision_stats(r.to_dicts())
    assert st["native"]["n"] == 30 and st["native"]["mean"] == pytest.approx(0.5) and st["native"]["share_up"] == 1.0


# ------------------------------------------------------------------------------------------------ selection / build


def test_select_candidates_prefers_fresh_then_rank() -> None:
    today = date(2026, 10, 12)
    d_fresh = months(date(2016, 1, 1), 128)        # through 2026-08
    d_stale = months(date(2010, 1, 1), 150)        # through 2022-06
    meta = {"a:stale": info("a:stale", "cpi_yoy", rank=1), "b:fresh": info("b:fresh", "cpi_yoy", rank=2),
            "c:fresh1": info("c:fresh1", "unemployment", scale="percent", rank=1)}
    o = obs_frame(obs_rows("a:stale", d_stale, [1.0] * 150) + obs_rows("b:fresh", d_fresh, [1.0] * 128)
                  + obs_rows("c:fresh1", d_fresh, [4.0] * 128))
    c = select_candidates(meta, pit_latest(o, today), today)
    assert c[("US", "cpi_yoy")] == ["b:fresh", "a:stale"] and c[("US", "unemployment")] == ["c:fresh1"]


def test_build_cells_pit_and_spread() -> None:
    today = date(2026, 10, 12)
    days = [today - timedelta(days=k) for k in range(1, 3700)][::-1]
    d = months(date(2012, 1, 1), 176)
    rng = np.random.default_rng(1)
    meta = {"fred:DGS10": info("fred:DGS10", "yield_10y", "D", "percent", lag=1),
            "fred:DGS2": info("fred:DGS2", "yield_2y", "D", "percent", lag=1),
            "fred:CPIAUCSL": info("fred:CPIAUCSL", "cpi_yoy")}
    rows = [("fred:DGS10", x, 4.0 + 0.001 * i, x + timedelta(days=1), True) for i, x in enumerate(days)]
    rows += [("fred:DGS2", x, 3.5, x + timedelta(days=1), True) for x in days]
    rows += obs_rows("fred:CPIAUCSL", d, list(100 * np.exp(np.cumsum(rng.normal(0.002, 0.001, len(d))))))
    o = obs_frame(rows)
    cells, hist = build_cells(today, meta=meta, obs=o, countries=["US"])
    by = {r["indicator"]: r for r in cells.to_dicts()}
    assert set(by) == {"yield_10y", "yield_2y", "curve_2s10s", "cpi_yoy"}
    sp = by["curve_2s10s"]
    assert sp["value"] == pytest.approx(by["yield_10y"]["value"] - 3.5) and sp["inputs"] == ["fred:DGS10", "fred:DGS2"]
    assert by["cpi_yoy"]["transform"] == "yoy" and by["cpi_yoy"]["units"] == "% y/y" and by["cpi_yoy"]["z_10y"] is not None
    assert hist.filter(pl.col("series_id") == "derived:US.curve_2s10s").height > 100
    assert hist.filter(pl.col("is_primary")).get_column("series_id").n_unique() == 4
    old, _ = build_cells(date(2020, 6, 30), meta=meta, obs=o, countries=["US"], with_history=False)
    o10 = {r["indicator"]: r for r in old.to_dicts()}["yield_10y"]
    assert o10["period"] <= date(2020, 6, 30) and o10["value"] < by["yield_10y"]["value"]  # PIT snapshot


def test_akshare_is_excluded() -> None:
    from app.macro.indicators import EXCLUDED_SOURCES
    assert "akshare_cn" in EXCLUDED_SOURCES and len(COUNTRIES) == 24


# ------------------------------------------------------------------------------------------------ cycle


def test_quadrants() -> None:
    assert cyc.quadrant(1, -1) == "recovery" and cyc.quadrant(1, 1) == "expansion"
    assert cyc.quadrant(-1, 1) == "slowdown" and cyc.quadrant(-1, -1) == "contraction"
    assert cyc.quadrant(None, 1) is None and cyc.quadrant(float("nan"), 1) is None


def test_cycle_clock_rotates_on_synthetic_cycle() -> None:
    """Growth ~ sin, inflation lags growth by a quarter cycle -> the clock visits the quadrants in the order
    recovery -> expansion -> slowdown -> contraction."""
    n = 240
    d = months(date(2005, 1, 1), n)
    t = np.arange(n) * 2 * math.pi / 48
    inputs = {"industrial_production": (d, list(3 * np.sin(t)), "M"), "pmi_proxy": (d, list(100 + 2 * np.sin(t)), "M"),
              "cpi_yoy": (d, list(2 + np.sin(t - math.pi / 2)), "M")}
    out = cyc.country_cycle(inputs, d[-1])
    assert set(out.columns) >= {"growth_momentum", "inflation_momentum", "n_growth", "n_inflation", "inputs"}
    q = [cyc.quadrant(g, i) for g, i in zip(out["growth_momentum"], out["inflation_momentum"], strict=True)]
    q = [x for x in q[-96:] if x]
    seq = [x for i, x in enumerate(q) if i == 0 or x != q[i - 1]]
    order = ["recovery", "expansion", "slowdown", "contraction"]
    pairs = list(zip(seq, seq[1:], strict=False))
    assert len(pairs) >= 6
    assert sum(order[(order.index(a) + 1) % 4] == b for a, b in pairs) / len(pairs) >= 0.8
    assert (out["n_growth"].iloc[-1], out["n_inflation"].iloc[-1]) == (2, 1)


def test_build_cycle_and_pick_input_fallback() -> None:
    now = datetime.now(UTC)
    d_long = months(date(2010, 1, 1), 66, 3)    # quarterly CPI 2010-2026
    d_short = months(date(2025, 4, 1), 17)      # new monthly CPI (primary, too short)
    d_ip = months(date(2010, 1, 1), 200)
    rows = []
    for sid, ds, prim, ind, f in (("q:cpi", d_long, False, "cpi_yoy", "Q"), ("m:cpi", d_short, True, "cpi_yoy", "M"),
                                  ("m:ip", d_ip, True, "industrial_production", "M")):
        for i, x in enumerate(ds):
            rows.append({"series_id": sid, "measure": ind, "country": "AU", "indicator": ind, "date": x,
                         "value": float(np.sin(i / 5)), "transform": "level", "units": "%", "frequency": f,
                         "is_primary": prim, "computed_at": now})
    hist = pl.DataFrame(rows)
    picked = cyc.pick_input(hist.filter(pl.col("indicator") == "cpi_yoy"), date(2026, 10, 1))
    assert picked.get_column("series_id")[0] == "q:cpi"
    out = cyc.build_cycle(hist, date(2026, 10, 1))
    last = out.sort("date").row(-1, named=True)
    assert last["country"] == "AU" and last["n_inflation"] == 1 and last["quadrant"] in cyc.QUADRANTS


def test_rebased_vintages_flagged_and_excluded_from_native_stats() -> None:
    """Old vintages on a different index base (x3) must not pollute native revision stats; the canonical y/y basis is
    base-invariant and imputed pre-archive first releases are excluded from both blocks."""
    d = months(date(2000, 1, 1), 48)
    lvl = [100.0 * 1.02 ** (i / 12) for i in range(48)]
    rows = []
    for i, x in enumerate(d):
        old_base = i < 24                       # first published on an old base (values x 1/3), later rebased
        first = lvl[i] / 3.0 if old_base else lvl[i]
        rows.append(("fred:CPIAUCSL", x, first, x + timedelta(days=15), False))
        if old_base:                            # the rebase vintage restates the whole history at once
            rows.append(("fred:CPIAUCSL", x, lvl[i], date(2002, 1, 10), False))
        rows.append(("fred:CPIAUCSL", x, lvl[i] + (0.01 if not old_base else 0.0), date(2010, 1, 1), False))
    rows.append(("fred:CPIAUCSL", date(1999, 12, 1), 50.0, date(2009, 1, 1), True))   # imputed first release
    rows.append(("fred:CPIAUCSL", date(1999, 12, 1), 51.0, date(2010, 1, 1), False))
    r = series_revisions("fred:CPIAUCSL", obs_frame(rows), info("fred:CPIAUCSL", "cpi_yoy"))
    assert r.filter(pl.col("rebased")).height == 24 and r.filter(~pl.col("rebased")).height == 25
    st = revision_stats(r.to_dicts())
    assert st["basis"] == "canonical" and st["n_rebased_excluded"] == 24 and st["n_estimated_excluded"] == 1
    assert st["native"]["n"] == 24 and st["native"]["max_abs"] == pytest.approx(0.01)
    assert st["canonical"]["max_abs"] < 0.05     # y/y within one vintage is base-invariant
