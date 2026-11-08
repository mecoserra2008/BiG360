"""Cross-asset signals (roadmap v2 item #5a) -> cross_asset_signals, cross_asset_regime, cross_asset_corr.
Job `cross_asset_signals` (nightly, after futures_curve / prices_finalize / fx_rates / cb_rates / macro_fred).
Every definition is also written up in docs/CROSS_ASSET_DEFINITIONS.md.

Universe (ASSETS): commodities (every commodity futures root of app.ingestion.futures_curve.ROOTS, priced from the Yahoo
continuous front month IDX:<root>=F), FX pairs vs USD (OFFICIAL fx_rates fixings: ECB > FRED > central bank,
forward-filled rows excluded), equity indices (cash index levels, Yahoo), US Treasuries 2/5/10/30y (synthetic
constant-maturity total-return index built from OFFICIAL FRED H.15 CMT yields), US credit (HYG / LQD total return
plus OFFICIAL ICE BofA OAS from FRED) and crypto futures (CME BTC / ETH front month).

Per asset and trading day (all returns are log returns; windows count rows = trading days of the asset):
  mom_1m / mom_3m / mom_12m   ln(P_t / P_{t-h}), h = 21 / 63 / 252; mom_12_1 = ln(P_{t-21} / P_{t-252})
  trend_signal (MODEL-BASED)  mean over h in {21, 63, 252} of clip(mom_h / (sigma_63 * sqrt(h)), -2, 2) / 2, where sigma_63 is
                              the 63-day std of daily log returns; it lies in [-1, 1] and needs >= 2 horizons
  rv_1m / rv_3m               std of daily log returns over 21 / 63 days (min 15 / 45) * sqrt(252)
  iv                          implied-vol index close / 100 (VIX family, OVX, GVZ, INDIAVIX, AXVI); UST10Y uses ^MOVE (bp)
  iv_rv_ratio                 iv / rv_1m (UST10Y: MOVE / realised bp vol of the 10y CMT yield, 21 days)
  carry (annualised fraction, by asset class; carry_method says which):
    futures_roll          commodities, crypto: ln(F1 / F2) * 365 / (T2 - T1), where F1 / F2 are the nearest two contracts with
                          >= 5 days to expiry and the front within 200 days (positive = backwardation)
    fx_rate_diff          FX BASE/QUOTE: (policy rate BASE - policy rate QUOTE) / 100, OFFICIAL cb_policy_rates
    bond_carry_rolldown   UST T: (y_T - y_3m) / 100 + D_mod(T, y_T) * (y_T - y_{T-1}) / 100 (yield over funding plus 1-year
                          roll-down on the official CMT curve, linear interpolation; D_mod = par-bond modified duration)
    equity_div_funding    trailing 12m cash distributions of the tracking ETF / ETF close - local policy rate / 100
    credit_oas            OAS / 100 (spread over duration-matched Treasuries; expected default losses NOT deducted)
  carry_curve / carry_12m     futures roll yield from the curve (front vs next; front vs the contract ~1 year further out,
                              seasonality-neutral) wherever a curve exists. FX pairs quoted USD/X use the sign-flipped
                              curve of the X future (6J for USDJPY)
  curve_slope                 OLS slope of ln F on years to expiry over contracts with 5..400 days to expiry (>= 3);
                              positive = contango. curve_shape: contango > +1 %/yr, backwardation < -1 %/yr, else flat
  value (value_method):
    reversal_5y           commodities, equity indices: ln(mean P over t-1386..t-1134) - ln(P_t) (4.5-5.5y ago vs now)
    fx_real_5y            -(5y change of ln S + 5y CPI inflation BASE - 5y CPI inflation QUOTE): minus the 5y real
                          appreciation of BASE (OFFICIAL CPI y/y, macro_cell_history, known ~46 days after the period start)
    real_yield            UST5Y / UST10Y: TIPS real yield DFII5 / DFII10 (percent), OFFICIAL FRED
    oas_level             credit: OAS (percent)
  real_rate_diff              FX: (policy - CPI y/y) BASE - (policy - CPI y/y) QUOTE, in percentage points
  z_*                         time-series z-score: (x - rolling mean) / rolling std over 756 rows (min 126)
  cot_*                       CFTC positioning (OFFICIAL Commitments of Traders, cftc_signals) joined by join_positioning():
                              market = cftc_markets.root == futures_root; report disagg_fut / managed money for
                              commodities, tff_fut / leveraged funds for financial futures; the latest report RELEASED
                              (knowledge_date) on or before the row date, stale beyond COT_MAX_AGE_DAYS -> null; sign-
                              flipped for pairs quoted inversely to the futures (USDJPY vs 6J: index 1 - x, z / % OI negated)

PIT: rows are dated by the asset's trading date; FRED inputs are as-of their knowledge_date (H.15 = next day), CPI as-of
period start + 46 days, policy rates as in force on the date, dividends by ex date. UST rows carry FRED's knowledge_date.

cross_asset_regime (per date, fixed basket CORR_BASKET, only while every member has data):
  eig1_share_w   largest eigenvalue / N of the w-day (60 / 120) correlation matrix of daily log returns ("everything
                 moving together"; 1/N = no common factor, 1 = one factor). Prices are forward-filled up to 3 days on the
                 union calendar, rows need every member. Closes are asynchronous across time zones (Asia / Europe /
                 US), which biases cross-region correlations down.
  avg_corr_w     mean off-diagonal correlation; eig1_z_120 = time-series z of eig1_share_120 (756 / 252).
  risk_composite MODEL-BASED risk-on / risk-off gauge: mean of the available component z-scores (>= 4 of 8; each
                 clipped to +-3, z over 756 days min 252, sign so that + = risk-on): S&P 500 3m momentum (+), VIX (-),
                 VIX / VIX3M term structure (-), HYG vs IEF 3m relative return (+), HY OAS (-), copper / gold 3m (+),
                 AUD/JPY 3m (+, official FX), Fed broad dollar index 3m (-, official). risk_regime: risk_on > 0.5,
                 risk_off < -0.5, else neutral.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger

log = get_logger("app.analytics.cross_asset")

Z_WIN, Z_MIN = 756, 126
REG_Z_MIN = 252
CPI_LAG_DAYS = 46
FRONT_MAX_DTE = 200
SLOPE_MAX_DTE = 400
SHAPE_EPS = 0.01
QUANT_BITS = 12
#: positioning join: asset class -> (cftc report type, trader category). Commodities: disaggregated managed money;
#: financial futures: TFF leveraged funds (the speculative category; asset managers are structural real-money longs)
COT_SPEC: dict[str, tuple[str, str]] = {"commodity": ("disagg_fut", "m_money")}
COT_SPEC_DEFAULT = ("tff_fut", "lev_money")
COT_MAX_AGE_DAYS = 28


# ------------------------------------------------------------------------------------------------ universe


@dataclass(frozen=True)
class Asset:
    asset_id: str
    name: str
    asset_class: str                 # commodity | fx | equity_index | rates | credit | crypto
    sector: str | None = None        # commodity sector / region
    security_id: str | None = None   # prices_daily id ('px' assets)
    fx: tuple[str, str] | None = None  # (base, quote): price = quote per base (official fx_rates)
    tenor: int | None = None         # UST tenor (rates)
    futures_root: str | None = None  # futures_curve root (carry_curve / curve_slope; CFTC join key)
    curve_sign: int = 1              # -1 when the futures quote is the inverse of the asset (6J for USDJPY)
    iv_id: str | None = None         # implied-vol index security_id
    etf: str | None = None           # dividend source (equity) / total-return price (credit)
    ccy: str | None = None           # funding currency (equity)
    oas: str | None = None           # FRED OAS series (credit)
    real_yield: str | None = None    # FRED TIPS series (rates)


def _commodities() -> list[Asset]:
    from app.ingestion.futures_curve import ROOTS  # noqa: PLC0415

    iv = {"CL": "IDX:^OVX", "GC": "IDX:^GVZ"}
    return [Asset(r.root, r.name, "commodity", r.asset_class, security_id=r.front_security_id, futures_root=r.root,
                  iv_id=iv.get(r.root))
            for r in ROOTS if r.asset_class in ("energy", "metals", "grains", "softs", "livestock")]


_EQ = [  # asset_id, name, index id, futures root, iv, dividend ETF, currency, region
    ("SPX", "S&P 500", "IDX:^GSPC", "ES", "IDX:^VIX", "ETF:SPY", "USD", "US"),
    ("NDX", "Nasdaq-100", "IDX:^NDX", "NQ", "IDX:^VXN", "ETF:QQQ", "USD", "US"),
    ("RUT", "Russell 2000", "IDX:^RUT", "RTY", None, "ETF:IWM", "USD", "US"),
    ("DJI", "Dow Jones Industrial Average", "IDX:^DJI", "YM", "IDX:^VXD", "ETF:DIA", "USD", "US"),
    ("SX5E", "EURO STOXX 50", "IDX:^STOXX50E", None, None, "ETF:FEZ", "EUR", "EU"),
    ("DAX", "DAX", "IDX:^GDAXI", None, None, "ETF:EWG", "EUR", "EU"),
    ("UKX", "FTSE 100", "IDX:^FTSE", None, None, "ETF:EWU", "GBP", "UK"),
    ("SMI", "SMI", "IDX:^SSMI", None, None, "ETF:EWL", "CHF", "EU"),
    ("NKY", "Nikkei 225", "IDX:^N225", None, None, "ETF:EWJ", "JPY", "JP"),
    ("HSI", "Hang Seng", "IDX:^HSI", None, None, "ETF:EWH", "HKD", "HK"),
    ("CSI300", "CSI 300", "IDX:000300.SS", None, None, "ETF:ASHR", "CNY", "CN"),
    ("KOSPI", "KOSPI Composite", "IDX:^KS11", None, None, "ETF:EWY", "KRW", "KR"),
    ("NIFTY", "Nifty 50", "IDX:^NSEI", None, "IDX:^INDIAVIX", "ETF:INDA", "INR", "APAC"),
    ("AS51", "S&P/ASX 200", "IDX:^AXJO", None, "IDX:^AXVI", "ETF:EWA", "AUD", "APAC"),
    ("TSX", "S&P/TSX Composite", "IDX:^GSPTSE", None, None, "ETF:EWC", "CAD", "CA"),
    ("IBOV", "Ibovespa", "IDX:^BVSP", None, None, "ETF:EWZ", "BRL", "LATAM"),
    ("MEXBOL", "S&P/BMV IPC", "IDX:^MXX", None, None, "ETF:EWW", "MXN", "LATAM"),
    ("EEM", "MSCI Emerging Markets (ETF)", "ETF:EEM", None, None, "ETF:EEM", "USD", "EM"),
]
_FX = [  # base, quote, futures root, sign
    ("EUR", "USD", "6E", 1), ("GBP", "USD", "6B", 1), ("AUD", "USD", None, 1), ("NZD", "USD", None, 1),
    ("USD", "JPY", "6J", -1), ("USD", "CHF", None, 1), ("USD", "CAD", None, 1), ("USD", "SEK", None, 1),
    ("USD", "NOK", None, 1), ("USD", "MXN", None, 1), ("USD", "BRL", None, 1), ("USD", "CLP", None, 1),
    ("USD", "COP", None, 1), ("USD", "PEN", None, 1), ("USD", "INR", None, 1), ("USD", "KRW", None, 1),
    ("USD", "CNY", None, 1),
]


def build_assets() -> list[Asset]:
    out = _commodities()
    out += [Asset(a, n, "equity_index", reg, security_id=sid, futures_root=root, iv_id=iv, etf=etf, ccy=ccy)
            for a, n, sid, root, iv, etf, ccy, reg in _EQ]
    out += [Asset(f"{b}{q}", f"{b}/{q} (official fixings)", "fx", "G10" if q in ("USD", "JPY", "CHF", "CAD", "SEK", "NOK")
                  and b in ("EUR", "GBP", "AUD", "NZD", "USD") else "EM", fx=(b, q), futures_root=root, curve_sign=sg)
            for b, q, root, sg in _FX]
    out += [
        Asset("UST2Y", "US Treasury 2y (synthetic CMT)", "rates", "US", tenor=2, futures_root="ZT"),
        Asset("UST5Y", "US Treasury 5y (synthetic CMT)", "rates", "US", tenor=5, futures_root="ZF", real_yield="fred:DFII5"),
        Asset("UST10Y", "US Treasury 10y (synthetic CMT)", "rates", "US", tenor=10, futures_root="ZN",
              iv_id="IDX:^MOVE", real_yield="fred:DFII10"),
        Asset("UST30Y", "US Treasury 30y (synthetic CMT)", "rates", "US", tenor=30, futures_root="ZB"),
        Asset("US_HY", "US high yield (HYG, ICE BofA HY OAS)", "credit", "US", security_id="ETF:HYG", etf="ETF:HYG",
              oas="fred:BAMLH0A0HYM2"),
        Asset("US_IG", "US investment grade (LQD, ICE BofA IG OAS)", "credit", "US", security_id="ETF:LQD",
              etf="ETF:LQD", oas="fred:BAMLC0A0CM"),
        Asset("BTC", "Bitcoin (CME front month)", "crypto", None, security_id="IDX:BTC=F", futures_root="BTC"),
        Asset("ETH", "Ether (CME front month)", "crypto", None, security_id="IDX:ETH=F", futures_root="ETH"),
    ]
    return out


def asset_registry(assets: list[Asset] | None = None) -> pl.DataFrame:
    """Serving / API view of the universe: one row per asset with display metadata, the instrument page to link
    (instrument_id), every instrument id whose page shows this asset (linked_ids) and the COT report / category used."""
    from app.ingestion.futures_curve import ROOT_BY_ID  # noqa: PLC0415

    rows = []
    for i, a in enumerate(assets or build_assets()):
        r = ROOT_BY_ID.get(a.futures_root or "")
        fx_id = f"IDX:{a.fx[0]}{a.fx[1]}=X" if a.fx else None
        front = f"IDX:{a.futures_root}=F" if a.futures_root else None
        inst = fx_id or (a.security_id if a.asset_class != "rates" else None) or front
        linked = list(dict.fromkeys(x for x in (a.security_id, fx_id, front, a.etf) if x))
        rt, cat = cot_spec(a.asset_class) if a.futures_root else (None, None)
        rows.append({"asset_id": a.asset_id, "name": a.name, "asset_class": a.asset_class, "sector": a.sector,
                     "sort_order": i, "security_id": a.security_id, "instrument_id": inst, "linked_ids": linked,
                     "futures_root": a.futures_root, "root_name": r.name if r else None,
                     "root_exchange": r.exchange if r else None, "curve_sign": a.curve_sign,
                     "iv_source": a.iv_id.removeprefix("IDX:") if a.iv_id else None, "etf": a.etf,
                     "fx_base": a.fx[0] if a.fx else None, "fx_quote": a.fx[1] if a.fx else None, "tenor": a.tenor,
                     "cot_report_type": rt, "cot_category": cat})
    return pl.DataFrame(rows, schema={"asset_id": pl.Utf8, "name": pl.Utf8, "asset_class": pl.Utf8, "sector": pl.Utf8,
                                      "sort_order": pl.Int32, "security_id": pl.Utf8, "instrument_id": pl.Utf8,
                                      "linked_ids": pl.List(pl.Utf8), "futures_root": pl.Utf8, "root_name": pl.Utf8,
                                      "root_exchange": pl.Utf8, "curve_sign": pl.Int8, "iv_source": pl.Utf8,
                                      "etf": pl.Utf8, "fx_base": pl.Utf8, "fx_quote": pl.Utf8, "tenor": pl.Int16,
                                      "cot_report_type": pl.Utf8, "cot_category": pl.Utf8})


#: currency -> (central bank code in cb_policy_rates, CPI country in macro_cell_history)
CCY_BANK = {"USD": "fed", "EUR": "ecb", "GBP": "boe", "JPY": "boj", "CHF": "snb", "CAD": "boc", "AUD": "rba",
            "SEK": "riksbank", "NOK": "norges", "BRL": "bcb", "MXN": "banxico", "CLP": "bcch", "COP": "banrep",
            "PEN": "bcrp", "INR": "rbi", "KRW": "bok", "CNY": "pboc"}
CCY_CPI = {"USD": "US", "EUR": "EA", "GBP": "GB", "JPY": "JP", "CHF": "CH", "CAD": "CA", "AUD": "AU", "BRL": "BR",
           "MXN": "MX", "CLP": "CL", "COP": "CO", "PEN": "PE", "INR": "IN", "KRW": "KR", "CNY": "CN", "HKD": "HK"}
UST_POINTS = {1: "fred:DGS1", 2: "fred:DGS2", 3: "fred:DGS3", 5: "fred:DGS5", 7: "fred:DGS7", 10: "fred:DGS10",
              20: "fred:DGS20", 30: "fred:DGS30"}
UST_FUNDING = "fred:DGS3MO"
#: fixed basket of the correlation regime (asset_ids)
CORR_BASKET = ("SPX", "SX5E", "NKY", "EEM", "CL", "GC", "HG", "UST10Y", "EURUSD", "USDJPY", "AUDUSD", "US_HY")


# ------------------------------------------------------------------------------------------------ loaders


def _empty_px() -> pl.DataFrame:
    return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "close": pl.Float64, "adj_close": pl.Float64})


def load_prices(ids: list[str], settings: Settings | None = None) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    if not ids:
        return _empty_px()
    return (scan_table("prices_daily", settings).filter(pl.col("security_id").is_in(ids))
            .select("security_id", "date", "close", "adj_close").collect()
            .filter(pl.col("close").is_not_null() & (pl.col("close") > 0)).sort("security_id", "date"))


def load_fred(series: list[str], settings: Settings | None = None) -> pl.DataFrame:
    """[series_id, date, knowledge_date, value]: first release per observation date (rates are not revised)."""
    from app.core.storage import scan_table  # noqa: PLC0415

    return (scan_table("macro_observations", settings).filter(pl.col("series_id").is_in(series))
            .select("series_id", "date", "knowledge_date", "value").collect()
            .filter(pl.col("value").is_not_null()).sort("series_id", "date", "knowledge_date")
            .unique(subset=["series_id", "date"], keep="first", maintain_order=True))


def load_fx(settings: Settings | None = None) -> pl.DataFrame:
    """Official fixings [date, currency, usd_per_unit] excluding forward-filled holiday rows."""
    from app.core.storage import scan_table  # noqa: PLC0415

    return (scan_table("fx_rates", settings).filter(pl.col("source_id") != "filled")
            .select("date", "currency", "usd_per_unit").collect().sort("currency", "date"))


def load_policy(settings: Settings | None = None) -> pl.DataFrame:
    """[currency, date, rate] official policy rates (percent)."""
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        cb = read_table("cb_policy_rates", columns=["bank", "date", "policy_rate"], settings=settings)
    except Exception:  # noqa: BLE001
        return pl.DataFrame(schema={"currency": pl.Utf8, "date": pl.Date, "rate": pl.Float64})
    inv = {v: k for k, v in CCY_BANK.items()}
    return (cb.filter(pl.col("bank").is_in(list(inv)) & pl.col("policy_rate").is_not_null())
            .select(pl.col("bank").replace_strict(inv).alias("currency"), "date", pl.col("policy_rate").alias("rate"))
            .sort("currency", "date"))


def load_cpi(settings: Settings | None = None) -> pl.DataFrame:
    """[country, known_date, cpi_yoy]: primary CPI y/y per country, known period start + 46 days (approximation)."""
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        h = read_table("macro_cell_history", columns=["country", "indicator", "is_primary", "date", "value", "frequency"],
                       settings=settings)
    except Exception:  # noqa: BLE001
        return pl.DataFrame(schema={"country": pl.Utf8, "known_date": pl.Date, "cpi_yoy": pl.Float64})
    return (h.filter((pl.col("indicator") == "cpi_yoy") & pl.col("is_primary") & pl.col("value").is_not_null())
            .sort("country", "date").unique(subset=["country", "date"], keep="first", maintain_order=True)
            .select("country", (pl.col("date") + pl.duration(days=CPI_LAG_DAYS)).alias("known_date"),
                    pl.col("value").alias("cpi_yoy")).sort("country", "known_date"))


def load_dividends(ids: list[str], settings: Settings | None = None) -> pl.DataFrame:
    from app.core.storage import scan_table  # noqa: PLC0415

    try:
        return (scan_table("corporate_actions", settings)
                .filter(pl.col("security_id").is_in(ids) & (pl.col("action_type") == "cash_dividend")
                        & pl.col("amount").is_not_null())
                .select("security_id", "ex_date", "amount").collect().sort("security_id", "ex_date"))
    except Exception:  # noqa: BLE001
        return pl.DataFrame(schema={"security_id": pl.Utf8, "ex_date": pl.Date, "amount": pl.Float64})


def load_curve(settings: Settings | None = None) -> pl.DataFrame:
    from app.core.storage import scan_table, table_dir  # noqa: PLC0415

    if not table_dir("futures_curve", settings).exists():
        return pl.DataFrame(schema={"root": pl.Utf8, "date": pl.Date, "contract": pl.Utf8, "days_to_expiry": pl.Int16,
                                    "close": pl.Float32})
    return scan_table("futures_curve", settings).select("root", "date", "contract", "days_to_expiry", "close").collect()


# ------------------------------------------------------------------------------------------------ building blocks


def asof(left: pl.DataFrame, right: pl.DataFrame, left_on: str, right_on: str, by: str | None = None,
         tolerance: str | None = None) -> pl.DataFrame:
    """Backward as-of join (sorts both sides)."""
    lsort = [by, left_on] if by else [left_on]
    rsort = [by, right_on] if by else [right_on]
    return left.sort(lsort).join_asof(right.sort(rsort), left_on=left_on, right_on=right_on, by=by,
                                      strategy="backward", tolerance=tolerance, check_sortedness=False)


def curve_metrics(curve: pl.DataFrame, min_dte: int = 5, front_max: int = FRONT_MAX_DTE,
                  slope_max: int = SLOPE_MAX_DTE) -> pl.DataFrame:
    """Per (root, date): roll yield front vs next, 12m roll yield, OLS slope of ln F on years, shape label."""
    cols = {"root": pl.Utf8, "date": pl.Date, "carry_curve": pl.Float64, "carry_12m": pl.Float64,
            "carry_front_contract": pl.Utf8, "carry_dte_front": pl.Int16, "carry_dte_next": pl.Int16,
            "curve_slope": pl.Float64, "curve_n": pl.Int16}
    if curve.is_empty():
        return pl.DataFrame(schema=cols)
    c = (curve.filter((pl.col("days_to_expiry") >= min_dte) & (pl.col("close") > 0))
         .with_columns(pl.col("close").cast(pl.Float64).log().alias("lf"), (pl.col("days_to_expiry").cast(pl.Float64)).alias("t"))
         .sort("root", "date", "t"))
    g = c.group_by("root", "date").agg(
        pl.col("lf").first().alias("lf1"), pl.col("t").first().alias("t1"), pl.col("contract").first().alias("k1"),
        pl.col("lf").slice(1, 1).first().alias("lf2"), pl.col("t").slice(1, 1).first().alias("t2"),
        pl.col("lf").alias("lfs"), pl.col("t").alias("ts"))
    # 12m: contract whose dte is closest to t1 + 365 within [t1 + 300, t1 + 430]
    tgt = g.explode("lfs", "ts").filter((pl.col("ts") >= pl.col("t1") + 300) & (pl.col("ts") <= pl.col("t1") + 430)) \
        .with_columns((pl.col("ts") - pl.col("t1") - 365).abs().alias("gap")).sort("gap") \
        .group_by("root", "date").agg(pl.col("lfs").first().alias("lf12"), pl.col("ts").first().alias("t12"))
    # slope over 5..slope_max days
    sl = c.filter(pl.col("t") <= slope_max).with_columns((pl.col("t") / 365.0).alias("x")).group_by("root", "date").agg(
        (((pl.col("x") - pl.col("x").mean()) * (pl.col("lf") - pl.col("lf").mean())).sum()
         / ((pl.col("x") - pl.col("x").mean()) ** 2).sum()).alias("curve_slope"),
        pl.len().cast(pl.Int16).alias("curve_n"))
    sl = sl.with_columns(pl.when(pl.col("curve_n") >= 3).then(pl.col("curve_slope")).otherwise(None).alias("curve_slope"))
    out = g.join(tgt, on=["root", "date"], how="left").join(sl, on=["root", "date"], how="left")
    ok = (pl.col("t1") <= front_max) & pl.col("t2").is_not_null() & (pl.col("t2") > pl.col("t1"))
    out = out.select(
        "root", "date",
        pl.when(ok).then((pl.col("lf1") - pl.col("lf2")) * 365.0 / (pl.col("t2") - pl.col("t1"))).otherwise(None).alias("carry_curve"),
        pl.when((pl.col("t1") <= front_max) & pl.col("t12").is_not_null())
        .then((pl.col("lf1") - pl.col("lf12")) * 365.0 / (pl.col("t12") - pl.col("t1"))).otherwise(None).alias("carry_12m"),
        pl.when(pl.col("t1") <= front_max).then(pl.col("k1")).otherwise(None).alias("carry_front_contract"),
        pl.when(pl.col("t1") <= front_max).then(pl.col("t1")).otherwise(None).cast(pl.Int16).alias("carry_dte_front"),
        pl.when(ok).then(pl.col("t2")).otherwise(None).cast(pl.Int16).alias("carry_dte_next"),
        "curve_slope", pl.col("curve_n").fill_null(0).cast(pl.Int16),
    )
    return out.sort("root", "date")


def shape_label(slope: pl.Expr) -> pl.Expr:
    return (pl.when(slope.is_null()).then(None).when(slope > SHAPE_EPS).then(pl.lit("contango"))
            .when(slope < -SHAPE_EPS).then(pl.lit("backwardation")).otherwise(pl.lit("flat")))


def par_mod_duration(y_pct: pl.Expr, tenor: float) -> pl.Expr:
    """Modified duration of a semi-annual par bond with coupon = yield (approximation)."""
    y = (y_pct / 100.0).clip(1e-6, None)
    return (1.0 / y) * (1.0 - (1.0 + y / 2.0) ** (-2.0 * tenor))


def _interp_yield(wide: pl.DataFrame, t: float) -> pl.Expr:
    pts = sorted(UST_POINTS)
    lo = max(p for p in pts if p <= t)
    hi = min(p for p in pts if p >= t)
    if lo == hi:
        return pl.col(f"y{lo}")
    w = (t - lo) / (hi - lo)
    return pl.col(f"y{lo}") * (1 - w) + pl.col(f"y{hi}") * w


def ust_frames(fred: pl.DataFrame) -> dict[int, pl.DataFrame]:
    """tenor -> [date, knowledge_date, price (synthetic TR index), yld, carry, rv_bp_1m] from official CMT yields."""
    want = {**{f"y{t}": s for t, s in UST_POINTS.items()}, "y3m": UST_FUNDING}
    parts = []
    for col, sid in want.items():
        f = fred.filter(pl.col("series_id") == sid).select("date", "knowledge_date", pl.col("value").alias(col))
        parts.append(f)
    if not parts or any(p.is_empty() for p in parts[:1]):
        return {}
    wide = parts[0]
    for p in parts[1:]:
        wide = wide.join(p.drop("knowledge_date"), on="date", how="left")
    wide = wide.sort("date")
    out: dict[int, pl.DataFrame] = {}
    for tenor in (2, 5, 10, 30):
        yc = f"y{tenor}"
        if yc not in wide.columns:
            continue
        f = wide.filter(pl.col(yc).is_not_null()).sort("date")
        dur = par_mod_duration(pl.col(yc), tenor)
        f = f.with_columns(dur.alias("dur"), _interp_yield(f, tenor - 1).alias("yprev"))
        f = f.with_columns(
            ((pl.col(yc).shift(1) / 100.0) * (pl.col("date") - pl.col("date").shift(1)).dt.total_days() / 365.0
             - pl.col("dur").shift(1) * (pl.col(yc) - pl.col(yc).shift(1)) / 100.0).alias("r"),
            ((pl.col(yc) - pl.col(yc).shift(1)) * 100.0).alias("dy_bp"))
        f = f.with_columns((1.0 + pl.col("r").fill_null(0.0)).log().cum_sum().exp().alias("price"))
        f = f.with_columns(
            ((pl.col(yc) - pl.col("y3m")) / 100.0 + pl.col("dur") * (pl.col(yc) - pl.col("yprev")) / 100.0).alias("carry"),
            (pl.col("dy_bp").rolling_std(21, min_samples=15) * math.sqrt(252)).alias("rv_bp_1m"),
            (pl.col(yc) - pl.col("y3m")).alias("term_spread"))
        out[tenor] = f.select("date", "knowledge_date", "price", pl.col(yc).alias("yld"), "carry", "rv_bp_1m")
    return out


def fx_pair(fx: pl.DataFrame, base: str, quote: str) -> pl.DataFrame:
    """[date, price] quote units per 1 base from official fixings (dates where both legs are fixed)."""
    b = fx.filter(pl.col("currency") == base).select("date", pl.col("usd_per_unit").alias("b"))
    q = fx.filter(pl.col("currency") == quote).select("date", pl.col("usd_per_unit").alias("q"))
    if base == "USD":
        b = q.select("date", pl.lit(1.0).alias("b"))
    if quote == "USD":
        q = b.select("date", pl.lit(1.0).alias("q"))
    return b.join(q, on="date", how="inner").select("date", (pl.col("b") / pl.col("q")).alias("price")) \
        .filter(pl.col("price").is_finite() & (pl.col("price") > 0)).sort("date")


def _infl(dates: pl.DataFrame, cpi: pl.DataFrame, country: str | None) -> pl.DataFrame:
    """[date, infl_5y, cpi_yoy]: sum_{k=0..4} ln(1 + cpi_yoy(as-of date - 365k) / 100), official y/y as known then."""
    if not country:
        return dates.select("date", pl.lit(None, pl.Float64).alias("infl_5y"), pl.lit(None, pl.Float64).alias("cpi_yoy"))
    c = cpi.filter(pl.col("country") == country).select("known_date", "cpi_yoy")
    acc = dates.select("date")
    for k in range(5):
        lag = acc.select("date", (pl.col("date") - pl.duration(days=365 * k)).alias("d_k")).sort("d_k")
        if c.is_empty():
            j = lag.select("date", pl.lit(None, pl.Float64).alias(f"c{k}"))
        else:
            j = asof(lag, c, "d_k", "known_date", tolerance="75d").select("date", pl.col("cpi_yoy").alias(f"c{k}"))
        acc = acc.join(j, on="date", how="left")
    full = pl.all_horizontal([pl.col(f"c{k}").is_not_null() for k in range(5)])
    tot = pl.sum_horizontal([(1.0 + pl.col(f"c{k}") / 100.0).log() for k in range(5)])
    return acc.select("date", pl.when(full).then(tot).otherwise(None).alias("infl_5y"), pl.col("c0").alias("cpi_yoy"))


def _rate(dates: pl.DataFrame, policy: pl.DataFrame, ccy: str | None, name: str) -> pl.DataFrame:
    p = policy.filter(pl.col("currency") == (ccy or "")).select(pl.col("date").alias("rd"), pl.col("rate").alias(name))
    if p.is_empty():
        return dates.select("date", pl.lit(None, pl.Float64).alias(name))
    return asof(dates.select("date"), p, "date", "rd", tolerance="10d").select("date", name)


def ttm_dividend_yield(px: pl.DataFrame, divs: pl.DataFrame) -> pl.DataFrame:
    """[date, dividend_yield]: trailing-365-day cash distributions / close (needs 365 days of ETF history)."""
    if px.is_empty():
        return pl.DataFrame(schema={"date": pl.Date, "dividend_yield": pl.Float64})
    d = divs.group_by("ex_date").agg(pl.col("amount").sum()).rename({"ex_date": "date"})
    cal = pl.concat([px.select("date"), d.select("date")]).unique().sort("date")
    s = cal.join(d, on="date", how="left").with_columns(pl.col("amount").fill_null(0.0)) \
        .with_columns(pl.col("amount").rolling_sum_by("date", window_size="365d").alias("ttm"))
    first = px.get_column("date").min()
    out = px.select("date", "close").join(s.select("date", "ttm"), on="date", how="left")
    return out.select("date", pl.when((pl.col("date") >= first + timedelta(days=365)) & (pl.col("ttm") > 0))
                      .then(pl.col("ttm") / pl.col("close")).otherwise(None).alias("dividend_yield"))


def tsz(col: str, by: str = "asset_id", w: int = Z_WIN, minp: int = Z_MIN) -> pl.Expr:
    x = pl.col(col)
    m = x.rolling_mean(w, min_samples=minp).over(by)
    sd = x.rolling_std(w, min_samples=minp).over(by)
    return pl.when(sd > 0).then((x - m) / sd).otherwise(None)


def price_signals(df: pl.DataFrame) -> pl.DataFrame:
    """df [asset_id, date, price] -> + momentum, trend_signal, rv (sorted by asset_id, date)."""
    a = "asset_id"
    df = df.sort(a, "date").with_columns(pl.col("price").log().alias("_lp"))
    df = df.with_columns((pl.col("_lp") - pl.col("_lp").shift(1)).over(a).alias("_lr"))
    df = df.with_columns(
        (pl.col("_lp") - pl.col("_lp").shift(21)).over(a).alias("mom_1m"),
        (pl.col("_lp") - pl.col("_lp").shift(63)).over(a).alias("mom_3m"),
        (pl.col("_lp") - pl.col("_lp").shift(252)).over(a).alias("mom_12m"),
        (pl.col("_lp").shift(21) - pl.col("_lp").shift(252)).over(a).alias("mom_12_1"),
        (pl.col("_lr").rolling_std(21, min_samples=15).over(a) * math.sqrt(252)).alias("rv_1m"),
        (pl.col("_lr").rolling_std(63, min_samples=45).over(a) * math.sqrt(252)).alias("rv_3m"),
        pl.col("_lr").rolling_std(63, min_samples=45).over(a).alias("_sd"),
        # 5y reversal value: mean log price 4.5-5.5 years ago
        pl.col("_lp").shift(1134).rolling_mean(253, min_samples=200).over(a).alias("_lp5"),
    )
    parts = [((pl.col(f"mom_{k}") / (pl.col("_sd") * math.sqrt(h))).clip(-2, 2) / 2)
             for k, h in (("1m", 21), ("3m", 63), ("12m", 252))]
    n_ok = pl.sum_horizontal([p.is_not_null().cast(pl.Int8) for p in parts])
    df = df.with_columns(pl.when(n_ok >= 2).then(pl.mean_horizontal(parts)).otherwise(None).alias("trend_signal"),
                         (pl.col("_lp5") - pl.col("_lp")).alias("reversal_5y"))
    return df.drop("_lp", "_lr", "_sd", "_lp5")


# ------------------------------------------------------------------------------------------------ signals


def compute_signals(assets: list[Asset], *, prices: pl.DataFrame, fx: pl.DataFrame, fred: pl.DataFrame,
                    policy: pl.DataFrame, cpi: pl.DataFrame, divs: pl.DataFrame, curve: pl.DataFrame,
                    run_id: str | None = None, now: datetime | None = None,
                    start: date | None = date(2000, 1, 1)) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (cross_asset_signals rows, long price panel [asset_id, date, price] used by the regime)."""
    now = now or datetime.now(UTC)
    ust = ust_frames(fred)
    cm = curve_metrics(curve)
    frames: list[pl.DataFrame] = []
    panel: list[pl.DataFrame] = []
    for a in assets:
        extra: dict[str, Any] = {}
        if a.asset_class == "fx" and a.fx:
            p = fx_pair(fx, *a.fx).with_columns(pl.col("date").alias("knowledge_date"))
            src = "official_fx"
        elif a.asset_class == "rates" and a.tenor in ust:
            u = ust[a.tenor]
            p = u.select("date", "price", "knowledge_date")
            src = "official_ust_synthetic"
        elif a.security_id:
            px = prices.filter(pl.col("security_id") == a.security_id)
            col = "adj_close" if a.security_id.startswith("ETF:") else "close"
            p = px.select("date", pl.col(col).alias("price"), pl.col("date").alias("knowledge_date"))
            src = "yahoo"
        else:
            continue
        p = p.filter(pl.col("price").is_not_null() & (pl.col("price") > 0)).sort("date")
        if p.height < 30:
            log.info("cross_asset: %s skipped (%d rows)", a.asset_id, p.height)
            continue
        p = p.with_columns(pl.lit(a.asset_id).alias("asset_id"))
        panel.append(p.select("asset_id", "date", "price"))
        s = price_signals(p)
        dates = s.select("date")
        # ---------------- implied vol
        iv_units = None
        if a.iv_id:
            ivp = prices.filter(pl.col("security_id") == a.iv_id).select(pl.col("date").alias("ivd"), pl.col("close").alias("iv"))
            if a.iv_id == "IDX:^MOVE":
                iv_units = "bp"
            else:
                ivp = ivp.with_columns(pl.col("iv") / 100.0)
                iv_units = "fraction"
            s = asof(s, ivp, "date", "ivd", tolerance="4d").drop("ivd")
        else:
            s = s.with_columns(pl.lit(None, pl.Float64).alias("iv"))
        if a.asset_class == "rates" and a.tenor in ust:
            s = s.join(ust[a.tenor].select("date", "rv_bp_1m"), on="date", how="left")
            denom = pl.col("rv_bp_1m") if iv_units == "bp" else pl.col("rv_1m")
            s = s.with_columns(pl.when(denom > 0).then(pl.col("iv") / denom).otherwise(None).alias("iv_rv_ratio")).drop("rv_bp_1m")
        else:
            s = s.with_columns(pl.when(pl.col("rv_1m") > 0).then(pl.col("iv") / pl.col("rv_1m")).otherwise(None).alias("iv_rv_ratio"))
        # ---------------- curve
        if a.futures_root:
            c = cm.filter(pl.col("root") == a.futures_root).drop("root")
            s = s.join(c, on="date", how="left")
            if a.curve_sign == -1:
                s = s.with_columns(-pl.col("carry_curve"), -pl.col("carry_12m"))
        # ---------------- fundamental carry + value
        carry_fund = pl.lit(None, pl.Float64)
        value = pl.lit(None, pl.Float64)
        value_method = None
        carry_method = None
        if a.asset_class == "fx" and a.fx:
            b, q = a.fx
            s = s.join(_rate(dates, policy, b, "rb"), on="date", how="left").join(_rate(dates, policy, q, "rq"), on="date", how="left")
            ib = _infl(dates, cpi, CCY_CPI.get(b)).rename({"infl_5y": "ib5", "cpi_yoy": "cb"})
            iq = _infl(dates, cpi, CCY_CPI.get(q)).rename({"infl_5y": "iq5", "cpi_yoy": "cq"})
            s = s.join(ib, on="date", how="left").join(iq, on="date", how="left")
            s = s.with_columns((pl.col("price").log() - pl.col("price").log().shift(1260)).alias("_ds5"))
            carry_fund = (pl.col("rb") - pl.col("rq")) / 100.0
            value = -(pl.col("_ds5") + pl.col("ib5") - pl.col("iq5"))
            extra["real_rate_diff"] = (pl.col("rb") - pl.col("cb")) - (pl.col("rq") - pl.col("cq"))
            extra["funding_rate"] = pl.col("rq") / 100.0
            carry_method, value_method = "fx_rate_diff", "fx_real_5y"
        elif a.asset_class == "rates" and a.tenor in ust:
            s = s.join(ust[a.tenor].select("date", "carry", "yld"), on="date", how="left")
            carry_fund = pl.col("carry")
            extra["funding_rate"] = pl.lit(None, pl.Float64)
            carry_method = "bond_carry_rolldown"
            if a.real_yield:
                ry = fred.filter(pl.col("series_id") == a.real_yield).select(pl.col("knowledge_date").alias("rk"), pl.col("value").alias("ry"))
                s = asof(s, ry, "knowledge_date", "rk", tolerance="10d").drop("rk")
                value, value_method = pl.col("ry"), "real_yield"
        elif a.asset_class == "equity_index" and a.etf:
            etf_px = prices.filter(pl.col("security_id") == a.etf).select("date", "close")
            dy = ttm_dividend_yield(etf_px, divs.filter(pl.col("security_id") == a.etf))
            s = asof(s, dy.rename({"date": "dyd"}), "date", "dyd", tolerance="7d").drop("dyd")
            s = s.join(_rate(dates, policy, a.ccy, "rf"), on="date", how="left")
            carry_fund = pl.col("dividend_yield") - pl.col("rf") / 100.0
            extra["funding_rate"] = pl.col("rf") / 100.0
            extra["dividend_yield"] = pl.col("dividend_yield")
            carry_method = "equity_div_funding"
            value, value_method = pl.col("reversal_5y"), "reversal_5y"
        elif a.asset_class == "credit" and a.oas:
            o = fred.filter(pl.col("series_id") == a.oas).select(pl.col("knowledge_date").alias("ok"), pl.col("value").alias("oas"))
            s = asof(s, o, "date", "ok", tolerance="10d").drop("ok")
            carry_fund = pl.col("oas") / 100.0
            value, value_method = pl.col("oas"), "oas_level"
            carry_method = "credit_oas"
        elif a.asset_class in ("commodity", "crypto"):
            carry_method = "futures_roll"
            if a.asset_class == "commodity":
                value, value_method = pl.col("reversal_5y"), "reversal_5y"
        for col, dt in (("carry_curve", pl.Float64), ("carry_12m", pl.Float64), ("carry_front_contract", pl.Utf8),
                        ("carry_dte_front", pl.Int16), ("carry_dte_next", pl.Int16), ("curve_slope", pl.Float64),
                        ("curve_n", pl.Int16)):
            if col not in s.columns:
                s = s.with_columns(pl.lit(None, dt).alias(col))
        s = s.with_columns(carry_fund.alias("carry_fundamental"), value.alias("value"),
                           *[e.alias(k) for k, e in extra.items()])
        carry = pl.col("carry_curve") if carry_method == "futures_roll" else pl.col("carry_fundamental")
        s = s.with_columns(
            carry.alias("carry"), pl.lit(carry_method, pl.Utf8).alias("carry_method"),
            pl.lit(value_method, pl.Utf8).alias("value_method"),
            pl.lit(a.asset_class).alias("asset_class"), pl.lit(a.sector, pl.Utf8).alias("sector"),
            pl.lit(a.futures_root, pl.Utf8).alias("futures_root"), pl.lit(src).alias("price_source"),
            pl.lit(a.iv_id.removeprefix("IDX:") if a.iv_id else None, pl.Utf8).alias("iv_source"),
            pl.lit(iv_units, pl.Utf8).alias("iv_units"),
            shape_label(pl.col("curve_slope")).alias("curve_shape"))
        frames.append(s)
    if not frames:
        return pl.DataFrame(), pl.DataFrame()
    df = pl.concat(frames, how="diagonal_relaxed").sort("asset_id", "date")
    df = df.with_columns(tsz("mom_12m").alias("z_mom_12m"), tsz("carry").alias("z_carry"), tsz("value").alias("z_value"),
                         tsz("rv_1m").alias("z_rv"), tsz("iv_rv_ratio").alias("z_iv_rv"),
                         tsz("curve_slope").alias("z_curve_slope"))
    if start is not None:
        df = df.filter(pl.col("date") >= start)
    df = df.with_columns(pl.lit(now).alias("computed_at"), pl.lit(run_id, pl.Utf8).alias("run_id"))
    from app.core.schema import get_contract  # noqa: PLC0415

    keep = [c for c in get_contract("cross_asset_signals").column_names if c in df.columns]
    f32 = [c for c in keep if df.schema[c] in (pl.Float64,)]
    df = df.select(keep).with_columns([pl.col(c).fill_nan(None).cast(pl.Float32) for c in f32])
    df = quantize_f32(df, [c for c in f32 if c != "price"])
    return df, pl.concat(panel, how="vertical_relaxed")


