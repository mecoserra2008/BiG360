"""Backtesting lab catalog: signal sources and fields, screen fields, event triggers, regimes, benchmarks, presets,
with point-in-time coverage measured on the lake (cached per serving snapshot). Owner: backtest."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import duckdb

from app.core.config import Settings, get_settings

from ._panel import PRICE_FIELDS
from .runner import SURVIVORSHIP_CAVEAT, cache_dir, snapshot_id
from .spec import ASSET_CLASSES, ASSET_METRICS, FACTORS, PROFILES, screen_field_map

CATALOG_VERSION = "3"
_lock = threading.Lock()
_mem: dict[str, Any] = {}

F = dict[str, Any]


def _f(field: str, label: str, units: str | None = None, hib: bool | None = True, model_based: bool = False) -> F:
    return {"field": field, "label": label, "units": units, "higher_is_better": hib, "model_based": model_based}


SOURCES: list[dict[str, Any]] = [
    {"source": "factor_scores", "title": "Factor scores (0-100)", "universe": "securities", "table": "factor_scores",
     "date_col": "as_of_date", "pit": "monthly point-in-time history (scoring job, knowledge-dated inputs)",
     "fields": [_f(f, f"{f.replace('_', ' ')} factor", "score 0-100") for f in FACTORS]},
    {"source": "composite_scores", "title": "Composite scores by profile", "universe": "securities",
     "table": "composite_scores", "date_col": "as_of_date", "pit": "monthly point-in-time history",
     "fields": [_f(p, f"composite: {p.replace('_', ' ')}", "score 0-100") for p in PROFILES]},
    {"source": "ml", "title": "ML predictions (XGBoost, walk-forward OOS)", "universe": "securities",
     "table": "ml_predictions", "date_col": "date", "model_based": True,
     "pit": "expanding walk-forward out-of-sample predictions (purged, embargoed); production model xgboost_rank; "
            "options.model_type=ft_transformer is DEMOTED (failed the label-permutation test)",
     "fields": [_f(f"{h}:{c}", f"ML {h} {lab}", u, True, True) for h in ("1M", "3M", "6M")
                for c, lab, u in (("rank_pct", "rank pct", "0-1"), ("expected_excess_return", "expected excess return", "fraction"),
                                  ("score", "score", None), ("decile", "decile", "1-10"))]},
    {"source": "technicals", "title": "Technical score and components", "universe": "securities", "table": "tech_setups",
     "date_col": "as_of_date", "pit": "stored for the latest as-of date only (history accumulates from now on)",
     "fields": [_f("technical_score", "Technical score", "score 0-100"), _f("score_trend", "Trend component", "score"),
                _f("score_momentum", "Momentum component", "score"), _f("rsi14", "RSI 14", None, None),
                _f("reward_risk", "Reward / risk to target", "x")]},
    {"source": "tech_factors", "title": "Technical factors (evidence-based)", "universe": "securities", "table": "tech_factors",
     "date_col": "as_of_date", "pit": "daily, bars <= t (2004 -> for US / EU / UK / JP); percentiles within region x sector among "
            "liquid names; gate per factor in docs/TECHNICALS_V3_VALIDATION.md",
     "fields": [_f("hi52", "52-week-high proximity", "ratio"), _f("pct_hi52", "52-week-high proximity percentile", "0-100"),
                _f("mom", "12-1 month momentum", "log return"), _f("pct_mom", "12-1 momentum percentile", "0-100"),
                _f("rmom", "Residual momentum", "z"), _f("pct_rmom", "Residual momentum percentile", "0-100"),
                _f("strev", "1-month return (reversal: lower is better)", "log return", False),
                _f("pct_strev", "1-month return percentile (lower is better)", "0-100", False),
                _f("maxret", "MAX: largest daily return 1m (lower is better)", "fraction", False),
                _f("pct_maxret", "MAX percentile (lower is better)", "0-100", False),
                _f("ivol", "Idiosyncratic volatility (lower is better)", "annualised", False),
                _f("pct_ivol", "Idiosyncratic volatility percentile (lower is better)", "0-100", False),
                _f("indmom", "Industry momentum 6m", "log return"), _f("pct_indmom", "Industry momentum percentile", "0-100"),
                _f("composite", "Technical factor composite (gate-passing factors)", "0-100"),
                _f("composite_pct", "Technical factor composite percentile", "0-100")]},
    {"source": "insider", "title": "Insider signals", "universe": "securities", "table": "insider_signals",
     "date_col": "as_of_date", "pit": "Form 4 filing dates (knowledge-dated)",
     "fields": [_f("signal_score", "Insider signal score", "score", True, True), _f("net_buy_mcap_bps_90d", "Net buying 90d (bps of mcap)", "bps"),
                _f("n_buyers_90d", "Buyers 90d", "count"), _f("cluster_buy", "Cluster buy (0/1)", "flag"),
                _f("anomaly_score", "Anomaly score", None, True, True), _f("xgb_prob_up", "XGB P(up)", "prob", True, True)]},
    {"source": "sentiment", "title": "News sentiment / attention", "universe": "securities", "table": "sentiment_daily",
     "date_col": "date", "model_based": True, "pit": "daily, FinBERT / GDELT tone (model-based)",
     "fields": [_f("sentiment_score", "Sentiment score", "score", True, True), _f("sentiment_z", "Sentiment z", "z", True, True),
                _f("news_count_z", "Attention z", "z", None), _f("sentiment_momentum", "Sentiment momentum", None, True, True),
                _f("lazy_prices_pct", "Lazy prices similarity pct", "pct")]},
    {"source": "event_exposure", "title": "Geopolitical / event exposure", "universe": "securities",
     "table": "company_event_exposure", "date_col": "as_of", "model_based": True, "pit": "snapshot as-of dates (model-based)",
     "fields": [_f("exposure_score", "Event exposure score", None, False, True), _f("event_pressure", "Event pressure", None, False, True),
                _f("expected_impact", "Expected impact", "fraction", True, True)]},
    {"source": "pead", "title": "Earnings PEAD / model surprise", "universe": "securities", "table": "earnings_pead",
     "date_col": "signal_date", "model_based": True,
     "pit": "known on signal_date (reaction day + 1 / knowledge date); surprises vs MODEL-BASED expectations (SARIMA)",
     "fields": [_f("pead_score", "PEAD score", None, True, True), _f("sue", "Standardised unexpected EPS (model)", "z", True, True),
                _f("ear_z", "Earnings announcement return z", "z"), _f("rev_surprise_z", "Revenue surprise z (model)", "z", True, True),
                _f("decile", "PEAD decile", "1-10")]},
    {"source": "valuation", "title": "Valuation multiples (daily)", "universe": "securities", "table": "valuation_daily",
     "date_col": "date", "pit": "price x PIT fundamentals (fundamentals_knowledge_date)",
     "fields": [_f("earnings_yield", "Earnings yield", "fraction"), _f("fcf_yield", "FCF yield", "fraction"),
                _f("pb", "P/B", "x", False), _f("ev_ebitda", "EV / EBITDA", "x", False), _f("dividend_yield", "Dividend yield", "fraction"),
                _f("shareholder_yield", "Shareholder yield", "fraction"), _f("gp_to_ev", "Gross profit / EV", "fraction"),
                _f("market_cap_usd", "Market cap (USD)", "USD", None)]},
    {"source": "ratios", "title": "Financial ratios (point-in-time)", "universe": "securities", "table": "ratios",
     "date_col": "knowledge_date", "pit": "as of knowledge_date (filing date); any ratios metric id is accepted",
     "fields": [_f("roic", "ROIC", "fraction"), _f("gross_profitability", "Gross profitability", "fraction"),
                _f("revenue_growth_yoy", "Revenue growth YoY", "fraction"), _f("debt_to_equity", "Debt / equity", "x", False),
                _f("piotroski_f", "Piotroski F", "score"), _f("sloan_accruals", "Sloan accruals", None, False),
                _f("share_count_change_yoy", "Share count change YoY", "fraction", False)]},
    {"source": "price", "title": "Price-derived (computed from daily prints <= t)", "universe": "both",
     "table": "prices_daily", "date_col": "date", "pit": "daily adjusted prints on or before the rebalance date",
     "fields": [_f(k, v, None, not (k.startswith("vol") or k == "beta_1y")) for k, v in PRICE_FIELDS.items()]},
    {"source": "studio_run", "title": "Model Studio run (out-of-fold predictions)", "universe": "securities",
     "table": None, "date_col": None, "model_based": True,
     "pit": "field = Studio run id of a panel tree_ensemble / regularized / lightgbm / stacking / ltr / mlp / rnn run; use cv.scheme=expanding (walk-forward) "
            "- other schemes train on future data and are flagged LOOK-AHEAD",
     "fields": []},
    {"source": "bql", "title": "BQL expression (cross-sectional, point-in-time per rebalance)", "universe": "securities",
     "table": None, "date_col": None,
     "pit": "field = a BQL-lite expression, e.g. cs_zscore(chg_pct(px, 3M)) - cs_zscore(pe_ttm): evaluated at every "
            "rebalance date with bars / knowledge dates <= the date; cross-sectional functions run over the universe; "
            "snapshot-only fields (no history) are rejected",
     "fields": []},
    {"source": "cross_asset", "title": "Cross-asset signals (assets universe)", "universe": "assets",
     "table": "cross_asset_signals", "date_col": "knowledge_date", "model_based": True,
     "pit": "as of knowledge_date; trend / curve shape / value / z metrics are model-based",
     "fields": [_f(m, m.replace("_", " "), None, None if m in ("price", "rv_1m", "rv_3m", "iv") else True,
                   m in ("trend_signal", "value") or m.startswith("z_")) for m in ASSET_METRICS if not m.startswith("cot_")]},
    {"source": "cftc", "title": "CFTC positioning via cross-asset join (assets universe)", "universe": "assets",
     "table": "cross_asset_signals", "date_col": "knowledge_date",
     "pit": "official CFTC COT, latest report released on or before t (stale > 28 days -> null)",
     "fields": [_f("cot_index_3y", "COT index 3y (0-1)", "0-1", False), _f("cot_z_3y", "COT net z 3y", "z", False),
                _f("cot_net_pct_oi", "Net % open interest", "fraction", False)]},
]

EVENTS: list[dict[str, Any]] = [
    {"trigger": "insider_cluster", "title": "Insider cluster buy", "universe": "securities", "table": "insider_signals",
     "date_col": "as_of_date", "params": {}, "model_based": False,
     "description": "first insider_signals snapshot with cluster_buy per name; long"},
    {"trigger": "earnings_surprise", "title": "Earnings model surprise", "universe": "securities", "table": "earnings_pead",
     "date_col": "signal_date", "params": {"field": "sue | pead_score | ear_z | rev_surprise_z", "threshold": 2.0},
     "model_based": True, "description": "|field| >= threshold on signal_date; direction = sign (shorts need allow_short)"},
    {"trigger": "cftc_extreme", "title": "CFTC positioning extreme", "universe": "assets", "table": "cross_asset_signals",
     "date_col": "knowledge_date", "params": {"high": 0.9, "low": 0.1, "mode": "contrarian | trend"}, "model_based": False,
     "description": "COT index (3y) crossing above high / below low; contrarian fades the crowd"},
    {"trigger": "macro_surprise", "title": "Macro surprise", "universe": "both", "table": "macro_surprises",
     "date_col": "release_date", "params": {"threshold": 1.5, "country": "US", "theme": "growth | inflation | labor",
                                            "series_ids": [], "targets": ["ETF:SPY"], "sign": "direct | inverse"},
     "model_based": True, "description": "|signed z| vs model-based expectations >= threshold: trade the targets"},
    {"trigger": "confluence_touch", "title": "Confluence-zone touch", "universe": "securities", "table": "confluence_zones",
     "date_col": "as_of_date", "params": {"max_distance_atr": 0.25}, "model_based": True,
     "description": "price inside / within x ATR of the top-ranked zone: long at support, short at resistance"},
    {"trigger": "signal_threshold", "title": "Signal threshold crossing", "universe": "both", "table": None,
     "date_col": None, "params": {"threshold": 0.0, "short_below": None}, "model_based": False,
     "description": "the combined signal crossing the threshold at a rebalance date"},
    {"trigger": "calibrated_band_breakout", "title": "Calibrated-band breakout (experimental)", "universe": "securities",
     "table": None, "date_col": None, "params": {"horizon": 5, "level": 90, "direction": "up | both"}, "model_based": True,
     "experimental": True, "validation": "FAILED in validation: not better than a naive N-day-high breakout "
                                         "(docs/CONFORMAL_VALIDATION.md)",
     "description": "the h-session move leaves the marginal conformal band emitted h sessions earlier (walk-forward "
                    "EWMA band on the panel prices); long (both: also short downside exits)"},
    {"trigger": "peer_breakaway", "title": "Peer break-away fade (experimental)", "universe": "securities",
     "table": "conformal_peer_scores", "date_col": "as_of_date", "params": {"z": 2.5, "source": "auto | walk_forward | live"},
     "model_based": True, "experimental": True,
     "validation": "FAILED in validation: negative Sharpe after costs, null vs a naive fade (docs/CONFORMAL_VALIDATION.md)",
     "description": "fade the member with |z_cond| >= z on days the peer group's joint score exceeded its 90% radius "
                    "(GCN-GRU walk-forward signals 2010-2026 or the live table); needs allow_short for shorts"},
    {"trigger": "meta_setups", "title": "Meta-labelled setups (Technicals v3)", "universe": "securities",
     "table": "tech_meta_setups", "date_col": "as_of_date",
     "params": {"horizon": 20, "mode": "all | take | sized", "source": "auto | walk_forward | live", "direction": "long | both"},
     "model_based": True, "experimental": True,
     "validation": "see docs/TECHNICALS_V3_VALIDATION.md (Meta-labelling): the run notes carry the current gate verdict",
     "description": "the v2 weekly trade setups (target T1 / stop at the invalidation per trade) - all, only those the "
                    "meta-labelling model takes, or taken and sized by its quarter-Kelly fraction (walk-forward OOS 2013-2026 "
                    "for the gate universe, or the live table)"},
]

BENCHMARKS = [("ETF:SPY", "S&P 500 (SPY, total return)"), ("ETF:ACWI", "MSCI ACWI (ACWI)"), ("ETF:VEA", "Developed ex-US (VEA)"),
              ("ETF:EEM", "Emerging markets (EEM)"), ("ETF:IWM", "Russell 2000 (IWM)"), ("ETF:QQQ", "Nasdaq 100 (QQQ)"),
              ("ETF:VGK", "Europe (VGK)"), ("ETF:EWJ", "Japan (EWJ)"), ("IDX:^GSPC", "S&P 500 price index"),
              ("universe_ew", "Universe equal weight"), ("none", "No benchmark")]

PRESETS: list[dict[str, Any]] = [
    {"id": "value_ls_us", "title": "US value long-short (quintiles, monthly)", "spec": {
        "name": "US value long-short", "universe": {"universe_id": "mvp_us"},
        "signal": {"variables": [{"name": "value", "source": "factor_scores", "field": "value"}]},
        "construction": {"side": "long_short", "quantiles": 5, "rebalance": "M"}}},
    {"id": "quality_momentum_lo", "title": "Quality + momentum long-only, sector neutral", "spec": {
        "name": "Quality + momentum", "universe": {"universe_id": "mvp_us"},
        "signal": {"combine": "zscore", "variables": [{"name": "quality", "source": "factor_scores", "field": "quality"},
                                                      {"name": "mom", "source": "price", "field": "mom_12_1"}]},
        "construction": {"side": "long_only", "quantiles": 5, "neutralize": "sector", "max_weight": 0.05}}},
    {"id": "ml_3m_top_decile", "title": "ML 3M top decile (walk-forward OOS)", "spec": {
        "name": "ML 3M top decile", "universe": {"universe_id": "mvp_us"},
        "signal": {"variables": [{"name": "ml", "source": "ml", "field": "3M:rank_pct"}]},
        "construction": {"side": "long_only", "quantiles": 10, "rebalance": "M", "holding_periods": 3}}},
    {"id": "low_vol_risk_parity", "title": "Low volatility, risk parity", "spec": {
        "name": "Low vol risk parity", "universe": {"universe_id": "mvp_us"},
        "signal": {"variables": [{"name": "lowvol", "source": "price", "field": "vol_1y", "higher_is_better": False}]},
        "construction": {"side": "long_only", "quantiles": 5, "weighting": "risk_parity"}}},
    {"id": "pead_events", "title": "Post-earnings drift (model surprise events)", "spec": {
        "name": "PEAD events", "mode": "event", "universe": {"universe_id": "mvp_us"},
        "signal": {"variables": [{"name": "sue", "source": "pead", "field": "sue"}]},
        "event": {"trigger": "earnings_surprise", "params": {"field": "sue", "threshold": 2.0}, "hold_days": 40,
                  "max_positions": 40, "allow_short": True}}},
    {"id": "insider_cluster", "title": "Insider cluster buys (60-day hold)", "spec": {
        "name": "Insider cluster buys", "mode": "event", "universe": {"universe_id": "full"},
        "signal": {"variables": [{"name": "ins", "source": "insider", "field": "signal_score"}]},
        "event": {"trigger": "insider_cluster", "hold_days": 60, "max_positions": 30, "stop_loss": 0.2}}},
    {"id": "xa_trend", "title": "Cross-asset trend (weekly, long-short)", "spec": {
        "name": "Cross-asset trend", "universe": {"kind": "assets"},
        "signal": {"variables": [{"name": "trend", "source": "cross_asset", "field": "trend_signal"}]},
        "construction": {"side": "long_short", "quantiles": 3, "rebalance": "W", "weighting": "inverse_vol"}}},
    {"id": "xa_carry", "title": "Cross-asset carry (monthly)", "spec": {
        "name": "Cross-asset carry", "universe": {"kind": "assets"},
        "signal": {"variables": [{"name": "carry", "source": "cross_asset", "field": "carry"}]},
        "construction": {"side": "long_short", "quantiles": 3, "rebalance": "M", "weighting": "inverse_vol"}}},
    {"id": "cftc_contrarian", "title": "CFTC extremes, contrarian (20-day hold)", "spec": {
        "name": "CFTC contrarian", "mode": "event", "universe": {"kind": "assets", "asset_classes": ["commodity"]},
        "signal": {"variables": [{"name": "cot", "source": "cftc", "field": "cot_index_3y"}]},
        "event": {"trigger": "cftc_extreme", "params": {"high": 0.9, "low": 0.1, "mode": "contrarian"}, "hold_days": 20,
                  "allow_short": True, "max_positions": 10}}},
]
# S1.4: the permutation test is opt-in (Robustness.permutations default 0); presets keep their outputs (20)
for _p in PRESETS:
    _p["spec"].setdefault("robustness", {}).setdefault("permutations", 20)


def _coverage(con: duckdb.DuckDBPyConnection, table: str | None, dcol: str | None) -> dict[str, Any] | None:
    if not table or not dcol:
        return None
    ent = "asset_id" if table == "cross_asset_signals" else "security_id"
    if table == "macro_surprises":
        ent = "series_id"
    if table == "ratios":
        ent = "issuer_id"
    try:
        r = con.execute(f'SELECT min("{dcol}"), max("{dcol}"), count(DISTINCT "{dcol}"), count(DISTINCT "{ent}"), count(*) '
                        f'FROM "{table}"').fetchone()
    except duckdb.Error:
        return None
    iso = lambda v: v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else None)  # noqa: E731
    return {"first_date": iso(r[0]), "last_date": iso(r[1]), "n_dates": int(r[2] or 0), "n_entities": int(r[3] or 0),
            "n_rows": int(r[4] or 0), "backtestable": int(r[2] or 0) >= 12}


def build(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    snap = snapshot_id(s)
    key = f"{snap}_{CATALOG_VERSION}"
    with _lock:
        hit = _mem.get(key)
        if hit and time.monotonic() - hit["_t"] < 3600:
            return {k: v for k, v in hit.items() if k != "_t"}
        disk = cache_dir(s) / f"catalog_{key}.json"
        if disk.exists():
            try:
                cat = json.loads(disk.read_text(encoding="utf-8"))
                _mem.clear()
                _mem[key] = {**cat, "_t": time.monotonic()}
                return cat
            except ValueError:
                pass
        tables = sorted({x["table"] for x in SOURCES + EVENTS if x.get("table")} | {"macro_factors"})
        from app.modeling.datasets import lake_connection  # noqa: PLC0415

        con = lake_connection(tables, s)
        try:
            cat = _compute(con)
        finally:
            con.close()
        cat["snapshot_id"] = snap
        disk.write_text(json.dumps(cat, default=str), encoding="utf-8")
        _mem.clear()
        _mem[key] = {**cat, "_t": time.monotonic()}
        return cat


def _compute(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    sources = []
    for src in SOURCES:
        sources.append({**{k: v for k, v in src.items() if k not in ("table", "date_col")},
                        "model_based": bool(src.get("model_based")), "coverage": _coverage(con, src.get("table"), src.get("date_col"))})
    events = [{**{k: v for k, v in e.items() if k not in ("table", "date_col")},
               "coverage": _coverage(con, e.get("table"), e.get("date_col"))} for e in EVENTS]
    regimes = []
    try:
        rows = con.execute("SELECT factor_id, list(DISTINCT state), any_value(model_based), min(date), max(date) FROM macro_factors "
                           "WHERE CAST(unit AS VARCHAR) = 'state' AND CAST(frequency AS VARCHAR) = 'M' GROUP BY 1 ORDER BY 1").fetchall()
        for fid, states, mb, d0, d1 in rows:
            for rt in ("cycle_quadrant", "hmm_regime", "rate_cycle", "inflation_regime"):
                if fid.startswith(rt + "_"):
                    regimes.append({"regime_type": rt, "scope": fid[len(rt) + 1:], "states": sorted(x for x in states if x),
                                    "model_based": bool(mb), "first_date": str(d0), "last_date": str(d1)})
    except duckdb.Error:
        pass
    try:
        assets = [{"asset_id": a, "asset_class": c} for a, c in con.execute(
            "SELECT DISTINCT asset_id, CAST(asset_class AS VARCHAR) FROM cross_asset_signals ORDER BY 2, 1").fetchall()]
    except duckdb.Error:
        assets = []
    fmap = screen_field_map()
    screen = [{"field": k, "label": v.get("label") or k, "kind": v["kind"]} for k, v in fmap.items()]
    return {"sources": sources, "event_triggers": events, "regimes": regimes, "assets": assets,
            "asset_classes": list(ASSET_CLASSES), "screen_fields": screen,
            "screen_note": "fields without point-in-time history (DCF fair value, forecasts, zones, KPIs, data quality, "
                           "short interest) are dropped from backtested screens with a warning; any ratios metric id is accepted",
            "universes": ["mvp_us", "mvp_global", "full", "eu_broad"],
            "benchmarks": [{"id": b, "label": lab} for b, lab in BENCHMARKS],
            "presets": PRESETS, "survivorship_caveat": SURVIVORSHIP_CAVEAT,
            "costs_default": {"commission_bps": 1.0, "spread_buckets": [{"min_adv_usd": a, "half_spread_bps": b} for a, b in
                                                                          [(1e9, 1.0), (1e8, 3.0), (2e7, 8.0), (5e6, 20.0), (0.0, 50.0)]],
                              "impact_coef": 0.1, "capital_usd": 1e7, "borrow_bps": 50.0, "htb_borrow_bps": 300.0,
                              "htb_adv_usd": 5e6, "fx_bps": 2.0},
            "limits": {"max_sweep": 64, "max_permutations": 200, "max_entities": 6000},
            "stop_modes": [{"mode": "fixed", "title": "Fixed fractions", "experimental": False},
                           {"mode": "calibrated_band", "title": "Calibrated band (alpha)", "experimental": True,
                            "params": {"band_level": [80, 90, 95], "band_horizon": [1, 5, 10, 20], "band_target": False},
                            "validation": "MIXED in validation: band stops beat ATR stops, but no stop beat 'no stop' "
                                          "(docs/CONFORMAL_VALIDATION.md)"}],
            "validation_doc": "docs/CONFORMAL_VALIDATION.md"}
