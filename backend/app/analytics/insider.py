"""Insider signals (job `insider_signals`, owner signals-alt).

Point-in-time rules (ARCHITECTURE §7): an insider transaction is usable from its filing date (knowledge_date), never
from the trade date. Rows restated by a 4/A (`superseded_by_filing_id`) are dropped from the amendment's knowledge
date on; in history mode they are dropped altogether and the amendment row (which carries the later knowledge date)
is used instead — conservative, never early.

Pipeline
1. `load_open_market()`   open-market buys (code P) and sells (code S) only: grants/exercises/tax withholding/gifts
                          (A/M/F/G/...) and derivative-table rows are excluded; source keys resolved to
                          issuer/security via app.normalize.identity (re-runs cleanly once identity back-fills ids).
2. `transaction_features()` per-transaction PIT features: USD value (shares x close when the filing has no price),
                          role weight (CEO/Chair/CFO > other officers > directors > 10% owners), 10b5-1 flag,
                          signed holding change, distinct same-direction insiders filed within 30 d (cluster count),
                          first buy by this insider in 2 years, size vs the insider's own past trades, Cohen-Malloy-
                          Pomorski routine flag (same calendar month in each of the 3 prior years), pre-filing price
                          drawdown / run-up / volatility / ADV, filing lag.
3. `assign_clusters()`    cluster_id per open-market BUY: buys of one issuer chained while consecutive trade dates are
                          <= 30 days apart; episodes with >= 2 distinct insiders get an id (API: InsiderTransaction.
                          cluster_id); `cluster_buy` on the signal requires >= 3 distinct buyers within 30 days.
4. `compute_signals()`    per issuer and as-of date: 30/90 d net open-market value, buyers/sellers, cluster/c-suite
                          flags, opportunistic (non-routine) buy/sell value, anomaly score (IsolationForest, from
                          app.analytics.insider_models), XGBoost P(3M excess > 0) and the 0-100 `signal_score`
                          (50 = no activity).

Outputs: insider_signals (contract) and insider_txn_scores (per-transaction anomaly score / cluster id / event
features consumed by the API's insider feed).

Job params: mode = "latest" (default: ctx.as_of only) | "history" (month-ends from params.start, default 2017-01-01,
plus the last `daily_days` (default 30) calendar days); skip_txn_scores (bool).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import orjson
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table
from app.nlp.common import has_data, load_prices, resolve_entities

log = get_logger(__name__)

ROLE_WEIGHT: dict[str, float] = {
    "ceo": 3.0, "chair": 2.75, "cfo": 2.5, "president": 2.25, "coo": 2.0, "officer_other": 1.5, "director": 1.25,
    "owner": 0.75, "other": 1.0,
}
C_SUITE = ("ceo", "cfo")
CLUSTER_WINDOW_D = 30
CLUSTER_MIN_INSIDERS_SIGNAL = 3
CLUSTER_MIN_INSIDERS_ID = 2
FIRST_BUY_YEARS = 2
DECAY_HALFLIFE_D = 45.0
SCORE_SCALE = 5.0
SCORE_TANH = 2.5

_TXN_COLS = ["txn_id", "region", "filing_id", "source_id", "source_entity_id", "issuer_id", "security_id", "insider_id",
             "insider_name", "is_director", "is_officer", "is_ten_pct_owner", "role_norm", "transaction_date",
             "knowledge_date", "txn_code", "txn_type", "is_open_market", "is_derivative", "is_10b5_1", "shares",
             "price", "currency", "value_usd", "shares_owned_after", "holding_change_pct", "superseded_by_filing_id"]

# features used by the IsolationForest (per transaction) -- order matters for persisted models
TXN_MODEL_FEATURES = ["direction", "log_value", "role_w", "is_10b5_1_f", "holding_change", "filing_lag",
                      "n_same_dir_30d", "first_buy_2y", "size_vs_hist", "routine", "ret_21", "ret_63", "ret_126",
                      "drawdown_252", "vol_63", "log_value_to_adv"]


# ================================================================================================ loading


def load_open_market(as_of: date | None = None, settings: Settings | None = None,
                     resolve: bool = True) -> pl.DataFrame:
    """Open-market, non-derivative buys/sells known by `as_of` (None = full history) with superseded rows removed."""
    s = settings or get_settings()
    lf = scan_table("insider_transactions", s)
    names = lf.collect_schema().names()
    lf = lf.select([c for c in _TXN_COLS if c in names])
    if as_of is not None:
        lf = lf.filter(pl.col("knowledge_date") <= as_of)
    df = lf.filter(pl.col("txn_type").is_in(["buy", "sell"]) & pl.col("is_open_market")
                   & ~pl.col("is_derivative").fill_null(False) & (pl.col("shares") > 0)).collect()
    if "superseded_by_filing_id" in df.columns and df["superseded_by_filing_id"].is_not_null().any():
        amend_kd = (scan_table("insider_transactions", s).select("filing_id", "knowledge_date")
                    .filter(pl.col("filing_id").is_in(df["superseded_by_filing_id"].drop_nulls().unique().implode()))
                    .group_by("filing_id").agg(pl.col("knowledge_date").min().alias("__sup_kd")).collect())
        df = df.join(amend_kd, left_on="superseded_by_filing_id", right_on="filing_id", how="left")
        # amendment not found -> treat as superseded from the row's own filing date (conservative)
        sup_kd = pl.coalesce("__sup_kd", "knowledge_date")
        keep = pl.col("superseded_by_filing_id").is_null()
        if as_of is not None:
            keep = keep | (sup_kd > as_of)
        df = df.filter(keep).drop("__sup_kd")
    if resolve:
        df = resolve_entities(df, as_of=None)
    return df.with_columns(
        pl.when(pl.col("txn_type") == "buy").then(1).otherwise(-1).cast(pl.Int8).alias("direction"),
        pl.col("role_norm").fill_null("other"),
        pl.coalesce("issuer_id", pl.concat_str([pl.col("source_id"), pl.lit(":"), pl.col("source_entity_id")]))
        .alias("issuer_key"),
    )


# ================================================================================================ price features


def price_features(prices: pl.DataFrame) -> pl.DataFrame:
    """Per (security_id, date): trailing returns, drawdown vs 252d high, 63d vol, 20d ADV (USD), row index."""
    if prices.is_empty():
        return prices
    p = prices.sort(["security_id", "date"])
    lr = (pl.col("adj_close") / pl.col("adj_close").shift(1)).log()
    return p.with_columns(
        pl.int_range(pl.len()).over("security_id").alias("idx"),
        (pl.col("adj_close") / pl.col("adj_close").shift(21) - 1).over("security_id").alias("ret_21"),
        (pl.col("adj_close") / pl.col("adj_close").shift(63) - 1).over("security_id").alias("ret_63"),
        (pl.col("adj_close") / pl.col("adj_close").shift(126) - 1).over("security_id").alias("ret_126"),
        (pl.col("adj_close") / pl.col("adj_close").rolling_max(252, min_samples=20) - 1).over("security_id")
        .alias("drawdown_252"),
        lr.rolling_std(63, min_samples=20).over("security_id").alias("vol_63"),
        pl.col("dollar_volume_usd").rolling_mean(20, min_samples=5).over("security_id").alias("adv20_usd"),
    )


def attach_price_features(df: pl.DataFrame, pf: pl.DataFrame, date_col: str = "knowledge_date") -> pl.DataFrame:
    """As-of (backward) join of price features on `date_col` (the close on or before the filing date)."""
    cols = ["ret_21", "ret_63", "ret_126", "drawdown_252", "vol_63", "adv20_usd", "close_usd_px", "px_date"]
    if pf.is_empty() or df.is_empty():
        return df.with_columns([pl.lit(None, pl.Float64).alias(c) for c in cols[:-1]] +
                               [pl.lit(None, pl.Date).alias("px_date")])
    right = pf.select("security_id", pl.col("date").alias("px_date"), "ret_21", "ret_63", "ret_126", "drawdown_252",
                      "vol_63", "adv20_usd", pl.coalesce("close_usd", "adj_close").alias("close_usd_px"))
    left = df.with_row_index("__r").sort(date_col)
    has = left.filter(pl.col("security_id").is_not_null())
    out = has.join_asof(right.sort("px_date"), left_on=date_col, right_on="px_date", by="security_id",
                        strategy="backward", tolerance=timedelta(days=10), check_sortedness=False)
    out = pl.concat([out, left.filter(pl.col("security_id").is_null())], how="diagonal_relaxed")
    return out.sort("__r").drop("__r")


# ================================================================================================ transaction features


def transaction_features(om: pl.DataFrame, pf: pl.DataFrame | None = None) -> pl.DataFrame:
    """Per-transaction PIT features (see module docstring). `pf` = price_features() output (optional)."""
    if om.is_empty():
        return om
    df = om
    if pf is not None:
        df = attach_price_features(df, pf, "knowledge_date")
    else:
        df = attach_price_features(df, pl.DataFrame(), "knowledge_date")
    # USD value: reported value, else shares x price (USD filings), else shares x close_usd near the filing
    df = df.with_columns(
        pl.coalesce(
            pl.col("value_usd"),
            pl.when(pl.col("currency").fill_null("USD") == "USD").then(pl.col("shares") * pl.col("price")),
            pl.col("shares") * pl.col("close_usd_px"),
        ).clip(0, 5e9).alias("value_usd_est"))  # cap obvious unit errors (e.g. price in cents)
    df = df.with_columns(
        pl.col("value_usd_est").fill_null(0.0).clip(0, None).log1p().alias("log_value"),
        pl.col("role_norm").replace_strict(ROLE_WEIGHT, default=1.0, return_dtype=pl.Float64).alias("role_w"),
        pl.col("is_10b5_1").fill_null(False).cast(pl.Float64).alias("is_10b5_1_f"),
        pl.col("holding_change_pct").clip(-1.0, 5.0).alias("holding_change"),
        (pl.col("knowledge_date") - pl.col("transaction_date")).dt.total_days().clip(0, 365).cast(pl.Float64)
        .alias("filing_lag"),
        pl.when(pl.col("adv20_usd") > 0).then(pl.col("value_usd_est") / pl.col("adv20_usd")).otherwise(None)
        .clip(0, None).log1p().alias("log_value_to_adv"),
    )
    # --- distinct same-direction insiders filed within the last 30 days (PIT: by knowledge_date)
    df = df.sort(["issuer_key", "direction", "knowledge_date"])
    roll = (df.select("issuer_key", "direction", "knowledge_date", "insider_id")
            .rolling(index_column="knowledge_date", period=f"{CLUSTER_WINDOW_D}d", group_by=["issuer_key", "direction"])
            .agg(pl.col("insider_id").n_unique().alias("n_same_dir_30d"))
            .unique(["issuer_key", "direction", "knowledge_date"], keep="last"))
    df = df.join(roll, on=["issuer_key", "direction", "knowledge_date"], how="left")
    # --- insider history at the issuer (prior trades, strictly earlier filings)
    df = df.sort(["issuer_key", "insider_id", "knowledge_date", "txn_id"])
    grp = ["issuer_key", "insider_id"]
    prev_buy_kd = (pl.when(pl.col("direction") == 1).then(pl.col("knowledge_date")).otherwise(None)
                   .shift(1).forward_fill().over(grp))
    df = df.with_columns(prev_buy_kd.alias("__prev_buy_kd"))
    # same-filing rows share a date: only filings strictly before count as history
    df = df.with_columns(
        pl.when(pl.col("__prev_buy_kd") == pl.col("knowledge_date")).then(None).otherwise(pl.col("__prev_buy_kd"))
        .alias("__prev_buy_kd"))
    df = df.with_columns(
        pl.when(pl.col("direction") == 1).then(
            pl.col("__prev_buy_kd").is_null()
            | ((pl.col("knowledge_date") - pl.col("__prev_buy_kd")).dt.total_days() > 365 * FIRST_BUY_YEARS))
        .otherwise(False).cast(pl.Float64).alias("first_buy_2y"))
    g3 = ["issuer_key", "insider_id", "direction"]
    csum = pl.col("log_value").cum_sum().over(g3) - pl.col("log_value")
    ccnt = pl.col("log_value").cum_count().over(g3) - 1
    df = df.with_columns(
        pl.when(ccnt > 0).then(pl.col("log_value") - csum / ccnt).otherwise(None).alias("size_vs_hist"))
    # --- Cohen-Malloy-Pomorski routine: traded in the same calendar month in each of the 3 prior years
    ym = df.select("issuer_key", "insider_id", pl.col("transaction_date").dt.year().alias("y"),
                   pl.col("transaction_date").dt.month().alias("m")).unique()
    df = df.with_columns(pl.col("transaction_date").dt.year().alias("__y"), pl.col("transaction_date").dt.month()
                         .alias("__m"))
    for k in (1, 2, 3):
        df = df.join(ym.with_columns((pl.col("y") + k).alias("__y"), pl.lit(True).alias(f"__r{k}")).drop("y")
                     .rename({"m": "__m"}), on=["issuer_key", "insider_id", "__y", "__m"], how="left")
    df = df.with_columns(
        (pl.col("__r1").fill_null(False) & pl.col("__r2").fill_null(False) & pl.col("__r3").fill_null(False))
        .cast(pl.Float64).alias("routine")).drop("__r1", "__r2", "__r3", "__y", "__m", "__prev_buy_kd")
    return df


# ================================================================================================ clusters


def assign_clusters(df: pl.DataFrame, gap_days: int = CLUSTER_WINDOW_D,
                    min_insiders: int = CLUSTER_MIN_INSIDERS_ID) -> pl.DataFrame:
    """cluster_id + cluster_n_insiders on open-market buys (null for sells / singleton episodes)."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, pl.Utf8).alias("cluster_id"), pl.lit(None, pl.Int32).alias("cluster_n"))
    b = df.filter(pl.col("direction") == 1).select("txn_id", "issuer_key", "insider_id", "transaction_date") \
        .sort(["issuer_key", "transaction_date"])
    b = b.with_columns(
        ((pl.col("transaction_date") - pl.col("transaction_date").shift(1)).dt.total_days().over("issuer_key")
         .fill_null(10**6) > gap_days).cum_sum().over("issuer_key").alias("__ep"))
    ep = b.group_by("issuer_key", "__ep").agg(pl.col("insider_id").n_unique().alias("cluster_n"),
                                              pl.col("transaction_date").min().alias("__d0"))
    ep = ep.with_columns(
        pl.when(pl.col("cluster_n") >= min_insiders).then(
            pl.concat_str([pl.lit("C"), pl.col("issuer_key").str.replace_all(r"[^A-Za-z0-9]", ""), pl.lit("-"),
                           pl.col("__d0").dt.strftime("%Y%m%d")])).otherwise(None).alias("cluster_id"))
    b = b.join(ep, on=["issuer_key", "__ep"], how="left").select(
        "txn_id", "cluster_id", pl.when(pl.col("cluster_id").is_not_null()).then(pl.col("cluster_n")).cast(pl.Int32)
        .alias("cluster_n"))
    return df.join(b, on="txn_id", how="left")


