"""A6 company -> company links (job `linkage_links`) -> company_links.

1. xbrl_fsds   SEC FSDS facts with a MajorCustomers= axis member (landed by app.linkage.georev): revenue by named
               customer (share = value / reported total revenue) and ConcentrationRiskPercentage1 with a customer
               member and a revenue benchmark (share = the percentage). Generic members (CustomerA, OneCustomer,
               SignificantCustomers, OtherCustomers, ...) are dropped; "...Government..." members -> dst_kind government.
2. text_10k    rule-based extraction (no LLM) from the 10-K / 20-F full text landed by sec_text: sentences like
               "Sales to MMC totaled 17% of consolidated revenues", "Walmart Inc. ("Walmart") accounted for 15% of our
               net sales". Named counterparties only; percentages 1-100.
   Customer names are resolved to issuers with app.nlp.gdelt_link.OrgLinker (exact / fuzzy alias match; ambiguous
   multi-issuer aliases stay unresolved, dst_issuer_id null, dst_key 'name:<normalized>').
3. gdelt       co-mention edges from events_v2 gdelt_article_labels: issuer pairs linked in the same article
               (articles with 2-6 linked issuers), monthly counts >= MIN_COMENTIONS; weight = n_ij / sqrt(n_i n_j).
Roadmap #9 (v2): text links come from app.linkage.textrules over the compact unit store (app.linkage.textunits):
   customer and supplier patterns, reverse (supplier-disclosed) edges as link_type supplier, provenance columns (rule,
   snippet <= 200 chars, form_type), confidence = rule prior x name match; weak-context matches need an exact issuer
   match. Co-mentions are computed one year_month partition at a time (bounded memory).
PIT: knowledge_date = filing date (1, 2) / month end (3).
"""

from __future__ import annotations

import gzip
import re
from functools import lru_cache
from typing import Any

import polars as pl

from app.core.config import Settings
from app.linkage.common import log, now_utc, settings_of, update_progress, write

MIN_COMENTIONS = 3
GENERIC_RX = re.compile(
    r"^(customer|customers|client|clients|major|significant|largest|top|other|others|all|one|two|three|four|five|"
    r"single|external|third|unaffiliated|intersegment|related|nonrelated|affiliated|distributor|distributors|reseller|"
    r"wholesaler|retailer|retailers|oem|end|key|principal|individual|commercial|domestic|international|foreign|"
    r"product|service|services|segment|total|remaining|various|largest|ten|no|none|any|each|several)\b|"
    r"\b(customer|customers|client|clients)\s*[a-z0-9]?$|^[a-z]$|^(company|entity|party|counterparty)\s*[a-z0-9]$|"
    r"\b(segment|revenue|revenues|sales|member|concentration)$", re.I)
GOV_RX = re.compile(r"(government|governmental|department of|ministry|federal|agency|medicare|medicaid|"
                    r"\bdod\b|navy|army|air force|nasa|state of|municipal|commonwealth)", re.I)
REVENUE_BENCH = re.compile(r"ConcentrationRiskByBenchmark=(Revenue|SalesRevenueNet|Revenues|SalesRevenue|"
                           r"RevenueFromContractWithCustomer[A-Za-z]*|CostOfGoods[A-Za-z]*)?[^;]*;")

SUFFIX = (r"(?:Inc|Ltd|Limited|Co|Corp|Corporation|LLC|L\.L\.C|plc|PLC|N\.V|NV|S\.A|SA|AG|SE|L\.P|LP|GmbH|Company|"
          r"Holdings|Group)\.?")
NAME = (r"(?P<name>(?:[A-Z][\w&.'’\-]*)(?:\s+(?:[A-Z][\w&.'’\-]*|of|and|&|de|du|la|del)){0,6}"
        r"(?:,\s+" + SUFFIX + r")*)")
