"""Cost of capital (WACC / cost of equity) per issuer, point-in-time, shared by `moat` and `forecast`.

Spec: contracts/scoring.yaml `cost_of_capital`. Output table: `cost_of_capital` (scores zone, PK as_of_date+issuer_id),
written by both the moat and the forecast job (upsert; same inputs -> same row).

Method (all inputs PIT at as_of D):
* risk-free: 10y government yield of the *reporting currency* from macro_observations (knowledge_date <= D, obs
  <= 120 days old): USD fred:DGS10, EUR ECB AAA 10y (fallback FRED Bund), daily national 10y where available (JGB MoF,
  gilts BoE, BoC, RBA, SNB, ChinaBond, BanRep TES, BCRP) then OECD/FRED monthly 10y (MXN, BRL, INR, CLP, ...),
  HKD -> USD (peg), DKK -> EUR (peg). Otherwise the dated FALLBACK_RF table (flag `rf_fallback`; e.g. TWD, ARS).
  For emerging-market local-currency yields (country risk premium >= 2%) the sovereign default spread
  (~CRP/1.5) is removed so it is not double counted with the CRP.
* ERP 5% (contract) + country risk premium: country seed (COUNTRY_CRP) else the contract's region table.
* beta: weekly log returns vs the local benchmark of the primary listing (3y window, >= 52 weeks), Blume-adjusted
  0.67*b + 0.33, clipped [0.4, 2.0], then shrunk 50% toward the subsector median (>= 3 peers; bottom-up style);
  fallback subsector median of computed betas, then 1.0 (flag `beta_fallback`).
* cost of debt: interest_expense / avg(total_debt) floored at rf + synthetic-rating spread (interest coverage,
  Damodaran large-firm table) and clipped to [rf + 0.5%, rf + 8%] (contract).
* tax: median effective rate of the last 3 FY clipped [0, 0.40]; statutory fallback by country.
* weights: E = market cap (PIT shares x close, converted to reporting currency), D = book total_debt.
* cost of equity floored at max(rf + 0.6 * (ERP + CRP), 5%).
* WACC clipped to [5%, 15% + max(0, rf - 5%)] (upper bound widened for high-rate currencies, logged in
  CONTRACT_CHANGES). Financials (banks / insurers / consumer finance / holdings) use cost of equity only.
* terminal growth (for DCF) = min(long-run nominal GDP proxy - 0.5%, rf, 3% (+ EM excess growth)), clip [0.5%, 7%].
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

import numpy as np
import polars as pl
import yaml

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table
from app.normalize.pit import asof
from app.normalize.taxonomy import subsector_groups

log = get_logger(__name__)

# ------------------------------------------------------------------------------------------------ seed tables

#: reporting currency -> macro series ids (first available wins)
RF_SERIES: dict[str, list[str]] = {
    "USD": ["fred:DGS10", "ust:10Y"],
    "EUR": ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "fred:IRLTLT01DEM156N", "bundesbank:BBSIS.D.R10XX"],
    "JPY": ["mof_jp:JGB.10Y", "fred:IRLTLT01JPM156N", "oecd:KEI.JPN.IRLT"],
    "GBP": ["boe:IUDMNZC", "boe:IUDMNPY", "fred:IRLTLT01GBM156N", "oecd:KEI.GBR.IRLT"],  # A9: ZC first (par frozen)
    "KRW": ["bok_ecos:817Y002.010210000", "fred:IRLTLT01KRM156N", "oecd:KEI.KOR.IRLT"],
    "MXN": ["fred:IRLTLT01MXM156N", "oecd:KEI.MEX.IRLT"],
    "HKD": ["fred:DGS10"],                      # USD peg
    "DKK": ["ecb:YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "fred:IRLTLT01DEM156N"],  # EUR peg
    "CAD": ["boc:BD.CDN.10YR.DQ.YLD", "fred:IRLTLT01CAM156N", "oecd:KEI.CAN.IRLT"],
    "AUD": ["rba:FCMYGBAG10D", "fred:IRLTLT01AUM156N", "oecd:KEI.AUS.IRLT"],
    "CHF": ["snb:rendeiduebd.CHF.10J", "fred:IRLTLT01CHM156N", "oecd:KEI.CHE.IRLT"],
    "SEK": ["fred:IRLTLT01SEM156N"], "NOK": ["fred:IRLTLT01NOM156N"], "ZAR": ["fred:IRLTLT01ZAM156N"],
    # macro-coverage additions (contracts/macro_catalog.yaml; all stored in percent)
    "CNY": ["akshare_cn:cgb_10y", "oecd:KEI.CHN.IRLT"],
    # BRL: no keyless 10Y market yield (OECD 'long-term rate' for Brazil is an administered ~9% rate) -> FALLBACK_RF
    "INR": ["oecd:KEI.IND.IRLT"],
    "CLP": ["oecd:KEI.CHL.IRLT", "fred:IRLTLT01CLM156N"],
    "COP": ["banrep:DF_TES_MONTHLY_HIST.IRGBRY10", "oecd:KEI.COL.IRLT"],
    "PEN": ["bcrp:PD31895MM"],
}
#: dated assumption (approx. 10y local-currency government yields, mid-2026) used when no series is available
FALLBACK_RF_ASOF = "2026-06-30"
FALLBACK_RF: dict[str, float] = {
    "USD": 0.042, "EUR": 0.026, "JPY": 0.016, "GBP": 0.046, "KRW": 0.029, "MXN": 0.093, "CNY": 0.018, "BRL": 0.135,
    "TWD": 0.015, "HKD": 0.042, "INR": 0.065, "CHF": 0.004, "SEK": 0.025, "NOK": 0.038, "DKK": 0.024, "AUD": 0.043,
    "CAD": 0.032, "NZD": 0.044, "SGD": 0.025, "CLP": 0.055, "COP": 0.110, "PEN": 0.065, "ARS": 0.250, "ZAR": 0.100,
    "IDR": 0.068, "THB": 0.022, "MYR": 0.037, "PHP": 0.062, "ILS": 0.042, "PLN": 0.054, "TRY": 0.280, "SAR": 0.050,
}
#: long-run nominal GDP growth proxy per currency (terminal-growth cap)
NOMINAL_GDP_LR: dict[str, float] = {
    "USD": 0.040, "EUR": 0.030, "JPY": 0.015, "GBP": 0.035, "CHF": 0.025, "CNY": 0.050, "HKD": 0.040, "KRW": 0.035,
    "TWD": 0.040, "BRL": 0.075, "MXN": 0.065, "INR": 0.090, "CAD": 0.040, "AUD": 0.045, "DKK": 0.030, "SEK": 0.035,
    "NOK": 0.040, "SGD": 0.040, "ZAR": 0.065, "CLP": 0.055, "COP": 0.070, "PEN": 0.060,
}
#: Damodaran-style country risk add-ons (seed, overrides the contract's region table)
COUNTRY_CRP: dict[str, float] = {
    "BR": 0.035, "MX": 0.020, "AR": 0.080, "CL": 0.010, "CO": 0.030, "PE": 0.020, "TW": 0.006, "IN": 0.025,
    "ZA": 0.035, "ID": 0.025, "TR": 0.045, "IT": 0.012, "ES": 0.009, "PT": 0.010, "GR": 0.015, "CH": 0.0,
    "DE": 0.0, "NL": 0.0, "DK": 0.0, "SE": 0.0, "NO": 0.0, "FR": 0.005, "IE": 0.006, "BE": 0.006, "FI": 0.004,
}
#: statutory corporate tax (incl. typical local surcharges), fallback when the effective rate is unusable
STATUTORY_TAX: dict[str, float] = {
    "US": 0.25, "JP": 0.30, "DE": 0.30, "FR": 0.25, "GB": 0.25, "CN": 0.25, "HK": 0.165, "KR": 0.24, "BR": 0.34,
    "MX": 0.30, "TW": 0.20, "NL": 0.258, "CH": 0.19, "DK": 0.22, "ES": 0.25, "IT": 0.24, "PT": 0.21, "CA": 0.265,
    "IN": 0.25, "SE": 0.206, "NO": 0.22, "FI": 0.20, "IE": 0.125, "BE": 0.25, "AU": 0.30, "SG": 0.17,
}
DEFAULT_TAX = 0.25
#: synthetic rating by interest coverage (EBIT / interest) -> default spread (Damodaran large non-financial firms)
COVERAGE_SPREADS: list[tuple[float, float]] = [
    (8.5, 0.0060), (6.5, 0.0080), (5.5, 0.0100), (4.25, 0.0110), (3.0, 0.0130), (2.5, 0.0170), (2.25, 0.0240),
    (2.0, 0.0280), (1.75, 0.0350), (1.5, 0.0420), (1.25, 0.0520), (0.8, 0.0700), (0.65, 0.0850), (-1e18, 0.1100),
]
#: benchmark index per listing country (fallback per region)
COUNTRY_BENCHMARK = {"US": "IDX:^GSPC", "CA": "IDX:^GSPC", "MX": "IDX:^MXX", "BR": "IDX:^BVSP", "DE": "IDX:^GDAXI",
                     "FR": "IDX:^FCHI", "PT": "IDX:PSI20.LS", "GB": "IDX:^FTSE", "JP": "IDX:^N225", "KR": "IDX:^KS11",
                     "HK": "IDX:^HSI", "CN": "IDX:000300.SS"}
REGION_BENCHMARK = {"US": "IDX:^GSPC", "CA": "IDX:^GSPC", "EU": "IDX:^STOXX50E", "UK": "IDX:^FTSE", "JP": "IDX:^N225",
                    "KR": "IDX:^KS11", "HK": "IDX:^HSI", "CN": "IDX:000300.SS", "LATAM": "IDX:^BVSP",
                    "APAC": "IDX:^GSPC", "OTHER": "IDX:^GSPC"}

FIN_GROUPS = {"banks", "insurers", "consumer_finance"}
FIN_SUBSECTORS = {"diversified_holdings"}
#: cost-of-equity floors (low-beta / low-rate artefacts, e.g. CNY state banks with beta ~0.1): beta-equivalent 0.6
#: and an absolute nominal 5%
KE_MIN_BETA = 0.6
#: weight of the issuer's own (Blume) beta vs the subsector median beta (noise shrinkage, bottom-up style)
BETA_OWN_WEIGHT = 0.5
KE_FLOOR = 0.05
BETA_WINDOW_WEEKS = 156
BETA_MIN_WEEKS = 52


@lru_cache(maxsize=1)
def contract_coc() -> dict[str, Any]:
    p = get_settings().contracts_dir / "scoring.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))["cost_of_capital"]


def is_financial(subsector_id: str | None) -> bool:
    """Banks / insurers / consumer finance / diversified holdings -> equity (excess-return) models."""
    if not subsector_id:
        return False
    return subsector_id in FIN_SUBSECTORS or bool(FIN_GROUPS & set(subsector_groups(subsector_id)))


def financial_flag(subsector_id: str | None, sector_id: str | None, total_assets: float | None,
                   total_equity: float | None, revenue: float | None = None) -> bool:
    """Equity-model routing: financial subsectors, or a FINS issuer with a balance-sheet business (assets > 5x
    revenue and leverage > 6x, e.g. broker-dealers MS/GS where debt is operating; not MA/V whose leverage comes
    from buybacks)."""
    if is_financial(subsector_id):
        return True
    if sector_id == "FINS" and total_assets and total_equity and total_equity > 0 and revenue and revenue > 0:
        return total_assets / total_equity > 6.0 and total_assets > 5.0 * revenue
    return False


def coverage_spread(coverage: float | None) -> float:
    if coverage is None or not math.isfinite(coverage):
        return 0.0170  # unknown -> BBB
    for lo, sp in COVERAGE_SPREADS:
        if coverage > lo:
            return sp
    return COVERAGE_SPREADS[-1][1]


def terminal_growth(currency: str | None, rf: float) -> float:
    """min(long-run nominal GDP - 0.5%, rf, 3% + excess nominal growth over 4%) clipped [0.5%, 7%]
    (ARCHITECTURE §11: <= 3% for developed-market currencies)."""
    gdp = NOMINAL_GDP_LR.get(currency or "", 0.045)
    return float(min(max(min(gdp - 0.005, rf, 0.03 + max(0.0, gdp - 0.04)), 0.005), 0.07))


def rates(rf: float, beta: float, crp_: float, kd_post: float, weight_equity: float, is_fin: bool,
          erp: float) -> tuple[float, float]:
    """(cost_of_equity, wacc) for a given ERP - same formulas as `compute` (used for implied-ERP calibration)."""
    ke = max(rf + beta * (erp + crp_), rf + KE_MIN_BETA * (erp + crp_), KE_FLOOR)
    if is_fin:
        return ke, ke
    lo, hi = wacc_bounds(rf)
    return ke, min(max(weight_equity * ke + (1 - weight_equity) * kd_post, lo), hi)


def wacc_bounds(rf: float) -> tuple[float, float]:
    lo, hi = contract_coc().get("wacc_clip", [0.05, 0.15])
    return float(lo), float(hi) + max(0.0, rf - 0.05)


# ------------------------------------------------------------------------------------------------ loaders


def _d(x: date | str) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x)[:10])


def load_issuers(settings: Settings | None = None, issuer_ids: Iterable[str] | None = None) -> pl.DataFrame:
    """issuer_id, name, region, country, sector_id, subsector_id, reporting_currency + primary security/listing."""
    s = settings or get_settings()
    iss = scan_table("issuers", s).select("issuer_id", "name", "region", "country", "sector_id", "subsector_id",
                                          "reporting_currency").collect()
    if issuer_ids is not None:
        iss = iss.filter(pl.col("issuer_id").is_in(list(issuer_ids)))
    sm = (scan_table("securities_master", s)
          .select("security_id", "issuer_id", "display_ticker", "trading_currency", "adr_ratio", "primary_listing_id",
                  "is_primary_security", "security_type")
          .collect())
    prim = (sm.sort(["issuer_id", "is_primary_security"], descending=[False, True], nulls_last=True)
            .unique("issuer_id", keep="first"))
    li = scan_table("listings", s).select("listing_id", "exchange_country").collect().unique("listing_id")
    prim = prim.join(li.rename({"listing_id": "primary_listing_id"}), on="primary_listing_id", how="left")
    return iss.join(prim.select("issuer_id", "security_id", "display_ticker", "trading_currency", "adr_ratio",
                                "exchange_country", "security_type"), on="issuer_id", how="left")


def load_fundamentals(settings: Settings | None = None, issuer_ids: Iterable[str] | None = None,
                      kinds: Sequence[str] = ("FY",), basis: str = "LOCAL") -> pl.DataFrame:
    """All versions (for PIT slicing later) of fundamentals_wide rows."""
    lf = scan_table("fundamentals_wide", settings or get_settings()).filter(
        (pl.col("currency_basis") == basis) & pl.col("period_kind").is_in(list(kinds)))
    if issuer_ids is not None:
        lf = lf.filter(pl.col("issuer_id").is_in(list(issuer_ids)))
    return lf.collect()


def fy_asof(fw_all: pl.DataFrame, as_of: date, max_years: int = 12) -> pl.DataFrame:
    """FY panel as known at as_of: one row per (issuer, fiscal_year), last `max_years` years, sorted."""
    if fw_all.is_empty():
        return fw_all
    snap = asof(fw_all.filter(pl.col("period_end") <= as_of), as_of,
                ["issuer_id", "period_end", "period_kind", "currency_basis"])
    snap = (snap.sort(["issuer_id", "fiscal_year", "period_end"])
            .unique(["issuer_id", "fiscal_year"], keep="last", maintain_order=True))
    snap = snap.with_columns(pl.col("fiscal_year").rank("dense", descending=True).over("issuer_id").alias("_r"))
    return snap.filter(pl.col("_r") <= max_years).drop("_r").sort(["issuer_id", "fiscal_year"])


def latest_asof(fw_all: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """Most recent period row (any kind) per issuer known at as_of (latest balance sheet / shares)."""
    if fw_all.is_empty():
        return fw_all
    snap = asof(fw_all.filter(pl.col("period_end") <= as_of), as_of,
                ["issuer_id", "period_end", "period_kind", "currency_basis"])
    # interim rows often carry total_debt = 0 derived from missing components -> treat as unknown
    if "long_term_debt" in snap.columns:
        snap = snap.with_columns(pl.when((pl.col("total_debt") == 0) & pl.col("long_term_debt").is_null())
                                 .then(None).otherwise(pl.col("total_debt")).alias("total_debt"))
    order = {"FY": 0, "TTM": 1, "Q": 2, "H": 3}
    snap = (snap.filter(pl.col("period_end") >= as_of - timedelta(days=550))
            .with_columns(pl.col("period_kind").replace_strict(order, default=9).alias("_k"))
            .sort(["issuer_id", "period_end", "_k"], descending=[False, False, True]))
    vals = [c for c in snap.columns if c not in ("issuer_id", "period_kind", "currency_basis", "_k")]
    # per field: the most recent non-null value (balance sheet / shares of the latest filing that reports it)
    return snap.group_by("issuer_id").agg([pl.col(c).drop_nulls().last().alias(c) for c in vals])


def load_prices(security_ids: Iterable[str], start: date, end: date, settings: Settings | None = None) -> pl.DataFrame:
    ids = sorted({x for x in security_ids if x})
    if not ids:
        return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "close": pl.Float64,
                                    "adj_close": pl.Float64, "split_factor": pl.Float64, "currency": pl.Utf8})
    lf = scan_table("prices_daily", settings or get_settings()).filter(
        pl.col("year").is_between(start.year, end.year) & pl.col("security_id").is_in(ids)
        & pl.col("date").is_between(start, end))
    return (lf.select("security_id", "date", "close", "adj_close", "split_factor", "currency").collect()
            .unique(["security_id", "date"], keep="first").sort(["security_id", "date"]))


def close_asof(prices: pl.DataFrame, as_of: date, max_stale_days: int = 10) -> pl.DataFrame:
    """security_id, price_date, close, currency (last close <= as_of, not older than max_stale_days)."""
    if prices.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "price_date": pl.Date, "close": pl.Float64,
                                    "price_currency": pl.Utf8})
    return (prices.filter((pl.col("date") <= as_of) & (pl.col("date") >= as_of - timedelta(days=max_stale_days)))
            .sort("date").group_by("security_id").last()
            .select("security_id", pl.col("date").alias("price_date"), "close", pl.col("currency").alias("price_currency")))


SPLIT_SHARE_COLS = ("shares_outstanding", "shares_diluted_weighted", "shares_basic_weighted")
SPLIT_PER_SHARE_COLS = ("eps_diluted", "eps_basic", "dps", "dividends_per_share", "book_value_per_share")


def split_adjust(df: pl.DataFrame, prices: pl.DataFrame, issuers: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """Put share counts / per-share items of a fundamentals panel on the share basis of the as_of price.

    Values reported before a later split (BKNG 1:25 and 8766.T 1:15 in 2026) are pre-split while the as_of close is
    post-split -> price/EPS and shares x price are off by the ratio. m = split_factor(as_of) / split_factor(period_end)
    of the issuer's primary security (period basis, as valuation_daily; a wide row's knowledge_date is the max over its
    items and says nothing about the basis of each value). Rows whose share count is already on the current basis
    (restated comparatives: closer to the latest post-split count unadjusted than adjusted) keep m = 1.
    Shares x m, per-share / m."""
    if df.is_empty() or prices.is_empty() or "split_factor" not in prices.columns or "security_id" not in issuers.columns:
        return df
    sf = (prices.filter((pl.col("date") <= as_of) & pl.col("split_factor").is_not_null() & (pl.col("split_factor") > 0))
          .select(pl.col("security_id").alias("__sid"), pl.col("date").alias("__d"), pl.col("split_factor").alias("__sf")))
    if sf.is_empty():
        return df
    now = sf.sort("__d").group_by("__sid").last().select("__sid", pl.col("__sf").alias("__sf_now"))
    sec = issuers.select("issuer_id", pl.col("security_id").alias("__sid")).drop_nulls().unique(subset=["issuer_id"])
    d = df.with_row_index("__i").join(sec, on="issuer_id", how="left")
    look = (d.select("__i", "__sid", pl.col("period_end").alias("__pe")).drop_nulls().sort("__pe")
            .join_asof(sf.sort("__d"), left_on="__pe", right_on="__d", by="__sid", strategy="nearest",
                       check_sortedness=False)
            .join(now, on="__sid", how="left")
            .select("__i", (pl.col("__sf_now") / pl.col("__sf")).alias("__m")))
    d = d.join(look, on="__i", how="left")
    m0 = pl.when(pl.col("__m").is_not_null() & pl.col("__m").is_finite() & ((pl.col("__m") - 1).abs() > 1e-6))         .then(pl.col("__m")).otherwise(1.0)
    share_cols = [c for c in SPLIT_SHARE_COLS if c in d.columns]
    d = d.with_columns(m0.alias("__m"),
                       (pl.coalesce(*[pl.col(c) for c in share_cols]) if share_cols else pl.lit(None, pl.Float64))
                       .cast(pl.Float64).alias("__s"))
    # latest share count already on the current basis (post-split period) per issuer
    ref = (d.filter((pl.col("__m") == 1.0) & (pl.col("__s") > 0)).sort("period_end").group_by("issuer_id")
           .agg(pl.col("__s").last().alias("__sref")))
    d = d.join(ref, on="issuer_id", how="left")
    already = (pl.col("__sref").is_not_null() & (pl.col("__s") > 0)
               & ((pl.col("__s") / pl.col("__sref")).log().abs()
                  < (pl.col("__s") * pl.col("__m") / pl.col("__sref")).log().abs()))
    m = pl.when(already.fill_null(False)).then(1.0).otherwise(pl.col("__m"))
    d = d.with_columns(m.alias("__m"))
    d = d.with_columns([(pl.col(c) * pl.col("__m")).alias(c) for c in share_cols]
                       + [(pl.col(c) / pl.col("__m")).alias(c) for c in SPLIT_PER_SHARE_COLS if c in d.columns])
    return d.drop("__i", "__sid", "__m", "__s", "__sref")


def load_macro(settings: Settings | None = None) -> pl.DataFrame:
    ids = sorted({x for v in RF_SERIES.values() for x in v})
    try:
        return (scan_table("macro_observations", settings or get_settings())
                .filter(pl.col("series_id").is_in(ids) & pl.col("value").is_not_null())
                .select("series_id", "date", "value", "knowledge_date").collect())
    except Exception as e:  # noqa: BLE001
        log.warning("macro_observations unavailable: %s", e)
        return pl.DataFrame(schema={"series_id": pl.Utf8, "date": pl.Date, "value": pl.Float64,
                                    "knowledge_date": pl.Date})


def load_fx(currencies: Iterable[str], start: date, end: date, settings: Settings | None = None) -> pl.DataFrame:
    from app.normalize.fx import load_rates  # noqa: PLC0415
    try:
        return load_rates(sorted({c for c in currencies if c}), start, end, settings)
    except Exception as e:  # noqa: BLE001
        log.warning("fx_rates unavailable: %s", e)
        return pl.DataFrame(schema={"date": pl.Date, "currency": pl.Utf8, "usd_per_unit": pl.Float64,
                                    "source_id": pl.Utf8})


def usd_per_unit(fx: pl.DataFrame, ccy: str | None, d: date, max_stale_days: int = 10) -> float | None:
    from app.normalize.fx import base_currency  # noqa: PLC0415
    base, mult = base_currency(ccy)
    if base is None:
        return None
    if base == "USD":
        return mult
    r = fx.filter((pl.col("currency") == base) & (pl.col("date") <= d)
                  & (pl.col("date") >= d - timedelta(days=max_stale_days)))
    if r.is_empty():
        return None
    return float(r.sort("date").get_column("usd_per_unit")[-1]) * mult


def fx_cross(fx: pl.DataFrame, from_ccy: str | None, to_ccy: str | None, d: date) -> float | None:
    """Units of `to_ccy` per 1 unit of `from_ccy` at d."""
    if not from_ccy or not to_ccy:
        return None
    if from_ccy == to_ccy:
        return 1.0
    a, b = usd_per_unit(fx, from_ccy, d), usd_per_unit(fx, to_ccy, d)
    if not a or not b:
        return None
    return a / b


# ------------------------------------------------------------------------------------------------ components


def risk_free(macro: pl.DataFrame, currency: str | None, as_of: date, max_stale_days: int = 120) -> tuple[float, str]:
    """(rf as fraction, source) for a reporting currency at as_of (PIT on knowledge_date)."""
    ccy = currency or "USD"
    for sid in RF_SERIES.get(ccy, []):
        r = macro.filter((pl.col("series_id") == sid) & (pl.col("knowledge_date") <= as_of)
                         & (pl.col("date") <= as_of) & (pl.col("date") >= as_of - timedelta(days=max_stale_days)))
        if r.height:
            v = float(r.sort("date").get_column("value")[-1]) / 100.0
            if math.isfinite(v):
                return v, sid
    if ccy in FALLBACK_RF:
        return FALLBACK_RF[ccy], f"fallback:{FALLBACK_RF_ASOF}"
    us, _ = risk_free(macro, "USD", as_of, max_stale_days) if ccy != "USD" else (FALLBACK_RF["USD"], "")
    return us + 0.02, f"fallback:USD+2%:{FALLBACK_RF_ASOF}"


def crp(country: str | None, region: str | None) -> float:
    if country and country in COUNTRY_CRP:
        return COUNTRY_CRP[country]
    return float(contract_coc().get("country_risk_premium", {}).get(region or "OTHER", 0.02))


def weekly_returns(prices: pl.DataFrame) -> pl.DataFrame:
    """security_id, week, r (weekly log return of adj_close; last close of each ISO week)."""
    if prices.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "week": pl.Date, "r": pl.Float64})
    w = (prices.filter(pl.col("adj_close") > 0)
         .with_columns(pl.col("date").dt.truncate("1w").alias("week"))
         .sort(["security_id", "date"]).group_by(["security_id", "week"], maintain_order=True)
         .agg(pl.col("adj_close").last()))
    return (w.sort(["security_id", "week"])
            .with_columns((pl.col("adj_close").log() - pl.col("adj_close").log().shift(1).over("security_id")).alias("r"))
            .filter(pl.col("r").is_not_null() & pl.col("r").is_finite() & (pl.col("r").abs() < 1.0))
            .select("security_id", "week", "r"))


def betas(wret: pl.DataFrame, sec_bench: pl.DataFrame, as_of: date, window_weeks: int = BETA_WINDOW_WEEKS
          ) -> pl.DataFrame:
    """Raw OLS beta of weekly returns vs the security's benchmark over the window ending at as_of.

    sec_bench: security_id, bench_id. Returns security_id, beta_raw, n_weeks."""
    if wret.is_empty() or sec_bench.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "beta_raw": pl.Float64, "n_weeks": pl.UInt32})
    lo = as_of - timedelta(weeks=window_weeks)
    win = wret.filter((pl.col("week") > lo) & (pl.col("week") <= as_of))
    b = win.rename({"security_id": "bench_id", "r": "rm"})
    x = (win.join(sec_bench, on="security_id", how="inner").join(b, on=["bench_id", "week"], how="inner")
         .filter(pl.col("security_id") != pl.col("bench_id")))
    return (x.group_by("security_id").agg(
        pl.len().alias("n_weeks"),
        ((pl.col("r") * pl.col("rm")).mean() - pl.col("r").mean() * pl.col("rm").mean()).alias("cov"),
        ((pl.col("rm") ** 2).mean() - pl.col("rm").mean() ** 2).alias("var"))
        .with_columns(pl.when(pl.col("var") > 0).then(pl.col("cov") / pl.col("var")).alias("beta_raw"))
        .select("security_id", "beta_raw", "n_weeks"))


def effective_tax(fy: pl.DataFrame) -> pl.DataFrame:
    """issuer_id, tax_eff: median of income_tax/pretax_income (pretax > 0) over the last 3 FY, clipped [0, 0.40]."""
    if fy.is_empty():
        return pl.DataFrame(schema={"issuer_id": pl.Utf8, "tax_eff": pl.Float64})
    t = (fy.sort(["issuer_id", "fiscal_year"]).group_by("issuer_id").tail(3)
         .filter(pl.col("pretax_income") > 0)
         .with_columns((pl.col("income_tax") / pl.col("pretax_income")).alias("tr"))
         .filter(pl.col("tr").is_finite()))
    return t.group_by("issuer_id").agg(pl.col("tr").median().clip(0.0, 0.40).alias("tax_eff"))


@dataclass
class Inputs:
    """Data loaded once per job run and sliced per as_of date."""
    issuers: pl.DataFrame
    fy_all: pl.DataFrame
    latest_all: pl.DataFrame
    fy_usd_all: pl.DataFrame
    prices: pl.DataFrame
    wret: pl.DataFrame
    sec_bench: pl.DataFrame
    macro: pl.DataFrame
    fx: pl.DataFrame
    meta: dict[str, Any] = field(default_factory=dict)


def load_inputs(as_of_dates: Sequence[date], settings: Settings | None = None,
                issuer_ids: Iterable[str] | None = None) -> Inputs:
    s = settings or get_settings()
    d0, d1 = min(as_of_dates), max(as_of_dates)
    fy_all = load_fundamentals(s, issuer_ids, ("FY",), "LOCAL")
    ids = fy_all.get_column("issuer_id").unique().to_list() if issuer_ids is None else list(issuer_ids)
    iss = load_issuers(s, ids)
    latest_all = load_fundamentals(s, ids, ("Q", "TTM", "H", "FY"), "LOCAL").select(
        "issuer_id", "period_end", "period_kind", "currency_basis", "knowledge_date", "fiscal_year", "currency",
        *[c for c in ("shares_outstanding", "shares_diluted_weighted", "cash_and_st_investments", "total_debt",
                      "minority_interest", "preferred_equity", "total_equity_parent", "total_equity",
                      "long_term_investments", "long_term_debt") if c in fy_all.columns])
    fy_usd_all = load_fundamentals(s, ids, ("FY",), "USD").select(
        "issuer_id", "period_end", "period_kind", "currency_basis", "knowledge_date", "fiscal_year", "revenue")
    bench_ids = sorted(set(COUNTRY_BENCHMARK.values()) | set(REGION_BENCHMARK.values()))
    sec_ids = [x for x in iss.get_column("security_id").to_list() if x]
    prices = load_prices(sec_ids + bench_ids, d0 - timedelta(weeks=BETA_WINDOW_WEEKS + 6), d1, s)
    wret = weekly_returns(prices)
    sb = iss.select("security_id", "exchange_country", "region").filter(pl.col("security_id").is_not_null())
    sb = sb.with_columns(pl.struct("exchange_country", "region").map_elements(
        lambda r: COUNTRY_BENCHMARK.get(r["exchange_country"] or "") or REGION_BENCHMARK.get(r["region"] or "OTHER",
                                                                                              "IDX:^GSPC"),
        return_dtype=pl.Utf8).alias("bench_id")).select("security_id", "bench_id")
    ccys = set(iss.get_column("reporting_currency").drop_nulls().to_list()) | set(
        iss.get_column("trading_currency").drop_nulls().to_list()) | set(fy_all.get_column("currency").unique().to_list())
    fx = load_fx(ccys, d0 - timedelta(days=30), d1, s)
    return Inputs(iss, fy_all, latest_all, fy_usd_all, prices.select("security_id", "date", "close", "split_factor", "currency"),
                  wret, sb, load_macro(s), fx)


def compute(inp: Inputs, as_of: date, fy: pl.DataFrame | None = None, run_id: str | None = None) -> pl.DataFrame:
    """Cost-of-capital rows (contract `cost_of_capital`) for every issuer with FY fundamentals at as_of."""
    fy = fy if fy is not None else split_adjust(fy_asof(inp.fy_all, as_of), inp.prices, inp.issuers, as_of)
    if fy.is_empty():
        return pl.DataFrame()
    last = fy.sort(["issuer_id", "fiscal_year"]).group_by("issuer_id").last()
    prev = (fy.sort(["issuer_id", "fiscal_year"]).group_by("issuer_id").agg(pl.col("total_debt").tail(2).first()
                                                                            .alias("debt_prev")))
    lat = split_adjust(latest_asof(inp.latest_all, as_of), inp.prices, inp.issuers, as_of)
    tax = effective_tax(fy)
    px = close_asof(inp.prices, as_of)
    b = betas(inp.wret, inp.sec_bench, as_of)
    base = (last.select("issuer_id", pl.col("currency").alias("fin_currency"), "fiscal_year", "period_end", "ebit",
                        "interest_expense", "total_debt", "revenue", "total_assets",
                        pl.col("total_equity").alias("equity_fy"))
            .join(prev, on="issuer_id", how="left")
            .join(lat.select("issuer_id", pl.col("shares_outstanding").alias("sh_out"),
                             pl.col("shares_diluted_weighted").alias("sh_dil"),
                             pl.col("total_debt").alias("debt_latest"), pl.col("currency").alias("lat_ccy")),
                  on="issuer_id", how="left")
            .join(inp.issuers, on="issuer_id", how="left")
            .join(tax, on="issuer_id", how="left")
            .join(px, on="security_id", how="left")
            .join(b, on="security_id", how="left"))
    rows: list[dict[str, Any]] = []
    for r in base.iter_rows(named=True):
        ccy = r["fin_currency"] or r["reporting_currency"] or "USD"
        flags: list[str] = []
        rf_raw, rf_src = risk_free(inp.macro, ccy, as_of)
        if rf_src.startswith("fallback"):
            flags.append("rf_fallback")
        c = crp(r["country"], r["region"])
        rf = rf_raw - (c / 1.5 if c >= 0.02 and ccy not in ("USD", "EUR") else 0.0)
        erp = float(contract_coc().get("equity_risk_premium", 0.05))
        beta_raw = r["beta_raw"] if (r["n_weeks"] or 0) >= BETA_MIN_WEEKS else None
        beta = None if beta_raw is None or not math.isfinite(beta_raw) else min(max(0.67 * beta_raw + 0.33, 0.4), 2.0)
        # market cap (reporting currency): shares x close / adr_ratio, FX trading -> reporting
        shares = r["sh_out"] if (r["sh_out"] or 0) > 0 else r["sh_dil"]
        mcap = None
        if shares and r["close"] and r["close"] > 0:
            k = fx_cross(inp.fx, r["price_currency"], ccy, as_of)
            if k:
                mcap = shares * r["close"] / (r["adr_ratio"] or 1.0) * k
            else:
                flags.append("fx_missing")
        debt = r["debt_latest"] if r["debt_latest"] is not None else r["total_debt"]
        debt = max(debt or 0.0, 0.0)
        tax_rate = r["tax_eff"]
        tax_src = "effective_3y"
        if tax_rate is None or tax_rate < 0.05:
            tax_rate, tax_src = STATUTORY_TAX.get(r["country"] or "", DEFAULT_TAX), "statutory"
        avg_debt = np.nanmean([x for x in (r["total_debt"], r["debt_prev"]) if x is not None]) if (
            r["total_debt"] is not None or r["debt_prev"] is not None) else None
        kd_actual = (r["interest_expense"] / avg_debt) if (r["interest_expense"] and avg_debt and avg_debt > 0) else None
        cov = (r["ebit"] / r["interest_expense"]) if (r["ebit"] is not None and r["interest_expense"]
                                                      and r["interest_expense"] > 0) else (50.0 if debt == 0 else None)
        spread = coverage_spread(cov)
        kd_pre = max(kd_actual if kd_actual is not None and math.isfinite(kd_actual) else 0.0, rf_raw + spread)
        kd_pre = min(max(kd_pre, rf_raw + 0.005), rf_raw + 0.08)
        rows.append({"issuer_id": r["issuer_id"], "security_id": r["security_id"], "currency": ccy,
                     "rf": rf, "rf_source": rf_src, "erp": erp, "crp": c, "beta_raw": beta_raw, "beta": beta,
                     "n_weeks": r["n_weeks"], "tax_rate": float(tax_rate), "tax_source": tax_src,
                     "kd_pre": kd_pre, "interest_coverage": cov, "mcap": mcap, "debt": debt,
                     "subsector_id": r["subsector_id"], "flags": flags,
                     "is_financial": financial_flag(r["subsector_id"], r["sector_id"], r["total_assets"], r["equity_fy"],
                                                    r["revenue"])})
    out = pl.DataFrame(rows, infer_schema_length=None)
    # beta fallback: subsector median of computed betas, then 1.0
    med = (out.filter(pl.col("beta").is_not_null()).group_by("subsector_id")
           .agg(pl.col("beta").median().alias("b_med"), pl.len().alias("b_n")))
    out = out.join(med, on="subsector_id", how="left").with_columns(
        # bottom-up blend: 50% own (Blume) beta, 50% subsector median when >= 3 peers have a regression beta
        pl.when(pl.col("beta").is_not_null() & (pl.col("b_n") >= 3))
        .then((BETA_OWN_WEIGHT * pl.col("beta") + (1 - BETA_OWN_WEIGHT) * pl.col("b_med")).clip(0.4, 2.0))
        .otherwise(pl.col("beta")).alias("beta"))
    out = out.with_columns(
        pl.when(pl.col("beta").is_not_null()).then(pl.lit("regression"))
        .when(pl.col("b_med").is_not_null()).then(pl.lit("subsector_median")).otherwise(pl.lit("default_1.0"))
        .alias("beta_source"),
        pl.coalesce("beta", "b_med", pl.lit(1.0)).alias("beta"))
    out = out.with_columns(
        pl.max_horizontal(pl.col("rf") + pl.col("beta") * (pl.col("erp") + pl.col("crp")),
                          pl.col("rf") + KE_MIN_BETA * (pl.col("erp") + pl.col("crp")), pl.lit(KE_FLOOR))
        .alias("cost_of_equity"),
        (pl.col("kd_pre") * (1 - pl.col("tax_rate"))).alias("kd_post"))
    w_e = pl.when(pl.col("mcap").is_not_null() & ((pl.col("mcap") + pl.col("debt")) > 0)).then(
        pl.col("mcap") / (pl.col("mcap") + pl.col("debt"))).otherwise(0.8)
    out = out.with_columns(pl.when(pl.col("is_financial")).then(1.0).otherwise(w_e).alias("weight_equity"))
    out = out.with_columns(
        (pl.col("weight_equity") * pl.col("cost_of_equity") + (1 - pl.col("weight_equity")) * pl.col("kd_post"))
        .alias("wacc_raw"))
    lo_hi = [wacc_bounds(x) for x in out.get_column("rf").to_list()]
    out = out.with_columns(pl.Series("wlo", [x[0] for x in lo_hi]), pl.Series("whi", [x[1] for x in lo_hi]))
    out = out.with_columns(
        pl.when(pl.col("is_financial")).then(pl.col("cost_of_equity"))
        .otherwise(pl.col("wacc_raw").clip(pl.col("wlo"), pl.col("whi"))).alias("wacc"),
        pl.struct("currency", "rf").map_elements(lambda x: terminal_growth(x["currency"], x["rf"]),
                                                 return_dtype=pl.Float64).alias("terminal_growth"),
        pl.when(pl.col("beta_source") != "regression")
        .then(pl.col("flags").list.concat(pl.lit(["beta_fallback"]))).otherwise(pl.col("flags")).alias("flags"))
    now = datetime.now(UTC)
    return out.select(
        pl.lit(as_of).alias("as_of_date"), "issuer_id", "security_id", "currency", "rf", "rf_source", "erp", "crp",
        "beta_raw", "beta", "beta_source", pl.col("n_weeks").cast(pl.Int32), "cost_of_equity", "kd_pre", "kd_post",
        "tax_rate", "tax_source", "interest_coverage", pl.col("mcap").alias("market_cap"),
        pl.col("debt").alias("total_debt"), "weight_equity", "wacc", "terminal_growth", "is_financial",
        pl.col("flags").list.unique().list.sort(), pl.lit(now).alias("computed_at"), pl.lit(run_id).alias("run_id"))
