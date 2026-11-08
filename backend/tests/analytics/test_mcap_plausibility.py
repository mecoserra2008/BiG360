"""Coverage v3 A6: implausible market caps. PIT share counts (regulator precedence, Yahoo fetch-date split basis,
1000x XBRL share-scale guard), the unknown-ADR-ratio gate in valuation, and the market-cap plausibility DQ checks.
Fixture data only."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.analytics import mcap_plausibility as MP
from app.analytics import shares_pit as SP
from app.core.storage import write_table

NOW = datetime(2026, 11, 6, tzinfo=UTC)


def _item(iss, item, kind, pe, kd, value, src="sec_edgar", concept="x", written=date(2026, 11, 1)):
    return {"issuer_id": iss, "item_id": item, "period_kind": kind, "period_end": pe, "knowledge_date": kd,
            "value": value, "source_id": src, "source_concept": concept, "written": written}


def _items(rows):
    return pl.DataFrame(rows, schema={"issuer_id": pl.Utf8, "item_id": pl.Utf8, "period_kind": pl.Utf8,
                                      "period_end": pl.Date, "knowledge_date": pl.Date, "value": pl.Float64,
                                      "source_id": pl.Utf8, "source_concept": pl.Utf8, "written": pl.Date})


# ------------------------------------------------------------------------------------------------ shares_pit (pure)


def test_candidates_prefer_regulator_and_reported_counts_and_skip_info_rows():
    d, k = date(2026, 6, 30), date(2026, 8, 10)
    it = _items([_item("I", "shares_outstanding", "INSTANT", d, k, 100.0, "yfinance", "Ordinary Shares Number"),
                 _item("I", "shares_outstanding", "INSTANT", d, k, 90.0, "edinet"),
                 _item("I", "shares_diluted_weighted", "Q", d, k, 95.0, "edinet"),
                 _item("I", "shares_outstanding", "INSTANT", date(2026, 10, 11), date(2026, 10, 11), 7.0, "yfinance",
                       "info:sharesOutstanding")])
    c = SP.build_candidates(it)
    assert c.height == 1
    r = c.row(0, named=True)
    assert (r["shares"], r["shares_w"], r["src"]) == (90.0, 95.0, "regulator")


def test_regulator_precedence_is_point_in_time():
    c = pl.DataFrame({"issuer_id": ["I"] * 3, "period_end": [date(2025, 6, 30), date(2026, 6, 26), date(2026, 6, 30)],
                      "knowledge_date": [date(2025, 8, 29), date(2026, 6, 26), date(2026, 8, 29)],
                      "src": ["yahoo", "regulator", "yahoo"], "shares": [2.85e10, 1.93e9, 2.85e10]})
    out = SP.regulator_precedence(c)
    # the 2025 Yahoo row stays (no regulator count known on 2025-08-29); the 2026 one is superseded
    assert out.sort("knowledge_date").get_column("src").to_list() == ["yahoo", "regulator"]
    stale = c.with_columns(pl.when(pl.col("src") == "regulator").then(pl.lit(date(2024, 6, 21)))
                           .otherwise(pl.col("period_end")).alias("period_end"))
    assert SP.regulator_precedence(stale).filter(pl.col("src") == "yahoo").height == 2   # regulator too old


def test_yahoo_basis_is_the_fetch_date():
    c = pl.DataFrame({"issuer_id": ["I", "I"], "period_end": [date(2026, 6, 30), date(2026, 6, 30)],
                      "knowledge_date": [date(2026, 8, 29), date(2026, 7, 1)], "src": ["yahoo", "regulator"],
                      "source_concept": ["Ordinary Shares Number", "x"], "written": [date(2026, 11, 3)] * 2})
    f = pl.DataFrame({"issuer_id": ["I"], "period_end": [date(2026, 6, 30)], "source_concept": ["Ordinary Shares Number"],
                      "fetched": [date(2026, 10, 12)]})
    out = SP.attach_basis(c, f).sort("src")
    assert out.get_column("basis_date").to_list() == [date(2026, 6, 30), date(2026, 10, 12)]
    assert SP.attach_basis(c, None).filter(pl.col("src") == "yahoo").item(0, "basis_date") == date(2026, 11, 3)


def test_unit_guard_eps_and_history():
    q = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
    kd = [d + timedelta(days=40) for d in q]
    # RPAY-like: every count x1000, EPS fine; TBLA-like: clean history, then counts AND EPS x1000
    rows = []
    for d, k in zip(q, kd, strict=True):
        rows += [_item("R", "shares_outstanding", "INSTANT", d, k, 8.9e10), _item("R", "eps_basic", "Q", d, k, -0.12),
                 _item("R", "net_income", "Q", d, k, -0.12 * 8.9e7)]
        big = d >= date(2025, 9, 30)
        rows += [_item("T", "shares_outstanding", "INSTANT", d, k, 3.1e8 * (1000 if big else 1)),
                 _item("T", "eps_basic", "Q", d, k, 0.07 * (1000 if big else 1)),
                 _item("T", "net_income", "Q", d, k, 0.07 * 3.1e8)]
        rows += [_item("A", "shares_outstanding", "INSTANT", d, k, 9.9e10), _item("A", "eps_basic", "Q", d, k, 0.5),
                 _item("A", "net_income", "Q", d, k, 0.5 * 9.9e7)]               # ADR issuer: never touched
    it = _items(rows)
    c = SP.build_candidates(it.filter(pl.col("item_id").str.starts_with("shares")))
    # the market confirms both repairs: RPAY-like 1.07e6 a day on 8.9e10 shares = 1.2e-5, TBLA-like 1.5e6 on 3.1e11
    vol = pl.DataFrame({"issuer_id": ["R"] * 4 + ["T"] * 4 + ["A"] * 4, "date": q * 3,
                        "med_volume": [1.07e6] * 4 + [1.5e6] * 4 + [1e6] * 4})
    g = SP.apply_unit_guard(c, SP.eps_implied_shares(it), skip={"A"}, volume=vol).sort("issuer_id", "period_end")
    r, t, a = (g.filter(pl.col("issuer_id") == x) for x in ("R", "T", "A"))
    assert r.get_column("shares").to_list() == pytest.approx([8.9e7] * 4)
    assert r.get_column("unit_fix").to_list() == [-3] * 4
    assert t.get_column("shares").to_list() == pytest.approx([3.1e8] * 4)
    assert t.get_column("unit_fix").to_list() == [0, 0, -3, -3]
    assert a.get_column("shares").to_list() == [9.9e10] * 4 and a.get_column("unit_fix").to_list() == [0] * 4
    # no market data: every proposal is vetoed (count kept, proposal recorded)
    n = SP.apply_unit_guard(c, SP.eps_implied_shares(it), skip={"A"}).sort("issuer_id", "period_end")
    assert n.get_column("unit_fix").to_list() == [0] * 12
    assert n.filter(pl.col("issuer_id") == "R").get_column("unit_suspect").to_list() == [-3] * 4
    # T: the vetoed Q3 count stays the history reference, so Q4 (same level) proposes nothing; share_events still
    # reads the volume for T because of its first proposal
    assert n.filter(pl.col("issuer_id") == "T").get_column("unit_suspect").to_list() == [0, 0, -3, 0]
    assert n.filter(pl.col("issuer_id") == "R").get_column("shares").to_list() == [8.9e10] * 4


def test_unit_guard_volume_vetoes_an_implausible_repair():
    # KFFB-like: net income tagged x1000 implies a count 1000x larger (+3); 1,100 shares a day on 3e6 shares is a
    # normal 3.7e-4 turnover, and 3.7e-7 after the repair: vetoed
    d, k = date(2026, 6, 30), date(2026, 8, 10)
    it = _items([_item("K", "shares_outstanding", "INSTANT", d, k, 3e6), _item("K", "eps_basic", "Q", d, k, 0.2),
                 _item("K", "net_income", "Q", d, k, 0.2 * 3e9)])
    c = SP.build_candidates(it.filter(pl.col("item_id").str.starts_with("shares")))
    vol = pl.DataFrame({"issuer_id": ["K"], "date": [date(2026, 6, 29)], "med_volume": [1100.0]})
    g = SP.apply_unit_guard(c, SP.eps_implied_shares(it), volume=vol)
    assert g.row(0, named=True)["shares"] == 3e6 and g.item(0, "unit_fix") == 0 and g.item(0, "unit_suspect") == 3
    assert SP._market_confirms(1.2e-5, -3) and not SP._market_confirms(3.7e-4, 3) and not SP._market_confirms(None, -3)


def test_formula_eps_never_checks_the_count_it_was_computed_from():
    d, k = date(2026, 6, 30), date(2026, 8, 10)
    base = [_item("G", "shares_outstanding", "INSTANT", d, k, 4e10), _item("G", "net_income", "Q", d, k, 2e6)]
    eps = _item("G", "eps_basic", "Q", d, k, 0.05)
    it = _items(base + [eps]).with_columns(pl.lit("reported").alias("derivation"))
    assert SP.eps_implied_shares(it).height == 1
    der = it.with_columns(pl.when(pl.col("item_id") == "eps_basic").then(pl.lit("derived_formula"))
                          .otherwise(pl.col("derivation")).alias("derivation"))
    assert SP.eps_implied_shares(der).is_empty()            # GBR / LFST: net income / our weighted count


def test_unit_guard_history_needs_a_recent_count_of_the_same_kind():
    # BRK-B-like: a 2015 count 1000x smaller is too old to chain from; a weighted count tagged in thousands is not
    # compared with an outstanding one (GERN-like)
    it = _items([_item("B", "shares_outstanding", "INSTANT", date(2015, 12, 31), date(2016, 2, 20), 2.14e6),
                 _item("B", "shares_outstanding", "INSTANT", date(2026, 6, 30), date(2026, 8, 3), 2.14e9),
                 _item("W", "shares_basic_weighted", "Q", date(2026, 3, 31), date(2026, 5, 1), 2.0e5),
                 _item("W", "shares_outstanding", "INSTANT", date(2026, 6, 30), date(2026, 8, 1), 2.0e8)])
    c = SP.build_candidates(it)
    g = SP.apply_unit_guard(c, SP.eps_implied_shares(it))
    assert g.get_column("unit_suspect").to_list() == [0, 0, 0, 0] and g.get_column("unit_fix").to_list() == [0] * 4
    # the same jump within HISTORY_MAX_DAYS and of the same kind is proposed
    it2 = _items([_item("B", "shares_outstanding", "INSTANT", date(2026, 3, 31), date(2026, 5, 2), 2.14e6),
                  _item("B", "shares_outstanding", "INSTANT", date(2026, 6, 30), date(2026, 8, 3), 2.14e9)])
    g2 = SP.apply_unit_guard(SP.build_candidates(it2), SP.eps_implied_shares(it2)).sort("period_end")
    assert g2.get_column("unit_suspect").to_list() == [0, -3]


def test_split_restated_basis_reads_the_closer_split_basis():
    sf = pl.DataFrame({"security_id": ["M"] * 4 + ["E"] * 4,
                       "date": [date(2026, 3, 31), date(2026, 6, 30), date(2026, 7, 1), date(2026, 8, 1)] * 2,
                       "split_factor": [0.5, 0.5, 1.0, 1.0] * 2})               # 2-for-1 on 2026-07-01
    pe1, kd1, pe2, kd2 = date(2026, 3, 31), date(2026, 4, 30), date(2026, 6, 30), date(2026, 7, 25)
    ev = pl.DataFrame({"security_id": ["M", "M", "E", "E", "Y"], "period_end": [pe1, pe2, pe1, pe2, pe2],
                       "knowledge_date": [kd1, kd2, kd1, kd2, kd2],
                       # MLI: the Q2 10-Q count is already post-split; EDINET-like: the actual period-end count
                       "shares": [1.1e8, 2.21e8, 1.1e8, 1.105e8, 5e7], "basis_date": [pe1, pe2, pe1, pe2, pe2],
                       "src": ["regulator"] * 4 + ["yahoo"]})
    out = SP.split_restated_basis(ev, sf)
    m2 = out.filter((pl.col("security_id") == "M") & (pl.col("period_end") == pe2)).row(0, named=True)
    e2 = out.filter((pl.col("security_id") == "E") & (pl.col("period_end") == pe2)).row(0, named=True)
    assert (m2["basis_rule"], m2["basis_date"], m2["sf_basis"]) == ("restated", kd2, 1.0)
    assert (e2["basis_rule"], e2["basis_date"], e2["sf_basis"]) == ("period", pe2, 0.5)
    assert out.filter(pl.col("period_end") == pe1).get_column("basis_rule").to_list() == [None, None]  # nothing to compare
    assert out.filter(pl.col("security_id") == "Y").item(0, "basis_rule") is None                       # Yahoo: fetch date
    assert out.height == ev.height and out.get_column("security_id").to_list() == ev.get_column("security_id").to_list()


def test_unit_guard_keeps_a_reverse_split():
    d = [date(2025, 3, 31), date(2025, 6, 30)]
    it = _items([_item("P", "shares_outstanding", "INSTANT", d[0], d[0], 5e9),
                 _item("P", "shares_outstanding", "INSTANT", d[1], d[1], 5e6)])       # 1-for-1000 reverse split
    c = SP.build_candidates(it)
    g = SP.apply_unit_guard(c, SP.eps_implied_shares(it))
    assert g.sort("period_end").get_column("shares").to_list() == [5e9, 5e6]


# ------------------------------------------------------------------------------------------------ valuation (lake)


def _canonical_rows(iss, rows):
    out = []
    for item, kind, pe, kd, v, src, concept in rows:
        out.append({"issuer_id": iss, "item_id": item, "fiscal_year": pe.year, "fiscal_period": "FY" if kind == "FY" else "Q2",
                    "period_kind": kind, "period_start": None, "period_end": pe, "value": v, "currency": None,
                    "value_usd": None, "fx_rate": None, "fx_method": None, "fx_source": None, "gaap": "other",
                    "derivation": "reported", "source_concept": concept, "source_id": src, "filing_id": None,
                    "knowledge_date": kd, "knowledge_date_estimated": src == "yfinance", "is_restatement": False,
                    "dq_score": 1.0, "ingested_at": NOW, "run_id": None})
    return out


def _sec(sid, iss, tk, typ="common", adr=None):
    return {"security_id": sid, "issuer_id": iss, "security_type": typ, "primary_listing_id": "L" + sid,
            "display_ticker": tk, "name": tk, "trading_currency": "USD", "is_primary_security": True,
            "is_active": True, "updated_at": NOW, "adr_ratio": adr}


def _px(sid, days, close, sf, vol=1e6):
    return pl.DataFrame({"security_id": sid, "listing_id": "L" + sid, "date": days, "region": "US",
                         "year": [d.year for d in days], "close": close, "adj_factor": sf, "split_factor": sf,
                         "adj_close": [c * f for c, f in zip(close, sf, strict=True)], "close_usd": close,
                         "volume": vol, "currency": "USD", "usd_per_unit": 1.0, "source_id": "yfinance",
                         "ingested_at": NOW})


def test_daily_market_cap_split_basis_precedence_and_adr_gate(tmp_data):
    from app.analytics.valuation import daily_market_cap

    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(100)]
    split = date(2026, 9, 29)                                    # 15-for-1 (Tokio Marine 2026)
    sf = [1 / 15 if d < split else 1.0 for d in days]
    close = [7700.0 if d < split else 520.0 for d in days]
    write_table(pl.DataFrame([_sec("S1", "I1", "8766.T"), _sec("S2", "I2", "YH.T"), _sec("S3", "I3", "ADRX", "adr"),
                              _sec("S4", "I4", "ADR1", "adr")]), "securities_master")
    px = pl.concat([_px("S1", days, close, sf), _px("S2", days, close, sf), _px("S3", days, [10.0] * 100, [1.0] * 100, 500.0),
                    _px("S4", days, [10.0] * 100, [1.0] * 100, 4e6)])
    write_table(px, "prices_daily")
    pe, kd = date(2026, 6, 30), date(2026, 8, 29)
    rows = (_canonical_rows("I1", [("shares_outstanding", "INSTANT", date(2026, 6, 26), date(2026, 6, 26), 1.934e9, "edinet", "jpcrp"),
                                   ("shares_outstanding", "INSTANT", pe, kd, 2.85e10, "yfinance", "Ordinary Shares Number")])
            # Yahoo only: the count was fetched after the split (post-split basis)
            + _canonical_rows("I2", [("shares_outstanding", "INSTANT", pe, kd, 2.85e10, "yfinance", "Ordinary Shares Number")])
            + _canonical_rows("I3", [("shares_outstanding", "INSTANT", pe, kd, 1.5e11, "sec_edgar", "dei")])
            + _canonical_rows("I4", [("shares_outstanding", "INSTANT", pe, kd, 4e9, "sec_edgar", "dei")]))
    write_table(pl.DataFrame(rows), "fundamentals_canonical")
    write_table(pl.DataFrame([{"source_id": "yfinance_fund", "fiscal_year": 2026, "source_entity_id": "YH.T",
                               "issuer_id": "I2", "filing_id": "yfinance_fund:na", "form_type": None, "taxonomy": "yfinance",
                               "concept": "Ordinary Shares Number", "label": None, "period_type": "instant",
                               "period_start": None, "period_end": pe, "fiscal_period": None, "value": 2.85e10,
                               "unit": "shares", "decimals": None, "dimensions_hash": "", "dimensions_json": None,
                               "consolidated": True, "knowledge_date": kd, "knowledge_date_estimated": True,
                               "ingested_at": datetime(2026, 10, 12, tzinfo=UTC), "frame": None, "is_latest": True,
                               "is_forecast": None}]), "fundamentals_raw")
    mc = daily_market_cap(pl.DataFrame({"security_id": ["S1", "S2", "S3", "S4"], "issuer_id": ["I1", "I2", "I3", "I4"]}),
                          date(2026, 9, 1), days[-1])

    def at(sid, d):
        return mc.filter((pl.col("security_id") == sid) & (pl.col("date") == d)).row(0, named=True)

    post, pre = days[-1], date(2026, 9, 15)
    # regulator count (EDINET, pre-split basis) wins and is split-adjusted once: 1.934e9 x 15
    assert at("S1", post)["shares_out"] == pytest.approx(1.934e9 * 15)
    assert at("S1", pre)["shares_out"] == pytest.approx(1.934e9)
    # Yahoo-only: the post-split count is NOT multiplied again after the split, and is un-split before it
    assert at("S2", post)["shares_out"] == pytest.approx(2.85e10)
    assert at("S2", pre)["shares_out"] == pytest.approx(2.85e10 / 15)
    # ADR without a ratio whose volume contradicts 1:1 (500 ADS a day on 1.5e11 shares): withheld; a liquid one
    # (4e6 a day on 4e9 shares = 1e-3) kept
    assert at("S3", post)["market_cap_usd"] is None
    assert at("S4", post)["market_cap_usd"] == pytest.approx(4e10)


def _yahoo_raw(iss, tk, pe, kd, v, fetched):
    return {"source_id": "yfinance_fund", "fiscal_year": pe.year, "source_entity_id": tk, "issuer_id": iss,
            "filing_id": "yfinance_fund:na", "form_type": None, "taxonomy": "yfinance", "concept": "Ordinary Shares Number",
            "label": None, "period_type": "instant", "period_start": None, "period_end": pe, "fiscal_period": None,
            "value": v, "unit": "shares", "decimals": None, "dimensions_hash": "", "dimensions_json": None,
            "consolidated": True, "knowledge_date": kd, "knowledge_date_estimated": True, "ingested_at": fetched,
            "frame": None, "is_latest": True, "is_forecast": None}


def test_daily_market_cap_window_end_does_not_change_the_split_basis(tmp_data):
    """A6 review: with `end` before a Yahoo count's fetch date the split factor at the fetch date was missing (15x)."""
    from app.analytics.valuation import daily_market_cap

    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(100)]
    split = date(2026, 9, 29)
    sf = [1 / 15 if d < split else 1.0 for d in days]
    write_table(pl.DataFrame([_sec("S2", "I2", "YH.T")]), "securities_master")
    write_table(_px("S2", days, [7700.0 if d < split else 520.0 for d in days], sf), "prices_daily")
    pe, kd = date(2026, 6, 30), date(2026, 8, 29)
    write_table(pl.DataFrame(_canonical_rows("I2", [("shares_outstanding", "INSTANT", pe, kd, 2.85e10, "yfinance",
                                                     "Ordinary Shares Number")])), "fundamentals_canonical")
    write_table(pl.DataFrame([_yahoo_raw("I2", "YH.T", pe, kd, 2.85e10, datetime(2026, 10, 12, tzinfo=UTC))]),
                "fundamentals_raw")
    sec = pl.DataFrame({"security_id": ["S2"], "issuer_id": ["I2"]})
    full = daily_market_cap(sec, date(2026, 9, 1), days[-1])
    capped = daily_market_cap(sec, date(2026, 9, 1), date(2026, 9, 20))
    d = date(2026, 9, 15)
    a = full.filter(pl.col("date") == d).item(0, "shares_out")
    assert capped.filter(pl.col("date") == d).item(0, "shares_out") == pytest.approx(a) == pytest.approx(2.85e10 / 15)


