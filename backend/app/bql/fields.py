"""BQL field dictionary: every queryable serving column gets a mnemonic, a description, a unit, its source table, its
point-in-time rule, its frequency and (where the snapshot or the lake keeps one) a history source.

Generated from the contracts (contracts/schemas/*.yaml descriptions + types, sector_kpis.yaml metric registry,
scoring.yaml factors / profiles, canonical_line_items.yaml) and the serving builder's table layout (the empty snapshot
schema), plus a curated alias layer (CURATED: Bloomberg-style names such as px_last, pe_fwd, mkt_cap_usd, div_yld).
The result is published as contracts/bql_fields.yaml and read back at runtime:

    python -m app.bql.fields --write      # regenerate contracts/bql_fields.yaml
    python -m app.bql.fields --check      # exit 1 when the published file is stale

History sources (FieldDef.hist):
* px      - daily bars (serving `prices`, 15 years; weekly `prices_w` before): value = SQL over the bar columns
* event   - a dated stream (key, date, value) of a serving table or a lake table (`lake:<contract>`); the value at
            date D is the latest row with date <= D (date = the day the value became known)
* period  - a knowledge-dated period stream (ratios / fundamentals): the value at D belongs to the latest period known
            with knowledge_date <= D (restatements of older periods never overwrite a newer period)
* expr    - a BQL expression over other fields (chg_pct_1m = chg_pct(px, 21)) used for history / PIT queries while the
            snapshot column serves the latest value
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DICT_VERSION = 1
YAML_NAME = "bql_fields.yaml"

UNITS = ("pct", "pctile", "price", "currency", "currency_usd", "x", "score", "z", "days", "years", "count", "ratio",
         "rank", "decile", "shares", "date", "text", "bool", "number", "bps")
CONVERTIBLE_UNITS = ("price", "currency", "currency_usd")  # with(currency=...) converts these


@dataclass(frozen=True)
class Hist:
    kind: str                    # px | event | period | expr
    table: str = ""              # serving table, or lake:<contract>
    key: str = "security_id"     # security_id | issuer_id
    date: str = "date"           # column: date the value became known
    value: str = ""              # SQL over the source columns (code-defined, never user text)
    where: str = ""              # static SQL condition (code-defined)
    period: str = ""             # kind=period: period end column
    prefer: str = ""             # kind=period: tie-break (TRUE first) at equal period end
    expr: str = ""               # kind=expr: BQL expression
    frequency: str = "D"


@dataclass(frozen=True)
class FieldDef:
    mnemonic: str
    description: str
    unit: str
    dtype: str                   # number | text | bool | date
    group: str
    table: str | None            # snapshot table (None = history only)
    column: str | None           # snapshot column, or SQL with {t} = the table alias
    key: str = "security_id"
    frequency: str = "snapshot"
    pit: str = ""
    source: str = ""             # lake contract table(s)
    hist: Hist | None = None
    aliases: tuple[str, ...] = ()
    static: bool = False         # identity attribute: valid at every date (current classification)
    model_based: bool = False
    curated: bool = False

    def public(self) -> dict[str, Any]:
        d = {"mnemonic": self.mnemonic, "description": self.description, "unit": self.unit, "dtype": self.dtype,
             "group": self.group, "table": self.table, "column": self.column if self.column and "{" not in self.column else
             (self.column and "expression"), "key": self.key, "frequency": self.frequency, "pit": self.pit,
             "source": self.source, "aliases": list(self.aliases), "static": self.static,
             "model_based": self.model_based, "curated": self.curated, "has_history": self.hist is not None,
             "history": None}
        if self.hist is not None:
            h = self.hist
            d["history"] = {"kind": h.kind, "table": h.table or None, "frequency": h.frequency,
                            **({"expr": h.expr} if h.kind == "expr" else {})}
        return d


#: per-security / per-issuer snapshot tables: namespace -> (serving table or virtual SQL, key, lake contract,
#: history (table, date col) or None, description prefix). Columns come from the contract x the snapshot schema.
NAMESPACES: dict[str, dict[str, Any]] = {
    "tech": {"table": "tv3_factors", "contract": "tech_factors", "hist": ("lake:tech_factors", "as_of_date"),
             "group": "technical", "title": "Technicals v3 evidence-based factor", "freq": "D"},
    "cf": {"table": "cf_envelopes", "contract": "conformal_envelopes", "hist": ("lake:conformal_envelopes", "as_of_date"),
           "group": "conformal", "title": "Conformal envelope (model-based)", "model": True, "freq": "D"},
    "tp": {"table": "tp_scores", "contract": "tech_prob_scores", "hist": ("lake:tech_prob_scores", "as_of_date"),
           "group": "technical", "title": "Technical score calibration (model-based)", "model": True, "freq": "W"},
    "setup": {"table": "setups", "contract": "tech_setups", "hist": ("lake:tech_setups", "as_of_date"),
              "group": "technical", "title": "Technical setup", "freq": "D"},
    "bo": {"table": "tv3_bo_state", "contract": "tech_breakout_state", "hist": None, "group": "technical",
           "title": "Breakout state", "freq": "D"},
    "sm": {"table": "sm_signals_latest", "contract": "smart_money_signals", "hist": ("sm_signals", "as_of_date"),
           "group": "smart_money", "title": "Smart-money signal (score model-based)", "model": True, "freq": "W"},
    "si": {"table": "sm_si_latest", "contract": "si_signals", "hist": ("sm_si", "knowledge_date"),
           "group": "smart_money", "title": "FINRA short interest", "freq": "event"},
    "own": {"table": "sm_inst_latest", "contract": "inst_ownership", "hist": ("sm_inst", "knowledge_date"),
            "group": "smart_money", "title": "13F institutional ownership", "freq": "Q"},
    "ins": {"table": "insider_latest", "contract": "insider_signals", "hist": ("insider_signals", "as_of_date"),
            "group": "insider", "title": "Insider signal", "freq": "D"},
    "sent": {"table": "sentiment_latest", "contract": "sentiment_daily", "hist": ("sentiment_daily", "date"),
             "group": "sentiment", "title": "News sentiment (daily)", "freq": "D"},
    "val": {"table": "valuation_latest", "contract": "valuation_daily", "hist": ("lake:valuation_daily", "date"),
            "group": "valuation", "title": "Daily valuation", "freq": "D"},
    "shrt": {"table": "short_interest_latest", "contract": "short_interest", "hist": ("lake:short_interest", "knowledge_date"),
             "group": "smart_money", "title": "Short interest (raw)", "freq": "event"},
    "coc": {"table": "coc_latest", "contract": "cost_of_capital", "key": "issuer_id",
            "hist": ("lake:cost_of_capital", "as_of_date"), "group": "valuation", "title": "Cost of capital (model-based)",
            "model": True, "freq": "M"},
    "earn": {"table": "earn_latest", "contract": "earnings_events", "key": "issuer_id", "hist": ("earn_events", "knowledge_date"),
             "group": "earnings", "title": "Latest earnings release (expectations model-based, not consensus)",
             "model": True, "freq": "event", "contracts_extra": ["earnings_reactions", "earnings_expectations"]},
    "pead": {"table": "pead_latest", "contract": "earnings_pead", "hist": ("earn_pead", "signal_date"),
             "group": "earnings", "title": "Post-earnings drift signal (model-based)", "model": True, "freq": "event"},
    # W3 rates & debt (rates-w3): issuer debt from filings (refi_* MODEL-BASED, labelled in the contract descriptions)
    "debt": {"table": "debt_latest", "contract": "company_debt", "key": "issuer_id",
             "hist": ("lake:company_debt", "knowledge_date"), "group": "credit",
             "title": "Issuer debt from filings", "freq": "Q"},
}

#: virtual snapshot tables (latest row per key of a serving history table); code-defined SQL only
VIRTUAL_TABLES: dict[str, str] = {
    "earn_latest": "(SELECT * FROM earn_events QUALIFY row_number() OVER (PARTITION BY issuer_id "
                   "ORDER BY release_date DESC NULLS LAST, period_end DESC) = 1)",
    "pead_latest": "(SELECT * FROM earn_pead QUALIFY row_number() OVER (PARTITION BY security_id "
                   "ORDER BY signal_date DESC) = 1)",
}
#: serving history tables whose rows are per security/issuer and date (used by the event histories above)
HIST_TABLES = {"sm_signals", "sm_si", "sm_inst", "insider_signals", "sentiment_daily", "earn_events", "earn_pead",
               "factor_hist", "composite_hist", "metrics", "fund", "prices"}
_SKIP_COLS = {"security_id", "issuer_id", "year", "computed_at", "run_id", "model_based", "updated_at", "ingested_at",
              "source_id", "region", "label"}
_NS_SKIP = {"earn": {"url", "filing_id", "source_label", "report_form_type", "release_ts"},
            "own": set(), "cf": set(), "debt": {"fiscal_year", "knowledge_date_estimated", "fx_to_usd"}}

# ------------------------------------------------------------------------------------------------ curated layer


def _C(mn: str, table: str | None, column: str | None, unit: str, desc: str, group: str, *, hist: Hist | None = None,
       aliases: tuple[str, ...] = (), static: bool = False, model: bool = False, freq: str = "D", pit: str = "",
       source: str = "", dtype: str = "number", key: str = "security_id") -> FieldDef:
    return FieldDef(mn, desc, unit, dtype, group, table, column, key, freq, pit, source, hist, aliases, static, model, True)


PIT_PX = "daily bar of the last trading session <= the date (prices_daily; weekly bars before the 15-year daily window)"
PIT_SNAP = "snapshot only: the latest value at the snapshot as-of date (no point-in-time history in the snapshot)"
PIT_STATIC = "current classification (not point-in-time; identity attributes)"
PIT_INSTANT = "latest balance-sheet date known with knowledge_date <= the date (every filed version is kept)"
PIT_METRIC = ("latest period known with knowledge_date <= the date (TTM preferred, FY otherwise; filing / publication "
              "date = knowledge date)")


def _px(value: str) -> Hist:
    return Hist("px", "prices", value=value)


def _ex(expr: str) -> Hist:
    return Hist("expr", expr=expr)


def _val_hist(col: str) -> Hist:
    return Hist("event", "lake:valuation_daily", date="date", value=col)


CURATED: list[FieldDef] = [
    # -------- identity (static)
    _C("ticker", "sec", "ticker", "text", "Display ticker (Yahoo-style suffix for non-US listings)", "identity",
       static=True, dtype="text", freq="static", pit=PIT_STATIC, source="securities_master"),
    _C("name", "sec", "name", "text", "Security name (Latin script)", "identity", static=True, dtype="text",
       freq="static", pit=PIT_STATIC, source="securities_master", aliases=("long_comp_name",)),
    _C("id", "sec", "security_id", "text", "Internal security id (S..., IDX:..., ETF:...)", "identity", static=True,
       dtype="text", freq="static", pit=PIT_STATIC, source="securities_master", aliases=("security_id",)),
    _C("issuer", "sec", "issuer_id", "text", "Internal issuer id", "identity", static=True, dtype="text",
       freq="static", pit=PIT_STATIC, source="issuers", aliases=("issuer_id",)),
    _C("isin", "sec", "isin", "text", "ISIN", "identity", static=True, dtype="text", freq="static", pit=PIT_STATIC,
       source="securities_master", aliases=("id_isin",)),
    _C("mic", "sec", "mic", "text", "Primary listing MIC", "identity", static=True, dtype="text", freq="static",
       pit=PIT_STATIC, source="listings"),
    _C("exchange", "sec", "exchange_name", "text", "Primary listing exchange name", "identity", static=True,
       dtype="text", freq="static", pit=PIT_STATIC, source="listings", aliases=("exch_name", "exchange_name")),
    _C("country", "sec", "country", "text", "Issuer country (ISO2)", "identity", static=True, dtype="text",
       freq="static", pit=PIT_STATIC, source="issuers", aliases=("cntry_of_domicile",)),
    _C("region", "sec", "region", "text", "Region (US CA LATAM UK EU JP KR CN HK APAC OTHER)", "identity",
       static=True, dtype="text", freq="static", pit=PIT_STATIC, source="issuers"),
    _C("sector", "sec", "sector_id", "text", "Sector id (sector_taxonomy.yaml: TECH, HLTH, FINS, ...)", "identity",
       static=True, dtype="text", freq="static", pit=PIT_STATIC, source="issuers", aliases=("sector_id", "gics_sector")),
    _C("subsector", "sec", "subsector_id", "text", "Subsector id", "identity", static=True, dtype="text",
       freq="static", pit=PIT_STATIC, source="issuers", aliases=("subsector_id", "industry")),
    _C("currency", "sec", "currency", "text", "Trading currency (ISO 4217)", "identity", static=True, dtype="text",
       freq="static", pit=PIT_STATIC, source="securities_master", aliases=("crncy",)),
    _C("security_type", "sec", "security_type", "text", "common / adr / preferred / etf / index / fx / future",
       "identity", static=True, dtype="text", freq="static", pit=PIT_STATIC, source="securities_master",
       aliases=("type",)),
    _C("is_primary", "sec", "is_primary_security", "bool", "Primary security of its issuer", "identity", static=True,
       dtype="bool", freq="static", pit=PIT_STATIC, source="securities_master"),
    _C("size_bucket", "sec", "size_bucket", "text", "large >= $10B, mid >= $2B, small (market cap)", "identity",
       dtype="text", freq="snapshot", pit=PIT_SNAP, source="valuation_daily"),
    # -------- prices
    _C("px", None, None, "price", "Adjusted close (splits and dividends; trading currency)", "price",
       hist=_px("adj_close"), pit=PIT_PX, source="prices_daily", aliases=("px_adj", "adj_close")),
    _C("px_last", "sec", "close", "price", "Last unadjusted close (trading currency)", "price", hist=_px("close"),
       pit=PIT_PX, source="prices_daily", aliases=("last_price", "close")),
    _C("px_open", None, None, "price", "Unadjusted open", "price", hist=_px("open"), pit=PIT_PX, source="prices_daily"),
    _C("px_high", None, None, "price", "Unadjusted high", "price", hist=_px("high"), pit=PIT_PX, source="prices_daily"),
    _C("px_low", None, None, "price", "Unadjusted low", "price", hist=_px("low"), pit=PIT_PX, source="prices_daily"),
    _C("px_high_adj", None, None, "price", "Adjusted high (high x adj_close / close)", "price",
       hist=_px("high * adj_close / NULLIF(close, 0)"), pit=PIT_PX, source="prices_daily"),
    _C("px_low_adj", None, None, "price", "Adjusted low (low x adj_close / close)", "price",
       hist=_px("low * adj_close / NULLIF(close, 0)"), pit=PIT_PX, source="prices_daily"),
    _C("px_volume", "sec", "volume", "shares", "Volume (shares) of the last session", "price", hist=_px("volume"),
       pit=PIT_PX, source="prices_daily", aliases=("volume",)),
    _C("px_last_usd", "sec", "close_usd", "currency_usd", "Last close in USD", "price", hist=_px("close_usd"),
       pit=PIT_PX, source="prices_daily", aliases=("close_usd",)),
    _C("turnover_usd", None, None, "currency_usd", "Dollar volume (close in USD x volume)", "price",
       hist=_px("close_usd * volume"), pit=PIT_PX, source="prices_daily"),
    _C("px_date", "sec", "px_date", "date", "Date of the last price bar", "price", dtype="date",
       hist=_px("date"), pit=PIT_PX, source="prices_daily"),
    # -------- returns (snapshot column for the latest value, expression for history / PIT)
    _C("chg_pct_1d", "sec", "chg_1d", "pct", "1-session return (adjusted)", "return", hist=_ex("chg_pct(px, 1)"),
       pit=PIT_PX, source="prices_daily", aliases=("chg_1d", "day_to_day_tot_return")),
    _C("chg_pct_1w", "sec", "chg_1w", "pct", "5-session return", "return", hist=_ex("chg_pct(px, 5)"), pit=PIT_PX,
       source="prices_daily", aliases=("chg_1w",)),
    _C("chg_pct_1m", "sec", "chg_1m", "pct", "21-session return", "return", hist=_ex("chg_pct(px, 21)"), pit=PIT_PX,
       source="prices_daily", aliases=("chg_1m", "ret_1m")),
    _C("chg_pct_3m", "sec", "chg_3m", "pct", "63-session return", "return", hist=_ex("chg_pct(px, 63)"), pit=PIT_PX,
       source="prices_daily", aliases=("chg_3m", "ret_3m")),
    _C("chg_pct_6m", "sec", "chg_6m", "pct", "126-session return", "return", hist=_ex("chg_pct(px, 126)"),
       pit=PIT_PX, source="prices_daily", aliases=("chg_6m", "ret_6m")),
    _C("chg_pct_12m", "sec", "ret_12m", "pct", "252-session return", "return", hist=_ex("chg_pct(px, 252)"),
       pit=PIT_PX, source="prices_daily", aliases=("ret_12m", "chg_1y", "chg_pct_1y")),
    _C("chg_pct_ytd", "sec", "chg_ytd", "pct", "Year-to-date return (vs the last close of the previous year)",
       "return", hist=_ex("ytd(px)"), pit=PIT_PX, source="prices_daily", aliases=("chg_ytd", "ytd_return")),
    _C("mom_12_1", "sec", "mom_12_1", "pct", "12-1 month momentum: px[t-21] / px[t-252] - 1", "return",
       hist=_ex("lag(px, 21) / lag(px, 252) - 1"), pit=PIT_PX, source="prices_daily", aliases=("momentum",)),
    _C("mom_6_1", "sec", "mom_6_1", "pct", "6-1 month momentum: px[t-21] / px[t-126] - 1", "return",
       hist=_ex("lag(px, 21) / lag(px, 126) - 1"), pit=PIT_PX, source="prices_daily"),
    # -------- price technicals
    _C("hi52", "sec", "high_52w", "price", "52-week high (unadjusted scale of the last close)", "technical",
       hist=_ex("max(px_high_adj, 252)"), pit=PIT_PX, source="prices_daily", aliases=("high_52week", "high_52w")),
    _C("lo52", "sec", "low_52w", "price", "52-week low (unadjusted scale of the last close)", "technical",
       hist=_ex("min(px_low_adj, 252)"), pit=PIT_PX, source="prices_daily", aliases=("low_52week", "low_52w")),
    _C("px_to_hi52", "sec", "high_52w_proximity", "ratio", "Adjusted close / 52-week high", "technical",
       hist=_ex("px / max(px_high_adj, 252)"), pit=PIT_PX, source="prices_daily",
       aliases=("high_52w_proximity", "pct_of_52w_high")),
    _C("sma50", "sec", "sma50", "price", "50-session simple moving average", "technical", hist=_ex("avg(px, 50)"),
       pit=PIT_PX, source="prices_daily", aliases=("mov_avg_50d",)),
    _C("sma200", "sec", "sma200", "price", "200-session simple moving average", "technical", hist=_ex("avg(px, 200)"),
       pit=PIT_PX, source="prices_daily", aliases=("mov_avg_200d",)),
    _C("trend_200d", "sec", "trend_200d", "pct", "Distance to the 200-session average (px / sma200 - 1)",
       "technical", hist=_ex("px / avg(px, 200) - 1"), pit=PIT_PX, source="prices_daily"),
    _C("above_50d", "sec", "above_50d", "bool", "Close above the 50-session average", "technical", dtype="bool",
       hist=_ex("px > avg(px, 50)"), pit=PIT_PX, source="prices_daily"),
    _C("above_200d", "sec", "above_200d", "bool", "Close above the 200-session average", "technical", dtype="bool",
       hist=_ex("px > avg(px, 200)"), pit=PIT_PX, source="prices_daily"),
    _C("new_hi52", "sec", "new_high_52w", "bool", "Session high at a new 52-week high", "technical", dtype="bool",
       pit=PIT_SNAP, freq="snapshot", source="prices_daily", aliases=("new_high_52w",)),
    _C("new_lo52", "sec", "new_low_52w", "bool", "Session low at a new 52-week low", "technical", dtype="bool",
       pit=PIT_SNAP, freq="snapshot", source="prices_daily", aliases=("new_low_52w",)),
    _C("vol_1y", "sec", "vol_1y", "pct", "Annualised volatility of daily log returns, 252 sessions", "technical",
       hist=_ex("std(ln(px / lag(px, 1)), 252) * sqrt(252)"), pit=PIT_PX, source="prices_daily",
       aliases=("volatility_260d", "volatility")),
    _C("atr14", "sec", "atr14", "price", "Average true range, 14 sessions", "technical", pit=PIT_SNAP,
       freq="snapshot", source="prices_daily", aliases=("atr",)),
    _C("volume_z", "sec", "volume_z", "z", "Volume z-score vs the previous 20 sessions", "technical", pit=PIT_SNAP,
       freq="snapshot", source="prices_daily"),
    _C("beta_1y", "sec", "beta_1y", "x", "1-year beta of daily log returns vs the region benchmark", "technical",
       pit=PIT_SNAP, freq="snapshot", source="prices_daily", aliases=("beta", "beta_raw")),
    _C("adv_usd_20d", "sec", "adv_usd_20d", "currency_usd", "Average dollar volume, 20 sessions", "technical",
       hist=_ex("avg(turnover_usd, 20)"), pit=PIT_PX, source="prices_daily", aliases=("adv_20d", "adv")),
    # -------- valuation
    _C("mkt_cap_usd", "sec", "market_cap_usd", "currency_usd", "Market capitalisation (USD)", "valuation",
       hist=_val_hist("market_cap_usd"), pit="valuation_daily row of the date (fundamentals known <= date)",
       source="valuation_daily", aliases=("market_cap_usd", "cur_mkt_cap", "mkt_cap")),
    _C("ev_usd", "sec", "enterprise_value_usd", "currency_usd", "Enterprise value (USD)", "valuation",
       hist=_val_hist("enterprise_value_usd"), pit="valuation_daily row of the date", source="valuation_daily",
       aliases=("enterprise_value_usd", "curr_entp_val")),
    _C("pe_ttm", "sec", "pe_ttm", "x", "Price / trailing-12-month EPS", "valuation", hist=_val_hist("pe_ttm"),
       pit="valuation_daily row of the date", source="valuation_daily", aliases=("pe_ratio", "pe")),
    _C("pe_fwd", "sec", "pe_ntm", "x", "Price / next fiscal year EPS of the base-case forecast (MODEL-BASED, not "
       "consensus)", "valuation", model=True, pit=PIT_SNAP, freq="snapshot", source="forecasts",
       aliases=("pe_ntm", "best_pe_ratio")),
    _C("pb", "sec", "pb", "x", "Price / book", "valuation", hist=_val_hist("pb"), pit="valuation_daily row of the date",
       source="valuation_daily", aliases=("px_to_book", "px_to_book_ratio")),
    _C("ps_ttm", "sec", "ps_ttm", "x", "Price / sales TTM", "valuation", hist=_val_hist("ps_ttm"),
       pit="valuation_daily row of the date", source="valuation_daily", aliases=("px_to_sales", "px_to_sales_ratio")),
    _C("ev_sales", "sec", "ev_sales", "x", "EV / sales TTM", "valuation", hist=_val_hist("ev_sales"),
       pit="valuation_daily row of the date", source="valuation_daily"),
    _C("ev_ebitda", "sec", "ev_ebitda", "x", "EV / EBITDA TTM", "valuation", hist=_val_hist("ev_ebitda"),
       pit="valuation_daily row of the date", source="valuation_daily", aliases=("ev_to_ebitda",)),
    _C("ev_ebit", "sec", "ev_ebit", "x", "EV / EBIT TTM", "valuation", hist=_val_hist("ev_ebit"),
       pit="valuation_daily row of the date", source="valuation_daily", aliases=("ev_to_ebit",)),
    _C("fcf_yield", "sec", "fcf_yield", "pct", "Free cash flow TTM / market cap", "valuation",
       hist=_val_hist("fcf_yield"), pit="valuation_daily row of the date", source="valuation_daily"),
    _C("div_yld", "sec", "dividend_yield", "pct", "Dividend yield (TTM dividends / price)", "valuation",
       hist=_val_hist("dividend_yield"), pit="valuation_daily row of the date", source="valuation_daily",
       aliases=("dividend_yield", "eqy_dvd_yld_ind", "dvd_yld")),
    _C("earnings_yield", "sec", "earnings_yield", "pct", "EPS TTM / price", "valuation",
       hist=_val_hist("earnings_yield"), pit="valuation_daily row of the date", source="valuation_daily"),
    _C("shareholder_yield", "sec", "shareholder_yield", "pct", "Dividends + net buybacks TTM / market cap",
       "valuation", hist=_val_hist("shareholder_yield"), pit="valuation_daily row of the date", source="valuation_daily"),
    _C("p_ffo", "sec", "p_ffo", "x", "Price / FFO (REITs)", "valuation", hist=_val_hist("p_ffo"),
       pit="valuation_daily row of the date", source="valuation_daily"),
    _C("p_tbv", "sec", "p_tbv", "x", "Price / tangible book", "valuation", hist=_val_hist("p_tbv"),
       pit="valuation_daily row of the date", source="valuation_daily"),
    _C("fair_value", "sec", "fv_weighted", "price", "Probability-weighted DCF x multiples fair value per share "
       "(MODEL-BASED)", "valuation", model=True, pit=PIT_SNAP, freq="snapshot", source="forecast_valuation",
       aliases=("fv_weighted",)),
    _C("dcf_upside", "sec", "fv_upside", "pct", "Fair value / last close - 1 (MODEL-BASED)", "valuation", model=True,
       pit=PIT_SNAP, freq="snapshot", source="forecast_valuation", aliases=("fv_upside",)),
    _C("implied_cagr", "sec", "fv_implied_cagr", "pct", "Reverse-DCF implied 5-year revenue CAGR (MODEL-BASED)",
       "valuation", model=True, pit=PIT_SNAP, freq="snapshot", source="forecast_valuation"),
    _C("shares_out", "sec", "shares_out", "shares", "Shares outstanding", "valuation", pit=PIT_SNAP, freq="snapshot",
       source="valuation_daily", aliases=("eqy_sh_out",)),
    # -------- scores
    _C("composite", "sec", "c_balanced", "score", "Composite score 0..100 (balanced profile)", "score",
       hist=Hist("event", "composite_hist", date="as_of_date", value="score", where="profile_id = 'balanced'",
                 frequency="M"),
       pit="latest monthly composite with as_of_date <= the date", source="composite_scores", freq="M",
       aliases=("composite_score",)),
    _C("grade", "sec", "g_balanced", "text", "Composite grade A..E (balanced)", "score", dtype="text", pit=PIT_SNAP,
       freq="snapshot", source="composite_scores"),
    _C("rank_global", "sec", "rk_balanced", "rank", "Global rank of the balanced composite (1 = best)", "score",
       pit=PIT_SNAP, freq="snapshot", source="composite_scores"),
    _C("moat", "sec", "moat_class", "text", "Moat class (wide / narrow / none / insufficient_data)", "score",
       dtype="text", pit=PIT_SNAP, freq="snapshot", source="moat", aliases=("moat_class",)),
    _C("moat_score", "sec", "moat_score", "score", "Moat score 0..100", "score", pit=PIT_SNAP, freq="snapshot",
       source="moat"),
    _C("dq_score", "sec", "dq_score", "score", "Data-quality score 0..1", "score", pit=PIT_SNAP, freq="snapshot",
       source="composite_scores"),
    # -------- alt data / signals
    _C("insider_signal", "sec", "insider_signal", "score", "Insider signal score", "insider",
       hist=Hist("event", "insider_signals", date="as_of_date", value="signal_score"),
       pit="latest insider_signals row with as_of_date <= the date (Form 4 filing dates)", source="insider_signals"),
    _C("sentiment", "sec", "sentiment_score", "score", "News sentiment score (latest day)", "sentiment",
       hist=Hist("event", "sentiment_daily", date="date", value="sentiment_score"),
       pit="sentiment_daily row of the last day <= the date (items known that day)", source="sentiment_daily",
       aliases=("sentiment_score", "news_sentiment")),
    _C("news_count_30d", "sec", "news_count_30d", "count", "Linked news items, last 30 days", "sentiment",
       pit=PIT_SNAP, freq="snapshot", source="sentiment_daily"),
    _C("attention_z", "sec", "attention_z", "z", "GDELT media attention z-score", "sentiment", pit=PIT_SNAP,
       freq="snapshot", source="gdelt_company_daily"),
    _C("si.pct_float", "sm_si_latest", "si_pct_out", "pct",
       "Short interest as a fraction of the float (latest FINRA settlement; FINRA publishes no float, so the float is "
       "proxied by shares outstanding)", "smart_money",
       hist=Hist("event", "sm_si", date="knowledge_date", value="si_pct_out", frequency="event"),
       pit="latest settlement published with knowledge_date <= the date", source="short_interest, si_signals",
       freq="event", aliases=("short_pct_float", "short_int_ratio_float")),
    _C("si.days", "sec", "days_to_cover", "days", "Days to cover (short interest / average daily volume)",
       "smart_money", hist=Hist("event", "lake:short_interest", date="knowledge_date", value="days_to_cover",
                                frequency="event"),
       pit="latest settlement published with knowledge_date <= the date", source="short_interest", freq="event",
       aliases=("days_to_cover",)),
    _C("eps_surprise", None, None, "pct", "EPS surprise of the latest release vs the MODEL-BASED expectation (not "
       "consensus), fraction", "earnings", model=True,
       hist=Hist("event", "earn_events", key="issuer_id", date="knowledge_date", value="eps_surprise_pct",
                 frequency="event"),
       pit="latest release with knowledge_date <= the date", source="earnings_expectations", freq="event",
       key="issuer_id", aliases=("eps_surprise_pct", "earn_eps_surprise")),
    _C("eps_surprise_z", None, None, "z", "Standardised EPS surprise vs the MODEL-BASED expectation", "earnings",
       model=True, hist=Hist("event", "earn_events", key="issuer_id", date="knowledge_date", value="eps_surprise_z",
                             frequency="event"),
       pit="latest release with knowledge_date <= the date", source="earnings_expectations", freq="event",
       key="issuer_id"),
    _C("rev_surprise", None, None, "pct", "Revenue surprise vs the MODEL-BASED expectation, fraction", "earnings",
       model=True, hist=Hist("event", "earn_events", key="issuer_id", date="knowledge_date", value="rev_surprise_pct",
                             frequency="event"),
       pit="latest release with knowledge_date <= the date", source="earnings_expectations", freq="event",
       key="issuer_id", aliases=("rev_surprise_pct", "sales_surprise")),
    _C("cf.touch_20d", "cf_envelopes", "exp(sqrt({t}.qm_20_90 * {t}.v_20)) - 1", "pct",
       "Calibrated 20-session move to the 90% marginal band edge, fraction of the close (a move this size or larger "
       "was touched ~10% of the time historically; MODEL-BASED)", "conformal", model=True,
       hist=Hist("event", "lake:conformal_envelopes", date="as_of_date", value="exp(sqrt(qm_20_90 * v_20)) - 1"),
       pit="latest envelope with as_of_date <= the date (prices known at that close)", source="conformal_envelopes",
       aliases=("cf.band_20d_90",)),
    _C("cf.band_5d_90", "cf_envelopes", "exp(sqrt({t}.qm_5_90 * {t}.v_5)) - 1", "pct",
       "Half-width of the 90% marginal 5-session calibrated band, fraction of the close (MODEL-BASED)", "conformal",
       model=True, hist=Hist("event", "lake:conformal_envelopes", date="as_of_date", value="exp(sqrt(qm_5_90 * v_5)) - 1"),
       pit="latest envelope with as_of_date <= the date", source="conformal_envelopes", aliases=("cf.touch_5d",)),
]


# ------------------------------------------------------------------------------------------------ generator helpers

_LABEL_UNITS = {"pct": "pct", "x": "x", "days": "days", "currency_usd": "currency_usd", "score": "score",
                "years": "years", "ratio": "ratio", "count": "count", "currency": "currency", "": "number"}


def _unit_of(col: str, ctype: str, desc: str) -> tuple[str, str]:
    """(unit, dtype) heuristics for auto-generated fields."""
    t = ctype.lower()
    d = desc.lower()
    if t == "bool" or t == "boolean":
        return "bool", "bool"
    if t in ("date",):
        return "date", "date"
    if t.startswith(("datetime", "timestamp")):
        return "date", "date"
    if t in ("string", "enum", "varchar", "json") or t.startswith(("list", "struct")):
        return "text", "text"
    if ("0..100" in d or "percentile" in d) and ("pct" in col or "percentile" in d):
        return "pctile", "number"
    if col.startswith("z_") or col.endswith("_z") or "z-score" in d or col.endswith("_z_1y") or "_z_" in col:
        return "z", "number"
    if col.endswith("_usd") or "(usd)" in d or " usd" in d:
        return "currency_usd", "number"
    if col.endswith(("_pct", "_share", "_chg_pct")) or col.startswith("pct_") or "fraction" in d or "share of" in d:
        return "pct", "number"
    if "score" in col:
        return "score", "number"
    if col.startswith(("n_", "num_")) or col.endswith("_count"):
        return "count", "number"
    if "days" in col:
        return "days", "number"
    if "decile" in col:
        return "decile", "number"
    return "number", "number"


def _ns_name(ns: str, col: str) -> str:
    """tech.pct_hi52 -> tech.hi52_pct (percentile columns read as <factor>_pct)."""
    if col.startswith("pct_"):
        col = col[4:] + "_pct"
    return f"{ns}.{col}"


def serving_schema() -> dict[str, dict[str, str]]:
    """{serving table: {column: duckdb type}} from the empty snapshot (every serving table, exact builder schema)."""
    from app.api.serving import empty_snapshot

    con = empty_snapshot()
    try:
        out: dict[str, dict[str, str]] = {}
        for (t,) in con.execute("SELECT table_name FROM duckdb_tables() WHERE schema_name = 'main'").fetchall():
            out[t] = {r[0]: r[1] for r in con.execute(f'DESCRIBE "{t}"').fetchall()}
        return out
    finally:
        con.close()


def generate(schema: dict[str, dict[str, str]] | None = None) -> list[FieldDef]:
    """The full dictionary (curated first, then generated groups). Deterministic order."""
    from app.api.serving import FACTORS, contract_yaml, metric_registry, profiles
    from app.core.schema import load_contracts

    schema = schema or serving_schema()
    contracts = load_contracts()
    out: list[FieldDef] = []
    used: set[str] = set()

    def add(f: FieldDef) -> None:
        if f.mnemonic in used:
            return
        al = tuple(a for a in f.aliases if a not in used and a != f.mnemonic)
        if al != f.aliases:
            f = FieldDef(**{**asdict_shallow(f), "aliases": al})
        used.update((f.mnemonic,) + al)
        out.append(f)

    for f in CURATED:
        add(f)
    sec_cols = schema.get("sec", {})

    # -------- metrics (ratios + accounting scores) and sector KPIs
    kp = contract_yaml("sector_kpis.yaml")
    meta = {m["id"]: m for m in kp.get("generic_metrics", []) + kp.get("accounting_scores", [])}
    kmeta: dict[str, dict[str, Any]] = {}
    for ks in kp.get("kpi_sets", []):
        for k in ks.get("kpis") or []:
            kmeta.setdefault(k["id"], k)
    reg = metric_registry()
    from app.api.routers._common import label_for

    for mid in reg["generic"] + reg["accounting"]:
        col = "m_" + mid
        if col not in sec_cols:
            continue
        m = meta.get(mid, {})
        unit = _LABEL_UNITS.get(m.get("unit", ""), "number")
        desc = f"{m.get('label') or label_for(mid)}" + (f" = {m['formula']}" if m.get("formula") else "")
        add(FieldDef(mid, desc, unit, "number", "fundamental", "sec", col, "security_id", "Q", PIT_METRIC, "ratios",
                     Hist("period", "metrics", key="issuer_id", date="knowledge_date", period="period_end", value="value",
                          where=f"metric_id = '{mid}' AND period_kind IN ('TTM', 'FY')", prefer="period_kind = 'TTM'",
                          frequency="Q"),
                     ("m." + mid,), False, False, False))
    for kid in reg["kpi"]:
        col = "k_" + kid
        if col not in sec_cols:
            continue
        k = kmeta.get(kid, {})
        unit = _LABEL_UNITS.get(k.get("unit", ""), "number")
        desc = f"Sector KPI: {k.get('label') or label_for(kid)}" + (f" = {k['formula']}" if k.get("formula") else "")
        add(FieldDef("kpi." + kid, desc, unit, "number", "sector_kpi", "sec", col, "security_id", "Q", PIT_METRIC,
                     "sector_kpis",
                     Hist("period", "metrics", key="issuer_id", date="knowledge_date", period="period_end", value="value",
                          where=f"metric_id = '{kid}' AND period_kind IN ('TTM', 'FY')", prefer="period_kind = 'TTM'",
                          frequency="Q"), (), False, False, False))

    # -------- factor scores / composites
    sc = contract_yaml("scoring.yaml")
    fnames = {f["id"]: f.get("name", f["id"]) for f in sc.get("factors", [])}
    for fid in FACTORS:
        add(FieldDef(f"f.{fid}", f"Factor score 0..100: {fnames.get(fid, fid)} (sector-relative percentile blend)",
                     "score", "number", "factor", "sec", f"f_{fid}", "security_id", "M",
                     "latest monthly factor score with as_of_date <= the date", "factor_scores",
                     Hist("event", "factor_hist", date="as_of_date", value="score", where=f"factor_id = '{fid}'",
                          frequency="M"), (f"{fid}_score",), False, False, False))
        add(FieldDef(f"f.{fid}.rank_sector", f"Rank of the {fid} factor score within the sector (1 = best)", "rank",
                     "number", "factor", "sec", f"fr_{fid}", "security_id", "snapshot", PIT_SNAP, "factor_scores"))
        add(FieldDef(f"f.{fid}.chg_1m", f"1-month change of the {fid} factor score", "score", "number", "factor", "sec",
                     f"fd_{fid}", "security_id", "snapshot", PIT_SNAP, "factor_scores"))
    for pid in profiles():
        add(FieldDef(f"c.{pid}", f"Composite score 0..100, profile {pid}" + (" (MODEL-BASED ML blend)" if pid == "ml_blend" else ""),
                     "score", "number", "score", "sec", f"c_{pid}", "security_id", "M",
                     "latest monthly composite with as_of_date <= the date", "composite_scores",
                     Hist("event", "composite_hist", date="as_of_date", value="score", where=f"profile_id = '{pid}'",
                          frequency="M"), (), False, pid == "ml_blend", False))
        add(FieldDef(f"grade.{pid}", f"Composite grade A..E, profile {pid}", "text", "text", "score", "sec", f"g_{pid}",
                     "security_id", "snapshot", PIT_SNAP, "composite_scores"))
        add(FieldDef(f"rank.{pid}", f"Global rank of the {pid} composite (1 = best)", "rank", "number", "score", "sec",
                     f"rk_{pid}", "security_id", "snapshot", PIT_SNAP, "composite_scores"))
        add(FieldDef(f"rank_sector.{pid}", f"Sector rank of the {pid} composite (1 = best)", "rank", "number", "score",
                     "sec", f"rks_{pid}", "security_id", "snapshot", PIT_SNAP, "composite_scores"))

    # -------- full-universe extension (2026-10-30): ranks among ALL scored securities, scope, extrapolation flags
    for fid in FACTORS:
        if f"frf_{fid}" in sec_cols:
            add(FieldDef(f"f.{fid}.rank_sector_full", f"Rank of the {fid} factor score among all scored securities of "
                         "its region x sector (MVP + extended, 1 = best)", "rank", "number", "factor", "sec", f"frf_{fid}",
                         "security_id", "snapshot", PIT_SNAP, "factor_scores"))
    for col, nm, desc, unit, dtype in (
            ("rgf_balanced", "rank_full", "Rank of the balanced composite among all scored securities (MVP + extended, "
             "1 = best)", "rank", "number"),
            ("rsf_balanced", "rank_sector_full", "Rank of the balanced composite within region x sector among all scored "
             "securities (1 = best)", "rank", "number"),
            ("nsf_balanced", "n_sector_full", "Scored securities in the region x sector (balanced composite)", "number",
             "number"),
            ("score_scope", "score_scope", "Scoring scope: mvp (model universe) or extended (anchored to the MVP "
             "reference cross-section)", "text", "text"),
            ("score_missing", "score_missing", "Missing inputs when the composite is null (insufficient_data)", "text",
             "text")):
        if col in sec_cols:
            add(FieldDef(nm, desc, unit, dtype, "score", "sec", col, "security_id", "snapshot", PIT_SNAP,
                         "composite_scores"))
    for h in ("1M", "3M", "6M"):
        hl = h.lower()
        if f"ml_extrap_{h}" in sec_cols:
            add(FieldDef(f"ml.extrapolated_{hl}", f"ML {h} prediction is extrapolated (security outside the MVP model "
                         "universe; MVP-trained production model) (MODEL-BASED)", "bool", "bool", "model", "sec",
                         f"ml_extrap_{h}", "security_id", "snapshot", PIT_SNAP, "ml_predictions", None, (), False, True,
                         False))
        if f"ml_status_{h}" in sec_cols:
            add(FieldDef(f"ml.status_{hl}", f"ML {h} coverage outside the MVP universe: served / no_prediction", "text",
                         "text", "model", "sec", f"ml_status_{h}", "security_id", "snapshot", PIT_SNAP, "ml_scope_status"))

    # -------- ML (model-based) and macro exposures from sec
    for h in ("1M", "3M", "6M"):
        hl = h.lower()
        for col, nm, desc, unit in ((f"ml_rp_{h}", f"ml.rank_pct_{hl}", f"ML predicted cross-sectional rank percentile {h}", "pct"),
                                    (f"ml_dec_{h}", f"ml.decile_{hl}", f"ML predicted decile {h} (10 = best)", "decile"),
                                    (f"ml_score_{h}", f"ml.score_{hl}", f"ML raw score {h}", "number"),
                                    (f"ml_exp_{h}", f"ml.exp_excess_{hl}", f"ML expected excess return {h} (OOS decile mean)", "pct")):
            if col in sec_cols:
                add(FieldDef(nm, desc + " (MODEL-BASED; production model)", unit, "number", "model", "sec", col,
                             "security_id", "snapshot", PIT_SNAP, "ml_predictions", None, (), False, True, False))
    for col, nm, desc in (("mx_beta_rates", "macro.beta_rates", "Beta to the 10y yield (+100bp)"),
                          ("mx_beta_usd", "macro.beta_usd", "Elasticity to the broad USD"),
                          ("mx_beta_oil", "macro.beta_oil", "Elasticity to WTI oil"),
                          ("mx_beta_credit", "macro.beta_credit", "Beta to the credit spread (+100bp)"),
                          ("mx_beta_growth", "macro.beta_growth", "Beta to the growth surprise index (+100 pts)"),
                          ("mx_rates_down_100", "macro.rates_down_100bp", "Modelled impact if rates fall 100bp")):
        if col in sec_cols:
            add(FieldDef(nm, desc + " (MODEL-BASED rolling exposure)", "x" if "beta" in nm else "pct", "number", "macro",
                         "sec", col, "security_id", "snapshot", PIT_SNAP, "macro_exposures", None, (), False, True, False))
    for col, nm, desc, unit in (("ins_cluster_buy", "ins.cluster_buy_flag", "Insider cluster buy (>= 3 insiders)", "bool"),
                                ("ins_c_suite_buy", "ins.c_suite_buy_flag", "C-suite open-market buy", "bool"),
                                ("ins_net_buy_90d", "ins.net_buy_90d_usd", "Insider net buying, 90 days (USD)", "currency_usd"),
                                ("ins_anomaly", "ins.anomaly", "Insider anomaly score (MODEL-BASED)", "score"),
                                ("tz_side", "zone.side", "Top confluence zone side (support / resistance)", "text"),
                                ("tz_center", "zone.center", "Top confluence zone center", "price"),
                                ("tz_distance_atr", "zone.distance_atr", "Distance to the top zone (ATR)", "number"),
                                ("tz_rr", "zone.reward_risk", "Top zone reward / risk", "x")):
        if col in sec_cols:
            add(FieldDef(nm, desc, unit, "bool" if unit == "bool" else ("text" if unit == "text" else "number"),
                         "insider" if nm.startswith("ins.") else "technical", "sec", col, "security_id", "snapshot",
                         PIT_SNAP, "insider_signals" if nm.startswith("ins.") else "confluence_zones"))

    # -------- namespace tables (columns from the contract x the snapshot schema)
    for ns, spec in NAMESPACES.items():
        table = spec["table"]
        tcols = schema.get(table) or (schema.get(_virtual_base(table)) if table in VIRTUAL_TABLES else None) or {}
        cnames = [spec["contract"]] + list(spec.get("contracts_extra", []))
        cdesc: dict[str, tuple[str, str]] = {}
        for cn in cnames:
            c = contracts.get(cn)
            if c is None:
                continue
            for col in c.columns:
                cdesc.setdefault(col.name, (col.type, col.description or ""))
        pit_rule = ""
        c0 = contracts.get(spec["contract"])
        if c0 is not None and c0.pit:
            pit_rule = str(c0.pit.get("rule") or "")
        key = spec.get("key", "security_id")
        hist_src = spec.get("hist")
        hist_cols = schema.get(hist_src[0], {}) if hist_src and not hist_src[0].startswith("lake:") else None
        lake_cols = {col.name for col in c0.columns} if (c0 is not None and hist_src and hist_src[0].startswith("lake:")) else set()
        for col, typ in tcols.items():
            if col in _SKIP_COLS or col in _NS_SKIP.get(ns, set()):
                continue
            ctype, desc = cdesc.get(col, (typ, ""))
            unit, dtype = _unit_of(col, ctype, desc)
            if not desc:
                desc = col.replace("_", " ")
            nm = _ns_name(ns, col)
            h = None
            if hist_src and dtype in ("number", "bool") and (
                    (hist_cols is not None and col in hist_cols) or (hist_cols is None and col in lake_cols)):
                h = Hist("event", hist_src[0], key=key, date=hist_src[1], value=col, frequency=spec.get("freq", "D"))
            pit = (f"latest row with {hist_src[1]} <= the date" + (f" ({pit_rule})" if pit_rule else "")) if h else PIT_SNAP
            add(FieldDef(nm, f"{spec['title']}: {desc}", unit, dtype, spec["group"], table, col, key,
                         spec.get("freq", "D") if h else "snapshot", pit, spec["contract"], h, (), False,
                         bool(spec.get("model")), False))

    # -------- W3 rates context per security (domicile-country curve, spreads, OAS; app.rates.bql)
    from app.rates.bql import rate_fields

    for f in rate_fields(schema):
        add(f)

    # -------- fundamentals line items (serving `fund`, every knowledge-dated version)
    li = contract_yaml("canonical_line_items.yaml")
    for it in li.get("items", []):
        iid = it["id"]
        instant = it.get("period") == "instant"
        u = it.get("unit")
        monetary = u in (None, "per_share")
        kinds = "('INSTANT')" if instant else "('TTM', 'FY')"
        add(FieldDef(f"fa.{iid}", f"{it.get('label', iid)} ({it.get('statement')}, "
                     f"{'balance-sheet point' if instant else 'TTM, FY otherwise'}{', USD' if monetary else ''})",
                     "currency_usd" if monetary else ({"shares": "shares", "count": "count", "pure": "pct"}.get(u or "", "number")),
                     "number", "fundamental", None, None, "issuer_id", "Q",
                     PIT_INSTANT if instant else PIT_METRIC,
                     "fundamentals_canonical",
                     Hist("period", "fund", key="issuer_id", date="knowledge_date", period="period_end",
                          value="value_usd" if monetary else "value",
                          where=f"item_id = '{iid}' AND period_kind IN {kinds}",
                          prefer="period_kind = 'TTM'" if not instant else "", frequency="Q"),
                     (), False, False, False))

    # -------- remaining sec columns (plain names)
    covered = {f.column for f in out if f.table == "sec"}
    skip = {"search_text", "aliases", "logo_url", "lei_1", "description", "website", "dq_score_c", "n_bars", "fx_to_usd",
            "val_date", "size_bucket_v", "score_date", "name_local", "short_name", "cusip", "figi", "lei", "cik",
            "reporting_currency", "reporting_gaap", "fiscal_year_end_mmdd", "employees", "issuer_active", "is_active",
            "kpi_set_id", "fund_period_end", "fund_knowledge_date", "last_filing_date", "shares_outstanding", "fund_gaap",
            "moat_confidence", "moat_as_of", "ins_date", "sent_date", "tz_zone_id", "fv_currency", "mx_as_of",
            "exchange_country", "px_currency"}
    extra_desc = {"u_mvp_us": "Member of the mvp_us universe (top ~500 US by market cap)",
                  "u_mvp_global": "Member of the mvp_global universe (top non-US names)",
                  "u_full": "Member of the full equity universe", "u_watch": "Member of the watch universe",
                  "employees": "Employees", "fiscal_year_end_mmdd": "Fiscal year end (MMDD)"}
    for col, typ in sec_cols.items():
        if col in covered or col in skip or re.match(r"^(f[cnrd]?|frf|fnf|cs|gs|rg|rs|rgf|ngf|rsf|nsf|cr|cc|c|g|rk|rks|m|k|ml_[a-z]+)_", col):
            continue
        unit, dtype = _unit_of(col, typ, "")
        add(FieldDef(col, extra_desc.get(col, col.replace("_", " ")), unit, dtype, "other", "sec", col, "security_id",
                     "snapshot", PIT_SNAP, "", None, (), col.startswith("u_"), False, False))
    for col, nm, desc, unit, dtype in (("employees", "employees", "Employees", "count", "number"),
                                       ("cik", "cik", "SEC CIK", "text", "text"),
                                       ("lei", "lei", "Legal entity identifier", "text", "text"),
                                       ("figi", "figi", "FIGI", "text", "text"),
                                       ("cusip", "cusip", "CUSIP", "text", "text"),
                                       ("reporting_currency", "reporting_currency", "Reporting currency", "text", "text"),
                                       ("last_filing_date", "last_filing_date", "Last periodic filing (knowledge date)",
                                        "date", "date")):
        if col in sec_cols:
            add(FieldDef(nm, desc, unit, dtype, "identity", "sec", col, "security_id", "static" if dtype == "text" else "snapshot",
                         PIT_STATIC if dtype == "text" else PIT_SNAP, "issuers", None, (), dtype == "text", False, False))
    return out


def _virtual_base(t: str) -> str:
    return {"earn_latest": "earn_events", "pead_latest": "earn_pead"}.get(t, t)


def asdict_shallow(f: FieldDef) -> dict[str, Any]:
    return {k: getattr(f, k) for k in f.__dataclass_fields__}


# ------------------------------------------------------------------------------------------------ YAML publish / load


def _contracts_dir() -> Path:
    from app.core.config import get_settings

    return get_settings().contracts_dir


def to_yaml_doc(fields: list[FieldDef]) -> dict[str, Any]:
    rows = []
    for f in fields:
        d = asdict(f)
        if f.hist is None:
            d.pop("hist")
        else:
            d["hist"] = {k: v for k, v in asdict(f.hist).items() if v not in ("", None)}
        d["aliases"] = list(f.aliases)
        rows.append(d)
    groups: dict[str, int] = {}
    for f in fields:
        groups[f.group] = groups.get(f.group, 0) + 1
    return {
        "version": DICT_VERSION,
        "generated_by": "app.bql.fields (python -m app.bql.fields --write); do not edit by hand",
        "description": ("BQL-lite field dictionary: every queryable serving column with its mnemonic, unit, source table, "
                        "point-in-time rule, frequency and history source. Units: pct = fraction (0.15 = 15%, write 15% "
                        "in queries), pctile = percentile points 0..100, price / currency = trading currency (convertible "
                        "with with(currency=...)), currency_usd = USD."),
        "counts": {"fields": len(fields), "aliases": sum(len(f.aliases) for f in fields),
                   "curated": sum(1 for f in fields if f.curated), "with_history": sum(1 for f in fields if f.hist),
                   "groups": dict(sorted(groups.items()))},
        "fields": rows,
    }


def write_yaml(path: Path | None = None) -> Path:
    p = path or (_contracts_dir() / YAML_NAME)
    doc = to_yaml_doc(generate())
    p.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=140), encoding="utf-8")
    return p


def _from_row(d: dict[str, Any]) -> FieldDef:
    h = d.get("hist")
    return FieldDef(d["mnemonic"], d.get("description", ""), d.get("unit", "number"), d.get("dtype", "number"),
                    d.get("group", "other"), d.get("table"), d.get("column"), d.get("key", "security_id"),
                    d.get("frequency", "snapshot"), d.get("pit", ""), d.get("source", ""),
                    Hist(**h) if h else None, tuple(d.get("aliases") or ()), bool(d.get("static")),
                    bool(d.get("model_based")), bool(d.get("curated")))


@lru_cache(maxsize=4)
def _load(path: str) -> tuple[dict[str, FieldDef], dict[str, str]]:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    by: dict[str, FieldDef] = {}
    alias: dict[str, str] = {}
    for row in doc.get("fields", []):
        f = _from_row(row)
        by[f.mnemonic] = f
    for f in by.values():
        for a in f.aliases:
            alias.setdefault(a.lower(), f.mnemonic)
    return by, alias


def dictionary() -> dict[str, FieldDef]:
    return _load(str(_contracts_dir() / YAML_NAME))[0]


def aliases() -> dict[str, str]:
    return _load(str(_contracts_dir() / YAML_NAME))[1]


def resolve(name: str) -> FieldDef | None:
    d = dictionary()
    f = d.get(name)
    if f is not None:
        return f
    low = name.lower()
    f = d.get(low)
    if f is not None:
        return f
    m = aliases().get(low)
    return d.get(m) if m else None


def suggest(name: str, n: int = 4) -> list[str]:
    pool = list(dictionary()) + list(aliases())
    return difflib.get_close_matches(name.lower(), pool, n=n, cutoff=0.6)


def search(q: str | None = None, group: str | None = None, limit: int = 50, offset: int = 0) -> tuple[int, list[FieldDef]]:
    """Ranked search: exact mnemonic / alias > prefix > word match in mnemonic or description."""
    items = list(dictionary().values())
    if group:
        items = [f for f in items if f.group == group]
    if q:
        t = q.strip().lower()
        scored = []
        for f in items:
            names = [f.mnemonic.lower()] + [a.lower() for a in f.aliases]
            if t in names:
                s = 100
            elif any(n.startswith(t) for n in names):
                s = 80 - min(len(f.mnemonic) - len(t), 20) * 0.5
            elif any(t in n for n in names):
                s = 60
            elif t in f.description.lower():
                s = 40
            else:
                continue
            scored.append((-(s + (5 if f.curated else 0)), f.mnemonic, f))
        scored.sort(key=lambda x: (x[0], x[1]))
        items = [x[2] for x in scored]
    return len(items), items[offset:offset + limit]


def groups() -> list[str]:
    return sorted({f.group for f in dictionary().values()})


def check_published() -> list[str]:
    """Differences between the generator and the published YAML (empty = up to date)."""
    p = _contracts_dir() / YAML_NAME
    if not p.exists():
        return [f"{p} missing"]
    want = to_yaml_doc(generate())
    have = yaml.safe_load(p.read_text(encoding="utf-8"))
    errs = []
    wf = {r["mnemonic"]: r for r in want["fields"]}
    hf = {r["mnemonic"]: r for r in have.get("fields", [])}
    for k in sorted(set(wf) - set(hf)):
        errs.append(f"missing field {k}")
    for k in sorted(set(hf) - set(wf)):
        errs.append(f"stale field {k}")
    for k in sorted(set(wf) & set(hf)):
        if wf[k] != hf[k]:
            errs.append(f"changed field {k}")
    return errs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BQL field dictionary")
    ap.add_argument("--write", action="store_true", help="regenerate contracts/bql_fields.yaml")
    ap.add_argument("--check", action="store_true", help="exit 1 when the published file is stale")
    a = ap.parse_args(argv)
    if a.write:
        p = write_yaml()
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        print(f"wrote {p}: {doc['counts']}")
        return 0
    errs = check_published()
    for e in errs[:50]:
        print(e)
    print("up to date" if not errs else f"{len(errs)} differences")
    return 1 if (a.check and errs) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
