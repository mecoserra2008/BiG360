"""CFTC contract markets -> our futures instruments and asset classes (curated table + name matching).

CURATED: CFTC contract market code -> headline market. `sid` = futures instrument in securities_master (IDX:<ROOT>=F);
`proxy` = price series used when we have no futures series for that market (labelled proxy in the UI / event study);
`links` = further instruments whose page links to this market (micro / mini contracts on the same underlying).
Codes verified against the CFTC Public Reporting Environment on 2026-10-19 (codes are stable across renames, e.g.
067651 "CRUDE OIL, LIGHT SWEET" -> "WTI-PHYSICAL").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Curated:
    root: str | None
    asset_class: str
    short_name: str
    sid: str | None = None
    proxy: str | None = None
    links: tuple[str, ...] = field(default_factory=tuple)


def _f(root: str) -> str:
    return f"IDX:{root}=F"


CURATED: dict[str, Curated] = {
    # energy
    "067651": Curated("CL", "energy", "WTI crude (NYMEX)", _f("CL"), links=(_f("QM"), _f("MCL"))),
    "06765T": Curated("BZ", "energy", "Brent last day (NYMEX)", _f("BZ")),
    "023651": Curated("NG", "energy", "Henry Hub natural gas (NYMEX)", _f("NG")),
    "111659": Curated("RB", "energy", "RBOB gasoline", _f("RB")),
    "022651": Curated("HO", "energy", "NY Harbor ULSD (heating oil)", _f("HO")),
    "067411": Curated(None, "energy", "WTI crude (ICE Europe)"),
    "025651": Curated(None, "energy", "Ethanol (NYMEX)"),
    # metals
    "088691": Curated("GC", "metals", "Gold (COMEX)", _f("GC")),
    "088695": Curated("MGC", "metals", "Micro gold", _f("MGC")),
    "084691": Curated("SI", "metals", "Silver (COMEX)", _f("SI")),
    "084694": Curated("SIL", "metals", "Micro silver", _f("SIL")),
    "085692": Curated("HG", "metals", "Copper (COMEX)", _f("HG")),
    "076651": Curated("PL", "metals", "Platinum", _f("PL")),
    "075651": Curated("PA", "metals", "Palladium", _f("PA")),
    "191691": Curated("ALI", "metals", "Aluminum (COMEX)", _f("ALI")),
    "192651": Curated("HRC", "metals", "US Midwest HRC steel", _f("HRC")),
    # grains & oilseeds
    "002602": Curated("ZC", "ags", "Corn", _f("ZC")),
    "001602": Curated("ZW", "ags", "Wheat SRW (Chicago)", _f("ZW")),
    "001612": Curated("KE", "ags", "Wheat HRW (Kansas City)", _f("KE")),
    "005602": Curated("ZS", "ags", "Soybeans", _f("ZS")),
    "007601": Curated("ZL", "ags", "Soybean oil", _f("ZL")),
    "026603": Curated("ZM", "ags", "Soybean meal", _f("ZM")),
    "004603": Curated("ZO", "ags", "Oats", _f("ZO")),
    "039601": Curated("ZR", "ags", "Rough rice", _f("ZR")),
    "135731": Curated(None, "ags", "Canola (ICE)"),
    "001626": Curated(None, "ags", "Wheat HRS (Minneapolis)"),
    # softs
    "083731": Curated("KC", "softs", "Coffee C", _f("KC")),
    "080732": Curated("SB", "softs", "Sugar No. 11", _f("SB")),
    "073732": Curated("CC", "softs", "Cocoa (ICE US)", _f("CC")),
    "033661": Curated("CT", "softs", "Cotton No. 2", _f("CT")),
    "040701": Curated("OJ", "softs", "Orange juice (FCOJ)", _f("OJ")),
    "058644": Curated("LBR", "softs", "Lumber", _f("LBR")),
    # livestock & dairy
    "057642": Curated("LE", "livestock", "Live cattle", _f("LE")),
    "054642": Curated("HE", "livestock", "Lean hogs", _f("HE")),
    "061641": Curated("GF", "livestock", "Feeder cattle", _f("GF")),
    "052641": Curated("DC", "livestock", "Class III milk", _f("DC")),
    # rates
    "020601": Curated("ZB", "rates", "UST bond", _f("ZB")),
    "020604": Curated(None, "rates", "Ultra UST bond"),
    "043602": Curated("ZN", "rates", "UST 10Y note", _f("ZN")),
    "043607": Curated(None, "rates", "Ultra UST 10Y"),
    "044601": Curated("ZF", "rates", "UST 5Y note", _f("ZF")),
    "042601": Curated("ZT", "rates", "UST 2Y note", _f("ZT")),
    "045601": Curated(None, "rates", "30-day Fed funds"),
    "134741": Curated(None, "rates", "SOFR 3M"),
    "047745": Curated(None, "rates", "Euro short-term rate (CME)"),
    # equity indices & volatility
    "13874A": Curated("ES", "equity", "E-mini S&P 500", _f("ES")),
    "209742": Curated("NQ", "equity", "E-mini Nasdaq-100", _f("NQ")),
    "124603": Curated("YM", "equity", "E-mini Dow ($5)", _f("YM")),
    "239742": Curated("RTY", "equity", "E-mini Russell 2000", _f("RTY")),
    "240743": Curated(None, "equity", "Nikkei 225 (yen, CME)", proxy="IDX:^N225"),
    "244042": Curated(None, "equity", "MSCI EM (ICE)", proxy="ETF:EEM"),
    "244041": Curated(None, "equity", "MSCI EAFE (ICE)", proxy="ETF:EFA"),
    "1170E1": Curated(None, "equity", "VIX futures (CFE)", proxy="IDX:^VIX"),
    # currencies (CME, quoted USD per unit except where noted); links = the spot pair's instrument page (guide 02b)
    "099741": Curated("6E", "fx", "Euro FX", _f("6E"), links=("IDX:EURUSD=X",)),
    "096742": Curated("6B", "fx", "British pound", _f("6B"), links=("IDX:GBPUSD=X",)),
    "097741": Curated("6J", "fx", "Japanese yen", _f("6J"), links=("IDX:USDJPY=X",)),
    "232741": Curated(None, "fx", "Australian dollar", proxy="IDX:AUDUSD=X"),
    "090741": Curated(None, "fx", "Canadian dollar", links=("IDX:USDCAD=X",)),
    "092741": Curated(None, "fx", "Swiss franc", links=("IDX:USDCHF=X",)),
    "112741": Curated(None, "fx", "New Zealand dollar", proxy="IDX:NZDUSD=X"),
    "095741": Curated(None, "fx", "Mexican peso", links=("IDX:USDMXN=X",)),
    "102741": Curated(None, "fx", "Brazilian real", links=("IDX:USDBRL=X",)),
    "122741": Curated(None, "fx", "South African rand", links=("IDX:USDZAR=X",)),
    "098662": Curated(None, "fx", "US dollar index (ICE)", proxy="IDX:DX-Y.NYB"),
    # crypto
    "133741": Curated("BTC", "crypto", "Bitcoin (CME)", _f("BTC"), links=("ETF:IBIT",)),
    "133742": Curated(None, "crypto", "Micro bitcoin (CME)", proxy=_f("BTC")),
    "146021": Curated("ETH", "crypto", "Ether (CME)", _f("ETH"), links=("ETF:ETHA",)),
    "146022": Curated(None, "crypto", "Micro ether (CME)", proxy=_f("ETH")),
}

# CFTC commodity_subgroup_name -> asset class (upper-cased lookup)
SUBGROUP_CLASS: dict[str, str] = {
    "PETROLEUM AND PRODUCTS": "energy", "NATURAL GAS AND PRODUCTS": "energy", "ELECTRICITY AND SOURCES": "energy",
    "COAL": "energy", "PRECIOUS METALS": "metals", "BASE METALS": "metals", "GRAINS": "ags",
    "OILSEED AND PRODUCTS": "ags", "FOODSTUFFS/SOFTS": "softs", "FIBER": "softs", "WOOD PRODUCTS": "softs",
    "LIVESTOCK/MEAT PRODUCTS": "livestock", "DAIRY PRODUCTS": "livestock", "CURRENCY": "fx",
    "CURRENCY(NON-MAJOR)": "fx", "DIGITAL ASSET": "crypto", "DIGITAL ASSET (NON-MAJOR)": "crypto",
    "INTEREST RATES - U.S. TREASURY": "rates", "INTEREST RATES - NON U.S. TREASURY": "rates",
    "INTEREST RATE SWAPS": "rates", "STOCK INDICES": "equity", "EMISSIONS": "other", "CHEMICALS": "other",
    "FERTILIZER": "other", "OTHER FINANCIAL INSTRUMENTS": "other",
}

# name keyword fallbacks (old rows without a subgroup); first match wins
_NAME_RULES: list[tuple[str, str]] = [
    (r"BITCOIN|ETHER\b|ETHEREUM|SOLANA|\bXRP\b|CRYPTO|LITECOIN|DOGECOIN|\bPERP\b", "crypto"),
    (r"S&P|NASDAQ|DJIA|DOW JONES|RUSSELL|NIKKEI|MSCI|STOCK INDEX|VIX|\bINDEX\b.*STOCK", "equity"),
    (r"TREASURY|T-BOND|T-NOTE|\bUST\b|EURODOLLAR|SOFR|FED FUNDS|LIBOR|INTEREST RATE|SWAP RATE|\bNOTE\b|\bBOND\b", "rates"),
    (r"DOLLAR|EURO FX|YEN|POUND|FRANC|PESO|REAL\b|RAND|RUBLE|KRONA|RUPEE|RENMINBI|YUAN|ZLOTY|FORINT|SHEKEL|WON\b", "fx"),
    (r"CRUDE|WTI|BRENT|GASOLINE|HEATING OIL|ULSD|DIESEL|NAT(URAL)? GAS|HENRY HUB|PROPANE|ETHANE|BUTANE|NAPHTHA|"
     r"JET|FUEL OIL|ELECTRIC|POWER|COAL|ETHANOL|GASOIL|CONDENSATE", "energy"),
    (r"GOLD|SILVER|PLATINUM|PALLADIUM|COPPER|ALUMIN|ZINC|NICKEL|STEEL|IRON ORE|COBALT|LITHIUM|LEAD\b", "metals"),
    (r"CATTLE|HOGS|PORK|MILK|BUTTER|CHEESE|WHEY|DAIRY|BROILER", "livestock"),
    (r"COFFEE|SUGAR|COCOA|COTTON|ORANGE JUICE|LUMBER|RUBBER", "softs"),
    (r"CORN|WHEAT|SOYBEAN|OATS|RICE|CANOLA|PALM OIL|SORGHUM", "ags"),
]


def asset_class_for(code: str, subgroup: str | None, name: str | None, group: str | None = None) -> str:
    """Asset class: curated, then CFTC subgroup, then name keywords, else other."""
    c = CURATED.get(code)
    if c:
        return c.asset_class
    if subgroup:
        hit = SUBGROUP_CLASS.get(subgroup.strip().upper())
        if hit:
            return hit
    nm = (name or "").upper()
    for pat, cls in _NAME_RULES:
        if re.search(pat, nm):
            return cls
    return "other"


_STRIP = re.compile(r"\(.*?\)|\bCONTINUOUS FRONT MONTH\b|\bFUTURES?\b|[^A-Z0-9 ]")


def norm_name(s: str | None) -> str:
    """Normalized instrument / market name for matching ('Gold (continuous front month)' -> 'GOLD')."""
    t = _STRIP.sub(" ", (s or "").upper())
    return " ".join(t.split())


def name_match(market_name: str, futures: dict[str, str]) -> str | None:
    """security_id of the futures instrument whose normalized name equals the normalized CFTC market name
    (futures: security_id -> name). Exact equality only - ambiguous fuzzy matches are worse than none."""
    key = norm_name(market_name)
    if not key:
        return None
    hits = [sid for sid, nm in futures.items() if norm_name(nm) == key]
    return hits[0] if len(hits) == 1 else None


def primary_report_for(asset_class: str, report_types: list[str]) -> str:
    """Headline report family: TFF for financials, Disaggregated for physical commodities, else Legacy."""
    fams = {rt.split("_")[0] for rt in report_types}
    if asset_class in ("rates", "equity", "fx", "crypto") and "tff" in fams:
        return "tff"
    if "disagg" in fams and asset_class not in ("rates", "equity", "fx", "crypto"):
        return "disagg"
    return "legacy"


def title_name(s: str) -> str:
    """'WHEAT-SRW' -> 'Wheat-Srw' style fallback display label (keeps short tokens upper)."""
    out = []
    for w in s.split():
        out.append(w if (len(w) <= 3 and w.isalpha() and w not in ("AND", "THE")) or any(ch.isdigit() for ch in w) else w.capitalize())
    return " ".join(out)