def test_daily_market_cap_does_not_split_a_restated_count_twice(tmp_data):
    """MLI-like: 2-for-1 on 2026-07-01, the Q2 10-Q (filed 07-25) already reports the post-split count."""
    from app.analytics.valuation import daily_market_cap

    days = [date(2026, 3, 1) + timedelta(days=i) for i in range(190)]
    split = date(2026, 7, 1)
    sf = [0.5 if d < split else 1.0 for d in days]
    write_table(pl.DataFrame([_sec("SM", "IM", "MLI")]), "securities_master")
    write_table(_px("SM", days, [100.0 if d < split else 50.0 for d in days], sf, vol=1e6), "prices_daily")
    write_table(pl.DataFrame(_canonical_rows("IM", [
        ("shares_outstanding", "INSTANT", date(2026, 3, 31), date(2026, 4, 30), 1.1e8, "sec_edgar", "dei"),
        ("shares_diluted_weighted", "Q", date(2026, 3, 31), date(2026, 4, 30), 1.1e8, "sec_edgar", "us-gaap"),
        ("shares_outstanding", "INSTANT", date(2026, 6, 30), date(2026, 7, 25), 2.21e8, "sec_edgar", "dei"),
        ("shares_diluted_weighted", "Q", date(2026, 6, 30), date(2026, 7, 25), 2.2e8, "sec_edgar", "us-gaap")])),
        "fundamentals_canonical")
    mc = daily_market_cap(pl.DataFrame({"security_id": ["SM"], "issuer_id": ["IM"]}), date(2026, 5, 1), days[-1])

    def sh(d):
        return mc.filter(pl.col("date") == d).item(0, "shares_out")

    assert sh(date(2026, 6, 15)) == pytest.approx(1.1e8)                   # pre-split, Q1 count
    assert sh(date(2026, 7, 10)) == pytest.approx(2.2e8)                   # Q1 count split-adjusted once
    assert sh(date(2026, 8, 15)) == pytest.approx(2.21e8)                  # restated Q2 count: not x2 again
    assert mc.filter(pl.col("date") == date(2026, 8, 15)).item(0, "market_cap_usd") == pytest.approx(2.21e8 * 50)


