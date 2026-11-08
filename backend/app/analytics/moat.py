"""Moat classification (weekly job `moat`) -> table `moat` (+ `cost_of_capital`).

Spec: contracts/scoring.yaml `moat`. Evidence per issuer from the PIT FY panel (fundamentals_wide LOCAL, last 10y):

* roic_wacc_spread / roic_wacc_persistence: ROIC = EBIT*(1-t)/avg(invested_capital); per-year WACC =
  WACC(as_of) shifted by the change in the currency's 10y yield (rates regime). Financials (banks, insurers,
  consumer finance, holdings, levered broker-dealers): ROE - cost of equity.
  Spread score = 0.5 * percentile within sector + 0.5 * absolute map (-5% -> 0, +15% -> 100).
* gm_level / gm_stability: gross margin level (5y mean) and -stdev (window): 0.5 * percentile within subsector
  + 0.5 * absolute anchor (GM 60% -> 100; stdev 0 -> 100, 8pp -> 0); financials: pretax margin. Through-cycle min
  kept as evidence. Commodity producers (mining / energy / commodity processors) are capped at narrow.
* margin_premium: mean over window of (EBIT margin - subsector median that year), global percentile.
* market_share: revenue (USD) / sum subsector revenue of *our universe* (not true market share);
  0.5 * percentile within subsector + 0.5 * min(100, 100 * share * n_peers / 3).
* intangibles_intensity: (R&D + intangible amortization + capitalized intangibles) / revenue, 3y mean, sector pct.
* revenue_stability (switching-cost proxy): -stdev of yoy revenue growth, global percentile.
* customer_lockin (switching-cost proxy): deferred revenue / revenue, 3y mean, global percentile (null if absent).

Percentiles fall back subsector -> sector -> all when a group has < 5 members. moat_score = weight-renormalized mean
of available components; classification/confidence per scoring.yaml (thresholds calibrated on live data, see
`classify`). Sources of moat (API vocabulary): cost_advantage, intangibles, switching_costs, network,
efficient_scale, scale - each with a 0..100 strength.

Job params: mode = latest (default; auto-backfills yearly history when the table has none) | history;
history_years (default 5); universe (mvp|full) / tickers restrict the *written* issuers (peer groups always use
every issuer with fundamentals).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

import numpy as np
import orjson
import polars as pl
import yaml

from app.analytics import cost_of_capital as cc
from app.core.config import get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table
from app.normalize.taxonomy import subsector_groups

log = get_logger(__name__)

WINDOW = 10
MIN_PEERS = 5
CORE = ["roic_wacc_spread", "roic_wacc_persistence", "gm_level", "gm_stability", "margin_premium", "market_share",
        "intangibles_intensity"]
#: component pass thresholds (score 0..100); persistence is a share-of-years score (60 = 6 of 10 years)
THRESHOLDS = {"roic_wacc_spread": 60.0, "roic_wacc_persistence": 70.0, "gm_level": 60.0, "gm_stability": 60.0,
              "margin_premium": 60.0, "market_share": 60.0, "intangibles_intensity": 60.0,
              "revenue_stability": 60.0, "customer_lockin": 60.0}
COMMODITY_GROUPS = {"mining", "energy", "commodity_processors"}
NETWORK_SUBSECTORS = {"payments_fintech", "internet_platforms", "capital_markets_exchanges", "software_application",
                      "software_infrastructure", "ecommerce", "video_games"}
EFFICIENT_SCALE_GROUPS = {"utilities", "transport", "telecom", "energy"}
EFFICIENT_SCALE_SUBSECTORS = {"rail_marine_infra", "midstream", "capital_markets_exchanges", "water_utilities",
                              "gas_utilities", "electric_utilities", "multi_utilities", "construction_materials"}


@lru_cache(maxsize=1)
def moat_spec() -> dict[str, Any]:
    p = get_settings().contracts_dir / "scoring.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))["moat"]


def weights() -> dict[str, float]:
    spec = moat_spec()
    w = {k: float(v["weight"]) for k, v in spec["components"].items()}
    w.update({k: float(v["weight"]) for k, v in (spec.get("supplementary_components") or {}).items()})
    t = sum(w.values())
    return {k: v / t for k, v in w.items()}


# ------------------------------------------------------------------------------------------------ helpers


def _pct(col: str, by: list[str], out: str, higher: bool = True) -> list[pl.Expr]:
    """Percentile 0..100 of `col` within `by` (nulls stay null); n==1 -> 50."""
    x = (pl.col(col) if higher else -pl.col(col)).round(6)  # float noise must not break ties
    n = pl.col(col).count().over(by)
    r = x.rank("average").over(by)
    return [pl.when(pl.col(col).is_null()).then(None).when(n <= 1).then(50.0)
            .otherwise((r - 1) / (n - 1) * 100).alias(out), n.alias(out + "__n")]


def _pct_fallback(df: pl.DataFrame, col: str, out: str, levels: list[list[str]], higher: bool = True) -> pl.DataFrame:
    """Percentile within the first grouping level with >= MIN_PEERS non-null members."""
    parts = []
    for i, by in enumerate(levels):
        df = df.with_columns(*_pct(col, by, f"_p{i}", higher))
        parts.append(pl.when(pl.col(f"_p{i}__n") >= MIN_PEERS).then(pl.col(f"_p{i}")))
    df = df.with_columns(*_pct(col, ["_all"], "_pall", higher))
    df = df.with_columns(pl.coalesce([*parts, pl.col("_pall")]).alias(out))
    return df.drop([c for c in df.columns if c.startswith("_p")])


def _slope(xs: list[float], ys: list[float]) -> float | None:
    pts = [(x, y) for x, y in zip(xs, ys, strict=False) if y is not None and math.isfinite(y)]
    if len(pts) < 3:
        return None
    x = np.array([p[0] for p in pts], float)
    y = np.array([p[1] for p in pts], float)
    x = x - x.mean()
    d = (x * x).sum()
    return float((x * (y - y.mean())).sum() / d) if d > 0 else None


def classify(score: float | None, persistence: float | None, years: int, coverage: float,
             spec: dict[str, Any] | None = None) -> str:
    """scoring.yaml classification (thresholds from the contract's `calibrated` block when present)."""
    spec = spec or moat_spec()
    cal = spec.get("calibrated", {})
    min_years = int(spec.get("min_years", 5))
    if years < min_years or coverage < 0.6 or score is None:
        return "insufficient_data"
    p = persistence or 0.0
    if score >= cal.get("wide_score", 70) and p >= cal.get("wide_persistence", 0.8) and years >= cal.get(
            "wide_min_years", 7):
        return "wide"
    if score >= cal.get("narrow_score", 50) and p >= cal.get("narrow_persistence", 0.6):
        return "narrow"
    return "none"


def confidence(years: int, coverage: float, spread_std: float | None) -> float:
    s = 0.0 if spread_std is None or not math.isfinite(spread_std) else min(spread_std / 0.15, 1.0)
    return round(0.4 * min(years / 10, 1.0) + 0.3 * coverage + 0.3 * (1 - s), 4)


# ------------------------------------------------------------------------------------------------ panel


def robust_ebit() -> pl.Expr:
    """EBIT capped at pretax + interest + 5% of revenue (guards mis-tagged operating income), fallback pretax-based."""
    base = pl.col("pretax_income") + pl.col("interest_expense").fill_null(0)
    e = pl.coalesce("ebit", "operating_income")
    return (pl.when(e.is_not_null() & base.is_not_null())
            .then(pl.min_horizontal(e, base + 0.05 * pl.col("revenue").abs().fill_null(0)))
            .otherwise(pl.coalesce(e, base)))


def robust_invested_capital() -> pl.Expr:
    eq = pl.coalesce("total_equity", pl.col("total_assets") - pl.col("total_liabilities"))
    alt = pl.col("total_debt").fill_null(0) + pl.col("operating_lease_liabilities").fill_null(0) + eq         - pl.col("cash_and_st_investments").fill_null(0)
    return pl.coalesce("invested_capital", alt)


def _rf_by_year(inp: cc.Inputs, fy: pl.DataFrame) -> pl.DataFrame:
    pairs = fy.select("currency", "period_end").unique()
    rows = []
    for ccy, pe in pairs.iter_rows():
        rf, src = cc.risk_free(inp.macro, ccy, pe)
        rows.append({"currency": ccy, "period_end": pe, "rf_y": rf, "rf_fb": src.startswith("fallback")})
    return pl.DataFrame(rows, schema={"currency": pl.Utf8, "period_end": pl.Date, "rf_y": pl.Float64,
                                      "rf_fb": pl.Boolean})


def build_panel(inp: cc.Inputs, as_of: date, coc: pl.DataFrame) -> pl.DataFrame:
    """Per (issuer, fiscal_year) evidence series over the last WINDOW(+1) years known at as_of."""
    fy = cc.fy_asof(inp.fy_all, as_of, max_years=WINDOW + 1)
    if fy.is_empty():
        return fy
    need = ["revenue", "gross_profit", "ebit", "income_tax", "pretax_income", "invested_capital", "net_income_parent",
            "total_equity_parent", "rnd", "amortization_intangibles", "capex_intangibles", "deferred_revenue",
            "interest_expense", "operating_income", "total_debt", "total_equity", "total_assets", "total_liabilities",
            "cash_and_st_investments", "operating_lease_liabilities"]
    fy = fy.with_columns([pl.lit(None, pl.Float64).alias(c) for c in need if c not in fy.columns])
    fy = fy.with_columns(robust_ebit().alias("ebit"), robust_invested_capital().alias("invested_capital"),
                         pl.coalesce("total_equity_parent", "total_equity",
                                     pl.col("total_assets") - pl.col("total_liabilities")).alias("total_equity_parent"))
    rfy = _rf_by_year(inp, fy)
    now_rf = {r["currency"]: cc.risk_free(inp.macro, r["currency"], as_of)[0] for r in
              coc.select("currency").unique().iter_rows(named=True)}
    c = coc.select("issuer_id", "wacc", "cost_of_equity", "tax_rate", "is_financial", "currency").with_columns(
        pl.col("currency").replace_strict(now_rf, default=None, return_dtype=pl.Float64).alias("rf_now"))
    p = (fy.sort(["issuer_id", "fiscal_year"])
         .join(c.drop("currency"), on="issuer_id", how="inner")
         .join(rfy, on=["currency", "period_end"], how="left")
         .with_columns(
             pl.col("invested_capital").shift(1).over("issuer_id").alias("ic_prev"),
             pl.col("total_equity_parent").shift(1).over("issuer_id").alias("eq_prev"),
             pl.col("revenue").shift(1).over("issuer_id").alias("rev_prev"),
             pl.col("fiscal_year").shift(1).over("issuer_id").alias("fy_prev")))
    consec = pl.col("fy_prev") == pl.col("fiscal_year") - 1
    tax_y = pl.when(pl.col("pretax_income") > 0).then(
        (pl.col("income_tax") / pl.col("pretax_income")).clip(0.0, 0.40)).otherwise(pl.col("tax_rate"))
    ic_avg = pl.when(consec & (pl.col("ic_prev") > 0)).then((pl.col("invested_capital") + pl.col("ic_prev")) / 2) \
        .otherwise(pl.col("invested_capital"))
    eq_avg = pl.when(consec & (pl.col("eq_prev") > 0)).then((pl.col("total_equity_parent") + pl.col("eq_prev")) / 2) \
        .otherwise(pl.col("total_equity_parent"))
    shift = (pl.col("rf_y") - pl.col("rf_now")).fill_null(0.0)
    p = p.with_columns(
        pl.when(ic_avg > 0).then((pl.col("ebit") * (1 - tax_y.fill_null(pl.col("tax_rate"))) / ic_avg).clip(-1.0, 1.0))
        .alias("roic"),
        pl.when(eq_avg > 0).then((pl.col("net_income_parent") / eq_avg).clip(-1.0, 1.0)).alias("roe"),
        (pl.col("wacc") + shift).clip(0.03, 0.30).alias("wacc_y"),
        (pl.col("cost_of_equity") + shift).clip(0.03, 0.35).alias("ke_y"),
        pl.when(pl.col("revenue") > 0).then(pl.col("gross_profit") / pl.col("revenue")).alias("gm"),
        pl.when(pl.col("revenue") > 0).then(pl.col("pretax_income") / pl.col("revenue")).alias("ptm"),
        pl.when(pl.col("revenue") > 0).then(pl.col("ebit") / pl.col("revenue")).alias("om"),
        pl.when(pl.col("revenue") > 0).then(pl.col("net_income_parent") / pl.col("revenue")).alias("nm"),
        pl.when(consec & (pl.col("rev_prev") > 0) & (pl.col("revenue") > 0))
        .then(pl.col("revenue") / pl.col("rev_prev") - 1).alias("rev_g"),
        pl.when(pl.col("revenue") > 0).then(
            (pl.col("rnd").fill_null(0) + pl.col("amortization_intangibles").fill_null(0)
             + pl.col("capex_intangibles").fill_null(0)) / pl.col("revenue")).alias("intang"),
        pl.when((pl.col("revenue") > 0) & pl.col("deferred_revenue").is_not_null())
        .then(pl.col("deferred_revenue") / pl.col("revenue")).alias("lockin"))
    p = p.with_columns(
        pl.when(pl.col("is_financial")).then(pl.col("roe") - pl.col("ke_y"))
        .otherwise(pl.col("roic") - pl.col("wacc_y")).clip(-0.5, 0.5).alias("spread"),
        pl.when(pl.col("is_financial")).then(pl.col("ptm")).otherwise(pl.col("gm")).clip(-1.0, 1.0).alias("gm_x"),
        pl.when(pl.col("is_financial")).then(pl.col("nm")).otherwise(pl.col("om")).clip(-1.0, 1.0).alias("om_x"))
    # keep the evaluation window (the extra oldest year only fed the averages)
    p = p.with_columns(pl.col("fiscal_year").rank("dense", descending=True).over("issuer_id").alias("_r"))
    return p.filter(pl.col("_r") <= WINDOW).drop("_r")


def compute(inp: cc.Inputs, as_of: date, coc: pl.DataFrame, run_id: str | None = None) -> pl.DataFrame:
    """Moat rows (contract `moat`) for every issuer with FY fundamentals at as_of."""
    p = build_panel(inp, as_of, coc)
    if p.is_empty():
        return pl.DataFrame()
    iss = inp.issuers.select("issuer_id", "sector_id", "subsector_id", "region")
    p = p.join(iss, on="issuer_id", how="left").with_columns(
        pl.col("subsector_id").fill_null("_none"), pl.col("sector_id").fill_null("_none"))
    # subsector median EBIT margin per fiscal year (fallback sector, then all of the same model family)
    p = p.with_columns(
        pl.col("om_x").median().over(["subsector_id", "fiscal_year", "is_financial"]).alias("med_sub"),
        pl.col("om_x").count().over(["subsector_id", "fiscal_year", "is_financial"]).alias("n_sub"),
        pl.col("om_x").median().over(["sector_id", "fiscal_year", "is_financial"]).alias("med_sec"),
        pl.col("om_x").count().over(["sector_id", "fiscal_year", "is_financial"]).alias("n_sec"),
        pl.col("om_x").median().over(["fiscal_year", "is_financial"]).alias("med_all"))
    p = p.with_columns((pl.col("om_x") - pl.when(pl.col("n_sub") >= 3).then(pl.col("med_sub"))
                        .when(pl.col("n_sec") >= 3).then(pl.col("med_sec")).otherwise(pl.col("med_all")))
                       .alias("om_prem"))
    agg = p.sort(["issuer_id", "fiscal_year"]).group_by("issuer_id").agg(
        pl.col("spread").drop_nulls().len().alias("years"),
        pl.col("spread").mean().alias("spread_avg"),
        (pl.col("spread").drop_nulls() > 0).mean().alias("persistence"),
        pl.col("spread").std().alias("spread_std"),
        pl.col("fiscal_year"), pl.col("spread").alias("spread_series"), pl.col("roic"), pl.col("roe"),
        pl.col("wacc_y"), pl.col("ke_y"),
        pl.col("gm_x").tail(5).mean().alias("gm_level"),
        pl.col("gm_x").std().alias("gm_std"), pl.col("gm_x").min().alias("gm_min"), pl.col("gm_x").alias("gm_series"),
        pl.col("om_prem").mean().alias("om_prem"), pl.col("om_prem").alias("om_prem_series"),
        pl.col("intang").tail(3).mean().alias("intang"),
        pl.col("rev_g").std().alias("rev_g_std"), pl.col("rev_g").drop_nulls().len().alias("n_g"),
        pl.col("lockin").tail(3).mean().alias("lockin"),
        pl.col("is_financial").last(), pl.col("subsector_id").last(), pl.col("sector_id").last(),
        pl.col("wacc").last(), pl.col("period_end").last().alias("last_fy_end"))
    agg = agg.with_columns(pl.when(pl.col("n_g") >= 4).then(pl.col("rev_g_std")).alias("rev_g_std"),
                           pl.when(pl.col("years") >= 3).then(pl.col("gm_std")).alias("gm_std"))
    # market share (USD revenue of the latest FY, within 18 months of as_of)
    usd = cc.fy_asof(inp.fy_usd_all, as_of, max_years=WINDOW)
    usd = usd.join(iss, on="issuer_id", how="left").with_columns(pl.col("subsector_id").fill_null("_none"))
    last_usd = (usd.filter(pl.col("revenue") > 0).sort(["issuer_id", "fiscal_year"]).group_by("issuer_id").last()
                .filter(pl.col("period_end") >= pl.lit(as_of) - pl.duration(days=550)))
    last_usd = last_usd.with_columns(
        (pl.col("revenue") / pl.col("revenue").sum().over("subsector_id")).alias("share"),
        pl.col("revenue").count().over("subsector_id").alias("n_peers_sub"),
        pl.col("revenue").rank("ordinal", descending=True).over("subsector_id").alias("scale_rank"))
    agg = agg.join(last_usd.select("issuer_id", "share", "n_peers_sub", "scale_rank",
                                   pl.col("revenue").alias("revenue_usd")), on="issuer_id", how="left")
    # staleness: last FY must be within ~2 years of as_of
    agg = agg.with_columns((pl.col("last_fy_end") >= pl.lit(as_of) - pl.duration(days=730)).alias("fresh"))
    agg = agg.with_columns(pl.lit(1).alias("_all"))
    sub, sec = ["subsector_id"], ["sector_id"]
    fam = ["is_financial"]
    agg = _pct_fallback(agg, "spread_avg", "p_spread", [sec + fam, fam])
    agg = _pct_fallback(agg, "gm_level", "s_gm_level", [sub, sec + fam, fam])
    agg = _pct_fallback(agg, "gm_std", "s_gm_stability", [sub, sec + fam, fam], higher=False)
    agg = _pct_fallback(agg, "om_prem", "s_margin_premium", [fam])
    agg = _pct_fallback(agg, "share", "p_share", [sub])
    agg = _pct_fallback(agg, "intang", "s_intangibles", [sec])
    agg = _pct_fallback(agg, "rev_g_std", "s_rev_stab", [fam], higher=False)
    agg = _pct_fallback(agg, "lockin", "s_lockin", [["_all"]])
    agg = agg.with_columns(
        pl.when(pl.col("spread_avg").is_not_null()).then(
            0.5 * pl.col("p_spread") + 0.5 * ((pl.col("spread_avg") + 0.05) / 0.20 * 100).clip(0, 100))
        .alias("s_spread"),
        pl.when(pl.col("years") > 0).then(pl.col("persistence") * 100).alias("s_persist"),
        # absolute anchors blended with the peer percentiles (small peer groups are noisy)
        pl.when(pl.col("s_gm_level").is_not_null()).then(
            0.5 * pl.col("s_gm_level") + 0.5 * (pl.col("gm_level") / 0.6 * 100).clip(0, 100)).alias("s_gm_level"),
        pl.when(pl.col("s_gm_stability").is_not_null()).then(
            0.5 * pl.col("s_gm_stability") + 0.5 * (100 - pl.col("gm_std") / 0.08 * 100).clip(0, 100))
        .alias("s_gm_stability"),
        pl.when(pl.col("share").is_not_null()).then(
            0.5 * pl.col("p_share") + 0.5 * (100 * pl.col("share") * pl.col("n_peers_sub") / 3).clip(0, 100))
        .alias("s_share"))
    # financials: gm-like components only when their proxy exists; intangibles not meaningful
    agg = agg.with_columns(pl.when(pl.col("is_financial")).then(None).otherwise(pl.col("s_intangibles"))
                           .alias("s_intangibles"))
    return _finalize(agg, as_of, run_id)


COMP_COLS = {"roic_wacc_spread": "s_spread", "roic_wacc_persistence": "s_persist", "gm_level": "s_gm_level",
             "gm_stability": "s_gm_stability", "margin_premium": "s_margin_premium", "market_share": "s_share",
             "intangibles_intensity": "s_intangibles", "revenue_stability": "s_rev_stab",
             "customer_lockin": "s_lockin"}
RAW_COLS = {"roic_wacc_spread": "spread_avg", "roic_wacc_persistence": "persistence", "gm_level": "gm_level",
            "gm_stability": "gm_std", "margin_premium": "om_prem", "market_share": "share",
            "intangibles_intensity": "intang", "revenue_stability": "rev_g_std", "customer_lockin": "lockin"}


def _r(x: float | None, n: int = 4) -> float | None:
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else round(float(x), n)


def sources(r: dict[str, Any], sc: dict[str, float | None]) -> list[dict[str, Any]]:
    """Heuristic moat-source tags with 0..100 strength (API vocabulary)."""
    def m(*ks: str) -> float | None:
        v = [sc.get(k) for k in ks if sc.get(k) is not None]
        return float(np.mean(v)) if v else None
    out: list[tuple[str, float | None]] = []
    groups = set(subsector_groups(r.get("subsector_id")))
    sub = r.get("subsector_id") or ""
    st = m("intangibles_intensity", "gm_level")
    if st is not None and st >= 65 and (sc.get("gm_level") or 0) >= 60:
        out.append(("intangibles", st))
    st = m("revenue_stability", "gm_stability", "customer_lockin")
    if st is not None and st >= 65:
        out.append(("switching_costs", st))
    st = m("market_share", "margin_premium")
    if sub in NETWORK_SUBSECTORS and st is not None and st >= 65:
        out.append(("network", st))
    st = m("margin_premium", "market_share")
    if (sc.get("margin_premium") or 0) >= 65 and (sc.get("gm_level") or 100) < 60:
        out.append(("cost_advantage", st))
    if (sub in EFFICIENT_SCALE_SUBSECTORS or groups & EFFICIENT_SCALE_GROUPS) and (sc.get("market_share") or 0) >= 70:
        out.append(("efficient_scale", sc.get("market_share")))
    if (sc.get("market_share") or 0) >= 80 and (r.get("scale_rank") or 99) <= 3 and not any(
            t == "efficient_scale" for t, _ in out):
        out.append(("scale", sc.get("market_share")))
    return [{"type": t, "strength": _r(s, 1)} for t, s in sorted(out, key=lambda x: -(x[1] or 0))]


def _finalize(agg: pl.DataFrame, as_of: date, run_id: str | None) -> pl.DataFrame:
    w = weights()
    now = datetime.now(UTC)
    rows = []
    for r in agg.iter_rows(named=True):
        sc = {k: _r(r.get(c), 2) for k, c in COMP_COLS.items()}
        if not r["fresh"]:
            sc = {k: None for k in sc}
        avail = {k: v for k, v in sc.items() if v is not None and k in w}
        tw = sum(w[k] for k in avail)
        score = sum(w[k] * v for k, v in avail.items()) / tw if tw > 0 else None
        cov = sum(1 for k in CORE if sc.get(k) is not None) / len(CORE)
        years = int(r["years"] or 0) if r["fresh"] else 0
        cls = classify(score, r["persistence"], years, cov)
        capped = None
        if cls == "wide" and set(subsector_groups(r.get("subsector_id"))) & COMMODITY_GROUPS:
            cls, capped = "narrow", "commodity_cyclical"  # cyclical commodity producers: wide needs more than a boom
        conf = confidence(years, cov, r["spread_std"]) if cls != "insufficient_data" else round(
            0.4 * min(years / 10, 1.0) + 0.3 * cov, 4)
        fys = r["fiscal_year"] or []
        detail: dict[str, Any] = {}
        series = {"roic_wacc_spread": r["spread_series"], "gm_level": r["gm_series"],
                  "margin_premium": r["om_prem_series"]}
        for k in COMP_COLS:
            th = THRESHOLDS.get(k, 50.0)
            d: dict[str, Any] = {"score": sc[k], "value": _r(r.get(RAW_COLS[k])), "threshold": th,
                                 "passed": None if sc[k] is None else bool(sc[k] >= th), "weight": w.get(k)}
            if k in series and series[k] is not None:
                d["history"] = [{"fiscal_year": int(y), "value": _r(v)} for y, v in zip(fys, series[k], strict=False)
                                if v is not None]
            detail[k] = d
        detail["roic_wacc_spread"]["through_cycle"] = {
            "trend_per_year": _r(_slope(fys, r["spread_series"] or [])), "stdev": _r(r["spread_std"])}
        detail["gm_stability"]["through_cycle_min"] = _r(r["gm_min"])
        detail["market_share"]["note"] = "share of this platform's universe revenue in the subsector, not true market share"
        detail["market_share"]["scale_rank"] = r["scale_rank"]
        if capped:
            detail["_cap"] = capped
        detail["_roic_vs_wacc"] = [
            {"fiscal_year": int(y), "roic": _r(ro if not r["is_financial"] else re),
             "hurdle": _r(wy if not r["is_financial"] else ky)}
            for y, ro, re, wy, ky in zip(fys, r["roic"] or [], r["roe"] or [], r["wacc_y"] or [], r["ke_y"] or [],
                                         strict=False)]
        src = sources(r, sc) if cls in ("wide", "narrow") else []
        rows.append({
            "as_of_date": as_of, "issuer_id": r["issuer_id"], "moat_class": cls, "confidence": conf,
            "moat_score": _r(score, 2), "roic_wacc_spread_avg": _r(r["spread_avg"]),
            "roic_wacc_persistence": _r(r["persistence"]), "years_evaluated": years or None,
            "gm_level_pct": sc["gm_level"], "gm_stability_pct": sc["gm_stability"],
            "margin_premium_pct": sc["margin_premium"], "subsector_revenue_share": _r(r["share"], 6),
            "intangibles_intensity_pct": sc["intangibles_intensity"],
            "component_scores": orjson.dumps({k: v for k, v in sc.items() if v is not None}).decode(),
            "component_detail": orjson.dumps(detail).decode(),
            "sources_of_moat": [s["type"] for s in src] or None,
            "moat_sources": orjson.dumps(src).decode(),
            "model_variant": "financial" if r["is_financial"] else "standard",
            "wacc": _r(r["wacc"]), "computed_at": now, "run_id": run_id})
    return pl.DataFrame(rows, infer_schema_length=None)


# ------------------------------------------------------------------------------------------------ job


def target_issuers(params: dict[str, Any], as_of: date, settings=None) -> set[str] | None:
    """Issuers to write (None = all with fundamentals). params.universe mvp|full, params.tickers."""
    s = settings or get_settings()
    uni = params.get("universe")
    tickers = params.get("tickers")
    ids: set[str] | None = None
    sm = scan_table("securities_master", s).select("security_id", "issuer_id", "display_ticker").collect()
    if uni:
        names = ["mvp_us", "mvp_global"] if uni == "mvp" else [uni]
        um = (scan_table("universe_membership", s)
              # current members (valid_from is the membership creation date, often after the data as_of)
              .filter(pl.col("universe_id").is_in(names) & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= as_of)))
              .select("security_id").collect())
        ids = set(sm.join(um, on="security_id").get_column("issuer_id").to_list())
    if tickers:
        tk = [t.strip() for t in (tickers if isinstance(tickers, list) else str(tickers).split(","))]
        t_ids = set(sm.filter(pl.col("display_ticker").is_in(tk)).get_column("issuer_id").to_list())
        ids = t_ids if ids is None else ids & t_ids
    return ids


