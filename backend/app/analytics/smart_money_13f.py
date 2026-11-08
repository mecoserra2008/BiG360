"""13F ownership analytics (roadmap #7 smart money; job `smart_money_13f`, weekly) -> inst_filers, inst_ownership.

Point-in-time rule. A quarter's 13F snapshot is taken at its CUTOFF = the filing deadline (period end + 45 days,
rolled to the next SEC business day) + 1 business day or the run date while the quarter is still being filed (is_complete = false). Only
holdings rows with knowledge_date (= the filing date) <= cutoff are used and every output row carries
knowledge_date = cutoff: nothing is visible before the filings were public. Caveat: institutional_holdings keeps the
latest version of a (filer, period, CUSIP) key, so a restatement filed after the deadline removes that filer's
original rows from the deadline snapshot (conservative, ~5% of filings are amendments).

Filer metrics (inst_filers), per filer x quarter (long equity positions = put_call 'none', amount type SH):
  aum_usd, n_positions, top10_weight, hhi_weights (sum of squared value weights), option_share (put + call value /
  total reported value), turnover = min(buys, sells) / mean(AUM q, AUM q-1) with buys / sells = |share changes| x
  price (value / shares) per CUSIP (CRSP-style, so price moves are not turnover).
  stock_share = value in positions mapped to single stocks (common / ADR / preferred / REIT units) / AUM.
  HEDGE-FUND-LIKE (transparent rule, labelled model-based): 10 <= n_positions <= 1000, AUM >= $50M, stock_share >= 0.6
  and (turnover >= 0.15 or option_share >= 0.05 or (top10_weight >= 0.50 and turnover >= 0.05)), i.e. active,
  options-using or concentrated-and-trading stock pickers; index-like books (> 1000 positions), ETF-allocation wealth
  managers (low stock_share) and buy-and-hold corporate / strategic holders (turnover < 5%) never qualify.
  Portfolio return of the reported quarter-end book over the following quarter (buy-and-hold, value weights at the
  period end, adj_close from prices_daily, mapped securities only; ret_coverage = priced value / AUM >= 0.5 required),
  minus the equal-weight average of all filers that quarter = excess_ret_next_q. It is known only at the NEXT period
  end (ret_known_date) and is a past-performance input, never a forward label of the score.
  SMART COHORT (model-based): at quarter q, smart_score = mean excess return of the filer's books of q-4 .. q-1 (all
  realised by period end q, before cutoff q; >= 3 quarters); a hedge-fund-like filer is in the cohort when its
  smart_score is > 0 and in the top decile of hedge-fund-like filers with a score.

Splits: 13F share counts are as reported; previous-quarter shares are re-expressed on the current share basis with
prices_daily cumulative split factors before any change / flow / turnover is computed.

Security metrics (inst_ownership), per security x quarter (rows mapped to a security_id via CUSIP):
  n_holders, shares_held, value_usd, shares_out (valuation_daily on / before the period end, else the latest
  fundamentals shares_outstanding known by the cutoff), inst_own_pct (null if shares_out unknown or ratio > 2),
  changes vs the previous quarter (among filers that filed both quarters): n_new / n_added / n_trimmed / n_exited /
  n_unchanged (|change| <= 1% = unchanged), net_flow_shares, flow_pct_out, breadth = (new + added - trimmed - exited)
  / previous holders; hhi (holder concentration of 13F shares) and top10_share; hedge-fund-like holders, their share of
  shares outstanding and hf_top20_count = number of hedge-fund-like filers holding the security among their top-20
  positions (crowding; crowding_pct = percentile within the quarter, model-based); smart cohort flows; put / call value.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import duckdb
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.analytics.smart_money_13f")

DEADLINE_DAYS = 45
MIN_FILERS_QUARTER = 1000          # quarters with fewer filers are stray late filings, not analysed
HF_RULE = {"min_positions": 10, "max_positions": 1000, "min_aum": 50e6, "stock_share": 0.60, "turnover": 0.15,
           "options": 0.05, "top10": 0.50, "top10_turnover": 0.05}
CHANGE_TOL = 0.01
SMART_MIN_Q = 3
SMART_TOP = 0.10


def _presidents_day(y: int) -> date:
    d = date(y, 2, 15)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d


def _is_bday(d: date) -> bool:
    return d.weekday() < 5 and d != _presidents_day(d.year)


def deadline(pe: date) -> date:
    """Snapshot cutoff: 13F due date (period end + 45 days, rolled to the next SEC business day) + 1 business day
    (EDGAR dates filings accepted after 17:30 ET on the next business day). Weekends + Presidents' Day (the only
    federal holiday that can fall on a 13F due date) are skipped."""
    d = pe + timedelta(days=DEADLINE_DAYS)
    while not _is_bday(d):
        d += timedelta(days=1)
    d += timedelta(days=1)
    while not _is_bday(d):
        d += timedelta(days=1)
    return d


def is_quarter_end(d: date) -> bool:
    return (d.month, d.day) in ((3, 31), (6, 30), (9, 30), (12, 31))


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
    register_views(con, ["institutional_holdings", "prices_daily", "valuation_daily", "fundamentals_canonical",
                         "securities_master"], settings=s)
    return con


def quarters(con: duckdb.DuckDBPyConnection, as_of: date, start: date | None = None,
             min_filers: int = MIN_FILERS_QUARTER) -> list[date]:
    rows = con.execute(f"""SELECT period_end, count(DISTINCT filer_cik) FROM institutional_holdings
                           WHERE knowledge_date <= ? GROUP BY 1 HAVING count(DISTINCT filer_cik) >= {int(min_filers)}
                           ORDER BY 1""", [as_of]).fetchall()
    return [r[0] for r in rows if is_quarter_end(r[0]) and (start is None or r[0] >= start)]


# ------------------------------------------------------------------------------------------------ staging


def stage_holdings(con: duckdb.DuckDBPyConnection, qs: list[date], as_of: date) -> list[tuple[date, date, bool]]:
    """Temp tables: _qtr (pe, cutoff, complete, idx), _h (filer x cusip long SH rows used), _opt (option value per
    security). Returns [(pe, cutoff, complete)]."""
    meta = [(pe, min(deadline(pe), as_of), as_of >= deadline(pe)) for pe in qs]
    con.execute("CREATE OR REPLACE TEMP TABLE _qtr (pe DATE, cutoff DATE, complete BOOLEAN, idx INTEGER)")
    con.executemany("INSERT INTO _qtr VALUES (?, ?, ?, ?)", [(pe, c, k, i) for i, (pe, c, k) in enumerate(meta)])
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _h AS
        SELECT h.filer_cik, any_value(h.filer_name) AS filer_name, h.period_end AS pe, q.idx, h.cusip,
               any_value(h.security_id) AS security_id, sum(h.shares)::DOUBLE AS shares,
               sum(h.value_usd)::DOUBLE AS value_usd, max(h.knowledge_date) AS kd,
               coalesce(any_value(sm.security_type) IN ('common', 'adr', 'gdr', 'preferred', 'reit_unit'), false) AS is_stock
        FROM institutional_holdings h JOIN _qtr q ON q.pe = h.period_end AND h.knowledge_date <= q.cutoff
        LEFT JOIN (SELECT security_id, any_value(security_type) AS security_type FROM securities_master GROUP BY 1) sm
               ON sm.security_id = h.security_id
        WHERE h.put_call = 'none' AND coalesce(h.amount_type, 'SH') = 'SH' AND h.shares > 0
        GROUP BY h.filer_cik, h.period_end, q.idx, h.cusip""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _opt AS
        SELECT h.security_id, q.idx, (sum(h.value_usd) FILTER (WHERE h.put_call = 'put'))::DOUBLE AS put_value,
               (sum(h.value_usd) FILTER (WHERE h.put_call = 'call'))::DOUBLE AS call_value,
               sum(h.value_usd)::DOUBLE AS opt_value_all, h.filer_cik
        FROM institutional_holdings h JOIN _qtr q ON q.pe = h.period_end AND h.knowledge_date <= q.cutoff
        WHERE h.put_call IN ('put', 'call') GROUP BY h.security_id, q.idx, h.filer_cik""")
    return meta