def test_unit_guard_skip_dr_and_multi_class_issuers(tmp_data):
    rows = [_sec("A1", "IA", "TLK", "adr"), _sec("A2", "IA", "TLKM.JK"),
            _sec("B1", "IB", "BRK-A"), _sec("B2", "IB", "BRK-B"),
            _sec("C1", "IC", "RPAY"),
            _sec("D1", "ID", "OLD"), {**_sec("D2", "ID", "NEW"), "is_active": False}]
    write_table(pl.DataFrame(rows), "securities_master")
    assert SP.unit_guard_skip(["IA", "IB", "IC", "ID"]) == {"IA", "IB"}
    assert SP.unit_guard_skip([]) == set()


def test_share_events_applies_a_volume_confirmed_repair_only(tmp_data):
    """RPAY-like through the lake: x1000 count, EPS fine. Applied with the primary line's volume; kept without."""
    q = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31)]
    rows = []
    for d in q:
        k = d + timedelta(days=40)
        rows += [("shares_outstanding", "INSTANT", d, k, 8.9e10, "sec_edgar", "dei"),
                 ("eps_basic", "Q", d, k, -0.12, "sec_edgar", "us-gaap:EarningsPerShareBasic"),
                 ("net_income", "Q", d, k, -0.12 * 8.9e7, "sec_edgar", "us-gaap:NetIncomeLoss")]
    write_table(pl.DataFrame(_canonical_rows("IR", rows)), "fundamentals_canonical")
    write_table(pl.DataFrame([_sec("SR", "IR", "RPAY")]), "securities_master")
    no_px = SP.share_events(["IR"])
    assert no_px.get_column("unit_fix").to_list() == [0] * 4 and no_px.get_column("shares").max() == 8.9e10
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(400)]
    write_table(_px("SR", days, [4.5] * len(days), [1.0] * len(days), vol=1.07e6), "prices_daily")
    ev = SP.share_events(["IR"]).sort("period_end")
    assert ev.get_column("unit_fix").to_list() == [-3] * 4
    assert ev.get_column("shares").to_list() == pytest.approx([8.9e7] * 4)


