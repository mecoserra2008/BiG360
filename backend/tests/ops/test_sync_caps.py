"""Coverage v3 A3: startup / interval sync caps. Interval syncs ran 33-39 h on 11-01..11-04; now each sync has a
wall-clock and a per-pass job budget (the rest is deferred with a reason and carried to the next sync, first in
line), per-job timeouts come from run history (p95 x 1.5, clamped), the runner defers jobs that cannot start in the
budget, a lock-owner record lets the supervisor recover a hung sync, and the summary says what was deferred and why.
BIG360_SYNC_CAPS=0 restores the previous behaviour."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import polars as pl
import pytest

from app.ops.sync import caps as C
from app.ops.sync import state as S
from app.ops.sync.run import SyncRun
from app.scheduler.runner import JobOutcome, RunReport
from tests.ops.test_sync_run import REG, FakeRunner, FakeScan, item
from tests.platform.conftest import make_registry


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BIG360_HEAVY_SLOT", "0")
    for k in ("BIG360_SYNC_CAPS", "BIG360_SYNC_MAX_WALL_MIN", "BIG360_SYNC_PASS_MAX_JOBS"):
        monkeypatch.delenv(k, raising=False)


# ------------------------------------------------------------------------------------------------ timeouts


def test_timeout_from_p95_with_floor_ceiling_and_fallbacks():
    caps = C.Caps()
    # cmf_cl_filings-like: usually ~2-3 min, contract 180 -> 10 min floor instead of 180
    t, src, q = C.timeout_for(180, [120, 130, 150, 180, 140], caps)
    assert (t, src) == (10.0, "p95") and q == pytest.approx(3.0)
    # fundamentals_normalize-like: p95 59 min, contract 60 -> 88.5 min (history knows better than the contract)
    t, src, _ = C.timeout_for(60, [3000, 3300, 3540], caps)
    assert (t, src) == (88.5, "p95")
    # ceiling: max(180, contract)
    assert C.timeout_for(60, [9000] * 5, caps)[0] == 180.0
    assert C.timeout_for(600, [24000] * 5, caps)[0] == 600.0                  # dart_bulk-like: the contract wins
    # too few runs -> contract; the latest run timed out under a shorter history timeout -> contract (escalation)
    assert C.timeout_for(45, [60, 60], caps)[:2] == (45, "contract")
    assert C.timeout_for(180, [120] * 5, caps, last_timed_out_below_contract=True)[:2] == (180, "escalated")
    assert C.timeout_for(30, [120] * 5, C.Caps(enabled=False))[:2] == (30, "contract")
    assert C.p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 10 and C.p95([]) is None


def test_history_timeouts_from_job_runs():
    reg = make_registry([{"id": "fast", "module": "x", "timeout_min": 180},
                         {"id": "grown", "module": "x", "timeout_min": 60},
                         {"id": "new", "module": "x", "timeout_min": 30}])
    now = datetime(2026, 11, 6, 12, tzinfo=UTC)
    rows = []
    for k in range(6):
        rows.append({"job_id": "fast", "status": "success", "duration_s": 100.0 + k, "error_type": None,
                     "started_at": now - timedelta(days=k + 1), "params": "{}"})
        rows.append({"job_id": "grown", "status": "success", "duration_s": 300.0, "error_type": None,
                     "started_at": now - timedelta(days=k + 1), "params": "{}"})
    rows.append({"job_id": "grown", "status": "failed", "duration_s": 600.0, "error_type": "TimeoutError",
                 "started_at": now - timedelta(hours=1), "params": json.dumps({"timeout_min": 10.0})})
    rows.append({"job_id": "fast", "status": "success", "duration_s": 99999.0, "error_type": None,
                 "started_at": now - timedelta(days=60), "params": "{}"})       # outside the 30-day window
    jr = pl.DataFrame(rows, schema={"job_id": pl.Utf8, "status": pl.Utf8, "duration_s": pl.Float64,
                                    "error_type": pl.Utf8, "started_at": pl.Datetime("us", "UTC"), "params": pl.Utf8})
    out = C.history_timeouts(jr, reg, C.Caps(), now)
    assert out["fast"]["timeout_min"] == 10.0 and out["fast"]["source"] == "p95" and out["fast"]["n"] == 6
    assert out["grown"]["source"] == "escalated" and out["grown"]["timeout_min"] == 60.0
    assert "new" not in out


# ------------------------------------------------------------------------------------------------ budget


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def test_budget_wall_jobs_and_fit():
    clk = Clock()
    b = C.Budget(C.Caps(max_wall_min=60, pass_max_jobs=2), t0=clk.t, clock=clk)
    assert b.can_start(1) == (True, None)
    b.note_start(1)
    b.note_start(1)
    ok, why = b.can_start(1)
    assert not ok and "maximum of 2 jobs" in why and b.hit["reason"] == why
    assert b.can_start(2)[0]                                                   # the job cap is per pass
    # p95 70 min > the WHOLE 60-min budget: it can never fit, so deferring it would starve it -> it starts
    assert b.can_start(2, expected_s=70 * 60) == (True, None)
    clk.t += 20 * 60                                                           # 40 min left
    ok, why = b.can_start(2, expected_s=50 * 60)                               # p95 50 min > 40 min left, < 60
    assert not ok and "first in line next sync" in why and "40 min left" in why
    assert b.can_start(2, expected_s=50 * 60, carried=True) == (True, None)    # carried: exempt from the fit rule
    assert b.can_start(2, expected_s=40 * 60) == (True, None)                  # boundary: exactly what is left fits
    assert not b.can_start(2, expected_s=40 * 60 + 1)[0]
    assert b.can_start(2, expected_s=60 * 60 + 1) == (True, None)              # just over the whole budget: starts
    clk.t += 41 * 60                                                           # e.g. the laptop slept
    ok, why = b.can_start(2, carried=True)
    assert not ok and "budget of 60 min used up" in why
    assert C.Budget(C.Caps(enabled=False, max_wall_min=1), t0=0, clock=clk).can_start(1) == (True, None)
    d = b.to_dict()
    assert d["jobs_started"] == {"1": 2} and d["hit"]["reason"].startswith("pass 1") and d["remaining_min"] == 0


def test_load_caps_from_config_env_and_kill_switch(monkeypatch):
    assert C.load_caps({}).max_wall_min == 120 and C.load_caps({}, "startup").max_wall_min == 240
    assert C.load_caps({"max_wall_min": 45, "pass_max_jobs": 9}).pass_max_jobs == 9
    monkeypatch.setenv("BIG360_SYNC_MAX_WALL_MIN", "30")
    monkeypatch.setenv("BIG360_SYNC_PASS_MAX_JOBS", "5")
    c = C.load_caps({"max_wall_min": 45})
    assert (c.max_wall_min, c.pass_max_jobs, c.enabled) == (30, 5, True)
    monkeypatch.setenv("BIG360_SYNC_CAPS", "0")
    assert C.load_caps({}).enabled is False


# ------------------------------------------------------------------------------------------------ sync runs


def _plan(jobs):
    return {j: item(j, f"d.{j}", "*") for j in jobs}


def _sync(s, plan, script=None, passes=1, runner=None, publish=False):
    runner = runner or FakeRunner(s, script)
    sr = SyncRun(s, trigger="interval", registry=REG, runner=runner,
                 scan_fn=FakeScan(plan, {f"d.{j}": ["*"] for j in plan}), publish=publish, passes=passes,
                 pressure_fn=lambda: None, sleep_s=0.02)
    return sr, runner


def test_pass_job_cap_defers_the_rest_and_carries_it(tmp_data, monkeypatch):
    monkeypatch.setenv("BIG360_SYNC_PASS_MAX_JOBS", "2")
    net = ["fx_rates", "macro_eu", "macro_intl", "cftc_cot"]
    sr, runner = _sync(tmp_data, _plan(net))
    out = sr.run()
    ran = [c[0] for c in runner.calls]
    assert len(ran) == 2
    deferred = {d["job"]: d for d in out["deferred"]}
    assert set(deferred) == set(net) - set(ran)
    assert all("maximum of 2 jobs" in d["reason"] for d in deferred.values())
    assert all(out["jobs"][j]["reason"].startswith("deferred: ") for j in deferred)
    assert out["caps"]["hit"]["reason"].startswith("pass 1 started its maximum")
    st = S.read_status(tmp_data)
    assert {d["job"] for d in st["deferred"]} == set(deferred) and st["caps"]["pass_max_jobs"] == 2
    carry = json.loads(C.carry_path(tmp_data).read_text(encoding="utf-8"))
    assert {e["job"] for e in carry["jobs"]} == set(deferred) and carry["run_id"] == sr.run_id
    assert all(e["times"] == 1 for e in carry["jobs"])
    hist = S.read_history(tmp_data)[0]
    assert len(hist["deferred"]) == 2 and hist["caps"]["hit"]


def test_carried_derive_job_runs_first_next_sync(tmp_data, monkeypatch):
    # a previous sync deferred tech_factors (an extended derive job) and a fetch job
    C.write_carry([{"job": "tech_factors", "pass_no": 2, "reason": "sync budget of 120 min used up",
                    "deferred_at": S.now_iso()},
                   {"job": "fx_rates", "pass_no": 1, "reason": "x", "deferred_at": S.now_iso()},
                   {"job": "ratios_kpis", "pass_no": 2, "reason": "old",
                    "deferred_at": (datetime.now(UTC) - timedelta(hours=30)).isoformat()}], "S0", tmp_data)
    carried = C.read_carry(tmp_data)
    assert set(carried) == {"tech_factors", "fx_rates"}                        # entries older than 24 h dropped
    monkeypatch.setattr(SyncRun, "gate", lambda self, n, nodes: (True, None))  # every input "changed"
    sr, runner = _sync(tmp_data, _plan(["sec_filings_index"]), passes=2)
    out = sr.run()
    assert "tech_factors" in out["carried_in"]
    node = sr.nodes[(2, "tech_factors")]
    assert node.carried == 1 and any("deferred by the previous sync" in w for w in node.why)
    assert "tech_factors" in [c[0] for c in runner.calls]
    # among ready nodes, carried work starts before the rest of its lane (network still first)
    from app.ops.sync.run import Node, ready_order

    a = Node("tech_factors", 2, "heavy", "extended", carried=1)
    b = Node("forecast", 2, "heavy", "extended")
    c = Node("fx_rates", 1, "network", "core")
    assert sorted([b, a, c], key=lambda n: ready_order(n, {"forecast": 300.0, "tech_factors": 5.0})) == [c, a, b]
    # it ran: dropped from the carry file
    left = {e["job"] for e in json.loads(C.carry_path(tmp_data).read_text(encoding="utf-8"))["jobs"]}
    assert "tech_factors" not in left


def test_exhausted_budget_defers_pass_two_and_the_guards(tmp_data, monkeypatch):
    monkeypatch.setenv("BIG360_SYNC_MAX_WALL_MIN", "1")
    sr, runner = _sync(tmp_data, _plan(["fx_rates", "sec_text"]), passes=2)
    real_execute = sr.execute
    calls = {"n": 0}

    def execute(nodes):
        calls["n"] += 1
        if calls["n"] == 2:                      # pass 2 starts after the wall budget is gone (e.g. a long pass 1)
            sr.budget.t0 -= 3600
        real_execute(nodes)

    sr.execute = execute
    out = sr.run()
    assert [c[0] for c in runner.calls] == ["fx_rates"]
    assert out["jobs"]["sec_text#2"]["reason"].startswith("deferred: sync budget of 1 min used up")
    assert out["caps"]["hit"]["reason"] == "sync budget of 1 min used up"
    # the QA / DQ guards wait for the next sync's budget
    from app.ops.sync.run import Node

    n = Node("qa_regression", 2, "light", "core", trigger="post")
    sr._run_job(n)
    assert n.status == "skipped" and n.reason.startswith("deferred: sync budget") and "the next sync runs it" in n.reason


def test_runner_deferred_outcome_is_carried(tmp_data):
    class Deferring(FakeRunner):
        def run_jobs(self, ids, opts):
            j = ids[0]
            self.calls.append((j, dict(opts.params)))
            assert opts.ram_defer and opts.start_by and opts.ram_wait_max_s >= 60
            o = JobOutcome(j, "skipped", "deferred", ["R1"], 0, 0.0, 0, "RamFloor",
                           "deferred: RAM floor: free 5.0 GB - reserved 0.0 GB - budget 7.0 GB = -2.0 GB < floor 3 GB")
            return RunReport(j, None, opts.as_of, {j: o}, datetime.now(UTC))

    sr, _ = _sync(tmp_data, _plan(["fx_rates"]), runner=Deferring(tmp_data))
    out = sr.run()
    assert out["jobs"]["fx_rates"]["reason"].startswith("deferred: RAM floor")
    assert [d["job"] for d in out["deferred"]] == ["fx_rates"]
    assert [e["job"] for e in json.loads(C.carry_path(tmp_data).read_text(encoding="utf-8"))["jobs"]] == ["fx_rates"]


class _DeferJobs(FakeRunner):
    """FakeRunner whose `defer` jobs come back deferred by the runner (RAM floor); records each call's ram_defer."""

    def __init__(self, settings, defer=(), **kw):
        super().__init__(settings, **kw)
        self.defer = set(defer)
        self.ram_defer: dict[str, bool] = {}

    def run_jobs(self, ids, opts):
        j = ids[0]
        self.ram_defer[j] = bool(opts.ram_defer)
        if j in self.defer and opts.ram_defer:
            self.calls.append((j, dict(opts.params)))
            o = JobOutcome(j, "skipped", "deferred", ["R1"], 0, 0.0, 0, "RamFloor",
                           "deferred: RAM floor: free 7.9 GB - reserved 0.5 GB - budget 10.0 GB = -2.6 GB < floor 3 GB")
            return RunReport(j, None, opts.as_of, {j: o}, datetime.now(UTC))
        return super().run_jobs(ids, opts)


