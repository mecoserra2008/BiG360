"""GDELT organisation linking + per-issuer daily GDELT aggregates (job `gdelt_company_daily`, owner signals-alt).

Linking (`OrgLinker`): GKG organisation surface forms -> issuer with a confidence:
  1. exact alias (issuer legal name / short name / aliases / identifier_map gdelt_org_name), normalised with the same
     rules as ingestion (app.ingestion.gdelt_gkg.norm_org_expr): legal name 0.97, other alias 0.93;
  2. rapidfuzz token_sort_ratio >= 92 against aliases of >= 6 characters (0.6 + 0.3 * (score - 92) / 8, max 0.9);
  3. ambiguous aliases (common words / brand names with non-company meanings: apple, shell, target, visa, amazon, meta,
     delta, gap, ...) get a low base confidence that is RAISED by corroborating context in the article title
     (company name, ticker, finance vocabulary, brand-specific cues such as iphone / instagram) and CUT to 0.05 by
     contradicting context (rainforest, immigration, missile, fruit, ...). GKG themes are used the same way.
  Links below MIN_CONFIDENCE (0.5) are kept in the mention rows but excluded from the aggregates.

Inputs: raw GKG org mentions written by ingest-market (data/raw/gdelt_gkg/org_mentions/dt=*/part-*.parquet; no
contract table yet), gdelt_events (filtered Events, already ISO2 via FIPS->ISO2 at ingest; FIPS "CH" = China -> "CN").
Outputs:
  * gdelt_company_daily: mentions, unique sources, tone mean/std, negative share, attention z (vs own 90 d), top
    themes, linked event ids, plus headline, country exposure (ISO2), event intensity, link confidence, coverage flag.
  * gdelt_events.issuer_ids (+ headline): events whose SOURCEURL is an article that mentions a linked issuer.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table
from app.nlp.common import has_data, rolling_z

log = get_logger(__name__)

MIN_CONFIDENCE = 0.5
FUZZY_MIN = 92.0

# alias -> base confidence when seen WITHOUT corroborating context
AMBIGUOUS: dict[str, float] = {
    "apple": 0.55, "shell": 0.55, "target": 0.3, "visa": 0.35, "amazon": 0.5, "meta": 0.3, "delta": 0.35,
    "gap": 0.2, "ball": 0.2, "block": 0.2, "square": 0.25, "oracle": 0.6, "ford": 0.55, "national bank": 0.2,
    "discovery": 0.3, "progressive": 0.25, "chase": 0.35, "mercury": 0.25, "genesis": 0.25, "pioneer": 0.25,
    "sky": 0.3, "orange": 0.35, "next": 0.15, "ally": 0.2, "united": 0.15, "american": 0.1, "general": 0.1,
    "sun": 0.15, "corning": 0.5, "alphabet": 0.6, "intel": 0.5, "unity": 0.25, "snap": 0.3, "match": 0.15,
    "zoom": 0.4, "lumen": 0.3, "crown": 0.2, "aflac": 0.9, "fortune": 0.15, "edison": 0.3, "carnival": 0.4,
    "domino": 0.4, "southern": 0.15, "dominion": 0.3, "eaton": 0.5, "sony": 0.85, "infosys": 0.95,
    "wells": 0.2, "jacobs": 0.3, "nike": 0.75, "tesla": 0.7, "microsoft": 0.95,
    # names that GKG mostly extracts from boilerplate ("follow us on Facebook", "according to The New York Times",
    # "listed on Nasdaq") rather than company news -> require the name / a brand cue in the title
    "facebook": 0.3, "google": 0.4, "youtube": 0.3, "new york times": 0.35, "washington post": 0.35, "nasdaq": 0.35,
    "london stock exchange": 0.4, "first national": 0.2, "mitsui": 0.4, "reuters": 0.2, "bloomberg": 0.2,
}
AMBIGUOUS.update({"shell": 0.35, "apple": 0.4})
FINANCE_CUES = re.compile(
    r"\b(shares?|stocks?|earnings|revenue|profit|quarter(ly)?|ceo|cfo|inc\.?|corp\.?|plc|ltd|investors?|analysts?|"
    r"dividend|ipo|nasdaq|nyse|market cap|guidance|outlook|acquisition|acquire[sd]?|merger|layoffs?|sales|"
    r"price target|downgrade[sd]?|upgrade[sd]?|buyback|lawsuit|antitrust|regulators?)\b", re.I)
BRAND_CUES: dict[str, re.Pattern[str]] = {
    "apple": re.compile(r"\b(iphone|ipad|mac|macbook|ios|app store|tim cook|cupertino|vision pro|airpods)\b", re.I),
    "meta": re.compile(r"\b(facebook|instagram|whatsapp|zuckerberg|threads|llama|oculus|quest|meta ai|reality labs)\b", re.I),
    "amazon": re.compile(r"\b(aws|prime|alexa|bezos|jassy|kindle|amazon\.com|whole foods|e-?commerce)\b", re.I),
    "visa": re.compile(r"\b(payments?|card|cardholders?|visa inc|mastercard|transactions?|fintech)\b", re.I),
    "target": re.compile(r"\b(retailer|stores?|target corp|shoppers?|brian cornell|black friday)\b", re.I),
    "shell": re.compile(r"\b(oil|gas|lng|energy|refiner(y|ies)|petrol|wael sawan|bp|exxon)\b", re.I),
    "delta": re.compile(r"\b(airlines?|flights?|airport|passengers?|delta air)\b", re.I),
    "oracle": re.compile(r"\b(cloud|database|larry ellison|oci|software)\b", re.I),
    "ford": re.compile(r"\b(cars?|vehicles?|ev|f-150|mustang|recall|auto(maker)?|uaw|jim farley)\b", re.I),
    "facebook": re.compile(r"\b(facebook|instagram|whatsapp|zuckerberg|meta platforms|threads)\b", re.I),
    "google": re.compile(r"\b(google|alphabet|gemini|android|youtube|pichai|waymo|chrome)\b", re.I),
    "intel": re.compile(r"\b(chips?|semiconductor|foundry|cpu|processor|gelsinger|lip-bu tan)\b", re.I),
}
CONTRA_CUES: dict[str, re.Pattern[str]] = {
    "amazon": re.compile(r"\b(rainforest|river|basin|deforestation|indigenous|tribes?|jungle|amaz[oô]nia)\b", re.I),
    "visa": re.compile(r"\b(immigra\w*|h-?1b|student visas?|work visas?|travel visas?|visa-free|embass(y|ies)|"
                       r"consulates?|deport\w*|golden visa|asylum|migrants?|passports?)\b", re.I),
    "target": re.compile(r"\b(missiles?|strikes?|military|troops|attacks?|killed|targeted|drones?|bomb\w*|shot)\b", re.I),
    "apple": re.compile(r"\b(fruit|orchards?|cider|pie|harvest|growers?)\b", re.I),
    "shell": re.compile(r"\b(shelling|artillery|shellfish|eggs?|turtle)\b", re.I),
    "delta": re.compile(r"\b(variant|covid|river delta|mekong|niger delta|nile delta)\b", re.I),
    "meta": re.compile(r"\b(meta-analysis|metadata|metaverse of)\b", re.I),
}
CONTRA_THEMES: dict[str, tuple[str, ...]] = {
    "amazon": ("ENV_DEFORESTATION", "ENV_FORESTS", "NATURAL_DISASTER"),
    "visa": ("IMMIGRATION", "REFUGEES", "BORDER"),
    "target": ("MILITARY", "ARMEDCONFLICT", "KILL"),
}


def _norm(s: str) -> str:
    from app.ingestion.gdelt_gkg import norm_org  # noqa: PLC0415

    return norm_org(s)


def clean_title(t: str | None) -> str | None:
    if t is None:
        return None
    t = html.unescape(html.unescape(t)).strip()
    return re.sub(r"\s+", " ", t) or None


_SLUG_EXT_RE = re.compile(r"\.(s?html?|php\d?|aspx?|jsp|cms|ece|amp|cfm)$", re.I)
# numeric / hex article ids ('26620982', '279362841', 'a1b2c3d4e5'), 'ar-AA1abcd'-style ids; short numbers ('9', '50k',
# '2026') are kept because they are usually part of the title
_SLUG_ID_RE = re.compile(r"^(?:\d{5,}|(?=[0-9a-f]*\d)[0-9a-f]{8,}|[a-z]{0,3}\d{5,}[a-z]{0,3}|aa(?=\w*\d)\w{4,}|id\d+)$", re.I)
_SLUG_WIRE_RE = re.compile(r"^(?:\d+(?:st|nd|rd|th)\s+)?(?:ld\s+)?writethru\s+", re.I)
_SMALL_WORDS = frozenset("a an and as at but by for from in into of on or over per the to vs via with".split())
_ACRONYMS = frozenset("us uk un eu usa uae nato imf ecb ceo cfo ipo ai gdp nhs fbi cia sec icc ev evs nba nfl "
                      "mlb bbc cnn opec g7 g20 rbi pbr esg".split())


def _title_case(words: list[str]) -> str:
    out = []
    for i, w in enumerate(words):
        lw = w.lower()
        if lw in _ACRONYMS:
            out.append(lw.upper())
        elif i and lw in _SMALL_WORDS:
            out.append(lw)
        elif w != lw:  # already mixed case in the URL (e.g. 'iPhone') -> keep
            out.append(w)
        else:
            out.append(lw[:1].upper() + lw[1:])
    return " ".join(out)


def _strip_date_prefix(words: list[str]) -> list[str]:
    """'2026-10-08-police-investigate' -> drop the leading YYYY MM [DD] date stamp some CMSs put in the slug."""
    if len(words) >= 2 and re.fullmatch(r"(?:19|20)\d{2}", words[0]) and re.fullmatch(r"\d{1,2}", words[1]):
        k = 3 if len(words) >= 3 and re.fullmatch(r"\d{1,2}", words[2]) else 2
        return words[k:]
    return words


def headline_from_url(url: str | None) -> str | None:
    """Readable title from a URL slug: '/news/26620982.pentagon-briefs-allies.html' -> 'Pentagon Briefs Allies'.

    Strips file extensions, numeric/hex article ids (also when glued with '.'), wire prefixes ('1st ld writethru'),
    splits on - _ + . separators and title-cases. Returns None when no path segment has >= 3 words."""
    if not url:
        return None
    try:
        path = unquote(urlsplit(url).path)
    except ValueError:
        return None
    segs = [s for s in path.split("/") if s]
    best: list[str] = []
    for s in segs:
        s2 = _SLUG_EXT_RE.sub("", s.strip())
        words = _strip_date_prefix([w for w in re.split(r"[-_+.,~:;!|]+", s2) if w and not _SLUG_ID_RE.match(w)])
        if len(words) >= 3 and len(" ".join(words)) > len(" ".join(best)):
            best = words
    if not best:
        return None
    text = _SLUG_WIRE_RE.sub("", " ".join(best)).strip()
    words = [w for w in text.split() if w]
    return _title_case(words)[:200] if len(words) >= 2 else None


# ================================================================================================ linker


class OrgLinker:
    """Alias table + confidence rules (see module docstring)."""

    def __init__(self, aliases: pl.DataFrame):
        # aliases: [org_norm, issuer_id, kind(legal|alias|gdelt), name, ticker]
        self.aliases = aliases.unique(subset=["org_norm", "issuer_id"], keep="first")
        amb = self.aliases.group_by("org_norm").agg(pl.col("issuer_id").n_unique().alias("n"))
        self._multi = set(amb.filter(pl.col("n") > 1).get_column("org_norm").to_list())
        first = self.aliases.sort("kind").unique("org_norm", keep="first")
        self._exact: dict[str, dict[str, Any]] = {r["org_norm"]: r for r in first.iter_rows(named=True)}
        self._fuzzy_keys = [k for k in self._exact if len(k) >= 6 and k not in self._multi]
        names = self.aliases.select("issuer_id", "name", "ticker").unique("issuer_id")
        self._issuer_names = {r["issuer_id"]: (r["name"] or "", r["ticker"] or "") for r in names.iter_rows(named=True)}

    @classmethod
    def from_reference(cls, settings: Settings | None = None) -> OrgLinker:
        s = settings or get_settings()
        rows: list[dict[str, Any]] = []
        if has_data("issuers", s):
            iss = scan_table("issuers", s).select("issuer_id", "name", "short_name", "aliases", "is_active").collect()
            from app.nlp.common import security_map  # noqa: PLC0415

            tick = dict(security_map(s).select("issuer_id", "ticker").iter_rows())
            for r in iss.iter_rows(named=True):
                if r["is_active"] is False:
                    continue
                for kind, n in [("a_legal", r["name"]), ("b_alias", r["short_name"]),
                                *[("b_alias", a) for a in (r["aliases"] or [])]]:
                    if n:
                        rows.append({"org": n, "issuer_id": r["issuer_id"], "kind": kind, "name": r["name"],
                                     "ticker": tick.get(r["issuer_id"])})
        if has_data("identifier_map", s):
            im = scan_table("identifier_map", s).filter(pl.col("id_type") == "gdelt_org_name") \
                .select("id_value", "issuer_id").collect()
            rows += [{"org": r["id_value"], "issuer_id": r["issuer_id"], "kind": "c_gdelt", "name": None, "ticker": None}
                     for r in im.iter_rows(named=True)]
        return cls.from_rows(rows)

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]]) -> OrgLinker:
        from app.ingestion.gdelt_gkg import norm_org_expr  # noqa: PLC0415

        schema = {"org": pl.Utf8, "issuer_id": pl.Utf8, "kind": pl.Utf8, "name": pl.Utf8, "ticker": pl.Utf8}
        df = pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
        df = df.with_columns(norm_org_expr(pl.col("org")).alias("org_norm")).filter(
            pl.col("org_norm").str.len_chars() >= 2)
        nm = df.filter(pl.col("name").is_not_null()).group_by("issuer_id").agg(pl.col("name").first().alias("__n"),
                                                                               pl.col("ticker").first().alias("__t"))
        df = df.join(nm, on="issuer_id", how="left").with_columns(
            pl.coalesce("name", "__n").alias("name"), pl.coalesce("ticker", "__t").alias("ticker")).drop("__n", "__t")
        return cls(df.select("org_norm", "issuer_id", "kind", "name", "ticker"))

    # ------------------------------------------------------------------ resolution
    @lru_cache(maxsize=200_000)  # noqa: B019 - linker objects are long-lived per job
    def resolve(self, org_norm: str) -> tuple[str | None, float, str]:
        """(issuer_id, base confidence, method) ignoring context."""
        if not org_norm:
            return None, 0.0, "none"
        if org_norm in self._multi:
            return None, 0.0, "ambiguous_multi_issuer"
        r = self._exact.get(org_norm)
        if r is not None:
            base = 0.97 if r["kind"] == "a_legal" else 0.93
            return r["issuer_id"], min(base, AMBIGUOUS.get(org_norm, base)), "exact"
        if len(org_norm) >= 6 and self._fuzzy_keys:
            from rapidfuzz import fuzz, process  # noqa: PLC0415

            m = process.extractOne(org_norm, self._fuzzy_keys, scorer=fuzz.token_sort_ratio, score_cutoff=FUZZY_MIN)
            if m:
                key, score, _ = m
                conf = min(0.9, 0.6 + 0.3 * (score - FUZZY_MIN) / (100 - FUZZY_MIN))
                return self._exact[key]["issuer_id"], min(conf, AMBIGUOUS.get(key, conf)), "fuzzy"
        return None, 0.0, "none"

    def confidence(self, org_norm: str, title: str | None = None, themes: list[str] | None = None,
                   issuer_id: str | None = None) -> tuple[str | None, float, str]:
        """Context-adjusted link: (issuer_id, confidence, method)."""
        iid, conf, method = self.resolve(org_norm)
        if iid is None and issuer_id is None:
            return None, 0.0, method
        if iid is None:  # matched by the ingestion dictionary only (e.g. alias since dropped from reference data)
            iid, conf, method = issuer_id, min(0.6, AMBIGUOUS.get(org_norm, 0.6)), "ingest_exact"
        t = (title or "").lower()
        if org_norm in AMBIGUOUS and AMBIGUOUS[org_norm] < 0.9:
            contra = CONTRA_CUES.get(org_norm)
            th = set(themes or [])
            if (contra and contra.search(t)) or any(any(x.startswith(c) for x in th)
                                                    for c in CONTRA_THEMES.get(org_norm, ())):
                return iid, 0.05, method + "+contra"
            name, ticker = self._issuer_names.get(iid, ("", ""))
            name_l = name.lower()
            cues = BRAND_CUES.get(org_norm)
            full_name = bool(name_l) and len(name_l) > len(org_norm) + 3 and _norm(name_l) != org_norm \
                and _norm(name_l) in _norm(t)
            tick = bool(ticker) and len(ticker) >= 2 and re.search(rf"(\$|\(|nyse:\s*|nasdaq:\s*){re.escape(ticker.lower())}\b", t)
            # the alias itself in the headline (multi-word or >= 6 chars; short words like 'target' are often verbs)
            in_title = (len(org_norm) >= 6 or " " in org_norm) and f" {org_norm} " in f" {_norm(t)} "
            if full_name or tick or in_title or (cues and cues.search(t)):
                return iid, max(conf, 0.9), method + "+context"
            if FINANCE_CUES.search(t) and org_norm in t:
                return iid, max(conf, 0.7), method + "+finance"
        return iid, conf, method


# ================================================================================================ mentions


def read_org_mentions(settings: Settings | None = None, start: date | None = None,
                      end: date | None = None) -> pl.DataFrame:
    s = settings or get_settings()
    root = s.raw_dir / "gdelt_gkg" / "org_mentions"
    files: list[Path] = []
    for d in sorted(root.glob("dt=*")) if root.exists() else []:
        try:
            dd = date.fromisoformat(d.name[3:])
        except ValueError:
            continue
        if (start and dd < start) or (end and dd > end):
            continue
        files += sorted(d.glob("*.parquet"))
    if not files:
        return pl.DataFrame()
    return pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")


def link_mentions(m: pl.DataFrame, linker: OrgLinker) -> pl.DataFrame:
    """Add link_issuer_id, link_confidence, link_method to org-mention rows (context = page title + themes)."""
    if m.is_empty():
        return m
    keys = m.select("org_norm", "title", "themes", "issuer_id").with_row_index("__r")
    out_i, out_c, out_m = [], [], []
    for r in keys.iter_rows(named=True):
        iid, conf, meth = linker.confidence(r["org_norm"], clean_title(r["title"]), r["themes"], r["issuer_id"])
        out_i.append(iid)
        out_c.append(conf)
        out_m.append(meth)
    return m.with_columns(pl.Series("link_issuer_id", out_i, pl.Utf8), pl.Series("link_confidence", out_c, pl.Float64),
                          pl.Series("link_method", out_m, pl.Utf8))


def _norm_url(u: pl.Expr) -> pl.Expr:
    return u.str.to_lowercase().str.replace(r"^https?://(www\.)?", "").str.replace(r"[?#].*$", "").str.strip_chars_end("/")


def company_daily(m: pl.DataFrame, events: pl.DataFrame | None = None, history: pl.DataFrame | None = None,
                  min_conf: float = MIN_CONFIDENCE) -> pl.DataFrame:
    """Linked mention rows -> gdelt_company_daily rows (one per date x issuer)."""
    if m.is_empty():
        return pl.DataFrame()
    m = m.with_columns(pl.col("published_at").dt.date().alias("date"))
    # coverage health per day (total mention rows vs trailing median) -- GDELT volume collapses are not "no news"
    vol = m.group_by("date").agg(pl.len().alias("__day_rows")).sort("date").with_columns(
        pl.col("__day_rows").rolling_median(30, min_samples=1).shift(1).alias("__med"))
    vol = vol.with_columns((pl.col("__med").is_null() | (pl.col("__day_rows") >= 0.5 * pl.col("__med")))
                           .alias("coverage_ok"))
    ln = m.filter(pl.col("link_issuer_id").is_not_null() & (pl.col("link_confidence") >= min_conf))
    if ln.is_empty():
        return pl.DataFrame()
    ln = ln.unique(subset=["gkg_record_id", "link_issuer_id"], keep="first").with_columns(
        pl.col("title").map_elements(clean_title, return_dtype=pl.Utf8).alias("title"),
        _norm_url(pl.col("url")).alias("__u"))
    agg = ln.group_by("date", "link_issuer_id").agg(
        pl.col("gkg_record_id").n_unique().cast(pl.Int32).alias("mentions"),
        pl.col("publisher").n_unique().cast(pl.Int32).alias("unique_sources"),
        pl.col("tone").mean().alias("avg_tone"), pl.col("tone").std().alias("tone_std"),
        (pl.col("tone") < -2).mean().alias("neg_share"),
        pl.col("themes").flatten().drop_nulls().alias("__th"),
        pl.col("countries").flatten().drop_nulls().alias("__cc"),
        pl.col("title").drop_nulls().alias("__titles"), pl.col("tone").alias("__tones"),
        pl.col("__u").unique().alias("__urls"),
        pl.col("link_confidence").mean().alias("link_confidence_mean"),
    ).rename({"link_issuer_id": "issuer_id"})

    def top_k(xs: list[str] | None, k: int, skip: tuple[str, ...] = ()) -> list[str]:
        c = Counter(x for x in (xs or []) if x and not x.startswith(skip))
        return [x for x, _ in c.most_common(k)]

    def headline(titles: list[str] | None) -> str | None:
        c = Counter(t for t in (titles or []) if t)
        return c.most_common(1)[0][0] if c else None

    agg = agg.with_columns(
        pl.col("__th").map_elements(lambda x: top_k(list(x), 5, ("TAX_", "WB_", "CRISISLEX_", "UNGP_")),
                                    return_dtype=pl.List(pl.Utf8)).alias("top_themes"),
        pl.col("__cc").map_elements(lambda x: top_k(list(x), 5), return_dtype=pl.List(pl.Utf8)).alias("country_exposure"),
        pl.col("__titles").map_elements(lambda x: headline(list(x)), return_dtype=pl.Utf8).alias("headline"),
    )
    if events is not None and events.height:
        ev = events.select(pl.col("global_event_id").cast(pl.Utf8).alias("__eid"), _norm_url(pl.col("source_url"))
                           .alias("__u"), "goldstein", "num_articles")
        ex = agg.select("date", "issuer_id", "__urls").explode("__urls").rename({"__urls": "__u"}).join(ev, on="__u")
        ea = ex.group_by("date", "issuer_id").agg(
            pl.col("__eid").unique().head(20).alias("linked_event_ids"),
            (pl.col("goldstein").abs().fill_null(0) * pl.col("num_articles").fill_null(1)).sum().log1p()
            .alias("event_intensity"))
        agg = agg.join(ea, on=["date", "issuer_id"], how="left")
    else:
        agg = agg.with_columns(pl.lit(None, pl.List(pl.Utf8)).alias("linked_event_ids"),
                               pl.lit(None, pl.Float64).alias("event_intensity"))
    agg = agg.join(vol.select("date", "coverage_ok"), on="date", how="left").drop(
        [c for c in agg.columns if c.startswith("__")])
    # attention z vs own trailing 90 rows (history = previously written days)
    cols = ["date", "issuer_id", "mentions"]
    base = agg.select(cols)
    if history is not None and history.height:
        base = pl.concat([history.select(cols).filter(~pl.struct("date", "issuer_id").is_in(
            agg.select(pl.struct("date", "issuer_id")).to_series().implode())), base], how="vertical_relaxed")
    base = base.sort(["issuer_id", "date"]).with_columns(pl.col("mentions").cast(pl.Float64).log1p().alias("__lm"))
    base = rolling_z(base, "issuer_id", "__lm", 90, "attention_z", min_periods=5)
    agg = agg.join(base.select("date", "issuer_id", "attention_z"), on=["date", "issuer_id"], how="left")
    return agg.with_columns(pl.col("date").dt.year().cast(pl.Int16).alias("year"))


def event_issuer_links(m: pl.DataFrame, events: pl.DataFrame, min_conf: float = MIN_CONFIDENCE) -> pl.DataFrame:
    """gdelt_events rows with issuer_ids (articles that mention linked issuers) and headline (GKG title or URL slug)."""
    if events.is_empty():
        return events
    ev = events.with_columns(_norm_url(pl.col("source_url")).alias("__u"))
    if m.height:
        ln = m.filter(pl.col("link_issuer_id").is_not_null() & (pl.col("link_confidence") >= min_conf)).with_columns(
            _norm_url(pl.col("url")).alias("__u"))
        lk = ln.group_by("__u").agg(pl.col("link_issuer_id").unique().sort().alias("__iss"))
        tt = m.with_columns(_norm_url(pl.col("url")).alias("__u")).filter(pl.col("title").is_not_null()) \
            .group_by("__u").agg(pl.col("title").first().alias("__title"))
        ev = ev.join(lk, on="__u", how="left").join(tt, on="__u", how="left")
    else:
        ev = ev.with_columns(pl.lit(None, pl.List(pl.Utf8)).alias("__iss"), pl.lit(None, pl.Utf8).alias("__title"))
    slug = ev.get_column("source_url").map_elements(headline_from_url, return_dtype=pl.Utf8)
    ev = ev.with_columns(
        pl.when(pl.col("__iss").is_not_null()).then(
            pl.concat_list([pl.col("issuer_ids").fill_null(pl.lit([], pl.List(pl.Utf8))), pl.col("__iss")]).list.unique()
            .list.sort()).otherwise(pl.col("issuer_ids")).alias("issuer_ids"),
        pl.coalesce(pl.col("__title").map_elements(clean_title, return_dtype=pl.Utf8), slug).alias("headline"))
    return ev.drop("__u", "__iss", "__title")


# ================================================================================================ job


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    s = ctx.settings
    p = ctx.params or {}
    lookback = int(p.get("lookback_days", 3))
    start = None if p.get("full") else ctx.as_of - timedelta(days=lookback)
    m = read_org_mentions(s, start, None)
    if m.is_empty():
        return JobResult(status="success", message="no GKG org mentions in window")
    linker = OrgLinker.from_reference(s)
    ctx.check_cancelled()
    m = link_mentions(m, linker)
    days = m.get_column("published_at").dt.date().unique().to_list()
    ev = pl.DataFrame()
    if has_data("gdelt_events", s):
        yms = sorted({d.strftime("%Y-%m") for d in days})
        ev = scan_table("gdelt_events", s).filter(pl.col("year_month").is_in(yms)).collect()
    hist = None
    if has_data("gdelt_company_daily", s):
        hist = scan_table("gdelt_company_daily", s).filter(pl.col("date") >= min(days) - timedelta(days=150)).select(
            "date", "issuer_id", "mentions").collect()
    daily = company_daily(m, ev.filter(pl.col("knowledge_date").is_in(days)) if ev.height else ev, hist)
    rows: dict[str, int] = {}
    if daily.height:
        daily = daily.with_columns(pl.lit(datetime.now(UTC)).alias("computed_at"))
        rows["gdelt_company_daily"] = write_table(daily, "gdelt_company_daily", settings=s)
    if ev.height and not p.get("skip_events"):
        upd = event_issuer_links(m, ev)
        rows["gdelt_events"] = write_table(upd, "gdelt_events", settings=s)
    n_link = m.filter(pl.col("link_confidence") >= MIN_CONFIDENCE).height
    msg = (f"mention_rows={m.height} linked={n_link} ({n_link / max(m.height, 1):.1%}) days={len(days)} "
           f"issuer_days={daily.height}")
    return JobResult(status="success", rows_written=rows, entities_ok=daily.height, message=msg)