# ------------------------------------------------------------------------------------------------ DQ checks (pure)


def test_security_flags():
    val = pl.DataFrame({"security_id": ["A", "B", "C", "D"], "issuer_id": ["IA", "IB", "IC", "ID"],
                        "market_cap_usd": [1e9, 3.8e11, 2e9, 1e12], "close_usd": [10.0, 4.56, 10.0, 10.0],
                        "shares_out": [1e8, 8.33e10, 1e8, 1e11]})
    sec = pl.DataFrame({"security_id": ["A", "B", "C", "D"], "security_type": ["common", "common", "common", "adr"],
                        "adr_ratio": [None, None, None, None], "display_ticker": ["A", "RPAY", "C", "TLK"]})
    vol = pl.DataFrame({"security_id": ["A", "B", "C", "D"], "med_volume": [1e6, 3.8e5, 1e6, 8.4e5],
                        "traded_sessions": [60, 60, 60, 60]})
    f = MP.security_flags(val, sec, vol).sort("security_id")
    assert f.get_column("flagged").to_list() == [False, True, True, True]
    assert f.filter(pl.col("security_id") == "C").item(0, "inconsistent")          # 2e9 != 10 x 1e8
    assert f.filter(pl.col("security_id") == "B").item(0, "low_turnover")          # large cap at 4.6e-6
    assert f.filter(pl.col("security_id") == "D").item(0, "adr_ratio_missing")
    # a count cut too far: BRK-B on 2.14e6 shares trades 2x its shares a day (high_turnover); thin trading -> no flag
    v2 = pl.DataFrame({"security_id": ["E", "F"], "issuer_id": ["IE", "IF"], "market_cap_usd": [1.07e9, 1.07e9],
                       "close_usd": [500.0, 500.0], "shares_out": [2.14e6, 2.14e6]})
    s2 = pl.DataFrame({"security_id": ["E", "F"], "security_type": ["common", "common"], "adr_ratio": [None, None],
                       "display_ticker": ["BRK-B", "THIN"]})
    f2 = MP.security_flags(v2, s2, pl.DataFrame({"security_id": ["E", "F"], "med_volume": [4.3e6, 4.3e6],
                                                 "traded_sessions": [60, 5]})).sort("security_id")
    assert f2.get_column("high_turnover").to_list() == [True, False]
    assert f2.get_column("flagged").to_list() == [True, False]


