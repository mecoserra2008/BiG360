"""Coverage v3 A2: heavy jobs strictly one at a time. A job with a RAM budget >= 7 GB takes the heavy slot
exclusively: no other job >= 3 GB runs beside it (any runner), bigs queue FIFO, medium jobs back off while a big
waits or holds, small jobs are unaffected, the filler's heavy tasks yield, and every wait has a visible reason."""

from __future__ import annotations

import threading
import time

import pytest

from app.ops import governor as G
from app.ops import heavy_slot as HS
from app.ops import ram_budget as RB


@pytest.fixture()
def slot(tmp_data, monkeypatch):
    monkeypatch.setenv("BIG360_HEAVY_SLOT", "1")
    monkeypatch.delenv("BIG360_HEAVY_SLOT_GB", raising=False)
    monkeypatch.delenv("BIG360_HEAVY_PEER_GB", raising=False)
    return tmp_data


def _bg(fn, *a, **kw):
    out: dict = {}

    def run() -> None:
        t0 = time.monotonic()
        out["r"] = fn(*a, **kw)
        out["waited"] = time.monotonic() - t0

    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, out


def test_resource_class_by_budget(monkeypatch):
    assert HS.resource_class("prices_finalize", "process", None, 10.0) == "big"
    assert HS.resource_class("tech_levels", "process", None, 7.0) == "big"
    assert HS.resource_class("ratios_kpis", "process", None, 6.0) == "heavy"
    assert HS.resource_class("ml_features", "thread", None, 3.0) == "heavy"     # thread job >= 3 GB is a peer
    assert HS.resource_class("fx_rates", "thread", None, 0.5) == "light"
    assert HS.resource_class("build_serving_db", "process", None, 9.0) == "exclusive"
    assert HS.resource_class("serving_sections_publish", "process", "exclusive", 9.0) == "exclusive"
    monkeypatch.setenv("BIG360_HEAVY_SLOT_GB", "8")
    assert HS.resource_class("tech_levels", "process", None, 7.0) == "heavy"


def test_big_waits_for_peers_but_not_for_small_jobs(slot):
    small, _ = HS.acquire_heavy("news_hourly", 600, slot, mem_gb=2.0)
    medium, _ = HS.acquire_heavy("ratios_kpis", 600, slot, mem_gb=6.0)
    th, got = _bg(HS.acquire_big, "prices_finalize", 10.0, 600, slot, poll_s=0.05, wait_max_s=30)
    time.sleep(0.4)
    assert "r" not in got                                                   # the 6 GB job still runs
    st = HS.slot_status(slot)
    assert [w["job_id"] for w in st["waiting"]] == ["prices_finalize"] and st["holder"] is None
    assert "ratios_kpis" in RB.current_waits()["prices_finalize"]["reason"]  # visible wait reason
    medium.release()
    th.join(5)
    m, why = got["r"]
    assert m is not None and why is None                                    # the 2 GB job does not block it
    assert HS.big_holder(slot)["job_id"] == "prices_finalize"
    # its companion heavy marker keeps the readers of heavy markers (QA smoke planner, studio lane) informed
    assert "prices_finalize" in {h["job_id"] for h in HS.holders("heavy", slot)}
    assert "prices_finalize" not in RB.current_waits()
    m.release()
    small.release()
    assert HS.holders("big", slot) == [] and HS.holders("heavy", slot) == []


def test_peers_never_join_a_holder_small_jobs_do(slot):
    m, _ = HS.acquire_big("confluence_zones", 9.0, 600, slot, poll_s=0.05)
    try:
        # a 4 GB job waits - even past its max wait (strict while a big holds the slot)
        th, got = _bg(HS.acquire_heavy, "tech_factors", 600, slot, poll_s=0.05, wait_max_s=0.2, mem_gb=4.0)
        time.sleep(0.6)
        assert "r" not in got and HS.holders("heavy", slot)[0]["job_id"] == "confluence_zones"
        mm, why = HS.acquire_heavy("story_cluster", 600, slot, defer=True, mem_gb=5.0)
        assert mm is None and "confluence_zones" in why
        small, why = HS.acquire_heavy("prices_global_yf", 600, slot, mem_gb=2.0)   # below the peer threshold
        assert small is not None and why is None
        small.release()
    finally:
        m.release()
    th.join(5)
    assert got["r"][0] is not None
    got["r"][0].release()


