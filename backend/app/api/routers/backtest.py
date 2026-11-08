"""Backtesting lab (tag: backtest, roadmap v2 item #4): catalog, spec validation, "backtest this screen", runs (sync
or async on the worker), holdings / trades, compare, saved strategies (paper strategies with drift alerts) and
script export. Owner: backtest. Logic lives in app.backtest; this module only adapts HTTP.

Errors: invalid specs / unsatisfiable panels -> 422 problem+json with `type` = "urn:backtest:<code>" (invalid_spec,
spec, dataset). Lake reads are read-only; app_state holds strategies and runs.
"""

from __future__ import annotations

from typing import Any, Literal

import polars as pl
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.deps import FastRoute
from app.api.routers._common import not_found
from app.core.config import get_settings

router = APIRouter(tags=["backtest"], route_class=FastRoute)
PROBLEM = "application/problem+json"


def _problem(status: int, code: str, detail: str, errors: list[Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": f"urn:backtest:{code}", "title": "Backtest rejected" if status == 422 else "Error",
                            "status": status, "detail": detail}
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status, media_type=PROBLEM)


def _guard(fn: Any) -> Any:
    from app.backtest._panel import PanelError
    from app.modeling._core import SpecError

    try:
        return fn()
    except ValidationError as e:
        errs = [{"loc": [str(x) for x in er.get("loc", [])], "msg": er.get("msg", "")} for er in e.errors()]
        return _problem(422, "invalid_spec", "invalid backtest specification", errs)
    except (PanelError, SpecError) as e:
        return _problem(422, "dataset", str(e))
    except ValueError as e:
        return _problem(422, "spec", str(e))


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any]
    mode: Literal["auto", "sync", "async"] = "auto"
    strategy_id: str | None = None


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any]


class StrategyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    spec: dict[str, Any]
    tags: list[str] = Field(default_factory=list)
    origin: dict[str, Any] | None = None


class StrategyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    spec: dict[str, Any] | None = None
    tags: list[str] | None = None
    schedule: Literal["none", "weekly"] | None = None
    drift_threshold: float | None = Field(default=None, gt=0, le=10)


class StrategyRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["auto", "sync", "async"] = "auto"


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_ids: list[str] = Field(min_length=1, max_length=12)


class ScreenFilterIn(BaseModel):
    field: str
    op: str
    value: Any = None
    value2: float | None = None


class FromScreenRequest(BaseModel):
    """Body of POST /screener/query (subset) -> a draft backtest spec evaluated point-in-time."""
    universe_id: str = "full"
    regions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    sector_ids: list[str] = Field(default_factory=list)
    subsector_ids: list[str] = Field(default_factory=list)
    size_buckets: list[str] = Field(default_factory=list)
    min_market_cap_usd: float | None = None
    filters: list[ScreenFilterIn] = Field(default_factory=list)
    profile_id: str | None = None
    factor_weights: dict[str, float] | None = None
    sort_by: str = "composite"
    sort_dir: Literal["asc", "desc"] = "desc"
    limit: int = Field(50, ge=1, le=1000)
    columns: list[str] = Field(default_factory=list)
    offset: int = 0


# ------------------------------------------------------------------------------------------------ catalog / validate


@router.get("/backtest/catalog", operation_id="backtestCatalog")
def catalog() -> dict[str, Any]:
    from app.backtest.catalog import build

    return build()


