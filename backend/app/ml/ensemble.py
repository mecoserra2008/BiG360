"""Rank-average ensemble of the latest XGBoost + Transformer runs, and the production-model gate. Job
`ml_ensemble` (weekly, depends_on: [ml_train_xgb, ml_train_transformer]). Owner: ml.

Blend: for each (date, security_id) present in BOTH models' prediction sets, `ensemble_rank_pct = mean(rank_pct)`
of the two source models (a rank-average, not a stacked/learned blend -- robust with only two base models and no
risk of overfitting blend weights to the walk-forward OOS sample). Evaluated the same way as a standalone model
(`app.ml.evaluate`), including a full re-run of `evaluate_predictions` on the blended OOS set.

Gate (brief: "the Transformer only becomes production if it beats XGBoost OOS rank-IC; otherwise
ensemble/XGBoost wins"): among the latest completed run per model_type for this horizon (xgboost_rank,
ft_transformer if present, ensemble), the one with the highest `rank_ic_mean` is marked
`model_runs.is_production = true`; the others are set to `false`. Exactly one production model per horizon
across model types (the API / `scoring_ml_blend` consume "the production model" as a single choice per
contracts/scoring.yaml, not per model_type), decision recorded in `model_runs.notes`.

**Second gate, added after a 2026-10-11 orchestrator-requested leakage audit (see `tests/ml/test_leakage.py` and
`model_runs.notes` on the demoted 3M run): a non-xgboost model_type (`ft_transformer`, `ensemble`) must also
pass `app.ml.train.run_permutation_leakage_check` (|rank_IC| < 0.01 when trained on labels permuted within each
date) before it may be promoted.** This is NOT automated inside `apply_production_gate` yet (the Transformer's
permutation check costs real training minutes, too slow to run unconditionally on every weekly gate call);
until it is, promoting a non-xgboost model_type is a manual step that must be preceded by running that check
by hand and recording the result in the run's `notes`. `apply_production_gate` as written will happily pick
whichever model_type has the highest `rank_ic_mean` with NO automatic check of this gate -- the 3M
`ft_transformer` run that triggered this audit (rank-IC 0.143) failed the permutation test (consistent
+0.06-0.09 IC against permuted-label training, not noise) and was demoted by hand; xgboost_rank is production
for all three horizons as of that date.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, write_table
from app.ml import evaluate
from app.ml.labels import HORIZONS
from app.scheduler.base import new_run_id

log = get_logger(__name__)
BASE_MODEL_TYPES = ("xgboost_rank", "ft_transformer")
#: a non-xgboost run is eligible for production only when its notes record a passed label-permutation leakage
#: check (app.ml.train.run_permutation_leakage_check, run by hand) - the second gate described above, now enforced
LEAKAGE_PASS_MARK = "[LEAKAGE_CHECK] passed"
ALWAYS_ELIGIBLE = ("xgboost_rank",)
#: the pass mark must stand on its own: a quoted mention (the gate's own "not eligible: no '[LEAKAGE_CHECK] passed'
#: recorded" note, written before 2026-10-30) is NOT a pass. The old substring test matched that note and re-promoted
#: the leaky FT-Transformer for 1M / 3M on 2026-10-25 (found by the full-scope audit 2026-10-30).
_PASS_RE = re.compile(r"(?<!['\"])\[LEAKAGE_CHECK\] passed(?!['\"])")


def leakage_check_passed(notes: str | None) -> bool:
    """True when the run notes record a passed label-permutation leakage check (a standalone pass mark)."""
    return bool(_PASS_RE.search(notes or ""))


def gate_eligible(model_type: str, row: dict[str, Any]) -> bool:
    return model_type in ALWAYS_ELIGIBLE or leakage_check_passed(row.get("notes"))


def ensemble_fold_metrics(oos_eval: pl.DataFrame, base_folds: list[dict[str, Any]], horizon: str) -> list[dict[str, Any]]:
    """F28: the blend has no folds of its own, but its OOS rows inherit the xgboost base model's walk-forward test
    windows: rank IC / long-short Sharpe of the blend per base fold window (same fields as app.ml.train)."""
    out = []
    for f in base_folds or []:
        try:
            lo, hi = pl.lit(f["test_start"]).str.to_date(), pl.lit(f["test_end"]).str.to_date()
        except Exception:  # noqa: BLE001
            continue
        win = oos_eval.filter((pl.col("date") >= lo) & (pl.col("date") <= hi)).drop_nulls(["score", "fwd_excess_sector"])
        if win.is_empty():
            continue
        s = evaluate.evaluate_predictions(win, horizon)["summary"]
        out.append({"fold_id": f.get("fold_id"), "train_start": f.get("train_start"), "train_end": f.get("train_end"),
                    "test_start": f.get("test_start"), "test_end": f.get("test_end"), "n_train": f.get("n_train"),
                    "n_test": win.height, "rank_ic": s["rank_ic_mean"], "sharpe": s["long_short_sharpe"],
                    "seconds": None, "source": "blend OOS on the xgboost_rank base folds"})
    return out


def inherited_importance(xgb_row: dict[str, Any], weight: float = 0.5) -> list[dict[str, Any]] | None:
    """F28: the rank-average blend's feature importance = the xgboost base model's mean |SHAP| x its blend weight
    (the transformer base has no importance attribution); every entry is labelled with its source."""
    try:
        fi = json.loads(xgb_row.get("feature_importance") or "null")
    except ValueError:
        return None
    if not isinstance(fi, list) or not fi:
        return None
    return [{**x, "importance": (x.get("importance") or 0.0) * weight,
             "source": f"xgboost_rank base {xgb_row.get('model_run_id')} x {weight:g} (rank-average weight)"}
            for x in fi if isinstance(x, dict)]


def _latest_run(horizon: str, model_type: str, settings: Settings) -> dict[str, Any] | None:
    mr = read_table("model_runs", settings=settings).filter(
        (pl.col("horizon") == horizon) & (pl.col("model_type") == model_type) & (pl.col("status") == "completed"))
    if mr.is_empty():
        return None
    return mr.sort("created_at").tail(1).to_dicts()[0]


def _predictions(model_run_id: str, settings: Settings) -> pl.DataFrame:
    return read_table("ml_predictions", settings=settings).filter(pl.col("model_run_id") == model_run_id)


def _blend(a: pl.DataFrame, b: pl.DataFrame) -> pl.DataFrame:
    j = a.select("date", "security_id", pl.col("rank_pct").alias("rp_a")).join(
        b.select("date", "security_id", pl.col("rank_pct").alias("rp_b")), on=["date", "security_id"], how="inner")
    return j.with_columns(((pl.col("rp_a") + pl.col("rp_b")) / 2.0).alias("score"))


def run_ensemble_horizon(horizon: str, settings: Settings) -> dict[str, Any]:
    runs = {mt: _latest_run(horizon, mt, settings) for mt in BASE_MODEL_TYPES}
    present = {mt: r for mt, r in runs.items() if r is not None}
    if len(present) < 2:
        return {"status": "skipped", "message": f"need both {BASE_MODEL_TYPES} trained for {horizon}; have {list(present)}"}
    preds = {mt: _predictions(r["model_run_id"], settings) for mt, r in present.items()}
    oos = {mt: p.filter(pl.col("is_oos")) for mt, p in preds.items()}
    live = {mt: p.filter(pl.col("is_live")) for mt, p in preds.items()}
    oos_blend = _blend(oos["xgboost_rank"], oos["ft_transformer"])
    live_blend = _blend(live["xgboost_rank"], live["ft_transformer"])
    lab = read_table("ml_labels", settings=settings).filter(pl.col("horizon") == horizon).select(
        "date", "security_id", "fwd_return", "fwd_excess_sector", "rank_label")
    oos_eval = oos_blend.join(lab, on=["date", "security_id"], how="left")
    ev = evaluate.evaluate_predictions(oos_eval.drop_nulls(["score", "fwd_excess_sector"]), horizon)

    run_id = new_run_id("M")
    now = datetime.now(UTC)

    def _finish_oos(df: pl.DataFrame) -> pl.DataFrame:
        d = df.with_columns((pl.col("score") * 10).ceil().clip(1, 10).cast(pl.Int8).alias("decile"))
        return d.select(
            pl.lit(run_id).alias("model_run_id"), "date", "security_id", pl.lit(horizon).alias("horizon"),
            "score", pl.col("score").alias("rank_pct"), "decile", pl.lit(True).alias("is_oos"),
            pl.lit(False).alias("is_live"), pl.lit(None, dtype=pl.Utf8).alias("shap_top"),
            pl.lit(None, dtype=pl.Float64).alias("expected_excess_return"),
            pl.lit(None, dtype=pl.Float64).alias("interval_low"), pl.lit(None, dtype=pl.Float64).alias("interval_high"),
            pl.lit(None, dtype=pl.Float64).alias("confidence"), pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("trained_at"),
            pl.lit(None, dtype=pl.Utf8).alias("feature_set_version"), pl.lit(now).alias("computed_at"))

    n_pred = write_table(_finish_oos(oos_blend), "ml_predictions", settings=settings)
    series_df = evaluate.series_rows_to_frame(ev["series"], run_id)
    n_series = write_table(series_df, "model_eval_series", settings=settings)

    base_runs = {mt: r["model_run_id"] for mt, r in present.items()}
    cv_scheme = {"type": "rank_average_ensemble", "base_models": base_runs}
    try:
        base_folds = json.loads(present["xgboost_rank"].get("fold_metrics") or "[]")
    except ValueError:
        base_folds = []
    fold_metrics = ensemble_fold_metrics(oos_eval, base_folds, horizon)
    importance = inherited_importance(present["xgboost_rank"])
    row = pl.DataFrame([{
        "model_run_id": run_id, "model_type": "ensemble", "horizon": horizon,
        "universe_id": present["xgboost_rank"]["universe_id"],
        "feature_set_version": present["xgboost_rank"]["feature_set_version"],
        "train_start": present["xgboost_rank"]["train_start"], "train_end": present["xgboost_rank"]["train_end"],
        "oos_start": present["xgboost_rank"]["oos_start"], "oos_end": present["xgboost_rank"]["oos_end"],
        "cv_scheme": json.dumps(cv_scheme), "params": json.dumps({"weights": "equal_rank_average"}),
        "ic_mean": ev["summary"]["ic_mean"], "rank_ic_mean": ev["summary"]["rank_ic_mean"],
        "rank_ic_ir": ev["summary"]["rank_ic_ir"], "rank_ic_tstat": ev["summary"]["rank_ic_tstat"],
        "decile_spread_ann": ev["summary"]["decile_spread_ann"], "long_short_sharpe": ev["summary"]["long_short_sharpe"],
        "turnover_monthly": ev["summary"]["turnover_monthly"], "hit_rate": ev["summary"]["hit_rate"],
        "status": "completed", "is_production": False, "artifact_path": None,
        # F28: labelled inherited importance + blend metrics on the base model's fold windows (no folds of its own)
        "feature_importance": json.dumps(importance) if importance else None,
        "fold_metrics": json.dumps(fold_metrics) if fold_metrics else None,
        "notes": (f"Rank-average of {base_runs}. {ev['method_note']} Feature importance is the xgboost_rank base "
                  f"model's mean |SHAP| x 0.5 (the transformer base has none); fold metrics are the blend's OOS on "
                  f"the xgboost base folds."), "created_at": now, "finished_at": now,
    }])
    write_table(row, "model_runs", settings=settings)
    model_run_dict = row.to_dicts()[0]
    try:
        from app.ml import predict as P  # noqa: PLC0415 - avoids importing predict at ensemble module load

        calibrated = P.calibrate_live(run_id, live_blend.select("date", "security_id", "score"), settings)
        n_pred += P.write_live(model_run_dict, calibrated, settings)
    except Exception as e:  # noqa: BLE001 - live calibration is best-effort, OOS results already committed
        log.warning("ensemble live calibration/write failed for %s: %s", run_id, e)
    present["ensemble"] = model_run_dict
    decision = apply_production_gate(horizon, present, settings)
    return {"status": "success", "model_run_id": run_id, "rows": {"ml_predictions": n_pred, "model_eval_series": n_series},
           "summary": ev["summary"], "decision": decision}


def apply_production_gate(horizon: str, candidates: dict[str, dict[str, Any]], settings: Settings) -> dict[str, Any]:
    """candidates: model_type -> its model_runs row (dict). Flags the single best rank_ic_mean among the ELIGIBLE runs
    as is_production (non-xgboost runs need a recorded passed leakage check, see LEAKAGE_PASS_MARK); ineligible
    runs rank after every eligible one and are never promoted."""
    scored = [(mt, r, (r.get("rank_ic_mean") if r.get("rank_ic_mean") is not None else float("-inf")))
             for mt, r in candidates.items()]
    scored.sort(key=lambda t: (gate_eligible(t[0], t[1]), t[2]), reverse=True)
    if not gate_eligible(scored[0][0], scored[0][1]):
        log.warning("production gate %s: no eligible candidate (%s); keeping the ranking but nothing is promoted",
                    horizon, [m for m, _, _ in scored])
    winner_type, winner_row, winner_ic = scored[0]
    rows = []
    now = datetime.now(UTC)
    winner_ok = gate_eligible(winner_type, winner_row)
    for mt, r, ic in scored:
        r = dict(r)
        r["is_production"] = (mt == winner_type) and winner_ok
        if not gate_eligible(mt, r) and "[GATE] not eligible" not in (r.get("notes") or ""):
            r["notes"] = (r.get("notes") or "") + (" [GATE] not eligible for production: no passed label-permutation "
                                                   "leakage check recorded (run by hand).")
        if mt == winner_type and winner_ok:
            r["notes"] = (r.get("notes") or "") + (
                f" [GATE] production model for horizon={horizon}: {mt} (rank_ic_mean={ic:.4f})."
                f" Runner-up(s): {[(m, round(i, 4)) for m, _, i in scored[1:]]}")
        r["finished_at"] = now
        rows.append(r)
    # exactly one production run per horizon: older runs still flagged (e.g. a demoted transformer) are cleared
    keep = {r["model_run_id"] for r in rows}
    try:
        stale = read_table("model_runs", settings=settings).filter(
            (pl.col("horizon") == horizon) & pl.col("is_production").fill_null(False)
            & pl.col("model_type").is_in([*BASE_MODEL_TYPES, "ensemble"]) & ~pl.col("model_run_id").is_in(list(keep)))
        for r in stale.to_dicts():
            r["is_production"] = False
            r["finished_at"] = now
            rows.append(r)
    except Exception as e:  # noqa: BLE001 - the gate decision itself is still written
        log.warning("clearing older production flags for %s failed: %s", horizon, e)
    write_table(pl.DataFrame(rows), "model_runs", settings=settings)
    return {"winner": winner_type if winner_ok else None, "rank_ic_mean": winner_ic,
           "ranking": [(mt, ic, gate_eligible(mt, r)) for mt, r, ic in scored]}


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `ml_ensemble`. params: horizon (default: all 3)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    horizons = [p["horizon"]] if p.get("horizon") else list(HORIZONS)
    results, failed = {}, 0
    for h in horizons:
        ctx.check_cancelled()
        try:
            results[h] = run_ensemble_horizon(h, ctx.settings)
        except Exception as e:  # noqa: BLE001
            log.exception("ensemble %s failed", h)
            results[h] = {"status": "failed", "message": str(e)}
            failed += 1
    rows = {"model_runs": sum(1 for r in results.values() if r.get("status") == "success"),
           "ml_predictions": sum(r.get("rows", {}).get("ml_predictions", 0) for r in results.values()),
           "model_eval_series": sum(r.get("rows", {}).get("model_eval_series", 0) for r in results.values())}
    return JobResult(status="partial" if failed else "success", rows_written=rows,
                     entities_ok=len(horizons) - failed, entities_failed=failed,
                     watermark=ctx.as_of.isoformat(), message=str({h: r.get("status") for h, r in results.items()}))
