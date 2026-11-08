"""BQL-lite engine on a hand-made snapshot: point-in-time correctness vs hand-computed values, time-series and
cross-sectional functions, grouping, currency, sort / limit, and the compiled SQL shape."""

from __future__ import annotations

import statistics
from datetime import date, timedelta

import pytest

from app.bql import engine
from app.bql.grammar import BqlError
from tests.bql.conftest import AS_OF, DAYS, by_ticker, col, price, px_at

A, C_ = "SAAA00000001", "SCCC00000003"


def run(env, q, **kw):
    return engine.run(q, env["db"], use_cache=False, **kw)


def test_cross_section_snapshot(bql_env):
    r = run(bql_env, "get(px_last, roic, pe_fwd, sector) for(['AAA US', 'CCC GY', 'nope'])")
    assert r["mode"] == "cross_section" and r["as_of"] == AS_OF.isoformat()
    assert by_ticker(r, "px_last") == {"AAA": pytest.approx(price(A, len(DAYS) - 1)),
                                       "CCC.DE": pytest.approx(price(C_, len(DAYS) - 1))}
    assert by_ticker(r, "roic") == {"AAA": 0.26, "CCC.DE": 0.30}
    assert by_ticker(r, "sector") == {"AAA": "TECH", "CCC.DE": "HLTH"}
    assert any("unresolved securities: nope" in w for w in r["warnings"])
    assert any("pe_fwd is model-based" in w for w in r["warnings"])
    units = r["units"]
    assert units["px_last"] == "price" and units["roic"] == "pct" and units["sector"] == "text"


@pytest.mark.parametrize("as_of,roic,rev", [
    (date(2025, 4, 30), None, None),       # 2025-03 TTM known only 2025-05-01; 2024-12 row restated 2025-06-01
    (date(2025, 5, 15), 0.22, None),
    (date(2025, 6, 15), 0.22, None),       # the restated OLDER period (kd 06-01) never overwrites 2025-03
    (date(2025, 11, 15), 0.22, 1000.0),
    (date(2025, 12, 15), 0.22, 1010.0),    # same-period restatement visible from its knowledge date
    (date(2026, 3, 1), 0.26, 1100.0),      # TTM preferred over FY at the same period end
])
def test_point_in_time_knowledge_dates(bql_env, as_of, roic, rev):
    r = run(bql_env, f"get(roic, fa.revenue, px_last, f.quality) for(['AAA US']) with(as_of={as_of.isoformat()})")
    assert r["as_of"] == as_of.isoformat()
    row = dict(zip([c["name"] for c in r["columns"]], r["rows"][0], strict=True))
    assert row["roic"] == (pytest.approx(roic) if roic is not None else None)
    assert row["fa.revenue"] == (pytest.approx(rev) if rev is not None else None)
    assert row["px_last"] == pytest.approx(px_at(A, as_of))
    # factor scores: monthly as_of_date from 2025-10-31 (score 40 + 5k for AAA, k = month index)
    months = [date(2025, 10, 31), date(2025, 11, 30), date(2025, 12, 31), date(2026, 1, 31), date(2026, 2, 28)]
    known = [k for k, m in enumerate(months) if m <= as_of]
    assert row["f.quality"] == (pytest.approx(40 + 5 * known[-1]) if known else None)


def test_timeseries_pit_grid(bql_env):
    r = run(bql_env, "get(roic, px_last) for(['AAA US']) with(dates=range(2025-04-01, 2025-07-31), freq=M)")
    assert r["mode"] == "timeseries" and r["dates"]["freq"] == "M"
    assert col(r, "date") == ["2025-04-30", "2025-05-31", "2025-06-30", "2025-07-31"]
    assert col(r, "roic") == [None, pytest.approx(0.22), pytest.approx(0.22), pytest.approx(0.22)]
    assert col(r, "px_last") == [pytest.approx(px_at(A, date.fromisoformat(d))) for d in col(r, "date")]
    # the end date is always the last grid point
    r2 = run(bql_env, "get(px_last) for(['AAA US']) with(dates=range(2026-02-15, 2026-03-18), freq=W)")
    assert col(r2, "date")[-1] == "2026-03-18" and col(r2, "date")[0] == "2026-02-20"


