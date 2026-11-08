"""Model Studio dataset engine: standalone core (owner: model-studio).

This module deliberately imports nothing from `app` except the shared PIT helper (with a fallback), so that
`app.modeling.export` can embed its source verbatim in an exported script that rebuilds a dataset straight from the
Parquet lake without the backend installed. Dependencies: duckdb, polars, numpy.

Entry point: build(spec, con, check=None) -> (frame, meta)
  * spec: a *normalized* DatasetSpec dict (app.modeling.datasets.DatasetSpec.model_dump(mode="json"))
  * con:  a DuckDB connection with one view per lake table used (see SOURCES[...]["table"] plus securities_master,
          issuers, universe_membership)

Semantics (also documented in docs/MODEL_STUDIO_PLAN.md):
  * Grid: period ends at the dataset frequency (D = trading days present in the data or Mon-Fri, W = Fridays,
    M = month ends, Q = quarter ends). Only buckets <= the data end are kept (no partial last period).
  * Event sources (prices, valuation, scores, sentiment, insider, technicals) are aggregated into buckets in DuckDB
    (last/first/mean/sum/min/max/median/count) then forward-filled per entity up to `missing.ffill_limit` buckets.
  * Knowledge-dated sources (ratios, fundamentals, macro in "pit" alignment) are sampled point-in-time: the value at
    grid date D is the latest period known with knowledge_date <= D (as-of join on knowledge_date).
  * Macro in "period" alignment is indexed by observation period (latest vintage), which is what a pure macro
    time-series model wants; "auto" = period when every variable is macro, else pit.
  * Per-variable `pre_transforms` run at entity level (before cross-entity aggregation), `transforms` after.
  * Time-series transforms run on the complete entity x grid frame (before survivorship masking); cross-sectional
    transforms (cs_*) only rank/standardize among universe members at each date.
"""

from __future__ import annotations

import calendar
import math
import time
from datetime import date, timedelta
from typing import Any, Callable

import duckdb
import polars as pl

try:  # inside the backend: the shared point-in-time helper (ARCHITECTURE section 7)
    from app.normalize.pit import asof_join as _asof_join
except ImportError:  # exported standalone script: app.normalize.pit.asof_join is embedded above this module
    _asof_join = asof_join  # type: ignore[name-defined]  # noqa: F821

CORE_VERSION = "1.0"

# ------------------------------------------------------------------------------------------------ sources

#: source id -> how to read it. `fields` lists default aggregations for well-known columns; any numeric column of the
#: table is accepted for wide sources. Long sources take the key value (metric/factor/series id) as `field`.
SOURCES: dict[str, dict[str, Any]] = {
    "prices": {"table": "prices_daily", "entity": "security", "kind": "event", "layout": "wide", "date_col": "date",
               "native_freq": "D", "title": "Daily prices & returns",
               "derived": {"ret": "adj_close", "log_ret": "adj_close"},
               "default_agg": {"volume": "sum", "dollar_volume_usd": "sum", "trades": "sum"},
               "exclude": ["security_id", "listing_id", "date", "region", "year", "currency", "source_id", "dq_flags",
                           "ingested_at"]},
    "valuation": {"table": "valuation_daily", "entity": "security", "kind": "event", "layout": "wide",
                  "date_col": "date", "native_freq": "D", "title": "Daily valuation multiples",
                  "exclude": ["security_id", "issuer_id", "date", "year", "size_bucket", "fundamentals_knowledge_date",
                              "computed_at"]},
    "factor_scores": {"table": "factor_scores", "entity": "security", "kind": "event", "layout": "long",
                      "date_col": "as_of_date", "key_col": "factor_id", "value_col": "score", "native_freq": "M",
                      "title": "Factor scores (0-100)"},
    "composite_scores": {"table": "composite_scores", "entity": "security", "kind": "event", "layout": "long",
                         "date_col": "as_of_date", "key_col": "profile_id", "value_col": "score", "native_freq": "M",
                         "title": "Composite scores by profile (0-100)"},
    "sentiment": {"table": "sentiment_daily", "entity": "security", "kind": "event", "layout": "wide",
                  "date_col": "date", "native_freq": "D", "title": "News sentiment (daily)",
                  "exclude": ["date", "year", "security_id", "issuer_id", "computed_at"]},
    "insider": {"table": "insider_signals", "entity": "security", "kind": "event", "layout": "wide",
                "date_col": "as_of_date", "native_freq": "D", "title": "Insider signals",
                "exclude": ["as_of_date", "year", "security_id", "issuer_id", "top_transactions", "computed_at",
                            "run_id"]},
    "technicals": {"table": "tech_setups", "entity": "security", "kind": "event", "layout": "wide",
                   "date_col": "as_of_date", "native_freq": "D", "title": "Technical setups (latest + month ends)",
                   "exclude": ["as_of_date", "security_id", "trend", "structure_state", "last_break_kind",
                               "last_break_dir", "last_break_date", "bias", "score_components", "note", "computed_at",
                               "support_zone_id", "resistance_zone_id"]},
    "ratios": {"table": "ratios", "entity": "issuer", "kind": "knowledge", "layout": "long", "key_col": "metric_id",
               "value_col": "value", "period_col": "period_end", "knowledge_col": "knowledge_date", "native_freq": "Q",
               "options": {"period_kind": "TTM"}, "title": "Financial ratios (point-in-time)"},
    "fundamentals": {"table": "fundamentals_wide", "entity": "issuer", "kind": "knowledge", "layout": "wide",
                     "period_col": "period_end", "knowledge_col": "knowledge_date", "native_freq": "Q",
                     "options": {"period_kind": "TTM", "currency_basis": "USD"},
                     "title": "Fundamentals line items (point-in-time)",
                     "exclude": ["issuer_id", "currency_basis", "period_kind", "fiscal_year", "fiscal_period",
                                 "period_start", "period_end", "currency", "gaap", "knowledge_date",
                                 "knowledge_date_estimated", "coverage", "dq_score", "computed_at"]},
    "factor_ls": {"table": "factor_scores", "entity": "factor", "kind": "factor", "layout": "long",
                  "date_col": "as_of_date", "key_col": "factor_id", "value_col": "score", "native_freq": "M",
                  "options": {"quantiles": 5},
                  "title": "Long-short factor returns built from factor scores (top minus bottom quantile, "
                           "sorted on the previous period's score, equal weight, over the entity filter)"},
    "macro": {"table": "macro_observations", "entity": "macro", "kind": "knowledge", "layout": "long",
              "key_col": "series_id", "value_col": "value", "period_col": "date", "knowledge_col": "knowledge_date",
              "native_freq": "M", "title": "Macro series (vintage-aware)"},
    # CFTC Commitments of Traders positioning (official; weekly, PIT on the release date). A time series like macro:
    # field = "<market_code>:<report_type>:<category>:<metric>", e.g. "067651:disagg_fut:m_money:cot_index_3y".
    "cftc": {"table": "cftc_signals", "entity": "macro", "kind": "knowledge", "layout": "long", "key_col": "market_code",
             "period_col": "report_date", "knowledge_col": "knowledge_date", "native_freq": "W",
             "field_split": ["market_code", "report_type", "category"],
             "metrics": ["net", "net_pct_oi", "long_share", "chg_1w", "chg_4w", "chg_13w", "flow_z", "cot_index_1y",
                         "cot_index_3y", "z_1y", "z_3y", "pct_rank_3y", "divergence_13w", "divergence_26w"],
             "title": "CFTC positioning (COT, weekly; field = market:report_type:category:metric)"},
    # Cross-asset signals (roadmap #5; docs/CROSS_ASSET_DEFINITIONS.md): daily per-asset trend / vol / carry / curve /
    # value / z-scores + the CFTC positioning join, PIT on knowledge_date. field = "<asset_id>:<metric>", e.g. "CL:carry".
    "cross_asset": {"table": "cross_asset_signals", "entity": "macro", "kind": "knowledge", "layout": "long",
                    "key_col": "asset_id", "period_col": "date", "knowledge_col": "knowledge_date", "native_freq": "D",
                    "field_split": ["asset_id"],
                    "metrics": ["price", "trend_signal", "mom_1m", "mom_3m", "mom_12m", "mom_12_1", "rv_1m", "rv_3m", "iv",
                                "iv_rv_ratio", "carry", "carry_curve", "carry_12m", "carry_fundamental", "curve_slope",
                                "dividend_yield", "funding_rate", "value", "real_rate_diff", "z_mom_12m", "z_carry",
                                "z_value", "z_rv", "z_iv_rv", "z_curve_slope", "cot_index_3y", "cot_z_3y", "cot_net_pct_oi"],
                    "title": "Cross-asset signals (daily; field = asset:metric; trend / curve shape / value / z model-based)"},
    # Smart money (roadmap #7; docs/SMART_MONEY.md): weekly per-security composite of 13F flows, smart-cohort flows,
    # insider, FINRA short interest and EU / UK short disclosures (all inputs known on as_of_date; MODEL-BASED score).
    "smart_money": {"table": "smart_money_signals", "entity": "security", "kind": "event", "layout": "wide",
                    "date_col": "as_of_date", "native_freq": "W",
                    "title": "Smart-money signals (weekly; 13F / insider / short interest / EU shorts; score model-based)",
                    "exclude": ["as_of_date", "year", "security_id", "issuer_id", "region", "sector_id", "knowledge_date",
                                "inst_period_end", "si_settlement_date", "computed_at", "run_id"]},
    # Technicals v3 #1 (docs/TECHNICALS_V3_VALIDATION.md): daily point-in-time evidence-based technical factors (52-week
    # high, 12-1 / residual / industry momentum, reversal, MAX, idiosyncratic vol), raw + percentile within region x
    # sector among liquid names, and the composite of the gate-passing factors; history 2004 -> (US / EU / UK / JP).
    "tech_factors": {"table": "tech_factors", "entity": "security", "kind": "event", "layout": "wide",
                     "date_col": "as_of_date", "native_freq": "D",
                     "title": "Technical factors (evidence-based, daily; 52w high, momentum, residual momentum, reversal, MAX, IVOL)",
                     "exclude": ["as_of_date", "year", "security_id", "region", "sector_id", "size_bucket", "pct_scope", "liquid",
                                 "computed_at"]},
}

