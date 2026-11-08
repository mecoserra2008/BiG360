"""Cross-asset signal math (roadmap v2 #5a): curve carry / slope, momentum / trend / vol, FX pairs from official
fixings, UST carry + roll-down, dividend yield, eigen-concentration, risk composite, quantisation, contract conformity."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from app.analytics import cross_asset as ca
from app.core.schema import conform


def _bdays(n: int, start: date = date(2018, 1, 1)) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_curve_metrics_roll_yield_slope_and_front_rules():
    d = date(2026, 10, 16)
    # contango: F = 100 * exp(0.05 * t_years); a contract 3 days from expiry must be skipped
    rows = [{"root": "X", "date": d, "contract": f"K{i}", "days_to_expiry": dte, "close": 100 * math.exp(0.05 * dte / 365)}
            for i, dte in enumerate([3, 30, 60, 90, 120, 395])]
    rows += [{"root": "Y", "date": d, "contract": "A", "days_to_expiry": 250, "close": 90.0},
             {"root": "Y", "date": d, "contract": "B", "days_to_expiry": 280, "close": 91.0}]
    m = ca.curve_metrics(pl.DataFrame(rows).with_columns(pl.col("days_to_expiry").cast(pl.Int16), pl.col("close").cast(pl.Float32)))
    x = m.filter(pl.col("root") == "X").row(0, named=True)
    assert x["carry_front_contract"] == "K1" and x["carry_dte_front"] == 30 and x["carry_dte_next"] == 60
    assert x["carry_curve"] == pytest.approx(-0.05, abs=2e-4)      # contango -> negative roll yield
    assert x["carry_12m"] == pytest.approx(-0.05, abs=2e-4)        # 30 -> 395 days (gap 365)
    assert x["curve_slope"] == pytest.approx(0.05, abs=2e-4) and x["curve_n"] == 5   # 30..395 days
    y = m.filter(pl.col("root") == "Y").row(0, named=True)
    assert y["carry_curve"] is None and y["carry_front_contract"] is None   # front beyond 200 days: no roll carry
    shapes = pl.DataFrame({"s": [0.05, -0.05, 0.0, None]}).select(ca.shape_label(pl.col("s"))).to_series().to_list()
    assert shapes == ["contango", "backwardation", "flat", None]


def test_price_signals_momentum_trend_and_vol():
    n = 600
    rng = np.random.default_rng(0)
    lr = 0.0005 + 0.01 * rng.standard_normal(n)
    p = 100 * np.exp(np.cumsum(lr))
    df = pl.DataFrame({"asset_id": "A", "date": _bdays(n), "price": p})
    s = ca.price_signals(df)
    last = s.row(-1, named=True)
    lp = np.log(p)
    assert last["mom_1m"] == pytest.approx(lp[-1] - lp[-22], rel=1e-9)
    assert last["mom_12m"] == pytest.approx(lp[-1] - lp[-253], rel=1e-9)
    assert last["mom_12_1"] == pytest.approx(lp[-22] - lp[-253], rel=1e-9)
    assert last["rv_1m"] == pytest.approx(np.std(np.diff(lp)[-21:], ddof=1) * math.sqrt(252), rel=1e-9)
    assert -1 <= last["trend_signal"] <= 1
    # a steady uptrend saturates the signal at +1
    up = ca.price_signals(pl.DataFrame({"asset_id": "U", "date": _bdays(400), "price": 100 * np.exp(0.002 * np.arange(400)
                                                                                                     + 1e-4 * rng.standard_normal(400))}))
    assert up.get_column("trend_signal")[-1] == pytest.approx(1.0)
    assert s.get_column("mom_12m")[:252].null_count() == 252


def test_fx_pair_from_official_fixings():
    d = [date(2026, 10, 15), date(2026, 10, 16)]
    fx = pl.DataFrame({"date": d * 3, "currency": ["USD"] * 2 + ["EUR"] * 2 + ["JPY"] * 2,
                       "usd_per_unit": [1.0, 1.0, 1.10, 1.12, 1 / 150, 1 / 151]})
    eur = ca.fx_pair(fx, "EUR", "USD").get_column("price").to_list()
    jpy = ca.fx_pair(fx, "USD", "JPY").get_column("price").to_list()
    eurjpy = ca.fx_pair(fx, "EUR", "JPY").get_column("price").to_list()
    assert eur == pytest.approx([1.10, 1.12]) and jpy == pytest.approx([150, 151]) and eurjpy == pytest.approx([165, 169.12])


def test_ust_carry_rolldown_and_duration():
    assert pl.select(ca.par_mod_duration(pl.lit(4.0), 10)).item() == pytest.approx(8.18, abs=0.01)
    dates = _bdays(30)
    vals = {"fred:DGS1": 4.0, "fred:DGS2": 4.1, "fred:DGS3": 4.2, "fred:DGS5": 4.4, "fred:DGS7": 4.6,
            "fred:DGS10": 4.9, "fred:DGS20": 5.2, "fred:DGS30": 5.3, "fred:DGS3MO": 4.5}
    rows = [{"series_id": k, "date": d, "knowledge_date": d + timedelta(days=1), "value": v + (0.01 * i if k == "fred:DGS10" else 0)}
            for k, v in vals.items() for i, d in enumerate(dates)]
    u = ca.ust_frames(pl.DataFrame(rows))
    last = u[10].row(-1, named=True)
    y10 = 4.9 + 0.01 * 29
    y9 = 4.6 + (y10 - 4.6) * 2 / 3
    dur = (1 / (y10 / 100)) * (1 - (1 + y10 / 200) ** (-20))
    assert last["carry"] == pytest.approx((y10 - 4.5) / 100 + dur * (y10 - y9) / 100, rel=1e-9)
    assert last["knowledge_date"] == dates[-1] + timedelta(days=1)
    # rising yields -> the synthetic total-return index falls
    assert u[10].get_column("price")[-1] < 1.0 and u[2].get_column("price")[-1] > 1.0


def test_ttm_dividend_yield():
    dates = _bdays(400, date(2025, 1, 1))
    px = pl.DataFrame({"date": dates, "close": [100.0] * 400})
    divs = pl.DataFrame({"security_id": "E", "ex_date": [date(2025, 3, 20), date(2025, 6, 20), date(2025, 9, 19),
                                                           date(2025, 12, 19), date(2026, 3, 27)],
                         "amount": [0.5] * 5})
    dy = ca.ttm_dividend_yield(px, divs)
    assert dy.filter(pl.col("date") < date(2026, 1, 1)).get_column("dividend_yield").null_count() > 0  # < 1y history
    assert dy.filter(pl.col("date") == date(2026, 3, 19)).get_column("dividend_yield").item() == pytest.approx(0.02)
    # 2025-03-20 has left the trailing 365-day window, 2026-03-27 has not arrived yet
    assert dy.filter(pl.col("date") == date(2026, 3, 24)).get_column("dividend_yield").item() == pytest.approx(0.015)
    assert dy.filter(pl.col("date") == date(2026, 3, 27)).get_column("dividend_yield").item() == pytest.approx(0.02)


def test_eigen_concentration_bounds():
    rng = np.random.default_rng(1)
    n, k = 300, 6
    common = rng.standard_normal((n, 1))
    together = common + 0.05 * rng.standard_normal((n, k))
    indep = rng.standard_normal((n, k))
    dates = _bdays(n)
    e1 = ca.eigen_concentration(dates, together, 60).get_column("eig1_share_60").drop_nulls()
    e2 = ca.eigen_concentration(dates, indep, 60).get_column("eig1_share_60").drop_nulls()
    assert e1.min() > 0.95 and e2.max() < 0.5 and e2.min() >= 1 / k
    assert ca.eigen_concentration(dates, together, 60).get_column("eig1_share_60")[:59].null_count() == 59


def test_quantize_f32_precision():
    df = pl.DataFrame({"x": pl.Series([0.123456789, -3.14159265, None, 1e-6], dtype=pl.Float32)})
    q = ca.quantize_f32(df, ["x"]).get_column("x").to_list()
    assert q[2] is None
    for a, b in zip([0.123456789, -3.14159265, 1e-6], [q[0], q[1], q[3]], strict=True):
        assert abs(a - b) <= abs(a) * 2.5e-4


def _synthetic_inputs(n: int = 900):
    dates = _bdays(n, date(2020, 1, 1))
    rng = np.random.default_rng(2)
    p = 50 * np.exp(np.cumsum(0.01 * rng.standard_normal(n)))
    prices = pl.DataFrame({"security_id": "IDX:CL=F", "date": dates, "close": p, "adj_close": p})
    prices = pl.concat([prices, pl.DataFrame({"security_id": "IDX:^OVX", "date": dates, "close": [40.0] * n,
                                              "adj_close": [40.0] * n})])
    fx = pl.DataFrame({"date": dates * 3, "currency": ["USD"] * n + ["EUR"] * n + ["JPY"] * n,
                       "usd_per_unit": [1.0] * n + list(1.1 + 0.01 * np.sin(np.arange(n) / 30)) + [1 / 140] * n})
    policy = pl.DataFrame({"currency": ["USD"] * n + ["EUR"] * n, "date": dates * 2, "rate": [5.0] * n + [3.0] * n})
    months = [date(2014 + k // 12, k % 12 + 1, 15) for k in range(12 * 11)]
    cpi = pl.DataFrame({"country": ["US"] * len(months) + ["EA"] * len(months), "known_date": months * 2,
                        "cpi_yoy": [3.0] * len(months) + [2.0] * len(months)})
    fred = pl.DataFrame(schema={"series_id": pl.Utf8, "date": pl.Date, "knowledge_date": pl.Date, "value": pl.Float64})
    divs = pl.DataFrame(schema={"security_id": pl.Utf8, "ex_date": pl.Date, "amount": pl.Float64})
    curve = pl.DataFrame({"root": "CL", "date": [dates[-1]] * 3, "contract": ["X26", "Z26", "F27"],
                          "days_to_expiry": [20, 50, 80], "close": [80.0, 79.0, 78.0]}).with_columns(
        pl.col("days_to_expiry").cast(pl.Int16), pl.col("close").cast(pl.Float32))
    return dict(prices=prices, fx=fx, fred=fred, policy=policy, cpi=cpi, divs=divs, curve=curve)


def test_compute_signals_conforms_to_contract(tmp_data):
    assets = [a for a in ca.build_assets() if a.asset_id in ("CL", "EURUSD")]
    now = datetime(2026, 10, 19, tzinfo=UTC)
    sig, panel = ca.compute_signals(assets, **_synthetic_inputs(), now=now, start=None)
    out = conform(sig, "cross_asset_signals")
    assert set(out.get_column("asset_id")) == {"CL", "EURUSD"}
    cl = out.filter(pl.col("asset_id") == "CL").row(-1, named=True)
    assert cl["carry_method"] == "futures_roll" and cl["carry"] == pytest.approx(math.log(80 / 79) * 365 / 30, rel=1e-3)
    assert cl["curve_shape"] == "backwardation" and cl["iv"] == pytest.approx(0.40, rel=1e-3) and cl["iv_source"] == "^OVX"
    eu = out.filter(pl.col("asset_id") == "EURUSD").row(-1, named=True)
    assert eu["carry_method"] == "fx_rate_diff" and eu["carry"] == pytest.approx(-0.02, rel=1e-3)
    assert eu["real_rate_diff"] == pytest.approx((3 - 2) - (5 - 3), abs=1e-3)
    assert eu["price_source"] == "official_fx" and eu["cot_index_3y"] is None
    assert eu["value_method"] == "fx_real_5y" and eu["value"] is None   # needs 5 years of price history
    assert out.schema["mom_12m"] == pl.Float32
    assert panel.height == 2 * 900


def test_risk_composite_signs():
    n = 800
    dates = _bdays(n, date(2020, 1, 1))
    t = np.arange(n)
    spx = 100 * np.exp(0.001 * t)
    spx[-60:] = spx[-61] * np.exp(-0.01 * np.arange(1, 61))         # sharp sell-off at the end
    vix = np.full(n, 15.0)
    vix[-60:] = 45.0
    panel = pl.DataFrame({"asset_id": "SPX", "date": dates, "price": spx})
    prices = pl.concat([pl.DataFrame({"security_id": sid, "date": dates, "close": v, "adj_close": v})
                        for sid, v in (("IDX:^VIX", vix + 0.1 * np.sin(t)), ("IDX:^VIX3M", np.full(n, 18.0) + 0.1 * np.cos(t)),
                                       ("ETF:HYG", 80 + np.sin(t / 7)), ("ETF:IEF", 95 + np.cos(t / 9)),
                                       ("IDX:HG=F", 4 + 0.1 * np.sin(t / 11)), ("IDX:GC=F", 2000 + 10 * np.cos(t / 13)))])
    fx = pl.DataFrame({"date": dates * 2, "currency": ["AUD"] * n + ["JPY"] * n,
                       "usd_per_unit": list(0.7 + 0.01 * np.sin(t / 17)) + [1 / 140] * n})
    fred = pl.DataFrame(schema={"series_id": pl.Utf8, "date": pl.Date, "knowledge_date": pl.Date, "value": pl.Float64})
    rc = ca.risk_composite(panel, prices, fred, fx)
    last = rc.row(-1, named=True)
    assert last["comp_vix"] < -1 and last["comp_equity_trend"] < -1
    assert last["risk_n_components"] == 6 and last["risk_regime"] == "risk_off"
