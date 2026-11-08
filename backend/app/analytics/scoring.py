"""Peer / self-history / market-cap scoring -> peer_scores, factor_scores, composite_scores (owner: analytics-scoring).

Implements contracts/scoring.yaml (job id `scoring`). For every as-of date D (nightly: ctx.as_of; history mode:
month-ends of the last N years, so ML can use PIT factor scores as features):

1. Metric values known at D (strict PIT: every input has knowledge_date <= D)
   * fundamentals (ratios + sector_kpis): latest TTM row (period_end within 400 d), else latest FY
   * valuation (valuation_daily, last row within 10 d of D); market metrics from prices_daily adj_close
   * insider_signals / sentiment_daily / macro_betas x macro_regimes / moat / forecast_valuation(market_implied)
   Scored metric set per issuer = factor members (with kpi-set overrides) + kpi_set.quality_weights + accounting
   block + kpi_set.peer_metrics ("most relevant" sector metrics), minus kpi_set.excluded_generic.
2. Peer score: average-rank percentile of the direction-adjusted value within the first peer level with >= 8 valid
   values (subsector x peer_region x size -> subsector x region -> subsector -> sector x region -> sector), group key
   and size recorded. Market/sentiment/macro metrics: peer_region -> global.
3. Self-history score: z of the current value vs the issuer's own TTM history (window 10y, >= 8 obs and >= 5y span),
   std floor 0.05|mean|, clip +-3, score = Phi(z) * 100. Valuation metrics use quarter-end valuation samples.
4. Market-cap/quality adjusted score (valuation metrics): OLS within the peer level with >= 20 obs (else sector /
   global) of log(multiple) (yields: level) on log(mcap) + quality factor score + revenue CAGR 3y (winsorized
   1/99%); residual direction-adjusted -> percentile.
5. Blend (fundamental 0.7/0.3, valuation 0.40/0.25/0.35, market/other 1.0; renormalized) -> metric_score.
   positive_only multiples <= 0 (and net debt/EBITDA with EBITDA <= 0) -> score 5, imputed.
6. Factors: weighted mean (renormalized) -> score_raw; coverage = non-imputed weight share; coverage < 0.3 -> null
   (insider/sentiment/macro -> 50 with coverage 0). score = re-percentile within peer_region x sector (min 8, else
   peer_region, else global). Insider and Moat are already on a 0..100 scale and pass through.
7. Composites per profile (ml_blend is written by scoring_ml_blend), >= 5 factors incl. 2 of {quality,
   profitability, value, growth}; grades = quintiles over the scored universe; ranks global and sector x region.

Params: universe (mvp|full), mode (latest|history), years (history depth, default 10), start, end, dates (list),
        tickers, write_peer (bool, default true).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

import numpy as np
import polars as pl
import yaml

from app.analytics import ratios as R
from app.analytics.valuation import universe_frame
from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table

log = get_logger(__name__)

FACTORS = ["quality", "profitability", "growth", "value", "balance_sheet", "momentum", "insider", "sentiment",
           "macro", "moat"]
CORE = ["quality", "profitability", "value", "growth"]
LEVELS = [("subsector_region_size", ["subsector_id", "peer_region", "size_bucket"]),
          ("subsector_region", ["subsector_id", "peer_region"]),
          ("subsector_global", ["subsector_id"]),
          ("sector_region", ["sector_id", "peer_region"]),
          ("sector_global", ["sector_id"])]
MKT_LEVELS = [("region_all", ["peer_region"]), ("global_all", [])]
IMPUTE_WORST_IF_UNDEFINED = {"net_debt_to_ebitda", "net_debt_incl_leases_to_ebitda"}
WORST = 5.0
CONF_FLOOR = 40.0   # scoring.yaml forecast_confidence.min_confidence
SQRT2 = math.sqrt(2.0)


# ------------------------------------------------------------------------------------------------ spec


@lru_cache(maxsize=4)
def _spec_cached(path: str) -> dict[str, Any]:
    return yaml.safe_load(open(path, encoding="utf-8").read())


def spec(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    return _spec_cached(str(s.contracts_dir / "scoring.yaml"))


def smart_money_option(settings: Settings | None = None) -> dict[str, Any] | None:
    """scoring.yaml optional_inputs.smart_money (roadmap #7): the MODEL-BASED smart-money score (score_pct, 0..100)
    as an extra member of one factor. Default OFF - returns None unless enabled: true (production scores unchanged)."""
    o = ((spec(settings).get("optional_inputs") or {}).get("smart_money") or {})
    if not o.get("enabled"):
        return None
    return {"factor": o.get("factor", "insider"), "metric": o.get("metric", "smart_money_score"),
            "weight": float(o.get("member_weight", 0.3)), "max_age_days": int(o.get("max_age_days", 10))}


def metric_catalog(settings: Settings | None = None) -> dict[str, dict[str, Any]]:
    """metric_id -> {kind: fundamental|valuation|market|other|macro, hib, target, positive_only, scaled, source}"""
    sp, reg = spec(settings), R.registry(settings)
    cat: dict[str, dict[str, Any]] = {}
    for mid, m in reg["meta"].items():
        if not m.get("score", True):
            continue
        cat[mid] = {"kind": "fundamental", "hib": m.get("hib"), "target": m.get("target"), "positive_only": False,
                    "scaled": False, "source": m["source"]}
    for mid, m in (sp.get("valuation_metrics") or {}).items():
        cat[mid] = {"kind": "valuation", "hib": m.get("higher_is_better"), "target": None,
                    "positive_only": bool(m.get("positive_only")), "scaled": False, "source": "valuation"}
    for mid, m in (sp.get("market_metrics") or {}).items():
        cat[mid] = {"kind": "market", "hib": m.get("higher_is_better"), "target": None, "positive_only": False,
                    "scaled": False, "source": "market"}
    for mid, m in (sp.get("macro_metrics") or {}).items():
        cat[mid] = {"kind": "other", "hib": m.get("higher_is_better"), "target": None, "positive_only": False,
                    "scaled": False, "source": "macro"}
    for mid, m in (sp.get("other_metrics") or {}).items():
        cat[mid] = {"kind": "other", "hib": m.get("higher_is_better"), "target": None, "positive_only": False,
                    "scaled": bool(m.get("already_scaled_0_100")), "source": m.get("source"), "column": m.get("column")}
    sm = smart_money_option(settings)
    if sm:
        cat[sm["metric"]] = {"kind": "other", "hib": True, "target": None, "positive_only": False, "scaled": True,
                             "source": "smart_money", "column": "score_pct"}
    return cat


def factor_member_table(settings: Settings | None = None) -> pl.DataFrame:
    """kpi_set_id, factor_id, metric_id, weight, block ('main' | 'kpi' | 'acc'), after exclusions."""
    sp, reg = spec(settings), R.registry(settings)
    cat = metric_catalog(settings)
    rows = []
    for set_id, ks in reg["kpi_sets"].items():
        excl = set(ks.get("excluded_generic") or [])
        if not ks.get("rnd_expected"):
            excl.add("rnd_to_revenue")
        for f in sp["factors"]:
            fid = f["id"]
            if fid == "quality":
                parts = [("kpi", 0.8, ks.get("quality_weights") or {}), ("acc", 0.2, f.get("accounting_block") or {})]
            else:
                mem = (f.get("overrides") or {}).get(set_id) or f.get("members") or {}
                sm = smart_money_option(settings)
                if sm and fid == sm["factor"]:
                    mem = {**mem, sm["metric"]: sm["weight"]}
                parts = [("main", 1.0, mem)]
            for block, bw, mem in parts:
                for mid, w in mem.items():
                    if mid in excl or mid not in cat:
                        continue
                    rows.append({"kpi_set_id": set_id, "factor_id": fid, "metric_id": mid, "weight": float(w),
                                 "block": block, "block_weight": bw})
    return pl.DataFrame(rows)


def metric_factor_frame(settings: Settings | None = None) -> pl.DataFrame:
    """kpi_set_id, metric_id, factor_id: the factor a metric feeds for issuers of that kpi set (specific factors
    preferred over the broad quality composite; non-member metrics use metric_factor_map)."""
    pref = {f: i for i, f in enumerate(["profitability", "growth", "value", "balance_sheet", "momentum", "insider",
                                         "sentiment", "macro", "moat", "quality"])}
    fm = factor_member_table(settings).with_columns(
        pl.col("factor_id").replace_strict(pref, default=99, return_dtype=pl.Int32).alias("__o"))
    return fm.sort("__o").group_by("kpi_set_id", "metric_id").agg(pl.col("factor_id").first())


def metric_factor_map(settings: Settings | None = None) -> dict[str, str]:
    fm = factor_member_table(settings)
    out: dict[str, str] = {}
    for fid in FACTORS:
        for m in fm.filter(pl.col("factor_id") == fid)["metric_id"].unique().to_list():
            out.setdefault(m, fid)
    cat = metric_catalog(settings)
    for m, c in cat.items():
        if m in out:
            continue
        if c["kind"] == "valuation":
            out[m] = "value"
        elif "growth" in m or "cagr" in m:
            out[m] = "growth"
        elif c["kind"] == "market":
            out[m] = "momentum"
        else:
            out[m] = "quality"
    return out


def scored_metric_table(settings: Settings | None = None) -> pl.DataFrame:
    """kpi_set_id, metric_id: every metric scored for issuers of the set (members + peer_metrics)."""
    reg = R.registry(settings)
    cat = metric_catalog(settings)
    fm = factor_member_table(settings)
    rows = fm.select("kpi_set_id", "metric_id").unique().rows()
    for set_id, ks in reg["kpi_sets"].items():
        excl = set(ks.get("excluded_generic") or [])
        for m in ks.get("peer_metrics") or []:
            if m in cat and m not in excl:
                rows.append((set_id, m))
    return pl.DataFrame(rows, schema=["kpi_set_id", "metric_id"], orient="row").unique()


# ------------------------------------------------------------------------------------------------ math helpers


def winsorize(e: pl.Expr, by: Sequence[str], lo: float = 0.01, hi: float = 0.99) -> pl.Expr:
    if by:
        return e.clip(e.quantile(lo).over(by), e.quantile(hi).over(by))
    return e.clip(e.quantile(lo), e.quantile(hi))


def pct_rank(v: pl.Expr, by: Sequence[str]) -> tuple[pl.Expr, pl.Expr]:
    """(percentile 0..100 by average rank, n valid) of v within groups `by` (nulls excluded)."""
    if by:
        r = v.rank("average").over(by)
        n = v.count().over(by)
    else:
        r = v.rank("average")
        n = v.count()
    return (r - 0.5) / n * 100.0, n


def norm_cdf(z: pl.Expr) -> pl.Expr:
    # Abramowitz-Stegun 7.1.26 erf approximation (|err| < 1.5e-7), vectorized in Polars
    x = z / SQRT2
    s = pl.when(x < 0).then(-1.0).otherwise(1.0)
    a = x.abs()
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * (-a * a).exp()
    return 0.5 * (1.0 + s * y)


def direction_expr(value: pl.Expr, hib: pl.Expr, target: pl.Expr) -> pl.Expr:
    """100 = best convention: higher_is_better true -> x; false -> -x; band metrics -> -|x - target|."""
    return (pl.when(hib == 1).then(value).when(hib == -1).then(-value)
            .when(target.is_not_null()).then(-(value - target).abs()).otherwise(None))


# ------------------------------------------------------------------------------------------------ loaders


class Inputs:
    """Everything a scoring run needs, loaded once (PIT filters are applied per as-of date)."""

    def __init__(self, universe: str, settings: Settings, tickers: Sequence[str] | None = None,
                 start: date | None = None, end: date | None = None, uni: pl.DataFrame | None = None):
        """`uni` (security_id, issuer_id) overrides the universe lookup (app.analytics.scoring_full chunks)."""
        self.s = settings
        sp = spec(settings)
        self.cat = metric_catalog(settings)
        uni = universe_frame(universe, None, settings, tickers) if uni is None else uni.select("security_id", "issuer_id")
        sm = scan_table("securities_master", settings).select("security_id", "issuer_id", "is_primary_security").collect()
        u = uni.join(sm.select("security_id", "is_primary_security"), on="security_id", how="left")
        # one scoring security per issuer (primary preferred); others get copies of the issuer's rows
        # deterministic tie-break (2026-10-30): issuers with two equally-primary listings used to get a random
        # scoring security per run (unique() without maintain_order); now the highest security_id wins
        self.score_sec = (u.sort(["is_primary_security", "security_id"], nulls_last=False)
                          .unique(subset=["issuer_id"], keep="last", maintain_order=True)
                          .select("security_id", "issuer_id"))
        self.extra_sec = u.join(self.score_sec.select(pl.col("security_id").alias("x"), "issuer_id"), on="issuer_id") \
            .filter(pl.col("security_id") != pl.col("x")).select("security_id", "issuer_id")
        self.issuers = self.score_sec["issuer_id"].unique().to_list()
        meta = R.issuer_meta(self.issuers, settings)
        prm = sp["peer_groups"]["peer_region_map"]
        self.meta = meta.with_columns(pl.col("region").replace_strict(prm, default="GLOBAL", return_dtype=pl.Utf8)
                                      .alias("peer_region")).select(
            "issuer_id", "sector_id", "subsector_id", "region", "peer_region", "kpi_set_id")
        self.members = factor_member_table(settings)
        self.scored = scored_metric_table(settings)
        self.mfac = metric_factor_map(settings)
        self.mfac_set = metric_factor_frame(settings)
        fund_ids = [m for m, c in self.cat.items() if c["kind"] == "fundamental"]
        needed = set(self.scored["metric_id"].to_list()) | {"revenue_cagr_3y"}
        fund_ids = [m for m in fund_ids if m in needed]
        self.fund = self._load_fund(fund_ids)
        lo = (start or date(2000, 1, 1)) - timedelta(days=int(sp["self_history"]["window_years"]) * 366 + 30)
        hi = end or date(2100, 1, 1)
        secs = self.score_sec["security_id"].to_list()
        self.val = (scan_table("valuation_daily", settings)
                    .filter(pl.col("security_id").is_in(secs), pl.col("date") >= lo, pl.col("date") <= hi)
                    .select("security_id", "date", "market_cap_usd", "size_bucket",
                            *[m for m in (sp.get("valuation_metrics") or {})])
                    .collect().sort("security_id", "date"))
        # quarter-end valuation samples (self history)
        if not self.val.is_empty():
            q = self.val.with_columns(pl.col("date").dt.truncate("1q").alias("__q"))
            self.val_q = q.sort("security_id", "date").group_by("security_id", "__q").agg(pl.all().last()).drop("__q")
        else:
            self.val_q = self.val
        self.mkt = self._market(secs, start, end)
        self.insider = self._safe(lambda: scan_table("insider_signals", settings)
                                  .filter(pl.col("security_id").is_in(secs))
                                  .select("security_id", pl.col("as_of_date").alias("date"), "signal_score").collect())
        self.smart_opt = smart_money_option(settings)
        self.smart = self._safe(lambda: scan_table("smart_money_signals", settings)
                                .filter(pl.col("security_id").is_in(secs))
                                .select("security_id", pl.col("as_of_date").alias("date"), "score_pct").collect()) \
            if self.smart_opt else pl.DataFrame()
        self.sent = self._safe(lambda: scan_table("sentiment_daily", settings)
                               .filter(pl.col("security_id").is_in(secs))
                               .select("security_id", "date", "sentiment_ewm_30d", "gdelt_tone_ewm_7d",
                                       "lazy_prices_pct").collect())
        self.moat = self._safe(lambda: scan_table("moat", settings).filter(pl.col("issuer_id").is_in(self.issuers))
                               .select("issuer_id", pl.col("as_of_date").alias("date"), "moat_score").collect())
        self.fv = self._safe(lambda: self._load_fv(settings))
        self.betas = self._safe(lambda: scan_table("macro_betas", settings).filter(pl.col("security_id").is_in(secs))
                                .select("security_id", pl.col("as_of_date").alias("date"), "factor_id", "beta").collect())
        self.regimes = self._safe(lambda: scan_table("macro_regimes", settings)
                                  .select("model_id", "date", "region_scope", "probabilities", "state_labels").collect())

    def _load_fv(self, settings: Settings) -> pl.DataFrame:
        lf = scan_table("forecast_valuation", settings)
        conf = pl.col("valuation_confidence") if "valuation_confidence" in lf.collect_schema().names()             else pl.lit(None, pl.Float64).alias("valuation_confidence")
        return (lf.filter(pl.col("issuer_id").is_in(self.issuers), pl.col("scenario") == "market_implied")
                .select("issuer_id", pl.col("as_of_date").alias("date"), "implied_vs_hist_growth_pct", conf).collect())

    @staticmethod
    def _safe(fn):
        try:
            return fn()
        except Exception as e:  # upstream table absent / schema drift -> treated as missing
            log.warning("optional input unavailable: %s", e)
            return pl.DataFrame()

    def _load_fund(self, ids: Sequence[str]) -> pl.DataFrame:
        cols = ["issuer_id", "metric_id", "period_kind", "period_end", "knowledge_date", "value", "inputs_complete",
                "dq_score"]
        parts = []
        r = scan_table("ratios", self.s)
        if "dq_score" not in r.collect_schema().names():
            r = r.with_columns(pl.lit(None, pl.Float32).alias("dq_score"))
        parts.append(r.filter(pl.col("issuer_id").is_in(self.issuers), pl.col("metric_id").is_in(list(ids)),
                              pl.col("period_kind").is_in(["TTM", "FY"])).select(cols).collect())
        k = scan_table("sector_kpis", self.s)
        if "dq_score" not in k.collect_schema().names():
            k = k.with_columns(pl.lit(None, pl.Float32).alias("dq_score"))
        parts.append(k.filter(pl.col("issuer_id").is_in(self.issuers), pl.col("kpi_id").is_in(list(ids)),
                              pl.col("period_kind").is_in(["TTM", "FY"]))
                     .rename({"kpi_id": "metric_id"}).select(cols).collect())
        df = pl.concat([p for p in parts if not p.is_empty()], how="vertical_relaxed") if any(
            not p.is_empty() for p in parts) else pl.DataFrame(schema={c: pl.Utf8 for c in cols})
        if df.is_empty():
            return df
        return df.with_columns(pl.col("dq_score").cast(pl.Float64)).sort(
            "issuer_id", "metric_id", "period_kind", "period_end", "knowledge_date")

    def _market(self, secs: Sequence[str], start: date | None, end: date | None) -> pl.DataFrame:
        lo = (start or date(2000, 1, 1)) - timedelta(days=420)
        lf = scan_table("prices_daily", self.s).filter(pl.col("security_id").is_in(list(secs)), pl.col("date") >= lo)
        if end:
            lf = lf.filter(pl.col("date") <= end)
        px = (lf.select("security_id", "date", "adj_close", "high", "adj_factor").collect()
              .unique(subset=["security_id", "date"], keep="last").sort("security_id", "date"))
        if px.is_empty():
            return px
        g = "security_id"
        c = pl.col("adj_close")
        ah = (pl.col("high") * pl.col("adj_factor")).fill_null(c)
        lr = (c / c.shift(1).over(g)).log()
        px = px.with_columns(pl.int_range(pl.len()).over(g).alias("__n"), lr.alias("__lr"), ah.alias("__ah"))
        px = px.with_columns(
            (c.shift(21).over(g) / c.shift(252).over(g) - 1).alias("mom_12_1"),
            (c.shift(21).over(g) / c.shift(126).over(g) - 1).alias("mom_6_1"),
            (c / c.shift(21).over(g) - 1).alias("mom_1m"),
            (c / pl.col("__ah").rolling_max(252, min_samples=126).over(g)).alias("high_52w_proximity"),
            (c / c.rolling_mean(200, min_samples=126).over(g) - 1).alias("trend_200d"),
            (pl.col("__lr").rolling_std(252, min_samples=126).over(g) * math.sqrt(252)).alias("vol_1y"),
        )
        ok = pl.col("__n") >= 125
        mm = ["mom_12_1", "mom_6_1", "mom_1m", "high_52w_proximity", "trend_200d", "vol_1y"]
        px = px.with_columns([pl.when(ok & pl.col(m).is_finite()).then(pl.col(m)).otherwise(None).alias(m) for m in mm])
        if start:
            px = px.filter(pl.col("date") >= start - timedelta(days=15))
        return px.select("security_id", "date", *mm)


def _asof_rows(df: pl.DataFrame, D: date, key: str, max_age: int, date_col: str = "date") -> pl.DataFrame:
    if df.is_empty():
        return df
    return (df.filter(pl.col(date_col) <= D, pl.col(date_col) > D - timedelta(days=max_age))
            .sort(key, date_col).group_by(key).agg(pl.all().last()))


# ------------------------------------------------------------------------------------------------ per-date values


def fundamental_values(inp: Inputs, D: date) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(current, history): current = issuer_id, metric_id, raw_value, inputs_complete, dq_score, period_end;
    history = issuer_id, metric_id, value (obs before the current period within the self-history window)."""
    sp = spec(inp.s)
    f = inp.fund
    empty = pl.DataFrame(schema={"issuer_id": pl.Utf8, "metric_id": pl.Utf8, "raw_value": pl.Float64,
                                 "inputs_complete": pl.Boolean, "dq_score": pl.Float64, "period_end": pl.Date,
                                 "period_kind": pl.Utf8})
    if f.is_empty():
        return empty, pl.DataFrame(schema={"issuer_id": pl.Utf8, "metric_id": pl.Utf8, "value": pl.Float64})
    stale = int(sp["as_of"]["staleness_days"])
    win = int(sp["self_history"]["window_years"])
    k = ["issuer_id", "metric_id", "period_kind", "period_end"]
    known = (f.filter(pl.col("knowledge_date") <= D, pl.col("period_end") > D - timedelta(days=366 * (win + 1)))
             .group_by(k).agg(pl.all().last()))
    # latest non-null value per kind within the staleness window (else the latest null row, which may carry an
    # 'undefined' imputation), then TTM preferred over FY among equally-informative rows
    fresh = known.filter(pl.col("period_end") >= D - timedelta(days=stale))         .with_columns(pl.col("value").is_not_null().alias("__h"))
    latest = fresh.sort("__h", "period_end").group_by("issuer_id", "metric_id", "period_kind").agg(pl.all().last())
    pick = (latest.with_columns((pl.col("period_kind") == "TTM").alias("__t"))
            .sort("__h", "__t").group_by("issuer_id", "metric_id").agg(pl.all().last()).drop("__t", "__h"))
    cur = pick.select("issuer_id", "metric_id", pl.col("value").alias("raw_value"), "inputs_complete", "dq_score",
                      "period_end", "period_kind")
    hist = (known.join(pick.select("issuer_id", "metric_id", "period_kind", pl.col("period_end").alias("__cpe")),
                       on=["issuer_id", "metric_id", "period_kind"], how="inner")
            .filter(pl.col("period_end") < pl.col("__cpe"),
                    pl.col("period_end") >= pl.col("__cpe").dt.offset_by(f"-{win}y"), pl.col("value").is_not_null())
            .select("issuer_id", "metric_id", "value", "period_end", "__cpe"))
    return cur, hist


def valuation_values(inp: Inputs, D: date) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """(current long, history long, size frame security_id/market_cap_usd/size_bucket)."""
    sp = spec(inp.s)
    vm = list((sp.get("valuation_metrics") or {}).keys())
    if inp.val.is_empty():
        e = pl.DataFrame(schema={"security_id": pl.Utf8, "metric_id": pl.Utf8, "raw_value": pl.Float64})
        return e, pl.DataFrame(schema={"security_id": pl.Utf8, "metric_id": pl.Utf8, "value": pl.Float64}), \
            pl.DataFrame(schema={"security_id": pl.Utf8, "market_cap_usd": pl.Float64, "size_bucket": pl.Utf8})
    cur = _asof_rows(inp.val, D, "security_id", 10)
    size = cur.select("security_id", "market_cap_usd", "size_bucket")
    cl = cur.filter(pl.col("market_cap_usd").is_not_null()).unpivot(
        index="security_id", on=vm, variable_name="metric_id", value_name="raw_value")
    win = int(sp["self_history"]["window_years"])
    h = inp.val_q.filter(pl.col("date") < D - timedelta(days=45), pl.col("date") >= D - timedelta(days=366 * win))
    hl = h.unpivot(index=["security_id", "date"], on=vm, variable_name="metric_id", value_name="value") \
        .filter(pl.col("value").is_not_null())
    return cl, hl, size


def other_values(inp: Inputs, D: date) -> pl.DataFrame:
    """security_id, metric_id, raw_value for market / insider / sentiment / macro / moat / forecast metrics."""
    parts = []
    if not inp.mkt.is_empty():
        m = _asof_rows(inp.mkt, D, "security_id", 7)
        mm = [c for c in m.columns if c not in ("security_id", "date")]
        parts.append(m.unpivot(index="security_id", on=mm, variable_name="metric_id", value_name="raw_value"))
    if not inp.insider.is_empty():
        x = _asof_rows(inp.insider, D, "security_id", 10)
        parts.append(x.select("security_id", pl.lit("insider_signal").alias("metric_id"),
                              pl.col("signal_score").alias("raw_value")))
    if getattr(inp, "smart_opt", None) and not inp.smart.is_empty():  # optional, default OFF (scoring.yaml)
        x = _asof_rows(inp.smart, D, "security_id", inp.smart_opt["max_age_days"])
        parts.append(x.select("security_id", pl.lit(inp.smart_opt["metric"]).alias("metric_id"),
                              pl.col("score_pct").alias("raw_value")))
    if not inp.sent.is_empty():
        x = _asof_rows(inp.sent, D, "security_id", 10)
        for c in ("sentiment_ewm_30d", "gdelt_tone_ewm_7d", "lazy_prices_pct"):
            parts.append(x.select("security_id", pl.lit(c).alias("metric_id"), pl.col(c).alias("raw_value")))
    sec = inp.score_sec
    if not inp.moat.is_empty():
        x = _asof_rows(inp.moat, D, "issuer_id", 400).join(sec, on="issuer_id")
        parts.append(x.select("security_id", pl.lit("moat_score").alias("metric_id"), pl.col("moat_score").alias("raw_value")))
    if not inp.fv.is_empty():
        x = _asof_rows(inp.fv, D, "issuer_id", 60).join(sec, on="issuer_id")
        # DCF-derived inputs are confidence-weighted (scoring.yaml forecast_confidence): ignored below the floor,
        # otherwise their factor weight is scaled by confidence/100 (multiples carry the rest after renormalization)
        conf = pl.col("valuation_confidence").cast(pl.Float64)
        ok = conf.is_null() | (conf >= CONF_FLOOR)
        parts.append(x.select("security_id", pl.lit("rdcf_implied_vs_hist").alias("metric_id"),
                              pl.when(ok).then(pl.col("implied_vs_hist_growth_pct")).otherwise(None).alias("raw_value"),
                              pl.when(ok).then((conf / 100.0).clip(0.0, 1.0).fill_null(1.0)).otherwise(None)
                              .alias("wmult")))
    mac = macro_values(inp, D)
    if not mac.is_empty():
        parts.append(mac)
    parts = [p.with_columns(pl.col("raw_value").cast(pl.Float64)) for p in parts if not p.is_empty()]
    if not parts:
        return pl.DataFrame(schema={"security_id": pl.Utf8, "metric_id": pl.Utf8, "raw_value": pl.Float64,
                                    "wmult": pl.Float64})
    out = pl.concat(parts, how="diagonal_relaxed")
    if "wmult" not in out.columns:
        out = out.with_columns(pl.lit(None, pl.Float64).alias("wmult"))
    return out


def macro_values(inp: Inputs, D: date) -> pl.DataFrame:
    if inp.betas.is_empty():
        return pl.DataFrame()
    b = inp.betas.filter(pl.col("date") <= D, pl.col("date") > D - timedelta(days=400))
    if b.is_empty():
        return pl.DataFrame()
    b = b.sort("date").group_by("security_id", "factor_id").agg(pl.col("beta").last())
    out = [b.filter(pl.col("factor_id") == "rates_10y").select(
        "security_id", pl.lit("macro_rate_beta_abs").alias("metric_id"), pl.col("beta").abs().alias("raw_value"))]
    if not inp.regimes.is_empty():
        rg = inp.regimes.filter(pl.col("date") <= D).sort("date").group_by("region_scope").agg(pl.all().last())
        probs: dict[str, dict[str, float]] = {}
        for r in rg.iter_rows(named=True):
            probs[r["region_scope"]] = {lab: float(p) for lab, p in zip(r["state_labels"], r["probabilities"], strict=False)}
        if probs:
            reg_sec = inp.score_sec.join(inp.meta.select("issuer_id", "region"), on="issuer_id")
            scope = reg_sec.select("security_id", pl.when(pl.col("region").is_in(["US", "CA"])).then(pl.lit("US"))
                                   .otherwise(pl.lit("GLOBAL")).alias("scope"))
            pr = pl.DataFrame([{"scope": sc, "factor_id": f"regime_{lab}", "p": p}
                               for sc, d in probs.items() for lab, p in d.items()])
            if "GLOBAL" not in probs and "US" in probs:
                pr = pl.concat([pr, pr.filter(pl.col("scope") == "US").with_columns(pl.lit("GLOBAL").alias("scope"))])
            if "US" not in probs and "GLOBAL" in probs:
                pr = pl.concat([pr, pr.filter(pl.col("scope") == "GLOBAL").with_columns(pl.lit("US").alias("scope"))])
            fit = (b.filter(pl.col("factor_id").str.starts_with("regime_")).join(scope, on="security_id")
                   .join(pr, on=["scope", "factor_id"]).filter(pl.col("beta").is_not_null())
                   .group_by("security_id").agg(((pl.col("p") * pl.col("beta")).sum() / pl.col("p").sum()).alias("raw_value"),
                                                pl.col("p").sum().alias("__ps"))
                   .filter(pl.col("__ps") > 0.5).select("security_id", pl.lit("macro_regime_fit").alias("metric_id"),
                                                       "raw_value"))
            out.append(fit)
    return pl.concat(out, how="vertical_relaxed")


# ------------------------------------------------------------------------------------------------ scoring core


def _cat_frame(cat: dict[str, dict[str, Any]]) -> pl.DataFrame:
    return pl.DataFrame([{"metric_id": m, "kind": c["kind"],
                          "hib": 1 if c["hib"] is True else (-1 if c["hib"] is False else 0),
                          "target": float(c["target"]) if c.get("target") is not None else None,
                          "positive_only": bool(c["positive_only"]), "scaled": bool(c["scaled"])}
                         for m, c in cat.items()],
                        schema={"metric_id": pl.Utf8, "kind": pl.Utf8, "hib": pl.Int8, "target": pl.Float64,
                                "positive_only": pl.Boolean, "scaled": pl.Boolean})


def peer_percentiles(df: pl.DataFrame, levels: Sequence[tuple[str, list[str]]], min_peers: int,
                     value_col: str = "v", raw_col: str = "raw_value") -> pl.DataFrame:
    """Adds peer_percentile, peer_count, peer_median, peer_group_level, peer_group_key using the first level with
    >= min_peers valid values (falls back to the broadest level when none qualifies and it has >= 3)."""
    lv_cols = []
    for i, (lid, keys) in enumerate(levels):
        by = ["metric_id", *keys]
        pct, n = pct_rank(pl.col(value_col), by)
        med = pl.col(raw_col).filter(pl.col(value_col).is_not_null()).median().over(by)
        ok_keys = pl.all_horizontal([pl.col(k).is_not_null() for k in keys]) if keys else pl.lit(True)
        key = pl.concat_str([pl.lit(f"{lid}:")] + ([pl.concat_str([pl.col(k).cast(pl.Utf8) for k in keys], separator="|")]
                                                   if keys else [pl.lit("all")]))
        df = df.with_columns(pl.when(ok_keys).then(pct).otherwise(None).alias(f"__p{i}"),
                             pl.when(ok_keys).then(n).otherwise(0).alias(f"__n{i}"),
                             med.alias(f"__m{i}"), key.alias(f"__k{i}"))
        lv_cols.append(i)
    choice = pl.lit(None, pl.Int32)
    last = len(levels) - 1
    for i in reversed(lv_cols):
        choice = pl.when(pl.col(f"__n{i}") >= min_peers).then(pl.lit(i)).otherwise(choice)
    choice = pl.when(choice.is_null() & (pl.col(f"__n{last}") >= 3)).then(pl.lit(last)).otherwise(choice)
    df = df.with_columns(choice.alias("__lv"))

    def pick(prefix: str) -> pl.Expr:
        e = pl.lit(None)
        for i in reversed(lv_cols):
            e = pl.when(pl.col("__lv") == i).then(pl.col(f"{prefix}{i}")).otherwise(e)
        return e

    names = [lid for lid, _ in levels]
    df = df.with_columns(
        pl.when(pl.col(value_col).is_not_null()).then(pick("__p")).otherwise(None).cast(pl.Float64).alias("peer_percentile"),
        pick("__n").cast(pl.Int32).alias("peer_count"),
        pick("__m").cast(pl.Float64).alias("peer_median"),
        pl.col("__lv").replace_strict(dict(enumerate(names)), default=None, return_dtype=pl.Utf8).alias("peer_group_level"),
        pick("__k").cast(pl.Utf8).alias("peer_group_key"),
    )
    return df.drop([c for c in df.columns if c.startswith("__p") or c.startswith("__n") or c.startswith("__m")
                    or c.startswith("__k")] + ["__lv"])


def self_scores(cur: pl.DataFrame, hist: pl.DataFrame, keys: Sequence[str], catf: pl.DataFrame,
                min_obs: int, min_years: float, clip: float) -> pl.DataFrame:
    """Adds self_z, self_hist_n, self_score: z of current direction-adjusted value vs own history."""
    if hist.is_empty():
        return cur.with_columns(pl.lit(None, pl.Float64).alias("self_z"), pl.lit(None, pl.Int32).alias("self_hist_n"),
                                pl.lit(None, pl.Float64).alias("self_score"))
    date_col = "period_end" if "period_end" in hist.columns else "date"
    h = hist.join(catf.select("metric_id", "hib", "target", "positive_only"), on="metric_id", how="inner")
    h = h.filter(~pl.col("positive_only") | (pl.col("value") > 0))
    h = h.with_columns(direction_expr(pl.col("value"), pl.col("hib"), pl.col("target")).alias("y"))
    agg = h.group_by(*keys, "metric_id").agg(
        pl.col("y").mean().alias("__mu"), pl.col("y").std().alias("__sd"), pl.col("y").count().alias("self_hist_n"),
        ((pl.col(date_col).max() - pl.col(date_col).min()).dt.total_days() / 365.25).alias("__span"))
    out = cur.join(agg, on=[*keys, "metric_id"], how="left")
    sd = pl.max_horizontal(pl.col("__sd").fill_null(0), 0.05 * pl.col("__mu").abs() + 1e-9)
    z = ((pl.col("v") - pl.col("__mu")) / sd).clip(-clip, clip)
    okh = (pl.col("self_hist_n") >= min_obs) & (pl.col("__span") >= min_years - 0.3) & pl.col("v").is_not_null()
    out = out.with_columns(pl.when(okh).then(z).otherwise(None).alias("self_z"))
    out = out.with_columns((norm_cdf(pl.col("self_z")) * 100).alias("self_score"),
                           pl.col("self_hist_n").cast(pl.Int32))
    return out.drop("__mu", "__sd", "__span")


def mcap_adjusted(df: pl.DataFrame, min_obs: int) -> pl.DataFrame:
    """df rows: valuation metrics with columns v (direction-adjusted), raw_value, market_cap_usd, quality_score,
    revenue_cagr_3y, level keys. Adds mcap_adj_score (0..100)."""
    if df.is_empty():
        return df.with_columns(pl.lit(None, pl.Float64).alias("mcap_adj_score"))
    log_metrics = set(spec()["transforms"].get("log_before_regression") or [])
    df = df.with_columns(
        pl.when(pl.col("metric_id").is_in(list(log_metrics)))
        .then(pl.when(pl.col("raw_value") > 0).then(pl.col("raw_value").log()).otherwise(None))
        .otherwise(pl.col("raw_value")).alias("__y"),
        pl.col("market_cap_usd").log().alias("__lm"),
    )
    valid = pl.col("__y").is_not_null() & pl.col("__lm").is_not_null() & pl.col("__y").is_finite()
    df = df.with_columns(pl.when(valid).then(pl.col("__y")).otherwise(None).alias("__y"))
    # choose regression group: first level with >= min_obs valid (as in peer_percentiles), else global
    levels = LEVELS + [("global", [])]
    for i, (_, keys) in enumerate(levels):
        by = ["metric_id", *keys]
        df = df.with_columns(pl.col("__y").count().over(by).alias(f"__rn{i}"),
                             (pl.concat_str([pl.lit(str(i))] + [pl.col(k).cast(pl.Utf8).fill_null("?") for k in keys],
                                            separator="|") if keys else pl.lit(str(i))).alias(f"__rk{i}"))
    g = pl.lit(None, pl.Utf8)
    for i in reversed(range(len(levels))):
        keys = levels[i][1]
        ok = pl.col(f"__rn{i}") >= (min_obs if i < len(levels) - 1 else 8)
        if keys:
            ok = ok & pl.all_horizontal([pl.col(k).is_not_null() for k in keys])
        g = pl.when(ok).then(pl.col(f"__rk{i}")).otherwise(g)
    df = df.with_columns(g.alias("__grp")).drop([c for c in df.columns if c.startswith("__rn") or c.startswith("__rk")])
    out = []
    for (mid, grp), part in df.group_by("metric_id", "__grp"):
        resid = np.full(part.height, np.nan)
        if grp is not None:
            sub = part.with_row_index("__i").filter(pl.col("__y").is_not_null())
            if sub.height >= 8:
                y = sub["__y"].to_numpy().astype(float)
                lo, hi = np.nanquantile(y, [0.01, 0.99])
                y = np.clip(y, lo, hi)
                cols = [np.ones(sub.height), sub["__lm"].to_numpy().astype(float)]
                q = sub["quality_score"].fill_null(50.0).to_numpy().astype(float)
                cols.append(q)
                gr = sub["revenue_cagr_3y"].to_numpy().astype(float)
                med = np.nanmedian(gr) if np.isfinite(gr).any() else 0.0
                gr = np.where(np.isfinite(gr), gr, med)
                glo, ghi = np.quantile(gr, [0.01, 0.99])
                cols.append(np.clip(gr, glo, ghi))
                X = np.column_stack(cols)
                beta, *_ = np.linalg.lstsq(X, y, rcond=None)
                r = y - X @ beta
                resid[sub["__i"].to_numpy()] = r
        out.append(part.with_columns(pl.Series("__res", resid, dtype=pl.Float64).fill_nan(None)))
    df = pl.concat(out, how="vertical_relaxed")
    # cheaper than predicted = better: multiples (hib false) -> -resid; yields -> +resid
    adj = pl.when(pl.col("hib") == -1).then(-pl.col("__res")).otherwise(pl.col("__res"))
    pct, _ = pct_rank(adj, ["metric_id", "__grp"])
    df = df.with_columns(pl.when(pl.col("__res").is_not_null()).then(pct).otherwise(None).alias("mcap_adj_score"))
    return df.drop("__y", "__lm", "__grp", "__res")


def factor_raw_frame(ms: pl.DataFrame, inp: Inputs) -> pl.DataFrame:
    """Per-security factor aggregation (no cross-section): every scoring security x factor with score_raw (null when
    coverage < 0.30), coverage, sector_id, peer_region. Shared by factor_scores_frame and app.analytics.scoring_full."""
    mem = inp.members
    base = inp.score_sec.join(inp.meta, on="issuer_id", how="inner")
    m = base.select("security_id", "issuer_id", "kpi_set_id").join(mem, on="kpi_set_id", how="inner")
    wm = pl.col("wmult").fill_null(1.0) if "wmult" in ms.columns else pl.lit(1.0)
    m = m.join(ms.select("security_id", "metric_id", "metric_score", "imputed", wm.alias("__wm")),
               on=["security_id", "metric_id"],
               how="left")
    m = m.with_columns((pl.col("weight") * pl.col("__wm").fill_null(1.0)).alias("__we"))
    has = pl.col("metric_score").is_not_null()
    blk = (m.group_by("security_id", "issuer_id", "factor_id", "block").agg(
        pl.col("block_weight").first(),
        (pl.col("__we") * pl.col("metric_score")).filter(has).sum().alias("__ws"),
        pl.col("__we").filter(has).sum().alias("__wp"),
        pl.col("__we").filter(has & ~pl.col("imputed").fill_null(False)).sum().alias("__wc"),
        pl.col("weight").sum().alias("__wt"))
        .with_columns(pl.when(pl.col("__wp") > 0).then(pl.col("__ws") / pl.col("__wp")).otherwise(None).alias("__raw"),
                      (pl.col("__wc") / pl.col("__wt")).alias("__cov")))
    f = blk.group_by("security_id", "issuer_id", "factor_id").agg(
        ((pl.col("block_weight") * pl.col("__raw")).filter(pl.col("__raw").is_not_null()).sum()
         / pl.col("block_weight").filter(pl.col("__raw").is_not_null()).sum()).fill_nan(None).alias("score_raw"),
        ((pl.col("block_weight") * pl.col("__cov")).sum() / pl.col("block_weight").sum()).alias("coverage"))
    # every scoring security x every factor
    allf = base.select("security_id", "issuer_id", "sector_id", "peer_region").join(
        pl.DataFrame({"factor_id": FACTORS}), how="cross")
    f = allf.join(f, on=["security_id", "issuer_id", "factor_id"], how="left").with_columns(
        pl.col("coverage").fill_null(0.0))
    low = pl.col("coverage") < 0.30
    return f.with_columns(pl.when(low).then(None).otherwise(pl.col("score_raw")).alias("score_raw"))


PASSTHRU_FACTORS = ["insider", "moat"]


def factor_scores_frame(ms: pl.DataFrame, inp: Inputs, D: date) -> pl.DataFrame:
    """ms: metric scores (security_id, issuer_id, metric_id, metric_score, imputed). -> factor rows."""
    sp = spec(inp.s)
    f = factor_raw_frame(ms, inp)
    neutral = [x["id"] for x in sp["factors"] if x.get("missing_policy") == "neutral_50"]
    passthru = PASSTHRU_FACTORS
    # re-percentile within peer_region x sector (min 8) -> peer_region -> global
    v = pl.when(pl.col("factor_id").is_in(passthru)).then(None).otherwise(pl.col("score_raw"))
    f = f.with_columns(v.alias("__v"))
    p1, n1 = pct_rank(pl.col("__v"), ["factor_id", "peer_region", "sector_id"])
    p2, n2 = pct_rank(pl.col("__v"), ["factor_id", "peer_region"])
    p3, _ = pct_rank(pl.col("__v"), ["factor_id"])
    rep = pl.when(n1 >= 8).then(p1).when(n2 >= 8).then(p2).otherwise(p3)
    f = f.with_columns(
        pl.when(pl.col("factor_id").is_in(passthru)).then(pl.col("score_raw"))
        .when(pl.col("__v").is_not_null()).then(rep).otherwise(None).alias("score"))
    f = f.with_columns(
        pl.when(pl.col("score").is_null() & pl.col("factor_id").is_in(neutral)).then(50.0)
        .otherwise(pl.col("score")).alias("score"),
        pl.when(pl.col("score").is_null() & pl.col("factor_id").is_in(neutral)).then(0.0)
        .otherwise(pl.col("coverage")).alias("coverage"))
    f = f.with_columns(
        pl.col("score").rank("ordinal", descending=True).over("factor_id", "sector_id", "peer_region")
        .cast(pl.Int32).alias("rank_in_sector"),
        pl.col("score").count().over("factor_id", "sector_id", "peer_region").cast(pl.Int32).alias("sector_count"))
    f = f.with_columns(pl.when(pl.col("score").is_null()).then(None).otherwise(pl.col("rank_in_sector"))
                       .alias("rank_in_sector"))
    return f.with_columns(pl.lit(D).alias("as_of_date")).drop("__v")


def composite_frame(fs: pl.DataFrame, inp: Inputs, D: date, dq: pl.DataFrame) -> pl.DataFrame:
    sp = spec(inp.s)
    neutral = [x["id"] for x in sp["factors"] if x.get("missing_policy") == "neutral_50"]
    usable = pl.col("score").is_not_null() & ((pl.col("coverage") >= 0.30) | pl.col("factor_id").is_in(neutral))
    valid = fs.group_by("security_id").agg(
        pl.col("score").is_not_null().sum().alias("__nf"),
        (pl.col("score").is_not_null() & pl.col("factor_id").is_in(CORE)).sum().alias("__nc"))
    out = []
    for pid, w in sp["composite"]["profiles"].items():
        if any(k.startswith("_") for k in w):
            continue
        wdf = pl.DataFrame({"factor_id": list(w.keys()), "w": [float(x) for x in w.values()]})
        j = fs.join(wdf, on="factor_id", how="inner")
        c = j.group_by("security_id", "issuer_id", "sector_id", "peer_region").agg(
            ((pl.col("w") * pl.col("score")).filter(usable).sum() / pl.col("w").filter(usable).sum())
            .fill_nan(None).alias("score"),
            ((pl.col("w") * pl.col("coverage")).sum() / pl.col("w").sum()).alias("coverage"))
        c = c.join(valid, on="security_id", how="left").with_columns(
            pl.when((pl.col("__nf") >= 5) & (pl.col("__nc") >= 2)).then(pl.col("score")).otherwise(None).alias("score"))
        out.append(c.with_columns(pl.lit(pid).alias("profile_id")).drop("__nf", "__nc"))
    c = pl.concat(out, how="vertical_relaxed")
    c = add_ranks(c)
    c = c.join(dq, on="issuer_id", how="left").with_columns(pl.col("dq_score").fill_null(0.0))
    return c.with_columns(pl.lit(D).alias("as_of_date"))


def add_ranks(c: pl.DataFrame) -> pl.DataFrame:
    """rank_global / rank_sector (sector x peer_region) / quintile grade per profile (A = top 20%)."""
    rg = pl.col("score").rank("ordinal", descending=True).over("profile_id")
    n = pl.col("score").count().over("profile_id")
    rs = pl.col("score").rank("ordinal", descending=True).over("profile_id", "sector_id", "peer_region")
    c = c.with_columns(rg.cast(pl.Int32).alias("rank_global"), rs.cast(pl.Int32).alias("rank_sector"), n.alias("__n"))
    q = ((pl.col("rank_global") - 1) * 5 / pl.col("__n")).floor().cast(pl.Int32)
    grade = q.replace_strict({0: "A", 1: "B", 2: "C", 3: "D", 4: "E"}, default="E", return_dtype=pl.Utf8)
    return c.with_columns(pl.when(pl.col("score").is_null()).then(None).otherwise(grade).alias("grade"),
                          pl.when(pl.col("score").is_null()).then(None).otherwise(pl.col("rank_global")).alias("rank_global"),
                          pl.when(pl.col("score").is_null()).then(None).otherwise(pl.col("rank_sector")).alias("rank_sector")
                          ).drop("__n")


def assemble_metrics(inp: Inputs, D: date) -> dict[str, Any]:
    """Stage 1 of score_date (per security, no cross-section): every applicable metric row with its raw value,
    imputation flag and direction-adjusted value `v`, plus the self-history inputs. Shared by score_date and
    app.analytics.scoring_full."""
    catf = _cat_frame(inp.cat)
    base = inp.score_sec.join(inp.meta, on="issuer_id", how="inner")
    # ---- current values
    fcur, fhist = fundamental_values(inp, D)
    vcur, vhist, size = valuation_values(inp, D)
    ocur = other_values(inp, D)
    applicable = base.select("security_id", "issuer_id", "kpi_set_id").join(inp.scored, on="kpi_set_id", how="inner")
    ap = applicable.join(catf, on="metric_id", how="inner")
    fund = ap.filter(pl.col("kind") == "fundamental").join(
        fcur.select("issuer_id", "metric_id", "raw_value", "inputs_complete", "dq_score"),
        on=["issuer_id", "metric_id"], how="left")
    val = ap.filter(pl.col("kind") == "valuation").join(vcur, on=["security_id", "metric_id"], how="left") \
        .with_columns(pl.lit(None, pl.Boolean).alias("inputs_complete"), pl.lit(None, pl.Float64).alias("dq_score"))
    oth = ap.filter(pl.col("kind").is_in(["market", "other"])).join(ocur, on=["security_id", "metric_id"], how="left") \
        .with_columns(pl.lit(None, pl.Boolean).alias("inputs_complete"), pl.lit(None, pl.Float64).alias("dq_score"))
    fund = fund.with_columns(pl.lit(None, pl.Float64).alias("wmult"))
    val = val.with_columns(pl.lit(None, pl.Float64).alias("wmult"))
    cols = ["security_id", "issuer_id", "kpi_set_id", "metric_id", "kind", "hib", "target", "positive_only", "scaled",
            "raw_value", "inputs_complete", "dq_score", "wmult"]
    df = pl.concat([x.select(cols) for x in (fund, val, oth)], how="vertical_relaxed")
    df = df.join(base.select("security_id", "sector_id", "subsector_id", "peer_region"), on="security_id", how="left")
    df = df.join(size, on="security_id", how="left")
    has_val = pl.col("market_cap_usd").is_not_null()
    # ---- imputations (worst score) and direction-adjusted value
    imp_pos = pl.col("positive_only") & has_val & (pl.col("raw_value").is_null() | (pl.col("raw_value") <= 0))
    imp_und = pl.col("metric_id").is_in(list(IMPUTE_WORST_IF_UNDEFINED)) & pl.col("raw_value").is_null() & \
        pl.col("inputs_complete").fill_null(False)
    df = df.with_columns(pl.when(pl.col("raw_value").is_finite()).then(pl.col("raw_value")).otherwise(None)
                         .alias("raw_value"))
    df = df.with_columns((imp_pos | imp_und).alias("imputed"))
    v = direction_expr(pl.col("raw_value"), pl.col("hib"), pl.col("target"))
    df = df.with_columns(pl.when(pl.col("imputed") | pl.col("scaled")).then(None).otherwise(v).alias("v"))
    return {"df": df, "fcur": fcur, "fhist": fhist, "vhist": vhist, "base": base, "catf": catf, "fund": fund}


def score_date(inp: Inputs, D: date, capture: dict[str, Any] | None = None) -> dict[str, pl.DataFrame]:
    """All scores for one as-of date: {'peer': ..., 'factor': ..., 'composite': ...}. `capture` (optional dict)
    receives the intermediate frames that app.analytics.scoring_full anchors the full universe to (outputs are the
    same with or without it)."""
    sp = spec(inp.s)
    pg = sp["peer_groups"]
    min_peers = int(pg["min_peers"])
    sh = sp["self_history"]
    zclip = float(sp["transforms"]["z"]["clip"])
    a = assemble_metrics(inp, D)
    df, fcur, fhist, vhist, base, catf, fund = (a["df"], a["fcur"], a["fhist"], a["vhist"], a["base"], a["catf"],
                                                a["fund"])
    # ---- peer percentiles
    fv = df.filter(pl.col("kind").is_in(["fundamental", "valuation"]))
    mk = df.filter(pl.col("kind").is_in(["market", "other"]))
    if capture is not None:
        capture["fv"], capture["mk"] = fv, mk
    fv = peer_percentiles(fv, LEVELS, min_peers)
    mk = peer_percentiles(mk, MKT_LEVELS, min_peers)
    # ---- self history
    fh = fhist.join(base.select("issuer_id", "security_id"), on="issuer_id").drop("issuer_id") if not fhist.is_empty() \
        else pl.DataFrame()
    fpart = fv.filter(pl.col("kind") == "fundamental")
    vpart = fv.filter(pl.col("kind") == "valuation")
    fpart = self_scores(fpart, fh, ["security_id"], catf, int(sh["min_obs"]), float(sh["min_window_years"]), zclip)
    vpart = self_scores(vpart, vhist, ["security_id"], catf, int(sh["min_obs"]), float(sh["min_window_years"]), zclip)
    mk = mk.with_columns(pl.lit(None, pl.Float64).alias("self_z"), pl.lit(None, pl.Int32).alias("self_hist_n"),
                         pl.lit(None, pl.Float64).alias("self_score"))
    blend = sp["metric_blend"]

    def blended(parts: dict[str, str], w: dict[str, float]) -> pl.Expr:
        num = pl.sum_horizontal([pl.col(c).fill_null(0) * w[k] for k, c in parts.items()])
        den = pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Float64) * w[k] for k, c in parts.items()])
        return pl.when(den > 0).then(num / den).otherwise(None)

    fpart = fpart.with_columns(pl.lit(None, pl.Float64).alias("mcap_adj_score"),
                               blended({"peer": "peer_percentile", "self": "self_score"}, blend["fundamental"])
                               .alias("metric_score"))
    mk = mk.with_columns(pl.lit(None, pl.Float64).alias("mcap_adj_score"),
                         pl.when(pl.col("scaled")).then(pl.col("raw_value").clip(0, 100))
                         .otherwise(pl.col("peer_percentile")).alias("metric_score"))
    # ---- quality factor first (needed by the market-cap regression)
    pre = pl.concat([fpart, mk], how="diagonal_relaxed").with_columns(
        pl.when(pl.col("imputed")).then(WORST).otherwise(pl.col("metric_score")).alias("metric_score"))
    fs0 = factor_scores_frame(pre, inp, D)
    qual = fs0.filter(pl.col("factor_id") == "quality").select("security_id", pl.col("score").alias("quality_score"))
    rc = fcur.filter(pl.col("metric_id") == "revenue_cagr_3y").select("issuer_id", pl.col("raw_value").alias("revenue_cagr_3y"))
    vpart = vpart.join(qual, on="security_id", how="left").join(rc, on="issuer_id", how="left")
    if capture is not None:
        capture["vpart"], capture["fs0"] = vpart, fs0
    vpart = mcap_adjusted(vpart, int(sp["market_cap_adjustment"]["min_obs"]))
    vpart = vpart.with_columns(
        blended({"peer": "peer_percentile", "self": "self_score", "mcap_adj": "mcap_adj_score"}, blend["valuation"])
        .alias("metric_score")).drop("quality_score", "revenue_cagr_3y")
    ms = pl.concat([fpart, vpart, mk], how="diagonal_relaxed")
    ms = ms.with_columns(
        pl.when(pl.col("imputed")).then(WORST).otherwise(pl.col("metric_score")).alias("metric_score"),
        pl.when(pl.col("imputed")).then(WORST).otherwise(pl.col("peer_percentile")).alias("peer_percentile"))
    fs = factor_scores_frame(ms, inp, D)
    # ---- dq: mean dq of the fundamental inputs used
    dq = fund.filter(pl.col("raw_value").is_not_null()).group_by("issuer_id").agg(
        pl.col("dq_score").mean().alias("dq_score"))
    comp = composite_frame(fs, inp, D, dq)
    if capture is not None:
        capture["ms"], capture["fs"], capture["comp"] = ms, fs, comp
    peer = ms.filter(pl.col("raw_value").is_not_null() | pl.col("imputed"))         .join(inp.mfac_set, on=["kpi_set_id", "metric_id"], how="left").with_columns(
        pl.coalesce(pl.col("factor_id"), pl.col("metric_id").replace_strict(inp.mfac, default="quality",
                                                                            return_dtype=pl.Utf8)).alias("factor_id"),
        pl.lit(D).alias("as_of_date"))
    return {"peer": peer, "factor": fs, "composite": comp}


