"""Walk-forward training. Jobs `ml_train_xgb` / `ml_train_transformer` (weekly, same module, `params.model_type`
selects `xgboost_rank` | `ft_transformer`). Owner: ml.

Walk-forward CV: expanding window, `min_train_years` initial training history, then `test_months`-long OOS test
folds stepped by `step_months`, with a **purge** gap between train and test equal to the label horizon (so no
training sample's label window overlaps the test period) plus an **embargo** of `embargo_days` extra calendar
days (de Prado-style purged walk-forward). Both the XGBoost and Transformer jobs share the same fold boundaries
(`walk_forward_folds`) and the same cross-sectional transform (`cs_transform`, from `feature_registry.yaml`), so
their OOS predictions are directly comparable fold-by-fold.

The Transformer is trained on **month-end dates only** (ARCHITECTURE §18 S2 scale-up note, applied already at
MVP for the obvious reason: it keeps per-fold training time well inside the `<30 min` budget on CPU) while
XGBoost trains on the full weekly+month-end grid; `app.ml.ensemble` blends on their common (month-end) dates.

Hyperparameters are not searched across folds here (no outer grid over the whole walk-forward schedule): fixed,
modest defaults are used throughout (`models/xgb.py: DEFAULT_PARAMS`, `models/ft_transformer.py: FTConfig`
defaults) to avoid the much larger risk, at this data volume (~700 names, price+macro features only, honestly
weak expected signal), of fitting hyperparameters to walk-forward noise. This is a documented simplification of
the brief's "hyperparameter search ... nested inside training folds only", not a silent omission.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from scipy.special import ndtri

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, write_table
from app.ml import evaluate
from app.ml.features import live_feature_ids, load_registry
from app.ml.labels import DEFAULT_UNIVERSES
from app.ml.models import ft_transformer as ftm
from app.ml.models import xgb as xgbm
from app.scheduler.base import JobCancelled, new_run_id

log = get_logger(__name__)

HORIZON_DAYS = {"1M": 21, "3M": 63, "6M": 126}
PERIODS_PER_YEAR = {"1M": 12.0, "3M": 4.0, "6M": 2.0}


# ------------------------------------------------------------------------------------------------ data


def load_training_frame(horizon: str, settings: Settings, universe_ids: tuple[str, ...] = DEFAULT_UNIVERSES
                        ) -> pl.DataFrame:
    feat = read_table("ml_features", settings=settings).filter(pl.col("in_universe"))
    lab = read_table("ml_labels", settings=settings).filter(pl.col("horizon") == horizon)
    df = feat.join(lab, on=["date", "security_id"], how="inner")
    return df.sort(["date", "security_id"])


def cs_transform(df: pl.DataFrame, registry: dict[str, Any] | None = None) -> pl.DataFrame:
    """Cross-sectional transform of every *live* feature, per date: rank-gauss (default), z-score, or passthrough
    ('none'), per the registry. Nulls stay null (XGBoost treats NaN as missing; the Transformer fills 0
    downstream).

    'none' exists for features that are broadcast identically to every security on a date (the macro regime
    probabilities: there is no cross-sectional distribution to rank or z-score against). 'zscore' guards against
    a zero cross-sectional std with a null (not a 0/0 NaN) for the same reason -- a feature wrongly registered
    as 'zscore' despite having no cross-sectional variance on some dates must not silently poison every row
    downstream with NaN (XGBoost tolerates NaN-as-missing, but the Transformer's NaN-fill only catches nulls,
    not floating-point NaN, so a stray 0/0 here previously propagated through the whole network and corrupted
    every prediction -- see tests/ml/test_train_smoke.py::test_cs_transform_handles_date_constant_features)."""
    reg = registry or load_registry()
    exprs = []
    for fid in live_feature_ids(reg):
        col, kind = f"f_{fid}", reg["by_id"][fid]["transform"]
        present = pl.col(col).is_not_null()
        n = present.sum().over("date")
        if kind == "none":
            exprs.append(pl.col(col))
            continue
        if kind == "cs_rank_gauss":
            pct = ((pl.col(col).rank(method="average").over("date") - 0.5) / n).clip(1e-4, 1 - 1e-4)
            expr = pct.map_batches(lambda s: pl.Series(values=ndtri(s.to_numpy())), return_dtype=pl.Float64)
        else:  # zscore
            std = pl.col(col).std().over("date")
            expr = pl.when(std > 1e-12).then((pl.col(col) - pl.col(col).mean().over("date")) / std).otherwise(None)
        exprs.append(pl.when(present & (n >= 5)).then(expr).otherwise(None).alias(col))
    return df.with_columns(exprs)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, [31, 29 if y % 4 == 0 and (y % 100 != 0 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30,
                      31, 30, 31][m - 1])
    return date(y, m, day)


@dataclass
class Fold:
    fold_id: str
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def walk_forward_folds(dates: list[date], horizon: str, min_train_years: int = 5, test_months: int = 24,
                       step_months: int = 24, embargo_days: int = 10) -> list[Fold]:
    dates = sorted(set(dates))
    if not dates:
        return []
    purge = int(round(HORIZON_DAYS[horizon] * 7 / 5)) + embargo_days
    start, last = dates[0], dates[-1]
    train_end = _add_months(start, min_train_years * 12)
    folds = []
    i = 0
    while True:
        test_start = train_end + timedelta(days=purge)
        test_end = _add_months(test_start, test_months)
        if test_start > last:
            break
        test_end = min(test_end, last)
        folds.append(Fold(f"F{i}", start, train_end, test_start, test_end))
        train_end = _add_months(train_end, step_months)
        i += 1
    return folds


# ------------------------------------------------------------------------------------------------ leakage guard


_LABEL_LEAK_PATTERN = re.compile(r"(?i)\b(label|rank_label|fwd_return|fwd_excess|target|decile)\b")


def assert_no_label_leakage(feature_cols: list[str], registry: dict[str, Any] | None = None) -> None:
    """Hard guard, run at the top of every walk-forward/permutation-check call: `feature_cols` must be exactly
    the registry's live `f_*` ids (no extras slipped in, nothing missing), and no column name may match a
    label-derived pattern (label, rank_label, fwd_return, fwd_excess*, target, decile). This is the cheap,
    static half of the leakage defense; `run_permutation_leakage_check` below is the empirical half -- added
    per an orchestrator-requested audit after the 3M Transformer's OOS numbers (rank-IC 0.143, hit-rate 97%)
    were judged implausible. Neither check found an actual leak (see that function's docstring + the audit
    trail in model_runs.notes for the retrained run), but both now run on every training call going forward."""
    reg = registry or load_registry()
    expected = {f"f_{fid}" for fid in live_feature_ids(reg)}
    actual = set(feature_cols)
    if actual != expected:
        raise AssertionError(f"feature_cols != registry live features: missing={expected - actual} "
                             f"extra={actual - expected}")
    bad = [c for c in feature_cols if _LABEL_LEAK_PATTERN.search(c)]
    if bad:
        raise AssertionError(f"feature_cols contain label-derived names: {bad}")


def permuted_rank_label(df: pl.DataFrame, seed: int = 0) -> pl.DataFrame:
    """Shuffle `rank_label` across securities *within each date* (preserves each date's label distribution,
    destroys the security<->future-return mapping a model could otherwise exploit). Only ever used by the
    permutation leakage test, never by real training."""
    rng = np.random.default_rng(seed)
    parts = []
    for _, g in df.group_by("date", maintain_order=True):
        vals = g["rank_label"].to_numpy().copy()
        idx = np.where(~np.isnan(vals))[0]
        shuffled = vals.copy()
        shuffled[idx] = vals[rng.permutation(idx)]
        parts.append(g.with_columns(pl.Series("rank_label", shuffled)))
    return pl.concat(parts)


def run_permutation_leakage_check(horizon: str, model_type: str, settings: Settings, train_months: int = 36,
                                  test_months: int = 6, max_epochs: int = 4, seed: int = 0) -> dict[str, Any]:
    """Definitive empirical leakage test (coordinator spec): train on labels shuffled *within each training
    date*, so the model can learn no true security<->future-return relationship from the target -- only from
    whatever is in the *features*. Score the real, unpermuted test fold normally. If any feature leaks future
    information (through any channel: a mis-dated upstream table, a lag computed the wrong direction, a label
    column slipping into the feature set, cross-date normalization, early stopping on the test fold, ...), the
    model can still find spurious structure and OOS rank-IC against the true labels will be non-zero even
    though it was never shown a true target. Pass bar: |rank_IC| < 0.01. A short recent train/test window (not
    the full 9-fold schedule) is used deliberately: this is a diagnostic, not a performance claim, and the
    full walk-forward is ~60-115 min/run on this box for the Transformer."""
    registry = load_registry()
    feature_cols = [f"f_{fid}" for fid in live_feature_ids(registry)]
    assert_no_label_leakage(feature_cols, registry)
    df = load_training_frame(horizon, settings)
    df = cs_transform(df, registry)
    if model_type == "ft_transformer":
        me_dates = _month_end_dates(df["date"].unique().to_list())
        df = df.filter(pl.col("date").is_in(list(me_dates)))
    dates = sorted(df["date"].unique().to_list())
    if not dates:
        return {"status": "skipped", "message": "no data"}
    purge = int(round(HORIZON_DAYS[horizon] * 7 / 5)) + 10
    test_end = dates[-1]
    test_start = _add_months(test_end, -test_months)
    train_end = test_start - timedelta(days=purge)
    train_start = _add_months(train_end, -train_months)
    tr = df.filter((pl.col("date") >= train_start) & (pl.col("date") <= train_end)).drop_nulls("rank_label")
    te = df.filter((pl.col("date") >= test_start) & (pl.col("date") <= test_end)).drop_nulls("rank_label")
    if tr.is_empty() or te.is_empty():
        return {"status": "skipped", "message": "empty train/test window for this diagnostic's date range"}
    tr_perm = permuted_rank_label(tr, seed=seed)
    sector_vocab = _sector_vocab(df)
    if model_type == "xgboost_rank":
        _model, pred = run_xgb_fold(tr_perm, te, feature_cols)
    else:
        _model, pred, _info = run_transformer_fold(tr_perm, te, feature_cols, sector_vocab, max_epochs=max_epochs)
    ev = evaluate.evaluate_predictions(pred.drop_nulls(["score", "fwd_excess_sector"]), horizon)
    ric = ev["summary"]["rank_ic_mean"]
    passed = bool(ric is not None and not np.isnan(ric) and abs(ric) < 0.01)
    return {"status": "success", "rank_ic_mean": ric, "passed": passed, "n_train_dates": tr["date"].n_unique(),
           "n_test_dates": te["date"].n_unique(), "train_window": (train_start, train_end),
           "test_window": (test_start, test_end)}


# ------------------------------------------------------------------------------------------------ xgb path


def _xy(df: pl.DataFrame, feature_cols: list[str], target: str = "rank_label") -> tuple[np.ndarray, np.ndarray]:
    # float32 (not float64): this shared machine runs several agents' jobs concurrently and has been observed
    # to hit XGBoost "bad allocation" under memory pressure; halving the design-matrix footprint is cheap
    # insurance and costs nothing in accuracy at this feature scale (~20 cols).
    X = df.select(feature_cols).to_numpy().astype(np.float32)
    y = df[target].to_numpy().astype(np.float32)
    return X, y


def run_xgb_fold(train_df: pl.DataFrame, test_df: pl.DataFrame, feature_cols: list[str]) -> tuple[Any, pl.DataFrame]:
    tr = train_df.drop_nulls("rank_label")
    X, y = _xy(tr, feature_cols)
    model = xgbm.fit(X, y)
    Xte, _ = _xy(test_df, feature_cols)
    score = xgbm.predict(model, Xte)
    pred = test_df.select("date", "security_id", "fwd_return", "fwd_excess_sector", "rank_label").with_columns(
        pl.Series("score", score))
    return model, pred


# ------------------------------------------------------------------------------------------------ transformer path


def _month_end_dates(dates: list[date]) -> set[date]:
    df = pl.DataFrame({"date": dates}).with_columns(pl.col("date").dt.truncate("1mo").alias("m"))
    return set(df.group_by("m").agg(pl.col("date").max())["date"].to_list())


def _sector_vocab(df: pl.DataFrame) -> dict[str, int]:
    sectors = sorted(df["sector_id"].drop_nulls().unique().to_list())
    return {s: i for i, s in enumerate(sectors)}


def _build_batches(df: pl.DataFrame, feature_cols: list[str], sector_vocab: dict[str, int]) -> list[ftm.DateBatch]:
    """df must be sorted by [security_id, date] with f_* already cs-transformed. Builds one DateBatch per date,
    with x_lag1/x_lag2 = that security's previous 1/2 *rows in this same (month-end-only) frame*."""
    d = df.with_columns(
        *[pl.col(c).shift(1).over("security_id").alias(f"__lag1_{c}") for c in feature_cols],
        *[pl.col(c).shift(2).over("security_id").alias(f"__lag2_{c}") for c in feature_cols],
    )
    fill_cols = feature_cols + [f"__lag1_{c}" for c in feature_cols] + [f"__lag2_{c}" for c in feature_cols]
    # fill_null() only catches missing values; a stray floating-point NaN (e.g. from a mis-registered transform
    # dividing by a zero cross-sectional std) is a distinct IEEE value that survives fill_null and would
    # silently poison every downstream prediction once torch acts on it. Belt-and-suspenders on top of
    # cs_transform's own guard (which is the real fix): replace any remaining NaN with the same neutral 0.0.
    d = d.with_columns([pl.when(pl.col(c).is_null() | pl.col(c).is_nan()).then(0.0).otherwise(pl.col(c)).alias(c)
                       for c in fill_cols])
    n_unk = len(sector_vocab)
    sec_idx = d["sector_id"].map_elements(lambda s: sector_vocab.get(s, n_unk), return_dtype=pl.Int64)
    d = d.with_columns(sec_idx.alias("__sector_idx"))
    batches = []
    for dt, g in d.sort("date").group_by("date", maintain_order=True):
        dt0 = dt[0] if isinstance(dt, tuple) else dt
        batches.append(ftm.DateBatch(
            date=dt0, x=g.select(feature_cols).to_numpy().astype(np.float32),
            x_lag1=g.select([f"__lag1_{c}" for c in feature_cols]).to_numpy().astype(np.float32),
            x_lag2=g.select([f"__lag2_{c}" for c in feature_cols]).to_numpy().astype(np.float32),
            sector_idx=g["__sector_idx"].to_numpy().astype(np.int64),
            y=g["rank_label"].fill_null(0.5).to_numpy().astype(np.float32),
            security_ids=g["security_id"].to_list(),
        ))
    return batches


