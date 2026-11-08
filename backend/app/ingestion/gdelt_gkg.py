"""GDELT GKG 2.1 (English + translingual 15-minute files) -> organisation mentions of universe issuers + news_items.
Job `gdelt_gkg`.

Per research §7.2/7.3: ~192 files/day (~1.4 GB zipped). Each file is downloaded into memory, unzipped, parsed with
Polars (only the 10 needed columns), pre-filtered with a vectorised Aho-Corasick scan (`str.contains_any`) of
V1Organizations against the organisation-name dictionary, then exploded and matched EXACTLY on normalised names.
Raw zips are discarded (optional <=7-day buffer via keep_raw). Work runs in a thread pool (Polars releases the GIL;
default 12 workers) so RAM stays bounded (~1-2 GB).

Dictionary (`load_org_dictionary`): issuers.name/short_name/aliases + identifier_map gdelt_org_name rows when
ref-identity has published them; otherwise the seed universe names (app.normalize.prices.SEED_*) plus curated
aliases (google -> Alphabet, facebook -> Meta, ...).

Outputs
* data/raw/gdelt_gkg/org_mentions/dt=YYYY-MM-DD/part-<n>.parquet — one row per (GKG record, matched org) with
  tone/positive/negative/polarity/word_count, themes, ISO2 countries, page title, language, issuer_id/ticker
  (no contract table yet -> proposal `gdelt_org_mentions` logged in docs/CONTRACT_CHANGES.md; signals-alt's
  gdelt_company_daily can aggregate these files).
* news_items (source_id=gdelt_gkg): one row per matched article URL with a PAGE_TITLE (dedup by URL hash; existing
  item_ids are never rewritten so sentiment columns filled later are preserved).
"""

from __future__ import annotations

import hashlib
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import polars as pl

from app.core.config import Settings, get_settings
from app.core.logsetup import get_logger
from app.core.storage import scan_table, write_table
from app.ingestion.gdelt_events import FIPS_TO_ISO2, GdeltFiles, read_tsv, slot_times, slot_url
from app.normalize.prices import SEED_GLOBAL, SEED_US, _table_has_data

log = get_logger(__name__)

GKG_COLS = ["GKGRECORDID", "DATE", "SourceCollectionIdentifier", "SourceCommonName", "DocumentIdentifier", "V1Counts",
            "V21Counts", "V1Themes", "V2EnhancedThemes", "V1Locations", "V2EnhancedLocations", "V1Persons",
            "V2EnhancedPersons", "V1Organizations", "V2EnhancedOrganizations", "V15Tone", "V21EnhancedDates", "V2GCAM",
            "V21SharingImage", "V21RelatedImages", "V21SocialImageEmbeds", "V21SocialVideoEmbeds", "V21Quotations",
            "V21AllNames", "V21Amounts", "V21TranslationInfo", "V2ExtrasXML"]
_USE = ["GKGRECORDID", "DATE", "SourceCommonName", "DocumentIdentifier", "V1Themes", "V1Locations", "V1Organizations",
        "V15Tone", "V21TranslationInfo", "V2ExtrasXML"]
_USE_IDX = [GKG_COLS.index(c) for c in _USE]

_SUFFIX_RE = (r"(?:\s(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|sa|ag|nv|se|asa|ab|spa|"
              r"s a|n v|a g|s p a|a s|s e|sab de c v|s a b de c v|holdings?|group|the))+$")