# ------------------------------------------------------------------------------------------------ job


def month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while True:
        nm = date(y + (m == 12), m % 12 + 1, 1)
        d = nm - timedelta(days=1)
        if d > end:
            break
        if d >= start:
            out.append(d)
        y, m = nm.year, nm.month
    return out


def _expand_extra(df: pl.DataFrame, inp: Inputs) -> pl.DataFrame:
    """Copy issuer-level rows onto the issuer's other universe securities."""
    if inp.extra_sec.is_empty() or df.is_empty():
        return df
    x = df.join(inp.extra_sec.rename({"security_id": "__s2"}), on="issuer_id", how="inner") \
        .with_columns(pl.col("__s2").alias("security_id")).drop("__s2")
    return pl.concat([df, x.select(df.columns)], how="vertical_relaxed")


def _delta_1m(fs: pl.DataFrame, settings: Settings, table: str = "factor_scores") -> pl.DataFrame:
    """score - score ~1 month earlier (21 trading days ~ 30 calendar days; previous row within 20..45 days)."""
    dates = fs["as_of_date"].unique()
    lo, hi = dates.min() - timedelta(days=50), dates.max()
    try:
        prev = (scan_table(table, settings).filter(pl.col("as_of_date") >= lo, pl.col("as_of_date") <= hi)
                .select("as_of_date", "security_id", "factor_id", "score").collect())
    except Exception:
        prev = pl.DataFrame()
    hist = pl.concat([prev, fs.select("as_of_date", "security_id", "factor_id", "score")], how="vertical_relaxed") \
        if not prev.is_empty() else fs.select("as_of_date", "security_id", "factor_id", "score")
    hist = hist.unique(subset=["as_of_date", "security_id", "factor_id"], keep="last") \
        .filter(pl.col("score").is_not_null()) \
        .rename({"as_of_date": "__pd", "score": "__ps"}).sort("security_id", "factor_id", "__pd")
    # The stored history is month-end + weekly rows, so the row ~28 days back is often missing (as_of 10-23 ->
    # target 09-25: the backward match is 08-31, 53 days old, and delta_1m was null for every security; completeness
    # audit 2026-10-27). Take the reference row closest to the target on either side, 20..50 days before as_of.
    q = fs.with_columns((pl.col("as_of_date") - timedelta(days=28)).alias("__t")).sort("security_id", "factor_id", "__t")
    by = ["security_id", "factor_id"]
    jb = q.join_asof(hist, left_on="__t", right_on="__pd", by=by, strategy="backward", check_sortedness=False)
    hf = hist.rename({"__pd": "__pf", "__ps": "__psf"})
    j = jb.sort("security_id", "factor_id", "__t").join_asof(hf, left_on="__t", right_on="__pf", by=by,
                                                              strategy="forward", check_sortedness=False)
    gap_b = (pl.col("as_of_date") - pl.col("__pd")).dt.total_days()
    gap_f = (pl.col("as_of_date") - pl.col("__pf")).dt.total_days()
    ok_b = gap_b.is_between(20, 50)
    ok_f = gap_f.is_between(20, 50)
    dist_b = (pl.col("__t") - pl.col("__pd")).dt.total_days().abs()
    dist_f = (pl.col("__pf") - pl.col("__t")).dt.total_days().abs()
    ref = (pl.when(ok_b & ok_f).then(pl.when(dist_b <= dist_f).then(pl.col("__ps")).otherwise(pl.col("__psf")))
           .when(ok_b).then(pl.col("__ps")).when(ok_f).then(pl.col("__psf")).otherwise(None))
    return j.with_columns((pl.col("score") - ref).alias("delta_1m")).drop("__t", "__pd", "__ps", "__pf", "__psf")


