"""Incremental prices_finalize (refresh-pipeline fixes 2026-10-26): the auto mode must give the same prices_daily as a
full rebuild of the same staging, while re-building only what changed; the O2.2a guards (identity breaks, unit
repairs, unfinished bars) still apply, and a guard firing in a tail window escalates the security to a full-history
re-build."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.core.storage import read_table, scan_table, write_table
from app.normalize import prices as P
from app.normalize import prices_incremental as INC

NOW = datetime.now(UTC)
TODAY = NOW.date()
T0 = NOW - timedelta(days=30)        # history ingested a month ago (after those sessions closed)
T1 = NOW                             # the new week, ingested now
HIST = (TODAY - timedelta(days=430), TODAY - timedelta(days=16))
NEW = (TODAY - timedelta(days=14), TODAY - timedelta(days=1))
COLS = ["close", "open", "high", "low", "volume", "adj_factor", "split_factor", "adj_close", "close_usd"]


def _sessions(start: date, end: date) -> list[date]:
    ss = P.trading_sessions("XNYS", start, end)
    assert ss, "exchange_calendars needed"
    return sorted(ss)


def _bars(sym: str, days: list[date], base: float, ingested: datetime, seed: int,
          scale: dict[date, float] | None = None) -> pl.DataFrame:
    rows, px = [], base
    for i, d in enumerate(days):
        px *= 1.0 + 0.01 * math.sin(0.7 * i + seed)            # deterministic random-ish walk (no stale runs)
        c = px * (scale or {}).get(d, 1.0)
        rows.append({"source_id": "yfinance", "year": d.year, "symbol": sym, "date": d, "open": c * 0.995,
                     "high": c * 1.01, "low": c * 0.99, "close": c, "adj_close": None, "volume": 1e6 + 1000 * i,
                     "vwap": None, "trades": None, "currency": "USD", "price_basis": "raw", "is_benchmark": False,
                     "ingested_at": ingested})
    return pl.DataFrame(rows)


def _actions(rows: list[tuple[str, date, str, float | None, float | None, datetime]]) -> pl.DataFrame:
    return pl.DataFrame([{"source_id": "yfinance", "symbol": s, "ex_date": d, "action_type": a, "ratio": r,
                          "amount": m, "amount_basis": "raw", "currency": "USD", "ingested_at": t}
                         for s, d, a, r, m, t in rows])


def _stored(s) -> pl.DataFrame:
    return read_table("prices_daily", settings=s).sort("security_id", "date")


def _expected(s) -> pl.DataFrame:
    """A full rebuild of the current staging (the reference the incremental result must match)."""
    uni = P.load_universe(s, include_delisted=True)
    st = scan_table("prices_staging", s).collect()
    act = scan_table("corporate_actions_staging", s).collect()
    out, _ = P.build_prices(st, uni, act, settings=s)
    return out.sort("security_id", "date")


def _assert_same(got: pl.DataFrame, exp: pl.DataFrame) -> None:
    assert got.select("security_id", "date").equals(exp.select("security_id", "date"))
    for c in COLS:
        a, b = got.get_column(c).to_list(), exp.get_column(c).to_list()
        for x, y in zip(a, b, strict=True):
            if x is None or y is None:
                assert x is None and y is None, c
            else:
                assert abs(x - y) <= 2e-6 * max(1.0, abs(y)), (c, x, y)
    # dq flags identical (same guards, same context)
    fa = got.get_column("dq_flags").to_list()
    fb = exp.get_column("dq_flags").to_list()
    assert [sorted(x or []) for x in fa] == [sorted(x or []) for x in fb]


@pytest.fixture()
def lake(tmp_data):
    s = tmp_data
    hist = _sessions(*HIST)
    st = pl.concat([_bars("AAPL", hist, 150, T0, 1), _bars("MSFT", hist, 300, T0, 2), _bars("NVDA", hist, 90, T0, 3),
                    _bars("JPM", hist, 180, T0, 4)])
    write_table(st, "prices_staging", settings=s)
    write_table(_actions([("AAPL", hist[150], "cash_dividend", None, 0.26, T0),
                          ("NVDA", hist[100], "split", 10.0, None, T0)]), "corporate_actions_staging", settings=s)
    rep = P.finalize(s, mode="full")
    assert rep["rows_written"] > 0
    # the full run "happened" right after the history was ingested: watermark T0 (+1 h, past the 15-min overlap)
    st = INC.load_state(s)
    assert "US" in st["regions"]
    for r in st["regions"].values():
        r["watermark"] = (T0 + timedelta(hours=1)).isoformat()
    INC.save_state(s, st)
    return s, hist


def test_auto_matches_full_rebuild_tail_dividend_and_revision(lake):
    s, hist = lake
    new = _sessions(*NEW)
    # new sessions for all four; MSFT goes ex-dividend inside the new week (history factors change -> rescale);
    # JPM gets a revised bar 120 days back (history revision -> full re-build)
    st = pl.concat([_bars(sym, hist + new, base, T1, seed).filter(pl.col("date") >= new[0])
                    for sym, base, seed in (("AAPL", 150, 1), ("MSFT", 300, 2), ("NVDA", 90, 3), ("JPM", 180, 4))])
    rev = _bars("JPM", hist, 180, T1, 4).filter(pl.col("date") == hist[-80]).with_columns(pl.col("close") * 1.02)
    write_table(pl.concat([st, rev]), "prices_staging", settings=s)
    write_table(_actions([("MSFT", new[3], "cash_dividend", None, 0.83, T1)]), "corporate_actions_staging", settings=s)
    rep = P.finalize(s)  # mode=auto
    assert rep["mode"] == "incremental"
    us = rep["by_region"]["US"]
    assert us["full_securities"] == 1 and us["tail_securities"] == 3, str(us)   # JPM full; AAPL / MSFT / NVDA tail
    assert us["rescaled_securities"] == 1                                    # MSFT history x (1 - D / P)
    _assert_same(_stored(s), _expected(s))
    # an immediate re-run re-processes the 15-min watermark overlap: idempotent
    before = _stored(s)
    P.finalize(s)
    _assert_same(_stored(s), before)
    # later, without new staging rows: a no-op
    st = INC.load_state(s)
    for r in st["regions"].values():
        r["watermark"] = (T1 + timedelta(hours=1)).isoformat()
    INC.save_state(s, st)
    rep2 = P.finalize(s)
    assert rep2["rows_written"] == 0 and rep2["by_region"]["US"]["tail_securities"] == 0


def test_unit_flip_in_tail_escalates_to_full_history(lake):
    s, hist = lake
    new = _sessions(*NEW)
    flip = {d: 100.0 for d in new[2:5]}                    # Yahoo quotes NVDA 100x for three sessions (flip pair)
    st = pl.concat([_bars("NVDA", hist + new, 90, T1, 3, scale=flip).filter(pl.col("date") >= new[0]),
                    _bars("AAPL", hist + new, 150, T1, 1).filter(pl.col("date") >= new[0])])
    write_table(st, "prices_staging", settings=s)
    rep = P.finalize(s)
    us = rep["by_region"]["US"]
    assert us["escalated"] == 1 and us["full_securities"] == 1, us
    got = _stored(s)
    nv = got.filter(pl.col("security_id") == "P:NVDA")
    assert nv.filter(pl.col("dq_flags").list.contains("unit_rescaled")).height == 3   # repaired, not a 100x jump
    _assert_same(got, _expected(s))


def test_unfinished_bar_guard_still_applies(lake):
    s, hist = lake
    new = _sessions(*NEW)
    # the last bar was fetched before its session closed (16:00 ET = 21:00 UTC): dropped, like in the full build
    early = datetime(new[-1].year, new[-1].month, new[-1].day, 15, 0, tzinfo=UTC)
    st = _bars("AAPL", hist + new, 150, T1, 1).filter(pl.col("date") >= new[0]).with_columns(
        pl.when(pl.col("date") == new[-1]).then(pl.lit(early)).otherwise(pl.col("ingested_at")).alias("ingested_at"))
    write_table(st, "prices_staging", settings=s)
    P.finalize(s)
    got = _stored(s).filter(pl.col("security_id") == "P:AAPL")
    assert got.get_column("date").max() == new[-2]
    _assert_same(_stored(s), _expected(s))


def test_plan_region_rules():
    dirty = pl.DataFrame({"security_id": ["A", "B", "C", "D"],
                          "d_new": [date(2026, 3, 2), date(2025, 11, 1), date(2026, 3, 2), date(2026, 3, 2)]})
    last = pl.DataFrame({"security_id": ["A", "B", "D", "E", "Z"], "last_date": [date(2026, 2, 27)] * 5})
    ca = pl.DataFrame({"security_id": ["D", "E"], "ex_date": [date(2026, 3, 4), date(2026, 3, 10)]})
    plan = INC.plan_region(dirty, last, ca, set(), set(), {"A", "B", "C", "D", "E", "Z"})
    assert plan["full"] == {"B", "C"}                                     # revision, new security
    assert plan["why"] == {"new_security": 1, "history_revision": 1}
    assert plan["tail"]["A"] == date(2026, 3, 2) and plan["tail"]["D"] == date(2026, 3, 2)
    assert plan["tail"]["E"] == date(2026, 2, 27)                         # only a future dividend: from its last bar
    assert plan["delete"] == set()
    # a security that left the universe is deleted; a mass "deletion" (broken universe load) is suppressed
    p2 = INC.plan_region(dirty.head(0), last, ca.head(0), set(), set(), {"A", "B", "D", "E"}, max_delete_share=0.5)
    assert p2["delete"] == {"Z"}
    p3 = INC.plan_region(dirty.head(0), last, ca.head(0), set(), set(), {"A"})
    assert p3["delete"] == set() and p3["why"]["delete_suppressed"] == 4


def test_factor_rescale_ratios():
    ref = pl.DataFrame({"security_id": ["A", "B", "C"], "date": [date(2026, 3, 1)] * 3,
                        "adj_factor": [0.5, 1.0, 1.0], "split_factor": [1.0, 1.0, 1.0]})
    win = pl.DataFrame({"security_id": ["A", "B"], "date": [date(2026, 3, 1)] * 2,
                        "adj_factor": [0.49, 1.0000000001], "split_factor": [1.0, 1.0]})
    r, missing = INC.factor_rescale(ref, win)
    assert missing == {"C"}
    assert r.get_column("security_id").to_list() == ["A"] and abs(r.item(0, "__ra") - 0.98) < 1e-12


def test_stitch_guard_uses_full_history_decisions_in_a_window():
    """A tail window of a security whose Yahoo history disagrees with the official source must drop the Yahoo
    gap-fill bars exactly as the full build does, even when the window alone looks consistent."""
    d0 = date(2025, 1, 1)
    full_rows, win_rows = [], []
    for i in range(120):
        d = d0 + timedelta(days=i)
        bad = i < 60                              # Yahoo 20% off early on (unpublished adjustment)
        full_rows.append({"security_id": "B", "date": d, "source_id": "yfinance", "close": 80.0 if bad else 100.0})
        if i % 10 != 0:                           # official source misses every 10th day -> Yahoo gap fills
            full_rows.append({"security_id": "B", "date": d, "source_id": "twse", "close": 100.0})
    full = pl.DataFrame(full_rows).with_columns(pl.col("source_id").replace_strict(P.SOURCE_PRIORITY, default=9).alias("__p"))
    _, frep = P._stitch_guard(full)
    dec = frep["_decisions"]
    assert dec.filter(pl.col("security_id") == "B").item(0, "__sbad") is True
    win = full.filter(pl.col("date") >= d0 + timedelta(days=90))      # window: Yahoo agrees with the official prints
    _, wrep = P._stitch_guard(win)
    assert wrep["yahoo_rows_dropped"] == 0                             # the window alone would keep the gap fills
    out, rep = P._stitch_guard(win, decisions=dec)
    gap_days = [d0 + timedelta(days=i) for i in range(90, 120) if i % 10 == 0]
    assert rep["yahoo_rows_dropped"] == len(gap_days)
    assert out.filter(pl.col("date").is_in(gap_days)).is_empty()
