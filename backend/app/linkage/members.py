"""Geographic member normalization (A5): XBRL geographic axis members -> ISO2 country / region code.

Inputs seen in the wild:
  * SEC FSDS segments: ISO2 already for srt country members ("US", "JP"), else the custom element name without
    "Member" ("SoutheastAsia", "NonUs", "PRCHK", "NortheastRegion", "Guangdong").
  * xBRL-JSON / IFRS: "ifrs-full:CountryOfDomicileMember", "ifrs-full:ForeignCountriesMember", "country:US",
    "tx:IFRS8InfoAreaGeog_Chile_7", "abc:EuropeMember".
member_type:
  country        iso2 set
  region         region code (REGIONS keys) - multi-country area; resolved to countries with region_countries()
  domestic       company home country (iso2 = company_country when known)
  rest_of_world  "other countries", "international", "non-US" ... = everything not separately reported
  other          not geographic (eliminations, corporate, segment names) - excluded from shares
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from typing import Any

import polars as pl

from app.core.config import Settings

EU27 = ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
        "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"]
MIDDLE_EAST = ["AE", "BH", "IL", "IQ", "IR", "JO", "KW", "LB", "OM", "PS", "QA", "SA", "SY", "TR", "YE", "EG"]
LATAM_X = ["MX", "GT", "BZ", "SV", "HN", "NI", "CR", "PA", "CU", "DO", "HT", "JM", "PR", "TT", "BS", "BB"]

#: region code -> definition: explicit ISO2 list and/or Natural Earth (continent | subregion) selectors
REGIONS: dict[str, dict[str, Any]] = {
    "EUROPE": {"continent": ["Europe"]},
    "EU": {"iso2": EU27},
    "WESTERN_EUROPE": {"subregion": ["Western Europe", "Northern Europe", "Southern Europe"]},
    "EASTERN_EUROPE": {"subregion": ["Eastern Europe"]},
    "NORDICS": {"iso2": ["DK", "FI", "IS", "NO", "SE"]},
    "ASIA": {"continent": ["Asia"]},
    "ASIA_PACIFIC": {"continent": ["Asia", "Oceania"]},
    "EAST_ASIA": {"subregion": ["Eastern Asia"]},
    "SOUTHEAST_ASIA": {"subregion": ["South-Eastern Asia"]},
    "SOUTH_ASIA": {"subregion": ["Southern Asia"]},
    "GREATER_CHINA": {"iso2": ["CN", "HK", "MO", "TW"]},
    "CHINA_HK": {"iso2": ["CN", "HK", "MO"]},
    "OCEANIA": {"continent": ["Oceania"]},
    "AUSTRALASIA": {"iso2": ["AU", "NZ"]},
    "NORTH_AMERICA": {"iso2": ["US", "CA", "MX"]},
    "US_CA": {"iso2": ["US", "CA"]},
    "LATAM": {"subregion": ["South America", "Central America", "Caribbean"], "iso2": LATAM_X},
    "SOUTH_AMERICA": {"subregion": ["South America"]},
    "CENTRAL_AMERICA_CARIBBEAN": {"subregion": ["Central America", "Caribbean"]},
    "AMERICAS": {"continent": ["North America", "South America"]},
    "MIDDLE_EAST": {"iso2": MIDDLE_EAST},
    "AFRICA": {"continent": ["Africa"]},
    "MEA": {"continent": ["Africa"], "iso2": MIDDLE_EAST},
    "EMEA": {"continent": ["Europe", "Africa"], "iso2": MIDDLE_EAST},
    "CIS": {"iso2": ["RU", "BY", "KZ", "UZ", "KG", "TJ", "TM", "AM", "AZ", "MD", "GE", "UA"]},
}

#: normalized alias (lowercase letters only) -> region code / special type
REGION_ALIASES: dict[str, str] = {
    "europe": "EUROPE", "eu": "EU", "europeanunion": "EU", "europeexcludingunitedkingdom": "EUROPE",
    "continentaleurope": "EUROPE", "westerneurope": "WESTERN_EUROPE", "easterneurope": "EASTERN_EUROPE",
    "centralandeasterneurope": "EASTERN_EUROPE", "nordic": "NORDICS", "nordics": "NORDICS", "nordiccountries": "NORDICS",
    "scandinavia": "NORDICS", "asia": "ASIA", "asiapacific": "ASIA_PACIFIC", "apac": "ASIA_PACIFIC", "pacific": "OCEANIA",
    "asiapacificexcludingchina": "ASIA_PACIFIC", "asiaandpacific": "ASIA_PACIFIC", "asiaexcludingchina": "ASIA",
    "otherasia": "ASIA", "otherasiapacific": "ASIA_PACIFIC", "restofasia": "ASIA", "restofasiapacific": "ASIA_PACIFIC",
    "eastasia": "EAST_ASIA", "southeastasia": "SOUTHEAST_ASIA", "asean": "SOUTHEAST_ASIA", "southasia": "SOUTH_ASIA",
    "greaterchina": "GREATER_CHINA", "prchk": "CHINA_HK", "chinaincludinghongkong": "CHINA_HK",
    "chinaandhongkong": "CHINA_HK", "mainlandchinaandhongkong": "CHINA_HK", "oceania": "OCEANIA",
    "australasia": "AUSTRALASIA", "australiaandnewzealand": "AUSTRALASIA", "northamerica": "NORTH_AMERICA",
    "unitedstatesandcanada": "US_CA", "usandcanada": "US_CA", "latinamerica": "LATAM", "latam": "LATAM",
    "southamerica": "SOUTH_AMERICA", "centralandsouthamerica": "LATAM", "southandcentralamerica": "LATAM",
    "latinamericaandcaribbean": "LATAM", "centralamerica": "CENTRAL_AMERICA_CARIBBEAN", "caribbean": "CENTRAL_AMERICA_CARIBBEAN",
    "americas": "AMERICAS", "otheramericas": "AMERICAS", "americasexcludingunitedstates": "AMERICAS",
    "middleeast": "MIDDLE_EAST", "middleeastandafrica": "MEA", "mea": "MEA", "africa": "AFRICA", "africaandmiddleeast": "MEA",
    "emea": "EMEA", "europemiddleeastandafrica": "EMEA", "europeandmiddleeast": "EMEA", "cis": "CIS",
    "commonwealthofindependentstates": "CIS", "fareast": "EAST_ASIA", "pacificrim": "ASIA_PACIFIC",
    "asiapacificandjapan": "ASIA_PACIFIC", "apj": "ASIA_PACIFIC", "apac": "ASIA_PACIFIC", "emeia": "EMEA",
    "europemiddleeastindiaandafrica": "EMEA", "europemiddleeastandafrica": "EMEA", "europemiddleeastafrica": "EMEA",
    "eame": "EMEA", "europa": "EUROPE", "norteamerica": "NORTH_AMERICA", "americadelnorte": "NORTH_AMERICA",
    "americalatina": "LATAM", "latinoamerica": "LATAM", "sudamerica": "SOUTH_AMERICA", "americadelsur": "SOUTH_AMERICA",
    "centroamerica": "CENTRAL_AMERICA_CARIBBEAN", "caribe": "CENTRAL_AMERICA_CARIBBEAN", "africa_": "AFRICA",
    "oriente medio": "MIDDLE_EAST", "medioriente": "MIDDLE_EAST", "orientemedio": "MIDDLE_EAST", "mena": "MIDDLE_EAST", "gcc": "MIDDLE_EAST", "nafta": "NORTH_AMERICA", "usmca": "NORTH_AMERICA",
}
ROW_ALIASES = {
    "nonus", "international", "foreign", "foreigncountries", "foreigncountriesmember", "outsideus",
    "outsideunitedstates", "nonunitedstates", "restofworld", "restoftheworld", "otherforeigncountries", "othercountries",
    "allothercountries", "othercountry", "otherinternational", "othernonus", "otherforeign", "allotherforeigncountries",
    "othergeographicalareas", "othergeographicareas", "otherregions", "otherregion", "worldwide", "global",
    "allothergeographicareas", "allother", "rest", "others", "other", "foreigncountriesother", "overseas",
    "internationaloperations", "nondomestic", "exports", "export", "outsideofunitedstates", "internationalmarkets",
    "extranjero", "exterior", "otrospaises", "restodelmundo", "otros", "otrasregiones", "internacional",
    "totalinternational", "internationalsegment", "foreignoperations", "geographicdistributionforeign",
    "othergeographicregion", "othergeographicalarea", "othergeographicarea", "otherlocation", "otherlocations",
    "othergeographical", "othergeographicallocation", "othergeographiclocations", "othergeographiclocation",
    "othergeographicregions", "othergeographicalregion", "othergeographicalregions", "otherareas", "otherarea",
    "allothergeographicsegments", "segmentgeographicalgroupofothercountries", "foreignsegment", "nonusa",
}
DOMESTIC_ALIASES = {"geographicdistributiondomestic", "domesticsegment", "domestic", "countryofdomicile", "countryofdomicilemember", "home", "homecountry", "local",
                    "domesticmarket", "domesticoperations"}
NOT_GEO_RX = re.compile(r"(elimination|corporate|unallocated|segment|consolidat|intersegment|adjust|reconcil|"
                        r"management|services?$|product|business|operation(s)?$|total$|discontinued)")

US_SUBREGIONS = {"northeast", "northeastregion", "southeast", "southeastregion", "midwest", "midwestregion", "west",
                 "westregion", "southwest", "southwestregion", "northwest", "pacificnorthwest", "mountain", "midatlantic",
                 "texas", "california", "florida", "newyork", "newengland", "gulfcoast", "south", "southregion",
                 "central", "centralregion", "eastregion", "northregion", "puertorico_", "unitedstatesexcludingpuertorico"}
CN_PROVINCES = {"anhui", "beijing", "chongqing", "fujian", "gansu", "guangdong", "guangxi", "guizhou", "hainan", "hebei",
                "heilongjiang", "henan", "hubei", "hunan", "jiangsu", "jiangxi", "jilin", "liaoning", "innermongolia",
                "ningxia", "qinghai", "shaanxi", "shandong", "shanghai", "shanxi", "sichuan", "tianjin", "tibet",
                "xinjiang", "yunnan", "zhejiang", "mainlandchina", "prc", "peoplesrepublicofchina", "chinamainland"}
CA_PROVINCES = {"ontario", "quebec", "alberta", "britishcolumbia", "manitoba", "saskatchewan", "novascotia",
                "newbrunswick", "newfoundlandandlabrador", "princeedwardisland"}
COUNTRY_ALIASES: dict[str, str] = {
    "unitedstates": "US", "usa": "US", "us": "US", "unitedstatesofamerica": "US", "uk": "GB", "unitedkingdom": "GB",
    "greatbritain": "GB", "england": "GB", "scotland": "GB", "britain": "GB", "china": "CN", "korea": "KR",
    "southkorea": "KR", "republicofkorea": "KR", "taiwan": "TW", "russia": "RU", "russianfederation": "RU",
    "hongkong": "HK", "macau": "MO", "macao": "MO", "vietnam": "VN", "holland": "NL", "thenetherlands": "NL",
    "czechrepublic": "CZ", "czechia": "CZ", "turkey": "TR", "turkiye": "TR", "uae": "AE", "unitedarabemirates": "AE",
    "saudiarabia": "SA", "ksa": "SA", "brasil": "BR", "mexico": "MX", "espana": "ES", "deutschland": "DE",
    "suisse": "CH", "schweiz": "CH", "puertorico": "PR", "ivorycoast": "CI", "cotedivoire": "CI", "slovakia": "SK",
    "laos": "LA", "iran": "IR", "syria": "SY", "bolivia": "BO", "venezuela": "VE", "tanzania": "TZ",
}


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"^[A-Za-z0-9_-]+:", "", s.strip())  # namespace prefix (ifrs-full:, country:, tx:)
    s = re.sub(r"Member$", "", s)
    s = re.sub(r"^IFRS8InfoAreaGeog_?", "", s)
    s = re.sub(r"_?\d+$", "", s)
    return re.sub(r"[^a-z]", "", s.lower())


@lru_cache(maxsize=4)
def _geo_names(_key: str) -> tuple[dict[str, str], dict[str, dict[str, list[str]]]]:
    """(normalized country name -> ISO2, {continent|subregion: {name: [iso2]}}) from geo_countries."""
    from app.core.storage import read_table  # noqa: PLC0415

    names: dict[str, str] = {}
    groups: dict[str, dict[str, list[str]]] = {"continent": {}, "subregion": {}}
    try:
        g = read_table("geo_countries")
    except Exception:  # noqa: BLE001 - not built yet: static aliases only
        return names, groups
    for r in g.iter_rows(named=True):
        for n in [r.get("name"), r.get("name_long"), r.get("formal_name"), *(r.get("alt_names") or [])]:
            k = norm(n)
            if k and k not in names:
                names[k] = r["iso2"]
        for f in ("continent", "subregion"):
            if r.get(f):
                groups[f].setdefault(r[f], []).append(r["iso2"])
    return names, groups


def region_countries(region: str, settings: Settings | None = None) -> list[str]:
    """Countries of a region code; composite codes ('ASIA_PACIFIC+JP') are unions, ISO2 parts are themselves."""
    if "+" in region:
        return sorted({c for part in region.split("+") for c in region_countries(part, settings)})
    d = REGIONS.get(region)
    if not d:
        return [region] if len(region) == 2 and region.isalpha() and region.isupper() else []
    _, groups = _geo_names(str(settings.data_dir) if settings else "")
    out = set(d.get("iso2", []))
    for f in ("continent", "subregion"):
        for v in d.get(f, []):
            out |= set(groups[f].get(v, []))
    return sorted(out)


def classify(member_raw: str | None, company_country: str | None = None,
             settings: Settings | None = None) -> tuple[str, str | None, str | None]:
    """member -> (member_type, iso2, region)."""
    raw = (member_raw or "").strip()
    if not raw:
        return "other", None, None
    bare = re.sub(r"^[A-Za-z0-9_-]+:", "", raw)
    bare = re.sub(r"Member$", "", bare)
    if len(bare) == 2 and bare.isalpha() and bare.isupper():
        return ("country", "GB" if bare == "UK" else bare, None)
    k = norm(raw)
    if not k:
        return "other", None, None
    if k in DOMESTIC_ALIASES:
        return "domestic", company_country, None
    if k in ROW_ALIASES:
        return "rest_of_world", None, None
    if k in REGION_ALIASES:
        return "region", None, REGION_ALIASES[k]
    if k in COUNTRY_ALIASES:
        return "country", COUNTRY_ALIASES[k], None
    names, _ = _geo_names(str(settings.data_dir) if settings else "")
    if k in names:
        return "country", names[k], None
    if k in CN_PROVINCES:
        return "country", "CN", None
    if k in CA_PROVINCES:
        return "country", "CA", None
    if k in US_SUBREGIONS and (company_country in (None, "", "US")):
        return "country", "US", None
    # composite like "UnitedStatesAndCanada" / "EuropeAndAsia" not in aliases: try "<country>excluding..."
    for pref in ("otherthan", "excluding", "outside", "nonusa", "non"):
        if k.startswith(pref):
            return "rest_of_world", None, None
    m = re.match(r"^(other|rest)(of)?(the)?(.+)$", k)
    if m and m.group(4) in REGION_ALIASES:
        return "region", None, REGION_ALIASES[m.group(4)]
    comp = composite(k, settings)
    if comp is not None:
        return comp
    if NOT_GEO_RX.search(k):
        return "other", None, None
    for suf in ("region", "countries", "country", "markets", "market", "operations", "area", "excludingus"):
        if k.endswith(suf) and k[: -len(suf)]:
            t, i, r = classify(k[: -len(suf)], company_country, settings)
            if t != "other":
                return t, i, r
    return "other", None, None


FILLER = {"and", "other", "others", "the", "of", "rest", "countries", "country", "region", "regions", "area", "areas",
          "geographic", "geographical", "location", "locations", "all", "remaining", "total", "markets", "market",
          "operations", "excluding", "excl", "except", "ex", "outside", "than", "non", "international", "foreign",
          "world", "plus", "including", "incl", "territories", "continental", "mainland", "sales", "revenue",
          "revenues", "segment", "segments", "member", "nations", "islands", "y", "el", "los", "otros", "otras",
          "del", "de", "resto", "mundo", "paises"}
_STOP = {"excluding", "excl", "except", "ex", "outside", "than", "non"}


def _vocab(settings: Settings | None) -> dict[str, str]:
    names, _ = _geo_names(str(settings.data_dir) if settings else "")
    v: dict[str, str] = {}
    for k, iso in {**names, **COUNTRY_ALIASES}.items():
        if len(k) >= 4 or k in ("us", "uk", "usa", "uae"):
            v[k] = iso
    for k, r in REGION_ALIASES.items():
        v[k] = r
    for k in ("asia", "pacific", "europe", "africa", "americas", "middleeast", "latinamerica", "northamerica",
              "southamerica", "oceania", "caribbean", "centralamerica", "fareast", "nordic", "nordics", "scandinavia"):
        v.setdefault(k, REGION_ALIASES.get(k, "OCEANIA" if k == "pacific" else k.upper()))
    for f in FILLER:
        v.setdefault(f, "")
    return v


def segment(k: str, vocab: dict[str, str]) -> list[str] | None:
    """Split a normalized member into vocabulary tokens (max coverage DP); None if it cannot be fully covered."""
    n = len(k)
    best: list[list[str] | None] = [None] * (n + 1)
    best[0] = []
    for i in range(n):
        if best[i] is None:
            continue
        for j in range(min(n, i + 40), i, -1):
            w = k[i:j]
            if w in vocab and (best[j] is None or len(best[j]) > len(best[i]) + 1):
                best[j] = best[i] + [w]
    return best[n]


def composite(k: str, settings: Settings | None = None) -> tuple[str, str | None, str | None] | None:
    """'AsiaPacificAndJapan' -> region 'ASIA_PACIFIC+JP'; 'AmericasOtherThanUS' -> region 'AMERICAS';
    'OtherEuropeanCountries' -> region 'EUROPE'; 'OtherInternational' -> rest_of_world."""
    vocab = _vocab(settings)
    k = k.replace("amrica", "america")  # accent dropped by the filer ("Norteamrica")
    toks = segment(k, vocab) or segment(
        k.replace("european", "europe").replace("asian", "asia").replace("african", "africa"), vocab)
    if not toks:
        return None
    for i, w in enumerate(toks):  # "X excluding Y" / "X other than Y" -> X
        if w in _STOP and i > 0:
            toks = toks[:i]
            break
    geo = [vocab[w] for w in toks if vocab.get(w)]
    if not geo:
        if any(w in ("other", "others", "rest", "international", "foreign", "remaining", "world", "otros", "otras",
                     "resto") for w in toks):
            return "rest_of_world", None, None
        return None
    geo = sorted(set(geo))
    if len(geo) == 1 and len(geo[0]) == 2 and "other" not in toks and "rest" not in toks:
        return "country", geo[0], None
    return "region", None, "+".join(geo)


def map_members(pairs: pl.DataFrame, settings: Settings | None = None) -> pl.DataFrame:
    """[member_raw, company_country] -> + [member_type, iso2, region]."""
    rows = []
    for raw, cc in pairs.select("member_raw", "company_country").iter_rows():
        t, i, r = classify(raw, cc, settings)
        rows.append({"member_raw": raw, "company_country": cc, "member_type": t, "iso2": i, "region": r})
    return pl.DataFrame(rows, schema={"member_raw": pl.Utf8, "company_country": pl.Utf8, "member_type": pl.Utf8,
                                      "iso2": pl.Utf8, "region": pl.Utf8})
