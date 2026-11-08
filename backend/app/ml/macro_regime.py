"""Point-in-time macro regime model: Gaussian HMM (4 states) on monthly macro factors.

Jobs (contracts/jobs.yaml):
  * `macro_regime_fit`     (weekly, params.mode='fit')    -> refits the HMM on the full PIT history, writes the
    whole monthly regime path to `macro_regimes` + a `model_runs` row, and persists the fitted parameters under
    `models_store/macro_hmm_<region_scope>/latest.joblib` for the nowcast job to reuse.
  * `macro_regime_nowcast` (nightly, params.mode='filter') -> loads the latest persisted fit (no re-estimation) and
    appends one *daily* nowcast row (ffill of the latest known monthly factor values) for `ctx.as_of`.

PIT safety
----------
* Inputs are read from `macro_observations` with `knowledge_date <= as_of` via `app.normalize.pit.asof`, so ALFRED
  vintages (GDP, CPI, payrolls, ...) use the value that was actually public on `as_of`, not the latest revision.
* State probabilities are the HMM **forward filter** (alpha pass), computed from scratch in `forward_filter()`
  below rather than via `hmmlearn`'s smoother (`predict_proba` / `score_samples` run the forward-*backward*
  algorithm, which uses observations *after* t to infer the state at t -- a direct look-ahead leak). The filter at
  month t uses only observations <= t, which is what the contract means by "filtered (not smoothed)".
* The standardization (z-score) constants and the HMM parameters themselves are estimated only from the fit
  window's history up to `as_of` (the `fit` job's `as_of`), never refreshed inside the `filter` job, so a nightly
  nowcast cannot change the shape of the historical path (idempotent nowcast, no retroactive relabeling other than
  through the next weekly refit, which is the explicit "stability" boundary documented in `model_runs.notes`).

Known limitations (documented honestly, not hidden):
* `GLOBAL` scope reuses the US high-yield OAS, VIX and broad-USD series for credit/vol/usd factors (no good
  single global daily series for these at MVP); only growth (OECD CLI panel) and inflation (euro-area HICP) are
  genuinely global.
* Regional scopes (O2.2c / audit F27, MODEL-BASED like every scope): EU (euro area), JP, CN and KR use only official
  series already ingested - Eurostat / ECB (industrial production, core HICP, 10Y and 3M rates, CISS stress index),
  OECD (KEI industrial production, CPI, interest rates, composite leading indicator), IMF CPI, BIS nominal effective
  exchange rates. Factors without an official regional series are left out rather than proxied with US data: no
  long official euro / Asian credit-spread history exists in the lake (ICE BofA euro HY on FRED starts 2023), so the
  regional models have no `credit` factor; `fx` (effective exchange rate, m/m log change) replaces the US-only
  broad-dollar factor and `stress` (ECB CISS) replaces VIX where it exists. The 4-state labelling heuristic is the
  same (growth / inflation centroids), so state meanings are comparable but each scope is fitted on its own history.
* hmmlearn's EM is a local optimizer: we fit several random seeds and keep the highest log-likelihood, then align
  the new states to the *previous* persisted fit's centroids (nearest-centroid matching in standardized feature
  space) so `regime_id` doesn't flip meaning between weekly refits purely from label switching.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import polars as pl
from scipy.special import logsumexp

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, write_table
from app.normalize import pit

log = get_logger(__name__)

N_STATES = 4
FEATURES = ["growth", "inflation", "curve", "credit", "vol", "usd", "fx", "stress"]

#: factor -> (series_id, default_transform). transform applied here (macro_observations stores raw values; the
#: transform recorded in macro_series.default_transform tells us *how*, not pre-applied).
US_SERIES: dict[str, tuple[str, str]] = {
    "growth": ("fred:INDPRO", "yoy"),
    "inflation": ("fred:CPILFESL", "yoy"),
    "curve": ("fred:T10Y2Y", "level"),
    "credit": ("fred:BAA10Y", "level"),
    "vol": ("fred:VIXCLS", "level"),
    "usd": ("fred:DTWEXBGS", "log_diff"),
}
GLOBAL_SERIES: dict[str, tuple[str, str]] = {
    "growth": ("fred:G7LOLITOAASTSAM", "level"),          # OECD composite leading indicator, G7
    "inflation": ("fred:CP0000EZ19M086NEST", "yoy"),       # euro-area HICP
    "curve": ("fred:IRLTLT01DEM156N", "level"),            # DE 10y (paired with ECBDFR below)
    "credit": ("fred:BAA10Y", "level"),                    # proxy, documented limitation
    "vol": ("fred:VIXCLS", "level"),                       # proxy, documented limitation
    "usd": ("fred:DTWEXBGS", "log_diff"),                  # proxy, documented limitation
}
# Regional scopes (F27). Values of Eurostat / ECB / OECD "y/y" series are already growth rates in percent ->
# transform "level" (the HMM standardises every factor, so percent vs fraction does not matter).
EU_SERIES: dict[str, tuple[str, str]] = {
    "growth": ("eurostat:sts_inpr_m.EA.PCH_SM", "level"),          # euro-area industrial production y/y (Eurostat)
    "inflation": ("ecb:HICP.M.U2.N.XEF000.4D0.ANR", "level"),      # euro-area core HICP y/y (ECB)
    "curve": ("ecb:FM.M.U2.EUR.4F.BB.U2_10Y.YLD", "level"),        # 10Y benchmark yield minus 3M Euribor (below)
    "stress": ("ecb:CISS.D.U2.Z0Z.4F.EC.SS_CIN.IDX", "level"),     # ECB composite indicator of systemic stress
    "fx": ("bis:WS_EER.M.N.B.XM", "log_diff"),                     # euro nominal effective exchange rate (BIS)
}
JP_SERIES: dict[str, tuple[str, str]] = {
    "growth": ("oecd:KEI.JPN.PRVM.GY", "level"),                   # industrial production y/y (OECD KEI)
    "inflation": ("oecd:PRICES.JPN.CORE", "level"),                # core CPI y/y (OECD)
    "curve": ("oecd:KEI.JPN.IRLT", "level"),                       # 10Y JGB minus 3M interbank (below)
    "fx": ("bis:WS_EER.M.N.B.JP", "log_diff"),                     # yen nominal effective exchange rate (BIS)
}
CN_SERIES: dict[str, tuple[str, str]] = {
    "growth": ("oecd:CLI.CHN.LI", "level"),                        # OECD composite leading indicator (amplitude adj.)
    "inflation": ("imf:CPI.CHN.YOY", "level"),                     # CPI y/y (IMF)
    "curve": ("oecd:KEI.CHN.IRLT", "level"),                       # 10Y CGB minus 3M interbank (below)
    "stress": ("ecb:CISS.D.CN.Z0Z.4F.EC.SS_CIN.IDX", "level"),     # CISS for China (ECB)
    "fx": ("bis:WS_EER.M.N.B.CN", "log_diff"),                     # renminbi nominal effective exchange rate (BIS)
}
KR_SERIES: dict[str, tuple[str, str]] = {
    "growth": ("oecd:KEI.KOR.PRVM.GY", "level"),                   # industrial production y/y (OECD KEI)
    "inflation": ("oecd:PRICES99.KOR.CORE", "level"),              # core CPI y/y (OECD)
    "curve": ("oecd:KEI.KOR.IRLT", "level"),                       # 10Y KTB minus 3M interbank (below)
    "fx": ("bis:WS_EER.M.N.B.KR", "log_diff"),                     # won nominal effective exchange rate (BIS)
}
REGION_SERIES: dict[str, dict[str, tuple[str, str]]] = {"US": US_SERIES, "GLOBAL": GLOBAL_SERIES, "EU": EU_SERIES,
                                                         "JP": JP_SERIES, "CN": CN_SERIES, "KR": KR_SERIES}
EXTRA_GLOBAL_CURVE_SHORT = "fred:ECBDFR"  # subtracted from DE 10y to get a EU curve slope
#: short leg subtracted from each scope's `curve` series (curve = long yield - short rate, percentage points)
CURVE_SHORT_LEG: dict[str, str] = {"GLOBAL": EXTRA_GLOBAL_CURVE_SHORT, "EU": "ecb:FM.M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA",
                                   "JP": "oecd:KEI.JPN.IR3TIB", "CN": "oecd:KEI.CHN.IR3TIB", "KR": "oecd:KEI.KOR.IR3TIB"}
#: true unit of each regional raw factor value (API drivers, F08): Eurostat / ECB / OECD / IMF y/y series are percent
REGION_UNITS: dict[str, dict[str, str]] = {
    "EU": {"growth": "percent", "inflation": "percent", "curve": "percent", "stress": "index", "fx": "fraction"},
    "JP": {"growth": "percent", "inflation": "percent", "curve": "percent", "fx": "fraction"},
    "CN": {"growth": "index", "inflation": "percent", "curve": "percent", "stress": "index", "fx": "fraction"},
    "KR": {"growth": "percent", "inflation": "percent", "curve": "percent", "fx": "fraction"},
}
#: scopes fitted / filtered when the job params give none
DEFAULT_SCOPES = ["US", "GLOBAL", "EU", "JP", "CN", "KR"]
#: official sources behind each scope (model_runs.notes; every regime output is model-based)
SCOPE_SOURCES = {"US": "FRED (Fed / BLS / CBOE / Moody's)", "GLOBAL": "FRED (OECD CLI, Eurostat HICP; US proxies)",
                 "EU": "Eurostat, ECB (HICP, rates, CISS), BIS", "JP": "OECD (KEI, CPI), BIS",
                 "CN": "OECD (CLI, KEI), IMF CPI, ECB CISS, BIS", "KR": "OECD (KEI, CPI), BIS"}

RECESSIONARY_LABELS = ("contraction", "stagflation")


# ------------------------------------------------------------------------------------------------ data loading


def _transform(df: pl.DataFrame, kind: str) -> pl.DataFrame:
    """df: [date, value] sorted by date. Returns [date, value] with `kind` applied."""
    df = df.sort("date")
    if kind == "level":
        return df
    if kind == "diff":
        return df.with_columns((pl.col("value") - pl.col("value").shift(1)).alias("value"))
    if kind == "pct_change":
        return df.with_columns((pl.col("value") / pl.col("value").shift(1) - 1.0).alias("value"))
    if kind == "log_diff":
        return df.with_columns((pl.col("value").log() - pl.col("value").log().shift(1)).alias("value"))
    if kind == "yoy":
        return df.with_columns((pl.col("value") / pl.col("value").shift(12) - 1.0).alias("value"))
    raise ValueError(f"unknown transform {kind!r}")


def _series_asof(series_id: str, as_of: date, settings: Settings) -> pl.DataFrame:
    """PIT snapshot of one macro series: [date, value], one row per observation date, as known at `as_of`."""
    lf = pit.asof(
        __import__("app.core.storage", fromlist=["scan_table"]).scan_table("macro_observations", settings)
        .filter(pl.col("series_id") == series_id),
        as_of, key_cols=["series_id", "date"],
    )
    return lf.select("date", "value").collect().drop_nulls("date")


def load_factor_panel(as_of: date, region_scope: str, settings: Settings | None = None) -> pl.DataFrame:
    """Monthly PIT factor panel [date, growth, inflation, curve, credit, vol, usd] up to `as_of`.

    Daily/weekly source series are resampled to month-end (last observation in the month) *before* applying the
    transform where the transform itself is not already a monthly-frequency operation; this keeps the HMM input
    at a consistent monthly cadence as the contract specifies.
    """
    s = settings or get_settings()
    specs = REGION_SERIES[region_scope]
    cols: list[pl.DataFrame] = []
    for factor, (sid, kind) in specs.items():
        raw = _series_asof(sid, as_of, s)
        if raw.is_empty():
            continue
        monthly = (
            raw.with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
            .sort("date").group_by("month", maintain_order=True).agg(pl.col("value").last())
            .rename({"month": "date"})
        )
        monthly = _transform(monthly, kind).rename({"value": factor})
        cols.append(monthly)
    short_id = CURVE_SHORT_LEG.get(region_scope)
    if short_id:
        short = _series_asof(short_id, as_of, s)
        if not short.is_empty():
            short_m = (short.with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
                       .sort("date").group_by("month", maintain_order=True).agg(pl.col("value").last())
                       .rename({"month": "date", "value": "short_rate"}))
            cols = [c.join(short_m, on="date", how="left") if "curve" in c.columns else c for c in cols]
            for i, c in enumerate(cols):
                if "curve" in c.columns and "short_rate" in c.columns:
                    cols[i] = c.with_columns((pl.col("curve") - pl.col("short_rate")).alias("curve")).drop("short_rate")
    panel = cols[0]
    for c in cols[1:]:
        panel = panel.join(c, on="date", how="full", coalesce=True)
    panel = panel.sort("date").filter(pl.col("date") <= pl.lit(as_of).cast(pl.Date))
    present = [f for f in FEATURES if f in panel.columns]
    all_null = pl.fold(pl.lit(True), lambda a, b: a & b, [pl.col(c).is_null() for c in present])
    return panel.select(["date", *present]).filter(~all_null)


# ------------------------------------------------------------------------------------------------ HMM


def _standardize(X: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mu, sd = np.nanmean(X, axis=0), np.nanstd(X, axis=0)
    sd = np.where(sd < 1e-8, 1.0, sd)
    return (X - mu) / sd, mu, sd


def _gaussian_logpdf_diag(X: np.ndarray, mean: np.ndarray, var: np.ndarray) -> np.ndarray:
    var = np.maximum(var, 1e-6)
    diff = X - mean
    return -0.5 * (np.sum(diff**2 / var, axis=1) + np.sum(np.log(var)) + X.shape[1] * np.log(2 * np.pi))


def forward_filter(startprob: np.ndarray, transmat: np.ndarray, means: np.ndarray, covars: np.ndarray,
                   X: np.ndarray) -> np.ndarray:
    """PIT-safe forward (alpha) pass: filtered P(state_t | obs_1..t). X: (T, D). Returns (T, K)."""
    T, K = X.shape[0], len(startprob)
    logB = np.column_stack([_gaussian_logpdf_diag(X, means[k], covars[k]) for k in range(K)])
    log_trans = np.log(np.maximum(transmat, 1e-300))
    log_alpha = np.empty((T, K))
    log_alpha[0] = np.log(np.maximum(startprob, 1e-300)) + logB[0]
    log_alpha[0] -= logsumexp(log_alpha[0])
    for t in range(1, T):
        log_alpha[t] = logsumexp(log_alpha[t - 1][:, None] + log_trans, axis=0) + logB[t]
        log_alpha[t] -= logsumexp(log_alpha[t])
    return np.exp(log_alpha)


def _fit_best_seed(X: np.ndarray, n_states: int, seeds: tuple[int, ...] = (0, 1, 2, 7, 42)) -> Any:
    from hmmlearn.hmm import GaussianHMM  # noqa: PLC0415

    best, best_ll = None, -np.inf
    for seed in seeds:
        m = GaussianHMM(n_components=n_states, covariance_type="diag", n_iter=300, random_state=seed, tol=1e-4)
        try:
            m.fit(X)
            ll = m.score(X)
        except Exception as e:  # noqa: BLE001 - degenerate covariance on a bad seed
            log.warning("hmm fit seed=%d failed: %s", seed, e)
            continue
        if ll > best_ll:
            best, best_ll = m, ll
    if best is None:
        raise RuntimeError("HMM fit failed for all seeds")
    return best


def _diag_covars(model: Any) -> np.ndarray:
    cv = model.covars_
    return cv if cv.ndim == 2 else np.array([np.diag(c) for c in cv])


def _align_to_reference(means: np.ndarray, ref_means: np.ndarray | None) -> np.ndarray:
    """Index array `idx` such that `means[idx]` is reordered to match `ref_means`'s state ordering (nearest-
    centroid matching, by Euclidean distance in standardized feature space, via the Hungarian algorithm).

    `linear_sum_assignment(cost)` with `cost[r, c] = dist(means[r], ref_means[c])` returns, for square cost,
    `row = arange(k)` and `col[r]` = the reference index that `means[r]` is matched to -- i.e. `col` answers
    "where does row r's centroid belong", not "which row belongs at position c". Since callers index with
    `means[idx]` (answering "which row belongs at position c"), `idx` must be the *inverse* of that matching:
    `idx = argsort(col)` (equivalently `argsort(row)[argsort(col)]`, but row is already sorted). Verified by
    `tests/ml/test_macro_regime.py::test_align_to_reference_recovers_a_shuffled_permutation`.
    """
    k = means.shape[0]
    if ref_means is None or ref_means.shape != means.shape:
        return np.arange(k)
    from scipy.optimize import linear_sum_assignment  # noqa: PLC0415

    cost = np.linalg.norm(means[:, None, :] - ref_means[None, :, :], axis=2)
    row, col = linear_sum_assignment(cost)
    match = np.empty(k, dtype=int)
    match[row] = col  # match[r] = reference index that means[r] belongs to
    return np.argsort(match)


def label_states(means: np.ndarray, transmat: np.ndarray, feature_names: list[str]) -> list[str]:
    """Heuristic economic labels for each (standardized) state centroid.

    1. growth, inflation are z-scored means per state -> rank by growth.
    2. the lowest-growth state is 'stagflation' if its inflation is above the cross-state median, else 'contraction'.
    3. the highest-growth state is 'expansion'.
    4. of the two middle states, the one more likely to transition *into* the low-growth state is 'slowdown';
       the other (more likely to transition into the high-growth state, or out of the low-growth one) is 'recovery'.
    """
    gi = feature_names.index("growth") if "growth" in feature_names else 0
    ii = feature_names.index("inflation") if "inflation" in feature_names else 1
    g, i = means[:, gi], means[:, ii]
    order = np.argsort(g)  # ascending growth
    low, mid1, mid2, high = order
    labels = [""] * len(g)
    labels[low] = "stagflation" if i[low] >= np.median(i) else "contraction"
    labels[high] = "expansion"
    # P(mid -> low) vs P(mid -> high) decides slowdown (heading down) vs recovery (heading up)
    to_low = {mid1: transmat[mid1, low], mid2: transmat[mid2, low]}
    to_high = {mid1: transmat[mid1, high], mid2: transmat[mid2, high]}
    if to_low[mid1] - to_high[mid1] >= to_low[mid2] - to_high[mid2]:
        labels[mid1], labels[mid2] = "slowdown", "recovery"
    else:
        labels[mid1], labels[mid2] = "recovery", "slowdown"
    return labels


@dataclass
class FittedHMM:
    region_scope: str
    feature_names: list[str]
    mu: np.ndarray
    sd: np.ndarray
    startprob: np.ndarray
    transmat: np.ndarray
    means: np.ndarray
    covars: np.ndarray
    state_labels: list[str]
    train_start: date
    train_end: date
    fitted_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "region_scope": self.region_scope, "feature_names": self.feature_names,
            "mu": self.mu.tolist(), "sd": self.sd.tolist(), "startprob": self.startprob.tolist(),
            "transmat": self.transmat.tolist(), "means": self.means.tolist(), "covars": self.covars.tolist(),
            "state_labels": self.state_labels, "train_start": self.train_start.isoformat(),
            "train_end": self.train_end.isoformat(), "fitted_at": self.fitted_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FittedHMM:
        return cls(
            region_scope=d["region_scope"], feature_names=d["feature_names"],
            mu=np.array(d["mu"]), sd=np.array(d["sd"]), startprob=np.array(d["startprob"]),
            transmat=np.array(d["transmat"]), means=np.array(d["means"]), covars=np.array(d["covars"]),
            state_labels=d["state_labels"], train_start=date.fromisoformat(d["train_start"]),
            train_end=date.fromisoformat(d["train_end"]), fitted_at=datetime.fromisoformat(d["fitted_at"]),
        )


def _artifact_path(region_scope: str, settings: Settings) -> Path:
    return settings.models_dir / f"macro_hmm_{region_scope.lower()}" / "latest.joblib"


def save_fit(fit: FittedHMM, settings: Settings) -> Path:
    p = _artifact_path(fit.region_scope, settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(fit.to_dict(), p)
    return p


def load_fit(region_scope: str, settings: Settings) -> FittedHMM | None:
    p = _artifact_path(region_scope, settings)
    if not p.exists():
        return None
    return FittedHMM.from_dict(joblib.load(p))


def fit_regime(region_scope: str, as_of: date, settings: Settings | None = None,
               min_months: int = 60) -> tuple[FittedHMM, pl.DataFrame]:
    """Fit the HMM on the PIT panel up to `as_of` and return (fit, monthly regime-path DataFrame)."""
    s = settings or get_settings()
    panel = load_factor_panel(as_of, region_scope, s).drop_nulls()
    if panel.height < min_months:
        raise RuntimeError(f"{region_scope}: only {panel.height} complete monthly rows (<{min_months})")
    feats = [c for c in FEATURES if c in panel.columns]
    X_raw = panel.select(feats).to_numpy()
    Xz, mu, sd = _standardize(X_raw)
    model = _fit_best_seed(Xz, N_STATES)
    means, covars = model.means_, _diag_covars(model)
    prev = load_fit(region_scope, s)
    perm = _align_to_reference(means, prev.means if prev else None)
    means, covars = means[perm], covars[perm]
    transmat = model.transmat_[np.ix_(perm, perm)]
    startprob = model.startprob_[perm]
    labels = label_states(means, transmat, feats)
    probs = forward_filter(startprob, transmat, means, covars, Xz)
    regime_id = probs.argmax(axis=1)
    now = datetime.now(UTC)
    path = panel.select("date").with_columns(
        pl.Series("regime_id", regime_id.astype(np.int8)),
        pl.Series("regime_label", [labels[i] for i in regime_id]),
        pl.Series("probabilities", [p.tolist() for p in probs]),
        pl.Series("inputs_snapshot", [json.dumps(dict(zip(feats, row.tolist(), strict=True))) for row in X_raw]),
    )
    fit = FittedHMM(region_scope, feats, mu, sd, startprob, transmat, means, covars, labels,
                    panel["date"].min(), panel["date"].max(), now)
    return fit, path


def recession_probability(probabilities: list[float], state_labels: list[str]) -> float:
    """Sum of filtered probability over recessionary states (contraction + stagflation)."""
    return float(sum(p for p, lbl in zip(probabilities, state_labels, strict=True) if lbl in RECESSIONARY_LABELS))


# ------------------------------------------------------------------------------------------------ writing


def _rows_from_path(model_id: str, region_scope: str, path: pl.DataFrame, state_labels: list[str],
                    run_id: str | None) -> pl.DataFrame:
    now = datetime.now(UTC)
    return path.select(
        pl.lit(model_id).alias("model_id"),
        pl.col("date").cast(pl.Date),
        pl.lit(region_scope).alias("region_scope"),
        pl.col("regime_id"),
        pl.col("regime_label"),
        pl.col("probabilities"),
        pl.lit(state_labels).alias("state_labels"),
        pl.col("inputs_snapshot"),
        pl.lit(now).alias("computed_at"),
        pl.lit(run_id).alias("run_id"),
    )


def run_fit(region_scope: str, as_of: date, settings: Settings, run_id: str | None) -> dict[str, Any]:
    fit, path = fit_regime(region_scope, as_of, settings)
    save_fit(fit, settings)
    model_id = f"hmm4_{region_scope.lower()}_v1"
    rows = _rows_from_path(model_id, region_scope, path, fit.state_labels, run_id)
    n = write_table(rows, "macro_regimes", settings=settings)
    last = path.tail(1).to_dicts()[0]
    model_run = pl.DataFrame([{
        "model_run_id": run_id or model_id, "model_type": "macro_hmm", "horizon": None,
        "universe_id": "full", "feature_set_version": "macro_v1", "train_start": fit.train_start,
        "train_end": fit.train_end, "oos_start": None, "oos_end": None,
        "cv_scheme": json.dumps({"type": "full_refit", "n_folds": 1}),
        "params": json.dumps({"n_states": N_STATES, "features": fit.feature_names, "region_scope": region_scope}),
        "ic_mean": None, "rank_ic_mean": None, "rank_ic_ir": None, "rank_ic_tstat": None,
        "decile_spread_ann": None, "long_short_sharpe": None, "turnover_monthly": None, "hit_rate": None,
        "status": "completed", "is_production": True,
        "artifact_path": str(_artifact_path(region_scope, settings).relative_to(settings.models_dir)),
        "feature_importance": None,
        "notes": (f"MODEL-BASED: Gaussian HMM, 4 states, filtered (forward-only) probabilities on official series "
                  f"({SCOPE_SOURCES.get(region_scope, 'official')}); factors {fit.feature_names}. "
                  f"Latest state={last['regime_label']} "
                  f"recession_prob={recession_probability(last['probabilities'], fit.state_labels):.3f}."
                  + (" GLOBAL scope reuses US credit/vol/usd proxies (documented limitation)." if region_scope == "GLOBAL"
                     else "")),
        "created_at": datetime.now(UTC), "finished_at": datetime.now(UTC),
    }])
    write_table(model_run, "model_runs", settings=settings)
    return {"rows": n, "model_id": model_id, "latest": last["regime_label"]}


def run_filter(region_scope: str, as_of: date, settings: Settings, run_id: str | None) -> dict[str, Any]:
    fit = load_fit(region_scope, settings)
    if fit is None:
        return {"rows": 0, "skipped": "no fitted model yet; run macro_regime_fit first"}
    panel = load_factor_panel(as_of, region_scope, settings)
    feats = fit.feature_names
    panel = panel.select(["date", *feats]).drop_nulls()
    if panel.is_empty():
        return {"rows": 0, "skipped": "no PIT factor data available"}
    X = (panel.select(feats).to_numpy() - fit.mu) / fit.sd
    probs = forward_filter(fit.startprob, fit.transmat, fit.means, fit.covars, X)
    last_idx = int(probs[-1].argmax())
    model_id = f"hmm4_{region_scope.lower()}_v1"
    now = datetime.now(UTC)
    row = pl.DataFrame([{
        "model_id": model_id, "date": as_of, "region_scope": region_scope,
        "regime_id": last_idx, "regime_label": fit.state_labels[last_idx],
        "probabilities": probs[-1].tolist(), "state_labels": fit.state_labels,
        "inputs_snapshot": json.dumps(dict(zip(feats, panel.select(feats).tail(1).to_numpy()[0].tolist(), strict=True))),
        "computed_at": now, "run_id": run_id,
    }], schema={"model_id": pl.Utf8, "date": pl.Date, "region_scope": pl.Utf8, "regime_id": pl.Int8,
               "regime_label": pl.Utf8, "probabilities": pl.List(pl.Float64), "state_labels": pl.List(pl.Utf8),
               "inputs_snapshot": pl.Utf8, "computed_at": pl.Datetime("us", "UTC"), "run_id": pl.Utf8})
    n = write_table(row, "macro_regimes", settings=settings)
    return {"rows": n, "model_id": model_id, "latest": fit.state_labels[last_idx],
           "recession_probability": recession_probability(probs[-1].tolist(), fit.state_labels)}


def run(ctx):  # noqa: ANN001 - JobContext
    """Jobs `macro_regime_fit` (params.mode='fit') and `macro_regime_nowcast` (params.mode='filter')."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    mode = p.get("mode", "filter")
    scopes = p.get("region_scopes", DEFAULT_SCOPES)
    rows, msgs, failed = 0, [], 0
    for scope in scopes:
        ctx.check_cancelled()
        try:
            out = (run_fit if mode == "fit" else run_filter)(scope, ctx.as_of, ctx.settings, ctx.run_id)
            rows += out.get("rows", 0)
            msgs.append(f"{scope}: {out}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            msgs.append(f"{scope} FAILED: {e}")
            log.warning("macro_regime %s %s failed: %s", mode, scope, e)
    return JobResult(status="partial" if failed else "success", rows_written={"macro_regimes": rows},
                     entities_ok=len(scopes) - failed, entities_failed=failed,
                     watermark=ctx.as_of.isoformat(), message="; ".join(msgs))