# curated aliases for the seed universe (GDELT org names are lower-case surface forms)
SEED_ALIASES: dict[str, list[str]] = {
    "GOOGL": ["google", "alphabet"], "META": ["facebook", "meta platforms"], "BRK-B": ["berkshire hathaway"],
    "JPM": ["jpmorgan", "jp morgan", "jpmorgan chase"], "XOM": ["exxonmobil", "exxon mobil", "exxon"],
    "JNJ": ["johnson johnson"], "PG": ["procter gamble"], "KO": ["coca cola", "coca-cola"],
    "AMD": ["advanced micro devices", "amd"], "GE": ["general electric", "ge aerospace"], "IBM": ["ibm"],
    "TMO": ["thermo fisher", "thermo fisher scientific"], "HD": ["home depot"], "MCD": ["mcdonald"],
    "SHEL.L": ["shell"], "HSBA.L": ["hsbc"], "BP.L": ["bp"], "GSK.L": ["gsk", "glaxosmithkline"],
    "MC.PA": ["lvmh"], "OR.PA": ["l oreal", "loreal"], "TTE.PA": ["totalenergies", "total energies"],
    "INGA.AS": ["ing group", "ing groep"], "NOVO-B.CO": ["novo nordisk"], "7203.T": ["toyota", "toyota motor"],
    "6758.T": ["sony"], "8306.T": ["mitsubishi ufj", "mufg"], "9984.T": ["softbank", "softbank group"],
    "005930.KS": ["samsung electronics", "samsung"], "000660.KS": ["sk hynix"], "600519.SS": ["kweichow moutai", "moutai"],
    "0700.HK": ["tencent"], "9988.HK": ["alibaba", "alibaba group"], "1299.HK": ["aia group", "aia"],
    "PETR4.SA": ["petrobras"], "VALE3.SA": ["vale"], "ITUB4.SA": ["itau unibanco", "itau"],
    "2330.TW": ["tsmc", "taiwan semiconductor", "taiwan semiconductor manufacturing"], "BHP.AX": ["bhp"],
    "RY.TO": ["royal bank of canada"], "AMXB.MX": ["america movil"], "WALMEX.MX": ["walmex", "walmart de mexico"],
    "NVDA": ["nvidia"], "MSFT": ["microsoft"], "AAPL": ["apple"], "AMZN": ["amazon"], "TSLA": ["tesla"],
    "SAP.DE": ["sap"], "ASML.AS": ["asml"], "AZN.L": ["astrazeneca"], "EDP.LS": ["edp", "energias de portugal"],
    "GALP.LS": ["galp"], "JMT.LS": ["jeronimo martins"], "SAN.MC": ["banco santander", "santander"],
}
_MIN_LEN_WHITELIST = {"bp", "sap", "ibm", "amd", "edp", "gsk", "bhp", "aia", "ing"}


def norm_org_expr(col: pl.Expr) -> pl.Expr:
    e = col.str.to_lowercase().str.replace_all(r"[^a-z0-9 ]", " ").str.replace_all(r"\s+", " ").str.strip_chars()
    e = e.str.replace(r"^the\s", "").str.replace(_SUFFIX_RE, "")
    return e.str.strip_chars()


def norm_org(s: str) -> str:
    return pl.select(norm_org_expr(pl.lit(s))).item()


def load_org_dictionary(settings: Settings | None = None) -> pl.DataFrame:
    """[org_norm, issuer_id, ticker, display] (one row per normalised alias)."""
    s = settings or get_settings()
    rows: list[dict[str, Any]] = []
    if _table_has_data("issuers", s):
        iss = scan_table("issuers", s).select("issuer_id", "name", "short_name", "aliases", "is_active").collect()
        for r in iss.iter_rows(named=True):
            if r["is_active"] is False:
                continue
            for n in [r["name"], r["short_name"], *(r["aliases"] or [])]:
                if n:
                    rows.append({"org": n, "issuer_id": r["issuer_id"], "ticker": None, "display": r["name"]})
    if _table_has_data("identifier_map", s):
        im = scan_table("identifier_map", s).filter(pl.col("id_type") == "gdelt_org_name").select(
            "id_value", "issuer_id").collect()
        rows += [{"org": r["id_value"], "issuer_id": r["issuer_id"], "ticker": None, "display": r["id_value"]}
                 for r in im.iter_rows(named=True)]
    if not rows:
        for t, n in SEED_US + SEED_GLOBAL:
            for alias in [n, *SEED_ALIASES.get(t, [])]:
                rows.append({"org": alias, "issuer_id": None, "ticker": t, "display": n})
    df = pl.DataFrame(rows, schema={"org": pl.Utf8, "issuer_id": pl.Utf8, "ticker": pl.Utf8, "display": pl.Utf8})
    df = df.with_columns(norm_org_expr(pl.col("org")).alias("org_norm")).filter(
        (pl.col("org_norm").str.len_chars() >= 4) | pl.col("org_norm").is_in(list(_MIN_LEN_WHITELIST)))
    # an alias that points at several issuers is ambiguous -> drop
    amb = df.group_by("org_norm").agg(pl.col("issuer_id").n_unique().alias("ni"), pl.col("ticker").n_unique().alias("nt"))
    bad = amb.filter((pl.col("ni") > 1) | (pl.col("nt") > 1)).get_column("org_norm").to_list()
    return df.filter(~pl.col("org_norm").is_in(bad)).unique(subset=["org_norm"], keep="first").select(
        "org_norm", "issuer_id", "ticker", "display")


def _lang_map() -> dict[str, str]:
    try:
        import pycountry  # noqa: PLC0415

        return {lng.alpha_3: lng.alpha_2 for lng in pycountry.languages if hasattr(lng, "alpha_2")}
    except Exception:  # pragma: no cover
        return {}


LANG3_TO_2 = _lang_map()