REFERENCE_TABLES = ("securities_master", "issuers", "universe_membership")
LEAD_OPS = frozenset({"lead", "fwd_sum", "fwd_compound"})
CS_OPS = frozenset({"cs_rank", "cs_zscore", "cs_demean"})
FREQ_DAYS = {"D": 1, "W": 7, "M": 30.44, "Q": 91.3}
AGG_SQL = {
    "last": "arg_max({v}, {d}) FILTER (WHERE {v} IS NOT NULL)",
    "first": "arg_min({v}, {d}) FILTER (WHERE {v} IS NOT NULL)",
    "mean": "avg({v})", "sum": "sum({v})", "min": "min({v})", "max": "max({v})", "median": "median({v})",
    "count": "count({v})",
}


class SpecError(ValueError):
    """Invalid or unsatisfiable dataset spec (maps to HTTP 422)."""


def tables_needed(spec: dict[str, Any]) -> list[str]:
    out = {SOURCES[v["source"]]["table"] for v in spec["variables"] if v["source"] in SOURCES}
    if any(SOURCES[v["source"]]["entity"] != "macro" for v in spec["variables"] if v["source"] in SOURCES):
        out.update(REFERENCE_TABLES)
    if any(SOURCES.get(v["source"], {}).get("kind") == "factor" for v in spec["variables"]):
        out.add("prices_daily")
    return sorted(out)


def uses_future(var: dict[str, Any]) -> bool:
    return any(t["op"] in LEAD_OPS for t in (var.get("pre_transforms") or []) + (var.get("transforms") or []))


# ------------------------------------------------------------------------------------------------ calendar


