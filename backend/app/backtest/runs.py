"""Backtesting lab run lifecycle (owner: backtest; pattern of app.modeling.engine).

    code, status = submit(spec, mode="auto", strategy_id=None, prior=None)   # 200 finished (sync) | 202 queued (async)

* Every run lives in data/backtest/runs/<run_id>/: spec.json, status.json (lifecycle, source of truth while in flight),
  result.json (envelope: result + reproducibility block), holdings.parquet, trades.parquet, CANCEL (cancel flag).
* Sync path: the API executes inline when the estimate is below SYNC_BUDGET_S (cached panel, small robustness).
* Async path: status queued + the generic `backtest_run` job (contracts/jobs.yaml, manual DAG, process executor) on
  the file-queue worker; the job runs the requested run, then drains other queued runs.
* The worker never opens app_state: the API mirrors status.json into backtest_runs (app.backtest.store), except the
  weekly `backtest_scheduled` job which, like studio_scheduled, writes app_state through short retried transactions.
"""

from __future__ import annotations

import json
import os
import platform
import secrets
import time
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from . import _engine, _panel
from .runner import backtest_dir, cache_dir, compute, run_dict, snapshot_id
from .spec import BacktestSpec, make_plan, plan_key

log = get_logger("app.backtest.runs")

SYNC_BUDGET_S = 4.0
TERMINAL = ("completed", "failed", "cancelled")
STALE_RUNNING_S = 240
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
UNIVERSE_SIZE_GUESS = {"mvp_us": 510, "mvp_global": 280, "full": 3000, "eu_broad": 1200}


class RunCancelled(RuntimeError):
    pass


class RunTimeout(RuntimeError):
    pass


def new_run_id() -> str:
    return "BR" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "".join(secrets.choice(_B32) for _ in range(5))


def run_dir(run_id: str, settings: Settings | None = None) -> Path:
    if not run_id or any(c in run_id for c in "/\\.:") or len(run_id) > 64:
        raise ValueError("bad run id")
    return backtest_dir(settings) / "runs" / run_id


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, obj: Any) -> None:
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(obj, default=str, ensure_ascii=False), encoding="utf-8")
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05 * (2 ** i))
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    for _ in range(5):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (PermissionError, ValueError):
            time.sleep(0.05)
    return None


def library_versions() -> dict[str, str]:
    out = {"python": platform.python_version(), "backtest_engine": _engine.ENGINE_VERSION,
           "backtest_panel": _panel.PANEL_VERSION}
    for m in ("numpy", "polars", "duckdb", "pydantic"):
        try:
            out[m] = __import__(m).__version__
        except Exception:  # pragma: no cover
            out[m] = "n/a"
    return out


# ------------------------------------------------------------------------------------------------ status files


def read_status(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        d = run_dir(run_id, settings)
    except ValueError:
        return None
    st = _read_json(d / "status.json")
    if st and st.get("status") == "running":
        hb = st.get("heartbeat_at") or st.get("started_at")
        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(hb)).total_seconds()
        except (TypeError, ValueError):
            age = 0
        if age > STALE_RUNNING_S + float(st.get("timeout_s") or 0):
            st.update(status="failed", error="run stopped without a result (worker died or was killed)",
                      error_code="stale", finished_at=_now())
            _write_json(d / "status.json", st)
    return st


