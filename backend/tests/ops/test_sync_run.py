"""Startup sync A2 / A6: executor with fake sources (a fake JobRunner and a fake scan).

Reason paths after a run: not published yet (fetch ok, period still missing -> back-off, parked after 6), key missing,
source down (network error -> source_unavailable), failed (other error, parked after 3 with an alert), partial (one
dataset fails, the rest is published, nothing aborts). Lanes: network fetchers in parallel, heavy steps one at a time.
No overlap: a held sync lock or a running refresh DAG skips the run; a waiting refresh makes it yield."""

from __future__ import annotations

import threading
import time
from datetime import UTC, date, datetime

import pytest
from filelock import FileLock

from app.ops.sync import policy as P
from app.ops.sync import state as S
from app.ops.sync.run import SyncRun
from app.ops.sync.scan import DatasetSync, EntitySync, PlanItem, ScanResult
from app.scheduler.runner import JobOutcome, JobRegistry, RunReport


class FakeRunner:
    """Outcome per job from `script` {job: (status, message, error_type)}; records concurrency per lane."""

    def __init__(self, settings, script=None, sleep=0.05):
        from app.scheduler.runner import JobRunStore

        self.store = JobRunStore(settings)
        self.script = dict(script or {})
        self.sleep = sleep
        self.calls: list[tuple[str, dict]] = []
        self.active: set[str] = set()
        self.max_active: dict[str, int] = {}
        self.lock = threading.Lock()

    def run_jobs(self, ids, opts):
        from app.ops.sync import graph as G

        j = ids[0]
        lane = G.lane(REG.jobs[j])
        with self.lock:
            self.calls.append((j, dict(opts.params)))
            self.active.add(j)
            n = sum(1 for a in self.active if G.lane(REG.jobs[a]) == lane)
            self.max_active[lane] = max(self.max_active.get(lane, 0), n)
        time.sleep(self.sleep)
        sc = self.script.get(j, ("success", "ok", None))
        if isinstance(sc, list):   # successive outcomes
            sc = sc.pop(0) if len(sc) > 1 else sc[0]
        st, msg, et = sc
        with self.lock:
            self.active.discard(j)
        o = JobOutcome(j, st, "job_skipped" if st == "skipped" else None, ["R1"], 1, self.sleep, 5, et, msg)
        return RunReport(j, None, opts.as_of or date.today(), {j: o}, datetime.now(UTC))


REG = JobRegistry.load()


def item(job, ds, ent, period="2026-10-27", trigger="dataset", params=None):
    return PlanItem(job, dict(params or {}), [ds], [f"{ds} due"], "core", trigger, {f"{ds}|{ent}": [period]})


class FakeScan:
    """First call: the plan; later calls: `still` decides which (dataset, entity) are still due after the fetch."""

    def __init__(self, plan, datasets, still=()):
        self.plan, self.datasets, self.still, self.n = plan, datasets, set(still), 0

    def __call__(self, settings, datasets=None, ledger=None):
        self.n += 1
        out = []
        for did, ents in self.datasets.items():
            es = []
            for e in ents:
                due = self.n == 1 or (did, e) in self.still
                held = ledger.active(did, e, "2026-10-27") if ledger is not None and self.n > 1 else None
                es.append(EntitySync(e, (held or {}).get("reason") or ("due" if due else "fresh"),
                                     retry_at=(held or {}).get("retry_at"), attempts=(held or {}).get("attempts")))
            out.append(DatasetSync(did, did, "x", entities=es).finish())
        return ScanResult(datetime.now(UTC).isoformat(), "2026-10-27", out, self.plan if self.n == 1 else {},
                          {"US": "2026-10-27"}, 0.01)


def _run(s, plan, datasets, still=(), script=None, passes=1, **kw):
    runner = FakeRunner(s, script)
    sr = SyncRun(s, trigger="manual", registry=REG, runner=runner, scan_fn=FakeScan(plan, datasets, still),
                 publish=False, passes=passes, pressure_fn=lambda: None, sleep_s=0.02, **kw)
    return sr, runner, sr.run()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("BIG360_HEAVY_SLOT", "0")


