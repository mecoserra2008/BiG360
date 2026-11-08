"""Backtesting lab: strategy specification (pydantic) and the normalized panel *plan* (owner: backtest).

    spec = BacktestSpec.model_validate({...})
    plan, warnings = make_plan(spec)        # JSON plan executed by app.backtest._panel.build

The spec is declarative; its normalized JSON (model_dump(mode="json")) is what the run engine stores, hashes
(spec_hash, without name / description / timeout) and embeds in exported scripts.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.modeling._core import SOURCES as STUDIO_SOURCES
from app.modeling.datasets import Transform

from ._panel import EXTRA_SOURCES, PRICE_FIELDS

NAME_RX = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
FACTORS = ("quality", "profitability", "growth", "value", "balance_sheet", "momentum", "insider", "sentiment", "macro",
           "moat")
PROFILES = ("balanced", "growth", "momentum", "quality_value", "defensive")
RESERVED = {"date", "entity_id", "entity_label", "entity_name", "issuer_id", "sector_id", "region", "country",
            "signal", "eligible", "member", "delisted", "mcap", "vol", "adv_usd", "group_sector", "group_country",
            "currency", "asset_class"} | set(PRICE_FIELDS)
STUDIO_SIGNAL_SOURCES = ("factor_scores", "composite_scores", "valuation", "ratios", "fundamentals", "insider",
                         "sentiment", "technicals", "prices", "tech_factors")
ASSET_METRICS = ("price", "trend_signal", "mom_1m", "mom_3m", "mom_12m", "mom_12_1", "rv_1m", "rv_3m", "iv",
                 "iv_rv_ratio", "carry", "carry_curve", "carry_12m", "carry_fundamental", "curve_slope", "dividend_yield",
                 "funding_rate", "value", "real_rate_diff", "z_mom_12m", "z_carry", "z_value", "z_rv", "z_iv_rv",
                 "z_curve_slope", "cot_index_3y", "cot_z_3y", "cot_net_pct_oi")
ASSET_CLASSES = ("commodity", "fx", "equity_index", "rates", "credit", "crypto")
EVENT_TRIGGERS = ("insider_cluster", "earnings_surprise", "cftc_extreme", "macro_surprise", "confluence_touch",
                  "signal_threshold", "calibrated_band_breakout", "peer_breakaway", "meta_setups")
#: experimental conformal triggers / stop mode (failed or neutral in docs/CONFORMAL_VALIDATION.md; never promoted)
EXPERIMENTAL_TRIGGERS = ("calibrated_band_breakout", "peer_breakaway")
REGIME_TYPES = ("cycle_quadrant", "hmm_regime", "rate_cycle", "inflation_regime")
FREQ_FFILL = {"D": 25, "W": 5, "M": 1}
MAX_SWEEP = 64


class SpecError(ValueError):
    """Invalid backtest specification (HTTP 422)."""


class ScreenRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(description="screener field id evaluated point-in-time (see GET /backtest/catalog)")
    op: Literal["eq", "neq", "gt", "gte", "lt", "lte", "between", "in", "not_in", "is_null", "not_null"]
    value: Any = None
    value2: float | None = None


class UniverseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["securities", "assets"] = "securities"
    universe_id: str | None = Field(default="mvp_us", description="universe_membership id (mvp_us, mvp_global, full, eu_broad)")
    regions: list[str] = Field(default_factory=list)
    countries: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    security_ids: list[str] = Field(default_factory=list)
    asset_ids: list[str] = Field(default_factory=list, description="assets universe: cross-asset asset ids (CL, SPX, EURUSD, UST10Y, ...)")
    asset_classes: list[str] = Field(default_factory=list)
    include_delisted: bool | None = Field(default=None, description="add inactive US names while they traded (default: on for "
                                          "universe full / region-only universes, off for curated mvp_* lists)")
    min_market_cap_usd: float | None = Field(default=None, ge=0)
    min_adv_usd: float | None = Field(default=None, ge=0)
    screen: list[ScreenRule] = Field(default_factory=list, max_length=30)
    max_entities: int = Field(default=3000, ge=1, le=6000)
    primary_only: bool = True


class SignalVar(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    source: str = Field(description="factor_scores | composite_scores | valuation | ratios | fundamentals | insider | sentiment | "
                        "technicals | prices | tech_factors | price | ml | pead | event_exposure | studio_run | cross_asset | cftc | "
                        "bql (field = a BQL-lite expression, evaluated point-in-time per rebalance)")
    field: str
    transforms: list[Transform] = Field(default_factory=list, description="Studio transforms (securities, Studio sources)")
    options: dict[str, Any] = Field(default_factory=dict)
    max_age_days: int | None = Field(default=None, ge=1, le=3650, description="extra sources: drop values older than this")
    weight: float = 1.0
    higher_is_better: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RX.match(v) or v in RESERVED or v.startswith(("f_", "s_")):
            raise ValueError("signal variable names: identifier, <= 41 chars, not reserved, no f_ / s_ prefix")
        return v


class SignalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variables: list[SignalVar] = Field(min_length=1, max_length=12)
    combine: Literal["single", "zscore", "rank", "formula"] = "single"
    formula: str | None = Field(default=None, description="formula over variable names (app.normalize.formula syntax; no panel functions)")
    higher_is_better: bool = True
    lag_periods: int = Field(default=0, ge=0, le=24, description="extra signal lag in rebalance periods")

    @model_validator(mode="after")
    def _check(self) -> SignalSpec:
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError("signal variable names must be unique")
        if self.combine == "formula":
            if not self.formula:
                raise ValueError("combine=formula needs a formula")
            from app.normalize.formula import (
                FormulaError,
                dependencies,
                uses_panel_functions,
                validate_expr,
            )

            try:
                validate_expr(self.formula)
            except FormulaError as e:
                raise ValueError(f"formula: {e}") from e
            if uses_panel_functions(self.formula):
                raise ValueError("formula: panel functions (lag, avg, growth_yoy, cagr, ...) are not allowed")
            unknown = dependencies(self.formula) - set(names)
            if unknown:
                raise ValueError(f"formula references unknown variables {sorted(unknown)}")
        return self


class RegimeFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regime_type: Literal["cycle_quadrant", "hmm_regime", "rate_cycle", "inflation_regime"]
    scope: str = "US"
    states: list[str] = Field(min_length=1)
    action: Literal["cash", "half"] = "cash"


class Construction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rebalance: Literal["D", "W", "M"] = "M"
    side: Literal["long_only", "long_short"] = "long_only"
    selection: Literal["quantile", "top_n", "all"] = "quantile"
    quantiles: int = Field(default=5, ge=2, le=20)
    top_n: int | None = Field(default=None, ge=1, le=2000)
    weighting: Literal["equal", "cap", "signal", "inverse_vol", "risk_parity"] = "equal"
    vol_lookback_days: int = Field(default=63, ge=20, le=504)
    neutralize: Literal["none", "sector", "country"] = "none"
    max_weight: float | None = Field(default=None, gt=0, le=1)
    max_turnover: float | None = Field(default=None, gt=0, le=2, description="one-way turnover cap per rebalance (fraction of NAV)")
    holding_periods: int = Field(default=1, ge=1, le=24, description="overlapping tranches (Jegadeesh-Titman): each rebalance re-forms 1/K of the book")
    gross: float | None = Field(default=None, gt=0, le=4, description="gross exposure (default 1 long-only, 2 long-short = 100/100)")
    execution_lag_days: int = Field(default=1, ge=0, le=10)
    min_names: int = Field(default=5, ge=1, le=500)
    regime_filter: RegimeFilter | None = None


class EventSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger: Literal["insider_cluster", "earnings_surprise", "cftc_extreme", "macro_surprise", "confluence_touch",
                     "signal_threshold", "calibrated_band_breakout", "peer_breakaway", "meta_setups"]
    params: dict[str, Any] = Field(default_factory=dict)
    hold_days: int = Field(default=20, ge=1, le=756)
    stop_loss: float | None = Field(default=None, gt=0, le=1)
    take_profit: float | None = Field(default=None, gt=0, le=10)
    stop_mode: Literal["fixed", "calibrated_band"] = Field(
        default="fixed", description="fixed = stop_loss / take_profit fractions; calibrated_band (EXPERIMENTAL) = per-trade "
        "stop at the calibrated band edge of band_level / band_horizon emitted on the event date")
    band_level: Literal[80, 90, 95] = 90
    band_horizon: Literal[1, 5, 10, 20] = 20
    band_target: bool = Field(default=False, description="calibrated_band: also take profit at the opposite band edge")
    max_positions: int = Field(default=20, ge=1, le=500)
    position_weight: float | None = Field(default=None, gt=0, le=1)
    allow_short: bool = False
    entry_lag_days: int = Field(default=1, ge=0, le=10)


class SpreadBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_adv_usd: float = Field(ge=0)
    half_spread_bps: float = Field(ge=0, le=1000)


class Costs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    commission_bps: float = Field(default=1.0, ge=0, le=100)
    spread_model: Literal["adv_buckets", "fixed"] = "adv_buckets"
    fixed_spread_bps: float = Field(default=5.0, ge=0, le=500)
    spread_buckets: list[SpreadBucket] | None = None
    impact_coef: float = Field(default=0.1, ge=0, le=5, description="square-root impact: coef x daily vol x sqrt(trade / ADV)")
    capital_usd: float = Field(default=1.0e7, gt=0, le=1e12)
    borrow_bps: float = Field(default=50.0, ge=0, le=5000)
    htb_borrow_bps: float = Field(default=300.0, ge=0, le=10000)
    htb_adv_usd: float = Field(default=5.0e6, ge=0)
    fx_bps: float = Field(default=2.0, ge=0, le=100)
    currency: Literal["USD", "local"] = "USD"


class Period(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: date | None = None
    end: date | None = None
    oos_start: date | None = Field(default=None, description="mandatory out-of-sample holdout start (default: last oos_fraction)")
    oos_fraction: float = Field(default=0.3, ge=0.1, le=0.6)
    benchmark: str | None = Field(default=None, description="security id (ETF:SPY, IDX:^GSPC, ...), universe_ew or none; "
                                  "default ETF:SPY (US) / ETF:ACWI (global) / universe_ew (assets)")
    rf: Literal["cash", "zero"] = "cash"


class Robustness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    permutations: int = Field(default=0, ge=0, le=200, description="signal-permutation test runs (heavy extra, "
                              "opt-in since S1.4: 20 is a typical choice; presets set 20)")
    stale_check: bool = True
    sweep: dict[str, list[Any]] = Field(default_factory=dict, description="dotted param path -> values (walk-forward grid)")
    wf_folds: int = Field(default=4, ge=2, le=12)
    cscv_blocks: int = Field(default=10, ge=4, le=16)
    attribution: bool = True
    deciles: int = Field(default=10, ge=3, le=20)

    @model_validator(mode="after")
    def _grid(self) -> Robustness:
        n = 1
        for k, v in self.sweep.items():
            if not v:
                raise ValueError(f"sweep {k}: empty value list")
            if k.split(".")[0] not in ("construction", "signal", "event", "costs"):
                raise ValueError(f"sweep {k}: only construction.*, signal.lag_periods, event.*, costs.* can be swept")
            n *= len(v)
        if n > MAX_SWEEP:
            raise ValueError(f"sweep grid has {n} combinations (> {MAX_SWEEP})")
        return self


class BacktestSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="Backtest", min_length=1, max_length=200)
    description: str | None = None
    mode: Literal["rank", "event"] = "rank"
    universe: UniverseSpec = Field(default_factory=UniverseSpec)
    signal: SignalSpec
    construction: Construction = Field(default_factory=Construction)
    event: EventSpec | None = None
    costs: Costs = Field(default_factory=Costs)
    period: Period = Field(default_factory=Period)
    robustness: Robustness = Field(default_factory=Robustness)
    delisting_return: float = Field(default=0.0, ge=-1, le=1, description="return applied when a held name stops trading")
    seed: int = 42
    timeout_s: int = Field(default=900, ge=10, le=3600)

    @model_validator(mode="after")
    def _check(self) -> BacktestSpec:
        if self.mode == "event" and self.event is None:
            raise ValueError("mode=event needs an event block")
        if self.period.start and self.period.end and self.period.start >= self.period.end:
            raise ValueError("period.start must be before period.end")
        if self.period.oos_start and ((self.period.start and self.period.oos_start <= self.period.start)
                                      or (self.period.end and self.period.oos_start >= self.period.end)):
            raise ValueError("period.oos_start must fall inside the period")
        assets = self.universe.kind == "assets"
        for v in self.signal.variables:
            ok = ({"cross_asset", "cftc", "price"} if assets else
                  set(STUDIO_SIGNAL_SOURCES) | {"price"} | set(EXTRA_SOURCES))
            if v.source not in ok:
                raise ValueError(f"signal {v.name}: source {v.source!r} not available for a {self.universe.kind} universe "
                                 f"({', '.join(sorted(ok))})")
            if v.source in ("cross_asset", "cftc") and v.field not in ASSET_METRICS:
                raise ValueError(f"signal {v.name}: unknown cross-asset metric {v.field!r}")
            if v.source == "cftc" and not v.field.startswith("cot_"):
                raise ValueError(f"signal {v.name}: CFTC fields are cot_index_3y, cot_z_3y, cot_net_pct_oi")
            if v.source == "price" and v.field not in PRICE_FIELDS:
                raise ValueError(f"signal {v.name}: unknown price field {v.field!r} ({', '.join(PRICE_FIELDS)})")
            if v.transforms and v.source not in STUDIO_SOURCES:
                raise ValueError(f"signal {v.name}: transforms apply to Studio sources only")
            if any(t.op in ("lead", "fwd_sum", "fwd_compound") for t in v.transforms):
                raise ValueError(f"signal {v.name}: look-ahead transform ({', '.join(t.op for t in v.transforms)}) "
                                 "is not allowed in a backtest signal")
        if self.event and self.event.trigger == "cftc_extreme" and not assets:
            raise ValueError("cftc_extreme events need an assets universe")
        return self

    def normalized(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def spec_hash(self) -> str:
        d = self.normalized()
        for k in ("name", "description", "timeout_s"):
            d.pop(k, None)
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------------------------------------ screen field map


def screen_field_map() -> dict[str, dict[str, Any]]:
    """Screener field id -> how it is evaluated point-in-time in a backtest:
    {kind: studio|price|attr|derived|extra, source, field, column, label}. Fields absent here have no PIT history."""
    m: dict[str, dict[str, Any]] = {}
    for f in FACTORS:
        m[f] = {"kind": "studio", "source": "factor_scores", "field": f, "label": f"{f} factor score"}
    m["composite"] = {"kind": "studio", "source": "composite_scores", "field": "balanced", "label": "Composite (balanced)"}
    m["grade"] = {"kind": "derived", "column": "_grade", "needs": "composite", "label": "Grade (quintile of composite)"}
    m["rank"] = {"kind": "derived", "column": "_rank", "needs": "composite", "label": "Rank by composite"}
    for c in ("market_cap_usd", "enterprise_value_usd", "pe_ttm", "earnings_yield", "pb", "ps_ttm", "ev_sales", "ev_ebitda",
              "ev_ebit", "fcf_yield", "dividend_yield", "shareholder_yield", "p_ffo", "p_tbv", "gp_to_ev"):
        m[c] = {"kind": "studio", "source": "valuation", "field": c, "label": c.replace("_", " ")}
    m["size_bucket"] = {"kind": "derived", "column": "_size_bucket", "needs": "mcap", "label": "Size bucket (PIT market cap)"}
    for c in ("sector_id", "region", "country", "currency"):
        m[c] = {"kind": "attr", "column": c, "label": c}
    for f, lab in (("chg_1m", "ret_1m"), ("chg_3m", "ret_3m"), ("chg_6m", "ret_6m"), ("ret_12m", "ret_12m"),
                   ("mom_12_1", "mom_12_1"), ("mom_6_1", "mom_6_1"), ("high_52w_proximity", "high_52w_proximity"),
                   ("trend_200d", "trend_200d"), ("vol_1y", "vol_1y"), ("adv_usd_20d", "adv_usd_20d"),
                   ("beta_1y", "beta_1y"), ("above_200d", "above_200d")):
        m[f] = {"kind": "price", "field": lab, "column": lab, "label": PRICE_FIELDS[lab]}
    m["insider_signal"] = {"kind": "studio", "source": "insider", "field": "signal_score", "label": "Insider signal"}
    m["insider_cluster_buy"] = {"kind": "studio", "source": "insider", "field": "cluster_buy", "label": "Insider cluster buy"}
    m["insider_net_buy_90d_usd"] = {"kind": "studio", "source": "insider", "field": "net_buy_value_usd_90d",
                                    "label": "Insider net buying 90d"}
    m["sentiment_score"] = {"kind": "studio", "source": "sentiment", "field": "sentiment_score", "label": "Sentiment score"}
    m["attention_z"] = {"kind": "studio", "source": "sentiment", "field": "news_count_z", "label": "Media attention z"}
    for h in ("1M", "3M", "6M"):
        hl = h.lower()
        m[f"ml_rank_pct_{hl}"] = {"kind": "extra", "source": "ml", "field": f"{h}:rank_pct", "label": f"ML rank pct {h}"}
        m[f"ml_decile_{hl}"] = {"kind": "extra", "source": "ml", "field": f"{h}:decile", "label": f"ML decile {h}"}
        m[f"ml_expected_excess_{hl}"] = {"kind": "extra", "source": "ml", "field": f"{h}:expected_excess_return",
                                         "label": f"ML expected excess {h}"}
    m["revenue_growth"] = {"kind": "studio", "source": "ratios", "field": "revenue_growth_yoy", "label": "Revenue growth YoY"}
    m["net_debt_ebitda"] = {"kind": "studio", "source": "ratios", "field": "net_debt_to_ebitda", "label": "Net debt / EBITDA"}
    return m


RATIO_SCREEN_PREFIX = "ratio:"  # any ratios metric id is accepted as "<metric_id>" when known to the ratios table


def _studio_var(name: str, source: str, field: str, transforms: list[dict[str, Any]] | None = None,
                options: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.modeling.datasets import Variable  # noqa: PLC0415

    return Variable.model_validate({"name": name, "source": source, "field": field, "transforms": transforms or [],
                                    "options": options or None}).model_dump(mode="json")


def ratio_metric_ids() -> set[str]:
    try:
        from app.api.routers._common import metric_meta  # noqa: PLC0415

        return {m["id"] for m in metric_meta() if m["source"] == "ratios"}
    except Exception:  # noqa: BLE001
        return set()


# ------------------------------------------------------------------------------------------------ plan


def default_benchmark(spec: BacktestSpec) -> str:
    if spec.period.benchmark:
        return spec.period.benchmark
    if spec.universe.kind == "assets":
        return "universe_ew"
    u = spec.universe
    us_only = (u.universe_id == "mvp_us") or (u.regions == ["US"]) or (u.countries == ["US"])
    return "ETF:SPY" if us_only else "ETF:ACWI"


def make_plan(spec: BacktestSpec) -> tuple[dict[str, Any], list[str]]:
    """BacktestSpec -> (plan for app.backtest._panel.build, warnings)."""
    warns: list[str] = []
    u, sig, cons = spec.universe, spec.signal, spec.construction
    freq = cons.rebalance
    plan: dict[str, Any] = {
        "kind": u.kind, "frequency": freq, "start": spec.period.start.isoformat() if spec.period.start else None,
        "end": spec.period.end.isoformat() if spec.period.end else None, "currency": spec.costs.currency,
        "benchmark": default_benchmark(spec), "rf": spec.period.rf, "min_adv_usd": u.min_adv_usd,
        "min_market_cap_usd": u.min_market_cap_usd, "screen": [], "extra": [], "price_fields": [],
        "signal": {"combine": sig.combine, "formula": sig.formula, "higher_is_better": sig.higher_is_better,
                   "lag_periods": sig.lag_periods, "vars": []},
        "regime": cons.regime_filter.model_dump(mode="json") if cons.regime_filter else None,
        "events": {"trigger": spec.event.trigger, "params": spec.event.params,
                   "base_weight": spec.event.position_weight or (1.0 / spec.event.max_positions)}
        if (spec.mode == "event" and spec.event) else None,
    }
    if plan["events"] is not None and spec.event and spec.event.stop_mode == "calibrated_band":
        plan["events"]["band_stops"] = {"level": spec.event.band_level, "horizon": spec.event.band_horizon,
                                        "target": spec.event.band_target}
        warns.append("stop mode calibrated band: EXPERIMENTAL - band stops beat ATR stops but no stop beat 'no stop' "
                     "in validation (docs/CONFORMAL_VALIDATION.md)")
    if plan["events"] is not None and spec.event and spec.event.trigger in EXPERIMENTAL_TRIGGERS:
        warns.append(f"event trigger {spec.event.trigger}: EXPERIMENTAL - failed in validation "
                     "(docs/CONFORMAL_VALIDATION.md); model-based, not a recommended strategy")
    price_fields: list[str] = []
    if u.kind == "assets":
        metrics = []
        for v in sig.variables:
            if v.source == "price":
                price_fields.append(v.field)
                col = v.field
            else:
                metrics.append(v.field)
                col = v.field
            plan["signal"]["vars"].append({"name": col, "weight": v.weight, "higher_is_better": v.higher_is_better})
        if sig.combine == "formula":
            alias = {v.name: (v.field) for v in sig.variables}
            expr = sig.formula or ""
            for a, b in sorted(alias.items(), key=lambda x: -len(x[0])):
                expr = re.sub(rf"\b{re.escape(a)}\b", b, expr)
            plan["signal"]["formula"] = expr
        if u.screen:
            for r in u.screen:
                if r.field in ASSET_METRICS:
                    metrics.append(r.field)
                    plan["screen"].append({"field": r.field, "column": r.field, "op": r.op, "value": r.value, "value2": r.value2})
                elif r.field in ("asset_class",):
                    plan["screen"].append({"field": r.field, "column": "sector_id", "op": r.op, "value": r.value, "value2": r.value2})
                else:
                    warns.append(f"screen rule on {r.field} ignored for an assets universe")
        plan["assets"] = {"asset_ids": u.asset_ids, "asset_classes": u.asset_classes, "metrics": sorted(set(metrics))}
        plan["price_fields"] = sorted(set(price_fields) | {"mom_12_1"})
        return plan, warns

    variables: list[dict[str, Any]] = []
    optional: list[str] = []
    neutral: list[str] = []  # screen variables: sampled on the signal grid, never narrow it
    for v in sig.variables:
        if v.source in STUDIO_SOURCES:
            variables.append(_studio_var(v.name, v.source, v.field, [t.model_dump(mode="json") for t in v.transforms],
                                         v.options or None))
        elif v.source == "price":
            price_fields.append(v.field)
        else:
            plan["extra"].append({"name": v.name, "source": v.source, "field": v.field, "options": v.options,
                                  "max_age_days": v.max_age_days or {"ml": 45, "pead": 63, "event_exposure": 14,
                                                                     "studio_run": 45, "bql": 45}[v.source]})
        col = v.field if v.source == "price" else v.name
        plan["signal"]["vars"].append({"name": col, "weight": v.weight, "higher_is_better": v.higher_is_better})
    if sig.combine == "formula":
        alias = {v.name: v.field for v in sig.variables if v.source == "price"}
        expr = sig.formula or ""
        for a, b in sorted(alias.items(), key=lambda x: -len(x[0])):
            expr = re.sub(rf"\b{re.escape(a)}\b", b, expr)
        plan["signal"]["formula"] = expr
    # market cap (cap weighting, size screens, SMB attribution) - optional
    variables.append(_studio_var("s_mcap", "valuation", "market_cap_usd"))
    optional.append("s_mcap")
    plan["mcap_var"] = "s_mcap"
    fmap = screen_field_map()
    ratios = None
    for r in u.screen:
        f = fmap.get(r.field)
        if f is None:
            ratios = ratios if ratios is not None else ratio_metric_ids()
            if r.field in ratios:
                f = {"kind": "studio", "source": "ratios", "field": r.field}
        if f is None:
            warns.append(f"screen rule on {r.field} dropped: no point-in-time history for this field")
            continue
        col = None
        if f["kind"] == "studio":
            col = f"s_{r.field}"
            if all(x["name"] != col for x in variables):
                variables.append(_studio_var(col, f["source"], f["field"]))
                neutral.append(col)
        elif f["kind"] == "price":
            price_fields.append(f["field"])
            col = f["column"]
        elif f["kind"] == "attr":
            col = f["column"]
        elif f["kind"] == "extra":
            col = f"s_{r.field}"
            if all(x["name"] != col for x in plan["extra"]):
                plan["extra"].append({"name": col, "source": f["source"], "field": f["field"], "options": {},
                                      "max_age_days": 45})
        elif f["kind"] == "derived":
            col = f["column"]
            if f["needs"] == "composite" and all(x["name"] != "s_composite" for x in variables):
                variables.append(_studio_var("s_composite", "composite_scores", "balanced"))
                neutral.append("s_composite")
                plan["composite_var"] = "s_composite"
            elif f["needs"] == "composite":
                plan["composite_var"] = "s_composite"
        val = r.value
        if r.field == "insider_cluster_buy" and isinstance(val, bool):
            val = 1.0 if val else 0.0
        plan["screen"].append({"field": r.field, "column": col, "op": r.op, "value": val, "value2": r.value2})
    if spec.robustness.attribution:
        for f in ("value", "momentum", "quality", "growth", "profitability", "balance_sheet", "insider", "sentiment",
                  "macro", "moat"):
            n = f"f_{f}"
            variables.append(_studio_var(n, "factor_scores", f))
            optional.append(n)
        price_fields.append("mom_12_1")
    price_fields.append("adv_usd_20d")
    include_dl = u.include_delisted
    if include_dl is None:
        include_dl = (u.universe_id in (None, "full")) and not u.security_ids
    regions = u.regions or (["US"] if u.universe_id in ("mvp_us", None, "full") else [])
    plan["delisted"] = {"enabled": bool(include_dl and ("US" in regions or u.countries == ["US"])), "regions": ["US"]}
    ent: dict[str, Any] = {"primary_only": u.primary_only}
    if u.security_ids:
        ent["security_ids"] = u.security_ids
    if u.universe_id and not u.security_ids:
        ent["universe"] = u.universe_id
    for k, src in (("regions", u.regions), ("countries", u.countries), ("sectors", u.sectors)):
        if src:
            ent[k] = src
    if all(v["name"] in optional or v["name"] in neutral for v in variables):
        # signal from price / extra sources only: the daily closes define the universe x rebalance grid
        variables.insert(0, _studio_var("s_px", "prices", "close"))
    from app.modeling.datasets import DatasetSpec  # noqa: PLC0415

    ds = DatasetSpec.model_validate({
        "kind": "panel", "frequency": freq, "start": spec.period.start, "end": spec.period.end, "entities": ent,
        "variables": variables, "missing": {"ffill_limit": FREQ_FFILL[freq]}, "pit_universe": True,
        "max_entities": u.max_entities, "max_rows": 20_000_000})
    plan["dataset"] = ds.normalized()
    plan["optional_vars"] = optional
    plan["grid_neutral_vars"] = neutral
    plan["price_fields"] = sorted(set(price_fields))
    return plan, warns


def plan_key(plan: dict[str, Any], snapshot: str) -> str:
    from app.modeling import _core  # noqa: PLC0415

    from . import _engine, _panel  # noqa: PLC0415

    raw = json.dumps({"plan": plan, "snapshot": snapshot, "panel": _panel.PANEL_VERSION, "engine": _engine.ENGINE_VERSION,
                      "core": _core.CORE_VERSION}, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()[:20]


def set_path(d: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Copy of the normalized spec dict with a dotted path set (used by parameter sweeps)."""
    out = json.loads(json.dumps(d))
    cur = out
    keys = path.split(".")
    for k in keys[:-1]:
        if cur.get(k) is None:
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value
    return out
