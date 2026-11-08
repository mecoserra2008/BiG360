"""Gap filler: resource governor, rate budgets, back-off, task processes (graceful stop + tree kill), gap bookkeeping."""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.ops import governor as gov
from app.ops import tasks as T
from app.ops import winsys
from app.ops.budgets import RateBudgets
from app.ops.filler import Filler, backoff_delay


# ------------------------------------------------------------------------------------------------ governor


def ri(tid, prio=50, rss=1.0, pre=True, y=True):
    return gov.RunningInfo(tid, prio, rss, pre, y)


def test_governor_pauses_on_battery_metered_disk_and_low_ram():
    run = [ri("a"), ri("b")]
    for sm, why in ((gov.Sample(on_ac=False, free_ram_gb=10), "battery"), (gov.Sample(metered=True, free_ram_gb=10), "metered"),
                    (gov.Sample(disk_free_gb=5, free_ram_gb=10), "disk"), (gov.Sample(free_ram_gb=2.5), "free RAM"),
                    (gov.Sample(free_ram_gb=10, manual_pause=True), "user")):
        d = gov.decide(sm, run)
        assert d.state == "paused" and not d.allow_new and set(d.stop) == {"a", "b"}, why
        assert any(why in r for r in d.reasons)
    assert gov.decide(gov.Sample(on_ac=None, free_ram_gb=10), []).state == "running"   # unknown power = desktop
    assert gov.decide(gov.Sample(on_ac=False, free_ram_gb=10), [], {"pause_on_battery": False}).state == "running"


def test_governor_ram_hysteresis():
    sm = gov.Sample(free_ram_gb=3.2)
    assert gov.decide(sm, [], was_paused_for_ram=False).state == "running"
    assert gov.decide(sm, [], was_paused_for_ram=True).state == "paused"     # resume only above 3.5 GB
    assert gov.decide(gov.Sample(free_ram_gb=3.6), [], was_paused_for_ram=True).state == "running"


def test_governor_yields_to_refresh_dags_and_caps_ram():
    d = gov.decide(gov.Sample(free_ram_gb=10, dags_running=["nightly"]), [ri("gdelt", 70, 4.0, y=True), ri("small", 10, 0.2, y=False)])
    assert d.state == "yielding" and not d.allow_new and d.stop == ["gdelt"]
    d = gov.decide(gov.Sample(free_ram_gb=10), [ri("a", 70, 2.5)])
    assert d.allow_new and d.ram_budget_gb == pytest.approx(1.5)
    assert gov.admit(1.0, d, 1) and not gov.admit(2.0, d, 1)
    assert gov.admit(9.0, gov.decide(gov.Sample(free_ram_gb=10), []), 0)       # alone, a big campaign may run
    d = gov.decide(gov.Sample(free_ram_gb=10), [ri("a", 70, 4.2)])
    assert d.state == "capped" and not d.allow_new and not d.stop                # over cap: no new work
    d = gov.decide(gov.Sample(free_ram_gb=10), [ri("a", 70, 5.0), ri("b", 30, 1.5)])
    assert d.stop == ["a"]                                                        # over 1.5x cap: least urgent stops
    d = gov.decide(gov.Sample(free_ram_gb=10), [ri("a"), ri("b")])
    assert not d.allow_new and "max concurrency 2" in d.reasons


def test_preemption_only_for_much_more_urgent_work():
    run = [ri("gdelt", 70, 4.5), ri("x", 40, 0.5, pre=False)]
    assert gov.should_preempt(11, run) == "gdelt"
    assert gov.should_preempt(60, run) is None
    assert gov.should_preempt(11, [ri("x", 40, 0.5, pre=False)]) is None


