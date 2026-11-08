"""Daily sentiment aggregates per security (job `sentiment_daily`, owner signals-alt).

Inputs (all PIT: an item counts on day D only if knowledge_date <= D):
  news_items         model sentiment (FinBERT / XLM-R), GDELT tone, Polygon provider sentiment, issuer_relevance
  gdelt_company_daily avg_tone / mentions / coverage flag
  filing_text_changes latest Lazy Prices similarity + PIT universe percentile
  prices_daily        20-session return for sentiment/price divergence

Item score = weighted mean of the available opinions (model 0.6, provider 0.25, GDELT tone/5 0.15), then
DE-MEANED PER SOURCE by that source's trailing 30-day (incl. same day) universe mean item score (GKG headlines, Polygon and RSS feeds have
different tone levels). Per issuer-day: relevance-weighted mean (items with relevance < 0.5 are ignored, i.e. GKG organisation-only mentions whose title does not name the company count for GDELT attention but not for sentiment).

Robustness to source-volume collapses (GDELT DOC/GKG outages): per source and day the universe-wide item volume is
compared with its trailing 30-day median; days below 50% are flagged (coverage_ok=false). On such days attention is
NOT computed from the depressed counts (news_count_z = null) and missing volume never enters the sentiment means:
days without items carry no information (irregular-time EWM where absent days only decay the weight; the EWM is
set to null once its effective weight falls below `min_weight`, i.e. stale).

Outputs: sentiment_daily rows for every (security, day) in the window with any input. sentiment_score (0..100) blends
the per-date cross-sectional percentiles of sentiment_ewm_30d (0.40) and gdelt_tone_ewm_7d (0.20) with lazy_prices_pct
(0.40, already a PIT percentile) like scoring.yaml, renormalised over the available components; 50 = no data.

params: mode latest|history; write_days (nightly rows written, default 7); start (history start, default as_of-365);
        min_relevance (0.5).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table
from app.nlp.common import ewm_by_group, has_data, load_prices, rolling_z, security_map

log = get_logger(__name__)

W_MODEL, W_PROVIDER, W_TONE = 0.6, 0.25, 0.15
MIN_WEIGHT = 0.05
COVERAGE_RATIO = 0.5


def _provider_by_ticker(ps: str | None, ticker: str | None) -> float | None:
    from app.nlp.sentiment import provider_score  # noqa: PLC0415

    if not ps:
        return None
    v = provider_score(ps, [ticker] if ticker else None)
    return v if v is not None else provider_score(ps)


def item_issuer_frame(news: pl.DataFrame, secmap: pl.DataFrame, min_relevance: float = 0.5) -> pl.DataFrame:
    """Explode news to (item, issuer) with relevance and the combined item score."""
    if news.is_empty():
        return pl.DataFrame()
    n = news.filter(pl.col("issuer_ids").list.len() > 0)
    if "issuer_relevance" not in n.columns:
        n = n.with_columns(pl.lit(None, pl.List(pl.Float64)).alias("issuer_relevance"))
    n = n.with_columns(
        pl.when(pl.col("issuer_relevance").list.len() == pl.col("issuer_ids").list.len())
        .then(pl.col("issuer_relevance"))
        .otherwise(pl.col("issuer_ids").list.eval(pl.lit(0.5))).alias("__rel"))
    ex = n.select("item_id", "source_id", "knowledge_date", "sentiment_score", "gdelt_tone", "provider_sentiment",
                  pl.col("issuer_ids").alias("issuer_id"), pl.col("__rel").alias("relevance")).explode(
        ["issuer_id", "relevance"])
    ex = ex.filter(pl.col("relevance") >= min_relevance)
    ex = ex.join(secmap.select("issuer_id", "ticker"), on="issuer_id", how="left")
    prov = [_provider_by_ticker(ps, t) for ps, t in zip(ex["provider_sentiment"].to_list(), ex["ticker"].to_list(),
                                                         strict=True)]
    ex = ex.with_columns(pl.Series("provider_score", prov, pl.Float64),
                         (pl.col("gdelt_tone") / 5).clip(-1, 1).alias("__tone"))
    parts = [(pl.col("sentiment_score"), W_MODEL), (pl.col("provider_score"), W_PROVIDER), (pl.col("__tone"), W_TONE)]
    num = sum(e.fill_null(0.0) * w for e, w in parts)
    den = sum(e.is_not_null().cast(pl.Float64) * w for e, w in parts)
    return ex.with_columns(pl.when(den > 0).then(num / den).otherwise(None).alias("item_score")).drop("__tone")


def source_baselines(ex: pl.DataFrame, all_news: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(per source-day score baseline [source_id, knowledge_date, score_base],
        per source-day coverage [source_id, knowledge_date, coverage_ratio])."""
    vol = all_news.group_by("source_id", "knowledge_date").agg(pl.len().alias("n")).sort(["source_id",
                                                                                          "knowledge_date"])
    vol = vol.with_columns(pl.col("n").cast(pl.Float64).shift(1).rolling_median(30, min_samples=5).over("source_id")
                           .alias("__med"))
    cov = vol.select("source_id", "knowledge_date",
                     pl.when(pl.col("__med") > 0).then(pl.col("n") / pl.col("__med")).otherwise(None)
                     .alias("coverage_ratio"))
    sm = ex.group_by("source_id", "knowledge_date").agg(pl.col("item_score").mean().alias("m"),
                                                        pl.col("item_score").count().alias("c")).sort(
        ["source_id", "knowledge_date"])
    # trailing 30 source-days INCLUDING the current day (same-day items are known by the end of day D -> PIT-safe):
    # the aggregate measures sentiment relative to the source's recent average across the whole universe
    num = (pl.col("m").fill_null(0.0) * pl.col("c")).rolling_sum(30, min_samples=1).over("source_id")
    den = pl.col("c").cast(pl.Float64).rolling_sum(30, min_samples=1).over("source_id")
    sm = sm.with_columns(pl.when(den > 0).then(num / den).otherwise(None).alias("score_base"))
    return sm.select("source_id", "knowledge_date", pl.col("score_base").fill_nan(None).fill_null(0.0)), cov