def test_reason_paths_after_a_run(tmp_data):
    plan = {"prices_us_polygon": item("prices_us_polygon", "prices.US", "US"),
            "fx_rates": item("fx_rates", "fx.rates", "fixings"),
            "cftc_cot": item("cftc_cot", "cftc.cot", "legacy_fut"),
            "prices_kr_krx": item("prices_kr_krx", "prices.KR", "KR"),
            "sec_filings_index": item("sec_filings_index", "filings.sec", "*")}
    ds = {"prices.US": ["US"], "fx.rates": ["fixings"], "cftc.cot": ["legacy_fut"], "prices.KR": ["KR"],
          "filings.sec": ["*"]}
    script = {"fx_rates": ("failed", "ConnectionError: connection reset by peer", "ConnectionError"),
              "cftc_cot": ("failed", "KeyError: 'report_date'", "KeyError"),
              "prices_kr_krx": ("skipped", "KRX_OPENAPI_KEY missing", None)}
    sr, runner, out = _run(tmp_data, plan, ds, still={("prices.US", "US"), ("fx.rates", "fixings"),
                                                      ("cftc.cot", "legacy_fut"), ("prices.KR", "KR")}, script=script)
    led = S.Ledger(tmp_data)
    assert led.get("prices.US", "US", "2026-10-27")["reason"] == "not_yet_published"   # fetched, still missing
    assert led.get("fx.rates", "fixings", "2026-10-27")["reason"] == "source_unavailable"
    assert led.get("cftc.cot", "legacy_fut", "2026-10-27")["reason"] == "failed"
    kr = led.get("prices.KR", "KR", "2026-10-27")
    assert kr["reason"] == "needs_key" and kr["key"] == "KRX_OPENAPI_KEY"
    assert led.get("filings.sec", "*", "2026-10-27") is None                            # brought: cleared
    assert out["result"] == "partial" and set(out["failed_jobs"]) == {"fx_rates", "cftc_cot"}
    final = {d["dataset"]: d["reason"] for d in S.read_status(tmp_data)["datasets"]}
    assert final == {"prices.US": "not_yet_published", "fx.rates": "source_unavailable", "cftc.cot": "failed",
                     "prices.KR": "needs_key", "filings.sec": "fresh"}
    st = S.read_status(tmp_data)
    assert st["state"] == "idle" and st["progress"]["skipped"] == 4
    assert S.read_history(tmp_data)[0]["run_id"] == sr.run_id


def test_failures_are_parked_after_three_attempts_with_an_alert(tmp_data):
    plan = {"cftc_cot": item("cftc_cot", "cftc.cot", "legacy_fut")}
    script = {"cftc_cot": ("failed", "ValueError: bad payload", "ValueError")}
    for k in range(3):
        led = S.Ledger(tmp_data)
        if k:   # make the previous retry due again
            rec = led.items["cftc.cot|legacy_fut|2026-10-27"]
            rec["retry_at"] = "2000-01-01T00:00:00+00:00"
            led.save()
        sr, _, out = _run(tmp_data, plan, {"cftc.cot": ["legacy_fut"]}, still={("cftc.cot", "legacy_fut")},
                          script=script)
    rec = S.Ledger(tmp_data).get("cftc.cot", "legacy_fut", "2026-10-27")
    assert rec["attempts"] == 3 and "parked" in rec["error"]
    assert (S.parse_ts(rec["retry_at"]) - S.parse_ts(rec["last_at"])).total_seconds() == P.PARK_HOURS * 3600
    assert any("ALERT" in n and "parked" in n for n in out["notes"])


def test_not_published_back_off_grows(tmp_data):
    plan = {"finra_short_interest": item("finra_short_interest", "short_interest", "*")}
    waits = []
    for k in range(3):
        led = S.Ledger(tmp_data)
        if k:
            led.items["short_interest|*|2026-10-27"]["retry_at"] = "2000-01-01T00:00:00+00:00"
            led.save()
        _run(tmp_data, plan, {"short_interest": ["*"]}, still={("short_interest", "*")})
        rec = S.Ledger(tmp_data).get("short_interest", "*", "2026-10-27")
        waits.append((S.parse_ts(rec["retry_at"]) - S.parse_ts(rec["last_at"])).total_seconds() / 60)
    assert waits == [float(m) for m in P.RETRY_NOT_PUBLISHED_MIN[:3]]


def test_network_parallel_heavy_sequential(tmp_data):
    net = ["fx_rates", "macro_eu", "macro_intl", "cftc_cot", "sec_filings_index"]
    heavy = ["prices_finalize", "macro_cells"]
    plan = {j: item(j, f"d.{j}", "*") for j in net}
    plan |= {j: item(j, f"d.{j}", "*", trigger="upstream") for j in heavy}
    sr, runner, out = _run(tmp_data, plan, {f"d.{j}": ["*"] for j in net + heavy}, script=None)
    ran = [c[0] for c in runner.calls]
    assert set(net + heavy) <= set(ran)
    assert runner.max_active.get("network", 0) >= 3       # fetchers overlap
    assert runner.max_active.get("heavy", 0) == 1         # finalise / heavy derive one at a time
    assert ran.index("prices_finalize") > min(ran.index(j) for j in net)
    # derive jobs whose inputs did not change are skipped with the fingerprint reason, never failed
    skipped = [j for j, v in out["jobs"].items() if v["status"] == "skipped"]
    assert all(out["jobs"][j]["reason"] for j in skipped)