# ------------------------------------------------------------------------------------------------ budgets


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_rate_budgets_sliding_hour(tmp_path):
    clk = Clock()
    b = RateBudgets({"polygon": {"budget": {"unit": "requests", "per_hour": 120}}, "free": {}}, tmp_path / "b.json", clock=clk)
    assert b.can_spend("polygon", 100)
    b.spend("polygon", 100)
    assert not b.can_spend("polygon", 30) and b.can_spend("polygon", 20)
    assert b.wait_s("polygon", 30) == pytest.approx(3600)
    clk.t += 1800
    b.spend("polygon", 20)
    clk.t += 1801                                       # the first spend left the window
    assert b.used("polygon") == 20 and b.can_spend("polygon", 100)
    assert b.can_spend("free", 1e9) and b.can_spend(None, 5)
    # an item bigger than the whole hourly budget runs only when nothing was spent in the window
    assert not b.can_spend("polygon", 500)
    clk.t += 3601
    assert b.can_spend("polygon", 500)
    b.spend("polygon", 7)
    b.save()
    b2 = RateBudgets({"polygon": {"budget": {"per_hour": 120}}}, tmp_path / "b.json", clock=clk)
    doc = json.loads((tmp_path / "b.json").read_text())
    assert doc["events"]["polygon"][-1][1] == 7
    snap = b.snapshot()["polygon"]
    assert snap["used_1h"] == 7 and snap["pct"] == pytest.approx(5.8, abs=0.1)
    assert b2.limit("polygon") == 120


def test_backoff_is_exponential_and_capped():
    assert backoff_delay(1) == timedelta(minutes=30)
    assert backoff_delay(3) == timedelta(minutes=120)
    assert backoff_delay(20) == timedelta(hours=24)


# ------------------------------------------------------------------------------------------------ task processes


def _spec(tmp_path, code: str, stop_file=None, tid="t:1"):
    return T.TaskSpec(tid, "test", "gap", 10, [sys.executable, "-c", code], stop_file=stop_file)


def test_task_graceful_stop_file(tmp_data, tmp_path):
    stop = tmp_path / "STOP"
    code = f"import time,pathlib\np=pathlib.Path(r'{stop}')\nwhile not p.exists(): time.sleep(0.05)\n"
    t = T.TaskProc(_spec(tmp_path, code, stop), tmp_data, priority="normal")
    t.start()
    try:
        assert t.poll() is None and winsys.pid_alive(t.pid)
        t.request_stop()
        assert stop.exists() and t.created_stop_file
        deadline = time.time() + 90
        while t.poll() is None and time.time() < deadline:
            time.sleep(0.1)
        assert t.poll() == 0
        t.enforce_stop()
        assert not stop.exists()                       # the filler removes the STOP file it created
    finally:
        if t.poll() is None:
            winsys.kill_tree(t.pid)


def test_task_tree_is_killed_after_grace(tmp_data, tmp_path, monkeypatch):
    # parent ignores CTRL+BREAK and spawns a child: both must be gone after the grace period (no orphans, F18 / R4)
    code = ("import signal,subprocess,sys,time\n"
            "signal.signal(getattr(signal,'SIGBREAK',signal.SIGINT), signal.SIG_IGN)\n"
            "c=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(120)\n")
    monkeypatch.setitem(T.GRACE_S, "default", 0.5)
    t = T.TaskProc(_spec(tmp_path, code, tid="t:kill"), tmp_data)
    t.start()
    child = None
    deadline = time.time() + 20
    while time.time() < deadline and child is None:
        txt = t.tail()
        lines = [x for x in txt.splitlines() if x.strip().isdigit()]
        child = int(lines[-1]) if lines else None
        time.sleep(0.1)
    assert child and winsys.pid_alive(child)
    t.request_stop()
    time.sleep(1.0)
    assert t.enforce_stop()
    time.sleep(0.5)
    assert t.poll() is not None and not winsys.pid_alive(child)


# ------------------------------------------------------------------------------------------------ queue + bookkeeping


