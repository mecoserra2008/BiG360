"""Model Studio run engine (owner: model-studio).

    from app.modeling.engine import submit, execute, read_status, read_result, cancel
    code, payload = submit(study, mode="auto")     # 200 + finished run (sync) or 202 + queued run (async)

* Every run lives in data/modeling/runs/<run_id>/: spec.json (StudySpec), status.json (lifecycle, source of truth for
  status), result.json (envelope: dataset summary + Result + reproducibility block), CANCEL (cancel request flag).
* Sync path (API process): when the estimated cost is < SYNC_BUDGET_S the API executes inline so the UI feels instant.
* Async path: the API writes spec/status (queued) and enqueues the generic `studio_run` job (contracts/jobs.yaml,
  manual DAG, process executor). The job (`run_job`) executes the requested run and then drains any other queued
  runs (the runner allows one concurrent run per job id, so a second request may be skipped while the first drains).
* Cancellation: DELETE writes CANCEL; running fits check it between stages and inside search loops (plus
  ctx.check_cancelled() and the study's timeout_s). Status: queued | running | completed | failed | cancelled.
* The worker never touches app_state.duckdb: the API syncs studies/study_runs from status.json (app.modeling.store).
"""

from __future__ import annotations

import json
import os
import platform
import random
import secrets
import shutil
import time
import traceback
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

from . import _core
from .datasets import SpecError, build_dataset, dataset_key, modeling_dir, snapshot_id
from .diagnostics import check_lookahead
from .registry import FitData, StudySpec, get_method
from .results import GuardrailError, clean

log = get_logger("app.modeling.engine")

SYNC_BUDGET_S = 2.0
TERMINAL = ("completed", "failed", "cancelled")
STALE_RUNNING_S = 180
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
UNIVERSE_SIZE_GUESS = {"mvp_us": 520, "mvp_global": 280, "full": 3000, "eu_broad": 1200, "watch": 50}


class RunCancelled(RuntimeError):
    pass


class RunTimeout(RuntimeError):
    pass


def new_run_id() -> str:
    return "SR" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "".join(secrets.choice(_B32) for _ in range(5))


def run_dir(run_id: str, settings: Settings | None = None) -> Path:
    if not run_id or any(c in run_id for c in "/\\.:") or len(run_id) > 64:
        raise ValueError("bad run id")
    return modeling_dir(settings) / "runs" / run_id


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
    out = {"python": platform.python_version(), "studio_core": _core.CORE_VERSION}
    for m in ("numpy", "pandas", "polars", "duckdb", "scipy", "statsmodels", "sklearn", "pydantic"):
        try:
            out[m] = __import__(m).__version__
        except Exception:  # pragma: no cover
            out[m] = "n/a"
    return out


# ------------------------------------------------------------------------------------------------ status


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
        if age > max(STALE_RUNNING_S, 0) + float(st.get("timeout_s") or 0):
            st.update(status="failed", error="run stopped without a result (worker died or was killed)",
                      error_code="stale", finished_at=_now())
            _write_json(d / "status.json", st)
    return st


