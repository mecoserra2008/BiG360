"""Price finalization: prices_staging (source-native bars) -> prices_daily. Owner: ingest-market.

Job `prices_finalize` (contracts/jobs.yaml) calls run(ctx). Steps:

1. Universe + symbol resolution (`load_universe`, `resolve_symbols`): staged symbols are mapped to
   listings/securities through reference/listings (polygon_ticker / yf_ticker) or app.normalize.identity.
   Benchmarks (indices, sector ETFs not in the security master) get readable synthetic ids `IDX:^GSPC` /
   `ETF:XLK`. Before ref-identity has published listings, universe seed symbols get provisional ids
   `P:<yf symbol>` (flag `provisional_id`); they disappear on the first rebuild after listings exist.
2. Source choice per (security, date): priority polygon > krx > baostock = twse = tpex = b3_cotahist > yfinance.
3. Cross-source reconciliation on overlaps (Polygon vs yfinance raw close, tolerance 1%) -> `source_disagreement`.
4. Corporate actions (corporate_actions_staging): cumulative split factor and split+cash-dividend factor
   (`compute_adjustment_factors`); adj_close = close * adj_factor (total-return style, CRSP convention).
5. close_usd via fx_rates (as-of, <= 5 days), GBp/ZAc/ILA minor units normalised by price_divisor.
6. DQ flags: ohlc_inconsistent, zero_volume, stale, return_outlier, non_session (exchange_calendars),
   source_disagreement, single_source_overlap_gap, provisional_id, unit_rescaled. A JSON report lands in
   data/logs/reports/.
7. Integrity guards (O2.2a, app.normalize.price_integrity): Yahoo bars ingested before their session closed are
   dropped (`drop_unfinished_staged`); ticker-reuse breaks split each source series so a security only carries its
   current issuer's bars (`detect_identity_breaks`, overrides in reference/seeds/price_identity_breaks.csv); 10^k unit
   flips are repaired after adjustment (`repair_unit_scale`).

Also hosts the shared market metadata used by all price adapters (yfinance suffix table, benchmark list,
built-in seed universe, country->region map, exchange calendars) and `finalize_corporate_actions`.
"""

from __future__ import annotations

import json
import time
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import read_table, scan_table, table_dir, write_table

log = get_logger(__name__)


def _asof(left: pl.DataFrame, right: pl.DataFrame, **kw: Any) -> pl.DataFrame:
    """join_asof without the 'sortedness cannot be checked' warning (we always sort on the asof key first)."""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Sortedness of columns cannot be checked")
        return left.join_asof(right, **kw)

# ================================================================================================ market metadata


@dataclass(frozen=True)
class Market:
    suffix: str
    country: str
    currency: str        # quote currency as delivered by Yahoo (GBp/ZAc/ILA = minor units)
    mic: str
    calendar: str | None  # exchange_calendars code
    timezone: str


_MARKETS: list[Market] = [
    Market("", "US", "USD", "XNYS", "XNYS", "America/New_York"),
    Market(".TO", "CA", "CAD", "XTSE", "XTSE", "America/Toronto"),
    Market(".V", "CA", "CAD", "XTSX", "XTSE", "America/Toronto"),
    Market(".MX", "MX", "MXN", "XMEX", "XMEX", "America/Mexico_City"),
    Market(".SA", "BR", "BRL", "BVMF", "BVMF", "America/Sao_Paulo"),
    Market(".BA", "AR", "ARS", "XBUE", "XBUE", "America/Argentina/Buenos_Aires"),
    Market(".SN", "CL", "CLP", "XSGO", "XSGO", "America/Santiago"),
    Market(".CL", "CO", "COP", "XBOG", "XBOG", "America/Bogota"),
    Market(".LM", "PE", "PEN", "XLIM", "XLIM", "America/Lima"),
    Market(".L", "GB", "GBp", "XLON", "XLON", "Europe/London"),
    Market(".IL", "GB", "USD", "XLON", "XLON", "Europe/London"),
    Market(".PA", "FR", "EUR", "XPAR", "XPAR", "Europe/Paris"),
    Market(".AS", "NL", "EUR", "XAMS", "XAMS", "Europe/Amsterdam"),
    Market(".BR", "BE", "EUR", "XBRU", "XBRU", "Europe/Brussels"),
    Market(".LS", "PT", "EUR", "XLIS", "XLIS", "Europe/Lisbon"),
    Market(".IR", "IE", "EUR", "XDUB", "XDUB", "Europe/Dublin"),
    Market(".OL", "NO", "NOK", "XOSL", "XOSL", "Europe/Oslo"),
    Market(".DE", "DE", "EUR", "XETR", "XETR", "Europe/Berlin"),
    Market(".F", "DE", "EUR", "XFRA", "XFRA", "Europe/Berlin"),
    Market(".MI", "IT", "EUR", "XMIL", "XMIL", "Europe/Rome"),
    Market(".MC", "ES", "EUR", "XMAD", "XMAD", "Europe/Madrid"),
    Market(".SW", "CH", "CHF", "XSWX", "XSWX", "Europe/Zurich"),
    Market(".VI", "AT", "EUR", "XWBO", "XWBO", "Europe/Vienna"),
    Market(".ST", "SE", "SEK", "XSTO", "XSTO", "Europe/Stockholm"),
    Market(".CO", "DK", "DKK", "XCSE", "XCSE", "Europe/Copenhagen"),
    Market(".HE", "FI", "EUR", "XHEL", "XHEL", "Europe/Helsinki"),
    Market(".IC", "IS", "ISK", "XICE", "XICE", "Atlantic/Reykjavik"),
    Market(".WA", "PL", "PLN", "XWAR", "XWAR", "Europe/Warsaw"),
    Market(".PR", "CZ", "CZK", "XPRA", "XPRA", "Europe/Prague"),
    Market(".BD", "HU", "HUF", "XBUD", "XBUD", "Europe/Budapest"),
    Market(".AT", "GR", "EUR", "ASEX", "ASEX", "Europe/Athens"),
    Market(".IS", "TR", "TRY", "XIST", "XIST", "Europe/Istanbul"),
    Market(".T", "JP", "JPY", "XTKS", "XTKS", "Asia/Tokyo"),
    Market(".KS", "KR", "KRW", "XKRX", "XKRX", "Asia/Seoul"),
    Market(".KQ", "KR", "KRW", "XKOS", "XKRX", "Asia/Seoul"),
    Market(".SS", "CN", "CNY", "XSHG", "XSHG", "Asia/Shanghai"),
    Market(".SZ", "CN", "CNY", "XSHE", "XSHG", "Asia/Shanghai"),
    Market(".HK", "HK", "HKD", "XHKG", "XHKG", "Asia/Hong_Kong"),
    Market(".TW", "TW", "TWD", "XTAI", "XTAI", "Asia/Taipei"),
    Market(".TWO", "TW", "TWD", "ROCO", "XTAI", "Asia/Taipei"),
    Market(".SI", "SG", "SGD", "XSES", "XSES", "Asia/Singapore"),
    Market(".AX", "AU", "AUD", "XASX", "XASX", "Australia/Sydney"),
    Market(".NZ", "NZ", "NZD", "XNZE", "XNZE", "Pacific/Auckland"),
    Market(".NS", "IN", "INR", "XNSE", "XBOM", "Asia/Kolkata"),
    Market(".BO", "IN", "INR", "XBOM", "XBOM", "Asia/Kolkata"),
    Market(".JK", "ID", "IDR", "XIDX", "XIDX", "Asia/Jakarta"),
    Market(".BK", "TH", "THB", "XBKK", "XBKK", "Asia/Bangkok"),
    Market(".KL", "MY", "MYR", "XKLS", "XKLS", "Asia/Kuala_Lumpur"),
    Market(".JO", "ZA", "ZAc", "XJSE", "XJSE", "Africa/Johannesburg"),
    Market(".SR", "SA", "SAR", "XSAU", "XSAU", "Asia/Riyadh"),
    Market(".TA", "IL", "ILA", "XTAE", "XTAE", "Asia/Jerusalem"),
]
SUFFIX_MARKET: dict[str, Market] = {m.suffix: m for m in _MARKETS}
MIC_MARKET: dict[str, Market] = {m.mic: m for m in _MARKETS}

#: listing MICs (segment / operating) that share another market's exchange calendar
MIC_CALENDAR_ALIASES: dict[str, str] = {
    "XNAS": "XNYS", "XNGS": "XNYS", "XNMS": "XNYS", "XNCM": "XNYS", "XASE": "XNYS", "ARCX": "XNYS", "BATS": "XNYS",
    "IEXG": "XNYS", "XNYS": "XNYS", "XKOS": "XKRX", "XKRX": "XKRX", "XSHE": "XSHG", "XJPX": "XTKS", "XOSE": "XTKS",
    "XVTX": "XSWX", "XTSX": "XTSE", "MTAA": "XMIL", "XMCE": "XMAD", "XEUR": "XETR", "XLON": "XLON", "XHKG": "XHKG",
}

#: minor-unit quote currencies -> (major currency, divisor)
MINOR_UNITS: dict[str, tuple[str, float]] = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0), "ZAc": ("ZAR", 100.0),
                                              "ZAC": ("ZAR", 100.0), "ILA": ("ILS", 100.0), "ILa": ("ILS", 100.0)}