# ================================================================================================ signals


def txn_weight_exprs() -> tuple[pl.Expr, pl.Expr]:
    """(buy weight, sell weight) per transaction, before time decay."""
    size = (pl.col("value_usd_est").fill_null(0.0) / 1e4).log1p()
    buy = (pl.col("role_w")
           * (1 + 0.5 * (pl.col("n_same_dir_30d").fill_null(1) >= CLUSTER_MIN_INSIDERS_SIGNAL).cast(pl.Float64)
              + 0.4 * pl.col("first_buy_2y").fill_null(0.0)
              + 0.4 * (pl.col("drawdown_252").fill_null(0.0) < -0.2).cast(pl.Float64))
           * pl.when(pl.col("is_10b5_1_f") > 0).then(0.5).otherwise(1.0)
           * pl.when(pl.col("routine") > 0).then(0.6).otherwise(1.0) * size)
    sell = (pl.col("role_w") * 0.35
            * pl.when((pl.col("is_10b5_1_f") > 0) | (pl.col("routine") > 0)).then(0.2).otherwise(1.0)
            * (1 + 0.3 * (pl.col("ret_126").fill_null(0.0) > 0.3).cast(pl.Float64)) * size)
    return buy, sell


def compute_rule_raw(feat: pl.DataFrame) -> pl.DataFrame:
    """Rule-based contribution of each filing-day event (sum of buy weights - sell weights): [issuer_key,
    knowledge_date, rule_raw]. Used as the honest benchmark for the XGBoost model."""
    buy_w, sell_w = txn_weight_exprs()
    return feat.with_columns(pl.when(pl.col("direction") == 1).then(buy_w).otherwise(-sell_w).alias("__c")) \
        .group_by("issuer_key", "knowledge_date").agg(pl.col("__c").sum().alias("rule_raw"))


