"""Alert rule engine (job `alerts_eval`, owner: platform). Reads rules from app_state.duckdb `alerts` (written by the
API), evaluates them against the latest data and appends fired events to `alert_events`.

Rule types (contracts/schemas/alerts.yaml; params JSON):
    price_cross     {level, direction: above|below, metric?: close|adj_close}   threshold crossing on the latest bar
    zone_touch      {zone_rank_max, side?: support|resistance|any}               price entering a confluence zone
    score_change    {factor_id | profile_id, mode?: delta|level, delta, level, direction?: up|down|any, lookback_days?: 1}
                    delta: |change| >= delta; level: score crosses `level` (up = from below, down = from above)
    price_change_pct_1d {pct (fraction, 0.05 = 5%), direction?: up|down|any}  1-day adj_close change (alerts v2)
    insider_buy     {min_value_usd, cluster_only}                                new insider buy signal (transition)
    sentiment_shift {threshold_z, window_days?: 90}                              |z| of 7d sentiment vs trailing window
    new_filing      {form_classes: [annual, quarterly, current_report, ...]}     filings published in (prev bday, as_of]
    model_decile    {horizon, min_decile}                                        live prediction enters top deciles
    earnings_upcoming {days_ahead?: 3, date_sources?}                            earnings expected within days_ahead
                    (earnings_calendar; message carries the date-source label: company-announced / provider estimate /
                    model-based projection); fires once per expected date
    earnings_surprise {threshold_z?: 2.0, items?: [eps_diluted, revenue]}        |model surprise z| >= threshold on an
                    actual that became known in (prev bday, as_of] (MODEL-BASED expectation, not consensus)
    activist_13d {include_amendments?: false, include_switch?: true}             new 13D (13G -> 13D switch) filed in
                    (prev bday, as_of] on the security (SEC)
    short_interest_spike {min_chg_pct?: 0.25, min_si_pct?: 0.0}                  FINRA short interest published in the
                    window rose >= min_chg_pct vs the previous settlement (and is >= min_si_pct of shares outstanding)
    eu_short_position {min_pct?: 0.5, include_increases?: true, min_increase_pp?: 0.1}  new or increased EU / UK net
                    short position (named holder) published in the window
    thesis_kpi / thesis_review / thesis_status / thesis_catalyst / thesis_positioning / thesis_event_shock {thesis_id,..}
                    thesis-tracker rules (alerts.thesis_id set): evaluated by the thesis_eval job, no-op here
    price_outside_calibrated_range {level?: 80|90|95 (default 95), horizon?: 1|5|10|20 (default 1), side?: up|down|any}
                    the h-session move (adj_close) ended outside the MARGINAL conformal band emitted h sessions earlier
                    (conformal_envelopes qm_*; calibrated to level% over history - model-based, not 'X% chance today')
    watchlist_news {tiers?, min_abs_sentiment?, languages?, sources?, max_per_day?} / story_velocity {min_articles?,
                    min_ratio?, min_sources?, themes?, countries?}: live news rules (app.news_live.alerts; realtime
                    alert consumer only - no-op in this nightly evaluator)
    conformal_peer_anomaly {percentile?: 0.95 (fraction), scope?: group|member|any (default any)}
                    group: the session's peer-group joint-anomaly percentile >= percentile (conformal_peer_scores);
                    member: the security's |z_cond| within its group is at or above its own trailing-year percentile
                    (conformal_peer_members). Descriptive (model-based), not a trading signal

Targets: an alert has a security_id, or a watchlist_id (expanded through watchlist_items).
Idempotency / noise control: one event per (alert_id, security_id, as_of_date); cooldown_days per (alert, security).
The evaluator also sets alerts.last_fired_at (the only alerts column it writes).

Event windows (guide 13): the "published in the window" rules (new_filing, earnings_surprise, activist_13d,
short_interest_spike, eu_short_position, macro_release, macro_surprise) look at (window_start, as_of] where
window_start = the as_of of the last successful alerts run (data/state/alerts_eval.json), never later than the
previous business day and at most MAX_WINDOW_DAYS back; without a previous run it is as_of - the schedule profile's
period (weekly profile: 7 days, so a Saturday run sees the whole week, not only Friday). Each such trigger carries
dedup keys (filing id, release event id, settlement date ...) stored in the event payload; an event id that already
fired for the alert is never fired again, so overlapping windows and re-runs are safe.

Data access goes through SqlDataProvider(con): any DuckDB connection exposing the contract tables by name — the lake
views (lake_provider()) in production, in-memory tables built from fixture frames in tests (frames_provider()).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import connect_app_state, duckdb_connect, ensure_duckdb_tables, register_views

log = get_logger("app.scheduler.alerts")

RULE_TYPES = ("price_cross", "zone_touch", "score_change", "insider_buy", "sentiment_shift", "new_filing", "model_decile",
              "price_change_pct_1d", "studio_drift", "macro_release", "macro_surprise", "regime_change",
              "earnings_upcoming", "earnings_surprise", "backtest_drift", "activist_13d", "short_interest_spike",
              "eu_short_position", "thesis_kpi", "thesis_review", "thesis_status", "thesis_catalyst",
              "thesis_positioning", "thesis_event_shock", "price_outside_calibrated_range", "conformal_peer_anomaly",
              "watchlist_news", "story_velocity")
#: live news rules (roadmap v3 N4.3): evaluated by the realtime alert consumer on news.item / news.story, no-op here
NEWS_RULE_TYPES = ("watchlist_news", "story_velocity")
#: thesis tracker (roadmap #8) rule types: events appended by the thesis_eval job (app.theses.evaluate), no-op here
THESIS_RULE_TYPES = ("thesis_kpi", "thesis_review", "thesis_status", "thesis_catalyst", "thesis_positioning",
                     "thesis_event_shock")
#: macro rules (macro-w2) have no security / watchlist target: they fire per country; Trigger.security_id = "macro:<CC>"
MACRO_RULE_TYPES = ("macro_release", "macro_surprise", "regime_change")
MACRO_TABLES = ("macro_releases", "macro_release_events", "macro_surprises", "macro_recession", "macro_cycle")
#: earnings (roadmap #6) tables; registered when the contracts exist
EARNINGS_TABLES = ("earnings_calendar", "earnings_expectations")
#: smart-money (roadmap #7) tables; registered when the contracts exist
SMART_MONEY_TABLES = ("activist_events", "si_signals", "eu_short_positions")
#: conformal ellipsoids (docs/CONFORMAL_TECHNICALS_PLAN.md P3); registered when the contracts exist
CONFORMAL_TABLES = ("conformal_envelopes", "conformal_peer_scores", "conformal_peer_members", "conformal_peer_groups")


@dataclass
class Trigger:
    security_id: str
    as_of_date: date
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    #: stable ids of the underlying events (window rules): de-duplication across runs / overlapping windows
    keys: list[str] = field(default_factory=list)


#: window rules never look further back than this (a worker that was off for months must not flood the inbox)
MAX_WINDOW_DAYS = 31
WINDOW_STATE_FILE = "alerts_eval.json"


def _win(dp: Any, as_of: date) -> date:
    """Exclusive start of the event window: the provider's window_start (set by evaluate_alerts), at the latest the
    previous business day."""
    pb = _prev_bday(as_of)
    ws = getattr(dp, "window_start", None)
    return min(ws, pb) if isinstance(ws, date) else pb


# ------------------------------------------------------------------------------------------------ data provider


def _ids_sql(ids: list[str]) -> str:
    return ", ".join("'" + i.replace("'", "''") + "'" for i in ids) or "NULL"


#: last_two_bars scan window: prices_daily holds decades of history per security; the latest two bars of anything
#: still worth alerting on are within a year (also lets DuckDB prune the year partitions)
LAST_BARS_LOOKBACK_DAYS = 370


class SqlDataProvider:
    """Queries for the evaluators. `con` must expose the contract tables (views or tables) by name."""

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con

    def _df(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        return self.con.execute(sql, params or []).pl()

    def last_two_bars(self, ids: list[str], as_of: date) -> pl.DataFrame:
        return self._df(f"""
            SELECT security_id, date, open, high, low, close, adj_close,
                   LEAD(close) OVER w AS prev_close, LEAD(adj_close) OVER w AS prev_adj_close,
                   LEAD(high) OVER w AS prev_high, LEAD(low) OVER w AS prev_low, rn
            FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                  FROM prices_daily WHERE security_id IN ({_ids_sql(ids)}) AND date <= ? AND date >= ?
                    AND year >= ?)
            WINDOW w AS (PARTITION BY security_id ORDER BY date DESC)
            QUALIFY rn = 1""", [as_of, as_of - timedelta(days=LAST_BARS_LOOKBACK_DAYS),
                                (as_of - timedelta(days=LAST_BARS_LOOKBACK_DAYS)).year])

    def zones(self, ids: list[str], as_of: date) -> pl.DataFrame:
        return self._df(f"""
            SELECT * FROM confluence_zones z
            WHERE security_id IN ({_ids_sql(ids)}) AND as_of_date = (
                SELECT MAX(as_of_date) FROM confluence_zones z2 WHERE z2.security_id = z.security_id AND z2.as_of_date <= ?)
            """, [as_of])

    def score_series(self, ids: list[str], as_of: date, factor_id: str | None, profile_id: str | None) -> pl.DataFrame:
        if factor_id:
            tbl, key, val = "factor_scores", "factor_id", factor_id
        else:
            tbl, key, val = "composite_scores", "profile_id", profile_id or "balanced"
        return self._df(f"""SELECT security_id, as_of_date, score FROM {tbl}
                            WHERE security_id IN ({_ids_sql(ids)}) AND {key} = ? AND as_of_date <= ?
                              AND score IS NOT NULL ORDER BY security_id, as_of_date""", [val, as_of])

    def insider_last_two(self, ids: list[str], as_of: date) -> pl.DataFrame:
        return self._df(f"""
            SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY as_of_date DESC) AS rn
                           FROM insider_signals WHERE security_id IN ({_ids_sql(ids)}) AND as_of_date <= ?)
            WHERE rn <= 2 ORDER BY security_id, rn""", [as_of])

    def sentiment_window(self, ids: list[str], as_of: date, window_days: int) -> pl.DataFrame:
        return self._df(f"""SELECT security_id, date, sentiment_mean, sentiment_ewm_7d FROM sentiment_daily
                            WHERE security_id IN ({_ids_sql(ids)}) AND date <= ? AND date > ?
                            ORDER BY security_id, date""", [as_of, as_of - timedelta(days=window_days)])

    def filings(self, ids: list[str], start_excl: date, end_incl: date) -> pl.DataFrame:
        return self._df(f"""
            SELECT s.security_id, f.filing_id, f.form_type, f.form_class, f.knowledge_date, f.period_end, f.url
            FROM filings_index f JOIN securities_master s ON s.issuer_id = f.issuer_id
            WHERE s.security_id IN ({_ids_sql(ids)}) AND f.knowledge_date > ? AND f.knowledge_date <= ?
            ORDER BY f.knowledge_date""", [start_excl, end_incl])

    def predictions_last_two(self, ids: list[str], as_of: date, horizon: str) -> pl.DataFrame:
        return self._df(f"""
            SELECT * FROM (
                SELECT security_id, date, decile, rank_pct, model_run_id,
                       ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
                FROM ml_predictions
                WHERE security_id IN ({_ids_sql(ids)}) AND horizon = ? AND date <= ? AND (is_live OR is_oos))
            WHERE rn <= 2 ORDER BY security_id, rn""", [horizon, as_of])

    # ---- earnings (roadmap #6)
    def earnings_upcoming(self, ids: list[str], start: date, end: date) -> pl.DataFrame:
        return self._df(f"""SELECT security_id, issuer_id, period_end, fiscal_period, expected_date, expected_ts, session,
                                   date_source, source_label, window_lo, window_hi
                            FROM earnings_calendar WHERE security_id IN ({_ids_sql(ids)})
                              AND expected_date >= ? AND expected_date <= ? ORDER BY expected_date""", [start, end])

    def earnings_surprises(self, ids: list[str], start_excl: date, end_incl: date) -> pl.DataFrame:
        return self._df(f"""
            SELECT s.security_id, x.issuer_id, x.item_id, x.period_end, x.fiscal_period, x.expectation, x.actual,
                   x.surprise_pct, x.surprise_z, x.model, x.actual_knowledge_date
            FROM earnings_expectations x JOIN securities_master s ON s.issuer_id = x.issuer_id
            WHERE s.security_id IN ({_ids_sql(ids)}) AND x.is_primary AND x.status = 'released'
              AND x.surprise_z IS NOT NULL AND x.actual_knowledge_date > ? AND x.actual_knowledge_date <= ?
            ORDER BY x.actual_knowledge_date""", [start_excl, end_incl])

    # ---- smart money (roadmap #7): 13D / 13G events, FINRA short interest, EU / UK net short positions
    def activist_between(self, ids: list[str], start_excl: date, end_incl: date) -> pl.DataFrame:
        return self._df(f"""SELECT security_id, filing_id, event_date, event_type, form_type, filer_name, subject_name, url
                            FROM activist_events WHERE security_id IN ({_ids_sql(ids)}) AND schedule = '13D'
                              AND knowledge_date > ? AND knowledge_date <= ? ORDER BY event_date""", [start_excl, end_incl])

    def si_between(self, ids: list[str], start_excl: date, end_incl: date) -> pl.DataFrame:
        return self._df(f"""SELECT security_id, settlement_date, knowledge_date, si_shares, si_pct_out, si_chg_pct,
                                   days_to_cover, squeeze_flag
                            FROM si_signals WHERE security_id IN ({_ids_sql(ids)})
                              AND knowledge_date > ? AND knowledge_date <= ? ORDER BY settlement_date""",
                        [start_excl, end_incl])

    def eu_short_between(self, ids: list[str], start_excl: date, end_incl: date) -> pl.DataFrame:
        """Individual EU / UK notifications published in the window with the holder's previous position on the issuer."""
        return self._df(f"""
            SELECT * FROM (
                SELECT security_id, regulator, holder_name, isin, issuer_name, position_pct, position_date, knowledge_date,
                       lag(position_pct) OVER (PARTITION BY regulator, holder_name, isin ORDER BY position_date, knowledge_date)
                           AS prev_pct
                FROM eu_short_positions WHERE security_id IN ({_ids_sql(ids)}) AND position_type = 'individual'
                  AND knowledge_date <= ?)
            WHERE knowledge_date > ? ORDER BY position_date""", [end_incl, start_excl])

    # ---- macro (macro-w2): model-based expectations / surprises, recession panel, cycle clock
    def macro_releases_between(self, start_excl: date, end_incl: date) -> pl.DataFrame:
        return self._df("""
            SELECT e.release_event_id, e.series_id, e.country, e.theme, e.release_date, e.period, e.actual, e.prior,
                   r.release_name, r.importance
            FROM macro_release_events e JOIN macro_releases r USING (release_event_id)
            WHERE e.status = 'released' AND e.release_date > ? AND e.release_date <= ?
            ORDER BY e.release_date""", [start_excl, end_incl])

    def macro_surprises_between(self, start_excl: date, end_incl: date) -> pl.DataFrame:
        return self._df("""SELECT * FROM macro_surprises WHERE release_date > ? AND release_date <= ?
                           AND surprise_z IS NOT NULL ORDER BY release_date""", [start_excl, end_incl])

    def macro_recession_last_two(self, as_of: date) -> pl.DataFrame:
        return self._df("""
            SELECT * FROM (SELECT method, country, date, probability, signal,
                                  ROW_NUMBER() OVER (PARTITION BY method, country ORDER BY date DESC) AS rn
                           FROM macro_recession WHERE date <= ? AND probability IS NOT NULL)
            WHERE rn <= 2 ORDER BY method, country, rn""", [as_of])

    def macro_cycle_last_two(self, as_of: date) -> pl.DataFrame:
        return self._df("""
            SELECT * FROM (SELECT country, date, quadrant,
                                  ROW_NUMBER() OVER (PARTITION BY country ORDER BY date DESC) AS rn
                           FROM macro_cycle WHERE date <= ? AND quadrant IS NOT NULL)
            WHERE rn <= 2 ORDER BY country, rn""", [as_of])