def bucket_of(d: date, freq: str) -> date:
    if freq == "D":
        return d
    if freq == "W":
        return d - timedelta(days=d.weekday()) + timedelta(days=4)
    if freq == "M":
        return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])
    if freq == "Q":
        m = ((d.month - 1) // 3) * 3 + 3
        return date(d.year, m, calendar.monthrange(d.year, m)[1])
    raise SpecError(f"unknown frequency {freq!r}")


def next_bucket(b: date, freq: str) -> date:
    if freq == "D":
        return b + timedelta(days=1)
    if freq == "W":
        return b + timedelta(days=7)
    if freq == "M":
        return bucket_of(b + timedelta(days=1), "M")
    return bucket_of(bucket_of(b + timedelta(days=1), "M") + timedelta(days=62), "Q")


def prev_bucket(b: date, freq: str) -> date:
    if freq == "D":
        return b - timedelta(days=1)
    if freq == "W":
        return b - timedelta(days=7)
    if freq == "M":
        return bucket_of(b.replace(day=1) - timedelta(days=1), "M")
    return bucket_of(date(b.year, b.month - 2, 1) - timedelta(days=1), "Q")


def bucket_sql(col: str, freq: str) -> str:
    c = f"CAST({col} AS DATE)"
    if freq == "D":
        return c
    if freq == "W":
        return f"CAST(date_trunc('week', {c}) + INTERVAL 4 DAY AS DATE)"
    if freq == "M":
        return f"CAST(last_day({c}) AS DATE)"
    if freq == "Q":
        return f"CAST(last_day(date_trunc('quarter', {c}) + INTERVAL 2 MONTH) AS DATE)"
    raise SpecError(f"unknown frequency {freq!r}")


def make_grid(start: date, end: date, freq: str, trading_days: list[date] | None = None) -> list[date]:
    if freq == "D":
        if trading_days:
            return sorted(d for d in set(trading_days) if start <= d <= end)
        out, d = [], start
        while d <= end:
            if d.weekday() < 5:
                out.append(d)
            d += timedelta(days=1)
        return out
    out, b = [], bucket_of(start, freq)
    while b <= end:
        out.append(b)
        b = next_bucket(b, freq)
    return out


def _last_business_day(b: date) -> date:
    while b.weekday() >= 5:
        b -= timedelta(days=1)
    return b


def _q(v: Any) -> str:
    return "'" + str(v).replace("'", "''") + "'"


def _ident(c: str) -> str:
    return '"' + c.replace('"', '""') + '"'


def _retry(fn: Callable[[], Any], attempts: int = 4) -> Any:
    """Lake partitions are swapped atomically by writers; a scan can race a rename on Windows. Retry briefly."""
    for i in range(attempts):
        try:
            return fn()
        except (duckdb.IOException, duckdb.InvalidInputException) as e:
            if i == attempts - 1 or "No files found" in str(e):
                raise
            time.sleep(0.3 * (i + 1))
    return None


# ------------------------------------------------------------------------------------------------ entities


def resolve_entities(con: duckdb.DuckDBPyConnection, flt: dict[str, Any], start: date | None, end: date | None,
                     max_entities: int) -> tuple[pl.DataFrame, pl.DataFrame | None, list[str]]:
    """Securities matching the filter -> (entities, membership intervals or None, warnings)."""
    warns: list[str] = []
    ents = _retry(lambda: con.execute(
        "SELECT s.security_id, s.issuer_id, s.display_ticker AS ticker, s.name, i.sector_id, "
        "CAST(i.region AS VARCHAR) AS region, i.country, COALESCE(s.is_primary_security, TRUE) AS is_primary "
        "FROM securities_master s LEFT JOIN issuers i USING (issuer_id)").pl())
    members = None
    explicit = False
    for col, key in (("security_id", "security_ids"), ("issuer_id", "issuer_ids"), ("ticker", "tickers")):
        vals = flt.get(key)
        if vals:
            explicit = True
            vals = [str(v).upper() for v in vals] if col == "ticker" else list(vals)
            expr = pl.col(col).str.to_uppercase().is_in(vals) if col == "ticker" else pl.col(col).is_in(vals)
            ents = ents.filter(expr)
    universe = flt.get("universe")
    if not explicit and not universe and not any(flt.get(k) for k in ("sectors", "regions", "countries")):
        universe = "full"
        warns.append("no entity filter given: using universe 'full'")
    if universe:
        lo = start or date(1900, 1, 1)
        hi = end or date(2100, 1, 1)
        members = _retry(lambda: con.execute(
            "SELECT security_id, valid_from, valid_to FROM universe_membership WHERE universe_id = ? "
            "AND (valid_to IS NULL OR valid_to >= ?)", [universe, lo]).pl())
        if members.is_empty():
            raise SpecError(f"universe {universe!r} has no members in the window")
        # Membership history starts when the universe was first built: rows from that initial snapshot carry the
        # build date as valid_from, not a real inclusion date. Treat them as members since inception (exits via
        # valid_to stay point-in-time; later additions keep their real valid_from).
        first = _retry(lambda: con.execute("SELECT min(valid_from) FROM universe_membership WHERE universe_id = ?",
                                           [universe]).fetchone()[0])
        # (a 30-day grace window covers universes built/expanded in several passes right after creation)
        members = members.with_columns(pl.when(pl.col("valid_from") <= first + timedelta(days=30))
                                       .then(pl.lit(date(1900, 1, 1))).otherwise(pl.col("valid_from")).alias("valid_from"))
        members = members.filter(pl.col("valid_from") <= hi)
        if members.is_empty():
            raise SpecError(f"universe {universe!r} has no members in the window")
        if start is None or (first and first > start):
            warns.append(f"universe {universe!r} membership history starts {first}: earlier dates use the initial "
                         "member list (survivorship bias possible before that date)")
        ents = ents.filter(pl.col("security_id").is_in(members["security_id"].unique().to_list()))
    if flt.get("sectors"):
        ents = ents.filter(pl.col("sector_id").is_in(list(flt["sectors"])))
    if flt.get("regions"):
        ents = ents.filter(pl.col("region").is_in(list(flt["regions"])))
    if flt.get("countries"):
        ents = ents.filter(pl.col("country").is_in([c.upper() for c in flt["countries"]]))
    if flt.get("primary_only", True) and not explicit:
        ents = ents.filter(pl.col("is_primary"))
    ents = ents.unique("security_id").sort("security_id")
    if ents.is_empty():
        raise SpecError("entity filter matches no securities")
    if ents.height > max_entities:
        warns.append(f"{ents.height} entities match; truncated to max_entities={max_entities}")
        ents = ents.head(max_entities)
    if members is not None:
        members = members.filter(pl.col("security_id").is_in(ents["security_id"].to_list()))
    return ents.drop("is_primary"), members, warns


def _member_mask(frame: pl.DataFrame, members: pl.DataFrame | None) -> pl.DataFrame:
    """Adds boolean `_member`: entity in the universe at that date (survivorship-free, point-in-time)."""
    if members is None:
        return frame.with_columns(pl.lit(True).alias("_member"))
    j = frame.select("entity_id", "date").unique().join(
        members.rename({"security_id": "entity_id"}), on="entity_id", how="inner")
    j = j.filter((pl.col("valid_from") <= pl.col("date")) & (pl.col("valid_to").is_null() | (pl.col("valid_to") >= pl.col("date"))))
    ok = j.select("entity_id", "date").unique().with_columns(pl.lit(True).alias("_member"))
    return frame.join(ok, on=["entity_id", "date"], how="left").with_columns(pl.col("_member").fill_null(False))


# ------------------------------------------------------------------------------------------------ loading


def _macro_alignment(spec: dict[str, Any]) -> str:
    a = spec.get("macro_alignment") or "auto"
    if a != "auto":
        return a
    return "period" if all(SOURCES[v["source"]]["entity"] == "macro" for v in spec["variables"]) else "pit"


def _warmup_days(spec: dict[str, Any]) -> int:
    n = 2
    for v in spec["variables"]:
        for t in (v.get("pre_transforms") or []) + (v.get("transforms") or []):
            n += int(t.get("k") or 0) + int(t.get("window") or 0)
    return int(n * FREQ_DAYS[spec["frequency"]] * 1.2) + 10


#: tables hive-partitioned by year(date) (contracts/schemas: prices_daily [region, year], valuation_daily [year])
YEAR_PARTITIONED = frozenset({"prices_daily", "valuation_daily"})
INLINE_IDS_MAX = 400


def _has_col(con: duckdb.DuckDBPyConnection, table: str, col: str) -> bool:
    try:
        return col in [d[0] for d in con.execute(f"SELECT * FROM {_ident(table)} LIMIT 0").description]
    except duckdb.Error:
        return False


def _load_event(con, var: dict[str, Any], src: dict[str, Any], freq: str, ids: list[str], lo: date | None,
                hi: date | None, sqls: list[str]) -> pl.DataFrame:
    field = var["field"]
    dcol = src["date_col"]
    base_field = src.get("derived", {}).get(field, field)
    agg = var.get("agg") or ("last" if field in src.get("derived", {}) else src.get("default_agg", {}).get(field, "last"))
    if agg not in AGG_SQL:
        raise SpecError(f"{var['name']}: unknown aggregation {agg!r}")
    if src["layout"] == "long":
        vexpr = f"TRY_CAST({_ident(src['value_col'])} AS DOUBLE)"
        where = [f"{_ident(src['key_col'])} = {_q(field)}"]
    else:
        vexpr = f"TRY_CAST({_ident(base_field)} AS DOUBLE)"
        where = []
    # predicate pushdown (S1.5): a short id list inline (Parquet min/max stats skip row groups; the files are sorted
    # by security_id), the hive `year` partitions of the date window pruned (year = year(date) in these tables)
    if 0 < len(ids) <= INLINE_IDS_MAX:
        where.append(f"security_id IN ({', '.join(_q(i) for i in ids)})")
    else:
        where.append("security_id IN (SELECT entity_id FROM _studio_ids)")
    if lo:
        where.append(f"{_ident(dcol)} >= DATE {_q(lo.isoformat())}")
    if hi:
        where.append(f"{_ident(dcol)} <= DATE {_q(hi.isoformat())}")
    if src["table"] in YEAR_PARTITIONED and dcol == "date" and _has_col(con, src["table"], "year"):
        if lo:
            where.append(f"year >= {int(lo.year)}")
        if hi:
            where.append(f"year <= {int(hi.year)}")
    b = bucket_sql(_ident(dcol), freq)
    sql = (f"SELECT security_id AS entity_id, {b} AS date, "
           f"{AGG_SQL[agg].format(v=vexpr, d=_ident(dcol))} AS v, max({_ident(dcol)}) AS _raw_max "
           f"FROM {_ident(src['table'])} WHERE {' AND '.join(where)} GROUP BY 1, 2")
    sqls.append(sql)
    con.register("_studio_ids", pl.DataFrame({"entity_id": ids}).to_arrow())
    try:
        out = _retry(lambda: con.execute(sql).pl())
    except duckdb.BinderException as e:
        raise SpecError(f"{var['name']}: field {base_field!r} not found in {src['table']} ({str(e).splitlines()[0]})") from e
    finally:
        con.unregister("_studio_ids")
    return out.with_columns(pl.col("date").cast(pl.Date), pl.col("v").cast(pl.Float64))


def _load_knowledge(con, var: dict[str, Any], src: dict[str, Any], keys: list[str], hi: date | None,
                    sqls: list[str]) -> pl.DataFrame:
    """Rows (key, period, knowledge_date, v) for PIT sampling."""
    opts = {**src.get("options", {}), **(var.get("options") or {})}
    kcol, pcol = src["knowledge_col"], src["period_col"]
    if src["entity"] == "macro" and src.get("field_split"):
        parts = str(var["field"]).split(":")
        if len(parts) != len(src["field_split"]) + 1 or parts[-1] not in src["metrics"]:
            raise SpecError(f"{var['name']}: field must be {':'.join(src['field_split'])}:<metric> with metric in "
                            f"{', '.join(src['metrics'])}")
        keycol = _q(var["field"])
        where = [f"{_ident(c)} = {_q(v)}" for c, v in zip(src["field_split"], parts[:-1], strict=True)]
        vexpr = f"TRY_CAST({_ident(parts[-1])} AS DOUBLE)"
    elif src["entity"] == "macro":
        keycol = "series_id"
        where = [f"series_id = {_q(var['field'])}"]
        vexpr = "TRY_CAST(value AS DOUBLE)"
    else:
        keycol = "issuer_id"
        where = ["issuer_id IN (SELECT k FROM _studio_keys)"]
        for o in ("period_kind", "currency_basis"):
            if o in opts and opts[o] and (o != "currency_basis" or src["table"] == "fundamentals_wide"):
                where.append(f"{o} = {_q(opts[o])}")
        if src["layout"] == "long":
            where.append(f"{_ident(src['key_col'])} = {_q(var['field'])}")
            vexpr = f"TRY_CAST({_ident(src['value_col'])} AS DOUBLE)"
        else:
            vexpr = f"TRY_CAST({_ident(var['field'])} AS DOUBLE)"
    if hi:
        where.append(f"{_ident(kcol)} <= DATE {_q(hi.isoformat())}")
    sql = (f"SELECT {keycol} AS k, CAST({_ident(pcol)} AS DATE) AS period, CAST({_ident(kcol)} AS DATE) AS kd, "
           f"{vexpr} AS v FROM {_ident(src['table'])} WHERE {' AND '.join(where)} AND {vexpr} IS NOT NULL")
    sqls.append(sql)
    if src["entity"] != "macro":
        con.register("_studio_keys", pl.DataFrame({"k": keys}).to_arrow())
    try:
        out = _retry(lambda: con.execute(sql).pl())
    except duckdb.BinderException as e:
        raise SpecError(f"{var['name']}: field {var['field']!r} not found in {src['table']} ({str(e).splitlines()[0]})") from e
    finally:
        if src["entity"] != "macro":
            con.unregister("_studio_keys")
    return out.with_columns(pl.col("period").cast(pl.Date), pl.col("kd").cast(pl.Date), pl.col("v").cast(pl.Float64))


def _latest_known_stream(raw: pl.DataFrame) -> pl.DataFrame:
    """Knowledge events -> (k, kd, v, period): at each knowledge date the value of the newest period known so far.

    Revisions of older periods are ignored (they do not change "the latest known value"); a revision of the newest
    period replaces it. Ties on knowledge date keep the newest period."""
    if raw.is_empty():
        return raw.select("k", "kd", "v", "period")
    df = raw.sort(["k", "kd", "period"])
    df = df.with_columns(pl.col("period").cum_max().over("k").alias("_pmax"))
    df = df.filter(pl.col("period") == pl.col("_pmax"))
    df = df.unique(subset=["k", "kd"], keep="last", maintain_order=True)
    return df.select("k", "kd", "v", "period")


def _pit_sample(stream: pl.DataFrame, left: pl.DataFrame, key_col: str, max_stale_days: int | None) -> pl.Series:
    """For each left row (key_col, date) the latest-known value as of date (shared PIT as-of join)."""
    if stream.is_empty():
        return pl.Series("v", [None] * left.height, dtype=pl.Float64)
    right = stream.rename({"k": key_col, "kd": "knowledge_date"})
    lft = left.select(key_col, "date").with_row_index("_row")
    j = _asof_join(lft, right, "date", by=[key_col], knowledge_col="knowledge_date")
    if max_stale_days:
        j = j.with_columns(pl.when((pl.col("date") - pl.col("period")).dt.total_days() <= max_stale_days)
                           .then(pl.col("v")).otherwise(None).alias("v"))
    return j.sort("_row")["v"].cast(pl.Float64)


# ------------------------------------------------------------------------------------------------ transforms


def _time_op(e: pl.Expr, t: dict[str, Any], over: str | None) -> pl.Expr:
    op = t["op"]
    k = int(t.get("k") or 1)
    w = int(t.get("window") or 12)
    mp = int(t.get("min_periods") or w)

    def ov(x: pl.Expr) -> pl.Expr:
        return x.over(over) if over else x

    if op == "lag":
        return ov(e.shift(k))
    if op == "lead":
        return ov(e.shift(-k))
    if op == "diff":
        return e - ov(e.shift(k))
    if op == "log":
        return pl.when(e > 0).then(e.log()).otherwise(None)
    if op == "log_diff":
        lg = pl.when(e > 0).then(e.log()).otherwise(None)
        return lg - ov(lg.shift(k))
    if op == "pct_change":
        prev = ov(e.shift(k))
        return pl.when(prev != 0).then(e / prev - 1.0).otherwise(None)
    if op == "rolling_mean":
        return ov(e.rolling_mean(w, min_samples=mp))
    if op == "rolling_std":
        return ov(e.rolling_std(w, min_samples=mp))
    if op == "rolling_sum":
        return ov(e.rolling_sum(w, min_samples=mp))
    if op == "rolling_max":
        return ov(e.rolling_max(w, min_samples=mp))
    if op == "rolling_min":
        return ov(e.rolling_min(w, min_samples=mp))
    if op == "rolling_z":
        m, s = ov(e.rolling_mean(w, min_samples=mp)), ov(e.rolling_std(w, min_samples=mp))
        return pl.when(s > 0).then((e - m) / s).otherwise(None)
    if op == "drawdown":
        mx = ov(e.rolling_max(w, min_samples=1))
        return pl.when(mx > 0).then(e / mx - 1.0).otherwise(None)
    if op == "fwd_sum":
        return ov(e.rolling_sum(k, min_samples=k).shift(-k))
    if op == "fwd_compound":
        lg = (1.0 + e).log()
        return ov(lg.rolling_sum(k, min_samples=k).shift(-k)).exp() - 1.0
    if op == "abs":
        return e.abs()
    if op == "square":
        return e * e
    if op == "neg":
        return -e
    if op == "scale":
        return e * float(t.get("factor") if t.get("factor") is not None else 1.0)
    if op == "clip":
        return e.clip(t.get("lower"), t.get("upper"))
    if op == "binarize":
        thr = float(t.get("threshold") or 0.0)
        return pl.when(e.is_null()).then(None).when(e > thr).then(1.0).otherwise(0.0)
    raise SpecError(f"unknown transform {op!r}")


def _apply_transforms(df: pl.DataFrame, col: str, transforms: list[dict[str, Any]], entity: bool,
                      member_col: str | None) -> pl.DataFrame:
    """Apply a transform chain to `col` of a frame sorted by (entity_id, date) / date."""
    over = "entity_id" if entity else None
    for t in transforms or []:
        op = t["op"]
        e = pl.col(col)
        if op in CS_OPS or (op in ("winsorize", "standardize") and (t.get("scope") or ("cross_section" if entity else "all")) == "cross_section"):
            if not entity:
                if op in CS_OPS:
                    raise SpecError(f"{col}: cross-sectional transform {op} needs a panel / cross-section dataset")
            valid = pl.col(member_col) & e.is_not_null() if member_col else e.is_not_null()
            ex = pl.when(valid).then(e).otherwise(None)
            if op == "cs_rank":
                n = ex.count().over("date")
                expr = pl.when(n > 1).then((ex.rank("average").over("date") - 1) / (n - 1)).otherwise(None)
            elif op in ("cs_zscore", "standardize"):
                sd = ex.std().over("date")
                expr = pl.when(sd > 0).then((ex - ex.mean().over("date")) / sd).otherwise(None)
            elif op == "cs_demean":
                expr = ex - ex.mean().over("date")
            else:  # winsorize per date
                lo_q, hi_q = float(t.get("lower") or 0.01), float(t.get("upper") or 0.99)
                expr = ex.clip(ex.quantile(lo_q).over("date"), ex.quantile(hi_q).over("date"))
            df = df.with_columns(expr.alias(col))
        elif op in ("winsorize", "standardize"):
            scope = t.get("scope") or "all"
            grp = over if (scope == "entity" and entity) else None
            if op == "winsorize":
                lo_q, hi_q = float(t.get("lower") or 0.01), float(t.get("upper") or 0.99)
                lo, hi = e.quantile(lo_q), e.quantile(hi_q)
                expr = e.clip(lo.over(grp) if grp else lo, hi.over(grp) if grp else hi)
            else:
                m, s = e.mean(), e.std()
                if grp:
                    m, s = m.over(grp), s.over(grp)
                expr = pl.when(s > 0).then((e - m) / s).otherwise(None)
            df = df.with_columns(expr.alias(col))
        else:
            df = df.with_columns(_time_op(e, t, over).alias(col))
    return df


# ------------------------------------------------------------------------------------------------ build


def build(spec: dict[str, Any], con: duckdb.DuckDBPyConnection, check: Callable[[], None] | None = None,
          today: date | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Build the dataset described by a normalized spec. Returns (frame, meta)."""
    t0 = time.perf_counter()
    check = check or (lambda: None)
    today = today or date.today()
    kind, freq = spec["kind"], spec["frequency"]
    if not spec.get("variables"):
        raise SpecError("dataset has no variables")
    names = [v["name"] for v in spec["variables"]]
    if len(set(names)) != len(names):
        raise SpecError("variable names must be unique")
    for v in spec["variables"]:
        if v["source"] not in SOURCES:
            raise SpecError(f"{v['name']}: unknown source {v['source']!r}")
    warnings: list[str] = []
    sqls: list[str] = []
    s_start = date.fromisoformat(spec["start"]) if spec.get("start") else None
    s_end = date.fromisoformat(spec["end"]) if spec.get("end") else None
    if kind == "cross_section":
        as_of = date.fromisoformat(spec["as_of"]) if spec.get("as_of") else (s_end or today)
        s_end = as_of
        s_start = as_of - timedelta(days=int(int(spec.get("lookback") or 13) * FREQ_DAYS[freq]) + 5)
    align = _macro_alignment(spec)
    entity_vars = [v for v in spec["variables"] if SOURCES[v["source"]]["entity"] != "macro"]
    if kind in ("panel", "cross_section") and not [v for v in entity_vars if SOURCES[v["source"]]["entity"] != "factor"]:
        raise SpecError(f"a {kind} dataset needs at least one company-level variable")
    warm = _warmup_days(spec)
    lo = (s_start - timedelta(days=warm)) if s_start else None
    hi = s_end
    # forward-looking targets (lead / fwd_*) read data beyond an explicit window end (the sample dates stay in-window)
    lead_k = max([sum(int(t.get("k") or 1) for t in (v.get("pre_transforms") or []) + (v.get("transforms") or [])
                      if t["op"] in LEAD_OPS) for v in spec["variables"]] + [0])
    hi_load = (s_end + timedelta(days=int(lead_k * FREQ_DAYS[freq] * 1.2) + 10)) if (s_end and lead_k) else s_end

    # ---- entities (time series: each variable may carry its own filter, aggregated across its entities)
    ent_sets: dict[str, tuple[pl.DataFrame, pl.DataFrame | None]] = {}
    var_ent: dict[str, str] = {}

    def _ents_for(flt: dict[str, Any]) -> str:
        key = repr(sorted((k, repr(v)) for k, v in (flt or {}).items()))
        if key not in ent_sets:
            e, m, w = resolve_entities(con, flt or {}, s_start, s_end, int(spec.get("max_entities") or 3000))
            warnings.extend(x for x in w if x not in warnings)
            ent_sets[key] = (e, m if spec.get("pit_universe", True) else None)
        return key

    ents, members = None, None
    if entity_vars:
        if kind != "timeseries" or any(not v.get("entities") for v in entity_vars):
            ents, members = ent_sets[_ents_for(spec.get("entities") or {})]
        for v in entity_vars:
            if v.get("entities") and kind != "timeseries":
                warnings.append(f"{v['name']}: variable-level entity filters apply to time-series datasets only (ignored)")
            var_ent[v["name"]] = _ents_for(v["entities"]) if (v.get("entities") and kind == "timeseries") \
                else _ents_for(spec.get("entities") or {})
    check()

    # ---- load raw per variable
    raw: dict[str, pl.DataFrame] = {}
    avail: dict[str, tuple[date | None, date | None]] = {}
    trading_days: set[date] = set()
    for v in spec["variables"]:
        src = SOURCES[v["source"]]
        v_ents = ent_sets[var_ent[v["name"]]][0] if v["name"] in var_ent else None
        if src["kind"] == "factor":
            ids = v_ents["security_id"].to_list()
            sc = _load_event(con, {"name": v["name"], "field": v["field"], "agg": "last"}, src, freq, ids, lo, hi_load, sqls)
            px = _load_event(con, {"name": v["name"], "field": "adj_close", "agg": "last"}, SOURCES["prices"], freq, ids,
                             lo, hi_load, sqls)
            raw[v["name"]] = (sc, px)
            if sc.is_empty() or px.is_empty():
                avail[v["name"]] = (None, None)
            else:
                rmax = px["_raw_max"].max()
                last_b = bucket_of(rmax, freq)
                avail[v["name"]] = (max(sc["date"].min(), px["date"].min()),
                                    last_b if rmax >= _last_business_day(last_b) else rmax)
                if freq == "D":
                    trading_days.update(px["date"].unique().to_list())
        elif src["kind"] == "event":
            df = _load_event(con, v, src, freq, v_ents["security_id"].to_list(), lo, hi_load, sqls)
            raw[v["name"]] = df
            if df.is_empty():
                avail[v["name"]] = (None, None)
            else:
                first = df["date"].min()
                rmax = df["_raw_max"].max()
                rmax = rmax.date() if hasattr(rmax, "date") and not isinstance(rmax, date) else rmax
                last_b = bucket_of(rmax, freq)
                last = last_b if rmax >= _last_business_day(last_b) else rmax
                avail[v["name"]] = (first, last)
                if freq == "D":
                    trading_days.update(df["date"].unique().to_list())
        elif src["entity"] == "macro" and align == "period" and not src.get("field_split"):
            sql = (f"WITH latest AS (SELECT date AS d, arg_max(TRY_CAST(value AS DOUBLE), knowledge_date) AS v "
                   f"FROM macro_observations WHERE series_id = {_q(v['field'])} GROUP BY 1) "
                   f"SELECT {bucket_sql('d', freq)} AS date, "
                   f"{AGG_SQL[v.get('agg') or 'last'].format(v='v', d='d')} AS v FROM latest GROUP BY 1 ORDER BY 1")
            sqls.append(sql)
            df = _retry(lambda sql=sql: con.execute(sql).pl()).with_columns(pl.col("date").cast(pl.Date),
                                                                          pl.col("v").cast(pl.Float64))
            raw[v["name"]] = df
            avail[v["name"]] = (df["date"].min(), df["date"].max()) if not df.is_empty() else (None, None)
        else:
            keys = v_ents["issuer_id"].drop_nulls().unique().to_list() if src["entity"] == "issuer" else []
            df = _load_knowledge(con, v, src, keys, hi_load, sqls)
            raw[v["name"]] = _latest_known_stream(df)
            avail[v["name"]] = (df["kd"].min(), None) if not df.is_empty() else (None, None)
        if avail[v["name"]][0] is None:
            raise SpecError(f"{v['name']}: no data for {v['source']}.{v['field']} with the given filters")
        check()

    # ---- grid
    starts = [a[0] for a in avail.values() if a[0] is not None]
    ends = [a[1] for a in avail.values() if a[1] is not None]
    g_start = s_start or max(starts)
    g_end = s_end or min(ends + [today])
    if g_end < g_start:
        raise SpecError(f"empty date window: {g_start} > {g_end} (variables do not overlap in time)")
    grid = make_grid(bucket_of(g_start, freq) if freq != "D" else g_start, g_end, freq, sorted(trading_days) or None)
    # warm-up rows before the window so lags/rolling windows have history
    n_warm = int(math.ceil(warm / FREQ_DAYS[freq]))
    pre: list[date] = []
    if freq == "D":
        pre = sorted(d for d in trading_days if d < g_start)[-n_warm:] if trading_days else []
    else:
        b = bucket_of(g_start, freq)
        for _ in range(n_warm):
            b = prev_bucket(b, freq)
            pre.insert(0, b)
    post: list[date] = []
    if lead_k and s_end and grid:
        if freq == "D":
            post = sorted(d for d in trading_days if d > grid[-1])[:lead_k] if trading_days else []
        else:
            b = grid[-1]
            for _ in range(lead_k):
                b = next_bucket(b, freq)
                if b > today:
                    break
                post.append(b)
    full_grid = pre + [d for d in grid if d not in set(pre)] + post
    if not grid:
        raise SpecError("the date window contains no complete period at this frequency")
    gdf = pl.DataFrame({"date": full_grid}, schema={"date": pl.Date})
    n_ent = ents.height if ents is not None else 1
    max_rows = int(spec.get("max_rows") or 5_000_000)
    if entity_vars and kind != "timeseries" and n_ent * len(grid) > max_rows:
        raise SpecError(f"dataset would have {n_ent * len(grid):,} rows (> max_rows={max_rows:,}); narrow the "
                        "entities, the window or use a lower frequency")
    ffill = spec.get("missing", {}).get("ffill_limit")
    if ffill is None:
        ffill = {"D": 5, "W": 2, "M": 1, "Q": 1}[freq]

    bases: dict[str, pl.DataFrame] = {}

    def _base(key: str) -> pl.DataFrame:
        if key not in bases:
            e, m = ent_sets[key]
            b = e.select(pl.col("security_id").alias("entity_id"), "issuer_id").join(gdf, how="cross") \
                .sort(["entity_id", "date"])
            bases[key] = _member_mask(b, m)
        return bases[key]

    base_ent = _base(_ents_for(spec.get("entities") or {})) if ents is not None else None

    group_col = {"sector": "sector_id", "region": "region", "country": "country"}.get(spec.get("entity_level") or "security")

    # ---- per-variable frames
    frames: list[tuple[str, pl.DataFrame, list[str]]] = []
    for v in spec["variables"]:
        name, src = v["name"], SOURCES[v["source"]]
        is_entity = src["entity"] not in ("macro", "factor")
        if is_entity:
            f = _base(var_ent[name])
            if src["kind"] == "event":
                f = f.join(raw[name].select("entity_id", "date", "v"), on=["entity_id", "date"], how="left")
                agg = v.get("agg") or ("last" if v["field"] in src.get("derived", {}) else
                                       src.get("default_agg", {}).get(v["field"], "last"))
                if agg in ("last", "first", "mean", "median", "min", "max") and ffill:
                    f = f.with_columns(pl.col("v").forward_fill(limit=int(ffill)).over("entity_id"))
                if v["field"] in src.get("derived", {}):
                    prev = pl.col("v").shift(1).over("entity_id")
                    if v["field"] == "ret":
                        f = f.with_columns(pl.when(prev > 0).then(pl.col("v") / prev - 1.0).otherwise(None).alias("v"))
                    else:
                        f = f.with_columns(pl.when((prev > 0) & (pl.col("v") > 0)).then((pl.col("v") / prev).log())
                                           .otherwise(None).alias("v"))
            else:
                vals = _pit_sample(raw[name], f.select("issuer_id", "date"), "issuer_id", v.get("max_stale_days"))
                f = f.with_columns(vals.alias("v"))
            f = f.select("entity_id", "date", "_member", pl.col("v").alias(name))
            f = _apply_transforms(f, name, v.get("pre_transforms") or [], True, "_member")
            if kind == "timeseries" or group_col:
                agg = v.get("entity_agg") or "mean"
                mem = f.filter(pl.col("_member"))
                if group_col:
                    mem = mem.join(ents.select(pl.col("security_id").alias("entity_id"), pl.col(group_col).alias("_g")),
                                   on="entity_id", how="left").filter(pl.col("_g").is_not_null())
                    keys = ["_g", "date"]
                else:
                    keys = ["date"]
                ea = {"mean": pl.col(name).mean(), "median": pl.col(name).median(), "sum": pl.col(name).sum(),
                      "count": pl.col(name).count().cast(pl.Float64), "min": pl.col(name).min(),
                      "max": pl.col(name).max()}.get(agg)
                if ea is None:
                    raise SpecError(f"{name}: unknown entity_agg {agg!r}")
                n_obs = pl.col(name).count()
                g = mem.group_by(keys).agg(pl.when(n_obs > 0).then(ea).otherwise(None).alias(name))
                if group_col:
                    g = g.rename({"_g": "entity_id"}).with_columns(pl.lit(True).alias("_member"))
                    g = g.sort(["entity_id", "date"])
                    full = g.select("entity_id").unique().join(gdf, how="cross")
                    g = full.join(g, on=["entity_id", "date"], how="left").with_columns(pl.col("_member").fill_null(False))
                    g = g.sort(["entity_id", "date"])
                    g = _apply_transforms(g, name, v.get("transforms") or [], True, "_member")
                    frames.append((name, g, ["entity_id", "date"]))
                else:
                    g = gdf.join(g, on="date", how="left").sort("date")
                    g = _apply_transforms(g, name, v.get("transforms") or [], False, None)
                    frames.append((name, g.select("date", name), ["date"]))
            else:
                f = _apply_transforms(f, name, v.get("transforms") or [], True, "_member")
                frames.append((name, f, ["entity_id", "date"]))
        else:
            if src["kind"] == "factor":
                sc, px = raw[name]
                q = int((v.get("options") or {}).get("quantiles") or 5)
                b = _base(var_ent[name])
                b = b.join(sc.select("entity_id", "date", pl.col("v").alias("_s")), on=["entity_id", "date"], how="left") \
                    .join(px.select("entity_id", "date", pl.col("v").alias("_p")), on=["entity_id", "date"], how="left")
                if ffill:
                    b = b.with_columns(pl.col("_s").forward_fill(limit=int(ffill)).over("entity_id"),
                                       pl.col("_p").forward_fill(limit=int(ffill)).over("entity_id"))
                prev = pl.col("_p").shift(1).over("entity_id")
                b = b.with_columns(pl.when(prev > 0).then(pl.col("_p") / prev - 1.0).otherwise(None).alias("_r"),
                                   pl.col("_s").shift(1).over("entity_id").alias("_sl"))
                b = b.filter(pl.col("_member") & pl.col("_r").is_not_null() & pl.col("_sl").is_not_null())
                b = b.with_columns((pl.col("_sl").rank("average").over("date") / pl.len().over("date")).alias("_pct"),
                                   pl.len().over("date").alias("_n"))
                ls = b.filter(pl.col("_n") >= 2 * q).group_by("date").agg(
                    (pl.col("_r").filter(pl.col("_pct") > 1 - 1 / q).mean()
                     - pl.col("_r").filter(pl.col("_pct") <= 1 / q).mean()).alias("v"))
                f = gdf.join(ls, on="date", how="left").sort("date")
            elif (align == "period" and not src.get("field_split")) or src["kind"] == "event":
                f = gdf.join(raw[name], on="date", how="left").sort("date")
                if ffill:
                    f = f.with_columns(pl.col("v").forward_fill(limit=int(ffill)))
            else:
                left = gdf.with_columns(pl.lit(v["field"]).alias("series_id"))
                f = gdf.with_columns(_pit_sample(raw[name], left, "series_id", v.get("max_stale_days")).alias("v"))
            f = f.select("date", pl.col("v").alias(name))
            if any(t["op"] in CS_OPS for t in (v.get("pre_transforms") or []) + (v.get("transforms") or [])):
                raise SpecError(f"{name}: cross-sectional transforms do not apply to a macro series")
            f = _apply_transforms(f, name, (v.get("pre_transforms") or []) + (v.get("transforms") or []), False, None)
            frames.append((name, f, ["date"]))
        check()

    # ---- assemble
    panel = kind != "timeseries"
    if panel and not group_col:
        out = base_ent.select("entity_id", "date", "_member")
    elif panel:
        ent_frames = [fr for _, fr, k in frames if k == ["entity_id", "date"]]
        out = ent_frames[0].select("entity_id", "date", "_member")
        for fr in ent_frames[1:]:
            out = out.join(fr.select("entity_id", "date", pl.col("_member").alias("_m2")), on=["entity_id", "date"],
                           how="full", coalesce=True).with_columns(
                (pl.col("_member").fill_null(False) | pl.col("_m2").fill_null(False)).alias("_member")).drop("_m2")
    else:
        out = gdf
    for name, fr, keys in frames:
        cols = keys + [name]
        out = out.join(fr.select(cols), on=keys, how="left")

    # ---- dummies + interactions
    attr_cols: list[str] = []
    if panel and ents is not None:
        if group_col:
            out = out.with_columns(pl.col("entity_id").alias("entity_label"))
        else:
            lab = ents.select(pl.col("security_id").alias("entity_id"),
                              pl.coalesce(pl.col("ticker"), pl.col("security_id")).alias("entity_label"),
                              "issuer_id", "sector_id", "region", "country", pl.col("name").alias("entity_name"))
            out = out.join(lab, on="entity_id", how="left")
            attr_cols = ["issuer_id", "sector_id", "region", "country", "entity_name"]
    dummy_cols: list[str] = []
    for dname in spec.get("dummies") or []:
        if dname in ("month", "quarter", "year"):
            src_expr = {"month": pl.col("date").dt.month(), "quarter": pl.col("date").dt.quarter(),
                        "year": pl.col("date").dt.year()}[dname].cast(pl.Utf8)
        elif dname in ("sector", "region", "country"):
            if not panel or group_col:
                raise SpecError(f"dummy {dname!r} needs a security-level panel / cross-section")
            src_expr = pl.col({"sector": "sector_id", "region": "region", "country": "country"}[dname]).cast(pl.Utf8)
        else:
            raise SpecError(f"unknown dummy {dname!r}")
        cats = sorted(c for c in out.select(src_expr.alias("_c"))["_c"].unique().to_list() if c is not None)
        for c in cats[1:]:  # drop_first: the first category is the baseline
            col = f"{dname}_{c}"
            out = out.with_columns(pl.when(src_expr.is_null()).then(None).otherwise((src_expr == c).cast(pl.Float64)).alias(col))
            dummy_cols.append(col)
    inter_cols: list[str] = []
    inter_pairs: dict[str, list[str]] = {}
    for pair in spec.get("interactions") or []:
        a, b = pair[0], pair[1]
        if a not in out.columns or b not in out.columns:
            raise SpecError(f"interaction {a} x {b}: unknown column")
        col = f"{a}_x_{b}"
        out = out.with_columns((pl.col(a) * pl.col(b)).alias(col))
        inter_cols.append(col)
        inter_pairs[col] = [a, b]

    # ---- window, survivorship, missing policy
    out = out.filter((pl.col("date") >= grid[0]) & (pl.col("date") <= grid[-1]))
    if panel:
        out = out.filter(pl.col("_member")).drop("_member")
    var_cols = names + dummy_cols + inter_cols
    miss = spec.get("missing") or {}
    fill = miss.get("fill") or "none"
    if fill == "zero":
        out = out.with_columns([pl.col(c).fill_null(0.0) for c in names])
    elif fill == "ffill":
        out = out.with_columns([(pl.col(c).forward_fill().over("entity_id") if panel else pl.col(c).forward_fill())
                                for c in names])
    elif fill == "mean":
        out = out.with_columns([(pl.col(c).fill_null(pl.col(c).mean().over("date")) if panel
                                 else pl.col(c).fill_null(pl.col(c).mean())) for c in names])
        if not panel:
            warnings.append("missing.fill=mean on a time series uses the full-sample mean (mild look-ahead)")
    if kind == "cross_section":
        last = out["date"].max()
        out = out.filter(pl.col("date") == last)
    if (miss.get("drop") or "none") == "any":
        out = out.drop_nulls(subset=var_cols)
    lead_cols = [v["name"] for v in spec["variables"] if uses_future(v)]
    if any((t["op"] in ("winsorize", "standardize") and (t.get("scope") or "all") == "all")
           for v in spec["variables"] for t in (v.get("transforms") or []) + (v.get("pre_transforms") or [])) \
            and kind == "timeseries":
        warnings.append("full-sample winsorize/standardize uses future observations for the bounds (mild look-ahead)")
    sort_keys = ["entity_id", "date"] if panel else ["date"]
    lead = ["date"] + (["entity_id", "entity_label"] if panel else [])
    out = out.sort(sort_keys).select(lead + [c for c in attr_cols if c in out.columns] + var_cols)
    meta = {
        "core_version": CORE_VERSION, "kind": kind, "frequency": freq, "macro_alignment": align,
        "start": grid[0].isoformat(), "end": grid[-1].isoformat(), "n_periods": len(grid),
        "n_entities": int(out["entity_id"].n_unique()) if panel and out.height else (0 if panel else 1),
        "n_rows": out.height, "entity_level": spec.get("entity_level") or "security",
        "variables": names, "dummies": dummy_cols, "interactions": inter_cols, "interaction_pairs": inter_pairs, "attributes": attr_cols,
        "lead_variables": lead_cols, "warnings": warnings, "sql": sqls,
        "lead_k": {v["name"]: sum(int(t.get("k") or 1) for t in (v.get("pre_transforms") or []) + (v.get("transforms") or [])
                                  if t["op"] in LEAD_OPS) for v in spec["variables"]},
        "build_seconds": round(time.perf_counter() - t0, 3),
    }
    return out, meta
