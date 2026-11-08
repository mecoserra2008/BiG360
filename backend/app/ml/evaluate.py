"""OOS evaluation metrics shared by `app.ml.train` and `app.ml.ensemble`. Not a standalone job (jobs.yaml has no
`ml_evaluate` entry): each training job calls `evaluate_predictions()` and writes the result to `model_eval_series`
/ `model_runs` itself, since both write those tables as part of the same job (contracts/jobs.yaml `writes:`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.logsetup import get_logger

log = get_logger(__name__)


def _hac_tstat(x: np.ndarray, maxlags: int = 4) -> tuple[float, float]:
    """Newey-West mean/t-stat of a (possibly autocorrelated) time series of per-date metric values."""
    import statsmodels.api as sm  # noqa: PLC0415

    x = x[~np.isnan(x)]
    if len(x) < 3:
        return (float(np.mean(x)) if len(x) else float("nan")), float("nan")
    model = sm.OLS(x, np.ones_like(x)).fit(cov_type="HAC", cov_kwds={"maxlags": min(maxlags, len(x) - 1)})
    return float(model.params[0]), float(model.tvalues[0])


_BY_DATE_SCHEMA = {"date": pl.Date, "value": pl.Float64}


def _spearman_by_date(df: pl.DataFrame, pred_col: str, label_col: str) -> pl.DataFrame:
    """[date, value] Spearman corr(pred, label) per date (>=5 names required)."""
    out = []
    for d, g in df.drop_nulls([pred_col, label_col]).group_by("date"):
        if g.height < 5:
            continue
        r1, r2 = g[pred_col].rank(), g[label_col].rank()
        sd1, sd2 = r1.std(), r2.std()
        corr = None if not sd1 or not sd2 else float(np.corrcoef(r1.to_numpy(), r2.to_numpy())[0, 1])
        out.append({"date": d[0] if isinstance(d, tuple) else d, "value": corr})
    # explicit schema: if every fold happened to be degenerate (constant prediction -> corr=None for all rows,
    # or no fold had >=5 names), polars would otherwise infer the all-null `value` column as dtype Null, which
    # cannot be compared with `> 0` downstream (hit_rate) -- forcing Float64 keeps every null-handling path sane.
    return pl.DataFrame(out, schema=_BY_DATE_SCHEMA) if out else pl.DataFrame(schema=_BY_DATE_SCHEMA)


def _pearson_by_date(df: pl.DataFrame, pred_col: str, label_col: str) -> pl.DataFrame:
    out = []
    for d, g in df.drop_nulls([pred_col, label_col]).group_by("date"):
        if g.height < 5:
            continue
        x, y = g[pred_col].to_numpy(), g[label_col].to_numpy()
        corr = None if x.std() == 0 or y.std() == 0 else float(np.corrcoef(x, y)[0, 1])
        out.append({"date": d[0] if isinstance(d, tuple) else d, "value": corr})
    return pl.DataFrame(out, schema=_BY_DATE_SCHEMA) if out else pl.DataFrame(schema=_BY_DATE_SCHEMA)


def decile_table(df: pl.DataFrame, score_col: str = "score") -> pl.DataFrame:
    """Per-date decile (1..10 by `score_col`) mean fwd_excess_sector, used for both decile_return series and
    the long-short spread. Deciles are assigned fresh from the model's own score (not the label's `decile`)."""
    ranked = df.drop_nulls([score_col, "fwd_excess_sector"]).with_columns(
        (pl.col(score_col).rank(method="average").over("date")
         / pl.col(score_col).count().over("date") * 10).ceil().clip(1, 10).cast(pl.Int8).alias("pred_decile"))
    return ranked.group_by(["date", "pred_decile"]).agg(pl.col("fwd_excess_sector").mean().alias("ret"))


def long_short_series(dec: pl.DataFrame, horizon_days: int) -> pl.DataFrame:
    """[date, ls_return] D10-D1 spread per rebalance date. NOTE: at the full rebalance grid (weekly for XGB,
    monthly for the Transformer) each row's `ls_return` is a horizon-long forward return, so *consecutive rows
    overlap* whenever the grid is finer than the horizon (always true for XGB: weekly rebalances with a 1M/3M/6M
    forward window). Never sum/compound this series directly -- use `non_overlapping_subsample()` first (see
    `evaluate_predictions`, which is the only correct caller)."""
    d10 = dec.filter(pl.col("pred_decile") == 10).select("date", pl.col("ret").alias("r10"))
    d1 = dec.filter(pl.col("pred_decile") == 1).select("date", pl.col("ret").alias("r1"))
    j = d10.join(d1, on="date", how="inner").sort("date").with_columns((pl.col("r10") - pl.col("r1")).alias("ls_return"))
    return j.select("date", "ls_return")


def non_overlapping_subsample(df: pl.DataFrame, horizon_days: int, date_col: str = "date") -> pl.DataFrame:
    """Greedily keep rows spaced >= horizon_days (calendar) apart, starting from the earliest date.

    Fixes a real bug found via frontend verification: `ls["ls_return"]` at the full rebalance grid holds
    overlapping-window forward returns (e.g. weekly rebalances x a 3M-forward label), so summing or compounding
    them directly double/triple/quadruple-counts the same underlying price moves (observed: a ~3,200% cumulative
    long-short series). Subsampling to one row per non-overlapping horizon-length window, THEN compounding, is
    the standard fix (the alternative -- Jegadeesh-Titman overlapping-portfolio averaging, i.e. 1/horizon capital
    in each of `horizon` staggered sub-portfolios -- is equivalent in expectation but noisier here with only a
    few hundred OOS dates; non-overlapping subsampling is simpler and was chosen for that reason)."""
    if df.height < 2:
        return df
    d = df.sort(date_col)
    dates = d[date_col].to_list()
    kept = [0]
    last = dates[0]
    for i in range(1, len(dates)):
        if (dates[i] - last).days >= horizon_days:
            kept.append(i)
            last = dates[i]
    return d[kept]


def turnover(df: pl.DataFrame, score_col: str = "score", top_frac: float = 0.1) -> float:
    """Average week-over-week fraction of the top-decile names that changed, across consecutive rebalance dates."""
    dates = sorted(df["date"].unique().to_list())
    sets = {}
    for d in dates:
        g = df.filter(pl.col("date") == d).drop_nulls(score_col)
        if g.is_empty():
            continue
        n = max(1, int(round(g.height * top_frac)))
        top = set(g.sort(score_col, descending=True).head(n)["security_id"].to_list())
        sets[d] = top
    vals = []
    prev = None
    for d in sorted(sets):
        if prev is not None and sets[prev]:
            changed = len(sets[d] - sets[prev]) / len(sets[prev])
            vals.append(changed)
        prev = d
    return float(np.mean(vals)) if vals else float("nan")


def evaluate_predictions(pred: pl.DataFrame, horizon: str, periods_per_year: dict[str, float] | None = None
                         ) -> dict[str, Any]:
    """pred: [date, security_id, score, fwd_return, fwd_excess_sector, rank_label]. Returns a dict with
    `series` (model_eval_series rows, model_run_id-less) and `summary` (model_runs metric fields)."""
    ppy = (periods_per_year or {"1M": 12.0, "3M": 4.0, "6M": 2.0})[horizon]
    ic_s = _pearson_by_date(pred, "score", "fwd_excess_sector")
    ric_s = _spearman_by_date(pred, "score", "fwd_excess_sector")
    ic_mean, _ = _hac_tstat(ic_s["value"].to_numpy())
    ric_mean, ric_t = _hac_tstat(ric_s["value"].to_numpy())
    ric_std = float(np.nanstd(ric_s["value"].to_numpy())) if ric_s.height else float("nan")
    ric_ir = ric_mean / ric_std if ric_std else float("nan")
    _hit_mean = (ric_s["value"] > 0).mean() if ric_s.height else None
    hit_rate = float(_hit_mean) if _hit_mean is not None else float("nan")
    horizon_cal_days = {"1M": 21, "3M": 63, "6M": 126}[horizon] * 7 / 5  # trading days -> approx calendar days
    dec = decile_table(pred)
    dec_summary = dec.group_by("pred_decile").agg(pl.col("ret").mean().alias("mean_fwd_excess"))
    ls_full = long_short_series(dec, horizon_cal_days)
    ls = non_overlapping_subsample(ls_full, horizon_cal_days)  # see non_overlapping_subsample() docstring
    _ls_mean = ls["ls_return"].mean() if ls.height else None
    ls_ann = float(_ls_mean * ppy) if _ls_mean is not None else float("nan")
    _ls_std = ls["ls_return"].std() if ls.height > 1 else None
    ls_sharpe = float(_ls_mean / _ls_std * np.sqrt(ppy)) if _ls_mean is not None and _ls_std else float("nan")
    to = turnover(pred)
    now = datetime.now(UTC)
    series_rows: list[dict[str, Any]] = []
    for row in ic_s.iter_rows(named=True):
        series_rows.append({"metric": "ic", "date": row["date"], "bucket": "", "value": row["value"]})
    for row in ric_s.iter_rows(named=True):
        series_rows.append({"metric": "rank_ic", "date": row["date"], "bucket": "", "value": row["value"]})
    for row in dec_summary.iter_rows(named=True):
        series_rows.append({"metric": "decile_return", "date": date(1900, 1, 1), "bucket": str(row["pred_decile"]),
                           "value": row["mean_fwd_excess"]})
    cum = 1.0
    for row in ls.sort("date").iter_rows(named=True):
        cum *= 1.0 + row["ls_return"]  # geometric compounding over the non-overlapping subsample (see above)
        series_rows.append({"metric": "cum_long_short", "date": row["date"], "bucket": "", "value": cum - 1.0})
    n_dates_series = pred.select("date").unique().with_columns(
        pl.lit("coverage").alias("metric"), pl.lit("").alias("bucket"))
    coverage = pred.group_by("date").agg(pl.col("security_id").n_unique().alias("value")).with_columns(
        pl.lit("coverage").alias("metric"), pl.lit("").alias("bucket"))
    for row in coverage.iter_rows(named=True):
        series_rows.append({"metric": "coverage", "date": row["date"], "bucket": "", "value": float(row["value"])})
    for r in series_rows:
        r["computed_at"] = now
    summary = {
        "ic_mean": ic_mean, "rank_ic_mean": ric_mean, "rank_ic_ir": ric_ir, "rank_ic_tstat": ric_t,
        "decile_spread_ann": ls_ann, "long_short_sharpe": ls_sharpe, "turnover_monthly": to, "hit_rate": hit_rate,
    }
    method_note = (f"decile_spread_ann/long_short_sharpe/cum_long_short use a non-overlapping {horizon}-spaced "
                   f"subsample of the D10-D1 spread ({ls.height} of {ls_full.height} rebalance dates kept) with "
                   f"geometric compounding, to avoid double-counting overlapping {horizon}-forward windows at a "
                   f"finer rebalance grid. ic_mean/rank_ic_mean/hit_rate use the full (overlap-safe: per-date "
                   f"cross-sectional, not time-compounded) grid of {ric_s.height} dates.")
    return {"series": series_rows, "summary": summary, "method_note": method_note}


def series_rows_to_frame(series_rows: list[dict[str, Any]], model_run_id: str) -> pl.DataFrame:
    if not series_rows:
        return pl.DataFrame(schema={"model_run_id": pl.Utf8, "metric": pl.Utf8, "date": pl.Date, "bucket": pl.Utf8,
                                    "value": pl.Float64, "computed_at": pl.Datetime("us", "UTC")})
    df = pl.DataFrame(series_rows)
    return df.with_columns(pl.lit(model_run_id).alias("model_run_id")).select(
        "model_run_id", "metric", "date", "bucket", "value", "computed_at")


def add_fold_metrics(series_rows: list[dict[str, Any]], fold_id: str, fold_end: date, rank_ic: float) -> None:
    """Only the rank-IC scalar goes into model_eval_series (metric='fold_rank_ic' is declared as an IC-shaped
    quantity by contracts/schemas/model_eval_series.yaml; a Sharpe ratio does not belong under that metric name
    and was previously stored there under a '{fold_id}_sharpe' bucket, which is what let fold Sharpe values
    (e.g. 1.37, 1.71) masquerade as an IC in frontend verification -- fixed. Fold Sharpe + the train/test window
    dates now live in `model_runs.fold_metrics` (one JSON list, built by app.ml.train), which is also where a
    consumer should look for per-fold windows, per the brief's 'as the contract supports')."""
    series_rows.append({"metric": "fold_rank_ic", "date": fold_end, "bucket": fold_id, "value": rank_ic,
                        "computed_at": datetime.now(UTC)})