# ---- conformal queries (P3 alert types), attached to SqlDataProvider


def _recent_bars(self: SqlDataProvider, ids: list[str], as_of: date, n: int) -> pl.DataFrame:
    """The last n bars per security (rn 1 = latest) within ~2 months."""
    return self._df(f"""
        SELECT security_id, date, adj_close, close, rn FROM (
            SELECT security_id, date, adj_close, close,
                   ROW_NUMBER() OVER (PARTITION BY security_id ORDER BY date DESC) AS rn
            FROM prices_daily WHERE security_id IN ({_ids_sql(ids)}) AND date <= ? AND date >= ? AND year >= ?)
        WHERE rn <= ?""", [as_of, as_of - timedelta(days=70), (as_of - timedelta(days=70)).year, n])


def _envelope_rows(self: SqlDataProvider, ids: list[str], as_of: date) -> pl.DataFrame:
    cols = ", ".join(f"mu_{h}, v_{h}, " + ", ".join(f"qm_{h}_{x}" for x in (80, 90, 95)) for h in (1, 5, 10, 20))
    return self._df(f"""SELECT security_id, as_of_date, last_close, {cols} FROM conformal_envelopes
                         WHERE security_id IN ({_ids_sql(ids)}) AND as_of_date <= ? AND as_of_date >= ?""",
                    [as_of, as_of - timedelta(days=45)])