def test_time_series_functions(bql_env):
    n = len(DAYS) - 1
    r = run(bql_env, "get(chg_pct(px, 21), chg_pct(px, 1M), chg(px, 5), lag(px, 3), avg(px, 5), std(px, 5), "
                     "min(px, 5), max(px, 5), zscore(px, 5), ytd(px), sum(px, 2), lag(px, 1M)) for(['DDD US'])")
    D = "SDDD00000004"
    row = dict(zip([c["name"] for c in r["columns"]], r["rows"][0], strict=True))
    last5 = [price(D, n - k) for k in range(5)]
    assert row["chg_pct(px,21)"] == pytest.approx(price(D, n) / price(D, n - 21) - 1)
    assert row["chg_pct(px,1M)"] == pytest.approx(price(D, n) / px_at(D, date(2026, 2, 28)) - 1)
    assert row["lag(px,1M)"] == pytest.approx(px_at(D, date(2026, 2, 28)))
    assert row["chg(px,5)"] == pytest.approx(price(D, n) - price(D, n - 5))
    assert row["lag(px,3)"] == pytest.approx(price(D, n - 3))
    assert row["avg(px,5)"] == pytest.approx(statistics.mean(last5))
    assert row["std(px,5)"] == pytest.approx(statistics.stdev(last5))
    assert row["min(px,5)"] == min(last5) and row["max(px,5)"] == max(last5)
    assert row["zscore(px,5)"] == pytest.approx((price(D, n) - statistics.mean(last5)) / statistics.stdev(last5))
    assert row["ytd(px)"] == pytest.approx(price(D, n) / px_at(D, date(2025, 12, 31)) - 1)
    assert row["sum(px,2)"] == pytest.approx(price(D, n) + price(D, n - 1))
    assert r["units"]["chg_pct(px,21)"] == "pct" and r["units"]["zscore(px,5)"] == "z"


def test_pct_rank_corr_beta_rolling_resample(bql_env):
    D = "SDDD00000004"
    n = len(DAYS) - 1
    r = run(bql_env, "get(pct_rank(px, 10), corr(px, px, 20), beta(px, px, 20), rolling(avg, px, 5), "
                     "rolling(std(px), 5), resample(px, M, how=max), beta(px, 'SPX Index', 60)) for(['DDD US'])")
    row = dict(zip([c["name"] for c in r["columns"]], r["rows"][0], strict=True))
    w = [price(D, n - k) for k in range(10)]
    assert row["pct_rank(px,10)"] == pytest.approx(sum(1 for x in w if x < price(D, n)) / 9)
    assert row["corr(px,px,20)"] == pytest.approx(1.0)
    assert row["beta(px,px,20)"] == pytest.approx(1.0)
    assert row["rolling(avg,px,5)"] == pytest.approx(statistics.mean(w[:5]))
    assert row["rolling(std(px),5)"] == pytest.approx(statistics.stdev(w[:5]))
    march = [price(D, i) for i, d in enumerate(DAYS) if d >= date(2026, 3, 1)]
    assert row["resample(px,M,how=max)"] == pytest.approx(max(march))
    assert "beta(px,'SPX Index',60)" in row  # bench returns are constant here: slope undefined -> null allowed


def test_cross_sectional_functions_and_where_order(bql_env):
    r = run(bql_env, "get(roic, cs_zscore(roic), rank(roic), cs_rank(roic), cs_demean(roic), cs_rank(roic, by=sector)) "
                     "for(members('full'))")
    vals = {"AAA": 0.26, "BBB": 0.10, "CCC.DE": 0.30, "DDD": 0.05}
    m, s = statistics.mean(vals.values()), statistics.stdev(vals.values())
    assert r["universe_size"] == 4
    z = by_ticker(r, "cs_zscore(roic)")
    for t, v in vals.items():
        assert z[t] == pytest.approx((v - m) / s)
    assert by_ticker(r, "rank(roic)") == {"CCC.DE": 1, "AAA": 2, "BBB": 3, "DDD": 4}
    assert by_ticker(r, "cs_rank(roic)") == {"DDD": 0, "BBB": pytest.approx(1 / 3), "AAA": pytest.approx(2 / 3), "CCC.DE": 1}
    assert by_ticker(r, "cs_rank(roic,by=sector)") == {"BBB": 0, "AAA": 1, "DDD": 0, "CCC.DE": 1}
    assert by_ticker(r, "cs_demean(roic)")["AAA"] == pytest.approx(0.26 - m)
    # where() filters the universe BEFORE the cross-sectional functions of get()
    r2 = run(bql_env, "get(cs_rank(roic)) for(members('full')) where(region == 'US')")
    assert by_ticker(r2, "cs_rank(roic)") == {"DDD": 0, "BBB": 0.5, "AAA": 1}
    # where() with its own cross-sectional function (over the whole for() universe)
    r3 = run(bql_env, "get(roic) for(members('full')) where(cs_rank(roic) >= 0.5)")
    assert set(by_ticker(r3, "roic")) == {"AAA", "CCC.DE"}


