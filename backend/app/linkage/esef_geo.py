"""Roadmap #9: geographic revenue from ESEF annual reports where it is tagged (job `linkage_esef_geo`).

ESEF (EU / EEA annual financial reports, FY2022+) requires block tagging of the notes: the segment note is tagged as
an ifrs-full text block (DisclosureOfEntitysReportableSegmentsExplanatory, DisclosureOfGeographicalAreasExplanatory,
DisclosureOfOperatingSegmentsExplanatory, DisclosureOfDisaggregationOfRevenueFromContractsWithCustomersExplanatory),
an ix:nonNumeric element (+ ix:continuation chain) that contains the HTML tables of the note. Detailed dimensional
tagging (ifrs-full:GeographicalAreasAxis) is optional and rare - those facts are already read by
app.linkage.georev.ifrs_dimensional. Here the TABLES inside the tagged text blocks are parsed with rules (no LLM):

  * a table qualifies when >= 2 row labels map to countries / regions (Natural Earth names in de / es / fr / it / nl /
    pl / pt / sv + Nordic aliases + region words in 10 languages, then app.linkage.members.classify), the mapped rows
    are >= 60% of the labelled numeric rows, and its title / header / preceding text reads as revenue ("revenue",
    "external customers", "intäkter", "omsetning", "chiffre d'affaires", "ingresos", "ricavi", "omzet", "Umsatz" ...)
    rather than assets;
  * value column = the header column with the report's fiscal year (else the first numeric column); multi-measure
    tables (revenue | non-current assets side by side) use the column group whose header reads as revenue;
  * shares = value / the table's total row when it matches the sum of rows within 3%, else / sum of rows;
  * confidence 0.7 (+0.1 when the total row reconciles); method ixbrl_textblock_table.
Inputs: the raw ESEF packages landed by the national-OAM adapters (raw/<source>/packages/**.zip|.xbri), matched to
filings_index (filing id, entity, issuer, period end, knowledge_date = filing date, PIT). One package at a time.
Output: raw/linkage/esef_geo/rows_<stamp>.parquet (company_geo_revenue columns), merged by georev.build().
State data/state/linkage_esef_geo.json {"done": {package: n_rows}}.
"""

from __future__ import annotations

import re
import unicodedata
import zipfile
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import polars as pl

from app.core.config import Settings
from app.linkage.common import load_state, log, now_utc, raw_dir, save_state, settings_of

STATE = "esef_geo"
SOURCES = ("oslo_newsweb", "nasdaq_nordic", "afm_nl", "fsma_be", "dila_fr", "cnmv_es", "emarket_it")
ANNUAL_FORMS = {"AFR", "URD", "ESEF", "AR", "ACS", "10-K"}
BLOCKS = ("DisclosureOfGeographicalAreasExplanatory", "DisclosureOfEntitysReportableSegmentsExplanatory",
          "DisclosureOfOperatingSegmentsExplanatory", "DisclosureOfDisaggregationOfRevenueFromContractsWithCustomersExplanatory",
          "DisclosureOfRevenueFromContractsWithCustomersExplanatory", "DisclosureOfRevenueExplanatory")
IX = "{http://www.xbrl.org/2013/inlineXBRL}"
XH = "{http://www.w3.org/1999/xhtml}"
MAX_XHTML = 120_000_000

REV_WORDS = re.compile(
    r"revenue|revenues|sales|turnover|external\s+customers|income\s+from\s+customers|net\s+sales|intäkter|intakter|"
    r"omsättning|omsattning|nettoomsättning|försäljning|inntekter|driftsinntekter|omsetning|salgsinntekter|"
    r"omsætning|omsaetning|nettoomsætning|indtægter|liikevaihto|myynti|tuotot|chiffre\s+d.affaires|ventes|produits\s+des|"
    r"ingresos|ventas|cifra\s+de\s+negocios|importe\s+neto|ricavi|fatturato|vendite|omzet|opbrengsten|netto-omzet|"
    r"umsatz|umsatzerlöse|umsatzerlose|erlöse|receitas|vendas|volume\s+de\s+negócios|przychody|sprzedaż", re.I)