_EU = {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT",
       "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "CH", "NO", "IS", "LI", "MC"}
_LATAM = {"MX", "BR", "AR", "CL", "CO", "PE", "UY", "PA", "CR", "EC", "VE", "BO", "PY"}
_APAC = {"TW", "SG", "AU", "NZ", "IN", "ID", "TH", "MY", "PH", "VN", "PK", "LK", "BD"}


def country_to_region(cc: str | None) -> str:
    cc = (cc or "").upper()
    if cc in ("US", "CA", "JP", "KR", "CN", "HK"):
        return cc
    if cc in ("GB", "UK", "JE", "GG", "IM"):
        return "UK"
    if cc in _EU:
        return "EU"
    if cc in _LATAM:
        return "LATAM"
    if cc in _APAC:
        return "APAC"
    return "OTHER"


def yf_suffix(symbol: str) -> str:
    """'7203.T' -> '.T'; 'BRK-B' -> ''; '^GSPC' -> '' (indices handled by BENCHMARKS)."""
    if "." in symbol and not symbol.startswith("^"):
        suf = "." + symbol.rsplit(".", 1)[1].upper()
        if suf in SUFFIX_MARKET:
            return suf
    return ""


def market_for_yf(symbol: str) -> Market:
    return SUFFIX_MARKET[yf_suffix(symbol)]


def polygon_to_yf(ticker: str) -> str:
    """Polygon class separator '.' -> Yahoo '-' (BRK.B -> BRK-B)."""
    return ticker.replace(".", "-")


def yf_to_polygon(symbol: str) -> str | None:
    if yf_suffix(symbol) or symbol.startswith("^"):
        return None
    return symbol.replace("-", ".")


# ------------------------------------------------------------------------------------------------ benchmarks


@dataclass(frozen=True)
class Benchmark:
    yf_ticker: str
    name: str
    region: str
    currency: str
    kind: str                    # index | etf | index_ext | future | fx (index_ext/future: P2; fx: price-history)
    calendar: str | None
    alternates: tuple[str, ...] = ()

    @property
    def security_id(self) -> str:
        return f"{'ETF' if self.kind == 'etf' else 'IDX'}:{self.yf_ticker}"


BENCHMARKS: list[Benchmark] = [
    Benchmark("^GSPC", "S&P 500", "US", "USD", "index", "XNYS"),
    Benchmark("^STOXX50E", "EURO STOXX 50", "EU", "EUR", "index", "XETR"),
    Benchmark("^N225", "Nikkei 225", "JP", "JPY", "index", "XTKS"),
    Benchmark("^KS11", "KOSPI Composite", "KR", "KRW", "index", "XKRX"),
    Benchmark("^HSI", "Hang Seng", "HK", "HKD", "index", "XHKG"),
    Benchmark("000300.SS", "CSI 300", "CN", "CNY", "index", "XSHG"),
    Benchmark("^BVSP", "Ibovespa", "LATAM", "BRL", "index", "BVMF"),
    Benchmark("^FTSE", "FTSE 100", "UK", "GBP", "index", "XLON"),
    Benchmark("^GDAXI", "DAX", "EU", "EUR", "index", "XETR"),
    Benchmark("^FCHI", "CAC 40", "EU", "EUR", "index", "XPAR"),
    Benchmark("PSI20.LS", "PSI (Portugal)", "EU", "EUR", "index", "XLIS", ("^PSI20", "PSI.LS")),
    Benchmark("^MXX", "S&P/BMV IPC", "LATAM", "MXN", "index", "XMEX"),
    Benchmark("SPY", "SPDR S&P 500 ETF", "US", "USD", "etf", "XNYS"),
    Benchmark("XLK", "Technology Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLF", "Financial Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLV", "Health Care Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLE", "Energy Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLI", "Industrial Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLY", "Consumer Discretionary Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLP", "Consumer Staples Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLU", "Utilities Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLB", "Materials Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLC", "Communication Services Select Sector SPDR", "US", "USD", "etf", "XNYS"),
    Benchmark("XLRE", "Real Estate Select Sector SPDR", "US", "USD", "etf", "XNYS"),
]
def _extended_benchmarks() -> list[Benchmark]:
    """Price-coverage P2: major global ETFs, extra indices and continuous futures (app.ingestion.price_coverage_lists);
    price-history: FX pairs (kind fx, region FX), extra commodity futures, commodity indices / ETFs and volatility
    indices (app.ingestion.price_history_lists). FX pairs are market instruments only: conversions use fx_rates."""
    from app.ingestion.price_coverage_lists import extended_benchmarks  # noqa: PLC0415
    from app.ingestion.price_history_lists import history_instruments  # noqa: PLC0415

    have = {b.yf_ticker for b in BENCHMARKS}
    out: list[Benchmark] = []
    for t, n, r, c, k, cal in [*extended_benchmarks(), *history_instruments()]:
        if t not in have:
            have.add(t)
            out.append(Benchmark(t, n, r, c, k, cal))
    return out


BENCHMARKS += _extended_benchmarks()
BENCHMARK_BY_TICKER: dict[str, Benchmark] = {b.yf_ticker: b for b in BENCHMARKS}
for _b in BENCHMARKS:
    for _alt in _b.alternates:
        BENCHMARK_BY_TICKER.setdefault(_alt, _b)


def benchmark_security_id(ticker: str) -> str | None:
    """Synthetic security_id used in prices_daily for a benchmark (e.g. 'IDX:^GSPC'), or None."""
    b = BENCHMARK_BY_TICKER.get(ticker)
    return b.security_id if b else None


# ------------------------------------------------------------------------------------------------ seed universe
# Used only until ref-identity publishes reference/listings (and for the live smoke run).

SEED_US: list[tuple[str, str]] = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "Nvidia"), ("AMZN", "Amazon"), ("GOOGL", "Alphabet"),
    ("META", "Meta Platforms"), ("BRK-B", "Berkshire Hathaway"), ("TSLA", "Tesla"), ("AVGO", "Broadcom"),
    ("JPM", "JPMorgan Chase"), ("LLY", "Eli Lilly"), ("V", "Visa"), ("UNH", "UnitedHealth"), ("XOM", "Exxon Mobil"),
    ("MA", "Mastercard"), ("JNJ", "Johnson & Johnson"), ("PG", "Procter & Gamble"), ("HD", "Home Depot"),
    ("COST", "Costco"), ("ABBV", "AbbVie"), ("WMT", "Walmart"), ("NFLX", "Netflix"), ("BAC", "Bank of America"),
    ("KO", "Coca-Cola"), ("CRM", "Salesforce"), ("ORCL", "Oracle"), ("CVX", "Chevron"), ("MRK", "Merck"),
    ("AMD", "Advanced Micro Devices"), ("PEP", "PepsiCo"), ("TMO", "Thermo Fisher"), ("ADBE", "Adobe"),
    ("LIN", "Linde"), ("MCD", "McDonald's"), ("CSCO", "Cisco"), ("ACN", "Accenture"), ("ABT", "Abbott"),
    ("WFC", "Wells Fargo"), ("DIS", "Walt Disney"), ("INTU", "Intuit"), ("IBM", "IBM"), ("QCOM", "Qualcomm"),
    ("CAT", "Caterpillar"), ("GE", "GE Aerospace"), ("TXN", "Texas Instruments"), ("VZ", "Verizon"),
    ("AMGN", "Amgen"), ("PFE", "Pfizer"), ("NOW", "ServiceNow"), ("GS", "Goldman Sachs"), ("NKE", "Nike"),
]
SEED_GLOBAL: list[tuple[str, str]] = [
    ("SHEL.L", "Shell"), ("AZN.L", "AstraZeneca"), ("HSBA.L", "HSBC"), ("ULVR.L", "Unilever"), ("BP.L", "BP"),
    ("RIO.L", "Rio Tinto"), ("GSK.L", "GSK"), ("MC.PA", "LVMH"), ("OR.PA", "L'Oreal"), ("TTE.PA", "TotalEnergies"),
    ("SAN.PA", "Sanofi"), ("AIR.PA", "Airbus"), ("ASML.AS", "ASML"), ("INGA.AS", "ING"), ("SAP.DE", "SAP"),
    ("SIE.DE", "Siemens"), ("ALV.DE", "Allianz"), ("DTE.DE", "Deutsche Telekom"), ("NESN.SW", "Nestle"),
    ("NOVN.SW", "Novartis"), ("ROG.SW", "Roche"), ("ENI.MI", "Eni"), ("ISP.MI", "Intesa Sanpaolo"),
    ("SAN.MC", "Banco Santander"), ("IBE.MC", "Iberdrola"), ("ITX.MC", "Inditex"), ("EDP.LS", "EDP"),
    ("GALP.LS", "Galp Energia"), ("JMT.LS", "Jeronimo Martins"), ("NOVO-B.CO", "Novo Nordisk"),
    ("7203.T", "Toyota Motor"), ("6758.T", "Sony"), ("8306.T", "Mitsubishi UFJ"), ("6861.T", "Keyence"),
    ("9984.T", "SoftBank Group"), ("005930.KS", "Samsung Electronics"), ("000660.KS", "SK Hynix"),
    ("035420.KS", "Naver"), ("600519.SS", "Kweichow Moutai"), ("601318.SS", "Ping An Insurance"),
    ("000858.SZ", "Wuliangye"), ("0700.HK", "Tencent"), ("9988.HK", "Alibaba"), ("1299.HK", "AIA Group"),
    ("0005.HK", "HSBC Holdings"), ("PETR4.SA", "Petrobras"), ("VALE3.SA", "Vale"), ("ITUB4.SA", "Itau Unibanco"),
    ("WALMEX.MX", "Walmart de Mexico"), ("AMXB.MX", "America Movil"), ("RY.TO", "Royal Bank of Canada"),
    ("SHOP.TO", "Shopify"), ("2330.TW", "TSMC"), ("BHP.AX", "BHP"),
]


# ================================================================================================ universe / resolution

_RES_SCHEMA = {
    "key": pl.Utf8, "yf_ticker": pl.Utf8, "polygon_ticker": pl.Utf8, "security_id": pl.Utf8, "listing_id": pl.Utf8,
    "region": pl.Utf8, "country": pl.Utf8, "mic": pl.Utf8, "quote_currency": pl.Utf8, "currency": pl.Utf8,
    "price_divisor": pl.Float64, "calendar": pl.Utf8, "is_benchmark": pl.Boolean, "provisional": pl.Boolean,
    "name": pl.Utf8, "px_from": pl.Date, "px_to": pl.Date,
}


