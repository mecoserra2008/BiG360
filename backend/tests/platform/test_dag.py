"""DAG resolution: registry parsing, validation, cycles, waves, critical path, cron translation, run keys."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.scheduler.runner import (
    DagCycleError,
    JobRegistry,
    _expand_cron_dow,
    check_available,
    cron_trigger,
    format_plan,
    make_run_key,
    previous_business_day,
)
from tests.platform.conftest import FAKE, make_registry


def test_real_jobs_yaml_loads_and_validates():
    reg = JobRegistry.load()
    assert reg.validate() == []
    nightly = reg.dag_jobs("nightly")
    order = reg.topo_order(nightly)
    pos = {j: i for i, j in enumerate(order)}
    for j in nightly:
        for d in reg.jobs[j].depends_on:
            if d in pos:
                assert pos[d] < pos[j], f"{d} must precede {j}"
    assert order[-1] in ("thesis_eval", "qa_regression") and pos["alerts_eval"] < pos["thesis_eval"]  # thesis_eval (item #8) runs after alerts
    assert pos["alerts_eval"] < pos["qa_regression"]  # O2.3 regression guard after the published snapshot
    # platform modules exist
    assert check_available(reg.jobs["dq_checks"])[0]
    assert check_available(reg.jobs["alerts_eval"])[0]


def test_cycle_detected_with_path():
    with pytest.raises(ValueError, match="cycle"):
        make_registry([
            {"id": "a", "module": f"{FAKE}:ok", "depends_on": ["c"]},
            {"id": "b", "module": f"{FAKE}:ok", "depends_on": ["a"]},
            {"id": "c", "module": f"{FAKE}:ok", "depends_on": ["b"]},
        ])
    reg = make_registry([{"id": "a", "module": f"{FAKE}:ok"}])
    reg.jobs["a"] = reg.jobs["a"].__class__(**{**reg.jobs["a"].__dict__, "depends_on": ("a",)})
    with pytest.raises(DagCycleError):
        reg.topo_order(["a"])


def test_unknown_dependency_and_bad_executor_rejected():
    with pytest.raises(ValueError, match="unknown dependency"):
        make_registry([{"id": "a", "module": f"{FAKE}:ok", "depends_on": ["nope"]}])
    with pytest.raises(ValueError, match="executor"):
        make_registry([{"id": "a", "module": f"{FAKE}:ok", "executor": "gpu"}])


def test_waves_critical_path_and_downstream():
    reg = make_registry([
        {"id": "a", "module": f"{FAKE}:ok", "est_min_mvp": 1},
        {"id": "b", "module": f"{FAKE}:ok", "est_min_mvp": 10, "depends_on": ["a"]},
        {"id": "c", "module": f"{FAKE}:ok", "est_min_mvp": 2, "depends_on": ["a"]},
        {"id": "d", "module": f"{FAKE}:ok", "est_min_mvp": 1, "depends_on": ["b", "c"]},
        {"id": "e", "module": f"{FAKE}:ok", "est_min_mvp": 1},
    ])
    ids = list(reg.jobs)
    assert reg.waves(ids) == [["a", "e"], ["b", "c"], ["d"]]
    minutes, path = reg.critical_path(ids)
    assert path == ["a", "b", "d"] and minutes == 12
    assert reg.downstream("a") == ["b", "c", "d"]
    assert reg.upstream("d") == ["a", "b", "c"]
    rp = reg.remaining_path(ids)
    assert rp["a"] == 12 and rp["e"] == 1


def test_module_function_syntax_and_availability():
    reg = make_registry([
        {"id": "x", "module": f"{FAKE}:ok"},
        {"id": "y", "module": "app.does_not_exist.module"},
    ])
    assert reg.jobs["x"].module == FAKE and reg.jobs["x"].function == "ok"
    assert reg.jobs["y"].function == "run"
    assert check_available(reg.jobs["x"]) == (True, None)
    ok, why = check_available(reg.jobs["y"])
    assert not ok and "not found" in why


def test_plan_dry_run_output():
    reg = JobRegistry.load()
    plan = reg.plan("nightly")
    assert plan["n_jobs"] == len(reg.dag_jobs("nightly"))
    assert plan["critical_path"][-1] in ("thesis_eval", "qa_regression")
    txt = format_plan(plan)
    assert "critical path" in txt and "wave 0" in txt
    full = reg.plan("nightly", universe="full")
    assert full["critical_path_min"] > plan["critical_path_min"]


def test_cron_weekday_translation():
    # standard cron: 0 = Sunday. APScheduler 3 would read "2-6" as Wed-Sun without translation.
    assert _expand_cron_dow("2-6") == "tue,wed,thu,fri,sat"
    assert _expand_cron_dow("0") == "sun" and _expand_cron_dow("7") == "sun"
    assert _expand_cron_dow("*") == "*"
    assert _expand_cron_dow("1-5/2") == "mon,wed,fri"
    tz = ZoneInfo("Europe/Lisbon")
    trig = cron_trigger("30 1 * * 2-6", tz)
    nxt = trig.get_next_fire_time(None, datetime(2026, 10, 10, 12, 0, tzinfo=tz))  # Saturday noon
    assert nxt.date() == date(2026, 10, 13) and (nxt.hour, nxt.minute) == (1, 30)  # Tuesday 01:30
    first_sunday = cron_trigger("0 8 1-7 * 0", tz).get_next_fire_time(None, datetime(2026, 10, 10, tzinfo=tz))
    assert first_sunday.date() == date(2026, 11, 1) and first_sunday.weekday() == 6


def test_run_key_and_business_day():
    k1 = make_run_key("j", date(2026, 10, 9), {"b": 1, "a": [1, 2]})
    k2 = make_run_key("j", date(2026, 10, 9), {"a": [1, 2], "b": 1, "force": True})
    assert k1 == k2 and k1.startswith("j|2026-10-09|")
    assert make_run_key("j", date(2026, 10, 9), {"a": 1}) != k1
    assert previous_business_day(date(2026, 10, 12)) == date(2026, 10, 9)  # Monday -> Friday
    assert previous_business_day(date(2026, 10, 14)) == date(2026, 10, 13)
