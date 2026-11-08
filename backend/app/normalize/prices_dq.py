"""Price-coverage DQ rules (hook `price_coverage` in app.core.dq; docs/MODEL_STUDIO_PLAN.md P3).

* coverage:<MIC>          share of current primary listings of each bulk-covered market with a bar in the last
                          `recent_days` calendar days (warn below `min_share`; markets without listings are skipped).
* freshness:<region>      days since the latest bar per prices_daily region (warn above `max_age_days`).
* delisted_window         bars dated after the listing's valid_to for inactive securities (must be 0, error).
* instruments:<type> / history:<type> / ohlc_consistency:<type>   market instruments (fx, future, index, etf;
                          price-history workstream): freshness share, median history depth, OHLC consistency.
* large_cap_jumps         (O2.2a, audit F04/F32) securities with market cap > $2bn whose adjusted close moved > 3x
                          (or < 1/3) in one bar in the last 400 days without a corporate action that day (warn: never blocks publishing):
                          ticker reuse or unit flips that escaped the prices_finalize guards.
"""

from __future__ import annotations

from typing import Any

COVERAGE_MICS = ("XSHG", "XSHE", "XTAI", "ROCO", "BVMF", "XTKS", "XKRX", "XKOS", "XHKG")


def coverage_outcomes(ctx: Any, recent_days: int = 10, min_share: float = 0.85, max_age_days: int = 6) -> list[Any]:
    from app.core.dq import DqOutcome  # noqa: PLC0415

    if not ctx.has_data("prices_daily"):
        return []
    out: list[Any] = []
    if ctx.has_data("listings"):
        rows = ctx.con.execute(f"""
            WITH cur AS (SELECT listing_id, mic FROM listings
                         WHERE is_primary AND valid_to IS NULL AND mic IN ({",".join(f"'{m}'" for m in COVERAGE_MICS)})),
                 px AS (SELECT DISTINCT listing_id FROM prices_daily
                        WHERE date >= current_date - INTERVAL {int(recent_days)} DAY)
            SELECT cur.mic, COUNT(*) AS n, COUNT(px.listing_id) AS n_px
            FROM cur LEFT JOIN px USING (listing_id) GROUP BY cur.mic ORDER BY cur.mic""").fetchall()
        for mic, n, n_px in rows:
            share = (n_px / n) if n else 0.0
            out.append(DqOutcome("prices_daily", f"coverage:{mic}", "warn", share >= min_share, share, min_share,
                                 {"listings": int(n), "with_recent_bar": int(n_px), "recent_days": recent_days}))
        if ctx.has_data("securities_master"):
            bad = ctx.scalar("""
                SELECT COUNT(*) FROM prices_daily p
                JOIN listings l ON l.listing_id = p.listing_id AND l.valid_to IS NOT NULL AND l.is_primary
                JOIN securities_master s ON s.security_id = p.security_id AND NOT s.is_active
                WHERE p.date > l.valid_to""") or 0
            out.append(DqOutcome("prices_daily", "delisted_window", "error", int(bad) == 0, float(bad), 0.0,
                                 {"rows_after_delisting": int(bad)}))
    if ctx.has_data("securities_master"):
        out += instrument_outcomes(ctx, recent_days)
    if ctx.has_data("valuation_daily"):
        out += jump_outcomes(ctx)
    for region, last, age in ctx.con.execute("""
            SELECT region, MAX(date), date_diff('day', MAX(date), current_date) FROM prices_daily
            GROUP BY region ORDER BY region""").fetchall():
        out.append(DqOutcome("prices_daily", f"freshness:{region}", "warn", int(age) <= max_age_days, float(age),
                             float(max_age_days), {"last_date": str(last)}))
    return out


#: market-instrument types (price-history workstream: FX pairs, futures, indices incl. volatility, ETFs)
INSTRUMENT_TYPES = ("fx", "future", "index", "etf")