def stage_prices(con: duckdb.DuckDBPyConnection) -> None:
    """_px: adj_close of mapped securities on / up to 10 days before each period end."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _px AS
        WITH ids AS (SELECT DISTINCT security_id FROM _h WHERE security_id IS NOT NULL)
        SELECT p.security_id, q.pe, arg_max(p.adj_close, p.date) AS px, arg_max(p.split_factor, p.date) AS sf
        FROM prices_daily p JOIN _qtr q ON p.date <= q.pe AND p.date > q.pe - INTERVAL 10 DAY
        WHERE p.security_id IN (SELECT security_id FROM ids) AND p.adj_close > 0
          AND p.date >= (SELECT min(pe) FROM _qtr) - INTERVAL 10 DAY
        GROUP BY 1, 2""")


def stage_splits(con: duckdb.DuckDBPyConnection) -> None:
    """13F share counts are not split-adjusted: add _h.shares_nb = shares re-expressed on the NEXT quarter's share
    basis (x split_factor(next period end) / split_factor(this period end), prices_daily cumulative split factors), so
    quarter-over-quarter changes are not distorted by splits."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _ratio AS
        SELECT a.security_id, qa.idx, b.sf / a.sf AS ratio
        FROM _px a JOIN _qtr qa ON qa.pe = a.pe JOIN _qtr qb ON qb.idx = qa.idx + 1
        JOIN _px b ON b.security_id = a.security_id AND b.pe = qb.pe
        WHERE a.sf > 0 AND b.sf > 0 AND abs(b.sf / a.sf - 1) > 1e-6""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _h AS
        SELECT h.*, h.shares * coalesce(r.ratio, 1.0) AS shares_nb
        FROM _h h LEFT JOIN _ratio r ON r.security_id = h.security_id AND r.idx = h.idx""")