def test_exchange_flags():
    val = pl.DataFrame({"security_id": ["J1", "J2", "C1", "R1", "R2"], "issuer_id": ["IJ1", "IJ2", "IC", "IR", "IR"],
                        "market_cap_usd": [1.6e12, 3e11, 3.5e10, 3.9e11, 3.9e11]})
    sec = pl.DataFrame({"security_id": ["J1", "J2", "C1", "R1", "R2"],
                        "display_ticker": ["8766.T", "7203.T", "CEZ.PR", "RO.SW", "ROG.SW"]})
    country = pl.DataFrame({"security_id": ["J1", "J2", "C1", "R1", "R2"], "exchange_country": ["JP", "JP", "CZ", "CH", "CH"]})
    by, big = MP.exchange_flags(val, sec, country, totals={"JP": 7.0, "CZ": 0.05, "CH": 2.1})
    assert big.get_column("display_ticker").to_list() == ["8766.T"]       # CEZ 70 % of a small market is allowed
    ch = by.filter(pl.col("exchange_country") == "CH").row(0, named=True)
    assert ch["issuers"] == 1 and ch["valued_usd_tn"] == pytest.approx(0.39)   # two classes, one issuer, counted once
    by2, _ = MP.exchange_flags(val, sec, country, totals={"JP": 1.0})
    assert by2.filter(pl.col("exchange_country") == "JP").item(0, "flagged")  # valued 1.9 tn > 1.5 x 1.0 tn


