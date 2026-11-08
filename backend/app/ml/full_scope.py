"""Full-universe ML (full-scope extension 2026-10-30). Owner: ml. Job `ml_predict_full`.

Decision (docs/DATA_COMPLETENESS.md "Full-universe scores & ML"; evidence data/state/full_scope.json `ml`): the weekly
models keep training on the MVP universe (option a). For a security outside the MVP universe the production model
of each horizon predicts from the same feature registry, with every cross-sectional feature rank-gauss-transformed
AGAINST THE MVP CROSS-SECTION of the same date (`anchored_cs_transform`: the position the value would take among the
MVP names, so the model sees inputs on its training scale and the MVP predictions are unchanged); the prediction's
rank_pct / decile are its position among that date's MVP predictions. Such predictions are labelled
`scope = extrapolated` and are written only for segments (peer region x liquidity bucket) whose out-of-sample
extrapolation check passed in the purged walk-forward (`segment_gate`, from the evidence file); every other security
gets a `no_prediction` row in ml_scope_status with the reason (suppressed / insufficient_data / stale).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import polars as pl
from scipy.special import ndtri

from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table
from app.ml.features import live_feature_ids, load_registry

log = get_logger(__name__)

#: liquidity buckets on the ADV20 feature (USD); PIT proxy for size (market caps exist PIT for MVP names only)
ADV_BUCKETS = ((50e6, "large"), (10e6, "mid"), (1e6, "small"), (0.0, "micro"))
EVIDENCE_KEY = "ml"
MIN_REF = 5


def adv_bucket_expr(col: str = "adv_usd_20d") -> pl.Expr:
    e = pl.lit(None, pl.Utf8)
    for lo, name in reversed(ADV_BUCKETS):
        e = pl.when(pl.col(col) >= lo).then(pl.lit(name)).otherwise(e)
    return e


def anchored_cs_transform(ext: pl.DataFrame, ref: pl.DataFrame, registry: dict[str, Any] | None = None
                          ) -> pl.DataFrame:
    """app.ml.train.cs_transform for rows OUTSIDE the reference cross-section: per date and live feature, the
    rank-gauss of the value's insertion percentile among the reference values ((#below + 0.5 #equal + 0.5) /
    (n_ref + 1), clipped to [1e-4, 1 - 1e-4]); 'none' features pass through; < MIN_REF reference values -> null.
    ext / ref: date, security_id, f_* raw values. Returns ext with transformed f_* (Float32)."""
    reg = registry or load_registry()
    out = ext.select("date", "security_id").with_row_index("__rid")
    for fid in live_feature_ids(reg):
        col, kind = f"f_{fid}", reg["by_id"][fid]["transform"]
        if col not in ext.columns:
            continue
        if kind == "none":
            out = out.with_columns(ext[col].cast(pl.Float32).alias(col))
            continue
        e = ext.select(pl.int_range(pl.len(), dtype=pl.UInt32).alias("__rid"), "date", pl.col(col).cast(pl.Float64).alias("__x")) \
            .filter(pl.col("__x").is_not_null() & pl.col("__x").is_finite())
        r = ref.select("date", pl.col(col).cast(pl.Float64).alias("__x")).filter(
            pl.col("__x").is_not_null() & pl.col("__x").is_finite())
        if e.is_empty() or r.is_empty():
            out = out.with_columns(pl.lit(None, pl.Float32).alias(col))
            continue
        u = pl.concat([r.with_columns(pl.lit(1, pl.Int32).alias("__r"), pl.lit(None, pl.UInt32).alias("__rid"))
                       .select("date", "__x", "__r", "__rid"),
                       e.with_columns(pl.lit(0, pl.Int32).alias("__r")).select("date", "__x", "__r", "__rid")],
                      how="vertical_relaxed")
        cs = pl.col("__r").cum_sum().over("date")
        less = u.sort("date", "__x", "__r").with_columns(cs.alias("__c")).filter(pl.col("__r") == 0) \
            .select("__rid", pl.col("__c").alias("__lt"))
        le = u.sort(["date", "__x", "__r"], descending=[False, False, True]).with_columns(cs.alias("__c")) \
            .filter(pl.col("__r") == 0).select("__rid", pl.col("__c").alias("__le"))
        n = r.group_by("date").agg(pl.len().alias("__n"))
        j = e.join(n, on="date", how="left").join(less, on="__rid").join(le, on="__rid")
        if kind == "cs_rank_gauss":
            pct = ((pl.col("__lt") + 0.5 * (pl.col("__le") - pl.col("__lt")) + 0.5) / (pl.col("__n") + 1)).clip(1e-4, 1 - 1e-4)
            j = j.with_columns(pl.when(pl.col("__n") >= MIN_REF).then(pct).otherwise(None).alias("__p"))
            vals = j["__p"].to_numpy()
            z = np.where(np.isnan(vals), np.nan, ndtri(np.nan_to_num(vals, nan=0.5)))
            j = j.with_columns(pl.Series("__z", z).fill_nan(None).cast(pl.Float32))
        else:  # zscore against the reference mean / std of the date
            ms = r.group_by("date").agg(pl.col("__x").mean().alias("__mu"), pl.col("__x").std().alias("__sd"))
            j = j.join(ms, on="date", how="left").with_columns(
                pl.when((pl.col("__n") >= MIN_REF) & (pl.col("__sd") > 1e-12))
                .then((pl.col("__x") - pl.col("__mu")) / pl.col("__sd")).otherwise(None).cast(pl.Float32).alias("__z"))
        out = out.join(j.select("__rid", pl.col("__z").alias(col)), on="__rid", how="left")
    rest = [c for c in ext.columns if not c.startswith("f_") and c not in ("date", "security_id")]
    out = out.sort("__rid").drop("__rid")
    return pl.concat([out, ext.select(rest)], how="horizontal") if rest else out


def anchored_rank_pct(scores: np.ndarray, ref_scores: np.ndarray) -> np.ndarray:
    """Position of each score among the reference (MVP) scores of the date, 0..1 (predict._rank_pct convention:
    rank / n with the value inserted)."""
    r = np.sort(ref_scores[np.isfinite(ref_scores)])
    less = np.searchsorted(r, scores, side="left")
    le = np.searchsorted(r, scores, side="right")
    return (less + 0.5 * (le - less) + 1.0) / (len(r) + 1)


# ------------------------------------------------------------------------------------------------ evidence / gate


def evidence_path(settings: Settings) -> Path:
    return settings.state_dir / "full_scope.json"


def load_evidence(settings: Settings) -> dict[str, Any]:
    try:
        return (json.loads(evidence_path(settings).read_text(encoding="utf-8")) or {}).get(EVIDENCE_KEY) or {}
    except (OSError, ValueError):
        return {}


def segment_gate(evidence: dict[str, Any], horizon: str, region: str | None, bucket: str | None) -> tuple[bool, str]:
    """(serve?, detail) for the extrapolated predictions of one segment: the walk-forward extrapolation IC of the
    segment must be positive with a HAC t-stat >= 2 over >= 24 OOS dates (evidence `segments`)."""
    seg = ((evidence.get("segments") or {}).get(horizon) or {}).get(f"{region}|{bucket}")
    if not seg:
        return False, f"no out-of-sample extrapolation evidence for {region} / {bucket} ({horizon})"
    ok = bool(seg.get("served"))
    det = (f"extrapolation OOS rank IC {seg.get('rank_ic'):+.3f} (t {seg.get('t'):.1f}, {seg.get('n_dates')} dates)"
           if seg.get("rank_ic") is not None else "insufficient OOS dates")
    if evidence.get("extrapolation_leakage_passed") is False:
        return False, det + "; the label-permutation leakage check of out-of-universe extrapolation failed"
    return ok, det


# ------------------------------------------------------------------------------------------------ live predictions


def _production_runs(settings: Settings) -> dict[str, dict[str, Any]]:
    from app.analytics.scoring_ml_blend import production_run  # noqa: PLC0415

    mr = read_table("model_runs", settings=settings)
    out = {}
    for h in ("1M", "3M", "6M"):
        rid = production_run(h, settings)
        if rid is None:
            continue
        row = mr.filter(pl.col("model_run_id") == rid).sort("finished_at").tail(1).to_dicts()
        if row and row[0]["model_type"] == "xgboost_rank":
            out[h] = row[0]
        elif row:
            log.warning("ml_predict_full %s: production run %s is %s (only xgboost_rank artifacts are extrapolated)", h,
                        rid, row[0]["model_type"])
    return out


def predict_full(as_of: date, settings: Settings, ctx: Any = None, chunk: int = 3000) -> dict[str, Any]:
    from app.analytics.scoring_full import extended_universe, mvp_securities, peer_region_of  # noqa: PLC0415
    from app.ml.features import compute_features  # noqa: PLC0415
    from app.ml.predict import artifact_dir  # noqa: PLC0415

    t0 = time.time()
    reg = load_registry()
    cols = [f"f_{f}" for f in live_feature_ids(reg)]
    evidence = load_evidence(settings)
    runs = _production_runs(settings)
    mvp = mvp_securities(settings)
    ext = extended_universe(settings, mvp)
    ext = ext.join(peer_region_of(ext, settings), on="issuer_id", how="left")
    D = scan_table("ml_features", settings).filter(pl.col("in_universe")).select(pl.col("date").max()).collect().item()
    servable = {h for h in runs if evidence.get("extrapolation_leakage_passed") is not False
                and any(v.get("served") for v in ((evidence.get("segments") or {}).get(h) or {}).values())}
    if not servable:
        # nothing passed the gate: no features / model calls at all, only the per-security reason (light path)
        return _status_only(D, ext, runs, evidence, settings, t0)
    ref = scan_table("ml_features", settings).filter(pl.col("in_universe"), pl.col("date") == D).collect()
    try:
        xfs = scan_table("factor_scores_ext", settings).filter(pl.col("as_of_date") <= D,
                                                                pl.col("as_of_date") >= D - timedelta(days=60)) \
            .select("as_of_date", "security_id", "factor_id", "score").collect()
    except Exception:  # noqa: BLE001
        xfs = None
    live = scan_table("ml_predictions", settings).filter(
        pl.col("model_run_id").is_in([r["model_run_id"] for r in runs.values()]), pl.col("is_live"), pl.col("date") == D,
        pl.col("scope").fill_null("mvp") == "mvp").select("model_run_id", "horizon", "score").collect()
    models = {h: joblib.load(artifact_dir(r["model_run_id"], settings) / "model.joblib") for h, r in runs.items()}
    preds: list[pl.DataFrame] = []
    status: list[pl.DataFrame] = []
    ids = sorted(ext["security_id"].to_list())
    for k in range(0, len(ids), chunk):
        if ctx is not None:
            ctx.check_cancelled()
        f = compute_features(D, settings, security_ids=ids[k:k + chunk], dates=[D], extra_factor_scores=xfs)
        # a security priced in two regions comes back twice from the feature panel's region join: keep one row
        f = f.unique(subset=["security_id"], keep="first", maintain_order=True)
        f = f.join(ext.select("security_id", "peer_region").unique(subset=["security_id"]), on="security_id", how="left")
        fresh = f.filter(pl.col("adv_usd_20d").is_not_null())
        tr = anchored_cs_transform(fresh.select("date", "security_id", *cols), ref, reg) if fresh.height else fresh
        tr = tr.join(fresh.select("security_id", "peer_region", "adv_usd_20d"), on="security_id", how="left") \
            .with_columns(adv_bucket_expr().alias("bucket"))
        n_feat = tr.select(pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in cols]).alias("n")) \
            if tr.height else pl.DataFrame({"n": []})
        tr = tr.with_columns(n_feat["n"].alias("__nf")) if tr.height else tr
        for h, run in runs.items():
            base = f.select("security_id").join(tr.select("security_id", "peer_region", "bucket", "__nf", *cols),
                                                on="security_id", how="left") if tr.height else \
                f.select("security_id").with_columns(pl.lit(None).alias("bucket"))
            refs = live.filter(pl.col("model_run_id") == run["model_run_id"], pl.col("horizon") == h)["score"].to_numpy()
            ok_rows = base.filter(pl.col("__nf") >= 8) if "__nf" in base.columns else base.head(0)
            if ok_rows.height and len(refs) >= 50:
                X = ok_rows.select(cols).to_numpy().astype(np.float64)
                sc = models[h].predict(X)
                rp = anchored_rank_pct(sc, refs)
                ok_rows = ok_rows.with_columns(pl.Series("score", sc), pl.Series("rank_pct", rp))
            else:
                ok_rows = ok_rows.with_columns(pl.lit(None, pl.Float64).alias("score"),
                                               pl.lit(None, pl.Float64).alias("rank_pct"))
            gates = {(r_, b_): segment_gate(evidence, h, r_, b_)
                     for r_, b_ in ok_rows.select("peer_region", "bucket").unique().iter_rows()}
            serve = ok_rows.with_columns(
                pl.struct("peer_region", "bucket").map_elements(lambda s_: gates[(s_["peer_region"], s_["bucket"])][0],
                                                                return_dtype=pl.Boolean).alias("__serve"),
                pl.struct("peer_region", "bucket").map_elements(lambda s_: gates[(s_["peer_region"], s_["bucket"])][1],
                                                                return_dtype=pl.Utf8).alias("__det")) \
                if ok_rows.height else ok_rows.with_columns(pl.lit(False).alias("__serve"), pl.lit(None, pl.Utf8).alias("__det"))
            served = serve.filter(pl.col("__serve") & pl.col("score").is_not_null())
            if served.height:
                preds.append(served.select(
                    pl.lit(run["model_run_id"]).alias("model_run_id"), pl.lit(D).alias("date"), "security_id",
                    pl.lit(h).alias("horizon"), "score", "rank_pct"))
            st_ok = served.select("security_id", pl.lit(run["model_run_id"]).alias("model_run_id"),
                                  pl.concat_str(["peer_region", pl.lit("|"), "bucket"]).alias("segment"),
                                  pl.lit("served").alias("status"), pl.lit(None, pl.Utf8).alias("reason"),
                                  pl.concat_str([pl.lit("Extrapolated: MVP-trained model, "), pl.col("__det")]).alias("detail"))
            st_sup = serve.filter(~pl.col("__serve") & pl.col("score").is_not_null()).select(
                "security_id", pl.lit(None, pl.Utf8).alias("model_run_id"),
                pl.concat_str(["peer_region", pl.lit("|"), "bucket"]).alias("segment"), pl.lit("no_prediction").alias("status"),
                pl.lit("suppressed").alias("reason"),
                pl.concat_str([pl.lit("Withheld: the MVP-trained model shows no validated out-of-sample skill outside "
                                      "the MVP universe for this segment ("), pl.col("__det"), pl.lit(")")]).alias("detail"))
            done = set(st_ok["security_id"].to_list()) | set(st_sup["security_id"].to_list())
            rest = f.filter(~pl.col("security_id").is_in(list(done))).select(
                "security_id", pl.lit(None, pl.Utf8).alias("model_run_id"), pl.lit(None, pl.Utf8).alias("segment"),
                pl.lit("no_prediction").alias("status"),
                pl.when(pl.col("adv_usd_20d").is_null()).then(pl.lit("stale")).otherwise(pl.lit("insufficient_data"))
                .alias("reason"),
                pl.when(pl.col("adv_usd_20d").is_null()).then(pl.lit(f"No price bar in the 20 sessions before {D}"))
                .otherwise(pl.lit("Fewer than 8 of the model's features available (price history < 252 sessions)"))
                .alias("detail"))
            status.append(pl.concat([st_ok, st_sup, rest], how="vertical_relaxed").with_columns(pl.lit(h).alias("horizon")))
        del f, fresh, tr
    now = datetime.now(UTC)
    rows = {}
    if preds:
        p = pl.concat(preds, how="vertical_relaxed").unique(subset=["model_run_id", "security_id", "horizon"],
                                                            keep="first", maintain_order=True).with_columns(
            (pl.col("rank_pct") * 10).ceil().clip(1, 10).cast(pl.Int8).alias("decile"), pl.lit(False).alias("is_oos"),
            pl.lit(True).alias("is_live"), pl.lit(None, pl.Utf8).alias("shap_top"), pl.lit("extrapolated").alias("scope"),
            pl.lit(now).alias("computed_at"))
        p = p.join(pl.DataFrame([{"model_run_id": r["model_run_id"], "trained_at": r["created_at"],
                                  "feature_set_version": r["feature_set_version"]} for r in runs.values()]),
                   on="model_run_id", how="left")
        # previous extrapolated rows of these runs on other dates are left in place (history); same-date rows replaced
        rows["ml_predictions"] = write_table(p, "ml_predictions", settings=settings)
    st = pl.concat(status, how="vertical_relaxed").with_columns(pl.lit(D).alias("as_of_date"), pl.lit(now).alias("computed_at")) \
        if status else pl.DataFrame()
    if st.height:
        st = st.unique(subset=["security_id", "horizon"], keep="first", maintain_order=True)
        rows["ml_scope_status"] = write_table(st, "ml_scope_status", mode="overwrite", settings=settings)
    summary = st.group_by("horizon", "status", "reason").len().sort("horizon", "status").to_dicts() if st.height else []
    return {"date": str(D), "runs": {h: r["model_run_id"] for h, r in runs.items()}, "rows": rows, "summary": summary,
            "seconds": round(time.time() - t0, 1)}


def _status_only(D: date, ext: pl.DataFrame, runs: dict[str, dict[str, Any]], evidence: dict[str, Any],
                 settings: Settings, t0: float) -> dict[str, Any]:
    """ml_scope_status for every extended security when no segment is servable: stale (no bar in the 20 sessions
    before D), insufficient_data (< 252 sessions of history, too few features), else suppressed with the evidence."""
    ids = ext["security_id"].unique().to_list()
    px = (scan_table("prices_daily", settings).filter(pl.col("security_id").is_in(ids), pl.col("date") <= D,
                                                      pl.col("date") >= D - timedelta(days=400))
          .group_by("security_id").agg(pl.col("date").max().alias("last"), pl.len().alias("n")).collect())
    j = ext.select("security_id", "peer_region").unique(subset=["security_id"]).join(px, on="security_id", how="left")
    why = (evidence.get("why") or "no out-of-sample evidence for out-of-universe predictions yet")
    rows = []
    for h in ("1M", "3M", "6M"):
        rows.append(j.select(
            "security_id", pl.lit(None, pl.Utf8).alias("model_run_id"), pl.col("peer_region").alias("segment"),
            pl.lit("no_prediction").alias("status"),
            pl.when(pl.col("last").is_null() | (pl.col("last") < D - timedelta(days=30))).then(pl.lit("stale"))
            .when(pl.col("n") < 200).then(pl.lit("insufficient_data")).otherwise(pl.lit("suppressed")).alias("reason"),
            pl.when(pl.col("last").is_null() | (pl.col("last") < D - timedelta(days=30)))
            .then(pl.lit("No recent price bar (suspended, delisted or not refreshed)"))
            .when(pl.col("n") < 200).then(pl.lit("Price history too short for the model's features (< 252 sessions)"))
            .otherwise(pl.lit("Withheld outside the MVP model universe: " + why[:900])).alias("detail"),
            pl.lit(h).alias("horizon")))
    st = pl.concat(rows, how="vertical_relaxed").with_columns(pl.lit(D).alias("as_of_date"),
                                                               pl.lit(datetime.now(UTC)).alias("computed_at"))
    n = write_table(st, "ml_scope_status", mode="overwrite", settings=settings)
    summary = st.group_by("horizon", "status", "reason").len().sort("horizon", "reason").to_dicts()
    return {"date": str(D), "runs": {h: r["model_run_id"] for h, r in runs.items()},
            "rows": {"ml_scope_status": n}, "summary": summary, "seconds": round(time.time() - t0, 1),
            "served": 0}


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    out = predict_full(ctx.as_of, ctx.settings, ctx)
    n = (out.get("rows") or {}).get("ml_predictions", 0)
    log.info("ml_predict_full: %s", json.dumps(out, default=str))
    return JobResult(status="success", rows_written=out.get("rows") or {}, entities_ok=int(n),
                     watermark=out["date"], message=f"{n} extrapolated predictions on {out['date']} "
                                                   f"({out['runs']}); {out['seconds']}s")
