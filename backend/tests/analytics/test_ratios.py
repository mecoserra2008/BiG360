"""app.analytics.ratios / sector_kpis: accounting-score formulas (textbook values), PIT vintage panels, dedupe."""

from __future__ import annotations

import math
from datetime import date

import polars as pl
import pytest

from app.analytics import ratios as R
from app.analytics import sector_kpis as SK


def _panel(rows: list[dict], fin: bool = False, groups=("industrial_mfg",), kpi_set="industrials") -> pl.DataFrame:
    df = pl.DataFrame(rows).with_columns(
        pl.lit("I1").alias("issuer_id"), pl.col("knowledge_date").alias("vintage"), pl.lit(True).alias("emit"),
        pl.lit(fin).alias("is_financial"), pl.lit(list(groups), dtype=pl.List(pl.Utf8)).alias("groups"),
        pl.lit(kpi_set).alias("kpi_set_id"), pl.lit("FY").alias("period_kind"))
    # single vintage holding the whole history (all rows known at the last date)
    last = df["knowledge_date"].max()
    return df.with_columns(pl.lit(last).alias("vintage"))


def _evaluated(rows: list[dict], **kw) -> pl.DataFrame:
    reg = R.registry()
    pan = _panel(rows, **kw)
    gen = reg["generic"]
    pan = R.eval_metrics(pan, {m: gen[m]["formula"] for m in gen}, reg["generic_order"])
    return R.accounting_scores(pan).sort("period_end")


BASE = dict(revenue=1000.0, cost_of_revenue=600.0, gross_profit=400.0, sga=150.0, ebit=200.0, net_income=120.0,
            net_income_continuing=120.0, net_income_parent=120.0, cfo=180.0, total_assets=2000.0,
            total_current_assets=800.0, total_current_liabilities=400.0, long_term_debt=300.0, total_liabilities=1000.0,
            total_equity=1000.0, retained_earnings=600.0, working_capital=400.0, accounts_receivable=150.0,
            ppe_net=700.0, depreciation_amortization=70.0, shares_outstanding=100.0)


def _two_years(cur: dict, prev: dict | None = None) -> list[dict]:
    p = dict(BASE, **(prev or {}))
    c = dict(BASE, **cur)
    return [dict(p, period_end=date(2022, 12, 31), knowledge_date=date(2023, 2, 15), fiscal_year=2022,
                 fiscal_period="FY", dq_score=1.0),
            dict(c, period_end=date(2023, 12, 31), knowledge_date=date(2024, 2, 15), fiscal_year=2023,
                 fiscal_period="FY", dq_score=1.0)]


def test_altman_z2_textbook():
    # WC/TA .2, RE/TA .3, EBIT/TA .1, BE/TL 1.0 -> 6.56*.2 + 3.26*.3 + 6.72*.1 + 1.05*1 = 4.012
    out = _evaluated(_two_years({}))
    assert out["altman_z2"][-1] == pytest.approx(4.012, abs=1e-9)


def test_altman_z_original_uses_pit_mcap():
    rows = _two_years({})
    pan = _panel(rows).with_columns(pl.lit(1500.0).alias("mcap"), pl.lit(1000.0).alias("total_liabilities__usd"))
    reg = R.registry()
    gen = reg["generic"]
    pan = R.eval_metrics(pan, {m: gen[m]["formula"] for m in gen}, reg["generic_order"])
    out = R.accounting_scores(pan).sort("period_end")
    # 1.2*.2 + 1.4*.3 + 3.3*.1 + 0.6*1.5 + 1.0*.5 = 2.39
    assert out["altman_z"][-1] == pytest.approx(2.39, abs=1e-9)


def test_altman_z_only_for_manufacturers_and_scores_null_for_financials():
    out = _evaluated(_two_years({}), groups=("software",))
    assert out["altman_z"][-1] is None
    fin = _evaluated(_two_years({}), fin=True, groups=("banks",), kpi_set="banks")
    assert fin["piotroski_f"][-1] is None and fin["altman_z2"][-1] is None and fin["beneish_m"][-1] is None


def test_beneish_neutral_firm():
    # identical years -> every index 1.0; tata = (NI - CFO)/TA = -60/2000
    out = _evaluated(_two_years({}))
    m = -4.84 + 0.920 + 0.528 + 0.404 + 0.892 + 0.115 - 0.172 + 4.679 * (-60 / 2000) - 0.327
    assert out["beneish_m"][-1] == pytest.approx(m, abs=1e-9)


def test_beneish_manipulator_signals_raise_m():
    # receivables up 3x vs sales, sales +50%, margins down -> M well above the -1.78 threshold
    cur = dict(revenue=1500.0, cost_of_revenue=1100.0, gross_profit=400.0, accounts_receivable=675.0, cfo=20.0,
               net_income=200.0, net_income_continuing=200.0)
    out = _evaluated(_two_years(cur))
    assert out["beneish_m"][-1] > -1.78


def test_piotroski_all_nine_signals():
    prev = dict(net_income=50.0, net_income_continuing=50.0, net_income_parent=50.0, long_term_debt=500.0,
                total_current_assets=700.0, shares_outstanding=110.0, gross_profit=300.0, cost_of_revenue=700.0,
                revenue=900.0)
    cur = dict(net_income=150.0, net_income_continuing=150.0, net_income_parent=150.0, cfo=200.0, long_term_debt=200.0,
               total_current_assets=900.0, shares_outstanding=100.0, gross_profit=450.0, cost_of_revenue=650.0,
               revenue=1100.0)
    out = _evaluated(_two_years(cur, prev))
    assert out["piotroski_f"][-1] == 9.0
    # deteriorating firm: loss, negative CFO, more debt, dilution, margin & turnover down -> 0
    bad = dict(net_income=-50.0, net_income_continuing=-50.0, net_income_parent=-50.0, cfo=-60.0, long_term_debt=900.0,
               total_current_assets=500.0, shares_outstanding=130.0, gross_profit=150.0, cost_of_revenue=650.0,
               revenue=800.0)
    out2 = _evaluated(_two_years(bad, cur))
    assert out2["piotroski_f"][-1] == 0.0


