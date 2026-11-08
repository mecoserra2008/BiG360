"""Roadmap #9 link extraction v2: rule-based (no LLM) customer / supplier mentions in 10-K / 20-F / 40-F text.

Pipeline (all pure functions, bounded memory: one filing at a time):
  1. candidate_units(text)  flattened filing text (app.ingestion.sec_text.html_to_text: one block per line, table cells
                            separated by tabs) -> short "units": sentences that contain a customer / supplier / revenue
                            keyword, and table rows inside a customer-concentration table ("[T] Walmart Inc. \t 21 % ...").
                            Only these units are kept on disk (raw/linkage/text_units), never the full HTML.
  2. extract_links(units)   units -> Mention(name, share, link_type, rule, rule_conf, metric, snippet)
       link_type customer   the filer sells to `name` (name = customer)
                 supplier   the filer buys from `name` (name = supplier; a "reverse" edge seen from the customer)
       share                fraction of the filer's revenue (customer) / purchases or cost of sales (supplier), else None
       rule / rule_conf     which pattern matched and its prior precision (see RULES); the stored link confidence is
                            rule_conf x the name -> issuer match confidence (app.linkage.customers.Linker)
       snippet              <= 200 chars of the sentence around the match (provenance)
  Names must look like proper names (capitalized tokens, optional legal suffix) and pass the generic-name filter of
  app.linkage.customers (no "our largest customer", markets, countries, segments ...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULES_VERSION = "v2.1"
SNIPPET_CHARS = 200

#: rule id -> (description, prior precision used as rule confidence)
RULES: dict[str, tuple[str, float]] = {
    "c_sales_to_pct": ("Sales / revenue to NAME ... accounted for X% of revenue", 0.95),
    "c_name_pct": ("NAME accounted for / represented X% of revenue", 0.9),
    "c_pct_from_name": ("X% of revenue was derived from / attributable to NAME", 0.9),
    "c_respectively": ("NAME1 and NAME2 accounted for X% and Y% ... respectively", 0.85),
    "c_paren_pct": ("customer list with percentages: NAME (X%)", 0.8),
    "c_largest": ("our largest / principal customer(s), NAME", 0.75),
    "c_include": ("our customers include NAME, NAME and NAME", 0.55),
    "c_table": ("customer-concentration table row: NAME | X%", 0.65),
    "s_purchases_pct": ("purchases from NAME accounted for X% of purchases / cost of sales", 0.9),
    "s_pct_from_name": ("X% of purchases / inventory were from NAME", 0.85),
    "s_sole": ("NAME is our sole / single / principal / largest supplier / manufacturer", 0.8),
    "s_rely": ("we rely / depend on NAME to supply / manufacture ...", 0.75),
    "s_purchase_all": ("we purchase substantially all / most of our X from NAME", 0.8),
    "s_made_by": ("manufactured for us by NAME / NAME manufactures substantially all of our", 0.75),
    "s_include": ("our suppliers / contract manufacturers include NAME, NAME", 0.55),
}

SUFFIX = (r"(?:Inc|Ltd|Limited|Co|Corp|Corporation|LLC|L\.L\.C|plc|PLC|N\.V|NV|S\.A|SA|AG|SE|L\.P|LP|GmbH|Company|"
          r"Holdings|Group|K\.K|S\.p\.A|SpA|ASA|AB|A/S|Oyj|B\.V|BV|S\.A\.S|Pty|Bhd|Berhad|Co\.,\s?Ltd)\.?")
_TOK = r"[A-Z][\w&.'’\-]*+"           # possessive: no backtracking inside a token (no catastrophic regex cost)
_CONN = r"(?:of|and|&|de|du|la|del|der|for|the)"
NAME = (r"(?P<name>" + _TOK + r"(?:\s+(?:" + _TOK + r"|" + _CONN + r"(?=\s+[A-Z]))){0,6}"
        r"(?:,?\s+" + SUFFIX + r")*)")
NAME_ONLY = re.compile(r"^(?:[Tt]he\s+)?" + NAME.replace("(?P<name>", "(?:") + r"$")
PCT = r"(?P<pct>\d{1,2}(?:\.\d+)?|100)\s?(?:%|percent\b)"
APPROX = r"(?:approximately\s+|about\s+|roughly\s+|nearly\s+|over\s+|more\s+than\s+|less\s+than\s+|almost\s+|around\s+)?"
_DET = (r"(?:our\s+|the\s+company['’]s\s+|its\s+|[A-Z][\w]*['’]s\s+|total\s+|consolidated\s+|net\s+|gross\s+|annual\s+|"
        r"combined\s+|the\s+|worldwide\s+|global\s+|fiscal\s+(?:year\s+)?(?:\d{4}\s+)?|\d{4}\s+)*")
REV = r"(?:net\s+)?(?:revenue|revenues|sales|net\s+sales|operating\s+revenues|turnover|product\s+sales|product\s+revenues?)"
OF_REV = r"\s+of\s+" + _DET + REV
PURCH = (r"(?:purchases|inventory\s+purchases|total\s+purchases|raw\s+materials?|materials?\s+purchases|cost\s+of\s+"
         r"(?:sales|goods\s+sold|revenues?|products\s+sold)|inventory|supplies|components)")
OF_PURCH = r"\s+of\s+" + _DET + PURCH
VERB = (r"(?:totaled|totalled|accounted\s+for|represented|comprised|constituted|amounted\s+to|made\s+up|contributed|"
        r"generated|were|was|equaled|equalled)")
PAREN = r"(?:\s*\([^)]{1,80}\))?"
#: further percentages of the same name for earlier years ("21%, 19% and 18% of net sales in 2025, 2024 and 2023")
PCT_TAIL = r"(?:(?:,\s*|\s+and\s+|,\s*and\s+)\d{1,2}(?:\.\d+)?\s?%){0,4}(?:\s*,\s*respectively,?)?"
#: a name list up to the end of the sentence; periods allowed inside (Inc., N.V.) unless a new sentence starts
LIST = r"(?P<list>(?:[^.;:]|\.(?!\s+[A-Z][a-z]|\s*$)){3,400})"
_I = re.IGNORECASE

KEYWORDS = re.compile(r"customer|client|suppl|vendor|manufactur|sole[\s-]source|single[\s-]source|purchas|"
                      r"sales\s+to|revenues?\s+from|derived|foundr|"
                      r"distributor|foundr|contract\s+manufactur|%\s+of|percent\s+of|respectively", _I)
CUST_CTX = re.compile(r"customer|client|sales\s+to|revenue[s]?\s+from|net\s+sales|of\s+(?:our\s+)?(?:total\s+)?revenue", _I)
SUPP_CTX = re.compile(r"supplier|vendor|manufactur|purchas|foundr|source", _I)
ABBREV = re.compile(r"\b(Inc|Corp|Co|Ltd|No|Nos|Mr|Ms|Dr|St|Jr|Sr|vs|approx|U\.S|U\.K|N\.V|S\.A|L\.P|B\.V|K\.K|"
                    r"S\.p\.A|e\.g|i\.e|Fig|Inc\.,)\.", re.I)


@dataclass
class Mention:
    name: str
    share: float | None
    link_type: str           # customer | supplier
    rule: str
    rule_conf: float
    metric: str | None       # revenue | purchases | None
    snippet: str
    strict: bool = False     # keep only when the name resolves exactly to one of our issuers (list / weak-context rules)


# ================================================================================================ units
def split_sentences(line: str) -> list[str]:
    """Sentence split that keeps legal abbreviations (Inc., Corp., U.S.) inside the sentence."""
    protected = ABBREV.sub(lambda m: m.group(0).replace(".", "\x00"), line)
    parts = re.split(r"(?<=[.;!?])\s+(?=[A-Z(\"“])", protected)
    return [p.replace("\x00", ".").strip() for p in parts if p.strip()]


def candidate_units(text: str, max_unit_chars: int = 1500, max_units: int = 4000) -> list[str]:
    """Filing text -> keyword sentences + customer-table rows ("[T] ..."). Bounded output.

    A short line mentioning customers (a table title / header such as "Major customers" or "Customer | % of net
    sales") opens a 12-line table window; tab-separated rows inside the window that carry a percentage (or whose
    window header mentions % / percent) become "[T]" units."""
    out: list[str] = []
    seen: set[str] = set()
    table_ctx, ctx_pct = 0, False
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        cells = [c.strip() for c in line.split("\t") if c.strip()] if "\t" in line else []
        joined = " ".join(cells) if cells else line
        if len(joined) < 200 and re.search(r"customer", joined, _I):
            table_ctx, ctx_pct = 12, bool(re.search(r"%|percent", joined, _I))
        elif len(joined) < 200 and table_ctx > 0 and re.search(r"%\s+of|percent", joined, _I):
            ctx_pct = True
        elif cells and table_ctx > 0:
            table_ctx -= 1
            if re.search(r"\d", joined) and len(joined) < 300 and ("%" in joined or ctx_pct):
                u = "[T] " + "\t".join(cells)
                if u not in seen:
                    seen.add(u)
                    out.append(u)
            continue
        elif len(joined) > 200:
            table_ctx = 0
        line = joined
        if len(line) > 20000:
            continue
        if not KEYWORDS.search(line):
            continue
        for s in split_sentences(line):
            if len(s) > max_unit_chars or len(s) < 25 or not KEYWORDS.search(s) or not re.search(r"[A-Z]", s[1:]):
                continue
            if s not in seen:
                seen.add(s)
                out.append(s)
        if len(out) >= max_units:
            break
    return out


# ================================================================================================ patterns
def _c(p: str) -> re.Pattern[str]:
    return re.compile(p)


P_SALES_TO = _c(r"(?:[Ss]ales|[Rr]evenues?|[Nn]et\s+sales|[Ss]hipments)\s+(?:to|from)\s+" + NAME + PAREN
                + r"(?:,[^,.]{0,80},)?\s+(?:\w+\s+){0,3}?" + VERB + r"\s+" + APPROX + PCT + PCT_TAIL + OF_REV)
P_NAME_PCT = _c(NAME + PAREN + r",?\s+(?:(?:our|its|the\s+Company['’]s)\s+(?:largest|biggest|principal|major|"
                r"most\s+significant)\s+customer,?\s+|which\s+is\s+our\s+largest\s+customer,\s+)?(?:which\s+)?"
                r"(?:accounted\s+for|represented|comprised|constituted|contributed)\s+" + APPROX + PCT
                + PCT_TAIL + OF_REV)
P_PCT_FROM = _c(APPROX + PCT + OF_REV + r"(?:\s+(?:in|for|during)\s+[^,.]{1,40})?,?\s+(?:was|were|is|are)\s+"
                r"(?:derived\s+|generated\s+|attributable\s+|attributed\s+|earned\s+)?(?:from|to)\s+(?:sales\s+to\s+|"
                r"(?:our\s+)?(?:contracts|agreements|arrangements)\s+with\s+)?" + NAME)
P_DERIVED = _c(r"\b(?:we|We|the\s+Company)\s+(?:derived|generated|earned|received)\s+" + APPROX + PCT + OF_REV
               + r"\s+from\s+(?:sales\s+to\s+)?" + NAME)
P_LARGEST = _c(r"(?:[Oo]ur|[Ii]ts|[Tt]he\s+Company['’]s)\s+(?:largest|biggest|principal|most\s+significant|single\s+"
               r"largest|two\s+largest|three\s+largest|primary|top|key|major)\s+(?:end\s+)?customers?\s*(?:is|are|was|"
               r"were|include|includes|,)\s+" + LIST + r"")
P_INCLUDE_C = _c(r"(?:[Oo]ur|[Ii]ts)\s+(?:[a-z\-]+\s+){0,3}customers\s+(?:include|includes|included|have\s+included|"
                 r"consist\s+of|are)\s+(?:companies\s+such\s+as\s+|among\s+others,?\s+)?" + LIST + r"")
P_CUST_SUCHAS = _c(r"customers\s*,?\s+(?:such\s+as|including)\s+" + LIST + r"")
P_PAREN_PCT = _c(NAME + r"\s*\(\s*" + PCT + r"\s*\)")
P_RESPECT = _c(r"(?P<names>(?:" + _TOK + r"[\w&.,'’\-\s]{0,120}?))\s+(?:accounted\s+for|represented|comprised)\s+"
               + APPROX + r"(?P<pcts>\d{1,2}(?:\.\d+)?\s?%(?:\s*(?:,|and|,\s*and)\s*\d{1,2}(?:\.\d+)?\s?%)+)"
               r"[^.]{0,80}?" + OF_REV + r"[^.]{0,120}?respectively")

S_PURCH_PCT = _c(r"(?:[Pp]urchases|[Pp]urchased\s+(?:materials|products|inventory)|[Ii]nventory\s+purchases|[Pp]ayments)"
                 r"\s+from\s+" + NAME + PAREN + r",?\s+(?:\w+\s+){0,3}?" + VERB + r"\s+" + APPROX + PCT
                 + PCT_TAIL + OF_PURCH)
S_NAME_SUPPLIED = _c(NAME + PAREN + r",?\s+(?:supplied|provided|manufactured|produced)\s+" + APPROX + PCT + OF_PURCH)
S_PCT_FROM = _c(APPROX + PCT + OF_PURCH + r"(?:\s+(?:in|for|during)\s+[^,.]{1,40})?,?\s+(?:was|were|is|are|came)\s+"
                r"(?:purchased\s+|sourced\s+|acquired\s+|supplied\s+|obtained\s+)?(?:from|by)\s+" + NAME)
S_SOLE = _c(NAME + PAREN + r",?\s+(?:is|was|are|were|remains|has\s+been|serves\s+as|acts\s+as)\s+(?:currently\s+)?"
            r"(?:our|the\s+Company['’]s|its|the)\s+(?:sole|single|only|exclusive|primary|principal|largest|main|key|major|"
            r"predominant)\s+(?:source\s+of\s+supply|(?:third[\s-]party\s+|contract\s+|outside\s+)?(?:supplier|"
            r"manufacturer|vendor|source|foundry|provider|producer|distributor\s+and\s+supplier))")
S_RELY = _c(r"\b(?:[Ww]e|[Tt]he\s+Company)\s+(?:currently\s+)?(?:rely|relies|depend|depends)\s+(?:\w+\s+){0,2}?"
            r"(?:on|upon)\s+" + NAME + PAREN + r",?\s+(?:\w+\s+){0,3}?(?:to|for|as)\s+(?:\w+\s+){0,2}?"
            r"(?:supply|supplies|manufacture|manufacturing|produce|production|fabricat\w*|assembl\w*|provide\s+"
            r"(?:us\s+with\s+)?(?:\w+\s+){0,3}?(?:components|chips|wafers|materials|products|inventory)|"
            r"(?:our\s+|the\s+)?(?:sole\s+|single\s+|exclusive\s+)?(?:source|supplier|manufacturer|foundry))")
S_PURCH_ALL = _c(r"\b(?:[Ww]e|[Tt]he\s+Company)\s+(?:currently\s+)?(?:purchase|purchases|purchased|source|sources|"
                 r"obtain|obtains|buy|buys|acquire|acquires)\s+(?:substantially\s+all|all|most|the\s+majority|a\s+"
                 r"majority|a\s+significant\s+(?:portion|majority|amount)|a\s+substantial\s+(?:portion|majority)|the\s+"
                 r"bulk)\s+of\s+(?:our|its|the)\s+[^.;]{1,80}?\s+from\s+" + NAME)
S_MADE_BY = _c(r"(?:manufactured|produced|fabricated|assembled)\s+(?:for\s+us\s+|exclusively\s+|primarily\s+|"
               r"solely\s+)?by\s+" + NAME + PAREN + r"(?:,|\s+\(|\s+under|\s+pursuant|\s+in\s+|\s*\.)")
S_MAKES_ALL = _c(NAME + PAREN + r",?\s+(?:currently\s+)?(?:manufactures|produces|fabricates|assembles|supplies)\s+"
                 r"(?:substantially\s+all|all|most|the\s+majority|a\s+majority)\s+of\s+(?:our|the\s+Company['’]s)")
S_INCLUDE = _c(r"(?:[Oo]ur|[Ii]ts)\s+(?:[a-z\-]+\s+){0,3}(?:suppliers|vendors|contract\s+manufacturers|foundries|"
               r"manufacturing\s+partners|foundry\s+partners)\s+(?:include|includes|included|are|consist\s+of)\s+"
               r"" + LIST + r"")
S_SUCHAS = _c(r"(?:suppliers|contract\s+manufacturers|foundries|manufacturing\s+partners)\s*,?\s+(?:such\s+as|including)\s+"
              r"" + LIST + r"")
T_ROW = _c(r"^\[T\]\s+(?:Customer\s+)?" + NAME + r"\s*(?:\(\d\))?\s*\t\s*(?:\$?\s*[\d,.]+\s*\t\s*)?"
           r"(?P<pct>\d{1,2}(?:\.\d+)?|100)\s*%?(?:\s*\t|$)")

_LIST_SPLIT = re.compile(r"\s*,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+as\s+well\s+as\s+|\s*;\s*")
_TRAIL = re.compile(r"\s+(?:among\s+others|and\s+others|and\s+other\s+.*|as\s+well\s+as.*|which\s+.*|who\s+.*|that\s+.*|"
                    r"each\s+of\s+which.*|in\s+(?:the|our|fiscal).*|for\s+.*|to\s+.*|through\s+.*|with\s+.*)$", _I)
_STOP_NAME = re.compile(r"^(?:Customer|Customers|Company|Companies|Supplier|Suppliers|Vendor|Vendors|Manufacturer|"
                        r"Fiscal|Year|Years|Note|Notes|Item|Table|Total|Net|Gross|Revenue|Revenues|Sales|Percentage|"
                        r"Percent|December|January|February|March|April|May|June|July|August|September|October|"
                        r"November|Q[1-4]|FY|In|As|For|During|At|On|By|Of)\b")


def _clean(name: str) -> str:
    n = name.strip(" ,.;:'’\"()").replace("’", "'")
    n = re.sub(r"^(?:[Tt]he|[Ss]ales\s+to|[Rr]evenues?\s+from|[Pp]urchases\s+from|[Ss]ubsidiaries\s+of|[Aa]ffiliates\s+of)\s+",
               "", n)
    n = re.sub(r"\s+(?:and|of|&|the|for|de|la|du|del)$", "", n)
    n = re.sub(r"\s*\([^)]*\)$", "", n)
    return n.strip(" ,.;:")


def _snip(s: str, a: int, b: int) -> str:
    """<= SNIPPET_CHARS around [a, b) of s (cut at word boundaries)."""
    s2 = s.replace("\t", " | ")
    if len(s2) <= SNIPPET_CHARS:
        return s2
    mid_len = b - a
    if mid_len >= SNIPPET_CHARS:
        return s2[a:a + SNIPPET_CHARS - 1].rstrip() + "…"
    pad = (SNIPPET_CHARS - mid_len) // 2
    lo, hi = max(0, a - pad), min(len(s2), b + pad)
    if lo == 0:
        hi = min(len(s2), SNIPPET_CHARS)
    elif hi == len(s2):
        lo = max(0, len(s2) - SNIPPET_CHARS)
    out = ("…" if lo > 0 else "") + s2[lo:hi].strip() + ("…" if hi < len(s2) else "")
    return out if len(out) <= SNIPPET_CHARS else out[:SNIPPET_CHARS - 1].rstrip() + "…"


#: region / structure abbreviations that are never counterparties (they can collide with issuer aliases, e.g. "APAC")
REGION_TOKENS = {"APAC", "EMEA", "PRC", "OUS", "ROW", "LATAM", "US", "USA", "EU", "UK", "ASEAN", "MENA", "DACH", "CIS",
                 "ANZ", "APJ", "AMER", "AMERICAS", "EMEIA", "VIE", "VIES", "WFOE", "SAR", "GCC", "NA", "SEA", "HK", "ROC"}
#: product / segment / project names are not customers ("Demand Response Business", "Phase VI", "BIGO Ads")
SEGMENT_TAIL = re.compile(r"\b(?:business|businesses|segment|segments|division|divisions|markets?|products?|brands?|"
                          r"lines?|projects?|programs?|ads|advertising|services|platforms?|portfolio|operations|"
                          r"subsidiar(?:y|ies)|region|regions|area|areas)$|^(?:phase|project|program)\b", re.I)


def _ok_name(n: str) -> bool:
    from app.linkage.customers import is_generic  # noqa: PLC0415 - shared generic-name filter

    if len(n) < 2 or len(n) > 80 or _STOP_NAME.match(n) or not re.match(r"[A-Z0-9]", n):
        return False
    if len(n.split()) == 1 and len(n) <= 2:
        return False
    if re.search(r"\d{4}", n) or n.count(" ") > 7:
        return False
    if n.upper() in REGION_TOKENS or SEGMENT_TAIL.search(n):
        return False
    return not is_generic(n)


def _pct(v: str) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x / 100.0 if 1.0 <= x <= 100.0 else None


def _list_names(chunk: str) -> list[str]:
    chunk = re.sub(r"\([^)]*\)", "", chunk)
    chunk = _TRAIL.sub("", chunk.strip())
    out = []
    for part in _LIST_SPLIT.split(chunk):
        p = _clean(part)
        if p and NAME_ONLY.match(p) and _ok_name(p):
            out.append(p)
        if len(out) >= 12:
            break
    return out


COMPETE = re.compile(r"compet|rival|alternative\s+(?:products|solutions|vendors)", _I)
CUST_WORD = re.compile(r"customer|client|distributor|wholesaler|retailer|sales\s+to|licensee|reseller|purchaser|"
                       r"buyer|offtaker|off-taker|charterer|tenant|lessee|operator", _I)
OWN_PRODUCTS = re.compile(r"for\s+us|our\s+products?|our\s+(?:\w+\s+){0,2}(?:are|is)\s+(?:manufactured|produced)|"
                          r"substantially\s+all|all\s+of\s+our|on\s+our\s+behalf|contract\s+manufactur", _I)
NON_CUST = re.compile(r"product|brand|segment|franchise|region|countr|division|business\s+line|product\s+line|drug|"
                      r"therap|portfolio|geograph|state\s+of|province", _I)
_GEO_BEFORE = re.compile(r"\b(?:in|within|outside|from|of|across|throughout)\s*(?:the\s+)?$", _I)


#: cheap keyword checks run before the (costlier) NAME-led patterns
PREFILTER = {
    P_SALES_TO.pattern: re.compile(r"(?:sales|revenues?|shipments)\s+(?:to|from)\s+[A-Z]", _I),
    P_NAME_PCT.pattern: re.compile(r"accounted\s+for|represented|comprised|constituted|contributed", _I),
    P_PCT_FROM.pattern: re.compile(r"(?:derived|generated|attributable|attributed|earned)?\s*(?:from|to)\s", _I),
    P_DERIVED.pattern: re.compile(r"\b(?:derived|generated|earned|received)\s", _I),
    S_SOLE.pattern: re.compile(r"\b(?:sole|single|only|exclusive|primary|principal|largest|main|key|major|predominant)\s+"
                               r"(?:source|(?:third[\s-]party\s+|contract\s+|outside\s+)?(?:supplier|manufacturer|vendor|"
                               r"foundry|provider|producer|distributor))", _I),
    S_RELY.pattern: re.compile(r"\b(?:rely|relies|depend|depends)\b", _I),
    S_PURCH_ALL.pattern: re.compile(r"\b(?:purchase|purchases|purchased|source|sources|obtain|obtains|buy|buys|acquire|"
                                    r"acquires)\s+(?:substantially|all|most|the\s+majority|a\s+majority|a\s+significant|"
                                    r"a\s+substantial|the\s+bulk)", _I),
    S_MADE_BY.pattern: re.compile(r"(?:manufactured|produced|fabricated|assembled)\s+(?:\w+\s+){0,2}by\s+[A-Z]", _I),
    S_MAKES_ALL.pattern: re.compile(r"(?:manufactures|produces|fabricates|assembles|supplies)\s+(?:substantially\s+all|all|"
                                    r"most|the\s+majority|a\s+majority)\s+of", _I),
}


def extract_links(units: list[str]) -> list[Mention]:
    """Units of one filing -> de-duplicated mentions (highest-confidence rule per (link_type, name)).

    Precision guards: sentences about competition (compete / competitors) never yield list edges; percentage rules
    need the name not to follow a geographic preposition ("revenues generated in the Republic of Ireland represented
    2%"); weak-context matches are flagged strict (kept only when the name resolves exactly to one of our issuers)."""
    found: dict[tuple[str, str], Mention] = {}

    def add(name: str, share: float | None, lt: str, rule: str, metric: str | None, s: str, a: int, b: int,
            strict: bool = False, name_start: int | None = None) -> None:
        n = _clean(name)
        if not n or not _ok_name(n):
            return
        if share is not None and rule != "c_table" and re.search(r"\s(?:and|or)\s+[A-Z]", n):
            return  # "Apple and Samsung accounted for 25%": a combined share, not attributable to one name
        if name_start is not None and _GEO_BEFORE.search(s[max(0, name_start - 25):name_start]):
            return
        key = (lt, n.lower())
        conf = RULES[rule][1] * (0.85 if strict else 1.0)
        cur = found.get(key)
        if cur is not None and (cur.rule_conf > conf or (cur.rule_conf == conf and (cur.share or 0) >= (share or 0))):
            return
        found[key] = Mention(n, share, lt, rule, round(conf, 3), metric, _snip(s, a, b), strict)

    for s in units:
        if s.startswith("[T] "):
            m = T_ROW.match(s)
            if m and (sh := _pct(m.group("pct"))) is not None:
                add(m.group("name"), sh, "customer", "c_table", "revenue", s, 0, len(s), strict=True)
            continue
        cust = bool(CUST_CTX.search(s))
        supp = bool(SUPP_CTX.search(s))
        compete = bool(COMPETE.search(s))
        cword = bool(CUST_WORD.search(s))
        weak = not cword and bool(NON_CUST.search(s))   # product / segment / geography context: names must resolve
        if re.search(r"%|percent", s):
            for rx, rule in ((P_SALES_TO, "c_sales_to_pct"), (P_NAME_PCT, "c_name_pct"), (P_PCT_FROM, "c_pct_from_name"),
                             (P_DERIVED, "c_pct_from_name")):
                if not PREFILTER[rx.pattern].search(s):
                    continue
                for m in rx.finditer(s):
                    if (sh := _pct(m.group("pct"))) is not None:
                        add(m.group("name"), sh, "customer", rule, "revenue", s, m.start(), m.end(),
                            strict=not cword and not (rule == "c_sales_to_pct" and re.match(
                                r"(?:net\s+)?(?:sales|shipments)\s+to\b", s[m.start():m.start() + 20], _I)),
                            name_start=m.start("name") if rule == "c_name_pct" else None)
            if "respectively" in s:
                for m in P_RESPECT.finditer(s):
                    names = _list_names(m.group("names"))
                    pcts = [_pct(x) for x in re.findall(r"(\d{1,2}(?:\.\d+)?)\s?%", m.group("pcts"))]
                    if names and len(names) == len(pcts):
                        for n, sh in zip(names, pcts, strict=True):
                            add(n, sh, "customer", "c_respectively", "revenue", s, m.start(), m.end(), strict=weak)
            if cust:
                for m in P_PAREN_PCT.finditer(s):
                    if (sh := _pct(m.group("pct"))) is not None and sh <= 0.95:
                        add(m.group("name"), sh, "customer", "c_paren_pct", "revenue", s, m.start(), m.end(),
                            strict=not cword)
            if supp:
                for rx, rule in ((S_PURCH_PCT, "s_purchases_pct"), (S_NAME_SUPPLIED, "s_purchases_pct"),
                                 (S_PCT_FROM, "s_pct_from_name")):
                    for m in rx.finditer(s):
                        if (sh := _pct(m.group("pct"))) is not None:
                            add(m.group("name"), sh, "supplier", rule, "purchases", s, m.start(), m.end(),
                                name_start=m.start("name") if rx is S_NAME_SUPPLIED else None)
        if cust and not compete:
            for m in P_LARGEST.finditer(s):
                names = _list_names(m.group("list"))
                for n in names:
                    add(n, None, "customer", "c_largest", None, s, m.start(), m.end(), strict=len(names) > 2)
            for rx in (P_INCLUDE_C, P_CUST_SUCHAS):
                for m in rx.finditer(s):
                    for n in _list_names(m.group("list")):
                        add(n, None, "customer", "c_include", None, s, m.start(), m.end(), strict=True)
        if supp and not compete:
            for rx, rule in ((S_SOLE, "s_sole"), (S_RELY, "s_rely"), (S_PURCH_ALL, "s_purchase_all"),
                             (S_MADE_BY, "s_made_by"), (S_MAKES_ALL, "s_made_by")):
                if rule == "s_made_by" and not OWN_PRODUCTS.search(s):
                    continue
                if not PREFILTER[rx.pattern].search(s):
                    continue
                for m in rx.finditer(s):
                    add(m.group("name"), None, "supplier", rule, None, s, m.start(), m.end(), strict=rule in ("s_sole", "s_made_by"))
            for rx in (S_INCLUDE, S_SUCHAS):
                for m in rx.finditer(s):
                    for n in _list_names(m.group("list")):
                        add(n, None, "supplier", "s_include", None, s, m.start(), m.end(), strict=True)
    return list(found.values())