ASSET_WORDS = re.compile(
    r"assets|anläggningstillgångar|anlaggningstillgangar|tillgångar|eiendeler|anleggsmidler|anlægsaktiver|aktiver|"
    r"pitkäaikaiset\s+varat|varat|actifs|immobilisations|activos|attività|attivita|immobilizzazioni|activa|"
    r"vaste\s+activa|vermögenswerte|vermogenswerte|anlagevermögen|ativos|aktywa|investments|capex|employees|"
    r"anställda|ansatte|medarbejdere|effectifs|empleados|dipendenti|werknemers|mitarbeiter", re.I)
TOTAL_WORDS = re.compile(
    r"^(total|totals|totalt|summa|sum|i\s+alt|yhteensä|yhteensa|total\s+group|group|koncernen|konsernet|koncern|"
    r"totale|totaal|gesamt|insgesamt|razem|ogółem|consolidated|eliminations?|elimineringar|elimineringer|"
    r"unallocated|corporate|total\s+revenue|total\s+sales|net\s+sales|revenue)\b", re.I)
OTHER_WORDS = {
    "other", "others", "othercountries", "otherregions", "otherareas", "otherworld", "restofworld", "restoftheworld",
    "rest", "row", "övriga", "ovriga", "övrigt", "ovrigt", "övrigavärlden", "ovrigavarlden", "övrigaländer",
    "ovrigalander", "andra", "øvrige", "ovrige", "øvrigeland", "ovrigeland", "restenavverden", "resteneuropa", "andre",
    "andreland", "muut", "muutmaat", "muualla", "muumaailma", "autres", "autrespays", "restedumonde", "reste",
    "otros", "otrospaises", "restodelmundo", "resto", "altri", "altripaesi", "restodelmondo", "overige", "overig",
    "overigelanden", "restvandewereld", "sonstige", "übrige", "ubrige", "restderwelt", "sonstigelander", "outros",
    "outrospaises", "restodomundo", "pozostałe", "pozostale", "international", "internationalt", "utland",
    "utlandet", "udland", "ulkomaat", "etranger", "extranjero", "estero", "buitenland", "ausland", "exterior",
}
REGION_WORDS = {  # normalized -> REGIONS code of app.linkage.members
    "europa": "EUROPE", "europe": "EUROPE", "eu": "EU", "eurooppa": "EUROPE", "nordic": "NORDICS", "nordics": "NORDICS",
    "norden": "NORDICS", "pohjoismaat": "NORDICS", "skandinavien": "NORDICS", "skandinavia": "NORDICS",
    "scandinavia": "NORDICS", "asien": "ASIA", "asia": "ASIA", "aasia": "ASIA", "asie": "ASIA", "azja": "ASIA",
    "nordamerika": "NORTH_AMERICA", "northamerica": "NORTH_AMERICA", "amériquedunord": "NORTH_AMERICA",
    "ameriquedunord": "NORTH_AMERICA", "norteamérica": "NORTH_AMERICA", "norteamerica": "NORTH_AMERICA",
    "americadelnord": "NORTH_AMERICA", "noordamerika": "NORTH_AMERICA", "pohjoisamerikka": "NORTH_AMERICA",
    "sydamerika": "SOUTH_AMERICA", "southamerica": "SOUTH_AMERICA", "sudamerica": "SOUTH_AMERICA",
    "latinamerika": "LATAM", "latinamerica": "LATAM", "amériquelatine": "LATAM", "ameriquelatine": "LATAM",
    "latinoamérica": "LATAM", "latinoamerica": "LATAM", "americalatina": "LATAM", "lateinamerika": "LATAM",
    "amerika": "AMERICAS", "americas": "AMERICAS", "amériques": "AMERICAS", "ameriques": "AMERICAS",
    "américas": "AMERICAS", "afrika": "AFRICA", "africa": "AFRICA", "afrique": "AFRICA", "mellanöstern": "MIDDLE_EAST",
    "mellanostern": "MIDDLE_EAST", "midtøsten": "MIDDLE_EAST", "midtosten": "MIDDLE_EAST", "middleeast": "MIDDLE_EAST",
    "oceanien": "OCEANIA", "oceania": "OCEANIA", "océanie": "OCEANIA", "oceanie": "OCEANIA",
    "asienochstillahavsregionen": "ASIA_PACIFIC", "asiapacific": "ASIA_PACIFIC", "apac": "ASIA_PACIFIC",
    "asiastillahavet": "ASIA_PACIFIC", "asiaogstillehavet": "ASIA_PACIFIC", "asiepacifique": "ASIA_PACIFIC",
    "asiapacífico": "ASIA_PACIFIC", "asiapacifico": "ASIA_PACIFIC", "emea": "EMEA", "europamellanösternochafrika": "EMEA",
    "östeuropa": "EASTERN_EUROPE", "osteuropa": "EASTERN_EUROPE", "easterneurope": "EASTERN_EUROPE",
    "västeuropa": "WESTERN_EUROPE", "vasteuropa": "WESTERN_EUROPE", "westerneurope": "WESTERN_EUROPE",
    "fjärranöstern": "EAST_ASIA", "fjarranostern": "EAST_ASIA", "kinainklhongkong": "CHINA_HK", "storkina": "GREATER_CHINA",
}
NORDIC_ALIASES = {  # Norwegian / Danish / Finnish / Icelandic names not in Natural Earth's language set
    "norge": "NO", "noreg": "NO", "norja": "NO", "sverige": "SE", "ruotsi": "SE", "danmark": "DK", "tanska": "DK",
    "suomi": "FI", "finland": "FI", "island": "IS", "islanti": "IS", "tyskland": "DE", "saksa": "DE",
    "frankrike": "FR", "frankrig": "FR", "ranska": "FR", "storbritannia": "GB", "storbritannien": "GB", "britannia": "GB",
    "isobritannia": "GB", "englanti": "GB", "nederland": "NL", "nederlandene": "NL", "nederländerna": "NL",
    "alankomaat": "NL", "spania": "ES", "spanien": "ES", "espanja": "ES", "italia": "IT", "italien": "IT",
    "polen": "PL", "puola": "PL", "usa": "US", "usa og canada": "US", "yhdysvallat": "US", "forentedestater": "US",
    "kina": "CN", "kiina": "CN", "japani": "JP", "intia": "IN", "venaja": "RU", "venäjä": "RU", "russland": "RU",
    "ryssland": "RU", "rusland": "RU", "baltikum": "EE", "estland": "EE", "viro": "EE", "lettland": "LV", "latvia": "LV",
    "litauen": "LT", "liettua": "LT", "belgia": "BE", "belgien": "BE", "sveits": "CH", "schweiz": "CH", "sveitsi": "CH",
    "østerrike": "AT", "osterrike": "AT", "østrig": "AT", "ostrig": "AT", "itävalta": "AT", "irland": "IE", "irlanti": "IE",
    "brasil": "BR", "brasilien": "BR", "brasilia": "BR", "australia": "AU", "australien": "AU", "kanada": "CA",
    "canada": "CA", "sydkorea": "KR", "korea": "KR", "etelakorea": "KR", "singapore": "SG", "singapur": "SG",
    "mexiko": "MX", "meksiko": "MX", "tjeckien": "CZ", "tsjekkia": "CZ", "tjekkiet": "CZ", "tsekki": "CZ",
    "turkiet": "TR", "tyrkia": "TR", "tyrkiet": "TR", "turkki": "TR", "sydafrika": "ZA", "sorafrika": "ZA",
    "uk": "GB", "storbritannienochirland": "GB", "storbritannienirland": "GB", "usaochkanada": "US",
}
BLOCK_SHORT = {b: b for b in BLOCKS}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"\(.*?\)|\d+\)|[\*†‡¹²³⁴⁵]", "", s)
    return re.sub(r"[^a-zà-ÿøåæœß]", "", s)


