"""W3 jobs and ops rules: publication-time expectations (published_dates), the resumable curve backfill with
point-in-time knowledge dates, company_debt derivation (SEC tags, ratios, walls) - no network."""

from __future__ import annotations

from datetime import UTC, date, datetime

import polars as pl
import pytest

from app.rates import company_debt as CD
from app.rates import curves_job as CJ
from app.rates import ops_checks as OC
from app.rates import registry as R
from app.rates.common import curve_rows
from app.rates.sources import Loaded


# ------------------------------------------------------------------------------------------------ published_dates

def test_expected_daily_rule_with_lag_and_publication_time():
    r = {**OC.DEFAULT_RULE, "cal": "weekdays", "tz": "Europe/Berlin", "at": "12:30", "lag": 1}
    # Monday 2026-11-02 11:00 CET: Friday's curve publishes Monday 12:30 -> Thursday is the latest public date
    exp, nxt = OC.expected(r, datetime(2026, 11, 2, 10, 0, tzinfo=UTC))
    assert exp == date(2026, 10, 29) and nxt == datetime(2026, 11, 2, 11, 30, tzinfo=UTC)
    exp2, _ = OC.expected(r, datetime(2026, 11, 2, 12, 0, tzinfo=UTC))
    assert exp2 == date(2026, 10, 30)
    us = {**OC.DEFAULT_RULE, "cal": "weekdays", "tz": "America/New_York", "at": "18:00", "lag": 0}
    assert OC.expected(us, datetime(2026, 10, 30, 21, 0, tzinfo=UTC))[0] == date(2026, 10, 29)   # 17:00 ET
    assert OC.expected(us, datetime(2026, 10, 30, 23, 0, tzinfo=UTC))[0] == date(2026, 10, 30)


def test_expected_monthly_batch_weekly():
    m = {**OC.DEFAULT_RULE, "freq": "M", "lag_days": 25, "tz": "Europe/Berlin", "at": "15:00"}
    assert OC.expected(m, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))[0] == date(2026, 9, 1)
    assert OC.expected(m, datetime(2026, 10, 20, 12, 0, tzinfo=UTC))[0] == date(2026, 8, 1)
    b = {**OC.DEFAULT_RULE, "freq": "batch", "lag_days": 5, "tz": "Europe/Zurich", "at": "14:30"}
    assert OC.expected(b, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))[0] == date(2026, 9, 30)
    w = {**OC.DEFAULT_RULE, "freq": "W", "weekday": 5, "lag_days": 1, "tz": "Europe/Paris", "at": "09:00"}
    assert OC.expected(w, datetime(2026, 11, 1, 12, 0, tzinfo=UTC))[0] == date(2026, 10, 31)
    assert OC.expected(w, datetime(2026, 10, 31, 12, 0, tzinfo=UTC))[0] == date(2026, 10, 24)


def test_evaluate_entities_and_groups():
    spec = {"id": "rates.curves", "sla": 1, "key_column": "curve_id",
            "entities": {"curves": {"US_TSY": {"cal": "weekdays", "tz": "America/New_York", "at": "18:00", "lag": 0,
                                               "source": "ust"}},
                         "LT10": {"keys": ["IT_LT10", "FR_LT10"], "freq": "M", "lag_days": 25, "source": "ecb_irs"}}}
    now = datetime(2026, 10, 30, 23, 0, tzinfo=UTC)
    res = {r["entity"]: r for r in OC.evaluate(spec, {"US_TSY": date(2026, 10, 29), "IT_LT10": date(2026, 9, 1)}, now)}
    assert res["US_TSY"]["status"] == "due" and res["US_TSY"]["source"] == "ust"
    assert res["LT10"]["status"] == "fresh" and res["LT10"]["data_as_of"] == date(2026, 9, 1)
    res2 = OC.evaluate(spec, {"US_TSY": date(2026, 10, 30)}, now)
    assert {r["entity"]: r["status"] for r in res2} == {"US_TSY": "fresh", "LT10": "missing"}