def test_issued_including_treasury_is_a_fallback():
    iss = "jpcrp_cor:NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc"
    c = pl.DataFrame({"issuer_id": ["H"] * 3,
                      "period_end": [date(2025, 12, 31), date(2026, 6, 18), date(2027, 9, 1)],
                      "knowledge_date": [date(2026, 3, 1), date(2026, 6, 18), date(2027, 9, 1)],
                      "src": ["yahoo", "regulator", "regulator"], "shares": [3.89e9, 4.53e9, 4.5e9],
                      "source_concept": ["Ordinary Shares Number", iss, iss]})
    out = SP.regulator_precedence(SP.demote_issued_counts(c)).sort("period_end")
    # Honda: the issued count (incl. ~14 % treasury) is dropped while a recent outstanding count is known; kept as
    # the only count once the outstanding one is older than REGULATOR_FRESH_DAYS
    assert out.get_column("shares").to_list() == [3.89e9, 4.5e9]


def test_issued_fallback_never_supersedes_outstanding_counts():
    iss = "jpcrp_cor:NumberOfIssuedSharesAsOfFilingDateIssuedSharesTotalNumberOfSharesEtc"
    c = pl.DataFrame({"issuer_id": ["T"] * 3,
                      "period_end": [date(2025, 6, 19), date(2025, 6, 30), date(2026, 6, 30)],
                      "knowledge_date": [date(2025, 6, 19), date(2025, 8, 29), date(2026, 8, 29)],
                      "src": ["regulator", "yahoo", "yahoo"], "shares": [1.934e9, 2.87e10, 2.85e10],
                      "source_concept": [iss, "Ordinary Shares Number", "Ordinary Shares Number"]})
    out = SP.regulator_precedence(SP.demote_issued_counts(c)).sort("period_end")
    assert out.get_column("shares").to_list() == [1.934e9, 2.87e10, 2.85e10]   # fallback first, then Yahoo