def _gap_rows(rows):
    from app.core.schema import get_contract

    c = get_contract("data_gaps")
    now = datetime(2026, 10, 20, tzinfo=UTC)
    base = {"gap_group": "prices", "expected": 100, "actual": 0, "priority": 11, "fill_params": None, "source": "yfinance",
            "cost_units": None, "note": "n", "origin": "detector", "first_seen": now, "last_seen": now, "attempts": 0,
            "last_attempt_at": None, "next_attempt_at": None, "last_error": None, "resolved_at": None,
            "detector_run_id": None, "updated_at": now}
    return pl.DataFrame([{**base, **r} for r in rows], schema=c.polars_schema())


def test_gap_tasks_group_dates_per_region_and_respect_backoff(tmp_data):
    from app.ops.coverage import load_coverage

    reg = load_coverage(tmp_data)
    later = datetime(2030, 1, 1, tzinfo=UTC)
    df = _gap_rows([
        {"gap_id": "a", "dataset": "prices.EU", "entity": "EU", "gap_class": "missing", "status": "open", "fill_kind": "yf_refetch",
         "period_start": date(2026, 10, 12), "period_end": date(2026, 10, 12)},
        {"gap_id": "b", "dataset": "prices.EU", "entity": "EU", "gap_class": "partial", "status": "failed", "fill_kind": "yf_refetch",
         "period_start": date(2026, 10, 7), "period_end": date(2026, 10, 7)},
        {"gap_id": "c", "dataset": "prices.KR", "entity": "KR", "gap_class": "missing", "status": "open", "fill_kind": "yf_refetch",
         "period_start": date(2026, 10, 12), "period_end": date(2026, 10, 12), "next_attempt_at": later},
        {"gap_id": "d", "dataset": "gdelt.days", "entity": "2026-10-15", "gap_class": "partial", "status": "open",
         "fill_kind": "gdelt_redo", "priority": 30, "fill_params": json.dumps({"day": "2026-10-15"}),
         "period_start": date(2026, 10, 15), "period_end": date(2026, 10, 15)},
        {"gap_id": "e", "dataset": "fx.rates", "entity": "TWD", "gap_class": "stale", "status": "manual", "fill_kind": "job"},
    ])
    ts = T.gap_tasks(df, reg, tmp_data, now=datetime(2026, 10, 20, 12, tzinfo=UTC))
    ids = [t.task_id for t in ts]
    assert ids == ["gap:prices.EU:EU", "gap:gdelt.days:2026-10-15"]           # KR backs off, manual never runs
    eu = ts[0]
    spec = json.loads(eu.spec_path.read_text())
    assert spec["kind"] == "yf_refetch" and spec["dates"] == ["2026-10-07", "2026-10-12"] and spec["detect_after"] == ["prices.EU"]
    assert set(eu.gap_ids) == {"a", "b"} and eu.source == "yfinance"
    g = ts[1]
    assert g.lock == "events_v2_backfill" and "--redo" in g.command and g.command[g.command.index("--start") + 1] == "2026-10-15"
    assert T.gap_tasks(df, reg, tmp_data, now=datetime(2026, 10, 20, 12, tzinfo=UTC), skip_ids={"gap:prices.EU:EU"})[0].task_id == "gap:gdelt.days:2026-10-15"


class FakeProc:
    def __init__(self, spec):
        self.spec = spec
        self.adopted = False
        self.stop_requested_at = None
        self.started_at = datetime.now(UTC)

    def result(self):
        return {"status": "failed", "message": "HTTP 500 from source"}

    def tail(self, n=2000):
        return ""

    def cleanup(self):
        pass