def _ascii(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def multilang_names(zip_path: str = "") -> dict[str, str]:
    """normalized country name (de / es / fr / it / nl / pl / pt / sv / en + Nordic aliases) -> ISO2."""
    out: dict[str, str] = {}
    try:
        import pyogrio  # noqa: PLC0415

        cols = ["ISO_A2_EH", "NAME", "NAME_EN", "NAME_DE", "NAME_ES", "NAME_FR", "NAME_IT", "NAME_NL", "NAME_PL",
                "NAME_PT", "NAME_SV"]
        df = pyogrio.read_dataframe(f"zip://{zip_path}", read_geometry=False, columns=cols)
        for rec in df.to_dict("records"):
            iso = rec.get("ISO_A2_EH")
            if not isinstance(iso, str) or len(iso) != 2 or iso == "-9":
                continue
            for c in cols[1:]:
                v = rec.get(c)
                if isinstance(v, str) and v:
                    for k in {_norm(v), _norm(_ascii(v))}:
                        if len(k) >= 3:
                            out.setdefault(k, iso)
    except Exception as e:  # noqa: BLE001 - Natural Earth zip not landed: aliases only
        log.info("esef_geo: multilingual names unavailable (%s)", e)
    for k, v in NORDIC_ALIASES.items():
        out[_norm(k)] = v
    return out


def classify_label(label: str, company_country: str | None, names: dict[str, str],
                   settings: Settings | None = None) -> tuple[str, str | None, str | None]:
    """Row label -> (member_type, iso2, region) or ('other', None, None)."""
    from app.linkage.members import classify  # noqa: PLC0415

    k = _norm(label)
    ka = _norm(_ascii(label))
    if not k:
        return "other", None, None
    m = re.match(r"^(?:other|ovrig[a-z]*|ovrige|rest(?:of)?|autres?|otros?|altri|overige?|sonstige?|muut|outros?)"
                 r"(?:the)?([a-z]+)$", ka)
    if m and m.group(1) in REGION_WORDS:          # "Other Europe": the region, net of separately reported countries
        return "region", None, REGION_WORDS[m.group(1)]
    if k in OTHER_WORDS or ka in OTHER_WORDS or OTHER_RX.match(ka):
        return "rest_of_world", None, None
    if k in names or ka in names:
        return "country", names.get(k) or names.get(ka), None
    if k in REGION_WORDS or ka in REGION_WORDS:
        return "region", None, REGION_WORDS.get(k) or REGION_WORDS.get(ka)
    if k in ("domestic", "inland", "innenlands", "hemmamarknad", "national", "nacional", "nazionale", "binnenland"):
        return "domestic", company_country, None
    t, i, r = classify(_ascii(label), company_country, settings)
    return t, i, r


OTHER_RX = re.compile(r"^(?:other|others|ovrig\w*|ovrige?|rest|restof|autres?|otros?|altri|overige?|sonstige?|muut|"
                      r"outros?|andre|andra)(?:the)?(?:countries|country|regions|region|markets|market|world|areas|"
                      r"area|europe|asia|americas|lander|land|landen|pays|paises|paesi|maat|welt|mundo|monde|mondo|"
                      r"wereld|verden|varlden|marknader|markeder|international|internationalt)?$")
_NUM = re.compile(r"^\(?-?\s*[\d][\d\s.,'’ ]*\)?$")


def parse_number(s: str) -> float | None:
    """'2 435' / '1,234.5' / '1.234,5' / '(12)' / '–' -> float."""
    t = (s or "").replace(" ", " ").replace(" ", " ").strip()
    t = t.replace("−", "-").replace("–", "-").replace("—", "-")
    if t in ("", "-", "–"):
        return None
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = t.strip("()- ")
    if not _NUM.match(t):
        return None
    t = t.replace(" ", "").replace("'", "").replace("’", "")
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".") if t.rfind(",") > t.rfind(".") else t.replace(",", "")
    elif "," in t:
        parts = t.split(",")
        t = t.replace(",", "") if all(len(p) == 3 for p in parts[1:]) else t.replace(",", ".")
    elif t.count(".") > 1:
        t = t.replace(".", "")
    elif "." in t and len(t.split(".")[1]) == 3 and len(t.split(".")[0]) <= 3:
        t = t.replace(".", "")     # 1.234 = thousands separator (continental)
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _cells(tr) -> list[str]:
    out = []
    for td in tr:
        if not isinstance(td.tag, str):
            continue
        tag = td.tag.split("}")[-1].lower()
        if tag not in ("td", "th"):
            continue
        txt = re.sub(r"\s+", " ", "".join(td.itertext())).strip()
        span = int(td.get("colspan") or 1) if str(td.get("colspan") or "1").isdigit() else 1
        out.append(txt)
        out.extend([""] * (min(span, 8) - 1))
    return out