def stage_shares(con: duckdb.DuckDBPyConnection) -> None:
    """_so: shares outstanding per security x quarter (valuation_daily first, else fundamentals known by cutoff)."""
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _so AS
        WITH ids AS (SELECT DISTINCT security_id FROM _h WHERE security_id IS NOT NULL),
        v AS (SELECT v.security_id, q.idx, arg_max(v.shares_out, v.date) AS so
              FROM valuation_daily v JOIN _qtr q ON v.date <= q.pe AND v.date > q.pe - INTERVAL 30 DAY
              WHERE v.security_id IN (SELECT security_id FROM ids) AND v.shares_out > 0 GROUP BY 1, 2),
        prim AS (SELECT issuer_id, arg_max(security_id, is_primary_security::INT) AS security_id
                 FROM securities_master GROUP BY 1),
        f AS (SELECT p.security_id, q.idx, arg_max(f.value, (f.period_end, f.knowledge_date)) AS so
              FROM fundamentals_canonical f JOIN prim p USING (issuer_id)
              JOIN _qtr q ON f.knowledge_date <= q.cutoff AND f.period_end <= q.pe + INTERVAL 45 DAY
                         AND f.period_end > q.pe - INTERVAL 400 DAY
              WHERE f.item_id = 'shares_outstanding' AND f.value > 0
                AND p.security_id IN (SELECT security_id FROM ids) GROUP BY 1, 2)
        SELECT coalesce(v.security_id, f.security_id) AS security_id, coalesce(v.idx, f.idx) AS idx,
               coalesce(v.so, f.so) AS shares_out,
               CASE WHEN v.so IS NOT NULL THEN 'valuation_daily' ELSE 'fundamentals' END AS shares_out_source
        FROM v FULL OUTER JOIN f ON f.security_id = v.security_id AND f.idx = v.idx""")


# ------------------------------------------------------------------------------------------------ filers


def filer_metrics(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    r = HF_RULE
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _f AS
        WITH base AS (
            SELECT filer_cik, idx, any_value(filer_name) AS filer_name, max(kd) AS knowledge_date,
                   count(*) AS n_positions, sum(value_usd) AS aum_usd,
                   sum(value_usd) FILTER (WHERE is_stock) / NULLIF(sum(value_usd), 0) AS stock_share
            FROM _h GROUP BY 1, 2),
        w AS (SELECT h.filer_cik, h.idx, h.value_usd / NULLIF(b.aum_usd, 0) AS wt,
                     row_number() OVER (PARTITION BY h.filer_cik, h.idx ORDER BY h.value_usd DESC) AS rk
              FROM _h h JOIN base b USING (filer_cik, idx)),
        conc AS (SELECT filer_cik, idx, sum(wt) FILTER (WHERE rk <= 10) AS top10_weight, sum(wt * wt) AS hhi_weights
                 FROM w GROUP BY 1, 2),
        opt AS (SELECT filer_cik, idx, sum(opt_value_all) AS opt_value FROM _opt GROUP BY 1, 2),
        chg AS (
            SELECT coalesce(c.filer_cik, p.filer_cik) AS filer_cik, coalesce(c.idx, p.idx + 1) AS idx,
                   sum(greatest(coalesce(c.shares, 0) - coalesce(p.shares_nb, 0), 0)
                       * coalesce(c.value_usd / NULLIF(c.shares, 0), p.value_usd / NULLIF(p.shares_nb, 0))) AS buys,
                   sum(greatest(coalesce(p.shares_nb, 0) - coalesce(c.shares, 0), 0)
                       * coalesce(c.value_usd / NULLIF(c.shares, 0), p.value_usd / NULLIF(p.shares_nb, 0))) AS sells
            FROM _h c FULL OUTER JOIN _h p ON p.filer_cik = c.filer_cik AND p.cusip = c.cusip AND p.idx = c.idx - 1
            GROUP BY 1, 2)
        SELECT b.*, q.pe AS period_end, q.cutoff, q.complete, c.top10_weight, c.hhi_weights,
               coalesce(o.opt_value, 0) / NULLIF(b.aum_usd + coalesce(o.opt_value, 0), 0) AS option_share,
               CASE WHEN bp.aum_usd IS NOT NULL THEN least(ch.buys, ch.sells) / NULLIF((b.aum_usd + bp.aum_usd) / 2, 0)
               END AS turnover
        FROM base b JOIN _qtr q USING (idx)
        LEFT JOIN conc c USING (filer_cik, idx) LEFT JOIN opt o USING (filer_cik, idx)
        LEFT JOIN base bp ON bp.filer_cik = b.filer_cik AND bp.idx = b.idx - 1
        LEFT JOIN chg ch ON ch.filer_cik = b.filer_cik AND ch.idx = b.idx""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _f2 AS
        SELECT *, coalesce(n_positions BETWEEN {r['min_positions']} AND {r['max_positions']} AND aum_usd >= {r['min_aum']}
                   AND stock_share >= {r['stock_share']}
                   AND (coalesce(turnover, 0) >= {r['turnover']} OR option_share >= {r['options']}
                        OR (top10_weight >= {r['top10']} AND coalesce(turnover, 0) >= {r['top10_turnover']})), false)
                   AS hedge_fund_like
        FROM _f""")
    # reported-book return over the following quarter (past performance once realised)
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _ret AS
        WITH pr AS (
            SELECT h.filer_cik, h.idx, sum(h.value_usd) AS priced_value,
                   sum(h.value_usd * (p1.px / p0.px - 1)) / NULLIF(sum(h.value_usd), 0) AS ret
            FROM _h h JOIN _qtr q0 ON q0.idx = h.idx JOIN _qtr q1 ON q1.idx = h.idx + 1
            JOIN _px p0 ON p0.security_id = h.security_id AND p0.pe = q0.pe
            JOIN _px p1 ON p1.security_id = h.security_id AND p1.pe = q1.pe
            WHERE h.security_id IS NOT NULL AND p1.px / p0.px BETWEEN 0.02 AND 50
            GROUP BY 1, 2)
        SELECT pr.filer_cik, pr.idx, pr.ret, pr.priced_value / NULLIF(f.aum_usd, 0) AS ret_coverage, q1.pe AS ret_known_date
        FROM pr JOIN _f2 f USING (filer_cik, idx) JOIN _qtr q1 ON q1.idx = pr.idx + 1""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _ret2 AS
        SELECT r.*, r.ret - avg(r.ret) OVER (PARTITION BY r.idx) AS excess_ret
        FROM _ret r WHERE r.ret_coverage >= 0.5""")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _filers AS
        WITH sc AS (
            SELECT f.filer_cik, f.idx, avg(r.excess_ret) AS smart_score, count(r.excess_ret) AS smart_n_quarters
            FROM _f2 f JOIN _ret2 r ON r.filer_cik = f.filer_cik AND r.idx BETWEEN f.idx - 4 AND f.idx - 1
            GROUP BY 1, 2),
        rk AS (
            SELECT f.filer_cik, f.idx, s.smart_score, s.smart_n_quarters,
                   CASE WHEN f.hedge_fund_like AND s.smart_n_quarters >= {SMART_MIN_Q}
                        THEN percent_rank() OVER (PARTITION BY f.idx, (f.hedge_fund_like AND s.smart_n_quarters >= {SMART_MIN_Q})
                                                  ORDER BY s.smart_score) END AS smart_rank_pct
            FROM _f2 f LEFT JOIN sc s USING (filer_cik, idx))
        SELECT f.filer_cik, f.filer_name, f.period_end, f.cutoff AS knowledge_date, f.complete AS is_complete,
               f.n_positions::INTEGER AS n_positions, f.aum_usd, f.stock_share, f.top10_weight, f.hhi_weights, f.option_share,
               f.turnover, f.hedge_fund_like, r.ret AS ret_next_q, r.excess_ret AS excess_ret_next_q, r.ret_coverage,
               r.ret_known_date, k.smart_score, k.smart_n_quarters::INTEGER AS smart_n_quarters, k.smart_rank_pct,
               coalesce(k.smart_rank_pct >= {1 - SMART_TOP} AND k.smart_score > 0, false) AS in_smart_cohort, f.idx
        FROM _f2 f LEFT JOIN _ret2 r USING (filer_cik, idx) LEFT JOIN rk k USING (filer_cik, idx)""")
    return con.execute("SELECT * EXCLUDE (idx) FROM _filers").pl()


