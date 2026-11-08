"""Market instruments for the price-history workstream (docs/PRICE_HISTORY_PLAN.md): FX pairs, extra commodity futures,
commodity indices / ETFs and volatility indices, priced through Yahoo Finance as benchmarks.

They are appended to app.normalize.prices.BENCHMARKS (synthetic ids `IDX:<yf>` / `ETF:<yf>`, so every existing
`NOT LIKE 'IDX:%' AND NOT LIKE 'ETF:%'` equity filter keeps excluding them) and registered by ref_universe /
identity_resolve with security_type fx | future | index | etf; never members of an equity universe.

FX POLICY: the Yahoo `XXXYYY=X` OHLC series are MARKET INSTRUMENTS (charts, Model Studio, technicals). Every currency
conversion in the platform (close_usd, fundamentals, macro) keeps using the official `fx_rates` table (ECB / FRED /
central banks) as the single source of truth; nothing reads these series for conversions.

Tuples: (yf_ticker, name, region, currency, kind, calendar). Instruments that Yahoo did not serve in the 2026-10-13
probe are left out (see docs/PRICE_HISTORY_PLAN.md, "Instruments").
"""

from __future__ import annotations

#: quote currency -> ISO2 country used as the listing's exchange_country for FX pairs (EURUSD=X -> US)
CCY_COUNTRY = {
    "USD": "US", "EUR": "DE", "JPY": "JP", "GBP": "GB", "CHF": "CH", "CAD": "CA", "AUD": "AU", "NZD": "NZ",
    "SEK": "SE", "NOK": "NO", "DKK": "DK", "SGD": "SG", "HKD": "HK", "CNY": "CN", "CNH": "HK", "KRW": "KR",
    "TWD": "TW", "INR": "IN", "IDR": "ID", "THB": "TH", "MYR": "MY", "PHP": "PH", "VND": "VN", "PKR": "PK",
    "MXN": "MX", "BRL": "BR", "ARS": "AR", "CLP": "CL", "COP": "CO", "PEN": "PE", "ZAR": "ZA", "TRY": "TR",
    "PLN": "PL", "CZK": "CZ", "HUF": "HU", "RON": "RO", "ILS": "IL", "SAR": "SA", "AED": "AE", "QAR": "QA",
    "KWD": "KW", "EGP": "EG", "NGN": "NG", "KES": "KE", "KZT": "KZ", "RUB": "RU", "ISK": "IS", "BGN": "BG",
}

#: FX pairs: majors / minors / EM vs USD, then EUR and other crosses (Yahoo `BASEQUOTE=X`; price = QUOTE per 1 BASE)
FX_USD_BASE = ["JPY", "CHF", "CAD", "SEK", "NOK", "DKK", "SGD", "HKD", "CNY", "CNH", "KRW", "TWD", "INR", "IDR", "THB",
               "MYR", "PHP", "VND", "PKR", "MXN", "BRL", "ARS", "CLP", "COP", "PEN", "ZAR", "TRY", "PLN", "CZK", "HUF",
               "RON", "ILS", "SAR", "AED", "QAR", "KWD", "EGP", "NGN", "KES", "KZT", "RUB", "ISK", "BGN"]
FX_PAIRS: list[str] = (
    ["EURUSD", "GBPUSD", "AUDUSD", "NZDUSD"] + [f"USD{c}" for c in FX_USD_BASE]
    + [f"EUR{c}" for c in ("GBP", "JPY", "CHF", "SEK", "NOK", "DKK", "PLN", "CZK", "HUF", "RON", "TRY", "AUD", "CAD",
                           "NZD", "CNY", "HKD", "SGD", "ZAR", "MXN", "INR", "BRL", "KRW", "ISK")]
    + ["GBPJPY", "GBPCHF", "GBPAUD", "AUDJPY", "AUDNZD", "AUDCAD", "CADJPY", "CHFJPY", "NZDJPY"]
)

#: extra continuous front-month futures (the P2 list in price_coverage_lists already has CL BZ NG RB HO GC SI HG PL PA
#: ZC ZW ZS KC SB CC CT LE HE + rates / equity / FX / BTC futures)
FUTURES_EXTRA: list[tuple[str, str]] = [
    ("ZO=F", "Oats"), ("ZL=F", "Soybean oil"), ("ZM=F", "Soybean meal"), ("KE=F", "KC HRW wheat"),
    ("ZR=F", "Rough rice"), ("OJ=F", "Orange juice"), ("LBR=F", "Lumber"), ("LBS=F", "Lumber (legacy)"),
    ("GF=F", "Feeder cattle"), ("DC=F", "Class III milk"), ("ALI=F", "Aluminum (COMEX)"), ("HRC=F", "US Midwest HRC steel"),
    ("TTF=F", "Dutch TTF natural gas"), ("UX=F", "Uranium"), ("MGC=F", "Micro gold"), ("QM=F", "E-mini crude oil"),
    ("B0=F", "Mont Belvieu propane"), ("ETH=F", "Ether (CME)"), ("SIL=F", "Micro silver"), ("MCL=F", "Micro WTI crude"),
]