def _context(table, limit: int = 400) -> str:
    """Text of up to 3 preceding siblings (title / caption paragraphs)."""
    parts = []
    el = table
    for _ in range(3):
        el = el.getprevious()
        if el is None:
            break
        parts.append(re.sub(r"\s+", " ", "".join(el.itertext())).strip())
    return " ".join(reversed(parts))[-limit:]


def parse_table(rows: list[list[str]], context: str, fy: int | None, company_country: str | None, names: dict[str, str],
                settings: Settings | None = None) -> dict[str, Any] | None:
    """Rows of cell texts -> {rows: [(label, value, member_type, iso2, region)], total, reconciled} or None."""
    data, head = [], []
    for r in rows:
        nums = [parse_number(c) for c in r[1:]]
        lab = r[0].strip() if r else ""
        vals = [(c, v) for c, v in zip(r[1:], nums) if v is not None]
        if vals and all(re.fullmatch(r"(?:FY\s?)?(?:19|20)\d\d(?:/\d\d)?", c.strip()) for c, _ in vals):
            head.append(" ".join(c for c in r if c))     # a header row of years ("Operating revenues | 2023 | 2022")
            continue
        if lab and any(v is not None for v in nums) and parse_number(lab) is None:
            data.append((lab, nums))
        elif not data:
            head.append(" ".join(c for c in r if c))
        elif not lab or parse_number(lab) is not None:
            data.append(("__total__", [parse_number(c) for c in r] if parse_number(lab) is not None else nums))
    if len(data) < 2:
        return None
    htxt = " ".join(head)
    ctx = f"{context} {htxt}"
    rev, ast = bool(REV_WORDS.search(ctx)), bool(ASSET_WORDS.search(ctx))
    if not rev and not (htxt and not ast and REV_WORDS.search(htxt)):
        return None
    # value column: header row cells aligned on the data columns
    col = None
    header_rows = [r for r in rows if r and any(re.fullmatch(r"(?:FY\s?)?(?:19|20)\d\d(?:/\d\d)?", c.strip()) for c in r[1:])][:3]
    for hr in header_rows:
        cells = hr[1:]
        for j, c in enumerate(cells):
            if fy and str(fy) in c:
                if rev and ast:   # revenue and assets side by side: the first fy column after a revenue label
                    lab_before = " ".join(cells[:j + 1])
                    if not REV_WORDS.search(lab_before) and ASSET_WORDS.search(lab_before):
                        continue
                col = j
                break
        if col is not None:
            break
    if rev and ast and col is None:
        return None
    mapped, unmapped, total = [], 0, None
    for lab, nums in data:
        vals = [v for v in nums if v is not None]
        v = nums[col] if col is not None and col < len(nums) else (vals[0] if vals else None)
        if v is None and vals:
            v = vals[0]
        if v is None:
            continue
        if lab == "__total__" or TOTAL_WORDS.match(lab.strip()):
            total = v if total is None else total
            continue
        t, iso, reg = classify_label(lab, company_country, names, settings)
        if t == "other":
            unmapped += 1
            continue
        mapped.append((lab, v, t, iso, reg))
    geo = [m for m in mapped if m[2] in ("country", "region", "domestic")]
    if len(geo) < 2 or len(mapped) < 0.6 * (len(mapped) + unmapped):
        return None
    s = sum(m[1] for m in mapped)
    if s <= 0 or any(m[1] < 0 for m in mapped):
        return None
    rec = total is not None and total > 0 and abs(total - s) <= 0.03 * total
    return {"rows": mapped, "total": total if rec else s, "reconciled": rec}