def parse_gkg(raw: bytes, dictionary: pl.DataFrame, translated: bool = False) -> pl.DataFrame:
    """One GKG file -> matched org-mention rows."""
    df = read_tsv(raw, GKG_COLS, _USE_IDX)
    if df.is_empty():
        return pl.DataFrame()
    patterns = dictionary.get_column("org_norm").to_list()
    orgs_lc = pl.col("V1Organizations").fill_null("").str.to_lowercase()
    pre = df.filter(orgs_lc.str.contains_any(patterns, ascii_case_insensitive=True))
    if pre.is_empty():
        return pl.DataFrame()
    ex = pre.with_columns(orgs_lc.str.split(";").alias("__org")).explode("__org").with_columns(
        norm_org_expr(pl.col("__org")).alias("org_norm")).join(dictionary, on="org_norm", how="inner")
    if ex.is_empty():
        return pl.DataFrame()
    ex = ex.unique(subset=["GKGRECORDID", "org_norm"], keep="first")
    tone = pl.col("V15Tone").str.split(",")
    countries = (pl.col("V1Locations").fill_null("").str.split(";")
                 .list.eval(pl.element().str.split("#").list.get(2, null_on_oob=True)
                            .replace_strict(FIPS_TO_ISO2, default=None, return_dtype=pl.Utf8))
                 .list.drop_nulls().list.unique())
    srclc = pl.col("V21TranslationInfo").str.extract(r"srclc:([a-z]{3})", 1)
    lang = (srclc.replace_strict(LANG3_TO_2, default=None, return_dtype=pl.Utf8) if translated
            else pl.lit("en"))
    out = ex.select(
        pl.col("GKGRECORDID").alias("gkg_record_id"),
        pl.col("DATE").str.to_datetime("%Y%m%d%H%M%S", strict=False, time_zone="UTC").alias("published_at"),
        pl.col("SourceCommonName").alias("publisher"), pl.col("DocumentIdentifier").alias("url"),
        "org_norm", "issuer_id", "ticker", "display",
        tone.list.get(0, null_on_oob=True).cast(pl.Float64, strict=False).alias("tone"),
        tone.list.get(1, null_on_oob=True).cast(pl.Float64, strict=False).alias("positive"),
        tone.list.get(2, null_on_oob=True).cast(pl.Float64, strict=False).alias("negative"),
        tone.list.get(3, null_on_oob=True).cast(pl.Float64, strict=False).alias("polarity"),
        tone.list.get(6, null_on_oob=True).cast(pl.Int32, strict=False).alias("word_count"),
        pl.col("V1Themes").fill_null("").str.split(";").list.filter(pl.element() != "").list.unique(maintain_order=True)
        .list.head(15).alias("themes"),
        countries.alias("countries"),
        pl.col("V2ExtrasXML").str.extract(r"<PAGE_TITLE>(.*?)</PAGE_TITLE>", 1).str.strip_chars().alias("title"),
        lang.alias("language"), pl.lit(translated).alias("translated"),
    )
    return out.filter(pl.col("published_at").is_not_null() & pl.col("url").is_not_null())


# ------------------------------------------------------------------------------------------------ news_items helpers


_DROP_QS = re.compile(r"^(utm_|fbclid|gclid|cmpid|ncid|ref$|cid$|guccounter)")


def normalize_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    q = "&".join(kv for kv in p.query.split("&") if kv and not _DROP_QS.match(kv.split("=", 1)[0].lower()))
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower() or "https", p.netloc.lower().removeprefix("www."), path, q, ""))


def item_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def existing_item_ids(year_months: list[str], settings: Settings | None = None) -> set[str]:
    if not year_months or not _table_has_data("news_items", settings):
        return set()
    return set(scan_table("news_items", settings).filter(pl.col("year_month").is_in(year_months))
               .select("item_id").collect().get_column("item_id").to_list())


def write_news(df: pl.DataFrame, settings: Settings | None = None) -> int:
    """Dedupe on item_id (within batch and against stored rows) and append-only upsert into news_items."""
    if df.is_empty():
        return 0
    df = df.unique(subset=["item_id"], keep="first")
    have = existing_item_ids(df.get_column("year_month").unique().to_list(), settings)
    if have:
        df = df.filter(~pl.col("item_id").is_in(list(have)))
    return write_table(df, "news_items", settings=settings) if df.height else 0