# ------------------------------------------------------------------------------------------------ A6 review round 2


def test_latest_known_keeps_one_event_per_knowledge_date():
    from app.analytics.valuation import latest_known

    kd = date(2026, 4, 30)
    rows = pl.DataFrame({"issuer_id": ["C"] * 4,
                         "period_end": [date(2025, 12, 31), date(2023, 12, 31), date(2024, 12, 31), date(2026, 3, 31)],
                         "knowledge_date": [kd, kd, kd, date(2026, 5, 20)],
                         "shares": [3.2e7, 3.0e7, 3.1e7, 3.25e7]})
    for perm in ([0, 1, 2, 3], [3, 2, 1, 0], [1, 3, 0, 2]):
        out = latest_known(rows[perm]).sort("knowledge_date")
        assert out.select("period_end", "shares").rows() == [(date(2025, 12, 31), 3.2e7), (date(2026, 3, 31), 3.25e7)]


def test_daily_market_cap_tied_knowledge_dates_do_not_depend_on_the_batch(tmp_data):
    """CAAS-like: one filing first reports FY23, FY24 and FY25. The count is the FY25 one, alone or in any batch."""
    from app.analytics.valuation import daily_market_cap

    days = [date(2026, 4, 1) + timedelta(days=i) for i in range(60)]
    kd = date(2026, 4, 30)
    secs = [_sec("SC", "IC", "CAAS")] + [_sec(f"X{i}", f"IX{i}", f"X{i}") for i in range(6)]
    write_table(pl.DataFrame(secs), "securities_master")
    write_table(pl.concat([_px(x["security_id"], days, [4.0] * len(days), [1.0] * len(days)) for x in secs]),
                "prices_daily")
    rows = _canonical_rows("IC", [("shares_outstanding", "INSTANT", date(y, 12, 31), kd, v, "sec_edgar", "dei")
                                  for y, v in ((2023, 3.0e7), (2024, 3.1e7), (2025, 3.2e7))])
    for i in range(6):
        rows += _canonical_rows(f"IX{i}", [("shares_outstanding", "INSTANT", date(2025, 12, 31), date(2026, 3, 1),
                                            1e8 + i, "sec_edgar", "dei")])
    write_table(pl.DataFrame(rows), "fundamentals_canonical")
    d = date(2026, 5, 15)

    def sh(batch):
        mc = daily_market_cap(pl.DataFrame({"security_id": [x["security_id"] for x in batch],
                                            "issuer_id": [x["issuer_id"] for x in batch]}), days[0], days[-1])
        return mc.filter((pl.col("security_id") == "SC") & (pl.col("date") == d)).item(0, "shares_out")

    got = {sh(secs[:1]), sh(secs), sh(list(reversed(secs))), sh([secs[3], secs[0], secs[5]])}
    assert got == {3.2e7}


def test_classify_split_steps_rules():
    d0 = date(2026, 1, 1)
    days = [d0 + timedelta(days=i) for i in range(200)]
    t = days[100]

    def bars(key, r, vol_after):
        return pl.DataFrame({"security_id": key, "date": days,
                             "split_factor": [1.0 / r if d < t else 1.0 for d in days],
                             "volume": [1e6 if d < t else vol_after for d in days]})

    # A clean 2:1 (no volume needed) | B FDX-like 1.241, volume flat | C non-clean 1.7071, volume x1.7 | D 3 % step |
    # E 1.241 with a Polygon split record | F ADR whose underlying has no split | G ADR whose underlying U splits too |
    # H non-clean 1.7071, volume flat
    spec = {"A": (2.0, 1e6), "B": (1.241, 1e6), "C": (1.7071, 1.7e6), "D": (1.032, 1.03e6), "E": (1.241, 1e6),
            "F": (2.0, 2e6), "G": (2.0, 2e6), "U": (2.0, 2e6), "H": (1.7071, 1e6)}
    bb = pl.concat([bars(k, r, v) for k, (r, v) in spec.items()])
    steps = SP.split_steps(bb.select("security_id", "date", "split_factor"))
    assert steps.height == len(spec) and steps.filter(pl.col("security_id") == "A").item(0, "date") == t
    poly = pl.DataFrame({"security_id": ["E"], "date": [t + timedelta(days=1)], "r": [1.241]})
    dr = pl.DataFrame({"security_id": ["F", "G"], "underlying": ["NOPE", "U"]})
    c = SP.classify_split_steps(steps, bb.select("security_id", "date", "volume"), poly, dr)
    why = dict(c.select("security_id", "why").iter_rows())
    gen = dict(c.select("security_id", "genuine").iter_rows())
    assert why == {"A": "clean_ratio", "B": "unconfirmed", "C": "volume", "D": "small_unclean", "E": "polygon",
                   "F": "dr_no_underlying", "G": "underlying", "U": "clean_ratio", "H": "unconfirmed"}
    assert sorted(k for k, v in gen.items() if v) == ["A", "C", "E", "G", "U"]
    f = SP.share_split_factor(bb, c)
    a = f.filter(pl.col("security_id") == "A")
    assert a.filter(pl.col("date") < t).get_column("split_factor").unique().to_list() == [0.5]
    assert a.filter(pl.col("date") >= t).get_column("split_factor").unique().to_list() == [1.0]
    assert f.filter(pl.col("security_id") == "B").get_column("split_factor").unique().to_list() == [1.0]
    assert SP.clean_ratio(1 / 30) and SP.clean_ratio(1.5) and not SP.clean_ratio(1.241) and not SP.clean_ratio(1.196)