def _prices_plan():
    from tests.ops.test_sync_run import item as it

    return {"prices_us_polygon": it("prices_us_polygon", "prices.US", "US"),
            "prices_finalize": it("prices_finalize", "prices.US", "US", trigger="upstream",
                                  params={"regions": ["US"]})}


def test_deferred_finalize_leaves_the_ledger_alone(tmp_data):
    """Review 2026-11-07: the fetchers succeeded and prices_finalize was deferred, so the period was still due - verify
    recorded not_yet_published ("prices_us_polygon ran but ... is still missing at the source") with back-off and parked
    it after 6 attempts. A deferred job of the period means it was not attempted end to end: no ledger entry."""
    from tests.ops.test_sync_run import FakeScan as FS

    runner = _DeferJobs(tmp_data, defer={"prices_finalize"})
    sr = SyncRun(tmp_data, trigger="interval", registry=REG, runner=runner,
                 scan_fn=FS(_prices_plan(), {"prices.US": ["US"]}, still={("prices.US", "US")}), publish=False,
                 passes=1, pressure_fn=lambda: None, sleep_s=0.02)
    out = sr.run()
    assert out["jobs"]["prices_us_polygon"]["status"] == "success"
    assert out["jobs"]["prices_finalize"]["reason"].startswith("deferred: RAM floor")
    assert S.Ledger(tmp_data).get("prices.US", "US", "2026-10-27") is None
    assert "prices_finalize" in {e["job"] for e in json.loads(C.carry_path(tmp_data).read_text(encoding="utf-8"))["jobs"]}
    # control: the same run with prices_finalize done blames the source as before
    runner2 = _DeferJobs(tmp_data)
    sr2 = SyncRun(tmp_data, trigger="interval", registry=REG, runner=runner2,
                  scan_fn=FS(_prices_plan(), {"prices.US": ["US"]}, still={("prices.US", "US")}), publish=False,
                  passes=1, pressure_fn=lambda: None, sleep_s=0.02)
    sr2.run()
    assert S.Ledger(tmp_data).get("prices.US", "US", "2026-10-27")["reason"] == "not_yet_published"