def _table_has_data(table: str, settings: Settings | None = None) -> bool:
    try:
        root = table_dir(table, settings)
    except KeyError:
        return False
    return root.exists() and any(root.rglob("*.parquet"))


#: countries whose exchanges quote under the bare local symbol on Yahoo / Polygon (US: AAPL, BRK-B / BRK.B). Elsewhere a
#: listing resolves only through its own venue symbol (yf_ticker with the exchange suffix).
BARE_SYMBOL_COUNTRIES = frozenset({"US"})


def listing_key_expr() -> pl.Expr:
    """Resolution key of a listing row (listings columns): its yfinance symbol; the bare local symbol only on a venue
    that quotes under it (US). Ticker-collision fix (A6, 2026-11-06): the key used to be coalesce(yf_ticker,
    local_symbol), so a listing on a venue no price source serves (Sofia, Zagreb, Malta, Cyprus, Bratislava, Ljubljana,
    Luxembourg: yf_ticker null) took the bare local ticker as its Yahoo key - CVS@XMAL, HON@XBRA, ETR@XBUL then pulled the
    US stock's Yahoo and Polygon bars, and load_universe's key de-duplication dropped the US security itself (which of
    the two won depended on row order). Such a listing now has no key: it resolves no staged symbol and stays unpriced
    (reason mapping_gap, app.normalize.price_collisions)."""
    return pl.coalesce(pl.col("yf_ticker"),
                       pl.when(pl.col("exchange_country").is_in(list(BARE_SYMBOL_COUNTRIES)))
                       .then(pl.col("local_symbol")).otherwise(pl.lit(None, pl.Utf8)))


def load_listings(settings: Settings | None = None, include_delisted: bool = False) -> pl.DataFrame:
    """Current primary listings joined with securities_master (empty frame if reference data is not built yet).

    include_delisted (prices_finalize): also the last primary listing of every INACTIVE security (delisted names,
    survivorship) with a price window px_to = listing valid_to; an active listing that reuses the same symbol then only
    takes bars after that date (px_from), so a recycled ticker never mixes two companies."""
    s = settings or get_settings()
    if not _table_has_data("listings", s):
        return pl.DataFrame(schema=_RES_SCHEMA)
    try:
        all_l = read_table("listings", settings=s)
        lst = all_l.filter(pl.col("is_primary") & pl.col("valid_to").is_null()).with_columns(
            pl.lit(None, pl.Date).alias("px_from"), pl.lit(None, pl.Date).alias("px_to"))
        if include_delisted and _table_has_data("securities_master", s):
            inactive = read_table("securities_master", settings=s).filter(~pl.col("is_active")).get_column("security_id")
            dl = (all_l.filter(pl.col("is_primary") & pl.col("valid_to").is_not_null()
                               & pl.col("security_id").is_in(inactive.implode())
                               & ~pl.col("security_id").is_in(lst.get_column("security_id").implode()))
                  .sort("valid_to", descending=True).unique(subset=["security_id"], keep="first")
                  .with_columns(pl.lit(None, pl.Date).alias("px_from"), pl.col("valid_to").alias("px_to")))
            if dl.height:
                k = listing_key_expr()
                last_dl = dl.filter(k.is_not_null()).group_by(k.alias("__k")).agg(pl.col("valid_to").max().alias("__d"))
                lst = (lst.join(last_dl, left_on=k, right_on="__k", how="left")
                       .with_columns((pl.col("__d") + pl.duration(days=1)).alias("px_from")).drop("__d", "__k",
                                                                                                  strict=False))
                lst = pl.concat([lst, dl.select(lst.columns)], how="vertical_relaxed")
        sm = read_table("securities_master", settings=s).select(
            "security_id", "name", "trading_currency", "is_active", "security_type") if _table_has_data(
            "securities_master", s) else None
    except Exception as e:  # pragma: no cover - partially written reference data
        log.warning("listings unreadable (%s); falling back to seeds", type(e).__name__)
        return pl.DataFrame(schema=_RES_SCHEMA)
    if sm is not None:
        lst = lst.join(sm, on="security_id", how="left")
    else:
        lst = lst.with_columns(pl.lit(None, pl.Utf8).alias("name"), pl.lit(None, pl.Utf8).alias("trading_currency"))
    cal = {**{m.mic: m.calendar for m in _MARKETS}, **MIC_CALENDAR_ALIASES}
    if "security_type" not in lst.columns:
        lst = lst.with_columns(pl.lit(None, pl.Utf8).alias("security_type"))
    out = lst.select(
        listing_key_expr().alias("key"),
        pl.col("yf_ticker"), pl.col("polygon_ticker"), pl.col("security_id"), pl.col("listing_id"),
        # FX pairs live in their own prices_daily region partition (region=FX); everything else by exchange country
        pl.when(pl.col("security_type") == "fx").then(pl.lit("FX"))
        .otherwise(pl.col("exchange_country").map_elements(country_to_region, return_dtype=pl.Utf8)).alias("region"),
        pl.col("exchange_country").alias("country"), pl.col("mic"), pl.col("quote_currency"),
        pl.coalesce(pl.col("trading_currency"),
                    pl.col("quote_currency").replace({k: v[0] for k, v in MINOR_UNITS.items()})).alias("currency"),
        pl.col("price_divisor").fill_null(1.0),
        pl.col("mic").replace_strict(cal, default=None, return_dtype=pl.Utf8).alias("calendar"),
        pl.col("security_id").str.contains(r"^(IDX|ETF):").alias("is_benchmark"), pl.lit(False).alias("provisional"),
        pl.col("name"), "px_from", "px_to",
    )
    # maintain_order (A6): load_universe keeps the first row per key, so the order must not change from run to run
    return out.cast(_RES_SCHEMA).unique(subset=["security_id"], keep="first", maintain_order=True)  # type: ignore[arg-type]


def _seed_symbols(settings: Settings) -> list[tuple[str, str]]:
    """Seed universe: ref-identity seed CSVs if present (columns yf_ticker|ticker [+ name]), else built-in lists."""
    seeds: list[tuple[str, str]] = []
    sdir = settings.data_dir / "reference" / "seeds"
    for f, builtin in ((sdir / "mvp_us.csv", SEED_US), (sdir / "mvp_global.csv", SEED_GLOBAL)):
        got: list[tuple[str, str]] = []
        if f.exists():
            try:
                df = pl.read_csv(f, infer_schema_length=0, comment_prefix="#")
                col = next((c for c in ("yf_ticker", "yahoo", "ticker", "symbol") if c in df.columns), None)
                ncol = next((c for c in ("name", "company", "issuer_name") if c in df.columns), None)
                if col:
                    for r in df.iter_rows(named=True):
                        if not r[col] or str(r.get("action") or "").lower() == "exclude":
                            continue
                        t = str(r[col]).strip()
                        if not yf_suffix(t) and "." in t and not t.startswith("^"):
                            t = polygon_to_yf(t)  # SEC/Massive class separator '.' -> Yahoo '-'
                        got.append((t, str(r[ncol]) if ncol and r[ncol] else t))
            except Exception as e:  # pragma: no cover
                log.warning("seed csv %s unreadable: %s", f.name, e)
        seeds += got or builtin  # per-file fallback to the built-in list
    return list(dict.fromkeys(seeds))


def _provisional_row(yf: str, name: str, currency_hint: str | None = None) -> dict[str, Any]:
    m = market_for_yf(yf)
    qc = currency_hint or m.currency
    major, div = MINOR_UNITS.get(qc, (qc, 1.0))
    return {"key": yf, "yf_ticker": yf, "polygon_ticker": yf_to_polygon(yf), "security_id": f"P:{yf}",
            "listing_id": f"P:{yf}", "region": country_to_region(m.country), "country": m.country, "mic": m.mic,
            "quote_currency": qc, "currency": major, "price_divisor": div, "calendar": m.calendar,
            "is_benchmark": False, "provisional": True, "name": name, "px_from": None, "px_to": None}


def _benchmark_rows() -> list[dict[str, Any]]:
    rows = []
    for b in BENCHMARKS:
        for t in (b.yf_ticker, *b.alternates):
            rows.append({"key": t, "yf_ticker": t, "polygon_ticker": b.yf_ticker if b.kind == "etf" else None,
                         "security_id": b.security_id, "listing_id": b.security_id, "region": b.region,
                         "country": None, "mic": None, "quote_currency": b.currency, "currency": b.currency,
                         "price_divisor": 1.0, "calendar": b.calendar, "is_benchmark": True, "provisional": False,
                         "name": b.name, "px_from": None, "px_to": None})
    return rows


def load_universe(settings: Settings | None = None, include_benchmarks: bool = True,
                  currency_hints: dict[str, str] | None = None, include_delisted: bool = False) -> pl.DataFrame:
    """Symbols to price. Reference listings when available (authoritative), else the seed universe with provisional
    ids. Benchmarks are appended unless the security master already contains them (e.g. sector ETFs).
    include_delisted: see load_listings (prices_finalize only; fetchers never request delisted symbols)."""
    s = settings or get_settings()
    lst = load_listings(s, include_delisted=include_delisted)
    if lst.height:
        uni = lst
    else:
        hints = currency_hints or {}
        uni = pl.DataFrame([_provisional_row(t, n, hints.get(t)) for t, n in _seed_symbols(s)], schema=_RES_SCHEMA)
    if include_benchmarks:
        bm = pl.DataFrame(_benchmark_rows(), schema=_RES_SCHEMA)
        known = set(uni.get_column("yf_ticker").drop_nulls().to_list())
        bm = bm.filter(~pl.col("yf_ticker").is_in(list(known)))
        uni = pl.concat([uni, bm], how="vertical")
    # one row per resolution key (A6): rows without a key (listing on a venue no price source serves) are all kept -
    # they resolve nothing - instead of collapsing into one null-key row; a duplicate key is logged, never silent
    uni = uni.with_row_index("__o")
    keyed = uni.filter(pl.col("key").is_not_null())
    dup = keyed.filter(pl.struct("key", "px_to").is_duplicated())
    if dup.height:
        log.warning("load_universe: %d securities share %d resolution keys; keeping the first per key (e.g. %s)",
                    dup.height, dup.get_column("key").n_unique(), dup.get_column("key").unique().sort().head(5).to_list())
    keyed = keyed.unique(subset=["key", "px_to"], keep="first", maintain_order=True)
    return pl.concat([keyed, uni.filter(pl.col("key").is_null())], how="vertical").sort("__o").drop("__o")


