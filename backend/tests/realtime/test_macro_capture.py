"""R1.2 macro release capture with a fake clock: schedule-driven polling, actual in the release measure, surprise vs
the stored model-based expectation, idempotent key, poll cadence."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from app.realtime.streams.base import StreamContext
from app.realtime.streams.macro_capture import MacroCaptureStream, Pending, measure_value, poll_every, surprise

SCHED = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)   # 08:30 ET


class FakeClock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def levels(with_sep: bool) -> pd.Series:
    idx = [date(2026, m, 1) for m in range(1, 9 + int(with_sep))]
    vals = [300.0 + i for i in range(len(idx))]
    if with_sep:
        vals[-1] = vals[-2] * 1.004   # +0.4% m/m in September
    return pd.Series(vals, index=idx, dtype=float)


def make_pending() -> Pending:
    return Pending(release_event_id="RE1", series_id="fred:CPIAUCSL", country="US", period=date(2026, 9, 1),
                   measure="mom_pct", freq="M", scheduled_at=SCHED, time_known=True, release_name="CPI",
                   publisher="BLS", importance=1, theme="inflation", direction=1, prior=0.33,
                   expectation={"point": 0.2, "lo80": 0.05, "hi80": 0.35, "model": "sarima", "version": "v2"},
                   scale=0.1)


def ctx_with(clock: FakeClock, settings) -> StreamContext:
    return StreamContext(settings=settings, bus=None, out=asyncio.Queue(), stop=asyncio.Event(),  # type: ignore[arg-type]
                         clock=clock)


def test_measure_and_surprise_helpers():
    cur, prev = measure_value(levels(True), date(2026, 9, 1), "mom_pct", "M")
    assert cur == pytest.approx(0.4, abs=1e-9) and prev == pytest.approx(100 * (307 / 306 - 1))
    assert measure_value(levels(False), date(2026, 9, 1), "mom_pct", "M") == (None, None)
    s = surprise(0.4, {"point": 0.2}, 0.1, -1)
    assert s["error"] == pytest.approx(0.2) and s["surprise_z"] == pytest.approx(2.0) and s["signed_z"] == pytest.approx(-2.0)
    assert surprise(10.0, {"point": 0.0}, 0.1, 1)["surprise_z"] == 5.0          # clipped
    assert surprise(0.4, None, 0.1, 1)["surprise_z"] is None
    assert [poll_every(x) for x in (0, 599, 600, 3599, 3600)] == [15, 15, 60, 60, 300]


def test_capture_flow_with_fake_clock(tmp_data):
    clock = FakeClock(SCHED - timedelta(minutes=5))
    calls: list[tuple[datetime, str]] = []
    published = {"sep": False}

    def fetcher(sid: str) -> pd.Series:
        calls.append((clock(), sid))
        return levels(published["sep"])

    w = MacroCaptureStream(schedule_loader=lambda now: [make_pending()], fetcher=fetcher)
    ctx = ctx_with(clock, tmp_data)
    # before the scheduled time: nothing is polled; the next wake-up is the scheduled time (capped at 60 s)
    assert w.run_due(ctx) == [] and calls == []
    assert w.interval(ctx) == pytest.approx(60.0)
    clock.t = SCHED - timedelta(seconds=20)
    assert w.interval(ctx) == pytest.approx(20.0)
    # at the scheduled time the source is polled; the period is not there yet
    clock.t = SCHED
    assert w.run_due(ctx) == [] and len(calls) == 1
    assert w.interval(ctx) == pytest.approx(15.0)        # fast cadence in the first 10 minutes
    clock.t = SCHED + timedelta(seconds=5)
    assert w.run_due(ctx) == [] and len(calls) == 1      # not due yet
    # the actual appears 40 s after the scheduled time
    published["sep"] = True
    clock.t = SCHED + timedelta(seconds=40)
    out = w.run_due(ctx)
    assert len(out) == 1 and len(calls) == 2
    e = out[0]
    assert e.topic == "raw.macro" and e.key == "RE1|fred:CPIAUCSL" and e.type == "release" and e.ts_source == SCHED
    p = e.payload
    assert p["actual"] == pytest.approx(0.4) and p["expectation"] == pytest.approx(0.2)
    assert p["surprise_z"] == pytest.approx(2.0) and p["signed_z"] == pytest.approx(2.0)
    assert p["capture_delay_s"] == pytest.approx(40.0) and p["model_based"] is True and "not consensus" in p["label"]
    assert p["attempts"] == 2 and p["period"] == "2026-09-01"
    # captured entries are never polled again, even after a schedule reload
    clock.t = SCHED + timedelta(minutes=30)
    w._loaded_at = -1e18
    assert w.run_due(ctx) == [] and len(calls) == 2


def test_capture_window_expires_and_errors_are_isolated(tmp_data):
    clock = FakeClock(SCHED)
    p2 = make_pending()
    p2.series_id, p2.release_event_id = "fred:UNRATE", "RE2"

    def fetcher(sid: str) -> pd.Series:
        if sid == "fred:UNRATE":
            raise RuntimeError("source down")
        return levels(True)

    w = MacroCaptureStream(schedule_loader=lambda now: [make_pending(), p2], fetcher=fetcher)
    ctx = ctx_with(clock, tmp_data)
    out = w.run_due(ctx)
    assert [e.key for e in out] == ["RE1|fred:CPIAUCSL"]                  # the failing series does not block
    assert w.pending["RE2|fred:UNRATE"].last_error.startswith("RuntimeError")
    clock.t = SCHED + timedelta(hours=7)                                  # past the 6 h window
    assert w.run_due(ctx) == [] and "RE2|fred:UNRATE" not in w.pending


def test_unknown_time_starts_early():
    p = make_pending()
    p.time_known = False
    assert p.active_from() == SCHED - timedelta(minutes=30)
    assert p.active_until(6) == SCHED + timedelta(hours=24)
