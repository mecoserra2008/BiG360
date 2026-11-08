"""Model Studio (tag: studio): catalogs, dataset preview, runs (sync / async on the worker), saved studies, compare
and script export. Owner: model-studio. Logic lives in app.modeling; this module only adapts HTTP.

Errors: guardrail / dataset-spec failures -> 422 problem+json with `type` = "urn:studio:<code>" (e.g. lookahead,
too_few_obs, singular, unknown_column, dataset). Pydantic validation errors -> 422 with `errors[]`.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.deps import FastRoute
from app.api.routers._common import not_found
from app.core.config import get_settings

router = APIRouter(tags=["studio"], route_class=FastRoute)
PROBLEM = "application/problem+json"


def _problem(status: int, code: str, detail: str, errors: list[Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": f"urn:studio:{code}", "title": "Study rejected" if status == 422 else "Error",
                            "status": status, "detail": detail}
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def _guard(fn: Any) -> Any:
    """Run fn() mapping modeling errors to problem responses."""
    from app.modeling._core import SpecError
    from app.modeling.results import GuardrailError

    try:
        return fn()
    except GuardrailError as e:
        return _problem(422, e.code, str(e))
    except SpecError as e:
        return _problem(422, "dataset", str(e))
    except ValidationError as e:
        errs = [{"loc": [str(x) for x in er.get("loc", [])], "msg": er.get("msg", "")} for er in e.errors()]
        return _problem(422, "invalid_spec", "invalid study / dataset / method-parameter specification", errs)


# ------------------------------------------------------------------------------------------------ request bodies


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: dict[str, Any]
    n: int = Field(default=50, ge=1, le=1000)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study: dict[str, Any]
    mode: Literal["auto", "sync", "async"] = "auto"
    study_id: str | None = None


class EstimateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study: dict[str, Any]
    mode: Literal["auto", "sync", "async"] = "auto"


class StudyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    study: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    preset_id: str | None = None


class StudyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    study: dict[str, Any] | None = None
    tags: list[str] | None = None
    schedule: Literal["none", "weekly", "monthly"] | None = None
    drift_threshold: float | None = Field(default=None, gt=0, le=10)


class LeaderboardRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    dataset: dict[str, Any]
    y: str
    x: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(min_length=2, max_length=12)
    task: Literal["auto", "regression", "classification"] = "auto"
    metric: Literal["rmse", "mae", "auc", "logloss"] | None = None
    cv: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["auto", "sync", "async"] = "auto"
    study_id: str | None = None
    seed: int = 42


class PeerSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[str] = Field(min_length=1, max_length=20)
    benchmark_run_id: str | None = Field(default=None, description="v1.50: DM / Clark-West benchmark (default: the first run)")


class LintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    study: dict[str, Any]


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=200)
    owner: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    stage: Literal["candidate", "production", "archived"] = "candidate"
    monitor: Literal["none", "weekly", "monthly"] = "none"
    psi_threshold: float = Field(default=0.25, gt=0, le=5)
    metric_floor: float | None = None


class ModelUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["candidate", "production", "archived"] | None = None
    owner: str | None = Field(default=None, max_length=120)
    description: str | None = Field(default=None, max_length=4000)
    monitor: Literal["none", "weekly", "monthly"] | None = None
    psi_threshold: float | None = Field(default=None, gt=0, le=5)
    metric_floor: float | None = None


class MonitorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str | None = Field(default=None, description="evaluate this (newer) run of the model; default: the promoted run")
    alert: bool = True


# ------------------------------------------------------------------------------------------------ catalogs


@router.get("/studio/catalog/datasets", operation_id="studioCatalogDatasets")
def catalog_datasets() -> dict[str, Any]:
    from app.modeling.datasets import list_sources

    return list_sources()


@router.get("/studio/catalog/variables", operation_id="studioCatalogVariables")
def catalog_variables(source: str | None = None, q: str | None = None,
                      entity_type: Literal["security", "issuer", "macro"] | None = None,
                      frequency: Literal["D", "W", "M", "Q", "A"] | None = None,
                      limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    from app.modeling.datasets import list_variables

    return list_variables(source, q, entity_type, frequency, limit, offset)


@router.get("/studio/catalog/methods", operation_id="studioCatalogMethods")
def catalog_methods() -> list[dict[str, Any]]:
    from app.modeling.registry import all_methods

    return [m.describe() for m in all_methods()]


@router.get("/studio/presets", operation_id="studioPresets")
def presets() -> list[dict[str, Any]]:
    from app.modeling.registry import list_presets

    return list_presets()


# ------------------------------------------------------------------------------------------------ datasets


@router.post("/studio/datasets/preview", operation_id="studioPreviewDataset")
def preview_dataset(body: PreviewRequest) -> Any:
    from app.modeling.datasets import DatasetSpec, preview

    def go() -> Any:
        spec = DatasetSpec.model_validate(body.dataset)
        return preview(spec, body.n)

    return _guard(go)


# ------------------------------------------------------------------------------------------------ runs


def _run_payload(run_id: str, include_result: bool = True) -> dict[str, Any] | None:
    from app.modeling import engine, store

    st = store.sync_run(run_id)
    if st is None:
        return None
    out: dict[str, Any] = {"run": store.run_out(st), "result": None, "preview": None}
    if include_result and st.get("status") in engine.TERMINAL:
        out["result"] = engine.read_result(run_id)
    elif isinstance(st.get("preview"), dict) and st["preview"].get("status") == "completed":
        out["preview"] = engine.read_preview(run_id)  # S1.3: sample result while the full run refines it
    return out


@router.post("/studio/estimate", operation_id="studioEstimateRun")
def estimate_run(body: EstimateRequest) -> dict[str, Any]:
    """Learned estimate of a run before it is submitted (S1.2): path (sync | lane | worker), queue, overhead, dataset
    build, fit, heavy extras with their cost, and how many past runs of the method it learned from."""
    from app.modeling import engine, estimator, lane
    from app.modeling.registry import StudySpec

    def go() -> Any:
        study = StudySpec.model_validate(body.study)
        params = engine.validate_study(study)
        est = estimator.estimate_studio(study, path="sync", params=params)
        if body.mode == "async" or (body.mode == "auto" and est["run_s"] >= engine.SYNC_BUDGET_S):
            on_lane = lane.available()
            est = estimator.estimate_studio(study, path="lane" if on_lane else "worker", params=params,
                                            queue_s=lane.queue_s() if on_lane else 0.0)
        return {**est, "sync_budget_s": engine.SYNC_BUDGET_S, "will_run": "sync" if est["path"] == "sync" else "async"}

    return _guard(go)


@router.get("/studio/lane", operation_id="studioLaneStatus")
def lane_status() -> dict[str, Any]:
    """Interactive studio lane (S1.1): alive, pool, priority, running / queued runs, what blocks it, and the
    observed / estimated end-to-end ratio of recent Studio and backtest runs (estimate accuracy diagnostics)."""
    from app.modeling import estimator, lane

    st = lane.status(max_age_s=0) or {}
    return {"enabled": lane.enabled(), "alive": bool(st.get("alive")), "pid": st.get("pid"),
            "started_at": st.get("started_at"), "heartbeat_age_s": st.get("heartbeat_age_s"),
            "pool": st.get("pool"), "cores": st.get("cores"), "priority": st.get("priority"),
            "heavy_jobs": st.get("heavy_jobs") or [], "running": st.get("running") or [], "queue": st.get("queue") or [],
            "blocked_by": st.get("blocked_by"), "memory_mb": st.get("memory_mb"), "ram_cap_mb": st.get("ram_cap_mb"),
            "done": st.get("done"), "failed": st.get("failed"), "last_runs": st.get("last_runs") or [],
            "fallback": "worker" if not st.get("alive") else None,
            "accuracy": {"studio": estimator.accuracy("studio"), "backtest": estimator.accuracy("backtest")}}


@router.post("/studio/runs", operation_id="studioSubmitRun")
def submit_run(body: RunRequest) -> Any:
    from app.modeling import engine, store
    from app.modeling.registry import StudySpec

    def go() -> Any:
        if body.study_id and store.get_study(body.study_id) is None:
            raise not_found(f"study {body.study_id} not found")
        study = StudySpec.model_validate(body.study)
        code, st = engine.submit(study, body.mode, body.study_id)
        store.upsert_run(st, study.model_dump(mode="json"))
        payload = _run_payload(st["run_id"], include_result=code == 200)
        return payload if code == 200 else JSONResponse(payload, status_code=202)

    return _guard(go)


@router.get("/studio/runs", operation_id="studioListRuns")
def list_runs(study_id: str | None = None, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    from app.modeling import store

    return store.list_runs(study_id, limit)


@router.get("/studio/runs/{run_id}", operation_id="studioGetRun")
def get_run(run_id: str) -> dict[str, Any]:
    p = _run_payload(run_id)
    if p is None:
        raise not_found(f"run {run_id} not found")
    return p


@router.delete("/studio/runs/{run_id}", operation_id="studioCancelRun")
def cancel_run(run_id: str) -> dict[str, Any]:
    from app.modeling import engine, store

    st = engine.cancel(run_id)
    if st is None:
        raise not_found(f"run {run_id} not found")
    store.upsert_run(st)
    return store.run_out(st)


@router.get("/studio/runs/{run_id}/export.py", operation_id="studioExportRun")
def export_run(run_id: str) -> Response:
    from app.modeling import engine
    from app.modeling.export import export_script

    env = engine.read_result(run_id)
    if env is None:
        raise not_found(f"run {run_id} has no result")
    if env.get("status") != "completed":
        return _problem(409, "not_completed", f"run {run_id} is {env.get('status')}: only completed runs export")
    src = export_script(env, engine.run_dir(run_id), get_settings().data_dir)
    return PlainTextResponse(src, media_type="text/x-python",
                             headers={"Content-Disposition": f'attachment; filename="study_{run_id}.py"'})


@router.post("/studio/compare", operation_id="studioCompareRuns")
def compare_runs(body: CompareRequest) -> dict[str, Any]:
    from app.modeling import engine, store
    from app.modeling.compare import compare_oos

    out = store.compare(body.run_ids)
    specs = {}
    for rid in out["run_ids"]:
        sp = engine.read_spec(rid)
        specs[rid] = sp.model_dump(mode="json") if sp else {}
    out["oos"] = compare_oos(out["run_ids"], specs, body.benchmark_run_id)  # v1.50: DM / Clark-West / equity curves
    return out


# ------------------------------------------------------------------------------------------------ guided builder (v1.50)


@router.post("/studio/guide/suggest", operation_id="studioGuideSuggest")
def guide_suggest(body: dict[str, Any]) -> Any:
    from app.modeling.guide import Question, suggest

    return _guard(lambda: suggest(Question.model_validate(body)))


@router.post("/studio/lint", operation_id="studioLintStudy")
def lint_study(body: LintRequest) -> Any:
    from app.modeling.guide import lint
    from app.modeling.registry import StudySpec

    return _guard(lambda: lint(StudySpec.model_validate(body.study)))


# ------------------------------------------------------------------------------------------------ model registry (v1.50)


def _reg_guard(fn: Any) -> Any:
    from app.modeling.model_registry import RegistryError

    try:
        return fn()
    except RegistryError as e:
        if e.code == "not_found":
            raise not_found(str(e)) from e
        return _problem(409, e.code, str(e))


@router.post("/studio/runs/{run_id}/promote", operation_id="studioPromoteRun", status_code=201)
def promote_run(run_id: str, body: PromoteRequest) -> Any:
    from app.modeling import model_registry as MR

    return _reg_guard(lambda: JSONResponse(MR.promote(run_id, **body.model_dump()), status_code=201))


@router.get("/studio/registry", operation_id="studioListModels")
def list_models(name: str | None = None) -> list[dict[str, Any]]:
    from app.modeling import model_registry as MR

    return MR.list_models(name)


@router.get("/studio/registry/{model_id}", operation_id="studioGetModel")
def get_model(model_id: str) -> dict[str, Any]:
    from app.modeling import model_registry as MR

    m = MR.get_model(model_id)
    if m is None:
        raise not_found(f"model {model_id} not found")
    m["versions"] = MR.list_models(m["name"])
    return m


@router.patch("/studio/registry/{model_id}", operation_id="studioUpdateModel")
def update_model(model_id: str, body: ModelUpdate) -> dict[str, Any]:
    from app.modeling import model_registry as MR

    m = MR.update_model(model_id, body.model_dump(exclude_unset=True))
    if m is None:
        raise not_found(f"model {model_id} not found")
    return m


@router.delete("/studio/registry/{model_id}", operation_id="studioDeleteModel", status_code=204)
def delete_model(model_id: str) -> Response:
    from app.modeling import model_registry as MR

    if not MR.delete_model(model_id):
        raise not_found(f"model {model_id} not found")
    return Response(status_code=204)


@router.post("/studio/registry/{model_id}/monitor", operation_id="studioMonitorModel")
def monitor_model(model_id: str, body: MonitorRequest) -> dict[str, Any]:
    from app.modeling import model_registry as MR

    r = MR.monitor(model_id, body.run_id, body.alert)
    if r is None:
        raise not_found(f"model {model_id} not found")
    return r


# ------------------------------------------------------------------------------------------------ studies


@router.get("/studio/studies", operation_id="studioListStudies")
def list_studies() -> list[dict[str, Any]]:
    from app.modeling import store

    return store.list_studies()


@router.post("/studio/studies", operation_id="studioCreateStudy", status_code=201)
def create_study(body: StudyCreate) -> Any:
    from app.modeling import engine, store
    from app.modeling.registry import StudySpec

    def go() -> Any:
        study = StudySpec.model_validate(body.study)
        engine.validate_study(study)
        return JSONResponse(store.create_study(body.name, study, body.description, body.tags, body.preset_id),
                            status_code=201)

    return _guard(go)


@router.get("/studio/studies/{study_id}", operation_id="studioGetStudy")
def get_study(study_id: str) -> dict[str, Any]:
    from app.modeling import store

    st = store.get_study(study_id)
    if st is None:
        raise not_found(f"study {study_id} not found")
    return {"study": st, "runs": store.list_runs(study_id)}


@router.patch("/studio/studies/{study_id}", operation_id="studioUpdateStudy")
def update_study(study_id: str, body: StudyUpdate) -> Any:
    from app.modeling import engine, store
    from app.modeling.registry import StudySpec

    def go() -> Any:
        fields = body.model_dump(exclude_unset=True)
        sched = fields.pop("schedule", None)
        thr = fields.pop("drift_threshold", None)
        if (sched is not None or thr is not None) and store.get_study(study_id) is not None:
            cur = store.get_study(study_id)
            store.set_schedule(study_id, sched or cur.get("schedule") or "none", thr)
        if fields.get("study") is not None:
            fields["study"] = StudySpec.model_validate(fields["study"])
            engine.validate_study(fields["study"])
        out = store.update_study(study_id, fields)
        if out is None:
            raise not_found(f"study {study_id} not found")
        return out

    return _guard(go)


@router.delete("/studio/studies/{study_id}", operation_id="studioDeleteStudy", status_code=204)
def delete_study(study_id: str) -> Response:
    from app.modeling import store

    if not store.delete_study(study_id):
        raise not_found(f"study {study_id} not found")
    return Response(status_code=204)


# ------------------------------------------------------------------------------------------------ leaderboard / peer sets


@router.post("/studio/leaderboard", operation_id="studioLeaderboard")
def leaderboard(body: LeaderboardRequest) -> Any:
    """Shortcut for POST /studio/runs with method=leaderboard: same dataset / target, identical CV folds."""
    from app.modeling.registry import StudySpec

    def go() -> Any:
        study = StudySpec.model_validate({
            "name": body.name or "Leaderboard", "dataset": body.dataset, "method": "leaderboard", "y": body.y,
            "x": body.x, "seed": body.seed,
            "params": {"candidates": body.candidates, "task": body.task, "metric": body.metric, "cv": body.cv}})
        return submit_run(RunRequest(study=study.model_dump(mode="json"), mode=body.mode, study_id=body.study_id))

    return _guard(go)


@router.post("/studio/runs/{run_id}/peer-set", operation_id="studioCreatePeerSet", status_code=201)
def create_peer_set(run_id: str, body: PeerSetCreate) -> Any:
    from app.modeling import engine, store

    env = engine.read_result(run_id)
    if env is None or env.get("status") != "completed":
        raise not_found(f"completed run {run_id} not found")
    cl = ((env.get("result") or {}).get("clusters")) or {}
    mem = [{"security_id": m["entity_id"], "label": m.get("label"), "cluster": int(m["cluster"])}
           for m in cl.get("members") or [] if m.get("entity_id") and int(m.get("cluster", -1)) >= 0]
    mem = [m for m in mem if str(m["security_id"]).startswith(("S", "IDX:", "ETF:"))]
    if not cl or not mem:
        return _problem(422, "not_clustering", "the run has no security-level cluster members (use a clustering method "
                                               "on a security-level cross-section / panel)")
    uniq = list({m["security_id"]: m for m in mem}.values())
    return JSONResponse(store.create_peer_set(body.name, body.description, run_id, env["method"], uniq), status_code=201)


@router.get("/studio/peer-sets", operation_id="studioListPeerSets")
def list_peer_sets() -> list[dict[str, Any]]:
    from app.modeling import store

    return store.list_peer_sets()


@router.get("/studio/peer-sets/{peer_set_id}", operation_id="studioGetPeerSet")
def get_peer_set(peer_set_id: str) -> dict[str, Any]:
    from app.modeling import store

    ps = store.get_peer_set(peer_set_id)
    if ps is None:
        raise not_found(f"peer set {peer_set_id} not found")
    return ps


@router.delete("/studio/peer-sets/{peer_set_id}", operation_id="studioDeletePeerSet", status_code=204)
def delete_peer_set(peer_set_id: str) -> Response:
    from app.modeling import store

    if not store.delete_peer_set(peer_set_id):
        raise not_found(f"peer set {peer_set_id} not found")
    return Response(status_code=204)
