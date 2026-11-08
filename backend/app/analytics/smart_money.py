"""Smart-money signals (roadmap #7; job `smart_money_signals`, nightly) -> si_signals, activist_events,
smart_money_signals (+ the rank-IC sanity report, data/logs/reports/smart_money_ic_<date>.json).

si_signals (FINRA short interest, official; SEC fails-to-deliver, official), per security x settlement date, PIT on
the FINRA publication date: si_pct_out = short interest / shares outstanding (float is not published - shares
outstanding is the documented proxy; valuation_daily on / before the settlement, else fundamentals shares_outstanding
known by the publication date; null if unknown or > 150%), si_chg_pct vs the previous settlement, si_z_1y = z of the
shares short vs the previous 24 settlements (>= 8), si_pct_rank (cross-section), ret_20d (adj_close settlement vs 28 days
before), FTD: largest daily fail of the latest half-month SEC file published by the SI publication date, ftd_pct_out,
ftd_z vs the previous 12 files, ftd_spike (MODEL-BASED: >= 0.5% of shares outstanding, the Reg SHO threshold level,
or z >= 3 with >= 100k shares). squeeze_flag (MODEL-BASED): si_pct_out >= 15% and days_to_cover >= 5 and ret_20d >=
+10% and low float (shares outstanding <= 150M or market cap <= $2B); squeeze_score = mean percentile of SI %, days to
cover, 20d return and small share count within the settlement date.

activist_events (SEC 13D / 13G, official), per filing: new_13d, amend_13d, switch_13g_to_13d (a 13D whose filer's
previous filing on the same subject within 3 years was a 13G), new_13g, amend_13g.

smart_money_signals (MODEL-BASED composite, weekly Fridays + the run date), per security x as_of_date, every input
known on as_of_date (knowledge_date <= as_of_date): 13F flow (flow_pct_out, breadth; latest quarter cutoff <= D and
period end within 200 days), smart-cohort flow (smart_flow_pct_out), insider (insider_signals.signal_score, <= 35 days
old), short interest (si_pct_out level and si_chg_pct, publication <= 45 days old; higher = bearish) and EU / UK
disclosed net short % (regulator aggregate when published, else the sum of active >= 0.5% holder positions; level and
30-day change; higher = bearish). Each raw input is winsorised (1 / 99%) and robust-z-scored within the date
((x - median) / 1.4826 MAD, clipped +-4; since 2026-10-27), signed so that
higher = more bullish, averaged into five groups (inst, smart, insider, short_interest, eu_short); score = mean of the
available group z's, score_pct = percentile 0..100 within the date. NO forward-return columns (label-free); the
backtesting lab joins forward returns itself.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.analytics.smart_money")

SQUEEZE = {"si_pct": 0.15, "dtc": 5.0, "ret_20d": 0.10, "max_shares": 150e6, "max_mcap": 2e9}
FTD_SPIKE = {"pct_out": 0.005, "z": 3.0, "min_shares": 100_000}
GROUPS = {"inst": ["z_inst_flow", "z_inst_breadth"], "smart": ["z_smart_flow"], "insider": ["z_insider"],
          "short_interest": ["z_si_level", "z_si_chg"], "eu_short": ["z_eu_level", "z_eu_chg"]}
#: raw column -> (z column, sign: +1 = higher is bullish)
RAW = {"inst_flow": ("z_inst_flow", 1), "inst_breadth": ("z_inst_breadth", 1), "smart_flow": ("z_smart_flow", 1),
       "insider_signal": ("z_insider", 1), "si_pct_out": ("z_si_level", -1), "si_chg_pct": ("z_si_chg", -1),
       "eu_short_pct": ("z_eu_level", -1), "eu_short_chg_30d": ("z_eu_chg", -1)}
TABLES = ("short_interest", "fails_to_deliver", "valuation_daily", "fundamentals_canonical", "securities_master",
          "prices_daily", "activist_filings", "inst_ownership", "insider_signals", "eu_short_positions", "issuers")


def connect(settings: Settings | None = None, memory: str = "2GB") -> duckdb.DuckDBPyConnection:
    from app.core.storage import duckdb_connect, register_views  # noqa: PLC0415

    s = settings or get_settings()
    con = duckdb_connect(None, settings=s)
    spill = s.data_dir / "tmp" / "x7_duck"
    spill.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET memory_limit = '{memory}'")
    con.execute("SET threads = 4")
    con.execute(f"SET temp_directory = '{spill.as_posix()}'")
    con.execute("SET TimeZone = 'UTC'")
    register_views(con, list(TABLES), settings=s)
    return con


# ------------------------------------------------------------------------------------------------ short interest


def si_signals(con: duckdb.DuckDBPyConnection, as_of: date) -> pl.DataFrame:
    q = SQUEEZE
    f = FTD_SPIKE
    return con.execute(f"""
        WITH si AS (
            SELECT security_id, settlement_date, short_interest_shares AS si_shares, avg_daily_volume, days_to_cover,
                   knowledge_date, coalesce(knowledge_date_estimated, false) AS kd_est
            FROM short_interest
            WHERE security_id NOT LIKE 'finra:%' AND knowledge_date <= ? AND short_interest_shares >= 0),
        prim AS (SELECT issuer_id, arg_max(security_id, is_primary_security::INT) AS security_id
                 FROM securities_master GROUP BY 1),
        vso AS (SELECT s.security_id, s.settlement_date, arg_max(v.shares_out, v.date) AS so, arg_max(v.market_cap_usd, v.date) AS mcap
                FROM si s JOIN valuation_daily v ON v.security_id = s.security_id AND v.date <= s.settlement_date
                     AND v.date > s.settlement_date - INTERVAL 30 DAY WHERE v.shares_out > 0 GROUP BY 1, 2),
        fso AS (SELECT s.security_id, s.settlement_date, arg_max(fc.value, (fc.period_end, fc.knowledge_date)) AS so
                FROM si s JOIN prim p ON p.security_id = s.security_id
                JOIN fundamentals_canonical fc ON fc.issuer_id = p.issuer_id AND fc.item_id = 'shares_outstanding'
                     AND fc.knowledge_date <= s.knowledge_date AND fc.period_end > s.settlement_date - INTERVAL 400 DAY
                     AND fc.value > 0
                GROUP BY 1, 2),
        px AS (SELECT s.security_id, s.settlement_date,
                      arg_max(p.adj_close, p.date) FILTER (WHERE p.date > s.settlement_date - INTERVAL 7 DAY) AS px0,
                      arg_max(p.adj_close, p.date) FILTER (WHERE p.date <= s.settlement_date - INTERVAL 28 DAY) AS px20
               FROM si s JOIN prices_daily p ON p.security_id = s.security_id AND p.date <= s.settlement_date
                    AND p.date > s.settlement_date - INTERVAL 40 DAY AND p.adj_close > 0
               GROUP BY 1, 2),
        ftd_f AS (SELECT security_id, file_name, max(quantity_fails) AS ftd_max, max(knowledge_date) AS ftd_kd,
                         max(settlement_date) AS ftd_end
                  FROM fails_to_deliver WHERE security_id IS NOT NULL AND knowledge_date <= ? GROUP BY 1, 2),
        ftd_z AS (SELECT *, (ftd_max - avg(ftd_max) OVER w) / NULLIF(stddev_samp(ftd_max) OVER w, 0) AS ftd_z,
                         count(*) OVER w AS ftd_n
                  FROM ftd_f WINDOW w AS (PARTITION BY security_id ORDER BY ftd_end ROWS BETWEEN 12 PRECEDING AND 1 PRECEDING)),
        ftd AS (SELECT s.security_id, s.settlement_date, arg_max(z.ftd_max, z.ftd_end) AS ftd_max_shares,
                       arg_max(CASE WHEN z.ftd_n >= 4 THEN z.ftd_z END, z.ftd_end) AS ftd_z,
                       max(z.ftd_kd) AS ftd_knowledge_date, max(z.ftd_end) AS ftd_end
                FROM si s JOIN ftd_z z ON z.security_id = s.security_id AND z.ftd_kd <= s.knowledge_date
                     AND z.ftd_end > s.settlement_date - INTERVAL 31 DAY
                GROUP BY 1, 2),
        b AS (
            SELECT s.*, coalesce(v.so, f2.so) AS shares_out,
                   CASE WHEN v.so IS NOT NULL THEN 'valuation_daily' WHEN f2.so IS NOT NULL THEN 'fundamentals' END AS shares_out_source,
                   v.mcap AS market_cap_usd, px.px0 / NULLIF(px.px20, 0) - 1 AS ret_20d,
                   ftd.ftd_max_shares, ftd.ftd_z, ftd.ftd_knowledge_date
            FROM si s LEFT JOIN vso v USING (security_id, settlement_date) LEFT JOIN fso f2 USING (security_id, settlement_date)
            LEFT JOIN px USING (security_id, settlement_date) LEFT JOIN ftd USING (security_id, settlement_date)),
        c AS (
            SELECT *, CASE WHEN shares_out > 0 AND si_shares / shares_out <= 1.5 THEN si_shares / shares_out END AS si_pct_out,
                   si_shares / NULLIF(lag(si_shares) OVER (PARTITION BY security_id ORDER BY settlement_date), 0) - 1 AS si_chg_pct,
                   ftd_max_shares / NULLIF(shares_out, 0) AS ftd_pct_out
            FROM b),
        d AS (
            SELECT *, si_shares AS lvl FROM c),
        e AS (
            SELECT *, CASE WHEN count(*) OVER w >= 8 THEN (lvl - avg(lvl) OVER w) / NULLIF(stddev_samp(lvl) OVER w, 0) END AS si_z_1y
            FROM d WINDOW w AS (PARTITION BY security_id ORDER BY settlement_date ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING)),
        r AS (
            SELECT *,
                   CASE WHEN si_pct_out IS NOT NULL THEN percent_rank() OVER (PARTITION BY settlement_date, si_pct_out IS NULL ORDER BY si_pct_out) END AS si_pct_rank,
                   CASE WHEN si_pct_out IS NOT NULL THEN percent_rank() OVER (PARTITION BY settlement_date, si_pct_out IS NULL ORDER BY si_pct_out) END AS r1,
                   percent_rank() OVER (PARTITION BY settlement_date ORDER BY coalesce(days_to_cover, 0)) AS r2,
                   percent_rank() OVER (PARTITION BY settlement_date ORDER BY coalesce(ret_20d, 0)) AS r3,
                   CASE WHEN shares_out IS NOT NULL THEN percent_rank() OVER (PARTITION BY settlement_date, shares_out IS NULL ORDER BY -shares_out) END AS r4
            FROM e)
        SELECT security_id, settlement_date, knowledge_date, kd_est AS knowledge_date_estimated, si_shares, avg_daily_volume,
               days_to_cover, shares_out, shares_out_source, market_cap_usd, si_pct_out, si_chg_pct, si_z_1y, si_pct_rank,
               ret_20d,
               CASE WHEN si_pct_out IS NOT NULL THEN (r1 + r2 + r3 + coalesce(r4, 0.5)) / 4 END AS squeeze_score,
               coalesce(si_pct_out >= {q['si_pct']} AND days_to_cover >= {q['dtc']} AND ret_20d >= {q['ret_20d']}
                        AND (shares_out <= {q['max_shares']} OR market_cap_usd <= {q['max_mcap']}), false) AS squeeze_flag,
               ftd_max_shares, ftd_pct_out, ftd_z, ftd_knowledge_date,
               coalesce(ftd_pct_out >= {f['pct_out']} OR (ftd_z >= {f['z']} AND ftd_max_shares >= {f['min_shares']}), false)
                   AS ftd_spike,
               year(settlement_date)::SMALLINT AS year
        FROM r""", [as_of, as_of]).pl()


# ------------------------------------------------------------------------------------------------ activism


def activist_events(con: duckdb.DuckDBPyConnection, as_of: date) -> pl.DataFrame:
    return con.execute("""
        WITH f AS (SELECT * FROM activist_filings WHERE knowledge_date <= ?),
        p AS (
            SELECT f.*, lag(schedule) OVER w AS prev_schedule, lag(knowledge_date) OVER w AS prev_date,
                   max(CASE WHEN schedule = '13G' THEN knowledge_date END) OVER (
                       PARTITION BY filer_cik, subject_cik ORDER BY knowledge_date, filing_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_13g_date,
                   count(*) FILTER (WHERE schedule = '13D') OVER (
                       PARTITION BY filer_cik, subject_cik ORDER BY knowledge_date, filing_id
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS n_prior_13d
            FROM f WINDOW w AS (PARTITION BY filer_cik, subject_cik ORDER BY knowledge_date, filing_id))
        SELECT filing_id, knowledge_date AS event_date, knowledge_date,
               CASE WHEN schedule = '13D' AND filer_cik IS NOT NULL AND prev_schedule = '13G'
                         AND prev_date >= knowledge_date - INTERVAL 3 YEAR THEN 'switch_13g_to_13d'
                    WHEN schedule = '13D' AND NOT is_amendment THEN 'new_13d'
                    WHEN schedule = '13D' THEN 'amend_13d'
                    WHEN NOT is_amendment THEN 'new_13g' ELSE 'amend_13g' END AS event_type,
               form_type, schedule, is_amendment, subject_cik, subject_name, filer_cik, filer_name, n_filers,
               issuer_id, security_id, prior_13g_date, n_prior_13d::INTEGER AS n_prior_13d, role_source, url,
               year(knowledge_date)::SMALLINT AS year
        FROM p""", [as_of]).pl()


# ------------------------------------------------------------------------------------------------ EU shorts


def eu_short_asof(con: duckdb.DuckDBPyConnection, d: date) -> pl.DataFrame:
    """security_id -> disclosed net short % (sum of active holder positions, or the regulator aggregate when the
    regulator publishes aggregates by d), n_holders, as of d (knowledge_date <= d)."""
    return con.execute("""
        WITH e AS (SELECT * FROM eu_short_positions WHERE knowledge_date <= ? AND security_id IS NOT NULL),
        ind AS (
            SELECT regulator, security_id, sum(position_pct) AS pct, count(*) AS n FROM (
                SELECT *, row_number() OVER (PARTITION BY regulator, holder_name, isin ORDER BY position_date DESC,
                                                          knowledge_date DESC) AS rn
                FROM e WHERE position_type = 'individual')
            WHERE rn = 1 AND NOT below_threshold AND position_pct >= 0.5 AND position_date > ? - INTERVAL 730 DAY
            GROUP BY 1, 2),
        agg AS (
            SELECT regulator, security_id, arg_max(position_pct, position_date) AS pct FROM e
            WHERE position_type = 'aggregate' AND position_date > ? - INTERVAL 730 DAY GROUP BY 1, 2),
        regs_agg AS (SELECT DISTINCT regulator FROM e WHERE position_type = 'aggregate'),
        comb AS (
            SELECT coalesce(a.regulator, i.regulator) AS regulator, coalesce(a.security_id, i.security_id) AS security_id,
                   CASE WHEN coalesce(a.regulator, i.regulator) IN (SELECT regulator FROM regs_agg)
                        THEN coalesce(a.pct, 0) ELSE i.pct END AS pct, i.n
            FROM agg a FULL OUTER JOIN ind i ON i.regulator = a.regulator AND i.security_id = a.security_id)
        SELECT security_id, max(pct) AS eu_short_pct, sum(coalesce(n, 0))::INTEGER AS eu_short_holders FROM comb
        GROUP BY 1""", [d, d, d]).pl()


# ------------------------------------------------------------------------------------------------ composite


def signal_dates(as_of: date, start: date) -> list[date]:
    d = start + timedelta(days=(4 - start.weekday()) % 7)
    out = []
    while d <= as_of:
        out.append(d)
        d += timedelta(days=7)
    if not out or out[-1] != as_of:
        out.append(as_of)
    return out


def raw_components(con: duckdb.DuckDBPyConnection, d: date, si: pl.DataFrame) -> pl.DataFrame:
    inst = con.execute("""
        SELECT security_id, arg_max(coalesce(flow_pct_out, shares_held_chg_pct), knowledge_date) AS inst_flow,
               arg_max(breadth, knowledge_date) AS inst_breadth,
               arg_max(coalesce(smart_flow_pct_out, smart_net_flow_shares / NULLIF(shares_held_prev, 0)), knowledge_date) AS smart_flow,
               max(knowledge_date) AS inst_kd, arg_max(period_end, knowledge_date) AS inst_period_end
        FROM inst_ownership WHERE knowledge_date <= ? AND period_end > ? - INTERVAL 200 DAY GROUP BY 1""", [d, d]).pl()
    ins = con.execute("""
        SELECT security_id, arg_max(signal_score, as_of_date) AS insider_signal, max(as_of_date) AS insider_kd
        FROM insider_signals WHERE as_of_date <= ? AND as_of_date > ? - INTERVAL 35 DAY GROUP BY 1""", [d, d]).pl()
    s = (si.filter((pl.col("knowledge_date") <= d) & (pl.col("knowledge_date") > d - timedelta(days=45)))
         .sort("knowledge_date").group_by("security_id").agg(
             pl.col("si_pct_out").last(), pl.col("si_chg_pct").last(), pl.col("settlement_date").last().alias("si_settlement_date"),
             pl.col("knowledge_date").last().alias("si_kd")))
    eu = eu_short_asof(con, d)
    eu30 = eu_short_asof(con, d - timedelta(days=30)).rename({"eu_short_pct": "eu30", "eu_short_holders": "_h30"})
    eu = eu.join(eu30, on="security_id", how="full", coalesce=True).with_columns(
        pl.col("eu_short_pct").fill_null(0.0), pl.col("eu30").fill_null(0.0)).with_columns(
        (pl.col("eu_short_pct") - pl.col("eu30")).alias("eu_short_chg_30d")).drop("eu30", "_h30")
    eu = eu.with_columns(pl.col("eu_short_holders").fill_null(0))
    out = inst
    for x in (ins, s, eu):
        out = out.join(x, on="security_id", how="full", coalesce=True)
    return out.with_columns(pl.lit(d).alias("as_of_date"))


Z_CLIP = 4.0


def _z(col: str) -> pl.Expr:
    """Robust cross-sectional z: (x - median) / (1.4826 MAD), clipped to +-4 (IQR / 1.349, then the std as fallback
    scales). The flows are extremely heavy-tailed (13F smart_flow ranges -0.66 .. 1,047 because small prior holdings
    blow up the ratio), so the former mean / std z on 1 / 99 % winsorized values put the whole centre of the
    distribution at one value (z_smart_flow ~ -0.14 for most names, z_inst_flow ~ -0.16; completeness audit
    2026-10-27)."""
    x = pl.col(col).cast(pl.Float64)
    lo, hi = x.quantile(0.01), x.quantile(0.99)
    w = x.clip(lo, hi)
    med = w.median()
    mad = (w - med).abs().median() * 1.4826
    iqr = (w.quantile(0.75) - w.quantile(0.25)) / 1.349
    sd = w.std()
    scale = pl.when(mad > 0).then(mad).when(iqr > 0).then(iqr).when(sd > 0).then(sd).otherwise(None)
    return ((w - med) / scale).clip(-Z_CLIP, Z_CLIP).fill_nan(None)


def standardize(raw: pl.DataFrame) -> pl.DataFrame:
    df = raw
    for c, (zc, sign) in RAW.items():
        if c not in df.columns:
            df = df.with_columns(pl.lit(None, pl.Float64).alias(c))
        df = df.with_columns((sign * _z(c).over("as_of_date")).alias(zc))
    grp = []
    for g, cols in GROUPS.items():
        grp.append(pl.mean_horizontal([pl.col(c) for c in cols]).alias(f"g_{g}"))
    df = df.with_columns(grp)
    gcols = [f"g_{g}" for g in GROUPS]
    df = df.with_columns(pl.sum_horizontal([pl.col(c).is_not_null().cast(pl.Int32) for c in gcols]).alias("n_components"),
                         pl.mean_horizontal([pl.col(c) for c in gcols]).alias("score"))
    df = df.filter(pl.col("n_components") >= 1)
    return df.with_columns(((pl.col("score").rank("average").over("as_of_date") - 0.5)
                            / pl.len().over("as_of_date") * 100.0).alias("score_pct"))


def composite(con: duckdb.DuckDBPyConnection, si: pl.DataFrame, dates: list[date]) -> pl.DataFrame:
    parts = [raw_components(con, d, si) for d in dates]
    raw = pl.concat([p for p in parts if p.height], how="diagonal_relaxed") if parts else pl.DataFrame()
    if raw.is_empty():
        return raw
    df = standardize(raw)
    meta = con.execute("""SELECT s.security_id, s.issuer_id, coalesce(i.region, 'OTHER') AS region, i.sector_id
                          FROM securities_master s LEFT JOIN issuers i USING (issuer_id)""").pl()
    df = df.join(meta, on="security_id", how="inner")
    kd = pl.max_horizontal([pl.col(c) for c in ("inst_kd", "insider_kd", "si_kd") if c in df.columns])
    return df.with_columns(pl.coalesce(kd, pl.col("as_of_date")).alias("knowledge_date"),
                           pl.col("as_of_date").dt.year().cast(pl.Int16).alias("year"))


# ------------------------------------------------------------------------------------------------ sanity check


def rank_ic(con: duckdb.DuckDBPyConnection, sig: pl.DataFrame, horizons: tuple[int, ...] = (21, 63),
            cols: tuple[str, ...] = ("score", "g_inst", "g_smart", "g_insider", "g_short_interest", "g_eu_short"),
            ) -> dict[str, Any]:
    """Spearman rank IC of signal columns vs forward returns, PIT and purged: entry = first close AFTER as_of_date
    (the signal date's own close is never used), exit = h trading days later; dates are sampled at least h trading
    days apart (non-overlapping windows) so the t-stat uses independent observations."""
    if sig.is_empty():
        return {}
    ids = sig["security_id"].unique().to_list()
    con.register("_ids", pl.DataFrame({"security_id": ids}).to_arrow())
    first = min(sig["as_of_date"])
    px = con.execute("""SELECT security_id, date, adj_close FROM prices_daily
                        WHERE security_id IN (SELECT security_id FROM _ids) AND date > ? AND adj_close > 0
                        ORDER BY security_id, date""", [first]).pl()
    con.unregister("_ids")
    px = px.with_columns(pl.col("date").rank("dense").over("security_id").alias("_k"))
    dates = sorted(sig["as_of_date"].unique().to_list())
    out: dict[str, Any] = {"n_securities": len(ids), "first_date": str(dates[0]), "last_date": str(dates[-1]), "horizons": {}}
    bdays = sorted(px["date"].unique().to_list())
    for h in horizons:
        pick, last_idx = [], -10**9
        for d in dates:
            i = int(np.searchsorted(np.array(bdays, dtype="datetime64[D]"), np.datetime64(d), side="right"))
            if i + h >= len(bdays):
                break
            if i - last_idx >= h:
                pick.append((d, bdays[i], bdays[i + h]))
                last_idx = i
        res: dict[str, Any] = {"n_dates": len(pick), "by_column": {}}
        per: dict[str, list[float]] = {c: [] for c in cols}
        for d, d0, d1 in pick:
            p0 = px.filter(pl.col("date") == d0).select("security_id", pl.col("adj_close").alias("p0"))
            p1 = px.filter(pl.col("date") == d1).select("security_id", pl.col("adj_close").alias("p1"))
            x = sig.filter(pl.col("as_of_date") == d).join(p0, on="security_id").join(p1, on="security_id")
            x = x.with_columns((pl.col("p1") / pl.col("p0") - 1).alias("fwd"))
            for c in cols:
                if c not in x.columns:
                    continue
                y = x.filter(pl.col(c).is_not_null() & pl.col("fwd").is_not_null())
                if y.height < 30:
                    continue
                per[c].append(float(np.corrcoef(y[c].rank(), y["fwd"].rank())[0, 1]))
        for c, v in per.items():
            if not v:
                continue
            a = np.array(v)
            sd = float(a.std(ddof=1)) if len(a) > 1 else float("nan")
            res["by_column"][c] = {"mean_ic": round(float(a.mean()), 4), "n": len(a),
                                   "t_stat": round(float(a.mean() / (sd / np.sqrt(len(a)))), 2) if len(a) > 1 and sd > 0 else None,
                                   "hit_rate": round(float((a > 0).mean()), 3)}
        out["horizons"][f"{h}d"] = res
    return out


# ------------------------------------------------------------------------------------------------ job


SIGNAL_COLS = ["as_of_date", "security_id", "issuer_id", "region", "sector_id", "knowledge_date", "inst_period_end",
               "inst_flow", "inst_breadth", "smart_flow", "insider_signal", "si_pct_out", "si_chg_pct",
               "si_settlement_date", "eu_short_pct", "eu_short_chg_30d", "eu_short_holders",
               *[z for z, _ in RAW.values()], *[f"g_{g}" for g in GROUPS], "n_components", "score", "score_pct", "year"]


def run(ctx: Any) -> Any:
    from app.core.storage import write_table  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = dict(ctx.params or {})
    now = datetime.now(UTC)
    con = connect(s)
    try:
        si = si_signals(con, ctx.as_of)
        ev = activist_events(con, ctx.as_of)
        start = date.fromisoformat(p["start"]) if p.get("start") else ctx.as_of - timedelta(days=int(p.get("days", 35)))
        sig = composite(con, si, signal_dates(ctx.as_of, start))
        rep = rank_ic(con, sig) if p.get("ic_report") else {}
    finally:
        con.close()
    rows = {}
    rows["si_signals"] = write_table(si.with_columns(pl.lit(now).alias("computed_at"), pl.lit(ctx.run_id).alias("run_id")),
                                     "si_signals", mode="overwrite", settings=s)
    rows["activist_events"] = write_table(ev.with_columns(pl.lit(now).alias("computed_at")), "activist_events",
                                          mode="overwrite", settings=s)
    if sig.height:
        sig = sig.select([c for c in SIGNAL_COLS if c in sig.columns]).with_columns(
            pl.lit(now).alias("computed_at"), pl.lit(ctx.run_id).alias("run_id"))
        rows["smart_money_signals"] = write_table(sig, "smart_money_signals", settings=s)
    msg = f"si={rows['si_signals']} events={rows['activist_events']} signals={rows.get('smart_money_signals', 0)}"
    if rep:
        path = s.data_dir / "logs" / "reports" / f"smart_money_ic_{ctx.as_of:%Y%m%d}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rep, indent=1), encoding="utf-8")
        msg += f" ic_report={path.name}"
    return JobResult(status="success", rows_written=rows, message=msg)
