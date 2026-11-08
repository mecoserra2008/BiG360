"""Nightly inference-only refresh of live predictions. Job `ml_predict` (nightly, depends_on: [ml_features]).
Owner: ml. Unlike the weekly training jobs, this does **not** retrain: it loads each model_type's latest
*completed* `model_runs` artifact (saved by `app.ml.train` / blended by `app.ml.ensemble`) and re-scores only the
latest `ml_features` date, for every (model_type, horizon) combination that has a trained run -- "both models
side by side" per docs/CONTRACT_GAPS_FRONTEND.md #11, not just the currently-production one.

Calibration (`ml_predictions` v2 columns): `expected_excess_return` / `interval_low` / `interval_high` /
`confidence` are derived from each model_run's own OOS (score, fwd_excess_sector) pairs --
`sklearn.isotonic.IsotonicRegression` maps the unitless score to an excess-return scale, and the empirical
10th/90th percentile of OOS residuals (conformal-style, no distributional assumption) gives the interval.
`confidence` is a bounded heuristic (not a calibrated probability): 1 - the interval width's cross-sectional
percentile rank on that date (narrower interval among that day's names -> higher confidence).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import polars as pl
import torch

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, write_table
from app.ml.features import load_registry
from app.ml.models import ft_transformer as ftm
from app.ml.models import xgb as xgbm

# `app.ml.train` is imported lazily inside functions below (not at module scope): train.py needs
# `save_xgb_artifact` / `calibrate_live` / `write_live` from this module to finish a training run, so a
# top-level import here would be circular. Both modules are fully loaded by the time these functions run.

log = get_logger(__name__)
CONFORMAL_LOW_Q, CONFORMAL_HIGH_Q = 0.10, 0.90


def artifact_dir(model_run_id: str, settings: Settings) -> Path:
    return settings.models_dir / model_run_id


def save_xgb_artifact(model: Any, model_run_id: str, settings: Settings) -> str:
    d = artifact_dir(model_run_id, settings)
    d.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, d / "model.joblib")
    return str(d.relative_to(settings.models_dir))


def save_transformer_artifact(model: ftm.FTTransformerCS, sector_vocab: dict[str, int], feature_cols: list[str],
                              model_run_id: str, settings: Settings) -> str:
    d = artifact_dir(model_run_id, settings)
    d.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "cfg": model.cfg.__dict__}, d / "model.pt")
    (d / "meta.json").write_text(json.dumps({"sector_vocab": sector_vocab, "feature_cols": feature_cols}))
    return str(d.relative_to(settings.models_dir))


def _load_transformer(path: Path) -> tuple[ftm.FTTransformerCS, dict[str, int], list[str]]:
    blob = torch.load(path / "model.pt", weights_only=False)
    cfg = ftm.FTConfig(**blob["cfg"])
    model = ftm.FTTransformerCS(cfg)
    model.load_state_dict(blob["state_dict"])
    meta = json.loads((path / "meta.json").read_text())
    return model, meta["sector_vocab"], meta["feature_cols"]


# ------------------------------------------------------------------------------------------------ scoring


def score_latest_xgb(model_run: dict[str, Any], settings: Settings) -> pl.DataFrame:
    from app.ml import train as T  # noqa: PLC0415 - see module-level note (circular import)

    d = artifact_dir(model_run["model_run_id"], settings)
    model = joblib.load(d / "model.joblib")
    registry = load_registry()
    feature_cols = [f"f_{fid}" for fid in T.live_feature_ids(registry)]
    feat = read_table("ml_features", settings=settings).filter(pl.col("in_universe"))
    latest = feat["date"].max()
    df = T.cs_transform(feat.filter(pl.col("date") == latest), registry)
    X = df.select(feature_cols).to_numpy().astype(np.float64)
    score = xgbm.predict(model, X)
    return df.select("date", "security_id").with_columns(pl.Series("score", score)), df, feature_cols, X


def score_latest_transformer(model_run: dict[str, Any], settings: Settings) -> pl.DataFrame:
    from app.ml import train as T  # noqa: PLC0415 - see module-level note (circular import)

    d = artifact_dir(model_run["model_run_id"], settings)
    model, sector_vocab, feature_cols = _load_transformer(d)
    registry = load_registry()
    feat = read_table("ml_features", settings=settings).filter(pl.col("in_universe"))
    me_dates = T._month_end_dates(feat["date"].unique().to_list())
    latest = max(me_dates)
    df = T.cs_transform(feat.filter(pl.col("date").is_in(list(me_dates))), registry)
    if "rank_label" not in df.columns:
        df = df.with_columns(pl.lit(0.5).alias("rank_label"))  # inference only: _build_batches' y is unused here
    # _build_batches needs the FULL month-end history (not just `latest`) to compute x_lag1/x_lag2 via
    # shift().over("security_id") -- passing only the latest date's rows (as an earlier version of this
    # function did) silently zeroed every lag feature at inference time, a real discrepancy vs. training.
    batches = T._build_batches(df.sort(["security_id", "date"]), feature_cols, sector_vocab)
    latest_batch = next(b for b in batches if b.date == latest)
    preds = ftm.predict_batches(model, [latest_batch])
    rows = [{"date": latest, "security_id": sid, "score": float(s)}
           for sid, s in zip(latest_batch.security_ids, preds[latest], strict=True)]
    return pl.DataFrame(rows), None, feature_cols, None


# ------------------------------------------------------------------------------------------------ calibration


def _rank_pct(df: pl.DataFrame, col: str = "score") -> pl.DataFrame:
    return df.with_columns((pl.col(col).rank(method="average").over("date") / pl.col(col).count().over("date"))
                          .alias("rank_pct"))


def calibrate_live(model_run_id: str, live: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """live: [date, security_id, score]. Adds expected_excess_return/interval_low/interval_high/confidence using
    this model_run's own OOS (score, fwd_excess_sector)."""
    from sklearn.isotonic import IsotonicRegression  # noqa: PLC0415

    oos = read_table("ml_predictions", settings=settings).filter(
        (pl.col("model_run_id") == model_run_id) & pl.col("is_oos"))
    mr = read_table("model_runs", columns=["model_run_id", "horizon"], settings=settings).filter(
        pl.col("model_run_id") == model_run_id)
    horizon = mr["horizon"][0] if mr.height else None
    lab = read_table("ml_labels", settings=settings).filter(pl.col("horizon") == horizon).select(
        "date", "security_id", "fwd_excess_sector")
    oos = oos.join(lab, on=["date", "security_id"], how="left").drop_nulls(["score", "fwd_excess_sector"])
    live = _rank_pct(live)
    if oos.height < 30:
        return live.with_columns(pl.lit(None, dtype=pl.Float64).alias("expected_excess_return"),
                                 pl.lit(None, dtype=pl.Float64).alias("interval_low"),
                                 pl.lit(None, dtype=pl.Float64).alias("interval_high"),
                                 pl.lit(None, dtype=pl.Float64).alias("confidence"))
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(oos["score"].to_numpy(), oos["fwd_excess_sector"].to_numpy())
    resid = oos["fwd_excess_sector"].to_numpy() - iso.predict(oos["score"].to_numpy())
    lo_q, hi_q = np.quantile(resid, [CONFORMAL_LOW_Q, CONFORMAL_HIGH_Q])
    expected = iso.predict(live["score"].to_numpy())
    live = live.with_columns(
        pl.Series("expected_excess_return", expected),
        pl.Series("interval_low", expected + lo_q),
        pl.Series("interval_high", expected + hi_q),
    )
    width = live["interval_high"] - live["interval_low"]
    conf = 1.0 - (width.rank(method="average") / width.len())
    return live.with_columns(conf.alias("confidence"))