def test_coverage_entries_cover_every_daily_curve():
    from app.ops.coverage import load_coverage

    ds = load_coverage()["datasets"]
    ents = OC.entity_specs(ds["rates.curves"])
    for c in R.CURVES:
        assert c.curve_id in ents and ents[c.curve_id]["source"] == c.source_id, c.curve_id
    lt = {k for e in ents.values() for k in e["keys"] if k.endswith("_LT10")}
    assert lt == {c.curve_id for c in R.ALL_CURVES if c.group == "lt10"}
    for did in ("rates.money", "credit.oas", "rates.auctions", "credit.bonds_eu", "debt.company", "sovereign.debt"):
        assert ds[did]["check"] == "published_dates"


# ------------------------------------------------------------------------------------------------ curves job

def test_publication_date_and_frames_pit():
    c = R.BY_ID["EA_AAA"]          # TARGET day + 1
    assert CJ.publication_date(c, date(2026, 10, 30)) == date(2026, 11, 2)
    assert CJ.publication_date(R.BY_ID["IT_LT10"], date(2026, 9, 1)) == date(2026, 10, 25)
    res = Loaded(rows=curve_rows("EA_AAA", "nominal", "ecb", "spot", [(date(2020, 1, 2), 120, -0.1),
                                                                       (date(2026, 10, 30), 120, 2.9)]))
    df, _ = CJ.frames(res, date(2026, 10, 31))
    kd = dict(zip(df["date"].to_list(), df["knowledge_date"].to_list(), strict=True))
    est = dict(zip(df["date"].to_list(), df["knowledge_date_estimated"].to_list(), strict=True))
    assert kd[date(2020, 1, 2)] == date(2020, 1, 3) and est[date(2020, 1, 2)] is True
    assert kd[date(2026, 10, 30)] == date(2026, 10, 31) and est[date(2026, 10, 30)] is False   # observed today
    assert df["value_bp_x100"].dtype == pl.Int32 and df["year"].to_list() == [2020, 2026]


def test_run_curves_incremental_and_resumable(tmp_data, monkeypatch):
    from app.core.config import get_settings
    from app.core.storage import read_table
    from app.rates.common import read_state

    s = get_settings()
    calls: list[tuple[str, date, bool]] = []

    def fake(src: str):
        def load(_s, start, backfill):
            calls.append((src, start, backfill))
            if src == "boe":
                raise RuntimeError("boom")
            rows = []
            for c in R.by_source(src):   # every curve of the source gets rows -> the next run is incremental
                rows += curve_rows(c.curve_id, c.kind, src, c.types[0],
                                   [(date(2026, 10, 28), 120, 3.0), (date(2026, 10, 29), 120, 3.1)])
            return Loaded(rows=rows, requests=1)
        return load
    monkeypatch.setattr(CJ, "LOADERS", {k: fake(k) for k in ("ust", "boe", "ecb_irs")})
    rep = CJ.run_curves(s, {"sources": ["ust", "boe", "ecb_irs"], "backfill": True})
    assert rep["failed"] == ["boe"] and rep["sources"]["ust"]["status"] == "ok"
    st = read_state(s)["backfill"]
    assert st["ust"]["done"] and "boe" not in st
    calls.clear()
    rep2 = CJ.run_curves(s, {"sources": ["ust", "boe"], "backfill": True})
    assert rep2["sources"]["ust"]["status"] == "skipped" and [c[0] for c in calls] == ["boe"]   # resumes at boe
    calls.clear()
    CJ.run_curves(s, {"sources": ["ust"]})                     # nightly: incremental from last stored - overlap
    assert calls[0][1] == date(2026, 10, 29) - __import__("datetime").timedelta(days=10)
    df = read_table("yield_curves", settings=s)
    assert df.filter(pl.col("curve_id") == "US_TSY").height == 2 and df["value_bp_x100"].max() == 31000
    assert df.filter(pl.col("curve_id") == "US_TSY_REAL").height == 2
    pr = CJ.probe_progress(s)
    assert pr["unit"] == "sources" and pr["done_units"] >= 2 and not pr["done"]


# ------------------------------------------------------------------------------------------------ company debt