def test_carried_job_is_not_deferred_again(tmp_data):
    """Review 2026-11-07: a job a previous sync deferred asks the runner for no defer (ram_defer False): after the RAM
    wait it starts in spill mode instead of being carried on every sync."""
    from tests.ops.test_sync_run import FakeScan as FS

    C.write_carry([{"job": "prices_finalize", "pass_no": 1, "reason": "RAM floor: ...", "deferred_at": S.now_iso()}],
                  "S0", tmp_data)
    runner = _DeferJobs(tmp_data, defer={"prices_finalize"})
    sr = SyncRun(tmp_data, trigger="interval", registry=REG, runner=runner,
                 scan_fn=FS(_prices_plan(), {"prices.US": ["US"]}), publish=False, passes=1,
                 pressure_fn=lambda: None, sleep_s=0.02)
    out = sr.run()
    assert runner.ram_defer["prices_finalize"] is False and runner.ram_defer["prices_us_polygon"] is True
    assert out["jobs"]["prices_finalize"]["status"] == "success" and sr.nodes[(1, "prices_finalize")].carried == 1
    left = {e["job"] for e in json.loads(C.carry_path(tmp_data).read_text(encoding="utf-8"))["jobs"]}
    assert "prices_finalize" not in left


def test_kill_switch_restores_previous_behaviour(tmp_data, monkeypatch):
    monkeypatch.setenv("BIG360_SYNC_CAPS", "0")
    monkeypatch.setenv("BIG360_SYNC_PASS_MAX_JOBS", "1")

    class Plain(FakeRunner):
        def run_jobs(self, ids, opts):
            assert not opts.ram_defer and opts.start_by is None
            return super().run_jobs(ids, opts)

    net = ["fx_rates", "macro_eu", "cftc_cot"]
    sr, runner = _sync(tmp_data, _plan(net), runner=Plain(tmp_data))
    out = sr.run()
    assert sorted(c[0] for c in runner.calls) == sorted(net) and out["deferred"] == []
    assert not C.carry_path(tmp_data).exists()