def write_scores(peer: list[pl.DataFrame], fac: list[pl.DataFrame], comp: list[pl.DataFrame], inp: Inputs,
                 run_id: str | None, write_peer: bool = True) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    stamp = [pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id")]
    out = {}
    if fac:
        fs = _expand_extra(pl.concat(fac, how="vertical_relaxed"), inp)
        fs = _delta_1m(fs, inp.s)
        fs = fs.with_columns(pl.col("as_of_date").dt.year().cast(pl.Int16).alias("year"), *stamp).select(
            "as_of_date", "year", "security_id", "issuer_id", "factor_id", "score", "score_raw",
            pl.col("coverage").cast(pl.Float32), "rank_in_sector", "sector_count", "delta_1m", "computed_at", "run_id")
        out["factor_scores"] = write_table(fs, "factor_scores")
    if comp:
        cs = _expand_extra(pl.concat(comp, how="vertical_relaxed"), inp)
        cs = cs.with_columns(pl.col("as_of_date").dt.year().cast(pl.Int16).alias("year"), *stamp).select(
            "as_of_date", "year", "security_id", "issuer_id", "profile_id", "score", "grade", "rank_global",
            "rank_sector", pl.col("coverage").cast(pl.Float32), pl.col("dq_score").cast(pl.Float32), "computed_at",
            "run_id")
        out["composite_scores"] = write_table(cs, "composite_scores")
    if peer and write_peer:
        ps = _expand_extra(pl.concat(peer, how="vertical_relaxed"), inp)
        ps = ps.with_columns(pl.col("as_of_date").dt.strftime("%Y-%m").alias("as_of_month"), *stamp).select(
            "as_of_date", "as_of_month", "security_id", "issuer_id", "metric_id", "factor_id", "raw_value",
            "peer_group_key", "peer_group_level", "peer_count", "peer_percentile", "peer_median", "self_z",
            "self_hist_n", "self_score", "mcap_adj_score", "metric_score", pl.col("imputed").fill_null(False),
            "computed_at", "run_id")
        out["peer_scores"] = write_table(ps, "peer_scores")
    return out


def prune_peer_scores(D: date, settings: Settings | None = None, keep_days: int = 35) -> int:
    """peer_scores retention (contract): daily rows for the last `keep_days`, older dates only Fridays + month-ends.
    Rewrites only the month partitions that still hold prunable dates. Returns rows removed."""
    cutoff = D - timedelta(days=keep_days)
    lf = scan_table("peer_scores", settings).filter(pl.col("as_of_date") < cutoff)
    is_me = (pl.col("as_of_date") + timedelta(days=1)).dt.month() != pl.col("as_of_date").dt.month()
    keep = (pl.col("as_of_date").dt.weekday() == 5) | is_me
    months = lf.filter(~keep).select("as_of_month").unique().collect()["as_of_month"].to_list()
    removed = 0
    for m in months:
        part = scan_table("peer_scores", settings).filter(pl.col("as_of_month") == m).collect()
        kept = part.filter((pl.col("as_of_date") >= cutoff) | keep)
        removed += part.height - kept.height
        if kept.height:
            write_table(kept, "peer_scores", mode="overwrite_partition")
    return removed


def run(ctx):  # noqa: ANN001 - JobContext
    import time

    from app.scheduler.base import JobResult

    s = ctx.settings
    p = ctx.params or {}
    mode = p.get("mode", "latest")
    end = date.fromisoformat(str(p["end"])) if p.get("end") else ctx.as_of
    if p.get("dates"):
        dates = sorted(date.fromisoformat(str(d)) for d in p["dates"])
    elif mode == "history":
        start = date.fromisoformat(str(p.get("start") or p.get("since"))) if (p.get("start") or p.get("since")) \
            else date(end.year - int(p.get("years", 10)), end.month, 1)
        dates = month_ends(start, end)
        if not dates or dates[-1] != end:
            dates.append(end)
    else:
        dates = [end]
    t0 = time.time()
    inp = Inputs(p.get("universe", "mvp"), s, p.get("tickers"), dates[0], dates[-1])
    log.info("scoring inputs loaded in %.1fs: %d issuers, %d dates", time.time() - t0, len(inp.issuers), len(dates))
    rows: dict[str, int] = {}
    batch: dict[str, list[pl.DataFrame]] = {"peer": [], "factor": [], "composite": []}
    cur_year = None
    n_comp = 0
    for D in dates:
        ctx.check_cancelled()
        if cur_year is not None and D.year != cur_year:
            for k, v in write_scores(batch["peer"], batch["factor"], batch["composite"], inp, ctx.run_id,
                                     p.get("write_peer", True)).items():
                rows[k] = rows.get(k, 0) + v
            batch = {"peer": [], "factor": [], "composite": []}
        cur_year = D.year
        res = score_date(inp, D)
        for k in batch:
            batch[k].append(res[k])
        if D == dates[-1]:
            n_comp = res["composite"].filter(pl.col("profile_id") == "balanced", pl.col("score").is_not_null()).height
        log.info("scored %s: composites=%d", D, res["composite"].filter(pl.col("score").is_not_null()).height)
    for k, v in write_scores(batch["peer"], batch["factor"], batch["composite"], inp, ctx.run_id,
                             p.get("write_peer", True)).items():
        rows[k] = rows.get(k, 0) + v
    if mode != "history" and p.get("prune", True):
        try:
            pruned = prune_peer_scores(dates[-1], s)
            if pruned:
                log.info("peer_scores retention: removed %d rows", pruned)
        except Exception as e:  # retention is housekeeping; never fail the scoring run
            log.warning("peer_scores prune failed: %s", e)
    n = len(inp.issuers)
    return JobResult(status="success" if n_comp else "partial", rows_written=rows, entities_ok=n_comp,
                     entities_failed=n - n_comp,
                     message=f"{len(dates)} dates ({dates[0]}..{dates[-1]}); balanced composite for {n_comp}/{n} issuers "
                             f"on {dates[-1]}; {time.time() - t0:.0f}s")