def quantize_f32(df: pl.DataFrame, cols: list[str], bits: int = QUANT_BITS) -> pl.DataFrame:
    """Round float32 columns to `bits` mantissa bits (12 bits = relative precision 2.4e-4, ~3.6 significant digits).
    Derived signals do not need 7 digits; the zeroed low bits let ZSTD compress the table ~30 % smaller."""
    keep = np.uint32((0xFFFFFFFF << (23 - bits)) & 0xFFFFFFFF)
    half = np.uint32(1 << (22 - bits))
    out = []
    for c in cols:
        a = df.get_column(c).to_numpy(allow_copy=True).astype(np.float32, copy=True)
        nan = np.isnan(a)
        u = (a.view(np.uint32) + half) & keep
        q = u.view(np.float32)
        q[nan] = np.nan
        out.append(pl.Series(c, q, dtype=pl.Float32).fill_nan(None))
    return df.with_columns(out) if out else df


# ------------------------------------------------------------------------------------------------ positioning (CFTC)


COT_COLS = ("cot_market_code", "cot_report_type", "cot_category", "cot_report_date", "cot_knowledge_date", "cot_inverted",
            "cot_index_3y", "cot_z_3y", "cot_net_pct_oi")
COT_DTYPES: dict[str, Any] = {
    "cot_market_code": pl.Utf8, "cot_report_type": pl.Utf8, "cot_category": pl.Utf8, "cot_report_date": pl.Date,
    "cot_knowledge_date": pl.Date, "cot_inverted": pl.Boolean, "cot_index_3y": pl.Float32, "cot_z_3y": pl.Float32,
    "cot_net_pct_oi": pl.Float32}
