"""O2.2a data integrity (docs/AUDIT_2026-10-19.md F04 / F05 / F32): ticker-reuse breaks, 100x unit flips, missing
exchange sessions, unfinished Yahoo bars. Regression fixtures are real staging slices (tests/fixtures/o2_integrity):
SPCX / BNY / FIG (Polygon grouped-daily + Yahoo), ROSE.L (GBp/GBP flips), DIA.MC (1000:1 reverse split, Yahoo raw
history split-unadjusted twice)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.normalize import price_integrity as PI
from app.normalize.prices import compute_adjustment_factors

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "o2_integrity"


@pytest.fixture(scope="module")
def bars() -> pl.DataFrame:
    return pl.read_csv(FIX / "bars.csv", try_parse_dates=True).with_columns(
        pl.col("symbol").alias("security_id"), pl.col("date").cast(pl.Date))


@pytest.fixture(scope="module")
def actions() -> pl.DataFrame:
    return pl.read_csv(FIX / "actions.csv", try_parse_dates=True).with_columns(
        pl.col("symbol").alias("security_id"), pl.col("ex_date").cast(pl.Date),
        pl.lit(None, pl.Float64).alias("amount"))


# ------------------------------------------------------------------------------------------------ identity breaks


def test_reuse_breaks_detected_for_spcx_bny_fig(bars, actions):
    reuse = bars.filter(pl.col("symbol").is_in(["SPCX", "BNY", "FIG"]))
    br = PI.detect_identity_breaks(reuse, actions).filter(pl.col("is_break"))
    got = {r["security_id"]: r for r in br.to_dicts()}
    assert set(got) == {"SPCX", "BNY", "FIG"}
    assert all(r["source_id"] == "polygon" for r in got.values())  # Yahoo history follows the current issuer
    assert got["SPCX"]["break_date"] == date(2026, 6, 12)
    assert got["FIG"]["break_date"] == date(2025, 7, 31)
    assert got["BNY"]["break_date"] == date(2026, 5, 21)
    assert got["SPCX"]["ratio"] > 7 and got["SPCX"]["gap_sessions"] >= 40
    assert "other_source_starts" in got["SPCX"]["evidence"]  # Yahoo SpaceX history starts on the listing day
    assert "other_source_continuous" in got["BNY"]["evidence"]  # Yahoo BK/BNY history spans the junction


def test_reuse_split_keeps_only_the_current_issuer(bars, actions):
    reuse = bars.filter(pl.col("symbol").is_in(["SPCX", "BNY", "FIG"]))
    br = PI.detect_identity_breaks(reuse, actions)
    out, rep = PI.apply_identity_breaks(reuse, br)
    assert rep["securities_split"] == 3 and rep["rows_dropped"] > 800
    first_poly = dict(out.filter(pl.col("source_id") == "polygon").group_by("security_id")
                      .agg(pl.col("date").min()).iter_rows())
    assert first_poly == {"SPCX": date(2026, 6, 12), "FIG": date(2025, 7, 31), "BNY": date(2026, 5, 21)}
    # Yahoo bars are untouched: BNY keeps the BNY Mellon (ex-BK) history through the ticker change
    yf_before = reuse.filter(pl.col("source_id") == "yfinance").height
    assert out.filter(pl.col("source_id") == "yfinance").height == yf_before
    # winner per date (polygon priority) is now one issuer: no >3x junction left in any of the three series
    win = (out.with_columns((pl.col("source_id") != "polygon").cast(pl.Int8).alias("p"))
           .sort("security_id", "date", "p").unique(subset=["security_id", "date"], keep="first").sort("security_id", "date"))
    r = win.with_columns((pl.col("close") / pl.col("close").shift(1).over("security_id")).alias("r"))
    assert r.filter((pl.col("r") > 3) | (pl.col("r") < 1 / 3)).is_empty()
    # YTD base: SpaceX has no 2025 bar any more -> chg_ytd / ret_12m cannot be computed from the old ETF
    assert win.filter((pl.col("security_id") == "SPCX") & (pl.col("date") < date(2026, 1, 1))).is_empty()


def _series(sid: str, closes: list[float], start: date = date(2025, 1, 6), gap_at: int | None = None,
            gap_days: int = 0, vol: list[float] | None = None, source: str = "polygon") -> pl.DataFrame:
    days, d = [], start
    for i in range(len(closes)):
        if gap_at is not None and i == gap_at:
            d += timedelta(days=gap_days)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        days.append(d)
        d += timedelta(days=1)
    return pl.DataFrame({"security_id": sid, "source_id": source, "date": days, "close": closes,
                         "volume": vol or [1e5] * len(closes)})


def test_no_break_without_gap_split_or_small_move():
    # 3x biotech pop without a gap (no break), a gap with a +40% move (no break), a 1:10 reverse split across a gap
    pop = _series("POP", [2.0] * 30 + [6.5] * 30)
    calm = _series("CALM", [10.0] * 30 + [14.0] * 30, gap_at=30, gap_days=40)
    rs = _series("RS", [0.5] * 30 + [5.2] * 30, gap_at=30, gap_days=30)
    acts = pl.DataFrame({"security_id": ["RS"], "ex_date": [rs.item(30, "date")], "action_type": ["split"],
                         "ratio": [0.1]})
    br = PI.detect_identity_breaks(pl.concat([pop, calm, rs]), acts)
    assert br.filter(pl.col("is_break")).is_empty()


def test_gap_and_jump_needs_corroboration():
    weak = _series("WEAK", [10.0] * 30 + [27.0] * 30, gap_at=30, gap_days=20)            # 2.7x, same volume
    vol = _series("VOL", [10.0] * 30 + [27.0] * 30, gap_at=30, gap_days=20,
                  vol=[1e4] * 30 + [5e5] * 30)                                              # 2.7x + 50x volume
    br = PI.detect_identity_breaks(pl.concat([weak, vol]), None)
    assert br.filter(pl.col("is_break")).get_column("security_id").to_list() == ["VOL"]
    assert br.filter(pl.col("security_id") == "WEAK").item(0, "is_break") is False


def test_overrides_suppress_and_force():
    s = _series("X", [10.0] * 30 + [60.0] * 30, gap_at=30, gap_days=60)
    br = PI.detect_identity_breaks(s, None)
    assert br.item(0, "is_break") is True
    ov = pl.DataFrame({"key": ["X"], "source_id": ["*"], "break_date": [br.item(0, "break_date")],
                       "action": ["suppress"]})
    assert PI.apply_overrides(br, ov).filter(pl.col("is_break")).is_empty()
    force = pl.DataFrame({"key": ["XYZ"], "source_id": ["*"], "break_date": [date(2025, 2, 3)], "action": ["force"]},
                         schema={"key": pl.Utf8, "source_id": pl.Utf8, "break_date": pl.Date, "action": pl.Utf8})
    syms = pl.DataFrame({"source_id": ["polygon"], "symbol": ["XYZ"], "security_id": ["SXYZ"]})
    out = PI.apply_overrides(br, force, syms)
    f = out.filter(pl.col("security_id") == "SXYZ")
    assert f.height == 1 and f.item(0, "evidence").to_list() == ["manual_override"]
    res = _series("SXYZ", [1.0] * 40, source="yfinance")
    kept, rep = PI.apply_identity_breaks(res, f)
    assert kept.get_column("date").min() >= date(2025, 2, 3) and rep["rows_dropped"] > 0


# ------------------------------------------------------------------------------------------------ unit flips


def _finalized(bars: pl.DataFrame, actions: pl.DataFrame, symbols: list[str]) -> pl.DataFrame:
    u = bars.filter(pl.col("symbol").is_in(symbols)).with_columns(
        pl.when(pl.col("currency") == "GBp").then(pl.col("close") / 100).otherwise(pl.col("close")).alias("close"),
        pl.col("currency").alias("quote_currency"))
    return compute_adjustment_factors(u, actions.select("security_id", "ex_date", "action_type", "ratio", "amount"))


def _decade_jumps(df: pl.DataFrame) -> pl.DataFrame:
    a = df.sort("security_id", "date").with_columns((pl.col("close") * pl.col("adj_factor")).alias("a"))
    return a.with_columns((pl.col("a") / pl.col("a").shift(1).over("security_id")).alias("r")).filter(
        (pl.col("r") > 30) | (pl.col("r") < 1 / 30))


def test_rose_l_gbp_flips_repaired(bars, actions):
    w = _finalized(bars, actions, ["ROSE.L"])
    assert _decade_jumps(w).height == 4  # two GBp->GBP->GBp round trips
    fixed, rep = PI.repair_unit_scale(w)
    assert rep["by_reason"] == {"flip_pair": 2} and rep["unresolved"] == 0
    assert _decade_jumps(fixed).is_empty()
    feb = fixed.filter(pl.col("date") == date(2025, 2, 24)).row(0, named=True)
    assert feb["close"] == pytest.approx(7.2) and feb["__unit_fix"] is True


def test_dia_mc_double_unadjusted_reverse_split_repaired(bars, actions):
    w = _finalized(bars, actions, ["DIA.MC"])
    assert _decade_jumps(w).height == 1
    fixed, rep = PI.repair_unit_scale(w)
    assert rep["by_reason"] == {"double_unadjusted": 1}
    assert _decade_jumps(fixed).is_empty()
    jan = fixed.filter(pl.col("date") == date(2025, 1, 29)).row(0, named=True)
    assert jan["close"] == pytest.approx(0.0181, rel=1e-2)          # raw pre-split print (EUR 0.018)
    assert jan["close"] * jan["adj_factor"] == pytest.approx(18.1, rel=1e-2)
    assert jan["volume"] == pytest.approx(1.493e7, rel=1e-3)        # Yahoo volume was inflated 1000x too


def test_unrecorded_split_fixes_factors_not_prints():
    n = 40
    d0 = date(2025, 3, 3)
    days = [d0 + timedelta(days=i) for i in range(n * 2) if (d0 + timedelta(days=i)).weekday() < 5][:n]
    df = pl.DataFrame({"security_id": "S", "date": days, "close": [0.05] * 20 + [5.0] * 20,
                       "adj_factor": 1.0, "split_factor": 1.0, "volume": [1e8] * 20 + [1e6] * 20})
    fixed, rep = PI.repair_unit_scale(df)
    assert rep["by_reason"] == {"unrecorded_split": 1}
    pre = fixed.filter(pl.col("date") == days[0]).row(0, named=True)
    assert pre["close"] == pytest.approx(0.05) and pre["adj_factor"] == pytest.approx(100.0)


def test_real_crash_is_not_rescaled():
    days = [date(2025, 5, 1) + timedelta(days=i) for i in range(60)]
    df = pl.DataFrame({"security_id": "C", "date": days, "close": [4.0] * 30 + [0.04] * 30, "adj_factor": 1.0,
                       "volume": [1e6] * 30 + [3e6] * 30, "currency": "USD"})
    fixed, rep = PI.repair_unit_scale(df)
    assert rep["bars_rescaled"] == 0 and rep["unresolved"] == 1
    assert fixed.get_column("close").to_list() == df.get_column("close").to_list()


def test_fx_scale_glitch_flip_pair():
    days = [date(2013, 1, 1) + timedelta(days=i) for i in range(40)]
    v = [20000.0] * 15 + [2000.0] * 3 + [20000.0] * 22
    g = PI.fx_scale_glitches(pl.DataFrame({"security_id": "FX:USDVND", "date": days, "close": v}))
    assert g.height == 1 and g.item(0, "start") == days[15] and g.item(0, "end") == days[17]


# ------------------------------------------------------------------------------------------------ sessions


def test_session_coverage_flags_missing_and_partial():
    sess = [date(2026, 10, d) for d in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16)]
    n = {d: 336 for d in sess}
    n[date(2026, 10, 12)] = 1     # UK on 2026-10-12: 1 bar of 336 (audit F05)
    n[date(2026, 10, 14)] = 120   # partial
    counts = pl.DataFrame({"calendar": "XLON", "date": list(n), "n": list(n.values())})
    gaps = PI.session_coverage(counts, {"XLON": sess}, min_coverage=0.5)
    got = {r["session_date"]: r["status"] for r in gaps.to_dicts()}
    assert got == {date(2026, 10, 12): "partial", date(2026, 10, 14): "partial"}
    counts2 = counts.filter(pl.col("date") != date(2026, 10, 12))
    got2 = {r["session_date"]: r["status"] for r in PI.session_coverage(counts2, {"XLON": sess}).to_dicts()}
    assert got2[date(2026, 10, 12)] == "missing"


def test_session_calendar_holidays_are_not_gaps():
    from app.normalize.prices import trading_sessions

    jp = trading_sessions("XTKS", date(2026, 10, 5), date(2026, 10, 16))
    assert jp is not None and date(2026, 10, 12) not in jp      # Sports Day: JP closed
    uk = trading_sessions("XLON", date(2026, 10, 5), date(2026, 10, 16))
    assert uk is not None and date(2026, 10, 12) in uk           # London open: a gap there is real
    counts = pl.DataFrame({"calendar": "XTKS", "date": sorted(jp), "n": [3650] * len(jp)})
    assert PI.session_coverage(counts, {"XTKS": jp}).is_empty()


def test_drop_unfinished_bars_live_session():
    # fetch at 2026-10-13 09:21 Lisbon (08:21 UTC): XLON 10-13 session open, XKRX 10-13 closed, XNYS 10-12 closed
    now = datetime(2026, 10, 13, 8, 21, tzinfo=UTC)
    df = pl.DataFrame({"symbol": ["SHEL.L", "SHEL.L", "005930.KS", "AAPL"],
                       "date": [date(2026, 10, 9), date(2026, 10, 13), date(2026, 10, 13), date(2026, 10, 12)],
                       "close": [3793.5, 3830.5, 232000.0, 250.0]})
    out, n = PI.drop_unfinished_bars(df, now=now)
    assert n == 1
    assert out.filter(pl.col("symbol") == "SHEL.L").get_column("date").to_list() == [date(2026, 10, 9)]
    assert out.filter(pl.col("symbol") == "005930.KS").height == 1 and out.filter(pl.col("symbol") == "AAPL").height == 1


def test_drop_unfinished_staged_by_ingest_time():
    # SHEL.L 2026-10-13 fetched 11:25 Lisbon (10:25 UTC), London closes 15:30 UTC -> partial bar, dropped;
    # older yfinance bars and Polygon bars are kept
    ing = datetime(2026, 10, 13, 10, 25, tzinfo=UTC)
    res = pl.DataFrame({"source_id": ["yfinance", "yfinance", "polygon"], "calendar": ["XLON", "XLON", "XNYS"],
                        "date": [date(2026, 10, 13), date(2026, 10, 9), date(2026, 10, 12)],
                        "ingested_at": [ing, ing, ing], "close": [3830.5, 3793.5, 250.0]})
    out, n = PI.drop_unfinished_staged(res, now=datetime(2026, 10, 14, tzinfo=UTC))
    assert n == 1 and out.filter(pl.col("date") == date(2026, 10, 13)).is_empty()
    later = res.with_columns(pl.lit(datetime(2026, 10, 13, 23, 35, tzinfo=UTC)).alias("ingested_at"))
    assert PI.drop_unfinished_staged(later, now=datetime(2026, 10, 14, tzinfo=UTC))[1] == 0