def parse_package(path: Path, fy: int | None, company_country: str | None, names: dict[str, str],
                  settings: Settings | None = None) -> list[dict[str, Any]]:
    """ESEF package -> parsed geographic revenue tables [{block, rows, total, reconciled}] (first qualifying table)."""
    from lxml import etree  # noqa: PLC0415

    with zipfile.ZipFile(path) as zf:
        cand = sorted([x for x in zf.namelist() if x.lower().endswith((".xhtml", ".html", ".htm"))],
                      key=lambda x: -zf.getinfo(x).file_size)
        if not cand or zf.getinfo(cand[0]).file_size > MAX_XHTML:
            return []
        b = zf.read(cand[0])
    if b"ix:nonNumeric" not in b or not any(k.encode() in b for k in BLOCKS):
        return []
    root = etree.fromstring(b, parser=etree.XMLParser(recover=True, huge_tree=True))
    del b
    conts = {c.get("id"): c for c in root.iter(f"{IX}continuation")}
    out = []
    blocks = [e for e in root.iter(f"{IX}nonNumeric") if (e.get("name") or "").split(":")[-1] in BLOCKS]
    blocks.sort(key=lambda e: BLOCKS.index((e.get("name") or "").split(":")[-1]))
    for e in blocks:
        parts = [e]
        nxt = e.get("continuedAt")
        while nxt and nxt in conts and len(parts) < 200:
            parts.append(conts[nxt])
            nxt = conts[nxt].get("continuedAt")
        for pp in parts:
            for t in pp.iter(f"{XH}table", "table"):
                rows = [c for c in (_cells(tr) for tr in t.iter(f"{XH}tr", "tr")) if any(x for x in c)]
                rows = [[c for i, c in enumerate(r) if c or i == 0] for r in rows]
                ctx = _context(t)
                for k, sec in enumerate(split_sections(rows)):
                    res = parse_table(sec, ctx if k == 0 else "", fy, company_country, names, settings)
                    if res:
                        res["block"] = (e.get("name") or "").split(":")[-1]
                        out.append(res)
                        return out     # first qualifying table of the most specific block
    return out


