"""Static instrument lists for price coverage P2 (docs/MODEL_STUDIO_PLAN.md): major global ETFs, extra indices and
continuous futures contracts, priced through yfinance as benchmarks (synthetic ids ETF:<t> / IDX:<t>; futures use the
IDX: prefix so every existing `NOT LIKE 'IDX:%' AND NOT LIKE 'ETF:%'` security filter keeps excluding them).

Tuples: (yf_ticker, name, region, currency, calendar). Appended to app.normalize.prices.BENCHMARKS with kinds
`etf`, `index_ext` (not shown in the market overview, which lists kind == 'index') and `future`.
"""

from __future__ import annotations

_US = ("US", "USD", "XNYS")

ETFS: list[tuple[str, str]] = [
    # broad / size / style
    ("IVV", "iShares Core S&P 500"), ("VOO", "Vanguard S&P 500"), ("VTI", "Vanguard Total Stock Market"),
    ("QQQ", "Invesco QQQ (Nasdaq-100)"), ("DIA", "SPDR Dow Jones Industrial Average"), ("IWM", "iShares Russell 2000"),
    ("IWB", "iShares Russell 1000"), ("IWD", "iShares Russell 1000 Value"), ("IWF", "iShares Russell 1000 Growth"),
    ("IWN", "iShares Russell 2000 Value"), ("IWO", "iShares Russell 2000 Growth"), ("MDY", "SPDR S&P MidCap 400"),
    ("IJH", "iShares Core S&P Mid-Cap"), ("IJR", "iShares Core S&P Small-Cap"), ("RSP", "Invesco S&P 500 Equal Weight"),
    ("VTV", "Vanguard Value"), ("VUG", "Vanguard Growth"), ("VB", "Vanguard Small-Cap"), ("VO", "Vanguard Mid-Cap"),
    ("VEA", "Vanguard FTSE Developed Markets"), ("VWO", "Vanguard FTSE Emerging Markets"), ("EFA", "iShares MSCI EAFE"),
    ("EEM", "iShares MSCI Emerging Markets"), ("IEMG", "iShares Core MSCI Emerging Markets"),
    ("ACWI", "iShares MSCI ACWI"), ("VT", "Vanguard Total World Stock"), ("VXUS", "Vanguard Total International Stock"),
    # industries / themes
    ("XBI", "SPDR S&P Biotech"), ("IBB", "iShares Biotechnology"), ("SMH", "VanEck Semiconductor"),
    ("SOXX", "iShares Semiconductor"), ("KRE", "SPDR S&P Regional Banking"), ("KBE", "SPDR S&P Bank"),
    ("XHB", "SPDR S&P Homebuilders"), ("ITB", "iShares US Home Construction"), ("XME", "SPDR S&P Metals & Mining"),
    ("XOP", "SPDR S&P Oil & Gas E&P"), ("OIH", "VanEck Oil Services"), ("GDX", "VanEck Gold Miners"),
    ("GDXJ", "VanEck Junior Gold Miners"), ("IYR", "iShares US Real Estate"), ("VNQ", "Vanguard Real Estate"),
    ("XRT", "SPDR S&P Retail"), ("IGV", "iShares Expanded Tech-Software"), ("KWEB", "KraneShares CSI China Internet"),
    ("ITA", "iShares US Aerospace & Defense"), ("JETS", "US Global Jets"), ("TAN", "Invesco Solar"),
    ("ICLN", "iShares Global Clean Energy"), ("LIT", "Global X Lithium & Battery Tech"), ("ARKK", "ARK Innovation"),
    # factors / dividends
    ("MTUM", "iShares MSCI USA Momentum Factor"), ("QUAL", "iShares MSCI USA Quality Factor"),
    ("USMV", "iShares MSCI USA Min Vol Factor"), ("VLUE", "iShares MSCI USA Value Factor"),
    ("SIZE", "iShares MSCI USA Size Factor"), ("SPLV", "Invesco S&P 500 Low Volatility"),
    ("SPHB", "Invesco S&P 500 High Beta"), ("DVY", "iShares Select Dividend"), ("VIG", "Vanguard Dividend Appreciation"),
    ("SCHD", "Schwab US Dividend Equity"), ("NOBL", "ProShares S&P 500 Dividend Aristocrats"),
    ("VYM", "Vanguard High Dividend Yield"), ("MOAT", "VanEck Morningstar Wide Moat"), ("COWZ", "Pacer US Cash Cows 100"),
    # countries / regions
    ("EWJ", "iShares MSCI Japan"), ("EWG", "iShares MSCI Germany"), ("EWU", "iShares MSCI United Kingdom"),
    ("EWQ", "iShares MSCI France"), ("EWI", "iShares MSCI Italy"), ("EWP", "iShares MSCI Spain"),
    ("EWL", "iShares MSCI Switzerland"), ("EWN", "iShares MSCI Netherlands"), ("EWD", "iShares MSCI Sweden"),
    ("EWC", "iShares MSCI Canada"), ("EWA", "iShares MSCI Australia"), ("EWH", "iShares MSCI Hong Kong"),
    ("EWS", "iShares MSCI Singapore"), ("EWT", "iShares MSCI Taiwan"), ("EWY", "iShares MSCI South Korea"),
    ("EWZ", "iShares MSCI Brazil"), ("EWW", "iShares MSCI Mexico"), ("MCHI", "iShares MSCI China"),
    ("FXI", "iShares China Large-Cap"), ("ASHR", "Xtrackers Harvest CSI 300 China A"), ("INDA", "iShares MSCI India"),
    ("EPI", "WisdomTree India Earnings"), ("EZA", "iShares MSCI South Africa"), ("TUR", "iShares MSCI Turkey"),
    ("EIDO", "iShares MSCI Indonesia"), ("THD", "iShares MSCI Thailand"), ("EWM", "iShares MSCI Malaysia"),
    ("EPHE", "iShares MSCI Philippines"), ("ARGT", "Global X MSCI Argentina"), ("ECH", "iShares MSCI Chile"),
    ("EPU", "iShares MSCI Peru"), ("KSA", "iShares MSCI Saudi Arabia"), ("QAT", "iShares MSCI Qatar"),
    ("UAE", "iShares MSCI UAE"), ("VGK", "Vanguard FTSE Europe"), ("EZU", "iShares MSCI Eurozone"),
    ("FEZ", "SPDR EURO STOXX 50"), ("AAXJ", "iShares MSCI All Country Asia ex Japan"),
    ("ILF", "iShares Latin America 40"), ("EWO", "iShares MSCI Austria"), ("EWK", "iShares MSCI Belgium"),
    ("ENOR", "iShares MSCI Norway"), ("EDEN", "iShares MSCI Denmark"), ("EFNL", "iShares MSCI Finland"),
    ("EIRL", "iShares MSCI Ireland"), ("PGAL", "Global X MSCI Portugal"), ("GREK", "Global X MSCI Greece"),
    ("EPOL", "iShares MSCI Poland"), ("VNM", "VanEck Vietnam"),
    # bonds
    ("AGG", "iShares Core US Aggregate Bond"), ("BND", "Vanguard Total Bond Market"),
    ("TLT", "iShares 20+ Year Treasury Bond"), ("IEF", "iShares 7-10 Year Treasury Bond"),
    ("SHY", "iShares 1-3 Year Treasury Bond"), ("IEI", "iShares 3-7 Year Treasury Bond"),
    ("GOVT", "iShares US Treasury Bond"), ("LQD", "iShares iBoxx IG Corporate Bond"),
    ("HYG", "iShares iBoxx High Yield Corporate Bond"), ("JNK", "SPDR Bloomberg High Yield Bond"),
    ("TIP", "iShares TIPS Bond"), ("VCIT", "Vanguard Intermediate-Term Corporate Bond"),
    ("VCSH", "Vanguard Short-Term Corporate Bond"), ("MUB", "iShares National Muni Bond"),
    ("EMB", "iShares JP Morgan USD Emerging Markets Bond"), ("BNDX", "Vanguard Total International Bond"),
    ("BWX", "SPDR Bloomberg International Treasury Bond"), ("MBB", "iShares MBS"),
    ("SHV", "iShares Short Treasury Bond"), ("BIL", "SPDR Bloomberg 1-3 Month T-Bill"),
    ("FLOT", "iShares Floating Rate Bond"),
    # commodities / currencies / volatility / crypto
    ("GLD", "SPDR Gold Shares"), ("IAU", "iShares Gold Trust"), ("SLV", "iShares Silver Trust"),
    ("USO", "United States Oil Fund"), ("BNO", "United States Brent Oil Fund"), ("UNG", "United States Natural Gas Fund"),
    ("DBC", "Invesco DB Commodity Index Tracking"), ("DBA", "Invesco DB Agriculture"),
    ("PDBC", "Invesco Optimum Yield Diversified Commodity"), ("CPER", "United States Copper Index"),
    ("PPLT", "abrdn Platinum ETF"), ("URA", "Global X Uranium"), ("WEAT", "Teucrium Wheat"), ("CORN", "Teucrium Corn"),
    ("UUP", "Invesco DB US Dollar Index Bullish"), ("FXE", "Invesco CurrencyShares Euro"),
    ("FXY", "Invesco CurrencyShares Japanese Yen"), ("FXB", "Invesco CurrencyShares British Pound"),
    ("VIXY", "ProShares VIX Short-Term Futures"), ("IBIT", "iShares Bitcoin Trust"), ("ETHA", "iShares Ethereum Trust"),
]