def shap_top_xgb(model: Any, X: np.ndarray, feature_cols: list[str], top_n: int = 10) -> list[str | None]:
    contrib = xgbm.shap_contributions(model, X)
    out = []
    for row in contrib:
        vals = row[:-1]
        order = np.argsort(-np.abs(vals))[:top_n]
        out.append(json.dumps([{"feature": feature_cols[i], "value": None, "contribution": float(vals[i])}
                              for i in order]))
    return out


# ------------------------------------------------------------------------------------------------ writing


def write_live(model_run: dict[str, Any], live: pl.DataFrame, settings: Settings,
               shap_json: list[str | None] | None = None) -> int:
    now = datetime.now(UTC)
    live = live.with_columns((pl.col("rank_pct") * 10).ceil().clip(1, 10).cast(pl.Int8).alias("decile"))
    live = live.with_columns(
        pl.lit(model_run["model_run_id"]).alias("model_run_id"), pl.lit(model_run["horizon"]).alias("horizon"),
        pl.lit(True).alias("is_live"), pl.lit(False).alias("is_oos"),
        pl.lit(model_run["created_at"]).alias("trained_at"),
        pl.lit(model_run["feature_set_version"]).alias("feature_set_version"), pl.lit(now).alias("computed_at"),
        pl.Series("shap_top", shap_json) if shap_json is not None else pl.lit(None, dtype=pl.Utf8).alias("shap_top"),
    )
    cols = ["model_run_id", "date", "security_id", "horizon", "score", "rank_pct", "decile", "is_oos", "is_live",
           "shap_top", "expected_excess_return", "interval_low", "interval_high", "confidence", "trained_at",
           "feature_set_version", "computed_at"]
    return write_table(live.select(cols), "ml_predictions", settings=settings)