def test_daily_market_cap_ignores_a_spin_off_price_factor(tmp_data):
    """FDX-like (A6 review): Yahoo records the FedEx Freight spin-off as a 1.241 'split' on 2026-06-01; volume is
    unchanged. The 10-K count of 2026-05-31 (filed 07-15) is the share count: it must not become x1.241."""
    from app.analytics.valuation import daily_market_cap

    days = [date(2026, 2, 1) + timedelta(days=i) for i in range(200)]
    ex = date(2026, 6, 1)
    sf = [1 / 1.241 if d < ex else 1.0 for d in days]
    write_table(pl.DataFrame([_sec("SF", "IF", "FDX")]), "securities_master")
    write_table(_px("SF", days, [390.0 if d < ex else 316.0 for d in days], sf, vol=1.6e6), "prices_daily")
    write_table(pl.DataFrame(_canonical_rows("IF", [
        ("shares_outstanding", "INSTANT", date(2026, 2, 28), date(2026, 3, 20), 2.38e8, "sec_edgar", "dei"),
        ("shares_outstanding", "INSTANT", date(2026, 5, 31), date(2026, 7, 15), 2.3658e8, "sec_edgar", "dei")])),
        "fundamentals_canonical")
    mc = daily_market_cap(pl.DataFrame({"security_id": ["SF"], "issuer_id": ["IF"]}), date(2026, 3, 1), days[-1])

    def sh(d):
        return mc.filter(pl.col("date") == d).item(0, "shares_out")

    assert sh(date(2026, 5, 15)) == pytest.approx(2.38e8)
    assert sh(date(2026, 6, 15)) == pytest.approx(2.38e8)          # after the spin-off: not x1.241
    assert sh(date(2026, 8, 3)) == pytest.approx(2.3658e8)         # the 10-K count as filed
    assert mc.filter(pl.col("date") == date(2026, 8, 3)).item(0, "market_cap_usd") == pytest.approx(2.3658e8 * 316.0)


def test_adr_seed_ratio_is_used_before_the_master_has_it(tmp_data):
    """A6 review: an ADR whose ratio only the (generated) SEC seed knows is valued with it, not withheld."""
    from app.analytics.valuation import daily_market_cap
    from app.normalize.identity_universe import seeds_dir

    days = [date(2026, 8, 1) + timedelta(days=i) for i in range(100)]
    write_table(pl.DataFrame([_sec("ST", "IT", "TLK", "adr"), _sec("SA", "IA", "ADRZ", "adr")]), "securities_master")
    write_table(pl.concat([_px("ST", days, [20.0] * 100, [1.0] * 100, 8.0e5),
                           _px("SA", days, [10.0] * 100, [1.0] * 100, 500.0)]), "prices_daily")
    pe, kd = date(2026, 6, 30), date(2026, 8, 29)
    write_table(pl.DataFrame(_canonical_rows("IT", [("shares_outstanding", "INSTANT", pe, kd, 9.9e10, "sec_edgar", "dei")])
                             + _canonical_rows("IA", [("shares_outstanding", "INSTANT", pe, kd, 1.5e11, "sec_edgar",
                                                       "dei")])), "fundamentals_canonical")
    d = seeds_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "adr_ratios_sec.csv").write_text("# generated\nadr_ticker,adr_ratio,underlying,verified,note\n"
                                          "TLK,100,,sec_cover,x\n", encoding="utf-8")
    mc = daily_market_cap(pl.DataFrame({"security_id": ["ST", "SA"], "issuer_id": ["IT", "IA"]}), date(2026, 9, 1),
                          days[-1])
    last = mc.filter(pl.col("date") == days[-1])
    # TLK: 9.9e10 Series B shares / 100 per ADS x USD 20 = USD 19.8 bn (turnover 8e5 / 9.9e10 would have withheld it)
    assert last.filter(pl.col("security_id") == "ST").item(0, "market_cap_usd") == pytest.approx(9.9e10 / 100 * 20.0)
    assert last.filter(pl.col("security_id") == "SA").item(0, "market_cap_usd") is None     # no ratio anywhere


def test_isin_ids_matches_a_plain_list():
    lf = pl.LazyFrame({"security_id": ["a", "b", "c", None], "v": [1, 2, 3, 4]})
    assert lf.filter(SP.isin_ids("security_id", ["c", "a"])).collect().get_column("v").to_list() == [1, 3]
    assert lf.filter(SP.isin_ids("security_id", [])).collect().is_empty()