_YEAR_CELL = re.compile(r"(?:FY\s?)?(?:19|20)\d\d(?:/\d\d)?(?:-\d\d-\d\d)?")


def _is_year_header(r: list[str]) -> bool:
    cells = [c.strip() for c in r[1:] if c.strip()]
    return bool(cells) and all(_YEAR_CELL.fullmatch(c) for c in cells)


def split_sections(rows: list[list[str]]) -> list[list[list[str]]]:
    """One HTML table holding several sub-tables (revenue by market, then assets by market): a year-header row
    after data rows starts a new section (the title row just before it moves with it)."""
    out: list[list[list[str]]] = [[]]
    seen_data = False
    for r in rows:
        if _is_year_header(r) and seen_data:
            prev = out[-1]
            carry = [prev.pop()] if prev and not any(parse_number(c) is not None for c in prev[-1][1:]) else []
            out.append(carry + [r])
            seen_data = False
            continue
        out[-1].append(r)
        if r and r[0].strip() and any(parse_number(c) is not None for c in r[1:]) and not _is_year_header(r):
            seen_data = True
    return [x for x in out if x]


# ================================================================================================ landing
def _filings(settings: Settings) -> dict[tuple[str, str], dict[str, Any]]:
    from app.core.storage import read_table  # noqa: PLC0415

    fi = read_table("filings_index", columns=["source_id", "filing_id", "source_entity_id", "issuer_id", "form_type",
                                              "period_end", "knowledge_date"],
                    filters=pl.col("source_id").is_in(list(SOURCES)), settings=settings)
    out = {}
    for r in fi.iter_rows(named=True):
        native = r["filing_id"].split(":", 1)[1]
        out[(r["source_id"], native.replace(":", "_"))] = r
    return out


def _currency_country(settings: Settings) -> tuple[dict[str, str], dict[str, str]]:
    from app.core.storage import read_table  # noqa: PLC0415

    try:
        iss = read_table("issuers", columns=["issuer_id", "country", "reporting_currency"], settings=settings)
    except Exception:  # noqa: BLE001
        return {}, {}
    return ({a: b for a, b, _ in iss.iter_rows() if b}, {a: c for a, _, c in iss.iter_rows() if c})


