"""Data-quality rules against small lake fixtures written into a temp data dir."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.core import dq
from app.core.schema import get_contract
from app.core.storage import read_table, write_table
from app.scheduler.base import JobContext


def _fx(rows, ingested=None):
    ing = ingested or datetime.now(UTC)
    return pl.DataFrame({
        "date": [r[0] for r in rows], "year": [r[0].year for r in rows], "currency": [r[1] for r in rows],
        "usd_per_unit": [r[2] for r in rows], "units_per_usd": [1 / r[2] if r[2] else None for r in rows],
        "source_id": ["ecb"] * len(rows), "ingested_at": [ing] * len(rows),
    })


def _prices(rows):
    """rows: (security_id, date, close, currency)"""
    n = len(rows)
    return pl.DataFrame({
        "security_id": [r[0] for r in rows], "listing_id": ["L" + r[0] for r in rows], "date": [r[1] for r in rows],
        "region": ["US"] * n, "year": [r[1].year for r in rows], "open": [r[2] for r in rows],
        "high": [r[2] * 1.01 for r in rows], "low": [r[2] * 0.99 for r in rows], "close": [r[2] for r in rows],
        "adj_factor": [1.0] * n, "split_factor": [1.0] * n, "adj_close": [r[2] for r in rows],
        "currency": [r[3] for r in rows], "close_usd": [r[2] for r in rows], "source_id": ["polygon"] * n,
        "ingested_at": [datetime.now(UTC)] * n,
    })


def _by(outcomes, table, check):
    return [o for o in outcomes if o.table_name == table and o.check_id == check]


def test_empty_tables_are_info_only(tmp_data):
    out = dq.run_checks(dq.default_checks(tables=["fx_rates", "prices_daily"]), "R1", write=False)
    assert {o.check_id for o in out} == {"has_data"}
    assert all(o.severity == "info" for o in out) and dq.blocking_failures(out) == []


def test_pk_duplicates_and_value_range_errors(tmp_data):
    today = date.today()
    write_table(_fx([(today, "EUR", 1.1)]), "fx_rates")
    write_table(_fx([(today, "EUR", 1.2)]), "fx_rates", mode="append")  # same PK twice -> duplicate
    write_table(_fx([(today, "JPY", -1.0)]), "fx_rates", mode="append")
    out = dq.run_checks(dq.default_checks(tables=["fx_rates"]), "R1", write=False)
    pk = _by(out, "fx_rates", "pk_unique")[0]
    assert not pk.passed and pk.severity == "error" and pk.metric_value == 1 and pk.details["sample"]
    rng = _by(out, "fx_rates", "value_range:usd_per_unit")[0]
    assert not rng.passed and rng.severity == "error"
    assert _by(out, "fx_rates", "schema_conformance")[0].passed
    assert _by(out, "fx_rates", "freshness")[0].passed


def test_freshness_stale_and_row_count_delta(tmp_data):
    old = datetime.now(UTC) - timedelta(days=20)
    write_table(_fx([(date(2026, 1, 2), "EUR", 1.1), (date(2026, 1, 2), "GBP", 1.3)], ingested=old), "fx_rates")
    out1 = dq.run_checks(dq.default_checks(tables=["fx_rates"]), "R1")
    f = _by(out1, "fx_rates", "freshness")[0]
    assert not f.passed and f.severity == "error" and f.metric_value > 19  # > 3x the 4-day nightly cadence
    assert _by(out1, "fx_rates", "row_count")[0].metric_value == 2
    assert not _by(out1, "fx_rates", "row_count_delta")  # no baseline yet
    write_table(_fx([(date(2026, 1, d), c, 1.0) for d in range(5, 9) for c in ("EUR", "GBP")]), "fx_rates")
    out2 = dq.run_checks(dq.default_checks(tables=["fx_rates"]), "R2")
    delta = _by(out2, "fx_rates", "row_count_delta")[0]
    assert delta.metric_value == 4.0 and not delta.passed and delta.severity == "warn"
    assert _by(out2, "fx_rates", "freshness")[0].passed  # new rows are fresh


def test_price_rules_and_fx_coverage(tmp_data):
    d0 = date.today() - timedelta(days=3)
    rows = [("S1", d0, 100.0, "USD"), ("S1", d0 + timedelta(days=1), 250.0, "USD"),  # +150% jump
            ("S2", d0, 50.0, "EUR"), ("S2", d0 + timedelta(days=1), 51.0, "EUR")]
    write_table(_prices(rows), "prices_daily")
    write_table(_fx([(d0 - timedelta(days=30), "JPY", 0.007)]), "fx_rates")  # EUR has no official FX at all
    out = dq.run_checks(dq.default_checks(tables=["prices_daily", "fx_rates"]), "R0", write=False)
    assert _by(out, "fx_rates", "fx_coverage")[0].passed  # unsupported currency is not an error ...
    unsup = _by(out, "fx_rates", "fx_unsupported_currencies")[0]
    assert not unsup.passed and unsup.metric_value == 1 and unsup.severity == "warn"  # ... but a warning
    write_table(_fx([(d0 - timedelta(days=30), "EUR", 1.1)]), "fx_rates")  # supported, but stale in window
    out = dq.run_checks(dq.default_checks(tables=["prices_daily", "fx_rates"]), "R1", write=False)
    jump = _by(out, "prices_daily", "value_range:daily_return_gt_80pct")[0]
    assert not jump.passed and jump.details["violations"] == 1
    assert _by(out, "prices_daily", "value_range:ohlc_inconsistent")[0].passed
    cov = _by(out, "fx_rates", "fx_coverage")[0]
    assert not cov.passed and cov.metric_value == 1 and cov.severity == "error"
    write_table(_fx([(d0, "EUR", 1.1), (d0 + timedelta(days=1), "EUR", 1.1)]), "fx_rates")
    out = dq.run_checks(dq.default_checks(tables=["prices_daily", "fx_rates"]), "R2", write=False)
    assert _by(out, "fx_rates", "fx_coverage")[0].passed


def test_null_rate_baseline_and_custom_hook(tmp_data):
    d0 = date.today()
    write_table(_prices([("S1", d0, 10.0, "USD"), ("S2", d0, 20.0, "USD")]), "prices_daily")
    calls = []

    def my_hook(ctx):
        calls.append(ctx.run_id)
        return [dq.DqOutcome("prices_daily", "custom_hook", "warn", False, 1.0, 0.0)]

    dq.register_hook("my_hook", my_hook)
    checks = [dq.NullRate("prices_daily", "close_usd"),
              dq.NullRate("prices_daily", "close_usd", max_rate=0.0, check_id="null_rate_abs"),
              dq.HookCheck("my_hook", "prices_daily")]
    out = dq.run_checks(checks, "RH")
    assert calls == ["RH"]
    assert _by(out, "prices_daily", "null_rate:close_usd")[0].details == {"baseline": "none yet"}
    assert _by(out, "prices_daily", "custom_hook")[0].passed is False
    nulls = _prices([("S3", d0, 5.0, "USD"), ("S4", d0, 6.0, "USD")]).with_columns(pl.lit(None, pl.Float64).alias("close_usd"))
    write_table(nulls, "prices_daily")
    out2 = dq.run_checks(checks, "RH2", write=False)
    nr = _by(out2, "prices_daily", "null_rate:close_usd")[0]
    assert not nr.passed and nr.metric_value == 0.5 and nr.details["baseline"] == 0.0
    assert not _by(out2, "prices_daily", "null_rate_abs")[0].passed


def test_accounting_identity_hook(tmp_data):
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE fundamentals_wide AS SELECT * FROM (VALUES (100.0, 60.0, 40.0), (100.0, 50.0, 30.0)) "
                "t(total_assets, total_liabilities, total_equity)")
    ctx = dq.DqContext("RA", tmp_data, con=con)
    ctx._has["fundamentals_wide"] = True
    out = dq.accounting_identity_hook(ctx)
    assert out[0].metric_value == 0.5 and not out[0].passed


def test_summary_and_job_entry_point(tmp_data):
    today = date.today()
    write_table(_fx([(today, "EUR", 1.1)]), "fx_rates")
    write_table(_fx([(today, "EUR", 1.2)]), "fx_rates", mode="append")
    ctx = JobContext(job_id="dq_checks", run_id="RJOB", as_of=today, params={"tables": ["fx_rates", "prices_daily"]},
                     settings=tmp_data)
    res = dq.run(ctx)
    assert res.status == "failed" and res.retryable is False and "pk_unique" in res.message
    stored = read_table("dq_results")
    assert set(stored["run_id"].to_list()) == {"RJOB"}
    summ = dq.dq_summary()
    assert summ["run_id"] == "RJOB" and summ["blocking"] is True
    assert summ["tables"]["fx_rates"] == "error" and summ["tables"]["prices_daily"] == "no_data"
    assert any(f["check_id"] == "pk_unique" for f in summ["failed"])


# ------------------------------------------------------------------------------------------------ date sanity (A5)

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


def _insider(rows):
    """rows: (txn_id, transaction_date, knowledge_date) -> conformed insider_transactions frame."""
    from app.core.schema import conform

    n = len(rows)
    df = pl.DataFrame({
        "txn_id": [r[0] for r in rows], "region": ["US"] * n, "year": [r[2].year for r in rows],
        "filing_id": ["sec_edgar:" + r[0] for r in rows], "source_id": ["sec_edgar"] * n,
        "source_entity_id": ["0000000001"] * n, "insider_id": ["sec_edgar:1"] * n, "insider_name": ["A"] * n,
        "is_director": [True] * n, "is_officer": [False] * n, "is_ten_pct_owner": [False] * n,
        "transaction_date": [r[1] for r in rows], "knowledge_date": [r[2] for r in rows], "txn_type": ["sell"] * n,
        "is_open_market": [True] * n, "is_derivative": [False] * n, "shares": [100.0] * n, "is_amendment": [False] * n,
        "ingested_at": [NOW] * n,
    })
    return conform(df, "insider_transactions")


def test_date_sanity_insider_typos_warn_with_examples(tmp_data):
    """The 2026-11 lake: Form 4 dates up to 2047 and two-digit years (0016) - certainly wrong, but a handful among many
    rows is a warning with counts and examples, never a blocking error."""
    ok = [(f"T{i}", date(2026, 10, 1), date(2026, 10, 3)) for i in range(200)]
    bad = [("PEGA", date(2047, 6, 7), date(2017, 6, 9)), ("CWT", date(16, 3, 16), date(2016, 3, 18)),
           ("EARLY", date(2026, 10, 20), date(2026, 10, 10))]       # 10 days after its filing date
    write_table(_insider(ok + bad), "insider_transactions")
    out = dq.run_checks(dq.default_checks(tables=["insider_transactions"]), "RD", write=False,
                        ctx=dq.DqContext("RD", tmp_data, now=NOW, tables=["insider_transactions"]))
    fut = _by(out, "insider_transactions", "date_future:transaction_date")[0]
    assert not fut.passed and fut.severity == "warn" and fut.threshold == 0.0
    assert fut.details["violations"] == 1 and fut.details["certainly_wrong"] == 1 and fut.details["max"] == "2047-06-07"
    ex = fut.details["examples"]
    assert ex[0] == ["txn_id", "transaction_date"] and ex[1] == ["PEGA", "2047-06-07"]
    old = _by(out, "insider_transactions", "date_too_old:transaction_date")[0]
    assert not old.passed and old.severity == "warn" and old.details["violations"] == 1
    assert old.details["min"] == "0016-03-16" and old.details["impossible_before"] == "1900-01-01"
    kb = _by(out, "insider_transactions", "knowledge_before:transaction_date")[0]
    assert not kb.passed and kb.severity == "warn" and kb.details["violations"] == 2   # PEGA + EARLY (1-day tolerance)
    assert {r[0] for r in kb.details["examples"][1:]} == {"PEGA", "EARLY"}
    assert _by(out, "insider_transactions", "date_future:knowledge_date")[0].passed      # generic rule
    assert _by(out, "insider_transactions", "date_future:ingested_at")[0].passed
    assert not [o for o in dq.blocking_failures(out) if o.check_id.startswith(("date_", "knowledge_before"))]


def _ctx_with(tmp_data, table, ddl):
    import duckdb

    con = duckdb.connect()
    con.execute(f"CREATE TABLE {table} AS {ddl}")
    ctx = dq.DqContext("RX", tmp_data, con=con, now=NOW)
    ctx._has[table] = True
    return ctx


def test_date_sanity_error_only_when_certain_and_systemic(tmp_data):
    # a parse bug: 30 of 100 transaction dates in 2047 -> certainly wrong AND systemic -> blocking error
    ctx = _ctx_with(tmp_data, "insider_transactions",
                    "SELECT 'T' || i AS txn_id, CASE WHEN i < 30 THEN DATE '2047-06-07' ELSE DATE '2026-10-01' END "
                    "AS transaction_date, DATE '2026-10-03' AS knowledge_date FROM range(100) r(i)")
    rules, order = dq.date_rules_for(get_contract("insider_transactions"))
    out = dq.DateSanity("insider_transactions", rules, order).run(ctx)
    fut = _by(out, "insider_transactions", "date_future:transaction_date")[0]
    assert not fut.passed and fut.severity == "error" and fut.metric_value == pytest.approx(0.3)
    assert len(fut.details["examples"]) == 1 + dq.DATE_EXAMPLES
    assert _by(out, "insider_transactions", "knowledge_before:transaction_date")[0].severity == "error"
    assert dq.blocking_failures(out)
    # fewer than DATE_ERROR_MIN_ROWS certainly-wrong rows stay a warning even at a high rate
    ctx = _ctx_with(tmp_data, "insider_transactions",
                    "SELECT 'T' || i AS txn_id, CASE WHEN i < 5 THEN DATE '2047-06-07' ELSE DATE '2026-10-01' END "
                    "AS transaction_date, DATE '2026-10-03' AS knowledge_date FROM range(20) r(i)")
    out = dq.DateSanity("insider_transactions", rules, order).run(ctx)
    assert _by(out, "insider_transactions", "date_future:transaction_date")[0].severity == "warn"
    # scheduled events are never certainly wrong: half of Polygon's dividends 3-6 years ahead -> warning only
    ctx = _ctx_with(tmp_data, "corporate_events",
                    "SELECT 'E' || i AS event_id, CASE WHEN i < 50 THEN DATE '2032-12-13' ELSE DATE '2028-06-01' END "
                    "AS event_date, CASE WHEN i < 50 THEN DATE '2032-12-13' ELSE DATE '2028-06-01' END AS knowledge_date "
                    "FROM range(100) r(i)")
    rules, order = dq.date_rules_for(get_contract("corporate_events"))
    out = dq.DateSanity("corporate_events", rules, order).run(ctx)
    for cid in ("date_future:event_date", "date_future:knowledge_date"):
        o = _by(out, "corporate_events", cid)[0]
        assert not o.passed and o.severity == "warn" and o.details["violations"] == 50 and "certainly_wrong" not in o.details


def test_date_sanity_tolerances_scopes_and_old_dates(tmp_data):
    # periodic reports cannot cover a future period; an 8-K "period" a few months ahead is within tolerance
    ctx = _ctx_with(tmp_data, "filings_index",
                    "SELECT * FROM (VALUES ('F1', 'annual', DATE '4172-12-31', DATE '2026-10-22'), "
                    "('F2', 'current_report', DATE '2027-01-31', DATE '2026-10-22'), "
                    "('F3', 'quarterly', DATE '2026-09-30', DATE '2026-10-30'), "
                    "('F4', 'annual', DATE '1312-12-31', DATE '2026-03-01')) t(filing_id, form_class, period_end, knowledge_date)")
    rules, order = dq.date_rules_for(get_contract("filings_index"))
    out = dq.DateSanity("filings_index", rules, order).run(ctx)
    per = _by(out, "filings_index", "date_future:period_end:periodic")[0]
    assert not per.passed and per.details["violations"] == 1 and per.details["rows"] == 3   # scope: periodic forms
    assert _by(out, "filings_index", "date_future:period_end:other_forms")[0].passed       # 400-day tolerance
    old = _by(out, "filings_index", "date_too_old:period_end:periodic")[0]
    assert not old.passed and old.details["examples"][1][0] == "F4"
    kb = _by(out, "filings_index", "knowledge_before:period_end")[0]
    assert kb.details["violations"] == 1 and "certainly_wrong" not in kb.details   # pit_violation stays the gate
    # TDnet forecasts (next fiscal year end) are within the 731-day period tolerance; year 4172 is not
    ctx = _ctx_with(tmp_data, "fundamentals_raw",
                    "SELECT * FROM (VALUES (DATE '2027-09-30', DATE '2026-10-01', DATE '2026-11-01'), "
                    "(DATE '4172-12-31', DATE '4172-01-01', DATE '2026-05-01'), (DATE '2025-12-31', DATE '0001-01-01', "
                    "DATE '2026-03-01')) t(period_end, period_start, knowledge_date)")
    rules, order = dq.date_rules_for(get_contract("fundamentals_raw"))
    out = dq.DateSanity("fundamentals_raw", rules, order).run(ctx)
    assert _by(out, "fundamentals_raw", "date_future:period_end")[0].details["violations"] == 1
    ps = _by(out, "fundamentals_raw", "date_too_old:period_start")[0]
    assert not ps.passed and ps.severity == "warn" and ps.details["certainly_wrong"] == 1
    # a missing column skips its rule instead of erroring
    ctx = _ctx_with(tmp_data, "short_interest", "SELECT DATE '2026-10-15' AS settlement_date")
    rules, order = dq.date_rules_for(get_contract("short_interest"))
    out = dq.DateSanity("short_interest", rules, order).run(ctx)
    assert {o.check_id for o in out} == {"date_future:settlement_date"} and out[0].passed


def test_date_rules_cover_event_tables_and_generic_timestamps():
    rules, order = dq.date_rules_for(get_contract("fx_rates"))
    assert {r.column for r in rules} == {"date", "ingested_at"} and all(r.certain for r in rules) and not order
    rules, _ = dq.date_rules_for(get_contract("corporate_events"))
    kd = next(r for r in rules if r.column == "knowledge_date")
    assert not kd.certain and kd.future_days == 1830          # scheduled events: knowledge_date = event date
    rules, order = dq.date_rules_for(get_contract("insider_transactions"))
    assert {r.column for r in rules} >= {"transaction_date", "knowledge_date", "knowledge_ts", "ingested_at"}
    assert [(o.event_col, o.tolerance_days, o.certain) for o in order] == [("transaction_date", 1, True)]
    checks = dq.default_checks(tables=["insider_transactions", "fx_rates"])
    assert {c.table for c in checks if isinstance(c, dq.DateSanity)} == {"insider_transactions", "fx_rates"}


def _runs(*rows):
    return pl.DataFrame([{"job_id": j, "status": s, "error_type": e, "started_at": t, "finished_at": t}
                         for j, s, e, t in rows],
                        schema={"job_id": pl.Utf8, "status": pl.Utf8, "error_type": pl.Utf8,
                                "started_at": pl.Datetime("us", "UTC"), "finished_at": pl.Datetime("us", "UTC")})


def test_post_publish_due_follows_the_nightly_cadence():
    h = lambda n: NOW - timedelta(hours=n)  # noqa: E731
    assert dq.post_publish_due(None, NOW)[0]
    assert dq.post_publish_due(_runs(("qa_regression", "success", None, h(1))), NOW)[0]
    due, why = dq.post_publish_due(_runs(("dq_checks", "success", None, h(3))), NOW)
    assert not due and "at most every 20 h" in why
    # errors found = a complete run (JobReportedFailure): not re-run within the interval either
    assert not dq.post_publish_due(_runs(("dq_checks", "failed", "JobReportedFailure", h(5))), NOW)[0]
    due, why = dq.post_publish_due(_runs(("dq_checks", "success", None, h(30))), NOW)
    assert due and "last complete run" in why
    # an attempt without results (timeout / OOM) is retried, but not by every 30-min sync
    runs = _runs(("dq_checks", "success", None, h(30)), ("dq_checks", "failed", "TimeoutError", h(2)))
    due, why = dq.post_publish_due(runs, NOW)
    assert not due and "TimeoutError" in why
    runs = _runs(("dq_checks", "success", None, h(30)), ("dq_checks", "failed", "OOM", h(7)))
    assert dq.post_publish_due(runs, NOW)[0]
    # the last complete run on 10-31 21:53, a sync publishing on 11-06: due
    assert dq.post_publish_due(_runs(("dq_checks", "success", None, datetime(2026, 10, 31, 21, 53, tzinfo=UTC))), NOW)[0]


def test_post_publish_due_after_a_deferral_retries_within_the_hour():
    """A run that never started (the sync's bounded admission deferred it: heavy slot held by a big job, RAM floor,
    start budget) is not an attempt without results: the next publishing sync after 1 h tries again, not after 6 h."""
    h = lambda n: NOW - timedelta(hours=n)  # noqa: E731
    for et in ("HeavySlotBusy", "StartBudget", "RamFloor"):
        runs = _runs(("dq_checks", "success", None, h(30)), ("dq_checks", "skipped", et, h(0.5)))
        due, why = dq.post_publish_due(runs, NOW)
        assert not due and et in why and f"next try {dq.POST_PUBLISH_DEFER_RETRY_H:g} h" in why
        runs = _runs(("dq_checks", "success", None, h(30)), ("dq_checks", "skipped", et, h(1.5)))
        assert dq.post_publish_due(runs, NOW)[0]
    assert dq.POST_PUBLISH_DEFER_RETRY_H < dq.POST_PUBLISH_RETRY_H