def daily_frame(ex: pl.DataFrame, base: pl.DataFrame, cov: pl.DataFrame) -> pl.DataFrame:
    """(issuer_id, date) -> news_count, sentiment sums, coverage."""
    e = ex.join(base, on=["source_id", "knowledge_date"], how="left").with_columns(
        (pl.col("item_score") - pl.col("score_base").fill_null(0.0)).alias("score_norm")).join(
        cov, on=["source_id", "knowledge_date"], how="left")
    return e.group_by("issuer_id", pl.col("knowledge_date").alias("date")).agg(
        pl.col("item_id").n_unique().cast(pl.Int32).alias("news_count"),
        (pl.col("relevance") * pl.col("score_norm")).filter(pl.col("score_norm").is_not_null()).sum().alias("__num"),
        pl.col("relevance").filter(pl.col("score_norm").is_not_null()).sum().alias("__den"),
        pl.col("relevance").sum().alias("relevance_sum"),
        pl.col("source_id").n_unique().cast(pl.Int32).alias("n_sources"),
        pl.col("provider_score").mean().alias("provider_sentiment_mean"),
        (pl.col("relevance") / pl.col("coverage_ratio").clip(0.25, None).fill_null(1.0)).sum().alias("__adj_count"),
        (pl.col("coverage_ratio").fill_null(1.0) >= COVERAGE_RATIO).all().alias("__cov_items"),
    )


