"""app.normalize.fundamentals on synthetic SEC-style facts: tag chains, discrete quarters (Q4 = FY - 9M), restatements
(PIT versions), TTM, derived items (FCF / EBITDA), bank revenue rule, FX, data-quality flags, wide pivot, PIT guard."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import polars as pl
import pytest

from app.core.storage import read_table
from app.normalize import fundamentals as F
from app.normalize.pit import asof

RAW_COLS = {
    "source_id": pl.Utf8, "fiscal_year": pl.Int16, "source_entity_id": pl.Utf8, "issuer_id": pl.Utf8, "filing_id": pl.Utf8,
    "form_type": pl.Utf8, "taxonomy": pl.Utf8, "concept": pl.Utf8, "label": pl.Utf8, "period_type": pl.Utf8,
    "period_start": pl.Date, "period_end": pl.Date, "fiscal_period": pl.Utf8, "value": pl.Float64, "unit": pl.Utf8,
    "decimals": pl.Int16, "dimensions_hash": pl.Utf8, "dimensions_json": pl.Utf8, "consolidated": pl.Boolean,
    "knowledge_date": pl.Date, "knowledge_date_estimated": pl.Boolean, "ingested_at": pl.Datetime("us", "UTC"),
}


def fact(concept, start, end, value, filed, *, iid="IAAA", tax="us-gaap", unit="USD", filing=None, dims="", cons=True):
    return {"source_id": "sec_edgar", "fiscal_year": end.year, "source_entity_id": "0000000001", "issuer_id": iid,
            "filing_id": filing or f"sec_edgar:{filed.isoformat()}", "form_type": "10-K", "taxonomy": tax, "concept": concept,
            "label": None, "period_type": "duration" if start else "instant", "period_start": start, "period_end": end,
            "fiscal_period": None, "value": float(value), "unit": unit, "decimals": None, "dimensions_hash": dims,
            "dimensions_json": None, "consolidated": cons, "knowledge_date": filed, "knowledge_date_estimated": False,
            "ingested_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}


D = date
Q1 = (D(2023, 1, 1), D(2023, 3, 31), D(2023, 5, 1))
H1 = (D(2023, 1, 1), D(2023, 6, 30), D(2023, 8, 1))
Q2 = (D(2023, 4, 1), D(2023, 6, 30), D(2023, 8, 1))
N9 = (D(2023, 1, 1), D(2023, 9, 30), D(2023, 11, 1))
FY = (D(2023, 1, 1), D(2023, 12, 31), D(2024, 2, 15))


def _raw() -> pl.DataFrame:
    rows = [
        # revenue: tag chain #2 (fallback), quarterly + YTD
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *Q1[:2], 100, Q1[2]),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *Q2[:2], 110, Q2[2]),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *H1[:2], 210, H1[2]),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *N9[:2], 320, N9[2]),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *FY[:2], 400, FY[2]),
        # restated FY in next year's 10-K (comparative) by +1% -> new version; tiny 0.5% change ignored
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *FY[:2], 402, D(2024, 8, 1)),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *FY[:2], 410, D(2025, 2, 14)),
        # cash flows: YTD only (Q2 = H1 - Q1, Q3 = 9M - H1, Q4 = FY - 9M)
        fact("NetCashProvidedByUsedInOperatingActivities", *Q1[:2], 30, Q1[2]),
        fact("NetCashProvidedByUsedInOperatingActivities", *H1[:2], 70, H1[2]),
        fact("NetCashProvidedByUsedInOperatingActivities", *N9[:2], 100, N9[2]),
        fact("NetCashProvidedByUsedInOperatingActivities", *FY[:2], 150, FY[2]),
        fact("PaymentsToAcquirePropertyPlantAndEquipment", *FY[:2], -40, FY[2]),     # negative in source -> outflow_positive
        fact("OperatingIncomeLoss", *FY[:2], 90, FY[2]),
        fact("DepreciationDepletionAndAmortization", *FY[:2], 20, FY[2]),
        fact("NetIncomeLoss", *FY[:2], 60, FY[2]),
        # balance sheet (identity holds at FY end, broken at Q3)
        fact("Assets", None, D(2023, 12, 31), 1000, FY[2]),
        fact("Liabilities", None, D(2023, 12, 31), 600, FY[2]),
        fact("StockholdersEquity", None, D(2023, 12, 31), 400, FY[2]),
        fact("Assets", None, D(2023, 9, 30), 1000, N9[2]),
        fact("Liabilities", None, D(2023, 9, 30), 500, N9[2]),
        fact("StockholdersEquity", None, D(2023, 9, 30), 300, N9[2]),
        # dimensional fact ignored, PIT-violating fact dropped, cover fact snapped to report period end
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *FY[:2], 999, FY[2], dims="abc"),
        fact("NetIncomeLoss", D(2024, 1, 1), D(2024, 12, 31), 5, D(2024, 2, 15)),
        fact("EntityCommonStockSharesOutstanding", None, D(2024, 2, 10), 50, FY[2], tax="dei", unit="shares", filing="sec_edgar:fy"),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", *FY[:2], 400, FY[2], filing="sec_edgar:fy"),
    ]
    return pl.DataFrame(rows, schema=RAW_COLS)


ISS = pl.DataFrame({"issuer_id": ["IAAA", "IBNK", "IEUR"], "region": ["US", "US", "EU"], "reporting_gaap": ["us_gaap", "us_gaap", "ifrs"],
                    "reporting_currency": ["USD", "USD", "EUR"], "subsector_id": ["software_application", "banks_regional", "utilities"],
                    "fiscal_year_end_mmdd": ["12-31", "12-31", "12-31"]})
EMPTY_RATES = pl.DataFrame(schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64, "source_id": pl.Utf8})


@pytest.fixture()
def canon():
    res = F.normalize(raw=_raw(), issuers=ISS, write=False, rates=EMPTY_RATES)
    return res["frames"]["canonical"], res["frames"]["wide"]


def _get(c, item, kind, end, kd=None):
    x = c.filter((pl.col("item_id") == item) & (pl.col("period_kind") == kind) & (pl.col("period_end") == end))
    if kd is not None:
        x = asof(x, kd, ["item_id", "period_kind", "period_end"])
    return x.sort("knowledge_date")


def test_quarters_and_q4_derivation(canon):
    c, _ = canon
    q = c.filter((pl.col("item_id") == "revenue") & (pl.col("period_kind") == "Q")).sort("period_end")
    assert q.get_column("fiscal_period").unique(maintain_order=True).to_list() == ["Q1", "Q2", "Q3", "Q4"]
    first = q.unique(subset=["fiscal_period"], keep="first", maintain_order=True)
    assert first.get_column("value").to_list() == [100, 110, 110, 80]
    q4 = q.filter(pl.col("fiscal_period") == "Q4").sort("knowledge_date")
    assert q4.item(0, "derivation") == "derived_period" and q4.item(0, "period_start") == D(2023, 10, 1)
    assert q4.item(0, "knowledge_date") == FY[2]
    cfo = c.filter((pl.col("item_id") == "cfo") & (pl.col("period_kind") == "Q")).sort("period_end")
    assert cfo.get_column("value").to_list() == [30, 40, 30, 50]


def test_restatement_versions_and_pit(canon):
    c, _ = canon
    fy = _get(c, "revenue", "FY", D(2023, 12, 31))
    assert fy.get_column("value").to_list() == [400, 410]          # +0.5% change ignored, +2.5% is a new version
    assert fy.get_column("is_restatement").to_list() == [False, True]
    assert fy.item(0, "derivation") == "fallback_tag" and fy.item(0, "dq_score") == pytest.approx(0.9)
    # as of 2024-12-31 only the original is visible; Q4 follows the restated FY later (PIT-derived period)
    assert _get(c, "revenue", "FY", D(2023, 12, 31), kd=D(2024, 12, 31)).item(0, "value") == 400
    q4 = _get(c, "revenue", "Q", D(2023, 12, 31))
    assert q4.get_column("value").to_list() == [80, 90]
    assert _get(c, "revenue", "Q", D(2023, 12, 31), kd=D(2023, 12, 31)).is_empty()   # not known before the 10-K


def test_ttm_and_derived_items(canon):
    c, _ = canon
    ttm = _get(c, "revenue", "TTM", D(2023, 12, 31))
    assert ttm.item(0, "value") == 400 and ttm.item(0, "derivation") == "ttm_sum"
    capex = _get(c, "capex", "FY", D(2023, 12, 31))
    assert capex.item(0, "value") == 40                             # outflow stored positive
    fcf = _get(c, "fcf", "FY", D(2023, 12, 31))
    assert fcf.item(0, "value") == 110 and fcf.item(0, "derivation") == "derived_formula"
    assert fcf.item(0, "dq_score") == pytest.approx(0.85)
    ebitda = _get(c, "ebitda", "FY", D(2023, 12, 31))
    assert ebitda.item(0, "value") == 110                           # ebit (= operating income 90) + D&A 20


def test_quality_flags_and_guards(canon):
    c, _ = canon
    bad = _get(c, "total_assets", "INSTANT", D(2023, 9, 30))
    ok = _get(c, "total_assets", "INSTANT", D(2023, 12, 31))
    assert bad.item(0, "dq_score") == pytest.approx(0.7) and ok.item(0, "dq_score") == pytest.approx(1.0)
    assert c.filter((pl.col("item_id") == "revenue") & (pl.col("value") == 999)).is_empty()     # dimensional ignored
    assert c.filter(pl.col("period_end") == D(2024, 12, 31)).is_empty()                        # kd < period_end dropped
    sh = c.filter(pl.col("item_id") == "shares_outstanding")
    assert sh.item(0, "period_end") == D(2023, 12, 31) and sh.item(0, "currency") is None      # cover fact snapped
    assert set(c.get_column("fiscal_year").to_list()) == {2023}
    assert c.select(["issuer_id", "item_id", "period_end", "period_kind", "knowledge_date"]).is_duplicated().sum() == 0


def test_wide_pivot(canon):
    _, w = canon
    fy = w.filter((pl.col("period_kind") == "FY") & (pl.col("currency_basis") == "LOCAL")).sort("knowledge_date")
    assert fy.height == 2 and fy.get_column("revenue").to_list() == [400, 410]
    r0 = fy.row(0, named=True)
    assert r0["total_assets"] == 1000 and r0["fcf"] == 110 and r0["currency"] == "USD" and 0 < r0["coverage"] < 1
    usd = w.filter((pl.col("period_kind") == "FY") & (pl.col("currency_basis") == "USD"))
    assert usd.get_column("revenue").to_list()[0] == 400            # USD issuer: identity conversion


def test_bank_revenue_rule():
    rows = [fact("Revenues", *FY[:2], 500, FY[2], iid="IBNK"),
            fact("InterestIncomeExpenseNet", *FY[:2], 300, FY[2], iid="IBNK"),
            fact("NoninterestIncome", *FY[:2], 150, FY[2], iid="IBNK")]
    res = F.normalize(raw=pl.DataFrame(rows, schema=RAW_COLS), issuers=ISS, write=False, rates=EMPTY_RATES)
    c = res["frames"]["canonical"]
    rev = c.filter((pl.col("item_id") == "revenue") & (pl.col("period_kind") == "FY"))
    assert rev.height == 1 and rev.item(0, "value") == 450 and rev.item(0, "derivation") == "derived_formula"


def test_fx_conversion_eur_issuer():
    days = [D(2023, 1, 1) + timedelta(days=i) for i in range(365)]
    rates = pl.DataFrame({"date": days, "currency": ["EUR"] * 365, "usd_per_unit": [1.1] * 180 + [1.2] * 185,
                          "source_id": ["ecb"] * 365})
    rows = [fact("Revenue", *FY[:2], 1000, FY[2], iid="IEUR", tax="ifrs-full", unit="EUR"),
            fact("Assets", None, FY[1], 5000, FY[2], iid="IEUR", tax="ifrs-full", unit="EUR")]
    res = F.normalize(raw=pl.DataFrame(rows, schema=RAW_COLS), issuers=ISS, write=False, rates=rates)
    c = res["frames"]["canonical"]
    rev = c.filter((pl.col("item_id") == "revenue") & (pl.col("period_kind") == "FY")).row(0, named=True)
    assert rev["fx_method"] == "period_average" and rev["value_usd"] == pytest.approx(1000 * (180 * 1.1 + 185 * 1.2) / 365)
    assert rev["gaap"] == "ifrs" and rev["currency"] == "EUR"
    ta = c.filter(pl.col("item_id") == "total_assets").row(0, named=True)
    assert ta["fx_method"] == "period_end" and ta["value_usd"] == pytest.approx(6000)


def test_write_and_replace(tmp_data):
    raw = _raw()
    F.normalize(raw=raw, issuers=ISS, write=True, rates=EMPTY_RATES, settings=tmp_data)
    n1 = read_table("fundamentals_canonical").height
    w1 = read_table("fundamentals_wide").height
    F.normalize(raw=raw, issuers=ISS, write=True, rates=EMPTY_RATES, settings=tmp_data)   # idempotent re-run
    assert read_table("fundamentals_canonical").height == n1 and read_table("fundamentals_wide").height == w1 and n1 > 0


def test_ext_global_yfinance_chain_low_precedence():
    rows = [fact("Total Revenue", *FY[:2], 300, FY[2] + timedelta(days=200), iid="IEUR", tax="yfinance", unit="EUR"),
            fact("Capital Expenditure", *FY[:2], -50, FY[2] + timedelta(days=200), iid="IEUR", tax="yfinance", unit="EUR")]
    raw = pl.DataFrame(rows, schema=RAW_COLS).with_columns(pl.lit("yfinance_fund").alias("source_id"),
                                                          pl.lit(True).alias("knowledge_date_estimated"))
    c = F.normalize(raw=raw, issuers=ISS, write=False, rates=EMPTY_RATES)["frames"]["canonical"]
    rev = c.filter((pl.col("item_id") == "revenue") & (pl.col("period_kind") == "FY")).row(0, named=True)
    assert rev["value"] == 300 and rev["source_id"] == "yfinance" and rev["knowledge_date_estimated"]
    assert rev["dq_score"] == pytest.approx(0.8)                    # estimated knowledge date penalty


def test_regressions_ttm_disclosures_null_sums_currency_and_shares():
    rows = []
    for y in (2023, 2024, 2025):
        s, e, f = D(y, 1, 1), D(y, 12, 31), D(y + 1, 2, 1)
        for concept, v in (("Revenues", 100 * y), ("NetIncomeLoss", 10), ("Assets", 0), ("WeightedAverageNumberOfDilutedSharesOutstanding", 50)):
            if concept == "Assets":
                rows.append(fact(concept, None, e, 1000, f, iid="IAAA"))
            elif concept.startswith("Weighted"):
                rows.append(fact(concept, s, e, v, f, iid="IAAA", unit="shares"))
            else:
                rows.append(fact(concept, s, e, v, f, iid="IAAA"))
        # Amazon-style trailing-twelve-month disclosure in a 10-Q (12M duration ending mid-year): not a fiscal year
        rows.append(fact("NetCashProvidedByUsedInOperatingActivities", D(y - 1, 7, 1), D(y, 6, 30), 5, D(y, 8, 1), iid="IAAA"))
    # a USD 20-F style fact for an issuer whose top-precedence source reports in BRL (dropped, not mixed)
    rows.append(fact("Revenues", D(2025, 1, 1), D(2025, 12, 31), 999, D(2026, 3, 1), iid="IAAA", unit="BRL", filing="sec_edgar:x"))
    c = F.normalize(raw=pl.DataFrame(rows, schema=RAW_COLS), issuers=ISS, write=False, rates=EMPTY_RATES)["frames"]["canonical"]
    fy = c.filter((pl.col("item_id") == "revenue") & (pl.col("period_kind") == "FY"))
    assert sorted(fy.get_column("fiscal_year").unique().to_list()) == [2023, 2024, 2025]
    assert set(fy.get_column("currency").to_list()) == {"USD"}
    assert c.filter(pl.col("item_id") == "total_debt").is_empty()          # no debt inputs -> null, never 0
    sh = c.filter(pl.col("item_id") == "shares_outstanding")
    assert sh.height == 3 and sh.item(0, "value") == 50 and sh.item(0, "derivation") == "derived_formula"