_COT_IN = {"root": pl.Utf8, "market_code": pl.Utf8, "report_type": pl.Utf8, "category": pl.Utf8, "report_date": pl.Date,
           "knowledge_date": pl.Date, "cot_index_3y": pl.Float32, "z_3y": pl.Float32, "net_pct_oi": pl.Float32}


def cot_spec(asset_class: str) -> tuple[str, str]:
    """(report type, trader category) used for an asset class."""
    return COT_SPEC.get(asset_class, COT_SPEC_DEFAULT)


def load_cot(assets: list[Asset], settings: Settings | None = None) -> pl.DataFrame:
    """CFTC rows needed by join_positioning: [root, market_code, report_type, category, report_date, knowledge_date,
    cot_index_3y, z_3y, net_pct_oi] for the futures roots of `assets`. Empty when the CFTC tables are absent (soft
    dependency on cftc_signals). One market per root: active first, then curated, then the largest open interest."""
    from app.core.storage import read_table, scan_table, table_dir  # noqa: PLC0415

    empty = pl.DataFrame(schema=_COT_IN)
    if not (table_dir("cftc_markets", settings).exists() and table_dir("cftc_signals", settings).exists()):
        log.warning("cross_asset: CFTC tables absent - positioning columns left null")
        return empty
    roots = sorted({a.futures_root for a in assets if a.futures_root})
    mk = read_table("cftc_markets", columns=["market_code", "root", "is_active", "is_major", "latest_open_interest"],
                    settings=settings).filter(pl.col("root").is_in(roots))
    mk = mk.sort(["root", "is_active", "is_major", "latest_open_interest"], descending=[False, True, True, True],
                 nulls_last=True).unique(subset=["root"], keep="first", maintain_order=True)
    if mk.is_empty():
        return empty
    specs = sorted({cot_spec(a.asset_class) for a in assets if a.futures_root})
    want = pl.DataFrame({"report_type": [x[0] for x in specs], "category": [x[1] for x in specs]})
    sig = (scan_table("cftc_signals", settings)
           .filter(pl.col("market_code").is_in(mk.get_column("market_code").to_list())
                   & pl.col("report_type").is_in(want.get_column("report_type").to_list())
                   & pl.col("category").is_in(want.get_column("category").to_list()))
           .select("market_code", "report_type", "category", "report_date", "knowledge_date", "cot_index_3y", "z_3y",
                   "net_pct_oi").collect())
    sig = sig.join(want, on=["report_type", "category"], how="semi")
    out = sig.join(mk.select("market_code", "root"), on="market_code", how="inner")
    return out.select([pl.col(c).cast(dt) for c, dt in _COT_IN.items()])