def resolve_symbols(staged: pl.DataFrame, universe: pl.DataFrame | None = None,
                    settings: Settings | None = None) -> pl.DataFrame:
    """Map staged (source_id, symbol[, currency]) to universe rows. Returns the input columns + _RES_SCHEMA columns
    (inner join: symbols outside the universe are dropped)."""
    uni = universe if universe is not None else load_universe(settings)
    if "currency" in staged.columns:
        staged = staged.rename({"currency": "src_currency"})
    clash = [c for c in staged.columns if c in _RES_SCHEMA and c != "key"]
    if clash:
        staged = staged.drop(clash)
    st = staged.with_columns(
        pl.when(pl.col("source_id") == "polygon").then(pl.col("symbol").str.replace_all(r"\.", "-"))
        .otherwise(pl.col("symbol")).alias("__yf"))
    by_poly = uni.filter(pl.col("polygon_ticker").is_not_null()).select(
        pl.col("polygon_ticker").alias("__poly"), *[pl.col(c) for c in _RES_SCHEMA if c != "key"])
    poly = st.filter(pl.col("source_id") == "polygon")
    other = st.filter(pl.col("source_id") != "polygon")
    poly_hit = poly.join(by_poly, left_on="symbol", right_on="__poly", how="inner")
    poly_miss = poly.join(by_poly.select("__poly"), left_on="symbol", right_on="__poly", how="anti")
    by_key = uni.filter(pl.col("key").is_not_null()).select(
        pl.col("key").alias("__k"), *[pl.col(c) for c in _RES_SCHEMA if c != "key"]) \
        .unique(subset=["__k", "px_to"], keep="first", maintain_order=True)
    rest_hit = other.join(by_key, left_on="__yf", right_on="__k", how="inner")
    # Polygon is US-only: a Polygon ticker without a polygon_ticker match may only fall back to the Yahoo key of a US
    # row (A6: the fallback used to reach any key, so ADVE / VIS / TOK grouped-daily bars landed on Sofia / Zagreb lines)
    us_key = by_key.filter((pl.col("country") == "US") | (pl.col("country").is_null() & (pl.col("region") == "US")))
    poly_key_hit = poly_miss.join(us_key, left_on="__yf", right_on="__k", how="inner")
    cols = [*staged.columns, *[c for c in _RES_SCHEMA if c != "key"]]
    out = pl.concat([poly_hit.select(cols), rest_hit.select(cols), poly_key_hit.select(cols)], how="vertical_relaxed")
    # price windows of delisted listings / recycled symbols (null = unbounded)
    dcol = "date" if "date" in out.columns else ("ex_date" if "ex_date" in out.columns else None)
    if dcol and out.height and (out.get_column("px_from").is_not_null().any() or out.get_column("px_to").is_not_null().any()):
        out = out.filter((pl.col("px_from").is_null() | (pl.col(dcol) >= pl.col("px_from")))
                         & (pl.col("px_to").is_null() | (pl.col(dcol) <= pl.col("px_to"))))
    return out


# ================================================================================================ calendars


@lru_cache(maxsize=64)
def _all_sessions(calendar: str) -> tuple[date, ...] | None:
    """Every session of an exchange calendar from 1990 (or its first available session) to ~1y ahead. Built once."""
    try:
        import exchange_calendars as xcals  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return None
    for start in ("1990-01-02", None):
        try:
            cal = xcals.get_calendar(calendar, start=start) if start else xcals.get_calendar(calendar)
            return tuple(d.date() for d in cal.sessions)
        except Exception as e:  # start before the calendar's bound -> retry with the default
            log.debug("calendar %s start=%s unavailable: %s", calendar, start, e)
    return None


def trading_sessions(calendar: str | None, start: date, end: date) -> frozenset[date] | None:
    """Exchange sessions in [start, end] (None if the calendar is unknown). Calendars are cached per code."""
    if not calendar or end < start:
        return None
    allss = _all_sessions(calendar)
    if not allss:
        return None
    import bisect  # noqa: PLC0415

    i, j = bisect.bisect_left(allss, start), bisect.bisect_right(allss, end)
    return frozenset(allss[i:j])


def calendar_first_session(calendar: str | None) -> date | None:
    allss = _all_sessions(calendar) if calendar else None
    return allss[0] if allss else None


def last_completed_session(calendar: str = "XNYS", today: date | None = None) -> date:
    today = today or datetime.now(UTC).date()
    ss = trading_sessions(calendar, today - timedelta(days=14), today - timedelta(days=1))
    if not ss:
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d
    return max(ss)


# ================================================================================================ adjustments


def compute_adjustment_factors(bars: pl.DataFrame, actions: pl.DataFrame, key: str = "security_id") -> pl.DataFrame:
    """Backward-looking cumulative adjustment factors (CRSP style; latest bar has factor 1).

    bars:    [key, date, close] with RAW closes (same units as the dividend amounts).
    actions: [key, ex_date, action_type, ratio, amount] — splits use ratio new/old (2.0 = 2-for-1), cash dividends
             use the RAW per-share amount.
    Returns bars + split_factor, adj_factor (adj_price = raw_price * factor) and `__div_bad` (dividend ignored
    because amount >= previous close or no previous close).
    For a split with ex-date e and ratio r, every bar before e is multiplied by 1/r. For a cash dividend D with
    ex-date e and previous raw close P, every bar before e is multiplied by (1 - D/P).
    """
    b = bars.select(key, "date", "close").sort("date")
    empty = b.with_columns(pl.lit(1.0).alias("split_factor"), pl.lit(1.0).alias("adj_factor"))
    if actions is None or actions.is_empty():
        return bars.join(empty.select(key, "date", "split_factor", "adj_factor"), on=[key, "date"], how="left")             .with_columns(pl.lit(None, pl.Boolean).alias("__div_bad"))
    a = actions.filter(pl.col(key).is_in(b.get_column(key).unique().to_list()))
    splits = a.filter(pl.col("action_type").is_in(["split", "stock_dividend"]) & (pl.col("ratio") > 0)
                      & ((pl.col("ratio") - 1.0).abs() > 1e-9)).select(
        key, "ex_date", (1.0 / pl.col("ratio")).alias("m_split"), (1.0 / pl.col("ratio")).alias("m_total"))
    divs = a.filter(pl.col("action_type").is_in(["cash_dividend", "special_dividend"]) & (pl.col("amount") > 0)).select(
        key, "ex_date", "amount").with_columns((pl.col("ex_date") - pl.duration(days=1)).alias("__lk")).sort("__lk")
    if divs.height:
        divs = _asof(divs, b.rename({"date": "__pd", "close": "__pc"}), left_on="__lk", right_on="__pd", by=key,
                              strategy="backward")
        divs = divs.with_columns((1.0 - pl.col("amount") / pl.col("__pc")).alias("__f"))
        ok = pl.col("__f").is_not_null() & (pl.col("__f") > 0.0) & (pl.col("__f") < 1.0)
        bad = divs.filter(~ok).select(key, "ex_date")
        divs = divs.filter(ok).select(key, "ex_date", pl.lit(1.0).alias("m_split"), pl.col("__f").alias("m_total"))
    else:
        bad = pl.DataFrame(schema={key: pl.Utf8, "ex_date": pl.Date})
        divs = pl.DataFrame(schema={key: pl.Utf8, "ex_date": pl.Date, "m_split": pl.Float64, "m_total": pl.Float64})
    ev = pl.concat([splits.cast({"m_split": pl.Float64, "m_total": pl.Float64}), divs], how="vertical_relaxed")
    if ev.is_empty():
        out = empty
    else:
        ev = (ev.group_by(key, "ex_date").agg(pl.col("m_split").product(), pl.col("m_total").product())
              .sort([key, "ex_date"], descending=[False, True])
              .with_columns(pl.col("m_split").cum_prod().over(key).alias("C_split"),
                            pl.col("m_total").cum_prod().over(key).alias("C_total"))
              .sort("ex_date"))
        bb = b.with_columns((pl.col("date") + pl.duration(days=1)).alias("__nx")).sort("__nx")
        j = _asof(bb, ev.select(key, "ex_date", "C_split", "C_total"), left_on="__nx", right_on="ex_date", by=key,
                         strategy="forward")
        out = j.select(key, "date", pl.col("C_split").fill_null(1.0).alias("split_factor"),
                       pl.col("C_total").fill_null(1.0).alias("adj_factor"))
    res = bars.join(out.select(key, "date", "split_factor", "adj_factor"), on=[key, "date"], how="left")
    if bad.height:
        res = res.join(bad.with_columns(pl.lit(True).alias("__div_bad")).rename({"ex_date": "date"}),
                       on=[key, "date"], how="left")
    else:
        res = res.with_columns(pl.lit(None, pl.Boolean).alias("__div_bad"))
    return res


