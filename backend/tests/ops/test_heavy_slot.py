"""Exclusive heavy slot for build_serving_db (refresh-pipeline fixes 2026-10-26): the build never overlaps a heavy
(process) job, heavy jobs wait while it runs, the hourly news jobs are deferred, the filler yields, and a crashed
owner never blocks the slot."""

from __future__ import annotations

import json
import threading
import time

import pytest

from app.ops import governor as G
from app.ops import heavy_slot as HS


@pytest.fixture()
def slot(tmp_data, monkeypatch):
    monkeypatch.setenv("BIG360_HEAVY_SLOT", "1")
    return tmp_data


def test_exclusive_waits_for_running_heavy_job(slot):
    heavy, why = HS.acquire_heavy("tech_levels", 600, slot)
    assert heavy is not None and why is None
    got: dict = {}

    def build() -> None:
        t0 = time.monotonic()
        got["m"] = HS.acquire_exclusive("build_serving_db", 600, slot, poll_s=0.05, wait_max_s=10)
        got["waited"] = time.monotonic() - t0

    th = threading.Thread(target=build)
    th.start()
    time.sleep(0.4)
    assert "m" not in got                                  # still waiting for the heavy job
    assert HS.exclusive_holder(slot)["job_id"] == "build_serving_db"   # announced: nothing new may start
    heavy.release()
    th.join(5)
    assert got["m"] is not None and got["waited"] >= 0.3
    got["m"].release()
    assert HS.exclusive_holder(slot) is None


def test_heavy_job_waits_or_defers_while_exclusive_held(slot):
    ex = HS.acquire_exclusive("build_serving_db", 600, slot, poll_s=0.05, wait_max_s=1)
    m, why = HS.acquire_heavy("news_finalise", 600, slot, defer=True)
    assert m is None and "build_serving_db" in why          # hourly jobs: deferred to the next run
    got: dict = {}

    def heavy() -> None:
        got["r"] = HS.acquire_heavy("conformal_envelopes", 600, slot, poll_s=0.05, wait_max_s=10)

    th = threading.Thread(target=heavy)
    th.start()
    time.sleep(0.3)
    assert "r" not in got                                  # waits, does not overlap the build
    assert HS.holders("heavy", slot) == []                 # withdrew its marker while waiting
    ex.release()
    th.join(5)
    assert got["r"][0] is not None and got["r"][1] is None
    got["r"][0].release()


def test_stale_markers_never_block(slot):
    d = HS.slot_dir(slot)
    (d / "exclusive-build_serving_db-999999-dead.json").write_text(
        json.dumps({"job_id": "build_serving_db", "pid": 999999, "expires": time.time() + 3600}), encoding="utf-8")
    (d / "heavy-x-1-old.json").write_text(json.dumps({"job_id": "x", "pid": 1, "expires": time.time() - 1}),
                                          encoding="utf-8")
    assert HS.exclusive_holder(slot) is None               # dead owner pid
    m, why = HS.acquire_heavy("tech_levels", 60, slot)
    assert m is not None and why is None
    m.release()
    assert HS.holders("heavy", slot) == []                 # expired marker ignored ...
    assert not list(d.glob("*.json"))                      # ... and every stale file removed


def test_resource_classes():
    assert HS.resource_class("build_serving_db", "thread") == "exclusive"
    assert HS.resource_class("tech_levels", "process") == "heavy"
    assert HS.resource_class("fx_rates", "thread") == "light"
    assert HS.resource_class("event_hotspots", "thread", "heavy") == "heavy"


def test_governor_yields_to_the_serving_build():
    running = [G.RunningInfo("campaign:e6", 70, 2.0, heavy=True), G.RunningInfo("gap:fx", 40, 0.2)]
    d = G.decide(G.Sample(free_ram_gb=12.0, dags_running=[G.EXCLUSIVE_SLOT]), running,
                 {"yield_to_dags": []})                    # even with an empty yield list
    assert d.state == "yielding" and not d.allow_new
    assert d.stop == ["campaign:e6"]                       # heavy task stopped; the light one finishes


def test_runner_defers_and_serialises(slot, monkeypatch):
    from app.scheduler.runner import JobRunner, JobSpec

    r = JobRunner.__new__(JobRunner)
    r.settings = slot
    r.stop_event = threading.Event()
    ex = HS.acquire_exclusive("build_serving_db", 600, slot, poll_s=0.05, wait_max_s=1)
    try:
        spec = JobSpec(id="news_hourly", dag="hourly", module="x", executor="process")
        m, why = r._acquire_slot(spec)
        assert m is None and "exclusive heavy slot" in why
        light = JobSpec(id="fx_rates", dag="nightly", module="x")
        assert r._acquire_slot(light) == (None, None)
    finally:
        ex.release()