PCT = r"(?P<pct>\d{1,2}(?:\.\d+)?)\s?%"
VERB = r"(?:totaled|accounted\s+for|represented|comprised|constituted|amounted\s+to|were|was|made\s+up)"
OF_REV = (r"\s+of\s+(?:our\s+|the\s+company['’]s\s+|its\s+|[A-Z][\w]*['’]s\s+|total\s+|consolidated\s+|net\s+|"
          r"annual\s+|combined\s+|the\s+)*(?:net\s+)?(?:revenue|revenues|sales|net\s+sales|operating\s+revenues)")
TEXT_PATTERNS = [
    re.compile(r"(?:[Ss]ales|[Rr]evenues?|[Nn]et\s+sales)\s+(?:to|from)\s+" + NAME
               + r"(?:\s*\([^)]{1,60}\))?(?:,[^,.]{0,80},)?\s+" + VERB + r"\s+(?:approximately\s+|about\s+|roughly\s+)?"
               + PCT + OF_REV),
    re.compile(NAME + r"(?:\s*\([^)]{1,60}\))?,?\s+(?:our\s+largest\s+customer,\s+|which\s+is\s+our\s+largest\s+customer,\s+)?"
               + r"(?:which\s+)?" + r"(?:accounted\s+for|represented|comprised|constituted)\s+(?:approximately\s+|about\s+)?"
               + PCT + OF_REV),
]
BAD_FIRST = {"No", "None", "One", "Our", "The", "Customer", "Customers", "Sales", "Revenue", "Revenues", "Each", "Two",
             "Three", "Four", "Five", "This", "These", "In", "During", "For", "Collectively", "Approximately", "Total",
             "Fiscal", "Net", "Its", "Their", "Such", "A", "An", "Any", "All", "Other", "Products", "Product", "Services",
             "International", "Domestic", "Europe", "Asia", "North", "South", "United", "U.S", "U.S.", "Americas",
             "We", "It", "As", "At", "Of", "Segment", "Government", "Largest", "Top", "Direct", "Indirect", "Sale",
             "Distributors", "Distributor", "Retail", "Wholesale", "Company", "Business", "Group", "Consolidated",
             "Lease", "Leases", "Contract", "Contracts", "Interest", "Income", "Shipments", "Export", "Exports"}


def decamel(s: str) -> str:
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    s = re.sub(r"\b(Mc|Mac) (?=[A-Z])", r"\1", s)  # McKesson, MacDonald
    return re.sub(r"\d+$", "", s).strip()


#: customer-category words (XBRL MajorCustomers members are often markets, not companies)
CATEGORY_WORDS = set("""industrial industrials automotive auto residential consumer consumers medical private sector public
authority authorities aerospace defense defence military oilfield telecommunications telecom managed care cooperative
members member non nonmember commercial transportation pharmacy pharmacies provider providers owners owner electronics
energy utility utilities retail retailers wholesale wholesalers oem oems healthcare hospital hospitals education
institutional institutions agriculture agricultural construction mining marine rail trucking airline airlines
semiconductor semiconductors data center centers cloud enterprise enterprises small medium business businesses
mass merchant merchants channel channels dealer dealers partner partners grocery food beverage hyperscale hyperscalers
financial banks bank insurance insurers payers payer self pay patients patient individuals households u s us usa
international domestic foreign global north america americas europe asia pacific and of the other others total
unaffiliated affiliate affiliates entity entities related party parties third major significant large largest key
segment segments revenue revenues sales customer customers client clients market markets end user users
agencies agency payor payors carrier carriers reorder order orders new political excluding laboratory laboratories
strategic proprietary funds fund sponsored investment investments portfolio portfolios national security veterans
administration aso providers clinical cable program programs manager managers contractor contractors corporate
academic research government governments state local municipal school schools university universities nonprofit
consumers wholesale club clubs specialty department stores store chain chains independent independents
""".split())