def _peer_membership(self: SqlDataProvider, ids: list[str]) -> pl.DataFrame:
    return self._df(f"""SELECT security_id, group_id, label, group_kind FROM conformal_peer_groups
                         WHERE version = (SELECT max(version) FROM conformal_peer_groups)
                           AND security_id IN ({_ids_sql(ids)})""")


def _peer_scores_latest(self: SqlDataProvider, gids: list[str], as_of: date) -> pl.DataFrame:
    return self._df(f"""SELECT * FROM (
        SELECT group_id, as_of_date, score, score_pct, anomaly_90, anomaly_95, top_member, breakaway_member, breakaway_z,
               ROW_NUMBER() OVER (PARTITION BY group_id ORDER BY as_of_date DESC) AS rn
        FROM conformal_peer_scores WHERE group_id IN ({_ids_sql(gids)}) AND as_of_date <= ? AND as_of_date >= ?
          AND year >= ?) WHERE rn = 1""", [as_of, as_of - timedelta(days=10), (as_of - timedelta(days=10)).year])


def _member_z_history(self: SqlDataProvider, ids: list[str], gids: list[str], as_of: date) -> pl.DataFrame:
    return self._df(f"""SELECT group_id, security_id, as_of_date, z_cond FROM conformal_peer_members
                         WHERE security_id IN ({_ids_sql(ids)}) AND group_id IN ({_ids_sql(gids)})
                           AND as_of_date <= ? AND as_of_date > ? AND year >= ? AND z_cond IS NOT NULL""",
                    [as_of, as_of - timedelta(days=370), (as_of - timedelta(days=370)).year])


SqlDataProvider.recent_bars = _recent_bars  # type: ignore[attr-defined]
SqlDataProvider.envelope_rows = _envelope_rows  # type: ignore[attr-defined]
SqlDataProvider.peer_membership = _peer_membership  # type: ignore[attr-defined]
SqlDataProvider.peer_scores_latest = _peer_scores_latest  # type: ignore[attr-defined]
SqlDataProvider.member_z_history = _member_z_history  # type: ignore[attr-defined]


def lake_provider(settings: Settings | None = None) -> SqlDataProvider:
    con = duckdb_connect(None, settings=settings)
    register_views(con, ["prices_daily", "confluence_zones", "composite_scores", "factor_scores", "insider_signals",
                         "sentiment_daily", "filings_index", "securities_master", "ml_predictions"], settings=settings)
    _register_macro(con, settings)
    from app.core.schema import load_contracts

    have = set(load_contracts())
    register_views(con, [x for x in EARNINGS_TABLES if x in have], settings=settings)
    register_views(con, [x for x in SMART_MONEY_TABLES if x in have], settings=settings)
    register_views(con, [x for x in CONFORMAL_TABLES if x in have], settings=settings)
    return SqlDataProvider(con)


def _register_macro(con: duckdb.DuckDBPyConnection, settings: Settings | None = None) -> None:
    """Macro views for the macro rule types (skipped silently for contracts not present yet)."""
    from app.core.schema import load_contracts

    have = set(load_contracts())
    register_views(con, [t for t in MACRO_TABLES if t in have], settings=settings)