@router.post("/backtest/validate", operation_id="backtestValidate")
def validate(body: ValidateRequest) -> Any:
    """Parse a spec, build its panel plan and estimate the run time (no data is read)."""
    from app.backtest.runs import SYNC_BUDGET_S, estimate_seconds
    from app.backtest.spec import BacktestSpec, default_benchmark, make_plan

    def go() -> Any:
        sp = BacktestSpec.model_validate(body.spec)
        plan, warns = make_plan(sp)
        from app.modeling import estimator, lane

        e = estimator.estimate_backtest(sp, path="sync")
        if e["run_s"] >= SYNC_BUDGET_S:
            on_lane = lane.available()
            e = estimator.estimate_backtest(sp, path="lane" if on_lane else "worker",
                                            queue_s=lane.queue_s() if on_lane else 0.0)
        est = e["total_s"]
        n_grid = 1
        for v in sp.robustness.sweep.values():
            n_grid *= len(v)
        return {"spec": sp.normalized(), "spec_hash": sp.spec_hash(), "warnings": warns, "estimate_s": est,
                "sync_budget_s": SYNC_BUDGET_S, "will_run": "sync" if e["path"] == "sync" else "async",
                "estimate": e, "prior_estimate_s": estimate_seconds(sp),
                "benchmark": default_benchmark(sp), "n_sweep": n_grid if sp.robustness.sweep else 0,
                "variables": [v["name"] for v in (plan.get("dataset") or {}).get("variables", [])],
                "extra_sources": [x["source"] for x in plan.get("extra") or []],
                "price_fields": plan.get("price_fields") or [], "delisted": (plan.get("delisted") or {}).get("enabled", False)}

    return _guard(go)


def _signal_from_field(fid: str, hib: bool) -> dict[str, Any] | None:
    from app.backtest.spec import ratio_metric_ids, screen_field_map

    f = screen_field_map().get(fid)
    if f is None and fid in ratio_metric_ids():
        f = {"kind": "studio", "source": "ratios", "field": fid}
    if f is None:
        return None
    if f["kind"] == "studio":
        return {"name": "sort", "source": f["source"], "field": f["field"], "higher_is_better": hib}
    if f["kind"] == "price":
        return {"name": "sort", "source": "price", "field": f["field"], "higher_is_better": hib}
    if f["kind"] == "extra":
        return {"name": "sort", "source": f["source"], "field": f["field"], "higher_is_better": hib}
    if f["kind"] == "derived" and f["needs"] == "composite":
        return {"name": "sort", "source": "composite_scores", "field": "balanced", "higher_is_better": fid != "rank" and hib}
    return None


def _scored_ids() -> list[str]:
    """Security ids with factor-score history (lake, read-only)."""
    from app.modeling.datasets import lake_connection

    try:
        con = lake_connection(["factor_scores"])
        try:
            return [r[0] for r in con.execute("SELECT DISTINCT security_id FROM factor_scores ORDER BY 1").fetchall()]
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - fall back to the universe as given
        return []