@lru_cache(maxsize=1)
def _country_words() -> frozenset[str]:
    from app.linkage.members import COUNTRY_ALIASES, _geo_names  # noqa: PLC0415

    names, _ = _geo_names("")
    return frozenset(set(names) | set(COUNTRY_ALIASES))


def is_generic(name: str) -> bool:
    n = name.strip()
    if len(n) < 2 or bool(GENERIC_RX.search(n)) or n.split()[0] in BAD_FIRST:
        return True
    if re.fullmatch(SUFFIX, n) or re.fullmatch(r"[\W\d_]+", n):
        return True
    words = [w for w in re.split(r"[^a-z]+", n.lower()) if w]
    if words and all(w in CATEGORY_WORDS for w in words):
        return True
    cw = _country_words()
    toks = [re.sub(r"[^a-z]", "", w) for w in re.split(r"\s+(?:and|&)\s+|,\s*", n.lower())]
    return bool(toks) and all(tk in cw or tk in ("", "the") for tk in toks)


def dst_kind(name: str) -> str:
    return "government" if GOV_RX.search(name) else "company"


# ================================================================================================ linker
class Linker:
    def __init__(self, settings: Settings | None = None):
        from app.ingestion.gdelt_gkg import norm_org  # noqa: PLC0415
        from app.nlp.gdelt_link import OrgLinker  # noqa: PLC0415

        self.norm = norm_org
        try:
            self.ol = OrgLinker.from_reference(settings)
        except Exception as e:  # noqa: BLE001
            log.warning("OrgLinker unavailable: %s", e)
            self.ol = None
        self._cache: dict[str, tuple[str, str | None, float]] = {}
        self._method: dict[str, str] = {}

    def __call__(self, name: str) -> tuple[str, str | None, float]:
        """name -> (normalized name, issuer_id | None, confidence)."""
        if name in self._cache:
            return self._cache[name]
        n = self.norm(name)
        iid, conf, method = None, 0.0, "none"
        if self.ol is not None and n:
            iid, conf, method = self.ol.resolve(n)
        self._cache[name] = (n, iid, float(conf))
        self._method[name] = method if iid else "none"
        return self._cache[name]

    def method(self, name: str) -> str:
        """exact | fuzzy | ambiguous_multi_issuer | none (after __call__)."""
        if name not in self._method:
            self(name)
        return self._method.get(name, "none")