def run_transformer_fold(train_df: pl.DataFrame, test_df: pl.DataFrame, feature_cols: list[str],
                         sector_vocab: dict[str, int], max_epochs: int = 12) -> tuple[Any, pl.DataFrame, dict]:
    tr = train_df.drop_nulls("rank_label").sort(["security_id", "date"])
    all_dates = sorted(tr["date"].unique().to_list())
    n_val = max(1, int(len(all_dates) * 0.15))
    val_dates, fit_dates = set(all_dates[-n_val:]), set(all_dates[:-n_val])
    batches = _build_batches(tr, feature_cols, sector_vocab)
    fit_b = [b for b in batches if b.date in fit_dates and len(b.y) >= 10]
    val_b = [b for b in batches if b.date in val_dates and len(b.y) >= 10]
    model, info = ftm.train_model(fit_b, val_b, n_features=len(feature_cols), n_sectors=len(sector_vocab),
                                  max_epochs=max_epochs)
    te = test_df.sort(["security_id", "date"])
    test_batches = _build_batches(te, feature_cols, sector_vocab)
    preds = ftm.predict_batches(model, test_batches)
    rows = []
    for b in test_batches:
        s = preds[b.date]
        for sid, sc in zip(b.security_ids, s, strict=True):
            rows.append({"date": b.date, "security_id": sid, "score": float(sc)})
    pred = pl.DataFrame(rows).join(
        te.select("date", "security_id", "fwd_return", "fwd_excess_sector", "rank_label"),
        on=["date", "security_id"], how="left")
    return model, pred, info