@router.post("/backtest/from-screen", operation_id="backtestFromScreen")
def from_screen(body: FromScreenRequest) -> Any:
    """Translate a Screener query into a draft backtest spec (screen rules evaluated point-in-time at each rebalance);
    fields without point-in-time history are reported in `dropped`."""
    from app.api import app_state
    from app.backtest.spec import (
        FACTORS,
        PROFILES,
        BacktestSpec,
        ratio_metric_ids,
        screen_field_map,
    )

    def go() -> Any:
        fmap = screen_field_map()
        ratios = ratio_metric_ids()
        dropped, notes, screen = [], [], []
        for f in body.filters:
            if f.field in fmap or f.field in ratios:
                screen.append({"field": f.field, "op": f.op, "value": f.value, "value2": f.value2})
            else:
                dropped.append(f.field)
        if body.size_buckets:
            screen.append({"field": "size_bucket", "op": "in", "value": list(body.size_buckets)})
        if body.subsector_ids:
            dropped.append("subsector_ids")
        from app.api.routers._common import taxonomy

        by_name = {v.lower(): k for k, v in taxonomy()["sectors"].items()}
        sectors = [by_name.get(x.lower(), x) for x in body.sector_ids]  # ids or display names
        uni: dict[str, Any] = {"universe_id": body.universe_id if body.universe_id != "watch" else None,
                               "regions": body.regions, "countries": [c.upper() for c in body.countries],
                               "sectors": sectors, "min_market_cap_usd": body.min_market_cap_usd, "screen": screen}
        if body.universe_id == "watch":
            ids = app_state.all_watch_security_ids()
            if not ids:
                return _problem(422, "spec", "the watch universe is empty")
            uni["security_ids"] = ids
            notes.append(f"watch universe: the {len(ids)} current watchlist names (today's membership)")
        hib = body.sort_dir == "desc"
        if body.factor_weights and any(w > 0 for w in body.factor_weights.values()):
            sig = {"combine": "zscore", "variables": [
                {"name": f"w_{k}", "source": "factor_scores", "field": k, "weight": float(w)}
                for k, w in body.factor_weights.items() if k in FACTORS and w > 0]}
            notes.append("signal: z-score blend of the custom factor weights")
        elif body.sort_by in ("composite", "grade", "rank", ""):
            prof = body.profile_id if body.profile_id in PROFILES else "balanced"
            sig = {"variables": [{"name": "composite", "source": "composite_scores", "field": prof,
                                  "higher_is_better": hib if body.sort_by == "composite" else True}]}
        else:
            v = _signal_from_field(body.sort_by, hib)
            if v is None:
                dropped.append(f"sort_by:{body.sort_by}")
                v = {"name": "composite", "source": "composite_scores", "field": "balanced"}
                notes.append("sort field has no point-in-time history: ranking on the balanced composite")
            sig = {"variables": [v]}
        score_based = all(v.get("source") in ("factor_scores", "composite_scores") for v in sig["variables"])
        if uni.get("universe_id") == "full" and score_based:
            # factor / composite scores exist only for the scored names (mvp_us + mvp_global): rank those instead of
            # truncating the 20k-name 'full' universe to max_entities
            scored = _scored_ids()
            if scored:
                uni.update(universe_id=None, security_ids=scored)
                notes.append(f"universe full -> the {len(scored)} names with factor-score history (scores cover the "
                             "mvp_us + mvp_global lists; today's list, see the survivorship caveat)")
        n = min(int(body.limit or 50), 100)
        spec = {"name": "Screen backtest", "universe": uni, "signal": sig,
                "construction": {"side": "long_only", "selection": "top_n", "top_n": n, "weighting": "equal",
                                 "rebalance": "M"}}
        sp = BacktestSpec.model_validate(spec)
        notes.append(f"holds the top {n} names passing the screen each month (equal weight), like the screener's first page")
        return {"spec": sp.normalized(), "dropped": dropped, "notes": notes}

    return _guard(go)


# ------------------------------------------------------------------------------------------------ runs


def _payload(run_id: str, include_result: bool = True) -> dict[str, Any] | None:
    from app.backtest import runs, store

    st = store.sync_run(run_id)
    if st is None:
        return None
    out: dict[str, Any] = {"run": store.run_out(st), "result": None, "reproducibility": None, "spec": None,
                           "preview": None}
    if include_result and st.get("status") in runs.TERMINAL:
        env = runs.read_result(run_id) or {}
        out.update(result=env.get("result"), reproducibility=env.get("reproducibility"), spec=env.get("spec"))
    else:
        doc = runs.read_spec(run_id) or {}
        out["spec"] = doc.get("spec")
        pv = st.get("preview")
        if isinstance(pv, dict) and pv.get("status") == "completed":
            out["preview"] = runs.read_preview(run_id)  # S1.3: result without the heavy robustness part
    return out


def _submit(spec_d: dict[str, Any], mode: str, strategy_id: str | None) -> Any:
    from app.backtest import runs, store
    from app.backtest.spec import BacktestSpec

    sp = BacktestSpec.model_validate(spec_d)
    prior = store.prior_trials(strategy_id, sp.spec_hash())
    code, st = runs.submit(sp, mode, strategy_id, prior=prior)
    store.upsert_run(st, sp.normalized())
    payload = _payload(st["run_id"], include_result=code == 200)
    return payload if code == 200 else JSONResponse(payload, status_code=202)


@router.post("/backtest/runs", operation_id="backtestSubmitRun")
def submit_run(body: RunRequest) -> Any:
    from app.backtest import store

    def go() -> Any:
        if body.strategy_id and store.get_strategy(body.strategy_id) is None:
            raise not_found(f"strategy {body.strategy_id} not found")
        return _submit(body.spec, body.mode, body.strategy_id)

    return _guard(go)


@router.get("/backtest/runs", operation_id="backtestListRuns")
def list_runs(strategy_id: str | None = None, limit: int = Query(100, ge=1, le=500)) -> list[dict[str, Any]]:
    from app.backtest import store

    return store.list_runs(strategy_id, limit)


