"""BQL-lite security (injection attempts are rejected or inert: literals are bound parameters, identifiers come only
from the field dictionary) and limits (rows, universe x dates, timeout)."""

from __future__ import annotations

import time

import duckdb
import pytest

from app.bql import compiler as C
from app.bql import engine
from app.bql.grammar import BqlError, BqlSyntaxError, parse


def run(env, q, **kw):
    return engine.run(q, env["db"], use_cache=False, **kw)


LEX_REJECTED = [
    "get(px_last); DROP TABLE sec",
    "get(px_last) for(['AAA']) -- comment",
    "get(px_last) /* x */ for(['AAA'])",
    "get(px_last) for(['AAA']) where(1 = 1) ; SELECT 1",
    "get(px_last) for(['AAA']) where(`x` = 1)",
    "get(px_last) for(['AAA']) where(x @> 1)",
    "get(px_last) for(['AAA']) where(x :: int)",
    "get(px_last) for(['AAA']) with(currency=$1)",
]


@pytest.mark.parametrize("q", LEX_REJECTED)
def test_lexer_rejects_sql_syntax(q):
    with pytest.raises(BqlSyntaxError):
        parse(q)


SEMANTIC_REJECTED = [
    ("get(search_text) for(['AAA US'])", "unknown field"),                    # internal columns are not fields
    ("get(sec.close) for(['AAA US'])", "unknown field"),
    ("get(read_csv('c:/x.csv')) for(['AAA US'])", "unknown function"),
    ("get(px_last) for(read_parquet('x'))", "unknown universe function"),
    ("get(px_last) for(members('x'' UNION SELECT security_id FROM sec --'))", "unknown universe"),
    ("get(px_last) for(members('watchlist:x'' OR 1=1'))", "unknown watchlist"),
    ("get(px_last) for(['AAA US']) with(currency='USD''; --')", "currency must be"),
    ("get(px_last) for(['AAA US']) with(freq='D OR 1=1')", "freq must be"),
    ("get(px_last) for(['AAA US']) with(evil=1)", "unknown with() option"),
    ("get(px_last) for(['AAA US']) group(sector).agg(sum_of_all)", "unknown aggregation"),
    ("get(px_last) for(['AAA US']) sort(drop desc)", "not an output column"),
    ("get(chg_pct(px, 'x'' ; drop')) for(['AAA US'])", "window"),
    ("get(px_last) for(sector('x''); DROP TABLE sec; --'))", "unknown sector"),
]


@pytest.mark.parametrize("q,msg", SEMANTIC_REJECTED)
def test_semantic_rejections(bql_env, q, msg):
    with pytest.raises(BqlError) as ei:
        run(bql_env, q)
    assert msg in str(ei.value)


INERT = [
    "get(px_last) for(['AAA'' OR ''1''=''1'])",
    "get(px_last) for(['AAA US']) where(name == 'x''; DROP TABLE sec; --')",
    "get(px_last, 'Robert''); DROP TABLE sec;--' as s) for(['AAA US'])",
    "get(px_last) for(['AAA US']) where(ticker in ['AAA'' OR 1=1 --', 'x'])",
]


@pytest.mark.parametrize("q", INERT)
def test_string_literals_are_bound_parameters(bql_env, q):
    r = run(bql_env, q)  # runs without error; the strings are data
    ex = engine.explain(q, bql_env["db"])
    for frag in ("DROP TABLE", "OR 1=1", "OR '1'='1"):
        assert frag not in ex["sql"] and frag not in ex["universe_sql"]
    assert r["n_rows"] <= 1
    # the snapshot is untouched
    assert bql_env["con"].execute("SELECT count(*) FROM sec").fetchone()[0] == 5


def test_identifiers_only_from_dictionary(bql_env):
    ex = engine.explain("get(roic, tech.hi52_pct, chg_pct(px, 1M)) for(['AAA US'])", bql_env["db"])
    # every quoted identifier in the SQL is a dictionary / builder identifier (no user text)
    import re

    quoted = set(re.findall(r'"([^"]+)"', ex["sql"]))
    allowed = {"security_id", "issuer_id", "date", "close", "close_usd", "adj_close", "open", "high", "low", "volume",
               "m_roic", "pct_hi52", "tv3_factors", "knowledge_date", "period_end", "value", "metrics", "sec"}
    assert quoted <= allowed, quoted - allowed


def test_row_limits_and_cells(bql_env, monkeypatch):
    r = run(bql_env, "get(px) for(['AAA US']) with(dates=range(-1Y, 0D), freq=D)", max_rows=10)
    assert r["n_rows"] == 10 and r["truncated"]
    monkeypatch.setattr(C, "MAX_CELLS", 50)
    with pytest.raises(BqlError, match="query too large"):
        run(bql_env, "get(px) for(members('full')) with(dates=range(-1M, 0D), freq=D)")
    monkeypatch.setattr(C, "MAX_CELLS", 2_000_000)
    monkeypatch.setattr(C, "MAX_DAILY_ROWS", 100)
    with pytest.raises(BqlError, match="daily bars"):
        run(bql_env, "get(chg_pct(px, 1Y)) for(members('full'))")
    monkeypatch.setattr(C, "MAX_DAILY_ROWS", 40_000_000)
    monkeypatch.setattr(C, "MAX_UNIVERSE", 2)
    with pytest.raises(BqlError, match="universe of 4"):
        run(bql_env, "get(px_last) for(members('full'))")
    monkeypatch.setattr(C, "MAX_UNIVERSE", 25_000)
    with pytest.raises(BqlError, match="date span"):
        run(bql_env, "get(px) for(['AAA US']) with(dates=range(-31Y, 0D), freq=M)")
    monkeypatch.setattr(C, "MAX_GRID_POINTS", 100)
    with pytest.raises(BqlError, match="lower frequency"):
        run(bql_env, "get(px) for(['AAA US']) with(dates=range(-1Y, 0D), freq=D)")


class _P:
    def __init__(self, sql: str):
        self.sql, self.params = sql, {}


def test_timeout_interrupts_the_statement():
    con = duckdb.connect()
    cur = con.cursor()
    t0 = time.monotonic()
    with pytest.raises(engine.BqlTimeout, match="timed out"):
        engine._execute(cur, _P("SELECT count(*) FROM range(100000000000) a, range(10) b"), 0.3)  # type: ignore[arg-type]
    assert time.monotonic() - t0 < 10
    # the cursor is usable afterwards
    assert cur.execute("SELECT 42").fetchone()[0] == 42
    con.close()


def test_timeout_setting(monkeypatch):
    monkeypatch.setenv("BIG360_BQL_TIMEOUT_S", "0.01")
    assert engine.timeout_s() == 0.5
    monkeypatch.setenv("BIG360_BQL_TIMEOUT_S", "9999")
    assert engine.timeout_s() == engine.MAX_TIMEOUT_S
    monkeypatch.setenv("BIG360_BQL_TIMEOUT_S", "bad")
    assert engine.timeout_s() == engine.DEFAULT_TIMEOUT_S