# ------------------------------------------------------------------------------------------------ orchestration


def _fit_fold_with_retry(model_type: str, tr: pl.DataFrame, te: pl.DataFrame, feature_cols: list[str],
                         sector_vocab: dict[str, int], attempts: int = 3, backoff_s: float = 20.0
                         ) -> tuple[Any, pl.DataFrame]:
    """This box runs several agents' training/ingestion jobs concurrently and has been observed to run low on
    free memory (XGBoost raises a transient 'bad allocation'); retry with backoff rather than losing an
    otherwise-good walk-forward run to someone else's momentary memory spike."""
    import time as _time  # noqa: PLC0415

    last: Exception | None = None
    for attempt in range(attempts):
        try:
            if model_type == "xgboost_rank":
                return run_xgb_fold(tr, te, feature_cols)
            model, pred, _info = run_transformer_fold(tr, te, feature_cols, sector_vocab)
            return model, pred
        except Exception as e:  # noqa: BLE001 - XGBoostError/MemoryError are not a stable importable type across versions
            last = e
            if attempt == attempts - 1:
                raise
            log.warning("fold fit attempt %d/%d failed (%s), retrying in %.0fs", attempt + 1, attempts, e, backoff_s)
            _time.sleep(backoff_s)
    raise last  # pragma: no cover - unreachable, satisfies type checkers