def test_bigs_queue_fifo_and_never_overlap(slot):
    a, _ = HS.acquire_big("prices_finalize", 10.0, 600, slot, poll_s=0.05)
    tb, gb = _bg(HS.acquire_big, "forecast", 8.5, 600, slot, poll_s=0.05)
    time.sleep(0.2)
    tc, gc = _bg(HS.acquire_big, "tech_levels", 7.0, 600, slot, poll_s=0.05)
    time.sleep(0.3)
    assert "r" not in gb and "r" not in gc
    assert [w["job_id"] for w in HS.slot_status(slot)["waiting"]] == ["forecast", "tech_levels"]
    a.release()
    tb.join(5)
    assert gb["r"][0] is not None and "r" not in gc                         # FIFO: forecast first
    time.sleep(0.3)
    assert "r" not in gc and HS.big_holder(slot)["job_id"] == "forecast"
    gb["r"][0].release()
    tc.join(5)
    assert gc["r"][0] is not None
    gc["r"][0].release()


def test_medium_backs_off_while_a_big_waits_and_times_out_only_then(slot):
    medium, _ = HS.acquire_heavy("ratios_kpis", 600, slot, mem_gb=6.0)
    tb, gb = _bg(HS.acquire_big, "regime_performance", 9.0, 600, slot, poll_s=0.05)
    time.sleep(0.2)
    m2, why = HS.acquire_heavy("sentiment_daily", 600, slot, defer=True, mem_gb=4.0)
    assert m2 is None and "regime_performance" in why and "claimed" in why
    # the big only waits (it does not hold): a medium job may run anyway after its own max wait
    m3, why = HS.acquire_heavy("sentiment_daily", 600, slot, poll_s=0.05, wait_max_s=0.2, mem_gb=4.0)
    assert m3 is not None and why is None
    m3.release()
    medium.release()
    tb.join(5)
    gb["r"][0].release()


def test_give_up_at_and_exclusive_interplay(slot):
    medium, _ = HS.acquire_heavy("ratios_kpis", 600, slot, mem_gb=6.0)
    m, why = HS.acquire_big("forecast", 8.5, 600, slot, poll_s=0.05, give_up_at=time.time() + 0.3)
    assert m is None and "start budget ran out" in why and HS.holders("big", slot) == []
    medium.release()
    big, _ = HS.acquire_big("forecast", 8.5, 600, slot, poll_s=0.05)
    th, got = _bg(HS.acquire_exclusive, "build_serving_db", 600, slot, poll_s=0.05, wait_max_s=10)
    time.sleep(0.3)
    assert "r" not in got                                                   # the serving build waits for the big job
    big.release()
    th.join(5)
    ex = got["r"]
    m, why = HS.acquire_big("tech_levels", 7.0, 600, slot, poll_s=0.05, give_up_at=time.time() + 0.3)
    assert m is None and "build_serving_db" in why                          # and a big job for the serving build
    ex.release()


def test_runner_uses_the_big_slot_and_prechecks(slot):
    from app.scheduler.runner import JobRunner, JobSpec, RunOptions

    r = JobRunner.__new__(JobRunner)
    r.settings = slot
    r.stop_event = threading.Event()
    big = JobSpec(id="prices_finalize", dag="nightly", module="x", executor="process", mem_gb=10.0)
    mid = JobSpec(id="ratios_kpis", dag="nightly", module="x", executor="process", mem_gb=6.0)
    small = JobSpec(id="news_hourly", dag="hourly", module="x", executor="process", mem_gb=2.0)
    m, why = r._acquire_slot(big)
    assert m is not None and m.kind == "big" and why is None
    try:
        assert "prices_finalize" in r._slot_busy(mid) and r._slot_busy(small) is None
        assert r._slot_busy(JobSpec(id="forecast", dag="nightly", module="x", executor="process", mem_gb=8.5))
        mm, why = r._acquire_slot(mid, RunOptions(start_by=time.time() + 0.2))
        assert mm is None and "start budget ran out" in why
    finally:
        m.release()
    assert r._slot_busy(mid) is None


def test_governor_heavy_slot_stops_heavy_tasks_only():
    running = [G.RunningInfo("campaign:e6", 70, 2.5, heavy=True), G.RunningInfo("gap:fx", 40, 0.2)]
    d = G.decide(G.Sample(free_ram_gb=12.0, dags_running=[G.HEAVY_SLOT]), running,
                 {"yield_to_dags": [], "max_concurrency": 3})
    assert d.stop == ["campaign:e6"] and d.block_heavy and d.allow_new
    assert not G.admit(3.0, d, 1, heavy=True, running=[running[1]])
    assert G.admit(0.3, d, 1, heavy=False, running=[running[1]])
    assert d.to_dict()["block_heavy"] is True
