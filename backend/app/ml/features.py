"""Point-in-time cross-sectional feature panel. Job `ml_features` (nightly, latest date only) and
`rebuild_features` (manual, `params.mode='full_history'`). Owner: ml.

Feature registry: `backend/app/ml/feature_registry.yaml` (loaded by `load_registry()`), declaring per feature its
family, source table, lag, transform and `status` (live | pending). `ml_features` stores RAW values; the
cross-sectional transform declared in the registry (rank-gauss / z-score) is applied inside the training pipeline
(`app.ml.train`), never here -- this keeps the stored panel reusable across models with different normalization
choices and auditable against raw inputs.

PIT construction
-----------------
* Rebalance dates: `app.ml.labels.rebalance_dates()` (shared grid with `ml_labels`, every ISO week's + every
  month's last trading day).
* Price/vol/liquidity features are rolling windows on each security's own daily bar sequence, computed once on
  the full history then attached to each rebalance date via a *backward* as-of join (value as of the latest
  trading day <= D) -- correct even when a rebalance date isn't a trading day for a given exchange (holiday).
* Macro features (regime, betas) are attached the same way from `macro_regimes` / `macro_betas`, whose own
  `date` / `as_of_date` is already a PIT cutoff (<= D is required, enforced by the backward join). `macro_betas`
  only has as-of snapshots going back to when the weekly job started running (documented in the final report):
  rebalance dates earlier than the first snapshot correctly get a null beta rather than the lookahead that would
  result from attaching today's beta to 2015 history.
* Pending-table features (factor_scores, tech_levels/confluence_zones, insider_signals, sentiment_daily,
  fundamentals_wide) are read defensively: a missing/empty table yields an all-null column, never a crash
  (`_safe_read`).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, write_table
from app.ml.labels import DEFAULT_UNIVERSES, rebalance_dates

log = get_logger(__name__)

REGISTRY_PATH = Path(__file__).parent / "feature_registry.yaml"
MOM_WINDOWS = {"mom_21d": 21, "mom_63d": 63, "mom_126d": 126, "mom_12m": 252}
VOL_WINDOWS = {"vol_21d": 21, "vol_63d": 63}
REGIME_LABELS = ["expansion", "slowdown", "contraction", "recovery", "stagflation"]
BETA_FACTORS = ["market", "rates_10y", "curve_2s10s", "credit_spread", "usd_index", "oil"]


def load_registry() -> dict[str, Any]:
    doc = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8"))
    doc["by_id"] = {f["id"]: f for f in doc["features"]}
    return doc


def feature_set_version(registry: dict[str, Any] | None = None) -> str:
    reg = registry or load_registry()
    key = "|".join(f"{f['id']}:{f['transform']}:{f['status']}" for f in sorted(reg["features"], key=lambda f: f["id"]))
    return f"v{reg['version']}_" + hashlib.sha1(key.encode()).hexdigest()[:10]


def live_feature_ids(registry: dict[str, Any] | None = None) -> list[str]:
    return [f["id"] for f in (registry or load_registry())["features"] if f["status"] == "live"]


def pending_feature_ids(registry: dict[str, Any] | None = None) -> list[str]:
    return [f["id"] for f in (registry or load_registry())["features"] if f["status"] == "pending"]


def _safe_read(table: str, settings: Settings, **kw: Any) -> pl.DataFrame:
    try:
        return read_table(table, settings=settings, **kw)
    except Exception as e:  # noqa: BLE001 - table not yet populated by its owner, or contract not final
        log.info("ml.features: %s unavailable (%s); pending features will be null", table, e)
        return pl.DataFrame()


# ------------------------------------------------------------------------------------------------ price block


#: calendar warm-up before the first grid date for the row-based windows (252 bars, ~3y so thinly traded names keep them)
PRICE_WARMUP_DAYS = 1100


def _price_daily_block(security_ids: list[str], settings: Settings, start: date | None = None) -> pl.DataFrame:
    """Daily price features. `start` bounds the scan (first grid date - PRICE_WARMUP_DAYS): prices_daily now holds
    Yahoo history back to the 1920s, which the 252-bar windows never need."""
    lf = scan_table("prices_daily", settings).filter(pl.col("security_id").is_in(security_ids))
    if start is not None:
        lf = lf.filter(pl.col("date") >= start)
    px = (lf
         .select("security_id", "date", "adj_close", "dollar_volume_usd").sort(["security_id", "date"]).collect())
    ret = pl.col("adj_close") / pl.col("adj_close").shift(1).over("security_id") - 1.0
    exprs = [ret.alias("__ret")]
    for fid, w in MOM_WINDOWS.items():
        exprs.append((pl.col("adj_close") / pl.col("adj_close").shift(w).over("security_id") - 1.0).alias(f"f_{fid}"))
    exprs.append((pl.col("adj_close").shift(21).over("security_id")
                 / pl.col("adj_close").shift(252).over("security_id") - 1.0).alias("f_mom_12m_1m"))
    exprs.append((pl.col("adj_close") / pl.col("adj_close").shift(5).over("security_id") - 1.0).alias("f_rev_5d"))
    exprs.append((pl.col("dollar_volume_usd").rolling_mean(window_size=20).over("security_id").log()
                 ).alias("f_adv_usd_20d_log"))
    px = px.with_columns(exprs)
    for fid, w in VOL_WINDOWS.items():
        px = px.with_columns((pl.col("__ret").rolling_std(window_size=w).over("security_id")
                             * (252.0**0.5)).alias(f"f_{fid}"))
    return px.drop("__ret", "adj_close", "dollar_volume_usd")


def _asof_attach(grid: pl.DataFrame, daily: pl.DataFrame, value_cols: list[str]) -> pl.DataFrame:
    """grid: [security_id, date]; daily: [security_id, date, *value_cols] sorted. Backward as-of per security."""
    g = grid.sort(["security_id", "date"])
    d = daily.select("security_id", "date", *value_cols).sort(["security_id", "date"])
    return g.join_asof(d, on="date", by="security_id", strategy="backward")


# ------------------------------------------------------------------------------------------------ macro block


def _macro_regime_wide(scope: str, settings: Settings) -> pl.DataFrame:
    model_id = f"hmm4_{scope.lower()}_v1"
    rows = scan_table("macro_regimes", settings).filter(pl.col("model_id") == model_id).select(
        "date", "probabilities", "state_labels").sort("date").collect()
    if rows.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, **{f"f_macro_regime_{scope.lower()}_{lbl}_prob": pl.Float64
                                                        for lbl in REGIME_LABELS}})
    out = []
    for r in rows.iter_rows(named=True):
        d = dict(zip(r["state_labels"], r["probabilities"], strict=True))
        row = {"date": r["date"]}
        for lbl in REGIME_LABELS:
            row[f"f_macro_regime_{scope.lower()}_{lbl}_prob"] = d.get(lbl, 0.0)
        out.append(row)
    wide = pl.DataFrame(out)
    if scope == "US":
        wide = wide.with_columns((pl.col("f_macro_regime_us_contraction_prob")
                                 + pl.col("f_macro_regime_us_stagflation_prob")).alias("f_macro_recession_prob_us"))
    return wide


def _macro_betas_wide(settings: Settings) -> pl.DataFrame:
    mb = scan_table("macro_betas", settings).filter(pl.col("factor_id").is_in(BETA_FACTORS)).select(
        "as_of_date", "security_id", "factor_id", "beta").collect()
    if mb.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "as_of_date": pl.Date,
                                    **{f"f_macro_beta_{f}": pl.Float64 for f in BETA_FACTORS}})
    wide = mb.pivot(on="factor_id", index=["as_of_date", "security_id"], values="beta")
    return wide.rename({f: f"f_macro_beta_{f}" for f in BETA_FACTORS if f in wide.columns})


# ------------------------------------------------------------------------------------------------ pending block


def _pending_block(grid: pl.DataFrame, registry: dict[str, Any]) -> pl.DataFrame:
    cols = {f"f_{fid}": pl.lit(None, dtype=pl.Float64) for fid in pending_feature_ids(registry)}
    return grid.with_columns(**cols) if cols else grid


# ---- factor_scores / insider_signals / sentiment_daily / tech_setups (2026-10-11: now populated by their owners)


_FACTOR_ID_MAP = {
    "factor_value": "value", "factor_quality": "quality", "factor_growth": "growth",
    "factor_profitability": "profitability", "factor_balance_sheet": "balance_sheet", "factor_moat": "moat",
    "fundamental_quality": "profitability",  # repurposed, see feature_registry.yaml note
}


def _factor_scores_wide(settings: Settings, extra: pl.DataFrame | None = None) -> pl.DataFrame:
    fs = _safe_read("factor_scores", settings, columns=["as_of_date", "security_id", "factor_id", "score"])
    if extra is not None and not extra.is_empty():
        fs = pl.concat([fs, extra.select("as_of_date", "security_id", "factor_id", "score")], how="vertical_relaxed")             if not fs.is_empty() else extra.select("as_of_date", "security_id", "factor_id", "score")
    if fs.is_empty():
        return pl.DataFrame(schema={"as_of_date": pl.Date, "security_id": pl.Utf8})
    wanted = {v for k, v in _FACTOR_ID_MAP.items()}
    wide = fs.filter(pl.col("factor_id").is_in(list(wanted))).pivot(
        on="factor_id", index=["as_of_date", "security_id"], values="score")
    rename = {}
    for fid, factor_id in _FACTOR_ID_MAP.items():
        if factor_id in wide.columns and f"f_{fid}" not in rename.values():
            rename[factor_id] = f"f_{fid}"
    # fundamental_quality and factor_profitability share the same source column: duplicate it explicitly
    out = wide.rename({k: v for k, v in rename.items() if k != "profitability"})
    if "profitability" in wide.columns:
        out = out.with_columns(pl.col("profitability").alias("f_factor_profitability"),
                               pl.col("profitability").alias("f_fundamental_quality")).drop("profitability")
    return out


def _insider_wide(settings: Settings) -> pl.DataFrame:
    df = _safe_read("insider_signals", settings, columns=["as_of_date", "security_id", "signal_score"])
    return df.rename({"signal_score": "f_insider_net_buy_score"}) if not df.is_empty() else pl.DataFrame(
        schema={"as_of_date": pl.Date, "security_id": pl.Utf8, "f_insider_net_buy_score": pl.Float64})


def _sentiment_wide(settings: Settings) -> pl.DataFrame:
    df = _safe_read("sentiment_daily", settings, columns=["date", "security_id", "sentiment_score"])
    return df.rename({"sentiment_score": "f_sentiment_score"}) if not df.is_empty() else pl.DataFrame(
        schema={"date": pl.Date, "security_id": pl.Utf8, "f_sentiment_score": pl.Float64})


def _tech_wide(settings: Settings) -> pl.DataFrame:
    """tech_setups is a current-snapshot table (not a PIT history): this yields a usable feature only near the
    latest date, null for older training rows (documented in feature_registry.yaml, not a bug)."""
    df = _safe_read("tech_setups", settings, columns=["as_of_date", "security_id", "bias", "trend"])
    if df.is_empty():
        return pl.DataFrame(schema={"as_of_date": pl.Date, "security_id": pl.Utf8, "f_tech_confluence_score": pl.Float64})
    bias_n = pl.when(pl.col("bias") == "long").then(1.0).when(pl.col("bias") == "short").then(-1.0).otherwise(0.0)
    trend_n = pl.when(pl.col("trend") == "up").then(1.0).when(pl.col("trend") == "down").then(-1.0).otherwise(0.0)
    return df.with_columns(((bias_n * 2 + trend_n) / 3.0).alias("f_tech_confluence_score")).select(
        "as_of_date", "security_id", "f_tech_confluence_score")


# ------------------------------------------------------------------------------------------------ main


def compute_features(as_of_end: date, settings: Settings, universe_ids: tuple[str, ...] = DEFAULT_UNIVERSES,
                     start: date | None = None, dates: list[date] | None = None,
                     security_ids: list[str] | None = None, extra_factor_scores: pl.DataFrame | None = None
                     ) -> pl.DataFrame:
    """`security_ids` (full-universe extension, app.ml.full_scope) computes the panel for those securities instead of
    the universe members (in_universe stays membership of `universe_ids`); `extra_factor_scores` (as_of_date,
    security_id, factor_id, score) adds the anchored factor_scores_ext rows of securities outside the MVP."""
    registry = load_registry()
    fsv = feature_set_version(registry)
    um = read_table("universe_membership", settings=settings)
    um = um.filter(pl.col("universe_id").is_in(list(universe_ids)))
    uni_members = um["security_id"].unique().to_list()
    members = uni_members if security_ids is None else sorted(set(security_ids))
    if not members:
        return pl.DataFrame()
    rebal = dates or rebalance_dates(settings, start=start, end=as_of_end)
    grid = pl.DataFrame({"security_id": members}).join(pl.DataFrame({"date": rebal}), how="cross")

    daily = _price_daily_block(members, settings, min(rebal) - timedelta(days=PRICE_WARMUP_DAYS) if rebal else None)
    price_cols = [c for c in daily.columns if c.startswith("f_")]
    out = _asof_attach(grid, daily, price_cols)

    for scope in ("US", "GLOBAL"):
        rw = _macro_regime_wide(scope, settings)
        if not rw.is_empty():
            out = out.sort("date").join_asof(rw.sort("date"), on="date", strategy="backward")

    betas = _macro_betas_wide(settings)
    beta_cols = [c for c in betas.columns if c.startswith("f_")]
    if beta_cols:
        out = _asof_attach(out, betas.rename({"as_of_date": "date"}), beta_cols)

    for wide, date_col in ((_factor_scores_wide(settings, extra_factor_scores), "as_of_date"),
                           (_insider_wide(settings), "as_of_date"),
                           (_sentiment_wide(settings), "date"), (_tech_wide(settings), "as_of_date")):
        cols = [c for c in wide.columns if c.startswith("f_")]
        if cols:
            out = _asof_attach(out, wide.rename({date_col: "date"}) if date_col != "date" else wide, cols)

    out = _pending_block(out, registry)  # safety net: any feature still marked 'pending' gets an all-null column

    from app.normalize.prices import country_to_region  # noqa: PLC0415

    sec = read_table("securities_master", columns=["security_id", "issuer_id", "primary_listing_id"],
                     settings=settings)
    iss = read_table("issuers", columns=["issuer_id", "sector_id", "subsector_id"], settings=settings)
    lst = read_table("listings", columns=["listing_id", "exchange_country"], settings=settings)
    # ram_fix 2026-10-31: only the panel's securities. The unfiltered scan read security_id / region of all ~99 M
    # prices_daily rows and peaked the job at ~13 GB for a 781-row daily run (machine free RAM 0.0 GB)
    region = (scan_table("prices_daily", settings).filter(pl.col("security_id").is_in(members))
              .select("security_id", "region").unique().collect())
    meta = (sec.join(iss, on="issuer_id", how="left")
           .join(lst, left_on="primary_listing_id", right_on="listing_id", how="left")
           .join(region, on="security_id", how="left"))
    meta = meta.with_columns(
        pl.when(pl.col("region").is_null())
        .then(pl.col("exchange_country").map_elements(country_to_region, return_dtype=pl.Utf8))
        .otherwise(pl.col("region")).alias("region"),
        pl.col("sector_id").fill_null("UNKNOWN"),
    ).select("security_id", "issuer_id", "sector_id", "subsector_id", "region")
    out = out.join(meta, on="security_id", how="left")
    out = out.with_columns(pl.col("region").fill_null("OTHER"), pl.col("sector_id").fill_null("UNKNOWN"))

    adv = daily.select("security_id", "date", pl.col("f_adv_usd_20d_log").exp().alias("adv_usd_20d"))
    out = _asof_attach(out.sort(["security_id", "date"]), adv, ["adv_usd_20d"])

    now = datetime.now(UTC)
    in_uni = set(uni_members)
    out = out.with_columns(
        pl.col("date").dt.year().cast(pl.Int16).alias("year"),
        pl.lit(None, dtype=pl.Float64).alias("market_cap_usd"),  # pending fundamentals_wide (shares outstanding)
        pl.lit(None, dtype=pl.Utf8).alias("size_bucket"),
        pl.col("security_id").is_in(list(in_uni)).alias("in_universe"),
        pl.lit(fsv).alias("feature_set_version"),
        pl.lit(now).alias("computed_at"),
    )
    keep = ["date", "year", "security_id", "issuer_id", "sector_id", "subsector_id", "region", "size_bucket",
           "market_cap_usd", "adv_usd_20d", "in_universe", "feature_set_version", "computed_at"]
    keep += [f"f_{fid}" for fid in registry["by_id"]]
    for c in keep:
        if c not in out.columns:
            out = out.with_columns(pl.lit(None).alias(c))
    return out.select(keep)


def run(ctx):  # noqa: ANN001 - JobContext
    """Job `ml_features` (nightly: latest date only) / `rebuild_features` (manual, params.mode='full_history')."""
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    universe_ids = tuple(p.get("universe_ids", DEFAULT_UNIVERSES))
    if p.get("mode") == "full_history":
        start = date.fromisoformat(p["start"]) if p.get("start") else date(2005, 1, 1)
        dates = None
    else:
        start, dates = None, [ctx.as_of]
    try:
        df = compute_features(ctx.as_of, ctx.settings, universe_ids, start=start, dates=dates)
    except Exception as e:  # noqa: BLE001
        log.exception("ml_features failed")
        return JobResult(status="failed", message=str(e))
    if df.is_empty():
        return JobResult(status="skipped", message="no universe members yet")
    n = write_table(df, "ml_features", settings=ctx.settings)
    return JobResult(status="success", rows_written={"ml_features": n}, entities_ok=df["security_id"].n_unique(),
                     watermark=ctx.as_of.isoformat(), message=f"rows={n} dates={df['date'].n_unique()}")