# ------------------------------------------------------------------------------------------------ lock


def test_lock_owner_and_stale_status(tmp_data):
    seen = {}

    class Peek(FakeRunner):
        def run_jobs(self, ids, opts):
            seen["owner"] = S.read_owner(tmp_data)
            seen["lock"] = S.lock_status(tmp_data)
            return super().run_jobs(ids, opts)

    sr, _ = _sync(tmp_data, _plan(["fx_rates"]), runner=Peek(tmp_data))
    sr.run()
    assert seen["owner"]["run_id"] == sr.run_id and seen["lock"]["held"] and not seen["lock"]["stale"]
    assert S.read_owner(tmp_data) == {}                                       # cleared at the end
    now = datetime.now(UTC)
    S.write_status({"run_id": "S1", "state": "running", "heartbeat_at": (now - timedelta(minutes=20)).isoformat(),
                    "started_at": (now - timedelta(hours=1)).isoformat()}, tmp_data)
    S.write_owner("S1", "interval", tmp_data)
    ls = S.lock_status(tmp_data, stale_heartbeat_s=900, held=True)
    assert ls["stale"] and "no heartbeat for 20 min" in ls["why"]
    S.write_status({"run_id": "S1", "state": "running", "heartbeat_at": now.isoformat()}, tmp_data)
    owner = S.read_owner(tmp_data)
    owner["started_at"] = (now - timedelta(hours=9)).isoformat()
    S.owner_path(tmp_data).write_text(json.dumps(owner), encoding="utf-8")
    ls = S.lock_status(tmp_data, hard_max_h=8, held=True)
    assert ls["stale"] and "hard maximum 8 h" in ls["why"]
    assert not S.lock_status(tmp_data, held=False)["stale"]                   # nobody holds it: nothing to recover


