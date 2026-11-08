"""SEC acceptance-time fix (workstream B): EDGAR submissions acceptanceDateTime is late by the US/Eastern offset once a
filing is settled. Rule accuracy on recorded pairs (truth = Atom feed / SGML header), known cases, truth override,
the parsers that use it, and the one-time backfill of stored rows (idempotent)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path


from app.ingestion import sec_acceptance as SA

FIX = Path(__file__).parent / "fixtures" / "sec_acceptance_pairs.json"


def _pairs() -> list[dict]:
    return json.loads(FIX.read_text(encoding="utf-8"))["pairs"]


def test_rule_accuracy_on_recorded_pairs():
    ok = bad = old_bad = 0
    for p in _pairs():
        truth = datetime.fromisoformat(p["truth"])
        got, _m = SA.correct_acceptance(p["raw"], p["form"], date.fromisoformat(p["filing_date"]),
                                        date.fromisoformat(p["landing_date"]))
        ok += got == truth
        bad += got != truth
        old_bad += SA.to_utc(p["raw"]) != truth          # the old face-value parser
    n = ok + bad
    assert n == 734
    assert ok / n >= 0.93, (ok, n)                       # measured 95%
    assert old_bad / n > 0.70                            # face value was wrong for most stored rows


def test_known_cases():
    # Apple 8-K Item 2.02 of 30 Jul 2026 (16:30 ET) as served by the submissions API after settling
    t, m = SA.correct_acceptance("2026-07-31T00:30:28.000Z", "8-K", date(2026, 7, 30), date(2026, 10, 24))
    assert t == datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC) and m == "shifted"
    # winter (EST): a 18:30 ET Form 4 reads +5 h
    t, m = SA.correct_acceptance("2025-11-15T04:30:15.000Z", "4", date(2025, 11, 14), date(2026, 10, 24))
    assert t == datetime(2025, 11, 14, 23, 30, 15, tzinfo=UTC) and m == "shifted"
    # fetched the same day and plausible as is -> kept (bank pre-market 8-K 06:45 ET = 10:45Z)
    t, m = SA.correct_acceptance("2026-10-23T10:45:00.000Z", "8-K", date(2026, 10, 23), date(2026, 10, 23))
    assert t == datetime(2026, 10, 23, 10, 45, tzinfo=UTC) and m == "as_is"
    # a 22:14Z (18:14 ET) value cannot be right for a settled record: 02:14Z is outside EDGAR hours as-is
    t, m = SA.correct_acceptance("2026-10-08T02:14:25.000Z", "4", date(2026, 10, 7), date(2026, 10, 7))
    assert t == datetime(2026, 10, 7, 22, 14, 25, tzinfo=UTC) and m == "shifted"
    # Schedule 13D/A at 18:20 ET keeps the same filing date (22:00 ET cutoff), so as-is stays plausible when fresh
    t, m = SA.correct_acceptance("2026-10-23T22:20:00.000Z", "SCHEDULE 13D/A", date(2026, 10, 23), date(2026, 10, 24))
    assert m == "as_is" and t == datetime(2026, 10, 23, 22, 20, tzinfo=UTC)
    assert SA.correct_acceptance(None) == (None, "none")


def test_truth_wins_and_parse_wrappers():
    truth = datetime(2026, 10, 24, 0, 0, 6, tzinfo=UTC)
    assert SA.correct_acceptance("2026-10-23T20:00:06.000Z", "4", date(2026, 10, 23), date(2026, 10, 24),
                                 truth) == (truth, "truth")
    from app.ingestion.sec_index import parse_acceptance_utc

    assert parse_acceptance_utc("2026-07-31T00:30:28.000Z", "8-K", "2026-07-30", "2026-10-24") == \
        datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)
    # no context = long settled (older pages / unknown fetch day)
    assert parse_acceptance_utc("2024-11-01T00:30:25.000Z") == datetime(2024, 10, 31, 20, 30, 25, tzinfo=UTC)


def test_sec_history_rows_use_landing_day():
    from app.earnings import sec_history as SH

    block = {"accessionNumber": ["0000320193-26-000071", "0000320193-26-000080"], "form": ["8-K", "8-K"],
             "acceptanceDateTime": ["2026-07-31T00:30:28.000Z", "2026-10-23T20:31:00.000Z"],
             "filingDate": ["2026-07-30", "2026-10-23"], "reportDate": ["", ""], "items": ["2.02,9.01", "2.02"],
             "primaryDocument": ["a.htm", "b.htm"]}
    rows = SH._rows(block, "0000320193", fetched_on=date(2026, 10, 23))
    assert rows[0]["acceptance_ts"] == datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)   # settled -> shifted
    assert rows[1]["acceptance_ts"] == datetime(2026, 10, 23, 20, 31, tzinfo=UTC)       # same-day fetch -> as is
    assert SH.landing_date(Path("x/dt=2026-10-24/CIK0000320193.gz")) == date(2026, 10, 24)


def _fi_row(acc: str, ts: datetime, form: str, kd: date, ingested: datetime) -> dict:
    return {"filing_id": f"sec_edgar:{acc}", "source_id": "sec_edgar", "year": kd.year, "source_entity_id": "0000320193",
            "issuer_id": None, "form_type": form, "form_class": "current_report", "period_end": None,
            "fiscal_year": None, "fiscal_period": None, "knowledge_date": kd, "knowledge_ts": ts,
            "is_amendment": False, "amends_filing_id": None, "language": "en", "url": "https://www.sec.gov/x",
            "doc_path": None, "has_xbrl": True, "items": ["2.02"], "ingested_at": ingested}


def test_backfill_filings_index_once(tmp_data):
    from app.core.storage import read_table, write_table
    from app.ingestion.sec_index import filings_frame

    ing = datetime(2026, 10, 24, 7, 0, tzinfo=UTC)
    rows = [_fi_row("0000320193-26-000071", datetime(2026, 7, 31, 0, 30, 28, tzinfo=UTC), "8-K", date(2026, 7, 30), ing),
            _fi_row("0000320193-26-000090", datetime(2026, 10, 23, 14, 0, tzinfo=UTC), "8-K", date(2026, 10, 23), ing),
            _fi_row("0000320193-26-000091", datetime(2026, 10, 23, 20, 0, 6, tzinfo=UTC), "4", date(2026, 10, 23), ing)]
    write_table(filings_frame(rows), "filings_index", mode="upsert", settings=tmp_data)
    # the Atom feed saw accession 91 at 20:00:06 ET = 00:00:06Z (the stored value is the ET wall clock)
    from app.realtime.bus import SqliteBus

    bus = SqliteBus(settings=tmp_data)
    bus.publish("filings", "sec_edgar:0000320193-26-000091", {"accession": "0000320193-26-000091", "form_type": "4"},
                type="filing", ts_source=datetime(2026, 10, 24, 0, 0, 6, tzinfo=UTC))
    bus.close()

    dry = SA.backfill(tmp_data, apply=False, tables=["filings_index"])
    t = dry["tables"]["filings_index"]
    assert t["status"] == "dry run" and t["rows_corrected"] == 2 and t["methods"]["truth"] == 1
    rep = SA.backfill(tmp_data, apply=True, tables=["filings_index"])
    assert rep["tables"]["filings_index"]["rows_corrected"] == 2
    got = {r["filing_id"][-6:]: r["knowledge_ts"] for r in read_table("filings_index", settings=tmp_data).iter_rows(named=True)}
    assert got["000071"] == datetime(2026, 7, 30, 20, 30, 28, tzinfo=UTC)
    assert got["000090"] == datetime(2026, 10, 23, 14, 0, tzinfo=UTC)          # fresh + plausible: unchanged
    assert got["000091"] == datetime(2026, 10, 24, 0, 0, 6, tzinfo=UTC)
    # idempotent: a second run does nothing; --force only touches rows ingested after the recorded cutoff
    assert SA.backfill(tmp_data, apply=True, tables=["filings_index"])["tables"]["filings_index"]["status"] == "already applied"
    again = SA.backfill(tmp_data, apply=True, force=True, tables=["filings_index"])
    assert again["tables"]["filings_index"]["rows_corrected"] == 0
    got2 = {r["filing_id"][-6:]: r["knowledge_ts"] for r in read_table("filings_index", settings=tmp_data).iter_rows(named=True)}
    assert got2 == got
    assert SA.read_state(tmp_data)["tables"]["filings_index"]["version"] == SA.FIX_VERSION


def test_candidates_and_plausibility():
    raw = datetime(2026, 7, 31, 0, 30, 28, tzinfo=UTC)
    c = SA.candidates(raw)
    assert c["shifted"] == raw - timedelta(hours=4) and c["et_wall"] == raw + timedelta(hours=4)
    assert not SA.plausible(datetime(2026, 10, 24, 15, 0, tzinfo=UTC), "8-K", None)      # Saturday
    assert not SA.plausible(datetime(2026, 10, 23, 4, 0, tzinfo=UTC), "8-K", None)       # 00:00 ET
    assert SA.plausible(datetime(2026, 10, 23, 22, 0, tzinfo=UTC), "8-K", date(2026, 10, 26))  # 18:00 ET Fri -> Mon
    assert SA.plausible(datetime(2026, 10, 23, 22, 0, tzinfo=UTC), "4", date(2026, 10, 23))    # Form 4 until 22:00


def test_per_file_vote():
    # a file that serves RIGHT times (pre-market 8-Ks at 06:30 / 07:00 ET read as is; shifted they would be 02:30 ET)
    right = {"acceptanceDateTime": ["2024-11-07T11:30:00.000Z", "2024-08-07T11:00:00.000Z", "2024-11-07T21:03:32.000Z"],
             "form": ["8-K", "8-K", "8-K"], "filingDate": ["2024-11-07", "2024-08-07", "2024-11-07"]}
    assert SA.block_vote(right) is False
    # its settled 16:03 ET 8-K stays as is (the age rule alone would have shifted it to 12:03 ET)
    t, m = SA.correct_acceptance("2024-11-07T21:03:32.000Z", "8-K", date(2024, 11, 7), date(2026, 10, 24),
                                 file_shifted=SA.block_vote(right))
    assert t == datetime(2024, 11, 7, 21, 3, 32, tzinfo=UTC) and m == "as_is"
    # a late file (after-close releases read 00:30Z next day: implausible as is)
    late = {"acceptanceDateTime": ["2026-07-31T00:30:28.000Z", "2025-10-31T00:30:35.000Z", "2026-07-31T14:01:02.000Z"],
            "form": ["8-K", "8-K", "10-Q"], "filingDate": ["2026-07-30", "2025-10-30", "2026-07-31"]}
    assert SA.block_vote(late) is True
    assert SA.vote([]) is None


def test_sec_index_records_carry_file_vote():
    from app.ingestion.sec_index import submissions_recent_by_acc

    recs = submissions_recent_by_acc({"filings": {"recent": {
        "accessionNumber": ["a1", "a2"], "acceptanceDateTime": ["2026-07-31T00:30:28.000Z", "2025-10-31T00:30:35.000Z"],
        "form": ["8-K", "8-K"], "filingDate": ["2026-07-30", "2025-10-30"]}}})
    assert recs["a1"]["_file_late"] is True and recs["a1"]["form"] == "8-K"


def test_resolve_with_landed_file_reference():
    late_raw = datetime(2026, 4, 2, 0, 5, 19, tzinfo=UTC)          # latest landing: late by 4 h
    right = datetime(2026, 4, 1, 20, 5, 19, tzinfo=UTC)
    land = date(2026, 10, 24)
    # stored before the file went late (Horizon Bancorp case): kept
    assert SA.resolve(right, "8-K", date(2026, 4, 1), date(2026, 4, 1), None, True, (late_raw, land)) == (right, "as_is_ref")
    # stored late, the file now serves the right value: take it
    assert SA.resolve(late_raw, "8-K", date(2026, 4, 1), date(2026, 10, 10), None, None, (right, land)) == (right, "ref")
    # same value as the file: the vote applies (late file -> shifted)
    assert SA.resolve(late_raw, "8-K", date(2026, 4, 1), date(2026, 10, 10), None, True, (late_raw, land))[0] == right
    # a right file (vote False) keeps an ambiguous settled value
    v = datetime(2024, 11, 7, 21, 3, 32, tzinfo=UTC)
    assert SA.resolve(v, "8-K", date(2024, 11, 7), date(2026, 10, 10), None, False, (v, land)) == (v, "as_is")