def mentions_to_news(m: pl.DataFrame) -> pl.DataFrame:
    """Org-mention rows -> news_items rows (one per article URL with a title)."""
    m = m.filter(pl.col("title").is_not_null() & (pl.col("title").str.len_chars() > 0))
    if m.is_empty():
        return pl.DataFrame()
    g = m.group_by("url").agg(
        pl.col("published_at").min(), pl.col("publisher").first(), pl.col("title").first(),
        pl.col("language").first(), pl.col("issuer_id").drop_nulls().unique().alias("issuer_ids"),
        pl.col("ticker").drop_nulls().unique().alias("tickers"), pl.col("display").unique().alias("entities"),
        pl.col("countries").first(), pl.col("themes").first(), pl.col("tone").first().alias("gdelt_tone"))
    ids = [item_id(u) for u in g.get_column("url").to_list()]
    return g.with_columns(
        pl.Series("item_id", ids, dtype=pl.Utf8),
        pl.col("published_at").dt.strftime("%Y-%m").alias("year_month"),
        pl.col("published_at").dt.date().alias("knowledge_date"),
        pl.lit("gdelt_gkg").alias("source_id"),
        pl.lit(None, pl.Utf8).alias("snippet"),
        pl.col("issuer_ids").list.eval(pl.element().is_not_null().cast(pl.Float64)).alias("link_confidence"),
        pl.lit(None, pl.Utf8).alias("provider_sentiment"),
        pl.lit(datetime.now(UTC)).alias("ingested_at"),
    )


# ------------------------------------------------------------------------------------------------ run


def run_gkg(settings: Settings | None = None, days: list[date] | None = None, translation: bool = True,
            workers: int = 12, write: bool = True, keep_raw: bool = False, max_files: int | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    t0 = time.perf_counter()
    dictionary = load_org_dictionary(s)
    gf = GdeltFiles(s, workers=workers, keep_raw=keep_raw)
    latest = gf.latest_slot()
    days = days or [(latest or datetime.now(UTC)).date() - timedelta(days=1)]
    jobs: list[tuple[str, bool]] = []
    for d in days:
        for ts in slot_times(d):
            if latest is not None and ts > latest:
                continue
            jobs.append((slot_url(ts, "gkg"), False))
            if translation:
                jobs.append((slot_url(ts, "translation.gkg"), True))
    if max_files:
        jobs = jobs[:max_files]
    trans = {u for u, t in jobs if t}
    res = gf.process([u for u, _ in jobs], lambda raw, u: parse_gkg(raw, dictionary, u in trans))
    res = [r for r in res if r.height]
    mentions = pl.concat(res, how="vertical_relaxed") if res else pl.DataFrame()
    rep: dict[str, Any] = {
        "days": [d.isoformat() for d in days], "files": len(jobs), "files_ok": gf.files_ok,
        "files_missing": len(gf.files_missing), "gb_downloaded": round(gf.bytes_downloaded / 1e9, 3),
        "dictionary_names": dictionary.height, "mention_rows": mentions.height,
        "download_thread_s": round(gf.download_s, 1), "parse_thread_s": round(gf.parse_s, 1),
    }
    if mentions.height:
        rep["articles_matched"] = mentions.get_column("url").n_unique()
        rep["top_orgs"] = dict(mentions.group_by("display").len().sort("len", descending=True).head(10).iter_rows())
        if write:
            for (d,), part in mentions.with_columns(pl.col("published_at").dt.date().alias("__d")) \
                    .partition_by("__d", as_dict=True).items():
                out = s.raw_dir / "gdelt_gkg" / "org_mentions" / f"dt={d.isoformat()}"
                out.mkdir(parents=True, exist_ok=True)
                for old in out.glob("*.parquet"):
                    old.unlink()
                part.drop("__d").write_parquet(out / "part-0.parquet", compression="zstd")
            news = mentions_to_news(mentions)
            rep["news_items_written"] = write_news(news, s)
    rep["seconds_total"] = round(time.perf_counter() - t0, 1)
    return rep


def run(ctx):  # noqa: ANN001 - JobContext
    from app.scheduler.base import JobResult  # noqa: PLC0415

    p = ctx.params or {}
    days = [date.fromisoformat(x) for x in p["days"]] if p.get("days") else None
    if days is None and not p.get("force"):  # events-v2 alias: gdelt_stream already wrote these outputs
        from app.events_v2.pipeline import legacy_done  # noqa: PLC0415

        y = datetime.now(UTC).date() - timedelta(days=1)
        if legacy_done(y, ctx.settings):
            return JobResult(status="success", watermark=y.isoformat(),
                             message=f"alias of gdelt_stream: legacy %s outputs for {y} already written" % "gdelt_gkg")
    rep = run_gkg(ctx.settings, days, translation=bool(p.get("translation", True)), workers=int(p.get("workers", 12)),
                  keep_raw=bool(p.get("keep_raw", False)))
    status = "success" if rep["files_ok"] else "failed"
    if rep["files_ok"] and rep["files_missing"]:
        status = "partial"
    return JobResult(status=status, rows_written={"news_items": rep.get("news_items_written", 0)},
                     entities_ok=rep["files_ok"], entities_failed=rep["files_missing"], http_requests=rep["files"],
                     watermark=max(rep["days"]), message=str({k: rep.get(k) for k in (
                         "gb_downloaded", "mention_rows", "articles_matched", "seconds_total")}))
