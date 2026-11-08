"""Rolling per-security macro factor betas + regime-conditional performance. Job `macro_betas` (weekly).

Methodology
-----------
* Weekly (Friday-to-Friday) returns, trailing `window_weeks` (default 156 = 3y, minimum 52) OLS regression per
  security: `r_i,t = alpha + sum_f beta_f * factor_f,t + eps_t`, factors:
  market (IDX:^GSPC weekly return), rates_10y (Delta DGS10), curve_2s10s (Delta T10Y2Y), credit_spread (Delta
  BAA10Y -- long-history Baa-Treasury spread; the FRED ICE BofA HY OAS series only keeps 3 years, documented in
  `app.ml.macro_regime`), usd_index (DTWEXBGS weekly % change), oil (DCOILWTICO weekly % change), copper
  (PCOPPUSDM monthly % change, forward-filled to weekly), inflation_surprise (week-over-week change in realized
  YoY CPI (CPIAUCSL), forward-filled to weekly -- a realized-revision proxy; no consensus/survey data is ingested
  at MVP so this is *not* a true survey surprise, documented limitation). `gold` (listed in the schema description)
  is omitted: no gold price series is ingested yet (owned by ingest-market, out of scope here).
* Newey-West (HAC, 4 lags) t-stats via `statsmodels` to account for serial correlation.
* Shrinkage: `beta_shrunk = w * beta_raw + (1-w) * sector_mean(beta_raw)`, `w = n_obs / (n_obs + k)` (empirical-Bayes
  style shrinkage toward the sector grand mean, k=26 weeks of "prior strength"). Stored `beta` is the shrunk value;
  `t_stat`/`r2` describe the underlying (unshrunk) regression so they remain interpretable as significance of the
  raw estimate.
* `peer_percentile`: percentile rank of the *shrunk* beta within sector (same as_of_date, same factor_id).
* Regime-conditional performance: `factor_id = 'regime_<label>'` rows hold the security's mean weekly return,
  annualized to a monthly-equivalent figure (`*4.33`), across all historical weeks the US HMM (`hmm4_us_v1`,
  `app.ml.macro_regime`) assigned to that label -- this doubles as the "regime-conditional sector performance
  table" required by the brief when run over the sector ETFs (ETF:XL*) alongside individual securities.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table

log = get_logger(__name__)

MARKET_ID = "IDX:^GSPC"
FACTOR_SERIES = {
    "rates_10y": ("fred:DGS10", "diff"),
    "curve_2s10s": ("fred:T10Y2Y", "diff"),
    "credit_spread": ("fred:BAA10Y", "diff"),
    "usd_index": ("fred:DTWEXBGS", "pct_change"),
    "oil": ("fred:DCOILWTICO", "pct_change"),
    "copper": ("fred:PCOPPUSDM", "pct_change"),
    "inflation_yoy": ("fred:CPIAUCSL", "diff_of_yoy"),  # -> factor_id "inflation_surprise"
}
FACTOR_RENAME = {"inflation_yoy": "inflation_surprise"}
WINDOW_WEEKS_DEFAULT = 156
MIN_WEEKS = 52
SHRINK_K = 26.0


# ------------------------------------------------------------------------------------------------ data


def _weekly_returns(security_ids: list[str] | None, settings: Settings) -> pl.DataFrame:
    """[security_id, week, ret] weekly simple returns on adj_close (last trading day of each ISO week)."""
    pr = scan_table("prices_daily", settings)
    if security_ids is not None:
        pr = pr.filter(pl.col("security_id").is_in(security_ids))
    pr = pr.select("security_id", "date", "adj_close").drop_nulls()
    weekly = (
        pr.with_columns(pl.col("date").dt.truncate("1w").alias("week"))
        .sort(["security_id", "date"])
        .group_by(["security_id", "week"], maintain_order=True)
        .agg(pl.col("adj_close").last().alias("close"))
        .sort(["security_id", "week"])
        # consecutive weeks only: a return across a gap (suspension, stitch-guard hole in long Yahoo history) would
        # be a multi-week move regressed on one week of factors
        .with_columns(pl.when((pl.col("week") - pl.col("week").shift(1).over("security_id")).dt.total_days() <= 7)
                      .then(pl.col("close") / pl.col("close").shift(1).over("security_id") - 1.0)
                      .otherwise(None).alias("ret"))
        .drop_nulls("ret")
    )
    return weekly.select("security_id", "week", "ret").collect()


def _factor_series(series_id: str, settings: Settings) -> pl.DataFrame:
    """[date, value] full history (knowledge_date doesn't matter for a model that is refit weekly on recent
    history only -- using the latest vintage of historical macro data here is standard practice and immaterial
    for betas estimated on data already years old; `app.ml.macro_regime` is the PIT-sensitive path)."""
    return (scan_table("macro_observations", settings).filter(pl.col("series_id") == series_id)
            .select("date", "value").sort("date").collect())


def _to_weekly_factor(raw: pl.DataFrame, kind: str, weeks: pl.Series) -> pl.DataFrame:
    """raw: [date, value] (any native frequency). Returns [week, value] aligned to `weeks` (ffill)."""
    df = raw.sort("date")
    if kind == "diff_of_yoy":
        df = df.with_columns((pl.col("value") / pl.col("value").shift(12) - 1.0).alias("value")).drop_nulls("value")
        df = df.with_columns((pl.col("value") - pl.col("value").shift(1)).alias("value"))
    weeks_df = weeks.unique().sort().to_frame("week")
    j = weeks_df.join_asof(df.rename({"date": "week"}), on="week", strategy="backward")
    j = j.rename({"value": "level"})
    if kind == "diff":
        j = j.with_columns((pl.col("level") - pl.col("level").shift(1)).alias("value"))
    elif kind == "pct_change":
        j = j.with_columns((pl.col("level") / pl.col("level").shift(1) - 1.0).alias("value"))
    else:  # diff_of_yoy already produced the value level-to-level diff above
        j = j.rename({"level": "value"})
    return j.select("week", "value").drop_nulls("value")


def build_factor_panel(weeks: pl.Series, settings: Settings) -> pl.DataFrame:
    mkt = _weekly_returns([MARKET_ID], settings).rename({"ret": "market"}).select("week", "market")
    panel = mkt
    for fid, (sid, kind) in FACTOR_SERIES.items():
        raw = _factor_series(sid, settings)
        if raw.is_empty():
            continue
        f = _to_weekly_factor(raw, kind, weeks).rename({"value": FACTOR_RENAME.get(fid, fid)})
        panel = panel.join(f, on="week", how="left")
    return panel.sort("week")


# ------------------------------------------------------------------------------------------------ regression


def _ols_hac(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """OLS with Newey-West HAC(4) covariance. X has no intercept column (added here). Returns (betas, tstats, r2)."""
    import statsmodels.api as sm  # noqa: PLC0415

    Xc = sm.add_constant(X)
    model = sm.OLS(y, Xc, missing="drop").fit(cov_type="HAC", cov_kwds={"maxlags": 4})
    betas = model.params[1:]
    tstats = model.tvalues[1:]
    return betas, tstats, float(model.rsquared)


def rolling_betas(sec_weekly: pl.DataFrame, factor_panel: pl.DataFrame, factor_cols: list[str],
                  as_of_week: Any, window_weeks: int) -> pl.DataFrame:
    """Per-security betas as of the latest `window_weeks` weekly observations <= as_of_week."""
    fp = factor_panel.filter(pl.col("week") <= as_of_week).tail(window_weeks)
    rows = []
    for sid, g in sec_weekly.filter(pl.col("week") <= as_of_week).group_by("security_id"):
        g = g.tail(window_weeks).join(fp, on="week", how="inner").drop_nulls()
        n = g.height
        if n < MIN_WEEKS:
            continue
        y = g["ret"].to_numpy()
        X = g.select(factor_cols).to_numpy()
        try:
            betas, tstats, r2 = _ols_hac(y, X)
        except Exception as e:  # noqa: BLE001 - singular design on a near-constant factor window
            log.warning("macro_betas: OLS failed for %s: %s", sid, e)
            continue
        for f, b, t in zip(factor_cols, betas, tstats, strict=True):
            rows.append({"security_id": sid[0] if isinstance(sid, tuple) else sid, "factor_id": f,
                        "beta": float(b), "t_stat": float(t), "r2": r2, "n_obs": n})
    return pl.DataFrame(rows) if rows else pl.DataFrame(
        schema={"security_id": pl.Utf8, "factor_id": pl.Utf8, "beta": pl.Float64, "t_stat": pl.Float64,
               "r2": pl.Float64, "n_obs": pl.Int64})


def shrink_betas(betas: pl.DataFrame, sector_map: pl.DataFrame, k: float = SHRINK_K) -> pl.DataFrame:
    """beta_shrunk = w*beta_raw + (1-w)*sector_mean(beta_raw); w = n/(n+k). peer_percentile within sector."""
    b = betas.join(sector_map, on="security_id", how="left").with_columns(
        pl.col("sector_id").fill_null("UNKNOWN"))
    sector_mean = b.group_by(["sector_id", "factor_id"]).agg(pl.col("beta").mean().alias("sector_mean"))
    b = b.join(sector_mean, on=["sector_id", "factor_id"], how="left")
    w = pl.col("n_obs") / (pl.col("n_obs") + k)
    b = b.with_columns((w * pl.col("beta") + (1 - w) * pl.col("sector_mean")).alias("beta_shrunk"))
    pct = (b.with_columns(pl.col("beta_shrunk").rank(method="average").over(["sector_id", "factor_id"]).alias("__r"),
                          pl.len().over(["sector_id", "factor_id"]).alias("__n"))
          .with_columns((pl.col("__r") / pl.col("__n") * 100.0).alias("peer_percentile")))
    return pct.select("security_id", "factor_id", pl.col("beta_shrunk").alias("beta"), "t_stat", "r2", "n_obs",
                      "peer_percentile")


# ------------------------------------------------------------------------------------------------ regime performance


def regime_performance(sec_weekly: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """factor_id='regime_<label>' rows: mean weekly return * 4.33 (monthly-equivalent) per security per US regime,
    over *all* available price history (not the 3y beta window): regime-conditional performance is only
    meaningful across multiple cycles (GFC, COVID, ...), which a 3y window would rarely contain."""
    regimes = scan_table("macro_regimes", settings).filter(pl.col("model_id") == "hmm4_us_v1").select(
        "date", "regime_label").collect()
    if regimes.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "factor_id": pl.Utf8, "beta": pl.Float64,
                                    "t_stat": pl.Float64, "r2": pl.Float64, "n_obs": pl.Int64,
                                    "peer_percentile": pl.Float64})
    regimes = regimes.with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
    sw = sec_weekly.with_columns(pl.col("week").dt.truncate("1mo").alias("month"))
    j = sw.join(regimes.select("month", "regime_label"), on="month", how="inner")
    agg = j.group_by(["security_id", "regime_label"]).agg(
        (pl.col("ret").mean() * 4.33).alias("beta"), pl.len().alias("n_obs"))
    return agg.select("security_id", pl.concat_str([pl.lit("regime_"), pl.col("regime_label")]).alias("factor_id"),
                      "beta", pl.lit(None, dtype=pl.Float64).alias("t_stat"),
                      pl.lit(None, dtype=pl.Float64).alias("r2"), "n_obs",
                      pl.lit(None, dtype=pl.Float64).alias("peer_percentile"))


# ------------------------------------------------------------------------------------------------ job


def run_betas(as_of: date, settings: Settings, security_ids: list[str], window_weeks: int = WINDOW_WEEKS_DEFAULT,
             run_id: str | None = None) -> dict[str, Any]:
    sec_weekly = _weekly_returns(security_ids + [MARKET_ID], settings)
    if sec_weekly.is_empty():
        return {"rows": 0, "skipped": "no price history"}
    weeks = sec_weekly["week"]
    as_of_week = weeks.filter(weeks <= as_of).max()
    if as_of_week is None:
        return {"rows": 0, "skipped": "as_of before any weekly data"}
    factor_panel = build_factor_panel(weeks, settings)
    factor_cols = [c for c in factor_panel.columns if c != "week"]
    sec_only = sec_weekly.filter(pl.col("security_id") != MARKET_ID)
    betas = rolling_betas(sec_only, factor_panel, factor_cols, as_of_week, window_weeks)
    issuers = read_table("issuers", columns=["issuer_id", "sector_id"], settings=settings)
    sec_master = read_table("securities_master", columns=["security_id", "issuer_id"], settings=settings)
    sector_map = sec_master.join(issuers, on="issuer_id", how="left").select("security_id", "sector_id")
    shrunk = shrink_betas(betas, sector_map) if not betas.is_empty() else betas
    regime_rows = regime_performance(sec_only, settings)
    out = pl.concat([shrunk, regime_rows], how="diagonal_relaxed") if not shrunk.is_empty() else regime_rows
    if out.is_empty():
        return {"rows": 0, "skipped": "no betas computed (insufficient history)"}
    now = datetime.now(UTC)
    out = out.with_columns(
        pl.lit(as_of).alias("as_of_date"), pl.lit(window_weeks).cast(pl.Int16).alias("window_months"),
        pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id"),
    )  # NB: `window_months` column holds window_weeks (contract column name is legacy from a monthly-window draft)
    n = write_table(out.select("as_of_date", "security_id", "factor_id", "beta", "t_stat", "r2", "n_obs",
                               "window_months", "peer_percentile", "computed_at", "run_id"),
                   "macro_betas", settings=settings)
    return {"rows": n, "securities": sec_only["security_id"].n_unique()}


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `macro_betas`. params: universe_ids (default [mvp_us, mvp_global]), window_weeks."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    universe_ids = p.get("universe_ids", ["mvp_us", "mvp_global"])
    um = read_table("universe_membership", settings=ctx.settings)
    um = um.filter(pl.col("universe_id").is_in(universe_ids) & pl.col("valid_to").is_null())
    security_ids = um["security_id"].unique().to_list()
    if not security_ids:
        return JobResult(status="skipped", message="no securities in target universes yet")
    try:
        out = run_betas(ctx.as_of, ctx.settings, security_ids, int(p.get("window_weeks", WINDOW_WEEKS_DEFAULT)),
                        ctx.run_id)
    except Exception as e:  # noqa: BLE001
        log.exception("macro_betas failed")
        return JobResult(status="failed", message=str(e), retryable=True)
    return JobResult(status="skipped" if out.get("skipped") else "success",
                     rows_written={"macro_betas": out.get("rows", 0)},
                     entities_ok=out.get("securities", 0), watermark=ctx.as_of.isoformat(), message=str(out))