def test_group_agg_and_sort_limit(bql_env):
    r = run(bql_env, "get(roic, px_last) for(members('full')) group(sector).agg(mean, count) sort(sector desc)")
    names = [c["name"] for c in r["columns"]]
    assert names == ["sector", "n", "mean(roic)", "count(roic)", "mean(px_last)", "count(px_last)"]
    rows = {x[0]: x for x in r["rows"]}
    assert [x[0] for x in r["rows"]] == ["TECH", "HLTH"]
    assert rows["TECH"][2] == pytest.approx((0.26 + 0.10) / 2) and rows["TECH"][1] == 2
    assert rows["HLTH"][2] == pytest.approx((0.30 + 0.05) / 2)
    r2 = run(bql_env, "get(roic) for(members('full')) sort(roic desc) limit(2)")
    assert col(r2, "ticker") == ["CCC.DE", "AAA"] and not r2["truncated"]
    r3 = run(bql_env, "get(roic) for(members('full'))", max_rows=3)
    assert r3["n_rows"] == 3 and r3["truncated"] and any("truncated" in w for w in r3["warnings"])


def test_timeseries_top_n_by_last_date(bql_env):
    r = run(bql_env, "get(chg_pct(px, 21) as m) for(members('full')) with(dates=range(-1M, 0D), freq=W) "
                     "sort(m desc) limit(2)")
    n = len(DAYS) - 1
    last = {sid: price(sid, n) / price(sid, n - 21) - 1 for sid in ("SAAA00000001", "SBBB00000002", "SCCC00000003",
                                                                   "SDDD00000004")}
    top = [k for k, _ in sorted(last.items(), key=lambda kv: -kv[1])[:2]]
    ids = list(dict.fromkeys(col(r, "security_id")))
    assert ids == top
    assert len(r["rows"]) == 2 * r["dates"]["n"]


def test_currency_conversion(bql_env):
    n = len(DAYS) - 1
    r = run(bql_env, "get(px_last, mkt_cap_usd) for(['CCC GY', 'AAA US']) with(currency=USD)")
    assert by_ticker(r, "px_last")["CCC.DE"] == pytest.approx(price(C_, n) * 1.10)
    assert by_ticker(r, "px_last")["AAA"] == pytest.approx(price(A, n))
    r2 = run(bql_env, "get(px_last, mkt_cap_usd) for(['AAA US']) with(currency=EUR)")
    assert by_ticker(r2, "px_last")["AAA"] == pytest.approx(price(A, n) / 1.10)
    assert by_ticker(r2, "mkt_cap_usd")["AAA"] == pytest.approx(3e11 / 1.10)
    assert {c["name"]: c["currency"] for c in r2["columns"] if c["role"] == "value"} == {"px_last": "EUR",
                                                                                         "mkt_cap_usd": "EUR"}
    r3 = run(bql_env, "get(px) for(['CCC GY']) with(dates=range(-5D, 0D), currency=USD)")
    assert col(r3, "px")[-1] == pytest.approx(price(C_, n) * 1.10)
    # no lake FX history in the fixture: the latest rate is applied (labelled)
    r4 = run(bql_env, "get(px) for(['AAA US']) with(dates=range(-5D, 0D), currency=EUR)")
    assert col(r4, "px")[-1] == pytest.approx(price(A, n) / 1.10)
    assert any("latest rate" in w for w in r4["warnings"])


def test_fill_none_vs_prev(bql_env):
    q = "get(f.quality) for(['AAA US']) with(dates=range(2026-01-26, 2026-02-03), freq=D, fill={})"
    prev = run(bql_env, q.format("prev"))
    none = run(bql_env, q.format("none"))
    d = col(prev, "date")
    assert d == ["2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30", "2026-02-02", "2026-02-03"]
    assert col(prev, "f.quality") == [50.0] * 5 + [55.0, 55.0]      # 2025-12-31 (k=2), then 2026-01-31 (k=3)
    assert col(none, "f.quality") == [50.0] + [None] * 4 + [55.0, None]  # only a print since the previous grid date


def test_snapshot_only_fields_rejected_for_history(bql_env):
    with pytest.raises(BqlError, match="no point-in-time history"):
        run(bql_env, "get(pe_fwd) for(['AAA US']) with(as_of=2025-06-30)")
    with pytest.raises(BqlError, match="no point-in-time history"):
        run(bql_env, "get(pe_fwd) for(['AAA US']) with(dates=range(-1M, 0D))")
    with pytest.raises(BqlError, match="snapshot"):
        run(bql_env, "get(avg(pe_fwd, 3)) for(['AAA US'])")