def unadjust_split_basis(bars: pl.DataFrame, splits: pl.DataFrame, key: str = "symbol") -> pl.DataFrame:
    """Convert split-adjusted bars (Yahoo Close/Volume) to raw prints: price *= prod(ratios of splits with ex > d),
    volume /= same. `splits`: [key, ex_date, ratio]. Exact when `splits` covers every split after each bar date
    (true for a download whose window ends today)."""
    if splits is None or splits.is_empty():
        return bars.with_columns(pl.lit(1.0).alias("__S"))
    ev = (splits.filter((pl.col("ratio") > 0) & ((pl.col("ratio") - 1).abs() > 1e-9))
          .group_by(key, "ex_date").agg(pl.col("ratio").product())
          .sort([key, "ex_date"], descending=[False, True])
          .with_columns(pl.col("ratio").cum_prod().over(key).alias("__S")).sort("ex_date"))
    if ev.is_empty():
        return bars.with_columns(pl.lit(1.0).alias("__S"))
    bb = bars.with_columns((pl.col("date") + pl.duration(days=1)).alias("__nx")).sort("__nx")
    j = _asof(bb, ev.select(key, pl.col("ex_date").alias("__ex"), "__S"), left_on="__nx", right_on="__ex", by=key,
                     strategy="forward").drop("__nx", "__ex")
    j = j.with_columns(pl.col("__S").fill_null(1.0))
    price_cols = [c for c in ("open", "high", "low", "close") if c in j.columns]
    exprs = [(pl.col(c) * pl.col("__S")).alias(c) for c in price_cols]
    if "volume" in j.columns:
        exprs.append((pl.col("volume") / pl.col("__S")).alias("volume"))
    return j.with_columns(exprs)


# ================================================================================================ reconciliation / dq

#: official / exchange sources outrank Yahoo, which fills gaps and earlier history (price-history workstream: B3
#: COTAHIST raised from 4 to 2 now that Yahoo stages full .SA histories)
SOURCE_PRIORITY: dict[str, int] = {"polygon": 0, "krx": 1, "baostock": 2, "twse": 2, "tpex": 2, "b3_cotahist": 2,
                                   "yfinance": 3, "other": 9}


def reconcile_sources(bars: pl.DataFrame, tolerance: float = 0.01) -> tuple[pl.DataFrame, dict[str, Any]]:
    """bars: resolved staging rows already on a RAW, major-unit basis with columns security_id, date, source_id,
    close. Returns (diffs per overlapping (security, date) with rel_diff and flag, summary dict)."""
    multi = bars.group_by("security_id", "date").agg(pl.col("source_id").n_unique().alias("n")).filter(pl.col("n") > 1)
    if multi.is_empty():
        return pl.DataFrame(schema={"security_id": pl.Utf8, "date": pl.Date, "rel_diff": pl.Float64,
                                    "disagree": pl.Boolean}), {"overlap_rows": 0, "flagged_rows": 0}
    ov = bars.join(multi.select("security_id", "date"), on=["security_id", "date"], how="inner")
    agg = ov.group_by("security_id", "date").agg(
        pl.col("close").max().alias("cmax"), pl.col("close").min().alias("cmin"),
        pl.col("source_id").sort().str.join(",").alias("sources"))
    agg = agg.with_columns(((pl.col("cmax") - pl.col("cmin")) / pl.col("cmin")).alias("rel_diff"))
    agg = agg.with_columns((pl.col("rel_diff") > tolerance).alias("disagree"))
    flagged = agg.filter(pl.col("disagree"))
    worst = (flagged.group_by("security_id").agg(pl.len().alias("days"), pl.col("rel_diff").max().alias("max_diff"))
             .sort("days", descending=True).head(15))
    summary = {
        "tolerance": tolerance,
        "overlap_rows": agg.height,
        "overlap_securities": agg.get_column("security_id").n_unique(),
        "flagged_rows": flagged.height,
        "flagged_share": round(flagged.height / agg.height, 5) if agg.height else 0.0,
        "median_rel_diff": float(agg.get_column("rel_diff").median() or 0.0),
        "p99_rel_diff": float(agg.get_column("rel_diff").quantile(0.99) or 0.0),
        "worst": worst.to_dicts(),
    }
    return agg.select("security_id", "date", "rel_diff", "disagree"), summary


def _dq_flags(df: pl.DataFrame, outlier_threshold: float) -> pl.DataFrame:
    """Adds `dq_flags` (list[str] | null) from row-level checks. Requires sorted (security_id, date)."""
    eps = 1e-6
    ohlc_bad = (
        pl.all_horizontal(pl.col("open").is_not_null(), pl.col("high").is_not_null(), pl.col("low").is_not_null())
        & ((pl.col("high") < pl.max_horizontal("open", "close", "low") * (1 - eps))
           | (pl.col("low") > pl.min_horizontal("open", "close", "high") * (1 + eps)))
    )
    ret = (pl.col("adj_close") / pl.col("adj_close").shift(1).over("security_id") - 1.0)
    same = (pl.col("close") == pl.col("close").shift(1).over("security_id"))
    run_id = (~same.fill_null(False)).cast(pl.Int32).cum_sum().over("security_id")
    df = df.with_columns(ret.alias("__ret"), run_id.alias("__run"))
    df = df.with_columns(pl.int_range(pl.len()).over(["security_id", "__run"]).alias("__pos"))
    no_vol = pl.col("volume").is_null() | (pl.col("volume") == 0)
    flags = pl.concat_list([
        pl.when(ohlc_bad).then(pl.lit("ohlc_inconsistent")),
        pl.when((pl.col("volume") == 0) & ~pl.col("__is_bm")).then(pl.lit("zero_volume")),
        pl.when((pl.col("__pos") >= 4) & no_vol).then(pl.lit("stale")),
        pl.when((pl.col("__ret").abs() > outlier_threshold) & ~pl.col("__has_action").fill_null(False))
        .then(pl.lit("return_outlier")),
        pl.when(pl.col("__disagree").fill_null(False)).then(pl.lit("source_disagreement")),
        pl.when(pl.col("__non_session").fill_null(False)).then(pl.lit("non_session")),
        pl.when(pl.col("__div_bad").fill_null(False)).then(pl.lit("dividend_ignored")),
        pl.when(pl.col("provisional")).then(pl.lit("provisional_id")),
        pl.when(pl.col("__unit_fix").fill_null(False) if "__unit_fix" in df.columns else pl.lit(False))
        .then(pl.lit("unit_rescaled")),
    ]).list.drop_nulls()
    df = df.with_columns(pl.when(flags.list.len() > 0).then(flags).otherwise(None).alias("dq_flags"))
    return df.drop("__ret", "__run", "__pos")


# ================================================================================================ fx


#: currencies whose only official daily-ish source lags (FRED H.10 weekly for TWD): as-of tolerance in days
SLOW_FX_GAP_DAYS: dict[str, int] = {"TWD": 12}


def attach_usd(df: pl.DataFrame, settings: Settings | None = None, max_gap_days: int = 5) -> pl.DataFrame:
    """Add `usd_per_unit` (as-of fx_rates rate, <= max_gap_days stale) for df[currency, date]."""
    ccys = [c for c in df.get_column("currency").unique().to_list() if c]
    fx = None
    if _table_has_data("fx_rates", settings) and ccys:
        fx = (scan_table("fx_rates", settings).filter(pl.col("currency").is_in(ccys))
              .select("date", "currency", "usd_per_unit").collect().sort("date"))
    if fx is None or fx.is_empty():
        return df.with_columns(pl.when(pl.col("currency") == "USD").then(1.0).otherwise(None).alias("usd_per_unit"))
    fx = fx.rename({"date": "__fxd"})
    out = _asof(df.sort("date"), fx, left_on="date", right_on="__fxd", by="currency", strategy="backward",
                                    tolerance=timedelta(days=max_gap_days)).drop("__fxd")
    # slow official sources (TWD: weekly Fed H.10 only) get a longer as-of tolerance instead of a null close_usd
    slow = {c: d for c, d in SLOW_FX_GAP_DAYS.items() if c in ccys and d > max_gap_days}
    if slow and out.filter(pl.col("usd_per_unit").is_null() & pl.col("currency").is_in(list(slow))).height:
        for c, d in slow.items():
            m = pl.col("usd_per_unit").is_null() & (pl.col("currency") == c)
            fill = _asof(out.filter(m).drop("usd_per_unit").sort("date"), fx.filter(pl.col("currency") == c),
                         left_on="date", right_on="__fxd", by="currency", strategy="backward",
                         tolerance=timedelta(days=d)).drop("__fxd")
            out = pl.concat([out.filter(~m.fill_null(False)), fill.select(out.columns)], how="vertical_relaxed")
    return out.with_columns(pl.when(pl.col("currency") == "USD").then(1.0).otherwise(pl.col("usd_per_unit"))
                            .alias("usd_per_unit"))


# ================================================================================================ finalize


def _load_staged_actions(symbols: pl.DataFrame, settings: Settings) -> pl.DataFrame:
    """corporate_actions_staging rows for the resolved symbols, on RAW basis, with security_id."""
    if not _table_has_data("corporate_actions_staging", settings):
        return pl.DataFrame(schema={"security_id": pl.Utf8, "ex_date": pl.Date, "action_type": pl.Utf8,
                                    "ratio": pl.Float64, "amount": pl.Float64, "source_id": pl.Utf8})
    ca = scan_table("corporate_actions_staging", settings).collect()
    # yfinance amounts are stored raw by yf_prices (converted at ingestion); split_adjusted amounts (other sources)
    # would need un-adjusting — none are written today.
    ca = ca.join(symbols.select("source_id", "symbol", "security_id", "price_divisor").unique(), on=["source_id", "symbol"],
                 how="inner")
    ca = ca.with_columns((pl.col("amount") / pl.col("price_divisor")).alias("amount"))
    # one row per (security, ex_date, action_type): source priority
    ca = ca.with_columns(pl.col("source_id").replace_strict(SOURCE_PRIORITY, default=9).alias("__p")).sort("__p") \
        .unique(subset=["security_id", "ex_date", "action_type"], keep="first")
    return ca.select("security_id", "ex_date", "action_type", "ratio", "amount", "source_id")