def run_walk_forward(horizon: str, model_type: str, settings: Settings, ctx: Any = None,
                     min_train_years: int = 5, test_months: int = 24, step_months: int = 24) -> dict[str, Any]:
    registry = load_registry()
    feature_cols = [f"f_{fid}" for fid in live_feature_ids(registry)]
    assert_no_label_leakage(feature_cols, registry)
    df = load_training_frame(horizon, settings)
    df = cs_transform(df, registry)
    if model_type == "ft_transformer":
        me_dates = _month_end_dates(df["date"].unique().to_list())
        df = df.filter(pl.col("date").is_in(list(me_dates)))
    all_dates = df["date"].unique().to_list()
    folds = walk_forward_folds(all_dates, horizon, min_train_years, test_months, step_months)
    if not folds:
        return {"status": "skipped", "message": "not enough history for one walk-forward fold yet"}
    sector_vocab = _sector_vocab(df)
    oos_frames, series_rows, fold_times, fold_records = [], [], [], []
    for fold in folds:
        if ctx is not None:
            ctx.check_cancelled()
        tr = df.filter((pl.col("date") >= fold.train_start) & (pl.col("date") <= fold.train_end))
        te = df.filter((pl.col("date") >= fold.test_start) & (pl.col("date") <= fold.test_end))
        if tr.is_empty() or te.is_empty():
            continue
        t0 = datetime.now(UTC)
        model, pred = _fit_fold_with_retry(model_type, tr, te, feature_cols, sector_vocab)
        fold_times.append((datetime.now(UTC) - t0).total_seconds())
        oos_frames.append(pred.with_columns(pl.lit(fold.fold_id).alias("fold_id")))
        fold_metrics = evaluate.evaluate_predictions(pred.drop_nulls(["score", "fwd_excess_sector"]), horizon)
        evaluate.add_fold_metrics(series_rows, fold.fold_id, fold.test_end, fold_metrics["summary"]["rank_ic_mean"])
        # per-fold train/test windows + sharpe live in model_runs.fold_metrics (JSON), not model_eval_series:
        # a Sharpe ratio stored under metric='fold_rank_ic' was exactly the bug found via frontend verification
        # (values >1 that aren't a correlation) -- see evaluate.add_fold_metrics docstring.
        fold_records.append({
            "fold_id": fold.fold_id, "train_start": fold.train_start.isoformat(), "train_end": fold.train_end.isoformat(),
            "test_start": fold.test_start.isoformat(), "test_end": fold.test_end.isoformat(),
            "n_train": tr.height, "n_test": te.height, "rank_ic": fold_metrics["summary"]["rank_ic_mean"],
            "sharpe": fold_metrics["summary"]["long_short_sharpe"], "seconds": fold_times[-1],
        })
        log.info("train %s %s fold %s: train=[%s..%s] test=[%s..%s] n_train=%d n_test=%d rank_ic=%.4f (%.1fs)",
                 model_type, horizon, fold.fold_id, fold.train_start, fold.train_end, fold.test_start, fold.test_end,
                 tr.height, te.height, fold_metrics["summary"]["rank_ic_mean"], fold_times[-1])
    if not oos_frames:
        return {"status": "skipped", "message": "no fold produced OOS predictions"}
    oos = pl.concat(oos_frames, how="diagonal_relaxed")
    # Adjacent expanding-window folds can share a boundary date (test_end of fold i == test_start of fold i+1,
    # e.g. when a month-length rounding coincidence lines up): dedupe on the ml_predictions PK, keeping the
    # later fold's prediction (the more-recently-retrained model's). Sort by the numeric fold index (not the
    # "F0".."F10" string, which would sort "F10" before "F2").
    oos = oos.with_columns(pl.col("fold_id").str.strip_prefix("F").cast(pl.Int32).alias("__fold_n"))
    oos = oos.sort("__fold_n").unique(subset=["date", "security_id"], keep="last", maintain_order=True).drop("__fold_n")
    full_eval = evaluate.evaluate_predictions(oos.drop_nulls(["score", "fwd_excess_sector"]), horizon)
    series_rows = series_rows + full_eval["series"]

    # live prediction: refit on ALL available history through the latest date, predict the latest date(s)
    latest_date = df["date"].max()
    live_tr = df.drop_nulls("rank_label")
    if model_type == "xgboost_rank":
        live_model, _ = _fit_fold_with_retry(model_type, live_tr, df.filter(pl.col("date") == latest_date),
                                             feature_cols, sector_vocab)
        Xlive, _ = _xy(df.filter(pl.col("date") == latest_date), feature_cols)
        live_score = xgbm.predict(live_model, Xlive)
    else:
        live_model, _info = ftm.train_model(
            _build_batches(live_tr.sort(["security_id", "date"]), feature_cols, sector_vocab),
            [], n_features=len(feature_cols), n_sectors=len(sector_vocab), max_epochs=6)
        lb = _build_batches(df.filter(pl.col("date") == latest_date).sort(["security_id", "date"]), feature_cols,
                           sector_vocab)
        preds = ftm.predict_batches(live_model, lb)
        live_score = preds.get(latest_date, np.array([]))
    live_df = df.filter(pl.col("date") == latest_date).select("date", "security_id").with_columns(
        pl.Series("score", live_score))

    fsv = df.select("feature_set_version").head(1)
    return {
        "status": "success", "oos": oos, "live": live_df, "model": live_model, "feature_cols": feature_cols,
        "summary": full_eval["summary"], "series_rows": series_rows, "folds": folds, "sector_vocab": sector_vocab,
        "fold_seconds": fold_times, "live_X": Xlive if model_type == "xgboost_rank" else None,
        "feature_set_version": fsv["feature_set_version"][0] if fsv.height else "unknown",
        "fold_records": fold_records, "method_note": full_eval["method_note"],
    }


