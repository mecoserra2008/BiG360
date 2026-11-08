"""Coverage v3 A7: GET /system/status is computed in the background (app.api.status_cache) - single flight, short TTL,
stale-while-revalidate, bounded cold wait, last good payload kept on errors, `as_of` + `cache` on every response."""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.api.status_cache import BackgroundCache
from tests.api_contract import validate


class _Slow:
    def __init__(self, delay: float = 0.3):
        self.delay = delay
        self.calls = 0
        self.fail = False
        self.tag = "R1"
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
            n = self.calls
        time.sleep(self.delay)
        if self.fail:
            raise ValueError("jobs.yaml invalid: soft dependency 'x' must also be listed in depends_on")
        return {"n": n}, self.tag


def test_single_flight_on_cold_cache():
    f = _Slow(0.3)
    c = BackgroundCache("t", f, ttl_s=60, cold_wait_s=5)
    out: list = []
    ths = [threading.Thread(target=lambda: out.append(c.get("k", "R1"))) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    assert f.calls == 1                                   # 8 concurrent cold requests -> one computation
    assert all(e is not None and e.value == {"n": 1} for e, _ in out)
    assert all(m["state"] == "fresh" and m["computed_at"] for _, m in out)


def test_stale_while_revalidate_never_blocks():
    f = _Slow(0.4)
    c = BackgroundCache("t", f, ttl_s=0.05, cold_wait_s=5)
    c.get("k", "R1")
    time.sleep(0.1)                                       # past the TTL
    t0 = time.monotonic()
    e, m = c.get("k", "R1")
    assert time.monotonic() - t0 < 0.2                    # served the cached copy, did not wait for the 0.4 s refresh
    assert e.value == {"n": 1} and m["state"] == "stale" and m["refreshing"] and m["age_s"] >= 0.05
    for _ in range(5):                                    # pollers during the refresh start no second computation
        c.get("k", "R1")
    assert c.wait_idle(5)
    assert f.calls == 2
    c.ttl_s = 60
    e, m = c.get("k", "R1")
    assert e.value == {"n": 2} and m["state"] == "fresh" and not m["refreshing"]


def test_snapshot_switch_triggers_refresh_but_serves_previous_copy():
    f = _Slow(0.2)
    c = BackgroundCache("t", f, ttl_s=60, cold_wait_s=5)
    c.get("k", "R1")
    f.tag = "R2"
    e, m = c.get("k", "R2")                               # new snapshot: refresh in the background, old copy now
    assert e.tag == "R1" and m["state"] == "stale" and m["snapshot_run_id"] == "R1"
    assert c.wait_idle(5)
    e, m = c.get("k", "R2")
    assert e.tag == "R2" and m["state"] == "fresh" and f.calls == 2


def test_failure_keeps_last_good_payload():
    f = _Slow(0.0)
    c = BackgroundCache("t", f, ttl_s=0.01, cold_wait_s=5)
    c.get("k")
    f.fail = True
    time.sleep(0.02)
    c.get("k")
    assert c.wait_idle(5)
    e, m = c.get("k")
    assert e.value == {"n": 1}                            # the previous payload, not an error
    assert "jobs.yaml invalid" in m["last_error"] and m["last_error_at"]


def test_cold_wait_is_bounded():
    f = _Slow(1.0)
    c = BackgroundCache("t", f, ttl_s=60, cold_wait_s=0.1)
    t0 = time.monotonic()
    e, m = c.get("k")
    assert time.monotonic() - t0 < 0.6
    assert e is None and m["state"] == "warming" and m["refreshing"]
    assert c.wait_idle(5) and c.get("k")[0].value == {"n": 1}


def test_other_key_is_cold_and_reset_discards_running_result():
    f = _Slow(0.2)
    c = BackgroundCache("t", f, ttl_s=60, cold_wait_s=5)
    c.get("a")
    e, _ = c.get("b")                                     # another data dir never sees the first one's payload
    assert e.value == {"n": 2}
    c.ttl_s, c.cold_wait_s = 60, 0.0
    c.reset()
    c.get("b")                                            # starts computation 3, returns at once (cold wait 0)
    c.reset()                                             # ... whose result is discarded
    time.sleep(0.35)
    assert c.peek("b") is None


def test_ttl_zero_computes_inline():
    f = _Slow(0.0)
    c = BackgroundCache("t", f, ttl_s=0, cold_wait_s=5)
    assert c.get("k")[0].value == {"n": 1} and c.get("k")[0].value == {"n": 2}


# ------------------------------------------------------------------------------------------------ the endpoint


@pytest.fixture()
def client(tmp_data, monkeypatch):
    from app.api.deps import snapshots
    from app.api.main import create_app
    from app.api.routers import system

    snapshots.reset()
    system.reset_status_cache()
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "60")
    monkeypatch.setenv("BIG360_STATUS_COLD_WAIT_S", "10")
    yield TestClient(create_app()), system
    system.reset_status_cache()
    snapshots.reset()


def test_status_endpoint_serves_cached_copy_with_as_of(client, monkeypatch):
    c, system = client
    calls = []
    real = system._compute_status

    def counting(db):
        calls.append(1)
        return real(db)

    monkeypatch.setattr(system, "_compute_status", counting)
    r1 = c.get("/api/v1/system/status")
    assert r1.status_code == 200
    b1 = r1.json()
    validate("GET", "/api/v1/system/status", 200, b1)
    assert b1["as_of"] and b1["cache"]["state"] == "fresh" and b1["cache"]["ttl_s"] == 60
    assert b1["cache"]["snapshot_run_id"] == "empty" and b1["serving_run_id"] is None
    assert set(b1["keys_present"]) >= {"fred", "polygon"}
    for _ in range(5):
        b = c.get("/api/v1/system/status").json()
        assert b["as_of"] == b1["as_of"]                  # same cached payload
    assert len(calls) == 1