# ================================================================================================ 1. XBRL members
def xbrl_customers(settings: Settings | None = None) -> pl.DataFrame:
    from app.linkage.georev import REVENUE_TAGS, landed  # noqa: PLC0415

    lz = landed(settings)
    if lz is None:
        return pl.DataFrame()
    num, sub = lz
    # bounded memory (roadmap #9): stream only the MajorCustomers rows and the revenue totals of those filings
    seg = pl.col("segments").fill_null("")
    cols_needed = ["adsh", "tag", "ddate", "qtrs", "uom", "segments", "value"]
    mc = num.select(cols_needed).filter(seg.str.contains("MajorCustomers=")).collect(engine="streaming")
    adsh = mc.get_column("adsh").unique().implode()
    num = num.select(cols_needed).filter((seg == "") & pl.col("tag").is_in(REVENUE_TAGS) & pl.col("adsh").is_in(adsh)
                                         ).with_columns(seg.alias("segments")).collect(engine="streaming")
    mc = mc.with_columns(seg.alias("segments"))
    mc = mc.with_columns(pl.col("segments").str.extract(r"MajorCustomers=([^;]*);").alias("member_raw"),
                         pl.col("segments").str.replace_all(r"MajorCustomers=[^;]*;", "").alias("__rest"))
    # revenue by customer (only axis besides optional benchmark noise) / concentration % with a revenue benchmark
    rev = mc.filter(pl.col("tag").is_in(REVENUE_TAGS) & (pl.col("__rest") == ""))
    pct = mc.filter((pl.col("tag") == "ConcentrationRiskPercentage1")
                    & pl.col("__rest").str.replace_all(r"ConcentrationRiskByType=[^;]*;", "").str.contains(
                        r"^ConcentrationRiskByBenchmark=(Revenue|SalesRevenue|Revenues|RevenueFromContract)[A-Za-z]*;$"))
    tot = (num.filter((pl.col("segments") == "") & pl.col("tag").is_in(REVENUE_TAGS))
           .select("adsh", "tag", "ddate", "qtrs", "uom", pl.col("value").cast(pl.Float64, strict=False).alias("total"))
           .unique(["adsh", "tag", "ddate", "qtrs", "uom"]))
    rev = rev.join(tot, on=["adsh", "tag", "ddate", "qtrs", "uom"], how="left").with_columns(
        (pl.col("value").cast(pl.Float64, strict=False) / pl.col("total")).alias("share"))
    pct = pct.with_columns(pl.col("value").cast(pl.Float64, strict=False).alias("share"))
    cols = ["adsh", "tag", "ddate", "qtrs", "member_raw", "share"]
    df = pl.concat([rev.select(cols), pct.select(cols)], how="vertical_relaxed").filter(
        pl.col("share").is_not_null() & (pl.col("share") > 0) & (pl.col("share") <= 1.0))
    # keep annual / the widest period per filing and member; prefer the concentration percentage
    df = df.with_columns(pl.col("qtrs").cast(pl.Int32, strict=False)).sort(
        ["adsh", "member_raw", "ddate", "qtrs", "tag"], descending=[False, False, True, True, False]).unique(
        ["adsh", "member_raw"], keep="first")
    df = df.join(sub.select("adsh", "cik", "name", "filed", "form").collect(), on="adsh", how="left")
    rows = []
    for r in df.iter_rows(named=True):
        nm = decamel(r["member_raw"] or "")
        if not nm or is_generic(nm):
            continue
        rows.append({"source": "xbrl_fsds", "src_entity_id": str(r["cik"]).zfill(10), "src_name": r["name"],
                     "dst_name": nm, "share": float(r["share"]), "period_end": r["ddate"], "knowledge_date": r["filed"],
                     "filing_id": f"sec_edgar:{r['adsh']}", "evidence": f"{r['tag']} MajorCustomers={r['member_raw']}",
                     "form_type": r.get("form"), "rule": "xbrl_major_customers", "rule_conf": 1.0, "strict": False,
                     "metric": "revenue", "link_type": "customer",
                     "snippet": f"XBRL {r['tag']} MajorCustomers={r['member_raw']}"[:200]})
    out = pl.DataFrame(rows)
    if out.is_empty():
        return out
    return out.with_columns(pl.col("period_end").str.strptime(pl.Date, "%Y%m%d", strict=False),
                            pl.col("knowledge_date").str.strptime(pl.Date, "%Y%m%d", strict=False))


# ================================================================================================ 2. 10-K text
def extract_text_customers(text: str) -> list[tuple[str, float, str]]:
    """(name, share, sentence) for named >=1% customers in a filing text."""
    out: list[tuple[str, float, str]] = []
    t = " ".join(text.split())
    seen: set[str] = set()
    for rx in TEXT_PATTERNS:
        for m in rx.finditer(t):
            name = m.group("name").strip(" ,.;:'’\"").replace("’", "'")
            name = re.sub(r"^(?:[Tt]he|[Ss]ales to|[Rr]evenue from)\s+", "", name)
            name = re.sub(r"\s+(?:and|of|&)$", "", name)
            try:
                pct = float(m.group("pct"))
            except ValueError:
                continue
            if not (1 <= pct <= 100) or not name or is_generic(name) or name.lower() in seen:
                continue
            if len(name.split()) == 1 and len(name) <= 2:
                continue
            seen.add(name.lower())
            a, b = max(0, m.start() - 80), min(len(t), m.end() + 40)
            out.append((name, pct / 100.0, t[a:b]))
    return out