def _resolve_stage(staged: pl.DataFrame, universe: pl.DataFrame, actions_staged: pl.DataFrame | None,
                   s: Settings, report: dict[str, Any]) -> tuple[pl.DataFrame, pl.DataFrame] | None:
    """First half of build_prices (shared with the incremental finalize's stitch-decision pass): resolve symbols, raw
    major units, corporate actions per security, O2.2a unfinished-bar and identity-break guards. Returns (res, ca)
    or None when nothing resolves."""
    res = resolve_symbols(staged, universe)
    report["resolved_rows"] = res.height
    report["unresolved_symbols"] = staged.select("source_id", "symbol").unique().height - \
        res.select("source_id", "symbol").unique().height
    if res.is_empty():
        return None
    # to raw major units
    res = res.filter(pl.col("close") > 0)
    if "price_basis" in res.columns and res.filter(pl.col("price_basis") == "split_adjusted").height:
        report["split_adjusted_rows_unconverted"] = res.filter(pl.col("price_basis") == "split_adjusted").height
    # provisional rows: trust the currency delivered by the source (fixes .L lines quoted in GBP/USD, not GBp)
    if "src_currency" in res.columns:
        prov = pl.col("provisional") & pl.col("src_currency").is_not_null()
        res = res.with_columns(pl.when(prov).then(pl.col("src_currency")).otherwise(pl.col("quote_currency"))
                               .alias("quote_currency"))
        res = res.with_columns(
            pl.when(prov).then(pl.col("quote_currency").replace_strict({k: v[1] for k, v in MINOR_UNITS.items()},
                                                                        default=1.0, return_dtype=pl.Float64))
            .otherwise(pl.col("price_divisor")).alias("price_divisor"),
            pl.when(prov).then(pl.col("quote_currency").replace({k: v[0] for k, v in MINOR_UNITS.items()}))
            .otherwise(pl.col("currency")).alias("currency"))
    div = pl.col("price_divisor")
    res = res.with_columns([(pl.col(c) / div).alias(c) for c in ("open", "high", "low", "close", "vwap")])
    # corporate actions (resolved to securities; also used by the identity-break check to excuse split jumps)
    if actions_staged is not None and actions_staged.height:
        sym = res.select("source_id", "symbol", "security_id", "price_divisor").unique()
        ca = actions_staged.join(sym, on=["source_id", "symbol"], how="inner").with_columns(
            (pl.col("amount") / pl.col("price_divisor")).alias("amount"),
            pl.col("source_id").replace_strict(SOURCE_PRIORITY, default=9).alias("__p"))
        ca = ca.sort("__p").unique(subset=["security_id", "ex_date", "action_type"], keep="first") \
            .select("security_id", "ex_date", "action_type", "ratio", "amount")
    else:
        ca = pl.DataFrame(schema={"security_id": pl.Utf8, "ex_date": pl.Date, "action_type": pl.Utf8,
                                  "ratio": pl.Float64, "amount": pl.Float64})
    # O2.2a integrity guards: unfinished Yahoo bars, then identifier-reuse breaks per source series
    from app.normalize import price_integrity as PI  # noqa: PLC0415

    res, report["unfinished_bars_dropped"] = PI.drop_unfinished_staged(res)
    breaks = PI.detect_identity_breaks(res.select("security_id", "source_id", "date", "close", "volume"), ca)
    breaks = PI.apply_overrides(breaks, PI.load_break_overrides(s), res.select("source_id", "symbol", "security_id").unique())
    res, report["identity_breaks"] = PI.apply_identity_breaks(res, breaks)
    report["identity_breaks"]["candidates"] = breaks.height
    report["_break_sids"] = set(breaks.filter(pl.col("is_break")).get_column("security_id").to_list()) \
        if breaks.height else set()
    return res, ca