#: commodity indices (index) and broad commodity ETFs/ETNs (etf) not already in the P2 ETF list
COMMODITY_INDICES: list[tuple[str, str]] = [
    ("^BCOM", "Bloomberg Commodity Index"), ("^SPGSCI", "S&P GSCI"), ("^BCOMTR", "Bloomberg Commodity TR"),
    ("^DJCI", "Dow Jones Commodity Index"), ("^CRB", "Refinitiv/CoreCommodity CRB"), ("^RJI", "Rogers International"),
]
COMMODITY_ETFS: list[tuple[str, str]] = [
    ("GSG", "iShares S&P GSCI Commodity-Indexed Trust"), ("DJP", "iPath Bloomberg Commodity ETN"),
    ("COMT", "iShares GSCI Commodity Dynamic Roll"), ("BCI", "abrdn Bloomberg All Commodity Strategy"),
    ("USCI", "United States Commodity Index Fund"), ("DBB", "Invesco DB Base Metals"), ("DBE", "Invesco DB Energy"),
    ("DBO", "Invesco DB Oil"), ("DBP", "Invesco DB Precious Metals"), ("UGA", "United States Gasoline Fund"),
    ("PALL", "abrdn Palladium"), ("SIVR", "abrdn Silver"), ("SGOL", "abrdn Gold"), ("JJA", "iPath Agriculture ETN"),
]

#: volatility indices (index): (yf, name, region, currency, calendar)
VOL_INDICES: list[tuple[str, str, str, str, str | None]] = [
    ("^VXN", "CBOE Nasdaq-100 Volatility", "US", "USD", "XNYS"), ("^VVIX", "CBOE VIX of VIX", "US", "USD", "XNYS"),
    ("^VIX9D", "CBOE S&P 500 9-Day Volatility", "US", "USD", "XNYS"),
    ("^VIX3M", "CBOE S&P 500 3-Month Volatility", "US", "USD", "XNYS"),
    ("^VIX6M", "CBOE S&P 500 6-Month Volatility", "US", "USD", "XNYS"),
    ("^VIX1Y", "CBOE S&P 500 1-Year Volatility", "US", "USD", "XNYS"),
    ("^RVX", "CBOE Russell 2000 Volatility", "US", "USD", "XNYS"),
    ("^VXD", "CBOE DJIA Volatility", "US", "USD", "XNYS"), ("^OVX", "CBOE Crude Oil Volatility", "US", "USD", "XNYS"),
    ("^GVZ", "CBOE Gold Volatility", "US", "USD", "XNYS"), ("^VXEEM", "CBOE Emerging Markets ETF Volatility", "US", "USD", "XNYS"),
    ("^SKEW", "CBOE S&P 500 Skew Index", "US", "USD", "XNYS"), ("^EVZ", "CBOE EuroCurrency Volatility", "US", "USD", "XNYS"),
    ("^MOVE", "ICE BofAML MOVE Index (Treasury vol)", "US", "USD", "XNYS"),
    ("^V2TX", "VSTOXX (EURO STOXX 50 Volatility)", "EU", "EUR", "XETR"),
    ("^VHSI", "HSI Volatility Index", "HK", "HKD", "XHKG"), ("^JNIV", "Nikkei Stock Average Volatility", "JP", "JPY", "XTKS"),
    ("^INDIAVIX", "India VIX", "APAC", "INR", "XBOM"), ("^AXVI", "S&P/ASX 200 VIX", "APAC", "AUD", "XASX"),
]

#: 2026-10-13 probe (data/state/price_history/probe_instruments.json): symbols Yahoo did not serve (kept out of
#: BENCHMARKS). Alternates tried without success: V2TX.DE, ^VSTOXX, ^STOXX50EVI, BGN=X, ^BCOMSP, ^DJUBS.
#: The second group is quote-only on Yahoo (period=max rejected: "must be one of 1d, 5d"; no daily history).
UNAVAILABLE: set[str] = {"USDBGN=X", "LBS=F", "UX=F", "^BCOM", "^BCOMTR", "^CRB", "^RJI", "JJA", "^RVX", "^EVZ",
                         "^V2TX", "^VHSI", "^JNIV",
                         "USDCNH=X", "B0=F", "MCL=F", "^DJCI", "^VIX1Y", "^VXEEM"}


def fx_name(pair: str) -> str:
    return f"{pair[:3]}/{pair[3:]} (Yahoo market rate)"


def history_instruments() -> list[tuple[str, str, str, str, str, str | None]]:
    """(yf_ticker, name, region, currency, kind, calendar) for every price-history instrument (minus UNAVAILABLE)."""
    out: list[tuple[str, str, str, str, str, str | None]] = []
    out += [(f"{p}=X", fx_name(p), "FX", p[3:], "fx", None) for p in FX_PAIRS]
    out += [(t, f"{n} (continuous front month)", "US", "USD", "future", None) for t, n in FUTURES_EXTRA]
    out += [(t, n, "US", "USD", "index_ext", "XNYS") for t, n in COMMODITY_INDICES]
    out += [(t, n, "US", "USD", "etf", "XNYS") for t, n in COMMODITY_ETFS]
    out += [(t, n, r, c, "index_ext", cal) for t, n, r, c, cal in VOL_INDICES]
    seen: set[str] = set()
    res = []
    for row in out:
        if row[0] in UNAVAILABLE or row[0] in seen:
            continue
        seen.add(row[0])
        res.append(row)
    return res