def text_customers(settings: Settings | None = None, forms: tuple[str, ...] = ("10-K", "10-K/A", "20-F")) -> pl.DataFrame:
    s = settings_of(settings)
    idx_p = s.raw_dir / "sec_text" / "_index.parquet"
    if not idx_p.exists():
        return pl.DataFrame()
    idx = pl.read_parquet(idx_p).filter((pl.col("section") == "full") & pl.col("form_type").is_in(list(forms)))
    rows = []
    for r in idx.iter_rows(named=True):
        p = s.data_dir / r["path"]
        try:
            with gzip.open(p, "rt", encoding="utf-8", errors="ignore") as fh:
                txt = fh.read()
        except OSError:
            continue
        for name, share, ev in extract_text_customers(txt):
            rows.append({"source": "text_10k", "src_entity_id": str(r["cik"]).zfill(10), "src_name": None,
                         "dst_name": name, "share": share, "period_end": r["period_end"],
                         "knowledge_date": r["knowledge_date"], "filing_id": r["filing_id"], "evidence": ev[:400]})
    return pl.DataFrame(rows)


def text_links(settings: Settings | None = None) -> pl.DataFrame:
    """Roadmap #9 v2 rules (app.linkage.textrules) over the compact unit store (app.linkage.textunits), one unit
    part file (~40 filings) at a time. Falls back to the v1 sentence rules on the sec_text corpus when no units exist."""
    from app.linkage.textrules import RULES_VERSION, extract_links  # noqa: PLC0415
    from app.linkage.textunits import units_dir  # noqa: PLC0415

    s = settings_of(settings)
    files = sorted(units_dir(s).glob("units_*.parquet"))
    if not files:
        df = text_customers(s)
        if df.is_empty():
            return df
        return df.with_columns(pl.lit("customer").alias("link_type"), pl.lit("text_v1").alias("rule"),
                               pl.lit(0.85).alias("rule_conf"), pl.lit(False).alias("strict"),
                               pl.lit("revenue").alias("metric"), pl.col("evidence").str.slice(0, 200).alias("snippet"))
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in reversed(files):           # newest part wins when a filing was re-processed
        u = pl.read_parquet(f).sort(["filing_id", "unit_no"])
        for (fid,), g in u.group_by(["filing_id"], maintain_order=True):
            if fid in seen:
                continue
            seen.add(fid)
            meta = g.row(0, named=True)
            for m in extract_links(g["text"].to_list()):
                rows.append({"source": "text_10k", "link_type": m.link_type, "src_entity_id": str(meta["cik"]).zfill(10),
                             "src_name": None, "dst_name": m.name, "share": m.share,
                             "period_end": meta["period_end"] or meta["knowledge_date"],
                             "knowledge_date": meta["knowledge_date"], "filing_id": fid, "form_type": meta["form_type"],
                             "evidence": m.snippet, "snippet": m.snippet, "rule": f"{m.rule}", "rule_conf": m.rule_conf,
                             "strict": m.strict, "metric": m.metric, "rules_version": RULES_VERSION})
        del u
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows, infer_schema_length=None)


