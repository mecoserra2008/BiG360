"""ingest-global shared helpers: parsing, PIT rules, row builders, quota, hard timeouts, mapping (no network)."""

from __future__ import annotations

from datetime import date

import pytest
import yaml

from app.core.schema import conform
from app.ingestion import _global_common as g
from app.ingestion.global_mapping import core_items, coverage, hint, load_chains


def test_parse_num_formats():
    assert g.parse_num("1.234,56", decimal=",") == pytest.approx(1234.56)
    assert g.parse_num("1,234.56") == pytest.approx(1234.56)
    assert g.parse_num("(1,000)") == -1000
    assert g.parse_num("△500") == -500
    assert g.parse_num("-") is None and g.parse_num("－") is None and g.parse_num(None) is None
    assert g.parse_num("176,64 EUR", decimal=",") == pytest.approx(176.64)


def test_dates():
    assert g.to_date("2026-10-09T18:10:03+02:00") == date(2026, 10, 9)
    assert g.to_date("20261009") == date(2026, 10, 9)
    assert g.to_date("09.10.2026 14:27:19") == date(2026, 10, 9)
    assert g.to_date("09/10/2026") == date(2026, 10, 9)  # day-first
    assert g.to_date_us("10/8/2026 12:00:00 AM") == date(2026, 10, 8)
    assert g.parse_text_date("9 October 2026") == date(2026, 10, 9)
    assert g.parse_text_date("February 10, 2026") == date(2026, 2, 10)
    assert g.jst_to_utc(date(2026, 10, 9), "19:30").hour == 10


def test_fiscal_year_and_periods():
    assert g.fiscal_year_for(date(2025, 6, 30), 3) == 2026      # Toyota Q1 -> FY ending Mar-2026
    assert g.fiscal_year_for(date(2026, 3, 31), 3) == 2026
    assert g.fiscal_year_for(date(2025, 12, 31), None) == 2025
    assert g.period_start_for(date(2026, 6, 30), 3) == date(2026, 4, 1)
    assert g.period_start_for(date(2025, 12, 31), 12) == date(2025, 1, 1)
    assert g.period_start_for(date(2026, 2, 28), 3) == date(2025, 12, 1)


def test_impute_knowledge_date_caps_at_first_seen():
    assert g.impute_knowledge_date(date(2025, 12, 31), True, "EU") == date(2026, 4, 30)
    assert g.impute_knowledge_date(date(2025, 12, 31), False, "US") == date(2026, 2, 14)
    assert g.impute_knowledge_date(date(2026, 9, 30), False, "JP", first_seen=date(2026, 10, 10)) == date(2026, 10, 10)


def test_insider_row_signed_change_and_conform(tmp_data):
    r = g.insider_row(source_id="x", filing_id="x:1", line=0, region="EU", source_entity_id="LEI", insider_name="Jane Doe",
                      transaction_date=date(2026, 1, 2), knowledge_date=date(2026, 1, 3), txn_type="sell", shares=-100,
                      is_open_market=True, officer_title="Chief Financial Officer", price=10.0, shares_owned_after=900)
    assert r["shares"] == 100 and r["value_local"] == 1000 and r["acquired_disposed"] == "D"
    assert r["holding_change_pct"] == pytest.approx(-0.1) and r["role_norm"] == "cfo" and r["is_officer"]
    df = g.frame([r], "insider_transactions")
    assert conform(df, "insider_transactions").height == 1


def test_daily_quota_persists(tmp_data):
    q = g.DailyQuota("dartx", limit=10, settings=tmp_data, reserve=2)
    assert all(q.take() for _ in range(8))
    assert not q.take()
    assert g.DailyQuota("dartx", limit=10, settings=tmp_data, reserve=2).used() == 8


def test_hard_timeout_runner_kills_and_recovers():
    with g.HardTimeoutRunner(timeout_s=30) as r:
        assert r.call("app.ingestion._global_common:sleep_for", seconds=0.01) == 0.01
        with pytest.raises(g.CallTimeout):
            r.call("app.ingestion._global_common:sleep_for", timeout_s=1.0, seconds=20)
        assert r.call("app.ingestion._global_common:sleep_for", seconds=0.01) == 0.01  # fresh child after kill


def test_ext_mapping_items_exist_and_hints():
    from app.core.config import get_settings

    s = get_settings()
    base = {i["id"] for i in yaml.safe_load((s.contracts_dir / "canonical_line_items.yaml").read_text(encoding="utf-8"))["items"]}
    ext = yaml.safe_load((s.contracts_dir / "canonical_line_items_ext_global.yaml").read_text(encoding="utf-8"))
    assert set(ext["items"]) <= base
    assert {ns for v in ext["items"].values() for ns in v} <= {"yfinance", "baostock", "kr_label", "ifrs_uk", "tdnet", "tifrs", "cnmv_ipp"}
    assert hint("edinet", "jppfs", "jppfs_cor:NetSales") == "revenue"
    assert hint("esef_xbrl_org", "ifrs-full", "Assets") == "total_assets"
    assert hint("akshare", "cas_label", "营业总收入") == "revenue"
    assert hint("yfinance_fund", "yfinance", "Total Revenue") == "revenue"
    assert hint("opendart", "dart", "dart_OperatingIncomeLoss") == "operating_income"
    assert {"ifrs", "jp", "kr", "cn", "br_cvm", "yfinance"} <= set(load_chains())
    assert len(core_items()) >= 30


def test_coverage_report(tmp_data):
    rows = [g.fact_row(source_id="cvm", source_entity_id="9512", filing_id="cvm:1", form_type="DFP", taxonomy="cvm_account",
                       concept=c, label=None, period_start=date(2025, 1, 1), period_end=date(2025, 12, 31), value=1.0,
                       unit="BRL", knowledge_date=date(2026, 3, 1)) for c in ("3.01", "3.11", "6.01")]
    rep = coverage(g.frame(rows, "fundamentals_raw"))
    assert rep[0]["namespace"] == "br_cvm" and rep[0]["entities"] == 1 and 0 < rep[0]["mean_coverage"] < 1


def test_circuit_breaker_opens():
    from datetime import UTC, datetime

    from app.ingestion.base import FetchResult

    fr = FetchResult("x", started_at=datetime.now(UTC))

    def boom():
        raise ConnectionError("blocked")

    for i in range(15):
        g.safe_call(boom, fr, f"e{i}")
    assert fr.meta["_circuit_open"] == "ConnectionError"
    assert fr.entities_failed["e14"].startswith("circuit open")