def compute_signals(feat: pl.DataFrame, dates: list[date], event_probs: pl.DataFrame | None = None,
                    base_rate: float | None = None, mcap: pl.DataFrame | None = None) -> pl.DataFrame:
    """Per (as_of_date, issuer) insider signal from transaction features (rows with knowledge_date <= date only).

    event_probs: [issuer_key, knowledge_date, xgb_prob_up] (model output per filing-day event, PIT-safe: OOS or
    post-training-window predictions only). mcap: [security_id, date, market_cap_usd]."""
    if feat.is_empty() or not dates:
        return pl.DataFrame()
    buy_w, sell_w = txn_weight_exprs()
    base = feat.with_columns(buy_w.alias("__bw"), sell_w.alias("__sw"))
    outs = []
    for d in sorted(set(dates)):
        w = base.filter((pl.col("knowledge_date") <= d) & (pl.col("knowledge_date") > d - timedelta(days=90)))
        if w.is_empty():
            continue
        age = (pl.lit(d) - pl.col("knowledge_date")).dt.total_days().cast(pl.Float64)
        dec = (0.5 ** (age / DECAY_HALFLIFE_D))
        w = w.with_columns(
            age.alias("__age"),
            pl.when(pl.col("direction") == 1).then(dec * pl.col("__bw")).otherwise(-dec * pl.col("__sw")).alias("__c"),
            (pl.col("direction").cast(pl.Float64) * pl.col("value_usd_est").fill_null(0.0)).alias("__sv"))
        in30 = pl.col("__age") < 30
        buy = pl.col("direction") == 1
        sell = pl.col("direction") == -1
        agg = w.group_by("issuer_key").agg(
            pl.col("issuer_id").drop_nulls().first(), pl.col("security_id").drop_nulls().first(),
            pl.col("__sv").filter(in30).sum().alias("net_buy_value_usd_30d"),
            pl.col("__sv").sum().alias("net_buy_value_usd_90d"),
            pl.col("insider_id").filter(buy).n_unique().cast(pl.Int32).alias("n_buyers_90d"),
            pl.col("insider_id").filter(sell).n_unique().cast(pl.Int32).alias("n_sellers_90d"),
            (pl.col("n_same_dir_30d").filter(buy & in30).max().fill_null(0) >= CLUSTER_MIN_INSIDERS_SIGNAL)
            .alias("cluster_buy"),
            (buy & pl.col("role_norm").is_in(C_SUITE)).any().alias("c_suite_buy"),
            pl.col("value_usd_est").filter(buy & (pl.col("routine") == 0)).sum().alias(
                "opportunistic_buy_value_usd_90d"),
            pl.col("value_usd_est").filter(sell & (pl.col("routine") == 0) & (pl.col("is_10b5_1_f") == 0)).sum()
            .alias("opportunistic_sell_value_usd_90d"),
            pl.col("__c").sum().alias("__raw"),
            pl.col("anomaly_score").filter(pl.col("knowledge_date") == pl.col("knowledge_date").max()).max()
            .alias("anomaly_score") if "anomaly_score" in w.columns
            else pl.lit(None, pl.Float64).alias("anomaly_score"),
            pl.col("txn_id").sort_by(pl.col("__c").abs(), descending=True).head(5).alias("__top"),
        )
        agg = agg.with_columns(pl.lit(d).alias("as_of_date"))
        if event_probs is not None and event_probs.height:
            ep = (event_probs.filter((pl.col("knowledge_date") <= d) & (pl.col("knowledge_date") > d - timedelta(days=90)))
                  .sort("knowledge_date").group_by("issuer_key").agg(pl.col("xgb_prob_up").last()))
            agg = agg.join(ep, on="issuer_key", how="left")
        else:
            agg = agg.with_columns(pl.lit(None, pl.Float64).alias("xgb_prob_up"))
        outs.append(agg)
    if not outs:
        return pl.DataFrame()
    sig = pl.concat(outs, how="diagonal_relaxed")
    # signed-log compression then tanh: one CEO $1M buy ~ 74, large multi-insider cluster ~ 94, heavy selling ~ 10
    comp = pl.col("__raw").sign() * (pl.col("__raw").abs() / SCORE_SCALE).log1p()
    rule = 50 + 50 * (comp / SCORE_TANH).tanh()
    if base_rate is not None:
        model = (50 + 250 * (pl.col("xgb_prob_up") - base_rate)).clip(0, 100)
        score = pl.when(pl.col("xgb_prob_up").is_not_null()).then(0.7 * rule + 0.3 * model).otherwise(rule)
    else:
        score = rule
    sig = sig.with_columns(score.clip(0, 100).alias("signal_score"))
    if mcap is not None and mcap.height:
        m = mcap.sort("date").rename({"date": "__md"})
        sig = sig.sort("as_of_date").join_asof(m, left_on="as_of_date", right_on="__md", by="security_id",
                                               strategy="backward", tolerance=timedelta(days=10), check_sortedness=False)
        sig = sig.with_columns((pl.col("net_buy_value_usd_90d") / pl.col("market_cap_usd") * 1e4)
                               .alias("net_buy_mcap_bps_90d"))
    else:
        sig = sig.with_columns(pl.lit(None, pl.Float64).alias("net_buy_mcap_bps_90d"))
    sig = sig.with_columns(
        pl.col("__top").map_elements(lambda x: orjson.dumps(list(x)).decode(), return_dtype=pl.Utf8)
        .alias("top_transactions"))
    return sig.drop([c for c in sig.columns if c.startswith("__") or c in ("market_cap_usd",)])