# ================================================================================================ 3. GDELT co-mentions
def gdelt_comentions(settings: Settings | None = None, min_count: int = MIN_COMENTIONS) -> pl.DataFrame:
    """Monthly co-mention edges, computed ONE year_month partition at a time (bounded memory: the article-label lake
    grows with the GDELT backfill; a single all-months self-join spilled > 30 GB)."""
    import duckdb  # noqa: PLC0415

    s = settings_of(settings)
    root = s.data_dir / "normalized" / "gdelt_article_labels"
    lab = s.data_dir / "reference" / "event_labels"
    if not root.exists() or not lab.exists():
        return pl.DataFrame()
    months = sorted(p for p in root.glob("year_month=*") if p.is_dir())
    con = duckdb.connect()
    con.execute("SET memory_limit = '1500MB'; SET threads = 4; SET preserve_insertion_order = false")
    out = []
    try:
        con.execute(f"""CREATE TEMP TABLE labs AS SELECT id, code FROM read_parquet('{lab.as_posix()}/**/*.parquet',
                        hive_partitioning=1, union_by_name=1) WHERE dict_type = 'issuer'""")
        for mp in months:
            files = (mp / "**" / "*.parquet").as_posix()
            try:
                con.execute(f"""CREATE OR REPLACE TEMP TABLE a AS
                    SELECT row_number() OVER () AS rid, last_day(date) AS m, list_distinct(issuer_ids) AS ids
                    FROM read_parquet('{files}', union_by_name=1) WHERE len(issuer_ids) >= 1""")
                df = con.sql(f"""
                    WITH u AS (SELECT rid, m, unnest(ids) AS i FROM a WHERE len(ids) BETWEEN 2 AND 6),
                    pairs AS (SELECT u1.m, u1.i, u2.i AS j, count(*) AS n FROM u u1 JOIN u u2
                              ON u1.rid = u2.rid AND u1.i < u2.i GROUP BY ALL HAVING count(*) >= {int(min_count)}),
                    solo AS (SELECT m, i, count(*) AS n FROM (SELECT m, unnest(ids) AS i FROM a) GROUP BY m, i)
                    SELECT pairs.m AS period_end, li.code AS src, lj.code AS dst, pairs.n,
                           pairs.n / sqrt(si.n * sj.n) AS weight
                    FROM pairs JOIN solo si ON si.m = pairs.m AND si.i = pairs.i
                               JOIN solo sj ON sj.m = pairs.m AND sj.i = pairs.j
                               JOIN labs li ON li.id = pairs.i JOIN labs lj ON lj.id = pairs.j""").pl()
            except Exception as e:  # noqa: BLE001 - partition mid-write by the backfill: skip this month
                log.warning("gdelt co-mentions %s: %s", mp.name, e)
                continue
            if not df.is_empty():
                out.append(df)
        con.execute("DROP TABLE IF EXISTS a")
    except Exception as e:  # noqa: BLE001 - events_v2 tables mid-backfill / absent
        log.warning("gdelt co-mentions: %s", e)
    finally:
        con.close()
    return pl.concat(out, how="vertical_relaxed") if out else pl.DataFrame()


# ================================================================================================ build
def cik_to_issuer(settings: Settings | None = None) -> dict[str, str]:
    from app.linkage.georev import issuer_ids  # noqa: PLC0415

    ids = issuer_ids(settings)
    return {} if ids is None else dict(ids.select("entity_id", "issuer_id").iter_rows())


