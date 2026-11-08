"""Composite x ML blend -> composite_scores profile `ml_blend` (owner: analytics-scoring). Job id `composite_ml_blend`.

scoring.yaml composite.profiles.ml_blend:
    score = 0.5 * balanced composite + 0.5 * (production model, horizon 3M) rank_pct * 100
Production model (same rule as the serving layer): API promotion (app_state.model_promotions) > model_runs.is_production
> preference ensemble, xgboost_rank, ft_transformer, cs_attention > latest predictions. Only return-forecasting model
types are eligible (insider/macro models are not).
Missing ML for a security -> the balanced score alone (weights renormalized, coverage halved for the ML part);
balanced null -> null. No eligible production model / no predictions -> nothing written (status skipped-like success),
so the API falls back to its own computation from factor_scores.

Params: mode latest (ctx.as_of) | history (every as_of_date with balanced composites in [start, end]), start, end,
        horizon (default from scoring.yaml, 3M), max_pred_age_days (default 7).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl

from app.analytics.scoring import add_ranks, spec
from app.core.config import Settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger(__name__)

MODEL_PREFERENCE = ("ensemble", "xgboost_rank", "ft_transformer", "cs_attention")


def production_run(horizon: str, settings: Settings | None = None) -> str | None:
    try:
        runs = (scan_table("model_runs", settings)
                .filter(pl.col("horizon") == horizon, pl.col("model_type").is_in(list(MODEL_PREFERENCE)),
                        pl.col("status").is_in(["completed", "archived"]))
                .select("model_run_id", "model_type", "is_production", "created_at").collect())
    except Exception as e:
        log.info("model_runs unavailable: %s", e)
        return None
    if runs.is_empty():
        return None
    promos: set[str] = set()
    try:
        from app.api.app_state import model_promotions

        promos = {p["model_run_id"] for p in model_promotions()}
    except Exception as e:  # app_state locked / missing -> promotions are advisory
        log.info("model promotions unavailable: %s", e)
    have = (scan_table("ml_predictions", settings).filter(pl.col("model_run_id").is_in(runs["model_run_id"].to_list()),
                                                          pl.col("horizon") == horizon)
            .group_by("model_run_id").agg(pl.col("date").max().alias("last_date")).collect())
    runs = runs.join(have, on="model_run_id", how="inner")
    if runs.is_empty():
        return None
    pref = {m: i for i, m in enumerate(MODEL_PREFERENCE)}
    runs = runs.with_columns(pl.col("model_run_id").is_in(list(promos)).alias("promoted"),
                             pl.col("model_type").replace_strict(pref, default=9, return_dtype=pl.Int32).alias("pref"))
    runs = runs.sort(["promoted", "is_production", "pref", "last_date", "created_at"],
                     descending=[True, True, False, True, True])
    return runs["model_run_id"][0]


def blend(bal: pl.DataFrame, preds: pl.DataFrame, w_base: float, w_ml: float) -> pl.DataFrame:
    """bal: as_of_date, security_id, issuer_id, sector_id, peer_region, score, coverage, dq_score;
    preds: as_of_date, security_id, ml (0..100)."""
    j = bal.join(preds, on=["as_of_date", "security_id"], how="left")
    has = pl.col("ml").is_not_null()
    score = pl.when(pl.col("score").is_null()).then(None) \
        .when(has).then((w_base * pl.col("score") + w_ml * pl.col("ml")) / (w_base + w_ml)) \
        .otherwise(pl.col("score"))
    cov = (w_base * pl.col("coverage") + w_ml * has.cast(pl.Float64)) / (w_base + w_ml)
    out = j.with_columns(score.alias("score"), cov.alias("coverage"), pl.lit("ml_blend").alias("profile_id"))
    parts = [add_ranks(g) for _, g in out.group_by("as_of_date")]
    return pl.concat(parts, how="vertical_relaxed") if parts else out


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult

    s = ctx.settings
    p = ctx.params or {}
    prof = spec(s)["composite"]["profiles"]["ml_blend"]
    w_base, w_ml = float(prof.get("_base_weight", 0.5)), float(prof.get("_ml_weight", 0.5))
    base_id = prof.get("_base_profile", "balanced")
    horizon = str(p.get("horizon", "3M"))
    run_id = production_run(horizon, s)
    if run_id is None:
        return JobResult(status="success", message=f"no production ML model with {horizon} predictions; ml_blend not written")
    mode = p.get("mode", "latest")
    end = date.fromisoformat(str(p["end"])) if p.get("end") else ctx.as_of
    start = date.fromisoformat(str(p["start"])) if p.get("start") else (date(2000, 1, 1) if mode == "history" else end)
    cs = (scan_table("composite_scores", s)
          .filter(pl.col("profile_id") == base_id, pl.col("as_of_date") >= start, pl.col("as_of_date") <= end)
          .select("as_of_date", "security_id", "issuer_id", "score", "coverage", "dq_score").collect())
    if cs.is_empty():
        return JobResult(status="success", message=f"no {base_id} composites in {start}..{end}")
    iss = scan_table("issuers", s).select("issuer_id", "sector_id", "region").collect()
    prm = spec(s)["peer_groups"]["peer_region_map"]
    cs = cs.join(iss, on="issuer_id", how="left").with_columns(
        pl.col("region").replace_strict(prm, default="GLOBAL", return_dtype=pl.Utf8).alias("peer_region")).drop("region")
    age = int(p.get("max_pred_age_days", 7))
    pr = (scan_table("ml_predictions", s)
          .filter(pl.col("model_run_id") == run_id, pl.col("horizon") == horizon,
                  pl.col("date") >= start - timedelta(days=age), pl.col("date") <= end)
          .select("security_id", "date", (pl.col("rank_pct") * 100).alias("ml")).collect().sort("security_id", "date"))
    q = cs.select("as_of_date", "security_id").sort("security_id", "as_of_date")
    preds = q.join_asof(pr, left_on="as_of_date", right_on="date", by="security_id", strategy="backward",
                        tolerance=timedelta(days=age), check_sortedness=False).drop("date")
    out = blend(cs, preds, w_base, w_ml)
    blended = out
    now = datetime.now(timezone.utc)
    out = out.with_columns(pl.col("as_of_date").dt.year().cast(pl.Int16).alias("year"), pl.lit(now).alias("computed_at"),
                           pl.lit(ctx.run_id).alias("run_id")).select(
        "as_of_date", "year", "security_id", "issuer_id", "profile_id", "score", "grade", "rank_global", "rank_sector",
        pl.col("coverage").cast(pl.Float32), pl.col("dq_score").cast(pl.Float32), "computed_at", "run_id")
    n = write_table(out, "composite_scores")
    n_ml = preds["ml"].is_not_null().sum()
    rows = {"composite_scores": n}
    msg = f"ml_blend from {run_id} ({horizon}); {n_ml}/{out.height} rows with ML predictions"
    try:
        n_ext, n_ext_ml = blend_extended(blended, run_id, horizon, start, end, age, w_base, w_ml, ctx.run_id, s)
        if n_ext:
            rows["composite_scores_ext"] = n_ext
            msg += f"; extended {n_ext_ml}/{n_ext} with (extrapolated) ML"
    except Exception as e:  # noqa: BLE001 - the extension never fails the MVP blend
        log.warning("ml_blend extension failed: %s", e)
    return JobResult(status="success", rows_written=rows, entities_ok=int(n_ml), message=msg)


def blend_extended(mvp_blend: pl.DataFrame, run_id: str, horizon: str, start: date, end: date, age: int,
                   w_base: float, w_ml: float, job_run_id: str | None, s: Settings) -> tuple[int, int]:
    """Full-universe extension (2026-10-30): ml_blend rows for composite_scores_ext. Balanced score of the extended
    row blended with the production model's (extrapolated) prediction when one is served; grade anchored to the MVP
    ml_blend distribution of the date; ranks among MVP + extended ml_blend rows (one per issuer)."""
    from app.analytics.scoring_full import anchored_grades  # noqa: PLC0415

    try:
        cs = (scan_table("composite_scores_ext", s)
              .filter(pl.col("profile_id") == "balanced", pl.col("as_of_date") >= start, pl.col("as_of_date") <= end)
              .select("as_of_date", "security_id", "issuer_id", "score", "coverage", "dq_score", "scope").collect())
    except Exception:  # noqa: BLE001 - table not created yet
        return 0, 0
    if cs.is_empty():
        return 0, 0
    iss = scan_table("issuers", s).select("issuer_id", "sector_id", "region").collect()
    prm = spec(s)["peer_groups"]["peer_region_map"]
    cs = cs.join(iss, on="issuer_id", how="left").with_columns(
        pl.col("region").replace_strict(prm, default="GLOBAL", return_dtype=pl.Utf8).alias("peer_region")).drop("region")
    pr = (scan_table("ml_predictions", s)
          .filter(pl.col("model_run_id") == run_id, pl.col("horizon") == horizon, pl.col("is_live"),
                  pl.col("date") >= start - timedelta(days=age), pl.col("date") <= end)
          .select("security_id", "date", (pl.col("rank_pct") * 100).alias("ml")).collect().sort("security_id", "date"))
    q = cs.select("as_of_date", "security_id").sort("security_id", "as_of_date")
    preds = q.join_asof(pr, left_on="as_of_date", right_on="date", by="security_id", strategy="backward",
                        tolerance=timedelta(days=age), check_sortedness=False).drop("date")
    j = cs.join(preds, on=["as_of_date", "security_id"], how="left")
    has = pl.col("ml").is_not_null()
    score = pl.when(pl.col("score").is_null()).then(None) \
        .when(has).then((w_base * pl.col("score") + w_ml * pl.col("ml")) / (w_base + w_ml)).otherwise(pl.col("score"))
    cov = (w_base * pl.col("coverage") + w_ml * has.cast(pl.Float64)) / (w_base + w_ml)
    ext = j.with_columns(score.alias("score"), cov.alias("coverage"), pl.lit("ml_blend").alias("profile_id"))
    parts = []
    for (d,), g in ext.group_by("as_of_date"):
        ref = mvp_blend.filter(pl.col("as_of_date") == d)
        g = anchored_grades(g, ref.select("profile_id", "score")) if ref.height else g.with_columns(
            pl.lit(None, pl.Utf8).alias("grade"))
        # ranks among MVP + extended scoring rows (one per issuer)
        both = pl.concat([ref.select("issuer_id", "sector_id", "peer_region", "score").with_columns(pl.lit(True).alias("__m")),
                          g.select("issuer_id", "sector_id", "peer_region", "score").with_columns(pl.lit(False).alias("__m"))],
                         how="vertical_relaxed").unique(subset=["issuer_id"], keep="first", maintain_order=True)
        both = both.with_columns(
            pl.col("score").rank("ordinal", descending=True).cast(pl.Int32).alias("rank_global"),
            pl.col("score").rank("ordinal", descending=True).over("sector_id", "peer_region").cast(pl.Int32).alias("rank_sector"),
            pl.col("score").count().over("sector_id", "peer_region").cast(pl.Int32).alias("sector_count"),
            pl.col("score").count().cast(pl.Int32).alias("n_scored")).filter(~pl.col("__m"))
        g = g.join(both.select("issuer_id", "rank_global", "rank_sector", "sector_count", "n_scored"), on="issuer_id",
                   how="left").with_columns(*[pl.when(pl.col("score").is_null()).then(None).otherwise(pl.col(c)).alias(c)
                                              for c in ("rank_global", "rank_sector")])
        parts.append(g)
    ext = pl.concat(parts, how="diagonal_relaxed")
    # rows of MVP-issuer copies (scope mvp) take the MVP issuer's ml_blend row
    copies = ext.filter(pl.col("scope") == "mvp").select("as_of_date", "security_id", "issuer_id")
    mv = mvp_blend.select("as_of_date", "issuer_id", "score", "grade", "rank_global", "rank_sector", "coverage",
                          "dq_score").unique(subset=["as_of_date", "issuer_id"], keep="first")
    cp = copies.join(mv, on=["as_of_date", "issuer_id"], how="inner").with_columns(
        pl.lit("ml_blend").alias("profile_id"), pl.lit("mvp").alias("scope"), pl.lit(None, pl.Int32).alias("sector_count"),
        pl.lit(None, pl.Int32).alias("n_scored"))
    ext = pl.concat([ext.filter(pl.col("scope") != "mvp"), cp], how="diagonal_relaxed")
    now = datetime.now(timezone.utc)
    ext = ext.with_columns(
        pl.col("as_of_date").dt.strftime("%Y-%m").alias("as_of_month"), pl.lit(now).alias("computed_at"),
        pl.lit(job_run_id).alias("run_id"),
        pl.when(pl.col("score").is_null()).then(pl.lit("insufficient_data")).otherwise(None).alias("reason"))
    # missing inputs of the balanced row carry over
    miss = (scan_table("composite_scores_ext", s)
            .filter(pl.col("profile_id") == "balanced", pl.col("as_of_date") >= start, pl.col("as_of_date") <= end)
            .select("as_of_date", "security_id", "missing_inputs").collect())
    ext = ext.join(miss, on=["as_of_date", "security_id"], how="left")
    out = ext.select("as_of_date", "as_of_month", "security_id", "issuer_id", "profile_id", "score", "grade", "rank_global",
                     "rank_sector", "sector_count", "n_scored", pl.col("coverage").cast(pl.Float32),
                     pl.col("dq_score").fill_null(0.0).cast(pl.Float32), "scope", "reason", "missing_inputs",
                     "computed_at", "run_id")
    n = write_table(out, "composite_scores_ext", settings=s)
    return n, int(ext.filter(has).height)