def _write_oos_predictions(model_run_id: str, oos: pl.DataFrame, horizon: str, settings: Settings) -> int:
    """OOS rows only: expected_excess_return/interval/confidence are null here by design (the realized
    fwd_excess_sector, already in `oos`/model_eval_series, is the better 'how good was this prediction' signal
    for backtested rows; calibration is reserved for live rows, see app.ml.predict)."""
    now = datetime.now(UTC)
    d = oos.with_columns(
        (pl.col("score").rank(method="average").over("date") / pl.col("score").count().over("date")).alias("rank_pct"))
    d = d.with_columns((pl.col("rank_pct") * 10).ceil().clip(1, 10).cast(pl.Int8).alias("decile"))
    d = d.select(
        pl.lit(model_run_id).alias("model_run_id"), "date", "security_id", pl.lit(horizon).alias("horizon"),
        "score", "rank_pct", "decile", pl.lit(True).alias("is_oos"), pl.lit(False).alias("is_live"),
        pl.lit(None, dtype=pl.Utf8).alias("shap_top"), pl.lit(None, dtype=pl.Float64).alias("expected_excess_return"),
        pl.lit(None, dtype=pl.Float64).alias("interval_low"), pl.lit(None, dtype=pl.Float64).alias("interval_high"),
        pl.lit(None, dtype=pl.Float64).alias("confidence"), pl.lit(None, dtype=pl.Datetime("us", "UTC")).alias("trained_at"),
        pl.lit(None, dtype=pl.Utf8).alias("feature_set_version"), pl.lit(now).alias("computed_at"))
    return write_table(d, "ml_predictions", settings=settings)