def test_attempts_backoff_and_unfillable_after_max(tmp_data):
    from app.core.storage import write_table
    from app.ops.gaps import read_gaps

    write_table(_gap_rows([{"gap_id": "a", "dataset": "prices.EU", "entity": "EU", "gap_class": "missing", "status": "filling",
                            "fill_kind": "yf_refetch", "period_start": date(2026, 10, 12), "period_end": date(2026, 10, 12)}]),
                "data_gaps", mode="overwrite", settings=tmp_data)
    f = Filler(tmp_data)
    spec = T.TaskSpec("gap:prices.EU:EU", "x", "gap", 11, ["x"], gap_ids=["a"], dataset="prices.EU")
    for i in range(1, 7):
        f._finish(FakeProc(spec), 1)
        r = read_gaps(tmp_data).row(0, named=True)
        assert r["attempts"] == i and r["last_error"] == "HTTP 500 from source"
        if i < 6:
            assert r["status"] == "failed" and r["next_attempt_at"] > datetime.now(UTC)
    assert r["status"] == "unfillable" and r["gap_class"] == "unfillable" and "gave up" in r["note"]
    assert f.state["recent"][0]["outcome"] == "failed"


def test_stopped_or_blocked_tasks_do_not_count_as_attempts(tmp_data):
    from app.core.storage import write_table
    from app.ops.gaps import read_gaps

    write_table(_gap_rows([{"gap_id": "a", "dataset": "gdelt.days", "entity": "2025-06-20", "gap_class": "missing", "status": "filling",
                            "fill_kind": "gdelt_redo", "fill_params": json.dumps({"day": "2025-06-20", "outage": True})}]),
                "data_gaps", mode="overwrite", settings=tmp_data)
    f = Filler(tmp_data)
    spec = T.TaskSpec("gap:gdelt.days:2025-06-20", "x", "gap", 30, ["x"], gap_ids=["a"], dataset="gdelt.days", lock="events_v2_backfill")
    p = FakeProc(spec)
    p.stop_requested_at = 1.0
    f._finish(p, 1)
    r = read_gaps(tmp_data).row(0, named=True)
    assert r["attempts"] == 0 and r["status"] == "open"
    f._finish(FakeProc(spec), 2)                      # backfill lock held elsewhere -> blocked
    assert read_gaps(tmp_data).row(0, named=True)["attempts"] == 0
    for _ in range(3):                                # successful re-runs that never fill an outage day
        q = FakeProc(spec)
        q.result = lambda: {"status": "ok"}
        f._finish(q, 0)
    r = read_gaps(tmp_data).row(0, named=True)
    assert r["status"] == "unfillable" and "outage" in r["note"]