def join_positioning(sig: pl.DataFrame, cot: pl.DataFrame, assets: list[Asset],
                     max_age_days: int = COT_MAX_AGE_DAYS) -> pl.DataFrame:
    """Fill the cot_* columns of cross_asset_signals rows, strictly point-in-time: for each (asset, date) the latest
    report of the asset's market / report type / category whose RELEASE date (knowledge_date) is <= date (the Friday
    15:30 ET release counts for the Friday close row); reports released more than max_age_days before the date are
    stale -> null. Assets quoted inversely to their futures (curve_sign = -1, USDJPY vs 6J) get 1 - COT index and
    negated z / % OI (cot_inverted = true)."""
    base = sig.drop([c for c in COT_COLS if c in sig.columns])
    if base.width == 0:
        return base
    nulls = [pl.lit(None, dt).alias(c) for c, dt in COT_DTYPES.items()]
    if base.is_empty() or cot.is_empty():
        return base.with_columns(nulls)
    amap = pl.DataFrame([{"asset_id": a.asset_id, "root": a.futures_root, "report_type": cot_spec(a.asset_class)[0],
                          "category": cot_spec(a.asset_class)[1], "_sign": float(a.curve_sign)}
                         for a in assets if a.futures_root],
                        schema={"asset_id": pl.Utf8, "root": pl.Utf8, "report_type": pl.Utf8, "category": pl.Utf8,
                                "_sign": pl.Float64})
    right = cot.join(amap, on=["root", "report_type", "category"], how="inner")
    if right.is_empty():
        return base.with_columns(nulls)
    # several reports can share a release date (shutdown backlogs): the newest report per release date wins
    right = (right.sort("asset_id", "knowledge_date", "report_date")
             .unique(subset=["asset_id", "knowledge_date"], keep="last", maintain_order=True)
             .select("asset_id", pl.col("knowledge_date").alias("_kd"), "market_code", "report_type", "category",
                     "report_date", "cot_index_3y", "z_3y", "net_pct_oi", "_sign"))
    left = base.select("asset_id", "date").with_row_index("_row")
    j = left.sort("asset_id", "date").join_asof(right.sort("asset_id", "_kd"), left_on="date", right_on="_kd",
                                                 by="asset_id", strategy="backward",
                                                 tolerance=timedelta(days=int(max_age_days)), check_sortedness=False)
    inv = pl.col("_sign") < 0
    j = j.select(
        "_row",
        pl.col("market_code").alias("cot_market_code"), pl.col("report_type").alias("cot_report_type"),
        pl.col("category").alias("cot_category"), pl.col("report_date").alias("cot_report_date"),
        pl.col("_kd").alias("cot_knowledge_date"),
        pl.when(pl.col("market_code").is_null()).then(None).otherwise(inv).alias("cot_inverted"),
        pl.when(inv).then(1.0 - pl.col("cot_index_3y")).otherwise(pl.col("cot_index_3y")).alias("cot_index_3y"),
        (pl.col("z_3y") * pl.col("_sign")).alias("cot_z_3y"),
        (pl.col("net_pct_oi") * pl.col("_sign")).alias("cot_net_pct_oi"),
    ).sort("_row")
    out = base.with_columns(j.drop("_row").get_columns())
    return out.with_columns([pl.col(c).cast(dt) for c, dt in COT_DTYPES.items()])