def build_links(settings: Settings | None = None, text: bool = True, gdelt: bool = True) -> dict[str, Any]:
    s = settings_of(settings)
    link = Linker(s)
    c2i = cik_to_issuer(s)
    parts = [xbrl_customers(s)]
    if text:
        parts.append(text_links(s))
    cust = pl.concat([p for p in parts if not p.is_empty()], how="diagonal_relaxed") if any(
        not p.is_empty() for p in parts) else pl.DataFrame()
    frames = []
    rep: dict[str, Any] = {}
    if not cust.is_empty():
        names = cust["dst_name"].to_list()
        res = [link(n) for n in names]
        cust = cust.with_columns(
            pl.Series("dst_norm", [r[0] for r in res], pl.Utf8), pl.Series("dst_issuer_id", [r[1] for r in res], pl.Utf8),
            pl.Series("match_confidence", [r[2] if r[1] else 0.0 for r in res], pl.Float64),
            pl.Series("match_method", [link.method(n) for n in names], pl.Utf8),
            pl.Series("dst_kind", [dst_kind(n) for n in names], pl.Utf8),
            pl.col("src_entity_id").replace_strict(c2i, default=None, return_dtype=pl.Utf8).alias("src_issuer_id"),
            pl.col("rule_conf").fill_null(1.0), pl.col("strict").fill_null(False))
        # strict (list / weak-context) mentions survive only with an exact name -> issuer match
        n0 = cust.height
        cust = cust.filter(~pl.col("strict") | (pl.col("match_method") == "exact"))
        rep["text_strict_dropped"] = n0 - cust.height
        cust = cust.filter(pl.col("dst_norm").str.len_chars() >= 2).with_columns(
            pl.when(pl.col("dst_kind") == "government").then(pl.lit(None, pl.Utf8)).otherwise(pl.col("dst_issuer_id"))
            .alias("dst_issuer_id"))
        cust = cust.filter(pl.col("dst_issuer_id").is_null() | (pl.col("dst_issuer_id") != pl.col("src_issuer_id"))
                           | pl.col("src_issuer_id").is_null())
        cust = cust.with_columns(
            pl.coalesce(pl.col("dst_issuer_id"), pl.lit("name:") + pl.col("dst_norm")).alias("dst_key"),
            pl.col("link_type").fill_null("customer"),
            pl.col("share").alias("weight"), pl.lit(None, pl.Int32).alias("n_mentions"),
            pl.when(pl.col("dst_issuer_id").is_not_null()).then(pl.col("match_confidence") * pl.col("rule_conf"))
            .otherwise(0.0).alias("confidence"),
            pl.col("snippet").str.slice(0, 200), pl.col("filing_id").fill_null(""))
        frames.append(cust)
        for lt in ("customer", "supplier"):
            c = cust.filter(pl.col("link_type") == lt)
            rep[f"{lt}_links"] = c.height
            rep[f"{lt}_links_resolved"] = c.filter(pl.col("dst_issuer_id").is_not_null()).height
        rep["customer_src_companies"] = cust["src_entity_id"].n_unique()
        rep["by_source"] = dict(cust.group_by("source").len().iter_rows())
        rep["by_rule"] = dict(cust.group_by("rule").len().iter_rows())
    if gdelt:
        g = gdelt_comentions(s)
        if not g.is_empty():
            g = g.select(pl.lit("gdelt").alias("source"), pl.lit("co_mention").alias("link_type"),
                         pl.col("src").alias("src_entity_id"), pl.col("src").alias("src_issuer_id"),
                         pl.col("dst").alias("dst_key"), pl.col("dst").alias("dst_issuer_id"),
                         pl.lit("company").alias("dst_kind"), pl.col("n").cast(pl.Int32).alias("n_mentions"),
                         pl.col("weight").cast(pl.Float64), pl.lit(None, pl.Float64).alias("share"),
                         pl.lit(0.5).alias("confidence"), pl.col("period_end").cast(pl.Date),
                         pl.col("period_end").cast(pl.Date).alias("knowledge_date"), pl.lit("").alias("filing_id"),
                         pl.lit("gdelt_comention").alias("rule"))
            frames.append(g)
        rep["comention_edges"] = g.height
    if not frames:
        return rep
    df = pl.concat(frames, how="diagonal_relaxed").with_columns(pl.lit(now_utc()).alias("ingested_at")).drop(
        "strict", strict=False)
    df = df.sort("confidence", descending=True).unique(
        ["link_type", "source", "src_entity_id", "dst_key", "period_end", "filing_id"], keep="first")
    rep["rows"] = write(df, "company_links", s, mode="overwrite")
    return rep


def run(ctx: Any) -> Any:
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    rep = build_links(ctx.settings, text=str(p.get("text", "true")).lower() != "false",
                      gdelt=str(p.get("gdelt", "true")).lower() != "false")
    update_progress("A6", "done" if rep.get("rows") else "partial", settings=ctx.settings, **rep)
    return JobResult(status="success", rows_written={"company_links": rep.get("rows", 0)}, message=str(rep))