def test_deferred_tasks_back_off_park_and_alert_without_counting_attempts(tmp_data):
    """Coverage v3 A2 review 2026-11-07: a task whose step the runner deferred (RAM floor / heavy slot; taskrun result
    status deferred) was `blocked`: +30 min, no attempt, forever - a silent 30-min loop. Now deferrals back off (30, 60, 120, 240,
    360 min), the 6th in a row parks the task 24 h with an alert, and a real attempt resets the count."""
    from app.core.storage import write_table
    from app.ops.filler import DEFER_PARK_AFTER, EXIT_DEFERRED
    from app.ops.gaps import read_gaps
    from app.ops.health import ops_alerts

    write_table(_gap_rows([{"gap_id": "a", "dataset": "prices.US", "entity": "US", "gap_class": "missing",
                            "status": "filling", "fill_kind": "polygon_dates", "period_start": date(2026, 11, 5),
                            "period_end": date(2026, 11, 5)}]), "data_gaps", mode="overwrite", settings=tmp_data)
    f = Filler(tmp_data)
    spec = T.TaskSpec("gap:prices.US:US", "Fill US prices", "gap", 11, ["x"], gap_ids=["a"], dataset="prices.US")
    msg = "deferred: RAM floor: free 7.9 GB - reserved 0.5 GB - budget 10.0 GB = -2.6 GB < floor 3 GB"
    waits = []
    for i in range(1, DEFER_PARK_AFTER + 1):
        p = FakeProc(spec)
        p.result = lambda: {"status": "deferred", "message": msg}
        t0 = datetime.now(UTC)
        f._finish(p, EXIT_DEFERRED)
        r = read_gaps(tmp_data).row(0, named=True)
        assert r["attempts"] == 0 and r["status"] == "open" and r["last_error"] is None
        waits.append(round((r["next_attempt_at"] - t0).total_seconds() / 60))
        d = f.state["deferrals"]["gap:prices.US:US"]
        assert d["n"] == i and d["reason"] == msg and d["parked"] == (i >= DEFER_PARK_AFTER)
    assert waits == [30, 60, 120, 240, 360, 24 * 60]
    assert "parked after repeated deferrals" in r["note"] and f.state["recent"][0]["outcome"] == "deferred"
    filler = {"heartbeat_at": datetime.now(UTC).isoformat(), "governor": {"state": "running", "reasons": []},
              "deferrals": f.state["deferrals"]}
    sch = {"enabled": True, "worker_alive": True, "profile": "weekly", "next_runs": {}, "last_success": {}}
    a = next(a for a in ops_alerts(sch, {"datasets": {}}, filler, [], None) if a["id"] == "filler_deferred")
    assert a["severity"] == "warn" and "Fill US prices: deferred 6 times" in a["detail"]
    f._finish(FakeProc(spec), 1)                       # a real (failed) attempt: counted, the deferral count resets
    assert read_gaps(tmp_data).row(0, named=True)["attempts"] == 1 and "gap:prices.US:US" not in f.state["deferrals"]
    q = FakeProc(spec)                                 # exit 3 without taskrun's "deferred" result: blocked as before
    q.result = lambda: {"status": "blocked", "message": "another run of this job is in progress"}
    f._finish(q, EXIT_DEFERRED)
    assert f.state["recent"][0]["outcome"] == "blocked" and "gap:prices.US:US" not in f.state["deferrals"]
    assert read_gaps(tmp_data).row(0, named=True)["attempts"] == 1
    # campaigns: the same back-off, the count is visible in the campaign state
    cs = T.TaskSpec("campaign:earnings_dates", "Earnings dates", "campaign", 60, ["x"], campaign="earnings_dates")
    c = FakeProc(cs)
    c.result = lambda: {"status": "deferred", "message": msg}
    f._finish(c, EXIT_DEFERRED)
    cst = f.state["campaigns"]["earnings_dates"]
    assert cst["deferrals"] == 1 and cst["last_outcome"] == "deferred" and not cst.get("attempts")
    assert datetime.fromisoformat(cst["next_attempt_at"]) > datetime.now(UTC) + timedelta(minutes=29)


def test_campaign_probe_gdelt_range_eta(tmp_data):
    days = {(date(2026, 7, 13) - timedelta(days=i)).isoformat(): {"files_ok": 576} for i in range(100)}
    st = {"days": days, "backfill_runs": {"e6": {"status": "running", "pid": 1, "batches": [{"days_per_hour": 20.0}, {"days_per_hour": 22.0}]}}}
    (tmp_data.state_dir / "events_v2.json").write_text(json.dumps(st), encoding="utf-8")
    from app.ops.coverage import load_coverage

    c = load_coverage(tmp_data)["campaigns"]["gdelt_e6"]
    pr = T.probe(c, tmp_data)
    total = (date(2026, 7, 13) - date(2015, 2, 19)).days + 1
    assert pr["done_units"] == 100 and pr["total_units"] == total and not pr["done"]
    assert pr["rate_per_h"] == 21.0 and pr["eta_h"] == pytest.approx((total - 100) / 21.0, abs=0.1)
    ts = T.campaign_tasks(load_coverage(tmp_data), {}, pl.DataFrame(), tmp_data)
    e6 = next(t for t in ts if t.campaign == "gdelt_e6")
    assert e6.command[1:4] == ["-m", "app.events_v2.backfill", "--label"] and e6.stop_file.name == "STOP"
    assert e6.lock == "events_v2_backfill" and e6.priority == 70