# ------------------------------------------------------------------------------------------------ regime


def _wide_returns(panel: pl.DataFrame, basket: tuple[str, ...], ffill: int = 3) -> tuple[list[date], np.ndarray, list[str]]:
    p = panel.filter(pl.col("asset_id").is_in(list(basket)))
    have = [a for a in basket if a in set(p.get_column("asset_id").unique().to_list())]
    w = p.pivot(on="asset_id", index="date", values="price", aggregate_function="last").sort("date")
    # keep dates where >= 75 % of the members printed (drops one-region holidays / the not-yet-closed US session)
    fresh = pl.sum_horizontal([pl.col(a).is_not_null().cast(pl.Int16) for a in have])
    w = w.with_columns(*[pl.col(a).forward_fill(limit=ffill) for a in have], fresh.alias("_fresh"))
    w = w.filter(pl.col("_fresh") >= math.ceil(0.75 * len(have))).drop("_fresh")
    r = w.select("date", *[(pl.col(a).log() - pl.col(a).log().shift(1)).alias(a) for a in have]).slice(1)
    dates = r.get_column("date").to_list()
    return dates, r.select(have).to_numpy().astype(np.float64), have


def eigen_concentration(dates: list[date], R: np.ndarray, window: int, min_frac: float = 0.8) -> pl.DataFrame:
    """Rolling share of the first eigenvalue of the correlation matrix (rows with any NaN are dropped per window)."""
    n, k = R.shape
    eig = np.full(n, np.nan)
    avg = np.full(n, np.nan)
    complete = ~np.isnan(R).any(axis=1)
    cum = np.concatenate([[0], np.cumsum(complete)])
    need = int(window * min_frac)
    for i in range(window - 1, n):
        if cum[i + 1] - cum[i + 1 - window] < need or not complete[i]:
            continue
        X = R[i + 1 - window:i + 1]
        X = X[complete[i + 1 - window:i + 1]]
        sd = X.std(axis=0)
        if (sd <= 0).any():
            continue
        C = np.corrcoef(X, rowvar=False)
        ev = np.linalg.eigvalsh(C)
        eig[i] = ev[-1] / k
        avg[i] = (C.sum() - k) / (k * (k - 1))
    return pl.DataFrame({"date": dates, f"eig1_share_{window}": eig, f"avg_corr_{window}": avg}).with_columns(
        pl.col(f"eig1_share_{window}").fill_nan(None), pl.col(f"avg_corr_{window}").fill_nan(None))


