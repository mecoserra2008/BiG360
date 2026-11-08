"""Model Lab (tag: models): model registry, run detail (IC series, deciles, folds, importance, benchmark), promotion,
training requests (enqueued to the worker) and the prediction cross-section."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.api import app_state, pricebars
from app.api.deps import FastRoute, Db, get_db, response_cache
from app.api.routers._common import (
    conflict,
    fnum,
    invalid,
    iso,
    jload,
    not_found,
    parse_regions,
    region_clause,
    regions_query,
    rnd,
    sec_ref,
    sec_ref_sql,
)
from app.api.routers.system import trigger

router = APIRouter(tags=["models"], route_class=FastRoute)

ModelType = Literal["xgboost_rank", "ft_transformer", "cs_attention", "ensemble", "insider_xgb", "insider_iforest", "macro_hmm"]
Horizon = Literal["1M", "3M", "6M"]
TRAIN_JOB = {"xgboost_rank": "ml_train_xgb", "ft_transformer": "ml_train_transformer", "cs_attention": "ml_train_transformer",
             "ensemble": "ml_ensemble", "insider_xgb": "insider_models_fit", "insider_iforest": "insider_models_fit",
             "macro_hmm": "macro_regime_fit"}


def _promoted() -> set[str]:
    try:
        return {p["model_run_id"] for p in app_state.model_promotions()}
    except Exception:
        return set()


#: ranking model types that must pass the label-permutation leakage check to be production-eligible (app.ml.ensemble)
_LEAKAGE_GATED = {"ft_transformer", "cs_attention", "ensemble"}
_LEAKAGE_PASS_MARK = "[LEAKAGE_CHECK] passed"


def leakage_gate(r: dict[str, Any]) -> tuple[str | None, str | None]:
    """(status, note) of the label-permutation leakage gate for ranking models (v1.39.0); (None, None) otherwise."""
    mt = r.get("model_type")
    if mt == "xgboost_rank":
        return "passed", "label-permutation leakage test passed (docs/PHASE3_TODO.md)"
    if mt in _LEAKAGE_GATED:
        from app.ml.ensemble import leakage_check_passed  # noqa: PLC0415

        if leakage_check_passed(r.get("notes")):
            return "passed", "label-permutation leakage check recorded as passed in the run notes"
        why = ("blends FT-Transformer predictions, which failed the label-permutation leakage test" if mt == "ensemble"
               else "FT-Transformer architecture failed the label-permutation leakage test (shuffled-label rank IC "
                    "+0.06-0.09; docs/PHASE3_TODO.md)")
        return "failed", f"DEMOTED: {why}; comparison only, not production-eligible"
    return None, None


def run_out(r: dict[str, Any], promoted: set[str] | None = None, prod_ids: set[str] | None = None) -> dict[str, Any]:
    fi = jload(r.get("feature_importance"), []) or []
    params = jload(r.get("params"), {}) or {}
    n_feat = params.get("n_features") if isinstance(params, dict) else None
    is_prod = bool(r.get("is_production")) or r["model_run_id"] in (promoted or set()) or r["model_run_id"] in (prod_ids or set())
    return {
        "model_run_id": r["model_run_id"], "model_type": r["model_type"], "horizon": r.get("horizon"),
        "universe_id": r.get("universe_id") or "", "train_start": iso(r.get("train_start")), "train_end": iso(r.get("train_end")),
        "oos_start": iso(r.get("oos_start")), "oos_end": iso(r.get("oos_end")),
        "cv_scheme": jload(r.get("cv_scheme"), {}) or {}, "params": params if isinstance(params, dict) else {},
        **{k: fnum(r.get(k)) for k in ("ic_mean", "rank_ic_mean", "rank_ic_ir", "rank_ic_tstat", "decile_spread_ann",
                                       "long_short_sharpe", "turnover_monthly", "hit_rate")},
        "status": r["status"], "is_production": is_prod, "created_at": iso(r["created_at"]),
        "finished_at": iso(r.get("finished_at")), "feature_set_version": r.get("feature_set_version"),
        "n_features": int(n_feat) if isinstance(n_feat, (int, float)) else (len(fi) if isinstance(fi, list) and fi else None),
        "leakage_gate": leakage_gate(r)[0], "gate_note": leakage_gate(r)[1],
    }


@router.get("/models/runs", operation_id="listModelRuns")
def list_model_runs(model_type: ModelType | None = None, horizon: Horizon | None = None,
                    status: Literal["running", "completed", "failed", "archived"] | None = None,
                    db: Db = Depends(get_db)) -> list[dict[str, Any]]:
    conds, params = ["TRUE"], []
    for col, v in (("model_type", model_type), ("horizon", horizon), ("status", status)):
        if v:
            conds.append(f"{col} = ?")
            params.append(v)
    rows = db.all(f"SELECT * FROM model_runs WHERE {' AND '.join(conds)} ORDER BY created_at DESC", params)
    prom = _promoted()
    return [run_out(r, prom) for r in rows]


def _series(db: Db, run_id: str, metric: str) -> list[dict[str, Any]]:
    return [{"date": iso(r["date"]), "value": fnum(r["value"])} for r in db.all(
        "SELECT date, avg(value) AS value FROM model_eval WHERE model_run_id = ? AND metric = ? AND date > DATE '1901-01-01' "
        "AND bucket = '' GROUP BY 1 ORDER BY 1", [run_id, metric])]


@router.get("/models/runs/{model_run_id}", operation_id="getModelRun")
def get_model_run(model_run_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = db.one("SELECT * FROM model_runs WHERE model_run_id = ?", [model_run_id])
    if r is None:
        raise not_found(f"unknown model_run_id {model_run_id!r}")
    dec = db.all("""SELECT TRY_CAST(bucket AS INTEGER) AS decile, avg(value) AS m FROM model_eval
                    WHERE model_run_id = ? AND metric = 'decile_return' AND TRY_CAST(bucket AS INTEGER) IS NOT NULL
                    GROUP BY 1 ORDER BY 1""", [model_run_id])
    folds = db.all("""SELECT bucket AS fold, max(date) AS end_date, avg(value) AS v FROM model_eval
                      WHERE model_run_id = ? AND metric = 'fold_rank_ic' GROUP BY 1 ORDER BY 2""", [model_run_id])
    cv = jload(r.get("cv_scheme"), {}) or {}
    fold_meta = {}
    for f in (cv.get("folds") or []) if isinstance(cv, dict) else []:
        if isinstance(f, dict):
            fold_meta[str(f.get("fold", f.get("id", "")))] = f
    # F28: per-fold windows / Sharpe are persisted in model_runs.fold_metrics (app.ml.train, app.ml.ensemble)
    fm = jload(r.get("fold_metrics"), []) or []
    for f in fm if isinstance(fm, list) else []:
        if isinstance(f, dict) and f.get("fold_id") is not None:
            fold_meta.setdefault(str(f["fold_id"]), f)
    if not folds and fold_meta:  # runs without fold_rank_ic rows in model_eval (ensemble): folds from the JSON
        folds = [{"fold": k, "end_date": v.get("test_end"), "v": v.get("rank_ic")}
                 for k, v in sorted(fold_meta.items(), key=lambda kv: str(kv[1].get("test_end") or ""))]
    sect = db.all("""SELECT bucket, avg(value) AS v FROM model_eval WHERE model_run_id = ? AND metric = 'rank_ic'
                     AND bucket <> '' GROUP BY 1 ORDER BY 1""", [model_run_id])
    fi = jload(r.get("feature_importance"), []) or []
    cls = _series(db, model_run_id, "cum_long_short")
    bench: list[dict[str, Any]] = []
    if cls:
        d0, d1 = date.fromisoformat(cls[0]["date"]), date.fromisoformat(cls[-1]["date"])
        bid = "IDX:^GSPC"  # benchmark for the long-short curve (S&P 500; universes are US-anchored at MVP)
        # serving `prices` holds 15y of daily bars; older history is in `prices_w` (weekly): pricebars merges both so a
        # long-short curve starting before the daily window gets a benchmark from its first date
        px = [{"date": b["date"], "adj_close": b["c"]} for b in pricebars.bars(db, bid, d0, d1, "D", adjusted=True)]
        if px:
            want = {c["date"] for c in cls}
            base = fnum(px[0]["adj_close"])
            bench = [{"date": iso(p["date"]), "value": rnd(fnum(p["adj_close"]) / base - 1, 6) if base else None}
                     for p in px if iso(p["date"]) in want] or \
                    [{"date": iso(p["date"]), "value": rnd(fnum(p["adj_close"]) / base - 1, 6) if base else None} for p in px]
    run = run_out(r, _promoted())
    return {
        "run": run, "ic_series": _series(db, model_run_id, "ic"), "rank_ic_series": _series(db, model_run_id, "rank_ic"),
        "cum_long_short": cls,
        "decile_returns": [{"decile": int(x["decile"]), "mean_excess_return": fnum(x["m"]) or 0.0} for x in dec],
        "fold_metrics": [{"fold": str(f["fold"]), "end_date": iso(f["end_date"]), "rank_ic": fnum(f["v"]),
                          "train_start": iso(fold_meta.get(str(f["fold"]), {}).get("train_start")),
                          "train_end": iso(fold_meta.get(str(f["fold"]), {}).get("train_end")),
                          "test_start": iso(fold_meta.get(str(f["fold"]), {}).get("test_start")),
                          "test_end": iso(fold_meta.get(str(f["fold"]), {}).get("test_end") or f["end_date"]),
                          "sharpe": fnum(fold_meta.get(str(f["fold"]), {}).get("sharpe"))} for f in folds],
        "feature_importance": [{"feature": str(x.get("feature")), "family": x.get("family") or
                                (str(x.get("feature")).removeprefix("f_").split("_")[0] if "_" in str(x.get("feature")) else None),
                                "importance": fnum(x.get("importance")) or 0.0}
                               for x in (fi if isinstance(fi, list) else []) if isinstance(x, dict) and x.get("feature")],
        "sector_rank_ic": {s["bucket"]: rnd(s["v"], 6) for s in sect},
        "benchmark_series": bench, "n_features": run["n_features"], "data_status": "ok",
    }


@router.post("/models/runs/{model_run_id}/promote", operation_id="promoteModelRun")
def promote_model_run(model_run_id: str, db: Db = Depends(get_db)) -> dict[str, Any]:
    r = db.one("SELECT * FROM model_runs WHERE model_run_id = ?", [model_run_id])
    if r is None:
        raise not_found(f"unknown model_run_id {model_run_id!r}")
    if r["status"] != "completed":
        raise conflict(f"model run {model_run_id} has status {r['status']!r}; only completed runs can be promoted")
    app_state.promote_model(model_run_id, r["model_type"], r.get("horizon"))
    response_cache.clear()
    return run_out(r, {model_run_id})


class TrainRequest(BaseModel):
    model_type: ModelType
    horizon: Horizon
    universe_id: str = "full"
    params: dict[str, Any] = Field(default_factory=dict)


@router.post("/models/train", operation_id="trainModel", status_code=202)
def train_model(req: TrainRequest) -> JSONResponse:
    job = TRAIN_JOB.get(req.model_type)
    if job is None:
        raise invalid(f"no training job for model_type {req.model_type!r}")
    params = {**req.params, "model_type": req.model_type, "horizon": req.horizon, "universe_id": req.universe_id}
    return trigger(job, params)


@router.get("/models/predictions", operation_id="listPredictions")
def list_predictions(horizon: Horizon = "3M", model_run_id: str | None = None, date_: date | None = Query(None, alias="date"),
                     region: str | None = None, regions: list[str] | None = regions_query(), sector_id: str | None = None,
                     limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                     db: Db = Depends(get_db)) -> dict[str, Any]:
    mid = model_run_id or db.scalar("SELECT model_run_id FROM ml_prod WHERE horizon = ?", [horizon])
    if not mid:
        return {"model_run_id": "", "horizon": horizon, "date": iso(date_ or db.as_of), "total": 0, "limit": limit,
                "offset": offset, "items": [], "data_status": "pending" if not db.snap.input_ok("ml_predictions") else "ok"}
    d = date_ or db.scalar("SELECT max(date) FROM ml_pred WHERE model_run_id = ? AND horizon = ?", [mid, horizon])
    regs = parse_regions(region, regions)
    rc, rp = region_clause(regs)
    conds = [rc]
    if sector_id:
        conds.append("s.sector_id = ?")
        rp.append(sector_id)
    rows = db.all(f"""SELECT {sec_ref_sql('s')}, s.c_balanced, p.score, p.rank_pct, p.decile, count(*) OVER () AS _total
                      FROM ml_pred p JOIN sec s USING (security_id)
                      WHERE p.model_run_id = ? AND p.horizon = ? AND p.date = ? AND {' AND '.join(conds)}
                      ORDER BY p.rank_pct DESC, s.security_id LIMIT ? OFFSET ?""", [mid, horizon, d] + rp + [limit, offset])
    total = int(rows[0]["_total"]) if rows else 0
    return {"model_run_id": mid, "horizon": horizon, "date": iso(d or db.as_of), "total": total, "limit": limit,
            "offset": offset,
            "items": [{"security": sec_ref(r), "score": fnum(r["score"]), "rank_pct": fnum(r["rank_pct"]) or 0.0,
                       "decile": int(r["decile"] or 0), "composite": fnum(r["c_balanced"])} for r in rows],
            "data_status": "ok"}