def frames_provider(frames: dict[str, pl.DataFrame]) -> SqlDataProvider:
    """Test helper: register Polars frames as DuckDB tables; missing tables become typed empty tables."""
    from app.core.schema import empty_frame, get_contract

    from app.core.schema import load_contracts

    con = duckdb.connect(":memory:")
    have = set(load_contracts())
    for t in ("prices_daily", "confluence_zones", "composite_scores", "factor_scores", "insider_signals",
              "sentiment_daily", "filings_index", "securities_master", "ml_predictions",
              *[m for m in MACRO_TABLES if m in have], *[m for m in EARNINGS_TABLES if m in have],
              *[m for m in SMART_MONEY_TABLES if m in have], *[m for m in CONFORMAL_TABLES if m in have]):
        df = frames.get(t)
        if df is None:
            df = empty_frame(t)
        else:  # add contract columns the fixture omitted (typed nulls)
            c = get_contract(t)
            df = df.with_columns([pl.lit(None, dtype=col.polars).alias(col.name) for col in c.columns
                                  if col.name not in df.columns])
        con.register(f"_{t}_src", df.to_arrow())
        con.execute(f'CREATE TABLE "{t}" AS SELECT * FROM "_{t}_src"')
    return SqlDataProvider(con)


# ------------------------------------------------------------------------------------------------ evaluators

Evaluator = Callable[[dict[str, Any], list[str], SqlDataProvider, date], list[Trigger]]
EVALUATORS: dict[str, Evaluator] = {}


def evaluator(rule_type: str) -> Callable[[Evaluator], Evaluator]:
    def deco(fn: Evaluator) -> Evaluator:
        EVALUATORS[rule_type] = fn
        return fn
    return deco