# ------------------------------------------------------------------------------------------------ securities


def security_metrics(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    tol = CHANGE_TOL
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _hs AS
        SELECT filer_cik, idx, security_id, sum(shares) AS shares, sum(shares_nb) AS shares_nb, sum(value_usd) AS value_usd
        FROM _h WHERE security_id IS NOT NULL GROUP BY 1, 2, 3""")
    con.execute("CREATE OR REPLACE TEMP TABLE _filed AS SELECT DISTINCT filer_cik, idx FROM _h")
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _chg AS
        SELECT coalesce(c.security_id, p.security_id) AS security_id, coalesce(c.idx, p.idx + 1) AS idx,
               coalesce(c.filer_cik, p.filer_cik) AS filer_cik, coalesce(c.shares, 0) AS sh, coalesce(p.shares_nb, 0) AS sh_prev,
               CASE WHEN p.shares IS NULL THEN 'new' WHEN c.shares IS NULL THEN 'exited'
                    WHEN c.shares > p.shares_nb * (1 + {tol}) THEN 'added' WHEN c.shares < p.shares_nb * (1 - {tol}) THEN 'trimmed'
                    ELSE 'unchanged' END AS kind
        FROM _hs c FULL OUTER JOIN _hs p ON p.security_id = c.security_id AND p.filer_cik = c.filer_cik AND p.idx = c.idx - 1
        WHERE EXISTS (SELECT 1 FROM _filed a WHERE a.filer_cik = coalesce(c.filer_cik, p.filer_cik)
                                              AND a.idx = coalesce(c.idx, p.idx + 1))
          AND EXISTS (SELECT 1 FROM _filed b WHERE b.filer_cik = coalesce(c.filer_cik, p.filer_cik)
                                              AND b.idx = coalesce(c.idx, p.idx + 1) - 1)
          AND coalesce(c.idx, p.idx + 1) <= (SELECT max(idx) FROM _qtr)""")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _top20 AS
        SELECT h.security_id, h.idx, count(*) AS hf_top20_count FROM (
            SELECT filer_cik, idx, security_id,
                   row_number() OVER (PARTITION BY filer_cik, idx ORDER BY value_usd DESC) AS rk FROM _hs) h
        JOIN _filers f ON f.filer_cik = h.filer_cik AND f.idx = h.idx AND f.hedge_fund_like
        WHERE h.rk <= 20 GROUP BY 1, 2""")
    return con.execute("""
        WITH agg AS (
            SELECT h.security_id, h.idx, count(*) AS n_holders, sum(h.shares) AS shares_held,
                   sum(h.shares_nb) AS shares_held_nb, sum(h.value_usd) AS value_usd,
                   sum(h.shares) FILTER (WHERE f.hedge_fund_like) AS hf_shares,
                   count(*) FILTER (WHERE f.hedge_fund_like) AS n_hf_holders,
                   count(*) FILTER (WHERE f.in_smart_cohort) AS n_smart_holders
            FROM _hs h JOIN _filers f ON f.filer_cik = h.filer_cik AND f.idx = h.idx GROUP BY 1, 2),
        conc AS (
            SELECT security_id, idx, sum(w * w) AS hhi, sum(w) FILTER (WHERE rk <= 10) AS top10_share FROM (
                SELECT security_id, idx, shares / sum(shares) OVER (PARTITION BY security_id, idx) AS w,
                       row_number() OVER (PARTITION BY security_id, idx ORDER BY shares DESC) AS rk FROM _hs)
            GROUP BY 1, 2),
        ch AS (
            SELECT c.security_id, c.idx,
                   count(*) FILTER (WHERE kind = 'new') AS n_new, count(*) FILTER (WHERE kind = 'added') AS n_added,
                   count(*) FILTER (WHERE kind = 'trimmed') AS n_trimmed, count(*) FILTER (WHERE kind = 'exited') AS n_exited,
                   count(*) FILTER (WHERE kind = 'unchanged') AS n_unchanged,
                   count(*) FILTER (WHERE sh_prev > 0) AS n_prev_cont,
                   sum(sh - sh_prev) AS net_flow_shares,
                   sum(sh - sh_prev) FILTER (WHERE f.in_smart_cohort) AS smart_net_flow_shares,
                   count(*) FILTER (WHERE f.in_smart_cohort AND kind IN ('new', 'added')) AS smart_buyers,
                   count(*) FILTER (WHERE f.in_smart_cohort AND kind IN ('trimmed', 'exited')) AS smart_sellers,
                   sum(sh - sh_prev) FILTER (WHERE f.hedge_fund_like) AS hf_net_flow_shares
            FROM _chg c LEFT JOIN _filers f ON f.filer_cik = c.filer_cik AND f.idx = c.idx GROUP BY 1, 2),
        op AS (SELECT security_id, idx, sum(put_value) AS put_value_usd, sum(call_value) AS call_value_usd
               FROM _opt WHERE security_id IS NOT NULL GROUP BY 1, 2),
        cur AS (
            SELECT a.*, q.pe AS period_end, q.cutoff AS knowledge_date, q.complete AS is_complete, so.shares_out,
                   so.shares_out_source, cn.hhi, cn.top10_share, ch.n_new, ch.n_added, ch.n_trimmed, ch.n_exited,
                   ch.n_unchanged, ch.n_prev_cont, ch.net_flow_shares, ch.smart_net_flow_shares, ch.smart_buyers,
                   ch.smart_sellers, ch.hf_net_flow_shares, coalesce(t.hf_top20_count, 0) AS hf_top20_count,
                   op.put_value_usd, op.call_value_usd,
                   CASE WHEN so.shares_out > 0 AND a.shares_held / so.shares_out <= 2 THEN a.shares_held / so.shares_out END
                       AS inst_own_pct
            FROM agg a JOIN _qtr q USING (idx) LEFT JOIN _so so USING (security_id, idx) LEFT JOIN conc cn USING (security_id, idx)
            LEFT JOIN ch USING (security_id, idx) LEFT JOIN _top20 t USING (security_id, idx) LEFT JOIN op USING (security_id, idx))
        SELECT c.security_id, s.issuer_id, c.period_end, c.knowledge_date, c.is_complete,
               c.n_holders::INTEGER AS n_holders, p.n_holders::INTEGER AS n_holders_prev,
               c.n_new::INTEGER AS n_new, c.n_added::INTEGER AS n_added, c.n_trimmed::INTEGER AS n_trimmed,
               c.n_exited::INTEGER AS n_exited, c.n_unchanged::INTEGER AS n_unchanged,
               c.shares_held, p.shares_held_nb AS shares_held_prev, c.value_usd, c.shares_out, c.shares_out_source,
               c.inst_own_pct, p.inst_own_pct AS inst_own_pct_prev, c.inst_own_pct - p.inst_own_pct AS inst_own_chg,
               c.shares_held / NULLIF(p.shares_held_nb, 0) - 1 AS shares_held_chg_pct,
               c.net_flow_shares, c.net_flow_shares / NULLIF(c.shares_out, 0) AS flow_pct_out,
               (coalesce(c.n_new, 0) + coalesce(c.n_added, 0) - coalesce(c.n_trimmed, 0) - coalesce(c.n_exited, 0))
                   / NULLIF(c.n_prev_cont + coalesce(c.n_new, 0), 0) AS breadth,
               c.hhi, c.top10_share, c.n_hf_holders::INTEGER AS n_hf_holders,
               c.hf_shares / NULLIF(c.shares_out, 0) AS hf_shares_pct, c.hf_shares / NULLIF(c.shares_held, 0) AS hf_share_of_inst,
               c.hf_net_flow_shares, c.hf_top20_count::INTEGER AS hf_top20_count,
               percent_rank() OVER (PARTITION BY c.idx ORDER BY c.hf_top20_count) AS crowding_pct,
               c.n_smart_holders::INTEGER AS n_smart_holders, c.smart_net_flow_shares,
               c.smart_net_flow_shares / NULLIF(c.shares_out, 0) AS smart_flow_pct_out,
               c.smart_buyers::INTEGER AS smart_buyers, c.smart_sellers::INTEGER AS smart_sellers,
               c.put_value_usd, c.call_value_usd,
               c.put_value_usd / NULLIF(c.call_value_usd, 0) AS put_call_ratio
        FROM cur c LEFT JOIN cur p ON p.security_id = c.security_id AND p.idx = c.idx - 1
        LEFT JOIN (SELECT security_id, any_value(issuer_id) AS issuer_id FROM securities_master GROUP BY 1) s
               ON s.security_id = c.security_id""").pl()


def compute(con: duckdb.DuckDBPyConnection, as_of: date, start: date | None = None,
            min_filers: int = MIN_FILERS_QUARTER) -> tuple[pl.DataFrame, pl.DataFrame]:
    qs = quarters(con, as_of, start, min_filers)
    if len(qs) < 1:
        return pl.DataFrame(), pl.DataFrame()
    stage_holdings(con, qs, as_of)
    stage_prices(con)
    stage_splits(con)
    stage_shares(con)
    filers = filer_metrics(con)
    secs = security_metrics(con)
    return filers, secs


def run(ctx: Any) -> Any:
    from app.core.storage import write_table  # noqa: PLC0415
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    con = connect(s)
    try:
        start = date.fromisoformat(ctx.params["start"]) if ctx.params.get("start") else None
        filers, secs = compute(con, ctx.as_of, start)
    finally:
        con.close()
    if secs.is_empty():
        return JobResult(status="skipped", message="no 13F quarter with enough filers")
    now = datetime.now(UTC)
    filers = filers.with_columns(pl.lit(now).alias("computed_at"), pl.lit(ctx.run_id).alias("run_id"))
    secs = secs.with_columns(pl.lit(now).alias("computed_at"), pl.lit(ctx.run_id).alias("run_id"))
    rows = {"inst_filers": write_table(filers, "inst_filers", settings=s),
            "inst_ownership": write_table(secs, "inst_ownership", settings=s)}
    qs = sorted(secs["period_end"].unique().to_list())
    n_hf = filers.filter(pl.col("hedge_fund_like") & (pl.col("period_end") == qs[-1])).height
    n_sm = filers.filter(pl.col("in_smart_cohort") & (pl.col("period_end") == qs[-1])).height
    return JobResult(status="success", rows_written=rows, entities_ok=len(qs),
                     message=f"quarters {qs[0]}..{qs[-1]} ({len(qs)}); latest: hf_like={n_hf} smart_cohort={n_sm}")