# ================================================================================================ job


def _mcap_frame(settings: Settings) -> pl.DataFrame | None:
    if not has_data("valuation_daily", settings):
        return None
    try:
        return scan_table("valuation_daily", settings).select("security_id", "date", "market_cap_usd") \
            .filter(pl.col("market_cap_usd").is_not_null()).collect()
    except Exception:  # noqa: BLE001
        return None


def signal_dates(as_of: date, mode: str, start: date | None = None, daily_days: int = 30) -> list[date]:
    if mode != "history":
        return [as_of]
    start = start or date(2017, 1, 1)
    out: list[date] = []
    d = date(start.year, start.month, 1)
    while d <= as_of:
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        me = nxt - timedelta(days=1)
        if me <= as_of:
            out.append(me)
        d = nxt
    out += [as_of - timedelta(days=i) for i in range(daily_days)]
    return sorted(set(out))


def build_features(settings: Settings | None = None, as_of: date | None = None,
                   models: Any | None = None) -> pl.DataFrame:
    """Open-market transactions with features, clusters and (if a model is persisted) anomaly scores."""
    s = settings or get_settings()
    om = load_open_market(as_of, s)
    if om.is_empty():
        return om
    secs = om.get_column("security_id").drop_nulls().unique().to_list()
    start = om.get_column("knowledge_date").min() - timedelta(days=400)
    pf = price_features(load_prices(secs, start, s)) if secs else pl.DataFrame()
    feat = assign_clusters(transaction_features(om, pf))
    try:
        from app.analytics import insider_models  # noqa: PLC0415

        feat = insider_models.score_anomaly(feat, models)
    except Exception as e:  # noqa: BLE001 - no model yet
        log.info("anomaly scores unavailable: %s", e)
        feat = feat.with_columns(pl.lit(None, pl.Float64).alias("anomaly_score"))
    return feat