def corr_snapshots(dates: list[date], R: np.ndarray, names: list[str], windows=(60, 120),
                   now: datetime | None = None) -> pl.DataFrame:
    now = now or datetime.now(UTC)
    complete = ~np.isnan(R).any(axis=1)
    idx = np.where(complete)[0]
    rows = []
    if idx.size == 0:
        return pl.DataFrame()
    for snap, back in (("latest", 0), ("1m", 21), ("1y", 252)):
        if idx.size <= back:
            continue
        sub = idx[: idx.size - back]
        end = int(sub[-1])
        for w in windows:
            sel = sub[-w:]
            if sel.size < w:
                continue
            C = np.corrcoef(R[sel], rowvar=False)
            for i, a in enumerate(names):
                for j, b in enumerate(names):
                    rows.append({"snapshot": snap, "window": w, "as_of": dates[end], "asset_i": a, "asset_j": b,
                                 "corr": float(C[i, j]), "n_obs": int(sel.size)})
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).with_columns(pl.col("window").cast(pl.Int16), pl.col("corr").cast(pl.Float32),
                                           pl.col("n_obs").cast(pl.Int16), pl.lit(now).alias("computed_at"))


RISK_COMPONENTS = ("comp_equity_trend", "comp_vix", "comp_vix_term", "comp_credit_rel", "comp_hy_oas",
                   "comp_copper_gold", "comp_audjpy", "comp_usd")