def test_trigger_recovers_a_hung_sync(tmp_data, monkeypatch):
    from app.ops import winsys
    from tests.ops.test_sync_trigger import _trigger

    killed = []
    monkeypatch.setattr(winsys, "kill_tree", lambda pid, procs=None: killed.append(pid) or [pid])
    t, clock, procs = _trigger(tmp_data, stale_confirm_s=60, stale_heartbeat_s=900)
    t.tick(api_ready=True)
    clock.t += 91
    assert t.tick() == "startup"
    now = datetime.now(UTC)
    S.write_status({"run_id": "S9", "state": "running", "heartbeat_at": (now - timedelta(hours=2)).isoformat()},
                   tmp_data)
    S.write_owner("S9", "startup", tmp_data)
    t.tick()                                                                   # stale seen: confirming
    assert not S.stop_path(tmp_data).exists()
    clock.t += 61
    t.tick()
    assert S.stop_path(tmp_data).exists() and "stop requested" in t.detail     # graceful first
    clock.t += 2                                                               # stop_grace_s = 1 in _trigger
    t.tick()
    assert killed == [procs[0].pid] and t.stale_kills == 1 and t.status()["last_stale"]["run_id"] == "S9"
    # a heartbeat that comes back (e.g. a resumed laptop) resets the confirmation
    S.write_status({"run_id": "S9", "state": "running", "heartbeat_at": datetime.now(UTC).isoformat()}, tmp_data)
    t.tick()
    assert t._stale_since is None