def land(settings: Settings | None = None, max_packages: int = 100000, redo: bool = False) -> dict[str, Any]:
    s = settings_of(settings)
    names = multilang_names(str(raw_dir("natural_earth", s) / "ne_10m_admin_0_countries.zip"))
    fil = _filings(s)
    country, ccy = _currency_country(s)
    st = load_state(STATE, s)
    done = {} if redo else dict(st.get("done", {}))
    out_d = raw_dir("esef_geo", s)
    rows: list[dict[str, Any]] = []
    rep = {"packages": 0, "tables": 0, "rows": 0, "unmatched_filing": 0, "errors": 0}
    n = 0
    for src in SOURCES:
        pk = sorted((s.raw_dir / src / "packages").rglob("*.zip")) + sorted((s.raw_dir / src / "packages").rglob("*.xbri"))
        for p in pk:
            key = f"{src}/{p.name}"
            if key in done:
                continue
            if n >= max_packages:
                break
            n += 1
            f = fil.get((src, p.stem))
            if f is None or (f["form_type"] or "").upper() not in ANNUAL_FORMS or f["period_end"] is None:
                done[key] = -1
                rep["unmatched_filing"] += f is None
                continue
            iid = f["issuer_id"]
            cc = country.get(iid) if iid else None
            try:
                res = parse_package(p, f["period_end"].year, cc, names, s)
            except Exception as e:  # noqa: BLE001 - corrupt package / parser failure: skip
                rep["errors"] += 1
                done[key] = -2
                log.info("esef_geo %s: %s", key, e)
                continue
            rep["packages"] += 1
            k = 0
            for t in res:
                rep["tables"] += 1
                nc = sum(1 for r in t["rows"] if r[2] == "country")
                for lab, v, typ, iso, reg in t["rows"]:
                    rows.append({
                        "source_id": "esef_textblock", "entity_id": f["source_entity_id"] or f["filing_id"],
                        "issuer_id": iid, "name": None, "filing_id": f["filing_id"], "form_type": f["form_type"],
                        "measure": "revenue", "concept": t["block"],
                        "period_start": date(f["period_end"].year - 1, f["period_end"].month, 1) if f["period_end"] else None,
                        "period_end": f["period_end"], "qtrs": 4, "fiscal_period": "FY", "member_raw": lab[:120],
                        "member_type": typ, "iso2": iso if typ in ("country", "domestic") else None, "region": reg,
                        "value": float(v), "currency": (ccy.get(iid) if iid else None) or "XXX",
                        "denominator": float(t["total"]), "share": float(v) / float(t["total"]) if t["total"] else None,
                        "n_countries": nc, "company_country": cc, "knowledge_date": f["knowledge_date"],
                        "method": "ixbrl_textblock_table", "confidence": 0.8 if t["reconciled"] else 0.7})
                    k += 1
            rep["rows"] += k
            done[key] = k
            if n % 50 == 0:
                _flush(rows, out_d, done, s)
                rows = []
    _flush(rows, out_d, done, s)
    return rep


def _flush(rows: list[dict[str, Any]], out_d: Path, done: dict[str, int], s: Settings) -> None:
    if rows:
        df = pl.DataFrame(rows, infer_schema_length=None).unique(
            ["entity_id", "filing_id", "member_raw"], keep="first")
        df.write_parquet(out_d / f"rows_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}.parquet")
    st = load_state(STATE, s)
    st["done"] = done
    st["updated"] = now_utc().isoformat(timespec="seconds")
    save_state(STATE, st, s)


def load_rows(settings: Settings | None = None) -> pl.DataFrame:
    """Landed text-block rows in company_geo_revenue shape (latest landing per filing / member wins)."""
    files = sorted(raw_dir("esef_geo", settings).glob("rows_*.parquet"))
    if not files:
        return pl.DataFrame()
    df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    return df.reverse().unique(["source_id", "entity_id", "filing_id", "member_raw"], keep="first").with_columns(
        pl.lit(now_utc()).alias("ingested_at"), pl.col("qtrs").cast(pl.Int16), pl.col("n_countries").cast(pl.Int32))


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = land(ctx.settings, max_packages=int(p.get("max_packages", 100000)),
               redo=str(p.get("redo", "false")).lower() == "true")
    return JobResult(status="success", rows_written={}, message=str(rep))


if __name__ == "__main__":  # python -m app.linkage.esef_geo [max_packages] [redo]
    import sys

    a = sys.argv[1:]
    print(land(max_packages=int(a[0]) if a else 100000, redo=len(a) > 1 and a[1] == "redo"))