def txn_scores_frame(feat: pl.DataFrame, run_id: str | None) -> pl.DataFrame:
    now = datetime.now(UTC)
    cols = ["txn_id", "issuer_id", "security_id", "source_entity_id", "knowledge_date", "transaction_date",
            "direction", "value_usd_est", "role_w", "n_same_dir_30d", "first_buy_2y", "routine", "size_vs_hist",
            "drawdown_252", "ret_126", "cluster_id", "cluster_n", "anomaly_score"]
    out = feat.select([c for c in cols if c in feat.columns]).with_columns(
        pl.col("knowledge_date").dt.year().cast(pl.Int16).alias("year"),
        pl.col("first_buy_2y").cast(pl.Boolean).alias("first_buy_2y"),
        pl.col("routine").cast(pl.Boolean).alias("routine"),
        pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id"))
    return out.rename({"value_usd_est": "value_usd", "role_w": "role_weight", "n_same_dir_30d": "n_insiders_same_dir_30d",
                       "first_buy_2y": "first_buy_2y", "cluster_n": "cluster_n_insiders", "ret_126": "pre_ret_126d",
                       "drawdown_252": "pre_drawdown_252d"})


def run(ctx):  # noqa: ANN001 - JobContext
    from app.analytics import insider_models  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    mode = p.get("mode", "latest")
    models = insider_models.load_models(s)
    feat = build_features(s, None if mode == "history" else ctx.as_of, models)
    if feat.is_empty():
        return JobResult(status="success", message="no open-market insider transactions")
    ctx.check_cancelled()
    rows: dict[str, int] = {}
    if not p.get("skip_txn_scores"):
        ts = txn_scores_frame(feat.filter(pl.col("knowledge_date") >= date(1990, 1, 1)), ctx.run_id)
        if mode != "history":  # nightly: rewrite only the recent window (clusters/anomaly of older rows are stable)
            ts = ts.filter(pl.col("knowledge_date") > ctx.as_of - timedelta(days=400))
        rows["insider_txn_scores"] = write_table(ts, "insider_txn_scores", settings=s)
    events = insider_models.event_frame(feat)
    probs = insider_models.predict_events(events, models) if models else None
    meta = (models or {}).get("meta", {})
    # blend the model into signal_score only if it passed the OOS promotion gate
    base_rate = meta.get("base_rate_3m") if meta.get("gate_passed") else None
    dates = signal_dates(ctx.as_of, mode, date.fromisoformat(p["start"]) if p.get("start") else None,
                         int(p.get("daily_days", 30)))
    sig_parts = []
    for i in range(0, len(dates), 24):
        ctx.check_cancelled()
        sig_parts.append(compute_signals(feat, dates[i:i + 24], probs, base_rate, _mcap_frame(s) if i == 0 else None))
    sig = pl.concat([x for x in sig_parts if x.height], how="diagonal_relaxed") if sig_parts else pl.DataFrame()
    n_unres = 0
    if sig.height:
        n_unres = sig.filter(pl.col("security_id").is_null() | pl.col("issuer_id").is_null()).height
        sig = sig.filter(pl.col("security_id").is_not_null() & pl.col("issuer_id").is_not_null())
        sig = sig.unique(["as_of_date", "security_id"], keep="first").with_columns(
            pl.col("as_of_date").dt.year().cast(pl.Int16).alias("year"), pl.lit(datetime.now(UTC)).alias("computed_at"),
            pl.lit(ctx.run_id).alias("run_id")).drop("issuer_key")
        rows["insider_signals"] = write_table(sig, "insider_signals", settings=s)
    latest = sig.filter(pl.col("as_of_date") == ctx.as_of) if sig.height else sig
    msg = (f"txns={feat.height} dates={len(dates)} signals={sig.height} latest_issuers={latest.height} "
           f"unresolved_issuer_dates={n_unres} model={meta.get('version', 'none')} gate={meta.get('gate_passed')}")
    return JobResult(status="success", rows_written=rows, entities_ok=latest.height, message=msg)