def read_result(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        return _read_json(run_dir(run_id, settings) / "result.json")
    except ValueError:
        return None


def read_spec(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        return _read_json(run_dir(run_id, settings) / "spec.json")
    except ValueError:
        return None


def _update(run_id: str, settings: Settings, **fields: Any) -> dict[str, Any]:
    p = run_dir(run_id, settings) / "status.json"
    st = _read_json(p) or {"run_id": run_id}
    st.update(fields)
    _write_json(p, st)
    return st


# ------------------------------------------------------------------------------------------------ estimate


def estimate_seconds(spec: BacktestSpec, settings: Settings | None = None) -> float:
    """Static prior wall time (panel build when not cached + simulations). The estimate shown to the user and used
    for the sync decision is the learned one (`app.modeling.estimator.estimate_backtest`, S1.2)."""
    return prior_parts(spec, settings)["total_s"]


def prior_parts(spec: BacktestSpec, settings: Settings | None = None, cached: bool | None = None) -> dict[str, Any]:
    """Static prior pieces {total_s, panel_s, sim_s, n, cached, extras[]} (real universe sizes from the snapshot)."""
    from app.modeling.estimator import entity_count  # noqa: PLC0415

    s = settings or get_settings()
    if cached is None:
        try:
            plan, _ = make_plan(spec)
            cached = (cache_dir(s) / f"{plan_key(plan, snapshot_id(s))}.meta.json").exists()
        except Exception:  # noqa: BLE001
            cached = False
    u = spec.universe
    if u.kind == "assets":
        n = len(u.asset_ids) or 73
    else:
        n = len(u.security_ids) or entity_count(u, int(u.max_entities), s)
    end = spec.period.end or date.today()
    start = spec.period.start or date(end.year - 10, end.month, 1)
    years = max((end - start).days / 365.25, 0.5)
    per_year = {"D": 252, "W": 52, "M": 12}[spec.construction.rebalance]
    sim = 0.05 + n * years * 2.5e-5 + years * per_year * 1.5e-3
    rb = spec.robustness
    grid = 1
    for v in rb.sweep.values():
        grid *= len(v)
    extras = [{"name": "permutations", "label": f"Permutation test ({rb.permutations})", "enabled": rb.permutations > 0,
               "cost_s": round(sim * max(rb.permutations, 20), 2)},
              {"name": "sweep", "label": f"Parameter sweep ({grid if rb.sweep else 0} configs)", "enabled": bool(rb.sweep),
               "cost_s": round(sim * (grid if rb.sweep else 4) * rb.wf_folds / 2, 2)}]
    n_sims = 2 + rb.permutations + int(rb.stale_check) + (grid if rb.sweep else 0)
    panel = 0.2 if cached else 3.0 + n * years * 2e-3
    return {"total_s": round(panel + sim * n_sims + 1.0, 2), "panel_s": round(panel, 2), "sim_s": round(sim, 3),
            "n": int(n), "cached": bool(cached), "extras": extras}


# ------------------------------------------------------------------------------------------------ execution


def _make_check(run_id: str, settings: Settings, deadline: float, external: Callable[[], None] | None):
    flag = run_dir(run_id, settings) / "CANCEL"
    last = [time.monotonic()]

    def check() -> None:
        if external:
            external()
        if flag.exists():
            raise RunCancelled("cancelled by user")
        if time.monotonic() > deadline:
            raise RunTimeout("run exceeded its timeout")
        if time.monotonic() - last[0] > 5:
            last[0] = time.monotonic()
            try:
                _update(run_id, settings, heartbeat_at=_now())
            except OSError:
                pass

    return check


def prepare(spec: BacktestSpec, mode: str, strategy_id: str | None = None, settings: Settings | None = None,
            estimate: float | None = None, prior: dict[str, Any] | None = None, scheduled: bool = False,
            **extra: Any) -> dict[str, Any]:
    """Validate (plan builds) and persist a new run with status queued."""
    s = settings or get_settings()
    plan, plan_warns = make_plan(spec)
    rid = new_run_id()
    d = run_dir(rid, s)
    d.mkdir(parents=True, exist_ok=True)
    if "estimate_detail" in extra:  # learned breakdown (S1.2) -> status field `estimate`
        extra["estimate"] = extra.pop("estimate_detail")
    _write_json(d / "spec.json", {"run_id": rid, "strategy_id": strategy_id, "spec": spec.normalized(),
                                  "prior": prior or {}, "scheduled": scheduled})
    st = {"run_id": rid, "strategy_id": strategy_id, "status": "queued", "mode": mode, "bt_mode": spec.mode,
          "name": spec.name, "created_at": _now(), "started_at": None, "finished_at": None, "duration_s": None,
          "heartbeat_at": None, "error": None, "error_code": None, "spec_hash": spec.spec_hash(),
          "plan_key": None, "snapshot_id": None, "seed": spec.seed, "metrics": {}, "warnings_count": 0,
          "estimate_s": estimate, "timeout_s": spec.timeout_s, "requeued": 0, "scheduled": scheduled,
          "n_trials": int((prior or {}).get("n_trials") or 0), "trial_sharpes": [], "plan_warnings": plan_warns,
          **extra}
    _write_json(d / "status.json", st)
    return st


PREVIEW_MIN_S = 3.0   # estimated run time below which no preview is made (S1.3)


def preview_spec(spec: BacktestSpec) -> BacktestSpec | None:
    """The fast preview of a backtest: same panel (cached by the preview, reused by the full run), no permutation
    test, no sweep, no stale check. None when the run has no heavy robustness part to skip."""
    rb = spec.robustness
    if not (rb.permutations or rb.sweep or rb.stale_check):
        return None
    return spec.model_copy(update={"robustness": rb.model_copy(update={"permutations": 0, "sweep": {},
                                                                       "stale_check": False})})


def _run_preview(run_id: str, s: Settings, d: Path, spec: BacktestSpec, check: Callable[[], None],
                 prior: dict[str, Any]) -> float:
    t = time.perf_counter()
    ps = preview_spec(spec)
    if ps is None:
        return 0.0
    rb = spec.robustness
    try:
        _update(run_id, s, stage="preview")
        res = compute(ps, s, check, prior)
        res.pop("_holdings", None)
        res.pop("_trades", None)
        dur = round(time.perf_counter() - t, 3)
        info = {"status": "completed", "by": "robustness", "fraction": None, "duration_s": dur, "at": _now(),
                "skipped": [k for k, on in (("permutations", rb.permutations), ("sweep", rb.sweep),
                                            ("stale_check", rb.stale_check)) if on]}
        _write_json(d / "preview.json", {"run_id": run_id, "status": "completed", "preview": info, "result": res})
        _update(run_id, s, preview=info, stage="full")
        return dur
    except (RunCancelled, RunTimeout):
        raise
    except Exception as e:  # noqa: BLE001 - a failed preview never fails the run
        _update(run_id, s, preview={"status": "failed", "error": f"{type(e).__name__}: {e}"[:300], "at": _now()},
                stage="full")
        return round(time.perf_counter() - t, 3)


def read_preview(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        return _read_json(run_dir(run_id, settings) / "preview.json")
    except ValueError:
        return None


def execute(run_id: str, settings: Settings | None = None, external_check: Callable[[], None] | None = None,
            preview: bool = False) -> dict[str, Any]:
    """Run a prepared backtest to a terminal status. Never raises for spec / data errors (status=failed).
    preview=True (studio lane): a run without the heavy robustness part first (preview.json), S1.3."""
    s = settings or get_settings()
    d = run_dir(run_id, s)
    doc = _read_json(d / "spec.json")
    if doc is None:
        raise FileNotFoundError(run_id)
    st0 = _read_json(d / "status.json") or {}
    if st0.get("status") in TERMINAL:
        return st0
    if (d / "CANCEL").exists():
        return _update(run_id, s, status="cancelled", finished_at=_now(), error="cancelled before start",
                       error_code="cancelled")
    spec = BacktestSpec.model_validate(doc["spec"])
    t0, started = time.perf_counter(), _now()
    _update(run_id, s, status="running", started_at=started, heartbeat_at=started, pid=os.getpid(), stage="panel")
    check = _make_check(run_id, s, time.monotonic() + spec.timeout_s, external_check)
    res, status, err, code = None, "failed", None, None
    plan = None
    try:
        plan, _ = make_plan(spec)
        est_run = float((st0.get("estimate") or {}).get("run_s") or st0.get("estimate_s") or 0)
        pv_s = _run_preview(run_id, s, d, spec, check, doc.get("prior") or {}) if preview and est_run >= PREVIEW_MIN_S else 0.0
        res = compute(spec, s, check, doc.get("prior") or {})
        if pv_s and isinstance(res.get("timings"), dict):
            res["timings"]["preview_s"] = pv_s
        status = "completed"
    except RunCancelled as e:
        status, err, code = "cancelled", str(e), "cancelled"
    except RunTimeout as e:
        err, code = f"{e} ({spec.timeout_s} s)", "timeout"
    except (_panel.PanelError, ValueError, ValidationError) as e:
        err, code = str(e), "spec"
        from app.modeling._core import SpecError  # noqa: PLC0415

        if isinstance(e, SpecError):
            code = "dataset"
    except Exception as e:  # noqa: BLE001
        log.exception("backtest run %s crashed", run_id)
        err, code = f"{type(e).__name__}: {e}", "internal"
        (d / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finished = _now()
    dur = round(time.perf_counter() - t0, 3)
    hold = trades = None
    if res is not None:
        hold, trades = res.pop("_holdings", None), res.pop("_trades", None)
        for name, df in (("holdings", hold), ("trades", trades)):
            if df is not None and df.height:
                df.write_parquet(d / f"{name}.parquet")
    panel_meta = (res or {}).get("panel") or {}
    repro = {"spec_hash": spec.spec_hash(), "plan_key": panel_meta.get("plan_key"),
             "snapshot_id": panel_meta.get("snapshot_id") or snapshot_id(s), "seed": spec.seed,
             "engine_version": _engine.ENGINE_VERSION, "panel_version": _panel.PANEL_VERSION,
             "versions": library_versions(), "started_at": started, "finished_at": finished, "duration_s": dur,
             "timings": (res or {}).get("timings")}
    rb = (res or {}).get("robustness") or {}
    sweep_sr = [g.get("is_sr") for g in ((rb.get("sweep") or {}).get("grid") or []) if g.get("is_sr") is not None]
    trial_sr = sweep_sr or ([rb["trial_sharpe"]] if rb.get("trial_sharpe") is not None else [])
    env = {"run_id": run_id, "strategy_id": doc.get("strategy_id"), "status": status, "name": spec.name,
           "mode": spec.mode, "error": err, "error_code": code, "spec": spec.normalized(),
           "run_spec": run_dict(spec), "plan": plan, "result": res, "reproducibility": repro,
           "scheduled": doc.get("scheduled", False)}
    _write_json(d / "result.json", env)
    flat = (res or {}).get("flat_metrics") or {}
    return _update(run_id, s, status=status, finished_at=finished, duration_s=dur, error=err, error_code=code,
                   plan_key=repro["plan_key"], snapshot_id=repro["snapshot_id"], metrics=flat,
                   warnings_count=len((res or {}).get("warnings") or []), heartbeat_at=finished,
                   trial_sharpes=trial_sr, n_trials=int(rb.get("n_trials") or 0),
                   has_oos=bool(((res or {}).get("metrics") or {}).get("out_of_sample")), stage=None)


def cancel(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    st = read_status(run_id, s)
    if st is None:
        return None
    if st["status"] in TERMINAL:
        return st
    (run_dir(run_id, s) / "CANCEL").write_text(_now(), encoding="utf-8")
    if st["status"] == "queued":
        return _update(run_id, s, status="cancelled", finished_at=_now(), error="cancelled before start",
                       error_code="cancelled")
    return _update(run_id, s, cancel_requested_at=_now())


def enqueue(run_id: str, settings: Settings | None = None) -> dict[str, Any]:
    from app.core.jobqueue import enqueue_job  # noqa: PLC0415

    return enqueue_job("backtest_run", params={"run_id": run_id}, trigger="api", settings=settings)


def submit(spec: BacktestSpec, mode: str = "auto", strategy_id: str | None = None, settings: Settings | None = None,
           prior: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """API entry: (200, status) after an inline run, or (202, status) for a queued run. Async runs go to the warm
    studio lane while it is up (S1.1), else to the worker job backtest_run (fallback, BIG360_STUDIO_LANE=0)."""
    from app.modeling import estimator, lane  # noqa: PLC0415

    s = settings or get_settings()
    est = estimator.estimate_backtest(spec, s, path="sync")
    run_sync = mode == "sync" or (mode == "auto" and est["run_s"] < SYNC_BUDGET_S)
    use_lane = not run_sync and lane.available(s)
    if not run_sync:
        est = estimator.estimate_backtest(spec, s, path="lane" if use_lane else "worker",
                                          queue_s=lane.queue_s(s) if use_lane else 0.0)
    st = prepare(spec, "sync" if run_sync else "async", strategy_id, s, est["total_s"], prior, estimate_detail=est,
                 lane="sync" if run_sync else ("lane" if use_lane else "worker"), prior_estimate_s=est["prior_s"])
    if run_sync:
        return 200, execute(st["run_id"], s)
    if use_lane:
        lane.submit("backtest", st["run_id"], est["run_s"], s)
        return 202, st
    req = enqueue(st["run_id"], s)
    return 202, _update(st["run_id"], s, queue_request_id=req.get("request_id"))


def requeue_if_stale(st: dict[str, Any], settings: Settings | None = None, max_age_s: float = 90) -> dict[str, Any]:
    """A queued async run whose queue request vanished (skipped while another backtest_run drained) is re-enqueued;
    a lane run still queued while the lane is down is withdrawn and moved to the worker."""
    if st.get("status") != "queued" or st.get("mode") != "async":
        return st
    s = settings or get_settings()
    if st.get("lane") == "lane":
        from app.modeling import lane  # noqa: PLC0415

        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(st["created_at"])).total_seconds()
        except (KeyError, ValueError):
            return st
        if age < lane.FALLBACK_AFTER_S or lane.available(s) or not lane.withdraw("backtest", st["run_id"], s):
            return st
        req = enqueue(st["run_id"], s)
        return _update(st["run_id"], s, lane="worker", queue_request_id=req.get("request_id"), requeued_at=_now(),
                       fallback="studio lane unavailable")
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(st.get("requeued_at") or st["created_at"])).total_seconds()
    except (KeyError, ValueError):
        return st
    if age < max_age_s or int(st.get("requeued") or 0) >= 3:
        return st
    from app.core.jobqueue import list_requests  # noqa: PLC0415

    live = [r for state in ("pending", "processing") for r in list_requests(state, s) if r.get("job_id") == "backtest_run"]
    if live:
        return st
    req = enqueue(st["run_id"], s)
    return _update(st["run_id"], s, requeued=int(st.get("requeued") or 0) + 1, queue_request_id=req.get("request_id"),
                   requeued_at=_now())


def queued_runs(settings: Settings | None = None) -> list[str]:
    root = backtest_dir(settings) / "runs"
    out = []
    for p in sorted(root.glob("*/status.json")):
        st = _read_json(p)
        if st and st.get("status") == "queued" and st.get("mode") == "async" and st.get("lane") != "lane":
            out.append((st.get("created_at") or "", st["run_id"]))
    return [r for _, r in sorted(out)]


def run_job(ctx: Any) -> Any:
    """`backtest_run` job (manual DAG, process executor): execute params.run_id, then drain other queued runs."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    first = ctx.params.get("run_id")
    done: list[str] = []
    failed = 0
    order = ([first] if first else []) + [r for r in queued_runs(s) if r != first]
    seen: set[str] = set()
    while order:
        rid = order.pop(0)
        if rid in seen:
            continue
        seen.add(rid)
        st = read_status(rid, s)
        if not st or st.get("status") != "queued":
            continue
        ctx.log.info("backtest run %s (%s)", rid, st.get("name"))
        out = execute(rid, s, external_check=ctx.check_cancelled, preview=True)
        done.append(rid)
        failed += out.get("status") == "failed"
        if not order:
            order = [r for r in queued_runs(s) if r not in seen]
    return JobResult(status="success" if not failed else "partial", entities_ok=len(done) - failed,
                     entities_failed=failed, message=f"backtest runs: {', '.join(done) or 'none queued'}",
                     retryable=False)