def test_no_overlap_lock_and_refresh(tmp_data):
    lk = S.sync_lock(tmp_data)
    lk.acquire()
    try:
        _, runner, out = _run(tmp_data, {}, {})
        assert out == {"status": "skipped", "reason": "already running"} and runner.calls == []
    finally:
        lk.release()
    tmp_data.locks_dir.mkdir(parents=True, exist_ok=True)
    dl = FileLock(str(tmp_data.locks_dir / "dag_weekly.lock"), timeout=0)
    dl.acquire()
    try:
        _, runner, out = _run(tmp_data, {"fx_rates": item("fx_rates", "fx.rates", "fixings")}, {"fx.rates": ["fixings"]})
        assert out["status"] == "skipped" and "weekly" in out["reason"] and runner.calls == []
    finally:
        dl.release()


def test_yield_to_a_waiting_refresh(tmp_data):
    plan = {j: item(j, f"d.{j}", "*") for j in ("fx_rates", "macro_eu")}
    S.yield_path(tmp_data).parent.mkdir(parents=True, exist_ok=True)

    class YieldingScan(FakeScan):
        def __call__(self, settings, datasets=None, ledger=None):
            if self.n == 0:   # the refresh starts waiting while the sync scans
                S.yield_path(tmp_data).write_text("refresh waiting", encoding="utf-8")
            return super().__call__(settings, datasets, ledger)

    runner = FakeRunner(tmp_data)
    sr = SyncRun(tmp_data, registry=REG, runner=runner, scan_fn=YieldingScan(plan, {f"d.{j}": ["*"] for j in plan}),
                 publish=False, passes=2, pressure_fn=lambda: None, sleep_s=0.02)
    t = threading.Thread(target=sr.run)
    t.start()
    t.join(30)
    st = S.read_status(tmp_data)
    assert st["result"] in ("yielded", "partial") and runner.calls == []
    assert all(j["reason"] == "yielded" for j in st["jobs"] if j["status"] == "skipped")


def test_interrupted_status_when_the_lock_is_free(tmp_data):
    S.write_status({"run_id": "S0", "state": "running", "heartbeat_at": "2026-10-27T10:00:00+00:00"}, tmp_data)
    st = S.read_status(tmp_data)
    assert st["state"] == "interrupted" and "resumes" in st["detail"]


def test_heavy_jobs_wait_while_the_machine_is_under_pressure(tmp_data):
    plan = {"prices_finalize": item("prices_finalize", "prices.US", "US", trigger="upstream")}
    calls = {"n": 0}

    def pressure():
        calls["n"] += 1
        return "free RAM 1.0 GB < 3 GB" if calls["n"] < 4 else None

    runner = FakeRunner(tmp_data)
    sr = SyncRun(tmp_data, registry=REG, runner=runner, scan_fn=FakeScan(plan, {"prices.US": ["US"]}), publish=False,
                 passes=1, pressure_fn=pressure, sleep_s=0.02)
    sr.run()
    assert calls["n"] >= 4 and [c[0] for c in runner.calls] == ["prices_finalize"]


def test_file_race_is_retried_once_alone(tmp_data):
    plan = {"sec_xbrl_facts": item("sec_xbrl_facts", "d.x", "*", trigger="dataset"),
            "cn_fundamentals": item("cn_fundamentals", "d.c", "*")}
    race = ("failed", "O sistema nao conseguiu localizar (os error 2): .../fundamentals_raw/x.parquet", "FileNotFoundError")
    _, runner, out = _run(tmp_data, plan, {"d.x": ["*"], "d.c": ["*"]},
                          script={"sec_xbrl_facts": [race, ("success", "ok", None)]}, passes=2)   # an extended fetch: pass 2
    assert [c[0] for c in runner.calls].count("sec_xbrl_facts") == 2
    assert out["jobs"]["sec_xbrl_facts#2"]["status"] == "success" and not out["failed_jobs"]
    assert any("file race" in n for n in out["notes"])


def test_news_only_changes_are_not_republished_every_sync(tmp_data, monkeypatch):
    from datetime import timedelta

    from app.ops.sync import publish as PB

    monkeypatch.setattr(PB, "default_as_of", lambda s=None: date(2026, 10, 29))
    monkeypatch.setattr(PB, "plan_publish", lambda s=None, a=None: {
        "mode": "incremental", "as_of": "2026-10-29", "dirty": {"events_v2": "news_stories changed"}, "est_s": 200,
        "full_s": 630, "snapshot": "R1"})
    monkeypatch.setattr(PB, "last_published_at", lambda s=None: datetime.now(UTC) - timedelta(hours=1))
    runner = FakeRunner(tmp_data)
    sr = SyncRun(tmp_data, registry=REG, runner=runner, scan_fn=FakeScan({}, {}), pressure_fn=lambda: None)
    out = sr._publish(1, [])
    assert out["mode"] == "none" and "events_v2" in out["detail"] and runner.calls == []
    monkeypatch.setattr(PB, "last_published_at", lambda s=None: datetime.now(UTC) - timedelta(hours=7))
    out = sr._publish(1, [])
    assert out["mode"] == "incremental" and [c[0] for c in runner.calls] == ["serving_sections_publish"]
    assert runner.calls[0][1]["sections"] == ["events_v2"]