def history_dates(as_of: date, years: int) -> list[date]:
    return [date(as_of.year - k, 12, 31) for k in range(years, 0, -1) if date(as_of.year - k, 12, 31) < as_of]


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    as_of = ctx.as_of
    dates = [as_of]
    mode = p.get("mode", "latest")
    if mode == "history":
        dates = history_dates(as_of, int(p.get("history_years", 5))) + [as_of]
    else:
        try:
            have = scan_table("moat", s).filter(pl.col("as_of_date") < as_of).select(pl.len()).collect().item()
        except Exception:  # noqa: BLE001
            have = 0
        if not have:
            dates = history_dates(as_of, int(p.get("history_years", 5))) + [as_of]
    keep = target_issuers(p, as_of, s)
    inp = cc.load_inputs(dates, s)
    moat_parts, coc_parts = [], []
    for d in dates:
        ctx.check_cancelled()
        coc = cc.compute(inp, d, run_id=ctx.run_id)
        if coc.is_empty():
            continue
        m = compute(inp, d, coc, run_id=ctx.run_id)
        if keep is not None:
            coc, m = coc.filter(pl.col("issuer_id").is_in(list(keep))), m.filter(pl.col("issuer_id").is_in(list(keep)))
        coc_parts.append(coc)
        moat_parts.append(m)
    if not moat_parts:
        return JobResult(status="success", message="no fundamentals")
    moat = pl.concat(moat_parts, how="diagonal_relaxed")
    coc_all = pl.concat(coc_parts, how="diagonal_relaxed")
    rows = {"cost_of_capital": write_table(coc_all, "cost_of_capital", settings=s),
            "moat": write_table(moat, "moat", settings=s)}
    cur = moat.filter(pl.col("as_of_date") == as_of)
    dist = dict(cur.group_by("moat_class").len().iter_rows())
    return JobResult(status="success", rows_written=rows, entities_ok=cur.height,
                     message=f"dates={len(dates)} issuers={cur.height} classes={dist}")