#: (yf_ticker, name, region, currency, calendar)
INDICES: list[tuple[str, str, str, str, str | None]] = [
    ("^DJI", "Dow Jones Industrial Average", "US", "USD", "XNYS"), ("^IXIC", "Nasdaq Composite", "US", "USD", "XNYS"),
    ("^NDX", "Nasdaq-100", "US", "USD", "XNYS"), ("^RUT", "Russell 2000", "US", "USD", "XNYS"),
    ("^VIX", "CBOE Volatility Index", "US", "USD", "XNYS"), ("^MID", "S&P MidCap 400", "US", "USD", "XNYS"),
    ("^SML", "S&P SmallCap 600", "US", "USD", "XNYS"), ("^TNX", "US Treasury 10Y yield (%)", "US", "USD", "XNYS"),
    ("^FVX", "US Treasury 5Y yield (%)", "US", "USD", "XNYS"), ("^TYX", "US Treasury 30Y yield (%)", "US", "USD", "XNYS"),
    ("^IRX", "US Treasury 13W yield (%)", "US", "USD", "XNYS"), ("DX-Y.NYB", "US Dollar Index", "US", "USD", None),
    ("^GSPTSE", "S&P/TSX Composite", "CA", "CAD", "XTSE"), ("^AXJO", "S&P/ASX 200", "APAC", "AUD", "XASX"),
    ("^BSESN", "BSE Sensex", "APAC", "INR", "XBOM"), ("^NSEI", "Nifty 50", "APAC", "INR", "XBOM"),
    ("^TWII", "TAIEX", "APAC", "TWD", "XTAI"), ("^STI", "Straits Times Index", "APAC", "SGD", "XSES"),
    ("^JKSE", "Jakarta Composite", "APAC", "IDR", "XIDX"), ("^KLSE", "FTSE Bursa Malaysia KLCI", "APAC", "MYR", "XKLS"),
    ("^SET.BK", "SET Index", "APAC", "THB", "XBKK"), ("^KQ11", "KOSDAQ Composite", "KR", "KRW", "XKRX"),
    ("^HSCE", "Hang Seng China Enterprises", "HK", "HKD", "XHKG"), ("000001.SS", "SSE Composite", "CN", "CNY", "XSHG"),
    ("399001.SZ", "SZSE Component", "CN", "CNY", "XSHG"), ("399006.SZ", "ChiNext Index", "CN", "CNY", "XSHG"),
    ("^AEX", "AEX", "EU", "EUR", "XAMS"), ("^IBEX", "IBEX 35", "EU", "EUR", "XMAD"), ("^SSMI", "SMI", "EU", "CHF", "XSWX"),
    ("FTSEMIB.MI", "FTSE MIB", "EU", "EUR", "XMIL"), ("^OMX", "OMX Stockholm 30", "EU", "SEK", "XSTO"),
    ("^BFX", "BEL 20", "EU", "EUR", "XBRU"), ("^ATX", "ATX", "EU", "EUR", "XWBO"), ("^STOXX", "STOXX Europe 600", "EU", "EUR", "XETR"),
    ("^MERV", "MERVAL", "LATAM", "ARS", "XBUE"), ("^IPSA", "S&P IPSA", "LATAM", "CLP", "XSGO"),
]