def test_status_endpoint_never_500_on_compute_errors(client, monkeypatch):
    c, system = client

    def broken(db):
        raise ValueError("jobs.yaml invalid: tech_meta_score: soft dependency 'macro_factors' ...")

    monkeypatch.setattr(system, "_compute_status", broken)
    r = c.get("/api/v1/system/status")                    # cold cache + failed computation -> light payload, 200
    assert r.status_code == 200
    b = r.json()
    validate("GET", "/api/v1/system/status", 200, b)
    assert b["as_of"] is None and b["cache"]["state"] == "error" and "jobs.yaml invalid" in b["cache"]["last_error"]
    assert b["data_freshness"] == [] and b["version"]


def test_status_section_error_keeps_previous_section(client, monkeypatch):
    c, system = client
    good = c.get("/api/v1/system/status").json()
    assert good["cache"]["section_errors"] == []

    def bad_runs():
        raise RuntimeError("job_runs being rewritten")

    monkeypatch.setattr(system, "_dag_runs", bad_runs)
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "0.01")
    time.sleep(0.05)
    c.get("/api/v1/system/status")                        # stale -> background refresh with a failing section
    assert system._STATUS.wait_idle(10)
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "60")
    b = c.get("/api/v1/system/status").json()
    validate("GET", "/api/v1/system/status", 200, b)
    assert b["as_of"] != good["as_of"] and b["last_dag_runs"] == good["last_dag_runs"]
    assert any(e.startswith("last_dag_runs: RuntimeError") for e in b["cache"]["section_errors"])
    # review fix: not 'fresh' - the reused section keeps its original computation time
    assert b["cache"]["state"] == "partial"
    assert b["cache"]["section_as_of"]["last_dag_runs"] == good["cache"]["section_as_of"]["last_dag_runs"]
    assert b["cache"]["section_as_of"]["data_freshness"] > good["cache"]["section_as_of"]["data_freshness"]


def _refresh(c, system, monkeypatch) -> dict:
    """One background refresh past the TTL, then the served payload."""
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "0.01")
    time.sleep(0.05)
    c.get("/api/v1/system/status")
    assert system._STATUS.wait_idle(10)
    monkeypatch.setenv("BIG360_STATUS_TTL_S", "60")
    return c.get("/api/v1/system/status").json()


def test_failed_section_is_not_reused_past_the_cap(client, monkeypatch):
    """A section that keeps failing is served empty (with its error) once its last good value is older than
    BIG360_STATUS_SECTION_MAX_REUSE_S - stale freshness rows never pass for current indefinitely."""
    c, system = client
    good = c.get("/api/v1/system/status").json()
    assert good["data_freshness"] and good["cache"]["state"] == "fresh"

    def bad():
        raise ValueError("jobs.yaml invalid")

    monkeypatch.setattr(system, "_freshness", lambda db: bad())
    b = _refresh(c, system, monkeypatch)                 # within the cap: last good rows, original time
    assert b["data_freshness"] == good["data_freshness"] and b["cache"]["state"] == "partial"
    assert b["cache"]["section_as_of"]["data_freshness"] == good["cache"]["section_as_of"]["data_freshness"]
    b = _refresh(c, system, monkeypatch)                 # reuse of a reused value keeps the ORIGINAL time
    assert b["cache"]["section_as_of"]["data_freshness"] == good["cache"]["section_as_of"]["data_freshness"]
    monkeypatch.setenv("BIG360_STATUS_SECTION_MAX_REUSE_S", "0")
    b = _refresh(c, system, monkeypatch)                 # past the cap: empty + the error, no as-of
    validate("GET", "/api/v1/system/status", 200, b)
    assert b["data_freshness"] == [] and "data_freshness" not in b["cache"]["section_as_of"]
    assert any(e.startswith("data_freshness: ValueError") for e in b["cache"]["section_errors"])


def test_broken_registry_keeps_previous_ops_block(client, monkeypatch):
    """The jobs.yaml mid-edit failure: scheduler / ops alerts keep their last good value (section 'ops_status'), never
    'scheduler None, no alerts, state fresh' (the tray re-toasted every alert after that)."""
    c, system = client
    good = c.get("/api/v1/system/status").json()
    assert good["scheduler"] is not None and good["ops_alerts"] and good["cache"]["section_errors"] == []

    def broken():
        raise ValueError("jobs.yaml invalid: soft dependency 'x' must also be listed in depends_on")

    monkeypatch.setattr(system, "_registry", broken)
    with system._TTL_LOCK:
        system._TTL.clear()                              # registry / scheduler TTL caches
    b = _refresh(c, system, monkeypatch)
    validate("GET", "/api/v1/system/status", 200, b)
    assert b["cache"]["state"] == "partial"
    assert any(e.startswith("ops_status: ValueError: jobs.yaml invalid") for e in b["cache"]["section_errors"])
    assert b["scheduler"] == good["scheduler"] and b["ops_alerts"] == good["ops_alerts"]
    assert b["data_as_of"] == good["data_as_of"] and b["serving_sections"] == good["serving_sections"]
    monkeypatch.setenv("BIG360_STATUS_SECTION_MAX_REUSE_S", "0")
    b = _refresh(c, system, monkeypatch)                 # past the cap: empty, still flagged
    assert b["scheduler"] is None and b["ops_alerts"] == [] and b["cache"]["state"] == "partial"
    assert any(e.startswith("ops_status") for e in b["cache"]["section_errors"])
