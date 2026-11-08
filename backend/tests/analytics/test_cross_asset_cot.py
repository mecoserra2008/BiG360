"""CFTC positioning join into cross_asset_signals (roadmap v2 #5b): strictly point-in-time on the release date,
report type / category per asset class, staleness cut-off, inversion for USD/X pairs, contract conformity."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.analytics import cross_asset as ca
from app.core.schema import conform

CL = ca.Asset("CL", "WTI", "commodity", "energy", security_id="IDX:CL=F", futures_root="CL")
SPX = ca.Asset("SPX", "S&P 500", "equity_index", "US", security_id="IDX:^GSPC", futures_root="ES")
JPY = ca.Asset("USDJPY", "USD/JPY", "fx", "G10", fx=("USD", "JPY"), futures_root="6J", curve_sign=-1)
NOROOT = ca.Asset("DAX", "DAX", "equity_index", "EU", security_id="IDX:^GDAXI")


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


def _cot_rows() -> pl.DataFrame:
    """Weekly reports: as-of Tuesday, released Friday (knowledge_date = Tuesday + 3 days)."""
    rows = []
    for k in range(6):
        tue = date(2026, 9, 1) + timedelta(weeks=k)  # 2026-09-01 is a Tuesday
        for root, mk, rt, cat in (("CL", "067651", "disagg_fut", "m_money"), ("CL", "067651", "tff_fut", "lev_money"),
                                  ("ES", "13874A", "tff_fut", "lev_money"), ("6J", "097741", "tff_fut", "lev_money")):
            v = 0.1 * k + (0.5 if rt == "tff_fut" and root == "CL" else 0.0)
            rows.append({"root": root, "market_code": mk, "report_type": rt, "category": cat, "report_date": tue,
                         "knowledge_date": tue + timedelta(days=3), "cot_index_3y": v, "z_3y": float(k) - 2.0,
                         "net_pct_oi": 0.01 * k})
    return pl.DataFrame(rows).with_columns(pl.col("cot_index_3y", "z_3y", "net_pct_oi").cast(pl.Float32))


def _sig(assets: list[ca.Asset], days: list[date]) -> pl.DataFrame:
    return pl.DataFrame([{"asset_id": a.asset_id, "date": d, "price": 1.0} for a in assets for d in days])


def test_join_is_point_in_time_on_release_date():
    days = _days(date(2026, 9, 1), 45)
    out = ca.join_positioning(_sig([CL], days), _cot_rows(), [CL])
    by = {r["date"]: r for r in out.iter_rows(named=True)}
    # before the first release (Fri 2026-09-04) nothing is known
    assert by[date(2026, 9, 3)]["cot_market_code"] is None and by[date(2026, 9, 3)]["cot_index_3y"] is None
    # the release day itself knows the Tuesday report; the Tuesday of the next report still shows the previous one
    r = by[date(2026, 9, 4)]
    assert r["cot_report_date"] == date(2026, 9, 1) and r["cot_knowledge_date"] == date(2026, 9, 4)
    r = by[date(2026, 9, 8)]
    assert r["cot_report_date"] == date(2026, 9, 1)
    assert by[date(2026, 9, 11)]["cot_report_date"] == date(2026, 9, 8)
    # never a report released after the row date
    assert out.filter(pl.col("cot_knowledge_date") > pl.col("date")).height == 0
    # commodity -> disaggregated managed money (not the TFF row with +0.5)
    assert r["cot_report_type"] == "disagg_fut" and r["cot_category"] == "m_money"
    assert r["cot_index_3y"] == pytest.approx(0.0) and r["cot_inverted"] is False


def test_join_staleness_cutoff():
    days = _days(date(2026, 9, 1), 90)
    out = ca.join_positioning(_sig([CL], days), _cot_rows(), [CL], max_age_days=28)
    last_rel = date(2026, 9, 1) + timedelta(weeks=5, days=3)
    by = {r["date"]: r for r in out.iter_rows(named=True)}
    assert by[last_rel + timedelta(days=28)]["cot_market_code"] == "067651"
    assert by[last_rel + timedelta(days=29)]["cot_market_code"] is None


def test_financials_use_tff_leveraged_funds_and_inversion():
    days = _days(date(2026, 9, 20), 3)
    out = ca.join_positioning(_sig([SPX, JPY, NOROOT], days), _cot_rows(), [SPX, JPY, NOROOT])
    s = out.filter((pl.col("asset_id") == "SPX") & (pl.col("date") == date(2026, 9, 21))).row(0, named=True)
    j = out.filter((pl.col("asset_id") == "USDJPY") & (pl.col("date") == date(2026, 9, 21))).row(0, named=True)
    d = out.filter(pl.col("asset_id") == "DAX")
    assert s["cot_report_type"] == "tff_fut" and s["cot_category"] == "lev_money" and s["cot_market_code"] == "13874A"
    # report of 2026-09-15 (k = 2) released 09-18
    assert s["cot_report_date"] == date(2026, 9, 15) and s["cot_index_3y"] == pytest.approx(0.2)
    assert s["cot_z_3y"] == pytest.approx(0.0) and s["cot_inverted"] is False
    # USD/JPY is quoted inversely to the yen future: index 1 - x, z and % OI negated
    assert j["cot_inverted"] is True and j["cot_index_3y"] == pytest.approx(0.8)
    assert j["cot_net_pct_oi"] == pytest.approx(-0.02)
    assert d.get_column("cot_market_code").null_count() == d.height and d.get_column("cot_inverted").null_count() == d.height


def test_join_without_cftc_keeps_nulls_and_conforms(tmp_data):
    days = _days(date(2026, 9, 1), 5)
    sig = _sig([CL], days)
    empty = ca.load_cot([CL])  # no CFTC tables in the temporary lake -> soft dependency
    assert empty.is_empty()
    out = ca.join_positioning(sig, empty, [CL])
    assert set(ca.COT_COLS) <= set(out.columns) and out.get_column("cot_index_3y").null_count() == 5
    full = ca.join_positioning(sig, _cot_rows(), [CL]).with_columns(
        pl.lit("commodity").alias("asset_class"), pl.lit("yahoo").alias("price_source"),
        pl.col("date").alias("knowledge_date"), pl.lit(datetime(2026, 10, 1)).alias("computed_at"))
    conform(full, "cross_asset_signals")  # cot_* columns match the contract


def test_registry_links_instruments():
    reg = ca.asset_registry()
    r = {x["asset_id"]: x for x in reg.iter_rows(named=True)}
    assert r["CL"]["instrument_id"] == "IDX:CL=F" and r["CL"]["cot_report_type"] == "disagg_fut"
    assert r["EURUSD"]["instrument_id"] == "IDX:EURUSD=X" and "IDX:6E=F" in r["EURUSD"]["linked_ids"]
    assert r["UST10Y"]["instrument_id"] == "IDX:ZN=F" and r["SPX"]["cot_category"] == "lev_money"
    assert r["DAX"]["cot_report_type"] is None and reg.height == len(ca.build_assets())