SURVIVORSHIP_NOTE = (
    "Survivorship: universe_membership is a current-only snapshot at this stage of the build (no historical "
    "valid_from/valid_to intervals yet), so labels/features apply today's constituent list retroactively; "
    "yfinance (the price source for most pre-Polygon history) does not carry tickers that delisted before we "
    "started ingesting them, so names that disappeared long ago are simply absent rather than scored with a "
    "terminal loss. Names we DO observe delisting for get a flat zero-drift continuation (ml_labels.delisted_in_window)."
)


def run_training(horizon: str, model_type: str, ctx: Any, settings: Settings) -> dict[str, Any]:
    from app.ml import predict as P  # noqa: PLC0415 - circular at module scope, see app.ml.predict's note

    out = run_walk_forward(horizon, model_type, settings, ctx=ctx)
    if out["status"] != "success":
        return out
    run_id = new_run_id("M")
    n_pred = _write_oos_predictions(run_id, out["oos"], horizon, settings)
    series_df = evaluate.series_rows_to_frame(out["series_rows"], run_id)
    n_series = write_table(series_df, "model_eval_series", settings=settings)
    importance = None
    if model_type == "xgboost_rank" and out.get("live_X") is not None and len(out["live_X"]):
        try:
            importance = xgbm.feature_importance(out["model"], out["feature_cols"], out["live_X"])
        except Exception as e:  # noqa: BLE001 - importance is best-effort, never fails the training job
            log.warning("feature_importance failed: %s", e)
    if model_type == "xgboost_rank":
        artifact_path = P.save_xgb_artifact(out["model"], run_id, settings)
    else:
        artifact_path = P.save_transformer_artifact(out["model"], out["sector_vocab"], out["feature_cols"],
                                                     run_id, settings)
    created_at = datetime.now(UTC)
    model_runs_row = pl.DataFrame([{
        "model_run_id": run_id, "model_type": model_type, "horizon": horizon, "universe_id": "mvp_us+mvp_global",
        "feature_set_version": out["feature_set_version"], "train_start": out["folds"][0].train_start,
        "train_end": out["folds"][-1].train_end, "oos_start": out["folds"][0].test_start,
        "oos_end": out["folds"][-1].test_end,
        "cv_scheme": json.dumps({"type": "expanding", "n_folds": len(out["folds"]),
                                               "test_months": 24, "purge_days": HORIZON_DAYS[horizon],
                                               "embargo_days": 10}),
        "params": json.dumps(xgbm.DEFAULT_PARAMS if model_type == "xgboost_rank"
                                           else {"d_model": 32, "n_heads": 4, "n_layers": 2,
                                                "n_features": len(out["feature_cols"])}),
        "ic_mean": out["summary"]["ic_mean"], "rank_ic_mean": out["summary"]["rank_ic_mean"],
        "rank_ic_ir": out["summary"]["rank_ic_ir"], "rank_ic_tstat": out["summary"]["rank_ic_tstat"],
        "decile_spread_ann": out["summary"]["decile_spread_ann"], "long_short_sharpe": out["summary"]["long_short_sharpe"],
        "turnover_monthly": out["summary"]["turnover_monthly"], "hit_rate": out["summary"]["hit_rate"],
        "status": "completed", "is_production": False,
        "artifact_path": artifact_path, "feature_importance": json.dumps(importance) if importance else None,
        "fold_metrics": json.dumps(out["fold_records"]),
        "notes": (f"{len(out['folds'])} walk-forward folds, {sum(out['fold_seconds']):.0f}s total. "
                 f"{out['method_note']} {SURVIVORSHIP_NOTE}"),
        "created_at": created_at, "finished_at": created_at,
    }])
    write_table(model_runs_row, "model_runs", settings=settings)
    model_run_dict = model_runs_row.to_dicts()[0]
    try:
        calibrated = P.calibrate_live(run_id, out["live"], settings)
        n_pred += P.write_live(model_run_dict, calibrated, settings,
                              P.shap_top_xgb(out["model"], out["live_X"], out["feature_cols"])
                              if model_type == "xgboost_rank" and out.get("live_X") is not None else None)
    except Exception as e:  # noqa: BLE001 - live calibration is best-effort, OOS results already committed
        log.warning("live calibration/write failed for %s: %s", run_id, e)
    return {"status": "success", "model_run_id": run_id, "rows": {"ml_predictions": n_pred,
            "model_eval_series": n_series}, "summary": out["summary"], "n_folds": len(out["folds"])}


def run(ctx):  # noqa: ANN001 - JobContext
    """Jobs `ml_train_xgb` / `ml_train_transformer`. params: model_type (required), horizon (default: all 3)."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    model_type = p.get("model_type", "xgboost_rank")
    horizons = [p["horizon"]] if p.get("horizon") else list(HORIZON_DAYS)
    results, failed = {}, 0
    for h in horizons:
        try:
            out = run_training(h, model_type, ctx, ctx.settings)
            results[h] = out
        except JobCancelled:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("train %s %s failed", model_type, h)
            results[h] = {"status": "failed", "message": str(e)}
            failed += 1
    rows = {"model_runs": sum(1 for r in results.values() if r.get("status") == "success"),
           "ml_predictions": sum(r.get("rows", {}).get("ml_predictions", 0) for r in results.values()),
           "model_eval_series": sum(r.get("rows", {}).get("model_eval_series", 0) for r in results.values())}
    return JobResult(status="partial" if failed else "success", rows_written=rows,
                     entities_ok=len(horizons) - failed, entities_failed=failed,
                     watermark=ctx.as_of.isoformat(), message=str({h: r.get("status") for h, r in results.items()}))