def risk_composite(panel: pl.DataFrame, prices: pl.DataFrame, fred: pl.DataFrame, fx: pl.DataFrame,
                   min_components: int = 4) -> pl.DataFrame:
    """MODEL-BASED risk-on / risk-off composite on the S&P 500 calendar."""
    spx = panel.filter(pl.col("asset_id") == "SPX").select("date", pl.col("price").log().alias("spx")).sort("date")
    if spx.is_empty():
        return pl.DataFrame()
    base = spx.select("date")

    def px(sid: str, name: str) -> pl.DataFrame:
        col = "adj_close" if sid.startswith("ETF:") else "close"
        p = prices.filter(pl.col("security_id") == sid).select(pl.col("date").alias("d"), pl.col(col).log().alias(name))
        return asof(base, p, "date", "d", tolerance="5d").select("date", name)

    def fred_s(sid: str, name: str, log_: bool = False) -> pl.DataFrame:
        f = fred.filter(pl.col("series_id") == sid).select(pl.col("knowledge_date").alias("k"),
                                                           (pl.col("value").log() if log_ else pl.col("value")).alias(name))
        if f.is_empty():
            return base.with_columns(pl.lit(None, pl.Float64).alias(name))
        return asof(base, f, "date", "k", tolerance="10d").select("date", name)

    aj = fx_pair(fx, "AUD", "JPY").select(pl.col("date").alias("d"), pl.col("price").log().alias("audjpy"))
    df = base.join(spx, on="date")
    for sid, name in (("IDX:^VIX", "vix"), ("IDX:^VIX3M", "vix3m"), ("ETF:HYG", "hyg"), ("ETF:IEF", "ief"),
                      ("IDX:HG=F", "hg"), ("IDX:GC=F", "gc")):
        df = df.join(px(sid, name), on="date", how="left")
    df = df.join(fred_s("fred:BAMLH0A0HYM2", "hy_oas"), on="date", how="left")
    df = df.join(fred_s("fred:DTWEXBGS", "usd", log_=True), on="date", how="left")
    df = df.join(asof(base, aj, "date", "d", tolerance="5d").select("date", "audjpy"), on="date", how="left")
    df = df.sort("date").with_columns(
        (pl.col("spx") - pl.col("spx").shift(63)).alias("r_equity_trend"),
        pl.col("vix").exp().alias("r_vix"),
        (pl.col("vix") - pl.col("vix3m")).alias("r_vix_term"),
        ((pl.col("hyg") - pl.col("ief")) - (pl.col("hyg") - pl.col("ief")).shift(63)).alias("r_credit_rel"),
        pl.col("hy_oas").alias("r_hy_oas"),
        ((pl.col("hg") - pl.col("gc")) - (pl.col("hg") - pl.col("gc")).shift(63)).alias("r_copper_gold"),
        (pl.col("audjpy") - pl.col("audjpy").shift(63)).alias("r_audjpy"),
        (pl.col("usd") - pl.col("usd").shift(63)).alias("r_usd"),
    )
    sign = {"equity_trend": 1, "vix": -1, "vix_term": -1, "credit_rel": 1, "hy_oas": -1, "copper_gold": 1,
            "audjpy": 1, "usd": -1}
    zs = []
    for k, sg in sign.items():
        x = pl.col(f"r_{k}")
        m = x.rolling_mean(Z_WIN, min_samples=REG_Z_MIN)
        sd = x.rolling_std(Z_WIN, min_samples=REG_Z_MIN)
        zs.append((pl.when(sd > 0).then(sg * (x - m) / sd).otherwise(None)).clip(-3, 3).fill_nan(None).alias(f"comp_{k}"))
    df = df.with_columns(zs)
    comps = [pl.col(c) for c in RISK_COMPONENTS]
    n = pl.sum_horizontal([c.is_not_null().cast(pl.Int8) for c in comps])
    df = df.with_columns(n.cast(pl.Int8).alias("risk_n_components"),
                         pl.when(n >= min_components).then(pl.mean_horizontal(comps)).otherwise(None).alias("risk_composite"))
    df = df.with_columns(pl.when(pl.col("risk_composite").is_null()).then(None)
                         .when(pl.col("risk_composite") > 0.5).then(pl.lit("risk_on"))
                         .when(pl.col("risk_composite") < -0.5).then(pl.lit("risk_off")).otherwise(pl.lit("neutral"))
                         .alias("risk_regime"))
    return df.select("date", "risk_composite", "risk_regime", "risk_n_components", *RISK_COMPONENTS)


