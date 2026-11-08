"""PAUSE flag: the nightly gdelt_stream holds data/state/events_v2/PAUSE (try/finally) and the deep backfill issues no
new GDELT download while it is set (file-level yield, nothing lost); stale flags (dead owner / too old) are ignored."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date

from app.events_v2 import pause as P
from app.events_v2.pipeline import Streamer, update_state, watermark_days


def test_priority_sets_and_always_clears(tmp_data):
    assert not P.pause_active(tmp_data)
    try:
        with P.gdelt_priority(tmp_data):
            assert P.pause_path(tmp_data).exists()
            assert P.pause_active(tmp_data)
            assert P.read_pause(tmp_data)["pid"] == os.getpid()
            raise RuntimeError("job failed")
    except RuntimeError:
        pass
    assert not P.pause_path(tmp_data).exists()
    assert not P.pause_active(tmp_data)


def test_stale_flags_are_ignored(tmp_data):
    p = P.pause_path(tmp_data)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"owner": "gdelt_stream", "pid": 2_000_000_000}), encoding="utf-8")  # no such process
    assert not P.pause_active(tmp_data)
    p.write_text(json.dumps({"owner": "gdelt_stream", "pid": os.getpid()}), encoding="utf-8")
    assert P.pause_active(tmp_data)
    old = time.time() - (P.MAX_AGE_H + 1) * 3600
    os.utime(p, (old, old))
    assert not P.pause_active(tmp_data)


def test_backfill_downloads_wait_while_paused(tmp_data):
    st = Streamer.__new__(Streamer)  # no process pool / HTTP client needed for _fetch's pause gate
    calls: list[float] = []

    class _Http:
        def get(self, url, cache_ttl=0):  # noqa: ANN001
            calls.append(time.monotonic())
            return type("R", (), {"content": b"zip"})()

    st.http = _Http()
    st.pause_check = lambda: P.pause_active(tmp_data)
    P.set_pause(tmp_data)
    out: list = []
    th = threading.Thread(target=lambda: out.append(st._fetch("https://x/f.zip")))
    import app.events_v2.pipeline as pl_mod

    real_sleep = pl_mod.time.sleep
    pl_mod.time.sleep = lambda s: real_sleep(0.05)  # fast poll in the test
    try:
        th.start()
        real_sleep(0.4)
        assert calls == [] and th.is_alive()  # paused: no request issued
        P.clear_pause(tmp_data)
        th.join(5)
    finally:
        pl_mod.time.sleep = real_sleep
    assert len(calls) == 1 and out[0][0] == b"zip"


def test_watermark_days(tmp_data, monkeypatch):
    def upd(stt):
        stt["days"] = {"2026-10-09": {}, "2026-10-10": {}, "2020-01-01": {}}
    update_state(upd, tmp_data)
    assert watermark_days(tmp_data, end=date(2026, 10, 12)) == [date(2026, 10, 11), date(2026, 10, 12)]
    assert watermark_days(tmp_data, end=date(2026, 10, 10)) == []
    assert watermark_days(tmp_data, end=date(2026, 10, 20), max_days=2) == [date(2026, 10, 11), date(2026, 10, 12)]


def test_cancelled_stream_skips_downloads(tmp_data):
    st = Streamer.__new__(Streamer)
    st.pause_check = None
    st.stop_check = lambda: True

    class _Http:
        def get(self, url, cache_ttl=0):  # noqa: ANN001
            raise AssertionError("no download after cancellation")

    st.http = _Http()
    assert st._fetch("https://x/f.zip") == (None, 0.0)