def test_universe_algebra_and_members(bql_env):
    r = run(bql_env, "get(ticker) for(members('S&P 500') & sector('Tech'))")
    assert sorted(col(r, "ticker")) == ["AAA", "BBB"]
    assert any("approximation" in w for w in r["warnings"])
    r = run(bql_env, "get(ticker) for(members('full') - region('US'))")
    assert col(r, "ticker") == ["CCC.DE"]
    r = run(bql_env, "get(ticker) for(sector('Health') | ['AAA US'])")
    assert sorted(col(r, "ticker")) == ["AAA", "CCC.DE", "DDD"]
    r = run(bql_env, "get(ticker) for(['SPX Index', 'IDX:^GSPC'])")
    assert col(r, "ticker") == ["^GSPC"]
    with pytest.raises(BqlError, match="unknown universe"):
        run(bql_env, "get(ticker) for(members('Nikkei 225'))")
    with pytest.raises(BqlError, match="unknown sector"):
        run(bql_env, "get(ticker) for(sector('Gadgets'))")


def test_cache_hits_and_explain_shape(bql_env):
    db = bql_env["db"]
    engine.cache.clear()
    q = "get(px_last) for(['AAA US']) where(name != 'zz''zz')"
    a = engine.run(q, db)
    b = engine.run("GET(px_last)  FOR(['AAA US'])  WHERE(name <> 'zz''zz')", db)
    assert not a["cached"] and b["cached"] and a["rows"] == b["rows"]
    db.snap.run_id = "TESTRUN2"  # a new snapshot never reuses cached results
    try:
        assert not engine.run(q, db)["cached"]
    finally:
        db.snap.run_id = "TESTRUN1"
    ex = engine.explain(q, db)
    assert "zz'zz" not in ex["sql"] and "AAA" not in ex["sql"] and "$p" in ex["sql"]
    assert "AAA" not in ex["universe_sql"] and "$u1" in ex["universe_sql"]
    assert all(set(p) == {"name", "type"} for p in ex["params"])
    assert ex["ast"]["get"][0]["expr"] == {"name": "px_last"}
    assert ex["estimates"]["needs_daily_panel"] is False
    ex2 = engine.explain("get(chg_pct(px, 1M)) for(['AAA US']) with(dates=range(-1Y, 0D), freq=M)", db)
    assert ex2["estimates"]["needs_daily_panel"] and ex2["estimates"]["daily_from"] < (AS_OF - timedelta(days=395)).isoformat()


def test_exports(bql_env):
    r = run(bql_env, "get(px_last, roic) for(['AAA US', 'BBB US'])")
    csv_text = engine.to_csv(r)
    assert csv_text.splitlines()[0] == "security_id,ticker,name,px_last,roic"
    assert len(csv_text.splitlines()) == 3
    js = engine.to_json(r)
    assert '"roic": 0.26' in js
    if engine.xlsx_available():
        b = engine.to_xlsx(r)
        assert b[:2] == b"PK"


def test_panel_helper(bql_env):
    grid = [date(2025, 12, 31), date(2026, 1, 30), date(2026, 3, 31)]
    rows, warns = engine.panel("cs_rank(roic)", bql_env["db"], ["SAAA00000001", "SBBB00000002", "SDDD00000004"],
                               dates=grid)
    got = {(r[0], r[1]): r[2] for r in rows}
    # 2025-12-31: only AAA known (0.22) -> rank 0 of 1 is undefined (single name) -> 0.0 percent_rank
    assert got[("SAAA00000001", date(2026, 3, 31))] == 1.0 and got[("SDDD00000004", date(2026, 3, 31))] == 0.0
    assert ("SBBB00000002", date(2025, 12, 31)) not in got  # BBB's first filing is 2026-02-01 (PIT)


def test_key_fields_not_duplicated_and_group_sort_by_n(bql_env):
    """Completeness audit 2026-10-27: get(name, ...) produced a second `name` column; group().agg() could not sort(n)."""
    from app.bql import engine

    r = engine.run("get(name, ticker, roic) for(['AAA US', 'BBB US'])", bql_env["db"], use_cache=False)
    names = [c["name"] for c in r["columns"]]
    assert names.count("name") == 1 and names.count("ticker") == 1 and names[-1] == "roic"
    assert all(len(row) == len(names) for row in r["rows"])
    r2 = engine.run("get(name) for(['AAA US'])", bql_env["db"], use_cache=False)
    assert [c["name"] for c in r2["columns"]] == ["security_id", "ticker", "name"] and r2["rows"][0][2] == "AAA Corp"
    aliased = engine.run("get(name as nm) for(['AAA US'])", bql_env["db"], use_cache=False)
    assert [c["name"] for c in aliased["columns"]][-1] == "nm"
    g = engine.run("get(roic) for(members('full')) group(region).agg(median) sort(n desc)", bql_env["db"], use_cache=False)
    cols = [c["name"] for c in g["columns"]]
    ni = cols.index("n")
    ns = [row[ni] for row in g["rows"]]
    assert ns == sorted(ns, reverse=True) and len(ns) >= 2
    g2 = engine.run("get(roic) for(members('full')) group(region).agg(median) sort(n asc)", bql_env["db"], use_cache=False)
    assert [row[ni] for row in g2["rows"]] == sorted(ns)