def refresh_model_run(model_run: dict[str, Any], settings: Settings) -> dict[str, Any]:
    mt = model_run["model_type"]
    if mt == "xgboost_rank":
        live, _df, feature_cols, X = score_latest_xgb(model_run, settings)
        model = joblib.load(artifact_dir(model_run["model_run_id"], settings) / "model.joblib")
        shap_json = shap_top_xgb(model, X, feature_cols)
    elif mt == "ft_transformer":
        live, _df, _fc, _X = score_latest_transformer(model_run, settings)
        shap_json = None  # integrated-gradients batch available via models.ft_transformer if needed later
    elif mt == "ensemble":
        live = _score_latest_ensemble(model_run, settings)
        shap_json = None
    else:
        return {"status": "skipped", "message": f"unknown model_type {mt}"}
    if live.is_empty():
        return {"status": "skipped", "message": "no live rows scored"}
    calibrated = calibrate_live(model_run["model_run_id"], live, settings)
    n = write_live(model_run, calibrated, settings, shap_json)
    return {"status": "success", "rows": n}


def _score_latest_ensemble(model_run: dict[str, Any], settings: Settings) -> pl.DataFrame:
    cv = json.loads(model_run["cv_scheme"])
    base_ids = cv.get("base_models", {})
    frames = []
    for mt, run_id in base_ids.items():
        p = read_table("ml_predictions", settings=settings).filter(
            (pl.col("model_run_id") == run_id) & pl.col("is_live"))
        if p.is_empty():
            continue
        latest = p["date"].max()
        frames.append(p.filter(pl.col("date") == latest).select("date", "security_id",
                                                                 pl.col("rank_pct").alias(f"rp_{mt}")))
    if len(frames) < 2:
        return pl.DataFrame()
    j = frames[0].join(frames[1], on=["date", "security_id"], how="inner")
    rp_cols = [c for c in j.columns if c.startswith("rp_")]
    return j.with_columns(pl.mean_horizontal(rp_cols).alias("score")).select("date", "security_id", "score")


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `ml_predict`. Re-scores the latest ml_features date for every (model_type, horizon) with a completed
    model_runs row; no retraining."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    mr = read_table("model_runs", settings=ctx.settings).filter(pl.col("status") == "completed")
    if mr.is_empty():
        return JobResult(status="skipped", message="no trained models yet")
    latest_per = (mr.sort("created_at").group_by(["model_type", "horizon"]).agg(pl.all().last()))
    n_rows, ok, failed = 0, 0, 0
    for row in latest_per.iter_rows(named=True):
        ctx.check_cancelled()
        try:
            out = refresh_model_run(row, ctx.settings)
            if out["status"] == "success":
                n_rows += out["rows"]
                ok += 1
            else:
                log.info("ml_predict skip %s/%s: %s", row["model_type"], row["horizon"], out.get("message"))
        except Exception as e:  # noqa: BLE001
            log.exception("ml_predict failed for %s/%s", row["model_type"], row["horizon"])
            failed += 1
    return JobResult(status="partial" if failed else "success", rows_written={"ml_predictions": n_rows},
                     entities_ok=ok, entities_failed=failed, watermark=ctx.as_of.isoformat())
