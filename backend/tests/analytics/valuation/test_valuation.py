"""Cost of capital, moat and forecast/valuation: balance-sheet identity, DCF math vs hand calculation, reverse-DCF
root finding, excess-return model, moat classification edge cases, point-in-time behaviour and an end-to-end run
on a synthetic universe validated against the table contracts (owner: analytics-valuation)."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import orjson
import polars as pl
import pytest

from app.analytics import cost_of_capital as cc
from app.analytics import forecast as fc
from app.analytics import moat
from app.core.schema import conform

AS_OF = date(2026, 6, 30)


# ------------------------------------------------------------------------------------------------ synthetic data


def make_inputs(n: int = 12, seed: int = 7, late_restatement: bool = False) -> cc.Inputs:
    """n issuers (half software, half airlines) with 10 FY of fundamentals, 4y of daily prices, DGS10 + FX."""
    rng = np.random.default_rng(seed)
    iss_rows, fy_rows, lat_rows, usd_rows, px_rows = [], [], [], [], []
    days = pl.date_range(date(2022, 1, 3), AS_OF, "1d", eager=True).filter(
        pl.date_range(date(2022, 1, 3), AS_OF, "1d", eager=True).dt.weekday() <= 5)
    m = rng.normal(0.0003, 0.01, len(days))
    bench = 100 * np.exp(np.cumsum(m))
    px_rows += [{"security_id": "IDX:^GSPC", "date": d, "close": c, "adj_close": c, "currency": "USD"}
                for d, c in zip(days, bench, strict=False)]
    for i in range(n):
        soft = i < n // 2
        iid, sid = f"I{i:012d}", f"S{i:012d}"
        iss_rows.append({"issuer_id": iid, "name": f"Co{i}", "region": "US", "country": "US",
                         "sector_id": "TECH" if soft else "INDU",
                         "subsector_id": "software_application" if soft else "airlines",
                         "reporting_currency": "USD", "security_id": sid, "display_ticker": f"T{i}",
                         "trading_currency": "USD", "adr_ratio": None, "exchange_country": "US"})
        rev, shares = 1000.0 * (1 + i), 1000.0 + 10 * i
        beta = 1.2 if soft else 0.9
        r = beta * m + rng.normal(0, 0.012, len(days))
        p = 50 * np.exp(np.cumsum(r))
        px_rows += [{"security_id": sid, "date": d, "close": c, "adj_close": c, "currency": "USD"}
                    for d, c in zip(days, p, strict=False)]
        for k, y in enumerate(range(2016, 2026)):
            g = 0.12 if soft else rng.normal(0.03, 0.15)
            rev = rev * (1 + g)
            om = (0.30 if soft else 0.04 + rng.normal(0, 0.05))
            ebit = rev * om
            pe = date(y, 12, 31)
            row = {"issuer_id": iid, "period_end": pe, "period_kind": "FY", "currency_basis": "LOCAL",
                   "knowledge_date": pe + timedelta(days=60), "fiscal_year": y, "currency": "USD",
                   "revenue": rev, "gross_profit": rev * (0.8 if soft else 0.25), "ebit": ebit,
                   "operating_income": ebit, "pretax_income": ebit - 10, "interest_expense": 10.0,
                   "income_tax": max(ebit - 10, 0) * 0.21, "net_income": (ebit - 10) * 0.79,
                   "net_income_parent": (ebit - 10) * 0.79, "depreciation_amortization": rev * 0.04,
                   "capex": rev * (0.03 if soft else 0.08), "operating_working_capital": rev * 0.05,
                   "sbc": rev * (0.05 if soft else 0.005), "dividends_paid": max(ebit, 0) * 0.2,
                   "buybacks": max(ebit, 0) * 0.1, "cash_and_st_investments": rev * 0.3, "total_debt": rev * 0.4,
                   "long_term_debt": rev * 0.35, "total_equity": rev * 0.8, "total_equity_parent": rev * 0.8,
                   "total_assets": rev * 2.0, "total_liabilities": rev * 1.2, "ppe_net": rev * (0.2 if soft else 1.0),
                   "intangible_assets": rev * 0.05, "rnd": rev * (0.15 if soft else 0.0), "deferred_revenue":
                   rev * (0.25 if soft else 0.02), "shares_outstanding": shares, "shares_diluted_weighted": shares,
                   "eps_diluted": (ebit - 10) * 0.79 / shares, "minority_interest": 0.0}
            row["invested_capital"] = row["total_debt"] + row["total_equity"] - row["cash_and_st_investments"]
            fy_rows.append(row)
            usd_rows.append({k2: row[k2] for k2 in ("issuer_id", "period_end", "period_kind", "knowledge_date",
                                                    "fiscal_year", "revenue")} | {"currency_basis": "USD"})
            lat_rows.append({k2: row.get(k2) for k2 in (
                "issuer_id", "period_end", "period_kind", "currency_basis", "knowledge_date", "fiscal_year", "currency",
                "shares_outstanding", "shares_diluted_weighted", "cash_and_st_investments", "total_debt",
                "minority_interest", "total_equity_parent", "total_equity", "long_term_debt")} | {
                "preferred_equity": None, "long_term_investments": None})
            if late_restatement and k == 9:  # a restated FY2025 published AFTER as_of: must be invisible
                bad = dict(row, knowledge_date=AS_OF + timedelta(days=10), revenue=row["revenue"] * 50,
                           ebit=row["ebit"] * 50)
                fy_rows.append(bad)
    fy = pl.DataFrame(fy_rows, infer_schema_length=None)
    prices = pl.DataFrame(px_rows)
    macro = pl.DataFrame({"series_id": ["fred:DGS10"] * 3, "date": [date(2016, 12, 30), date(2026, 6, 26),
                                                                       date(2026, 6, 29)],
                          "value": [2.5, 4.0, 9.9], "knowledge_date": [date(2016, 12, 31), date(2026, 6, 27),
                                                                      date(2026, 7, 2)]})
    iss = pl.DataFrame(iss_rows, infer_schema_length=None)
    sb = iss.select("security_id", pl.lit("IDX:^GSPC").alias("bench_id"))
    fx = pl.DataFrame({"date": [AS_OF], "currency": ["EUR"], "usd_per_unit": [1.1], "source_id": ["ecb"]})
    return cc.Inputs(iss, fy, pl.DataFrame(lat_rows, infer_schema_length=None),
                     pl.DataFrame(usd_rows, infer_schema_length=None), prices.select("security_id", "date", "close",
                                                                                     "currency"),
                     cc.weekly_returns(prices), sb, macro, fx)


@pytest.fixture(scope="module")
def inputs() -> cc.Inputs:
    return make_inputs()


# ------------------------------------------------------------------------------------------------ cost of capital


def test_risk_free_is_point_in_time(inputs):
    rf, src = cc.risk_free(inputs.macro, "USD", AS_OF)
    assert src == "fred:DGS10" and rf == pytest.approx(0.04)  # the 9.9% print is known only on 2026-07-02
    rf_late, _ = cc.risk_free(inputs.macro, "USD", date(2026, 7, 3))
    assert rf_late == pytest.approx(0.099)
    rf_cny, src_cny = cc.risk_free(inputs.macro, "CNY", AS_OF)
    assert src_cny.startswith("fallback") and rf_cny == cc.FALLBACK_RF["CNY"]


def test_coverage_spread_and_terminal_growth():
    assert cc.coverage_spread(20) == 0.006 and cc.coverage_spread(0.5) == 0.11 and cc.coverage_spread(None) == 0.017
    assert cc.terminal_growth("USD", 0.05) == pytest.approx(0.03)      # developed cap 3%
    assert cc.terminal_growth("JPY", 0.015) == pytest.approx(0.01)     # gdp 1.5% - 0.5%
    assert cc.terminal_growth("CNY", 0.018) == pytest.approx(0.018)    # capped by rf
    assert cc.terminal_growth("BRL", 0.11) == pytest.approx(0.065)     # EM excess nominal growth


def test_financial_routing():
    assert cc.financial_flag("banks_global", "FINS", None, None)
    assert cc.financial_flag("diversified_holdings", "FINS", None, None)
    assert cc.financial_flag("capital_markets_exchanges", "FINS", 1200.0, 100.0, 60.0)       # broker-dealer
    assert not cc.financial_flag("payments_fintech", "FINS", 48.0, 7.0, 28.0)              # MA-like buyback leverage
    assert not cc.financial_flag("software_application", "TECH", 10.0, 1.0, 1.0)


def test_cost_of_capital_rows_and_rates_consistency(inputs):
    coc = cc.compute(inputs, AS_OF, run_id="Rtest")
    conform(coc, "cost_of_capital")
    assert coc.height == 12
    r = coc.row(0, named=True)
    assert r["beta_source"] == "regression" and 0.4 <= r["beta"] <= 2.0
    assert r["rf"] == pytest.approx(0.04)
    ke, w = cc.rates(r["rf"], r["beta"], r["crp"], r["kd_post"], r["weight_equity"], r["is_financial"], r["erp"])
    assert ke == pytest.approx(r["cost_of_equity"]) and w == pytest.approx(r["wacc"])
    soft = coc.filter(pl.col("issuer_id") == "I000000000000").row(0, named=True)
    air = coc.filter(pl.col("issuer_id") == "I000000000011").row(0, named=True)
    assert soft["beta_raw"] > air["beta_raw"]  # 1.2 vs 0.9 true betas


def test_fy_asof_ignores_later_restatement():
    inp = make_inputs(late_restatement=True)
    fy = cc.fy_asof(inp.fy_all, AS_OF)
    last = fy.filter((pl.col("issuer_id") == "I000000000000") & (pl.col("fiscal_year") == 2025))
    assert last.height == 1 and last["knowledge_date"][0] <= AS_OF
    fy_late = cc.fy_asof(inp.fy_all, AS_OF + timedelta(days=20))
    late = fy_late.filter((pl.col("issuer_id") == "I000000000000") & (pl.col("fiscal_year") == 2025))
    assert late["revenue"][0] == pytest.approx(last["revenue"][0] * 50)


# ------------------------------------------------------------------------------------------------ DCF math


def test_dcf_value_matches_hand_calculation():
    fcff = np.array([100.0, 110.0, 120.0, 130.0, 140.0])
    tau = np.arange(1, 6, dtype=float)
    w, g, tcf = 0.10, 0.02, 140.0 * 1.02
    hand = sum(cf / 1.1 ** t for t, cf in zip(range(1, 6), fcff, strict=False)) + tcf / (w - g) / 1.1 ** 5
    assert fc.dcf_value(fcff, tau, w, g, tcf) == pytest.approx(hand, rel=1e-12)
    assert fc.dcf_value(fcff, tau, 0.02, 0.02, tcf) is None  # w <= g + 0.5%


def test_value_driver_terminal_and_fade_stage():
    assert fc.terminal_fcff(100.0, 0.03, 0.15) == pytest.approx(100 * 1.03 * (1 - 0.2))
    # revenue 1000, EBITA margin 13.3% -> NOPAT 100 (tax 25%), operating capital 1000 -> ROIC 10%
    proj = {"fcff": np.full(5, 80.0), "nopat": np.full(5, 100.0), "revenue": np.full(5, 1000.0),
            "ebit": np.full(5, 100.0 / 0.75), "operating_capital": np.full(5, 1000.0)}
    tau = np.arange(1, 6, dtype=float)
    d0 = fc.value_fcff_detail(proj, tau, 0.09, 0.03, fade_years=0, tax=0.25)
    assert d0["ronic"] == pytest.approx(max(0.09, (0.10 + 0.09) / 2))
    hand = fc.dcf_value(proj["fcff"], tau, 0.09, 0.03, fc.terminal_fcff(100.0, 0.03, d0["ronic"]))
    assert d0["ev"] == pytest.approx(hand)
    assert d0["tv_share"] == pytest.approx(d0["pv_tv"] / d0["ev"]) and 0 < d0["tv_share"] < 1
    # ROIC (10%) above WACC (9%): faster growth in the fade stage adds value
    ev_lo, _ = fc.value_fcff(proj, tau, 0.09, 0.03, g_start=0.03, fade_years=5, tax=0.25)
    ev_hi, _ = fc.value_fcff(proj, tau, 0.09, 0.03, g_start=0.08, fade_years=5, tax=0.25)
    assert ev_hi > ev_lo
    # margin keeps converging in the fade stage toward the long-run margin
    ev_up, _ = fc.value_fcff(proj, tau, 0.09, 0.03, g_start=0.03, m_lr=0.25, tax=0.25)
    assert ev_up > ev_lo


def test_margin_path_speed_cap():
    p = fc.margin_path(-0.10, 0.20, 0.04)
    assert np.allclose(np.diff(np.r_[-0.10, p]), 0.04)
    assert np.allclose(fc.margin_path(0.10, 0.11, 0.02), [0.11] * 5)


def test_multiples_equity_methods():
    peer = {"ev_ebita": 10.0, "pe": 15.0, "ev_sales": 2.0, "pb": 1.5}
    eq, parts = fc.multiples_equity(peer, ebita_norm=100.0, ni_norm=60.0, rev=1000.0, bv=None, claims=200.0,
                                    is_fin=False, loss_maker=False)
    assert parts == {"ev_ebita": 800.0, "pe": 900.0} and eq == pytest.approx((800.0 * 900.0) ** 0.5)
    eq, parts = fc.multiples_equity(peer, ebita_norm=None, ni_norm=None, rev=1000.0, bv=None, claims=200.0,
                                    is_fin=False, loss_maker=True)
    assert parts == {"ev_sales": 1800.0}
    eq, parts = fc.multiples_equity(peer, ebita_norm=None, ni_norm=10.0, rev=None, bv=100.0, claims=0.0,
                                    is_fin=True, loss_maker=False)
    assert set(parts) == {"pe", "pb"}
    assert fc.multiples_equity(None, ebita_norm=1, ni_norm=1, rev=1, bv=1, claims=0, is_fin=False,
                               loss_maker=False) == (None, {})


def test_blend_confidence_and_extreme_flag():
    def rows(dcf):
        return [{"scenario": "base", "fair_value_per_share": dcf, "_dps": 0.0},
                {"scenario": "bear", "fair_value_per_share": dcf * 0.8, "_dps": 0.0},
                {"scenario": "bull", "fair_value_per_share": dcf * 1.2, "_dps": 0.0},
                {"scenario": "market_implied", "fair_value_per_share": None}]
    r = rows(110.0)
    info = fc.blend_and_score(r, fv_mult=105.0, s=1.0, q=1.0, reasons=[], price=100.0, to_px=1.0, shares=10.0,
                              claims=0.0, ke=0.08)
    base = r[0]
    assert info["confidence"] > 90 and base["valuation_confidence"] > 90 and base["confidence_reasons"] is None
    assert 100 < base["fair_value_per_share"] < 110 and r[1]["fair_value_per_share"] < base["fair_value_per_share"] \
        < r[2]["fair_value_per_share"]
    assert base["fair_value_low"] == 105.0 and base["fair_value_high"] == 110.0
    r = rows(900.0)  # DCF 9x the price, multiples 4x -> capped confidence + reason
    fc.blend_and_score(r, fv_mult=400.0, s=1.0, q=1.0, reasons=[], price=100.0, to_px=1.0, shares=10.0,
                       claims=0.0, ke=0.08)
    assert r[0]["upside_pct"] > 1.0 and r[0]["valuation_confidence"] <= 35
    assert "extreme_upside" in r[0]["confidence_reasons"]
    r = rows(-50.0)  # DCF non-positive -> multiples only, low confidence
    fc.blend_and_score(r, fv_mult=120.0, s=0.5, q=1.0, reasons=[], price=100.0, to_px=1.0, shares=10.0,
                       claims=0.0, ke=0.08)
    assert "model:dcf_nonpositive" in r[0]["confidence_reasons"] and r[0]["fair_value_per_share"] > 100


def test_discount_times_and_theil_sen():
    tau = fc.discount_times(date(2025, 12, 31), date(2026, 6, 30))
    assert tau[0] == pytest.approx(0.5, abs=0.01) and tau[4] == pytest.approx(4.5, abs=0.01)
    yrs = list(range(2016, 2026))
    assert fc.theil_sen_growth(yrs, [100 * 1.07 ** k for k in range(10)]) == pytest.approx(0.07)


# ------------------------------------------------------------------------------------------------ 3 statements


def _drivers(**kw) -> fc.Drivers:
    d = dict(growth=np.full(5, 0.06), ebit_margin=np.linspace(0.15, 0.18, 5), gross_margin=np.full(5, 0.5),
             da_pct=0.04, capex_pct=0.05, nwc_pct=0.1, sbc_pct=0.01, tax_rate=0.21, kd=0.05, cash_yield=0.03,
             payout=0.3, buyback=0.2)
    d.update(kw)
    return fc.Drivers(**d)


def test_balance_sheet_balances_every_year():
    op = fc.Opening(revenue=1000, cash=150, debt=300, equity=600, nwc=90, fixed=500, shares=100, price=20.0)
    p = fc.project(_drivers(), op)
    assert np.abs(p["bs_imbalance"]).max() < 1e-6
    assert (p["revolver"] == 0).all()
    # cash identity: change in cash = CFO - capex - dividends - buybacks (no debt flows)
    dc = np.diff(np.r_[150.0, p["cash_and_st_investments"]])
    assert np.allclose(dc, p["cfo"] - p["capex"] - p["dividends_paid"] - p["buybacks"])
    assert p["shares_diluted_weighted"][-1] < 100  # buybacks retire shares


def test_revolver_plug_when_cash_runs_out():
    op = fc.Opening(revenue=1000, cash=5, debt=300, equity=600, nwc=90, fixed=500, shares=100)
    p = fc.project(_drivers(capex_pct=0.4, payout=1.0, buyback=0.2), op)
    assert np.abs(p["bs_imbalance"]).max() < 1e-6
    assert p["revolver"][-1] > 0
    assert np.allclose(p["cash_and_st_investments"], 0.02 * p["revenue"])
    assert np.allclose(p["total_debt"], 300 + p["revolver"])


def test_excess_return_model():
    tau = np.arange(1, 6, dtype=float)
    v, arr = fc.excess_return_value(1000.0, np.full(5, 0.10), 0.5, 0.10, 0.02, tau)
    assert v == pytest.approx(1000.0)  # ROE == ke -> worth book
    assert np.allclose(arr["residual_income"], 0)
    v2, _ = fc.excess_return_value(1000.0, np.full(5, 0.15), 0.5, 0.10, 0.02, tau)
    assert v2 > 1000.0
    assert fc.excess_return_value(1000.0, np.full(5, 0.15), 0.5, 0.02, 0.02, tau)[0] is None


def test_reverse_dcf_root_recovers_growth():
    op = fc.Opening(revenue=1000, cash=150, debt=300, equity=600, nwc=90, fixed=500, shares=100)
    tau = np.arange(1, 6, dtype=float)

    def ev(gc: float) -> float:
        p = fc.project(_drivers(growth=np.full(5, gc)), op)
        return fc.value_fcff(p, tau, 0.09, 0.025, None, g_start=gc)[0]

    target = ev(0.11)
    root = fc.implied_root(lambda gc: ev(gc) - target, -0.5, 1.5)
    assert root == pytest.approx(0.11, abs=1e-4)
    assert fc.implied_root(lambda gc: 1.0 + gc * 0, -0.5, 1.5) is None  # not bracketed


# ------------------------------------------------------------------------------------------------ moat


@pytest.mark.parametrize(("score", "pers", "years", "cov", "expected"), [
    (90, 1.0, 10, 1.0, "wide"),
    (90, 1.0, 6, 1.0, "narrow"),        # wide needs >= 7 years
    (90, 0.7, 10, 1.0, "narrow"),       # persistence below the wide bar
    (90, 0.5, 10, 1.0, "none"),
    (40, 1.0, 10, 1.0, "none"),
    (90, 1.0, 4, 1.0, "insufficient_data"),
    (90, 1.0, 10, 0.5, "insufficient_data"),
    (None, None, 10, 1.0, "insufficient_data"),
])
def test_moat_classification_edges(score, pers, years, cov, expected):
    spec = {"min_years": 5, "calibrated": {"wide_score": 70, "wide_persistence": 0.8, "wide_min_years": 7,
                                           "narrow_score": 50, "narrow_persistence": 0.6}}
    assert moat.classify(score, pers, years, cov, spec) == expected


def test_moat_confidence_formula():
    assert moat.confidence(10, 1.0, 0.0) == pytest.approx(1.0)
    assert moat.confidence(5, 4 / 7, 0.30) == pytest.approx(0.4 * 0.5 + 0.3 * 4 / 7, abs=1e-4)


def test_moat_end_to_end(inputs):
    coc = cc.compute(inputs, AS_OF)
    m = moat.compute(inputs, AS_OF, coc, run_id="Rtest")
    conform(m, "moat")
    soft = m.filter(pl.col("issuer_id") == "I000000000000").row(0, named=True)
    air = m.filter(pl.col("issuer_id") == "I000000000011").row(0, named=True)
    assert soft["moat_class"] in ("wide", "narrow") and air["moat_class"] in ("none", "narrow")
    assert soft["moat_score"] > air["moat_score"]
    assert soft["roic_wacc_persistence"] == 1.0 and soft["years_evaluated"] == 10
    det = orjson.loads(soft["component_detail"])
    assert det["roic_wacc_spread"]["score"] >= 50 and len(det["roic_wacc_spread"]["history"]) == 10
    assert det["roic_wacc_persistence"]["passed"] is True
    assert {"threshold", "score", "value"} <= set(det["gm_level"])
    assert all(s["type"] in {"scale", "intangibles", "switching_costs", "network", "cost_advantage",
                             "efficient_scale"} for s in orjson.loads(soft["moat_sources"]))


def test_moat_insufficient_history():
    inp = make_inputs()
    inp.fy_all = inp.fy_all.filter(~((pl.col("issuer_id") == "I000000000000") & (pl.col("fiscal_year") < 2022)))
    m = moat.compute(inp, AS_OF, cc.compute(inp, AS_OF))
    assert m.filter(pl.col("issuer_id") == "I000000000000")["moat_class"][0] == "insufficient_data"


def test_moat_is_point_in_time():
    base = moat.compute(make_inputs(), AS_OF, cc.compute(make_inputs(), AS_OF))
    inp = make_inputs(late_restatement=True)
    late = moat.compute(inp, AS_OF, cc.compute(inp, AS_OF))
    a = base.sort("issuer_id").select("issuer_id", "moat_score", "moat_class")
    b = late.sort("issuer_id").select("issuer_id", "moat_score", "moat_class")
    assert a.equals(b)


# ------------------------------------------------------------------------------------------------ forecast e2e


def test_forecast_end_to_end_contracts_and_identity(inputs):
    coc = cc.compute(inputs, AS_OF)
    fdf, vdf, stat = fc.compute(inputs, AS_OF, coc, run_id="Rtest", erp_mode="fixed")
    assert stat["ok"] == 12, stat
    conform(fdf, "forecasts")
    conform(vdf, "forecast_valuation")
    assert set(vdf["scenario"].unique()) == {"bear", "base", "bull", "market_implied"}
    for iid in vdf["issuer_id"].unique():
        rows = {r["scenario"]: r for r in vdf.filter(pl.col("issuer_id") == iid).iter_rows(named=True)}
        a = orjson.loads(rows["base"]["assumptions"])
        assert a["bs_max_imbalance"] < 1e-3
        assert not (rows["base"]["flags"] or []) or "share_basis_mismatch" not in rows["base"]["flags"]
        if rows["bear"]["fair_value_per_share"] and rows["bull"]["fair_value_per_share"]:
            assert rows["bear"]["fair_value_per_share"] <= rows["base"]["fair_value_per_share"] \
                <= rows["bull"]["fair_value_per_share"]
        if not rows["base"]["fair_value_dcf"]:
            assert rows["base"]["sensitivity_grid"] is None  # DCF <= 0: grid suppressed, value from multiples
            continue
        grid = orjson.loads(rows["base"]["sensitivity_grid"])
        assert len(grid["wacc"]) == 5 and len(grid["fair_value"][0]) == 5
        mid = grid["fair_value"][2][2]
        assert mid == pytest.approx(rows["base"]["fair_value_per_share"], rel=1e-5, abs=1e-5)
        if mid > 0:  # lower WACC -> higher value (fair values are floored at 0)
            assert grid["fair_value"][1][2] > mid >= grid["fair_value"][3][2]
    base = vdf.filter(pl.col("scenario") == "base")
    assert base["valuation_confidence"].drop_nulls().is_between(0, 100).all()
    assert base["fair_value_multiples"].drop_nulls().len() >= 6
    ext = base.filter(pl.col("upside_pct").abs() > 1.0)
    assert ext.filter(pl.col("valuation_confidence") > 35).height == 0
    # base forecast years follow the last FY; actual anchors flagged
    yrs = fdf.filter((pl.col("scenario") == "base") & (~pl.col("is_actual")))["fiscal_year"].unique().sort()
    assert yrs.to_list() == [2026, 2027, 2028, 2029, 2030]
    assert fdf.filter(pl.col("is_actual"))["fiscal_year"].max() == 2025


def test_reverse_dcf_reproduces_price(inputs):
    """Plugging the implied CAGR back into the model gives the market EV."""
    coc = cc.compute(inputs, AS_OF)
    _, vdf, _ = fc.compute(inputs, AS_OF, coc, erp_mode="fixed")
    mi = vdf.filter((pl.col("scenario") == "market_implied") & pl.col("implied_revenue_cagr_5y").is_not_null())
    assert mi.height >= 6
    r = mi.row(0, named=True)
    assert -0.5 < r["implied_revenue_cagr_5y"] < 1.5 and 0 <= r["implied_vs_hist_growth_pct"] <= 100


def test_implied_erp_calibration_centres_median(inputs):
    coc = cc.compute(inputs, AS_OF)
    _, vdf, stat = fc.compute(inputs, AS_OF, coc, erp_mode="implied")
    erp = stat["erp"].get("ALL")
    assert erp is not None and fc.ERP_BOUNDS[0] <= erp <= fc.ERP_BOUNDS[1]
    up = vdf.filter(pl.col("scenario") == "base")["upside_pct"].drop_nulls()
    if fc.ERP_BOUNDS[0] < erp < fc.ERP_BOUNDS[1]:
        assert abs(up.median()) < 0.05


def test_forecast_is_point_in_time():
    a_in, b_in = make_inputs(), make_inputs(late_restatement=True)
    _, va, _ = fc.compute(a_in, AS_OF, cc.compute(a_in, AS_OF), erp_mode="fixed")
    _, vb, _ = fc.compute(b_in, AS_OF, cc.compute(b_in, AS_OF), erp_mode="fixed")
    cols = ["issuer_id", "scenario", "fair_value_per_share", "implied_revenue_cagr_5y"]
    assert va.sort(["issuer_id", "scenario"]).select(cols).equals(vb.sort(["issuer_id", "scenario"]).select(cols))


def test_bank_excess_return_route():
    inp = make_inputs()
    inp.issuers = inp.issuers.with_columns(
        pl.when(pl.col("issuer_id") == "I000000000011").then(pl.lit("banks_global")).otherwise(pl.col("subsector_id"))
        .alias("subsector_id"),
        pl.when(pl.col("issuer_id") == "I000000000011").then(pl.lit("FINS")).otherwise(pl.col("sector_id"))
        .alias("sector_id"))
    coc = cc.compute(inp, AS_OF)
    assert coc.filter(pl.col("issuer_id") == "I000000000011")["is_financial"][0]
    fdf, vdf, _ = fc.compute(inp, AS_OF, coc, erp_mode="fixed")
    bank = vdf.filter(pl.col("issuer_id") == "I000000000011")
    assert set(bank["model"].unique()) == {"excess_return"}
    assert bank.filter(pl.col("scenario") == "base")["wacc"][0] == pytest.approx(
        bank.filter(pl.col("scenario") == "base")["cost_of_equity"][0])
    items = set(fdf.filter(pl.col("issuer_id") == "I000000000011")["item_id"].unique())
    assert {"residual_income", "drv_roe", "total_equity_parent"} <= items
    assert "implied_roe" in orjson.loads(bank["assumptions"][0])
    assert math.isfinite(bank.filter(pl.col("scenario") == "base")["fair_value_per_share"][0])
    _ = datetime.now(UTC)
