"""Beneish M-score: components, textbook score and a recalibrated, MODEL-BASED manipulation screen (O2.2a, audit F15).

Owner: analytics-scoring. `app.analytics.ratios.accounting_scores` evaluates the eight indices on the PIT vintage
panels (TTM primary, FY too; lag(x, 1) = the same period one year earlier) and emits them as extra `ratios` rows
(metric ids `beneish_<index>`, plus `beneish_m_growth_adj`, `beneish_red_flags`, `beneish_flag_adj`). They are not in
the sector_kpis registry, so they are never scored or percentiled; the API reads them for one issuer at a time.

Textbook score (Beneish 1999, 8-variable): M = -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI
- 0.172 SGAI + 4.679 TATA - 0.327 LVGI, flag M > -1.78. Missing index -> 1.0 (neutral), missing TATA -> 0; more than
two missing -> no score. Financials (sector FINS) are excluded from the score; the recalibrated screen also excludes
REITs (sector REAL): the model was estimated on non-financial firms and its accrual / asset-quality terms are not
meaningful for balance-sheet businesses.

Why recalibrate. On our universe the textbook rule flags 4.6% of names, mostly hyper-growth companies (NVDA, MU):
the sales-growth index SGI (weight 0.892) and the accruals that working capital builds up during fast growth push
M over -1.78 with no other warning sign. Beneish himself notes growth firms as the main false positives.

Recalibrated screen (`assess`, model-based, NOT an accounting finding):
  1. textbook M > -1.78, AND
  2. growth-neutral M > -1.78, where the growth-neutral score replaces SGI by 1.0 (no growth contribution), AND
  3. at least two "red flags": non-growth indices beyond the mean of Beneish's manipulator sample
     (DSRI > 1.465, GMI > 1.193, AQI > 1.254, DEPI > 1.077, SGAI > 1.041, LVGI > 1.111, TATA > 0.031), AND
  4. at least one corroborating signal outside the M-score: Piotroski F <= 3, cash conversion CFO / net income < 0.5
     with positive net income, or Altman Z'' < 1.1 (distress zone).
  level: "flag" (all four), "watch" (textbook only: growth-driven or uncorroborated), "none", "not_applicable".
  On the 2026-10-19 universe (618 issuers with a score): textbook 28 (4.5%) -> flag 4 (0.6%); NVDA -> watch.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

WEIGHTS: dict[str, float] = {"dsri": 0.920, "gmi": 0.528, "aqi": 0.404, "sgi": 0.892, "depi": 0.115, "sgai": -0.172,
                             "tata": 4.679, "lvgi": -0.327}
CONST = -4.84
THRESHOLD = -1.78
#: means of the manipulator sample (Beneish 1999, Table 2): an index above it is a "red flag"
MANIPULATOR_MEANS: dict[str, float] = {"dsri": 1.465, "gmi": 1.193, "aqi": 1.254, "sgi": 1.607, "depi": 1.077,
                                       "sgai": 1.041, "tata": 0.031, "lvgi": 1.111}
GROWTH_INDEX = "sgi"
MIN_RED_FLAGS = 2
#: corroborating signals outside the M-score (any one is enough)
CORROBORATION = {"piotroski_f_max": 3.0, "cfo_to_net_income_min": 0.5, "altman_z2_distress": 1.1}
EXCLUDED_SECTORS = frozenset({"FINS", "REAL"})
METHOD = ("model-based screen: Beneish (1999) 8-variable M-score; flag only when M > -1.78, the growth-neutral score "
          "(SGI = 1) is also > -1.78, >= 2 non-growth indices exceed the manipulator-sample means and one outside "
          "signal corroborates (Piotroski F <= 3, CFO / net income < 0.5, or Altman Z'' < 1.1)")

#: formula-engine expressions (app.normalize.formula) on vintage panels
COMPONENT_FORMULAS: dict[str, str] = {
    "dsri": "div(div(accounts_receivable, revenue), lag(div(accounts_receivable, revenue), 1))",
    "gmi": "div(lag(gross_margin, 1), gross_margin)",
    "aqi": "div(1 - div(total_current_assets + ppe_net, total_assets), lag(1 - div(total_current_assets + ppe_net, total_assets), 1))",
    "sgi": "div(revenue, lag(revenue, 1))",
    "depi": "div(lag(div(depreciation_amortization, depreciation_amortization + ppe_net), 1), div(depreciation_amortization, depreciation_amortization + ppe_net))",
    "sgai": "div(div(sga, revenue), lag(div(sga, revenue), 1))",
    "tata": "div(coalesce(net_income_continuing, net_income) - cfo, total_assets)",
    "lvgi": "div(div(total_liabilities, total_assets), lag(div(total_liabilities, total_assets), 1))",
}
COMPONENTS = tuple(COMPONENT_FORMULAS)
#: extra `ratios` metric ids written by ratios_kpis (not in the sector_kpis registry: never scored)
COMPONENT_METRIC_IDS = tuple(f"beneish_{k}" for k in COMPONENTS)
DERIVED_METRIC_IDS = ("beneish_m_growth_adj", "beneish_red_flags", "beneish_flag_adj")
METRIC_UNITS = {**{m: "x" for m in COMPONENT_METRIC_IDS}, "beneish_m_growth_adj": "score", "beneish_red_flags": "count",
                "beneish_flag_adj": "score"}


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def m_score(comp: Mapping[str, Any], neutral_growth: bool = False) -> float | None:
    """Textbook M from the eight indices (missing index -> 1.0, TATA -> 0; > 2 missing or SGI missing -> None)."""
    vals = {k: _num(comp.get(k)) for k in COMPONENTS}
    if vals["sgi"] is None or sum(v is None for v in vals.values()) > 2:
        return None
    m = CONST
    for k, w in WEIGHTS.items():
        v = vals[k]
        if neutral_growth and k == GROWTH_INDEX:
            v = 1.0
        m += w * (v if v is not None else (0.0 if k == "tata" else 1.0))
    return m


def red_flags(comp: Mapping[str, Any]) -> list[str]:
    """Non-growth indices beyond the manipulator-sample mean."""
    out = []
    for k, mean in MANIPULATOR_MEANS.items():
        if k == GROWTH_INDEX:
            continue
        v = _num(comp.get(k))
        if v is not None and v > mean:
            out.append(k)
    return out


def contributions(comp: Mapping[str, Any]) -> dict[str, float | None]:
    """Contribution of each index to M relative to a neutral firm (index 1.0, TATA 0): w * (x - neutral)."""
    out: dict[str, float | None] = {}
    for k, w in WEIGHTS.items():
        v = _num(comp.get(k))
        out[k] = None if v is None else round(w * (v - (0.0 if k == "tata" else 1.0)), 4)
    return out


def corroborating(signals: Mapping[str, Any] | None) -> list[str]:
    """Outside-the-model warning signs: piotroski_f, cfo_to_net_income (+ net_margin > 0), altman_z2."""
    sig = signals or {}
    out = []
    pf = _num(sig.get("piotroski_f"))
    if pf is not None and pf <= CORROBORATION["piotroski_f_max"]:
        out.append("piotroski_low")
    cn, nm = _num(sig.get("cfo_to_net_income")), _num(sig.get("net_margin"))
    if cn is not None and cn < CORROBORATION["cfo_to_net_income_min"] and (nm is None or nm > 0):
        out.append("weak_cash_conversion")
    z2 = _num(sig.get("altman_z2"))
    if z2 is not None and z2 < CORROBORATION["altman_z2_distress"]:
        out.append("altman_distress")
    return out


def assess(comp: Mapping[str, Any], *, sector_id: str | None = None, is_financial: bool | None = None,
           m_reported: float | None = None, signals: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Recalibrated screen for one issuer-period. `comp` holds the eight indices (keys dsri..lvgi); `m_reported`
    (the stored beneish_m) is used when the indices are unavailable (then only the textbook level is known);
    `signals` = {piotroski_f, cfo_to_net_income, net_margin, altman_z2} for the corroboration step."""
    applicable = not (bool(is_financial) or (sector_id in EXCLUDED_SECTORS))
    have = any(_num(comp.get(k)) is not None for k in COMPONENTS)
    m = m_score(comp) if have else None
    if m is None:
        m = _num(m_reported)
    base: dict[str, Any] = {"m_score": None if m is None else round(m, 4), "threshold": THRESHOLD,
                            "model_based": True, "method": METHOD, "applicable": applicable,
                            "components": {k: (None if _num(comp.get(k)) is None else round(_num(comp.get(k)), 4))
                                           for k in COMPONENTS},
                            "manipulator_means": MANIPULATOR_MEANS}
    if not applicable:
        return {**base, "textbook_flag": None, "flag": None, "level": "not_applicable", "m_score_growth_adj": None,
                "red_flags": [], "corroboration": [], "growth_driven": None, "drivers": []}
    if m is None:
        return {**base, "textbook_flag": None, "flag": None, "level": "none", "m_score_growth_adj": None,
                "red_flags": [], "corroboration": [], "growth_driven": None, "drivers": []}
    textbook = m > THRESHOLD
    mg = m_score(comp, neutral_growth=True) if have else None
    rf = red_flags(comp) if have else []
    cor = corroborating(signals)
    if have:
        flag = textbook and mg is not None and mg > THRESHOLD and len(rf) >= MIN_RED_FLAGS and bool(cor)
    else:
        flag = False  # textbook score only (no indices stored yet): never escalate
    contrib = contributions(comp) if have else {}
    drivers = [k for k, v in sorted(contrib.items(), key=lambda kv: -(kv[1] or 0.0)) if v is not None and v > 0.05][:3]
    growth_driven = bool(textbook and mg is not None and mg <= THRESHOLD)
    level = "flag" if flag else ("watch" if textbook else "none")
    return {**base, "textbook_flag": textbook, "flag": flag, "level": level,
            "m_score_growth_adj": None if mg is None else round(mg, 4), "red_flags": rf, "corroboration": cor,
            "growth_driven": growth_driven,
            "contributions": contrib, "drivers": drivers}