def build(settings: Settings | None = None, as_of: date | None = None, start: date | None = None,
          min_relevance: float = 0.5) -> pl.DataFrame:
    s = settings or get_settings()
    as_of = as_of or date.today()
    start = start or as_of - timedelta(days=365)
    load_from = start - timedelta(days=150)
    secmap = security_map(s)
    news = pl.DataFrame()
    if has_data("news_items", s):
        news = scan_table("news_items", s).filter(
            (pl.col("knowledge_date") >= load_from) & (pl.col("knowledge_date") <= as_of)).collect()
    ex = item_issuer_frame(news, secmap, min_relevance) if news.height else pl.DataFrame()
    parts = []
    day_cov = None
    if ex.height:
        base, cov = source_baselines(ex, news)
        parts.append(daily_frame(ex, base, cov))
        # a day is degraded if any source that normally contributes is below 50% of its baseline
        day_cov = cov.group_by("knowledge_date").agg(
            (pl.col("coverage_ratio").fill_null(1.0) >= COVERAGE_RATIO).all().alias("__day_cov")).rename(
            {"knowledge_date": "date"})
    gd = pl.DataFrame()
    if has_data("gdelt_company_daily", s):
        gd = scan_table("gdelt_company_daily", s).filter(
            (pl.col("date") >= load_from) & (pl.col("date") <= as_of)).collect()
    lp = pl.DataFrame()
    if has_data("filing_text_changes", s):
        lp = scan_table("filing_text_changes", s).filter(pl.col("knowledge_date") <= as_of).collect()
    issuers: set[str] = set()
    for df in (parts[0] if parts else pl.DataFrame(), gd, lp):
        if df.height and "issuer_id" in df.columns:
            issuers |= set(df.get_column("issuer_id").drop_nulls().to_list())
    if not issuers:
        return pl.DataFrame()
    days = pl.date_range(load_from, as_of, "1d", eager=True).alias("date")
    grid = pl.DataFrame({"issuer_id": sorted(issuers)}).join(days.to_frame(), how="cross")
    if parts:
        grid = grid.join(parts[0], on=["issuer_id", "date"], how="left")
    else:
        grid = grid.with_columns(pl.lit(None, pl.Int32).alias("news_count"), pl.lit(None, pl.Float64).alias("__num"),
                                 pl.lit(None, pl.Float64).alias("__den"), pl.lit(None, pl.Float64).alias("__adj_count"),
                                 pl.lit(None, pl.Boolean).alias("__cov_items"),
                                 pl.lit(None, pl.Float64).alias("relevance_sum"), pl.lit(None, pl.Int32).alias("n_sources"),
                                 pl.lit(None, pl.Float64).alias("provider_sentiment_mean"))
    grid = grid.with_columns(pl.col("news_count").fill_null(0))
    if day_cov is not None:
        grid = grid.join(day_cov, on="date", how="left")
    else:
        grid = grid.with_columns(pl.lit(None, pl.Boolean).alias("__day_cov"))
    if gd.height:
        # GDELT tone relative to the universe (mention-weighted) mean tone of the trailing 30 days incl. the day
        tm = gd.group_by("date").agg(((pl.col("avg_tone") * pl.col("mentions")).sum()).alias("__s"),
                                     pl.col("mentions").sum().cast(pl.Float64).alias("__n")).sort("date")
        tm = tm.with_columns((pl.col("__s").rolling_sum(30, min_samples=1) / pl.col("__n").rolling_sum(30, min_samples=1))
                             .alias("__tbase")).select("date", "__tbase")
        gd = gd.join(tm, on="date", how="left").with_columns((pl.col("avg_tone") - pl.col("__tbase")).alias("avg_tone"))
        grid = grid.join(gd.select("issuer_id", "date", pl.col("avg_tone").alias("__gt"),
                                   pl.col("mentions").cast(pl.Float64).alias("__gm"),
                                   pl.col("coverage_ok").alias("__gcov") if "coverage_ok" in gd.columns
                                   else pl.lit(None, pl.Boolean).alias("__gcov")), on=["issuer_id", "date"], how="left")
    else:
        grid = grid.with_columns(pl.lit(None, pl.Float64).alias("__gt"), pl.lit(None, pl.Float64).alias("__gm"),
                                 pl.lit(None, pl.Boolean).alias("__gcov"))
    grid = grid.sort(["issuer_id", "date"]).with_columns(
        pl.when(pl.col("__den") > 0).then(pl.col("__num") / pl.col("__den")).otherwise(None).alias("sentiment_mean"),
        pl.coalesce(pl.col("__day_cov"), pl.col("__gcov"), pl.lit(True)).alias("coverage_ok"),
        (pl.col("__gt") * pl.col("__gm")).alias("__gnum"))
    grid = ewm_by_group(grid, "issuer_id", "date", "__num", "__den", 7, "sentiment_ewm_7d", "__w7")
    grid = ewm_by_group(grid, "issuer_id", "date", "__num", "__den", 30, "sentiment_ewm_30d", "__w30")
    grid = ewm_by_group(grid, "issuer_id", "date", "__gnum", "__gm", 7, "gdelt_tone_ewm_7d", "__wg")
    grid = grid.with_columns(
        pl.when(pl.col("__w7") >= MIN_WEIGHT).then(pl.col("sentiment_ewm_7d")).alias("sentiment_ewm_7d"),
        pl.when(pl.col("__w30") >= MIN_WEIGHT).then(pl.col("sentiment_ewm_30d")).alias("sentiment_ewm_30d"),
        pl.when(pl.col("__wg") >= MIN_WEIGHT).then(pl.col("gdelt_tone_ewm_7d")).alias("gdelt_tone_ewm_7d"))
    # attention: coverage-adjusted log count, null on degraded days (never "less news")
    grid = grid.with_columns(
        pl.when(pl.col("coverage_ok") & pl.col("__cov_items").fill_null(True))
        .then(pl.col("__adj_count").fill_null(0.0).log1p()).otherwise(None).alias("__att"))
    grid = rolling_z(grid, "issuer_id", "__att", 90, "news_count_z", min_periods=10)
    grid = rolling_z(grid, "issuer_id", "sentiment_ewm_7d", 365, "sentiment_z", min_periods=20)
    grid = grid.with_columns((pl.col("sentiment_ewm_7d") - pl.col("sentiment_ewm_30d")).alias("sentiment_momentum"))
    # Lazy Prices: latest full-document (fallback risk factors) similarity known by the date
    if lp.height:
        lps = (lp.filter(pl.col("section").is_in(["full", "risk_factors"]))
               .with_columns(pl.when(pl.col("section") == "full").then(0).otherwise(1).alias("__pri"))
               .sort(["issuer_id", "knowledge_date", "__pri"]).unique(["issuer_id", "knowledge_date"], keep="first")
               .select("issuer_id", pl.col("knowledge_date").alias("__lkd"), pl.col("cosine_tfidf").alias("lazy_prices_sim"),
                       pl.col("universe_pct").alias("lazy_prices_pct") if "universe_pct" in lp.columns
                       else pl.lit(None, pl.Float64).alias("lazy_prices_pct"),
                       pl.col("large_change").alias("lazy_prices_large_change") if "large_change" in lp.columns
                       else pl.lit(None, pl.Boolean).alias("lazy_prices_large_change"))
               .sort("__lkd"))
        grid = grid.sort("date").join_asof(lps, left_on="date", right_on="__lkd", by="issuer_id", strategy="backward",
                                           tolerance=timedelta(days=400), check_sortedness=False).drop("__lkd")
    else:
        grid = grid.with_columns(pl.lit(None, pl.Float64).alias("lazy_prices_sim"),
                                 pl.lit(None, pl.Float64).alias("lazy_prices_pct"),
                                 pl.lit(None, pl.Boolean).alias("lazy_prices_large_change"))
    grid = grid.join(secmap.select("issuer_id", "security_id"), on="issuer_id", how="inner").filter(
        pl.col("security_id").is_not_null() & (pl.col("date") >= start))
    grid = _divergence(grid, s, start)
    # 0..100: per-date cross-sectional percentile of each component (heavily covered names dominate the source
    # baselines, so raw levels are not centred), weights as scoring.yaml, renormalised over available components
    pct = lambda c: ((pl.col(c).rank("average").over("date") - 0.5) / pl.col(c).count().over("date"))  # noqa: E731
    comps = [(pct("sentiment_ewm_30d"), 0.40), (pct("gdelt_tone_ewm_7d"), 0.20), (pl.col("lazy_prices_pct"), 0.40)]
    num = sum(c.fill_null(0.0) * w for c, w in comps)
    den = sum(c.is_not_null().cast(pl.Float64) * w for c, w in comps)
    grid = grid.with_columns(pl.when(den > 0).then(100 * num / den).otherwise(50.0).clip(0, 100)
                             .alias("sentiment_score"))
    keep = (pl.col("news_count") > 0) | pl.col("sentiment_ewm_30d").is_not_null() | pl.col("gdelt_tone_ewm_7d").is_not_null() \
        | pl.col("lazy_prices_sim").is_not_null()
    return grid.filter(keep).with_columns(pl.col("date").dt.year().cast(pl.Int16).alias("year")).drop(
        [c for c in grid.columns if c.startswith("__")])