@evaluator("price_cross")
def _price_cross(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    level = float(params["level"])
    direction = params.get("direction", "above")
    metric = "adj_close" if params.get("metric") == "adj_close" else "close"
    out = []
    for r in dp.last_two_bars(ids, as_of).iter_rows(named=True):
        cur, prev = r[metric], r[f"prev_{metric}"]
        if cur is None or prev is None:
            continue
        hit = (prev < level <= cur) if direction == "above" else (prev > level >= cur)
        if hit:
            out.append(Trigger(r["security_id"], r["date"], f"{metric} crossed {direction} {level:g} ({prev:g} -> {cur:g})",
                               {"level": level, "direction": direction, "prev": prev, "value": cur}))
    return out


@evaluator("price_change_pct_1d")
def _price_change_pct_1d(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    pct = abs(float(params["pct"]))
    direction = params.get("direction", "any")
    out = []
    for r in dp.last_two_bars(ids, as_of).iter_rows(named=True):
        cur, prev = r["adj_close"], r["prev_adj_close"]
        if cur is None or not prev:
            continue
        chg = cur / prev - 1.0
        hit = {"up": chg >= pct, "down": chg <= -pct}.get(direction, abs(chg) >= pct)
        if hit:
            out.append(Trigger(r["security_id"], r["date"], f"1-day move {chg:+.1%} (threshold {pct:.1%}, {direction})",
                               {"pct_change": chg, "threshold": pct, "direction": direction, "prev_adj_close": prev,
                                "adj_close": cur}))
    return out


@evaluator("zone_touch")
def _zone_touch(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    rank_max = int(params.get("zone_rank_max", 3))
    side = params.get("side", "any")
    bars = {r["security_id"]: r for r in dp.last_two_bars(ids, as_of).iter_rows(named=True)}
    out = []
    for z in dp.zones(ids, as_of).sort(["security_id", "rank"]).iter_rows(named=True):
        if z["rank"] > rank_max or (side != "any" and z["side"] != side):
            continue
        b = bars.get(z["security_id"])
        if not b:
            continue
        lo, hi = (b["low"] if b["low"] is not None else b["close"]), (b["high"] if b["high"] is not None else b["close"])
        inside_now = lo <= z["zone_high"] and hi >= z["zone_low"]
        plo = b["prev_low"] if b["prev_low"] is not None else b["prev_close"]
        phi = b["prev_high"] if b["prev_high"] is not None else b["prev_close"]
        inside_prev = plo is not None and phi is not None and plo <= z["zone_high"] and phi >= z["zone_low"]
        if inside_now and not inside_prev:
            out.append(Trigger(z["security_id"], b["date"],
                               f"price entered {z['side']} zone #{z['rank']} [{z['zone_low']:g}-{z['zone_high']:g}]",
                               {"zone_id": z["zone_id"], "rank": z["rank"], "side": z["side"], "zone_low": z["zone_low"],
                                "zone_high": z["zone_high"], "strength": z["strength"], "low": lo, "high": hi}))
            bars.pop(z["security_id"])  # one event per security: the best-ranked zone entered
    return out


@evaluator("score_change")
def _score_change(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    mode = params.get("mode", "delta")
    delta = float(params.get("delta", 10))
    level = float(params["level"]) if mode == "level" else None
    direction = params.get("direction") or ("any" if mode == "level" else ("up" if delta > 0 else "down"))
    lookback = int(params.get("lookback_days", 1))
    factor_id, profile_id = params.get("factor_id"), params.get("profile_id")
    df = dp.score_series(ids, as_of, factor_id, profile_id)
    out = []
    for sid, g in df.group_by("security_id"):
        g = g.sort("as_of_date")
        if g.height < 2:
            continue
        last = g.row(-1, named=True)
        prev_rows = g.filter(pl.col("as_of_date") <= last["as_of_date"] - timedelta(days=lookback))
        if prev_rows.is_empty():
            continue
        prev = prev_rows.row(-1, named=True)
        chg = last["score"] - prev["score"]
        if level is not None:
            up, down = prev["score"] < level <= last["score"], prev["score"] > level >= last["score"]
            hit = {"up": up, "down": down}.get(direction, up or down)
        else:
            hit = {"up": chg >= abs(delta), "down": chg <= -abs(delta)}.get(direction, abs(chg) >= abs(delta))
        if hit:
            what = f"factor {factor_id}" if factor_id else f"composite {profile_id or 'balanced'}"
            how = f" crossed {level:g}" if level is not None else ""
            out.append(Trigger(str(sid[0] if isinstance(sid, tuple) else sid), last["as_of_date"],
                               f"{what} score{how} {prev['score']:.1f} -> {last['score']:.1f} ({chg:+.1f})",
                               {"mode": mode, "level": level, "from": prev["score"], "to": last["score"], "change": chg,
                                "since": str(prev["as_of_date"])}))
    return out


@evaluator("insider_buy")
def _insider_buy(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    min_value = float(params.get("min_value_usd", 0))
    cluster_only = bool(params.get("cluster_only", False))

    def qualifies(r: dict[str, Any] | None) -> bool:
        if r is None:
            return False
        v = r.get("net_buy_value_usd_30d") or 0.0
        return v >= min_value and v > 0 and (r.get("cluster_buy") or not cluster_only)

    df = dp.insider_last_two(ids, as_of)
    out = []
    for sid, g in df.group_by("security_id"):
        rows = sorted(g.iter_rows(named=True), key=lambda r: r["rn"])
        cur, prev = rows[0], (rows[1] if len(rows) > 1 else None)
        if qualifies(cur) and not qualifies(prev):
            out.append(Trigger(cur["security_id"], cur["as_of_date"],
                               f"insider buying ${cur['net_buy_value_usd_30d']:,.0f} (30d)"
                               + (" — cluster buy" if cur.get("cluster_buy") else ""),
                               {"net_buy_value_usd_30d": cur["net_buy_value_usd_30d"], "cluster_buy": cur.get("cluster_buy"),
                                "c_suite_buy": cur.get("c_suite_buy"), "signal_score": cur.get("signal_score")}))
    return out


@evaluator("sentiment_shift")
def _sentiment_shift(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    thr = float(params.get("threshold_z", 2.0))
    window = int(params.get("window_days", 90))
    df = dp.sentiment_window(ids, as_of, window)
    out = []
    for _, g in df.group_by("security_id"):
        g = g.sort("date")
        hist = g["sentiment_mean"].drop_nulls()
        last = g.row(-1, named=True)
        cur = last["sentiment_ewm_7d"] if last["sentiment_ewm_7d"] is not None else last["sentiment_mean"]
        if cur is None or hist.len() < 10:
            continue
        sd = hist.std()
        if not sd:
            continue
        z = (cur - hist.mean()) / sd
        if abs(z) >= thr:
            out.append(Trigger(last["security_id"], last["date"], f"sentiment shift z={z:+.2f} ({'positive' if z > 0 else 'negative'})",
                               {"z": z, "current": cur, "mean": hist.mean(), "std": sd, "window_days": window}))
    return out


@evaluator("new_filing")
def _new_filing(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    classes = set(params.get("form_classes") or ["annual", "quarterly", "current_report"])
    start = as_of - timedelta(days=int(params.get("lookback_days", 0))) if params.get("lookback_days") else _win(dp, as_of)
    df = dp.filings(ids, start, as_of)
    out = []
    for sid, g in df.filter(pl.col("form_class").is_in(list(classes))).group_by("security_id"):
        rows = list(g.sort("knowledge_date").iter_rows(named=True))
        last = rows[-1]
        forms = ", ".join(sorted({r["form_type"] for r in rows}))
        out.append(Trigger(last["security_id"], last["knowledge_date"], f"new filing: {forms}",
                           {"filings": [{"filing_id": r["filing_id"], "form_type": r["form_type"], "url": r["url"],
                                         "knowledge_date": str(r["knowledge_date"])} for r in rows]},
                           keys=[f"filing:{r['filing_id']}" for r in rows]))
    return out


@evaluator("studio_drift")
def _studio_drift(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """Events for Model Studio drift alerts are appended by the studio_scheduled job (app.modeling.scheduled)."""
    return []


@evaluator("backtest_drift")
def _backtest_drift(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """Events for backtesting-lab paper-strategy drift are appended by the backtest_scheduled job (app.backtest.scheduled)."""
    return []


def _thesis_noop(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """thesis_* rules (roadmap #8): evaluated by the thesis_eval job (app.theses.evaluate), which appends the events."""
    return []


for _rt in THESIS_RULE_TYPES:
    EVALUATORS[_rt] = _thesis_noop


def _news_noop(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """watchlist_news / story_velocity: live-only (app.realtime.alerts_live + app.news_live.alerts)."""
    return []


for _rt in NEWS_RULE_TYPES:
    EVALUATORS[_rt] = _news_noop


@evaluator("model_decile")
def _model_decile(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    horizon = str(params.get("horizon", "3M"))
    min_decile = int(params.get("min_decile", 10))
    df = dp.predictions_last_two(ids, as_of, horizon)
    out = []
    for _, g in df.group_by("security_id"):
        rows = sorted(g.iter_rows(named=True), key=lambda r: r["rn"])
        cur, prev = rows[0], (rows[1] if len(rows) > 1 else None)
        if cur["decile"] >= min_decile and (prev is None or prev["decile"] < min_decile):
            out.append(Trigger(cur["security_id"], cur["date"], f"model {horizon} decile {cur['decile']} (>= {min_decile})",
                               {"decile": cur["decile"], "prev_decile": prev["decile"] if prev else None,
                                "rank_pct": cur["rank_pct"], "model_run_id": cur["model_run_id"]}))
    return out


SESSION_TXT = {"pre_market": "before the open", "intraday": "during the session", "post_market": "after the close"}


@evaluator("earnings_upcoming")
def _earnings_upcoming(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    days = int(params.get("days_ahead", 3))
    srcs = params.get("date_sources")
    out = []
    for r in dp.earnings_upcoming(ids, as_of, as_of + timedelta(days=days)).iter_rows(named=True):
        if srcs and r["date_source"] not in srcs:
            continue
        when = SESSION_TXT.get(r["session"] or "", "")
        win = (f" (window {r['window_lo']} to {r['window_hi']})" if r["date_source"] == "model_projection"
               and r["window_lo"] else "")
        out.append(Trigger(r["security_id"], r["expected_date"],
                           f"earnings expected {r['expected_date']}{' ' + when if when else ''}"
                           f"{' - ' + r['fiscal_period'] if r['fiscal_period'] else ''} - {r['source_label']}{win}",
                           {"expected_date": str(r["expected_date"]), "period_end": str(r["period_end"]),
                            "fiscal_period": r["fiscal_period"], "session": r["session"], "date_source": r["date_source"],
                            "source_label": r["source_label"]}))
    return out


@evaluator("earnings_surprise")
def _earnings_surprise(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    thr = abs(float(params.get("threshold_z", 2.0)))
    items = set(params.get("items") or ["eps_diluted", "revenue"])
    best: dict[str, dict[str, Any]] = {}
    for r in dp.earnings_surprises(ids, _win(dp, as_of), as_of).iter_rows(named=True):
        if r["item_id"] not in items or abs(r["surprise_z"]) < thr:
            continue
        b = best.get(r["security_id"])
        if b is None or abs(r["surprise_z"]) > abs(b["surprise_z"]):
            best[r["security_id"]] = r
    out = []
    for sid, r in best.items():
        lbl = "EPS" if r["item_id"] == "eps_diluted" else "revenue"
        pct = f" ({r['surprise_pct']:+.1%})" if r["surprise_pct"] is not None else ""
        out.append(Trigger(sid, r["actual_knowledge_date"],
                           f"{lbl} surprise z={r['surprise_z']:+.2f}{pct}: actual {r['actual']:.4g} vs model-based "
                           f"expectation {r['expectation']:.4g} (not consensus)",
                           {"item_id": r["item_id"], "period_end": str(r["period_end"]), "surprise_z": r["surprise_z"],
                            "surprise_pct": r["surprise_pct"], "actual": r["actual"], "expectation": r["expectation"],
                            "model": r["model"], "model_based": True},
                           keys=[f"earn:{sid}:{r['item_id']}:{r['period_end']}"]))
    return out


@evaluator("activist_13d")
def _activist_13d(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """New 13D (and 13G -> 13D switch) on the security filed in (prev business day, as_of]; amendments on request."""
    types = {"new_13d"}
    if params.get("include_switch", True):
        types.add("switch_13g_to_13d")
    if params.get("include_amendments", False):
        types.add("amend_13d")
    labels = {"new_13d": "new 13D", "switch_13g_to_13d": "13G -> 13D switch", "amend_13d": "13D amendment"}
    out = []
    for r in dp.activist_between(ids, _win(dp, as_of), as_of).iter_rows(named=True):
        if r["event_type"] not in types:
            continue
        filer = r["filer_name"] or "unknown filer"
        out.append(Trigger(r["security_id"], r["event_date"],
                           f"{labels[r['event_type']]} filed by {filer} ({r['form_type']}, SEC)",
                           {"filing_id": r["filing_id"], "event_type": r["event_type"], "filer_name": r["filer_name"],
                            "url": r["url"]}, keys=[f"13d:{r['filing_id']}"]))
    return out


@evaluator("short_interest_spike")
def _short_interest_spike(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """FINRA short interest published in (prev business day, as_of] up >= min_chg_pct vs the previous settlement."""
    chg = float(params.get("min_chg_pct", 0.25))
    lvl = float(params.get("min_si_pct", 0.0))
    out = []
    for r in dp.si_between(ids, _win(dp, as_of), as_of).iter_rows(named=True):
        if r["si_chg_pct"] is None or r["si_chg_pct"] < chg or (lvl and (r["si_pct_out"] or 0) < lvl):
            continue
        pct = f", {r['si_pct_out']:.1%} of shares outstanding" if r["si_pct_out"] is not None else ""
        out.append(Trigger(r["security_id"], r["knowledge_date"],
                           f"short interest {r['si_chg_pct']:+.0%} to {r['si_shares']:,.0f} shares{pct} "
                           f"(settlement {r['settlement_date']}, FINRA)",
                           {"settlement_date": str(r["settlement_date"]), "si_chg_pct": r["si_chg_pct"],
                            "si_pct_out": r["si_pct_out"], "squeeze_flag": r["squeeze_flag"]},
                           keys=[f"si:{r['security_id']}:{r['settlement_date']}"]))
    return out


@evaluator("eu_short_position")
def _eu_short_position(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """New (crossing min_pct) or increased (>= min_increase_pp) EU / UK net short position published in the window."""
    lo = float(params.get("min_pct", 0.5))
    inc = bool(params.get("include_increases", True))
    step = float(params.get("min_increase_pp", 0.1))
    out = []
    for r in dp.eu_short_between(ids, _win(dp, as_of), as_of).iter_rows(named=True):
        pct, prev = r["position_pct"], r["prev_pct"]
        if pct is None or pct < lo:
            continue
        new = prev is None or prev < lo
        up = inc and prev is not None and pct - prev >= step - 1e-9
        if not (new or up):
            continue
        what = "new net short position" if new else f"net short position increased from {prev:.2f}%"
        out.append(Trigger(r["security_id"], r["knowledge_date"],
                           f"{what}: {r['holder_name']} {pct:.2f}% of {r['issuer_name'] or r['isin']} "
                           f"(position date {r['position_date']}, {r['regulator'].upper()})",
                           {"regulator": r["regulator"], "holder_name": r["holder_name"], "isin": r["isin"],
                            "position_pct": pct, "prev_pct": prev, "position_date": str(r["position_date"])},
                           keys=[f"eus:{r['regulator']}:{r['holder_name']}:{r['isin']}:{r['position_date']}:{pct:.4f}"]))
    return out


@evaluator("price_outside_calibrated_range")
def _price_outside_calibrated_range(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """The h-session move ending on the latest bar left the marginal conformal band emitted at its origin (model-based;
    the band is calibrated to level% over history, so ~(100 - level)% of moves end outside it)."""
    import math  # noqa: PLC0415

    from app.conformal import serve as CS  # noqa: PLC0415

    lvl = int(params.get("level", 95))
    h = int(params.get("horizon", 1))
    side = params.get("side", "any")
    bars: dict[str, dict[int, dict[str, Any]]] = {}
    for r in dp.recent_bars(ids, as_of, h + 1).iter_rows(named=True):  # type: ignore[attr-defined]
        bars.setdefault(r["security_id"], {})[int(r["rn"])] = r
    env = {(r["security_id"], r["as_of_date"]): r for r in dp.envelope_rows(ids, as_of).iter_rows(named=True)}  # type: ignore[attr-defined]
    out = []
    for sid, b in bars.items():
        cur, org = b.get(1), b.get(h + 1)
        if not cur or not org or not cur["adj_close"] or not org["adj_close"]:
            continue
        row = env.get((sid, org["date"]))
        if row is None:
            continue
        move = math.log(cur["adj_close"] / org["adj_close"])
        if not CS.outside_band(move, row, h, lvl):
            continue
        direction = "up" if move > (row.get(f"mu_{h}") or 0.0) else "down"
        if side in ("up", "down") and direction != side:
            continue
        hw = CS.half_width(row, h, lvl) or 0.0
        out.append(Trigger(sid, cur["date"],
                           f"close outside the {lvl}% calibrated {h}-session range ({direction}: move {math.expm1(move):+.1%} "
                           f"vs +-{math.expm1(hw):.1%}; band from {org['date']}; model-based)",
                           {"level": lvl, "horizon": h, "direction": direction, "move_log": move, "half_width_log": hw,
                            "origin_date": str(org["date"]), "model_based": True}))
    return out


@evaluator("conformal_peer_anomaly")
def _conformal_peer_anomaly(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    """Group: the peer group's joint-anomaly percentile >= percentile. Member: the security's |z_cond| within the group
    at or above its own trailing-year percentile. One event per security (the strongest). Descriptive, model-based."""
    p = float(params.get("percentile", 0.95))
    scope = params.get("scope", "any")
    mem = dp.peer_membership(ids)  # type: ignore[attr-defined]
    if mem.is_empty():
        return []
    gids = sorted(set(mem["group_id"].to_list()))
    sc = {r["group_id"]: r for r in dp.peer_scores_latest(gids, as_of).iter_rows(named=True)}  # type: ignore[attr-defined]
    hist: dict[tuple[str, str], list[tuple[date, float]]] = {}
    if scope in ("member", "any"):
        for r in dp.member_z_history(ids, gids, as_of).iter_rows(named=True):  # type: ignore[attr-defined]
            hist.setdefault((r["group_id"], r["security_id"]), []).append((r["as_of_date"], abs(float(r["z_cond"]))))
    best: dict[str, tuple[float, Trigger]] = {}
    for r in mem.iter_rows(named=True):
        sid, gid = r["security_id"], r["group_id"]
        s = sc.get(gid)
        if not s:
            continue
        cands: list[tuple[float, str, dict[str, Any]]] = []
        pct = s.get("score_pct")
        if scope in ("group", "any") and pct is not None and pct >= p:
            cands.append((float(pct), f"joint anomaly in peer group {r['label']}: percentile {pct:.1%} (top contributor "
                                      f"{s.get('top_member')}, break-away {s.get('breakaway_member')})",
                          {"scope": "group", "score_pct": pct}))
        hs = sorted(hist.get((gid, sid), []))
        if scope in ("member", "any") and len(hs) >= 60 and hs[-1][0] == s["as_of_date"]:
            z = hs[-1][1]
            mpct = (sum(1 for _, v in hs if v < z) + 0.5 * sum(1 for _, v in hs if v == z)) / len(hs)  # mid-rank
            if mpct >= p:
                cands.append((mpct, f"break-away move within peer group {r['label']}: |z_cond| {z:.2f} at its "
                                    f"{mpct:.1%} trailing-year percentile",
                              {"scope": "member", "member_pct": mpct, "z_cond_abs": z}))
        for v, msg, extra in cands:
            t = Trigger(sid, s["as_of_date"], msg + " - descriptive, model-based",
                        {"group_id": gid, "group_label": r["label"], "percentile": p, "model_based": True, **extra})
            if sid not in best or v > best[sid][0]:
                best[sid] = (v, t)
    return [t for _, t in best.values()]


def _prev_bday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# ---- macro rule types (macro-w2). Expectations are MODEL-BASED (not consensus). Params (all optional):
#   macro_release  {countries: [CC], series_ids: [..], max_importance: 1..3 (default 1)}  release published in (prev bday, as_of]
#   macro_surprise {threshold_z: 2.0, countries, themes: [growth|inflation|labor], series_ids, max_importance: 3}
#   regime_change  {kind: cycle_quadrant|recession_probability|any (default any), countries, level: 0.5, methods: [..]}
#                  cycle quadrant change (macro_cycle) or a recession probability crossing `level` (macro_recession)


def _in(v: Any, allowed: Any) -> bool:
    return not allowed or v in set(allowed)


@evaluator("macro_release")
def _macro_release(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    imp = int(params.get("max_importance", 1))
    out = []
    for r in dp.macro_releases_between(_win(dp, as_of), as_of).iter_rows(named=True):
        if (r["importance"] or 3) > imp or not _in(r["country"], params.get("countries")) \
                or not _in(r["series_id"], params.get("series_ids")):
            continue
        out.append(Trigger(f"macro:{r['country']}", r["release_date"],
                           f"{r['country']} {r['release_name']}: {r['series_id']} = {r['actual']:.4g} "
                           f"(prior {r['prior'] if r['prior'] is None else format(r['prior'], '.4g')})",
                           {"release_event_id": r["release_event_id"], "series_id": r["series_id"],
                            "actual": r["actual"], "prior": r["prior"], "importance": r["importance"]},
                           keys=[f"rel:{r['release_event_id']}"]))
    return out


@evaluator("macro_surprise")
def _macro_surprise(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    thr = abs(float(params.get("threshold_z", 2.0)))
    imp = int(params.get("max_importance", 3))
    out = []
    for r in dp.macro_surprises_between(_win(dp, as_of), as_of).iter_rows(named=True):
        if abs(r["surprise_z"]) < thr or (r["importance"] or 3) > imp or not _in(r["country"], params.get("countries")) \
                or not _in(r["theme"], params.get("themes")) or not _in(r["series_id"], params.get("series_ids")):
            continue
        out.append(Trigger(f"macro:{r['country']}", r["release_date"],
                           f"{r['country']} {r['series_id']} surprise z={r['surprise_z']:+.2f}: actual {r['actual']:.4g} vs "
                           f"model-based expectation {r['expectation']:.4g} (not consensus)",
                           {"release_event_id": r["release_event_id"], "series_id": r["series_id"],
                            "surprise_z": r["surprise_z"], "actual": r["actual"], "expectation": r["expectation"],
                            "model_based": True}, keys=[f"sur:{r['release_event_id']}"]))
    return out


@evaluator("regime_change")
def _regime_change(params: dict[str, Any], ids: list[str], dp: SqlDataProvider, as_of: date) -> list[Trigger]:
    kind = params.get("kind", "any")
    out = []
    if kind in ("any", "cycle_quadrant"):
        try:
            df = dp.macro_cycle_last_two(as_of)
        except Exception:  # macro_cycle not built yet
            df = pl.DataFrame()
        for _, g in (df.group_by("country") if df.height else []):
            rows = sorted(g.iter_rows(named=True), key=lambda r: r["rn"])
            if len(rows) < 2 or not _in(rows[0]["country"], params.get("countries")):
                continue
            if rows[0]["quadrant"] != rows[1]["quadrant"]:
                c = rows[0]
                out.append(Trigger(f"macro:{c['country']}", c["date"],
                                   f"{c['country']} cycle quadrant {rows[1]['quadrant']} -> {c['quadrant']}",
                                   {"kind": "cycle_quadrant", "from": rows[1]["quadrant"], "to": c["quadrant"]}))
    if kind in ("any", "recession_probability"):
        level = float(params.get("level", 0.5))
        df = dp.macro_recession_last_two(as_of)
        for _, g in (df.group_by("method", "country") if df.height else []):
            rows = sorted(g.iter_rows(named=True), key=lambda r: r["rn"])
            if len(rows) < 2 or not _in(rows[0]["country"], params.get("countries")) \
                    or not _in(rows[0]["method"], params.get("methods")):
                continue
            cur, prev = rows[0]["probability"], rows[1]["probability"]
            if (prev < level <= cur) or (prev >= level > cur):
                c = rows[0]
                d = "above" if cur >= level else "below"
                out.append(Trigger(f"macro:{c['country']}", c["date"],
                                   f"{c['country']} recession probability ({c['method']}) crossed {d} {level:.0%}: "
                                   f"{prev:.1%} -> {cur:.1%}",
                                   {"kind": "recession_probability", "method": c["method"], "prev": prev, "value": cur,
                                    "level": level}))
    return out


# ------------------------------------------------------------------------------------------------ engine


def _as_dicts(x: pl.DataFrame | Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if x is None:
        return []
    return list(x.iter_rows(named=True)) if isinstance(x, pl.DataFrame) else list(x)


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _payload_keys(payload: Any) -> list[str]:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except ValueError:
            return []
    return [str(k) for k in (payload or {}).get("dedup_keys") or []] if isinstance(payload, dict) else []


def evaluate_alerts(alerts: pl.DataFrame | Iterable[dict[str, Any]], provider: SqlDataProvider, as_of: date,
                    watchlist_items: pl.DataFrame | Iterable[dict[str, Any]] | None = None,
                    existing_events: pl.DataFrame | Iterable[dict[str, Any]] | None = None,
                    now: datetime | None = None, window_start: date | None = None) -> list[dict[str, Any]]:
    """Pure evaluation: returns new alert_events rows (dicts). No I/O besides provider queries.
    window_start: exclusive start of the event window of the window rules (default: previous business day)."""
    now = now or datetime.now(UTC)
    provider.window_start = window_start  # type: ignore[attr-defined]
    win = _win(provider, as_of)
    members: dict[str, list[str]] = {}
    for it in _as_dicts(watchlist_items):
        members.setdefault(it["watchlist_id"], []).append(it["security_id"])
    last_fire: dict[tuple[str, str], datetime] = {}
    fired_keys: set[tuple[str, str, date]] = set()
    for ev in _as_dicts(existing_events):
        k = (ev["alert_id"], ev["security_id"])
        fa = _utc(ev["fired_at"])
        if fa and (k not in last_fire or fa > last_fire[k]):
            last_fire[k] = fa
        fired_keys.add((ev["alert_id"], ev["security_id"], ev["as_of_date"]))
    seen: dict[str, set[str]] = {}
    for ev in _as_dicts(existing_events):
        ks = _payload_keys(ev.get("payload"))
        if ks:
            seen.setdefault(ev["alert_id"], set()).update(ks)
    events: list[dict[str, Any]] = []
    for a in _as_dicts(alerts):
        if not a.get("enabled", True):
            continue
        rt = a["rule_type"]
        fn = EVALUATORS.get(rt)
        if fn is None:
            log.warning("alert %s: unknown rule_type %s", a["alert_id"], rt)
            continue
        params = a["params"]
        if isinstance(params, str):
            params = json.loads(params or "{}")
        ids = [a["security_id"]] if a.get("security_id") else members.get(a.get("watchlist_id") or "", [])
        if rt in MACRO_RULE_TYPES:
            ids = ["macro"]  # country-level rules: targets come from the params (countries filter)
        ids = sorted(set(i for i in ids if i))
        if not ids:
            continue
        try:
            triggers = fn(params or {}, ids, provider, as_of)
        except Exception as e:  # one broken rule must not stop the others
            log.warning("alert %s (%s) evaluation failed: %s", a["alert_id"], rt, e)
            continue
        cooldown = timedelta(days=int(a.get("cooldown_days") or 0))
        done = seen.setdefault(a["alert_id"], set())
        for t in triggers:
            key = (a["alert_id"], t.security_id)
            if t.keys:
                fresh = [k for k in t.keys if k not in done]
                if not fresh:
                    continue                          # every underlying event already alerted (earlier run / window)
                if len(fresh) < len(t.keys) and isinstance(t.payload.get("filings"), list):
                    fl = [f for f in t.payload["filings"] if f"filing:{f.get('filing_id')}" in fresh]
                    t.payload["filings"] = fl
                    t.message = "new filing: " + ", ".join(sorted({str(f.get("form_type")) for f in fl}))
                t.keys = fresh
            elif (a["alert_id"], t.security_id, t.as_of_date) in fired_keys:
                continue
            lf = last_fire.get(key)
            if lf is not None and cooldown and now - lf < cooldown:
                continue
            extra = {"dedup_keys": t.keys, "window_start": str(win)} if t.keys else {}
            events.append({
                "event_id": uuid.uuid4().hex, "alert_id": a["alert_id"], "security_id": t.security_id, "fired_at": now,
                "as_of_date": t.as_of_date, "message": t.message,
                "payload": json.dumps({"rule_type": rt, **t.payload, **extra}, default=str), "read": False,
            })
            fired_keys.add((a["alert_id"], t.security_id, t.as_of_date))
            done.update(t.keys)
            last_fire[key] = now
    return events


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]).fetchone()[0])


def _profile_period_days(settings: Settings) -> int:
    """Refresh period of the active schedule profile (weekly 7, daily 1); weekly when unknown."""
    try:
        from app.ops.schedule import active_profile, load_config  # noqa: PLC0415

        pid, _src = active_profile(settings)
        return max(1, int((load_config(settings).get("profiles", {}).get(pid) or {}).get("period_days") or 7))
    except Exception:  # noqa: BLE001 - schedule config unreadable: assume the default weekly profile
        return 7


def window_state(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    try:
        return json.loads((s.state_dir / WINDOW_STATE_FILE).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def alert_window_start(as_of: date, settings: Settings | None = None, state: dict[str, Any] | None = None,
                       period_days: int | None = None) -> date:
    """Exclusive start of the event window for a run at as_of (see the module docstring)."""
    s = settings or get_settings()
    st = window_state(s) if state is None else state
    period = period_days if period_days is not None else _profile_period_days(s)
    try:
        last = date.fromisoformat(str(st.get("last_as_of"))) if st.get("last_as_of") else None
        prev_ws = date.fromisoformat(str(st.get("window_start"))) if st.get("window_start") else None
    except ValueError:
        last, prev_ws = None, None
    if last is not None and last < as_of:
        start = last                                      # everything after the last successful run
    elif last is not None and last == as_of and prev_ws is not None:
        start = prev_ws                                   # re-run of the same date: same window (dedup keeps it quiet)
    else:
        start = as_of - timedelta(days=period)            # first run (or clock moved back): one schedule period
    start = min(start, _prev_bday(as_of))
    return max(start, as_of - timedelta(days=MAX_WINDOW_DAYS))


def _write_window_state(s: Settings, as_of: date, start: date, n_events: int) -> None:
    p = s.state_dir / WINDOW_STATE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"last_as_of": as_of.isoformat(), "window_start": start.isoformat(),
                                   "finished_at": datetime.now(UTC).isoformat(), "events": n_events}), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("alerts window state not written: %s", e)


def evaluate_and_store(as_of: date, provider: SqlDataProvider | None = None, settings: Settings | None = None,
                       con: duckdb.DuckDBPyConnection | None = None) -> list[dict[str, Any]]:
    """Read rules from app_state.duckdb, evaluate, append alert_events and set alerts.last_fired_at.
    The event window starts at the last successful run (alert_window_start); a successful run records its as_of."""
    s = settings or get_settings()
    own = con is None
    con = con or connect_app_state(settings=s)
    try:
        ensure_duckdb_tables(con, ["alert_events"])
        if not _table_exists(con, "alerts"):
            log.info("no alerts table yet (API has not created any rule)")
            return []
        start = alert_window_start(as_of, s)
        alerts = con.execute("SELECT * FROM alerts WHERE enabled").pl()
        if alerts.is_empty():
            _write_window_state(s, as_of, start, 0)
            return []
        items = con.execute("SELECT watchlist_id, security_id FROM watchlist_items").pl() \
            if _table_exists(con, "watchlist_items") else None
        existing = con.execute("SELECT alert_id, security_id, fired_at, as_of_date, payload FROM alert_events "
                               "WHERE fired_at >= now() - INTERVAL 400 DAY").pl()
        events = evaluate_alerts(alerts, provider or lake_provider(s), as_of, items, existing, window_start=start)
        if events:
            df = pl.DataFrame(events)
            con.register("_new_events", df.to_arrow())
            con.execute("""INSERT INTO alert_events (event_id, alert_id, security_id, fired_at, as_of_date, message, payload, read)
                           SELECT event_id, alert_id, security_id, fired_at, as_of_date, message, payload, read FROM _new_events""")
            con.unregister("_new_events")
            last = df.group_by("alert_id").agg(pl.col("fired_at").max())
            for r in last.iter_rows(named=True):
                con.execute("UPDATE alerts SET last_fired_at = ? WHERE alert_id = ?", [r["fired_at"], r["alert_id"]])
        _write_window_state(s, as_of, start, len(events))
        return events
    finally:
        if own:
            con.close()


def run(ctx: Any) -> Any:
    """jobs.yaml `alerts_eval`."""
    from app.scheduler.base import JobResult

    events = evaluate_and_store(ctx.as_of, settings=ctx.settings)
    msg = f"{len(events)} alert events fired"
    ctx.log.info(msg)
    return JobResult(status="success", rows_written={"alert_events": len(events)}, entities_ok=len(events), message=msg)