def instrument_outcomes(ctx: Any, recent_days: int = 10, min_share: float = 0.9, min_median_years: float = 10.0,
                        max_ohlc_bad: float = 0.01) -> list[Any]:
    """Per instrument type (securities_master.security_type of the IDX:/ETF: market instruments):
    * instruments:<type>      share with a bar in the last `recent_days` days (warn below `min_share`)
    * history:<type>          median years of daily history (warn below `min_median_years`)
    * ohlc_consistency:<type> share of bars flagged ohlc_inconsistent (warn above `max_ohlc_bad`)"""
    from app.core.dq import DqOutcome  # noqa: PLC0415

    types = ",".join(f"'{t}'" for t in INSTRUMENT_TYPES)
    rows = ctx.con.execute(f"""
        WITH inst AS (SELECT security_id, CAST(security_type AS VARCHAR) AS t FROM securities_master
                      WHERE is_active AND CAST(security_type AS VARCHAR) IN ({types})
                        AND (security_id LIKE 'IDX:%' OR security_id LIKE 'ETF:%')),
             px AS (SELECT p.security_id, max(p.date) AS last, min(p.date) AS first, count(*) AS n,
                           count(*) FILTER (WHERE list_contains(p.dq_flags, 'ohlc_inconsistent')) AS bad
                    FROM prices_daily p JOIN inst USING (security_id) GROUP BY 1)
        SELECT inst.t, count(*) AS n, count(px.security_id) FILTER (
                   WHERE px.last >= current_date - INTERVAL {int(recent_days)} DAY) AS fresh,
               median(date_diff('day', px.first, px.last) / 365.25) AS med_years,
               sum(px.bad) AS bad, sum(px.n) AS bars
        FROM inst LEFT JOIN px USING (security_id) GROUP BY 1 ORDER BY 1""").fetchall()
    out: list[Any] = []
    for t, n, fresh, med_years, bad, bars in rows:
        share = (fresh / n) if n else 0.0
        out.append(DqOutcome("prices_daily", f"instruments:{t}", "warn", share >= min_share, share, min_share,
                             {"instruments": int(n), "with_recent_bar": int(fresh), "recent_days": recent_days}))
        my = float(med_years or 0.0)
        out.append(DqOutcome("prices_daily", f"history:{t}", "warn", my >= min_median_years, my, min_median_years,
                             {"median_years": round(my, 1)}))
        rate = (float(bad or 0) / float(bars)) if bars else 0.0
        out.append(DqOutcome("prices_daily", f"ohlc_consistency:{t}", "warn", rate <= max_ohlc_bad, rate,
                             max_ohlc_bad, {"bars": int(bars or 0), "ohlc_inconsistent": int(bad or 0)}))
    return out


def jump_outcomes(ctx: Any, min_mcap_usd: float = 2e9, max_ratio: float = 3.0, days: int = 400) -> list[Any]:
    """large_cap_jumps: > max_ratio one-bar adjusted moves without a corporate action, mcap > min_mcap_usd."""
    from app.core.dq import DqOutcome  # noqa: PLC0415

    ca = ("LEFT JOIN (SELECT DISTINCT security_id, ex_date FROM corporate_actions) a "
          "ON a.security_id = j.security_id AND a.ex_date = j.date") if ctx.has_data("corporate_actions") else ""
    ca_null = "AND a.security_id IS NULL" if ca else ""
    rows = ctx.con.execute(f"""
        WITH p AS (SELECT security_id, date, close * adj_factor AS a FROM prices_daily
                   WHERE date >= current_date - INTERVAL {int(days) + 10} DAY AND security_id NOT LIKE 'IDX:%'
                     AND security_id NOT LIKE 'ETF:%' AND close > 0),
             j AS (SELECT security_id, date, a / lag(a) OVER (PARTITION BY security_id ORDER BY date) AS r FROM p),
             m AS (SELECT security_id, arg_max(market_cap_usd, date) AS mcap FROM valuation_daily
                   WHERE date >= current_date - INTERVAL 30 DAY GROUP BY 1)
        SELECT j.security_id, j.date, j.r FROM j JOIN m USING (security_id) {ca}
        WHERE m.mcap > {float(min_mcap_usd)} AND j.date >= current_date - INTERVAL {int(days)} DAY
          AND (j.r > {float(max_ratio)} OR j.r < {1.0 / float(max_ratio)}) {ca_null}
        ORDER BY j.date DESC LIMIT 50""").fetchall()
    n = len({r[0] for r in rows})
    return [DqOutcome("prices_daily", "large_cap_jumps", "warn", n == 0, float(n), 0.0,
                      {"securities": n, "examples": [{"security_id": r[0], "date": str(r[1]), "ratio": round(float(r[2]), 3)}
                                                     for r in rows[:10]]})]