@router.get("/backtest/runs/{run_id}", operation_id="backtestGetRun")
def get_run(run_id: str, series: str = Query("full", pattern="^(full|weekly|none)$")) -> dict[str, Any]:
    p = _payload(run_id)
    if p is None:
        raise not_found(f"run {run_id} not found")
    if series != "full" and isinstance(p.get("result"), dict) and isinstance(p["result"].get("series"), dict):
        p["result"] = {**p["result"], "series": thin_series(p["result"]["series"], series)}
    return p


def thin_series(sr: dict[str, Any], how: str) -> dict[str, Any]:
    """v1.30 (F30): weekly = last observation of each ISO week + the final day; none = empty arrays."""
    dates = list(sr.get("dates") or [])
    if how == "none":
        keep: list[int] = []
    else:
        from datetime import date as _d

        keep = []
        for i, d in enumerate(dates):
            try:
                wk = _d.fromisoformat(str(d)[:10]).isocalendar()[:2]
                nxt = _d.fromisoformat(str(dates[i + 1])[:10]).isocalendar()[:2] if i + 1 < len(dates) else None
            except ValueError:
                wk, nxt = None, None
            if nxt is None or wk != nxt:
                keep.append(i)
    out: dict[str, Any] = {}
    for k, v in sr.items():
        out[k] = [v[i] for i in keep if i < len(v)] if isinstance(v, list) and len(v) == len(dates) else ([] if how == "none" and isinstance(v, list) else v)
    return out


@router.delete("/backtest/runs/{run_id}", operation_id="backtestCancelRun")
def cancel_run(run_id: str) -> dict[str, Any]:
    from app.backtest import runs, store

    st = runs.cancel(run_id)
    if st is None:
        raise not_found(f"run {run_id} not found")
    store.upsert_run(st)
    return store.run_out(st)


def _frame(run_id: str, name: str) -> pl.DataFrame | None:
    from app.backtest import runs

    try:
        p = runs.run_dir(run_id) / f"{name}.parquet"
    except ValueError:
        return None
    return pl.read_parquet(p) if p.exists() else None


def _rows(df: pl.DataFrame) -> list[dict[str, Any]]:
    out = []
    for r in df.iter_rows(named=True):
        out.append({k: (v.isoformat() if hasattr(v, "isoformat") else (None if isinstance(v, float) and v != v else v))
                    for k, v in r.items()})
    return out


@router.get("/backtest/runs/{run_id}/holdings", operation_id="backtestRunHoldings")
def run_holdings(run_id: str, date: str | None = Query(None, description="rebalance date (default latest)"),
                 limit: int = Query(500, ge=1, le=5000)) -> dict[str, Any]:
    from app.backtest import runs

    if runs.read_status(run_id) is None:
        raise not_found(f"run {run_id} not found")
    df = _frame(run_id, "holdings")
    if df is None or df.is_empty():
        return {"run_id": run_id, "dates": [], "date": None, "total": 0, "rows": []}
    dates = sorted({d.isoformat() for d in df["date"].unique().to_list()})
    d = date if date in dates else dates[-1]
    sub = df.filter(pl.col("date").cast(pl.Utf8) == d).sort("weight", descending=True)
    return {"run_id": run_id, "dates": dates, "date": d, "total": sub.height, "rows": _rows(sub.head(limit))}


@router.get("/backtest/runs/{run_id}/trades", operation_id="backtestRunTrades")
def run_trades(run_id: str, entity_id: str | None = None, limit: int = Query(500, ge=1, le=5000),
               offset: int = Query(0, ge=0)) -> dict[str, Any]:
    from app.backtest import runs

    if runs.read_status(run_id) is None:
        raise not_found(f"run {run_id} not found")
    df = _frame(run_id, "trades")
    if df is None or df.is_empty():
        return {"run_id": run_id, "total": 0, "limit": limit, "offset": offset, "kind": None, "rows": []}
    kind = "round_trip" if "exit_date" in df.columns else "rebalance"
    if entity_id:
        df = df.filter(pl.col("entity_id") == entity_id)
    sort_col = "entry_date" if kind == "round_trip" else "date"
    df = df.sort(sort_col, descending=True)
    return {"run_id": run_id, "total": df.height, "limit": limit, "offset": offset, "kind": kind,
            "rows": _rows(df.slice(offset, limit))}