def read_result(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        return _read_json(run_dir(run_id, settings) / "result.json")
    except ValueError:
        return None


def read_spec(run_id: str, settings: Settings | None = None) -> StudySpec | None:
    try:
        doc = _read_json(run_dir(run_id, settings) / "spec.json")
    except ValueError:
        return None
    return StudySpec.model_validate(doc["study"]) if doc else None


def _update_status(run_id: str, settings: Settings, **fields: Any) -> dict[str, Any]:
    p = run_dir(run_id, settings) / "status.json"
    st = _read_json(p) or {"run_id": run_id}
    st.update(fields)
    _write_json(p, st)
    return st


# ------------------------------------------------------------------------------------------------ validation / estimate


def validate_study(study: StudySpec) -> Any:
    """Method exists, params parse, roles fit the method, columns exist in the dataset spec, no look-ahead x."""
    m = get_method(study.method)
    m.validate_roles(study)
    params = m.parse_params(study.params)
    names = {v.name for v in study.dataset.variables}
    for c in [study.y] + list(study.x) + [study.weights] + m.extra_columns(params):
        if c and c not in names and not _derived_column(c, study):
            raise GuardrailError("unknown_column", f"column {c!r} is not a dataset variable / dummy / interaction")
    leads = [v.name for v in study.dataset.variables if v.uses_future]
    pairs = {f"{a}_x_{b}": [a, b] for a, b in study.dataset.interactions}
    check_lookahead({"lead_variables": leads, "interaction_pairs": pairs},
                    list(study.x) + m.regressor_columns(params), study.weights)
    return params


def _derived_column(c: str, study: StudySpec) -> bool:
    if c in {f"{a}_x_{b}" for a, b in study.dataset.interactions}:
        return True
    return any(c.startswith(f"{d}_") for d in study.dataset.dummies)


def estimate_seconds(study: StudySpec, settings: Settings | None = None) -> float:
    """Static prior cost (dataset build + fit, no extras / queue / overhead). The estimate shown to the user and used
    for the sync decision is the learned one (`app.modeling.estimator.estimate_studio`, S1.2)."""
    p = prior_parts(study, settings)
    return round(p["build_s"] + p["fit_s"], 3)


def prior_parts(study: StudySpec, settings: Settings | None = None, cached: bool | None = None) -> dict[str, Any]:
    """Static prior pieces: {build_s, fit_s, rows, n_ent, cached}. Entity counts are the snapshot's real universe
    sizes (estimator.entity_count, capped at max_entities like the builder)."""
    from .estimator import entity_count

    ds = study.dataset
    s = settings or get_settings()
    if cached is None:
        key = dataset_key(ds, snapshot_id(s))
        cached = (modeling_dir(s) / "cache" / f"{key}.parquet").exists()
    end = ds.end or ds.as_of or date.today()
    start = ds.start or date(end.year - (20 if ds.frequency in ("M", "Q") else 10), end.month, 1)
    years = max((end - start).days / 365.25, 0.1)
    periods = years * {"D": 252, "W": 52, "M": 12, "Q": 4}[ds.frequency]
    if ds.kind == "cross_section":
        periods = ds.lookback + 1

    def guess(e: Any) -> int:
        return entity_count(e, int(getattr(ds, "max_entities", 3000) or 3000), s)

    n_ent = guess(ds.entities)
    build = 0.05
    if not cached:
        for v in ds.variables:
            if v.source == "bql":  # roadmap v3 B1: BQL expression on the serving snapshot
                build += 0.5 + n_ent * periods * 5e-7
                continue
            src = _core.SOURCES[v.source]
            if src["entity"] == "macro":
                build += 0.1
            else:
                ne = guess(v.entities) if (v.entities and ds.kind == "timeseries") else n_ent
                big = src["table"] in ("prices_daily", "valuation_daily")
                build += 0.3 + ne * periods * (8e-7 if big else 3e-7)
    rows = periods if ds.kind == "timeseries" else (n_ent if ds.kind == "cross_section" else n_ent * periods)
    if ds.entity_level != "security" and ds.kind == "panel":
        rows = 12 * periods
    m = get_method(study.method)
    try:
        params = m.parse_params(study.params)
        fit = m.cost(int(rows), max(len(study.x), 1), params)
    except Exception:
        fit = 1.0
    return {"build_s": round(build, 3), "fit_s": round(float(fit), 3), "rows": int(rows), "n_ent": int(n_ent),
            "cached": bool(cached)}


# ------------------------------------------------------------------------------------------------ execution


def _make_check(run_id: str, settings: Settings, deadline: float, external: Callable[[], None] | None):
    cancel_flag = run_dir(run_id, settings) / "CANCEL"
    last_hb = [time.monotonic()]

    def check() -> None:
        if external:
            external()
        if cancel_flag.exists():
            raise RunCancelled("cancelled by user")
        if time.monotonic() > deadline:
            raise RunTimeout("run exceeded its timeout")
        if time.monotonic() - last_hb[0] > 5:
            last_hb[0] = time.monotonic()
            try:
                _update_status(run_id, settings, heartbeat_at=_now())
            except OSError:
                pass

    return check


def prepare(study: StudySpec, mode: str, study_id: str | None = None, settings: Settings | None = None,
            estimate: float | None = None, **extra: Any) -> dict[str, Any]:
    """Validate and persist a new run (status queued). Raises GuardrailError / pydantic errors."""
    s = settings or get_settings()
    validate_study(study)
    rid = new_run_id()
    d = run_dir(rid, s)
    d.mkdir(parents=True, exist_ok=True)
    if "estimate_detail" in extra:  # learned breakdown (S1.2) -> status field `estimate`
        extra["estimate"] = extra.pop("estimate_detail")
    _write_json(d / "spec.json", {"run_id": rid, "study_id": study_id, "study": study.model_dump(mode="json")})
    st = {"run_id": rid, "study_id": study_id, "status": "queued", "mode": mode, "method": study.method,
          "name": study.name, "created_at": _now(), "started_at": None, "finished_at": None, "duration_s": None,
          "heartbeat_at": None, "error": None, "error_code": None, "spec_hash": study.spec_hash(),
          "dataset_key": None, "snapshot_id": None, "seed": study.seed, "metrics": {}, "warnings_count": 0,
          "estimate_s": estimate, "timeout_s": study.timeout_s, "requeued": 0, **extra}
    _write_json(d / "status.json", st)
    return st


# ------------------------------------------------------------------------------------------------ preview (S1.3)

PREVIEW_MIN_FIT_S = 1.5      # runs whose estimated fit is shorter get no preview (the full result comes as fast)
PREVIEW_MIN_ROWS = 200
#: heavy optional outputs turned off in the preview (bools) or cut to their minimum (counts)
PREVIEW_OFF = ("shap", "placebo", "permutation")
PREVIEW_MIN = ("bootstrap", "permutations", "n_boot", "n_bootstrap", "pdp_features")
#: counts capped in the preview (trees / search restarts; cv folds capped at 3)
PREVIEW_CAP = {"n_estimators": 100, "search_reps": 2, "max_iter": 100}


def preview_fraction(est_fit_s: float) -> float:
    return 0.2 if est_fit_s <= 10 else (0.15 if est_fit_s <= 30 else 0.1)


def preview_params(params: Any) -> Any:
    """Params of the preview fit: heavy extras off / at their minimum (any method; unknown fields untouched)."""
    fields = getattr(type(params), "model_fields", {})
    upd: dict[str, Any] = {}
    for name, f in fields.items():
        if name in PREVIEW_OFF and isinstance(getattr(params, name, None), bool):
            upd[name] = False
        elif name in PREVIEW_MIN and isinstance(getattr(params, name, None), int):
            lo = [getattr(m, "ge", None) for m in f.metadata if getattr(m, "ge", None) is not None]
            upd[name] = int(lo[0]) if lo else getattr(params, name)
        elif name in PREVIEW_CAP and isinstance(getattr(params, name, None), int):
            upd[name] = min(getattr(params, name), PREVIEW_CAP[name])
        elif name == "cv" and isinstance(getattr(getattr(params, name, None), "folds", None), int):
            cv = getattr(params, name)
            upd[name] = cv.model_copy(update={"folds": min(cv.folds, 3)})
    return params.model_copy(update=upd) if upd else params


def sample_frame(frame: Any, kind: str, fraction: float, seed: int) -> tuple[Any, str]:
    """10-20 % sample of a dataset frame: entities (panel / cross-section) or the most recent dates (time series)."""
    import polars as pl

    if kind != "timeseries" and "entity_id" in frame.columns:
        ents = frame["entity_id"].unique().sort()
        n = max(int(round(len(ents) * fraction)), min(len(ents), 30))
        keep = ents.sample(n=n, seed=seed % (2 ** 32)) if n < len(ents) else ents
        return frame.filter(pl.col("entity_id").is_in(keep.to_list())), "entities"
    n = max(int(round(frame.height * fraction)), min(frame.height, 120))
    return frame.tail(n), "dates"


def _run_preview(run_id: str, s: Settings, d: Path, study: StudySpec, method: Any, params: Any, ds: Any,
                 check: Callable[[], None], est_fit: float) -> float:
    """Fit the method on a sample, write preview.json and the status `preview` block. Returns its seconds."""
    t = time.perf_counter()
    frac = preview_fraction(est_fit)
    try:
        frame, how = sample_frame(ds.frame, ds.meta.get("kind", study.dataset.kind), frac, study.seed)
        if frame.height >= 0.8 * ds.frame.height:  # too small to sample: the full run is the preview
            return 0.0
        _update_status(run_id, s, stage="preview")
        res = method.fit(FitData(frame, ds.meta, study.y, list(study.x), study.weights, study.seed, check),
                         preview_params(params))
        dur = round(time.perf_counter() - t, 3)
        info = {"status": "completed", "fraction": frac, "by": how, "n_rows": frame.height,
                "n_rows_full": ds.frame.height, "duration_s": dur, "at": _now()}
        _write_json(d / "preview.json", clean({"run_id": run_id, "status": "completed", "method": study.method,
                                               "preview": info, "result": res.to_dict()}))
        _update_status(run_id, s, preview=info, stage="fit")
        return dur
    except (RunCancelled, RunTimeout):
        raise
    except Exception as e:  # noqa: BLE001 - a failed preview never fails the run
        _update_status(run_id, s, preview={"status": "failed", "error": f"{type(e).__name__}: {e}"[:300],
                                           "fraction": frac, "at": _now()}, stage="fit")
        return round(time.perf_counter() - t, 3)


def read_preview(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    try:
        return _read_json(run_dir(run_id, settings) / "preview.json")
    except ValueError:
        return None


def execute(run_id: str, settings: Settings | None = None, external_check: Callable[[], None] | None = None,
            preview: bool = False) -> dict[str, Any]:
    """Run a prepared study to a terminal status. Never raises for study errors (they become status=failed).
    preview=True (studio lane): a sample fit first (preview.json) when the estimated fit is long enough (S1.3)."""
    s = settings or get_settings()
    d = run_dir(run_id, s)
    doc = _read_json(d / "spec.json")
    if doc is None:
        raise FileNotFoundError(run_id)
    st0 = _read_json(d / "status.json") or {}
    if st0.get("status") in TERMINAL:
        return st0
    if (d / "CANCEL").exists():
        return _update_status(run_id, s, status="cancelled", finished_at=_now(), error="cancelled before start")
    study = StudySpec.model_validate(doc["study"])
    t0, started = time.perf_counter(), _now()
    _update_status(run_id, s, status="running", started_at=started, heartbeat_at=started, pid=os.getpid(),
                   stage="dataset")
    check = _make_check(run_id, s, time.monotonic() + study.timeout_s, external_check)
    random.seed(study.seed)
    np.random.seed(study.seed % (2 ** 32))
    timings: dict[str, float] = {}
    try:
        method = get_method(study.method)
        params = validate_study(study)
        check()
        ds = build_dataset(study.dataset, s, check=check)
        timings["dataset_s"] = round(time.perf_counter() - t0, 3)
        check_lookahead(ds.meta, list(study.x) + method.regressor_columns(params), study.weights)
        missing = [c for c in ([study.y] + list(study.x) + [study.weights]) if c and c not in ds.frame.columns]
        if missing:
            raise GuardrailError("unknown_column", f"columns not in the dataset: {missing}")
        est_fit = float((st0.get("estimate") or {}).get("fit_s") or 0.0)
        if preview and est_fit >= PREVIEW_MIN_FIT_S and ds.frame.height >= PREVIEW_MIN_ROWS:
            timings["preview_s"] = _run_preview(run_id, s, d, study, method, params, ds, check, est_fit)
        else:
            _update_status(run_id, s, stage="fit")
        t1 = time.perf_counter()
        data = FitData(ds.frame, ds.meta, study.y, list(study.x), study.weights, study.seed, check)
        res = method.fit(data, params)
        timings["fit_s"] = round(time.perf_counter() - t1, 3)
        for w in ds.meta.get("warnings", []):
            res.info("dataset", w)
        if study.y and study.y in ds.meta.get("lead_variables", []):
            res.info("target_uses_future", f"{study.y} looks forward (lead/fwd transform): fine as a target; "
                     "overlapping forward windows make residuals autocorrelated (use HAC / clustered SEs)")
        result = res.to_dict()
        for name, art in (getattr(res, "artifacts", None) or {}).items():  # e.g. oos_predictions (backtest lab)
            try:
                art.write_parquet(d / f"{name}.parquet")
            except Exception:  # noqa: BLE001 - artifacts are optional
                log.warning("studio run %s: artifact %s not written", run_id, name)
        status, err, code = "completed", None, None
    except RunCancelled as e:
        result, status, err, code = None, "cancelled", str(e), "cancelled"
    except RunTimeout as e:
        result, status, err, code = None, "failed", f"{e} ({study.timeout_s} s)", "timeout"
    except GuardrailError as e:
        result, status, err, code = None, "failed", str(e), e.code
    except SpecError as e:
        result, status, err, code = None, "failed", str(e), "dataset"
    except Exception as e:  # noqa: BLE001
        log.exception("studio run %s crashed", run_id)
        result, status, err, code = None, "failed", f"{type(e).__name__}: {e}", "internal"
        (d / "traceback.txt").write_text(traceback.format_exc(), encoding="utf-8")
    finished = _now()
    dur = round(time.perf_counter() - t0, 3)
    ds_info = None
    if status == "completed":
        cov = ds.meta.get("coverage", [])
        ds_info = {"key": ds.key, "snapshot_id": ds.snapshot_id, "cached": ds.cached, "kind": ds.meta["kind"],
                   "frequency": ds.meta["frequency"], "macro_alignment": ds.meta.get("macro_alignment"),
                   "start": ds.meta["start"], "end": ds.meta["end"], "n_rows": ds.meta["n_rows"],
                   "n_entities": ds.meta["n_entities"], "n_periods": ds.meta["n_periods"], "coverage": cov,
                   "warnings": ds.meta.get("warnings", []), "build_seconds": ds.meta.get("build_seconds")}
        try:
            src = modeling_dir(s) / "cache" / f"{ds.key}.parquet"
            if src.exists() and src.stat().st_size < 50 * 1024 * 1024:
                shutil.copyfile(src, d / "dataset.parquet")
        except OSError:
            pass
    repro = {"spec_hash": study.spec_hash(), "dataset_key": ds_info["key"] if ds_info else None,
             "snapshot_id": ds_info["snapshot_id"] if ds_info else snapshot_id(s), "seed": study.seed,
             "versions": library_versions(), "started_at": started, "finished_at": finished, "duration_s": dur,
             "timings": timings}
    envelope = clean({"run_id": run_id, "study_id": doc.get("study_id"), "status": status, "method": study.method,
                      "name": study.name, "error": err, "error_code": code, "study": study.model_dump(mode="json"),
                      "dataset": ds_info, "result": result, "reproducibility": repro})
    _write_json(d / "result.json", envelope)
    metrics = (result or {}).get("metrics", {}) if result else {}
    n_obs = ((result or {}).get("summary") or {}).get("n_obs") if result else None
    return _update_status(run_id, s, status=status, title=(result or {}).get("title") if result else None,
                          n_obs=n_obs if isinstance(n_obs, int) else None, finished_at=finished, duration_s=dur, error=err, error_code=code,
                          dataset_key=repro["dataset_key"], snapshot_id=repro["snapshot_id"], metrics=metrics,
                          warnings_count=len((result or {}).get("warnings", [])), heartbeat_at=finished, stage=None)


def cancel(run_id: str, settings: Settings | None = None) -> dict[str, Any] | None:
    s = settings or get_settings()
    st = read_status(run_id, s)
    if st is None:
        return None
    if st["status"] in TERMINAL:
        return st
    (run_dir(run_id, s) / "CANCEL").write_text(_now(), encoding="utf-8")
    if st["status"] == "queued":
        return _update_status(run_id, s, status="cancelled", finished_at=_now(), error="cancelled before start",
                              error_code="cancelled")
    return _update_status(run_id, s, cancel_requested_at=_now())


def enqueue(run_id: str, settings: Settings | None = None) -> dict[str, Any]:
    from app.core.jobqueue import enqueue_job

    return enqueue_job("studio_run", params={"run_id": run_id}, trigger="api", settings=settings)


def submit(study: StudySpec, mode: str = "auto", study_id: str | None = None,
           settings: Settings | None = None) -> tuple[int, dict[str, Any]]:
    """API entry: (200, status) after an inline run, or (202, status) for a queued run.

    Async runs go to the warm studio lane while it is up (app.modeling.lane, S1.1), else to the worker job
    studio_run (fallback; BIG360_STUDIO_LANE=0 forces it). The estimate is the learned one (S1.2) for the path taken:
    queue + overhead + dataset build + fit."""
    from . import estimator, lane

    s = settings or get_settings()
    est = estimator.estimate_studio(study, s, path="sync")
    run_sync = mode == "sync" or (mode == "auto" and est["run_s"] < SYNC_BUDGET_S)
    use_lane = not run_sync and lane.available(s)
    if not run_sync:
        path = "lane" if use_lane else "worker"
        est = estimator.estimate_studio(study, s, path=path, queue_s=lane.queue_s(s) if use_lane else 0.0)
    st = prepare(study, "sync" if run_sync else "async", study_id, s, est["total_s"], estimate_detail=est,
                 lane="sync" if run_sync else ("lane" if use_lane else "worker"), prior_estimate_s=est["prior_s"])
    if run_sync:
        return 200, execute(st["run_id"], s)
    if use_lane:
        lane.submit("studio", st["run_id"], est["run_s"], s)
        return 202, st
    req = enqueue(st["run_id"], s)
    st = _update_status(st["run_id"], s, queue_request_id=req.get("request_id"))
    return 202, st


def requeue_if_stale(st: dict[str, Any], settings: Settings | None = None, max_age_s: float = 90) -> dict[str, Any]:
    """A queued async run whose queue request vanished (e.g. skipped while another studio_run drained) is re-enqueued.
    A lane run still queued while the lane is down (> lane.FALLBACK_AFTER_S) is withdrawn and moved to the worker."""
    if st.get("status") != "queued" or st.get("mode") != "async":
        return st
    s = settings or get_settings()
    if st.get("lane") == "lane":
        from . import lane

        try:
            age = (datetime.now(UTC) - datetime.fromisoformat(st["created_at"])).total_seconds()
        except (KeyError, ValueError):
            return st
        if age < lane.FALLBACK_AFTER_S or lane.available(s) or not lane.withdraw("studio", st["run_id"], s):
            return st
        req = enqueue(st["run_id"], s)
        return _update_status(st["run_id"], s, lane="worker", queue_request_id=req.get("request_id"),
                              requeued_at=_now(), fallback="studio lane unavailable")
    try:
        age = (datetime.now(UTC) - datetime.fromisoformat(st.get("requeued_at") or st["created_at"])).total_seconds()
    except (KeyError, ValueError):
        return st
    if age < max_age_s or int(st.get("requeued") or 0) >= 3:
        return st
    from app.core.jobqueue import list_requests

    live = [r for state in ("pending", "processing") for r in list_requests(state, s) if r.get("job_id") == "studio_run"]
    if live:
        return st
    req = enqueue(st["run_id"], s)
    return _update_status(st["run_id"], s, requeued=int(st.get("requeued") or 0) + 1,
                          queue_request_id=req.get("request_id"), requeued_at=_now())


def queued_runs(settings: Settings | None = None) -> list[str]:
    root = modeling_dir(settings) / "runs"
    out = []
    for p in sorted(root.glob("*/status.json")):
        st = _read_json(p)
        if st and st.get("status") == "queued" and st.get("mode") == "async" and st.get("lane") != "lane":
            out.append((st.get("created_at") or "", st["run_id"]))
    return [r for _, r in sorted(out)]


# ------------------------------------------------------------------------------------------------ worker job


def run_job(ctx: Any) -> Any:
    """`studio_run` job (manual DAG, process executor): execute params.run_id, then drain other queued runs."""
    from app.scheduler.base import JobResult

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
        ctx.log.info("studio run %s (%s)", rid, st.get("method"))
        out = execute(rid, s, external_check=ctx.check_cancelled, preview=True)
        done.append(rid)
        failed += out.get("status") == "failed"
        if not order:
            order = [r for r in queued_runs(s) if r not in seen]
    return JobResult(status="success" if not failed else "partial", entities_ok=len(done) - failed,
                     entities_failed=failed, message=f"studio runs: {', '.join(done) or 'none queued'}",
                     retryable=False)