def test_extract_companyfacts_latest_filed_wins():
    doc = {"facts": {"us-gaap": {
        "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths": {"units": {"USD": [
            {"end": "2025-12-31", "val": 100, "filed": "2026-02-20", "form": "10-K"},
            {"end": "2025-12-31", "val": 110, "filed": "2026-05-01", "form": "10-K/A"},
            {"end": "2025-12-31", "val": 999, "filed": "2026-06-01", "form": "8-K"}]}},
        "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive": {"units": {"USD": [
            {"end": "2025-12-31", "val": 500, "filed": "2026-02-20", "form": "10-K"}]}},
        "DebtWeightedAverageInterestRate": {"units": {"pure": [{"end": "2025-12-31", "val": 0.043, "filed": "2026-02-20",
                                                                 "form": "10-K"}]}},
        "Revenues": {"units": {"USD": [{"end": "2025-12-31", "val": 1, "filed": "2026-02-20", "form": "10-K"}]}}}}}
    rows = {r["field"]: r for r in CD.extract_companyfacts(doc, "0000000001")}
    assert set(rows) == {"mat_y1", "mat_after5", "weighted_avg_rate"}
    assert rows["mat_y1"]["val"] == 110 and rows["mat_y1"]["filed"] == date(2026, 5, 1)   # amendment wins, 8-K ignored


def test_build_company_debt_ratios_and_pit():
    pan = pl.DataFrame([
        {"issuer_id": "I1", "period_end": date(2024, 12, 31), "total_debt": 1000.0, "short_term_debt": 100.0,
         "long_term_debt": 900.0, "cash_and_equivalents": 200.0, "net_debt": None, "ebit": 300.0, "ebitda": 400.0,
         "interest_expense": 40.0, "currency": "USD", "fx_to_usd": 1.0, "knowledge_date": date(2025, 2, 20),
         "knowledge_date_estimated": False, "period_kind": "FY"},
        {"issuer_id": "I1", "period_end": date(2025, 12, 31), "total_debt": 1200.0, "short_term_debt": 200.0,
         "long_term_debt": 1000.0, "cash_and_equivalents": 100.0, "net_debt": None, "ebit": 330.0, "ebitda": 450.0,
         "interest_expense": 55.0, "currency": "USD", "fx_to_usd": 1.0, "knowledge_date": date(2026, 2, 20),
         "knowledge_date_estimated": False, "period_kind": "FY"}])
    prof = pl.DataFrame([{"issuer_id": "I1", "mat_period_end": date(2025, 12, 31), "mat_y1": 200.0, "mat_y2": 100.0,
                          "mat_y3": 100.0, "mat_y4": 100.0, "mat_y5": 100.0, "mat_after5": 600.0, "mat_y2_5": None,
                          "fixed_debt": 900.0, "floating_debt": 300.0, "weighted_avg_rate": 0.045,
                          "mat_source": "us_gaap", "mat_kd": date(2026, 3, 1)}])
    cd = CD.build_company_debt(pan, prof, None, {"USD": 0.04, "IG": 0.01, "HY": 0.03})
    r = cd.filter(pl.col("period_end") == date(2025, 12, 31)).to_dicts()[0]
    assert r["avg_cost_of_debt"] == pytest.approx(55 / 1100) and r["interest_coverage"] == pytest.approx(6.0)
    assert r["net_debt"] == 1100 and r["net_debt_to_ebitda"] == pytest.approx(1100 / 450)
    assert r["refi_share_24m"] == pytest.approx(0.25) and r["floating_share"] == pytest.approx(0.25)
    assert r["wam_years"] == pytest.approx((200 * .5 + 100 * 1.5 + 100 * 2.5 + 100 * 3.5 + 100 * 4.5 + 600 * 7.5) / 1200)
    assert r["refi_market_rate"] == pytest.approx(0.05) and r["mat_source"] == "us_gaap"   # IG: coverage 6 >= 4
    assert r["knowledge_date"] == date(2026, 3, 1)       # max of the inputs' knowledge dates (PIT)
    r0 = cd.filter(pl.col("period_end") == date(2024, 12, 31)).to_dicts()[0]
    assert r0["mat_source"] == "none" and r0["avg_cost_of_debt"] == pytest.approx(0.04)   # no profile <= 2024-12-31
    walls = CD.build_walls(cd, pl.DataFrame([{"issuer_id": "I1", "region": "US", "sector_id": "TECH"}]), date(2026, 6, 30))
    tot = walls.filter((pl.col("region") == "US") & (pl.col("sector_id") == "TECH") & (pl.col("bucket") == "total")).to_dicts()[0]
    assert tot["amount_usd"] == 1200 and tot["refi_share_24m"] == pytest.approx(0.25) and tot["n_issuers"] == 1
    assert set(walls["bucket"].unique()) == {"y1", "y2", "y3", "y4", "y5", "after5", "total"}
