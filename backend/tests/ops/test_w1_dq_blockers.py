"""COVERAGE_V2 W1 (2026-10-31): root-cause fixes of the data-quality failures that blocked the Saturday publish -
publication-calendar freshness (13F, FINRA), d- / p- story-file overlaps, the derived-period PIT leak behind ratios
pit_violation, stale ratio vintages after an incremental recompute, duplicate ml_labels keys."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from app.core import dq
from app.ops.sync import expect as X

# ------------------------------------------------------------------------------------------------ freshness


def test_13f_freshness_follows_the_filing_deadline():
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    chk = dq.ScheduledFreshness("institutional_holdings", "quarterly_13f", "period_end", 14)
    assert chk.overdue(date(2026, 6, 30), now)[0] == 0.0           # Q3 deadline (11-14) not reached: fresh
    days, det = chk.overdue(date(2026, 3, 31), now)                # Q2 (deadline 08-14) missing
    assert det["missing_period"] == "2026-06-30" and 77 < days < 78
    assert chk.overdue(None, now)[0] > 14


def test_finra_freshness_follows_the_publication_calendar():
    now = datetime(2026, 10, 31, 12, tzinfo=UTC)
    chk = dq.ScheduledFreshness("short_interest", "finra_settlements", "settlement_date", 10)
    exp = X.finra_expected(now)
    assert chk.overdue(exp, now)[0] == 0.0
    days, det = chk.overdue(exp - timedelta(days=20), now)
    pub = X._finra_pub(date.fromisoformat(det["missing_period"]))
    assert abs(days - (now - pub).total_seconds() / 86400) < 1e-6 and pub < now   # oldest unloaded publication


def test_default_checks_use_the_calendar_rule(tmp_data):
    checks = dq.default_checks(tmp_data, tables=["institutional_holdings", "short_interest", "prices_daily"])
    fr = {c.table: type(c).__name__ for c in checks if c.check_id == "freshness"}
    assert fr["institutional_holdings"] == "ScheduledFreshness" and fr["short_interest"] == "ScheduledFreshness"
    assert fr.get("prices_daily", "Freshness") == "Freshness"


# ------------------------------------------------------------------------------------------------ story files


def _members(day: date, hashes: list[int], src: int) -> pl.DataFrame:
    return pl.DataFrame({"date": [day] * len(hashes), "story_id": [1] * len(hashes), "url_hash": hashes,
                         "source_id": [src] * len(hashes), "domain_id": [None] * len(hashes), "minute": [60] * len(hashes),
                         "tone_x100": [0] * len(hashes), "lang_id": [None] * len(hashes)})


def test_day_file_supersedes_partial_keys(tmp_data):
    from app.core.storage import scan_table
    from app.events_v2 import store

    d = date(2026, 10, 31)
    store.write_partial("story_members", d, _members(d, [1, 2, 3], 0), tmp_data)
    store.write_day("story_members", d, _members(d, [3, 4], 2), tmp_data)          # story_cluster on the live day
    assert sorted(store.read_partial("story_members", d, tmp_data)["url_hash"].to_list()) == [1, 2]
    store.write_partial("story_members", d, _members(d, [1, 2, 3, 4, 5], 0), tmp_data)   # next hourly fold
    assert sorted(store.read_partial("story_members", d, tmp_data)["url_hash"].to_list()) == [1, 2, 5]
    lake = scan_table("story_members", tmp_data).collect()
    assert lake.height == lake.select("url_hash", "date").unique().height == 5      # no duplicate key in the lake


def test_heal_partial_overlaps_repairs_old_files(tmp_data):
    from app.events_v2 import store

    d = date(2026, 10, 31)
    store.write_day("story_members", d, _members(d, [7, 8], 2), tmp_data)
    # a partial written before the guard (bypass write_partial)
    pf = store.partial_file("story_members", d, tmp_data)
    store._write_atomic(store._prepare("story_members", _members(d, [8, 9], 0)), pf, store.DAY_LEVEL)
    assert store.heal_partial_overlaps("story_members", tmp_data) == {"days": 1, "rows_removed": 1}
    assert store.read_partial("story_members", d, tmp_data)["url_hash"].to_list() == [9]
    assert store.heal_partial_overlaps("story_members", tmp_data)["rows_removed"] == 0


# ------------------------------------------------------------------------------------------------ fundamentals PIT


def _span(item: str, span: str, start: date, end: date, kd: date, value: float) -> dict:
    return {"issuer_id": "IWK", "item_id": item, "fiscal_year": 2011, "span": span, "kd": kd, "value": value,
            "kd_est": False, "period_start": start, "period_end": end, "fy_start": date(2011, 1, 2),
            "fy_end": date(2011, 12, 31), "currency": "USD", "gaap": "us_gaap", "source_id": "sec_edgar",
            "concept": "c", "filing_id": "f", "derivation": "reported", "disagree": False, "is_restatement": False}


def test_derived_period_known_before_its_end_is_dropped():
    from app.normalize import fundamentals as F

    a, h, n = date(2011, 1, 2), date(2011, 7, 2), date(2011, 10, 1)
    rows = [_span(i, "YTD6", a, h, date(2011, 8, 11), 30.0) for i in ("revenue", "gross_profit")]
    rows += [_span(i, "YTD9", a, n, date(2011, 11, 10), 50.0) for i in ("revenue", "gross_profit")]
    # 52/53-week mislabel: the year-earlier YTD facts of cfo carry fiscal_year 2011
    rows += [_span("cfo", "YTD6", date(2010, 1, 3), date(2010, 7, 3), date(2010, 8, 12), 25.0),
             _span("cfo", "YTD9", date(2010, 1, 3), date(2010, 10, 2), date(2010, 11, 12), 45.0)]
    out = F._discrete(pl.DataFrame(rows))
    der = out.filter(pl.col("derivation") == "derived_period")
    assert der.filter(pl.col("kd") < pl.col("period_end")).is_empty()
    assert der.filter((pl.col("item_id") == "cfo") & (pl.col("fiscal_period") == "Q3")).is_empty()   # garbage Q3 gone
    q3 = der.filter((pl.col("item_id") == "revenue") & (pl.col("fiscal_period") == "Q3"))
    assert q3.height == 1 and q3.item(0, "value") == 20.0 and q3.item(0, "period_end") == n


# ------------------------------------------------------------------------------------------------ ratios vintages


def test_incremental_ratios_drop_vintages_no_longer_produced(tmp_data):
    from app.analytics.ratios import drop_stale_rows
    from app.core.storage import scan_table, write_table

    old, new = datetime(2026, 10, 30, tzinfo=UTC), datetime(2026, 10, 31, tzinfo=UTC)

    def row(iid: str, kd: date, ts: datetime) -> dict:
        return {"issuer_id": iid, "metric_id": "net_margin", "period_kind": "TTM", "fiscal_year": 2022,
                "fiscal_period": "TTM", "period_end": date(2022, 12, 31), "value": 0.1, "unit": "pct",
                "inputs_complete": True, "knowledge_date": kd, "dq_score": 1.0, "computed_at": ts, "run_id": "r"}

    write_table(pl.DataFrame([row("IA", date(2022, 3, 2), old), row("IA", date(2023, 3, 1), new),
                              row("IB", date(2022, 3, 2), old)]), "ratios", settings=tmp_data)
    assert drop_stale_rows("ratios", ["IA"], new, tmp_data) == 1
    left = scan_table("ratios", tmp_data).collect().sort("issuer_id")
    assert left.select("issuer_id", "knowledge_date").rows() == [("IA", date(2023, 3, 1)), ("IB", date(2022, 3, 2))]
    assert drop_stale_rows("ratios", ["IA"], new, tmp_data) == 0


# ------------------------------------------------------------------------------------------------ ml_labels


@pytest.mark.usefixtures("tmp_data")
def test_ml_labels_unique_with_overlapping_memberships(tmp_data):
    from app.core.schema import get_contract
    from app.core.storage import write_table
    from app.ml.labels import compute_labels
    from tests.ml.test_labels_pit import _prices, _seed_universe, _trading_days

    _seed_universe(tmp_data, ["AAA", "BBB"])
    now = datetime.now(UTC)
    # AAA is also in mvp_global and re-entered mvp_us: three membership intervals
    extra = pl.DataFrame({"universe_id": ["mvp_global", "mvp_us"], "security_id": ["AAA", "AAA"],
                          "valid_from": [date(2000, 1, 1), date(2019, 6, 1)], "valid_to": [None, date(2019, 6, 1)],
                          "rank_at_entry": [1, 1], "reason": ["seed", "seed"], "updated_at": [now, now]})
    write_table(extra, "universe_membership", settings=tmp_data)
    dates = _trading_days(date(2020, 1, 1), 260)
    for sid in ("AAA", "BBB"):
        write_table(_prices(sid, dates, [100.0 + i for i in range(260)]), "prices_daily", settings=tmp_data)
    df = compute_labels(dates[-1], tmp_data, ("mvp_us", "mvp_global"), start=dates[0])
    pk = get_contract("ml_labels").primary_key
    assert df.height > 0 and df.height == df.select(pk).unique().height
