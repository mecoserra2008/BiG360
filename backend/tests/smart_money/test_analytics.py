"""Smart-money analytics (roadmap #7): 13F PIT cutoff, holder-change counts, ownership %, HHI, hedge-fund-like rule,
smart cohort without look-ahead; short-interest / squeeze / FTD math; activism events; EU short as-of; composite PIT
and sign; alert evaluators."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import polars as pl
import pytest

from app.analytics import smart_money as SM
from app.analytics import smart_money_13f as S13
from app.scheduler import alerts as AL

Q = [date(2025, 3, 31), date(2025, 6, 30), date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30)]


def con_with(frames: dict[str, pl.DataFrame]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for k, v in frames.items():
        con.register(f"_{k}", v.to_arrow())
        con.execute(f'CREATE TABLE "{k}" AS SELECT * FROM "_{k}"')
    return con


def sm_frame(ids: list[str]) -> pl.DataFrame:
    return pl.DataFrame({"security_id": ids, "issuer_id": [f"I{x}" for x in ids], "is_primary_security": [True] * len(ids),
                         "security_type": ["common"] * len(ids)})


def h(filer: str, pe: date, sid: str, sh: float, kd: date | None = None, pc: str = "none", px: float = 10.0) -> dict:
    return {"filer_cik": filer, "filer_name": filer, "period_end": pe, "cusip": f"C{sid}", "security_id": sid,
            "put_call": pc, "shares": sh, "value_usd": sh * px, "knowledge_date": kd or pe + timedelta(days=40),
            "amount_type": "SH"}


def prices(ids: list[str], start: date, end: date, drift: dict[str, float] | None = None) -> pl.DataFrame:
    rows, d = [], start
    k = 0
    while d <= end:
        if d.weekday() < 5:
            for s in ids:
                rows.append({"security_id": s, "date": d, "adj_close": 10.0 * (1 + (drift or {}).get(s, 0.0)) ** k,
                             "split_factor": 1.0})
            k += 1
        d += timedelta(days=1)
    return pl.DataFrame(rows)


# ------------------------------------------------------------------------------------------------ 13F


def test_deadline_rules():
    assert S13.deadline(date(2026, 6, 30)) == date(2026, 8, 17)       # Fri 14 Aug due -> +1 business day (Mon)
    assert S13.deadline(date(2025, 12, 31)) == date(2026, 2, 18)      # Sat 14 Feb -> Tue 17 (Presidents' Day) -> +1
    assert S13.deadline(date(2026, 3, 31)) == date(2026, 5, 18)       # Fri 15 May -> Mon 18


def _base_13f() -> dict[str, pl.DataFrame]:
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    rows = [h("A", q1, "X", 100), h("A", q2, "X", 200),                  # added
            h("B", q1, "X", 100), h("B", q2, "X", 50),                   # trimmed
            h("C", q1, "Y", 10), h("C", q2, "X", 30),                    # new on X (filed both quarters)
            h("D", q1, "X", 40), h("D", q2, "Y", 5),                     # exited X
            h("E", q1, "X", 70), h("E", q2, "X", 70),                    # unchanged
            h("F", q2, "X", 1000),                                       # first-time filer: not a "new" position
            h("G", q2, "X", 5000, kd=date(2026, 9, 1)),                  # filed after the cutoff: invisible
            h("A", q2, "X", 7, pc="put")]
    ids = ["X", "Y"]
    return {"institutional_holdings": pl.DataFrame(rows), "prices_daily": prices(ids, date(2026, 3, 1), date(2026, 7, 10)),
            "valuation_daily": pl.DataFrame({"security_id": ["X", "X"], "date": [q1, q2], "shares_out": [1000.0, 2000.0],
                                             "market_cap_usd": [1e4, 2e4]}),
            "fundamentals_canonical": pl.DataFrame({"issuer_id": ["IY"], "item_id": ["shares_outstanding"], "value": [100.0],
                                                    "period_end": [date(2026, 3, 1)], "knowledge_date": [date(2026, 4, 1)]}),
            "securities_master": sm_frame(ids)}


def test_holder_changes_ownership_and_pit_cutoff():
    con = con_with(_base_13f())
    filers, inst = S13.compute(con, date(2026, 10, 9), min_filers=1)
    x = inst.filter((pl.col("security_id") == "X") & (pl.col("period_end") == date(2026, 6, 30))).row(0, named=True)
    assert x["knowledge_date"] == date(2026, 8, 17) and x["is_complete"]
    assert (x["n_new"], x["n_added"], x["n_trimmed"], x["n_exited"], x["n_unchanged"]) == (1, 1, 1, 1, 1)
    assert x["n_holders"] == 5                                            # A B C E F (G filed after the cutoff)
    assert x["shares_held"] == 200 + 50 + 30 + 70 + 1000                  # G (late) excluded, put rows excluded
    assert x["net_flow_shares"] == (200 - 100) + (50 - 100) + 30 - 40 + 0
    assert x["inst_own_pct"] == pytest.approx(x["shares_held"] / 2000.0)
    assert x["shares_out_source"] == "valuation_daily" and x["put_value_usd"] == 70.0
    w = [1000, 200, 70, 50, 30]
    assert x["hhi"] == pytest.approx(sum((v / sum(w)) ** 2 for v in w))
    y1 = inst.filter((pl.col("security_id") == "Y") & (pl.col("period_end") == date(2026, 3, 31))).row(0, named=True)
    assert y1["shares_out_source"] == "fundamentals" and y1["inst_own_pct"] == pytest.approx(10 / 100)
    # in-progress quarter: cutoff = run date, not complete
    _, inst2 = S13.compute(con_with(_base_13f()), date(2026, 8, 12), min_filers=1)
    x2 = inst2.filter((pl.col("security_id") == "X") & (pl.col("period_end") == date(2026, 6, 30))).row(0, named=True)
    assert x2["knowledge_date"] == date(2026, 8, 12) and not x2["is_complete"]
    assert filers.filter(pl.col("filer_cik") == "G").is_empty() or \
        filers.filter((pl.col("filer_cik") == "G") & (pl.col("period_end") == date(2026, 6, 30))).is_empty()


def _book_frames(n_hf: int = 12, n_idx: int = 12, after: float = 0.0) -> dict[str, pl.DataFrame]:
    """HF filers rotate half of a 12-name book each quarter; index filers hold 40 names unchanged. Stock S0..S5 rise."""
    ids = [f"S{i}" for i in range(60)]
    rows = []
    for f in range(n_hf):
        for qi, pe in enumerate(Q):
            book = [(f * 3 + qi * 6 + k) % 60 for k in range(12)]
            for k in book:
                rows.append(h(f"HF{f}", pe, ids[k], 1e6 + f))
    for f in range(n_idx):
        for pe in Q:
            for k in range(40):
                rows.append(h(f"IX{f}", pe, ids[k], 1e7))
    drift = {s: (0.003 if int(s[1:]) % 7 == 0 else 0.0) for s in ids}
    px = prices(ids, date(2025, 3, 1), date(2026, 9, 30), drift)
    if after:
        px = px.with_columns(pl.when(pl.col("date") > Q[-1]).then(pl.col("adj_close") * (1 + after)).otherwise(pl.col("adj_close"))
                             .alias("adj_close"))
    return {"institutional_holdings": pl.DataFrame(rows), "prices_daily": px,
            "valuation_daily": pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "shares_out": pl.Float64,
                                                    "market_cap_usd": pl.Float64}),
            "fundamentals_canonical": pl.DataFrame(schema={"issuer_id": pl.Utf8, "item_id": pl.Utf8, "value": pl.Float64,
                                                           "period_end": pl.Date, "knowledge_date": pl.Date}),
            "securities_master": sm_frame(ids)}


def test_hedge_fund_like_and_smart_cohort_pit():
    filers, inst = S13.compute(con_with(_book_frames()), date(2026, 10, 9), min_filers=1)
    last = filers.filter(pl.col("period_end") == Q[-1])
    hf = last.filter(pl.col("filer_cik").str.starts_with("HF"))
    ix = last.filter(pl.col("filer_cik").str.starts_with("IX"))
    assert hf["hedge_fund_like"].all() and not ix["hedge_fund_like"].any()   # turnover vs index books (40 names, 0)
    assert (hf["turnover"] > 0.2).all() and (ix["turnover"] == 0).all()
    assert last["smart_n_quarters"].max() == 4 and last["in_smart_cohort"].sum() >= 1
    assert not ix["in_smart_cohort"].any()                                  # cohort only among hedge-fund-like
    # returns of the last book are known only at the NEXT period end -> not used for any smart_score
    assert last["ret_next_q"].is_null().all()
    # changing prices after the last period end must not change any PIT score of that quarter
    f2, _ = S13.compute(con_with(_book_frames(after=0.5)), date(2026, 10, 9), min_filers=1)
    a = last.sort("filer_cik").select("filer_cik", "smart_score", "in_smart_cohort")
    b = f2.filter(pl.col("period_end") == Q[-1]).sort("filer_cik").select("filer_cik", "smart_score", "in_smart_cohort")
    assert a["filer_cik"].equals(b["filer_cik"]) and a["in_smart_cohort"].equals(b["in_smart_cohort"])
    assert (a["smart_score"] - b["smart_score"]).abs().max() < 1e-12      # parallel float sums: tolerance only
    assert inst.filter(pl.col("period_end") == Q[-1])["hf_top20_count"].max() > 0


# ------------------------------------------------------------------------------------------------ short interest


def _si_frames() -> dict[str, pl.DataFrame]:
    s0 = date(2026, 9, 15)
    si = []
    for j in range(10):
        sd = s0 - timedelta(days=15 * j)
        for sid, base in (("A", 0.20), ("B", 0.02)):
            si.append({"security_id": sid, "settlement_date": sd, "short_interest_shares": base * 1e8 * (1 - 0.02 * j),
                       "avg_daily_volume": 1e6, "days_to_cover": 8.0 if sid == "A" else 1.0, "knowledge_date": sd + timedelta(days=9),
                       "knowledge_date_estimated": False})
    si.append({"security_id": "finra:ZZZ", "settlement_date": s0, "short_interest_shares": 1.0, "avg_daily_volume": 1.0,
               "days_to_cover": 1.0, "knowledge_date": s0, "knowledge_date_estimated": False})
    px = prices(["A", "B"], date(2026, 1, 1), date(2026, 10, 1), {"A": 0.01, "B": 0.0})
    ftd = []
    for k in range(8):
        end = s0 - timedelta(days=15 * k)
        ftd.append({"security_id": "B", "cusip": "CB", "file_name": f"f{k}", "settlement_date": end,
                    "quantity_fails": 5e6 if k == 0 else 1e3, "knowledge_date": end + timedelta(days=5)})
    ftd.append({"security_id": "A", "cusip": "CA", "file_name": "late", "settlement_date": s0, "quantity_fails": 9e9,
                "knowledge_date": s0 + timedelta(days=30)})                  # published after the SI -> not attached
    return {"short_interest": pl.DataFrame(si), "prices_daily": px, "fails_to_deliver": pl.DataFrame(ftd),
            "valuation_daily": pl.DataFrame({"security_id": ["A", "B"], "date": [s0, s0], "shares_out": [1e8, 1e8],
                                             "market_cap_usd": [1e9, 5e10]}),
            "fundamentals_canonical": pl.DataFrame(schema={"issuer_id": pl.Utf8, "item_id": pl.Utf8, "value": pl.Float64,
                                                           "period_end": pl.Date, "knowledge_date": pl.Date}),
            "securities_master": sm_frame(["A", "B"])}


def test_si_signals_math_squeeze_ftd():
    out = SM.si_signals(con_with(_si_frames()), date(2026, 10, 9))
    assert "finra:ZZZ" not in out["security_id"].to_list()
    a = out.filter((pl.col("security_id") == "A") & (pl.col("settlement_date") == date(2026, 9, 15))).row(0, named=True)
    assert a["si_pct_out"] == pytest.approx(0.20) and a["si_chg_pct"] == pytest.approx(1 / 0.98 - 1)
    assert a["si_z_1y"] is not None and a["si_z_1y"] > 0                  # rising short interest
    assert a["ret_20d"] > 0.10 and a["squeeze_flag"]                       # 20% SI, DTC 8, rising price, mcap 1B
    assert a["ftd_max_shares"] is None                                     # the FTD file was published after the SI
    b = out.filter((pl.col("security_id") == "B") & (pl.col("settlement_date") == date(2026, 9, 15))).row(0, named=True)
    assert not b["squeeze_flag"] and b["ftd_spike"] and b["ftd_pct_out"] == pytest.approx(0.05)
    # PIT: nothing published after as_of
    early = SM.si_signals(con_with(_si_frames()), date(2026, 9, 20))
    assert early["knowledge_date"].max() <= date(2026, 9, 20)


# ------------------------------------------------------------------------------------------------ activism / EU


def test_activist_events_switch():
    f = pl.DataFrame([
        {"filing_id": "a", "knowledge_date": date(2025, 1, 5), "schedule": "13G", "is_amendment": False, "filer_cik": "F",
         "subject_cik": "S", "form_type": "SC 13G"},
        {"filing_id": "b", "knowledge_date": date(2026, 5, 5), "schedule": "13D", "is_amendment": False, "filer_cik": "F",
         "subject_cik": "S", "form_type": "SCHEDULE 13D"},
        {"filing_id": "c", "knowledge_date": date(2026, 6, 5), "schedule": "13D", "is_amendment": True, "filer_cik": "F",
         "subject_cik": "S", "form_type": "SCHEDULE 13D/A"},
        {"filing_id": "d", "knowledge_date": date(2026, 6, 6), "schedule": "13D", "is_amendment": False, "filer_cik": "G",
         "subject_cik": "S", "form_type": "SCHEDULE 13D"},
        {"filing_id": "e", "knowledge_date": date(2026, 12, 1), "schedule": "13D", "is_amendment": False, "filer_cik": "H",
         "subject_cik": "S", "form_type": "SCHEDULE 13D"},
    ]).with_columns(pl.lit("x").alias("subject_name"), pl.lit("n").alias("filer_name"), pl.lit(1).alias("n_filers"),
                    pl.lit(None, pl.Utf8).alias("issuer_id"), pl.lit(None, pl.Utf8).alias("security_id"),
                    pl.lit("ticker_map").alias("role_source"), pl.lit("u").alias("url"))
    ev = {r["filing_id"]: r for r in SM.activist_events(con_with({"activist_filings": f}), date(2026, 10, 9)).to_dicts()}
    assert ev["a"]["event_type"] == "new_13g" and ev["b"]["event_type"] == "switch_13g_to_13d"
    assert ev["b"]["prior_13g_date"] == date(2025, 1, 5)
    assert ev["c"]["event_type"] == "amend_13d" and ev["d"]["event_type"] == "new_13d"
    assert "e" not in ev                                                    # filed after as_of


def _eu(reg: str, ptype: str, holder: str, pct: float | None, d: date, below: bool = False, sid: str = "X") -> dict:
    return {"regulator": reg, "position_type": ptype, "holder_name": holder, "isin": "GB0000000001", "security_id": sid,
            "position_pct": pct, "below_threshold": below, "position_date": d, "knowledge_date": d + timedelta(days=2),
            "issuer_name": "X plc"}


def test_eu_short_asof_rules():
    rows = [_eu("afm", "individual", "H1", 0.8, date(2026, 9, 1)), _eu("afm", "individual", "H2", 0.6, date(2026, 9, 1)),
            _eu("afm", "individual", "H2", 0.4, date(2026, 9, 20), below=True),
            _eu("fca", "individual", "H3", 1.0, date(2026, 6, 1), sid="Y"),
            _eu("fca", "aggregate", "(aggregate)", 2.5, date(2026, 9, 10), sid="Y")]
    con = con_with({"eu_short_positions": pl.DataFrame(rows)})
    a = {r["security_id"]: r for r in SM.eu_short_asof(con, date(2026, 9, 30)).to_dicts()}
    assert a["X"]["eu_short_pct"] == pytest.approx(0.8)                    # H2 fell below 0.5% -> dropped
    assert a["Y"]["eu_short_pct"] == pytest.approx(2.5)                    # regulator publishes aggregates -> used
    b = {r["security_id"]: r for r in SM.eu_short_asof(con, date(2026, 9, 10)).to_dicts()}
    assert b["X"]["eu_short_pct"] == pytest.approx(1.4)                    # before H2's closing notification was public


# ------------------------------------------------------------------------------------------------ composite


def test_standardize_signs_and_groups():
    raw = pl.DataFrame({"as_of_date": [date(2026, 10, 9)] * 4, "security_id": list("ABCD"),
                        "si_pct_out": [0.01, 0.05, 0.10, 0.30], "si_chg_pct": [0.0, 0.0, 0.0, 0.0],
                        "inst_flow": [None, None, None, None]})
    df = SM.standardize(raw).sort("security_id")
    assert df["z_si_level"].to_list() == sorted(df["z_si_level"].to_list(), reverse=True)   # more short interest = lower
    assert df["score"].to_list() == sorted(df["score"].to_list(), reverse=True)
    assert (df["n_components"] == 1).all() and df["g_inst"].is_null().all()
    assert df["score_pct"].min() > 0 and df["score_pct"].max() < 100


def test_raw_components_pit():
    si = pl.DataFrame({"security_id": ["A", "A"], "settlement_date": [date(2026, 9, 15), date(2026, 9, 30)],
                       "knowledge_date": [date(2026, 9, 24), date(2026, 10, 9)], "si_pct_out": [0.1, 0.5],
                       "si_chg_pct": [0.0, 4.0]})
    empty = {"inst_ownership": pl.DataFrame(schema={"security_id": pl.Utf8, "knowledge_date": pl.Date, "period_end": pl.Date,
                                                    "flow_pct_out": pl.Float64, "shares_held_chg_pct": pl.Float64,
                                                    "breadth": pl.Float64, "smart_flow_pct_out": pl.Float64,
                                                    "smart_net_flow_shares": pl.Float64, "shares_held_prev": pl.Float64}),
             "insider_signals": pl.DataFrame(schema={"security_id": pl.Utf8, "as_of_date": pl.Date, "signal_score": pl.Float64}),
             "eu_short_positions": pl.DataFrame([_eu("afm", "individual", "H", 0.9, date(2026, 10, 7), sid="A")])}
    r = SM.raw_components(con_with(empty), date(2026, 10, 8), si).row(0, named=True)
    assert r["si_pct_out"] == 0.1 and r["si_settlement_date"] == date(2026, 9, 15)   # 30 Sep published on 9 Oct
    assert r["eu_short_pct"] is None                                                 # notification public on 9 Oct
    r2 = SM.raw_components(con_with(empty), date(2026, 10, 9), si).row(0, named=True)
    assert r2["si_pct_out"] == 0.5 and r2["eu_short_pct"] == 0.9 and r2["eu_short_chg_30d"] == 0.9


def test_signal_dates():
    ds = SM.signal_dates(date(2026, 10, 14), date(2026, 9, 28))
    assert ds == [date(2026, 10, 2), date(2026, 10, 9), date(2026, 10, 14)]


# ------------------------------------------------------------------------------------------------ alerts


def test_alert_evaluators():
    as_of = date(2026, 10, 9)
    ev = pl.DataFrame([{"security_id": "X", "filing_id": "f1", "event_date": as_of, "knowledge_date": as_of,
                        "event_type": "new_13d", "form_type": "SCHEDULE 13D", "schedule": "13D", "filer_name": "Elliott",
                        "subject_name": "X", "url": "u"},
                       {"security_id": "X", "filing_id": "f2", "event_date": as_of, "knowledge_date": as_of,
                        "event_type": "amend_13d", "form_type": "SCHEDULE 13D/A", "schedule": "13D", "filer_name": "Trian",
                        "subject_name": "X", "url": "u"}])
    si = pl.DataFrame([{"security_id": "X", "settlement_date": date(2026, 9, 30), "knowledge_date": as_of, "si_shares": 2e6,
                        "si_pct_out": 0.12, "si_chg_pct": 0.5, "days_to_cover": 3.0, "squeeze_flag": False}])
    eu = pl.DataFrame([_eu("afm", "individual", "H", 0.45, date(2026, 9, 1)),
                       {**_eu("afm", "individual", "H", 0.62, date(2026, 10, 6)), "knowledge_date": as_of},
                       {**_eu("afm", "individual", "K", 0.9, date(2026, 9, 1)), "knowledge_date": date(2026, 9, 3)},
                       {**_eu("afm", "individual", "K", 0.95, date(2026, 10, 6)), "knowledge_date": as_of}])
    dp = AL.frames_provider({"activist_events": ev, "si_signals": si, "eu_short_positions": eu})
    t = AL.EVALUATORS["activist_13d"]({}, ["X"], dp, as_of)
    assert [x.payload["filing_id"] for x in t] == ["f1"] and "Elliott" in t[0].message
    assert len(AL.EVALUATORS["activist_13d"]({"include_amendments": True}, ["X"], dp, as_of)) == 2
    s = AL.EVALUATORS["short_interest_spike"]({"min_chg_pct": 0.25}, ["X"], dp, as_of)
    assert len(s) == 1 and "FINRA" in s[0].message
    assert not AL.EVALUATORS["short_interest_spike"]({"min_chg_pct": 0.6}, ["X"], dp, as_of)
    e = AL.EVALUATORS["eu_short_position"]({}, ["X"], dp, as_of)
    assert {x.payload["holder_name"] for x in e} == {"H"}                 # K: +0.05 pp < 0.1 pp step
    assert len(AL.EVALUATORS["eu_short_position"]({"min_increase_pp": 0.05}, ["X"], dp, as_of)) == 2


def test_split_adjusted_changes():
    """A 10:1 split between quarters must not look like a 10x accumulation."""
    q1, q2 = date(2026, 3, 31), date(2026, 6, 30)
    fr = _base_13f()
    rows = [h("A", q1, "Z", 100), h("A", q2, "Z", 1000), h("B", q1, "Z", 100), h("B", q2, "Z", 1500),
            h("A", q1, "X", 1), h("B", q1, "X", 1), h("A", q2, "X", 1), h("B", q2, "X", 1)]
    px = prices(["X", "Z"], date(2026, 3, 1), date(2026, 7, 10)).with_columns(
        pl.when((pl.col("security_id") == "Z") & (pl.col("date") <= date(2026, 5, 1))).then(0.1).otherwise(1.0).alias("split_factor"))
    fr.update(institutional_holdings=pl.DataFrame(rows), prices_daily=px, securities_master=sm_frame(["X", "Z"]))
    _, inst = S13.compute(con_with(fr), date(2026, 10, 9), min_filers=1)
    z = inst.filter((pl.col("security_id") == "Z") & (pl.col("period_end") == q2)).row(0, named=True)
    assert (z["n_added"], z["n_unchanged"]) == (1, 1)                   # A unchanged after the split, B added
    assert z["net_flow_shares"] == pytest.approx(500) and z["shares_held_prev"] == pytest.approx(2000)
    assert z["shares_held_chg_pct"] == pytest.approx(0.25)