def build_prices(staged: pl.DataFrame, universe: pl.DataFrame, actions_staged: pl.DataFrame | None = None, *,
                 tolerance: float = 0.01, outlier_threshold: float = 0.5, settings: Settings | None = None,
                 stitch_decisions: pl.DataFrame | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Pure transform (testable): staged rows -> prices_daily rows + report. `actions_staged` are staging-shaped
    corporate actions (source_id, symbol, ex_date, action_type, ratio, amount). `stitch_decisions` (incremental
    finalize): per-security stitch-guard decisions computed on the full history (stitch_decisions()), used instead of
    statistics of a partial window. report["_break_sids"] / ["_repair_sids"] list the securities whose identity-break /
    unit-repair guards fired (the incremental finalize re-builds those from their full history)."""
    s = settings or get_settings()
    report: dict[str, Any] = {"staged_rows": staged.height}
    rc = _resolve_stage(staged, universe, actions_staged, s, report)
    if rc is None:
        return pl.DataFrame(), report
    res, ca = rc
    from app.normalize import price_integrity as PI  # noqa: PLC0415

    # reconciliation across sources
    diffs, rsum = reconcile_sources(res.select("security_id", "date", "source_id", "close"), tolerance)
    report["reconciliation"] = rsum
    # winner per (security, date)
    res = res.with_columns(pl.col("source_id").replace_strict(SOURCE_PRIORITY, default=9).alias("__p"))
    res, report["stitch_guard"] = _stitch_guard(res, tolerance, decisions=stitch_decisions)
    report["stitch_guard"].pop("_decisions", None)
    win = res.sort(["security_id", "date", "__p"]).unique(subset=["security_id", "date"], keep="first")
    win = win.join(diffs.select("security_id", "date", pl.col("disagree").alias("__disagree")),
                   on=["security_id", "date"], how="left")
    report["winner_by_source"] = dict(win.group_by("source_id").len().iter_rows())
    # corporate actions -> factors (ca resolved above)
    report["corporate_actions_used"] = ca.height
    win = compute_adjustment_factors(win, ca)
    act_days = ca.select("security_id", pl.col("ex_date").alias("date")).unique().with_columns(
        pl.lit(True).alias("__has_action"))
    win = win.join(act_days, on=["security_id", "date"], how="left")
    # O2.2a: 10^k unit flips (GBp/GBP days, double-unadjusted reverse splits, unrecorded splits)
    fx_only = "region" in win.columns and win.get_column("region").unique().to_list() == ["FX"]
    # FX pairs never move 10x in a day: also repair 10x flips there (Yahoo FX scale glitches, PHASE3_TODO)
    win, report["unit_repairs"] = PI.repair_unit_scale(win, PI.UnitParams(decades=(1, 2, 3)) if fx_only else None)
    report["_repair_sids"] = set(win.filter(pl.col("__unit_fix")).get_column("security_id").unique().to_list()) \
        if "__unit_fix" in win.columns else set()
    win = win.with_columns((pl.col("close") * pl.col("adj_factor")).alias("adj_close"))
    # calendars
    win = _flag_sessions(win, report)
    # fx
    win = attach_usd(win, s)
    win = win.with_columns((pl.col("close") * pl.col("usd_per_unit")).alias("close_usd"))
    win = win.with_columns((pl.col("close_usd") * pl.col("volume")).alias("dollar_volume_usd"))
    report["missing_fx_rows"] = win.filter(pl.col("close_usd").is_null()).height
    win = win.with_columns(pl.col("is_benchmark").fill_null(False).alias("__is_bm")).sort(["security_id", "date"])
    win = _dq_flags(win, outlier_threshold)
    out = win.select(
        "security_id", "listing_id", "date", "region", pl.col("date").dt.year().cast(pl.Int16).alias("year"),
        "open", "high", "low", "close", "volume", "vwap", "trades", "adj_factor", "split_factor", "adj_close",
        "currency", "close_usd", "dollar_volume_usd",
        pl.col("source_id").replace({"krx": "other", "baostock": "other", "b3_cotahist": "other", "twse": "other",
                                      "tpex": "other"}).alias("source_id"),
        "dq_flags", pl.lit(datetime.now(UTC)).alias("ingested_at"), "usd_per_unit")
    flag_counts = (out.select(pl.col("dq_flags").explode().alias("f")).drop_nulls().group_by("f").len()
                   .sort("len", descending=True))
    report["dq_flag_counts"] = dict(flag_counts.iter_rows())
    report["rows_out"] = out.height
    report["securities_out"] = out.get_column("security_id").n_unique()
    report["provisional_securities"] = out.filter(pl.col("security_id").str.starts_with("P:")) \
        .get_column("security_id").n_unique()
    return out, report


#: official bulk sources whose raw prints Yahoo history is stitched onto (price-history workstream)
STITCH_GUARD_SOURCES = ("baostock", "twse", "tpex", "b3_cotahist", "krx")


def _stitch_guard(res: pl.DataFrame, tolerance: float = 0.01, junction_days: int = 20,
                  max_share: float = 0.05, decisions: pl.DataFrame | None = None) -> tuple[pl.DataFrame, dict[str, Any]]:
    """Yahoo history stitched onto an official raw series must be on the same basis. Yahoo back-adjusts some markets
    for events it does not publish as splits (TW stock dividends / capital reductions, BR / CN rights), so its "raw"
    prints drift away from the official ones going back in time. Per security with an official bulk source
    (STITCH_GUARD_SOURCES) and Yahoo bars:
    * junction: median |yf/official - 1| over the first `junction_days` overlapping dates > tolerance -> Yahoo bars
      dated before the official series starts are dropped (they would create a level jump at the junction);
    * overall: share of overlapping dates with |yf/official - 1| > tolerance above `max_share` -> Yahoo gap-fill bars
      inside the official coverage are dropped as well.
    Overlapping Yahoo bars are kept (they lose to the official source and feed the reconciliation).

    `decisions` (incremental finalize): rows [security_id, __d0, __jmed, __share, __jbad, __sbad] computed on the
    FULL history replace the statistics of the (partial) window for those securities. rep["_decisions"] carries the
    decision of every security with an overlap (the incremental finalize's decision pass)."""
    rep: dict[str, Any] = {"securities_junction_bad": 0, "securities_overlap_bad": 0, "yahoo_rows_dropped": 0}
    off = res.filter(pl.col("source_id").is_in(list(STITCH_GUARD_SOURCES)))
    is_yf = pl.col("source_id") == "yfinance"
    if off.is_empty() or not res.filter(is_yf).height:
        return res, rep
    o = off.group_by("security_id", "date").agg(pl.col("close").first().alias("__co"))
    ov = (res.filter(is_yf).select("security_id", "date", pl.col("close").alias("__cy"))
          .join(o, on=["security_id", "date"], how="inner").sort("security_id", "date")
          .with_columns(((pl.col("__cy") / pl.col("__co")) - 1.0).abs().alias("__rel"))
          .with_columns(pl.int_range(pl.len()).over("security_id").alias("__rn")))
    if ov.is_empty() and decisions is None:
        return res, rep
    st = ov.group_by("security_id").agg(
        pl.col("__rel").filter(pl.col("__rn") < junction_days).median().alias("__jmed"),
        (pl.col("__rel") > tolerance).mean().alias("__share"))
    d0 = o.group_by("security_id").agg(pl.col("date").min().alias("__d0"))
    st = st.join(d0, on="security_id", how="left").with_columns(
        (pl.col("__jmed") > tolerance).alias("__jbad"), (pl.col("__share") > max_share).alias("__sbad"))
    st = st.select("security_id", "__d0", "__jmed", "__share", "__jbad", "__sbad")
    if decisions is not None:
        dec = decisions.select(st.columns).cast(st.schema)  # type: ignore[arg-type]
        st = pl.concat([st.join(dec.select("security_id"), on="security_id", how="anti"), dec], how="vertical")
    rep["_decisions"] = st
    bad = st.filter(pl.col("__jbad") | pl.col("__sbad"))
    rep["securities_junction_bad"] = int(st["__jbad"].sum())
    rep["securities_overlap_bad"] = int(st["__sbad"].sum())
    if bad.is_empty():
        return res, rep
    tagged = (res.join(bad.select("security_id", "__d0", "__jbad", "__sbad"), on="security_id", how="left")
              .join(o.select("security_id", "date", pl.lit(True).alias("__has_off")), on=["security_id", "date"],
                    how="left"))
    drop = is_yf & pl.col("__d0").is_not_null() & (
        (pl.col("__jbad") & (pl.col("date") < pl.col("__d0")))
        | (pl.col("__sbad") & (pl.col("date") >= pl.col("__d0")) & pl.col("__has_off").is_null()))
    n0 = tagged.height
    out = tagged.filter(~drop.fill_null(False)).drop("__d0", "__jbad", "__sbad", "__has_off")
    rep["yahoo_rows_dropped"] = n0 - out.height
    rep["worst"] = bad.sort("__share", descending=True).head(10).select(
        "security_id", pl.col("__jmed").round(4).alias("junction_median_rel"),
        pl.col("__share").round(3).alias("overlap_share_gt_tol")).to_dicts()
    return out, rep


def _flag_sessions(win: pl.DataFrame, report: dict[str, Any]) -> pl.DataFrame:
    """non_session flag + missing-session counts per security (report only, no gap filling)."""
    if "calendar" not in win.columns:
        return win.with_columns(pl.lit(False).alias("__non_session"))
    rng = win.group_by("security_id", "calendar").agg(pl.col("date").min().alias("d0"), pl.col("date").max().alias("d1"),
                                                      pl.col("date"))
    non_session_keys: list[tuple[str, date]] = []
    missing: dict[str, int] = {}
    for r in rng.iter_rows(named=True):
        first = calendar_first_session(r["calendar"])
        if first is None:
            continue
        ss = trading_sessions(r["calendar"], max(r["d0"], first), r["d1"])
        if not ss:
            continue
        dates = {d for d in r["date"] if d >= first}
        non_session_keys.extend((r["security_id"], d) for d in dates if d not in ss)
        m = len([d for d in ss if d not in dates])
        if m:
            missing[r["security_id"]] = m
    report["non_session_rows"] = len(non_session_keys)
    report["missing_sessions_total"] = int(sum(missing.values()))
    report["missing_sessions_top"] = dict(sorted(missing.items(), key=lambda kv: -kv[1])[:10])
    if non_session_keys:
        ns = pl.DataFrame({"security_id": [k[0] for k in non_session_keys], "date": [k[1] for k in non_session_keys]},
                          schema={"security_id": pl.Utf8, "date": pl.Date}).with_columns(
            pl.lit(True).alias("__non_session"))
        return win.join(ns, on=["security_id", "date"], how="left")
    return win.with_columns(pl.lit(False).alias("__non_session"))


def finalize(settings: Settings | None = None, regions: Sequence[str] | None = None, tolerance: float = 0.01,
             write: bool = True, chunk_securities: int | None = None, mode: str = "auto",
             parallel: int | None = None) -> dict[str, Any]:
    """prices_staging -> prices_daily. Returns the report.

    mode="auto" (default, refresh-pipeline fixes 2026-10-26): incremental per region since the region's finalised
    watermark (app.normalize.prices_incremental: tail re-builds of the changed securities, full-history re-builds where
    a guard / action / revision needs it, parallel regions). mode="full": the O2.2a full rebuild per region via
    overwrite_partition (back-fills; regions built one at a time, bounded memory with the price-coverage markets); it
    also resets the regions' watermarks."""
    s = settings or get_settings()
    if mode not in ("auto", "incremental", "full"):
        raise ValueError(f"mode must be auto|incremental|full, got {mode!r}")
    if mode != "full":
        from app.normalize.prices_incremental import finalize_incremental  # noqa: PLC0415

        return finalize_incremental(s, regions=regions, tolerance=tolerance, write=write,
                                    chunk_securities=chunk_securities, parallel=parallel)
    t0 = time.perf_counter()
    if not _table_has_data("prices_staging", s):
        return {"status": "skipped", "reason": "prices_staging empty"}
    t_scan = datetime.now(UTC)
    uni = load_universe(s, include_delisted=True)
    todo = list(regions) if regions else sorted(r for r in uni.get_column("region").unique().to_list() if r)
    if len(todo) == 1:
        rep = _finalize_region(s, uni.filter(pl.col("region") == todo[0]), tolerance, write, t0, chunk_securities)
        if write:
            _mark_full(s, todo, t_scan)
        return rep
    agg: dict[str, Any] = {"by_region": {}, "rows_written": 0, "rows_out": 0, "securities_out": 0,
                           "provisional_securities": 0, "reconciliation": {"overlap_rows": 0, "flagged_rows": 0},
                           "winner_by_source": {}, "dq_flag_counts": {}}
    agg["identity_series_split"] = agg["unit_securities_repaired"] = 0
    for r in todo:
        rep = _finalize_region(s, uni.filter(pl.col("region") == r), tolerance, write, time.perf_counter(),
                               chunk_securities)
        agg["by_region"][r] = {k: rep.get(k) for k in ("rows_out", "securities_out", "seconds_total")}
        agg["by_region"][r]["identity_breaks"] = {k: (rep.get("identity_breaks") or {}).get(k)
                                                  for k in ("series_split", "rows_dropped")}
        agg["by_region"][r]["unit_repairs"] = (rep.get("unit_repairs") or {}).get("by_reason")
        agg["identity_series_split"] += int((rep.get("identity_breaks") or {}).get("series_split") or 0)
        agg["unit_securities_repaired"] += int((rep.get("unit_repairs") or {}).get("securities_repaired") or 0)
        for k in ("rows_written", "rows_out", "securities_out", "provisional_securities"):
            agg[k] += int(rep.get(k) or 0)
        for k in ("overlap_rows", "flagged_rows"):
            agg["reconciliation"][k] += int((rep.get("reconciliation") or {}).get(k) or 0)
        for fld in ("winner_by_source", "dq_flag_counts"):
            for k, v in (rep.get(fld) or {}).items():
                agg[fld][k] = agg[fld].get(k, 0) + int(v or 0)
    agg["seconds_total"] = round(time.perf_counter() - t0, 2)
    agg["mode"] = "full"
    _write_report("prices_finalize_all", agg, s)
    if write:
        _mark_full(s, todo, t_scan)
    return agg


def _mark_full(s: Settings, regions: Sequence[str], t_scan: datetime) -> None:
    try:
        from app.normalize.prices_incremental import mark_full_watermarks  # noqa: PLC0415

        mark_full_watermarks(s, regions, t_scan)
    except Exception as e:  # noqa: BLE001 - the next auto run bootstraps the watermark from prices_daily
        log.warning("prices_finalize: watermark state not saved (%s)", e)


#: securities per build chunk inside one region (bounded memory once Yahoo long histories are staged: a region is
#: built in security chunks, the outputs are concatenated and written once per (region, year) partition)
FINALIZE_CHUNK_SECURITIES = 1500


def _staged_for(uni: pl.DataFrame, s: Settings) -> pl.DataFrame:
    keys_poly = uni.get_column("polygon_ticker").drop_nulls().to_list()
    keys_yf = uni.get_column("key").drop_nulls().to_list()
    return scan_table("prices_staging", s).filter(
        ((pl.col("source_id") == "polygon") & (pl.col("symbol").is_in(keys_poly) |
                                                pl.col("symbol").str.replace_all(r"\.", "-").is_in(keys_yf)))
        | ((pl.col("source_id") != "polygon") & pl.col("symbol").is_in(keys_yf))
    ).collect(engine="streaming")  # O2.2a: streaming scan of the 1.8 GB staging (peak ~2.7 GB instead of ~11 GB)


def _merge_reports(reps: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-chunk build reports of one region (counts summed; reconciliation quantiles approximated)."""
    if len(reps) == 1:
        return reps[0]
    out: dict[str, Any] = {"chunks": len(reps)}
    for k in ("staged_rows", "resolved_rows", "unresolved_symbols", "corporate_actions_used", "non_session_rows",
              "missing_sessions_total", "missing_fx_rows", "rows_out", "securities_out", "provisional_securities",
              "split_adjusted_rows_unconverted"):
        if any(k in r for r in reps):
            out[k] = sum(int(r.get(k) or 0) for r in reps)
    for fld in ("winner_by_source", "dq_flag_counts"):
        agg: dict[str, int] = {}
        for r in reps:
            for k, v in (r.get(fld) or {}).items():
                agg[k] = agg.get(k, 0) + int(v or 0)
        out[fld] = agg
    for fld, keys in (("identity_breaks", ("series_split", "securities_split", "rows_dropped", "candidates")),
                      ("unit_repairs", ("securities_repaired", "bars_rescaled", "unresolved"))):
        parts = [r.get(fld) or {} for r in reps]
        out[fld] = {k: sum(int(x.get(k) or 0) for x in parts) for k in keys}
        lst = "breaks" if fld == "identity_breaks" else "repairs_top"
        out[fld][lst] = [b for x in parts for b in (x.get(lst) or [])][:50]
        if fld == "unit_repairs":
            by: dict[str, int] = {}
            for x in parts:
                for k, v in (x.get("by_reason") or {}).items():
                    by[k] = by.get(k, 0) + int(v)
            out[fld]["by_reason"] = by
    out["unfinished_bars_dropped"] = sum(int(r.get("unfinished_bars_dropped") or 0) for r in reps)
    sg = [r.get("stitch_guard") or {} for r in reps]
    out["stitch_guard"] = {k: sum(int(x.get(k) or 0) for x in sg)
                           for k in ("securities_junction_bad", "securities_overlap_bad", "yahoo_rows_dropped")}
    top: dict[str, int] = {}
    for r in reps:
        top.update(r.get("missing_sessions_top") or {})
    out["missing_sessions_top"] = dict(sorted(top.items(), key=lambda kv: -kv[1])[:10])
    recs = [r.get("reconciliation") or {} for r in reps]
    ov = sum(int(x.get("overlap_rows") or 0) for x in recs)
    fl = sum(int(x.get("flagged_rows") or 0) for x in recs)
    worst = sorted([w for x in recs for w in (x.get("worst") or [])], key=lambda w: -int(w.get("days") or 0))[:15]
    out["reconciliation"] = {
        "tolerance": next((x.get("tolerance") for x in recs if x.get("tolerance") is not None), None),
        "overlap_rows": ov, "overlap_securities": sum(int(x.get("overlap_securities") or 0) for x in recs),
        "flagged_rows": fl, "flagged_share": round(fl / ov, 5) if ov else 0.0,
        "median_rel_diff": max((float(x.get("median_rel_diff") or 0) for x in recs), default=0.0),
        "p99_rel_diff": max((float(x.get("p99_rel_diff") or 0) for x in recs), default=0.0),
        "worst": worst, "quantiles": "max over chunks"}
    return out


def _finalize_region(s: Settings, uni: pl.DataFrame, tolerance: float, write: bool, t0: float,
                     chunk_securities: int | None = None) -> dict[str, Any]:
    actions = scan_table("corporate_actions_staging", s).collect() if _table_has_data(
        "corporate_actions_staging", s) else None
    n = chunk_securities or FINALIZE_CHUNK_SECURITIES
    sids = uni.get_column("security_id").unique().sort().to_list()
    outs: list[pl.DataFrame] = []
    reps: list[dict[str, Any]] = []
    for part in _iter_chunks(sids, n):
        u = uni.filter(pl.col("security_id").is_in(list(part))) if len(sids) > n else uni
        o, r = build_prices(_staged_for(u, s), u, actions, tolerance=tolerance, settings=s)
        if o.height:
            outs.append(o)
        reps.append(r)
    out = pl.concat(outs, how="vertical_relaxed") if outs else pl.DataFrame()
    report = _merge_reports(reps)
    report["seconds_build"] = round(time.perf_counter() - t0, 2)
    if write and out.height:
        report["rows_written"] = write_table(out, "prices_daily", mode="overwrite_partition", settings=s)
        _drop_stale_partitions(out, s)
    report["seconds_total"] = round(time.perf_counter() - t0, 2)
    _write_report("prices_finalize", report, s)
    return report


def _drop_stale_partitions(out: pl.DataFrame, s: Settings) -> None:
    """Remove (region, year) partitions of rebuilt regions that no longer have rows (e.g. after provisional ids
    were replaced). Only regions present in `out` are touched."""
    root = table_dir("prices_daily", s)
    live = {(r, y) for r, y in out.select("region", "year").unique().iter_rows()}
    for r in {r for r, _ in live}:
        rdir = root / f"region={r}"
        if not rdir.exists():
            continue
        for ydir in rdir.glob("year=*"):
            try:
                y = int(ydir.name.split("=", 1)[1])
            except ValueError:
                continue
            if (r, y) not in live:
                import shutil  # noqa: PLC0415

                shutil.rmtree(ydir, ignore_errors=True)


def _write_report(name: str, report: dict[str, Any], s: Settings) -> Path:
    d = s.logs_dir / "reports"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}_{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.json"
    p.write_text(json.dumps(_public(report), default=str, indent=1), encoding="utf-8")
    return p


def _public(x: Any) -> Any:
    """Report without the internal `_*` keys (security sets used by the incremental finalize)."""
    if isinstance(x, dict):
        return {k: _public(v) for k, v in x.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(x, list):
        return [_public(v) for v in x]
    return x


def finalize_corporate_actions(settings: Settings | None = None, write: bool = True) -> int:
    """corporate_actions_staging -> corporate_actions (resolved security_id, raw amounts in trading currency)."""
    s = settings or get_settings()
    if not _table_has_data("corporate_actions_staging", s):
        return 0
    st = scan_table("corporate_actions_staging", s).collect()
    uni = load_universe(s)
    res = resolve_symbols(st, uni).rename({"src_currency": "__ccy"})
    if res.is_empty():
        return 0
    res = res.with_columns(
        (pl.col("amount") / pl.col("price_divisor")).alias("amount"),
        pl.coalesce(pl.when(pl.col("__ccy").is_in(list(MINOR_UNITS))).then(None).otherwise(pl.col("__ccy")),
                    pl.col("currency")).alias("ccy"),
        pl.col("source_id").replace_strict(SOURCE_PRIORITY, default=9).alias("__p"),
    ).sort("__p").unique(subset=["security_id", "ex_date", "action_type"], keep="first")
    out = res.select("security_id", "region", "ex_date", "action_type", "ratio", "amount",
                     pl.when(pl.col("amount").is_not_null()).then(pl.col("ccy")).alias("currency"),
                     "record_date", "pay_date", "declared_date", "details", "source_id",
                     pl.lit(datetime.now(UTC)).alias("ingested_at"))
    # full rebuild from staging (drops rows keyed by superseded provisional ids)
    return write_table(out, "corporate_actions", mode="overwrite", settings=s) if write else out.height


# ================================================================================================ job entry point


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    regions = p.get("regions")
    if isinstance(regions, str):  # CLI convenience: --param regions=APAC,CN,LATAM
        regions = [r.strip() for r in regions.split(",") if r.strip()]
    # chunk_securities: smaller security chunks bound peak memory (default FINALIZE_CHUNK_SECURITIES)
    cs = int(p["chunk_securities"]) if p.get("chunk_securities") else None
    # mode: auto (incremental since the per-region watermark, default) | full (O2.2a full rebuild, back-fills)
    mode = str(p.get("mode", "auto")).lower()
    if str(p.get("full", "false")).lower() in ("1", "true", "yes"):
        mode = "full"
    par = int(p["parallel"]) if p.get("parallel") else None
    rep = finalize(ctx.settings, regions=regions, tolerance=float(p.get("tolerance", 0.01)), chunk_securities=cs,
                   mode=mode, parallel=par)
    if rep.get("status") == "skipped":
        return JobResult(status="success", message=rep.get("reason"))
    rows = rep.get("rows_written", 0)
    rec = rep.get("reconciliation", {})
    msg = (f"mode={rep.get('mode', 'full')} rows={rows} securities={rep.get('securities_out')} overlap={rec.get('overlap_rows')} "
           f"disagree={rec.get('flagged_rows')} provisional={rep.get('provisional_securities')} "
           f"identity_splits={rep.get('identity_series_split', (rep.get('identity_breaks') or {}).get('series_split', 0))} "
           f"unit_repairs={rep.get('unit_securities_repaired', (rep.get('unit_repairs') or {}).get('securities_repaired', 0))}")
    if rep.get("mode") == "incremental":
        msg += " " + "; ".join(f"{r}: tail={v.get('tail_securities')} full={v.get('full_securities')} "
                               f"{v.get('seconds_total')}s" for r, v in (rep.get("by_region") or {}).items())
    errs = rep.get("errors") or []
    if errs:  # the other regions are written and their watermarks advanced; the failed ones retry next run
        return JobResult(status="partial", rows_written={"prices_daily": rows}, entities_ok=rep.get("securities_out", 0),
                         entities_failed=len(errs), message=f"regions failed: {errs}; {msg}"[:2000])
    return JobResult(status="success", rows_written={"prices_daily": rows}, entities_ok=rep.get("securities_out", 0),
                     message=msg[:2000])


__all__ = [
    "BENCHMARKS", "BENCHMARK_BY_TICKER", "SEED_US", "SEED_GLOBAL", "SUFFIX_MARKET", "MIC_MARKET", "MINOR_UNITS",
    "Market", "Benchmark", "benchmark_security_id", "country_to_region", "market_for_yf", "yf_suffix", "polygon_to_yf",
    "yf_to_polygon", "load_listings", "load_universe", "resolve_symbols", "trading_sessions", "last_completed_session",
    "compute_adjustment_factors", "unadjust_split_basis", "reconcile_sources", "attach_usd", "build_prices",
    "finalize", "finalize_corporate_actions", "run",
]


def _iter_chunks(seq: Sequence[Any], n: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]