#: continuous front-month futures (Yahoo `=F`)
FUTURES: list[tuple[str, str]] = [
    ("CL=F", "WTI crude oil"), ("BZ=F", "Brent crude oil"), ("NG=F", "Henry Hub natural gas"),
    ("RB=F", "RBOB gasoline"), ("HO=F", "Heating oil"), ("GC=F", "Gold"), ("SI=F", "Silver"), ("HG=F", "Copper"),
    ("PL=F", "Platinum"), ("PA=F", "Palladium"), ("ZC=F", "Corn"), ("ZW=F", "Wheat"), ("ZS=F", "Soybeans"),
    ("KC=F", "Coffee"), ("SB=F", "Sugar #11"), ("CC=F", "Cocoa"), ("CT=F", "Cotton"), ("LE=F", "Live cattle"),
    ("HE=F", "Lean hogs"), ("ZN=F", "10-Year T-Note"), ("ZB=F", "30-Year T-Bond"), ("ZF=F", "5-Year T-Note"),
    ("ZT=F", "2-Year T-Note"), ("ES=F", "E-mini S&P 500"), ("NQ=F", "E-mini Nasdaq-100"), ("YM=F", "E-mini Dow"),
    ("RTY=F", "E-mini Russell 2000"), ("6E=F", "Euro FX"), ("6J=F", "Japanese yen"), ("6B=F", "British pound"),
    ("BTC=F", "Bitcoin (CME)"),
]


def extended_benchmarks() -> list[tuple[str, str, str, str, str, str | None]]:
    """(yf_ticker, name, region, currency, kind, calendar) for every P2 instrument."""
    out = [(t, n, _US[0], _US[1], "etf", _US[2]) for t, n in ETFS]
    out += [(t, n, r, c, "index_ext", cal) for t, n, r, c, cal in INDICES]
    out += [(t, f"{n} (continuous front month)", "US", "USD", "future", None) for t, n in FUTURES]
    return out