@router.get("/backtest/runs/{run_id}/export.py", operation_id="backtestExportRun")
def export_run(run_id: str) -> Response:
    from app.backtest import runs
    from app.backtest.export import export_script

    env = runs.read_result(run_id)
    if env is None:
        raise not_found(f"run {run_id} has no result")
    if env.get("status") != "completed":
        return _problem(409, "not_completed", f"run {run_id} is {env.get('status')}: only completed runs export")
    src = export_script(env, get_settings().data_dir)
    return PlainTextResponse(src, media_type="text/x-python",
                             headers={"Content-Disposition": f'attachment; filename="backtest_{run_id}.py"'})


@router.post("/backtest/compare", operation_id="backtestCompareRuns")
def compare_runs(body: CompareRequest) -> dict[str, Any]:
    from app.backtest import store

    return store.compare(body.run_ids)


# ------------------------------------------------------------------------------------------------ strategies


@router.get("/backtest/strategies", operation_id="backtestListStrategies")
def list_strategies() -> list[dict[str, Any]]:
    from app.backtest import store

    return store.list_strategies()


@router.post("/backtest/strategies", operation_id="backtestCreateStrategy", status_code=201)
def create_strategy(body: StrategyCreate) -> Any:
    from app.backtest import store
    from app.backtest.spec import BacktestSpec, make_plan

    def go() -> Any:
        sp = BacktestSpec.model_validate(body.spec)
        make_plan(sp)
        return JSONResponse(store.create_strategy(body.name, sp, body.description, body.tags, body.origin), status_code=201)

    return _guard(go)


@router.get("/backtest/strategies/{strategy_id}", operation_id="backtestGetStrategy")
def get_strategy(strategy_id: str) -> dict[str, Any]:
    from app.backtest import store

    st = store.get_strategy(strategy_id)
    if st is None:
        raise not_found(f"strategy {strategy_id} not found")
    return {"strategy": st, "runs": store.list_runs(strategy_id)}


@router.patch("/backtest/strategies/{strategy_id}", operation_id="backtestUpdateStrategy")
def update_strategy(strategy_id: str, body: StrategyUpdate) -> Any:
    from app.backtest import store
    from app.backtest.spec import BacktestSpec, make_plan

    def go() -> Any:
        if store.get_strategy(strategy_id) is None:
            raise not_found(f"strategy {strategy_id} not found")
        fields = body.model_dump(exclude_unset=True)
        sched = fields.pop("schedule", None)
        thr = fields.pop("drift_threshold", None)
        if fields.get("spec") is not None:
            fields["spec"] = BacktestSpec.model_validate(fields["spec"])
            make_plan(fields["spec"])
        if sched is not None or thr is not None:
            cur = store.get_strategy(strategy_id) or {}
            store.set_schedule(strategy_id, sched or cur.get("schedule") or "none", thr)
        out = store.update_strategy(strategy_id, fields)
        if out is None:
            raise not_found(f"strategy {strategy_id} not found")
        return out

    return _guard(go)


@router.delete("/backtest/strategies/{strategy_id}", operation_id="backtestDeleteStrategy", status_code=204)
def delete_strategy(strategy_id: str) -> Response:
    from app.backtest import store

    if not store.delete_strategy(strategy_id):
        raise not_found(f"strategy {strategy_id} not found")
    return Response(status_code=204)


@router.post("/backtest/strategies/{strategy_id}/run", operation_id="backtestRunStrategy")
def run_strategy(strategy_id: str, body: StrategyRunRequest | None = None) -> Any:
    from app.backtest import store

    st = store.get_strategy(strategy_id)
    if st is None:
        raise not_found(f"strategy {strategy_id} not found")
    mode = body.mode if body is not None else "auto"
    return _guard(lambda: _submit(st["spec"], mode, strategy_id))