def test_sloan_accruals():
    out = _evaluated(_two_years({}))
    assert out["sloan_accruals"][-1] == pytest.approx((120 - 180) / 2000)


def test_generic_metrics_basic_values():
    out = _evaluated(_two_years({"revenue": 1100.0}))
    last = out.row(-1, named=True)
    assert last["gross_margin"] == pytest.approx(400 / 1100)
    assert last["revenue_growth_yoy"] == pytest.approx(0.1)
    assert last["roic"] is None or math.isfinite(last["roic"])


# ------------------------------------------------------------------------------------------------ PIT vintages


def _versions() -> pl.DataFrame:
    """FY2020 revenue 100 (filed 2021-02-01), FY2021 110 (filed 2022-02-01, with FY2020 restated to 105),
    FY2020 restated again to 100 on 2023-01-15 (late amendment)."""
    return pl.DataFrame({
        "issuer_id": ["I1"] * 4, "period_kind": ["FY"] * 4, "fiscal_period": ["FY"] * 4,
        "fiscal_year": [2020, 2020, 2021, 2020],
        "period_end": [date(2020, 12, 31), date(2020, 12, 31), date(2021, 12, 31), date(2020, 12, 31)],
        "knowledge_date": [date(2021, 2, 1), date(2022, 2, 1), date(2022, 2, 1), date(2023, 1, 15)],
        "revenue": [100.0, 105.0, 110.0, 100.0], "dq_score": [1.0] * 4,
    })


def test_vintage_expand_uses_only_known_rows():
    pan = R.vintage_expand(_versions())
    v1 = pan.filter(pl.col("vintage") == date(2021, 2, 1))
    assert v1.height == 1 and v1["revenue"][0] == 100.0           # FY2021 not visible yet
    v2 = pan.filter(pl.col("vintage") == date(2022, 2, 1)).sort("period_end")
    assert v2["revenue"].to_list() == [105.0, 110.0]               # restated comparative known at the same filing
    v3 = pan.filter(pl.col("vintage") == date(2023, 1, 15)).sort("period_end")
    assert v3["revenue"].to_list() == [100.0, 110.0]
    assert v3.filter(pl.col("emit"))["period_end"].min() == date(2020, 12, 31)


def test_growth_is_point_in_time_and_reemitted_after_restatement():
    pan = R.vintage_expand(_versions())
    pan = R.eval_metrics(pan, {"revenue_growth_yoy": "growth_yoy(revenue)"})
    long = R.dedupe_versions(R.to_long(pan, ["revenue_growth_yoy"], {"revenue_growth_yoy": "pct"}))
    g21 = long.filter(pl.col("period_end") == date(2021, 12, 31)).sort("knowledge_date")
    # never known before its own filing date (no look-ahead)
    assert g21["knowledge_date"].min() == date(2022, 2, 1)
    assert g21["value"].to_list() == pytest.approx([110 / 105 - 1, 110 / 100 - 1])
    assert g21["knowledge_date"].to_list() == [date(2022, 2, 1), date(2023, 1, 15)]


def test_dedupe_drops_unchanged_versions():
    df = pl.DataFrame({"issuer_id": ["I"] * 3, "metric_id": ["m"] * 3, "period_kind": ["TTM"] * 3,
                       "period_end": [date(2024, 3, 31)] * 3,
                       "knowledge_date": [date(2024, 5, 1), date(2024, 8, 1), date(2025, 5, 1)],
                       "value": [0.1, 0.1, 0.2]})
    out = R.dedupe_versions(df)
    assert out["value"].to_list() == [0.1, 0.2]


def test_registry_kpi_sets_cover_taxonomy_and_topo_order():
    from app.normalize.taxonomy import load_taxonomy

    reg = R.registry()
    subs = set(load_taxonomy()["subsectors"])
    assert subs <= set(reg["sub2set"]), subs - set(reg["sub2set"])
    order = reg["generic_order"]
    assert order.index("tax_rate_norm") < order.index("nopat") < order.index("roic") < order.index("roic_mean_5y")


def test_bank_kpis_only_for_banks():
    rows = [dict(period_end=date(2023, 12, 31), knowledge_date=date(2024, 2, 15), fiscal_year=2023, fiscal_period="FY",
                 dq_score=0.9, net_interest_income=50.0, noninterest_income=20.0, noninterest_expense=40.0,
                 loans_net=800.0, deposits=1000.0, total_assets=1500.0, total_equity_parent=120.0)]
    pan = _panel(rows, fin=True, groups=("banks",), kpi_set="banks").with_columns(
        pl.lit("FINS").alias("sector_id"), pl.lit("banks_regional").alias("subsector_id"))
    k = SK.kpi_rows(pan)
    vals = dict(zip(k["kpi_id"].to_list(), k["value"].to_list(), strict=True))
    assert vals["efficiency_ratio"] == pytest.approx(40 / 70)
    assert vals["loans_to_deposits"] == pytest.approx(0.8)
    assert set(k["kpi_set_id"].unique().to_list()) == {"banks"}
    assert k.filter(pl.col("kpi_id") == "rote")["peer_rank"][0] == 1
