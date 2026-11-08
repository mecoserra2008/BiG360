"""E1 label taxonomy: market themes (bitmask), GKG theme -> market theme / sector maps, CAMEO -> theme, countries
(ISO2 dictionary with GLOBAL = 0, FIPS 10-4 -> ISO2), sectors (bit per taxonomy sector) and the GLOBAL rule.

Everything here is static and versioned (TAXONOMY_VERSION); the persisted dictionary table `event_labels`
(app.events_v2.dicts) is generated from it, so stored ints stay decodable. NEVER renumber a theme bit or a sector bit:
append new themes at the end (bits 0..62 are available; bit 63 is never used so masks stay positive int64).

Theme sources, in order of precision:
  * GKG V1Themes (exact theme or prefix match, e.g. NATURAL_DISASTER_* ),
  * English headline keyword rules (corporate themes GKG does not code well: earnings, M&A, recalls, ...),
  * CAMEO event codes for Events 2.0 rows (protest 14x, sanctions 163, coerce 17x, conflict 18-20, ...).
Very broad GKG themes (TAX_FNCACT, WB_2670_JOBS, ECON_WORLDCURRENCIES, TAX_ECON_PRICE, MANMADE_DISASTER_IMPLIED,
EPU_POLICY, ...) are deliberately NOT mapped: they appear in most articles and would make every row "labelled".

GLOBAL rule (`countries_with_global`):
  * an article/event with no resolvable country (no V1Locations / ActionGeo / actor country / linked-issuer country)
    is labelled GLOBAL;
  * an article that mentions >= GLOBAL_MIN_COUNTRIES distinct countries is ALSO labelled GLOBAL (multi-country /
    world-market stories), next to its top-3 countries;
  * a few inherently global themes (commodities, crypto, pandemic) add GLOBAL when no country gets >= 50% of the
    location mentions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import polars as pl

from app.ingestion.gdelt_events import FIPS_TO_ISO2, ISO3_TO_ISO2

TAXONOMY_VERSION = 1
GLOBAL = "GLOBAL"
GLOBAL_ID = 0
GLOBAL_MIN_COUNTRIES = 5
MAX_COUNTRIES = 3

# ------------------------------------------------------------------------------------------------ sectors (bit = index)
SECTORS: list[tuple[str, str]] = [
    ("TECH", "Technology"), ("COMM", "Communication & Media"), ("CDIS", "Consumer Discretionary"),
    ("CSTA", "Consumer Staples"), ("HLTH", "Healthcare"), ("FINS", "Financials"), ("INDU", "Industrials"),
    ("MATR", "Materials"), ("ENRG", "Energy"), ("UTIL", "Utilities"), ("REAL", "Real Estate"),
]
SECTOR_BIT: dict[str, int] = {s: i for i, (s, _) in enumerate(SECTORS)}


def sector_mask(sector_ids: list[str] | tuple[str, ...] | set[str] | None) -> int:
    m = 0
    for s in sector_ids or ():
        b = SECTOR_BIT.get(s)
        if b is not None:
            m |= 1 << b
    return m


def sectors_of_mask(mask: int | None) -> list[str]:
    return [s for i, (s, _) in enumerate(SECTORS) if mask and (int(mask) >> i) & 1]


# ------------------------------------------------------------------------------------------------ market themes
@dataclass(frozen=True)
class Theme:
    bit: int
    id: str
    name: str
    group: str
    gkg: tuple[str, ...] = ()           # exact GKG theme names
    gkg_prefix: tuple[str, ...] = ()    # GKG theme prefixes (THEME or THEME_*)
    cameo: tuple[str, ...] = ()         # CAMEO EventCode prefixes ("14" = all protest codes, "163" = sanctions)
    title: str | None = None            # English headline regex (case-insensitive, word-bounded)
    sectors: tuple[str, ...] = ()       # sectors typically affected (UI hint / theme -> sector map)
    global_theme: bool = False          # inherently global (see GLOBAL rule)
    extra: dict = field(default_factory=dict)


THEMES: list[Theme] = [
    Theme(0, "inflation", "Inflation & prices", "macro",
          gkg=("ECON_INFLATION", "ECON_COST_OF_LIVING", "ECON_PRICECONTROL"),
          title=r"inflation|consumer prices|cpi|cost of living|price hikes?", sectors=("CSTA", "CDIS")),
    Theme(1, "monetary_policy", "Rates & central banks", "macro",
          gkg=("ECON_CENTRALBANK", "ECON_INTEREST_RATES", "EPU_POLICY_CENTRAL_BANK", "WB_1235_CENTRAL_BANKS",
               "EPU_CATS_MONETARY_POLICY", "EPU_POLICY_INTEREST_RATES", "EPU_POLICY_FEDERAL_RESERVE"),
          title=r"rate (cut|hike)s?|interest rates?|central bank|federal reserve|the fed|ecb|bank of (england|japan|canada)|"
                r"monetary policy|pboc|rbi|basis points", sectors=("FINS", "REAL")),
    Theme(2, "fiscal_tax", "Fiscal policy & taxes", "macro",
          gkg=("EPU_CATS_FISCAL_POLICY", "ECON_TAXATION", "EPU_CATS_TAXES", "ECON_SUBSIDIES"),
          title=r"budget deficit|fiscal|tax (hike|cut|reform)s?|stimulus package|government shutdown"),
    Theme(3, "sovereign_debt", "Sovereign debt & IMF", "macro",
          gkg=("ECON_DEBT", "EPU_POLICY_DEBT_CEILING", "ECON_DEVELOPMENTORGS_INTERNATIONAL_MONETARY_FUND",
               "ECON_DEVELOPMENTORGS_IMF"),
          title=r"bond yields?|sovereign debt|debt ceiling|credit rating|downgrades? .{0,20}rating|imf (bailout|loan|program)",
          sectors=("FINS",)),
    Theme(4, "fx_currency", "Currencies & FX", "macro",
          gkg=("ECON_CURRENCY_EXCHANGE_RATE", "ECON_DEVALUATION", "ECON_CURRENCY_RESERVES"),
          title=r"(dollar|euro|yen|yuan|rupee|peso|lira|pound|won|ruble|real) (falls?|rises?|slumps?|surges?|weakens?|strengthens?)|"
                r"exchange rate|devaluation|forex"),
    Theme(5, "growth_outlook", "Growth, recession & outlook", "macro",
          gkg=("ECON_CUTOUTLOOK", "EPU_UNCERTAINTY", "ECON_RECESSION"),
          title=r"recession|gdp|economic (growth|slowdown|outlook)|pmi|economy (shrinks|grows|contracts|expands)"),
    Theme(6, "labour_market", "Jobs & labour market", "macro",
          gkg=("UNEMPLOYMENT", "ECON_UNEMPLOYMENT"),
          title=r"unemployment|jobless|payrolls|layoffs?|job cuts?|hiring freeze", sectors=("INDU",)),
    Theme(7, "strikes", "Strikes & labour action", "social",
          gkg=("STRIKE", "ECON_UNIONS"), cameo=("143",),
          title=r"(?:labou?r|workers'?|general|national|rail|port|dock|teachers'?|nurses'?|doctors'?|union) strikes?|"
                r"strike action|walkouts?|industrial action|go(?:es)? on strike", sectors=("INDU",)),
    Theme(8, "trade_tariffs", "Trade & tariffs", "geo",
          gkg=("ECON_TRADE_DISPUTE", "EPU_CATS_TRADE_POLICY", "ECON_FREETRADE", "ECON_BOYCOTT", "TRADE_DISPUTE"),
          cameo=("1056", "0856"),
          title=r"tariffs?|trade (war|deal|talks|dispute|deficit|surplus)|export (ban|controls?)|import duties|anti-?dumping",
          sectors=("INDU", "MATR", "CDIS", "TECH")),
    Theme(9, "sanctions", "Sanctions & embargoes", "geo",
          gkg=("SANCTIONS", "EMBARGO"), cameo=("163", "1621", "1622", "1623"),
          title=r"sanctions?|embargo|blacklist(ed)?|entity list", sectors=("ENRG", "MATR", "FINS"), global_theme=False),
    Theme(10, "m_and_a", "M&A & deals", "corporate",
          gkg=("ECON_MERGERS", "ECON_ACQUISITION", "ECON_TAKEOVER"),
          title=r"merger|acquisitions?|acquires?|takeover|buyout|deal to (?:buy|acquire)|"
                r"(?:buys?|takes?|acquires?|sells?) (?:a )?stake in|divest(?:s|ment|iture)?|spin-?off"),
    Theme(11, "earnings", "Earnings & guidance", "corporate",
          gkg=("ECON_EARNINGSREPORT",),
          title=r"earnings|quarterly (?:results|profit|loss|revenue|sales)|q[1-4] (?:results|profit|revenue|sales|earnings)|"
                r"net (?:profit|income)|profit warning|beats? (?:estimates|expectations)|miss(?:es)? estimates|"
                r"full-year (?:results|profit|guidance)|(?:raises?|cuts?|lowers?) (?:its )?guidance"),
    Theme(12, "capital_markets", "Stock market, IPOs & capital raising", "markets",
          gkg=("ECON_STOCKMARKET", "ECON_IPO"),
          title=r"ipo|initial public offering|stocks? (fall|rise|drop|surge|rally|plunge|slump|tumble)s?|shares (fall|rise|drop|surge|jump|plunge|slump|tumble)|"
                r"share (buyback|offering|sale)|rights issue|bond (sale|issue)|nasdaq|dow jones|s&p 500|nikkei|hang seng|ftse|dax|sensex",
          sectors=("FINS",)),
    Theme(13, "bankruptcy_default", "Bankruptcy & default", "corporate",
          gkg=("ECON_BANKRUPTCY",),
          title=r"bankrupt(?:cy)?|insolven(?:t|cy)|chapter 11|enters? administration|liquidation|default(?:s|ed)? on|"
                r"debt restructuring|creditors",
          sectors=("FINS",)),
    Theme(14, "regulation_antitrust", "Regulation & antitrust", "policy",
          gkg=("EPU_CATS_REGULATION", "EPU_CATS_FINANCIAL_REGULATION", "ECON_MONOPOLY", "ECON_DEREGULATION"),
          title=r"antitrust|competition (?:authority|regulator|watchdog)|regulators?|watchdog|fined|probe|investigation into|ban on",
          sectors=("FINS", "TECH", "COMM", "HLTH")),
    Theme(15, "litigation", "Litigation & legal", "corporate",
          gkg=("ECON_LAWSUIT",),
          title=r"lawsuits?|sues|sued|class action|settles? (?:lawsuit|case|charges|claims)|court (?:rules|ruling|orders)|"
                r"patent (?:infringement|dispute|lawsuit)"),
    Theme(16, "fraud_corruption", "Fraud, corruption & scandals", "corporate",
          gkg=("CORRUPTION", "ECON_FRAUD", "FRAUD"),
          title=r"fraud|embezzl\w*|brib\w*|scandal|accounting (irregularities|scandal)|money laundering|insider trading",
          sectors=("FINS",)),
    Theme(17, "recall_safety", "Product recalls & safety", "corporate",
          gkg=("PRODUCT_RECALL", "FOOD_SAFETY"),
          title=r"(?:recalls?|recalled) (?:\d[\d,]* )?(?:vehicles?|cars?|trucks?|products?|units|batches|lots|devices)|"
                r"product recalls?|safety recall|safety (?:probe|warning)|defective|contaminat\w*",
          sectors=("CDIS", "HLTH", "CSTA")),
    Theme(18, "cyber", "Cyber attacks & outages", "tech",
          gkg=("CYBER_ATTACK", "INFO_HACKING", "CYBERSECURITY"),
          title=r"cyber ?attacks?|hack(ed|ers?)|ransomware|data breach|outage|it failure", sectors=("TECH",)),
    Theme(19, "tech_ai", "Technology, AI & chips", "tech",
          gkg=("SOC_EMERGINGTECH", "TECH_AUTOMATION", "SOC_INNOVATION"),
          title=r"artificial intelligence|ai|chips? (?:maker|makers|exports?|ban|shortage)|semiconductors?|chipmakers?|"
                r"data cent(?:er|re)s?|5g|quantum computing|cloud computing",
          sectors=("TECH", "COMM")),
    Theme(20, "natural_disaster", "Natural disasters & extreme weather", "risk",
          gkg_prefix=("NATURAL_DISASTER",), cameo=(),
          title=r"earthquake|hurricane|typhoon|cyclone|flood(s|ing)?|wildfires?|tsunami|drought|heatwave|volcan\w*",
          sectors=("FINS", "UTIL", "INDU")),
    Theme(21, "climate_energy_transition", "Climate & energy transition", "risk",
          gkg=("ENV_CLIMATECHANGE", "ENV_CARBONCAPTURE", "ENV_GREEN", "ENV_EMISSIONS", "UNGP_CLIMATE_CHANGE_ACTION"),
          title=r"climate|emissions|carbon|net zero|renewables?|electric vehicles?|\bevs?\b", sectors=("ENRG", "UTIL")),
    Theme(22, "pandemic_epidemic", "Pandemics & epidemics", "risk",
          gkg=("HEALTH_PANDEMIC", "TAX_DISEASE_COVID", "TAX_DISEASE_COVID-19", "TAX_DISEASE_CORONAVIRUS", "HEALTH_SEXTRANSDISEASE_EPIDEMIC",
               "TAX_DISEASE_EBOLA", "TAX_DISEASE_BIRD_FLU", "TAX_DISEASE_MPOX", "TAX_DISEASE_MONKEYPOX", "TAX_DISEASE_CHOLERA"),
          title=r"pandemic|epidemic|outbreak|covid|coronavirus|lockdowns?|bird flu|mpox", sectors=("HLTH", "CDIS"),
          global_theme=True),
    Theme(23, "healthcare_pharma", "Healthcare & pharma", "sector",
          gkg=("EPU_CATS_HEALTHCARE", "HEALTH_VACCINATION"),
          title=r"fda|ema|drug (approval|prices?)|clinical trials?|vaccines?|pharma\w*|biotech|medicare|obesity drugs?",
          sectors=("HLTH",)),
    Theme(24, "conflict_military", "Armed conflict & military", "geo",
          gkg=("ARMEDCONFLICT", "WB_2433_CONFLICT_AND_VIOLENCE", "REBELLION", "REBELS", "TAX_MILITARY_TITLE_SOLDIER",
               "MILITARY_COOPERATION", "BLOCKADE"),
          cameo=("15", "18", "19", "20"),
          title=r"war|missiles?|air ?strikes?|troops|invasion|ceasefire|shelling|drone attacks?|military",
          sectors=("ENRG", "INDU", "MATR")),
    Theme(25, "terrorism", "Terrorism", "geo",
          gkg=("TERROR", "SUICIDE_ATTACK", "TAX_TERROR_GROUP"), cameo=("1831", "1832", "1833", "1834", "184"),
          title=r"terror\w*|suicide bomb\w*|hostages?", sectors=("CDIS",)),
    Theme(26, "geopolitical_tension", "Geopolitical tension & diplomacy", "geo",
          gkg=("EPU_CATS_NATIONAL_SECURITY", "WMD"),
          cameo=("12", "13", "16", "17"),
          title=r"tensions?|retaliat\w*|diplomat\w*|nuclear (?:test|weapons?|talks|deal)|expels?|ultimatum", sectors=("ENRG",)),
    Theme(27, "elections_politics", "Elections & politics", "politics",
          gkg=("ELECTION", "ELECTION_FRAUD"),
          title=r"elections?|opinion polls?|exit polls?|ballots?|referendum|coalition government|impeach\w*|snap poll"),
    Theme(28, "protest_unrest", "Protests & civil unrest", "social",
          gkg=("PROTEST", "UNREST_CURFEW", "RIOT", "UNREST_BELLIGERENT", "UNREST_POLICEBRUTALITY"), cameo=("14",),
          title=r"protests?|protesters|riots?|demonstrat\w*|unrest|curfew"),
    Theme(29, "coup_regime", "Coups & regime change", "politics",
          gkg=("COUP", "REGIME_CHANGE", "TAX_FNCACT_JUNTA"), cameo=("1041", "1724"),
          title=r"coup|junta|martial law|ousted|toppled"),
    Theme(30, "oil_gas", "Oil, gas & fuel prices", "commodities",
          gkg=("ENV_OIL", "ECON_OILPRICE", "ENV_NATURALGAS", "ECON_NATGASPRICE", "FUELPRICES", "ECON_GASOLINEPRICE",
               "ECON_DIESELPRICE", "ECON_HEATINGOIL", "ENV_FRACKING", "ENV_COAL", "OPEC"),
          title=r"crude|brent|wti|opec\+?|natural gas|lng|gasoline|petrol prices|diesel|refiner(?:y|ies)|oil and gas|"
                r"oil (?:price|prices|output|supply|production|exports?|imports?|demand|majors?|company|companies|fields?|tankers?|spill)|"
                r"coal (?:mines?|prices|plants?)|gas (?:prices|supply|pipeline)",
          sectors=("ENRG",), global_theme=True),
    Theme(31, "power_utilities", "Power & utilities", "commodities",
          gkg=("POWER_OUTAGE", "ENV_NUCLEARPOWER", "ENV_SOLAR", "ENV_WINDPOWER", "ENV_HYDRO", "ECON_ELECTRICALPRICE",
               "ENV_ELECTRICITY"),
          title=r"power (outage|cuts?|grid|prices?)|blackouts?|electricity|nuclear (plant|reactor)|solar|wind (farm|power)|utilit(y|ies)",
          sectors=("UTIL",)),
    Theme(32, "metals_mining", "Metals & mining", "commodities",
          gkg=("ENV_MINING", "ENV_METALS", "ECON_GOLD", "ENV_GOLD", "ENV_COPPER", "ENV_LITHIUM", "ENV_RARE_EARTHS"),
          title=r"gold (?:price|prices|futures)|copper|lithium|iron ore|nickel|steel|alumin(?:i)?um|rare earths?|mining|miners|cobalt",
          sectors=("MATR",), global_theme=True),
    Theme(33, "agriculture_food", "Agriculture & food", "commodities",
          gkg=("AGRICULTURE", "FOOD_SECURITY", "FOOD_STAPLE", "TAX_FOODSTAPLES", "ENV_FISHERY"),
          title=r"(?:wheat|corn|soybeans?|grain|cocoa|coffee|sugar|rice|palm oil|cattle|beef|pork) "
                r"(?:prices?|exports?|imports?|futures|harvest|output|crops?|production)|fertili[sz]ers?|food prices|crop failure|"
                r"harvests?|farmers",
          sectors=("CSTA", "MATR"), global_theme=True),
    Theme(34, "supply_chain_shipping", "Supply chain & shipping", "risk",
          gkg=("SHORTAGE", "MARITIME_INCIDENT", "ECON_TRANSPORT_COST", "PIRACY"),
          title=r"supply chains?|shortages?|shipping|freight|container ships?|port (?:congestion|strike|closures?)|red sea|suez|"
                r"panama canal|logistics",
          sectors=("INDU", "CDIS")),
    Theme(35, "housing_real_estate", "Housing & real estate", "sector",
          gkg=("ECON_HOUSING_PRICES", "ECON_MORTGAGE", "HOUSING_PRICES"),
          title=r"home prices|house prices|housing (market|starts)|mortgages?|property (market|developer|prices)|real estate",
          sectors=("REAL", "FINS")),
    Theme(36, "banking_financial_stress", "Banking & financial stress", "markets",
          gkg=("ECON_BANKRUN", "ECON_BAILOUT", "WB_1937_FINANCIAL_STABILITY"),
          title=r"bank run|bailouts?|deposit (?:flight|outflows)|liquidity (?:crisis|crunch)|credit crunch|shadow bank\w*|"
                r"capital requirements|bank (?:failure|collapse|stress tests?)|banking (?:crisis|sector|stocks)|"
                r"banks? (?:shares|stocks|profits?)",
          sectors=("FINS",)),
    Theme(37, "crypto", "Crypto & digital assets", "markets",
          gkg=("ECON_CRYPTOCURRENCY", "ECON_BITCOIN", "TAX_CRYPTOCURRENCY", "ECON_BLOCKCHAIN"),
          title=r"bitcoin|crypto\w*|blockchain|stablecoins?|ethereum|digital assets?", sectors=("FINS", "TECH"),
          global_theme=True),
    Theme(38, "nationalization_state", "Nationalization & state intervention", "policy",
          gkg=("ECON_NATIONALIZE", "ECON_PRIVATIZATION"),
          title=r"nationali[sz]\w*|expropriat\w*|state takeover|privati[sz]\w*|windfall tax", sectors=("ENRG", "MATR", "UTIL")),
    Theme(39, "foreign_investment", "Foreign investment & industrial policy", "policy",
          gkg=("ECON_FOREIGNINVEST", "ECON_FREETRADEZONE"),
          title=r"foreign (direct )?investment|fdi|investment (pledge|plan)|new (plant|factory)|gigafactory|subsid(y|ies)|chips act",
          sectors=("INDU",)),
    Theme(40, "aviation_transport", "Aviation & transport disruption", "sector",
          gkg=("AVIATION_INCIDENT", "TRANSPORT_DISRUPTION", "AIR_CRASH", "PLANE_CRASH"),
          title=r"airlines?|flights? (cancel\w*|delay\w*)|plane crash|air traffic|rail strike|derail\w*|boeing|airbus",
          sectors=("INDU", "CDIS")),
]
assert [t.bit for t in THEMES] == list(range(len(THEMES))), "theme bits must be dense 0..n-1"
assert len(THEMES) <= 63
THEME_BY_ID: dict[str, Theme] = {t.id: t for t in THEMES}
THEME_BIT: dict[str, int] = {t.id: t.bit for t in THEMES}
GLOBAL_THEME_MASK = sum(1 << t.bit for t in THEMES if t.global_theme)

# GKG theme -> sector (fine-grained; "affected sector" hints). Matching: exact theme or prefix (ending with '*').
GKG_SECTOR: dict[str, tuple[str, ...]] = {
    "ENV_OIL": ("ENRG",), "ECON_OILPRICE": ("ENRG",), "ENV_NATURALGAS": ("ENRG",), "ECON_NATGASPRICE": ("ENRG",),
    "FUELPRICES": ("ENRG",), "ECON_GASOLINEPRICE": ("ENRG",), "ECON_DIESELPRICE": ("ENRG",), "ENV_FRACKING": ("ENRG",),
    "ENV_COAL": ("ENRG", "MATR"), "OPEC": ("ENRG",),
    "ENV_MINING": ("MATR",), "ENV_METALS": ("MATR",), "ENV_GOLD": ("MATR",), "ENV_COPPER": ("MATR",),
    "ENV_LITHIUM": ("MATR",), "ENV_RARE_EARTHS": ("MATR",),
    "POWER_OUTAGE": ("UTIL",), "ENV_NUCLEARPOWER": ("UTIL",), "ENV_SOLAR": ("UTIL", "ENRG"), "ENV_WINDPOWER": ("UTIL",),
    "ENV_HYDRO": ("UTIL",), "ECON_ELECTRICALPRICE": ("UTIL",),
    "ECON_HOUSING_PRICES": ("REAL",), "ECON_MORTGAGE": ("REAL", "FINS"),
    "ECON_CENTRALBANK": ("FINS",), "ECON_INTEREST_RATES": ("FINS",), "WB_1235_CENTRAL_BANKS": ("FINS",),
    "ECON_STOCKMARKET": ("FINS",), "ECON_IPO": ("FINS",), "ECON_BANKRUN": ("FINS",), "ECON_BAILOUT": ("FINS",),
    "EPU_CATS_FINANCIAL_REGULATION": ("FINS",), "ECON_CRYPTOCURRENCY": ("FINS",), "ECON_BITCOIN": ("FINS",),
    "CYBER_ATTACK": ("TECH",), "INFO_HACKING": ("TECH",), "SOC_EMERGINGTECH": ("TECH",), "TECH_AUTOMATION": ("TECH",),
    "WB_133_INFORMATION_AND_COMMUNICATION_TECHNOLOGIES*": ("TECH", "COMM"), "MEDIA_SOCIAL": ("COMM",),
    "WB_694_BROADCAST_AND_MEDIA": ("COMM",),
    "HEALTH_PANDEMIC": ("HLTH",), "HEALTH_VACCINATION": ("HLTH",), "EPU_CATS_HEALTHCARE": ("HLTH",),
    "WB_1331_HEALTH_TECHNOLOGIES": ("HLTH",), "MEDICAL_SECURITY": ("HLTH",),
    "AGRICULTURE": ("CSTA",), "FOOD_SECURITY": ("CSTA",), "FOOD_STAPLE": ("CSTA",), "TAX_FOODSTAPLES*": ("CSTA",),
    "ENV_FISHERY": ("CSTA",),
    "MARITIME_INCIDENT": ("INDU",), "ECON_TRANSPORT_COST": ("INDU",), "WB_1804_AIRPORTS": ("INDU",),
    "AVIATION_INCIDENT": ("INDU",), "WB_1803_TRANSPORT_INFRASTRUCTURE": ("INDU",), "STRIKE": ("INDU",),
    "ECON_UNIONS": ("INDU",),
}

# ------------------------------------------------------------------------------------------------ countries
def _iso2_list() -> list[str]:
    codes = set(FIPS_TO_ISO2.values()) | set(ISO3_TO_ISO2.values()) | {"XK"}
    return sorted(c for c in codes if c and len(c) == 2)


COUNTRIES: list[str] = [GLOBAL, *_iso2_list()]          # id = index; GLOBAL = 0 (never renumber: append only)
COUNTRY_ID: dict[str, int] = {c: i for i, c in enumerate(COUNTRIES)}


def country_name(iso2: str) -> str:
    if iso2 == GLOBAL:
        return "Global / multi-country"
    try:
        import pycountry  # noqa: PLC0415

        c = pycountry.countries.get(alpha_2=iso2)
        return c.name if c else iso2
    except Exception:  # pragma: no cover
        return iso2


def fips_to_iso2(code: str | None) -> str | None:
    return FIPS_TO_ISO2.get(code.upper()) if code else None


def countries_with_global(counts: list[tuple[str, int]], theme_mask: int = 0) -> list[str]:
    """Ranked ISO2 (by location mentions) -> label list: top MAX_COUNTRIES plus GLOBAL per the GLOBAL rule."""
    if not counts:
        return [GLOBAL]
    counts = sorted(counts, key=lambda x: (-x[1], x[0]))
    top = [c for c, _ in counts[:MAX_COUNTRIES]]
    total = sum(n for _, n in counts) or 1
    is_global = len(counts) >= GLOBAL_MIN_COUNTRIES or (
        bool(theme_mask & GLOBAL_THEME_MASK) and counts[0][1] / total < 0.5 and len(counts) >= 2)
    return [*top, GLOBAL] if is_global else top


# ------------------------------------------------------------------------------------------------ polars expressions
def _theme_alt(names: tuple[str, ...], prefixes: tuple[str, ...]) -> str | None:
    parts = [re.escape(n) for n in names] + [re.escape(p) + r"(?:_[^;,]*)?" for p in prefixes]
    if not parts:
        return None
    return r"(?:^|;)(?:" + "|".join(parts) + r")(?:;|,|$)"


def gkg_theme_mask_expr(themes_col: str = "V1Themes") -> pl.Expr:
    """GKG V1Themes ('A;B;C') -> int64 market-theme mask (any single mention)."""
    col = pl.col(themes_col).fill_null("")
    terms = []
    for t in THEMES:
        rx = _theme_alt(t.gkg, t.gkg_prefix)
        if rx:
            terms.append(pl.when(col.str.contains(rx)).then(pl.lit(1 << t.bit, pl.Int64)).otherwise(pl.lit(0, pl.Int64)))
    return pl.sum_horizontal(terms).cast(pl.Int64)


# Specific (rare, unambiguous) GKG themes count from a single mention; every other mapped GKG theme must be mentioned
# at least BROAD_MIN_MENTIONS times in the article (V2EnhancedThemes lists one 'THEME,offset' entry per occurrence),
# otherwise passing references ("... amid the war ...") would label most of the news flow.
STRONG_GKG = frozenset({
    "ECON_BANKRUPTCY", "SANCTIONS", "EMBARGO", "CYBER_ATTACK", "ECON_CENTRALBANK", "ECON_INTEREST_RATES", "ECON_IPO",
    "ECON_EARNINGSREPORT", "ECON_TRADE_DISPUTE", "PRODUCT_RECALL", "COUP", "ECON_NATIONALIZE", "HEALTH_PANDEMIC",
    "ECON_HOUSING_PRICES", "ECON_OILPRICE", "ECON_NATGASPRICE", "ECON_CURRENCY_EXCHANGE_RATE", "ECON_DEVALUATION",
    "ECON_CRYPTOCURRENCY", "ECON_BITCOIN", "ECON_MONOPOLY", "POWER_OUTAGE", "ECON_INFLATION", "ECON_STOCKMARKET",
    "EPU_CATS_MONETARY_POLICY", "EPU_CATS_TRADE_POLICY", "ECON_MERGERS", "ECON_ACQUISITION", "ECON_TAKEOVER",
    "ECON_CUTOUTLOOK", "ECON_GASOLINEPRICE", "ECON_DIESELPRICE",
    "ECON_ELECTRICALPRICE", "ECON_FOREIGNINVEST", "ECON_SUBSIDIES", "ENV_MINING", "ENV_NUCLEARPOWER",
})
BROAD_MIN_MENTIONS = 2


def gkg_theme_mask_counted_expr(enhanced_col: str = "V2EnhancedThemes", min_broad: int = BROAD_MIN_MENTIONS) -> pl.Expr:
    """GKG V2EnhancedThemes ('A,12;B,40;A,77') -> int64 mask with the per-theme mention-count rule
    (translingual articles use min_broad = 3: machine-translated theme coding is noisier)."""
    col = pl.col(enhanced_col).fill_null("")
    terms = []
    for t in THEMES:
        strong = tuple(g for g in t.gkg if g in STRONG_GKG)
        broad = tuple(g for g in t.gkg if g not in STRONG_GKG)
        conds = []
        if strong:
            conds.append(col.str.contains(r"(?:^|;)(?:" + "|".join(re.escape(g) for g in strong) + r"),"))
        if broad or t.gkg_prefix:
            parts = [re.escape(g) for g in broad] + [re.escape(p) + r"(?:_[^;,]*)?" for p in t.gkg_prefix]
            rx = r"(?:^|;)(?:" + "|".join(parts) + r"),"
            conds.append(col.str.count_matches(rx) >= min_broad)
        if conds:
            hit = conds[0] if len(conds) == 1 else (conds[0] | conds[1])
            terms.append(pl.when(hit).then(pl.lit(1 << t.bit, pl.Int64)).otherwise(pl.lit(0, pl.Int64)))
    return pl.sum_horizontal(terms).cast(pl.Int64)


_TITLE_RX: list[tuple[int, str]] = [(t.bit, r"(?i)\b(?:" + t.title + r")\b") for t in THEMES if t.title]


def title_theme_mask_expr(title_col: str = "title") -> pl.Expr:
    """English headline keyword rules -> int64 mask (apply to English titles only)."""
    col = pl.col(title_col).fill_null("")
    terms = [pl.when(col.str.contains(rx)).then(pl.lit(1 << b, pl.Int64)).otherwise(pl.lit(0, pl.Int64))
             for b, rx in _TITLE_RX]
    return pl.sum_horizontal(terms).cast(pl.Int64)


def title_theme_mask(title: str | None) -> int:
    if not title:
        return 0
    m = 0
    for b, rx in _TITLE_RX:
        if re.search(rx, title):
            m |= 1 << b
    return m


def _sector_rx() -> list[tuple[int, str]]:
    by_sector: dict[str, list[str]] = {}
    for k, secs in GKG_SECTOR.items():
        pat = re.escape(k[:-1]) + r"[^;,]*" if k.endswith("*") else re.escape(k)
        for s in secs:
            by_sector.setdefault(s, []).append(pat)
    return [(SECTOR_BIT[s], r"(?:^|;)(?:" + "|".join(p) + r")(?:;|,|$)") for s, p in by_sector.items()]


_SECTOR_RX = _sector_rx()


def gkg_sector_mask_expr(themes_col: str = "V1Themes") -> pl.Expr:
    col = pl.col(themes_col).fill_null("")
    terms = [pl.when(col.str.contains(rx)).then(pl.lit(1 << b, pl.Int16)).otherwise(pl.lit(0, pl.Int16))
             for b, rx in _SECTOR_RX]
    return pl.sum_horizontal(terms).cast(pl.Int16)


def cameo_theme_mask_expr(code_col: str = "EventCode") -> pl.Expr:
    """CAMEO EventCode (string, e.g. '0231', '143', '1831') -> int64 theme mask (prefix rules)."""
    col = pl.col(code_col).fill_null("")
    terms = []
    for t in THEMES:
        if t.cameo:
            rx = r"^(?:" + "|".join(re.escape(c) for c in t.cameo) + r")"
            terms.append(pl.when(col.str.contains(rx)).then(pl.lit(1 << t.bit, pl.Int64)).otherwise(pl.lit(0, pl.Int64)))
    return pl.sum_horizontal(terms).cast(pl.Int64)


# market theme -> sector LABEL (sector-specific themes only; macro / geopolitical themes do not label a sector -
# their "typically affected" sectors live in Theme.sectors as a UI hint). Article / event sector_mask =
# linked issuers' sectors | sectors of these themes.
SECTOR_LABEL_THEMES: dict[str, tuple[str, ...]] = {
    "monetary_policy": ("FINS",), "capital_markets": ("FINS",), "banking_financial_stress": ("FINS",),
    "crypto": ("FINS",), "cyber": ("TECH",), "tech_ai": ("TECH",), "pandemic_epidemic": ("HLTH",),
    "healthcare_pharma": ("HLTH",), "oil_gas": ("ENRG",), "power_utilities": ("UTIL",), "metals_mining": ("MATR",),
    "agriculture_food": ("CSTA",), "supply_chain_shipping": ("INDU",), "housing_real_estate": ("REAL",),
    "aviation_transport": ("INDU",),
}
_SECTOR_BY_BIT: list[tuple[int, int]] = [(1 << THEME_BIT[t], sector_mask(secs)) for t, secs in SECTOR_LABEL_THEMES.items()]


def theme_sector_mask_expr(mask_col: str = "theme_mask") -> pl.Expr:
    """int64 theme mask -> int16 sector mask via SECTOR_LABEL_THEMES."""
    terms = [pl.when((pl.col(mask_col) & tb) != 0).then(pl.lit(sm, pl.Int32)).otherwise(pl.lit(0, pl.Int32))
             for tb, sm in _SECTOR_BY_BIT]
    return pl.reduce(lambda acc, x: acc | x, terms).cast(pl.Int16)


def theme_sector_mask(mask: int | None) -> int:
    m = int(mask or 0)
    out = 0
    for tb, sm in _SECTOR_BY_BIT:
        if m & tb:
            out |= sm
    return out


def mask_to_theme_ids(mask: int | None) -> list[str]:
    if not mask:
        return []
    m = int(mask)
    return [t.id for t in THEMES if (m >> t.bit) & 1]


def theme_ids_to_mask(ids: list[str] | None) -> int:
    m = 0
    for i in ids or ():
        t = THEME_BY_ID.get(i)
        if t is None:
            raise KeyError(f"unknown theme {i!r}")
        m |= 1 << t.bit
    return m


__all__ = ["THEMES", "Theme", "SECTORS", "COUNTRIES", "COUNTRY_ID", "GLOBAL", "GLOBAL_ID", "GKG_SECTOR",
           "gkg_theme_mask_expr", "title_theme_mask_expr", "gkg_sector_mask_expr", "cameo_theme_mask_expr",
           "countries_with_global", "mask_to_theme_ids", "theme_ids_to_mask", "sector_mask", "sectors_of_mask",
           "fips_to_iso2", "title_theme_mask", "TAXONOMY_VERSION", "field"]