# ------------------------------------------------------------------------------------------------ Polars (ratios job)


def polars_columns(frame: Any) -> Any:
    """Add `beneish_<k>` index columns, `beneish_m` (textbook), `beneish_m_growth_adj`, `beneish_red_flags` and
    `beneish_flag_adj` (1.0 / 0.0) to a vintage panel frame that already has the formula inputs, `is_financial`
    and `sector_id`. Uses app.analytics.ratios._ev (formula engine on narrow projections)."""
    import polars as pl  # noqa: PLC0415

    from app.analytics.ratios import _ev  # noqa: PLC0415

    for k, e in COMPONENT_FORMULAS.items():
        frame = frame.with_columns(_ev(frame, e, k).alias(f"beneish_{k}"))
    cols = {k: pl.col(f"beneish_{k}") for k in COMPONENTS}
    miss = pl.sum_horizontal([c.is_null().cast(pl.Int32) for c in cols.values()])
    sec = pl.col("sector_id") if "sector_id" in frame.columns else pl.lit(None, pl.Utf8)
    # textbook beneish_m keeps its historical scope (non-financials); the recalibrated screen also skips REITs
    ok = ~pl.col("is_financial").fill_null(False) & (miss <= 2) & cols["sgi"].is_not_null()
    ok_adj = ok & ~sec.is_in(list(EXCLUDED_SECTORS)).fill_null(False)

    def score(neutral_growth: bool) -> pl.Expr:
        m = pl.lit(CONST)
        for k, w in WEIGHTS.items():
            v = pl.lit(1.0) if (neutral_growth and k == GROWTH_INDEX) else cols[k].fill_null(0.0 if k == "tata" else 1.0)
            m = m + w * v
        return pl.when(ok_adj if neutral_growth else ok).then(m).otherwise(None)

    rf = pl.sum_horizontal([(cols[k] > mean).fill_null(False).cast(pl.Int32)
                            for k, mean in MANIPULATOR_MEANS.items() if k != GROWTH_INDEX])
    frame = frame.with_columns(score(False).alias("beneish_m"), score(True).alias("beneish_m_growth_adj"),
                               pl.when(ok_adj).then(rf.cast(pl.Float64)).otherwise(None).alias("beneish_red_flags"),
                               (miss == 0).alias("__c_beneish_m"))
    def col(c: str) -> pl.Expr:
        return pl.col(c).cast(pl.Float64) if c in frame.columns else pl.lit(None, pl.Float64)

    cor = ((col("piotroski_f") <= CORROBORATION["piotroski_f_max"]).fill_null(False)
           | ((col("cfo_to_net_income") < CORROBORATION["cfo_to_net_income_min"])
              & (col("net_margin").is_null() | (col("net_margin") > 0))).fill_null(False)
           | (col("altman_z2") < CORROBORATION["altman_z2_distress"]).fill_null(False))
    flag = ((pl.col("beneish_m") > THRESHOLD) & (pl.col("beneish_m_growth_adj") > THRESHOLD)
            & (pl.col("beneish_red_flags") >= MIN_RED_FLAGS) & cor)
    frame = frame.with_columns(pl.when(ok_adj).then(flag.cast(pl.Float64)).otherwise(None).alias("beneish_flag_adj"))
    for m in (*COMPONENT_METRIC_IDS, *DERIVED_METRIC_IDS):
        frame = frame.with_columns((miss == 0).alias(f"__c_{m}"))
    return frame


__all__ = ["WEIGHTS", "CONST", "THRESHOLD", "MANIPULATOR_MEANS", "COMPONENTS", "COMPONENT_FORMULAS",
           "COMPONENT_METRIC_IDS", "DERIVED_METRIC_IDS", "METRIC_UNITS", "EXCLUDED_SECTORS", "METHOD", "CORROBORATION",
           "m_score", "red_flags", "corroborating", "contributions", "assess", "polars_columns"]