def compute_regime(panel: pl.DataFrame, prices: pl.DataFrame, fred: pl.DataFrame, fx: pl.DataFrame,
                   run_id: str | None = None, now: datetime | None = None,
                   basket: tuple[str, ...] = CORR_BASKET) -> tuple[pl.DataFrame, pl.DataFrame]:
    now = now or datetime.now(UTC)
    dates, R, names = _wide_returns(panel, basket)
    if len(names) < len(basket):
        log.warning("correlation basket incomplete: missing %s", sorted(set(basket) - set(names)))
    e60 = eigen_concentration(dates, R, 60)
    e120 = eigen_concentration(dates, R, 120)
    reg = e60.join(e120, on="date", how="full", coalesce=True).sort("date")
    x = pl.col("eig1_share_120")
    reg = reg.with_columns(
        pl.when(x.rolling_std(Z_WIN, min_samples=REG_Z_MIN) > 0)
        .then((x - x.rolling_mean(Z_WIN, min_samples=REG_Z_MIN)) / x.rolling_std(Z_WIN, min_samples=REG_Z_MIN))
        .otherwise(None).fill_nan(None).alias("eig1_z_120"),
        pl.lit(len(names), pl.Int16).alias("n_assets"))
    rc = risk_composite(panel, prices, fred, fx)
    if not rc.is_empty():
        reg = reg.join(rc, on="date", how="full", coalesce=True)
    reg = reg.filter(pl.any_horizontal([pl.col(c).is_not_null() for c in ("eig1_share_60", "eig1_share_120", "risk_composite")
                                        if c in reg.columns]))
    reg = reg.with_columns(pl.col("date").alias("knowledge_date"), pl.lit(",".join(names)).alias("basket"),
                           pl.lit(now).alias("computed_at"), pl.lit(run_id, pl.Utf8).alias("run_id")).sort("date")
    f64 = [c for c, t in reg.schema.items() if t == pl.Float64]
    reg = reg.with_columns([pl.col(c).cast(pl.Float32) for c in f64])
    snaps = corr_snapshots(dates, R, names, now=now)
    return reg, snaps


# ------------------------------------------------------------------------------------------------ job


def load_inputs(assets: list[Asset], settings: Settings | None = None) -> dict[str, pl.DataFrame]:
    ids = sorted({x for a in assets for x in (a.security_id, a.iv_id, a.etf) if x}
                 | {"IDX:^VIX", "IDX:^VIX3M", "ETF:HYG", "ETF:IEF", "IDX:HG=F", "IDX:GC=F"})
    series = sorted(set(UST_POINTS.values()) | {UST_FUNDING, "fred:BAMLH0A0HYM2", "fred:DTWEXBGS"}
                    | {x for a in assets for x in (a.oas, a.real_yield) if x})
    return {"prices": load_prices(ids, settings), "fx": load_fx(settings), "fred": load_fred(series, settings),
            "policy": load_policy(settings), "cpi": load_cpi(settings),
            "divs": load_dividends(sorted({a.etf for a in assets if a.etf}), settings), "curve": load_curve(settings)}


def run_all(settings: Settings | None = None, run_id: str | None = None, write: bool = True) -> dict[str, Any]:
    from app.core.storage import write_table  # noqa: PLC0415
    from app.ingestion.futures_curve import update_state  # noqa: PLC0415

    s = settings or get_settings()
    assets = build_assets()
    inp = load_inputs(assets, s)
    now = datetime.now(UTC)
    sig, panel = compute_signals(assets, **inp, run_id=run_id, now=now)
    sig = join_positioning(sig, load_cot(assets, s), assets)  # soft dependency on cftc_signals
    reg, snaps = compute_regime(panel, inp["prices"], inp["fred"], inp["fx"], run_id=run_id, now=now)
    rows = {"cross_asset_signals": sig.height, "cross_asset_regime": reg.height, "cross_asset_corr": snaps.height}
    if write:
        write_table(sig, "cross_asset_signals", mode="overwrite", settings=s)
        write_table(reg, "cross_asset_regime", mode="overwrite", settings=s)
        if snaps.height:
            write_table(snaps, "cross_asset_corr", mode="overwrite", settings=s)
    last = sig.get_column("date").max() if sig.height else None
    pos = sig.filter(pl.col("cot_market_code").is_not_null()) if sig.height else sig
    res = {"status": "done", "rows": rows, "assets": int(sig.get_column("asset_id").n_unique()) if sig.height else 0,
           "assets_with_positioning": int(pos.get_column("asset_id").n_unique()) if pos.height else 0,
           "cot_last_report": str(pos.get_column("cot_report_date").max()) if pos.height else None,
           "last_date": str(last), "finished_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if write:
        update_state(s, steps={"cross_asset_signals": res})
    return res


def run(ctx):  # -> JobResult
    from app.scheduler.base import JobResult  # noqa: PLC0415

    res = run_all(ctx.settings, ctx.run_id)
    return JobResult(status="success", rows_written=res["rows"], entities_ok=res["assets"], watermark=res["last_date"])


def main(argv: list[str] | None = None) -> int:
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from app.core.logsetup import setup_logging  # noqa: PLC0415

    setup_logging(log_file="roadmap_v2/item5a.log")
    args = argv if argv is not None else sys.argv[1:]
    res = run_all(write="--dry-run" not in args)
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
