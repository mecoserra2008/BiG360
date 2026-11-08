"""COVERAGE_V2 W1 (2026-10-31): earnings for the full universe - official schedule sources, incremental Yahoo due rules,
official release rows in earnings_events, the seasonal random walk for short histories, per-section reasons."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl

from app.earnings import announce as A
from app.earnings import common as C
from app.earnings import events as E
from app.earnings import expectations as X
from app.earnings import fetch as F

# ------------------------------------------------------------------------------------------------ HKEX BMN

_BMN = """<html><body><table class=textfont>
<tr><td>BM Date</td><td></td><td>Stock Short Name</td><td>&nbsp;Code</td><td>Purpose</td><td>Period</td></tr>
<tr><td width=75>30/10/2026</td><td></td><td>ABC</td><td>&nbsp;1288</td><td>RESULTS</td><td>QTR ENDED30/09/26</td></tr>
<tr><td>27/11/2026</td><td></td><td>STYLAND HOLD</td><td>&nbsp;&nbsp;211</td><td>INT RES/DIV</td><td>6-MTH-ENDED30/09/26</td></tr>
<tr><td>30/10/2026</td><td></td><td>BOC HONG KONG</td><td>2388</td><td>3RD QTR RES/INT DIV</td><td>THIRD QUARTER 2026</td></tr>
<tr><td>02/11/2026</td><td></td><td>SOME CO</td><td>1</td><td>INT DIV</td><td>6-MTH-ENDED30/09/26</td></tr>
</table></body></html>"""


def test_parse_bmn_results_rows_only():
    rows = A.parse_bmn(_BMN)
    assert [r["code"] for r in rows] == ["1288", "0211", "2388"]          # dividend-only purpose dropped, codes padded
    assert rows[0]["bm_date"] == date(2026, 10, 30) and rows[0]["period_end"] == date(2026, 9, 30)
    assert rows[1]["period_end"] == date(2026, 9, 30)
    assert rows[2]["period_end"] == date(2026, 9, 30)                      # "THIRD QUARTER 2026" -> calendar Q3 end


def test_bmn_period_end_must_precede_the_meeting():
    assert A.bmn_period_end("Y.E.31/07/26", date(2026, 10, 30)) == date(2026, 7, 31)
    assert A.bmn_period_end("Y.E.31/12/26", date(2026, 10, 30)) is None   # not ended yet: cadence decides
    assert A.bmn_period_end("", date(2026, 10, 30)) is None


# ------------------------------------------------------------------------------------------------ NSM notices


def test_notice_date_skips_period_ends_and_weekends():
    txt = ("Acme plc will announce its half year results for the six months ended 31 October 2026 on Thursday "
           "10 December 2026. A presentation follows on 11 December 2026.")
    assert A.notice_date(txt, date(2026, 11, 2)) == date(2026, 12, 10)
    assert A.notice_date("Results on November 12, 2026 at 7am", date(2026, 10, 30)) == date(2026, 11, 12)
    assert A.notice_date("results for the year to 30 June 2026", date(2026, 7, 2)) is None
    assert A.notice_date("results on 31 October 2026", date(2026, 10, 1)) is None   # a Saturday
    assert A.notice_date("results on 3 March 2026", date(2026, 10, 1)) is None      # in the past


# ------------------------------------------------------------------------------------------------ CNINFO


def test_parse_cninfo_booked_vs_actual():
    rows = [{"seccode": "000001", "f001d_0102": "2026-09-30", "f002d_0102": "2026-10-24", "f003d_0102": "",
             "f004d_0102": "", "f005d_0102": "", "f006d_0102": "2026-10-24"},
            {"seccode": "600000", "f001d_0102": "2026-12-31", "f002d_0102": "2027-04-20", "f003d_0102": "2027-03-28",
             "f004d_0102": "", "f005d_0102": "", "f006d_0102": ""}]
    out = A.parse_cninfo(rows, date(2026, 9, 30))
    assert out[0] == {"code": "000001", "booked": date(2026, 10, 24), "actual": date(2026, 10, 24),
                      "period_end": date(2026, 9, 30)}
    assert out[1]["booked"] == date(2027, 3, 28) and out[1]["actual"] is None   # the latest change wins


def test_cninfo_sections_latest_ended_and_next():
    assert A.cninfo_sections(date(2026, 10, 31)) == [date(2026, 9, 30), date(2026, 12, 31)]
    assert A.cninfo_sections(date(2027, 1, 15)) == [date(2026, 12, 31), date(2027, 3, 31)]


# ------------------------------------------------------------------------------------------------ hints / events


def test_hint_period_formats():
    assert E.hint_period("Q2 FYE 2027-03-31")[1] == "Q2"
    assert E.hint_period("PE 2026-09-30 | RESULTS QTR ENDED30/09/26") == (date(2026, 9, 30), None)
    assert E.hint_period("IR | Half-year Financial Report") == (None, "H1")
    assert E.hint_period(None) == (None, None)


def test_official_release_row_becomes_the_period_event():
    m = {"region": "CN", "timezone": "Asia/Shanghai", "security_id": "S1", "country": "CN", "fiscal_year_end_mmdd": "1231"}
    periodic = [{"period_end": date(2026, 6, 30), "fiscal_period": "H1", "fiscal_year": 2026, "ts": None,
                 "kdate": date(2026, 8, 28), "source_id": "akshare", "form_type": "CN_PERIODIC", "filing_id": "f1",
                 "url": None}]
    releases = [{"ts": None, "kdate": date(2026, 10, 27), "source_id": "cninfo", "form_type": "PE 2026-09-30",
                 "filing_id": "cninfo:S1:2026-10-27", "url": "u", "fiscal_period": None, "period_end": date(2026, 9, 30)}]
    provider = [{"event_date": date(2026, 10, 28), "event_ts": None}]   # Yahoo one day later: the same event
    evs = E.build_issuer_events("I1", m, periodic, releases, provider, [])
    q3 = next(e for e in evs if e["period_end"] == date(2026, 9, 30))
    assert q3["date_source"] == "official_release" and q3["release_date"] == date(2026, 10, 27)
    assert q3["source_label"].startswith("CNINFO") and q3["provider_date"] == date(2026, 10, 28)


# ------------------------------------------------------------------------------------------------ Yahoo due rules


def _ann(sid: str, d: date, fetched: datetime) -> dict:
    return {"source_id": "yahoo", "security_id": sid, "issuer_id": "I" + sid, "event_date": d, "event_ts": None,
            "time_known": False, "period_hint": None, "provider_symbol": sid, "knowledge_date": fetched.date(),
            "fetched_at": fetched}


def test_yahoo_due_rules(tmp_data, monkeypatch):
    now = C.now_utc()
    today = now.date()
    sids = ["NEAR", "NEARFRESH", "NEW", "OLD", "FAR", "FARFRESH", "EMPTY", "EMPTYOLD"]
    um = pl.DataFrame({"universe_id": ["full"] * len(sids), "security_id": sids, "valid_from": [date(2020, 1, 1)] * len(sids),
                       "valid_to": [None] * len(sids), "rank_at_entry": [1] * len(sids), "reason": ["t"] * len(sids),
                       "updated_at": [now] * len(sids)})
    from app.core.storage import write_table

    write_table(um, "universe_membership", settings=tmp_data)
    meta = pl.DataFrame({"security_id": sids, "issuer_id": ["I" + x for x in sids], "yf_ticker": sids,
                         "timezone": ["America/New_York"] * len(sids), "region": ["US"] * len(sids)})
    rows = [_ann("NEAR", today + timedelta(days=3), now - timedelta(days=2)),
            _ann("NEARFRESH", today + timedelta(days=3), now - timedelta(hours=3)),
            _ann("OLD", today - timedelta(days=60), now - timedelta(days=20)),
            _ann("FAR", today + timedelta(days=80), now - timedelta(days=20)),
            _ann("FARFRESH", today + timedelta(days=80), now - timedelta(days=40))]
    C.write(pl.DataFrame(rows), "earnings_announcements", tmp_data, mode="upsert")
    F.write_fetch_log(tmp_data, {"EMPTY": (now - timedelta(days=10)).isoformat(),
                                 "EMPTYOLD": (now - timedelta(days=40)).isoformat()})
    got = [t["security_id"] for t in F._yahoo_targets(tmp_data, meta, ["full"], 14)]
    # near first, then never fetched, then stale (14 d; 30 d when the next date is far), empty answers after 30 d
    assert got == ["NEAR", "NEW", "FARFRESH", "OLD", "EMPTYOLD"]   # stale ones oldest first


# ------------------------------------------------------------------------------------------------ seasonal RW


def _task(vers: list[tuple[date, date, float, str]], targets: list[dict]) -> dict:
    return {"issuer_id": "I1", "months": 6, "series": {"revenue": vers}, "eps_basis": None, "splits": [],
            "targets": targets, "currency": "JPY", "fpath": {}}


def test_seasonal_rw_uses_the_prior_year_comparative_of_the_release():
    # TDnet-like history: only the release of H1 2026 (2026-08-10) carrying the H1 2025 comparative
    vers = [(date(2026, 6, 30), date(2026, 8, 10), 120.0, "reported"), (date(2025, 6, 30), date(2026, 8, 10), 100.0, "reported")]
    tgt = [{"period_end": date(2026, 6, 30), "fiscal_period": "H1", "fiscal_year": 2027, "release_date": date(2026, 8, 10),
            "origin": date(2026, 8, 9)}]
    rows = X.issuer_targets(_task(vers, tgt))
    assert len(rows) == 1 and rows[0]["model"] == "seasonal_rw"
    assert rows[0]["expectation"] == 100.0 and rows[0]["actual"] == 120.0
    assert "comparative" in rows[0]["model_spec"] and rows[0]["sigma"] is None
    out = X.finish(pl.DataFrame(rows))
    r = out.row(0, named=True)
    assert r["is_primary"] and abs(r["surprise_pct"] - 0.2) < 1e-12 and r["surprise_z"] is None


def test_seasonal_rw_prefers_the_value_known_at_the_origin():
    vers = [(date(2025, 6, 30), date(2025, 8, 12), 90.0, "reported"),        # public a year earlier
            (date(2025, 6, 30), date(2026, 8, 10), 100.0, "reported"),       # restated comparative
            (date(2026, 6, 30), date(2026, 8, 10), 120.0, "reported")]
    tgt = [{"period_end": date(2026, 6, 30), "fiscal_period": "H1", "fiscal_year": 2027, "release_date": date(2026, 8, 10),
            "origin": date(2026, 8, 9)}]
    rows = X.issuer_targets(_task(vers, tgt))
    assert rows[0]["expectation"] == 90.0 and rows[0]["model_spec"] == X.SPEC_RW


def test_seasonal_rw_needs_a_year_ago_value():
    vers = [(date(2026, 6, 30), date(2026, 8, 10), 120.0, "reported")]
    tgt = [{"period_end": date(2026, 6, 30), "fiscal_period": "H1", "fiscal_year": 2027, "release_date": date(2026, 8, 10),
            "origin": date(2026, 8, 9)}]
    assert X.issuer_targets(_task(vers, tgt)) == []


# ------------------------------------------------------------------------------------------------ API reasons


def test_earnings_availability_specific_reasons():
    from app.api.routers.earnings import earnings_availability

    none = earnings_availability("HK", True, False, [], [], [], 0)
    assert none["history"]["code"] == "insufficient_data" and none["next"]["code"] == "insufficient_data"
    assert none["reactions"] is None                                      # nothing released -> no reaction expected
    assert none["expectations"]["code"] in ("insufficient_data", "no_source", "needs_key", "source_unavailable")
    hist = [{"car_m1_p1": None}]
    some = earnings_availability("US", False, True, hist, [], [], 0)
    assert some["history"] is None and some["next"]["code"] == "not_yet_published"
    assert some["reactions"]["code"] == "no_source"
    assert "same period a year earlier" in some["expectations"]["message"]
    full = earnings_availability("US", True, True, hist, [{"x": 1}], [{"y": 1}], 3)
    assert all(v is None for v in full.values())
    assert earnings_availability("US", True, True, hist, [], [{"y": 1}], 3, active=False)["next"]["code"] == "not_applicable"


def test_release_sources_are_contract_values():
    from app.core.schema import get_contract

    c = get_contract("earnings_announcements")
    vals = set(c.column("source_id").values or [])
    assert {"hkex_bmn", "cninfo", "fca_nsm"} <= vals and set(E.RELEASE_SOURCES) <= vals
    assert "event_kind" in c.column_names and "url" in c.column_names
    assert "seasonal_rw" in (get_contract("earnings_expectations").column("model").values or [])


def test_deadline_projection_for_issuers_without_history():
    m = {"region": "CN", "security_id": "S1", "country": "CN", "sector_id": "INDU", "fiscal_year_end_mmdd": "1231"}
    rows = E.project_deadline("I1", m, date(2026, 11, 5))
    # Q3 (due 10-30) is more than 3 days past -> the annual report, due 120 days after 12-31
    assert rows[0]["period_end"] == date(2026, 12, 31) and rows[0]["expected_date"] == date(2027, 4, 30)
    assert rows[0]["date_source"] == "model_projection" and rows[0]["source_id"] == "deadline_model"
    assert rows[0]["window_lo"] == date(2027, 3, 31) and rows[0]["n_history"] == 0
    hk = E.project_deadline("I2", m | {"region": "HK", "fiscal_year_end_mmdd": "03-31"}, date(2026, 10, 1))
    assert hk[0]["period_end"] == date(2026, 9, 30) and hk[0]["expected_date"] == date(2026, 11, 30)
    assert E.project_deadline("I3", m | {"region": "OTHER"}, date(2026, 10, 1)) == []


def test_release_wins_over_a_same_day_schedule_row():
    base = {"source_id": "fca_nsm", "security_id": "S1", "issuer_id": "I1", "event_date": date(2026, 11, 12)}
    df = A._frame([base | {"event_kind": "release", "time_known": True}, base | {"event_kind": "schedule"}])
    assert df.height == 1 and df.item(0, "event_kind") == "release"