def _divergence(grid: pl.DataFrame, s: Settings, start: date) -> pl.DataFrame:
    """+1 = sentiment momentum up while the 20-session return is down (bullish divergence), -1 the opposite."""
    secs = grid.get_column("security_id").unique().to_list()
    px = load_prices(secs, start - timedelta(days=60), s)
    if px.is_empty():
        return grid.with_columns(pl.lit(None, pl.Float64).alias("ret_20d"), pl.lit(None, pl.Int8).alias("price_divergence"))
    px = px.with_columns((pl.col("adj_close") / pl.col("adj_close").shift(20) - 1).over("security_id").alias("ret_20d"))
    grid = grid.sort("date").join_asof(px.select("security_id", pl.col("date").alias("__pd"), "ret_20d").sort("__pd"),
                                       left_on="date", right_on="__pd", by="security_id", strategy="backward",
                                       tolerance=timedelta(days=7), check_sortedness=False).drop("__pd")
    mom = pl.col("sentiment_momentum")
    return grid.with_columns(
        pl.when(mom.is_null() | pl.col("ret_20d").is_null()).then(None)
        .when((mom > 0.05) & (pl.col("ret_20d") < -0.02)).then(1)
        .when((mom < -0.05) & (pl.col("ret_20d") > 0.02)).then(-1).otherwise(0).cast(pl.Int8).alias("price_divergence"))


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    mode = p.get("mode", "latest")
    start = date.fromisoformat(p["start"]) if p.get("start") else ctx.as_of - timedelta(days=365)
    df = build(s, ctx.as_of, start, float(p.get("min_relevance", 0.5)))
    if df.is_empty():
        return JobResult(status="success", message="no sentiment inputs")
    if mode != "history":
        df = df.filter(pl.col("date") > ctx.as_of - timedelta(days=int(p.get("write_days", 7))))
    df = df.with_columns(pl.lit(datetime.now(UTC)).alias("computed_at")).unique(["date", "security_id"], keep="first")
    n = write_table(df, "sentiment_daily", settings=s)
    latest = df.filter(pl.col("date") == ctx.as_of)
    msg = (f"rows={n} latest_securities={latest.height} with_news_30d={latest.filter(pl.col('sentiment_ewm_30d').is_not_null()).height} "
           f"degraded_days={df.filter(~pl.col('coverage_ok')).get_column('date').n_unique()}")
    return JobResult(status="success", rows_written={"sentiment_daily": n}, entities_ok=latest.height, message=msg)


